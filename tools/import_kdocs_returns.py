"""从金山文档「返件登记」导入历史记录

数据来源
--------
`data/_kdocs_returns_raw.json` —— 由 MCP 读取金山表格选区后保存的规范化数据
（结构：{"header": [...], "rows": [{"row": n, "cells": [...]}]}）。

字段映射（金山 26 列 → 三库）
----------------------------
⚠️ **金山那边删过一列「快递费用」**，所以第 10 列往后的下标相对早期版本**左移了 1**
（ERP处理 11 / 信息来源 12 / 检测时间 13 … 备注 25）。本文件的映射表按**当前表头**写死；
若金山再加/删列，必须回来核对 —— 错位不会报错，只会把值写进相邻字段（极难发现）。
| 归属 | 映射 |
|---|---|
| 退回登记库 | 售后单号 · 风机厂家 · 项目风场 · 产品编号 · 产品型号 · 产品类别<br>生产年份 · 退回数量 · 退回时间 · 退回单号 · 信息来源 · 反馈现象 · 备注 |
| 检测登记库 | 检测时间 · 完结状况 · 检测结果 · 故障原因 · 改善措施 · 处理方案<br>问题分类 · 责任归属 · 照片证据 · 报告编号 · ERP处理 |
| 自动推导 | 快递公司（按退回单号前缀识别）· 明细唯一键 · 行号 |
| 跳过 | 快递费用 · 是否待返回 · 回复进度（系统已移除这三个字段） |

导入后统一标记 `source='kdocs'`（数据来源留在金山，本地这份是副本）。
⚠️ 早期版本还写过 `sync_state='synced'` / `synced_at` —— **这两列 2026-09-21 已随
金山同步层一起删掉**，再写就是 Unknown column。

用法
----
    python tools/import_kdocs_returns.py                     # 导入全部（追加）
    python tools/import_kdocs_returns.py --replace           # 先清空上一批金山导入，再整表重导
    python tools/import_kdocs_returns.py --limit 20          # 只导前 20 条
    python tools/import_kdocs_returns.py --dry-run           # 只看映射结果不写库
    python tools/import_kdocs_returns.py --no-rekey          # 保留金山原样单号（会违反铁律）
"""
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.db import close_conn, db_status, get_conn, init_db, tx  # noqa: E402
from core.repository import (create_returns_batch,      # noqa: E402
                             refresh_dict_options)
from core import repo_handle                             # noqa: E402

DEFAULT_SRC = pathlib.Path(__file__).resolve().parent.parent / "data" / "_kdocs_returns_raw.json"

# 金山列索引 → 退回登记库字段
RETURNS_MAP = {
    0: "order_no",          # 售后单号
    1: "turbine_vendor",    # 风机厂家
    2: "project_site",      # 项目风场
    3: "product_code",      # 产品编号
    4: "product_model",     # 产品型号
    5: "product_category",  # 产品类别（公式列）
    6: "production_year",   # 生产年份（公式列）
    7: "return_qty",        # 退回数量
    8: "return_date",       # 退回时间（日期）
    9: "return_no",         # 退回单号
    12: "info_source",      # 信息来源
    15: "feedback_issue",   # 反馈现象
    25: "remark",           # 备注
}

# 金山列索引 → 检测登记库字段
# 注意：`erp_handled` 落在**处理库**（handle_records），其余落在检测库 ——
# 这里只负责把值取出来，按库分流由 create_returns_batch 做。
INSPECT_MAP = {
    11: "erp_handled",      # ERP处理 → handle_records
    13: "test_date",        # 检测时间（日期）
    14: "completion",       # 完结状况（公式列）
    16: "test_result",      # 检测结果
    17: "fault_cause",      # 故障原因
    18: "improvement",      # 改善措施
    19: "solution",         # 处理方案
    20: "issue_category",   # 问题分类
    21: "responsibility",   # 责任归属
    22: "photo_evidence",   # 照片证据
    23: "report_no",        # 报告编号
}

# 系统已移除 / 不需要的列
SKIPPED = {10: "是否待返回", 24: "回复进度"}

# 表示「无内容」的占位值
BLANKS = {"/", "-", "—", "无", "N/A", "n/a"}


def clean(v) -> str:
    s = str(v or "").strip()
    return "" if s in BLANKS else s


