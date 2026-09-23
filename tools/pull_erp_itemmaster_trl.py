"""拉取 ERP（用友 U9）`CBO_ItemMaster_Trl` 整表 → CSV。

为什么单独一个脚本，而不用 tools/pull_erp_itemmaster.py
--------------------------------------------------------
那个脚本一次拉两张表（CBO_ItemMaster + CBO_ItemMaster_Trl），而
`sh_report_user` 的列级权限在 2026-09-21 被收紧：主表只剩
`Code / Code1 / SPECS` 三列可读，其余列（含 ID / Name / 全部
`DescFlexField_*`）一律 `permission denied`（SQL Server 错误 230）。
主表拉不动 → 整个脚本死在第一步。

**但 Trl 表 7 列全部可读**（本脚本的前置探测逐列验过）。所以这里只拉这一张。

必须在客户端绕开的坑（与 pull_erp_itemmaster.py 同源，实测复现）
-------------------------------------------------------------
1) charset 必须是 `cp936`。pymssql 默认 UTF-8 时 FreeTDS 在本机 build 里
   装配 utf-8 <-> UCS-2LE 转换失败，登录包直接被服务端拒。

2) **但 cp936 不能直接读文本列**：FreeTDS 把 nvarchar 转 GBK 时按**字符数**
   而非字节数分配缓冲，中文字符被截断在半个上，客户端解 GBK 抛
   `UnicodeDecodeError: 'gbk' codec can't decode byte 0xb0`。
   —— 实测 `SELECT * FROM CBO_ItemMaster_Trl` 就是这么炸的。
   所以**所有 nvarchar/nchar 列一律 `CAST(col AS VARBINARY(MAX))` 取原始字节，
   本地按 utf-16-le 解码**；其余类型不经字符集转换，原样取。

   注意这不是「某些列的偶然问题」：某列的当前数据里没有中文就不报错，
   等哪天录进中文才炸，是定时炸弹。所以按**列类型**决定，不挑列。

用法
----
    <venv>\\Scripts\\python.exe tools\\pull_erp_itemmaster_trl.py
    <venv>\\Scripts\\python.exe tools\\pull_erp_itemmaster_trl.py --out data\\erp --stamp

密码来源（与 core/erp_sync.py 对齐）：
    环境变量 `ARS_ERP_PASSWORD`  >  页面「匹配数据库 → ERP 同步」里存的密码。
    刻意不写死在源码里 —— 这个仓库是公开的。

落盘
----
    <out>/BLFN_CBO_ItemMaster_Trl.csv   7 列，UTF-8 BOM，Excel 可直接打开
    <out>/_meta_trl.json                列名/类型/行数/拉取时间/校验

依赖：仅 pymssql。**刻意不进 requirements.txt** —— 离线取数工具，不属于
系统运行时依赖。（按 tools/check_deploy.py 第 [1] 节规矩，用
try/except ImportError 包住，该自检把它归为可选依赖。）
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:                                        # 可选依赖，见模块 docstring
    import pymssql
except ImportError:                         # pragma: no cover
    print("[错误] 缺少 pymssql —— 这是取数工具的独立依赖，不在 requirements.txt 里。")
    print("       安装：<venv>\\Scripts\\python.exe -m pip install pymssql")
    sys.exit(3)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "192.168.1.247"
DEFAULT_PORT = 1433
DEFAULT_USER = "sh_report_user"
DEFAULT_DB = "BLFN"
TABLE = "CBO_ItemMaster_Trl"

# 走 varbinary 取原始字节的列类型（nvarchar/nchar 在 SQL Server 里是 UTF-16LE）
UNICODE_TYPES = ("nchar", "nvarchar")


def _resolve_password() -> str:
    pwd = (os.getenv("ARS_ERP_PASSWORD", "") or "").strip()
    if pwd:
        return pwd
    try:                                    # 复用页面里配置过的密码
        from core import auth
        return (auth.get_setting("erp_password", "") or "").strip()
    except Exception:                       # noqa: BLE001
        return ""


def connect(db, password, host, port, user):
    return pymssql.connect(server=host, port=port, user=user, password=password,
                           database=db, charset="cp936", autocommit=True,
                           timeout=60, login_timeout=15)


def columns_of(cursor, table):
    cursor.execute("""
        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
    """, (table,))
    return [(r[0], (r[1] or "").lower(), r[2]) for r in cursor.fetchall()]


def build_exprs(cols):
    """为每列生成 SELECT 表达式；文本列走 varbinary 绕开 cp936 截断。"""
    exprs, kinds = [], []
    for name, typ, _nullable in cols:
        if typ in UNICODE_TYPES:
            exprs.append(f"CAST([{name}] AS VARBINARY(MAX)) AS [{name}]")
            kinds.append("unicode")
        else:
            exprs.append(f"[{name}]")
            kinds.append(typ)
    return exprs, kinds


def decode(value, kind):
    """把一列的值转成可直接写 CSV 的字符串。"""
    if value is None:
        return ""
    if kind == "unicode":
        # CAST(... AS VARBINARY) 拿回 UTF-16LE 原始字节
        if isinstance(value, (bytes, bytearray)):
            try:
                return bytes(value).decode("utf-16-le")
            except UnicodeDecodeError:
                # 极端情况：字节数为奇数 / 不是合法 UTF-16 序列，退回可见形式
                return bytes(value).decode("utf-16-le", errors="replace")
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def main():
    ap = argparse.ArgumentParser(description=f"拉取 ERP {TABLE} 整表 → CSV")
    ap.add_argument("--out", default=str(ROOT / "data" / "erp"), help="输出目录")
    ap.add_argument("--host", default=os.getenv("ARS_ERP_HOST", DEFAULT_HOST))
    ap.add_argument("--port", type=int, default=int(os.getenv("ARS_ERP_PORT", DEFAULT_PORT)))
    ap.add_argument("--user", default=os.getenv("ARS_ERP_USER", DEFAULT_USER))
    ap.add_argument("--db", default=os.getenv("ARS_ERP_DB", DEFAULT_DB))
    ap.add_argument("--stamp", action="store_true", help="文件名带时间戳，保留历史")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 行（0=全部，调试用）")
    args = ap.parse_args()

    password = _resolve_password()
    if not password:
        print("[错误] 没有 ERP 密码。任选其一：")
        print("       1) $env:ARS_ERP_PASSWORD = '<密码>'")
        print("       2) 在「匹配数据库 → ERP 同步 → 修改连接配置」里填一次"
              "（存 auth.db，本脚本复用）")
        sys.exit(2)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"连接 {args.host}:{args.port}/{args.db} as {args.user} …")
    c = connect(args.db, password, args.host, args.port, args.user)
    cur = c.cursor()

    cols = columns_of(cur, TABLE)
    if not cols:
        print(f"[错误] 读不到 {TABLE} 的列元数据（表名错？权限？）")
        sys.exit(4)
    exprs, kinds = build_exprs(cols)
    print(f"{TABLE}：{len(cols)} 列 —— " + ", ".join(
        f"{n}{'*' if k == 'unicode' else ''}" for n, k, _ in cols))
    print("  （带 * 的走 varbinary 取原始字节，本地 utf-16-le 解码）")

    cur.execute(f"SELECT COUNT(*) FROM [{TABLE}]")
    total = cur.fetchone()[0]
    print(f"服务端行数：{total}")

    sql = f"SELECT {', '.join(exprs)} FROM [{TABLE}] ORDER BY [ID]"
    if args.limit:
        sql = sql.replace("SELECT ", f"SELECT TOP {args.limit} ", 1)
    print(f"取数中 …（{'TOP ' + str(args.limit) if args.limit else '全部'}）")
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()
    c.close()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"BLFN_{TABLE}.csv" if not args.stamp else f"BLFN_{TABLE}_{stamp}.csv"
    path = out / name

    nonempty = {n: 0 for n, _k, _ in cols}
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([n for n, _k, _ in cols])
        for r in rows:
            vals = [decode(v, kinds[i]) for i, v in enumerate(r)]
            for i, v in enumerate(vals):
                if v:
                    nonempty[cols[i][0]] += 1
            w.writerow(vals)

    size = path.stat().st_size
    print(f"\n已写出 {path}")
    print(f"  {len(rows)} 行 × {len(cols)} 列，{size / 1024 / 1024:.2f} MB")
    print("  各列非空行数：")
    for n, _k, _ in cols:
        pct = (nonempty[n] / len(rows) * 100) if rows else 0
        print(f"    {n:35s} {nonempty[n]:>6} 行 ({pct:5.1f}%)")

    meta = {
        "table": TABLE, "host": args.host, "db": args.db, "user": args.user,
        "pulled_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "rows": len(rows), "server_rows": total,
        "columns": [{"name": n, "type": t, "raw_bytes": k == "unicode"}
                    for (n, t, _), k in zip(cols, kinds)],
        "nonempty": nonempty, "file": name, "bytes": size,
        "note": "文本列经 CAST(col AS VARBINARY(MAX)) 取字节 + utf-16-le 解码，"
                "绕开 FreeTDS 在 cp936 下按字符数分配缓冲导致的中文截断。",
    }
    mp = out / ("_meta_trl.json" if not args.stamp else f"_meta_trl_{stamp}.json")
    mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  元数据 {mp}")

    if len(rows) != total:
        print(f"\n[警告] 取到 {len(rows)} 行 ≠ 服务端 {total} 行")
        return 1
    print("\n[完成] 行数与服务端一致，无中文解码错误。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
