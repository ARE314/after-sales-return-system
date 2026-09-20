"""多库数据层 —— 跨库连接（ATTACH）、建表、索引、迁移

库划分（一个模块一个库）
------------------------
    returns.db   退回登记库   returns / model_dict / dict_option / op_log / sync_log
    inspect.db   检测登记库   inspect_records / dict_option / op_log
    handle.db    处理登记库   handle_records / dict_option / op_log
    items.db     匹配数据库   item_master
    auth.db      接入库       users / permission_group / session / setting / open_api_log

五库通过 ATTACH 挂到同一条连接上，跨库 JOIN 的写法和单库完全一致：

    SELECT r.*, i.*, h.*
    FROM returns r
    LEFT JOIN inspect_db.inspect_records i ON i.detail_key = r.detail_key
    LEFT JOIN handle_db.handle_records  h ON h.detail_key = r.detail_key

约定
----
* **关联键统一为 `detail_key`**（明细唯一键 = 售后单号-行号）。
* **检测 / 处理记录稀疏存储**：没检测过的明细在检测库里没有行，
  没处理的在 handle 库里没有行。因此所有跨库查询必须用 LEFT JOIN，
  判断「未检测」用 `i.detail_key IS NULL`；
  对这些字段做条件判断时必须先 COALESCE —— LEFT JOIN 未命中时该侧全为 NULL，
  而 `NULL NOT LIKE '%x%'` 的结果是 NULL（不成立），会漏掉未检测的记录。
* **写入不跨库**：退回登记只写 returns.db，检测登记只写 inspect.db，
  处理登记只写 handle.db。
  SQLite 的 ATTACH 不提供跨库原子事务，保持「一次写入只落一个库」即可规避。
  仅「按明细键更新」与「删除」会同时触及两库，属低频且有兜底，可接受非原子。
"""
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from config import (AUTH_DB, DATA_DIR, EXPORT_DIR, HANDLE_DB,
                    HANDLE_DONE_VALUES, INSPECT_DB, ITEMS_DB, RETURNS_DB)


class InvalidField(ValueError):
    """字段名不在白名单里。

    三个仓储模块的 `_safe*()` 都抛它。放在 db.py 是因为它是三者共同依赖的
    最底层（repo_inspect / repo_handle 都从这里导入，不会循环导入）。

    **必须由路由层统一转成 400** —— 否则这条 ValueError 会一路冒到 ASGI，
    变成 500 并把堆栈写进日志。实测 `GET /api/dict/<任意非字段名>` 就是 500，
    任何人（登录后）都能拿它刷错误日志。
    它继承 ValueError，所以既有的 `except ValueError` 仍然能接住。
    """


_local = threading.local()

# 检测登记库的字段（用于旧库迁移与跨库查询）
# 注意：erp_handled 已于 2026-09-20 迁到处理登记库（见 HANDLE_COLUMNS）
INSPECT_COLUMNS = [
    "test_date", "test_result", "fault_cause", "improvement", "solution",
    "issue_category", "responsibility", "completion", "report_no",
    "photo_evidence",
]

# 处理登记库的字段（一个模块一个库：处理环节独立成库）
#   erp_handled      ERP 处理（已处理 / 待处理，纯下拉锁定项）
#   handle_solution  后续处理方案（可搜索下拉，库内没有则自动新增）
HANDLE_COLUMNS = [
    "erp_handled",
    "handle_solution",
]

# ---------------------------------------------------------------------------
# 退回登记库
# ---------------------------------------------------------------------------

CREATE_RETURNS = """
CREATE TABLE IF NOT EXISTS returns (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 明细标识
    detail_key        TEXT UNIQUE,          -- 明细唯一键  260918001-001
    order_no          TEXT,                 -- 售后单号    260918001
    line_no           INTEGER,              -- 行号

    -- 整单共享
    return_no         TEXT,                 -- 退回单号（快递单）
    carrier           TEXT,                 -- 快递公司
    return_date       TEXT,                 -- 退回时间    YYYY-MM-DD
    turbine_vendor    TEXT,                 -- 风机厂家
    project_site      TEXT,                 -- 项目风场
    info_source       TEXT,                 -- 快递归属

    -- 产品明细
    product_code      TEXT,                 -- 产品编号
    material_no       TEXT,                 -- 料号
    product_model     TEXT,                 -- 产品型号
    product_category  TEXT,                 -- 产品类别
    product_name      TEXT,                 -- 品名
    spec              TEXT,                 -- 规格
    production_stat   TEXT,                 -- 生产统计
    production_year   TEXT,                 -- 生产年份
    production_month  TEXT,                 -- 生产月份
    return_qty        REAL DEFAULT 1,       -- 退回数量
    feedback_issue    TEXT,                 -- 反馈现象
    analysis_report   TEXT,                 -- 分析报告（是/否）

    -- 登记信息
    registrar         TEXT,                 -- 登记人
    remark            TEXT,                 -- 备注
    registered_at     TEXT,                 -- 登记时间
    match_path        TEXT,                 -- 匹配路径
    match_status      TEXT,                 -- 匹配状态

    -- 程序自用
    source            TEXT DEFAULT 'local', -- local / kdocs
    sync_state        TEXT DEFAULT 'pending',
    synced_at         TEXT,
    created_at        TEXT,
    updated_at        TEXT
);
"""

