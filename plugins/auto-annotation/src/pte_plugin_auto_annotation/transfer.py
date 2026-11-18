"""
Transferring the annotation decisions of an earlier version (the source) to another version of the
same document and variant whose text differs (the target), typically a new extraction after text
recognition (mpilhlt/pdf-tei-editor#822). The target's text is never changed.

1. The texts of source and target are aligned by character (alignment.py).
2. Every review unit of the target is planned (`plan_unit_transfer()`, `plan_zone_transfer()`):

   - `copied`: the source's decision applies without a model call. For unit documents: the unit
     corresponds to exactly one source unit with the same text and `<lb/>`s, whose XML passes the
     checks against the target unit. For zone documents (pages): every character of the page takes
     the label of its aligned source character, inserted text the label of its neighbours.
   - `model`: the unit and the corresponding part of the source (its fragment) go to the model,
     which reproduces the source's decisions on the target's text.
   - `unmatched`: less than half of the unit's characters are aligned; it is kept as extracted.

3. Optionally, the model adds doubts: a short rationale where it is sure that the earlier decision
   violates the rules. Copied units are then sent in a doubts-only mode, so that they are checked too.

The plan is deterministic, so every request of a job computes it again instead of storing it.

Pure module apart from the zone part, which needs the editor's zone_lines module (plugin API 1.5).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from lxml import etree

from .alignment import Alignment, align_texts
from .batching import BatchSpec, make_batches
from .checks import ProposalStatus, check_proposal, check_segments, unit_text
from .correction import ChatClient, RetryPolicy, Sleep, send_request
from .prompts import parse_unit_answer
from .units import Unit, canonical_form, element_text, find_text_element, find_unit_elements, local_name, parse_document, parse_unit, serialize_unit
from .zones import ZONES_AVAILABLE, ZoneDoc, judge_answer, zone_lines

COPIED = "copied"
MODEL = "model"
UNMATCHED = "unmatched"
CATEGORIES = (COPIED, MODEL, UNMATCHED)

# A unit with a smaller share of aligned characters has no counterpart in the source
MIN_ALIGNED_SHARE = 0.5
# Below this share of the source's characters aligned to the target, the versions are not the same text
MIN_COVERAGE = 0.5
# Projected label runs shorter than this without an identical run in the source are sent to the model
MIN_LABEL_RUN = 3
DEFAULT_UNIT_BATCH = "units:8"
DEFAULT_PAGE_BATCH = "pages:2"
DEFAULT_DOUBT_UNIT_BATCH = "units:16"
DEFAULT_DOUBT_PAGE_BATCH = "pages:4"
NO_DOUBTS = "NO DOUBTS"
CHANGE_LABEL = "Annotation transfer"

_DOUBT_LINE_RE = re.compile(r"^\s*DOUBT\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_DOUBT_HEADER_RE = re.compile(r"^###\s*Doubt\s+(\d+)\s*$", re.MULTILINE | re.IGNORECASE)
_LINE_DOUBT_RE = re.compile(r"^\s*DOUBT\s+S?(\d+)(?:\s*[-–]\s*S?(\d+))?\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class UnitTransfer:
    """The plan for one unit (a review unit, or a page of a zone document) of the target."""

    index: int
    category: str
    # unit documents: the XML of the corresponding source unit(s), '' if unmatched
    fragment: str = ""
    # copied units: the unit's result (unit XML, or for pages the label changes in the line form; '' for none)
    proposal: str | None = None
    # zone documents: the source lines [first, end) that the page corresponds to
    source_lines: tuple[int, int] | None = None


@dataclass
class TransferPlan:
    """The plan for all units of the target, in order."""

    units: list[UnitTransfer]
    coverage: float
    # zone documents: the source read as zones, for the source listing of the prompts
    source_zones: "zone_lines.ZoneDocument | None" = None

    def counts(self) -> dict[str, int]:
        """Units per category."""
        return {category: sum(u.category == category for u in self.units) for category in CATEGORIES}

    def indices(self, category: str) -> list[int]:
        """The indices of the units of a category, in order."""
        return [u.index for u in self.units if u.category == category]

    def by_index(self) -> dict[int, UnitTransfer]:
        return {u.index: u for u in self.units}


class TransferError(ValueError):
    """The source cannot be transferred to the target."""


def check_coverage(plan: TransferPlan) -> None:
    """
    Raises:
        TransferError: too little of the source's text is aligned to the target.
    """
    if plan.coverage < MIN_COVERAGE:
        raise TransferError(
            f"The versions do not seem to contain the same text: only {plan.coverage:.0%} of the earlier version's text was found in the current one"
        )


def consecutive_batches(indices: Sequence[int], units: Sequence[Unit], spec: BatchSpec) -> list[list[int]]:
    """Batches of the given units in which the units are consecutive in the document."""
    by_index = {u.index: u for u in units}
    runs: list[list[Unit]] = []
    for index in indices:
        if runs and runs[-1][-1].index == index - 1:
            runs[-1].append(by_index[index])
        else:
            runs.append([by_index[index]])
    return [[u.index for u in batch] for run in runs for batch in make_batches(run, spec)]


# ── Unit documents ───────────────────────────────────────────────────


@dataclass
class TextStream:
    """The text of `<text>` with whitespace, and where each unit lies in its character stream."""

    text: str
    # [start, end) of each unit in the stream of non-whitespace characters
    unit_ranges: list[tuple[int, int]]
    # stream positions of the <lb/>s of each unit, relative to the unit's start
    unit_lbs: list[list[int]]


def text_stream(root: etree._Element, units: Sequence[etree._Element]) -> TextStream:
    """The text stream of a document's `<text>` with the ranges of the given unit elements."""
    unit_of = {id(element): i for i, element in enumerate(units)}
    starts: dict[int, int] = {}
    ranges: list[tuple[int, int]] = [(0, 0)] * len(units)
    lbs: list[list[int]] = [[] for _ in units]
    parts: list[str] = []
    count = 0
    current: list[int] = []

    def emit(text: str | None) -> None:
        nonlocal count
        if text:
            parts.append(text)
            count += sum(not c.isspace() for c in text)

    def walk(element: etree._Element) -> None:
        unit = unit_of.get(id(element))
        if unit is not None:
            starts[unit] = count
            current.append(unit)
        if isinstance(element.tag, str):
            if local_name(element.tag) == "lb":
                # a line break separates words
                parts.append(" ")
                if current:
                    lbs[current[-1]].append(count - starts[current[-1]])
            emit(element.text)
            for child in element:
                walk(child)
                emit(child.tail)
        if unit is not None:
            ranges[unit] = (starts[unit], count)
            current.pop()

    text_element = find_text_element(root)
    if text_element is not None:
        walk(text_element)
    return TextStream("".join(parts), ranges, lbs)


