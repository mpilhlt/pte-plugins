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

With `source_stable_id`, the same routes transfer the annotation decisions of that version (another
version of the same document and variant, e.g. the gold version from an older text layer) to the
version (transfer.py): units whose text is the same are copied, the others go to the model with the
corresponding part of the source, units without a counterpart are kept. The plan is computed again
by every request. With `marks`, the model also reports doubts, and /finish returns them as not-OK
marks (fingerprints of the new version) for the client to record. The result gets the status of the
source's last revision change.

  GET  /api/plugins/auto-annotation/correct/transfer-preview  the counts of a transfer, without a job

Zone documents (see zones.py) are corrected by page: the units are the pages of the document's
line layout, a unit's correction is its label changes in the line form, and /finish applies the
changes of all pages at once. Without the layout, /start refuses with the reason.

For annotators, reviewers and admins.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
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
from fastapi_app.lib.permissions.access_control import check_file_access
from fastapi_app.lib.utils.tei_utils import extract_last_revision_status
from fastapi_app.lib.repository.file_repository import FileRepository
from fastapi_app.lib.secrets import secret_required_detail
from fastapi_app.lib.sse.sse_service import SSEService
from fastapi_app.lib.sse.sse_utils import broadcast_to_other_sessions
from fastapi_app.lib.storage.file_storage import FileStorage
from fastapi_app.plugin_api import get_settings

from .annotation import load_input
from .batching import BatchSpec
from .correction import ProviderChatClient, RetryPolicy
from .document import transfer_description_xml, write_corrections, write_transfer, write_zone_corrections
from .models import ModelResolutionError, ensure_model_access, provider_for_user
from .single import CorrectionSummary, check_corrections, correct_batch, correct_page_batch, plan_batches
from .sources import SourceError, gather_rules, is_segmenting_variant, read_tei, review_unit_path, tei_file, zone_layout
from .transfer import (
    CHANGE_LABEL,
    COPIED,
    DEFAULT_DOUBT_PAGE_BATCH,
    DEFAULT_DOUBT_UNIT_BATCH,
    DEFAULT_UNIT_BATCH,
    MODEL,
    TransferError,
    TransferPlan,
    TransferSummary,
    check_coverage,
    check_transfer_results,
    consecutive_batches,
    doubt_marks,
    doubt_page_batch,
    doubt_unit_batch,
    merge_corrections,
    new_unit_positions,
    plan_unit_transfer,
    plan_zone_transfer,
    transfer_page_batch,
    transfer_unit_batch,
)
from .transfer import DEFAULT_PAGE_BATCH as DEFAULT_TRANSFER_PAGE_BATCH
from .units import Unit, document_units
from .zones import DEFAULT_PAGE_BATCH, ZoneDoc, apply_page_changes, read_zone_doc
from .zones import zone_lines as core_zone_lines

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
    """The version to correct, its review units and the rules; for zone documents, its pages as units."""

    file: FileMetadata
    xml: str
    unit_path: str | None
    units: list[Unit]
    rules: list[tuple[str, str, str]]
    segmenting: bool
    zone: ZoneDoc | None = None


def load_source(stable_id: str, user: dict[str, object], repo: FileRepository, storage: FileStorage, with_rules: bool = True) -> CorrectionSource:
    """
    Raises:
        SourceError: the version is missing, not readable, a gold version, has no review units, or no
            rules; a zone document without layout.
    """
    file, xml = load_input(repo, storage, stable_id, user)
    unit_path = review_unit_path(file.variant)
    layout = zone_layout([xml], file.doc_id, file.variant)
    zone: ZoneDoc | None = None
    try:
        if layout is not None:
            zone = read_zone_doc(xml, layout)
            units = zone.units
        else:
            units = document_units(xml, unit_path)
    except ValueError as e:
        raise SourceError(f"{stable_id}: {e}") from e
    if not units:
        raise SourceError("The document has no units to correct" + (f" ({unit_path})" if unit_path else ""))
    rules = gather_rules(xml, UrlCache(get_settings().annotation_rules_cache_dir)) if with_rules else []
    return CorrectionSource(file, xml, unit_path, units, rules, is_segmenting_variant(file.variant), zone)


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


