"""发货申请库（delivery.db）的数据访问

结构是**主表 + 明细子表**（本项目第一对真正的父子表）：

* `delivery_request`      —— 整单信息（8 个填写字段 + 系统字段）
* `delivery_request_item` —— 发货内容，一行一种产品，关联键 `request_no` + `line_no`

与其余模块的差别：退回登记是「单表 + 行号」（一表兼整单与明细），
检测/处理是「明细的 1:1 扩展」（主键 detail_key）。这里两表独立，
所以**不需要**进 repository.py 的 `_DETAIL_FROM` / `_qualify` 那套跨库骨架 ——
发货数据不参与明细查询、关键词检索与完结判定，自成一套查询。

三个设计要点（落地时最容易做错的地方）：

1. **明细的四个产品列是「选定结果」，不是输入**
   `material_no` / `product_model` / `product_name` / `spec` 都由前端的
   「产品搜索框」选定候选后带出；搜索框里敲的文字**不落库**。
   后端在这里做结构校验兜底（料号与型号必须非空），防止绕过前端塞脏数据。

2. **单号靠「查最大值 + 1」，用锁兜住并发**
   SQLite 没有序列对象，`next_request_no()` 必须与 INSERT 在同一个临界区内
   完成，否则并发提交会撞 `request_no` 的 UNIQUE 约束。

3. **物料搜索在内存里打分**
   SQL 做不了「料号前缀 > 料号包含 > 型号包含 > 品名…」这种分级排序，
   所以全量取物料表 + Python 打分。物料表带一个「行数 + 最新更新时间」
   缓存键 —— 物料维护（增删 / 导入）后缓存自动失效，不需要别的模块来通知。
"""
import json
import re
import threading
from datetime import datetime

from config import (DELIVERY_CLEAR_REASON_MIN, DELIVERY_LEDGER_DIMENSIONS,
                    DELIVERY_MATCH_LIMIT, DELIVERY_MATCH_SCORES,
                    DELIVERY_NO_PREFIX, DELIVERY_NO_SEQ_WIDTH,
                    DELIVERY_SHIP_SOURCE_DEFAULT, DELIVERY_SHIP_SOURCES,
                    DELIVERY_STATUS, DELIVERY_STATUS_SHIPPED,
                    DELIVERY_STATUS_SUBMIT, DELIVERY_TRACK_STATES,
                    SHIP_DETAIL_COLUMNS, SHIP_DETAIL_DEFAULT_STATUS,
                    SHIP_DETAIL_MAX_PAGE_SIZE, SHIP_DETAIL_PAGE_SIZE,
                    SHIP_DETAIL_SEARCH_FIELDS, SHIP_DETAIL_SORTABLE)
from core.db import InvalidField, get_conn, tx
from core import recycle

# 主表可写字段（其余字段由系统生成，不接受前端传入）
_HEADER_FIELDS = (
    "turbine_vendor", "project_site", "expect_ship_date", "ship_address",
    "ship_contact", "ship_phone", "replace_reason", "express_req",
)
# 明细可写字段（line_no / request_no 由服务端按顺序生成）
_ITEM_FIELDS = ("material_no", "product_model", "product_name", "spec",
                "qty", "need_return")

# 主表必填（与前端一致，这里是最后一道防线）
_REQUIRED_HEADER = _HEADER_FIELDS

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PHONE_RE = re.compile(r"^(1[3-9]\d{9}|0\d{2,3}-?\d{7,8}(-\d{1,5})?)$")

# 单号临界区：取号 + 插入必须在锁内完成（见模块 docstring 第 2 点）
_SEQ_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 物料模糊搜索（明细行「产品」栏的候选来源）
# ---------------------------------------------------------------------------

def _norm(value) -> str:
    """全角转半角 + 去掉所有空白 + 统一大写。

    这样 `１０００１－０００１`、`10001 0001`、`blf1-c` 都能搜到；
    也避免用户多打一个空格就搜不到。
    """
    out = []
    for ch in str(value if value is not None else ""):
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:        # 全角 ASCII → 半角
            out.append(chr(code - 0xFEE0))
        elif ch in " \t\r\n\u3000":
            continue
        else:
            out.append(ch)
    return "".join(out).upper()


# 物料表缓存：key = (行数, 最新 updated_at)，物料变动后自动失效
_ITEM_CACHE = {"key": None, "rows": None}


def _load_items() -> list:
    """全量取物料表并预算好归一化字段（1735 条约 20ms，只在缓存失效时做）。"""
    conn = get_conn()
    sig = conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m "
        "FROM items_db.item_master;").fetchone()
    key = (sig["c"], sig["m"])
    if _ITEM_CACHE["key"] == key and _ITEM_CACHE["rows"] is not None:
        return _ITEM_CACHE["rows"]

    rows = []
    for r in conn.execute(
        "SELECT material_no, model_no, product_name, spec, description "
        "FROM items_db.item_master;"
    ):
        rows.append({
            "material_no": r["material_no"] or "",
            "product_model": r["model_no"] or "",
            "product_name": r["product_name"] or "",
            "spec": r["spec"] or "",
            "description": r["description"] or "",
            # 归一化结果挂在行上，搜索时直接用（前缀下划线 = 内部字段，不外传）
            "_n": (_norm(r["material_no"]), _norm(r["model_no"]),
                   _norm(r["product_name"]), _norm(r["spec"]),
                   _norm(r["description"])),
        })
    _ITEM_CACHE["key"] = key
    _ITEM_CACHE["rows"] = rows
    return rows


def _score(item: dict, word: str) -> int:
    """单个关键词在一条物料上的得分（0 = 未命中）。

    权重表见 config.DELIVERY_MATCH_SCORES：
      料号 精确1000 > 前缀800 > 包含600（唯一键，命中即定，排最高）
      型号 精确500 > 前缀450 > 包含400（前缀档是给「只记得前几个字符」的输入方式）
      品名300 > 规格200 > 描述100（描述只作兜底噪声）
    """
    no, mo, nm, sp, de = item["_n"]
    s = DELIVERY_MATCH_SCORES
    score = 0
    if no == word:
        score += s["material_no_exact"]
    elif no.startswith(word):
        score += s["material_no_prefix"]
    elif word in no:
        score += s["material_no_part"]
    if mo == word:
        score += s["model_exact"]
    elif mo.startswith(word):
        score += s["model_prefix"]
    elif word in mo:
        score += s["model_part"]
    if word in nm:
        score += s["name_part"]
    if word in sp:
        score += s["spec_part"]
    if word in de:
        score += s["desc_part"]
    return score


def search_items(keyword: str = "", limit: int = 0) -> dict:
    """物料模糊搜索 —— 匹配 料号 / 型号 / 品名 / 规格 / 描述。

    规则（见《发货申请单-字段定义》2.3）：
      · 归一化后按**空格分词**，每个词都要命中（AND，可跨字段）
      · 得分累加，降序；同分按料号升序（保证结果稳定、可预期）
      · 只返回前 `limit` 条（默认 config.DELIVERY_MATCH_LIMIT），
        并回报 total，前端据此提示「继续输入可收窄」

    返回的每条含 material_no / product_model / product_name / spec / description。
    """
    limit = int(limit or DELIVERY_MATCH_LIMIT)
    raw = str(keyword if keyword is not None else "").strip()
    words = [w for w in (_norm(x) for x in raw.split()) if w]
    if not words:
        return {"keyword": raw, "total": 0, "limit": limit, "rows": []}

    hits = []
    for item in _load_items():
        total = 0
        for word in words:
            part = _score(item, word)
            if not part:
                break           # 有一个词没命中 → 整条淘汰（AND）
            total += part
        else:
            hits.append((total, item))

    hits.sort(key=lambda pair: (-pair[0], pair[1]["material_no"]))
    rows = []
    for _, item in hits[:limit]:
        rows.append({k: v for k, v in item.items() if k != "_n"})
    return {"keyword": raw, "total": len(hits), "limit": limit, "rows": rows}


def get_item(material_no: str) -> dict:
    """按料号取一条物料（前端选定候选后可用它复核）。"""
    key = _norm(material_no)
    if not key:
        return {}
    for item in _load_items():
        if item["_n"][0] == key:
            return {k: v for k, v in item.items() if k != "_n"}
    return {}


# ---------------------------------------------------------------------------
# 单号与序号
# ---------------------------------------------------------------------------

def next_request_no(day: str = "") -> str:
    """生成下一个申请单号：`FH` + `YYYYMMDD` + 3 位当日流水。

    例：FH20260921001。日期段用 8 位（与退回登记的 6 位短号不同 ——
    发货单号要对外沟通，年份写全更不容易混）。
    """
    day = (day or _today()).strip()
    prefix = DELIVERY_NO_PREFIX + day.replace("-", "")
    conn = get_conn()
    row = conn.execute(
        # ⚠️ MySQL 没有 CAST(... AS INTEGER)，整数用 SIGNED（理由同 repository.py）
        "SELECT MAX(CAST(substr(request_no, ?) AS SIGNED)) AS m "
        "FROM delivery_db.delivery_request WHERE request_no LIKE ?;",
        (len(prefix) + 1, prefix + "%"),
    ).fetchone()
    seq = (row["m"] or 0) + 1
    return f"{prefix}{str(seq).zfill(DELIVERY_NO_SEQ_WIDTH)}"


def exists(request_no: str) -> bool:
    conn = get_conn()
    return conn.execute(
        "SELECT 1 FROM delivery_db.delivery_request WHERE request_no = ?;",
        (str(request_no or "").strip(),)).fetchone() is not None


def count() -> int:
    conn = get_conn()
    return conn.execute(
        "SELECT COUNT(*) AS c FROM delivery_db.delivery_request;").fetchone()["c"]


# ---------------------------------------------------------------------------
# 清洗与校验
# ---------------------------------------------------------------------------

def _clean_header(header: dict, partial: bool = False) -> dict:
    """清洗主表数据：只留白名单字段、去首尾空格、做必填与格式校验。"""
    header = header or {}
    data = {}
    for name in _HEADER_FIELDS:
        value = header.get(name)
        if value is None:
            continue
        data[name] = str(value).strip()

    missing = [n for n in _REQUIRED_HEADER if not data.get(n)]
    # 编辑时允许只传部分字段：只校验「传了的那些」，没传的保持原值
    if partial:
        missing = [n for n in missing if n in data]
    if missing:
        from config import FIELD_LABELS
        labels = "、".join(FIELD_LABELS.get(n, n) for n in missing)
        raise ValueError(f"以下必填项不能为空：{labels}")

    if "expect_ship_date" in data and data["expect_ship_date"]:
        if not _DATE_RE.match(data["expect_ship_date"]):
            raise ValueError("期望发货日格式应为 YYYY-MM-DD")
    if data.get("ship_phone") and not _PHONE_RE.match(data["ship_phone"]):
        raise ValueError(
            "电话格式不对：手机填 11 位（1 开头）；座机写「区号-号码」，如 0577-12345678")
    return data


def _clean_items(items) -> list:
    """清洗明细：**每行必须已从物料库选定**（料号 + 型号非空）。

    这是「产品必须从候选中选定、不接受手工填写」在后端的兜底 ——
    手填的品名给不出料号 / 型号，台账核销就失去依据。
    """
    cleaned = []
    for idx, raw in enumerate(items or []):
        if not isinstance(raw, dict):
            continue
        row = {}
        for name in _ITEM_FIELDS:
            value = raw.get(name)
            if value is None:
                continue
            row[name] = str(value).strip() if name != "qty" else value

        material_no = str(row.get("material_no") or "").strip()
        model = str(row.get("product_model") or "").strip()
        if not material_no or not model:
            raise ValueError(
                f"第 {idx + 1} 行产品没有从候选里选定 —— "
                "产品必须从物料库候选中选定，不能手工填写")
        row["material_no"] = material_no
        row["product_model"] = model

        qty = row.get("qty", 1)
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            raise ValueError(f"第 {idx + 1} 行数量必须是整数") from None
        if qty < 1:
            raise ValueError(f"第 {idx + 1} 行数量必须 ≥ 1")
        row["qty"] = qty

        # 逐行「是否需要返回」：默认 1（需返回）。前端传布尔或 0/1 都认。
        need = row.get("need_return", 1)
        row["need_return"] = 1 if str(need).lower() in ("1", "true", "yes", "on") else 0

        cleaned.append(row)
    return cleaned


def _write_items(conn, request_no: str, items: list, now: str, start: int = 1) -> None:
    """按顺序插入明细行（调用方保证已在事务内）。"""
    cols = ["request_no", "line_no"] + list(_ITEM_FIELDS) + ["created_at", "updated_at"]
    marks = ", ".join("?" for _ in cols)
    sql = (f"INSERT INTO delivery_db.delivery_request_item ({', '.join(cols)}) "
           f"VALUES ({marks});")
    for offset, row in enumerate(items):
        values = [request_no, start + offset]
        for name in _ITEM_FIELDS:
            values.append(row.get(name, "" if name != "qty" else 1))
        values += [now, now]
        conn.execute(sql, values)


def _log(conn, action: str, request_no: str, payload: dict,
         operator: str, now: str) -> None:
    conn.execute(
        "INSERT INTO delivery_db.op_log "
        "(action, request_no, payload, operator, created_at) VALUES (?,?,?,?,?);",
        (action, request_no,
         json.dumps(payload or {}, ensure_ascii=False), operator or "", now))


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def create_request(header: dict, items: list, operator: str = "") -> dict:
    """新建发货申请（主表 + 明细，同一事务）。

    `header` 里若带了 request_no / applicant / apply_date / status 会用其值，
    否则自动生成（申请人取 operator）。
    """
    clean_items = _clean_items(items)
    if not clean_items:
        raise ValueError("至少需要一行产品明细")

    header = header or {}
    # 取号 + 插入放在同一临界区：SQLite 没有序列对象，靠「查最大值 + 1」，
    # 并发下两个请求可能取到同一个号，撞 request_no 的 UNIQUE 约束。
    with _SEQ_LOCK:
        now = _now()
        day = _today()
        payload = _clean_header(header)
        request_no = str(header.get("request_no") or "").strip() or next_request_no(day)
        payload["request_no"] = request_no
        payload["applicant"] = str(header.get("applicant") or operator or "").strip()
        payload["apply_date"] = str(header.get("apply_date") or day).strip()
        status = str(header.get("status") or DELIVERY_STATUS_SUBMIT).strip()
        payload["status"] = status if status in DELIVERY_STATUS else DELIVERY_STATUS_SUBMIT

        cols = list(payload.keys()) + ["created_at", "updated_at"]
        marks = ", ".join("?" for _ in cols)
        with tx() as conn:
            conn.execute(
                f"INSERT INTO delivery_db.delivery_request ({', '.join(cols)}) "
                f"VALUES ({marks});",
                list(payload.values()) + [now, now])
            _write_items(conn, request_no, clean_items, now)
            _log(conn, "create", request_no, payload, operator, now)

    return {"request_no": request_no, "count": len(clean_items),
            "status": payload["status"], "apply_date": payload["apply_date"]}


def update_request(request_no: str, header: dict = None, items: list = None,
                   operator: str = "") -> dict:
    """编辑申请单。

    * `header` 传了就更新（支持只传部分字段 —— 未提交的字段保持原值）
    * `items` 传了就**整组替换**（先删后插，line_no 重新按 1..N 编号）

    明细之所以整组替换而不是逐行 diff：行可能被增、删、改、换序，
    diff 的收益抵不过「漏判某一行」的风险。明细行数不多（通常 < 20），
    整组替换的开销可以忽略。
    """
    request_no = str(request_no or "").strip()
    if not request_no or not exists(request_no):
        raise ValueError(f"申请单不存在：{request_no}")

    now = _now()
    with _SEQ_LOCK:
        with tx() as conn:
            changed = {}
            if header is not None:
                data = _clean_header(header, partial=True)
                if data:
                    sets = ", ".join(f"{k} = ?" for k in data)
                    conn.execute(
                        f"UPDATE delivery_db.delivery_request SET {sets}, "
                        f"updated_at = ? WHERE request_no = ?;",
                        list(data.values()) + [now, request_no])
                    changed.update(data)

            count = None
            if items is not None:
                clean_items = _clean_items(items)
                if not clean_items:
                    raise ValueError("至少需要一行产品明细")
                conn.execute(
                    "DELETE FROM delivery_db.delivery_request_item "
                    "WHERE request_no = ?;", (request_no,))
                _write_items(conn, request_no, clean_items, now)
                count = len(clean_items)
                conn.execute(
                    "UPDATE delivery_db.delivery_request SET updated_at = ? "
                    "WHERE request_no = ?;", (now, request_no))

            _log(conn, "update", request_no,
                 {"header": changed, "items": count}, operator, now)

    result = {"request_no": request_no, "updated_at": now}
    if count is not None:
        result["count"] = count
    return result


def delete_request(request_no: str, operator: str = "") -> int:
    """删除申请单（主表 + 明细 + 发货记录 + 留一条日志）。

    ★ **发货记录必须一并删**。它是申请单的附属数据，留着会产生「没有申请单的
      孤儿发货记录」；而单号是按「当日最大流水 + 1」重算的（删单后会**复用**），
      孤儿记录会被新单继承 —— 表现为「刚建的单一查发货记录，居然有两条」。
      2026-09-21 实测踩到（DOM 校验里新建的单复用了自检删掉的单号）。
    ★ 无条件删（即使主表那次没删到行）—— 顺手清掉历史孤儿。
    ★ 操作日志不删：删了什么、删掉几条发货记录，都留在 op_log 里可追溯。
    """
    request_no = str(request_no or "").strip()
    if not request_no:
        return 0
    now = _now()
    with tx() as conn:
        shipped = conn.execute(
            "SELECT COUNT(*) AS c FROM delivery_db.delivery_shipment "
            "WHERE request_no = ?;", (request_no,)).fetchone()["c"]
        cur = conn.execute(
            "DELETE FROM delivery_db.delivery_request WHERE request_no = ?;",
            (request_no,))
        changed = cur.rowcount
        conn.execute(
            "DELETE FROM delivery_db.delivery_request_item WHERE request_no = ?;",
            (request_no,))
        conn.execute(
            "DELETE FROM delivery_db.delivery_shipment WHERE request_no = ?;",
            (request_no,))
        if changed:
            _log(conn, "delete", request_no,
                 {"shipped_records_removed": shipped}, operator, now)
    return changed


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

_SORTABLE = {"request_no", "apply_date", "expect_ship_date", "id",
             "applicant", "status"}