def words(xml: str) -> list[str]:
    """The words of a unit's text (or of several elements): whitespace-delimited, as GROBID tokenizes on whitespace too."""
    content = parse_unit(xml) if xml.strip() else []
    return " ".join(element_text(c) if not isinstance(c, str) else c for c in content).split()


def check_transferred(original_xml: str, proposal_xml: str, segmenting: bool = False) -> ProposalStatus:
    """
    The checks of checks.py, and the target's words unchanged: unlike a correction, a transfer sees
    text with other whitespace (the earlier version's), which must not slip into the target.
    """
    status = (check_segments if segmenting else check_proposal)(original_xml, proposal_xml)
    if status == ProposalStatus.OK and not segmenting and words(proposal_xml) != words(original_xml):
        return ProposalStatus.TEXT_CHANGED
    return status


def _canonical(xml: str) -> str:
    content = parse_unit(xml)
    return "".join(canonical_form(c) if not isinstance(c, str) else c for c in content)


def _source_units_in(stream: TextStream, low: int, high: int) -> list[int]:
    """The source units that overlap the stream positions [low, high]."""
    return [i for i, (start, end) in enumerate(stream.unit_ranges) if start <= high and end > low]


def plan_unit_transfer(source_xml: str, target_xml: str, unit_path: str | None) -> TransferPlan:
    """
    The plan for the review units of a unit document (see the module doc).

    Raises:
        ValueError: a document is not well-formed, or the unit path is invalid.
    """
    source_root = parse_document(source_xml)
    target_root = parse_document(target_xml)
    source_units = find_unit_elements(source_root, unit_path)
    target_units = find_unit_elements(target_root, unit_path)
    source_stream = text_stream(source_root, source_units)
    target_stream = text_stream(target_root, target_units)
    alignment = align_texts(source_stream.text, target_stream.text)
    source_xmls = [serialize_unit(e) for e in source_units]
    plans: list[UnitTransfer] = []
    for index, element in enumerate(target_units):
        start, end = target_stream.unit_ranges[index]
        if alignment.aligned_share(start, end) < MIN_ALIGNED_SHARE:
            plans.append(UnitTransfer(index, UNMATCHED))
            continue
        mapped = [m for m in alignment.mapping[start:end] if m is not None]
        matches = _source_units_in(source_stream, min(mapped), max(mapped))
        if not matches:
            plans.append(UnitTransfer(index, UNMATCHED))
            continue
        fragment = "\n".join(source_xmls[i] for i in matches)
        if len(matches) == 1:
            source_index = matches[0]
            target_xml_unit = serialize_unit(element)
            same_lbs = source_stream.unit_lbs[source_index] == target_stream.unit_lbs[index]
            if same_lbs and check_transferred(target_xml_unit, source_xmls[source_index]) == ProposalStatus.OK:
                unchanged = _canonical(source_xmls[source_index]) == _canonical(target_xml_unit)
                plans.append(UnitTransfer(index, COPIED, fragment, "" if unchanged else source_xmls[source_index]))
                continue
        plans.append(UnitTransfer(index, MODEL, fragment))
    return TransferPlan(plans, alignment.coverage)


