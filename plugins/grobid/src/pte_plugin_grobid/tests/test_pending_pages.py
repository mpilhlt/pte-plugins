"""
Unit tests for the pages of a segmentation document whose text corrections the feature file does not have yet.

@testCovers plugins/grobid/src/pte_plugin_grobid/routes.py
"""

import unittest
from unittest import mock

from fastapi_app.lib.models.models import FileMetadata
from pte_plugin_grobid.routes import _pending_pages

TEI = FileMetadata(id="hash", stable_id="tei-1", filename="doc1.tei.xml", doc_id="doc1", file_type="tei", file_size=1)


def pdf_with(records: list[dict[str, object]]) -> FileMetadata:
    return FileMetadata(id="pdf", stable_id="pdf-1", filename="doc1.pdf", doc_id="doc1", file_type="pdf", file_size=1, file_metadata={"text_corrections": records})


def _reference(file_metadata: dict[str, object]) -> list[int]:
    """The core's pending_text_correction_pages() (plugin API 1.11), for cores that do not have it yet."""
    pages: set[int] = set()
    records = file_metadata.get("text_corrections")
    for record in reversed(records if isinstance(records, list) else []):
        if not isinstance(record, dict) or not isinstance(record.get("pages"), list):
            break
        pages.update(record["pages"])
    return sorted(pages)


class PendingPagesTest(unittest.TestCase):
    def setUp(self):
        # the tests run against the editor's current release too, which may lack the function
        import fastapi_app.plugin_api as plugin_api

        if not hasattr(plugin_api, "pending_text_correction_pages"):
            patcher = mock.patch.object(plugin_api, "pending_text_correction_pages", _reference, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_pages_written_page_by_page(self):
        repo = mock.Mock()
        repo.get_pdf_for_file.return_value = pdf_with([{"corrections": []}, {"pages": [4, 2]}])
        self.assertEqual(_pending_pages(repo, TEI), [2, 4])

    def test_whole_document_write_clears_them(self):
        repo = mock.Mock()
        repo.get_pdf_for_file.return_value = pdf_with([{"pages": [4]}, {"corrections": []}])
        self.assertEqual(_pending_pages(repo, TEI), [])

    def test_older_core(self):
        repo = mock.Mock()
        repo.get_pdf_for_file.return_value = pdf_with([{"pages": [4]}])
        with mock.patch.dict("sys.modules", {"fastapi_app.plugin_api": object()}):
            self.assertEqual(_pending_pages(repo, TEI), [])

    def test_no_pdf(self):
        repo = mock.Mock()
        repo.get_pdf_for_file.return_value = None
        self.assertEqual(_pending_pages(repo, TEI), [])