def query_requests(filters: dict = None, page: int = 1, page_size: int = 50,
                   sort_by: str = "id", sort_dir: str = "desc") -> dict:
    """申请单列表（一行一单），带筛选与分页。

    筛选支持：keyword（单号 / 收件人 / 地址 / 调换原因模糊）、
    applicant / turbine_vendor / project_site / status 精确、
    apply_date 与 expect_ship_date 的区间。
    """
    filters = {k: v for k, v in (filters or {}).items() if v not in (None, "", 0, False)}
    where, params = [], []

    keyword = str(filters.get("keyword") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        where.append("(request_no LIKE ? OR ship_contact LIKE ? OR ship_address LIKE ? "
                     "OR replace_reason LIKE ? OR project_site LIKE ?)")
        params += [like] * 5

    for name in ("applicant", "turbine_vendor", "project_site", "status"):
        value = filters.get(name)
        if value:
            where.append(f"{name} = ?")
            params.append(str(value).strip())

    for name, op in (("apply_date_from", ">="), ("apply_date_to", "<="),
                     ("expect_date_from", ">="), ("expect_date_to", "<=")):
        value = filters.get(name)
        if not value:
            continue
        col = "apply_date" if name.startswith("apply") else "expect_ship_date"
        where.append(f"TRIM(COALESCE({col}, '')) <> '' AND {col} {op} ?")
        params.append(str(value).strip())

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    conn = get_conn()
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM delivery_db.delivery_request{clause};",
        params).fetchone()["c"]

    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 50)), 500)
    sort_by = sort_by if sort_by in _SORTABLE else "id"
    sort_dir = "ASC" if str(sort_dir).lower() == "asc" else "DESC"

    rows = conn.execute(
        f"SELECT * FROM delivery_db.delivery_request{clause} "
        f"ORDER BY {sort_by} {sort_dir}, id {sort_dir} LIMIT ? OFFSET ?;",
        params + [page_size, (page - 1) * page_size]).fetchall()

    # 顺带带上每单的明细行数与件数（列表里显示「3 种 / 6 件」）
    stats = {}
    nos = [r["request_no"] for r in rows if r["request_no"]]
    if nos:
        marks = ", ".join("?" for _ in nos)
        for s in conn.execute(
            f"SELECT request_no, COUNT(*) AS `lines`, COALESCE(SUM(qty), 0) AS qty "
            f"FROM delivery_db.delivery_request_item "
            f"WHERE request_no IN ({marks}) GROUP BY request_no;", nos):
            stats[s["request_no"]] = {"lines": s["lines"], "qty": s["qty"]}

    out = []
    for r in rows:
        item = dict(r)
        st = stats.get(r["request_no"], {})
        item["line_count"] = st.get("lines", 0)
        item["total_qty"] = st.get("qty", 0)
        item["status_label"] = DELIVERY_STATUS.get(r["status"], r["status"] or "")
        out.append(item)

    return {"rows": out, "total": total, "page": page, "page_size": page_size}


def list_items(request_no: str) -> list:
    """取某单的全部明细行（按 line_no 升序）。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM delivery_db.delivery_request_item "
        "WHERE request_no = ? ORDER BY line_no ASC;",
        (str(request_no or "").strip(),)).fetchall()
    return [dict(r) for r in rows]


def get_request(request_no: str, with_items: bool = True) -> dict:
    """取申请单详情（含明细）。"""
    request_no = str(request_no or "").strip()
    if not request_no:
        return {}
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM delivery_db.delivery_request WHERE request_no = ?;",
        (request_no,)).fetchone()
    if not row:
        return {}
    data = dict(row)
    data["status_label"] = DELIVERY_STATUS.get(row["status"], row["status"] or "")
    if with_items:
        data["items"] = list_items(request_no)
        data["total_qty"] = sum(int(i.get("qty") or 0) for i in data["items"])
        data["need_return_qty"] = sum(
            int(i.get("qty") or 0) for i in data["items"] if i.get("need_return"))
    return data


def list_logs(request_no: str = "", limit: int = 50) -> list:
    """操作日志（按单筛选，倒序）。"""
    conn = get_conn()
    if request_no:
        rows = conn.execute(
            "SELECT * FROM delivery_db.op_log WHERE request_no = ? "
            "ORDER BY id DESC LIMIT ?;", (str(request_no).strip(), int(limit)))
    else:
        rows = conn.execute(
            "SELECT * FROM delivery_db.op_log ORDER BY id DESC LIMIT ?;",
            (int(limit),))
    return [dict(r) for r in rows]


def status_options() -> list:
    """状态枚举（供前端下拉）。"""
    return [{"value": k, "label": v} for k, v in DELIVERY_STATUS.items()]

# ---------------------------------------------------------------------------
# ② 待发货清单（**派生视图，不落表**）+ 标记已发货（出队）
#
# 设计依据见《框架大纲》5.2 ②：
#   * 本质是申请表的派生视图 —— 筛 status='submitted' 的整单；
#   * **不单独落表**（遵循「派生表必须能重建」的既有约定）；
#   * 出队条件：该单的实际发货被确认 → 自动移出清单。
#
# 出队的动作 = **写一条 delivery_shipment + 把状态改成 shipped**，同一事务。
# 现在由人工在清单页点「标记已发货」触发；将来 ③ 接 ERP 后由同步任务写
# 同一张表（source='erp'），**清单页与业务不用改** —— 这也是为什么发货记录
# 现在就要建表，而不是等 ③ 再补。
# ---------------------------------------------------------------------------

def _overdue_days(expect_date: str, today: str) -> int:
    """超期天数：期望发货日**早于**今天才算超期，返回正数；否则 0。

    日期格式不对时返回 0（**不猜**）—— 超期标红只是提示，宁可漏报，
    也不能因为解析异常让一张本来正常的单子显示成「超期 999 天」。
    """
    expect_date = str(expect_date or "").strip()
    if not _DATE_RE.match(expect_date):
        return 0
    try:
        d1 = datetime.strptime(expect_date, "%Y-%m-%d")
        d0 = datetime.strptime(today, "%Y-%m-%d")
    except ValueError:
        return 0
    days = (d0 - d1).days
    return days if days > 0 else 0


def query_pending(filters: dict = None, page: int = 1, page_size: int = 50) -> dict:
    """待发货清单（一行一单）。**派生视图，不落表。**

    筛选支持：keyword（单号 / 收件人 / 地址 / 项目名模糊）、
    turbine_vendor / project_site 精确、expect_date 区间、only_overdue（只看超期）。

    ★ **固定按「期望发货日升序」排** —— 清单要回答的是「接下来该发哪些」，
      最急的必须排在最前。这与申请单列表刻意不同（那边按 id 倒序，看最新提交的）。
      允许传 sort_by/sort_dir 覆盖，但默认就是清单语义的顺序。

    ★ 每行带 `overdue_days`（今天 − 期望发货日，正数表示已超期），
      前端据此标红「超期 N 天」；排序仍是按日期，两者一致。
    """
    filters = {k: v for k, v in (filters or {}).items()
               if v not in (None, "", 0, False)}
    today = _today()

    where = ["status = ?"]
    params = [DELIVERY_STATUS_SUBMIT]

    keyword = str(filters.get("keyword") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        where.append("(request_no LIKE ? OR ship_contact LIKE ? OR ship_address LIKE ? "
                     "OR project_site LIKE ? OR replace_reason LIKE ?)")
        params += [like] * 5

    for name in ("turbine_vendor", "project_site", "applicant"):
        value = filters.get(name)
        if value:
            where.append(f"{name} = ?")
            params.append(str(value).strip())

    for name, op in (("expect_date_from", ">="), ("expect_date_to", "<=")):
        value = filters.get(name)
        if value:
            where.append(f"TRIM(COALESCE(expect_ship_date, '')) <> '' "
                         f"AND expect_ship_date {op} ?")
            params.append(str(value).strip())

    # 只看超期：期望发货日 < 今天（空值不算超期 —— 它压根没日期可判）
    if filters.get("only_overdue"):
        where.append("TRIM(COALESCE(expect_ship_date, '')) <> '' "
                     "AND expect_ship_date < ?")
        params.append(today)

    clause = " WHERE " + " AND ".join(where)
    conn = get_conn()

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM delivery_db.delivery_request{clause};",
        params).fetchone()["c"]

    # 页头要显示的汇总（分页不会影响这几个数，单独查一次）
    agg = conn.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN TRIM(COALESCE(expect_ship_date,'')) <> ''
                                      AND expect_ship_date < ? THEN 1 ELSE 0 END), 0) AS overdue
            FROM delivery_db.delivery_request{clause};""",
        [today] + params).fetchone()

    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 50)), 500)
    sortable = {"expect_ship_date", "apply_date", "request_no", "id",
                "turbine_vendor", "project_site"}
    sort_by = filters.get("sort_by") if filters.get("sort_by") in sortable         else "expect_ship_date"
    sort_dir = "DESC" if str(filters.get("sort_dir")).lower() == "desc" else "ASC"

    rows = conn.execute(
        f"SELECT * FROM delivery_db.delivery_request{clause} "
        f"ORDER BY {sort_by} {sort_dir}, id ASC LIMIT ? OFFSET ?;",
        params + [page_size, (page - 1) * page_size]).fetchall()

    nos = [r["request_no"] for r in rows if r["request_no"]]
    stats = {}
    if nos:
        marks = ", ".join("?" for _ in nos)
        for st in conn.execute(
            f"""SELECT i.request_no, COUNT(*) AS `lines`,
                       COALESCE(SUM(i.qty), 0) AS qty,
                       COALESCE(SUM(CASE WHEN i.need_return THEN i.qty ELSE 0 END), 0)
                         AS return_qty,
                       COALESCE(SUM(CASE WHEN EXISTS (
                             SELECT 1 FROM delivery_db.delivery_shipment s
                              WHERE s.request_no = i.request_no
                                AND s.line_no = i.line_no)
                           THEN 1 ELSE 0 END), 0) AS shipped_lines
                FROM delivery_db.delivery_request_item i
                WHERE i.request_no IN ({marks}) GROUP BY i.request_no;""", nos):
            stats[st["request_no"]] = dict(st)

    # 已经发过货的单（把出队信息一并带出，便于排查「为什么这张不在清单里」）
    shipped_map = {}
    if nos:
        marks = ", ".join("?" for _ in nos)
        for sh in conn.execute(
            f"""SELECT request_no, COUNT(*) AS n, MAX(ship_date) AS last_date
                FROM delivery_db.delivery_shipment
                WHERE request_no IN ({marks}) GROUP BY request_no;""", nos):
            shipped_map[sh["request_no"]] = dict(sh)

    out = []
    for r in rows:
        item = dict(r)
        st = stats.get(r["request_no"], {})
        item["line_count"] = st.get("lines", 0)
        item["total_qty"] = st.get("qty", 0)
        item["return_qty"] = st.get("return_qty", 0)
        item["status_label"] = DELIVERY_STATUS.get(r["status"], r["status"] or "")
        item["overdue_days"] = _overdue_days(r["expect_ship_date"], today)
        sh = shipped_map.get(r["request_no"])
        item["ship_count"] = (sh or {}).get("n", 0)
        item["last_ship_date"] = (sh or {}).get("last_date") or ""
        # ★ 2026-09-21 改版：清单不再「标记已发货」，所以每一行要自己说清楚
        #   「已经登记了几行 / 共几行」—— 发货员才知道这张单还差不差东西。
        #   （部分发货的单仍留在清单里：它确实还有没做的。）
        item["apply_lines"] = item.get("line_count", 0)
        item["shipped_lines"] = st.get("shipped_lines", 0)
        item["pending_lines"] = max(0, item["apply_lines"] - item["shipped_lines"])
        item["partial"] = bool(item["shipped_lines"])
        out.append(item)

    # 总件数用「子查询」而不是 JOIN 主表 ——
    # 两张表都有 request_no / status 这些列名，JOIN 后 WHERE 里的裸列名会
    # 变成 ambiguous column name（2026-09-21 实测踩到）；子查询里只有一个表，
    # 条件原样复用，不用改写、也不会歧义。
    total_qty = 0
    if total:
        total_qty = conn.execute(
            f"""SELECT COALESCE(SUM(qty), 0) AS qty
                FROM delivery_db.delivery_request_item
                WHERE request_no IN (
                    SELECT request_no FROM delivery_db.delivery_request{clause});""",
            params).fetchone()["qty"]

    # 「部分已发」的单数（全量，不受分页影响）—— 清单要能一眼看出
    # 「哪些单已经开了个头、还没做完」，那是发货员最需要盯的。
    partial = conn.execute(
        f"""SELECT COUNT(*) AS c FROM delivery_db.delivery_request r{clause}
             AND EXISTS (SELECT 1 FROM delivery_db.delivery_request_item i
                          WHERE i.request_no = r.request_no
                            AND EXISTS (SELECT 1 FROM delivery_db.delivery_shipment s
                                         WHERE s.request_no = i.request_no
                                           AND s.line_no = i.line_no));""",
        params).fetchone()["c"]

    return {
        "rows": out, "total": total, "page": page, "page_size": page_size,
        "today": today,
        "summary": {
            "orders": total,
            "overdue": agg["overdue"] if agg else 0,
            "qty": total_qty or 0,
            "partial": partial or 0,
        },
    }


def list_shipments(request_no: str = "", limit: int = 200) -> list:
    """发货记录（按单筛选）。③ 发货跟踪的主列表见 `query_shipments`。"""
    conn = get_conn()
    if request_no:
        rows = conn.execute(
            "SELECT * FROM delivery_db.delivery_shipment WHERE request_no = ? "
            "ORDER BY id DESC LIMIT ?;", (str(request_no).strip(), int(limit)))
    else:
        rows = conn.execute(
            "SELECT * FROM delivery_db.delivery_shipment ORDER BY id DESC LIMIT ?;",
            (int(limit),))
    out = []
    for r in rows:
        d = dict(r)
        d["source_label"] = DELIVERY_SHIP_SOURCES.get(d.get("source"), d.get("source") or "")
        out.append(d)
    return out


def pending_count() -> int:
    """待发货单数（导航角标 / 首页提示用）。"""
    conn = get_conn()
    return conn.execute(
        "SELECT COUNT(*) AS c FROM delivery_db.delivery_request WHERE status = ?;",
        (DELIVERY_STATUS_SUBMIT,)).fetchone()["c"]

# ---------------------------------------------------------------------------
# ③ 发货跟踪（delivery_shipment 的读写）
#
# 这张表有两个写入方：
#   * 人工 —— ② 待发货清单「标记已发货」（按申请明细逐行写）、本页手工登记与 Excel 导入；
#   * ERP  —— 将来的同步任务（`source='erp'`），**表结构不用改**。
#
# 三个约定：
#   1. **幂等**：同一 `(ship_no, material_no, request_no)` 重复同步/导入只更新，不新增
#      （大纲 5.2 ③ 的要求：避免台账虚增）。
#   2. **不猜**：与申请单的关联优先按单号自动对上；对不上就留空，
#      由人在页面上**人工挂接**并留痕 —— 绝不按"看起来像"去猜。
#   3. 未关联申请单的记录**照样参与核销**（厂家/风场冗余在自己身上）。
# ---------------------------------------------------------------------------

_SHIP_SORTABLE = {"id", "ship_date", "created_at", "ship_no", "qty"}
_SHIP_LIST_COLS = (
    "id, turbine_vendor, project_site, request_no, ship_no, ship_date, "
    "express_no, carrier, qty, line_no, material_no, product_model, "
    "need_return, source, remark, sync_at, operator, created_at, updated_at"
)


def _clean_shipment(payload: dict, partial: bool = False) -> dict:
    """校验并整理「手工登记 / 导入」的单条发货记录。"""
    d = {}
    for k in ("request_no", "ship_no", "express_no", "carrier", "material_no",
              "product_model", "turbine_vendor", "project_site", "remark",
              "ship_date"):
        d[k] = str((payload or {}).get(k) or "").strip()

    if not d["product_model"]:
        raise ValueError("产品型号必填 —— 发货跟踪按型号逐行核对，缺了认不出是哪件货。")
    if not d["turbine_vendor"]:
        raise ValueError("整机厂家必填（发货跟踪与核销台账都要用）。")

    ship_date = d["ship_date"] or _today()
    if not _DATE_RE.match(ship_date):
        raise ValueError("发货日期格式应为 YYYY-MM-DD。")
    d["ship_date"] = ship_date

    try:
        qty = int((payload or {}).get("qty") or 0)
    except (TypeError, ValueError):
        raise ValueError("发货件数应为整数。")
    if qty < 1:
        raise ValueError("发货件数至少为 1。")
    d["qty"] = qty

    d["need_return"] = 1 if (payload or {}).get("need_return", True) else 0
    src = str((payload or {}).get("source") or DELIVERY_SHIP_SOURCE_DEFAULT).strip()
    if src not in DELIVERY_SHIP_SOURCES:
        raise ValueError(f"未知的发货来源：{src}")
    d["source"] = src
    d["line_no"] = (payload or {}).get("line_no")
    return d


_STATE_ORDER = {"pending": 0, "shipped": 1, "unlinked": 2}


def _track_row(state: str, **kw) -> dict:
    """造一行发货跟踪（两种来源共用，未给的字段取空默认值）。

    ★ 行里有**两套数量**，别混：
      `apply_qty` 申请件数（申请单填的）、`ship_qty` 实发件数（登记时填的）。
      少发时两者不同 —— 列表两列都显示，`qty` 只是"这一行代表多少件"的展示值。
    """
    row = {
        "row_key": "", "kind": "request", "state": state,
        "request_no": "", "line_no": None,
        "turbine_vendor": "", "project_site": "",
        "product_model": "", "material_no": "", "product_name": "", "spec": "",
        "apply_qty": 0, "qty": 0, "need_return": 0,
        "expect_ship_date": "", "applicant": "", "apply_date": "",
        "req_status": "", "req_status_label": "",
        "id": None, "ship_id": None,
        "ship_no": "", "ship_date": "", "express_no": "", "carrier": "",
        "ship_qty": 0, "source": "", "remark": "", "sync_at": "",
        "operator": "", "created_at": "",
    }
    row.update(kw)
    row["state_label"] = DELIVERY_TRACK_STATES.get(state, state)
    row["source_label"] = DELIVERY_SHIP_SOURCES.get(row["source"], row["source"])
    row["shipped"] = state == "shipped"
    row["linked"] = bool(str(row["request_no"] or "").strip())
    row["need_return_label"] = "需返回" if row["need_return"] else "不返回"
    row["track_date"] = row["ship_date"] or row["expect_ship_date"]
    if not row["row_key"]:
        row["row_key"] = (f"req:{row['request_no']}:{row['line_no']}"
                          if row["kind"] == "request" else f"ship:{row['id']}")
    return row


