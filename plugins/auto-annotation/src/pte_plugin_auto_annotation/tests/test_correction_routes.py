"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/correction_routes.py
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_app.lib.core.dependencies import (
    get_file_repository,
    get_file_storage,
    get_session_manager,
    get_sse_service,
    require_authenticated_user,
    require_session_id,
)
from fastapi_app.lib.llm import LLMModel, LLMProvider, LLMProviderRegistry
from pte_plugin_auto_annotation import annotation as annotation_module
from pte_plugin_auto_annotation import correction_routes
from pte_plugin_auto_annotation import models as models_module
from pte_plugin_auto_annotation import sources as sources_module
from pte_plugin_auto_annotation.routes import router
from pte_plugin_auto_annotation.units import document_units

from .fixtures import GOLD_UNITS, GOLD_XML, INPUT_XML, OracleClient, gold_mapping

PREFIX = "/api/plugins/auto-annotation/correct"
UNIT_PATH = ".//listBibl/bibl"


def _file(stable_id: str, gold: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=f"hash-{stable_id}", stable_id=stable_id, doc_id="doc1", file_type="tei", variant="grobid.training.references",
                           is_gold_standard=gold, version=1, created_at="2026-11-01", parent_stable_id=None)


FILES = {"v1": _file("v1"), "gold": _file("gold", gold=True)}
CONTENT = {"hash-v1": INPUT_XML, "hash-gold": GOLD_XML}


class FakeRepo:
    def get_file_by_stable_id(self, stable_id):
        return FILES.get(stable_id)


class FakeStorage:
    def read_file(self, file_hash, file_type):
        content = CONTENT.get(file_hash)
        return content.encode() if content is not None else None


class ClientProvider(LLMProvider):
    def __init__(self, id: str, client: object):
        self.id = id
        self.label = id.upper()
        self.client = client

    def list_models(self) -> list[LLMModel]:
        return [{"id": "m", "label": "M", "capabilities": set(), "status": None}]  # type: ignore[typeddict-item]

    async def chat_completion(self, model_id, system_prompt, user_prompt, temperature=0.2) -> str:
        return await self.client.complete(system_prompt, user_prompt)  # type: ignore[attr-defined]


class FakeLocks:
    """The locks table: file id -> holder."""

    def __init__(self):
        self.locks: dict[str, str] = {}

    def acquire(self, file_id, session_id, db_dir, logger):
        if self.locks.get(file_id, session_id) != session_id:
            return False
        self.locks[file_id] = session_id
        return True

    def release(self, file_id, session_id, db_dir, logger):
        if self.locks.get(file_id) != session_id:
            return {"status": "error", "action": "not_owner", "message": ""}
        del self.locks[file_id]
        return {"status": "success", "action": "released", "message": ""}


