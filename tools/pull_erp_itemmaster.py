"""拉取 ERP（用友 U9 基础档案 CBO_ItemMaster 系列）物料主档到本地 CSV

> ## ⛔ 2026-09-21 起，本脚本**已经跑不通了** —— 别白试
>
> `sh_report_user` 的列级权限被收紧到**只剩三列**（实测
> `sys.database_permissions` 逐列核对）：
>
>     GRANT SELECT ON CBO_ItemMaster.Code / Code1 / SPECS
>
> 其余 266 列全部 `permission denied`，**包括本脚本要用的 `ID`**
> （读列元数据时会碰 `ID`，`sys.columns` 那步就挂；`COUNT(*)` 也会因碰 `ID` 失败）。
>
> 所以：
> * 要**刷新匹配库** → 用页面「匹配数据库 → ERP 同步」或
>   `core/erp_sync.py`（只取那三列，够用）；
> * 要**重新拿全量快照** → 得先让 ERP 管理员放开列权限，再回来跑本脚本；
> * 已有快照 `data/erp/`（当轮权限还全开时拉的，269 列且有值）仍可用于
>   离线拼装，见 `tools/export_itemmaster_csv.py`。
>
> 密码也**不再写在本文件里**（本仓库是公开的）：从环境变量
> `ARS_ERP_PASSWORD` 或页面配置读取，见 `_resolve_password()`。

用法：
    python tools/pull_erp_itemmaster.py                 # 默认拉到 data/erp/
    python tools/pull_erp_itemmaster.py --out-dir=D:\\x
    python tools/pull_erp_itemmaster.py --stamp         # 文件名带时间戳（不覆盖）
    python tools/pull_erp_itemmaster.py --limit=200     # 只拉前 200 行（自测用）

数据源
------
    192.168.1.247:1433  Microsoft SQL Server 2019
    库      BLFN        （实例排序规则 Chinese_PRC_CI_AS）
    账号    sh_report_user —— 该账号**只能访问 BLFN**，
            同实例的 0905 / BLFNWDK / BLFNWDK11111 / Btest / U9toQC 等库 HAS_DBACCESS=0。
    表      CBO_ItemMaster      13179 行 × 269 列
            CBO_ItemMaster_Trl  13179 行 ×   7 列（SysMLFlag 全为 zh-CN）

    **这是用友 U9 的基础档案，不是 SAP B1、也不是金蝶。** 判据：
    `ItemSource` 列全表为 `'U9'`；同实例存在 `U9toQC` 库；
    表结构是 U9 特有的 `CBO_` + `Segment1..30` / `NameSegment1..30` +
    `DescFlexField_*` 自定义特征字段体系。

    字段落点（拉完实测得出，供后续映射参考）：
      Code      料号（唯一）          如 10001-0001
      Name      品名                  如 低温型风速传感器
      SPECS     规格                  如 51177.67.773C
      Code1     **型号**（8055 种）    如 BLF1-S / BLF1-SIII
      Code2     图号 / 物料组编号      如 012203300
      Segment1  料号前缀大类（119 种） 如 10001
      NameSegment1 品名（同 Name）
      MainItemCategory / StockCategory  分类内码（119 种），需另一张分类表翻译
      State     0 / 2（启用 / 停用）；Status 全空
      Picture   varbinary(max)，全表仅 2 行非空

必须在客户端绕开的两个坑（否则必挂）
-------------------------------------
1) **字符集**：pymssql 默认 charset=UTF-8 时，FreeTDS 在本机 build 里装配
   utf-8 <-> UCS-2LE 转换失败，登录包直接被服务端拒绝，报
   「Error converting characters into server's character set」。
   实测唯一可用值是 `cp936`。

2) **但 cp936 不能用来读文本列**：FreeTDS 把 nvarchar 转 GBK 时按**字符数**
   而非字节数分配缓冲，中文字符被截断在半个上，客户端解 GBK 直接抛
   `'gbk' codec can't decode byte 0xb0`。实测必挂 10 列 ——
   主表 Name / SPECS / Code1 / NameSegment1 / DescFlexField_PrivateDescSeg8,9,17，
   Trl 表 Description / NameCombineName / DescFlexField_CombineName。
   **注意这不是「少数列的偶然问题」**：某列当前数据里没有中文就不会报错，
   等哪天录进中文才炸，属于典型的定时炸弹。所以这里不挑列
   —— 所有文本列一律 `CAST(col AS VARBINARY(MAX))` 取原始字节，
   本地按 utf-16-le 解码。非文本列（bigint/bit/decimal/int/datetime/
   uniqueidentifier/varbinary）不经字符集转换，原样取。

落盘
----
    <out-dir>/BLFN_CBO_ItemMaster.csv        269 列，UTF-8 BOM，Excel 可直接打开
    <out-dir>/BLFN_CBO_ItemMaster_Trl.csv      7 列
    <out-dir>/blob/<列名>_<ID>.<ext>          二进制列（Picture）落成独立文件
    <out-dir>/_meta.json                     列名/类型/行数/拉取时间/校验结果

    约定：
    * NULL 与空字符串在 CSV 里都写成空字段（CSV 无类型，无法区分）；
    * datetime 统一格式化为 `YYYY-MM-DD HH:MM:SS`；
    * bit 写 0/1；
    * `Picture` 是 varbinary(max)（整表仅 2 行非空，691KB / 255KB），
      **不写进 CSV**，落到 blob/ 子目录，CSV 里只留相对路径 ——
      单个字段几十万字符会撞穿 csv 模块默认的 field_size_limit=131072，
      也会把 Excel / pandas 一起拖死；
    * 文件名默认固定（覆盖式刷新，同时清理上一轮的 blob 残留），
      加 --stamp 则带时间戳保留历史（此时不动已有 blob）。

依赖
----
    仅 pymssql（本机默认 venv 已装）。**刻意不进 requirements.txt** ——
    这是离线取数工具，不属于系统运行时依赖，写进去只会让部署多一个必然失败的坑。

    按 tools/check_deploy.py 第 [1] 节的规矩，本文件用 try/except ImportError 包住
    pymssql：该自检把「异常处理里能降级」的 import 归为可选依赖，
    只列不报错。少了这个 try，自检会本仓自己判成「requirements 漏包」并失败。
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:                                   # 可选依赖，见模块 docstring「依赖」一节
    import pymssql
except ImportError:                    # pragma: no cover
    print("[错误] 缺少 pymssql —— 这是取数工具的独立依赖，不在 requirements.txt 里。")
    print("       安装：<venv>\\Scripts\\python.exe -m pip install pymssql")
    sys.exit(3)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_HOST = "192.168.1.247"
DEFAULT_PORT = 1433
DEFAULT_USER = "sh_report_user"
# ⚠️ **密码不写在这里**。这个仓库是公开的，把生产 ERP 的库口令写进源码
# 等于连同账号一起公开。取值优先级与 core/erp_sync.py 对齐：
#   环境变量 ARS_ERP_PASSWORD  >  页面「匹配数据库 → ERP 同步」里配置的密码
def _resolve_password() -> str:
    pwd = (os.getenv("ARS_ERP_PASSWORD", "") or "").strip()
    if pwd:
        return pwd
    try:                       # 读页面配置（auth.db.setting）——离线取数也能复用
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from core import auth
        return (auth.get_setting("erp_password", "") or "").strip()
    except Exception:                                   # noqa: BLE001
        return ""


DEFAULT_DB = "BLFN"
TABLES = ("CBO_ItemMaster", "CBO_ItemMaster_Trl")

# 走 varbinary 取原始字节的列类型（nvarchar/nchar 是 utf-16le，其余按库排序规则）
UNICODE_TYPES = ("nchar", "nvarchar")
BYTE_TYPES = ("varchar", "char")
VBIN_TYPES = ("varbinary", "binary", "image")

BATCH = 2000


def _connect(db: str):
    """按实测可用的参数连接（charset 必须是 cp936，见模块 docstring）。

    直接开 autocommit：pymssql 默认 autocommit=False，SELECT 会挂在隐式事务里
    直到 commit；对着生产 ERP 拉全表不该长时间占锁。
    """
    password = _resolve_password()
    if not password:
        print("[错误] 没有 ERP 密码。任选其一：")
        print("       1) set ARS_ERP_PASSWORD=<密码>")
        print("       2) 在「匹配数据库 → ERP 同步 → 修改连接配置」里填一次"
              "（存 auth.db，本脚本会复用）")
        sys.exit(3)
    conn = pymssql.connect(
        server=os.getenv("ERP_HOST", DEFAULT_HOST),
        port=int(os.getenv("ERP_PORT", DEFAULT_PORT)),
        user=os.getenv("ERP_USER", DEFAULT_USER),
        password=password,
        database=db,
        charset="cp936",
        tds_version="7.3",
        timeout=300,
        login_timeout=15,
    )
    conn.autocommit(True)
    return conn


def _columns(cur, table: str):
    """读取列元数据，保持表内原始顺序。"""
    cur.execute("""
        SELECT c.name, ty.name, c.max_length, c.is_nullable
        FROM sys.columns c
        JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        WHERE c.object_id = OBJECT_ID('dbo.' + %s)
        ORDER BY c.column_id
    """, (table,))
    return [{"name": n, "type": t, "max_length": ln, "nullable": bool(nul)}
            for n, t, ln, nul in cur.fetchall()]


def _select_list(cols) -> str:
    """文本列转 varbinary 绕过 FreeTDS 的字符集转换，其余原样。"""
    parts = []
    for c in cols:
        n, t = c["name"], c["type"]
        if t in UNICODE_TYPES or t in BYTE_TYPES:
            parts.append(f"CAST([{n}] AS VARBINARY(MAX)) AS [{n}]")
        else:
            parts.append(f"[{n}]")
    return ", ".join(parts)


def _sniff_ext(data: bytes) -> str:
    """按魔数猜扩展名，猜不出就 .bin。"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:2] == b"BM":
        return ".bmp"
    if data[:4] == b"%PDF":
        return ".pdf"
    if data[:4] == b"PK\x03\x04":
        return ".zip"
    return ".bin"


