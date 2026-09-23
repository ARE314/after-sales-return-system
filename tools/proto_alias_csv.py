"""核销台账原型 · 别名对照表生成器（原型配套，不改程序、不写库）

用途：把「返件登记侧的厂家/风场写法」与「发货侧（ERP 出货镜像）写法」的差异列成表。
用户核对后回一份 `data/proto/别名表.json`（{"vendors":{"写法A":"写法B"},"sites":{...}}，
方向不限），原型与本事具都会自动载入校准。

产物（都在 data/proto/）：
    厂家别名-待补.csv   返件侧每个厂家 ↔ 发货侧候选厂家（匹配方式：完全一致/别名表/互相包含/无候选）
    风场别名-待补.csv   返件侧每个「厂家+风场」分组 ↔ 发货侧同厂家候选风场（前 3 个）+ 原型当前归位结果
    发货侧厂家清单.csv  发货侧拆出来的整机厂家（发货行/件数、风场数、原始 customer 示例）
    发货侧维度清单.csv  发货侧全部「厂家+风场」维度的单据数/行数/件数

匹配规则与原型完全一致（直接 import tools/proto_ledger.py 的
`norm/vendor_match/site_score/parse_dim`，并复用同一份 SHIP_SQL/RET_SQL），
所以表里写「已归位」的，原型里一定归位。

用法：
    python tools/proto_alias_csv.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import proto_ledger as P  # noqa: E402

from core import dbapi  # noqa: E402

OUT = P.OUT_DIR


def _ship_dims(ships, rv_set):
    """{(vendor, site): {"docs":set, "rows":int, "qty":float, "raw":set}}

    厂家先过 canon_vendor 收敛（与原型同一逻辑），所以清单里的维度就是台账里会出现的维度。
    """
    dims: dict[tuple, dict] = {}
    for r in ships:
        v, s = P.parse_dim(r["customer"])
        v = P.canon_vendor(v, rv_set)
        d = dims.setdefault((v, s), {"docs": set(), "rows": 0, "qty": 0.0, "raw": set()})
        d["docs"].add(r["doc_no"])
        d["rows"] += 1
        d["qty"] += float(r["qty"] or 0)
        if len(d["raw"]) < 2:
            d["raw"].add(r["customer"] or "")
    return dims


def _ret_dims(rets):
    """{(vendor, site): {"rows":int, "qty":float}}"""
    dims: dict[tuple, dict] = {}
    for r in rets:
        v = (r["turbine_vendor"] or "").strip()
        s = (r["project_site"] or "").strip()
        d = dims.setdefault((v, s), {"rows": 0, "qty": 0.0})
        d["rows"] += 1
        d["qty"] += float(r["return_qty"] or 0)
    return dims


def _cands(rv, rs, sdims):
    """与原型同一套规则，返回 [(score, vendor, site, dim), ...] 按分数/件数倒序。"""
    out = []
    same_vendor = [(k, d) for k, d in sdims.items() if P.vendor_match(k[0], rv)]
    for (v, s), d in same_vendor:
        sc = P.site_score(s, rs)
        if sc == 0:
            # 返件没填风场时，只有该厂家唯一一个分组才敢归位（与原型一致）
            if not P.norm(rs) and len(same_vendor) == 1:
                sc = 1
            else:
                continue
        out.append((sc, v, s, d))
    out.sort(key=lambda x: (-x[0], -x[3]["qty"]))
    return out


def _bigrams(s: str) -> set:
    s = P.norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def _near(rs, same_vendor, n=3):
    """严格规则匹配不到时，给「同厂家最相近的风场写法」做人工核对参考（不参与自动归位）。"""
    b = _bigrams(rs)
    out = []
    for (v, s), d in same_vendor:
        o = _bigrams(s)
        if not b or not o:
            continue
        j = len(b & o) / len(b | o)
        if j > 0:
            out.append((j, v, s, d))
    out.sort(key=lambda x: (-x[0], -x[3]["qty"]))
    return out[:n]


def main() -> int:
    cur = dbapi.get_conn().cursor()
    cur.execute(P.SHIP_SQL)
    ships = cur.fetchall()
    cur.execute(P.RET_SQL)
    rets = cur.fetchall()
    rv_set = {(r["turbine_vendor"] or "").strip() for r in rets}
    if not ships:
        print("发货明细里没有「售后发货单 + 已核准」的数据，先同步发货明细")
        return 1

    sdims = _ship_dims(ships, rv_set)
    rdims = _ret_dims(rets)
    svendors: dict[str, dict] = {}
    for (v, s), d in sdims.items():
        o = svendors.setdefault(v, {"rows": 0, "qty": 0.0, "sites": set()})
        o["rows"] += d["rows"]
        o["qty"] += d["qty"]
        o["sites"].add(s)

    OUT.mkdir(parents=True, exist_ok=True)
    alias_v = sorted({k for k in P.VENDOR_ALIAS} | {v for v in P.VENDOR_ALIAS.values()})

    # ① 厂家差异表
    rvendors: dict[str, dict] = {}
    for (v, s), d in rdims.items():
        o = rvendors.setdefault(v, {"rows": 0, "qty": 0.0})
        o["rows"] += d["rows"]
        o["qty"] += d["qty"]
    rows = []
    for rv, o in sorted(rvendors.items(), key=lambda x: -x[1]["qty"]):
        cands = [(v, d) for v, d in svendors.items() if P.vendor_match(v, rv)]
        if not cands:
            rows.append([rv, o["rows"], o["qty"], "", 0, 0.0, "无候选（发货侧没这个厂）", 0, ""])
            continue
        cands.sort(key=lambda x: -x[1]["qty"])
        v, d = cands[0]
        if P.norm(v) == P.norm(rv):
            how = "完全一致"
        elif P.VENDOR_ALIAS.get(v) or P.VENDOR_ALIAS.get(rv):
            how = "别名表（已内置）"
        else:
            how = "互相包含（已自动匹配）"
        rows.append([rv, o["rows"], o["qty"], v, d["rows"], d["qty"], how, len(d["sites"]),
                     "" if how == "完全一致" else f"{rv} = {v}"])
    _csv(OUT / "厂家别名-待补.csv",
         ["返件侧厂家", "返件行数", "返件件数", "发货侧匹配厂家", "发货行数", "发货件数",
          "匹配方式", "发货侧风场数", "拟用别名（请核对/改写，或留空）"], rows)

    # ② 风场差异表（只列返件侧填了风场的分组）
    rows = []
    for (rv, rs), o in sorted(rdims.items(), key=lambda x: -x[1]["qty"]):
        if not rs:
            continue
        cs = _cands(rv, rs, sdims)
        top = cs[:3]
        if top:
            cand_txt = "；".join(f"{c[2]}（匹配度{c[0]}，{c[3]['rows']}行/{c[3]['qty']:,.0f}件）" for c in top)
        else:
            same_vendor = [(k, d) for k, d in sdims.items() if P.vendor_match(k[0], rv)]
            near = _near(rs, same_vendor)
            cand_txt = ("近似候选：" + "；".join(f"{c[2]}（重合度{c[0]:.2f}，{c[3]['rows']}行）" for c in near)) if near else "（无候选）"
        if not cs:
            result = "未匹配"
        else:
            ties = sum(1 for c in cs if c[0] == cs[0][0])
            result = "已归位" if ties == 1 else f"已归位（同分候选 {ties} 个）"
        rows.append([
            rv, rs, o["rows"], o["qty"], cand_txt,
            {3: "完全一致/别名", 2: "互相包含", 1: "该厂家唯一分组"}.get(cs[0][0], "—") if cs else "—",
            result,
            f"{rs} = {cs[0][2]}" if cs and result == "已归位" and cs[0][0] != 3 else "",
        ])
    _csv(OUT / "风场别名-待补.csv",
         ["返件侧厂家", "返件侧项目风场", "返件行数", "返件件数", "发货侧候选风场（前3）",
          "最佳匹配方式", "原型当前结果", "拟用别名（请核对/改写，或留空）"], rows)

    # ③ 发货侧厂家清单
    rows = []
    for v, o in sorted(svendors.items(), key=lambda x: -x[1]["qty"]):
        raw = set()
        for k, d in sdims.items():
            if k[0] == v:
                raw |= d["raw"]
        rows.append([v, o["rows"], o["qty"], len(o["sites"]), "；".join(sorted(raw)[:2])])
    _csv(OUT / "发货侧厂家清单.csv",
         ["发货侧厂家", "发货行数", "发货件数", "风场数", "原始 customer 示例"], rows)

    # ④ 发货侧维度清单
    rows = []
    for (v, s), d in sorted(sdims.items(), key=lambda x: -x[1]["qty"]):
        rows.append([v, s or "（未标注风场）", len(d["docs"]), d["rows"], round(d["qty"], 1)])
    _csv(OUT / "发货侧维度清单.csv",
         ["整机厂家", "项目风场", "发货单数", "发货行数", "发货件数"], rows)

    print(f"厂家差异表 {len(rvendors)} 个返件侧厂家 · 风场差异表 {sum(1 for k in rdims if k[1])} 个分组 · "
          f"发货侧 {len(svendors)} 个厂家 / {len(sdims)} 个维度 / {len(ships)} 行发货明细")
    print(f"别名表内置：厂家 {len(alias_v)} 条 · 风场 {len(P.SITE_ALIAS)} 条 · "
          f"{P.ALIAS_FILE.name} {'已载入' if P.ALIAS_FILE.exists() else '还不存在（把填好的 JSON 放到这个路径即可）'}")
    print("产物：" + " · ".join(p.name for p in
          [OUT / "厂家别名-待补.csv", OUT / "风场别名-待补.csv",
           OUT / "发货侧厂家清单.csv", OUT / "发货侧维度清单.csv"]))
    return 0


def _csv(path: Path, header: list[str], rows: list[list]):
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
