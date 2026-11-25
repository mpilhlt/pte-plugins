"""
Unit tests for the summary of TEI files whose registry language was not declared.

@testCovers plugins/update-metadata/src/pte_plugin_update_metadata/routes.py
"""

import unittest

from pte_plugin_update_metadata.routes import _language_kept_html


class TestLanguageKeptHtml(unittest.TestCase):

    def test_no_entries(self):
        self.assertEqual(_language_kept_html([]), "")

    def test_lists_entries(self):
        html = _language_kept_html([
            {"stable_id": "abc123", "registry": "en", "detected": "de"},
            {"stable_id": "<x>", "registry": "en", "detected": "fr"},
        ])
        self.assertIn("Registry language not declared:</strong> 2", html)
        self.assertIn("abc123: registry <code>en</code>, text <code>de</code>", html)
        self.assertIn("&lt;x&gt;", html)


if __name__ == "__main__":
    unittest.main()
