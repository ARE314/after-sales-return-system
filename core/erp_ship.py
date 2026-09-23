"""发货明细同步：ERP(U9) 出货明细 → delivery.db.ship_detail

「发货明细」子模块的数据源。把 ERP 的出货明细拉到本地做**只读镜像**，
供查询与追溯；不参与发货跟踪 / 台账的核销计算（原因见 core/db.py 的
`CREATE_SHIP_DETAIL` 注释）。

--------------------------------------------------------------------------
三条必须遵守的取数规则（都是实测踩出来的，改 SQL 前先读这里）
--------------------------------------------------------------------------
1. **charset 只能是 cp936**。pymssql 默认 UTF-8 时 FreeTDS 在本机这个 build 下
   utf-8 ↔ UCS-2LE 转换装配失败，登录包就被服务端拒掉，报
   「Error converting characters into server's character set」。

2. **文本列一律 CAST 成 VARBINARY 再本地解码**。cp936 下 FreeTDS 转 nvarchar
   时按**字符数**而非字节数分配缓冲，中文被截在半个字上，客户端解 GBK 抛
   `'gbk' codec can't decode byte 0xb0`。所以文本列取原始字节、本地按
   utf-16-le 解。**这不是「少数列偶尔出问题」**：某列当前没录中文就不报错，
   等录进去才炸 —— 是个定时炸弹。

3. **SQL 里绝不写中文常量**。`iif(a.Status=0,'草稿',…)` 在 cp936 连接下按
   varchar 送达、与库排序规则不匹配，整列返回乱码（实测 `ò?o?×?`）。
   状态只取原始整数，翻译在 Python 本地用 config.SHIP_DETAIL_STATUS 做。

三条规则的具体实现分别在 `core/erp_sync.py`（规则 1、2，以 `connect_erp` /
`decode_erp_text` 复用）与本模块（规则 3）。

--------------------------------------------------------------------------
账号权限：`sh_report_user` 是**列级授权**，不是表级
--------------------------------------------------------------------------
上面那条查询引用的每一列都在授权清单内，但 `SELECT COUNT(*)` 会因为没有
`SM_ShipLine.ID` 的 SELECT 权限而失败（错误 230）。
**不要用 COUNT(*) 探活** —— 会得出「查询跑不通」的错误结论。

--------------------------------------------------------------------------
凭证
--------------------------------------------------------------------------
**本模块自己一份**（2026-09-22 起）：环境变量 `ARS_SHIP_ERP_*` >
`auth_db.setting` 的 `ship_erp_*` > 代码里的 `CONN_DEFAULTS`。
与匹配数据库那份（`erp_*`）**互相独立** —— 两个数据源本来就可能换成
不同账号 / 不同库；共用时「改一处忘另一处」最难发现（两个界面都正常，
只有一个同步在后台连不上）。升级时 `ensure_conn_seeded()` 会把老那份搬一次。

⚠️ **连接方式仍只有一份实现**：cp936 + varbinary/utf-16-le 那两条坑
（见上）复用 `core/erp_sync.connect_erp` / `decode_erp_text`。
"""
import json
import threading
import time
import unicodedata
from datetime import datetime, timedelta

from config import (SHIP_DETAIL_STALE_HOURS, SHIP_DETAIL_STATUS,
                    SHIP_DETAIL_STATUS_FILE)
from core import erp_conn, erp_sync, sched

# ⚠️ 2026-09-22 去掉「同步范围」这套东西（SCOPE_KEYS / set_scope /
#    auth_db.setting 里的 ship_detail_date_from|to）：用户口径是「不留范围入口」——
#    「立即同步」固定走常规增量，「全量同步」固定整表覆盖，两条路都不需要
#    用户填日期。留着一个可写的范围，等于给「点一次就把 8.9 万行换成一段区间」
#    留后门。

# 自动同步开关与间隔。键名**刻意避开 `erp_*` / `ship_erp_*`**：这两个任务的
# 配置项将来若被合回同一个 payload（或谁把某张白名单放宽），同名键就会变成
# 「在发货明细上调间隔，顺手把匹配数据库的间隔也改了」—— 那种错不报错，
# 只看结果才会发现。存的是
# `ship_detail_auto_enabled` / `ship_detail_auto_interval_hours`。
AUTO_PREFIX = "ship_detail_"
AUTO_KEYS = ("auto_enabled", "auto_interval_hours")
AUTO_DEFAULTS = {"auto_enabled": "1", "auto_interval_hours": "8"}

