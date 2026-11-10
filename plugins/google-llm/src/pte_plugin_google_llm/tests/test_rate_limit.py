"""
A 429 of the Gemini API becomes LLMRateLimitError, and the key is remembered as limited per model, so that further calls fail without reaching the API.

@testCovers plugins/google-llm/src/pte_plugin_google_llm/llm_provider.py:_provider_error
@testCovers plugins/google-llm/src/pte_plugin_google_llm/llm_provider.py:_limited_until
@testCovers plugins/google-llm/src/pte_plugin_google_llm/llm_provider.py:GoogleLLMProvider
"""

import logging
import unittest
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from google.genai import errors as genai_errors

from fastapi_app.lib.llm.rate_limit import DEFAULT_LIMITED_SECONDS, RateLimitTracker
from fastapi_app.plugin_api import LLMProviderError, LLMRateLimitError
from pte_plugin_google_llm.llm_provider import GoogleLLMProvider, _limited_until, _provider_error

_RETRY_INFO = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}
_PER_MINUTE = {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests", "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "quotaValue": "15"}]}
_PER_DAY = {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests", "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "50"}]}

_KEY = "secret-key-123"


def _error(code: int, status: str, details: Sequence[Mapping[str, object]]) -> genai_errors.APIError:
    return genai_errors.APIError(code, {"error": {"code": code, "message": "You exceeded your current quota", "status": status, "details": details}})


class TestProviderError(unittest.TestCase):
    def test_quota_error_with_retry_delay(self):
        before = datetime.now(timezone.utc)
        error = _provider_error(_error(429, "RESOURCE_EXHAUSTED", [_PER_MINUTE, _RETRY_INFO]))
        self.assertIsInstance(error, LLMRateLimitError)
        assert isinstance(error, LLMRateLimitError) and error.available_at is not None
        self.assertGreaterEqual(error.available_at, before + timedelta(seconds=33))
        self.assertLess(error.available_at, before + timedelta(seconds=40))
        self.assertIn("exceeded your current quota", str(error))

    def test_429_without_retry_delay_is_limited_for_the_default_period(self):
        now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        error = _provider_error(_error(429, "RESOURCE_EXHAUSTED", []), now)
        assert isinstance(error, LLMRateLimitError)
        self.assertEqual(error.available_at, now + timedelta(seconds=DEFAULT_LIMITED_SECONDS))

    def test_503_is_overload_not_a_rate_limit(self):
        error = _provider_error(_error(503, "UNAVAILABLE", []))
        self.assertIsInstance(error, LLMProviderError)
        self.assertNotIsInstance(error, LLMRateLimitError)


class TestDailyQuota(unittest.TestCase):
    """An exhausted daily quota is limited until midnight Pacific time, even if the API gives a short retry delay."""

    def _details(self, *entries: Mapping[str, object]) -> dict[str, object]:
        return {"error": {"code": 429, "details": list(entries)}}

    def test_daily_quota_ignores_the_short_retry_delay(self):
        now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)  # 04:00 PST
        until = _limited_until(self._details(_PER_DAY, _RETRY_INFO), now)
        self.assertEqual(until, datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc))  # midnight PST

    def test_daily_reset_in_daylight_saving_time(self):
        now = datetime(2026, 7, 1, 6, 30, tzinfo=timezone.utc)  # 23:30 PDT on June 30
        until = _limited_until(self._details(_PER_DAY), now)
        self.assertEqual(until, datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc))  # midnight PDT

    def test_per_minute_quota_uses_the_retry_delay(self):
        now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(_limited_until(self._details(_PER_MINUTE, _RETRY_INFO), now), now + timedelta(seconds=34))


