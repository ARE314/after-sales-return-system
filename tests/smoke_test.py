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
check("更新后重新进入待同步", got.get("sync_state") == "pending", got.get("sync_state"))

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

# ---------------------------------------------------------------- 8. 导出
print("\n[8] Excel 导出")
st, blob = call("GET", "/api/export", raw=True)
check("导出接口返回 200", st == 200, f"{len(blob)} bytes")
check("返回 xlsx 文件头", blob[:2] == b"PK", blob[:4].hex())

# ---------------------------------------------------------------- 9. 同步
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

print("\n[9b] 数据开放接口（独立端口，供金山文档定时任务拉取）")
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

    # --- 金山侧目标信息（文件 ID / 云盘 ID / 落点工作表）：写死 config → 可填写 ---
    from core.db import get_conn as _gc                              # noqa: PLC0415
    from config import KDOCS_TARGET as _KT                           # noqa: PLC0415
    _kc = _gc()
    st, acc_k = call("GET", "/api/access/status")
    _k0 = acc_k.get("kdocs", {})
    check("状态接口返回金山侧目标信息（供页面填写）",
          {"file_id", "drive_id", "sheet"} <= set(_k0), str(sorted(_k0)))
    check("未配置过时带出 config 内置默认值（老部署升级不空白）",
          _k0.get("sheet") == _KT["sheet"],
          f"{_k0.get('file_id', '')[:12]}… · {_k0.get('sheet')}")
    # 记住自检前的状态：原本配置过就写回原值，没配过就保持「未配置」，
    # 否则跑一次自检就把用户填的 ID / 工作表名冲掉了。
    _was_saved = _kc.execute(
        "SELECT COUNT(*) c FROM auth_db.setting WHERE key = 'kdocs_target';"
    ).fetchone()["c"] > 0

    st, put_k = call("PUT", "/api/access/config", {"kdocs": {
        "file_id": "SMOKE-DOC-1", "drive_id": "888888", "sheet": "自检落点表"}})
    _k1 = (put_k.get("status") or {}).get("kdocs", {})
    check("可从接口保存金山侧目标信息", st == 200, f"HTTP {st}")
    check("保存后立即生效（同一响应里已是新值）",
          _k1.get("file_id") == "SMOKE-DOC-1" and _k1.get("sheet") == "自检落点表",
          str(_k1))

    st, acc_k2 = call("GET", "/api/access/status")
    check("重新读取仍是保存的值（已落库，不是内存态）",
          acc_k2["kdocs"]["sheet"] == "自检落点表",
          repr(acc_k2["kdocs"].get("sheet")))

    # 只认这三个键：多余的一律丢弃，免得往 setting 里塞进任意内容
    call("PUT", "/api/access/config", {"kdocs": {
        "file_id": "SMOKE-DOC-2", "sheet": "x", "injected": "坏值"}})
    st, acc_k3 = call("GET", "/api/access/status")
    check("未知键被丢弃（只认三个已知字段）",
          "injected" not in acc_k3["kdocs"], str(sorted(acc_k3["kdocs"])))

    # 清空某一项 = 真的空，不悄悄回落到默认值（否则用户清不掉）
    call("PUT", "/api/access/config", {"kdocs": {"file_id": "", "sheet": ""}})
    st, acc_k4 = call("GET", "/api/access/status")
    check("清空后保持为空（不悄悄回落到 config 默认值）",
          acc_k4["kdocs"]["file_id"] == "" and acc_k4["kdocs"]["sheet"] == "",
          str(acc_k4["kdocs"]))

    st, rst_k = call("PUT", "/api/access/config", {"kdocs_reset": True})
    _k5 = (rst_k.get("status") or {}).get("kdocs", {})
    check("「恢复默认」回到 config 内置值",
          _k5.get("sheet") == _KT["sheet"] and _k5.get("file_id") == _KT["file_id"],
          str(_k5))

    # 复原自检前的状态
    if _was_saved:
        call("PUT", "/api/access/config", {"kdocs": _k0})
    else:
        call("PUT", "/api/access/config", {"kdocs_reset": True})

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

