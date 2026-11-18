"""
Tests of transferring annotation decisions from an earlier version (alignment.py, transfer.py).

@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/alignment.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/transfer.py
"""

import asyncio
import json
import unittest
from pathlib import Path

from pte_plugin_auto_annotation.alignment import align_texts, normalize_char
from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.checks import ProposalStatus
from pte_plugin_auto_annotation.transfer import (
    COPIED,
    MODEL,
    UNMATCHED,
    TransferError,
    build_unit_user_prompt,
    check_coverage,
    consecutive_batches,
    doubt_marks,
    doubt_unit_batch,
    merge_corrections,
    new_unit_positions,
    parse_doubt_units,
    plan_unit_transfer,
    plan_zone_transfer,
    split_line_doubts,
    transfer_page_batch,
    transfer_unit_batch,
)
from pte_plugin_auto_annotation.units import document_units
from pte_plugin_auto_annotation.zones import ZONES_AVAILABLE, ZoneLayout, apply_page_changes, read_zone_doc

HERE = Path(__file__).parent
RULES = [("references", "Tag authors and titles.")]


def tei(*bibls: str) -> str:
    body = "\n".join(f"        {b}" for b in bibls)
    return (
        '<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader/><text><body><listBibl>\n'
        f"{body}\n      </listBibl></body></text></TEI>"
    )


# the earlier annotation, from a poor text layer: "Srnith", "Tor ts", a missing year
SOURCE = tei(
    "<bibl><author>Srnith, J.</author> <title>Contracts</title>, 2001.</bibl>",
    "<bibl><author>Doe, A.</author> <title>Tor ts</title>, 1999.</bibl>",
    "<bibl><author>Roe, B.</author> <title>Property</title>.</bibl>",
)
# the new extraction: corrected text, no markup, and a reference the old text layer had lost
TARGET = tei(
    "<bibl>Smith, J. Contracts, 2001.</bibl>",
    "<bibl>Doe, A. Torts, 1999.</bibl>",
    "<bibl>Roe, B. Property, 2010.</bibl>",
    "<bibl>Completely new reference text that was not recognized before at all.</bibl>",
)
UNIT_PATH = ".//listBibl/bibl"


class TestAlignment(unittest.TestCase):
    def test_identical_texts(self):
        alignment = align_texts("a bc  def", "a bc def")
        self.assertEqual(alignment.mapping, [0, 1, 2, 3, 4, 5])
        self.assertEqual(alignment.coverage, 1.0)

    def test_misread_and_inserted_characters(self):
        alignment = align_texts("Srnith wrote", "Smith wrote it")
        # S, m?, i, t, h aligned; "it" inserted
        self.assertEqual(alignment.mapping[0], 0)
        self.assertEqual(alignment.mapping[-2:], [None, None])
        self.assertEqual(alignment.mapping[5:10], [6, 7, 8, 9, 10])

    def test_split_words_and_ligatures(self):
        alignment = align_texts("Rechts fragen ﬁnden", "Rechtsfragen finden")
        self.assertNotIn(None, alignment.mapping)
        self.assertEqual(normalize_char("ſ"), "s")
        self.assertEqual(normalize_char("­"), "")


