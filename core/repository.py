"""数据访问层 —— 登记、匹配、查询、统计

所有 SQL 集中在此模块，上层（API）不直接操作数据库。
字段名一律经过白名单校验后才拼入 SQL，避免注入。

多库架构
--------
数据分落三个库（见 core/db.py）：退回登记库 / 检测登记库 / 匹配数据库。
跨库读取统一用 `_DETAIL_FROM` 提供的 LEFT JOIN 骨架，写入则各库自理。
本模块内部按库分节：

    【退回登记库】编号生成 / 型号字典 / 登记写入 / 跨库查询
    【检测登记库】见 core/repo_inspect.py
    【匹配数据库】item_master 的增删改查与导入导出
    【跨库组合】明细拼接查询、统计、关键词检索、导出、扫码匹配
"""
import json
import sqlite3
import threading
from datetime import datetime

from config import (CARRIER_PREFIX_RULES, CODE_PERIOD_CENTURY,
                    CODE_PERIOD_MONTH_SUFFIX, CODE_PERIOD_PREFIX_LEN,
                    DICT_FIELDS, DICT_OPTION_LIMIT, FIELD_LABELS,
                    HANDLE_DEFAULT_VALUES, HANDLE_DICT_FIELDS,
                    HANDLE_DONE_VALUES, INSPECT_DICT_FIELDS, LINE_NO_WIDTH,
                    MATCH_FIELDS, MATCH_FUZZY_ENABLED, ORDER_NO_SEQ_WIDTH,
                    ORDER_NO_TOTAL_LEN, PERIOD_UNKNOWN, RETURNS_DICT_FIELDS)
from core.db import (HANDLE_COLUMNS, INSPECT_COLUMNS, InvalidField, get_conn,
                     tx)
from core import repo_handle
from core import repo_inspect
from core import photos

# 各库所属字段（决定跨库查询时挂 r. / i. / h. 前缀）
INSPECT_FIELD_SET = set(INSPECT_COLUMNS)
HANDLE_FIELD_SET = set(HANDLE_COLUMNS)

# 允许出现在 WHERE / GROUP BY / ORDER BY 中的字段（白名单）
ALLOWED_FIELDS = {
    "detail_key", "order_no", "turbine_vendor", "project_site",
    "return_no", "carrier", "return_date", "line_no", "product_code",
    "product_model", "product_category", "production_year", "production_month",
    "return_qty", "material_no", "product_name", "spec", "production_stat",
    "match_path", "match_status", "registrar", "remark", "registered_at",
    "test_date", "feedback_issue", "test_result", "fault_cause", "improvement",
    "solution", "issue_category", "responsibility", "photo_evidence",
    "report_no", "analysis_report", "erp_handled", "handle_solution",
    "info_source", "completion", "source", "sync_state", "id",
}

# 新增字段时**必须**同步登记进上面这个白名单。
# 漏登记的后果比想象中严重：_qualify() 会抛 ValueError，而关键词检索
# 会对 KEYWORD_FIELDS 里每个字段调一次 —— 于是**任何**关键词搜索都直接 500，
# 报错信息还指向一个和搜索无关的地方。（handle_solution 就踩过。）

# 登记表单允许写入退回登记库的字段
WRITABLE_FIELDS = ALLOWED_FIELDS - {"id", "detail_key", "sync_state"}

# 其中落退回登记库的部分（检测字段写 inspect.db、处理字段写 handle.db，
# 两处都要扣除 —— 漏扣会让该字段被当成退回侧字段塞进 returns 表）
WRITABLE_RETURNS_FIELDS = WRITABLE_FIELDS - INSPECT_FIELD_SET - HANDLE_FIELD_SET

# 可按明细键更新的检测字段（写检测登记库）
WRITABLE_INSPECT_FIELDS = set(INSPECT_COLUMNS) | {"detail_key"}

# 全局关键词检索覆盖的字段（跨三库）
KEYWORD_FIELDS = [
    "detail_key", "order_no", "turbine_vendor", "project_site", "return_no",
    "carrier", "product_code", "product_model", "product_category",
    "material_no", "product_name", "spec", "remark", "feedback_issue",
    "fault_cause", "test_result", "issue_category", "responsibility",
    "report_no", "solution", "improvement", "registrar",
    "erp_handled", "handle_solution",
]

# ---------------------------------------------------------------------------
# 跨库查询骨架
# ---------------------------------------------------------------------------

# 明细 = 退回登记 LEFT JOIN 检测登记。
# 必须 LEFT JOIN：检测记录稀疏存储，未检测的明细在检测库中没有行，
# 用 INNER JOIN 会让这些记录从查询、看板、导出里整体消失。
_DETAIL_FROM = ("FROM returns r "
                "LEFT JOIN inspect_db.inspect_records i "
                "ON i.detail_key = r.detail_key "
                "LEFT JOIN handle_db.handle_records h "
                "ON h.detail_key = r.detail_key")

# 检测进度判定片段。注意 LEFT JOIN 未命中时检测侧为 NULL，
# 而 `NULL NOT LIKE '%完结%'` 结果是 NULL（不成立），必须用 COALESCE 兜住。
_PENDING_SQL = ("(i.detail_key IS NULL "
                " OR TRIM(COALESCE(i.completion, '')) = '' "
                " OR i.completion NOT LIKE '%完结%')")
_UNTESTED_SQL = ("(i.detail_key IS NULL "
                 " OR TRIM(COALESCE(i.test_date, '')) = '')")

# 「已检测」= 检测时间非空。与 _UNTESTED_SQL 严格互补（同一批记录不会两边都算），
# 看板的「检测汇总」用它作为基础口径 —— 只统计真的走过检测环节的明细。
# 注意不能写成「检测库有行」：录入一半（有行但没填检测时间）的记录会与
# 「待检测」重复计数。
_TESTED_SQL = "TRIM(COALESCE(i.test_date, '')) <> ''"

# 「待处理」= 已检测、且 ERP 处理不是「已处理」。
#
# 用**反选**（`<> 已处理`）而不是 `= 待处理`：处理库是稀疏存储，
# 未处理的明细可能整行都不存在（LEFT JOIN 后为 NULL），
# 用正选会把它们全漏掉 —— 表现为「待处理清单少了 60 多条」，不报错。
_HANDLE_DONE = HANDLE_DONE_VALUES.get("erp_handled", "已处理")
_HANDLE_PENDING_SQL = ("(" + _TESTED_SQL
                       + f" AND TRIM(COALESCE(h.erp_handled, '')) <> "
                         f"'{_HANDLE_DONE}')")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符。"""
    return (value.replace("\\", "\\\\")
                 .replace("%", "\\%")
                 .replace("_", "\\_"))


def _safe_field(name: str) -> str:
    if name not in ALLOWED_FIELDS:
        raise InvalidField(f"非法字段: {str(name)[:60]}")
    return name


def _qualify(name: str) -> str:
    """给字段加上所属库的别名前缀，供跨库查询使用。"""
    _safe_field(name)
    if name in HANDLE_FIELD_SET:
        return f"h.{name}"
    return f"i.{name}" if name in INSPECT_FIELD_SET else f"r.{name}"


def _value_expr(field: str) -> str:
    """字段的「归一化取值」SQL 表达式：空值按 config 的默认值算。

    `erp_handled` 在库里可能是空值或整行缺失（处理库稀疏存储），
    但语义上等价于「待处理」。**所有读取路径都必须走这个表达式** ——
    否则同一个字段在列表、导出、筛选、统计里会出现三种表现
    （空 / 待处理 / 查不到），而且都不报错。
    """
    col = _qualify(field)
    dft = HANDLE_DEFAULT_VALUES.get(field)
    if not dft:
        return col
    # dft 来自 config 的代码级常量，不是用户输入，可以直接内联
    return f"COALESCE(NULLIF(TRIM({col}), ''), '{dft}')"


# 明细查询要取的列：退回侧全部 + 检测侧 11 列 + 处理侧 2 列（不含 detail_key，避免重名）。
# 处理侧字段带空值归一 —— 见 _value_expr。
_DETAIL_COLS = ("r.*, "
                + ", ".join(f"i.{c}" for c in INSPECT_COLUMNS)
                + ", "
                + ", ".join(
                    (f"{_value_expr(c)} AS {c}"
                     if c in HANDLE_DEFAULT_VALUES else f"h.{c}")
                    for c in HANDLE_COLUMNS))


# ---------------------------------------------------------------------------
# 编号生成
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 单号竞争的串行化
#
# `next_order_no()` 是「查 MAX + 1」，**只探测不占号** —— 两个人同时登记时，
# 两个请求会拿到同一个单号，再抢同一个 detail_key，其中一个撞唯一约束失败。
# 实测 8 个并发登记挂 5 个，而且报错是数据库层的英文原文
# （`UNIQUE constraint failed: returns.detail_key`），登记员完全看不懂。
#
# 处理方式：把「取号 → 插入」整段放进同一临界区。单进程部署下这彻底消除了
# 窗口；锁只管得住本进程，所以外面再套一层重试兜住多进程 / 多实例的残存竞争。
# ---------------------------------------------------------------------------
_ORDER_SEQ_LOCK = threading.Lock()
_ORDER_RETRY = 5


def _is_detail_key_conflict(exc: BaseException) -> bool:
    """是否撞了明细唯一键。

    只认 detail_key —— 别的唯一约束（例如物料主键重复）重试也没用，
    笼统地捕获 IntegrityError 只会白转几圈再报同一个错，还会把真实错误
    掩盖成「单号被占用」。
    """
    return isinstance(exc, sqlite3.IntegrityError) and "detail_key" in str(exc)


def next_order_no(day: str = "") -> str:
    """生成下一个售后单号。

    规则：年份后两位 + 月日 + 顺序号，顺序号按日重置。
    例：2026-01-01 的第 1 单 -> 260101001，同日第 2 单 -> 260101002。
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    prefix = f"{day[2:4]}{day[5:7]}{day[8:10]}"
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(CAST(substr(order_no, 7, ?) AS INTEGER)) AS m FROM returns "
        "WHERE order_no LIKE ? AND LENGTH(order_no) = ?;",
        (ORDER_NO_SEQ_WIDTH, prefix + "%", ORDER_NO_TOTAL_LEN),
    ).fetchone()
    seq = (row["m"] or 0) + 1
    return f"{prefix}{str(seq).zfill(ORDER_NO_SEQ_WIDTH)}"


