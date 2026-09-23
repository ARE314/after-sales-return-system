#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成「核销台账」改版的**初版原型**（自包含 HTML，真实数据烘焙进去）。

用户需求（2026-09-22）：
  1. 核销台账内只显示发货明细内的「售后发货单」；
  2. 显示的售后发货单必须是已核准状态、且发货申请时填了「需要返回」的；
  3. 主界面按「同整机厂家 + 同项目风场」汇总，显示
     发货数量 / 已返回数量 / 已核销数量 / 待核销数量 / 核销状态；
  4. 点汇总条目进二级页面，分别显示这五项的明细；
  5. 二级页显示「该项目已返回传感器但未作为核销关联」的数量与明细，并带手动核销关联按钮。

本脚本**只产出原型文件**，不写任何程序代码、不改数据库、不动 static/ 与 core/。
原型里的「手动/自动核销关联」只暂存在浏览器 localStorage（供评审交互用），
每一处暂定口径在页面顶部「口径」面板里写明。

用法：
    python tools/proto_ledger.py
产物：
    data/proto/核销台账-初版.html
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import dbapi  # noqa: E402

OUT_DIR = ROOT / "data" / "proto"
OUT_FILE = OUT_DIR / "核销台账-初版.html"

# 只取「售后发货单 + 已核准」，其余单据类型不进这个台账（需求 1、2）
SHIP_SQL = """
SELECT doc_no, doc_date, material_no, material_name, product_model, qty, serial_no,
       customer, express_no, contact, address
FROM delivery_db.ship_detail
WHERE doc_type = '售后发货单' AND doc_status = '已核准'
ORDER BY doc_date DESC, doc_no DESC
"""

RET_SQL = """
SELECT id, detail_key, order_no, return_no, carrier, return_date,
       turbine_vendor, project_site, info_source, product_model, product_category,
       product_name, spec, return_qty, registrar
FROM returns_db.returns
ORDER BY return_date DESC, id DESC
"""

# 本地发货申请链路 —— 需求 2「需要返回」的判定基准。
#   2026-09-22 用户定口径：「需要返回关联发货申请单判断」→ 不再用料号前缀猜。
#   链路：发货明细的售后发货单号 ←→ delivery_shipment.ship_no
#         （发货员在 ERP 做完单子回来登记时填的那个单号）
#         → delivery_shipment.request_no → delivery_request_item.need_return
#   ⚠️ delivery_request / delivery_shipment 现在都是 0 行，所以**正式口径下台账暂时是空的**
#      —— 原型会自动切到「料号前缀」演示口径，并在页面顶部写明原因（见 needInfo）。
LINK_SQL = """
SELECT s.ship_no, s.request_no, s.line_no, s.material_no, s.product_model, s.qty,
       COALESCE(s.need_return, 0) AS need_return,
       r.turbine_vendor, r.project_site
FROM delivery_db.delivery_shipment s
LEFT JOIN delivery_db.delivery_request r ON r.request_no = s.request_no
ORDER BY s.id DESC
"""

REQ_SQL = """
SELECT COUNT(*) AS items,
       COALESCE(SUM(CASE WHEN need_return THEN 1 ELSE 0 END), 0) AS need
FROM delivery_db.delivery_request_item
"""

# 厂家别名：发货侧写法 → 返件登记侧写法（返件是人工登记的，用词不一致）
VENDOR_ALIAS = {
    "明阳风电": "明阳智能",
    "明阳智慧能源集团股份公司": "明阳智能",
    "长沙七维传感技术有限公司": "长沙七维",
    "上海读风者新能源有限公司": "上海读风者",
    "安赛尔": "安赛尔机电",
}

# 风场别名（返件侧写法 → 发货侧写法）。用户会照 data/proto/风场别名-待补.csv
# 给一份，届时直接写进 data/proto/别名表.json，不用改代码：
#   {"vendors": {"返件侧写法": "发货侧写法"}, "sites": {"返件风场写法": "发货侧风场写法"}}
SITE_ALIAS = {}

ALIAS_FILE = OUT_DIR / "别名表.json"
if ALIAS_FILE.exists():
    try:
        _al = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
        VENDOR_ALIAS.update(_al.get("vendors") or {})
        SITE_ALIAS.update(_al.get("sites") or {})
        print(f"已载入别名表 {ALIAS_FILE.name}：厂家 {len(_al.get('vendors') or {})} 条、"
              f"风场 {len(_al.get('sites') or {})} 条")
    except Exception as exc:  # 别名表写坏了不该让原型生成失败
        print(f"⚠️ 别名表 {ALIAS_FILE} 解析失败（已忽略）：{exc}")

DIM_RE = re.compile(r"^(?P<v>.*?)\s*[（(]\s*(?P<s>.*?)\s*[）)]\s*$")


def norm(s) -> str:
    """去空白、统一全半角括号、去掉结尾的分隔点，用于比对。"""
    if s is None:
        return ""
    t = str(s).replace("（", "(").replace("）", ")")
    t = re.sub(r"[\s\u3000]+", "", t)
    return t.strip(".．。")


def parse_dim(customer: str):
    """把发货明细的 customer 拆成（整机厂家, 项目风场）。"""
    c = (customer or "").strip()
    m = DIM_RE.match(c)
    if m:
        return m.group("v").strip(), m.group("s").strip()
    return c, ""


def vendor_match(v: str, rv: str) -> bool:
    a, b = norm(v), norm(rv)
    if not a or not b:
        return False
    if a == b:
        return True
    alias = VENDOR_ALIAS.get(v) or VENDOR_ALIAS.get(v.strip())
    if alias and norm(alias) == b:
        return True
    # 别名表方向不限：两边都查一遍，用户给的对照表怎么顺手怎么写
    alias2 = VENDOR_ALIAS.get(rv) or VENDOR_ALIAS.get(str(rv or "").strip())
    if alias2 and norm(alias2) == a:
        return True
    return len(a) >= 3 and len(b) >= 3 and (a in b or b in a)


def canon_vendor(v: str, rv_set: set) -> str:
    """把发货侧厂家的各种写法**收敛成同一个维度**（别名表自动校准）。

    用户口径：「目前明阳风电就是明阳智能」→ 发货侧写「明阳风电（xx项目）」的那些行，
    必须和写「明阳智能（xx项目）」的行汇总成同一行，否则台账里会出现两个明阳。

    规则（确定性、可解释）：
      1. 若 v 在别名表里 → 取别名目标；
      2. 若别名目标/原名里有一个是真的「返件侧厂家」写法 → 优先用它（台账是给返件核销看的，
         用返件登记的写法做维度名最直观）；
      3. 都不在返件侧 → 用别名目标（没有别名就是原名）。
    """
    a = VENDOR_ALIAS.get(v) or VENDOR_ALIAS.get((v or "").strip()) or v
    if a in rv_set:
        return a
    if v in rv_set:
        return v
    return a


def site_score(site: str, rsite: str) -> int:
    a, b = norm(site), norm(rsite)
    if not a or not b:
        return 0
    if a == b:
        return 3
    for x, y in ((rsite, a), (site, b)):
        alias = SITE_ALIAS.get(x) or SITE_ALIAS.get(str(x or "").strip())
        if alias and norm(alias) == y:
            return 3
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return 2
    return 0