class TestUnitPlan(unittest.TestCase):
    def setUp(self):
        self.plan = plan_unit_transfer(SOURCE, TARGET, UNIT_PATH)

    def test_categories(self):
        self.assertEqual([u.category for u in self.plan.units], [MODEL, MODEL, MODEL, UNMATCHED])
        self.assertIn("Srnith", self.plan.units[0].fragment)
        self.assertEqual(self.plan.units[3].fragment, "")

    def test_identical_text_is_copied(self):
        target = tei("<bibl>Doe, A. Tor ts, 1999.</bibl>", "<bibl>Roe, B. Property.</bibl>")
        plan = plan_unit_transfer(SOURCE, target, UNIT_PATH)
        self.assertEqual([u.category for u in plan.units], [COPIED, COPIED])
        self.assertEqual(plan.units[0].proposal, "<bibl><author>Doe, A.</author> <title>Tor ts</title>, 1999.</bibl>")

    def test_copied_needs_the_same_line_breaks(self):
        source = tei("<bibl><author>Doe, A.</author> <lb/><title>Torts</title></bibl>")
        target = tei("<bibl>Doe, A. Torts</bibl>")
        self.assertEqual(plan_unit_transfer(source, target, UNIT_PATH).units[0].category, MODEL)

    def test_unchanged_copy_has_no_proposal(self):
        plan = plan_unit_transfer(SOURCE, SOURCE, UNIT_PATH)
        self.assertEqual([u.category for u in plan.units], [COPIED] * 3)
        self.assertEqual([u.proposal for u in plan.units], ["", "", ""])

    def test_coverage(self):
        check_coverage(self.plan)
        other = plan_unit_transfer(SOURCE, tei("<bibl>Nothing in common here whatsoever.</bibl>"), UNIT_PATH)
        with self.assertRaises(TransferError):
            check_coverage(other)

    def test_merge_corrections(self):
        target = tei("<bibl>Doe, A. Tor ts, 1999.</bibl>", "<bibl>Roe, B. Property, 2010.</bibl>")
        plan = plan_unit_transfer(SOURCE, target, UNIT_PATH)
        merged = merge_corrections(plan, {1: "<bibl>x</bibl>"})
        self.assertEqual(set(merged), {0, 1})
        with self.assertRaises(TransferError):
            merge_corrections(plan, {0: "<bibl>x</bibl>"})

    def test_consecutive_batches(self):
        units = document_units(TARGET, UNIT_PATH)
        self.assertEqual(consecutive_batches([0, 1, 3], units, BatchSpec.parse("units:8")), [[0, 1], [3]])
        self.assertEqual(consecutive_batches([0, 1, 2], units, BatchSpec.parse("units:2")), [[0, 1], [2]])


class FakeClient:
    """Answers with fixed text and records the prompts."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    async def complete(self, system_prompt, user_prompt):
        self.prompts.append((system_prompt, user_prompt))
        return self.answers.pop(0)


class TestTransferUnits(unittest.TestCase):
    def setUp(self):
        self.plan = plan_unit_transfer(SOURCE, TARGET, UNIT_PATH)
        self.units = document_units(TARGET, UNIT_PATH)

    def run_batch(self, answers, indices, marks=False):
        client = FakeClient(answers)
        result = asyncio.run(transfer_unit_batch(client, RULES, [self.units[i] for i in indices], self.plan.by_index(), marks=marks, split_on_failure=None))
        return client, result

    def test_reproduces_and_checks(self):
        client, result = self.run_batch([
            "### Unit 1\n<bibl><author>Smith, J.</author> <title>Contracts</title>, 2001.</bibl>\nDOUBT: the year should be a date.\n"
            "### Unit 2\n<bibl><author>Doe, A.</author> <title>Tort</title>, 1999.</bibl>"
        ], [0, 1], marks=True)
        self.assertIn("Earlier annotation", client.prompts[0][1])
        self.assertIn("<title>Tor ts</title>", client.prompts[0][1])
        self.assertIn("DOUBT", client.prompts[0][0])
        first, second = result.units
        self.assertEqual(first.status, ProposalStatus.OK)
        self.assertEqual(first.xml, "<bibl><author>Smith, J.</author> <title>Contracts</title>, 2001.</bibl>")
        self.assertEqual(first.doubt, "the year should be a date.")
        # the model changed the text: rejected
        self.assertEqual(second.status, ProposalStatus.TEXT_CHANGED)
        self.assertIsNone(second.xml)

    def test_doubts_only_with_marks(self):
        _, result = self.run_batch(["### Unit 1\n<bibl><author>Smith, J.</author> Contracts, 2001.</bibl>\nDOUBT: x"], [0])
        self.assertIsNone(result.units[0].doubt)
        self.assertEqual(result.units[0].status, ProposalStatus.OK)

    def test_segmenting_prompt_shows_the_earlier_annotation_once(self):
        prompt = build_unit_user_prompt(RULES, self.units[:2], ["<bibl>a</bibl>", "<bibl>a</bibl>"], segmenting=True)
        self.assertEqual(prompt.count("<bibl>a</bibl>"), 1)

    def test_doubt_batch(self):
        target = tei("<bibl>Doe, A. Tor ts, 1999.</bibl>", "<bibl>Roe, B. Property.</bibl>")
        plan = plan_unit_transfer(SOURCE, target, UNIT_PATH)
        units = document_units(target, UNIT_PATH)
        client = FakeClient(["### Doubt 2\nThe title is not a title."])
        result = asyncio.run(doubt_unit_batch(client, RULES, units, plan.by_index()))
        self.assertIn("<title>Tor ts</title>", client.prompts[0][1])
        self.assertEqual([u.doubt for u in result.units], [None, "The title is not a title."])
        self.assertEqual(parse_doubt_units("NO DOUBTS"), {})


class TestResults(unittest.TestCase):
    def test_new_unit_positions(self):
        self.assertEqual(new_unit_positions({1: "<bibl>a</bibl><bibl>b</bibl>", 2: ""}, 4), [0, 1, -1, 3])

    def test_doubt_marks(self):
        self.assertEqual(doubt_marks(["a", "b"], {1: "x", 5: "y"}), [{"fingerprint": "b", "comment": "x"}])
        self.assertEqual(doubt_marks(["a", "b", "c"], {2: "x"}, [0, -1, 2]), [{"fingerprint": "c", "comment": "x"}])

    def test_line_doubts(self):
        answer, doubts = split_line_doubts("0001-0003 body\nDOUBT 0002-0003: running header\n0004 page")
        self.assertEqual(answer.split(), ["0001-0003", "body", "0004", "page"])
        self.assertEqual(doubts, [(1, 2, "running header")])


XML = (HERE / "segmentation-sample.tei.xml").read_text(encoding="utf-8")
_LAYOUT = json.loads((HERE / "segmentation-sample.layout.json").read_text(encoding="utf-8"))
LAYOUT = ZoneLayout(_LAYOUT["labels"], _LAYOUT["pages"], [[] for _ in _LAYOUT["pages"]])


def annotated_with_old_ocr() -> str:
    """The sample with the reference's labels, and misread text as from a poor text layer."""
    zdoc = read_zone_doc(XML, LAYOUT)
    xml, problems = apply_page_changes(zdoc, {0: "0002 front", 2: "0016 listBibl"})
    assert not problems
    return xml.replace("Jane Doe", "Jarie Dce").replace("The second page", "Tne secand page").replace("stray text outside", "stray texl ouside")


