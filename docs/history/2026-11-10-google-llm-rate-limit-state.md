# google-llm: remember rate limits between requests

Issue #37 (follow-up to #36, plugin side of mpilhlt/pdf-tei-editor#721), worked on from the open items in #38 on 2026-11-10.

## Problem

`google-llm` turned a 429 into `LLMRateLimitError` (#36), but kept no state: the `google-genai` SDK makes its own requests, so the editor's `RateLimitTracker`, which `post_completion()` feeds for the other providers, never learned about the limit. Every further call reached the API again.

## Findings

- The SDK does not retry 429 unless `HttpRetryOptions` are given: `retry_args(None)` in `google/genai/_api_client.py` (google-genai 3.0.0) stops after one attempt. Only connection errors are retried implicitly. Nothing to switch off.
- The Gemini API reports the exceeded quota in a `google.rpc.QuotaFailure` entry of the error details (`quotaId`, e.g. `GenerateRequestsPerMinutePerProjectPerModel-FreeTier` or `GenerateRequestsPerDayPerProjectPerModel-FreeTier`) next to the `google.rpc.RetryInfo` with `retryDelay`. For an exhausted daily quota the `retryDelay` can still be short, so following it would only lead to the next failing request. Daily quotas are reset at midnight Pacific time. This is based on the documented error format; a real daily-quota 429 has not been observed in this work.

## Implementation

- `llm_provider.py`: before `chat_completion()` and `chat_completion_with_pdf()`, `_raise_if_limited()` checks `get_rate_limit_tracker().limited_until(GEMINI_ENDPOINT, api_key, model_id)` and raises `LLMRateLimitError` without calling the SDK. On a failed call, `_call_error()` marks the key and model as limited when the error is a 429.
- `_limited_until()`: until the next midnight Pacific time if a quota id contains `PerDay`, otherwise for the `retryDelay`, otherwise for the editor's `DEFAULT_LIMITED_SECONDS` (60 s). Without time zone data, a fixed UTC-7 offset is used, so that the limit never ends after the reset.
- A 429 now always has an `available_at` (previously None without `retryDelay`).
- The log line on a 429 names the exceeded quota ids and the retry delay. The tracker keys by a hash of the API key, so the key does not appear in state, logs or messages.
- `get_rate_limit_tracker` and `DEFAULT_LIMITED_SECONDS` are imported from `fastapi_app.lib.llm.rate_limit`, which exists from editor 0.82.0 on (the plugin already requires it), but they are not part of `fastapi_app.plugin_api`. Exporting them there would make the plugin independent of the editor's internal module layout.

## Tests

`tests/test_rate_limit.py`: retry delay, default period, daily quota (standard and daylight saving time), the next call raising without an SDK call (also for the PDF variant), the limit ending, state per model and per API key, a 429 of the PDF upload, other errors not being remembered, and the API key not appearing in state, logs or errors. 35 tests of `google-llm` pass; pyright reports no errors for the plugin.
