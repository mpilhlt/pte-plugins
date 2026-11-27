"""
Restore the dashes of the PDF, and the spaces after them, in GROBID's TEI.

GROBID normalizes every dash character (Unicode class Pd and U+2212 MINUS SIGN) to `-` in the
text it reads from the PDF (`UnicodeUtil.normaliseText()`), so its TEI has `-` where the PDF has
`—` or `–`. It also adds no space after a token that ends with a dash, so that a hyphen at the end
of a line stays attached to its word (`PDFALTOSaxHandler`); only newer versions restore the space
if pdfalto reports a wide enough one. In OCR text this turns "— II." into "-II." and "rechts- und"
into "rechts-und".

`restore_dashes()` puts the PDF's characters back: for each `-` in the `<text>` of the TEI, it
looks for the surrounding characters (without whitespace) in the text of the PDF, and if they occur
exactly once, takes the dash found there, and adds a space after it if the PDF has one there on the
same line and the TEI has none.

GROBID's trainers normalize the tokens of the TEI in the same way before matching them with the
feature file, so the restored dashes do not affect training. Comparisons with the feature file in
this plugin fold the dashes with `fold_dashes()`.
"""

from __future__ import annotations

import html
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DASHES = frozenset("-֊־᐀᠆‐‑‒–—―⸗⸚⸺⸻⹀〜〰゠︱︲﹘﹣－−")
"""The characters GROBID normalizes to '-': Unicode class Pd (dash punctuation) and the minus sign."""

_FOLD = str.maketrans({c: "-" for c in DASHES})

CONTEXT = 12
"""Characters (without whitespace) on each side of a dash that locate it in the PDF text; doubled once if they are ambiguous."""

_TEXT_RE = re.compile(r"<text[\s>/]")
_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")


def fold_dashes(text: str) -> str:
    """The text with every dash replaced by '-', as GROBID normalizes it."""
    return text.translate(_FOLD)


def pdf_text(pdf_path: str | Path) -> str:
    """The text of all pages of a PDF, as pdfium extracts it (including invisible OCR text)."""
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        parts: list[str] = []
        for index in range(len(pdf)):
            page = pdf[index]
            textpage = page.get_textpage()
            try:
                parts.append(textpage.get_text_range())
            finally:
                textpage.close()
                page.close()
        return "\n".join(parts)
    finally:
        pdf.close()


def _text_chars(xml: str) -> list[tuple[str, int, int]]:
    """The non-whitespace characters of the `<text>` element outside of markup, decoded, with their span in `xml`."""
    match = _TEXT_RE.search(xml)
    if match is None:
        return []
    end = xml.rfind("</text>")
    end = len(xml) if end < match.start() else end
    chars: list[tuple[str, int, int]] = []
    i = match.start()
    while i < end:
        c = xml[i]
        if c == "<":
            close = xml.find("-->", i) + 3 if xml.startswith("<!--", i) else xml.find(">", i) + 1
            i = close if close > i else end
            continue
        if c == "&":
            entity = _ENTITY_RE.match(xml, i)
            if entity is not None:
                for d in html.unescape(entity.group(0)):
                    if not d.isspace():
                        chars.append((d, i, entity.end()))
                i = entity.end()
                continue
        if not c.isspace():
            chars.append((c, i, i + 1))
        i += 1
    return chars


def restore_dashes(xml: str, source_text: str) -> tuple[str, int]:
    """
    The TEI with the `-` of its `<text>` replaced by the dashes of `source_text` (the text of the
    PDF), and spaces added after them as in `source_text`, where they can be located unambiguously;
    and the number of changed dashes.
    """
    positions = [i for i, c in enumerate(source_text) if not c.isspace()]
    original = "".join(source_text[i] for i in positions)
    folded_pdf = fold_dashes(original)
    chars = _text_chars(xml)
    folded_tei = fold_dashes("".join(c for c, _, _ in chars))
    replacements: list[tuple[int, int, str]] = []
    for index, (c, start, end) in enumerate(chars):
        if c != "-" or end - start != 1:
            continue
        for width in (CONTEXT, 2 * CONTEXT):
            left = folded_tei[max(0, index - width) : index]
            pattern = left + "-" + folded_tei[index + 1 : index + 1 + width]
            found = folded_pdf.find(pattern)
            if found < 0:
                break
            if folded_pdf.find(pattern, found + 1) >= 0:
                continue  # ambiguous: try a wider context
            at = found + len(left)
            dash = original[at]
            after = source_text[positions[at] + 1 : positions[at] + 2]
            space = after.isspace() and after not in "\r\n\f\v\u2028\u2029" and end < len(xml) and not xml[end].isspace() and xml[end] != "<"
            if dash != "-" or space:
                replacements.append((start, end, dash + (" " if space else "")))
            break
    for start, end, dash in reversed(replacements):
        xml = xml[:start] + dash + xml[end:]
    return xml, len(replacements)
