"""
Tests of correcting and scoring zone documents by page (zones.py, zone_scoring.py).

@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/zones.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/zone_scoring.py
"""

import asyncio
import json
import re
import unittest
from unittest import mock
from pathlib import Path

from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.checks import ProposalStatus
from pte_plugin_auto_annotation.evaluation import EvalSource, evaluate, sweep
from pte_plugin_auto_annotation.report import markdown_report
from pte_plugin_auto_annotation.zones import (
    ZONES_AVAILABLE,
    ZoneLayout,
    apply_page_changes,
    build_user_prompt,
    correct_pages,
    judge_answer,
    read_zone_doc,
)

HERE = Path(__file__).parent
XML = (HERE / "segmentation-sample.tei.xml").read_text(encoding="utf-8")
_LAYOUT = json.loads((HERE / "segmentation-sample.layout.json").read_text(encoding="utf-8"))
LAYOUT = ZoneLayout(_LAYOUT["labels"], _LAYOUT["pages"], [["top"] if i == 10 else [] for i in range(len(_LAYOUT["pages"]))])
RULES = [("segmentation", "Lines outside zones belong to the zone around them.")]
# The reference: "Cover page & notes" belongs to the front, the stray line to the bibliography
GOLD_CHANGES = "0002 front\n0016 listBibl"
_ROW_RE = re.compile(r"^(\d{4})\b")


def gold_xml() -> str:
    zdoc = read_zone_doc(XML, LAYOUT)
    xml, problems = apply_page_changes(zdoc, {0: "0002 front", 2: "0016 listBibl"})
    assert not problems
    return xml


class OracleClient:
    """Answers with the reference's labels of the lines of the pages it is shown (not of the context)."""

    def __init__(self, gold: str, mode: str = "all", garbage_above: int | None = None):
        self.gold = read_zone_doc(gold, LAYOUT)
        self.mode = mode
        self.garbage_above = garbage_above
        self.windows: list[list[int]] = []

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        listing = user_prompt.split("## Document lines", 1)[1]
        ids: list[int] = []
        in_page = False
        for row in listing.splitlines():
            if row.startswith("==="):
                in_page = row.startswith("=== Page")
            elif in_page and (m := _ROW_RE.match(row)):
                ids.append(int(m.group(1)) - 1)
        self.windows.append(ids)
        if self.garbage_above is not None and len(ids) > self.garbage_above:
            return "I cannot do that."
        rows = []
        for i in ids:
            line = self.gold.doc.lines[i]
            current = self.gold.doc.lines[i]
            pieces = line.pieces
            if self.mode == "changed" and i not in (1, 15):
                continue
            if len(pieces) == 1:
                rows.append(f"{i + 1:04d} {pieces[0].label}")
            elif pieces:
                rows.append(f"{i + 1:04d} | " + " ".join(f"[{p.label}]{p.text}" for p in pieces))
            else:
                rows.append(f"{i + 1:04d} {current.empty_label}")
        return "\n".join(rows) or "NO CHANGES"


@unittest.skipUnless(ZONES_AVAILABLE, "needs the editor's zone_lines module")
class TestPages(unittest.TestCase):
    def setUp(self):
        self.zdoc = read_zone_doc(XML, LAYOUT)

    def test_pages_are_units(self):
        self.assertEqual([u.index for u in self.zdoc.units], [0, 1, 2, 3])
        self.assertIn("0011 {top} | [page]2 [note:headnote]Doe and Roe, A Sample Article", self.zdoc.units[2].xml)
        self.assertEqual([u.fingerprint for u in self.zdoc.units], [p.fingerprint for p in self.zdoc.pages])

    def test_prompt_lists_window_with_context(self):
        prompt = build_user_prompt(RULES, self.zdoc, self.zdoc.pages[1:2])
        self.assertIn("## Zone labels", prompt)
        self.assertIn("=== Context before (do not change) ===", prompt)
        self.assertIn("=== Page 2 ===", prompt)
        self.assertNotIn("=== Page 3 ===", prompt)

    def test_batch_spec_pages(self):
        self.assertEqual(BatchSpec.parse("pages:4").label, "pages:4")
        self.assertTrue(BatchSpec.parse("pages:all").whole)
        self.assertEqual(BatchSpec.parse("units:all").label, "units:all")
        with self.assertRaises(ValueError):
            BatchSpec.parse("chars:all")


