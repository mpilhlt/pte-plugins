"""
Correcting zone documents (variants whose extractor declares `zoneLabels`, such as
`grobid.training.segmentation`) by page.

The children of `<text>` of a zone document are zones that partition one text stream; correcting
it means relabeling zones and moving their boundaries, which the unit checks of checks.py do not
allow. So the review units of these documents are pages of the document's line layout, and the
model is given windows of whole pages (`pages:4` by default) as numbered lines with zone labels and
layout hints (see the editor's `fastapi_app/lib/tei/zone_lines.py`), with a few lines of context
before and after. It answers in the plain line form:

    0413-0420 body
    0412 | [page]12 [note:headnote]Iacino et al., Text and Data Mining

In mode `all` the answer covers every line of the window, in mode `changed` only the lines whose
labels change (or `NO CHANGES`). Since the model never writes XML, an answer cannot be malformed;
a page fails with TEXT_CHANGED when the text of one of its piece lines differs from the line, and
with BAD_LABELS when the answer has unknown labels, overlapping changes or (mode `all`) leaves out
lines of the page. A window whose pages fail is split in half and retried: by pages, then a single
page by lines.

A page's proposal is its changes in the line form, one per line ('' for none); the changes of all
pages are applied to the document in one `zone_lines.apply_labels()` call.

The pure part of this module needs only the editor's zone_lines module (plugin API 1.5); without
it, zone documents are corrected unit by unit as other documents.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from .batching import BatchSpec, make_batches
from .checks import ProposalStatus
from .correction import BatchRecord, ChatClient, CorrectionRun, RetryPolicy, Sleep, UnitOutcome, send_request
from .prompts import NO_CHANGES, OutputMode
from .units import Unit

try:
    from fastapi_app.lib.tei import zone_lines

    ZONES_AVAILABLE = True
except ImportError:  # editor older than plugin API 1.5
    zone_lines = None  # type: ignore[assignment]
    ZONES_AVAILABLE = False

DEFAULT_PAGE_BATCH = "pages:4"
# The output mode of "Correct annotations in new version" for zone documents
CORRECTION_MODE: OutputMode = "all"
# A page is split by lines down to parts of this many lines
MIN_SPLIT_LINES = 4


@dataclass(frozen=True)
class ZoneLayout:
    """What a zone document is read with: the variant's zone labels and the document's line layout."""

    labels: list[dict[str, object]]  # `zoneLabels` entries: {code, element, attributes}
    line_pages: list[int]
    hints: list[list[str]] = field(default_factory=list)


@dataclass
class ZoneDoc:
    """A zone document read as lines and pages; `units` are its pages as review units."""

    xml: str
    labels: list["zone_lines.ZoneLabel"]
    doc: "zone_lines.ZoneDocument"
    pages: list["zone_lines.Page"]
    hints: list[list[str]]
    units: list[Unit]

    @property
    def width(self) -> int:
        """Digits of the line ids."""
        return zone_lines.line_id_width(len(self.doc.lines))

    def hints_of(self, line: int) -> list[str]:
        return self.hints[line] if line < len(self.hints) else []


def read_zone_doc(xml: str, layout: ZoneLayout) -> ZoneDoc:
    """
    The pages of a zone document; a page's unit XML is its listing (without header and context).

    Raises:
        ValueError: the document cannot be read as zones.
    """
    labels = zone_lines.zone_labels(layout.labels)
    doc = zone_lines.read_zone_document(xml, labels)
    pages = zone_lines.pages_of(doc, layout.line_pages)
    width = zone_lines.line_id_width(len(doc.lines))
    hints = [list(h) for h in layout.hints]
    units = [
        Unit(
            index=page.index,
            xml="\n".join(zone_lines.format_line(doc, i, width, hints[i] if i < len(hints) else ()) for i in page.lines),
            fingerprint=page.fingerprint,
        )
        for page in pages
    ]
    return ZoneDoc(xml, labels, doc, pages, hints, units)


# ── Prompts ──────────────────────────────────────────────────────────


def build_system_prompt(mode: OutputMode) -> str:
    """Instructions for correcting the zone labels of a page window."""
    if mode == "all":
        answer = (
            "Return the labels of every line of the pages (not of the context), in order, one entry per row: "
            "`<first id>-<last id> <label>` for consecutive lines with the same label, `<id> <label>` for a single line, "
            "and `<id> | [<label>]<text> [<label>]<text>` for a line whose text lies in several zones. Leave out no line."
        )
    else:
        answer = (
            "Return only the lines whose labels you change, one entry per row: `<first id>-<last id> <label>` for "
            "consecutive lines that get the same label, `<id> <label>` for a single line, and "
            "`<id> | [<label>]<text> [<label>]<text>` for a line whose text lies in several zones. If no label needs "
            f'a change, answer with the single line "{NO_CHANGES}".'
        )
    return (
        "You correct the segmentation of a document into zones, produced by a machine-learning tool. The document's "
        "text is divided into zones such as the header, the body, footnotes, running headers and page numbers. You "
        "are given annotation rules, each labeled with its category, the table of zone labels, the table of layout "
        "hints, and pages of the document as numbered lines.\n\n"
        "Each line of the listing reads `<id> <label> {<hints>} | <text>`: the zone label of the line, layout hints "
        "(only where they apply) and the line's text. A line whose text lies in several zones has no label after its "
        "id; instead, every part of its text starts with its label in brackets: "
        "`<id> {<hints>} | [page]12 [note:headnote]Running title`. The label `-` marks text outside any zone. Lines "
        "under a \"Context\" heading are only shown for orientation: do not return them.\n\n"
        "Correct the labels so that they follow the rules. Use only the labels of the table. In the pieces form, copy "
        "the line's text exactly and only insert the label markers: never change the text.\n\n"
        f"{answer} Example:\n0412 | [page]12 [note:headnote]Iacino et al., Text and Data Mining\n0413-0420 body\n\n"
        "Do not wrap the answer in a code fence and write nothing else."
    )


def build_user_prompt(rules: Sequence[tuple[str, str]], zdoc: ZoneDoc, window: Sequence["zone_lines.Page"]) -> str:
    """Rules, label and hint tables, then the window's listing with its context lines."""
    rule_text = "\n\n".join(f"## Rule category: {category}\n\n{text}" for category, text in rules)
    listing = zone_lines.format_listing(zdoc.doc, window, zdoc.hints)
    return (
        f"{rule_text}\n\n## Zone labels\n\n{zone_lines.format_label_table(zdoc.labels)}\n\n"
        f"## Layout hints\n\n{zone_lines.format_hint_table()}\n\n## Document lines\n\n{listing}"
    )


