"""
Scoring corrections of zone documents (see zones.py) against a reference version.

Input and reference share the text stream, so both are read as zones and aligned on the
whitespace-free stream of their text characters (as segmentation.py does for segmenting variants).
Every aligned character has a label in the input, the reference and the proposal, and the scores
compare them by position:

- label accuracy: aligned characters whose proposed label is the reference's
- edits: an edit is a stretch of consecutive characters whose label changes to the same new label;
  needed edits are those between input and reference, made edits those between input and
  proposal, and a made edit is correct if it is also needed (same stretch, same label)
- zone boundaries: positions between two characters with different labels; boundary
  precision and recall compare the proposal's with the reference's (reported as the field scores)
- exact: pages all of whose characters carry the reference's labels

Each count belongs to the page of its (first) character, so the scores sum over pages as the
unit scores of other variants do.

Pure module (needs the editor's zone_lines module).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from .scoring import UnitScore
from .units import Unit
from .zones import ZoneDoc, ZoneLayout, parse_page_changes, read_zone_doc, zone_lines


@dataclass
class ZoneAlignment:
    """The input's pages and, for each of its text characters, the reference's label (None if unaligned)."""

    zdoc: ZoneDoc
    units: list[Unit]
    chars: list[int]  # atom indices of the input's text characters, in order
    char_pages: list[int]  # page index of each character
    gold_labels: list[str | None]
    unmatched_input: int  # input characters without a reference character
    unmatched_gold: int

    def truncated(self, max_units: int) -> "ZoneAlignment":
        """Only the first `max_units` pages are evaluated."""
        return ZoneAlignment(self.zdoc, self.units[:max_units], self.chars, self.char_pages, self.gold_labels, self.unmatched_input, self.unmatched_gold)


def _text_chars(doc: "zone_lines.ZoneDocument") -> list[int]:
    return [i for i, atom in enumerate(doc.atoms) if atom.kind == zone_lines.CHAR]


def align_zones(input_xml: str, gold_xml: str, layout: ZoneLayout) -> ZoneAlignment:
    """
    Raises:
        ValueError: a version cannot be read as zones.
    """
    zdoc = read_zone_doc(input_xml, layout)
    gold = zone_lines.read_zone_document(gold_xml, zdoc.labels)
    chars, gold_chars = _text_chars(zdoc.doc), _text_chars(gold)
    a = "".join(zdoc.doc.atoms[i].char for i in chars)
    b = "".join(gold.atoms[i].char for i in gold_chars)
    gold_labels: list[str | None] = [None] * len(chars)
    if a == b:
        blocks = [(0, 0, len(a))]
    else:
        blocks = [(m.a, m.b, m.size) for m in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks() if m.size]
    matched = 0
    for start_a, start_b, size in blocks:
        for k in range(size):
            gold_labels[start_a + k] = gold.atoms[gold_chars[start_b + k]].label
        matched += size
    line_page = {line: page.index for page in zdoc.pages for line in page.lines}
    char_pages = [line_page.get(zdoc.doc.atoms[i].line, 0) for i in chars]
    return ZoneAlignment(zdoc, list(zdoc.units), chars, char_pages, gold_labels, len(a) - matched, len(b) - matched)


def proposed_labels(alignment: ZoneAlignment, proposals: Mapping[int, str | None]) -> list[str]:
    """The label of each input character after the valid changes of the pages' proposals (page index -> changes)."""
    zdoc = alignment.zdoc
    changes: list[zone_lines.LabelChange] = []
    for index, text in proposals.items():
        if text:
            valid, _ = parse_page_changes(zdoc, index, text)
            changes.extend(valid)
    overrides = zone_lines.atom_labels(zdoc.doc, changes)
    return [overrides.get(i, zdoc.doc.atoms[i].label) for i in alignment.chars]


def _edits(base: Sequence[str], target: Sequence[str | None]) -> dict[tuple[int, int, str], int]:
    """Stretches of aligned characters whose label changes from `base` to the same label of `target` -> first character."""
    edits: dict[tuple[int, int, str], int] = {}
    start: int | None = None
    for k in range(len(base) + 1):
        changed = k < len(base) and target[k] is not None and target[k] != base[k]
        if start is not None and (not changed or target[k] != target[start] or base[k] != base[k - 1]):
            label = target[start]
            assert label is not None
            edits[(start, k, label)] = start
            start = None
        if changed and start is None:
            start = k
    return edits


def _boundaries(labels: Sequence[str | None], aligned: Sequence[bool]) -> set[int]:
    """Positions k (between characters k-1 and k, both aligned) where the label changes."""
    return {k for k in range(1, len(labels)) if aligned[k] and aligned[k - 1] and labels[k] != labels[k - 1]}


def page_scores(alignment: ZoneAlignment, proposal: Sequence[str] | None) -> dict[int, UnitScore]:
    """Scores of the evaluated pages; without a proposal, of the input itself (baseline)."""
    doc = alignment.zdoc.doc
    base = [doc.atoms[i].label for i in alignment.chars]
    labels = list(proposal) if proposal is not None else base
    gold = alignment.gold_labels
    aligned = [g is not None for g in gold]
    pages = alignment.char_pages
    counts: dict[int, dict[str, int]] = {
        u.index: dict(needed=0, made=0, correct_edits=0, gold_fields=0, proposal_fields=0, correct_fields=0, positions=0, correct_positions=0)
        for u in alignment.units
    }

    def add(position: int, key: str, n: int = 1) -> None:
        page = pages[position]
        if page in counts:
            counts[page][key] += n

    needed = _edits(base, gold)
    made = _edits(base, labels)
    for first in needed.values():
        add(first, "needed")
    for edit, first in made.items():
        add(first, "made")
        if edit in needed:
            add(first, "correct_edits")
    gold_b = _boundaries(gold, aligned)
    proposal_b = _boundaries(labels, aligned)
    for k in gold_b:
        add(k, "gold_fields")
    for k in proposal_b:
        add(k, "proposal_fields")
        if k in gold_b:
            add(k, "correct_fields")
    for k, g in enumerate(gold):
        if g is not None:
            add(k, "positions")
            if labels[k] == g:
                add(k, "correct_positions")
    return {
        index: UnitScore(exact=c["positions"] == c["correct_positions"], **c)
        for index, c in counts.items()
    }
