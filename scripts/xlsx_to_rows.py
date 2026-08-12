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

When the file ships a calendar-date style (built-ins 14..17, 22, 27..31,
34..36, 50..58, or a custom date format), those cells render as ISO date.
You can also force columns via --date-cols (1-based letters or 0-based indices).

Date serials whose day-of-month is <= 12 are AMBIGUOUS: a DD/MM-vs-MM/DD locale
mismatch at input time could have silently swapped day and month (e.g. an Israeli
"07/04" = 7 Apr stored by a US-locale sheet as 4 Jul). Such cells are reported in
"ambiguous_dates" as {"cell","value","swapped"} so the caller can cross-check the
true date (e.g. against an FX-rate column or a cumulative total) — this is judgment,
left to the sync skill. Cells with day > 12 are self-resolving and never flagged.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import posixpath
import re
import sys
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NSMAP = {"s": NS}
EXCEL_EPOCH = date(1899, 12, 30)  # accounts for the 1900 leap-year bug
EXCEL_1904_EPOCH = date(1904, 1, 1)
ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "inbox" / "staging"
MAX_XLSX_ENTRIES = 10_000
MAX_XLSX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_XLSX_XML_ENTRY_BYTES = 32 * 1024 * 1024
MAX_XLSX_METADATA_BYTES = 4 * 1024 * 1024
MAX_XLSX_COLUMNS = 16_384  # Excel's XFD limit; also bounds sparse-row padding.
MAX_XLSX_ROWS = 100_000
MAX_XLSX_CELLS = 1_000_000
MAX_XLSX_OUTPUT_TEXT_CHARS = 32 * 1024 * 1024
MAX_XLSX_CELL_TEXT_CHARS = 100_000
MAX_XLSX_SHARED_STRINGS = 500_000
MAX_XLSX_SHEETS = 256
MAX_XLSX_STYLES = 65_536
MAX_XLSX_XML_ELEMENTS = 3_000_000
MAX_XLSX_XML_DEPTH = 128
MAX_XLSX_AMBIGUOUS_DATES = 50_000
MAX_XLSX_TEXT_PARTS_PER_CELL = 10_000

# Built-ins that contain a calendar date. Time-only formats (18..21, 32..33,
# 45..47) are intentionally excluded rather than becoming 1899/1904 dates.
BUILTIN_DATE_NUMFMTS = (
    set(range(14, 18)) | {22} | set(range(27, 32)) | set(range(34, 37)) | set(range(50, 59))
)


def serial_to_iso(serial: float, epoch: date = EXCEL_EPOCH) -> str:
    days = int(serial)
    return (epoch + timedelta(days=days)).isoformat()


def custom_format_is_date(format_code: str) -> bool:
    """Recognize calendar tokens while ignoring quoted/escaped literals."""
    cleaned = re.sub(r'"(?:[^"]|"")*"', '', format_code)
    cleaned = re.sub(r'\\.', '', cleaned)
    cleaned = re.sub(r'\[[^]]*\]', '', cleaned)
    cleaned = cleaned.lower()
    has_day_or_year = bool(re.search(r'[dy]', cleaned))
    has_month = 'm' in cleaned
    has_time = bool(re.search(r'[hs]', cleaned))
    return has_day_or_year or (has_month and not has_time)


def validate_input_path(raw_path: str, root: Path = ROOT) -> Path:
    if root.is_symlink():
        raise ValueError("private staging root may not be a symlink")
    root = Path(os.path.abspath(root))
    inbox = root / "inbox"
    staging = inbox / "staging"
    if inbox.is_symlink() or staging.is_symlink():
        raise ValueError("private staging directories may not be symlinks")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = Path(os.path.abspath(candidate))
    if candidate.is_symlink():
        raise ValueError("XLSX input may not be a symlink")
    try:
        relative = candidate.relative_to(staging)
    except ValueError as exc:
        raise ValueError("XLSX input must be under inbox/staging") from exc
    current = staging
    for part in relative.parent.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("XLSX input path may not cross a symlink")
    if candidate.suffix.lower() != ".xlsx" or not candidate.is_file():
        raise ValueError("input must be a staged .xlsx file")
    return candidate


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
    if not re.fullmatch(r"[A-Za-z]+", letter) or len(letter) > 3:
        raise ValueError("column reference is outside Excel's supported range")
    letter = letter.upper()
    n = 0
    for c in letter:
        n = n * 26 + (ord(c) - ord("A") + 1)
    index = n - 1
    if index < 0 or index >= MAX_XLSX_COLUMNS:
        raise ValueError("column reference is outside Excel's supported range")
    return index


