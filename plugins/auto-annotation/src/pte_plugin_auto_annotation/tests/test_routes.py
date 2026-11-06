"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/routes.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/sources.py
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_app.lib.core.dependencies import get_file_repository, get_file_storage, require_reviewer_or_admin
from fastapi_app.lib.llm import LLMModel, LLMProvider, LLMProviderError, LLMProviderRegistry
from fastapi_app.lib.llm.model_access import ModelAccessDenied
from pte_plugin_auto_annotation import api_client
from pte_plugin_auto_annotation import routes as routes_module
from pte_plugin_auto_annotation import sources as sources_module
from pte_plugin_auto_annotation.routes import router

from .fixtures import CORRECTED, EXTRACTION, GOLD_XML, INPUT_UNITS, INPUT_XML, RENAMED, tei

CORRECTED_COPY_XML = tei(INPUT_UNITS, changes=EXTRACTION + CORRECTED + RENAMED)


def _file(stable_id: str, *, gold: bool = False, doc_id: str = "doc1", file_type: str = "tei", version: int = 1,
          created_at: str = "2026-01-01", variant: str = "grobid.training.references", parent: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=f"hash-{stable_id}", stable_id=stable_id, doc_id=doc_id, file_type=file_type, variant=variant,
                           is_gold_standard=gold, version=version, created_at=created_at, parent_stable_id=parent)


class FakeRepo:
    def __init__(self):
        self.files = {f.stable_id: f for f in (
            # v2 is listed first and v1b is a later extraction with the same version number
            _file("v2", version=2), _file("v1b", created_at="2026-02-01"), _file("v1"), _file("gold1", gold=True),
            _file("seg-gold", gold=True, variant="grobid.training.segmentation"),
            _file("other", doc_id="doc2"), _file("other-gold", doc_id="doc2", gold=True),
            _file("two-gold-a", doc_id="doc3", gold=True), _file("two-gold-b", doc_id="doc3", gold=True),
            # doc4: the gold version was derived from the later extraction x2 (via r1); x0 is an older extraction
            _file("x0", doc_id="doc4"), _file("x2", doc_id="doc4", created_at="2026-03-01"),
            _file("r1", doc_id="doc4", version=2, parent="x2"), _file("g4", doc_id="doc4", gold=True, parent="r1"),
            # doc5: the gold version's parent is of another variant, so the lineage gives no input
            _file("seg5", doc_id="doc5", variant="grobid.training.segmentation"), _file("x5", doc_id="doc5"),
            _file("g5", doc_id="doc5", gold=True, parent="seg5"),
            # doc6: the lineage starts with a corrected copy (an older document), an unrelated extraction exists
            _file("c6", doc_id="doc6"), _file("e6", doc_id="doc6", version=2), _file("g6", doc_id="doc6", gold=True, parent="c6"),
            # doc7: no extraction at all
            _file("c7", doc_id="doc7"), _file("g7", doc_id="doc7", gold=True, parent="c7"),
        )}

    def get_file_by_stable_id(self, stable_id):
        return self.files.get(stable_id)

    def get_files_by_doc_id(self, doc_id):
        return [f for f in self.files.values() if f.doc_id == doc_id]

    def get_all_versions(self, doc_id, variant=None):
        return [f for f in self.files.values() if f.doc_id == doc_id and not f.is_gold_standard and f.variant == variant]


class FakeStorage:
    def read_file(self, file_hash, file_type):
        return {"hash-gold1": GOLD_XML, "hash-v1": INPUT_XML, "hash-v2": GOLD_XML, "hash-other": INPUT_XML,
                "hash-g4": GOLD_XML, "hash-x2": INPUT_XML, "hash-x0": INPUT_XML, "hash-g5": GOLD_XML, "hash-x5": INPUT_XML,
                "hash-g6": GOLD_XML, "hash-c6": CORRECTED_COPY_XML, "hash-e6": INPUT_XML,
                "hash-g7": GOLD_XML, "hash-c7": CORRECTED_COPY_XML}.get(file_hash, "").encode() or None


