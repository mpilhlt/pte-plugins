"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/single.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/document.py
"""

import asyncio
import unittest

from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.checks import ProposalStatus
from pte_plugin_auto_annotation.document import write_corrections
from pte_plugin_auto_annotation.single import CorrectionSummary, check_corrections, correct_batch, plan_batches
from pte_plugin_auto_annotation.units import Unit, document_units

from .fixtures import GOLD_UNITS, INPUT_XML, FailingClient, FixedAnswerClient, OracleClient, gold_mapping

UNIT_PATH = ".//listBibl/bibl"
RULES = [("references", "RULES")]


async def _no_sleep(_seconds: float) -> None:
    return None


class TestSingleModel(unittest.TestCase):
    def setUp(self):
        self.units = document_units(INPUT_XML, UNIT_PATH)

    def test_plan_batches(self):
        self.assertEqual(plan_batches(self.units, BatchSpec(max_units=3)), [[0, 1, 2], [3]])

    def test_correct_batch_returns_only_changed_valid_units(self):
        mapping = gold_mapping()
        # the second unit is answered unchanged
        mapping[self.units[1].xml] = self.units[1].xml
        result = asyncio.run(correct_batch(OracleClient(mapping), RULES, self.units[:2], sleep=_no_sleep))
        self.assertIsNone(result.error)
        self.assertEqual(result.calls, 1)
        self.assertEqual([(u.index, u.status, u.xml) for u in result.units], [(0, ProposalStatus.OK, GOLD_UNITS[0]), (1, ProposalStatus.OK, None)])

    def test_correct_batch_keeps_the_original_of_invalid_answers(self):
        result = asyncio.run(correct_batch(FixedAnswerClient("<bibl>other text</bibl>"), RULES, self.units[:1], sleep=_no_sleep))
        self.assertEqual([(u.status, u.xml) for u in result.units], [(ProposalStatus.TEXT_CHANGED, None)])

    def test_correct_batch_reports_a_failed_request(self):
        result = asyncio.run(correct_batch(FailingClient(), RULES, self.units[:2], sleep=_no_sleep))
        self.assertIn("provider down", result.error or "")
        self.assertEqual({u.status for u in result.units}, {ProposalStatus.ERROR})

    def test_check_corrections(self):
        self.assertEqual(check_corrections(self.units, {0: GOLD_UNITS[0]}), [])
        self.assertEqual(check_corrections(self.units, {9: GOLD_UNITS[0]}), ["No unit with index 9"])
        self.assertEqual(check_corrections(self.units, {0: "<bibl>other</bibl>"}), ["Unit 0: text_changed"])
        self.assertEqual(check_corrections(self.units, {0: "<bibl>"}), ["Unit 0: malformed"])

    def test_check_corrections_of_a_segmenting_variant(self):
        units = [Unit(0, "<bibl>A B</bibl>", "f0"), Unit(1, "<bibl>C</bibl>", "f1")]
        # text moved into the first unit, the second one removed
        self.assertEqual(check_corrections(units, {0: "<bibl>A</bibl> <bibl>B C</bibl>", 1: ""}, segmenting=True), [])
        self.assertEqual(check_corrections(units, {1: ""}, segmenting=True), ["The corrections change the text of the document"])

    def test_write_corrections_records_the_model_and_the_counts(self):
        summary = CorrectionSummary(units=4, processed=3, changed=1, invalid=1, errors=0)
        xml = write_corrections(INPUT_XML, UNIT_PATH, {0: GOLD_UNITS[0]}, "kisski", "deepseek", summary, "2026-11-11T10:00:00Z", 75, model_label="DeepSeek <V4>")
        self.assertEqual([u.xml for u in document_units(xml, UNIT_PATH)], [GOLD_UNITS[0], *[u.xml for u in self.units[1:]]])
        self.assertIn('who="#auto-annotation"', xml)
        self.assertIn('Annotation corrected by <name type="model" subtype="kisski" key="kisski/deepseek">DeepSeek &lt;V4&gt;</name>: 4 units, 1 changed. Stopped after 3 of 4 units.', xml)
        self.assertIn('<measure type="corrected-units" subtype="changed" commodity="kisski/deepseek" quantity="1">changed 1</measure>', xml)
        self.assertIn('subtype="unchanged" commodity="kisski/deepseek" quantity="1"', xml)
        self.assertIn('subtype="unprocessed" commodity="kisski/deepseek" quantity="1"', xml)
        self.assertIn('<time dur="PT1M15S">1 min 15 s</time>', xml)


if __name__ == "__main__":
    unittest.main()