class TestCorrectionRoutes(unittest.TestCase):
    def setUp(self):
        self.locks = FakeLocks()
        self.broadcasts: list[dict] = []
        for target, name, value in (
            (models_module, "check_model_access", None), (models_module, "get_default_model", None),
            (annotation_module, "check_file_access", True), (sources_module, "is_safe_fetch_url", True),
            (sources_module, "fetch_rule_excerpt", "RULES"), (correction_routes, "review_unit_path", UNIT_PATH),
            (correction_routes, "is_segmenting_variant", False),
        ):
            patcher = mock.patch.object(target, name, return_value=value)
            self.addCleanup(patcher.stop)
            patcher.start()
        for name, fake in (("acquire_lock", self.locks.acquire), ("release_lock", self.locks.release),
                           ("broadcast_to_other_sessions", lambda **kw: self.broadcasts.append(kw["data"]))):
            patcher = mock.patch.object(correction_routes, name, side_effect=fake)
            self.addCleanup(patcher.stop)
            patcher.start()
        self.user = {"username": "ann", "roles": ["annotator"]}
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_authenticated_user] = lambda: self.user
        app.dependency_overrides[require_session_id] = lambda: "session-1"
        app.dependency_overrides[get_file_repository] = FakeRepo
        app.dependency_overrides[get_file_storage] = FakeStorage
        app.dependency_overrides[get_sse_service] = lambda: None
        app.dependency_overrides[get_session_manager] = lambda: None
        self.client = TestClient(app)
        LLMProviderRegistry.reset_instance()
        self.addCleanup(LLMProviderRegistry.reset_instance)
        LLMProviderRegistry.get_instance().register(ClientProvider("pa", OracleClient(gold_mapping())))

    def post(self, path: str, **body):
        return self.client.post(f"{PREFIX}/{path}", json=body)

    def start(self, **body):
        response = self.post("start", stable_id="v1", provider_id="pa", model_id="m", batch="units:3", **body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_full_correction(self):
        self.locks.locks["v1"] = "session-1"
        job = self.start()
        holder = f"auto-annotation:{job['job_id']}"
        self.assertEqual((job["doc_id"], job["unit_count"], job["batches"]), ("doc1", 4, [[0, 1, 2], [3]]))
        # the job took over the user's lock
        self.assertEqual(self.locks.locks, {"v1": holder})
        ids = {"job_id": job["job_id"], "stable_id": "v1"}
        corrections = []
        for units in job["batches"]:
            response = self.post("batch", **ids, provider_id="pa", model_id="m", units=units)
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertIsNone(body["error"])
            corrections += [{"index": u["index"], "xml": u["xml"]} for u in body["units"] if u["xml"] is not None]
        self.assertEqual(len(corrections), 4)
        self.assertEqual(self.post("heartbeat", **ids).json(), {"status": "locked"})
        response = self.post("finish", **ids, provider_id="pa", model_id="m", corrections=corrections, processed=4)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["changed"], 4)
        self.assertIn('<name type="model" subtype="pa" key="pa/m">pa/m</name>', response.json()["xml"])
        self.assertEqual([u.xml for u in document_units(response.json()["xml"], UNIT_PATH)], GOLD_UNITS)
        self.assertEqual(self.post("release", **ids).json(), {"status": "released"})
        self.assertEqual(self.locks.locks, {})
        self.assertEqual(self.broadcasts, [{"stable_id": "v1"}])

    def test_start_refuses_a_document_locked_by_another_session(self):
        self.locks.locks["v1"] = "session-2"
        response = self.post("start", stable_id="v1", provider_id="pa", model_id="m")
        self.assertEqual(response.status_code, 423)
        self.assertEqual(self.locks.locks, {"v1": "session-2"})

    def test_start_errors(self):
        self.assertEqual(self.post("start", stable_id="gold", provider_id="pa", model_id="m").status_code, 422)
        self.assertEqual(self.post("start", stable_id="nope", provider_id="pa", model_id="m").status_code, 404)
        self.assertEqual(self.post("start", stable_id="v1", provider_id="nope", model_id="m").status_code, 404)
        self.assertEqual(self.post("start", stable_id="v1", provider_id="pa", model_id="m", batch="units:0").status_code, 422)
        self.user = {"username": "u", "roles": ["user"]}
        self.assertEqual(self.post("start", stable_id="v1", provider_id="pa", model_id="m").status_code, 403)

    def test_a_lost_lock_stops_the_job(self):
        job = self.start()
        ids = {"job_id": job["job_id"], "stable_id": "v1"}
        self.locks.locks["v1"] = "session-2"
        self.assertEqual(self.post("batch", **ids, provider_id="pa", model_id="m", units=[0]).status_code, 409)
        self.assertEqual(self.post("heartbeat", **ids).status_code, 409)
        self.assertEqual(self.post("release", **ids).json(), {"status": "not-locked"})
        self.assertEqual(self.locks.locks, {"v1": "session-2"})

    def test_finish_checks_the_corrections(self):
        job = self.start()
        ids = {"job_id": job["job_id"], "stable_id": "v1"}
        response = self.post("finish", **ids, provider_id="pa", model_id="m", corrections=[{"index": 0, "xml": "<bibl>other</bibl>"}], processed=4)
        self.assertEqual(response.status_code, 422)
        self.assertIn("text_changed", response.text)
        self.assertEqual(self.post("batch", **ids, provider_id="pa", model_id="m", units=[7]).status_code, 422)


if __name__ == "__main__":
    unittest.main()
