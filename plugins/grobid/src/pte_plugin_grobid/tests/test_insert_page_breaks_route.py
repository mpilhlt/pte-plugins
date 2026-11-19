"""
Unit tests for the insert-page-breaks route (TEI Wizard enhancement "Insert page breaks").

@testCovers plugins/grobid/src/pte_plugin_grobid/routes.py
"""

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_app.lib.core.dependencies import get_auth_manager, get_db, get_session_manager
from fastapi_app.lib.models.models import FileMetadata
from pte_plugin_grobid.routes import router
from pte_plugin_grobid.tests.test_layout import DOC, FEATURES


class InsertPageBreaksRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        session_manager = mock.Mock()
        session_manager.is_session_valid.return_value = True
        auth_manager = mock.Mock()
        auth_manager.get_user_by_session_id.return_value = {"username": "annotator", "roles": ["annotator"]}
        self.app.dependency_overrides[get_session_manager] = lambda: session_manager
        self.app.dependency_overrides[get_auth_manager] = lambda: auth_manager
        self.app.dependency_overrides[get_db] = lambda: mock.Mock()
        self.client = TestClient(self.app)

        self.file_repo = mock.Mock()
        self.file_repo.get_file_by_stable_id.return_value = FileMetadata(
            id="hash", stable_id="tei-1", filename="doc1.tei.xml", doc_id="doc1", file_type="tei", file_size=1
        )
        for target, value in (
            ("fastapi_app.config.get_settings", mock.Mock(session_timeout=3600)),
            ("fastapi_app.lib.repository.file_repository.FileRepository", self.file_repo),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch("pte_plugin_grobid.cache.get_cache_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, xml: str = DOC):
        return self.client.post(
            "/api/plugins/grobid/insert-page-breaks", json={"stable_id": "tei-1", "xml": xml}, headers={"X-Session-ID": "abc"}
        )

    def test_requires_authentication(self):
        response = self.client.post("/api/plugins/grobid/insert-page-breaks", json={"stable_id": "tei-1", "xml": DOC})
        self.assertEqual(response.status_code, 401)

    def test_inserts_page_breaks(self):
        (Path(self.tmp.name) / "doc1_abc123").mkdir()
        with zipfile.ZipFile(Path(self.tmp.name) / "doc1_abc123" / "training.zip", "w") as zf:
            zf.writestr("doc1.training.segmentation", FEATURES)
        response = self.post()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["status"], body["inserted"]), ("ok", 2))
        self.assertIn('<pb n="2"/>\n    <page>2</page>', body["xml"])

    def test_without_feature_file(self):
        self.assertEqual(self.post().json()["status"], "no_feature_file")

    def test_other_variant(self):
        self.assertEqual(self.post(DOC.replace("grobid.training.segmentation", "grobid.training.header")).json()["status"], "not_segmentation")

    def test_unknown_file(self):
        self.file_repo.get_file_by_stable_id.return_value = None
        self.assertEqual(self.post().status_code, 404)
