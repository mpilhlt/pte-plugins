"""
Users' ready MPCDF endpoints as models in the editor's inference menu.

One LLMProvider per catalog preset (``mpcdf-llm-<preset>``, a stable id, so a saved model choice
works for every user). The registered provider offers nothing by itself: ``for_user()`` returns
a copy bound to the requesting user if that user has a ready endpoint of the preset, else None,
so each user only sees and uses the endpoints they started with their own MPCDF session.

``for_user()`` runs on every listing and must not use the network, so the watch loop keeps a
per-user cache of the ready endpoints (preset and model, never credentials). A bound copy takes
the current credentials from the user's manager for each request. Error messages never contain
the endpoint URL or key.
"""

import asyncio
import copy
import logging
import time
from typing import Any

import httpx
from mpcdf_llm.errors import LoginRequired, MpcdfError
from mpcdf_llm.manager import ProvisioningManager

from fastapi_app.plugin_api import LLMModel, LLMProvider, LLMProviderError, LLMProviderRegistry

from . import manager as manager_module

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 10.0
CREDENTIAL_TTL_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 600.0
PROVIDER_ID_PREFIX = "mpcdf-llm-"

# Provider ids this process has registered
_registered: set[str] = set()
# Ready endpoints per user in this process: username -> preset -> model
_ready: dict[str, dict[str, str]] = {}


def new_http_client() -> httpx.AsyncClient:
    """The HTTP client of one chat request (replaced in tests)."""
    return httpx.AsyncClient(timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS), follow_redirects=False)


def ready_presets(username: str) -> dict[str, str]:
    """The user's presets with a ready endpoint in this worker's cache, with their model."""
    return dict(_ready.get(username, {}))


