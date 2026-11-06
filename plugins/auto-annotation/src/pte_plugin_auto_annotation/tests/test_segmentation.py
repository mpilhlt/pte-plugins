"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/segmentation.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/checks.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/correction.py
"""

import asyncio
import unittest

from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.checks import ProposalStatus, check_segments
from pte_plugin_auto_annotation.correction import correct_units
from pte_plugin_auto_annotation.evaluation import EvalSource, evaluate, sweep
from pte_plugin_auto_annotation.report import markdown_report
from pte_plugin_auto_annotation.segmentation import align_segments, is_segmenting, stream_fields, unit_scores
from pte_plugin_auto_annotation.units import document_units

from .fixtures import OracleClient, tei

SEGMENTER = "grobid.training.references.referenceSegmenter"
PATH = ".//listBibl/bibl"

# GROBID put each footnote into one <bibl> and missed the second label
INPUT_UNITS = [
    "<bibl><label>1</label> Smith, Contracts, 2001; Doe, Torts, 1999.</bibl>",
    "<bibl>2 Roe, Property, 2010.</bibl>",
]
# The annotator split the first footnote into two references and marked the second label
GOLD_UNITS = [
    "<bibl><label>1</label> Smith, Contracts, 2001; </bibl>",
    "<bibl>Doe, Torts, 1999.</bibl>",
    "<bibl><label>2</label> Roe, Property, 2010.</bibl>",
]
# The correct answer per input unit
ANSWERS = {
    INPUT_UNITS[0]: "<bibl><label>1</label> Smith, Contracts, 2001; </bibl>\n<bibl>Doe, Torts, 1999.</bibl>",
    INPUT_UNITS[1]: "<bibl><label>2</label> Roe, Property, 2010.</bibl>",
}


def source(gold_units: list[str] = GOLD_UNITS) -> EvalSource:
    return EvalSource(
        reference_stable_id="gold1", input_stable_id="in1", variant=SEGMENTER, unit_path=PATH,
        gold_xml=tei(gold_units, pretty=True), input_xml=tei(INPUT_UNITS), rules=[("segmentation", "rules")],
    )


class TestCheckSegments(unittest.TestCase):
    def test_statuses(self):
        original = INPUT_UNITS[0]
        self.assertEqual(check_segments(original, ANSWERS[original]), ProposalStatus.OK)  # split
        self.assertEqual(check_segments(original, original), ProposalStatus.OK)
        self.assertEqual(check_segments(original, ""), ProposalStatus.TEXT_CHANGED)  # emptied (merge)
        self.assertEqual(check_segments(original, "<bibl><label>1</label> Smith, Contracts, 2001;</bibl>"), ProposalStatus.TEXT_CHANGED)
        self.assertEqual(check_segments(original, "<note>x</note>"), ProposalStatus.WRONG_ROOT)
        self.assertEqual(check_segments(original, "<bibl>1 Smith, Contracts, 2001;</bibl> stray <bibl>Doe, Torts, 1999.</bibl>"), ProposalStatus.WRONG_ROOT)
        self.assertEqual(check_segments(original, "<bibl>"), ProposalStatus.MALFORMED)


class TestCorrectUnitsSegmenting(unittest.TestCase):
    def units(self):
        return document_units(tei(INPUT_UNITS), PATH)

    def test_split_and_prompt(self):
        client = OracleClient(ANSWERS)
        run = asyncio.run(correct_units(client, [("r", "rules")], self.units(), BatchSpec(max_units=2), segmenting=True))
        self.assertEqual([o.status for o in run.outcomes], [ProposalStatus.OK, ProposalStatus.OK])
        self.assertIn("split it by returning several elements", client.calls[0][0])
        self.assertNotIn("Keep the unit's outermost element", client.calls[0][0])

    def test_text_moved_between_units_is_accepted_only_if_the_batch_keeps_its_text(self):
        units = self.units()
        # the model moves "Doe, Torts, 1999." from unit 1 to the start of unit 2
        moved = {
            INPUT_UNITS[0]: "<bibl><label>1</label> Smith, Contracts, 2001; </bibl>",
            INPUT_UNITS[1]: "<bibl>Doe, Torts, 1999.</bibl>\n<bibl><label>2</label> Roe, Property, 2010.</bibl>",
        }
        run = asyncio.run(correct_units(OracleClient(moved), [("r", "rules")], units, BatchSpec(max_units=2), segmenting=True))
        self.assertEqual([o.status for o in run.outcomes], [ProposalStatus.OK, ProposalStatus.OK])
        # batches of one unit cannot move text: both units fail and fall back to the input
        run = asyncio.run(correct_units(OracleClient(moved), [("r", "rules")], units, BatchSpec(max_units=1), segmenting=True))
        self.assertEqual([o.status for o in run.outcomes], [ProposalStatus.TEXT_CHANGED, ProposalStatus.TEXT_CHANGED])
        self.assertEqual([o.proposal for o in run.outcomes], [None, None])
        # without segmenting, a split is a wrong root
        run = asyncio.run(correct_units(OracleClient(ANSWERS), [("r", "rules")], units, BatchSpec(max_units=2)))
        self.assertEqual(run.outcomes[0].status, ProposalStatus.WRONG_ROOT)


class TestAlignAndScore(unittest.TestCase):
    def test_stream_fields(self):
        text, spans, fields = stream_fields(["<bibl><label>1</label> A b;</bibl>", "", "<bibl>C</bibl><bibl>D</bibl>"])
        self.assertEqual((text, spans), ("1Ab;CD", [(0, 4), (4, 4), (4, 6)]))
        self.assertEqual(sorted(fields), [("bibl", (), 0, 4), ("bibl", (), 4, 5), ("bibl", (), 5, 6), ("bibl/label", (), 0, 1)])

    def test_alignment_maps_gold_fields_onto_the_input_text(self):
        alignment = align_segments(tei(INPUT_UNITS), tei(GOLD_UNITS, pretty=True), PATH)
        self.assertEqual(([u.index for u in alignment.units], alignment.unmatched_input, alignment.unmatched_gold), ([0, 1], 0, 0))
        baseline = unit_scores(alignment, None)
        # unit 1: the footnote bibl is replaced by two bibls (3 edits); unit 2: a label to add (1 edit)
        self.assertEqual((baseline[0].needed, baseline[0].made, baseline[0].exact), (3, 0, False))
        self.assertEqual((baseline[1].needed, baseline[1].made), (1, 0))

    def test_units_whose_text_the_gold_version_changed_are_not_evaluated(self):
        gold = [*GOLD_UNITS[:2], "<bibl><label>2</label> Roe, Property law, 2010.</bibl>"]
        alignment = align_segments(tei(INPUT_UNITS), tei(gold, pretty=True), PATH)
        self.assertEqual(([u.index for u in alignment.units], alignment.unmatched_input, alignment.unmatched_gold), ([0], 1, 1))


class TestEvaluateSegmenting(unittest.TestCase):
    def test_oracle_scores_perfectly_at_every_batch_size(self):
        self.assertTrue(is_segmenting(SEGMENTER) and not is_segmenting("grobid.training.references"))
        configs = sweep([BatchSpec(max_units=1), BatchSpec(max_units=2)], ["all", "changed"], 1)
        result = asyncio.run(evaluate(OracleClient(ANSWERS), "mock/oracle", source(), configs))
        for summary in result.summaries:
            self.assertEqual((summary.edit_precision, summary.edit_recall, summary.exact_rate, summary.failure_rate), (1.0, 1.0, 1.0, 0.0), summary)
        self.assertEqual(result.baseline.edit_recall if result.baseline else None, 0.0)
        self.assertIn("Segmenting variant", markdown_report([result]))


if __name__ == "__main__":
    unittest.main()
