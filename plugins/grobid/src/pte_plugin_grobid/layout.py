"""
Line layout of segmentation documents from GROBID's feature file (the core's layout provider API,
see `fastapi_app/lib/tei/layout.py` in pdf-tei-editor).

GROBID writes, with the segmentation training data, a feature file with one row per line of the
document (`FeaturesVectorSegmentation.printVector()`): the line's first token, block status
(`BLOCKSTART`/`BLOCKIN`/`BLOCKEND`), page status (`PAGESTART`/`PAGEIN`/`PAGEEND`), font status,
font size relative to the line before (`HIGHERFONT`/`SAMEFONTSIZE`/`LOWERFONT`), bold, italic, ...,
the relative position on the page, figures around the line, whether the line repeats on other
pages and whether it is in the page's main area. The grobid plugin keeps that file in its cache
(`training.zip`).

The rows are mapped to the `<lb/>`-delimited lines of `<text>` by their first token, with the
forward scan the feature-file lint uses (`extensions/grobid-sync.js`). Since text and `<lb/>`
never change, the mapping holds for every later version of the document. Lines without a row
inherit the page and hints of the line before. A page starts at every row with `PAGESTART`.

The columns are found by their values where possible (block, page and font size status), and the
others relative to the font size column, so that flavors which append columns are read as well.
"""

from __future__ import annotations

import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from pte_plugin_grobid.config.review_units import ZONE_LABELS

logger = logging.getLogger(__name__)

SEGMENTATION = "grobid.training.segmentation"

_BLOCK = {"BLOCKSTART", "BLOCKIN", "BLOCKEND"}
_PAGE = {"PAGESTART", "PAGEIN", "PAGEEND"}
_FONT_SIZE = {"HIGHERFONT", "SAMEFONTSIZE", "LOWERFONT"}
# columns after the font size column (printVector(): bold, italic, capitalisation, digit, single
# char, seven lexical flags, relative document position, relative page position, punctuation
# profile and count, line length, bitmap, vector, repetitive pattern, first repetitive pattern, main area)
_BOLD, _ITALIC, _PAGE_POSITION = 1, 2, 14
_BITMAP, _VECTOR, _REPEATED, _MAIN_AREA = 18, 19, 20, 22
# relative page position is discretized into 12 bins
_TOP_BIN, _BOTTOM_BIN = 0, 11
# share of the lines whose first token must be found in the feature file
MIN_MATCHED_SHARE = 0.5
# lookahead of the alignment, as in grobid-sync.js
WINDOW = 5


@dataclass(frozen=True)
class FeatureRow:
    """The layout of one feature file row."""

    token: str
    page_start: bool
    hints: tuple[str, ...] = field(default_factory=tuple)


def _find(columns: list[str], values: set[str], start: int = 1) -> int | None:
    return next((i for i in range(start, len(columns)) if columns[i] in values), None)


def parse_row(columns: list[str]) -> FeatureRow | None:
    """The layout of a feature file row (its whitespace-separated columns); None if it has no page status."""
    if not columns:
        return None
    page = _find(columns, _PAGE, 7)
    if page is None:
        return None
    hints: list[str] = []
    block = _find(columns, _BLOCK, 7)
    if block is not None and columns[block] == "BLOCKSTART":
        hints.append("block")
    size = _find(columns, _FONT_SIZE, page + 1)
    if size is not None:

        def flag(offset: int) -> bool:
            return size + offset < len(columns) and columns[size + offset] == "1"

        def number(offset: int) -> int | None:
            if size + offset >= len(columns):
                return None
            try:
                return int(columns[size + offset])
            except ValueError:
                return None

        position = number(_PAGE_POSITION)
        if position is not None and position <= _TOP_BIN:
            hints.append("top")
        elif position is not None and position >= _BOTTOM_BIN:
            hints.append("bottom")
        if flag(_REPEATED):
            hints.append("repeated")
        if columns[size] == "HIGHERFONT":
            hints.append("larger")
        elif columns[size] == "LOWERFONT":
            hints.append("smaller")
        if flag(_BOLD):
            hints.append("bold")
        if flag(_ITALIC):
            hints.append("italic")
        if flag(_BITMAP) or flag(_VECTOR):
            hints.append("figure")
        if size + _MAIN_AREA < len(columns) and columns[size + _MAIN_AREA] == "0":
            hints.append("margin")
    return FeatureRow(columns[0], columns[page] == "PAGESTART", tuple(hints))