# ⚠️ 检测字段**不能**用 clean()：那边的 `/` 必须保留。
#
# 金山的 `/` 是人工明确填的「本栏不适用、已确认过」——是**已填写**，不是没填。
# 而检测侧那 7 项是「完结状况」的判定依据（`config.COMPLETION_FIELDS`）：
# 清掉 `/` 就等于把已确认的单子判成未完结。2026-09-22 实测 —— 清掉 `/` 后
# 程序只判得出 166 条完结，而金山表里标着 3007 条（差 18 倍，看板严重失真）。
def clean_inspect(v) -> str:
    """检测字段清洗：只去空白，**保留 `/` 等占位符**（与退回侧不同）。"""
    return str(v or "").strip()


# 三列的 `/` 有更准确的写法（2026-09-22 用户拍板）：
#   故障原因「/」→「NTF」（本就不适用 = 非产品故障，与问题分类同词）
#   改善措施「/」→「暂无改善」（明确"没有改善措施"）
#   处理方案「/」→「拆解报废」（`solution` 是**固定选项**字段，值必须在
#     `config.FIXED_OPTIONS` 清单内才能跨记录统计；`/` 不在清单里）
# 其余栏位（检测结果 …）的「/」原样保留，判定时算作已填写。
PLACEHOLDER_TEXT = {
    "fault_cause": "NTF",
    "improvement": "暂无改善",
    "solution": "拆解报废",
}


def clean_inspect_field(field: str, v) -> str:
    """检测字段取值：占位符按 `PLACEHOLDER_TEXT` 换词，没配映射的原样保留。"""
    s = clean_inspect(v)
    if s in BLANKS:
        return PLACEHOLDER_TEXT.get(field, s)
    return s


# 金山「照片证据」列存的是**表格内嵌图片公式**（`=DISPIMG("ID_…")`），
# 图片本体不在导出文件里。这种值导入后解析不出任何图片，落库只是干扰，
# 因此统一视为「无内容」。原始 ID 仍保留在 data/_kdocs_returns_raw.json 可回溯。
IMG_FORMULA = re.compile(r"^\s*=?\s*DISPIMG\s*\(", re.IGNORECASE)


def clean_photo(v) -> str:
    """照片列清洗：内嵌图片公式不算内容（真照片由登记页上传）。"""
    s = clean(v)
    return "" if IMG_FORMULA.match(s) else s


DATE_RE = re.compile(r"^(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D*$")


def clean_date(v) -> str:
    """日期列归一成 `YYYY-MM-DD`。

    库里既有的那批就是 `2026-01-05`；金山导出可能是「2026年1月5日」或 Excel
    序列号。**不归一就会在同一个字段里出现两种格式**，而排序/筛选全按字符串比。
    认不出来就原样返回（宁可保留怪值，也不要猜错成另一个日期）。
    """
    s = clean(v)
    if not s:
        return ""
    m = DATE_RE.match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return "%04d-%02d-%02d" % (y, mo, d)
    if s.isdigit() and len(s) == 5:          # Excel 序列号（1900 起）
        import datetime as _dt
        base = _dt.date(1899, 12, 30)
        return (base + _dt.timedelta(days=int(s))).isoformat()
    return s


# 金山各关键列的下标（按当前表头；改表要回来核对）
ORDER_NO_COL = 0        # 售后单号
RETURN_NO_COL = 9       # 退回单号（快递单号）
ERP_HANDLED_COL = 11    # ERP处理（自由文本，见下方 map_erp_handled）
# 程序里 `erp_handled` 是**两值锁定**（config.FIXED_OPTIONS）。
ERP_DONE = "已处理"
ERP_NONE = {"", "/", "-", "—", "无", "N/A", "n/a"}


def map_erp_handled(v) -> str:
    """把金山的自由文本降成程序要的两值。

    金山那一列写什么的都有：「已入返修库」595 行、「已入不良品库」104 行、
    「已入成品库」86 行、「退回供应商」「改制」「已做单」……而程序里
    `FIXED_OPTIONS['erp_handled']` 只有 `已处理 / 待处理`（要跨记录统计、
    口径必须唯一）。**只能降维**：凡是「ERP 侧已经动过」的写法一律归「已处理」，
    空值与占位符归空（读取时归一为「待处理」）。
    原始文本没有丢 —— 完整快照留在 `data/_kdocs_returns_raw.json` 里可回溯。
    """
    s = clean(v)
    if not s or s in ERP_NONE:
        return ""
    if s == "待处理":
        return "待处理"
    return ERP_DONE


