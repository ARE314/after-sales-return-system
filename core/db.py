"""多库数据层 —— 建表、索引、轻量迁移（MySQL 8）

库划分（一个模块一个 schema）
----------------------------
    returns_db   退回登记库   returns / model_dict / dict_option / op_log
    inspect_db   检测登记库   inspect_records / dict_option / op_log
    handle_db    处理登记库   handle_records / dict_option / op_log
    items_db     匹配数据库   item_master
    delivery_db  发货库       delivery_request / delivery_request_item
                              / delivery_shipment / ledger_clear / ship_detail
    auth_db      接入库       users / permission_group / session / setting
                              / open_api_log

六个 schema 建在**同一个 MySQL 实例**上，跨库 JOIN 的写法和单库完全一致：

    SELECT r.*, i.*, h.*
    FROM returns r
    LEFT JOIN inspect_db.inspect_records i ON i.detail_key = r.detail_key
    LEFT JOIN handle_db.handle_records  h ON h.detail_key = r.detail_key

（2026-09-22 之前是「6 个 SQLite 文件 + ATTACH」。改用 MySQL 的 schema.table 之后
这些前缀**一个字都没改** —— 连接默认 schema 就是 returns_db，所以不带前缀的
表名照旧落在退回登记库。）

约定
----
* **关联键统一为 `detail_key`**（明细唯一键 = 售后单号-行号）。
* **检测 / 处理记录稀疏存储**：没检测过的明细在检测库里没有行，
  没处理的在 handle 库里没有行。因此所有跨库查询必须用 LEFT JOIN，
  判断「未检测」用 `i.detail_key IS NULL`；
  对这些字段做条件判断时必须先 COALESCE —— LEFT JOIN 未命中时该侧全为 NULL，
  而 `NULL NOT LIKE '%x%'` 的结果是 NULL（不成立），会漏掉未检测的记录。
* **日期与时间列一律 VARCHAR(32)，不用 DATETIME**。理由：全项目的日期比较
  都在 SQL 里按字符串做（`test_date >= '2026-01-01'`），Python 侧也当字符串
  格式化、直接塞进 JSON 回给前端。换成 DATETIME 会让 PyMySQL 返回 datetime
  对象，字符串比较失效、JSON 序列化也变样 —— 那是另一场事故，不是本次迁移
  该顺手做的事。真需要日期运算的地方显式写 `STR_TO_DATE(col, '%Y-%m-%d')`。
* **写入不跨库**的旧约束已取消：MySQL 的 InnoDB 事务本身跨 schema 原子。
"""
import threading
from datetime import datetime

from config import (AUTH_DB, DELIVERY_DB, DELIVERY_STATUS_SUBMIT, DATA_DIR,
                    DELIVERY_DB as _DELIVERY, EXPORT_DIR, HANDLE_DB,
                    HANDLE_DONE_VALUES, INSPECT_DB, ITEMS_DB, MAIN_SCHEMA,
                    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, RETURNS_DB, SCHEMAS)
from core import dbapi
from core.dbapi import close_conn, get_conn, tx          # noqa: F401 —— 对外仍叫 db.get_conn()

__all__ = ["InvalidField", "get_conn", "tx", "close_conn", "init_db",
           "db_status", "INDEXES", "COLUMNS_MYSQL"]

_local = threading.local()


class InvalidField(ValueError):
    """字段名不在白名单里。

    三个仓储模块的 `_safe*()` 都抛它。放在 db.py 是因为它是三者共同依赖的
    最底层（repo_inspect / repo_handle 都从这里导入，不会循环导入）。

    **必须由路由层统一转成 400** —— 否则这条 ValueError 会一路冒到 ASGI，
    变成 500 并把堆栈写进日志。实测 `GET /api/dict/<任意非字段名>` 就是 500，
    任何人（登录后）都能拿它刷错误日志。
    它继承 ValueError，所以既有的 `except ValueError` 仍然能接住。
    """


# 检测登记库的字段（供跨库查询使用）
# 注意：erp_handled 已于 2026-09-20 迁到处理登记库（见 HANDLE_COLUMNS）
INSPECT_COLUMNS = [
    "detail_key", "test_date", "test_result", "fault_cause", "improvement",
    "solution", "issue_category", "responsibility", "completion", "report_no",
    "photo_evidence",
]
HANDLE_COLUMNS = [
    "detail_key", "erp_handled", "handle_solution",
]

# 每个表都要带的尾巴：InnoDB + utf8mb4。
# 字符集**必须显式写**：库级默认虽然也是 utf8mb4，但显式写下来，
# 以后谁改了库默认也不会把某张表悄悄变成 latin1（中文会变问号）。
_TAIL = """
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
"""

# ---------------------------------------------------------------------------
# 退回登记库
# ---------------------------------------------------------------------------

CREATE_RETURNS = """
CREATE TABLE IF NOT EXISTS returns (
    id                INT NOT NULL AUTO_INCREMENT,

    -- 明细标识
    detail_key        VARCHAR(64) UNIQUE,   -- 明细唯一键  260918001-001
    order_no          VARCHAR(64),          -- 售后单号    260918001
    line_no           INT,                  -- 行号

    -- 整单共享
    return_no         VARCHAR(64),          -- 退回单号（快递单）
    carrier           VARCHAR(64),          -- 快递公司
    return_date       VARCHAR(32),          -- 退回时间    YYYY-MM-DD
    turbine_vendor    VARCHAR(128),         -- 风机厂家
    project_site      VARCHAR(255),         -- 项目风场
    info_source       VARCHAR(64),          -- 快递归属

    -- 产品明细
    product_code      VARCHAR(64),          -- 产品编号
    material_no       VARCHAR(64),          -- 料号
    product_model     VARCHAR(128),         -- 产品型号
    product_category  VARCHAR(64),          -- 产品类别
    product_name      VARCHAR(255),         -- 品名
    spec              VARCHAR(255),         -- 规格
    production_stat   VARCHAR(64),          -- 生产统计
    production_year   VARCHAR(16),          -- 生产年份
    production_month  VARCHAR(16),          -- 生产月份
    return_qty        DOUBLE NOT NULL DEFAULT 1,   -- 退回数量
    feedback_issue    TEXT,                 -- 反馈现象
    analysis_report   VARCHAR(32),          -- 分析报告（是/否）

    -- 登记信息
    registrar         VARCHAR(64),          -- 登记人
    remark            TEXT,                 -- 备注
    registered_at     VARCHAR(32),          -- 登记时间
    match_path        TEXT,                 -- 匹配路径
    match_status      VARCHAR(128),         -- 匹配状态

    -- 程序自用
    source            VARCHAR(32) NOT NULL DEFAULT 'local',  -- local / kdocs
    created_at        VARCHAR(32),
    updated_at        VARCHAR(32),

    PRIMARY KEY (id)
""" + _TAIL

