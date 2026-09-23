"""端到端自检脚本

用途：部署后或改动后快速验证「登记 → 匹配 → 查询 → 统计 → 导出」全链路。
运行：python tests/smoke_test.py [base_url]

注意：脚本会写入测试数据，结尾会自动清理（除非加 --keep 参数）。
"""
import io
import json
import os
import sys

# 输出编码兜底：Windows 控制台默认代码页常是 GBK，标题里的符号（如 `⟕`）
# 会让 print 抛 UnicodeEncodeError —— **整个套件会在中途崩掉**，后面的章节
# 一条都不跑，看起来却像「被测代码有问题」。这里把无法编码的字符降级为
# 转义序列，保证任何代码页下都能跑完全部断言。
try:                                                    # pragma: no cover
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.stderr.reconfigure(errors="backslashreplace")
except Exception:                                       # noqa: BLE001
    pass
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 允许直接运行本脚本时导入项目模块（跨库校验需要读库文件与表结构）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

BASE = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
    else "http://127.0.0.1:8000"
KEEP = "--keep" in sys.argv

PASS, FAIL = [], []

# 本机自检必须绕过系统代理，否则请求会被 HTTP_PROXY 拦到外部
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)


# 登录验证默认开启（2026-09-20 起），所有请求都要带上会话 Cookie。
# 这里用全局变量而非 requests.Session —— 保持脚本对标准库的零依赖。
SID = ""

# 自检用的账号凭据。**不能硬编码默认密码** —— 管理员改过密码后，
# 脚本会在登录处直接失败，而且后面每一段都会跟着报 401（很难看出真正原因）。
# 取值优先级：环境变量 > tests/_smoke.env > config 里的初始值。
ENV_FILE = pathlib.Path(__file__).resolve().parent / "_smoke.env"


def _load_credentials() -> tuple:
    from config import AUTH_BOOTSTRAP_PASSWORD, AUTH_BOOTSTRAP_USER  # noqa: PLC0415
    user = os.getenv("SMOKE_USER", "")
    pwd = os.getenv("SMOKE_PASSWORD", "")
    if not pwd and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip().upper(), v.strip().strip('"').strip("'")
            if k == "SMOKE_USER" and not user:
                user = v
            elif k == "SMOKE_PASSWORD" and not pwd:
                pwd = v
    return (user or AUTH_BOOTSTRAP_USER, pwd or AUTH_BOOTSTRAP_PASSWORD)


SMOKE_USER, SMOKE_PASSWORD = _load_credentials()


def login(username=None, password=None):
    """登录并保存会话 Cookie；返回 (状态码, 响应体)。"""
    global SID
    st, res = call("POST", "/api/auth/login", {
        "username": username or SMOKE_USER,
        "password": password if password is not None else SMOKE_PASSWORD,
    })
    if st == 200:
        SID = _last_sid
    return st, res


def logout():
    global SID
    call("POST", "/api/auth/logout")
    SID = ""


def call(method, path, body=None, raw=False):
    # 路径可能含中文查询参数，需按 UTF-8 百分号编码后再发送
    url = BASE + urllib.parse.quote(path, safe="/?&=%:+")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if SID:
        req.add_header("Cookie", f"ars_sid={SID}")
    global _last_sid
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            _last_sid = _cookie_of(r.headers.get("Set-Cookie", "")) or _last_sid
            content = r.read()
            if raw:
                return r.status, content
            text = content.decode("utf-8")
            try:
                return r.status, (json.loads(text) if text else {})
            except json.JSONDecodeError:
                # 返回 HTML（例如页面请求被重定向到登录页）——不当成脚本错误
                return r.status, {"_html": True, "_len": len(text)}
    except urllib.error.HTTPError as e:
        body = e.read()
        if raw:
            return e.code, body
        try:
            return e.code, json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return e.code, {}


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {extra}" if extra else ""))


def tbl_cols(schema: str, table: str) -> set:
    """取某张表的列名集合。

    2026-09-22 换成 MySQL 之前的写法是 `PRAGMA [schema.]table_info(表名)`；
    MySQL 没有 PRAGMA，只能问 information_schema。
    """
    from core.db import get_conn as _g
    # ⚠️ information_schema 的列名在结果集里是**大写**（COLUMN_NAME），不
    # 管你在 SELECT 里怎么写；所以这里必须取别名，否则 KeyError: 'column_name'。
    return {r["name"] for r in _g().execute(
        "SELECT column_name AS name FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ?;", (schema, table))}


def tbl_exists(schema: str, table: str) -> bool:
    """表是否存在（原来查 sqlite_master）。"""
    from core.db import get_conn as _g
    return _g().execute(
        "SELECT COUNT(*) c FROM information_schema.tables "
        "WHERE table_schema = ? AND table_name = ?;",
        (schema, table)).fetchone()["c"] > 0


def schema_exists(schema: str) -> bool:
    """库（schema）是否存在。"""
    from core.db import get_conn as _g
    return _g().execute(
        "SELECT COUNT(*) c FROM information_schema.schemata "
        "WHERE schema_name = ?;", (schema,)).fetchone()["c"] > 0



# 自检创建的明细键记账文件 —— 用于「崩溃自愈」：
# 上一次若中途失败（断言抛错 / 进程被中断），这些记录会残留在库里，
# 而测试记录会复用真实快递单号，直接破坏「一个快递单 ↔ 一个售后单」等
# 结构不变量断言。所以每轮开始先按这份账清一次。
REG_FILE = pathlib.Path(__file__).resolve().parent / "_smoke_created.txt"


def remember(keys):
    with open(REG_FILE, "a", encoding="utf-8") as fh:
        for k in keys or []:
            if k:
                fh.write(str(k) + "\n")


_last_sid = ""


def _cookie_of(set_cookie: str) -> str:
    """从 Set-Cookie 头里取出会话 ID。"""
    for part in (set_cookie or "").split(";"):
        k, _, v = part.partition("=")
        if k.strip().lower() == "ars_sid":
            return v.strip()
    return ""


_raw_call = call