def _decode(value, col, warn: dict, bin_dir: Path | None = None, row_id=None) -> str:
    """把一行里的单个值转成写入 CSV 的字符串。

    二进制列（varbinary/image）不塞进 CSV —— 单个字段几十万字符，
    csv 模块默认 `field_size_limit = 131072` 会直接抛
    `_csv.Error: field larger than field limit`，Excel 和 pandas 同样打不开。
    实测本表 Picture 列有 2 行带图（691KB / 255KB），塞进去整份 CSV 就废了。
    因此改为落到 <out-dir>/blob/ 下的独立文件，CSV 里只存相对路径。
    """
    if value is None:
        return ""
    t = col["type"]
    name = col["name"]
    if t in UNICODE_TYPES:
        if len(value) % 2:                       # 奇数字节：utf-16 不可能合法
            warn.setdefault("odd_bytes", []).append(name)
            value = value[:-1]
        text = value.decode("utf-16-le", errors="replace")
    elif t in BYTE_TYPES:
        text = value.decode("cp936", errors="replace")
        if "\ufffd" in text:
            warn.setdefault("gbk_lossy", []).append(name)
    elif t in VBIN_TYPES:
        if bin_dir is None:
            return ""
        bin_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{name}_{row_id}{_sniff_ext(value)}"
        (bin_dir / fname).write_bytes(value)
        warn.setdefault("binary_cols", set()).add(name)
        return f"{bin_dir.name}/{fname}"
    elif t == "bit":
        return "1" if value else "0"
    elif t == "datetime":
        return value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else str(value)
    elif t == "uniqueidentifier":
        return str(value)
    else:                                        # int / bigint / decimal ...
        return str(value)

    if "\x00" in text:
        warn.setdefault("nul_char", []).append(name)
    return text


