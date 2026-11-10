"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/pipeline.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/document.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/arbitration.py
"""

import asyncio
import re
import unittest

from pte_plugin_auto_annotation.arbitration import ORIGINAL, segments_key
from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.checks import ProposalStatus, check_segments, unit_text
from pte_plugin_auto_annotation.document import write_result
from pte_plugin_auto_annotation.pipeline import Decision, PipelineConfig, PipelineResult, annotate_units, segment_groups
from pte_plugin_auto_annotation.units import document_units

from .fixtures import EchoClient, OracleClient, prompt_units, tei
from .test_segmentation import ANSWERS, GOLD_UNITS, INPUT_UNITS, PATH

RULES = [("references", "rules")]
CONFIG = PipelineConfig(spec=BatchSpec(max_units=4), arbiter_spec=BatchSpec(max_units=4), retry=None, segmenting=True)
MODELS = {"A": "p/a", "B": "p/b", "arbiter": "p/c"}
WHEN = "2026-11-06T12:00:00Z"
INPUT_XML = tei(INPUT_UNITS)
GOLD = "\n".join(GOLD_UNITS)
# The model moves "Doe, Torts, 1999." from the first unit to the start of the second
MOVED = {
    INPUT_UNITS[0]: "<bibl><label>1</label> Smith, Contracts, 2001; </bibl>",
    INPUT_UNITS[1]: "<bibl>Doe, Torts, 1999.</bibl>\n<bibl><label>2</label> Roe, Property, 2010.</bibl>",
}


class GroupArbiter:
    """Arbiter that answers every unit (or group) whose original is in the mapping, else with the original."""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        parts = []
        for uid, shown in prompt_units(user_prompt).items():
            original = re.split(r"\n(?:Candidate |\(No candidate)", shown.split("Original:\n", 1)[1], maxsplit=1)[0]
            parts.append(f"### Unit {uid}\n{self.mapping.get(original, original)}")
        return "\n\n".join(parts)


def _run(a, b, arbiter) -> PipelineResult:
    return asyncio.run(annotate_units(document_units(INPUT_XML, PATH), RULES, a, b, arbiter, CONFIG))


def _output_units(result: PipelineResult) -> tuple[list[str], str]:
    out = write_result(INPUT_XML, PATH, result, MODELS, WHEN, 1)
    return [u.xml for u in document_units(out, PATH)], out


class TestSegmentingPipeline(unittest.TestCase):
    def test_agreed_split(self):
        result = _run(OracleClient(ANSWERS), OracleClient(ANSWERS), None)
        self.assertEqual([d.decision for d in result.decisions], [Decision.AGREED, Decision.AGREED])
        units, out = _output_units(result)
        self.assertEqual([segments_key(u) for u in units], [segments_key(g) for g in GOLD_UNITS])
        self.assertNotIn("unit-review", out)

    def test_moved_text_and_split_agree_as_a_group(self):
        result = _run(OracleClient(MOVED), OracleClient(ANSWERS), None)
        self.assertEqual(segment_groups(document_units(INPUT_XML, PATH), list(result.runs.values())), [[0, 1]])
        [decision] = result.decisions
        self.assertEqual((decision.decision, decision.covered), (Decision.AGREED, (0, 1)))
        units, _ = _output_units(result)
        self.assertEqual([segments_key(u) for u in units], [segments_key(g) for g in GOLD_UNITS])

    def test_arbiter_decides_per_unit(self):
        arbiter = GroupArbiter(ANSWERS)
        result = _run(OracleClient(ANSWERS), EchoClient(), arbiter)
        self.assertEqual([(d.decision, d.chosen) for d in result.decisions], [(Decision.ARBITRATED, "A"), (Decision.ARBITRATED, "A")])
        self.assertIn("split into several elements or merged", arbiter.calls[0][0])
        units, _ = _output_units(result)
        self.assertEqual(len(units), 3)

    def test_arbiter_decides_a_group_and_the_result_replaces_its_units(self):
        group = "\n".join(INPUT_UNITS)
        arbiter = GroupArbiter({group: GOLD})
        result = _run(OracleClient(MOVED), EchoClient(), arbiter)
        [decision] = result.decisions
        self.assertEqual((decision.decision, decision.covered, decision.chosen), (Decision.ARBITRATED, (0, 1), "A"))
        self.assertIn(f"Original:\n{group}\nCandidate A:", arbiter.calls[0][1])
        units, _ = _output_units(result)
        self.assertEqual([segments_key(u) for u in units], [segments_key(g) for g in GOLD_UNITS])
        self.assertEqual(unit_text("".join(units)), unit_text("".join(INPUT_UNITS)))

    def test_arbiter_answer_with_other_text_falls_back_to_model_b(self):
        group = "\n".join(INPUT_UNITS)
        result = _run(OracleClient(MOVED), EchoClient(), GroupArbiter({group: GOLD_UNITS[0]}))
        [decision] = result.decisions
        self.assertEqual((decision.decision, decision.arbiter_status), (Decision.FALLBACK, ProposalStatus.TEXT_CHANGED))
        units, _ = _output_units(result)
        self.assertEqual(units, INPUT_UNITS)

    def test_arbiter_may_keep_the_original(self):
        result = _run(OracleClient(ANSWERS), EchoClient(), GroupArbiter({}))
        self.assertEqual([d.chosen for d in result.decisions], [ORIGINAL, ORIGINAL])
        self.assertFalse(any(d.changed for d in result.decisions))

    def test_check_segments_accepts_a_group_as_original(self):
        group = "\n".join(INPUT_UNITS)
        self.assertEqual(check_segments(group, GOLD), ProposalStatus.OK)
        self.assertEqual(check_segments(group, GOLD_UNITS[0]), ProposalStatus.TEXT_CHANGED)


if __name__ == "__main__":
    unittest.main()
