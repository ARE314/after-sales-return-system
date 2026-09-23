# ⚠️ SQLite 时代的一次性脚本（2026-09-22 改 MySQL 之前写的）。
# 它直接读写 data/*.db 旧库文件，**不碰** MySQL 里的现网数据。
# 旧库仅作为回退底稿保留；若这些维护动作将来还要做，
# 必须先把本脚本改到 MySQL（参考 tools/backup.py 的改法）。
"""三库架构迁移 —— 旧单库 → 一个模块一个库

迁移目标
--------
    returns.db   退回登记库   returns / model_dict / dict_option / op_log / sync_log
    inspect.db   检测登记库   inspect_records / dict_option / op_log
    items.db     匹配数据库   item_master

迁移内容
--------
1. returns 表里的 11 个检测字段 → inspect.db 的 inspect_records
   （由 core.db.init_db() 自动完成，迁移前会整库备份到 data/backup_legacy_*/）
2. item_master 表 → items.db（本脚本完成）

脚本可重复执行：已迁移的部分会自动跳过。

用法：
    python tools/migrate_to_multidb.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.db import (INSPECT_COLUMNS, close_conn, db_status,  # noqa: E402
                     get_conn, init_db)

# item_master 的业务列（id 由新库自增）
ITEM_COLUMNS = [
    "material_no", "old_material_no", "product_name", "model_no", "spec",
    "description", "customer", "production_stat", "param1", "param2",
    "param3", "category", "source", "created_at", "updated_at",
]


def table_exists(conn, name: str, schema: str = "main") -> bool:
    prefix = "" if schema == "main" else f"{schema}."
    row = conn.execute(
        f"SELECT name FROM {prefix}sqlite_master "
        f"WHERE type='table' AND name = ?;", (name,)
    ).fetchone()
    return row is not None


def main() -> int:
    print("=" * 66)
    print("  三库架构迁移")
    print("=" * 66)

    # 建三库结构；若 returns 表仍是旧结构，会先整库备份再迁移检测字段
    init_db()
    conn = get_conn()

    # ---------- 1. 检测字段 ----------
    legacy_left = [c for c in INSPECT_COLUMNS
                   if c in {r["name"] for r in conn.execute("PRAGMA table_info(returns);")}]
    inspected = conn.execute(
        "SELECT COUNT(*) c FROM inspect_db.inspect_records;").fetchone()["c"]
    print(f"\n[1] 检测登记库")
    print(f"    returns 表残留检测字段 : {legacy_left or '无（已清理）'}")
    print(f"    inspect_records        : {inspected} 行")

    # ---------- 2. 匹配数据库 ----------
    print(f"\n[2] 匹配数据库")
    migrated = 0
    if table_exists(conn, "item_master", "main"):
        cols = ", ".join(ITEM_COLUMNS)
        before = conn.execute(
            "SELECT COUNT(*) c FROM items_db.item_master;").fetchone()["c"]
        conn.execute(
            f"INSERT OR REPLACE INTO items_db.item_master ({cols}) "
            f"SELECT {cols} FROM item_master;")
        conn.commit()
        migrated = conn.execute(
            "SELECT COUNT(*) c FROM items_db.item_master;").fetchone()["c"]
        conn.execute("DROP TABLE item_master;")
        conn.commit()
        print(f"    旧表 item_master       : 已迁入")
        print(f"    items.db 迁移前 / 后   : {before} → {migrated} 行")
    else:
        migrated = conn.execute(
            "SELECT COUNT(*) c FROM items_db.item_master;").fetchone()["c"]
        print(f"    旧表已不存在（此前已迁移）")
        print(f"    items.db item_master   : {migrated} 行")

    # ---------- 3. 结构核对 ----------
    print(f"\n[3] 三库结构核对")
    for label, schema, table in (
        ("退回登记库 returns", "main", "returns"),
        ("退回登记库 model_dict", "main", "model_dict"),
        ("退回登记库 dict_option", "main", "dict_option"),
        ("退回登记库 op_log", "main", "op_log"),
        ("退回登记库 sync_log", "main", "sync_log"),
        ("检测登记库 inspect_records", "inspect_db", "inspect_records"),
        ("检测登记库 dict_option", "inspect_db", "dict_option"),
        ("检测登记库 op_log", "inspect_db", "op_log"),
        ("匹配数据库 item_master", "items_db", "item_master"),
    ):
        prefix = "" if schema == "main" else f"{schema}."
        n = conn.execute(f"SELECT COUNT(*) c FROM {prefix}{table};").fetchone()["c"]
        cols = len(conn.execute(f"PRAGMA {prefix}table_info({table});").fetchall())
        print(f"    {label:<28} {n:>6} 行  {cols:>2} 列")

    # ---------- 4. 归属校验 ----------
    print(f"\n[4] 字段归属校验")
    returns_cols = {r["name"] for r in conn.execute("PRAGMA table_info(returns);")}
    inspect_cols = {r["name"]
                    for r in conn.execute("PRAGMA inspect_db.table_info(inspect_records);")}
    leak = returns_cols & set(INSPECT_COLUMNS)
    dup = (returns_cols & inspect_cols) - {"detail_key"}
    print(f"    returns 混入检测字段   : {leak or '无 ✓'}")
    print(f"    两库重复的业务字段     : {dup or '无（仅共享 detail_key）✓'}")

    status = db_status()
    print(f"\n[5] 概览")
    for k, v in status["databases"].items():
        print(f"    {k:<9} {v}")
    print(f"    退回 {status['total']} 条 · 已检测 {status['inspected']} 条 · "
          f"物料 {status['item_master']} 条")

    close_conn()
    print("\n" + "=" * 66)
    print("  迁移完成")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
