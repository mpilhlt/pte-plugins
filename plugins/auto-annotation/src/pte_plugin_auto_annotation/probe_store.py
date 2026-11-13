"""
Storage of the model tests started in the editor ("Test model with current document"), one
directory per run in the plugin data directory:

    <plugins data dir>/auto-annotation/probe-runs/<run id>/
        meta.json      who started the run, model, document, versions, parameters
        status.json    state, runs finished and planned, heartbeat, error (rewritten while running)
        cancel         present once a cancel was requested
        units.csv, batches.jsonl, summary.json, report.md   results, as written by the probe CLI

The job runs in one worker, but every worker answers status requests and serves the results from
these files. A running job writes its heartbeat regularly; a run that is still "running" with an
old heartbeat was stopped with its worker and is reported as "interrupted".

Pure module (file system only).
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import time
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

RUNS_DIR_NAME = "probe-runs"

# A running job writes its heartbeat every HEARTBEAT_SECONDS; after STALE_SECONDS without one, it counts as interrupted
HEARTBEAT_SECONDS = 15.0
STALE_SECONDS = 120.0

# Result files that can be downloaded, with their media types
RESULT_FILES = {
    "report.md": "text/markdown; charset=utf-8",
    "summary.json": "application/json",
    "units.csv": "text/csv; charset=utf-8",
    "batches.jsonl": "application/x-ndjson",
    "meta.json": "application/json",
}

_RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")

ProbeState = Literal["running", "done", "failed", "cancelled", "interrupted"]
FINAL_STATES: frozenset[str] = frozenset({"done", "failed", "cancelled", "interrupted"})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ProbeStatus:
    """The progress of a run, as status.json holds it."""

    state: ProbeState
    planned_runs: int
    finished_runs: int = 0
    # Number (1-based) and label of the run under way, e.g. "units:8, all, #1"
    current_run: int = 0
    current: str | None = None
    error: str | None = None
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    # time.time() of the last sign of life of the job
    heartbeat: float = field(default_factory=time.time)

    @classmethod
    def from_json(cls, data: dict[str, object]) -> "ProbeStatus":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})  # type: ignore[arg-type]  # status.json is written by this class


@dataclass
class StoredRun:
    """A run as listed: its metadata and its status."""

    run_id: str
    meta: dict[str, object]
    status: ProbeStatus


class UnknownRunError(Exception):
    """No run with this id."""


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class ProbeStore:
    """The run directories below `root` (<plugins data dir>/auto-annotation/probe-runs)."""

    def __init__(self, root: Path, clock: Callable[[], float] | None = None):
        self.root = Path(root)
        self._clock = clock or time.time

    @staticmethod
    def new_run_id() -> str:
        return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"

    @staticmethod
    def is_run_id(run_id: str) -> bool:
        return bool(_RUN_ID.match(run_id))

    def run_dir(self, run_id: str) -> Path:
        """
        Raises:
            UnknownRunError: not a run id, or no such run.
        """
        if not self.is_run_id(run_id):
            raise UnknownRunError(f"Unknown run {run_id!r}")
        path = self.root / run_id
        if not (path / "meta.json").is_file():
            raise UnknownRunError(f"Unknown run {run_id!r}")
        return path

    def create(self, run_id: str, meta: dict[str, object], status: ProbeStatus) -> Path:
        path = self.root / run_id
        path.mkdir(parents=True, exist_ok=False)
        _write_atomic(path / "meta.json", json.dumps({"run_id": run_id, **meta}, indent=2, ensure_ascii=False))
        self.write_status(run_id, status, path)
        return path

    def write_status(self, run_id: str, status: ProbeStatus, path: Path | None = None) -> None:
        _write_atomic((path or self.run_dir(run_id)) / "status.json", json.dumps(asdict(status), indent=2, ensure_ascii=False))

    def read_meta(self, run_id: str) -> dict[str, object]:
        data = json.loads((self.run_dir(run_id) / "meta.json").read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        return data

    def read_status(self, run_id: str) -> ProbeStatus:
        """The status; a running job without a recent heartbeat is reported as interrupted."""
        path = self.run_dir(run_id) / "status.json"
        try:
            status = ProbeStatus.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return ProbeStatus(state="interrupted", planned_runs=0, error="The status of the run cannot be read")
        if status.state == "running" and self._clock() - status.heartbeat > STALE_SECONDS:
            status.state = "interrupted"
            status.error = status.error or "The test stopped without finishing, probably because the server restarted. The results of the finished runs are kept."
        return status

    def request_cancel(self, run_id: str) -> None:
        (self.run_dir(run_id) / "cancel").write_text(now_iso(), encoding="utf-8")

    def cancel_requested(self, run_id: str) -> bool:
        return (self.root / run_id / "cancel").exists()

    def list_runs(self) -> list[StoredRun]:
        """All runs, newest first."""
        if not self.root.is_dir():
            return []
        runs: list[StoredRun] = []
        for path in sorted(self.root.iterdir(), reverse=True):
            if not path.is_dir() or not self.is_run_id(path.name):
                continue
            try:
                runs.append(StoredRun(path.name, self.read_meta(path.name), self.read_status(path.name)))
            except (UnknownRunError, OSError, ValueError):
                continue
        return runs

    def read_summary(self, run_id: str) -> list[dict[str, object]]:
        """summary.json (one entry per model and document; a run of the UI has one), or [] before the first run finished."""
        path = self.run_dir(run_id) / "summary.json"
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []

    def result_path(self, run_id: str, name: str) -> Path:
        """
        Raises:
            UnknownRunError: no such run, or no such result file (yet).
        """
        if name not in RESULT_FILES:
            raise UnknownRunError(f"Unknown result file {name!r}")
        path = self.run_dir(run_id) / name
        if not path.is_file():
            raise UnknownRunError(f"The run {run_id} has no {name} yet")
        return path

    def zip_results(self, run_id: str) -> bytes:
        """All result files of a run as a zip archive."""
        path = self.run_dir(run_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in RESULT_FILES:
                if (path / name).is_file():
                    archive.write(path / name, f"{run_id}/{name}")
        return buffer.getvalue()

    def delete(self, run_id: str) -> None:
        shutil.rmtree(self.run_dir(run_id))