# ── Zone documents ───────────────────────────────────────────────────


def zone_text(doc: "zone_lines.ZoneDocument") -> tuple[str, list[int]]:
    """
    The text of a zone document for the alignment (`<lb/>` as a space) and the atom index of each
    character of its stream.
    """
    parts: list[str] = []
    atoms: list[int] = []
    for i, atom in enumerate(doc.atoms):
        if atom.kind == zone_lines.CHAR:
            if atom.char.isspace():
                parts.append(" ")
                continue
            parts.append(atom.char)
            atoms.append(i)
        elif atom.kind in (zone_lines.SPACE, zone_lines.LB):
            parts.append(" ")
    return "".join(parts), atoms


def _runs(labels: Sequence[str | None]) -> list[tuple[int, int, str | None]]:
    """(start, end, label) of the runs of equal labels."""
    runs: list[tuple[int, int, str | None]] = []
    for i, label in enumerate(labels):
        if runs and runs[-1][2] == label:
            runs[-1] = (runs[-1][0], i + 1, label)
        else:
            runs.append((i, i + 1, label))
    return runs


def project_labels(source_labels: Sequence[str], alignment: Alignment) -> list[str | None]:
    """
    The label of every target character: that of its aligned source character; inserted characters
    take the label of their neighbours if both have the same, else None. Short runs without an
    identical run in the source are set to None (likely alignment noise), see the module doc.
    """
    mapping = alignment.mapping
    labels: list[str | None] = [source_labels[m] if m is not None else None for m in mapping]
    source_runs = {(start, end) for start, end, _ in _runs(source_labels)}
    for start, end, label in _runs(labels):
        if label is not None:
            continue
        before = labels[start - 1] if start > 0 else None
        after = labels[end] if end < len(labels) else None
        if before is not None and (after is None and end == len(labels) or before == after):
            for i in range(start, end):
                labels[i] = before
        elif after is not None and start == 0:
            for i in range(start, end):
                labels[i] = after
    for start, end, label in _runs(labels):
        if label is None or end - start >= MIN_LABEL_RUN:
            continue
        mapped = [m for m in mapping[start:end] if m is not None]
        if len(mapped) < end - start:
            continue
        first, last = mapped[0], mapped[-1]
        identical = (first, last + 1) in source_runs and mapped == list(range(first, last + 1))
        if not identical:
            for i in range(start, end):
                labels[i] = None
    return labels


def plan_zone_transfer(source_xml: str, target: ZoneDoc) -> TransferPlan:
    """
    The plan for the pages of a zone document (see the module doc).

    Raises:
        ValueError: the source cannot be read as zones.
    """
    if not ZONES_AVAILABLE:
        raise TransferError("Transferring zone documents needs an editor with the layout API (plugin API 1.5)")
    source = zone_lines.read_zone_document(source_xml, target.labels)
    source_text, source_atoms = zone_text(source)
    target_text, target_atoms = zone_text(target.doc)
    alignment = align_texts(source_text, target_text)
    source_labels = [source.atoms[i].label for i in source_atoms]
    projected = project_labels(source_labels, alignment)
    char_of_atom = {atom: position for position, atom in enumerate(target_atoms)}
    overrides: dict[int, str] = {}
    for position, label in enumerate(projected):
        if label is not None:
            overrides[target_atoms[position]] = label
    pieces = target.doc.line_pieces(overrides)
    source_line_of = [source.atoms[i].line for i in source_atoms]
    plans: list[UnitTransfer] = []
    for page in target.pages:
        positions = [char_of_atom[a] for line in page.lines for a in target.doc.lines[line].atoms if a in char_of_atom]
        if not positions or alignment.aligned_share(positions[0], positions[-1] + 1) < MIN_ALIGNED_SHARE:
            plans.append(UnitTransfer(page.index, UNMATCHED))
            continue
        mapped = [m for m in (alignment.mapping[p] for p in positions) if m is not None]
        source_lines = (source_line_of[min(mapped)], source_line_of[max(mapped)] + 1)
        if any(projected[p] is None for p in positions):
            plans.append(UnitTransfer(page.index, MODEL, source_lines=source_lines))
            continue
        changes = zone_lines.changes_between(target.doc, pieces, page.lines)
        proposal = "\n".join(change.format(target.width) for change in changes)
        plans.append(UnitTransfer(page.index, COPIED, proposal=proposal, source_lines=source_lines))
    return TransferPlan(plans, alignment.coverage, source)


