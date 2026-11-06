"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/arbitration.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/pipeline.py
"""

import asyncio
import unittest

from pte_plugin_auto_annotation.arbitration import (
    ORIGINAL,
    canonical_key,
    distinct_candidates,
    render_candidates,
    which_candidate,
)
from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.checks import ProposalStatus
from pte_plugin_auto_annotation.pipeline import Decision, PipelineConfig, annotate_units
from pte_plugin_auto_annotation.units import Unit, document_units

from .fixtures import GOLD_UNITS, INPUT_XML, ArbiterOracle, EchoClient, FailingClient, FixedAnswerClient, OracleClient, gold_mapping

RULES = [("references", "rules")]
CONFIG = PipelineConfig(spec=BatchSpec(max_units=4), arbiter_spec=BatchSpec(max_units=4), retry=None)


def _units() -> list[Unit]:
    return document_units(INPUT_XML, ".//listBibl/bibl")


def _run(a, b, arbiter, config=CONFIG):
    return asyncio.run(annotate_units(_units(), RULES, a, b, arbiter, config))


class TestArbitrationHelpers(unittest.TestCase):
    def test_canonical_key_ignores_formatting(self):
        self.assertEqual(canonical_key('<bibl a="1"  b="2">x  y</bibl>'), canonical_key('<bibl b="2" a="1">x\n y</bibl>'))
        self.assertIsNone(canonical_key("<bibl>"))
        self.assertIsNone(canonical_key("<bibl/><bibl/>"))

    def test_distinct_candidates_merges_duplicates_and_drops_the_original(self):
        unit = Unit(0, "<bibl>x</bibl>", "fp")
        self.assertEqual(distinct_candidates(unit, {"A": "<bibl><title>x</title></bibl>", "B": "<bibl><title>x</title></bibl>"}),
                         {"A+B": "<bibl><title>x</title></bibl>"})
        self.assertEqual(distinct_candidates(unit, {"A": "<bibl>x</bibl>", "B": None}), {})

    def test_render_and_which_candidate(self):
        unit = Unit(0, "<bibl>x</bibl>", "fp")
        candidates = {"A": "<bibl><title>x</title></bibl>", "B": "<bibl><date>x</date></bibl>"}
        rendered = render_candidates(unit, candidates)
        self.assertTrue(rendered.startswith("Original:\n<bibl>x</bibl>\nCandidate A:"))
        self.assertIn("No candidate", render_candidates(unit, {}))
        self.assertEqual(which_candidate("<bibl><date>x</date></bibl>", unit, candidates), "B")
        self.assertEqual(which_candidate("<bibl>x</bibl>", unit, candidates), ORIGINAL)
        self.assertEqual(which_candidate("<bibl><idno>x</idno></bibl>", unit, candidates), "own")


class TestPipeline(unittest.TestCase):
    def test_agreement_needs_no_arbiter(self):
        arbiter = ArbiterOracle(gold_mapping())
        result = _run(OracleClient(gold_mapping()), OracleClient(gold_mapping()), arbiter)
        self.assertEqual([d.decision for d in result.decisions], [Decision.AGREED] * 4)
        self.assertEqual([d.result_xml for d in result.decisions], GOLD_UNITS)
        self.assertEqual(arbiter.calls, [])
        self.assertEqual(result.counts(), {"agreed": 4, "arbitrated": 0, "unresolved": 0, "changed": 4})
        self.assertEqual(set(result.runs), {"A", "B"})

    def test_disagreement_goes_to_the_arbiter_with_both_candidates(self):
        mapping_b = gold_mapping()
        second = _units()[1].xml
        mapping_b[second] = second.replace("<title>", '<title level="a">')
        arbiter = ArbiterOracle(gold_mapping())
        result = _run(OracleClient(gold_mapping()), OracleClient(mapping_b), arbiter)
        decision = result.decisions[1]
        self.assertEqual((decision.decision, decision.chosen, decision.result_xml), (Decision.ARBITRATED, "A", GOLD_UNITS[1]))
        self.assertEqual(len(arbiter.calls), 1)
        prompt = arbiter.calls[0][1]
        self.assertIn("Candidate A:", prompt)
        self.assertIn("Candidate B:", prompt)
        self.assertEqual(prompt.count("### Unit"), 1)  # only the disputed unit
        self.assertIn("arbiter chose A", decision.comment)

    def test_without_arbiter_disagreements_stay_unresolved(self):
        result = _run(OracleClient(gold_mapping()), EchoClient(), None)
        self.assertEqual({d.decision for d in result.decisions}, {Decision.UNRESOLVED})
        self.assertEqual([d.result_xml for d in result.decisions], [u.xml for u in _units()])
        self.assertFalse(any(d.changed for d in result.decisions))
        self.assertIn("no arbiter", result.decisions[0].comment)

    def test_arbiter_failing_the_checks_leaves_the_unit_unresolved(self):
        result = _run(OracleClient(gold_mapping()), EchoClient(), FixedAnswerClient("<bibl>other text</bibl>"))
        decision = result.decisions[0]
        self.assertEqual((decision.decision, decision.arbiter_status), (Decision.UNRESOLVED, ProposalStatus.TEXT_CHANGED))
        self.assertEqual(decision.result_xml, _units()[0].xml)

    def test_arbiter_corrects_when_both_models_fail(self):
        broken = FixedAnswerClient("<bibl>changed text</bibl>")
        result = _run(broken, FailingClient(), ArbiterOracle(gold_mapping()))
        decision = result.decisions[0]
        self.assertEqual(decision.statuses, {"A": ProposalStatus.TEXT_CHANGED, "B": ProposalStatus.ERROR})
        self.assertEqual((decision.decision, decision.chosen, decision.result_xml), (Decision.ARBITRATED, "own", GOLD_UNITS[0]))
        self.assertIn("A: text_changed", decision.comment)

    def test_agreement_on_the_input_counts_as_agreed_and_unchanged(self):
        result = _run(EchoClient(), EchoClient(), None)
        self.assertEqual(result.counts(), {"agreed": 4, "arbitrated": 0, "unresolved": 0, "changed": 0})


if __name__ == "__main__":
    unittest.main()