CREATE_MODEL_DICT = """
CREATE TABLE IF NOT EXISTS model_dict (
    id                INT NOT NULL AUTO_INCREMENT,
    product_model     VARCHAR(128) UNIQUE,  -- 产品型号（唯一）
    product_code      VARCHAR(64),          -- 典型产品编号
    material_no       VARCHAR(64),          -- 料号
    product_name      VARCHAR(255),         -- 品名
    spec              VARCHAR(255),         -- 规格
    production_stat   VARCHAR(64),          -- 生产统计
    product_category  VARCHAR(64),          -- 产品类别（一级）
    category_l1       VARCHAR(64),          -- 三级分类 - 一级
    category_l2       VARCHAR(64),          -- 三级分类 - 二级
    category_l3       VARCHAR(64),          -- 三级分类 - 三级
    updated_at        VARCHAR(32),

    PRIMARY KEY (id)
""" + _TAIL

# 字典选项与操作日志在两个业务库里各存一份（各自汇总自己字段的候选）
CREATE_DICT_OPTION = """
CREATE TABLE IF NOT EXISTS dict_option (
    id                INT NOT NULL AUTO_INCREMENT,
    field             VARCHAR(64) NOT NULL,
    value             VARCHAR(255) NOT NULL,
    use_count         INT DEFAULT 0,
    sort_order        INT DEFAULT 0,
    updated_at        VARCHAR(32),
    PRIMARY KEY (id),
    UNIQUE KEY uq_dict_option (field, value)
""" + _TAIL

