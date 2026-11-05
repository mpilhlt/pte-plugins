"""
Prompts for correcting a batch of review units, and parsing of the model's answer.

Units are sent and returned in a plain delimited format instead of JSON, so the model never has
to escape the quotes of XML attributes, and a truncated answer still yields its complete units:

    ### Unit 3
    <bibl>...</bibl>

Pure module.
"""

from __future__ import annotations

import re
from typing import Literal, Sequence

from .units import Unit

OutputMode = Literal["all", "changed"]
OUTPUT_MODES: tuple[OutputMode, ...] = ("all", "changed")

# Version of the prompt texts; recorded in every evaluation so that results stay comparable
PROMPT_VERSION = "1"

_HEADER_RE = re.compile(r"^###\s*Unit\s+(\d+)\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^```[\w-]*\s*$", re.MULTILINE)
NO_CHANGES = "NO CHANGES"


def build_system_prompt(mode: OutputMode) -> str:
    """Instructions for correcting the annotation of a batch of units."""
    if mode == "all":
        answer = (
            "Return every unit you were given, in the same order, each under its own header line "
            '"### Unit <number>" followed by the corrected XML of the unit (unchanged if it is already correct).'
        )
    else:
        answer = (
            "Return only the units you changed, each under its own header line \"### Unit <number>\" followed by "
            f'the corrected XML of the unit. If no unit needs a change, answer with the single line "{NO_CHANGES}".'
        )
    return (
        "You correct the TEI annotation of a document that was produced by a machine-learning tool. You are given "
        "annotation rules, each labeled with its category, followed by numbered units of the document (one XML "
        "element each).\n\n"
        "For every unit, fix the markup so that it follows the rules: add missing elements and attributes, remove "
        "wrong ones, and move element boundaries where they are wrong.\n\n"
        "CRITICAL: never change, add, remove or reorder the text itself, only the markup around it. Keep the unit's "
        "outermost element. Do not add comments or explanations.\n\n"
        f"{answer} Do not wrap the answer in a code fence and write nothing else."
    )


def format_units(units: Sequence[Unit], ids: Sequence[int]) -> str:
    """The delimited unit list, with the given ids."""
    return "\n\n".join(f"### Unit {uid}\n{unit.xml}" for uid, unit in zip(ids, units, strict=True))


def build_user_prompt(rules: Sequence[tuple[str, str]], units: Sequence[Unit], ids: Sequence[int]) -> str:
    """Rules first (so providers with prefix caching can reuse them across batches), then the units."""
    rules_block = "\n\n".join(f"## Rule category: {category}\n\n{text}" for category, text in rules)
    return f"{rules_block}\n\n## Units\n\n{format_units(units, ids)}"


def parse_unit_answer(raw: str) -> dict[int, str]:
    """
    The units in a model's answer, by id. Code fences and text before the first header are ignored;
    a repeated id keeps its first occurrence. An answer without headers (including NO CHANGES) yields {}.
    """
    text = _FENCE_RE.sub("", raw)
    headers = list(_HEADER_RE.finditer(text))
    units: dict[int, str] = {}
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        uid = int(header.group(1))
        if uid not in units:
            units[uid] = text[header.end():end].strip()
    return units
