"""MySQL 访问层 —— 连接、事务、方言适配

为什么单独一个模块
------------------
整个项目**只有这个文件知道后端是 MySQL**。六个仓储模块、鉴权、对外开放都只调
`get_conn()` / `tx()`，拿到的东西和 `sqlite3.Connection` 一样有 `.execute()`
并直接返回游标 —— 这样 400 多处 `conn.execute(...).fetchone()["c"]` 一个字都不用改。

调用契约（与 sqlite3.Connection 对齐）
------------------------------------
    conn = get_conn()
    row  = conn.execute("SELECT COUNT(*) c FROM returns").fetchone()["c"]
    cur  = conn.execute("INSERT INTO returns (order_no) VALUES (?)", (no,))
    new_id = cur.lastrowid
    rows = conn.execute("SELECT * FROM items_db.item_master WHERE id = ?",
                        (item_id,)).fetchall()

* 占位符照旧写 `?`，由 `translate()` 翻成 PyMySQL 的 `%s`；
* 行是 dict（原来是 `sqlite3.Row`），`row["c"]` 照旧、`dict(row)` 照旧；
* `execute()` **一次性把结果读进内存**再返回。原因：SQLite 的游标绑定在连接上，
  调用方可以边遍历边发下一条查询；MySQL 的游标不行，没读完就发新查询会
  `Commands out of sync`。先读进 list，遍历就与后端无关了。

两个必须记住的 MySQL 坑（都在这里处理掉）
----------------------------------------
1. `?` → `%s` 之前必须先把 SQL 里**字面的 `%` 转义成 `%%`**。PyMySQL 只在
   「传了参数」时才做 `%` 格式化，所以顺序只能是先 `%`→`%%` 再 `?`→`%s`；
   反了会把刚生成的 `%s` 也转义掉。没有参数时不转义（那时 `%` 就是字面量）。
2. `ONLY_FULL_GROUP_BY`：在 config 的 `MYSQL_SQL_MODE` 里去掉了，连接建立时
   再逐会话设一次，免得别的客户端把它改回去。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

import decimal

import pymysql
from pymysql.constants import FIELD_TYPE
from pymysql.converters import conversions
from pymysql.cursors import DictCursor

from config import (MAIN_SCHEMA, MYSQL_CHARSET, MYSQL_HOST, MYSQL_PASSWORD,
                    MYSQL_PORT, MYSQL_SQL_MODE, MYSQL_USER)

# 把用得到的异常名重新导出：仓储层原来写 `except sqlite3.IntegrityError`，
# 现在写 `except dbapi.IntegrityError`，语义一一对应。
Error = pymysql.err.MySQLError
IntegrityError = pymysql.err.IntegrityError
InterfaceError = pymysql.err.InterfaceError
OperationalError = pymysql.err.OperationalError
ProgrammingError = pymysql.err.ProgrammingError

# 连接断掉时 MySQL 报的号：2006 = server has gone away，2013 = Lost connection。
# 常见于服务器重启过，或线程闲置超过 wait_timeout。命中后就地重连再试一次。
# 注意：套接字已经死透时还会以 `InterfaceError(0, '')` 的形式报出来，那个分支
# 不在这里 —— 见 `_run()`。
_LOST = (2006, 2013)
_DUP = 1062          # duplicate entry（唯一键冲突）

_local = threading.local()


# DECIMAL → int/float：MySQL 的 `SUM(整数表达式)` 与 `AVG(整数)` 都返回 DECIMAL，
# 而 SQLite 分别返回 INTEGER / REAL。不转的话：
#   * `Decimal * 100.0` 直接抛 TypeError（检测汇总 KPI 就是这么炸的）；
#   * Decimal 进不了 `json.dumps`，得层层加 jsonable_encoder。
# 取整值转 int、其余转 float，是两端口径最接近的映射。
def _num(raw: str):
    d = decimal.Decimal(raw)
    return int(d) if d == d.to_integral_value() else float(d)


_CONV = dict(conversions)
_CONV[FIELD_TYPE.DECIMAL] = _num
_CONV[FIELD_TYPE.NEWDECIMAL] = _num


# ---------------------------------------------------------------------------
# 方言
# ---------------------------------------------------------------------------

def translate(sql: str, params) -> str:
    """`?` 占位符 → `%s`，并把字面 `%` 转义成 `%%`。

    只在「有参数」时转义：PyMySQL 不传参数时不做 % 格式化，
    这时 SQL 里的 `%` 本来就是字面量，转义反而多出一个 %。
    """
    if params is None:
        return sql
    return sql.replace("%", "%%").replace("?", "%s")


def is_duplicate(exc: BaseException, *needles: str) -> bool:
    """是不是唯一键冲突？给 needles 时还要求错误文本里出现其中之一。

    对应原来 `isinstance(exc, sqlite3.IntegrityError) and "detail_key" in str(exc)`
    的写法 —— 但这里先认错误号 1062，再挑关键词，比纯文本匹配稳。
    """
    if not isinstance(exc, pymysql.err.IntegrityError):
        return False
    if exc.args and exc.args[0] != _DUP:
        return False
    if not needles:
        return True
    text = str(exc)
    return any(n in text for n in needles)


# ---------------------------------------------------------------------------
# 结果与连接
# ---------------------------------------------------------------------------

class Result:
    """一次 execute 的结果（行已全部缓冲，可以随便遍历）。"""

    __slots__ = ("_rows", "lastrowid", "rowcount", "_cols")

    def __init__(self, rows, lastrowid=0, rowcount=-1, cols=()):
        self._rows = rows
        self.lastrowid = lastrowid
        self.rowcount = rowcount
        self._cols = cols

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def fetchmany(self, size: int = 1):
        return self._rows[:size]

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)

    @property
    def description(self):
        return self._cols

    def close(self) -> None:
        """兼容 `cur.close()` 的调用习惯；结果是内存里的 list，清掉即可。"""
        self._rows = []


def connect_raw():
    """新建一条原始 PyMySQL 连接。

    `init_command` 里逐会话设 sql_mode —— 见模块 docstring 第 2 条。
    认证插件是 mysql_native_password（见 tools/dev_mysql.py 的 my.ini），
    这样不需要 `cryptography` 那个要编译的依赖。
    """
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MAIN_SCHEMA,
        charset=MYSQL_CHARSET,
        cursorclass=DictCursor,
        conv=_CONV,
        autocommit=True,
        connect_timeout=10,
        init_command=(
            f"SET SESSION sql_mode = '{MYSQL_SQL_MODE}', "
            # GROUP_CONCAT 在 MySQL 里默认只留 1024 字节 —— 核销台账按物料号
            # 聚合时会静默截断，列表看着「少了几个」而不报错。SQLite 无此上限。
            "SESSION group_concat_max_len = 10000000"
        ),
    )


class Connection:
    """套在 PyMySQL 连接外面的薄壳，补上 sqlite3 才有的 `conn.execute()`。"""

    def __init__(self, raw):
        self._raw = raw

    # —— 与 sqlite3.Connection 对齐 ——
    def execute(self, sql: str, params=None) -> Result:
        return self._run(sql, params)

    def executemany(self, sql: str, seq) -> Result:
        rows = list(seq or [])
        if not rows:
            return Result([])
        return self._run(sql, rows, many=True)

    def _run(self, sql, params, many: bool = False) -> Result:
        last = None
        for attempt in (1, 2):
            try:
                cur = self._raw.cursor()
                try:
                    if many:
                        cur.executemany(translate(sql, params[0]), params)
                    else:
                        cur.execute(translate(sql, params),
                                    params if params is not None else None)
                    rows = cur.fetchall() if cur.description else []
                    return Result(rows, cur.lastrowid, cur.rowcount,
                                  cur.description or ())
                finally:
                    cur.close()
            except pymysql.err.InterfaceError as exc:
                # 掉线后套接字已死时 PyMySQL 抛的是 InterfaceError(0, '')，而不是
                # OperationalError。旧代码只 catch 后者 → 永远走不到重连，
                # 进程就一直 500 到人工重启（2026-09-22 实测：MySQL 重启后
                # /api/health 与两个同步定时器持续报 InterfaceError: (0, '')）。
                # 这类错误一律重连一次再试 —— 服务端回来了就自愈。
                last = exc
                if attempt == 2:
                    raise
                self._reconnect()
            except pymysql.err.OperationalError as exc:
                last = exc
                if attempt == 2 or not (exc.args and exc.args[0] in _LOST):
                    raise
                self._reconnect()
        raise last          # pragma: no cover —— 循环必然 return 或 raise

    def _reconnect(self) -> None:
        try:
            self._raw.close()
        except Exception:                       # noqa: BLE001 —— 关不掉也要重连
            pass
        self._raw = connect_raw()

    # —— 其余透传 ——
    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def begin(self) -> None:
        self._raw.begin()

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:                       # noqa: BLE001
            pass

    def ping(self, reconnect: bool = True) -> None:
        self._raw.ping(reconnect)

    def cursor(self):
        return self._raw.cursor()

    @property
    def raw(self):
        return self._raw


# ---------------------------------------------------------------------------
# 线程本地连接（与原来 SQLite 的写法一致）
# ---------------------------------------------------------------------------

def get_conn() -> Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = Connection(connect_raw())
        _local.conn = conn
    return conn


@contextmanager
def tx():
    """写事务上下文。

    比 SQLite 那版更硬：InnoDB 的事务**跨 schema 是原子的**，
    所以旧代码里「一次写入只落一个库」的约束不再需要（跨库更新也能一起回滚）。
    """
    conn = get_conn()
    conn.begin()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


def reset_conn() -> None:
    """丢掉本线程的连接并立刻重开（迁移脚本、自检改完配置后用）。"""
    close_conn()
    get_conn()


def server_info() -> dict:
    """连得上吗？连的哪个版本、什么字符集。启动横幅与自检都靠它。"""
    row = get_conn().execute(
        "SELECT VERSION() v, @@character_set_connection cs, "
        "@@collation_connection cc, @@sql_mode m, DATABASE() db;").fetchone()
    return dict(row or {})