def parse_date_cols(spec: str) -> set[int]:
    out: set[int] = set()
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.isdigit():
            index = int(tok)
            if index >= MAX_XLSX_COLUMNS:
                raise ValueError("date column is outside Excel's supported range")
            out.add(index)
        elif re.fullmatch(r"[A-Za-z]+", tok):
            out.add(col_letter_to_index(tok))
        else:
            raise ValueError(f"invalid date column: {tok!r}")
    return out


class LimitedReader:
    """File-like reader that enforces an actual decompressed-byte limit."""

    def __init__(self, stream, limit: int):
        self.stream = stream
        self.limit = limit
        self.total = 0

    def read(self, size: int = -1):
        remaining = self.limit - self.total
        request = remaining + 1 if size < 0 or size > remaining + 1 else size
        data = self.stream.read(request)
        self.total += len(data)
        if self.total > self.limit:
            raise ValueError("XLSX XML part is too large to process safely")
        return data


def xml_events(z: zipfile.ZipFile, name: str, limit: int):
    """Yield streaming XML events while bounding declared and actual bytes."""
    info = z.getinfo(name)
    if info.is_dir() or info.file_size > limit:
        raise ValueError("XLSX XML part is too large to process safely")
    with z.open(info) as raw:
        depth = 0
        elements = 0
        for event, element in ET.iterparse(
            LimitedReader(raw, limit), events=("start", "end")
        ):
            if event == "start":
                depth += 1
                elements += 1
                if depth > MAX_XLSX_XML_DEPTH:
                    raise ValueError("XLSX XML is nested too deeply")
                if elements > MAX_XLSX_XML_ELEMENTS:
                    raise ValueError("XLSX XML contains too many elements")
            yield event, element
            if event == "end":
                depth -= 1
        if depth:
            raise ValueError("XLSX XML structure is malformed")


def _remove_completed(stack, element) -> None:
    if not stack or stack[-1] is not element:
        raise ValueError("XLSX XML structure is malformed")
    if len(stack) > 1:
        stack[-2].remove(element)
    element.clear()
    stack.pop()


