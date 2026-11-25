"""
Unit tests for the garbage collection of the GROBID training-data cache: only entries that no
training document refers to are deleted.

Run manually:
    uv run --project "$PTE_ROOT" python -m pytest plugins/grobid/src/pte_plugin_grobid/tests/test_cache_gc.py -v

@testCovers plugins/grobid/src/pte_plugin_grobid/cache_gc.py
"""

import gc
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from fastapi_app.lib.core.database import DatabaseManager
from fastapi_app.lib.models.models import FileCreate
from fastapi_app.plugin_api import FileRepository, FileStorage, GcContext
from pte_plugin_grobid.cache import get_cache_key
from pte_plugin_grobid.cache_gc import collect_cache_garbage

HASH_A = "a" * 64
HASH_B = "b" * 64


def tei(revision: str, pdf_hash: str | None = None, flavor: str | None = None) -> bytes:
    labels = f'<label type="variant-id">grobid.training.segmentation</label><label type="revision">{revision}</label>'
    if flavor:
        labels += f'<label type="flavor">{flavor}</label>'
    if pdf_hash:
        labels += f'<label type="pdf-hash">{pdf_hash}</label>'
    return (
        '<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><encodingDesc><appInfo>'
        f'<application ident="GROBID" type="extractor">{labels}</application>'
        f'</appInfo></encodingDesc></teiHeader><text><p>{revision}{pdf_hash}{flavor}</p></text></TEI>'
    ).encode()


class TestCacheGarbageCollection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = DatabaseManager(self.tmp / "metadata.db")
        self.repo = FileRepository(self.db)
        self.storage = FileStorage(self.tmp / "files", self.db)
        self.cache_dir = self.tmp / "cache"
        self.cache_dir.mkdir()
        patcher = mock.patch("pte_plugin_grobid.cache_gc.get_cache_dir", return_value=self.cache_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        # get_cache_zip() builds the path from the real cache dir; only the entry names matter
        settings = mock.Mock(plugins_data_dir=self.tmp / "plugins")
        settings_patcher = mock.patch("fastapi_app.config.get_settings", return_value=settings)
        settings_patcher.start()
        self.addCleanup(settings_patcher.stop)

    def tearDown(self):
        gc.collect()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_document(self, content: bytes, doc_id: str = "doc-1", variant: str = "grobid.training.segmentation") -> str:
        file_hash, _ = self.storage.save_file(content, "tei", increment_ref=False)
        self.repo.insert_file(FileCreate(id=file_hash, filename="d.tei.xml", doc_id=doc_id, file_type="tei", file_size=len(content), variant=variant))
        return file_hash

    def add_entry(self, name: str, age_seconds: float = 7200) -> Path:
        entry = self.cache_dir / name
        entry.mkdir()
        zip_path = entry / "training.zip"
        zip_path.write_bytes(b"zip" * 10)
        old = time.time() - age_seconds
        for path in (zip_path, entry):
            os.utime(path, (old, old))
        return entry

    def run_gc(self, dry_run: bool = False):
        ctx = GcContext(deleted_before=datetime(2999, 1, 1), file_repo=self.repo, file_storage=self.storage,
                        db_dir=self.tmp, is_admin=True, dry_run=dry_run)
        return collect_cache_garbage(ctx)

    def test_only_unreferenced_entries_are_deleted(self):
        self.add_document(tei("rev-1", HASH_A))  # current extraction
        self.add_document(tei("rev-1"))  # extracted before the pdf-hash label existed
        self.add_document(tei("rev-2", HASH_B, flavor="article/light"))  # other flavor
        keep = [
            self.add_entry(get_cache_key("doc-1", "rev-1", "default", HASH_A)),
            self.add_entry(get_cache_key("doc-1", "rev-1", "default")),
            self.add_entry(get_cache_key("doc-1", "rev-2", "article/light", HASH_B)),
        ]
        drop = [
            self.add_entry(get_cache_key("doc-1", "rev-0", "default", HASH_A)),  # older revision
            self.add_entry(get_cache_key("doc-1", "rev-1", "default", HASH_B)),  # other PDF
            self.add_entry(get_cache_key("doc-gone", "rev-1", "default", HASH_A)),  # document purged
        ]

        dry = self.run_gc(dry_run=True)
        self.assertEqual(dry.items_deleted, 3)
        self.assertEqual(dry.bytes_freed, 3 * 30)
        self.assertTrue(all(entry.exists() for entry in keep + drop))

        result = self.run_gc()
        self.assertEqual(result.details, {"entries_deleted": 3, "entries_kept": 3})
        self.assertTrue(all(entry.exists() for entry in keep))
        self.assertFalse(any(entry.exists() for entry in drop))

    def test_soft_deleted_documents_keep_their_entries(self):
        file_hash = self.add_document(tei("rev-1", HASH_A))
        # keep the content (another record references it), as a restorable document would
        self.storage.save_file(tei("rev-1", HASH_A), "tei", increment_ref=True)
        self.repo.delete_file(file_hash)
        entry = self.add_entry(get_cache_key("doc-1", "rev-1", "default", HASH_A))
        self.assertEqual(self.run_gc().items_deleted, 0)
        self.assertTrue(entry.exists())

    def test_recent_entries_are_kept(self):
        entry = self.add_entry(get_cache_key("doc-new", "rev-1", "default", HASH_A), age_seconds=60)
        self.assertEqual(self.run_gc().items_deleted, 0)
        self.assertTrue(entry.exists())

    def test_other_variants_do_not_refer_to_the_cache(self):
        self.add_document(tei("rev-1", HASH_A), variant="grobid.service.fulltext")
        entry = self.add_entry(get_cache_key("doc-1", "rev-1", "default", HASH_A))
        self.assertEqual(self.run_gc().items_deleted, 1)
        self.assertFalse(entry.exists())

    def test_missing_cache_directory(self):
        shutil.rmtree(self.cache_dir)
        self.assertEqual(self.run_gc().items_deleted, 0)


if __name__ == "__main__":
    unittest.main()