# ── Prompts ──────────────────────────────────────────────────────────


def _doubt_instruction(marks: bool, zones: bool) -> str:
    if not marks:
        return ""
    form = (
        "`DOUBT <first id>-<last id>: <one sentence>` (or `DOUBT <id>: ...`) on a row of its own, with the ids of the lines to annotate"
        if zones
        else "a line `DOUBT: <one sentence>` right after the unit's XML"
    )
    return (
        "\n\nWhere you are sure that a decision of the earlier annotation violates the rules, still reproduce it, "
        f"and add {form} that says why. Add doubts only where you are sure; most units have none."
    )


def build_unit_system_prompt(segmenting: bool, marks: bool) -> str:
    """Instructions for reproducing the earlier annotation on the units of a batch."""
    if segmenting:
        structure = (
            "The earlier annotation decided where one unit ends and the next begins: reproduce these boundaries. Split a unit "
            "by returning several elements of the same kind one after the other under its header, or move text into the "
            "neighbouring unit and return the emptied unit as its header line alone. The text of all units together must stay "
            "exactly the same, in the same order."
        )
    else:
        structure = "Keep the unit's outermost element."
    return (
        "You transfer the TEI annotation of an earlier version of a document to a new version of it. The new version was "
        "produced from a better text recognition, so its text differs in places: misread characters are corrected, words "
        "that were missing are present, and line breaks may be elsewhere. You are given annotation rules, each labeled with "
        "its category, and numbered units of the new version (one XML element each), each with the corresponding part of "
        "the earlier annotation.\n\n"
        "For every unit, reproduce the decisions of the earlier annotation faithfully: the same elements and attributes "
        "around the same parts of the text, also where they seem to contradict the rules. Where the text differs, put the "
        "markup where the earlier annotation would have put it. Text without a counterpart in the earlier annotation is "
        "annotated by the rules, consistently with its surroundings.\n\n"
        "CRITICAL: never change, add, remove or reorder the text of the new version, only the markup around it. Keep its "
        f"<lb/> elements where they are. {structure} Do not add comments or explanations."
        f"{_doubt_instruction(marks, False)}\n\n"
        'Return every unit you were given, in the same order, each under its own header line "### Unit <number>" followed '
        "by the annotated XML of the unit. Do not wrap the answer in a code fence and write nothing else."
    )


def render_unit(unit: Unit, fragment: str) -> str:
    """A unit in the prompt: the earlier annotation, then the unit to annotate."""
    return f"Earlier annotation (from an older text recognition; its text may differ):\n{fragment}\nText to annotate:\n{unit.xml}"


def build_unit_user_prompt(rules: Sequence[tuple[str, str]], units: Sequence[Unit], fragments: Sequence[str], segmenting: bool) -> str:
    """Rules, then the units; for segmenting variants the earlier annotation of the batch comes once, before the units."""
    rule_text = "\n\n".join(f"## Rule category: {category}\n\n{text}" for category, text in rules)
    if segmenting:
        earlier = "\n".join(dict.fromkeys(f for f in fragments if f))
        body = "\n\n".join(f"### Unit {i}\n{unit.xml}" for i, unit in enumerate(units, 1))
        return f"{rule_text}\n\n## Earlier annotation (from an older text recognition; its text may differ)\n\n{earlier}\n\n## Units\n\n{body}"
    body = "\n\n".join(f"### Unit {i}\n{render_unit(unit, fragment)}" for i, (unit, fragment) in enumerate(zip(units, fragments), 1))
    return f"{rule_text}\n\n## Units\n\n{body}"


def build_page_system_prompt(marks: bool) -> str:
    """Instructions for reproducing the earlier zone labels on a window of pages."""
    return (
        "You transfer the segmentation of an earlier version of a document into zones (header, body, footnotes, running "
        "headers, page numbers ...) to a new version of it. The new version was produced from a better text recognition, so "
        "its text differs in places and its lines may be broken elsewhere. You are given annotation rules, each labeled with "
        "its category, the table of zone labels, the table of layout hints, the pages of the new version as numbered lines, "
        "and the corresponding lines of the earlier version (their ids start with S).\n\n"
        "Each line reads `<id> <label> {<hints>} | <text>`: the zone label of the line, layout hints (only where they apply) "
        "and the line's text. A line whose text lies in several zones has no label after its id; instead, every part of its "
        "text starts with its label in brackets: `<id> | [page]12 [note:headnote]Running title`. The label `-` marks text "
        "outside any zone. Lines under a \"Context\" heading are only shown for orientation: do not return them.\n\n"
        "Give every line of the new version's pages the zones that the earlier version gives the same text, faithfully, also "
        "where they seem to contradict the rules. Text without a counterpart in the earlier version gets the zone of its "
        "surroundings, by the rules. In the pieces form, copy the line's text exactly and only insert the label markers: "
        f"never change the text.{_doubt_instruction(marks, True)}\n\n"
        "Return the labels of every line of the new version's pages (not of the context, not of the earlier version), in "
        "order, one entry per row: `<first id>-<last id> <label>` for consecutive lines with the same label, `<id> <label>` "
        "for a single line, and `<id> | [<label>]<text> [<label>]<text>` for a line whose text lies in several zones. Leave "
        "out no line. Do not wrap the answer in a code fence and write nothing else."
    )