CREATE_MODEL_DICT = """
CREATE TABLE IF NOT EXISTS model_dict (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_model     TEXT UNIQUE,          -- 产品型号（唯一）
    product_code      TEXT,                 -- 典型产品编号
    material_no       TEXT,                 -- 料号
    product_name      TEXT,                 -- 品名
    spec              TEXT,                 -- 规格
    production_stat   TEXT,                 -- 生产统计
    product_category  TEXT,                 -- 产品类别（一级）
    category_l1       TEXT,                 -- 三级分类 - 一级
    category_l2       TEXT,                 -- 三级分类 - 二级
    category_l3       TEXT,                 -- 三级分类 - 三级
    updated_at        TEXT
);
"""

CREATE_SYNC_LOG = """
CREATE TABLE IF NOT EXISTS sync_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    direction         TEXT,                 -- push / pull
    target            TEXT,                 -- 目标表名
    started_at        TEXT,
    finished_at       TEXT,
    total             INTEGER DEFAULT 0,
    success           INTEGER DEFAULT 0,
    failed            INTEGER DEFAULT 0,
    message           TEXT
);
"""

# 字典选项与操作日志在两个业务库里各存一份（各自汇总自己字段的候选）
CREATE_DICT_OPTION = """
CREATE TABLE IF NOT EXISTS dict_option (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    field             TEXT NOT NULL,
    value             TEXT NOT NULL,
    use_count         INTEGER DEFAULT 0,
    sort_order        INTEGER DEFAULT 0,
    updated_at        TEXT,
    UNIQUE(field, value)
);
"""

CREATE_OP_LOG = """
CREATE TABLE IF NOT EXISTS op_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    action            TEXT,
    detail_key        TEXT,
    payload           TEXT,
    operator          TEXT,
    created_at        TEXT
);
"""

# ---------------------------------------------------------------------------
# 检测登记库
# ---------------------------------------------------------------------------

CREATE_INSPECT_RECORDS = """
CREATE TABLE IF NOT EXISTS inspect_db.inspect_records (
    detail_key        TEXT PRIMARY KEY,     -- 关联退回登记库的明细唯一键

    test_date         TEXT,                 -- 检测时间
    test_result       TEXT,                 -- 检测结果
    fault_cause       TEXT,                 -- 故障原因
    improvement       TEXT,                 -- 改善措施
    solution          TEXT,                 -- 处理方案

    issue_category    TEXT,                 -- 问题分类
    responsibility    TEXT,                 -- 责任归属
    completion        TEXT,                 -- 完结状况
    report_no         TEXT,                 -- 报告编号
    photo_evidence    TEXT,                 -- 照片证据

    created_at        TEXT,
    updated_at        TEXT
);
"""

CREATE_INSPECT_DICT_OPTION = """
CREATE TABLE IF NOT EXISTS inspect_db.dict_option (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    field             TEXT NOT NULL,
    value             TEXT NOT NULL,
    use_count         INTEGER DEFAULT 0,
    sort_order        INTEGER DEFAULT 0,
    updated_at        TEXT,
    UNIQUE(field, value)
);
"""

CREATE_INSPECT_OP_LOG = """
CREATE TABLE IF NOT EXISTS inspect_db.op_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    action            TEXT,
    detail_key        TEXT,
    payload           TEXT,
    operator          TEXT,
    created_at        TEXT
);
"""

# ---------------------------------------------------------------------------
# 处理登记库（2026-09-20 从检测登记拆出：ERP 处理等事后跟进事项）
# ---------------------------------------------------------------------------

