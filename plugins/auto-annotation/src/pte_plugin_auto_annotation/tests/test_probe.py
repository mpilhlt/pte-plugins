"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/probe.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/api_client.py
"""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from pte_plugin_auto_annotation import probe
from pte_plugin_auto_annotation.api_client import ApiSession, ModelsBusyError
from pte_plugin_auto_annotation.cli_common import load_env
from pte_plugin_auto_annotation.evaluation import EvalSource
from pte_plugin_auto_annotation.units import document_units

from .fixtures import GOLD_UNITS, GOLD_XML, INPUT_XML, prompt_units


class FakeSession:
    """Stands in for ApiSession: one gold document, a model that answers with the gold units."""

    def __init__(self):
        self.mapping = {u.xml: g for u, g in zip(document_units(INPUT_XML, ".//listBibl/bibl"), GOLD_UNITS)}
        self.completions: list[tuple[str, str]] = []
        self.sources: list[tuple[str | None, str | None, str | None, str | None, bool]] = []

    def eval_source(self, doc_id=None, variant=None, reference_stable_id=None, input_stable_id=None, with_rules=True):
        self.sources.append((doc_id, variant, reference_stable_id, input_stable_id, with_rules))
        return EvalSource(reference_stable_id or "gold1", "v1", "grobid.training.references", ".//listBibl/bibl", GOLD_XML, INPUT_XML,
                          [("references", "RULES")] if with_rules else [], doc_id=doc_id or "doc1",
                          reference_is_gold=reference_stable_id is None)

    def resolve_model(self, spec):
        return "endpoint-local", spec.split("/")[-1]

    busy = False

    def ensure_models_not_busy(self, specs):
        if self.busy:
            raise ModelsBusyError("these models are busy: kisski/m")

    def complete(self, provider_id, model_id, system_prompt, user_prompt, temperature):
        self.completions.append((model_id, user_prompt))
        return "\n\n".join(f"### Unit {uid}\n{self.mapping[xml]}" for uid, xml in prompt_units(user_prompt).items())


class TestBusyModels(unittest.TestCase):
    PROVIDERS = [
        {"id": "kisski", "models": [
            {"id": "free", "status": {"availability": "available", "detail": "demand: 0"}},
            {"id": "busy", "status": {"availability": "busy", "detail": "demand: 5"}},
            {"id": "full", "status": {"availability": "very_busy", "detail": "demand: 9"}},
        ]},
        {"id": "anthropic", "models": [{"id": "sonnet", "status": None}]},
        {"id": "other", "models": [], "error": "Could not list models"},
    ]

    def setUp(self):
        self.session = ApiSession("http://server", "sid")
        patcher = mock.patch("pte_plugin_auto_annotation.api_client.requests.get")
        get = patcher.start()
        self.addCleanup(patcher.stop)
        get.return_value.ok = True
        get.return_value.json.return_value = self.PROVIDERS

    def test_reports_busy_and_very_busy_with_their_detail(self):
        busy = self.session.busy_models([("kisski", "free"), ("kisski", "busy"), ("kisski", "full")])
        self.assertEqual(busy, ["kisski/busy (busy, demand: 5)", "kisski/full (very_busy, demand: 9)"])

    def test_no_status_or_unknown_model_counts_as_free(self):
        self.assertEqual(self.session.busy_models([("anthropic", "sonnet"), ("other", "x"), ("kisski", "unknown")]), [])

    def test_model_used_twice_is_reported_once(self):
        self.assertEqual(len(self.session.busy_models([("kisski", "busy"), ("kisski", "busy")])), 1)

    def test_ensure_raises_and_names_the_option(self):
        with mock.patch.object(self.session, "resolve_model", return_value=("kisski", "busy")):
            with self.assertRaisesRegex(ModelsBusyError, "kisski/busy.*--force-busy"):
                self.session.ensure_models_not_busy(["kisski/busy"])

    def test_ensure_passes_for_free_models(self):
        with mock.patch.object(self.session, "resolve_model", return_value=("kisski", "free")):
            self.session.ensure_models_not_busy(["kisski/free"])


class TestProbeCli(unittest.TestCase):
    def run_main(self, *args: str) -> tuple[int, str, FakeSession]:
        session = FakeSession()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(probe.ApiSession, "connect", return_value=session), redirect_stdout(out), redirect_stderr(err):
            code = probe.main(["--user", "u", "--password", "p", "--env", "/nonexistent", *args])
        return code, out.getvalue(), session

    def test_runs_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, session = self.run_main(
                "--model", "local/qwen", "--model", "local/llama", "--doc", "doc1", "--variant", "grobid.training.references",
                "--batches", "units:1,units:4", "--modes", "all,changed", "--out", tmp,
            )
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "report.md").is_file())
        self.assertIn("## endpoint-local/qwen on doc1", out)
        self.assertIn("## endpoint-local/llama on doc1", out)
        self.assertIn("Reference version `gold1` (gold)", out)
        # 2 models x (4 + 1 calls) x 2 modes
        self.assertEqual(len(session.completions), 20)

    def test_busy_model_stops_the_run(self):
        with mock.patch.object(FakeSession, "busy", True), tempfile.TemporaryDirectory() as tmp:
            code, _, session = self.run_main("--model", "m", "--doc", "doc1", "--variant", "v", "--batches", "units:4", "--out", tmp)
        self.assertEqual(code, 1)
        self.assertEqual(session.completions, [])

    def test_force_busy_starts_anyway(self):
        with mock.patch.object(FakeSession, "busy", True), tempfile.TemporaryDirectory() as tmp:
            code, _, session = self.run_main("--model", "m", "--doc", "doc1", "--variant", "v", "--batches", "units:4", "--force-busy", "--out", tmp)
        self.assertEqual(code, 0)
        self.assertGreater(len(session.completions), 0)

    def test_rules_file_pins_the_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.md"
            rules.write_text("PINNED RULES")
            code, out, session = self.run_main("--model", "m", "--doc", "doc1", "--variant", "v", "--batches", "units:4", "--rules-file", str(rules), "--out", tmp)
        self.assertEqual(code, 0)
        self.assertEqual(session.sources, [("doc1", "v", None, None, False)])
        self.assertIn("PINNED RULES", session.completions[0][1])
        self.assertIn("## endpoint-local/m on doc1", out)  # the pinned source keeps the document id

    def test_reference_replaces_the_gold_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, session = self.run_main("--model", "m", "--reference", "v7", "--variant", "x", "--input", "v1", "--batches", "units:4", "--out", tmp)
        self.assertEqual(code, 0)
        self.assertEqual(session.sources, [(None, "x", "v7", "v1", True)])
        self.assertIn("Reference version `v7` (not the gold version)", out)

    def test_results_are_written_after_every_run_and_kept_on_interruption(self):
        session = FakeSession()
        calls = {"n": 0}
        complete = session.complete

        def interrupting(*args):
            calls["n"] += 1
            if calls["n"] > 3:  # doc1: 1 call per run (units:4) x 2 runs; doc2 is interrupted in its second run
                raise KeyboardInterrupt
            return complete(*args)

        session.complete = interrupting  # type: ignore[method-assign]
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(probe.ApiSession, "connect", return_value=session), redirect_stdout(out), redirect_stderr(err):
            code = probe.main(["--user", "u", "--password", "p", "--env", "/nonexistent", "--model", "m", "--doc", "doc1", "--doc", "doc2", "--variant", "grobid.training.references",
                               "--batches", "units:4", "--repeats", "2", "--out", tmp])
            report = (Path(tmp) / "report.md").read_text()
            self.assertFalse(any(p.name.endswith(".tmp") for p in Path(tmp).iterdir()))
        self.assertEqual(code, 130)
        self.assertIn("Interrupted", err.getvalue())
        self.assertIn("## endpoint-local/m on doc1", report)
        self.assertIn("## endpoint-local/m on doc2\n\n**Incomplete:** 1 of 2 runs finished.", report)
        self.assertEqual(report.count("**Incomplete:**"), 1)  # doc1 is complete

    def test_argument_errors(self):
        for args in (["--model", "m"], ["--model", "m", "--doc", "a", "--doc", "b", "--input", "x"],
                     ["--model", "m", "--doc", "a", "--doc", "b", "--reference", "x"],
                     ["--model", "m", "--doc", "a"], ["--model", "m", "--doc", "a", "--variant", "v", "--modes", "diff"], ["--model", "m", "--doc", "a", "--variant", "v", "--batches", "8"]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.run_main(*args)

    def test_load_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# comment\nexport API_USER=alice\nAPI_PASSWORD='s3cret'\n")
            self.assertEqual(load_env(env), {"API_USER": "alice", "API_PASSWORD": "s3cret"})


if __name__ == "__main__":
    unittest.main()
