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
from fastapi_app.lib.llm import LLMModel, LLMProvider, LLMProviderRegistry
from fastapi_app.lib.llm.model_access import ModelAccessDenied
from pte_plugin_auto_annotation import api_client
from pte_plugin_auto_annotation import routes as routes_module
from pte_plugin_auto_annotation import sources as sources_module
from pte_plugin_auto_annotation.routes import router

from .fixtures import GOLD_XML, INPUT_XML


def _file(stable_id: str, *, gold: bool = False, doc_id: str = "doc1", file_type: str = "tei") -> SimpleNamespace:
    return SimpleNamespace(id=f"hash-{stable_id}", stable_id=stable_id, doc_id=doc_id, file_type=file_type,
                           variant="grobid.training.references", is_gold_standard=gold)


class FakeRepo:
    def __init__(self):
        self.files = {f.stable_id: f for f in (_file("gold1", gold=True), _file("v1"), _file("v2"), _file("other", doc_id="doc2"))}

    def get_file_by_stable_id(self, stable_id):
        return self.files.get(stable_id)

    def get_all_versions(self, doc_id, variant=None):
        return [f for f in self.files.values() if f.doc_id == doc_id and not f.is_gold_standard and f.variant == variant]


class FakeStorage:
    def read_file(self, file_hash, file_type):
        return {"hash-gold1": GOLD_XML, "hash-v1": INPUT_XML, "hash-v2": "<TEI/>"}.get(file_hash, "").encode() or None


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

    def test_eval_source_defaults_to_the_earliest_version(self):
        response = self.client.get("/api/plugins/auto-annotation/eval-source", params={"gold": "gold1"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["input_stable_id"], body["unit_path"]), ("v1", ".//listBibl/bibl"))
        self.assertEqual(body["input_xml"], INPUT_XML)
        self.assertEqual(body["rules"], [{"category": "references", "text": "RULES", "url": "https://raw.example.org/guidelines-llm.md#L1-L5"}])

    def test_eval_source_errors(self):
        get = lambda **p: self.client.get("/api/plugins/auto-annotation/eval-source", params=p).status_code  # noqa: E731
        self.assertEqual(get(gold="nope"), 404)
        self.assertEqual(get(gold="v1"), 422)  # not a gold version
        self.assertEqual(get(gold="gold1", input="other"), 422)  # another document
        self.assertEqual(get(gold="gold1", input="v2", rules="false"), 200)
        self.mock_fetch_rule_excerpt.side_effect = OSError("down")
        self.assertEqual(get(gold="gold1"), 422)  # no rules
        self.mock_check_file_access.return_value = False
        self.assertEqual(get(gold="gold1", rules="false"), 403)

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
            source = session.eval_source("gold1")
            self.assertEqual((source.input_stable_id, source.rules), ("v1", [("references", "RULES")]))
            self.assertEqual(session.resolve_model("Local/qwen"), ("endpoint-local", "qwen"))
            chat = api_client.ServerChatClient(session, "endpoint-local", "qwen")
            self.assertEqual(asyncio.run(chat.complete("S", "U")), "### Unit 1\n<bibl/>")
            with self.assertRaises(api_client.ApiError):
                session.eval_source("nope")

    def test_complete_unknown_provider(self):
        response = self.client.post("/api/plugins/auto-annotation/complete", json={
            "provider_id": "nope", "model_id": "m", "system_prompt": "S", "user_prompt": "U",
        })
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
