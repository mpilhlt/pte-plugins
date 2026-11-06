"""
Scoring proposed units against gold units, and aligning a document's units with its gold version.

A unit is described by its fields: one (element path, attributes, text) tuple per element, the
unit's own element included, as GROBID's field-level evaluation does. Because the input (GROBID's
output) is mostly right already, the main measure scores the edits:

- needed edits: fields to add (in gold, not in input) and to remove (in input, not in gold)
- made edits: the same between input and proposal
- edit precision = correct edits / made edits, edit recall = correct edits / needed edits

Pure module.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Hashable, Sequence, TypeVar

from lxml import etree

from .units import Unit, attributes, collapse, document_units, element_text, local_name, parse_unit, text_key

# Attributes that carry no annotation decision and are left out of the fields
IGNORED_ATTRIBUTES = frozenset({"xml:id"})

Field = tuple[str, tuple[tuple[str, str], ...], str]
FieldT = TypeVar("FieldT", bound=Hashable)


def unit_fields(xml: str) -> Counter[Field]:
    """
    The fields of a unit as a multiset.

    Raises:
        ValueError: the unit is not well-formed or not a single element.
    """
    content = parse_unit(xml)
    if len(content) != 1 or isinstance(content[0], str):
        raise ValueError("a unit must be exactly one element")
    root = content[0]
    fields: Counter[Field] = Counter()

    def walk(element: etree._Element, path: str) -> None:
        attrs = tuple(sorted(
            (name, collapse(value)) for name, value in attributes(element) if name not in IGNORED_ATTRIBUTES
        ))
        fields[(path, attrs, collapse(element_text(element)))] += 1
        for child in element:
            if isinstance(child.tag, str):
                walk(child, f"{path}/{local_name(child.tag)}")

    walk(root, local_name(root.tag))
    return fields


@dataclass(frozen=True)
class UnitScore:
    """Counts for one unit; summed over units for micro-averaged scores."""

    exact: bool  # proposal has the same fields as gold
    needed: int
    made: int
    correct_edits: int
    gold_fields: int
    proposal_fields: int
    correct_fields: int


def score_unit(input_xml: str, gold_xml: str, proposal_xml: str | None) -> UnitScore:
    """Score one proposal. A missing proposal (None) is scored as if the input were kept."""
    input_f = unit_fields(input_xml)
    return score_fields(input_f, unit_fields(gold_xml), unit_fields(proposal_xml) if proposal_xml is not None else input_f)


def score_fields(input_f: Counter[FieldT], gold_f: Counter[FieldT], proposal_f: Counter[FieldT]) -> UnitScore:
    """Edit and field counts from the fields of the input, the gold version and the proposal."""
    needed_add, needed_del = gold_f - input_f, input_f - gold_f
    made_add, made_del = proposal_f - input_f, input_f - proposal_f
    correct = (made_add & needed_add).total() + (made_del & needed_del).total()
    return UnitScore(
        exact=proposal_f == gold_f,
        needed=needed_add.total() + needed_del.total(),
        made=made_add.total() + made_del.total(),
        correct_edits=correct,
        gold_fields=gold_f.total(),
        proposal_fields=proposal_f.total(),
        correct_fields=(proposal_f & gold_f).total(),
    )


def ratio(numerator: int, denominator: int) -> float | None:
    """numerator / denominator, None if the denominator is 0."""
    return numerator / denominator if denominator else None


def f1(precision: float | None, recall: float | None) -> float | None:
    """Harmonic mean; None if either is undefined."""
    if precision is None or recall is None:
        return None
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


@dataclass(frozen=True)
class AlignedUnit:
    """An input unit and the gold unit with the same text."""

    input: Unit
    gold: Unit


@dataclass(frozen=True)
class Alignment:
    """Units matched by their text, and the counts of those that could not be matched."""

    pairs: list[AlignedUnit]
    unmatched_input: int
    unmatched_gold: int


def _unit_text_key(unit: Unit) -> str:
    content = parse_unit(unit.xml)
    return "".join(text_key(c) if not isinstance(c, str) else c for c in content)


def align_units(input_units: Sequence[Unit], gold_units: Sequence[Unit]) -> Alignment:
    """
    Match input and gold units in order by their text, ignoring whitespace. Units whose text differs
    (the annotator split, merged or corrected them) are left out and counted.
    """
    input_keys = [_unit_text_key(u) for u in input_units]
    gold_keys = [_unit_text_key(u) for u in gold_units]
    pairs: list[AlignedUnit] = []
    matcher = SequenceMatcher(a=input_keys, b=gold_keys, autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            pairs.append(AlignedUnit(input_units[block.a + k], gold_units[block.b + k]))
    return Alignment(pairs, len(input_units) - len(pairs), len(gold_units) - len(pairs))


def align_documents(input_xml: str, gold_xml: str, unit_path: str | None) -> Alignment:
    """
    Find the units of both documents and align them.

    Raises:
        ValueError: a document is not well-formed, or the unit path is invalid.
    """
    return align_units(document_units(input_xml, unit_path), document_units(gold_xml, unit_path))
