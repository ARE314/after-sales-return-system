"""核销台账 返件归属差异表 —— 用**程序自己的匹配逻辑**把归位不上的返件摊开。

为什么要有这个脚本：
    台账的一行 = 整机厂家 + 项目风场。返件（returns_db.returns）靠厂家名匹配 +
    风场名评分自动归位；对不上的那些**件数不会进「已返回」**。用户要据此给一份
    别名表（`auth_db.setting.ledger_alias`，形如 {"vendors":{},"sites":{}}），
    所以这里把「归位不上」的返件按（厂家）/（厂家+风场）聚合出来，并给出近似建议。

用法（在项目根目录跑）：
    python tools/ledger_alias_report.py                      # 自动口径
    python tools/ledger_alias_report.py --need-mode demo     # 指定口径
    python tools/ledger_alias_report.py --out data/exports   # 换输出目录

产出（UTF-8-SIG，Excel 双击不乱码）：
    台账-归属统计.csv        归位 / 未匹配 / 歧义 的行数件数，按原因分类
    台账-厂家差异-待补.csv   返件侧厂家 → 发货侧候选厂家（附近似建议）
    台账-风场差异-待补.csv   （返件厂家 + 返件风场）→ 台账候选维度（附分数）
    台账-发货侧维度清单.csv  台账现有的厂家/风场/发货件数/返件件数

只读：不改任何数据、不写 setting，别名要用户在界面上填或自己写库。
"""
from __future__ import annotations

import argparse
import csv
import difflib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DATA_DIR                                   # noqa: E402
from core import repo_delivery as R                           # noqa: E402

PLACEHOLDERS = ("(待补充)", "（待补充）", "待补充", "-", "--", "无", "/", "N/A", "n/a")


def _is_blank(v) -> bool:
    s = str(v or "").strip()
    return (not s) or (s in PLACEHOLDERS)


def _why(item: dict, kind: str) -> str:
    """给一行归位不上的返件写人话原因。

    「候选不唯一」其实分两种，用户要做的事完全不同：
      · 候选列表是空的 → 厂家对得上、风场名一个都对不上（要给风场别名或补录）；
      · 有候选但同分 → 名字都对得上，认不出是哪一个（要给风场别名裁决）。
    """
    v_blank, s_blank = _is_blank(item.get("turbine_vendor")), _is_blank(item.get("project_site"))
    if kind == "unmatched":
        if v_blank:
            return "返件没填整机厂家"
        return "发货侧没有这个厂家的任何维度（可能只是叫法不同）"
    if not (item.get("candidates") or []):
        if s_blank:
            return "返件没填项目风场（填的是占位符），归不到任何风场"
        return "厂家对得上，但风场名与台账里的都对不上"
    if s_blank:
        return "返件没填项目风场，而该厂家在发货侧有多个风场，认不出归哪个"
    return "风场名对得上多个台账维度，认不出是哪一个（要给风场别名）"


def _why_text(counter: dict) -> str:
    """把同一厂家的多种原因按行数从多到少拼起来（只写一个原因会漏掉另一半）。"""
    if not counter:
        return ""
    return "；".join(f"{why}（{n} 行）"
                    for why, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, R._ledger_norm(a), R._ledger_norm(b)).ratio()


def _suggest(name: str, pool: list, top: int = 3) -> str:
    scored = sorted(((_sim(name, x), x) for x in pool), reverse=True)
    good = [(round(s, 2), x) for s, x in scored if s >= 0.4][:top]
    return "；".join(f"{x}（{s}）" for s, x in good)


