"""数据开放接口 —— 设置项、数据取数与拉取日志

方向说明（2026-09-20 改造，2026-09-21 去掉金山专属部分）
-------------------------------------------------------
原来的设计是**本系统主动推送**到金山「服务器数据」表：本系统要持有金山侧
凭据、要维护推送队列、要处理冲突。现改为**反方向拉取**：

    外部系统的定时任务  ──HTTP──▶  本系统开放接口（独立端口）──▶  JSON/CSV

这样本系统不需要持有任何外部系统的凭据，也不需要出网；拉取节奏完全由对方
控制。代价是要把接口暴露出去，所以这里把「令牌 + 来源 IP 白名单 + 数据集
范围」三件事都做成可配置项，且默认只监听 127.0.0.1（见 config.OPEN_API_HOST）。

本模块只管**设置与数据**，HTTP 层在 open_api.py。
"""
import ipaddress
import json
import re
import secrets
from datetime import datetime

from config import MAIN_SCHEMA, OPEN_API_IPS, OPEN_API_SCOPES, OPEN_API_TOKEN
from core import dbapi
from core.db import get_conn, tx

# ---------------------------------------------------------------------------
# 可拉取的数据集
#
# 与业务模块一一对应，便于按需最小授权：只让金山拉「退回明细」时，
# 检测与处理库的数据就不必暴露。字段清单同步返回，供金山侧建表头。
# ---------------------------------------------------------------------------
DATASETS = {
    "detail": {
        "label": "明细汇总",
        # 跨三库拼成一张平表：金山侧一次拉取就能建表，不必自己做 VLOOKUP。
        # 特殊数据集，取数走 repository 的跨库骨架（见 fetch 里的分支）。
        "desc": "退回 + 检测 + 处理，一行一条明细（推荐）",
        "table": "returns",
        "joined": True,
    },
    "returns": {
        "label": "退回登记",
        "desc": "仅退回侧字段（检测 / 处理结论见各自数据集，按 detail_key 关联）",
        "table": "returns",
    },
    "inspect": {
        "label": "检测记录",
        "desc": "送检后才能确定的字段，按明细键关联",
        "table": "inspect_db.inspect_records",
    },
    "handle": {
        "label": "处理记录",
        "desc": "ERP 处理等事后跟进字段",
        "table": "handle_db.handle_records",
    },
    "items": {
        "label": "物料主档",
        "desc": "匹配数据库（物料主档）全量",
        "table": "items_db.item_master",
    },
}

DATASET_KEYS = list(DATASETS)


class BadSince(Exception):
    """`since` 不是可识别的时间格式。

    **刻意不继承 ValueError**：路由层用 `except ValueError` 把「数据集不存在」
    转成 404，而 since 的问题应该是 400。若继承 ValueError 会被那个 except
    抢先捕获，报成「数据集不存在」—— 调用方会去查数据集名，方向被带偏。
    """


# YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS（也接受 T 分隔）
_SINCE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2})(?::(\d{2}))?)?$")


def normalize_since(since: str) -> str:
    """校验并规范化增量起点。

    为什么必须校验：`since` 是直接参与**字符串比较**的（`updated_at > ?`），
    格式不对不会报错，只会静默给出错误结果 ——
      * `since=not-a-date` → 比较恒不成立 → 返回 0 条
        → 金山侧以为「没有新数据」，这批数据就永久漏掉了；
      * `since=' OR 1=1 --` → 比较恒成立 → 返回全量
        → 金山侧把整表重写一遍，目标表出现重复行。
    两种都是「看起来拉取成功了」的静默错误，极难自查，所以宁可 400
    让调用方立刻发现。（注：这两条都**不是** SQL 注入 —— 查询是参数化的，
    只是字符串比较的巧合结果。）
    """
    s = str(since or "").strip()
    if not s:
        return ""
    m = _SINCE_RE.match(s)
    if not m:
        raise BadSince(
            f"since 格式无法识别：{s[:40]!r} —— "
            f"应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM[:SS]")
    date, hm, sec = m.group(1), m.group(2), m.group(3)
    # 只给日期时补 00:00:00：原来靠字符串前缀比较，行为等价，但补全后
    # 响应体里的 since 是明确的完整时间，调用方回传时不会歧义。
    stamp = f"{date} {hm or '00:00'}:{sec or '00'}"
    # 正则只管形状，`2026-13-99` 这种也能匹配上 —— 再走一次日期解析，
    # 否则非法月份/日同样会变成「比较恒不成立、静默返回 0 条」。
    try:
        datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise BadSince(f"since 不是有效日期：{s[:40]!r}") from None
    return stamp


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 设置项
# ---------------------------------------------------------------------------

def get_token() -> str:
    """读取访问令牌。首次调用时生成并落库（config 里配了就用配置的）。"""
    row = get_conn().execute(
        "SELECT value FROM auth_db.setting WHERE `key` = 'open_api_token';"
    ).fetchone()
    if row and row["value"]:
        return row["value"]
    token = OPEN_API_TOKEN or ("ars_" + secrets.token_urlsafe(32))
    _set("open_api_token", token)
    return token


