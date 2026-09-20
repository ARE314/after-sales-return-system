"""售后返件登记系统 —— 主服务（界面 + 业务接口）

启动：  python app.py
访问：  http://127.0.0.1:8000      （本机）
        http://<本机IP>:8000       （局域网内同事）

角色划分
--------
本文件只提供**面向人的界面与业务接口**。面向机器的数据开放接口在
`open_api.py`，跑在独立端口（默认 8100），鉴权方式也完全不同
（令牌 + 来源 IP 白名单，而不是会话登录）。两者分开是为了让防火墙 /
安全组能只放行对外开放的那一个端口。

登录验证
--------
自 2026-09-20 起默认开启（可运行时关闭）。未登录访问页面会被重定向到
`/login.html`，访问 `/api/*` 会得到 401。权限模型见 `core/auth.py`。
"""
import io
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List
from urllib.parse import quote

import re

from fastapi import (Body, FastAPI, File, HTTPException, Query, Request,
                     Response, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from config import (BASE_DIR, CARRIER_OPTIONS, CARRIER_PREFIX_RULES,
                    CODE_PERIOD_CENTURY, CODE_PERIOD_MONTH_SUFFIX,
                    CODE_PERIOD_PREFIX_LEN, COOKIE_SECURE, CORS_ALLOW_ORIGINS,
                    DEFAULT_REGISTRAR, FIELD_LABELS, FIXED_OPTIONS,
                    HANDLE_DEFAULT_VALUES, HOST,
                    HTTPS_ONLY, ITEM_IMPORT_MAP, ITEM_LABELS,
                    OPEN_API_ENABLED, OPEN_API_HOST, OPEN_API_PORT,
                    PERIOD_UNKNOWN, PHOTO_DIR, PHOTO_EXTS, PHOTO_MAX_MB,
                    PHOTO_MAX_PER_RECORD, PHOTO_THUMB_SIZE, PORT,
                    PUBLIC_BASE_URL, SESSION_COOKIE, STATIC_DIR,
                    TRUSTED_PROXIES)
import open_api as openapi_server

from core import auth
from core import openapi as oa
from core import photos as photos_mod
from core import repository as repo
from core.db import InvalidField, db_status, init_db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动时初始化数据库、字典、照片目录、鉴权与数据开放接口。"""
    init_db()
    repo.refresh_dict_options()
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)

    # 鉴权：预置权限组与首个管理员（幂等），顺手清掉过期会话
    boot = auth.ensure_bootstrap()
    auth.purge_expired_sessions()
    if boot["admin"]:
        print(f"  已创建初始管理员：{boot['admin']['username']}"
              f"（首次登录需改密，密码见 config.AUTH_BOOTSTRAP_PASSWORD）")
    # 数据开放接口：独立端口，后台线程启动（关闭信号由主服务统一处理）
    if OPEN_API_ENABLED:
        st = openapi_server.start_in_thread()
        print(f"  数据接口    http://{st['host']}:{st['port']}/api/open/health")
    yield


app = FastAPI(title="售后返件登记系统", version="1.0.0",
              docs_url="/api/docs", redoc_url=None, lifespan=lifespan)

# 跨域：同源部署（页面与接口同端口）时留空即可。
# 公网部署务必按实际域名收窄 —— 原先是 allow_origins=["*"] 配
# allow_credentials=True，这在浏览器侧本就是无效组合（通配来源不允许带凭据），
# 一旦有人补上凭据就变成「任何站点都能拿着用户 Cookie 调接口」。
if CORS_ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ALLOW_ORIGINS, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )


# ---------------------------------------------------------------------------
# 登录拦截 + 权限校验
#
# 三件事按顺序做：能不能进（未登录）→ 能不能到（页面权限）→ 能不能做（操作权限）。
# 只在 auth_enabled() 为真时生效；关掉开关就是完全放行，不留半拦截状态。
# ---------------------------------------------------------------------------

# 「必须先改密」状态下仍可访问的接口：查自己是谁、改密、退出登录。
# 其余接口一律 403 —— 否则强制改密就只是前端的一句提示，
# 初始密码 admin123 等于没有约束（改地址就能继续用）。
PWD_CHANGE_PATHS = {"/api/auth/me", "/api/auth/password", "/api/auth/logout"}

# 无需登录即可访问：登录页、登录/查询自身身份的接口、健康检查、静态资源。
# /api/health 放开是刻意的 —— 登录页要靠它提示「服务是否连通」。
PUBLIC_PATHS = {"/login.html", "/api/auth/login", "/api/auth/me",
                "/api/health", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/",)

# 操作权限规则：(方法, 路径正则, 权限点)，**顺序敏感**，先匹配先生效。
# 未列入的接口默认「登录即可访问」—— 查询、看板这类只读接口不再逐个设点，
# 页面级权限已经决定了用户能看到哪些入口。
PERM_RULES = [
    ("*", r"^/api/auth/(users|groups|logs|sessions|summary)", "act.user"),
    ("*", r"^/api/auth/settings", "act.settings"),
    ("*", r"^/api/access/", "act.openapi"),
    # 页面级权限：只读接口靠页面权限兜住。原先「数据接口」页只靠前端
    # hasPerm() 隐藏导航，只读组直接敲 URL 仍能打开（页面渲染静默降级成
    # 空页），这里补上服务端判定，与另外八个页面的口径一致。
    ("GET", r"^/api\.html$", "act.openapi"),
    ("GET", r"^/api/export$", "act.export"),
    ("GET", r"^/api/items/export$", "act.export"),
    ("POST", r"^/api/items$", "act.items"),
    ("POST", r"^/api/items/import$", "act.items"),
    ("DELETE", r"^/api/items/[^/]+$", "act.items"),
    ("POST", r"^/api/returns$", "act.create"),
    ("POST", r"^/api/returns/batch$", "act.create"),
    ("POST", r"^/api/returns/check$", "act.create"),
    ("POST", r"^/api/scan/match$", "act.create"),
    ("GET", r"^/api/next-order-no$", "act.create"),
    ("PUT", r"^/api/returns/[^/]+$", "act.edit"),
    ("POST", r"^/api/returns/[^/]+/photos$", "act.edit"),
    ("DELETE", r"^/api/returns/[^/]+/photos/[^/]+$", "act.edit"),
    ("DELETE", r"^/api/returns/[^/]+$", "act.delete"),
    ("POST", r"^/api/returns/batch-delete$", "act.delete"),
]


# 字段白名单校验失败 → 400，而不是让它冒成 500。
# 三个仓储模块的 _safe*() 抛的都是 InvalidField，收在这里转换一次，
# 免得每个路由各写一遍 try/except（而且很容易漏掉某个新路由）。
@app.exception_handler(InvalidField)
async def invalid_field_handler(request: Request, exc: InvalidField):
    return JSONResponse(
        {"detail": {"code": "invalid_field", "message": str(exc)[:120]}},
        status_code=400)


def required_perm(method: str, path: str) -> str:
    """这条请求需要哪个权限点；返回空串表示登录即可。"""
    if path == "/" or path.endswith(".html"):
        name = "index.html" if path == "/" else path.lstrip("/")
        return auth.PAGE_PERMS.get(name, "")
    for m, pattern, perm in PERM_RULES:
        if (m == "*" or m == method) and re.match(pattern, path):
            return perm
    return ""


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    # 预检请求不带 Cookie，放行交给 CORS 中间件处理
    if request.method == "OPTIONS" or not auth.auth_enabled():
        return await call_next(request)
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)

    user = auth.get_session(request.cookies.get(SESSION_COOKIE, ""))
    if not user:
        # 接口返回 401，页面跳登录页，图片等资源直接 401（不能跳，<img> 会显示裂图）
        if path.startswith("/api/"):
            return JSONResponse(
                {"detail": {"code": "unauthorized",
                            "message": "登录状态已失效，请重新登录"}},
                status_code=401)
        if not path.endswith(".html") and path != "/":
            return Response(status_code=401)
        nxt = "/" if path == "/" else path
        return RedirectResponse(f"/login.html?next={quote(nxt, safe='')}",
                                status_code=302)

    # 必须先改密 → 只放行改密相关的三个接口与登录页。
    # 放在权限判定**之前**：这是账号状态问题而不是权限问题，
    # 先报「没有权限」会把用户引到错误的方向。
    if user.get("must_change_pwd"):
        if path.startswith("/api/") and path not in PWD_CHANGE_PATHS:
            return JSONResponse(
                {"detail": {"code": "must_change_pwd",
                            "message": "首次登录必须先修改密码"}},
                status_code=403)
        if not path.startswith("/api/") and path != "/login.html":
            return RedirectResponse("/login.html", status_code=302)

    perm = required_perm(request.method, path)
    if perm and perm not in auth.perms_of(user):
        label = auth.PERM_LABELS.get(perm, perm)
        if path.startswith("/api/"):
            return JSONResponse(
                {"detail": {"code": "forbidden",
                            "message": f"当前账号没有「{label}」权限"}},
                status_code=403)
        return RedirectResponse(f"/?denied={quote(path, safe='')}",
                               status_code=302)

    request.state.user = user
    return await call_next(request)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """静态资源禁止强缓存。

    本地工具升级频繁，若浏览器沿用旧缓存会出现「改了没生效」的假象，
    这里统一要求每次带 ETag 回源校验（未变返回 304，变了返回新内容）。

    例外（可强缓存，避免每次开页面都重新拉）：
    * `/photos/` —— 照片文件名唯一且内容不变（改了就是新文件名）；
    * `/favicon.ico` —— 站点图标几乎不变，且它在**每个**页面加载时都会被请求。

    注意：这里会**覆盖**路由上设的 Cache-Control。所以给某个路径开强缓存时，
    必须同时把它加进下面这个判断，否则路由里的设置会被静默抹掉
    （表现是「我明明设了缓存，network 里还是每次 200」）。
    """
    response = await call_next(request)
    path = request.url.path
    cacheable = (path.startswith("/photos/") or path == "/favicon.ico")
    if not path.startswith("/api/") and not cacheable:
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

# ---------------------------------------------------------------------------
# 字段元数据
#   type 取值：
#     text         普通文本框（scan=True 表示支持扫码枪直接填充）
#     number/date  数字 / 日期
#     textarea     多行文本
#     search-dict  可搜索下拉：输入关键字实时过滤库内已有值，
#                  库内没有则可直接录入新值，提交后自动汇入候选
#     select-fixed 纯下拉：选项锁定在 config.FIXED_OPTIONS，不允许自由输入
#     photos       图片上传：缩略图网格 + 多选上传 + 点击放大 + 单张删除
#                  （仅 photo_evidence 使用，文件落 data/photos/）
# ---------------------------------------------------------------------------
FIELD_DEFS = {
    # --- 整单共享 ---
    "return_no": {"type": "text", "placeholder": "扫描快递单号", "scan": True},
    "carrier": {"type": "select-fixed"},
    "return_date": {"type": "date", "default": "today", "readonly": True},
    "order_no": {"type": "text", "readonly": True, "placeholder": "自动生成"},
    "turbine_vendor": {"type": "search-dict"},
    "project_site": {"type": "search-dict"},
    "info_source": {"type": "select-fixed"},
    "remark": {"type": "textarea"},
    "registrar": {"type": "text", "default": DEFAULT_REGISTRAR},
    # --- 明细行 · 产品信息 ---
    # search-item = 可搜索下拉 + 候选实时检索（与检测登记的模糊匹配同一套交互）：
    #   source=items   候选来自匹配数据库（物料主档 1735 条），输入片段即模糊匹配；
    #   source=history 候选来自本字段历史登记过的值（实时去重，非字典表）。
    # 产品编号 = 铭牌序列号，**不做候选下拉**（2026-09-20 用户确认）：
    # 历史上每个编号基本只出现一次，模糊候选只会干扰，还可能误选到别的序列号。
    # 扫码 / 手打直接落格；回车照旧解析生产年月。
    "product_code": {"type": "text",
                     "placeholder": "扫描铭牌条码", "scan": True},
    "material_no": {"type": "search-item", "source": "items",
                    "placeholder": "料号 / 关键字搜索", "scan": True},
    "product_model": {"type": "search-dict"},
    "product_category": {"type": "search-dict"},
    "product_name": {"type": "search-dict"},
    "spec": {"type": "search-dict"},
    "production_stat": {"type": "search-dict"},
    "production_year": {"type": "search-item", "source": "history"},
    "production_month": {"type": "search-item", "source": "history"},
    "return_qty": {"type": "number", "default": 1},
    # --- 明细行 · 检测信息 ---
    # test_date 锁定为当天（与退回登记的 return_date 同一套处理）：检测时间只是
    # 登记痕迹，不需要人工选择；确需修正时到「明细查询」页编辑（该页不识别
    # readonly 标记，因此不受锁定影响）。
    "test_date": {"type": "date", "default": "today", "readonly": True},
    "feedback_issue": {"type": "search-dict"},
    # 下面三项为「纯下拉锁定项」：选项在 config.FIXED_OPTIONS 中集中维护，
    # 不接受自由输入，保证检测结论的统计口径统一。
    "test_result": {"type": "search-dict"},
    "fault_cause": {"type": "search-dict"},
    "improvement": {"type": "search-dict"},
    "solution": {"type": "select-fixed"},
    # --- 明细行 · 归因与回复 ---
    "issue_category": {"type": "select-fixed"},
    "responsibility": {"type": "select-fixed"},
    # 完结状况：由其余检测字段自动判定（config.COMPLETION_FIELDS 全填才算完结），
    # 任何页面都不接受手工填写 —— auto 标记让前端渲染为只读
    "completion": {"type": "text", "auto": True},
    "report_no": {"type": "text"},
    "analysis_report": {"type": "select-fixed"},
    # 处理登记模块（handle.db）。erp_handled 是**两值锁定项**：
    # 空值在读取时统一归一为「待处理」（见 config.HANDLE_DEFAULT_VALUES）。
    #   no_empty=True → 下拉里不出现「请选择 / 未填写」这类空选项
    #   default      → 编辑空值时的落点（与后端归一值保持一致）
    "erp_handled": {"type": "select-fixed", "no_empty": True,
                    "default": HANDLE_DEFAULT_VALUES["erp_handled"]},
    # 后续处理方案：可搜索下拉，候选来自历史累积，库内没有则直接录入新值
    # （与检测登记里锁定的「处理方案」solution 是两个字段，见 config 注释）
    "handle_solution": {"type": "search-dict"},
    "photo_evidence": {"type": "photos"},
}


def _field(name: str) -> dict:
    """把字段名展开为前端描述符。"""
    d = dict(FIELD_DEFS.get(name, {"type": "text"}))
    d["name"] = name
    d["label"] = FIELD_LABELS.get(name, name)
    return d


def _group(key: str, title: str, names: list) -> dict:
    return {"key": key, "title": title, "fields": [_field(n) for n in names]}


# 整单共享字段：填一次，作用于该快递单下的全部明细行
HEADER_GROUPS = [
    _group("consignment", "收货信息", [
        "return_no", "carrier", "return_date", "order_no",
        "turbine_vendor", "project_site", "info_source",
    ]),
]
HEADER_FIELDS = [f["name"] for g in HEADER_GROUPS for f in g["fields"]]

# 明细行字段：一行 = 一只产品
# 分两类：① 产品固有属性 + 收货初判 → 退回登记时填
#         ② 检测与结论 → 送检后由「检测登记」模块填
LINE_GROUPS = [
    _group("product", "产品信息", [
        "product_code", "material_no", "product_model", "product_category",
        "product_name", "spec", "production_stat", "production_year",
        "production_month", "return_qty",
    ]),
    _group("receive", "收货初判", [
        "feedback_issue", "analysis_report",
    ]),
]

# 检测登记模块的字段分组（送检后才能确定的内容）
INSPECT_GROUPS = [
    _group("inspect", "检测信息", [
        "test_date", "test_result", "fault_cause", "improvement", "solution",
    ]),
    _group("conclusion", "归因与结论", [
        "issue_category", "responsibility", "completion",
        "report_no", "photo_evidence",
    ]),
]
INSPECT_FIELDS = [f["name"] for g in INSPECT_GROUPS for f in g["fields"]]

# 处理登记模块的字段分组（2026-09-20 从检测登记拆出）
# ERP 处理属于「检测完之后」的跟进环节，单独一页一库。
HANDLE_GROUPS = [
    _group("handle", "处理信息", [
        "erp_handled", "handle_solution",
    ]),
]
HANDLE_FIELDS = [f["name"] for g in HANDLE_GROUPS for f in g["fields"]]

# 明细行表格列（顺序即显示顺序）
# 只放「收到产品后初步能填写」的内容，检测信息一律在检测登记模块录入
# 明细行表格默认列（11 列）。「生产统计」不在此列 —— 匹配库里它是类别信息的
# 载体，登记时的值由「产品类别」体现；字段本身保留在库中，明细查询页可见可改。
LINE_COLUMNS = [
    "product_code", "material_no", "product_model", "product_category",
    "product_name", "spec", "production_year",
    "production_month", "return_qty", "feedback_issue", "analysis_report",
]

# 全部字段分组（供明细查询页的编辑弹窗使用；备注与登记人不再出现在登记页）
FIELD_GROUPS = (HEADER_GROUPS + LINE_GROUPS + INSPECT_GROUPS + HANDLE_GROUPS
                + [_group("misc", "其他", ["remark", "registrar"])])

# 筛选面板字段
FILTER_FIELDS = [
    {"name": "product_category", "label": "产品类别", "type": "search-dict"},
    {"name": "turbine_vendor", "label": "风机厂家", "type": "search-dict"},
    {"name": "product_model", "label": "产品型号", "type": "search-dict"},
    {"name": "feedback_issue", "label": "反馈现象", "type": "search-dict"},
    {"name": "fault_cause", "label": "故障原因", "type": "search-dict"},
    {"name": "solution", "label": "处理方案", "type": "select-fixed"},
    {"name": "issue_category", "label": "问题分类", "type": "select-fixed"},
    {"name": "responsibility", "label": "责任归属", "type": "select-fixed"},
    {"name": "completion", "label": "完结状况", "type": "search-dict"},
    {"name": "info_source", "label": "快递归属", "type": "search-dict"},
    {"name": "production_year", "label": "生产年份", "type": "search-dict"},
]
# 刻意不提供的筛选项（字段本身仍在表格 / 编辑弹窗里可见可改）：
#   快递公司 —— 十个候选逐个点选的价值低，且退回单号本身就能定位；
#   分析报告 —— 只有「是 / 否」，用全局关键词检索更快；
#   登记人   —— 当前基本只有一个人登记，筛了也等于没筛。
# 需要时随时加回来（加回一行即可，前端与后端都会自动生效）。

# 明细表格默认展示列
TABLE_COLUMNS = [
    "detail_key", "order_no", "return_no", "carrier", "return_date",
    "turbine_vendor", "project_site", "product_code", "product_model",
    "product_category", "material_no", "product_name", "spec",
    "production_year", "return_qty", "feedback_issue", "fault_cause",
    "test_result", "solution", "issue_category", "responsibility",
    "completion", "analysis_report", "erp_handled", "handle_solution",
    "registrar", "remark",
]


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health(request: Request):
    """连通性探测。

    这个接口在**免登录白名单**里（登录页要靠它提示服务是否连通），
    所以未登录时不能回细节：`db_status()` 里带 `db_path` 与五个库文件的
    绝对路径，等于把服务端的目录结构告诉任何连得上端口的人（实测可见）。
    已登录、或整站关掉鉴权时照旧返回全量。
    """
    now = datetime.now().isoformat(timespec="seconds")
    if auth.auth_enabled() and not auth.get_session(
            request.cookies.get(SESSION_COOKIE, "")):
        return {"ok": True, "time": now}
    return {"ok": True, "time": now, "db": db_status()}


@app.get("/api/meta")
def meta():
    # 筛选面板中若字段属于固定选项，直接内联 options，前端无需再拉字典
    filters = []
    for f in FILTER_FIELDS:
        item = dict(f)
        if "options" not in item and item["name"] in FIXED_OPTIONS:
            item["options"] = FIXED_OPTIONS[item["name"]]
        filters.append(item)

    return {
        "field_labels": FIELD_LABELS,
        "field_groups": FIELD_GROUPS,          # 全部字段（查询页编辑用）
        "header_groups": HEADER_GROUPS,        # 登记页：整单共享区
        "header_fields": HEADER_FIELDS,
        "line_groups": LINE_GROUPS,            # 登记页：明细行字段分组
        "line_columns": LINE_COLUMNS,          # 登记页：明细行默认列
        "inspect_groups": INSPECT_GROUPS,      # 检测登记：字段分组
        "inspect_fields": INSPECT_FIELDS,      # 检测登记：字段名扁平列表
        "handle_groups": HANDLE_GROUPS,        # 处理登记：字段分组
        "handle_fields": HANDLE_FIELDS,        # 处理登记：字段名扁平列表
        "filter_fields": filters,
        "table_columns": TABLE_COLUMNS,
        "default_registrar": DEFAULT_REGISTRAR,
        "fixed_options": FIXED_OPTIONS,        # 纯下拉字段的锁定选项
        # 快递公司识别规则：退回单号前缀 → 快递公司
        "carrier_rules": [{"prefix": p, "carrier": c}
                          for p, c in CARRIER_PREFIX_RULES],
        "carrier_options": CARRIER_OPTIONS,
        # 产品编号 → 生产年月（YYMM）的解析参数，前端本地解析时用同一套规则
        "period_rule": {
            "prefix_len": CODE_PERIOD_PREFIX_LEN,
            "century": CODE_PERIOD_CENTURY,
            "month_suffix": CODE_PERIOD_MONTH_SUFFIX,
            "unknown": PERIOD_UNKNOWN,
        },
        # 照片证据的上传限制（前后端共用一份，避免两边硬编码不一致）
        "photo_limits": {
            "max_per_record": PHOTO_MAX_PER_RECORD,
            "max_mb": PHOTO_MAX_MB,
            "exts": PHOTO_EXTS,
            "thumb_size": PHOTO_THUMB_SIZE[0],
        },
    }


@app.get("/api/dict")
def dict_all():
    return repo.get_dict_options()


@app.get("/api/dict/{field}")
def dict_one(field: str, keyword: str = "", limit: int = 300):
    if keyword:
        return {"field": field, "options": repo.distinct_values(field, keyword, limit)}
    return repo.get_dict_options(field)


@app.get("/api/models")
def models(keyword: str = "", limit: int = 2000):
    return {"rows": repo.list_models(keyword, limit)}


@app.get("/api/next-order-no")
def next_order_no():
    return {"order_no": repo.next_order_no()}


# ---------------------------------------------------------------------------
# 扫码匹配
# ---------------------------------------------------------------------------

@app.post("/api/scan/match")
def scan_match(payload: dict = Body(...)):
    code = (payload.get("code") or "").strip()
    if not code:
        raise HTTPException(400, "条码为空")
    return repo.match_code(code)


# ---------------------------------------------------------------------------
# 登记增删改查
# ---------------------------------------------------------------------------

@app.post("/api/returns")
def create_return(payload: dict = Body(...)):
    data = dict(payload)
    operator = (data.pop("_operator", None)
                or data.get("registrar") or DEFAULT_REGISTRAR)
    try:
        result = repo.create_return(data, operator)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"登记失败：{exc}") from exc
    return {"ok": True, **result}


@app.post("/api/returns/batch")
def create_returns_batch(payload: dict = Body(...)):
    """批量登记：一个快递单号下逐行录入产品，一行对应一只。

    售后单号由后端决定 —— 快递单号已在库中则自动归入该单（补登），
    否则按「年份后两位+月日+顺序号」生成新单。
    同一快递单下已登记过相同产品时返回 409 并给出重复明细。
    """
    header = payload.get("header") or {}
    items = payload.get("items") or []
    operator = (payload.get("_operator") or header.get("registrar")
                or DEFAULT_REGISTRAR)
    try:
        result = repo.create_returns_batch(
            header, items, operator,
            allow_duplicate=bool(payload.get("allow_duplicate")),
        )
    except repo.DuplicateReturnError as exc:
        raise HTTPException(409, detail={
            "message": "存在重复登记，无法提交",
            "duplicates": exc.duplicates,
        }) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"批量登记失败：{exc}") from exc
    return {"ok": True, **result}


@app.post("/api/returns/check")
def check_returns(payload: dict = Body(...)):
    """提交前预检：解析归属售后单号 + 检查是否重复登记。"""
    header = payload.get("header") or {}
    items = payload.get("items") or []
    return_no = str(header.get("return_no") or "").strip()
    order_no, is_new = repo.resolve_order_no(return_no)
    dups = repo.find_duplicates(return_no, items)
    return {
        "return_no": return_no,
        "order_no": order_no,
        "is_new_order": is_new,
        "continues_existing": bool(return_no) and not is_new,
        "duplicates": dups,
        "has_duplicate": bool(dups),
        "line_count": len(items),
    }


@app.put("/api/returns/{detail_key}")
def update_return(detail_key: str, payload: dict = Body(...)):
    data = dict(payload)
    operator = data.pop("_operator", None) or DEFAULT_REGISTRAR
    changed = repo.update_return(detail_key, data, operator)
    if not changed:
        raise HTTPException(404, f"未找到记录或没有变更：{detail_key}")
    return {"ok": True, "detail_key": detail_key, "changed": changed}


@app.get("/api/returns/{detail_key}")
def get_return(detail_key: str):
    row = repo.get_return(detail_key)
    if not row:
        raise HTTPException(404, f"未找到记录：{detail_key}")
    return row


@app.delete("/api/returns/{detail_key}")
def delete_return(detail_key: str, operator: str = DEFAULT_REGISTRAR):
    changed = repo.delete_return(detail_key, operator)
    if not changed:
        raise HTTPException(404, f"未找到记录：{detail_key}")
    return {"ok": True, "deleted": changed}


@app.post("/api/returns/batch-delete")
def batch_delete_returns(payload: dict = Body(...)):
    """批量删除明细（明细查询页的勾选删除）。

    一次事务删完，字典只在最后重建一次 —— 逐条调 DELETE 会把
    全量字典重建放大到 N 次。
    """
    keys = payload.get("detail_keys") or []
    if not isinstance(keys, list) or not keys:
        raise HTTPException(400, "detail_keys 不能为空")
    if len(keys) > 2000:
        raise HTTPException(400, "单次最多删除 2000 条")
    operator = payload.get("_operator") or DEFAULT_REGISTRAR
    result = repo.delete_returns(keys, operator)
    if not result["deleted"]:
        raise HTTPException(404, "没有找到可删除的记录")
    return {"ok": True, **result}


@app.post("/api/returns/{detail_key}/photos")
async def upload_photos(
    detail_key: str,
    files: List[UploadFile] = File(...),
    operator: str = Query(DEFAULT_REGISTRAR),
):
    """上传照片证据（可一次传多张）。

    文件落 `data/photos/<售后单号>/`，字段 `photo_evidence` 存文件名。
    **先整批校验再落盘** —— 任一张不合格就整批拒绝，不会出现「传了一半」。
    附带的旧文本（如金山内嵌图片公式）会在同字段内并存保留，不被覆盖。
    """
    row = repo.get_return(detail_key)
    if not row:
        raise HTTPException(404, f"未找到记录：{detail_key}")

    payloads = [(f.filename or "", await f.read()) for f in (files or [])]
    if not payloads:
        raise HTTPException(400, "没有收到文件")

    names, legacy = photos_mod.parse_value(row.get("photo_evidence"))
    order_no = row.get("order_no") or ""
    try:
        updated = photos_mod.save_uploads(detail_key, order_no, payloads, names)
    except photos_mod.PhotoError as exc:
        raise HTTPException(400, str(exc)) from exc

    value = photos_mod.dump_value(updated, legacy)
    repo.update_return(detail_key, {"photo_evidence": value}, operator)
    return {"ok": True, "detail_key": detail_key,
            "added": len(updated) - len(names), "total": len(updated),
            "value": value, "legacy": legacy,
            "photos": photos_mod.describe(order_no, updated)}


@app.delete("/api/returns/{detail_key}/photos/{name}")
def delete_photo(detail_key: str, name: str,
                 operator: str = Query(DEFAULT_REGISTRAR)):
    """删除单张照片（原图 + 缩略图一并删除）。"""
    row = repo.get_return(detail_key)
    if not row:
        raise HTTPException(404, f"未找到记录：{detail_key}")

    names, legacy = photos_mod.parse_value(row.get("photo_evidence"))
    if name not in names:
        raise HTTPException(404, f"该明细下没有这张照片：{name}")
    try:
        photos_mod.delete_one(detail_key, name)
    except photos_mod.PhotoError as exc:
        raise HTTPException(400, str(exc)) from exc

    # 文件是否真的删掉了要如实回给调用方 —— 原实现无条件报成功，
    # 而字段值已经把该文件名摘掉，于是出现「列表里没有、磁盘上还在」
    # 且没有任何提示。与删记录时 _warn_leftover_photos 的
    # 「尽力而为 + 可见失败」保持同一口径。
    order_no = row.get("order_no") or ""
    folder = photos_mod.dir_of(order_no, create=False)
    leftover = [n for n in (name, photos_mod.thumb_name(name))
                if (folder / n).is_file()]

    updated = [n for n in names if n != name]
    value = photos_mod.dump_value(updated, legacy)
    repo.update_return(detail_key, {"photo_evidence": value}, operator)
    if leftover:
        print(f"[warn] 照片文件未能从磁盘删除（被占用或受环境策略拦截）："
              f"{detail_key} {leftover}", flush=True)
    return {"ok": True, "detail_key": detail_key, "deleted": name,
            "total": len(updated), "value": value,
            "files_removed": not leftover, "leftover": leftover,
            "photos": photos_mod.describe(order_no, updated)}


@app.get("/api/inspect/orders")
def inspect_orders(pending: int = 0, keyword: str = "", page: int = 1,
                   page_size: int = 50, sort_dir: str = "desc"):
    """检测登记：**按售后单号聚合**的待检清单（一行 = 一个售后单）。

    与 `/api/returns` 的区别：后者是明细级（一行 = 一只产品）。
    整单状态取「最落后的那一行」，pending=1 时只返回未完结的单。
    """
    filters = {}
    if pending:
        filters["inspect_pending"] = 1
    if keyword:
        filters["keyword"] = keyword
    return repo.query_inspect_orders(filters, page=page, page_size=page_size,
                                    sort_dir=sort_dir)


@app.get("/api/handle/orders")
def handle_orders(pending: int = 1, keyword: str = "", page: int = 1,
                  page_size: int = 50, sort_dir: str = "desc"):
    """处理登记：**按售后单号聚合**的待处理清单（一行 = 一个售后单）。

    pending=1（默认）时只返回「还有明细没做 ERP 处理」的单 ——
    处理登记页就是一个待办清单，处理完的单自然移出。
    """
    filters = {}
    if pending:
        filters["handle_pending"] = 1
    if keyword:
        filters["keyword"] = keyword
    return repo.query_handle_orders(filters, page=page, page_size=page_size,
                                    sort_dir=sort_dir)


@app.get("/api/returns")
def list_returns(
    keyword: str = "", page: int = 1, page_size: int = 50,
    sort_by: str = "id", sort_dir: str = "desc",
    product_category: str = "", turbine_vendor: str = "", product_model: str = "",
    carrier: str = "", feedback_issue: str = "",
    fault_cause: str = "", solution: str = "", issue_category: str = "",
    responsibility: str = "", completion: str = "", analysis_report: str = "",
    info_source: str = "",
    # 检测侧字段：FastAPI 只认签名里声明过的查询参数，未声明的**静默丢弃** ——
    # 界面选了筛选却返回全量，所以新增筛选项时必须同步改这里与
    # repository._build_where 的精确匹配清单，两处都改才算接上。
    test_result: str = "", improvement: str = "", report_no: str = "",
    erp_handled: str = "", handle_solution: str = "",
    registrar: str = "", production_year: str = "", project_site: str = "",
    product_name: str = "", spec: str = "", material_no: str = "",
    order_no: str = "",
    return_date_from: str = "", return_date_to: str = "",
    registered_at_from: str = "", registered_at_to: str = "",
    test_date_from: str = "", test_date_to: str = "",
    inspect_pending: bool = False, untested_only: bool = False,
    handle_pending: bool = False,
):
    filters = {k: v for k, v in locals().items()
               if k not in ("page", "page_size", "sort_by", "sort_dir") and v}
    return repo.query_returns(filters, page, page_size, sort_by, sort_dir)


# ---------------------------------------------------------------------------
# 看板统计
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def stats(
    scope: str = "return",
    granularity: str = "month",
    product_category: str = "", turbine_vendor: str = "", product_model: str = "",
    carrier: str = "", feedback_issue: str = "",
    fault_cause: str = "", solution: str = "", issue_category: str = "",
    responsibility: str = "", completion: str = "", analysis_report: str = "",
    info_source: str = "", test_result: str = "", improvement: str = "",
    erp_handled: str = "",
    registrar: str = "", production_year: str = "", project_site: str = "",
    return_date_from: str = "", return_date_to: str = "",
    test_date_from: str = "", test_date_to: str = "",
):
    """看板统计。

    scope="return"  返件汇总（收到货就能确定的维度）
    scope="inspect" 检测汇总（送检后才能确定的维度，只统计已检测明细）
    """
    scope = "inspect" if scope == "inspect" else "return"
    filters = {k: v for k, v in locals().items() if k not in ("granularity", "scope") and v}
    return repo.stats_dashboard(filters, granularity, scope)


@app.get("/api/stats/options")
def stats_options():
    conn_fields = [
        ("product_category", "产品类别"), ("turbine_vendor", "风机厂家"),
        ("product_model", "产品型号"), ("feedback_issue", "反馈现象"),
        ("fault_cause", "故障原因"), ("solution", "处理方案"),
        ("issue_category", "问题分类"), ("responsibility", "责任归属"),
        ("completion", "完结状况"), ("carrier", "快递公司"),
        ("info_source", "快递归属"), ("registrar", "登记人"),
        ("product_name", "品名"), ("project_site", "项目风场"),
    ]
    return {"fields": [{"field": f, "label": l, "options": repo.distinct_values(f)}
                       for f, l in conn_fields]}


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

@app.get("/api/export")
def export(
    # 筛选参数必须与 /api/returns 保持同一套 —— 否则「筛选后导出」会导出全量。
    # 新增筛选项时三处同步：本函数、list_returns、repository._build_where。
    keyword: str = "", product_category: str = "", turbine_vendor: str = "",
    product_model: str = "", carrier: str = "",
    feedback_issue: str = "", fault_cause: str = "", solution: str = "",
    issue_category: str = "", responsibility: str = "", completion: str = "",
    analysis_report: str = "",
    test_result: str = "", improvement: str = "", report_no: str = "",
    erp_handled: str = "", handle_solution: str = "",
    info_source: str = "", registrar: str = "", production_year: str = "",
    project_site: str = "", product_name: str = "", spec: str = "",
    material_no: str = "", order_no: str = "",
    return_date_from: str = "", return_date_to: str = "",
    registered_at_from: str = "", registered_at_to: str = "",
    test_date_from: str = "", test_date_to: str = "",
    inspect_pending: bool = False, untested_only: bool = False,
    handle_pending: bool = False,
):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    filters = {k: v for k, v in locals().items() if v}
    rows = repo.query_all(filters)

    columns = [c for c in TABLE_COLUMNS]
    wb = Workbook()
    ws = wb.active
    ws.title = "返件明细"
    ws.append([FIELD_LABELS.get(c, c) for c in columns])

    head_fill = PatternFill("solid", fgColor="DCE6F1")
    for cell in ws[1]:
        cell.font = Font(bold=True, size=11)
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for r in rows:
        ws.append([r.get(c) if r.get(c) is not None else "" for c in columns])

    widths = {"detail_key": 16, "order_no": 12, "return_no": 20, "carrier": 10,
              "return_date": 12, "turbine_vendor": 14, "project_site": 16,
              "product_code": 14, "product_model": 18, "product_category": 12,
              "material_no": 14, "product_name": 18, "spec": 16,
              "production_year": 10, "return_qty": 10, "feedback_issue": 16,
              "fault_cause": 16, "test_result": 22, "solution": 12,
              "issue_category": 12, "responsibility": 12, "completion": 12,
              "analysis_report": 10, "erp_handled": 10,
              "handle_solution": 18,
              "registrar": 10, "remark": 20}
    from openpyxl.utils import get_column_letter
    for i, c in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(c, 14)
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cn_name = f"返件明细_{stamp}.xlsx"
    ascii_name = f"returns_{stamp}.xlsx"
    # HTTP 头只能是 latin-1，中文文件名须按 RFC 5987 百分号编码后放入 filename*
    disposition = (f'attachment; filename="{ascii_name}"; '
                   f"filename*=UTF-8''{quote(cn_name)}")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


# ---------------------------------------------------------------------------
# 匹配数据库（物料主档 Item Master）
# ---------------------------------------------------------------------------

@app.get("/api/items")
def list_items(
    keyword: str = "", page: int = 1, page_size: int = 50,
    sort_by: str = "material_no", sort_dir: str = "asc",
    product_name: str = "", model_no: str = "", production_stat: str = "",
    customer: str = "", category: str = "",
):
    filters = {k: v for k, v in locals().items()
               if k in ("product_name", "model_no", "production_stat",
                        "customer", "category") and v}
    return repo.list_items(keyword, filters, page, page_size, sort_by, sort_dir)


@app.get("/api/items/meta")
def items_meta():
    return {
        "labels": ITEM_LABELS,
        "fields": repo.ITEM_FIELDS,
        "search_fields": repo.ITEM_SEARCH_FIELDS,
        "facets": repo.item_facets(),
        "stats": repo.item_stats(),
    }


@app.get("/api/items/search")
def items_search(kw: str = "", limit: int = 20):
    """明细行「料号」格的候选列表 —— 模糊检索匹配数据库（物料主档）。

    与 `/api/items/fill` 的分工：那个是「拿一个确定的码去回填」，
    这个是「输入片段，返回一批候选供挑选」。
    """
    return {"keyword": kw,
            "items": repo.search_items(kw, min(max(limit, 1), 50))}


@app.get("/api/items/lookup")
def items_lookup(code: str):
    item = repo.lookup_item(code)
    return {"found": bool(item), "item": item, "suggest": repo.item_suggest(code)}


@app.get("/api/items/fill")
def items_fill(code: str):
    """退回登记明细行：输入料号 / 规格号后自动回填产品属性。

    返回 料号 · 产品型号 · 规格 · 品名 · 产品类别（+ 生产统计）与命中来源。
    """
    return repo.suggest_by_material(code)


@app.post("/api/items")
def save_item(payload: dict = Body(...)):
    data = dict(payload)
    operator = data.pop("_operator", None) or DEFAULT_REGISTRAR
    try:
        return {"ok": True, **repo.upsert_item(data, operator)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"保存失败：{exc}") from exc


@app.delete("/api/items/{item_id}")
def remove_item(item_id: int, operator: str = DEFAULT_REGISTRAR):
    changed = repo.delete_item(item_id, operator)
    if not changed:
        raise HTTPException(404, f"记录不存在：{item_id}")
    return {"ok": True, "deleted": changed}


@app.post("/api/items/import")
async def import_items(
    file: UploadFile = File(...),
    mode: str = Query("upsert", pattern="^(upsert|replace)$"),
):
    """上传 Excel 导入物料主档。

    mode=upsert  按料号覆盖已有、新增缺失（默认，安全）
    mode=replace 先清空整表再导入
    """
    from openpyxl import load_workbook

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "上传文件为空")
    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"无法解析 Excel：{exc}") from exc

    ws = wb[wb.sheetnames[0]]
    it = ws.iter_rows(values_only=True)
    try:
        header = [str(c).strip() if c is not None else "" for c in next(it)]
    except StopIteration:
        wb.close()
        raise HTTPException(400, "工作表没有内容") from None

    idx = {ITEM_IMPORT_MAP[h]: i for i, h in enumerate(header)
           if h in ITEM_IMPORT_MAP}
    if "material_no" not in idx:
        wb.close()
        raise HTTPException(400, f"表头缺少「料号」列。实际表头：{header}")

    rows = []
    for r in it:
        rec = {}
        for field, i in idx.items():
            v = r[i] if i < len(r) else None
            if v is not None and str(v).strip() != "":
                rec[field] = str(v).strip()
        if rec.get("material_no"):
            rows.append(rec)
    sheet_name = ws.title
    wb.close()

    if not rows:
        raise HTTPException(400, "未读取到有效数据行（料号列全为空）")

    result = repo.import_items(rows, operator=DEFAULT_REGISTRAR, mode=mode)
    return {
        "ok": True, "file": file.filename, "sheet": sheet_name,
        "recognized": sorted(idx.keys()), "rows": len(rows),
        "stats": repo.item_stats(), **result,
    }


@app.get("/api/items/export")
def export_items(
    keyword: str = "", product_name: str = "", model_no: str = "",
    production_stat: str = "", customer: str = "", category: str = "",
):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    filters = {k: v for k, v in locals().items() if k != "keyword" and v}
    rows = repo.query_items_all(keyword, filters)
    columns = repo.ITEM_FIELDS

    wb = Workbook()
    ws = wb.active
    ws.title = "物料主档"
    ws.append([ITEM_LABELS.get(c, c) for c in columns])
    for cell in ws[1]:
        cell.font = Font(bold=True, size=11)
        cell.fill = PatternFill("solid", fgColor="DCE6F1")
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for r in rows:
        ws.append([r.get(c) if r.get(c) is not None else "" for c in columns])

    widths = {"material_no": 14, "old_material_no": 14, "product_name": 26,
              "model_no": 18, "spec": 18, "description": 30, "customer": 12,
              "production_stat": 18, "param1": 16, "param2": 16, "param3": 12,
              "category": 14}
    for i, c in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(c, 14)
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cn = f"物料主档_{stamp}.xlsx"
    disposition = (f'attachment; filename="item_master_{stamp}.xlsx"; '
                   f"filename*=UTF-8''{quote(cn)}")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


# ---------------------------------------------------------------------------
# 同步
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 登录与身份
#
# 这些路由由中间件之外的路径进入（login / me 在白名单里），所以这里要自己
# 从 Cookie 解析身份，不能依赖 request.state.user。
# ---------------------------------------------------------------------------

def _current_user(request: Request) -> dict:
    """取当前登录用户。

    鉴权关闭时返回一个「本地模式」占位身份 —— 让下游代码不必到处判空，
    也让审计日志里能看出这条记录来自未启用鉴权的本地使用。
    """
    u = getattr(request.state, "user", None)
    if u:
        return u
    if not auth.auth_enabled():
        return {"id": 0, "username": "local", "display_name": "本地模式",
                "group_id": 0, "group_name": "管理员",
                "perms": list(auth.ALL_PERMS), "enabled": True}
    return {}


def _client_ip(request: Request) -> str:
    """记录日志用的来源 IP。

    只有配置了受信任代理时才看 X-Forwarded-For —— 否则这个头由客户端
    随意伪造，日志里的来源会完全失真。
    """
    ip = request.client.host if request.client else ""
    if TRUSTED_PROXIES:
        for p in TRUSTED_PROXIES:
            if ip == p or ip.startswith(p):
                fwd = request.headers.get("x-forwarded-for", "")
                if fwd:
                    return fwd.split(",")[0].strip()
                break
    return ip


def _is_secure(request: Request) -> bool:
    """当前请求是否走的安全连接（直连 HTTPS，或受信任代理转发的 HTTPS）。"""
    if request.url.scheme == "https":
        return True
    if TRUSTED_PROXIES:
        ip = request.client.host if request.client else ""
        if any(ip == p or ip.startswith(p) for p in TRUSTED_PROXIES):
            return request.headers.get("x-forwarded-proto", "") == "https"
    return False


def _set_session_cookie(resp: Response, sid: str) -> None:
    resp.set_cookie(
        SESSION_COOKIE, sid, max_age=auth.SESSION_HOURS * 3600,
        httponly=True,          # 脚本读不到，降低 XSS 拿到会话的风险
        samesite="lax",         # 挡住跨站表单提交携带 Cookie（CSRF 的主要路径）
        secure=COOKIE_SECURE,   # HTTPS 部署时置 True，见 config
        path="/",
    )


@app.post("/api/auth/login")
def auth_login(request: Request, payload: dict = Body(...)):
    """登录：校验账号密码，创建服务端会话，签发 Cookie。"""
    if HTTPS_ONLY and not _is_secure(request):
        raise HTTPException(400, "当前配置要求通过 HTTPS 访问，请改用 https:// 打开")
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    ip = _client_ip(request)
    user = auth.authenticate(username, password) if username else None
    if not user:
        auth.log_action("login.fail", username or "-", "账号或密码错误",
                        username or "-")
        # 不区分「账号不存在」与「密码错误」，避免被用来枚举账号
        raise HTTPException(401, "账号或密码不正确")

    sess = auth.create_session(
        user, ip=ip, user_agent=request.headers.get("user-agent", ""))
    auth.log_action("login", user["username"], f"来源 {ip}", user["username"])
    resp = JSONResponse({
        "ok": True, "user": user,
        "must_change_pwd": user["must_change_pwd"],
        "expires_at": sess["expires_at"],
        "auth_enabled": True,
    })
    _set_session_cookie(resp, sess["sid"])
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request):
    """当前身份。未登录时返回 authenticated=false（不报错）。

    这个接口在免登录白名单里：登录页要靠它判断「是不是已经登录过了」，
    以及读 must_change_pwd 决定是否强制跳改密。
    """
    user = auth.get_session(request.cookies.get(SESSION_COOKIE, ""))
    return {
        "authenticated": bool(user),
        "auth_enabled": auth.auth_enabled(),
        "user": user or None,
        "session_hours": auth.SESSION_HOURS,
        "public_base_url": PUBLIC_BASE_URL,
    }


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    sid = request.cookies.get(SESSION_COOKIE, "")
    user = auth.get_session(sid)
    if user:
        auth.log_action("logout", user["username"], "", user["username"])
    auth.revoke_session(sid)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.post("/api/auth/password")
def auth_password(request: Request, payload: dict = Body(...)):
    """自助改密。改完只保留当前会话，其它设备上的登录立即失效。"""
    sid = request.cookies.get(SESSION_COOKIE, "")
    user = auth.get_session(sid)
    if not user:
        raise HTTPException(401, "登录状态已失效，请重新登录")
    try:
        auth.change_password(user["id"], payload.get("old_password", ""),
                             payload.get("new_password", ""), keep_sid=sid)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.log_action("password.change", user["username"], "", user["username"])
    return {"ok": True, "message": "密码已修改"}


# ---------------------------------------------------------------------------
# 权限设置：用户 / 权限组 / 审计（需 act.user）
# ---------------------------------------------------------------------------

@app.get("/api/auth/summary")
def auth_summary():
    return auth.summary()


@app.get("/api/auth/users")
def auth_users():
    return {"rows": auth.list_users(),
            "groups": [{"id": g["id"], "name": g["name"]}
                       for g in auth.list_groups()]}


@app.post("/api/auth/users")
def auth_user_create(request: Request, payload: dict = Body(...)):
    me = _current_user(request)
    try:
        uid = auth.create_user(
            payload.get("username", ""), payload.get("password", ""),
            int(payload.get("group_id") or 0),
            payload.get("display_name", ""),
            bool(payload.get("must_change_pwd", True)))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.log_action("user.create", payload.get("username", ""),
                    f"权限组 #{payload.get('group_id')}", me["username"])
    return {"ok": True, "id": uid}


@app.put("/api/auth/users/{user_id}")
def auth_user_update(user_id: int, request: Request, payload: dict = Body(...)):
    me = _current_user(request)
    try:
        n = auth.update_user(
            user_id, display_name=payload.get("display_name"),
            group_id=(int(payload["group_id"])
                      if payload.get("group_id") not in (None, "") else None),
            enabled=(bool(payload["enabled"])
                     if payload.get("enabled") is not None else None),
            password=payload.get("password") or None)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.log_action("user.update", str(user_id),
                    ",".join(sorted(payload)), me["username"])
    return {"ok": True, "changed": n}


@app.delete("/api/auth/users/{user_id}")
def auth_user_delete(user_id: int, request: Request):
    me = _current_user(request)
    if me.get("id") == user_id:
        raise HTTPException(400, "不能删除当前登录的账号")
    try:
        auth.delete_user(user_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.log_action("user.delete", str(user_id), "", me["username"])
    return {"ok": True}


@app.get("/api/auth/groups")
def auth_groups():
    return {"rows": auth.list_groups(),
            "permission_groups": auth.PERMISSION_GROUPS,
            "all_perms": auth.ALL_PERMS,
            "locked": auth.LOCKED_GROUPS}


@app.post("/api/auth/groups")
def auth_group_create(request: Request, payload: dict = Body(...)):
    me = _current_user(request)
    try:
        gid = auth.create_group(payload.get("name", ""),
                                payload.get("description", ""),
                                payload.get("perms") or [])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.log_action("group.create", payload.get("name", ""),
                    f"{len(payload.get('perms') or [])} 项权限", me["username"])
    return {"ok": True, "id": gid}


@app.put("/api/auth/groups/{group_id}")
def auth_group_update(group_id: int, request: Request,
                      payload: dict = Body(...)):
    me = _current_user(request)
    try:
        auth.update_group(group_id, name=payload.get("name"),
                          description=payload.get("description"),
                          perms=payload.get("perms"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.log_action("group.update", str(group_id),
                    f"{len(payload.get('perms') or [])} 项权限", me["username"])
    return {"ok": True}


@app.delete("/api/auth/groups/{group_id}")
def auth_group_delete(group_id: int, request: Request):
    me = _current_user(request)
    try:
        auth.delete_group(group_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.log_action("group.delete", str(group_id), "", me["username"])
    return {"ok": True}


@app.get("/api/auth/logs")
def auth_logs(limit: int = Query(100, ge=1, le=500)):
    return {"rows": auth.recent_logs(limit)}


@app.get("/api/auth/sessions")
def auth_sessions(limit: int = Query(50, ge=1, le=200)):
    return {"rows": auth.active_sessions(limit)}


# ---------------------------------------------------------------------------
# 系统开关（需 act.settings）
# ---------------------------------------------------------------------------

@app.get("/api/auth/settings")
def auth_settings_get():
    return {
        "auth_enabled": auth.auth_enabled(),
        "session_hours": auth.SESSION_HOURS,
        "cookie_secure": COOKIE_SECURE,
        "https_only": HTTPS_ONLY,
        "host": HOST,
        "port": PORT,
        "public_base_url": PUBLIC_BASE_URL,
        "cors_origins": CORS_ALLOW_ORIGINS,
        "trusted_proxies": TRUSTED_PROXIES,
        "databases": db_status()["databases"],
        # 备份是否超期 —— 定时备份失败是静默的，把它放到界面上自己浮出来
        "backup": auth.backup_status(),
    }


@app.put("/api/auth/settings")
def auth_settings_put(request: Request, payload: dict = Body(...)):
    """运行时开关。改完立即生效（中间件每次请求都读一次）。"""
    me = _current_user(request)
    if "auth_enabled" in payload:
        on = bool(payload["auth_enabled"])
        auth.set_auth_enabled(on)
        auth.log_action("settings.auth_enabled", "1" if on else "0",
                        "登录验证开关", me["username"])
    return {"ok": True, "auth_enabled": auth.auth_enabled()}


# ---------------------------------------------------------------------------
# 数据开放接口的配置（需 act.openapi）
#
# 注意路由前缀用的是 /api/access/* 而不是 /api/open/* —— 后者属于独立端口上
# 那个对外服务（open_api.py）。同名前缀跑在两个端口上极易配错防火墙规则。
# ---------------------------------------------------------------------------

@app.get("/api/access/status")
def access_status():
    st = oa.status()
    live = openapi_server.state()
    st["running"] = bool(live)
    st["thread_alive"] = bool(live.get("thread") and live["thread"].is_alive())
    return st


@app.put("/api/access/config")
def access_config(request: Request, payload: dict = Body(...)):
    me = _current_user(request)
    if payload.get("ips") is not None:
        oa.set_allowed_ips(payload["ips"])
        auth.log_action("access.ips", ",".join(payload["ips"]) or "(空)",
                        "IP 白名单", me["username"])
    if payload.get("scopes") is not None:
        picked = oa.set_scopes(payload["scopes"])
        auth.log_action("access.scopes", ",".join(picked), "数据范围",
                        me["username"])
    # 金山侧目标信息（文件 ID / 云盘 ID / 落点工作表）：可填写项。
    # 它不驱动本系统行为，但会写进审计日志 —— 否则「谁把落点工作表改到了
    # 别的表」这种问题事后无从查起。
    if payload.get("kdocs") is not None:
        t = oa.set_kdocs_target(payload["kdocs"])
        auth.log_action(
            "access.kdocs",
            f"file={t.get('file_id') or '-'} · drive={t.get('drive_id') or '-'}"
            f" · sheet={t.get('sheet') or '-'}",
            "金山侧目标信息", me["username"])
    if payload.get("kdocs_reset"):
        t = oa.reset_kdocs_target()
        auth.log_action(
            "access.kdocs",
            f"reset → file={t.get('file_id') or '-'} · sheet={t.get('sheet') or '-'}",
            "金山侧目标信息恢复默认", me["username"])
    return {"ok": True, "status": oa.status()}


@app.post("/api/access/token/rotate")
def access_token_rotate(request: Request):
    me = _current_user(request)
    token = oa.rotate_token()
    auth.log_action("access.token", "rotate", "重置访问令牌", me["username"])
    return {"ok": True, "token": token,
            "message": "令牌已重置，请同步更新金山文档定时任务里的配置"}


@app.get("/api/access/logs")
def access_logs(limit: int = Query(100, ge=1, le=500)):
    return {"rows": oa.recent_pulls(limit)}


# ---------------------------------------------------------------------------
# 静态页面
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# 页面名白名单。
#
# **不要**改成「过滤 .. 」的黑名单写法：路径参数会被 Starlette 解码，而
# Windows 下 `\` 也是路径分隔符 —— `/..%5C..%5Cfoo.html` 能拼出 static 之外的
# 路径（实测能读到项目外的任意 .html，HTTP 200）。白名单只放行
# 字母 / 数字 / 下划线 / 连字符，不依赖「列举危险字符」这种迟早会漏的思路。
# （`%2F` 形式挡得住是因为 uvicorn 先解码成 `/`，正则会失配 —— 但那是巧合，
#   不是设计。）
_PAGE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


# 站点图标走独立路由，不能靠下面的 `/{page}.html`。
# 那条路由**只匹配以 .html 结尾的路径**，所以 /favicon.ico 根本到不了它 ——
# 而 PUBLIC_PATHS 里早就写了 "/favicon.ico"（原意是「放行它」），
# 结果就是「白名单里有个永远不存在的路由」：浏览器每次开页面都拿 404，
# 服务端日志里也看不出毛病。实测确认过这一点（404 / {"detail":"Not Found"}）。
#
# 文件放在**项目根**（与 data/photos/ 同一模式：根下实体 + 独立路由），
# 不放 static/ —— URL 是 /favicon.ico，根下同名文件与 URL 一一对应，
# 测试脚本与后来的人都不用再想「这个 URL 到底映射到哪」。
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    path = BASE_DIR / "favicon.ico"
    if not path.is_file():
        raise HTTPException(404, "favicon 不存在")
    # 图标基本不变，给它一点强缓存，省掉每个页面加载时的一次请求
    # （no_cache_static 中间件里同步放行了这个路径，否则这里会被覆盖）
    return FileResponse(str(path), media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/{page}.html")
def pages(page: str):
    if not _PAGE_NAME.fullmatch(page):
        raise HTTPException(404, "页面不存在")
    path = STATIC_DIR / f"{page}.html"
    if not path.is_file():
        raise HTTPException(404, "页面不存在")
    return FileResponse(str(path))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# 照片证据（原图与缩略图）。文件名唯一且不复用，可安全强缓存。
PHOTO_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/photos", StaticFiles(directory=str(PHOTO_DIR)), name="photos")


def _lan_ips() -> list:
    """列出本机可用于局域网访问的 IPv4 地址。"""
    import socket
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:  # noqa: BLE001
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:  # noqa: BLE001
            pass
    return ips


def main() -> None:
    """命令行启动。

    优先级：命令行参数 > 环境变量（config 里读）> 内置默认值。
    服务器部署建议全部走环境变量，这样配置文件保持原样、便于随代码更新。
    """
    import uvicorn
    # 只认「非选项」的位置参数为 host / port —— 否则启动脚本透传过来的
    # --lan / --public 之类开关会被当成监听地址（表现为 Address not available）
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    host = positional[0] if len(positional) > 0 else HOST
    port = int(positional[1]) if len(positional) > 1 else PORT
    if "--local" in sys.argv:
        host = "127.0.0.1"
    init_db()
    repo.refresh_dict_options()
    boot = auth.ensure_bootstrap()
    auth.purge_expired_sessions()

    open_state = {}
    if OPEN_API_ENABLED:
        open_state = openapi_server.start_in_thread()

    print("=" * 60)
    print("  售后返件登记系统 · 服务已启动")
    print("-" * 60)
    print(f"  本机访问    http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        for ip in _lan_ips():
            print(f"  局域网访问  http://{ip}:{port}")
        if not PUBLIC_BASE_URL:
            print("  公网部署    建议设置 ARS_PUBLIC_BASE_URL，"
                  "并在反代上启用 HTTPS")
    else:
        print(f"  监听地址    {host}（仅本机可访问）")
    if open_state:
        oh, op = open_state["host"], open_state["port"]
        print(f"  数据接口    http://{oh}:{op}/api/open/health")
        if oh in ("127.0.0.1", "localhost"):
            print("              （仅本机；要让金山文档定时任务访问需改 "
                  "ARS_OPEN_API_HOST=0.0.0.0）")
    print("-" * 60)
    print(f"  登录验证    {'已开启' if auth.auth_enabled() else '已关闭'}"
          f"（可在「权限设置」页切换）")
    if boot["admin"]:
        print(f"  初始管理员  {boot['admin']['username']} / "
              f"密码见 config.AUTH_BOOTSTRAP_PASSWORD（首次登录需改密）")
    print(f"  数据目录    {db_status()['db_path'].rsplit(chr(92), 1)[0]}")
    print("  停止服务    按 Ctrl+C")
    print("=" * 60)
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        if open_state.get("server"):
            open_state["server"].should_exit = True


if __name__ == "__main__":
    main()
