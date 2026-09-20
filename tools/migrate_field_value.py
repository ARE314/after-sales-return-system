"""字段值批量迁移 —— 口径统一用（一次性修正）。

当某字段的取值口径调整（如「无法判定」统一为「无法判断」）时，
用本脚本把历史数据整批改过来，避免新旧两种写法并存导致统计分裂。

字段归属自动判定：检测侧字段写检测登记库，其余写退回登记库。
迁移完成后字典候选会在下次刷新时自动重建，无需手工处理。

用法：
    python tools/migrate_field_value.py --field issue_category \
        --from 无法判定 --to 无法判断 --dry-run
    python tools/migrate_field_value.py --field issue_category \
        --from 无法判定 --to 无法判断
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import INSPECT_COLUMNS, close_conn, get_conn, tx   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="把某字段的旧值批量迁移为新值")
    ap.add_argument("--field", required=True, help="字段名（数据库列名）")
    ap.add_argument("--from", dest="old", required=True, help="原值")
    ap.add_argument("--to", dest="new", required=True, help="新值")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写库")
    args = ap.parse_args()

    field, old, new = args.field, args.old, args.new
    if old == new:
        print("原值与新值相同，无需迁移")
        return 0

    inspect_side = field in INSPECT_COLUMNS
    table = "inspect_db.inspect_records" if inspect_side else "returns"
    side = "检测登记库" if inspect_side else "退回登记库"

    conn = get_conn()
    col_exists = [c[1] for c in conn.execute(f"PRAGMA table_info({table.split('.')[-1]})")]
    if field not in col_exists:
        print(f"[错误] {side}的 {table} 中没有字段 {field}")
        close_conn()
        return 2

    total = conn.execute(f"SELECT COUNT(*) c FROM {table};").fetchone()["c"]
    hit = conn.execute(
        f"SELECT detail_key FROM {table} WHERE TRIM({field}) = ?;", (old,)
    ).fetchall()

    print("=" * 62)
    print(f"  字段值迁移：{field}")
    print(f"  归属    : {side} · {table}")
    print(f"  变更    : 「{old}」 → 「{new}」")
    print(f"  命中    : {len(hit)} 条（该表共 {total} 条）")
    print("=" * 62)
    for r in hit[:12]:
        print(f"    {r['detail_key']}")
    if len(hit) > 12:
        print(f"    … 另有 {len(hit) - 12} 条")

    if not hit:
        print("\n  没有需要迁移的记录")
        close_conn()
        return 0
    if args.dry_run:
        print("\n  [dry-run] 未写入任何数据")
        close_conn()
        return 0

    with tx() as c:
        cur = c.execute(
            f"UPDATE {table} SET {field} = ?, updated_at = datetime('now','localtime') "
            f"WHERE TRIM({field}) = ?;", (new, old)
        )
        changed = cur.rowcount

    # 字典候选重建（检测侧字段在检测库，其余在退回库）
    if inspect_side:
        from core import repo_inspect
        repo_inspect.refresh_dict_options()
    else:
        from core import repository
        repository.refresh_dict_options()

    print(f"\n  已迁移 {changed} 条，字典候选已重建")
    print(f"\n  {field} 现状：")
    for r in conn.execute(
        f"SELECT COALESCE(NULLIF(TRIM({field}),''),'(空)') v, COUNT(*) n "
        f"FROM {table} GROUP BY 1 ORDER BY n DESC;"
    ):
        print(f"    {r['v']:<14} {r['n']} 条")

    close_conn()
    return 0


if __name__ == "__main__":
    sys.exit(main())
