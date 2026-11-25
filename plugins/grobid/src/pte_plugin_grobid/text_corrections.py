"""
Corrected-PDF handler of segmentation documents (the core's corrected-PDF handler API, plugin API
1.8, see `fastapi_app/lib/ocr/text_corrections.py` in pdf-tei-editor, pdf-tei-editor#931).

When the editor writes the text corrections of a document into the OCR text layer of its PDF
(Tools > Write text corrections to PDF), the PDF gets a new content hash, and GROBID's feature
file of the old PDF no longer belongs to it. Before the editor replaces the PDF, it calls
`corrected_pdf_handler()`, which fetches the training data of the corrected PDF from GROBID,
caches it under the new PDF hash and returns the document with the `revision` and `pdf-hash`
labels pointing to that cache entry. The text of the document is not changed.

The handler refuses the corrected PDF (`CorrectedPdfMismatch`, and the editor replaces nothing)
if its feature file matches the lines of the document worse than the old one: every line that
matched before must match. Corrected lines that still do not match are logged.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from collections.abc import Mapping
from pathlib import Path

from pte_plugin_grobid.cache import cache_training_data, get_cache_zip
from pte_plugin_grobid.config import get_feature_suffix, get_grobid_server_url
from pte_plugin_grobid.handlers.training import TrainingHandler
from pte_plugin_grobid.layout import MIN_MATCHED_SHARE, SEGMENTATION, _zone_document, align, first_token, read_feature_rows
from pte_plugin_grobid.reload_feature_file import check_grobid_revision
from pte_plugin_grobid.sync import parse_encoding_labels, set_pdf_hash_label, set_revision_label

logger = logging.getLogger(__name__)

MISMATCH_MESSAGE = "The feature file of the corrected PDF does not match these lines of the document."


def _line_tokens(xml: str, doc_id: str) -> list[str | None] | None:
    """The first token of every zone line of a segmentation document, or None if it cannot be read as zones."""
    doc = _zone_document(xml, doc_id)
    if doc is None:
        return None
    return [first_token(" ".join(p.text for p in line.pieces)) for line in doc.lines]


def unmatched_lines(xml: str, doc_id: str, labels: Mapping[str, str]) -> set[int] | None:
    """
    The indexes of the zone lines with text whose first token has no row with that token in the
    cached feature file for `labels` (`revision`, `flavor`, `pdf-hash`, see cache.get_cache_zip()),
    by the alignment of the layout provider (layout.align()); None if there is no such cache entry,
    it has no feature file, or the document cannot be read as zones.
    """
    tokens = _line_tokens(xml, doc_id)
    if tokens is None:
        return None
    zip_path = get_cache_zip(doc_id, dict(labels))
    if zip_path is None:
        return None
    rows = read_feature_rows(zip_path, get_feature_suffix(SEGMENTATION))
    if rows is None:
        return None
    mapping = align(tokens, [r.token for r in rows])
    return {i for i, token in enumerate(tokens) if token is not None and ((r := mapping[i]) is None or rows[r].token != token)}


def corrected_pdf_handler(xml: str, doc_id: str, pdf_path: Path, pdf_hash: str, corrected_lines: frozenset[int]) -> str:
    """
    Corrected-PDF handler of `grobid.training.segmentation`: fetches the training data of the
    corrected PDF from GROBID, caches it under `pdf_hash` and the current GROBID revision, and
    returns `xml` with the `revision` and `pdf-hash` labels set to them. Called by the editor in
    a worker thread, before it replaces the PDF.

    The feature file of the corrected PDF must match the document's lines at least as well as
    the one of the old PDF: with `before` and `after` the unmatched lines (unmatched_lines())
    of the old and the new feature file, every line in `after` must have been unmatched before.
    Corrected lines that are still unmatched are only logged: a correction in the middle of a
    line (a footnote marker, say) does not change a first token that differs for other reasons
    (reading order, tokenization). If there is no cache entry for the old PDF, `after` is accepted
    if at least MIN_MATCHED_SHARE of the lines with text match, as the layout provider requires.
    Otherwise raises CorrectedPdfMismatch with the offending lines; the new cache entry is then
    left to the cache garbage collection, since no document refers to it.

    Raises RuntimeError if GROBID is not configured or unreachable, the fetch fails (also when
    GROBID returns no ZIP), the document
    has no `revision` label or the package has no segmentation feature file.
    """
    from fastapi_app.plugin_api import CorrectedPdfMismatch

    labels = parse_encoding_labels(xml)
    old_revision = labels.get("revision")
    if not old_revision:
        raise RuntimeError("The document has no GROBID revision label, so its feature file cannot be replaced.")
    grobid_server_url = get_grobid_server_url()
    if not grobid_server_url:
        raise RuntimeError("No GROBID server is configured.")
    tokens = _line_tokens(xml, doc_id)
    if tokens is None:
        raise RuntimeError("The document cannot be read as zones.")

    before = unmatched_lines(xml, doc_id, labels)
    _, revision = check_grobid_revision(grobid_server_url)
    flavor = labels.get("flavor", "default")
    try:
        temp_dir, files = TrainingHandler()._fetch_training_package(str(pdf_path), grobid_server_url, flavor)
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"GROBID did not return a training package for the corrected PDF: {e}") from e
    try:
        cache_training_data(doc_id, revision, flavor, temp_dir, files, pdf_hash=pdf_hash)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    updated = set_pdf_hash_label(set_revision_label(xml, old_revision, revision), pdf_hash)
    after = unmatched_lines(updated, doc_id, {**labels, "revision": revision, "pdf-hash": pdf_hash})
    if after is None:
        raise RuntimeError("GROBID returned no segmentation feature file for the corrected PDF.")

    if before is None:
        with_text = sum(1 for t in tokens if t is not None)
        offending = after if with_text and (with_text - len(after)) / with_text < MIN_MATCHED_SHARE else set()
    else:
        offending = after - before
    still_unmatched = sorted(after & corrected_lines)
    if still_unmatched:
        logger.warning(f"grobid: {len(still_unmatched)} corrected line(s) {still_unmatched} of {doc_id} still do not match the feature file of the corrected PDF")
    if offending:
        logger.warning(f"grobid: the feature file of the corrected PDF of {doc_id} does not match lines {sorted(offending)}")
        raise CorrectedPdfMismatch(MISMATCH_MESSAGE, sorted(offending))
    return updated