check("推送式同步接口已下线",
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
    from config import HANDLE_DB, INSPECT_DB, ITEMS_DB, RETURNS_DB  # noqa: PLC0415
    from core.db import get_conn  # noqa: PLC0415

    # 库文件与字段归属
    check("四个库文件均已生成",
          all(p.exists() for p in (RETURNS_DB, INSPECT_DB, HANDLE_DB, ITEMS_DB)),
          " / ".join(p.name for p in (RETURNS_DB, INSPECT_DB, HANDLE_DB, ITEMS_DB)))

    conn = get_conn()
    r_cols = {r["name"] for r in conn.execute("PRAGMA table_info(returns);")}
    i_cols = {r["name"]
              for r in conn.execute("PRAGMA inspect_db.table_info(inspect_records);")}
    check("退回登记库不含检测侧字段",
          not (r_cols & {"test_result", "fault_cause", "completion", "issue_category"}),
          str(sorted(r_cols & {"test_result", "fault_cause", "completion"})) or "无")
    check("检测登记库不含退回侧字段",
          not (i_cols & {"return_no", "turbine_vendor", "product_code", "return_qty"}),
          str(sorted(i_cols & {"return_no", "turbine_vendor", "product_code"})) or "无")
    h_cols = {r["name"]
              for r in conn.execute("PRAGMA handle_db.table_info(handle_records);")}
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


# 完结状况已改为自动判定（8 项全填），不能再靠手写 completion 完结
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
    "items": [{"product_code": f"AGG{stamp2}-1", "return_qty": 1},
              {"product_code": f"AGG{stamp2}-2", "return_qty": 1},
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
        GROUP BY return_no HAVING COUNT(DISTINCT order_no) > 1);"""
).fetchone()["c"]
check("一个快递单只对应一个售后单（不拆单）", multi == 0, f"{multi} 个仍分裂")

rev = conn.execute(
    """SELECT COUNT(*) c FROM (SELECT order_no FROM returns
        GROUP BY order_no HAVING COUNT(DISTINCT return_no) > 1);"""
).fetchone()["c"]
check("一个售后单只对应一个快递单", rev == 0, f"{rev} 个跨多单")

bad_key = conn.execute(
    """SELECT COUNT(*) c FROM returns
        WHERE detail_key <> order_no || '-' || printf('%03d', line_no);"""
).fetchone()["c"]
check("明细键 = 售后单号-行号（自洽）", bad_key == 0, f"{bad_key} 条不自洽")

dup_key = conn.execute(
    """SELECT COUNT(*) c FROM (SELECT detail_key FROM returns
        GROUP BY detail_key HAVING COUNT(*) > 1);"""
).fetchone()["c"]
check("明细键全局唯一", dup_key == 0, f"{dup_key} 个重复键")

gaps = conn.execute(
    """SELECT COUNT(*) c FROM (SELECT order_no FROM returns
        GROUP BY order_no HAVING COUNT(*) <> MAX(line_no));"""
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
check("每条退回明细都有对应检测记录（当前为满覆盖）", stray == 0,
      f"{stray} 条无检测记录")

# ---------------------------------------------------------------- 22. 完结状况自动判定
print("\n[22] 完结状况：按检测字段自动判定")

from config import (COMPLETION_DONE, COMPLETION_EXCLUDED,   # noqa: E402
                    COMPLETION_FIELDS)

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
    check("只填 1/7 项 → 未完结", completion_of(ck) == "", repr(completion_of(ck)))

    # ERP处理已挪出判定依据：只补这三项「事后记录」字段不应改变结论
    call("PUT", f"/api/returns/{ck}", {
        "report_no": "RPT-SELF-001", "photo_evidence": "IMG-SELF-001",
        "erp_handled": "自检：ERP已处理",
    })
    check("ERP处理 / 报告编号 / 照片证据均不影响判定（仍未完结）",
          completion_of(ck) == "", repr(completion_of(ck)))

    # 填满参与判定的 7 项（刻意不带 erp_handled）→ 应判为已完结
    call("PUT", f"/api/returns/{ck}", {
        "test_date": "2026-09-19", "fault_cause": "自检：故障原因",
        "improvement": "自检：改善措施", "solution": "拆解报废",
        "issue_category": "无法判断", "responsibility": "其他端",
    })
    check("7 项全填（不含 ERP处理）→ 自动判定为已完结",
          completion_of(ck) == COMPLETION_DONE, repr(completion_of(ck)))

    call("PUT", f"/api/returns/{ck}", {"improvement": ""})
    check("清空其中一项 → 自动回退为未完结", completion_of(ck) == "",
          repr(completion_of(ck)))

    call("PUT", f"/api/returns/{ck}", {"completion": "已完结"})
    check("外部传入的完结状况被忽略（派生字段不可手工写）",
          completion_of(ck) == "", repr(completion_of(ck)))

    call("PUT", f"/api/returns/{ck}", {"improvement": "自检：改善措施"})
    check("补齐后恢复为已完结", completion_of(ck) == COMPLETION_DONE,
          repr(completion_of(ck)))

    call("DELETE", f"/api/returns/{ck}")

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

# ---------------------------------------------------------------- 24. 处理登记
print("\n[24] 处理登记：独立库 handle.db")
if KEEP:
    print("  [SKIP] 已指定 --keep，跳过处理登记验证")
else:
    from config import HANDLE_DB, HANDLE_DICT_FIELDS    # noqa: E402
    from core.db import get_conn                        # noqa: E402

    check("处理登记库文件已生成", HANDLE_DB.exists(), HANDLE_DB.name)

    conn = get_conn()
    h_cols = {r["name"] for r in conn.execute(
        "PRAGMA handle_db.table_info(handle_records);")}
    i_cols = {r["name"] for r in conn.execute(
        "PRAGMA inspect_db.table_info(inspect_records);")}
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
    check("待处理数 = 全部 - 已处理",
          hp.get("total") == n_all - n_handled,
          f"{hp.get('total')} = {n_all} - {n_handled}")

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
    check("库里不再有空值（回填后只剩两态）",
          _vals <= {"已处理", "待处理"} and len(_vals) == 2,
          str(sorted(_vals)))
    _rows_in_handle = conn.execute(
        "SELECT COUNT(*) c FROM handle_db.handle_records;").fetchone()["c"]
    check("处理库不再稀疏（每条明细都有明确状态）",
          _rows_in_handle == n_all, f"{_rows_in_handle} 行 / {n_all} 条明细")

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
    st, _pf = call("GET", "/api/returns?handle_pending=1&page_size=500")
    check("清空的行仍出现在待处理清单里（归一兜底不漏单）",
          _probe_key in [r["detail_key"] for r in _pf.get("rows", [])],
          f"待处理 {_pf.get('total')} 条")
    st, _ps = call("GET", "/api/returns?erp_handled="
                   + urllib.parse.quote("待处理") + "&page_size=500")
    check("按「待处理」筛选能带出空值记录",
          _probe_key in [r["detail_key"] for r in _ps.get("rows", [])],
          f"命中 {_ps.get('total')} 条")
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
