"""
Unit tests for the layout provider of segmentation documents (rows of GROBID's feature file
mapped to the lines of <text>).

@testCovers plugins/grobid/src/pte_plugin_grobid/layout.py
"""

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from pte_plugin_grobid.config import has_layout_api

from pte_plugin_grobid.layout import (
    FeatureRow,
    align,
    line_layout_from_rows,
    parse_row,
    read_feature_rows,
    segmentation_layout,
)

DOC = """<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader><encodingDesc><appInfo>
    <application ident="GROBID" type="extractor">
      <label type="revision">abc123</label>
      <label type="flavor">default</label>
      <label type="variant-id">grobid.training.segmentation</label>
    </application>
  </appInfo></encodingDesc></teiHeader>
  <text>
    <front>A Sample Article <lb/>Jane Doe <lb/></front>
    <body>Introduction text <lb/>more text <lb/></body>
    <page>2</page> <note place="headnote">Doe, A Sample Article <lb/></note>
    <body>Second page <lb/>filtered • <lb/>end of page <lb/></body>
  </text>
</TEI>"""


def row(token: str, block: str = "BLOCKIN", page: str = "PAGEIN", size: str = "SAMEFONTSIZE", bold: str = "0",
        position: str = "5", bitmap: str = "0", repeated: str = "0", main: str = "1", extra: int = 0) -> str:
    """A feature file row as printVector() writes it (33 columns), plus `extra` appended flavor columns."""
    prefixes = [token[:n] for n in range(1, 5)]
    columns = [token, "x", token.lower(), *prefixes, block, page, "SAMEFONT", size, bold, "0", "INITCAP", "NODIGIT",
               "0", "0", "0", "0", "0", "0", "0", "0", "3", position, "no", "0", "8", bitmap, "0", repeated, "0", main]
    columns += ["0"] * extra
    return " ".join(columns)


FEATURES = "\n".join([
    row("A", block="BLOCKSTART", page="PAGESTART", size="HIGHERFONT", bold="1", position="0"),
    row("Jane"),
    row("Introduction", block="BLOCKSTART"),
    row("more", position="11"),
    row("2", block="BLOCKSTART", page="PAGESTART", position="0", repeated="1"),
    row("Second", size="LOWERFONT", bitmap="1"),
    # "filtered •" has no row (GROBID filtered the line)
    row("end", main="0", page="PAGEEND"),
]) + "\n"


class TestRows(unittest.TestCase):
    def test_parse_row(self):
        parsed = parse_row(row("A", block="BLOCKSTART", page="PAGESTART", size="HIGHERFONT", bold="1", position="0").split())
        self.assertEqual(parsed, FeatureRow("A", True, ("block", "top", "larger", "bold")))
        self.assertEqual(parse_row(row("x", position="11", bitmap="1", main="0", repeated="1").split()).hints, ("bottom", "repeated", "figure", "margin"))
        self.assertIsNone(parse_row(["just", "words"]))

    def test_appended_flavor_columns_are_ignored(self):
        self.assertEqual(parse_row(row("x", bitmap="1", extra=5).split()).hints, ("figure",))

    def test_align_skips_rows_and_lines(self):
        self.assertEqual(align(["a", "b", "c", "d"], ["a", "x", "b", "d"]), [0, 2, None, 3])
        self.assertEqual(align(["a", None, "b"], ["a", "b"]), [0, None, 1])
        # a different token without a match nearby pairs with the next row
        self.assertEqual(align(["a", "q", "c"], ["a", "z", "c"]), [0, 1, 2])

    def test_lines_without_row_inherit(self):
        rows = [FeatureRow("a", True, ("block", "top")), FeatureRow("c", True, ())]
        pages, hints = line_layout_from_rows(["a", "b", "c"], rows)
        self.assertEqual(pages, [0, 0, 1])
        self.assertEqual(hints, [["block", "top"], ["top"], []])

    def test_mismatching_feature_file(self):
        self.assertIsNone(line_layout_from_rows(["a", "b", "c"], [FeatureRow("x", True), FeatureRow("y", False)]))


@unittest.skipUnless(has_layout_api(), "needs the editor's layout API (plugin API 1.5)")
class TestProvider(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch("pte_plugin_grobid.cache.get_cache_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_zip(self, features: str, key: str = "doc1_abc123") -> None:
        target = Path(self.tmp.name) / key
        target.mkdir()
        with zipfile.ZipFile(target / "training.zip", "w") as zf:
            zf.writestr("doc1.training.segmentation", features)
            zf.writestr("doc1.training.segmentation.tei.xml", DOC)

    def test_layout_of_the_document(self):
        self.write_zip(FEATURES)
        layout = segmentation_layout(DOC, "doc1")
        self.assertIsNotNone(layout)
        self.assertEqual(layout.pages, [0, 0, 0, 0, 1, 1, 1, 1])
        self.assertEqual(layout.hints[0], ["block", "top", "larger", "bold"])
        self.assertEqual(layout.hints[4], ["block", "top", "repeated"])
        self.assertEqual(layout.hints[7], ["margin"])
        self.assertEqual(len(read_feature_rows(Path(self.tmp.name) / "doc1_abc123" / "training.zip")), 7)

    def test_layout_of_the_pdf_the_document_was_extracted_from(self):
        # text recognition replaced the PDF: the entry of the document's PDF hash is used
        doc = DOC.replace('<label type="revision">abc123</label>', f'<label type="revision">abc123</label><label type="pdf-hash">{"a" * 64}</label>')
        self.assertIsNone(segmentation_layout(doc, "doc1"))
        self.write_zip(FEATURES, f"doc1_abc123_pdf-{'a' * 16}")
        layout = segmentation_layout(doc, "doc1")
        self.assertIsNotNone(layout)
        self.assertEqual(layout.pages, [0, 0, 0, 0, 1, 1, 1, 1])

    def test_no_cache(self):
        self.assertIsNone(segmentation_layout(DOC, "doc1"))

    def test_other_variant(self):
        self.write_zip(FEATURES)
        self.assertIsNone(segmentation_layout(DOC.replace("grobid.training.segmentation", "grobid.training.header"), "doc1"))


if __name__ == "__main__":
    unittest.main()
