"""清掉「照片证据」字段里遗留的历史文本（金山表格内嵌图片公式）。

**背景**
金山「照片证据」列存的是内嵌图片公式 `=DISPIMG("ID_…")`，图片本体不在
导出文件里 —— 这类值在新系统里解析不出任何图片，显示出来只是干扰。
照片改为在检测登记页上传后，这些公式就属于无用残留，一次性清理。

**安全性**
- 只清「历史文本」，**不动真实上传的照片**：两者并存时保留 photos，
  仅去掉 legacy 部分
- 原始 ID 仍保留在 `data/_kdocs_returns_raw.json`（导入源），需要时可回溯
- 走 `repo.update_return()` 写入 —— 与界面保存同一条路径，字段归属、
  操作日志、完结状况重算都由它统一处理

**用法**
    python tools/strip_photo_legacy.py --dry-run
    python tools/strip_photo_legacy.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db import close_conn, get_conn                # noqa: E402
from core import photos as photos_mod                   # noqa: E402
from core import repository as repo                     # noqa: E402


def main() -> int:
    dry = "--dry-run" in sys.argv
    conn = get_conn()

    rows = conn.execute(
        "SELECT detail_key, photo_evidence FROM inspect_db.inspect_records "
        "WHERE TRIM(COALESCE(photo_evidence,'')) <> '' ORDER BY detail_key;"
    ).fetchall()

    print("=" * 70)
    print("  清理「照片证据」里的历史文本")
    print("=" * 70)
    print(f"  字段非空的记录：{len(rows)} 条")

    targets = []
    for r in rows:
        names, legacy = photos_mod.parse_value(r["photo_evidence"])
        if legacy:
            targets.append((r["detail_key"], names, legacy))

    print(f"  含历史文本的  ：{len(targets)} 条")
    print(f"  纯照片无文本的：{len(rows) - len(targets)} 条")
    if not targets:
        print("\n  无需改动。")
        close_conn()
        return 0

    print("\n  明细键             照片  历史文本")
    for key, names, legacy in targets:
        print(f"  {key:<16} {len(names):>3} 张  {legacy[:52]}"
              f"{'…' if len(legacy) > 52 else ''}")

    if dry:
        print("\n  [dry-run] 未写入任何数据")
        close_conn()
        return 0

    operator = "工具:清理照片历史文本"
    changed = 0
    for key, names, legacy in targets:
        new_value = photos_mod.dump_value(names)          # legacy 不再回写
        try:
            if repo.update_return(key, {"photo_evidence": new_value}, operator):
                changed += 1
        except Exception as exc:                          # noqa: BLE001
            print(f"  [失败] {key}：{exc}")

    left = conn.execute(
        "SELECT COUNT(*) c FROM inspect_db.inspect_records "
        "WHERE TRIM(COALESCE(photo_evidence,'')) <> '' AND photo_evidence LIKE '%DISPIMG%';"
    ).fetchone()["c"]
    close_conn()

    print(f"\n  已更新 {changed}/{len(targets)} 条")
    print(f"  仍含 DISPIMG 的记录：{left} 条")
    print(f"  耗时 {time.strftime('%H:%M:%S')}")
    return 0 if changed == len(targets) and left == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