CREATE_HANDLE_RECORDS = """
CREATE TABLE IF NOT EXISTS handle_db.handle_records (
    detail_key        TEXT PRIMARY KEY,     -- 关联退回登记库的明细唯一键

    erp_handled       TEXT,                 -- ERP处理（已处理 / 待处理）
    handle_solution   TEXT,                 -- 后续处理方案

    created_at        TEXT,
    updated_at        TEXT
);
"""

CREATE_HANDLE_DICT_OPTION = """
CREATE TABLE IF NOT EXISTS handle_db.dict_option (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    field             TEXT NOT NULL,
    value             TEXT NOT NULL,
    use_count         INTEGER DEFAULT 0,
    sort_order        INTEGER DEFAULT 0,
    updated_at        TEXT,
    UNIQUE(field, value)
);
"""

CREATE_HANDLE_OP_LOG = """
CREATE TABLE IF NOT EXISTS handle_db.op_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    action            TEXT,
    detail_key        TEXT,
    payload           TEXT,
    operator          TEXT,
    created_at        TEXT
);
"""

# ---------------------------------------------------------------------------
# 接入库（2026-09-20 新增：登录验证 + 权限组 + 对外开放接口）
#
# 单独成库的理由与业务库一致：这是「谁能进来、能干什么」的横切关注点，
# 与退回/检测/处理三个业务库没有业务耦合，将来接入 SSO 或换鉴权方案时
# 只动这一个库。它也是唯一需要长期保留审计痕迹的库（登录记录不可再生）。
# ---------------------------------------------------------------------------

CREATE_AUTH_GROUP = """
CREATE TABLE IF NOT EXISTS auth_db.permission_group (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL UNIQUE,  -- 权限组名（界面上显示）
    description       TEXT,                  -- 说明：这个组是给谁用的
    perms             TEXT DEFAULT '[]',     -- JSON 数组，权限点 key 列表
    builtin           INTEGER DEFAULT 0,     -- 内置组（管理员/登记员/只读）不可删
    created_at        TEXT,
    updated_at        TEXT
);
"""

CREATE_AUTH_USERS = """
CREATE TABLE IF NOT EXISTS auth_db.users (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    username          TEXT NOT NULL UNIQUE,
    display_name      TEXT,
    password_hash     TEXT NOT NULL,         -- PBKDF2-HMAC-SHA256，格式见 core/auth.py
    group_id          INTEGER,               -- 关联 permission_group.id
    enabled           INTEGER DEFAULT 1,     -- 0 = 停用（保留账号但禁止登录）
    must_change_pwd   INTEGER DEFAULT 0,     -- 1 = 下次登录必须改密
    last_login_at     TEXT,
    created_at        TEXT,
    updated_at        TEXT
);
"""

CREATE_AUTH_SESSION = """
CREATE TABLE IF NOT EXISTS auth_db.session (
    sid               TEXT PRIMARY KEY,      -- 随机会话 ID（Cookie 里传的就是它）
    user_id           INTEGER NOT NULL,
    username          TEXT,
    ip                TEXT,
    user_agent        TEXT,
    created_at        TEXT,
    expires_at        TEXT,
    revoked           INTEGER DEFAULT 0
);
"""

CREATE_AUTH_SETTING = """
CREATE TABLE IF NOT EXISTS auth_db.setting (
    key               TEXT PRIMARY KEY,      -- auth_enabled / open_api_token / ...
    value             TEXT,
    updated_at        TEXT
);
"""

CREATE_AUTH_OP_LOG = """
CREATE TABLE IF NOT EXISTS auth_db.op_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    action            TEXT,                  -- login / logout / user.create / ...
    detail_key        TEXT,                  -- 复用现有列名：这里存目标对象
    payload           TEXT,
    operator          TEXT,
    created_at        TEXT
);
"""

CREATE_AUTH_OPEN_API_LOG = """
CREATE TABLE IF NOT EXISTS auth_db.open_api_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT,
    ip                TEXT,
    dataset           TEXT,                  -- 拉取的数据集
    endpoint          TEXT,
    rows              INTEGER DEFAULT 0,     -- 返回条数
    ok                INTEGER DEFAULT 1,
    message           TEXT
);
"""

# ---------------------------------------------------------------------------
# 匹配数据库
# ---------------------------------------------------------------------------