class TestLimitState(unittest.IsolatedAsyncioTestCase):
    """The provider remembers a 429 in the editor's tracker, per API key and model."""

    def setUp(self):
        self.now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        self.tracker = RateLimitTracker(now=lambda: self.now)
        tracker_patch = patch("pte_plugin_google_llm.llm_provider.get_rate_limit_tracker", return_value=self.tracker)
        tracker_patch.start()
        self.addCleanup(tracker_patch.stop)
        client_patch = patch("pte_plugin_google_llm.llm_provider.genai.Client")
        self.client_cls = client_patch.start()
        self.addCleanup(client_patch.stop)
        self.clients: dict[str, MagicMock] = {}
        self.client_cls.side_effect = lambda api_key: self.clients.setdefault(api_key, self._client())

    @staticmethod
    def _client() -> MagicMock:
        client = MagicMock()
        response = MagicMock()
        response.text = "ok"
        client.aio.models.generate_content = AsyncMock(return_value=response)
        uploaded = MagicMock()
        uploaded.name = "files/abc"
        client.aio.files.upload = AsyncMock(return_value=uploaded)
        client.aio.files.delete = AsyncMock()
        return client

    def _provider(self, api_key: str = _KEY) -> GoogleLLMProvider:
        return GoogleLLMProvider(id="google", label="Google Gemini", api_key=api_key)

    def _mock(self, provider: GoogleLLMProvider) -> MagicMock:
        """The mocked SDK client of the provider."""
        self.assertIsNotNone(provider._client)
        return self.clients[provider._api_key or ""]

    async def _hit_limit(self, provider: GoogleLLMProvider, model_id: str, details: Sequence[Mapping[str, object]]) -> LLMRateLimitError:
        client = self._mock(provider)
        client.aio.models.generate_content.side_effect = _error(429, "RESOURCE_EXHAUSTED", details)
        with self.assertRaises(LLMRateLimitError) as raised:
            await provider.chat_completion(model_id, "system", "user")
        client.aio.models.generate_content.side_effect = None
        return raised.exception

    async def test_next_call_raises_without_calling_the_sdk(self):
        provider = self._provider()
        await self._hit_limit(provider, "gemini-2.5-flash", [_RETRY_INFO])
        client = self._mock(provider)
        self.assertEqual(client.aio.models.generate_content.await_count, 1)

        with self.assertRaises(LLMRateLimitError) as raised:
            await provider.chat_completion("gemini-2.5-flash", "system", "user")
        self.assertEqual(raised.exception.available_at, self.now + timedelta(seconds=34))
        self.assertEqual(client.aio.models.generate_content.await_count, 1)

        with self.assertRaises(LLMRateLimitError):
            await provider.chat_completion_with_pdf("gemini-2.5-flash", "system", "user", "/tmp/x.pdf")
        client.aio.files.upload.assert_not_awaited()

    async def test_limit_ends_after_the_retry_delay(self):
        provider = self._provider()
        await self._hit_limit(provider, "gemini-2.5-flash", [_RETRY_INFO])
        self.now += timedelta(seconds=35)
        self.assertEqual(await provider.chat_completion("gemini-2.5-flash", "system", "user"), "ok")

    async def test_state_is_per_model(self):
        provider = self._provider()
        await self._hit_limit(provider, "gemini-2.5-flash", [_RETRY_INFO])
        self.assertEqual(await provider.chat_completion("gemini-2.5-pro", "system", "user"), "ok")

    async def test_state_is_per_api_key(self):
        await self._hit_limit(self._provider(), "gemini-2.5-flash", [_RETRY_INFO])
        self.assertEqual(await self._provider("other-key").chat_completion("gemini-2.5-flash", "system", "user"), "ok")

    async def test_429_without_retry_delay_is_limited_for_the_default_period(self):
        provider = self._provider()
        error = await self._hit_limit(provider, "gemini-2.5-flash", [])
        self.assertEqual(error.available_at, self.now + timedelta(seconds=DEFAULT_LIMITED_SECONDS))
        self.now += timedelta(seconds=DEFAULT_LIMITED_SECONDS - 1)
        with self.assertRaises(LLMRateLimitError):
            await provider.chat_completion("gemini-2.5-flash", "system", "user")
        self.now += timedelta(seconds=2)
        self.assertEqual(await provider.chat_completion("gemini-2.5-flash", "system", "user"), "ok")

    async def test_429_of_the_pdf_upload_is_remembered(self):
        provider = self._provider()
        client = self._mock(provider)
        client.aio.files.upload.side_effect = _error(429, "RESOURCE_EXHAUSTED", [_RETRY_INFO])
        with self.assertRaises(LLMRateLimitError):
            await provider.chat_completion_with_pdf("gemini-2.5-flash", "system", "user", "/tmp/x.pdf")
        with self.assertRaises(LLMRateLimitError):
            await provider.chat_completion("gemini-2.5-flash", "system", "user")
        client.aio.models.generate_content.assert_not_awaited()

    async def test_other_errors_are_not_remembered(self):
        provider = self._provider()
        client = self._mock(provider)
        client.aio.models.generate_content.side_effect = _error(503, "UNAVAILABLE", [])
        with self.assertRaises(LLMProviderError):
            await provider.chat_completion("gemini-2.5-flash", "system", "user")
        client.aio.models.generate_content.side_effect = None
        self.assertEqual(await provider.chat_completion("gemini-2.5-flash", "system", "user"), "ok")

    async def test_api_key_not_in_state_logs_or_errors(self):
        provider = self._provider()
        with self.assertLogs("pte_plugin_google_llm.llm_provider", level=logging.INFO) as logs:
            first = await self._hit_limit(provider, "gemini-2.5-flash", [_PER_DAY, _RETRY_INFO])
        with self.assertRaises(LLMRateLimitError) as second:
            await provider.chat_completion("gemini-2.5-flash", "system", "user")
        self.assertIn("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "\n".join(logs.output))
        for text in ["\n".join(logs.output), str(first), str(second.exception), repr(self.tracker._limited)]:
            self.assertNotIn(_KEY, text)


if __name__ == "__main__":
    unittest.main()
