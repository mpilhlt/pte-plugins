"""
Routes of "Test model with current document" (frontend: extensions/auto-annotation.js): the model
probe of probe.py as a server job, with its results stored in the plugin data directory (see
probe_store.py and probe_job.py).

  POST /api/plugins/auto-annotation/probe/start    check the parameters and start the job
  GET  /api/plugins/auto-annotation/probe/status   the state of a run
  GET  /api/plugins/auto-annotation/probe/jobs     the user's running jobs (to resume their progress widgets)
  POST /api/plugins/auto-annotation/probe/cancel   stop a running job; the finished runs are kept
  POST /api/plugins/auto-annotation/probe/delete   delete a run and its results
  GET  /api/plugins/auto-annotation/probe/view     the page of a run, with the comparison with other runs
  GET  /api/plugins/auto-annotation/probe/history  all runs
  GET  /api/plugins/auto-annotation/probe/export   a result file of a run, or all of them as a zip

For reviewers and admins. Every reviewer sees all runs; only the user who started a run and admins
can cancel or delete it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from fastapi_app.lib.core.dependencies import get_file_repository, get_file_storage, require_reviewer_or_admin
from fastapi_app.lib.core.url_cache import UrlCache
from fastapi_app.lib.repository.file_repository import FileRepository
from fastapi_app.lib.secrets import secret_required_detail
from fastapi_app.lib.storage.file_storage import FileStorage
from fastapi_app.plugin_api import get_settings

from .batching import BatchSpec
from .correction import ProviderChatClient, RetryPolicy
from .evaluation import EvalSource, align_source, aligned_units, sweep
from .models import ModelResolutionError, ensure_model_access, provider_for_user
from .probe_compare import latest_complete_per_reference, model_key, pooled_summaries, runs_of_model, runs_on_reference, overview
from .probe_job import start_job
from .probe_store import RESULT_FILES, RUNS_DIR_NAME, ProbeStatus, ProbeStore, StoredRun, UnknownRunError
from .probe_views import render_history_page, render_run_page
from .prompts import PROMPT_VERSION, OutputMode
from .sources import SourceError, resolve_source

logger = logging.getLogger(__name__)

# Included by routes.router, which adds /api/plugins/auto-annotation
router = APIRouter(prefix="/probe")

# Upper bound of runs (batch specs x modes x repetitions) of one test
MAX_RUNS = 60


def get_probe_store() -> ProbeStore:
    return ProbeStore(get_settings().plugins_data_dir / "auto-annotation" / RUNS_DIR_NAME)


def _is_admin(user: dict[str, object]) -> bool:
    roles = user.get("roles")
    return isinstance(roles, list) and any(role in ("admin", "*") for role in roles)


def _may_manage(user: dict[str, object], meta: dict[str, object]) -> bool:
    return _is_admin(user) or meta.get("username") == user.get("username")


def _model_http_error(e: ModelResolutionError) -> HTTPException:
    if e.missing_secret is not None:
        return HTTPException(status_code=428, detail=secret_required_detail(e.missing_secret))
    return HTTPException(status_code=e.status, detail=str(e))


def _stored(store: ProbeStore, run_id: str) -> StoredRun:
    try:
        return StoredRun(run_id, store.read_meta(run_id), store.read_status(run_id))
    except UnknownRunError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


class ProbeStartRequest(BaseModel):
    reference_stable_id: str
    input_stable_id: str | None = None
    provider_id: str
    model_id: str
    model_label: str | None = None
    # The document's label as the file selection shows it
    doc_label: str | None = None
    batches: list[str] = Field(default_factory=lambda: ["units:1", "units:4", "units:8", "units:16", "units:32"], min_length=1, max_length=20)
    modes: list[OutputMode] = Field(default_factory=lambda: ["all"], min_length=1)
    repeats: int = Field(default=1, ge=1, le=10)
    concurrency: int = Field(default=1, ge=1, le=8)
    retries: int = Field(default=RetryPolicy.retries, ge=0, le=10)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_units: int | None = Field(default=None, ge=1)
    force_busy: bool = False


class ProbeStartResponse(BaseModel):
    run_id: str
    planned_runs: int
    units: int
    reference_is_gold: bool
    input_stable_id: str


class ProbeStatusResponse(BaseModel):
    run_id: str
    state: str
    planned_runs: int
    finished_runs: int
    current_run: int
    current: str | None
    error: str | None
    started_at: str
    finished_at: str | None
    model: str
    model_label: str
    doc_label: str | None
    reference_stable_id: str


class RunRequest(BaseModel):
    run_id: str


def _doc_label(meta: dict[str, object]) -> str | None:
    # "title": runs stored before the label was used
    label = meta.get("doc_label") or meta.get("title")
    return str(label) if label else None


def _status_response(run: StoredRun) -> ProbeStatusResponse:
    meta = run.meta
    status = run.status
    return ProbeStatusResponse(
        run_id=run.run_id, state=status.state, planned_runs=status.planned_runs, finished_runs=status.finished_runs,
        current_run=status.current_run, current=status.current, error=status.error, started_at=status.started_at,
        finished_at=status.finished_at, model=model_key(meta), model_label=str(meta.get("model_label") or model_key(meta)),
        doc_label=_doc_label(meta), reference_stable_id=str(meta.get("reference_stable_id")),
    )


@router.post("/start", response_model=ProbeStartResponse)
async def start(
    body: ProbeStartRequest,
    current_user: dict[str, object] = Depends(require_reviewer_or_admin),
    repo: FileRepository = Depends(get_file_repository),
    storage: FileStorage = Depends(get_file_storage),
    store: ProbeStore = Depends(get_probe_store),
) -> ProbeStartResponse:
    """Check the parameters, the model and the documents, then start the test as a job of this worker."""
    username = str(current_user["username"])
    try:
        specs = [BatchSpec.parse(s) for s in body.batches]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    configs = sweep(specs, list(dict.fromkeys(body.modes)), body.repeats)
    if len(configs) > MAX_RUNS:
        raise HTTPException(status_code=422, detail=f"{len(configs)} runs are too many for one test (at most {MAX_RUNS}); choose fewer batch sizes, modes or repetitions")

    running = [r for r in store.list_runs() if r.meta.get("username") == username and r.status.state == "running"]
    if running:
        raise HTTPException(status_code=409, detail=f"You already have a model test running ({running[0].meta.get('model_label') or model_key(running[0].meta)}). Wait for it to finish or cancel it.")

    try:
        provider = provider_for_user(body.provider_id, username)
        ensure_model_access(provider, body.model_id, current_user)
    except ModelResolutionError as e:
        raise _model_http_error(e) from e
    if not body.force_busy:
        try:
            models = provider.list_models()
        except Exception as e:  # noqa: BLE001 - a provider that cannot list its models may still answer
            logger.info(f"auto-annotation: cannot list the models of {body.provider_id}: {e}")
            models = []
        model_status = next((m.get("status") for m in models if m["id"] == body.model_id), None)
        if model_status and model_status["availability"] != "available":
            raise HTTPException(status_code=409, detail=f"The model is {model_status['availability'].replace('_', ' ')} ({model_status['detail']}); most requests would time out. Try again later, or start anyway.")

    try:
        resolved = resolve_source(None, None, body.reference_stable_id, body.input_stable_id, current_user, repo, storage, UrlCache(get_settings().annotation_rules_cache_dir))
    except SourceError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    if resolved.input_stable_id == resolved.reference_stable_id:
        raise HTTPException(status_code=422, detail="The version to correct is the current version itself; choose another input version")
    source = EvalSource(
        reference_stable_id=resolved.reference_stable_id, input_stable_id=resolved.input_stable_id, variant=resolved.variant,
        unit_path=resolved.unit_path, gold_xml=resolved.gold_xml, input_xml=resolved.input_xml,
        rules=[(category, text) for category, text, _ in resolved.rules], doc_id=resolved.doc_id,
        reference_is_gold=resolved.reference_is_gold, input_choice=resolved.input_choice,
        input_is_extraction=resolved.input_is_extraction, segmenting=resolved.segmenting,
    )
    try:
        units = len(aligned_units(align_source(source, body.max_units)))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    run_id = store.new_run_id()
    model = f"{body.provider_id}/{body.model_id}"
    meta: dict[str, object] = {
        "username": username,
        "provider_id": body.provider_id, "model_id": body.model_id, "model_label": body.model_label or model,
        "doc_id": source.doc_id, "doc_label": body.doc_label, "variant": source.variant,
        "reference_stable_id": source.reference_stable_id, "reference_is_gold": source.reference_is_gold,
        "input_stable_id": source.input_stable_id, "input_choice": source.input_choice, "input_is_extraction": source.input_is_extraction,
        "aligned_units": units, "prompt_version": PROMPT_VERSION, "rules_hash": source.rules_hash,
        "rule_categories": [category for category, _ in source.rules],
        "params": {
            "batches": [s.label for s in specs], "modes": list(dict.fromkeys(body.modes)), "repeats": body.repeats,
            "concurrency": body.concurrency, "retries": body.retries, "temperature": body.temperature, "max_units": body.max_units,
        },
    }
    store.create(run_id, meta, ProbeStatus(state="running", planned_runs=len(configs)))
    client = ProviderChatClient(provider, body.model_id, body.temperature)
    start_job(store, run_id, client, model, source, configs, body.concurrency, RetryPolicy(retries=body.retries), body.max_units)
    logger.info(f"auto-annotation: {username} started model test {run_id} of {model} on {source.reference_stable_id} ({len(configs)} runs, {units} units)")
    return ProbeStartResponse(run_id=run_id, planned_runs=len(configs), units=units, reference_is_gold=source.reference_is_gold, input_stable_id=source.input_stable_id)


@router.get("/status", response_model=ProbeStatusResponse)
def status(
    run: str = Query(...),
    current_user: dict[str, object] = Depends(require_reviewer_or_admin),
    store: ProbeStore = Depends(get_probe_store),
) -> ProbeStatusResponse:
    return _status_response(_stored(store, run))


@router.get("/jobs", response_model=list[ProbeStatusResponse])
def jobs(
    current_user: dict[str, object] = Depends(require_reviewer_or_admin),
    store: ProbeStore = Depends(get_probe_store),
) -> list[ProbeStatusResponse]:
    """The user's running tests."""
    return [_status_response(r) for r in store.list_runs() if r.meta.get("username") == current_user.get("username") and r.status.state == "running"]


