"""处理登记库（handle.db）的数据访问

一行 = 一条明细的后续处理登记，关联键为 detail_key（对应退回登记库的明细唯一键）。

**稀疏存储**：没处理过的明细在本库中没有行。因此：
* 判断「有没有处理」= 本表是否存在该 detail_key 的行；
* 跨库查询必须用 LEFT JOIN（见 core/repository.py 的 _DETAIL_FROM）。

**为什么单独成库**：ERP 处理属于「检测完了之后」的跟进环节，与检测结论无关
（也不参与完结状况判定）。2026-09-20 从检测登记库拆出，让检测登记页只放
送检后能判定的内容，处理跟进另开一页。

本库自带 dict_option 与 op_log，只汇总处理字段的候选值。
"""
import json
from datetime import datetime

from config import DICT_OPTION_LIMIT, HANDLE_DICT_FIELDS
from core.db import HANDLE_COLUMNS, InvalidField, get_conn, tx


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _escape_like(value: str) -> str:
    return (value.replace("\\", "\\\\")
                 .replace("%", "\\%")
                 .replace("_", "\\_"))


def _safe(field: str) -> str:
    """处理库字典字段白名单。"""
    if field not in HANDLE_DICT_FIELDS:
        raise InvalidField(f"非法处理字段: {str(field)[:60]}")
    return field


# ---------------------------------------------------------------------------
# 记录读写
# ---------------------------------------------------------------------------

def get(detail_key: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM handle_db.handle_records WHERE detail_key = ?;",
        (detail_key,),
    ).fetchone()
    return dict(row) if row else None


def exists(detail_key: str) -> bool:
    conn = get_conn()
    return conn.execute(
        "SELECT 1 FROM handle_db.handle_records WHERE detail_key = ?;",
        (detail_key,),
    ).fetchone() is not None


def count() -> int:
    conn = get_conn()
    return conn.execute(
        "SELECT COUNT(*) c FROM handle_db.handle_records;").fetchone()["c"]


def upsert(detail_key: str, data: dict, operator: str = "",
           refresh_dict: bool = True) -> int:
    """写入或更新一条处理记录（不存在则新建，保持稀疏）。

    `refresh_dict=False` 供**批量导入**用：字典重建是全表扫描，逐行调用会变成
    O(n²)（实测 3942 行要跑好几分钟）。批量场景传 False，收尾时手动调一次
    `refresh_dict_options()` —— 它本身是幂等的重建，中间不重建不影响最终结果。
    """
    payload = {k: v for k, v in (data or {}).items()
               if k in HANDLE_COLUMNS and k != "detail_key"}
    if not payload:
        return 0

    now = _now()
    cols = ["detail_key"] + list(payload.keys()) + ["created_at", "updated_at"]
    marks = ", ".join("?" for _ in cols)
    updates = ", ".join(f"`{k}` = VALUES(`{k}`)" for k in payload)
    with tx() as conn:
        conn.execute(
            f"""INSERT INTO handle_db.handle_records ({', '.join(cols)})
                VALUES ({marks})
                ON DUPLICATE KEY UPDATE
                    {updates}, updated_at = VALUES(updated_at);""",
            [detail_key] + list(payload.values()) + [now, now],
        )
        conn.execute(
            "INSERT INTO handle_db.op_log "
            "(action, detail_key, payload, operator, created_at) VALUES (?,?,?,?,?);",
            ("handle", detail_key,
             json.dumps(payload, ensure_ascii=False), operator, now),
        )

    if refresh_dict:
        refresh_dict_options()
    return 1


def delete(detail_key: str) -> int:
    """删除某明细的处理记录（退回登记记录被删除时同步清理）。"""
    with tx() as conn:
        cur = conn.execute(
            "DELETE FROM handle_db.handle_records WHERE detail_key = ?;",
            (detail_key,),
        )
        changed = cur.rowcount
    if changed:
        refresh_dict_options()
    return changed


def delete_many(detail_keys) -> int:
    """批量删除处理记录 —— 字典只重建一次（理由同 repo_inspect.delete_many）。"""
    keys = [str(k).strip() for k in (detail_keys or []) if str(k or "").strip()]
    if not keys:
        return 0
    marks = ", ".join("?" for _ in keys)
    with tx() as conn:
        cur = conn.execute(
            f"DELETE FROM handle_db.handle_records WHERE detail_key IN ({marks});",
            keys,
        )
        changed = cur.rowcount
    if changed:
        refresh_dict_options()
    return changed


