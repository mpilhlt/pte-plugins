"""
Unit tests for restoring the PDF's dashes and the spaces after them in GROBID's TEI.

@testCovers plugins/grobid/src/pte_plugin_grobid/dashes.py
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pte_plugin_grobid.dashes import fold_dashes, pdf_text, restore_dashes
from pte_plugin_grobid.layout import first_token

TEI = '<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><title>A - B</title></teiHeader><text>{}</text></TEI>'
PDF = "3. Das Ziel dieses Beitrags: Gierke und seiner Nachwirkung auf den Grund gehen. — II. Was\r\nbedeutet eigentlich „Rechtsfähigkeit“ fiir Flume und den BGH?, S. 362. — III. Die \"Rechts-\r\nfähigkeit\" der Gesamthand bei Gierke — eine wissenschaftshistorische Analyse, S. 365–370,\r\n"


def tei(text: str) -> str:
    return TEI.format(text)


class TestRestoreDashes(unittest.TestCase):
    def test_dashes_and_spaces_are_restored(self):
        xml = tei('<front>3. Das Ziel dieses Beitrags: Gierke und seiner Nachwirkung auf den Grund gehen. -II. Was <lb/>bedeutet eigentlich „Rechtsfähigkeit“ fiir Flume und den BGH?, S. 362. -III. Die &quot;Rechts-<lb/>fähigkeit&quot; der Gesamthand bei Gierke -eine wissenschaftshistorische Analyse, S. 365-370, <lb/></front>')
        result, count = restore_dashes(xml, PDF)
        self.assertEqual(count, 4)
        self.assertIn("gehen. — II. Was", result)
        self.assertIn("S. 362. — III. Die", result)
        self.assertIn("Gierke — eine", result)
        self.assertIn("S. 365–370,", result)
        # a line-end hyphen keeps its place, the header is not changed
        self.assertIn("&quot;Rechts-<lb/>fähigkeit", result)
        self.assertIn("<title>A - B</title>", result)

    def test_hyphen_with_space_in_pdf_gets_its_space(self):
        result, count = restore_dashes(tei("<body>die rechts-und handlungsfähige Gesamthand <lb/></body>"), "die rechts- und handlungsfähige Gesamthand")
        self.assertEqual((result.count("rechts- und"), count), (1, 1))

    def test_ambiguous_or_missing_context_is_left_alone(self):
        xml = tei("<body>a -b <lb/></body>")
        self.assertEqual(restore_dashes(xml, "a — b a — b"), (xml, 0))
        self.assertEqual(restore_dashes(xml, "other text"), (xml, 0))

    def test_wider_context_resolves_ambiguity(self):
        repeated = "x" * 12
        xml = tei(f"<body>first {repeated} -{repeated} second <lb/></body>")
        pdf = f"other {repeated} – {repeated} text first {repeated} — {repeated} second"
        result, count = restore_dashes(xml, pdf)
        self.assertEqual(count, 1)
        self.assertIn(f"{repeated} — {repeated} second", result)

    def test_comments_and_attributes_are_not_changed(self):
        xml = tei('<!-- a-b --><body type="x-y">a-b <lb/></body>')
        result, _ = restore_dashes(xml, "a–b")
        self.assertIn("<!-- a-b -->", result)
        self.assertIn('type="x-y"', result)
        self.assertIn(">a–b <lb/>", result)

    def test_fold_dashes_and_first_token(self):
        self.assertEqual(fold_dashes("— – − ‐ -"), "- - - - -")
        self.assertEqual(first_token("  —II. Was"), "-II.")


class TestPdfText(unittest.TestCase):
    def test_text_of_all_pages(self):
        import pypdfium2 as pdfium  # type: ignore[import-untyped]
        import pypdfium2.raw as raw  # type: ignore[import-untyped]

        pdf = pdfium.PdfDocument.new()
        font = raw.FPDFText_LoadStandardFont(pdf.raw, b"Helvetica")
        for text in ("eins", "zwei"):
            page = pdf.new_page(200, 200)
            obj = raw.FPDFPageObj_NewTextObj(pdf.raw, b"Helvetica", 12.0)
            encoded = (text + "\0").encode("utf-16-le")
            raw.FPDFText_SetText(obj, (raw.FPDF_WCHAR * (len(encoded) // 2)).from_buffer_copy(encoded))
            raw.FPDFPage_InsertObject(page.raw, obj)
            raw.FPDFPage_GenerateContent(page.raw)
            page.close()
        del font
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.pdf"
            pdf.save(str(path))
            pdf.close()
            text = pdf_text(path)
        self.assertIn("eins", text)
        self.assertIn("zwei", text)


class TestExtractorHook(unittest.TestCase):
    def test_failure_leaves_the_document_unchanged(self):
        from pte_plugin_grobid.extractor import GrobidTrainingExtractor

        extractor = GrobidTrainingExtractor.__new__(GrobidTrainingExtractor)
        with mock.patch("pte_plugin_grobid.dashes.pdf_text", side_effect=OSError("broken")):
            with self.assertLogs("pte_plugin_grobid.extractor", level="WARNING"):
                self.assertEqual(extractor._restore_dashes("<TEI/>", "x.pdf"), "<TEI/>")


if __name__ == "__main__":
    unittest.main()