CREATE_ITEM_MASTER = """
CREATE TABLE IF NOT EXISTS items_db.item_master (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    material_no       TEXT UNIQUE,          -- 料号（唯一键）
    old_material_no   TEXT,                 -- 旧料号
    product_name      TEXT,                 -- 品名
    model_no          TEXT,                 -- 型号（系列号，如 BLF1-S）
    spec              TEXT,                 -- 规格（如 51177.67.773C）
    description       TEXT,                 -- 描述
    customer          TEXT,                 -- 客户
    production_stat   TEXT,                 -- 生产统计
    param1            TEXT,                 -- 产品量程
    param2            TEXT,                 -- 信号输出
    param3            TEXT,                 -- 防护等级
    category          TEXT,                 -- 产品类别
    source            TEXT DEFAULT 'import',
    created_at        TEXT,
    updated_at        TEXT
);
"""

# 按库分组的建表语句
RETURNS_DDL = (CREATE_RETURNS, CREATE_MODEL_DICT, CREATE_DICT_OPTION,
               CREATE_SYNC_LOG, CREATE_OP_LOG)
INSPECT_DDL = (CREATE_INSPECT_RECORDS, CREATE_INSPECT_DICT_OPTION,
               CREATE_INSPECT_OP_LOG)
HANDLE_DDL = (CREATE_HANDLE_RECORDS, CREATE_HANDLE_DICT_OPTION,
              CREATE_HANDLE_OP_LOG)
AUTH_DDL = (CREATE_AUTH_GROUP, CREATE_AUTH_USERS, CREATE_AUTH_SESSION,
            CREATE_AUTH_SETTING, CREATE_AUTH_OP_LOG, CREATE_AUTH_OPEN_API_LOG)
ITEMS_DDL = (CREATE_ITEM_MASTER,)

# 索引：跨库查询的关联键与常用筛选维度
INDEXES = [
    # 退回登记库
    "CREATE INDEX IF NOT EXISTS idx_returns_order_no       ON returns(order_no);",
    "CREATE INDEX IF NOT EXISTS idx_returns_return_no      ON returns(return_no);",
    "CREATE INDEX IF NOT EXISTS idx_returns_product_code   ON returns(product_code);",
    "CREATE INDEX IF NOT EXISTS idx_returns_material_no    ON returns(material_no);",
    "CREATE INDEX IF NOT EXISTS idx_returns_product_model  ON returns(product_model);",
    "CREATE INDEX IF NOT EXISTS idx_returns_return_date    ON returns(return_date);",
    "CREATE INDEX IF NOT EXISTS idx_returns_registered_at  ON returns(registered_at);",
    "CREATE INDEX IF NOT EXISTS idx_returns_category       ON returns(product_category);",
    "CREATE INDEX IF NOT EXISTS idx_returns_vendor         ON returns(turbine_vendor);",
    "CREATE INDEX IF NOT EXISTS idx_returns_sync_state     ON returns(sync_state);",
    "CREATE INDEX IF NOT EXISTS idx_dict_field             ON dict_option(field);",
    "CREATE INDEX IF NOT EXISTS idx_model_dict_code        ON model_dict(product_code);",
    # 检测登记库（detail_key 已是主键，天然有索引）
    # 注意：SQLite 的跨库索引要把库前缀加在**索引名**上，
    # 写成 `CREATE INDEX 库名.索引名 ON 表名(列)`，表名不能带库前缀。
    "CREATE INDEX IF NOT EXISTS inspect_db.idx_inspect_test_date  ON inspect_records(test_date);",
    "CREATE INDEX IF NOT EXISTS inspect_db.idx_inspect_completion ON inspect_records(completion);",
    "CREATE INDEX IF NOT EXISTS inspect_db.idx_inspect_dict_field ON dict_option(field);",
    # 处理登记库（同样：库前缀加在索引名上）
    "CREATE INDEX IF NOT EXISTS handle_db.idx_handle_erp        ON handle_records(erp_handled);",
    "CREATE INDEX IF NOT EXISTS handle_db.idx_handle_solution   ON handle_records(handle_solution);",
    "CREATE INDEX IF NOT EXISTS handle_db.idx_handle_dict_field ON dict_option(field);",
    # 接入库
    "CREATE INDEX IF NOT EXISTS auth_db.idx_users_group      ON users(group_id);",
    "CREATE INDEX IF NOT EXISTS auth_db.idx_session_user     ON session(user_id);",
    "CREATE INDEX IF NOT EXISTS auth_db.idx_session_expires  ON session(expires_at);",
    "CREATE INDEX IF NOT EXISTS auth_db.idx_openlog_time     ON open_api_log(created_at);",
    # 匹配数据库
    "CREATE INDEX IF NOT EXISTS items_db.idx_item_material_no ON item_master(material_no);",
    "CREATE INDEX IF NOT EXISTS items_db.idx_item_old_no      ON item_master(old_material_no);",
    "CREATE INDEX IF NOT EXISTS items_db.idx_item_model       ON item_master(model_no);",
    "CREATE INDEX IF NOT EXISTS items_db.idx_item_spec        ON item_master(spec);",
    "CREATE INDEX IF NOT EXISTS items_db.idx_item_name        ON item_master(product_name);",
]