# ── Answers ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PageJudgement:
    """The checked answer for one page (or part of a page) of a window."""

    status: ProposalStatus
    changes: list["zone_lines.LabelChange"]


def _clip(change: "zone_lines.LabelChange", lines: range) -> "zone_lines.LabelChange | None":
    """A range change limited to `lines`; a piece change outside them is None."""
    first, last = max(change.first, lines.start), min(change.last, lines.stop - 1)
    if first > last:
        return None
    return replace(change, first=first, last=last)


def _page_parts(change: "zone_lines.LabelChange", window: Sequence["zone_lines.Page"]) -> list[tuple[int, "zone_lines.LabelChange"]]:
    """The change split at page boundaries, with the position of each part's page in the window."""
    parts: list[tuple[int, zone_lines.LabelChange]] = []
    for position, page in enumerate(window):
        part = _clip(change, page.lines)
        if part is not None:
            parts.append((position, part))
    return parts


def judge_answer(zdoc: ZoneDoc, window: Sequence["zone_lines.Page"], raw: str, mode: OutputMode) -> list[PageJudgement]:
    """
    Check the answer for a window: one judgement per page of the window, in order. Changes of
    context lines are ignored, ranges are cut at page boundaries.
    """
    lines = range(window[0].first_line, window[-1].end_line)
    parsed = zone_lines.parse_answer(raw)
    parts: list[tuple[int, zone_lines.LabelChange]] = []
    for change in parsed:
        clipped = _clip(change, lines)
        if clipped is not None:
            parts.extend(_page_parts(clipped, window))
    failed: dict[int, ProposalStatus] = {}
    page_of = {line: position for position, page in enumerate(window) for line in page.lines}
    valid, problems = zone_lines.check_changes(zdoc.doc, [c for _, c in parts], lines)
    covered: set[int] = set()
    for problem in problems:
        position = page_of[problem.change.first]
        if problem.reason == "noop":
            covered.update(range(problem.change.first, problem.change.last + 1))
        elif problem.reason == "text":
            failed.setdefault(position, ProposalStatus.TEXT_CHANGED)
        else:
            failed[position] = ProposalStatus.BAD_LABELS
    for change in valid:
        covered.update(range(change.first, change.last + 1))
    unparsable = not parsed and raw.strip() and NO_CHANGES not in raw.upper()
    judgements: list[PageJudgement] = []
    for position, page in enumerate(window):
        own = [c for c in valid if page_of[c.first] == position]
        if own:
            # as few changes as possible: ranges are cut to the lines whose labels change
            own = zone_lines.changes_between(zdoc.doc, zone_lines.changed_pieces(zdoc.doc, own), page.lines)
        status = failed.get(position)
        if status is None and mode == "all":
            missing = [i for i in page.lines if zdoc.doc.lines[i].pieces and i not in covered]
            status = ProposalStatus.BAD_LABELS if missing else None
        if status is None and mode == "changed" and unparsable:
            status = ProposalStatus.BAD_LABELS
        if status is not None:
            judgements.append(PageJudgement(status, []))
        elif not own and mode == "changed":
            judgements.append(PageJudgement(ProposalStatus.UNCHANGED, []))
        else:
            judgements.append(PageJudgement(ProposalStatus.OK, own))
    return judgements


