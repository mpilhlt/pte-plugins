"""
Writing evaluation results: units CSV, batches JSONL (with raw answers), summary JSON and a
Markdown report.

Pure module.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, fields
from pathlib import Path
from typing import Sequence

from .evaluation import EvaluationResult, Summary, UnitRecord


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{100 * value:.1f}"


def markdown_report(results: Sequence[EvaluationResult]) -> str:
    """One table per (model, gold document), with the GROBID baseline as the first row."""
    lines = ["# Model probe report", ""]
    for result in results:
        meta = result.meta
        lines += [
            f"## {result.model} on {meta['gold_stable_id']}",
            "",
            f"Input version `{meta['input_stable_id']}`, variant `{meta['variant']}`, unit path `{meta['unit_path']}`, "
            f"rules `{meta['rules_hash']}` ({', '.join(c for c, _ in result.source.rules)}), prompt v{meta['prompt_version']}. "
            f"{meta['aligned_units']} units aligned, {meta['unmatched_input_units']} input and {meta['unmatched_gold_units']} gold units unmatched.",
            "",
            "| Batching | Mode | Runs | Calls | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        rows: list[Summary] = ([result.baseline] if result.baseline else []) + result.summaries
        for s in rows:
            quarters = " / ".join(_pct(q) for q in s.edit_recall_by_quarter) if s.calls else "–"
            lines.append(
                f"| {s.batching} | {s.mode} | {s.repeats} | {s.calls} | {_pct(s.failure_rate)} | {_pct(s.exact_rate)} | "
                f"{_pct(s.edit_precision)} | {_pct(s.edit_recall)} | {_pct(s.edit_f1)} | {_pct(s.field_f1)} | {quarters} | "
                f"{s.seconds:.1f} | {s.prompt_chars} | {s.response_chars} |"
            )
        lines.append("")
    lines += [
        "Edit P/R: precision and recall of the changes made to the input, against the changes the gold version needs. "
        "Failures: units without a usable proposal (missing, malformed, other outermost element, text changed); they are scored as unchanged input. "
        "The baseline row scores the input itself.",
        "",
    ]
    return "\n".join(lines)


def write_results(results: Sequence[EvaluationResult], out_dir: Path) -> list[Path]:
    """Write all output files into out_dir (created if needed) and return their paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    units_path = out_dir / "units.csv"
    with units_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[fld.name for fld in fields(UnitRecord)])
        writer.writeheader()
        for result in results:
            for record in result.units:
                writer.writerow(asdict(record))
    batches_path = out_dir / "batches.jsonl"
    with batches_path.open("w", encoding="utf-8") as f:
        for result in results:
            for batch in result.batches:
                f.write(json.dumps({"gold_stable_id": result.source.gold_stable_id, **batch}, ensure_ascii=False) + "\n")
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps([
        {
            "meta": result.meta,
            "baseline": asdict(result.baseline) if result.baseline else None,
            "summaries": [asdict(s) for s in result.summaries],
        }
        for result in results
    ], indent=2, ensure_ascii=False), encoding="utf-8")
    report_path = out_dir / "report.md"
    report_path.write_text(markdown_report(results), encoding="utf-8")
    return [units_path, batches_path, summary_path, report_path]