# ---------------------------------------------------------------------------
# ERP 连接（**发货明细自己一份**，与匹配库那份分开）
# ---------------------------------------------------------------------------
# 2026-09-22 之前两份共用 `erp_*`；现在各存各的，键前缀与环境变量都独立。
CONN_PREFIX = "ship_erp_"
CONN_ENV_KEYS = {
    "host": "ARS_SHIP_ERP_HOST",
    "port": "ARS_SHIP_ERP_PORT",
    "db": "ARS_SHIP_ERP_DB",
    "user": "ARS_SHIP_ERP_USER",
    "password": "ARS_SHIP_ERP_PASSWORD",
}
CONN_DEFAULTS = {
    "host": "192.168.1.247",
    "port": "1433",
    "db": "BLFN",
    "user": "sh_report_user",
    # 与匹配库那边一样：**密码没有默认值** —— 缺了就是没配好，
    # 同步会明确报错，而不是拿一个写死的弱口令去连生产库。
    "password": "",
}
# 「老那份共用配置已经搬过来」的标记。用它而不是「检查键空不空」——
# 用户主动清空发货明细的连接时，不该被再搬一次。
CONN_SEEDED_KEY = "ship_erp_seeded"


def get_conn(redact: bool = True) -> dict:
    """读**发货明细自己那一份** ERP 连接。首次调用会把老的共用配置搬过来。"""
    ensure_conn_seeded()
    return erp_conn.read(CONN_PREFIX, CONN_ENV_KEYS, CONN_DEFAULTS,
                         redact=redact)


def set_conn(data: dict) -> dict:
    """写发货明细自己的连接。密码只写不读；空值不留库（回落默认值）。"""
    return erp_conn.write(CONN_PREFIX, CONN_ENV_KEYS, CONN_DEFAULTS, data)


def ensure_conn_seeded() -> bool:
    """把 2026-09-22 之前那份**共用**的 `erp_*` 连接搬成发货明细自己的。

    不搬的后果很直接：密码当时存在 `erp_password` 里，拆开后本模块读
    `ship_erp_password` —— 拿不到密码就是「未配置 ERP 密码」，自动同步
    立刻静默停摆（`auto_due()` 返回 False，界面上什么都不红）。

    幂等：只在 `CONN_SEEDED_KEY` 标记不存在时搬一次；且**只在某一项还没被
    单独配过**时才覆盖它（免得把用户已经改好的值冲掉）。
    """
    from core import auth
    if auth.get_setting(CONN_SEEDED_KEY, ""):
        return False
    shared = erp_sync.get_config(redact=False)
    for k in CONN_DEFAULTS:
        if auth.get_setting(CONN_PREFIX + k, ""):
            continue                       # 已经单独配过，不动
        v = str(shared.get(k) or "").strip()
        if v:
            auth.set_setting(CONN_PREFIX + k, v)
    auth.set_setting(CONN_SEEDED_KEY, "1")
    return True

# 自动同步的口径（2026-09-22 用户定）：定时任务**不再全量拉取**，只刷两类数据：
#   ① ERP 里单据日期落在「今天 / 前一天」的（刚出库、还在变的单）；
#   ② 本地状态还没核准的（草稿 / 开立 / 核准中）—— 这些单在 ERP 那边还会改，
#      而且它们多半早于前一天，光靠日期窗口永远刷不到。
# 另外：本地未核准的单据如果在 ERP 里被删掉了，本地也要跟着删（用户口径），
# 这件事由「按同一个谓词先删后插」自然完成 —— 删掉的是单号集合里的行，
# 而 ERP 没返回行就意味着不再插回去。
# 全量只留给手动「立即同步」（范围留空），用来重建镜像或对账。
# 未核准单号有上限：MSSQL 一条语句最多 2100 个参数，这里留足余量取 800。
AUTO_WINDOW_DAYS = 2
PENDING_DOC_LIMIT = 800

_LOCK = threading.Lock()
_runtime: dict = {"running": False, "started_at": "", "last_error": ""}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 查询构造
# ---------------------------------------------------------------------------
# (输出键, SQL 表达式, 是否文本列)
#   文本列 → CAST 成 VARBINARY 取原始字节，本地 utf-16-le 解（规则 2）
#   非文本列原样取：qty 是 decimal、_status 是 int、_date 是 datetime
#   `_` 前缀的是中间列，翻译完就从结果里去掉，不落到表上。
_SELECT = (
    ("doc_no",        "a.DocNo",                         True),
    ("doc_type",      "d.Name",                          True),
    ("material_no",   "b.ItemInfo_ItemCode",             True),
    ("material_name", "b.ItemInfo_ItemName",             True),
    ("spec",          "e.SPECS",                         True),
    ("product_model", "e.Code1",                         True),
    ("serial_no",     "b.DescFlexField_PrivateDescSeg6", True),
    ("customer",      "g.Name",                          True),
    ("contact",       "a.DescFlexField_PrivateDescSeg1", True),
    ("express_no",    "a.DescFlexField_PrivateDescSeg5", True),
    ("address",       "a.DescFlexField_PrivateDescSeg2", True),
    ("qty",           "b.ShipQtyInvAmount",              False),
    ("_status",       "a.Status",                        False),
    ("_date",         "a.BusinessDate",                  False),
)