def query_shipments(filters: dict = None, page: int = 1, page_size: int = 50,
                    sort_by: str = "", sort_dir: str = "") -> dict:
    """发货跟踪列表（③ 发货跟踪页）。

    ★ **数据来源是两条路的并集**（2026-09-21 改版，起因见下）：
      ① **申请明细**（`delivery_request_item`）—— 申请单**一提交就出现在这里**，
         那时还没有发货单号，状态是「待发货」。这是列表的**主体**：
         「发货跟踪」跟的是「申请了的货，发出去了没有」。
      ② **对不上明细的发货记录** —— `delivery_shipment` 里没有 `(申请单号, 行号)`
         对应明细的行（批量导入 / 将来 ERP 同步来的）。标「未关联」，
         逼着人把它们**人工挂接**掉（挂接时会按料号补上行号，见 link_shipment）。

    ⚠️ 改版前这里**只查 delivery_shipment** —— 于是「发货记录」必须等人在
       ② 待发货清单点「标记已发货」才产生，发货跟踪永远是滞后的。
       现在两件事分开了：**列表的出现时机**提前到申请提交；
       **发货记录的写入时机**是发货员在 ERP 做完单子回来登记
       （② 待发货清单从此**不写任何数据**，只是清点用的只读视图）。

    ★ 用**应用层合并**而不是 SQL JOIN：两侧都可能只在一边出现（申请了没发的、
      发了没申请的），SQLite 没有 FULL OUTER JOIN，用 LEFT JOIN 串一定漏
      （同 ④ 台账的做法）。

    ★ 列表默认顺序 = **待发货在前**（按期望发货日升序，最急的在前），
      已发货与未关联在后（按发货日倒序，最新的在前）。清单要回答的是
      「还差哪些没发」，所以待发货必须第一时间看见。

    筛选：keyword、state（pending/shipped/unlinked）、source、
    厂家 / 风场 / 型号 / 申请单号（精确）、日期区间（按 track_date：
    已发用发货日、待发用期望发货日）、only_return / only_pending / only_unlinked。
    汇总随筛选走（在分页之前算）。
    """
    filters = {k: v for k, v in (filters or {}).items()
               if v not in (None, "", 0, False)}
    conn = get_conn()

    # ---- ① 申请明细 + 表头（全量取，合并与筛选都在内存里做）----
    req_rows = conn.execute(
        """SELECT i.request_no, i.line_no, i.material_no, i.product_model,
                  i.product_name, i.spec, i.qty AS apply_qty,
                  COALESCE(i.need_return, 0) AS need_return,
                  r.turbine_vendor, r.project_site, r.expect_ship_date,
                  r.applicant, r.apply_date, r.status AS req_status
           FROM delivery_db.delivery_request_item i
           JOIN delivery_db.delivery_request r ON r.request_no = i.request_no;"""
    ).fetchall()

    # ---- ② 发货记录 ----
    ship_rows = conn.execute(
        f"SELECT {_SHIP_LIST_COLS} FROM delivery_db.delivery_shipment;").fetchall()

    # ---- ③ 按 (申请单号, 行号) 索引发货记录 ----
    # 同一明细行理论上只有一条记录（写入端「一行只登记一次」挡住重复），
    # 这里取 id 最大的那条只是兜底。
    ship_by_line = {}
    for s in ship_rows:
        no, ln = str(s["request_no"] or "").strip(), s["line_no"]
        if no and ln is not None:
            key = (no, int(ln))
            if key not in ship_by_line or (s["id"] or 0) > (ship_by_line[key]["id"] or 0):
                ship_by_line[key] = dict(s)
    detail_keys = {(str(r["request_no"]), int(r["line_no"] or 0)) for r in req_rows}

    # ---- ④ 组装两类行 ----
    rows = []
    for r in req_rows:
        d = dict(r)
        no, ln = str(d["request_no"]), int(d["line_no"] or 0)
        s = ship_by_line.get((no, ln))
        row = _track_row(
            "shipped" if s else "pending",
            kind="request", request_no=no, line_no=ln,
            turbine_vendor=d.get("turbine_vendor") or "",
            project_site=d.get("project_site") or "",
            product_model=d.get("product_model") or "",
            material_no=d.get("material_no") or "",
            product_name=d.get("product_name") or "",
            spec=d.get("spec") or "",
            apply_qty=int(d.get("apply_qty") or 0),
            need_return=1 if d.get("need_return") else 0,
            expect_ship_date=d.get("expect_ship_date") or "",
            applicant=d.get("applicant") or "",
            apply_date=d.get("apply_date") or "",
            req_status=d.get("req_status") or "",
            req_status_label=DELIVERY_STATUS.get(d.get("req_status"),
                                                 d.get("req_status") or ""),
        )
        if s:
            row.update({
                "id": s["id"], "ship_id": s["id"],
                "ship_no": s.get("ship_no") or "",
                "ship_date": s.get("ship_date") or "",
                "express_no": s.get("express_no") or "",
                "carrier": s.get("carrier") or "",
                "ship_qty": int(s.get("qty") or 0),
                "source": s.get("source") or "",
                "remark": s.get("remark") or "",
                "sync_at": s.get("sync_at") or "",
                "operator": s.get("operator") or "",
                "created_at": s.get("created_at") or "",
            })
            row["qty"] = row["ship_qty"]          # 已发货：以实发为准
            row["track_date"] = row["ship_date"] or row["expect_ship_date"]
            # ★ source 是**这里**才 update 进去的，而 _track_row() 里算 source_label
            #   时还看不到它 —— 必须补算一次，否则「来源」列永远是空白
            #   （2026-09-21 自检抓到：来源标为「按申请单登记」这条直接 FAIL）。
            row["source_label"] = DELIVERY_SHIP_SOURCES.get(row["source"],
                                                            row["source"])
        else:
            row["qty"] = row["apply_qty"]         # 待发货：显示申请件数
        rows.append(row)

    for s in ship_rows:
        no, ln = str(s["request_no"] or "").strip(), s["line_no"]
        if no and ln is not None and (no, int(ln)) in detail_keys:
            continue                              # 已并到申请明细行上了
        d = dict(s)
        rows.append(_track_row(
            "unlinked", kind="standalone",
            request_no=no, line_no=(int(ln) if ln is not None else None),
            turbine_vendor=d.get("turbine_vendor") or "",
            project_site=d.get("project_site") or "",
            product_model=d.get("product_model") or "",
            material_no=d.get("material_no") or "",
            need_return=1 if d.get("need_return") else 0,
            id=d.get("id"), ship_id=d.get("id"),
            ship_no=d.get("ship_no") or "", ship_date=d.get("ship_date") or "",
            express_no=d.get("express_no") or "", carrier=d.get("carrier") or "",
            qty=int(d.get("qty") or 0), ship_qty=int(d.get("qty") or 0),
            source=d.get("source") or "", remark=d.get("remark") or "",
            sync_at=d.get("sync_at") or "", operator=d.get("operator") or "",
            created_at=d.get("created_at") or "",
        ))

    # ---- ⑤ 筛选 ----
    keyword = str(filters.get("keyword") or "").strip().lower()
    if keyword:
        keys = ("ship_no", "express_no", "request_no", "product_model",
                "material_no", "product_name", "turbine_vendor", "project_site",
                "carrier", "applicant")
        rows = [r for r in rows
                if keyword in " ".join(str(r.get(k) or "") for k in keys).lower()]

    state = str(filters.get("state") or "").strip()
    if state in DELIVERY_TRACK_STATES:
        rows = [r for r in rows if r["state"] == state]
    if filters.get("only_pending"):
        rows = [r for r in rows if r["state"] == "pending"]
    if filters.get("only_unlinked"):
        rows = [r for r in rows if r["state"] == "unlinked"]
    if filters.get("only_return"):
        rows = [r for r in rows if r["need_return"]]
    if filters.get("source"):
        rows = [r for r in rows if r["source"] == str(filters["source"]).strip()]

    for name in ("turbine_vendor", "project_site", "product_model", "request_no"):
        value = str(filters.get(name) or "").strip()
        if value:
            rows = [r for r in rows if str(r.get(name) or "") == value]

    # 日期区间按 track_date。待发货行的 track_date 可能为空（申请单没填期望发货日）
    # —— 指定了区间就排除它：没有日期可比，留着会变成"筛不掉也说不清"的行。
    d_from = str(filters.get("date_from") or "").strip()
    d_to = str(filters.get("date_to") or "").strip()
    if d_from:
        rows = [r for r in rows if r["track_date"] and r["track_date"] >= d_from]
    if d_to:
        rows = [r for r in rows if r["track_date"] and r["track_date"] <= d_to]

    # ---- ⑥ 汇总（筛选后、分页前）----
    summary = {
        "pending_lines": sum(1 for r in rows if r["state"] == "pending"),
        "shipped_lines": sum(1 for r in rows if r["state"] == "shipped"),
        "unlinked": sum(1 for r in rows if r["state"] == "unlinked"),
        "pending_qty": sum(r["apply_qty"] for r in rows if r["state"] == "pending"),
        "shipped_qty": sum(r["ship_qty"] for r in rows if r["state"] == "shipped"),
        "return_qty": sum(r["ship_qty"] for r in rows
                          if r["state"] == "shipped" and r["need_return"]),
        "orders": len({r["request_no"] for r in rows if r["request_no"]}),
    }

    # ---- ⑦ 排序 ----
    sortable = {"id", "ship_date", "track_date", "created_at", "ship_no",
                "qty", "request_no", "line_no", "product_model"}
    if sort_by in sortable:
        rev = str(sort_dir).lower() == "desc"
        rows.sort(key=lambda r: str(r.get(sort_by) if r.get(sort_by) is not None
                                    else ""), reverse=rev)
    else:
        # 三趟**稳定**排序：从最次要的键排到最重要的键。
        rows.sort(key=lambda r: str(r.get("ship_date") or ""), reverse=True)
        rows.sort(key=lambda r: str(r.get("expect_ship_date") or "9999-99-99"))
        rows.sort(key=lambda r: _STATE_ORDER.get(r["state"], 9))

    # ---- ⑧ 分页 ----
    total = len(rows)
    page = max(1, int(page or 1))
    page_size = min(max(1, int(page_size or 50)), 500)
    start = (page - 1) * page_size
    return {"rows": rows[start:start + page_size], "total": total,
            "page": page, "page_size": page_size, "summary": summary}


def _sync_request_status(conn, request_no: str, now: str) -> str:
    """按明细的登记情况刷新申请单状态，返回新状态。

    ★ **「已发货」是推论，不是独立的事实**（2026-09-21 改版）：
      * 全部明细行都已登记发货 → `shipped`
      * 还有没登记的（含一条都没登记）→ `submitted`（继续留在待发货清单里）

    ⚠️ 改版前「已发货」是人在待发货清单里点出来的，一个**独立动作**。
      那样会出现分叉：甲点了「标记已发货」，乙去发货跟踪却看不到任何发货单号。
      现在状态由明细的登记情况**推**出来 —— 人不需要、也不应该手工改它。
    """
    stat = conn.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(CASE WHEN EXISTS (
                        SELECT 1 FROM delivery_db.delivery_shipment s
                         WHERE s.request_no = i.request_no
                           AND s.line_no = i.line_no) THEN 1 ELSE 0 END), 0)
                    AS done
             FROM delivery_db.delivery_request_item i
            WHERE i.request_no = ?;""", (request_no,)).fetchone()
    total, done = int(stat["total"] or 0), int(stat["done"] or 0)
    state = (DELIVERY_STATUS_SHIPPED
             if total and done >= total else DELIVERY_STATUS_SUBMIT)
    # 只在状态真的变了时才写 —— 否则每登记一行都会刷新 updated_at，
    # 「最后修改时间」就不再反映"这张单最后一次被人改动"。
    conn.execute(
        "UPDATE delivery_db.delivery_request SET status = ?, updated_at = ? "
        "WHERE request_no = ? AND status <> ?;", (state, now, request_no, state))
    return state


def register_shipment(payload: dict, operator: str = "") -> dict:
    """对申请单的**某一行明细**登记发货（③ 发货跟踪页的主动作）。

    ★ **发货单号必填** —— 台账上每一笔都要能追回 ERP 单号；它同时也是批量导入
      那条记录的幂等键的一半「(发货单号 + 料号)」。没有单号的「已发货」
      在对账时是查不下去的（2026-09-21 用户确认）。
    ★ **一行只登记一次**：已经登记过的行**不覆盖**，让用户走「修改」或「撤销登记」
      —— 否则误点一下就把单号换了，而对账时看不出换过。
    ★ 厂家 / 风场 / 型号 / 需返回**从申请单带出**，不接受前端传入：
      台账按前三项核销，必须和申请单完全一致，否则登记完核不上。
    ★ 登记后申请单状态**自动跟着走**（`_sync_request_status`）：整单明细都登记完
      就自动变「已发货」，不需要人再点一次「标记已发货」。
    """
    request_no = str((payload or {}).get("request_no") or "").strip()
    if not request_no:
        raise ValueError("请指定申请单号。")
    try:
        line_no = int((payload or {}).get("line_no"))
    except (TypeError, ValueError):
        raise ValueError("请指定要登记的产品行（行号）。")

    ship_no = str((payload or {}).get("ship_no") or "").strip()
    if not ship_no:
        raise ValueError("发货单号必填 —— 台账要能追回 ERP 单号。")

    ship_date = str((payload or {}).get("ship_date") or "").strip() or _today()
    if not _DATE_RE.match(ship_date):
        raise ValueError("发货日期格式应为 YYYY-MM-DD。")

    src = str((payload or {}).get("source")
              or DELIVERY_SHIP_SOURCE_DEFAULT).strip()
    if src not in DELIVERY_SHIP_SOURCES:
        raise ValueError(f"未知的发货来源：{src}")

    now = _now()
    with tx() as conn:
        head = conn.execute(
            "SELECT turbine_vendor, project_site FROM "
            "delivery_db.delivery_request WHERE request_no = ?;",
            (request_no,)).fetchone()
        if not head:
            raise ValueError(f"申请单不存在：{request_no}")
        item = conn.execute(
            """SELECT material_no, product_model, qty, need_return
                 FROM delivery_db.delivery_request_item
                WHERE request_no = ? AND line_no = ?;""",
            (request_no, line_no)).fetchone()
        if not item:
            raise ValueError(f"申请单 {request_no} 没有第 {line_no} 行。")

        dup = conn.execute(
            """SELECT id, ship_no FROM delivery_db.delivery_shipment
                WHERE request_no = ? AND line_no = ? LIMIT 1;""",
            (request_no, line_no)).fetchone()
        if dup:
            raise ValueError(
                f"第 {line_no} 行已经登记过发货（发货单号 "
                f"{dup['ship_no'] or '—'}）—— 要改请用「修改」，"
                f"要退回请用「撤销登记」。")

        # 实发件数默认取申请件数；少发时人工填（补发是另一条记录）。
        # ⚠️ 必须**显式区分「没传」和「传了 0」** —— 写成
        #   `int(payload.get("qty") or item["qty"])` 的话，0 是 falsy，
        #   会被当成"没传"而静默套用申请件数，于是「实发 0 件」也能登记成功
        #   （2026-09-21 自检实测抓到：件数 0 期望 400、实际 200 并写进了库）。
        raw_qty = (payload or {}).get("qty")
        if raw_qty in (None, ""):
            qty = int(item["qty"] or 0)
        else:
            try:
                qty = int(raw_qty)
            except (TypeError, ValueError):
                raise ValueError("发货件数应为整数。")
        if qty < 1:
            raise ValueError("发货件数至少为 1。")

        cur = conn.execute(
            """INSERT INTO delivery_db.delivery_shipment
               (turbine_vendor, project_site, request_no, ship_no, ship_date,
                express_no, carrier, qty, line_no, material_no, product_model,
                need_return, source, remark, sync_at, operator,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);""",
            (head["turbine_vendor"] or "", head["project_site"] or "",
             request_no, ship_no, ship_date,
             str((payload or {}).get("express_no") or "").strip(),
             str((payload or {}).get("carrier") or "").strip(),
             qty, line_no, item["material_no"] or "", item["product_model"] or "",
             1 if item["need_return"] else 0, src,
             str((payload or {}).get("remark") or "").strip(),
             now, operator, now, now))
        new_id = cur.lastrowid
        state = _sync_request_status(conn, request_no, now)
        _log(conn, "shipment_register", request_no,
             {"line_no": line_no, "ship_no": ship_no, "ship_date": ship_date,
              "qty": qty}, operator, now)
    return {"id": new_id, "request_no": request_no, "line_no": line_no,
            "ship_no": ship_no, "ship_date": ship_date, "qty": qty,
            "request_status": state}


def update_shipment(ship_id: int, payload: dict, operator: str = "") -> dict:
    """修改一条已登记发货记录的发货信息（单号 / 日期 / 物流 / 件数 / 备注）。

    ★ **不允许改归属**（申请单号与行号）：要换到别的行，必须**撤销后重新登记**
      —— 否则改归属会静默改变台账的核销结果（件数从 A 维度挪到 B 维度），
      对账时看不出发生过什么。改单号、改日期这类「同一件事的修正」才可就地改。
    ★ 发货单号仍**必填** —— 与登记同一条底线。
    """
    ship_id = int(ship_id)
    ship_no = str((payload or {}).get("ship_no") or "").strip()
    if not ship_no:
        raise ValueError("发货单号必填 —— 台账要能追回 ERP 单号。")
    ship_date = str((payload or {}).get("ship_date") or "").strip() or _today()
    if not _DATE_RE.match(ship_date):
        raise ValueError("发货日期格式应为 YYYY-MM-DD。")
    try:
        qty = int((payload or {}).get("qty") or 0)
    except (TypeError, ValueError):
        raise ValueError("发货件数应为整数。")
    if qty < 1:
        raise ValueError("发货件数至少为 1。")

    now = _now()
    with tx() as conn:
        row = conn.execute(
            "SELECT request_no FROM delivery_db.delivery_shipment WHERE id = ?;",
            (ship_id,)).fetchone()
        if not row:
            raise ValueError(f"发货记录不存在：{ship_id}")
        conn.execute(
            """UPDATE delivery_db.delivery_shipment
                  SET ship_no = ?, ship_date = ?, express_no = ?, carrier = ?,
                      qty = ?, remark = ?, updated_at = ?
                WHERE id = ?;""",
            (ship_no, ship_date,
             str((payload or {}).get("express_no") or "").strip(),
             str((payload or {}).get("carrier") or "").strip(),
             qty, str((payload or {}).get("remark") or "").strip(),
             now, ship_id))
        # sqlite3.Row 没有 .get()，用下标取
        no = str(row["request_no"] or "").strip()
        state = _sync_request_status(conn, no, now) if no else ""
        _log(conn, "shipment_update", no,
             {"ship_id": ship_id, "ship_no": ship_no, "qty": qty}, operator, now)
    return {"id": ship_id, "ship_no": ship_no, "ship_date": ship_date,
            "qty": qty, "request_status": state}


def import_shipments(rows_in: list, operator: str = "", source: str = "") -> dict:
    """批量导入发货记录（Excel 多行粘贴 / 将来的 ERP 同步都走这里）。

    ★ **幂等**：`(发货单号 + 料号)` 相同即视为同一条，只更新数量与日期，不新增 ——
      对端重复推送不会让台账虚增（大纲 5.2 ③）。**单号为空时不判重**（无法确定是不是
      同一条，宁可新增也不静默合并）。
    ★ 逐行处理，**一行失败不影响其余**，失败原因随行返回（不静默跳过）。
    """
    # 导入的默认来源是 "import"（**不是** DELIVERY_SHIP_SOURCE_DEFAULT ——
    # 那个是「按申请单登记」，用来标记导入的记录会让人误以为是逐行登记的）。
    src = str(source or "import").strip()
    if src not in DELIVERY_SHIP_SOURCES:
        raise ValueError(f"未知的发货来源：{src}")

    created, updated, failed = 0, 0, []
    now = _now()
    for i, raw in enumerate(rows_in or []):
        try:
            payload = dict(raw or {})
            payload.setdefault("source", src)
            d = _clean_shipment(payload)
        except (ValueError, AttributeError) as exc:
            failed.append({"row": i + 1, "reason": str(exc)})
            continue

        with tx() as conn:
            # ★ 幂等键 = **（发货单号 + 料号）**，刻意**不含申请单号**。
            #   同一个物理发货，手工出队写进去的记录带着申请单号、ERP 导入的没有
            #   —— 若把申请单号也算进键里，两者会被判成两条，台账凭空多出一倍
            #   （2026-09-21 自检实测到：40 件变 80 件）。
            # ★ 发货单号为空时**不判重** —— 没有单号就无法确定是不是同一条，
            #   宁可新增让用户自己发现，也不要静默把两条不同的记录合并成一条。
            old = None
            if d["ship_no"]:
                old = conn.execute(
                    """SELECT id FROM delivery_db.delivery_shipment
                       WHERE ship_no = ? AND COALESCE(material_no, '') = ?
                       ORDER BY id LIMIT 1;""",
                    (d["ship_no"], d["material_no"])).fetchone()
            if old:
                conn.execute(
                    """UPDATE delivery_db.delivery_shipment
                       SET ship_date = ?, qty = ?, express_no = ?, carrier = ?,
                           product_model = ?, turbine_vendor = ?, project_site = ?,
                           need_return = ?, sync_at = ?, updated_at = ?
                       WHERE id = ?;""",
                    (d["ship_date"], d["qty"], d["express_no"], d["carrier"],
                     d["product_model"], d["turbine_vendor"], d["project_site"],
                     d["need_return"], now, now, old["id"]))
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO delivery_db.delivery_shipment
                       (turbine_vendor, project_site, request_no, ship_no,
                        ship_date, express_no, carrier, qty, line_no, material_no,
                        product_model, need_return, source, remark, sync_at,
                        operator, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);""",
                    (d["turbine_vendor"], d["project_site"], d["request_no"],
                     d["ship_no"], d["ship_date"], d["express_no"], d["carrier"],
                     d["qty"], d["line_no"], d["material_no"], d["product_model"],
                     d["need_return"], d["source"], d["remark"], now, operator,
                     now, now))
                created += 1
        if i == 0:
            with tx() as conn:
                _log(conn, "shipment_import", "", {"source": src}, operator, now)

    return {"created": created, "updated": updated, "failed": failed,
            "count": created + updated}