def source_listing(source: "zone_lines.ZoneDocument", lines: tuple[int, int]) -> str:
    """The earlier version's lines, ids prefixed with S."""
    width = zone_lines.line_id_width(len(source.lines))
    return "\n".join("S" + zone_lines.format_line(source, i, width) for i in range(*lines))


def window_source_lines(plans: Sequence[UnitTransfer], source: "zone_lines.ZoneDocument") -> tuple[int, int]:
    """The source lines of a window of pages, with a line of context on either side."""
    ranges = [p.source_lines for p in plans if p.source_lines is not None]
    if not ranges:
        return (0, 0)
    return (max(0, min(r[0] for r in ranges) - 1), min(len(source.lines), max(r[1] for r in ranges) + 1))


def build_page_user_prompt(
    rules: Sequence[tuple[str, str]], zdoc: ZoneDoc, window: Sequence["zone_lines.Page"], source: "zone_lines.ZoneDocument", lines: tuple[int, int]
) -> str:
    """Rules, label and hint tables, the new version's listing, then the earlier version's lines."""
    rule_text = "\n\n".join(f"## Rule category: {category}\n\n{text}" for category, text in rules)
    listing = zone_lines.format_listing(zdoc.doc, window, zdoc.hints)
    return (
        f"{rule_text}\n\n## Zone labels\n\n{zone_lines.format_label_table(zdoc.labels)}\n\n"
        f"## Layout hints\n\n{zone_lines.format_hint_table()}\n\n## New version: lines to annotate\n\n{listing}\n\n"
        f"## Earlier version: corresponding lines\n\n{source_listing(source, lines)}"
    )


def build_doubt_system_prompt(zones: bool) -> str:
    """Instructions for checking units whose annotation was taken over unchanged."""
    if zones:
        subject = "pages of a document segmented into zones, as numbered lines with their zone labels"
        form = "`DOUBT <first id>-<last id>: <one sentence>` (or `DOUBT <id>: ...`), one per row"
    else:
        subject = "numbered units of a TEI document (one XML element each)"
        form = 'a header line "### Doubt <number>" with the unit\'s number, followed by one sentence'
    return (
        f"You check annotation. You are given annotation rules, each labeled with its category, and {subject}. Find the "
        f"places where you are sure that the annotation violates the rules. For each, answer with {form} that says why. "
        f'Report only what you are sure of. If there is nothing, answer with the single line "{NO_DOUBTS}". Do not wrap the '
        "answer in a code fence and write nothing else."
    )


def build_doubt_unit_user_prompt(rules: Sequence[tuple[str, str]], units: Sequence[str]) -> str:
    rule_text = "\n\n".join(f"## Rule category: {category}\n\n{text}" for category, text in rules)
    body = "\n\n".join(f"### Unit {i}\n{xml}" for i, xml in enumerate(units, 1))
    return f"{rule_text}\n\n## Units\n\n{body}"


def build_doubt_page_user_prompt(rules: Sequence[tuple[str, str]], zdoc: ZoneDoc, window: Sequence["zone_lines.Page"], pieces: Sequence[Sequence["zone_lines.Piece"]]) -> str:
    rule_text = "\n\n".join(f"## Rule category: {category}\n\n{text}" for category, text in rules)
    width = zdoc.width
    parts = [
        f"=== Page {page.number} ===\n" + "\n".join(zone_lines.format_line(zdoc.doc, i, width, zdoc.hints_of(i), pieces[i]) for i in page.lines)
        for page in window
    ]
    return (
        f"{rule_text}\n\n## Zone labels\n\n{zone_lines.format_label_table(zdoc.labels)}\n\n"
        f"## Layout hints\n\n{zone_lines.format_hint_table()}\n\n## Document lines\n\n" + "\n\n".join(parts)
    )


# ── Answers ──────────────────────────────────────────────────────────