# ---------------------------------------------------------------------------
# 连接管理
# ---------------------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    """每个线程持有一条连接，连接建立时挂载其余三个库。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(RETURNS_DB), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=15000;")

        # 挂载其余四个库，使跨库 JOIN 与单库写法一致。
        # 别名固定为 inspect_db / handle_db / items_db / auth_db，
        # 业务 SQL 一律用这四个前缀。
        conn.execute("ATTACH DATABASE ? AS inspect_db", (str(INSPECT_DB),))
        conn.execute("ATTACH DATABASE ? AS handle_db", (str(HANDLE_DB),))
        conn.execute("ATTACH DATABASE ? AS items_db", (str(ITEMS_DB),))
        conn.execute("ATTACH DATABASE ? AS auth_db", (str(AUTH_DB),))
        for alias in ("inspect_db", "handle_db", "items_db", "auth_db"):
            try:
                conn.execute(f"PRAGMA {alias}.journal_mode=WAL;")
            except sqlite3.Error:
                pass          # 新建库首次挂载可能尚未成型，忽略即可
        _local.conn = conn
    return conn


@contextmanager
def tx():
    """写事务上下文。

    注意：SQLite 的 ATTACH 不支持跨库原子事务，事务只对当前写入的库生效。
    因此业务上保证「一次写入只落一个库」（见模块 docstring）。
    """
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close_conn() -> None:
    """释放本线程连接（迁移脚本等场景需要先关闭再操作文件）。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


# ---------------------------------------------------------------------------
# 初始化与迁移
# ---------------------------------------------------------------------------

def init_db() -> None:
    """初始化五个库的结构（幂等，可在每次启动时调用）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    _backup_if_legacy()          # 旧单库结构先备份，再迁移

    conn = get_conn()
    for ddl in RETURNS_DDL + INSPECT_DDL + HANDLE_DDL + ITEMS_DDL + AUTH_DDL:
        conn.execute(ddl)
    conn.commit()

    # 补列必须在建索引之前 —— 新增字段索引会引用该列，
    # 顺序反了会报 "no such column"（新库首次启动也会中招，
    # 因为 CREATE TABLE 里的列要等补列逻辑处理老库，而索引是裸 SQL）。
    _ensure_columns(conn)

    for idx in INDEXES:
        conn.execute(idx)
    conn.commit()
    _migrate_legacy_returns(conn)
    _drop_stray_tables(conn)


def _drop_stray_tables(conn: sqlite3.Connection) -> list:
    """清理误建在主库的表。

    跨库建表时若漏写库前缀，表会落到主库（returns.db）而不是目标库。
    这里做一次自愈，避免残留的空表干扰后续查询。
    注意 item_master 不在清理范围内 —— 旧版本的主库里它承载真实数据，
    需由 tools/migrate_to_multidb.py 迁移而非删除。
    """
    stray = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table';")}
    dropped = []
    # 接入库的表同样要防「漏写库前缀建到主库」—— 落到主库不会报错，
    # 只是鉴权查询会一直读到空表（表现为「登录失败但账号明明存在」）。
    for name in ("inspect_records", "handle_records", "users",
                 "permission_group", "session", "setting", "open_api_log"):
        if name in stray:
            conn.execute(f"DROP TABLE IF EXISTS {name};")
            dropped.append(name)
    if dropped:
        conn.commit()
    return dropped


def _legacy_columns() -> list:
    """旧单库版 returns 表里残留的检测字段（存在即需要迁移）。"""
    if not RETURNS_DB.exists():
        return []
    try:
        probe = sqlite3.connect(str(RETURNS_DB))
        cols = {r[1] for r in probe.execute("PRAGMA table_info(returns);")}
        probe.close()
    except sqlite3.Error:
        return []
    return [c for c in INSPECT_COLUMNS if c in cols]