def link_shipment(ship_ids, request_no: str, operator: str = "") -> dict:
    """把若干发货记录**人工挂接**到一张申请单。

    为什么要有这个动作：ERP 同步来的记录往往只有发货单号与料号，对不上申请单号。
    这时候**不猜**（大纲 5.2 ③ 明说），列出来让人手工挂接，并留痕。
    挂接时顺带把厂家 / 风场从申请单带过来（保证核销维度一致）。
    """
    if isinstance(ship_ids, (str, int)):
        ship_ids = [ship_ids]
    ids = [int(x) for x in (ship_ids or []) if str(x).strip()]
    if not ids:
        raise ValueError("请先选择要挂接的发货记录。")
    request_no = str(request_no or "").strip()
    if not request_no:
        raise ValueError("请选择要挂接到的申请单。")

    now = _now()
    linked = 0
    with tx() as conn:
        head = conn.execute(
            "SELECT turbine_vendor, project_site FROM "
            "delivery_db.delivery_request WHERE request_no = ?;",
            (request_no,)).fetchone()
        if not head:
            raise ValueError(f"申请单不存在：{request_no}")
        for sid in ids:
            row = conn.execute(
                "SELECT material_no FROM delivery_db.delivery_shipment "
                "WHERE id = ?;", (sid,)).fetchone()
            if not row:
                continue
            # ★ 挂接时**按料号补上行号**（2026-09-21 改版）：
            #   明细行与发货记录的关联键是 (申请单号, 行号)，只挂单号不补行号，
            #   这条记录在发货跟踪里**仍然显示「未关联」** —— 用户会觉得
            #   "我明明挂了啊"。同一张申请单内料号唯一，按料号找行是**确定性**
            #   的匹配，不是猜（「不猜」指的是不去猜它属于哪张申请单）。
            mat = str(row["material_no"] or "").strip()
            line, need_ret = None, None
            if mat:
                it = conn.execute(
                    """SELECT line_no, need_return
                         FROM delivery_db.delivery_request_item
                        WHERE request_no = ? AND material_no = ?
                        ORDER BY line_no LIMIT 1;""",
                    (request_no, mat)).fetchone()
                if it:
                    line = it["line_no"]
                    # ★ **need_return 必须以申请明细为准**（2026-09-21 补）：
                    #   「要不要回收旧件」是申请单上的属性，而导入的记录只能带一个
                    #   批量默认值。不同步的话，一条"不返回"的明细被挂上需要返回的
                    #   记录后，就会凭空出现在台账里（自检实测：台账发货从 40 变 46）。
                    need_ret = 1 if it["need_return"] else 0
            if line is not None:
                # 该行已经有别的发货记录 —— **不抢**，留空让它继续显示「未关联」，
                # 由人自己去比对哪条才对。绝不覆盖已经登记好的那一行。
                busy = conn.execute(
                    """SELECT id FROM delivery_db.delivery_shipment
                        WHERE request_no = ? AND line_no = ? AND id <> ?
                        LIMIT 1;""", (request_no, line, sid)).fetchone()
                if busy:
                    line = None
            conn.execute(
                """UPDATE delivery_db.delivery_shipment
                      SET request_no = ?, turbine_vendor = ?, project_site = ?,
                          line_no = ?, need_return = COALESCE(?, need_return),
                          updated_at = ?
                    WHERE id = ?;""",
                (request_no, head["turbine_vendor"], head["project_site"],
                 line, need_ret, now, sid))
            linked += 1
        _log(conn, "shipment_link", request_no,
             {"shipment_ids": ids, "count": linked}, operator, now)
    return {"linked": linked, "request_no": request_no}


def delete_shipment(ship_id: int, operator: str = "") -> int:
    """删除一条发货记录（= 「撤销登记」）。

    ★ 删除后**必须回头刷新申请单状态**：这条记录可能正是让某张单变「已发货」的
      那一条，删掉它，单子要退回「已提交」（重新出现在待发货清单里）。
      漏了这一步就会出现「明明撤销了、单子还显示已发货」——
      而人要等到下次去看待发货清单才发现少了一张单。
    """
    now = _now()
    with tx() as conn:
        row = conn.execute(
            "SELECT request_no FROM delivery_db.delivery_shipment WHERE id = ?;",
            (int(ship_id),)).fetchone()
        cur = conn.execute(
            "DELETE FROM delivery_db.delivery_shipment WHERE id = ?;", (int(ship_id),))
        if cur.rowcount:
            no = str(row["request_no"] or "").strip() if row else ""
            if no:
                _sync_request_status(conn, no, now)
            _log(conn, "shipment_delete", no, {"ship_id": ship_id}, operator, now)
    return cur.rowcount


# ---------------------------------------------------------------------------
# ④ 核销台账（2026-09-22 改版：**以 ERP 售后发货单为基准**）
#
# 用户口径（2026-09-22 一次定调，改版逐条对应）：
#   ① 台账只显示「发货明细里 doc_type = 售后发货单 且 doc_status = 已核准」的行；
#   ② 是否需要返回，按**发货申请单的明细行**（delivery_request_item.need_return）判定，
#      不是按料号前缀猜；
#   ③ 维度 = 整机厂家 + 项目风场（**不带型号**：同厂家同风场要汇总成一行）；
#   ④ 二级页以**发货单**为基准列示（一行 = 一张售后发货单 + 该单的已核销 / 待核销）；
#   ⑤ 二级页要能看到「已返回但未核销关联」的剩余数量与明细，并就地
#      手动核销关联 / 手工清账（手工清账按钮就放在未关联返件那一行旁边）。
#
# ★ 三侧数据来源与合并方式（为什么在 Python 里合并、不用 SQL JOIN）：
#   发货在 delivery_db.ship_detail（ERP 出货明细镜像）、返件在 returns_db.returns、
#   核销记录在本库 delivery_db.ledger_clear。三边都要"全都有"才完整，
#   用 LEFT JOIN 串会漏掉只在返件侧出现的行，所以各路各查一次、在内存里按维度合并。
# ★ 返件归位靠**别名表 + 风场名评分**（见 `_match_returns`）：ERP 的 customer 是
#   「整机厂家（项目风场）」自由文本，人工登记的返件写法与它不总一致
#   （用户已确认：明阳风电 = 明阳智能）。别名表可在界面上改，存 auth_db.setting。
# ★ 「已返回但未核销关联」= 返件数量 − 已核销数量，**只在已归位的返件上算** ——
#   没归位的返件连维度都不属于，算进去只会让某个厂家凭空多出待核销。
# ★ 需返回的正式依据是发货申请；申请模块还没有数据时回落到「料号前缀 1/2」的
#   **演示口径**，并且把口径原样发给界面（meta.mode），页面必须醒目标注 ——
#   宁可写着"这是演示口径"，也不能让人以为正式台账就是空的（或反之）。
# ---------------------------------------------------------------------------

LEDGER_DOC_TYPE = "售后发货单"
LEDGER_DOC_STATUS = "已核准"
LEDGER_DEMO_PREFIXES = ("1", "2")
LEDGER_NEED_MODES = ("auto", "req", "demo")
LEDGER_ALIAS_KEY = "ledger_alias"

# 内置别名（发货侧写法 → 返件登记侧写法）。方向不限：两边都会查一遍，
# 用户给的对照表怎么顺手怎么写。界面改过之后以数据库里的那份为准（只增不改内置）。
LEDGER_VENDOR_ALIAS_DEFAULT = {
    "明阳风电": "明阳智能",
    "明阳智慧能源集团股份公司": "明阳智能",
    "长沙七维传感技术有限公司": "长沙七维",
    "上海读风者新能源有限公司": "上海读风者",
    "安赛尔": "安赛尔机电",
}
LEDGER_SITE_ALIAS_DEFAULT = {}

LEDGER_SORTABLE = ("pend", "ship_qty", "ret_qty", "clr_qty", "unlink_qty",
                   "docs", "vendor", "site", "last_ship")
_LEDGER_SORT_KEYS = {
    "pend": "pend_qty", "ship_qty": "ship_qty", "ret_qty": "ret_qty",
    "clr_qty": "clr_qty", "unlink_qty": "unlink_qty", "docs": "docs_n",
    "vendor": "turbine_vendor", "site": "project_site", "last_ship": "last_ship",
}
_LEDGER_STATUS_PENDING = "待核销"
_LEDGER_STATUS_PART = "部分核销"
_LEDGER_STATUS_DONE = "已核销"
_LEDGER_STATUS_NOSHIP = "无发货"

_DIM_SEP = "\x1f"
_DIM_RE = re.compile(r"^(?P<v>.*?)\s*[（(]\s*(?P<s>.*?)\s*[）)]\s*$")
_ALIAS_CACHE = {"raw": None, "value": None}

_SHIP_LEDGER_SQL = """
SELECT doc_no, doc_date, material_no, material_name, product_model,
       qty, serial_no, customer
FROM delivery_db.ship_detail
WHERE doc_type = ? AND doc_status = ?
"""
_RET_LEDGER_SQL = """
SELECT id, return_no, return_date, carrier, turbine_vendor, project_site,
       product_model, product_name, spec, material_no, product_code,
       COALESCE(NULLIF(return_qty, 0), 1) AS qty, registrar,
       info_source, feedback_issue
FROM returns_db.returns
WHERE TRIM(COALESCE(turbine_vendor, '')) <> ''
ORDER BY id DESC
"""
_CLR_LEDGER_SQL = """
SELECT id, COALESCE(NULLIF(kind, ''), 'clear') AS kind, turbine_vendor,
       project_site, product_model, qty, reason, return_id, return_no,
       material_no, doc_no, request_no, serial_no, operator, created_at, updated_at
FROM delivery_db.ledger_clear
ORDER BY id DESC
"""

# 台账数据清洗（2026-09-23 用户口径）：**销售端的返件不进台账** —— 台账算的是
# 「售后发货 × 售后返件」，销售那边退回来的货不该占着售后的差额。
# 「销售端」是 returns.info_source（快递归属）的取值，另一取值是「售后端」。
# 反馈现象里写明「销售错发 / 销售返回 / 销售样件」的那批，快递归属往往仍写着
# 「售后端」（实测 294 行销售端 + 57 行只在反馈现象里写销售），那属于业务判断，
# 所以默认只认快递归属；要连反馈现象一起排掉，把 LEDGER_SALES_HINTS 改成 ("销售",)。
LEDGER_AUTO_REASON = "按型号自动核销（发货单产品型号优先）"
LEDGER_SALES_SOURCES = ("销售端",)
LEDGER_SALES_HINTS = ()


def _is_sales_return(row: dict) -> bool:
    """这条返件算不算销售端的（销售端的按口径不显示在台账里）。"""
    if str(row.get("info_source") or "").strip() in LEDGER_SALES_SOURCES:
        return True
    hint = str(row.get("feedback_issue") or "")
    return any(k in hint for k in LEDGER_SALES_HINTS)


# 快照指纹（2026-09-22）：台账的一次全量快照要 1.5 秒（3 414 条返件按厂家
# 归位 + 6 083 行发货分组），而首屏一次要问两遍（options + list），翻页 / 改
# 筛选还各要一遍 —— 全是同一份快照。这里用「行数 + 最大 id + 数量和」这种
# 索引级就能拿到的信号当指纹，指纹没变就直接复用上一次算好的快照。
# 只认这些信号，直接 UPDATE qty 而不动行数/最大 id 的改动看不到；那种写入
# 走 invalidate_ledger_cache() 主动清（本模块的核销 / 别名写入都已挂上）。
_LEDGER_FP_SQL = """
SELECT
  (SELECT COUNT(*) FROM delivery_db.ship_detail) AS sd_n,
  (SELECT COALESCE(MAX(id), 0) FROM delivery_db.ship_detail) AS sd_max,
  (SELECT COUNT(*) FROM returns_db.returns) AS rt_n,
  (SELECT COALESCE(MAX(id), 0) FROM returns_db.returns) AS rt_max,
  (SELECT COALESCE(SUM(return_qty), 0) FROM returns_db.returns) AS rt_qty,
  (SELECT COUNT(*) FROM delivery_db.ledger_clear) AS lc_n,
  (SELECT COALESCE(MAX(id), 0) FROM delivery_db.ledger_clear) AS lc_max,
  (SELECT COALESCE(SUM(qty), 0) FROM delivery_db.ledger_clear) AS lc_qty,
  (SELECT COUNT(*) FROM delivery_db.delivery_shipment) AS ds_n,
  (SELECT COALESCE(MAX(id), 0) FROM delivery_db.delivery_shipment) AS ds_max,
  (SELECT COUNT(*) FROM delivery_db.delivery_request_item) AS di_n,
  (SELECT COALESCE(SUM(COALESCE(need_return, 0)), 0)
     FROM delivery_db.delivery_request_item) AS di_need
"""
# 只留最近一份：首屏那几个请求用同一个 key，够了；不同 key 互相不干扰。
_SNAP_CACHE = {"key": None, "snap": None}


def invalidate_ledger_cache() -> None:
    """核销 / 别名 / 发货明细写入后调一次，下一次查询重算快照。"""
    _SNAP_CACHE["key"] = None
    _SNAP_CACHE["snap"] = None


def _ledger_fingerprint(conn):
    """指纹取不到（表还没建等）就返回 None，此时不缓存，照旧全算。"""
    try:
        row = conn.execute(_LEDGER_FP_SQL).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        alias_raw = _read_setting(LEDGER_ALIAS_KEY)
    except Exception:
        alias_raw = ""
    return tuple(str(v) for v in row.values()) + (alias_raw,)


def _f(value, default=0.0) -> float:
    """数量和件数一律按浮点收（return_qty 是 double，qty 是 int）。"""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_setting(key: str) -> str:
    # ⚠️ `key` 是 MySQL 保留字，反引号不能省（同 core/auth.py:245）
    row = get_conn().execute(
        "SELECT value FROM auth_db.setting WHERE `key` = ?;", (key,)).fetchone()
    return row["value"] if row else ""


def _write_setting(key: str, value: str) -> None:
    """写 setting；值为空时**删行**（= 回落默认），别留一行空串。

    与 core/auth.py:262 的注释同源：「配过、值是空的」和「没配」长得一样的话，
    直接看库的人一定会读错。
    """
    with tx() as conn:
        if value:
            conn.execute(
                "INSERT INTO auth_db.setting(`key`, value, updated_at) "
                "VALUES (?,?,?) ON DUPLICATE KEY UPDATE "
                "value = VALUES(value), updated_at = VALUES(updated_at);",
                (key, str(value), _now()))
        else:
            conn.execute("DELETE FROM auth_db.setting WHERE `key` = ?;", (key,))


def _ledger_norm(text) -> str:
    """归一化到「能比对」的形态：全半角括号统一、去所有空白、去掉结尾的分隔点。

    只用于比对，**不落库** —— 界面显示的仍是 ERP / 登记里的原样文字。
    """
    if text is None:
        return ""
    t = str(text).replace("（", "(").replace("）", ")")
    t = re.sub(r"[\s\u3000]+", "", t)
    return t.strip(".．。")


def _parse_customer(customer):
    """把发货明细的 customer 拆成（整机厂家, 项目风场）。

    ERP 里这一列的约定写法是「厂家（风场）」，但也有只写厂家、或者只写风场的，
    所以拆不出来时整串当厂家、风场留空（不猜）。
    """
    c = str(customer or "").strip()
    m = _DIM_RE.match(c)
    if m:
        return m.group("v").strip(), m.group("s").strip()
    return c, ""


