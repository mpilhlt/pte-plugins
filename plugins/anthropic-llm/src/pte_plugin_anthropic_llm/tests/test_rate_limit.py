"""
A 429 of the Messages API becomes LLMRateLimitError.

@testCovers plugins/anthropic-llm/src/pte_plugin_anthropic_llm/llm_provider.py:_chat_completion_sync
"""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch

from fastapi_app.lib.llm import rate_limit
from fastapi_app.plugin_api import LLMRateLimitError
from pte_plugin_anthropic_llm.llm_provider import AnthropicLLMProvider


class TestRateLimit(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(rate_limit, "_tracker", rate_limit.RateLimitTracker())
        patcher.start()
        self.addCleanup(patcher.stop)

    @patch("pte_plugin_anthropic_llm.llm_provider.get_completion_session")
    def test_429_with_a_long_wait_raises_with_the_time(self, mock_get_session):
        limited = MagicMock(status_code=429, headers={"retry-after": "120", "anthropic-ratelimit-requests-remaining": "0"})
        mock_get_session.return_value.post.return_value = limited
        provider = AnthropicLLMProvider("anthropic", "Anthropic", "sk-test")
        before = rate_limit.utc_now()
        with self.assertRaises(LLMRateLimitError) as raised:
            provider._chat_completion_sync("claude-x", "sys", "user", 0.0)
        available_at = raised.exception.available_at
        assert available_at is not None
        self.assertGreaterEqual(available_at, before + timedelta(seconds=119))
        self.assertNotIn("sk-test", str(raised.exception))
        self.assertEqual(mock_get_session.return_value.post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
