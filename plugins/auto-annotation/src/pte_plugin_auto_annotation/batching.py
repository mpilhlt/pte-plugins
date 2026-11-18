"""
Grouping review units into the batches sent to a model in one request.

Pure module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence, TypeVar

from .units import Unit

T = TypeVar("T", bound=Unit)

_SPEC_RE = re.compile(r"^(units|pages|chars):(\d+|all)$")


@dataclass(frozen=True)
class BatchSpec:
    """
    How units are grouped: at most `max_units` units, or at most `max_chars` characters of unit XML
    per batch, or all units in one batch (`whole`). A unit longer than `max_chars` forms a batch of
    its own. The units of zone documents are pages (`pages:N`, `pages:all`; see zones.py), whose
    size is the length of their line listing.
    """

    max_units: int | None = None
    max_chars: int | None = None
    whole: bool = False
    # How the units are called in the label: "units" or "pages"
    unit_name: str = "units"

    def __post_init__(self) -> None:
        if sum((self.max_units is not None, self.max_chars is not None, self.whole)) != 1:
            raise ValueError("A batch spec needs exactly one of max_units, max_chars and whole")
        if not self.whole and (self.max_units or self.max_chars or 0) < 1:
            raise ValueError("Batch limits must be positive")
        if self.unit_name not in ("units", "pages"):
            raise ValueError(f'Unknown unit name "{self.unit_name}"')

    @property
    def label(self) -> str:
        """`units:8`, `pages:4`, `pages:all` or `chars:4000`; parse() reads it back."""
        if self.max_chars is not None:
            return f"chars:{self.max_chars}"
        return f"{self.unit_name}:{'all' if self.whole else self.max_units}"

    @property
    def is_pages(self) -> bool:
        """Whether the spec counts pages (only for zone documents)."""
        return self.unit_name == "pages"

    @classmethod
    def parse(cls, spec: str) -> "BatchSpec":
        """
        Parse `units:N`, `pages:N`, `chars:N`, `units:all` or `pages:all`.

        Raises:
            ValueError: not in one of these forms.
        """
        match = _SPEC_RE.match(spec.strip())
        if not match or (match.group(1) == "chars" and match.group(2) == "all"):
            raise ValueError(f'Invalid batch spec "{spec}" (expected units:N, pages:N, pages:all or chars:N)')
        kind, value = match.group(1), match.group(2)
        if kind == "chars":
            return cls(max_chars=int(value))
        if value == "all":
            return cls(whole=True, unit_name=kind)
        return cls(max_units=int(value), unit_name=kind)


def make_batches(units: Sequence[T], spec: BatchSpec) -> list[list[T]]:
    """Split units, in order, into consecutive batches according to spec."""
    if spec.whole:
        return [list(units)] if units else []
    batches: list[list[T]] = []
    current: list[T] = []
    size = 0
    for unit in units:
        if current and (
            (spec.max_units is not None and len(current) >= spec.max_units)
            or (spec.max_chars is not None and size + len(unit.xml) > spec.max_chars)
        ):
            batches.append(current)
            current, size = [], 0
        current.append(unit)
        size += len(unit.xml)
    if current:
        batches.append(current)
    return batches
