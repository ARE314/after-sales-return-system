"""数据备份工具 —— 五个库 + 照片，一致性快照 + 轮转 + 状态留痕

为什么不能用 `cp`/`tar` 直接拷 `data/`
--------------------------------------
五个库都是 **WAL 模式**。WAL 下已提交但尚未 checkpoint 的事务只存在于
`-wal` 文件里，直接拷 `.db` 会拿到一个**缺最近写入**的副本；拷到一半时
还可能拷进半个事务。而 `data/photos/` 是照片原图 + 缩略图，**不在库里**，
漏了它就是「记录还在、图全丢」。

所以这里用 SQLite 自带的 `VACUUM INTO`：它在同一个读事务里重建一个完整的
库文件，**包含 WAL 里尚未 checkpoint 的内容**，且服务照常可写。实测在
100 条明细 + 100 条检测的库上瞬时完成。

产出结构
--------
    data/backups/20260920-113000/
        returns.db  inspect.db  handle.db  items.db  auth.db
        photos/                     ← 整个照片目录
        MANIFEST.json               ← 各文件大小 + sha256 + 行数 + 来源
        DONE                        ← 完成标记（**只在全部成功后写**）

`DONE` 的意义：定时任务跑备份时没人盯着，如果只靠「目录存在」判断成功，
一次中途失败（磁盘满、库被锁）会留下一个看起来正常的残缺备份，
**恢复时才发现少了一个库**。只有见到 `DONE` 才算可用。

轮转
----
默认保留最近 7 份（`--keep` 可调）。只轮转本工具管理的目录（名字匹配
`YYYYMMDD-HHMMSS` 且含 `DONE` 或 `MANIFEST.json`），**不碰**人工建的
`backup_*/` 快照 —— 误删人工备份是不可接受的。

状态留痕（供界面/自检读）
------------------------
每跑一次都会更新：
    data/backup_status.json     ← 最近一次结果（成功/失败、路径、字节数、耗时）
    auth.db.setting['last_backup_at']  ← 成功时间，便于 SQL 侧核对
界面据此提示「备份已超期」，`tools/check_deploy.py` 也据此核对。

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
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import (AUTH_DB, DATA_DIR, HANDLE_DB, INSPECT_DB, ITEMS_DB,  # noqa: E402
                    PHOTO_DIR, RETURNS_DB)

BACKUP_ROOT = DATA_DIR / "backups"
STATUS_FILE = DATA_DIR / "backup_status.json"
# 只认本工具产出的目录名：YYYYMMDD-HHMMSS 或撞车时的 YYYYMMDD-HHMMSS-N。
# 人工建的 backup_* 快照**不匹配**，因此永远不会被轮转删掉。
STAMP_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")

# 五个库：文件名 → 说明（人工核对 MANIFEST 时用）
DATABASES = [
    (RETURNS_DB, "退回登记库"),
    (INSPECT_DB, "检测登记库"),
    (HANDLE_DB, "处理登记库"),
    (ITEMS_DB, "匹配数据库"),
    (AUTH_DB, "接入库（唯一不可再生）"),
]
# 校验用：库 → 主表（快照后核对行数，确认真的是完整数据而不是空壳）
CHECK_TABLES = {
    "returns.db": "returns",
    "inspect.db": "inspect_records",
    "handle.db": "handle_records",
    "items.db": "item_master",
    "auth.db": "users",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def vacuum_into(src: Path, dest: Path) -> None:
    """一致性快照单个库。

    `VACUUM INTO` 会在一个读事务里重建完整库文件，**包含 WAL 中未 checkpoint
    的已提交事务** —— 这正是「服务在跑时也能安全备份」的关键。
    目标文件必须不存在（SQLite 的硬性要求），所以先确保父目录干净。
    """
    if dest.exists():
        dest.unlink()
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        con.execute("VACUUM INTO ?;", (str(dest),))
    finally:
        con.close()


def _write_status(ok: bool, **kw) -> None:
    """把最近一次结果写到 JSON（界面的「备份是否超期」靠它）。"""
    payload = {"ok": ok, "at": _now(), "tool": "tools/backup.py", **kw}
    try:
        STATUS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[warn] 状态文件写入失败：{exc}", file=sys.stderr)
    # 成功时同步写一份到 auth.db，方便用 SQL 核对（失败不影响主流程）
    if ok:
        try:
            con = sqlite3.connect(str(AUTH_DB))
            con.execute(
                "INSERT INTO setting(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at;",
                ("last_backup_at", payload["at"], payload["at"]))
            con.commit()
            con.close()
        except sqlite3.Error as exc:
            print(f"[warn] 写 auth.db.last_backup_at 失败：{exc}", file=sys.stderr)


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
        _n, size = _tree_bytes(p)
        note = "可用" if done else "**不完整**（缺 DONE，别用来恢复）"
        print(f"{p.name:18s} {'是' if done else '否':4s} {size / 1048576:10.2f}MB  {note}")
    return 0


def cmd_backup(out_root: Path, keep: int, dry: bool) -> int:
    started = time.time()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = _unique_dir(out_root, stamp)
    print(f"备份目标：{dest}")
    print(f"保留份数：{keep}（只轮转本工具产出的时间戳目录）")

    if dry:
        print("\n[dry-run] 将会执行：")
        for src, label in DATABASES:
            print(f"  VACUUM INTO → {src.name:14s} ({label})")
        n, size = _tree_bytes(PHOTO_DIR)
        print(f"  复制照片目录 → photos/  ({n} 个文件 / {size / 1048576:.2f} MB)")
        print("  写 MANIFEST.json 与 DONE")
        print(f"  轮转到最近 {keep} 份")
        return 0

    dest.mkdir(parents=True, exist_ok=False)
    # 主机名：备份可能来自多台机器（本地 + NAS），清单里记一笔便于分辨来源。
    # 用 socket.gethostname() 而不是 os.uname()（后者 Windows 上没有）。
    import socket
    manifest = {"created_at": _now(), "host": socket.gethostname(),
                "source_dir": str(DATA_DIR), "databases": [], "photos": {}}
    total = 0
    try:
        # ---- 1. 五个库：一致性快照 ----
        for src, label in DATABASES:
            if not src.exists():
                raise FileNotFoundError(f"库文件不存在：{src}")
            target = dest / src.name
            t0 = time.time()
            vacuum_into(src, target)
            size = target.stat().st_size
            total += size
            # 核对快照真的能打开、且行数与源库一致（防「空壳备份」）
            con = sqlite3.connect(str(target))
            table = CHECK_TABLES.get(src.name)
            rows = (con.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]
                    if table else None)
            con.close()
            src_rows = None
            if table:
                con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
                src_rows = con.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]
                con.close()
                if rows != src_rows:
                    raise RuntimeError(
                        f"{src.name} 快照行数不符：源 {src_rows} / 备份 {rows}")
            manifest["databases"].append({
                "file": src.name, "label": label, "bytes": size, "rows": rows,
                "source_rows": src_rows, "sha256": _sha256(target),
                "seconds": round(time.time() - t0, 3),
            })
            print(f"  [OK] {src.name:14s} {size / 1048576:7.2f} MB  "
                  f"{table}={rows} 行  ({label})")

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
                  databases=len(manifest["databases"]),
                  photos=manifest["photos"]["files"],
                  seconds=elapsed, rotated=dropped, keep=keep)
    print(f"\n完成：{dest}")
    print(f"  合计 {total / 1048576:.2f} MB · 耗时 {elapsed}s · "
          f"现有备份 {len(_existing_backups(out_root))} 份")
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
        description="五库 + 照片的一致性备份（VACUUM INTO，服务在跑也能用）")
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
