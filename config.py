"""售后返件登记系统 —— 全局配置

所有可调参数集中在此文件，改完重启服务即可生效。
其中「服务监听」「公开访问」「鉴权」几节支持环境变量覆盖 ——
服务器部署时不必改文件，用环境变量注入即可（见 deploy/README 说明）。
"""
import json
import os
from pathlib import Path


def _env_str(key: str, default: str = "") -> str:
    """读环境变量（供服务器部署时注入配置）。"""
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = (os.getenv(key, "") or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_list(key: str) -> list:
    """逗号分隔的环境变量 → 列表（空则返回空列表）。"""
    raw = os.getenv(key, "") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]

# ---------- 路径 ----------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
EXPORT_DIR = DATA_DIR / "exports"
# 照片证据的原图与缩略图（按售后单分子目录）
PHOTO_DIR = DATA_DIR / "photos"
# 回收站里照片的暂存目录：`photos_trash/<回收站记录 id>/<售后单号>/<文件名>`。
# 删除明细时照片是**搬到这里**而不是 unlink —— 否则还原了记录、图却没了，
# 而照片删除不可逆。超过保留期由回收站清理时真删（见 core/recycle.py）。
PHOTO_TRASH_DIR = DATA_DIR / "photos_trash"

# ---------- 数据库：MySQL（一个模块一个 schema） ----------
# 2026-09-22 从「6 个 SQLite 文件 + ATTACH」改成「一个 MySQL 8 实例 + 6 个 schema」。
# 业务 SQL 里的跨库前缀（inspect_db. / handle_db. / items_db. / auth_db. /
# delivery_db.）**原样保留** —— MySQL 的 schema.table 原生支持跨库 JOIN；
# 而连接的默认 schema 就是 returns_db，所以不带前缀的表名照旧落在退回登记库。
#
# 连接信息取值优先级：环境变量 ARS_MYSQL_* > data/mysql.json > 下面的默认值。
# data/mysql.json 由 tools/dev_mysql.py 在装库时生成，data/ 已被 .gitignore 覆盖，
# 因此密码不会进版本库。
MYSQL_FILE = DATA_DIR / "mysql.json"
MYSQL_CONF: dict = {}
if MYSQL_FILE.exists():
    try:
        MYSQL_CONF = json.loads(MYSQL_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        MYSQL_CONF = {}


def _mysql_str(key: str, env: str, default: str) -> str:
    """环境变量优先，其次 data/mysql.json，最后内置默认值。"""
    return _env_str(env, str(MYSQL_CONF.get(key, default) or default)) or default


def _mysql_int(key: str, env: str, default: int) -> int:
    if os.getenv(env, "").strip():
        return _env_int(env, default)
    try:
        return int(MYSQL_CONF.get(key, default))
    except (TypeError, ValueError):
        return default


MYSQL_HOST = _mysql_str("host", "ARS_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = _mysql_int("port", "ARS_MYSQL_PORT", 3306)
MYSQL_USER = _mysql_str("user", "ARS_MYSQL_USER", "ars")
MYSQL_PASSWORD = _mysql_str("password", "ARS_MYSQL_PASSWORD", "")
MYSQL_CHARSET = _mysql_str("charset", "ARS_MYSQL_CHARSET", "utf8mb4")

# 六个 schema：一个模块一个。名字与旧的 SQLite 文件名对齐，
# 所以几百处跨库前缀（inspect_db.xxx）一个字都不用改。
RETURNS_DB = "returns_db"       # 退回登记库：returns / model_dict / 字典 / 日志
INSPECT_DB = "inspect_db"       # 检测登记库：inspect_records / 字典 / 日志
HANDLE_DB = "handle_db"         # 处理登记库：handle_records / 字典 / 日志
ITEMS_DB = "items_db"           # 匹配数据库：item_master
AUTH_DB = "auth_db"             # 接入库：users / permission_group / session / setting
DELIVERY_DB = "delivery_db"     # 发货库：delivery_request / ship_detail / ...
MAIN_SCHEMA = RETURNS_DB        # 连接默认 schema：不带前缀的表名落在这里
SCHEMAS = (RETURNS_DB, INSPECT_DB, HANDLE_DB, ITEMS_DB, AUTH_DB, DELIVERY_DB)

# 逐项复刻 MySQL 8 默认的 sql_mode，**只去掉 ONLY_FULL_GROUP_BY**。
# 原因：SQLite 允许「GROUP BY 后直接选非聚合列」（取该组任意一行），本项目的
# 聚合查询大量依赖这个行为。留着 ONLY_FULL_GROUP_BY 会在页面请求里抛 1055，
# 而且报错点离出错的 SQL 很远，很难定位。其余严格项一个不少地保留。
MYSQL_SQL_MODE = _env_str(
    "ARS_MYSQL_SQL_MODE",
    "STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION",
)

# ---------- 服务监听 ----------
# 127.0.0.1 = 仅本机可访问（默认，开发调试用）
# 0.0.0.0   = 局域网 / 公网可访问（服务器部署用）
# 可用环境变量覆盖：ARS_HOST / ARS_PORT，例如
#     ARS_HOST=0.0.0.0 ARS_PORT=8000 python app.py
HOST = _env_str("ARS_HOST", "127.0.0.1")
PORT = _env_int("ARS_PORT", 8000)

# ---------- 公开访问（服务器部署 / 公网）----------
# 走反向代理对外时填对外地址，用于页面里生成绝对链接、Cookie 域与文档说明。
# 例：ARS_PUBLIC_BASE_URL=https://returns.example.com
PUBLIC_BASE_URL = _env_str("ARS_PUBLIC_BASE_URL", "")
# 允许跨域访问本服务的来源（同源部署留空即可）。公网部署务必按实际域名收窄，
# 不要用 "*" —— 配合 Cookie 凭据时通配来源本身也是无效配置。
CORS_ALLOW_ORIGINS = _env_list("ARS_CORS_ORIGINS")
# 反向代理链路上受信任的代理地址；开启后才会信任 X-Forwarded-For / Proto。
TRUSTED_PROXIES = _env_list("ARS_TRUSTED_PROXIES")
# HTTPS 部署时置 True：会话 Cookie 加 Secure 标记，且只走安全连接。
# ARS_COOKIE_SECURE=1 / ARS_HTTPS_ONLY=1
COOKIE_SECURE = _env_bool("ARS_COOKIE_SECURE", False)
HTTPS_ONLY = _env_bool("ARS_HTTPS_ONLY", False)
# 会话有效期（小时）
SESSION_HOURS = _env_int("ARS_SESSION_HOURS", 12)

# ---------- 登录与权限（鉴权模块，2026-09-20 新增）----------
# 总开关的**初始默认值**；运行时可界面上切换，存在 auth.db 的 setting 表里。
AUTH_ENABLED_DEFAULT = _env_bool("ARS_AUTH_ENABLED", True)
# 首次启动自动创建的管理员账号（仅当用户表为空时创建）
AUTH_BOOTSTRAP_USER = _env_str("ARS_BOOTSTRAP_USER", "admin")
AUTH_BOOTSTRAP_PASSWORD = _env_str("ARS_BOOTSTRAP_PASSWORD", "admin123")
# 会话 Cookie 名称
SESSION_COOKIE = "ars_sid"

# ---------- 数据开放接口（供金山文档定时任务拉取）----------
# 方向已反转：不再由本系统「推送」到金山，改由金山侧定时调用这里的接口拉取。
# 独立端口与主界面分开，便于在防火墙/反向代理上只放行这一个端口。
OPEN_API_ENABLED = _env_bool("ARS_OPEN_API", True)
OPEN_API_HOST = _env_str("ARS_OPEN_API_HOST", "127.0.0.1")   # 对外需改 0.0.0.0
OPEN_API_PORT = _env_int("ARS_OPEN_API_PORT", 8100)
# 令牌：留空则由程序首次启动时随机生成并写入 auth.db（界面可查看/重置）
OPEN_API_TOKEN = _env_str("ARS_OPEN_API_TOKEN", "")
# 允许调用该接口的来源 IP / 网段（留空 = 不限制）。
# 例：ARS_OPEN_API_IPS=203.0.113.10,203.0.113.0/24
OPEN_API_IPS = _env_list("ARS_OPEN_API_IPS")
# 默认允许拉取的数据集（界面可再调整）：
#   detail  明细汇总（退回 + 检测 + 处理拼成一张平表）← 通常只要这一个
#   returns / inspect / handle / items  各自的单表
OPEN_API_SCOPES = _env_list("ARS_OPEN_API_SCOPES") or ["detail"]

# ---------- 数据备份 ----------
# 备份工具：`python tools/backup.py`（六库 VACUUM INTO 快照 + 照片 + 轮转）。
# 状态落在 data/backup_status.json，界面据此提示「备份是否超期」。
# 为什么要在界面上提示：定时任务失败是**静默**的 —— 没人盯 journal，
# 等到需要恢复时才发现最近一份是三个月前的，那时已经来不及了。
BACKUP_STATUS_FILE = DATA_DIR / "backup_status.json"
# 超过这个小时数没有成功备份就告警。默认 30：按「每天一次」的节奏，
# 偶发漏一次（关机、磁盘满）不会立刻飘红，漏两天就会。
BACKUP_STALE_HOURS = _env_int("ARS_BACKUP_STALE_HOURS", 30)

# ---------- 业务默认值 ----------
DEFAULT_REGISTRAR = "管理员"      # 默认登记人

# 售后单号规则：年份后两位 + 月日 + 顺序号（顺序号按日重置）
# 例：2026-01-01 的第 1 单 -> 260101001；同日第 2 单 -> 260101002
ORDER_NO_SEQ_WIDTH = 3           # 顺序号位数
ORDER_NO_TOTAL_LEN = 9           # 单号总长度

LINE_NO_WIDTH = 3                # 明细行号位数 -> -001

# ---------- 扫码匹配 ----------
# 多字段自动识别：输入任意码，按此顺序尝试精确匹配
MATCH_FIELDS = [
    ("return_no", "退回单号"),
    ("product_code", "产品编号"),
    ("material_no", "料号"),
    ("order_no", "售后单号"),
    ("product_model", "产品型号"),
]
# 精确匹配未命中时，是否启用模糊匹配（后缀 / 包含）
MATCH_FUZZY_ENABLED = True

# ---------- 产品编号 → 生产年月 ----------
# 产品编号前 4 位即生产年月（YYMM）：
#   20100341 → 20=2020 年，10=10 月
#   19121192 → 19=2019 年，12=12 月
# 前 4 位非纯数字、或月份不在 01-12、或年份超出合理范围时，
# 生产年月统一写 PERIOD_UNKNOWN（与历史数据既有口径一致）。
CODE_PERIOD_PREFIX_LEN = 4
CODE_PERIOD_CENTURY = 2000       # 年份 = 该值 + YY
CODE_PERIOD_MONTH_SUFFIX = "月"   # 月份格式：10 → "10月"
PERIOD_UNKNOWN = "无法确认"

# ---------- 字典字段 ----------
# 这些字段的下拉选项由数据库现有数据动态生成，无需手工维护。
# 前端的可搜索下拉框支持「库内没有则录入新值」，提交后自动汇入候选。
# 退回登记库的字典字段（候选值由退回登记数据汇总）
RETURNS_DICT_FIELDS = [
    "turbine_vendor", "project_site", "product_model",
    "product_category", "product_name", "spec", "material_no",
    "feedback_issue", "info_source", "production_stat",
]
# 检测登记库的字典字段（候选值由检测登记数据汇总）
# 注意：solution / issue_category / responsibility / completion 不在此列 ——
# 它们已改为「纯下拉锁定项」（见 FIXED_OPTIONS），不再自动积累候选。
# completion 尤其不能靠积累：未完结存的是**空值**，永远进不了候选表，
# 结果筛选下拉里只有「已完结」一项（2026-09-22 用户报的另一个现象）。
INSPECT_DICT_FIELDS = [
    "test_result", "fault_cause", "improvement",
]
# 处理登记库的字典字段（2026-09-20 从检测登记拆出，见 handle.db）
# 注意：erp_handled 已改为「纯下拉锁定项」（见 FIXED_OPTIONS），不再积累候选 ——
# 它只有两种状态，让历史值去堆候选没有意义，还会让口径漂移。
HANDLE_DICT_FIELDS = [
    "handle_solution",
]

# 处理登记库的空值默认值：库里为空 / 没有行时，语义上等价于这个值。
# 用途有三处，缺一处就会出现「同一个字段在不同地方显示不一样」：
#   1. 跨库查询取列时用 COALESCE 归一（repository._DETAIL_COLS）
#   2. 筛选时把空值一并算进该默认值（repository._build_where）
#   3. 处理登记页的下拉默认选中
HANDLE_DEFAULT_VALUES = {
    "erp_handled": "待处理",
}
# 「已处理」的取值 —— 待处理判定要用它做反选（`<> 已处理` 而不是 `= 待处理`），
# 这样历史上残留的空值也会被算作待处理，不会漏单。
HANDLE_DONE_VALUES = {
    "erp_handled": "已处理",
}

# ---------- 完结状况：自动判定 ----------
# 「完结状况」不接受手工填写，由下列检测字段算出：
# **全部有值** → 已完结；只要有任一项为空 → 未完结（存空值）。
# 不参与判定的字段（三者都是「事后登记」性质的记录项，缺了不影响检测结论）：
#   report_no（报告编号）· photo_evidence（照片证据）· erp_handled（ERP处理）
COMPLETION_FIELDS = [
    "test_date", "test_result", "fault_cause", "improvement",
    "solution", "issue_category", "responsibility",
]
COMPLETION_DONE = "已完结"
# 未完结态。**存储层仍然是空值**（跨库判定统一用 `NOT LIKE '%完结%'`，见本节末尾），
# 但在**读取 / 展示 / 筛选**三处都归一成这个值 —— 否则同一个字段会出现
# 「筛选下拉里没有『未完结』、明细里显示『未填写』」这种自相矛盾的表现
# （2026-09-22 用户报的就是这个）。机制照 `HANDLE_DEFAULT_VALUES`。
COMPLETION_PENDING = "未完结"
# 检测侧字段的空值归一表（与 HANDLE_DEFAULT_VALUES 同一机制，见 repository._value_expr）。
# ⚠️ 归一值**只用在下游展示/筛选**：写库仍是空值，SQL 里的裸列判定不受影响。
INSPECT_DEFAULT_VALUES = {
    "completion": COMPLETION_PENDING,
}
# 明确排除在判定之外的字段 —— 供文档与工具输出统一引用，
# 避免说明文案与 COMPLETION_FIELDS 各自漂移。
# 三者都是「事后登记」性质：erp_handled 现已拆到处理登记模块（handle.db），
# 报告编号与照片证据仍留在检测登记库，但同样不参与完结判定。
COMPLETION_EXCLUDED = ["report_no", "photo_evidence", "erp_handled"]
# ---------- 发货申请：状态机与单号 ----------
# **不做审批流程**：提交即进入待发货队列。下面两个取值是给将来接审批预留的
# 扩展点，当前不可达（详见《发货申请单-字段定义》第四节与《框架大纲》5.2）。
DELIVERY_STATUS = {
    "draft":     "草稿",
    "submitted": "已提交",   # 无审批时：提交后直接到此，同时进入「待发货清单」
    "shipped":   "已发货",
    "closed":    "已关闭",
    # ---- 审批流程预留，当前不可达 ----
    # "pending_approval": "待审批",
    # "rejected":         "已驳回",
}
# 新建申请单提交后的落库状态
DELIVERY_STATUS_SUBMIT = "submitted"
# 出队（标记已发货）后的状态 —— ② 待发货清单据此把单子移出队列
DELIVERY_STATUS_SHIPPED = "shipped"
# 单号规则：FH + YYYYMMDD + 3 位当日流水（例 FH20260921001）
DELIVERY_NO_PREFIX = "FH"
DELIVERY_NO_SEQ_WIDTH = 3

# ---------- 发货记录（delivery_shipment）----------
# ★ 2026-09-21 改版：发货记录不再由「待发货清单标记已发货」产生，
#   而是**发货员在 ③ 发货跟踪页对某一行登记**时产生（sources 里的 "request"）。
#   待发货清单从此**不写任何数据**（纯只读清点视图）。
DELIVERY_SHIP_SOURCES = {
    "request": "按申请单登记",   # ③ 发货跟踪：对申请明细行登记（主流程）
    "import":  "批量导入",       # ③ 发货跟踪：Excel 粘贴；将来 ERP 同步也走这里
    "manual":  "手工登记",       # 历史数据（入口已于 2026-09-21 移除）
    # "erp":   "ERP 同步",       # 预留：接 ERP 发货清单时启用
}
DELIVERY_SHIP_SOURCE_DEFAULT = "request"

# ③ 发货跟踪的**三种行态**（列表里每行必居其一）：
#   pending  —— 申请已提交、还没登记发货（这是列表主体，申请一提交就有）
#   shipped  —— 已登记发货，带着 ERP 单号
#   unlinked —— 有发货记录但对不上申请明细（导入/ERP 来的），等人工挂接
# 「发货跟踪」跟踪的就是**申请了的货发出去了没有**，所以主体是申请明细，
# 而不是发货记录 —— 后者只反映"已经发生的事"，回答不了"还差哪些"。
DELIVERY_TRACK_STATES = {
    "pending":  "待发货",
    "shipped":  "已发货",
    "unlinked": "未关联",
}
# 发货记录可写字段
DELIVERY_SHIP_FIELDS = (
    "request_no", "ship_no", "ship_date", "express_no", "carrier",
    "qty", "source", "remark", "sync_at",
    "line_no", "material_no", "product_model", "need_return",
)

# ---------- 发货明细（ship_detail）----------
# 发货模块下的**只读镜像**：把 ERP(U9) 的出货明细整表拉到本地，供查询与追溯。
#
# ⚠️ 为什么另建一张表，而不是直接写进 delivery_shipment：
#   delivery_shipment 是**核销工作集** —— 每一行都要挂到申请明细上（request_no）、
#   并参与「核销台账」的收发差额计算。ERP 那 8.9 万行出货明细一旦灌进去，
#   发货跟踪的「未关联」桶会立刻涨到 8.9 万行、台账差额随之失去意义。
#   发货明细回答的是「ERP 里到底发了哪些货」，是**参照数据**，不是核销对象。
#   （原设计确实预留了 source='erp' 写 delivery_shipment 的路子 ——
#     要做「按 ERP 发货单号核销」时再启用，不是这张表的职责。）
SHIP_DETAIL_STATUS = {0: "草稿", 1: "开立", 2: "核准中", 3: "已核准"}
# ⚠️ 状态**必须**在 SQL 里只取原始整数：cp936 连接下中文常量按 varchar 送达、
#   与库排序规则不匹配，整列会返回乱码（实测 `ò?o?×?`）。翻译在 Python 本地做。
SHIP_DETAIL_STATUS_ORDER = ["已核准", "核准中", "开立", "草稿"]
# 列表默认只看已核准 ——「草稿 / 开立」是还没真正出库的单据，
# 混进明细里会让「发了多少」这个数偏大。
SHIP_DETAIL_DEFAULT_STATUS = "已核准"

# 明细字段：(列名, 表头, 是否纳入关键词搜索)
SHIP_DETAIL_COLUMNS = (
    ("doc_date",      "日期",     False),
    ("doc_status",    "状态",     False),
    ("doc_no",        "单号",     True),
    ("doc_type",      "单据类型", True),
    ("material_no",   "料号",     True),
    ("material_name", "料品名称", True),
    ("spec",          "规格",     True),
    ("product_model", "型号",     True),
    ("qty",           "出货数量", False),
    ("serial_no",     "序列号",   True),
    ("customer",      "客户名称", True),
    ("contact",       "联系人",   True),
    ("express_no",    "承运单号", True),
    ("address",       "地址",     True),
)
SHIP_DETAIL_HEADERS = [c[1] for c in SHIP_DETAIL_COLUMNS]
SHIP_DETAIL_SEARCH_FIELDS = [c[0] for c in SHIP_DETAIL_COLUMNS if c[2]]
# 可排序字段（含同步时间，便于看「这批是哪次拉进来的」）
SHIP_DETAIL_SORTABLE = [c[0] for c in SHIP_DETAIL_COLUMNS] + ["sync_at"]
SHIP_DETAIL_DEFAULT_SORT = "-doc_date"

SHIP_DETAIL_PAGE_SIZE = 50
SHIP_DETAIL_MAX_PAGE_SIZE = 500
SHIP_DETAIL_EXPORT_PREFIX = "发货明细"
# 导出上限：全量 8.9 万行 CSV 约 24 MB，属正常用法；
# 这个数只是防「误点导出把内存打满」，不是产品限制。
SHIP_DETAIL_EXPORT_LIMIT = 200000
# 超过这个小时数没同步过，页面打「已超期」。
# 给到 3 天是因为出货明细是**月度对账**用的参照数据，不是实时台账。
SHIP_DETAIL_STALE_HOURS = 72
# 同步状态文件
SHIP_DETAIL_STATUS_FILE = DATA_DIR / "ship_detail_status.json"

# ---------- 核销台账（ledger_clear）----------
# 核销维度：**整机厂家 + 项目风场**（2026-09-22 用户口径）。
#
# ⚠️ 变过两次，别再按老注释去改：
#   ① 最早大纲写「料号」—— 实测退回侧 material_no 100% 为空，不可用；
#   ② 中途改成「厂家 + 风场 + 产品型号」—— 型号是收发两侧唯一的共同语言，能算；
#   ③ 2026-09-22 用户定调**去掉型号**：台账要看的是「这个风场到底还差多少台没回来」，
#      型号一拆，同一次发货会被切成好几行，用户要自己心算合计 —— 反而更难用。
#      （型号仍在发货行明细与二级页里展示，只是不参与维度聚合。）
DELIVERY_LEDGER_DIMENSIONS = ("turbine_vendor", "project_site")
# 手工清账的原因最少字数（台账上唯一要人写理由的地方，太短等于没写）
DELIVERY_CLEAR_REASON_MIN = 2
# 台账导出的文件名前缀
DELIVERY_LEDGER_EXPORT_PREFIX = "核销台账"

# 发货明细的「产品」是模糊搜索框：命中候选只返回前 N 条，
# 其余靠继续输入关键词收窄（避免「风速」这类词一发命中几百条把面板撑爆）。
DELIVERY_MATCH_LIMIT = 40
# 打分权重 —— 高 → 低。料号唯一、命中即定，排最高；描述只作兜底，最低。
# 型号给了「前缀」档：用户常常只记得型号前几个字符（如 BLDL03）。
DELIVERY_MATCH_SCORES = {
    "material_no_exact": 1000, "material_no_prefix": 800, "material_no_part": 600,
    "model_exact": 500, "model_prefix": 450, "model_part": 400,
    "name_part": 300, "spec_part": 200, "desc_part": 100,
}

# 未完结存空值而非「未完结」—— 跨库判定统一用 `completion NOT LIKE '%完结%'`，
# 这样「没检测过」与「检测了但没填完」在 SQL 里表现一致。
# 全部字典字段（前端一次性拉取候选时使用）
DICT_FIELDS = RETURNS_DICT_FIELDS + INSPECT_DICT_FIELDS + HANDLE_DICT_FIELDS
# 单个字段最多保留的候选值数量（按使用次数取前 N 个）
DICT_OPTION_LIMIT = 2000

# ---------- 照片证据（图片上传）----------
# 文件落 `data/photos/<售后单号>/`，字段 `photo_evidence` 里存**文件名 JSON 数组**。
# 每张图另生成一份 .thumb.jpg 缩略图供列表快速加载，原图完整保留。
PHOTO_MAX_PER_RECORD = 20              # 每条明细最多几张
PHOTO_MAX_MB = 10                      # 单张大小上限（MB）
PHOTO_THUMB_SIZE = (320, 320)          # 缩略图长边上限
PHOTO_THUMB_QUALITY = 82
# 允许的图片格式（Pillow 可解码为准；HEIC 需额外依赖，暂不支持）
PHOTO_EXTS = [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]

# ---------- 回收站 ----------
# 删除的记录（含关联的检测 / 处理登记行与照片文件）保留多少天。
# 照片是「搬进 photos_trash 暂存」而不是删除，所以保留期内还原能连图一起回来；
# 过期的由定时器清理，彻底删除。
RECYCLE_DAYS = 7
# 过期清理的节拍（小时）。启动时也会先清一次，所以停机再久也不会漏。
RECYCLE_PURGE_HOURS = 6

# ---------- 快递公司 ----------
# 人工选择时的候选清单（登记页为纯下拉，不接受自由输入）
CARRIER_OPTIONS = [
    "顺丰速运", "京东物流", "圆通速递", "中通快递", "韵达速递",
    "申通快递", "极兔速递", "邮政EMS", "德邦快递", "菜鸟速递",
]
# 退回单号前缀 → 快递公司（退回登记时自动识别）
# 其余快递公司的单号为纯数字，无法从单号区分，只能人工选择
CARRIER_PREFIX_RULES = [
    ("SF", "顺丰速运"),
    ("JD", "京东物流"),
    ("YT", "圆通速递"),
]

# ---------- 固定选项字段 ----------
# 这些字段不接受自由输入，只能从下列选项中挑选（前端渲染为纯下拉）
FIXED_OPTIONS = {
    "info_source": ["销售端", "售后端"],
    # ERP 处理：两值状态，不再是自由文本。空值一律按「待处理」呈现与统计
    # （见 HANDLE_DEFAULT_VALUES），因此这里必须给出「待处理」这一项，
    # 否则用户看到的是默认值却选不回去。
    "erp_handled": ["已处理", "待处理"],
    # 完结状况：两值枚举。**「未完结」必须在清单里**，否则用户看到的是
    # 归一后的「未完结」，想筛它却无此项可选（它本来就没存进库）。
    "completion": [COMPLETION_DONE, COMPLETION_PENDING],
    "analysis_report": ["是", "否"],
    "carrier": CARRIER_OPTIONS,
    # --- 检测结论类：口径锁定，不接受自由输入 ---
    # 这三项原先走「可搜索下拉 + 自动积累候选」，会出现同一含义多种写法
    # （如「无法判定」与「无法判断」），改为固定清单以保证统计口径统一。
    "solution": [
        "维修入库", "检测入库", "拆解报废", "供方分析",
        "维修返回", "原件返回", "拆解入库",
    ],
    "issue_category": [
        "NTF", "客户应用", "制程问题", "来料问题",
        "设计选型", "产品设计", "无法判断",
    ],
    "responsibility": ["贝良端", "客户端", "供应商端", "其他端"],
}

# 各字段中文名（前端表头、导出列名统一取此处）
FIELD_LABELS = {
    "detail_key": "明细唯一键",
    "order_no": "售后单号",
    "turbine_vendor": "风机厂家",
    "project_site": "项目风场",
    "return_no": "退回单号",
    "carrier": "快递公司",
    "return_date": "退回时间",
    "line_no": "行号",
    "product_code": "产品编号",
    "product_model": "产品型号",
    "product_category": "产品类别",
    "production_year": "生产年份",
    "production_month": "生产月份",
    "return_qty": "退回数量",
    "material_no": "料号",
    "product_name": "品名",
    "spec": "规格",
    "production_stat": "生产统计",
    "match_path": "匹配路径",
    "match_status": "匹配状态",
    "registrar": "登记人",
    "remark": "备注",
    "registered_at": "登记时间",
    "test_date": "检测时间",
    "feedback_issue": "反馈现象",
    "test_result": "检测结果",
    "fault_cause": "故障原因",
    "improvement": "改善措施",
    "solution": "处理方案",
    "issue_category": "问题分类",
    "responsibility": "责任归属",
    "photo_evidence": "照片证据",
    "report_no": "报告编号",
    "analysis_report": "分析报告",
    "erp_handled": "ERP处理",
    # 处理登记专属，与检测登记里那个锁定的「处理方案」（solution）是两件事：
    # 检测侧回答「这台设备怎么处置」，处理侧回答「这单后续怎么跟」。
    "handle_solution": "后续处理方案",
    "info_source": "快递归属",
    "completion": "完结状况",
    "source": "数据来源",
    # ---- 发货申请（主表 + 明细）----
    "request_no": "申请单号",
    "applicant": "申请人",
    "apply_date": "申请日期",
    "status": "状态",
    "expect_ship_date": "期望发货日",
    "ship_address": "收件地址",
    "ship_contact": "联系人",
    "ship_phone": "电话",
    "replace_reason": "调换原因",
    "express_req": "快递要求",
    "qty": "数量",
    "need_return": "是否需要返回",
    "description": "物料描述",
    # ---- 发货记录（delivery_shipment）----
    # 注意：carrier（快递公司）/ remark（备注）/ source（数据来源）三个 key
    # 上面已有，直接复用，不要重复定义（后写的会覆盖前写的）。
    "ship_no": "发货单号",
    "ship_date": "发货日期",
    "express_no": "物流单号",
    "sync_at": "同步时间",
}

# 匹配数据库（物料主档）字段中文名
ITEM_LABELS = {
    "material_no": "料号",
    "old_material_no": "旧料号",
    "product_name": "品名",
    "model_no": "型号",
    "spec": "规格",
    "description": "描述",
    "customer": "客户",
    "production_stat": "生产统计",
    "param1": "产品量程",
    "param2": "信号输出",
    "param3": "防护等级",
    "category": "产品类别",
    "source": "来源",
    "created_at": "创建时间",
    "updated_at": "更新时间",
}

# 导入物料主档时，Excel 表头 → 数据库列
# 同一列可挂多个表头别名（新名与旧名都接受），便于逐步替换源文件表头
ITEM_IMPORT_MAP = {
    "旧料号": "old_material_no",
    "料号": "material_no",
    "品名": "product_name",
    "型号": "model_no",
    "规格": "spec",
    "描述": "description",
    "客户": "customer",
    "生产统计": "production_stat",
    "产品量程": "param1", "标参1": "param1",
    "信号输出": "param2", "标参2": "param2",
    "防护等级": "param3", "标参3": "param3",
    "产品类别": "category",
}