def read_feature_rows(zip_path: Path, suffix: str = "segmentation") -> list[FeatureRow] | None:
    """The rows of the feature file for `suffix` in a cached training ZIP; None if there is none."""
    if not zip_path.exists():
        return None
    entry_suffix = f".training.{suffix}"
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            entry = next((n for n in zf.namelist() if n.endswith(entry_suffix) and not n.endswith(".tei.xml")), None)
            if entry is None:
                return None
            raw = zf.read(entry).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    rows: list[FeatureRow] = []
    for line in raw.splitlines():
        row = parse_row(line.split())
        if row is not None:
            rows.append(row)
    return rows


def align(line_tokens: list[str | None], row_tokens: list[str]) -> list[int | None]:
    """
    The row of every line (None for lines without a row), by first tokens: a forward scan that
    skips rows or lines where a lookahead of WINDOW finds the token again, and otherwise pairs
    the line with the next row.
    """
    result: list[int | None] = [None] * len(line_tokens)
    li = ri = 0
    while li < len(line_tokens) and ri < len(row_tokens):
        token = line_tokens[li]
        if token is None:
            li += 1
            continue
        if token == row_tokens[ri]:
            result[li] = ri
            li += 1
            ri += 1
            continue
        skipped = False
        for d in range(1, WINDOW + 1):
            if ri + d < len(row_tokens) and row_tokens[ri + d] == token:
                # rows without a line (GROBID's line the XML does not have)
                ri += d
                skipped = True
                break
            if li + d < len(line_tokens) and line_tokens[li + d] == row_tokens[ri]:
                # lines without a row (GROBID filtered them)
                li += d
                skipped = True
                break
        if not skipped:
            result[li] = ri
            li += 1
            ri += 1
    return result


def line_layout_from_rows(line_tokens: list[str | None], rows: list[FeatureRow]) -> tuple[list[int], list[list[str]]] | None:
    """
    (page index of every line, hints of every line), or None if fewer than MIN_MATCHED_SHARE of the
    lines with text have a row with their first token.
    """
    if not rows:
        return None
    mapping = align(line_tokens, [r.token for r in rows])
    with_text = [i for i, t in enumerate(line_tokens) if t is not None]
    matched = sum(1 for i in with_text if mapping[i] is not None and rows[mapping[i]].token == line_tokens[i])  # type: ignore[index]
    if not with_text or matched / len(with_text) < MIN_MATCHED_SHARE:
        return None
    row_pages: list[int] = []
    page = -1
    for row in rows:
        if row.page_start or page < 0:
            page += 1
        row_pages.append(page)
    pages: list[int] = []
    hints: list[list[str]] = []
    for row_index in mapping:
        if row_index is not None:
            pages.append(row_pages[row_index])
            hints.append(list(rows[row_index].hints))
        else:
            pages.append(pages[-1] if pages else 0)
            hints.append([h for h in hints[-1] if h != "block"] if hints else [])
    return pages, hints


def first_token(text: str) -> str | None:
    """The first whitespace-delimited word of a line's text."""
    words = text.split()
    return words[0] if words else None


def segmentation_layout(xml: str, doc_id: str):  # -> LineLayout | None (core type, imported lazily)
    """
    Layout provider of `grobid.training.segmentation`: the line layout of a document from the
    cached feature file of its extraction (revision and flavor from its encodingDesc), or None.
    """
    from fastapi_app.plugin_api import LineLayout, zone_lines

    from pte_plugin_grobid.cache import get_cache_dir, get_cache_key
    from pte_plugin_grobid.config import get_feature_suffix
    from pte_plugin_grobid.sync import parse_encoding_labels

    labels = parse_encoding_labels(xml)
    revision = labels.get("revision")
    if not revision or labels.get("variant-id", SEGMENTATION) != SEGMENTATION:
        return None
    zip_path = get_cache_dir() / get_cache_key(doc_id, revision, labels.get("flavor", "default")) / "training.zip"
    rows = read_feature_rows(zip_path, get_feature_suffix(SEGMENTATION))
    if rows is None:
        return None
    try:
        doc = zone_lines.read_zone_document(xml, zone_lines.zone_labels(ZONE_LABELS[SEGMENTATION]))
    except ValueError as e:
        logger.info(f"grobid layout: {doc_id} cannot be read as zones: {e}")
        return None
    tokens = [first_token(" ".join(p.text for p in line.pieces)) for line in doc.lines]
    result = line_layout_from_rows(tokens, rows)
    if result is None:
        logger.warning(f"grobid layout: the feature file of {doc_id} does not match the document's lines")
        return None
    return LineLayout(pages=result[0], hints=result[1])
