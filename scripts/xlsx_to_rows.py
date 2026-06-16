#!/usr/bin/env python3
"""
Minimal stdlib-only XLSX -> rows extractor.

Usage:
    python3 scripts/xlsx_to_rows.py <file.xlsx> [sheet_index] [--date-cols A,C,F]

Prints JSON: {"sheet": "<name>", "rows": [[...], ...], "ambiguous_dates": [...]}.

This is a mechanical parser. It does NOT make judgment calls about what the data
means — that's the sync skill's job. The only "smart" thing it does is recognize
Excel date-format cells and emit ISO 8601 strings instead of raw serials, because
Excel serial dates are easy to mis-read.

When the file ships Excel-formatted dates (style numFmtId in 14..22, 27..36, 45..58
or any custom format containing 'yy'/'mm'/'dd'), those cells render as ISO date.
You can also force columns via --date-cols (1-based letters or 0-based indices).

Date serials whose day-of-month is <= 12 are AMBIGUOUS: a DD/MM-vs-MM/DD locale
mismatch at input time could have silently swapped day and month (e.g. an Israeli
"07/04" = 7 Apr stored by a US-locale sheet as 4 Jul). Such cells are reported in
"ambiguous_dates" as {"cell","value","swapped"} so the caller can cross-check the
true date (e.g. against an FX-rate column or a cumulative total) — this is judgment,
left to the sync skill. Cells with day > 12 are self-resolving and never flagged.
"""

from __future__ import annotations
import json
import re
import sys
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NSMAP = {"s": NS}
EXCEL_EPOCH = date(1899, 12, 30)  # accounts for the 1900 leap-year bug

# numFmtIds that Excel treats as built-in date formats
BUILTIN_DATE_NUMFMTS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 59))


def serial_to_iso(serial: float) -> str:
    days = int(serial)
    return (EXCEL_EPOCH + timedelta(days=days)).isoformat()


def date_swap_if_ambiguous(iso: str) -> str | None:
    """If an ISO date could be a DD/MM<->MM/DD victim, return the day<->month
    swapped ISO; else None. The month is always 1..12, so ambiguity hinges solely
    on day <= 12 (then the swap is itself a valid date). day == month swaps to
    itself and is not worth flagging."""
    try:
        y, m, d = (int(x) for x in iso.split("-"))
    except (ValueError, AttributeError):
        return None
    if d <= 12 and d != m:
        return f"{y:04d}-{d:02d}-{m:02d}"
    return None


def col_letter_to_index(letter: str) -> int:
    letter = letter.upper()
    n = 0
    for c in letter:
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n - 1


def parse_date_cols(spec: str) -> set[int]:
    out: set[int] = set()
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.isdigit():
            out.add(int(tok))
        else:
            out.add(col_letter_to_index(tok))
    return out


def load_workbook(path: str):
    """Returns (open zipfile, strings, date_style_ids, sheets). Caller must close the zip."""
    z = zipfile.ZipFile(path)

    # shared strings
    strings: list[str] = []
    try:
        ss = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in ss:
            strings.append("".join(t.text or "" for t in si.iter(f"{{{NS}}}t")))
    except KeyError:
        pass

    # styles: which cellXfs entries are date-formatted
    date_style_ids: set[int] = set()
    try:
        styles = ET.fromstring(z.read("xl/styles.xml"))
        # custom numFmts
        custom_date_ids: set[int] = set()
        for nf in styles.iter(f"{{{NS}}}numFmt"):
            fmt = nf.attrib.get("formatCode", "")
            if re.search(r"[dDmMyY]", fmt) and "h" not in fmt.lower():
                custom_date_ids.add(int(nf.attrib["numFmtId"]))
        # cellXfs
        cell_xfs = styles.find(f"{{{NS}}}cellXfs")
        if cell_xfs is not None:
            for i, xf in enumerate(cell_xfs):
                nfid = int(xf.attrib.get("numFmtId", "0"))
                if nfid in BUILTIN_DATE_NUMFMTS or nfid in custom_date_ids:
                    date_style_ids.add(i)
    except KeyError:
        pass

    # workbook -> sheet names + r:id -> file mapping
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {
        r.attrib["Id"]: r.attrib["Target"] for r in rels.iter()
        if r.tag.endswith("Relationship")
    }
    sheets = []
    for s in wb.iter(f"{{{NS}}}sheet"):
        rid = s.attrib.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        )
        target = rid_to_target.get(rid, "").lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        sheets.append({"name": s.attrib["name"], "path": target})

    return z, strings, date_style_ids, sheets


def parse_sheet(z: zipfile.ZipFile, sheet_path: str, strings, date_style_ids, forced_date_cols: set[int]):
    sheet = ET.fromstring(z.read(sheet_path))
    rows = []
    ambiguous: list[dict] = []
    for row_el in sheet.iter(f"{{{NS}}}row"):
        cells = []
        for c in row_el.findall(f"{{{NS}}}c"):
            # Use the cell's 'r' attribute (e.g. "B3", "AA10") to place values in the
            # correct column. Sparse rows (skipped/empty cells) get padded with "".
            ref = c.attrib.get("r", "")
            m = re.match(r"[A-Z]+", ref)
            col_idx = col_letter_to_index(m.group(0)) if m else len(cells)
            while len(cells) < col_idx:
                cells.append("")

            t = c.attrib.get("t", "n")
            s_attr = c.attrib.get("s")
            style_id = int(s_attr) if s_attr is not None else -1
            v_el = c.find(f"{{{NS}}}v")
            is_el = c.find(f"{{{NS}}}is")

            if v_el is None and is_el is None:
                cells.append("")
                continue

            if t == "s":
                cells.append(strings[int(v_el.text)] if v_el is not None and v_el.text else "")
            elif t == "inlineStr" and is_el is not None:
                cells.append("".join(t.text or "" for t in is_el.iter(f"{{{NS}}}t")))
            elif t == "b":
                cells.append(bool(int(v_el.text)) if v_el is not None and v_el.text else False)
            elif t in ("n", "") or t is None:
                raw = v_el.text if v_el is not None else ""
                if not raw:
                    cells.append("")
                else:
                    if style_id in date_style_ids or col_idx in forced_date_cols:
                        try:
                            iso = serial_to_iso(float(raw))
                            cells.append(iso)
                            swapped = date_swap_if_ambiguous(iso)
                            if swapped:
                                ambiguous.append(
                                    {"cell": ref, "value": iso, "swapped": swapped}
                                )
                        except ValueError:
                            cells.append(raw)
                    else:
                        cells.append(raw)
            else:
                cells.append(v_el.text if v_el is not None else "")
        rows.append(cells)
    return rows, ambiguous


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    path = argv[1]
    sheet_index = 0
    forced_date_cols: set[int] = set()

    i = 2
    while i < len(argv):
        a = argv[i]
        if a.isdigit():
            sheet_index = int(a)
        elif a == "--date-cols":
            i += 1
            forced_date_cols = parse_date_cols(argv[i])
        i += 1

    z, strings, date_style_ids, sheets = load_workbook(path)
    if sheet_index >= len(sheets):
        print(f"sheet index {sheet_index} out of range (have {len(sheets)})", file=sys.stderr)
        sys.exit(2)
    sheet = sheets[sheet_index]
    rows, ambiguous = parse_sheet(z, sheet["path"], strings, date_style_ids, forced_date_cols)
    json.dump(
        {"sheet": sheet["name"], "rows": rows, "ambiguous_dates": ambiguous},
        sys.stdout,
        ensure_ascii=False,
    )
    z.close()


if __name__ == "__main__":
    main(sys.argv)
