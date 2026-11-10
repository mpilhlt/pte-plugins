"""
Google Gemini LLM provider connector.

Implements LLMProvider directly (not OpenAICompatibleProvider) since the
Gemini API is not itself an OpenAI chat/completions endpoint - unlike
Kisski, which hosts Gemma models behind an OpenAI-compatible endpoint.
Uses the google-genai SDK, an existing project dependency. Accepts PDF
input (chat_completion_with_pdf), which the LLamore extractor uses to send
the PDF itself rather than its text layer.
"""

import logging
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from google import genai
from google.genai import errors as genai_errors, types

from fastapi_app.lib.llm.rate_limit import DEFAULT_LIMITED_SECONDS, get_rate_limit_tracker
from fastapi_app.plugin_api import LLMModel, LLMProvider, LLMProviderError, LLMRateLimitError, get_config

logger = logging.getLogger(__name__)

# The google-genai SDK's Model type exposes no modality/capability field
# beyond `supported_actions` (which only distinguishes generateContent from
# embedContent/bidiGenerateContent/etc). Image-generation, TTS, transcription
# and computer-use models all support generateContent but don't behave like
# a plain text chat model, so they're recognized by name suffix instead
# (verified against the live API's actual model names).
_NON_CHAT_NAME_MARKERS = ("-image", "-tts", "-transcribe", "-computer-use")

_MAJOR_VERSION_RE = re.compile(r"^gemini-(\d+)")

# Errors of a Gemini API call: the API rejected the request, or the connection
# failed (the SDK sends its requests with httpx and does not wrap its errors)
_API_ERRORS = (genai_errors.APIError, httpx.HTTPError)


_RETRY_DELAY_RE = re.compile(r"(\d+(?:\.\d+)?)s")

#: The endpoint under which the editor's rate-limit tracker keeps the state of Gemini API keys
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com"

# Daily quotas of the Gemini API are reset at midnight Pacific time
try:
    _QUOTA_TIMEZONE: timezone | ZoneInfo = ZoneInfo("America/Los_Angeles")
except ZoneInfoNotFoundError:  # no time zone data installed: Pacific daylight time, so the limit ends at the latest at the reset
    _QUOTA_TIMEZONE = timezone(timedelta(hours=-7))


