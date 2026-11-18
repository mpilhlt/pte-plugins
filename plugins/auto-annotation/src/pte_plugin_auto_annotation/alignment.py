"""
Aligning the text of two versions of a document whose text differs, such as an annotated version
from an old text layer and a new extraction after text recognition (see transfer.py).

Both texts are given with their whitespace; the alignment maps every non-whitespace character of
the target to a non-whitespace character of the source, or to none (inserted). Characters are
compared after normalization (NFKC, so ligatures equal their letters; long s as s; soft hyphens
and zero-width characters dropped); the texts themselves are never changed.

The texts are aligned in two steps, so that documents of 100,000 characters align quickly: first
by words (whitespace-delimited), then by characters inside each pair of unequal word runs.

Pure module.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

_WORD_RE = re.compile(r"\S+")
# characters left out of the comparison
_DROPPED = {"­", "​", "‌", "‍", "⁠", "﻿"}
_REPLACED = {"ſ": "s", "‐": "-", "‑": "-", "–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "„": '"'}
# character comparison of an unequal region is skipped above this product of lengths (unrelated text)
MAX_REGION_PRODUCT = 4_000_000


def normalize_char(char: str) -> str:
    """The character as compared: '' for dropped characters, possibly several characters (ligatures)."""
    if char in _DROPPED:
        return ""
    char = _REPLACED.get(char, char)
    return unicodedata.normalize("NFKC", char)


@dataclass(frozen=True)
class Word:
    """A word of a text: its normalized characters and the position of each in the character stream."""

    norm: str
    positions: tuple[int, ...]  # stream position (non-whitespace character index) of each normalized character


def words_of(text: str) -> tuple[list[Word], int]:
    """
    The words of a text and the number of its non-whitespace characters (the length of its
    character stream).
    """
    words: list[Word] = []
    position = 0
    for match in _WORD_RE.finditer(text):
        norm: list[str] = []
        positions: list[int] = []
        for char in match.group():
            for n in normalize_char(char):
                norm.append(n)
                positions.append(position)
            position += 1
        if norm:
            words.append(Word("".join(norm), tuple(positions)))
    return words, position


@dataclass(frozen=True)
class Alignment:
    """The target's characters mapped to the source's (stream positions), None where inserted."""

    mapping: list[int | None]
    source_length: int

    @property
    def coverage(self) -> float:
        """Share of the source's characters that a target character is aligned to."""
        if not self.source_length:
            return 0.0
        return len({m for m in self.mapping if m is not None}) / self.source_length

    def aligned_share(self, start: int, end: int) -> float:
        """Share of the target characters in [start, end) that are aligned."""
        if end <= start:
            return 0.0
        return sum(m is not None for m in self.mapping[start:end]) / (end - start)


def _map_chars(source: Sequence[Word], target: Sequence[Word], mapping: list[int | None]) -> None:
    """Align the characters of two unequal runs of words and record the matches."""
    src_chars = "".join(w.norm for w in source)
    tgt_chars = "".join(w.norm for w in target)
    if not src_chars or not tgt_chars or len(src_chars) * len(tgt_chars) > MAX_REGION_PRODUCT:
        return
    src_pos = [p for w in source for p in w.positions]
    tgt_pos = [p for w in target for p in w.positions]
    matcher = SequenceMatcher(None, src_chars, tgt_chars, autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            target_position = tgt_pos[block.b + k]
            if mapping[target_position] is None:
                mapping[target_position] = src_pos[block.a + k]


def align_texts(source: str, target: str) -> Alignment:
    """Align the non-whitespace characters of `target` to those of `source` (see the module doc)."""
    src_words, src_length = words_of(source)
    tgt_words, tgt_length = words_of(target)
    mapping: list[int | None] = [None] * tgt_length
    matcher = SequenceMatcher(None, [w.norm for w in src_words], [w.norm for w in tgt_words], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for src_word, tgt_word in zip(src_words[i1:i2], tgt_words[j1:j2]):
                for s, t in zip(src_word.positions, tgt_word.positions):
                    if mapping[t] is None:
                        mapping[t] = s
        elif tag == "replace":
            _map_chars(src_words[i1:i2], tgt_words[j1:j2], mapping)
    return Alignment(mapping, src_length)