def load_transfer_source(
    source_stable_id: str, target: CorrectionSource, user: dict[str, object], repo: FileRepository, storage: FileStorage
) -> tuple[FileMetadata, str]:
    """
    The version whose annotation decisions are transferred to `target`, and its content.

    Raises:
        SourceError: missing, not readable, the target itself, or another document or variant.
    """
    source = tei_file(repo, source_stable_id)
    if not check_file_access(source, user, "read"):
        raise SourceError(f"No read access to {source_stable_id}", 403)
    if source.stable_id == target.file.stable_id:
        raise SourceError("The annotation cannot be transferred from the version itself")
    if source.doc_id != target.file.doc_id or source.variant != target.file.variant:
        raise SourceError("The annotation can only be transferred from a version of the same document and variant")
    return source, read_tei(storage, source)


def plan_transfer(target: CorrectionSource, source_xml: str) -> TransferPlan:
    """
    The plan of a transfer to `target`.

    Raises:
        SourceError: the source cannot be read, or too little of its text is found in the target.
    """
    try:
        plan = plan_zone_transfer(source_xml, target.zone) if target.zone is not None else plan_unit_transfer(source_xml, target.xml, target.unit_path)
        check_coverage(plan)
    except ValueError as e:  # includes TransferError
        raise SourceError(str(e)) from e
    return plan


def _transfer(source_stable_id: str, target: CorrectionSource, user: dict[str, object], repo: FileRepository, storage: FileStorage) -> tuple[FileMetadata, str, TransferPlan]:
    try:
        file, xml = load_transfer_source(source_stable_id, target, user, repo, storage)
        return file, xml, plan_transfer(target, xml)
    except SourceError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e


def _new_fingerprints(target: CorrectionSource, xml: str) -> list[str]:
    """The fingerprints of the units (pages) of a new version of `target`."""
    if target.zone is not None:
        line_pages = [page.number - 1 for page in target.zone.pages for _ in page.lines]
        doc = core_zone_lines.read_zone_document(xml, target.zone.labels)
        return [page.fingerprint for page in core_zone_lines.pages_of(doc, line_pages)]
    return [unit.fingerprint for unit in document_units(xml, target.unit_path)]


class TransferCounts(BaseModel):
    copied: int
    model: int
    unmatched: int
    # share of the source's text found in the target
    coverage: float


def _counts(plan: TransferPlan) -> TransferCounts:
    counts = plan.counts()
    return TransferCounts(copied=counts["copied"], model=counts["model"], unmatched=counts["unmatched"], coverage=round(plan.coverage, 3))


class StartRequest(BaseModel):
    stable_id: str
    provider_id: str
    model_id: str
    # Default: units:16, for zone documents pages:4 (transfer: units:8, pages:2)
    batch: str | None = None
    # Transfer the annotation decisions of this version instead of correcting
    source_stable_id: str | None = None
    # Transfer: let the model report doubts, which become not-OK marks
    marks: bool = False


class StartResponse(BaseModel):
    job_id: str
    doc_id: str
    variant: str | None
    unit_count: int
    # Unit indices of each batch, to be sent to /batch in turn
    batches: list[list[int]]
    # What the units are: "units", or "pages" for zone documents
    unit_name: str = "units"
    # Transfer: units per category; the batches hold the units for the model only
    transfer: TransferCounts | None = None
    # Transfer with marks: batches of copied units to check for doubts (mode "doubts")
    doubt_batches: list[list[int]] = []


class JobRequest(BaseModel):
    job_id: str
    stable_id: str


class BatchRequest(JobRequest):
    provider_id: str
    model_id: str
    units: list[int] = Field(min_length=1, max_length=256)
    retries: int = Field(default=4, ge=0, le=10)
    source_stable_id: str | None = None
    marks: bool = False
    # Transfer: "transfer" the units, or check copied units for "doubts"
    mode: Literal["transfer", "doubts"] = "transfer"


class UnitCorrectionResponse(BaseModel):
    index: int
    status: str
    xml: str | None
    # Transfer with marks: why the model doubts the earlier decision
    doubt: str | None = None


class BatchResponse(BaseModel):
    units: list[UnitCorrectionResponse]
    error: str | None
    calls: int
    retries: int
    seconds: float


class CorrectionItem(BaseModel):
    index: int
    xml: str


class DoubtItem(BaseModel):
    index: int
    comment: str


class MarkItem(BaseModel):
    fingerprint: str
    comment: str


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
    source_stable_id: str | None = None
    doubts: list[DoubtItem] = []


