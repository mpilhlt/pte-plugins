"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/arbitration.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/pipeline.py
"""

import asyncio
import unittest
from dataclasses import replace

from pte_plugin_auto_annotation.arbitration import (
    ORIGINAL,
    canonical_key,
    distinct_candidates,
    render_candidates,
    which_candidate,
)
from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.checks import ProposalStatus
from pte_plugin_auto_annotation.correction import ProviderChatClient, RetryPolicy, TransientError, correct_units, is_transient_message
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
        self.assertEqual(result.counts(), {"agreed": 4, "arbitrated": 0, "fallback": 0, "unresolved": 0, "changed": 4})
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

    def test_arbiter_failing_the_checks_falls_back_to_model_b(self):
        result = _run(EchoClient(), OracleClient(gold_mapping()), FixedAnswerClient("<bibl>other text</bibl>"))
        decision = result.decisions[0]
        self.assertEqual((decision.decision, decision.arbiter_status, decision.chosen), (Decision.FALLBACK, ProposalStatus.TEXT_CHANGED, "B"))
        self.assertEqual(decision.result_xml, GOLD_UNITS[0])
        self.assertIn("fell back to B", decision.comment)
        self.assertEqual(result.counts()["fallback"], 4)

    def test_failed_arbiter_request_falls_back_to_model_b(self):
        result = _run(EchoClient(), OracleClient(gold_mapping()), FailingClient())
        self.assertEqual((result.decisions[0].decision, result.decisions[0].arbiter_status), (Decision.FALLBACK, ProposalStatus.ERROR))

    def test_no_fallback_proposal_leaves_the_unit_unresolved(self):
        result = _run(OracleClient(gold_mapping()), FailingClient(), FixedAnswerClient("<bibl>other text</bibl>"))
        decision = result.decisions[0]
        self.assertEqual((decision.decision, decision.chosen), (Decision.UNRESOLVED, None))
        self.assertEqual(decision.result_xml, _units()[0].xml)

    def test_fallback_can_be_switched_off(self):
        result = _run(EchoClient(), OracleClient(gold_mapping()), FixedAnswerClient("<bibl>other text</bibl>"), replace(CONFIG, fallback=None))
        decision = result.decisions[0]
        self.assertEqual((decision.decision, decision.result_xml), (Decision.UNRESOLVED, _units()[0].xml))

    def test_arbiter_corrects_when_both_models_fail(self):
        broken = FixedAnswerClient("<bibl>changed text</bibl>")
        result = _run(broken, FailingClient(), ArbiterOracle(gold_mapping()))
        decision = result.decisions[0]
        self.assertEqual(decision.statuses, {"A": ProposalStatus.TEXT_CHANGED, "B": ProposalStatus.ERROR})
        self.assertEqual((decision.decision, decision.chosen, decision.result_xml), (Decision.ARBITRATED, "own", GOLD_UNITS[0]))
        self.assertIn("A: text_changed", decision.comment)

    def test_agreement_on_the_input_counts_as_agreed_and_unchanged(self):
        result = _run(EchoClient(), EchoClient(), None)
        self.assertEqual(result.counts(), {"agreed": 4, "arbitrated": 0, "fallback": 0, "unresolved": 0, "changed": 0})


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class StallingClient:
    """Every request takes 400 s and then times out."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = 0

    async def complete(self, system_prompt, user_prompt):
        self.calls += 1
        self.clock.now += 400
        raise TransientError("Read timed out")


async def _no_sleep(_seconds):
    return None


class TestTimeBudget(unittest.TestCase):
    def _run(self, budget):
        clock = FakeClock()
        client = StallingClient(clock)
        run = asyncio.run(correct_units(client, [], _units(), BatchSpec(max_units=2), retry=RetryPolicy(retries=4), sleep=_no_sleep, time_budget=budget, clock=clock))
        return client, run

    def test_retries_and_new_requests_stop_when_the_budget_is_used_up(self):
        client, run = self._run(500)
        self.assertEqual(client.calls, 2)
        self.assertEqual(run.batches[0].retries, 1)
        self.assertTrue(str(run.batches[1].error).startswith("TimeBudgetExceeded"))
        self.assertEqual({o.status for o in run.outcomes}, {ProposalStatus.ERROR})

    def test_without_a_budget_all_retries_are_used(self):
        client, _ = self._run(None)
        self.assertEqual(client.calls, 10)


class _TimingOutProvider:
    async def chat_completion(self, model_id, system_prompt, user_prompt, temperature=0.2):
        raise ConnectionError("HTTPSConnectionPool(host='x', port=443): Max retries exceeded (Caused by ReadTimeoutError(\"Read timed out. (read timeout=180)\"))")


class TestTimeoutsAreTransient(unittest.TestCase):
    def test_read_timeout_message_is_transient(self):
        self.assertTrue(is_transient_message("Read timed out. (read timeout=180)"))

    def test_provider_client_does_not_retry_a_rate_limit(self):
        from fastapi_app.lib.llm.rate_limit import LLMRateLimitError

        class LimitedProvider:
            async def chat_completion(self, model_id, system_prompt, user_prompt, temperature=0.2):
                raise LLMRateLimitError("Rate limit or quota reached for model 'm'")

        with self.assertRaises(LLMRateLimitError) as raised:
            asyncio.run(ProviderChatClient(LimitedProvider(), "m").complete("s", "u"))
        self.assertNotIsInstance(raised.exception, TransientError)

    def test_provider_client_retries_a_read_timeout(self):
        with self.assertRaises(TransientError):
            asyncio.run(ProviderChatClient(_TimingOutProvider(), "m").complete("s", "u"))


if __name__ == "__main__":
    unittest.main()
