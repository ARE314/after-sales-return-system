"""导入物料主档（匹配数据库）

用法：
    python tools/import_item_master.py <Excel路径> [--replace] [--sheet=物料主档]

说明：
    · 默认按「料号」upsert：已有料号则覆盖其字段，缺失则新增，不影响其他记录；
    · 加 --replace 则先清空整表再导入（适合整表刷新）；
    · 表头需含：旧料号 / 料号 / 品名 / 型号 / 规格 / 描述 / 客户 / 生产统计 / 标参1-3。
      缺失的列会被忽略，料号为空的行会被跳过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl  # noqa: E402

from config import ITEM_IMPORT_MAP  # noqa: E402
from core import repository as repo  # noqa: E402
from core.db import init_db  # noqa: E402

# Excel 表头 → 数据库列（与页面导入共用同一份映射，避免两处维护）
COLUMN_MAP = ITEM_IMPORT_MAP


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)

    path = args[0]
    mode = "replace" if "--replace" in sys.argv else "upsert"
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
    for raw in it:
        rec = {}
        for field, i in idx.items():
            v = raw[i] if i < len(raw) else None
            if v is not None and str(v).strip() != "":
                rec[field] = str(v).strip()
        if rec.get("material_no"):
            rows.append(rec)
    wb.close()

    print(f"\n读取有效记录 {len(rows)} 条，开始导入（模式：{mode}）…")
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
