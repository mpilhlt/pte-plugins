"""
A 429 of an endpoint becomes LLMRateLimitError instead of a plain LLMProviderError.

@testCovers plugins/inference-endpoints/src/pte_plugin_inference_endpoints/llm_provider.py:_chat_completion_sync
"""

import unittest
from unittest.mock import patch

from fastapi_app.lib.llm import rate_limit
from fastapi_app.plugin_api import LLMRateLimitError
from pte_plugin_inference_endpoints.llm_provider import EndpointLLMProvider
from pte_plugin_inference_endpoints.store import Endpoint

from .test_llm_provider import OLLAMA, ProviderTestCase, _response


class TestRateLimit(ProviderTestCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(rate_limit, "_tracker", rate_limit.RateLimitTracker())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_429_raises_rate_limit_error(self):
        self.session.post.return_value = _response(429, body={"error": "quota"}, headers={"Retry-After": "600"})
        with self.assertRaises(LLMRateLimitError) as raised:
            EndpointLLMProvider(OLLAMA)._chat_completion_sync("m", "sys", "user", 0.0)
        self.assertIsNotNone(raised.exception.available_at)

    def test_shared_key_is_sent_and_kept_out_of_the_error(self):
        self.session.post.return_value = _response(429, body={"error": "quota"})
        provider = EndpointLLMProvider(Endpoint(id="x", name="X", base_url="http://x/v1", key_mode="shared"))
        with self.assertRaises(LLMRateLimitError) as raised:
            provider._chat_completion_sync("m", "sys", "user", 0.0)
        self.assertEqual(self.session.post.call_args.kwargs["headers"]["Authorization"], "Bearer shared-secret")
        self.assertNotIn("shared-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
