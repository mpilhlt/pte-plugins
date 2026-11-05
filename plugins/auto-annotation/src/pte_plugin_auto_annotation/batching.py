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

_SPEC_RE = re.compile(r"^(units|chars):(\d+)$")


@dataclass(frozen=True)
class BatchSpec:
    """
    How units are grouped: at most `max_units` units, or at most `max_chars` characters of unit XML
    per batch. A unit longer than `max_chars` forms a batch of its own.
    """

    max_units: int | None = None
    max_chars: int | None = None

    def __post_init__(self) -> None:
        if (self.max_units is None) == (self.max_chars is None):
            raise ValueError("A batch spec needs exactly one of max_units and max_chars")
        if (self.max_units or self.max_chars or 0) < 1:
            raise ValueError("Batch limits must be positive")

    @property
    def label(self) -> str:
        """`units:8` or `chars:4000`; parse() reads it back."""
        return f"units:{self.max_units}" if self.max_units is not None else f"chars:{self.max_chars}"

    @classmethod
    def parse(cls, spec: str) -> "BatchSpec":
        """
        Parse `units:N` or `chars:N`.

        Raises:
            ValueError: not in either form.
        """
        match = _SPEC_RE.match(spec.strip())
        if not match:
            raise ValueError(f'Invalid batch spec "{spec}" (expected units:N or chars:N)')
        value = int(match.group(2))
        return cls(max_units=value) if match.group(1) == "units" else cls(max_chars=value)


def make_batches(units: Sequence[T], spec: BatchSpec) -> list[list[T]]:
    """Split units, in order, into consecutive batches according to spec."""
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
