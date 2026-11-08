"""
The automated first annotation of a list of review units: two models correct the units
independently, units on which they agree take their common proposal, the others go to the arbiter.

    agreed      both models returned valid, canonically equal proposals
    arbitrated  the arbiter returned a valid unit (a candidate, the original, or its own correction)
    unresolved  no arbiter, or its answer failed the checks: the original unit is kept

For segmenting variants (units may be split and merged, see correction.correct_units) decisions
are made per group: the shortest runs of consecutive units after which both models' proposals
have the original's text. A group's proposal is the sequence of its units' proposals; the arbiter
sees a group as one unit and may answer with any number of elements.

Pure module: the models are ChatClients, the result is a decision per unit (or group). Writing the
result into a document is done by document.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Sequence

from .arbitration import (
    agreed_proposal,
    build_arbiter_system_prompt,
    distinct_candidates,
    render_candidates,
    segments_key,
    valid_proposal,
    which_candidate,
)
from .batching import BatchSpec
from .checks import ProposalStatus, unit_text
from .correction import ChatClient, CorrectionRun, RetryPolicy, Sleep, UnitOutcome, correct_units
from .prompts import OutputMode
from .units import Unit

class Decision(StrEnum):
    """How a unit's result was reached."""

    AGREED = "agreed"
    ARBITRATED = "arbitrated"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class PipelineConfig:
    """Batching and robustness settings, shared by the two models and the arbiter."""

    spec: BatchSpec = field(default_factory=lambda: BatchSpec(max_units=16))
    mode: OutputMode = "all"
    # The arbiter sees up to three versions per unit, so its batches are smaller by default
    arbiter_spec: BatchSpec = field(default_factory=lambda: BatchSpec(max_units=8))
    # Share of failed units above which a batch is split in half and retried (None: never)
    split_on_failure: float | None = 0.25
    concurrency: int = 1
    retry: RetryPolicy | None = field(default_factory=RetryPolicy)
    # Units may be split and merged (decided per group, see the module docstring)
    segmenting: bool = False


@dataclass
class UnitDecision:
    """The result for one unit (or a group of units of a segmenting variant) and how it was reached."""

    # The unit; for a group, a unit with the first unit's index and the group's XML
    unit: Unit
    decision: Decision
    result_xml: str
    # Proposals of the models by label (None: no valid proposal) and the status of each answer
    proposals: dict[str, str | None]
    statuses: dict[str, ProposalStatus]
    # Arbitrated units: the arbiter's answer status and which version it chose (label, "original", "own")
    arbiter_status: ProposalStatus | None = None
    chosen: str | None = None
    # Indices of the units of a group (empty: just unit.index)
    indices: tuple[int, ...] = ()

    @property
    def covered(self) -> tuple[int, ...]:
        """Indices of the original units the result replaces."""
        return self.indices or (self.unit.index,)

    @property
    def changed(self) -> bool:
        """Whether the result differs from the original unit (ignoring formatting)."""
        return segments_key(self.result_xml) != segments_key(self.unit.xml)

    @property
    def comment(self) -> str:
        """A short explanation of the decision, for the run log."""
        if self.decision == Decision.AGREED:
            return ""
        failed = ", ".join(f"{label}: {status.value}" for label, status in self.statuses.items() if status.is_failure or status == ProposalStatus.ERROR)
        suffix = f" ({failed})" if failed else ""
        if self.decision == Decision.ARBITRATED:
            return f"Models disagreed{suffix}; the arbiter chose {self.chosen}."
        reason = "no arbiter" if self.arbiter_status is None else f"arbiter: {self.arbiter_status.value}"
        return f"Models disagreed{suffix}; unresolved ({reason}), original kept."


@dataclass
class PipelineResult:
    """Decisions in unit order, and the correction runs (by "A", "B", "arbiter") for the run log."""

    decisions: list[UnitDecision]
    runs: dict[str, CorrectionRun]
    # Results may consist of any number of elements (segmenting variant)
    segmenting: bool = False

    def counts(self) -> dict[str, int]:
        """Number of units per decision, plus the number of changed units."""
        result = {d.value: 0 for d in Decision}
        for decision in self.decisions:
            result[decision.decision.value] += 1
        result["changed"] = sum(d.changed for d in self.decisions)
        return result


def _text_length(xml: str) -> int:
    try:
        return len(unit_text(xml))
    except ValueError:
        return 0