# 金山表里的两处写法与程序口径不一致，导入时统一（否则每次重导都会把旧写法带回来）：
#   ① 问题分类「无法判定」→「无法判断」（程序锁定清单里只有后者，自检也断言必须统一）
#   ② 责任归属写成「NTF」的（这是问题分类用的词）→「其他端」（2026-09-22 用户拍板）
VALUE_FIXES = {
    "issue_category": {"无法判定": "无法判断"},
    "responsibility": {"NTF": "其他端"},
}


def normalize_value(field: str, value: str) -> str:
    """把金山里的个别写法归一到程序口径（表见 VALUE_FIXES）。"""
    return (VALUE_FIXES.get(field) or {}).get(value, value)


def clean_qty(v):
    s = clean(v)
    if not s:
        return 1
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return 1


def build_payload(cells: list) -> tuple:
    """把一行金山数据拆成 (header, item)。"""
    header, item, inspect = {}, {}, {}

    for idx, field in RETURNS_MAP.items():
        val = clean(cells[idx]) if idx < len(cells) else ""
        if not val:
            continue
        if field == "return_qty":
            item[field] = clean_qty(val)
        elif field == "order_no":
            header[field] = val          # 保留原售后单号，不重新生成
        elif field in ("return_date",):
            item[field] = clean_date(val)
        else:
            item[field] = val

    for idx, field in INSPECT_MAP.items():
        if field == "photo_evidence":
            val = clean_photo(cells[idx]) if idx < len(cells) else ""
        elif field == "test_date":
            val = clean_date(cells[idx]) if idx < len(cells) else ""
        else:
            # ⚠️ 走 clean_inspect_field（保留 `/`），不是 clean() —— 见上面的说明。
            val = clean_inspect_field(field, cells[idx]) if idx < len(cells) else ""
        if val:
            inspect[field] = normalize_value(field, val)

    # 检测字段与本行产品字段合并后一起提交，create_returns_batch 会按库分流
    item.update(inspect)
    return header, item


def rekey_by_return_no(rows: list) -> dict:
    """按**本库的口径**归一售后单号：同一快递单的多行共用一个售后单号。

    为什么需要它（2026-09-22 用户拍板）
    ----------------------------------
    金山表现在的编号口径是「**一行一个售后单号**」——同一个快递单寄回 64 只
    产品，表里就有 64 个号（20260004 / 20260005 / …）。而本库的铁律是
    「**一个快递单号 ↔ 一个售后单号**」：同快递单的多只产品共用一个售后单号、
    各占一行、行号自 1 连续（`smoke_test [21]` 有两条断言守着，违反会判红）。

    做法：按快递单号分组，把该组**第一行**的单号回写给组内其余行。
    导入时 `create_returns_batch(..., allow_duplicate=True)` 会给同一个单号
    续行号（1..N），于是又回到「一个快递单一个售后单、多产品各占一行」。

    代价（已知并接受）：组内后续行在本地的单号与金山表上那一个不再一致
    （表里 20260005 → 本地 20260004-002）。要保留金山原号就用 `--no-rekey`。

    返回改写统计，供导入报告打印。
    """
    # ⚠️ **分组键必须大小写不敏感**：金山表里同一个快递单出现过
    # `JDVA46911185924` 与 `jdva46911185924` 两种写法（实测 24 行是小写），
    # 而 MySQL 的排序规则默认不区分大小写 —— 于是「库里算是同一个快递单、
    # 归一时却分成两组」，结果库里真有 1 个快递单挂两个售后单号、
    # 自检 [21] 判红。这里统一按大写分组，与库的口径对齐。
    first, merged, rewritten = {}, set(), 0
    for r in rows:
        cells = r.get("cells") or []
        rn = clean(cells[RETURN_NO_COL]) if len(cells) > RETURN_NO_COL else ""
        on = clean(cells[ORDER_NO_COL]) if len(cells) > ORDER_NO_COL else ""
        key = rn.upper()
        if key and on and key not in first:
            first[key] = on
    for r in rows:
        cells = r.get("cells") or []
        rn = clean(cells[RETURN_NO_COL]) if len(cells) > RETURN_NO_COL else ""
        on = clean(cells[ORDER_NO_COL]) if len(cells) > ORDER_NO_COL else ""
        key = rn.upper()
        if key and on and key in first and first[key] != on:
            merged.add(on)
            cells[ORDER_NO_COL] = first[key]
            rewritten += 1
    return {"rewritten": rewritten, "dropped_nos": len(merged),
            "orders_merged": len(first)}


