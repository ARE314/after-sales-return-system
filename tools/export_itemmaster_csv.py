"""把 CBO_ItemMaster 导成真正的 CSV（离线快照 + 实时 Trl 描述）。

> ## ⛔ 2026-09-22 起**已过时**，别再用
>
> 这个工具存在的唯一理由，是「用户给的那条 11 列 SQL 在 sh_report_user 下
> 因列级权限跑不通」（下面有原始记录）。用户已决定**去掉快照、全部走实时**，
> 对应的 `core/erp_sync.py` 已改版：`fetch_trl` / `SNAPSHOT_DIR` /
> `SNAPSHOT_COLUMNS` / `_load_snapshot` / `merge_extras` **全部删除**，
> 本脚本引用的 `erp_sync.fetch_trl` 已经不存在 —— 跑它会 AttributeError。
>
> 等 ERP 管理员按 `core/erp_sync.py` 模块 docstring 里的 GRANT 语句放开那 8 列后：
> * 要刷匹配库 → 页面「匹配数据库 → ERP 同步」（或 `core.erp_sync.sync()`）；
> * 要一份 CSV → 直接跑用户给的那条 SQL，或用 `core.erp_sync.fetch_items()`。
>
> 快照文件（`data/erp/`）仍在本地，仅供回溯，不再参与同步。

为什么不能照原样实时拉
----------------------
用户给的那条

    SELECT a.DescFlexField_PrivateDescSeg5 旧料号, a.Code 料号, a.Name 品名,
           a.Code1 型号, a.SPECS 规格, b.Description 描述,
           a.DescFlexField_PrivateDescSeg4 客户, a.DescFlexField_PrivateDescSeg3 生产统计,
           a.DescFlexField_PrivateDescSeg8 标参1, a.DescFlexField_PrivateDescSeg9 标参2,
           a.DescFlexField_PrivateDescSeg10 标参3
      FROM CBO_ItemMaster a
      LEFT JOIN CBO_ItemMaster_Trl b ON a.ID = b.ID

连同 `SELECT * FROM CBO_ItemMaster`、`SELECT COUNT(*) FROM CBO_ItemMaster`，
在 `sh_report_user` 下**全部** `(230, b"The SELECT permission was denied on the
column 'ID' of the object 'CBO_ItemMaster', database 'BLFN', schema 'dbo'.")` ——
该账号在这张表上只有 `Code`/`Code1`/`SPECS` 三列的列级 SELECT
（2026-09-22 实测 `HAS_PERMS_BY_NAME('CBO_ItemMaster','OBJECT','SELECT',<列>,'COLUMN')`
对 ID/Name/Seg3/4/5/8/9/10 全 = 0）。

所以整表导出只能走离线快照：`data/erp/BLFN_CBO_ItemMaster.csv`（**实际是 xlsx**，
2026-09-21 冻结，268 列 × 13179 行；活表 269 列，只多一个 `CancelApproveOn`）。
`描述` 这一列快照里没有，改从**实时**多语言表 `CBO_ItemMaster_Trl` 取
（该表整表可读，用 ID 关联）。

输出
----
    data/exports/CBO_ItemMaster_full.csv   整表：268 列 × 13179 行
    data/exports/物料主档_11列.csv          上面那条 JOIN 的 11 列投影

两份都是真正的 CSV（`utf-8-sig`，Excel 直接双击不乱码）。

用法
----
    python tools/export_itemmaster_csv.py                # 两份都导
    python tools/export_itemmaster_csv.py --full         # 只导整表
    python tools/export_itemmaster_csv.py --join         # 只导 11 列投影
    python tools/export_itemmaster_csv.py --no-live      # 不连 ERP（描述留空）
    python tools/export_itemmaster_csv.py --snapshot <路径>
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_SNAPSHOT = os.path.join(ROOT, "data", "erp", "BLFN_CBO_ItemMaster.csv")
EXPORT_DIR = os.path.join(ROOT, "data", "exports")

FULL_NAME = "CBO_ItemMaster_full.csv"
JOIN_NAME = "物料主档_11列.csv"

#: 用户那条 JOIN 的列序 → 快照里的来源列（None = 实时多语言表的 `Description`）
JOIN_SPEC = [
    ("旧料号", "DescFlexField_PrivateDescSeg5"),
    ("料号", "Code"),
    ("品名", "Name"),
    ("型号", "Code1"),
    ("规格", "SPECS"),
    ("描述", None),
    ("客户", "DescFlexField_PrivateDescSeg4"),
    ("生产统计", "DescFlexField_PrivateDescSeg3"),
    ("标参1", "DescFlexField_PrivateDescSeg8"),
    ("标参2", "DescFlexField_PrivateDescSeg9"),
    ("标参3", "DescFlexField_PrivateDescSeg10"),
]


def _id_key(value):
    """快照里的 ID 与 Trl 的 ID 归一成同一个键（能转 int 就转）。"""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        text = str(value).strip()
        return text or None


def _cell(value):
    """CSV 单元格：None → 空串，其余原样（数字保持数字，逗号/换行交给 csv 模块加引号）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value


