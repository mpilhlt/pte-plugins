"""
Unit tests for the endpoint LLM provider.

@testCovers plugins/inference-endpoints/src/pte_plugin_inference_endpoints/llm_provider.py
"""

import base64
import unittest
from unittest.mock import MagicMock, patch

import requests

from fastapi_app.lib.llm import LLMImage, LLMProviderError
from pte_plugin_inference_endpoints.llm_provider import EndpointLLMProvider
from pte_plugin_inference_endpoints.store import Endpoint
from pte_plugin_inference_endpoints.url_policy import EndpointURLError

OLLAMA = Endpoint(id="ollama", name="Ollama", base_url="http://localhost:11434/v1")


def _response(status_code: int = 200, body: object = None, headers: dict[str, str] | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body
    response.text = str(body)
    response.headers = headers or {}
    response.is_redirect = 300 <= status_code < 400
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"{status_code} Error", response=response)
    return response


class ProviderTestCase(unittest.TestCase):
    def setUp(self):
        for target in ("check_url", "get_shared_key"):
            patcher = patch(f"pte_plugin_inference_endpoints.llm_provider.{target}")
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        self.get_shared_key.return_value = "shared-secret"
        patcher = patch("pte_plugin_inference_endpoints.llm_provider.get_retry_session")
        self.session = patcher.start().return_value
        self.addCleanup(patcher.stop)
        patcher = patch("pte_plugin_inference_endpoints.llm_provider.get_completion_session", return_value=self.session)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestKeys(ProviderTestCase):
    def test_no_key_sends_no_authorization_header(self):
        self.session.get.return_value = _response(body={"data": []})
        EndpointLLMProvider(OLLAMA).list_models()
        self.assertNotIn("Authorization", self.session.get.call_args[1]["headers"])

    def test_shared_key_is_read_from_the_secret_store(self):
        self.session.get.return_value = _response(body={"data": []})
        provider = EndpointLLMProvider(Endpoint(id="x", name="X", base_url="http://x/v1", key_mode="shared"))
        self.assertTrue(provider.has_shared_key())
        self.assertIsNone(provider.secret_service)
        provider.list_models()
        self.assertEqual(self.session.get.call_args[1]["headers"]["Authorization"], "Bearer shared-secret")

    def test_user_keys(self):
        self.session.get.return_value = _response(body={"data": []})
        provider = EndpointLLMProvider(Endpoint(id="x", name="X", base_url="http://x/v1", key_mode="user"))
        self.assertFalse(provider.has_shared_key())
        self.assertEqual(provider.secret_service, "inference-endpoint-x")
        user_provider = provider.with_api_key("user-key")
        self.assertTrue(user_provider.has_shared_key())
        user_provider.list_models()
        self.assertEqual(self.session.get.call_args[1]["headers"]["Authorization"], "Bearer user-key")
        self.get_shared_key.assert_not_called()


class TestListModels(ProviderTestCase):
    def test_lists_models_from_endpoint(self):
        self.session.get.return_value = _response(body={"data": [{"id": "qwen2.5:7b"}, {"id": "llama3.2:latest"}, {"object": "x"}]})
        models = EndpointLLMProvider(OLLAMA).list_models()
        self.assertEqual([m["id"] for m in models], ["llama3.2:latest", "qwen2.5:7b"])
        self.assertEqual(models[0]["capabilities"], frozenset({"chat"}))
        self.assertFalse(models[0]["free"])
        self.assertEqual(self.session.get.call_args[0][0], "http://localhost:11434/v1/models")
        self.assertFalse(self.session.get.call_args[1]["allow_redirects"])

    def test_configured_models_need_no_request(self):
        endpoint = Endpoint(id="o", name="O", base_url="http://o/v1", models=("llava",), vision=True, free=True)
        models = EndpointLLMProvider(endpoint).list_models()
        self.assertEqual(models[0]["id"], "llava")
        self.assertEqual(models[0]["capabilities"], frozenset({"chat", "vision"}))
        self.assertTrue(models[0]["free"])
        self.session.get.assert_not_called()

    def test_connection_error_becomes_provider_error(self):
        self.session.get.side_effect = requests.ConnectionError("refused")
        with self.assertRaisesRegex(LLMProviderError, "Could not reach Ollama"):
            EndpointLLMProvider(OLLAMA).list_models()

    def test_redirect_is_rejected(self):
        self.session.get.return_value = _response(302, headers={"location": "http://169.254.169.254/"})
        with self.assertRaisesRegex(LLMProviderError, "redirected"):
            EndpointLLMProvider(OLLAMA).list_models()

    def test_forbidden_url_is_rejected_before_request(self):
        self.check_url.side_effect = EndpointURLError("link-local")
        with self.assertRaisesRegex(LLMProviderError, "link-local"):
            EndpointLLMProvider(OLLAMA).list_models()
        self.session.get.assert_not_called()


class TestChatCompletion(ProviderTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_posts_openai_payload(self):
        self.session.post.return_value = _response(body={"choices": [{"message": {"content": "hello"}}]})
        result = await EndpointLLMProvider(OLLAMA).chat_completion("llama3.2", "sys", "usr", temperature=0.4)
        self.assertEqual(result, "hello")
        args, kwargs = self.session.post.call_args
        self.assertEqual(args[0], "http://localhost:11434/v1/chat/completions")
        self.assertEqual(kwargs["json"]["model"], "llama3.2")
        self.assertEqual(kwargs["json"]["messages"], [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}])
        self.assertEqual(kwargs["json"]["temperature"], 0.4)
        self.assertFalse(kwargs["json"]["stream"])
        self.assertEqual(kwargs["timeout"], (10, 600))
        self.assertFalse(kwargs["allow_redirects"])

    async def test_http_error_becomes_provider_error(self):
        self.session.post.return_value = _response(404, body={"error": "model 'x' not found"})
        with self.assertRaisesRegex(LLMProviderError, "Ollama returned 404"):
            await EndpointLLMProvider(OLLAMA).chat_completion("x", "s", "u")

    async def test_timeout_becomes_provider_error(self):
        self.session.post.side_effect = requests.ReadTimeout()
        with self.assertRaisesRegex(LLMProviderError, "within 600 seconds"):
            await EndpointLLMProvider(OLLAMA).chat_completion("x", "s", "u")

    async def test_empty_choices(self):
        self.session.post.return_value = _response(body={"choices": []})
        self.assertEqual(await EndpointLLMProvider(OLLAMA).chat_completion("x", "s", "u"), "")

    async def test_images_are_sent_as_data_urls(self):
        self.session.post.return_value = _response(body={"choices": [{"message": {"content": "text"}}]})
        provider = EndpointLLMProvider(Endpoint(id="v", name="V", base_url="http://v/v1", vision=True))
        self.assertTrue(provider.supports_image_input("m"))
        self.assertFalse(EndpointLLMProvider(OLLAMA).supports_image_input("m"))
        images: list[LLMImage] = [{"data": b"page-1", "media_type": "image/png"}]
        self.assertEqual(await provider.chat_completion_with_images("m", "sys", "usr", images), "text")
        content = self.session.post.call_args[1]["json"]["messages"][1]["content"]
        self.assertEqual(content[0], {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"page-1").decode()}})
        self.assertEqual(content[1], {"type": "text", "text": "usr"})


if __name__ == "__main__":
    unittest.main()
