"""检测登记库（inspect.db）的数据访问

一行 = 一条明细的检测结果，关联键为 detail_key（对应退回登记库的明细唯一键）。

**稀疏存储**：没检测过的明细在本库中没有行。因此：
* 判断「有没有检测」= 本表是否存在该 detail_key 的行；
* 跨库查询必须用 LEFT JOIN（见 core/repository.py 的 _DETAIL_FROM）。

本库自带 dict_option 与 op_log，只汇总检测字段的候选值。
"""
import json
from datetime import datetime

from config import (COMPLETION_DONE, COMPLETION_FIELDS, DICT_OPTION_LIMIT,
                    INSPECT_DICT_FIELDS)
from core.db import INSPECT_COLUMNS, InvalidField, get_conn, tx


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def compute_completion(values: dict) -> str:
    """按规则判定完结状况 —— 「完结状况」是派生字段，不接受手工填写。

    `config.COMPLETION_FIELDS` 里的检测字段**全部有值**才判为已完结，
    否则存空值（= 未完结）。报告编号、照片证据、ERP处理不参与判定
    （见 `config.COMPLETION_EXCLUDED`）。
    """
    for field in COMPLETION_FIELDS:
        if not str((values or {}).get(field) or "").strip():
            return ""
    return COMPLETION_DONE


def _escape_like(value: str) -> str:
    return (value.replace("\\", "\\\\")
                 .replace("%", "\\%")
                 .replace("_", "\\_"))


def _safe(field: str) -> str:
    """检测库字典字段白名单。"""
    if field not in INSPECT_DICT_FIELDS:
        raise InvalidField(f"非法检测字段: {str(field)[:60]}")
    return field


# ---------------------------------------------------------------------------
# 记录读写
# ---------------------------------------------------------------------------

def get(detail_key: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM inspect_db.inspect_records WHERE detail_key = ?;",
        (detail_key,),
    ).fetchone()
    return dict(row) if row else None


def exists(detail_key: str) -> bool:
    conn = get_conn()
    return conn.execute(
        "SELECT 1 FROM inspect_db.inspect_records WHERE detail_key = ?;",
        (detail_key,),
    ).fetchone() is not None


def count() -> int:
    conn = get_conn()
    return conn.execute(
        "SELECT COUNT(*) c FROM inspect_db.inspect_records;").fetchone()["c"]


def upsert(detail_key: str, data: dict, operator: str = "") -> int:
    """写入或更新一条检测记录（不存在则新建，保持稀疏）。

    完结状况由本函数统一重算：先与库中现有值合并，再按规则判定 ——
    这样「只提交部分字段」的调用（如 PUT 只改检测结果）也能得到正确结论。
    """
    payload = {k: v for k, v in (data or {}).items()
               if k in INSPECT_COLUMNS and k != "detail_key"}
    # 完结状况是派生的，忽略外部传入值（前端即使能编辑也不会生效）
    payload.pop("completion", None)
    if not payload:
        return 0

    now = _now()
    with tx() as conn:
        cur = conn.execute(
            "SELECT * FROM inspect_db.inspect_records WHERE detail_key = ?;",
            (detail_key,),
        ).fetchone()
        merged = dict(cur) if cur else {}
        merged.update(payload)
        payload["completion"] = compute_completion(merged)

        cols = ["detail_key"] + list(payload.keys()) + ["created_at", "updated_at"]
        marks = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{k} = excluded.{k}" for k in payload)
        conn.execute(
            f"""INSERT INTO inspect_db.inspect_records ({', '.join(cols)})
                VALUES ({marks})
                ON CONFLICT(detail_key) DO UPDATE SET
                    {updates}, updated_at = excluded.updated_at;""",
            [detail_key] + list(payload.values()) + [now, now],
        )
        conn.execute(
            "INSERT INTO inspect_db.op_log "
            "(action, detail_key, payload, operator, created_at) VALUES (?,?,?,?,?);",
            ("inspect", detail_key,
             json.dumps(payload, ensure_ascii=False), operator, now),
        )

    refresh_dict_options()
    return 1


def delete(detail_key: str) -> int:
    """删除某明细的检测记录（退回登记记录被删除时同步清理）。"""
    with tx() as conn:
        cur = conn.execute(
            "DELETE FROM inspect_db.inspect_records WHERE detail_key = ?;",
            (detail_key,),
        )
        changed = cur.rowcount
    if changed:
        refresh_dict_options()
    return changed


