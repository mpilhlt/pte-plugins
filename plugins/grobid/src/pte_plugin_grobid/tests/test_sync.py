"""
Unit tests for encodingDesc label patching in the grobid plugin's sync module.

Run manually:
    uv run --project "$PTE_ROOT" python -m pytest plugins/grobid/src/pte_plugin_grobid/tests/test_sync.py -v

@testCovers plugins/grobid/src/pte_plugin_grobid/sync.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent))

from pte_plugin_grobid.sync import parse_encoding_labels, set_pdf_hash_label, set_revision_label

SAMPLE_TEI = """<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <encodingDesc>
      <appInfo>
        <application version="1.0" ident="pdf-tei-editor" type="editor">
          <label>PDF-TEI Editor</label>
        </application>
        <application version="0.8.0" ident="GROBID" type="extractor">
          <label type="model">segmentation</label>
          <label type="flavor">default</label>
          <label type="variant-id">grobid.training.segmentation</label>
          <label type="revision">old-rev-123</label>
        </application>
      </appInfo>
    </encodingDesc>
  </teiHeader>
  <text>
    <body>
      <p>Some content with revision-like text old-rev-123 in it.</p>
    </body>
  </text>
</TEI>
"""


class TestSetRevisionLabel(unittest.TestCase):

    def test_replaces_revision_label_text_only(self):
        result = set_revision_label(SAMPLE_TEI, "old-rev-123", "new-rev-456")

        self.assertIn(
            '<label type="revision">new-rev-456</label>',
            result,
        )
        self.assertNotIn(
            '<label type="revision">old-rev-123</label>',
            result,
        )

    def test_does_not_touch_unrelated_occurrences_of_old_value(self):
        result = set_revision_label(SAMPLE_TEI, "old-rev-123", "new-rev-456")

        # The coincidental occurrence of the old revision string inside the
        # body text must survive untouched.
        self.assertIn("Some content with revision-like text old-rev-123 in it.", result)

    def test_leaves_rest_of_document_byte_identical(self):
        result = set_revision_label(SAMPLE_TEI, "old-rev-123", "new-rev-456")

        expected = SAMPLE_TEI.replace(
            '<label type="revision">old-rev-123</label>',
            '<label type="revision">new-rev-456</label>',
            1,
        )
        self.assertEqual(result, expected)

    def test_returns_unchanged_content_when_label_not_found(self):
        content_without_label = "<TEI><teiHeader/><text/></TEI>"
        result = set_revision_label(content_without_label, "old-rev-123", "new-rev-456")
        self.assertEqual(result, content_without_label)

    def test_returns_unchanged_content_when_old_value_does_not_match(self):
        result = set_revision_label(SAMPLE_TEI, "does-not-match", "new-rev-456")
        self.assertEqual(result, SAMPLE_TEI)


class TestSetPdfHashLabel(unittest.TestCase):

    def test_inserts_the_label_after_the_revision_label(self):
        result = set_pdf_hash_label(SAMPLE_TEI, "abc")
        self.assertIn('<label type="revision">old-rev-123</label>\n          <label type="pdf-hash">abc</label>', result)
        self.assertEqual(parse_encoding_labels(result)["pdf-hash"], "abc")
        self.assertEqual(result.replace('\n          <label type="pdf-hash">abc</label>', ""), SAMPLE_TEI)

    def test_replaces_an_existing_label(self):
        once = set_pdf_hash_label(SAMPLE_TEI, "abc")
        self.assertEqual(set_pdf_hash_label(once, "def"), once.replace(">abc<", ">def<"))

    def test_without_revision_label_nothing_changes(self):
        xml = SAMPLE_TEI.replace('<label type="revision">old-rev-123</label>', "")
        self.assertEqual(set_pdf_hash_label(xml, "abc"), xml)


if __name__ == "__main__":
    unittest.main()