def next_line_no(order_no: str) -> int:
    """同一售后单号下的下一个行号。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(line_no) AS m FROM returns WHERE order_no = ?;", (order_no,)
    ).fetchone()
    return int(row["m"] or 0) + 1


def make_detail_key(order_no: str, line_no: int) -> str:
    return f"{order_no}-{str(line_no).zfill(LINE_NO_WIDTH)}"


# ---------------------------------------------------------------------------
# 型号字典
# ---------------------------------------------------------------------------

def lookup_model(code: str):
    """按产品编号 / 料号 / 型号 在字典中反查产品信息。"""
    if not code:
        return None
    conn = get_conn()
    c = code.strip()
    row = conn.execute(
        "SELECT * FROM model_dict WHERE product_model = ? OR product_code = ? "
        "OR material_no = ? LIMIT 1;", (c, c, c)
    ).fetchone()
    return dict(row) if row else None


def upsert_model(model_data: dict) -> None:
    """写入或更新型号字典（登记时自动积累）。"""
    model = (model_data.get("product_model") or "").strip()
    if not model:
        return
    with tx() as conn:
        conn.execute(
            """INSERT INTO model_dict
               (product_model, product_code, material_no, product_name, spec,
                production_stat, product_category, category_l1, category_l2,
                category_l3, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(product_model) DO UPDATE SET
                 product_code    = COALESCE(excluded.product_code,    product_code),
                 material_no     = COALESCE(excluded.material_no,     material_no),
                 product_name    = COALESCE(excluded.product_name,    product_name),
                 spec            = COALESCE(excluded.spec,            spec),
                 production_stat = COALESCE(excluded.production_stat, production_stat),
                 product_category= COALESCE(excluded.product_category,product_category),
                 category_l1     = COALESCE(excluded.category_l1,     category_l1),
                 category_l2     = COALESCE(excluded.category_l2,     category_l2),
                 category_l3     = COALESCE(excluded.category_l3,     category_l3),
                 updated_at      = excluded.updated_at;""",
            (model,
             model_data.get("product_code"), model_data.get("material_no"),
             model_data.get("product_name"), model_data.get("spec"),
             model_data.get("production_stat"),
             model_data.get("product_category") or model_data.get("category_l1"),
             model_data.get("category_l1"), model_data.get("category_l2"),
             model_data.get("category_l3"), _now()),
        )


def prune_model_dict(models) -> int:
    """回收型号字典中已无返件记录引用的条目。

    model_dict 完全由 returns 派生（登记时积累），因此当某型号的最后一条
    记录被删除后，该条目应当一并移除，否则扫码时会用已删除产品的信息
    错误回填。item_master（匹配数据库）是独立主数据，不受影响。
    """
    models = [m for m in {str(x).strip() for x in (models or [])} if m]
    if not models:
        return 0
    marks = ", ".join("?" for _ in models)
    with tx() as conn:
        cur = conn.execute(
            f"""DELETE FROM model_dict
                WHERE product_model IN ({marks})
                  AND NOT EXISTS (
                    SELECT 1 FROM returns
                    WHERE TRIM(returns.product_model) = model_dict.product_model
                  );""",
            models,
        )
        return cur.rowcount


def list_models(keyword: str = "", limit: int = 2000) -> list:
    conn = get_conn()
    if keyword:
        kw = f"%{_escape_like(keyword)}%"
        rows = conn.execute(
            "SELECT * FROM model_dict WHERE product_model LIKE ? ESCAPE '\\' "
            "OR product_name LIKE ? ESCAPE '\\' OR product_code LIKE ? ESCAPE '\\' "
            "ORDER BY product_model LIMIT ?;", (kw, kw, kw, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM model_dict ORDER BY product_model LIMIT ?;", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 匹配数据库（物料主档 Item Master）
#   扫码后用料号反查品名 / 型号 / 规格 / 生产统计，是回填的第一优先来源。
# ---------------------------------------------------------------------------

ITEM_FIELDS = [
    "material_no", "old_material_no", "product_name", "model_no", "spec",
    "description", "customer", "production_stat", "param1", "param2",
    "param3", "category",
]

# 实际落库的列 = 业务字段 + 系统字段
ITEM_COLUMNS = ITEM_FIELDS + ["source", "created_at", "updated_at"]

ITEM_SEARCH_FIELDS = [
    "material_no", "old_material_no", "product_name", "model_no", "spec",
    "description", "customer", "production_stat",
]

# 匹配优先级：料号 → 旧料号 → 规格 → 型号
ITEM_MATCH_FIELDS = ["material_no", "old_material_no", "spec", "model_no"]


def _safe_item_field(name: str) -> str:
    if name not in ITEM_COLUMNS:
        raise InvalidField(f"非法字段: {str(name)[:60]}")
    return name


def get_item(item_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM items_db.item_master WHERE id = ?;", (item_id,)).fetchone()
    return dict(row) if row else None


def get_item_by_no(material_no: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM items_db.item_master WHERE material_no = ?;",
                       (material_no,)).fetchone()
    return dict(row) if row else None


def _item_where(keyword: str, filters: dict):
    """编译物料主档查询的 WHERE 子句与参数。"""
    filters = filters or {}
    clauses, params = [], []

    if keyword:
        kw = f"%{_escape_like(keyword)}%"
        sub = " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in ITEM_SEARCH_FIELDS)
        clauses.append(f"({sub})")
        params.extend([kw] * len(ITEM_SEARCH_FIELDS))

    for field in ("product_name", "model_no", "production_stat", "customer",
                  "category"):
        raw = filters.get(field)
        if not raw:
            continue
        values = [v.strip() for v in str(raw).split(",") if v.strip()]
        if not values:
            continue
        _safe_item_field(field)
        marks = ", ".join("?" for _ in values)
        clauses.append(f"{field} IN ({marks})")
        params.extend(values)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def list_items(keyword: str = "", filters: dict = None, page: int = 1,
               page_size: int = 50, sort_by: str = "material_no",
               sort_dir: str = "asc") -> dict:
    where, params = _item_where(keyword, filters)
    sort_by = sort_by if sort_by in ITEM_FIELDS else "material_no"
    _safe_item_field(sort_by)
    sort_dir = "DESC" if str(sort_dir).lower() == "desc" else "ASC"

    conn = get_conn()
    total = conn.execute(f"SELECT COUNT(*) c FROM items_db.item_master{where};",
                         params).fetchone()["c"]
    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 50)), 500)
    rows = conn.execute(
        f"SELECT * FROM items_db.item_master{where} ORDER BY {sort_by} {sort_dir} "
        f"LIMIT ? OFFSET ?;", params + [page_size, (page - 1) * page_size]
    ).fetchall()
    return {
        "total": total, "page": page, "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "rows": [dict(r) for r in rows],
    }


def query_items_all(keyword: str = "", filters: dict = None,
                    limit: int = 200000) -> list:
    """导出用：取全部匹配行（不受分页上限约束）。"""
    where, params = _item_where(keyword, filters)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM items_db.item_master{where} ORDER BY material_no LIMIT ?;",
        params + [limit]).fetchall()
    return [dict(r) for r in rows]


def upsert_item(data: dict, operator: str = "") -> dict:
    """按料号新增或更新一条物料主档。"""
    material_no = str((data or {}).get("material_no") or "").strip()
    if not material_no:
        raise ValueError("料号不能为空")
    payload = {}
    for k, v in data.items():
        if k not in ITEM_FIELDS or k == "material_no":
            continue
        payload[k] = v.strip() if isinstance(v, str) else v
    payload["material_no"] = material_no
    now = _now()

    conn = get_conn()
    with tx() as c:
        exists = c.execute("SELECT id FROM items_db.item_master WHERE material_no = ?;",
                           (material_no,)).fetchone()
        if exists:
            payload["updated_at"] = now
            sets = ", ".join(f"{_safe_item_field(k)} = ?" for k in payload)
            c.execute(f"UPDATE items_db.item_master SET {sets} WHERE material_no = ?;",
                      list(payload.values()) + [material_no])
            item_id, action = exists["id"], "update"
        else:
            payload["created_at"] = now
            payload["updated_at"] = now
            payload.setdefault("source", "manual")
            cols = ", ".join(_safe_item_field(k) for k in payload)
            marks = ", ".join("?" for _ in payload)
            item_id = c.execute(
                f"INSERT INTO items_db.item_master ({cols}) VALUES ({marks});",
                list(payload.values())).lastrowid
            action = "create"
        c.execute(
            "INSERT INTO op_log (action, detail_key, payload, operator, created_at) "
            "VALUES (?,?,?,?,?);",
            (f"item_{action}", material_no,
             json.dumps(payload, ensure_ascii=False), operator, now))
    return {"id": item_id, "material_no": material_no, "action": action}


def delete_item(item_id: int, operator: str = "") -> int:
    with tx() as conn:
        row = conn.execute("SELECT material_no FROM items_db.item_master WHERE id = ?;",
                           (item_id,)).fetchone()
        if not row:
            return 0
        changed = conn.execute("DELETE FROM items_db.item_master WHERE id = ?;",
                               (item_id,)).rowcount
        conn.execute(
            "INSERT INTO op_log (action, detail_key, payload, operator, created_at) "
            "VALUES (?,?,?,?,?);",
            ("item_delete", row["material_no"], None, operator, _now()))
        return changed


def import_items(rows: list, operator: str = "", mode: str = "upsert") -> dict:
    """批量导入物料主档。

    mode = "upsert"  按料号覆盖已有、新增缺失（默认）
    mode = "replace" 先清空整表再导入
    """
    now = _now()
    inserted = updated = skipped = 0
    with tx() as c:
        if mode == "replace":
            c.execute("DELETE FROM items_db.item_master;")
        for raw in rows:
            payload = {}
            for k, v in (raw or {}).items():
                if k not in ITEM_FIELDS:
                    continue
                payload[k] = str(v).strip() if v is not None else None
            material_no = (payload.get("material_no") or "").strip()
            if not material_no:
                skipped += 1
                continue
            payload["material_no"] = material_no
            exists = c.execute("SELECT id FROM items_db.item_master WHERE material_no = ?;",
                               (material_no,)).fetchone()
            if exists:
                keys = [k for k in payload if k != "material_no"]
                if keys:
                    payload["updated_at"] = now
                    sets = ", ".join(f"{_safe_item_field(k)} = ?" for k in keys)
                    c.execute(
                        f"UPDATE items_db.item_master SET {sets}, updated_at = ? "
                        f"WHERE material_no = ?;",
                        [payload[k] for k in keys] + [now, material_no])
                updated += 1
            else:
                payload["created_at"] = now
                payload["updated_at"] = now
                payload.setdefault("source", "import")
                cols = ", ".join(_safe_item_field(k) for k in payload)
                marks = ", ".join("?" for _ in payload)
                c.execute(f"INSERT INTO items_db.item_master ({cols}) VALUES ({marks});",
                          list(payload.values()))
                inserted += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped,
            "total": inserted + updated, "mode": mode}


def search_items(kw: str, limit: int = 20) -> list:
    """按关键字模糊检索物料主档 —— 供明细行输入时的候选列表。

    与 `lookup_item` 的分工：那个是「拿一个确定的码去回填」，
    这个是「输入片段，返回一批候选供挑选」。

    检索维度与 `ITEM_MATCH_FIELDS` 对齐（料号 / 旧料号 / 规格 / 型号），
    另加品名 —— 用户常常只记得「低温型风速传感器」而不记得料号。
    排序把**料号以关键字开头**的排最前：那最可能就是想找的那条。
    """
    text = str(kw or "").strip()
    if not text:
        return []
    like = f"%{_escape_like(text)}%"
    prefix = f"{_escape_like(text)}%"
    conn = get_conn()
    rows = conn.execute(
        "SELECT material_no, old_material_no, product_name, model_no, spec, "
        "       production_stat "
        "  FROM items_db.item_master "
        " WHERE material_no     LIKE ? ESCAPE '\\' "
        "    OR old_material_no LIKE ? ESCAPE '\\' "
        "    OR spec            LIKE ? ESCAPE '\\' "
        "    OR model_no        LIKE ? ESCAPE '\\' "
        "    OR product_name    LIKE ? ESCAPE '\\' "
        " ORDER BY (material_no LIKE ? ESCAPE '\\') DESC, material_no "
        " LIMIT ?;",
        (like, like, like, like, like, prefix, max(1, int(limit))),
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        name = str(d.get("product_name") or "").strip()
        model = str(d.get("model_no") or "").strip()
        spec = str(d.get("spec") or "").strip()
        out.append({
            "value": d.get("material_no") or "",
            "material_no": d.get("material_no") or "",
            "product_name": name,
            "model_no": model,
            "spec": spec,
            "production_stat": str(d.get("production_stat") or "").strip(),
            # 候选副标题：一眼分辨同名料号（品名优先，其次型号 / 规格）
            "meta": " · ".join(x for x in (name, model or spec) if x),
        })
    return out


def lookup_item(code: str):
    """按码反查物料主档：料号 → 旧料号 → 规格 → 型号，未命中再走模糊后缀。"""
    code = (code or "").strip()
    if not code:
        return None
    conn = get_conn()
    for field in ITEM_MATCH_FIELDS:
        row = conn.execute(
            f"SELECT * FROM items_db.item_master WHERE {_safe_item_field(field)} = ? LIMIT 1;",
            (code,)).fetchone()
        if row:
            d = dict(row)
            d["_matched_field"] = field
            d["_fuzzy"] = False
            return d
    if len(code) >= 4:
        esc = _escape_like(code)
        for field in ("material_no", "old_material_no", "spec"):
            row = conn.execute(
                f"SELECT * FROM items_db.item_master WHERE {_safe_item_field(field)} "
                f"LIKE ? ESCAPE '\\' LIMIT 1;", (f"%{esc}",)).fetchone()
            if row:
                d = dict(row)
                d["_matched_field"] = field
                d["_fuzzy"] = True
                return d
    return None


def item_facets() -> dict:
    """匹配库的筛选维度候选值。"""
    conn = get_conn()
    out = {}
    for field in ("product_name", "model_no", "production_stat", "customer",
                  "category"):
        _safe_item_field(field)
        rows = conn.execute(
            f"SELECT {field} AS v, COUNT(*) AS n FROM items_db.item_master "
            f"WHERE {field} IS NOT NULL AND TRIM({field}) <> '' "
            f"GROUP BY {field} ORDER BY n DESC LIMIT 200;").fetchall()
        out[field] = [{"value": r["v"], "use_count": r["n"]} for r in rows]
    return out


def item_stats() -> dict:
    conn = get_conn()
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  COUNT(DISTINCT product_name)  AS names,
                  COUNT(DISTINCT model_no)      AS models,
                  COUNT(DISTINCT production_stat) AS stats,
                  SUM(CASE WHEN spec IS NULL OR TRIM(spec) = '' THEN 1 ELSE 0 END) AS no_spec,
                  SUM(CASE WHEN category IS NULL OR TRIM(category) = '' THEN 1 ELSE 0 END) AS no_category
           FROM items_db.item_master;""").fetchone()
    return {k: (v or 0) for k, v in dict(row).items()}