_UNICODE_CAST = "CAST({col} AS VARBINARY(MAX))"

# 与用户给定的 U9 出货查询**逐字对应**，只做两处改动：
#   · 表名加 dbo. 前缀
#   · 去掉 `iif(Status=…,'草稿',…)` 的中文常量翻译（规则 3）
# 刻意**不加** `b.ItemInfo_ItemCode IS NOT NULL` 之类的过滤 —— 保持与
# 用户那条查询的行集合一致，免得日后对账时数字对不上却查不出差在哪。
_SQL_TEMPLATE = """
SELECT {cols}
FROM dbo.SM_Ship a
LEFT JOIN dbo.SM_ShipLine b       ON b.Ship = a.ID
LEFT JOIN dbo.SM_ShipDocType c    ON c.ID = a.DocumentType
LEFT JOIN dbo.SM_ShipDocType_Trl d ON d.ID = c.ID
LEFT JOIN dbo.CBO_ItemMaster e    ON e.Code = b.ItemInfo_ItemCode
LEFT JOIN dbo.CBO_Customer f      ON f.ID = a.OrderBy_Customer
LEFT JOIN dbo.CBO_Customer_Trl g  ON g.ID = f.ID
"""


def _build_query(date_from: str = "", date_to: str = "", doc_nos=None):
    """返回 (sql, params)。

    日期比较用 `CONVERT(varchar(10), …, 23)` 而不是直接比 datetime：
    style 23 就是 `yyyy-mm-dd`，字符串比较无歧义、不受会话语言/
    DATEFORMAT 设置影响，也不会因为传进去的字符串被当 varchar 而暗地转换出错。
    代价是用不上 BusinessDate 的索引 —— 8.9 万行的全表扫完全可以接受。

    `doc_nos` = 本地还没核准的单号清单（增量同步用）。日期窗口与单号清单之间
    是**或**关系：窗口管新单，单号管「旧、但还在改」的单。

    ⚠️ 两者必须拼在**同一条 SQL** 里。拆成两次查询的话，「既落在日期窗口内、
    又还没核准」的单据会被取回两次，而落库是「按同一谓词先删后插」——
    这张表没有唯一键挡重复（见 repo_delivery.replace_ship_details 的说明），
    结果就是行数直接翻倍。
    """
    cols = ", ".join(
        (_UNICODE_CAST.format(col=expr) if is_text else expr)
        for _key, expr, is_text in _SELECT)

    groups, window_params = [], []
    window = []
    if str(date_from or "").strip():
        window.append("CONVERT(varchar(10), a.BusinessDate, 23) >= %s")
        window_params.append(str(date_from).strip())
    if str(date_to or "").strip():
        window.append("CONVERT(varchar(10), a.BusinessDate, 23) <= %s")
        window_params.append(str(date_to).strip())
    if window:
        # 两个日期条件之间是「与」、与单号清单之间是「或」——括号不能省，
        # 否则 SQL 的优先级（AND 高于 OR）会把「前缀 >= A AND <= B OR IN(…)」
        # 解析成正好相反的意思。
        groups.append("(" + " AND ".join(window) + ")")

    nums = [str(n).strip() for n in (doc_nos or []) if str(n).strip()]
    if nums:
        groups.append(f"a.DocNo IN ({', '.join(['%s'] * len(nums))})")

    sql = _SQL_TEMPLATE.format(cols="  " + cols.replace(", ", ",\n  "))
    if groups:
        sql += "WHERE " + " OR ".join(groups)
    # 参数顺序必须与占位符出现的顺序一致：先日期、后单号。
    return sql, window_params + nums


# ---------------------------------------------------------------------------
# 文本归一化
# ---------------------------------------------------------------------------
# ERP 的地址 / 联系人里偶尔混进「部首」字符：Kangxi 部首（U+2F00–U+2FDF）
# 与 CJK 部首补充（U+2E80–U+2EFF）。它们的码位是独立字符，但字形画的是一个
# **部首**，页面上就成了「⻛⼤⼭⽔⻢⼴⿊⻰⾃」这种缺胳膊少腿的怪字 —— 用户
# 看到的「地址变形」正是它（全库扫描：只有发货明细的 address / contact 命中，
# 其它 5 个库一处都没有）。
#
# ⚠️ 绝不能对整串做 `unicodedata.normalize("NFKC", s)`：它顺手把全角标点也
# 折了。实测本地库会被它的改动量：`（`4.4 万处→`(`、`）`4.3 万处→`)`、
# `²`1.2 万处→`2`、`，`4716→`,`、`；`1684→`;`、`：`602→`:`、`Ⅰ`499→`I`、
# `℃`→`°C`、`⑤`→`5`、`U+00A0`303 处→空格。那是另一类「变形」。
# 所以这里逐字符判断、只对部首块下手。
_CJK_RADICAL_MAP = {
    "⻅": "见", "⻉": "贝", "⻋": "车", "⻓": "长", "⻔": "门", "⻘": "青",
    "⻙": "韦", "⻚": "页", "⻛": "风", "⻜": "飞", "⻢": "马", "⻥": "鱼",
    "⻦": "鸟", "⻧": "卤", "⻨": "麦", "⻩": "黄", "⻪": "黾", "⻬": "齐",
    "⻮": "齿", "⻰": "龙", "⻳": "龟",
}