def split_unit_doubts(answer: Mapping[int, str]) -> tuple[dict[int, str], dict[int, str]]:
    """The units of an answer without their DOUBT lines, and the doubts by unit id."""
    units: dict[int, str] = {}
    doubts: dict[int, str] = {}
    for uid, text in answer.items():
        found = _DOUBT_LINE_RE.findall(text)
        if found:
            doubts[uid] = " ".join(found)
        units[uid] = _DOUBT_LINE_RE.sub("", text).strip()
    return units, doubts


def parse_doubt_units(raw: str) -> dict[int, str]:
    """The doubts of a doubts-only answer for units, by unit id."""
    headers = list(_DOUBT_HEADER_RE.finditer(raw))
    doubts: dict[int, str] = {}
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw)
        text = " ".join(raw[header.end():end].split())
        if text:
            doubts.setdefault(int(header.group(1)), text)
    return doubts


def split_line_doubts(raw: str) -> tuple[str, list[tuple[int, int, str]]]:
    """The answer without its DOUBT rows, and the doubts as (first line, last line, text), 0-based."""
    doubts = [
        (int(m.group(1)) - 1, int(m.group(2) or m.group(1)) - 1, m.group(3))
        for m in _LINE_DOUBT_RE.finditer(raw)
    ]
    return _LINE_DOUBT_RE.sub("", raw), doubts


def page_doubts(doubts: Sequence[tuple[int, int, str]], window: Sequence["zone_lines.Page"]) -> dict[int, str]:
    """The doubts of the pages of a window (page index -> text); doubts outside the window are dropped."""
    found: dict[int, list[str]] = {}
    for first, _last, text in doubts:
        page = next((p for p in window if first in p.lines), None)
        if page is not None:
            found.setdefault(page.index, []).append(text)
    return {index: " ".join(texts) for index, texts in found.items()}


# ── Running a model ──────────────────────────────────────────────────


@dataclass(frozen=True)
class TransferredUnit:
    """The result for one unit of a batch."""

    index: int
    status: ProposalStatus
    # the unit's new XML (pages: label changes in the line form) if valid and different from the target, else None
    xml: str | None
    doubt: str | None = None


@dataclass
class TransferBatch:
    """The results for the units of one batch and the requests it took."""

    units: list[TransferredUnit] = field(default_factory=list)
    error: str | None = None
    calls: int = 0
    retries: int = 0
    seconds: float = 0.0


async def _request(client: ChatClient, system: str, user: str, retry: RetryPolicy | None, sleep: Sleep, result: TransferBatch) -> tuple[str, str | None]:
    raw, error, retries, seconds = await send_request(client, system, user, retry, sleep, lambda: False, None)
    result.calls += 1
    result.retries += retries
    result.seconds = round(result.seconds + seconds, 2)
    if error is not None and result.error is None:
        result.error = error
    return raw, error


def _failure_share(statuses: Sequence[ProposalStatus]) -> float:
    return sum(s.is_failure for s in statuses) / len(statuses) if statuses else 0.0


async def transfer_unit_batch(
    client: ChatClient,
    rules: Sequence[tuple[str, str]],
    units: Sequence[Unit],
    plans: Mapping[int, UnitTransfer],
    segmenting: bool = False,
    marks: bool = False,
    split_on_failure: float | None = 0.25,
    retry: RetryPolicy | None = None,
    sleep: Sleep = asyncio.sleep,
) -> TransferBatch:
    """
    Send the units with their fragments to the model in one request; a batch whose share of
    failed units exceeds `split_on_failure` is split in half and retried.
    """
    result = TransferBatch()
    system = build_unit_system_prompt(segmenting, marks)
    by_index: dict[int, TransferredUnit] = {}

    async def process(batch: list[Unit]) -> None:
        user = build_unit_user_prompt(rules, batch, [plans[u.index].fragment for u in batch], segmenting)
        raw, error = await _request(client, system, user, retry, sleep, result)
        answer, doubts = split_unit_doubts(parse_unit_answer(raw)) if error is None else ({}, {})
        statuses: list[ProposalStatus] = []
        proposals: list[str | None] = []
        for uid, unit in enumerate(batch, 1):
            proposal = answer.get(uid)
            if error is not None:
                status = ProposalStatus.ERROR
            elif proposal is None:
                status = ProposalStatus.MISSING
            else:
                status = check_transferred(unit.xml, proposal, segmenting)
            statuses.append(status)
            proposals.append(proposal)
        if segmenting and ProposalStatus.TEXT_CHANGED in statuses and all(
            s in (ProposalStatus.OK, ProposalStatus.TEXT_CHANGED) for s in statuses
        ):
            if "".join(unit_text(p or "") for p in proposals) == "".join(unit_text(u.xml) for u in batch):
                statuses = [ProposalStatus.OK for _ in statuses]
        if segmenting and all(s == ProposalStatus.OK for s in statuses):
            # the batch's words must stay the same, also where text moved between its units
            if [w for p in proposals for w in words(p or "")] != [w for u in batch for w in words(u.xml)]:
                statuses = [ProposalStatus.TEXT_CHANGED for _ in statuses]
        if error is None and split_on_failure is not None and len(batch) > 1 and _failure_share(statuses) > split_on_failure:
            half = len(batch) // 2
            await process(batch[:half])
            await process(batch[half:])
            return
        for uid, (unit, status, proposal) in enumerate(zip(batch, statuses, proposals), 1):
            xml = None
            if status == ProposalStatus.OK and proposal is not None and _canonical(proposal) != _canonical(unit.xml):
                xml = proposal
            by_index[unit.index] = TransferredUnit(unit.index, status, xml, doubts.get(uid) if marks else None)

    await process(list(units))
    result.units = [by_index[u.index] for u in units]
    return result