def rotate_token() -> str:
    """重置令牌（旧令牌立即失效）。"""
    token = "ars_" + secrets.token_urlsafe(32)
    _set("open_api_token", token)
    return token


def token_masked() -> str:
    """界面展示用：只露头尾，避免截图/投屏时整串外泄。"""
    t = get_token()
    if len(t) <= 12:
        return t
    return f"{t[:7]}{'*' * 10}{t[-4:]}"


def allowed_ips() -> list:
    raw = _get("open_api_ips")
    if raw is None:
        return list(OPEN_API_IPS)
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return []


def set_allowed_ips(ips) -> None:
    _set("open_api_ips", json.dumps([str(x).strip() for x in ips if
                                     str(x).strip()], ensure_ascii=False))


def client_allowed(ip: str) -> bool:
    """来源 IP 是否在白名单内。白名单为空 = 不限制。

    支持单 IP 与 CIDR 网段（如 203.0.113.0/24）；IPv4/IPv6 都走 ipaddress，
    非法条目直接跳过 —— 不能因为写错一条就把所有请求放行或全拦掉。
    """
    allow = allowed_ips()
    if not allow:
        return True
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    for item in allow:
        item = (item or "").strip()
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if "/" in item:
            if addr in net:
                return True
        elif addr == net.network_address:
            return True
    return False


def scopes() -> list:
    raw = _get("open_api_scopes")
    if raw is None:
        return [s for s in OPEN_API_SCOPES if s in DATASETS] or ["detail"]
    try:
        picked = [s for s in json.loads(raw) if s in DATASETS]
    except Exception:  # noqa: BLE001
        picked = []
    return picked


def set_scopes(items) -> list:
    picked = [s for s in (items or []) if s in DATASETS]
    _set("open_api_scopes", json.dumps(picked))
    return picked


def _get(key: str):
    row = get_conn().execute(
        "SELECT value FROM auth_db.setting WHERE `key` = ?;",
        (key,)).fetchone()
    return row["value"] if row else None


def _set(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO auth_db.setting(`key`, value, updated_at) "
            "VALUES (?,?,?) ON DUPLICATE KEY UPDATE "
            "value = VALUES(value), updated_at = VALUES(updated_at);",
            (key, value, _now()))


# ---------------------------------------------------------------------------
# 数据取数
# ---------------------------------------------------------------------------

def columns_of(dataset: str) -> list:
    """取数据集字段名。

    跨库表同样走 information_schema —— 这里必须带上 `table_schema` 才能
    定位到目标库，否则（只按表名查）会捞出别的库里同名表的列。
    """
    ds = DATASETS.get(dataset)
    if not ds:
        return []
    if ds.get("joined"):
        # 拼接视图：退回 → 检测 → 处理，同名键只保留一份
        from core.repository import HANDLE_COLUMNS, INSPECT_COLUMNS
        seen, cols = set(), []
        for c in (columns_of("returns") + list(INSPECT_COLUMNS)
                  + list(HANDLE_COLUMNS)):
            if c not in seen:
                seen.add(c)
                cols.append(c)
        return cols
    table = ds["table"]
    if "." in table:
        db, tbl = table.split(".", 1)
    else:
        db, tbl = MAIN_SCHEMA, table
    rows = get_conn().execute(
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? "
        "ORDER BY ordinal_position;", (db, tbl)).fetchall()
    return [r["name"] for r in rows]


def fetch(dataset: str, since: str = "", limit: int = 2000,
          offset: int = 0) -> dict:
    """按数据集取数。`since` 传上次拉取时间即增量（基于 updated_at）。

    统一按 updated_at 升序返回 —— 增量拉取时这个顺序最自然，
    也避免金山侧写入顺序与本地 id 顺序不一致。
    """
    ds = DATASETS.get(dataset)
    if not ds:
        raise ValueError(f"未知数据集：{dataset}")

    # 归一 / 校验 since（非法格式抛 BadSince → 路由层转 400）。
    # 放在数据集校验之后：数据集不存在应当先报 404，不该被 since 的问题盖住。
    since = normalize_since(since)

    if ds.get("joined"):
        return _fetch_joined(limit=limit, offset=offset, since=since)

    table = ds["table"]
    cols = columns_of(dataset)
    order_col = "updated_at" if "updated_at" in cols else "id"
    where, params = "", []
    if since and "updated_at" in cols:
        where = " WHERE updated_at > ?"
        params.append(since)
    elif since and "created_at" in cols:
        where = " WHERE created_at > ?"
        params.append(since)

    sql = (f"SELECT * FROM {table}{where} ORDER BY {order_col} ASC "
           f"LIMIT ? OFFSET ?;")
    rows = [dict(r) for r in
            get_conn().execute(sql, params + [limit, offset]).fetchall()]
    total = get_conn().execute(
        f"SELECT COUNT(*) c FROM {table}{where};", params).fetchone()["c"]
    return {
        "dataset": dataset, "label": ds["label"], "columns": cols,
        "rows": rows, "count": len(rows), "total": total,
        "since": since or "", "limit": limit, "offset": offset,
        "next_offset": (offset + len(rows)) if offset + len(rows) < total else 0,
        "fetched_at": _now(),
    }