@unittest.skipUnless(ZONES_AVAILABLE, "needs the editor's zone_lines module")
class TestJudge(unittest.TestCase):
    def setUp(self):
        self.zdoc = read_zone_doc(XML, LAYOUT)
        self.window = self.zdoc.pages[2:4]

    def statuses(self, raw: str, mode: str = "all") -> list[str]:
        return [j.status.value for j in judge_answer(self.zdoc, self.window, raw, mode)]  # type: ignore[arg-type]

    def test_complete_answer(self):
        raw = "0011 | [page]2 [note:headnote]Doe and Roe, A Sample Article\n0012-0013 body\n0014-0016 listBibl\n0017 note:headnote\n0018 body"
        judgements = judge_answer(self.zdoc, self.window, raw, "all")
        self.assertEqual([j.status for j in judgements], [ProposalStatus.OK, ProposalStatus.OK])
        self.assertEqual([c.format(4) for c in judgements[0].changes], ["0016 listBibl"])
        self.assertEqual(judgements[1].changes, [])

    def test_range_across_pages_is_cut(self):
        raw = "0011 | [page]2 [note:headnote]Doe and Roe, A Sample Article\n0012-0018 body"
        judgements = judge_answer(self.zdoc, self.window, raw, "all")
        self.assertEqual([c.format(4) for c in judgements[0].changes], ["0014-0016 body"])
        self.assertEqual([c.format(4) for c in judgements[1].changes], ["0017 body"])

    def test_left_out_lines(self):
        self.assertEqual(self.statuses("0011 | [page]2 [note:headnote]Doe and Roe, A Sample Article\n0017-0018 body"), ["bad_labels", "ok"])

    def test_unknown_label_and_changed_text(self):
        self.assertEqual(self.statuses("0012 sidebar\n0017 note:headnote", "changed"), ["bad_labels", "unchanged"])
        self.assertEqual(self.statuses("0011 | [page]3 [note:headnote]Doe and Roe, A Sample Article", "changed"), ["text_changed", "unchanged"])

    def test_changed_mode(self):
        self.assertEqual(self.statuses("NO CHANGES", "changed"), ["unchanged", "unchanged"])
        self.assertEqual(self.statuses("Sorry, I cannot help.", "changed"), ["bad_labels", "bad_labels"])
        # changes of context lines are ignored
        self.assertEqual(self.statuses("0005 body", "changed"), ["unchanged", "unchanged"])


@unittest.skipUnless(ZONES_AVAILABLE, "needs the editor's zone_lines module")
class TestCorrection(unittest.TestCase):
    def setUp(self):
        self.zdoc = read_zone_doc(XML, LAYOUT)
        self.gold = gold_xml()

    def test_oracle_corrects_and_changes_apply(self):
        client = OracleClient(self.gold)
        run = asyncio.run(correct_pages(client, RULES, self.zdoc, self.zdoc.units, BatchSpec.parse("pages:2")))
        self.assertEqual(client.windows, [list(range(0, 10)), list(range(10, 18))])
        self.assertEqual([o.status for o in run.outcomes], [ProposalStatus.OK] * 4)
        proposals = {o.unit.index: o.proposal or "" for o in run.outcomes}
        self.assertEqual(proposals, {0: "0002 front", 1: "", 2: "0016 listBibl", 3: ""})
        xml, problems = apply_page_changes(self.zdoc, {i: p for i, p in proposals.items() if p})
        self.assertEqual(problems, [])
        self.assertEqual(xml, self.gold)
        self.assertIn("<listBibl>Smith, J. (2020). A book. <lb/>Miller, K. (2021). Another book. <lb/>stray text outside <lb/></listBibl>", xml)

    def test_failed_window_is_split_by_pages_then_lines(self):
        client = OracleClient(self.gold, garbage_above=6)
        run = asyncio.run(correct_pages(client, RULES, self.zdoc, self.zdoc.units, BatchSpec.parse("pages:all"), split_on_failure=0.25))
        self.assertEqual(client.windows[0], list(range(18)))
        # page 2 (8 lines) is split into two parts of 4 lines, page 3 (6 lines) is answered
        self.assertIn([2, 3, 4, 5], client.windows)
        self.assertIn([6, 7, 8, 9], client.windows)
        self.assertEqual([o.status for o in run.outcomes], [ProposalStatus.OK] * 4)
        self.assertEqual(run.outcomes[2].proposal, "0016 listBibl")

    def test_apply_refuses_invalid_changes(self):
        _, problems = apply_page_changes(self.zdoc, {0: "0011 body"})
        self.assertTrue(problems)
        _, problems = apply_page_changes(self.zdoc, {9: "0001 body"})
        self.assertEqual(problems, ["No page with index 9"])


