"""
Unit tests for the GROBID training-data cache.

Run manually:
    uv run --project "$PTE_ROOT" python -m pytest plugins/grobid/src/pte_plugin_grobid/tests/test_cache.py -v

@testCovers plugins/grobid/src/pte_plugin_grobid/cache.py
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))


class CacheFlavorIsolationTestCase(unittest.TestCase):
    """Reproduces issue #480: the cache must not serve data cached under a
    different flavor for the same (doc_id, revision) pair, since flavors
    change the columns emitted by the GROBID feature files."""

    def setUp(self):
        self._cache_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cache_tmp.cleanup)
        cache_dir = Path(self._cache_tmp.name)

        patcher = mock.patch(
            "pte_plugin_grobid.cache.get_cache_dir",
            return_value=cache_dir,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        # Source directory whose content gets cached ("default" flavor extraction)
        self._src_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._src_tmp.cleanup)
        src_dir = Path(self._src_tmp.name)
        (src_dir / "doc-1.training.segmentation").write_text("col1 col2\n")
        self.src_dir = src_dir

    def test_different_flavor_is_a_cache_miss(self):
        from pte_plugin_grobid.cache import cache_training_data, check_cache

        cache_training_data(
            "doc-1", "rev-1", "default", str(self.src_dir),
            ["doc-1.training.segmentation"],
        )

        # Same doc_id and revision, but a different flavor: must NOT reuse
        # the "default"-flavor cache entry, since its feature file lacks the
        # extra columns the other flavor's model emits.
        result = check_cache("doc-1", "rev-1", "article/footnotes-refs")
        self.assertIsNone(result)

    def test_same_flavor_is_a_cache_hit(self):
        from pte_plugin_grobid.cache import cache_training_data, check_cache

        cache_training_data(
            "doc-1", "rev-1", "default", str(self.src_dir),
            ["doc-1.training.segmentation"],
        )

        result = check_cache("doc-1", "rev-1", "default")
        self.assertIsNotNone(result)
        self.assertIn("doc-1.training.segmentation", result["files"])


class CachePdfIsolationTestCase(CacheFlavorIsolationTestCase):
    """Text recognition replaces the PDF of a document: the cache must not serve, or overwrite,
    the training data of the earlier PDF (issue #822)."""

    def test_other_pdf_is_a_cache_miss(self):
        from pte_plugin_grobid.cache import cache_training_data, check_cache

        cache_training_data("doc-1", "rev-1", "default", str(self.src_dir), ["doc-1.training.segmentation"], pdf_hash="a" * 64)

        self.assertIsNone(check_cache("doc-1", "rev-1", "default", pdf_hash="b" * 64))
        self.assertIsNotNone(check_cache("doc-1", "rev-1", "default", pdf_hash="a" * 64))
        # the entry without hash (documents extracted before the hash was recorded) is separate
        self.assertIsNone(check_cache("doc-1", "rev-1", "default"))

    def test_cache_key(self):
        from pte_plugin_grobid.cache import get_cache_key

        self.assertEqual(get_cache_key("doc-1", "rev-1", "default"), "doc-1_rev-1")
        self.assertEqual(get_cache_key("doc-1", "rev-1", "default", "0123456789abcdef0123"), "doc-1_rev-1_pdf-0123456789abcdef")
        self.assertEqual(
            get_cache_key("doc-1", "rev-1", "article/footnotes-refs", "0123456789abcdef0123"),
            "doc-1_rev-1_article-footnotes-refs_pdf-0123456789abcdef",
        )

    def test_cache_zip_of_a_document(self):
        from pte_plugin_grobid.cache import get_cache_dir, get_cache_zip

        cache_dir = get_cache_dir()
        self.assertEqual(get_cache_zip("doc-1", {"revision": "rev-1"}), cache_dir / "doc-1_rev-1" / "training.zip")
        self.assertEqual(
            get_cache_zip("doc-1", {"revision": "rev-1", "pdf-hash": "a" * 64}),
            cache_dir / f"doc-1_rev-1_pdf-{'a' * 16}" / "training.zip",
        )
        self.assertIsNone(get_cache_zip("doc-1", {"flavor": "default"}))

    def test_delete_removes_entries_of_all_pdfs(self):
        from pte_plugin_grobid.cache import cache_training_data, check_cache, delete_cache_for_doc

        cache_training_data("doc-1", "rev-1", "default", str(self.src_dir), ["doc-1.training.segmentation"], pdf_hash="a" * 64)
        cache_training_data("doc-1", "rev-1", "default", str(self.src_dir), ["doc-1.training.segmentation"])
        self.assertTrue(delete_cache_for_doc("doc-1"))
        self.assertIsNone(check_cache("doc-1", "rev-1", "default", pdf_hash="a" * 64))
        self.assertIsNone(check_cache("doc-1", "rev-1", "default"))

    def test_pdf_content_hash_is_sha256(self):
        import hashlib

        from pte_plugin_grobid.cache import pdf_content_hash

        pdf = self.src_dir / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 test")
        self.assertEqual(pdf_content_hash(pdf), hashlib.sha256(b"%PDF-1.4 test").hexdigest())


class PackageLookupsTestCase(unittest.TestCase):
    """Which cache entries the training download uses for a gold file."""

    def lookups(self, labels):
        from pte_plugin_grobid.cache import package_lookups

        return package_lookups("rev-2", "current", labels)

    def test_document_of_the_current_pdf(self):
        self.assertEqual(self.lookups({"revision": "rev-1", "pdf-hash": "current"}), ([("rev-2", "current")], True))

    def test_document_of_a_replaced_pdf_only_uses_the_cache(self):
        self.assertEqual(
            self.lookups({"revision": "rev-1", "pdf-hash": "old"}),
            ([("rev-2", "old"), ("rev-1", "old")], False),
        )

    def test_document_without_hash_tries_the_entry_without_hash_first(self):
        self.assertEqual(self.lookups({"revision": "rev-2"}), ([("rev-2", None), ("rev-2", "current")], True))


if __name__ == "__main__":
    unittest.main()