# 顺手清掉的不可见字符：ERP 字段尾部的换行/制表符会把表格行撑开、
# 把一格文字顶到隔壁列上（实测 material_name 207 行含 `\n`、
# address 9 行 `\n` + 6 行 `\t`）。NBSP 换成普通空格，零宽字符直接去掉。
_CTRL_MAP = {ord(c): " " for c in "\r\n\t\v\f\u00a0"}
_ZERO_WIDTH = {ord(c): None for c in "\u200b\u200c\u200d\u200e\u200f\ufeff"}


def normalize_text(value):
    """把 ERP 文本里的「部首字符」折成正常汉字，并清掉不可见字符。

    - U+2F00–U+2FDF（Kangxi Radicals）逐个有兼容分解，逐字 NFKC 即可；
    - U+2E80–U+2EFF（CJK Radicals Supplement）没有兼容分解，查上面那张表。
    只对命中的字符做替换，其余字符（含全角标点、`㎡`、`℃`）原样保留。
    """
    if not value:
        return value
    text = str(value)
    # 快路径：绝大多数行一个部首都没有，先做一次 O(n) 的集合判断，
    # 免得 8.9 万行 × 每行 60 字全部走 Python 循环。
    out = None
    for i, ch in enumerate(text):
        code = ord(ch)
        if 0x2F00 <= code <= 0x2FDF:
            folded = unicodedata.normalize("NFKC", ch)
        elif 0x2E80 <= code < 0x2F00:
            folded = _CJK_RADICAL_MAP.get(ch, ch)
        else:
            continue
        if out is None:
            out = list(text)
        out[i] = folded
    if out is not None:
        text = "".join(out)
    return text.translate(_CTRL_MAP).translate(_ZERO_WIDTH)


_BAD_CHARS = {cp: None for cp in range(0x2E80, 0x2FE0)}
_BAD_CHARS.update(_CTRL_MAP)
_BAD_CHARS.update(_ZERO_WIDTH)


def needs_normalize(value) -> bool:
    """字符串里是否含部首字符 / 不可见字符。

    给「存量数据迁移」和自检用：`translate` 走 C 实现，8.9 万行 × 11 列
    全表扫一遍也是秒级，比逐字符 Python 循环快一个量级。
    """
    if not value:
        return False
    text = str(value)
    return text.translate(_BAD_CHARS) != text


