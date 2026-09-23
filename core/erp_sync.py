"""ERP（用友 U9）物料主档 → 本地匹配库 的自动同步

为什么需要它
------------
匹配库（`items_db.item_master`）是退回登记「输料号自动回填」的第一优先来源
（`repo.suggest_by_material`）。它原先靠人工把 ERP 导出的 Excel 拖进来刷新，
忘一次就整个库停在上次那份快照上 —— 表现为「新料号查不到、按料号回填不出来」，
而且**不报错**。这里把刷新变成自动的。

数据源：11 列，**全部实时**（2026-09-22 改版）
--------------------------------------------
一条 SQL 直接取全，**不再依赖任何离线快照**：

    SELECT A.Code, A.Code1, A.SPECS,
           A.DescFlexField_PrivateDescSeg3 / _Seg4 / _Seg5 / _Seg8 / _Seg9 / _Seg10,
           A.Name, B.Description
      FROM dbo.CBO_ItemMaster A
      LEFT JOIN dbo.CBO_ItemMaster_Trl B ON B.ID = A.ID AND B.SysMLFlag = 'zh-CN'

| 本地列 | ERP 列 |
|---|---|
| `material_no` 料号 | `A.Code` |
| `model_no` 型号 | `A.Code1` |
| `spec` 规格 | `A.SPECS` |
| `product_name` 品名 | `A.Name`（**主表**；见 `ITEMS_SQL` 下的注释） |
| `description` 描述 | `B.Description`（多语言表） |
| `old_material_no` 旧料号 | `A.DescFlexField_PrivateDescSeg5` |
| `customer` 客户 | `A.DescFlexField_PrivateDescSeg4` |
| `production_stat` 生产统计 | `A.DescFlexField_PrivateDescSeg3`（内码 → 翻译） |
| `param1/2/3` 标参1-3 | `A.DescFlexField_PrivateDescSeg8 / _Seg9 / _Seg10` |

⚠️ **前提：`sh_report_user` 要拿到 8 列权限**（旧版只要 3 列）：

    GRANT SELECT ON dbo.CBO_ItemMaster(
        ID, Name,
        DescFlexField_PrivateDescSeg3, DescFlexField_PrivateDescSeg4,
        DescFlexField_PrivateDescSeg5, DescFlexField_PrivateDescSeg8,
        DescFlexField_PrivateDescSeg9, DescFlexField_PrivateDescSeg10)
      TO sh_report_user;

`ID` 是 JOIN 键（`CBO_ItemMaster_Trl` 只有 ID、没有料号，两表只能靠 ID 关联，
而在 JOIN/WHERE 里引用某列同样需要它的 SELECT 权限）；`Name` 目前用不上
（品名取 Trl 的 `NameCombineName` 更准），要上是给将来留余地。

**权限没到位时不会静默降级**：`sync()` 会在取数这步失败并写明缺哪几列
（状态里的 `missing_cols`），匹配库保持上一次的数据**原样不动** ——
宁可停在旧数据上，也不让半份数据覆盖进去。

> 历史（为什么会有这次改版）：2026-09-21 之前是「实时 3 列 + 离线快照补 6 列 +
> ID→料号 桥」。快照冻结在 09-21，之后新建的料号拿不到补充列（实测 17 个）。
> 用户 2026-09-22 决定**去掉快照、全部走实时** —— 快照代码、`data/erp/` 依赖、
> `extra_hit` / `bridge_miss` / `has_id` 三个状态字段已随之删除。

文本列必须 CAST（两个坑，都实测踩过）
-------------------------------------
1) 直接读 nvarchar，FreeTDS 在 cp936 下**会静默丢字或抛 UnicodeDecodeError**：
   `SELECT * FROM CBO_ItemMaster_Trl` 取回的品名整列是空串，只有
   `CAST(col AS VARBINARY(MAX))` 再本地按 utf-16-le 解码才拿得到真值。
   分类翻译那张表更阴：6909 行直接读只剩 43 条活下来。
2) 发往 ERP 的 SQL 里**不要写中文别名**（会报 `(102, ... Incorrect syntax near '\xbe')`）。

生产统计能翻译成可读名
----------------------
`DescFlexField_PrivateDescSeg3` 存的是**分类内码**（`2320`/`2301`…），
本地历史上存的是**可读名**（`风速【低温】`/`低温型电缆`…）。直接对接会把类别
写成数字。`Base_DefineValue` + `Base_DefineValue_Trl` **有全列权限**，实测
73 个内码全部命中，翻译结果与本地既有可读名一致。所以同步时把内码翻成
可读名再写库，口径与历史数据保持连续；**翻不出来就不写**该列（不把内码灌进名字列）。

调度与并发
----------
* 后台守护线程按**墙上时钟**判断是否到点（`start_scheduler` → `core/sched.py`
  的节拍器：每分钟醒一次，比较「上次同步时间 + 间隔」与当前时间）。
  **幂等** —— `main()` 与 `lifespan` 都会调，重复启动会变成两个线程同时拉 ERP；
* 手动触发与定时触发共用一把锁（`_SYNC_LOCK`），不会并发写库；
* 拉取走只读连接 + autocommit，不占 ERP 的锁。

凭据
----
**只从环境变量或 `auth_db.setting` 读，代码里没有默认值 / 明文**：

    ARS_ERP_HOST / ARS_ERP_PORT / ARS_ERP_DB / ARS_ERP_USER / ARS_ERP_PASSWORD

页面可写（存 `setting`），但**接口永不回读密码**，只回 `password_set: true/false`。
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from config import DATA_DIR
from core import erp_conn, sched

# ---------------------------------------------------------------------------
# 配置（环境变量优先；页面写入的存 auth_db.setting）
# ---------------------------------------------------------------------------


DEFAULTS = {
    "host": "192.168.1.247",
    "port": "1433",
    "db": "BLFN",
    "user": "sh_report_user",
    # 密码**没有默认值** —— 缺了就是没配好，同步会明确报错而不是拿一个
    # 写死的弱口令去连生产库。
    "password": "",
    "interval_hours": "12",     # 自动同步间隔（小时）
    "enabled": "1",             # 自动同步总开关
}

# 环境变量名 → setting 键
ENV_KEYS = {
    "host": "ARS_ERP_HOST",
    "port": "ARS_ERP_PORT",
    "db": "ARS_ERP_DB",
    "user": "ARS_ERP_USER",
    "password": "ARS_ERP_PASSWORD",
}

STATUS_FILE = DATA_DIR / "erp_sync_status.json"

# ★ 主表 + 多语言表的**全实时**查询（11 列，一条 SQL）。
#   文本列一律 CAST 成 VARBINARY 再本地解码 —— 直接读 nvarchar 时 FreeTDS 在
#   cp936 下会截断 / 静默丢字（见模块 docstring 的两条坑）。
UNICODE_CAST = "CAST({col} AS VARBINARY(MAX))"


def _mc(col: str) -> str:
    """主表（别名 A）的列，带 CAST。"""
    return UNICODE_CAST.format(col="A.[%s]" % col)


def _tc(col: str) -> str:
    """多语言表（别名 B）的列，带 CAST。"""
    return UNICODE_CAST.format(col="B.[%s]" % col)


# 11 列一次取全。列顺序必须与 fetch_items 里的解包顺序**严格一致**。
ITEMS_SQL = (
    "SELECT " + ", ".join([
        _mc("Code"),                            # 1 料号
        _mc("Code1"),                           # 2 型号
        _mc("SPECS"),                           # 3 规格
        _mc("DescFlexField_PrivateDescSeg5"),   # 4 旧料号
        _mc("DescFlexField_PrivateDescSeg4"),   # 5 客户
        _mc("DescFlexField_PrivateDescSeg3"),   # 6 生产统计（内码，要翻译）
        _mc("DescFlexField_PrivateDescSeg8"),   # 7 标参1
        _mc("DescFlexField_PrivateDescSeg9"),   # 8 标参2
        _mc("DescFlexField_PrivateDescSeg10"),  # 9 标参3
        _mc("Name"),                            # 10 品名 —— **取主表**，见下
        _tc("Description"),                     # 11 描述（多语言表）
    ])
    + " FROM dbo.CBO_ItemMaster A"
      " LEFT JOIN dbo.CBO_ItemMaster_Trl B ON B.ID = A.ID"
      " WHERE A.Code IS NOT NULL AND LTRIM(RTRIM(A.Code)) <> ''"
)
# ⚠️ 两处刻意的取舍（2026-09-22 实测后定）：
#   ① **品名取主表 `A.Name`，不取 Trl 的 `NameCombineName`** —— 后者在权限重新
#      配置时被收窄掉了（不可读）。实测 `A.Name` 与本地库里那 13178 个品名
#      **完全一致**（100%），余下 17 个是"本地空、ERP 有值"（快照时代漏掉的料号），
#      正好补上。**规格一列做对照：13195/13195 全等**，证明取数无误。
#   ② **JOIN 不带 `B.SysMLFlag = 'zh-CN'`** —— `CBO_ItemMaster_Trl` 的 ID
#      实测**全局唯一**（13195 行 / 13195 个 ID），是单语言表，多语言过滤没有意义；
#      而该列同样被收窄、不可读。去掉它不少一行数据（主表 13195 行全部能 JOIN 上）。

# 这 8 列是「全实时」的前提。缺任何一列，同步会失败并把它记进 missing_cols
# （**不静默降级**：宁可停在旧数据，也不让半份数据覆盖匹配库）。
# `Name` 不在列表里 —— 品名走 Trl 的 NameCombineName；要上是给将来留余地。
REQUIRED_COLS = (
    "ID",
    "DescFlexField_PrivateDescSeg3",
    "DescFlexField_PrivateDescSeg4",
    "DescFlexField_PrivateDescSeg5",
    "DescFlexField_PrivateDescSeg8",
    "DescFlexField_PrivateDescSeg9",
    "DescFlexField_PrivateDescSeg10",
)

# 分类内码 → 可读名。★ `t.Name` 也必须 CAST —— 这是最阴的一处：直接读 nvarchar
# 时 cp936 下**大多数行的名字会静默变成空串**，`if c and n` 一过滤就只剩几十条
# 脏数据（2026-09-22 实测：6909 行的表只活下来 43 条）。
MAP_SQL = ("SELECT " + UNICODE_CAST.format(col="b.Code") + ", "
           + UNICODE_CAST.format(col="t.Name") + " "
           "FROM dbo.Base_DefineValue b "
           "JOIN dbo.Base_DefineValue_Trl t ON t.ID = b.ID AND t.SysMLFlag = 'zh-CN' "
           "WHERE b.Code IS NOT NULL AND LTRIM(RTRIM(b.Code)) <> '' "
           "  AND t.Name IS NOT NULL AND LTRIM(RTRIM(t.Name)) <> ''")

_SYNC_LOCK = threading.Lock()
_state: dict = {"running": False}
_runtime: dict = {"running": False, "started_at": "", "last_error": ""}


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------

def get_config(redact: bool = True) -> dict:
    """读**匹配数据库**的同步配置（`erp_*`；环境变量 `ARS_ERP_*` 优先）。

    ★ 2026-09-22 起两个任务的连接**分开存**：这里是匹配库那一份，
    发货明细那一份在 `core/erp_ship.get_conn()`（`ship_erp_*`）。
    通用的「环境变量 > setting > 默认值」规则两边共用，
    实现在 `core/erp_conn.py`。
    """
    return erp_conn.read("erp_", ENV_KEYS, DEFAULTS, redact=redact)


def set_config(data: dict) -> dict:
    """写匹配库的配置到 setting。

    **密码只写不读**：传空字符串 = 不改（页面上的空密码框不该抹掉已存的密码），
    传 None = 清除。其余字段传空 = **删掉该键**（回落默认值），
    不在表里留「显式空值」——那种值看着像没配、实际在生效。
    """
    return erp_conn.write("erp_", ENV_KEYS, DEFAULTS, data)


# ---------------------------------------------------------------------------
# 状态留痕（供界面/自检读）
# ---------------------------------------------------------------------------

def status() -> dict:
    """最近一次同步结果 + 是否该同步了。"""
    from config import DATA_DIR as _D                       # noqa: PLC0415
    cfg = get_config(redact=True)
    out = {
        "configured": bool(cfg.get("password_set")) and bool(cfg.get("host")),
        "enabled": cfg.get("enabled") == "1",
        "interval_hours": _as_int(cfg.get("interval_hours"), 12),
        "host": cfg.get("host"), "db": cfg.get("db"), "user": cfg.get("user"),
        "password_set": cfg.get("password_set"),
        "config_source": cfg.get("source"),
        "running": bool(_runtime.get("running")),
        # 界面要能回答「它到底排上了没有」。没有这一格，用户看到的只有
        # 「最近一次同步是 X 小时前」，分不清「没启用」和「启用但还没到点」。
        "next_at": next_sync_at(),
        "ok": None, "at": "", "age_hours": None, "stale": False,
        "inserted": 0, "updated": 0, "total_local": 0, "erp_rows": 0,
        "category_mapped": 0, "named": 0, "described": 0,
        "missing_cols": [],
        "note": "", "error": "",
        "scope": {
            "synced": ["全部料号（整表覆盖，ERP 里没有的本地删除）",
                       "品名 / 型号 / 规格（主表 Code·Name·Code1·SPECS，实时）",
                       "描述（多语言表 CBO_ItemMaster_Trl，实时）",
                       "旧料号 / 客户 / 标参1-3（主表 DescFlexField_*，实时）",
                       "生产统计（主表内码 → 可读名，实时）"],
            # 2026-09-22 起**没有**快照补充列了 —— 保留这个空键是为了让前端
            # 「数据源」那段在两种版本下都能渲染（渲染逻辑不必分支）。
            "snapshot": [],
            "why": "**全部实时**：一条 SQL 从主表 CBO_ItemMaster LEFT JOIN 多语言表 "
                   "CBO_ItemMaster_Trl 取全 11 列，不再依赖任何离线快照。"
                   "前提是 sh_report_user 拿到 8 列权限（ID 作 JOIN 键 + 7 个字段列）；"
                   "权限不足时同步会失败，并在「缺哪些列」里写明要授权哪一列，"
                   "匹配库保持上一次的数据不动。",
        },
    }
    if not STATUS_FILE.exists():
        out["note"] = "还没有同步记录"
        return out
    try:
        d = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:                                # noqa: BLE001
        out["note"] = f"状态文件无法解析：{exc}"
        return out

    out["ok"] = bool(d.get("ok"))
    out["at"] = str(d.get("at") or "")
    out["error"] = str(d.get("error") or "")
    # ★ 这个白名单漏一个键，界面就永远显示上面 :261-262 的默认值（0 / False）——
    #   2026-09-22 第一轮真同步就踩了：库里品名已经补好 13178 行，
    #   同步状态却报 named=0，看着像「品名根本没补上」。
    for k in ("inserted", "updated", "total_local", "erp_rows",
              "category_mapped", "named", "described", "missing_cols",
              "seconds", "trigger"):
        if k in d:
            out[k] = d[k]
    try:
        when = datetime.strptime(out["at"], "%Y-%m-%d %H:%M:%S")
        age = (datetime.now() - when).total_seconds() / 3600.0
        out["age_hours"] = round(age, 1)
        out["stale"] = age > out["interval_hours"] * 2
    except ValueError:
        out["note"] = f"同步时间无法解析：{out['at']!r}"
        return out

    if not out["ok"]:
        out["note"] = f"最近一次同步失败：{out['error'] or '未知原因'}"
    elif out["stale"]:
        out["note"] = (f"最近一次成功同步在 {out['age_hours']} 小时前，"
                       f"已超过 {out['interval_hours'] * 2} 小时（间隔的 2 倍）")
    else:
        out["note"] = (f"最近一次同步 {out['age_hours']} 小时前 · "
                       f"整表覆盖写入 {out['inserted'] + out['updated']} 行")
    return out


def _write_status(ok: bool = False, **kw) -> None:
    """把最近一次同步结果落盘（界面据此提示「同步是否超期」）。

    `ok` 给默认值是为了能用 `_write_status(**payload)` 这种写法 ——
    payload 里自带 ok 时不必再单独传，避免「多次赋值给 ok」的参数冲突。
    """
    payload = {"ok": bool(ok), "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               **kw}
    try:
        STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError as exc:
        print(f"[warn] ERP 同步状态写入失败：{exc}", flush=True)


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 离线快照：ID → 料号 的桥 + ERP 在线读不到的补充列
# ---------------------------------------------------------------------------

def pad_old_no(v: str) -> str:
    """旧料号补前导 0（返回值，不改入参）。

    ERP 的 `DescFlexField_PrivateDescSeg5` 是数值字段，前导 0 会被吃掉：
    `012100100` 读出来是 `12100100`，而本地这一列是文本、带 0。
    只处理纯数字、长度 6~10、且本来不以 0 开头的值 —— 长度离谱的先不动，
    宁可留空等人看一眼，也不要写出个 11 位的怪料号。

    规则与 `tools/import_item_master.py` 共用一份（那边 import 本函数）。
    """
    v = (v or "").strip()
    if v and v.isdigit() and not v.startswith("0") and 6 <= len(v) <= 10:
        return "0" + v
    return v


def _cell_text(v) -> str:
    """xlsx/csv 单元格 → 干净字符串。

    xlsx 里数值型单元格可能带 `.0`（`12100100.0`），而旧料号/内码必须是
    纯数字文本，所以在入口处统一抹掉，别让它流进库里变成 `12100100.0`。
    """
    s = str(v if v is not None else "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _int_or_none(v):
    s = _cell_text(v)
    return int(s) if s.isdigit() else None


def _connect(cfg: dict):
    try:
        import pymssql
    except ImportError as exc:                              # pragma: no cover
        # 2026-09-22 起 pymssql 已进 requirements.txt（ERP 同步是两个模块的
        # 自动同步赖以工作的依赖）。这里保留惰性导入与明确报错，是为了让
        # 「pip 装不上 pymssql 的机器」仍然能启动应用，只是同步用不了。
        raise RuntimeError(
            "缺少 pymssql —— ERP 取数依赖，已在 requirements.txt 里。"
            "安装：<venv>\\Scripts\\python.exe -m pip install pymssql") from exc

    return pymssql.connect(
        server=cfg["host"], port=_as_int(cfg.get("port"), 1433),
        user=cfg["user"], password=cfg["password"], database=cfg["db"],
        # cp936 是实测唯一可用的值；直接读 nvarchar 会踩 FreeTDS 截断，
        # 所以文本列一律 CAST 成 varbinary，见本模块顶部说明。
        charset="cp936", tds_version="7.3",
        timeout=300, login_timeout=15,
    )


def _decode(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        if len(v) % 2:
            v = v[:-1]
        return bytes(v).decode("utf-16-le", errors="replace")
    return str(v)


def fetch_category_map(cfg: dict) -> dict:
    """取「分类内码 → 可读名」映射（`Base_DefineValue` 有全列权限）。

    ★ 与匹配库那 11 列的权限**无关** —— 这张表一直是全列可读的，
      所以「内码 → 可读名」这一步在任何权限状态下都不受影响。
      受影响的只是内码本身（`DescFlexField_PrivateDescSeg3`）。
    """
    conn = _connect(cfg)
    try:
        conn.autocommit(True)
        cur = conn.cursor()
        cur.execute(MAP_SQL)
        out = {}
        for code, name in cur:
            c, n = _decode(code).strip(), _decode(name).strip()
            if c and n:
                out.setdefault(c, n)
        return out
    finally:
        conn.close()


def fetch_items(cfg: dict) -> list:
    """取主表 + 多语言表：**11 列一次拉全**（全实时，不依赖任何离线快照）。

    返回
    ----
    `[{'material_no','model_no','spec','old_material_no','customer','stat_code',
        'param1','param2','param3','product_name','description'}]`

    ★ **不做静默降级**：11 列里有任何一列没权限，SQL 会直接报 230，异常原样抛出，
      由 `sync()` 落进状态（`missing_cols`）并中止 —— 匹配库保持上一次的数据不动。
      刻意**不**退化成「只取能读的那几列」：那会写出半份数据覆盖全表，
      比同步失败难发现得多（旧版靠快照兜底就是为了绕开这个问题，
      用户 2026-09-22 明确要求去掉 —— 宁可失败，也不要一份来源混杂的库）。
    """
    conn = _connect(cfg)
    try:
        conn.autocommit(True)
        cur = conn.cursor()
        cur.execute(ITEMS_SQL)
        rows = []
        for (code, code1, specs, old_no, cust, stat, p1, p2, p3,
             name, desc) in cur:
            m = _decode(code).strip()
            if not m:
                continue                     # 没料号的行进不了匹配库（它是主键口径）
            rows.append({
                "material_no": m,
                "model_no": _decode(code1).strip(),
                "spec": _decode(specs).strip(),
                "old_material_no": _decode(old_no).strip(),
                "customer": _decode(cust).strip(),
                "stat_code": _decode(stat).strip(),      # 内码，稍后翻译
                "param1": _decode(p1).strip(),
                "param2": _decode(p2).strip(),
                "param3": _decode(p3).strip(),
                "product_name": _decode(name).strip(),
                "description": _decode(desc).strip(),
            })
        return rows
    finally:
        conn.close()


def finalize(items: list, cat: dict) -> dict:
    """收尾：旧料号补前导 0、生产统计内码翻译成可读名。

    这一小段是「失败得很安静」的高发区，三条底线（各有历史教训）：

    * **不把内码当名字写**：`DescFlexField_PrivateDescSeg3` 是分类内码
      （`2320`），本地存的是可读名（`风速【低温】`）。`Base_DefineValue` 命中不了
      的内码**宁可留空**，也不要把 `2320` 灌进类别列（那会静默污染 1700 多行）；
    * **旧料号补 0 只碰纯数字 6~10 位**（见 `pad_old_no`）—— ERP 该列是数值型，
      前导 0 被吃掉；离谱的值原样留着，让人能一眼看出来；
    * **空值不凑数**：ERP 里为空就一直为空，绝不拿别的列去填。

    返回各列命中数（写进同步状态，界面据此判断这次同步「补上了多少」）。
    """
    named = described = mapped = 0
    for it in items:
        if it.get("product_name"):
            named += 1
        if it.get("description"):
            described += 1
        it["old_material_no"] = pad_old_no(it.get("old_material_no", ""))
        code = (it.pop("stat_code", "") or "").strip()
        got = cat.get(code, "")
        it["production_stat"] = got
        if got:
            mapped += 1
    return {"named": named, "described": described, "category_mapped": mapped}


def _missing_cols_from_error(msg: str) -> list:
    """从 SQL Server 的 230 错误里抠出被拒的列名。

    SQL Server **一次只报它撞到的第一列**，所以拿到的通常是「第一个拦路的」，
    不是全部 —— 但足够让人去要权限了（要完第一次、再跑一次会报下一列）。
    非权限类错误（连接失败 / 超时）一律返回空，不会被误认成权限问题。
    """
    import re as _re
    if "permission was denied" not in msg.lower():
        return []
    m = _re.search(r"column '([^']+)'", msg)
    return [m.group(1)] if m else list(REQUIRED_COLS)


def sync(operator: str = "erp-sync", trigger: str = "manual",
         _locked: bool = False) -> dict:
    """跑一次同步。同一时刻只允许一个（定时与手动共用锁）。

    2026-09-22 起仍是**整表全量覆盖**（`mode="replace"`：先清空
    `items_db.item_master` 再写入本次的行），ERP 里删掉的料号本地也会消失。
    与早些时候的区别是 payload 现在带全 11 列，所以整表覆盖**不再等于**
    「把品名/旧料号/描述清空」—— 那是账号只有三列权限时的症状。

    `_locked=True` 表示调用方（`sync_in_thread`）**已经抢到了锁并置好 running**，
    这里不再重复抢 —— 那样会把自己锁在门外。
    """
    from core import repository as repo

    if not _locked:
        if not _SYNC_LOCK.acquire(blocking=False):
            return {"ok": False, "error": "已有同步在进行中，本次跳过"}
        _runtime.update({"running": True,
                         "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    started = time.time()
    try:
        cfg = get_config(redact=False)
        if not cfg.get("password"):
            raise RuntimeError(
                "未配置 ERP 密码。设置环境变量 ARS_ERP_PASSWORD，"
                "或在「匹配数据库 → ERP 同步」里填写")
        if not cfg.get("host") or not cfg.get("user"):
            raise RuntimeError("ERP 地址或账号未配置")

        print(f"[erp-sync] 开始（{trigger}）：{cfg['user']}@{cfg['host']}/"
              f"{cfg['db']}", flush=True)
        # ★ 11 列一次拉全（全实时，没有快照、没有桥）。列权限不足时这里直接
        #   抛 230，由下面的 except 落进状态 —— 匹配库一个字节都不会被动。
        items = fetch_items(cfg)
        print(f"[erp-sync] 主表+多语言表取到 {len(items)} 行（11 列全实时）",
              flush=True)

        # 内码 → 可读名（Base_DefineValue 有全列权限，与本表无关）
        cat = fetch_category_map(cfg)
        print(f"[erp-sync] 分类名映射 {len(cat)} 条", flush=True)

        # ⚠️ 2026-09-22 口径（用户拍板，取「方案 B」）：落地仍是**整表全量覆盖**
        #    （mode="replace"，先 DELETE 整张 item_master 再插入这里的行）。
        #    差别在于 payload 现在是完整的 11 列 —— 品名/描述来自实时多语言表，
        #    旧料号/客户/生产统计/标参来自快照。整表覆盖不再清空这些列，
        #    `tools/import_item_master.py --fill --pad-old-no` 也就不必再当"补丁"用。
        #
        #    历史原因留档：更早是「upsert + 类别只补空」，因为 ERP 分类表里同一
        #    内码可能译出更笼统的名字（内码 2335 → `III`，本地原本 `III型风向`），
        #    无条件写入会把 60 多个更精确的类别降级。整表覆盖后本地值本来就没了，
        #    这个顾虑不再成立 —— 类别现在一律按内码翻译后写入。
        #
        # 品名/描述按料号归位、快照补充列合并 —— 这一段全是"失败得很安静"的坑
        # （空值覆盖、内码当名字写、桥外料号瞎凑），所以抽成纯函数并配了
        # 反例测试（tests/smoke_test.py【34】直接喂假数据跑）。
        counts = finalize(items, cat)
        named, described = counts["named"], counts["described"]
        mapped = counts["category_mapped"]
        print(f"[erp-sync] 数据源：品名 {named} · 描述 {described} · "
              f"类别 {mapped}", flush=True)

        result = repo.import_items(items, operator=operator, mode="replace")
        st = repo.item_stats()
        seconds = round(time.time() - started, 2)

        payload = {
            "ok": True, "trigger": trigger,
            "erp_rows": len(items),
            "inserted": result["inserted"], "updated": result["updated"],
            "total_local": st["total"], "category_mapped": mapped,
            "named": named, "described": described,
            "mode": result["mode"],
            "seconds": seconds,
        }
        # 注意：payload 自己也带 "ok"，直接 `_write_status(True, **payload)`
        # 会撞成 "got multiple values for argument 'ok'"（实测踩过）。
        # 落盘内容与返回值都保留 ok 字段，所以先复制再传关键字。
        _write_status(**{**payload, "ok": True})
        print(f"[erp-sync] 完成：整表覆盖，写入 "
              f"{result['inserted'] + result['updated']} 行 · "
              f"本地共 {st['total']} · 品名 {named} · 描述 {described} · "
              f"类别 {mapped} · {seconds}s", flush=True)
        return payload
    except Exception as exc:                                # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"[:400]
        # ★ 权限不足时把「缺哪一列」抠出来 —— 只有「同步失败」四个字，
        #   拿到的人不知道该去要什么权限（这是这次改版最可能撞上的失败）。
        missing = _missing_cols_from_error(str(exc))
        if missing:
            msg = ("ERP 列权限不足，读不到：" + "、".join(missing)
                   + "。请让管理员按 core/erp_sync.py 模块 docstring 里的 GRANT "
                     "语句授权后重试（匹配库未被改动）")
        print(f"[erp-sync] 失败：{msg}", flush=True)
        _write_status(False, error=msg, trigger=trigger, missing_cols=missing,
                      seconds=round(time.time() - started, 2))
        return {"ok": False, "error": msg, "missing_cols": missing}
    finally:
        _runtime.update({"running": False, "started_at": ""})
        _SYNC_LOCK.release()


def sync_in_thread(operator: str = "erp-sync", trigger: str = "manual") -> bool:
    """后台跑一次（接口用）。返回是否真的启动了。

    **必须在派生线程之前抢占**：原来只检查 `_runtime["running"]`，
    而那个标志要等线程跑起来才置位 —— 两次请求挨着来就会双双通过检查
    （实测「第一次点就报已有同步在进行中」，因为状态是上一次同步残留的
    真实状态与预期的竞态窗口叠加）。这里改成先拿锁、再置 running、最后起线程，
    起不来就立刻释放。
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        return False
    _runtime.update({"running": True,
                     "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

    def _run():
        try:
            sync(operator=operator, trigger=trigger, _locked=True)
        except Exception as exc:                            # noqa: BLE001
            # sync() 自己会兜住异常并写状态；这里只是最后一道保险，
            # 保证锁一定被释放，否则一次意外会让同步永久卡死。
            print(f"[erp-sync] 线程异常：{exc}", flush=True)
            _runtime.update({"running": False, "started_at": ""})
            try:
                _SYNC_LOCK.release()
            except RuntimeError:
                pass

    threading.Thread(target=_run, name="erp-sync-once", daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# 定时调度
# ---------------------------------------------------------------------------

def last_sync_at():
    """上次同步时间（手动/定时都算）。读不到返回 None。

    拿状态文件里的 `at` 而不是内存变量：**手动同步也应当把节拍推后** ——
    用户刚点过一次，一分钟后又自动跑一次纯属浪费；而且进程重启后
    「上次是什么时候」还得从磁盘上找回，否则每次重启都必然多跑一次。
    """
    if not STATUS_FILE.exists():
        return None
    try:
        d = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        return datetime.strptime(str(d.get("at") or ""), "%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return None


def _interval_hours() -> int:
    return max(1, _as_int(get_config(redact=True).get("interval_hours"), 12))


def next_sync_at() -> str:
    """预计下次自动同步时间（界面显示用）。未启用/未配密码时返回空串。"""
    cfg = get_config(redact=True)
    if cfg.get("enabled") != "1" or not cfg.get("password_set"):
        return ""
    when = (last_sync_at() or datetime.now()) + timedelta(hours=_interval_hours())
    if when <= datetime.now():
        return "随时（已到点）"
    return when.strftime("%Y-%m-%d %H:%M")


def auto_due() -> tuple:
    """这一拍该不该自动跑。返回 `(是否到点, 原因)`。

    未启用 / 未配密码时返回空原因：节拍器不会为「没开」刷日志。
    """
    cfg = get_config(redact=True)
    if cfg.get("enabled") != "1":
        return False, ""
    if not cfg.get("password_set"):
        return False, ""
    hours = _interval_hours()
    last = last_sync_at()
    if last is None:
        return True, "还没有同步记录，启动后先跑一次"
    age = (datetime.now() - last).total_seconds()
    if age >= hours * 3600:
        return True, (f"距上次同步 {age / 3600:.1f} 小时，"
                      f"已达到 {hours} 小时间隔")
    return False, ""


def _run_scheduled() -> None:
    if not sync_in_thread(trigger="schedule"):
        print("[erp-sync] 已有同步在跑，本轮跳过", flush=True)


# 节拍器（`due` / `run` 都是上面这两个函数）。放在这里而不是文件顶部，
# 是因为 Ticker 只是把它们记下来，真正调用发生在 start_scheduler 之后。
_ticker = sched.Ticker("erp-sync", due=auto_due, run=_run_scheduled)


def start_scheduler() -> dict:
    """拉起后台定时同步线程。**幂等** —— 重复调用不会起第二个线程。

    幂等的理由同 `open_api.start_in_thread`：`main()` 与 FastAPI 的 `lifespan`
    都会调，重复启动会变成两个线程同时按各自节奏拉 ERP 并写同一个库。
    """
    return _ticker.start()


def stop_scheduler() -> None:
    _ticker.stop()


# 同一套 ERP 连接与文本解码规则，供 core/erp_ship.py（发货明细同步）复用。
# 显式再导出，而不是让对方去摸 `_connect` / `_decode` —— 这两条规则
# （cp936 连接、varbinary + utf-16-le 解码）是本项目踩过两次的坑，
# 只允许存在一份实现。
connect_erp = _connect
decode_erp_text = _decode


__all__ = ["get_config", "set_config", "status", "sync", "sync_in_thread",
           "fetch_items", "fetch_category_map", "finalize",
           "pad_old_no", "start_scheduler",
           "stop_scheduler", "STATUS_FILE", "connect_erp", "decode_erp_text"]
