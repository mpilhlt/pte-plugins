"""
Correcting the review units of a document with a single model, one batch per request, so that a
client can show the progress and stop between batches (the "Correct annotations in new version"
action, see correction_routes.py).

A unit takes the model's proposal if it passes the checks of checks.py; otherwise it keeps the
original. For segmenting variants, text may move between neighbouring units of a batch (see
correction.correct_units), so a unit may become several elements or none.

Pure module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .arbitration import segments_key, valid_proposal
from .batching import BatchSpec, make_batches
from .checks import ProposalStatus, check_proposal, check_segments, unit_text
from .correction import ChatClient, RetryPolicy, Sleep, correct_units
from .units import Unit


@dataclass(frozen=True)
class UnitCorrection:
    """The result for one unit."""

    index: int
    status: ProposalStatus
    # The corrected unit if the model's proposal is valid and differs from the original, else None.
    # With segmenting, it may consist of several elements, or be empty (the unit's text moved to a neighbour).
    xml: str | None


@dataclass(frozen=True)
class BatchCorrection:
    """The results for the units of one batch and the requests it took."""

    units: list[UnitCorrection]
    # Error of a request that failed (also after retries); its units have the status ERROR
    error: str | None
    calls: int
    retries: int
    seconds: float


@dataclass(frozen=True)
class CorrectionSummary:
    """Counts of a correction run, recorded in the revision change."""

    units: int  # review units of the document
    processed: int  # units sent to the model (fewer if the run was stopped)
    changed: int
    invalid: int  # answers that failed the checks or were missing
    errors: int  # units of requests that failed

    def measures(self) -> list[tuple[str, int]]:
        """(subtype, quantity) of the `measure` elements of the revision change."""
        unchanged = max(0, self.processed - self.changed - self.invalid - self.errors)
        return [
            ("changed", self.changed), ("unchanged", unchanged), ("invalid", self.invalid),
            ("error", self.errors), ("unprocessed", max(0, self.units - self.processed)),
        ]


def plan_batches(units: Sequence[Unit], spec: BatchSpec) -> list[list[int]]:
    """The unit indices of each batch, in document order."""
    return [[unit.index for unit in batch] for batch in make_batches(units, spec)]


async def correct_batch(
    client: ChatClient,
    rules: Sequence[tuple[str, str]],
    units: Sequence[Unit],
    segmenting: bool = False,
    split_on_failure: float | None = 0.25,
    retry: RetryPolicy | None = None,
    sleep: Sleep = asyncio.sleep,
) -> BatchCorrection:
    """
    Send the units to the model as one batch (split in half and retried where too many of its
    units fail, see correction.correct_units) and keep the valid proposals that change a unit.
    """
    run = await correct_units(
        client, rules, units, BatchSpec(max_units=max(1, len(units))), "all",
        split_on_failure=split_on_failure, retry=retry if retry is not None else RetryPolicy(), sleep=sleep, segmenting=segmenting,
    )
    results: list[UnitCorrection] = []
    for outcome in run.outcomes:
        proposal = valid_proposal(outcome)
        changed = proposal is not None and segments_key(proposal) != segments_key(outcome.unit.xml)
        results.append(UnitCorrection(outcome.unit.index, outcome.status, proposal if changed else None))
    error = next((b.error for b in run.batches if b.error), None)
    return BatchCorrection(
        results, error, calls=len(run.batches), retries=sum(b.retries for b in run.batches),
        seconds=round(sum(b.seconds for b in run.batches), 2),
    )


def check_corrections(units: Sequence[Unit], corrections: Mapping[int, str], segmenting: bool = False) -> list[str]:
    """
    Problems of corrections sent back by a client (index -> unit XML): unknown units, and
    corrections that fail the checks. With segmenting, a correction may change a unit's text as
    long as the text of all units together stays the same.
    """
    by_index = {unit.index: unit for unit in units}
    problems = [f"No unit with index {index}" for index in corrections if index not in by_index]
    if problems:
        return problems
    allowed = (ProposalStatus.OK, ProposalStatus.TEXT_CHANGED) if segmenting else (ProposalStatus.OK,)
    for index, xml in corrections.items():
        status = (check_segments if segmenting else check_proposal)(by_index[index].xml, xml)
        if status not in allowed:
            problems.append(f"Unit {index}: {status.value}")
    if segmenting and not problems:
        before = "".join(unit_text(unit.xml) for unit in units)
        texts = (corrections.get(unit.index, unit.xml) for unit in units)
        after = "".join(unit_text(xml) for xml in texts if xml.strip())
        if before != after:
            problems.append("The corrections change the text of the document")
    return problems