def purge_kdocs_rows(conn) -> dict:
    """清掉此前从金山导入的行（退回 / 检测 / 处理三库一起）。

    **只动 `source='kdocs'` 的行** —— 本系统自己登记、或后来被人改过的记录
    （`source='local'`）一律不碰。为什么需要它：本工具的
    `create_returns_batch(..., allow_duplicate=True)` 会**给同一个售后单号追加新行号**，
    所以「重复导同一份快照」不会覆盖，而是把每一条都变成两行（实测口径：
    100 行 → 200 行）。要重导就得先把上一批清掉，这份数据在金山那边是原件，
    清掉完全可复现。
    """
    n_ret = conn.execute(
        "SELECT COUNT(*) c FROM returns_db.returns WHERE source='kdocs';"
    ).fetchone()["c"]
    n_ins = conn.execute(
        "SELECT COUNT(*) c FROM inspect_db.inspect_records i "
        "JOIN returns_db.returns r ON r.detail_key = i.detail_key "
        "WHERE r.source='kdocs';").fetchone()["c"]
    n_hnd = conn.execute(
        "SELECT COUNT(*) c FROM handle_db.handle_records h "
        "JOIN returns_db.returns r ON r.detail_key = h.detail_key "
        "WHERE r.source='kdocs';").fetchone()["c"]
    conn.execute(
        "DELETE i FROM inspect_db.inspect_records i "
        "JOIN returns_db.returns r ON r.detail_key = i.detail_key "
        "WHERE r.source='kdocs';")
    conn.execute(
        "DELETE h FROM handle_db.handle_records h "
        "JOIN returns_db.returns r ON r.detail_key = h.detail_key "
        "WHERE r.source='kdocs';")
    conn.execute("DELETE FROM returns_db.returns WHERE source='kdocs';")
    # ⚠️ 有 DML 就无条件提交：本项目踩过「DELETE 影响 0 行也开事务、不提交
    # 就握着写锁，别人登录直接 500」的坑（见 core/auth.py 的 set_setting 注释）。
    conn.commit()
    return {"returns": n_ret, "inspect": n_ins, "handle": n_hnd}


