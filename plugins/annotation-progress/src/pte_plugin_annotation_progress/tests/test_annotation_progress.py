"""
Unit tests for Annotation Progress plugin.

@testCovers plugins/annotation-progress/src/pte_plugin_annotation_progress/plugin.py
@testCovers plugins/annotation-progress/src/pte_plugin_annotation_progress/routes.py
"""

import asyncio
import unittest
from unittest.mock import MagicMock

from pte_plugin_annotation_progress.plugin import AnnotationProgressPlugin
from pte_plugin_annotation_progress.routes import (
    _document_label,
    _extract_annotation_info,
    _format_history_html,
    _group_histories,
    _latest_annotation,
    _select_representative_annotation,
    _share_link_html,
)


class TestAnnotationProgressPlugin(unittest.TestCase):
    """Test cases for AnnotationProgressPlugin."""

    def setUp(self):
        """Set up test fixtures."""
        self.plugin = AnnotationProgressPlugin()

    def test_metadata(self):
        """Test plugin metadata structure."""
        metadata = self.plugin.metadata

        self.assertEqual(metadata["id"], "annotation-progress")
        self.assertEqual(metadata["name"], "Annotation Progress")
        self.assertEqual(metadata["category"], "collection")
        self.assertEqual(metadata["required_roles"], ["user"])
        self.assertEqual(len(metadata["endpoints"]), 1)

        endpoint = metadata["endpoints"][0]
        self.assertEqual(endpoint["name"], "show_progress")
        self.assertEqual(endpoint["label"], "Show Annotation Progress")
        self.assertIn("collection", endpoint["state_params"])
        self.assertIn("variant", endpoint["state_params"])
        self.assertTrue(endpoint["url_openable"])

    def test_get_endpoints(self):
        """Test endpoint registration."""
        endpoints = self.plugin.get_endpoints()

        self.assertIn("show_progress", endpoints)
        self.assertTrue(callable(endpoints["show_progress"]))

    def test_show_progress_no_collection(self):
        """Test show_progress with no collection parameter."""
        context = MagicMock()
        params = {}

        result = asyncio.run(self.plugin.show_progress(context, params))

        self.assertIn("error", result)

    def test_show_progress_with_collection(self):
        """Test show_progress with collection parameter."""
        context = MagicMock()
        params = {"collection": "test-collection", "variant": "test-variant"}

        result = asyncio.run(self.plugin.show_progress(context, params))

        self.assertIn("outputUrl", result)
        self.assertIn("test-collection", result["outputUrl"])
        self.assertIn("test-variant", result["outputUrl"])
        self.assertEqual(result["collection"], "test-collection")
        self.assertEqual(result["variant"], "test-variant")

    def test_extract_annotation_info(self):
        """Test annotation info extraction from TEI XML."""
        from datetime import datetime

        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt>
        <title level="a">Test Document</title>
      </titleStmt>
      <editionStmt>
        <edition>
          <title>Test Annotation</title>
        </edition>
      </editionStmt>
      <publicationStmt>
        <publisher>Test</publisher>
      </publicationStmt>
      <sourceDesc>
        <bibl>Test</bibl>
      </sourceDesc>
    </fileDesc>
    <revisionDesc>
      <change when="2024-01-01T10:00:00Z" status="created">First version</change>
      <change when="2024-01-02T10:00:00Z" status="updated">Second version</change>
      <change when="2024-01-03T10:00:00Z" status="reviewed">Third version</change>
    </revisionDesc>
  </teiHeader>
  <text>
    <body>
      <p>Test content</p>
    </body>
  </text>
