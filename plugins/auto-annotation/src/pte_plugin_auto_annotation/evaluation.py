"""
Evaluating one model on one document (against a reference, normally its gold version) across batch sizes and output modes.

The input is an EvalSource: the reference version, the version the model corrects (normally GROBID's
first extraction of the same document), the unit path and the rules, all fixed for the run. For
every (batch spec, output mode, repetition) the aligned input units are corrected with
correct_units() and every outcome is scored against its gold unit.

Pure module.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Callable, Sequence

from .batching import BatchSpec
from .checks import ProposalStatus
from .correction import BatchRecord, ChatClient, CorrectionRun, RetryPolicy, correct_units
from .prompts import PROMPT_VERSION, OutputMode
from .scoring import Alignment, UnitScore, align_documents, f1, ratio, score_unit
from .segmentation import SegmentAlignment, align_segments, proposal_fields, unit_scores
from .units import Unit


@dataclass(frozen=True)
class EvalSource:
    """Everything a run is computed from, as the server's /eval-source returns it."""

    reference_stable_id: str
    input_stable_id: str
    variant: str | None
    unit_path: str | None
    gold_xml: str
    input_xml: str
    rules: list[tuple[str, str]]
    doc_id: str = ""
    # False when the reference is not the document's gold version (probe --reference)
    reference_is_gold: bool = True
    # How the input was chosen: "given", "lineage" (the extraction the reference was derived from) or "earliest" (extraction)
    input_choice: str = "given"
    input_is_extraction: bool = True
    # Whether units may be split and merged (scored on text positions, see segmentation.py), as the
    # variant's extractor declares
    segmenting: bool = False

    @property
    def rules_hash(self) -> str:
        """Short hash of the rules text, to tell runs with different rules apart."""
        joined = "\n\0".join(f"{c}\n{t}" for c, t in self.rules)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class RunConfig:
    """One cell of the sweep."""

    spec: BatchSpec
    mode: OutputMode
    repeat: int


# Called before each run with (number of the run, total runs, its config)
ProgressCallback = Callable[[int, int, RunConfig], None]

# Called after each run with the result so far (summaries of the finished runs)
UpdateCallback = Callable[["EvaluationResult"], None]


@dataclass
class UnitRecord:
    """One scored unit of one run; a row of the units CSV."""

    model: str
    reference_stable_id: str
    batching: str
    mode: str
    repeat: int
    unit_index: int
    fingerprint: str
    batch_index: int
    position: int
    batch_size: int
    status: str
    exact: bool
    needed: int
    made: int
    correct_edits: int
    gold_fields: int
    proposal_fields: int
    correct_fields: int


@dataclass
class Summary:
    """Micro-averaged scores of one (model, batching, mode) cell over its repetitions."""

    model: str
    reference_stable_id: str
    batching: str
    mode: str
    repeats: int
    units: int
    calls: int
    # Retries after transient provider errors
    retries: int
    # Units whose request failed (provider error, also after retries); left out of all scores below
    error_rate: float
    # Units the model answered without a usable proposal, among the units with an answer
    failure_rate: float
    status_counts: dict[str, int]
    exact_rate: float
    edit_precision: float | None
    edit_recall: float | None
    edit_f1: float | None
    field_precision: float | None
    field_recall: float | None
    field_f1: float | None
    # Edit recall by position of the unit in its batch, in quarters of the batch (first to last)
    edit_recall_by_quarter: list[float | None]
    seconds: float
    prompt_chars: int
    response_chars: int


