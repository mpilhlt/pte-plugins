"""
Scoring variants whose model splits text into units, such as the reference segmenter.

Their units cannot be matched one-to-one with the gold version: correcting them means splitting
and merging units. Input and gold version share their text, though (data correction forbids
changing it), so both are read as one text stream (whitespace removed), and every element
becomes a field (element path, attributes, start, end) with its position in that stream. Gold
fields are mapped onto the input's stream where the two texts agree; a field is credited to the
input unit in which it starts. Edits are then counted as for other variants (scoring.score_fields).

Pure module.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import Match, SequenceMatcher
from typing import Sequence

from lxml import etree

from .correction import CorrectionRun, UnitOutcome
from .scoring import IGNORED_ATTRIBUTES, UnitScore, score_fields
from .units import Unit, attributes, collapse, document_units, local_name, parse_unit

# (element path, attributes, start, end); positions in the whitespace-free text stream
SpanField = tuple[str, tuple[tuple[str, str], ...], int, int]

_WS_RE = re.compile(r"\s+")


def _squeeze(text: str | None) -> str:
    return _WS_RE.sub("", text or "")


def _walk(element: etree._Element, path: str, pos: int, fields: Counter[SpanField]) -> int:
    start = pos
    pos += len(_squeeze(element.text))
    for child in element:
        if isinstance(child.tag, str):
            pos = _walk(child, f"{path}/{local_name(child.tag)}", pos, fields)
        pos += len(_squeeze(child.tail))
    attrs = tuple(sorted(
        (name, collapse(value)) for name, value in attributes(element) if name not in IGNORED_ATTRIBUTES
    ))
    fields[(path, attrs, start, pos)] += 1
    return pos


def stream_fields(xmls: Sequence[str]) -> tuple[str, list[tuple[int, int]], Counter[SpanField]]:
    """
    Read units (each one or more elements, possibly empty) as one text stream.

    Returns:
        (the whitespace-free text, the (start, end) span of each unit, the fields)

    Raises:
        ValueError: a unit is not well-formed.
    """
    pos = 0
    text: list[str] = []
    spans: list[tuple[int, int]] = []
    fields: Counter[SpanField] = Counter()
    for xml in xmls:
        start = pos
        for item in parse_unit(xml) if xml.strip() else []:
            if isinstance(item, str):
                pos += len(_squeeze(item))
                text.append(_squeeze(item))
            else:
                text.append(_squeeze("".join(str(t) for t in item.itertext())))
                pos = _walk(item, local_name(item.tag), pos, fields)
        spans.append((start, pos))
    return "".join(text), spans, fields


@dataclass(frozen=True)
class SegmentAlignment:
    """Input units and fields of a segmenting variant, with the gold fields in input positions."""

    # Input units whose text is shared with the gold version (these are evaluated)
    units: list[Unit]
    # (start, end) of every input unit in the input stream, by unit index
    spans: dict[int, tuple[int, int]]
    input_fields: Counter[SpanField]
    # Gold fields that lie in text shared with the input, in input positions
    gold_fields: Counter[SpanField]
    unmatched_input: int
    unmatched_gold: int

    def truncated(self, max_units: int) -> "SegmentAlignment":
        """Only the first max_units units are evaluated."""
        return SegmentAlignment(self.units[:max_units], self.spans, self.input_fields, self.gold_fields, self.unmatched_input, self.unmatched_gold)


def _within(blocks: Sequence[Match], start: int, end: int, side: str) -> Match | None:
    for block in blocks:
        offset = block.a if side == "a" else block.b
        if offset <= start and end <= offset + block.size:
            return block
    return None


def align_segments(input_xml: str, gold_xml: str, unit_path: str | None) -> SegmentAlignment:
    """
    Read both documents as text streams and map the gold fields onto the input stream. Input units
    whose text is not entirely shared with the gold version, and gold units that cannot be mapped,
    are counted as unmatched.

    Raises:
        ValueError: a document is not well-formed, or the unit path is invalid.
    """
    input_units = document_units(input_xml, unit_path)
    gold_units = document_units(gold_xml, unit_path)
    input_text, input_spans, input_fields = stream_fields([u.xml for u in input_units])
    gold_text, gold_spans, gold_fields = stream_fields([u.xml for u in gold_units])
    blocks = [b for b in SequenceMatcher(None, input_text, gold_text, autojunk=False).get_matching_blocks() if b.size]

    mapped: Counter[SpanField] = Counter()
    for (path, attrs, start, end), count in gold_fields.items():
        block = _within(blocks, start, end, "b")
        if block is not None:
            mapped[(path, attrs, start - block.b + block.a, end - block.b + block.a)] += count
    units = [u for u, (s, e) in zip(input_units, input_spans) if e > s and _within(blocks, s, e, "a") is not None]
    unmatched_gold = sum(1 for s, e in gold_spans if e <= s or _within(blocks, s, e, "b") is None)
    return SegmentAlignment(
        units=units,
        spans={u.index: span for u, span in zip(input_units, input_spans)},
        input_fields=input_fields,
        gold_fields=mapped,
        unmatched_input=len(input_units) - len(units),
        unmatched_gold=unmatched_gold,
    )


def _owned(fields: Counter[SpanField], start: int, end: int) -> Counter[SpanField]:
    return Counter({f: n for f, n in fields.items() if start <= f[2] < end})


def unit_scores(alignment: SegmentAlignment, proposal_fields: Counter[SpanField] | None) -> dict[int, UnitScore]:
    """Scores of the evaluated units; proposal_fields None scores the input itself (baseline)."""
    proposals = proposal_fields if proposal_fields is not None else alignment.input_fields
    scores: dict[int, UnitScore] = {}
    for unit in alignment.units:
        start, end = alignment.spans[unit.index]
        scores[unit.index] = score_fields(
            _owned(alignment.input_fields, start, end), _owned(alignment.gold_fields, start, end), _owned(proposals, start, end)
        )
    return scores


def proposal_fields(alignment: SegmentAlignment, run: CorrectionRun) -> Counter[SpanField]:
    """
    The fields of a run's proposals in input positions. Each batch is read as a stream (failed and
    missing units as their input) and mapped onto the spans of its units; correct_units() only
    accepts batches whose text is unchanged, so the stream has the length of those spans.
    """
    batches: dict[int, list[UnitOutcome]] = defaultdict(list)
    for outcome in run.outcomes:
        batches[outcome.batch_index].append(outcome)
    fields: Counter[SpanField] = Counter()
    for outcomes in batches.values():
        outcomes.sort(key=lambda o: o.position)
        _, _, local = stream_fields([o.proposal if o.proposal is not None else o.unit.xml for o in outcomes])
        # local stream position -> input position, piecewise over the batch's unit spans
        pieces: list[tuple[int, int, int]] = []  # (local start, local end, input start)
        cursor = 0
        for o in outcomes:
            start, end = alignment.spans[o.unit.index]
            pieces.append((cursor, cursor + end - start, start))
            cursor += end - start

        def to_input(pos: int, is_end: bool) -> int:
            for local_start, local_end, input_start in pieces:
                if local_start <= pos < local_end or (is_end and local_start < pos <= local_end):
                    return input_start + pos - local_start
            return pieces[-1][2] + pos - pieces[-1][0]

        for (path, attrs, start, end), count in local.items():
            input_start = to_input(start, False)
            fields[(path, attrs, input_start, to_input(end, True) if end > start else input_start)] += count
    return fields
