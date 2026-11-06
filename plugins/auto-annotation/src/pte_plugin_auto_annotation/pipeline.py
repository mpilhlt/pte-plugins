"""
The automated first annotation of a list of review units: two models correct the units
independently, units on which they agree take their common proposal, the others go to the arbiter.

    agreed      both models returned valid, canonically equal proposals
    arbitrated  the arbiter returned a valid unit (a candidate, the original, or its own correction)
    unresolved  no arbiter, or its answer failed the checks: the original unit is kept

Pure module: the models are ChatClients, the result is a decision per unit. Writing the result into
a document is done by document.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Sequence

from .arbitration import (
    agreed_proposal,
    build_arbiter_system_prompt,
    canonical_key,
    distinct_candidates,
    render_candidates,
    valid_proposal,
    which_candidate,
)
from .batching import BatchSpec
from .checks import ProposalStatus
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


@dataclass
class UnitDecision:
    """The result for one unit and how it was reached."""

    unit: Unit
    decision: Decision
    result_xml: str
    # Proposals of the models by label (None: no valid proposal) and the status of each answer
    proposals: dict[str, str | None]
    statuses: dict[str, ProposalStatus]
    # Arbitrated units: the arbiter's answer status and which version it chose (label, "original", "own")
    arbiter_status: ProposalStatus | None = None
    chosen: str | None = None

    @property
    def changed(self) -> bool:
        """Whether the result differs from the original unit (ignoring formatting)."""
        return canonical_key(self.result_xml) != canonical_key(self.unit.xml)

    @property
    def comment(self) -> str:
        """A short explanation for the unit review mark."""
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

    def counts(self) -> dict[str, int]:
        """Number of units per decision, plus the number of changed units."""
        result = {d.value: 0 for d in Decision}
        for decision in self.decisions:
            result[decision.decision.value] += 1
        result["changed"] = sum(d.changed for d in self.decisions)
        return result


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
            split_on_failure=config.split_on_failure, retry=config.retry, sleep=sleep,
        )
        for client in (model_a, model_b)
    ))
    runs: dict[str, CorrectionRun] = {"A": run_a, "B": run_b}

    decisions: list[UnitDecision] = []
    disputed: list[tuple[UnitDecision, dict[str, str]]] = []
    for unit, a, b in zip(units, run_a.outcomes, run_b.outcomes, strict=True):
        outcomes: dict[str, UnitOutcome] = {"A": a, "B": b}
        proposals = {label: valid_proposal(o) for label, o in outcomes.items()}
        statuses = {label: o.status for label, o in outcomes.items()}
        agreed = agreed_proposal([a, b])
        if agreed is not None:
            decisions.append(UnitDecision(unit, Decision.AGREED, agreed, proposals, statuses))
            continue
        decision = UnitDecision(unit, Decision.UNRESOLVED, unit.xml, proposals, statuses)
        decisions.append(decision)
        disputed.append((decision, distinct_candidates(unit, proposals)))

    if arbiter is not None and disputed:
        candidates_by_index = {d.unit.index: c for d, c in disputed}
        arbiter_run = await correct_units(
            arbiter, rules, [d.unit for d, _ in disputed], config.arbiter_spec, "all", concurrency=config.concurrency,
            split_on_failure=config.split_on_failure, retry=config.retry, sleep=sleep,
            system_prompt=build_arbiter_system_prompt(),
            render=lambda u: render_candidates(u, candidates_by_index[u.index]),
        )
        runs["arbiter"] = arbiter_run
        for (decision, candidates), outcome in zip(disputed, arbiter_run.outcomes, strict=True):
            decision.arbiter_status = outcome.status
            result = valid_proposal(outcome)
            if result is not None:
                decision.decision = Decision.ARBITRATED
                decision.result_xml = result
                decision.chosen = which_candidate(result, decision.unit, candidates)
    return PipelineResult(decisions, runs)
