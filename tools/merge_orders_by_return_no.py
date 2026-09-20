"""梳理明细库：把「同一个快递单号被拆成多个售后单」的历史记录合并回一个售后单。

背景
----
设计上「一个快递单 → 一个售后单 → 多行明细」。但金山导入的历史数据是
一行一个售后单（同一个快递单下 34 只产品 = 34 个售后单），导致
「扫快递单号补登」时不知道该往哪个单下续行。

本脚本按**退回单号（快递单号）**分组，把同组的多条明细收敛到一个售后单下：

    SF0252673088507（34 行 / 34 个售后单）
      20260057-001  →  20260057-001   （保留最小单号，行号不变）
      20260058-001  →  20260057-002
      …             →  …
      20260090-001  →  20260057-034

同时同步改写检测 / 处理登记库的关联键（`inspect_records` / `handle_records` 的
`detail_key`）——
它是跨库关联的唯一纽带，改错会让检测结果与产品对不上。

安全措施
--------
· 迁移前用 `VACUUM INTO` 做一致性快照备份到 `data/backup_merge_<时间戳>/`
  （比直接拷文件可靠：WAL 模式下未落盘的事务也在快照里）
· `--dry-run` 先预览变更计划
· 迁移后自动校验：明细总数、检测记录数、关联完整性、
  「一快递单对应一售后单」、明细键自洽、行号连续

用法
----
    python tools/merge_orders_by_return_no.py --dry-run
    python tools/merge_orders_by_return_no.py
"""
import datetime
import os
import shutil
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.db import close_conn, get_conn, tx          # noqa: E402
from core.repository import refresh_dict_options       # noqa: E402
from core import repo_inspect                          # noqa: E402

DATA = BASE / "data"
DBS = ("returns.db", "inspect.db", "items.db")


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------

def backup() -> Path:
    """把三个库做一致性快照 —— 用 VACUUM INTO，避免 WAL 下的半截事务。"""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = DATA / f"backup_merge_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for name in DBS:
        src = DATA / name
        if not src.exists():
            continue
        out = dest / name
        conn = sqlite3.connect(str(src))
        try:
            conn.execute(f"VACUUM INTO '{out.as_posix()}';")
        finally:
            conn.close()
        print(f"    已备份 {name:<12} → {out.name}（{out.stat().st_size:,} bytes）")
    return dest


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------

