"""Assemble data/_kdocs_returns_raw.json from per-page compact JSONs.

Reads every page-<start>-<end>.json under data/_kdocs/pages/, validates that
they form a contiguous block 0..N, takes row 0 as the header, and writes:

    {"header": [26 str], "rows": [{"row": r, "cells": [26 str]}, ...]}

where `row` is the 0-based sheet row (header excluded), `cells` is always 26
long in column order 0..25, empty cells are "".

Date columns 8 (退回时间) and 13 (检测时间) are normalized to YYYY-MM-DD using
the SAME logic as tools/import_kdocs_returns.py::clean_date.  All other columns
are kept verbatim (including "/", "无法确认" placeholders) -- no extra cleaning.

Read-only w.r.t. the source document; this only reads local page files.
"""
import json
import glob
import os
import datetime as _dt
import re
import sys

PAGES_DIR = r"E:/workbuddy/2026-09-18-10-24-48/data/_kdocs/pages"
OUT = r"E:/workbuddy/2026-09-18-10-24-48/data/_kdocs_returns_raw.json"

DATE_COLS = {8, 13}
DATE_RE = re.compile(r"^(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D*$")
BLANKS = {"/", "-", "—", "无", "N/A", "n/a"}


def clean(v):
    s = str(v or "").strip()
    return "" if s in BLANKS else s


def clean_date(v):
    s = clean(v)
    if not s:
        return ""
    m = DATE_RE.match(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return "%04d-%02d-%02d" % (y, mo, d)
    if s.isdigit() and len(s) == 5:  # Excel serial (1900 system, base 1899-12-30)
        base = _dt.date(1899, 12, 30)
        return (base + _dt.timedelta(days=int(s))).isoformat()
    return s


def main():
    files = sorted(glob.glob(os.path.join(PAGES_DIR, "page-*.json")))
    if not files:
        print("ERROR: no page files found"); sys.exit(1)

    # load + bookkeeping
    pages = {}
    for f in files:
        p = json.load(open(f, encoding="utf-8"))
        pages[p["start"]] = p

    starts = sorted(pages)
    # contiguity check
    expected = starts[0]
    maxrow = -1
    for s in starts:
        if s != expected:
            print(f"ERROR: gap between row {maxrow} and {s}")
            sys.exit(1)
        e = pages[s]["end"]
        expected = e + 1
        maxrow = e
    print(f"page coverage: rows {starts[0]}..{maxrow} across {len(pages)} pages")

    # build full ordered list of sheet rows (0..maxrow)
    all_rows = []
    for s in starts:
        all_rows.extend(pages[s]["rows"])
    # sanity: length
    assert len(all_rows) == maxrow + 1, f"len {len(all_rows)} != {maxrow+1}"

    header = [str(c) for c in all_rows[0]]
    assert len(header) == 26, f"header len {len(header)} != 26"

    out_rows = []
    for abs_r in range(1, maxrow + 1):
        cells = all_rows[abs_r]
        cells = [str(c) for c in cells]
        if len(cells) != 26:
            # pad / truncate defensively
            if len(cells) < 26:
                cells = cells + [""] * (26 - len(cells))
            else:
                cells = cells[:26]
        for col in DATE_COLS:
            cells[col] = clean_date(cells[col])
        out_rows.append({"row": abs_r, "cells": cells})

    payload = {"header": header, "rows": out_rows}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    print(f"wrote {OUT}: header={len(header)} cols, data rows={len(out_rows)}")


if __name__ == "__main__":
    main()