def ledger_alias() -> dict:
    """生效的别名表（内置默认 + 界面改过的覆盖）。"""
    raw = _read_setting(LEDGER_ALIAS_KEY)
    if _ALIAS_CACHE["raw"] == raw and _ALIAS_CACHE["value"] is not None:
        return _ALIAS_CACHE["value"]
    custom_v, custom_s = {}, {}
    if raw:
        try:
            data = json.loads(raw)
            for k, v in (data.get("vendors") or {}).items():
                k, v = str(k or "").strip(), str(v or "").strip()
                if k and v:
                    custom_v[k] = v
            for k, v in (data.get("sites") or {}).items():
                k, v = str(k or "").strip(), str(v or "").strip()
                if k and v:
                    custom_s[k] = v
        except (ValueError, TypeError, AttributeError):
            # 别名写坏了不该让整个台账打不开 —— 退回内置默认，页面上会少几条校准。
            custom_v, custom_s = {}, {}
    value = {
        "vendors": {**LEDGER_VENDOR_ALIAS_DEFAULT, **custom_v},
        "sites": {**LEDGER_SITE_ALIAS_DEFAULT, **custom_s},
        "custom": {"vendors": custom_v, "sites": custom_s},
        "defaults": {"vendors": dict(LEDGER_VENDOR_ALIAS_DEFAULT),
                     "sites": dict(LEDGER_SITE_ALIAS_DEFAULT)},
    }
    _ALIAS_CACHE["raw"], _ALIAS_CACHE["value"] = raw, value
    return value


def set_ledger_alias(payload: dict, operator: str = "") -> dict:
    """整表替换人工别名（内置默认那几条不受影响，只覆盖/新增）。"""
    payload = payload or {}
    data = {}
    for side in ("vendors", "sites"):
        items = payload.get(side) or {}
        cleaned = {}
        if isinstance(items, dict):
            for k, v in items.items():
                k, v = str(k or "").strip(), str(v or "").strip()
                if k and v and k != v:
                    cleaned[k] = v
        data[side] = cleaned
    _write_setting(LEDGER_ALIAS_KEY,
                   json.dumps(data, ensure_ascii=False) if (data["vendors"] or data["sites"]) else "")
    _ALIAS_CACHE["raw"] = _read_setting(LEDGER_ALIAS_KEY)
    _ALIAS_CACHE["value"] = None
    invalidate_ledger_cache()   # 别名改了，归位结果就得重算
    if operator:
        now = _now()
        with tx() as conn:
            _log(conn, "ledger_alias", "", {"vendors": len(data["vendors"]),
                                            "sites": len(data["sites"])}, operator, now)
    return ledger_alias()


def _alias_lookup(table: dict, key) -> str:
    """别名表的查法：方向不限，所以两边都查一遍。"""
    k = str(key or "").strip()
    if not k:
        return ""
    return table.get(key) or table.get(k) or ""


def _vendor_match(v: str, rv: str, alias: dict) -> bool:
    a, b = _ledger_norm(v), _ledger_norm(rv)
    if not a or not b:
        return False
    if a == b:
        return True
    left = _alias_lookup(alias, v)
    if left and _ledger_norm(left) == b:
        return True
    right = _alias_lookup(alias, rv)
    if right and _ledger_norm(right) == a:
        return True
    return len(a) >= 3 and len(b) >= 3 and (a in b or b in a)


def _canon_vendor(v: str, alias: dict, rv_set: set) -> str:
    """把发货侧厂家的各种写法**收敛成同一个维度**（别名表自动校准）。

    用户口径「目前明阳风电就是明阳智能」→ 发货侧写「明阳风电（xx项目）」的行，
    必须和写「明阳智能（xx项目）」的行汇总成同一行，否则台账里会出现两个明阳。

    规则（确定性、可解释）：① 别名表里有的取别名目标；② 目标/原名里若有一个
    是真的「返件侧厂家」写法，优先用它（台账是给返件核销看的，用登记的写法最直观）；
    ③ 都不在返件侧就用别名目标（没别名就是原名）。
    """
    a = _alias_lookup(alias, v) or v
    if a in rv_set:
        return a
    if v in rv_set:
        return v
    return a


def _site_score(site: str, rsite: str, alias: dict) -> int:
    """风场名评分：3 = 同名或别名命中，2 = 互相包含，0 = 不像。"""
    a, b = _ledger_norm(site), _ledger_norm(rsite)
    if not a or not b:
        return 0
    if a == b:
        return 3
    for x, y in ((rsite, a), (site, b)):
        hit = _alias_lookup(alias, x)
        if hit and _ledger_norm(hit) == y:
            return 3
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return 2
    return 0


def _need_index(conn) -> dict:
    """正式口径的需返回索引：{(发货单号, 料号): True}。

    依据是**发货申请单明细行**的 need_return（用户口径：需不需要返回是发货申请时填的）；
    发货记录上的 need_return 只是登记当时的快照，申请明细有值时以申请明细为准
    （原代码 core/repo_delivery.py:1476 只看发货记录，申请没数据时它恒为空）。
    """
    rows = conn.execute(
        """SELECT TRIM(s.ship_no) AS ship_no,
                  TRIM(COALESCE(s.material_no, '')) AS material_no,
                  COALESCE(i.need_return, s.need_return, 0) AS need
           FROM delivery_db.delivery_shipment s
           LEFT JOIN delivery_db.delivery_request_item i
                  ON i.request_no = s.request_no AND i.line_no = s.line_no
           WHERE TRIM(COALESCE(s.ship_no, '')) <> '';""").fetchall()
    by_mat, by_doc, loose = {}, {}, set()
    for r in rows:
        if int(r["need"] or 0) != 1:
            continue
        doc_no = str(r["ship_no"] or "").strip()
        mat = str(r["material_no"] or "").strip()
        by_doc[doc_no] = by_doc.get(doc_no, 0) + 1
        if mat:
            by_mat[(doc_no, mat)] = True
        else:
            # 申请行没填料号：整单都算需返回（否则这单会被静默丢掉，看不出为什么）
            loose.add(doc_no)
    return {"by_mat": by_mat, "by_doc": by_doc, "loose_docs": loose,
            "lines": len(rows), "docs": len(by_doc)}


def _row_is_need(row, mode: str, need: dict) -> bool:
    doc_no = str(row.get("doc_no") or "").strip()
    mat = str(row.get("material_no") or "").strip()
    if mode == "demo":
        return mat[:1] in LEDGER_DEMO_PREFIXES
    if (doc_no, mat) in need["by_mat"]:
        return True
    return doc_no in need["loose_docs"]


def _new_group(vendor: str, site: str) -> dict:
    return {
        "turbine_vendor": vendor, "project_site": site,
        "docs": {}, "docs_n": 0, "ship_lines": 0, "ship_qty": 0.0,
        "ret_lines": 0, "ret_qty": 0.0, "ret_ids": [],
        "clr_link": 0.0, "clr_clear": 0.0, "clr_auto": 0.0, "clr_ship": 0.0,
        "clr_qty": 0.0, "ret_clr_qty": 0.0, "clr_unassigned": 0.0,
        "pend_qty": 0.0, "unlink_qty": 0.0, "status": _LEDGER_STATUS_PENDING,
        "first_ship": "", "last_ship": "", "first_return": "", "last_return": "",
        "materials": set(), "models": set(), "lines": [],
    }


def _finish_group(g: dict) -> dict:
    # 2026-09-23 加两种核销：① 自动核销（kind='auto'）算**返件侧**核销；
    # ② 发货侧清账（kind='ship'/'shipdoc'）只减「待核销」，**不动**「已返回但
    #   未核销关联」—— 它清的是那批发货，跟有没有返件回来是两件事。
    g["ret_clr_qty"] = g["clr_link"] + g["clr_clear"] + g["clr_auto"]
    g["clr_qty"] = g["ret_clr_qty"] + g["clr_ship"]
    g["pend_qty"] = max(0.0, g["ship_qty"] - g["clr_qty"])
    g["unlink_qty"] = max(0.0, g["ret_qty"] - g["ret_clr_qty"])
    g["docs_n"] = len(g["docs"])
    if g["ship_qty"] <= 0:
        g["status"] = _LEDGER_STATUS_NOSHIP
    elif g["pend_qty"] <= 0 and g["clr_qty"] > 0:
        g["status"] = _LEDGER_STATUS_DONE
    elif g["clr_qty"] > 0:
        g["status"] = _LEDGER_STATUS_PART
    else:
        g["status"] = _LEDGER_STATUS_PENDING
    return g


def _group_public(g: dict) -> dict:
    """发给界面的台账行（内部用的行集合 / 明细不进 JSON）。"""
    out = {k: v for k, v in g.items()
           if k not in ("docs", "lines", "materials", "models", "ret_ids")}
    out["docs_n"] = len(g["docs"])
    out["doc_nos"] = list(g["docs"].keys())
    out["materials"] = sorted(x for x in g["materials"] if x)
    out["models"] = sorted(x for x in g["models"] if x)
    return out


def _ledger_snapshot(need_mode: str = "auto", date_from: str = "",
                     date_to: str = "") -> dict:
    """一次算全：分组汇总 + 返件归位 + 核销记录归集。

    2026-09-23：进台账的返件先过一道清洗 —— 销售端返件（快递归属=销售端）
    直接剔除，剔除行数记在 meta.ret_sales_lines / ret_sales_qty 里给页面显示。
    """
    conn = get_conn()
    fp = _ledger_fingerprint(conn)
    ckey = None if fp is None else (
        fp, str(need_mode), str(date_from or ""), str(date_to or ""))
    if ckey is not None and ckey == _SNAP_CACHE["key"]:
        return _SNAP_CACHE["snap"]
    alias_all = ledger_alias()
    va, sa = alias_all["vendors"], alias_all["sites"]
    need = _need_index(conn)
    mode = need_mode if need_mode in ("req", "demo") else (
        "req" if need["by_doc"] else "demo")

    all_rets = [dict(r) for r in conn.execute(_RET_LEDGER_SQL).fetchall()]
    sales_rets = [r for r in all_rets if _is_sales_return(r)]
    rets = [r for r in all_rets if not _is_sales_return(r)]
    sales_lines = len(sales_rets)
    sales_qty = round(sum(_f(r.get("qty")) for r in sales_rets), 1)
    rv_set = {str(r.get("turbine_vendor") or "").strip() for r in rets}

    ship_where, ship_params = "", [LEDGER_DOC_TYPE, LEDGER_DOC_STATUS]
    # 占位符照旧写 `?`（core/dbapi.py:84 的 translate() 会翻成 %s）
    if str(date_from or "").strip():
        ship_where += " AND doc_date >= ?"
        ship_params.append(str(date_from).strip())
    if str(date_to or "").strip():
        ship_where += " AND doc_date <= ?"
        ship_params.append(str(date_to).strip())
    ships = [dict(r) for r in conn.execute(_SHIP_LEDGER_SQL + ship_where,
                                           ship_params).fetchall()]
    ships = [r for r in ships if _row_is_need(r, mode, need)]

    groups, group_keys = {}, []
    for r in ships:
        vendor, site = _parse_customer(r.get("customer"))
        vendor = _canon_vendor(vendor, va, rv_set)
        key = vendor + _DIM_SEP + site
        g = groups.get(key)
        if g is None:
            g = _new_group(vendor, site)
            groups[key] = g
            group_keys.append(key)
        g["ship_lines"] += 1
        g["ship_qty"] += _f(r.get("qty"))
        g["lines"].append(r)
        mat = str(r.get("material_no") or "").strip()
        if mat:
            g["materials"].add(mat)
        model = str(r.get("product_model") or "").strip()
        if model:
            g["models"].add(model)
        date = str(r.get("doc_date") or "").strip()
        if date:
            if not g["first_ship"] or date < g["first_ship"]:
                g["first_ship"] = date
            if date > g["last_ship"]:
                g["last_ship"] = date
        doc_no = str(r.get("doc_no") or "").strip()
        d = g["docs"].get(doc_no)
        if d is None:
            d = {"doc_no": doc_no, "doc_date": date,
                 "customer": str(r.get("customer") or "").strip(),
                 "lines": 0, "qty": 0.0, "clr": 0.0, "pend": 0.0,
                 "status": _LEDGER_STATUS_PENDING}
            g["docs"][doc_no] = d
        d["lines"] += 1
        d["qty"] += _f(r.get("qty"))

    clrs = [dict(r) for r in conn.execute(_CLR_LEDGER_SQL).fetchall()]
    used_by_ret = {}
    for c in clrs:
        rid = int(c.get("return_id") or 0)
        if rid:
            used_by_ret[rid] = used_by_ret.get(rid, 0.0) + _f(c.get("qty"))

    matched, unmatched, ambiguous = _match_returns(groups, group_keys, rets, va, sa)
    ret_group = matched  # {return_id: group_key}

    # 未归属返件 = 归位不上任何维度的返件（它们不在台账的哪一行里）。它们也能
    # 手工清账 / 手动关联（2026-09-23 加），所以这里先把「已核销 / 未核销」算出来：
    # 已经清完的不再占着待处理的位置，单独计数给页面显示。
    for x in list(unmatched) + list(ambiguous):
        rid = int(x["return_id"])
        x["clr_qty"] = round(used_by_ret.get(rid, 0.0), 1)
        x["unlink_qty"] = round(max(0.0, _f(x.get("qty")) - x["clr_qty"]), 1)
    unattr_ids = ({int(x["return_id"]) for x in unmatched}
                  | {int(x["return_id"]) for x in ambiguous})
    settled = [x for x in list(unmatched) + list(ambiguous) if x["unlink_qty"] <= 0]
    settled_ids = {int(x["return_id"]) for x in settled}
    unmatched = [x for x in unmatched if int(x["return_id"]) not in settled_ids]
    ambiguous = [x for x in ambiguous if int(x["return_id"]) not in settled_ids]

    orphan = 0
    unassigned_clr = 0
    for c in clrs:
        qty = _f(c.get("qty"))
        kind = _clear_slot(c.get("kind"))
        key = None
        if c.get("return_id"):
            key = ret_group.get(int(c["return_id"]))
        if key is None:
            key = _find_group_by_dim(groups, c, va, rv_set)
        if key is None:
            # 落在「未归属返件」上的核销记录是人工故意记的，不算孤立（否则这批
            # 永远挂在 orphan_clears 上，看着像 bug）。
            if int(c.get("return_id") or 0) in unattr_ids:
                unassigned_clr += 1
            else:
                orphan += 1
            continue
        g = groups[key]
        g["clr_" + kind] += qty
        doc_no = str(c.get("doc_no") or "").strip()
        if doc_no and doc_no in g["docs"]:
            g["docs"][doc_no]["clr"] += qty
        else:
            g["clr_unassigned"] += qty

    auto_clrs = sum(1 for c in clrs if _clear_slot(c.get("kind")) == "auto")
    ship_clrs = sum(1 for c in clrs if _clear_slot(c.get("kind")) == "ship")

    for key in group_keys:
        g = groups[key]
        _finish_group(g)
        for d in g["docs"].values():
            d["pend"] = max(0.0, d["qty"] - d["clr"])
            if d["qty"] <= 0:
                d["status"] = _LEDGER_STATUS_NOSHIP
            elif d["pend"] <= 0 and d["clr"] > 0:
                d["status"] = _LEDGER_STATUS_DONE
            elif d["clr"] > 0:
                d["status"] = _LEDGER_STATUS_PART
            else:
                d["status"] = _LEDGER_STATUS_PENDING

    snap = {
        "mode": mode, "groups": groups, "order": group_keys, "ships": ships,
        "rets": rets, "ret_group": ret_group, "alias": alias_all,
        "need": need, "unmatched": unmatched, "ambiguous": ambiguous,
        "orphan_clears": orphan,
        "unattr_cleared": len(settled_ids),
        "unassigned_clears": unassigned_clr,
        "auto_clears": auto_clrs,
        "ship_clears": ship_clrs,
        "sales_lines": sales_lines,
        "meta": {
            "need_mode": mode,
            "need_rows": len(need["by_mat"]),
            "need_docs": len(need["by_doc"]),
            "demo_prefixes": list(LEDGER_DEMO_PREFIXES) if mode == "demo" else [],
            "ship_lines": len(ships),
            "ship_docs": len({str(r.get("doc_no") or "") for r in ships}),
            "ret_lines": len(rets),
            "ret_qty": round(sum(_f(r.get("qty")) for r in rets), 1),
            "ret_matched_lines": len(matched),
            "ret_matched_qty": round(sum(_f(r.get("qty")) for r in rets
                                         if int(r.get("id") or 0) in matched), 1),
            "ret_unmatched_lines": len(unmatched),
            "ret_unmatched_qty": round(sum(x["qty"] for x in unmatched), 1),
            "ret_ambiguous_lines": len(ambiguous),
            "ret_ambiguous_qty": round(sum(x["qty"] for x in ambiguous), 1),
            "ret_sales_lines": sales_lines,
            "ret_sales_qty": sales_qty,
            "unattr_cleared_lines": len(settled_ids),
            "unattr_cleared_qty": round(sum(_f(x.get("qty")) for x in settled), 1),
            "unassigned_clears": unassigned_clr,
            "auto_clears": auto_clrs,
            "ship_clears": ship_clrs,
            "sales_sources": list(LEDGER_SALES_SOURCES),
            "sales_hints": list(LEDGER_SALES_HINTS),
            "alias_vendors": len(alias_all["vendors"]),
            "alias_sites": len(alias_all["sites"]),
            "alias_custom_vendors": len(alias_all["custom"]["vendors"]),
            "alias_custom_sites": len(alias_all["custom"]["sites"]),
            "orphan_clears": orphan,
            "no_site_groups": sum(1 for k in group_keys
                                  if not (groups[k]["project_site"] or "").strip()),
            "unmatched": unmatched[:200],
            "ambiguous": ambiguous[:200],
        },
    }
    if ckey is not None:
        # 放进缓存后调用方只读：query_ledger / ledger_detail 都不改这份结构。
        _SNAP_CACHE["key"], _SNAP_CACHE["snap"] = ckey, snap
    return snap

