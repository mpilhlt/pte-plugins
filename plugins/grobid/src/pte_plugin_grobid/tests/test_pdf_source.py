"""
Unit tests for the PDF that feature files are computed from: the PDF a version is attached to
(pdf-tei-editor#944), which need not be the document's most recent PDF.

Run manually:
    uv run --project "$PTE_ROOT" python -m pytest plugins/grobid/src/pte_plugin_grobid/tests/test_pdf_source.py -v

@testCovers plugins/grobid/src/pte_plugin_grobid/pdf_source.py
@testCovers plugins/grobid/src/pte_plugin_grobid/routes.py
"""

import unittest
from pathlib import Path
from unittest import mock

from fastapi_app.lib.models.models import FileMetadata
from pte_plugin_grobid.pdf_source import get_source_pdf
from pte_plugin_grobid.routes import _package_source

CURRENT = "c" * 64
ORIGINAL = "o" * 64


def pdf(file_id: str, stable_id: str) -> FileMetadata:
    return FileMetadata(id=file_id, stable_id=stable_id, filename="doc.pdf", doc_id="doc-1", file_type="pdf", file_size=1)


def gold(pdf_stable_id: str | None = None) -> FileMetadata:
    meta = FileMetadata(id="t" * 64, stable_id="gold-1", filename="doc.tei.xml", doc_id="doc-1", file_type="tei", file_size=1)
    if pdf_stable_id:
        meta.pdf_stable_id = pdf_stable_id
    return meta


def labels_xml(pdf_hash: str | None) -> str:
    hash_label = f'<label type="pdf-hash">{pdf_hash}</label>' if pdf_hash else ""
    return (
        '<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><encodingDesc><appInfo>'
        '<application ident="GROBID" type="extractor"><label type="variant-id">grobid.training.segmentation</label>'
        f'<label type="revision">rev-1</label>{hash_label}</application></appInfo></encodingDesc></teiHeader><text/></TEI>'
    )


class TestGetSourcePdf(unittest.TestCase):
    def test_uses_get_pdf_for_file(self):
        repo = mock.Mock()
        earlier = pdf(ORIGINAL, "pdf-old")
        repo.get_pdf_for_file.return_value = earlier
        self.assertIs(get_source_pdf(repo, gold("pdf-old")), earlier)
        repo.get_pdf_for_document.assert_not_called()

    def test_falls_back_to_the_current_pdf_on_older_cores(self):
        repo = mock.Mock(spec=["get_pdf_for_document"])
        current = pdf(CURRENT, "pdf-1")
        repo.get_pdf_for_document.return_value = current
        self.assertIs(get_source_pdf(repo, gold()), current)


class TestPackageSource(unittest.TestCase):
    def setUp(self):
        self.current = pdf(CURRENT, "pdf-1")
        self.repo = mock.Mock()
        self.storage = mock.Mock()

    def source(self, gold_file: FileMetadata, recorded_hash: str | None):
        return _package_source(
            self.repo, self.storage, gold_file, labels_xml(recorded_hash), self.current, Path("/files/current.pdf"), "rev-2"
        )

    def test_version_of_the_current_pdf(self):
        self.repo.get_pdf_for_file.return_value = self.current
        lookups, may_fetch, source_hash, source_path = self.source(gold(), CURRENT)
        self.assertEqual(lookups, [("rev-2", CURRENT), ("rev-1", CURRENT)])
        self.assertTrue(may_fetch)
        self.assertEqual((source_hash, source_path), (CURRENT, Path("/files/current.pdf")))
        self.storage.get_file_path.assert_not_called()

    def test_version_attached_to_an_earlier_pdf_is_fetched_from_it(self):
        self.repo.get_pdf_for_file.return_value = pdf(ORIGINAL, "pdf-old")
        self.storage.get_file_path.return_value = Path("/files/original.pdf")
        lookups, may_fetch, source_hash, source_path = self.source(gold("pdf-old"), ORIGINAL)
        self.assertEqual(lookups, [("rev-2", ORIGINAL), ("rev-1", ORIGINAL)])
        self.assertTrue(may_fetch)
        self.assertEqual((source_hash, source_path), (ORIGINAL, Path("/files/original.pdf")))
        self.storage.get_file_path.assert_called_once_with(ORIGINAL, "pdf")

    def test_version_whose_original_pdf_was_not_kept_only_uses_the_cache(self):
        # OCR changed the PDF in place before pdf-tei-editor#924: the version's PDF has another hash
        self.repo.get_pdf_for_file.return_value = self.current
        lookups, may_fetch, _, _ = self.source(gold(), ORIGINAL)
        self.assertEqual(lookups, [("rev-2", ORIGINAL), ("rev-1", ORIGINAL)])
        self.assertFalse(may_fetch)

    def test_pdf_missing_in_storage_only_uses_the_cache(self):
        self.repo.get_pdf_for_file.return_value = pdf(ORIGINAL, "pdf-old")
        self.storage.get_file_path.return_value = None
        _, may_fetch, _, source_path = self.source(gold("pdf-old"), ORIGINAL)
        self.assertFalse(may_fetch)
        self.assertIsNone(source_path)


if __name__ == "__main__":
    unittest.main()