def _fetch_table(conn, table: str, limit, out_dir: Path, stamp: str | None):
    """流式拉取一张表 → 一个 CSV。

    * 用 pymssql 的**游标迭代**（结果集按需从协议里取），不整套 load 进内存：
      13k 行 × 269 列整块 load 大约要几百 MB，流式则只有单批的开销。
    * 排序用 `ORDER BY Code`（料号），不用 `ORDER BY ID` ——
      ID 是雪花号，排出来跟业务无关，CSV 给人看得按料号走。
      顺序只影响可读性，完整性由「写入行数 == 远端 COUNT(*)」兜底。
    * 连接开 autocommit：默认 autocommit=False 时 SELECT 会挂在隐式事务里，
      对着生产 ERP 拉全表不该长时间占锁。
    """
    cur = conn.cursor()
    cols = _columns(cur, table)
    if not cols:
        raise SystemExit(f"[错误] 库中不存在 dbo.{table}")
    names = [c["name"] for c in cols]
    if "ID" not in names:
        raise SystemExit(f"[错误] {table} 没有 ID 列，无法校验唯一性")

    cur.execute(f"SELECT COUNT(*) FROM dbo.[{table}]")
    remote_total = cur.fetchone()[0]

    body = _select_list(cols)
    top = f"TOP {limit} " if limit else ""
    order = "ORDER BY Code" if "Code" in names else "ORDER BY ID"

    suffix = f"_{stamp}" if stamp else ""
    path = out_dir / f"{DEFAULT_DB}_{table}{suffix}.csv"

    warn: dict = {}
    written = 0
    nulls = 0
    seen_ids: set = set()
    dup_ids = 0
    # 截断交叉校验：远端按字符数算的长度，与本地解码后的长度必须一致。
    # 这是专门用来钉死 FreeTDS 截断坑的 —— 该坑在 cp936 直读时表现为报错，
    # 但换成 varbinary 后如果以后再有人改回直读、或 nvarchar 长度溢出，
    # 症状会从「报错」退化成「静默少字」，只有长度对比抓得住。
    watch = [n for n in ("Name", "SPECS", "Description", "NameCombineName",
                         "Code", "SearchCode") if n in names]
    cur.execute("SELECT " + ", ".join(f"MAX(LEN([{n}]))" for n in watch or ["ID"])
                + f" FROM dbo.[{table}]")
    remote_len = dict(zip(watch or ["ID"], cur.fetchone()))
    local_len = {n: 0 for n in watch}

    conn.autocommit(True)          # 幂等：_connect 已开，这里只是防止被改回去
    cur.execute(f"SELECT {top}{body} FROM dbo.[{table}] {order}")
    id_idx = names.index("ID")
    watch_idx = [(n, names.index(n)) for n in watch]
    bin_dir = out_dir / "blob"

    # 覆盖式刷新：二进制列是派生出来的文件（blob/<列名>_<ID>.<ext>），
    # 上一轮的残留必须先清掉 —— 否则某条记录的图片在 ERP 里被删了，
    # 本地还留着一个孤儿文件，看着像还在。
    # 只删本表二进制列前缀的文件，不碰目录里其它东西。
    bin_cols = [c["name"] for c in cols if c["type"] in VBIN_TYPES]
    if bin_dir.is_dir() and not stamp:
        for col_name in bin_cols:
            for stale in bin_dir.glob(f"{col_name}_*"):
                if stale.is_file():
                    stale.unlink()

    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(names)
        try:
            for raw in cur:                    # 流式：边收边写
                rid = raw[id_idx]
                row = []
                for col, value in zip(cols, raw):
                    if value is None:
                        nulls += 1
                        row.append("")
                    else:
                        row.append(_decode(value, col, warn,
                                           bin_dir=bin_dir, row_id=rid))
                writer.writerow(row)
                written += 1
                if rid in seen_ids:
                    dup_ids += 1
                seen_ids.add(rid)
                for n, i in watch_idx:
                    v = row[i]
                    if len(v) > local_len[n]:
                        local_len[n] = len(v)
                if written % BATCH == 0:
                    print(f"    {table}: {written} 行…", flush=True)
        except Exception:
            fh.close()
            path.unlink(missing_ok=True)       # 半截文件比没有文件更危险
            raise

    expected = remote_total if not limit else min(limit, remote_total)
    # 长度校验拿远端全表 MAX(LEN()) 对本地实际最长值。
    # --limit 模式下本地只看到前 N 行，最长的可能没被选中，比对不成立，故跳过（ok=None）。
    len_check = {n: {"remote_max_chars": remote_len[n],
                     "local_max_chars": local_len[n],
                     "ok": None if limit else remote_len[n] == local_len[n]}
                 for n in watch}
    return {
        "table": table,
        "file": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
        "bytes": path.stat().st_size,
        "columns": len(cols),
        "column_defs": cols,
        "order_by": order.replace("ORDER BY ", ""),
        "rows_remote": remote_total,
        "rows_expected": expected,
        "rows_written": written,
        "rows_match": written == expected,
        "null_cells": nulls,
        "unique_ids": len(seen_ids),
        "id_duplicates": dup_ids,
        "length_check": len_check,
        "binary_columns": bin_cols,
        "blob_files": sorted(p.name for p in bin_dir.glob("*")) if bin_dir.is_dir() else [],
        "warnings": {k: (sorted(v) if isinstance(v, (set, list)) else v)
                     for k, v in warn.items()},
    }