def delete_many(detail_keys) -> int:
    """批量删除检测记录 —— 字典只重建一次。

    逐条调用 `delete()` 会在每一条之后触发一次检测库字典的全量重建，
    批量删除时会显著变慢；这里统一在最后重建一次。
    """
    keys = [str(k).strip() for k in (detail_keys or []) if str(k or "").strip()]
    if not keys:
        return 0
    marks = ", ".join("?" for _ in keys)
    with tx() as conn:
        cur = conn.execute(
            f"DELETE FROM inspect_db.inspect_records WHERE detail_key IN ({marks});",
            keys,
        )
        changed = cur.rowcount
    if changed:
        refresh_dict_options()
    return changed


# ---------------------------------------------------------------------------
# 字典（检测字段的候选值）
# ---------------------------------------------------------------------------

def _prune_field(conn, field: str) -> int:
    """删除已不存在于检测数据的候选值（墓碑清理）。"""
    _safe(field)
    cur = conn.execute(
        f"""DELETE FROM inspect_db.dict_option
            WHERE field = ?
              AND value NOT IN (
                SELECT value FROM (
                  SELECT TRIM({field}) AS value, COUNT(*) AS n
                  FROM inspect_db.inspect_records
                  WHERE {field} IS NOT NULL AND TRIM({field}) <> ''
                  GROUP BY {field} ORDER BY n DESC LIMIT {int(DICT_OPTION_LIMIT)}
                )
              );""",
        (field,),
    )
    return cur.rowcount


def refresh_dict_options() -> int:
    """重建检测库的字典候选（先清理不再存在的值，再 upsert）。"""
    now = _now()
    pruned = 0
    with tx() as conn:
        for field in INSPECT_DICT_FIELDS:
            _safe(field)
            pruned += _prune_field(conn, field)
            rows = conn.execute(
                f"SELECT {field} AS v, COUNT(*) AS n "
                f"FROM inspect_db.inspect_records "
                f"WHERE {field} IS NOT NULL AND TRIM({field}) <> '' "
                f"GROUP BY {field} ORDER BY n DESC LIMIT ?;",
                (DICT_OPTION_LIMIT,),
            ).fetchall()
            for r in rows:
                conn.execute(
                    """INSERT INTO inspect_db.dict_option (field, value, use_count, updated_at)
                       VALUES (?,?,?,?)
                       ON CONFLICT(field, value) DO UPDATE SET
                         use_count = excluded.use_count,
                         updated_at = excluded.updated_at;""",
                    (field, str(r["v"]).strip(), r["n"], now),
                )

        # 回收：字段口径变更后（如 solution 改为固定选项），它此前的候选
        # 永远不会再被读取，留着只会堆僵尸行。字典表只保留当前字典字段。
        if INSPECT_DICT_FIELDS:
            marks = ", ".join("?" for _ in INSPECT_DICT_FIELDS)
            conn.execute(
                f"DELETE FROM inspect_db.dict_option "
                f"WHERE field NOT IN ({marks});",
                INSPECT_DICT_FIELDS,
            )
    return pruned


def get_dict_options(field: str = "") -> dict:
    """读取检测库的字典候选。

    带 field 时返回 `{"field": …, "options": […]}`，与退回登记库的
    `repository.get_dict_options(field)` 完全一致 —— 否则
    `GET /api/dict/{field}` 会因字段归属不同而返回两种结构。
    不带 field 时返回 `{字段名: […]}` 的分组字典，供全量拉取时合并。
    """
    conn = get_conn()
    if field:
        _safe(field)
        rows = conn.execute(
            "SELECT value, use_count FROM inspect_db.dict_option WHERE field = ? "
            "ORDER BY use_count DESC, value;", (field,),
        ).fetchall()
        return {"field": field, "options": [dict(r) for r in rows]}

    marks = ", ".join("?" for _ in INSPECT_DICT_FIELDS)
    rows = conn.execute(
        f"SELECT field, value, use_count FROM inspect_db.dict_option "
        f"WHERE field IN ({marks}) ORDER BY field, use_count DESC;",
        INSPECT_DICT_FIELDS,
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["field"], []).append(
            {"value": r["value"], "use_count": r["use_count"]})
    return out


def distinct_values(field: str, keyword: str = "", limit: int = 300) -> list:
    """实时取检测字段的去重值（用于下拉搜索），不走字典快照。"""
    _safe(field)
    conn = get_conn()
    sql = (f"SELECT {field} AS v, COUNT(*) AS n "
           f"FROM inspect_db.inspect_records "
           f"WHERE {field} IS NOT NULL AND TRIM({field}) <> ''")
    params = []
    if keyword:
        sql += f" AND {field} LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(keyword)}%")
    sql += f" GROUP BY {field} ORDER BY n DESC LIMIT ?;"
    params.append(limit)
    return [{"value": r["v"], "use_count": r["n"]}
            for r in conn.execute(sql, params).fetchall()]
