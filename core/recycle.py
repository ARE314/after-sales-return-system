"""回收站 —— 删除先进这里，7 天内可还原。

为什么不是「软删除标记」
----------------------
业务表里**就是真的删掉**：列表 / 统计 / 字典候选的口径一个字都不用改。
软删除要改的地方多到无法验证 —— 几十个查询都得记得补 `WHERE deleted = 0`，
漏一处就是「删掉的数据还留在报表里」。回收站另起一张表存**整行快照**，
还原 = 把快照原样写回（连主键一起，`ledger_clear.return_id` 这类引用才不会断）。

一次删除 = 一条记录
------------------
批量删 100 条明细也只产生 1 条回收站记录（`row_count = 100`）——
它对应的是「用户点了一次删除」这一个动作，还原也该一次还原回去。

快照与删除写在同一个事务里
------------------------
`snapshot_*()` 都在调用方已经打开的 `tx()` 内执行（InnoDB 跨 schema 原子）。
要么「业务删除 + 入回收站」都成，要么都不成：不会出现「删了但没进回收站」，
那正是不可恢复的情形。

照片是搬走、不是删掉
------------------
明细的照片由 `core.photos.move_to_trash()` 搬进 `data/photos_trash/<回收站 id>/`，
还原时搬回原位，**彻底删除时才真删** —— 否则「记录还原了、图没了」，
而照片删除是不可逆的（`photos.delete_all` 直接 unlink）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from config import RECYCLE_DAYS, RECYCLE_PURGE_HOURS
from core import photos, sched
from core.db import get_conn, tx

# 回收站记录的类别 → 人看的名字。新增可回收对象时在这里加一项，
# 列表页的分类下拉与统计都会自动带上。
KIND_LABELS = {
    "return": "返件明细",
    "clear": "核销记录",
    "item": "匹配库物料",
}
KINDS = tuple(KIND_LABELS)


class _Abort(Exception):
    """事务内主动中止。

    `tx()` 只在**异常**时回滚；在 `with` 里直接 `return` 会走 commit ——
    还原到一半发现「目标表不存在」时那样就把前几张表写出去了（半截数据）。
    所以事务内的失败一律 raise，由 tx() 回滚后在外层转成失败结果。
    """

_PAGE_MAX = 200
_COL_CACHE: dict = {}

# 过期清理的节流：列表接口顺带清理，一分钟内不重复扫库
_last_purge = 0.0
_PURGE_MIN_GAP = 60.0
_ticker: "sched.Ticker | None" = None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _expires_at() -> str:
    days = max(1, int(RECYCLE_DAYS))
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _cut(text, size: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= size else s[:size - 1] + "…"


def _cols_of(conn, schema: str, table: str) -> list:
    """活表现在有哪些列。

    写回时按活表列过滤：快照里可能带了活表已经没有的列（有人手工改过表结构），
    照抄会整条还原失败 —— 少还原一列总比一条都回不来好。
    """
    key = f"{schema}.{table}"
    cached = _COL_CACHE.get(key)
    if cached is not None:
        return cached
    rows = conn.execute(
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position;",
        (schema, table)).fetchall()
    cols = [r["name"] for r in rows]
    _COL_CACHE[key] = cols
    return cols


# ---------------------------------------------------------------------------
# 入站：写入回收站（在调用方的事务里）
# ---------------------------------------------------------------------------

def add(conn, kind: str, ref_key: str, ref_label: str, summary: str,
        tables: list, operator: str = "", extra: dict | None = None) -> int:
    """在**调用方已经开启的事务里**写一条回收站记录，返回新 id。

    `tables` 是 `[(schema, table, [整行 dict, ...]), ...]`。
    """
    payload = {
        "tables": [{"schema": s, "table": t, "rows": [dict(r) for r in rows]}
                   for s, t, rows in tables if rows],
        "keys": [str(k) for k in (extra or {}).get("keys") or []],
        "photos": [],
        "extra": {k: v for k, v in (extra or {}).items() if k != "keys"},
    }
    row_count = sum(len(rows) for _s, _t, rows in tables)
    cur = conn.execute(
        "INSERT INTO recycle_bin (kind, ref_key, ref_label, summary, payload, "
        "row_count, operator, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?,?,?,?);",
        (kind, _cut(ref_key, 128), _cut(ref_label, 255), _cut(summary, 255),
         json.dumps(payload, ensure_ascii=False, default=str), row_count,
         _cut(operator, 64), _now(), _expires_at()))
    return int(cur.lastrowid or 0)


def _label_of(orders: list, keys: list, single: str) -> str:
    if len(orders) == 1:
        return orders[0]
    if orders:
        return f"{orders[0]} 等 {len(orders)} 单"
    return single or (keys[0] if keys else "")


def snapshot_returns(conn, rows, operator: str = "") -> int:
    """明细（含检测 / 处理登记行）入回收站。`rows` 是 returns 表的整行。"""
    rows = [dict(r) for r in (rows or []) if r]
    if not rows:
        return 0
    keys = [str(r.get("detail_key") or "") for r in rows]
    orders, seen = [], set()
    for r in rows:
        no = str(r.get("order_no") or "")
        if no and no not in seen:
            seen.add(no)
            orders.append(no)
    marks = ", ".join("?" for _ in keys)
    insp = conn.execute(
        f"SELECT * FROM inspect_db.inspect_records WHERE detail_key IN ({marks});",
        keys).fetchall()
    hand = conn.execute(
        f"SELECT * FROM handle_db.handle_records WHERE detail_key IN ({marks});",
        keys).fetchall()
    label = _label_of(orders, keys, keys[0] if keys else "")
    extra = {"keys": keys, "order_no": label}
    return add(
        conn, "return", _cut(",".join(keys), 128), label,
        f"售后单 {label} · {len(rows)} 行明细", 
        [("returns_db", "returns", rows),
         ("inspect_db", "inspect_records", insp),
         ("handle_db", "handle_records", hand)],
        operator, extra)


def snapshot_clear(conn, rows, operator: str = "") -> int:
    """核销记录（台账关联 / 清账）入回收站。"""
    rows = [dict(r) for r in (rows or []) if r]
    if not rows:
        return 0
    one = rows[0]
    label = (str(one.get("return_no") or "")
             or f"{one.get('turbine_vendor') or ''} / {one.get('project_site') or ''}")
    kind = "关联" if str(one.get("kind")) == "link" else "清账"
    return add(conn, "clear", str(one.get("id") or ""), label,
               f"{kind} {one.get('qty')} 件 · {one.get('reason') or ''}",
               [("delivery_db", "ledger_clear", rows)], operator,
               {"keys": [str(r.get("id")) for r in rows],
                "dimension": f"{one.get('turbine_vendor') or ''} / "
                             f"{one.get('project_site') or ''}"})


def snapshot_item(conn, rows, operator: str = "") -> int:
    """匹配库物料入回收站。"""
    rows = [dict(r) for r in (rows or []) if r]
    if not rows:
        return 0
    one = rows[0]
    label = str(one.get("material_no") or "")
    return add(conn, "item", label, label,
               f"物料 {label} · {one.get('product_name') or ''}",
               [("items_db", "item_master", rows)], operator,
               {"keys": [str(r.get("id")) for r in rows]})


def set_photos(item_id: int, entries: list) -> None:
    """把「搬进回收站的照片清单」写回 payload。

    照片只能在事务**提交之后**搬（文件 I/O 不该进数据库事务），
    所以清单是补写的。补写失败也不致命：文件还在原位，
    还原时按空清单处理即可（见 `photos.restore_from_trash`）。
    """
    if not entries:
        return
    try:
        conn = get_conn()
        row = conn.execute("SELECT payload FROM recycle_bin WHERE id = ?;",
                           (int(item_id),)).fetchone()
        if not row:
            return
        payload = json.loads(row["payload"] or "{}")
        payload["photos"] = entries
        conn.execute("UPDATE recycle_bin SET payload = ? WHERE id = ?;",
                     (json.dumps(payload, ensure_ascii=False, default=str),
                      int(item_id)))
    except Exception as exc:                                # noqa: BLE001
        print(f"[recycle] 照片清单补写失败（{item_id}）："
              f"{type(exc).__name__}: {exc}", flush=True)


# ---------------------------------------------------------------------------
# 出站：列表 / 详情 / 还原 / 彻底删除
# ---------------------------------------------------------------------------

def _days_left(expires_at: str) -> int:
    try:
        exp = datetime.strptime(str(expires_at), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0
    secs = (exp - datetime.now()).total_seconds()
    return max(0, int(secs // 86400) + (1 if secs % 86400 else 0))


def list_items(kind: str = "", keyword: str = "", page: int = 1,
               page_size: int = 50, include_restored: bool = True,
               date_from: str = "", date_to: str = "") -> dict:
    """回收站列表。顺手做一次过期清理（列表是最常打开的入口）。"""
    purge_expired()
    where, params = ["1=1"], []
    if kind in KIND_LABELS:
        where.append("kind = ?")
        params.append(kind)
    kw = str(keyword or "").strip()
    if kw:
        where.append("(ref_key LIKE ? OR ref_label LIKE ? OR summary LIKE ? "
                     "OR operator LIKE ?)")
        params.extend([f"%{kw}%"] * 4)
    if not include_restored:
        where.append("restored = 0")
    if date_from:
        where.append("created_at >= ?")
        params.append(f"{date_from} 00:00:00")
    if date_to:
        where.append("created_at <= ?")
        params.append(f"{date_to} 23:59:59")
    clause = " WHERE " + " AND ".join(where)

    size = max(1, min(_PAGE_MAX, int(page_size or 50)))
    total = int(get_conn().execute(
        f"SELECT COUNT(*) AS c FROM recycle_bin{clause};", params
    ).fetchone()["c"])
    pages = max(1, (total + size - 1) // size)
    cur_page = max(1, min(int(page or 1), pages))
    rows = get_conn().execute(
        f"SELECT id, kind, ref_key, ref_label, summary, row_count, operator, "
        f"created_at, expires_at, restored, restored_at, restored_by, "
        f"CHAR_LENGTH(payload) AS payload_size "
        f"FROM recycle_bin{clause} ORDER BY id DESC LIMIT ? OFFSET ?;",
        params + [size, (cur_page - 1) * size]).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["kind_label"] = KIND_LABELS.get(d["kind"], d["kind"])
        d["days_left"] = _days_left(d["expires_at"])
        d["expired"] = bool(d["days_left"] <= 0 and not d["restored"])
        items.append(d)
    return {"total": total, "page": cur_page, "page_size": size,
            "pages": pages, "rows": items,
            "retention_days": int(RECYCLE_DAYS),
            "kw": kw, "kind": kind}


def stats() -> dict:
    purge_expired()
    conn = get_conn()
    total = int(conn.execute("SELECT COUNT(*) AS c FROM recycle_bin;"
                             ).fetchone()["c"])
    alive = int(conn.execute(
        "SELECT COUNT(*) AS c FROM recycle_bin WHERE restored = 0;"
    ).fetchone()["c"])
    restored = total - alive
    by_kind = {r["kind"]: int(r["c"]) for r in conn.execute(
        "SELECT kind, COUNT(*) AS c FROM recycle_bin GROUP BY kind;").fetchall()}
    return {"total": total, "alive": alive, "restored": restored,
            "by_kind": {k: {"label": KIND_LABELS.get(k, k), "count": v}
                        for k, v in by_kind.items()},
            "retention_days": int(RECYCLE_DAYS),
            "purge_hours": int(RECYCLE_PURGE_HOURS)}


def get_item(item_id: int) -> dict | None:
    row = get_conn().execute("SELECT * FROM recycle_bin WHERE id = ?;",
                             (int(item_id),)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["kind_label"] = KIND_LABELS.get(d["kind"], d["kind"])
    d["days_left"] = _days_left(d["expires_at"])
    try:
        payload = json.loads(d.get("payload") or "{}")
    except ValueError:
        payload = {}
    d["tables"] = [{"schema": t.get("schema"), "table": t.get("table"),
                    "count": len(t.get("rows") or []),
                    "columns": sorted((t.get("rows") or [{}])[0].keys())
                    if t.get("rows") else []}
                   for t in payload.get("tables") or []]
    d["photos"] = len(payload.get("photos") or [])
    d["payload_json"] = d.get("payload")
    return d


def restore(item_id: int, operator: str = "") -> dict:
    """把这条回收站记录写回业务表。冲突（键已存在）时整条回滚。"""
    from core import dbapi                                  # 局部导入：避免环
    from core import repo_delivery, repo_handle, repo_inspect, repository
    item_id = int(item_id)
    restored: dict = {}
    payload: dict = {}
    ref_key = ""
    kind = ""
    conflict = ""
    try:
        with tx() as conn:
            row = conn.execute("SELECT * FROM recycle_bin WHERE id = ?;",
                               (item_id,)).fetchone()
            if not row:
                raise _Abort(f"回收站里没有这条记录：{item_id}")
            if row["restored"]:
                raise _Abort("这条记录已经还原过了")
            kind, ref_key = str(row["kind"]), str(row["ref_key"] or "")
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                raise _Abort("回收站记录已损坏（快照不是合法 JSON）") from None

            for tbl in payload.get("tables") or []:
                schema, table = str(tbl.get("schema") or ""), str(tbl.get("table") or "")
                rows = tbl.get("rows") or []
                cols = _cols_of(conn, schema, table)
                if not cols:
                    raise _Abort(f"目标表不存在：{schema}.{table}")
                for r in rows:
                    data = {k: v for k, v in dict(r).items() if k in cols}
                    if not data:
                        continue
                    names = ", ".join(f"`{c}`" for c in data)
                    marks = ", ".join("?" for _ in data)
                    conn.execute(
                        f"INSERT INTO {schema}.{table} ({names}) VALUES ({marks});",
                        list(data.values()))
                restored[f"{schema}.{table}"] = len(rows)

            conn.execute(
                "UPDATE recycle_bin SET restored = 1, restored_at = ?, "
                "restored_by = ? WHERE id = ?;",
                (_now(), _cut(operator, 64), item_id))
            # 还原动作本身也要留痕：删除有 op_log，还原没有就等于「东西自己回来了」
            conn.execute(
                "INSERT INTO op_log (action, detail_key, payload, operator, "
                "created_at) VALUES (?,?,?,?,?);",
                (f"recycle_restore_{kind}", _cut(ref_key, 128),
                 json.dumps(restored, ensure_ascii=False), operator, _now()))
    except _Abort as exc:
        return {"ok": False, "reason": str(exc)}
    except dbapi.IntegrityError as exc:
        text = str(exc)
        conflict = text.split("Duplicate entry", 1)[-1][:160] if "Duplicate" in text \
            else text[:160]
        return {"ok": False,
                "reason": f"还原失败：目标表里已经有了（{conflict}）。"
                          f"要么先删掉现在这条，要么放弃还原。"}
    except Exception as exc:                                # noqa: BLE001
        return {"ok": False, "reason": f"还原失败：{type(exc).__name__}: {exc}"}

    # —— 事务提交后的派生资产回收（文件 I/O 不进事务）——
    moved = photos.restore_from_trash(item_id, payload.get("photos") or [])
    try:
        if any("returns" in k for k in restored):
            rows = []
            for tbl in payload.get("tables") or []:
                if tbl.get("schema") == "returns_db":
                    rows = tbl.get("rows") or []
            for r in rows:
                if r.get("product_model"):
                    repository.upsert_model(r)
            repository.refresh_dict_options()
            repo_inspect.refresh_dict_options()
            repo_handle.refresh_dict_options()
        if any("ledger_clear" in k for k in restored):
            repo_delivery.invalidate_ledger_cache()
        if any("item_master" in k for k in restored):
            repository.refresh_dict_options()
    except Exception as exc:                                # noqa: BLE001
        print(f"[recycle] 还原后的派生数据刷新失败（{item_id}）："
              f"{type(exc).__name__}: {exc}", flush=True)

    return {"ok": True, "id": item_id, "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            "restored": restored, "photos_moved": moved,
            "ref_label": "", "operator": operator}


def purge_ids(ids) -> int:
    """彻底删除（不可还原）：先取照片目录再删记录，最后清磁盘。"""
    clean = []
    for i in ids or []:
        try:
            clean.append(int(i))
        except (TypeError, ValueError):
            continue
    if not clean:
        return 0
    marks = ", ".join("?" for _ in clean)
    with tx() as conn:
        conn.execute(f"DELETE FROM recycle_bin WHERE id IN ({marks});", clean)
    for i in clean:
        photos.purge_trash(i)
    return len(clean)


def purge_expired(force: bool = False) -> int:
    """清掉超过保留期的记录（含已还原的旧记录），连照片一起真删。

    `force=True` 供页面上的「清理过期」按钮用 —— 那个动作是用户明确点的，
    不能因为 60 秒节流而看起来「点了没反应」。
    """
    global _last_purge
    now = time.time()
    if not force and now - _last_purge < _PURGE_MIN_GAP:
        return 0
    _last_purge = now
    conn = get_conn()
    rows = conn.execute(
        "SELECT id FROM recycle_bin WHERE expires_at <> '' AND expires_at < ?;",
        (_now(),)).fetchall()
    ids = [int(r["id"]) for r in rows]
    if not ids:
        return 0
    n = purge_ids(ids)
    print(f"[recycle] 过保留期清理：{n} 条（照片一并删除）", flush=True)
    return n


def purge_all() -> int:
    conn = get_conn()
    ids = [int(r["id"]) for r in conn.execute(
        "SELECT id FROM recycle_bin;").fetchall()]
    if not ids:
        return 0
    return purge_ids(ids)


# ---------------------------------------------------------------------------
# 定时清理
# ---------------------------------------------------------------------------

def _run_purge() -> int:
    return purge_expired()


def start_scheduler() -> dict:
    """启动时先清一次，之后按 RECYCLE_PURGE_HOURS 小时的节拍清。幂等。"""
    global _ticker
    if _ticker is not None and _ticker.alive:
        return {"started": False, "reason": "已在运行"}
    try:
        purge_expired()
    except Exception as exc:                                # noqa: BLE001
        print(f"[recycle] 启动清理失败：{type(exc).__name__}: {exc}", flush=True)
    interval = max(1, int(RECYCLE_PURGE_HOURS)) * 3600
    state = {"last": time.time()}

    def due():
        if time.time() - state["last"] >= interval:
            return True, f"回收站过期清理（每 {int(RECYCLE_PURGE_HOURS)} 小时）"
        return False, ""

    def run():
        state["last"] = time.time()
        _run_purge()

    _ticker = sched.Ticker("recycle", due, run)
    _ticker.start()
    return {"started": True}


def stop_scheduler() -> None:
    global _ticker
    if _ticker is not None:
        _ticker.stop()
