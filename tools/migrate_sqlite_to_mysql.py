"""把 6 个 SQLite 库整体搬进 MySQL

用法
----
    python tools/migrate_sqlite_to_mysql.py                 # 只体检，不写库（默认）
    python tools/migrate_sqlite_to_mysql.py --apply         # 真搬（会先清空目标表）
    python tools/migrate_sqlite_to_mysql.py --apply --only ship_detail
    python tools/migrate_sqlite_to_mysql.py --check-length # 只做长度体检

为什么要有「体检」这一步
--------------------
MySQL 连接的 sql_mode 里带 STRICT_TRANS_TABLES —— **超长的值不会静默截断，
而是直接报 1406 让整条 INSERT 失败**。8.9 万行的 ship_detail 如果在第 7 万行
才撞上一列超长，前面 7 万行已经写进去了，事务一回滚就全白干，还看不出是哪一列。
所以先把每列的实际最大长度跟 MySQL 的字符上限比一遍，超了就在**动手之前**报出来。

列对齐规则
--------
* 以**目标表**的列顺序为准（INSERT 的列名显式写出来，不依赖两边列序一致）；
* 源里有、目标里没有的列（SQLite 时代的废弃列）→ 跳过并打印；
* 目标里有、源里没有的列 → 不写进 INSERT，让 MySQL 用它自己的 DEFAULT。

三种值类型会做显式转换（其余原样交给 PyMySQL）：
* INT 列：SQLite 里可能是 float 或数字字符串 → 转 int；
* DOUBLE 列：数字字符串 → float；
* 其余：bytes → utf-8 解码（SQLite 里偶有 BLOB）。

转不了的**不静默变 NULL**，而是把整张表标成失败并打印出是哪一行的哪一列 ——
迁移最怕的就是「搬完看起来成功了，其实少了几行」。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import DATA_DIR, SCHEMAS           # noqa: E402
from core import db as dbmod                   # noqa: E402

# 源文件与目标 schema 的对应关系。
# 目标 schema 名刻意与 SQLite 文件名对齐 —— 业务 SQL 里那几百处
# `inspect_db.xxx` 跨库前缀因此一个字都不用改。
SOURCES = {
    "returns_db": "returns.db",
    "inspect_db": "inspect.db",
    "handle_db": "handle.db",
    "items_db": "items.db",
    "auth_db": "auth.db",
    "delivery_db": "delivery.db",
}

BATCH = 500
INT_TYPES = {"tinyint", "smallint", "mediumint", "int", "bigint", "bit"}
FLOAT_TYPES = {"float", "double", "decimal", "numeric", "real"}


class BadValue(Exception):
    """某个值转不成目标列的类型。"""


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------

def src_columns(src: sqlite3.Connection, table: str) -> list:
    """SQLite 侧的列名（按建表顺序）。"""
    return [r[1] for r in src.execute(f"PRAGMA table_info(`{table}`);")]


def dst_columns(conn, schema: str, table: str) -> list:
    """MySQL 侧 (列名, data_type, 字符上限) 三元组，按 ordinal 排序。"""
    rows = conn.execute(
        "SELECT column_name c, data_type t, character_maximum_length n "
        "FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? "
        "ORDER BY ordinal_position;", (schema, table)).fetchall()
    return [(r["c"], (r["t"] or "").lower(), r["n"]) for r in rows]


def src_max_lengths(src: sqlite3.Connection, table: str, cols: list) -> dict:
    """源表每列的字符最大长度。空表或全 NULL 返回 None。"""
    if not cols:
        return {}
    expr = ", ".join(f"MAX(LENGTH(CAST(`{c}` AS TEXT))) AS `{c}`" for c in cols)
    row = src.execute(f"SELECT {expr} FROM `{table}`;").fetchone()
    return {c: row[i] for i, c in enumerate(cols)}


def src_row_count(src: sqlite3.Connection, table: str) -> int:
    return src.execute(f"SELECT COUNT(*) FROM `{table}`;").fetchone()[0]


# ---------------------------------------------------------------------------
# 体检
# ---------------------------------------------------------------------------

def check_lengths(src, schema: str, table: str, src_cols: list,
                  dst: list) -> list:
    """返回超长列表 [(列, 源长度, 上限, 源里的最长样例), ...]。"""
    wanted = {c: (t, n) for c, t, n in dst}
    common = [c for c in src_cols if c in wanted]
    maxima = src_max_lengths(src, table, common)
    bad = []
    for col in common:
        limit = wanted[col][1]
        got = maxima.get(col)
        if limit is None or got is None or got <= limit:
            continue
        sample = src.execute(
            f"SELECT CAST(`{col}` AS TEXT) FROM `{table}` "
            f"WHERE LENGTH(CAST(`{col}` AS TEXT)) = ? LIMIT 1;", (got,)).fetchone()
        bad.append((col, got, limit, (sample or [""])[0]))
    return bad


# ---------------------------------------------------------------------------
# 值转换
# ---------------------------------------------------------------------------

def coerce(value, dtype: str):
    if value is None:
        return None
    if dtype in INT_TYPES:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            raise BadValue(f"{value!r} 不是整数")
    if dtype in FLOAT_TYPES:
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            raise BadValue(f"{value!r} 不是数字")
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


# ---------------------------------------------------------------------------
# 搬运
# ---------------------------------------------------------------------------

def migrate_table(conn, src, schema: str, table: str, apply: bool) -> dict:
    """搬一张表。返回统计字典。"""
    out = {"schema": schema, "table": table, "ok": False, "src": 0, "dst": None,
           "skipped_cols": [], "overlong": [], "error": ""}
    if table not in dbmod.tables_of(conn, schema):
        out["error"] = f"目标库里没有这张表（{schema}.{table}）"
        return out

    src_cols = src_columns(src, table)
    dst = dst_columns(conn, schema, table)
    if not dst:
        out["error"] = "读不到目标表结构"
        return out
    dst_names = [c for c, _, _ in dst]
    common = [c for c in dst_names if c in src_cols]
    out["skipped_cols"] = [c for c in src_cols if c not in dst_names]
    types = {c: t for c, t, _ in dst}

    out["src"] = src_row_count(src, table)
    out["overlong"] = check_lengths(src, schema, table, src_cols, dst)
    if out["overlong"]:
        out["error"] = "有列超出 MySQL 字符上限，先加宽列再搬"
        return out

    if not apply:
        out["dst"] = conn.execute(
            f"SELECT COUNT(*) c FROM `{schema}`.`{table}`;").fetchone()["c"]
        out["ok"] = True
        return out

    if not common:
        out["error"] = "源表与目标表没有共同列"
        return out

    started = time.time()
    conn.execute(f"TRUNCATE TABLE `{schema}`.`{table}`;")
    collist = ", ".join(f"`{c}`" for c in common)
    marks = ", ".join("?" * len(common))
    insert = f"INSERT INTO `{schema}`.`{table}` ({collist}) VALUES ({marks})"

    batch, done = [], 0
    cur = src.execute(f"SELECT {', '.join(f'`{c}`' for c in common)} "
                      f"FROM `{table}`;")
    try:
        for row in cur:
            try:
                batch.append(tuple(coerce(row[i], types[c])
                                   for i, c in enumerate(common)))
            except BadValue as exc:
                out["error"] = f"第 {done + len(batch) + 1} 行 {exc}"
                raise
            if len(batch) >= BATCH:
                conn.executemany(insert, batch)
                done += len(batch)
                batch = []
        if batch:
            conn.executemany(insert, batch)
            done += len(batch)
    except Exception as exc:                       # noqa: BLE001 —— 逐表兜底
        out["error"] = out["error"] or f"{type(exc).__name__}: {exc}"
        out["dst"] = conn.execute(
            f"SELECT COUNT(*) c FROM `{schema}`.`{table}`;").fetchone()["c"]
        return out

    out["dst"] = conn.execute(
        f"SELECT COUNT(*) c FROM `{schema}`.`{table}`;").fetchone()["c"]
    out["seconds"] = round(time.time() - started, 2)
    out["ok"] = out["dst"] == out["src"]
    if not out["ok"]:
        out["error"] = f"行数对不上：源 {out['src']} / 目标 {out['dst']}"
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite → MySQL 数据迁移")
    parser.add_argument("--apply", action="store_true",
                        help="真的写库（缺省只体检）")
    parser.add_argument("--only", default="",
                        help="只搬这些表（逗号分隔）")
    parser.add_argument("--source", default=str(DATA_DIR),
                        help="SQLite 文件所在目录")
    args = parser.parse_args()

    only = {t.strip() for t in args.only.split(",") if t.strip()}
    source_dir = Path(args.source)

    conn = dbmod.get_conn()
    info = conn.execute("SELECT VERSION() v, DATABASE() d;").fetchone()
    print(f"目标：MySQL {info['v']} · 默认 schema {info['d']} · "
          f"{'写入' if args.apply else '体检（不写）'}")

    # 目标表先按 DDL 建齐（幂等）—— 缺表的话后面每张都会报「读不到表结构」
    dbmod.init_db()

    results, failed = [], []
    for schema in SCHEMAS:
        name = SOURCES.get(schema)
        path = source_dir / name if name else None
        if path is None or not path.exists():
            print(f"[跳过] {name} 不存在")
            continue
        src = sqlite3.connect(str(path))
        try:
            for table in dbmod.SCHEMA_TABLES.get(schema, ()):
                if only and table not in only:
                    continue
                r = migrate_table(conn, src, schema, table, args.apply)
                results.append(r)
                flag = "OK " if r["ok"] else "FAIL"
                extra = ""
                if r["dst"] is not None:
                    extra = f"  {r['src']} → {r['dst']}"
                elif r["src"]:
                    extra = f"  {r['src']} 行"
                if r.get("seconds"):
                    extra += f"  {r['seconds']}s"
                print(f"[{flag}] {schema}.{table}{extra}")
                if r["skipped_cols"]:
                    print(f"       源有目标无（跳过）：{r['skipped_cols']}")
                for col, got, limit, sample in r["overlong"]:
                    print(f"       ⚠ {col} 最长 {got} 字符 > 上限 {limit}"
                          f"，样例 {str(sample)[:60]!r}")
                if r["error"]:
                    print(f"       ✗ {r['error']}")
                    failed.append(r)
        finally:
            src.close()

    total_src = sum(r["src"] for r in results)
    total_dst = sum(r["dst"] or 0 for r in results)
    print(f"\n合计 {len(results)} 张表：源 {total_src} 行 / "
          f"目标 {total_dst} 行 · 失败 {len(failed)} 张")
    for r in failed:
        print(f"  ✗ {r['schema']}.{r['table']}: {r['error']}")

    if args.apply:
        status = {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": not failed and total_src == total_dst,
            "tables": [{k: r[k] for k in
                        ("schema", "table", "src", "dst", "ok", "error")}
                       for r in results],
            "total_src": total_src,
            "total_dst": total_dst,
        }
        out = DATA_DIR / "migrate_status.json"
        out.write_text(json.dumps(status, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"状态写入 {out}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
