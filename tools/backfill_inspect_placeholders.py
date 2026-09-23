"""补齐导入时被清掉的检测字段占位值（`/`）—— 2026-09-22。

为什么需要这个脚本
--------------------------------------------------------------------------
`import_kdocs_returns.py` 早期用 `clean()` 清洗检测字段，而 `clean()` 会把
`/`、`-`、`无` 这些占位符**清成空值**。退回侧那样没问题，但检测侧那 7 项是
「完结状况」的判定依据（`config.COMPLETION_FIELDS`，7 项全有值才判「已完结」），
清掉就等于把已经确认完的单子判成未完结：

    金山表标「完结」3007 条  →  程序只判出 166 条

导入工具已修（`clean_inspect_field`，`/` 保留；故障原因 `/`→NTF、
改善措施 `/`→暂无改善），**但已经导进去的数据补不回来** —— 原始 `/` 在库里
已经变成空值，只能回快照重取。所以有了这个脚本。

为什么不直接 `--replace` 重导
--------------------------------------------------------------------------
重导会清掉 `source='kdocs'` 的全部行再重来，**用户在程序里手工删掉的记录会复活**
（2026-09-22 真实发生过：469 条被手删的记录被重导带回来）。本脚本只
**UPDATE 已有行**、**只补空值**（`col IS NULL OR col=''`），
既不新增行、也不覆盖人工改过的值。

用法
--------------------------------------------------------------------------
    python tools/backfill_inspect_placeholders.py            # 干跑，只报告
    python tools/backfill_inspect_placeholders.py --apply    # 实际写入
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import COMPLETION_FIELDS                      # noqa: E402
from core.db import get_conn, tx                          # noqa: E402
from core.repo_inspect import compute_completion          # noqa: E402
from core import repo_inspect                             # noqa: E402

SNAPSHOT = ROOT / "data" / "_kdocs_returns_raw.json"

# 要补的列：检测判定字段里在金山侧出现过占位符的那几列。
# 顺序即报告顺序（先把最卡的 improvement/fault_cause 列出来）。
TARGET_FIELDS = ["fault_cause", "improvement", "test_result", "solution"]


def _load_import_tool():
    """把导入工具当模块加载 —— 复用它的列下标、清洗与归一函数。

    ⚠️ 一定要复用它，不能在本文件里重抄一份：口径必须**只有一个来源**，
    否则哪天导入规则变了、这个脚本还在按老规则算，就会把数据改坏。
    """
    spec = importlib.util.spec_from_file_location(
        "imp_kdocs", ROOT / "tools" / "import_kdocs_returns.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_detail_keys(mod, rows: list) -> tuple:
    """按导入时的真实顺序，复现每一行落库后的 `detail_key`。

    复现规则（与 `create_returns_batch` 逐条导入的行为一致）：
      · 归一后的售后单号为组；同一单号第 k 次出现 → `line_no = k`
      · 缺单号的行按快递单归入同组（导入时由 `resolve_order_no` 补登）
      · 全空行跳过（导入时不计入）

    返回 `(row → detail_key 的映射, 无法推导的行号列表)`。
    """
    mapping, skipped = {}, []
    seq = {}                                # order_no → 已分配的行号计数
    first_of = {}                           # return_no(大写) → 该组第一个有效单号
    for r in rows:
        cells = r.get("cells") or []
        if not any(str(c or "").strip() for c in cells):
            continue                        # 全空行：导入时跳过
        on = mod.clean(cells[mod.ORDER_NO_COL])
        rn = mod.clean(cells[mod.RETURN_NO_COL])
        key = rn.upper()
        if not on:
            on = first_of.get(key, "")      # 缺单号 → 归入同快递单已有的单
            if not on:
                skipped.append(r["row"])    # 该组一个号都没有 → 导入时会新建，无法推导
                continue
        else:
            first_of.setdefault(key, on)
        seq[on] = seq.get(on, 0) + 1
        mapping[r["row"]] = "%s-%03d" % (on, seq[on])
    return mapping, skipped


def main() -> int:
    apply = "--apply" in sys.argv
    if not SNAPSHOT.exists():
        print(f"✗ 找不到快照 {SNAPSHOT}")
        return 1

    mod = _load_import_tool()
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    rows = snap["rows"]
    print(f"快照：{len(rows)} 行 · {SNAPSHOT.name}")

    # ★ 必须先复现「归一并号」：导入时它对**内存里的 cells** 改写了售后单号
    #   （同快递单多行共用一个号）。少了这一步，下面推导出的键会全部是
    #   金山原号（一行一个），与库里的键完全对不上。
    rk = mod.rekey_by_return_no(rows)
    print(f"  归一并号复现：改写 {rk['rewritten']} 行 · "
          f"合并掉 {rk['dropped_nos']} 个单号 · 涉及 {rk['orders_merged']} 个快递单")

    bad_fields = [f for f in TARGET_FIELDS if f not in COMPLETION_FIELDS]
    if bad_fields:
        print(f"  ✗ 待补字段不在完结判定清单里：{bad_fields}（脚本前提已失真）")
        return 1

    # ---------- ① 复现 detail_key，并与库对账 ----------
    mapping, skipped = build_detail_keys(mod, rows)
    if skipped:
        print(f"  ⚠️ {len(skipped)} 行推导不出单号（该快递单整组都没号）：{skipped[:8]}")

    conn = get_conn()
    db_keys = {r["detail_key"] for r in
               conn.execute("SELECT detail_key FROM inspect_db.inspect_records").fetchall()}
    derived = set(mapping.values())
    missing = db_keys - derived                 # 库里有、快照推不出来 → 映射不可信
    extra = derived - db_keys                   # 快照推得出、库里没有 → 被删的行
    print(f"  库里 detail_key {len(db_keys)} 个 · 快照推导出 {len(derived)} 个")
    print(f"  推导不出的（库里有）: {len(missing)} 个 {sorted(missing)[:5]}")
    print(f"  库里没有的（快照有）: {len(extra)} 个 {sorted(extra)[:5]}")
    if missing:
        print("  ✗ 有库里存在却推导不出的键 —— 映射规则与导入时不一致，中止。")
        return 1
    print("  ✓ 映射可信（库里每个键都能在快照里定位到）")

    # ---------- ② 算出每行该有的目标值 ----------
    by_row = {r["row"]: r for r in rows}
    # 列下标：从导入工具的 INSPECT_MAP 反查，不重抄
    idx_of = {f: i for i, f in mod.INSPECT_MAP.items()}
    targets = {}                                # detail_key → {field: 目标值}
    for row_no, dk in mapping.items():
        cells = by_row[row_no]["cells"]
        vals = {}
        for f in TARGET_FIELDS:
            i = idx_of[f]
            v = mod.clean_inspect_field(f, cells[i]) if i < len(cells) else ""
            if v:
                vals[f] = mod.normalize_value(f, v)
        if vals:
            targets[dk] = vals

    # ---------- ③ 只补空（不新增行、不覆盖人工值）----------
    plan = []                                   # (dk, field, old, new)
    for dk, vals in targets.items():
        cur = conn.execute(
            "SELECT * FROM inspect_db.inspect_records WHERE detail_key = ?;",
            (dk,)).fetchone()
        if not cur:
            continue                            # 库里没有（被手删的）→ 不碰
        for f, v in vals.items():
            old = str(cur[f] or "").strip()
            if not old:                         # ★ 只补空：人工填过的一律不动
                plan.append((dk, f, old, v))

    print()
    print("── 待补写（只补空值）──")
    from collections import Counter
    cnt = Counter(f for _, f, _, _ in plan)
    for f in TARGET_FIELDS:
        if cnt.get(f):
            print(f"   {f:16s} {cnt[f]:5d} 格")
    print(f"   合计 {len(plan)} 格 · 涉及 {len({d for d, _, _, _ in plan})} 条明细")
    for dk, f, old, new in plan[:6]:
        print(f"     {dk} {f}: {old!r} → {new!r}")

    if not apply:
        print("\n（干跑，未写库。加 --apply 实际执行）")
        return 0

    # ---------- ④ 执行 ----------
    touched = sorted({dk for dk, _, _, _ in plan})
    with tx() as txc:
        for dk, f, _old, new in plan:
            txc.execute(
                "UPDATE inspect_db.inspect_records SET `%s` = ?, updated_at = NOW() "
                "WHERE detail_key = ?;" % f, (new, dk))
        # 重算完结状况：全量（判定只依赖那 7 项，改了值就要重判）
        recalc = 0
        for r in txc.execute(
                "SELECT * FROM inspect_db.inspect_records;").fetchall():
            comp = compute_completion(dict(r))
            if comp != (r["completion"] or ""):
                txc.execute(
                    "UPDATE inspect_db.inspect_records SET completion = ? "
                    "WHERE detail_key = ?;", (comp, r["detail_key"]))
                recalc += 1
        for dk in touched:
            txc.execute(
                "INSERT INTO inspect_db.op_log "
                "(action, detail_key, payload, operator, created_at) "
                "VALUES ('inspect', ?, ?, ?, NOW());",
                (dk, json.dumps({"source": "backfill_inspect_placeholders"},
                                ensure_ascii=False), "占位符补值"))
    print(f"\n✓ 已补写 {len(plan)} 格 / {len(touched)} 条明细 · "
          f"完结状况重判改变了 {recalc} 条")

    repo_inspect.refresh_dict_options()
    n_done = conn.execute("SELECT COUNT(*) c FROM inspect_db.inspect_records "
                          "WHERE completion = '已完结';").fetchone()["c"]
    n_all = conn.execute("SELECT COUNT(*) c FROM inspect_db.inspect_records").fetchone()["c"]
    print(f"  现在：已完结 {n_done} 条 / 检测行 {n_all} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
