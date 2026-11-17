"""
Unit tests for how the training data download handles documents GROBID cannot process (#504).

Run manually:
    uv run --project "$PTE_ROOT" python -m pytest plugins/grobid/src/pte_plugin_grobid/tests/test_training_download_failures.py -v

@testCovers plugins/grobid/src/pte_plugin_grobid/handlers/training.py
@testCovers plugins/grobid/src/pte_plugin_grobid/routes.py
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from pte_plugin_grobid.handlers.training import TrainingHandler
from pte_plugin_grobid.routes import _failed_documents_summary, _skipped_documents_report


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Server Error: Internal Server Error for url: https://grobid/api/createTraining", response=response)


class TestFetchTrainingPackage(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "doc.pdf"
        self.pdf.write_bytes(b"%PDF-1.4")
        settings = mock.Mock(tmp_dir=Path(self.tmp.name))
        patcher = mock.patch("pte_plugin_grobid.handlers.training.get_settings", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_http_500_is_not_retried(self) -> None:
        session = mock.Mock()
        session.post.return_value.raise_for_status.side_effect = _http_error(500)
        with mock.patch("pte_plugin_grobid.handlers.training.get_retry_session", return_value=session) as get_session, \
             self.assertLogs("pte_plugin_grobid.handlers.training", level="ERROR"):
            with self.assertRaises(RuntimeError) as cm:
                TrainingHandler()._fetch_training_package(str(self.pdf), "https://grobid", "default")
        self.assertNotIn(500, get_session.call_args.kwargs["status_forcelist"])
        self.assertIn(503, get_session.call_args.kwargs["status_forcelist"])
        self.assertEqual(str(cm.exception), "GROBID request to grobid failed: GROBID could not process this PDF (HTTP 500)")


class TestFailureReports(unittest.TestCase):
    def test_summary_names_documents(self) -> None:
        self.assertEqual(
            _failed_documents_summary([("2403.01028", "x")]),
            "GROBID could not create training data for 1 document (2403.01028).",
        )

    def test_summary_truncates_long_lists(self) -> None:
        failures = [(f"d{i}", "x") for i in range(7)]
        self.assertEqual(
            _failed_documents_summary(failures, limit=3),
            "GROBID could not create training data for 7 documents (d0, d1, d2 and 4 more).",
        )

    def test_report_lists_reasons(self) -> None:
        report = _skipped_documents_report([("d1", "HTTP 500"), ("d2", "timeout")])
        self.assertIn("d1\tHTTP 500\n", report)
        self.assertTrue(report.endswith("d2\ttimeout\n"))


if __name__ == "__main__":
    unittest.main()