class StubProvider(LLMProvider):
    def __init__(self, id: str, label: str, models: list[str]):
        self.id = id
        self.label = label
        self._models = models
        self.prompts: list[tuple[str, str, str, float]] = []

    def list_models(self) -> list[LLMModel]:
        return [{"id": m, "label": m.upper(), "capabilities": set(), "status": None} for m in self._models]  # type: ignore[typeddict-item]

    async def chat_completion(self, model_id, system_prompt, user_prompt, temperature=0.2) -> str:
        self.prompts.append((model_id, system_prompt, user_prompt, temperature))
        return "### Unit 1\n<bibl/>"


class TestRoutes(unittest.TestCase):
    def setUp(self):
        def patch(target: object, name: str, return_value: object = None) -> mock.MagicMock:
            patcher = mock.patch.object(target, name, return_value=return_value)
            self.addCleanup(patcher.stop)
            return patcher.start()

        self.mock_check_model_access = patch(routes_module, "check_model_access")
        patch(routes_module, "get_default_model")
        self.mock_check_file_access = patch(sources_module, "check_file_access", return_value=True)
        patch(sources_module, "is_safe_fetch_url", return_value=True)
        self.mock_fetch_rule_excerpt = patch(sources_module, "fetch_rule_excerpt", return_value="RULES")
        patch(sources_module, "review_unit_path", return_value=".//listBibl/bibl")
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_reviewer_or_admin] = lambda: {"username": "rev", "roles": ["reviewer"]}
        app.dependency_overrides[get_file_repository] = FakeRepo
        app.dependency_overrides[get_file_storage] = FakeStorage
        self.client = TestClient(app)
        LLMProviderRegistry.reset_instance()
        self.provider = StubProvider("endpoint-local", "Local", ["qwen", "llama"])
        LLMProviderRegistry.get_instance().register(self.provider)
        self.addCleanup(LLMProviderRegistry.reset_instance)

    def test_eval_source_uses_the_gold_version_and_the_earliest_version(self):
        response = self.client.get("/api/plugins/auto-annotation/eval-source", params={"doc": "doc1"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["doc_id"], body["reference_stable_id"], body["reference_is_gold"]), ("doc1", "gold1", True))
        self.assertEqual((body["variant"], body["input_stable_id"], body["input_choice"], body["unit_path"]), ("grobid.training.references", "v1", "earliest", ".//listBibl/bibl"))
        self.assertEqual(body["input_xml"], INPUT_XML)
        self.assertEqual(body["rules"], [{"category": "references", "text": "RULES", "url": "https://raw.example.org/guidelines-llm.md#L1-L5"}])

    def test_eval_source_follows_the_lineage_of_the_reference(self):
        get = lambda **p: self.client.get("/api/plugins/auto-annotation/eval-source", params={"rules": "false", **p}).json()  # noqa: E731
        body = get(doc="doc4")
        self.assertEqual((body["reference_stable_id"], body["input_stable_id"], body["input_choice"]), ("g4", "x2", "lineage"))
        body = get(doc="doc5")  # lineage leaves the variant: fall back to the earliest version
        self.assertEqual((body["input_stable_id"], body["input_choice"]), ("x5", "earliest"))
        body = get(doc="doc4", input="x0")
        self.assertEqual((body["input_stable_id"], body["input_choice"]), ("x0", "given"))

    def test_eval_source_uses_extractions_only(self):
        get = lambda **p: self.client.get("/api/plugins/auto-annotation/eval-source", params={"rules": "false", **p})  # noqa: E731
        body = get(doc="doc6").json()  # the corrected copy at the start of the lineage is skipped
        self.assertEqual((body["input_stable_id"], body["input_choice"], body["input_is_extraction"]), ("e6", "earliest", True))
        self.assertEqual(get(doc="doc7").status_code, 404)  # no extraction
        body = get(doc="doc7", input="c7").json()  # a given input is used, and flagged
        self.assertEqual((body["input_stable_id"], body["input_choice"], body["input_is_extraction"]), ("c7", "given", False))

    def test_eval_source_with_another_reference(self):
        get = lambda **p: self.client.get("/api/plugins/auto-annotation/eval-source", params={"rules": "false", **p})  # noqa: E731
        body = get(reference="v2").json()
        self.assertEqual((body["doc_id"], body["reference_stable_id"], body["reference_is_gold"], body["input_stable_id"]), ("doc1", "v2", False, "v1"))
        self.assertEqual(get(doc="doc1", reference="v2").status_code, 200)
        self.assertEqual(get(doc="doc2", reference="v2").status_code, 422)  # another document
        self.assertEqual(get(reference="v2", variant="grobid.training.segmentation").status_code, 422)  # another variant
        self.assertEqual(get(reference="v1", input="v1").json()["input_stable_id"], "v1")

    def test_eval_source_errors(self):
        get = lambda **p: self.client.get("/api/plugins/auto-annotation/eval-source", params=p).status_code  # noqa: E731
        self.assertEqual(get(), 422)  # neither document nor reference
        self.assertEqual(get(doc="nope"), 404)  # no gold version
        self.assertEqual(get(doc="doc1", variant="grobid.training.references.referenceSegmenter"), 404)
        self.assertEqual(get(doc="doc3"), 422)  # several gold versions
        self.assertEqual(get(reference="nope"), 404)
        self.assertEqual(get(doc="doc1", input="other"), 422)  # another document
        self.assertEqual(get(doc="doc1", input="v2", rules="false"), 200)
        self.mock_fetch_rule_excerpt.side_effect = OSError("down")
        self.assertEqual(get(doc="doc1"), 422)  # no rules
        self.mock_check_file_access.return_value = False
        self.assertEqual(get(doc="doc1", rules="false"), 403)

    def test_resolve_model(self):
        post = lambda m: self.client.post("/api/plugins/auto-annotation/resolve-model", json={"model": m})  # noqa: E731
        self.assertEqual(post("local/LLAMA").json(), {"provider_id": "endpoint-local", "model_id": "llama"})
        self.assertEqual(post("local").status_code, 404)  # several models, no default
        self.assertEqual(post("elsewhere/x").status_code, 404)
        self.mock_check_model_access.side_effect = ModelAccessDenied("not free")
        self.assertEqual(post("local/qwen").status_code, 403)

    def test_complete_relays_to_the_provider(self):
        response = self.client.post("/api/plugins/auto-annotation/complete", json={
            "provider_id": "endpoint-local", "model_id": "qwen", "system_prompt": "S", "user_prompt": "U", "temperature": 0.0,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["text"], "### Unit 1\n<bibl/>")
        self.assertEqual(self.provider.prompts, [("qwen", "S", "U", 0.0)])

    def test_api_client_against_the_routes(self):
        """The CLI's HTTP client reads what the routes return (requests are routed to the test app)."""
        client = self.client

        class Adapter:
            @staticmethod
            def _wrap(response):
                response.ok = response.is_success
                return response

            def get(self, url, params=None, headers=None, timeout=None):
                return self._wrap(client.get(url.replace("http://test", ""), params=params, headers=headers))

            def post(self, url, json=None, headers=None, timeout=None):
                return self._wrap(client.post(url.replace("http://test", ""), json=json, headers=headers))

        with mock.patch.object(api_client, "requests", Adapter()):
            session = api_client.ApiSession("http://test", "sid")
            source = session.eval_source("doc1")
            self.assertEqual((source.doc_id, source.reference_stable_id, source.reference_is_gold), ("doc1", "gold1", True))
            self.assertEqual((source.input_stable_id, source.rules), ("v1", [("references", "RULES")]))
            other = session.eval_source(reference_stable_id="v2", with_rules=False)
            self.assertEqual((other.reference_stable_id, other.reference_is_gold), ("v2", False))
            self.assertEqual(session.resolve_model("Local/qwen"), ("endpoint-local", "qwen"))
            chat = api_client.ServerChatClient(session, "endpoint-local", "qwen")
            self.assertEqual(asyncio.run(chat.complete("S", "U")), "### Unit 1\n<bibl/>")
            with self.assertRaises(api_client.ApiError):
                session.eval_source("nope")

    def test_complete_reports_transient_provider_errors_as_503(self):
        request = {"provider_id": "endpoint-local", "model_id": "qwen", "system_prompt": "S", "user_prompt": "U"}
        for message, status in (("503 This model is currently experiencing high demand", 503), ("400 invalid request", 502)):
            with self.subTest(message=message), mock.patch.object(self.provider, "chat_completion", side_effect=LLMProviderError(message)):
                self.assertEqual(self.client.post("/api/plugins/auto-annotation/complete", json=request).status_code, status)

    def test_complete_unknown_provider(self):
        response = self.client.post("/api/plugins/auto-annotation/complete", json={
            "provider_id": "nope", "model_id": "m", "system_prompt": "S", "user_prompt": "U",
        })
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