@dataclass
class EvaluationResult:
    """All units and batches of all runs, the summaries, and the GROBID baseline."""

    model: str
    source: EvalSource
    alignment: Alignment | SegmentAlignment
    units: list[UnitRecord] = field(default_factory=list)
    batches: list[dict[str, object]] = field(default_factory=list)
    summaries: list[Summary] = field(default_factory=list)
    baseline: Summary | None = None
    # Runs (configs) planned and finished; fewer finished than planned while running or after an interruption
    planned_runs: int = 0
    finished_runs: int = 0

    @property
    def complete(self) -> bool:
        return self.finished_runs >= self.planned_runs

    @property
    def meta(self) -> dict[str, object]:
        """Run parameters for the report."""
        return {
            "model": self.model,
            "doc_id": self.source.doc_id,
            "reference_stable_id": self.source.reference_stable_id,
            "reference_is_gold": self.source.reference_is_gold,
            "input_stable_id": self.source.input_stable_id,
            "input_choice": self.source.input_choice,
            "input_is_extraction": self.source.input_is_extraction,
            "variant": self.source.variant,
            "unit_path": self.source.unit_path,
            "rules_hash": self.source.rules_hash,
            "rule_categories": [c for c, _ in self.source.rules],
            "prompt_version": PROMPT_VERSION,
            "planned_runs": self.planned_runs,
            "finished_runs": self.finished_runs,
            "segmenting": self.source.segmenting,
            "aligned_units": len(aligned_units(self.alignment)),
            "unmatched_input_units": self.alignment.unmatched_input,
            "unmatched_gold_units": self.alignment.unmatched_gold,
        }