</TEI>"""

        file_metadata = MagicMock()
        file_metadata.stable_id = "test-stable-id"

        result = _extract_annotation_info(xml_content, file_metadata)

        self.assertIsNotNone(result)
        self.assertEqual(result["annotation_label"], "Test Annotation")
        self.assertEqual(result["revision_count"], 3)
        self.assertEqual(result["stable_id"], "test-stable-id")
        self.assertEqual(result["last_change_status"], "reviewed")
        self.assertIsNotNone(result["last_change_timestamp"])
        # Verify timestamp is parsed correctly (timezone stripped)
        expected_timestamp = datetime(2024, 1, 3, 10, 0, 0)
        self.assertEqual(result["last_change_timestamp"], expected_timestamp)
        # Verify change signatures are extracted
        self.assertIn("change_signatures", result)
        self.assertEqual(len(result["change_signatures"]), 3)

    def test_extract_annotation_info_no_edition_title(self):
        """Test annotation info extraction with no edition title."""
        xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt>
        <title level="a">Test Document Title</title>
      </titleStmt>
      <publicationStmt>
        <publisher>Test</publisher>
      </publicationStmt>
      <sourceDesc>
        <bibl>Test</bibl>
      </sourceDesc>
    </fileDesc>
    <revisionDesc>
      <change when="2024-01-01T10:00:00" status="created">First version</change>
    </revisionDesc>
  </teiHeader>
  <text>
    <body>
      <p>Test content</p>
    </body>
  </text>
</TEI>"""

        file_metadata = MagicMock()
        file_metadata.stable_id = "test-stable-id"

        result = _extract_annotation_info(xml_content, file_metadata)

        self.assertIsNotNone(result)
        self.assertEqual(result["annotation_label"], "Untitled")
        self.assertEqual(result["revision_count"], 1)


class TestSelectRepresentativeAnnotation(unittest.TestCase):
    """Test cases for _select_representative_annotation."""

    def setUp(self):
        self.lifecycle_order = ["draft", "review", "final"]

    def test_picks_highest_status_over_newest_timestamp(self):
        """A newer regression must not shadow an older, further-along version."""
        from datetime import datetime

        older_but_further = {
            "last_change_status": "final",
            "last_change_timestamp": datetime(2024, 1, 1),
        }
        newer_but_regressed = {
            "last_change_status": "draft",
            "last_change_timestamp": datetime(2024, 1, 5),
        }

        result = _select_representative_annotation(
            [newer_but_regressed, older_but_further], self.lifecycle_order
        )

        self.assertIs(result, older_but_further)

    def test_ties_broken_by_newest_timestamp(self):
        """Among versions with the same status, prefer the most recent one."""
        from datetime import datetime

        older = {
            "last_change_status": "review",
            "last_change_timestamp": datetime(2024, 1, 1),
        }
        newer = {
            "last_change_status": "review",
            "last_change_timestamp": datetime(2024, 1, 5),
        }

        result = _select_representative_annotation([older, newer], self.lifecycle_order)

        self.assertIs(result, newer)

    def test_falls_back_to_newest_when_no_status_recognized(self):
        """When no annotation's status is in lifecycle_order, fall back to
        the most recently changed one so the row still shows something."""
        from datetime import datetime

        older = {
            "last_change_status": "archived",
            "last_change_timestamp": datetime(2024, 1, 1),
        }
        newer = {
            "last_change_status": "",
            "last_change_timestamp": datetime(2024, 1, 5),
        }

        result = _select_representative_annotation([older, newer], self.lifecycle_order)

        self.assertIs(result, newer)

    def test_empty_list_returns_none(self):
        self.assertIsNone(_select_representative_annotation([], self.lifecycle_order))


def _ann(stable_id, version=1, parent=None, gold=False, ts=None, variant=None):
    """Annotation info dict as produced by `_extract_annotation_info`."""
    return {
        "annotation_label": f"Label {stable_id}",
        "stable_id": stable_id,
        "version": None if gold else version,
        "parent_stable_id": parent,
        "is_gold_standard": gold,
        "last_change_timestamp": ts,
        "variant": variant,
    }


def _ids(anns):
    return [a["stable_id"] for a in anns]


class TestGroupHistories(unittest.TestCase):
    """Test cases for grouping a document's versions into version histories."""

    def test_unrelated_versions_form_separate_histories(self):
        histories = _group_histories([_ann("a", 1), _ann("b", 2)], set())
        self.assertEqual([(_ids(h["leaves"]), _ids(h["ancestors"])) for h in histories], [(["a"], []), (["b"], [])])

    def test_chain_with_siblings(self):
        # a -> b -> (c, d); a and b are frozen
        anns = [_ann("a", 1), _ann("b", 2, "a"), _ann("c", 3, "b"), _ann("d", 4, "b")]
        histories = _group_histories(anns, {"a", "b"})
        self.assertEqual(len(histories), 1)
        self.assertEqual(_ids(histories[0]["leaves"]), ["c", "d"])
        self.assertEqual(_ids(histories[0]["ancestors"]), ["b", "a"])

    def test_gold_is_own_history_and_ends_chain(self):
        # gold -> a -> b
        anns = [_ann("g", gold=True), _ann("a", 1, "g"), _ann("b", 2, "a")]
        histories = _group_histories(anns, {"g", "a"})
        self.assertEqual([(_ids(h["leaves"]), _ids(h["ancestors"])) for h in histories], [(["g"], []), (["b"], ["a"])])

    def test_orphaned_frozen_versions_are_kept(self):
        # x is frozen, but its child is not in the list (e.g. other variant filtered out)
        histories = _group_histories([_ann("x", 1)], {"x"})
        self.assertEqual([(_ids(h["leaves"]), _ids(h["ancestors"])) for h in histories], [([], ["x"])])

    def test_empty(self):
        self.assertEqual(_group_histories([], set()), [])


