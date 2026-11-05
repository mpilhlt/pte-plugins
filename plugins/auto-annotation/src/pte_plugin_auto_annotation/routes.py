"""
Routes of the auto-annotation plugin, used by the model probe CLI.

  GET  /api/plugins/auto-annotation/eval-source   reference version, input version, unit path and rules
  POST /api/plugins/auto-annotation/resolve-model model name -> (provider id, model id)
  POST /api/plugins/auto-annotation/complete      one chat completion with a resolved model (503 when retrying may help)

All routes are for reviewers and admins. The completion route only relays prompts the client
built; it applies the same model access rules as the LLM Reviewer.
"""

from __future__ import annotations

import logging
import time

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from fastapi_app.lib.core.dependencies import get_file_repository, get_file_storage, require_reviewer_or_admin
from fastapi_app.lib.core.url_cache import UrlCache
from fastapi_app.lib.llm import LLMProvider, LLMProviderError, LLMProviderRegistry, get_default_model
from fastapi_app.lib.llm.model_access import ModelAccessDenied, check_model_access
from fastapi_app.lib.repository.file_repository import FileRepository
from fastapi_app.lib.secrets import MissingSecretError, secret_required_detail
from fastapi_app.lib.storage.file_storage import FileStorage
from fastapi_app.plugin_api import get_settings

from .correction import is_transient_message
from .model_spec import ModelSpecError, ProviderInfo, resolve_model_spec
from .sources import DEFAULT_VARIANT, SourceError, resolve_source

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plugins/auto-annotation", tags=["auto-annotation"])


class RuleResponse(BaseModel):
    category: str
    text: str
    url: str


class EvalSourceResponse(BaseModel):
    doc_id: str
    reference_stable_id: str
    reference_is_gold: bool
    input_stable_id: str
    input_choice: str
    variant: str | None
    unit_path: str | None
    gold_xml: str
    input_xml: str
    rules: list[RuleResponse]


class ResolveModelRequest(BaseModel):
    model: str


class ResolvedModelResponse(BaseModel):
    provider_id: str
    model_id: str


class CompleteRequest(BaseModel):
    provider_id: str
    model_id: str
    system_prompt: str
    user_prompt: str
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class CompleteResponse(BaseModel):
    text: str
    seconds: float


@router.get("/eval-source", response_model=EvalSourceResponse)
def eval_source(
    doc: str | None = Query(None, description="Document id; its gold version of the variant is the reference"),
    variant: str | None = Query(None, description=f"Variant (default: {DEFAULT_VARIANT}, or the reference's variant)"),
    reference: str | None = Query(None, description="Stable id of the reference version, instead of the document's gold version"),
    input: str | None = Query(None, description="Stable id of the version to correct; default: the first version the reference was derived from, else the document's earliest version of the variant"),
    rules: bool = Query(True, description="Resolve the rules from the reference version's editorialDecl"),
    current_user: dict = Depends(require_reviewer_or_admin),
    repo: FileRepository = Depends(get_file_repository),
    storage: FileStorage = Depends(get_file_storage),
) -> EvalSourceResponse:
    """The fixed inputs of an evaluation run."""
    cache = UrlCache(get_settings().annotation_rules_cache_dir)
    try:
        source = resolve_source(doc, variant, reference, input, current_user, repo, storage, cache, with_rules=rules)
    except SourceError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    return EvalSourceResponse(
        doc_id=source.doc_id, reference_stable_id=source.reference_stable_id, reference_is_gold=source.reference_is_gold,
        input_stable_id=source.input_stable_id, input_choice=source.input_choice, variant=source.variant, unit_path=source.unit_path,
        gold_xml=source.gold_xml, input_xml=source.input_xml,
        rules=[RuleResponse(category=c, text=t, url=u) for c, t, u in source.rules],
    )


def _provider_for_user(provider_id: str, username: str) -> LLMProvider:
    try:
        return LLMProviderRegistry.get_instance().get_provider_for_user(provider_id, username)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Unknown LLM provider {provider_id}") from e
    except MissingSecretError as e:
        raise HTTPException(status_code=428, detail=secret_required_detail(e)) from e


@router.post("/resolve-model", response_model=ResolvedModelResponse)
def resolve_model(
    body: ResolveModelRequest,
    current_user: dict = Depends(require_reviewer_or_admin),
) -> ResolvedModelResponse:
    """Resolve a model name (see model_spec.py) among the providers the user can use."""
    wanted = body.model.strip().lower()
    infos: list[ProviderInfo] = []
    for registered in LLMProviderRegistry.get_instance().list_providers(available_only=True):
        info = ProviderInfo(registered.id, registered.label)
        if not any(wanted == n or wanted.startswith(n + "/") for n in info.names()):
            infos.append(info)  # listed for the error message only; its models are not needed
            continue
        models: dict[str, str] = {}
        try:
            provider = LLMProviderRegistry.get_instance().get_provider_for_user(registered.id, current_user["username"])
            models = {m["id"]: m["label"] for m in provider.list_models()}
        except Exception as e:  # noqa: BLE001 - a provider without models can still be named with a model id
            logger.info(f"auto-annotation: cannot list models of {registered.id}: {e}")
        infos.append(ProviderInfo(registered.id, registered.label, models))
    try:
        provider_id, model_id = resolve_model_spec(body.model, infos, get_default_model())
    except ModelSpecError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    try:
        check_model_access(_provider_for_user(provider_id, current_user["username"]), model_id, current_user)
    except ModelAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return ResolvedModelResponse(provider_id=provider_id, model_id=model_id)


@router.post("/complete", response_model=CompleteResponse)
async def complete(
    body: CompleteRequest,
    current_user: dict = Depends(require_reviewer_or_admin),
) -> CompleteResponse:
    """One chat completion; the answer text and the time the provider took."""
    provider = _provider_for_user(body.provider_id, current_user["username"])
    try:
        check_model_access(provider, body.model_id, current_user)
        started = time.monotonic()
        text = await provider.chat_completion(body.model_id, body.system_prompt, body.user_prompt, body.temperature)
    except ModelAccessDenied as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except (LLMProviderError, requests.RequestException) as e:
        logger.warning(f"auto-annotation: provider '{body.provider_id}' failed: {e}")
        # 503 tells the client that retrying may help (overload, rate limit, timeout)
        transient = isinstance(e, (requests.ConnectionError, requests.Timeout)) or is_transient_message(str(e))
        raise HTTPException(status_code=503 if transient else 502, detail=f"LLM provider request failed: {e}") from e
    return CompleteResponse(text=text, seconds=time.monotonic() - started)
