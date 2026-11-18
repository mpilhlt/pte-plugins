"""Review units of the editor's unit review mode for each GROBID variant, and the zone labels of zone documents."""

# Path of the elements below TEI/text that are reviewed (and marked) as one unit, keyed by
# variant id. Steps are element names or `*`, separated by `/` (child) or `//` (descendant).
# Variants without an entry use the editor's automatic default (the children of the first
# element below <text> that has more than one child element).
ReviewUnits = dict[str, str]


REVIEW_UNITS: ReviewUnits = {
    # the pages of the document, from the layout in GROBID's feature file (see layout.py); editors
    # without the layout API (plugin API < 1.5) get the zones instead, see get_review_units()
    "grobid.training.segmentation": "pages",
    # one reference each
    "grobid.training.references.referenceSegmenter": ".//listBibl/bibl",
    "grobid.training.references": ".//listBibl/bibl",
}


# Variants whose review units a correction may split and merge, because the model decides where one
# unit ends and the next begins (the reference segmenter cuts the reference list into references).
# Tools such as the auto-annotation plugin then compare the units by their text positions.
SEGMENTING_VARIANTS: tuple[str, ...] = ("grobid.training.references.referenceSegmenter",)


# The zones of zone documents: the children of <text> partition the text into zones, so the review
# unit is the page and models see the text as lines with zone labels (see the editor's
# fastapi_app/lib/tei/zone_lines.py). One entry per zone element of fossil's "Document
# segmentation model" table, with the short code shown to models and reviewers.
ZoneLabels = dict[str, list[dict[str, object]]]


def _zone(code: str, element: str, **attributes: str) -> dict[str, object]:
    return {"code": code, "element": element, "attributes": attributes}


ZONE_LABELS: ZoneLabels = {
    "grobid.training.segmentation": [
        _zone("titlePage", "titlePage"),
        _zone("front", "front"),
        _zone("body", "body"),
        _zone("listBibl", "listBibl"),
        _zone("page", "page"),
        _zone("note:headnote", "note", place="headnote"),
        _zone("note:footnote", "note", place="footnote"),
        _zone("div:toc", "div", type="toc"),
        _zone("div:annex", "div", type="annex"),
        _zone("div:acknowledgement", "div", type="acknowledgement"),
        _zone("div:acknowledgment", "div", type="acknowledgment"),
        _zone("div:availability", "div", type="availability"),
        _zone("div:funding", "div", type="funding"),
        _zone("div:conflict", "div", type="conflict"),
        _zone("div:contribution", "div", type="contribution"),
    ],
}

# Unit path of zone documents in editors without the layout API
ZONE_FALLBACK_UNIT_PATH = "./*"