def _match_returns(groups: dict, group_keys: list, rets: list, va: dict, sa: dict):
    """返件归位：先按厂家匹配，再按风场名评分；歧义（多个同分候选）不硬塞。

    返回 (return_id → group_key, 未匹配明细, 有候选但不唯一的明细)。
    """
    matched, unmatched, ambiguous = {}, [], []
    # 先按厂家建桶：原实现对每个返件都遍历全部维度（3 414 条返件 × 1 224 个维度
    # ≈ 418 万次字符串归一化），单次查询要 13 秒；返件侧只有几十个厂家写法，
    # 每个写法算一次「哪些维度厂家对得上」，之后只在这个桶里评分即可。
    # 结果与原实现完全相同：厂家不匹配的维度本来就会被 continue 掉。
    by_vendor, vcache, scache = {}, {}, {}
    for bkey in group_keys:
        by_vendor.setdefault(groups[bkey]["turbine_vendor"], []).append(bkey)
    for r in rets:
        rv, rs = r.get("turbine_vendor"), r.get("project_site")
        hits = vcache.get(rv)
        if hits is None:
            hits = [gv for gv in by_vendor if _vendor_match(gv, rv, va)]
            vcache[rv] = hits
        cands = []
        for gv in hits:
            for key in by_vendor[gv]:
                g = groups[key]
                skey = (g["project_site"], rs)
                score = scache.get(skey)
                if score is None:
                    score = _site_score(g["project_site"], rs, sa)
                    scache[skey] = score
                cands.append((score, key, g))
        best_key, best_score, ties = None, 0, 0
        for score, key, g in cands:
            if score == 0:
                # 返件没填风场时，只有该厂家唯一一个分组才敢归位
                if not _ledger_norm(rs) and len(cands) == 1:
                    score = 1
                else:
                    continue
            if score > best_score:
                best_key, best_score, ties = key, score, 1
            elif score == best_score:
                ties += 1
        rid = int(r.get("id") or 0)
        item = {"return_id": rid, "return_no": r.get("return_no"),
                "return_date": r.get("return_date"),
                "turbine_vendor": rv, "project_site": rs,
                "product_model": r.get("product_model"),
                "product_name": r.get("product_name"),
                "qty": _f(r.get("qty"))}
        # 同分里先挑「更具体」的那个：返件写「盐城大丰三峡H8-2」而台账里同时有
        # 「大丰三峡」与「大丰三峡H8-2」时，长的那个才是真的对得上（都是包含匹配，
        # 分数一样）。只有长度也一样（真的分不出来）才留给用户用别名表裁决。
        if best_key is not None and ties > 1 and best_score >= 2:
            tied = [c for c in cands if c[0] == best_score]
            tied.sort(key=lambda c: -len(_ledger_norm(c[2]["project_site"])))
            if len({len(_ledger_norm(c[2]["project_site"])) for c in tied}) > 1:
                best_key, ties = tied[0][1], 1
        if best_key is not None and ties == 1:
            matched[rid] = best_key
            g = groups[best_key]
            g["ret_lines"] += 1
            g["ret_qty"] += _f(r.get("qty"))
            g["ret_ids"].append(rid)
            date = str(r.get("return_date") or "").strip()
            if date:
                if not g["first_return"] or date < g["first_return"]:
                    g["first_return"] = date
                if date > g["last_return"]:
                    g["last_return"] = date
            continue
        item["group"] = ""
        if best_key:
            g = groups[best_key]
            item["group"] = f"{g['turbine_vendor']} / {g['project_site']}"
        item["ties"] = ties
        item["candidates"] = sorted(
            ({"vendor": g["turbine_vendor"], "site": g["project_site"],
              "score": score} for score, _k, g in cands if score > 0),
            key=lambda x: -x["score"])[:3]
        (ambiguous if cands else unmatched).append(item)
    return matched, unmatched, ambiguous


def _find_group_by_dim(groups: dict, clear_row: dict, va: dict, rv_set: set):
    """老核销记录（没有 return_id）靠存下来的维度名回退找组。"""
    v = _canon_vendor(str(clear_row.get("turbine_vendor") or "").strip(), va, rv_set)
    s = str(clear_row.get("project_site") or "").strip()
    key = v + _DIM_SEP + s
    if key in groups:
        return key
    for k, g in groups.items():
        if g["turbine_vendor"] == v and _ledger_norm(g["project_site"]) == _ledger_norm(s):
            return k
    return None


def _ledger_kw_haystack(g: dict) -> str:
    parts = [g["turbine_vendor"], g["project_site"]]
    parts += list(g["docs"].keys())
    parts += sorted(g["materials"])
    parts += sorted(g["models"])
    return _ledger_norm(" ".join(str(x) for x in parts if x)).lower()


def ledger_options(need_mode: str = "auto") -> dict:
    """台账筛选用的下拉项：整机厂家 / 项目风场（含所属厂家）/ 状态。"""
    snap = _ledger_snapshot(need_mode)
    vendors, sites = set(), {}
    for k in snap["order"]:
        g = snap["groups"][k]
        if g["turbine_vendor"]:
            vendors.add(g["turbine_vendor"])
        sites.setdefault(g["turbine_vendor"], set()).add(g["project_site"])
    return {
        "vendors": sorted(vendors),
        "sites": [{"vendor": v, "site": s}
                  for v in sorted(sites) for s in sorted(sites[v])],
        "status": [_LEDGER_STATUS_PENDING, _LEDGER_STATUS_PART,
                   _LEDGER_STATUS_DONE],
        "need_mode": snap["mode"],
        "meta": snap["meta"],
    }


def query_ledger(filters: dict = None, page: int = 1, page_size: int = 50,
                 sort_by: str = "pend", sort_dir: str = "desc") -> dict:
    """核销台账（一行 = 整机厂家 + 项目风场）。

    `filters` 认：keyword / turbine_vendor / project_site / status /
    only_pending / only_unlink / only_no_site / need_mode / date_from / date_to。
    """
    f = {k: v for k, v in (filters or {}).items() if v not in (None, "", 0)}
    snap = _ledger_snapshot(str(f.get("need_mode") or "auto"),
                            f.get("date_from"), f.get("date_to"))
    groups = [snap["groups"][k] for k in snap["order"]]

    kw = _ledger_norm(f.get("keyword") or "").lower()
    vendor = str(f.get("turbine_vendor") or "").strip()
    site = str(f.get("project_site") or "").strip()
    status = str(f.get("status") or "").strip()
    only_pending = bool(f.get("only_pending"))
    only_unlink = bool(f.get("only_unlink"))
    only_no_site = bool(f.get("only_no_site"))
    rows = []
    for g in groups:
        if vendor and g["turbine_vendor"] != vendor:
            continue
        if site and g["project_site"] != site:
            continue
        if status and g["status"] != status:
            continue
        if only_pending and g["pend_qty"] <= 0:
            continue
        if only_unlink and g["unlink_qty"] <= 0:
            continue
        if only_no_site and (g["project_site"] or "").strip():
            continue
        if kw and kw not in _ledger_kw_haystack(g):
            continue
        rows.append(g)

    sort_key = _LEDGER_SORT_KEYS.get(sort_by or "", "pend_qty")
    reverse = str(sort_dir or "desc").lower() != "asc"
    # 排序键相同时用厂家/风场兜底 —— 翻页顺序必须稳定，否则同一行可能第 1 页
    # 出现一次、第 2 页又出现一次（用户会以为数据重复了）。
    if sort_key in ("turbine_vendor", "project_site", "last_ship"):
        rows.sort(key=lambda g: (str(g[sort_key] or ""), g["turbine_vendor"],
                                 g["project_site"]), reverse=reverse)
    else:
        rows.sort(key=lambda g: (_f(g[sort_key]), g["turbine_vendor"],
                                 g["project_site"]), reverse=reverse)
    total = len(rows)
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(page_size or 50)
    except (TypeError, ValueError):
        page_size = 50
    page_size = max(1, min(500, page_size))
    start = (page - 1) * page_size
    paged = rows[start:start + page_size]

    summary = {
        "rows": total,
        "docs": sum(g["docs_n"] for g in rows),
        "ship_qty": round(sum(g["ship_qty"] for g in rows), 1),
        "ret_qty": round(sum(g["ret_qty"] for g in rows), 1),
        "clr_qty": round(sum(g["clr_qty"] for g in rows), 1),
        "clr_link": round(sum(g["clr_link"] for g in rows), 1),
        "clr_clear": round(sum(g["clr_clear"] for g in rows), 1),
        "clr_auto": round(sum(g["clr_auto"] for g in rows), 1),
        "clr_ship": round(sum(g["clr_ship"] for g in rows), 1),
        "pend_qty": round(sum(g["pend_qty"] for g in rows), 1),
        "unlink_qty": round(sum(g["unlink_qty"] for g in rows), 1),
        "pend_rows": sum(1 for g in rows if g["pend_qty"] > 0),
        "unlink_lines": sum(1 for g in rows if g["unlink_qty"] > 0),
        "no_site_rows": sum(1 for g in rows if not (g["project_site"] or "").strip()),
    }
    return {"rows": [_group_public(g) for g in paged], "total": total,
            "page": page, "page_size": page_size, "summary": summary,
            "meta": snap["meta"]}


def ledger_detail(turbine_vendor: str, project_site: str = "",
                  need_mode: str = "auto") -> dict:
    """一个维度的二级明细：发货单为基准 + 发货行 + 已归位返件 + 核销记录。"""
    v = str(turbine_vendor or "").strip()
    s = str(project_site or "").strip()
    if not v and not s:
        raise ValueError("请指定要查看的「整机厂家 / 项目风场」。")
    snap = _ledger_snapshot(need_mode)
    key = None
    for k, g in snap["groups"].items():
        if g["turbine_vendor"] == v and _ledger_norm(g["project_site"]) == _ledger_norm(s):
            key = k
            break
    if key is None:
        raise ValueError(f"台账里没有「{v or '（厂家为空）'} / {s or '（风场为空）'}」"
                         "这个维度 —— 请先确认发货记录与需返回口径。")
    g = snap["groups"][key]
    clrs = [c for c in (dict(r) for r in get_conn().execute(_CLR_LEDGER_SQL).fetchall())
            if _clear_group_key(c, snap) == key]
    per_ret = {}
    line_ship_clr = {}   # (发货单, 料号, 序列号) → 已清：序列号拆行后的行级清账
    line_ship_mat = {}   # (发货单, 料号) → 已清：没带序列号的记录
    for c in clrs:
        ck = _clear_slot(c.get("kind"))
        if ck == "ship":
            # 发货侧清账：带序列号的挂到「发货单 + 料号 + 序列号」这一行，
            # 不带序列号的挂到「发货单 + 料号」；整单清账（料号为空）只减单的
            # 待核销，不摊到具体行。
            doc_no = str(c.get("doc_no") or "").strip()
            if doc_no:
                mat = str(c.get("material_no") or "").strip()
                ser = str(c.get("serial_no") or "").strip()
                if ser:
                    lk = (doc_no, mat, ser)
                    line_ship_clr[lk] = line_ship_clr.get(lk, 0.0) + _f(c.get("qty"))
                else:
                    mk = (doc_no, mat)
                    line_ship_mat[mk] = line_ship_mat.get(mk, 0.0) + _f(c.get("qty"))
            continue
        rid = int(c.get("return_id") or 0)
        if not rid:
            continue
        slot = per_ret.setdefault(rid, {"link": 0.0, "clear": 0.0, "auto": 0.0})
        slot[ck] += _f(c.get("qty"))

    ret_map = {int(r.get("id") or 0): r for r in snap["rets"]}
    returns = []
    for rid in g["ret_ids"]:
        r = ret_map.get(int(rid))
        if not r:
            continue
        qty = _f(r.get("qty"))
        slot = per_ret.get(int(rid), {"link": 0.0, "clear": 0.0, "auto": 0.0})
        clr = slot["link"] + slot["clear"] + slot["auto"]
        returns.append({
            "id": int(rid), "return_no": r.get("return_no"),
            "return_date": r.get("return_date"),
            "turbine_vendor": r.get("turbine_vendor"),
            "project_site": r.get("project_site"),
            "product_model": r.get("product_model"),
            "product_name": r.get("product_name"),
            "spec": r.get("spec"), "material_no": r.get("material_no"),
            "qty": qty, "carrier": r.get("carrier"),
            "registrar": r.get("registrar"),
            "link_qty": slot["link"], "clear_qty": slot["clear"],
            "auto_qty": slot["auto"],
            "clr_qty": clr, "unlink_qty": max(0.0, qty - clr),
            "status": _LEDGER_STATUS_DONE if qty - clr <= 0 else (
                _LEDGER_STATUS_PART if clr > 0 else _LEDGER_STATUS_PENDING),
        })

    # 用户 2026-09-23 口径：「无法匹配的（还没核销的）排在后面」。
    returns.sort(key=lambda x: (str(x.get("return_date") or ""),
                                int(x.get("id") or 0)), reverse=True)
    returns.sort(key=lambda x: 1 if float(x.get("unlink_qty") or 0) > 0 else 0)

    lines = []
    for r in g["lines"]:
        lines.append({
            "doc_no": r.get("doc_no"), "doc_date": r.get("doc_date"),
            "material_no": r.get("material_no"),
            "material_name": r.get("material_name"),
            "product_model": r.get("product_model"), "qty": _f(r.get("qty")),
            "serial_no": r.get("serial_no"), "customer": r.get("customer"),
        })
    lines.sort(key=lambda x: (str(x["doc_date"] or ""), str(x["doc_no"] or "")),
               reverse=True)
    lines = _split_lines(lines)     # 先拆行，再逐行挂清账（拆出来的行按序列号认）
    for x in lines:
        d_no = str(x.get("doc_no") or "").strip()
        d_mat = str(x.get("material_no") or "").strip()
        if x.get("split"):
            clr = line_ship_clr.get((d_no, d_mat,
                                     str(x.get("serial_no") or "").strip()), 0.0)
        else:
            clr = line_ship_mat.get((d_no, d_mat), 0.0)
        x["clr_qty"] = clr
        x["ship_remain"] = max(0.0, _f(x.get("qty")) - clr)
    docs = sorted(g["docs"].values(),
                  key=lambda d: (str(d.get("doc_date") or ""), str(d.get("doc_no") or "")),
                  reverse=True)
    return {
        "turbine_vendor": g["turbine_vendor"], "project_site": g["project_site"],
        "need_mode": snap["mode"],
        "summary": {
            "docs": g["docs_n"], "ship_lines": g["ship_lines"],
            "ship_qty": round(g["ship_qty"], 1),
            "ret_lines": g["ret_lines"], "ret_qty": round(g["ret_qty"], 1),
            "clr_link": round(g["clr_link"], 1), "clr_clear": round(g["clr_clear"], 1),
            "clr_auto": round(g["clr_auto"], 1), "clr_ship": round(g["clr_ship"], 1),
            "ret_clr_qty": round(g["ret_clr_qty"], 1),
            "clr_qty": round(g["clr_qty"], 1),
            "pend_qty": round(g["pend_qty"], 1),
            "unlink_qty": round(g["unlink_qty"], 1),
            "clr_unassigned": round(g["clr_unassigned"], 1),
            "status": g["status"],
        },
        "docs": docs,
        "lines": lines,
        "returns": returns,
        "clears": clrs,
        "alias": snap["alias"]["custom"],
    }



# ---------------------------------------------------------------------------
# ⑦ 别名表明细（用户口径 2026-09-23：「别名表要能查询明细」）
#
# 别名表本身只是一张「原写法 = 目标写法」的映射，看不出一条别名到底归并了
# 哪些数据；这里按**与台账同一份输入**统计每条别名的命中，并支持下钻到具体
# 单号。统计口径：方向不限的别名表里，一条 (k = v) 命中的是「原始写法 = k
# 或 = v」的行，按**原始写法**分桶再相加 —— 这样 A→B、B→C 这类链式别名
# 也不会被重复计数。输入与 _ledger_snapshot 完全一致：发货侧 = 售后发货单·
# 已核准 且过需返回口径；返件侧 = returns 且已剔销售端。
# ---------------------------------------------------------------------------
LEDGER_ALIAS_ROW_LIMIT = 500


def _alias_bucket(hit: dict, name: str, qty: float, doc: str, dim: str = "") -> None:
    """按原始写法分桶（同一写法多行累加）。"""
    b = hit.get(name)
    if b is None:
        b = {"lines": 0, "qty": 0.0, "docs": set(), "dims": set()}
        hit[name] = b
    b["lines"] += 1
    b["qty"] = round(b["qty"] + qty, 1)
    if doc:
        b["docs"].add(doc)
    if dim:
        b["dims"].add(dim)


def _alias_scan(need_mode: str = "auto") -> dict:
    """扫一遍台账的输入，按原始写法分桶（发货侧厂家 / 风场 + 返件侧厂家 / 风场）。"""
    conn = get_conn()
    need = _need_index(conn)
    mode = need_mode if need_mode in ("req", "demo") else (
        "req" if need["by_doc"] else "demo")
    ships = [dict(r) for r in conn.execute(
        _SHIP_LEDGER_SQL, [LEDGER_DOC_TYPE, LEDGER_DOC_STATUS]).fetchall()]
    ships = [r for r in ships if _row_is_need(r, mode, need)]
    all_rets = [dict(r) for r in conn.execute(_RET_LEDGER_SQL).fetchall()]
    rets = [r for r in all_rets if not _is_sales_return(r)]
    ship_v, ship_s, ret_v, ret_s = {}, {}, {}, {}
    for r in ships:
        vendor, site = _parse_customer(r.get("customer"))
        vendor, site = str(vendor or "").strip(), str(site or "").strip()
        qty = _f(r.get("qty"))
        doc = str(r.get("doc_no") or "").strip()
        dim = vendor + _DIM_SEP + site
        if vendor:
            _alias_bucket(ship_v, vendor, qty, doc, dim)
        if site:
            _alias_bucket(ship_s, site, qty, doc, dim)
    for r in rets:
        vendor = str(r.get("turbine_vendor") or "").strip()
        site = str(r.get("project_site") or "").strip()
        qty = _f(r.get("qty"))
        doc = str(r.get("return_no") or "").strip()
        if vendor:
            _alias_bucket(ret_v, vendor, qty, doc)
        if site:
            _alias_bucket(ret_s, site, qty, doc)
    return {"mode": mode, "ships": ships, "rets": rets,
            "ship_v": ship_v, "ship_s": ship_s, "ret_v": ret_v, "ret_s": ret_s}


def _alias_hit(*buckets) -> dict:
    """把「原写法」「目标写法」两个桶合成一行展示用的命中数。"""
    lines, qty, docs, dims = 0, 0.0, set(), set()
    for b in buckets:
        if not b:
            continue
        lines += b["lines"]
        qty = round(qty + b["qty"], 1)
        docs |= b["docs"]
        dims |= b["dims"]
    return {"lines": lines, "qty": round(qty, 1),
            "docs": len(docs), "dims": len(dims)}


def _alias_pair_rows(table: dict, custom: dict, ship: dict, ret: dict) -> list:
    out = []
    for k, v in (table or {}).items():
        sh = _alias_hit(ship.get(k), ship.get(v))
        rt = _alias_hit(ret.get(k), ret.get(v))
        out.append({"key": k, "to": v,
                    "source": "自定义" if k in (custom or {}) else "内置",
                    "ship": sh, "ret": rt,
                    "used": bool(sh["lines"] or rt["lines"])})
    out.sort(key=lambda x: (-x["ship"]["lines"], -x["ret"]["lines"], x["key"]))
    return out


