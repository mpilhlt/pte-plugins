"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/annotation.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/routes.py
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_app.lib.core.dependencies import get_file_repository, get_file_storage, require_reviewer_or_admin
from fastapi_app.lib.llm import LLMModel, LLMProvider, LLMProviderRegistry
from pte_plugin_auto_annotation import annotation as annotation_module
from pte_plugin_auto_annotation import models as models_module
from pte_plugin_auto_annotation import sources as sources_module
from pte_plugin_auto_annotation.routes import router
from pte_plugin_auto_annotation.units import document_units

from .fixtures import GOLD_UNITS, GOLD_XML, INPUT_XML, ArbiterOracle, OracleClient, gold_mapping

VARIANT = "grobid.training.references"
ANNOTATED_XML = INPUT_XML.replace("</teiHeader>", '<revisionDesc><change who="#auto-annotation"/></revisionDesc></teiHeader>')


def _file(stable_id: str, doc_id: str, version: int | None, *, gold: bool = False, variant: str = VARIANT) -> SimpleNamespace:
    return SimpleNamespace(id=f"hash-{stable_id}", stable_id=stable_id, doc_id=doc_id, file_type="tei", variant=variant,
                           is_gold_standard=gold, version=version, created_at="2026-11-01", doc_collections=["c1"])


FILES = [
    _file("d1v1", "doc1", 1), _file("d1v2", "doc1", 2),  # doc1: already annotated in v2
    _file("d2v1", "doc2", 1),  # doc2: ready
    _file("d3v1", "doc3", 1), _file("d3g", "doc3", None, gold=True),  # doc3: has a gold version
    _file("d4s", "doc4", 1, variant="grobid.training.segmentation"),  # other variant only
]
CONTENT = {"hash-d1v1": INPUT_XML, "hash-d1v2": ANNOTATED_XML, "hash-d2v1": INPUT_XML, "hash-d3v1": INPUT_XML, "hash-d3g": GOLD_XML,
           "hash-d4s": INPUT_XML}


class FakeRepo:
    def get_file_by_stable_id(self, stable_id):
        return next((f for f in FILES if f.stable_id == stable_id), None)

    def get_files_by_doc_id(self, doc_id):
        return [f for f in FILES if f.doc_id == doc_id]

    def get_files_by_collection(self, collection_id):
        return [f for f in FILES if collection_id in f.doc_collections]


class FakeStorage:
    def read_file(self, file_hash, file_type):
        content = CONTENT.get(file_hash)
        return content.encode() if content is not None else None


class ClientProvider(LLMProvider):
    """A registered provider whose answers come from a mock ChatClient."""

    def __init__(self, id: str, client: object):
        self.id = id
        self.label = id.upper()
        self.client = client

    def list_models(self) -> list[LLMModel]:
        return [{"id": "m", "label": "M", "capabilities": set(), "status": None}]  # type: ignore[typeddict-item]

    async def chat_completion(self, model_id, system_prompt, user_prompt, temperature=0.2) -> str:
        return await self.client.complete(system_prompt, user_prompt)  # type: ignore[attr-defined]