def _split_window(window: list["zone_lines.Page"]) -> tuple[list["zone_lines.Page"], list["zone_lines.Page"]] | None:
    from .zones import _split

    return _split(window)


async def transfer_page_batch(
    client: ChatClient,
    rules: Sequence[tuple[str, str]],
    zdoc: ZoneDoc,
    pages: Sequence[Unit],
    plan: TransferPlan,
    marks: bool = False,
    split_on_failure: float | None = 0.25,
    retry: RetryPolicy | None = None,
    sleep: Sleep = asyncio.sleep,
) -> TransferBatch:
    """
    Send a window of consecutive pages with the corresponding source lines to the model; a page's
    result is its label changes in the line form. Windows whose pages fail are split as in
    zones.correct_pages().
    """
    assert plan.source_zones is not None
    result = TransferBatch()
    system = build_page_system_prompt(marks)
    plans = plan.by_index()
    by_page = {page.index: page for page in zdoc.pages}
    # page index -> parts (first line, judgement, doubt)
    parts: dict[int, list[tuple[int, ProposalStatus, list["zone_lines.LabelChange"], str | None]]] = {}

    async def process(window: list["zone_lines.Page"]) -> None:
        lines = window_source_lines([plans[p.index] for p in window], plan.source_zones)  # type: ignore[arg-type]
        user = build_page_user_prompt(rules, zdoc, window, plan.source_zones, lines)  # type: ignore[arg-type]
        raw, error = await _request(client, system, user, retry, sleep, result)
        answer, doubts = split_line_doubts(raw) if error is None else ("", [])
        judgements: list[tuple[ProposalStatus, list[zone_lines.LabelChange]]]
        if error is not None:
            judgements = [(ProposalStatus.ERROR, []) for _ in window]
        else:
            judgements = [(j.status, j.changes) for j in judge_answer(zdoc, window, answer, "all")]
        halves = _split_window(window) if error is None and split_on_failure is not None and _failure_share([s for s, _ in judgements]) > split_on_failure else None
        if halves is not None:
            await process(halves[0])
            await process(halves[1])
            return
        found = page_doubts(doubts, window) if marks else {}
        for page, (status, changes) in zip(window, judgements):
            parts.setdefault(page.index, []).append((page.first_line, status, changes, found.get(page.index)))

    await process([by_page[u.index] for u in pages])
    for unit in pages:
        page_parts = sorted(parts[unit.index], key=lambda p: p[0])
        failure = next((p[1] for p in page_parts if p[1].is_failure or p[1] == ProposalStatus.ERROR), None)
        doubt = " ".join(p[3] for p in page_parts if p[3]) or None
        if failure is not None:
            result.units.append(TransferredUnit(unit.index, failure, None, doubt))
            continue
        changes = [c for p in page_parts for c in p[2]]
        xml = "\n".join(c.format(zdoc.width) for c in changes)
        result.units.append(TransferredUnit(unit.index, ProposalStatus.OK, xml or None, doubt))
    return result


async def doubt_unit_batch(
    client: ChatClient,
    rules: Sequence[tuple[str, str]],
    units: Sequence[Unit],
    plans: Mapping[int, UnitTransfer],
    retry: RetryPolicy | None = None,
    sleep: Sleep = asyncio.sleep,
) -> TransferBatch:
    """Ask for doubts about copied units, as they will be saved."""
    result = TransferBatch()
    shown = [plans[u.index].proposal or u.xml for u in units]
    raw, error = await _request(client, build_doubt_system_prompt(False), build_doubt_unit_user_prompt(rules, shown), retry, sleep, result)
    doubts = parse_doubt_units(raw) if error is None else {}
    status = ProposalStatus.ERROR if error is not None else ProposalStatus.UNCHANGED
    result.units = [TransferredUnit(u.index, status, None, doubts.get(i)) for i, u in enumerate(units, 1)]
    return result


