"""
Running one model over a list of review units: batching, prompting, parsing and checking.

This is the step the pipeline runs for each of its models (A, B, and the arbiter's input), and
the step the batch-size probe measures. The model is reached through a ChatClient, so tests and
the CLI (over HTTP) and the server (through an LLMProvider) plug in their own.

Pure module.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from .batching import BatchSpec, make_batches
from .checks import ProposalStatus, check_proposal
from .prompts import OutputMode, build_system_prompt, build_user_prompt, parse_unit_answer
from .units import Unit


class ChatClient(Protocol):
    """One model, ready to answer a system + user prompt."""

    async def complete(self, system_prompt: str, user_prompt: str) -> str: ...


class ChatProvider(Protocol):
    """The part of the core's LLMProvider that ProviderChatClient needs."""

    async def chat_completion(self, model_id: str, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str: ...


@dataclass
class ProviderChatClient:
    """ChatClient over a registered LLM provider (server side)."""

    provider: ChatProvider
    model_id: str
    temperature: float = 0.0

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        return await self.provider.chat_completion(self.model_id, system_prompt, user_prompt, self.temperature)


@dataclass
class UnitOutcome:
    """What a model did with one unit."""

    unit: Unit
    status: ProposalStatus
    # The proposed unit XML: the model's answer, or the input for UNCHANGED; None on failure
    proposal: str | None
    batch_index: int
    position: int  # 0-based position of the unit in its batch
    batch_size: int


@dataclass
class BatchRecord:
    """One request: sizes, timing and the raw answer (kept for inspecting failures)."""

    index: int
    unit_indices: list[int]
    prompt_chars: int
    response_chars: int
    seconds: float
    raw_response: str
    error: str | None = None
    # Index of the batch this one was split from by split_on_failure, else None
    split_from: int | None = None


@dataclass
class CorrectionRun:
    """Outcomes per unit (in unit order) and the requests that produced them."""

    outcomes: list[UnitOutcome] = field(default_factory=list)
    batches: list[BatchRecord] = field(default_factory=list)


def _failure_share(outcomes: Sequence[UnitOutcome]) -> float:
    return sum(o.status.is_failure for o in outcomes) / len(outcomes) if outcomes else 0.0


async def correct_units(
    client: ChatClient,
    rules: Sequence[tuple[str, str]],
    units: Sequence[Unit],
    spec: BatchSpec,
    mode: OutputMode = "all",
    concurrency: int = 1,
    split_on_failure: float | None = None,
) -> CorrectionRun:
    """
    Send the units to the model in batches and check every proposed unit.

    Units are numbered 1..n within each batch. A failed request (exception from the client) marks
    all of its units MISSING and is recorded with its error. With split_on_failure set (a share
    between 0 and 1), a batch of more than one unit whose share of failed units exceeds it is split
    in half and each half is retried, recursively; the experiment leaves it off to measure the raw
    behaviour, the pipeline turns it on.
    """
    run = CorrectionRun()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    system_prompt = build_system_prompt(mode)
    by_index: dict[int, UnitOutcome] = {}
    lock = asyncio.Lock()

    async def process(batch: list[Unit], split_from: int | None) -> None:
        ids = list(range(1, len(batch) + 1))
        user_prompt = build_user_prompt(rules, batch, ids)
        error: str | None = None
        raw = ""
        async with semaphore:
            started = time.monotonic()
            try:
                raw = await client.complete(system_prompt, user_prompt)
            except Exception as e:  # noqa: BLE001 - a failed request is a measured outcome, not a crash
                error = f"{type(e).__name__}: {e}"
            seconds = time.monotonic() - started
        async with lock:
            batch_index = len(run.batches)
            run.batches.append(BatchRecord(
                index=batch_index, unit_indices=[u.index for u in batch], prompt_chars=len(system_prompt) + len(user_prompt),
                response_chars=len(raw), seconds=seconds, raw_response=raw, error=error, split_from=split_from,
            ))
        answer = parse_unit_answer(raw) if error is None else {}
        outcomes: list[UnitOutcome] = []
        for position, (uid, unit) in enumerate(zip(ids, batch)):
            proposal = answer.get(uid)
            if proposal is None:
                status = ProposalStatus.UNCHANGED if mode == "changed" and error is None else ProposalStatus.MISSING
                proposal = unit.xml if status == ProposalStatus.UNCHANGED else None
            else:
                status = check_proposal(unit.xml, proposal)
                if status.is_failure:
                    proposal = None
            outcomes.append(UnitOutcome(unit, status, proposal, batch_index, position, len(batch)))
        if split_on_failure is not None and len(batch) > 1 and _failure_share(outcomes) > split_on_failure:
            half = len(batch) // 2
            await asyncio.gather(process(batch[:half], batch_index), process(batch[half:], batch_index))
            return
        for outcome in outcomes:
            by_index[outcome.unit.index] = outcome

    await asyncio.gather(*(process(batch, None) for batch in make_batches(units, spec)))
    run.outcomes = [by_index[u.index] for u in units]
    run.batches.sort(key=lambda b: b.index)
    return run
