"""一次性数据修正：把「未处理」的明细补上「待处理」

背景
----
`erp_handled` 已从自由文本改为**两值锁定项**（已处理 / 待处理），
空值在读取时统一按「待处理」呈现（见 `config.HANDLE_DEFAULT_VALUES`）。

那为什么还要回填？因为**稀疏存储 + 读取归一**这个组合有个副作用：
- 处理库是稀疏的（没处理过就没有行）
- 库里同时可能出现三种状态：有行且=已处理、有行且=待处理、整行不存在

三态并存时，任何一处写 SQL 时忘了归一，都会静默算错人数或漏掉记录。
把「已知的未处理」显式写成「待处理」后，库里就只剩两态，
`= '待处理'` 这种直白写法也是对的 —— 读取归一仍然保留，作为兜底。

处理逻辑
--------
对**每一条退回明细**（不论是否已检测）：
    当前有效值 = handle_records.erp_handled（没有行则为空）
    若有效值 = 已处理  → 不动
    否则               → 若无行则 INSERT「待处理」；有行且为空则 UPDATE

用法
----
    python tools\\backfill_handle_pending.py --dry-run    # 只看会改什么
    python tools\\backfill_handle_pending.py              # 执行

建议先停掉服务再跑（写入期间界面上的计数会短暂不一致，无数据风险）。
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import HANDLE_DEFAULT_VALUES, HANDLE_DONE_VALUES  # noqa: E402
from core import repo_handle  # noqa: E402
from core.db import get_conn, init_db, tx  # noqa: E402

PENDING = HANDLE_DEFAULT_VALUES["erp_handled"]      # 待处理
DONE = HANDLE_DONE_VALUES["erp_handled"]            # 已处理


def survey(conn: sqlite3.Connection) -> dict:
    """盘点现状：三类明细各有多少。"""
    def one(sql, *args):
        return conn.execute(sql, args).fetchone()["c"]

    total = one("SELECT COUNT(*) c FROM returns;")
    done = one("SELECT COUNT(*) c FROM handle_db.handle_records "
               "WHERE TRIM(COALESCE(erp_handled,'')) = ?;", DONE)
    blank = one("""SELECT COUNT(*) c FROM returns r
                   JOIN handle_db.handle_records h
                     ON h.detail_key = r.detail_key
                   WHERE TRIM(COALESCE(h.erp_handled,'')) = '';""")
    nrow = one("""SELECT COUNT(*) c FROM returns r
                  WHERE NOT EXISTS (SELECT 1 FROM handle_db.handle_records h
                                    WHERE h.detail_key = r.detail_key);""")
    odd = one("""SELECT COUNT(*) c FROM handle_db.handle_records
                 WHERE TRIM(COALESCE(erp_handled,'')) NOT IN (?, ?);""",
              DONE, PENDING)
    return {"total": total, "done": done, "blank_row": blank,
            "no_row": nrow, "odd": odd,
            "pending_now": total - done}


def run(dry_run: bool) -> int:
    init_db()
    conn = get_conn()
    before = survey(conn)

    print(f"退回明细合计      {before['total']} 条")
    print(f"  已处理          {before['done']} 条（不动）")
    print(f"  待处理（待回填）{before['pending_now']} 条")
    print(f"    ├ 有行但为空  {before['blank_row']} 条 → UPDATE")
    print(f"    └ 整行不存在  {before['no_row']} 条 → INSERT")
    if before["odd"]:
        print(f"  非规范值        {before['odd']} 条 → 归一到「{PENDING}」")

    if not before["pending_now"] and not before["odd"]:
        print("\n无需处理：所有明细都已是「已处理」或「待处理」。")
        return 0

    if dry_run:
        print(f"\n[试运行] 将写入 {before['pending_now']} 条「{PENDING}」，未改动任何数据。")
        return 0

    # 一次性批量写：逐条调 repo_handle.upsert 会重复触发字典重建（很慢），
    # 而且会写 60 多条操作日志 —— 这是一次数据修正，不是用户操作。
    now = conn.execute("SELECT datetime('now','localtime');").fetchone()[0]
    inserted = updated = normalized = 0
    with tx() as c:
        # 1) 有行但为空 / 非规范值 → 直接改
        cur = c.execute(
            "UPDATE handle_db.handle_records SET erp_handled = ?, "
            "updated_at = ? WHERE TRIM(COALESCE(erp_handled,'')) = '';",
            (PENDING, now))
        updated = cur.rowcount
        cur = c.execute(
            "UPDATE handle_db.handle_records SET erp_handled = ?, "
            "updated_at = ? WHERE TRIM(COALESCE(erp_handled,'')) "
            "NOT IN (?, ?);", (PENDING, now, DONE, PENDING))
        normalized = cur.rowcount
        # 2) 整行不存在 → 补一行
        extra = updated + normalized
        cur = c.execute(
            """INSERT INTO handle_db.handle_records
                   (detail_key, erp_handled, created_at, updated_at)
               SELECT r.detail_key, ?, ?, ?
                 FROM returns r
                WHERE NOT EXISTS (
                      SELECT 1 FROM handle_db.handle_records h
                       WHERE h.detail_key = r.detail_key);""",
            (PENDING, now, now))
        inserted = cur.rowcount

    repo_handle.refresh_dict_options()

    after = survey(get_conn())
    print(f"\n已回填：新增 {inserted} 条 · 更新 {updated} 条"
          + (f" · 归一 {normalized} 条" if normalized else ""))
    print(f"复核：已处理 {after['done']} 条 · 待处理 {after['pending_now']} 条"
          f" · 处理库行数 {after['total'] - after['no_row']} 条")
    assert after["no_row"] == 0, "仍有明细没有处理记录行"
    assert after["done"] == before["done"], "「已处理」的条数被改动了"
    print("自检通过：所有明细都已有明确的两值状态。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把未处理的明细回填为「待处理」")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    args = ap.parse_args()
    print(f"处理登记库：{Path('data/handle.db').resolve()}")
    print(f"模式：{'试运行（不写库）' if args.dry_run else '执行回填'}\n")
    return run(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