async def doubt_page_batch(
    client: ChatClient,
    rules: Sequence[tuple[str, str]],
    zdoc: ZoneDoc,
    pages: Sequence[Unit],
    plan: TransferPlan,
    retry: RetryPolicy | None = None,
    sleep: Sleep = asyncio.sleep,
) -> TransferBatch:
    """Ask for doubts about copied pages, with their labels as they will be saved."""
    result = TransferBatch()
    plans = plan.by_index()
    changes: list[zone_lines.LabelChange] = []
    for unit in pages:
        proposal = plans[unit.index].proposal or ""
        changes.extend(zone_lines.parse_answer(proposal))
    pieces = zone_lines.changed_pieces(zdoc.doc, changes)
    by_page = {page.index: page for page in zdoc.pages}
    window = [by_page[u.index] for u in pages]
    raw, error = await _request(client, build_doubt_system_prompt(True), build_doubt_page_user_prompt(rules, zdoc, window, pieces), retry, sleep, result)
    found = page_doubts(split_line_doubts(raw)[1], window) if error is None else {}
    status = ProposalStatus.ERROR if error is not None else ProposalStatus.UNCHANGED
    result.units = [TransferredUnit(u.index, status, None, found.get(u.index)) for u in pages]
    return result


# ── Results ──────────────────────────────────────────────────────────


def merge_corrections(plan: TransferPlan, corrections: Mapping[int, str]) -> dict[int, str]:
    """The results of all units: the copied units' proposals and the model's corrections (which win)."""
    merged = {u.index: u.proposal for u in plan.units if u.category == COPIED and u.proposal}
    for index, xml in corrections.items():
        if plans_category(plan, index) != MODEL:
            raise TransferError(f"Unit {index} is not a unit for the model")
        merged[index] = xml
    return merged  # type: ignore[return-value]


def check_transfer_results(units: Sequence[Unit], results: Mapping[int, str], segmenting: bool = False) -> list[str]:
    """Problems of the results of a transfer sent back by a client (index -> unit XML), see check_transferred()."""
    by_index = {unit.index: unit for unit in units}
    problems = [f"No unit with index {index}" for index in results if index not in by_index]
    if problems:
        return problems
    allowed = (ProposalStatus.OK, ProposalStatus.TEXT_CHANGED) if segmenting else (ProposalStatus.OK,)
    for index, xml in results.items():
        status = check_transferred(by_index[index].xml, xml, segmenting)
        if status not in allowed:
            problems.append(f"Unit {index}: {status.value}")
    if segmenting and not problems:
        before = [w for unit in units for w in words(unit.xml)]
        after = [w for unit in units for w in words(results.get(unit.index, unit.xml))]
        if before != after:
            problems.append("The results change the text of the document")
    return problems


def plans_category(plan: TransferPlan, index: int) -> str | None:
    return next((u.category for u in plan.units if u.index == index), None)


def new_unit_positions(replacements: Mapping[int, str], unit_count: int) -> list[int]:
    """
    The index of each original unit's first element in the new document, for segmenting variants
    whose replacements may be several elements or none (-1 if the unit was removed).
    """
    positions: list[int] = []
    offset = 0
    for index in range(unit_count):
        xml = replacements.get(index)
        count = 1 if xml is None else len([c for c in (parse_unit(xml) if xml.strip() else []) if not isinstance(c, str)])
        positions.append(offset if count else -1)
        offset += count
    return positions


@dataclass(frozen=True)
class TransferSummary:
    """Counts of a transfer, recorded in the revision change."""

    units: int
    copied: int
    transferred: int  # model units with a valid result
    unmatched: int
    invalid: int
    errors: int
    unprocessed: int  # model units not sent (stopped run)
    doubted: int
    unit_name: str = "units"

    def measures(self) -> list[tuple[str, int]]:
        return [
            ("copied", self.copied), ("transferred", self.transferred), ("unmatched", self.unmatched),
            ("invalid", self.invalid), ("error", self.errors), ("unprocessed", self.unprocessed), ("doubted", self.doubted),
        ]


def doubt_marks(fingerprints: Sequence[str], doubts: Mapping[int, str], positions: Sequence[int] | None = None) -> list[dict[str, str]]:
    """Not-OK marks for the doubted units: fingerprint (of the unit in the new document) and comment."""
    marks: list[dict[str, str]] = []
    for index, comment in sorted(doubts.items()):
        position = positions[index] if positions is not None and index < len(positions) else index
        if 0 <= position < len(fingerprints):
            marks.append({"fingerprint": fingerprints[position], "comment": comment})
    return marks

