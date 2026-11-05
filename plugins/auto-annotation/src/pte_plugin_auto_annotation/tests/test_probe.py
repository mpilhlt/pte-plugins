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
from pte_plugin_auto_annotation.evaluation import EvalSource
from pte_plugin_auto_annotation.units import document_units

from .fixtures import GOLD_UNITS, GOLD_XML, INPUT_XML, prompt_units


class FakeSession:
    """Stands in for ApiSession: one gold document, a model that answers with the gold units."""

    def __init__(self):
        self.mapping = {u.xml: g for u, g in zip(document_units(INPUT_XML, ".//listBibl/bibl"), GOLD_UNITS)}
        self.completions: list[tuple[str, str]] = []
        self.sources: list[tuple[str, str | None, bool]] = []

    def eval_source(self, gold, input_id=None, with_rules=True):
        self.sources.append((gold, input_id, with_rules))
        return EvalSource(gold, "v1", "grobid.training.references", ".//listBibl/bibl", GOLD_XML, INPUT_XML,
                          [("references", "RULES")] if with_rules else [])

    def resolve_model(self, spec):
        return "endpoint-local", spec.split("/")[-1]

    def complete(self, provider_id, model_id, system_prompt, user_prompt, temperature):
        self.completions.append((model_id, user_prompt))
        return "\n\n".join(f"### Unit {uid}\n{self.mapping[xml]}" for uid, xml in prompt_units(user_prompt).items())


class TestProbeCli(unittest.TestCase):
    def run_main(self, *args: str) -> tuple[int, str, FakeSession]:
        session = FakeSession()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(probe.ApiSession, "login", return_value=session), redirect_stdout(out), redirect_stderr(err):
            code = probe.main(["--user", "u", "--password", "p", "--env", "/nonexistent", *args])
        return code, out.getvalue(), session

    def test_runs_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, session = self.run_main(
                "--model", "local/qwen", "--model", "local/llama", "--gold", "gold1",
                "--batches", "units:1,units:4", "--modes", "all,changed", "--out", tmp,
            )
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "report.md").is_file())
        self.assertIn("## endpoint-local/qwen on gold1", out)
        self.assertIn("## endpoint-local/llama on gold1", out)
        # 2 models x (4 + 1 calls) x 2 modes
        self.assertEqual(len(session.completions), 20)

    def test_rules_file_pins_the_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "rules.md"
            rules.write_text("PINNED RULES")
            code, _, session = self.run_main("--model", "m", "--gold", "gold1", "--batches", "units:4", "--rules-file", str(rules), "--out", tmp)
        self.assertEqual(code, 0)
        self.assertEqual(session.sources, [("gold1", None, False)])
        self.assertIn("PINNED RULES", session.completions[0][1])

    def test_argument_errors(self):
        for args in (["--model", "m", "--gold", "a", "--gold", "b", "--input", "x"], ["--model", "m", "--gold", "a", "--modes", "diff"],
                     ["--model", "m", "--gold", "a", "--batches", "8"]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.run_main(*args)

    def test_load_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("# comment\nexport API_USER=alice\nAPI_PASSWORD='s3cret'\n")
            self.assertEqual(probe._load_env(env), {"API_USER": "alice", "API_PASSWORD": "s3cret"})


if __name__ == "__main__":
    unittest.main()
