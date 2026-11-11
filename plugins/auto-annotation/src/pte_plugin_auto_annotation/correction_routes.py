"""
Routes of the "Correct annotations in new version" action (frontend: extensions/auto-annotation.js).

The client drives the correction one batch at a time, so that it can show the progress and stop
between batches, and saves the result as a new version through the editor's save API:

  POST /api/plugins/auto-annotation/correct/start      lock the version for the job, plan the batches
  POST /api/plugins/auto-annotation/correct/batch      correct the units of one batch with the model
  POST /api/plugins/auto-annotation/correct/heartbeat  keep the job's lock while a batch takes long
  POST /api/plugins/auto-annotation/correct/finish     the document with the corrections (not saved)
  POST /api/plugins/auto-annotation/correct/release    release the job's lock

While a job runs, the version is locked under the job's id (`auto-annotation:<job id>`) instead of
the user's session, so that it opens read-only everywhere, also in the user's own tab. The lock
expires like any other lock if the client stops sending requests (LOCK_TIMEOUT_SECONDS). Once the
new version is saved, the source is frozen by the editor since a version was derived from it.

For annotators, reviewers and admins.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from fastapi_app.lib.core.dependencies import (
    get_file_repository,
    get_file_storage,
    get_session_manager,
    get_sse_service,
    require_authenticated_user,
    require_session_id,
)
from fastapi_app.lib.core.locking import acquire_lock, release_lock
from fastapi_app.lib.core.sessions import SessionManager
from fastapi_app.lib.core.url_cache import UrlCache
from fastapi_app.lib.models.models import FileMetadata
from fastapi_app.lib.repository.file_repository import FileRepository
from fastapi_app.lib.secrets import secret_required_detail
from fastapi_app.lib.sse.sse_service import SSEService
from fastapi_app.lib.sse.sse_utils import broadcast_to_other_sessions
from fastapi_app.lib.storage.file_storage import FileStorage
from fastapi_app.plugin_api import get_settings

from .annotation import load_input
from .batching import BatchSpec
from .correction import ProviderChatClient, RetryPolicy
from .document import write_corrections
from .models import ModelResolutionError, ensure_model_access, provider_for_user
from .single import CorrectionSummary, check_corrections, correct_batch, plan_batches
from .sources import SourceError, gather_rules, is_segmenting_variant, review_unit_path
from .units import Unit, document_units

logger = logging.getLogger(__name__)

# Included by routes.router, which adds /api/plugins/auto-annotation
router = APIRouter(prefix="/correct")

LOCK_PREFIX = "auto-annotation:"
ALLOWED_ROLES = ("annotator", "reviewer", "admin", "*")


def require_annotator(user: dict[str, object] = Depends(require_authenticated_user)) -> dict[str, object]:
    """The user, if they may create versions (annotator, reviewer or admin)."""
    roles = user.get("roles")
    if not isinstance(roles, list) or not any(role in ALLOWED_ROLES for role in roles):
        raise HTTPException(status_code=403, detail="Annotator or reviewer role required")
    return user


def job_holder(job_id: str) -> str:
    """The id under which a job holds the lock of its source version."""
    return f"{LOCK_PREFIX}{job_id}"


@dataclass
class CorrectionSource:
    """The version to correct, its review units and the rules."""

    file: FileMetadata
    xml: str
    unit_path: str | None
    units: list[Unit]
    rules: list[tuple[str, str, str]]
    segmenting: bool


def load_source(stable_id: str, user: dict[str, object], repo: FileRepository, storage: FileStorage, with_rules: bool = True) -> CorrectionSource:
    """
    Raises:
        SourceError: the version is missing, not readable, a gold version, has no review units, or no rules.
    """
    file, xml = load_input(repo, storage, stable_id, user)
    unit_path = review_unit_path(file.variant)
    try:
        units = document_units(xml, unit_path)
    except ValueError as e:
        raise SourceError(f"{stable_id}: {e}") from e
    if not units:
        raise SourceError("The document has no units to correct" + (f" ({unit_path})" if unit_path else ""))
    rules = gather_rules(xml, UrlCache(get_settings().annotation_rules_cache_dir)) if with_rules else []
    return CorrectionSource(file, xml, unit_path, units, rules, is_segmenting_variant(file.variant))


def _source(stable_id: str, user: dict[str, object], repo: FileRepository, storage: FileStorage, with_rules: bool = True) -> CorrectionSource:
    try:
        return load_source(stable_id, user, repo, storage, with_rules)
    except SourceError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e


def _keep_lock(stable_id: str, job_id: str) -> None:
    """Refresh the job's lock; 409 if the job no longer holds it."""
    if not acquire_lock(stable_id, job_holder(job_id), get_settings().db_dir, logger):
        raise HTTPException(status_code=409, detail="The document is no longer locked for this correction; it may have been opened for editing in the meantime")