def _backup_if_legacy() -> str:
    """检测到旧结构时先整库备份，返回备份路径（无需备份时返回空串）。"""
    if not _legacy_columns():
        return ""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = DATA_DIR / f"backup_legacy_{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = RETURNS_DB.parent / (RETURNS_DB.name + suffix)
        if src.exists():
            shutil.copy2(src, target / src.name)
    return str(target)


def _migrate_legacy_returns(conn: sqlite3.Connection) -> int:
    """把旧版单库 returns 表里的检测字段搬进检测登记库。

    旧版（三库拆分之前）把 11 个检测字段与退回字段混在 returns 一张表里。
    这里把有内容的行写入 inspect_db.inspect_records，再从 returns 表移除这些列，
    使结构与新架构一致。全空的检测字段不建行（保持稀疏存储）。
    """
    legacy = _legacy_columns()
    if not legacy:
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sel = ", ".join(legacy)
    rows = conn.execute(
        f"SELECT detail_key, {sel} FROM returns "
        f"WHERE detail_key IS NOT NULL AND TRIM(detail_key) <> '';"
    ).fetchall()

    moved = 0
    for r in rows:
        values = [r[c] for c in legacy]
        if not any(str(v or "").strip() for v in values):
            continue                      # 该行没有任何检测内容，不建检测记录
        cols = ["detail_key"] + legacy + ["created_at", "updated_at"]
        marks = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT OR REPLACE INTO inspect_db.inspect_records "
            f"({', '.join(cols)}) VALUES ({marks});",
            [r["detail_key"]] + values + [now, now],
        )
        moved += 1

    # 移除 returns 表中的检测列
    for col in legacy:
        try:
            conn.execute(f"ALTER TABLE returns DROP COLUMN {col};")
        except sqlite3.OperationalError:
            pass                          # SQLite < 3.35 不支持 DROP COLUMN
    conn.commit()
    return moved


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """轻量迁移：补齐新增列、移除已废弃列。"""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(returns);")}
    wanted = {
        "source": "TEXT DEFAULT 'local'",
        "sync_state": "TEXT DEFAULT 'pending'",
        "synced_at": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
        "analysis_report": "TEXT",
    }
    for col, ddl in wanted.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE returns ADD COLUMN {col} {ddl};")

    # 处理登记库同样要补新列 —— SQLite 的 ALTER TABLE 支持「库名.表名」前缀。
    # 漏了这段的话新列不会存在，写入时报 "no such column"（新增字段必踩）。
    h_existing = {r["name"] for r in conn.execute(
        "PRAGMA handle_db.table_info(handle_records);")}
    h_wanted = {
        "handle_solution": "TEXT",
    }
    for col, ddl in h_wanted.items():
        if col not in h_existing:
            conn.execute(
                f"ALTER TABLE handle_db.handle_records ADD COLUMN {col} {ddl};")

    # 已废弃字段（旧库启动时自动清理）
    deprecated = (
        "reply_progress",   # 回复进度
        "order_status",     # 售后单状态
        "wait_return",      # 是否待返回
        "express_fee",      # 快递费用
    )
    for col in deprecated:
        if col in existing:
            try:
                conn.execute(f"ALTER TABLE returns DROP COLUMN {col};")
            except sqlite3.OperationalError:
                pass
    conn.commit()


def db_status() -> dict:
    """返回五库概览，供首页与健康检查使用。"""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM returns;").fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) c FROM returns WHERE sync_state='pending';"
    ).fetchone()["c"]
    models = conn.execute("SELECT COUNT(*) c FROM model_dict;").fetchone()["c"]
    inspected = conn.execute(
        "SELECT COUNT(*) c FROM inspect_db.inspect_records;").fetchone()["c"]
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
    return {
        # 保留 db_path 键以兼容既有调用方；附上五库路径便于排查
        "db_path": str(RETURNS_DB),
        "databases": {
            "returns": str(RETURNS_DB),
            "inspect": str(INSPECT_DB),
            "handle": str(HANDLE_DB),
            "items": str(ITEMS_DB),
            "auth": str(AUTH_DB),
        },
        "total": total,
        # pending_sync 是历史字段（推送式同步已于 2026-09-20 下线），
        # 保留计数只为兼容既有调用方与数据溯源，不再驱动任何行为。
        "pending_sync": pending,
        "model_dict": models,
        "inspected": inspected,
        "handled": handled,
        # 处理库的行数（含「待处理」占位行），供排查用
        "handle_rows": handle_rows,
        "users": users,
        "item_master": items,
    }
