"""
Comparing the stored model tests (probe_store.py): the runs of one model on other documents, with
scores pooled over the documents, and the runs of other models on the same reference version.

Scores are only directly comparable between runs with the same prompt version; on the same
document, also only with the same rules. The views mark runs that differ.

Pure module (file system only).
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

from .evaluation import Summary, UnitRecord, summarize
from .probe_store import ProbeStore, StoredRun, UnknownRunError

_INT_FIELDS = {f.name for f in fields(UnitRecord) if f.type in ("int", int)}
_BOOL_FIELDS = {f.name for f in fields(UnitRecord) if f.type in ("bool", bool)}


@dataclass
class RunOverview:
    """A stored run with the scores the comparison tables show."""

    run: StoredRun
    # summary.json entry of the run, None before its first run finished
    baseline: dict[str, object] | None
    summaries: list[dict[str, object]]

    @property
    def meta(self) -> dict[str, object]:
        return self.run.meta

    @property
    def best(self) -> dict[str, object] | None:
        """The configuration with the highest edit F1."""
        scored = [s for s in self.summaries if isinstance(s.get("edit_f1"), (int, float))]
        return max(scored, key=lambda s: float(s["edit_f1"])) if scored else None  # type: ignore[arg-type]  # filtered above


def model_key(meta: Mapping[str, object]) -> str:
    return f"{meta.get('provider_id')}/{meta.get('model_id')}"


def read_unit_records(path: Path) -> list[UnitRecord]:
    """The rows of a units.csv."""
    records: list[UnitRecord] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            values: dict[str, object] = {}
            for name, value in row.items():
                if name in _INT_FIELDS:
                    values[name] = int(value)
                elif name in _BOOL_FIELDS:
                    values[name] = value == "True"
                else:
                    values[name] = value
            records.append(UnitRecord(**values))  # type: ignore[arg-type]  # converted to the field types above
    return records


def overview(store: ProbeStore, run: StoredRun) -> RunOverview:
    entries = store.read_summary(run.run_id)
    entry = entries[0] if entries else {}
    baseline = entry.get("baseline") if isinstance(entry.get("baseline"), dict) else None
    summaries = entry.get("summaries") if isinstance(entry.get("summaries"), list) else []
    return RunOverview(run, baseline, list(summaries))  # type: ignore[arg-type]  # summary.json is written by report.write_results


def runs_of_model(store: ProbeStore, model: str, runs: list[StoredRun] | None = None) -> list[RunOverview]:
    """All runs of a model (provider id/model id), newest first."""
    return [overview(store, r) for r in (runs if runs is not None else store.list_runs()) if model_key(r.meta) == model]


def runs_on_reference(store: ProbeStore, reference_stable_id: str, runs: list[StoredRun] | None = None) -> list[RunOverview]:
    """The latest run of every model on a reference version that has a result, newest first."""
    latest: dict[str, RunOverview] = {}
    for run in runs if runs is not None else store.list_runs():
        if run.meta.get("reference_stable_id") != reference_stable_id or model_key(run.meta) in latest:
            continue
        item = overview(store, run)
        if item.summaries:
            latest[model_key(run.meta)] = item
    return list(latest.values())


def latest_complete_per_reference(items: list[RunOverview], prompt_version: object) -> list[RunOverview]:
    """Of the given runs (newest first), the latest finished run per reference version with this prompt version."""
    chosen: dict[str, RunOverview] = {}
    for item in items:
        reference = str(item.meta.get("reference_stable_id"))
        if item.run.status.state == "done" and item.meta.get("prompt_version") == prompt_version and reference not in chosen:
            chosen[reference] = item
    return list(chosen.values())


def pooled_summaries(store: ProbeStore, items: list[RunOverview]) -> list[Summary]:
    """
    Scores per batching and output mode, pooled over the units of all given runs (so that every
    document counts by its number of units); `repeats` holds the number of runs pooled.
    """
    cells: dict[tuple[str, str], tuple[list[UnitRecord], int]] = {}
    for item in items:
        try:
            records = read_unit_records(store.result_path(item.run.run_id, "units.csv"))
        except (UnknownRunError, OSError, ValueError):
            continue
        seen: set[tuple[str, str]] = set()
        for record in records:
            key = (record.batching, record.mode)
            cell_records, count = cells.get(key, ([], 0))
            cell_records.append(record)
            cells[key] = (cell_records, count + (key not in seen))
            seen.add(key)
    return [
        summarize(str(records[0].model), "pooled", batching, mode, records, [], count)
        for (batching, mode), (records, count) in cells.items()
    ]
