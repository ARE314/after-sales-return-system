# -*- coding: utf-8 -*-
"""导出「疑似销售端」返件清单，供业务确认清洗口径。

背景（2026-09-23）：台账的清洗口径是 `returns.info_source`（快递归属）='销售端'，
但库里还有一批返件快递归属写着「售后端」，反馈现象里却写着销售错发 / 销售返回 /
销售退回……。这批要不要也当销售端剔掉是**业务判断**，所以导成 CSV 给业务看。

输出（`data/exports/`）：
  台账-销售端候选-反馈现象.csv   逐行明细（单号 / 维度 / 数量 / 反馈现象）
  台账-销售端候选-按现象汇总.csv 按反馈现象汇总（行数 + 件数）

用法：python -X utf8 tools/ledger_sales_review.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import dbapi  # noqa: E402

OUT = ROOT / "data" / "exports"
OUT.mkdir(parents=True, exist_ok=True)

# 与台账 `_RET_LEDGER_SQL` 同一个范围：整机厂家填了的返件
SCOPE = "TRIM(COALESCE(turbine_vendor,'')) <> ''"
# 只挑「不是销售端快递归属、但反馈现象里写了销售」的
CAND = "COALESCE(info_source,'') <> '销售端' AND COALESCE(feedback_issue,'') LIKE '%销售%'"

DETAIL_SQL = f"""
SELECT id, return_no, return_date, info_source, turbine_vendor, project_site,
       product_model, product_name, spec, material_no, product_code,
       COALESCE(NULLIF(return_qty,0),1) AS qty, feedback_issue, registrar
  FROM returns_db.returns
 WHERE {SCOPE} AND {CAND}
 ORDER BY turbine_vendor, project_site, id
"""

SUMMARY_SQL = f"""
SELECT COALESCE(NULLIF(TRIM(feedback_issue),''),'(空)') AS issue,
       COUNT(*) AS cnt,
       ROUND(SUM(COALESCE(NULLIF(return_qty,0),1)),1) AS qty,
       SUM(CASE WHEN COALESCE(info_source,'')='' THEN 1 ELSE 0 END) AS no_source
  FROM returns_db.returns
 WHERE {SCOPE} AND {CAND}
 GROUP BY issue ORDER BY cnt DESC, qty DESC
"""


def main() -> int:
    conn = dbapi.get_conn()
    rows = conn.execute(DETAIL_SQL, ()).fetchall()
    sum_rows = conn.execute(SUMMARY_SQL, ()).fetchall()

    p1 = OUT / "台账-销售端候选-反馈现象.csv"
    with p1.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "退回单号", "退回日期", "快递归属", "整机厂家", "项目风场",
                    "产品型号", "产品名称", "规格", "物料编码", "产品编码", "数量",
                    "反馈现象", "登记员"])
        for i, r in enumerate(rows, 1):
            w.writerow([i, r["return_no"], r["return_date"], r["info_source"],
                        r["turbine_vendor"], r["project_site"], r["product_model"],
                        r["product_name"], r["spec"], r["material_no"],
                        r["product_code"], r["qty"], r["feedback_issue"],
                        r["registrar"]])

    p2 = OUT / "台账-销售端候选-按现象汇总.csv"
    with p2.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["反馈现象", "返件行数", "返件件数", "其中快递归属为空的行数"])
        for r in sum_rows:
            w.writerow([r["issue"], r["cnt"], r["qty"], r["no_source"]])

    total_q = round(sum(float(r["qty"] or 0) for r in rows), 1)
    print(f"明细 {len(rows)} 行 · {total_q} 件 → {p1}")
    print(f"汇总 {len(sum_rows)} 种现象 → {p2}")
    for r in sum_rows:
        print(f"  {r['cnt']:>3} 行 / {r['qty']:>6} 件  {r['issue']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
