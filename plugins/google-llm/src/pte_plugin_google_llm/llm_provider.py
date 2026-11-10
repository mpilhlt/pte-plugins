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
from datetime import datetime, timedelta, timezone

import httpx
from google import genai
from google.genai import errors as genai_errors, types

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


def _retry_delay_seconds(details: object) -> float | None:
    """The `retryDelay` of a google.rpc.RetryInfo in an error response ("34s"), or None."""
    error = details.get("error") if isinstance(details, dict) else None
    entries = error.get("details") if isinstance(error, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and str(entry.get("@type", "")).endswith("google.rpc.RetryInfo"):
            match = _RETRY_DELAY_RE.fullmatch(str(entry.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    return None


def _provider_error(error: genai_errors.APIError | httpx.HTTPError) -> LLMProviderError:
    """A user-presentable LLMProviderError for a failed Gemini API call; a 429 (rate limit, exhausted quota) is an LLMRateLimitError."""
    if isinstance(error, genai_errors.APIError):
        if error.code == 429:
            delay = _retry_delay_seconds(error.details)
            available_at = datetime.now(timezone.utc) + timedelta(seconds=delay) if delay is not None else None
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
        try:
            response = await self._client.aio.models.generate_content(
                model=model_id,
                contents=user_prompt,
                config=_generation_config(model_id, system_prompt, temperature),
            )
        except _API_ERRORS as e:
            raise _provider_error(e) from e
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
            raise _provider_error(e) from e
        return _response_text(model_id, response)


def _response_text(model_id: str, response: types.GenerateContentResponse) -> str:
    """The response text, logging a warning with the finish reason when it is empty."""
    if not response.text:
        finish_reason = response.candidates[0].finish_reason if response.candidates else None
        logger.warning(f"Gemini model '{model_id}' returned no text (finish reason: {finish_reason})")
    return response.text or ""
