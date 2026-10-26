"""Annotation guide URLs for GROBID variants.

Each entry names the variant(s) it applies to (`variant_ids`; the literal
"*" is planned to mean "every variant", once the matching logic supports it)
and the rule category it belongs to ("primary" is the sentinel for the
per-variant main section a human annotator should read; other categories are
optional, machine-only excerpts for LLM validation). URLs are branch-relative
GitHub blob links; they get permalink-pinned to a commit SHA when embedded
into a document's editorialDecl, and a heading-anchor URL is additionally
used to derive a machine-targeted (line-range) sibling ref - see
plugins/grobid/src/pte_plugin_grobid/annotation_rules.py and
docs/superpowers/specs/2026-09-22-annotation-guide-dual-target-refs-design.md.
The fossil guides set `machine_url` to the same section of
docs/guidelines-llm.md, fossil's condensed version of the guidelines for
LLMs, so the machine ref (what the LLM Reviewer sends) costs about a quarter
of the tokens of the full section.
"""

from typing import Literal, NotRequired, TypedDict


class AnnotationGuide(TypedDict):
    """
    A link to an annotation guide for one or more variants and a rule category.

    `machine_url` optionally points at a condensed, LLM-only version of the
    same section (a markdown heading-anchor or line-range URL). If set, the
    "machine" ref sent to LLMs is derived from it instead of from `url`,
    which then serves human readers only.

    `condense_prompt` optionally overrides the editor's default prompt for
    condensing this guide's section into its machine layer.
    """

    variant_ids: list[str]
    category: str
    type: Literal["markdown", "html"]
    url: str
    label: str
    machine_url: NotRequired[str]
    condense_prompt: NotRequired[str]


ANNOTATION_GUIDES: list[AnnotationGuide] = [
    {
        "variant_ids": ["grobid.training.segmentation"],
        "category": "primary",
        "type": "markdown",
        "url": "https://github.com/mpilhlt/fossil/blob/main/docs/guidelines.md#document-segmentation-model",
        "machine_url": "https://github.com/mpilhlt/fossil/blob/main/docs/guidelines-llm.md#document-segmentation-model",
        "label": "Document segmentation guidelines",
    },
    {
        "variant_ids": ["grobid.training.references.referenceSegmenter"],
        "category": "primary",
        "type": "markdown",
        "url": "https://github.com/mpilhlt/fossil/blob/main/docs/guidelines.md#reference-segmentation-model",
        "machine_url": "https://github.com/mpilhlt/fossil/blob/main/docs/guidelines-llm.md#reference-segmentation-model",
        "label": "Reference segmentation guidelines",
    },
    {
        "variant_ids": ["grobid.training.references"],
        "category": "primary",
        "type": "markdown",
        "url": "https://github.com/mpilhlt/fossil/blob/main/docs/guidelines.md#citation-model",
        "machine_url": "https://github.com/mpilhlt/fossil/blob/main/docs/guidelines-llm.md#citation-model",
        "label": "Citation model guidelines",
    },
    {
        "variant_ids": ["*"],
        "category": "data-correction",
        "type": "markdown",
        "url": "https://github.com/mpilhlt/fossil/blob/main/docs/guidelines.md#data-correction",
        "machine_url": "https://github.com/mpilhlt/fossil/blob/main/docs/guidelines-llm.md#data-correction",
        "label": "Data correction guidelines",
    },
]
