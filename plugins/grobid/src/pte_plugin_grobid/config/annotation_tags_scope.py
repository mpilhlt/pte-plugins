"""
Per-variant scope for schema-driven annotation tag generation: which
element is the "root" whose content-model children become chips, and
which of those children are excluded (no chip, no badge), and which chips
are display-only (`read_only`: shown as a badge, but not editable, not
offered in the palette and without coloring of their content, e.g. the
`text` root and the `<lb/>` and `<pb/>` markers of the PDF layout).

Unlike the annotation tag data itself (generated from the schema), this
config only changes if a variant's overall document shape changes, which
is rare — see docs/superpowers/specs/2026-09-05-annotation-chip-schema-redesign-design.md.
"""

from typing import NotRequired, TypedDict


class TagScope(TypedDict):
    root: str
    exclude: set[str]
    read_only: NotRequired[set[str]]


ANNOTATION_TAG_SCOPE: dict[str, TagScope] = {
    "grobid.training.segmentation": {
        "root": "text",
        "exclude": set(),
        "read_only": {"text", "lb", "pb"},
    },
    "grobid.training.references.referenceSegmenter": {
        "root": "bibl",
        "exclude": set(),
        "read_only": {"lb", "pb"},
    },
    "grobid.training.references": {
        "root": "bibl",
        "exclude": set(),
        "read_only": {"lb", "pb"},
    },
}