# ---------------------------------------------------------------------------
# 字典（处理字段的候选值）
# ---------------------------------------------------------------------------

def _prune_field(conn, field: str) -> int:
    """删除已不存在于处理数据的候选值（墓碑清理）。"""
    _safe(field)
    cur = conn.execute(
        f"""DELETE FROM handle_db.dict_option
            WHERE field = ?
              AND value NOT IN (
                SELECT value FROM (
                  SELECT TRIM({field}) AS value, COUNT(*) AS n
                  FROM handle_db.handle_records
                  WHERE {field} IS NOT NULL AND TRIM({field}) <> ''
                  GROUP BY {field} ORDER BY n DESC LIMIT {int(DICT_OPTION_LIMIT)}
                ) g
              );""",
        (field,),
    )
    return cur.rowcount


def refresh_dict_options() -> int:
    """重建处理库的字典候选（先清理不再存在的值，再 upsert）。"""
    now = _now()
    pruned = 0
    with tx() as conn:
        for field in HANDLE_DICT_FIELDS:
            _safe(field)
            pruned += _prune_field(conn, field)
            rows = conn.execute(
                f"SELECT {field} AS v, COUNT(*) AS n "
                f"FROM handle_db.handle_records "
                f"WHERE {field} IS NOT NULL AND TRIM({field}) <> '' "
                f"GROUP BY {field} ORDER BY n DESC LIMIT ?;",
                (DICT_OPTION_LIMIT,),
            ).fetchall()
            for r in rows:
                conn.execute(
                    """INSERT INTO handle_db.dict_option (field, value, use_count, updated_at)
                       VALUES (?,?,?,?)
                       ON DUPLICATE KEY UPDATE
                         use_count = VALUES(use_count),
                         updated_at = VALUES(updated_at);""",
                    (field, str(r["v"]).strip(), r["n"], now),
                )

        # 回收：字段口径变更后，此前的候选永远不会再被读取，留着只会堆僵尸行
        if HANDLE_DICT_FIELDS:
            marks = ", ".join("?" for _ in HANDLE_DICT_FIELDS)
            conn.execute(
                f"DELETE FROM handle_db.dict_option "
                f"WHERE field NOT IN ({marks});",
                HANDLE_DICT_FIELDS,
            )
    return pruned


def get_dict_options(field: str = "") -> dict:
    """读取处理库的字典候选。

    带 field 时返回 `{"field": …, "options": […]}`，与其余两个库的
    `get_dict_options(field)` 结构一致 —— 否则 `GET /api/dict/{field}`
    会因字段归属不同而返回两种形状。
    """
    conn = get_conn()
    if field:
        _safe(field)
        rows = conn.execute(
            "SELECT value, use_count FROM handle_db.dict_option WHERE field = ? "
            "ORDER BY use_count DESC, value;", (field,),
        ).fetchall()
        return {"field": field, "options": [dict(r) for r in rows]}

    marks = ", ".join("?" for _ in HANDLE_DICT_FIELDS)
    rows = conn.execute(
        f"SELECT field, value, use_count FROM handle_db.dict_option "
        f"WHERE field IN ({marks}) ORDER BY field, use_count DESC;",
        HANDLE_DICT_FIELDS,
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["field"], []).append(
            {"value": r["value"], "use_count": r["use_count"]})
    return out


def distinct_values(field: str, keyword: str = "", limit: int = 300) -> list:
    """实时取处理字段的去重值（用于下拉搜索），不走字典快照。"""
    _safe(field)
    conn = get_conn()
    sql = (f"SELECT {field} AS v, COUNT(*) AS n "
           f"FROM handle_db.handle_records "
           f"WHERE {field} IS NOT NULL AND TRIM({field}) <> ''")
    params = []
    if keyword:
        sql += f" AND {field} LIKE ? ESCAPE '\\\\'"
        params.append(f"%{_escape_like(keyword)}%")
    sql += f" GROUP BY {field} ORDER BY n DESC LIMIT ?;"
    params.append(limit)
    return [{"value": r["v"], "use_count": r["n"]}
            for r in conn.execute(sql, params).fetchall()]
