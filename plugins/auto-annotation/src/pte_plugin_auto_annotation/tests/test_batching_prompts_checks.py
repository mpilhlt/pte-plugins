"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/batching.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/prompts.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/checks.py
"""

import unittest

from pte_plugin_auto_annotation.batching import BatchSpec, make_batches
from pte_plugin_auto_annotation.checks import ProposalStatus, check_proposal
from pte_plugin_auto_annotation.prompts import build_system_prompt, build_user_prompt, parse_unit_answer
from pte_plugin_auto_annotation.units import Unit


def _units(*lengths: int) -> list[Unit]:
    return [Unit(i, "x" * n, f"fp{i}") for i, n in enumerate(lengths)]


class TestBatching(unittest.TestCase):
    def test_by_units(self):
        batches = make_batches(_units(1, 1, 1, 1, 1), BatchSpec(max_units=2))
        self.assertEqual([len(b) for b in batches], [2, 2, 1])

    def test_by_chars_oversize_unit_alone(self):
        batches = make_batches(_units(3, 3, 10, 2), BatchSpec(max_chars=6))
        self.assertEqual([[u.index for u in b] for b in batches], [[0, 1], [2], [3]])

    def test_parse_and_label(self):
        self.assertEqual(BatchSpec.parse("chars:4000"), BatchSpec(max_chars=4000))
        self.assertEqual(BatchSpec.parse("units:8").label, "units:8")
        for bad in ("8", "units:0", "lines:3"):
            with self.assertRaises(ValueError):
                BatchSpec.parse(bad)


class TestPrompts(unittest.TestCase):
    def test_user_prompt_has_rules_before_units(self):
        prompt = build_user_prompt([("references", "RULES")], _units(2, 3), [1, 2])
        self.assertLess(prompt.index("RULES"), prompt.index("### Unit 1"))
        self.assertEqual(parse_unit_answer(prompt.split("## Units", 1)[1]), {1: "xx", 2: "xxx"})

    def test_system_prompt_per_mode(self):
        self.assertIn("every unit", build_system_prompt("all"))
        self.assertIn("NO CHANGES", build_system_prompt("changed"))

    def test_parse_answer(self):
        raw = "Here you go:\n```xml\n### Unit 2\n<bibl>a</bibl>\n\n### Unit 1\n<bibl>b</bibl>\n### Unit 2\n<bibl>dup</bibl>\n```"
        self.assertEqual(parse_unit_answer(raw), {2: "<bibl>a</bibl>", 1: "<bibl>b</bibl>"})
        self.assertEqual(parse_unit_answer("NO CHANGES"), {})

    def test_truncated_answer_keeps_complete_units(self):
        self.assertEqual(parse_unit_answer("### Unit 1\n<bibl>a</bibl>\n### Unit 2\n<bibl>b")[2], "<bibl>b")


class TestChecks(unittest.TestCase):
    ORIGINAL = "<bibl><author>Doe</author> Torts, 1999.</bibl>"

    def test_ok(self):
        self.assertEqual(check_proposal(self.ORIGINAL, '<bibl><author>Doe</author>\n <title level="m">Torts</title>, <date>1999</date>.</bibl>'), ProposalStatus.OK)

    def test_failures(self):
        self.assertEqual(check_proposal(self.ORIGINAL, "<bibl>Doe Torts, 1999."), ProposalStatus.MALFORMED)
        self.assertEqual(check_proposal(self.ORIGINAL, "<note>Doe Torts, 1999.</note>"), ProposalStatus.WRONG_ROOT)
        self.assertEqual(check_proposal(self.ORIGINAL, "<bibl>Doe</bibl><bibl>Torts, 1999.</bibl>"), ProposalStatus.WRONG_ROOT)
        self.assertEqual(check_proposal(self.ORIGINAL, "<bibl><author>Doe</author> Torts, 2000.</bibl>"), ProposalStatus.TEXT_CHANGED)

    def test_failure_flag(self):
        self.assertFalse(ProposalStatus.UNCHANGED.is_failure)
        self.assertTrue(ProposalStatus.MISSING.is_failure)


if __name__ == "__main__":
    unittest.main()
