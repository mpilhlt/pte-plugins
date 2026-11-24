"""
Unit tests for the import of GROBID training data (#68).

Run manually:
    uv run --project "$PTE_ROOT" python -m pytest plugins/grobid/src/pte_plugin_grobid/tests/test_training_import.py -v

@testCovers plugins/grobid/src/pte_plugin_grobid/training_import.py
@testCovers plugins/grobid/src/pte_plugin_grobid/cache.py
"""

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi_app.lib.core.database import DatabaseManager
from fastapi_app.lib.models import FileCreate
from fastapi_app.lib.repository.file_repository import FileRepository
from fastapi_app.lib.storage.file_storage import FileStorage
from fastapi_app.lib.utils.doi_utils import encode_filename
from fastapi_app.lib.utils.hash_utils import generate_file_hash
from pte_plugin_grobid.cache import add_to_cache, get_cache_dir, get_cache_key
from pte_plugin_grobid.sync import parse_encoding_labels
from pte_plugin_grobid.training_import import TrainingDataImporter, find_documents

NAME = "10.1234_abc"
DOI = "10.1234/abc"
DOC_ID = encode_filename(DOI)
SEG = "grobid.training.segmentation"
REFSEG = "grobid.training.references.referenceSegmenter"

TEI = """<?xml version="1.0" encoding="utf-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0" xml:space="preserve">
<teiHeader>
  <fileDesc xml:id="{name}">
    <titleStmt><title level="a">A title</title></titleStmt>
    <publicationStmt><idno type="DOI">{doi}</idno></publicationStmt>
    <sourceDesc><bibl>CEX corpus</bibl></sourceDesc>
  </fileDesc>
</teiHeader>
<text xml:lang="en">{body}</text>
</TEI>"""


def write_dataset(root: Path, *, pdf: bytes | None = b"%PDF-1.4 abc", refseg_raw: bool = True) -> None:
    """A dataset in the layout of the CEX export: pdf/, {model}/tei/, {model}/raw/."""
    if pdf is not None:
        (root / "pdf").mkdir(parents=True)
        (root / "pdf" / f"{NAME}.pdf").write_bytes(pdf)
    for model, suffix, body in [
        ("segmentation", "training.segmentation", "<front>A title <lb/></front><body>Text <lb/></body>"),
        ("reference-segmenter", "training.references.referenceSegmenter", "<listBibl><bibl>A ref <lb/></bibl></listBibl>"),
    ]:
        (root / model / "tei").mkdir(parents=True)
        (root / model / "raw").mkdir(parents=True)
        (root / model / "tei" / f"{NAME}.{suffix}.tei.xml").write_text(TEI.format(name=NAME, doi=DOI, body=body))
        if model == "segmentation" or refseg_raw:
            (root / model / "raw" / f"{NAME}.{suffix}").write_text(f"raw {suffix}\n")
    (root / "README.txt").write_text("readme")
    (root / "mapping.tsv").write_text("document_id\tcex_id\n")


class FindDocumentsTestCase(unittest.TestCase):
    def test_files_are_grouped_by_name_and_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_dataset(root)
            (root / "unknown.xml").write_text("<x/>")
            documents, unrecognized = find_documents(root)
        self.assertEqual(list(documents), [NAME])
        doc = documents[NAME]
        self.assertIsNotNone(doc.pdf)
        self.assertEqual(sorted(doc.tei), [REFSEG, SEG])
        self.assertEqual(sorted(doc.raw), [REFSEG, SEG])
        self.assertEqual(unrecognized, ["unknown.xml"])


class AddToCacheTestCase(unittest.TestCase):
    def test_existing_files_are_kept(self):
        key = get_cache_key("cache-doc", "imported", "default", "f" * 64)
        shutil.rmtree(get_cache_dir() / key, ignore_errors=True)
        add_to_cache("cache-doc", "imported", "default", "f" * 64, {"a.training.segmentation": b"one"})
        zip_path = add_to_cache("cache-doc", "imported", "default", "f" * 64, {"a.training.segmentation": b"two", "b": b"x"})
        with zipfile.ZipFile(zip_path) as zf:
            self.assertEqual(sorted(zf.namelist()), ["a.training.segmentation", "b"])
            self.assertEqual(zf.read("a.training.segmentation"), b"one")


class TrainingDataImporterTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        db = DatabaseManager(base / "test.db")
        self.repo = FileRepository(db)
        self.storage = FileStorage(base / "files", db)
        self.dataset = base / "dataset"
        shutil.rmtree(get_cache_dir(), ignore_errors=True)

    def run_import(self, revision: str = "", flavor: str = "article/footnotes-refs"):
        importer = TrainingDataImporter(self.repo, self.storage, "cex", flavor, revision, "reviewer")
        return importer.import_directory(self.dataset)

    def teis(self):
        return sorted((f for f in self.repo.get_files_by_doc_id(DOC_ID) if f.file_type == "tei"), key=lambda f: (f.variant, f.version or 0))

    def test_import_stores_gold_files_and_caches_feature_files(self):
        write_dataset(self.dataset)
        report = self.run_import(revision="763b9495f")

        self.assertEqual(report.documents, 1)
        self.assertEqual(report.files_imported, 3)
        self.assertEqual(report.skipped, [])
        pdf = next(f for f in self.repo.get_files_by_doc_id(DOC_ID) if f.file_type == "pdf")
        self.assertEqual(pdf.doc_collections, ["cex"])
        teis = self.teis()
        self.assertEqual([(f.variant, f.is_gold_standard) for f in teis], [(REFSEG, True), (SEG, True)])

        xml = self.storage.read_file(teis[1].id, "tei").decode("utf-8")
        labels = parse_encoding_labels(xml)
        self.assertEqual(labels["revision"], "763b9495f")
        self.assertEqual(labels["flavor"], "article/footnotes-refs")
        self.assertEqual(labels["variant-id"], SEG)
        self.assertEqual(labels["pdf-hash"], pdf.id)
        self.assertIn("A title", xml)
        self.assertIn("<revisionDesc", xml)

        zip_path = get_cache_dir() / get_cache_key(DOC_ID, "763b9495f", "article/footnotes-refs", pdf.id) / "training.zip"
        with zipfile.ZipFile(zip_path) as zf:
            self.assertEqual(
                sorted(zf.namelist()),
                [f"{NAME}.training.references.referenceSegmenter", f"{NAME}.training.segmentation"],
            )

    def test_revision_defaults_to_imported(self):
        write_dataset(self.dataset)
        self.run_import()
        xml = self.storage.read_file(self.teis()[0].id, "tei").decode("utf-8")
        self.assertEqual(parse_encoding_labels(xml)["revision"], "imported")

    def test_second_import_is_skipped_as_identical_or_stored_as_version(self):
        write_dataset(self.dataset)
        self.run_import(revision="r1")
        report = self.run_import(revision="r2")
        teis = self.teis()
        self.assertEqual([(f.variant, f.is_gold_standard, f.version) for f in teis], [
            (REFSEG, True, None), (REFSEG, False, 1), (SEG, True, None), (SEG, False, 1),
        ])
        self.assertEqual(report.files_imported, 2)

    def test_document_without_pdf_is_skipped(self):
        write_dataset(self.dataset, pdf=None)
        report = self.run_import()
        self.assertEqual(report.documents, 0)
        self.assertEqual(report.skipped, [{"name": NAME, "reason": "no PDF"}])
        self.assertEqual(self.repo.get_files_by_doc_id(DOC_ID), [])

    def test_document_with_a_different_pdf_is_skipped(self):
        content = b"%PDF-1.4 other"
        self.repo.insert_file(FileCreate(id=generate_file_hash(content), filename="x.pdf", doc_id=DOC_ID, file_type="pdf", file_size=len(content)))
        write_dataset(self.dataset)
        report = self.run_import()
        self.assertEqual(report.documents, 0)
        self.assertEqual(report.skipped[0]["reason"], f"the collection has a different PDF for {DOC_ID}")
        self.assertEqual(self.teis(), [])

    def test_missing_feature_file_is_a_warning(self):
        write_dataset(self.dataset, refseg_raw=False)
        report = self.run_import()
        self.assertEqual(report.documents, 1)
        self.assertIn(f"{DOC_ID}: no feature file for {REFSEG}", report.warnings)


class ImportRouteTestCase(unittest.TestCase):
    """POST /api/plugins/grobid/import-training with a ZIP upload."""

    def setUp(self):
        import io
        from unittest import mock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from fastapi_app.lib.core.dependencies import get_db, get_file_storage, get_session_manager, get_sse_service
        from fastapi_app.plugin_api import require_reviewer_or_admin
        from pte_plugin_grobid.routes import router

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        db = DatabaseManager(base / "test.db")
        self.repo = FileRepository(db)
        storage = FileStorage(base / "files", db)
        write_dataset(base / "dataset")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for path in (base / "dataset").rglob("*"):
                if path.is_file():
                    zf.write(path, "cex23/" + str(path.relative_to(base / "dataset")))
        self.zip = buffer.getvalue()

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_reviewer_or_admin] = lambda: {"username": "reviewer", "roles": ["reviewer"]}
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_file_storage] = lambda: storage
        app.dependency_overrides[get_sse_service] = lambda: mock.Mock()
        app.dependency_overrides[get_session_manager] = lambda: mock.Mock()
        self.client = TestClient(app)
        for target, value in [
            ("pte_plugin_grobid.routes._importable_collections", [("cex", "CEX")]),
            ("fastapi_app.lib.sse.sse_utils.broadcast_to_all_sessions", 1),
        ]:
            patcher = mock.patch(target, return_value=value)
            setattr(self, target.rsplit(".", 1)[1], patcher.start())
            self.addCleanup(patcher.stop)

    def post(self, data: bytes, flavor: str = "default", collection: str = "cex"):
        return self.client.post(
            "/api/plugins/grobid/import-training",
            files={"file": ("cex23.zip", data, "application/zip")},
            data={"collection": collection, "flavor": flavor, "revision": ""},
        )

    def test_upload_imports_and_notifies_clients(self):
        response = self.post(self.zip)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["documents"], 1)
        self.assertEqual(len(self.repo.get_files_by_doc_id(DOC_ID)), 3)
        self.broadcast_to_all_sessions.assert_called_once()

    def test_no_zip_is_rejected(self):
        self.assertEqual(self.post(b"not a zip").status_code, 400)

    def test_collection_must_exist(self):
        response = self.post(self.zip, collection="new-collection")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Create it first", response.json()["detail"])
        self.assertEqual(self.repo.get_files_by_doc_id(DOC_ID), [])

    def test_view_lists_the_collections(self):
        response = self.client.get("/api/plugins/grobid/import-training/view?collection=cex")
        self.assertEqual(response.status_code, 200)
        self.assertIn('<option value="cex" selected>CEX</option>', response.text)

    def test_unknown_flavor_is_rejected(self):
        self.assertEqual(self.post(self.zip, flavor="nope").status_code, 400)


if __name__ == "__main__":
    unittest.main()
