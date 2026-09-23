"""Extract a page of cells from a saved kdocs read_file tool-result file.

The kdocs MCP `read_file` returns a huge structured JSON. The agent harness
auto-saves oversized tool results to a .txt file. This script parses that file,
pulls the `cellText` of every cell, and writes a compact page file:

    {"start": S, "end": E, "rows": [[26 strings], ...]}   # rows[i] == abs row S+i

Empty/missing cells become "".  For date columns (8, 13) we keep the raw
display text (e.g. "2026年1月5日") and let the assembler normalize later.
"""
import json
import sys
import os

DATE_COLS = {8, 13}


def load_result(path):
    text = open(path, encoding="utf-8", errors="replace").read()
    # find first '{' in case of any wrapper
    i = text.find("{")
    return json.loads(text[i:])


def extract(path, start, end, out_path):
    data = load_result(path)
    rd = data["data"]["content"]["range_data"]["detail"]["rangeData"]
    # group cells by (row, col)
    grid = {}
    for c in rd:
        r = c["originRow"]
        col = c["originCol"]
        txt = c.get("cellText")
        if txt is None:
            txt = c.get("originalCellValue", "")
        if txt is None:
            txt = ""
        grid[(r, col)] = str(txt)
    rows = []
    for r in range(start, end + 1):
        cells = [grid.get((r, col), "") for col in range(26)]
        rows.append(cells)
    payload = {"start": start, "end": end, "rows": rows}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    # diagnostics
    nonempty = sum(1 for r in rows if any(r))
    print(f"  {out_path}: rows {start}-{end} ({len(rows)} rows), "
          f"nonempty rows={nonempty}, sample row1={rows[1][:3] if len(rows) > 1 else None}")
    return rows


if __name__ == "__main__":
    _, path, start, end, out_path = sys.argv
    extract(path, int(start), int(end), out_path)
