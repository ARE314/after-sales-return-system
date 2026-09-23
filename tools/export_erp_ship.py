"""导出 ERP（用友 U9）出货明细 → CSV。

对应查询（用户 2026-09-21 提供）：SM_Ship ⟕ SM_ShipLine ⟕ SM_ShipDocType(_Trl)
⟕ CBO_ItemMaster ⟕ CBO_Customer(_Trl)，14 个字段。

为什么单独一个脚本
------------------
`sh_report_user` 是**列级授权**账号（`sys.database_permissions` 里逐列 GRANT）。
整表 `SELECT COUNT(*)` 会失败（错误 230），但这**不代表查询跑不通** ——
这条查询引用的列恰好全在授权清单内，且没有引用任何被拒的列。

两个必须绕开的坑（实测）
------------------------
1) **charset 必须是 cp936**（pymssql 默认 UTF-8 时 FreeTDS 登录包被服务端拒）。

2) **cp936 不能直接读 nvarchar**：FreeTDS 按字符数而非字节数分配缓冲，
   中文被截断在半个上，客户端解 GBK 抛 `UnicodeDecodeError`。
   → 所有文本列 `CAST(col AS VARBINARY(MAX))` 取原始字节 + 本地 `utf-16-le` 解码。

3) **SQL 里绝不写中文常量**（这一条是本脚本踩出来的）：
   `iif(a.Status=0,'草稿',…)` 在 cp936 连接下，字面量按 varchar 送达服务端后
   与库排序规则不匹配，返回的是乱码 `ò?o?×?`。
   → 只取原始 `a.Status` 整数，在**本地**映射成中文。

用法
----
    <venv>\\Scripts\\python.exe tools\\export_erp_ship.py
    <venv>\\Scripts\\python.exe tools\\export_erp_ship.py --since 2026-01-01
密码来源：环境变量 `ARS_ERP_PASSWORD` > 页面「匹配数据库 → ERP 同步」存的密码。

落盘
----
    data/exports/ERP出货明细.csv      14 列，UTF-8 BOM
    data/exports/_meta_ship.json      行数/时间范围/各列非空/校验

依赖：仅 pymssql。**刻意不进 requirements.txt**（离线取数工具，非运行时依赖）。
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
    print("[错误] 缺少 pymssql —— 取数工具的独立依赖，不在 requirements.txt 里。")
    print("       安装：<venv>\\Scripts\\python.exe -m pip install pymssql")
    sys.exit(3)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "192.168.1.247"
DEFAULT_PORT = 1433
DEFAULT_USER = "sh_report_user"
DEFAULT_DB = "BLFN"

# U9 SM_Ship.Status → 中文（**本地映射**，见 docstring 第 3 条）
STATUS_MAP = {0: "草稿", 1: "开立", 2: "核准中"}
STATUS_ELSE = "已核准"

# 输出列： (CSV 表头, SQL 别名, 是否文本列)
COLS = [
    ("日期",     "d_date",     False),
    ("状态",     "d_status",   False),   # 本地由 d_status_raw 生成
    ("单号",     "d_docno",    False),
    ("单据类型", "d_doctype",  True),
    ("料号",     "d_code",     False),
    ("料品名称", "d_itemname", True),
    ("规格",     "d_specs",    True),
    ("型号",     "d_model",    True),
    ("出货数量", "d_qty",      False),
    ("序列号",   "d_sn",       True),
    ("客户名称", "d_customer", True),
    ("联系人",   "d_contact",  True),
    ("承运单号", "d_carrier",  True),
    ("地址",     "d_addr",     True),
]


def _resolve_password() -> str:
    pwd = (os.getenv("ARS_ERP_PASSWORD", "") or "").strip()
    if pwd:
        return pwd
    try:
        from core import auth
        return (auth.get_setting("erp_password", "") or "").strip()
    except Exception:                       # noqa: BLE001
        return ""


def build_sql(since: str = "", until: str = "") -> str:
    """构造查询。文本列一律 CAST 成 varbinary，中文一律不写进 SQL。"""
    def cast(expr):
        return f"CAST({expr} AS VARBINARY(MAX))"

    where = []
    if since:
        where.append(f"a.BusinessDate >= '{since}'")
    if until:
        where.append(f"a.BusinessDate < DATEADD(DAY, 1, '{until}')")
    where_sql = ("where " + " and ".join(where)) if where else ""

    return f"""
select a.BusinessDate                                          as d_date,
       a.Status                                                as d_status_raw,
       a.DocNo                                                 as d_docno,
       {cast('d.Name')}                                        as d_doctype,
       b.ItemInfo_ItemCode                                     as d_code,
       {cast('b.ItemInfo_ItemName')}                           as d_itemname,
       {cast('e.SPECS')}                                       as d_specs,
       {cast('e.Code1')}                                       as d_model,
       b.ShipQtyInvAmount                                      as d_qty,
       {cast('b.DescFlexField_PrivateDescSeg6')}               as d_sn,
       {cast('g.Name')}                                        as d_customer,
       {cast('a.DescFlexField_PrivateDescSeg1')}               as d_contact,
       {cast('a.DescFlexField_PrivateDescSeg5')}               as d_carrier,
       {cast('a.DescFlexField_PrivateDescSeg2')}               as d_addr