def item_suggest(code: str) -> dict | None:
    """把物料主档记录转成登记表单可直接回填的字段集合。"""
    item = lookup_item(code)
    if not item:
        return None
    suggest = {}
    if item.get("material_no"):
        suggest["material_no"] = item["material_no"]
    if item.get("product_name"):
        suggest["product_name"] = item["product_name"]
    # 返件登记里的「产品型号」存的是规格号，故两者都取规格
    spec = item.get("spec") or item.get("model_no")
    if spec:
        suggest["product_model"] = spec
        suggest["spec"] = spec
    if item.get("production_stat"):
        suggest["production_stat"] = item["production_stat"]
    # 匹配库把「类别」信息存在 production_stat 列里，名为 category 的那列是空的
    # （实测 0/1735），所以以 production_stat 为准、category 仅作兜底。
    category = item.get("production_stat") or item.get("category")
    if category:
        suggest["product_category"] = category
    suggest["_source"] = "item_master"
    suggest["_desc"] = f"来自匹配数据库：{item.get('material_no')} {item.get('product_name') or ''}"
    return suggest


# 明细行「输料号自动回填」的字段集合（只回填产品固有属性）
FILL_FIELDS = ("material_no", "product_model", "spec", "product_name",
               "product_category", "production_stat")

# 刻意不回填的字段：生产年份 / 生产月份
# 匹配库没有这两个字段，而历史同规格记录的年份并不一致（实测 51258.68.420
# 横跨 2018-2023 共 6 种取值）—— 自动填入等于凭空造一个看似可信的错值，
# 不如留空让人照铭牌填。


def _fill_from_history(code: str) -> dict | None:
    """匹配库未命中时的兜底：按同码从返件历史取一份最完整的记录。

    历史里可能存着匹配库缺失的信息（尤其产品类别）。
    """
    conn = get_conn()
    row = None
    for field in ("material_no", "product_model", "product_code", "spec"):
        _safe_field(field)
        row = conn.execute(
            f"SELECT {_DETAIL_COLS} {_DETAIL_FROM} WHERE r.{field} = ? "
            f"ORDER BY r.id DESC LIMIT 1;", (code,)
        ).fetchone()
        if row:
            break
    if not row:
        return None
    d = dict(row)
    suggest = {k: d[k] for k in FILL_FIELDS if str(d.get(k) or "").strip()}
    if not suggest:
        return None
    suggest["_source"] = "history"
    suggest["_desc"] = f"来自历史记录 {d.get('detail_key')}"
    return suggest


def suggest_by_material(code: str) -> dict:
    """退回登记明细行「输入料号 → 自动回填」的建议值。

    优先级：匹配数据库 → 返件历史（同码）→ 型号字典。
    返回结构给前端，含命中来源与提示文案。
    """
    code = (code or "").strip()
    out = {"code": code, "found": False, "matched_field": None, "fuzzy": False,
           "source": None, "hint": "", "suggest": {}}
    if not code:
        return out

    item = lookup_item(code)
    suggest = item_suggest(code) if item else None
    source = "item_master"
    hint = (suggest or {}).get("_desc", "")

    if not suggest:
        # 匹配库无此码 → 历史 → 字典
        suggest = _fill_from_history(code)
        source = "history"
        hint = (suggest or {}).get("_desc", "")

    if not suggest:
        model = lookup_model(code)
        if model:
            suggest = {k: model[k] for k in FILL_FIELDS
                       if str(model.get(k) or "").strip()}
            if suggest:
                source = "model_dict"
                hint = f"来自型号字典 {model.get('product_model')}"

    if not suggest:
        return out

    if item:
        out["matched_field"] = item.get("_matched_field")
        out["fuzzy"] = bool(item.get("_fuzzy"))

    out["found"] = True
    out["source"] = source
    out["hint"] = hint or f"来自 {source}"
    out["suggest"] = {k: v for k, v in suggest.items()
                      if not k.startswith("_") and k in WRITABLE_RETURNS_FIELDS}
    return out


# ---------------------------------------------------------------------------
# 扫码实时匹配
# ---------------------------------------------------------------------------

def match_code(code: str) -> dict:
    """多字段自动识别。

    输入任意条码，依次尝试「退回单号 / 产品编号 / 料号 / 售后单号 / 产品型号」，
    返回命中记录与可用于回填的建议值。
    """
    code = (code or "").strip()
    result = {
        "code": code,
        "exact": False,
        "fuzzy": False,
        "matches": [],
        "history": [],
        "suggest": None,
    }
    if not code:
        return result

    conn = get_conn()
    for field, label in MATCH_FIELDS:
        _safe_field(field)
        rows = conn.execute(
            f"SELECT {_DETAIL_COLS} {_DETAIL_FROM} WHERE r.{field} = ? "
            f"ORDER BY r.id DESC LIMIT 50;",
            (code,),
        ).fetchall()
        if rows:
            records = [dict(r) for r in rows]
            result["matches"].append({
                "field": field,
                "field_label": label,
                "count": len(records),
                "records": records,
            })

    if result["matches"]:
        result["exact"] = True
        first = result["matches"][0]
        result["history"] = first["records"]
        result["suggest"] = _build_suggest(code, first["records"][0])
        return result

    # 精确未命中 -> 模糊匹配（按后缀再按包含）
    if MATCH_FUZZY_ENABLED and len(code) >= 4:
        esc = _escape_like(code)
        for mode, pattern in (("suffix", f"%{esc}"), ("contains", f"%{esc}%")):
            for field, label in MATCH_FIELDS:
                _safe_field(field)
                rows = conn.execute(
                    f"SELECT {_DETAIL_COLS} {_DETAIL_FROM} "
                    f"WHERE r.{field} LIKE ? ESCAPE '\\' "
                    f"ORDER BY r.id DESC LIMIT 20;", (pattern,)
                ).fetchall()
                if rows:
                    records = [dict(r) for r in rows]
                    result["matches"].append({
                        "field": field,
                        "field_label": label,
                        "count": len(records),
                        "fuzzy_mode": mode,
                        "records": records,
                    })
            if result["matches"]:
                break
        if result["matches"]:
            result["fuzzy"] = True
            result["history"] = result["matches"][0]["records"]
            result["suggest"] = _build_suggest(code, result["matches"][0]["records"][0])
            return result

    # 历史无记录 -> 仅从型号字典反查
    result["suggest"] = _build_suggest(code, None)
    return result


