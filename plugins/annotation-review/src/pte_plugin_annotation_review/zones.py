"""
Reviewing zone documents (variants whose extractor declares `zoneLabels`, such as
`grobid.training.segmentation`) by page.

The children of `<text>` of a zone document are zones that partition one text stream; correcting
it means relabeling zones and moving their boundaries. So instead of XML chunks, the model is
given page windows (whole pages of the document's line layout, `pages:2` by default) as numbered
lines with zone labels and layout hints (see the editor's `fastapi_app/lib/tei/zone_lines.py`),
and answers with label changes of lines:

    [{"lines": "0413-0420", "label": "body", "rationale": "..."},
     {"line": "0412", "pieces": "[page]12 [note:headnote]Iacino et al.", "rationale": "..."}]

The server turns them into the findings contract of the XML review (`{old, new, rationale}`):
`old` is the raw text from the zone tags before the changed text to the zone tags after it,
`new` the same span with the changes applied, widened until `old` is unique in the document.
Changes whose spans touch are one finding with their rationales joined. So each finding is
well-formed when applied alone, and the editor extension treats them as any other finding.

Needs the editor's line layout API (plugin API 1.5); without it, zone documents are reviewed as XML.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .prompts import Finding, _extract_json_list

try:
    from fastapi_app.plugin_api import MISSING_LAYOUT_MESSAGE, LineLayout, get_line_layout, get_zone_labels, zone_lines

    LAYOUT_API = True
except ImportError:  # editor older than plugin API 1.5
    LAYOUT_API = False

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_PAGES = 2


class MissingLayoutError(ValueError):
    """The document is a zone document, but its line layout is missing."""


@dataclass
class ZoneSource:
    """A zone document cut into page windows."""

    xml: str
    labels: list["zone_lines.ZoneLabel"]
    doc: "zone_lines.ZoneDocument"
    layout: "LineLayout"
    windows: list[list["zone_lines.Page"]]


def zone_labels_of(xml: str) -> list["zone_lines.ZoneLabel"] | None:
    """The zone labels of the document's variant (from its encodingDesc), or None if it is not a zone document."""
    if not LAYOUT_API:
        return None
    from fastapi_app.lib.doc_rules.rules_providers import extract_extractor_provenance

    provenance = extract_extractor_provenance(xml)
    return get_zone_labels(provenance[1]) if provenance else None


def prepare(
    xml: str,
    labels: Sequence["zone_lines.ZoneLabel"],
    doc_id: str | None,
    variant: str | None,
    scope: str | None = None,
    window_pages: int = DEFAULT_WINDOW_PAGES,
) -> ZoneSource:
    """
    The page windows of a zone document. With a scope (the raw XML of a zone selected in the
    editor), the single window holds the pages the scope lies on.

    Raises:
        MissingLayoutError: the document has no layout.
        ValueError: the document cannot be read as zones, or the scope is not part of its <text>.
    """
    layout = get_line_layout(xml, doc_id, variant) if doc_id else None
    if layout is None:
        raise MissingLayoutError(MISSING_LAYOUT_MESSAGE)
    doc = zone_lines.read_zone_document(xml, labels)
    pages = zone_lines.pages_of(doc, layout.pages)
    if scope is not None:
        start = xml.find(scope)
        if start < doc.start or start + len(scope) > doc.end:
            raise ValueError("The node to review is not part of the document's <text>")
        end = start + len(scope)
        lines = {a.line for a in doc.atoms if a.line >= 0 and a.start < end and a.end > start}
        if not lines:
            raise ValueError("The node to review has no text")
        windows = [[p for p in pages if set(p.lines) & lines]]
    else:
        windows = zone_lines.page_windows(doc, pages, max_pages=window_pages, hints=layout.hints)
    return ZoneSource(xml, list(labels), doc, layout, windows)


def window_lines(window: Sequence["zone_lines.Page"]) -> range:
    """The lines of a window (without its context)."""
    return range(window[0].first_line, window[-1].end_line)


def window_range(source: ZoneSource, window: Sequence["zone_lines.Page"]) -> tuple[int, int]:
    """
    Position of a window's text in the document, as (start, end) offsets in UTF-16 code units: from
    its first text character (or `<lb/>`) to after its last.
    """
    lines = window_lines(window)
    atoms = [a for a in source.doc.atoms if a.line in lines and a.kind in (zone_lines.CHAR, zone_lines.LB)]
    if not atoms:
        return (0, 0)

    def utf16(offset: int) -> int:
        return len(source.xml[:offset].encode("utf-16-le")) // 2

    return utf16(atoms[0].start), utf16(atoms[-1].end)