class TestAnnotationRoutes(unittest.TestCase):
    def setUp(self):
        for target, name, value in (
            (models_module, "check_model_access", None), (models_module, "get_default_model", None),
            (sources_module, "check_file_access", True), (annotation_module, "check_file_access", True),
            (sources_module, "is_safe_fetch_url", True), (sources_module, "fetch_rule_excerpt", "RULES"),
            (annotation_module, "review_unit_path", ".//listBibl/bibl"),
        ):
            patcher = mock.patch.object(target, name, return_value=value)
            self.addCleanup(patcher.stop)
            patcher.start()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_reviewer_or_admin] = lambda: {"username": "rev", "roles": ["reviewer"]}
        app.dependency_overrides[get_file_repository] = FakeRepo
        app.dependency_overrides[get_file_storage] = FakeStorage
        self.client = TestClient(app)
        LLMProviderRegistry.reset_instance()
        self.addCleanup(LLMProviderRegistry.reset_instance)
        mapping = gold_mapping()
        mapping_b = dict(mapping)
        second = document_units(INPUT_XML, ".//listBibl/bibl")[1].xml
        mapping_b[second] = second.replace("<title>", '<title level="a">')
        self.arbiter = ArbiterOracle(mapping)
        for provider in (ClientProvider("pa", OracleClient(mapping)), ClientProvider("pb", OracleClient(mapping_b)), ClientProvider("pc", self.arbiter)):
            LLMProviderRegistry.get_instance().register(provider)

    def post(self, **body):
        return self.client.post("/api/plugins/auto-annotation/annotate", json={"model_a": "pa", "model_b": "pb", "arbiter": "pc/m", **body})

    def test_annotate_returns_the_new_document_and_the_log(self):
        response = self.post(stable_id="d2v1", batch="units:2")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["input_stable_id"], body["doc_id"], body["variant"]), ("d2v1", "doc2", VARIANT))
        self.assertEqual(body["models"], {"A": "pa/m", "B": "pb/m", "arbiter": "pc/m"})
        self.assertEqual(body["counts"], {"agreed": 3, "arbitrated": 1, "unresolved": 0, "changed": 4})
        self.assertEqual([u.xml for u in document_units(body["xml"], ".//listBibl/bibl")], GOLD_UNITS)
        log = body["log"]
        self.assertEqual(log["runs"]["A"]["calls"], 2)
        self.assertEqual(log["runs"]["arbiter"]["calls"], 1)
        self.assertNotIn("raw_response", log["runs"]["A"]["batches"][0])
        self.assertEqual(log["units"][1]["decision"], "arbitrated")
        self.assertEqual(log["rules"], [{"category": "references", "url": "https://raw.example.org/guidelines-llm.md#L1-L5"}])

    def test_annotate_without_arbiter_and_with_raw_answers(self):
        response = self.post(stable_id="d2v1", arbiter=None, include_raw=True)
        body = response.json()
        self.assertEqual(body["counts"]["unresolved"], 1)
        self.assertIn("raw_response", body["log"]["runs"]["B"]["batches"][0])
        self.assertNotIn("standOff", body["xml"])
        self.assertEqual(self.arbiter.calls, [])

    def test_annotate_errors(self):
        self.assertEqual(self.post(stable_id="nope").status_code, 404)
        self.assertEqual(self.post(stable_id="d3g").status_code, 422)  # gold version
        self.assertEqual(self.post(stable_id="d2v1", model_a="unknown").status_code, 404)
        self.assertEqual(self.post(stable_id="d2v1", batch="16").status_code, 422)
        with mock.patch.object(annotation_module, "check_file_access", return_value=False):
            self.assertEqual(self.post(stable_id="d2v1").status_code, 403)

    def test_targets(self):
        response = self.client.get("/api/plugins/auto-annotation/targets", params={"collection": "c1", "variant": VARIANT})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [(t["doc_id"], t["input_stable_id"], t["state"], t["annotated_stable_id"]) for t in response.json()],
            [("doc1", "d1v1", "annotated", "d1v2"), ("doc2", "d2v1", "ready", None), ("doc3", "d3v1", "gold", None)],
        )
        by_doc = self.client.get("/api/plugins/auto-annotation/targets", params=[("doc", "doc4"), ("doc", "doc2"), ("variant", VARIANT)]).json()
        self.assertEqual([(t["doc_id"], t["state"]) for t in by_doc], [("doc4", "no-version"), ("doc2", "ready")])
        self.assertEqual(self.client.get("/api/plugins/auto-annotation/targets", params={"variant": VARIANT}).status_code, 422)
        self.assertEqual(self.client.get("/api/plugins/auto-annotation/targets", params={"collection": "c1"}).status_code, 422)  # no variant


if __name__ == "__main__":
    unittest.main()