def iter_snapshot(path):
    """读快照（xlsx 包在 .csv 后缀里）→ `(header, row_iterator)`。

    `openpyxl` 见到 `.csv` 后缀会直接抛 `InvalidFileException`，必须喂 `BytesIO`。
    """
    import openpyxl

    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] not in (b"PK", b"\x50\x4b"):
        raise SystemExit(
            f"[!] {path} 不是 xlsx（magic={raw[:4]!r}）。真 CSV 的快照不需要本工具转换。"
        )
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = [(_cell(v) if v is not None else "") for v in next(rows)]
    return wb, header, rows


def load_trl():
    """实时取多语言表 `{item_id: {'product_name','description'}}`；失败返回 `(None, 原因)`。"""
    try:
        from core import erp_sync
    except Exception as exc:  # pragma: no cover - 只在环境异常时触发
        return None, f"导入 core.erp_sync 失败：{exc}"
    try:
        cfg = erp_sync.get_config(redact=False)
    except Exception as exc:
        return None, f"读 ERP 连接配置失败：{exc}"
    if not cfg.get("password"):
        return None, "ERP 连接密码未配置"
    t0 = time.time()
    try:
        trl = erp_sync.fetch_trl(cfg)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    print(f"[trl] 实时多语言表 {len(trl)} 条，用时 {time.time() - t0:.1f}s")
    return trl, ""


def export(snapshot, want_full, want_join, live):
    if not os.path.isfile(snapshot):
        raise SystemExit(f"[!] 找不到快照文件：{snapshot}")
    os.makedirs(EXPORT_DIR, exist_ok=True)

    trl, trl_err = (load_trl() if (live and want_join) else ({}, "已按 --no-live 跳过"))
    if want_join and trl_err:
        print(f"[!] 描述列取不到实时值（{trl_err}），该列留空")

    wb, header, rows = iter_snapshot(snapshot)
    print(f"[src] {os.path.basename(snapshot)} · {len(header)} 列")
    idx = {name: i for i, name in enumerate(header)}
    missing = [src for _, src in JOIN_SPEC if src and src not in idx]
    if missing:
        print(f"[!] 快照里缺列（将留空）：{missing}")
    id_col = idx.get("ID")

    full_path = os.path.join(EXPORT_DIR, FULL_NAME)
    join_path = os.path.join(EXPORT_DIR, JOIN_NAME)
    fh_full = fh_join = None
    w_full = w_join = None
    if want_full:
        fh_full = open(full_path, "w", newline="", encoding="utf-8-sig")
        w_full = csv.writer(fh_full)
        w_full.writerow(header)
    if want_join:
        fh_join = open(join_path, "w", newline="", encoding="utf-8-sig")
        w_join = csv.writer(fh_join)
        w_join.writerow([label for label, _ in JOIN_SPEC])

    n = 0
    hit = desc_filled = 0
    t0 = time.time()
    try:
        for row in rows:
            n += 1
            if w_full is not None:
                w_full.writerow([_cell(v) for v in row])
            if w_join is not None:
                key = _id_key(row[id_col]) if id_col is not None else None
                trl_row = trl.get(key) if (trl and key is not None) else None
                if trl_row is not None:
                    hit += 1
                out = []
                for label, src in JOIN_SPEC:
                    if src is None:
                        desc = (trl_row or {}).get("description", "")
                        if desc:
                            desc_filled += 1
                        out.append(desc)
                    else:
                        i = idx.get(src)
                        out.append(_cell(row[i]) if i is not None else "")
                w_join.writerow(out)
            if n % 5000 == 0:
                print(f"    … {n} 行")
    finally:
        for fh in (fh_full, fh_join):
            if fh is not None:
                fh.close()
        try:
            wb.close()
        except Exception:
            pass

    print(f"[ok] {n} 行，用时 {time.time() - t0:.1f}s")
    if want_full:
        print(f"[out] {full_path} · {os.path.getsize(full_path):,} 字节 · {len(header)} 列 × {n} 行")
    if want_join:
        print(f"[out] {join_path} · {os.path.getsize(join_path):,} 字节 · {len(JOIN_SPEC)} 列 × {n} 行")
        if trl:
            print(f"[out] 描述列：ID 命中 {hit} 行，有值 {desc_filled} 行")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="把 CBO_ItemMaster 快照导成 CSV")
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT, help="离线快照路径（实为 xlsx）")
    ap.add_argument("--full", action="store_true", help="只导整表")
    ap.add_argument("--join", action="store_true", help="只导 11 列投影")
    ap.add_argument("--no-live", action="store_true", help="不连 ERP，描述列留空")
    args = ap.parse_args(argv)

    want_full = args.full or not args.join
    want_join = args.join or not args.full
    return export(args.snapshot, want_full, want_join, not args.no_live)


if __name__ == "__main__":
    raise SystemExit(main())