# ── Prompts ──────────────────────────────────────────────────────────


def build_system_prompt() -> str:
    """Instructions for reviewing the zones of a page window."""
    return (
        "You are reviewing the segmentation of a document into zones. The document's text is divided into zones "
        "such as the header, the body, footnotes, running headers and page numbers. You will be given annotation "
        "rules, each labeled with its category, the table of zone labels, the table of layout hints, and a part "
        "of the document as numbered lines.\n\n"
        "Each line of the listing reads `<id> <label> {<hints>} | <text>`: the zone label of the line, layout hints "
        "(only where they apply) and the line's text. A line whose text lies in several zones has no label after "
        "its id; instead, every part of its text starts with its label in brackets: "
        "`<id> {<hints>} | [page]12 [note:headnote]Running title`. The label `-` marks text outside any zone. "
        "Lines under a \"Context\" heading are only shown for orientation: do not change them.\n\n"
        "Check whether the zone labels follow the rules. For every line or range of consecutive lines whose "
        "label is wrong, propose a change. Respond with ONLY a JSON array (no prose, no markdown fence) of "
        "objects of one of these forms:\n"
        '  {"lines": "<first id>-<last id>", "label": "<label>", "rationale": "<why>"} to give a range of whole lines one label\n'
        '  {"line": "<id>", "pieces": "[<label>]<text> [<label>]<text>", "rationale": "<why>"} for a line whose text lies in several zones\n\n'
        'Example: [{"lines": "0413-0420", "label": "body", "rationale": "The text continues the body."}, '
        '{"line": "0412", "pieces": "[page]12 [note:headnote]Iacino et al., Text and Data Mining", "rationale": "Page number and running header."}]\n\n'
        "Use only the labels of the table. In the pieces form, copy the line's text exactly and only insert the "
        "label markers. Never change the text. Group consecutive lines with the same new label into one range. "
        "If all labels are correct, respond with an empty JSON array: []"
    )


def build_user_prompt(
    rule_excerpts: Sequence[tuple[str, str]],
    source: ZoneSource,
    window: Sequence["zone_lines.Page"],
    position: tuple[int, int],
) -> str:
    """Rules, label and hint tables, then the window's listing with its context lines."""
    rules = "\n\n".join(f"## Rule category: {category}\n\n{excerpt}" for category, excerpt in rule_excerpts)
    listing = zone_lines.format_listing(source.doc, window, source.layout.hints)
    index, total = position
    part = f" (part {index} of {total} of the document)" if total > 1 else ""
    return (
        f"{rules}\n\n## Zone labels\n\n{zone_lines.format_label_table(source.labels)}\n\n"
        f"## Layout hints\n\n{zone_lines.format_hint_table()}\n\n## Document lines{part}\n\n{listing}"
    )


def build_retry_note(problems: Sequence[str]) -> str:
    """Appended to the user prompt when every change of a first attempt was dropped."""
    listed = "\n".join(f"- {p}" for p in problems[:20])
    return (
        "\n\n## Note on a previous attempt\n\n"
        f"A previous review of these lines proposed changes that were all dropped:\n\n{listed}\n\n"
        "Review the lines again. Use only line ids of the pages (not of the context), only labels of the table, "
        "and copy the text of a line exactly in the pieces form."
    )


# ── Answers ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProposedChange:
    """A valid label change with its rationale."""

    change: "zone_lines.LabelChange"
    rationale: str


def _change_of(item: dict[str, object]) -> "zone_lines.LabelChange | None":
    if isinstance(item.get("lines"), str) and isinstance(item.get("label"), str):
        return zone_lines.parse_line_answer(f"{item['lines']} {item['label']}")
    if isinstance(item.get("line"), str | int) and isinstance(item.get("label"), str) and not item.get("pieces"):
        return zone_lines.parse_line_answer(f"{item['line']} {item['label']}")
    if isinstance(item.get("line"), str | int) and isinstance(item.get("pieces"), str):
        return zone_lines.parse_line_answer(f"{item['line']} | {item['pieces']}")
    return None


