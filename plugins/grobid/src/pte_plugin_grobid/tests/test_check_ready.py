"""
Tests for GrobidTrainingExtractor.check_ready(), which the editor calls before text recognition
so that a long OCR run is not wasted when the GROBID server is down.
"""

import asyncio
import unittest
from unittest.mock import patch

from pte_plugin_grobid import extractor as extractor_module
from pte_plugin_grobid.extractor import GrobidTrainingExtractor

URL = "http://grobid.example:8070"


class TestCheckReady(unittest.TestCase):
    def check(self) -> str | None:
        return asyncio.run(GrobidTrainingExtractor().check_ready({"variant_id": "grobid.training.segmentation"}))

    def test_ready(self):
        with patch.object(extractor_module, "get_grobid_server_url", return_value=URL), \
                patch.object(extractor_module, "check_space_running", return_value=None), \
                patch.object(GrobidTrainingExtractor, "_check_grobid_health", return_value=None) as health:
            self.assertIsNone(self.check())
        health.assert_called_once_with(URL)

    def test_no_server_configured(self):
        with patch.object(extractor_module, "get_grobid_server_url", return_value=None):
            message = self.check()
        self.assertIsNotNone(message)
        self.assertIn("No GROBID server is configured", message or "")

    def test_space_not_running(self):
        with patch.object(extractor_module, "get_grobid_server_url", return_value=URL), \
                patch.object(extractor_module, "check_space_running", return_value="The Hugging Face Space is sleeping"), \
                patch.object(GrobidTrainingExtractor, "_check_grobid_health") as health:
            self.assertEqual(self.check(), "The Hugging Face Space is sleeping")
        health.assert_not_called()

    def test_server_not_reachable(self):
        error = RuntimeError("Could not reach GROBID server: Connection refused")
        with patch.object(extractor_module, "get_grobid_server_url", return_value=URL), \
                patch.object(extractor_module, "check_space_running", return_value=None), \
                patch.object(GrobidTrainingExtractor, "_check_grobid_health", side_effect=error):
            self.assertEqual(self.check(), "Could not reach GROBID server: Connection refused")


if __name__ == "__main__":
    unittest.main()