def summarize(
    model: str, reference_stable_id: str, batching: str, mode: str, records: Sequence[UnitRecord],
    batches: Sequence[BatchRecord], repeats: int,
) -> Summary:
    """
    Aggregate the unit records (and the batches that produced them) of one cell. Units whose
    request failed say nothing about the model and are left out of the scores.
    """
    status_counts: dict[str, int] = {}
    for r in records:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1
    errors = status_counts.get(ProposalStatus.ERROR.value, 0)
    all_records = records
    records = [r for r in records if r.status != ProposalStatus.ERROR.value]

    def total(attr: str) -> int:
        return sum(getattr(r, attr) for r in records)

    failures = sum(n for s, n in status_counts.items() if ProposalStatus(s).is_failure)
    edit_p = ratio(total("correct_edits"), total("made"))
    edit_r = ratio(total("correct_edits"), total("needed"))
    field_p = ratio(total("correct_fields"), total("proposal_fields"))
    field_r = ratio(total("correct_fields"), total("gold_fields"))
    quarters: list[float | None] = []
    for q in range(4):
        in_q = [r for r in records if min(3, r.position * 4 // max(1, r.batch_size)) == q]
        quarters.append(ratio(sum(r.correct_edits for r in in_q), sum(r.needed for r in in_q)))
    return Summary(
        model=model, reference_stable_id=reference_stable_id, batching=batching, mode=mode, repeats=repeats,
        units=len(all_records), calls=len(batches), retries=sum(b.retries for b in batches),
        error_rate=errors / len(all_records) if all_records else 0.0,
        failure_rate=failures / len(records) if records else 0.0,
        status_counts=status_counts, exact_rate=sum(r.exact for r in records) / len(records) if records else 0.0,
        edit_precision=edit_p, edit_recall=edit_r, edit_f1=f1(edit_p, edit_r),
        field_precision=field_p, field_recall=field_r, field_f1=f1(field_p, field_r),
        edit_recall_by_quarter=quarters,
        seconds=sum(b.seconds for b in batches), prompt_chars=sum(b.prompt_chars for b in batches),
        response_chars=sum(b.response_chars for b in batches),
    )


def aligned_units(alignment: Alignment | SegmentAlignment) -> list[Unit]:
    """The input units that are evaluated."""
    return alignment.units if isinstance(alignment, SegmentAlignment) else [pair.input for pair in alignment.pairs]


def _scores(alignment: Alignment | SegmentAlignment, run: CorrectionRun | None) -> dict[int, UnitScore]:
    """Scores of the evaluated units by unit index; without a run, of the input itself (baseline)."""
    if isinstance(alignment, SegmentAlignment):
        return unit_scores(alignment, proposal_fields(alignment, run) if run is not None else None)
    if run is None:
        return {p.input.index: score_unit(p.input.xml, p.gold.xml, None) for p in alignment.pairs}
    gold_by_input = {pair.input.index: pair.gold for pair in alignment.pairs}
    return {o.unit.index: score_unit(o.unit.xml, gold_by_input[o.unit.index].xml, o.proposal) for o in run.outcomes}


def _records(model: str, source: EvalSource, config: RunConfig, alignment: Alignment | SegmentAlignment, run: CorrectionRun) -> list[UnitRecord]:
    scores = _scores(alignment, run)
    records: list[UnitRecord] = []
    for outcome in run.outcomes:
        records.append(UnitRecord(
            model=model, reference_stable_id=source.reference_stable_id, batching=config.spec.label, mode=config.mode,
            repeat=config.repeat, unit_index=outcome.unit.index, fingerprint=outcome.unit.fingerprint,
            batch_index=outcome.batch_index, position=outcome.position, batch_size=outcome.batch_size,
            status=outcome.status.value, **asdict(scores[outcome.unit.index]),
        ))
    return records


def sweep(specs: Sequence[BatchSpec], modes: Sequence[OutputMode], repeats: int) -> list[RunConfig]:
    """All combinations, repetitions innermost."""
    return [RunConfig(spec, mode, r) for spec in specs for mode in modes for r in range(1, repeats + 1)]


async def evaluate(
    client: ChatClient,
    model: str,
    source: EvalSource,
    configs: Sequence[RunConfig],
    concurrency: int = 1,
    progress: ProgressCallback | None = None,
    max_units: int | None = None,
    retry: RetryPolicy | None = None,
    on_update: UpdateCallback | None = None,
) -> EvaluationResult:
    """
    Run every config and score it; with max_units, only the first aligned units are evaluated.
    Requests failing with a transient error are retried as `retry` allows. After each run,
    on_update gets the result so far, so that callers can save partial results.

    Raises:
        ValueError: a document is not well-formed, the unit path is invalid, or no unit could be aligned.
    """
    alignment: Alignment | SegmentAlignment
    if source.segmenting:
        alignment = align_segments(source.input_xml, source.gold_xml, source.unit_path)
        if max_units is not None:
            alignment = alignment.truncated(max_units)
    else:
        alignment = align_documents(source.input_xml, source.gold_xml, source.unit_path)
        if max_units is not None:
            alignment = Alignment(alignment.pairs[:max_units], alignment.unmatched_input, alignment.unmatched_gold)
    units = aligned_units(alignment)
    if not units:
        raise ValueError("No unit of the input version matches the reference version by its text")
    result = EvaluationResult(model=model, source=source, alignment=alignment, planned_runs=len(configs))

    baseline = _scores(alignment, None)
    baseline_records = [
        UnitRecord(
            model="baseline (input)", reference_stable_id=source.reference_stable_id, batching="-", mode="-", repeat=1,
            unit_index=u.index, fingerprint=u.fingerprint, batch_index=0, position=0, batch_size=1,
            status=ProposalStatus.OK.value, **asdict(baseline[u.index]),
        )
        for u in units
    ]
    result.baseline = summarize("baseline (input)", source.reference_stable_id, "-", "-", baseline_records, [], 1)

    cells: dict[tuple[str, str], tuple[list[UnitRecord], list[BatchRecord], int]] = {}
    for number, config in enumerate(configs, start=1):
        if progress:
            progress(number, len(configs), config)
        run = await correct_units(
            client, source.rules, units, config.spec, config.mode, concurrency=concurrency, retry=retry, segmenting=source.segmenting,
        )
        records = _records(model, source, config, alignment, run)
        result.units.extend(records)
        for batch in run.batches:
            result.batches.append({
                "model": model, "batching": config.spec.label, "mode": config.mode, "repeat": config.repeat,
                **asdict(batch),
            })
        key = (config.spec.label, config.mode)
        cell_records, cell_batches, cell_repeats = cells.get(key, ([], [], 0))
        cells[key] = (cell_records + records, cell_batches + run.batches, cell_repeats + 1)
        result.finished_runs = number
        result.summaries = [
            summarize(model, source.reference_stable_id, batching, mode, cell_records, cell_batches, repeats)
            for (batching, mode), (cell_records, cell_batches, repeats) in cells.items()
        ]
        if on_update:
            on_update(result)
    return result
