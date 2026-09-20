"""从金山文档「返件登记」导入历史记录

数据来源
--------
`data/_kdocs_returns_raw.json` —— 由 MCP 读取金山表格选区后保存的规范化数据
（结构：{"header": [...], "rows": [{"row": n, "cells": [...]}]}）。

字段映射（金山 27 列 → 三库）
----------------------------
| 归属 | 映射 |
|---|---|
| 退回登记库 | 售后单号 · 风机厂家 · 项目风场 · 产品编号 · 产品型号 · 产品类别<br>生产年份 · 退回数量 · 退回时间 · 退回单号 · 信息来源 · 反馈现象 · 备注 |
| 检测登记库 | 检测时间 · 完结状况 · 检测结果 · 故障原因 · 改善措施 · 处理方案<br>问题分类 · 责任归属 · 照片证据 · 报告编号 · ERP处理 |
| 自动推导 | 快递公司（按退回单号前缀识别）· 明细唯一键 · 行号 |
| 跳过 | 快递费用 · 是否待返回 · 回复进度（系统已移除这三个字段） |

导入后统一标记 `source='kdocs'`、`sync_state='synced'` —— 数据本就在云端，
不需要再推回去。

用法
----
    python tools/import_kdocs_returns.py                     # 导入全部
    python tools/import_kdocs_returns.py --limit 20          # 只导前 20 条
    python tools/import_kdocs_returns.py --dry-run           # 只看映射结果不写库
"""
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.db import close_conn, db_status, get_conn, init_db, tx  # noqa: E402
from core.repository import create_returns_batch  # noqa: E402

DEFAULT_SRC = pathlib.Path(__file__).resolve().parent.parent / "data" / "_kdocs_returns_raw.json"

# 金山列索引 → 退回登记库字段
RETURNS_MAP = {
    0: "order_no",          # 售后单号
    1: "turbine_vendor",    # 风机厂家
    2: "project_site",      # 项目风场
    3: "product_code",      # 产品编号
    4: "product_model",     # 产品型号
    5: "product_category",  # 产品类别
    6: "production_year",   # 生产年份
    7: "return_qty",        # 退回数量
    8: "return_date",       # 退回时间
    9: "return_no",         # 退回单号
    13: "info_source",      # 信息来源 → 快递归属
    16: "feedback_issue",   # 反馈现象
    26: "remark",           # 备注
}

# 金山列索引 → 检测登记库字段
INSPECT_MAP = {
    12: "erp_handled",      # ERP处理
    14: "test_date",        # 检测时间
    15: "completion",       # 完结状况
    17: "test_result",      # 检测结果
    18: "fault_cause",      # 故障原因
    19: "improvement",      # 改善措施
    20: "solution",         # 处理方案
    21: "issue_category",   # 问题分类
    22: "responsibility",   # 责任归属
    23: "photo_evidence",   # 照片证据
    24: "report_no",        # 报告编号
}

# 系统已移除、直接跳过的列
SKIPPED = {10: "快递费用", 11: "是否待返回", 25: "回复进度"}

# 表示「无内容」的占位值
BLANKS = {"/", "-", "—", "无", "N/A", "n/a"}


def clean(v) -> str:
    s = str(v or "").strip()
    return "" if s in BLANKS else s


# 金山「照片证据」列存的是**表格内嵌图片公式**（`=DISPIMG("ID_…")`），
# 图片本体不在导出文件里。这种值导入后解析不出任何图片，落库只是干扰，
# 因此统一视为「无内容」。原始 ID 仍保留在 data/_kdocs_returns_raw.json 可回溯。
IMG_FORMULA = re.compile(r"^\s*=?\s*DISPIMG\s*\(", re.IGNORECASE)


def clean_photo(v) -> str:
    """照片列清洗：内嵌图片公式不算内容（真照片由登记页上传）。"""
    s = clean(v)
    return "" if IMG_FORMULA.match(s) else s


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
        else:
            item[field] = val

    for idx, field in INSPECT_MAP.items():
        if field == "photo_evidence":
            val = clean_photo(cells[idx]) if idx < len(cells) else ""
        else:
            val = clean(cells[idx]) if idx < len(cells) else ""
        if val:
            inspect[field] = val

    # 检测字段与本行产品字段合并后一起提交，create_returns_batch 会按库分流
    item.update(inspect)
    return header, item


def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
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
    started = time.time()
    ok = failed = 0
    order_nos = []
    failures = []

    for r, (header, item) in zip(rows, payloads):
        if not header.get("order_no"):
            failures.append((r["row"], "缺少售后单号"))
            failed += 1
            continue
        try:
            res = create_returns_batch(header, [item], operator="金山导入",
                                       allow_duplicate=True)
            order_nos.append(res["order_no"])
            ok += 1
        except Exception as exc:                     # noqa: BLE001
            failures.append((r["row"], f"{type(exc).__name__}: {exc}"))
            failed += 1

    # 导入的数据已在云端，标记为已同步，不参与后续推送
    stamped = 0
    if order_nos:
        with tx() as conn:
            marks = ", ".join("?" for _ in order_nos)
            cur = conn.execute(
                f"UPDATE returns SET source='kdocs', sync_state='synced', synced_at=? "
                f"WHERE order_no IN ({marks});",
                [time.strftime("%Y-%m-%d %H:%M:%S")] + order_nos,
            )
            stamped = cur.rowcount

    conn = get_conn()
    inspect_n = conn.execute(
        "SELECT COUNT(*) c FROM inspect_db.inspect_records;").fetchone()["c"]

    print(f"\n--- 导入结果 ---")
    print(f"  成功 {ok} 条 · 失败 {failed} 条 · 耗时 {time.time()-started:.1f}s")
    print(f"  标记为已同步（source=kdocs）：{stamped} 条")
    print(f"  检测登记库现有记录：{inspect_n} 条")
    if failures:
        print("  失败明细：")
        for line, msg in failures[:10]:
            print(f"    第 {line} 行：{msg}")

    st = db_status()
    print(f"\n--- 三库概览 ---")
    print(f"  退回 {st['total']} 条 · 已检测 {st['inspected']} 条 · "
          f"物料 {st['item_master']} 条 · 型号字典 {st['model_dict']} 条")
    close_conn()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