def _error_details(details: object) -> list[dict[str, object]]:
    """The entries of `error.details` in an error response."""
    error = details.get("error") if isinstance(details, dict) else None
    entries = error.get("details") if isinstance(error, dict) else None
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _retry_delay_seconds(details: object) -> float | None:
    """The `retryDelay` of a google.rpc.RetryInfo in an error response ("34s"), or None."""
    for entry in _error_details(details):
        if str(entry.get("@type", "")).endswith("google.rpc.RetryInfo"):
            match = _RETRY_DELAY_RE.fullmatch(str(entry.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    return None


def _exceeded_quota_ids(details: object) -> list[str]:
    """The `quotaId`s of the violations of a google.rpc.QuotaFailure in an error response, such as "GenerateRequestsPerDayPerProjectPerModel-FreeTier"."""
    quota_ids: list[str] = []
    for entry in _error_details(details):
        violations = entry.get("violations") if str(entry.get("@type", "")).endswith("google.rpc.QuotaFailure") else None
        for violation in violations if isinstance(violations, list) else []:
            if isinstance(violation, dict) and violation.get("quotaId"):
                quota_ids.append(str(violation["quotaId"]))
    return quota_ids


def _next_daily_reset(now: datetime) -> datetime:
    """The next midnight Pacific time, in UTC."""
    local = now.astimezone(_QUOTA_TIMEZONE)
    midnight = datetime.combine(local.date() + timedelta(days=1), time(), tzinfo=_QUOTA_TIMEZONE)
    return midnight.astimezone(timezone.utc)


def _limited_until(details: object, now: datetime) -> datetime:
    """Until when a 429 limits the key and model.

    The `retryDelay` of the error is used for limits per minute. When a daily quota is exhausted (a quota id with "PerDay"), the API may still give a short `retryDelay`, after which every request would fail again, so the key counts as limited until the daily reset. Without either, the key is limited for the editor's default period.
    """
    if any("PerDay" in quota_id for quota_id in _exceeded_quota_ids(details)):
        return _next_daily_reset(now)
    delay = _retry_delay_seconds(details)
    return now + timedelta(seconds=delay if delay is not None else DEFAULT_LIMITED_SECONDS)


def _provider_error(error: genai_errors.APIError | httpx.HTTPError, now: datetime | None = None) -> LLMProviderError:
    """A user-presentable LLMProviderError for a failed Gemini API call; a 429 (rate limit, exhausted quota) is an LLMRateLimitError."""
    if isinstance(error, genai_errors.APIError):
        if error.code == 429:
            available_at = _limited_until(error.details, now or datetime.now(timezone.utc))
            return LLMRateLimitError(f"{error.code} {error.message}", available_at=available_at)
        return LLMProviderError(f"{error.code} {error.message}")
    return LLMProviderError(f"Could not reach the Gemini API: {error or type(error).__name__}")


def _is_gemini_3_or_later(model_id: str) -> bool:
    """
    True for Gemini 3+ models, including the unversioned "-latest" aliases
    (e.g. "gemini-flash-lite-latest"), which resolve to the current generation.
    """
    if model_id.endswith("-latest"):
        return True
    match = _MAJOR_VERSION_RE.match(model_id)
    return match is not None and int(match.group(1)) >= 3


def _generation_config(model_id: str, system_prompt: str, temperature: float) -> types.GenerateContentConfig:
    """
    Gemini 3+ is tuned for its default sampling settings: Google advises
    removing temperature/top_p/top_k from requests, so the caller's
    temperature is only sent to older models. Gemini 3 Flash-Lite defaults
    to the "minimal" thinking level, at which it often answers a review task
    with an empty result; it is raised to "medium".
    """
    if not _is_gemini_3_or_later(model_id):
        return types.GenerateContentConfig(system_instruction=system_prompt, temperature=temperature)
    thinking_config = (
        types.ThinkingConfig(thinking_level=types.ThinkingLevel.MEDIUM) if "flash-lite" in model_id else None
    )
    return types.GenerateContentConfig(system_instruction=system_prompt, thinking_config=thinking_config)


class GoogleLLMProvider(LLMProvider):
    """LLM provider connector for Google's Gemini API.

    The shared key is read from `plugin.google-llm.api.key` on every use, so an admin can set or remove it without a restart. A key passed to the constructor (a user's own key, see with_api_key()) takes precedence.
    """

    def __init__(self, id: str, label: str, api_key: str | None, secret_service: str | None = None):
        self.id = id
        self.label = label
        self._own_api_key = api_key
        self._client_for_key: tuple[str, genai.Client] | None = None
        self.secret_service = secret_service

    @property
    def _api_key(self) -> str | None:
        return self._own_api_key or get_config().get("plugin.google-llm.api.key") or None

    @property
    def _client(self) -> genai.Client | None:
        api_key = self._api_key
        if not api_key:
            return None
        if self._client_for_key is None or self._client_for_key[0] != api_key:
            self._client_for_key = (api_key, genai.Client(api_key=api_key))
        return self._client_for_key[1]

    def has_shared_key(self) -> bool:
        return bool(self._api_key)

    def with_api_key(self, api_key: str) -> "GoogleLLMProvider":
        return GoogleLLMProvider(self.id, self.label, api_key, self.secret_service)

    def _raise_if_limited(self, model_id: str) -> None:
        """Raise LLMRateLimitError without calling the API if the key is known to be limited for the model.

        The SDK makes its own requests, so the limit state is kept in the editor's tracker here, as post_completion() does for the other providers.
        """
        until = get_rate_limit_tracker().limited_until(GEMINI_ENDPOINT, self._api_key, model_id)
        if until is not None:
            raise LLMRateLimitError(f"Rate limit or quota reached for model '{model_id}'; try again after {until:%Y-%m-%d %H:%M:%S} UTC", available_at=until)

    def _call_error(self, model_id: str, error: genai_errors.APIError | httpx.HTTPError) -> LLMProviderError:
        """The LLMProviderError for a failed call; a rate limit is remembered for the key and model."""
        tracker = get_rate_limit_tracker()
        provider_error = _provider_error(error, tracker.now())
        if isinstance(provider_error, LLMRateLimitError) and provider_error.available_at is not None:
            tracker.mark_limited(GEMINI_ENDPOINT, self._api_key, model_id, provider_error.available_at)
            details = error.details if isinstance(error, genai_errors.APIError) else None
            logger.info(f"Rate limit for model '{model_id}' at {GEMINI_ENDPOINT}: no requests until {provider_error.available_at.isoformat()}; quotas: {_exceeded_quota_ids(details) or 'not given'}, retry delay: {_retry_delay_seconds(details)}")
        return provider_error

    def list_models(self) -> list[LLMModel]:
        if self._client is None:
            return []
        models: list[LLMModel] = []
        try:
            listed = list(self._client.models.list())
        except _API_ERRORS as e:
            raise _provider_error(e) from e
        for model in listed:
            if not model.name or not model.name.startswith("models/gemini-"):
                continue
            if "generateContent" not in (model.supported_actions or []):
                continue
            model_id = model.name.replace("models/", "")
            if any(marker in model_id for marker in _NON_CHAT_NAME_MARKERS):
                continue
            models.append(
                LLMModel(
                    id=model_id,
                    label=model.display_name or model_id,
                    capabilities=frozenset({"chat"}),
                    status=None,
                    free=False,
                )
            )
        return models

    async def chat_completion(
        self,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> str:
        if self._client is None:
            raise RuntimeError("Google LLM provider not configured (missing API key)")
        self._raise_if_limited(model_id)
        try:
            response = await self._client.aio.models.generate_content(
                model=model_id,
                contents=user_prompt,
                config=_generation_config(model_id, system_prompt, temperature),
            )
        except _API_ERRORS as e:
            raise self._call_error(model_id, e) from e
        return _response_text(model_id, response)

    def supports_pdf_input(self, model_id: str) -> bool:
        return True

    async def chat_completion_with_pdf(
        self,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        pdf_path: str,
        temperature: float = 0.2,
    ) -> str:
        """Upload the PDF via the Files API (no inline size limit), prompt with it, then delete it."""
        if self._client is None:
            raise RuntimeError("Google LLM provider not configured (missing API key)")
        self._raise_if_limited(model_id)
        try:
            uploaded = await self._client.aio.files.upload(file=pdf_path, config={"mime_type": "application/pdf"})
            try:
                response = await self._client.aio.models.generate_content(
                    model=model_id,
                    contents=[uploaded, user_prompt],
                    config=_generation_config(model_id, system_prompt, temperature),
                )
            finally:
                if uploaded.name:
                    try:
                        await self._client.aio.files.delete(name=uploaded.name)
                    except _API_ERRORS as e:
                        logger.warning(f"Could not delete uploaded file '{uploaded.name}': {_provider_error(e)}")
        except _API_ERRORS as e:
            raise self._call_error(model_id, e) from e
        return _response_text(model_id, response)


def _response_text(model_id: str, response: types.GenerateContentResponse) -> str:
    """The response text, logging a warning with the finish reason when it is empty."""
    if not response.text:
        finish_reason = response.candidates[0].finish_reason if response.candidates else None
        logger.warning(f"Gemini model '{model_id}' returned no text (finish reason: {finish_reason})")
    return response.text or ""
