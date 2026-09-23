# ⚠️ SQLite 时代的一次性脚本（2026-09-22 改 MySQL 之前写的）。
# 它直接读写 data/*.db 旧库文件，**不碰** MySQL 里的现网数据。
# 旧库仅作为回退底稿保留；若这些维护动作将来还要做，
# 必须先把本脚本改到 MySQL（参考 tools/backup.py 的改法）。
"""把 erp_handled 从检测登记库迁到处理登记库（一次性迁移）

背景
----
「ERP处理」原本和检测结论同存于 `inspect.db.inspect_records`。2026-09-20
按模块职责拆出：ERP 属于「检测完之后」的跟进环节，独立成 `handle.db`。

本脚本做三件事（幂等，可重复执行）：
  1. 建 handle.db 的结构（若不存在）；
  2. 把 inspect_records 里 erp_handled 有值的行搬进 handle_records；
  3. 从 inspect_records 删掉 erp_handled 列 —— 否则两处都能存，
     日后必然出现「改了这处、那处还是旧值」的脏数据。

用法
----
    python tools\\migrate_erp_to_handle.py --dry-run    # 只看要迁什么
    python tools\\migrate_erp_to_handle.py              # 执行

跑之前建议先备份 data/ 目录（脚本会提示）。
"""
import argparse
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config import HANDLE_DB, INSPECT_DB                      # noqa: E402
from core.db import init_db                                   # noqa: E402


def _columns(conn: sqlite3.Connection, table: str, schema: str = "") -> list:
    name = f"{schema}.{table}" if schema else table
    # PRAGMA 不支持参数占位，表名来自本文件的字面量，无注入风险
    return [r[1] for r in conn.execute(f"PRAGMA table_info({name});")]


def _rebuild_dict(dry_run: bool) -> None:
    """重建处理库的字典候选。

    迁移只搬了记录、没搬字典快照 —— 不重建的话「ERP处理」下拉是空的，
    而新增的字段值又会自动汇入，两处口径就对不上了。
    """
    if dry_run:
        print("\n[试运行] 将重建处理库字典候选")
        return
    from core import repo_handle
    repo_handle.refresh_dict_options()
    c = sqlite3.connect(str(HANDLE_DB))
    rows = c.execute("SELECT field, value, use_count FROM dict_option "
                     "ORDER BY field, use_count DESC;").fetchall()
    c.close()
    print(f"\n已重建处理库字典候选：{len(rows)} 条")
    for f, v, n in rows:
        print(f"  {f} = {v!r} × {n}")


def main() -> int:
    ap = argparse.ArgumentParser(description="迁移 erp_handled 到处理登记库")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    args = ap.parse_args()

    print(f"检测登记库：{INSPECT_DB}")
    print(f"处理登记库：{HANDLE_DB}")
    print(f"模式：{'试运行（不写库）' if args.dry_run else '执行迁移'}\n")

    if not args.dry_run:
        print("提示：如未备份，请先停止服务并整目录复制 data/。\n")
        init_db()          # 确保 handle.db 及其表结构存在

    conn = sqlite3.connect(str(INSPECT_DB), timeout=15.0)
    conn.row_factory = sqlite3.Row
    if not args.dry_run:
        conn.execute("ATTACH DATABASE ? AS handle_db", (str(HANDLE_DB),))

    insp_cols = _columns(conn, "inspect_records")
    if "erp_handled" not in insp_cols:
        print("inspect_records 已无 erp_handled 列 —— 数据迁移此前已完成。")
        conn.close()
        _rebuild_dict(args.dry_run)      # 字典仍要确保是重建过的
        return 0

    rows = conn.execute(
        "SELECT detail_key, erp_handled FROM inspect_records "
        "WHERE erp_handled IS NOT NULL AND TRIM(erp_handled) <> '';"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) c FROM inspect_records;").fetchone()["c"]

    print(f"inspect_records：{total} 行，其中 erp_handled 有值 {len(rows)} 行")
    for r in rows[:5]:
        print(f"  {r['detail_key']:<18} {r['erp_handled']!r}")
    if len(rows) > 5:
        print(f"  … 其余 {len(rows) - 5} 行")

    if args.dry_run:
        print(f"\n[试运行] 将写入 handle_records {len(rows)} 行，"
              f"并从 inspect_records 删除 erp_handled 列。")
        conn.close()
        return 0

    now = conn.execute("SELECT datetime('now','localtime');").fetchone()[0]
    moved = 0
    for r in rows:
        conn.execute(
            """INSERT INTO handle_db.handle_records
                   (detail_key, erp_handled, created_at, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(detail_key) DO UPDATE SET
                   erp_handled = excluded.erp_handled,
                   updated_at  = excluded.updated_at;""",
            (r["detail_key"], r["erp_handled"], now, now),
        )
        moved += 1
    conn.commit()
    print(f"\n已写入 handle_records：{moved} 行")

    # 删列：SQLite 3.35+ 支持；失败时保留列并把值清空，
    # 保证不会出现「两处都有值」的脏状态。
    try:
        conn.execute("ALTER TABLE inspect_records DROP COLUMN erp_handled;")
        conn.commit()
        print("已从 inspect_records 删除 erp_handled 列")
    except sqlite3.Error as e:
        print(f"[warn] 删除列失败（{e}），改为清空该列的值")
        conn.execute("UPDATE inspect_records SET erp_handled = NULL;")
        conn.commit()

    left = _columns(conn, "inspect_records")
    print(f"\ninspect_records 现有列：{left}")
    conn.close()

    _rebuild_dict(args.dry_run)

    # 校验
    c2 = sqlite3.connect(str(HANDLE_DB))
    n = c2.execute("SELECT COUNT(*) c FROM handle_records;").fetchone()[0]
    vals = [dict(zip(("值", "行数"), r)) for r in c2.execute(
        "SELECT COALESCE(erp_handled,'(空)') v, COUNT(*) n FROM handle_records "
        "GROUP BY 1 ORDER BY n DESC;")]
    c2.close()
    print(f"handle_records 现有 {n} 行：")
    for v in vals:
        print(f"  {v['值']!r} × {v['行数']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