def format_changes(zdoc: ZoneDoc, changes: Sequence["zone_lines.LabelChange"]) -> str:
    """A page's proposal: its changes in the line form, one per row."""
    return "\n".join(change.format(zdoc.width) for change in changes)


def parse_page_changes(zdoc: ZoneDoc, page_index: int, text: str) -> tuple[list["zone_lines.LabelChange"], list[str]]:
    """
    The changes of a page's proposal (as format_changes() writes it), checked against the page.

    Returns:
        (valid changes, problems)
    """
    page = zdoc.pages[page_index]
    rows = [row for row in text.splitlines() if row.strip()]
    parsed = zone_lines.parse_answer(text)
    problems = [f"Page {page.number}: unreadable change" for _ in range(len(rows) - len(parsed))]
    valid, dropped = zone_lines.check_changes(zdoc.doc, parsed, page.lines)
    problems.extend(f"Page {page.number}: {p.change.format(zdoc.width)} ({p.reason})" for p in dropped if p.reason != "noop")
    return valid, problems


# ── Running a model ──────────────────────────────────────────────────


def _split(window: list["zone_lines.Page"]) -> tuple[list["zone_lines.Page"], list["zone_lines.Page"]] | None:
    """Halves of a window: by pages, or a single page by lines; None if it cannot be split further."""
    if len(window) > 1:
        half = len(window) // 2
        return window[:half], window[half:]
    page = window[0]
    if page.end_line - page.first_line < 2 * MIN_SPLIT_LINES:
        return None
    middle = (page.first_line + page.end_line) // 2
    return [replace(page, end_line=middle)], [replace(page, first_line=middle)]


