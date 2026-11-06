"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/versions.py
"""

import unittest

from pte_plugin_auto_annotation.versions import is_extraction

from .fixtures import CORRECTED, EXTRACTION, INPUT_UNITS, RENAMED, tei

RULES_UPDATED = '<change when="2026-01-04" who="#admin"><desc>Updated document rules</desc></change>'


class TestIsExtraction(unittest.TestCase):
    def test_extraction_with_metadata_changes_only(self):
        self.assertTrue(is_extraction(tei(INPUT_UNITS)))
        self.assertTrue(is_extraction(tei(INPUT_UNITS, changes=EXTRACTION + RENAMED + RULES_UPDATED)))

    def test_annotated_copies_and_other_histories(self):
        # a copy of the extraction that was corrected, then renamed
        self.assertFalse(is_extraction(tei(INPUT_UNITS, changes=EXTRACTION + CORRECTED + RENAMED)))
        self.assertFalse(is_extraction(tei(INPUT_UNITS, changes=CORRECTED)))  # does not start with the extraction
        self.assertFalse(is_extraction(tei(INPUT_UNITS, changes="")))  # no history
        self.assertFalse(is_extraction("<TEI"))  # not well-formed


if __name__ == "__main__":
    unittest.main()