def ledger_alias_detail(need_mode: str = "auto") -> dict:
    """别名表逐条命中汇总（页面「明细」页签）。"""
    alias_all = ledger_alias()
    scan = _alias_scan(need_mode)
    vendors = _alias_pair_rows(alias_all["vendors"],
                               alias_all["custom"]["vendors"],
                               scan["ship_v"], scan["ret_v"])
    sites = _alias_pair_rows(alias_all["sites"],
                             alias_all["custom"]["sites"],
                             scan["ship_s"], scan["ret_s"])
    pairs = vendors + sites
    return {"vendors": vendors, "sites": sites,
            "meta": {"need_mode": scan["mode"],
                     "pairs": len(pairs),
                     "used": sum(1 for x in pairs if x["used"]),
                     "unused": sum(1 for x in pairs if not x["used"]),
                     "ship_lines": len(scan["ships"]),
                     "ret_lines": len(scan["rets"]),
                     "row_limit": LEDGER_ALIAS_ROW_LIMIT}}


def _alias_side(side: str) -> str:
    s = str(side or "").strip().lower()
    if s in ("vendor", "vendors"):
        return "vendor"
    if s in ("site", "sites"):
        return "site"
    raise ValueError("side 只能是 vendor 或 site")


def ledger_alias_rows(side: str, key: str = "", to: str = "",
                      need_mode: str = "auto") -> dict:
    """某条别名命中的具体单号（发货侧 + 返件侧，每侧最多 LEDGER_ALIAS_ROW_LIMIT 行）。"""
    side = _alias_side(side)
    names = []
    for x in (str(key or "").strip(), str(to or "").strip()):
        if x and x not in names:
            names.append(x)
    if not names:
        raise ValueError("下钻别名明细至少要给一个名字")
    scan = _alias_scan(need_mode)
    ship_rows, ret_rows = [], []
    for r in scan["ships"]:
        vendor, site = _parse_customer(r.get("customer"))
        vendor, site = str(vendor or "").strip(), str(site or "").strip()
        raw = vendor if side == "vendor" else site
        if raw not in names:
            continue
        ship_rows.append({
            "doc_no": str(r.get("doc_no") or "").strip(),
            "doc_date": str(r.get("doc_date") or "").strip(),
            "customer": str(r.get("customer") or "").strip(),
            "vendor": vendor, "site": site,
            "material_no": str(r.get("material_no") or "").strip(),
            "material_name": str(r.get("material_name") or "").strip(),
            "product_model": str(r.get("product_model") or "").strip(),
            "qty": _f(r.get("qty")), "matched": raw})
    for r in scan["rets"]:
        raw = str((r.get("turbine_vendor") if side == "vendor"
                   else r.get("project_site")) or "").strip()
        if raw not in names:
            continue
        ret_rows.append({
            "return_no": str(r.get("return_no") or "").strip(),
            "return_date": str(r.get("return_date") or "").strip(),
            "turbine_vendor": str(r.get("turbine_vendor") or "").strip(),
            "project_site": str(r.get("project_site") or "").strip(),
            "product_model": str(r.get("product_model") or "").strip(),
            "product_name": str(r.get("product_name") or "").strip(),
            "material_no": str(r.get("material_no") or "").strip(),
            "qty": _f(r.get("qty")),
            "info_source": str(r.get("info_source") or "").strip(),
            "matched": raw})
    lim = LEDGER_ALIAS_ROW_LIMIT
    return {"side": side, "key": names[0], "to": names[1] if len(names) > 1 else "",
            "names": names, "need_mode": scan["mode"], "limit": lim,
            "ship": {"total": len(ship_rows), "rows": ship_rows[:lim],
                     "truncated": len(ship_rows) > lim},
            "ret": {"total": len(ret_rows), "rows": ret_rows[:lim],
                    "truncated": len(ret_rows) > lim}}




# ---------------------------------------------------------------------------
# ⑧ 核销升级（2026-09-23 用户口径）
#   ① 按型号自动核销：「优先匹配发货单产品型号，从上到下自动核销，匹配不上的
#      留在后面手动关联」→ kind='auto'，dry_run 先算给页面确认，可撤销。
#   ② 发货侧清账：发货单 / 发货行每行都能人工清账（kind='shipdoc' / 'ship'），
#      **原因必填**；它减的是「待核销」，不动返件侧的「已返回但未核销关联」。
#   ③ 序列号拆行：发货行明细「1 个序列号 = 1 行 × 1 件」，对不上的原样保留。
# ---------------------------------------------------------------------------


def _clear_slot(kind) -> str:
    """核销记录的 kind → 归集字段后缀（link/clear 是老写法，auto/ship 是新的）。"""
    k = str(kind or "").strip().lower()
    if k == "link":
        return "link"
    if k == "auto":
        return "auto"
    if k in ("ship", "shipdoc"):
        return "ship"
    return "clear"


def _model_norm(text) -> str:
    """型号归一化：大小写、空格、常见分隔符都不敏感（只用于比对，不落库）。

    口径与 2026-09-23 的摸底探针一致（空格 / - _ / 斜杠 / 括号都不算差异）。
    """
    s = str(text or "").strip().lower()
    return re.sub(r"[\s\-_/（）()\[\]]+", "", s)


def _model_hit(key: str, pool_keys) -> list:
    """返件型号命中发货侧哪些型号：先完全相等，再互为包含（短的要 ≥4 位，防误配）。"""
    if not key:
        return []
    if key in pool_keys:
        return [key]
    out = []
    for k in pool_keys:
        if len(key) >= 4 and len(k) >= 4 and (k in key or key in k):
            out.append(k)
    return out


_SERIAL_SPLIT = re.compile(r"[,，;；/、|\\\s]+")


def _serial_tokens(text) -> list:
    s = str(text or "").strip()
    if not s:
        return []
    return [t for t in _SERIAL_SPLIT.split(s) if t]


def _split_lines(lines: list) -> list:
    """发货行明细按序列号拆成「1 个序列号 = 1 行 × 1 件」（用户 2026-09-23 口径）。

    只在「序列号个数 == 数量」时拆；无序列号、或者个数跟数量对不上的行**原样保留**
    （「91 个序列号 / 数量 100」这种拆了就对不上账）。拆分只影响展示，件数合计不变。
    """
    out = []
    for r in lines:
        toks = _serial_tokens(r.get("serial_no"))
        q = _f(r.get("qty"))
        if len(toks) < 2 or len(toks) != q:
            out.append(r)
            continue
        for i, t in enumerate(toks):
            row = dict(r)
            row["qty"] = 1.0
            row["serial_no"] = t
            row["split"] = True
            row["split_part"] = i + 1
            row["split_total"] = len(toks)
            out.append(row)
    return out


def _ledger_group_key(snap: dict, v: str, s: str):
    """按维度名（整机厂家 + 项目风场）找组。"""
    v = str(v or "").strip()
    s = str(s or "").strip()
    for k, g in snap["groups"].items():
        if g["turbine_vendor"] == v and _ledger_norm(g["project_site"]) == _ledger_norm(s):
            return k
    return None


def _auto_plan(snap: dict, keys=None) -> dict:
    """算出「哪些返件行可以按型号自动核销」—— 纯计算，不写库。

    用户 2026-09-23 口径：型号规范化后**完全相等** → **互为包含** →
    「该维度发货侧只有一个型号」时**兜底**归它；匹配不上的留在
    「未关联返件」等人手动关联。发货侧按页面顺序（发货日期新 → 旧）
    自上而下占用「型号池」，池子 = 该型号发货件数 − 已核销件数；
    维度总池 = 发货件数 − 已核销件数（防止把待核销核成负数）。
    """
    groups = snap["groups"]
    keys = list(keys) if keys else [k for k in snap["order"]]
    clrs = [dict(r) for r in get_conn().execute(_CLR_LEDGER_SQL).fetchall()]
    used_dim, used_model, used_ret = {}, {}, {}
    for c in clrs:
        key = _clear_group_key(c, snap)
        if key is None:
            continue
        q = _f(c.get("qty"))
        used_dim[key] = used_dim.get(key, 0.0) + q
        mk = _model_norm(c.get("product_model"))
        if mk:
            used_model[(key, mk)] = used_model.get((key, mk), 0.0) + q
        rid = int(c.get("return_id") or 0)
        if rid:
            used_ret[rid] = used_ret.get(rid, 0.0) + q
    ret_map = {int(x.get("id") or 0): x for x in snap["rets"]}
    plan, skipped = [], []
    dims = 0
    for key in keys:
        g = groups.get(key)
        if not g:
            continue
        dims += 1
        entries = []
        for ln in sorted(g["lines"], key=lambda x: (str(x.get("doc_date") or ""),
                                                   str(x.get("doc_no") or "")),
                         reverse=True):
            raw = str(ln.get("product_model") or "").strip()
            mk = _model_norm(raw)
            q = _f(ln.get("qty"))
            if not mk or q <= 0:
                continue
            entries.append({"key": mk, "raw": raw,
                            "doc_no": str(ln.get("doc_no") or "").strip(),
                            "remain": q})
        for e in entries:
            have = used_model.get((key, e["key"]), 0.0)
            if have <= 0:
                continue
            take = min(have, e["remain"])
            e["remain"] -= take
            used_model[(key, e["key"])] = have - take
        dim_remain = max(0.0, g["ship_qty"] - used_dim.get(key, 0.0))
        rets = [ret_map[int(rid)] for rid in g["ret_ids"] if int(rid) in ret_map]
        rets.sort(key=lambda r: (str(r.get("return_date") or ""),
                                 int(r.get("id") or 0)), reverse=True)
        for r in rets:
            rid = int(r.get("id") or 0)
            left = _f(r.get("qty"), 1.0) - used_ret.get(rid, 0.0)
            if left <= 0:
                continue
            mk = _model_norm(r.get("product_model"))
            pool_keys = {e["key"] for e in entries if e["remain"] > 0}
            hits = _model_hit(mk, pool_keys)
            why = ""
            if not hits:
                uniq = {e["key"] for e in entries}
                if len(uniq) == 1:
                    hits = list(uniq)
                else:
                    why = "型号对不上发货侧" if mk else "返件没填型号"
            avail = sum(e["remain"] for e in entries if e["key"] in hits)
            take = float(int(min(left, avail, dim_remain)))
            if take <= 0:
                skipped.append({
                    "return_id": rid, "return_no": r.get("return_no"),
                    "turbine_vendor": g["turbine_vendor"],
                    "project_site": g["project_site"],
                    "product_model": r.get("product_model"), "qty": left,
                    "why": why or "发货侧这个型号已经核销完",
                })
                continue
            doc_no, ship_model, rest = "", "", take
            for e in entries:
                if e["key"] not in hits or e["remain"] <= 0:
                    continue
                if not doc_no:
                    doc_no, ship_model = e["doc_no"], e["raw"]
                cut = min(e["remain"], rest)
                e["remain"] -= cut
                rest -= cut
                if rest <= 0:
                    break
            dim_remain -= take
            used_ret[rid] = used_ret.get(rid, 0.0) + take
            plan.append({
                "turbine_vendor": g["turbine_vendor"], "project_site": g["project_site"],
                "return_id": rid, "return_no": r.get("return_no"),
                "material_no": str(r.get("material_no") or ""),
                "product_model": str(r.get("product_model") or ""),
                "ship_model": ship_model, "doc_no": doc_no, "qty": take,
            })
    return {"plan": plan, "skipped": skipped, "dims": dims}


def auto_link_ledger(payload: dict, operator: str = "") -> dict:
    """按型号自动核销（用户 2026-09-23 拍板：**按钮触发、真写记录、可撤销**）。

    `dry_run=1` 只算不写，页面拿它弹确认框；确认后再真写 kind='auto' 的记录，
    每条都能在「核销明细」页签里撤销。`all=1` 是全台账一次性跑。
    """
    payload = payload or {}
    need_mode = str(payload.get("need_mode") or "auto")
    snap = _ledger_snapshot(need_mode)
    keys = None
    if not payload.get("all"):
        v = str(payload.get("turbine_vendor") or "").strip()
        s = str(payload.get("project_site") or "").strip()
        key = _ledger_group_key(snap, v, s)
        if key is None:
            raise ValueError(f"台账里没有「{v or '（厂家为空）'} / {s or '（风场为空）'}」"
                             "这个维度 —— 请先确认发货记录与需返回口径。")
        keys = [key]
    calc = _auto_plan(snap, keys)
    rows = calc["plan"]
    out = {
        "dims": calc["dims"], "planned": len(rows),
        "qty": round(sum(x["qty"] for x in rows), 1),
        "skipped": len(calc["skipped"]),
        "rows": rows[:200], "skipped_rows": calc["skipped"][:200],
        "dry_run": bool(payload.get("dry_run")), "written": 0,
    }
    if payload.get("dry_run") or not rows:
        return out
    now = _now()
    ids = []
    with tx() as conn:
        for x in rows:
            cur = conn.execute(
                """INSERT INTO delivery_db.ledger_clear
                   (kind, turbine_vendor, project_site, product_model, qty, reason,
                    return_id, return_no, material_no, doc_no, request_no,
                    operator, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?);""",
                ("auto", x["turbine_vendor"], x["project_site"], x["product_model"],
                 int(x["qty"]), LEDGER_AUTO_REASON, int(x["return_id"]),
                 str(x["return_no"] or ""), x["material_no"], x["doc_no"], "",
                 operator, now, now))
            ids.append(cur.lastrowid)
        _log(conn, "ledger_auto", "", {"dims": out["dims"], "lines": len(rows),
                                       "qty": out["qty"]}, operator, now)
    invalidate_ledger_cache()   # 写了核销记录 = 快照立刻作废
    out["written"] = len(ids)
    out["ids"] = ids[:200]
    return out


def _write_ship_clear(payload: dict, operator: str, kind: str) -> dict:
    """发货侧清账：kind='shipdoc' 按发货单 / kind='ship' 按发货行。

    它减的是**待核销**（这批货不用再等返件回来了），不碰返件侧的
    「已返回但未核销关联」—— 两件事，别混。原因必填。
    """
    payload = payload or {}
    v = str(payload.get("turbine_vendor") or "").strip()
    s = str(payload.get("project_site") or "").strip()
    doc_no = str(payload.get("doc_no") or "").strip()
    if not doc_no:
        raise ValueError("请指定要清账的发货单号。")
    serial_no = str(payload.get("serial_no") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if len(reason) < DELIVERY_CLEAR_REASON_MIN:
        raise ValueError(f"清账原因必填（至少 {DELIVERY_CLEAR_REASON_MIN} 个字）—— "
                         "手工清账是把差额抹掉，必须留理由。")
    need_mode = str(payload.get("need_mode") or "auto")
    snap = _ledger_snapshot(need_mode)
    key = _ledger_group_key(snap, v, s)
    if key is None:
        raise ValueError(f"台账里没有「{v or '（厂家为空）'} / {s or '（风场为空）'}」这个维度。")
    g = snap["groups"][key]
    doc_rows = [ln for ln in g["lines"] if str(ln.get("doc_no") or "").strip() == doc_no]
    if not doc_rows:
        raise ValueError(f"这个维度里没有发货单 {doc_no}。")
    clrs = [dict(r) for r in get_conn().execute(_CLR_LEDGER_SQL).fetchall()]
    mine = [c for c in clrs if _clear_group_key(c, snap) == key]
    doc_qty = sum(_f(x.get("qty")) for x in doc_rows)
    doc_used = sum(_f(c.get("qty")) for c in mine
                   if str(c.get("doc_no") or "").strip() == doc_no)
    material_no = ""
    model = ""
    if kind == "shipdoc":
        remain = doc_qty - doc_used
    else:
        material_no = str(payload.get("material_no") or "").strip()
        model = str(payload.get("product_model") or "").strip()
        rows = [x for x in doc_rows if str(x.get("material_no") or "").strip() == material_no]
        if model and rows:
            rows = [x for x in rows if str(x.get("product_model") or "") == model] or rows
        if not rows:
            raise ValueError(f"发货单 {doc_no} 里没有这一行（料号对不上）。")
        if serial_no:
            # 序列号拆行后「一行」= 一个序列号：按序列号精确到件，不牵连同料号的其他行。
            hit = [x for x in rows if str(x.get("serial_no") or "").strip() == serial_no]
            if not hit:
                hit = [x for x in rows
                       if serial_no in _serial_tokens(str(x.get("serial_no") or ""))]
            if not hit:
                raise ValueError(f"发货单 {doc_no} 里没有序列号 {serial_no} 这一行。")
            rows = hit
        if serial_no:
            line_qty = float(len(rows))                            # 一个序列号 = 一件
        else:
            line_qty = sum(_f(x.get("qty")) for x in rows)
        line_used = sum(_f(c.get("qty")) for c in mine
                        if str(c.get("doc_no") or "").strip() == doc_no
                        and str(c.get("material_no") or "").strip() == material_no
                        and (not serial_no
                             or str(c.get("serial_no") or "").strip() == serial_no))
        remain = min(line_qty - line_used, doc_qty - doc_used)
    if remain <= 0:
        what = "这张单" if kind == "shipdoc" else "这一行"
        raise ValueError(f"{what}的待核销已经是 0 —— 要改请先撤销原有的核销记录。")
    if payload.get("qty") in (None, ""):
        qty = remain
    else:
        try:
            qty = float(payload.get("qty"))
        except (TypeError, ValueError):
            raise ValueError("清账数量应为数字。")
        if qty <= 0:
            raise ValueError("清账数量至少为 1。")
        if qty > remain:
            raise ValueError(f"清账数量 {qty:g} 超过这一行的待核销（{remain:g} 件）。")
    if qty != int(qty):
        raise ValueError("清账数量应为整数。")
    qty = int(qty)
    now = _now()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO delivery_db.ledger_clear
               (kind, turbine_vendor, project_site, product_model, qty, reason,
                return_id, return_no, material_no, doc_no, request_no,
                serial_no, operator, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);""",
            (kind, g["turbine_vendor"], g["project_site"], model, qty, reason,
             None, None, material_no, doc_no, "", serial_no, operator, now, now))
        new_id = cur.lastrowid
        _log(conn, "ledger_" + kind, doc_no,
             {"dim": [g["turbine_vendor"], g["project_site"]], "qty": qty,
              "material_no": material_no, "reason": reason}, operator, now)
    invalidate_ledger_cache()
    return {"id": new_id, "kind": kind, "qty": qty, "doc_no": doc_no,
            "material_no": material_no, "serial_no": serial_no,
            "turbine_vendor": g["turbine_vendor"],
            "project_site": g["project_site"], "remain": remain - qty}


def clear_ship_line(payload: dict, operator: str = "") -> dict:
    """发货行清账：这张单的**这一行**不用再等返件了。"""
    return _write_ship_clear(payload, operator, "ship")


def clear_ship_doc(payload: dict, operator: str = "") -> dict:
    """发货单清账：整张单不用再等返件了。"""
    return _write_ship_clear(payload, operator, "shipdoc")


def _clear_group_key(row: dict, snap: dict):
    """一条核销记录属于哪个维度：认 return_id（新写法），老行回退按维度名找。"""
    rid = int(row.get("return_id") or 0)
    if rid and rid in snap["ret_group"]:
        return snap["ret_group"][rid]
    return _find_group_by_dim(
        snap["groups"], row, snap["alias"]["vendors"],
        {str(r.get("turbine_vendor") or "").strip() for r in snap["rets"]})


def _return_row(return_id: int) -> dict:
    row = get_conn().execute(
        """SELECT id, return_no, return_date, turbine_vendor, project_site,
                  product_model, product_name, spec, material_no,
                  COALESCE(NULLIF(return_qty, 0), 1) AS qty
           FROM returns_db.returns WHERE id = ?;""", (int(return_id),)).fetchone()
    if not row:
        raise ValueError(f"返件记录不存在：{return_id}")
    return dict(row)


def _write_clear(payload: dict, operator: str, kind: str) -> dict:
    """写一条核销记录（kind='link' 手动核销关联 / kind='clear' 手工清账）。

    ★ 数量校验按**这条返件自己的剩余**算：已关联 + 已清账 + 本次 ≤ 返件数量。
      否则台账会出现「某条返件被核销 2 次」的负剩余（结算口径就假了）。
    """
    payload = payload or {}
    rid = int(payload.get("return_id") or 0)
    if not rid:
        raise ValueError("请指定要核销的返件记录。")
    row = _return_row(rid)
    qty_total = _f(row.get("qty"), 1.0)
    used = _f(get_conn().execute(
        "SELECT COALESCE(SUM(qty), 0) AS q FROM delivery_db.ledger_clear "
        "WHERE return_id = ?;", (rid,)).fetchone()["q"])
    remain = qty_total - used
    if remain <= 0:
        raise ValueError(f"这条返件已全部核销（返件 {qty_total:g} 件、已核销 {used:g} 件），"
                         "如要改请先取消原有的核销记录。")
    if payload.get("qty") in (None, ""):
        qty = remain
    else:
        try:
            qty = float(payload.get("qty"))
        except (TypeError, ValueError):
            raise ValueError("核销数量应为数字。")
        if qty <= 0:
            raise ValueError("核销数量至少为 1。")
        if qty > remain:
            raise ValueError(f"核销数量 {qty:g} 超过这条返件的剩余（{remain:g} 件）。")
    if qty != int(qty):
        raise ValueError("核销数量应为整数。")
    qty = int(qty)

    reason = str(payload.get("reason") or "").strip()
    if kind == "clear" and len(reason) < DELIVERY_CLEAR_REASON_MIN:
        raise ValueError(f"清账原因必填（至少 {DELIVERY_CLEAR_REASON_MIN} 个字）—— "
                         "手工清账是把差额抹掉，必须留理由。")
    if kind == "link" and not reason:
        reason = "手动核销关联"

    v = str(payload.get("turbine_vendor") or row.get("turbine_vendor") or "").strip()
    s = str(payload.get("project_site") or row.get("project_site") or "").strip()
    model = str(payload.get("product_model") or row.get("product_model") or "").strip()
    doc_no = str(payload.get("doc_no") or "").strip()
    now = _now()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO delivery_db.ledger_clear
               (kind, turbine_vendor, project_site, product_model, qty, reason,
                return_id, return_no, material_no, doc_no, request_no,
                operator, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?);""",
            (kind, v, s, model, qty, reason, rid,
             str(row.get("return_no") or ""), str(row.get("material_no") or ""),
             doc_no, str(payload.get("request_no") or ""), operator, now, now))
        new_id = cur.lastrowid
        _log(conn, "ledger_" + kind, str(row.get("return_no") or ""),
             {"dim": [v, s], "return_id": rid, "qty": qty, "doc_no": doc_no,
              "reason": reason}, operator, now)
    invalidate_ledger_cache()   # 写进核销记录 = 快照立刻作废
    return {"id": new_id, "kind": kind, "qty": qty, "return_id": rid,
            "return_no": row.get("return_no"), "turbine_vendor": v,
            "project_site": s, "product_model": model, "doc_no": doc_no,
            "remain": remain - qty}


