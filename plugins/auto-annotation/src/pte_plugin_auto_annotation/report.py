"""
Writing evaluation results: units CSV, batches JSONL (with raw answers), summary JSON and a
Markdown report.

Pure module.
"""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import asdict, fields
from pathlib import Path
from typing import Sequence

from .evaluation import EvaluationResult, Summary, UnitRecord


INPUT_CHOICES = {
    "given": "given",
    "lineage": "the extraction the reference was derived from",
    "earliest": "the earliest extraction; none in the reference's lineage",
}


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{100 * value:.1f}"


def markdown_report(results: Sequence[EvaluationResult]) -> str:
    """One table per (model, document), with the GROBID baseline as the first row."""
    lines = ["# Model probe report", ""]
    for result in results:
        meta = result.meta
        lines += [
            f"## {result.model} on {meta['doc_id'] or meta['reference_stable_id']}",
            "",
            *([] if result.complete else [f"**Incomplete:** {result.finished_runs} of {result.planned_runs} runs finished.", ""]),
            f"Reference version `{meta['reference_stable_id']}` ({'gold' if meta['reference_is_gold'] else 'not the gold version'}), input version `{meta['input_stable_id']}` ({INPUT_CHOICES.get(str(meta['input_choice']), meta['input_choice'])}{'' if meta['input_is_extraction'] else '; NOT an extraction'}), variant `{meta['variant']}`, unit path `{meta['unit_path']}`, "
            f"rules `{meta['rules_hash']}` ({', '.join(c for c, _ in result.source.rules)}), prompt v{meta['prompt_version']}. "
            f"{meta['aligned_units']} units aligned, {meta['unmatched_input_units']} input and {meta['unmatched_gold_units']} reference units unmatched.",
            "",
            "| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        rows: list[Summary] = ([result.baseline] if result.baseline else []) + result.summaries
        for s in rows:
            quarters = " / ".join(_pct(q) for q in s.edit_recall_by_quarter) if s.calls else "–"
            lines.append(
                f"| {s.batching} | {s.mode} | {s.repeats} | {s.calls} | {s.retries} | {_pct(s.error_rate)} | {_pct(s.failure_rate)} | {_pct(s.exact_rate)} | "
                f"{_pct(s.edit_precision)} | {_pct(s.edit_recall)} | {_pct(s.edit_f1)} | {_pct(s.field_f1)} | {quarters} | "
                f"{s.seconds:.1f} | {s.prompt_chars} | {s.response_chars} |"
            )
        lines.append("")
    lines += [
        "Edit P/R: precision and recall of the changes made to the input, against the changes the reference version needs. "
        "Retries: requests repeated after a provider overload or rate limit. "
        "Errors: units whose request still failed; they say nothing about the model and are left out of all other columns. "
        "Failures: units the model answered without a usable proposal (missing, malformed, other outermost element, text changed); they are scored as unchanged input. "
        "The baseline row scores the input itself.",
        "",
    ]
    return "\n".join(lines)


def _write_atomic(path: Path, text: str) -> None:
    """Replace path with text without ever leaving a half-written file (results are rewritten while running)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_results(results: Sequence[EvaluationResult], out_dir: Path) -> list[Path]:
    """
    Write all output files into out_dir (created if needed) and return their paths. Each file is
    replaced atomically, so the function can be called after every run to keep partial results.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    units = io.StringIO(newline="")
    writer = csv.DictWriter(units, fieldnames=[fld.name for fld in fields(UnitRecord)])
    writer.writeheader()
    for result in results:
        for record in result.units:
            writer.writerow(asdict(record))
    units_path = out_dir / "units.csv"
    _write_atomic(units_path, units.getvalue())
    batches_path = out_dir / "batches.jsonl"
    _write_atomic(batches_path, "".join(
        json.dumps({"reference_stable_id": result.source.reference_stable_id, **batch}, ensure_ascii=False) + "\n"
        for result in results for batch in result.batches
    ))
    summary_path = out_dir / "summary.json"
    _write_atomic(summary_path, json.dumps([
        {
            "meta": result.meta,
            "baseline": asdict(result.baseline) if result.baseline else None,
            "summaries": [asdict(s) for s in result.summaries],
        }
        for result in results
    ], indent=2, ensure_ascii=False))
    report_path = out_dir / "report.md"
    _write_atomic(report_path, markdown_report(results))
    return [units_path, batches_path, summary_path, report_path]