def _build_suggest(code: str, record) -> dict | None:
    """构造回填建议。

    产品信息以「匹配数据库（物料主档）」为准，业务上下文取自历史记录；
    两者都无命中时退回自动积累的型号字典。
    """
    suggest = {}
    source, desc = None, None

    if record:
        for key in ("order_no", "turbine_vendor", "project_site", "carrier",
                    "product_code", "product_model", "product_category",
                    "production_year", "production_month", "material_no",
                    "product_name", "spec", "production_stat", "return_qty",
                    "info_source"):
            if record.get(key) not in (None, ""):
                suggest[key] = record[key]
        source = "history"
        desc = f"来自历史记录 {record.get('detail_key') or code}"

    item = item_suggest(code)
    if item:
        for k, v in item.items():
            if k.startswith("_"):
                continue
            suggest[k] = v          # 主数据覆盖历史值
        source = "item_master"
        desc = item.get("_desc")

    if suggest:
        suggest["_source"] = source
        suggest["_desc"] = desc or f"来自 {code}"
        return suggest

    model = lookup_model(code)
    if model:
        for key in ("product_code", "product_model", "product_category",
                    "material_no", "product_name", "spec", "production_stat"):
            if model.get(key):
                suggest[key] = model[key]
        suggest["_source"] = "model_dict"
        suggest["_desc"] = f"来自型号字典 {model.get('product_model')}"
        return suggest
    return None


# ---------------------------------------------------------------------------
# 登记 / 更新
# ---------------------------------------------------------------------------

def create_return(data: dict, operator: str = "") -> dict:
    """新增一条返件明细（对外入口）。

    取号与插入必须在同一临界区：`next_order_no()` 只探测不占号，
    并发时两个请求会拿到同一个单号并抢同一个 detail_key。
    重试是跨进程的兜底 —— 锁只管得住本进程。见 _ORDER_SEQ_LOCK 的注释。
    """
    for _attempt in range(_ORDER_RETRY):
        with _ORDER_SEQ_LOCK:
            try:
                return _create_return_inner(data, operator)
            except sqlite3.IntegrityError as exc:
                if (not _is_detail_key_conflict(exc)
                        or _attempt >= _ORDER_RETRY - 1):
                    # 撞了别的唯一约束，或重试次数用尽 —— 转成登记员看得懂的话
                    raise ValueError(
                        f"单号连续 {_ORDER_RETRY} 次被占用，请重新提交") from exc
    raise ValueError("登记失败：单号分配异常")      # pragma: no cover


def _create_return_inner(data: dict, operator: str = "") -> dict:
    """新增一条返件明细，自动生成售后单号 / 行号 / 明细唯一键。

    检测字段若随请求传入，写入检测登记库（稀疏，通常由检测登记模块另行录入）。
    """
    data = data or {}
    payload = {k: v for k, v in data.items() if k in WRITABLE_RETURNS_FIELDS}
    inspect_payload = {k: v for k, v in data.items()
                       if k in INSPECT_FIELD_SET and str(v or "").strip()}
    now = _now()

    # 未指定快递公司时，按退回单号前缀自动识别
    if not str(payload.get("carrier") or "").strip():
        guessed = guess_carrier(payload.get("return_no"))
        if guessed:
            payload["carrier"] = guessed

    # 未指定生产年月时，按产品编号前 4 位（YYMM）解析
    fill_period(payload)

    order_no = (payload.get("order_no") or "").strip()
    is_new_order = not order_no
    if is_new_order:
        order_no = next_order_no()
    line_no = next_line_no(order_no)
    detail_key = make_detail_key(order_no, line_no)

    payload["order_no"] = order_no
    payload["line_no"] = line_no
    payload["detail_key"] = detail_key
    payload.setdefault("registered_at", now)
    if operator:
        payload["registrar"] = operator
    payload["source"] = payload.get("source") or "local"
    payload["sync_state"] = "pending"
    payload["created_at"] = now
    payload["updated_at"] = now

    cols = ", ".join(payload.keys())
    marks = ", ".join("?" for _ in payload)
    with tx() as conn:
        cur = conn.execute(
            f"INSERT INTO returns ({cols}) VALUES ({marks});", list(payload.values())
        )
        new_id = cur.lastrowid
        conn.execute(
            "INSERT INTO op_log (action, detail_key, payload, operator, created_at) "
            "VALUES (?,?,?,?,?);",
            ("create", detail_key, json.dumps(payload, ensure_ascii=False),
             operator or payload.get("registrar") or "", now),
        )

    if inspect_payload:
        repo_inspect.upsert(detail_key, inspect_payload, operator)
    upsert_model(payload)
    refresh_dict_options()
    return {"id": new_id, "detail_key": detail_key, "order_no": order_no,
            "line_no": line_no, "is_new_order": is_new_order,
            "registered_at": payload["registered_at"]}


class DuplicateReturnError(Exception):
    """同一快递单号下已登记过相同产品。"""

    def __init__(self, duplicates: list):
        self.duplicates = duplicates or []
        super().__init__("存在重复登记")


# 判断「同一件产品」时比对的字段，按优先级取第一个非空值
DUPLICATE_KEY_FIELDS = ("product_code", "material_no", "product_model", "product_name")


def item_identity(item: dict) -> str:
    """提取一行的产品标识，用于重复判定。"""
    for f in DUPLICATE_KEY_FIELDS:
        v = str((item or {}).get(f) or "").strip()
        if v:
            return f"{f}:{v}"
    return ""


def guess_carrier(return_no: str) -> str:
    """按退回单号前缀推断快递公司；无法识别时返回空串。

    目前只有顺丰（SF）、京东（JD）、圆通（YT）的单号带字母前缀，
    其余快递公司的单号为纯数字，无法从单号区分，只能人工选择。
    """
    code = str(return_no or "").strip().upper()
    if not code:
        return ""
    for prefix, carrier in CARRIER_PREFIX_RULES:
        if code.startswith(prefix.upper()):
            return carrier
    return ""


def parse_period_from_code(code: str) -> dict:
    """按产品编号前 4 位解析生产年月（YYMM）。

    产品编号自带生产年月，如 `20100341` → 2020 年 10 月、
    `19121192` → 2019 年 12 月。前两位为年份（+2000），后两位为月份。

    无法解析时返回 PERIOD_UNKNOWN 占位（而不是留空）—— 让「还没查」
    与「查了但编号不规范」在数据上可区分，便于后续筛选出来人工修正。

    返回空 dict 仅当产品编号本身为空（无任何依据，不写占位）。
    """
    text = str(code or "").strip()
    if not text:
        return {}

    unknown = {"production_year": PERIOD_UNKNOWN,
               "production_month": PERIOD_UNKNOWN}

    n = int(CODE_PERIOD_PREFIX_LEN)
    if len(text) < n:
        return unknown
    head = text[:n]
    # 逐字符限定 ASCII 数字：str.isdigit() 对全角数字也返回 True
    if not all(c in "0123456789" for c in head):
        return unknown

    year = int(CODE_PERIOD_CENTURY) + int(head[:2])
    month = int(head[2:])
    if not (1 <= month <= 12):
        return unknown
    # 上限取「明年」以容忍跨年生产；下限防止把 99xx 读成 2099
    this_year = datetime.now().year
    if not (CODE_PERIOD_CENTURY <= year <= this_year + 1):
        return unknown

    return {"production_year": str(year),
            "production_month": f"{month}{CODE_PERIOD_MONTH_SUFFIX}"}


def fill_period(payload: dict) -> dict:
    """给 payload 补上生产年月 —— 只补空缺，已有值（含人工填的）不动。"""
    period = parse_period_from_code(payload.get("product_code"))
    for key, value in period.items():
        if not str(payload.get(key) or "").strip():
            payload[key] = value
    return payload


def resolve_order_no(return_no: str = "") -> tuple:
    """决定本次登记归属的售后单号。

    补登规则：若快递单号（退回单号）已在库中存在，自动归入该单；
    否则按「年份后两位+月日+顺序号」生成新单。

    返回 (order_no, is_new_order)
    """
    code = (return_no or "").strip()
    if code:
        conn = get_conn()
        row = conn.execute(
            "SELECT order_no FROM returns WHERE return_no = ? "
            "AND order_no IS NOT NULL AND TRIM(order_no) <> '' "
            "ORDER BY id LIMIT 1;", (code,)).fetchone()
        if row and row["order_no"]:
            return str(row["order_no"]), False
    return next_order_no(), True


def find_duplicates(return_no: str, items: list) -> list:
    """检查待登记的明细是否与库中已有记录重复。

    判定依据：同一快递单号下，产品标识（产品编号/料号/型号/品名）已存在。
    """
    code = (return_no or "").strip()
    if not code or not items:
        return []

    conn = get_conn()
    rows = conn.execute(
        "SELECT detail_key, product_code, material_no, product_model, product_name "
        "FROM returns WHERE return_no = ?;", (code,)).fetchall()

    existing = {}
    for r in rows:
        ident = item_identity(dict(r))
        if ident:
            existing.setdefault(ident, []).append(r["detail_key"])

    dups = []
    for idx, it in enumerate(items):
        ident = item_identity(it)
        if not ident or ident not in existing:
            continue
        field = ident.split(":", 1)[0]
        dups.append({
            "line": idx + 1,
            "field": field,
            "field_label": FIELD_LABELS.get(field, field),
            "value": ident.split(":", 1)[1],
            "detail_key": existing[ident][0],
            "reason": "该快递单下已登记过同一产品",
        })
    return dups


def create_returns_batch(header: dict, items: list, operator: str = "",
                         allow_duplicate: bool = False) -> dict:
    """批量登记（对外入口）。

    整个流程放进单号临界区：归单 / 取行号 / 插入之间有竞争窗口 ——
    同一快递单号的两个并发请求会解析出同一个 order_no、拿到同一段行号，
    然后抢同一个 detail_key。与 create_return 同一个问题，见其注释。
    """
    with _ORDER_SEQ_LOCK:
        return _create_returns_batch_inner(header, items, operator,
                                           allow_duplicate)