class MpcdfLLMProvider(LLMProvider):
    """LLM provider for one preset; usable only as a copy bound to a user (see for_user())."""

    def __init__(self, preset: str, model: str, description: str = ""):
        self.preset = preset
        self.model = model
        self.description = description
        self.id = f"{PROVIDER_ID_PREFIX}{preset}"
        self.label = f"MPCDF: {preset}"
        self.username: str | None = None
        self._cached: Any = None
        self._cached_at = 0.0

    def info(self) -> tuple[str, str, str]:
        """What list_models() reports; a changed value means the provider must be re-registered."""
        return (self.preset, self.model, self.description)

    def for_user(self, username: str | None) -> "MpcdfLLMProvider | None":
        """A copy bound to the user if the user has a ready endpoint of this preset, else None."""
        if not username:
            return None
        model = _ready.get(username, {}).get(self.preset)
        if model is None:
            return None
        bound = copy.copy(self)
        bound.username = username
        bound.model = model
        bound._cached = None
        bound._cached_at = 0.0
        # The user runs on their own MPCDF account, as with an own API key
        bound.uses_user_key = True
        return bound

    def list_models(self) -> list[LLMModel]:
        return [
            LLMModel(
                id=self.model,
                label=f"{self.description or self.preset} ({self.model})",
                capabilities=frozenset({"chat"}),
                status=None,
                free=True,
            )
        ]

    def supports_image_input(self, model_id: str) -> bool:
        return False

    def supports_pdf_input(self, model_id: str) -> bool:
        return False

    def _gone(self) -> LLMProviderError:
        return LLMProviderError(f"MPCDF endpoint for {self.preset} is no longer running")

    def _manager(self) -> ProvisioningManager:
        manager = manager_module.existing_manager(self.username)
        if manager is None:
            raise self._gone()
        return manager

    async def _credentials(self, *, refresh: bool) -> Any:
        """The current Available of the preset (cached for CREDENTIAL_TTL_SECONDS)."""
        if not refresh and self._cached is not None and time.monotonic() - self._cached_at < CREDENTIAL_TTL_SECONDS:
            return self._cached
        self._cached = None
        manager = self._manager()
        try:
            available = await manager.available()
        except MpcdfError as e:
            raise self._gone() from e
        except Exception as e:  # raw httpx/socketio/OSError errors may name the URL
            logger.warning("Could not fetch MPCDF credentials: %s", type(e).__name__)
            raise LLMProviderError(f"Could not reach {self.label}") from None
        found = next((a for a in available if a.preset == self.preset), None)
        if found is None:
            raise self._gone()
        self._cached, self._cached_at = found, time.monotonic()
        return found

    async def _post(self, available: Any, model_id: str, messages: list[dict[str, str]], temperature: float) -> httpx.Response:
        creds = available.credentials
        async with new_http_client() as http:
            return await http.post(
                f"{creds.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {creds.api_key.get_secret_value()}"},
                json={"model": model_id, "messages": messages, "temperature": temperature, "stream": False},
            )

    async def chat_completion(
        self,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> str:
        if self.username is None:  # the registered provider is never bound to a session
            raise self._gone()
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
        response: httpx.Response | None = None
        for attempt in (0, 1):
            available = await self._credentials(refresh=attempt == 1)
            try:
                response = await self._post(available, model_id, messages, temperature)
            except httpx.TimeoutException:
                raise LLMProviderError(f"{self.label} did not answer in time") from None
            except httpx.ConnectError:
                # The request never left: safe to look again once. Other transport errors may
                # have reached the endpoint, and a retry would double the GPU work.
                if attempt == 0:
                    continue
                raise LLMProviderError(f"Could not reach {self.label}") from None
            except httpx.TransportError:
                raise LLMProviderError(f"Could not reach {self.label}") from None
            except httpx.HTTPError as e:
                raise LLMProviderError(f"Request to {self.label} failed: {type(e).__name__}") from None
            # Stale credentials (401) or an endpoint that just ended (503): look again once
            if response.status_code in (401, 503) and attempt == 0:
                continue
            break
        assert response is not None
        if response.status_code >= 400:
            raise LLMProviderError(f"{self.label} returned {response.status_code}")
        try:
            result = response.json()
        except ValueError:
            raise LLMProviderError(f"{self.label} returned an invalid response") from None
        # Activity keeps the endpoint from idling out
        try:
            await self._manager().touch(preset=self.preset)
        except Exception as e:  # a transient failure must not discard a reply that already succeeded
            logger.debug("Could not record MPCDF activity: %s", type(e).__name__)
        choices = result.get("choices") or []
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content") or ""


async def refresh_user(username: str, manager: ProvisioningManager) -> None:
    """Update the user's ready endpoints in the cache; keep them on a transient error."""
    if (await manager.session_status()).state != "ok":
        _ready.pop(username, None)  # no valid session: nothing available, and no network request
        return
    try:
        available = await manager.available()
    except LoginRequired:
        _ready.pop(username, None)
        return
    except MpcdfError as e:  # transient (timeouts, protocol): keep what is cached
        logger.warning("Could not refresh the MPCDF endpoints of a user: %s", type(e).__name__)
        return
    _ready[username] = {item.preset: item.model for item in available}


def sync_catalog() -> None:
    """Register a provider per catalog preset, unregister removed ones; other plugins' providers stay."""
    registry = LLMProviderRegistry.get_instance()
    current: dict[str, MpcdfLLMProvider] = {}
    for preset in manager_module.catalog().list():
        provider = MpcdfLLMProvider(preset.name, preset.model, preset.description or "")
        current[provider.id] = provider
    for provider_id in _registered - current.keys():
        registry.unregister(provider_id)
        _registered.discard(provider_id)
    for provider_id, provider in current.items():
        try:
            existing = registry.get_provider(provider_id) if provider_id in _registered else None
        except KeyError:
            existing = None
        if isinstance(existing, MpcdfLLMProvider) and existing.info() == provider.info():
            continue
        registry.register(provider)
        _registered.add(provider_id)


async def sync_providers() -> None:
    """One round of the watch loop: managers, catalog providers and the users' ready endpoints."""
    await manager_module.sync_users()
    sync_catalog()
    current = manager_module.managers()
    for username in list(_ready):
        if username not in current:
            _ready.pop(username, None)
    for username, manager in current.items():
        try:
            await refresh_user(username, manager)
        except Exception as e:  # one user's failure must not stop the others
            logger.warning("Could not refresh the MPCDF endpoints of a user: %s", type(e).__name__)


def unregister_all() -> None:
    """Unregister the providers this process has registered and forget the ready endpoints."""
    registry = LLMProviderRegistry.get_instance()
    for provider_id in _registered:
        registry.unregister(provider_id)
    _registered.clear()
    _ready.clear()


async def watch() -> None:
    """Keep the providers and the ready endpoints current, surviving errors."""
    while True:
        try:
            await sync_providers()
        except Exception as e:  # keep watching after an unexpected error
            logger.error("Could not update the MPCDF providers: %s", type(e).__name__)
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
