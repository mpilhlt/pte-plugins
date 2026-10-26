"""
Unit tests for plugins/grobid/src/pte_plugin_grobid/annotation_rules.py.

Run manually:
    uv run --project "$PTE_ROOT" python -m pytest plugins/grobid/src/pte_plugin_grobid/tests/test_annotation_rules.py -v

@testCovers plugins/grobid/src/pte_plugin_grobid/annotation_rules.py
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))

from pte_plugin_grobid.annotation_rules import GrobidRulesProvider, build_editorial_decl_entries


class TestBuildEditorialDeclEntriesVariantMatching(unittest.TestCase):
    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_matches_variant_listed_explicitly(self, mock_get_guides, mock_resolve):
        mock_get_guides.return_value = [
            {"variant_ids": ["grobid.training.segmentation"], "category": "primary",
             "type": "markdown", "url": "https://github.com/x/y/blob/main/g.md", "label": "Guide"},
        ]
        mock_resolve.side_effect = lambda url, cache: url

        entries = build_editorial_decl_entries("grobid.training.segmentation", MagicMock())
        guide_entries = [e for e in entries if e["category"] == "primary"]
        self.assertEqual(len(guide_entries), 1)

    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_wildcard_matches_any_variant(self, mock_get_guides, mock_resolve):
        mock_get_guides.return_value = [
            {"variant_ids": ["*"], "category": "house-style",
             "type": "markdown", "url": "https://github.com/x/y/blob/main/g.md", "label": "Guide"},
        ]
        mock_resolve.side_effect = lambda url, cache: url

        entries = build_editorial_decl_entries("grobid.training.header", MagicMock())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["category"], "house-style")

    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_returns_empty_list_for_unconfigured_variant(self, mock_get_guides, mock_resolve):
        mock_get_guides.return_value = [
            {"variant_ids": ["grobid.training.segmentation"], "category": "primary",
             "type": "markdown", "url": "https://github.com/x/y/blob/main/g.md", "label": "Guide"},
        ]
        entries = build_editorial_decl_entries("grobid.training.header", MagicMock())
        self.assertEqual(entries, [])
        mock_resolve.assert_not_called()


class TestBuildRefsForGuideShapeTable(unittest.TestCase):
    """
    Exercises the four ref-generation shapes documented in
    annotation_rules.py's _build_refs_for_guide(): no fragment, an
    already-machine line-range fragment, a heading-anchor with type "html",
    and a heading-anchor with type "markdown" (which additionally attempts
    translation to a machine ref).
    """

    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_no_fragment_produces_human_only(self, mock_get_guides, mock_resolve):
        mock_get_guides.return_value = [
            {"variant_ids": ["v1"], "category": "primary", "type": "markdown",
             "url": "https://github.com/x/y/blob/main/g.md", "label": "Guide"},
        ]
        mock_resolve.side_effect = lambda url, cache: url.replace("main", "sha1")

        entries = build_editorial_decl_entries("v1", MagicMock())
        self.assertEqual(entries, [
            {"category": "primary", "refs": [
                {"target": "https://github.com/x/y/blob/sha1/g.md", "content_type": "markdown", "subtype": "human"},
            ], "n": "Guide"},
        ])

    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_line_range_fragment_produces_machine_only(self, mock_get_guides, mock_resolve):
        mock_get_guides.return_value = [
            {"variant_ids": ["v1"], "category": "footnote-annotation", "type": "markdown",
             "url": "https://github.com/x/y/blob/main/g.md#L10-L50", "label": "Guide"},
        ]
        mock_resolve.side_effect = lambda url, cache: url.replace("main", "sha1")

        entries = build_editorial_decl_entries("v1", MagicMock())
        self.assertEqual(entries, [
            {"category": "footnote-annotation", "refs": [
                {"target": "https://github.com/x/y/blob/sha1/g.md#L10-L50", "content_type": "markdown", "subtype": "machine"},
            ], "n": "Guide"},
        ])

    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_heading_anchor_with_html_type_produces_human_only(self, mock_get_guides, mock_resolve):
        mock_get_guides.return_value = [
            {"variant_ids": ["v1"], "category": "primary", "type": "html",
             "url": "https://example.com/docs/g.html#section", "label": "Guide"},
        ]
        mock_resolve.side_effect = lambda url, cache: url

        entries = build_editorial_decl_entries("v1", MagicMock())
        self.assertEqual(entries, [
            {"category": "primary", "refs": [
                {"target": "https://example.com/docs/g.html#section", "content_type": "html", "subtype": "human"},
            ], "n": "Guide"},
        ])

    @patch("pte_plugin_grobid.annotation_rules.translate_anchor_to_line_range")
    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_heading_anchor_with_markdown_type_produces_both_refs(
        self, mock_get_guides, mock_resolve, mock_translate
    ):
        mock_get_guides.return_value = [
            {"variant_ids": ["v1"], "category": "primary", "type": "markdown",
             "url": "https://github.com/x/y/blob/main/g.md#segmentation", "label": "Guide"},
        ]
        mock_resolve.side_effect = lambda url, cache: url.replace("main", "sha1")
        mock_translate.return_value = (10, 40)

        entries = build_editorial_decl_entries("v1", MagicMock())

        self.assertEqual(entries, [
            {"category": "primary", "refs": [
                {"target": "https://github.com/x/y/blob/sha1/g.md#segmentation", "content_type": "markdown", "subtype": "human"},
                {"target": "https://github.com/x/y/blob/sha1/g.md#L10-L40", "content_type": "markdown", "subtype": "machine"},
            ], "n": "Guide"},
        ])
        mock_translate.assert_called_once()
        call_args = mock_translate.call_args[0]
        self.assertEqual(call_args[0], "https://github.com/x/y/blob/sha1/g.md")
        self.assertEqual(call_args[1], "segmentation")

    @patch("pte_plugin_grobid.annotation_rules.translate_anchor_to_line_range")
    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_heading_anchor_with_markdown_type_falls_back_to_human_only_when_anchor_not_found(
        self, mock_get_guides, mock_resolve, mock_translate
    ):
        mock_get_guides.return_value = [
            {"variant_ids": ["v1"], "category": "primary", "type": "markdown",
             "url": "https://github.com/x/y/blob/main/g.md#segmentation", "label": "Guide"},
        ]
        mock_resolve.side_effect = lambda url, cache: url.replace("main", "sha1")
        mock_translate.return_value = None

        entries = build_editorial_decl_entries("v1", MagicMock())

        self.assertEqual(entries, [
            {"category": "primary", "refs": [
                {"target": "https://github.com/x/y/blob/sha1/g.md#segmentation", "content_type": "markdown", "subtype": "human"},
            ], "n": "Guide"},
        ])


    @patch("pte_plugin_grobid.annotation_rules.translate_anchor_to_line_range")
    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_machine_url_replaces_the_derived_machine_ref(
        self, mock_get_guides, mock_resolve, mock_translate
    ):
        mock_get_guides.return_value = [
            {"variant_ids": ["v1"], "category": "primary", "type": "markdown",
             "url": "https://github.com/x/y/blob/main/g.md#segmentation",
             "machine_url": "https://github.com/x/y/blob/main/g-llm.md#segmentation", "label": "Guide"},
        ]
        mock_resolve.side_effect = lambda url, cache: url.replace("main", "sha1")
        mock_translate.return_value = (5, 9)

        entries = build_editorial_decl_entries("v1", MagicMock())

        self.assertEqual(entries[0]["refs"], [
            {"target": "https://github.com/x/y/blob/sha1/g.md#segmentation", "content_type": "markdown", "subtype": "human"},
            {"target": "https://github.com/x/y/blob/sha1/g-llm.md#L5-L9", "content_type": "markdown", "subtype": "machine"},
        ])
        mock_translate.assert_called_once()
        self.assertEqual(mock_translate.call_args[0][:2], ("https://github.com/x/y/blob/sha1/g-llm.md", "segmentation"))

    @patch("pte_plugin_grobid.annotation_rules.translate_anchor_to_line_range")
    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_machine_url_line_range_is_used_as_is(self, mock_get_guides, mock_resolve, mock_translate):
        mock_get_guides.return_value = [
            {"variant_ids": ["v1"], "category": "primary", "type": "markdown",
             "url": "https://github.com/x/y/blob/main/g.md#segmentation",
             "machine_url": "https://github.com/x/y/blob/main/g-llm.md#L3-L7", "label": "Guide"},
        ]
        mock_resolve.side_effect = lambda url, cache: url.replace("main", "sha1")

        entries = build_editorial_decl_entries("v1", MagicMock())

        self.assertEqual(entries[0]["refs"][1]["target"], "https://github.com/x/y/blob/sha1/g-llm.md#L3-L7")
        mock_translate.assert_not_called()

    @patch("pte_plugin_grobid.annotation_rules.translate_anchor_to_line_range")
    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_falls_back_to_the_guide_section_when_machine_url_anchor_not_found(
        self, mock_get_guides, mock_resolve, mock_translate
    ):
        mock_get_guides.return_value = [
            {"variant_ids": ["v1"], "category": "primary", "type": "markdown",
             "url": "https://github.com/x/y/blob/main/g.md#segmentation",
             "machine_url": "https://github.com/x/y/blob/main/g-llm.md#segmentation", "label": "Guide"},
        ]
        mock_resolve.side_effect = lambda url, cache: url.replace("main", "sha1")
        mock_translate.side_effect = lambda url, anchor, cache: None if "g-llm" in url else (10, 40)

        entries = build_editorial_decl_entries("v1", MagicMock())

        self.assertEqual(entries[0]["refs"][1]["target"], "https://github.com/x/y/blob/sha1/g.md#L10-L40")


class TestBuildEditorialDeclEntriesSchemaFragment(unittest.TestCase):
    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_appends_schema_fragment_entry_for_a_fragment_variant(self, mock_get_guides, mock_resolve):
        mock_get_guides.return_value = []
        mock_resolve.side_effect = lambda url, cache: url.replace("main", "sha1")

        entries = build_editorial_decl_entries("grobid.training.segmentation", MagicMock())

        self.assertEqual(entries, [
            {
                "category": "schema-fragment",
                "n": "Segmentation schema source",
                "refs": [{
                    "target": "https://github.com/mpilhlt/fossil/blob/sha1/schema/grobid.training.segmentation.rng",
                    "content_type": "xml",
                    "subtype": "human",
                }],
            },
        ])

    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_no_schema_fragment_entry_for_a_variant_without_a_fossil_source_file(self, mock_get_guides, mock_resolve):
        mock_get_guides.return_value = []
        mock_resolve.side_effect = lambda url, cache: url

        entries = build_editorial_decl_entries("grobid.training.header", MagicMock())

        self.assertEqual(entries, [])

    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_schema_fragment_entry_appended_alongside_guide_entries(self, mock_get_guides, mock_resolve):
        mock_get_guides.return_value = [
            {"variant_ids": ["grobid.training.segmentation"], "category": "primary", "type": "markdown",
             "url": "https://github.com/x/y/blob/main/g.md", "label": "Document segmentation guidelines"},
        ]
        mock_resolve.side_effect = lambda url, cache: url.replace("main", "sha1")

        entries = build_editorial_decl_entries("grobid.training.segmentation", MagicMock())

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["category"], "primary")
        self.assertEqual(entries[1]["category"], "schema-fragment")


class TestBuildEditorialDeclEntriesLabel(unittest.TestCase):
    @patch("pte_plugin_grobid.annotation_rules.resolve_forge_permalink")
    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_guide_label_becomes_entry_n(self, mock_get_guides, mock_resolve):
        mock_get_guides.return_value = [
            {"variant_ids": ["v1"], "category": "primary", "type": "markdown",
             "url": "https://github.com/x/y/blob/main/g.md", "label": "Citation model guidelines"},
        ]
        mock_resolve.side_effect = lambda url, cache: url

        entries = build_editorial_decl_entries("v1", MagicMock())

        self.assertEqual(entries[0]["n"], "Citation model guidelines")


class TestGetCondensePrompt(unittest.TestCase):
    GUIDES = [
        {"variant_ids": ["v1"], "category": "primary", "type": "markdown",
         "url": "https://github.com/x/y/blob/main/g.md#model-a", "label": "A",
         "condense_prompt": "custom prompt"},
        {"variant_ids": ["v2"], "category": "primary", "type": "markdown",
         "url": "https://github.com/x/y/blob/main/g.md#model-b", "label": "B"},
    ]

    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_pinned_url_matches_branch_url(self, mock_get_guides):
        mock_get_guides.return_value = self.GUIDES
        pinned = "https://github.com/x/y/blob/0123456789abcdef0123456789abcdef01234567/g.md#model-a"
        self.assertEqual(GrobidRulesProvider().get_condense_prompt(pinned), "custom prompt")

    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_guide_without_prompt_returns_none(self, mock_get_guides):
        mock_get_guides.return_value = self.GUIDES
        url = "https://github.com/x/y/blob/main/g.md#model-b"
        self.assertIsNone(GrobidRulesProvider().get_condense_prompt(url))

    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_unknown_guide_returns_none(self, mock_get_guides):
        mock_get_guides.return_value = self.GUIDES
        url = "https://github.com/x/y/blob/main/other.md"
        self.assertIsNone(GrobidRulesProvider().get_condense_prompt(url))

    @patch("pte_plugin_grobid.annotation_rules.get_annotation_guides")
    def test_later_guide_with_same_url_and_prompt_wins(self, mock_get_guides):
        url = "https://github.com/x/y/blob/main/g.md#model-a"
        mock_get_guides.return_value = [
            {"variant_ids": ["v1"], "category": "primary", "type": "markdown", "url": url, "label": "A"},
            {"variant_ids": ["v2"], "category": "other", "type": "markdown", "url": url, "label": "A2",
             "condense_prompt": "second"},
        ]
        self.assertEqual(GrobidRulesProvider().get_condense_prompt(url), "second")


if __name__ == "__main__":
    unittest.main()