def main() -> None:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "erp"))
    ap.add_argument("--stamp", action="store_true", help="文件名带时间戳，不覆盖历史")
    ap.add_argument("--limit", type=int, default=None, help="只拉前 N 行（自测用）")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") if args.stamp else None

    host = os.getenv("ERP_HOST", DEFAULT_HOST)
    print(f"数据源 {host} 库={DEFAULT_DB} 账号={os.getenv('ERP_USER', DEFAULT_USER)}")
    print(f"输出目录 {out_dir}\n")

    conn = _connect(DEFAULT_DB)
    results = []
    for table in TABLES:
        print(f"[拉取] {table}")
        info = _fetch_table(conn, table, args.limit, out_dir, stamp)
        size_mb = info["bytes"] / 1024 / 1024
        print(f"    -> {info['file']}  {info['rows_written']} 行 × "
              f"{info['columns']} 列  {size_mb:.2f} MB  "
              f"{'行数一致' if info['rows_match'] else '**行数不一致**'}")
        bad_len = [n for n, v in info["length_check"].items() if v["ok"] is False]
        if bad_len:
            print(f"    **长度校验不符**: {bad_len}")
        elif args.limit:
            print("    长度校验跳过（--limit 模式只比对前 N 行，远端最大值不可比）")
        if info["warnings"]:
            print(f"    告警: {info['warnings']}")
        results.append(info)
    # 覆盖式刷新的提示写在拉完之后：文件是当场覆盖的，说在前面等于没说
    if not stamp:
        print("    （文件名固定，旧快照已覆盖；需保留历史请加 --stamp）")
    conn.close()

    meta = {
        "source": {
            "host": host,
            "port": int(os.getenv("ERP_PORT", DEFAULT_PORT)),
            "database": DEFAULT_DB,
            "user": os.getenv("ERP_USER", DEFAULT_USER),
            "product": "Microsoft SQL Server 2019",
            "note": "账号仅可访问 BLFN；其余同实例库 HAS_DBACCESS=0",
        },
        "pulled_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "client": {"driver": "pymssql", "charset": "cp936", "tds_version": "7.3",
                   "text_columns": "CAST AS VARBINARY(MAX) + 本地 utf-16-le 解码"},
        "limit": args.limit,
        "tables": results,
    }
    meta_path = out_dir / "_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n元数据 -> {meta_path}")

    bad = [r["table"] for r in results
           if not r["rows_match"]
           or any(v["ok"] is False for v in r["length_check"].values())]
    if bad:
        print(f"\n[失败] 校验未通过: {', '.join(bad)}")
        raise SystemExit(2)
    print("\n[完成] 两张表均已按远端行数落地，关键字段长度校验通过")


if __name__ == "__main__":
    main()
