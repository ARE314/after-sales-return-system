"""导入物料主档（匹配数据库）

用法：
    python tools/import_item_master.py <Excel路径> [--replace | --fill]
                                      [--pad-old-no] [--dry-run] [--sheet=物料主档]

说明：
    · 默认按「料号」upsert：已有料号则覆盖其字段，缺失则新增，不影响其他记录；
    · 加 --replace 则先清空整表再导入（适合整表刷新）；
    · 加 --fill 只补空字段：本地已有值一律不动，导入值为空的也不写入。
      从 ERP 快照回填老数据时用这个 —— upsert 会把本地人工整理的字段覆盖掉。
      配 --dry-run 先空转，看清楚会改哪些字段再真跑。
    · 加 --pad-old-no 时，旧料号补一个前导 0（ERP 的 Seg5 是数值字段，丢了前导 0；
      本地已有的 1429 条里 1428 条正好等于「0 + ERP 值」，所以照本地风格补）。
    · 表头需含：旧料号 / 料号 / 品名 / 型号 / 规格 / 描述 / 客户 / 生产统计 / 标参1-3。
      缺失的列会被忽略，料号为空的行会被跳过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl  # noqa: E402

from config import ITEM_IMPORT_MAP, ITEM_LABELS  # noqa: E402
from core import repository as repo  # noqa: E402
from core.db import init_db  # noqa: E402
from core.erp_sync import pad_old_no as _pad_old_no_value  # noqa: E402

# Excel 表头 → 数据库列（与页面导入共用同一份映射，避免两处维护）
COLUMN_MAP = ITEM_IMPORT_MAP


def pad_old_no(rec: dict) -> None:
    """旧料号补前导 0（就地改 rec）。

    ERP 的 DescFlexField_PrivateDescSeg5 是数值字段，前导 0 会被吃掉：
    `012100100` 读出来是 `12100100`。本地那份是文本，带 0。
    只处理纯数字、长度 6~10、且本来不以 0 开头的值 —— 长度离谱的先不动，
    宁可留空等人看一眼，也不要写出个 11 位的怪料号。

    规则本身只有一份实现：`core.erp_sync.pad_old_no`（ERP 自动同步也用同一条）。
    这里只是把它套到整个 rec 上；两处各自实现过一次，改一处漏一处就会出
    「手工导入补了 0、自动同步没补」这种一半对一半错的数据。
    """
    v = (rec.get("old_material_no") or "").strip()
    if v:
        rec["old_material_no"] = _pad_old_no_value(v)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)

    path = args[0]
    if "--replace" in sys.argv and "--fill" in sys.argv:
        print("[错误] --replace 与 --fill 不能同时用")
        sys.exit(2)
    mode = "replace" if "--replace" in sys.argv else \
        ("fill" if "--fill" in sys.argv else "upsert")
    pad = "--pad-old-no" in sys.argv
    dry = "--dry-run" in sys.argv
    sheet = "物料主档"
    for a in sys.argv:
        if a.startswith("--sheet="):
            sheet = a.split("=", 1)[1]

    init_db()

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet in wb.sheetnames else wb[wb.sheetnames[0]]
    it = ws.iter_rows(values_only=True)
    header = [str(c).strip() if c is not None else "" for c in next(it)]

    idx = {COLUMN_MAP[h]: i for i, h in enumerate(header) if h in COLUMN_MAP}
    if "material_no" not in idx:
        print("[错误] 表头缺少「料号」列")
        print("实际表头:", header)
        wb.close()
        sys.exit(2)

    recognized = [h for h in header if h in COLUMN_MAP]
    print(f"工作表：{ws.title}")
    print(f"识别到的列（{len(recognized)}）：{', '.join(recognized)}")
    ignored = [h for h in header if h and h not in COLUMN_MAP]
    if ignored:
        print(f"忽略的列：{', '.join(ignored)}")

    rows = []
    stat_codes = 0
    for raw in it:
        rec = {}
        for field, i in idx.items():
            v = raw[i] if i < len(raw) else None
            if v is None or str(v).strip() == "":
                continue
            v = str(v).strip()
            # ERP 主表快照里的「生产统计」是**内码**（如 2338），而本地这一列存的是
            # 中文类别名（如 风速【低温】）。把编号写进名字列 = 往库里灌脏数据，
            # 所以纯数字一律跳过；翻译内码是 core/erp_sync.py 的事（它查
            # Base_DefineValue，权限不够时宁可留空）。2026-09-22 实测：不拦的话
            # 这次导入会往 70 行写进 2338 这种编号。
            if field == "production_stat" and v.isdigit():
                stat_codes += 1
                continue
            rec[field] = v
        if rec.get("material_no"):
            rows.append(rec)
    wb.close()

    if stat_codes:
        print(f"跳过生产统计内码 {stat_codes} 处（本地这列存中文类别名，编号由同步翻译）")

    if pad:
        for rec in rows:
            pad_old_no(rec)

    if dry:
        if mode != "fill":
            print("[提示] --dry-run 是为 --fill 设计的：upsert/replace 的空转没有意义")
        plan = repo.plan_item_fill(rows)
        print(f"\n空转（一条都没写）：读入 {plan['rows']} 条 · 会补 {plan['changed']} 行 · "
              f"库里没有的料号 {plan['new']} 条")
        print("每个字段会补多少行：")
        for k, n in sorted(plan["fields"].items(), key=lambda x: -x[1]):
            print(f"  {ITEM_LABELS.get(k, k):<10} {n}")
        print("\n样例：")
        for s in plan["samples"]:
            print(f"  {s['material_no']}  {s['fields']}")
        print("\n这是空转 —— 去掉 --dry-run 再跑一次才会落库。")
        return

    print(f"\n读取有效记录 {len(rows)} 条，开始导入（模式：{mode}"
          f"{' · 旧料号补前导 0' if pad else ''}）…")
    result = repo.import_items(rows, operator="import-script", mode=mode)
    print("导入完成：", result)

    st = repo.item_stats()
    print("\n匹配库现状：")
    print(f"  记录总数   {st['total']}")
    print(f"  品名种类   {st['names']}")
    print(f"  型号种类   {st['models']}")
    print(f"  缺规格     {st['no_spec']}")
    print(f"  缺产品类别 {st['no_category']}")


if __name__ == "__main__":
    main()