def _create_returns_batch_inner(header: dict, items: list, operator: str = "",
                                allow_duplicate: bool = False) -> dict:
    """批量登记：一个快递单号下逐行生成明细，一行对应一只产品。

    header —— 整单共享字段（退回单号、快递公司、退回时间、快递归属、
              风机厂家、项目风场、售后单号、备注等）
    items  —— 产品明细行列表；每行可携带自己的检测信息与根因归类字段

    行号在所选售后单号下连续递增，明细唯一键为「售后单号-行号」。
    """
    items = [it for it in (items or []) if isinstance(it, dict)]
    if not items:
        raise ValueError("至少需要一行产品明细")

    header = header or {}
    raw_order_no = str(header.get("order_no") or "").strip()
    if raw_order_no:
        # 显式传入单号（补录历史数据时使用）
        order_no, is_new_order = raw_order_no, False
    else:
        # 按快递单号自动归单：已在库则续接，否则新建
        order_no, is_new_order = resolve_order_no(header.get("return_no"))

    # 重复登记拦截：同一快递单号下同一产品已存在时拒绝
    if not allow_duplicate:
        dups = find_duplicates(header.get("return_no"), items)
        if dups:
            raise DuplicateReturnError(dups)

    # 整单共享字段只取退回登记库的部分；检测字段（若有）按行分流到检测库
    shared = {k: v for k, v in (header or {}).items()
              if k in WRITABLE_RETURNS_FIELDS and k != "order_no"}
    shared["order_no"] = order_no
    # 未指定快递公司时，按退回单号前缀自动识别（前端已做，此处兜底，
    # 保证导入脚本等非页面调用方也得到一致结果）
    if not str(shared.get("carrier") or "").strip():
        guessed = guess_carrier(shared.get("return_no"))
        if guessed:
            shared["carrier"] = guessed

    start_line = next_line_no(order_no)
    now = _now()
    created = []
    model_payloads = []

    with tx() as conn:
        for idx, item in enumerate(items):
            payload = dict(shared)
            # 行内非空值覆盖整单共享值，实现「行级独立关联检测/根因」
            inspect_payload = {}
            for k, v in item.items():
                if v in (None, "") or k == "order_no":
                    continue
                if k in INSPECT_FIELD_SET:
                    inspect_payload[k] = v      # 检测字段分流到检测登记库
                elif k in WRITABLE_RETURNS_FIELDS:
                    payload[k] = v

            # 行级兜底：退回单号可能只随明细行传入（如历史数据导入），
            # 此时整单层拿不到单号，需要在合并之后再识别一次快递公司
            if not str(payload.get("carrier") or "").strip():
                guessed = guess_carrier(payload.get("return_no"))
                if guessed:
                    payload["carrier"] = guessed

            # 生产年月：未提供时按产品编号前 4 位（YYMM）解析
            fill_period(payload)

            line_no = start_line + idx
            detail_key = make_detail_key(order_no, line_no)
            payload["line_no"] = line_no
            payload["detail_key"] = detail_key
            if not payload.get("registered_at"):
                payload["registered_at"] = now
            if operator and not payload.get("registrar"):
                payload["registrar"] = operator
            payload["source"] = "local"
            payload["sync_state"] = "pending"
            payload["created_at"] = now
            payload["updated_at"] = now

            cols = ", ".join(payload.keys())
            marks = ", ".join("?" for _ in payload)
            cur = conn.execute(
                f"INSERT INTO returns ({cols}) VALUES ({marks});",
                list(payload.values()),
            )
            conn.execute(
                "INSERT INTO op_log (action, detail_key, payload, operator, "
                "created_at) VALUES (?,?,?,?,?);",
                ("create", detail_key, json.dumps(payload, ensure_ascii=False),
                 operator or payload.get("registrar") or "", now),
            )
            created.append({"id": cur.lastrowid, "detail_key": detail_key,
                            "line_no": line_no,
                            "inspect": inspect_payload})
            model_payloads.append(payload)

    # 型号字典写在事务外：upsert_model 自带事务，嵌套会导致提前提交
    for mp in model_payloads:
        upsert_model(mp)
    # 检测库写入同样放在事务外（各自独立事务）
    for row in created:
        if row["inspect"]:
            repo_inspect.upsert(row["detail_key"], row["inspect"], operator)
    refresh_dict_options()
    return {
        "order_no": order_no,
        "is_new_order": is_new_order,
        "count": len(created),
        "detail_keys": [c["detail_key"] for c in created],
        "rows": [{k: v for k, v in c.items() if k != "inspect"} for c in created],
        "registered_at": now,
    }


def update_return(detail_key: str, data: dict, operator: str = "") -> int:
    """按明细唯一键更新。

    字段按归属分流：产品/整单类写 returns.db，检测/归因类写 inspect.db，
    处理跟进类写 handle.db。一次调用可能同时触及三个库 —— SQLite 的 ATTACH
    不提供跨库原子事务，这里按「退回 → 检测 → 处理」的顺序执行，各自成事务。
    检测侧与处理侧的写入都是 UPSERT，重复保存不会产生多余行。
    """
    data = data or {}

    # 退回登记库字段（order_no 由系统判定，不接受改写）
    returns_payload = {k: v for k, v in data.items()
                       if k in WRITABLE_RETURNS_FIELDS and k != "order_no"}
    # 检测登记库字段
    inspect_payload = {k: v for k, v in data.items()
                       if k in INSPECT_FIELD_SET}
    # 处理登记库字段
    handle_payload = {k: v for k, v in data.items()
                      if k in HANDLE_FIELD_SET}

    if not returns_payload and not inspect_payload and not handle_payload:
        return 0

    now = _now()
    changed = 0

    if returns_payload:
        sets = ", ".join(f"{_safe_field(k)} = ?" for k in returns_payload)
        old_models = []
        with tx() as conn:
            if "product_model" in returns_payload:
                row = conn.execute(
                    "SELECT product_model FROM returns WHERE detail_key = ?;",
                    (detail_key,),
                ).fetchone()
                if row and row["product_model"]:
                    old_models.append(row["product_model"])
            cur = conn.execute(
                f"UPDATE returns SET {sets}, updated_at = ?, sync_state = 'pending' "
                f"WHERE detail_key = ?;",
                list(returns_payload.values()) + [now, detail_key],
            )
            conn.execute(
                "INSERT INTO op_log (action, detail_key, payload, operator, created_at) "
                "VALUES (?,?,?,?,?);",
                ("update", detail_key,
                 json.dumps(returns_payload, ensure_ascii=False), operator, now),
            )
            changed = cur.rowcount
        if changed:
            refresh_dict_options()
            # 型号被改掉时，旧型号若已无记录引用则一并回收
            prune_model_dict(old_models)

    if inspect_payload:
        changed += repo_inspect.upsert(detail_key, inspect_payload, operator)
    if handle_payload:
        changed += repo_handle.upsert(detail_key, handle_payload, operator)

    return changed


def get_return(detail_key: str):
    """取一条完整明细：退回登记 LEFT JOIN 检测登记。"""
    conn = get_conn()
    row = conn.execute(
        f"SELECT {_DETAIL_COLS} {_DETAIL_FROM} WHERE r.detail_key = ?;",
        (detail_key,),
    ).fetchone()
    return dict(row) if row else None


def _warn_leftover_photos(keys) -> None:
    """清理这些明细的照片文件；删不掉的（被占用 / 受环境策略拦截）提示出来。

    照片是记录派生的附属资产，删记录时必须一并清理，否则磁盘上会留下
    永远访问不到的死图。清理失败不让主流程失败 —— 但**必须可见**，
    静默残留比直接报错更难发现。
    """
    leftover = []
    for k in keys:
        _removed, failed = photos.delete_all(k)
        if failed:
            leftover.append((k, failed))
    if leftover:
        total = sum(len(f) for _k, f in leftover)
        sample = "、".join(f"{k} {len(f)} 个" for k, f in leftover[:3])
        print(f"[warn] {total} 个照片文件未能删除（可能被占用）：{sample}",
              flush=True)


def delete_return(detail_key: str, operator: str = "") -> int:
    """删除明细：其余三库的记录一并清理。"""
    removed_model = None
    with tx() as conn:
        row = conn.execute(
            "SELECT product_model FROM returns WHERE detail_key = ?;",
            (detail_key,),
        ).fetchone()
        if row:
            removed_model = row["product_model"]
        cur = conn.execute("DELETE FROM returns WHERE detail_key = ?;", (detail_key,))
        conn.execute(
            "INSERT INTO op_log (action, detail_key, payload, operator, created_at) "
            "VALUES (?,?,?,?,?);",
            ("delete", detail_key, None, operator, _now()),
        )
        changed = cur.rowcount
    if changed:
        # 检测 / 处理登记库的对应行（都是稀疏存储，可能不存在）
        repo_inspect.delete(detail_key)
        repo_handle.delete(detail_key)
        # 三个派生资产都要回收：字典候选 / 型号映射 / 照片文件。
        # 照片是磁盘文件，漏清就会留下永远访问不到的死图。
        refresh_dict_options()
        prune_model_dict([removed_model])
        _warn_leftover_photos([detail_key])
    return changed