def build_plan(conn) -> tuple:
    """算出「旧明细键 → 新售后单号 / 行号 / 明细键」的映射。

    分组键是退回单号；退回单号为空的记录不参与（没有分组依据）。
    """
    rows = conn.execute(
        """SELECT detail_key, order_no, line_no, return_no, product_code
             FROM returns ORDER BY order_no, line_no;"""
    ).fetchall()

    groups, ungrouped = {}, []
    for r in rows:
        ret = (r["return_no"] or "").strip()
        if not ret:
            ungrouped.append(r)
            continue
        groups.setdefault(ret, []).append(r)

    plan, merged_groups = [], []
    for ret, items in groups.items():
        # 组内按售后单号升序 → 保留最小者作为主单，行号按此顺序重排
        items.sort(key=lambda x: (x["order_no"], x["line_no"]))
        keep = items[0]["order_no"]
        for i, r in enumerate(items, start=1):
            new_key = f"{keep}-{i:03d}"
            plan.append({
                "old_key": r["detail_key"],
                "new_order_no": keep,
                "new_line_no": i,
                "new_key": new_key,
                "return_no": ret,
                "product_code": r["product_code"],
                "changed": (r["detail_key"] != new_key
                            or r["order_no"] != keep
                            or r["line_no"] != i),
            })
        if len(items) > 1:
            merged_groups.append({
                "return_no": ret, "keep": keep,
                "count": len(items),
                "dropped": [x["order_no"] for x in items[1:]],
            })
    return plan, merged_groups, ungrouped


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def apply_plan(plan: list) -> int:
    """写入两个库。先改退回登记库，再改检测登记库（ATTACH 无跨库原子事务）。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = [p for p in plan if p["changed"]]
    if not changed:
        return 0

    with tx() as conn:
        for p in changed:
            conn.execute(
                "UPDATE returns SET order_no = ?, line_no = ?, detail_key = ?, "
                "updated_at = ? WHERE detail_key = ?;",
                (p["new_order_no"], p["new_line_no"], p["new_key"], now, p["old_key"]),
            )

    # 检测 / 处理登记库：只改关联键，其余字段原样不动。
    # 处理库 2026-09-20 才独立出来，这里必须一并覆盖 —— 漏掉的话
    # handle_records 里会留下一批指向已消失键的孤儿行（ERP 处理记录静默丢失）。
    # 但它也可能尚未建立（全新部署时先合并、后建处理库），那时无记录可同步。
    with tx() as conn:
        try:
            conn.execute("SELECT 1 FROM handle_db.handle_records LIMIT 1;")
            has_handle = True
        except sqlite3.Error:
            has_handle = False
        for p in changed:
            conn.execute(
                "UPDATE inspect_db.inspect_records SET detail_key = ? "
                "WHERE detail_key = ?;", (p["new_key"], p["old_key"]),
            )
            if has_handle:
                conn.execute(
                    "UPDATE handle_db.handle_records SET detail_key = ? "
                    "WHERE detail_key = ?;", (p["new_key"], p["old_key"]),
                )
    return len(changed)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def verify(conn, before: dict) -> list:
    """迁移后的完整性校验，返回 [(项, 通过?, 说明)]。"""
    checks = []

    total = conn.execute("SELECT COUNT(*) c FROM returns;").fetchone()["c"]
    checks.append(("明细总数不变", total == before["total"],
                   f"{before['total']} → {total}"))

    insp = conn.execute(
        "SELECT COUNT(*) c FROM inspect_db.inspect_records;").fetchone()["c"]
    checks.append(("检测记录数不变", insp == before["inspected"],
                   f"{before['inspected']} → {insp}"))

    orphan = conn.execute(
        """SELECT COUNT(*) c FROM inspect_db.inspect_records i
            WHERE NOT EXISTS (SELECT 1 FROM returns r
                               WHERE r.detail_key = i.detail_key);"""
    ).fetchone()["c"]
    checks.append(("检测记录无孤立（关联键全部有效）", orphan == 0, f"{orphan} 条孤立"))

    # 处理库可能尚未建立（全新部署：先合并、后建处理库）—— 取不到就跳过这两项
    try:
        hndl = conn.execute(
            "SELECT COUNT(*) c FROM handle_db.handle_records;").fetchone()["c"]
        h_orphan = conn.execute(
            """SELECT COUNT(*) c FROM handle_db.handle_records h
                WHERE NOT EXISTS (SELECT 1 FROM returns r
                                   WHERE r.detail_key = h.detail_key);"""
        ).fetchone()["c"]
    except sqlite3.Error:
        hndl = h_orphan = 0
    checks.append(("处理记录数不变", hndl == before.get("handled", hndl),
                   f"{before.get('handled', hndl)} → {hndl}"))
    checks.append(("处理记录无孤立（关联键全部有效）", h_orphan == 0,
                   f"{h_orphan} 条孤立"))

    bad_key = conn.execute(
        """SELECT COUNT(*) c FROM returns
            WHERE detail_key <> order_no || '-' || printf('%03d', line_no);"""
    ).fetchone()["c"]
    checks.append(("明细键 = 售后单号-行号（自洽）", bad_key == 0, f"{bad_key} 条不自洽"))

    dup_key = conn.execute(
        """SELECT COUNT(*) c FROM (SELECT detail_key FROM returns
            GROUP BY detail_key HAVING COUNT(*) > 1);"""
    ).fetchone()["c"]
    checks.append(("明细键唯一", dup_key == 0, f"{dup_key} 个重复键"))

    multi = conn.execute(
        """SELECT COUNT(*) c FROM (SELECT return_no FROM returns
            WHERE TRIM(COALESCE(return_no,'')) <> ''
            GROUP BY return_no HAVING COUNT(DISTINCT order_no) > 1);"""
    ).fetchone()["c"]
    checks.append(("一个快递单只对应一个售后单", multi == 0, f"{multi} 个仍分裂"))

    gaps = conn.execute(
        """SELECT COUNT(*) c FROM (SELECT order_no FROM returns
            GROUP BY order_no HAVING COUNT(*) <> MAX(line_no));"""
    ).fetchone()["c"]
    checks.append(("行号连续无跳号", gaps == 0, f"{gaps} 个单行号不连续"))

    orders = conn.execute(
        "SELECT COUNT(DISTINCT order_no) c FROM returns;").fetchone()["c"]
    returns_n = conn.execute(
        """SELECT COUNT(DISTINCT return_no) c FROM returns
            WHERE TRIM(COALESCE(return_no,'')) <> '';""").fetchone()["c"]
    checks.append(("售后单数收敛到快递单数", orders == returns_n + before["no_return_no"],
                   f"售后单 {orders} · 快递单 {returns_n}"))
    return checks


def main() -> int:
    dry = "--dry-run" in sys.argv

    conn = get_conn()
    before = {
        "total": conn.execute("SELECT COUNT(*) c FROM returns;").fetchone()["c"],
        "inspected": conn.execute(
            "SELECT COUNT(*) c FROM inspect_db.inspect_records;").fetchone()["c"],
        "orders": conn.execute(
            "SELECT COUNT(DISTINCT order_no) c FROM returns;").fetchone()["c"],
        "no_return_no": conn.execute(
            """SELECT COUNT(*) c FROM returns
                WHERE TRIM(COALESCE(return_no,'')) = '';""").fetchone()["c"],
    }
    # 处理库可能是新装的（结构尚未建），取不到就当 0 —— 校验里只比对增减
    try:
        before["handled"] = conn.execute(
            "SELECT COUNT(*) c FROM handle_db.handle_records;").fetchone()["c"]
    except sqlite3.Error:
        before["handled"] = 0

    plan, merged, ungrouped = build_plan(conn)
    changed = [p for p in plan if p["changed"]]

    print("=" * 74)
    print("  合并「同一快递单号下的多个售后单」")
    print("=" * 74)
    print(f"  明细总数        : {before['total']}")
    print(f"  售后单（迁移前）: {before['orders']}")
    print(f"  需要改动的明细  : {len(changed)} 条")
    print(f"  涉及快递单      : {len(merged)} 个")

    print("\n  合并明细（按影响行数排序，最多显示 8 组）：")
    for g in sorted(merged, key=lambda x: -x["count"])[:8]:
        head = ", ".join(g["dropped"][:6]) + ("…" if len(g["dropped"]) > 6 else "")
        print(f"    {g['return_no']:<18} {g['count']:>2} 行  → 保留 {g['keep']}"
              f"　并入：{head}")
    if len(merged) > 8:
        print(f"    … 另有 {len(merged) - 8} 个快递单")

    if ungrouped:
        print(f"\n  无退回单号、保持原样的记录：{len(ungrouped)} 条")
        for r in ungrouped[:5]:
            print(f"    {r['detail_key']}  产品={r['product_code']}")

    print("\n  变更样例（前 10 条）：")
    for p in changed[:10]:
        print(f"    {p['old_key']:<14} → {p['new_key']:<14} "
              f"（{p['return_no']} · {p['product_code'] or '无产品编号'}）")

    # 冲突预检：新键在现有库里是否已被占用
    existing = {r["detail_key"] for r in conn.execute("SELECT detail_key FROM returns;")}
    old_keys = {p["old_key"] for p in changed}
    clashes = [p for p in changed if p["new_key"] in existing and p["new_key"] not in old_keys]
    if clashes:
        print(f"\n  [中止] 有 {len(clashes)} 个新键与现有记录冲突：")
        for p in clashes[:5]:
            print(f"    {p['new_key']}（来自 {p['old_key']}）")
        close_conn()
        return 2
    print("\n  冲突预检：新键均未占用 ✓")

    if dry:
        print("\n  [dry-run] 未写入任何数据")
        close_conn()
        return 0

    if not changed:
        print("\n  没有需要合并的记录，无需改动")
        close_conn()
        return 0

    print("\n  迁移前备份：")
    dest = backup()
    print(f"    → {dest.relative_to(BASE)}")

    n = apply_plan(plan)
    print(f"\n  已改写 {n} 条明细键，检测登记库关联键同步更新")

    refresh_dict_options()
    repo_inspect.refresh_dict_options()

    print("\n" + "=" * 74)
    print("  迁移后校验")
    print("=" * 74)
    results = verify(conn, before)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<28} {detail}")

    after_orders = conn.execute(
        "SELECT COUNT(DISTINCT order_no) c FROM returns;").fetchone()["c"]
    print(f"\n  售后单：{before['orders']} → {after_orders}"
          f"（减少 {before['orders'] - after_orders} 个）")

    all_ok = all(ok for _, ok, _ in results)
    print("\n  结论：" + ("全部校验通过 ✓" if all_ok else "存在未通过项，请检查 ✗"))
    if not all_ok:
        print(f"  如需回滚，用备份覆盖：{dest.relative_to(BASE)}")

    close_conn()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