def _fetch_joined(limit: int = 2000, offset: int = 0,
                  since: str = "") -> dict:
    """明细汇总：退回 LEFT JOIN 检测 LEFT JOIN 处理。

    复用 repository 的跨库骨架（`_DETAIL_FROM` / `_DETAIL_COLS`），
    不在这里另写一份 JOIN —— 那边一旦调整字段归属，这里自动跟上。
    """
    from core.repository import _DETAIL_COLS, _DETAIL_FROM

    cols = columns_of("detail")
    where, params = "", []
    if since:
        where = " WHERE r.updated_at > ?"
        params.append(since)
    sql = (f"SELECT {_DETAIL_COLS} {_DETAIL_FROM}{where} "
           f"ORDER BY r.updated_at ASC, r.id ASC LIMIT ? OFFSET ?;")
    rows = [dict(r) for r in
            get_conn().execute(sql, params + [limit, offset]).fetchall()]
    total = get_conn().execute(
        f"SELECT COUNT(*) c FROM returns r "
        f"LEFT JOIN inspect_db.inspect_records i ON i.detail_key = r.detail_key "
        f"LEFT JOIN handle_db.handle_records h ON h.detail_key = r.detail_key"
        f"{where};", params).fetchone()["c"]
    return {
        "dataset": "detail", "label": DATASETS["detail"]["label"],
        "columns": cols, "rows": rows, "count": len(rows), "total": total,
        "since": since or "", "limit": limit, "offset": offset,
        "next_offset": (offset + len(rows)) if offset + len(rows) < total else 0,
        "fetched_at": _now(),
    }


def dataset_list() -> list:
    out = []
    for key in DATASET_KEYS:
        try:
            n = get_conn().execute(
                f"SELECT COUNT(*) c FROM {DATASETS[key]['table']};"
            ).fetchone()["c"]
        except dbapi.Error:
            n = 0
        out.append({
            "key": key, "label": DATASETS[key]["label"],
            "desc": DATASETS[key]["desc"],
            "rows": n, "allowed": key in scopes(),
            "columns": columns_of(key),
        })
    return out


# ---------------------------------------------------------------------------
# 拉取日志
# ---------------------------------------------------------------------------

def log_pull(ip: str, dataset: str, endpoint: str, rows: int, ok: bool = True,
             message: str = "") -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO auth_db.open_api_log(created_at, ip, dataset, "
            "endpoint, `rows`, ok, message) VALUES (?,?,?,?,?,?,?);",
            (_now(), ip or "", dataset or "", endpoint or "", rows or 0,
             1 if ok else 0, message or ""))


def recent_pulls(limit: int = 100) -> list:
    rows = get_conn().execute(
        "SELECT * FROM auth_db.open_api_log ORDER BY id DESC LIMIT ?;",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def last_pull():
    row = get_conn().execute(
        "SELECT * FROM auth_db.open_api_log WHERE ok = 1 "
        "ORDER BY id DESC LIMIT 1;").fetchone()
    return dict(row) if row else None


def new_since_last_pull() -> int:
    """自上次成功拉取以来新增的退回明细条数（页面上的「待拉取」提示）。

    用 returns.created_at 与上次成功拉取的时间比对 —— 拉取失败时系统并不知道，
    用时间戳比用本地标记更接近真实情况。
    """
    last = last_pull()
    conn = get_conn()
    if not last or not last.get("created_at"):
        return conn.execute("SELECT COUNT(*) c FROM returns;").fetchone()["c"]
    return conn.execute(
        "SELECT COUNT(*) c FROM returns WHERE created_at > ?;",
        (last["created_at"],)).fetchone()["c"]


def status() -> dict:
    from config import OPEN_API_ENABLED, OPEN_API_HOST, OPEN_API_PORT
    last = last_pull()
    return {
        "enabled": OPEN_API_ENABLED,
        "host": OPEN_API_HOST,
        "port": OPEN_API_PORT,
        "public": OPEN_API_HOST not in ("127.0.0.1", "localhost"),
        "token_masked": token_masked(),
        "token_set": bool(get_token()),
        "ips": allowed_ips(),
        "ip_restricted": bool(allowed_ips()),
        "scopes": scopes(),
        "datasets": dataset_list(),
        "last_pull": last,
        "new_since_last_pull": new_since_last_pull(),
        "pull_total": get_conn().execute(
            "SELECT COUNT(*) c FROM auth_db.open_api_log;").fetchone()["c"],
        "examples": {
            "ping": "GET /api/open/ping",
            "returns": "GET /api/open/data/returns?since=&limit=2000",
            "scopes": "GET /api/open/datasets",
            "export": "GET /api/open/export/returns.csv?since=",
        },
    }


__all__ = [
    "DATASETS", "DATASET_KEYS", "get_token", "rotate_token", "token_masked",
    "allowed_ips", "set_allowed_ips", "client_allowed",
    "scopes", "set_scopes", "columns_of", "fetch", "dataset_list",
    "log_pull", "recent_pulls", "last_pull", "new_since_last_pull", "status",
]
