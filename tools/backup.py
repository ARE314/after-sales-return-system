"""数据备份工具 —— 六个 schema + 照片，一致性快照 + 轮转 + 状态留痕

为什么不能直接拷 `data/`
------------------------
2026-09-22 起数据**不再**放在 `data/` 下的文件里，而在本机 MySQL 8 实例里
（六个 schema：returns_db / inspect_db / handle_db / delivery_db / items_db /
auth_db）。直接拷 `data/` 只能拿到一份空壳目录 —— 而 `data/photos/` 是照片
原图 + 缩略图，**不在库里**，所以照片仍然必须单独拷，漏了它就是
「记录还在、图全丢」。

所以这里用 `mysqldump --single-transaction`：在**一个可重复读事务**里导出
六个库，等价于旧版 SQLite 的 `VACUUM INTO` —— 服务照常在写，导出的却是一个
时间点一致的快照，不用锁表、不用停服。

几个关键参数（都不是可有可无的）
--------------------------------
    --single-transaction     InnoDB 一致性快照。没有它就会 `LOCK TABLES`，
                             等于备份期间整个系统只读。
    --databases             把 `CREATE DATABASE` / `USE` 一起写进 dump，
                             恢复只要一条命令，不用先手工建库。
    --hex-blob              二进制列（照片缩略图、任意 BLOB）按十六进制写，
                             否则 dump 不是纯文本，跨平台恢复会坏。
    --default-character-set 显式 utf8mb4。中文列按库默认导出，换台机器
                             （服务端默认 latin1）恢复就会变问号。
    --set-gtid-purged=OFF   没有主从复制时省掉 GTID 警告。

⚠️ 密码为什么不写在命令行：`mysqldump -p<密码>` 会让密码出现在进程表里
（同机任何用户 `ps` / 任务管理器都能看到）。`MYSQL_PWD` 环境变量则对子进程
可读。mysqldump 唯一安全的口子就是 `--defaults-extra-file` —— 一个权限收紧、
用完即删的临时配置文件。

产出结构
--------
    data/backups/20260922-113000/
        all.sql                     ← 六个 schema 的完整 dump
        photos/                     ← 整个照片目录
        MANIFEST.json               ← dump 的 sha256/字节数 + 每库表与行数 + 来源
        DONE                        ← 完成标记（**只在全部成功后写**）

`DONE` 的意义：定时任务跑备份时没人盯着，如果只靠「目录存在」判断成功，
一次中途失败（磁盘满、MySQL 掉线）会留下一个看起来正常的残缺备份，
**恢复时才发现少了一个库**。只有见到 `DONE` 才算可用。

「dump 成功」也要核对，不能只看退出码
------------------------------------
mysqldump 可以「成功」地少导一个库、或者只导了结构没导数据 —— 两种都要等到
恢复时才发现。所以这里除了检查结尾的 `-- Dump completed` 标记，还会**逐表**
核对：库里有的表在 dump 里必须有 `CREATE TABLE`，库里行数 > 0 的表必须出现
`INSERT INTO`。旧版 SQLite 是在备份文件上逐表 `COUNT(*)` 比对，同一个思路。

轮转
----
默认保留最近 7 份（`--keep` 可调）。只轮转本工具管理的目录（名字匹配
`YYYYMMDD-HHMMSS` 且含 `DONE` 或 `MANIFEST.json`），**不碰**人工建的
`backup_*/` 快照 —— 误删人工备份是不可接受的。

状态留痕（供界面/自检读）
------------------------
每跑一次都会更新：
    data/backup_status.json            ← 最近一次结果（成功/失败、路径、字节数、耗时）
    auth_db.setting['last_backup_at']  ← 成功时间，便于 SQL 侧核对
界面据此提示「备份已超期」，`tools/check_deploy.py` 也据此核对。

恢复（应急手册）
----------------
    mysql -h 127.0.0.1 -u ars -p < data/backups/<时间戳>/all.sql
    # 照片：把 photos/ 整个拷回 data/photos/
    # 若换了一台机器，先跑 `python tools/dev_mysql.py setup` 建账号与授权
用 `tools/check_backup.py` 做恢复演练（导进影子 schema 逐表比行数），
**只有演练通过过的备份才算真的可用**。

用法
----
    python tools/backup.py                      # 备份到 data/backups/
    python tools/backup.py --keep 14            # 保留 14 份
    python tools/backup.py --out /mnt/nas/ars   # 备份到别处（异地）
    python tools/backup.py --dry-run            # 只看会做什么
    python tools/backup.py --list               # 列出已有备份

退出码：0 成功 / 1 失败（cron、systemd 直接据此告警）。
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import (AUTH_DB, DATA_DIR, DELIVERY_DB, HANDLE_DB,  # noqa: E402
                    INSPECT_DB, ITEMS_DB, MYSQL_HOST, MYSQL_PASSWORD,
                    MYSQL_PORT, MYSQL_USER, PHOTO_DIR, RETURNS_DB)
from core.db import get_conn                                   # noqa: E402

BACKUP_ROOT = DATA_DIR / "backups"
STATUS_FILE = DATA_DIR / "backup_status.json"
# 只认本工具产出的目录名：YYYYMMDD-HHMMSS 或撞车时的 YYYYMMDD-HHMMSS-N。
# 人工建的 backup_* 快照**不匹配**，因此永远不会被轮转删掉。
STAMP_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")

# 六个库：schema 名 → 说明（人工核对 MANIFEST 时用）
#
# ⚠️ 新增模块时**别忘了往这里加** —— 漏了不会报错，备份照常「成功」，
#    只是新库不在快照里，等真要恢复时才发现。config.SCHEMAS 也在这里定义，
#    两处对不上时下面的自查会报错（见 _assert_schemas_registered）。
DATABASES = [
    (RETURNS_DB, "退回登记库"),
    (INSPECT_DB, "检测登记库"),
    (HANDLE_DB, "处理登记库"),
    (DELIVERY_DB, "发货库（申请 + 明细）"),
    (ITEMS_DB, "匹配数据库"),
    (AUTH_DB, "接入库（唯一不可再生）"),
]

DUMP_NAME = "all.sql"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _assert_schemas_registered() -> None:
    """config.SCHEMAS 与本文件的 DATABASES 必须一致。

    这两处是「新增一个库要改的四处」里最容易漏的两处，而且漏了**不报错**：
    config 加了、这里没加 → 备份少一个库；这里加了、config 没加 → 备份
    命令去导一个不存在的库，mysqldump 直接失败（还算幸运的）。
    干脆在开工前自查一次。
    """
    from config import SCHEMAS
    missing = sorted(set(SCHEMAS) - {s for s, _ in DATABASES})
    extra = sorted({s for s, _ in DATABASES} - set(SCHEMAS))
    if missing or extra:
        raise RuntimeError(
            f"config.SCHEMAS 与 tools/backup.py 的 DATABASES 不一致："
            f"缺 {missing or '无'} / 多 {extra or '无'}。两处都要改。")


def _tool(name: str) -> str:
    """定位 mysqldump / mysql 可执行文件。

    优先级：环境变量（ARS_MYSQLDUMP / ARS_MYSQL）> PATH > 常见安装位置。
    部署机上客户端可能在 `/usr/bin`，本机在便携版解包目录里 —— 写死任一个
    都会在另一台机器上找不到，所以按顺序找，全找不到就报明确的错。
    """
    env = os.getenv(f"ARS_{name.upper()}", "").strip()
    if env:
        if not Path(env).exists():
            raise FileNotFoundError(
                f"ARS_{name.upper()} 指向的文件不存在：{env}")
        return env
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        patterns = [
            r"C:\Program Files\MySQL\MySQL Server *\bin",
            r"C:\Program Files (x86)\MySQL\MySQL Server *\bin",
            str(Path.home() / ".workbuddy" / "binaries" / "mysql"),
        ]
        for pattern in patterns:
            for hit in sorted(glob.glob(pattern)):
                base = Path(hit)
                # 便携版解包后还可能再套一层：mysql-8.0.29-winx64\bin
                for cand in [base / f"{name}.exe",
                             *sorted(base.glob(f"*/bin/{name}.exe"))]:
                    if cand.is_file():
                        return str(cand)
    raise FileNotFoundError(
        f"找不到 {name}。请把它加进 PATH，或用 ARS_{name.upper()} 指定完整路径"
        f"（例如 ARS_MYSQLDUMP=/usr/bin/mysqldump）。")


def _live_rows() -> dict:
    """{schema: {表名: 行数}}，直接问 MySQL。

    逐表登记而不是抽查一张「代表表」：库是可以只有某张新表有数据的，
    抽查到一张空表时 0 == 0 恒真，防「空壳备份」的断言就成了摆设。
    （旧版 SQLite 是自己扫 sqlite_master 再 COUNT(*)，这里换成
      information_schema.tables + 逐表 COUNT(*)。）
    """
    conn = get_conn()
    out: dict = {}
    for schema, _label in DATABASES:
        names = [r["tbl"] for r in conn.execute(
            "SELECT table_name AS tbl FROM information_schema.tables "
            "WHERE table_schema = ? AND table_type = 'BASE TABLE' "
            "ORDER BY table_name;", (schema,))]
        out[schema] = {
            n: conn.execute(
                f"SELECT COUNT(*) AS c FROM `{schema}`.`{n}`;").fetchone()["c"]
            for n in names
        }
    return out


def _write_cnf() -> Path:
    """写一个权限收紧、用完即删的临时客户端配置（见模块 docstring 的密码说明）。

    ⚠️ 为什么不放系统临时目录：部署机上那可能是被沙箱/杀软管控的位置，
    写不进去会让备份直接失败。`data/` 一定可写，而且已被 .gitignore 覆盖，
    也不会落在备份目录里（所以密码不会跟着 dump 一起被拷走）。
    """
    path = DATA_DIR / ".backup_mysql.cnf"
    path.write_text(
        "[client]\n"
        f"host={MYSQL_HOST}\n"
        f"port={MYSQL_PORT}\n"
        f"user={MYSQL_USER}\n"
        f"password={MYSQL_PASSWORD}\n"
        "default-character-set=utf8mb4\n",
        encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _tree_bytes(path: Path) -> tuple:
    """目录的 (文件数, 字节数)。"""
    if not path.is_dir():
        return 0, 0
    n = size = 0
    for p in path.rglob("*"):
        if p.is_file():
            n += 1
            size += p.stat().st_size
    return n, size


def _run_dump(tool: str, cnf: Path, target: Path) -> None:
    """跑 mysqldump，stdout 直接落盘。

    ⚠️ 不要用管道收集输出：stdout/stderr 都指向真实文件句柄，既避免了
    「管道缓冲把大 dump 顶爆」，也绕开了受限环境里不能开匿名管道的问题。
    """
    cmd = [
        tool,
        f"--defaults-extra-file={cnf}",
        "--single-transaction",
        "--hex-blob",
        "--default-character-set=utf8mb4",
        "--set-gtid-purged=OFF",
        "--databases",
        *[schema for schema, _ in DATABASES],
    ]
    err_path = target.with_suffix(target.suffix + ".err")
    try:
        with open(target, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.run(cmd, stdout=out, stderr=err, check=False)
        if proc.returncode != 0:
            msg = err_path.read_text(encoding="utf-8", errors="replace").strip()
            raise RuntimeError(
                f"mysqldump 退出码 {proc.returncode}：{msg[:500] or '（无错误输出）'}")
    finally:
        err_path.unlink(missing_ok=True)


def _check_completed(path: Path, tail: int = 8192) -> None:
    """确认 dump 真的跑完了。

    mysqldump 成功时会在最后一行写 `-- Dump completed on ...`。中途被杀
    （磁盘满、MySQL 掉线、OOM）时文件可能已经有几百 MB，光看「文件存在且
    不小」根本区分不出来，所以认这个结尾标记。
    """
    size = path.stat().st_size
    if size < 1024:
        raise RuntimeError(f"dump 只有 {size} 字节，明显不完整")
    with open(path, "rb") as fh:
        fh.seek(max(0, size - tail))
        chunk = fh.read().decode("utf-8", "replace")
    if "-- Dump completed" not in chunk:
        raise RuntimeError(
            "dump 结尾没有 `-- Dump completed` 标记，说明 mysqldump 中途失败")


def _dump_tables(path: Path) -> dict:
    """从 dump 文本解出 {schema: {"created": set, "inserted": set}}。

    按 `USE \\`schema\\`;` 分段统计 `CREATE TABLE` / `INSERT INTO`。逐表核对，
    而不是只看退出码 —— 见模块 docstring 的「dump 成功也要核对」。
    """
    out: dict = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        cur = None
        for line in fh:
            if line.startswith("USE `"):
                parts = line.split("`")
                if len(parts) > 1:
                    cur = parts[1]
                    out.setdefault(cur, {"created": set(), "inserted": set()})
            elif cur is None:
                continue
            elif line.startswith("CREATE TABLE `"):
                out[cur]["created"].add(line.split("`")[1])
            elif line.startswith("INSERT INTO `"):
                out[cur]["inserted"].add(line.split("`")[1])
    return out


def _write_status(ok: bool, **kw) -> None:
    """把最近一次结果写到 JSON（界面的「备份是否超期」靠它）。"""
    payload = {"ok": ok, "at": _now(), "tool": "tools/backup.py", **kw}
    try:
        STATUS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[warn] 状态文件写入失败：{exc}", file=sys.stderr)
    if not ok:
        return
    # 成功时同步写一份到 auth_db.setting，方便用 SQL 核对（失败不影响主流程）
    try:
        get_conn().execute(
            "INSERT INTO auth_db.setting(`key`, value, updated_at) "
            "VALUES (?,?,?) "
            "ON DUPLICATE KEY UPDATE value = VALUES(value), "
            "updated_at = VALUES(updated_at);",
            ("last_backup_at", payload["at"], payload["at"]))
    except Exception as exc:                                    # noqa: BLE001
        print(f"[warn] 写 auth_db.setting.last_backup_at 失败：{exc}",
              file=sys.stderr)


def _existing_backups(out_root: Path) -> list:
    """只认本工具产出的目录（时间戳命名）。人工建的 backup_* 不参与轮转。"""
    if not out_root.is_dir():
        return []
    out = []
    for p in out_root.iterdir():
        if p.is_dir() and STAMP_RE.match(p.name):
            out.append(p)
    return sorted(out, key=lambda p: p.name)


def _unique_dir(out_root: Path, stamp: str) -> Path:
    """时间戳撞车时自动加后缀。

    只精确到秒，同一秒内连跑两次（测试、或手工紧接着重跑）会撞名 ——
    原来用 `exist_ok=False` 直接抛 FileExistsError，把「重名」报成了失败。
    现在最多试 20 个后缀，再不行才放弃。
    """
    for i in range(20):
        cand = out_root / (stamp if i == 0 else f"{stamp}-{i}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"{stamp} 附近已有 20 个同名前缀的备份，请清理 {out_root}")


def cmd_list(out_root: Path) -> int:
    rows = _existing_backups(out_root)
    if not rows:
        print(f"（{out_root} 下没有本工具产出的备份）")
        return 0
    print(f"{'备份':18s} {'完整':4s} {'大小':>12s}  说明")
    for p in rows:
        done = (p / "DONE").is_file()
        dump = p / DUMP_NAME
        _n, size = _tree_bytes(p)
        note = "可用" if done else "**不完整**（缺 DONE，别用来恢复）"
        if done and not dump.is_file():
            note = "**不完整**（没有 " + DUMP_NAME + "）"
        print(f"{p.name:18s} {'是' if done else '否':4s} {size / 1048576:10.2f}MB  {note}")
    return 0


def cmd_backup(out_root: Path, keep: int, dry: bool) -> int:
    started = time.time()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = _unique_dir(out_root, stamp)
    print(f"备份目标：{dest}")
    print(f"保留份数：{keep}（只轮转本工具产出的时间戳目录）")

    _assert_schemas_registered()
    live = _live_rows()
    grand = sum(sum(t.values()) for t in live.values())

    if dry:
        print("\n[dry-run] 将会执行：")
        for schema, label in DATABASES:
            rows = live.get(schema, {})
            print(f"  mysqldump → {schema:14s} {len(rows):2d} 张表 / "
                  f"{sum(rows.values()):>7d} 行  ({label})")
        n, size = _tree_bytes(PHOTO_DIR)
        print(f"     合计 {grand} 行")
        print(f"  复制照片目录 → photos/  ({n} 个文件 / {size / 1048576:.2f} MB)")
        print("  写 MANIFEST.json 与 DONE")
        print(f"  轮转到最近 {keep} 份")
        return 0

    dest.mkdir(parents=True, exist_ok=False)
    # 主机名：备份可能来自多台机器（本地 + NAS），清单里记一笔便于分辨来源。
    # 用 socket.gethostname() 而不是 os.uname()（后者 Windows 上没有）。
    manifest = {"created_at": _now(), "host": socket.gethostname(),
                "source": f"mysql://{MYSQL_USER}@{MYSQL_HOST}:{MYSQL_PORT}",
                "dump": {}, "databases": [], "photos": {}}
    total = 0
    cnf = None
    try:
        cnf = _write_cnf()
        tool = _tool("mysqldump")

        # ---- 1. 六个库：一致性快照 + 逐表核对 ----
        t0 = time.time()
        target = dest / DUMP_NAME
        _run_dump(tool, cnf, target)
        _check_completed(target)
        size = target.stat().st_size
        total += size
        found = _dump_tables(target)

        for schema, label in DATABASES:
            rows = live.get(schema, {})
            got = found.get(schema)
            if got is None:
                raise RuntimeError(
                    f"dump 里没有 {schema} 的 USE 段 —— 整个库都没导出来")
            missing = sorted(set(rows) - got["created"])
            if missing:
                raise RuntimeError(
                    f"{schema} 缺表结构：{', '.join(missing)}")
            # 有数据的表必须真的落进 dump（只导结构不导数据是 mysqldump
            # 在权限不足时最常见的「成功但没用」形态）
            empty = sorted(t for t, n in rows.items()
                           if n and t not in got["inserted"])
            if empty:
                raise RuntimeError(
                    f"{schema} 这些表有数据却没有 INSERT：{', '.join(empty)}")
            manifest["databases"].append({
                "schema": schema, "label": label,
                "tables": rows, "rows": sum(rows.values()),
                "tables_in_dump": len(got["created"]),
            })
            print(f"  [OK] {schema:14s} {len(rows):2d} 张表 · "
                  f"{sum(rows.values()):>7d} 行  ({label})")

        manifest["dump"] = {
            "file": DUMP_NAME, "bytes": size, "rows": grand,
            "sha256": _sha256(target), "seconds": round(time.time() - t0, 2),
        }

        # ---- 2. 照片（磁盘文件，不在库里）----
        n_photo, size_photo = _tree_bytes(PHOTO_DIR)
        if n_photo:
            shutil.copytree(PHOTO_DIR, dest / "photos")
            total += size_photo
            print(f"  [OK] photos/        {size_photo / 1048576:7.2f} MB  "
                  f"{n_photo} 个文件（原图 + 缩略图）")
        else:
            print("  [--] photos/        空（没有照片需要备份）")
        manifest["photos"] = {"files": n_photo, "bytes": size_photo}

        # ---- 3. 清单 + 完成标记 ----
        manifest["total_bytes"] = total
        manifest["seconds"] = round(time.time() - started, 2)
        (dest / "MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        # DONE 最后写：它的存在 = 「这份备份完整可用」
        (dest / "DONE").write_text(_now() + "\n", encoding="utf-8")
    except Exception as exc:                                    # noqa: BLE001
        print(f"\n[ERROR] 备份失败：{exc}", file=sys.stderr)
        print(f"        不完整的目录保留在 {dest}（没有 DONE 标记，"
              f"**不要**拿它恢复；确认后手工删除）", file=sys.stderr)
        _write_status(False, error=str(exc)[:300], path=str(dest))
        return 1
    finally:
        # 密码文件无论成败都要删掉
        if cnf is not None:
            cnf.unlink(missing_ok=True)

    # ---- 4. 轮转 ----
    rows = _existing_backups(out_root)
    dropped = []
    if keep > 0 and len(rows) > keep:
        for old in rows[:len(rows) - keep]:
            # 双保险：只删本工具产出的目录
            if not (STAMP_RE.match(old.name) and old.parent == out_root):
                continue
            shutil.rmtree(old, ignore_errors=True)
            dropped.append(old.name)
    if dropped:
        print(f"  轮转：删除 {len(dropped)} 份旧备份 → {', '.join(dropped)}")

    elapsed = round(time.time() - started, 2)
    _write_status(True, path=str(dest), bytes=total,
                  databases=len(manifest["databases"]), rows=grand,
                  photos=manifest["photos"]["files"],
                  seconds=elapsed, rotated=dropped, keep=keep)
    print(f"\n完成：{dest}")
    print(f"  合计 {total / 1048576:.2f} MB · {grand} 行 · 耗时 {elapsed}s · "
          f"现有备份 {len(_existing_backups(out_root))} 份")
    print(f"  恢复：mysql -h {MYSQL_HOST} -u {MYSQL_USER} -p < "
          f"{dest / DUMP_NAME}")
    return 0


def main() -> int:
    # 保留份数：命令行 > 环境变量 ARS_BACKUP_KEEP > 7。
    # 为什么要支持环境变量：systemd 的 ExecStart **不做变量展开**，
    # `--keep ${ARS_BACKUP_KEEP}` 会把字面量传进来。所以配置项走环境变量、
    # 由脚本自己读，unit 里就不用写 shell。
    default_keep = 7
    env_keep = os.getenv("ARS_BACKUP_KEEP", "").strip()
    if env_keep:
        try:
            default_keep = int(env_keep)
        except ValueError:
            print(f"[warn] ARS_BACKUP_KEEP={env_keep!r} 不是整数，用默认 {default_keep}",
                  file=sys.stderr)

    ap = argparse.ArgumentParser(
        description="六库 + 照片的一致性备份（mysqldump --single-transaction，"
                    "服务在跑也能用）")
    ap.add_argument("--out", default=os.getenv("ARS_BACKUP_OUT") or str(BACKUP_ROOT),
                    help=f"备份根目录（默认 {BACKUP_ROOT}，可用 ARS_BACKUP_OUT 覆盖）")
    ap.add_argument("--keep", type=int, default=default_keep,
                    help=f"保留份数（默认 {default_keep}，可用 ARS_BACKUP_KEEP 覆盖；0=不轮转）")
    ap.add_argument("--dry-run", action="store_true", help="只显示会做什么")
    ap.add_argument("--list", action="store_true", help="列出已有备份后退出")
    args = ap.parse_args()

    out_root = Path(args.out).expanduser()
    if args.list:
        return cmd_list(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    return cmd_backup(out_root, args.keep, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
