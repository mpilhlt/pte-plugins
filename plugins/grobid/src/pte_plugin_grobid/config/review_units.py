"""Review units of the editor's unit review mode for each GROBID variant."""

# Path of the elements below TEI/text that are reviewed (and marked) as one unit, keyed by
# variant id. Steps are element names or `*`, separated by `/` (child) or `//` (descendant).
# Variants without an entry use the editor's automatic default (the children of the first
# element below <text> that has more than one child element).
ReviewUnits = dict[str, str]


REVIEW_UNITS: ReviewUnits = {
    # the document sections: front, body, listBibl, note, page, ...
    "grobid.training.segmentation": "./*",
    # one reference each
    "grobid.training.references.referenceSegmenter": ".//listBibl/bibl",
    "grobid.training.references": ".//listBibl/bibl",
}


# Variants whose review units a correction may split and merge, because the model decides where one
# unit ends and the next begins (the reference segmenter cuts the reference list into references).
# Tools such as the auto-annotation plugin then compare the units by their text positions.
SEGMENTING_VARIANTS: tuple[str, ...] = ("grobid.training.references.referenceSegmenter",)