def link_ledger_return(payload: dict, operator: str = "") -> dict:
    """手动核销关联：人眼确认这条返件对上了发货 → 记成已核销。"""
    return _write_clear(payload, operator, "link")


def clear_ledger(payload: dict, operator: str = "") -> dict:
    """手工清账（换件不返还 / 现场留用 / 遗失等）。**原因必填**。

    2026-09-22 改版：从「按维度清账」改成「按返件行清账」—— 界面上这个按钮
    就放在「已返回但未核销关联」的那条返件旁边，清的就是那条货。
    旧调用（只给维度 + qty，不给 return_id）已不再支持：那种清账没法回答
    "清的是哪一件"，正是这次要修掉的含糊。
    """
    return _write_clear(payload, operator, "clear")


def list_clears(turbine_vendor: str = "", project_site: str = "",
                product_model: str = "", limit: int = 200) -> list:
    """核销记录（关联 + 清账，可按维度筛）。"""
    conn = get_conn()
    where, params = [], []
    for col, val in (("turbine_vendor", turbine_vendor),
                     ("project_site", project_site),
                     ("product_model", product_model)):
        if str(val or "").strip():
            where.append(f"{col} = ?")
            params.append(str(val).strip())
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM delivery_db.ledger_clear{clause} "
        f"ORDER BY id DESC LIMIT ?;", params + [int(limit)])
    return [dict(r) for r in rows]


def delete_clear(clear_id: int, operator: str = "") -> int:
    """撤销一条核销记录（关联或清账；记错了要能改回来）。

    撤销前整行进回收站 —— 核销结论是这套流程里最不能丢的数据，
    误撤销同样要能还原（还原后快照缓存也一并作废）。
    """
    now = _now()
    with tx() as conn:
        row = conn.execute("SELECT * FROM delivery_db.ledger_clear "
                           "WHERE id = ?;", (int(clear_id),)).fetchone()
        if row:
            recycle.snapshot_clear(conn, [row], operator)
        cur = conn.execute(
            "DELETE FROM delivery_db.ledger_clear WHERE id = ?;", (int(clear_id),))
        if cur.rowcount:
            _log(conn, "ledger_clear_del", (row or {}).get("return_no", "") if row else "",
                 {"clear_id": clear_id, "kind": (row or {}).get("kind", "") if row else ""},
                 operator, now)
    if cur.rowcount:
        invalidate_ledger_cache()   # 撤销核销记录同样让快照作废
    return cur.rowcount

# ---------------------------------------------------------------------------
# 发货明细（ERP 出货明细的本地镜像）
# ---------------------------------------------------------------------------
# 这一组**只碰本地表**，不连 ERP —— 取数与状态翻译在 core/erp_ship.py。
# 分开的理由：取数依赖 pymssql（2026-09-22 起已进 requirements.txt），查询不依赖；
# 混在一起会让「只想查本地明细」的调用也被迫导入 pymssql（它在个别环境装不上）。
#
# ★ 本表唯一的写入入口是 replace_ship_details()，语义是「按日期区间整体替换」。
#   刻意**不做**单行增删改：它是 ERP 的镜像，手工改一行会在下次同步时被覆盖，
#   给使用者「改了但没生效」的错觉 —— 与其留一个会骗人的接口，不如不提供。

_DETAIL_WRITE_FIELDS = tuple(c[0] for c in SHIP_DETAIL_COLUMNS)
_DETAIL_SORTABLE = frozenset(SHIP_DETAIL_SORTABLE)
# 允许做「取值候选」的列（筛选下拉用）。只放这几个：它们基数适中、
# 是使用者真正会拿来收窄范围的口径；doc_no 有 1.9 万种、address 更散，
# 做成下拉只会拖慢页面且没人拉。
_DETAIL_OPTION_FIELDS = ("doc_status", "doc_type", "customer", "product_model")


def _detail_qty(value) -> float:
    """ERP 的出货数量是 decimal，取回来形如 ``2.000000000``。

    存 REAL、显示时按需去尾零；这里只负责在写入前挡住非数值。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clean_detail(row: dict) -> dict:
    """归一化 ERP 取回的一行：只留白名单列。

    **只 strip，不折叠中间的空白** —— 这是镜像表，地址与品名里的空格是原始内容，
    顺手「清洗」会让本地数据与 ERP 对不上，日后对账时无从判断是谁改的。
    """
    out = {}
    for name in _DETAIL_WRITE_FIELDS:
        value = row.get(name)
        if name == "qty":
            out[name] = _detail_qty(value)
        elif value is None:
            out[name] = ""
        else:
            out[name] = str(value).strip()
    return out


def pending_doc_nos(approved: str = "", limit: int = 0) -> list:
    """还没核准的单号清单（增量同步的第二路取数依据）：去重、按单号排序。

    「未核准」= `doc_status <> 已核准` —— 草稿 / 开立 / 核准中 都算。这些单在
    ERP 侧还会被改，所以每次自动同步都要按单号精确重刷一次；只靠日期窗口是
    刷不到它们的（一张三个月前的单今天被核准，日期窗口永远够不着）。

    `limit > 0` 时按单号排序后截断 —— MSSQL 一条语句最多 2100 个参数。
    截断**不静默**：调用方拿到的就是截断后的清单，长度会进日志与状态文件，
    「未核准单多到一趟装不下」这件事必须能看见。
    """
    approved = str(approved or SHIP_DETAIL_DEFAULT_STATUS).strip()
    rows = get_conn().execute(
        "SELECT DISTINCT doc_no FROM delivery_db.ship_detail "
        "WHERE doc_no <> '' AND doc_status <> ? ORDER BY doc_no;",
        (approved,)).fetchall()
    out = [str(r["doc_no"]).strip() for r in rows if str(r["doc_no"]).strip()]
    if limit and int(limit) > 0:
        out = out[:int(limit)]
    return out


def replace_ship_details(rows: list, date_from: str = "", date_to: str = "",
                         sync_at: str = "", doc_nos=None) -> dict:
    """按**删除谓词整体替换**发货明细，返回 {deleted, inserted, ...}。

    删除谓词 = `(doc_date >= ? AND doc_date <= ?) OR doc_no IN (…)`，
    与取数用的谓词**逐字对应**（见 core/erp_ship.py `_build_query`）。
    两处必须一起想：删的行集合与「本来要覆盖写回」的行集合不一致，
    就会留下永远不更新的旧行，或者把没重新取到的行删掉。

    ⚠️ 调用方必须**先把 ERP 数据全部取回内存**再调这里。删除与插入虽然在同一个
    事务里，但若在取数过程中逐批发进来，中途失败仍会留下半截数据。

    幂等性来自**范围替换**而不是唯一键 —— 这张表没有天然唯一键：
    实测 8.9 万行里「单号+料号+序列号+规格+数量+型号」六列组合仍有 2296 组重复，
    ERP 允许同一张单、同一物料拆成内容完全相同的多条记录。
    所以增量同步也必须是「先删后插」，不能改成按业务键 upsert。
    """
    cleaned = [_clean_detail(r) for r in (rows or [])]
    now = _now()
    stamp = str(sync_at or now).strip()
    cols = _DETAIL_WRITE_FIELDS + ("sync_at", "created_at")

    groups, params = [], []
    window = []
    if str(date_from or "").strip():
        window.append("doc_date >= ?")
        params.append(str(date_from).strip())
    if str(date_to or "").strip():
        window.append("doc_date <= ?")
        params.append(str(date_to).strip())
    if window:
        # 与单号清单是「或」关系，括号必须留着（见 core/erp_ship.py 同一处注释）。
        groups.append("(" + " AND ".join(window) + ")")
    nums = [str(n).strip() for n in (doc_nos or []) if str(n).strip()]
    if nums:
        groups.append(f"doc_no IN ({', '.join('?' * len(nums))})")
        params.extend(nums)
    clause = (" WHERE " + " OR ".join(groups)) if groups else ""

    with tx() as conn:
        deleted = conn.execute(
            f"DELETE FROM delivery_db.ship_detail{clause};", params).rowcount
        if cleaned:
            marks = ", ".join("?" for _ in cols)
            conn.executemany(
                f"INSERT INTO delivery_db.ship_detail ({', '.join(cols)}) "
                f"VALUES ({marks});",
                [tuple(r[n] for n in _DETAIL_WRITE_FIELDS) + (stamp, now)
                 for r in cleaned])
    invalidate_ledger_cache()   # ERP 同步换了发货明细 = 台账快照作废
    return {"deleted": max(int(deleted or 0), 0), "inserted": len(cleaned),
            "date_from": str(date_from or "").strip(),
            "date_to": str(date_to or "").strip(), "doc_nos": len(nums),
            "sync_at": stamp}


def _detail_where(filters: dict) -> tuple:
    """把筛选条件拼成 (SQL 片段, 参数)。列表 / 统计 / 导出共用同一口径，
    避免三处各写一遍 WHERE 后慢慢漂移（本项目已经吃过一次跨库口径不一致的亏）。
    """
    raw = dict(filters or {})
    # 「不传 doc_status」= 用默认口径（只看已核准）；
    # 「显式传空串」= 明确要看全部。两者必须区分开 —— 否则首屏会混进
    # 草稿与开立单，把「发了多少」算大。
    if "doc_status" not in raw:
        raw["doc_status"] = SHIP_DETAIL_DEFAULT_STATUS
    filters = {k: v for k, v in raw.items() if v not in (None, "", 0, False)}

    where, params = [], []
    keyword = str(filters.get("keyword") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        cols = " OR ".join(f"{c} LIKE ?" for c in SHIP_DETAIL_SEARCH_FIELDS)
        where.append(f"({cols})")
        params += [like] * len(SHIP_DETAIL_SEARCH_FIELDS)

    for name in ("doc_status", "doc_type", "customer", "material_no",
                 "product_model"):
        value = str(filters.get(name) or "").strip()
        if value:
            where.append(f"{name} = ?")
            params.append(value)

    # 日期区间：doc_date 为空的行不该混进「某段时间的出货」里
    for name, op in (("date_from", ">="), ("date_to", "<=")):
        value = str(filters.get(name) or "").strip()
        if value:
            where.append(f"TRIM(COALESCE(doc_date, '')) <> '' AND doc_date {op} ?")
            params.append(value)

    return ((" WHERE " + " AND ".join(where)) if where else ""), params


def query_ship_details(filters: dict = None, page: int = 1, page_size: int = 0,
                       sort_by: str = "", sort_dir: str = "desc") -> dict:
    """发货明细列表（一行 = ERP 的一条出货明细行），带筛选与分页。"""
    clause, params = _detail_where(filters)
    conn = get_conn()
    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM delivery_db.ship_detail{clause};",
        params).fetchone()["c"]

    page = max(1, int(page or 1))
    size = min(max(1, int(page_size or SHIP_DETAIL_PAGE_SIZE)),
               SHIP_DETAIL_MAX_PAGE_SIZE)
    sort = sort_by if sort_by in _DETAIL_SORTABLE else "doc_date"
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"

    rows = conn.execute(
        f"SELECT * FROM delivery_db.ship_detail{clause} "
        f"ORDER BY {sort} {direction}, id {direction} LIMIT ? OFFSET ?;",
        params + [size, (page - 1) * size]).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total,
            "page": page, "page_size": size, "sort_by": sort,
            "sort_dir": direction.lower()}


def list_ship_details(filters: dict = None, limit: int = 0,
                      sort_by: str = "", sort_dir: str = "desc") -> list:
    """导出用：取全部命中行（不分页），排序口径与列表一致。"""
    clause, params = _detail_where(filters)
    sort = sort_by if sort_by in _DETAIL_SORTABLE else "doc_date"
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    sql = (f"SELECT * FROM delivery_db.ship_detail{clause} "
           f"ORDER BY {sort} {direction}, id {direction}")
    if limit:
        sql += " LIMIT ?"
        params = params + [int(limit)]
    return [dict(r) for r in get_conn().execute(sql, params).fetchall()]


def ship_detail_stats(filters: dict = None) -> dict:
    """页面顶部统计：**全局**各状态的条数 + 当前筛选的合计。

    两者都要：状态切换按钮显示的是全局条数（否则切到「已核准」后
    其它状态的数字会全变成 0，看着像数据没了），当前筛选的合计回答
    「现在这一屏到底是多少条 / 多少件」。
    """
    conn = get_conn()
    overall = conn.execute(
        "SELECT COUNT(*) AS c, MIN(doc_date) AS first_date, "
        "       MAX(doc_date) AS last_date, MAX(sync_at) AS last_sync "
        "FROM delivery_db.ship_detail;").fetchone()
    by_status = {r["doc_status"]: r["c"] for r in conn.execute(
        "SELECT doc_status, COUNT(*) AS c FROM delivery_db.ship_detail "
        "GROUP BY doc_status;")}

    clause, params = _detail_where(filters)
    cur = conn.execute(
        f"SELECT COUNT(*) AS c, COALESCE(SUM(qty), 0) AS qty, "
        f"       COUNT(DISTINCT doc_no) AS docs, "
        f"       COUNT(DISTINCT material_no) AS materials "
        f"FROM delivery_db.ship_detail{clause};", params).fetchone()
    return {
        "total": overall["c"],
        "by_status": by_status,
        "first_date": overall["first_date"] or "",
        "last_date": overall["last_date"] or "",
        "last_sync": overall["last_sync"] or "",
        "filtered": {
            "rows": cur["c"], "qty": cur["qty"] or 0,
            "docs": cur["docs"] or 0, "materials": cur["materials"] or 0,
        },
    }


def ship_detail_options(field: str, keyword: str = "", limit: int = 200) -> list:
    """某个列的取值清单（筛选下拉用）。只放行 _DETAIL_OPTION_FIELDS 里的列。"""
    field = str(field or "").strip()
    if field not in _DETAIL_OPTION_FIELDS:
        raise InvalidField(
            f"发货明细不支持按 {field or '(空)'} 取候选值")
    where = [f"TRIM(COALESCE({field}, '')) <> ''"]
    params = []
    kw = str(keyword or "").strip()
    if kw:
        where.append(f"{field} LIKE ?")
        params.append(f"%{kw}%")
    rows = get_conn().execute(
        f"SELECT {field} AS v, COUNT(*) AS n FROM delivery_db.ship_detail "
        f"WHERE {' AND '.join(where)} GROUP BY {field} "
        f"ORDER BY n DESC, v ASC LIMIT ?;", params + [int(limit)]).fetchall()
    return [{"value": r["v"], "count": r["n"]} for r in rows]

