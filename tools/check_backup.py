"""备份真实性守卫：把「备份能跑」升级为「备份能用」。

单独跑：python tools/check_backup.py
也被 tools/check_deploy.py 调用（作为部署自检的一环）。

守四件事：
  1. 最新备份有 DONE 标记（残缺备份不冒充可用）；
  2. 每个库的**行数与现库一致**（防「空壳备份」——目录在、数据没进去）；
  3. 快照能**独立打开**且 `PRAGMA integrity_check` 通过（不依赖原库）；
  4. MANIFEST 里的 sha256 与实际文件一致（防「清单写了、文件坏了」）。

**不写死任何具体数字**（如「100 条」）—— 数据会变，写死就会变成假失败。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from config import DATA_DIR                                        # noqa: E402

BACKUP_ROOT = DATA_DIR / "backups"
# 库文件 → 用来核对行数的表
TABLES = {
    "returns.db": "returns",
    "inspect.db": "inspect_records",
    "handle.db": "handle_records",
    "items.db": "item_master",
    "auth.db": "users",
}
FAIL = []


def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def count(db: Path, table: str) -> int:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]
    finally:
        con.close()


def main() -> int:
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
    check("有 MANIFEST.json", (newest / "MANIFEST.json").is_file())
    if not (newest / "MANIFEST.json").is_file():
        return 1

    man = json.loads((newest / "MANIFEST.json").read_text(encoding="utf-8"))

    # 5 个库齐全
    files = {d["file"] for d in man["databases"]}
    check("清单覆盖五个库", files == set(TABLES), ", ".join(sorted(files)))

    for name, table in TABLES.items():
        src, snap = DATA_DIR / name, newest / name
        if not snap.exists():
            check(f"{name} 快照存在", False)
            continue
        # 行数一致
        try:
            a, b = count(src, table), count(snap, table)
            check(f"{name} 行数与现库一致", a == b, f"现库 {a} / 备份 {b}")
        except sqlite3.Error as exc:
            check(f"{name} 行数可读", False, str(exc))
        # 独立可打开 + 完整性
        try:
            con = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
            ok = con.execute("PRAGMA integrity_check;").fetchone()[0]
            con.close()
            check(f"{name} integrity_check 通过", ok == "ok", str(ok))
        except sqlite3.Error as exc:
            check(f"{name} 可独立打开", False, str(exc))
        # sha256 一致
        rec = next((d for d in man["databases"] if d["file"] == name), None)
        if rec and rec.get("sha256"):
            h = hashlib.sha256(snap.read_bytes()).hexdigest()
            check(f"{name} sha256 与清单一致", h == rec["sha256"])

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