def delete_returns(detail_keys, operator: str = "") -> dict:
    """批量删除多条明细（退回库 + 检测库一并清理）。

    与逐条调用 `delete_return` 的关键区别：**所有删除在同一次事务内完成，
    字典与型号字典只在最后重建一次**。逐条调用会让每条记录都触发一次
    全量字典重建（对每个字典字段做 GROUP BY 全表扫描），删 100 条就是
    100 次重建 —— 批量场景下必须避开。
    """
    keys, seen = [], set()
    for k in (detail_keys or []):
        k = str(k or "").strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    if not keys:
        return {"requested": 0, "deleted": 0}

    now = _now()
    marks = ", ".join("?" for _ in keys)
    with tx() as conn:
        rows = conn.execute(
            f"SELECT product_model FROM returns WHERE detail_key IN ({marks});",
            keys,
        ).fetchall()
        removed_models = [r["product_model"] for r in rows if r["product_model"]]

        cur = conn.execute(
            f"DELETE FROM returns WHERE detail_key IN ({marks});", keys)
        deleted = cur.rowcount

        for k in keys:
            conn.execute(
                "INSERT INTO op_log (action, detail_key, payload, operator, created_at) "
                "VALUES (?,?,?,?,?);",
                ("delete", k, None, operator, now),
            )

    # 检测 / 处理登记库（都是稀疏存储，未处理过的明细本来就没有行）
    repo_inspect.delete_many(keys)
    repo_handle.delete_many(keys)

    if deleted:
        # 派生资产回收：字典候选 + 已无引用的型号 + 照片文件
        prune_model_dict(removed_models)
        repo_inspect.refresh_dict_options()
        repo_handle.refresh_dict_options()
        _warn_leftover_photos(keys)

    return {"requested": len(keys), "deleted": deleted,
            "missing": len(keys) - deleted}


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def _build_where(filters: dict, scope: str = ""):
    """把筛选条件编译为 WHERE 子句与参数列表。

    scope="inspect" 时追加「已检测」基础条件 —— 看板的检测汇总只统计走过
    检测环节的明细（检测数据稀疏存储，未检测的明细在检测库没有行）。
    """
    clauses, params = [], []

    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        kw = f"%{_escape_like(keyword)}%"
        sub = " OR ".join(f"{_qualify(f)} LIKE ? ESCAPE '\\'" for f in KEYWORD_FIELDS)
        clauses.append(f"({sub})")
        params.extend([kw] * len(KEYWORD_FIELDS))

    # 精确匹配（支持多选，逗号分隔）
    # 注意：这个清单必须覆盖所有对外暴露的筛选项。漏登记不会报错 ——
    # 条件被**静默丢弃**，界面上选了筛选却返回全量，比报错更难发现。
    for field in ("order_no", "product_category", "turbine_vendor", "carrier",
                  "product_model", "issue_category",
                  "responsibility", "solution", "completion", "info_source",
                  "product_name", "spec", "material_no",
                  "feedback_issue", "erp_handled", "handle_solution",
                  "analysis_report",
                  "registrar", "project_site", "production_year",
                  "production_month",
                  # --- 检测侧字段（看板检测汇总与明细查询都依赖）---
                  "test_result", "fault_cause", "improvement", "report_no"):
        raw = filters.get(field)
        if not raw:
            continue
        values = [v.strip() for v in str(raw).split(",") if v.strip()]
        if not values:
            continue
        marks = ", ".join("?" for _ in values)
        # 用归一化表达式而不是裸列名：这样「筛待处理」能把库里为空的记录
        # 一并带出来（否则用户筛了待处理，却看不到从没处理过的那些）。
        clauses.append(f"{_value_expr(field)} IN ({marks})")
        params.extend(values)

    # 日期范围
    for field in ("return_date", "registered_at", "test_date"):
        start = filters.get(f"{field}_from")
        end = filters.get(f"{field}_to")
        if start:
            clauses.append(f"{_qualify(field)} >= ?")
            params.append(start)
        if end:
            clauses.append(f"{_qualify(field)} <= ?")
            params.append(end + " 23:59:59" if len(str(end)) == 10 else end)

    # 检测进度过滤（检测登记模块用），判定片段见 _PENDING_SQL / _UNTESTED_SQL
    if filters.get("inspect_pending"):
        clauses.append(_PENDING_SQL)
    if filters.get("untested_only"):
        clauses.append(_UNTESTED_SQL)
    if filters.get("handle_pending"):
        clauses.append(_HANDLE_PENDING_SQL)

    # 同步状态
    if filters.get("sync_state"):
        clauses.append("r.sync_state = ?")
        params.append(filters["sync_state"])

    # 看板口径：检测汇总只看已检测的明细
    if scope == "inspect":
        clauses.append(_TESTED_SQL)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def query_returns(filters: dict, page: int = 1, page_size: int = 50,
                  sort_by: str = "id", sort_dir: str = "desc") -> dict:
    """明细列表：退回登记 LEFT JOIN 检测登记。"""
    sort_by = _qualify(sort_by) if sort_by in ALLOWED_FIELDS else "r.id"
    sort_dir = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    where, params = _build_where(filters or {})

    conn = get_conn()
    total = conn.execute(
        f"SELECT COUNT(*) c {_DETAIL_FROM}{where};", params
    ).fetchone()["c"]

    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 50)), 500)
    offset = (page - 1) * page_size

    rows = conn.execute(
        f"SELECT {_DETAIL_COLS} {_DETAIL_FROM}{where} "
        f"ORDER BY {sort_by} {sort_dir} LIMIT ? OFFSET ?;",
        params + [page_size, offset],
    ).fetchall()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "rows": [dict(r) for r in rows],
    }


def query_inspect_orders(filters: dict = None, page: int = 1, page_size: int = 50,
                         sort_dir: str = "desc") -> dict:
    """检测登记用：**按售后单号聚合**的待检清单（一行 = 一个售后单）。

    与 `query_returns` 的区别：后者是明细级（一行 = 一只产品），
    本函数把同一售后单下的多条明细合并成一行，并给出进度与整单状态。

    整单状态取「最落后的那一行」，即只要还有一行没完结，整单就未完结：
        未检测 = 全单都没有检测时间
        已完结 = 全部明细都已完结
        其余   = 检测中（含「部分已检测」与「已检测但未完结」）

    filters 支持关键词（行级 LIKE，聚合前过滤）与 inspect_pending（整单级）。
    """
    filters = dict(filters or {})
    # 待检是「整单级」判定，不能沿用 _build_where 里的行级条件
    pending = bool(filters.pop("inspect_pending", 0)
                   or filters.pop("untested_only", 0))
    where, params = _build_where(filters)

    tested_expr = ("SUM(CASE WHEN TRIM(COALESCE(i.test_date, '')) <> '' "
                   "THEN 1 ELSE 0 END)")
    done_expr = "SUM(CASE WHEN i.completion LIKE '%完结%' THEN 1 ELSE 0 END)"

    base = (
        f"SELECT r.order_no AS order_no, "
        f"  MIN(r.return_no)       AS return_no, "
        f"  MIN(r.carrier)         AS carrier, "
        f"  MIN(r.turbine_vendor)  AS turbine_vendor, "
        f"  MIN(r.project_site)    AS project_site, "
        f"  MIN(r.return_date)     AS return_date, "
        f"  MIN(r.registered_at)   AS registered_at, "
        f"  COUNT(*)               AS lines, "
        f"  {tested_expr}          AS tested, "
        f"  {done_expr}            AS done "
        f"{_DETAIL_FROM}{where} "
        f"GROUP BY r.order_no "
    )
    having = f"HAVING {done_expr} < COUNT(*) " if pending else ""
    order = f"ORDER BY MIN(r.registered_at) {'ASC' if str(sort_dir).lower() == 'asc' else 'DESC'} "

    conn = get_conn()
    total = conn.execute(
        f"SELECT COUNT(*) c FROM (SELECT r.order_no {_DETAIL_FROM}{where} "
        f"GROUP BY r.order_no {having});", params
    ).fetchone()["c"]

    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 50)), 500)
    offset = (page - 1) * page_size

    rows = conn.execute(
        base + having + order + "LIMIT ? OFFSET ?;", params + [page_size, offset]
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        lines, tested, done = d["lines"] or 0, d["tested"] or 0, d["done"] or 0
        d["untested"] = max(0, lines - tested)
        # 状态取「最落后的那一行」。「未检测」要求全单既没检测过、也没完结过 ——
        # 只要有一行动过（录了检测时间或标了完结），整单就算「检测中」。
        if lines and done >= lines:
            d["status"], d["status_key"] = "已完结", "done"
        elif tested == 0 and done == 0:
            d["status"], d["status_key"] = "未检测", "untested"
        else:
            d["status"], d["status_key"] = "检测中", "testing"
        out.append(d)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "rows": out,
    }


def query_handle_orders(filters: dict = None, page: int = 1, page_size: int = 50,
                        sort_dir: str = "desc") -> dict:
    """处理登记用：**按售后单号聚合**的待处理清单（一行 = 一个售后单）。

    与 `query_inspect_orders` 结构一致，但进度看的是 ERP 处理：
        待处理 = 全单都没有处理值
        已处理 = 全部明细都已处理
        部分处理 = 其余

    filters 支持关键词（行级 LIKE，聚合前过滤）与 handle_pending（整单级：
    只返回「还有明细没处理」的单）。
    """
    filters = dict(filters or {})
    # 待处理是「整单级」判定，不能沿用 _build_where 里的行级条件
    pending = bool(filters.pop("handle_pending", 0))
    where, params = _build_where(filters)

    # 「已处理」必须精确比较 —— erp_handled 是两值锁定项，且空值会被统一
    # 归一成「待处理」。写成 `<> ''`（有值即已处理）的话，归一后的「待处理」
    # 也算已处理 → 整单被判为已处理 → 待处理清单直接空掉。
    # 常量来自 config（代码级、非用户输入），因此内联进 SQL。
    handled_expr = (f"SUM(CASE WHEN TRIM(COALESCE(h.erp_handled, '')) = "
                    f"'{_HANDLE_DONE}' THEN 1 ELSE 0 END)")
    tested_expr = ("SUM(CASE WHEN TRIM(COALESCE(i.test_date, '')) <> '' "
                   "THEN 1 ELSE 0 END)")
    # 整单级「还有未处理的明细」—— 与行级口径 _HANDLE_PENDING_SQL 保持一致：
    #   ① 未检测的行本身也算未处理（"已检测了但没处理"的判断不能漏掉它们）；
    #   ② 但整单至少要有**一行检测过**，否则纯属还没送检的退回单会混进
    #      「待处理清单」，与模块职责（检测之后的跟进）不符。
    # 两个条件不重叠：全未检测且已处理数=0 只命中 ①，不命中 ②。
    unhandled_expr = ("SUM(CASE WHEN TRIM(COALESCE(i.test_date, '')) = '' "
                      f"OR TRIM(COALESCE(h.erp_handled, '')) <> "
                      f"'{_HANDLE_DONE}' THEN 1 ELSE 0 END)")
    at_least_one_tested = f"{tested_expr} > 0"

    base = (
        f"SELECT r.order_no AS order_no, "
        f"  MIN(r.return_no)       AS return_no, "
        f"  MIN(r.carrier)         AS carrier, "
        f"  MIN(r.turbine_vendor)  AS turbine_vendor, "
        f"  MIN(r.project_site)    AS project_site, "
        f"  MIN(r.return_date)     AS return_date, "
        f"  MIN(r.registered_at)   AS registered_at, "
        f"  COUNT(*)               AS lines, "
        f"  {tested_expr}          AS tested, "
        f"  {handled_expr}         AS handled "
        f"{_DETAIL_FROM}{where} "
        f"GROUP BY r.order_no "
    )
    having = (f"HAVING {unhandled_expr} > 0 AND {at_least_one_tested} "
              if pending else "")
    order = (f"ORDER BY MIN(r.registered_at) "
             f"{'ASC' if str(sort_dir).lower() == 'asc' else 'DESC'} ")

    conn = get_conn()
    total = conn.execute(
        f"SELECT COUNT(*) c FROM (SELECT r.order_no {_DETAIL_FROM}{where} "
        f"GROUP BY r.order_no {having});", params
    ).fetchone()["c"]

    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 50)), 500)
    offset = (page - 1) * page_size

    rows = conn.execute(
        base + having + order + "LIMIT ? OFFSET ?;", params + [page_size, offset]
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        lines = d["lines"] or 0
        tested = d["tested"] or 0
        handled = d["handled"] or 0
        d["untested"] = max(0, lines - tested)       # 尚未检测的行数
        d["unhandled"] = max(0, lines - handled)     # 尚未处理的行数
        if lines and handled >= lines:
            d["status"], d["status_key"] = "已处理", "done"
        elif handled == 0:
            d["status"], d["status_key"] = "待处理", "untested"
        else:
            d["status"], d["status_key"] = "部分处理", "testing"
        out.append(d)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "rows": out,
    }


