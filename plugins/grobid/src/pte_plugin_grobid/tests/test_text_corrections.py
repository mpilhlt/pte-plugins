"""
Unit tests for the corrected-PDF handler of segmentation documents: the feature file of a PDF
whose text layer the editor corrected is fetched, cached under the new PDF hash and checked
against the lines of the document (pdf-tei-editor#931).

@testCovers plugins/grobid/src/pte_plugin_grobid/text_corrections.py
"""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from pte_plugin_grobid.config import has_layout_api
from pte_plugin_grobid.sync import parse_encoding_labels
from pte_plugin_grobid.tests.test_layout import row

OLD_HASH = "a" * 64
NEW_HASH = "b" * 64

DOC = f"""<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader><encodingDesc><appInfo>
    <application ident="GROBID" type="extractor">
      <label type="revision">abc123</label>
      <label type="pdf-hash">{OLD_HASH}</label>
      <label type="flavor">default</label>
      <label type="variant-id">grobid.training.segmentation</label>
    </application>
  </appInfo></encodingDesc></teiHeader>
  <text>
    <front>Alpha title <lb/>Regel12). Some text <lb/></front>
    <body>Beta one <lb/>Gamma two <lb/>Delta three <lb/></body>
  </text>
</TEI>"""


def features(*tokens: str) -> str:
    return "\n".join(row(t, page="PAGESTART" if i == 0 else "PAGEIN") for i, t in enumerate(tokens)) + "\n"


def has_corrected_pdf_api() -> bool:
    """Whether the editor has the corrected-PDF handler API (plugin API 1.8)."""
    try:
        from fastapi_app.plugin_api import CorrectedPdfMismatch  # noqa: F401
    except ImportError:
        return False
    return True


# the feature file of the uncorrected PDF: line 1 starts with an OCR error
OLD_FEATURES = features("Alpha", "Rege112).", "Beta", "Gamma", "Delta")