def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    replace = "--replace" in args
    rekey = "--no-rekey" not in args          # 默认按本库口径归一
    limit = 0
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
    src = pathlib.Path(args[0]) if args and not args[0].startswith("-") else DEFAULT_SRC

    print("=" * 70)
    print("  金山「返件登记」导入")
    print("=" * 70)
    print(f"  数据源：{src}")

    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data["rows"]
    if limit:
        rows = rows[:limit]
    print(f"  待导入：{len(rows)} 条")
    if rekey:
        rk = rekey_by_return_no(rows)
        print(f"  售后单号归一：改写 {rk['rewritten']} 行 · "
              f"合并掉 {rk['dropped_nos']} 个单号 · 涉及 {rk['orders_merged']} 个快递单"
              f"（本库口径：一个快递单 = 一个售后单，多产品各占一行）")
    else:
        print("  ⚠️ --no-rekey：保留金山原样的一行一个单号"
              "（会违反「一个快递单 ↔ 一个售后单」的铁律，自检 [21] 会判红）")

    print("\n--- 字段映射 ---")
    print("  退回登记库：", " · ".join(RETURNS_MAP[i] for i in sorted(RETURNS_MAP)))
    print("  检测登记库：", " · ".join(INSPECT_MAP[i] for i in sorted(INSPECT_MAP)))
    print("  跳过列    ：", " · ".join(f"{n}(第{k}列)" for k, n in SKIPPED.items()))

    payloads = [build_payload(r["cells"]) for r in rows]
    if dry_run:
        print("\n--- 映射预览（前 3 条）---")
        for h, it in payloads[:3]:
            print(f"\n  header: {h}")
            for k, v in it.items():
                print(f"    {k:<18} {v}")
        print("\n[dry-run] 未写入数据库")
        return 0

    init_db()
    if replace:
        purged = purge_kdocs_rows(get_conn())
        print(f"  清空上一批金山导入：退回 {purged['returns']} · "
              f"检测 {purged['inspect']} · 处理 {purged['handle']} 条")
        if purged["returns"] == 0:
            print("  ⚠️ 没有 source='kdocs' 的行可清（可能已经清过了）")
    started = time.time()
    ok = failed = handled = 0
    order_nos = []
    failures = []

    skipped = []
    auto_no = 0
    for r, (header, item) in zip(rows, payloads):
        if not header.get("order_no"):
            # 两种要分开看，别一律当失败：
            #   ① 整行全空 → 金山里就是个空行（表格末尾或分隔行），跳过就好；
            #   ② 有数据却没有售后单号 → **这张单在金山里缺了单号**，
            #      不能猜一个（猜出来的单号在金山里不存在，以后对不上账），
            #      也不能当失败（会让整批导入的退出码变红）。单独列出来给人工补。
            if not any(str(c or "").strip() for c in (r.get("cells") or [])):
                skipped.append((r["row"], "空行"))
                continue
            # 有数据却没单号：**不跳过**（2026-09-22 用户拍板）。
            # 把快递单号交给程序去归单 —— `resolve_order_no` 的补登规则是
            # 「快递单号已在库则归入该单，否则按 YYMMDD+序号新建」，正是 App 里
            # 手工登记的行为。实测这两行的快递单（SF0216068102431）**同单还有
            # 另外两行**（3320/3321），所以它们会挂到那个已存在的售后单下续行号 ——
            # 而不是凭空多出一个号（那会再次破坏「一个快递单 ↔ 一个售后单」）。
            _rn = (clean(r["cells"][RETURN_NO_COL])
                   if len(r["cells"]) > RETURN_NO_COL else "")
            header = {"return_no": _rn} if _rn else {}
            auto_no += 1
        try:
            res = create_returns_batch(header, [item], operator="金山导入",
                                       allow_duplicate=True,
                                       refresh_dict=False)
            order_nos.append(res["order_no"])
            # ⚠️ `create_returns_batch` **不写处理库**（`erp_handled` 既不在检测
            #    字段集、也不在退回字段集，会被静默丢掉）—— 必须在这里单独写一次，
            #    否则「ERP 处理到哪一步」这一列整批丢失。
            dk = (res.get("detail_keys") or [None])[0]
            _erp = map_erp_handled(r["cells"][ERP_HANDLED_COL]
                                   if len(r["cells"]) > ERP_HANDLED_COL else "")
            if dk and _erp:
                repo_handle.upsert(dk, {"erp_handled": _erp},
                                   operator="金山导入", refresh_dict=False)
                handled += 1
            ok += 1
        except Exception as exc:                     # noqa: BLE001
            failures.append((r["row"], f"{type(exc).__name__}: {exc}"))
            failed += 1

    # 标记来源为 kdocs（`sync_state` / `synced_at` 两列随金山同步层一起删掉了，
    # 见 core/db.py 的 `_drop_legacy_sync` —— 这里原来还在写它们，会直接报
    # Unknown column 而把整批导入判成失败）。
    stamped = 0
    if order_nos:
        with tx() as conn:
            marks = ", ".join("?" for _ in order_nos)
            cur = conn.execute(
                f"UPDATE returns SET source='kdocs' WHERE order_no IN ({marks});",
                order_nos,
            )
            stamped = cur.rowcount

    conn = get_conn()
    inspect_n = conn.execute(
        "SELECT COUNT(*) c FROM inspect_db.inspect_records;").fetchone()["c"]

    print(f"\n--- 导入结果 ---")
    print(f"  成功 {ok} 条 · 跳过 {len(skipped)} 条 · 失败 {failed} 条 · "
          f"耗时 {time.time()-started:.1f}s")
    print(f"  标记来源 source=kdocs：{stamped} 条")
    print(f"  检测登记库现有记录：{inspect_n} 条")
    if auto_no:
        print(f"  缺售后单号的行：{auto_no} 条（已按快递单归单 —— "
              f"归入同快递单已有的售后单，或在库里没有该快递单时新建单号）")
    print(f"  处理登记库写入 erp_handled：{handled} 条"
          f"（金山那列是自由文本，已按「动过 = 已处理」降维）")
    if skipped:
        print(f"  跳过 {len(skipped)} 行（不计入失败）：")
        for line, msg in skipped[:12]:
            print(f"    第 {line} 行：{msg}")
    if failures:
        print("  失败明细：")
        for line, msg in failures[:10]:
            print(f"    第 {line} 行：{msg}")

    # 批量期间没有逐行重建字典（O(n²)），这里补一次 —— 它会把退回 / 检测 /
    # 处理三库的候选一起重建（见 repository.refresh_dict_options 的转发）。
    _rf = refresh_dict_options()
    print(f"  字典候选已重建（清理墓碑 {_rf.get('pruned', 0)} 项）")

    st = db_status()
    print(f"\n--- 三库概览 ---")
    print(f"  退回 {st['total']} 条 · 已检测 {st['inspected']} 条 · "
          f"物料 {st['item_master']} 条 · 型号字典 {st['model_dict']} 条")
    close_conn()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