def main() -> int:
    cur = dbapi.get_conn().cursor()
    cur.execute(SHIP_SQL)
    ships = cur.fetchall()
    cur.execute(RET_SQL)
    rets = cur.fetchall()
    rv_set = {(r["turbine_vendor"] or "").strip() for r in rets}
    cur.execute(LINK_SQL)
    link_rows = cur.fetchall()
    cur.execute(REQ_SQL)
    req = cur.fetchone() or {"items": 0, "need": 0}
    if not ships:
        print("发货明细里没有「售后发货单 + 已核准」的数据，无法生成原型")
        return 1

    # ---- 汇总维度：整机厂家 + 项目风场 ----
    # ★ 厂家先过一遍别名表收敛（canon_vendor）：发货侧「明阳风电（xx）」与「明阳智能（xx）」
    #   会汇成同一行；风场写法差异仍靠 site_score 评分归位。
    raw_vendors = set()
    groups = {}          # key -> {"id","vendor","site","ship":[...],"ret":[...]}
    for r in ships:
        vendor, site = parse_dim(r["customer"])
        raw_vendors.add(vendor)
        vendor = canon_vendor(vendor, rv_set)
        key = vendor + "\x1f" + site
        g = groups.setdefault(key, {"id": len(groups), "vendor": vendor, "site": site,
                                    "ship": [], "ret": []})
        g["ship"].append(r)
    merged = len(raw_vendors) - len({g["vendor"] for g in groups.values()})
    if merged:
        print(f"厂家别名收敛：发货侧 {len(raw_vendors)} 种写法 → {len({g['vendor'] for g in groups.values()})} 个维度厂家"
              f"（合并 {merged} 个）")

    # ---- 返件归位：先按厂家，再按风场名评分 ----
    matched = unmatched = ambiguous = 0
    for r in rets:
        rv, rs = r["turbine_vendor"], r["project_site"]
        best, best_score, ties = None, 0, 0
        for key, g in groups.items():
            if not vendor_match(g["vendor"], rv):
                continue
            sc = site_score(g["site"], rs)
            if sc == 0:
                # 返件没填风场时，只有该厂家唯一一个分组才敢归位
                if not norm(rs) and sum(1 for k2, g2 in groups.items()
                                        if vendor_match(g2["vendor"], rv)) == 1:
                    sc = 1
                else:
                    continue
            if sc > best_score:
                best, best_score, ties = key, sc, 1
            elif sc == best_score:
                ties += 1
        if best and ties == 1:
            groups[best]["ret"].append(r)
            matched += 1
        elif best and ties > 1:
            groups[best]["ret"].append(r)
            ambiguous += 1
        else:
            unmatched += 1

    # ---- 烘焙进原型的数据 ----
    by_key = {k: g for k, g in groups.items()}
    ship_rows = []
    for r in ships:
        vendor, site = parse_dim(r["customer"])
        vendor = canon_vendor(vendor, rv_set)
        gid = groups[vendor + "\x1f" + site]["id"]
        ship_rows.append([
            r["doc_no"] or "", r["doc_date"] or "", r["material_no"] or "",
            r["material_name"] or "", r["product_model"] or "", float(r["qty"] or 0),
            r["serial_no"] or "", r["customer"] or "", r["express_no"] or "",
            r["contact"] or "", (r["address"] or "")[:80], gid,
        ])
    gid_of_ret = {}
    for g in groups.values():
        for x in g["ret"]:
            gid_of_ret[x["id"]] = g["id"]
    ret_rows = []
    for r in rets:
        ret_rows.append([
            r["id"], r["return_no"] or "", r["return_date"] or "", r["product_model"] or "",
            r["product_name"] or "", r["product_category"] or "", r["spec"] or "",
            float(r["return_qty"] or 0), r["info_source"] or "", r["carrier"] or "",
            r["registrar"] or "", r["turbine_vendor"] or "", r["project_site"] or "",
            gid_of_ret.get(r["id"]), r["order_no"] or "", r["detail_key"] or "",
        ])
    gname = {g["id"]: (g["vendor"], g["site"]) for g in groups.values()}
    nosite = sum(1 for g in groups.values() if not g["site"])

    # 本地发货申请链路（需求 2 的判定基准）
    link_baked = [[r["ship_no"] or "", r["request_no"] or "", r["line_no"],
                   r["material_no"] or "", r["product_model"] or "", float(r["qty"] or 0),
                   1 if r["need_return"] else 0, r["turbine_vendor"] or "", r["project_site"] or ""]
                  for r in link_rows]
    link_docs = {x[0] for x in link_baked if x[6]}
    ship_docs = {r[0] for r in ship_rows}

    data = {
        "gen_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "groups": [{"id": g["id"], "k": k, "v": g["vendor"], "s": g["site"]}
                   for k, g in groups.items()],
        "ship": ship_rows,
        "ret": ret_rows,
        "links": link_baked,
        "meta": {
            "ship_lines": len(ship_rows), "ship_docs": len(ship_docs),
            "ship_qty": round(sum(r[5] for r in ship_rows), 1),
            "ret_lines": len(ret_rows), "ret_qty": round(sum(r[7] for r in ret_rows), 1),
            "ret_matched": matched, "ret_unmatched": unmatched, "ret_ambiguous": ambiguous,
            "link_rows": len(link_baked), "link_docs": len(link_docs),
            "link_hit": len(ship_docs & link_docs),
            "req_items": int(req["items"] or 0), "req_need": int(req["need"] or 0),
            "alias_vendors": len(VENDOR_ALIAS), "alias_sites": len(SITE_ALIAS),
        },
    }

    # 控制台核对用（原型页面里的数字由同一套规则在浏览器里重算）
    by_gid_ship = {}
    for row in ship_rows:
        st = by_gid_ship.setdefault(row[11], {"lines": 0, "qty": 0.0, "docs": set()})
        st["lines"] += 1
        st["qty"] += row[5]
        st["docs"].add(row[0])
    total_ret_qty = sum(r[7] for r in ret_rows)
    top = sorted(by_gid_ship.items(), key=lambda kv: -kv[1]["qty"])[:15]
    print(f"售后发货单(已核准) {len(ship_rows)} 行 / "
          f"{len({r[0] for r in ship_rows})} 单 / {sum(r[5] for r in ship_rows):,.1f} 件")
    print(f"汇总维度 {len(groups)} 组（其中 {nosite} 组拆不出风场）；"
          f"返件 {len(ret_rows)} 行 / {total_ret_qty:,.1f} 件"
          f"（归位 {matched}，多候选 {ambiguous}，未匹配 {unmatched}）")
    print("发货数量前十维度：")
    for gid, st in top:
        v, s = gname[gid]
        print(f"   {v[:14]:<16} {s[:30]:<32} {len(st['docs']):>4} 单 {st['lines']:>4} 行 {st['qty']:>10,.1f} 件")
    pre = {}
    for row in ship_rows:
        p = (row[2] or "?")[:1]
        st = pre.setdefault(p, [0, 0.0])
        st[0] += 1
        st[1] += row[5]
    print("料号前缀分布：" + " · ".join(
        f"{p}xxxx {v[0]}行/{v[1]:,.0f}件" for p, v in sorted(pre.items())))
    m = data["meta"]
    print(f"发货申请链路：申请明细 {m['req_items']} 行（其中需返回 {m['req_need']} 行）· "
          f"已登记发货记录 {m['link_rows']} 行 · 关联到申请单的发货单号 {m['link_docs']} 个 · "
          f"命中发货明细 {m['link_hit']} 单"
          + ("   ← 正式口径下台账为空（申请/发货单还没数据），原型默认切演示口径"
             if m['link_hit'] == 0 else ""))
    print(f"别名表：厂家 {m['alias_vendors']} 条 · 风场 {m['alias_sites']} 条"
          + (f"（来自 {ALIAS_FILE.name}）" if ALIAS_FILE.exists() else "（内置默认）"))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    link_info = (f"发货申请登记 {m['link_docs']} 个单号 / {m['link_rows']} 行，"
                 f"其中能对上发货明细 {m['link_hit']} 单")
    html = (HTML_TEMPLATE
            .replace("__DATA__", json.dumps(data, ensure_ascii=False))
            .replace("__GEN__", data["gen_at"])
            .replace("__SHIPN__", f"{len(ship_rows):,}")
            .replace("__SHIPDOC__", f"{len({r[0] for r in ship_rows}):,}")
            .replace("__SHIPQTY__", f"{sum(r[5] for r in ship_rows):,.1f}")
            .replace("__NOSITE__", str(nosite))
            .replace("__LINKINFO__", link_info)
            .replace("__RETN__", str(len(ret_rows))))
    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"\n已生成原型：{OUT_FILE}（{OUT_FILE.stat().st_size:,} 字节）")
    return 0


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>核销台账 · 初版原型</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--line:#e3e8ef;--ink:#1c2434;--mute:#6b7688;--brand:#2b5cd9;
      --ok:#0f9d58;--warn:#c77700;--danger:#d93025;--info:#1a73e8;--chip:#eef2ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