class FinishResponse(BaseModel):
    xml: str
    changed: int
    # Transfer: not-OK marks for the doubted units of the new version
    marks: list[MarkItem] = []


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
        provider = provider_for_user(body.provider_id, str(current_user["username"]))
        ensure_model_access(provider, body.model_id, current_user)
    except ModelResolutionError as e:
        raise _model_http_error(e) from e
    source = _source(body.stable_id, current_user, repo, storage)
    plan = _transfer(body.source_stable_id, source, current_user, repo, storage)[2] if body.source_stable_id else None
    if plan is not None:
        default_batch = DEFAULT_TRANSFER_PAGE_BATCH if source.zone else DEFAULT_UNIT_BATCH
    else:
        default_batch = DEFAULT_PAGE_BATCH if source.zone else "units:16"
    try:
        spec = BatchSpec.parse(body.batch or default_batch)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if spec.is_pages and source.zone is None:
        raise HTTPException(status_code=422, detail=f"{spec.label} applies to documents reviewed by page only")
    if plan is not None:
        # windows of pages and segmenting batches must be consecutive
        consecutive = source.zone is not None or source.segmenting
        model_units = plan.indices(MODEL)
        batches = consecutive_batches(model_units, source.units, spec) if consecutive else plan_batches([source.units[i] for i in model_units], spec)
        doubt_batches: list[list[int]] = []
        if body.marks:
            doubt_spec = BatchSpec.parse(DEFAULT_DOUBT_PAGE_BATCH if source.zone else DEFAULT_DOUBT_UNIT_BATCH)
            doubt_batches = consecutive_batches(plan.indices(COPIED), source.units, doubt_spec)
    else:
        batches = plan_batches(source.units, spec)
    stable_id = source.file.stable_id
    db_dir = get_settings().db_dir
    job_id = uuid.uuid4().hex
    # the user's own lock, if their session holds it; another session's lock stays and makes the acquisition fail
    release_lock(stable_id, session_id, db_dir, logger)
    if not acquire_lock(stable_id, job_holder(job_id), db_dir, logger):
        raise HTTPException(status_code=423, detail="The document is being edited by another user")
    what = f"transfer from {body.source_stable_id}" if plan is not None else "correction"
    logger.info(f"auto-annotation: {what} {job_id} of {stable_id} started by {current_user['username']} with {body.provider_id}/{body.model_id}")
    return StartResponse(
        job_id=job_id, doc_id=source.file.doc_id, variant=source.file.variant, unit_count=len(source.units),
        batches=batches, unit_name="pages" if source.zone else "units",
        transfer=_counts(plan) if plan is not None else None, doubt_batches=doubt_batches if plan is not None else [],
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
    client = ProviderChatClient(provider, body.model_id)
    rules = [(c, t) for c, t, _ in source.rules]
    units = [by_index[i] for i in body.units]
    if body.source_stable_id:
        plan = _transfer(body.source_stable_id, source, current_user, repo, storage)[2]
        plans = plan.by_index()
        wanted = COPIED if body.mode == "doubts" else MODEL
        if any(plans[i].category != wanted for i in body.units):
            raise HTTPException(status_code=422, detail=f"Not all units are {wanted} units of the transfer")
        retry = RetryPolicy(retries=body.retries)
        if body.mode == "doubts":
            if source.zone is not None:
                transferred = await doubt_page_batch(client, rules, source.zone, units, plan, retry=retry)
            else:
                transferred = await doubt_unit_batch(client, rules, units, plans, retry=retry)
        elif source.zone is not None:
            transferred = await transfer_page_batch(client, rules, source.zone, units, plan, marks=body.marks, retry=retry)
        else:
            transferred = await transfer_unit_batch(client, rules, units, plans, segmenting=source.segmenting, marks=body.marks, retry=retry)
        _keep_lock(body.stable_id, body.job_id)
        return BatchResponse(
            units=[UnitCorrectionResponse(index=u.index, status=u.status.value, xml=u.xml, doubt=u.doubt) for u in transferred.units],
            error=transferred.error, calls=transferred.calls, retries=transferred.retries, seconds=transferred.seconds,
        )
    if source.zone is not None:
        result = await correct_page_batch(client, rules, source.zone, units, retry=RetryPolicy(retries=body.retries))
    else:
        result = await correct_batch(client, rules, units, segmenting=source.segmenting, retry=RetryPolicy(retries=body.retries))
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
    if body.source_stable_id:
        return _finish_transfer(body, source, current_user, repo, storage)
    corrections = {item.index: item.xml for item in body.corrections}
    zone_xml: str | None = None
    if source.zone is not None:
        zone_xml, problems = apply_page_changes(source.zone, corrections)
    else:
        problems = check_corrections(source.units, corrections, source.segmenting)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems[:5]))
    summary = CorrectionSummary(
        units=len(source.units), processed=min(body.processed, len(source.units)), changed=len(corrections),
        invalid=body.invalid, errors=body.errors, unit_name="pages" if source.zone else "units",
    )
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        if zone_xml is not None:
            xml = write_zone_corrections(
                zone_xml, body.provider_id, body.model_id, summary, when, body.seconds,
                status=body.status, label=body.label, model_label=body.model_label,
            )
        else:
            xml = write_corrections(
                source.xml, source.unit_path, corrections, body.provider_id, body.model_id, summary, when, body.seconds,
                segmenting=source.segmenting, status=body.status, label=body.label, model_label=body.model_label,
            )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.info(f"auto-annotation: correction {body.job_id} of {body.stable_id} finished ({summary})")
    return FinishResponse(xml=xml, changed=len(corrections))


