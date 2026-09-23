r"""从备份 dump 里恢复指定的表 —— 灾难恢复专用。

背景
----
`tools/backup.py` 用 mysqldump 把六个库整份导出成 `data/backups/<ts>/all.sql`，
`tools/check_backup.py` 只做「影子库还原演练」。真出事（数据被误删）时缺一个
「把某几张表的数据灌回活库」的工具，这个文件补上那个缺口。

用法
----
    python tools/restore_backup.py --list
    python tools/restore_backup.py --empty-only --dry-run
    python tools/restore_backup.py --empty-only
    python tools/restore_backup.py --tables returns_db.returns,handle_db.handle_records
    python tools/restore_backup.py --tables returns_db.returns --force

约定
----
* 默认**拒绝**覆盖活库里已有数据的表（必须先 `--force`，那会先清空该表）。
* `--empty-only`：只恢复「活库为空、备份非空」的表，是最安全的急救姿势。
* 恢复完逐表核对行数，与 MANIFEST.json 的清单一致才算成功。
* 解析的是 mysqldump 的 `USE \`schema\`;` + `INSERT INTO \`table\` ... ;` 文本，
  只搬数据不动表结构（表结构由 `core/db.py` 的 DDL 负责，不该被旧 dump 覆盖）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.dbapi import get_conn                                    # noqa: E402
from config import DATA_DIR                                        # noqa: E402

USE_RE = re.compile(r"^USE `([^`]+)`;")
INS_RE = re.compile(r"^INSERT INTO `([^`]+)`")


def backups_dir() -> Path:
    return Path(DATA_DIR) / "backups"


def latest_backup() -> Path:
    """最新一个「已写完」（有 DONE 标记）的备份目录。"""
    cands = [p for p in sorted(backups_dir().iterdir())
             if p.is_dir() and (p / "DONE").exists() and (p / "all.sql").exists()]
    if not cands:
        raise SystemExit(f"没找到可用的备份目录（{backups_dir()}）")
    return cands[-1]


def manifest_of(bkp: Path) -> dict:
    p = bkp / "MANIFEST.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def claims(man: dict) -> dict:
    """{'schema.table': 行数}。"""
    out = {}
    for d in man.get("databases", []):
        for t, n in (d.get("tables") or {}).items():
            out[f"{d['schema']}.{t}"] = int(n)
    return out


def scan_dump(dump: Path, wanted: set) -> dict:
    """流式扫 dump，返回 {schema.table: [完整 INSERT 语句, ...]}。"""
    found: dict = {k: [] for k in wanted}
    schema = ""
    buf: list = []
    target = None
    with dump.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if not line:
                continue
            if target is not None:                      # 续行（一条 INSERT 跨多行）
                buf.append(line)
                if line.rstrip().endswith(";"):
                    found[target].append("\n".join(buf))
                    target, buf = None, []
                continue
            m = USE_RE.match(line)
            if m:
                schema = m.group(1)
                continue
            m = INS_RE.match(line)
            if m and f"{schema}.{m.group(1)}" in wanted:
                key = f"{schema}.{m.group(1)}"
                if line.rstrip().endswith(";"):
                    found[key].append(line)
                else:
                    target, buf = key, [line]
    return found


def live_count(conn, schema: str, table: str):
    try:
        return conn.execute(f"SELECT COUNT(*) c FROM `{schema}`.`{table}`;").fetchone()["c"]
    except Exception as exc:                            # noqa: BLE001
        return f"ERR:{exc}"


def qualify(sql: str, schema: str, table: str) -> str:
    """给 dump 里裸的 `INSERT INTO \\`t\\`` 补上库名。

    dump 靠 `USE \\`schema\\`;` 切库，而我们是拿现成连接直接执行的（会话默认库是
    returns_db），不补库名的话跨库的表会报
    `Table 'returns_db.handle_records' doesn't exist`。
    """
    head = f"INSERT INTO `{table}`"
    if sql.startswith(head):
        return f"INSERT INTO `{schema}`.`{table}`" + sql[len(head):]
    return sql


def run_sql(conn, stmts, schema: str, table: str) -> int:
    """用原始游标执行（不走 translate，dump 文本里的 % 就是字面量）。"""
    cur = conn.cursor()
    n = 0
    try:
        for sql in stmts:
            cur.execute(qualify(sql, schema, table))
            n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    finally:
        cur.close()
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从备份 dump 恢复指定的表")
    ap.add_argument("--backup", default="", help="备份目录（缺省=最新一个）")
    ap.add_argument("--tables", default="", help="逗号分隔的 schema.table")
    ap.add_argument("--empty-only", action="store_true",
                    help="只恢复「活库为空、备份非空」的表")
    ap.add_argument("--force", action="store_true",
                    help="允许覆盖活库已有数据的表（会先清空该表）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="列出备份里的表与行数")
    args = ap.parse_args(argv)

    bkp = Path(args.backup) if args.backup else latest_backup()
    man = manifest_of(bkp)
    want_all = claims(man)
    if args.list:
        print(f"备份目录：{bkp}")
        for k, v in sorted(want_all.items()):
            print(f"  {k:42s} {v}")
        return 0

    conn = get_conn()
    if args.tables:
        tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    else:
        rows = conn.execute(
            "SELECT CONCAT(table_schema,'.',table_name) k FROM information_schema.tables "
            "WHERE table_schema IN ('returns_db','inspect_db','handle_db','delivery_db',"
            "'items_db','auth_db') AND table_type='BASE TABLE' ORDER BY k;").fetchall()
        tables = [r["k"] for r in rows]

    plan = []
    for key in tables:
        schema, _, table = key.partition(".")
        if not table:
            print(f"  [跳过] 名字不合法：{key}")
            continue
        bk = want_all.get(key)
        live = live_count(conn, schema, table)
        if not isinstance(live, int):
            print(f"  [跳过] 活库读不到：{key} → {live}")
            continue
        if bk is None:
            print(f"  [跳过] 备份清单里没有：{key}")
            continue
        if args.empty_only and not (live == 0 and bk > 0):
            print(f"  [跳过] --empty-only：活库 {live} 行 / 备份 {bk} 行：{key}")
            continue
        if live > 0 and not args.force:
            print(f"  [拒绝] 活库已有 {live} 行，需 --force：{key}")
            continue
        plan.append((key, schema, table, live, bk))

    if not plan:
        print("没有需要恢复的表。")
        return 0

    print(f"备份：{bkp}")
    print("计划恢复：")
    for key, _s, _t, live, bk in plan:
        print(f"  {key:42s} 活库 {live} → 备份 {bk}")

    need = {k for k, *_ in plan}
    stmts = scan_dump(bkp / "all.sql", need)
    for k in sorted(need):
        print(f"  dump 里 {k} 的 INSERT 语句：{len(stmts[k])} 条")

    if args.dry_run:
        print("[dry-run] 未执行任何写入。")
        return 0

    for key, schema, table, live, bk in plan:
        if live > 0:
            conn.execute(f"DELETE FROM `{schema}`.`{table}`;")
            print(f"  [清空] {key}（原 {live} 行）")
        n = run_sql(conn, stmts[key], schema, table)
        print(f"  [写入] {key}：{n} 行")

    ok = True
    print("核对：")
    for key, schema, table, _live, bk in plan:
        now = live_count(conn, schema, table)
        flag = "OK" if now == bk else "!!"
        if now != bk:
            ok = False
        print(f"  [{flag}] {key:42s} 现有 {now} / 备份 {bk}")
    print("恢复" + ("成功。" if ok else "完成，但有行数不一致，请人工核对。"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
