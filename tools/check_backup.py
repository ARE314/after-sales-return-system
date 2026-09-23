"""备份真实性守卫：把「备份能跑」升级为「备份能用」。

单独跑：python tools/check_backup.py
也被 tools/check_deploy.py 调用（作为部署自检的一环）。

守五件事：
  1. 最新备份有 DONE 标记（残缺备份不冒充可用）；
  2. dump 文件与 MANIFEST 的 sha256 / 字节数一致，且以 `-- Dump completed` 收尾；
  3. dump 里每个 schema 的每张表都有 `CREATE TABLE`，**有数据的表必须有
     `INSERT INTO`** —— 权限不足时 mysqldump 最常见的形态是「成功但只导了结构」；
  4. **真的能还原**：把 dump 里六个 schema 名改写成 `verify_<schema>`，用 mysql
     客户端导进影子库，再逐表比行数。备份的价值只在这一条上 —— sha256 只证明
     文件没被改动过，`-- Dump completed` 只证明 mysqldump 自己觉得跑完了；
  5. 照片数量一致（照片是磁盘文件，不在库里）。

**不写死任何具体数字**（如「100 条」）—— 数据会变，写死就会变成假失败。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from config import DATA_DIR, SCHEMAS                            # noqa: E402
from core.db import get_conn                                    # noqa: E402

BACKUP_ROOT = DATA_DIR / "backups"
# 还原演练把 dump 导进这些影子库，跑完立刻删掉；名字带前缀是为了
# 绝不和真库撞名 —— 演练要是把真库覆盖了，这个脚本就成了数据事故。
VERIFY_PREFIX = "verify_"

FAIL = []


def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def _load_backup_module():
    """复用 tools/backup.py 的实现，不复制一份。

    复制一份必然漂移：备份命令改了参数、核对脚本还按老参数核对，
    而且两边都「通过」。找客户端、写 cnf、解 dump 全部共用。
    """
    spec = importlib.util.spec_from_file_location(
        "_bk", ROOT / "tools" / "backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows_of(schema: str) -> dict:
    """{表名: 行数}，直接问 MySQL（information_schema + 逐表 COUNT）。"""
    conn = get_conn()
    names = [r["tbl"] for r in conn.execute(
        "SELECT table_name AS tbl FROM information_schema.tables "
        "WHERE table_schema = ? AND table_type = 'BASE TABLE' "
        "ORDER BY table_name;", (schema,))]
    return {n: conn.execute(
        f"SELECT COUNT(*) AS c FROM `{schema}`.`{n}`;").fetchone()["c"]
        for n in names}


def _shadow_text(text: str, schemas) -> str:
    """把 dump 里的 `` `returns_db` `` 全部改写成 `` `verify_returns_db` ``。

    mysqldump 在 `--databases` 模式下把库名写进 `CREATE DATABASE` 与 `USE`
    两种语句里，都带反引号；连带反引号一起替换才不会误伤表名/字段里的同名片段。
    """
    for schema in schemas:
        text = text.replace(f"`{schema}`", f"`{VERIFY_PREFIX}{schema}`")
    return text


def _restore_drill(bkp, man: dict, dump: Path) -> None:
    schemas = [s for s, _label in bkp.DATABASES]
    shadows = [VERIFY_PREFIX + s for s in schemas]
    try:
        mysql = bkp._tool("mysql")
    except FileNotFoundError as exc:
        check("找到 mysql 客户端（还原演练要用）", False, str(exc))
        return

    conn = get_conn()
    for name in shadows:                       # 清掉上次演练的残骸
        try:
            conn.execute(f"DROP DATABASE IF EXISTS `{name}`;")
        except Exception:                                       # noqa: BLE001
            pass

    sql_path = DATA_DIR / ".verify_restore.sql"
    log_path = DATA_DIR / ".verify_restore.err"
    claims = {d["schema"]: d["tables"] for d in man["databases"]}
    try:
        # 改写后的 SQL 落在 data/ 而不是系统临时目录：部署机上临时目录可能
        # 被沙箱/杀软管控（与 backup.py 的 .cnf 同一个理由）。跑完必删。
        sql_path.write_text(
            _shadow_text(dump.read_text(encoding="utf-8", errors="replace"),
                         schemas),
            encoding="utf-8")
        cnf = bkp._write_cnf()
        try:
            # stdin/stdout 都指向真实文件句柄，不用管道（受限环境不能开匿名管道）
            with open(sql_path, "rb") as fin, open(log_path, "wb") as fout:
                proc = subprocess.run(
                    [mysql, f"--defaults-extra-file={cnf}",
                     "--max-allowed-packet=1G"],
                    stdin=fin, stdout=fout, stderr=subprocess.STDOUT)
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-400:].strip()
        finally:
            cnf.unlink(missing_ok=True)

        check("备份能导入 MySQL（影子库演练）", proc.returncode == 0,
              tail.replace("\n", " ")[:300] if proc.returncode else "")
        if proc.returncode != 0:
            # 权限不足是最常见的失败原因，而且报错本身（1044/1142）不会告诉
            # 使用者该补哪一句 GRANT —— 直接把命令打出来。
            if any(k in tail for k in ("Lost connection", "Can't connect",
                                       "2003", "2013")):
                print("        ⚠️ MySQL 掉线了 —— 先 `python tools/dev_mysql.py start` 再重跑")
            if any(k in tail for k in ("Access denied", "1044", "1142")):
                print("        ⚠️ 应用账号没有建/删影子库的权限，补一次：")
                print(f"        GRANT ALL PRIVILEGES ON `{VERIFY_PREFIX}\\_%`.* "
                      f"TO '{bkp.MYSQL_USER}'@'127.0.0.1';")
                print("        （跑 python tools/dev_mysql.py grant 会自动补齐）")
            return

        for schema, shadow in zip(schemas, shadows):
            want = claims.get(schema, {})
            got = _rows_of(shadow)
            missing = sorted(set(want) - set(got))
            diff = [f"{t} 清单 {want[t]} / 还原 {got[t]}"
                    for t in sorted(set(want) & set(got)) if want[t] != got[t]]
            check(f"{shadow} 逐表行数与备份清单一致",
                  not missing and not diff,
                  ("缺表 " + ", ".join(missing)) if missing
                  else ("；".join(diff) if diff
                        else f"{len(want)} 张表 / {sum(want.values())} 行"))
            # 与现库的差异只提示不算失败：应用在跑，备份之后还在写。
            live = _rows_of(schema)
            drift = [f"{t} 现库 {live.get(t, 0)}" for t in sorted(want)
                     if live.get(t, 0) != want[t]]
            if drift:
                print(f"        （现库已变化，正常：{'；'.join(drift[:4])}"
                      f"{' …' if len(drift) > 4 else ''}）")
    finally:
        sql_path.unlink(missing_ok=True)
        log_path.unlink(missing_ok=True)
        for name in shadows:                   # 演练残骸绝不留下
            try:
                get_conn().execute(f"DROP DATABASE IF EXISTS `{name}`;")
            except Exception:                                   # noqa: BLE001
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="核对最新备份是否真的能用")
    ap.add_argument("--no-restore", action="store_true",
                    help="跳过影子库还原演练（只做清单/结构核对）")
    args = ap.parse_args(argv)

    print("== 备份真实性核对 ==")
    if not BACKUP_ROOT.is_dir():
        print(f"  [SKIP] 还没有备份目录（{BACKUP_ROOT}）。"
              f"先跑 python tools/backup.py，或启用定时备份。")
        return 0

    stamps = sorted(p for p in BACKUP_ROOT.iterdir()
                    if p.is_dir() and p.name[:8].isdigit())
    if not stamps:
        print("  [SKIP] 备份目录里没有本工具产出的时间戳快照")
        return 0

    newest = stamps[-1]
    print(f"  最新备份：{newest.name}")

    check("有 DONE 完成标记（全部成功才写）", (newest / "DONE").is_file())
    man_path = newest / "MANIFEST.json"
    check("有 MANIFEST.json", man_path.is_file())
    if not man_path.is_file():
        return 1
    man = json.loads(man_path.read_text(encoding="utf-8"))

    # 连不上 MySQL 时后面每一步都会炸成一堆 traceback；先明确报一句。
    try:
        get_conn()
    except Exception as exc:                                    # noqa: BLE001
        check("能连上 MySQL（备份核对的前提）", False,
              f"{type(exc).__name__}: {exc}")
        print("        ⚠️ MySQL 没在跑？先 `python tools/dev_mysql.py start`")
        return 1

    bkp = _load_backup_module()
    check("清单覆盖全部库（与 config.SCHEMAS 一致）",
          {d["schema"] for d in man["databases"]} == set(SCHEMAS),
          ", ".join(sorted(d["schema"] for d in man["databases"])))

    dump = newest / man.get("dump", {}).get("file", bkp.DUMP_NAME)
    check("dump 文件存在", dump.is_file(), dump.name)
    if not dump.is_file():
        return 1

    size = dump.stat().st_size
    check("dump 字节数与清单一致", size == man["dump"].get("bytes"),
          f"{size} 字节")
    digest = hashlib.sha256()
    with open(dump, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    check("dump sha256 与清单一致", digest.hexdigest() == man["dump"].get("sha256"))

    try:
        bkp._check_completed(dump)
        check("dump 以 `-- Dump completed` 收尾（不是中途死掉的半截）", True)
    except RuntimeError as exc:
        check("dump 以 `-- Dump completed` 收尾（不是中途死掉的半截）",
              False, str(exc))

    # 逐表核对结构 + 数据：只看退出码区分不出「成功但只导了结构」
    found = bkp._dump_tables(dump)
    want_all = {d["schema"]: d["tables"] for d in man["databases"]}
    for schema, want in want_all.items():
        got = found.get(schema)
        if got is None:
            check(f"{schema} 在 dump 里有 USE 段", False)
            continue
        missing = sorted(set(want) - got["created"])
        empty = sorted(t for t, n in want.items()
                       if n and t not in got["inserted"])
        check(f"{schema} 表结构与数据都在 dump 里",
              not missing and not empty,
              (("缺结构 " + ", ".join(missing)) if missing
               else ("有数据却无 INSERT：" + ", ".join(empty)) if empty
               else f"{len(want)} 张表 / {sum(want.values())} 行"))

    if args.no_restore:
        print("  [SKIP] 还原演练（--no-restore）")
    else:
        _restore_drill(bkp, man, dump)

    # 照片：库里有照片记录时，备份里必须有对应目录
    n_live = sum(1 for p in (DATA_DIR / "photos").rglob("*") if p.is_file()) \
        if (DATA_DIR / "photos").is_dir() else 0
    n_bk = sum(1 for p in (newest / "photos").rglob("*") if p.is_file()) \
        if (newest / "photos").is_dir() else 0
    check("照片数量一致（现库 vs 备份）", n_live == n_bk,
          f"现库 {n_live} / 备份 {n_bk}")

    print()
    if FAIL:
        print(f"未通过 {len(FAIL)} 项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
