"""
Routes of the auto-annotation plugin, used by the model probe CLI and the annotation CLI.

  GET  /api/plugins/auto-annotation/eval-source   reference version, input version, unit path and rules
  POST /api/plugins/auto-annotation/resolve-model model name -> (provider id, model id)
  POST /api/plugins/auto-annotation/complete      one chat completion with a resolved model (503 when retrying may help)
  POST /api/plugins/auto-annotation/annotate      annotate one version with the pipeline (result returned, not saved)
  GET  /api/plugins/auto-annotation/targets       the version of each document of a collection to annotate

All routes are for reviewers and admins. The completion route only relays prompts the client
built; it applies the same model access rules as the LLM Reviewer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from fastapi_app.lib.core.dependencies import get_file_repository, get_file_storage, require_reviewer_or_admin
from fastapi_app.lib.core.url_cache import UrlCache
from fastapi_app.lib.llm import LLMProviderError
from fastapi_app.lib.repository.file_repository import FileRepository
from fastapi_app.lib.secrets import secret_required_detail
from fastapi_app.lib.storage.file_storage import FileStorage
from fastapi_app.plugin_api import get_settings

from .annotation import annotate_file, find_targets
from .batching import BatchSpec
from .correction import RetryPolicy, is_transient_message
from .pipeline import PipelineConfig
from .prompts import OutputMode
from .models import ModelResolutionError, ensure_model_access, provider_for_user, resolve_model_for_user
from .sources import SourceError, resolve_source

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
    input_is_extraction: bool
    variant: str | None
    unit_path: str | None
    gold_xml: str
    input_xml: str
    rules: list[RuleResponse]
    segmenting: bool = False


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
    variant: str | None = Query(None, description="Variant of the gold version (required with doc); default with reference: the reference's variant"),
    reference: str | None = Query(None, description="Stable id of the reference version, instead of the document's gold version"),
    input: str | None = Query(None, description="Stable id of the version to correct; default: the extraction the reference was derived from, else the document's earliest extraction of the variant"),
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
        input_stable_id=source.input_stable_id, input_choice=source.input_choice, input_is_extraction=source.input_is_extraction, variant=source.variant, unit_path=source.unit_path,
        gold_xml=source.gold_xml, input_xml=source.input_xml,
        rules=[RuleResponse(category=c, text=t, url=u) for c, t, u in source.rules], segmenting=source.segmenting,
    )


def _http_error(e: ModelResolutionError) -> HTTPException:
    if e.missing_secret is not None:
        return HTTPException(status_code=428, detail=secret_required_detail(e.missing_secret))
    return HTTPException(status_code=e.status, detail=str(e))


@router.post("/resolve-model", response_model=ResolvedModelResponse)
def resolve_model(
    body: ResolveModelRequest,
    current_user: dict = Depends(require_reviewer_or_admin),
) -> ResolvedModelResponse:
    """Resolve a model name (see model_spec.py) among the providers the user can use."""
    try:
        provider, model_id = resolve_model_for_user(body.model, current_user)
    except ModelResolutionError as e:
        raise _http_error(e) from e
    return ResolvedModelResponse(provider_id=provider.id, model_id=model_id)


@router.post("/complete", response_model=CompleteResponse)
async def complete(
    body: CompleteRequest,
    current_user: dict = Depends(require_reviewer_or_admin),
) -> CompleteResponse:
    """One chat completion; the answer text and the time the provider took."""
    try:
        provider = provider_for_user(body.provider_id, current_user["username"])
        ensure_model_access(provider, body.model_id, current_user)
    except ModelResolutionError as e:
        raise _http_error(e) from e
    try:
        started = time.monotonic()
        text = await provider.chat_completion(body.model_id, body.system_prompt, body.user_prompt, body.temperature)
    except (LLMProviderError, requests.RequestException) as e:
        logger.warning(f"auto-annotation: provider '{body.provider_id}' failed: {e}")
        # 503 tells the client that retrying may help (overload, rate limit, timeout)
        transient = isinstance(e, (requests.ConnectionError, requests.Timeout)) or is_transient_message(str(e))
        raise HTTPException(status_code=503 if transient else 502, detail=f"LLM provider request failed: {e}") from e
    return CompleteResponse(text=text, seconds=time.monotonic() - started)


class AnnotateRequest(BaseModel):
    stable_id: str
    model_a: str
    model_b: str
    arbiter: str | None = None
    batch: str = "units:16"
    arbiter_batch: str = "units:8"
    mode: OutputMode = "all"
    split_on_failure: float | None = Field(default=0.25, ge=0.0, le=1.0)
    concurrency: int = Field(default=1, ge=1, le=16)
    retries: int = Field(default=4, ge=0, le=10)
    marks: bool = True
    status: str = "extraction"
    label: str | None = "Auto-annotation"
    include_raw: bool = False


class AnnotateResponse(BaseModel):
    input_stable_id: str
    doc_id: str
    variant: str | None
    xml: str
    models: dict[str, str]
    counts: dict[str, int]
    log: dict[str, object]


@router.post("/annotate", response_model=AnnotateResponse)
async def annotate(
    body: AnnotateRequest,
    current_user: dict = Depends(require_reviewer_or_admin),
    repo: FileRepository = Depends(get_file_repository),
    storage: FileStorage = Depends(get_file_storage),
) -> AnnotateResponse:
    """
    Annotate a version with two models and an arbiter (see pipeline.py) and return the new document.
    Nothing is saved: the client saves the XML as a new version of `stable_id`.
    """
    try:
        config = PipelineConfig(
            spec=BatchSpec.parse(body.batch), mode=body.mode, arbiter_spec=BatchSpec.parse(body.arbiter_batch),
            split_on_failure=body.split_on_failure, concurrency=body.concurrency, retry=RetryPolicy(retries=body.retries),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    cache = UrlCache(get_settings().annotation_rules_cache_dir)
    try:
        outcome = await annotate_file(
            body.stable_id, current_user, repo, storage, cache, body.model_a, body.model_b, body.arbiter, config,
            marks=body.marks, status=body.status, label=body.label,
        )
    except SourceError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    except ModelResolutionError as e:
        raise _http_error(e) from e
    return AnnotateResponse(
        input_stable_id=outcome.input_stable_id, doc_id=outcome.doc_id, variant=outcome.variant, xml=outcome.xml,
        models=outcome.models, counts=outcome.counts, log=outcome.log(body.include_raw),
    )


class TargetResponse(BaseModel):
    doc_id: str
    input_stable_id: str | None
    state: str
    annotated_stable_id: str | None = None


@router.get("/targets", response_model=list[TargetResponse])
def targets(
    collection: str | None = Query(None, description="Collection id"),
    doc: list[str] | None = Query(None, description="Document ids (repeatable), instead of a collection"),
    variant: str = Query(..., description="Variant to annotate"),
    current_user: dict = Depends(require_reviewer_or_admin),
    repo: FileRepository = Depends(get_file_repository),
    storage: FileStorage = Depends(get_file_storage),
) -> list[TargetResponse]:
    """The version of each document the pipeline would annotate, and whether it is skipped."""
    if not collection and not doc:
        raise HTTPException(status_code=422, detail="Pass a collection or document ids")
    return [TargetResponse(**asdict(t)) for t in find_targets(repo, storage, current_user, variant, collection, doc)]