def load_shared_strings(z: zipfile.ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in z.namelist():
        return []
    strings: list[str] = []
    total_text = 0
    current_parts: list[str] | None = None
    current_chars = 0
    stack = []
    elements = 0
    for event, element in xml_events(z, name, MAX_XLSX_XML_ENTRY_BYTES):
        if event == "start":
            elements += 1
            if elements > MAX_XLSX_XML_ELEMENTS:
                raise ValueError("XLSX XML contains too many elements")
            stack.append(element)
            if element.tag == f"{{{NS}}}si":
                if current_parts is not None:
                    raise ValueError("XLSX shared strings are malformed")
                current_parts = []
                current_chars = 0
            continue

        if element.tag == f"{{{NS}}}t" and current_parts is not None:
            chunk = element.text or ""
            current_chars += len(chunk)
            if current_chars > MAX_XLSX_CELL_TEXT_CHARS:
                raise ValueError("XLSX cell text is too large")
            if len(current_parts) >= MAX_XLSX_TEXT_PARTS_PER_CELL:
                raise ValueError("XLSX cell text has too many fragments")
            current_parts.append(chunk)
        elif element.tag == f"{{{NS}}}si":
            if current_parts is None:
                raise ValueError("XLSX shared strings are malformed")
            if len(strings) >= MAX_XLSX_SHARED_STRINGS:
                raise ValueError("XLSX contains too many shared strings")
            value = "".join(current_parts)
            total_text += len(value)
            if total_text > MAX_XLSX_OUTPUT_TEXT_CHARS:
                raise ValueError("XLSX shared-string text is too large")
            strings.append(value)
            current_parts = None
            current_chars = 0
        _remove_completed(stack, element)
    if current_parts is not None or stack:
        raise ValueError("XLSX shared strings are malformed")
    return strings


def load_date_styles(z: zipfile.ZipFile) -> set[int]:
    name = "xl/styles.xml"
    if name not in z.namelist():
        return set()
    custom_date_ids: set[int] = set()
    date_style_ids: set[int] = set()
    style_index = 0
    stack = []
    elements = 0
    for event, element in xml_events(z, name, MAX_XLSX_METADATA_BYTES):
        if event == "start":
            elements += 1
            if elements > MAX_XLSX_XML_ELEMENTS:
                raise ValueError("XLSX XML contains too many elements")
            stack.append(element)
            continue

        parent = stack[-2] if len(stack) > 1 else None
        if element.tag == f"{{{NS}}}numFmt":
            if len(custom_date_ids) >= MAX_XLSX_STYLES:
                raise ValueError("XLSX contains too many number formats")
            fmt = element.attrib.get("formatCode", "")
            if len(fmt) > MAX_XLSX_CELL_TEXT_CHARS:
                raise ValueError("XLSX number format is too large")
            if custom_format_is_date(fmt):
                custom_date_ids.add(int(element.attrib["numFmtId"]))
        elif (
            element.tag == f"{{{NS}}}xf"
            and parent is not None
            and parent.tag == f"{{{NS}}}cellXfs"
        ):
            if style_index >= MAX_XLSX_STYLES:
                raise ValueError("XLSX contains too many cell styles")
            number_format = int(element.attrib.get("numFmtId", "0"))
            if number_format in BUILTIN_DATE_NUMFMTS or number_format in custom_date_ids:
                date_style_ids.add(style_index)
            style_index += 1
        _remove_completed(stack, element)
    if stack:
        raise ValueError("XLSX styles are malformed")
    return date_style_ids


def load_sheet_definitions(z: zipfile.ZipFile):
    workbook_name = "xl/workbook.xml"
    relationships_name = "xl/_rels/workbook.xml.rels"
    uses_1904_epoch = False
    raw_sheets: list[tuple[str, str]] = []
    stack = []
    elements = 0
    for event, element in xml_events(z, workbook_name, MAX_XLSX_METADATA_BYTES):
        if event == "start":
            elements += 1
            if elements > MAX_XLSX_XML_ELEMENTS:
                raise ValueError("XLSX XML contains too many elements")
            stack.append(element)
            continue
        if element.tag == f"{{{NS}}}workbookPr":
            uses_1904_epoch = element.attrib.get("date1904", "0").lower() in {
                "1",
                "true",
            }
        elif element.tag == f"{{{NS}}}sheet":
            if len(raw_sheets) >= MAX_XLSX_SHEETS:
                raise ValueError("XLSX contains too many sheets")
            name = element.attrib.get("name", "")
            relationship_id = element.attrib.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                "",
            )
            if not name or len(name) > 255 or not relationship_id:
                raise ValueError("XLSX sheet definition is malformed")
            raw_sheets.append((name, relationship_id))
        _remove_completed(stack, element)
    if stack:
        raise ValueError("XLSX workbook is malformed")

    wanted_relationships = {relationship_id for _, relationship_id in raw_sheets}
    relationships: dict[str, str] = {}
    stack = []
    elements = 0
    for event, element in xml_events(z, relationships_name, MAX_XLSX_METADATA_BYTES):
        if event == "start":
            elements += 1
            if elements > MAX_XLSX_XML_ELEMENTS:
                raise ValueError("XLSX XML contains too many elements")
            stack.append(element)
            continue
        if element.tag.endswith("Relationship"):
            relationship_id = element.attrib.get("Id", "")
            target = element.attrib.get("Target", "")
            if (
                relationship_id in wanted_relationships
                and element.attrib.get("TargetMode", "Internal") != "External"
                and target
            ):
                if relationship_id in relationships:
                    raise ValueError("XLSX workbook relationship is duplicated")
                relationships[relationship_id] = target
        _remove_completed(stack, element)
    if stack:
        raise ValueError("XLSX workbook relationships are malformed")

    sheets = []
    for name, relationship_id in raw_sheets:
        target = relationships.get(relationship_id, "").replace("\\", "/")
        if not target:
            raise ValueError("XLSX sheet target is malformed")
        if target.startswith("/"):
            target = posixpath.normpath(target.lstrip("/"))
        elif target.startswith("xl/"):
            target = posixpath.normpath(target)
        else:
            target = posixpath.normpath(posixpath.join("xl", target))
        if target == "xl" or not target.startswith("xl/"):
            raise ValueError("XLSX sheet target is malformed")
        if target not in z.NameToInfo:
            raise ValueError("XLSX sheet target is missing")
        sheets.append({"name": name, "path": target})
    return sheets, (EXCEL_1904_EPOCH if uses_1904_epoch else EXCEL_EPOCH)


