"""按规则重算「完结状况」——一次性修正历史数据。

规则见 `config.COMPLETION_FIELDS`：
其中列出的检测字段**全部有值**才算已完结，否则存空值（= 未完结）。
报告编号、照片证据、ERP处理不参与判定（见 `config.COMPLETION_EXCLUDED`）。

日常写入时由 `repo_inspect.upsert()` 自动重算，本脚本只用于把
规则变更前留下的旧值一次性对齐。

用法：
    python tools/recalc_completion.py --dry-run
    python tools/recalc_completion.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (COMPLETION_DONE, COMPLETION_EXCLUDED,   # noqa: E402
                    COMPLETION_FIELDS)
from core.db import close_conn, get_conn, tx            # noqa: E402
from core import repo_inspect                           # noqa: E402


def main() -> int:
    dry = "--dry-run" in sys.argv
    conn = get_conn()

    rows = conn.execute(
        "SELECT * FROM inspect_db.inspect_records ORDER BY detail_key;").fetchall()
    rows = [dict(r) for r in rows]

    changed, to_todo, to_done = [], [], []
    missing_stat = {f: 0 for f in COMPLETION_FIELDS}
    for r in rows:
        new = repo_inspect.compute_completion(r)
        old = (r.get("completion") or "").strip()
        if new == old:
            continue
        item = (r["detail_key"], old, new)
        changed.append(item)
        if old and not new:
            to_todo.append(item)
        elif new and not old:
            to_done.append(item)
        if not new:
            for f in COMPLETION_FIELDS:
                if not str(r.get(f) or "").strip():
                    missing_stat[f] += 1

    done_after = sum(1 for r in rows if repo_inspect.compute_completion(r) == COMPLETION_DONE)
    todo_after = len(rows) - done_after

    print("=" * 70)
    print("  重算完结状况")
    print("=" * 70)
    print(f"  判定规则    : {' · '.join(COMPLETION_FIELDS)}")
    print(f"  不参与判定  : {' · '.join(COMPLETION_EXCLUDED)}")
    print(f"  检测记录数  : {len(rows)}")
    print(f"  需要改动    : {len(changed)} 条")
    print(f"    已完结 → 未完结 : {len(to_todo)} 条")
    print(f"    未完结 → 已完结 : {len(to_done)} 条")
    print(f"\n  重算后：已完结 {done_after} · 未完结 {todo_after}")

    if changed:
        print(f"\n  变更样例（最多 10 条）：")
        for key, old, new in changed[:10]:
            print(f"    {key:<14} {old or '(空)':<10} → {new or '(空)'}")

    if todo_after:
        print(f"\n  未完结记录的缺口分布（补这些字段最有效）：")
        for f, n in sorted(missing_stat.items(), key=lambda x: -x[1]):
            if n:
                print(f"    {f:<18} 缺 {n:>3} 条")

    if dry:
        print("\n  [dry-run] 未写入任何数据")
        close_conn()
        return 0

    if not changed:
        print("\n  所有记录已符合规则，无需改动")
        close_conn()
        return 0

    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with tx() as c:
        for key, _old, new in changed:
            c.execute(
                "UPDATE inspect_db.inspect_records SET completion = ?, updated_at = ? "
                "WHERE detail_key = ?;", (new, now, key))

    # completion 是字典字段，重建一次（批量操作只在最后做）
    repo_inspect.refresh_dict_options()
    print(f"\n  已更新 {len(changed)} 条，字典候选已重建")

    left = conn.execute(
        "SELECT COALESCE(NULLIF(TRIM(completion),''),'(空)') v, COUNT(*) n "
        "FROM inspect_db.inspect_records GROUP BY 1 ORDER BY n DESC;").fetchall()
    print("\n  完结状况现状：")
    for r in left:
        print(f"    {r['v']:<10} {r['n']} 条")

    close_conn()
    return 0


if __name__ == "__main__":
    sys.exit(main())
