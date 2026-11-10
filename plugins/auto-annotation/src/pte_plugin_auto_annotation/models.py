"""
Server side: resolving a model name to a registered provider the user may use (see model_spec.py
for the accepted names).
"""

from __future__ import annotations

import logging

from fastapi_app.lib.llm import LLMProvider, LLMProviderRegistry, get_default_model
from fastapi_app.lib.llm.model_access import ModelAccessDenied, check_model_access
from fastapi_app.lib.secrets import MissingSecretError

from .model_spec import ModelSpecError, ProviderInfo, resolve_model_spec

logger = logging.getLogger(__name__)


class ModelResolutionError(Exception):
    """A model name cannot be used; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int, missing_secret: MissingSecretError | None = None):
        super().__init__(message)
        self.status = status
        self.missing_secret = missing_secret


def provider_for_user(provider_id: str, username: str) -> LLMProvider:
    """
    The provider with the user's key where it needs one.

    Raises:
        ModelResolutionError: unknown provider (404), or the user's key is missing (428).
    """
    try:
        return LLMProviderRegistry.get_instance().get_provider_for_user(provider_id, username)
    except KeyError as e:
        raise ModelResolutionError(f"Unknown LLM provider {provider_id}", 404) from e
    except MissingSecretError as e:
        raise ModelResolutionError(str(e), 428, e) from e


def ensure_model_access(provider: LLMProvider, model_id: str, user: dict[str, object]) -> None:
    """
    Raises:
        ModelResolutionError: the user may not use the model (403).
    """
    try:
        check_model_access(provider, model_id, user)
    except ModelAccessDenied as e:
        raise ModelResolutionError(str(e), 403) from e


def resolve_model_for_user(spec: str, user: dict[str, object]) -> tuple[LLMProvider, str]:
    """
    (provider, model id) for a model name, after checking that the user may use the model.

    Raises:
        ModelResolutionError: no or an ambiguous match (404), missing key (428), no access (403).
    """
    username = str(user["username"])
    wanted = spec.strip().lower()
    infos: list[ProviderInfo] = []
    for registered in LLMProviderRegistry.get_instance().list_providers(available_only=True):
        info = ProviderInfo(registered.id, registered.label)
        if not any(wanted == n or wanted.startswith(n + "/") for n in info.names()):
            infos.append(info)  # listed for the error message only; its models are not needed
            continue
        models: dict[str, str] = {}
        try:
            provider = LLMProviderRegistry.get_instance().get_provider_for_user(registered.id, username)
            models = {m["id"]: m["label"] for m in provider.list_models()}
        except Exception as e:  # noqa: BLE001 - a provider without models can still be named with a model id
            logger.info(f"auto-annotation: cannot list models of {registered.id}: {e}")
        infos.append(ProviderInfo(registered.id, registered.label, models))
    try:
        provider_id, model_id = resolve_model_spec(spec, infos, get_default_model())
    except ModelSpecError as e:
        raise ModelResolutionError(str(e), 404) from e
    provider = provider_for_user(provider_id, username)
    ensure_model_access(provider, model_id, user)
    return provider, model_id
