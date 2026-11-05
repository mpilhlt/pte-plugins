"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/correction.py
"""

import asyncio
import unittest

from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.checks import ProposalStatus
from pte_plugin_auto_annotation.correction import ProviderChatClient, correct_units
from pte_plugin_auto_annotation.units import document_units

from .fixtures import GOLD_UNITS, INPUT_XML, EchoClient, FailingClient, OracleClient

RULES = [("references", "rules")]


def _setup() -> tuple[list, dict[str, str]]:
    units = document_units(INPUT_XML, ".//listBibl/bibl")
    return units, {u.xml: g for u, g in zip(units, GOLD_UNITS)}


class TestCorrectUnits(unittest.TestCase):
    def test_oracle_all_mode(self):
        units, mapping = _setup()
        client = OracleClient(mapping)
        run = asyncio.run(correct_units(client, RULES, units, BatchSpec(max_units=3)))
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([o.proposal for o in run.outcomes], GOLD_UNITS)
        self.assertEqual([(o.batch_index, o.position, o.batch_size) for o in run.outcomes], [(0, 0, 3), (0, 1, 3), (0, 2, 3), (1, 0, 1)])
        self.assertTrue(all(o.status == ProposalStatus.OK for o in run.outcomes))
        self.assertEqual(run.batches[0].unit_indices, [0, 1, 2])

    def test_missing_units_in_all_mode_and_unchanged_in_changed_mode(self):
        units, mapping = _setup()
        run = asyncio.run(correct_units(OracleClient(mapping, drop_after=2), RULES, units, BatchSpec(max_units=4)))
        self.assertEqual([o.status for o in run.outcomes][2:], [ProposalStatus.MISSING] * 2)
        self.assertIsNone(run.outcomes[3].proposal)
        run = asyncio.run(correct_units(OracleClient(mapping, drop_after=2), RULES, units, BatchSpec(max_units=4), mode="changed"))
        self.assertEqual(run.outcomes[3].status, ProposalStatus.UNCHANGED)
        self.assertEqual(run.outcomes[3].proposal, units[3].xml)

    def test_no_changes_answer(self):
        units, _ = _setup()
        run = asyncio.run(correct_units(OracleClient({}, only_changed=True), RULES, units, BatchSpec(max_units=4), mode="changed"))
        self.assertTrue(all(o.status == ProposalStatus.UNCHANGED for o in run.outcomes))

    def test_failed_request_is_recorded(self):
        units, _ = _setup()
        run = asyncio.run(correct_units(FailingClient(), RULES, units, BatchSpec(max_units=2), mode="changed"))
        self.assertTrue(all(o.status == ProposalStatus.MISSING for o in run.outcomes))
        self.assertIn("provider down", run.batches[0].error or "")

    def test_split_on_failure_retries_halves(self):
        units, mapping = _setup()
        client = OracleClient(mapping, drop_after=1)  # every answer stops after its first unit
        run = asyncio.run(correct_units(client, RULES, units, BatchSpec(max_units=4), split_on_failure=0.0))
        self.assertTrue(all(o.status == ProposalStatus.OK for o in run.outcomes))
        self.assertEqual([o.proposal for o in run.outcomes], GOLD_UNITS)
        self.assertEqual(len(run.batches), 7)  # 4 -> 2 + 2 -> 1 + 1 + 1 + 1
        self.assertEqual(run.batches[1].split_from, 0)

    def test_concurrency_keeps_unit_order(self):
        units, _ = _setup()
        run = asyncio.run(correct_units(EchoClient(), RULES, units, BatchSpec(max_units=1), concurrency=4))
        self.assertEqual([o.unit.index for o in run.outcomes], [0, 1, 2, 3])
        self.assertEqual(sorted(b.index for b in run.batches), [0, 1, 2, 3])


class TestProviderChatClient(unittest.TestCase):
    def test_delegates_to_provider(self):
        class Provider:
            async def chat_completion(self, model_id, system_prompt, user_prompt, temperature=0.2):
                return f"{model_id}|{system_prompt}|{user_prompt}|{temperature}"

        client = ProviderChatClient(Provider(), "m1")
        self.assertEqual(asyncio.run(client.complete("s", "u")), "m1|s|u|0.0")


if __name__ == "__main__":
    unittest.main()