@unittest.skipUnless(ZONES_AVAILABLE, "needs the editor's zone_lines module")
class TestZonePlan(unittest.TestCase):
    def setUp(self):
        self.target = read_zone_doc(XML, LAYOUT)
        self.plan = plan_zone_transfer(annotated_with_old_ocr(), self.target)

    def test_labels_are_projected(self):
        self.assertEqual([u.category for u in self.plan.units], [COPIED] * 4)
        self.assertEqual([u.proposal for u in self.plan.units], ["0002 front", "", "0016 listBibl", ""])
        xml, problems = apply_page_changes(self.target, {u.index: u.proposal for u in self.plan.units if u.proposal})
        self.assertEqual(problems, [])
        self.assertIn("<front>Cover page &amp; notes <lb/>", xml)

    def test_new_text_with_different_neighbours_goes_to_the_model(self):
        # in the source, the page number "2" is missing; between the body and the headnote it gets no label
        source = annotated_with_old_ocr().replace("<page>2</page> ", "")
        plan = plan_zone_transfer(source, self.target)
        self.assertEqual(plan.units[2].category, MODEL)
        self.assertIsNotNone(plan.units[2].source_lines)

    def test_model_window(self):
        source = annotated_with_old_ocr().replace("<page>2</page> ", "")
        plan = plan_zone_transfer(source, self.target)
        page = self.target.units[2]
        rows = "\n".join(f"{i + 1:04d} body" for i in self.target.pages[2].lines)
        rows = rows.replace("0011 body", "0011 | [page]2 [note:headnote]Doe and Roe, A Sample Article")
        rows += "\nDOUBT 0012: the body continues on the next page"
        client = FakeClient([rows])
        result = asyncio.run(transfer_page_batch(client, RULES, self.target, [page], plan, marks=True))
        self.assertIn("## Earlier version: corresponding lines", client.prompts[0][1])
        self.assertIn("\nS0", client.prompts[0][1])
        self.assertEqual(result.units[0].status, ProposalStatus.OK)
        self.assertEqual(result.units[0].doubt, "the body continues on the next page")


if __name__ == "__main__":
    unittest.main()