def load_workbook(path: str):
    """Returns (open zipfile, strings, date_style_ids, sheets). Caller must close the zip."""
    z = zipfile.ZipFile(path)
    try:
        infos = z.infolist()
        names = [info.filename for info in infos]
        if (
            len(infos) > MAX_XLSX_ENTRIES
            or sum(info.file_size for info in infos) > MAX_XLSX_UNCOMPRESSED_BYTES
        ):
            raise ValueError("XLSX archive is too large to process safely")
        if len(names) != len(set(names)):
            raise ValueError("XLSX archive contains duplicate entries")
        strings = load_shared_strings(z)
        date_style_ids = load_date_styles(z)
        sheets, epoch = load_sheet_definitions(z)
        return z, strings, date_style_ids, sheets, epoch
    except Exception:
        z.close()
        raise


def parse_sheet(z: zipfile.ZipFile, sheet_path: str, strings, date_style_ids,
                forced_date_cols: set[int], epoch: date = EXCEL_EPOCH):
    rows = []
    ambiguous: list[dict] = []
    stack = []
    current_row = None
    current_cells = None
    current_cell = None
    current_value_parts: list[str] = []
    current_inline_parts: list[str] = []
    current_cell_chars = 0
    source_cells = 0
    output_cells = 0
    output_text_chars = 0
    elements = 0

    for event, element in xml_events(z, sheet_path, MAX_XLSX_XML_ENTRY_BYTES):
        if event == "start":
            elements += 1
            if elements > MAX_XLSX_XML_ELEMENTS:
                raise ValueError("XLSX XML contains too many elements")
            stack.append(element)
            if element.tag == f"{{{NS}}}row":
                if current_row is not None:
                    raise ValueError("XLSX contains nested rows")
                current_row = element
                current_cells = []
            elif element.tag == f"{{{NS}}}c":
                if current_row is None or current_cell is not None:
                    raise ValueError("XLSX cell structure is malformed")
                current_cell = element
                current_value_parts = []
                current_inline_parts = []
                current_cell_chars = 0
            continue

        if current_cell is not None and element is not current_cell:
            if element.tag in {f"{{{NS}}}v", f"{{{NS}}}t"}:
                chunk = element.text or ""
                current_cell_chars += len(chunk)
                if current_cell_chars > MAX_XLSX_CELL_TEXT_CHARS:
                    raise ValueError("XLSX cell text is too large")
                if element.tag == f"{{{NS}}}v":
                    if len(current_value_parts) >= MAX_XLSX_TEXT_PARTS_PER_CELL:
                        raise ValueError("XLSX cell text has too many fragments")
                    current_value_parts.append(chunk)
                else:
                    if len(current_inline_parts) >= MAX_XLSX_TEXT_PARTS_PER_CELL:
                        raise ValueError("XLSX cell text has too many fragments")
                    current_inline_parts.append(chunk)

        if element.tag == f"{{{NS}}}c":
            if current_cell is not element or current_cells is None:
                raise ValueError("XLSX cell structure is malformed")
            source_cells += 1
            if source_cells > MAX_XLSX_CELLS:
                raise ValueError("XLSX contains too many cells")

            ref = element.attrib.get("r", "")
            if ref:
                match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", ref)
                if not match:
                    raise ValueError("XLSX cell reference is malformed")
                col_idx = col_letter_to_index(match.group(1))
            else:
                col_idx = len(current_cells)
            if col_idx < len(current_cells):
                raise ValueError("XLSX cell references must be strictly increasing")
            if output_cells + col_idx + 1 > MAX_XLSX_CELLS:
                raise ValueError("XLSX sparse rows expand to too many cells")
            current_cells.extend([""] * (col_idx - len(current_cells)))

            cell_type = element.attrib.get("t", "n")
            style_text = element.attrib.get("s")
            style_id = int(style_text) if style_text is not None else -1
            raw = "".join(current_value_parts)
            inline = "".join(current_inline_parts)
            value = ""
            if cell_type == "s" and raw:
                index = int(raw)
                if index < 0 or index >= len(strings):
                    raise ValueError("XLSX shared-string index is out of range")
                value = strings[index]
            elif cell_type == "inlineStr":
                value = inline
            elif cell_type == "b":
                if raw not in {"", "0", "1"}:
                    raise ValueError("XLSX boolean cell is malformed")
                value = raw == "1"
            elif cell_type in ("n", "") or cell_type is None:
                if raw and (style_id in date_style_ids or col_idx in forced_date_cols):
                    try:
                        value = serial_to_iso(float(raw), epoch)
                    except (OverflowError, ValueError):
                        value = raw
                    else:
                        swapped = date_swap_if_ambiguous(value)
                        if swapped:
                            if len(ambiguous) >= MAX_XLSX_AMBIGUOUS_DATES:
                                raise ValueError("XLSX contains too many ambiguous dates")
                            ambiguous.append(
                                {"cell": ref, "value": value, "swapped": swapped}
                            )
                else:
                    value = raw
            else:
                value = raw

            if isinstance(value, str):
                output_text_chars += len(value)
                if output_text_chars > MAX_XLSX_OUTPUT_TEXT_CHARS:
                    raise ValueError("XLSX output text is too large")
            current_cells.append(value)
            current_cell = None
            current_value_parts = []
            current_inline_parts = []
            current_cell_chars = 0
        elif element.tag == f"{{{NS}}}row":
            if current_row is not element or current_cells is None or current_cell is not None:
                raise ValueError("XLSX row structure is malformed")
            if len(rows) >= MAX_XLSX_ROWS:
                raise ValueError("XLSX contains too many rows")
            output_cells += len(current_cells)
            rows.append(current_cells)
            current_row = None
            current_cells = None

        _remove_completed(stack, element)

    if stack or current_row is not None or current_cell is not None:
        raise ValueError("XLSX sheet is malformed")
    return rows, ambiguous


