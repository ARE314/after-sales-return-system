"""按产品编号前 4 位（YYMM）回填生产年月 —— 一次性历史数据修正。

规则与运行时一致（core/repository.parse_period_from_code）：
    20100341 → 2020 年 10 月      19121192 → 2019 年 12 月

写入策略（重要，避免把有信息的字段抹掉）：
  · 解析成功            → 生产年份、生产月份都写入解析值
                          （覆盖原先的「无法确认」，也修正与解析冲突的年份）
  · 解析失败（编号不规范）→ 只把**空的**字段写为「无法确认」，
                          已有值原样保留 —— 不能让「无法确认」盖掉真实年份

用法：
    python tools/backfill_period.py --dry-run   # 只看变更计划，不写库
    python tools/backfill_period.py             # 实际写入
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import PERIOD_UNKNOWN                    # noqa: E402
from core.db import close_conn, get_conn, tx          # noqa: E402
from core.repository import parse_period_from_code    # noqa: E402


def plan(conn) -> list:
    """算出每一条的变更（不落库）。"""
    changes = []
    rows = conn.execute(
        """SELECT detail_key, product_code, production_year, production_month
           FROM returns
           WHERE TRIM(COALESCE(product_code, '')) <> ''
           ORDER BY detail_key;"""
    ).fetchall()

    for r in rows:
        period = parse_period_from_code(r["product_code"])
        if not period:
            continue
        parsed_ok = period["production_year"] != PERIOD_UNKNOWN
        cur_year = (r["production_year"] or "").strip()
        cur_month = (r["production_month"] or "").strip()

        updates = {}
        for field, value in period.items():
            cur = cur_year if field == "production_year" else cur_month
            if cur == value:
                continue                       # 无需改动
            if parsed_ok or not cur:
                # 解析成功 → 一律写解析值；解析失败 → 只填空缺
                updates[field] = value
        if updates:
            changes.append({
                "detail_key": r["detail_key"],
                "product_code": r["product_code"],
                "before": {"production_year": cur_year, "production_month": cur_month},
                "updates": updates,
                "ok": parsed_ok,
            })
    return changes, len(rows)


def main() -> int:
    dry = "--dry-run" in sys.argv
    conn = get_conn()

    changes, scanned = plan(conn)
    fixed = [c for c in changes if c["ok"]]
    unknown = [c for c in changes if not c["ok"]]

    print("=" * 68)
    print("  按产品编号回填生产年月")
    print("=" * 68)
    print(f"  有产品编号的记录 : {scanned} 条")
    print(f"  需要变更         : {len(changes)} 条"
          f"（可解析 {len(fixed)} · 标记无法确认 {len(unknown)}）")

    print("\n  变更明细（最多显示 12 条）：")
    for c in changes[:12]:
        b = c["before"]
        parts = []
        for k, v in c["updates"].items():
            label = "年" if k == "production_year" else "月"
            old = b[k] or "(空)"
            parts.append(f"{label}: {old} → {v}")
        flag = "" if c["ok"] else "  [编号不规范]"
        print(f"    {c['detail_key']:<14} 编号={c['product_code']:<12} "
              f"{' · '.join(parts)}{flag}")
    if len(changes) > 12:
        print(f"    … 另有 {len(changes) - 12} 条")

    print("\n  无法解析的编号（保持原年份，仅空字段写占位）：")
    bad_codes = sorted({c["product_code"] for c in unknown})
    for code in bad_codes[:10]:
        print(f"    {code}")
    if not bad_codes:
        print("    （无）")

    if dry:
        print("\n  [dry-run] 未写入任何数据")
        close_conn()
        return 0

    with tx() as c:
        for ch in changes:
            sets = ", ".join(f"{k} = ?" for k in ch["updates"])
            c.execute(
                f"UPDATE returns SET {sets}, updated_at = datetime('now','localtime') "
                f"WHERE detail_key = ?;",
                list(ch["updates"].values()) + [ch["detail_key"]],
            )

    # 回填后重建字典（生产年份是字典字段）
    from core.repository import refresh_dict_options   # noqa: E402
    refresh_dict_options()

    print(f"\n  已写入 {len(changes)} 条变更")
    for row in conn.execute(
        """SELECT COALESCE(NULLIF(TRIM(production_year), ''), '(空)') y,
                  COALESCE(NULLIF(TRIM(production_month), ''), '(空)') m,
                  COUNT(*) n
           FROM returns GROUP BY y, m ORDER BY n DESC LIMIT 12;"""
    ):
        print(f"    {row['y']:<10} {row['m']:<10} {row['n']} 条")

    close_conn()
    return 0


if __name__ == "__main__":
    sys.exit(main())
