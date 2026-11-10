"""
A 429 of the Gemini API becomes LLMRateLimitError, with the retry delay of its error details.

@testCovers plugins/google-llm/src/pte_plugin_google_llm/llm_provider.py:_provider_error
"""

import unittest
from datetime import datetime, timedelta, timezone

from google.genai import errors as genai_errors

from fastapi_app.plugin_api import LLMProviderError, LLMRateLimitError
from pte_plugin_google_llm.llm_provider import _provider_error


def _error(code: int, status: str, details: list[dict[str, str]]) -> genai_errors.APIError:
    return genai_errors.APIError(code, {"error": {"code": code, "message": "You exceeded your current quota", "status": status, "details": details}})


class TestRateLimit(unittest.TestCase):
    def test_quota_error_with_retry_delay(self):
        before = datetime.now(timezone.utc)
        error = _provider_error(_error(429, "RESOURCE_EXHAUSTED", [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]))
        self.assertIsInstance(error, LLMRateLimitError)
        assert isinstance(error, LLMRateLimitError) and error.available_at is not None
        self.assertGreaterEqual(error.available_at, before + timedelta(seconds=33))
        self.assertIn("exceeded your current quota", str(error))

    def test_429_without_retry_delay_has_no_time(self):
        error = _provider_error(_error(429, "RESOURCE_EXHAUSTED", []))
        assert isinstance(error, LLMRateLimitError)
        self.assertIsNone(error.available_at)

    def test_503_is_overload_not_a_rate_limit(self):
        error = _provider_error(_error(503, "UNAVAILABLE", []))
        self.assertIsInstance(error, LLMProviderError)
        self.assertNotIsInstance(error, LLMRateLimitError)


if __name__ == "__main__":
    unittest.main()