def main(argv):
    parser = argparse.ArgumentParser(description="Extract rows from a staged XLSX file.")
    parser.add_argument("path")
    parser.add_argument("sheet_index", nargs="?", type=int, default=0)
    parser.add_argument("--date-cols", default="")
    args = parser.parse_args(argv[1:])
    if args.sheet_index < 0:
        parser.error("sheet_index must be non-negative")
    try:
        path = validate_input_path(args.path)
        forced_date_cols = parse_date_cols(args.date_cols)
        z, strings, date_style_ids, sheets, epoch = load_workbook(str(path))
        try:
            if args.sheet_index >= len(sheets):
                parser.error(f"sheet index {args.sheet_index} out of range (have {len(sheets)})")
            sheet = sheets[args.sheet_index]
            rows, ambiguous = parse_sheet(
                z, sheet["path"], strings, date_style_ids, forced_date_cols, epoch
            )
        finally:
            z.close()
    except (
        IndexError,
        OSError,
        OverflowError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        ET.ParseError,
        KeyError,
    ) as exc:
        parser.error(str(exc))
    json.dump(
        {"sheet": sheet["name"], "rows": rows, "ambiguous_dates": ambiguous},
        sys.stdout,
        ensure_ascii=False,
    )


if __name__ == "__main__":
    main(sys.argv)
