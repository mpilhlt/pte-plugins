"""
Comparing the proposals of two models per unit, and the arbiter's prompt.

Two proposals agree when their canonical forms are equal (the unit review fingerprint ignores
indentation and attribute order). Units without agreement go to the arbiter, which sees the
original unit and the valid candidates, and answers with the unit it considers correct: one of
the candidates, the original, or its own correction. For segmenting variants a proposal, and an
original that groups several units, is a sequence of elements (possibly empty); proposals are
compared element by element (segments_key).

Pure module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .checks import ProposalStatus
from .correction import UnitOutcome
from .prompts import NO_CHANGES
from .units import Unit, canonical_form, parse_unit

# Version of the arbiter prompt; recorded in the run log
ARBITER_PROMPT_VERSION = "1"

ORIGINAL = "original"


def canonical_key(xml: str) -> str | None:
    """Canonical form of a unit's XML, or None if it is not exactly one well-formed element."""
    try:
        content = parse_unit(xml)
    except ValueError:
        return None
    if len(content) != 1 or isinstance(content[0], str):
        return None
    return canonical_form(content[0])


def segments_key(xml: str) -> tuple[str, ...] | None:
    """
    Canonical forms of the elements of a unit or of several units (empty for blank XML), or None if
    the XML is not well-formed or has text outside the elements.
    """
    if not xml.strip():
        return ()
    try:
        content = parse_unit(xml)
    except ValueError:
        return None
    if any(isinstance(item, str) for item in content):
        return None
    return tuple(canonical_form(item) for item in content if not isinstance(item, str))


def valid_proposal(outcome: UnitOutcome) -> str | None:
    """The outcome's proposal if it passed the checks (an UNCHANGED unit proposes the input), else None."""
    if outcome.status in (ProposalStatus.OK, ProposalStatus.UNCHANGED):
        return outcome.proposal
    return None


def agreed_proposal(outcomes: Sequence[UnitOutcome]) -> str | None:
    """The common proposal if all outcomes are valid and canonically equal, else None."""
    proposals = [valid_proposal(o) for o in outcomes]
    if not proposals or any(p is None for p in proposals):
        return None
    keys = {segments_key(p) for p in proposals if p is not None}
    return proposals[0] if len(keys) == 1 and None not in keys else None


def distinct_candidates(unit: Unit, proposals: Mapping[str, str | None]) -> dict[str, str]:
    """
    The valid proposals by label, without duplicates and without those equal to the original
    (the arbiter sees the original anyway). Labels of merged duplicates are joined with `+`.
    """
    original_key = segments_key(unit.xml)
    by_key: dict[tuple[str, ...], tuple[list[str], str]] = {}
    for label, xml in proposals.items():
        if xml is None:
            continue
        key = segments_key(xml)
        if key is None or key == original_key:
            continue
        labels, _ = by_key.setdefault(key, ([], xml))
        labels.append(label)
    return {"+".join(labels): xml for labels, xml in by_key.values()}


def render_candidates(unit: Unit, candidates: Mapping[str, str]) -> str:
    """What the arbiter sees for one unit."""
    parts = [f"Original:\n{unit.xml}"]
    for label, xml in candidates.items():
        parts.append(f"Candidate {label}:\n{xml}")
    if not candidates:
        parts.append("(No candidate: correct the original yourself.)")
    return "\n".join(parts)


def which_candidate(result_xml: str, unit: Unit, candidates: Mapping[str, str]) -> str:
    """The label of the candidate the arbiter's result equals, ORIGINAL, or `own`."""
    key = segments_key(result_xml)
    if key is not None and key == segments_key(unit.xml):
        return ORIGINAL
    for label, xml in candidates.items():
        if key is not None and key == segments_key(xml):
            return label
    return "own"


def build_arbiter_system_prompt(segmenting: bool = False) -> str:
    """Instructions for the arbiter; with segmenting, units may be split and merged."""
    segments = (
        "A unit stands for a stretch of the document that may have to be split into several elements or merged into one: "
        "the original and the candidates may consist of several elements, and your answer may too. Answer with all elements "
        "of the unit, one after the other, keeping the outermost element name and the whole text of the unit.\n\n"
        if segmenting else ""
    )
    return (
        "You review corrections of the TEI annotation of a document that was produced by a machine-learning tool. "
        "You are given annotation rules, each labeled with its category, followed by numbered units of the document. "
        "For each unit you see the original XML and up to two candidate corrections proposed by other models, which "
        "disagree.\n\n"
        f"{segments}Decide which version follows the rules. Answer with the correct XML of the unit: a candidate as it is, the "
        "original as it is, or your own correction if none of them is right.\n\n"
        "CRITICAL: never change, add, remove or reorder the text itself, only the markup around it. Keep the unit's "
        "outermost element. Do not add comments or explanations.\n\n"
        'Return every unit, in the same order, each under its own header line "### Unit <number>" followed by the XML '
        f'of the unit. Do not wrap the answer in a code fence, do not write "{NO_CHANGES}", and write nothing else.'
    )
