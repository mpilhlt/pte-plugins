"""
LLM provider connector for an OpenAI-compatible inference endpoint (Ollama, vLLM,
LM Studio, llama.cpp server, LiteLLM, OpenRouter, ...).

Implements LLMProvider directly rather than through the core's
OpenAICompatibleProvider, because it needs an optional API key (Ollama has none),
image input, a configurable timeout, and the URL check of url_policy.py before every
request, with redirects disabled.
"""

import asyncio
import base64
import logging

import requests

from fastapi_app.lib.extraction.http_utils import get_retry_session
from fastapi_app.lib.secrets import SecretsError
from fastapi_app.plugin_api import LLMImage, LLMModel, LLMProvider, LLMProviderError, get_completion_session, post_completion

from .keys import get_shared_key
from .store import Endpoint
from .url_policy import EndpointURLError, check_url

logger = logging.getLogger(__name__)

LIST_MODELS_TIMEOUT_SECONDS = 10


class EndpointLLMProvider(LLMProvider):
    """LLM provider for one configured endpoint."""

    def __init__(self, endpoint: Endpoint, api_key: str | None = None):
        """
        Args:
            endpoint: The endpoint configuration
            api_key: A user's own key (key mode "user", see with_api_key()). The shared key
                (key mode "shared") is read from the secret store on every request, so that
                a changed key takes effect in all worker processes.
        """
        self.endpoint = endpoint
        self.id = endpoint.provider_id
        self.label = endpoint.name
        self.secret_service = endpoint.secret_service if endpoint.key_mode == "user" else None
        self._api_key = api_key

    def has_shared_key(self) -> bool:
        # Endpoints with per-user keys need the requesting user's key, all others are ready to use
        return self.endpoint.key_mode != "user" or bool(self._api_key)

    def with_api_key(self, api_key: str) -> "EndpointLLMProvider":
        return EndpointLLMProvider(self.endpoint, api_key)

    def _key(self) -> str | None:
        """The API key the requests send: the user's, or the endpoint's shared key."""
        if self.endpoint.key_mode == "shared":
            try:
                return get_shared_key(self.endpoint)
            except SecretsError as e:
                raise LLMProviderError(f"{self.label}: {e}") from e
        return self._api_key

    def _headers(self, api_key: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        api_key = api_key if api_key is not None else self._key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _url(self, path: str) -> str:
        url = f"{self.endpoint.base_url}/{path}"
        try:
            check_url(url)
        except EndpointURLError as e:
            raise LLMProviderError(f"{self.label}: {e}") from e
        return url

    def _error(self, error: requests.RequestException) -> LLMProviderError:
        """A user-presentable error for a failed request."""
        response = error.response if isinstance(error, requests.HTTPError) else None
        if response is not None:
            detail = response.text[:300].strip()
            return LLMProviderError(f"{self.label} returned {response.status_code}: {detail}")
        if isinstance(error, requests.Timeout):
            return LLMProviderError(f"{self.label} did not answer within {self.endpoint.timeout} seconds")
        return LLMProviderError(f"Could not reach {self.label} at {self.endpoint.base_url}: {type(error).__name__}")

    @staticmethod
    def _check_redirect(response: requests.Response) -> None:
        if response.is_redirect:
            raise LLMProviderError(f"The endpoint redirected to {response.headers.get('location', '?')}; enter the target URL as base URL instead")

    def fetch_models(self) -> list[str]:
        """The ids of the models listed by ``GET {base_url}/models``."""
        # No retries: a provider listing must not stall the model picker when the endpoint is down
        session = get_retry_session(retries=0)
        try:
            response = session.get(self._url("models"), headers=self._headers(), timeout=LIST_MODELS_TIMEOUT_SECONDS, allow_redirects=False)
            self._check_redirect(response)
            response.raise_for_status()
            entries = response.json().get("data", [])
        except requests.RequestException as e:
            raise self._error(e) from e
        except (ValueError, AttributeError) as e:
            raise LLMProviderError(f"{self.label} returned an invalid model list") from e
        return sorted(str(entry["id"]) for entry in entries if isinstance(entry, dict) and "id" in entry)

    def list_models(self) -> list[LLMModel]:
        """The configured models, or those listed by the endpoint."""
        capabilities = frozenset({"chat", "vision"} if self.endpoint.vision else {"chat"})
        ids = list(self.endpoint.models) or self.fetch_models()
        return [
            LLMModel(id=model_id, label=model_id, capabilities=capabilities, status=None, free=self.endpoint.free)
            for model_id in ids
        ]

    async def chat_completion(
        self,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> str:
        return await asyncio.to_thread(self._chat_completion_sync, model_id, system_prompt, user_prompt, temperature)

    def supports_image_input(self, model_id: str) -> bool:
        return self.endpoint.vision

    async def chat_completion_with_images(
        self,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        images: list[LLMImage],
        temperature: float = 0.2,
    ) -> str:
        """Send the images as base64 data URLs in `image_url` content parts, in order, ahead of the prompt text."""
        content: list[dict[str, object]] = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image['media_type']};base64,{base64.b64encode(image['data']).decode('ascii')}"},
            }
            for image in images
        ]
        content.append({"type": "text", "text": user_prompt})
        return await asyncio.to_thread(self._chat_completion_sync, model_id, system_prompt, content, temperature)

    def _chat_completion_sync(
        self,
        model_id: str,
        system_prompt: str,
        user_content: str | list[dict[str, object]],
        temperature: float,
    ) -> str:
        # A read timeout is not retried: a local model that is too slow would only be asked again.
        # A 429 is handled by post_completion, which raises LLMRateLimitError.
        session = get_completion_session(retries=2, backoff_factor=2.0, read_retries=0)
        api_key = self._key()
        data = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "stream": False,
        }
        try:
            response = post_completion(
                session, self._url("chat/completions"), endpoint=self.endpoint.base_url, api_key=api_key, model_id=model_id,
                headers=self._headers(api_key), json=data, timeout=(10, self.endpoint.timeout), allow_redirects=False,
            )
            self._check_redirect(response)
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as e:
            raise self._error(e) from e
        except ValueError as e:
            raise LLMProviderError(f"{self.label} returned an invalid response") from e
        choices = result.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content") or ""