@unittest.skipUnless(has_layout_api(), "needs the editor's layout API (plugin API 1.5)")
class TextCorrectionsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "cache"
        self.cache.mkdir()
        patcher = mock.patch("pte_plugin_grobid.cache.get_cache_dir", return_value=self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_zip(self, content: str, key: str = f"doc1_abc123_pdf-{OLD_HASH[:16]}") -> None:
        target = self.cache / key
        target.mkdir()
        with zipfile.ZipFile(target / "training.zip", "w") as zf:
            zf.writestr("doc1.training.segmentation", content)


class TestUnmatchedLines(TextCorrectionsTestBase):
    def test_lines_whose_first_token_has_no_row(self):
        from pte_plugin_grobid.text_corrections import unmatched_lines

        self.write_zip(OLD_FEATURES)
        self.assertEqual(unmatched_lines(DOC, "doc1", parse_encoding_labels(DOC)), {1})

    def test_no_cache_entry(self):
        from pte_plugin_grobid.text_corrections import unmatched_lines

        self.assertIsNone(unmatched_lines(DOC, "doc1", parse_encoding_labels(DOC)))


@unittest.skipUnless(has_corrected_pdf_api(), "needs the editor's corrected-PDF handler API (plugin API 1.8)")
class TestCorrectedPdfHandler(TextCorrectionsTestBase):
    def setUp(self):
        super().setUp()
        self.fetched: list[tuple[str, str, str]] = []
        self.new_features: str | None = features("Alpha", "Regel12).", "Beta", "Gamma", "Delta")
        for target, kwargs in [
            ("pte_plugin_grobid.text_corrections.get_grobid_server_url", {"return_value": "http://grobid.example"}),
            ("pte_plugin_grobid.text_corrections.check_grobid_revision", {"return_value": ("0.8.1", "def456")}),
            ("pte_plugin_grobid.handlers.training.TrainingHandler._fetch_training_package", {"side_effect": self.fake_fetch, "autospec": True}),
        ]:
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_fetch(self, _handler, pdf_path: str, url: str, flavor: str) -> tuple[str, list[str]]:
        self.fetched.append((pdf_path, url, flavor))
        temp_dir = tempfile.mkdtemp(dir=self.tmp.name)
        # without features, the package has no segmentation feature file
        name = "doc1.training.segmentation" if self.new_features is not None else "doc1.training.header"
        with open(os.path.join(temp_dir, name), "w") as f:
            f.write(self.new_features or "")
        return temp_dir, [name]

    def handle(self, corrected=frozenset({1}), xml: str = DOC) -> str:
        from pte_plugin_grobid.text_corrections import corrected_pdf_handler

        return corrected_pdf_handler(xml, "doc1", Path("/data/new.pdf"), NEW_HASH, corrected)

    def test_caches_the_feature_file_and_updates_the_labels(self):
        self.write_zip(OLD_FEATURES)
        xml = self.handle()
        labels = parse_encoding_labels(xml)
        self.assertEqual(labels["pdf-hash"], NEW_HASH)
        self.assertEqual(labels["revision"], "def456")
        self.assertEqual(self.fetched, [("/data/new.pdf", "http://grobid.example", "default")])
        self.assertTrue((self.cache / f"doc1_def456_pdf-{NEW_HASH[:16]}" / "training.zip").exists())
        # the temporary directory of the fetch is removed
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["cache"])

    def test_a_line_that_matched_before_no_longer_matches(self):
        from fastapi_app.plugin_api import CorrectedPdfMismatch

        self.write_zip(OLD_FEATURES)
        self.new_features = features("Alpha", "Regel12).", "Beta", "Xamma", "Delta")
        with self.assertRaises(CorrectedPdfMismatch) as cm:
            self.handle()
        self.assertEqual(cm.exception.lines, [3])

    def test_a_corrected_line_must_match(self):
        from fastapi_app.plugin_api import CorrectedPdfMismatch

        self.write_zip(OLD_FEATURES)
        self.new_features = OLD_FEATURES
        with self.assertRaises(CorrectedPdfMismatch) as cm:
            self.handle()
        self.assertEqual(cm.exception.lines, [1])

    def test_lines_that_did_not_match_before_are_tolerated(self):
        self.write_zip(OLD_FEATURES)
        self.new_features = OLD_FEATURES
        xml = self.handle(corrected=frozenset({3}))
        self.assertEqual(parse_encoding_labels(xml)["pdf-hash"], NEW_HASH)

    def test_without_old_cache_entry_the_share_of_matched_lines_is_checked(self):
        from fastapi_app.plugin_api import CorrectedPdfMismatch

        self.new_features = features("Alpha", "Regel12).", "Beta", "Xamma", "Delta")
        self.assertEqual(parse_encoding_labels(self.handle())["pdf-hash"], NEW_HASH)
        self.new_features = features("V", "W", "X", "Y", "Z")
        with self.assertRaises(CorrectedPdfMismatch) as cm:
            self.handle()
        self.assertEqual(cm.exception.lines, [0, 1, 2, 3, 4])

    def test_adds_the_pdf_hash_label(self):
        xml = DOC.replace(f'\n      <label type="pdf-hash">{OLD_HASH}</label>', "")
        self.write_zip(OLD_FEATURES, "doc1_abc123")
        self.assertEqual(parse_encoding_labels(self.handle(xml=xml))["pdf-hash"], NEW_HASH)

    def test_grobid_unreachable(self):
        with mock.patch("pte_plugin_grobid.text_corrections.check_grobid_revision", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                self.handle()
        with mock.patch("pte_plugin_grobid.text_corrections.get_grobid_server_url", return_value=None):
            with self.assertRaises(RuntimeError):
                self.handle()
        self.assertEqual(self.fetched, [])

    def test_no_feature_file_in_the_package(self):
        self.new_features = None
        with self.assertRaises(RuntimeError):
            self.handle()


class TestRegistration(unittest.TestCase):
    def test_registered_on_editors_with_the_api(self):
        from pte_plugin_grobid.plugin import register_corrected_pdf_handler_if_available

        if not has_corrected_pdf_api():
            self.assertFalse(register_corrected_pdf_handler_if_available("grobid"))
            return
        from fastapi_app.plugin_api import get_corrected_pdf_handler, unregister_corrected_pdf_handler
        from pte_plugin_grobid.plugin import register_corrected_pdf_handler_if_available
        from pte_plugin_grobid.text_corrections import corrected_pdf_handler

        self.addCleanup(unregister_corrected_pdf_handler, "grobid.training.segmentation")
        self.assertTrue(register_corrected_pdf_handler_if_available("grobid"))
        self.assertIs(get_corrected_pdf_handler("grobid.training.segmentation"), corrected_pdf_handler)


if __name__ == "__main__":
    unittest.main()
