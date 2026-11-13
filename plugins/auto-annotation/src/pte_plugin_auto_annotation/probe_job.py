"""
Running a model test of the editor as a server job: evaluate() of evaluation.py as an asyncio task
of the worker that started it, writing its progress and results to the run's directory (see
probe_store.py) after every run, so that every worker can report them.

The job stops

- when a cancel is requested (the cancel file, checked every CANCEL_POLL_SECONDS): "cancelled";
- when every request of a run failed (the provider is down, the key is wrong, the quota is used
  up), since the next runs would fail the same way: "failed", with the first error;
- when evaluate() raises: "failed";
- when the worker shuts down (the task is cancelled): "interrupted".

The results of the finished runs are kept in every case.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence

from .correction import ChatClient, RetryPolicy
from .evaluation import EvalSource, EvaluationResult, RunConfig, evaluate
from .probe_store import HEARTBEAT_SECONDS, ProbeStatus, ProbeStore, now_iso
from .report import write_results

logger = logging.getLogger(__name__)

CANCEL_POLL_SECONDS = 2.0

# Jobs of this worker by run id
_tasks: dict[str, asyncio.Task[None]] = {}


class RunFailedError(Exception):
    """Every request of a run failed; the test is stopped."""


def config_label(config: RunConfig) -> str:
    return f"{config.spec.label}, {config.mode}, #{config.repeat}"


def _all_requests_failed(result: EvaluationResult, config: RunConfig) -> str | None:
    """The first error of the run of `config` if all of its requests failed, else None."""
    batches = [b for b in result.batches if (b["batching"], b["mode"], b["repeat"]) == (config.spec.label, config.mode, config.repeat)]
    if batches and all(b.get("error") for b in batches):
        return str(batches[0]["error"])
    return None


async def run_job(
    store: ProbeStore,
    run_id: str,
    client: ChatClient,
    model: str,
    source: EvalSource,
    configs: Sequence[RunConfig],
    concurrency: int = 1,
    retry: RetryPolicy | None = None,
    max_units: int | None = None,
    poll_seconds: float = CANCEL_POLL_SECONDS,
) -> ProbeStatus:
    """Run the test and return its final status (also written to status.json)."""
    status = store.read_status(run_id)
    status.state = "running"
    status.heartbeat = time.time()
    store.write_status(run_id, status)
    out = store.run_dir(run_id)

    def progress(number: int, total: int, config: RunConfig) -> None:
        status.current_run = number
        status.current = config_label(config)
        status.heartbeat = time.time()
        store.write_status(run_id, status)

    def update(result: EvaluationResult) -> None:
        write_results([result], out)
        status.finished_runs = result.finished_runs
        status.heartbeat = time.time()
        store.write_status(run_id, status)
        error = _all_requests_failed(result, configs[result.finished_runs - 1])
        if error is not None:
            raise RunFailedError(f"All requests of the run {config_label(configs[result.finished_runs - 1])} failed: {error}")

    work: asyncio.Future[EvaluationResult] = asyncio.ensure_future(
        evaluate(client, model, source, configs, concurrency, progress, max_units=max_units, retry=retry, on_update=update)
    )
    cancelled_by_user = False
    last_heartbeat = time.monotonic()
    try:
        while not work.done():
            await asyncio.wait({work}, timeout=poll_seconds)
            if work.done():
                break
            if not cancelled_by_user and store.cancel_requested(run_id):
                cancelled_by_user = True
                work.cancel()
            if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                last_heartbeat = time.monotonic()
                status.heartbeat = time.time()
                store.write_status(run_id, status)
        work.result()
        status.state = "done"
    except asyncio.CancelledError:
        if not cancelled_by_user:
            # the worker shuts down
            work.cancel()
            status.state = "interrupted"
            status.error = "The test was stopped because the server shut down. The results of the finished runs are kept."
            status.finished_at = now_iso()
            store.write_status(run_id, status)
            raise
        status.state = "cancelled"
    except Exception as e:  # noqa: BLE001 - the job reports every failure in its status
        if not isinstance(e, (RunFailedError, ValueError)):
            logger.exception(f"auto-annotation: model test {run_id} failed")
        status.state = "failed"
        status.error = str(e)
    status.current = None
    status.finished_at = now_iso()
    status.heartbeat = time.time()
    store.write_status(run_id, status)
    return status


def start_job(
    store: ProbeStore, run_id: str, client: ChatClient, model: str, source: EvalSource, configs: Sequence[RunConfig],
    concurrency: int = 1, retry: RetryPolicy | None = None, max_units: int | None = None,
) -> asyncio.Task[None]:
    """Start run_job as a task of this worker's event loop."""

    async def job() -> None:
        try:
            await run_job(store, run_id, client, model, source, configs, concurrency, retry, max_units)
        finally:
            _tasks.pop(run_id, None)

    task = asyncio.get_running_loop().create_task(job(), name=f"auto-annotation-probe-{run_id}")
    _tasks[run_id] = task
    return task


async def stop_all_jobs() -> None:
    """Cancel the jobs of this worker (plugin cleanup); they are marked as interrupted."""
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