@unittest.skipUnless(ZONES_AVAILABLE, "needs the editor's zone_lines module")
class TestEvaluation(unittest.TestCase):
    def source(self) -> EvalSource:
        return EvalSource(
            reference_stable_id="gold", input_stable_id="input", variant="grobid.training.segmentation", unit_path="pages",
            gold_xml=gold_xml(), input_xml=XML, rules=RULES, zones=LAYOUT,
        )

    def test_scores(self):
        configs = sweep([BatchSpec.parse("pages:2")], ["all", "changed"], 1)
        result = asyncio.run(evaluate(OracleClient(gold_xml()), "oracle", self.source(), configs))
        baseline = result.baseline
        assert baseline is not None
        self.assertEqual(baseline.exact_rate, 0.5)
        self.assertEqual(baseline.edit_recall, 0.0)
        self.assertLess(baseline.label_accuracy or 0, 1.0)
        self.assertEqual(result.meta["zones"], True)
        for summary in result.summaries:
            self.assertEqual(summary.exact_rate, 1.0)
            self.assertEqual(summary.label_accuracy, 1.0)
            self.assertEqual((summary.edit_precision, summary.edit_recall), (1.0, 1.0))
            self.assertEqual(summary.field_f1, 1.0)
        report = markdown_report([result])
        self.assertIn("Label acc. %", report)
        self.assertIn("Boundary F1", report)
        self.assertIn("4 pages", report)

    def test_wrong_edit(self):
        class Wrong:
            async def complete(self, system_prompt: str, user_prompt: str) -> str:
                return "0016 body" if "=== Page 3 ===" in user_prompt else "NO CHANGES"

        configs = sweep([BatchSpec.parse("pages:1")], ["changed"], 1)
        result = asyncio.run(evaluate(Wrong(), "wrong", self.source(), configs))
        summary = result.summaries[0]
        self.assertEqual(summary.edit_precision, 0.0)
        self.assertEqual(summary.edit_recall, 0.0)
        self.assertEqual(summary.exact_rate, 0.5)


@unittest.skipUnless(ZONES_AVAILABLE, "needs the editor's zone_lines module")
class TestSources(unittest.TestCase):
    """The zone labels and the layout of a zone document (server side)."""

    def setUp(self):
        from pte_plugin_auto_annotation import sources

        self.sources = sources
        self.registry = mock.Mock()
        self.registry.list_extractors.return_value = [{"id": "x"}, {"id": "grobid", "zoneLabels": {"seg": _LAYOUT["labels"]}}]

    def test_layout(self):
        layout = mock.Mock(pages=[0, 1], hints=[["top"], []])
        with mock.patch.object(self.sources, "get_line_layout", side_effect=[None, layout]) as get:
            zones = self.sources.zone_layout(["<a/>", "<b/>"], "doc1", "seg", self.registry)
        assert zones is not None
        self.assertEqual((zones.line_pages, zones.hints, zones.labels[0]["code"]), ([0, 1], [["top"], []], "titlePage"))
        self.assertEqual(get.call_args_list[1].args, ("<b/>", "doc1", "seg"))
        self.assertIsNone(self.sources.zone_layout(["<a/>"], "doc1", "other", self.registry))

    def test_missing_layout(self):
        with mock.patch.object(self.sources, "get_line_layout", return_value=None):
            with self.assertRaises(self.sources.SourceError) as caught:
                self.sources.zone_layout(["<a/>"], "doc1", "seg", self.registry)
        self.assertIn("layout information", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