def _as_int_or_none(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _as_date(raw) -> str:
    if raw is None:
        return ""
    if hasattr(raw, "strftime"):
        return raw.strftime("%Y-%m-%d")
    return str(raw).strip()[:10]


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------

def auto_window() -> tuple:
    """自动同步的日期窗口 `(起点, 今天)` = 「当天及前一天」。

    只决定**刷哪些新单**。本地还没核准的旧单由单号清单另外覆盖，
    不靠把窗口拉长来兜（那会越拉越长，最后还是全量）。
    """
    today = datetime.now().date()
    start = today - timedelta(days=max(AUTO_WINDOW_DAYS - 1, 0))
    return start.isoformat(), today.isoformat()


def erp_has_ship_data(cfg: dict) -> bool:
    """ERP 里是否**至少有一条**出货记录：区分「这次查询确实没数据」与
    「账号看不见数据了」。

    ⚠️ 只能取 `a.DocNo`。`sh_report_user` 是列级授权，`COUNT(*)` 会因为拿不到
    `SM_ShipLine.ID` 的 SELECT 权限而报错 230（见模块头注释），
    那会把「权限正常但结果为空」误判成「查询跑不通」。
    """
    conn = erp_sync.connect_erp(cfg)
    try:
        cur = conn.cursor()
        cur.execute("SELECT TOP 1 a.DocNo FROM dbo.SM_Ship a;")
        return cur.fetchone() is not None
    finally:
        conn.close()


def fetch_ship_details(cfg: dict, date_from: str = "", date_to: str = "",
                       doc_nos=None) -> list:
    """从 ERP 取出货明细。返回可直接交给 repo.replace_ship_details 的行列表。

    ⚠️ **全部取回内存**后才返回 —— 调用方拿不到「逐批流出」的迭代器，
    因为落库是「按范围整体替换」，取数中途失败绝不能动本地数据。
    """
    sql, params = _build_query(date_from, date_to, doc_nos)
    conn = erp_sync.connect_erp(cfg)
    try:
        conn.autocommit(True)
        cur = conn.cursor()
        # 没有绑定参数时用单参数形式。传 `params or None` 虽然多数驱动也接受，
        # 但 pymssql 对「第二个位置参数是 None」的处理随版本而异，
        # 干脆分成两条明确的路径。
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)

        out = []
        for rec in cur:
            item = {}
            for (key, _expr, is_text), raw in zip(_SELECT, rec):
                if is_text:
                    # decode_erp_text 只负责字符集；字形层面的「部首字符」
                    # 与不可见字符由 normalize_text 处理（见上方注释）。
                    item[key] = normalize_text(
                        erp_sync.decode_erp_text(raw)).strip()
                elif key == "qty":
                    try:
                        item[key] = float(raw) if raw is not None else 0.0
                    except (TypeError, ValueError):
                        item[key] = 0.0
                elif key == "_status":
                    code = _as_int_or_none(raw)
                    # 状态码认不出来时保留 `未知(n)` 而不是丢空 ——
                    # 否则那批行会从所有状态筛选里凭空消失，且没人会发现。
                    item["doc_status"] = (
                        "" if code is None
                        else SHIP_DETAIL_STATUS.get(code, f"未知({code})"))
                elif key == "_date":
                    item["doc_date"] = _as_date(raw)
            out.append(item)
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 自动同步（多久拉一次）
# ---------------------------------------------------------------------------

def get_auto() -> dict:
    """读自动同步开关与间隔。缺省：启用、每 8 小时。"""
    from core import auth
    out = {}
    for k in AUTO_KEYS:
        raw = str(auth.get_setting(AUTO_PREFIX + k, "") or "").strip()
        out[k] = raw if raw else AUTO_DEFAULTS[k]
    return out


def set_auto(data: dict) -> dict:
    """写自动同步开关与间隔。**只认白名单键** —— 让同一个 PUT 既能配连接、
    又能配自动同步，而不会被别的字段顺手改掉东西。

    传空 = **删掉该键**（回落默认值），与连接那套规则一致（见 core/erp_conn.py）：
    在表里留一行空串的话，读出来和「没配」一模一样，看库的人必然读错。
    自检还原配置时也靠这条 —— 原值本来就是「没有这一行」时，
    写回空串并不能真的还原。"""
    from core import auth
    for k in AUTO_KEYS:
        if k in (data or {}) and data.get(k) is not None:
            v = str(data.get(k)).strip()
            if v:
                auth.set_setting(AUTO_PREFIX + k, v)
            else:
                auth.del_setting(AUTO_PREFIX + k)
    return get_auto()


def auto_hours() -> int:
    hours = _as_int_or_none(get_auto().get("auto_interval_hours"))
    return hours if hours and hours > 0 else 8


