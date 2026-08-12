"""Focused tests for XLSX date and path safety helpers."""

from __future__ import annotations

from datetime import date
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile


MODULE_PATH = Path(__file__).with_name("xlsx_to_rows.py")
SPEC = importlib.util.spec_from_file_location("xlsx_to_rows", MODULE_PATH)
assert SPEC and SPEC.loader
xlsx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(xlsx)


def memory_zip(entries: dict[str, str | bytes]) -> zipfile.ZipFile:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, contents in entries.items():
            archive.writestr(name, contents)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


def worksheet(body: str) -> str:
    return (
        f'<worksheet xmlns="{xlsx.NS}"><sheetData>{body}</sheetData></worksheet>'
    )


class XlsxHelpersTests(unittest.TestCase):
    def test_1904_epoch(self):
        self.assertEqual(xlsx.serial_to_iso(0, xlsx.EXCEL_1904_EPOCH), "1904-01-01")
        self.assertEqual(xlsx.serial_to_iso(1, xlsx.EXCEL_1904_EPOCH), "1904-01-02")

    def test_custom_format_ignores_literal_words(self):
        self.assertFalse(xlsx.custom_format_is_date('[Red]0.00 "days"'))
        self.assertTrue(xlsx.custom_format_is_date("dd/mm/yyyy"))
        self.assertFalse(xlsx.custom_format_is_date("hh:mm:ss"))
        self.assertFalse(xlsx.custom_format_is_date("[mm]:ss"))

    def test_column_reference_is_bounded_to_real_excel_columns(self):
        self.assertEqual(xlsx.col_letter_to_index("XFD"), 16_383)
        with self.assertRaisesRegex(ValueError, "supported range"):
            xlsx.col_letter_to_index("XFE")
        with self.assertRaisesRegex(ValueError, "supported range"):
            xlsx.col_letter_to_index("ZZZZZZZZ")
        with self.assertRaisesRegex(ValueError, "supported range"):
            xlsx.parse_date_cols("16384")

    def test_input_must_be_staged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.xlsx"
            outside.write_bytes(b"not relevant")
            with self.assertRaisesRegex(ValueError, "under inbox/staging"):
                xlsx.validate_input_path(str(outside), root)

    def test_rejects_symlinked_staging_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            target = outside / "private.xlsx"
            target.write_bytes(b"not relevant")
            (root / "inbox").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "directories may not be symlinks"):
                xlsx.validate_input_path(str(root / "inbox" / "private.xlsx"), root)


class XlsxStreamingTests(unittest.TestCase):
    def test_loads_shared_strings_styles_epoch_and_sheet_rows(self):
        entries = {
            "xl/sharedStrings.xml": (
                f'<sst xmlns="{xlsx.NS}"><si><r><t>Hello</t></r>'
                "<r><t> world</t></r></si></sst>"
            ),
            "xl/styles.xml": (
                f'<styleSheet xmlns="{xlsx.NS}"><numFmts count="1">'
                '<numFmt numFmtId="164" formatCode="dd/mm/yyyy"/></numFmts>'
                '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164"/>'
                "</cellXfs></styleSheet>"
            ),
            "xl/workbook.xml": (
                f'<workbook xmlns="{xlsx.NS}" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<workbookPr date1904="1"/><sheets><sheet name="Sheet 1" sheetId="1" '
                'r:id="rId1"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Target="/xl/worksheets/sheet1.xml"/>'
                "</Relationships>"
            ),
            "xl/worksheets/sheet1.xml": worksheet(
                '<row r="1"><c r="A1" t="s"><v>0</v></c>'
                '<c r="C1" s="1"><v>1</v></c>'
                '<c r="D1" t="inlineStr"><is><t>inline</t></is></c></row>'
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.xlsx"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, contents in entries.items():
                    archive.writestr(name, contents)
            archive, strings, date_styles, sheets, epoch = xlsx.load_workbook(path)
            try:
                rows, ambiguous = xlsx.parse_sheet(
                    archive, sheets[0]["path"], strings, date_styles, set(), epoch
                )
            finally:
                archive.close()

        self.assertEqual(strings, ["Hello world"])
        self.assertEqual(date_styles, {1})
        self.assertEqual(sheets, [{"name": "Sheet 1", "path": "xl/worksheets/sheet1.xml"}])
        self.assertEqual(epoch, date(1904, 1, 1))
        self.assertEqual(rows, [["Hello world", "", "1904-01-02", "inline"]])
        self.assertEqual(
            ambiguous,
            [{"cell": "C1", "value": "1904-01-02", "swapped": "1904-02-01"}],
        )

    def test_sparse_rows_cannot_expand_past_cell_limit(self):
        with memory_zip(
            {"sheet.xml": worksheet('<row><c r="C1"><v>1</v></c></row>')}
        ) as archive, mock.patch.object(xlsx, "MAX_XLSX_CELLS", 2):
            with self.assertRaisesRegex(ValueError, "sparse rows"):
                xlsx.parse_sheet(archive, "sheet.xml", [], set(), set())

    def test_row_count_is_bounded(self):
        with memory_zip(
            {"sheet.xml": worksheet('<row><c r="A1"><v>1</v></c></row><row><c r="A2"><v>2</v></c></row>')}
        ) as archive, mock.patch.object(xlsx, "MAX_XLSX_ROWS", 1):
            with self.assertRaisesRegex(ValueError, "too many rows"):
                xlsx.parse_sheet(archive, "sheet.xml", [], set(), set())

    def test_repeated_shared_string_output_is_bounded(self):
        with memory_zip(
            {"sheet.xml": worksheet(
                '<row><c r="A1" t="s"><v>0</v></c>'
                '<c r="B1" t="s"><v>0</v></c></row>'
            )}
        ) as archive, mock.patch.object(xlsx, "MAX_XLSX_OUTPUT_TEXT_CHARS", 3):
            with self.assertRaisesRegex(ValueError, "output text"):
                xlsx.parse_sheet(archive, "sheet.xml", ["ab"], set(), set())

    def test_ambiguous_date_limit_is_not_swallowed_as_a_bad_serial(self):
        with memory_zip(
            {"sheet.xml": worksheet('<row><c r="A1"><v>3</v></c></row>')}
        ) as archive, mock.patch.object(xlsx, "MAX_XLSX_AMBIGUOUS_DATES", 0):
            with self.assertRaisesRegex(ValueError, "too many ambiguous dates"):
                xlsx.parse_sheet(archive, "sheet.xml", [], set(), {0})

    def test_actual_decompressed_read_is_bounded(self):
        reader = xlsx.LimitedReader(io.BytesIO(b"abc"), 2)
        with self.assertRaisesRegex(ValueError, "too large"):
            reader.read()

    def test_xml_depth_is_bounded(self):
        with memory_zip({"deep.xml": "<a><b><c/></b></a>"}) as archive, mock.patch.object(
            xlsx, "MAX_XLSX_XML_DEPTH", 2
        ):
            with self.assertRaisesRegex(ValueError, "nested too deeply"):
                list(xlsx.xml_events(archive, "deep.xml", 100))

    def test_archive_uncompressed_total_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.xlsx"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("xl/workbook.xml", "longer than one byte")
            with mock.patch.object(xlsx, "MAX_XLSX_UNCOMPRESSED_BYTES", 1):
                with self.assertRaisesRegex(ValueError, "archive is too large"):
                    xlsx.load_workbook(path)


if __name__ == "__main__":
    unittest.main()