async def correct_pages(
    client: ChatClient,
    rules: Sequence[tuple[str, str]],
    zdoc: ZoneDoc,
    units: Sequence[Unit],
    spec: BatchSpec,
    mode: OutputMode = "all",
    concurrency: int = 1,
    split_on_failure: float | None = None,
    retry: RetryPolicy | None = None,
    sleep: Sleep = asyncio.sleep,
    time_budget: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CorrectionRun:
    """
    Send the pages (`units`, a subset of `zdoc.units`) to the model in windows of consecutive pages
    and check the answers (see the module doc). The outcome of a page has its changes in the line
    form as proposal ('' for none; None on failure). Requests are retried and limited as in
    correction.correct_units().
    """
    run = CorrectionRun()
    deadline = None if time_budget is None else clock() + time_budget

    def out_of_time() -> bool:
        return deadline is not None and clock() >= deadline

    semaphore = asyncio.Semaphore(max(1, concurrency))
    system_prompt = build_system_prompt(mode)
    lock = asyncio.Lock()
    # page index -> (first line of the part, judgement, batch index, position, batch size)
    parts: dict[int, list[tuple[int, PageJudgement, int, int, int]]] = {}

    async def process(window: list[zone_lines.Page], split_from: int | None) -> None:
        user_prompt = build_user_prompt(rules, zdoc, window)
        async with semaphore:
            raw, error, retries, seconds = await send_request(client, system_prompt, user_prompt, retry, sleep, out_of_time, time_budget)
        async with lock:
            batch_index = len(run.batches)
            run.batches.append(BatchRecord(
                index=batch_index, unit_indices=[p.index for p in window], prompt_chars=len(system_prompt) + len(user_prompt),
                response_chars=len(raw), seconds=seconds, raw_response=raw, error=error, split_from=split_from, retries=retries,
            ))
        if error is not None:
            judgements = [PageJudgement(ProposalStatus.ERROR, []) for _ in window]
        else:
            judgements = judge_answer(zdoc, window, raw, mode)
        failures = sum(j.status.is_failure for j in judgements)
        halves = _split(window) if split_on_failure is not None and error is None and failures / len(window) > split_on_failure else None
        if halves is not None:
            await asyncio.gather(process(halves[0], batch_index), process(halves[1], batch_index))
            return
        for position, (page, judgement) in enumerate(zip(window, judgements)):
            parts.setdefault(page.index, []).append((page.first_line, judgement, batch_index, position, len(window)))

    windows = make_batches(list(units), spec)
    by_index = {page.index: page for page in zdoc.pages}
    await asyncio.gather(*(process([by_index[u.index] for u in w], None) for w in windows))
    for unit in units:
        page_parts = sorted(parts[unit.index], key=lambda p: p[0])
        statuses = [p[1].status for p in page_parts]
        failure = next((s for s in statuses if s.is_failure or s == ProposalStatus.ERROR), None)
        changes = [c for p in page_parts for c in p[1].changes]
        _, _, batch_index, position, size = page_parts[0]
        if failure is not None:
            status, proposal = failure, None
        else:
            status = ProposalStatus.UNCHANGED if all(s == ProposalStatus.UNCHANGED for s in statuses) else ProposalStatus.OK
            proposal = format_changes(zdoc, changes)
        run.outcomes.append(UnitOutcome(unit, status, proposal, batch_index, position, size))
    run.batches.sort(key=lambda b: b.index)
    return run


def apply_page_changes(zdoc: ZoneDoc, proposals: Mapping[int, str]) -> tuple[str, list[str]]:
    """
    The document with the changes of the pages' proposals (page index -> changes in line form).

    Returns:
        (new XML, problems); with problems, the XML is the original.
    """
    changes: list[zone_lines.LabelChange] = []
    problems = [f"No page with index {index}" for index in proposals if not 0 <= index < len(zdoc.pages)]
    if problems:
        return zdoc.xml, problems
    for index in sorted(proposals):
        valid, page_problems = parse_page_changes(zdoc, index, proposals[index])
        changes.extend(valid)
        problems.extend(page_problems)
    if problems:
        return zdoc.xml, problems
    if not changes:
        return zdoc.xml, []
    return zone_lines.apply_labels(zdoc.xml, zdoc.labels, changes), []