def last_sync_at():
    """上次同步时间（手动/定时都算）。读不到返回 None。

    拿状态文件的 `at` 而不是内存变量：手动同步也应当把节拍推后，
    且进程重启后「上次是什么时候」得从磁盘找回来。
    """
    try:
        d = json.loads(SHIP_DETAIL_STATUS_FILE.read_text(encoding="utf-8"))
        return datetime.strptime(str(d.get("at") or ""), "%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return None


def next_sync_at() -> str:
    """预计下次自动同步时间（界面显示用）。未启用/未配密码时返回空串。"""
    if get_auto().get("auto_enabled") != "1":
        return ""
    if not get_conn(redact=True).get("password_set"):
        return ""
    when = (last_sync_at() or datetime.now()) + timedelta(hours=auto_hours())
    if when <= datetime.now():
        return "随时（已到点）"
    return when.strftime("%Y-%m-%d %H:%M")


def auto_due() -> tuple:
    """这一拍该不该自动跑。返回 `(是否到点, 原因)`。

    `auto_enabled` 缺省就是 `'1'`（`AUTO_DEFAULTS`）—— 发货明细是**只读镜像**，
    拉多了没有副作用，而「装了却不更新」才是真的坑。
    """
    if get_auto().get("auto_enabled") != "1":
        return False, ""
    if not get_conn(redact=True).get("password_set"):
        return False, ""
    hours = auto_hours()
    last = last_sync_at()
    if last is None:
        return True, "还没有同步记录，启动后先跑一次"
    age = (datetime.now() - last).total_seconds()
    if age >= hours * 3600:
        return True, (f"距上次同步 {age / 3600:.1f} 小时，"
                      f"已达到 {hours} 小时间隔")
    return False, ""


def _run_scheduled() -> None:
    # 定时任务固定走**常规同步（增量）**：只拉「今天 + 前一天」与「本地还没核准
    # 的单据」。早先这里是「不带范围 = 每次全量」，8.9 万行一趟十几秒，
    # 而真正会变的只有这两天的新单和那几百张未核准单。
    # 整表覆盖留给管理员点「全量同步」（sync(full=True)）。
    if not sync_in_thread(operator="erp-ship", trigger="schedule", full=False):
        print("[erp-ship] 已有同步在跑，本轮跳过", flush=True)


# ---------------------------------------------------------------------------
# 状态留痕
# ---------------------------------------------------------------------------

def _write_status(ok: bool = False, **kw) -> None:
    """把最近一次同步结果落盘。`ok` 给默认值以便用 `_write_status(**payload)`。"""
    payload = {"ok": bool(ok), "at": _now(), **kw}
    try:
        SHIP_DETAIL_STATUS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[warn] 发货明细同步状态写入失败：{exc}", flush=True)


def status() -> dict:
    """最近一次同步结果 + 本地数据概况（界面据此提示「该不该重拉」）。"""
    from core import repo_delivery as repo
    cfg = get_conn(redact=True)

    out = {
        "configured": bool(cfg.get("password_set")) and bool(cfg.get("host")),
        "host": cfg.get("host"), "port": cfg.get("port"), "db": cfg.get("db"),
        "user": cfg.get("user"), "password_set": cfg.get("password_set"),
        "config_source": cfg.get("source"),
        # 上一次同步**实际**覆盖的区间与模式，来自状态文件；
        # 不再是「用户配置的范围」—— 范围入口已经去掉（见文件头注释）。
        "date_from": "", "date_to": "",
        "full": None,
        "running": bool(_runtime.get("running")),
        # 自动同步：界面的开关绑这几个键。早先匹配数据库那边的开关绑错了
        # 字段（绑到 running 上，永远显示「关」），这里两个都明确给出。
        "enabled": get_auto().get("auto_enabled") == "1",
        "interval_hours": auto_hours(),
        "next_at": next_sync_at(),
        # 自动同步到底刷哪些数据（界面上要写清楚，否则「为什么只花了两秒」
        # 会被当成没同步成功）。
        "auto_window_days": AUTO_WINDOW_DAYS,
        "auto_pending_limit": PENDING_DOC_LIMIT,
        "pending_docs": 0,
        "stale_hours": SHIP_DETAIL_STALE_HOURS,
        "ok": None, "at": "", "age_hours": None, "stale": False,
        "fetched": 0, "deleted": 0, "inserted": 0, "elapsed_ms": 0,
        "rows": 0, "first_date": "", "last_date": "",
        "status_counts": {}, "note": "", "error": "",
    }

    try:
        stats = repo.ship_detail_stats({"doc_status": ""})
        out["rows"] = stats["total"]
        out["first_date"] = stats["first_date"]
        out["last_date"] = stats["last_date"]
        out["status_counts"] = stats["by_status"]
        # 未核准单据是自动同步的第二路取数依据；这个数字要露出来，
        # 否则「本来就没有未核准单」和「漏统计了」在界面上长得一样。
        out["pending_docs"] = len(repo.pending_doc_nos())
    except Exception as exc:                                  # noqa: BLE001
        # 本地表还没建起来（首启竞态）不该让整个状态接口 500 ——
        # 状态接口是页面首屏要调的东西，它一挂整页就白屏。
        out["error"] = f"读取本地明细失败：{exc}"

    saved = {}
    try:
        if SHIP_DETAIL_STATUS_FILE.is_file():
            saved = json.loads(SHIP_DETAIL_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if saved:
        out["ok"] = saved.get("ok")
        out["at"] = saved.get("at", "")
        for k in ("fetched", "deleted", "inserted", "elapsed_ms", "error",
                  "doc_nos", "full", "date_from", "date_to"):
            if saved.get(k) not in (None, ""):
                out[k] = saved[k]
        if out["at"]:
            try:
                when = datetime.strptime(out["at"], "%Y-%m-%d %H:%M:%S")
                out["age_hours"] = round(
                    (datetime.now() - when).total_seconds() / 3600, 1)
                out["stale"] = out["age_hours"] > SHIP_DETAIL_STALE_HOURS
            except ValueError:
                pass

    if not out["configured"]:
        out["note"] = "未配置 ERP 密码 —— 同步无法运行"
    elif out["error"]:
        out["note"] = out["error"]
    elif out["ok"] is False:
        out["note"] = f"最近一次同步失败：{out['error'] or '原因未记录'}"
    elif not out["rows"]:
        out["note"] = "本地还没有数据，点「立即同步」拉取"
    elif out["stale"]:
        out["note"] = (f"本地数据来自 {out['age_hours']} 小时前，"
                       f"已超过 {SHIP_DETAIL_STALE_HOURS} 小时，建议重新同步")
    else:
        out["note"] = (f"本地 {out['rows']} 行 · 数据 {out['first_date']} ~ "
                       f"{out['last_date']} · 最近同步 {out['age_hours']} 小时前")
        if out["enabled"]:
            out["note"] += (f" · 自动同步每 {out['interval_hours']} 小时走常规同步"
                            f"（近 {AUTO_WINDOW_DAYS} 天 + 未核准 "
                            f"{out['pending_docs']} 单）；整表覆盖请点「全量同步」")
        # 上次走的是哪条路：两条路耗时差一个数量级（0.5 秒 vs 十几秒），
        # 不标出来的话「这次怎么这么快」会被当成没同步成功。
        if out["full"]:
            out["note"] += " · 上次是**全量同步**（整表覆盖）"
        elif out["full"] is False:
            out["note"] += " · 上次是常规同步（增量）"
    return out


# ---------------------------------------------------------------------------
# 同步主体
# ---------------------------------------------------------------------------

def sync(operator: str = "erp-ship", trigger: str = "manual",
         full: bool = False, _locked: bool = False) -> dict:
    """跑一次发货明细同步。

    `full=False`（默认，定时任务也走这条）＝**常规同步 / 增量**：
    日期窗口 =「今天 + 前一天」，再加本地还没核准的单号清单。
    `full=True`（界面上海管理员专用的「全量同步」）＝**整表覆盖**：
    不带任何谓词，先清空整张表再把 ERP 的出货明细全部写回。

    ⚠️ 两条路的差别不是「拉多少」而是「删多少」：增量只删「窗口内 + 未核准」
    那些行，全量删掉整张表 —— 所以全量的 0 行守卫一律中止（见下方）。

    `_locked=True` 表示调用方（`sync_in_thread`）**已经抢到锁并置好 running**，
    这里不再重复抢 —— 否则会把自己锁在门外。

    幂等：同一个「窗口 + 单号清单」重拉多少次，落库结果都一样
    （按同一谓词先删后插，而不是靠唯一键 —— 这张表没有天然唯一键）。
    """
    from core import repo_delivery as repo

    if not _locked:
        if not _LOCK.acquire(blocking=False):
            return {"ok": False, "error": "已有同步在进行中，本次跳过"}
        _runtime.update({"running": True, "started_at": _now()})

    started = time.time()
    try:
        cfg = get_conn(redact=False)
        if not cfg.get("password"):
            raise RuntimeError(
                "未配置发货明细专用的 ERP 密码。请在「自动同步任务」页的"
                "「发货明细 · ERP 出货单」卡片上点「连接配置」填写，"
                "或设置环境变量 ARS_SHIP_ERP_PASSWORD。")

        date_from = date_to = ""
        doc_nos = []
        if full:
            # 整表覆盖：谓词为空 —— 取数不带 WHERE，删除也不带 WHERE。
            span = "整表覆盖（全部日期）"
        else:
            # 增量：① 近两天的新单；② 本地还没核准的单（它们可能远早于两天前，
            # 而且 ERP 侧还会改）。两路合进同一条 SQL 的同一个 OR 谓词里。
            date_from, date_to = auto_window()
            doc_nos = repo.pending_doc_nos(limit=PENDING_DOC_LIMIT)
            span = (f"近 {AUTO_WINDOW_DAYS} 天 {date_from} ~ {date_to}"
                    f" + 未核准 {len(doc_nos)} 单")
        print(f"[erp-ship] 开始同步出货明细"
              f"（{'全量' if full else '增量'} {span}）", flush=True)

        # ★ 先把 ERP 数据全部取回内存，再整体替换。
        #   取数中途抛错时本地一行都不动 —— 不会出现「删完了却没插进去」。
        rows = fetch_ship_details(cfg, date_from, date_to, doc_nos)

        # ★ 取回 0 行时的处理分两种，不能一刀切：
        #   · 全量：0 行一律中止替换。ERP 端权限被收紧、账号换库，都可能表现为
        #     「查询成功但一行没有」；照常执行就会把本地几万行静默清空，
        #     而备份之外无从恢复。
        #   · 增量：0 行**可能是真的** —— 本地未核准的单据在 ERP 那边被删了，
        #     这恰恰是用户要的「ERP 删了本地也删」（2026-09-22 口径）。
        #     但也可能是权限坏了，所以先用最便宜的一次探活确认「账号还看得见
        #     数据」；看得见才放行删除，看不见则照旧中止。
        if not rows:
            if (not full) and erp_has_ship_data(cfg):
                print("[erp-ship] 本次 ERP 一行未返回，但账号仍能看见数据："
                      "按「这些未核准单据已在 ERP 删除」处理，同步本地删除",
                      flush=True)
            else:
                raise RuntimeError(
                    f"本次从 ERP 取回 0 行，已中止替换（范围 {span}）。"
                    "本地数据保持原样。请先确认 ERP 权限与日期范围是否正确。")

        stamp = _now()
        # ⚠️ 删除谓词必须与上面的取数谓词**逐字同构**（同一条「窗口 OR 单号」
        #    谓词；全量时两边都是「无条件」）。改了 fetch_ship_details 的入参
        #    就必须同步改这里，否则会出现「留下永不更新的旧行」或「删掉了没
        #    重新取到的行」—— 两种都不报错（见 core/repo_delivery.py 的同名提醒）。
        result = repo.replace_ship_details(rows, date_from, date_to,
                                          sync_at=stamp, doc_nos=doc_nos)
        elapsed = int((time.time() - started) * 1000)

        payload = {"ok": True, "trigger": trigger, "operator": operator,
                   "fetched": len(rows), "deleted": result["deleted"],
                   "inserted": result["inserted"],
                   "date_from": date_from, "date_to": date_to,
                   "doc_nos": len(doc_nos), "full": bool(full),
                   "elapsed_ms": elapsed, "error": ""}
        _write_status(**payload)
        print(f"[erp-ship] 完成：取回 {len(rows)} 行，"
              f"替换 {result['deleted']} → {result['inserted']} 行"
              f"（{'全量' if full else '增量'} {span}）"
              f"，耗时 {elapsed} ms", flush=True)
        return payload
    except Exception as exc:                                  # noqa: BLE001
        msg = str(exc)[:300]
        _write_status(ok=False, trigger=trigger, operator=operator,
                      error=msg, elapsed_ms=int((time.time() - started) * 1000))
        print(f"[erp-ship] 同步失败：{msg}", flush=True)
        return {"ok": False, "error": msg}
    finally:
        # 与 core/erp_sync.py 保持同一套约定：**锁由本次真正持有它的一方释放**。
        # _locked=False 时锁是本函数在上面 _LOCK.acquire 拿的；
        # _locked=True 时锁是 sync_in_thread 拿的。
        # 两种情况下都从这里的 finally 放 —— 早先写成
        # `if not _locked: release`，结果走线程那条路锁永远不放回，
        # 第二次同步起每次都返回「已有同步在进行中」。
        _runtime.update({"running": False})
        try:
            _LOCK.release()
        except RuntimeError:
            pass


def sync_in_thread(operator: str = "erp-ship", trigger: str = "manual",
                   full: bool = False) -> bool:
    """后台线程跑同步（页面上的「立即同步」与「全量同步」都走这里）。返回是否已启动。

    `full=True` = 整表覆盖（管理员专用，路由见 app.py 的 PERM_RULES）。

    ⚠️ 必须在**派生线程之前**抢锁并置 running。第一版只检查 `_runtime["running"]`
    就返回，而那个标志要等线程起来才置位 —— 结果是连点两次都能启动，
    两次同时写同一张表。这个问题在匹配数据库同步那边已经踩过一次。
    """
    if not _LOCK.acquire(blocking=False):
        return False
    _runtime.update({"running": True, "started_at": _now(), "last_error": ""})

    def _run():
        try:
            sync(operator=operator, trigger=trigger, full=full, _locked=True)
        except Exception as exc:                              # noqa: BLE001
            # sync() 自己会兜住异常并写状态；这里只是最后一道保险，
            # 保证锁一定被放回，否则一次意外会让同步永久卡死。
            print(f"[erp-ship] 线程异常：{exc}", flush=True)
            _runtime.update({"running": False, "started_at": ""})
            try:
                _LOCK.release()
            except RuntimeError:
                pass

    threading.Thread(target=_run, name="erp-ship-sync", daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# 定时调度（与 core/erp_sync.py 共用 core/sched.py 的节拍器）
# ---------------------------------------------------------------------------

_ticker = sched.Ticker("erp-ship", due=auto_due, run=_run_scheduled)


def start_scheduler() -> dict:
    """拉起自动同步线程。**幂等** —— `main()` 与 `lifespan` 都会调。"""
    return _ticker.start()


def stop_scheduler() -> None:
    _ticker.stop()


# 便于自检直接拿到最终 SQL（不连库）
build_query = _build_query

__all__ = ["fetch_ship_details", "status", "sync",
           "sync_in_thread", "build_query", "get_auto", "set_auto",
           "auto_hours", "auto_due", "next_sync_at", "last_sync_at",
           "auto_window", "erp_has_ship_data", "AUTO_WINDOW_DAYS",
           "PENDING_DOC_LIMIT", "start_scheduler", "stop_scheduler"]
