"""
The multimodal call applies the rate-limit rules of the shared chat_completion_sync.

@testCovers plugins/kisski/src/pte_plugin_kisski/extractor.py:_call_llm_multimodal
"""

import unittest
from unittest.mock import MagicMock, patch

from fastapi_app.lib.llm import rate_limit
from fastapi_app.plugin_api import LLMRateLimitError
from pte_plugin_kisski.extractor import KisskiExtractor


def _response(status_code: int, headers: dict[str, str] | None = None, body: object = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.json.return_value = body
    return response


class TestMultimodalRateLimit(unittest.TestCase):
    def setUp(self):
        for target, value in ((rate_limit, "_tracker"),):
            patcher = patch.object(target, value, rate_limit.RateLimitTracker())
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("pte_plugin_kisski.extractor.get_config")
        patcher.start().return_value.get.return_value = "https://chat-ai.academiccloud.de/v1/"
        self.addCleanup(patcher.stop)
        patcher = patch("pte_plugin_kisski.extractor.get_completion_session")
        self.session = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.extractor = KisskiExtractor()
        self.extractor.client = "test-api-key"

    def test_long_wait_raises_and_blocks_further_requests_with_the_same_key(self):
        self.session.post.return_value = _response(429, {"Retry-After": "3600"})
        with self.assertRaises(LLMRateLimitError) as raised:
            self.extractor._call_llm_multimodal("sys", [{"type": "text", "text": "x"}], "m")
        self.assertIsNotNone(raised.exception.available_at)
        with self.assertRaises(LLMRateLimitError):
            self.extractor._call_llm_multimodal("sys", [{"type": "text", "text": "x"}], "m")
        self.assertEqual(self.session.post.call_count, 1)

    def test_answer_is_returned_and_the_endpoint_has_no_trailing_slash(self):
        self.session.post.return_value = _response(200, body={"choices": [{"message": {"content": "ok"}}]})
        self.assertEqual(self.extractor._call_llm_multimodal("sys", [], "m"), "ok")
        self.assertEqual(self.session.post.call_args.args[0], "https://chat-ai.academiccloud.de/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
