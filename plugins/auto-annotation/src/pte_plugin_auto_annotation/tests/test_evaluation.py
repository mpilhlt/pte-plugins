"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/evaluation.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/report.py
"""

import asyncio
import csv
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.evaluation import EvalSource, evaluate, sweep
from pte_plugin_auto_annotation.report import markdown_report, write_results
from pte_plugin_auto_annotation.units import document_units

from .fixtures import GOLD_UNITS, GOLD_XML, INPUT_XML, EchoClient, OracleClient

SOURCE = EvalSource(
    reference_stable_id="gold1", input_stable_id="in1", variant="grobid.training.references",
    unit_path=".//listBibl/bibl", gold_xml=GOLD_XML, input_xml=INPUT_XML, rules=[("references", "rules")],
)


def _mapping() -> dict[str, str]:
    return {u.xml: g for u, g in zip(document_units(INPUT_XML, ".//listBibl/bibl"), GOLD_UNITS)}


class TestEvaluate(unittest.TestCase):
    def test_oracle_scores_perfectly_and_baseline_has_no_edits(self):
        configs = sweep([BatchSpec(max_units=1), BatchSpec(max_units=4)], ["all"], 2)
        result = asyncio.run(evaluate(OracleClient(_mapping()), "mock/oracle", SOURCE, configs))
        self.assertEqual(len(result.units), 4 * 4)
        self.assertEqual([s.batching for s in result.summaries], ["units:1", "units:4"])
        for summary in result.summaries:
            self.assertEqual((summary.edit_precision, summary.edit_recall, summary.exact_rate, summary.failure_rate), (1.0, 1.0, 1.0, 0.0))
            self.assertEqual(summary.repeats, 2)
        self.assertEqual(result.summaries[0].calls, 8)
        self.assertEqual(result.summaries[1].calls, 2)
        assert result.baseline is not None
        self.assertIsNone(result.baseline.edit_precision)
        self.assertEqual(result.baseline.edit_recall, 0.0)

    def test_degradation_with_batch_size_shows_in_failures_and_quarters(self):
        # a model that answers only the first two units of every batch
        configs = sweep([BatchSpec(max_units=2), BatchSpec(max_units=4)], ["all"], 1)
        result = asyncio.run(evaluate(OracleClient(_mapping(), drop_after=2), "mock/short", SOURCE, configs))
        small, large = result.summaries
        self.assertEqual(small.failure_rate, 0.0)
        self.assertEqual(large.failure_rate, 0.5)
        self.assertEqual(large.status_counts, {"ok": 2, "missing": 2})
        self.assertEqual(large.edit_recall_by_quarter[2:], [0.0, 0.0])
        self.assertEqual(large.edit_recall_by_quarter[:2], [1.0, 1.0])

    def test_echo_model_makes_no_edits(self):
        result = asyncio.run(evaluate(EchoClient(), "mock/echo", SOURCE, sweep([BatchSpec(max_chars=10_000)], ["all"], 1)))
        self.assertEqual(result.summaries[0].edit_recall, 0.0)
        self.assertIsNone(result.summaries[0].edit_precision)

    def test_max_units(self):
        result = asyncio.run(evaluate(EchoClient(), "mock/echo", SOURCE, sweep([BatchSpec(max_units=8)], ["all"], 1), max_units=2))
        self.assertEqual(len(result.units), 2)

    def test_no_aligned_units(self):
        source = EvalSource("g", "i", None, ".//bibl", GOLD_XML, GOLD_XML.replace("Ibid.", "Op. cit.").replace("Smith", "X").replace("Doe", "Y").replace("Roe", "Z"), [])
        with self.assertRaises(ValueError):
            asyncio.run(evaluate(EchoClient(), "m", source, sweep([BatchSpec(max_units=1)], ["all"], 1)))


class TestReport(unittest.TestCase):
    def test_write_results(self):
        configs = sweep([BatchSpec(max_units=2)], ["all", "changed"], 1)
        result = asyncio.run(evaluate(OracleClient(_mapping()), "mock/oracle", SOURCE, configs))
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_results([result], Path(tmp) / "out")
            self.assertEqual([p.name for p in paths], ["units.csv", "batches.jsonl", "summary.json", "report.md"])
            with paths[0].open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 8)
            batches = [json.loads(line) for line in paths[1].read_text().splitlines()]
            self.assertEqual(len(batches), 4)
            self.assertIn("raw_response", batches[0])
            summary = json.loads(paths[2].read_text())
            self.assertEqual(summary[0]["meta"]["rules_hash"], SOURCE.rules_hash)
        report = markdown_report([result])
        self.assertIn("| units:2 | changed |", report)
        self.assertIn("| - | - |", report)  # baseline row


    def test_report_flags_an_input_that_is_not_an_extraction(self):
        source = dataclasses.replace(SOURCE, input_is_extraction=False)
        result = asyncio.run(evaluate(EchoClient(), "mock/echo", source, sweep([BatchSpec(max_units=4)], ["all"], 1)))
        self.assertIn("input version `in1` (given; NOT an extraction)", markdown_report([result]))

    def test_failed_requests_are_left_out_of_the_scores(self):
        class SecondCallFails:
            def __init__(self, client):
                self.client, self.calls = client, 0

            async def complete(self, system_prompt, user_prompt):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("provider down")
                return await self.client.complete(system_prompt, user_prompt)

        result = asyncio.run(evaluate(SecondCallFails(OracleClient(_mapping())), "mock/oracle", SOURCE, sweep([BatchSpec(max_units=2)], ["all"], 1)))
        summary = result.summaries[0]
        self.assertEqual((summary.units, summary.error_rate, summary.failure_rate), (4, 0.5, 0.0))
        self.assertEqual((summary.edit_precision, summary.exact_rate), (1.0, 1.0))  # the two answered units are right
        self.assertEqual(summary.status_counts, {"ok": 2, "error": 2})
        report = markdown_report([result])
        self.assertIn("| units:2 | all | 1 | 2 | 0 | 50.0 | 0.0 | 100.0 |", report)


if __name__ == "__main__":
    unittest.main()
