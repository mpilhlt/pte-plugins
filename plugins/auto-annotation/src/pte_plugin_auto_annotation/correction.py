"""
Running one model over a list of review units: batching, prompting, parsing and checking.

This is the step the pipeline runs for each of its models (A, B, and the arbiter's input), and
the step the batch-size probe measures. The model is reached through a ChatClient, so tests and
the CLI (over HTTP) and the server (through an LLMProvider) plug in their own.

Pure module.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from .batching import BatchSpec, make_batches
from .checks import ProposalStatus, check_proposal, check_segments, unit_text
from .prompts import OutputMode, UnitRenderer, build_system_prompt, build_user_prompt, parse_unit_answer
from .units import Unit


class TransientError(RuntimeError):
    """A request failed for a reason unrelated to the prompt (overload, rate limit); retrying may help."""


# HTTP statuses and wordings with which providers report overload and rate limits
_TRANSIENT = re.compile(
    r"\b(429|500|502|503|504)\b|overload|high demand|rate.?limit|too many requests|resource.?exhausted|temporarily unavailable|try again",
    re.IGNORECASE,
)


def is_transient_message(message: str) -> bool:
    """Whether a provider error message reports a temporary condition."""
    return bool(_TRANSIENT.search(message))


@dataclass(frozen=True)
class RetryPolicy:
    """Retries of requests failing with TransientError, with exponential backoff and jitter."""

    retries: int = 4
    base_delay: float = 2.0
    max_delay: float = 60.0

    def delay(self, attempt: int) -> float:
        """Seconds to wait before retry number `attempt` (1-based)."""
        return min(self.max_delay, self.base_delay * 2 ** (attempt - 1)) * random.uniform(0.75, 1.25)


Sleep = Callable[[float], Awaitable[None]]


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
        try:
            return await self.provider.chat_completion(self.model_id, system_prompt, user_prompt, self.temperature)
        except Exception as e:
            if is_transient_message(str(e)):
                raise TransientError(str(e)) from e
            raise


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
    # Retries after transient errors (see RetryPolicy)
    retries: int = 0


@dataclass
class CorrectionRun:
    """Outcomes per unit (in unit order) and the requests that produced them."""

    outcomes: list[UnitOutcome] = field(default_factory=list)
    batches: list[BatchRecord] = field(default_factory=list)


def _failure_share(outcomes: Sequence[UnitOutcome]) -> float:
    return sum(o.status.is_failure for o in outcomes) / len(outcomes) if outcomes else 0.0


def _accept_moved_text(outcomes: list[UnitOutcome]) -> list[UnitOutcome]:
    """
    Accept the units of a batch whose text changed if the batch as a whole keeps its text, i.e. the
    model only moved text between neighbouring units (segmenting variants).
    """
    if not any(o.status == ProposalStatus.TEXT_CHANGED for o in outcomes):
        return outcomes
    if any(o.status not in (ProposalStatus.OK, ProposalStatus.UNCHANGED, ProposalStatus.TEXT_CHANGED) for o in outcomes):
        return outcomes
    proposed = "".join(unit_text(o.proposal or "") for o in outcomes)
    original = "".join(unit_text(o.unit.xml) for o in outcomes)
    if proposed != original:
        return outcomes
    for o in outcomes:
        if o.status == ProposalStatus.TEXT_CHANGED:
            o.status = ProposalStatus.OK
    return outcomes


async def correct_units(
    client: ChatClient,
    rules: Sequence[tuple[str, str]],
    units: Sequence[Unit],
    spec: BatchSpec,
    mode: OutputMode = "all",
    concurrency: int = 1,
    split_on_failure: float | None = None,
    retry: RetryPolicy | None = None,
    sleep: Sleep = asyncio.sleep,
    segmenting: bool = False,
    system_prompt: str | None = None,
    render: UnitRenderer | None = None,
) -> CorrectionRun:
    """
    Send the units to the model in batches and check every proposed unit.

    Units are numbered 1..n within each batch. A request failing with TransientError is retried
    as `retry` allows (the wait does not count towards the request's seconds). A failed request
    marks all of its units ERROR and is recorded with its error.

    With segmenting (variants whose model splits text into units), a unit may come back as several
    elements or empty; units whose own text changed are accepted when the batch keeps its text as
    a whole (text moved between neighbouring units) and no unit of the batch is malformed or missing. With split_on_failure set (a share
    between 0 and 1), a batch of more than one unit whose share of failed units exceeds it is split
    in half and each half is retried, recursively; the experiment leaves it off to measure the raw
    behaviour, the pipeline turns it on.

    system_prompt and render replace the correction prompt and the text shown for each unit; the
    arbiter uses them to show the candidates. Answers are always checked against the unit's XML.
    """
    run = CorrectionRun()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    system_prompt = system_prompt if system_prompt is not None else build_system_prompt(mode, segmenting)
    by_index: dict[int, UnitOutcome] = {}
    lock = asyncio.Lock()

    async def process(batch: list[Unit], split_from: int | None) -> None:
        ids = list(range(1, len(batch) + 1))
        user_prompt = build_user_prompt(rules, batch, ids, render) if render else build_user_prompt(rules, batch, ids)
        error: str | None = None
        raw = ""
        retries = 0
        seconds = 0.0
        async with semaphore:
            while True:
                started = time.monotonic()
                try:
                    raw = await client.complete(system_prompt, user_prompt)
                    error = None
                except Exception as e:  # noqa: BLE001 - a failed request is a measured outcome, not a crash
                    error = f"{type(e).__name__}: {e}"
                    transient = isinstance(e, TransientError)
                else:
                    transient = False
                seconds += time.monotonic() - started
                if not (transient and retry is not None and retries < retry.retries):
                    break
                retries += 1
                await sleep(retry.delay(retries))
        async with lock:
            batch_index = len(run.batches)
            run.batches.append(BatchRecord(
                index=batch_index, unit_indices=[u.index for u in batch], prompt_chars=len(system_prompt) + len(user_prompt),
                response_chars=len(raw), seconds=seconds, raw_response=raw, error=error, split_from=split_from, retries=retries,
            ))
        answer = parse_unit_answer(raw) if error is None else {}
        outcomes: list[UnitOutcome] = []
        check = check_segments if segmenting else check_proposal
        for position, (uid, unit) in enumerate(zip(ids, batch)):
            proposal = answer.get(uid)
            if error is not None:
                status = ProposalStatus.ERROR
            elif proposal is None:
                status = ProposalStatus.UNCHANGED if mode == "changed" else ProposalStatus.MISSING
                proposal = unit.xml if status == ProposalStatus.UNCHANGED else None
            else:
                status = check(unit.xml, proposal)
            outcomes.append(UnitOutcome(unit, status, proposal, batch_index, position, len(batch)))
        if segmenting:
            outcomes = _accept_moved_text(outcomes)
        for outcome in outcomes:
            if outcome.status.is_failure:
                outcome.proposal = None
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