CREATE_OP_LOG = """
CREATE TABLE IF NOT EXISTS op_log (
    id                INT NOT NULL AUTO_INCREMENT,
    action            VARCHAR(64),
    detail_key        VARCHAR(255),
    payload           TEXT,
    operator          VARCHAR(64),
    created_at        VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

# ---------------------------------------------------------------------------
# 检测登记库
# ---------------------------------------------------------------------------

CREATE_INSPECT_RECORDS = """
CREATE TABLE IF NOT EXISTS inspect_db.inspect_records (
    detail_key        VARCHAR(64) NOT NULL, -- 关联退回登记库的明细唯一键

    test_date         VARCHAR(32),          -- 检测时间
    test_result       VARCHAR(64),          -- 检测结果
    fault_cause       TEXT,                 -- 故障原因
    improvement       TEXT,                 -- 改善措施
    solution          TEXT,                 -- 处理方案

    issue_category    VARCHAR(64),          -- 问题分类
    responsibility    VARCHAR(64),          -- 责任归属
    completion        VARCHAR(64),          -- 完结状况
    report_no         VARCHAR(128),         -- 报告编号
    photo_evidence    TEXT,                 -- 照片证据

    created_at        VARCHAR(32),
    updated_at        VARCHAR(32),

    PRIMARY KEY (detail_key)
""" + _TAIL

CREATE_INSPECT_DICT_OPTION = """
CREATE TABLE IF NOT EXISTS inspect_db.dict_option (
    id                INT NOT NULL AUTO_INCREMENT,
    field             VARCHAR(64) NOT NULL,
    value             VARCHAR(255) NOT NULL,
    use_count         INT DEFAULT 0,
    sort_order        INT DEFAULT 0,
    updated_at        VARCHAR(32),
    PRIMARY KEY (id),
    UNIQUE KEY uq_dict_option (field, value)
""" + _TAIL

CREATE_INSPECT_OP_LOG = """
CREATE TABLE IF NOT EXISTS inspect_db.op_log (
    id                INT NOT NULL AUTO_INCREMENT,
    action            VARCHAR(64),
    detail_key        VARCHAR(255),
    payload           TEXT,
    operator          VARCHAR(64),
    created_at        VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

# ---------------------------------------------------------------------------
# 处理登记库（2026-09-20 从检测登记拆出：ERP 处理等事后跟进事项）
# ---------------------------------------------------------------------------

CREATE_HANDLE_RECORDS = """
CREATE TABLE IF NOT EXISTS handle_db.handle_records (
    detail_key        VARCHAR(64) NOT NULL, -- 关联退回登记库的明细唯一键

    erp_handled       VARCHAR(64),          -- ERP处理（已处理 / 待处理）
    handle_solution   TEXT,                 -- 后续处理方案

    created_at        VARCHAR(32),
    updated_at        VARCHAR(32),

    PRIMARY KEY (detail_key)
""" + _TAIL

CREATE_HANDLE_DICT_OPTION = """
CREATE TABLE IF NOT EXISTS handle_db.dict_option (
    id                INT NOT NULL AUTO_INCREMENT,
    field             VARCHAR(64) NOT NULL,
    value             VARCHAR(255) NOT NULL,
    use_count         INT DEFAULT 0,
    sort_order        INT DEFAULT 0,
    updated_at        VARCHAR(32),
    PRIMARY KEY (id),
    UNIQUE KEY uq_dict_option (field, value)
""" + _TAIL

CREATE_HANDLE_OP_LOG = """
CREATE TABLE IF NOT EXISTS handle_db.op_log (
    id                INT NOT NULL AUTO_INCREMENT,
    action            VARCHAR(64),
    detail_key        VARCHAR(255),
    payload           TEXT,
    operator          VARCHAR(64),
    created_at        VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

# ---------------------------------------------------------------------------
# 接入库（2026-09-20 新增：登录验证 + 权限组 + 对外开放接口）
#
# 单独成库的理由与业务库一致：这是「谁能进来、能干什么」的横切关注点，
# 与退回/检测/处理三个业务库没有业务耦合，将来接入 SSO 或换鉴权方案时
# 只动这一个库。它也是唯一需要长期保留审计痕迹的库（登录记录不可再生）。
# ---------------------------------------------------------------------------

CREATE_AUTH_GROUP = """
CREATE TABLE IF NOT EXISTS auth_db.permission_group (
    id                INT NOT NULL AUTO_INCREMENT,
    name              VARCHAR(64) NOT NULL UNIQUE,   -- 权限组名（界面上显示）
    description       VARCHAR(255),         -- 说明：这个组是给谁用的
    perms             TEXT,                 -- JSON 数组，权限点 key 列表
    builtin           INT DEFAULT 0,        -- 内置组（管理员/登记员/只读）不可删
    created_at        VARCHAR(32),
    updated_at        VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

CREATE_AUTH_USERS = """
CREATE TABLE IF NOT EXISTS auth_db.users (
    id                INT NOT NULL AUTO_INCREMENT,
    username          VARCHAR(64) NOT NULL UNIQUE,
    display_name      VARCHAR(64),
    password_hash     VARCHAR(255) NOT NULL, -- PBKDF2-HMAC-SHA256，格式见 core/auth.py
    group_id          INT,                   -- 关联 permission_group.id
    enabled           INT DEFAULT 1,         -- 0 = 停用（保留账号但禁止登录）
    must_change_pwd   INT DEFAULT 0,         -- 1 = 下次登录必须改密
    last_login_at     VARCHAR(32),
    created_at        VARCHAR(32),
    updated_at        VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

CREATE_AUTH_SESSION = """
CREATE TABLE IF NOT EXISTS auth_db.session (
    sid               VARCHAR(64) NOT NULL,  -- 随机会话 ID（Cookie 里传的就是它）
    user_id           INT NOT NULL,
    username          VARCHAR(64),
    ip                VARCHAR(64),
    user_agent        VARCHAR(255),
    created_at        VARCHAR(32),
    expires_at        VARCHAR(32),
    revoked           INT DEFAULT 0,
    PRIMARY KEY (sid)
""" + _TAIL

# ⚠️ `key` 是 MySQL 保留字 —— 表和查询里都必须写成 `` `key` ``（反引号）。
# 从 SQLite 迁过来最容易漏的一处，漏了在建表时就报 1064，还算好查；
# 真正难查的是查询里漏写反引号时才报错，所以这里留个记号。
CREATE_AUTH_SETTING = """
CREATE TABLE IF NOT EXISTS auth_db.setting (
    `key`             VARCHAR(64) NOT NULL, -- auth_enabled / open_api_token / ...
    value             TEXT,
    updated_at        VARCHAR(32),
    PRIMARY KEY (`key`)
""" + _TAIL

CREATE_AUTH_OP_LOG = """
CREATE TABLE IF NOT EXISTS auth_db.op_log (
    id                INT NOT NULL AUTO_INCREMENT,
    action            VARCHAR(64),           -- login / logout / user.create / ...
    detail_key        VARCHAR(255),           -- 复用现有列名：这里存目标对象
    payload           TEXT,
    operator          VARCHAR(64),
    created_at        VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

# ⚠️ `rows` 同样是保留字，查询里要写 `` `rows` ``。
CREATE_AUTH_OPEN_API_LOG = """
CREATE TABLE IF NOT EXISTS auth_db.open_api_log (
    id                INT NOT NULL AUTO_INCREMENT,
    created_at        VARCHAR(32),
    ip                VARCHAR(64),
    dataset           VARCHAR(64),           -- 拉取的数据集
    endpoint          VARCHAR(128),
    `rows`            INT DEFAULT 0,         -- 返回条数
    ok                INT DEFAULT 1,
    message           TEXT,
    PRIMARY KEY (id)
""" + _TAIL

# ---------------------------------------------------------------------------
# 匹配数据库
# ---------------------------------------------------------------------------

CREATE_ITEM_MASTER = """
CREATE TABLE IF NOT EXISTS items_db.item_master (
    id                INT NOT NULL AUTO_INCREMENT,
    material_no       VARCHAR(64) UNIQUE,   -- 料号（唯一键）
    old_material_no   VARCHAR(64),          -- 旧料号
    product_name      VARCHAR(255),         -- 品名
    model_no          VARCHAR(128),         -- 型号（系列号，如 BLF1-S）
    spec              VARCHAR(255),         -- 规格（如 51177.67.773C）
    description       TEXT,                 -- 描述
    customer          VARCHAR(255),         -- 客户
    production_stat   VARCHAR(64),          -- 生产统计
    param1            VARCHAR(128),         -- 产品量程
    param2            VARCHAR(128),         -- 信号输出
    param3            VARCHAR(128),         -- 防护等级
    category          VARCHAR(64),          -- 产品类别
    source            VARCHAR(32) DEFAULT 'import',
    created_at        VARCHAR(32),
    updated_at        VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

# ---------------------------------------------------------------------------
# 发货申请库（2026-09-21 新增）
#
# 本项目**第一对真正的父子表** —— 其余模块要么是「单表 + 行号」（returns）、
# 要么是「明细的 1:1 扩展」（inspect_records / handle_records，主键 detail_key）。
# 这里主表存整单信息、明细表存发货内容（一行一种产品），关联键是对外单号
# request_no，行内顺序由 line_no 决定。
#
# 字段定义与设计理由见《发货申请单-字段定义.md》；
# 特别注意：明细的 material_no / product_model / product_name / spec 四列
# 都是「产品搜索框选定候选后带出」的结果，搜索框本身不落库。
# ---------------------------------------------------------------------------

CREATE_DELIVERY_REQUEST = """
CREATE TABLE IF NOT EXISTS delivery_db.delivery_request (
    id                INT NOT NULL AUTO_INCREMENT,

    -- 主表填写字段（8 项）
    turbine_vendor    VARCHAR(128) NOT NULL,  -- 风机厂家（与退回登记同名同义，台账匹配维度）
    project_site      VARCHAR(255) NOT NULL,  -- 项目名称（同上，即退回登记的「项目风场」）
    expect_ship_date  VARCHAR(32) NOT NULL,   -- 期望发货日 YYYY-MM-DD（待发货清单排序依据）
    ship_address      TEXT NOT NULL,          -- 收件地址（可由「地址+姓名+电话」整段自动拆分）
    ship_contact      VARCHAR(64) NOT NULL,   -- 联系人
    ship_phone        VARCHAR(64) NOT NULL,   -- 电话
    replace_reason    TEXT NOT NULL,          -- 调换原因（整单一个）
    express_req       TEXT NOT NULL,          -- 快递要求

    -- 系统字段
    request_no        VARCHAR(64) UNIQUE,     -- FH + YYYYMMDD + 3 位流水
    applicant         VARCHAR(64),            -- 申请人（登录用户）
    apply_date        VARCHAR(32),            -- 申请日期 YYYY-MM-DD
    status            VARCHAR(32) NOT NULL DEFAULT 'draft',  -- draft/submitted/shipped/closed

    created_at        VARCHAR(32),
    updated_at        VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

CREATE_DELIVERY_ITEM = """
CREATE TABLE IF NOT EXISTS delivery_db.delivery_request_item (
    id                INT NOT NULL AUTO_INCREMENT,
    request_no        VARCHAR(64) NOT NULL, -- 关联主表的对外单号
    line_no           INT NOT NULL,         -- 1、2、3…

    -- 以下四列由「产品搜索」选定候选后一次性带出（搜索框本身不落库）
    material_no       VARCHAR(64),          -- 料号（物料库唯一键，台账核销主依据）
    product_model     VARCHAR(128) NOT NULL,  -- 型号（由选定结果得出）
    product_name      VARCHAR(255),         -- 品名
    spec              VARCHAR(255),         -- 规格

    qty               INT NOT NULL DEFAULT 1,  -- 数量（台账按数量核销，不是按行数）
    need_return       INT NOT NULL DEFAULT 1,  -- 1=需返回，0=不需返回（逐行判断）

    created_at        VARCHAR(32),
    updated_at        VARCHAR(32),
    PRIMARY KEY (id),
    UNIQUE KEY uq_ditem_line (request_no, line_no)
""" + _TAIL

# 发货记录：待发货清单「出队」的落点，也是将来 ERP 发货清单的落地表。
#
# ★ 表名里的 shipment = 大纲 5.5 里预留的那张 `shipment`（发货跟踪 ③ 的数据源）。
#   现在只有手工登记（source='manual'）；接 ERP 后由同步任务写 source='erp'，
#   待发货清单与发货跟踪**读同一张表**，界面与业务都不用改。
# ★ 刻意**没有** qty 之外的核销字段 —— 核销关系在 ④ 台账的 ledger_clear 里，
#   发货记录只负责如实记「什么时候发了什么」，不预判能不能核销。
# ★ 也刻意**不加** (source, ship_no) 唯一约束：一张发货单可能同时发多张申请单的货，
#   加了会误伤。幂等留给 ③ 的实现按 (ship_no, 料号) 处理。
CREATE_DELIVERY_SHIPMENT = """
CREATE TABLE IF NOT EXISTS delivery_db.delivery_shipment (
    id                INT NOT NULL AUTO_INCREMENT,
    -- 核销维度 1、2：**冗余存在发货记录上**，不靠 JOIN 申请单取。
    -- 理由：ERP 同步来的发货记录可能根本没有申请单号（跨系统对不上），
    -- 但那些货同样要参与核销 —— 靠 JOIN 会把它们整批漏掉。
    turbine_vendor    VARCHAR(128),         -- 整机厂家（台账维度 1）
    project_site      VARCHAR(255),         -- 项目风场（台账维度 2）
    request_no        VARCHAR(64),          -- 关联申请单（未关联时可空，等人工挂接）
    ship_no           VARCHAR(64),          -- 发货单号（手工登记可空；ERP 同步必有）
    ship_date         VARCHAR(32),          -- 发货日期 YYYY-MM-DD
    express_no        VARCHAR(255),         -- 物流单号
    carrier           VARCHAR(64),          -- 承运商（手工填 / ERP 带出）
    qty               INT,                  -- 发货件数

    -- 下面四列把粒度降到「型号行」——**核销要的就是这一层**：
    -- 整单出队时按申请明细逐行写，ERP 同步本来就带料号，两边粒度因此对齐。
    line_no           INT,                  -- 对应申请明细的行号（手工登记时为空）
    material_no       VARCHAR(64),          -- 料号（发货侧记录；**不参与核销匹配**，见 config 说明）
    product_model     VARCHAR(128),         -- 产品型号（**核销维度**）
    need_return       INT,                  -- 1=该行需回收旧件（台账只核销这部分）
    source            VARCHAR(32) NOT NULL DEFAULT 'manual',  -- manual / erp（预留）
    remark            TEXT,                 -- 备注（补发 / 分批 / 差异说明）
    sync_at           VARCHAR(32),          -- 同步时间（ERP 同步时填，手工为空）
    operator          VARCHAR(64),          -- 操作人
    created_at        VARCHAR(32),
    updated_at        VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

# 发货明细：ERP(U9) 出货明细的**本地只读镜像**。
#
# ★ 内容完全来自 ERP，本地不手工增改。每次同步按**范围整体替换**
#   （先删该日期区间、再整体插入），不做逐行 upsert。
#
#   理由：ERP 出货明细**没有天然唯一键** —— 实测 8.9 万行里，
#   「单号+料号+序列号+规格+数量+型号」六列组合仍有 2296 组重复
#   （ERP 允许同一张单、同一物料、同行内容拆成多条记录）。
#   硬造一个唯一键只会把真实存在的行吞掉；按范围替换则天然幂等 ——
#   同一区间重拉多少次，结果都一致。
#
# ★ 同步顺序是**先把 ERP 数据全部取回内存、再在一个事务里删+插**。
#   取数失败时一行都不动 —— 不能出现「删完了却没插进去」的半截状态。
#
# ★ qty 用 DOUBLE 而不是 INT：ERP 的 ShipQtyInvAmount 是 decimal，
#   整数化会把小数件数悄悄抹平。
#
# ★ serial_no 实测最长 909 字符（其余各列最长：express_no 159 / spec 64 /
#   product_model 61 / customer 47 / material_name 43 / contact 31 / address 136）。
#   给它 VARCHAR(1000) 并**用前缀索引** —— utf8mb4 下 1000 字符 = 4000 字节，
#   超过 InnoDB 3072 字节的索引上限，整列建索引会直接报 1071。
CREATE_SHIP_DETAIL = """
CREATE TABLE IF NOT EXISTS delivery_db.ship_detail (
    id            INT NOT NULL AUTO_INCREMENT,
    doc_date      VARCHAR(32),  -- 日期（ERP BusinessDate，YYYY-MM-DD）
    doc_status    VARCHAR(32),  -- 状态（草稿/开立/核准中/已核准，**本地翻译**，见 config）
    doc_no        VARCHAR(64),  -- 单号（SM_Ship.DocNo）
    doc_type      VARCHAR(64),  -- 单据类型（销售发货单 / 售后发货单 / 期初…）
    material_no   VARCHAR(64),  -- 料号
    material_name VARCHAR(255), -- 料品名称
    spec          VARCHAR(255), -- 规格
    product_model VARCHAR(128), -- 型号
    qty           DOUBLE,       -- 出货数量
    serial_no     VARCHAR(1000),-- 序列号（实测仅约 20% 有值）
    customer      VARCHAR(255), -- 客户名称（实测就是整机厂家：金风 / 明阳 / 远景…）
    contact       VARCHAR(64),  -- 联系人
    express_no    VARCHAR(255), -- 承运单号（物流单号）
    address       TEXT,         -- 地址
    sync_at       VARCHAR(32),  -- 本行是哪次同步拉进来的（= 本次同步开始时间）
    created_at    VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

# 核销台账的核销记录（2026-09-22 改版：**一个表存两种核销**）。
#
# ★ kind = 'link'：**手动核销关联** —— 把某一行返件（returns_db.returns）关联到台账
#   维度上，表示「这批返件就是那批发货退回来的」。数量在返件行粒度上（一行 1 件）。
# ★ kind = 'clear'：**手工清账** —— 人的判断（换件不返还 / 现场留用 / 遗失），
#   算不出来，必须记下来并且**必填原因**。
#   为什么「自动核销」不落表：剩余未返回 = 发货 − 返件 − 手工核销，这个差值实时算得出来；
#   而「哪一件返件核销了哪一件发货」在数量级粒度下本就无法唯一确定（一行发货 N 件、
#   一行返件 1 件），存下来的"关系"没有信息量，只会随数据变动过期。
# ★ 维度：整机厂家 + 项目风场（2026-09-22 用户口径：不再带产品型号）。
#   return_id / return_no / material_no / doc_no 是**定位信息**：核销关联针对的是
#   具体某一行返件，撤销/统计都要能回到那一行；老行（改版前）这些列为空，
#   按厂家+风场回退匹配（见 core/repo_delivery.py 的 _clear_group_key）。
CREATE_LEDGER_CLEAR = """
CREATE TABLE IF NOT EXISTS delivery_db.ledger_clear (
    id                INT NOT NULL AUTO_INCREMENT,
    kind              VARCHAR(8) NOT NULL DEFAULT 'clear',  -- link=手动核销关联 / clear=手工清账
    turbine_vendor    VARCHAR(128) NOT NULL, -- 维度 1：整机厂家
    project_site      VARCHAR(255) NOT NULL DEFAULT '',  -- 维度 2：项目风场（空值统一存 ''）
    product_model     VARCHAR(128) NOT NULL DEFAULT '',  -- 冗余：留作明细展示，不再是维度
    qty               INT NOT NULL,          -- 核销件数
    reason            TEXT NOT NULL,         -- 原因（**必填**：不算出来才叫手工核销）
    return_id         INT,                   -- 核销关联的返件行 id（returns_db.returns.id）
    return_no         VARCHAR(64),           -- 返件单号（冗余，便于列表直接显示）
    material_no       VARCHAR(64),           -- 返件料号（冗余）
    serial_no         VARCHAR(64) NOT NULL DEFAULT '', -- 发货行序列号（按序列号清账时记录）
    doc_no            VARCHAR(64),           -- 关联的发货单号（冗余，可选）
    request_no        VARCHAR(64),           -- 可选：针对某张申请单核销
    operator          VARCHAR(64),
    created_at        VARCHAR(32),
    updated_at        VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

# 回收站：删除先入这里，7 天内可还原（见 core/recycle.py）。
# ★ 为什么存**整行快照 JSON** 而不是「只记一个 detail_key」：
#   还原要把删除那一刻的现场原样写回 —— 连主键一起。明细的 `returns.id`
#   被 `delivery_db.ledger_clear.return_id` 引用着，重新自增一个新 id
#   会让所有核销关联指向空。快照里的行就是「那一刻的行」。
# ★ 一次删除 = 一条记录：批量删 100 条明细也只留一条（row_count=100），
#   它对应「用户点了一次删除」，还原也一次还原回去。
CREATE_RECYCLE = """
CREATE TABLE IF NOT EXISTS returns_db.recycle_bin (
    id            INT NOT NULL AUTO_INCREMENT,
    kind          VARCHAR(24) NOT NULL,        -- return=返件明细(含检测/处理) / clear=核销记录 / item=匹配库物料
    ref_key       VARCHAR(128) NOT NULL DEFAULT '',  -- 业务主键（批量时是逗号拼接，预览用）
    ref_label     VARCHAR(255) NOT NULL DEFAULT '',  -- 列表直接显示的标签（售后单号 / 料号）
    summary       VARCHAR(255) NOT NULL DEFAULT '',  -- 一句话说明删了什么
    payload       LONGTEXT NOT NULL,           -- 整行快照 JSON：{"tables":[{schema,table,rows}],"keys":[],"photos":[]}
    row_count     INT NOT NULL DEFAULT 0,      -- 快照里的行数（列表显示「N 行」）
    operator      VARCHAR(64),                 -- 删除人（会话用户，不是客户端自报）
    created_at    VARCHAR(32),                 -- 删除时间
    expires_at    VARCHAR(32),                 -- 到期时间 = created_at + RECYCLE_DAYS
    restored      TINYINT NOT NULL DEFAULT 0,  -- 是否已还原
    restored_at   VARCHAR(32) NOT NULL DEFAULT '',
    restored_by   VARCHAR(64) NOT NULL DEFAULT '',
    PRIMARY KEY (id)
""" + _TAIL

CREATE_DELIVERY_OP_LOG = """
CREATE TABLE IF NOT EXISTS delivery_db.op_log (
    id                INT NOT NULL AUTO_INCREMENT,
    action            VARCHAR(64),
    request_no        VARCHAR(64),
    payload           TEXT,
    operator          VARCHAR(64),
    created_at        VARCHAR(32),
    PRIMARY KEY (id)
""" + _TAIL

# 按库分组的建表语句
RETURNS_DDL = (CREATE_RETURNS, CREATE_MODEL_DICT, CREATE_DICT_OPTION,
               CREATE_OP_LOG, CREATE_RECYCLE)
INSPECT_DDL = (CREATE_INSPECT_RECORDS, CREATE_INSPECT_DICT_OPTION,
               CREATE_INSPECT_OP_LOG)
HANDLE_DDL = (CREATE_HANDLE_RECORDS, CREATE_HANDLE_DICT_OPTION,
              CREATE_HANDLE_OP_LOG)
DELIVERY_DDL = (CREATE_DELIVERY_REQUEST, CREATE_DELIVERY_ITEM,
                CREATE_DELIVERY_SHIPMENT, CREATE_LEDGER_CLEAR,
                CREATE_DELIVERY_OP_LOG, CREATE_SHIP_DETAIL)
AUTH_DDL = (CREATE_AUTH_GROUP, CREATE_AUTH_USERS, CREATE_AUTH_SESSION,
            CREATE_AUTH_SETTING, CREATE_AUTH_OP_LOG, CREATE_AUTH_OPEN_API_LOG)
ITEMS_DDL = (CREATE_ITEM_MASTER,)

ALL_DDL = (RETURNS_DDL + INSPECT_DDL + HANDLE_DDL + DELIVERY_DDL
           + AUTH_DDL + ITEMS_DDL)

# 索引：(schema, 表, 索引名, 列定义)
#
# 为什么不是一串裸 SQL：MySQL **没有** `CREATE INDEX IF NOT EXISTS`
# （SQLite 有）。所以索引要拆成数据，先查 information_schema.statistics
# 里已有哪些，再补建缺的 —— 这样 init_db() 依然幂等。
#
# 列定义里的 `(191)` 是前缀索引：utf8mb4 一字符最多 4 字节，
# 191×4 = 764 字节，稳稳落在 InnoDB 的 3072 字节上限内。
INDEXES = [
    # 退回登记库
    ("returns_db", "returns", "idx_returns_order_no", "order_no"),
    ("returns_db", "returns", "idx_returns_return_no", "return_no"),
    ("returns_db", "returns", "idx_returns_product_code", "product_code"),
    ("returns_db", "returns", "idx_returns_material_no", "material_no"),
    ("returns_db", "returns", "idx_returns_product_model", "product_model"),
    ("returns_db", "returns", "idx_returns_return_date", "return_date"),
    ("returns_db", "returns", "idx_returns_registered_at", "registered_at"),
    ("returns_db", "returns", "idx_returns_category", "product_category"),
    ("returns_db", "returns", "idx_returns_vendor", "turbine_vendor"),

    ("returns_db", "dict_option", "idx_dict_field", "field"),
    ("returns_db", "model_dict", "idx_model_dict_code", "product_code"),
    # 回收站：列表按类别筛、按删除时间倒序；定时清理按到期时间扫
    ("returns_db", "recycle_bin", "idx_recycle_kind", "kind"),
    ("returns_db", "recycle_bin", "idx_recycle_expires", "expires_at"),
    # 检测登记库（detail_key 已是主键，天然有索引）
    ("inspect_db", "inspect_records", "idx_inspect_test_date", "test_date"),
    ("inspect_db", "inspect_records", "idx_inspect_completion", "completion"),
    ("inspect_db", "dict_option", "idx_inspect_dict_field", "field"),
    # 处理登记库
    ("handle_db", "handle_records", "idx_handle_erp", "erp_handled"),
    # 前缀索引：handle_solution 是 TEXT（自由文本的处理方案），
    # MySQL 不允许对 TEXT/BLOB 整列建索引，会报 1170。
    ("handle_db", "handle_records", "idx_handle_solution", "handle_solution(191)"),
    ("handle_db", "dict_option", "idx_handle_dict_field", "field"),
    # 接入库
    ("auth_db", "users", "idx_users_group", "group_id"),
    ("auth_db", "session", "idx_session_user", "user_id"),
    ("auth_db", "session", "idx_session_expires", "expires_at"),
    ("auth_db", "open_api_log", "idx_openlog_time", "created_at"),
    # 发货申请库
    ("delivery_db", "delivery_request", "idx_dreq_status", "status"),
    ("delivery_db", "delivery_request", "idx_dreq_expect", "expect_ship_date"),
    ("delivery_db", "delivery_request", "idx_dreq_apply", "apply_date"),
    # 台账核销要按「整机厂家 + 项目风场」匹配，这两列值得建联合索引
    ("delivery_db", "delivery_request", "idx_dreq_match",
     "turbine_vendor, project_site"),
    ("delivery_db", "delivery_request_item", "idx_ditem_req", "request_no"),
    ("delivery_db", "delivery_request_item", "idx_ditem_material", "material_no"),
    # 台账按「厂家 + 风场 + 料号」核销时，这个联合索引能少一次回表
    ("delivery_db", "delivery_request_item", "idx_ditem_match",
     "need_return, material_no"),
    ("delivery_db", "op_log", "idx_doplog_req", "request_no"),
    # 发货记录：清单出队后要能按申请单回查；台账按日期范围取发货量；
    # ship_no 建索引是为 ③ 接 ERP 后按发货单号做幂等查找。
    ("delivery_db", "delivery_shipment", "idx_dship_req", "request_no"),
    ("delivery_db", "delivery_shipment", "idx_dship_date", "ship_date"),
    ("delivery_db", "delivery_shipment", "idx_dship_no", "ship_no"),
    # 发货跟踪按「厂家 + 风场 + 型号」逐行核对，这三个列建联合索引；
    # 再加一个按型号单列的（按型号查「这个产品总共发了多少」也要快）。
    # ⚠️ 核销台账（2026-09-22 起）只到「厂家 + 风场」，读的是发货明细 ship_detail，
    #    不再读这张登记表 —— 这里的索引是**发货跟踪**在用。
    ("delivery_db", "delivery_shipment", "idx_dship_model",
     "turbine_vendor, project_site, product_model"),
    # 核销记录按维度回查（老行没有 return_id 时按厂家+风场回退找维度）
    ("delivery_db", "ledger_clear", "idx_dclear_dim",
     "turbine_vendor, project_site, product_model"),
    # 核销记录要能按返件行回查（撤销关联 / 统计某一行返件核销了多少）
    ("delivery_db", "ledger_clear", "idx_dclear_return", "return_id"),
    ("delivery_db", "ledger_clear", "idx_dclear_time", "created_at"),
    # 发货明细（ERP 出货明细镜像）：按日期区间整体替换 + 按日期倒序翻页是主用法，
    # 再给几个能独立定位的高基数列（单号 / 料号 / 序列号 / 承运单号 / 客户）建单列索引。
    ("delivery_db", "ship_detail", "idx_sdetail_date", "doc_date"),
    ("delivery_db", "ship_detail", "idx_sdetail_status", "doc_status, doc_date"),
    ("delivery_db", "ship_detail", "idx_sdetail_no", "doc_no"),
    ("delivery_db", "ship_detail", "idx_sdetail_material", "material_no"),
    # 前缀索引：serial_no 最长 909 字符，整列索引会超 3072 字节上限
    ("delivery_db", "ship_detail", "idx_sdetail_serial", "serial_no(191)"),
    ("delivery_db", "ship_detail", "idx_sdetail_express", "express_no"),
    ("delivery_db", "ship_detail", "idx_sdetail_customer", "customer"),
    # 匹配数据库
    ("items_db", "item_master", "idx_item_material_no", "material_no"),
    ("items_db", "item_master", "idx_item_old_no", "old_material_no"),
    ("items_db", "item_master", "idx_item_model", "model_no"),
    ("items_db", "item_master", "idx_item_spec", "spec"),
    ("items_db", "item_master", "idx_item_name", "product_name"),
]

# 供迁移脚本与自检使用：schema → 该库里应有的表
SCHEMA_TABLES = {
    "returns_db": ("returns", "model_dict", "dict_option", "op_log"),
    "inspect_db": ("inspect_records", "dict_option", "op_log"),
    "handle_db": ("handle_records", "dict_option", "op_log"),
    "items_db": ("item_master",),
    "delivery_db": ("delivery_request", "delivery_request_item",
                    "delivery_shipment", "ledger_clear", "op_log",
                    "ship_detail"),
    "auth_db": ("users", "permission_group", "session", "setting", "op_log",
                "open_api_log"),
}

# 各列在 MySQL 里的实际类型，供迁移脚本做「长度体检」：
# 从 information_schema 读回来比对，任何一列超长都会在灌数据前先报出来，
# 而不是等到 STRICT_TRANS_TABLES 在 8.9 万行中的第 7 万行上抛 1406。
COLUMNS_MYSQL = None          # 运行时由 columns_of() 填，保留名字给自检引用


# ---------------------------------------------------------------------------
# 元数据查询：MySQL 没有 PRAGMA，这些都要改问 information_schema
# ---------------------------------------------------------------------------

def tables_of(conn, schema: str) -> set:
    """这个 schema 里有哪些表（不含视图）。"""
    return {r["TABLE_NAME"] for r in conn.execute(
        "SELECT TABLE_NAME FROM information_schema.tables "
        "WHERE table_schema = ? AND table_type = 'BASE TABLE';", (schema,))}


def columns_of(conn, schema: str, table: str) -> set:
    """这张表有哪些列。"""
    return {r["COLUMN_NAME"] for r in conn.execute(
        "SELECT COLUMN_NAME FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ?;", (schema, table))}


def index_names_of(conn, schema: str) -> set:
    """这个 schema 里已存在的索引名。"""
    return {r["INDEX_NAME"] for r in conn.execute(
        "SELECT DISTINCT INDEX_NAME FROM information_schema.statistics "
        "WHERE table_schema = ?;", (schema,))}


def column_lengths(conn, schema: str) -> dict:
    """(表, 列) → 字符上限。TEXT 系列没有上限，返回 None。

    迁移前拿它跟 SQLite 源表的实际最大长度比一遍 —— 见 COLUMNS_MYSQL 的注释。
    """
    out = {}
    for r in conn.execute(
            "SELECT table_name t, column_name c, data_type dt, "
            "character_maximum_length n FROM information_schema.columns "
            "WHERE table_schema = ?;", (schema,)):
        dt = (r["dt"] or "").lower()
        out[(r["t"], r["c"])] = None if dt in (
            "text", "mediumtext", "longtext", "tinytext") else r["n"]
    return out


# ---------------------------------------------------------------------------
# 初始化与轻量迁移
# ---------------------------------------------------------------------------

def init_db() -> None:
    """初始化六个 schema 的结构（幂等，可在每次启动时调用）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    conn = get_conn()
    for ddl in ALL_DDL:
        conn.execute(ddl)

    # 补列必须在建索引之前 —— 新增字段索引会引用该列，
    # 顺序反了会报 "Unknown column"（索引是裸 SQL，不参与 CREATE TABLE 的补列）。
    _ensure_columns(conn)

    _create_indexes(conn)
    _drop_stray_tables(conn)
    _drop_legacy_sync(conn)


def _create_indexes(conn) -> list:
    """补建缺失的索引。MySQL 没有 CREATE INDEX IF NOT EXISTS，只能先查后建。"""
    created = []
    for schema in SCHEMAS:
        have = index_names_of(conn, schema)
        for sch, table, name, cols in INDEXES:
            if sch != schema or name in have:
                continue
            conn.execute(f"CREATE INDEX `{name}` ON `{sch}`.`{table}` ({cols});")
            created.append(f"{sch}.{name}")
    return created


def _drop_stray_tables(conn) -> list:
    """清理误建在主库（returns_db）的表。

    跨库建表时若漏写库前缀，表会落到主库而不是目标库。
    这里做一次自愈，避免残留的空表干扰后续查询。

    在 SQLite 时代这个自愈很关键（ATTACH 下漏写前缀不报错，只是查询读到空表）。
    到了 MySQL 其实会直接报 "Unknown database/table"，但保留这段没有坏处 ——
    它同时兜住「手工建表建错地方」这种人祸。
    注意 item_master 不在清理范围内 —— 旧版本的主库里它承载真实数据。
    """
    stray = tables_of(conn, MAIN_SCHEMA)
    dropped = []
    # 接入库的表同样要防「漏写库前缀建到主库」—— 落到主库不会报错，
    # 只是鉴权查询会一直读到空表（表现为「登录失败但账号明明存在」）。
    for name in ("inspect_records", "handle_records", "users",
                 "permission_group", "session", "setting", "open_api_log",
                 # 发货申请库：主表/明细表漏写前缀会落到主库，
                 # 症状是「提交成功但列表查不到」，比报错更难查。
                 "delivery_request", "delivery_request_item",
                 "delivery_shipment", "ledger_clear", "ship_detail"):
        if name in stray:
            conn.execute(f"DROP TABLE IF EXISTS `{MAIN_SCHEMA}`.`{name}`;")
            dropped.append(name)
    return dropped


def _drop_legacy_sync(conn) -> bool:
    """移除金山文档同步模块的遗留表（2026-09-21 该模块已删除）。

    `sync_log` 是旧「推送式同步」的写库日志：同步方向早在 2026-09-20 就改成了
    外部系统来拉，这张表从那时起一直是空的。留着它会让「本系统要往云端推数据」
    的误解继续存在，所以随模块一起清掉。

    同时清掉 `auth_db.setting` 里的 `kdocs_target`（金山侧文件 ID / 工作表名）——
    那三项只服务于同一个模块，本系统不再需要知道数据被拉到哪个表。

    幂等：都不存在时什么都不做，返回 False。
    """
    did = False
    if "sync_log" in tables_of(conn, MAIN_SCHEMA):
        conn.execute(f"DROP TABLE IF EXISTS `{MAIN_SCHEMA}`.`sync_log`;")
        did = True
    # 注意 `key` 是保留字，反引号不能省
    cur = conn.execute(
        "DELETE FROM auth_db.setting WHERE `key` = 'kdocs_target';")
    if cur.rowcount:
        did = True
    return did


def _ensure_columns(conn) -> None:
    """轻量迁移：补齐新增列、移除已废弃列。"""
    existing = columns_of(conn, MAIN_SCHEMA, "returns")
    wanted = {
        "source": "VARCHAR(32) NOT NULL DEFAULT 'local'",
        "created_at": "VARCHAR(32)",
        "updated_at": "VARCHAR(32)",
        "analysis_report": "VARCHAR(32)",
    }
    for col, ddl in wanted.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE returns ADD COLUMN {col} {ddl};")

    # 处理登记库同样要补新列 —— 漏了这段的话新列不会存在，
    # 写入时报 "Unknown column"（新增字段必踩）。
    h_existing = columns_of(conn, "handle_db", "handle_records")
    if "handle_solution" not in h_existing:
        conn.execute("ALTER TABLE handle_db.handle_records "
                     "ADD COLUMN handle_solution TEXT;")

    # 发货记录表同样要补列 —— 2026-09-21 把粒度从「整单」降到「型号行」，
    # 老的库是用旧 DDL 建的，不加这段会报 "Unknown column 'product_model'"。
    d_existing = columns_of(conn, "delivery_db", "delivery_shipment")
    d_wanted = {
        "line_no": "INT",
        "material_no": "VARCHAR(64)",
        "product_model": "VARCHAR(128)",
        "need_return": "INT",
        # 台账维度（冗余存，见 DDL 注释）
        "turbine_vendor": "VARCHAR(128)",
        "project_site": "VARCHAR(255)",
    }
    for col, ddl in d_wanted.items():
        if col not in d_existing:
            conn.execute("ALTER TABLE delivery_db.delivery_shipment "
                         f"ADD COLUMN {col} {ddl};")

    # 核销记录表：2026-09-22 改版后多了一列 kind（link=手动核销关联 /
    # clear=手工清账）与几个定位列（return_id / return_no / material_no / doc_no）。
    # 老库是按旧 DDL 建的 —— 不补这段，第一次核销就报 "Unknown column 'kind'"。
    l_existing = columns_of(conn, "delivery_db", "ledger_clear")
    l_wanted = {
        "kind": "VARCHAR(8) NOT NULL DEFAULT 'clear'",
        "return_id": "INT",
        "return_no": "VARCHAR(64)",
        "material_no": "VARCHAR(64)",
        "doc_no": "VARCHAR(64)",
        "serial_no": "VARCHAR(64) NOT NULL DEFAULT ''",
    }
    for col, ddl in l_wanted.items():
        if col not in l_existing:
            conn.execute("ALTER TABLE delivery_db.ledger_clear "
                         f"ADD COLUMN {col} {ddl};")

    # 已废弃字段（老库启动时自动清理）
    #
    # 2026-09-21：金山文档同步模块整体移除，`sync_state` / `synced_at` 不再有
    # 任何含义（旧的「推送式同步」早在 09-20 就已下线，改为外部系统来拉）。
    # 先摘掉建在 sync_state 上的索引 —— **有索引时 DROP COLUMN 会直接报错**。
    if "idx_returns_sync_state" in index_names_of(conn, MAIN_SCHEMA):
        conn.execute("DROP INDEX `idx_returns_sync_state` ON returns;")
    deprecated = (
        "reply_progress",   # 回复进度
        "order_status",     # 售后单状态
        "wait_return",      # 是否待返回
        "express_fee",      # 快递费用
        "sync_state",       # 同步状态（随金山同步模块移除）
        "synced_at",        # 同步时间（同上）
    )
    for col in deprecated:
        if col in existing:
            try:
                conn.execute(f"ALTER TABLE returns DROP COLUMN {col};")
            except dbapi.OperationalError:
                pass


def db_status() -> dict:
    """返回六库概览，供首页与健康检查使用。"""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM returns;").fetchone()["c"]
    models = conn.execute("SELECT COUNT(*) c FROM model_dict;").fetchone()["c"]
    # ⚠️ 「已检测」的口径是**检测时间非空**，不是「检测库有行」——
    # 检测库是稀疏存储：录了一半（只填了结果、没填检测时间）也会占一行，
    # 用行数算「已检测」会虚高（2026-09-22 自检的一条空检测行就顶出 +1，
    # 被自检那句「接口口径 = 检测时间非空」当场抓到）。
    # 与看板 `stats_inspect_overview` 的 `_TESTED_SQL` 必须同一口径。
    inspected = conn.execute(
        "SELECT COUNT(*) c FROM inspect_db.inspect_records "
        "WHERE TRIM(COALESCE(test_date, '')) <> '';").fetchone()["c"]
    # handled = **已完成 ERP 处理**的条数，不是处理库的行数 ——
    # 处理库是稀疏存储，行数只代表「填过这个字段」，
    # 而补过「待处理」的行也会占一行，用它算「已处理」会全算成已处理。
    handled = conn.execute(
        "SELECT COUNT(*) c FROM handle_db.handle_records "
        "WHERE TRIM(COALESCE(erp_handled, '')) = ?;",
        (HANDLE_DONE_VALUES.get("erp_handled", "已处理"),)).fetchone()["c"]
    handle_rows = conn.execute(
        "SELECT COUNT(*) c FROM handle_db.handle_records;").fetchone()["c"]
    users = conn.execute("SELECT COUNT(*) c FROM auth_db.users;").fetchone()["c"]
    items = conn.execute(
        "SELECT COUNT(*) c FROM items_db.item_master;").fetchone()["c"]
    deliveries = conn.execute(
        "SELECT COUNT(*) c FROM delivery_db.delivery_request;").fetchone()["c"]
    delivery_items = conn.execute(
        "SELECT COUNT(*) c FROM delivery_db.delivery_request_item;").fetchone()["c"]
    deliveries_pending = conn.execute(
        "SELECT COUNT(*) c FROM delivery_db.delivery_request "
        "WHERE status = ?;", (DELIVERY_STATUS_SUBMIT,)).fetchone()["c"]
    shipments = conn.execute(
        "SELECT COUNT(*) c FROM delivery_db.delivery_shipment;").fetchone()["c"]
    cleared = conn.execute(
        "SELECT COUNT(*) c FROM delivery_db.ledger_clear;").fetchone()["c"]
    ship_details = conn.execute(
        "SELECT COUNT(*) c FROM delivery_db.ship_detail;").fetchone()["c"]
    return {
        # 保留 db_path 键以兼容既有调用方（前端首页与 /api/status 都在读它）；
        # 现在给的是 MySQL 连接描述，六库共用。
        "db_path": f"mysql://{MYSQL_USER}@{MYSQL_HOST}:{MYSQL_PORT}",
        "databases": {name: f"mysql://{MYSQL_HOST}:{MYSQL_PORT}/{name}"
                      for name in SCHEMAS},
        "total": total,
        "model_dict": models,
        "inspected": inspected,
        "handled": handled,
        # 处理库的行数（含「待处理」占位行），供排查用
        "handle_rows": handle_rows,
        "users": users,
        "item_master": items,
        "deliveries": deliveries,
        "delivery_items": delivery_items,
        # 待发货清单是派生视图（不落表），但计数很能反映「队列有多长」，一并给出
        "deliveries_pending": deliveries_pending,
        "deliveries_shipped": shipments,
        "ledger_cleared": cleared,
        # 发货明细（ERP 出货明细镜像）行数 —— 8.9 万行级别，
        # 看一眼就知道上次同步到底有没有把数据拉进来
        "ship_details": ship_details,
    }