def parse_changes(raw_response: str, source: ZoneSource, window: Sequence["zone_lines.Page"]) -> tuple[list[ProposedChange], list[str]]:
    """
    The valid changes of an answer and a description of each dropped one: unreadable entries,
    lines outside the window, unknown labels, changed text, no-op changes and overlaps with an
    earlier change.

    Raises:
        UnusableResponseError: the response contains no JSON list.
    """
    parsed = _extract_json_list(raw_response)
    width = zone_lines.line_id_width(len(source.doc.lines))
    proposed: list[tuple["zone_lines.LabelChange", str]] = []
    problems: list[str] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        change = _change_of(item)
        rationale = item.get("rationale")
        if change is None:
            problems.append(f"unreadable: {item!r}"[:200])
            continue
        proposed.append((change, rationale if isinstance(rationale, str) else ""))
    valid, dropped = zone_lines.check_changes(source.doc, [c for c, _ in proposed], window_lines(window))
    reasons = {
        "outside": "line outside the pages under review", "label": "unknown label", "text": "text of the line changed",
        "noop": "changes nothing", "overlap": "overlaps another change",
    }
    problems += [f"{p.change.format(width)}: {reasons.get(p.reason, p.reason)}" for p in dropped if p.reason != "noop"]
    rationales = {id(c): r for c, r in proposed}
    result = [ProposedChange(c, rationales.get(id(c), "")) for c in valid]
    logger.info(f"annotation-review: kept {len(result)} of {len(proposed)} zone changes")
    return result, problems


# ── Findings ─────────────────────────────────────────────────────────


def _boundaries(doc: "zone_lines.ZoneDocument") -> list[int]:
    """Positions where a finding's span may start or end: the edges of the zone tags and of <text>'s content."""
    points = {doc.start, doc.end}
    for item in doc.items:
        if item.label != zone_lines.OUTSIDE:
            points.add(item.start)
            points.add(item.end)
    return sorted(points)


def _span(xml: str, new: str, boundaries: Sequence[int]) -> tuple[int, int, int]:
    """(start, end in xml, end in new) of the difference between xml and new, widened to zone tag boundaries."""
    prefix = 0
    limit = min(len(xml), len(new))
    while prefix < limit and xml[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and xml[len(xml) - 1 - suffix] == new[len(new) - 1 - suffix]:
        suffix += 1
    start = max((b for b in boundaries if b <= prefix), default=0)
    end = min((b for b in boundaries if b >= len(xml) - suffix), default=len(xml))
    return start, end, end + len(new) - len(xml)


def _finding(xml: str, labels: Sequence["zone_lines.ZoneLabel"], changes: Sequence["zone_lines.LabelChange"], boundaries: Sequence[int]) -> tuple[str, str, int, int] | None:
    """(old, new, start, end) of the finding for changes applied together, `old` unique in xml; None if not possible."""
    new_xml = zone_lines.apply_labels(xml, labels, changes)
    if new_xml == xml:
        return None
    start, end, new_end = _span(xml, new_xml, boundaries)
    delta = new_end - end
    while xml.count(xml[start:end]) != 1:
        wider_start = max((b for b in boundaries if b < start), default=start)
        wider_end = min((b for b in boundaries if b > end), default=end)
        if wider_start == start and wider_end == end:
            return None
        start, end = wider_start, wider_end
    return xml[start:end], new_xml[start:end + delta], start, end


def findings_from_changes(source: ZoneSource, proposed: Sequence[ProposedChange]) -> list[Finding]:
    """
    The findings for the changes: one per group of changes whose spans touch (rationales joined),
    in document order, each well-formed when applied alone.
    """
    boundaries = _boundaries(source.doc)
    items: list[tuple[int, int, list[ProposedChange]]] = []
    for item in proposed:
        result = _finding(source.xml, source.labels, [item.change], boundaries)
        if result is None:
            continue
        items.append((result[2], result[3], [item]))
    items.sort(key=lambda entry: entry[0])
    groups: list[tuple[int, int, list[ProposedChange]]] = []
    for start, end, members in items:
        if groups and start <= groups[-1][1]:
            last = groups[-1]
            groups[-1] = (last[0], max(last[1], end), last[2] + members)
        else:
            groups.append((start, end, members))
    findings: list[Finding] = []
    for _, _, members in groups:
        result = _finding(source.xml, source.labels, [m.change for m in members], boundaries)
        if result is None:
            continue
        rationale = "; ".join(dict.fromkeys(m.rationale for m in members if m.rationale)) or "Zone labels changed."
        findings.append({"id": len(findings), "old": result[0], "new": result[1], "rationale": rationale})
    return findings