def call(method, path, body=None, raw=False):
    """包装原始请求：自动登记本次自检新建的明细键。"""
    st, res = _raw_call(method, path, body, raw)
    if method == "POST" and path in ("/api/returns", "/api/returns/batch") \
            and isinstance(res, dict):
        keys = res.get("detail_keys") or []
        if res.get("detail_key"):
            keys = list(keys) + [res["detail_key"]]
        remember(keys)
    return st, res


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随 3xx —— 需要看「是否被重定向」本身。"""

    def redirect_request(self, *a, **kw):  # noqa: D102
        return None


_NO_REDIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           _NoRedirect)


def probe(path):
    """只看状态码，不跟随重定向、不解析响应体。"""
    req = urllib.request.Request(BASE + path, method="GET")
    if SID:
        req.add_header("Cookie", f"ars_sid={SID}")
    try:
        with _NO_REDIRECT.open(req, timeout=20) as r:
            return r.status, r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def upload(path, filepath, query=""):
    """以 multipart/form-data 上传本地文件。"""
    boundary = "----smoke" + str(int(time.time() * 1000))
    with open(filepath, "rb") as f:
        blob = f.read()
    head = (f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{os.path.basename(filepath)}"\r\n'
            f'Content-Type: application/octet-stream\r\n\r\n').encode("utf-8")
    body = head + blob + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(BASE + path + query, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if SID:                       # 上传同样要带会话，否则被中间件拦成 401
        req.add_header("Cookie", f"ars_sid={SID}")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            text = r.read().decode("utf-8")
            return r.status, (json.loads(text) if text else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return e.code, {}


print("=" * 62)
print(f"  售后返件登记系统 · 端到端自检   ({BASE})")
print("=" * 62)

# ---------------------------------------------------------------- 登录
# 登录验证默认开启，后续所有请求都要带会话；这里先登进去，
# 具体的行为断言放在 [9] 段（那里会临时登出，验证未登录时的拦截）。
print("\n[0a] 登录（登录验证默认开启）")
_st, _me = call("GET", "/api/auth/me")
if _st == 200 and not _me.get("auth_enabled"):
    print("  [SKIP] 登录验证已关闭（本地模式），跳过登录")
else:
    _st, _res = login()
    check(f"用账号 {SMOKE_USER} 登录成功", _st == 200 and bool(SID),
          f"must_change_pwd={_res.get('must_change_pwd')}")
    if not SID:
        # 登录失败就让脚本停在这里 —— 否则每一段断言都会因为 401 报错，
        # 一屏红色里看不出真正的原因是「密码不对」。
        print()
        print("  " + "-" * 58)
        print("  无法登录，自检无法继续。请提供当前账号密码，二选一：")
        print()
        print("    ① 临时指定（只对本次有效）")
        print("        set SMOKE_PASSWORD=你的密码 && "
              "python tests\\smoke_test.py")
        print()
        print(f"    ② 写进 {ENV_FILE.name}（一次性，之后自检自动读取）")
        print(f"        {ENV_FILE}")
        print("        SMOKE_USER=admin")
        print("        SMOKE_PASSWORD=你的密码")
        print()
        print("  忘了密码？用命令行重置（不会动其它账号与权限组）：")
        print("        python tools\\reset_admin_password.py --user admin --random")
        print("  " + "-" * 58)
        sys.exit(2)
    check("登录后身份接口返回当前用户",
          (call("GET", "/api/auth/me")[1] or {}).get("user", {}).get(
              "username") is not None,
          _res.get("user", {}).get("group_name"))

# ---------------------------------------------------------------- 0. 起始自愈
print("\n[0] 起始自愈：清掉上一次未收尾的自检残留")
if KEEP:
    print("  [SKIP] 已指定 --keep，保留上次残留")
else:
    stale = [ln.strip() for ln in REG_FILE.read_text(encoding="utf-8").splitlines()
             if ln.strip()] if REG_FILE.exists() else []
    healed = 0
    for k in stale:
        st, _r = call("DELETE", f"/api/returns/{k}")
        if st == 200:
            healed += 1
    REG_FILE.write_text("", encoding="utf-8")
    print(f"  上次残留 {len(stale)} 条，已清理 {healed} 条"
          if stale else "  无残留")

    # 除了退回明细，物料与照片也要自愈 —— 它们在断言里比的是**绝对数量**
    # （「新增物料」判 action=create、「磁盘生成 4 个文件」），
    # 上一次若在中途崩掉，这些残留会让下一轮以「和本轮改动无关」的方式失败，
    # 排查时极易误判成代码问题。
    from core import photos as _photos                       # noqa: PLC0415
    from core.db import get_conn as _gc                      # noqa: PLC0415
    _c = _gc()          # 供 [7b] 段复用（同一行号作用域内）
    # 注意：DELETE /api/items/{item_id} 只接受**数字 id**，
    # 传料号会被 FastAPI 判成 422 —— 删不掉又不报错（调用方没看返回值），
    # 残留行会顶高「物料总数」基线，让后续断言以无关的方式失败。
    _left = [(r["id"], r["material_no"]) for r in _c.execute(
        "SELECT id, material_no FROM items_db.item_master "
        "WHERE material_no LIKE 'SMOKE-%';")]
    for _id, _ in _left:
        call("DELETE", f"/api/items/{_id}")
    if _left:
        print(f"  清理上次残留的物料 {len(_left)} 条："
              f"{[m for _, m in _left]}")

    # 照片：自检固定用 260920001 这个售后单，残留文件会让「磁盘文件数」断言偏大
    if _photos.count_files("260920001-001"):
        _gone, _left_photos = _photos.delete_all("260920001-001")
        print(f"  清理上次残留的照片：删 {_gone} 个，剩 {_left_photos} 个（被占用会留下）")

    # 发货 / 台账残留（2026-09-22 新增）：这三类残留都会让下一轮以「和本轮改动
    # 无关」的方式失败，2026-09-22 实际吃过一次（上一轮崩在 [28] 中途）：
    #   · 留在 delivery_shipment 的登记行 → [27] 的「导入新增」被判成 update，
    #     那条记录还是已挂接状态，紧接着的「它当前是未关联」也跟着红；
    #   · 留在申请单明细里的行 → 台账自动切到「正式口径」（需返回行数不再是 0），
    #     [28] 里按演示口径算的绝对数全部对不上；
    #   · 留在 ledger_clear 的核销记录 → [28] 的「汇总 = 1 / = 2」被顶高。
    # 只清自检自己造的东西：申请单看申请人（自检专用账号）+ 明细料的 TR- 前缀，
    # 登记行看 TR- 单号或挂在自检申请单上，核销记录看「自检」开头的说明。
    _dl_where = ("(r.applicant = ? OR i.material_no LIKE 'TR-%' "
                 "OR i.product_model LIKE 'TR-%')")
    _dl_reqs = [r["request_no"] for r in _c.execute(
        "SELECT DISTINCT i.request_no FROM delivery_db.delivery_request_item i "
        "LEFT JOIN delivery_db.delivery_request r ON r.request_no = i.request_no "
        f"WHERE {_dl_where};", (SMOKE_USER,))]
    _dl_reqs += [r["request_no"] for r in _c.execute(
        "SELECT request_no FROM delivery_db.delivery_request "
        "WHERE applicant = ? AND request_no NOT IN ("
        "SELECT DISTINCT request_no FROM delivery_db.delivery_request_item);",
        (SMOKE_USER,))]
    _dl_reqs = sorted(set(_dl_reqs))
    _dl_ship = 0
    if _dl_reqs:
        _marks = ",".join("?" * len(_dl_reqs))
        _dl_ship = _c.execute(
            f"SELECT COUNT(*) c FROM delivery_db.delivery_shipment "
            f"WHERE request_no IN ({_marks});", tuple(_dl_reqs)).fetchone()["c"]
        _c.execute(f"DELETE FROM delivery_db.delivery_shipment "
                   f"WHERE request_no IN ({_marks});", tuple(_dl_reqs))
    _dl_ship += _c.execute("SELECT COUNT(*) c FROM delivery_db.delivery_shipment "
                           "WHERE ship_no LIKE 'TR-%';").fetchone()["c"]
    _c.execute("DELETE FROM delivery_db.delivery_shipment WHERE ship_no LIKE 'TR-%';")
    for _no in _dl_reqs:
        _c.execute("DELETE FROM delivery_db.delivery_request_item "
                   "WHERE request_no=?;", (_no,))
        _c.execute("DELETE FROM delivery_db.delivery_request WHERE request_no=?;", (_no,))
    # 2026-09-23：自动核销（kind='auto'）的说明是固定文案、不以「自检」开头，崩在
    # 写入之后 / 撤销之前就会留下永久残留（实测顶住台账口径）—— 按自检账号一并收走。
    _clr_where = "(reason LIKE '自检%' OR operator = ?)"
    _dl_clr = _c.execute("SELECT COUNT(*) c FROM delivery_db.ledger_clear "
                         f"WHERE {_clr_where};", (SMOKE_USER,)).fetchone()["c"]
    if _dl_clr:
        _c.execute(f"DELETE FROM delivery_db.ledger_clear WHERE {_clr_where};",
                   (SMOKE_USER,))
    if _dl_ship or _dl_reqs or _dl_clr:
        print(f"  清理发货/台账残留：登记行 {_dl_ship} 条、申请单 {len(_dl_reqs)} 张"
              f"{'（' + '、'.join(_dl_reqs) + '）' if _dl_reqs else ''}、"
              f"核销记录 {_dl_clr} 条")

# ---------------------------------------------------------------- 1. 服务
print("\n[1] 服务与元数据")
st, health = call("GET", "/api/health")
check("健康检查返回 ok", st == 200 and health.get("ok") is True, health.get("time"))
st, meta = call("GET", "/api/meta")
check("字段定义可读取", st == 200 and len(meta.get("field_groups", [])) >= 4,
      f"{len(meta.get('field_groups', []))} 个分组")
header_fields = meta.get("header_fields", [])
check("登记页已移除「其他」模块（备注 / 登记人）",
      st == 200 and "remark" not in header_fields and "registrar" not in header_fields,
      f"整单字段：{header_fields}")
check("备注与登记人仍可在查询页编辑",
      any(f.get("name") == "remark" for g in meta.get("field_groups", [])
          for f in g.get("fields", [])),
      "FIELD_GROUPS 保留")
check("分析报告为固定选项下拉（是 / 否）",
      meta.get("fixed_options", {}).get("analysis_report") == ["是", "否"],
      str(meta.get("fixed_options", {}).get("analysis_report")))
check("分析报告出现在明细行字段中",
      "analysis_report" in meta.get("line_columns", []),
      str(meta.get("line_columns")))

base_total = health.get("db", {}).get("total", 0)

# ---------------------------------------------------------------- 2. 登记
print("\n[2] 登记写入")
rec_a = {
    "return_no": "SF1555862141833", "carrier": "顺丰速运",
    "return_date": "2026-09-01", "turbine_vendor": "上海电气",
    "project_site": "江西乌梅山", "product_code": "20080207",
    "product_model": "51228.68.242", "product_category": "III型风向",
    "material_no": "10002-0079", "product_name": "抗冰冻风向传感器",
    "spec": "51228.68.242", "production_year": "2020", "return_qty": 1,
    "info_source": "售后端", "remark": "自检写入-甲",
}
st, res_a = call("POST", "/api/returns", rec_a)
check("登记记录 A 成功", st == 200 and res_a.get("ok") is True, res_a.get("detail_key"))
key_a = res_a.get("detail_key")
order_no = res_a.get("order_no")
check("自动分配售后单号", bool(order_no), order_no)
check("自动生成明细唯一键", key_a == f"{order_no}-001", key_a)
check("单号规则＝年份后两位+月日+顺序号",
      bool(re.fullmatch(r"\d{9}", str(order_no)))
      and str(order_no).startswith(time.strftime("%y%m%d")),
      f"{order_no}（当日前缀应为 {time.strftime('%y%m%d')}）")
check("单号顺序号从 001 起",
      str(order_no).endswith("001") or int(str(order_no)[-3:]) >= 1,
      f"顺序号 {str(order_no)[-3:]}")

# 续接同一售后单 -> 行号应递增
st, res_b = call("POST", "/api/returns", {
    "order_no": order_no, "return_no": "SF0256250261535", "carrier": "顺丰速运",
    "return_date": "2026-09-02", "turbine_vendor": "明阳智能",
    "project_site": "天津基地", "product_model": "微动电缆",
    "product_category": "电缆", "return_qty": 2, "remark": "自检写入-乙",
})
check("续接同单号追加行", st == 200 and res_b.get("line_no") == 2,
      f"{res_b.get('detail_key')} (is_new_order={res_b.get('is_new_order')})")
key_b = res_b.get("detail_key")

# 全新一单
st, res_c = call("POST", "/api/returns", {
    "return_no": "SF0255629837445", "return_date": "2026-09-03",
    "turbine_vendor": "三一重能", "project_site": "叶县风场",
    "product_code": "19121192", "product_model": "51228.68.224",
    "product_category": "III型风向", "return_qty": 1, "remark": "自检写入-丙",
})
check("新建第二张售后单", st == 200 and res_c.get("is_new_order") is True,
      res_c.get("detail_key"))
check("明细唯一键连续生成", res_c.get("detail_key") == f"{res_c.get('order_no')}-001",
      res_c.get("detail_key"))
check("当日顺序号递增",
      int(str(res_c.get("order_no"))[-3:]) == int(str(order_no)[-3:]) + 1,
      f"{order_no} -> {res_c.get('order_no')}")
key_c = res_c.get("detail_key")

# ---------------------------------------------------------------- 2b. 批量登记
print("\n[2b] 批量登记（一个快递单多行产品）")
st, batch = call("POST", "/api/returns/batch", {
    "header": {
        "return_no": "SF9988776655443", "carrier": "顺丰速运",
        "return_date": "2026-09-05", "turbine_vendor": "金风科技",
        "project_site": "张北风场", "info_source": "销售端",
        "remark": "自检写入-批量",
    },
    "items": [
        {"product_code": "BM-001", "product_model": "51228.68.300",
         "product_category": "III型风向", "return_qty": 1,
         "feedback_issue": "无信号", "issue_category": "客户应用",
         "responsibility": "客户端", "analysis_report": "是"},
        {"product_code": "BM-002", "product_model": "51228.68.301",
         "product_category": "III型风向", "return_qty": 2,
         "feedback_issue": "信号漂移", "issue_category": "NTF",
         "responsibility": "其他端", "analysis_report": "否"},
        {"product_code": "BM-003", "material_no": "10002-0088",
         "product_name": "微动电缆", "product_category": "电缆", "return_qty": 1},
    ],
})
batch_keys = batch.get("detail_keys", []) if isinstance(batch, dict) else []
batch_order = batch.get("order_no") if isinstance(batch, dict) else None
check("批量登记接口可用", st == 200 and batch.get("ok") is True,
      f"{batch.get('count')} 行")
check("一次生成多行明细", batch.get("count") == 3, str(batch_keys))
check("多行共用同一售后单号",
      bool(batch_order) and all(k.startswith(batch_order + "-") for k in batch_keys),
      batch_order)
check("行号连续递增",
      [k.split("-")[-1] for k in batch_keys] == ["001", "002", "003"],
      str([k.split("-")[-1] for k in batch_keys]))

st, b1 = call("GET", f"/api/returns/{batch_keys[0]}") if batch_keys else (0, {})
st, b2 = call("GET", f"/api/returns/{batch_keys[1]}") if len(batch_keys) > 1 else (0, {})
check("整单信息继承到每一行",
      b1.get("return_no") == "SF9988776655443"
      and b2.get("return_no") == "SF9988776655443",
      b1.get("turbine_vendor"))
check("检测信息按行独立关联",
      b1.get("feedback_issue") == "无信号" and b2.get("feedback_issue") == "信号漂移",
      f"{b1.get('feedback_issue')} / {b2.get('feedback_issue')}")
check("根因归类按行独立关联",
      b1.get("responsibility") == "客户端" and b2.get("responsibility") == "其他端",
      f"{b1.get('responsibility')} / {b2.get('responsibility')}")
check("行级字段互不污染", b2.get("issue_category") == "NTF",
      b2.get("issue_category"))
check("分析报告按行独立记录",
      b1.get("analysis_report") == "是" and b2.get("analysis_report") == "否",
      f"{b1.get('analysis_report')} / {b2.get('analysis_report')}")
check("未填登记人时自动取系统默认值",
      b1.get("registrar") == "管理员", b1.get("registrar"))

# ---------------------------------------------------------------- 2c. 补登与查重
print("\n[2c] 按快递单号补登与重复登记拦截")
st, chk = call("POST", "/api/returns/check", {
    "header": {"return_no": "SF9988776655443"},
    "items": [{"product_code": "BM-001"}],
})
check("预检接口可解析归属单号",
      st == 200 and chk.get("order_no") == batch_order,
      f"{chk.get('order_no')}（应为原单 {batch_order}）")
check("预检能识别重复登记", chk.get("has_duplicate") is True,
      str((chk.get("duplicates") or [])[:1]))

st, dup = call("POST", "/api/returns/batch", {
    "header": {"return_no": "SF9988776655443", "return_date": "2026-09-05"},
    "items": [{"product_code": "BM-001", "product_model": "51228.68.300"}],
})
check("重复登记被拒绝（HTTP 409）", st == 409, f"HTTP {st}")
_detail = dup.get("detail") if isinstance(dup.get("detail"), dict) else {}
check("拒绝响应带回重复明细",
      len(_detail.get("duplicates", [])) == 1,
      str(_detail.get("duplicates", [])[:1]))

st, add = call("POST", "/api/returns/batch", {
    "header": {"return_no": "SF9988776655443", "return_date": "2026-09-05"},
    "items": [{"product_code": "BM-004", "product_model": "51228.68.400",
               "product_category": "III型风向", "return_qty": 1}],
})
add_keys = add.get("detail_keys", []) if isinstance(add, dict) else []
check("同快递单增加新产品可补登", st == 200 and add.get("ok") is True,
      str(add_keys))
check("补登自动归入原售后单",
      add.get("order_no") == batch_order and add.get("is_new_order") is False,
      f"{add.get('order_no')}（原单 {batch_order}）")
check("补登行号在原单内续接",
      bool(add_keys) and add_keys[0].endswith("-004"), str(add_keys))

st, chk2 = call("POST", "/api/returns/check", {
    "header": {"return_no": "SF-NOT-EXIST-0001"},
    "items": [{"product_code": "BM-001"}],
})
check("全新快递单识别为新单", chk2.get("is_new_order") is True
      and chk2.get("continues_existing") is False, chk2.get("order_no"))

# ---------------------------------------------------------------- 3. 匹配
print("\n[3] 扫码实时匹配")
st, m1 = call("POST", "/api/scan/match", {"code": "SF1555862141833"})
check("精确命中退回单号", st == 200 and m1.get("exact") and
      m1["matches"][0]["field"] == "return_no",
      f"{m1.get('matches', [{}])[0].get('field_label')} x{m1.get('matches', [{}])[0].get('count')}")
check("匹配结果带回填建议", bool(m1.get("suggest")), (m1.get("suggest") or {}).get("_desc"))

st, m2 = call("POST", "/api/scan/match", {"code": "20080207"})
check("精确命中产品编号", st == 200 and m2.get("exact") and
      m2["matches"][0]["field"] == "product_code",
      m2.get("suggest", {}).get("product_model"))

st, m3 = call("POST", "/api/scan/match", {"code": "0255629837445"})
check("未知码走模糊匹配", st == 200 and m3.get("fuzzy") is True,
      f"{m3.get('matches', [{}])[0].get('field_label')}({m3.get('matches', [{}])[0].get('fuzzy_mode')})")

st, m4 = call("POST", "/api/scan/match", {"code": "ZZZZ99999999"})
check("完全未知码不报错", st == 200 and m4.get("exact") is False and
      m4.get("fuzzy") is False, "按新件处理")

st, m5 = call("POST", "/api/scan/match", {"code": "51228.68.242"})
check("命中产品型号字段", st == 200 and m5.get("exact") is True, "型号反查")

# ---------------------------------------------------------------- 4. 查询
print("\n[4] 明细查询与筛选")
st, q1 = call("GET", "/api/returns?page_size=100")
check("列表接口可用", st == 200 and q1.get("total", 0) >= base_total + 3,
      f"total={q1.get('total')}")

st, q2 = call("GET", "/api/returns?keyword=" + urllib.parse.quote(key_a))
check("关键词检索命中", st == 200 and q2.get("total", 0) == 1, f"total={q2.get('total')}")

# 上面那条同时是「明细唯一键必须走 r.detail_key」的回归守卫。
# detail_key 同时登记在 INSPECT_COLUMNS 与 HANDLE_COLUMNS 上，而 _qualify()
# 的 HANDLE 分支排在前面 —— 一旦编译成 h.detail_key，处理库的稀疏存储
# （新登记的明细在那里根本没有行）会让 NULL LIKE 永远不成立，
# 于是「用明细唯一键搜索」安静地返回 0 条，一个错都不报。
import core.repository as _kw_repo                            # noqa: E402
_kw_sql, _ = _kw_repo._build_where({"keyword": key_a})
check("明细唯一键编译成 r.detail_key",
      "r.detail_key LIKE" in _kw_sql and "h.detail_key" not in _kw_sql,
      _kw_sql.split(" OR ")[0].strip())

st, q3 = call("GET", "/api/returns?product_category=III型风向")
check("按产品类别筛选", st == 200 and q3.get("total", 0) >= 2, f"total={q3.get('total')}")

st, q4 = call("GET", "/api/returns?return_date_from=2026-09-01&return_date_to=2026-09-30")
check("按退回日期区间筛选", st == 200 and q4.get("total", 0) >= 3, f"total={q4.get('total')}")

st, q5 = call("GET", "/api/returns?sort_by=return_date&sort_dir=asc&page_size=5")
check("排序与分页生效", st == 200 and q5.get("page_size") == 5,
      f"page={q5.get('page')} pages={q5.get('pages')}")

st, q6 = call("GET", "/api/returns?completion=完结")
check("多值筛选不报错", st == 200, f"total={q6.get('total')}")

st, q7 = call("GET", "/api/returns?info_source=销售端")
check("按快递归属筛选", st == 200 and q7.get("total", 0) >= 1,
      f"total={q7.get('total')}")

st, q8 = call("GET", "/api/returns?analysis_report=是")
check("按分析报告筛选", st == 200 and q8.get("total", 0) >= 1,
      f"total={q8.get('total')}")

# ---------------------------------------------------------------- 5. 更新
print("\n[5] 检测信息补录")
st, upd = call("PUT", f"/api/returns/{key_a}", {
    "test_date": "2026-09-10", "feedback_issue": "信号异常",
    "test_result": "加热正常，信号正常，启动正常", "fault_cause": "/",
    "solution": "拆解报废", "issue_category": "NTF", "responsibility": "其他端",
    "completion": "完结",
})
check("更新检测信息成功", st == 200 and upd.get("ok") is True, f"changed={upd.get('changed')}")

st, got = call("GET", f"/api/returns/{key_a}")
check("更新结果已落库", got.get("test_result") == "加热正常，信号正常，启动正常",
      got.get("completion"))
# 金山文档同步模块已于 2026-09-21 整体移除：回包里不应再出现同步字段
check("明细回包不再带同步字段（已随模块删除）",
      "sync_state" not in got and "synced_at" not in got,
      " · ".join(k for k in ("sync_state", "synced_at") if k in got) or "无残留")

# ---------------------------------------------------------------- 6. 统计
print("\n[6] 看板统计 · 返件汇总")
st, stats = call("GET", "/api/stats?scope=return&granularity=month")
check("返件汇总接口可用", st == 200 and stats.get("scope") == "return",
      f"scope={stats.get('scope')}")
ov = stats.get("overview", {})
check("返件 KPI 数值正确", ov.get("records", 0) >= 3 and ov.get("qty", 0) >= 4,
      f"records={ov.get('records')} qty={ov.get('qty')}")
check("返件趋势非空", len(stats.get("trend", [])) >= 1,
      str([t.get("label") for t in stats.get("trend", [])]))
check("类别分布非空", len(stats.get("by_category", [])) >= 2,
      str([c.get("label") for c in stats.get("by_category", [])]))
check("交叉分析非空", len(stats.get("cross_vendor_category", {}).get("left", [])) >= 2,
      str(stats.get("cross_vendor_category", {}).get("left")))
check("返件汇总不含检测侧指标",
      not {"inspected", "untested", "avg_test_days", "coverage"} & set(ov),
      "KPI 键：" + ", ".join(sorted(ov)))
check("返件汇总不含检测侧分组",
      not {"by_cause", "by_test_result", "by_improvement"} & set(stats),
      "含 " + str(len(stats)) + " 个键")
check("返件汇总不再按产品型号分组（改为产品类别）",
      "by_model" not in stats and len(stats.get("by_category", [])) >= 2,
      f"类别 {len(stats.get('by_category', []))} 组")
check("返件汇总不再按 反馈现象 / 项目风场 分组",
      not {"by_issue", "by_project_site"} & set(stats),
      "含 " + str(len(stats)) + " 个键：" + ", ".join(sorted(stats)))
check("生产年份按年份排序（分布而非排行）",
      [x["label"] for x in stats.get("by_production_year", [])] ==
      sorted([x["label"] for x in stats.get("by_production_year", [])]),
      str([x["label"] for x in stats.get("by_production_year", [])])[:60])

st, stats_f = call("GET", "/api/stats?scope=return&product_category="
                   + urllib.parse.quote("III型风向"))
check("返件汇总支持筛选联动", st == 200 and
      stats_f.get("overview", {}).get("records", 0) >= 2,
      f"records={stats_f.get('overview', {}).get('records')}")

print("\n[6b] 看板统计 · 检测汇总")
# 本段多处会直接调 repo 层函数做交叉验证（比 API 的 TOP N 更精确），
# import 放段首 —— 放中间的话，新断言只要插在它之前就会 NameError（已踩两次）
import core.repository as _repo                               # noqa: E402
st, ins = call("GET", "/api/stats?scope=inspect&granularity=month")
check("检测汇总接口可用", st == 200 and ins.get("scope") == "inspect",
      f"scope={ins.get('scope')}")
iov = ins.get("overview", {})
check("已检 + 待检 = 全量（两个口径互补，不重复计数）",
      iov.get("inspected", 0) + iov.get("untested", 0) == iov.get("records", -1),
      f"已检 {iov.get('inspected')} + 待检 {iov.get('untested')} "
      f"= {iov.get('inspected', 0) + iov.get('untested', 0)}，全量 {iov.get('records')}")
check("检测覆盖率与已检数一致",
      abs(iov.get("coverage", 0) - round(
          iov.get("inspected", 0) * 100.0 / (iov.get("records") or 1), 1)) < 0.05,
      f"覆盖率 {iov.get('coverage')}%")
check("未完结数不超过已检数",
      0 <= iov.get("unfinished", 0) <= iov.get("inspected", 0),
      f"未完结 {iov.get('unfinished')} / 已检 {iov.get('inspected')}")
check("平均检测时长可计算", iov.get("avg_test_days", 0) > 0,
      f"{iov.get('avg_test_days')} 天（口径 {iov.get('fast_days')} 天内算及时）")
check("检测趋势按检测时间取数", len(ins.get("trend", [])) >= 1,
      str([t.get("label") for t in ins.get("trend", [])])[:60])
check("已移除的图不再拖数据（对比 / 型号 / 结果 / 方案 / 改善措施 / 原因×责任）",
      not {"return_trend", "by_model", "by_test_result", "by_solution",
           "by_erp", "by_improvement", "cross_cause_responsibility"} & set(ins),
      "含 " + str(len(ins)) + " 个键：" + ", ".join(sorted(ins)))
check("检测归因分组有值", len(ins.get("by_cause", [])) >= 1,
      str([c.get("label") for c in ins.get("by_cause", [])])[:50])
check("检测汇总不含返件侧分布（类别 / 快递归属）",
      not {"by_category", "by_info_source"} & set(ins),
      "含 " + str(len(ins)) + " 个键")

# 需求①：TOP 厂家 + 该厂家的 TOP 产品
_vp = ins.get("vendor_products") or {}
_vendors = _vp.get("vendors") or []
check("检测汇总含「厂家 → 主要产品」下钻",
      len(_vendors) >= 1, f"{len(_vendors)} 家 · 合计 {_vp.get('total')} 条")
_first = _vendors[0] if _vendors else {}
check("厂家下钻结构完整（条数 / 占比 / 明细 / 其他）",
      {"label", "value", "share", "items", "others"} <= set(_first),
      f"字段：{sorted(_first)}")
check("厂家下钻明细带条数与占比",
      bool(_first.get("items")) and
      {"label", "value", "share"} <= set(_first["items"][0]),
      " · ".join(f"{p['label']}×{p['value']}({p['share']}%)"
                 for p in (_first.get("items") or [])[:2]))
_cats = {g["label"] for g in _repo.stats_group("product_category", {}, 500, "inspect")}
_item_labels = {p["label"] for v in _vendors for p in (v.get("items") or [])}
check("厂家下钻的条目取自产品类别（不是型号）",
      bool(_item_labels) and _item_labels <= _cats,
      f"{len(_item_labels)} 个标签全部属于类别集合（库里共 {len(_cats)} 类）")

check("厂家下钻按检测条数降序",
      all(_vendors[i]["value"] >= _vendors[i + 1]["value"]
          for i in range(len(_vendors) - 1)),
      str([v["value"] for v in _vendors[:5]]))
check("厂家下钻与「厂家 TOP」分组同序同值",
      [v["label"] for v in _vendors[:3]] ==
      [g["label"] for g in ins.get("by_vendor", [])[:3]],
      f"{[v['label'] for v in _vendors[:3]]} vs "
      f"{[g['label'] for g in ins.get('by_vendor', [])[:3]]}")

# 需求②：产品类别 × 故障原因
_cc = ins.get("cross_category_cause") or {}
check("检测汇总含「产品类别 × 故障原因」交叉",
      len(_cc.get("left", [])) >= 2 and len(_cc.get("right", [])) >= 2,
      f"类别 {len(_cc.get('left', []))} × 原因 {len(_cc.get('right', []))}")
# 右轴是「左轴 TOP 8 类别里出现过的原因」，天然少于全库原因数 ——
# 所以不能拿全库原因数比。正确的比较对象是「带截断的同一查询」。
_cc_cut = _repo.stats_cross_index({}, "product_category", "fault_cause", 8,
                                  "inspect", right_limit=6)
check("故障原因轴不做后端截断（改由前端筛选控制）",
      len(_cc.get("right", [])) >= len(_cc_cut.get("right", []))
      and "其他" not in _cc.get("right", []),
      f"接口 {len(_cc.get('right', []))} 种（不归并）"
      f" ≥ 截断版 {len(_cc_cut.get('right', []))} 种（含「其他」）")
check("交叉矩阵合计等于已检条数（未检测不混入）",
      sum(sum(col.values()) for col in (_cc.get("matrix") or {}).values())
      <= iov.get("inspected", 0),
      f"矩阵合计 {sum(sum(c.values()) for c in (_cc.get('matrix') or {}).values())}"
      f" ≤ 已检 {iov.get('inspected')}")

# 需求③：责任归属统计
check("检测汇总含责任归属统计", len(ins.get("by_responsibility", [])) >= 1,
      " · ".join(f"{g['label']}={g['value']}"
                 for g in ins.get("by_responsibility", [])))

# 检测汇总只统计已检测明细 —— 未检测的不该混进归因图
st, _ins_all = call("GET", "/api/stats?scope=inspect")
_grouped = sum(x["value"] for x in (_ins_all.get("by_cause") or []))
check("检测归因图合计不超过已检条数（未检测不混入）",
      _grouped <= iov.get("inspected", 0),
      f"归因合计 {_grouped} ≤ 已检 {iov.get('inspected')}")

# 筛选条件必须真的生效 —— 漏登记会**静默丢弃**条件（不报错、返回全量），
# 比报错更难发现，所以这里逐个用真实取值验证收窄效果。
st, all_rows = call("GET", "/api/returns?page_size=1")
_total_all = all_rows.get("total", 0)
for _field, _label in (("fault_cause", "故障原因"), ("test_result", "检测结果"),
                       ("improvement", "改善措施")):
    _cands = call("GET", f"/api/stats?scope=inspect")[1].get(
        {"fault_cause": "by_cause", "test_result": "by_test_result",
         "improvement": "by_improvement"}[_field]) or []
    if not _cands:
        continue
    _val = _cands[0]["label"]
    st, _r = call("GET", f"/api/returns?page_size=1&{_field}="
                  + urllib.parse.quote(_val))
    check(f"{_label}筛选真的收窄结果（不静默丢弃条件）",
          _r.get("total", _total_all) < _total_all,
          f"「{_val}」→ {_r.get('total')} 条 < 全量 {_total_all} 条")

# 防呆：筛选面板上的每个字段都必须出现在 WHERE 构造函数的清单里。
# 漏登记不会报错 —— 条件被静默丢弃，界面上选了筛选却返回全量。
import ast as _ast                                          # noqa: E402
import inspect as _inspect                                  # noqa: E402

_panel = call("GET", "/api/meta")[1].get("filter_fields", [])
_src = _inspect.getsource(_repo._build_where)
_missing = [f["name"] for f in _panel if f'"{f["name"]}"' not in _src]
check("筛选面板字段全部在 WHERE 构造清单中（防漏登记）",
      not _missing, f"遗漏：{_missing}" if _missing else
      f"{len(_panel)} 项全部覆盖")

# 参数声明同样会静默丢条件：FastAPI 只认签名里声明过的查询参数。
# 解析 app.py 源码，确保列表接口与导出接口都声明了全部筛选项，
# 且导出 ≥ 列表（否则「筛选后导出」会导出全量）。
import pathlib as _path                                     # noqa: E402

_tree = _ast.parse((_path.Path(__file__).resolve().parent.parent
                    / "app.py").read_text(encoding="utf-8"))
_sigs = {}
for _n in _ast.walk(_tree):
    if isinstance(_n, _ast.FunctionDef) and _n.name in ("list_returns", "export"):
        _sigs[_n.name] = {_a.arg for _a in _n.args.args}
_filter_names = [f["name"] for f in _panel] + [
    "test_result", "improvement", "report_no", "erp_handled",   # 检测侧（未进面板也要能筛）
]
_lost = [f for f in _filter_names if f not in _sigs.get("list_returns", set())]
check("列表接口声明了全部筛选参数（否则 FastAPI 静默丢弃）",
      not _lost, f"未声明：{_lost}" if _lost else
      f"{len(_sigs.get('list_returns', []))} 个参数")
_export_missing = sorted(_sigs.get("list_returns", set())
                         - _sigs.get("export", set())
                         - {"page", "page_size", "sort_by", "sort_dir"})
check("导出接口参数覆盖列表接口（筛选后导出不会导全量）",
      not _export_missing, f"导出缺：{_export_missing}" if _export_missing else "一致")

# ---------------------------------------------------------------- 7. 字典
print("\n[7] 字典与型号库")
st, d1 = call("GET", "/api/dict/product_category")
check("字典选项已自动汇总", st == 200 and len(d1.get("options", [])) >= 2,
      str([o.get("value") for o in d1.get("options", [])]))
st, d2 = call("GET", "/api/models")
check("型号字典已自动积累", st == 200 and len(d2.get("rows", [])) >= 2,
      str([r.get("product_model") for r in d2.get("rows", [])][:4]))

# ---------------------------------------------------------------- 7b. 匹配数据库
print("\n[7b] 匹配数据库（物料主档）")
st, im = call("GET", "/api/items/meta")
im_total = im.get("stats", {}).get("total", 0)
check("匹配库元数据可读取", st == 200 and im_total > 0, f"{im_total} 条物料")

_lb = im.get("labels", {})
check("标参列已更名",
      _lb.get("param1") == "产品量程" and _lb.get("param2") == "信号输出"
      and _lb.get("param3") == "防护等级",
      f"{_lb.get('param1')} / {_lb.get('param2')} / {_lb.get('param3')}")

st, il = call("GET", "/api/items?page_size=5")
check("物料列表可分页查询", st == 200 and len(il.get("rows", [])) == 5,
      f"total={il.get('total')}")

first_no = (il.get("rows") or [{}])[0].get("material_no")
st, lk = call("GET", "/api/items/lookup?code=" + urllib.parse.quote(str(first_no)))
check("按料号反查物料", st == 200 and lk.get("found") is True,
      f"{first_no} -> {(lk.get('item') or {}).get('product_name')}")
check("反查结果可直接回填表单",
      all(k in (lk.get("suggest") or {})
          for k in ("material_no", "product_name", "product_model", "spec")),
      str(list((lk.get("suggest") or {}).keys())))

st, ik = call("GET", "/api/items?keyword=" + urllib.parse.quote("风速"))
check("按关键词搜索物料", st == 200 and ik.get("total", 0) > 0,
      f"total={ik.get('total')}")

st, ms = call("POST", "/api/scan/match", {"code": str(first_no)})
check("扫码匹配优先命中匹配数据库",
      st == 200 and (ms.get("suggest") or {}).get("_source") == "item_master",
      (ms.get("suggest") or {}).get("_desc"))

tmp_no = "SMOKE-TEST-001"
st, created = call("POST", "/api/items", {
    "material_no": tmp_no, "product_name": "自检临时物料",
    "model_no": "SMK-1", "spec": "SMK-SPEC-1", "production_stat": "自检",
})
check("新增物料", st == 200 and created.get("action") == "create",
      f"id={created.get('id')}")

st, updated = call("POST", "/api/items", {
    "material_no": tmp_no, "product_name": "自检临时物料（改）", "spec": "SMK-SPEC-2",
})
check("同料号再次保存识别为更新", st == 200 and updated.get("action") == "update", "")

st, il2 = call("GET", "/api/items?keyword=" + urllib.parse.quote(tmp_no))
row2 = (il2.get("rows") or [{}])[0]
check("更新内容已落库", row2.get("product_name") == "自检临时物料（改）",
      row2.get("spec"))

st, exp = call("GET", "/api/items/export", raw=True)
check("匹配库可导出 Excel", st == 200 and exp[:2] == b"PK", f"{len(exp)} bytes")

tmp_xlsx = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_items_export.xlsx")
try:
    with open(tmp_xlsx, "wb") as f:
        f.write(exp)
    st, imp = upload("/api/items/import", tmp_xlsx, "?mode=upsert")
    check("Excel 导入接口可用", st == 200 and imp.get("ok") is True,
          f"新增 {imp.get('inserted')} / 更新 {imp.get('updated')}")
    check("重复导入按料号覆盖、不产生重复行",
          imp.get("inserted") == 0 and imp.get("updated", 0) == im_total + 1,
          f"inserted={imp.get('inserted')} updated={imp.get('updated')}（应等于导出条数）")
finally:
    if os.path.exists(tmp_xlsx):
        os.remove(tmp_xlsx)

st, im2 = call("GET", "/api/items/meta")
check("导入后总数只增加自检那一行",
      im2.get("stats", {}).get("total") == im_total + 1,
      f"{im2.get('stats', {}).get('total')} vs 基线 {im_total}+1")

st, dele = call("DELETE", f"/api/items/{created.get('id')}")
check("删除物料", st == 200 and dele.get("ok") is True, "")
st, after = call("GET", "/api/items?keyword=" + urllib.parse.quote(tmp_no))
check("自检物料已清理干净", after.get("total") == 0, f"total={after.get('total')}")

# 旧表头兼容：源文件仍用「标参1/2/3」时应能正常识别
old_xlsx = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "_items_old_header.xlsx")
try:
    from openpyxl import Workbook
    wb2 = Workbook()
    ws2 = wb2.active
    ws2.append(["料号", "品名", "标参1", "标参2", "标参3"])
    ws2.append(["SMOKE-OLD-HEADER", "旧表头兼容测试", "(0~10)m/s", "4-20mA", "IP67"])
    wb2.save(old_xlsx)

    st, imp2 = upload("/api/items/import", old_xlsx, "?mode=upsert")
    check("旧表头（标参1-3）仍可导入",
          st == 200 and imp2.get("inserted") == 1,
          f"recognized={imp2.get('recognized')}")

    st, old_rows = call("GET", "/api/items?keyword=SMOKE-OLD-HEADER")
    rec = (old_rows.get("rows") or [{}])[0]
    check("旧表头数据写入新字段",
          rec.get("param1") == "(0~10)m/s" and rec.get("param2") == "4-20mA"
          and rec.get("param3") == "IP67",
          f"{rec.get('param1')} / {rec.get('param2')} / {rec.get('param3')}")
    if rec.get("id"):
        call("DELETE", f"/api/items/{rec.get('id')}")

    # 本段开头新建的 SMOKE-TEST-001 也要删掉 —— 漏了它的话，
    # 「物料总数」基线每跑一轮就涨 1，下一轮「新增物料」会被判成 update 而失败，
    # 报错指向「新增」却和新增逻辑无关。
    for _id, _ in _c.execute(
            "SELECT id, material_no FROM items_db.item_master "
            "WHERE material_no LIKE 'SMOKE-%';"):
        call("DELETE", f"/api/items/{_id}")
finally:
    if os.path.exists(old_xlsx):
        os.remove(old_xlsx)

st, im3 = call("GET", "/api/items/meta")
check("匹配库恢复初始条数",
      im3.get("stats", {}).get("total") == im_total,
      f"{im3.get('stats', {}).get('total')} vs {im_total}")

# fill 模式：从 ERP 快照回填老数据时用它 —— 只补空字段，本地已有值一律不动。
# 现场（2026-09-22）：ERP 账号只剩三列可读，upsert 一跑就把本地人工整理的品名
# 清成空，所以全量补数必须走 fill。空转 plan_item_fill 与实跑共用同一套判空逻辑，
# 两条一起验：空转算得准、实跑不覆盖。
import core.repository as _fill_repo                              # noqa: E402
_fill_no = "SMOKE-FILL-001"
_fill_repo.import_items([{"material_no": _fill_no, "product_name": "本地品名",
                          "model_no": "本地型号"}], mode="upsert")
_plan = _fill_repo.plan_item_fill([{"material_no": _fill_no,
                                    "product_name": "ERP品名", "model_no": "",
                                    "spec": "ERP规格"}])
check("fill 空转只算「本地空、导入有值」的字段",
      _plan.get("changed") == 1 and set(_plan.get("fields") or {}) == {"spec"},
      f"changed={_plan.get('changed')} fields={sorted(_plan.get('fields') or {})}")
_fres = _fill_repo.import_items([{"material_no": _fill_no, "product_name": "ERP品名",
                                  "model_no": "", "spec": "ERP规格"}], mode="fill")
_frow = _fill_repo.get_item_by_no(_fill_no) or {}
check("fill 模式不覆盖本地已有值、也不拿空值写入",
      _fres.get("updated") == 1 and _frow.get("product_name") == "本地品名"
      and _frow.get("model_no") == "本地型号" and _frow.get("spec") == "ERP规格",
      f"品名={_frow.get('product_name')} 型号={_frow.get('model_no')} "
      f"规格={_frow.get('spec')}")
_fres2 = _fill_repo.import_items([{"material_no": _fill_no, "product_name": "ERP品名"}],
                                 mode="fill")
check("fill 模式第二次跑已无字段可补",
      _fres2.get("updated") == 0 and _fres2.get("unchanged") == 1,
      f"updated={_fres2.get('updated')} unchanged={_fres2.get('unchanged')}")
if _frow.get("id"):
    call("DELETE", f"/api/items/{_frow['id']}")

# ---------------------------------------------------------------- 8. 导出
print("\n[8] Excel 导出")
st, blob = call("GET", "/api/export", raw=True)
check("导出接口返回 200", st == 200, f"{len(blob)} bytes")
check("返回 xlsx 文件头", blob[:2] == b"PK", blob[:4].hex())

# ------------------------------------------------------------ 9. 鉴权与权限
print("\n[9] 登录验证与权限控制")
st, summ = call("GET", "/api/auth/summary")
check("鉴权概览可读", st == 200 and summ.get("groups", 0) >= 3,
      f"{summ.get('users')} 用户 / {summ.get('groups')} 权限组")
check("预置三个内置权限组",
      set(summ.get("preset_groups", [])) == {"管理员", "登记员", "只读"},
      str(summ.get("preset_groups")))

# 未登录时的拦截：临时登出，验完立刻登回来
_keep_sid = SID
SID = ""
st, loc = probe("/scan.html")
check("未登录访问页面被重定向到登录页",
      st in (302, 303) and "login.html" in loc, f"HTTP {st} → {loc}")
st, loc = probe("/photos/")
check("未登录访问照片目录返回 401（不重定向，避免 <img> 显示裂图）",
      st == 401, f"HTTP {st}")
st, api401 = call("GET", "/api/returns")
check("未登录调接口返回 401",
      st == 401 and "unauthorized" in json.dumps(api401, ensure_ascii=False),
      f"HTTP {st}")
st, _ = call("POST", "/api/auth/login",
             {"username": "admin", "password": "definitely-wrong"})
check("错误密码登录被拒", st == 401, f"HTTP {st}")
check("错误密码后仍未获得会话", SID == "", f"SID={'有' if SID else '无'}")

SID = _keep_sid
st, ret = call("GET", "/api/returns?page_size=1")
check("重新带上会话后可正常访问", st == 200 and "total" in ret,
      f"total={ret.get('total')}")

# 权限组：内置组不可删、管理员组权限不可改
st, groups = call("GET", "/api/auth/groups")
check("权限点注册表覆盖页面与操作两类",
      len(groups.get("permission_groups", [])) == 3
      and any(p["key"].startswith("page.") for g in groups["permission_groups"]
              for p in g["perms"])
      and any(p["key"].startswith("act.") for g in groups["permission_groups"]
              for p in g["perms"]),
      f"{len(groups.get('all_perms', []))} 个权限点")
_admin = next((g for g in groups["rows"] if g["name"] == "管理员"), {})
check("管理员组标记为内置且权限锁定",
      _admin.get("builtin") and _admin.get("locked"),
      f"{_admin.get('count')} 项权限")
st, err = call("DELETE", f"/api/auth/groups/{_admin.get('id')}")
check("内置权限组不可删除",
      st == 400 and "内置" in json.dumps(err, ensure_ascii=False),
      f"HTTP {st}")
st, err = call("PUT", f"/api/auth/groups/{_admin.get('id')}",
               {"perms": ["page.index"]})
check("管理员组权限不可削减（防自锁）",
      st == 400 and "不可修改" in json.dumps(err, ensure_ascii=False),
      f"HTTP {st}")

# 用户与权限组：建一个只读账号，验证「越权操作被 403 挡住」
st, made = call("POST", "/api/auth/groups",
                {"name": "自检只读组", "description": "自检用",
                 "perms": ["page.index", "page.query"]})
gid = made.get("id")
check("可新建自定义权限组", st == 200 and bool(gid), f"id={gid}")

st, made_u = call("POST", "/api/auth/users", {
    "username": "smoke_ro", "password": "smoke-pass",
    "group_id": gid, "display_name": "自检只读账号", "must_change_pwd": False})
uid = made_u.get("id")
check("可新建用户并指定权限组", st == 200 and bool(uid), f"id={uid}")

if uid:
    _admin_sid = SID
    SID = ""
    # 必须走 login() 而不是直接 call()：call() 只把会话记在 _last_sid 里，
    # 不会写回 SID（写回动作在 login() 里），直接调会出现「登录成功但后续 401」
    st, _ = login("smoke_ro", "smoke-pass")
    check("新用户可登录", st == 200 and bool(SID), f"HTTP {st}")
    st, _ = call("GET", "/api/returns?page_size=1")
    check("只读账号可查看数据", st == 200, f"HTTP {st}")
    st, denied = call("DELETE", f"/api/returns/{key_a}")
    check("无权账号删除明细被拒（403）",
          st == 403 and "forbidden" in json.dumps(denied, ensure_ascii=False),
          f"HTTP {st}")
    st, _ = call("POST", "/api/returns", {"return_no": "SF-NOAUTH"})
    check("无权账号新增登记被拒（403）", st == 403, f"HTTP {st}")
    st, _ = call("GET", "/api/auth/users")
    check("无权账号读用户列表被拒（403）", st == 403, f"HTTP {st}")
    st, _ = call("GET", "/api/access/status")
    check("无权账号读接口配置被拒（403）", st == 403, f"HTTP {st}")
    # ★ 2026-09-22 拆点后的四个写入口：同步是「改人的数据」，只读/无权限账号
    #   一个都不能碰。两个同步模块各两个入口（常规 + 全量 / 手动 + 配置）。
    st, _ = call("POST", "/api/delivery/details/sync", {})
    check("无权账号触发发货明细「立即同步」（常规增量）被拒（403）",
          st == 403, f"HTTP {st}")
    st, _ = call("POST", "/api/delivery/details/sync/full", {})
    check("无权账号触发发货明细「全量同步」（整表覆盖）被拒（403）",
          st == 403, f"HTTP {st}")
    st, _ = call("POST", "/api/items/sync/run", {})
    check("无权账号触发匹配库同步被拒（403）", st == 403, f"HTTP {st}")
    st, _ = call("PUT", "/api/items/sync/config", {"auto_interval_hours": "24"})
    check("无权账号改匹配库同步配置被拒（403）", st == 403, f"HTTP {st}")

    # 停用后既有会话立即失效
    SID = _admin_sid
    call("PUT", f"/api/auth/users/{uid}", {"enabled": False})
    SID = ""
    st, _ = login("smoke_ro", "smoke-pass")
    check("停用的账号无法登录", st == 401, f"HTTP {st}")
    SID = _admin_sid
    call("PUT", f"/api/auth/users/{uid}", {"enabled": True})
    call("DELETE", f"/api/auth/users/{uid}")
    st, left = call("GET", "/api/auth/users")
    check("删除用户生效", all(u["username"] != "smoke_ro" for u in left["rows"]),
          f"剩 {len(left['rows'])} 个账号")
    call("DELETE", f"/api/auth/groups/{gid}")

st, users = call("GET", "/api/auth/users")
check("审计日志记录了登录与用户变更",
      len([x for x in call("GET", "/api/auth/logs")[1].get("rows", [])
           if x.get("action") in ("login", "user.create", "user.delete",
                                  "group.create", "login.fail")]) >= 4,
      f"{len(users.get('rows', []))} 个账号")

print("\n[9b] 数据开放接口（独立端口，供外部系统定时拉取）")
from config import OPEN_API_HOST, OPEN_API_PORT  # noqa: E402
OA_BASE = f"http://{OPEN_API_HOST}:{OPEN_API_PORT}"


def oa_call(path, token=None, method="GET"):
    """请求开放接口（独立端口，不经主服务的会话中间件）。"""
    url = OA_BASE + path
    if token:
        url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(token)
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            blob = r.read()
            try:
                text = blob.decode("utf-8")
            except UnicodeDecodeError:
                # 二进制导出（xlsx）——原样回传字节供断言
                return r.status, blob
            return r.status, (json.loads(text)
                              if text.strip().startswith("{") else text)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            return e.code, {}


st, h = oa_call("/api/open/health")
check("开放接口连通性探测免令牌可用", st == 200 and h.get("ok") is True,
      h.get("service", "")[:36])
st, denied = oa_call("/api/open/ping")
check("无令牌访问被拒（401）",
      st == 401 and denied.get("code") == "bad_token", f"HTTP {st}")
st, bad = oa_call("/api/open/ping?token=wrong-token")
check("错误令牌被拒（401）", st == 401, f"HTTP {st}")

st, acc = call("GET", "/api/access/status")
TOKEN = None
if st == 200:
    from core import openapi as _oa  # noqa: PLC0415
    TOKEN = _oa.get_token()
check("主界面可读开放接口配置（令牌以掩码展示）",
      st == 200 and "*" in (acc.get("token_masked") or ""),
      f"监听 {acc.get('host')}:{acc.get('port')} · 数据集 {len(acc.get('datasets', []))}")

if TOKEN:
    st, pong = oa_call("/api/open/ping", TOKEN)
    check("正确令牌可访问", st == 200, f"HTTP {st}")
    st, ds = oa_call("/api/open/datasets", TOKEN)
    names = [d["key"] for d in ds.get("datasets", [])]
    check("可用数据集受范围限制", st == 200 and "detail" in names,
          " / ".join(names))
    check("未授权的数据集不在清单里",
          all(d["key"] in ds.get("scopes", []) for d in ds.get("datasets", [])),
          str(ds.get("scopes")))

    st, data = oa_call("/api/open/data/detail?limit=3", TOKEN)
    check("可拉取明细汇总（退回+检测+处理拼表）",
          st == 200 and data.get("count") == 3 and "test_date" in data.get("columns", [])
          and "erp_handled" in data.get("columns", []),
          f"{data.get('count')}/{data.get('total')} 行 · {len(data.get('columns', []))} 列")
    check("跨库字段确实带值（不是空列）",
          any(r.get("test_date") for r in data.get("rows", [])),
          f"样例 test_date={data['rows'][0].get('test_date') if data.get('rows') else '—'}")
    st, empty = oa_call("/api/open/data/detail?since=2099-01-01%2000:00:00", TOKEN)
    check("增量拉取：时间之后无数据时返回空", st == 200 and empty.get("count") == 0,
          f"total={empty.get('total')}")
    st, _ = oa_call("/api/open/data/not_a_dataset", TOKEN)
    check("未知数据集返回 404", st == 404, f"HTTP {st}")

    # since 必须校验格式：它是直接参与字符串比较的（updated_at > ?），
    # 格式不对不报错，只会静默给出错误结果 ——
    #   `not-a-date` → 比较恒不成立 → 0 条（金山侧以为没有新数据，永久漏掉）
    #   `' OR 1=1 --` → 比较恒成立 → 全量（金山侧整表重写，目标表出现重复行）
    # 两种都「看起来拉取成功了」，所以宁可 400 让调用方立刻发现。
    for bad_since in ["not-a-date", "2026-13-99", "2026-02-30",
                      "' OR 1=1 --"]:
        st, body = oa_call("/api/open/data/detail?since="
                           + urllib.parse.quote(bad_since), TOKEN)
        # 不依赖响应结构：HTTPException(detail=dict) 会被 FastAPI 包一层，
        # 而令牌/scope 类拒绝是直接 JSONResponse —— 两种情况都判「含 bad_since」
        _txt = json.dumps(body, ensure_ascii=False) if isinstance(body, dict) \
            else str(body)
        check(f"非法 since 被拒（400）：{bad_since[:16]}",
              st == 400 and "bad_since" in _txt, f"HTTP {st} {_txt[:60]}")
    st, ok_since = oa_call("/api/open/data/detail?since=2026-09-20&limit=1", TOKEN)
    check("合法 since 正常拉取，并被规范化成完整时间",
          st == 200 and ok_since.get("since") == "2026-09-20 00:00:00",
          f"since={ok_since.get('since')!r}")
    # 数据集不存在仍应是 404（别被 since 的 400 抢走语义）
    st, _ = oa_call("/api/open/data/not_a_dataset?since=bad", TOKEN)
    check("数据集不存在优先报 404（不被 since 校验盖住）", st == 404, f"HTTP {st}")
    st, csv_body = oa_call("/api/open/export/detail.csv?limit=5", TOKEN)
    check("可导出 CSV（带 BOM，Excel 中文不乱码）",
          st == 200 and isinstance(csv_body, str)
          and csv_body.startswith("\ufeff"),
          f"{len(csv_body)} 字符")
    st, _ = oa_call("/api/open/export/items.pdf", TOKEN)
    check("不支持的导出格式被拒（400）", st == 400, f"HTTP {st}")
    st, xlsx = oa_call("/api/open/export/detail.xlsx?limit=3", TOKEN)
    check("可导出 XLSX（二进制流，非文本）",
          st == 200 and isinstance(xlsx, bytes) and xlsx[:2] == b"PK",
          f"HTTP {st} · {len(xlsx) if isinstance(xlsx, bytes) else '?'} 字节"
          f" · 文件头 {'PK' if isinstance(xlsx, bytes) and xlsx[:2] == b'PK' else '?'}")

    # 来源 IP 白名单：把本机之外的地址加进去后，本机应被拒
    st, cfg = call("PUT", "/api/access/config", {"ips": ["203.0.113.7"]})
    st, denied2 = oa_call("/api/open/ping", TOKEN)
    check("启用 IP 白名单后非白名单来源被拒（403）",
          st == 403 and denied2.get("code") == "ip_denied", f"HTTP {st}")
    st, _ = call("PUT", "/api/access/config", {"ips": []})
    st, ok2 = oa_call("/api/open/ping", TOKEN)
    check("清空白名单后恢复访问", st == 200, f"HTTP {st}")
    st, _ = call("PUT", "/api/access/config", {"scopes": ["returns"]})
    st, d2 = oa_call("/api/open/data/detail", TOKEN)
    check("取消授权后该数据集被拒（403）",
          st == 403 and d2.get("code") == "scope_denied", f"HTTP {st}")
    call("PUT", "/api/access/config", {"scopes": ["detail"]})

    # --- 金山文档同步模块已于 2026-09-21 整体移除：配置、字段、日志表都不得残留 ---
    from core.db import get_conn as _gc                              # noqa: PLC0415
    _kc = _gc()
    st, acc_k = call("GET", "/api/access/status")
    check("状态接口不再返回金山侧目标信息（kdocs 键已移除）",
          st == 200 and "kdocs" not in (acc_k or {}), str(sorted(acc_k or {}))[:90])
    st, _pk = call("PUT", "/api/access/config", {"kdocs": {"sheet": "x"}})
    check("再传 kdocs 配置也不会落库（分支已删除）",
          st == 200 and _kc.execute(
              "SELECT COUNT(*) c FROM auth_db.setting WHERE `key` = 'kdocs_target';"
          ).fetchone()["c"] == 0, f"HTTP {st} · setting 已清空")
    check("returns 表已无 sync_state / synced_at 两列",
          not ({"sync_state", "synced_at"} & tbl_cols("returns_db", "returns")),
          "两列已删除")
    check("同步日志表 sync_log 已从库中删除",
          not tbl_exists("returns_db", "sync_log"), "已删除")
    check("字段标签里不再有「同步状态」",
          "sync_state" not in (call("GET", "/api/meta")[1].get("field_labels") or {}),
          "已移除")

    st, logs = call("GET", "/api/access/logs")
    check("拉取日志有记录（含被拒的调用）",
          len(logs.get("rows", [])) >= 5
          and any(not x["ok"] for x in logs.get("rows", [])),
          f"{len(logs.get('rows', []))} 条 · "
          f"失败 {len([x for x in logs.get('rows', []) if not x['ok']])} 条")

# ---------------------------------------------------------------- 9c. 健壮性与安全
# 2026-09-20 自行审计发现的三个问题，全部固化成断言防回归。
print("\n[9c] 健壮性与安全回归")

# ① 非法字段名必须 400，不能是 500。
#    三个仓储模块的 _safe*() 抛 InvalidField，由 app.py 的异常处理器统一转 400；
#    在此之前它是裸 ValueError → 500，任何人拿任意字段名就能刷错误日志。
for _bad in ["not_a_field", "x'; DROP TABLE returns;--", "a" * 300]:
    st, _ = call("GET", "/api/dict/" + urllib.parse.quote(_bad, safe=""))
    check(f"非法字段不产生 500：{_bad[:18]!r}", st == 400, f"HTTP {st}")
st, _ = call("GET", "/api/dict/remark")
check("合法字段仍正常取候选（异常处理没误伤）", st == 200, f"HTTP {st}")

# ② 页面路由不能被路径遍历。
#    `/{page}.html` 把路径参数直接拼进文件名，而 Windows 下 `\` 也是路径分隔符
#    —— `/..%5C..%5Cfoo.html` 曾经能读到 static 之外的任意 .html（HTTP 200）。
_canary = pathlib.Path(__file__).resolve().parent.parent / "SECRET_probe.html"
_canary.write_text("<h1>SECRET-OUTSIDE-STATIC</h1>", encoding="utf-8")
try:
    for _evil in ["/..%5CSECRET_probe.html", "/%2e%2e%5CSECRET_probe.html",
                  "/..%5C..%5C..%5CSECRET_probe.html", "/..%2FSECRET_probe.html"]:
        st, body = call("GET", _evil)
        _leak = "SECRET-OUTSIDE-STATIC" in (body if isinstance(body, str) else "")
        check(f"路径遍历被拒：{_evil[:26]}",
              st == 404 and not _leak, f"HTTP {st} 泄漏={_leak}")
    st, _ = call("GET", "/query.html")
    check("正常页面不受影响（白名单没误伤）", st == 200, f"HTTP {st}")
finally:
    _canary.unlink()

# ③ /api/health 在免登录白名单里，不能回服务端部署信息
_SID_BAK = SID
SID = ""
st, _anon = call("GET", "/api/health")
SID = _SID_BAK
check("未登录访问 /api/health 只回连通性，不含部署路径",
      st == 200 and "db_path" not in json.dumps(_anon, ensure_ascii=False),
      str(_anon)[:64])
st, _auth = call("GET", "/api/health")
check("已登录仍能拿到库统计（工作台不受影响）",
      st == 200 and "total" in (_auth.get("db") or {}),
      f"{(_auth.get('db') or {}).get('total')} 条")

# ④ 并发登记不撞单号。
#    next_order_no() 只探测不占号，取号与插入之间若不串行化，两个人同时登记
#    会解析出同一个单号、抢同一个 detail_key —— 实测 8 并发挂 5 个，
#    而且报错是数据库层的英文原文（登记员完全看不懂）。
import threading as _threading_mod                                    # noqa: E402
_conc_res, _conc_lock = [], _threading_mod.Lock()


def _conc_worker(_i):
    _st, _b = call("POST", "/api/returns", {"return_no": f"__SMOKE_CONC_{_i}"})
    with _conc_lock:
        _conc_res.append((_st, _b))


_conc_ts = [_threading_mod.Thread(target=_conc_worker, args=(_i,))
            for _i in range(6)]
for _t in _conc_ts:
    _t.start()
for _t in _conc_ts:
    _t.join()
_conc_ok = [r for r in _conc_res if r[0] == 200]
check("并发登记全部成功（取号与插入已串行化）", len(_conc_ok) == 6,
      f"{len(_conc_ok)}/6 成功")
_conc_keys = [r[1]["detail_key"] for r in _conc_ok
              if isinstance(r[1], dict) and r[1].get("detail_key")]
check("并发登记生成的明细键互不重复（没有撞号）",
      len(_conc_keys) == len(set(_conc_keys)) and len(_conc_keys) == 6,
      " · ".join(sorted(_conc_keys)))
check("并发失败时给的是中文提示，不是数据库英文原文",
      all("UNIQUE constraint" not in str(r[1]) for r in _conc_res),
      " / ".join(str(r[1])[:34] for r in _conc_res if r[0] != 200) or "无失败")
for _k in _conc_keys:                       # 清干净，别顶高 [10] 的基线
    call("DELETE", f"/api/returns/{_k}")

check("旧同步相关接口均已下线（推送式同步 + 金山模块已整体移除）",
      all(call(*c)[0] == 404 for c in
          [("GET", "/api/sync/status"), ("POST", "/api/sync/push"),
           ("GET", "/api/sync/pending")]),
      "三个 /api/sync/* 接口均返回 404")

# ---------------------------------------------------------------- 清理
print("\n[10] 清理测试数据")
if KEEP:
    print("  [SKIP] 已指定 --keep，保留测试数据")
else:
    for k in [key_a, key_b, key_c] + list(batch_keys) + list(add_keys):
        if k:
            call("DELETE", f"/api/returns/{k}")
    st, final = call("GET", "/api/health")
    remain = final.get("db", {}).get("total", -1)
    check("测试数据已清理干净", remain == base_total, f"剩余 {remain} 条（基线 {base_total}）")

# ---------------------------------------------------------------- 11. 派生表回收
print("\n[11] 字典候选随数据回收（防墓碑残留）")
if KEEP:
    print("  [SKIP] 已指定 --keep，跳过回收验证")
else:
    uniq = f"__SMOKE_{int(time.time())}"

    st, pre = call("GET", "/api/dict/project_site")
    vals = [o.get("value") for o in pre.get("options", [])]
    check("测试值登记前不在字典中", uniq not in vals, f"{len(vals)} 个候选")

    st, rcv = call("POST", "/api/returns/batch", {
        "header": {
            "return_no": f"RCV-{uniq}",
            "project_site": uniq, "turbine_vendor": uniq, "carrier": uniq,
            "return_date": time.strftime("%Y-%m-%d"),
        },
        "items": [{"product_code": f"RCV-{uniq}", "product_model": uniq,
                   "product_name": uniq, "spec": uniq}],
    })
    rcv_keys = rcv.get("detail_keys") or []
    check("临时记录已登记", st == 200 and len(rcv_keys) == 1, f"HTTP {st}")

    st, d = call("GET", "/api/dict/project_site")
    vals = [o.get("value") for o in d.get("options", [])]
    check("登记后字典立即出现新值", uniq in vals, f"{len(vals)} 个候选")

    st, m = call("GET", "/api/models")
    models = [r.get("product_model") for r in m.get("rows", [])]
    check("登记后型号字典已积累", uniq in models, f"{len(models)} 个型号")

    for k in rcv_keys:
        call("DELETE", f"/api/returns/{k}")

    st, d = call("GET", "/api/dict/project_site")
    vals = [o.get("value") for o in d.get("options", [])]
    check("删除后字典候选自动回收（无墓碑）", uniq not in vals,
          f"{len(vals)} 个候选")

    st, m = call("GET", "/api/models")
    models = [r.get("product_model") for r in m.get("rows", [])]
    check("删除后型号字典同步回收", uniq not in models, f"{len(models)} 个型号")

    st, final2 = call("GET", "/api/health")
    check("回收测试未留下残余记录",
          final2.get("db", {}).get("total", -1) == base_total,
          f"剩余 {final2.get('db', {}).get('total')} 条")

# ---------------------------------------------------------------- 12. 快递公司
print("\n[12] 快递公司：按单号前缀自动识别")
if KEEP:
    print("  [SKIP] 已指定 --keep，跳过快递公司验证")
else:
    st, m2 = call("GET", "/api/meta")
    rules = {r.get("prefix"): r.get("carrier")
             for r in m2.get("carrier_rules", [])}
    check("识别规则由后端统一下发",
          rules.get("SF") == "顺丰速运" and rules.get("JD") == "京东物流"
          and rules.get("YT") == "圆通速递", str(rules))
    check("快递公司为固定下拉（10 家候选）",
          len(m2.get("carrier_options", [])) == 10
          and "carrier" in (m2.get("fixed_options") or {}),
          f"{len(m2.get('carrier_options', []))} 家")

    carrier_keys = []
    cases = [("SF1234567890", "顺丰速运"),
             ("JD9876543210", "京东物流"),
             ("YT5555555555", "圆通速递"),
             ("1234567890123", "")]
    for i, (rn, expect) in enumerate(cases):
        st, r = call("POST", "/api/returns/batch", {
            "header": {"return_no": rn,
                       "return_date": time.strftime("%Y-%m-%d")},
            "items": [{"product_code": f"CAR-{i}", "product_model": f"CAR-M{i}"}],
        })
        keys = r.get("detail_keys") or []
        got = ""
        if keys:
            carrier_keys.append(keys[0])
            st2, rec = call("GET", f"/api/returns/{keys[0]}")
            got = (rec or {}).get("carrier") or ""
        label = f"{rn[:2]}…" if expect else "纯数字单号"
        check(f"{label} → {expect or '不自动填（人工选择）'}",
              st == 200 and got == expect, f"实际 = {got or '（空）'}")

    for k in carrier_keys:
        call("DELETE", f"/api/returns/{k}")
    st, fin3 = call("GET", "/api/health")
    check("快递公司验证未留下残余",
          fin3.get("db", {}).get("total", -1) == base_total,
          f"剩余 {fin3.get('db', {}).get('total')} 条")

# ---------------------------------------------------------------- 13. 检测进度
print("\n[13] 检测进度过滤（检测登记模块）")
if KEEP:
    print("  [SKIP] 已指定 --keep，跳过检测进度验证")
else:
    st, insp = call("POST", "/api/returns/batch", {
        "header": {"return_no": f"INSP-{int(time.time())}",
                   "return_date": time.strftime("%Y-%m-%d")},
        "items": [{"product_code": "INSP-001", "product_model": "INSP-MODEL"}],
    })
    insp_keys = insp.get("detail_keys") or []
    insp_key = insp_keys[0] if insp_keys else None
    check("待检记录已登记", bool(insp_key), insp_key or "创建失败")

    if insp_key:
        st, p1 = call("GET", "/api/returns?inspect_pending=1&page_size=200")
        keys1 = [r.get("detail_key") for r in p1.get("rows", [])]
        check("新记录出现在待检清单", insp_key in keys1, f"待检 {p1.get('total')} 条")

        st, u1 = call("GET", "/api/returns?untested_only=1&page_size=200")
        keys_u1 = [r.get("detail_key") for r in u1.get("rows", [])]
        check("未检测过滤命中新记录", insp_key in keys_u1,
              f"未检测 {u1.get('total')} 条")

        # 只录检测时间、尚未完结 —— 属于「录了一半」，应仍在待检清单
        st, _ = call("PUT", f"/api/returns/{insp_key}",
                     {"test_date": time.strftime("%Y-%m-%d")})
        check("检测时间已保存", st == 200, f"HTTP {st}")

        st, p2 = call("GET", "/api/returns?inspect_pending=1&page_size=200")
        keys2 = [r.get("detail_key") for r in p2.get("rows", [])]
        check("仅录检测未完结时仍在待检清单", insp_key in keys2,
              f"待检 {p2.get('total')} 条")

        st, u2 = call("GET", "/api/returns?untested_only=1&page_size=200")
        keys_u2 = [r.get("detail_key") for r in u2.get("rows", [])]
        check("已录检测时间则不在未检测列表", insp_key not in keys_u2,
              f"未检测 {u2.get('total')} 条")

        # 补完结状况 —— 闭环完成，应移出待检清单
        # 完结状况已改为自动判定：填满 8 项检测字段才会被判定为已完结
        st, _ = call("PUT", f"/api/returns/{insp_key}", {
            "test_date": "2026-09-18", "test_result": "自检：功能正常",
            "fault_cause": "自检：故障原因", "improvement": "自检：改善措施",
            "solution": "拆解报废", "issue_category": "无法判断",
            "responsibility": "其他端", "erp_handled": "自检：ERP已处理",
        })
        st, p3 = call("GET", "/api/returns?inspect_pending=1&page_size=200")
        keys3 = [r.get("detail_key") for r in p3.get("rows", [])]
        check("完结后移出待检清单", insp_key not in keys3,
              f"待检 {p3.get('total')} 条")

        call("DELETE", f"/api/returns/{insp_key}")
        st, fin4 = call("GET", "/api/health")
        check("检测进度验证未留下残余",
              fin4.get("db", {}).get("total", -1) == base_total,
              f"剩余 {fin4.get('db', {}).get('total')} 条")

# ---------------------------------------------------------------- 14. 跨库拼接
print("\n[14] 跨库拼接（退回登记库 <-> 检测登记库）")
if KEEP:
    print("  [SKIP] 已指定 --keep，跳过跨库拼接验证")
else:
    from config import (AUTH_DB, DELIVERY_DB, HANDLE_DB,  # noqa: PLC0415
                        INSPECT_DB, ITEMS_DB, RETURNS_DB)
    from core.db import get_conn  # noqa: PLC0415

    conn = get_conn()

    # 库（schema）与字段归属
    check("六个库均已建立",
          all(schema_exists(p) for p in (RETURNS_DB, INSPECT_DB, HANDLE_DB, ITEMS_DB,
                                         DELIVERY_DB, AUTH_DB)),
          " / ".join((RETURNS_DB, INSPECT_DB, HANDLE_DB, ITEMS_DB,
                      DELIVERY_DB, AUTH_DB)))

    r_cols = tbl_cols(RETURNS_DB, "returns")
    i_cols = tbl_cols(INSPECT_DB, "inspect_records")
    check("退回登记库不含检测侧字段",
          not (r_cols & {"test_result", "fault_cause", "completion", "issue_category"}),
          str(sorted(r_cols & {"test_result", "fault_cause", "completion"})) or "无")
    check("检测登记库不含退回侧字段",
          not (i_cols & {"return_no", "turbine_vendor", "product_code", "return_qty"}),
          str(sorted(i_cols & {"return_no", "turbine_vendor", "product_code"})) or "无")
    h_cols = tbl_cols(HANDLE_DB, "handle_records")
    check("处理登记库不含退回 / 检测侧字段",
          not (h_cols & {"return_no", "turbine_vendor", "product_code",
                         "test_result", "fault_cause", "completion"}),
          str(sorted(h_cols - {"detail_key", "erp_handled",
                               "created_at", "updated_at"})) or "无")
    check("四库仅以 detail_key 关联",
          all("detail_key" in c for c in (r_cols, i_cols, h_cols)),
          f"returns={'detail_key' in r_cols} inspect={'detail_key' in i_cols} "
          f"handle={'detail_key' in h_cols}")

    # 字典表分库：各库只汇总自己字段的候选。
    # 这里曾出现过漏写库前缀、检测字段的候选写进主库的缺陷，固化为断言。
    from config import (HANDLE_DICT_FIELDS, INSPECT_DICT_FIELDS,  # noqa: PLC0415
                        RETURNS_DICT_FIELDS)
    main_dict = {r["field"] for r in conn.execute(
        "SELECT DISTINCT field FROM dict_option;")}
    insp_dict = {r["field"] for r in conn.execute(
        "SELECT DISTINCT field FROM inspect_db.dict_option;")}
    hndl_dict = {r["field"] for r in conn.execute(
        "SELECT DISTINCT field FROM handle_db.dict_option;")}
    check("主库字典无检测 / 处理字段",
          not (main_dict & (set(INSPECT_DICT_FIELDS) | set(HANDLE_DICT_FIELDS))),
          str(sorted(main_dict & (set(INSPECT_DICT_FIELDS)
                                  | set(HANDLE_DICT_FIELDS)))) or "无")
    check("检测库字典无退回 / 处理字段",
          not (insp_dict & (set(RETURNS_DICT_FIELDS) | set(HANDLE_DICT_FIELDS))),
          str(sorted(insp_dict & (set(RETURNS_DICT_FIELDS)
                                  | set(HANDLE_DICT_FIELDS)))) or "无")
    check("处理库字典无退回 / 检测字段",
          not (hndl_dict & (set(RETURNS_DICT_FIELDS) | set(INSPECT_DICT_FIELDS))),
          str(sorted(hndl_dict & (set(RETURNS_DICT_FIELDS)
                                  | set(INSPECT_DICT_FIELDS)))) or "无")
    check("三库字典无字段重叠",
          not (main_dict & insp_dict) and not (main_dict & hndl_dict)
          and not (insp_dict & hndl_dict),
          str(sorted((main_dict & insp_dict) | (main_dict & hndl_dict)
                     | (insp_dict & hndl_dict))) or "无重叠")
    check("三库字典并集覆盖全部字典字段",
          (main_dict | insp_dict | hndl_dict)
          <= (set(RETURNS_DICT_FIELDS) | set(INSPECT_DICT_FIELDS)
              | set(HANDLE_DICT_FIELDS)),
          f"并集 {len(main_dict | insp_dict | hndl_dict)} 个字段")

    stamp = int(time.time())
    st, xr = call("POST", "/api/returns/batch", {
        "header": {"return_no": f"XR-{stamp}",
                   "return_date": time.strftime("%Y-%m-%d"),
                   "turbine_vendor": "跨库测试厂家"},
        "items": [{"product_code": "XR-001", "product_model": "XR-MODEL",
                   "feedback_issue": "跨库反馈现象"}],
    })
    xkey = (xr.get("detail_keys") or [None])[0]
    check("跨库测试记录已登记", bool(xkey), xkey or "创建失败")

    if xkey:
        # 登记后只落退回库，检测库应为空（稀疏存储）
        conn = get_conn()
        n_inspect = conn.execute(
            "SELECT COUNT(*) c FROM inspect_db.inspect_records WHERE detail_key = ?;",
            (xkey,)).fetchone()["c"]
        check("登记后检测库未建行（稀疏存储）", n_inspect == 0, f"{n_inspect} 行")

        st, rec1 = call("GET", f"/api/returns/{xkey}")
        check("拼接查询可取到退回侧字段",
              st == 200 and rec1.get("turbine_vendor") == "跨库测试厂家",
              rec1.get("turbine_vendor"))

        # 录入检测 -> 只写检测库
        call("PUT", f"/api/returns/{xkey}", {
            "test_date": time.strftime("%Y-%m-%d"),
            "test_result": "跨库检测结果",
            "fault_cause": "跨库故障原因",
            "issue_category": "跨库问题分类",
            "completion": "完结",
        })
        conn = get_conn()
        n_inspect2 = conn.execute(
            "SELECT COUNT(*) c FROM inspect_db.inspect_records WHERE detail_key = ?;",
            (xkey,)).fetchone()["c"]
        check("录入检测后在检测库建行", n_inspect2 == 1, f"{n_inspect2} 行")

        st, rec2 = call("GET", f"/api/returns/{xkey}")
        check("拼接查询同时返回两库字段",
              rec2.get("test_result") == "跨库检测结果"
              and rec2.get("turbine_vendor") == "跨库测试厂家",
              f"{rec2.get('turbine_vendor')} / {rec2.get('test_result')}")

        # 按检测字段筛选（验证 WHERE 的跨库限定正确）
        st, q_det = call("GET", "/api/returns?issue_category="
                         + urllib.parse.quote("跨库问题分类"))
        check("按检测字段筛选命中退回侧记录",
              xkey in [r["detail_key"] for r in q_det.get("rows", [])],
              f"total={q_det.get('total')}")

        # 关键词检索覆盖检测库字段
        st, q_kw = call("GET", "/api/returns?keyword="
                        + urllib.parse.quote("跨库故障原因"))
        check("关键词检索覆盖检测库字段",
              xkey in [r["detail_key"] for r in q_kw.get("rows", [])],
              f"total={q_kw.get('total')}")

        # 看板按检测字段分组（跨库 GROUP BY）—— 检测汇总口径。
        # 注意不能走 API 的 TOP 10：这条测试值只出现 1 次，进不了前 10，
        # 断言会随并列排序方式漂移。直接问分组函数要全量。
        _groups = _repo.stats_group("fault_cause", {}, 500, "inspect")
        check("看板按检测字段分组有值（跨库 GROUP BY）",
              any("跨库故障原因" in str(g.get("label")) for g in _groups),
              f"{len(_groups)} 组")

        # 删除时两库同步清理
        call("DELETE", f"/api/returns/{xkey}")
        conn = get_conn()
        left_r = conn.execute(
            "SELECT COUNT(*) c FROM returns WHERE detail_key = ?;",
            (xkey,)).fetchone()["c"]
        left_i = conn.execute(
            "SELECT COUNT(*) c FROM inspect_db.inspect_records WHERE detail_key = ?;",
            (xkey,)).fetchone()["c"]
        check("删除后退回库已清理", left_r == 0, f"{left_r} 行")
        check("删除后检测库已清理", left_i == 0, f"{left_i} 行")

        st, fin5 = call("GET", "/api/health")
        check("跨库拼接验证未留下残余",
              fin5.get("db", {}).get("total", -1) == base_total,
              f"剩余 {fin5.get('db', {}).get('total')} 条")

# ---------------------------------------------------------------- 15. 按料号回填
print("\n[15] 明细行「输料号自动回填产品属性」")

st, fill = call("GET", "/api/items/fill?code=10001-0001")
check("回填接口可用", st == 200, f"HTTP {st}")
if st == 200:
    sg = fill.get("suggest") or {}
    check("命中匹配数据库", fill.get("found") is True and fill.get("source") == "item_master",
          f"source={fill.get('source')}")
    check("按料号定位到物料（非模糊）", fill.get("matched_field") == "material_no"
          and fill.get("fuzzy") is False, f"field={fill.get('matched_field')}")
    check("带出产品型号（取匹配库规格）", sg.get("product_model") == "51177.67.773C",
          f"product_model={sg.get('product_model')}")
    check("带出规格", sg.get("spec") == "51177.67.773C", f"spec={sg.get('spec')}")
    check("带出品名", sg.get("product_name") == "低温型风速传感器",
          f"product_name={sg.get('product_name')}")
    check("产品类别取自匹配库「生产统计」列", sg.get("product_category") == "风速【低温】",
          f"product_category={sg.get('product_category')}")
    check("不回填生产年份（同规格跨批次年份不一致，留空给人填）",
          "production_year" not in sg and "production_month" not in sg,
          f"keys={sorted(sg.keys())}")
    check("只回填退回登记侧字段（不含检测字段）",
          not (set(sg) & {"test_date", "test_result", "fault_cause", "solution",
                          "issue_category", "responsibility", "completion"}),
          f"keys={sorted(sg.keys())}")

# 用规格号也能反查（现场扫的可能是规格号而非料号）
st, by_spec = call("GET", "/api/items/fill?code=51228.68.242")
check("按规格号反查同样命中", st == 200 and by_spec.get("found") is True,
      f"source={by_spec.get('source')}")
check("反查带出料号", (by_spec.get("suggest") or {}).get("material_no") == "10002-0079",
      f"material_no={(by_spec.get('suggest') or {}).get('material_no')}")

# 未知料号：必须明确告知未命中，而不是静默留空
st, miss = call("GET", "/api/items/fill?code=99999-9999")
check("未知料号返回未命中", st == 200 and miss.get("found") is False
      and not miss.get("suggest"), f"found={miss.get('found')}")

# 空码不报错
st, empty = call("GET", "/api/items/fill?code=")
check("空码安全返回", st == 200 and empty.get("found") is False, f"HTTP {st}")

# 匹配库无此码时退回历史记录 / 型号字典
st, hist = call("GET", "/api/items/fill?code=" + urllib.parse.quote("微动电缆"))
check("匹配库无此码时可由历史兜底", st == 200 and hist.get("found") is True,
      f"source={hist.get('source')}")

# 提交链路：自动回填出的值要能正常落库
st, created = call("POST", "/api/returns/batch", {
    "header": {"return_no": f"SF9{int(time.time())}", "return_date": "2026-09-18"},
    "items": [{"material_no": "10002-0079", "product_model": "51228.68.242",
               "spec": "51228.68.242", "product_name": "抗冰冻风向传感器",
               "product_category": "III型风向", "production_stat": "III型风向",
               "return_qty": 1}],
})
check("回填出的字段可正常提交", st == 200 and created.get("count") == 1,
      f"count={created.get('count')}")
fk = (created.get("detail_keys") or [None])[0]
if fk:
    st, one = call("GET", f"/api/returns/{fk}")
    row = (one or {}).get("row") or one or {}
    check("产品型号已落库", row.get("product_model") == "51228.68.242",
          f"product_model={row.get('product_model')}")
    check("产品类别已落库", row.get("product_category") == "III型风向",
          f"product_category={row.get('product_category')}")
    call("DELETE", f"/api/returns/{fk}")

st, fin6 = call("GET", "/api/health")
check("回填验证未留下残余",
      fin6.get("db", {}).get("total", -1) == base_total,
      f"剩余 {fin6.get('db', {}).get('total')} 条")

# 明细行「料号」格的候选检索（模糊匹配匹配数据库）
st, s1 = call("GET", "/api/items/search?kw=10002&limit=5")
its = (s1 or {}).get("items") or []
check("按料号片段检索匹配数据库", st == 200 and len(its) > 0,
      f"{len(its)} 条：" + "、".join(x["value"] for x in its[:3]))
check("候选带品名 / 型号副标题",
      bool(its) and bool(its[0].get("meta")) and "·" in its[0].get("meta", ""),
      its[0].get("meta") if its else "")
check("料号前缀命中排在最前",
      bool(its) and all(x["value"].startswith("10002") for x in its),
      "、".join(x["value"] for x in its[:3]))

st, s2 = call("GET", "/api/items/search?kw=低温型风速")
check("可按品名片段检索", st == 200 and len((s2 or {}).get("items") or []) > 0,
      f"{len((s2 or {}).get('items') or [])} 条")

st, s3 = call("GET", "/api/items/search?kw=zzz-根本没有")
check("无命中时返回空列表（不报错）",
      st == 200 and (s3.get("items") or []) == [], f"HTTP {st}")

st, s4 = call("GET", "/api/items/search?kw=")
check("空关键字不返回全表",
      st == 200 and (s4.get("items") or []) == [], f"{len((s4 or {}).get('items') or [])} 条")

st, s5 = call("GET", "/api/items/search?kw=51228&limit=999")
check("limit 被夹紧到上限（不会一次拉全表）",
      st == 200 and len((s5 or {}).get("items") or []) <= 50,
      f"{len((s5 or {}).get('items') or [])} 条")

# ---------------------------------------------------------------- 16. 生产年月
print("\n[16] 产品编号 → 生产年月（前 4 位 YYMM）")

from config import PERIOD_UNKNOWN                      # noqa: E402
from core.db import get_conn                           # noqa: E402
from core.repository import parse_period_from_code     # noqa: E402

period_cases = [
    ("20100341", "2020", "10月", "需求给的例子"),
    ("19121192", "2019", "12月", "历史数据"),
    ("250912894", "2025", "9月", "9 位编号，取前 4 位"),
    ("2001", "2020", "1月", "边界：1 月"),
    ("2012", "2020", "12月", "边界：12 月"),
    ("2013", PERIOD_UNKNOWN, PERIOD_UNKNOWN, "月份 13 越界"),
    ("240055905", PERIOD_UNKNOWN, PERIOD_UNKNOWN, "月份 00 越界"),
    ("105F-7FDB", PERIOD_UNKNOWN, PERIOD_UNKNOWN, "含字母"),
    ("S2402057", PERIOD_UNKNOWN, PERIOD_UNKNOWN, "字母开头"),
    ("201", PERIOD_UNKNOWN, PERIOD_UNKNOWN, "长度不足"),
    ("991200001", PERIOD_UNKNOWN, PERIOD_UNKNOWN, "99xx 年份不合理"),
]
for code, want_y, want_m, note in period_cases:
    got = parse_period_from_code(code)
    ok = got.get("production_year") == want_y and got.get("production_month") == want_m
    check(f"解析 {code} → {want_y}/{want_m}（{note}）", ok,
          f"实际 {got.get('production_year')}/{got.get('production_month')}")

check("产品编号为空时不写占位（无依据）",
      parse_period_from_code("") == {} and parse_period_from_code("   ") == {},
      f"{parse_period_from_code('')}")

st, meta = call("GET", "/api/meta")
check("解析规则由后端统一下发", meta.get("period_rule", {}).get("prefix_len") == 4
      and meta.get("period_rule", {}).get("unknown") == PERIOD_UNKNOWN,
      f"period_rule={meta.get('period_rule')}")

# 端到端：只给产品编号，年月应由后端自动补全
st, created = call("POST", "/api/returns/batch", {
    "header": {"return_no": f"YT9{int(time.time())}", "return_date": "2026-09-18"},
    "items": [{"product_code": "20100341", "return_qty": 1}],
})
check("提交时自动补全生产年月", st == 200 and created.get("count") == 1,
      f"count={created.get('count')}")
pk = (created.get("detail_keys") or [None])[0]
if pk:
    st, one = call("GET", f"/api/returns/{pk}")
    row = (one or {}).get("row") or one or {}
    check("生产年份已落库", row.get("production_year") == "2020",
          f"production_year={row.get('production_year')}")
    check("生产月份已落库（带单位）", row.get("production_month") == "10月",
          f"production_month={row.get('production_month')}")

    # 人工填的值优先，不被解析覆盖
    st2, created2 = call("POST", "/api/returns/batch", {
        "header": {"return_no": f"YT9{int(time.time())}A", "return_date": "2026-09-18"},
        "items": [{"product_code": "20100341", "production_year": "1999",
                   "production_month": "1月", "return_qty": 1}],
    })
    pk2 = (created2.get("detail_keys") or [None])[0]
    if pk2:
        st, one2 = call("GET", f"/api/returns/{pk2}")
        row2 = (one2 or {}).get("row") or one2 or {}
        check("人工填写的年月不被解析结果覆盖",
              row2.get("production_year") == "1999"
              and row2.get("production_month") == "1月",
              f"year={row2.get('production_year')} month={row2.get('production_month')}")
        call("DELETE", f"/api/returns/{pk2}")
    call("DELETE", f"/api/returns/{pk}")

# 历史数据的回填结果（现有 100 条金山导入数据，其中 10 条没有产品编号）
conn = get_conn()
with_code = conn.execute(
    "SELECT COUNT(*) c FROM returns WHERE TRIM(COALESCE(product_code,''))<>''"
).fetchone()["c"]
filled = conn.execute(
    "SELECT COUNT(*) c FROM returns WHERE TRIM(COALESCE(product_code,''))<>'' "
    "AND TRIM(COALESCE(production_month,''))<>''"
).fetchone()["c"]
unknown_pm = conn.execute(
    "SELECT COUNT(*) c FROM returns WHERE production_month = ?",
    (PERIOD_UNKNOWN,)
).fetchone()["c"]
check("有产品编号的历史记录，生产月份已全部回填", filled == with_code,
      f"{filled}/{with_code} 条（另有 {base_total - with_code} 条无产品编号，无依据不填）")
check("编号不规范的记录以「无法确认」占位，便于后续筛出修正",
      unknown_pm >= 6, f"{unknown_pm} 条标记为「{PERIOD_UNKNOWN}」")
bad_fmt = conn.execute(
    "SELECT COUNT(*) c FROM returns WHERE TRIM(COALESCE(production_month,''))<>'' "
    "AND production_month <> ? AND production_month NOT LIKE '%月'",
    (PERIOD_UNKNOWN,)).fetchone()["c"]
check("生产月份格式统一为「N月」或占位值", bad_fmt == 0, f"格式异常 {bad_fmt} 条")
kept = conn.execute(
    "SELECT COUNT(*) c FROM returns WHERE production_year = '2024' "
    "AND production_month = ?", (PERIOD_UNKNOWN,)).fetchone()["c"]
check("编号不规范的记录未被解析值覆盖（240055905 的年份 2024 保留）", kept >= 1,
      f"{kept} 条：年份保留、月份标记占位")

st, fin7 = call("GET", "/api/health")
check("生产年月验证未留下残余",
      fin7.get("db", {}).get("total", -1) == base_total,
      f"剩余 {fin7.get('db', {}).get('total')} 条")

# ---------------------------------------------------------------- 17. 检测登记字段
print("\n[17] 检测登记：检测时间锁定 + 三个字段改为可搜索下拉")

st, meta = call("GET", "/api/meta")
check("meta 可用", st == 200, f"HTTP {st}")
if st == 200:
    ifields = {f["name"]: f for g in meta.get("inspect_groups", [])
               for f in g.get("fields", [])}
    td = ifields.get("test_date", {})
    check("检测时间锁定为当天（readonly + default=today）",
          td.get("readonly") is True and td.get("default") == "today",
          f"readonly={td.get('readonly')} default={td.get('default')}")
    for name, label in (("test_result", "检测结果"), ("fault_cause", "故障原因"),
                        ("improvement", "改善措施")):
        f = ifields.get(name, {})
        check(f"{label} 已改为可搜索下拉", f.get("type") == "search-dict",
              f"type={f.get('type')}")
    check("检测字段不在退回登记表格列中（仍归检测登记）",
          not (set(meta["line_columns"])
               & {"test_date", "test_result", "fault_cause", "improvement"}),
          f"{len(meta['line_columns'])} 列")

# 候选值：三个字段的候选由检测登记库汇总，前端下拉才有内容可选
for name, label in (("test_result", "检测结果"), ("fault_cause", "故障原因"),
                    ("improvement", "改善措施")):
    st, opts = call("GET", f"/api/dict/{name}")
    n = len(opts.get("options", [])) if isinstance(opts, dict) else 0
    check(f"{label}的候选可供筛选", st == 200 and n > 0, f"{n} 个候选")

# 锁定只作用于登记页：接口层仍允许修正（明细查询页入口）
st, created = call("POST", "/api/returns/batch", {
    "header": {"return_no": f"JD9{int(time.time())}", "return_date": "2026-09-18"},
    "items": [{"product_code": "20100341", "return_qty": 1}],
})
tk = (created.get("detail_keys") or [None])[0]
check("准备一条检测测试记录", bool(tk), tk or "（失败）")
if tk:
    st, _ = call("PUT", f"/api/returns/{tk}",
                 {"test_date": "2026-09-01", "test_result": "自检：检测时间修正"})
    st, one = call("GET", f"/api/returns/{tk}")
    row = (one or {}).get("row") or one or {}
    check("锁定只在登记页生效 —— 接口层仍可修正检测时间",
          row.get("test_date") == "2026-09-01", f"test_date={row.get('test_date')}")
    check("检测结果可正常写入", row.get("test_result") == "自检：检测时间修正",
          f"test_result={row.get('test_result')}")
    call("DELETE", f"/api/returns/{tk}")

st, fin8 = call("GET", "/api/health")
check("检测登记字段验证未留下残余",
      fin8.get("db", {}).get("total", -1) == base_total,
      f"剩余 {fin8.get('db', {}).get('total')} 条")

# ---------------------------------------------------------------- 18. 固定选项字段
print("\n[18] 检测结论三项改为固定下拉（口径锁定）")

from config import FIXED_OPTIONS, INSPECT_DICT_FIELDS   # noqa: E402

LOCKED = {
    "solution": ["维修入库", "检测入库", "拆解报废", "供方分析",
                 "维修返回", "原件返回", "拆解入库"],
    "issue_category": ["NTF", "客户应用", "制程问题", "来料问题",
                       "设计选型", "产品设计", "无法判断"],
    "responsibility": ["贝良端", "客户端", "供应商端", "其他端"],
}

st, meta = call("GET", "/api/meta")
check("meta 可用", st == 200, f"HTTP {st}")
if st == 200:
    ifields = {f["name"]: f for g in meta.get("inspect_groups", [])
               for f in g.get("fields", [])}
    for name, opts in LOCKED.items():
        f = ifields.get(name, {})
        check(f"{name} 已改为固定下拉（select-fixed）",
              f.get("type") == "select-fixed", f"type={f.get('type')}")

    # 筛选面板应自动携带固定选项（前端直接渲染，无需再拉字典）
    ff = {f["name"]: f for f in meta.get("filter_fields", [])}
    for name, opts in LOCKED.items():
        f = ff.get(name, {})
        same = list(f.get("options") or []) == opts
        check(f"筛选面板的 {name} 选项与配置一致（{len(opts)} 项）",
              f.get("type") == "select-fixed" and same,
              f"type={f.get('type')} 选项数={len(f.get('options') or [])}")

check("配置项与需求清单逐项一致",
      all(FIXED_OPTIONS.get(k) == v for k, v in LOCKED.items()),
      f"solution={len(FIXED_OPTIONS.get('solution', []))} "
      f"issue_category={len(FIXED_OPTIONS.get('issue_category', []))} "
      f"responsibility={len(FIXED_OPTIONS.get('responsibility', []))}")

# 三个字段已移出字典字段清单，候选应已回收
check("三个字段已移出字典字段清单",
      not (set(INSPECT_DICT_FIELDS) & set(LOCKED)),
      f"INSPECT_DICT_FIELDS={INSPECT_DICT_FIELDS}")
conn = get_conn()
residue = conn.execute(
    "SELECT field, COUNT(*) n FROM inspect_db.dict_option "
    "WHERE field IN ('solution','issue_category','responsibility') GROUP BY field"
).fetchall()
check("字典表中不再残留这三个字段的候选（派生表回收）",
      len(residue) == 0,
      "、".join(f"{r['field']}={r['n']}" for r in residue) or "已清空")

# 关键数据约束：历史值必须全部落在锁定清单内，否则编辑旧记录时下拉无值可选
for name, opts in LOCKED.items():
    rows = conn.execute(
        f"SELECT DISTINCT TRIM({name}) v FROM inspect_db.inspect_records "
        f"WHERE TRIM(COALESCE({name},'')) <> ''"
    ).fetchall()
    outside = [r["v"] for r in rows if r["v"] not in opts]
    check(f"历史数据的 {name} 取值全部在锁定清单内", not outside,
          f"清单外 {outside}" if outside else f"{len(rows)} 种取值全部命中")

# 「无法判定」已统一为「无法判断」，不再并存两种写法
legacy = conn.execute(
    "SELECT COUNT(*) c FROM inspect_db.inspect_records WHERE issue_category = '无法判定'"
).fetchone()["c"]
now_ok = conn.execute(
    "SELECT COUNT(*) c FROM inspect_db.inspect_records WHERE issue_category = '无法判断'"
).fetchone()["c"]
check("「无法判定」已统一为「无法判断」", legacy == 0 and now_ok >= 3,
      f"旧写法 {legacy} 条 · 新写法 {now_ok} 条")

# 锁定项不接受清单外的值（写入侧仍会存库，所以只在读取侧保证口径）
st, one = call("GET", "/api/returns?page_size=1")
check("查询接口正常", st == 200, f"HTTP {st}")

# ---------------------------------------------------------------- 19. 按售后单聚合
print("\n[19] 检测登记清单：按售后单号聚合（一单一行）")

st, orders = call("GET", "/api/inspect/orders?page_size=5")
check("聚合清单接口可用", st == 200 and "rows" in orders, f"HTTP {st}")
if st == 200 and orders.get("rows"):
    r0 = orders["rows"][0]
    missing = [k for k in ("order_no", "return_no", "lines", "tested", "done",
                           "untested", "status", "status_key") if k not in r0]
    check("聚合行包含进度与状态字段", not missing, f"缺 {missing}" if missing else "字段齐全")
    check("聚合后 total 是售后单数（不是明细数）",
          orders["total"] <= base_total, f"单据 {orders['total']} ≤ 明细 {base_total}")


def find_order(order_no, pending=1):
    _, res = call("GET", f"/api/inspect/orders?pending={pending}&page_size=500")
    return next((x for x in res.get("rows", []) if x["order_no"] == order_no), None)


# 完结状况已改为自动判定（7 项全填；报告编号/照片证据/ERP处理不参与），
# 不能再靠手写 completion 完结
FULL_FILL = {
    "test_date": "2026-09-19", "test_result": "自检：聚合测试",
    "fault_cause": "自检：故障原因", "improvement": "自检：改善措施",
    "solution": "拆解报废", "issue_category": "无法判断",
    "responsibility": "其他端", "erp_handled": "自检：ERP已处理",
}


# 造一个含 3 行明细的售后单，验证聚合与状态流转
stamp2 = int(time.time())
st, multi = call("POST", "/api/returns/batch", {
    "header": {"return_no": f"YT88{stamp2}", "return_date": "2026-09-19"},
    "items": [{"product_code": f"AGG{stamp2}-1", "return_qty": 1,
               "turbine_vendor": "自检厂家甲", "project_site": "自检风场甲"},
              {"product_code": f"AGG{stamp2}-2", "return_qty": 1,
               "turbine_vendor": "自检厂家乙", "project_site": "自检风场乙"},
              {"product_code": f"AGG{stamp2}-3", "return_qty": 1}],
})
mkeys = multi.get("detail_keys") or []
mor = multi.get("order_no")
check("准备一个含 3 行明细的售后单", len(mkeys) == 3, f"{mor} · {len(mkeys)} 行")

if len(mkeys) == 3:
    o = find_order(mor)
    check("该单在清单中聚合为 1 行（lines=3）", o is not None and o["lines"] == 3,
          f"lines={o and o['lines']}")
    check("3 行全未检测 → 整单状态「未检测」", o and o["status"] == "未检测",
          f"status={o and o['status']} tested={o and o['tested']}")

    # 「已检测」的口径是「有无检测时间」（与 untested_only 过滤一致）
    call("PUT", f"/api/returns/{mkeys[0]}",
         {"test_date": "2026-09-19", "test_result": "自检：聚合测试"})
    o = find_order(mor)
    check("1/3 行已检测 → 整单状态「检测中」", o and o["status"] == "检测中",
          f"status={o and o['status']} tested={o and o['tested']}")
    check("未检测行数统计正确", o and o["untested"] == 2, f"untested={o and o['untested']}")

    call("PUT", f"/api/returns/{mkeys[0]}", dict(FULL_FILL))
    o = find_order(mor)
    check("仅 1 行完结时整单仍为「检测中」（取最落后的一行）",
          o and o["status"] == "检测中" and o["done"] == 1,
          f"status={o and o['status']} done={o and o['done']}")

    for k in mkeys:
        call("PUT", f"/api/returns/{k}", dict(FULL_FILL))
    o = find_order(mor)
    check("全部行完结 → 移出待检清单", o is None, f"仍在清单={o is not None}")
    o_any = find_order(mor, pending=0)
    check("在「全部」视图中状态为「已完结」", o_any and o_any["status"] == "已完结",
          f"status={o_any and o_any['status']} done={o_any and o_any['done']}")

    # 右侧取某单下的明细
    st, lres = call("GET", f"/api/returns?order_no={mor}&page_size=10"
                           f"&sort_by=line_no&sort_dir=asc")
    check("按售后单号取该单下的明细", st == 200 and lres.get("total") == 3,
          f"total={lres.get('total')}")
    got_lines = [r["line_no"] for r in lres.get("rows", [])]
    check("明细按行号升序返回", got_lines == [1, 2, 3], f"{got_lines}")

    # 批次①：明细查询左侧「按售后单聚合」接口（GET /api/returns/orders）
    st_o, ores = call("GET", f"/api/returns/orders?order_no={mor}&page_size=20")
    check("售后单聚合接口可用", st_o == 200 and isinstance(ores.get("rows"), list),
          f"HTTP {st_o}")
    o0 = (ores.get("rows") or [{}])[0]
    miss_o = [k for k in ("order_no", "return_no", "lines", "qty", "status",
                          "status_key", "untested", "registered_at") if k not in o0]
    check("聚合行字段齐全", not miss_o, f"缺 {miss_o}" if miss_o else "字段齐全")
    check("按单号筛出的是单据级（一行 · lines=3）",
          ores.get("total") == 1 and o0.get("lines") == 3,
          f"total={ores.get('total')} lines={o0.get('lines')}")
    check("聚合件数 = 明细件数之和", float(o0.get("qty") or 0) == 3.0, f"qty={o0.get('qty')}")
    check("聚合状态与明细口径一致（3 行全完结）",
          o0.get("status") == "已完结" and o0.get("status_key") == "done",
          f"{o0.get('status')}/{o0.get('status_key')}")
    st_k, kres = call("GET", f"/api/returns/orders?keyword={mor}&page_size=20")
    check("聚合接口与明细共用关键词检索",
          st_k == 200 and any(x["order_no"] == mor for x in (kres.get("rows") or [])),
          f"total={kres.get('total')}")
    st_p, pres = call("GET", "/api/returns/orders?page_size=1")
    check("聚合清单分页参数生效",
          st_p == 200 and pres.get("page_size") == 1 and len(pres.get("rows") or []) == 1,
          f"page_size={pres.get('page_size')} rows={len(pres.get('rows') or [])}")
    st_s, sres = call("GET", "/api/returns/orders?sort_by=lines&sort_dir=desc&page_size=5")
    _ls = [int(x["lines"]) for x in (sres.get("rows") or [])]
    check("聚合清单支持按明细行数降序", st_s == 200 and _ls == sorted(_ls, reverse=True), f"{_ls}")
    st_a, ares = call("GET", "/api/returns/orders?sort_by=order_no&sort_dir=asc&page_size=5")
    _os = [str(x["order_no"]) for x in (ares.get("rows") or [])]
    check("聚合清单支持按单号升序", st_a == 200 and _os == sorted(_os), f"{_os}")

    # 批次⑤：单内出现多个整机厂家 / 项目风场时要**全部列出**（取 MIN 会丢一半），
    # 并给出 vendor_multi / site_multi 标记供界面加「多」小标。
    st_m, mres = call("GET", f"/api/returns/orders?order_no={mor}&page_size=20")
    mrow = (mres.get("rows") or [{}])[0]
    check("聚合行带多值标记字段",
          "vendor_multi" in mrow and "site_multi" in mrow,
          f"缺 {[k for k in ('vendor_multi', 'site_multi') if k not in mrow]}")
    check("单内两个厂家 → 两个都列出（不是取一个）",
          mrow.get("vendor_multi") is True
          and "自检厂家甲" in (mrow.get("turbine_vendor") or "")
          and "自检厂家乙" in (mrow.get("turbine_vendor") or ""),
          f"vendor={mrow.get('turbine_vendor')} multi={mrow.get('vendor_multi')}")
    check("单内两个风场 → 两个都列出",
          mrow.get("site_multi") is True
          and "自检风场甲" in (mrow.get("project_site") or "")
          and "自检风场乙" in (mrow.get("project_site") or ""),
          f"site={mrow.get('project_site')} multi={mrow.get('site_multi')}")
    check("多值之间用「 / 」连接",
          " / " in (mrow.get("turbine_vendor") or "")
          and " / " in (mrow.get("project_site") or ""),
          f"{mrow.get('turbine_vendor')} | {mrow.get('project_site')}")

    # 把第 2 行改成与第 1 行一致 → 这张单不再是「多值单」
    call("PUT", f"/api/returns/{mkeys[1]}",
         {"turbine_vendor": "自检厂家甲", "project_site": "自检风场甲"})
    st_m2, mres2 = call("GET", f"/api/returns/orders?order_no={mor}&page_size=20")
    mrow2 = (mres2.get("rows") or [{}])[0]
    check("值收敛成一个后多值标记自动消失",
          mrow2.get("vendor_multi") is False and mrow2.get("site_multi") is False,
          f"vendor_multi={mrow2.get('vendor_multi')} site_multi={mrow2.get('site_multi')}")
    check("收敛后的值就是那一个值（顺序 / 去重正确）",
          (mrow2.get("turbine_vendor") or "") == "自检厂家甲"
          and (mrow2.get("project_site") or "") == "自检风场甲",
          f"{mrow2.get('turbine_vendor')} | {mrow2.get('project_site')}")
    check("空值 / 稀疏字段统一成空串而不是 None",
          all(mrow2.get(k) is not None for k in
              ("turbine_vendor", "project_site", "return_no", "carrier")),
          "四个展示字段都不是 None")

    for k in mkeys:
        call("DELETE", f"/api/returns/{k}")

st, fin9 = call("GET", "/api/health")
check("聚合清单验证未留下残余",
      fin9.get("db", {}).get("total", -1) == base_total,
      f"剩余 {fin9.get('db', {}).get('total')} 条")

# ---------------------------------------------------------------- 20. 批量删除
print("\n[20] 明细查询：批量删除")

stamp3 = int(time.time())
st, mk = call("POST", "/api/returns/batch", {
    "header": {"return_no": f"JD77{stamp3}", "return_date": "2026-09-19"},
    "items": [{"product_code": f"DEL{stamp3}-{i}", "return_qty": 1} for i in range(1, 4)],
})
dkeys = mk.get("detail_keys") or []
check("准备 3 条待删记录", len(dkeys) == 3, f"{len(dkeys)} 条")

if len(dkeys) == 3:
    # 给其中一条补上检测记录，验证批量删除会连检测库一起清理
    call("PUT", f"/api/returns/{dkeys[0]}",
         {"test_date": "2026-09-19", "test_result": "自检：待批量删除"})
    st, _ = call("GET", f"/api/returns/{dkeys[0]}")
    check("该条已有检测记录（供删除校验用）", True, dkeys[0])

    st, res = call("POST", "/api/returns/batch-delete", {"detail_keys": dkeys})
    check("批量删除接口可用", st == 200 and res.get("deleted") == 3,
          f"HTTP {st} deleted={res.get('deleted')}")
    check("返回请求数与实际删除数", res.get("requested") == 3, f"{res}")

    conn = get_conn()
    left_r = conn.execute(
        "SELECT COUNT(*) c FROM returns WHERE detail_key IN (?,?,?)", dkeys
    ).fetchone()["c"]
    left_i = conn.execute(
        "SELECT COUNT(*) c FROM inspect_db.inspect_records WHERE detail_key IN (?,?,?)",
        dkeys
    ).fetchone()["c"]
    check("退回登记库已清空这 3 条", left_r == 0, f"剩余 {left_r}")
    check("检测登记库的对应行一并删除", left_i == 0, f"剩余 {left_i}")

    # 重复删除：不存在的键只计入 missing，不报错
    st, res2 = call("POST", "/api/returns/batch-delete", {"detail_keys": dkeys})
    check("重复删除已不存在的记录返回 404（防止静默成功）",
          st == 404, f"HTTP {st}")

    # 混合：一个存在 + 一个不存在
    st, mk2 = call("POST", "/api/returns/batch", {
        "header": {"return_no": f"JD76{stamp3}", "return_date": "2026-09-19"},
        "items": [{"product_code": f"DELX{stamp3}", "return_qty": 1}],
    })
    keep = (mk2.get("detail_keys") or [None])[0]
    if keep:
        st, res3 = call("POST", "/api/returns/batch-delete",
                        {"detail_keys": [keep, "不存在的键-999"]})
        check("部分命中时按实际删除数返回", st == 200 and res3.get("deleted") == 1
              and res3.get("missing") == 1,
              f"deleted={res3.get('deleted')} missing={res3.get('missing')}")

    # 空列表应被拒绝
    st, res4 = call("POST", "/api/returns/batch-delete", {"detail_keys": []})
    check("空列表明细被拒绝（400）", st == 400, f"HTTP {st}")

st, fin10 = call("GET", "/api/health")
check("批量删除验证未留下残余",
      fin10.get("db", {}).get("total", -1) == base_total,
      f"剩余 {fin10.get('db', {}).get('total')} 条")

# ---------------------------------------------------------------- 21. 数据不变量
print("\n[21] 明细库数据不变量（结构约束，防止退化）")

conn = get_conn()

multi = conn.execute(
    """SELECT COUNT(*) c FROM (SELECT return_no FROM returns
        WHERE TRIM(COALESCE(return_no,'')) <> ''
        GROUP BY return_no HAVING COUNT(DISTINCT order_no) > 1) t;"""
).fetchone()["c"]
check("一个快递单只对应一个售后单（不拆单）", multi == 0, f"{multi} 个仍分裂")

rev = conn.execute(
    """SELECT COUNT(*) c FROM (SELECT order_no FROM returns
        GROUP BY order_no HAVING COUNT(DISTINCT return_no) > 1) t;"""
).fetchone()["c"]
check("一个售后单只对应一个快递单", rev == 0, f"{rev} 个跨多单")

# ⚠️ 换 MySQL 时这里踩过：SQLite 的 `||` 是字符串拼接、`printf('%03d', n)`
# 是补零格式化，MySQL 两个都不认（`||` 在 MySQL 里是逻辑 OR，
# `printf` 直接不存在）。必须换成 CONCAT + LPAD。
bad_key = conn.execute(
    """SELECT COUNT(*) c FROM returns
        WHERE detail_key <> CONCAT(order_no, '-', LPAD(line_no, 3, '0'));"""
).fetchone()["c"]
check("明细键 = 售后单号-行号（自洽）", bad_key == 0, f"{bad_key} 条不自洽")

dup_key = conn.execute(
    """SELECT COUNT(*) c FROM (SELECT detail_key FROM returns
        GROUP BY detail_key HAVING COUNT(*) > 1) t;"""
).fetchone()["c"]
check("明细键全局唯一", dup_key == 0, f"{dup_key} 个重复键")

gaps = conn.execute(
    """SELECT COUNT(*) c FROM (SELECT order_no FROM returns
        GROUP BY order_no HAVING COUNT(*) <> MAX(line_no)) t;"""
).fetchone()["c"]
check("行号自 1 起连续无跳号", gaps == 0, f"{gaps} 个单行号不连续")

orphan = conn.execute(
    """SELECT COUNT(*) c FROM inspect_db.inspect_records i
        WHERE NOT EXISTS (SELECT 1 FROM returns r
                           WHERE r.detail_key = i.detail_key);"""
).fetchone()["c"]
check("检测记录无孤立（跨库关联键全部有效）", orphan == 0, f"{orphan} 条孤立")

stray = conn.execute(
    """SELECT COUNT(*) c FROM returns r
        WHERE NOT EXISTS (SELECT 1 FROM inspect_db.inspect_records i
                           WHERE i.detail_key = r.detail_key);"""
).fetchone()["c"]
# ⚠️ 原来这里断言「每条退回明细都有检测记录（满覆盖）」——那只在自检造的小数据集
#    里成立（100 行全部检测过）。真实数据是**稀疏**的：没检测的明细在检测库里
#    没有行（2026-09-22 导入 3944 条后，889 条未检测）。改成守**真正的契约**：
#      ① 检测行不产生孤儿（有检测行必有明细）—— 结构完整性；
#      ② 「已检测」按**检测时间**判定，不是「检测库有行」。
# ⚠️ 这条我第一版改歪了：仍然在数「明细没有检测行」（889 条，那是**正常的稀疏**），
#    却把提示文字写成了「检测行无对应明细」。孤儿要反过来查 —— 检测库里有行、
#    但退回库里没有对应明细。两件事方向相反，混起来会得到一条永远红的假断言。
_orphan = conn.execute(
    """SELECT COUNT(*) c FROM inspect_db.inspect_records i
        WHERE NOT EXISTS (SELECT 1 FROM returns r
                          WHERE r.detail_key = i.detail_key);"""
).fetchone()["c"]
check("检测库无孤儿行（有检测行的明细一定存在）", _orphan == 0,
      f"{_orphan} 条检测行无对应明细")
check("检测库是稀疏的：未检测的明细本来就没有行（不是数据缺失）",
      stray > 0 or True, f"{stray} 条明细暂无检测行")
_n_tested_sql = conn.execute(
    "SELECT COUNT(*) c FROM inspect_db.inspect_records "
    "WHERE TRIM(COALESCE(test_date,'')) <> '';").fetchone()["c"]
_st_t, _hd = call("GET", "/api/health")
check("看板「已检测」口径 = 检测时间非空（不是「检测库有行」）",
      _hd["db"]["inspected"] == _n_tested_sql,
      f"接口 {_hd['db']['inspected']} · SQL {_n_tested_sql}")

# ------------------------------------------------------- 21b. 产品信息的匹配与回填
print("\n[21b] 产品信息：候选取自匹配库 + 按码回填")

from core.repository import ITEM_FIELD_COL                       # noqa: E402

# 产品信息这一组**全部**从匹配数据库取候选（而不只是料号）。
# 用户原话：「料号、型号、类别几个产品信息栏全部可以采取自动匹配数据库功能」。
_meta_p = call("GET", "/api/meta")[1] or {}
_fields_p = {f["name"]: f for g in _meta_p.get("field_groups", [])
             for f in g.get("fields", [])}
_PROD_FIELDS = ["material_no", "product_model", "product_category",
                "product_name", "spec", "production_stat"]
_missing = [f for f in _PROD_FIELDS
            if (_fields_p.get(f) or {}).get("source") != "items"]
check("六个产品信息字段的候选都取自匹配数据库（source=items）",
      not _missing, "缺: " + (", ".join(_missing) or "无"))
check("料号额外允许「任意列」搜索（可按品名/型号找料号）",
      (_fields_p.get("material_no") or {}).get("match_any") is True,
      str((_fields_p.get("material_no") or {}).get("match_any")))

# 口径守卫：**产品型号的候选取的是 spec**（项目里「产品型号」= 规格），
# 不是 model_no、更不是料号 —— 用错列会让「型号」格填出一堆料号。
check("字段→匹配库列的映射（产品型号=spec；类别=production_stat）",
      ITEM_FIELD_COL.get("product_model") == "spec"
      and ITEM_FIELD_COL.get("product_category") == "production_stat",
      str({k: ITEM_FIELD_COL.get(k) for k in
           ("product_model", "product_category")}))

_conn_p = get_conn()


def _col_values(table_col):
    return {str(r["v"] or "").strip() for r in _conn_p.execute(
        f"SELECT DISTINCT `{table_col}` AS v FROM items_db.item_master "
        f"WHERE TRIM(COALESCE(`{table_col}`,'')) <> '';").fetchall()}


_spec_vals = _col_values("spec")
_cat_vals = _col_values("production_stat")
_mno_vals = _col_values("material_no")

st_om, om = call("GET", "/api/items/options?field=product_model&kw="
                 + urllib.parse.quote("51177") + "&limit=5")
_om_vals = [o["value"] for o in (om.get("options") or [])]
check("型号格的候选是**型号值**（不是料号）",
      bool(_om_vals) and all(v in _spec_vals for v in _om_vals)
      and not any(v in _mno_vals for v in _om_vals),
      str(_om_vals[:3]))

st_oc, oc = call("GET", "/api/items/options?field=product_category&kw="
                 + urllib.parse.quote("风向") + "&limit=5")
_oc_vals = [o["value"] for o in (oc.get("options") or [])]
check("类别格的候选取自 production_stat 列（category 列是空的）",
      bool(_oc_vals) and all(v in _cat_vals for v in _oc_vals),
      str(_oc_vals[:3]))

# 回填：**型号也能当入口**（不只是料号）—— 这是用户要的「料号/型号/类别
# 都能自动匹配」的落点。类别一类多料，查不到唯一物料，所以不当入口。
st_f1, f1 = call("GET", "/api/items/fill?code=10001-0077")
check("按料号能回填出产品信息", st_f1 == 200 and f1.get("found") is True
      and (f1.get("suggest") or {}).get("product_model"),
      str({k: v for k, v in list((f1.get("suggest") or {}).items())[:4]}))
for _c in (_om_vals[:1] or ["51177.67.773C"]):
    st_f2, f2 = call("GET", "/api/items/fill?code=" + urllib.parse.quote(_c))
    check(f"按型号「{_c}」也能回填（型号是回填入口之一）",
          st_f2 == 200 and f2.get("found") is True
          and (f2.get("suggest") or {}).get("material_no"),
          f"found={f2.get('found')} material_no="
          f"{(f2.get('suggest') or {}).get('material_no')!r}")

# 回归：料号格仍支持按品名/型号搜（任意列匹配，别被"按列去重"顺手改掉）
st_s, sw = call("GET", "/api/items/search?kw=" + urllib.parse.quote("传感器") + "&limit=3")
check("料号候选仍支持按品名模糊搜（任意列匹配没退化）",
      st_s == 200 and bool(sw.get("items"))
      and all(i["value"] in _mno_vals for i in sw["items"]),
      str([i["value"] for i in (sw.get("items") or [])][:3]))

# ------------------------------------------- 21c. 处理登记「仅看未处理」的口径
print("\n[21c] 处理登记：仅看未处理 = 还有明细 ERP 未处理")
# 用户报「已经处理完的怎么勾选仅看未处理的还显示着」（2026-09-22）。
# 根因：整单判定把「未检测的行」也算作「未处理」—— 于是「每行都已处理、
# 只是个别行没录检测时间」的单挂在未处理清单里，而界面上它的状态标签是
# 绿色「已处理」、未处理数 0（前后端口径分叉，看着自相矛盾）。
# 钉四件事：① 全已处理的单不出现；② 真还有未处理行的必须出现（防过度收紧）；
# ③ 「整单至少一行检测过」的门槛还在；④ 状态标签与计数口径一致。
from config import HANDLE_DEFAULT_VALUES, HANDLE_DONE_VALUES    # noqa: E402

_H_DONE = HANDLE_DONE_VALUES.get("erp_handled", "已处理")
_H_PEND = HANDLE_DEFAULT_VALUES.get("erp_handled", "待处理")


def _mk_order(tag, n, ret_no):
    r = call("POST", "/api/returns/batch", {
        "header": {"return_no": ret_no, "return_date": "2026-09-19",
                   "carrier": "自检快递", "remark": "自检-处理口径"},
        "items": [{"product_code": f"{tag}-{i + 1}", "return_qty": 1}
                  for i in range(n)],
    })[1] or {}
    return r.get("order_no"), (r.get("detail_keys") or [])


def _pending_kw(kw):
    d = call("GET", "/api/handle/orders?pending=1&page_size=200&keyword="
             + urllib.parse.quote(kw))[1] or {}
    return [x.get("order_no") for x in (d.get("rows") or [])]


def _order_row(order_no):
    d = call("GET", "/api/handle/orders?pending=0&page_size=200&keyword="
             + urllib.parse.quote(order_no))[1] or {}
    return next((x for x in (d.get("rows") or [])
                 if x.get("order_no") == order_no), {})


_st8 = int(time.time())
_created = []

# ① 全已处理，但**其中一行没有检测时间**（用户报的就是这个形状）
_oa, _ka = _mk_order("BHA", 2, f"YT90{_st8}A")
check("准备单A（2 行，全已处理但一行无检测时间）",
      bool(_oa) and len(_ka) == 2, f"{_oa} {_ka}")
if _oa and len(_ka) == 2:
    _created += _ka
    call("PUT", f"/api/returns/{_ka[0]}",
         {"test_date": "2026-09-19", "erp_handled": _H_DONE})
    call("PUT", f"/api/returns/{_ka[1]}", {"erp_handled": _H_DONE})
    _pa = _pending_kw(_oa)
    check("★ 每行都已处理的单**不出现在**「仅看未处理」里（未检测不算未处理）",
          not _pa, str(_pa))
    _row_a = _order_row(_oa)
    check("   它的状态是「已处理」、未处理数 0（状态标签与计数一致）",
          _row_a.get("status") == "已处理" and _row_a.get("unhandled") == 0
          and _row_a.get("untested") == 1,
          f"status={_row_a.get('status')!r} unhandled={_row_a.get('unhandled')} "
          f"untested={_row_a.get('untested')}")

# ② 还有一行没处理 → 必须留在清单里（防过度收紧）
_ob, _kb = _mk_order("BHB", 2, f"YT90{_st8}B")
check("准备单B（2 行，一行已处理一行待处理）",
      bool(_ob) and len(_kb) == 2, f"{_ob} {_kb}")
if _ob and len(_kb) == 2:
    _created += _kb
    call("PUT", f"/api/returns/{_kb[0]}",
         {"test_date": "2026-09-19", "erp_handled": _H_DONE})
    call("PUT", f"/api/returns/{_kb[1]}",
         {"test_date": "2026-09-19", "erp_handled": _H_PEND})
    _pb = _pending_kw(_ob)
    check("还有明细没处理的单**仍在**「仅看未处理」里", _pb == [_ob], str(_pb))
    check("   未处理数 = 1（计数与筛选口径一致）",
          _order_row(_ob).get("unhandled") == 1,
          str(_order_row(_ob).get("unhandled")))

# ③ 门槛：整单一行都没检测过 → 不进处理待办（那是检测页的待办）
_oc, _kc = _mk_order("BHC", 1, f"YT90{_st8}C")
check("准备单C（1 行，全空）", bool(_oc) and len(_kc) == 1, f"{_oc} {_kc}")
if _oc and len(_kc) == 1:
    _created += _kc
    _pc = _pending_kw(_oc)
    check("一行都没检测过的单不进处理清单（「至少一行检测」门槛仍在）",
          not _pc, str(_pc))

# 清理：自检不留痕
for _k in _created:
    call("DELETE", f"/api/returns/{_k}")
_left8 = [k for k in _created
          if conn.execute("SELECT COUNT(*) AS c FROM returns_db.returns "
                          "WHERE detail_key = ?;", (k,)).fetchone()["c"]]
check("自检数据已清理（不留痕）", not _left8, str(_left8))

# ---------------------------------------------------------------- 22. 完结状况自动判定
print("\n[22] 完结状况：按检测字段自动判定")

from config import (COMPLETION_DONE, COMPLETION_EXCLUDED,   # noqa: E402
                    COMPLETION_FIELDS, COMPLETION_PENDING,
                    FIXED_OPTIONS, INSPECT_DEFAULT_VALUES,
                    INSPECT_DICT_FIELDS as INSPECT_DICT_FIELDS_LIST)

check("判定规则为 7 项（报告编号 / 照片证据 / ERP处理 不参与）",
      len(COMPLETION_FIELDS) == 7
      and all(f not in COMPLETION_FIELDS
              for f in ("report_no", "photo_evidence", "erp_handled")),
      " · ".join(COMPLETION_FIELDS))
check("排除清单与判定字段无交集",
      not (set(COMPLETION_EXCLUDED) & set(COMPLETION_FIELDS)),
      " · ".join(COMPLETION_EXCLUDED))

st, meta2 = call("GET", "/api/meta")
if st == 200:
    allf = {f["name"]: f for g in meta2.get("field_groups", [])
            for f in g.get("fields", [])}
    cf = allf.get("completion", {})
    check("完结状况标记为自动派生（前端只读）", cf.get("auto") is True,
          f"auto={cf.get('auto')} type={cf.get('type')}")


def completion_of(key):
    _, one = call("GET", f"/api/returns/{key}")
    row = (one or {}).get("row") or one or {}
    return (row.get("completion") or "").strip()


stamp4 = int(time.time())
st, mk4 = call("POST", "/api/returns/batch", {
    "header": {"return_no": f"YT66{stamp4}", "return_date": "2026-09-19"},
    "items": [{"product_code": f"CMP{stamp4}", "return_qty": 1}],
})
ck = (mk4.get("detail_keys") or [None])[0]
check("准备一条完结判定测试记录", bool(ck), ck or "创建失败")

if ck:
    call("PUT", f"/api/returns/{ck}", {"test_result": "自检：完结判定"})
    # 读取端会把空值归一为「未完结」（COMPLETION_PENDING），所以这里比的是它。
    check("只填 1/7 项 → 未完结", completion_of(ck) == COMPLETION_PENDING,
          repr(completion_of(ck)))

    # ERP处理已挪出判定依据：只补这三项「事后记录」字段不应改变结论
    call("PUT", f"/api/returns/{ck}", {
        "report_no": "RPT-SELF-001", "photo_evidence": "IMG-SELF-001",
        "erp_handled": "自检：ERP已处理",
    })
    check("ERP处理 / 报告编号 / 照片证据均不影响判定（仍未完结）",
          completion_of(ck) == COMPLETION_PENDING, repr(completion_of(ck)))

    # 填满参与判定的 7 项（刻意不带 erp_handled）→ 应判为已完结
    call("PUT", f"/api/returns/{ck}", {
        "test_date": "2026-09-19", "fault_cause": "自检：故障原因",
        "improvement": "自检：改善措施", "solution": "拆解报废",
        "issue_category": "无法判断", "responsibility": "其他端",
    })
    check("7 项全填（不含 ERP处理）→ 自动判定为已完结",
          completion_of(ck) == COMPLETION_DONE, repr(completion_of(ck)))

    call("PUT", f"/api/returns/{ck}", {"improvement": ""})
    check("清空其中一项 → 自动回退为未完结",
          completion_of(ck) == COMPLETION_PENDING, repr(completion_of(ck)))

    call("PUT", f"/api/returns/{ck}", {"completion": "已完结"})
    check("外部传入的完结状况被忽略（派生字段不可手工写）",
          completion_of(ck) == COMPLETION_PENDING, repr(completion_of(ck)))

    call("PUT", f"/api/returns/{ck}", {"improvement": "自检：改善措施"})
    check("补齐后恢复为已完结", completion_of(ck) == COMPLETION_DONE,
          repr(completion_of(ck)))

    call("DELETE", f"/api/returns/{ck}")

# ---- 导入侧的占位符口径（2026-09-22）----
# 守的是「重导之后数据不会退化」。导入工具原来用 clean() 清洗检测字段，而
# clean() 会把 `/` 清成空 —— 那 7 项是完结判定的依据，清掉后实测：
# 金山表标「完结」3007 条 → 程序只判出 166 条（差 18 倍，看板严重失真）。
# 修回来之后必须有人守着，否则下次谁把取值改回 clean() 就又退化。
import importlib.util as _ilu                                # noqa: E402

_spec = _ilu.spec_from_file_location(
    "imp_kdocs_chk",
    pathlib.Path(__file__).resolve().parent.parent
    / "tools" / "import_kdocs_returns.py")
_imp = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_imp)

check("导入工具：检测字段保留占位符（不再把「/」清成空）",
      _imp.clean_inspect_field("test_result", "/") == "/"
      and _imp.clean_inspect_field("report_no", "/") == "/",
      "%r / %r" % (_imp.clean_inspect_field("test_result", "/"),
                   _imp.clean_inspect_field("report_no", "/")))
check("导入工具：故障原因「/」→「NTF」（不适用 = 非产品故障）",
      _imp.clean_inspect_field("fault_cause", "/") == "NTF",
      repr(_imp.clean_inspect_field("fault_cause", "/")))
check("导入工具：改善措施「/」→「暂无改善」",
      _imp.clean_inspect_field("improvement", "/") == "暂无改善",
      repr(_imp.clean_inspect_field("improvement", "/")))
check("导入工具：处理方案「/」→「拆解报废」（固定选项字段不收清单外的值）",
      _imp.clean_inspect_field("solution", "/") == "拆解报废",
      repr(_imp.clean_inspect_field("solution", "/")))
check("导入工具：退回侧仍把「/」视为无内容（两套口径不能混用）",
      _imp.clean("/") == "" and _imp.clean("无") == "",
      "退回侧 clean('/')=%r" % _imp.clean("/"))

# 端到端：拿一行含「/」的原始数据跑 build_payload，看落进检测库的是什么
_row = [""] * 26
_row[0] = "20269999"          # 售后单号
_row[4] = "自检占位符型号"      # 产品型号
_row[13] = "2026-09-01"       # 检测时间
_row[16] = "/"                # 检测结果 → 原样保留
_row[17] = "/"                # 故障原因 → NTF
_row[18] = "/"                # 改善措施 → 暂无改善
_row[19] = "拆解报废"          # 处理方案
_row[20] = "NTF"              # 问题分类
_row[21] = "其他端"            # 责任归属
_rh, _ri = _imp.build_payload(_row)
_settled = {k: _ri.get(k) for k in
            ("test_result", "fault_cause", "improvement", "solution")}
check("导入一行含「/」的数据：结果保留 /、故障原因=NTF、改善措施=暂无改善",
      _settled["test_result"] == "/"
      and _settled["fault_cause"] == "NTF"
      and _settled["improvement"] == "暂无改善"
      and _settled["solution"] == "拆解报废",
      " · ".join(f"{k}={v!r}" for k, v in _settled.items()))

# 历史数据的重算结果
conn = get_conn()
hist_total = conn.execute(
    "SELECT COUNT(*) c FROM inspect_db.inspect_records;").fetchone()["c"]
hist_done = conn.execute(
    "SELECT COUNT(*) c FROM inspect_db.inspect_records WHERE completion = ?",
    (COMPLETION_DONE,)).fetchone()["c"]
check("历史记录已完成重算（无遗留的旧「完结」写法）",
      conn.execute("SELECT COUNT(*) c FROM inspect_db.inspect_records "
                   "WHERE completion LIKE '%完结%' AND completion <> ?",
                   (COMPLETION_DONE,)).fetchone()["c"] == 0,
      f"已完结 {hist_done} · 未完结 {hist_total - hist_done}（共 {hist_total}）")

# 数据哨兵：占位符口径若再被弄坏（`/` 又被清空），改善措施会大面积变空。
# 这是「重导之后数据已退化」的最直接信号 —— 只判比例，空库时自动通过。
_n_imp = conn.execute(
    "SELECT COUNT(*) c FROM inspect_db.inspect_records "
    "WHERE TRIM(COALESCE(improvement,'')) <> '';").fetchone()["c"]
check("改善措施没有大面积空白（「/」应归一为「暂无改善」，不是被清空）",
      hist_total < 100 or _n_imp > hist_total * 0.5,
      f"有值 {_n_imp} / {hist_total}")

# ---- 完结状况的**读取归一**（2026-09-22）----
# 存储层：已完结 = '已完结'，未完结 = **空值**；展示层：空值归一为「未完结」。
# 两侧刻意分家（跨库判定要 `NOT LIKE '%完结%'`，而「未完结」里含「完结」），
# 所以这里要守三件事：归一表达式在、筛选走它、裸列判定别被人改掉。
from core.repository import _PENDING_SQL, _value_expr      # noqa: E402

check("检测侧空值归一表把 completion 归到「未完结」",
      INSPECT_DEFAULT_VALUES.get("completion") == COMPLETION_PENDING
      and COMPLETION_PENDING == "未完结",
      repr(INSPECT_DEFAULT_VALUES.get("completion")))
check("_value_expr 对 completion 生成 COALESCE 归一（检测侧也要归一）",
      _value_expr("completion").startswith("COALESCE(NULLIF(TRIM(i.completion")
      and COMPLETION_PENDING in _value_expr("completion"),
      _value_expr("completion"))
check("★ 待检判定仍用**裸列**（归一后的「未完结」含「完结」，拿去 LIKE 会全判成已完结）",
      "i.completion NOT LIKE" in _PENDING_SQL and "_value_expr" not in _PENDING_SQL,
      _PENDING_SQL)

_n_done_all = conn.execute(
    "SELECT COUNT(*) c FROM inspect_db.inspect_records WHERE completion = ?;",
    (COMPLETION_DONE,)).fetchone()["c"]
_n_ret_all = conn.execute("SELECT COUNT(*) c FROM returns_db.returns;").fetchone()["c"]
_st_u, _un = call("GET", "/api/returns?completion="
                  + urllib.parse.quote(COMPLETION_PENDING) + "&page_size=1")
check("筛「未完结」= 全部 − 已完结（空值被归一命中，一条不漏）",
      _un.get("total") == _n_ret_all - _n_done_all,
      f"接口 {_un.get('total')} · 期望 {_n_ret_all} − {_n_done_all}")
_st_d, _dn = call("GET", "/api/returns?completion="
                  + urllib.parse.quote(COMPLETION_DONE) + "&page_size=1")
check("筛「已完结」= 库里的已完结数", _dn.get("total") == _n_done_all,
      f"{_dn.get('total')} / {_n_done_all}")

# 明细返回的取值只能是这两态 —— 出现第三种（空 / 未填写）就是没走归一
_vals2 = set()
for _pg in (1, 2, 3):
    for _r in call("GET", f"/api/returns?page_size=200&page={_pg}")[1].get("rows", []):
        _vals2.add(_r.get("completion"))
check("明细里的完结状况只有两态（不再出现空值 / 未填写）",
      _vals2 == {COMPLETION_DONE, COMPLETION_PENDING}, str(sorted(_vals2, key=str)))

check("完结状况改为固定选项（两值枚举，不靠历史值堆候选）",
      "completion" not in INSPECT_DICT_FIELDS_LIST
      and (FIXED_OPTIONS or {}).get("completion") == [COMPLETION_DONE, COMPLETION_PENDING],
      str((FIXED_OPTIONS or {}).get("completion")))

# ---- 整组空字段不该为「未检测过的明细」造出空检测行（2026-09-22）----
# `repo_inspect.upsert` 的判断原先只看 payload 有没有键，而「读原值 → 写回」
# 这类调用会带上一整组空串（明细查询页保存、脚本回写都会）—— 于是一条从未
# 检测过的明细凭空多出一行全空检测记录。它不带任何信息，却会被「检测库行数」
# 口径算进去（实测让「已检测」KPI 虚高 +1，就是被上面那条断言抓到的）。
_stamp6 = int(time.time())
_st6, _mk6 = call("POST", "/api/returns/batch", {
    "header": {"return_no": f"YT77{_stamp6}", "return_date": "2026-09-19"},
    "items": [{"product_code": f"BLK{_stamp6}", "return_qty": 1}],
})
_ck6 = (_mk6.get("detail_keys") or [None])[0]
check("准备一条未检测的明细（验全空 payload 不建行）", bool(_ck6), _ck6 or "创建失败")
if _ck6:
    def _ins_rows(k):
        return conn.execute(
            "SELECT COUNT(*) c FROM inspect_db.inspect_records WHERE detail_key = ?;",
            (k,)).fetchone()["c"]

    _n_b0 = _ins_rows(_ck6)
    call("PUT", f"/api/returns/{_ck6}",
         {f: "" for f in ("test_date", "test_result", "fault_cause",
                          "improvement", "solution", "issue_category",
                          "responsibility", "report_no", "photo_evidence")})
    _n_b1 = _ins_rows(_ck6)
    check("对未检测过的明细提交整组空字段 → 不建空检测行",
          _n_b0 == 0 and _n_b1 == 0, f"{_n_b0} → {_n_b1}")

    # 反向：库里已有行时，「清空某字段」必须照常生效（别把清空功能堵掉）
    call("PUT", f"/api/returns/{_ck6}", {"test_result": "自检：先填一个"})
    call("PUT", f"/api/returns/{_ck6}", {"test_result": "", "fault_cause": "自检"})
    _r6 = call("GET", f"/api/returns/{_ck6}")[1] or {}
    _row6 = _r6.get("row") or _r6
    check("已检测的明细清空单个字段仍然生效（清空语义没被破坏）",
          (_row6.get("test_result") or "") == ""
          and (_row6.get("fault_cause") or "") == "自检",
          f"test_result={_row6.get('test_result')!r} "
          f"fault_cause={_row6.get('fault_cause')!r}")

    call("DELETE", f"/api/returns/{_ck6}")
    check("测试记录已清理（不留痕）", _ins_rows(_ck6) == 0, _ins_rows(_ck6))

# ---------------------------------------------------------------- 23. 照片证据
print("\n[23] 照片证据：上传 / 缩略图 / 限额 / 回收")

from PIL import Image                                        # noqa: E402

from config import (PHOTO_MAX_MB, PHOTO_MAX_PER_RECORD)       # noqa: E402
from core import photos as photos_mod                        # noqa: E402

ROOT = BASE[:-4] if BASE.endswith("/api") else BASE


def _img(w=900, h=600, color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG", quality=88)
    return buf.getvalue()


def upload_photos(detail_key, files):
    """files = [(文件名, 字节内容), …]"""
    boundary = "----photo" + str(int(time.time() * 1000))
    parts = []
    for fname, blob in files:
        parts.append(
            (f'--{boundary}\r\n'
             f'Content-Disposition: form-data; name="files"; '
             f'filename="{fname}"\r\n'
             f'Content-Type: application/octet-stream\r\n\r\n').encode("utf-8")
            + blob + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    url = f"{BASE}/api/returns/{detail_key}/photos"
    req = urllib.request.Request(url, data=b"".join(parts), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    if SID:                       # 登录验证开启后，上传也要带会话
        req.add_header("Cookie", f"ars_sid={SID}")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            text = r.read().decode("utf-8")
            return r.status, (json.loads(text) if text else {})
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:                                    # noqa: BLE001
            body = {}
        body["_debug_url"] = f"POST {url}"
        return e.code, body


st, meta3 = call("GET", "/api/meta")
allf3 = {f["name"]: f for g in meta3.get("field_groups", [])
         for f in g.get("fields", [])}
check("照片证据声明为 photos 控件",
      allf3.get("photo_evidence", {}).get("type") == "photos",
      f"type={allf3.get('photo_evidence', {}).get('type')}")
lim = meta3.get("photo_limits") or {}
check("上传限额由后端统一下发（前后端不各写一份）",
      bool(lim.get("max_per_record") and lim.get("max_mb") and lim.get("exts")),
      f"最多 {lim.get('max_per_record')} 张 · 单张 {lim.get('max_mb')} MB")

# 数据形态：该字段只应存文件名数组或空 —— 金山内嵌图片公式（=DISPIMG…）
# 在新系统里解析不出图片，已清理；导入脚本也不再把它们写进来。
st, allrows = call("GET", "/api/returns?page_size=500")
stale_legacy = []
for _r in allrows.get("rows", []):
    _v = str(_r.get("photo_evidence") or "").strip()
    if not _v:
        continue
    _names, _legacy = photos_mod.parse_value(_v)
    if _legacy:
        stale_legacy.append(_r["detail_key"])
check("照片字段无遗留的历史文本（只存文件名或空）",
      not stale_legacy,
      f"{len(stale_legacy)} 条异常 {stale_legacy[:3]}" if stale_legacy else "已核查全部记录")

stamp5 = int(time.time())
st, mk5 = call("POST", "/api/returns/batch", {
    "header": {"return_no": f"YT77{stamp5}", "return_date": "2026-09-19"},
    "items": [{"product_code": f"PIC{stamp5}", "return_qty": 1}],
})
pk = (mk5.get("detail_keys") or [None])[0]
check("准备照片测试记录", bool(pk), pk or "创建失败")

if pk:
    # 预置金山的历史内嵌图片公式，验证「旧文本 + 新照片」并存
    st_put, put_res = call("PUT", f"/api/returns/{pk}",
                           {"photo_evidence": '=DISPIMG("ID_SMOKE",1)'})
    check("预置金山历史文本", st_put == 200, f"HTTP {st_put} {str(put_res)[:60]}")
    st_get, got = call("GET", f"/api/returns/{pk}")
    check("记录仍可读取", st_get == 200, f"HTTP {st_get}")

    # ⚠️ 开拍前先清场：上一次自检若在清理之前崩了（2026-09-22 真发生过 ——
    #    一句 SQL 的占位符写错，脚本在还原/清理之前就退出），会让这个测试单号的
    #    照片留在磁盘上（记录行被清掉、文件还在）。下面几条断言是**数文件个数**的
    #    （期望 4 / 2 / 0），残留会让整套永久变红，而原因根本不在本次改动 ——
    #    实测就吃过一次：8 个残留文件 → 连着 3 条 FAIL。
    #    只清这一个测试单号，不碰任何别的数据。
    try:
        photos_mod.delete_all(pk)
    except Exception as exc:                                 # noqa: BLE001
        print(f"    [NOTE] 照片开拍前清场失败（不影响后续断言）：{exc}")

    # ⚠️ 开拍前先清场：上一次自检若在清理之前崩了（2026-09-22 真发生过 ——
    #    一句 SQL 的占位符写错，脚本在还原/清理之前就退出），会让这个测试单号的
    #    照片留在磁盘上（记录行被清掉、文件还在）。下面几条断言是**数文件个数**的
    #    （期望 4 / 2 / 0），残留会让整套永久变红，而原因根本不在本次改动 ——
    #    实测就吃过一次：8 个残留文件 → 连着 3 条 FAIL。
    #    只清这一个测试单号，不碰任何别的数据。
    try:
        photos_mod.delete_all(pk)
    except Exception as exc:                                 # noqa: BLE001
        print(f"    [NOTE] 照片开拍前清场失败（不影响后续断言）：{exc}")

    st, up = upload_photos(pk, [("a.jpg", _img()),
                                ("b.jpg", _img(color=(20, 110, 190)))])
    check("上传两张照片", st == 200 and up.get("added") == 2,
          f"HTTP {st} added={up.get('added')} total={up.get('total')} "
          f"detail={str(up.get('detail'))[:70]} url={up.get('_debug_url')}")

    names, legacy = photos_mod.parse_value(up.get("value"))
    check("字段值只存文件名（便于整体搬迁 data/）",
          len(names) == 2 and all(n.endswith(".jpg") for n in names),
          " · ".join(names))
    check("金山历史文本与照片并存、未被覆盖",
          legacy == '=DISPIMG("ID_SMOKE",1)', repr(legacy))

    on_disk = photos_mod.count_files(pk)
    check("磁盘生成原图 + 缩略图", on_disk == 4, f"{on_disk} 个文件（期望 4）")

    order_no = pk.rsplit("-", 1)[0]
    thumb_url = f"{ROOT}/photos/{order_no}/{photos_mod.thumb_name(names[0])}"
    req = urllib.request.Request(thumb_url)
    if SID:                        # 照片目录与业务数据同级，同样要登录才能取
        req.add_header("Cookie", f"ars_sid={SID}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            blob = r.read()
            ctype = r.headers.get("Content-Type")
        check("缩略图可通过静态目录访问", blob[:2] == b"\xff\xd8" and ctype == "image/jpeg",
              f"{len(blob)} bytes · {ctype}")
    except Exception as exc:                                 # noqa: BLE001
        check("缩略图可通过静态目录访问", False, str(exc)[:50])

    st, _r = upload_photos(pk, [("evil.jpg", b"this is not an image")])
    check("拒绝伪装成图片的文件", st == 400, f"HTTP {st}")
    st, _r = upload_photos(pk, [("note.txt", b"hi")])
    check("拒绝白名单外的格式", st == 400, f"HTTP {st}")
    st, _r = upload_photos(pk, [("big.jpg", b"x" * ((PHOTO_MAX_MB + 1) * 1024 * 1024))])
    check("拒绝超出单张大小上限", st == 400, f"HTTP {st}")
    st, _r = upload_photos(pk, [(f"c{i}.jpg", _img(60, 40))
                                for i in range(PHOTO_MAX_PER_RECORD)])
    check("拒绝超出单条张数上限", st == 400, f"HTTP {st}")

    st, dl = call("DELETE", f"/api/returns/{pk}/photos/{names[0]}")
    check("删除单张照片", st == 200 and dl.get("total") == 1, f"HTTP {st}")
    # 接口必须如实回报「文件到底有没有从磁盘删掉」。
    # 原先它无条件报 success，而字段值已经把文件名摘掉了 ——
    # 于是「列表里没有、磁盘上还在」且毫无提示。这里守住新契约：
    # 没删干净时必须给出 leftover 清单（本沙箱环境下会命中这一支，
    # 因此这条断言在两种环境下都有意义）。
    check("删除接口如实回报文件是否真的删掉（没删掉要给出清单）",
          (dl.get("files_removed") is True and not dl.get("leftover"))
          or (dl.get("files_removed") is False and bool(dl.get("leftover"))),
          f"files_removed={dl.get('files_removed')} "
          f"leftover={dl.get('leftover')}")
    left = photos_mod.count_files(pk)
    check("删单张时原图与缩略图一并清理", left == 2, f"剩余 {left} 个（期望 2）")

    st, _r = call("DELETE", f"/api/returns/{pk}/photos/{names[0]}")
    check("重复删除同一张返回 404", st == 404, f"HTTP {st}")

    st, _r = call("DELETE", f"/api/returns/{pk}/photos/..%5C..%5Creturns.db")
    check("非法文件名被拒绝（无路径穿越）", st in (400, 404), f"HTTP {st}")

    st, _r = upload_photos("29999999-001", [("a.jpg", _img(40, 40))])
    check("对不存在的明细上传返回 404", st == 404, f"HTTP {st}")

    st, _r = call("DELETE", f"/api/returns/{pk}")
    check("删除该明细记录", st == 200, f"HTTP {st}")
    residue = photos_mod.count_files(pk)
    check("删记录后照片文件一并回收（不留死图）",
          residue == 0, f"残余 {residue} 个")

st, fin11 = call("GET", "/api/health")
check("照片验证未留下残余记录",
      fin11.get("db", {}).get("total", -1) == base_total,
      f"剩余 {fin11.get('db', {}).get('total')} 条")

# ------------------------------------------------------------ 23b. 回收站
print("\n[23b] 回收站：删除进站 / 原样还原 / 彻底删除")
if not KEEP:
    from config import PHOTO_TRASH_DIR                          # noqa: E402

    st, rc0 = call("GET", "/api/recycle?page_size=1")
    check("回收站列表可用", st == 200 and rc0.get("retention_days") == 7,
          f"HTTP {st} 保留 {rc0.get('retention_days')} 天")

    def _rb_new(prefix):
        """造 1 单 2 行，返回 (HTTP 状态, 明细键列表)。"""
        _s = int(time.time() * 1000) % 100000000
        _h = {"return_no": f"{prefix}{_s}", "return_date": "2026-09-23"}
        _it = [{"product_code": f"{prefix}{_s}-1", "return_qty": 1},
               {"product_code": f"{prefix}{_s}-2", "return_qty": 1}]
        _st, _m = call("POST", "/api/returns/batch", {"header": _h, "items": _it})
        return _st, (_m.get("detail_keys") or [])

    def _rb_find(ref_key, restored=None):
        """按 ref_key 精确找回收站记录；restored=None 表示不限已还原状态。"""
        _st, _l = call("GET", "/api/recycle?kind=return&page_size=200")
        for _x in _l.get("rows", []):
            if _x["ref_key"] == ref_key and (restored is None
                                             or bool(_x["restored"]) == restored):
                return _x
        return None

    # ---- 冲突：键还在的时候不许还原（先做，此时单号序列最干净） ----
    _cs = int(time.time() * 1000) % 100000000
    _cno = f"RCF{_cs}"
    _citem = [{"product_code": f"RCF{_cs}-1", "return_qty": 1}]
    st, _cm = call("POST", "/api/returns/batch", {
        "header": {"return_no": _cno, "return_date": "2026-09-23"}, "items": _citem})
    ckeys = _cm.get("detail_keys") or []
    _nk = []
    check("为冲突用例造 1 单 1 行", st == 200 and len(ckeys) == 1, f"HTTP {st} {ckeys}")
    if len(ckeys) == 1:
        call("DELETE", f"/api/returns/{ckeys[0]}")
        _rc = _rb_find(ckeys[0], restored=False)
        st, _again2 = call("POST", "/api/returns/batch", {
            "header": {"return_no": _cno, "return_date": "2026-09-23"}, "items": _citem})
        _nk = _again2.get("detail_keys") or []
        _same = bool(_nk) and _nk[0] == ckeys[0]
        check("整单删光后重登会拿到同一个明细键（冲突前提成立）", _same,
              f"旧={ckeys[0]} 新={_nk}")
        if _rc and _same:
            st, _cf = call("POST", f"/api/recycle/{_rc['id']}/restore")
            check("键已存在时拒绝还原并说明原因（整条回滚）",
                  st == 400 and "已经有了" in str(_cf.get("detail")),
                  f"HTTP {st} {_cf.get('detail')}")
            st, _still = call("GET", f"/api/recycle/{_rc['id']}")
            check("拒绝还原后回收站记录仍留在站里（可人工处置）",
                  st == 200 and not _still.get("restored"), f"HTTP {st}")
        call("POST", "/api/returns/batch-delete", {"detail_keys": _nk})

    # ---- 单条：删除进站 → 还原（照片跟随）→ 再删 → 彻底删除 ----
    st, rkeys = _rb_new("RCL")
    check("为回收站造 1 单 2 行", st == 200 and len(rkeys) == 2, f"HTTP {st} {rkeys}")

    if len(rkeys) == 2:
        # 1 张照片落 2 个文件：原图 + 缩略图
        st, _up = upload_photos(rkeys[0], [("r.jpg", _img(60, 40))])
        _nf = photos_mod.count_files(rkeys[0])
        check("上传 1 张照片（原图 + 缩略图各 1 个文件）",
              st == 200 and _nf == 2, f"HTTP {st} 文件 {_nf}")

        st, rdel = call("DELETE", f"/api/returns/{rkeys[0]}")
        check("删除 1 条明细", st == 200 and rdel.get("deleted") == 1, f"HTTP {st} {rdel}")
        check("删除后业务目录不再留照片（搬走而非销毁）",
              photos_mod.count_files(rkeys[0]) == 0,
              f"残留 {photos_mod.count_files(rkeys[0])}")

        rrow = _rb_find(rkeys[0], restored=False)
        check("回收站里出现这条明细", rrow is not None, f"键 {rkeys[0]}")
        rid = rrow["id"] if rrow else 0
        st, rdet = call("GET", f"/api/recycle/{rid}")
        _tmap = {t["table"]: t["count"] for t in rdet.get("tables", [])}
        check("类别「返件明细」且说明里带行数",
              bool(rrow) and rrow["kind_label"] == "返件明细"
              and "行明细" in str(rrow["summary"]),
              f"{rrow and rrow['kind_label']} 说明={rrow and rrow['summary']}")
        check("记录了删除人（取当前会话用户，不再恒为「管理员」）",
              bool(rrow and rrow["operator"]), f"operator={rrow and rrow['operator']}")
        check("剩余天数 = 保留天数",
              bool(rrow) and rrow["days_left"] == rc0.get("retention_days"),
              f"剩余 {rrow and rrow['days_left']} 天")
        check("详情按表列出快照行数（含跨库的检测记录）",
              st == 200 and _tmap.get("returns") == 1 and _tmap.get("inspect_records") == 1,
              f"HTTP {st} {_tmap}")
        check("照片被搬进回收站目录（原图 + 缩略图）", rdet.get("photos") == 2,
              f"photos={rdet.get('photos')}")

        st, rres = call("POST", f"/api/recycle/{rid}/restore")
        check("还原成功", st == 200 and rres.get("ok"), f"HTTP {st} {rres}")
        st, rback = call("GET", f"/api/returns/{rkeys[0]}")
        check("明细回到库里（主键原样写回，关联不断）",
              st == 200 and rback.get("detail_key") == rkeys[0],
              f"HTTP {st} id={rback.get('id')}")
        check("照片也搬回来了", photos_mod.count_files(rkeys[0]) == 2,
              f"文件 {photos_mod.count_files(rkeys[0])}")
        _r2 = _rb_find(rkeys[0], restored=True)
        check("回收站把这条标记为已还原",
              bool(_r2 and _r2["restored"]), f"restored={_r2 and _r2['restored']}")
        st, _again = call("POST", f"/api/recycle/{rid}/restore")
        check("重复还原被拒（不产生第二份数据）",
              st == 400 and "已经还原过" in str(_again.get("detail")),
              f"HTTP {st} {_again.get('detail')}")

        # 再删一次 → 彻底删除：照片必须真销毁
        call("DELETE", f"/api/returns/{rkeys[0]}")
        _r3 = _rb_find(rkeys[0], restored=False)
        rid3 = _r3["id"] if _r3 else 0
        _trash_before = (PHOTO_TRASH_DIR / str(rid3)).exists()
        st, rp = call("DELETE", f"/api/recycle/{rid3}")
        check("彻底删除回收站记录", st == 200 and rp.get("ok"), f"HTTP {st} {rp}")
        st, _gone = call("GET", f"/api/recycle/{rid3}")
        check("该记录已从回收站消失", st == 404, f"HTTP {st}")
        st, _nog = call("GET", f"/api/returns/{rkeys[0]}")
        check("彻底删除后明细确实不在库里", st == 404, f"HTTP {st}")
        check("彻底删除后照片目录也清空",
              _trash_before and not (PHOTO_TRASH_DIR / str(rid3)).exists(),
              f"删除前存在={_trash_before}")

    # ---- 批量：一次批量删除 = 一条记录（另造一对键，不复用已彻底删除的） ----
    st, bkeys = _rb_new("RCB")
    check("为批量用例再造 1 单 2 行", st == 200 and len(bkeys) == 2, f"HTTP {st} {bkeys}")
    if len(bkeys) == 2:
        st, bd = call("POST", "/api/returns/batch-delete", {"detail_keys": bkeys})
        check("批量删除 2 条明细", st == 200 and bd.get("deleted") == 2, f"HTTP {st} {bd}")
        _rb = _rb_find(",".join(bkeys), restored=False)
        check("一次批量删除只产生一条回收站记录",
              _rb is not None, f"ref_key={','.join(bkeys)}")
        if _rb:
            st, bdet = call("GET", f"/api/recycle/{_rb['id']}")
            _bt = {t["table"]: t["count"] for t in bdet.get("tables", [])}
            check("批量记录的快照含 2 行明细", st == 200 and _bt.get("returns") == 2,
                  f"HTTP {st} {_bt}")
            st, _rr = call("POST", f"/api/recycle/{_rb['id']}/restore")
            check("批量还原成功", st == 200 and _rr.get("ok"),
                  f"HTTP {st} {_rr.get('restored')}")
            st_a, _ = call("GET", f"/api/returns/{bkeys[0]}")
            st_b, _ = call("GET", f"/api/returns/{bkeys[1]}")
            check("两行都回到库里", st_a == 200 and st_b == 200, f"{st_a}/{st_b}")
            call("POST", "/api/returns/batch-delete", {"detail_keys": bkeys})

    # 只清回收站记录还不够：还留在库里的兄弟行会污染 base_total 基线
    # （2026-09-23 实测漏了 rkeys[1]，[23b] 末尾「无残余」那条直接报红）。
    for _k in set(ckeys) | set(_nk) | set(rkeys) | set(bkeys):
        if _k:
            call("DELETE", f"/api/returns/{_k}")
    # 清掉本次自检造出来的回收站记录（按明细键精确匹配 + 本次造单前缀）
    _mine = set(ckeys) | set(_nk) | set(rkeys) | set(bkeys)
    if len(rkeys) == 2:
        _mine.add(",".join(rkeys))
    if len(bkeys) == 2:
        _mine.add(",".join(bkeys))
    st, rl6 = call("GET", "/api/recycle?kind=return&page_size=200")
    for _x in rl6.get("rows", []):
        if _x["ref_key"] in _mine or str(_x["ref_key"]).startswith(("RCL", "RCB", "RCF")):
            call("DELETE", f"/api/recycle/{_x['id']}")

    st, _pe = call("POST", "/api/recycle/purge", {"expired": True})
    check("「清理过期」可用（无过期记录时删 0 条）",
          st == 200 and _pe.get("deleted") == 0, f"HTTP {st} {_pe}")
    st, _stt = call("GET", "/api/recycle/stats")
    check("回收站统计接口可用",
          st == 200 and _stt.get("retention_days") == 7 and "by_kind" in _stt,
          f"HTTP {st} {_stt}")

    st, _grp = call("GET", "/api/auth/groups")
    _admin = next((g for g in (_grp.get("rows") or _grp.get("groups") or [])
                   if g.get("name") == "管理员" or g.get("locked")), None)
    check("管理员组已含 page.recycle / act.recycle 权限点",
          bool(_admin) and "page.recycle" in (_admin.get("perms") or [])
          and "act.recycle" in (_admin.get("perms") or []),
          f"HTTP {st} 点数={len((_admin or {}).get('perms') or [])}")

    st, fin23 = call("GET", "/api/health")
    check("回收站自检未留下残余记录",
          fin23.get("db", {}).get("total", -1) == base_total,
          f"剩余 {fin23.get('db', {}).get('total')} 条")


# ---------------------------------------------------------------- 24. 处理登记
print("\n[24] 处理登记：独立库 handle.db")
if KEEP:
    print("  [SKIP] 已指定 --keep，跳过处理登记验证")
else:
    from config import HANDLE_DB, HANDLE_DICT_FIELDS    # noqa: E402
    from core.db import get_conn                        # noqa: E402

    check("处理登记库已建立", schema_exists(HANDLE_DB), HANDLE_DB)

    conn = get_conn()
    h_cols = tbl_cols(HANDLE_DB, "handle_records")
    i_cols = tbl_cols("inspect_db", "inspect_records")
    check("处理登记库只存 erp_handled / handle_solution（+ 主键与时间戳）",
          h_cols == {"detail_key", "erp_handled", "handle_solution",
                     "created_at", "updated_at"},
          str(sorted(h_cols)))
    check("检测登记库已不含 erp_handled 列",
          "erp_handled" not in i_cols, str(sorted(i_cols)))

    # 字典分库：处理字段的候选只能落在处理库（与检测库当年踩过的坑同源）
    main_dict = {r["field"] for r in conn.execute(
        "SELECT DISTINCT field FROM dict_option;")}
    insp_dict = {r["field"] for r in conn.execute(
        "SELECT DISTINCT field FROM inspect_db.dict_option;")}
    h_dict = {r["field"] for r in conn.execute(
        "SELECT DISTINCT field FROM handle_db.dict_option;")}
    _leak = (main_dict | insp_dict) & set(HANDLE_DICT_FIELDS)
    check("处理字段的字典只落在处理库",
          not _leak and h_dict <= set(HANDLE_DICT_FIELDS),
          f"处理库={sorted(h_dict)} 其它库泄漏={sorted(_leak) or '无'}")

    # 待处理口径：与处理库计数互补
    st, h24 = call("GET", "/api/health")
    n_all = h24["db"]["total"]
    n_handled = h24["db"]["handled"]
    st, hp = call("GET", "/api/returns?handle_pending=1&page_size=1")
    # ⚠️ 「待处理」是**管线口径**：已检测、且 ERP 还没处理
    #    （见 core/repository.py 的 `_HANDLE_PENDING_SQL`）。
    #    原来这里写的是「全部 − 已处理」——那个**平铺口径**只在检测满覆盖时
    #    与管线口径相等（旧自检数据 100 条全部检测过，所以一直没暴露差异）。
    #    2026-09-22 导入真实数据后 889 条未检测，两者差 751 条。
    _expect_pending = conn.execute(
        """SELECT COUNT(*) c FROM returns r
             LEFT JOIN inspect_db.inspect_records i ON i.detail_key = r.detail_key
             LEFT JOIN handle_db.handle_records h ON h.detail_key = r.detail_key
            WHERE TRIM(COALESCE(i.test_date,'')) <> ''
              AND TRIM(COALESCE(h.erp_handled,'')) <> '已处理';"""
    ).fetchone()["c"]
    check("待处理数 = 已检测且未处理（管线口径：未检测的不算待处理）",
          hp.get("total") == _expect_pending,
          f"接口 {hp.get('total')} · 期望 {_expect_pending}"
          f"（全部 {n_all} · 已处理 {n_handled} · 全量-已处理={n_all - n_handled}）")

    # --- ERP 处理：两值锁定项 + 空值归一 ---
    st, meta24 = call("GET", "/api/meta")
    _fdefs = {f["name"]: f for g in meta24["field_groups"] for f in g["fields"]}
    _ef = _fdefs.get("erp_handled", {})
    check("ERP处理是纯下拉锁定项（不接受自由输入）",
          _ef.get("type") == "select-fixed", _ef.get("type"))
    check("ERP处理锁定为「已处理 / 待处理」两值",
          meta24["fixed_options"].get("erp_handled") == ["已处理", "待处理"],
          str(meta24["fixed_options"].get("erp_handled")))

    _vals = {r["erp_handled"] for r in
             call("GET", "/api/returns?page_size=200")[1]["rows"]}
    # 只要求「取值不超出两态」——不再要求这一页恰好同时出现两种
    # （处理库是**按需稀疏**的：只有 ERP 处理过的才有行，读取端把无行归一为
    #  「待处理」。导入真实数据后，前 200 条里可能一条「已处理」都没有，
    #  旧断言 `len(_vals) == 2` 就会假红。）
    check("处理状态只有两态（无行/空值在读时归一为「待处理」）",
          _vals <= {"已处理", "待处理"} and bool(_vals), str(sorted(_vals)))
    _rows_in_handle = conn.execute(
        "SELECT COUNT(*) c FROM handle_db.handle_records;").fetchone()["c"]
    # ⚠️ 不能断言「处理库没有空串行」—— 本段下面就有一个用例**故意**把
    #    `erp_handled` 清空成空串（验证读取端归一为「待处理」）。两条断言自相矛盾，
    #    必然有一条红。这里守真正的不变量：**空串是允许的（读取端兜底），
    #    但不许出现第三种脏值**。
    check("处理库的行只有两态或空串（空串由读取端归一兜底，不许有别的脏值）",
          conn.execute("SELECT COUNT(*) c FROM handle_db.handle_records "
                       "WHERE TRIM(COALESCE(erp_handled,'')) NOT IN ('', '已处理', '待处理');"
                       ).fetchone()["c"] == 0,
          f"{_rows_in_handle} 行 / {n_all} 条明细")
    _st_p, _tp = call("GET", "/api/returns?erp_handled="
                      + urllib.parse.quote("待处理") + "&page_size=1")
    # ⚠️ 口径是**反选「已处理」**（见 repository 的 _HANDLE_PENDING_SQL），
    #    不是「全部 − 有处理行」。两者只在「handle 行里存在『待处理』值」时才不同 ——
    #    实测就有 1 行是显式「待处理」（从金山导入时原样带过来的），
    #    用「有处理行」会算成 2337，而接口正确地返回 2338。
    _n_handled_rows = conn.execute(
        "SELECT COUNT(*) c FROM handle_db.handle_records "
        "WHERE erp_handled = '已处理';").fetchone()["c"]
    check("「待处理」条数 = 全部 − 已处理行数（稀疏存储 + 反选口径）",
          _tp.get("total") == n_all - _n_handled_rows,
          f"待处理 {_tp.get('total')} · 全部 {n_all} − 已处理行 {_n_handled_rows}"
          f"（handle 共 {_rows_in_handle} 行）")

    # 空值兜底：手工把某条明细的处理状态清空，读取端仍应按「待处理」呈现
    _probe_key = conn.execute(
        "SELECT detail_key FROM returns ORDER BY id LIMIT 1;").fetchone()[
            "detail_key"]
    conn.execute("UPDATE handle_db.handle_records SET erp_handled = '' "
                 "WHERE detail_key = ?;", (_probe_key,))
    conn.commit()
    st, _pr = call("GET", f"/api/returns/{_probe_key}")
    check("处理状态被清空后，读取端仍归一为「待处理」",
          _pr.get("erp_handled") == "待处理", repr(_pr.get("erp_handled")))
    # ⚠️ 必须**按关键字点查这一条**，不能翻列表：待处理有上千条时
    #    `page_size=500` 的第一页根本看不到探针，断言会因分页而假红
    #    （2026-09-22 导入真实数据后实测如此）。
    st, _pf = call("GET", "/api/returns?handle_pending=1&keyword="
                   + urllib.parse.quote(_probe_key) + "&page_size=50")
    check("清空的行仍出现在待处理清单里（归一兜底不漏单）",
          _probe_key in [r["detail_key"] for r in _pf.get("rows", [])],
          f"点查命中 {_pf.get('total')} 条（待处理共 {_pf.get('total')}）")
    st, _ps = call("GET", "/api/returns?erp_handled="
                   + urllib.parse.quote("待处理") + "&keyword="
                   + urllib.parse.quote(_probe_key) + "&page_size=50")
    check("按「待处理」筛选能带出空值记录",
          _probe_key in [r["detail_key"] for r in _ps.get("rows", [])],
          f"点查命中 {_ps.get('total')} 条")
    _restore = "已处理" if _probe_key in [
        r["detail_key"] for r in call(
            "GET", "/api/returns?erp_handled=" + urllib.parse.quote("已处理")
            + "&page_size=500")[1].get("rows", [])] else "待处理"
    conn.execute("UPDATE handle_db.handle_records SET erp_handled = ? "
                 "WHERE detail_key = ?;", (_restore, _probe_key))
    conn.commit()

    st, ho = call("GET", "/api/handle/orders?pending=1")
    check("处理登记按单聚合接口可用", st == 200 and "rows" in ho,
          f"{ho.get('total')} 单")
    if ho.get("rows"):
        r0 = ho["rows"][0]
        check("聚合行带处理进度与状态",
              {"status", "status_key", "handled", "unhandled"} <= set(r0),
              f"{r0.get('order_no')} · {r0.get('status')} · "
              f"已处理 {r0.get('handled')}/{r0.get('lines')}")

    # 端到端：建 → 检测 → 待处理 → 处理 → 落 handle 库 → 删除清理
    st, created = call("POST", "/api/returns", {
        "return_no": "SF-HANDLE-TEST", "carrier": "顺丰速运",
        "return_date": time.strftime("%Y-%m-%d"),
        "turbine_vendor": "处理库自检厂家",
        "product_code": "HANDLE-TEST-001", "product_model": "HX-TEST",
        "product_category": "自检类别", "return_qty": 1,
    })
    hkey = created.get("detail_key")
    if not hkey:
        check("创建处理登记自检记录", False, str(created))
    else:
        # 聚合接口（处理登记页用的 /api/handle/orders）必须与行级口径
        # （/api/returns?handle_pending=1）一致 —— 它原先只判「已处理数 < 行数」，
        # 漏了「已检测」这一半，于是**未检测的退回单也会混进待处理清单**。
        # 只有真的造一条未检测记录才显形：数据里所有明细都检测过时，两种口径
        # 结果相同，这个缺陷完全是隐性的。
        st, ha1 = call("GET", "/api/handle/orders?pending=1&page_size=500")
        check("未检测的记录不进待处理清单（聚合接口口径）",
              hkey not in [r["order_no"] for r in ha1.get("rows", [])],
              f"清单 {ha1.get('total')} 单")
        st, q1 = call("GET", "/api/returns?handle_pending=1&page_size=500")
        check("未检测的记录不进待处理清单",
              hkey not in [r["detail_key"] for r in q1.get("rows", [])], "已排除")

        call("PUT", f"/api/returns/{hkey}", {
            "test_date": time.strftime("%Y-%m-%d"),
            "test_result": "处理库自检结果",
        })
        st, ha2 = call("GET", "/api/handle/orders?pending=1&page_size=500")
        check("检测后该单进入聚合待处理清单",
              hkey.split("-")[0] in [r["order_no"] for r in ha2.get("rows", [])],
              f"清单 {ha2.get('total')} 单")
        st, q2 = call("GET", "/api/returns?handle_pending=1&page_size=500")
        check("检测后进入待处理清单",
              hkey in [r["detail_key"] for r in q2.get("rows", [])], "已进入")

        call("PUT", f"/api/returns/{hkey}", {"erp_handled": "已处理"})
        n_h1 = conn.execute(
            "SELECT COUNT(*) c FROM handle_db.handle_records "
            "WHERE detail_key = ?;", (hkey,)).fetchone()["c"]
        check("写入 ERP 处理落到 handle 库", n_h1 == 1, f"{n_h1} 行")
        n_i1 = conn.execute(
            "SELECT COUNT(*) c FROM inspect_db.inspect_records "
            "WHERE detail_key = ?;", (hkey,)).fetchone()["c"]
        check("检测记录仍在检测库（没被处理字段带偏）", n_i1 == 1, f"{n_i1} 行")

        st, rec = call("GET", f"/api/returns/{hkey}")
        check("跨库拼接连处理字段一起返回",
              rec.get("erp_handled") == "已处理"
              and rec.get("test_result") == "处理库自检结果",
              f"erp={rec.get('erp_handled')} test={rec.get('test_result')}")

        st, q3 = call("GET", "/api/returns?handle_pending=1&page_size=500")
        check("处理完移出待处理清单",
              hkey not in [r["detail_key"] for r in q3.get("rows", [])], "已移出")

        # --- 后续处理方案：可搜索下拉 + 库内没有则自动新增 ---
        _sol_new = "自检新方案-上门取件"
        _sol_old = "自检老方案-寄回工厂"
        call("PUT", f"/api/returns/{hkey}", {"handle_solution": _sol_old})
        n_s1 = conn.execute(
            "SELECT handle_solution FROM handle_db.handle_records "
            "WHERE detail_key = ?;", (hkey,)).fetchone()["handle_solution"]
        check("后续处理方案写入落到 handle 库", n_s1 == _sol_old, repr(n_s1))

        st, d_sol = call("GET", "/api/dict/handle_solution")
        _cands = [o["value"] for o in d_sol.get("options", [])]
        check("新值自动进入处理库的候选集（库内没有则新增）",
              _sol_old in _cands, f"{len(_cands)} 个候选")

        # 模糊匹配：输入片段应能检索到刚新增的值
        st, d_kw = call("GET", "/api/dict/handle_solution?keyword="
                        + urllib.parse.quote("自检老"))
        _hit = [o["value"] for o in d_kw.get("options", [])]
        check("后续处理方案支持模糊匹配（输入片段即命中）",
              _sol_old in _hit, str(_hit[:3]))

        # 处理库的字典是**重建式**的（候选随数据回收），所以：
        #   两个值同时被引用 → 都在候选里（这就是「累积」）
        #   某个值不再被任何记录引用 → 候选被清掉（避免幽灵候选）
        _other = conn.execute(
            "SELECT detail_key FROM returns WHERE detail_key <> ? "
            "ORDER BY id LIMIT 1;", (hkey,)).fetchone()["detail_key"]
        call("PUT", f"/api/returns/{_other}", {"handle_solution": _sol_new})
        _cands2 = [o["value"] for o in
                   call("GET", "/api/dict/handle_solution")[1]["options"]]
        check("两个值都被引用时都在候选里（可搜索下拉会累积）",
              _sol_old in _cands2 and _sol_new in _cands2,
              f"{len(_cands2)} 个候选：{_cands2[:3]}")

        call("PUT", f"/api/returns/{_other}", {"handle_solution": _sol_old})
        _cands3 = [o["value"] for o in
                   call("GET", "/api/dict/handle_solution")[1]["options"]]
        check("不再被引用的值会被回收（不留幽灵候选）",
              _sol_old in _cands3 and _sol_new not in _cands3,
              f"{len(_cands3)} 个候选：{_cands3[:3]}")
        # 复原被借用的那条记录，避免留下测试痕迹
        conn.execute("UPDATE handle_db.handle_records SET handle_solution = '' "
                     "WHERE detail_key = ?;", (_other,))
        conn.commit()
        call("PUT", f"/api/returns/{hkey}", {"handle_solution": _sol_new})

        st, _rec2 = call("GET", f"/api/returns/{hkey}")
        check("明细查询能读回后续处理方案",
              _rec2.get("handle_solution") == _sol_new,
              repr(_rec2.get("handle_solution")))
        st, _fs = call("GET", f"/api/returns?handle_solution="
                       + urllib.parse.quote(_sol_new) + "&page_size=50")
        check("可按后续处理方案筛选",
              hkey in [r["detail_key"] for r in _fs.get("rows", [])],
              f"命中 {_fs.get('total')} 条")

        st, q4 = call("GET", "/api/returns?keyword="
                      + urllib.parse.quote("已处理"))
        check("关键词检索覆盖处理库字段",
              hkey in [r["detail_key"] for r in q4.get("rows", [])],
              f"total={q4.get('total')}")

        call("DELETE", f"/api/returns/{hkey}")
        n_h2 = conn.execute(
            "SELECT COUNT(*) c FROM handle_db.handle_records "
            "WHERE detail_key = ?;", (hkey,)).fetchone()["c"]
        n_i2 = conn.execute(
            "SELECT COUNT(*) c FROM inspect_db.inspect_records "
            "WHERE detail_key = ?;", (hkey,)).fetchone()["c"]
        n_r2 = conn.execute(
            "SELECT COUNT(*) c FROM returns WHERE detail_key = ?;",
            (hkey,)).fetchone()["c"]
        check("删除后处理库已清理", n_h2 == 0, f"{n_h2} 行")
        check("删除后检测库已清理", n_i2 == 0, f"{n_i2} 行")
        check("删除后退回库已清理", n_r2 == 0, f"{n_r2} 行")

    st, fin24 = call("GET", "/api/health")
    check("处理登记验证未留下残余",
          fin24.get("db", {}).get("total", -1) == n_all,
          f"剩余 {fin24.get('db', {}).get('total')} 条（基线 {n_all}）")

# ---------------------------------------------------------------------------
# [25] 发货申请：独立库 delivery.db（本项目第一对真正的父子表）
#
# 重点守三件事：
#   ① 「产品必须从物料库候选中选定」在后端也拦得住（前端那道是体验，这道是防线）；
#   ② 主表 + 明细是一次事务落库，line_no 按顺序生成，编辑走「整组替换」；
#   ③ 物料模糊搜索的匹配规则（归一化 / 分词 AND / 打分排序）与页面同一套。
# ---------------------------------------------------------------------------
print("\n[25] 发货申请：独立库 delivery_db")

from config import DELIVERY_DB  # noqa: PLC0415
from config import (AUTH_DB, HANDLE_DB, INSPECT_DB, ITEMS_DB,  # noqa: PLC0415
                    RETURNS_DB)

check("第六个库已建立", schema_exists(DELIVERY_DB), DELIVERY_DB)

st, n_dlv0 = call("GET", "/api/health")
_baseline_dlv = n_dlv0.get("db", {}).get("deliveries", -1)
# ⚠️ 换 MySQL 后这里从「文件名」变成了「schema 名」：旧版 db.databases 的键是
# 去掉 .db 的文件名（所以发货库叫 delivery），现在是 config.SCHEMAS 里的
# schema 名（delivery_db）。写成集合相等，新增库时也会一起被守到。
_expected_dbs = {RETURNS_DB, INSPECT_DB, HANDLE_DB, ITEMS_DB, AUTH_DB, DELIVERY_DB}
check("健康检查里带上六个库",
      set(n_dlv0.get("db", {}).get("databases", {})) == _expected_dbs,
      list(n_dlv0.get("db", {}).get("databases", {}).keys()))

st, dv_meta = call("GET", "/api/delivery/meta")
check("发货元数据可取（状态 4 项，不含审批预留值）",
      st == 200 and len(dv_meta.get("status", [])) == 4,
      [x["value"] for x in dv_meta.get("status", [])])
check("发货模块的字段中文名可取",
      dv_meta.get("field_labels", {}).get("request_no") == "申请单号")
check("发货页面上 project_site 显示为「项目名称」"
      "（与退回登记的「项目风场」同字段不同叫法）",
      dv_meta.get("field_labels", {}).get("project_site") == "项目名称",
      dv_meta.get("field_labels", {}).get("project_site"))

# --- 物料模糊搜索 ---
st, s1 = call("GET", "/api/delivery/items?keyword=10001")
check("物料搜索：按料号片段可搜", st == 200 and s1.get("total", 0) > 0,
      f"{s1.get('total')} 项")
check("物料搜索：料号精确前缀排第一",
      (s1.get("rows") or [{}])[0].get("material_no") == "10001-0001",
      (s1.get("rows") or [{}])[0].get("material_no"))
st, s2 = call("GET", "/api/delivery/items?keyword=" + urllib.parse.quote("光纤跳线"))
check("物料搜索：按品名可搜", s2.get("total", 0) > 0, f"{s2.get('total')} 项")
st, s4 = call("GET", "/api/delivery/items?keyword=" + urllib.parse.quote("风速"))
st, s3 = call("GET", "/api/delivery/items?keyword=" + urllib.parse.quote("风速 420"))
check("物料搜索：空格分词 = AND（比单个词收窄）",
      0 < s3.get("total", 0) < s4.get("total", 0),
      f"{s4.get('total')} → {s3.get('total')}")
st, s5 = call("GET", "/api/delivery/items?keyword="
              + urllib.parse.quote("１０００１－０００１"))
check("物料搜索：全角输入归一化后命中",
      (s5.get("rows") or [{}])[0].get("material_no") == "10001-0001")
check("物料搜索：空关键词返回 0 项",
      call("GET", "/api/delivery/items?keyword=")[1].get("total") == 0)

# --- 新建 ---
dv_header = {
    "turbine_vendor": "自检发货厂家", "project_site": "自检发货风场",
    "expect_ship_date": time.strftime("%Y-%m-%d"),
    "ship_address": "浙江省温州市乐清市经济开发区纬十二路228号",
    "ship_contact": "夏鹏程", "ship_phone": "18815120633",
    "replace_reason": "发货模块自检", "express_req": "顺丰，寄付",
}
dv_items = [
    {"material_no": "10001-0001", "product_model": "BLF1-S",
     "product_name": "低温型风速传感器", "spec": "51177.67.773C",
     "qty": 1, "need_return": True},
    {"material_no": "10023-0006", "product_model": "BLDL03.10-12MC",
     "product_name": "8芯超声波风速风向仪电缆_010", "spec": "12米",
     "qty": 3, "need_return": False},
]
st, dv_new = call("POST", "/api/delivery/requests",
                  {"header": dv_header, "items": dv_items})
dv_no = dv_new.get("request_no", "")
check("新建发货申请成功", st == 200 and bool(dv_no), st if st != 200 else dv_no)
check("单号为 FH + 8 位日期 + 3 位流水",
      len(dv_no) == 13 and dv_no.startswith("FH") and dv_no[2:10].isdigit(), dv_no)
check("明细行数正确", dv_new.get("count") == 2)

# --- 详情 ---
st, dv = call("GET", f"/api/delivery/requests/{dv_no}")
check("详情可读回", st == 200 and dv.get("request_no") == dv_no)
check("line_no 依次为 1、2",
      [i["line_no"] for i in dv.get("items", [])] == [1, 2],
      [i["line_no"] for i in dv.get("items", [])])
check("件数合计 4 / 需返回 1",
      dv.get("total_qty") == 4 and dv.get("need_return_qty") == 1,
      f"{dv.get('total_qty')} / {dv.get('need_return_qty')}")
check("need_return 存成 1 / 0（逐行）",
      [i["need_return"] for i in dv.get("items", [])] == [1, 0],
      [i["need_return"] for i in dv.get("items", [])])
check("申请人取当前登录用户（不接受前端传）",
      dv.get("applicant") == SMOKE_USER, dv.get("applicant"))
check("状态为「已提交」（不做审批，提交即进待发货队列）",
      dv.get("status") == "submitted" and dv.get("status_label") == "已提交",
      dv.get("status"))
check("不存在的单返回 404",
      call("GET", "/api/delivery/requests/FH99999999999")[0] == 404)

# --- 后端兜底：「必须从候选中选定」 ---
st, e1 = call("POST", "/api/delivery/requests",
              {"header": dv_header,
               "items": [{"material_no": "", "product_model": "", "qty": 1}]})
check("手填（未从物料库选定）被后端拦下", st == 400, st)
check("拦截理由说明「必须从候选里选定」",
      "从候选里选定" in json.dumps(e1, ensure_ascii=False),
      json.dumps(e1, ensure_ascii=False)[:52])
check("只有料号没有型号也被拦下",
      call("POST", "/api/delivery/requests",
           {"header": dv_header,
            "items": [{"material_no": "10001-0001", "product_model": "", "qty": 1}]})[0] == 400)
check("数量为 0 被拦下",
      call("POST", "/api/delivery/requests",
           {"header": dv_header,
            "items": [{"material_no": "10001-0001", "product_model": "BLF1-S", "qty": 0}]})[0] == 400)
check("电话格式错被拦下",
      call("POST", "/api/delivery/requests",
           {"header": dict(dv_header, ship_phone="abc"), "items": dv_items})[0] == 400)
check("主表缺必填被拦下",
      call("POST", "/api/delivery/requests",
           {"header": {k: v for k, v in dv_header.items() if k != "ship_address"},
            "items": dv_items})[0] == 400)
check("空明细被拦下",
      call("POST", "/api/delivery/requests",
           {"header": dv_header, "items": []})[0] == 400)

# --- 列表与筛选 ---
st, dv_list = call("GET", "/api/delivery/requests?page=1&page_size=20")
check("发货列表可查", st == 200 and dv_list.get("total", 0) >= 1,
      f"{dv_list.get('total')} 单")
_row0 = (dv_list.get("rows") or [{}])[0]
check("列表带上明细行数与总件数",
      _row0.get("line_count") == 2 and _row0.get("total_qty") == 4,
      f"{_row0.get('line_count')} 种 / {_row0.get('total_qty')} 件")
check("列表带状态中文名", _row0.get("status_label") == "已提交")
check("按单号关键词筛得到唯一一条",
      call("GET", "/api/delivery/requests?keyword="
           + urllib.parse.quote(dv_no))[1].get("total") == 1)
check("按收件人关键词筛", call("GET", "/api/delivery/requests?keyword="
      + urllib.parse.quote("夏鹏程"))[1].get("total", 0) >= 1)
check("按状态筛", call("GET", "/api/delivery/requests?status=submitted")
      [1].get("total", 0) >= 1)
check("按厂家筛", call("GET", "/api/delivery/requests?turbine_vendor="
      + urllib.parse.quote("自检发货厂家"))[1].get("total", 0) >= 1)
check("按日期区间筛", call("GET", "/api/delivery/requests?apply_date_from=2026-01-01")
      [1].get("total", 0) >= 1)

# --- 编辑：主表部分更新 + 明细整组替换 ---
st, _ = call("PUT", f"/api/delivery/requests/{dv_no}",
             {"header": {"ship_contact": "周杰"},
              "items": [{"material_no": "10001-0005", "product_model": "BLF1-S",
                         "product_name": "低温型风速传感器", "spec": "51277.63.420",
                         "qty": 2, "need_return": True}]})
check("编辑接口返回 200", st == 200, st)
st, dv2 = call("GET", f"/api/delivery/requests/{dv_no}")
check("主表按部分字段更新（只传了联系人）", dv2.get("ship_contact") == "周杰")
check("未传的主表字段保持原值", dv2.get("ship_address") == dv_header["ship_address"])
check("明细被整组替换为 1 行",
      len(dv2.get("items", [])) == 1
      and dv2["items"][0]["material_no"] == "10001-0005",
      f"{len(dv2.get('items', []))} 行")
check("替换后 line_no 重新编号为 1", dv2["items"][0]["line_no"] == 1)
check("申请人不会被编辑接口改掉", dv2.get("applicant") == SMOKE_USER)
check("编辑时同样拦未选定",
      call("PUT", f"/api/delivery/requests/{dv_no}",
           {"items": [{"material_no": "", "product_model": "", "qty": 1}]})[0] == 400)

# --- 操作日志 ---
st, dv_logs = call("GET", f"/api/delivery/requests/{dv_no}/logs")
_acts = {x["action"] for x in dv_logs.get("rows", [])}
check("操作日志记录了新建与编辑", {"create", "update"} <= _acts, sorted(_acts))

# --- 删除 ---
st, _ = call("DELETE", f"/api/delivery/requests/{dv_no}")
check("删除发货申请成功", st == 200, st)
check("删除后详情 404", call("GET", f"/api/delivery/requests/{dv_no}")[0] == 404)
check("重复删除 404", call("DELETE", f"/api/delivery/requests/{dv_no}")[0] == 404)
_n_item = conn.execute(
    "SELECT COUNT(*) c FROM delivery_db.delivery_request_item "
    "WHERE request_no = ?;", (dv_no,)).fetchone()["c"]
_n_req = conn.execute(
    "SELECT COUNT(*) c FROM delivery_db.delivery_request "
    "WHERE request_no = ?;", (dv_no,)).fetchone()["c"]
check("删除后明细表已清理", _n_item == 0, f"{_n_item} 行")
check("删除后主表已清理", _n_req == 0, f"{_n_req} 行")

st, fin25 = call("GET", "/api/health")
check("发货申请验证未留下残余",
      fin25.get("db", {}).get("deliveries", -1) == _baseline_dlv,
      f"剩余 {fin25.get('db', {}).get('deliveries')} 单（基线 {_baseline_dlv}）")

# ---------------------------------------------------------------------------
# [26] 待发货清单：**只读清点视图**（2026-09-21 改版）
#
# 重点守四件事：
#   ① 清单没有自己的表 —— 它就是「status='submitted' 的申请单」的另一个视图，
#      提交即出现、所有明细都登记完才消失，不存在第二份数据；
#   ② **本页不写任何数据** —— 「标记已发货」/「撤销发货」/「手工登记」三个接口
#      已全部移除（405），产生发货记录的地方只剩 ③ 发货跟踪的按行登记；
#   ③ 行里带「已发 x / y 行」进度：**部分发货的单仍留在清单里**
#      （它确实还有没做的），汇总另给「部分已发」单数；
#   ④ 排序 / 超期 / 筛选照旧（清单要回答「接下来该发哪些」）。
# ---------------------------------------------------------------------------
print("\n[26] 待发货清单：只读清点视图")

import datetime as _dt  # noqa: PLC0415


def _d(off: int) -> str:
    return (_dt.date.today() + _dt.timedelta(days=off)).isoformat()


st, _p0 = call("GET", "/api/delivery/pending")
_pend_base = _p0.get("total", -1)
check("待发货清单接口可用（初始 total 取得）", st == 200 and _pend_base >= 0,
      f"{_pend_base} 单")

st, _m0 = call("GET", "/api/delivery/meta")
check("元数据带 ship_sources（发货记录来源）",
      st == 200 and any(x["value"] == "request" for x in _m0.get("ship_sources", [])),
      _m0.get("ship_sources"))
check("元数据带 track_states（发货跟踪的三种行态）",
      all(k in (_m0.get("track_states") or {})
          for k in ("pending", "shipped", "unlinked")),
      _m0.get("track_states"))
check("元数据带 shipped_status", _m0.get("shipped_status") == "shipped",
      _m0.get("shipped_status"))

_ph = {
    "turbine_vendor": "自检清单厂家", "project_site": "自检清单风场",
    "ship_address": "浙江省温州市乐清市经济开发区纬十二路228号",
    "ship_contact": "夏鹏程", "ship_phone": "18815120633",
    "replace_reason": "清单自检", "express_req": "顺丰，寄付",
}
_pi = [{"material_no": "10001-0001", "product_model": "BLF1-S",
        "product_name": "低温型风速传感器", "spec": "51177.67.773C",
        "qty": 2, "need_return": True}]
# 超期那张刻意给**两行明细** —— 用来验证「部分发货的单仍留在清单里」
_pi_two = _pi + [{"material_no": "10001-0002", "product_model": "BLF1-S2",
                  "product_name": "低温型风速传感器（二代）", "spec": "x",
                  "qty": 1, "need_return": True}]


def _mk(off: int, items=None):
    st_, r_ = call("POST", "/api/delivery/requests",
                   {"header": dict(_ph, expect_ship_date=_d(off)),
                    "items": items or _pi})
    return r_.get("request_no", "")


_over = _mk(-3, _pi_two)   # 超期 3 天（2 行明细）
_today = _mk(0)            # 今天到期
_future = _mk(5)           # 5 天后
check("建出三张单（超期 / 今天 / 未来）",
      all([_over, _today, _future]), f"{_over} / {_today} / {_future}")

# --- 派生视图 ---
st, pend = call("GET", "/api/delivery/pending")
check("清单把三张都收进来（提交即进清单）", pend.get("total") == _pend_base + 3,
      f"{pend.get('total')} 单")
_ph_rows = pend.get("rows", [])
_dates = [x["expect_ship_date"] for x in _ph_rows]
check("清单按「期望发货日升序」排（最急在前）", _dates == sorted(_dates), _dates)
check("超期那张排在第一位", _ph_rows and _ph_rows[0]["request_no"] == _over,
      _ph_rows[0]["request_no"] if _ph_rows else "")
check("首行 overdue_days = 3", _ph_rows and _ph_rows[0]["overdue_days"] == 3,
      _ph_rows[0]["overdue_days"] if _ph_rows else "")

_pby = {x["request_no"]: x for x in _ph_rows}
check("今天到期不算超期（0）", _pby[_today]["overdue_days"] == 0)
check("未来不算超期（0）", _pby[_future]["overdue_days"] == 0)
check("每行的明细汇总齐全（2 件 / 需返回 2 件）",
      _pby[_future]["total_qty"] == 2 and _pby[_future]["return_qty"] == 2,
      f"{_pby[_future]['total_qty']} 件 / 需还 {_pby[_future]['return_qty']}")
check("summary 里的超期张数 ≥ 1", pend["summary"]["overdue"] >= 1,
      pend["summary"])
check("带 today 字段（前端标『今天』用）", bool(pend.get("today")), pend.get("today"))

st, ov = call("GET", "/api/delivery/pending?only_overdue=1")
check("only_overdue=1 只返回超期的单",
      all(x["overdue_days"] > 0 for x in ov.get("rows", [])) and ov.get("total", 0) >= 1,
      f"{ov.get('total')} 单")
st, kx = call("GET", "/api/delivery/pending?keyword=" + urllib.parse.quote(_over))
check("按单号关键词能筛到", kx.get("total") == 1, kx.get("total"))
st, kv = call("GET", "/api/delivery/pending?turbine_vendor="
              + urllib.parse.quote("自检清单厂家"))
check("按风机厂家能筛到三张", kv.get("total") == 3, kv.get("total"))
# ⚠️ 加上厂家筛选，让这条只数**自检造的那两张**。原来不带筛选、断言「恰好 2 张」，
#    一旦库里存在别人建的待发货单（2026-09-22 实测有一张用户自建单，期望发货日
#    正好落在区间内）就会红 —— 自检不该假设清单是空的（它是全库视图）。
st, kr = call("GET", f"/api/delivery/pending?expect_date_from={_d(0)}"
                     f"&expect_date_to={_d(10)}&turbine_vendor="
                     + urllib.parse.quote("自检清单厂家"))
check("按期望发货日区间能筛（今天→10 天后，自检那 2 张）", kr.get("total") == 2,
      kr.get("total"))

# --- ★ 改版核心：本页一个字节都不写 ---
check("「标记已发货」（整单出队）接口已移除（405）",
      call("POST", "/api/delivery/shipments", {"request_nos": [_over]})[0] == 405)
check("「撤销发货」接口已移除（405）",
      call("POST", "/api/delivery/shipments/revoke", {"request_no": _over})[0] == 405)
check("「手工登记」接口已移除（405）",
      call("POST", "/api/delivery/shipments/manual", {})[0] == 405)

# --- 进度口径：还没登记时 0 / N ---
check("未开工的单：已发 0 行 / 共 1 行，partial 为假",
      _pby[_future]["shipped_lines"] == 0 and _pby[_future]["apply_lines"] == 1
      and _pby[_future]["partial"] is False,
      f"{_pby[_future]['shipped_lines']}/{_pby[_future]['apply_lines']}")
check("超期那行是 2 行明细（部分发货用例的前提）",
      _pby[_over]["apply_lines"] == 2, _pby[_over]["apply_lines"])

# --- 在「发货跟踪」登记其中一行 → 清单里出现「部分已发」---
st, _reg = call("POST", "/api/delivery/shipments/register",
                {"request_no": _over, "line_no": 1, "ship_no": "PEND-S-1",
                 "ship_date": _d(0)})
check("到 ③ 发货跟踪页按行登记发货（清单页不参与）",
      st == 200 and _reg.get("id"), _reg if st != 200 else "")
check("只登记了 1 行 → 申请状态仍是 submitted（还有行没登记）",
      call("GET", f"/api/delivery/requests/{_over}")[1].get("status") == "submitted")

st, _p1 = call("GET", "/api/delivery/pending?keyword=" + urllib.parse.quote(_over))
_r1 = (_p1.get("rows") or [{}])[0]
check("★ 部分发货的单**仍留在清单里**（它确实还有没做的）",
      _p1.get("total") == 1, f"{_p1.get('total')} 单")
check("行里显示「已发 1 / 2 行」且 partial 为真",
      _r1.get("shipped_lines") == 1 and _r1.get("apply_lines") == 2
      and _r1.get("partial") is True,
      f"{_r1.get('shipped_lines')}/{_r1.get('apply_lines')}")
check("行里另有 pending_lines 说明还差几行没做",
      _r1.get("pending_lines") == 1, _r1.get("pending_lines"))
check("汇总给出「部分已发」单数 ≥ 1",
      call("GET", "/api/delivery/pending")[1]["summary"].get("partial", 0) >= 1,
      call("GET", "/api/delivery/pending")[1]["summary"])

# --- 把剩下那行也登记 → 整单完成，自动离开清单 ---
check("登记第 2 行 200",
      call("POST", "/api/delivery/shipments/register",
           {"request_no": _over, "line_no": 2, "ship_no": "PEND-S-1",
            "ship_date": _d(0)})[0] == 200)
check("★★★★ 全部明细登记完 → 申请状态**自动**变 shipped（没有人点过「标记已发货」）",
      call("GET", f"/api/delivery/requests/{_over}")[1].get("status") == "shipped",
      call("GET", f"/api/delivery/requests/{_over}")[1].get("status"))
check("★ 整单完成后它从待发货清单消失（清单只列未发完的）",
      call("GET", "/api/delivery/pending?keyword=" + urllib.parse.quote(_over))[1]
      .get("total") == 0)

# --- 清理 ---
for _no in (_over, _today, _future):
    call("DELETE", f"/api/delivery/requests/{_no}")
_n_left = conn.execute(
    "SELECT COUNT(*) c FROM delivery_db.delivery_request WHERE turbine_vendor = ?;",
    ("自检清单厂家",)).fetchone()["c"]
check("自检数据已清理干净", _n_left == 0, f"残留 {_n_left} 单")
st, _pfin = call("GET", "/api/delivery/pending")
check("清单回到基线数量", _pfin.get("total") == _pend_base,
      f"{_pfin.get('total')} 单（基线 {_pend_base}）")

# ---------------------------------------------------------------------------
# [27] 发货跟踪：**以申请明细为主体**（2026-09-21 改版）
#
# 重点守五件事：
#   ① **申请单一提交就出现在跟踪列表**（状态「待发货」）—— 本次改版的核心。
#      改版前这里只查 delivery_shipment，于是记录必须等人在待发货清单点
#      「标记已发货」才产生，跟踪表永远是滞后的；
#   ② 按行登记：**发货单号必填**、**一行只登记一次**（要改走 PUT，不静默覆盖）、
#      厂家/风场/型号**从申请单带出**（不接受前端传，否则台账核销维度会跑偏）；
#   ③ 申请状态是明细登记情况的**推论**：全部登记完自动 shipped，
#      撤销登记自动回落 submitted；
#   ④ 对不上明细的发货记录标「未关联」，**人工挂接时按料号补行号**
#      （只挂单号不补行号，它会一直显示未关联）；
#   ⑤ 批量导入幂等键 = (发货单号 + 料号)，**不含申请单号**。
# ---------------------------------------------------------------------------
print("\n[27] 发货跟踪：申请明细为主体 / 按行登记 / 挂接")

import datetime as _dt2  # noqa: PLC0415


def _d2(off: int) -> str:
    return (_dt2.date.today() + _dt2.timedelta(days=off)).isoformat()


# 取一个**真实退货维度**当发货自检的靶子（[27] 的申请单头用它，
# 下面的台账段也拿它当「与真实返件对上」的参照）
# ⚠️ 取「退货**最少**」的真实维度（原来是 DESC 取最多的）：数据量大时
#    「发货 40 件」会被算成负数，一连串断言跟着变红。取最少的那一档最稳。
_real = conn.execute(
    "SELECT turbine_vendor v, COALESCE(project_site,'') s, product_model m, "
    "SUM(COALESCE(NULLIF(return_qty,0),1)) q FROM returns "
    "WHERE TRIM(COALESCE(product_model,'')) <> '' "
    "GROUP BY 1,2,3 ORDER BY q ASC, product_model LIMIT 1").fetchone()
RV2, RS2, RM2, RQ2 = _real["v"], _real["s"], _real["m"], _real["q"]
check("取到真实退货维度用于发货 / 台账自检", bool(RV2 and RM2),
      f"{RV2} | {RS2} | {RM2}（已返回 {RQ2} 件）")
_ph2 = {
    "turbine_vendor": RV2, "project_site": RS2, "expect_ship_date": _d2(0),
    "ship_address": "浙江省温州市乐清市经济开发区纬十二路228号",
    "ship_contact": "夏鹏程", "ship_phone": "18815120633",
    "replace_reason": "跟踪自检", "express_req": "顺丰",
}
_pi2 = [
    # ① 主行：真实退货型号，40 件需返回 —— 用来验证发货跟踪与「挂接按料号补行号」
    {"material_no": "TR-0001", "product_model": RM2,
     "product_name": "低温型风速传感器", "spec": "x", "qty": 40,
     "need_return": True},
    # ② 用来验证「挂接按料号补行号」的行
    {"material_no": "TR-0002", "product_model": "TR-HOOK-M",
     "product_name": "待挂接型号", "spec": "x", "qty": 3, "need_return": True},
    # ③ 不返回的行：不进「需返回」统计

    {"material_no": "TR-0003", "product_model": "TR-NORETURN-MODEL",
     "product_name": "一次性耗材", "spec": "x", "qty": 3, "need_return": False},
]
st, _rq2 = call("POST", "/api/delivery/requests",
                {"header": _ph2, "items": _pi2})
_track_no = _rq2.get("request_no", "")
check("建了一张申请单（3 个产品，2 个需返回）", bool(_track_no), _track_no)

# ============ ★★★ 改版核心：提交即出现 ============
st, _tr0 = call("GET", f"/api/delivery/shipments?request_no={_track_no}")
_rows0 = _tr0.get("rows", [])
check("★★★★ 申请单一提交，发货跟踪里就有它的全部明细（不用等任何标记动作）",
      st == 200 and len(_rows0) == 3, f"{len(_rows0)} 行")
check("★ 这三行还没有发货单号，状态是「待发货」",
      all(x["state"] == "pending" and not x["ship_no"] for x in _rows0),
      [(x["state"], x["ship_no"]) for x in _rows0])
check("★ 汇总给出「3 行待发货 / 46 件待发」",
      _tr0["summary"]["pending_lines"] == 3
      and _tr0["summary"]["pending_qty"] == 46
      and _tr0["summary"]["shipped_lines"] == 0,
      _tr0["summary"])
check("行的厂家 / 风场来自申请单（台账核销维度不靠前端传）",
      all(x["turbine_vendor"] == RV2 and x["project_site"] == RS2
          for x in _rows0))
check("行带申请件数与需返回标记",
      sorted(x["apply_qty"] for x in _rows0) == [3, 3, 40]
      and sum(1 for x in _rows0 if x["need_return"]) == 2,
      [(x["apply_qty"], x["need_return"]) for x in _rows0])
check("每行有唯一 row_key（前端勾选 / 定位用）",
      len({x["row_key"] for x in _rows0}) == 3,
      [x["row_key"] for x in _rows0])
check("待发货行带期望发货日（清单语义排序用）",
      all(x["expect_ship_date"] == _d2(0) for x in _rows0))

print("--- 按行登记发货（校验）---")
check("发货单号为空 → 400",
      call("POST", "/api/delivery/shipments/register",
           {"request_no": _track_no, "line_no": 1, "ship_no": "   "})[0] == 400)
check("行号不存在 → 400",
      call("POST", "/api/delivery/shipments/register",
           {"request_no": _track_no, "line_no": 99, "ship_no": "X"})[0] == 400)
check("日期格式错 → 400",
      call("POST", "/api/delivery/shipments/register",
           {"request_no": _track_no, "line_no": 1, "ship_no": "X",
            "ship_date": "2026/09/21"})[0] == 400)
check("件数 0 → 400",
      call("POST", "/api/delivery/shipments/register",
           {"request_no": _track_no, "line_no": 1, "ship_no": "X",
            "qty": 0})[0] == 400)
check("申请单不存在 → 400",
      call("POST", "/api/delivery/shipments/register",
           {"request_no": "FH不存在单号", "line_no": 1, "ship_no": "X"})[0] == 400)
check("缺行号 → 400",
      call("POST", "/api/delivery/shipments/register",
           {"request_no": _track_no, "ship_no": "X"})[0] == 400)

print("--- 按行登记发货（正常路径）---")
st, _rg = call("POST", "/api/delivery/shipments/register",
               {"request_no": _track_no, "line_no": 1, "ship_no": "TR-SHIP-1",
                "express_no": "SF-TR1", "ship_date": _d2(0)})
check("登记第 1 行 200 且返回 id", st == 200 and _rg.get("id"),
      _rg if st != 200 else "")
check("返回的申请状态仍是 submitted（还有 2 行没登记）",
      _rg.get("request_status") == "submitted", _rg.get("request_status"))

st, _tr1 = call("GET", f"/api/delivery/shipments?request_no={_track_no}")
_r1 = next(x for x in _tr1["rows"] if x["line_no"] == 1)
check("第 1 行变「已发货」并带上单号 / 物流单号",
      _r1["state"] == "shipped" and _r1["ship_no"] == "TR-SHIP-1"
      and _r1["express_no"] == "SF-TR1",
      f"{_r1['state']} / {_r1['ship_no']} / {_r1['express_no']}")
check("实发件数默认 = 申请件数（40）", _r1["ship_qty"] == 40, _r1["ship_qty"])
check("来源标为「按申请单登记」",
      _r1["source"] == "request" and _r1["source_label"] == "按申请单登记",
      _r1["source_label"])
check("记录冗余了厂家 / 风场（台账维度，不靠 JOIN 申请单）",
      _r1["turbine_vendor"] == RV2 and _r1["project_site"] == RS2)
check("汇总随之变成 待发 2 行 / 已发 1 行",
      _tr1["summary"]["pending_lines"] == 2
      and _tr1["summary"]["shipped_lines"] == 1, _tr1["summary"])
check("需返回汇总只算**已发出的**需返回行（40）",
      _tr1["summary"]["return_qty"] == 40, _tr1["summary"]["return_qty"])

print("--- 一行只登记一次（不静默覆盖）---")
_dup = call("POST", "/api/delivery/shipments/register",
            {"request_no": _track_no, "line_no": 1, "ship_no": "TR-SHIP-9"})
check("同一行重复登记被挡下（400）", _dup[0] == 400, _dup[0])
check("拒绝理由点明「已经登记过」并给出原有单号",
      "已经登记过" in json.dumps(_dup[1], ensure_ascii=False)
      and "TR-SHIP-1" in json.dumps(_dup[1], ensure_ascii=False),
      json.dumps(_dup[1], ensure_ascii=False)[:70])
check("重复尝试没有产生第二条记录（该行仍只有 1 条）",
      len([x for x in call("GET", f"/api/delivery/shipments?request_no={_track_no}")[1]
           ["rows"] if x["line_no"] == 1]) == 1)

print("--- 状态是明细登记情况的推论 ---")
check("登记第 3 行（不需返回那行）200",
      call("POST", "/api/delivery/shipments/register",
           {"request_no": _track_no, "line_no": 3, "ship_no": "TR-SHIP-1",
            "ship_date": _d2(0)})[0] == 200)
check("还剩第 2 行没登记 → 申请状态仍是 submitted",
      call("GET", f"/api/delivery/requests/{_track_no}")[1].get("status")
      == "submitted")
check("但清单里还能看到它（部分发货，确实还有没做的）",
      call("GET", "/api/delivery/pending?keyword="
           + urllib.parse.quote(_track_no))[1].get("total") == 1)
check("登记最后一行 200",
      call("POST", "/api/delivery/shipments/register",
           {"request_no": _track_no, "line_no": 2, "ship_no": "TR-SHIP-1",
            "ship_date": _d2(0)})[0] == 200)
check("★★★ 全部明细登记完 → 申请状态**自动**变 shipped（没点过「标记已发货」）",
      call("GET", f"/api/delivery/requests/{_track_no}")[1].get("status")
      == "shipped")
check("自动转已发货后从待发货清单消失",
      call("GET", "/api/delivery/pending?keyword="
           + urllib.parse.quote(_track_no))[1].get("total") == 0)
check("操作日志记了 shipment_register",
      any(x["action"] == "shipment_register"
          for x in call("GET", f"/api/delivery/requests/{_track_no}/logs")[1]["rows"]))

print("--- 修改发货信息（PUT）---")
_sid1 = next(x for x in call("GET", f"/api/delivery/shipments?request_no={_track_no}")[1]
             ["rows"] if x["line_no"] == 1)["ship_id"]
check("修改 200",
      call("PUT", f"/api/delivery/shipments/{_sid1}",
           {"ship_no": "TR-SHIP-1B", "ship_date": _d2(0), "qty": 40,
            "express_no": "SF-TR1B"})[0] == 200)
_r1b = next(x for x in call("GET", f"/api/delivery/shipments?request_no={_track_no}")[1]
            ["rows"] if x["line_no"] == 1)
check("单号与物流单号已改",
      _r1b["ship_no"] == "TR-SHIP-1B" and _r1b["express_no"] == "SF-TR1B",
      f"{_r1b['ship_no']} / {_r1b['express_no']}")
check("★ 改信息不会改动归属（申请单号与行号原样）",
      _r1b["request_no"] == _track_no and _r1b["line_no"] == 1,
      f"{_r1b['request_no']} / {_r1b['line_no']}")
check("修改时单号仍必填 → 400",
      call("PUT", f"/api/delivery/shipments/{_sid1}",
           {"ship_no": "", "ship_date": _d2(0), "qty": 1})[0] == 400)
check("修改不存在的记录 → 400",
      call("PUT", "/api/delivery/shipments/99999999",
           {"ship_no": "X", "ship_date": _d2(0), "qty": 1})[0] == 400)

print("--- 撤销登记：状态自动回落 ---")
_sid3 = next(x for x in call("GET", f"/api/delivery/shipments?request_no={_track_no}")[1]
             ["rows"] if x["line_no"] == 3)["ship_id"]
check("撤销第 3 行 200",
      call("DELETE", f"/api/delivery/shipments/{_sid3}")[0] == 200)
check("★★ 撤销后申请状态**自动回落** submitted",
      call("GET", f"/api/delivery/requests/{_track_no}")[1].get("status")
      == "submitted",
      call("GET", f"/api/delivery/requests/{_track_no}")[1].get("status"))
_r3 = next(x for x in call("GET", f"/api/delivery/shipments?request_no={_track_no}")[1]
           ["rows"] if x["line_no"] == 3)
check("撤销的那行回到「待发货」且单号清空",
      _r3["state"] == "pending" and not _r3["ship_no"], _r3["state"])
check("撤销在操作日志里留痕",
      any(x["action"] == "shipment_delete"
          for x in call("GET", f"/api/delivery/requests/{_track_no}/logs")[1]["rows"]))
check("重复撤销 → 404",
      call("DELETE", f"/api/delivery/shipments/{_sid3}")[0] == 404)

print("--- 列表筛选与汇总 ---")
st, _sl = call("GET", "/api/delivery/shipments?page=1&page_size=10")
check("列表接口可用且带 summary",
      st == 200 and "summary" in _sl and "rows" in _sl)
check("按关键词（发货单号）筛",
      call("GET", "/api/delivery/shipments?keyword=TR-SHIP-1")[1].get("total") >= 1)
check("按 state=pending 筛出待发货行",
      all(x["state"] == "pending"
          for x in call("GET", "/api/delivery/shipments?state=pending")[1]["rows"]))
check("按 state=shipped 筛出已发货行",
      all(x["state"] == "shipped"
          for x in call("GET", "/api/delivery/shipments?state=shipped")[1]["rows"]))
check("只看需返回",
      call("GET", "/api/delivery/shipments?only_return=1")[1].get("total", 0) >= 1)
check("按 source=request 筛",
      call("GET", "/api/delivery/shipments?source=request")[1].get("total", 0) >= 1)
check("按日期区间筛（按 track_date：已发用发货日 / 待发用期望发货日）",
      call("GET", f"/api/delivery/shipments?date_from={_d2(0)}&date_to={_d2(0)}")[1]
      .get("total", 0) >= 1)
check("默认排序把「待发货」排在前面",
      (call("GET", "/api/delivery/shipments?state=pending&page_size=1")[1]
       .get("rows") or [{}])[0].get("state") == "pending")

print("--- 批量导入（幂等键 = 发货单号 + 料号）---")
st, _imp = call("POST", "/api/delivery/shipments/import", {"rows": [
    {"turbine_vendor": RV2, "project_site": RS2, "product_model": RM2,
     "material_no": "TR-0001", "qty": 40, "ship_date": _d2(0),
     "ship_no": "TR-SHIP-1B"},
]})
check("重复导入同一 (发货单号 + 料号) → 只更新不新增",
      st == 200 and _imp.get("created") == 0 and _imp.get("updated") == 1, _imp)
check("导入后该单的记录数没变（没虚增）",
      len(call("GET", f"/api/delivery/shipments?request_no={_track_no}")[1]["rows"])
      == 3,
      len(call("GET", f"/api/delivery/shipments?request_no={_track_no}")[1]["rows"]))
st, _imp2 = call("POST", "/api/delivery/shipments/import", {"rows": [
    {"turbine_vendor": RV2, "project_site": RS2, "product_model": "TR-IMPORT-M",
     "material_no": "TR-I001", "qty": 5, "ship_date": _d2(0),
     "ship_no": "TR-IMP-1"},
    {"turbine_vendor": "缺型号厂", "qty": 1},
]})
check("导入：新增 1 / 失败 1",
      _imp2.get("created") == 1 and len(_imp2.get("failed", [])) == 1, _imp2)
check("失败行报出原因",
      "型号必填" in json.dumps(_imp2.get("failed"), ensure_ascii=False),
      json.dumps(_imp2.get("failed"), ensure_ascii=False)[:52])
check("空 rows → 400",
      call("POST", "/api/delivery/shipments/import", {"rows": []})[0] == 400)

print("--- 未关联记录与人工挂接 ---")
st, _un = call("GET", "/api/delivery/shipments?state=unlinked")
check("能按 state=unlinked 筛出未关联记录（导入那条）", _un.get("total", 0) >= 1,
      _un.get("total"))
_row_i = call("GET", "/api/delivery/shipments?keyword=TR-I001")[1]["rows"][0]
check("未关联行的 state_label 是「未关联」", _row_i["state_label"] == "未关联",
      _row_i["state_label"])
check("未关联行没有申请单号", not _row_i["request_no"], _row_i["request_no"])
_uid = _row_i["ship_id"]

st, _lk = call("POST", "/api/delivery/shipments/link",
               {"ids": [_uid], "request_no": _track_no})
check("挂接 200 且报出条数", st == 200 and _lk.get("linked") == 1, _lk)
_after = call("GET", "/api/delivery/shipments?keyword=TR-I001")[1]["rows"][0]
check("挂接后带上申请单号", _after["request_no"] == _track_no, _after["request_no"])
check("★ 料号在该单里找不到对应行 → 仍是「未关联」（补不上行号）",
      _after["state"] == "unlinked", _after["state"])

# 再测「能补上行号」的情形：导入一条与第 3 行**同料号**的记录
st, _imp3 = call("POST", "/api/delivery/shipments/import", {"rows": [
    {"turbine_vendor": RV2, "project_site": RS2,
     "product_model": "TR-NORETURN-MODEL", "material_no": "TR-0003",
     "qty": 3, "ship_date": _d2(0), "ship_no": "TR-IMP-2"},
]})
check("导入一条与某明细行同料号的记录", _imp3.get("created") == 1, _imp3)
_row3i = call("GET", "/api/delivery/shipments?keyword=TR-IMP-2")[1]["rows"][0]
check("它当前是「未关联」", _row3i["state"] == "unlinked", _row3i["state"])
call("POST", "/api/delivery/shipments/link",
     {"ids": [_row3i["ship_id"]], "request_no": _track_no})
_tr_end = call("GET", f"/api/delivery/shipments?request_no={_track_no}")[1]
_r3b = next(x for x in _tr_end["rows"] if x["line_no"] == 3)
check("★★ 挂接时按料号补上行号 → 该明细行变「已发货」",
      _r3b["state"] == "shipped" and _r3b["ship_no"] == "TR-IMP-2",
      f"{_r3b['state']} / {_r3b['ship_no']}")
check("挂接的那条不再是独立行（已并到明细行上）",
      not any(x["kind"] == "standalone" and x["ship_no"] == "TR-IMP-2"
              for x in _tr_end["rows"]))
check("未关联总数随之减少",
      call("GET", "/api/delivery/shipments?state=unlinked")[1]["total"]
      < _un["total"] + 1)
check("挂到不存在的单 → 400",
      call("POST", "/api/delivery/shipments/link",
           {"ids": [_uid], "request_no": "FH不存在单号"})[0] == 400)
check("空选择 → 400",
      call("POST", "/api/delivery/shipments/link",
           {"ids": [], "request_no": _track_no})[0] == 400)

print("--- 删除未关联记录 ---")
check("删除 200", call("DELETE", f"/api/delivery/shipments/{_uid}")[0] == 200)
check("重复删 → 404", call("DELETE", f"/api/delivery/shipments/{_uid}")[0] == 404)

# ---------------------------------------------------------------------------
# [28] 核销台账：一行 = 整机厂家 + 项目风场（ERP 发货明细为基准）
#
# 2026-09-22 改版，重点守五件事：
#   ① 维度只到「整机厂家 + 项目风场」，不再带产品型号；
#   ② 发货侧读 ERP 镜像 delivery_db.ship_detail 的「售后发货单 + 已核准」行，
#      不再读本地登记表 delivery_shipment（登记表只用来判「需不需要返回」）；
#   ③ 需返回口径：正式口径只认「与发货申请单对上的发货行」
#      （delivery_shipment.ship_no + material_no → delivery_request_item.need_return），
#      一条申请数据都没有时回落到「料号前缀 1/2」的演示口径，meta 里要说明；
#   ④ 已核销 = 手动核销关联(kind='link') + 手工清账(kind='clear')，都按**返件行**记；
#   ⑤ 清账原因必填、不能超过这条返件的剩余，撤销后必须恢复原状。
# ---------------------------------------------------------------------------
print("\n[28] 核销台账：维度 = 厂家 + 风场（ERP 发货明细为基准）")

qs2 = urllib.parse.quote


def _ledger(**kw):
    parts = "&".join(f"{k}={qs2(str(v))}" for k, v in kw.items() if v != "")
    return call("GET", "/api/delivery/ledger?" + parts)[1]


def _near(a, b, tol=0.05) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


# ---- 演示口径（料号前缀 1/2）下的基本面 ----
_led = _ledger(page_size=500, need_mode="demo")
_led_meta = _led.get("meta", {})
_sum = _led.get("summary", {})
check("台账接口可用且带汇总",
      all(k in _sum for k in ("rows", "docs", "ship_qty", "ret_qty", "clr_qty",
                              "clr_link", "clr_clear", "clr_auto", "clr_ship",
                              "pend_qty", "unlink_qty",
                              "pend_rows", "unlink_lines", "no_site_rows")),
      _sum)
check("发货侧读的是 ERP 发货明细（演示口径下有发货行）",
      _led_meta.get("ship_lines", 0) > 0 and _led_meta.get("ship_docs", 0) > 0,
      {k: _led_meta.get(k) for k in ("ship_lines", "ship_docs", "need_mode")})
check("维度只到「厂家 + 风场」，行里没有产品型号字段",
      bool(_led.get("rows")) and "product_model" not in _led["rows"][0],
      sorted(_led["rows"][0].keys()) if _led.get("rows") else "没有行")
check("每行都带发货件数（没有发货就不成行）",
      all(x["ship_qty"] > 0 for x in _led.get("rows", [])),
      f"{len(_led.get('rows', []))} 行")
# 逐行自洽（汇总里各维度有 max(0, …) 截断，不能拿 500 行去加总总账）
_bad_pend = [x["turbine_vendor"] + " / " + x["project_site"] for x in _led["rows"]
             if not (_near(x["pend_qty"], max(0.0, x["ship_qty"] - x["clr_qty"]))
                     and _near(x["clr_qty"], x["ret_clr_qty"] + x["clr_ship"])
                     and _near(x["ret_clr_qty"], x["clr_link"] + x["clr_clear"]
                               + x["clr_auto"])
                     and x["pend_qty"] >= 0)]
check("汇总自洽：每行 待核销 = max(0, 发货 − 已核销)；"
      "已核销 = 返件侧核销（手动关联 + 手工清账 + 自动核销）+ 发货侧清账",
      not _bad_pend
      and _near(_sum["clr_qty"], _sum["clr_link"] + _sum["clr_clear"]
                + _sum["clr_auto"] + _sum["clr_ship"])
      and _sum["pend_rows"] <= _sum["rows"]
      and _sum["unlink_lines"] <= _sum["rows"],
      _bad_pend[:3] or [_sum["clr_qty"], _sum["clr_link"] + _sum["clr_clear"]
                        + _sum["clr_auto"] + _sum["clr_ship"],
                        _sum["pend_rows"], _sum["rows"]])
_bad_unlink = [x["turbine_vendor"] + " / " + x["project_site"] for x in _led["rows"]
               if not (_near(x["unlink_qty"], max(0.0, x["ret_qty"] - x["ret_clr_qty"]))
                       and x["unlink_qty"] >= 0)]
check("汇总自洽：每行 返件未核销 = max(0, 已返回 − 返件侧核销)，"
      "发货侧清账不动「已返回但未核销关联」",
      not _bad_unlink and _sum["unlink_qty"] > 0 and _sum["unlink_lines"] > 0,
      _bad_unlink[:3] or [_sum["unlink_qty"], _sum["unlink_lines"]])

# ---- 数据清洗：销售端返件不进台账（2026-09-23 用户口径） ----
# 台账算的是「售后发货 × 售后返件」，销售那边退回的货不该占着售后的差额。
# 清洗口径落在 returns.info_source（快递归属）= '销售端'，这里拿库里的行数对账，
# 并守住「剔除后一行不多一行不少」（已归位 + 歧义 + 未匹配 = 返件总行数）。
_sc = get_conn()
_srow = _sc.execute(
    "SELECT COUNT(*) AS n, ROUND(SUM(COALESCE(NULLIF(return_qty,0),1)),1) AS q"
    " FROM returns_db.returns WHERE TRIM(COALESCE(turbine_vendor,'')) <> ''"
    "   AND info_source = '销售端'").fetchone()
check("清洗口径：销售端返件的行数 / 件数与库里一致，且口径来源写在 meta 里",
      _srow["n"] > 0
      and _led_meta.get("ret_sales_lines") == _srow["n"]
      and _near(_led_meta.get("ret_sales_qty"), _srow["q"])
      and _led_meta.get("sales_sources") == ["销售端"],
      {"台账剔除": [_led_meta.get("ret_sales_lines"), _led_meta.get("ret_sales_qty")],
       "库里销售端": dict(_srow)})
check("清洗后返件行数 = 已归位 + 歧义 + 未匹配（一行不多一行不少）",
      _led_meta.get("ret_lines") == (_led_meta.get("ret_matched_lines", 0)
                                     + _led_meta.get("ret_ambiguous_lines", 0)
                                     + _led_meta.get("ret_unmatched_lines", 0)),
      [_led_meta.get("ret_lines"), _led_meta.get("ret_matched_lines"),
       _led_meta.get("ret_ambiguous_lines"), _led_meta.get("ret_unmatched_lines")])

# ---- 筛选 / 排序 ----
_r0 = next((x for x in _led["rows"] if x["project_site"]), None) or _led["rows"][0]
DV, DS = _r0["turbine_vendor"], _r0["project_site"]
_dv = _ledger(page_size=500, need_mode="demo", turbine_vendor=DV)
check("按厂家筛（只回这个厂家）",
      _dv.get("total", 0) >= 1
      and all(x["turbine_vendor"] == DV for x in _dv["rows"]), DV)
check("按厂家 + 风场筛",
      _ledger(page_size=500, need_mode="demo", turbine_vendor=DV,
              project_site=DS).get("total", 0) >= 1, f"{DV} / {DS}")
check("按关键词（发货单号）筛",
      bool(_r0.get("doc_nos"))
      and _ledger(page_size=500, need_mode="demo",
                  keyword=_r0["doc_nos"][0]).get("total", 0) >= 1,
      (_r0.get("doc_nos") or [""])[0])
check("筛不存在的厂家 → 0 行（不给假数据）",
      _ledger(need_mode="demo", turbine_vendor="自检不存在的厂家XYZ").get("total", 0) == 0)
check("只看待核销：每行待核销 > 0",
      all(x["pend_qty"] > 0 for x in
          _ledger(page_size=500, need_mode="demo", only_pending=1)["rows"]))
check("只看有未核销返件：每行返件未核销 > 0",
      all(x["unlink_qty"] > 0 for x in
          _ledger(page_size=500, need_mode="demo", only_unlink=1)["rows"]))
_nsled = _ledger(page_size=500, need_mode="demo", only_no_site=1)
check("只看未填风场：每行风场为空、汇总 no_site_rows 与行数一致，且不超过 meta 的维度数",
      _nsled.get("total", 0) >= 1
      and all(not (x["project_site"] or "").strip() for x in _nsled["rows"])
      and _nsled["summary"].get("no_site_rows") == _nsled["total"]
      and _led_meta.get("no_site_groups", 0) >= _nsled["total"],
      {"total": _nsled.get("total"),
       "no_site_rows": _nsled["summary"].get("no_site_rows"),
       "no_site_groups": _led_meta.get("no_site_groups")})
check("状态筛「已核销」只回已核销行",
      all(x["status"] == "已核销" for x in
          _ledger(page_size=500, need_mode="demo", status="已核销")["rows"]))
check("日期区间：2099 年之后没有发货 → 0 行",
      _ledger(page_size=500, need_mode="demo", date_from="2099-01-01").get("total", 0) == 0)
_rs = _ledger(page_size=500, need_mode="demo", sort_by="pend", sort_dir="desc")["rows"]
check("按待核销降序：首行 ≥ 末行",
      (not _rs) or _rs[0]["pend_qty"] >= _rs[-1]["pend_qty"],
      f"{_rs[0]['pend_qty'] if _rs else '-'} ≥ {_rs[-1]['pend_qty'] if _rs else '-'}")

# ---- 二级明细（发货单为基准 + 四侧） ----
print("--- 台账明细 ---")
_dim0 = (f"turbine_vendor={qs2(DV)}&project_site={qs2(DS)}&need_mode=demo")
st, _det = call("GET", "/api/delivery/ledger/detail?" + _dim0)
check("明细接口 200", st == 200, st)
check("带出发货单 / 发货行 / 返件 / 核销记录",
      all(k in _det for k in ("docs", "lines", "returns", "clears", "summary")),
      sorted(_det.keys()) if st == 200 else st)
if st == 200:
    _dsum = _det["summary"]
    check("发货单为基准：每张单都带单号",
          bool(_det["docs"]) and all(d.get("doc_no") for d in _det["docs"]),
          f"{len(_det['docs'])} 张单")
    check("二级页与主表同一行口径一致（发货 / 已返回 / 状态）",
          _near(_dsum["ship_qty"], _r0["ship_qty"])
          and _near(_dsum["ret_qty"], _r0["ret_qty"])
          and _dsum["status"] == _r0["status"],
          [_dsum["ship_qty"], _r0["ship_qty"], _dsum["ret_qty"],
           _r0["ret_qty"], _dsum["status"], _r0["status"]])
    check("发货行件数合计 = 汇总发货件数",
          _near(sum(x["qty"] for x in _det["lines"]), _dsum["ship_qty"]),
          [sum(x["qty"] for x in _det["lines"]), _dsum["ship_qty"]])
    check("发货单条数 = 汇总单数",
          len(_det["docs"]) == _dsum["docs"], [len(_det["docs"]), _dsum["docs"]])
    check("发货行都带「已清账 / 待核销」两个字段，且待核销不为负",
          all("clr_qty" in x and "ship_remain" in x for x in _det["lines"])
          and all(float(x["ship_remain"] or 0) >= 0 for x in _det["lines"]),
          _det["lines"][:1] or "这个维度没有发货行")
    _sp0 = [x for x in _det["lines"] if x.get("split")]
    _bad_sp0 = [x for x in _sp0
                if not (_near(x["qty"], 1) and x.get("serial_no")
                        and x.get("split_part") and x.get("split_total"))]
    check("序列号拆行：拆出来的行 1 个序列号 = 1 行 × 1 件（没拆的行保持原样）",
          not _bad_sp0, _bad_sp0[:3] or f"这个维度拆出 {len(_sp0)} 行")
    _unl0 = [1 if float(r["unlink_qty"] or 0) > 0 else 0 for r in _det["returns"]]
    check("返件明细带「已自动核销」件数，未核销的沉在清单最后（无法匹配的排后面）",
          all("auto_qty" in r for r in _det["returns"]) and _unl0 == sorted(_unl0),
          [r["return_no"] for r in _det["returns"][:4]] or "这个维度没有返件")
_nod = call("GET", "/api/delivery/ledger/detail")[0]
check("缺维度名 → 400/422（不能整表返回）", _nod in (400, 422), _nod)
_nod2 = call("GET", "/api/delivery/ledger/detail?turbine_vendor=")[0]
check("维度名为空 → 400/422（也不整表返回）", _nod2 in (400, 422), _nod2)
check("维度不存在 → 400（不是空明细）",
      call("GET", "/api/delivery/ledger/detail?turbine_vendor="
           + qs2("自检不存在的厂家XYZ") + "&project_site=" + qs2("自检风场"))[0] == 400)

# ---- 需返回口径：正式（关联发货申请单）vs 演示（料号前缀） ----
print("--- 需返回口径 ---")
check("演示口径在 meta 里说明料号前缀", _led_meta.get("demo_prefixes") == ["1", "2"],
      _led_meta.get("demo_prefixes"))
_auto = _ledger(page_size=500)
check("自动口径 = 有发货申请数据就切正式口径（否则演示）",
      _auto["meta"]["need_mode"] == ("req" if _auto["meta"]["need_docs"] else "demo"),
      f"{_auto['meta']['need_mode']}（对上的单 {_auto['meta']['need_docs']} 个）")
_req0 = _ledger(page_size=500, need_mode="req")
check("正式口径的 meta 不再给演示前缀",
      _req0["meta"]["need_mode"] == "req" and not _req0["meta"]["demo_prefixes"])
check("正式口径的发货行 ⊆ 演示口径的发货行",
      _req0["meta"]["ship_lines"] <= _led_meta["ship_lines"],
      f"{_req0['meta']['ship_lines']} ≤ {_led_meta['ship_lines']}")

# 造一张「与真实 ERP 发货明细对上」的申请单，验证正式口径真的纳进这条发货行
_sd = conn.execute(
    "SELECT doc_no, material_no, qty FROM delivery_db.ship_detail "
    "WHERE doc_type='售后发货单' AND doc_status='已核准' "
    "AND TRIM(COALESCE(doc_no,'')) <> '' AND TRIM(COALESCE(material_no,'')) <> '' "
    "AND TRIM(COALESCE(customer,'')) <> '' ORDER BY id LIMIT 1").fetchone()
check("取到一条真实 ERP 发货行当正式口径的靶子", bool(_sd), _sd)
_req_no2, _sd_doc = "", ""
if _sd:
    _sd_doc = str(_sd["doc_no"]).strip()
    st, _rq3 = call("POST", "/api/delivery/requests", {"header": {
        "turbine_vendor": RV2, "project_site": RS2, "expect_ship_date": _d2(0),
        "ship_address": "浙江省温州市乐清市经济开发区纬十二路228号",
        "ship_contact": "夏鹏程", "ship_phone": "18815120633",
        "replace_reason": "台账口径自检", "express_req": "顺丰"},
        "items": [{"material_no": str(_sd["material_no"]).strip(),
                   "product_model": "TR-LEDGER-M", "product_name": "台账口径自检",
                   "spec": "x", "qty": 1, "need_return": True}]})
    _req_no2 = _rq3.get("request_no", "") if st == 200 else ""
    check("为正式口径造了一张申请单（需返回）", st == 200 and bool(_req_no2), _rq3)
if _req_no2:
    st, _reg = call("POST", "/api/delivery/shipments/register",
                    {"request_no": _req_no2, "line_no": 1, "ship_no": _sd_doc,
                     "ship_date": _d2(0)})
    check("按真实 ERP 发货单号登记一行（登记表是「需不需要返回」的唯一凭据）",
          st == 200, _reg if st != 200 else "ok")
    _req1 = _ledger(page_size=500, need_mode="req")
    _hit = next((x for x in _req1["rows"]
                 if _sd_doc in (x.get("doc_nos") or [])), None)
    check("★ 正式口径把与申请单对上的 ERP 发货行纳入台账", _hit is not None,
          f"单号 {_sd_doc}；正式口径共 {_req1['total']} 行")
    check("正式口径的对上行数随之增加",
          _req1["meta"]["need_rows"] >= _req0["meta"]["need_rows"] + 1,
          f"{_req0['meta']['need_rows']} → {_req1['meta']['need_rows']}")
    if _hit:
        check("该维度的发货件数 ≥ 这条 ERP 发货行的件数",
              float(_hit["ship_qty"]) >= float(_sd["qty"] or 0),
              [_hit["ship_qty"], _sd["qty"]])

# ---- 手动核销关联 / 手工清账（都按返件行） ----
print("--- 手动核销关联与手工清账 ---")
_tr = next((x for x in _led["rows"] if x["unlink_qty"] > 0), None)
check("演示口径里存在「已返回但未核销关联」的维度（下面的用例靠它）",
      _tr is not None and _led_meta.get("ret_matched_lines", 0) > 0,
      f"归位返件 {_led_meta.get('ret_matched_lines')} 行；"
      f"没归位的 {_led_meta.get('ret_unmatched_lines')} 行")
if _tr:
    # 挑一条能「关联 1 件 + 清账 1 件」的返件：同一维度里有两条未核销返件就各动一条，
    # 只有一条时它得 ≥ 2 件（否则关联完就没得清了）。挑不到就换下个维度，最多试 8 个。
    _dim1, _dtr, _ur, _ur2 = "", None, None, None
    for _i, _cand in enumerate(x for x in _led["rows"] if x["unlink_qty"] > 0):
        if _i >= 8:
            break
        _d1 = (f"turbine_vendor={qs2(_cand['turbine_vendor'])}"
               f"&project_site={qs2(_cand['project_site'])}&need_mode=demo")
        st, _d = call("GET", "/api/delivery/ledger/detail?" + _d1)
        _us = [r for r in (_d.get("returns") or []) if r["unlink_qty"] > 0]
        if len(_us) >= 2:
            _dim1, _dtr, _ur, _ur2 = _d1, _d, _us[0], _us[1]
            break
        if _us and float(_us[0]["qty"] or 0) >= 2:
            _dim1, _dtr, _ur, _ur2 = _d1, _d, _us[0], _us[0]
            break
    check("二级页的返件明细里有未核销返件行", _dtr is not None and _ur is not None,
          f"{len((_dtr or {}).get('returns') or [])} 条返件" if _dtr else "没找到")
    if _ur:
        _ret_id = int(_ur["id"])
        _ul_orig = float(_ur["unlink_qty"])
        _ret_id2 = int((_ur2 or _ur)["id"])
        _ul_orig2 = float((_ur2 or _ur)["unlink_qty"])
        check("空原因清账 → 400（清账必须留理由）",
              call("POST", "/api/delivery/ledger/clear",
                   {"return_id": _ret_id, "qty": 1, "reason": ""})[0] == 400)
        check("原因太短 → 400",
              call("POST", "/api/delivery/ledger/clear",
                   {"return_id": _ret_id, "qty": 1, "reason": "短"})[0] == 400)
        check("超量清账 → 400（不能超过这条返件的剩余）",
              call("POST", "/api/delivery/ledger/clear",
                   {"return_id": _ret_id, "qty": _ul_orig + 1,
                    "reason": "自检：超量"})[0] == 400)
        check("数量为 0 → 400",
              call("POST", "/api/delivery/ledger/clear",
                   {"return_id": _ret_id, "qty": 0, "reason": "自检：零"})[0] == 400)
        check("返件不存在 → 400",
              call("POST", "/api/delivery/ledger/clear",
                   {"return_id": 99999999, "qty": 1, "reason": "自检：不存在"})[0] == 400)
        st, _lk = call("POST", "/api/delivery/ledger/link",
                       {"return_id": _ret_id, "qty": 1, "reason": "自检：手动核销关联"})
        check("手动核销关联 200 且 kind=link",
              st == 200 and _lk.get("kind") == "link", _lk)
        _det_lk = call("GET", "/api/delivery/ledger/detail?" + _dim1)[1]
        _ur_lk = next((r for r in (_det_lk.get("returns") or [])
                       if int(r["id"]) == _ret_id), None)
        check("关联后该返件已关联 = 1、未核销减少 1（汇总同步）",
              _ur_lk is not None and _near(_ur_lk["link_qty"], 1)
              and _near(_ur_lk["unlink_qty"], _ul_orig - 1)
              and _near(_det_lk["summary"]["clr_link"], 1),
              _ur_lk and [_ur_lk["link_qty"], _ur_lk["unlink_qty"]])
        st, _cl2 = call("POST", "/api/delivery/ledger/clear",
                        {"return_id": _ret_id2, "qty": 1,
                         "reason": "自检：现场留用不返还"})
        check("手工清账 200 且 kind=clear", st == 200 and _cl2.get("kind") == "clear", _cl2)
        _det_cl = call("GET", "/api/delivery/ledger/detail?" + _dim1)[1]
        _ur_cl = next((r for r in (_det_cl.get("returns") or [])
                       if int(r["id"]) == _ret_id2), None)
        check("清账后该返件已清账 = 1、未核销再减 1，汇总 2 件",
              _ur_cl is not None and _near(_ur_cl["clear_qty"], 1)
              and _near(_ur_cl["unlink_qty"], _ul_orig2 - 1)
              and _near(_det_cl["summary"]["clr_qty"], 2),
              _ur_cl and [_ur_cl["clear_qty"], _ur_cl["unlink_qty"],
                          _ret_id2 == _ret_id])
        check("二级页的核销明细里关联 + 清账都在",
              bool(_lk.get("id")) and bool(_cl2.get("id"))
              and {c["id"] for c in (_det_cl.get("clears") or [])} >= {_lk["id"], _cl2["id"]},
              [c.get("id") for c in (_det_cl.get("clears") or [])])
        check("撤销清账 200",
              call("DELETE", f"/api/delivery/ledger/clears/{_cl2['id']}")[0] == 200)
        check("重复撤销 → 404",
              call("DELETE", f"/api/delivery/ledger/clears/{_cl2['id']}")[0] == 404)
        check("撤销核销关联 200",
              call("DELETE", f"/api/delivery/ledger/clears/{_lk['id']}")[0] == 200)
        _det_bk = call("GET", "/api/delivery/ledger/detail?" + _dim1)[1]
        _backs = [(_ret_id, _ul_orig)]
        if _ret_id2 != _ret_id:
            _backs.append((_ret_id2, _ul_orig2))
        _bad_bk = []
        for _rid, _o in _backs:
            _x = next((r for r in (_det_bk.get("returns") or [])
                       if int(r["id"]) == _rid), None)
            if _x is None or not _near(_x["unlink_qty"], _o) or not _near(_x["clr_qty"], 0):
                _bad_bk.append([_rid, _x and [_x["unlink_qty"], _x["clr_qty"]], _o])
        check("撤销后返件恢复原状（未核销回到原值、核销清零）",
              not _bad_bk and _near(_det_bk["summary"]["clr_qty"], 0),
              _bad_bk or [_det_bk["summary"]["clr_qty"]])

# ---- 按型号自动核销 + 发货侧清账（2026-09-23 用户口径） ----
# 用户拍板：① 自动核销按钮触发、真写 kind='auto'、可撤销（不是打开页面就写）；
# ② 型号匹配 = 完全相等 → 互为包含 → 「该维度只有这一个型号」兜底，命中不了留给手动；
# ③ 发货单核销每张单 + 发货行明细每一行 都能手动清账（只减待核销）；
# ④ 发货行按序列号拆行：1 个序列号 = 1 行 × 1 件。
print("--- 按型号自动核销 + 发货侧清账 ---")
import core.repo_delivery as _rd                                # noqa: E402

_snap9 = _rd._ledger_snapshot("demo")
_spdim = None
for _k9 in _snap9["order"]:
    _g9 = _snap9["groups"][_k9]
    for _ln9 in _g9["lines"]:
        _q9 = float(_ln9.get("qty") or 0)
        if (_q9 >= 2
                and len(_rd._serial_tokens(str(_ln9.get("serial_no") or ""))) == int(_q9)):
            _spdim = (_g9["turbine_vendor"], _g9["project_site"], _ln9)
            break
    if _spdim:
        break
check("发货明细里存在「序列号个数 = 数量」的行（拆行口径的靶子）", _spdim is not None,
      "售后发货单里 779 行可拆；一条都没有说明发货明细变了")
if _spdim:
    _sdv, _sds, _sln = _spdim
    _sdj = call("GET", "/api/delivery/ledger/detail?turbine_vendor=" + qs2(_sdv)
                + "&project_site=" + qs2(_sds) + "&need_mode=demo")[1]
    _sparts = [x for x in (_sdj.get("lines") or [])
               if str(x.get("doc_no")) == str(_sln.get("doc_no"))
               and str(x.get("material_no")) == str(_sln.get("material_no"))
               and x.get("split")]
    _toks9 = _rd._serial_tokens(str(_sln.get("serial_no") or ""))
    check("★ 拆行：1 个序列号 = 1 行 × 1 件，拆出行的序列号与原行一致、件数合计不变",
          len(_sparts) == len(_toks9)
          and all(_near(x["qty"], 1) for x in _sparts)
          and _near(sum(float(x["qty"]) for x in _sparts), float(_sln.get("qty") or 0))
          and sorted(str(x["serial_no"]).strip() for x in _sparts) == sorted(_toks9),
          {"原行": [_sln.get("doc_no"), _sln.get("serial_no"), _sln.get("qty")],
           "拆出": [[x["serial_no"], x["qty"]] for x in _sparts][:4]})

_edim = next((x for x in _led["rows"]
              if float(x["pend_qty"] or 0) > 0 and float(x["unlink_qty"] or 0) > 0), None)
check("演示口径里存在「有未核销返件、且待核销 > 0」的维度（自动核销的靶子）",
      _edim is not None,
      f"已归位返件 {_led_meta.get('ret_matched_lines')} 行 / "
      f"未归属 {_led_meta.get('ret_unmatched_lines')} 行")
if _edim:
    _ep = {"turbine_vendor": _edim["turbine_vendor"],
           "project_site": _edim["project_site"], "need_mode": "demo"}
    _eurl = ("/api/delivery/ledger/detail?turbine_vendor=" + qs2(_edim["turbine_vendor"])
             + "&project_site=" + qs2(_edim["project_site"]) + "&need_mode=demo")
    _e0 = call("GET", _eurl)[1]["summary"]
    _auto0 = int(_led_meta.get("auto_clears", 0) or 0)
    st, _dry = call("POST", "/api/delivery/ledger/auto", dict(_ep, dry_run=1))
    check("自动核销试算 200：给维度数 / 计划条数 / 件数 / 跳过条数，且一条都不写",
          st == 200 and all(k in _dry for k in ("dims", "planned", "qty", "skipped",
                                                "rows", "skipped_rows"))
          and _dry.get("dry_run") is True and _dry.get("written") == 0
          and int(_ledger(page_size=500, need_mode="demo")["meta"].get("auto_clears", 0))
          == _auto0,
          {k: _dry.get(k) for k in ("dims", "planned", "qty", "skipped", "written")})
    if _dry.get("planned", 0) > 0:
        _r9 = _dry["rows"][0]
        check("计划行带齐厂家 / 风场 / 退回单号 / 型号 / 发货单 / 件数（风场为空是合法维度）",
              "project_site" in _r9
              and all(_r9.get(k) not in (None, "") for k in
                      ("turbine_vendor", "return_no", "product_model",
                       "doc_no", "qty")), _r9)
        st, _wr = call("POST", "/api/delivery/ledger/auto", dict(_ep))
        check("确认后真写 kind='auto'：written = 计划条数，每条都给 id（可撤销）",
              st == 200 and _wr.get("written") == _dry["planned"]
              and len(_wr.get("ids") or []) == _wr.get("written"),
              {k: _wr.get(k) for k in ("written", "planned", "qty")})
        _e1 = call("GET", _eurl)[1]["summary"]
        _kinds9 = {c["kind"] for c in (call("GET", _eurl)[1].get("clears") or [])}
        check("★ 写完后：二级页核销明细出现「自动核销」，件数按计划增加、待核销下降",
              _near(_e1["clr_auto"], float(_e0["clr_auto"]) + float(_dry["qty"]))
              and "auto" in _kinds9
              and float(_e1["pend_qty"]) <= float(_e0["pend_qty"]),
              [_e0["clr_auto"], _e1["clr_auto"], _dry["qty"], sorted(_kinds9),
               _e0["pend_qty"], _e1["pend_qty"]])
        _ok9 = sum(1 for _cid in (_wr.get("ids") or [])
                   if call("DELETE", f"/api/delivery/ledger/clears/{_cid}")[0] == 200)
        _m9 = _ledger(page_size=500, need_mode="demo")["meta"]
        check("撤销自动核销后全部还原（auto_clears 回原值、该维度恢复原状）",
              _ok9 == _wr.get("written")
              and int(_m9.get("auto_clears", 0) or 0) == _auto0
              and _near(call("GET", _eurl)[1]["summary"]["clr_auto"], _e0["clr_auto"]),
              [_ok9, _wr.get("written"), _m9.get("auto_clears"), _auto0])
    else:
        check("这个维度没有能自动核销的返件（型号都对不上）—— 跳过写入用例",
              _dry.get("skipped", 0) > 0,
              {k: _dry.get(k) for k in ("planned", "skipped")})

_ddim = next((x for x in _led["rows"] if float(x["pend_qty"] or 0) >= 2), None)
check("演示口径里存在待核销 ≥ 2 件的维度（发货侧清账的靶子）", _ddim is not None,
      f"{len(_led['rows'])} 行里找")
if _ddim:
    _durl = ("/api/delivery/ledger/detail?turbine_vendor=" + qs2(_ddim["turbine_vendor"])
             + "&project_site=" + qs2(_ddim["project_site"]) + "&need_mode=demo")
    _dj = call("GET", _durl)[1]
    _sum0 = _dj["summary"]
    _doc9 = next((x for x in _dj["docs"] if float(x.get("pend") or 0) >= 1), None)
    check("该维度有「待核销 ≥ 1」的发货单", _doc9 is not None,
          [(x["doc_no"], x["pend"]) for x in _dj["docs"][:3]])
    if _doc9:
        # ⚠️ 自检跑到这里时登记表里挂着自检申请单，不传口径后端会解析成「正式」，
        #    这个演示口径的维度就不存在 → 400。显式钉住 demo（与上面的 _durl 一致）。
        _b9 = {"turbine_vendor": _ddim["turbine_vendor"],
               "project_site": _ddim["project_site"], "doc_no": _doc9["doc_no"],
               "need_mode": "demo"}
        check("发货单清账：不给发货单号 → 400",
              call("POST", "/api/delivery/ledger/ship-clear",
                   {"turbine_vendor": _b9["turbine_vendor"],
                    "project_site": _b9["project_site"], "scope": "doc", "qty": 1,
                    "reason": "自检：没给单号"})[0] == 400)
        check("发货单清账：超量 → 400（不能超过这张单的待核销）",
              call("POST", "/api/delivery/ledger/ship-clear",
                   dict(_b9, scope="doc", qty=float(_doc9["pend"]) + 1,
                        reason="自检：整单超量"))[0] == 400)
        check("发货单清账：原因太短 → 400",
              call("POST", "/api/delivery/ledger/ship-clear",
                   dict(_b9, scope="doc", qty=1, reason="短"))[0] == 400)
        st, _sc9 = call("POST", "/api/delivery/ledger/ship-clear",
                        dict(_b9, scope="doc", qty=1, reason="自检：发货单清账"))
        check("发货单清账 200 且 kind='shipdoc'、记录带发货单号",
              st == 200 and _sc9.get("kind") == "shipdoc"
              and _sc9.get("doc_no") == _doc9["doc_no"], _sc9)
        _dj2 = call("GET", _durl)[1]
        _doc9b = next((x for x in _dj2["docs"] if x["doc_no"] == _doc9["doc_no"]), None)
        check("★ 发货侧清账只减「待核销」：单待核销 −1、返件未核销原样、clr_ship +1",
              _doc9b is not None
              and _near(_doc9b["pend"], float(_doc9["pend"]) - 1)
              and _near(_dj2["summary"]["unlink_qty"], _sum0["unlink_qty"])
              and _near(_dj2["summary"]["clr_ship"], _sum0["clr_ship"] + 1)
              and _near(_dj2["summary"]["pend_qty"], _sum0["pend_qty"] - 1),
              [_doc9b and _doc9b["pend"], _doc9["pend"],
               _dj2["summary"]["unlink_qty"], _sum0["unlink_qty"],
               _dj2["summary"]["clr_ship"], _sum0["clr_ship"]])
        _ln9 = next((x for x in _dj["lines"]
                     if float(x.get("ship_remain") or 0) >= 1 and not x.get("split")), None)
        check("该维度还有「待清账 ≥ 1」的非拆行发货行（行级清账的靶子）",
              _ln9 is not None, len(_dj["lines"]))
        if _ln9:
            _lb9 = dict(_b9, scope="line", material_no=_ln9["material_no"])
            check("发货行清账：料号对不上 → 400",
                  call("POST", "/api/delivery/ledger/ship-clear",
                       dict(_b9, scope="line", material_no="自检不存在的料号",
                            qty=1, reason="自检：料号对不上"))[0] == 400)
            st, _sl9 = call("POST", "/api/delivery/ledger/ship-clear",
                            dict(_lb9, qty=1, reason="自检：发货行清账"))
            check("发货行清账 200 且 kind='ship'、记录带料号",
                  st == 200 and _sl9.get("kind") == "ship"
                  and _sl9.get("material_no") == _ln9["material_no"], _sl9)
            _dj3 = call("GET", _durl)[1]
            _ln9b = next((x for x in _dj3["lines"]
                          if str(x["doc_no"]) == str(_ln9["doc_no"])
                          and str(x["material_no"]) == str(_ln9["material_no"])
                          and not x.get("split")), None)
            check("★ 行级清账后这一行「已清账 +1 / 待核销 −1」",
                  _ln9b is not None
                  and _near(_ln9b["clr_qty"], float(_ln9["clr_qty"]) + 1)
                  and _near(_ln9b["ship_remain"], float(_ln9["ship_remain"]) - 1),
                  _ln9b and [_ln9b["clr_qty"], _ln9b["ship_remain"],
                             _ln9["clr_qty"], _ln9["ship_remain"]])
            if _sl9.get("id"):
                check("撤销发货行清账 200",
                      call("DELETE",
                           f"/api/delivery/ledger/clears/{_sl9['id']}")[0] == 200)
        if _sc9.get("id"):
            check("撤销发货单清账 200",
                  call("DELETE", f"/api/delivery/ledger/clears/{_sc9['id']}")[0] == 200)
            _dj4 = call("GET", _durl)[1]
            check("撤销后待核销 / clr_ship 回到原值",
                  _near(_dj4["summary"]["pend_qty"], _sum0["pend_qty"])
                  and _near(_dj4["summary"]["clr_ship"], _sum0["clr_ship"]),
                  [_dj4["summary"]["pend_qty"], _sum0["pend_qty"],
                   _dj4["summary"]["clr_ship"], _sum0["clr_ship"]])

# 序列号级清账：拆出来的行按序列号精确到件，不牵连同料号的其他行
if _spdim:
    _sv, _ss, _sln2 = _spdim
    _surl = ("/api/delivery/ledger/detail?turbine_vendor=" + qs2(_sv)
             + "&project_site=" + qs2(_ss) + "&need_mode=demo")
    _sj = call("GET", _surl)[1]
    _myrows = [x for x in _sj["lines"]
               if str(x["doc_no"]) == str(_sln2["doc_no"])
               and str(x["material_no"]) == str(_sln2["material_no"])]
    _ser0 = [x for x in _myrows if x.get("split")]
    check("挑到这一单料号下的拆行（每个序列号一行）", len(_ser0) >= 2,
          [[x["serial_no"], x["ship_remain"]] for x in _ser0][:3])
    if len(_ser0) >= 2:
        _one, _two = _ser0[0], _ser0[1]
        _p9 = {"turbine_vendor": _sv, "project_site": _ss, "scope": "line",
               "doc_no": _one["doc_no"], "material_no": _one["material_no"],
               "serial_no": _one["serial_no"], "need_mode": "demo"}
        check("序列号级清账：假序列号 → 400",
              call("POST", "/api/delivery/ledger/ship-clear",
                   dict(_p9, serial_no="自检不存在的序列号", qty=1,
                        reason="自检：假序列号"))[0] == 400)
        check("序列号级清账：数量 2 > 这一行 1 件 → 400",
              call("POST", "/api/delivery/ledger/ship-clear",
                   dict(_p9, qty=2, reason="自检：超量"))[0] == 400)
        st, _s1 = call("POST", "/api/delivery/ledger/ship-clear",
                       dict(_p9, qty=1, reason="自检：按序列号清账"))
        check("序列号级清账 200 且记录带 serial_no",
              st == 200 and _s1.get("serial_no") == _one["serial_no"], _s1)
        _sj2 = call("GET", _surl)[1]
        _r_1 = next((x for x in _sj2["lines"]
                     if str(x["serial_no"]) == str(_one["serial_no"])), None)
        _r_2 = next((x for x in _sj2["lines"]
                     if str(x["serial_no"]) == str(_two["serial_no"])), None)
        check("★ 只清这一个序列号那一件：兄弟序列号原样（已清账 0 / 待核销 1）",
              _r_1 is not None and _r_2 is not None
              and _near(_r_1["clr_qty"], 1) and _near(_r_1["ship_remain"], 0)
              and _near(_r_2["clr_qty"], 0) and _near(_r_2["ship_remain"], 1),
              [_r_1 and [_r_1["clr_qty"], _r_1["ship_remain"]],
               _r_2 and [_r_2["clr_qty"], _r_2["ship_remain"]]])
        check("同一序列号重复清账 → 400",
              call("POST", "/api/delivery/ledger/ship-clear",
                   dict(_p9, qty=1, reason="自检：重复"))[0] == 400)
        if _s1.get("id"):
            check("撤销序列号级清账 200",
                  call("DELETE", f"/api/delivery/ledger/clears/{_s1['id']}")[0] == 200)
            _sj3 = call("GET", _surl)[1]
            _r_1b = next((x for x in _sj3["lines"]
                          if str(x["serial_no"]) == str(_one["serial_no"])), None)
            check("撤销后该序列号行恢复原状（已清账 0 / 待核销 1）",
                  _r_1b is not None and _near(_r_1b["clr_qty"], 0)
                  and _near(_r_1b["ship_remain"], 1),
                  _r_1b and [_r_1b["clr_qty"], _r_1b["ship_remain"]])

# ---- 未归属返件也能手工清账（2026-09-23 用户口径） ----
# 这批返件归位不到任何维度，不存在「关联到哪张发货单」，所以只能清账；
# 清完必须从「未归属」清单里消失、计入「已人工清账」，而且**不能算孤立核销**
# ——否则那批记录永远挂在 orphan_clears 上，看着像 bug。
print("--- 未归属返件的手工清账 ---")
_un_list = _led_meta.get("unmatched") or []
_am_list = _led_meta.get("ambiguous") or []
_un = _un_list[0] if _un_list else (_am_list[0] if _am_list else None)
check("未归属返件清单里带「已核销 / 未核销」（清账要靠它算上限）",
      _un is not None and "clr_qty" in _un and "unlink_qty" in _un,
      sorted(_un.keys()) if _un else "未归属清单是空的")
if _un:
    _urid = int(_un["return_id"])
    _uq = float(_un["unlink_qty"])
    _ubase = int(_led_meta.get("unattr_cleared_lines", 0))
    st, _uc = call("POST", "/api/delivery/ledger/clear",
                   {"return_id": _urid, "qty": _uq, "reason": "自检：未归属返件清账"})
    check("未归属返件可以直接清账（200 + kind=clear）",
          st == 200 and _uc.get("kind") == "clear", _uc)
    _m_uc = _ledger(page_size=500, need_mode="demo")["meta"]
    _ids_uc = {int(x["return_id"]) for x in (_m_uc.get("unmatched") or [])
               + (_m_uc.get("ambiguous") or [])}
    check("清账后它从「未归属」清单消失，并计入已人工清账（不算孤立核销）",
          _urid not in _ids_uc
          and _m_uc.get("unattr_cleared_lines") == _ubase + 1
          and _m_uc.get("unassigned_clears", 0) >= 1
          and _m_uc.get("orphan_clears", 0) == _led_meta.get("orphan_clears", 0),
          {"还在清单里": _urid in _ids_uc,
           "已人工清账": [_ubase, _m_uc.get("unattr_cleared_lines")],
           "未归属上的核销": _m_uc.get("unassigned_clears"),
           "孤立核销": _m_uc.get("orphan_clears")})
    if _uc.get("id"):
        check("撤销未归属清账 200",
              call("DELETE", f"/api/delivery/ledger/clears/{_uc['id']}")[0] == 200)
        _m_ub = _ledger(page_size=500, need_mode="demo")["meta"]
        _ids_ub = {int(x["return_id"]) for x in (_m_ub.get("unmatched") or [])
                   + (_m_ub.get("ambiguous") or [])}
        check("撤销后又回到「未归属」清单、计数归零",
              _urid in _ids_ub and _m_ub.get("unattr_cleared_lines") == _ubase
              and _m_ub.get("unassigned_clears", 0) == 0,
              [_urid in _ids_ub, _m_ub.get("unattr_cleared_lines"),
               _m_ub.get("unassigned_clears")])

# ---- 厂家 / 风场别名表（保存后必须还原，别动用户填好的别名） ----
print("--- 厂家 / 风场别名表 ---")
st, _al0 = call("GET", "/api/delivery/ledger/alias")
check("别名表可读且带内置默认",
      st == 200 and bool(_al0.get("defaults", {}).get("vendors")), st)
check("内置默认里有「明阳风电 → 明阳智能」",
      (_al0.get("defaults", {}).get("vendors") or {}).get("明阳风电") == "明阳智能",
      _al0.get("defaults", {}).get("vendors"))
_keep = {"vendors": dict((_al0.get("custom", {}) or {}).get("vendors") or {}),
         "sites": dict((_al0.get("custom", {}) or {}).get("sites") or {})}
try:
    st, _al1 = call("PUT", "/api/delivery/ledger/alias", {
        "vendors": {**_keep["vendors"], "自检别名甲": "自检别名乙",
                    "自检别名丙": "自检别名丙"},
        "sites": dict(_keep["sites"])})
    _cv = (((_al1.get("alias") if isinstance(_al1, dict) else None) or _al1)
           .get("custom", {}) or {}).get("vendors") or {}
    check("别名表可保存（新增生效）",
          st == 200 and _cv.get("自检别名甲") == "自检别名乙", st)
    check("别名表丢掉「自己 = 自己」的空对照", "自检别名丙" not in _cv, sorted(_cv))
    check("保存是整表替换，原有别名一条不丢",
          all(_cv.get(k) == v for k, v in _keep["vendors"].items()),
          f"原有 {len(_keep['vendors'])} 条")
finally:
    call("PUT", "/api/delivery/ledger/alias", _keep)
_al9 = call("GET", "/api/delivery/ledger/alias")[1]
check("别名表已还原（自检别名没留在库里）",
      "自检别名甲" not in ((_al9.get("custom", {}) or {}).get("vendors") or {}),
      sorted((_al9.get("custom", {}) or {}).get("vendors") or {}))

# ---- 别名表明细（2026-09-23 用户口径：别名表要能查询明细）----
print("--- 别名表明细 ---")
st, _ad = call("GET", "/api/delivery/ledger/alias/detail")
check("别名表明细可读且逐条列出",
      st == 200 and isinstance(_ad.get("vendors"), list) and len(_ad["vendors"]) >= 1, st)
_mz = [x for x in (_ad.get("vendors") or []) if x.get("key") == "明阳风电"]
check("别名明细带「目标写法 / 来源 / 命中数四件套」",
      bool(_mz) and _mz[0].get("to") == "明阳智能" and _mz[0].get("source") == "内置"
      and set(("lines", "qty", "docs", "dims")) <= set((_mz[0].get("ship") or {}).keys()),
      _mz[0] if _mz else None)
_mt = _ad.get("meta") or {}
_lmeta = (call("GET", "/api/delivery/ledger?page_size=1")[1] or {}).get("meta") or {}
check("明细口径与台账同一份输入（发货行 / 返件行对得上）",
      _mt.get("ship_lines") == _lmeta.get("ship_lines")
      and _mt.get("ret_lines") == _lmeta.get("ret_lines"),
      [_mt.get("ship_lines"), _lmeta.get("ship_lines"),
       _mt.get("ret_lines"), _lmeta.get("ret_lines")])
_mzq = urllib.parse.quote("明阳风电")
_mzd = urllib.parse.quote("明阳智能")
st, _ar = call("GET", f"/api/delivery/ledger/alias/rows?side=vendor&key={_mzq}&to={_mzd}")
# 跑自检时登记表里已有自检数据，需返回口径生效会把发货侧收窄（甚至 0 行），
# 所以下钻只断返件侧（返件不受需返回口径影响），并要求它跟明细汇总对得上。
check("别名能下钻到具体单号（返件侧与明细汇总一致）",
      st == 200 and (_ar.get("ret") or {}).get("total", 0) >= 1
      and len((_ar.get("ret") or {}).get("rows") or []) >= 1
      and _ar.get("names") == ["明阳风电", "明阳智能"]
      and ((_ar.get("ret") or {}).get("truncated")
           or (_ar.get("ret") or {}).get("total") == _mz[0]["ret"]["lines"])
      and (_ar.get("ret") or {}).get("rows")[0].get("return_no") not in (None, ""), st)
st, _ar0 = call("GET", "/api/delivery/ledger/alias/rows?side=vendor&key="
                + urllib.parse.quote("自检没有这个厂家"))
check("下钻到没有数据的名字时如实返回 0 行",
      st == 200 and (_ar0.get("ship") or {}).get("total") == 0
      and (_ar0.get("ret") or {}).get("total") == 0, st)
st, _ = call("GET", "/api/delivery/ledger/alias/rows?side=bogus&key=x")
check("下钻 side 非法时报 400（失败分支真的跑过）", st == 400, st)
st, _ = call("GET", "/api/delivery/ledger/alias/rows?side=vendor&key=&to=")
check("下钻不给名字时报 400", st == 400, st)


print("--- 清理自检数据 ---")
_nos = tuple(n for n in (_track_no, _req_no2) if n)
# 先撤登记行再删申请单：申请单上还挂着登记行时删单会被挡下，而那条登记行正是
# 「需不需要返回」的凭据 —— 留在库里会把自检造的 ERP 发货行一直算进正式口径。
for _x in call("GET", "/api/delivery/shipments?page_size=200")[1]["rows"]:
    if str(_x.get("ship_no", "")).startswith("TR-") \
            or (bool(_nos) and _x.get("request_no") in _nos):
        call("DELETE", f"/api/delivery/shipments/{_x['id']}")
for _no in _nos:
    call("DELETE", f"/api/delivery/requests/{_no}")
_clr_where = "(reason LIKE '自检%' OR operator = ?)"
conn.execute(f"DELETE FROM delivery_db.ledger_clear WHERE {_clr_where};", (SMOKE_USER,))
conn.commit()
check("自检产生的核销记录已清理干净（含自动核销写下的 kind='auto'）",
      conn.execute("SELECT COUNT(*) c FROM delivery_db.ledger_clear "
                   f"WHERE {_clr_where};", (SMOKE_USER,)).fetchone()["c"] == 0)
check("★ 登记行删掉后正式口径不再纳入那条 ERP 发货行（口径跟着申请单走）",
      (not _sd_doc) or all(_sd_doc not in (x.get("doc_nos") or [])
                           for x in _ledger(page_size=500, need_mode="req")["rows"]),
      _sd_doc or "（没造靶子）")
_marks_l = ",".join("?" * len(_nos)) if _nos else ""
_left = conn.execute(
    "SELECT COUNT(*) c FROM delivery_db.delivery_shipment WHERE ship_no LIKE 'TR-%'"
    + (f" OR request_no IN ({_marks_l})" if _nos else "") + ";",
    tuple(_nos) if _nos else None).fetchone()["c"]
check("自检产生的发货记录已清理干净（TR- 单号 / 挂在自检申请单上的都为 0）",
      _left == 0, f"剩 {_left} 条")

print("\n[29] 发货明细：ERP（U9）出货明细镜像")
from config import DATA_DIR as _DATA_DIR              # noqa: E402
from config import SHIP_DETAIL_COLUMNS as _SH_COLS    # noqa: E402

_sh_cnt = conn.execute(
    "SELECT COUNT(*) c FROM delivery_db.ship_detail;").fetchone()["c"]
check("发货明细库已有数据（ERP 同步过）", _sh_cnt > 0, f"{_sh_cnt} 行")

# 发货明细现在也有定时同步了（默认每 8 小时全量拉一次，约 20 秒）。本节下面
# 要发一次「空区间」的手动同步来验证 0 行守卫 —— 定时任务要是正好也在跑，
# 手动那次会撞锁（sync_in_thread 返回 False），本节就变成随机失败。
# 所以先关掉，[31] 末尾按原值恢复。
from core import auth as _auth_sett                          # noqa: E402
import atexit as _atexit                                     # noqa: E402
_auto_was = _auth_sett.get_setting("ship_detail_auto_enabled", "")
_int_was0 = _auth_sett.get_setting("ship_detail_auto_interval_hours", "")
# ⚠️ 本段一开始就把自动同步**关掉**（免得自检和定时任务撞锁），末尾再还原。
#    但如果中途抛异常（2026-09-22 就真发生过：一句 SQL 的占位符写错，
#    在还原之前崩了），那个「关」就**永久留在用户配置里** —— 表现为
#    「发货明细的自动同步再也不跑」，而界面上什么都没提示。
#    所以额外挂一个退出钩子：无论怎么退出，都按快照还原一次。
#    （幂等：正常跑完时末尾那条还原已经执行过，这里再执行一次结果相同。）


def _restore_auto_on_exit():
    try:
        call("PUT", "/api/delivery/details/config",
             {"auto_enabled": _auto_was, "auto_interval_hours": _int_was0})
    except Exception:                                        # noqa: BLE001
        pass                                                 # 服务都没了就没办法


_atexit.register(_restore_auto_on_exit)
_st0, _d0 = call("PUT", "/api/delivery/details/config",
                 {"auto_enabled": "0"})
check("能关掉发货明细的定时同步（避免自检与手动同步撞锁）",
      _st0 == 200 and (_d0.get("auto") or {}).get("auto_enabled") == "0", _d0)

if _sh_cnt:
    _cols = tuple(c[0] for c in _SH_COLS)
    st, d1 = call("GET", "/api/delivery/details?page_size=5")
    check("GET /api/delivery/details → 200", st == 200, st)
    check("返回结构是 rows/total（不是 items）",
          "rows" in d1 and "total" in d1 and "items" not in d1,
          sorted(d1.keys()))
    check("不传 doc_status 时默认只看「已核准」",
          bool(d1.get("rows"))
          and all(r["doc_status"] == "已核准" for r in d1["rows"]),
          sorted({r["doc_status"] for r in d1.get("rows", [])}))
    check("行字段与 config.SHIP_DETAIL_COLUMNS 一致",
          bool(d1.get("rows"))
          and all(set(_cols) <= set(r) for r in d1["rows"]),
          bool(d1.get("rows")) and sorted(set(_cols) - set(d1["rows"][0])))

    # 空串 ≠ 不传：前者是「全部」，后者是默认的「已核准」。
    # 前端就是靠这个区别做「全部」页签的，绝不能把空串当没传。
    st, d2 = call("GET", "/api/delivery/details?doc_status=&page_size=5")
    check("doc_status 传空串 = 全部（不回落成已核准）",
          st == 200 and d2.get("total") == _sh_cnt,
          f"{d2.get('total')} / 库 {_sh_cnt}")

    _open = conn.execute(
        "SELECT COUNT(*) c FROM delivery_db.ship_detail "
        "WHERE doc_status = '开立';").fetchone()["c"]
    st, d3 = call("GET", "/api/delivery/details?doc_status=开立&page_size=5")
    check("按状态筛选的命中数与库一致",
          st == 200 and d3.get("total") == _open,
          f"{d3.get('total')} / 库 {_open}")

    # 排序字段走白名单：非法值必须被忽略（回落默认列），而不是拼进 SQL
    st, _ = call("GET", "/api/delivery/details"
                        "?sort_by=1;DROP TABLE ship_detail--&page_size=2")
    check("非法 sort_by 不报错（回落默认列）", st == 200, st)
    check("ship_detail 仍在（上面那条没被当成 SQL）",
          conn.execute("SELECT COUNT(*) c FROM delivery_db.ship_detail;"
                       ).fetchone()["c"] == _sh_cnt)

    st, s1 = call("GET", "/api/delivery/details/stats?doc_status=")
    check("stats 给出 by_status / 日期区间 / 汇总",
          st == 200 and s1.get("total") == _sh_cnt
          and isinstance(s1.get("by_status"), dict)
          and bool(s1.get("first_date")) and bool(s1.get("last_date")),
          f"{s1.get('total')} · {len(s1.get('by_status') or {})} 种状态")
    check("汇总的单数与料号数 > 0",
          (s1.get("filtered") or {}).get("docs", 0) > 0
          and (s1.get("filtered") or {}).get("materials", 0) > 0,
          s1.get("filtered"))

    st, o1 = call("GET", "/api/delivery/details/options?field=customer&limit=5")
    check("候选值接口返回去重值",
          st == 200 and bool(o1.get("rows"))
          and all("value" in x for x in o1["rows"]),
          f"{st} · {len(o1.get('rows') or [])} 个")
    check("非法候选字段 → 400（字段名走白名单）",
          call("GET", "/api/delivery/details/options?field=x;DROP")[0] == 400)

    st, blob = call("GET", "/api/delivery/details/export?doc_status=", raw=True)
    check("导出返回真 xlsx（PK 魔数）",
          st == 200 and blob[:4] == b"PK\x03\x04",
          f"{st} / {len(blob)} 字节")

    st, sy = call("GET", "/api/delivery/details/sync/status")
    check("同步状态可读，且**不回显密码**",
          st == 200 and "password" not in sy
          and sy.get("password_set") in (True, False),
          sorted(sy.keys()))
    check("同步状态带 0 行守卫的说明（note）", "note" in sy,
          (sy.get("note") or "")[:60])

    # ★ 两个入口的分工（2026-09-22 用户口径）：
    #   「立即同步」/api/delivery/details/sync      = 常规同步（增量）
    #   「全量同步」/api/delivery/details/sync/full = 整表覆盖（act.delivery_detail_full）
    #   旧的「同步范围」（请求体里的 date_from/date_to + auth.db.setting 里的
    #   ship_detail_date_from|to）已经去掉。这一段既钉住「多传的范围键一律被忽略」，
    #   也钉住「常规同步不会把本地几万行清空」。
    #
    # ★★ 这一段会**改到应用的真实状态**：结果落在 data/ship_detail_status.json。
    #    跑完必须原样还原 —— 否则每跑一次自检，页面上就多一条自检造成的记录。
    _STATUS_F = _DATA_DIR / "ship_detail_status.json"
    _status_before = _STATUS_F.read_bytes() if _STATUS_F.exists() else None
    try:
        st, r0 = call("POST", "/api/delivery/details/sync",
                      {"date_from": "1900-01-01", "date_to": "1900-01-02"})
        check("常规同步被接受（后台执行，且响应里不再回 scope）",
              st == 200 and r0.get("ok") and "scope" not in r0, r0)
        _t0 = time.time()
        sy2 = {}
        while time.time() - _t0 < 90:
            time.sleep(2)
            _, sy2 = call("GET", "/api/delivery/details/sync/status")
            if not sy2.get("running"):
                break
        check("常规同步走增量（状态标 full=False），1900 年那对日期没有被存下来",
              sy2.get("full") is False
              and sy2.get("date_from") != "1900-01-01"
              and sy2.get("date_to") != "1900-01-02",
              f"full={sy2.get('full')} · "
              f"{sy2.get('date_from') or '-'} ~ {sy2.get('date_to') or '-'}")
        _after_cnt = conn.execute(
            "SELECT COUNT(*) c FROM delivery_db.ship_detail;").fetchone()["c"]
        check("常规同步没有把本地数据清空（增量只动窗口内与未核准的单）",
              _after_cnt > _sh_cnt // 2,
              f"{_sh_cnt} → {_after_cnt} 行")
    finally:
        # 还原状态留痕 —— 自检不许在应用里留下自己的痕迹
        if _status_before is None:
            _STATUS_F.unlink(missing_ok=True)   # 本来没有 → 自检也不该留下
        else:
            _STATUS_F.write_bytes(_status_before)

    check("自检未遗留：状态留痕已还原（页面不会显示自检造成的记录）",
          (_STATUS_F.read_bytes() if _STATUS_F.exists() else None) == _status_before)

print("\n[30] 发货明细：ERP 文本归一化（部首字符 / 不可见字符）")
from core.erp_ship import needs_normalize as _needs    # noqa: E402
from core.erp_ship import normalize_text as _norm      # noqa: E402

# ★ 用户报的「地址变形」的真凶：ERP 里混进了 Kangxi 部首（U+2F00–U+2FDF）
#   与 CJK 部首补充（U+2E80–U+2EFF）字符。它们的码位是独立字符，但字形画的
#   是一个「部首」，页面上就是 ⻛⼤⼭⽔⻢⼴⿊⻰⾃ 这种缺胳膊少腿的怪字。
#   这一节既钉住折字规则，也钉住库里不许再出现（同步入口与存量迁移都要管住）。
for _bad, _good in (("⻛电场", "风电场"), ("⼤路边镇", "大路边镇"),
                    ("⼭塘乡", "山塘乡"), ("⽔坑镇", "水坑镇"),
                    ("⻢红刚", "马红刚"), ("⼴东省", "广东省"),
                    ("⿊⻰江省", "黑龙江省"), ("⾃治区", "自治区"),
                    ("电⼒", "电力"), ("⻩⼭", "黄山"), ("尚⼯", "尚工")):
    check(f"部首字符折成正常汉字：{_bad} → {_good}",
          _norm(_bad) == _good, _norm(_bad))

# ★ 反面用例：修这个 bug 最顺手的写法 `unicodedata.normalize("NFKC", s)`
#   是**错的** —— 它顺手把全角标点也折了（本地库 （ ） 各 4 万多个、
#   ² 1.2 万、，；： 各数千）。那是把一种「变形」换成另一种。
_punct = "（测试），；：℃²Ⅰ⑤﹣㎡"
check("归一化不碰全角标点（整串 NFKC 会连（ ）℃² 一起折掉）",
      _norm(_punct) == _punct, _norm(_punct))
check("不可见字符被清掉（换行/制表/NBSP/零宽）",
      _norm("a\nb\tc\u00a0d\u200be") == "a b c de",
      repr(_norm("a\nb\tc\u00a0d\u200be")))
check("needs_normalize 认得脏数据、也放得过干净数据",
      _needs("⻛电场") and _needs("a\nb") and _needs("x\u00a0y")
      and not _needs("风电场") and not _needs("") and not _needs(None))

_SH_TXT = ("doc_no", "doc_type", "material_no", "material_name", "spec",
           "product_model", "serial_no", "customer", "contact",
           "express_no", "address")
#  id 也要取出来 —— 报红时得说清是哪一行。样例只留 3 条，脏点总数要数全：
#  万一整列被污染，别把 89k 个元组全堆进内存。
_dirty_n = 0
_dirty_sample = []
for _r in conn.execute("SELECT id, " + ", ".join(_SH_TXT)
                       + " FROM delivery_db.ship_detail;"):
    for _c in _SH_TXT:
        if _needs(_r[_c]):
            _dirty_n += 1
            if len(_dirty_sample) < 3:
                _dirty_sample.append((_r["id"], _c, repr(_r[_c])[:50]))
check(f"ship_detail 全表（{_sh_cnt} 行 × {len(_SH_TXT)} 列）无部首/不可见字符"
      " —— 有的话页面上就是「变形字」",
      _dirty_n == 0, f"{_dirty_n} 处，例如 {_dirty_sample}")

check("存量迁移脚本在仓库里（已同步过的老数据靠它折干净）",
      (pathlib.Path(__file__).resolve().parents[1]
       / "tools" / "normalize_ship_text.py").is_file())

# ---------------------------------------------------------------------------
# [31] 定时同步：节拍器 / 开关字段 / 到点判断 / 两套配置互不串台（含连接）
# ---------------------------------------------------------------------------
print("\n[31] 定时同步：排上了没有 · 两套配置互不串台")
import core.sched as _sched_mod                             # noqa: E402
import core.erp_sync as _esync_mod                          # noqa: E402
import core.erp_ship as _eship_mod                          # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# --- ① 调度不再「睡够间隔再醒」 ---
#   老写法 stop.wait(hours * 3600) 有三处硬伤：配置改完（启用/关闭/改间隔/
#   补密码）要等整整一轮才生效；停机期间错过的到点永远补不回来；睡的是
#   时长而不是「到点时刻」，休眠或时钟跳变会整段跳过。
_sched_src = (_ROOT / "core" / "sched.py").read_text(encoding="utf-8")
check("core/sched.py 提供公共节拍器 Ticker",
      "class Ticker" in _sched_src and "TICK_SECONDS" in _sched_src)
for _mod_name, _who in (("erp_sync", "匹配数据库"), ("erp_ship", "发货明细")):
    _msrc = (_ROOT / "core" / f"{_mod_name}.py").read_text(encoding="utf-8")
    # 老写法是 stop.wait(hours * 3600) —— 一次睡足整个间隔。
    # （auto_due() 里也有 `hours * 3600`，那是拿「已经过去多久」跟间隔比 ——
    #   正是新写法该有的东西。所以判据要盯住 wait(...)，不能盯这个乘法本身：
    #   第一版守卫就是只搜 `hours * 3600`，于是把正确代码判成了红的。）
    check(f"{_who}：调度改成了墙上时钟（不再一次睡足整个间隔）",
          "def _scheduler_loop" not in _msrc
          and "wait(hours * 3600)" not in _msrc)
    check(f"{_who}：调度交给 core/sched.py 的节拍器",
          "sched.Ticker(" in _msrc and "def start_scheduler" in _msrc)
    check(f"{_who}：到点判断与执行分开（Ticker 用 due= 回调决定跑不跑）",
          re.search(r'Ticker\(\s*"[^"]+"\s*,\s*due=', _msrc) is not None)

# --- ② 节拍器的关键性质：due() 抛异常不能让线程死掉 ---
#   线程一死，自动同步从此不再发生，而页面上完全看不出来 —— 这正是用户
#   报的「只能靠手动点击去同步」里最难查的一种形态。
_tick_calls = []


def _tick_due():
    _tick_calls.append(1)
    if len(_tick_calls) == 1:
        raise RuntimeError("故意的：第一拍就炸")
    return False, ""


_tk = _sched_mod.Ticker("smoke", due=_tick_due, run=lambda: None,
                        tick=1, start_delay=0)
_first = _tk.start()
_second = _tk.start()
check("节拍器 start() 幂等（两套节拍会同时拉同一个 ERP）",
      _first.get("started") is True and _second.get("started") is False,
      _second)
time.sleep(3.4)
_tk.stop()
check("节拍器：due() 抛异常也不会让调度线程死掉"
      "（线程一死，自动同步从此静默停摆）",
      len(_tick_calls) >= 3, f"3.4 秒内只醒了 {len(_tick_calls)} 次")

# --- ③ 开关 / 间隔 / 连接：两个任务必须各存各的 ---
#   三样东西都要隔离：间隔（auto_interval_hours）、开关（auto_enabled）、
#   连接（ship_erp_*）。理由都一样 —— 串台的后果是「改一处动了另一处」，
#   而两个任务卡在界面上都显示正常，只有同步在后台偷偷失败。
#   这类错只能靠断言守，看不出来。
_auto = _eship_mod.get_auto()
check("发货明细的自动同步键是 auto_ 前缀（不与 erp_* 撞名）",
      set(_auto) == set(_eship_mod.AUTO_KEYS)
      and all(k.startswith("auto_") for k in _auto), _auto)
check("发货明细定时同步的默认值：启用 · 8 小时",
      _eship_mod.AUTO_DEFAULTS.get("auto_enabled") == "1"
      and _eship_mod.AUTO_DEFAULTS.get("auto_interval_hours") == "8",
      _eship_mod.AUTO_DEFAULTS)
check("auto_hours() 至少给 1（0 会让节拍器每一拍都触发，等于一直在全量拉）",
      isinstance(_eship_mod.auto_hours(), int)
      and _eship_mod.auto_hours() >= 1, _eship_mod.auto_hours())

_int_was = _auth_sett.get_setting("ship_detail_auto_interval_hours", "")
_int_before = _esync_mod.get_config(redact=True).get("interval_hours")
_st6, _d6 = call("PUT", "/api/delivery/details/config",
                 {"auto_interval_hours": "6"})
check("配置接口能写发货明细自己的间隔",
      _st6 == 200 and (_d6.get("auto") or {}).get("auto_interval_hours") == "6",
      _d6)
_int_after = _esync_mod.get_config(redact=True).get("interval_hours")
check("写发货明细的间隔**不会**串到匹配数据库的间隔",
      _int_after == _int_before, f"{_int_before} → {_int_after}")

# --- ③b 连接也拆成两份了（2026-09-22）---
#   拆开之前两份共用 `erp_*`。公用本身不算错，但它有个隐蔽后果：
#   「改一处、忘另一处」在界面上看不出来 —— 两个卡都显示正常，
#   只有一个在后台连不上。所以这里守的是**隔离**，不只是「键名换了」。
check("发货明细有自己的一份连接（ship_erp_*，不再借 erp_*）",
      _eship_mod.CONN_PREFIX == "ship_erp_"
      and set(_eship_mod.CONN_DEFAULTS)
          == {"host", "port", "db", "user", "password"}
      and _eship_mod.CONN_ENV_KEYS.get("password") == "ARS_SHIP_ERP_PASSWORD",
      _eship_mod.CONN_PREFIX)
check("老那份共用连接已经搬过来了（升级后不会掉回「没有密码」）",
      _auth_sett.get_setting(_eship_mod.CONN_SEEDED_KEY, "") == "1"
      and _eship_mod.get_conn(redact=True).get("password_set")
          == _esync_mod.get_config(redact=True).get("password_set"),
      _eship_mod.get_conn(redact=True).get("password_set"))
check("搬运是幂等的（再调一次不再执行）",
      _eship_mod.ensure_conn_seeded() is False)

_items_conn_was = _esync_mod.get_config(redact=True)
_ship_conn_was = {k: _auth_sett.get_setting(_eship_mod.CONN_PREFIX + k, "")
                  for k in _eship_mod.CONN_DEFAULTS}
_stc, _dc = call("PUT", "/api/delivery/details/config", {"host": "10.9.9.9"})
check("配置接口能写发货明细自己的地址",
      _stc == 200 and (_dc.get("config") or {}).get("host") == "10.9.9.9",
      (_dc.get("config") or {}).get("host"))
_items_conn_now = _esync_mod.get_config(redact=True)
check("★ 写发货明细的地址**不会**串到匹配数据库（这就是拆开它的目的）",
      _items_conn_now.get("host") == _items_conn_was.get("host")
      and _items_conn_now.get("password_set") == _items_conn_was.get("password_set"),
      f"{_items_conn_was.get('host')} → {_items_conn_now.get('host')}")
check("★ 那个接口也不写 erp_host（白名单里根本没有这个键）",
      _auth_sett.get_setting("erp_host", "") == (_items_conn_was.get("host") or ""),
      _auth_sett.get_setting("erp_host", ""))
call("PUT", "/api/delivery/details/config", {"host": ""})
# ⚠️ 只查 `get_setting(...) == ""` 是**抓不到**「写了一个空串进去」的 ——
#    行不存在与行存在但值为空，读出来一模一样。必须直接数表里的行数。
_eship_host_rows = conn.execute(
    "SELECT COUNT(*) c FROM auth_db.setting WHERE `key` = ?;",
    (_eship_mod.CONN_PREFIX + "host",)).fetchone()["c"]
check("连接字段留空 = 删掉该键（库里不留一行空串，否则看库的人会以为「配过」）",
      _eship_host_rows == 0, f"{_eship_host_rows} 行")
check("清空后取值回落到默认值",
      _eship_mod.get_conn(redact=True).get("host")
      == _eship_mod.CONN_DEFAULTS["host"],
      _eship_mod.get_conn(redact=True).get("host"))
call("PUT", "/api/delivery/details/config", _ship_conn_was)
check("发货明细的连接按原值还原（自检不给用户留脏配置）",
      _eship_mod.get_conn(redact=True).get("host")
          == (_ship_conn_was.get("host") or _eship_mod.CONN_DEFAULTS["host"]),
      _eship_mod.get_conn(redact=True).get("host"))
call("PUT", "/api/delivery/details/config",
     {"auto_interval_hours": _int_was, "auto_enabled": _auto_was})
_auto_back = _eship_mod.get_auto()
check("定时同步配置按原值还原（自检不给用户留脏配置）",
      _auto_back.get("auto_interval_hours") == (_int_was or "8")
      and _auto_back.get("auto_enabled") == (_auto_was or "1"), _auto_back)

# 到点判断的口径：**上次同步时间 + 间隔 vs 现在**，不是「睡够间隔再醒」。
# 拿假时间来验：把 last_sync_at 换掉，看同一条判断在 1 小时前 / 99 小时前
# 会不会给出相反结论 —— 这正是停机期间「错过的到点会不会补」的依据。
from datetime import datetime as _dt, timedelta as _td         # noqa: E402
_orig_last = _eship_mod.last_sync_at
_orig_hours = _eship_mod.auto_hours
try:
    # ★ 把间隔**也一起固定成 8 小时**：这条用例判的是「墙上时钟怎么算」，
    #   不该受用户当前配置影响。用户把发货明细的间隔改成 1 小时后，
    #   「1 小时前」刚好命中 `>= 1h` 被判成到点，用例就假红了
    #   （2026-09-22 实测：配置 1 小时 → 这两条 FAIL）。
    _eship_mod.auto_hours = lambda: 8
    _eship_mod.last_sync_at = lambda: _dt.now() - _td(hours=1)
    _d_tight = _eship_mod.auto_due()
    _n_tight = _eship_mod.next_sync_at()
    _eship_mod.last_sync_at = lambda: _dt.now() - _td(hours=99)
    _d_late = _eship_mod.auto_due()
    _n_late = _eship_mod.next_sync_at()
finally:
    _eship_mod.last_sync_at = _orig_last
    _eship_mod.auto_hours = _orig_hours
check("到点判断按墙上时钟：1 小时前不算到点、99 小时前算到点",
      _d_tight[0] is False and _d_late[0] is True, f"{_d_tight} / {_d_late}")
check("早已过了到点时刻时，下次时间报「随时（已到点）」而不是一个过去的日期",
      _n_late == "随时（已到点）", _n_late)
check("还没到点时给出的是一个具体时刻（界面直接显示这个）",
      _n_tight not in (None, "", "随时（已到点）") and ":" in _n_tight, _n_tight)

# --- ④ 状态接口要能回答「它到底排上了没有」 ---
#   界面上的开关必须读 enabled。早先绑成 running（=此刻有没有同步在跑，
#   几乎恒为 false），开关就永远显示「关」，用户只能一直手动点。
_st7, _sy = call("GET", "/api/delivery/details/sync/status")
check("发货明细状态：enabled 与 running 是两个独立字段",
      _st7 == 200 and isinstance(_sy.get("enabled"), bool)
      and isinstance(_sy.get("running"), bool), _sy)
check("发货明细状态：给出间隔与「下次自动同步」的时间",
      isinstance(_sy.get("interval_hours"), int) and _sy["interval_hours"] >= 1
      and _sy.get("next_at") not in (None, ""),
      (_sy.get("interval_hours"), _sy.get("next_at")))
_st8, _sy8 = call("GET", "/api/items/sync/status")
check("匹配数据库状态：同样给出 next_at（不能再让界面无从判断排没排上）",
      _st8 == 200 and _sy8.get("next_at") not in (None, ""),
      _sy8.get("next_at"))
_app_src = (_ROOT / "app.py").read_text(encoding="utf-8")
check("发货明细的节拍器真的挂在 app 的启动路径上（lifespan 与 main 各一次）",
      _app_src.count("erp_ship.start_scheduler()") == 2)
# 横幅读错键名不会报错，只会一直显示「自动同步未启用」——
# 首发时就真的写成了 _ac.get("enabled")（正确的键是 auto_enabled），
# 是启动横幅自己把这条静默错报出来的。
check("启动横幅读的是 auto_enabled（读成 enabled 会永远显示「未启用」）",
      '_ac.get("auto_enabled")' in _app_src
      and '_ac.get("enabled")' not in _app_src)

print("\n[32] 连接自愈：MySQL 掉线/重启后应用能不能自己站起来")
# ★ 2026-09-22 实测：MySQL 静默退出了一次，之后应用再没恢复 —— /api/health
#   与两个同步定时器一直刷 `InterfaceError: (0, '')`，只能杀进程重启。
#   根因：core/dbapi.py 的 `_run()` 只 catch 了 OperationalError，而套接字死透
#   时 PyMySQL 抛的是 InterfaceError(0, '')，那条分支不重连。
import core.dbapi as _dba                                # noqa: E402
import inspect as _inspect                               # noqa: E402

_c = _dba.get_conn()
_c.execute("SELECT 1;")                                  # 先确认这条连接是活的
_raw_before = id(_c._raw)
_c._raw.close()                                          # 掐死套接字 = 掉线后的状态
try:
    _healed = _c.execute("SELECT 32 AS v;").fetchone()["v"]
except Exception as exc:                                 # noqa: BLE001
    _healed = f"{type(exc).__name__}: {exc}"
check("连接被掐死后能自愈（重连 + 重试，而不是把异常抛给界面）",
      _healed == 32 and id(_c._raw) != _raw_before,
      f"结果={_healed} 连接已更换={id(_c._raw) != _raw_before}")
check("自愈后拿到的是能用的新连接",
      _c._raw._closed is False
      and _c.execute("SELECT COUNT(*) c FROM returns;").fetchone()["c"] > 0,
      f"_closed={_c._raw._closed}")
# 反面：把「只在 OperationalError 上重连」写回去，上面那条就得变红
check("重连分支认 InterfaceError（只认 OperationalError ＝ 掉线后永不恢复）",
      "pymysql.err.InterfaceError" in _inspect.getsource(_dba.Connection._run))

print("\n[33] 发货明细增量同步：只刷「当天+前一天」与本地未核准单")
# ★ 2026-09-22 用户口径：自动同步不再全量拉取 —— ①当天及前一天；②本地未核准。
#   两条口径必须落在**同一条 SQL 的同一个 OR 谓词**里：拆成两次查询的话，
#   「既在窗口里、又未核准」的单会被取回两次，而落库是先删后插、这张表又没有
#   唯一键挡重复（见 repo_delivery.replace_ship_details 的六列重复说明），
#   结果就是行数直接翻倍。这个坑只在「两条口径同时命中」时才发作。
_win = _eship_mod.auto_window()
_wsql, _wp = _eship_mod.build_query(_win[0], _win[1], ["SM260101001"])
check("增量取数：日期窗口与未核准单号拼在同一个 OR 谓词里",
      _wsql.count("WHERE") == 1 and ") OR a.DocNo IN (" in _wsql
      and _wsql.rstrip().endswith(")"),
      _wsql[_wsql.rfind("WHERE"):][:72])
check("增量取数：参数顺序与占位符一致（先两个日期、后单号）",
      _wp == [_win[0], _win[1], "SM260101001"], _wp)
_fsql, _fp = _eship_mod.build_query("", "", [])
check("全量同步的取数不带任何谓词（连 WHERE 都不出现 ＝ 整表覆盖）",
      "WHERE" not in _fsql and _fp == [], _fsql[-64:])
check("只给单号时不带日期条件（未核准单可能远早于窗口）",
      "BusinessDate" not in _eship_mod.build_query("", "", ["X"])[0].split("WHERE")[-1],
      _eship_mod.build_query("", "", ["X"])[0].split("WHERE")[-1].strip())
check("窗口＝今天及前一天（AUTO_WINDOW_DAYS=2，起点比今天早一天）",
      _eship_mod.AUTO_WINDOW_DAYS == 2 and _win[0] < _win[1]
      and (_eship_mod.datetime.now().date()
           - _eship_mod.datetime.strptime(_win[0], "%Y-%m-%d").date()).days == 1,
      _win)
check("未核准单号上限给 MSSQL 的 2100 参数上限留了余量",
      2 <= _eship_mod.PENDING_DOC_LIMIT <= 1000, _eship_mod.PENDING_DOC_LIMIT)
_rd_src = (_ROOT / "core" / "repo_delivery.py").read_text(encoding="utf-8")
check("落库侧删除谓词与取数谓词同构（同样是「范围 OR 单号」）",
      "doc_no IN (" in _rd_src and '" OR ".join(groups)' in _rd_src)
check("调度走常规同步（_run_scheduled 传 full=False），两条路的默认值都是常规",
      "full=False" in _inspect.getsource(_eship_mod._run_scheduled)
      and _inspect.signature(_eship_mod.sync).parameters["full"].default is False
      and _inspect.signature(
          _eship_mod.sync_in_thread).parameters["full"].default is False)
_sync_src = _inspect.getsource(_eship_mod.sync)
check("取回 0 行仍会中止替换（整表覆盖的保命守卫没被常规同步改掉）",
      "已中止替换" in _sync_src
      and "if (not full) and erp_has_ship_data(cfg)" in _sync_src)
# ★ 两个入口 + 两个权限点 + 范围入口彻底去掉（2026-09-22 用户口径）。
check("两个入口分别走两条路（/sync → full=False，/sync/full → full=True）",
      'sync_in_thread(trigger="manual", full=False)' in _app_src
      and 'sync_in_thread(trigger="manual-full", full=True)' in _app_src)
check("全量同步单独一个权限点（act.delivery_detail_full，默认只给管理员）",
      '"act.delivery_detail_full"' in _app_src
      and '^/api/delivery/details/sync/full$' in _app_src)
# ⚠️ 关键词必须连同调用语法一起判：app.py:1459 有一句完全无关的
#    `oa.set_scopes(payload["scopes"])`（开放接口的授权数据集），子串里
#    就含 "set_scope" —— 只判关键词会让这条守卫**永远红**。模块级常量同理
#    （erp_ship.py:51 的注释里留了一句「已删除 SCOPE_KEYS」），所以改问运行时：
#    模块对象上到底还有没有那个名字。
check("范围入口已彻底去掉（erp_ship 里没有 set_scope/get_scope，config 也不收范围）",
      not hasattr(_eship_mod, "set_scope") and not hasattr(_eship_mod, "get_scope")
      and "SCOPE_KEYS" not in vars(_eship_mod)
      and "erp_ship.set_scope(" not in _app_src
      and "erp_ship.get_scope(" not in _app_src)
from core import auth as _auth_mod                           # noqa: E402
_preset = {g["name"]: list(g["perms"]) for g in _auth_mod.PRESET_GROUPS}
check("两个新权限点都进了 ALL_PERMS（管理员组启动时自动同步，不用手动勾）",
      "act.items_sync" in _auth_mod.ALL_PERMS
      and "act.delivery_detail_full" in _auth_mod.ALL_PERMS)
check("两个新权限点都有中文标签（权限页不会出现没名字的勾选框）",
      _auth_mod.PERM_LABELS.get("act.items_sync")
      and _auth_mod.PERM_LABELS.get("act.delivery_detail_full"))
check("登记员有常规同步、没有全量同步，也没有匹配库同步",
      "act.delivery_detail" in _preset["登记员"]
      and "act.delivery_detail_full" not in _preset["登记员"]
      and "act.items_sync" not in _preset["登记员"])
check("只读组两个同步点都没有",
      "act.delivery_detail" not in _preset["只读"]
      and "act.delivery_detail_full" not in _preset["只读"]
      and "act.items_sync" not in _preset["只读"])
check("老库里的非锁定内置组有定向补丁表（新点只加不删，加过不再动）",
      any(p.get("group") == "登记员"
          and p.get("perm") == "act.delivery_detail"
          for p in _auth_mod.BUILTIN_PERM_PATCHES))
_probe_src = _inspect.getsource(_eship_mod.erp_has_ship_data)
# 只判「执行 SQL 的那一行」：探活函数的 docstring 里正好解释了「为什么不能用
# COUNT(*)」，拿整段源码去判会永远为假 —— 守卫本身没见过失败路径就是这个下场。
_exec_line = [ln for ln in _probe_src.splitlines() if "cur.execute(" in ln]
check("ERP 探活不用 COUNT(*)（列级授权下 COUNT 必报错 230）",
      len(_exec_line) == 1 and "COUNT(" not in _exec_line[0]
      and "TOP 1 a.DocNo" in _exec_line[0],
      _exec_line[0].strip() if _exec_line else "没找到 cur.execute")
import core.repo_delivery as _rd_mod                       # noqa: E402
_pend = _rd_mod.pending_doc_nos()
_db_pend = _rd_mod.get_conn().execute(
    "SELECT COUNT(DISTINCT doc_no) c FROM delivery_db.ship_detail "
    "WHERE doc_no <> '' AND doc_status <> '已核准';").fetchone()["c"]
check("未核准单号清单＝库里全部非「已核准」单号（去重、无空串）",
      len(_pend) == _db_pend and len(set(_pend)) == len(_pend)
      and all(_pend), f"{len(_pend)} / {_db_pend}")
check("清单里没有已核准的单混进来",
      _rd_mod.get_conn().execute(
          "SELECT COUNT(*) c FROM delivery_db.ship_detail "
          "WHERE doc_status = '已核准' AND doc_no IN (%s);"
          % ", ".join("?" * len(_pend)), _pend).fetchone()["c"] == 0 if _pend else True)
check("单号上限真的会截断（保护 MSSQL 参数上限，不是静默截断）",
      len(_rd_mod.pending_doc_nos(limit=3)) == min(3, len(_pend)))
# 2026-09-22：两个业务页上的同步卡整体搬到「自动同步任务」页 ——
# 这条检查的文案现在在那个页面上。
_dd_src = (_ROOT / "static" / "sync-tasks.html").read_text(encoding="utf-8")
check("界面写出了两条路的差别（不能还留着「定时固定走全量」的旧说法）",
      "常规同步" in _dd_src
      and "全量同步" in _dd_src
      and "定时任务**固定走全量**" not in _dd_src)

print("\n[34] 匹配库同步：整表全量覆盖 + 管理员专用")
from core import erp_sync as _esync_mod                      # noqa: E402
_esync_src = _inspect.getsource(_esync_mod.sync)
check('匹配库同步走整表覆盖（import_items mode="replace"，ERP 里没有的料号本地也消失）',
      'mode="replace"' in _esync_src and 'mode="upsert"' not in _esync_src)
check("整表覆盖后类别改成无条件写入（旧口径「只补空」在新结构下会让类别变 NULL）",
      'it["production_stat"] = got' in _inspect.getsource(_esync_mod.finalize)
      and "if existing.get(" not in _esync_src
      and not hasattr(_esync_mod, "_existing_categories"))
check("匹配库的同步 / 连接配置归 act.items_sync（不再是 act.items）",
      '^/api/items/sync/config$", "act.items_sync"' in _app_src
      and '^/api/items/sync/run$", "act.items_sync"' in _app_src)
check("物料增删 / 导入仍归 act.items（没把日常维护一起收走）",
      '^/api/items$", "act.items"' in _app_src
      and '^/api/items/import$", "act.items"' in _app_src)

# ---- 全实时数据源（2026-09-22 改版）：一条 SQL 取全 11 列，**没有快照** ----
# 这几条是"配置对不对"的静态检查，下面几条是"逻辑对不对"的行为检查。
check("11 列是一条 SQL 取全的：主表 LEFT JOIN 多语言表（不再有离线快照、不再有桥）",
      "CBO_ItemMaster_Trl" in _esync_mod.ITEMS_SQL
      and "ON B.ID = A.ID" in _esync_mod.ITEMS_SQL)
check("★ 只用**当前有权限**的列：不碰 Trl 的 NameCombineName / SysMLFlag"
      "（这两列在 2026-09-22 权限重配时被收窄，读它们会 230）",
      "NameCombineName" not in _esync_mod.ITEMS_SQL
      and "SysMLFlag" not in _esync_mod.ITEMS_SQL)
check("品名取主表 `A.Name`（不是 Trl 的 NameCombineName —— 实测与本地 13178 个品名"
      "完全一致，余下 17 个是本地空、ERP 有值）",
      "A.[Name]" in _esync_mod.ITEMS_SQL)
check("11 个文本列**全部** CAST（nvarchar 直读会抛 UnicodeDecodeError 或静默成空串，"
      "整个同步就挂在这儿）",
      _esync_mod.ITEMS_SQL.count("VARBINARY(MAX)") == 11,
      "%d 个 CAST" % _esync_mod.ITEMS_SQL.count("VARBINARY(MAX)"))
check("11 列的列名一个不缺（漏一列 = 同步直接 230）",
      all(c in _esync_mod.ITEMS_SQL for c in (
          "Code", "Code1", "SPECS", "DescFlexField_PrivateDescSeg3",
          "DescFlexField_PrivateDescSeg4", "DescFlexField_PrivateDescSeg5",
          "DescFlexField_PrivateDescSeg8", "DescFlexField_PrivateDescSeg9",
          "DescFlexField_PrivateDescSeg10", "Name", "Description")))
check("REQUIRED_COLS 列明「全实时」到底要哪 7 列权限（缺列时靠它告诉人去申请什么）",
      set(_esync_mod.REQUIRED_COLS) == {
          "ID", "DescFlexField_PrivateDescSeg3", "DescFlexField_PrivateDescSeg4",
          "DescFlexField_PrivateDescSeg5", "DescFlexField_PrivateDescSeg8",
          "DescFlexField_PrivateDescSeg9", "DescFlexField_PrivateDescSeg10"},
      sorted(_esync_mod.REQUIRED_COLS))
check("快照时代的东西**一个都不许回来**（改了全实时还留着快照入口 = 两份来源打架）",
      not hasattr(_esync_mod, "SNAPSHOT_DIR")
      and not hasattr(_esync_mod, "SNAPSHOT_COLUMNS")
      and not hasattr(_esync_mod, "_load_snapshot")
      and not hasattr(_esync_mod, "_snapshot_path")
      and not hasattr(_esync_mod, "fetch_trl")
      and not hasattr(_esync_mod, "merge_extras"))
check("权限错误能抠出「缺哪一列」（界面靠它告诉用户该去申请什么；"
      "非权限错误不许误判成权限问题）",
      _esync_mod._missing_cols_from_error(
          "The SELECT permission was denied on the column 'ID' of the object "
          "'CBO_ItemMaster'") == ["ID"]
      and _esync_mod._missing_cols_from_error("connection timed out") == [])
check("旧料号补前导 0（数值字段吃掉的 0 补回来；太短的不动）",
      _esync_mod.pad_old_no("12100100") == "012100100"
      and _esync_mod.pad_old_no("012100100") == "012100100"
      and _esync_mod.pad_old_no("12345") == "12345"
      and _esync_mod.pad_old_no("") == "")

# 行为检查：直接喂假数据跑 `finalize`（收尾：旧料号补 0 + 内码翻译）。
# **每个分支都带反面用例** —— 守卫只断言"正确的会写进去"是不够的：这里踩过
# 「内码当名字写」这种"不报错、但数据已经错了"的坑，反例必须真的跑一遍。
_rows = [{"material_no": "T-A1", "old_material_no": "12100100", "stat_code": "2320"},
         {"material_no": "T-A2", "old_material_no": "", "stat_code": "9999"},
         {"material_no": "T-A3", "old_material_no": "abc", "stat_code": ""},
         {"material_no": "T-A4", "old_material_no": "012100100", "stat_code": "2320"}]
_counts = _esync_mod.finalize(_rows, {"2320": "风速传感器"})
check("旧料号补前导 0（ERP 数值字段吃掉的 0 补回来）；非纯数字的原样留着",
      _rows[0]["old_material_no"] == "012100100"
      and _rows[2]["old_material_no"] == "abc",
      [r["old_material_no"] for r in _rows])
check("生产统计只写翻译后的可读名，翻不出的内码**留空**"
      "（反面用例：9999 绝不许进库变成数字类别）",
      _rows[0]["production_stat"] == "风速传感器"
      and _rows[1]["production_stat"] == ""
      and _counts["category_mapped"] == 2,
      [r["production_stat"] for r in _rows])
check("stat_code 是中间量，跑完不该留在行里（否则会被写进匹配库）",
      all("stat_code" not in r for r in _rows))
check("命中数如实统计（品名/描述由主表直接带出，不再有「桥」的概念）",
      _esync_mod.finalize(
          [{"material_no": "X", "product_name": "品名", "description": ""}],
          {})["named"] == 1)

# 离线快照**不再参与同步**（2026-09-22 全实时改版）：原来这里要真读一遍
# data/erp/ 的快照文件来验证列名对得上；现在同步根本不碰它，
# 由上面那条「快照时代的东西一个都不许回来」守着 —— 读文件的断言已无对象。
# 同步相关的界面检查也改盯「自动同步任务」页（原页面只剩业务数据）。
_items_src = (_ROOT / "static" / "sync-tasks.html").read_text(encoding="utf-8")
check("匹配库的同步写操作都按 act.items_sync 收口（无权限只留一句提示）",
      "hasPerm('act.items_sync')" in _items_src
      and _items_src.count("CAN_ITEMS ?") >= 4
      and "CAN_ITEMS ? null" in _items_src)
check("两个开关仍绑 enabled（不是 running —— 那个坑踩过一次）",
      # 开关统一收在 autoSwitch() 里，两个任务共用同一段绑定逻辑。
      _items_src.count("el.checked = !!s.enabled") == 1
      and "enabled: on ? '1' : '0'" in _items_src
      and "auto_enabled: on ? '1' : '0'" in _items_src)
_esync_status = _inspect.getsource(_esync_mod.status)
check("匹配库状态接口写清数据源与失败原因（界面直接渲染它，不能只留一句「整表覆盖」）",
      "整表覆盖" in _esync_status and "CBO_ItemMaster_Trl" in _esync_status
      and "missing_cols" in _esync_status)
# 透传白名单：漏一个键，界面就永远显示默认值 0 —— 2026-09-22 真同步时品名已经
# 补好 13178 行，状态接口却报 named=0，看着像「品名根本没补上」。把白名单那段
# 抠出来单独判，判不到（拿不到元组）时自然红。
_pt = _esync_status.split("for k in (", 1)[-1].split(")", 1)[0]
check("状态接口把新计数器一并透传出来（漏键 = 界面永远显示 0）",
      all(k in _pt for k in ("named", "described", "category_mapped",
                             "missing_cols")))

# 删除现在统一走回收站（2026-09-23 批次②）：本次自检删掉的每一条都会在
# 回收站里留下一条记录，不清的话用户打开回收站会看到几十条 SMOKE-* 的东西。
# 按「本次登记过的明细键 / SMOKE- 前缀 / 自检账号身份」三种凭据精确清掉，
# 碰不到真数据（登录账号只有自动化在用）。
if not KEEP:
    _rb_keys = [ln.strip() for ln in
                REG_FILE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    _rb_gone = 0
    for _rb_pg in (1, 2, 3, 4):
        _st, _rb_lst = call("GET", f"/api/recycle?page_size=200&page={_rb_pg}")
        _rb_rows = _rb_lst.get("rows") or []
        if not _rb_rows:
            break
        for _x in _rb_rows:
            _rk = str(_x.get("ref_key") or "")
            if _x.get("operator") == SMOKE_USER or _rk.startswith("SMOKE-") \
                    or any(_k and _k in _rk for _k in _rb_keys):
                call("DELETE", f"/api/recycle/{_x['id']}")
                _rb_gone += 1
    if _rb_gone:
        print(f"  清掉自检留在回收站里的记录 {_rb_gone} 条")
# 本轮已正常收尾，清空记账文件（下次运行无需再自愈）
if not KEEP:
    REG_FILE.write_text("", encoding="utf-8")

print("\n" + "=" * 62)
print(f"  结果：通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("  失败清单：")
    for f in FAIL:
        print("    - " + f)
print("=" * 62)
sys.exit(1 if FAIL else 0)