def segment_groups(units: Sequence[Unit], runs: Sequence[CorrectionRun]) -> list[list[int]]:
    """
    Positions of the units in groups: a group ends after a unit where the text of every run's
    proposals so far (the original for units without a valid proposal) equals the original's.
    """
    groups: list[list[int]] = []
    current: list[int] = []
    original = 0
    proposed = [0 for _ in runs]
    for position, unit in enumerate(units):
        current.append(position)
        original += _text_length(unit.xml)
        for r, run in enumerate(runs):
            proposal = valid_proposal(run.outcomes[position])
            proposed[r] += _text_length(proposal if proposal is not None else unit.xml)
        if all(p == original for p in proposed):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _group_status(outcomes: Sequence[UnitOutcome]) -> ProposalStatus:
    """The first failure of a group's outcomes, else OK (UNCHANGED if all are)."""
    for outcome in outcomes:
        if valid_proposal(outcome) is None:
            return outcome.status
    return ProposalStatus.UNCHANGED if all(o.status == ProposalStatus.UNCHANGED for o in outcomes) else ProposalStatus.OK


def _group_decision(units: Sequence[Unit], positions: Sequence[int], runs: dict[str, CorrectionRun]) -> UnitDecision:
    """A decision (agreed, or unresolved until the arbiter answers) for a group of units."""
    members = [units[p] for p in positions]
    group = members[0] if len(members) == 1 else Unit(members[0].index, "\n".join(u.xml for u in members), members[0].fingerprint)
    proposals: dict[str, str | None] = {}
    statuses: dict[str, ProposalStatus] = {}
    for label, run in runs.items():
        outcomes = [run.outcomes[p] for p in positions]
        statuses[label] = _group_status(outcomes)
        parts = [valid_proposal(o) for o in outcomes]
        proposals[label] = None if any(p is None for p in parts) else "\n".join(p for p in parts if p and p.strip())
    indices = tuple(u.index for u in members) if len(members) > 1 else ()
    keys = {segments_key(p) for p in proposals.values() if p is not None}
    if all(p is not None for p in proposals.values()) and len(keys) == 1 and None not in keys:
        agreed = next(p for p in proposals.values() if p is not None)
        return UnitDecision(group, Decision.AGREED, agreed, proposals, statuses, indices=indices)
    return UnitDecision(group, Decision.UNRESOLVED, group.xml, proposals, statuses, indices=indices)


async def annotate_units(
    units: Sequence[Unit],
    rules: Sequence[tuple[str, str]],
    model_a: ChatClient,
    model_b: ChatClient,
    arbiter: ChatClient | None,
    config: PipelineConfig | None = None,
    sleep: Sleep = asyncio.sleep,
) -> PipelineResult:
    """Run both models on all units (concurrently), then the arbiter on the units they disagree on."""
    config = config or PipelineConfig()

    run_a, run_b = await asyncio.gather(*(
        correct_units(
            client, rules, units, config.spec, config.mode, concurrency=config.concurrency,
            split_on_failure=config.split_on_failure, retry=config.retry, sleep=sleep, segmenting=config.segmenting,
        )
        for client in (model_a, model_b)
    ))
    runs: dict[str, CorrectionRun] = {"A": run_a, "B": run_b}

    decisions: list[UnitDecision] = []
    if config.segmenting:
        decisions = [_group_decision(units, positions, runs) for positions in segment_groups(units, [run_a, run_b])]
    else:
        for unit, a, b in zip(units, run_a.outcomes, run_b.outcomes, strict=True):
            outcomes: dict[str, UnitOutcome] = {"A": a, "B": b}
            proposals = {label: valid_proposal(o) for label, o in outcomes.items()}
            statuses = {label: o.status for label, o in outcomes.items()}
            agreed = agreed_proposal([a, b])
            decision = UnitDecision(unit, Decision.AGREED, agreed, proposals, statuses) if agreed is not None else UnitDecision(unit, Decision.UNRESOLVED, unit.xml, proposals, statuses)
            decisions.append(decision)
    disputed = [(d, distinct_candidates(d.unit, d.proposals)) for d in decisions if d.decision == Decision.UNRESOLVED]

    if arbiter is not None and disputed:
        candidates_by_index = {d.unit.index: c for d, c in disputed}
        arbiter_run = await correct_units(
            arbiter, rules, [d.unit for d, _ in disputed], config.arbiter_spec, "all", concurrency=config.concurrency,
            split_on_failure=config.split_on_failure, retry=config.retry, sleep=sleep,
            system_prompt=build_arbiter_system_prompt(config.segmenting),
            render=lambda u: render_candidates(u, candidates_by_index[u.index]),
            segmenting=config.segmenting, move_text=False,
        )
        runs["arbiter"] = arbiter_run
        for (decision, candidates), outcome in zip(disputed, arbiter_run.outcomes, strict=True):
            decision.arbiter_status = outcome.status
            result = valid_proposal(outcome)
            if result is not None:
                decision.decision = Decision.ARBITRATED
                decision.result_xml = result
                decision.chosen = which_candidate(result, decision.unit, candidates)
    return PipelineResult(decisions, runs, segmenting=config.segmenting)