from SM_Ship a
left join SM_ShipLine b        on a.ID = b.Ship
left join SM_ShipDocType c     on a.DocumentType = c.ID
left join SM_ShipDocType_Trl d on d.ID = c.ID
left join CBO_ItemMaster e     on e.Code = b.ItemInfo_ItemCode
left join CBO_Customer f       on f.ID = a.OrderBy_Customer
left join CBO_Customer_Trl g   on g.ID = f.ID
{where_sql}
"""


def cell(v, is_text):
    if v is None:
        return ""
    if is_text:
        if isinstance(v, (bytes, bytearray)):
            return bytes(v).decode("utf-16-le")     # CAST 回来的原始 UTF-16LE
        return str(v)
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def main():
    ap = argparse.ArgumentParser(description="导出 ERP U9 出货明细 → CSV")
    ap.add_argument("--out", default=str(ROOT / "data" / "exports"))
    ap.add_argument("--host", default=os.getenv("ARS_ERP_HOST", DEFAULT_HOST))
    ap.add_argument("--port", type=int, default=int(os.getenv("ARS_ERP_PORT", DEFAULT_PORT)))
    ap.add_argument("--user", default=os.getenv("ARS_ERP_USER", DEFAULT_USER))
    ap.add_argument("--db", default=os.getenv("ARS_ERP_DB", DEFAULT_DB))
    ap.add_argument("--since", default="", help="起始日期 YYYY-MM-DD（含）")
    ap.add_argument("--until", default="", help="结束日期 YYYY-MM-DD（含）")
    ap.add_argument("--stamp", action="store_true", help="文件名带时间戳")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 行（调试）")
    args = ap.parse_args()

    password = _resolve_password()
    if not password:
        print("[错误] 没有 ERP 密码。任选其一：")
        print("       1) $env:ARS_ERP_PASSWORD = '<密码>'")
        print("       2) 在「匹配数据库 → ERP 同步 → 修改连接配置」里填一次")
        sys.exit(2)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"连接 {args.host}:{args.port}/{args.db} as {args.user} …")
    c = pymssql.connect(server=args.host, port=args.port, user=args.user,
                        password=password, database=args.db, charset="cp936",
                        autocommit=True, timeout=300, login_timeout=15)
    cur = c.cursor()

    sql = build_sql(args.since, args.until)
    if args.limit:
        sql = sql.replace("select ", f"select top {args.limit} ", 1)

    print("查询中 …（89325 行量级，约十几秒）")
    t0 = datetime.now()
    cur.execute(sql)
    rows = cur.fetchall()
    names = [d[0] for d in cur.description]
    print(f"服务端返回 {len(rows)} 行 × {len(names)} 列，用时 {(datetime.now() - t0).seconds}s")
    cur.close()
    c.close()

    idx = {n: i for i, n in enumerate(names)}
    for need in ("d_date", "d_status_raw"):
        if need not in idx:
            print(f"[错误] 结果里没有 {need} 列，SQL 与解析不匹配")
            sys.exit(4)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "ERP出货明细.csv" if not args.stamp else f"ERP出货明细_{stamp}.csv"
    path = out / name

    nonempty = {hdr: 0 for hdr, _a, _t in COLS}
    dmin = dmax = None
    status_count = {}

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([hdr for hdr, _a, _t in COLS])
        for r in rows:
            vals = []
            for hdr, alias, is_text in COLS:
                if hdr == "状态":
                    raw = r[idx["d_status_raw"]]
                    v = STATUS_MAP.get(raw, STATUS_ELSE) if raw is not None else ""
                    status_count[v] = status_count.get(v, 0) + 1
                else:
                    v = cell(r[idx[alias]], is_text)
                if v:
                    nonempty[hdr] += 1
                vals.append(v)
            d = r[idx["d_date"]]
            if isinstance(d, datetime):
                dmin = d if dmin is None or d < dmin else dmin
                dmax = d if dmax is None or d > dmax else dmax
            w.writerow(vals)

    size = path.stat().st_size
    print(f"\n已写出 {path}")
    print(f"  {len(rows)} 行 × {len(COLS)} 列，{size / 1024 / 1024:.2f} MB")
    print(f"  日期范围 {dmin} → {dmax}")
    print(f"  状态分布 {status_count}")
    print("  各列非空行数：")
    for hdr, _a, _t in COLS:
        pct = (nonempty[hdr] / len(rows) * 100) if rows else 0
        print(f"    {hdr:10s} {nonempty[hdr]:>6} 行 ({pct:5.1f}%)")

    meta = {
        "query": "SM_Ship ⟕ SM_ShipLine ⟕ SM_ShipDocType(_Trl) ⟕ CBO_ItemMaster "
                 "⟕ CBO_Customer(_Trl)",
        "host": args.host, "db": args.db, "user": args.user,
        "since": args.since, "until": args.until,
        "pulled_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "rows": len(rows), "columns": [h for h, _a, _t in COLS],
        "date_min": dmin.strftime("%Y-%m-%d") if dmin else None,
        "date_max": dmax.strftime("%Y-%m-%d") if dmax else None,
        "status_counts": status_count, "nonempty": nonempty,
        "file": name, "bytes": size,
        "notes": [
            "文本列经 CAST(col AS VARBINARY(MAX)) + 本地 utf-16-le 解码，"
            "绕开 FreeTDS 在 cp936 下按字符数分配缓冲导致的中文截断。",
            "状态由 SM_Ship.Status 整数在本地映射，SQL 里不写中文字面量"
            "（cp936 连接下中文字面量会变成乱码）。",
            "表格为**出货行级**：一张出货单有 N 行物料就是 N 行。",
        ],
    }
    mp = out / ("_meta_ship.json" if not args.stamp else f"_meta_ship_{stamp}.json")
    mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  元数据 {mp}")
    print("\n[完成]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