@router.post("/cancel", response_model=ProbeStatusResponse)
def cancel(
    body: RunRequest,
    current_user: dict[str, object] = Depends(require_reviewer_or_admin),
    store: ProbeStore = Depends(get_probe_store),
) -> ProbeStatusResponse:
    """Ask the job to stop; it stops within seconds, keeping the finished runs."""
    run = _stored(store, body.run_id)
    if not _may_manage(current_user, run.meta):
        raise HTTPException(status_code=403, detail="Only the user who started the test and admins can cancel it")
    if run.status.state != "running":
        raise HTTPException(status_code=409, detail=f"The test is not running ({run.status.state})")
    store.request_cancel(body.run_id)
    return _status_response(run)


@router.post("/delete")
def delete(
    body: RunRequest,
    current_user: dict[str, object] = Depends(require_reviewer_or_admin),
    store: ProbeStore = Depends(get_probe_store),
) -> dict[str, str]:
    run = _stored(store, body.run_id)
    if not _may_manage(current_user, run.meta):
        raise HTTPException(status_code=403, detail="Only the user who started the test and admins can delete it")
    if run.status.state == "running":
        raise HTTPException(status_code=409, detail="The test is still running; cancel it first")
    store.delete(body.run_id)
    return {"status": "deleted"}


@router.get("/view", response_class=HTMLResponse)
def view(
    run: str = Query(...),
    current_user: dict[str, object] = Depends(require_reviewer_or_admin),
    store: ProbeStore = Depends(get_probe_store),
) -> HTMLResponse:
    """The page of a run and the comparison with the other stored runs."""
    stored = _stored(store, run)
    runs = store.list_runs()
    item = overview(store, stored)
    same_model = runs_of_model(store, model_key(stored.meta), runs)
    pooled = pooled_summaries(store, latest_complete_per_reference(same_model, stored.meta.get("prompt_version")))
    same_document = runs_on_reference(store, str(stored.meta.get("reference_stable_id")), runs)
    return HTMLResponse(render_run_page(item, same_model, pooled, same_document, _may_manage(current_user, stored.meta)))


@router.get("/history", response_class=HTMLResponse)
def history(
    model: str | None = Query(None, description="Only the runs of this model (provider id/model id)"),
    current_user: dict[str, object] = Depends(require_reviewer_or_admin),
    store: ProbeStore = Depends(get_probe_store),
) -> HTMLResponse:
    runs = store.list_runs()
    models = {model_key(r.meta): str(r.meta.get("model_label") or model_key(r.meta)) for r in runs}
    items = [overview(store, r) for r in runs if model is None or model_key(r.meta) == model]
    return HTMLResponse(render_history_page(items, model, models))


@router.get("/export")
def export(
    run: str = Query(...),
    file: str = Query("zip", description="zip, or one of the result files"),
    current_user: dict[str, object] = Depends(require_reviewer_or_admin),
    store: ProbeStore = Depends(get_probe_store),
) -> Response:
    _stored(store, run)
    if file == "zip":
        return Response(
            content=store.zip_results(run), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="model-test-{run}.zip"'},
        )
    try:
        path: Path = store.result_path(run, file)
    except UnknownRunError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return FileResponse(path, media_type=RESULT_FILES[file], filename=f"model-test-{run}-{file}")