class TestLatestAnnotation(unittest.TestCase):
    def test_picks_newest_timestamp(self):
        from datetime import datetime

        old, new = _ann("a", ts=datetime(2024, 1, 1)), _ann("b", ts=datetime(2024, 2, 1))
        self.assertIs(_latest_annotation([old, new, _ann("c")]), new)

    def test_falls_back_to_first(self):
        first = _ann("a")
        self.assertIs(_latest_annotation([first, _ann("b")]), first)


class TestFormatHistoryHtml(unittest.TestCase):
    """Test cases for version history HTML formatting."""

    def test_without_ancestors_has_no_toggle(self):
        html = _format_history_html({"leaves": [_ann("a")], "ancestors": []}, False)
        self.assertIn("sandbox.openDocument('a')", html)
        self.assertNotIn("see history", html)

    def test_with_ancestors_has_collapsed_lineage(self):
        html = _format_history_html({"leaves": [_ann("c"), _ann("d")], "ancestors": [_ann("b"), _ann("a")]}, False)
        self.assertEqual(html.count("(see history)"), 1)
        # the toggle follows the last current version
        self.assertLess(html.index("Label d"), html.index("(see history)"))
        self.assertIn('<div class="lineage" hidden>', html)
        self.assertIn("lineage-arrow", html)
        self.assertEqual(html.count('class="lineage-ancestor"'), 2)
        self.assertIn("sandbox.openDocument('a')", html)

    def test_gold_and_variant(self):
        html = _format_history_html({"leaves": [_ann("g", gold=True, variant="grobid.v1")], "ancestors": []}, True)
        self.assertIn("🥇 Label g", html)
        self.assertIn("(v1)", html)
        self.assertIn('title="grobid.v1"', html)
        html = _format_history_html({"leaves": [_ann("g", gold=True, variant="grobid.v1")], "ancestors": []}, False)
        self.assertNotIn("(v1)", html)

    def test_escapes_labels(self):
        ann = _ann("a")
        ann["annotation_label"] = "<b>x</b>"
        html = _format_history_html({"leaves": [ann], "ancestors": []}, False)
        self.assertNotIn("<b>x</b>", html)


class TestShareLinkHtml(unittest.TestCase):
    def test_link_hash_opens_view_for_collection_and_variant(self):
        from urllib.parse import parse_qs

        html = _share_link_html("batch-1", "grobid.v1")
        start = html.index('"#') + 2
        hash_params = parse_qs(html[start:html.index('"', start)])
        self.assertEqual(hash_params, {"plugin": ["annotation-progress/show_progress"], "collection": ["batch-1"], "variant": ["grobid.v1"]})
        self.assertIn('id="shareLinkBtn"', html)

    def test_collection_cannot_end_script(self):
        self.assertNotIn("</script><x>", _share_link_html("</script><x>", None))


class TestDocumentLabel(unittest.TestCase):
    def test_prefers_pdf_label_then_title_then_doc_id(self):
        pdf = MagicMock(label="My Label", doc_metadata={"title": "Title"})
        self.assertEqual(_document_label(pdf, "doc"), "My Label")
        pdf = MagicMock(label="Untitled", doc_metadata={"title": "Title"})
        self.assertEqual(_document_label(pdf, "doc"), "Title")
        pdf = MagicMock(label=None, doc_metadata={})
        self.assertEqual(_document_label(pdf, "doc"), "doc")
        self.assertEqual(_document_label(None, "doc"), "doc")


if __name__ == "__main__":
    unittest.main()