def _finish_transfer(
    body: FinishRequest, target: CorrectionSource, user: dict[str, object], repo: FileRepository, storage: FileStorage
) -> FinishResponse:
    """/finish of a transfer: the copied units and the model's results, with the source's status."""
    assert body.source_stable_id is not None
    source_file, source_xml, plan = _transfer(body.source_stable_id, target, user, repo, storage)
    try:
        results = merge_corrections(plan, {item.index: item.xml for item in body.corrections})
    except TransferError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    zone_xml: str | None = None
    if target.zone is not None:
        zone_xml, problems = apply_page_changes(target.zone, results)
    else:
        problems = check_transfer_results(target.units, results, target.segmenting)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems[:5]))
    counts = plan.counts()
    model_units = counts[MODEL]
    sent = min(body.processed, model_units)
    doubts = {item.index: item.comment for item in body.doubts}
    summary = TransferSummary(
        units=len(target.units), copied=counts[COPIED], transferred=max(0, sent - body.invalid - body.errors), unmatched=counts["unmatched"],
        invalid=body.invalid, errors=body.errors, unprocessed=model_units - sent, doubted=len(doubts),
        unit_name="pages" if target.zone else "units",
    )
    status = extract_last_revision_status(source_xml.encode("utf-8")) or "extraction"
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    desc = transfer_description_xml(
        body.provider_id, body.model_id, summary, body.seconds, source_file.stable_id, source_file.label, model_label=body.model_label,
    )
    try:
        xml = write_transfer(
            target.xml, target.unit_path, results, desc, when, status, CHANGE_LABEL, segmenting=target.segmenting, zone_xml=zone_xml,
        )
        positions = new_unit_positions(results, len(target.units)) if target.segmenting else None
        marks = doubt_marks(_new_fingerprints(target, xml), doubts, positions)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    logger.info(f"auto-annotation: transfer {body.job_id} from {source_file.stable_id} to {body.stable_id} finished ({summary})")
    return FinishResponse(xml=xml, changed=len(results), marks=[MarkItem(**m) for m in marks])


class PreviewResponse(BaseModel):
    unit_count: int
    unit_name: str
    transfer: TransferCounts


@router.get("/transfer-preview", response_model=PreviewResponse)
def transfer_preview(
    target: str = Query(..., description="Stable id of the version to transfer to"),
    source: str = Query(..., description="Stable id of the version to transfer from"),
    current_user: dict[str, object] = Depends(require_annotator),
    repo: FileRepository = Depends(get_file_repository),
    storage: FileStorage = Depends(get_file_storage),
) -> PreviewResponse:
    """The counts of a transfer (for the dialog), without a job; 422 if the versions do not share their text."""
    target_source = _source(target, current_user, repo, storage, with_rules=False)
    plan = _transfer(source, target_source, current_user, repo, storage)[2]
    return PreviewResponse(unit_count=len(target_source.units), unit_name="pages" if target_source.zone else "units", transfer=_counts(plan))


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