def query_all(filters: dict, limit: int = 50000) -> list:
    """导出用：取全部匹配行。"""
    where, params = _build_where(filters or {})
    conn = get_conn()
    rows = conn.execute(
        f"SELECT {_DETAIL_COLS} {_DETAIL_FROM}{where} "
        f"ORDER BY r.id DESC LIMIT ?;", params + [limit]
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 字典选项
# ---------------------------------------------------------------------------

def _prune_dict_field(conn, field: str) -> int:
    """删除该字段中已不存在于退回登记数据的候选值（墓碑清理）。

    与下面的汇总查询使用同一套筛选口径，保证「下拉框看得到的」
    与「搜索搜得到的」完全一致。检测字段的清理在 repo_inspect 中。
    """
    _safe_field(field)
    cur = conn.execute(
        f"""DELETE FROM dict_option
            WHERE field = ?
              AND value NOT IN (
                SELECT value FROM (
                  SELECT TRIM({field}) AS value, COUNT(*) AS n FROM returns
                  WHERE {field} IS NOT NULL AND TRIM({field}) <> ''
                  GROUP BY {field} ORDER BY n DESC LIMIT {int(DICT_OPTION_LIMIT)}
                )
              );""",
        (field,),
    )
    return cur.rowcount


def refresh_dict_options() -> dict:
    """重建退回登记库的字典候选，并转发检测库 / 处理库的重建。

    采用「重建」而非「累加」：先汇总当前数据的真实候选集，再删掉不在
    集合内的旧值。否则被删记录的值会永久留在下拉框里（墓碑），
    且与实时搜索（distinct_values 直查数据表）的返回口径不一致 ——
    表现为「下拉框看得到、输入关键字却搜不到」。
    """
    now = _now()
    pruned = 0
    with tx() as c:
        for field in RETURNS_DICT_FIELDS:
            _safe_field(field)
            pruned += _prune_dict_field(c, field)
            rows = c.execute(
                f"SELECT {field} AS v, COUNT(*) AS n FROM returns "
                f"WHERE {field} IS NOT NULL AND TRIM({field}) <> '' "
                f"GROUP BY {field} ORDER BY n DESC LIMIT ?;",
                (DICT_OPTION_LIMIT,),
            ).fetchall()
            for r in rows:
                c.execute(
                    """INSERT INTO dict_option (field, value, use_count, updated_at)
                       VALUES (?,?,?,?)
                       ON CONFLICT(field, value) DO UPDATE SET
                         use_count = excluded.use_count,
                         updated_at = excluded.updated_at;""",
                    (field, str(r["v"]).strip(), r["n"], now),
                )
    # 检测 / 处理登记库的字典候选各存各的 —— 两个都要转发。
    # 漏掉处理库的话，这里的文档承诺与实际行为不一致：处理库候选只能靠
    # repo_handle 自己的写入路径顺带重建，一旦出现绕过它的新路径，
    # 就会静默留下「幽灵候选」（本项目历史上修过的同类缺陷）。
    pruned += repo_inspect.refresh_dict_options()
    pruned += repo_handle.refresh_dict_options()
    return {"pruned": pruned}


def get_dict_options(field: str = "") -> dict:
    """读取下拉候选。按字段归属分流到检测库 / 处理库，其余取退回登记库。"""
    if field:
        _safe_field(field)
        if field in HANDLE_FIELD_SET:
            return repo_handle.get_dict_options(field)
        if field in INSPECT_FIELD_SET:
            return repo_inspect.get_dict_options(field)
        conn = get_conn()
        rows = conn.execute(
            "SELECT value, use_count FROM dict_option WHERE field = ? "
            "ORDER BY use_count DESC, value;", (field,)
        ).fetchall()
        return {"field": field, "options": [dict(r) for r in rows]}

    conn = get_conn()
    out = {}
    for r in conn.execute(
        f"SELECT field, value, use_count FROM dict_option "
        f"WHERE field IN ({', '.join('?' for _ in RETURNS_DICT_FIELDS)}) "
        f"ORDER BY field, use_count DESC;",
        RETURNS_DICT_FIELDS,
    ).fetchall():
        out.setdefault(r["field"], []).append(
            {"value": r["value"], "use_count": r["use_count"]}
        )
    # 合并检测登记库的候选（两库各存各的，前端一次拉全）
    for fname, opts in repo_inspect.get_dict_options().items():
        out.setdefault(fname, []).extend(opts)
    return out


def distinct_values(field: str, keyword: str = "", limit: int = 300) -> list:
    """直接取某字段的去重值（用于筛选下拉，实时反映数据）。

    按字段归属分流到检测库 / 处理库，其余走退回登记库 —— 与字典快照同一口径。
    """
    _safe_field(field)
    if field in HANDLE_FIELD_SET:
        return repo_handle.distinct_values(field, keyword, limit)
    if field in INSPECT_FIELD_SET:
        return repo_inspect.distinct_values(field, keyword, limit)

    conn = get_conn()
    sql = (f"SELECT {field} AS v, COUNT(*) AS n FROM returns "
           f"WHERE {field} IS NOT NULL AND TRIM({field}) <> ''")
    params = []
    if keyword:
        sql += f" AND {field} LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(keyword)}%")
    sql += f" GROUP BY {field} ORDER BY n DESC LIMIT ?;"
    params.append(limit)
    return [{"value": r["v"], "use_count": r["n"]}
            for r in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# 统计（看板）
#
# 看板分两块，按 scope 分派：
#   scope="return"  —— 返件汇总：收到货就能确定的维度（时间 · 来源 · 产品分布）
#   scope="inspect" —— 检测汇总：送检后才能确定的维度（工作量 · 时效 · 归因）
#
# 两块的基础口径**不同**，这是刻意的：
# 检测数据稀疏存储（未检测的明细在检测库没有行），若检测汇总仍按全部返件统计，
# 故障原因 / 改善措施这类归因图会混入一大块空值，「检测量」也就不再是检测量。
# 所以检测汇总的明细一律带 _TESTED_SQL；只有「待检数」这类**必须含未检测记录**
# 的指标才退回全量口径（否则范围被收窄成已检测，永远算不出待检数）。
# ---------------------------------------------------------------------------

FAST_TEST_DAYS = 7          # 检测时效口径：退回后 N 天内完成检测算「及时」


def stats_overview(filters: dict = None) -> dict:
    """返件汇总 KPI —— 只看退回登记侧。"""
    where, params = _build_where(filters or {})
    conn = get_conn()
    row = conn.execute(
        f"""SELECT
              COUNT(*)                                   AS records,
              COALESCE(SUM(r.return_qty), 0)             AS qty,
              COALESCE(SUM(CASE WHEN r.sync_state = 'pending'
                        THEN 1 ELSE 0 END), 0)                                    AS pending_sync,
              COUNT(DISTINCT r.order_no)                 AS orders,
              COUNT(DISTINCT r.product_model)            AS models,
              COUNT(DISTINCT r.turbine_vendor)           AS vendors
            {_DETAIL_FROM}{where};""", params
    ).fetchone()
    out = {k: (0 if v is None else v) for k, v in dict(row).items()}

    # 本月新增（不受筛选影响，取全局）
    month_prefix = datetime.now().strftime("%Y-%m")
    out["this_month"] = conn.execute(
        "SELECT COUNT(*) c FROM returns WHERE substr(registered_at,1,7) = ?;",
        (month_prefix,)
    ).fetchone()["c"]
    return out


def stats_inspect_overview(filters: dict = None) -> dict:
    """检测汇总 KPI。

    以「筛选后的返件全量」为分母同时给出已检 / 待检，因此这里**不能用**
    scope="inspect" 收窄范围 —— 那会把待检数永远算成 0。
    已检与待检用 _TESTED_SQL / _UNTESTED_SQL 两个互补条件，不会重复计数。
    """
    filters = filters or {}
    conn = get_conn()
    where_all, params_all = _build_where(filters)

    dated = (f"r.return_date IS NOT NULL AND TRIM(r.return_date) <> '' "
             f"AND i.test_date IS NOT NULL AND TRIM(i.test_date) <> '' "
             f"AND julianday(i.test_date) >= julianday(r.return_date)")
    in_time = f"{dated} AND julianday(i.test_date) - julianday(r.return_date) <= {FAST_TEST_DAYS}"

    row = conn.execute(
        f"""SELECT
              COUNT(*) AS records,
              COALESCE(SUM(CASE WHEN {_TESTED_SQL} THEN 1 ELSE 0 END), 0)      AS inspected,
              COALESCE(SUM(CASE WHEN {_UNTESTED_SQL} THEN 1 ELSE 0 END), 0)    AS untested,
              COALESCE(SUM(CASE WHEN {_TESTED_SQL} AND ({_PENDING_SQL})
                        THEN 1 ELSE 0 END), 0)                                 AS unfinished,
              COALESCE(SUM(CASE WHEN {_TESTED_SQL} AND {dated}
                        THEN 1 ELSE 0 END), 0)                                 AS dated,
              COALESCE(SUM(CASE WHEN {_TESTED_SQL} AND {in_time}
                        THEN 1 ELSE 0 END), 0)                                 AS fast,
              COUNT(DISTINCT CASE WHEN {_TESTED_SQL}
                        THEN r.order_no END)                                   AS orders
            {_DETAIL_FROM}{where_all};""", params_all
    ).fetchone()
    out = {k: (0 if v is None else v) for k, v in dict(row).items()}

    avg_row = conn.execute(
        f"""SELECT AVG(julianday(i.test_date) - julianday(r.return_date)) AS d
            {_DETAIL_FROM}{where_all}
            {" AND " if where_all else " WHERE "}
              r.return_date IS NOT NULL AND TRIM(r.return_date) <> ''
              AND i.test_date IS NOT NULL AND TRIM(i.test_date) <> ''
              AND julianday(i.test_date) >= julianday(r.return_date);""",
        params_all
    ).fetchone()
    out["avg_test_days"] = round(avg_row["d"], 1) if avg_row and avg_row["d"] else 0
    out["coverage"] = round(out["inspected"] * 100.0 / out["records"], 1) if out["records"] else 0
    out["fast_rate"] = round(out["fast"] * 100.0 / out["dated"], 1) if out["dated"] else 0
    out["fast_days"] = FAST_TEST_DAYS
    return out


def stats_group(field: str, filters: dict = None, limit: int = 12,
                scope: str = "", order: str = "value") -> list:
    """按字段分组计数，用于饼图 / 柱状图。字段可属两库中任一。

    order="value" 按数量倒序（TOP 榜）；order="label" 按取值排序 ——
    用于「生产年份」这类**分布**，按年份读比按数量读更自然。
    """
    # 归一到 _value_expr：erp_handled 这类字段若库里空值与「待处理」并存，
    # 直接按裸列分组会拆成两组（同一含义两种标签）。
    col = _value_expr(field)
    where, params = _build_where(filters or {}, scope)
    joiner = " AND " if where else " WHERE "
    order_sql = (f"ORDER BY {col} ASC" if order == "label"
                 # 二级按 label 排 —— 并列条数时顺序才稳定，不会出现
                 # 「TOP 厂家图」与「厂家→产品表」两家厂家对不上的情况
                 else "ORDER BY value DESC, label ASC")
    sql = (f"SELECT {col} AS label, COUNT(*) AS value, "
           f"COALESCE(SUM(r.return_qty),0) AS qty {_DETAIL_FROM}{where}"
           f"{joiner}{col} IS NOT NULL AND TRIM({col}) <> '' "
           f"GROUP BY {col} {order_sql} LIMIT ?;")
    conn = get_conn()
    return [dict(r) for r in conn.execute(sql, params + [limit]).fetchall()]


def stats_trend(filters: dict = None, granularity: str = "month",
                date_field: str = "return_date", scope: str = "") -> list:
    """按指定日期字段统计趋势。

    date_field="return_date" 画返件趋势，="test_date" 画检测趋势 ——
    两者共用同一套粒度与筛选，前端叠在同一张图里即可看出积压。
    """
    col = _qualify(date_field)
    where, params = _build_where(filters or {}, scope)
    joiner = " AND " if where else " WHERE "
    fmt = {"month": "%Y-%m", "week": "%Y-W%W", "day": "%Y-%m-%d",
           "year": "%Y"}.get(granularity, "%Y-%m")
    sql = (f"SELECT strftime('{fmt}', {col}) AS label, "
           f"COUNT(*) AS value, COALESCE(SUM(r.return_qty),0) AS qty "
           f"{_DETAIL_FROM}{where}{joiner}{col} IS NOT NULL "
           f"AND TRIM({col}) <> '' "
           f"GROUP BY label ORDER BY label;")
    conn = get_conn()
    return [dict(r) for r in conn.execute(sql, params).fetchall() if r["label"]]


def stats_cross_index(filters: dict = None, left: str = "turbine_vendor",
                      right: str = "product_category", limit: int = 8,
                      scope: str = "", right_limit: int = 0) -> dict:
    """交叉维度：左轴 TOP N × 右轴分类，用于堆叠柱状图。

    right_limit > 0 时右轴只保留全局 TOP N 项，其余并入「其他」——
    堆叠图的颜色数量有限（约 10 色），右轴有几十种取值时必须收口，
    否则后半截颜色全在重复、图例也没法看。
    """
    lcol, rcol = _qualify(left), _qualify(right)
    where, params = _build_where(filters or {}, scope)
    joiner = " AND " if where else " WHERE "
    sql = (
        f"SELECT {lcol} AS l, {rcol} AS r, COUNT(*) AS v {_DETAIL_FROM}{where}"
        f"{joiner}{lcol} IS NOT NULL AND {rcol} IS NOT NULL "
        f"AND TRIM({lcol})<>'' AND TRIM({rcol})<>'' "
        f"GROUP BY l, r ORDER BY v DESC LIMIT 2000;"
    )
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    left_totals = {}
    for r in rows:
        left_totals[r["l"]] = left_totals.get(r["l"], 0) + r["v"]
    lefts = sorted(left_totals, key=lambda k: -left_totals[k])[:limit]

    if right_limit:
        right_totals = {}
        for r in rows:
            if r["l"] in lefts and r["r"] != "其他":
                right_totals[r["r"]] = right_totals.get(r["r"], 0) + r["v"]
        keep = set(sorted(right_totals, key=lambda k: -right_totals[k])[:right_limit])

        def _bucket(name: str) -> str:
            return name if name in keep else "其他"
    else:
        def _bucket(name: str) -> str:
            return name

    rights = sorted({_bucket(r["r"]) for r in rows if r["l"] in lefts})
    matrix = {r: {l: 0 for l in lefts} for r in rights}
    for r in rows:
        if r["l"] in lefts:
            matrix[_bucket(r["r"])][r["l"]] += r["v"]
    return {"left": lefts, "right": rights, "matrix": matrix}


def stats_vendor_products(filters: dict = None, scope: str = "inspect",
                          vendor_limit: int = 10, item_limit: int = 3,
                          item_field: str = "product_category") -> dict:
    """TOP 厂家及其主要构成 —— 回答「返检集中的厂家，主要是什么在返」。

    item_field 指定下钻维度：默认 product_category（产品类别），也可传
    product_model（型号）。类别粒度更粗、跨厂家可比；型号是厂家专属的，
    各厂家体系互不相通，只在需要精确到料号时才用。

    该维度为空的行仍计入厂家总量（否则厂家条数与「TOP 厂家」图对不上），
    只是不进明细。
    """
    col = _qualify(item_field)
    where, params = _build_where(filters or {}, scope)
    joiner = " AND " if where else " WHERE "
    sql = (
        f"SELECT r.turbine_vendor AS v, {col} AS m, COUNT(*) AS n "
        f"{_DETAIL_FROM}{where}{joiner}"
        f"r.turbine_vendor IS NOT NULL AND TRIM(r.turbine_vendor) <> '' "
        f"GROUP BY v, m ORDER BY n DESC;"
    )
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    totals, buckets = {}, {}
    for r in rows:
        v = r["v"]
        totals[v] = totals.get(v, 0) + r["n"]
        m = (r["m"] or "").strip()
        if m:
            buckets.setdefault(v, []).append({"label": m, "value": r["n"]})

    grand = sum(totals.values()) or 1
    out = []
    # 与 stats_group 同序（条数倒序 + 名称升序），否则并列条数的厂家
    # 在「TOP 厂家图」和这份下钻里顺序不同，看起来像两套数据
    for v in sorted(totals, key=lambda k: (-totals[k], k))[:vendor_limit]:
        top = buckets.get(v, [])[:item_limit]
        shown = sum(p["value"] for p in top)
        out.append({
            "label": v,
            "value": totals[v],
            "share": round(totals[v] * 100.0 / grand, 1),
            "items": [
                {**p, "share": round(p["value"] * 100.0 / totals[v], 1)}
                for p in top
            ],
            # 厂家总量里「未被上面列出的产品」占多少 —— 让明细不至于误导成
            # 这个厂家只有这几款产品
            "others": totals[v] - shown,
        })
    return {"vendors": out, "total": grand}


def stats_dashboard(filters: dict = None, granularity: str = "month",
                    scope: str = "return") -> dict:
    """看板一次性取数，减少前端往返。

    scope 决定返回哪一套键 —— 返件汇总与检测汇总各自独立，
    前端按页签切换即可，不需要为另一侧付多余的查询开销。
    """
    filters = filters or {}

    if scope == "inspect":
        return {
            "scope": "inspect",
            "overview": stats_inspect_overview(filters),
            "trend": stats_trend(filters, granularity, "test_date", "inspect"),
            "by_cause": stats_group("fault_cause", filters, 10, "inspect"),
            "by_vendor": stats_group("turbine_vendor", filters, 10, "inspect"),
            # 厂家 → 主要构成：按需求用**产品类别**（跨厂家可比），不用型号
            "vendor_products": stats_vendor_products(
                filters, "inspect", 10, 3, "product_category"),
            "by_issue_category": stats_group("issue_category", filters, 8, "inspect"),
            "by_responsibility": stats_group("responsibility", filters, 8, "inspect"),
            # 产品类别 × 故障原因：右轴**不截断** —— 由前端筛选按钮控制看哪几个，
            # 后端擅自归并「其他」会把用户想看的项藏进聚合值里
            "cross_category_cause": stats_cross_index(
                filters, "product_category", "fault_cause", 8, "inspect"),
        }

    return {
        "scope": "return",
        "overview": stats_overview(filters),
        "trend": stats_trend(filters, granularity, "return_date", "return"),
        "by_category": stats_group("product_category", filters, 12, "return"),
        "by_vendor": stats_group("turbine_vendor", filters, 12, "return"),
        # 不再按产品型号分组：返件汇总里型号的粒度太细、与厂家/类别的高频信息
        # 重叠，已按需求改成「产品类别 TOP」。（检测侧另有厂家→型号的下钻）
        "by_info_source": stats_group("info_source", filters, 8, "return"),
        "by_production_year": stats_group("production_year", filters, 10,
                                          "return", "label"),
        # 不再按「反馈现象」「项目风场」分组（按需求移除对应图表）——
        # 分组查询一并去掉，避免每次开页白跑两次 SQL
        # 不做「快递公司」「登记人」两块图（按需求移除）—— 对应的分组查询
        # 也一并去掉，避免每次请求白跑两次 SQL。
        "cross_vendor_category": stats_cross_index(
            filters, "turbine_vendor", "product_category", 8, "return"),
    }