.wrap{max-width:1420px;margin:0 auto;padding:18px 22px 60px}
header.top{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:14px}
.brand{display:flex;align-items:center;gap:10px;font-size:20px;font-weight:700}
.sub{color:var(--mute);font-size:12.5px}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600;line-height:1.7}
.badge-proto{background:#fff4e5;color:#a55b00;border:1px solid #ffd8a8}
.badge-ok{background:#e7f6ec;color:#0b7a43;border:1px solid #b7e2c6}
.badge-warn{background:#fff7e6;color:#a55b00;border:1px solid #ffe0a3}
.badge-gray{background:#eef1f5;color:#5a6577;border:1px solid #dde3ec}
.badge-info{background:#e8f0fe;color:#1a56c4;border:1px solid #c6d8fb}
.badge-danger{background:#fdecea;color:#b3261e;border:1px solid #f7c9c4}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:14px;overflow:hidden}
.card-h{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line)}
.card-h h2{margin:0;font-size:15px}
.card-b{padding:14px 16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px;margin-bottom:14px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.kpi .l{color:var(--mute);font-size:12.5px}
.kpi .v{font-size:23px;font-weight:700;margin-top:3px;letter-spacing:.3px}
.kpi .v small{font-size:12px;font-weight:500;color:var(--mute);margin-left:3px}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:#fbfcfe}
.toolbar input[type=text],.toolbar select{padding:6px 9px;border:1px solid #cfd8e3;border-radius:7px;font:inherit;background:#fff}
.toolbar input[type=text]{width:210px}
.toolbar label.ck{display:inline-flex;align-items:center;gap:5px;padding:4px 9px;border:1px solid var(--line);border-radius:20px;background:#fff;cursor:pointer;font-size:12.5px}
.toolbar .sp{flex:1}
button{font:inherit;border-radius:8px;cursor:pointer}
.btn{padding:6px 13px;border:1px solid var(--brand);background:var(--brand);color:#fff}
.btn:hover{filter:brightness(1.06)}
.ghost{padding:5px 11px;border:1px solid var(--line);background:#fff;color:var(--ink)}
.ghost:hover{background:#f2f5fa}
.link{border:none;background:none;color:var(--brand);text-decoration:underline;padding:0;cursor:pointer}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid #eef1f5;text-align:left;vertical-align:top}
th{background:#f7f9fc;font-weight:600;color:#48536b;white-space:nowrap;position:sticky;top:0;z-index:2}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{color:var(--brand)}
tbody tr:hover{background:#f8fafd}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.empty{padding:26px;text-align:center;color:var(--mute)}
.pager{display:flex;align-items:center;gap:8px;padding:10px 16px}
.pager .info{color:var(--mute);font-size:12.5px}
.mono{font-family:Consolas,"Courier New",monospace;font-size:12.5px}
.dim{color:var(--mute);font-size:12px}
ol.scope{margin:0;padding-left:20px}
ol.scope li{margin:3px 0}
.q{background:#fff8e6;border:1px solid #ffe2a8;border-radius:9px;padding:10px 12px;margin-top:10px;font-size:13px}
.q b{color:#8a5200}
.modal{position:fixed;inset:0;background:rgba(16,24,40,.5);display:none;z-index:50}
.modal.on{display:block}
.sheet{position:absolute;right:0;top:0;bottom:0;width:min(1180px,94vw);background:#fff;overflow:auto;box-shadow:-8px 0 28px rgba(0,0,0,.16)}
.sheet-h{position:sticky;top:0;background:#fff;border-bottom:1px solid var(--line);padding:13px 18px;display:flex;align-items:center;justify-content:space-between;gap:12px;z-index:3}
.sheet-h h3{margin:0;font-size:16px}
.sheet-b{padding:16px 18px 50px}
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin:14px 0 0}
.tabs button{padding:8px 14px;border:1px solid transparent;border-bottom:none;background:none;color:var(--mute);border-radius:8px 8px 0 0}
.tabs button.on{background:#fff;border-color:var(--line);border-bottom:1px solid #fff;color:var(--brand);font-weight:600;margin-bottom:-1px}
.hint{background:#eef4ff;border:1px solid #cfe0fb;border-radius:9px;padding:9px 12px;font-size:12.5px;color:#2b4a86;margin:10px 0}
.warnbox{background:#fff4f2;border:1px solid #f7c9c4;border-radius:9px;padding:9px 12px;font-size:12.5px;color:#8c2b22;margin:10px 0}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;background:var(--chip);color:#334}
details>summary{cursor:pointer;font-weight:600;color:#3a4761}
.legend{color:var(--mute);font-size:12px;display:flex;gap:14px;flex-wrap:wrap;padding:4px 16px 8px}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <div class="brand">核销台账
        <span class="badge badge-proto">初版原型 · 未写入程序</span>
      </div>
      <div class="sub">数据时点 __GEN__ · 只读快照烘焙进本文件 · 「核销关联」仅暂存本机浏览器，不进数据库</div>
    </div>
    <div class="sub" id="edge"></div>
  </header>

  <section class="card">
    <div class="card-h">
      <h2>口径（初版暂定 · 需要你确认的地方都在这里）</h2>
      <button class="ghost" onclick="var b=document.getElementById('scopeBody');b.style.display=b.style.display==='none'?'block':'none'">收起 / 展开</button>
    </div>
    <div class="card-b" id="scopeBody">
      <ol class="scope">
        <li><b>数据来源</b>：发货侧只取「发货明细」里的 <span class="mono">doc_type=售后发货单</span> 且 <span class="mono">doc_status=已核准</span>（ERP 出货镜像，共 __SHIPN__ 行 / __SHIPDOC__ 单 / __SHIPQTY__ 件）。</li>
        <li><b>需返回口径（你的决定）</b>：以 <b>发货申请单</b> 为判断依据 —— 发货明细的 <span class="mono">doc_no</span> 关联到本系统的发货申请单（<span class="mono">delivery_shipment.ship_no</span>），且该申请行 <span class="mono">need_return = 1</span> 才算「需要返回」。
          本原型同时保留「料号前缀（演示）」口径作为兜底 —— 因为现在发货申请还没有数据（__LINKINFO__），正式口径下列表会是空的。</li>
        <li><b>维度</b>：发货明细的 <span class="mono">customer</span> 字段实际是「整机厂家（项目风场）」复合串 → 按最后一个括号拆成 <b>整机厂家</b> + <b>项目风场</b>；拆不出括号的（__NOSITE__ 种，如「鑫维商贸」「东气自控」）单独成组、风场显示「（未标注风场）」。</li>
        <li><b>发货数量</b> = 该维度下符合「需返回」口径的售后发货单行数量合计。</li>
        <li><b>已返回数量</b> = 退回登记里归属该维度的 <span class="mono">return_qty</span> 合计（返件人工登记，厂家/风场写法与发货侧不一致，靠 <b>别名表</b> 自动校准）。</li>
        <li><b>已核销数量</b> = 「核销关联」的返件数量（你在二级页手动关联） <b>+ 手工清账</b> 的数量。二级页「核销明细」把两种方式分开列。</li>
        <li><b>待核销数量</b> = 发货数量 − 已核销数量（发货单为基准，你的口径）；工具条可临时切到「已返回 − 已核销」对比。</li>
        <li><b>核销状态</b>：已返回=0 → <span class="badge badge-gray">未返回</span>；已返回&gt;0 且已核销=0 → <span class="badge badge-warn">待核销</span>；0&lt;已核销&lt;已返回 → <span class="badge badge-info">部分核销</span>；已核销≥已返回 → <span class="badge badge-ok">已核销</span>。</li>
        <li><b>二级页以发货单为基准</b>：第一张表按 <b>发货单号</b> 汇总（每单一行：发货数量 / 已核销 / 待核销 / 状态 / 关联返件），再往下才是发货行明细。</li>
        <li><b>手动核销关联 / 手动清账</b>：都在二级页「未关联返件」那张表里，两个按钮并排（你的要求）。</li>
      </ol>
      <div class="q">
        <b>你已经拍板的 4 条（已按此实现）：</b>
        <div>① 「需要返回」＝<b>关联发货申请单</b>判断，不靠料号前缀；② 二级页<b>以发货单为基准</b>显示待核销；③ 先给你<b>整机厂家差异表</b>，你回一份别名表，程序内自动校准（<span class="mono">data/proto/别名表.json</span> 放进去即生效）；④ <b>手动清账按钮</b>放在「已返回但未核销关联」的产品旁边。</div>
        <div style="margin-top:8px"><b>还差你一句话的 2 件事：</b>
          ① 发货申请模块何时开始录数据？在它之前，正式口径下的台账列表必然为空 —— 原型默认先切「料号前缀（演示）」给你看界面，要不要这样？
          ② 别名表里 <span class="mono">明阳风电 → 明阳智能</span> 我已按你说的加进内置；风场那 19 组（<span class="mono">data/proto/风场别名-待补.csv</span>）也等你回。</div>
      </div>
    </div>
  </section>

  <section class="kpis" id="kpis"></section>

  <section class="card">
    <div class="toolbar">
      <input type="text" id="kw" placeholder="关键字：厂家 / 风场 / 单号 / 料号" oninput="state.kw=this.value;state.page=1;render()">
      <select id="fv" onchange="state.vendor=this.value;state.page=1;render()"><option value="">全部整机厂家</option></select>
      <select id="fs" onchange="state.status=this.value;state.page=1;render()">
        <option value="">全部核销状态</option>
        <option value="未返回">未返回</option>
        <option value="待核销">待核销</option>
        <option value="部分核销">部分核销</option>
        <option value="已核销">已核销</option>
      </select>
      <span class="dim">需返回口径：</span>
      <select id="fneed" onchange="setNeedMode(this.value)">
        <option value="req">关联发货申请单（正式）</option>
        <option value="demo">料号前缀（演示）</option>
      </select>
      <span id="prefixWrap"><span class="dim">前缀：</span><span id="prefixes"></span></span>
      <label class="ck"><input type="checkbox" id="onlyPend" onchange="state.onlyPending=this.checked;state.page=1;render()">只看待核销</label>
      <label class="ck" title="待核销 = 发货 − 已核销（默认口径 A）／ 已返回 − 已核销（口径 B）">
        <input type="checkbox" id="modeB" onchange="state.modeB=this.checked;render()">待核销按「已返回−已核销」算
      </label>
      <span class="sp"></span>
      <button class="ghost" onclick="resetAll()">重置筛选</button>
    </div>
    <div class="legend" id="needInfo"></div>
    <div class="legend">
      <span>共 <b id="gcount">0</b> 个维度</span>
      <span>· 点维度行或「查看明细」进二级核销页</span>
      <span>· 附加列「返件未关联」＝ 已返回但还没做核销关联的数量（需求 5）</span>
    </div>
    <div style="overflow:auto;max-height:620px">
      <table id="mainTable">
        <thead><tr>
          <th class="sortable" data-k="v">整机厂家</th>
          <th class="sortable" data-k="s">项目风场</th>
          <th class="num sortable" data-k="docs">单据数</th>
          <th class="num sortable" data-k="ship">发货数量</th>
          <th class="num sortable" data-k="ret">已返回数量</th>
          <th class="num sortable" data-k="clr">已核销数量</th>
          <th class="num sortable" data-k="pend">待核销数量</th>
          <th>核销状态</th>
          <th class="num sortable" data-k="unlink">返件未关联</th>
          <th></th>
        </tr></thead>
        <tbody id="mainBody"></tbody>
      </table>
    </div>
    <div class="pager">
      <button class="ghost" onclick="goto(state.page-1)">上一页</button>
      <span class="info" id="pagerInfo"></span>
      <button class="ghost" onclick="goto(state.page+1)">下一页</button>
      <span class="sp"></span>
      <button class="ghost" onclick="exportLinks()">导出我这边的核销关联（JSON）</button>
      <button class="ghost" onclick="clearLinks()">清空原型关联</button>
    </div>
  </section>

  <section class="card">
    <div class="card-b">
      <details>
        <summary>返件匹配诊断（__RETN__ 条退回登记是怎么归到维度上的）</summary>
        <div class="hint">返件由人工登记，厂家/风场写法与发货侧不总一致。下面逐条列出归位结果；「未匹配」的返件不会出现在任何维度的已返回数量里，需要补别名或人工指定。</div>
        <div style="overflow:auto;max-height:460px"><table id="diagTable">
          <thead><tr><th>退回单号</th><th>退回日期</th><th>厂家（登记）</th><th>项目风场（登记）</th><th>型号</th><th class="num">数量</th><th>归位到的维度</th><th>结果</th></tr></thead>
          <tbody id="diagBody"></tbody>
        </table></div>
      </details>
    </div>
  </section>
</div>

<div class="modal" id="modal" onclick="if(event.target===this)closeModal()"><div class="sheet" id="sheet"></div></div>

<script>
const DATA = __DATA__;
const SHIP = DATA.ship, RET = DATA.ret, GROUPS = DATA.groups, LINKS = DATA.links||[], META = DATA.meta||{};
// ship 列序
const S_NO=0,S_DATE=1,S_MAT=2,S_NAME=3,S_MODEL=4,S_QTY=5,S_SN=6,S_CUST=7,S_EXP=8,S_CT=9,S_ADDR=10,S_G=11;
// ret 列序
const R_ID=0,R_NO=1,R_DATE=2,R_MODEL=3,R_PNAME=4,R_CAT=5,R_SPEC=6,R_QTY=7,R_SRC=8,R_CAR=9,R_REG=10,R_V=11,R_S=12,R_G=13,R_ON=14,R_DK=15;
// 发货申请链路列序：[单号, 申请单号, 行号, 料号, 型号, 数量, 需返回, 厂家, 风场]
const L_NO=0,L_REQ=1,L_LN=2,L_MAT=3,L_MODEL=4,L_QTY=5,L_NEED=6,L_V=7,L_S=8;
const LS_KEY = 'proto_ledger_links_v1';
const LS_CLEAR = 'proto_ledger_clears_v1';
let links = [];   // [{r: retId, s: shipIdx, q: qty, t: '手动'|'自动', at}]
let clears = [];  // [{r: retId, q: qty, reason, at}]  —— 手工清账
// 发货单号 → 申请单链路（只收 need_return=1 的）
const linkMap = new Map();
LINKS.filter(l=>l[L_NEED]).forEach(l=>{ const k=String(l[L_NO]||'');
  if(!linkMap.has(k)) linkMap.set(k, []); linkMap.get(k).push(l); });
// 需返回口径：req＝关联发货申请单（用户定的正式口径）；demo＝料号前缀（申请单还没数据时的演示兜底）
let state = {kw:'', vendor:'', status:'', needMode: (META.link_hit||0) > 0 ? 'req' : 'demo',
             prefixes:new Set(['1','2']), onlyPending:false, modeB:false,
             page:1, pageSize:30, sort:{k:'pend', d:-1}};

function fmt(n){ n = Number(n)||0; return (Math.round(n*100)/100).toLocaleString('zh-CN'); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function loadLinks(){ try{ links = JSON.parse(localStorage.getItem(LS_KEY)||'[]'); }catch(e){ links = []; } }
function saveLinks(){ localStorage.setItem(LS_KEY, JSON.stringify(links)); }
function loadClears(){ try{ clears = JSON.parse(localStorage.getItem(LS_CLEAR)||'[]'); }catch(e){ clears = []; } }
function saveClears(){ localStorage.setItem(LS_CLEAR, JSON.stringify(clears)); }
function linkQtyOfRet(id){ return links.filter(l=>l.r===id).reduce((a,l)=>a+(Number(l.q)||0),0); }
function linkQtyOfGroup(gid){ const ids=new Set(RET.filter(r=>r[R_G]===gid).map(r=>r[R_ID]));
  return links.filter(l=>ids.has(l.r)).reduce((a,l)=>a+(Number(l.q)||0),0); }
function clearQtyOfRet(id){ return clears.filter(c=>c.r===id).reduce((a,c)=>a+(Number(c.q)||0),0); }
function clearQtyOfGroup(gid){ const ids=new Set(RET.filter(r=>r[R_G]===gid).map(r=>r[R_ID]));
  return clears.filter(c=>ids.has(c.r)).reduce((a,c)=>a+(Number(c.q)||0),0); }
function clearedQtyOfRet(id){ return linkQtyOfRet(id) + clearQtyOfRet(id); }

// 需返回判定：正式口径＝「该发货单号关联到发货申请单，且申请行填了需要返回」
function needReturn(mat, docNo){
  if(state.needMode === 'req'){
    const ls = linkMap.get(String(docNo||''));
    if(!ls) return false;
    return ls.some(l=>!l[L_MAT] || String(l[L_MAT]) === String(mat||''));
  }
  return state.prefixes.has(String(mat||'?')[0]);
}

function buildStats(){
  const m = new Map();
  GROUPS.forEach(g=>m.set(g.id,{id:g.id,k:g.k,v:g.v,s:g.s,docs:new Set(),ship:0,lines:0,allShip:0,ret:0,retRows:0,clr:0}));
  SHIP.forEach((r,i)=>{
    const st = m.get(r[S_G]); if(!st) return;
    st.allShip += r[S_QTY];
    if(needReturn(r[S_MAT], r[S_NO])){ st.ship += r[S_QTY]; st.lines++; st.docs.add(r[S_NO]); }
  });
  RET.forEach(r=>{ const st=m.get(r[R_G]); if(st){ st.ret += r[R_QTY]; st.retRows++; } });
  m.forEach(st=>{ st.clrLink = linkQtyOfGroup(st.id); st.clrClear = clearQtyOfGroup(st.id);
    st.clr = st.clrLink + st.clrClear;
    st.pend = state.modeB ? Math.max(0, st.ret - st.clr) : Math.max(0, st.ship - st.clr);
    st.unlink = Math.max(0, st.ret - st.clrLink - st.clrClear);
    st.status = st.ret<=0 ? '未返回' : (st.clr<=0 ? '待核销' : (st.clr < st.ret ? '部分核销' : '已核销'));
  });
  return m;
}

function currentRows(){
  const all = [...buildStats().values()];
  const kw = state.kw.trim().toLowerCase();
  const out = all.filter(st=>{
    if(state.vendor && st.v!==state.vendor) return false;
    if(state.status && st.status!==state.status) return false;
    if(state.onlyPending && !(st.pend>0)) return false;
    if(state.needMode==='req' && st.ship<=0) return false;   // 正式口径：以发货单为基准，没有发货单就不成行
    if(st.ship<=0 && st.ret<=0) return false;
    if(kw){
      const hay = (st.v+' '+st.s).toLowerCase();
      if(!hay.includes(kw)){
        const hit = SHIP.some(r=>r[S_G]===st.id && needReturn(r[S_MAT], r[S_NO]) &&
          ((r[S_NO]||'')+' '+(r[S_MAT]||'')+' '+(r[S_NAME]||'')+' '+(r[S_SN]||'')).toLowerCase().includes(kw));
        if(!hit) return false;
      }
    }
    return true;
  });
  const {k,d} = state.sort;
  out.sort((a,b)=>{
    let x,y;
    if(k==='v'||k==='s'){ x=a[k]||''; y=b[k]||''; return x.localeCompare(y,'zh')*d; }
    if(k==='docs'){ x=a.docs.size; y=b.docs.size; } else { x=a[k]||0; y=b[k]||0; }
    return (x-y)*d;
  });
  return out;
}

function renderKPI(rows){
  const s = {docs:0,ship:0,ret:0,clrLink:0,clrClear:0,pend:0,unlink:0,pendRows:0,unmatch:RET.filter(r=>r[R_G]===null).length};
  rows.forEach(st=>{ s.docs+=st.docs.size; s.ship+=st.ship; s.ret+=st.ret; s.clrLink+=st.clrLink; s.clrClear+=st.clrClear;
                     s.pend+=st.pend; s.unlink+=st.unlink; if(st.pend>0) s.pendRows++; });
  const clr = s.clrLink + s.clrClear;
  const card=(l,v,u,cls)=>`<div class="kpi"><div class="l">${l}</div><div class="v ${cls||''}">${v}${u?`<small>${u}</small>`:''}</div></div>`;
  document.getElementById('kpis').innerHTML =
    card('维度数',rows.length,'个') + card('发货单数',fmt(s.docs),'单') + card('发货数量',fmt(s.ship),'件') +
    card('已返回数量',fmt(s.ret),'件') + card('已核销数量',fmt(clr),'件') + card('其中手工清账',fmt(s.clrClear),'件') +
    card('待核销数量',fmt(s.pend),'件') + card('返件未核销',fmt(s.unlink),'件') +
    card('待核销维度',fmt(s.pendRows),'个') + card('未匹配返件',fmt(s.unmatch),'条');
}

function badge(st){
  const map={'未返回':'badge-gray','待核销':'badge-warn','部分核销':'badge-info','已核销':'badge-ok'};
  return `<span class="badge ${map[st]||'badge-gray'}">${st}</span>`;
}

function render(){
  const rows = currentRows();
  renderKPI(rows);
  document.getElementById('gcount').textContent = rows.length;
  const pageSize = state.pageSize, pages = Math.max(1, Math.ceil(rows.length/pageSize));
  if(state.page>pages) state.page = pages;
  const start = (state.page-1)*pageSize, slice = rows.slice(start, start+pageSize);
  const body = document.getElementById('mainBody');
  body.innerHTML = slice.length ? slice.map(st=>`<tr>
      <td>${esc(st.v)}</td>
      <td>${st.s?esc(st.s):'<span class="dim">（未标注风场）</span>'}</td>
      <td class="num">${fmt(st.docs.size)}</td>
      <td class="num"><b>${fmt(st.ship)}</b></td>
      <td class="num">${fmt(st.ret)}</td>
      <td class="num">${fmt(st.clr)}</td>
      <td class="num" ${st.pend>0?'style="color:#b3261e;font-weight:600"':''}>${fmt(st.pend)}</td>
      <td>${badge(st.status)}</td>
      <td class="num">${st.unlink>0?fmt(st.unlink):'<span class="dim">0</span>'}</td>
      <td><button class="link" onclick="openDetail(${st.id})">查看明细</button></td>
    </tr>`).join('') : '<tr><td class="empty" colspan="10">没有符合筛选条件的维度</td></tr>';
  document.getElementById('pagerInfo').textContent =
    `第 ${state.page} / ${pages} 页 · 共 ${rows.length} 个维度 · 每页 ${pageSize} 行`;
  document.querySelectorAll('#mainTable th.sortable').forEach(th=>{
    th.onclick = ()=>{ const k=th.dataset.k;
      state.sort = (state.sort.k===k) ? {k, d:-state.sort.d} : {k, d:(k==='v'||k==='s')?1:-1};
      render(); };
  });
  const dsum = RET.length;
  document.getElementById('diagBody').innerHTML = RET.map(r=>{
    const g = (r[R_G]===null||r[R_G]===undefined) ? null : (GROUPS.find(x=>x.id===r[R_G])||null);
    return `<tr><td class="mono">${esc(r[R_NO])}</td><td>${esc(r[R_DATE])}</td><td>${esc(r[R_V])}</td>
      <td>${r[R_S]?esc(r[R_S]):'<span class="dim">—</span>'}</td><td class="mono">${esc(r[R_MODEL])}</td>
      <td class="num">${fmt(r[R_QTY])}</td>
      <td>${g?esc(g.v)+'（'+(g.s||'未标注风场')+'）':'<span class="dim">—</span>'}</td>
      <td>${g?'<span class="badge badge-ok">已归位</span>':'<span class="badge badge-danger">未匹配</span>'}</td></tr>`;
  }).join('') + `<tr><td colspan="8" class="dim">共 ${dsum} 条退回登记</td></tr>`;
}

function goto(p){ const pages=Math.max(1,Math.ceil(currentRows().length/state.pageSize));
  state.page=Math.min(Math.max(1,p),pages); render(); }

function detailRows(st){
  const ships = SHIP.map((r,i)=>({r,i})).filter(x=>x.r[S_G]===st.id && needReturn(x.r[S_MAT], x.r[S_NO]));
  const rets = RET.filter(r=>r[R_G]===st.id);
  return {ships, rets};
}

// 按发货单聚合（用户口径：待核销以「发货单」为基准显示）
function byDoc(ships){
  const m = new Map();
  ships.forEach(({r,i})=>{
    const k = r[S_NO] || '（无单号）';
    if(!m.has(k)) m.set(k, {no:k, date:r[S_DATE], cust:r[S_CUST], qty:0, used:0, retIds:new Set(), items:0});
    const d = m.get(k);
    d.qty += r[S_QTY]; d.items++;
    const u = links.filter(l=>l.s===i).reduce((a,l)=>a+(Number(l.q)||0),0);
    d.used += u;
    links.filter(l=>l.s===i).forEach(l=>d.retIds.add(l.r));
  });
  return [...m.values()].sort((a,b)=>b.qty-a.qty);
}

function openDetail(gid){
  const stats = buildStats(), st = stats.get(gid); if(!st) return;
  const {ships, rets} = detailRows(st);
  const retIds = new Set(rets.map(r=>r[R_ID]));
  const handled = r => linkQtyOfRet(r[R_ID]) + clearQtyOfRet(r[R_ID]);
  const unlinked = rets.filter(r=>handled(r) < r[R_QTY]);
  const unlinkQty = unlinked.reduce((a,r)=>a+Math.max(0, r[R_QTY]-handled(r)), 0);
  const linksOfGroup = links.filter(l=>retIds.has(l.r));
  const clearsOfGroup = clears.filter(c=>retIds.has(c.r));
  const clearQty = clearsOfGroup.reduce((a,c)=>a+(Number(c.q)||0),0);
  const kpi=(l,v,u,color)=>`<div class="kpi"><div class="l">${l}</div><div class="v" ${color?`style="color:${color}"`:''}>${v}${u?`<small>${u}</small>`:''}</div></div>`;
  const docs = byDoc(ships);
  const docRows = docs.map(d=>{
    const pend = Math.max(0, d.qty - d.used);
    const stt = d.used<=0 ? '<span class="badge badge-gray">未核销</span>'
      : (pend>0 ? '<span class="badge badge-info">部分核销</span>' : '<span class="badge badge-ok">已核销</span>');
    return `<tr><td class="mono">${esc(d.no)}</td><td>${esc(d.date)}</td><td>${esc(d.cust)}</td>
      <td class="num">${d.items}</td><td class="num"><b>${fmt(d.qty)}</b></td>
      <td class="num">${fmt(d.used)}</td>
      <td class="num" ${pend>0?'style="color:#b3261e;font-weight:600"':''}>${fmt(pend)}</td>
      <td>${stt}</td><td class="num">${d.retIds.size}</td></tr>`;
  }).join('');
  const shipRows = ships.map(({r,i})=>{
    const used = links.filter(l=>l.s===i).reduce((a,l)=>a+(Number(l.q)||0),0);
    return `<tr><td class="mono">${esc(r[S_NO])}</td><td>${esc(r[S_DATE])}</td>
      <td class="mono">${esc(r[S_MAT])}</td><td>${esc(r[S_NAME])}</td><td class="mono">${esc(r[S_MODEL])}</td>
      <td class="num">${fmt(r[S_QTY])}</td>
      <td class="num">${fmt(used)}</td>
      <td class="num">${fmt(Math.max(0, r[S_QTY]-used))}</td>
      <td class="mono">${esc(r[S_SN])}</td><td class="mono">${esc(r[S_EXP])}</td>
      <td class="dim">${esc(r[S_CT])} ${esc(r[S_ADDR])}</td></tr>`;
  }).join('');
  const retRows = rets.map(r=>{
    const lq = linkQtyOfRet(r[R_ID]), cq = clearQtyOfRet(r[R_ID]), hq = lq+cq;
    const st2 = hq<=0 ? '<span class="badge badge-warn">未核销</span>'
      : (hq < r[R_QTY] ? `<span class="badge badge-info">部分核销 ${fmt(hq)}</span>` : `<span class="badge badge-ok">已核销 ${fmt(hq)}</span>`);
    return `<tr><td class="mono">${esc(r[R_NO])}</td><td>${esc(r[R_DATE])}</td><td class="mono">${esc(r[R_MODEL])}</td>
      <td>${esc(r[R_PNAME])}</td><td>${esc(r[R_CAT])}</td><td class="mono">${esc(r[R_SPEC])}</td>
      <td class="num">${fmt(r[R_QTY])}</td><td class="num">${lq>0?fmt(lq):'<span class="dim">—</span>'}</td>
      <td class="num">${cq>0?fmt(cq):'<span class="dim">—</span>'}</td>
      <td>${esc(r[R_SRC])}</td><td>${esc(r[R_CAR])}</td><td>${esc(r[R_REG])}</td><td>${st2}</td></tr>`;
  }).join('');
  const linkRows = linksOfGroup.map(l=>{
    const r = RET.find(x=>x[R_ID]===l.r), s = SHIP[l.s];
    return `<tr><td class="mono">${esc(r?r[R_NO]:'')}</td><td class="mono">${esc(r?r[R_MODEL]:'')}</td>
      <td>${r?fmt(r[R_QTY]):''}</td><td class="mono">${esc(s?s[S_NO]:'')}</td><td class="mono">${esc(s?s[S_MAT]:'')}</td>
      <td class="num">${fmt(l.q)}</td><td>${esc(l.t||'手动')}</td><td class="dim">${esc(l.at||'')}</td>
      <td><button class="link" onclick="unlinkOne(${l.r},${l.s})">取消关联</button></td></tr>`;
  }).join('');
  const clearRows = clearsOfGroup.map(c=>{
    const r = RET.find(x=>x[R_ID]===c.r);
    return `<tr><td class="mono">${esc(r?r[R_NO]:'')}</td><td class="mono">${esc(r?r[R_MODEL]:'')}</td>
      <td>${r?fmt(r[R_QTY]):''}</td><td class="dim">—</td><td class="dim">—</td>
      <td class="num">${fmt(c.q)}</td><td>清账</td><td>${esc(c.reason||'')}</td>
      <td><button class="link" onclick="unclearOne(${c.r})">取消清账</button></td></tr>`;
  }).join('');
  const unlinkRows = unlinked.map(r=>{
    const left = Math.max(0, r[R_QTY]-handled(r));
    return `<tr><td class="mono">${esc(r[R_NO])}</td><td>${esc(r[R_DATE])}</td><td class="mono">${esc(r[R_MODEL])}</td>
      <td>${esc(r[R_PNAME])}</td><td>${esc(r[R_CAT])}</td><td class="num">${fmt(left)}</td><td>${esc(r[R_CAR])}</td>
      <td><button class="btn" onclick="openPicker(${r[R_ID]},${st.id})">手动核销关联</button>
          <button class="ghost" onclick="openClear(${r[R_ID]},${st.id})">手动清账</button></td></tr>`;
  }).join('') || '<tr><td colspan="8" class="empty">该项目没有「已返回但未核销关联」的返件 🎉</td></tr>';

  document.getElementById('sheet').innerHTML = `
    <div class="sheet-h">
      <div><h3>${esc(st.v)} · ${st.s?esc(st.s):'（未标注风场）'}</h3>
        <div class="sub">二级核销页 · 维度：整机厂家 + 项目风场</div></div>
      <div><button class="ghost" onclick="closeModal()">关闭</button></div>
    </div>
    <div class="sheet-b">
      <div class="kpis">
        ${kpi('发货数量',fmt(st.ship),'件')} ${kpi('已返回数量',fmt(st.ret),'件')} ${kpi('已核销数量',fmt(st.clr),'件')}
        ${kpi('待核销数量',fmt(st.pend),'件','#b3261e')} ${kpi('返件未核销',fmt(st.unlink),'件')}
        ${kpi('其中清账',fmt(clearQty),'件')} ${kpi('核销状态',badge(st.status),'')} ${kpi('发货单数',fmt(docs.length),'单')}
      </div>
      <div class="tabs">
        <button class="on" onclick="tab(this,'t1')">发货单核销（${docs.length}）</button>
        <button onclick="tab(this,'t2')">发货行明细（${ships.length}）</button>
        <button onclick="tab(this,'t3')">返件明细（${rets.length}）</button>
        <button onclick="tab(this,'t4')">核销明细（${linksOfGroup.length + clearsOfGroup.length}）</button>
        <button onclick="tab(this,'t5')">未关联返件（${unlinked.length}）</button>
      </div>
      <div id="t1" class="tabpane">
        <div class="hint">★ 以<b>发货单</b>为基准（用户口径）：每张售后发货单一行，发货数量 / 已核销 / 待核销 / 状态；
          本维度共 ${docs.length} 张发货单、${ships.length} 行发货明细。</div>
        <div style="overflow:auto;max-height:520px"><table>
          <thead><tr><th>发货单号</th><th>发货日期</th><th>发货客户（整机厂家/风场）</th><th class="num">行数</th>
            <th class="num">发货数量</th><th class="num">已核销</th><th class="num">待核销</th><th>核销状态</th><th class="num">关联返件</th></tr></thead>
          <tbody>${docRows||'<tr><td colspan="9" class="empty">无</td></tr>'}</tbody></table></div>
      </div>
      <div id="t2" class="tabpane" style="display:none">
        <div class="hint">发货行明细：ERP 出货镜像里的「售后发货单 + 已核准」行（按当前需返回口径过滤）。</div>
        <div style="overflow:auto;max-height:520px"><table>
          <thead><tr><th>单号</th><th>日期</th><th>料号</th><th>品名</th><th>型号</th><th class="num">数量</th>
            <th class="num">已核销</th><th class="num">待核销</th><th>序列号</th><th>快递单</th><th>收货信息</th></tr></thead>
          <tbody>${shipRows||'<tr><td colspan="11" class="empty">无</td></tr>'}</tbody></table></div>
      </div>
      <div id="t3" class="tabpane" style="display:none">
        <div class="hint">返件明细：退回登记里归位到该维度的记录（人工登记，可能不完整）。</div>
        <div style="overflow:auto;max-height:520px"><table>
          <thead><tr><th>退回单号</th><th>日期</th><th>型号</th><th>品名</th><th>类别</th><th>规格</th><th class="num">数量</th>
            <th class="num">已关联</th><th class="num">已清账</th><th>来源</th><th>承运</th><th>登记人</th><th>核销状态</th></tr></thead>
          <tbody>${retRows||'<tr><td colspan="13" class="empty">该项目还没有返件登记</td></tr>'}</tbody></table></div>
      </div>
      <div id="t4" class="tabpane" style="display:none">
        <div class="hint">核销明细：返件 ↔ 发货行的关联 + 手工清账记录（原型里存本机浏览器，不写库）。</div>
        <div style="overflow:auto;max-height:520px"><table>
          <thead><tr><th>退回单号</th><th>型号</th><th class="num">返件数量</th><th>发货单号</th><th>料号</th>
            <th class="num">核销数量</th><th>方式</th><th>备注/时间</th><th></th></tr></thead>
          <tbody>${(linkRows + clearRows)||'<tr><td colspan="9" class="empty">还没有核销关联或清账</td></tr>'}</tbody></table></div>
      </div>
      <div id="t5" class="tabpane" style="display:none">
        <div class="warnbox">该项目「已返回传感器但未作为核销关联」：<b>${unlinked.length}</b> 条 / <b>${fmt(unlinkQty)}</b> 件。
          点「手动核销关联」选一条发货明细建立关联；确实不用再返回的，点「手动清账」。</div>
        <div style="overflow:auto;max-height:520px"><table>
          <thead><tr><th>退回单号</th><th>日期</th><th>型号</th><th>品名</th><th>类别</th><th class="num">未核销数量</th><th>承运</th><th></th></tr></thead>
          <tbody>${unlinkRows}</tbody></table></div>
        <div class="hint">批量试用：<button class="ghost" onclick="autoLink(${st.id})">按型号自动关联（试用）</button>
          —— 只做「返件型号出现在发货行型号里」的匹配，正式规则等你定。</div>
      </div>
    </div>`;
  document.getElementById('modal').classList.add('on');
}
function tab(btn,id){
  document.querySelectorAll('.tabpane').forEach(p=>p.style.display='none');
  document.getElementById(id).style.display='block';
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
}
function closeModal(){ document.getElementById('modal').classList.remove('on'); }

function openPicker(retId, k){
  const r = RET.find(x=>x[R_ID]===retId);
  const left = Math.max(0, r[R_QTY]-linkQtyOfRet(retId));
  const cands = SHIP.map((s,i)=>({s,i})).filter(x=>x.s[S_G]===k && needReturn(x.s[S_MAT]));
  const rows = cands.map(({s,i})=>{
    const used = links.filter(l=>l.s===i).reduce((a,l)=>a+(Number(l.q)||0),0);
    return `<tr><td><input type="radio" name="pick" value="${i}" ${used>=s[S_QTY]?'disabled':''}></td>
      <td class="mono">${esc(s[S_NO])}</td><td>${esc(s[S_DATE])}</td><td class="mono">${esc(s[S_MAT])}</td>
      <td>${esc(s[S_NAME])}</td><td class="mono">${esc(s[S_MODEL])}</td>
      <td class="num">${fmt(s[S_QTY])}</td><td class="num">${fmt(s[S_QTY]-used)}</td>
      <td class="mono">${esc(s[S_SN])}</td></tr>`;
  }).join('');
  document.getElementById('sheet').innerHTML = `
    <div class="sheet-h"><div><h3>手动核销关联 · ${esc(r[R_NO])}</h3>
      <div class="sub">返件 ${esc(r[R_MODEL])}（${esc(r[R_PNAME])}）未关联数量 ${fmt(left)}</div></div>
      <div><button class="ghost" onclick="openDetail(${k})">返回</button>
           <button class="ghost" onclick="closeModal()">关闭</button></div></div>
    <div class="sheet-b">
      <div class="hint">选一条发货明细建立关联（原型：只存本机浏览器）。数量默认取「返件剩余」，可改小。</div>
      <div style="margin:10px 0">本次关联数量：<input type="number" id="pickQty" value="${left}" min="0.01" step="0.01" max="${left}" style="width:120px;padding:5px 8px;border:1px solid #cfd8e3;border-radius:7px"></div>
      <div style="overflow:auto;max-height:520px"><table>
        <thead><tr><th></th><th>单号</th><th>日期</th><th>料号</th><th>品名</th><th>型号</th><th class="num">发货数量</th><th class="num">可关联</th><th>序列号</th></tr></thead>
        <tbody>${rows||'<tr><td colspan="9" class="empty">该项目当前口径下没有可选发货行</td></tr>'}</tbody></table></div>
      <div style="margin-top:14px"><button class="btn" onclick="doLink(${retId},${k})">确认关联</button></div>
    </div>`;
}
function doLink(retId, k){
  const sel = document.querySelector('#sheet input[name=pick]:checked');
  if(!sel){ alert('请先选一条发货明细'); return; }
  const q = Number(document.getElementById('pickQty').value)||0;
  if(q<=0){ alert('关联数量要大于 0'); return; }
  links.push({r:retId, s:Number(sel.value), q, t:'手动', at:new Date().toISOString().slice(0,16)});
  saveLinks(); openDetail(k);
}
function unlinkOne(retId, shipIdx){
  links = links.filter(l=>!(l.r===retId && l.s===shipIdx));
  saveLinks();
  const row = RET.find(x=>x[R_ID]===retId);
  const gid = row ? row[R_G] : null;
  if(gid!==null && gid!==undefined) openDetail(gid); else { closeModal(); render(); }
}
// 手动清账：返件确实不用再关联发货单时，直接把这段数量核销掉（原因必填）
function openClear(retId, k){
  const r = RET.find(x=>x[R_ID]===retId);
  const left = Math.max(0, r[R_QTY]-linkQtyOfRet(retId)-clearQtyOfRet(retId));
  document.getElementById('sheet').innerHTML = `
    <div class="sheet-h"><div><h3>手动清账 · ${esc(r[R_NO])}</h3>
      <div class="sub">返件 ${esc(r[R_MODEL])}（${esc(r[R_PNAME])}）未核销数量 ${fmt(left)}</div></div>
      <div><button class="ghost" onclick="openDetail(${k})">返回</button>
           <button class="ghost" onclick="closeModal()">关闭</button></div></div>
    <div class="sheet-b">
      <div class="hint">清账 = 该返件不再作为「需要关联的发货单」的核销对象，直接从「未关联返件」中减去。
        原型只存本机浏览器，不写库；正式版要写 <code>ledger_clear</code> 并记操作人。</div>
      <div style="margin:10px 0">清账数量：<input type="number" id="clearQty" value="${left}" min="0.01" step="0.01" max="${left}" style="width:120px;padding:5px 8px;border:1px solid #cfd8e3;border-radius:7px"></div>
      <div style="margin:10px 0">清账原因（必填）：<br>
        <textarea id="clearReason" rows="3" style="width:100%;margin-top:6px;padding:8px;border:1px solid #cfd8e3;border-radius:7px"
          placeholder="例：客户确认为误发、返件已在别处核销、样品不回收……"></textarea></div>
      <div id="clearErr" class="hint" style="color:#b3261e"></div>
      <div style="margin-top:14px"><button class="btn" onclick="doClear(${retId},${k})">确认清账</button></div>
    </div>`;
}
function doClear(retId, k){
  const q = Number(document.getElementById('clearQty').value)||0;
  const reason = (document.getElementById('clearReason').value||'').trim();
  const left = Math.max(0, (RET.find(x=>x[R_ID]===retId)||{})[R_QTY] - linkQtyOfRet(retId) - clearQtyOfRet(retId));
  if(q<=0){ document.getElementById('clearErr').textContent='清账数量要大于 0'; return; }
  if(q>left+1e-9){ document.getElementById('clearErr').textContent='清账数量不能超过未核销数量 '+fmt(left); return; }
  if(!reason){ document.getElementById('clearErr').textContent='清账原因必填'; return; }
  clears.push({r:retId, q, reason, at:new Date().toISOString().slice(0,16)});
  saveClears(); openDetail(k);
}
function unclearOne(retId){
  clears = clears.filter(c=>c.r!==retId);
  saveClears();
  const row = RET.find(x=>x[R_ID]===retId);
  const gid = row ? row[R_G] : null;
  if(gid!==null && gid!==undefined) openDetail(gid); else { closeModal(); render(); }
}
function autoLink(k){
  const rets = RET.filter(r=>r[R_G]===k);
  let n=0;
  rets.forEach(r=>{
    let left = Math.max(0, r[R_QTY]-linkQtyOfRet(r[R_ID]));
    if(left<=0) return;
    const model = String(r[R_MODEL]||'').trim();
    if(model.length<3) return;
    SHIP.forEach((s,i)=>{
      if(left<=0 || s[S_G]!==k || !needReturn(s[S_MAT], s[S_NO])) return;
      if(!String(s[S_MODEL]||'').includes(model)) return;
      const used = links.filter(l=>l.s===i).reduce((a,l)=>a+(Number(l.q)||0),0);
      const room = Math.max(0, s[S_QTY]-used);
      if(room<=0) return;
      const q = Math.min(left, room);
      links.push({r:r[R_ID], s:i, q, t:'自动', at:new Date().toISOString().slice(0,16)});
      left -= q; n++;
    });
  });
  saveLinks(); openDetail(k);
  if(n===0) alert('没有可自动关联的行（返件型号没出现在该项目的发货行型号里）');
}
function exportLinks(){
  const blob = new Blob([JSON.stringify({export_at:new Date().toISOString(), links, clears}, null, 1)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = '核销关联-原型导出.json'; a.click();
}
function clearLinks(){ if(confirm('清空原型里的所有核销关联与手工清账？（只影响本机浏览器）')){
  links=[]; clears=[]; saveLinks(); saveClears(); closeModal(); render(); } }
function renderNeedInfo(){
  const el = document.getElementById('needInfo'); if(!el) return;
  const pw = document.getElementById('prefixWrap');
  if(pw) pw.style.display = (state.needMode === 'demo') ? '' : 'none';
  const fs = document.getElementById('fneed'); if(fs) fs.value = state.needMode;
  if(state.needMode === 'req'){
    el.innerHTML = `需返回口径：<b>关联发货申请单</b>（正式）——
      已登记发货申请 <b>${fmt(META.link_docs||0)}</b> 个单号 / ${fmt(META.link_rows||0)} 行，
      其中 <b>${fmt(META.link_hit||0)}</b> 个单号能在发货明细里对上；
      申请明细 ${fmt(META.req_items||0)} 行、标记需返回 ${fmt(META.req_need||0)} 行。`
      + ((META.link_hit||0)===0 ? ' <span style="color:#b3261e"><b>当前发货申请还没有数据，正式口径下列表是空的 —— 先切「料号前缀（演示）」看界面效果。</b></span>' : '');
  } else {
    el.innerHTML = `需返回口径：<b>料号前缀（演示）</b>—— 只有 ${[...state.prefixes].sort().join('、')} 开头的料号算需要返回；
      这个口径不对，仅供在发货申请数据补齐前演示界面。`;
  }
}
function setNeedMode(v){ state.needMode = v; state.page = 1; renderNeedInfo(); render(); }
function resetAll(){
  state.kw=''; state.vendor=''; state.status=''; state.onlyPending=false; state.modeB=false; state.page=1;
  document.getElementById('kw').value=''; document.getElementById('fv').value=''; document.getElementById('fs').value='';
  document.getElementById('onlyPend').checked=false; document.getElementById('modeB').checked=false;
  buildPrefixChips(); render();
}
function buildPrefixChips(){
  const pre = {};
  SHIP.forEach(r=>{ const p=String(r[S_MAT]||'?')[0]; const o=pre[p]||(pre[p]={n:0,q:0}); o.n++; o.q+=r[S_QTY]; });
  document.getElementById('prefixes').innerHTML = Object.keys(pre).sort().map(p=>
    `<label class="ck" title="${fmt(pre[p].q)} 件 / ${fmt(pre[p].n)} 行"><input type="checkbox" ${state.prefixes.has(p)?'checked':''}
      onchange="if(this.checked)state.prefixes.add('${p}');else state.prefixes.delete('${p}');state.page=1;render()">
      ${p}xxxx <span class="dim">${fmt(pre[p].q)}件</span></label>`).join(' ');
}
function init(){
  loadLinks(); loadClears();
  const vendors = [...new Set(GROUPS.map(g=>g.v))].sort((a,b)=>a.localeCompare(b,'zh'));
  document.getElementById('fv').innerHTML = '<option value="">全部整机厂家</option>' +
    vendors.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
  buildPrefixChips(); renderNeedInfo(); render();
}
init();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