def _model_http_error(e: ModelResolutionError) -> HTTPException:
    if e.missing_secret is not None:
        return HTTPException(status_code=428, detail=secret_required_detail(e.missing_secret))
    return HTTPException(status_code=e.status, detail=str(e))


class StartRequest(BaseModel):
    stable_id: str
    provider_id: str
    model_id: str
    batch: str = "units:16"


class StartResponse(BaseModel):
    job_id: str
    doc_id: str
    variant: str | None
    unit_count: int
    # Unit indices of each batch, to be sent to /batch in turn
    batches: list[list[int]]


class JobRequest(BaseModel):
    job_id: str
    stable_id: str


class BatchRequest(JobRequest):
    provider_id: str
    model_id: str
    units: list[int] = Field(min_length=1, max_length=256)
    retries: int = Field(default=4, ge=0, le=10)


class UnitCorrectionResponse(BaseModel):
    index: int
    status: str
    xml: str | None


class BatchResponse(BaseModel):
    units: list[UnitCorrectionResponse]
    error: str | None
    calls: int
    retries: int
    seconds: float


class CorrectionItem(BaseModel):
    index: int
    xml: str


class FinishRequest(JobRequest):
    # The model, recorded in the revision change (see document.model_name_xml)
    provider_id: str
    model_id: str
    model_label: str | None = None
    corrections: list[CorrectionItem]
    processed: int = Field(ge=0)
    invalid: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    seconds: float = Field(default=0.0, ge=0)
    status: str = "extraction"
    label: str | None = "Auto-annotation"


class FinishResponse(BaseModel):
    xml: str
    changed: int


class StatusResponse(BaseModel):
    status: str


@router.post("/start", response_model=StartResponse)
def start(
    body: StartRequest,
    current_user: dict[str, object] = Depends(require_annotator),
    session_id: str = Depends(require_session_id),
    repo: FileRepository = Depends(get_file_repository),
    storage: FileStorage = Depends(get_file_storage),
) -> StartResponse:
    """
    Check the version and the model, take over the version's lock from the user's session (423 if
    another session holds it) and return the batches.
    """
    try:
        spec = BatchSpec.parse(body.batch)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    try:
        provider = provider_for_user(body.provider_id, str(current_user["username"]))
        ensure_model_access(provider, body.model_id, current_user)
    except ModelResolutionError as e:
        raise _model_http_error(e) from e
    source = _source(body.stable_id, current_user, repo, storage)
    stable_id = source.file.stable_id
    db_dir = get_settings().db_dir
    job_id = uuid.uuid4().hex
    # the user's own lock, if their session holds it; another session's lock stays and makes the acquisition fail
    release_lock(stable_id, session_id, db_dir, logger)
    if not acquire_lock(stable_id, job_holder(job_id), db_dir, logger):
        raise HTTPException(status_code=423, detail="The document is being edited by another user")
    logger.info(f"auto-annotation: correction {job_id} of {stable_id} started by {current_user['username']} with {body.provider_id}/{body.model_id}")
    return StartResponse(
        job_id=job_id, doc_id=source.file.doc_id, variant=source.file.variant, unit_count=len(source.units),
        batches=plan_batches(source.units, spec),
    )


