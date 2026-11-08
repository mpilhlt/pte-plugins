"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/annotate.py
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from pte_plugin_auto_annotation import annotate
from pte_plugin_auto_annotation.api_client import ApiError

TARGETS = [
    {"doc_id": "doc1", "input_stable_id": "s1", "state": "ready"},
    {"doc_id": "doc2", "input_stable_id": "s2", "state": "annotated", "annotated_stable_id": "s2b"},
    {"doc_id": "doc3", "input_stable_id": "s3", "state": "gold"},
    {"doc_id": "doc4", "input_stable_id": None, "state": "no-version"},
]


class FakeSession:
    """Stands in for ApiSession."""

    def __init__(self, fail_on: str | None = None):
        self.fail_on = fail_on
        self.target_calls: list[tuple[str, str | None, list[str] | None]] = []
        self.payloads: list[dict[str, object]] = []
        self.saved: list[tuple[str, str]] = []
        self.released: list[str] = []

    def targets(self, variant, collection=None, doc_ids=None):
        self.target_calls.append((variant, collection, doc_ids))
        return TARGETS

    def annotate(self, payload):
        self.payloads.append(payload)
        stable_id = str(payload["stable_id"])
        if stable_id == self.fail_on:
            raise ApiError("model failed")
        doc_id = {"s1": "doc1", "s2": "doc2", "s3": "doc3"}.get(stable_id, "docX")
        return {"doc_id": doc_id, "xml": f"<TEI>{stable_id}</TEI>", "counts": {"agreed": 1}, "log": {"input_stable_id": stable_id}}

    def save_version(self, xml, source_stable_id):
        self.saved.append((xml, source_stable_id))
        return f"{source_stable_id}-new"

    def release_lock(self, stable_id):
        self.released.append(stable_id)


class TestAnnotateCli(unittest.TestCase):
    def run_main(self, *args: str, session: FakeSession | None = None) -> tuple[int, str, FakeSession]:
        session = session or FakeSession()
        err = io.StringIO()
        with mock.patch.object(annotate.ApiSession, "connect", return_value=session), redirect_stderr(err):
            code = annotate.main(["--user", "u", "--password", "p", "--env", "/nonexistent", "--model-a", "a", "--model-b", "b", "--arbiter", "c", *args])
        return code, err.getvalue(), session

    def test_timeout_reaches_the_session(self):
        for args, expected in (([], annotate.DEFAULT_ANNOTATE_TIMEOUT), (["--timeout", "123"], 123.0)):
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(annotate.ApiSession, "connect", return_value=FakeSession()) as connect, redirect_stderr(io.StringIO()):
                annotate.main(["--user", "u", "--password", "p", "--env", "/nonexistent", "--model-a", "a", "--model-b", "b", "--version", "s1", "--dry-run", "--out", tmp, *args])
            self.assertEqual(connect.call_args.kwargs["timeout"], expected)

    def test_collection_saves_ready_documents_and_skips_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, err, session = self.run_main("--collection", "coll", "--variant", "v.refs", "--out", tmp)
            self.assertEqual(code, 0)
            self.assertEqual(session.target_calls, [("v.refs", "coll", None)])
            self.assertEqual(session.saved, [("<TEI>s1</TEI>", "s1")])
            self.assertEqual(session.released, ["s1-new"])
            log = json.loads((Path(tmp) / "doc1.json").read_text(encoding="utf-8"))
            self.assertEqual(log, {"input_stable_id": "s1", "saved_stable_id": "s1-new"})
            self.assertFalse((Path(tmp) / "doc1.xml").exists())
        self.assertIn("skip doc2: already annotated (s2b)", err)
        self.assertIn("skip doc3: has a gold version", err)
        self.assertIn("skip doc4: no version of", err)
        payload = session.payloads[0]
        self.assertEqual((payload["model_a"], payload["model_b"], payload["arbiter"], payload["marks"]), ("a", "b", "c", True))

    def test_force_and_include_gold(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, session = self.run_main("--doc", "doc1", "--doc", "doc2", "--variant", "v.refs", "--force", "--include-gold", "--out", tmp)
        self.assertEqual(session.target_calls[0][2], ["doc1", "doc2"])
        self.assertEqual([p["stable_id"] for p in session.payloads], ["s1", "s2", "s3"])

    def test_dry_run_writes_xml_and_does_not_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, session = self.run_main("--version", "s9", "--dry-run", "--no-marks", "--out", tmp)
            self.assertEqual(code, 0)
            self.assertEqual((Path(tmp) / "docX.xml").read_text(encoding="utf-8"), "<TEI>s9</TEI>")
            self.assertIsNone(json.loads((Path(tmp) / "docX.json").read_text(encoding="utf-8"))["saved_stable_id"])
        self.assertEqual(session.target_calls, [])
        self.assertEqual(session.saved, [])
        self.assertFalse(session.payloads[0]["marks"])

    def test_failure_is_counted_and_run_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, err, session = self.run_main("--version", "s1", "--version", "s2", "--out", tmp, session=FakeSession(fail_on="s1"))
        self.assertEqual(code, 1)
        self.assertEqual(session.saved, [("<TEI>s2</TEI>", "s2")])
        self.assertIn("failed: model failed", err)
        self.assertIn("1 annotated, 1 failed", err)

    def test_split_on_failure_negative_disables_splitting(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, _, session = self.run_main("--version", "s1", "--split-on-failure", "-1", "--out", tmp)
        self.assertIsNone(session.payloads[0]["split_on_failure"])

    def test_argument_errors(self):
        for args in ([], ["--doc", "d", "--collection", "c", "--variant", "v"], ["--doc", "d", "--variant", "v", "--batch", "nonsense"],
                     ["--doc", "d"], ["--collection", "c"]):  # the last two: no --variant
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.run_main(*args)


if __name__ == "__main__":
    unittest.main()
