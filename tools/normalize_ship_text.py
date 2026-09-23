#!/usr/bin/env python
# ⚠️ SQLite 时代的一次性脚本（2026-09-22 改 MySQL 之前写的）。
# 它直接读写 data/*.db 旧库文件，**不碰** MySQL 里的现网数据。
# 旧库仅作为回退底稿保留；若这些维护动作将来还要做，
# 必须先把本脚本改到 MySQL（参考 tools/backup.py 的改法）。
"""把发货明细（ship_detail）里混进来的「部首字符」与不可见字符清掉。

背景：ERP 的地址 / 联系人里偶尔出现 Kangxi 部首（U+2F00–U+2FDF）与
CJK 部首补充（U+2E80–U+2EFF）字符。它们的码位是独立字符，但字形画的是一个
**部首**，页面上就是「⻛⼤⼭⽔⻢⼴⿊⻰⾃」这种缺胳膊少腿的怪字 ——
用户报的「地址变形」正是它。

新同步的数据由 `core.erp_ship.normalize_text()` 在入库前折掉；
本脚本负责把**已经躺在库里**的存量数据同样折一遍，可重复执行（幂等）。

    python tools/normalize_ship_text.py            # 只看会改哪几行（dry-run）
    python tools/normalize_ship_text.py --apply    # 真的写库

改动只发生在部首字符与不可见字符上，全角标点、数字、字母原样保留
（绝不能用整串 NFKC —— 它会把（ ） ， ； ： ℃ ² Ⅰ 也一起折掉）。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import DELIVERY_DB                            # noqa: E402
from core.erp_ship import needs_normalize, normalize_text  # noqa: E402

# 与 core/erp_ship.py 的 _SELECT 里 is_text=True 的那些列保持一致
TEXT_COLUMNS = (
    "doc_no", "doc_type", "material_no", "material_name", "spec",
    "product_model", "serial_no", "customer", "contact", "express_no",
    "address",
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="真的写库（缺省只做 dry-run 报告）")
    args = ap.parse_args()

    if not DELIVERY_DB.exists():
        print(f"找不到 {DELIVERY_DB}")
        return 1

    conn = sqlite3.connect(str(DELIVERY_DB))
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) c FROM ship_detail").fetchone()["c"]
        print(f"ship_detail：{total} 行")

        sel = "id, " + ", ".join(TEXT_COLUMNS)
        changed = []          # (id, 列, 旧值, 新值)
        for row in conn.execute(f"SELECT {sel} FROM ship_detail"):
            for col in TEXT_COLUMNS:
                old = row[col]
                if not needs_normalize(old):
                    continue
                new = normalize_text(old)
                if new != old:
                    changed.append((row["id"], col, old, new))

        cols = {}
        for _id, col, _old, _new in changed:
            cols[col] = cols.get(col, 0) + 1
        print(f"命中 {len(changed)} 处，分布：{cols or '无'}")

        for _id, col, old, new in changed[:60]:
            print(f"  id={_id:<7} {col:<14} {old!r}")
            print(f"  {'':<7} {'':<14} → {new!r}")
        if len(changed) > 60:
            print(f"  …… 其余 {len(changed) - 60} 处省略")

        if not changed:
            print("库已经是干净的，无需改动。")
            return 0
        if not args.apply:
            print("\n（dry-run，未写库。要写库请加 --apply）")
            return 0

        # 按行合并成一条 UPDATE（同一行多个列一起改），减少写入放大
        by_row = {}
        for _id, col, _old, new in changed:
            by_row.setdefault(_id, {})[col] = new
        with conn:
            for _id, patch in by_row.items():
                sets = ", ".join(f"{c} = ?" for c in patch)
                conn.execute(f"UPDATE ship_detail SET {sets} WHERE id = ?",
                             list(patch.values()) + [_id])

        left = sum(
            1 for row in conn.execute(f"SELECT {', '.join(TEXT_COLUMNS)} "
                                      "FROM ship_detail")
            for col in TEXT_COLUMNS if needs_normalize(row[col]))
        print(f"\n已写库：{len(by_row)} 行 / {len(changed)} 处；复扫剩余 {left} 处")
        return 0 if left == 0 else 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