@router.post("/batch", response_model=BatchResponse)
async def batch(
    body: BatchRequest,
    current_user: dict[str, object] = Depends(require_annotator),
    repo: FileRepository = Depends(get_file_repository),
    storage: FileStorage = Depends(get_file_storage),
) -> BatchResponse:
    """Correct the given units with the model. A failed request is reported in `error`, not as an HTTP error."""
    _keep_lock(body.stable_id, body.job_id)
    try:
        provider = provider_for_user(body.provider_id, str(current_user["username"]))
        ensure_model_access(provider, body.model_id, current_user)
    except ModelResolutionError as e:
        raise _model_http_error(e) from e
    source = _source(body.stable_id, current_user, repo, storage)
    by_index = {unit.index: unit for unit in source.units}
    unknown = [i for i in body.units if i not in by_index]
    if unknown:
        raise HTTPException(status_code=422, detail=f"No units with the indices {unknown}")
    result = await correct_batch(
        ProviderChatClient(provider, body.model_id), [(c, t) for c, t, _ in source.rules], [by_index[i] for i in body.units],
        segmenting=source.segmenting, retry=RetryPolicy(retries=body.retries),
    )
    _keep_lock(body.stable_id, body.job_id)
    return BatchResponse(
        units=[UnitCorrectionResponse(index=u.index, status=u.status.value, xml=u.xml) for u in result.units],
        error=result.error, calls=result.calls, retries=result.retries, seconds=result.seconds,
    )


@router.post("/heartbeat", response_model=StatusResponse)
def heartbeat(body: JobRequest, current_user: dict[str, object] = Depends(require_annotator)) -> StatusResponse:
    """Refresh the job's lock (409 if it was lost)."""
    _keep_lock(body.stable_id, body.job_id)
    return StatusResponse(status="locked")


@router.post("/finish", response_model=FinishResponse)
def finish(
    body: FinishRequest,
    current_user: dict[str, object] = Depends(require_annotator),
    repo: FileRepository = Depends(get_file_repository),
    storage: FileStorage = Depends(get_file_storage),
) -> FinishResponse:
    """
    The version with the corrections and a revision change by `#auto-annotation`; the client saves
    it as a new version of `stable_id`. The lock is kept until /release.
    """
    _keep_lock(body.stable_id, body.job_id)
    source = _source(body.stable_id, current_user, repo, storage, with_rules=False)
    corrections = {item.index: item.xml for item in body.corrections}
    problems = check_corrections(source.units, corrections, source.segmenting)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems[:5]))
    summary = CorrectionSummary(
        units=len(source.units), processed=min(body.processed, len(source.units)), changed=len(corrections),
        invalid=body.invalid, errors=body.errors,
    )
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        xml = write_corrections(
            source.xml, source.unit_path, corrections, body.provider_id, body.model_id, summary, when, body.seconds,
            segmenting=source.segmenting, status=body.status, label=body.label, model_label=body.model_label,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.info(f"auto-annotation: correction {body.job_id} of {body.stable_id} finished ({summary})")
    return FinishResponse(xml=xml, changed=len(corrections))


@router.post("/release", response_model=StatusResponse)
def release(
    body: JobRequest,
    current_user: dict[str, object] = Depends(require_annotator),
    session_id: str = Depends(require_session_id),
    sse_service: SSEService = Depends(get_sse_service),
    session_manager: SessionManager = Depends(get_session_manager),
) -> StatusResponse:
    """Release the job's lock and tell the other sessions, as releasing a lock in the editor does."""
    result = release_lock(body.stable_id, job_holder(body.job_id), get_settings().db_dir, logger)
    if result.get("status") != "success":
        return StatusResponse(status="not-locked")
    broadcast_to_other_sessions(
        sse_service=sse_service, session_manager=session_manager, current_session_id=session_id,
        event_type="lockReleased", data={"stable_id": body.stable_id}, logger=logger,
    )
    return StatusResponse(status="released")