def _write(path: str, header: list, rows: list) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--need-mode", default="auto", choices=R.LEDGER_NEED_MODES)
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "exports"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    snap = R._ledger_snapshot(args.need_mode)
    groups, meta = snap["groups"], snap["meta"]
    order = snap["order"]
    print(f"口径 {meta['need_mode']}｜台账 {len(order)} 个维度｜发货 "
          f"{meta['ship_lines']} 行 / {meta['ship_docs']} 单｜返件 {meta['ret_lines']} 行")

    # ---------- 发货侧清单 ----------
    vend_rows = {}
    vendor_sites: dict = {}
    for k in order:
        g = groups[k]
        v = vend_rows.setdefault(g["turbine_vendor"], [0, 0.0, 0.0, 0.0, 0.0])
        v[0] += 1
        v[1] += g["ship_qty"]
        v[2] += g["ret_qty"]
        v[3] += g["clr_qty"]
        v[4] += g["unlink_qty"]
        vendor_sites.setdefault(g["turbine_vendor"], []).append(g["project_site"])
    p_dim = os.path.join(args.out, "台账-发货侧维度清单.csv")
    _write(p_dim, ["整机厂家", "项目风场", "发货单数", "发货行数", "发货件数",
                   "已返回件数", "已核销件数", "返件未核销件数", "状态"],
           [[groups[k]["turbine_vendor"], groups[k]["project_site"],
             len(groups[k]["docs"]), groups[k]["ship_lines"], groups[k]["ship_qty"],
             groups[k]["ret_qty"], groups[k]["clr_qty"], groups[k]["unlink_qty"],
             groups[k]["status"]] for k in order])
    p_vend = os.path.join(args.out, "台账-发货侧厂家清单.csv")
    _write(p_vend, ["整机厂家", "维度数", "发货件数", "已返回件数", "已核销件数",
                    "返件未核销件数"],
           [[v, *vals] for v, vals in sorted(vend_rows.items())])
    ship_vendors = sorted(vend_rows)

    # ---------- 归位不上的返件：按厂家 / 按（厂家 + 风场）聚合 ----------
    bad = [dict(x, _kind="unmatched", _why=_why(x, "unmatched")) for x in snap["unmatched"]]
    bad += [dict(x, _kind="ambiguous", _why=_why(x, "ambiguous")) for x in snap["ambiguous"]]

    by_vendor: dict = {}
    by_vs: dict = {}
    for x in bad:
        v = str(x.get("turbine_vendor") or "").strip() or "（空）"
        s = str(x.get("project_site") or "").strip() or "（空）"
        a = by_vendor.setdefault(v, {"lines": 0, "qty": 0.0, "sites": set(),
                                     "kinds": set(), "whys": {}})
        a["lines"] += 1
        a["qty"] += float(x.get("qty") or 0)
        a["sites"].add(s)
        a["kinds"].add(x["_kind"])
        a["whys"][x["_why"]] = a["whys"].get(x["_why"], 0) + 1
        b = by_vs.setdefault((v, s), {"lines": 0, "qty": 0.0, "kinds": set(),
                                      "whys": set(), "cands": {}})
        b["lines"] += 1
        b["qty"] += float(x.get("qty") or 0)
        b["kinds"].add(x["_kind"])
        b["whys"].add(x["_why"])
        for c in x.get("candidates") or []:
            key = f"{c['vendor']} / {c['site']}"
            b["cands"][key] = max(b["cands"].get(key, 0), c.get("score", 0))

    p_vd = os.path.join(args.out, "台账-厂家差异-待补.csv")
    _write(p_vd, ["返件侧整机厂家", "返件行数", "返件件数", "涉及风场数",
                  "现在的状态", "对不上的原因", "发货侧近似候选（名字+相似度）",
                  "请填：归到发货侧的哪个厂家", "备注"],
           [[v, a["lines"], round(a["qty"], 1), len(a["sites"]),
             "／".join(sorted(a["kinds"])), _why_text(a["whys"]),
             "" if _is_blank(v) else _suggest(v, ship_vendors, 5), "", ""]
            for v, a in sorted(by_vendor.items(), key=lambda kv: -kv[1]["qty"])])

    p_ss = os.path.join(args.out, "台账-风场差异-待补.csv")
    _rows_ss = []
    for (v, s), a in sorted(by_vs.items(), key=lambda kv: -kv[1]["qty"]):
        cands = "；".join(f"{k}（{sc}）" for k, sc in
                          sorted(a["cands"].items(), key=lambda kv: -kv[1])[:3])
        if not cands and not _is_blank(s):
            # 一个分数都没有：给该厂家台账里的风场按名字相似度排个近似建议
            pool = vendor_sites.get(v) or ([] if _is_blank(v) else [])
            cands = ("近似：" + _suggest(s, pool, 3)) if pool else ""
        _rows_ss.append([v, s, a["lines"], round(a["qty"], 1),
                         "／".join(sorted(a["kinds"])), "；".join(sorted(a["whys"])),
                         cands, "", "", ""])
    _write(p_ss, ["返件侧整机厂家", "返件侧项目风场", "返件行数", "返件件数",
                  "现在的状态", "对不上的原因", "台账里的候选风场（名字+分数 / 近似）",
                  "请填：归到台账的哪个风场", "请填：该厂家的别名（如需）", "备注"],
           _rows_ss)

    # ---------- 统计 ----------
    matched_vendors = {groups[k]["turbine_vendor"] for k in order}
    ret_by_vendor: dict = {}
    for r in snap["rets"]:
        v = str(r.get("turbine_vendor") or "").strip() or "（空）"
        a = ret_by_vendor.setdefault(v, [0, 0.0])
        a[0] += 1
        a[1] += float(r.get("qty") or 0)
    p_st = os.path.join(args.out, "台账-归属统计.csv")
    stat = [
        ["口径", meta["need_mode"]],
        ["台账维度数", len(order)],
        ["发货行数", meta["ship_lines"]],
        ["发货单数", meta["ship_docs"]],
        ["返件行数", meta["ret_lines"]],
        ["返件件数", meta["ret_qty"]],
        ["已归位行数", meta["ret_matched_lines"]],
        ["已归位件数", meta["ret_matched_qty"]],
        ["归位不上：没候选", meta["ret_unmatched_lines"]],
        ["归位不上：没候选件数", meta["ret_unmatched_qty"]],
        ["归位不上：候选不唯一", meta["ret_ambiguous_lines"]],
        ["归位不上：候选不唯一件数", meta["ret_ambiguous_qty"]],
        ["返件侧出现过的厂家数", len(ret_by_vendor)],
        ["其中发货侧也有同名（或别名）的厂家数",
         sum(1 for v in ret_by_vendor
             if v in matched_vendors or any(
                 R._vendor_match(sv, v, snap["alias"]["vendors"]) for sv in ship_vendors))],
        ["厂家别名条数（内置 + 自定义）", meta["alias_vendors"]],
        ["风场别名条数（内置 + 自定义）", meta["alias_sites"]],
        ["孤立核销记录（找不到维度）", meta["orphan_clears"]],
    ]
    _write(p_st, ["指标", "数值"], stat)

    for p in (p_st, p_vd, p_ss, p_vend, p_dim):
        print("写好了：", p)
    print(f"归位不上：没候选 {meta['ret_unmatched_lines']} 行 / "
          f"{meta['ret_unmatched_qty']} 件；候选不唯一 {meta['ret_ambiguous_lines']} 行 / "
          f"{meta['ret_ambiguous_qty']} 件；厂家差异 {len(by_vendor)} 个、"
          f"风场差异 {len(by_vs)} 组")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
