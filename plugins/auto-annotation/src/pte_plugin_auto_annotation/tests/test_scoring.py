"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/scoring.py
"""

import unittest

from pte_plugin_auto_annotation.scoring import align_documents, f1, score_unit, unit_fields

from .fixtures import GOLD_UNITS, GOLD_XML, INPUT_UNITS, INPUT_XML, tei


class TestFields(unittest.TestCase):
    def test_fields(self):
        fields = unit_fields('<bibl><author xml:id="a1">Doe</author> <title level="m">T</title></bibl>')
        self.assertEqual(set(fields), {("bibl", (), "Doe T"), ("bibl/author", (), "Doe"), ("bibl/title", (("level", "m"),), "T")})


class TestScoreUnit(unittest.TestCase):
    def test_perfect_correction(self):
        s = score_unit(INPUT_UNITS[0], GOLD_UNITS[0], GOLD_UNITS[0])
        self.assertTrue(s.exact)
        self.assertEqual((s.needed, s.made, s.correct_edits), (s.needed, s.needed, s.needed))
        self.assertGreater(s.needed, 0)

    def test_no_proposal_counts_as_input(self):
        s = score_unit(INPUT_UNITS[0], GOLD_UNITS[0], None)
        self.assertEqual((s.made, s.correct_edits), (0, 0))
        self.assertFalse(s.exact)

    def test_partial_and_wrong_edits(self):
        # adds the right title but a wrong date element
        proposal = '<bibl><author>Doe, A.</author> <title level="m">Torts</title>, <idno>1999</idno>.</bibl>'
        s = score_unit(INPUT_UNITS[1], GOLD_UNITS[1], proposal)
        # needed: bibl text unchanged; title attr change (-1 +1), +date -> 3; made: title (-1 +1), +idno -> 3; correct: 2
        self.assertEqual((s.needed, s.made, s.correct_edits), (3, 3, 2))

    def test_f1(self):
        self.assertAlmostEqual(f1(0.5, 1.0) or 0, 2 / 3)
        self.assertIsNone(f1(None, 1.0))


class TestAlignment(unittest.TestCase):
    def test_aligns_by_text_and_counts_unmatched(self):
        gold = tei([GOLD_UNITS[0], "<bibl>Ibid., p. 7.</bibl><bibl>extra</bibl>", GOLD_UNITS[3]])
        alignment = align_documents(INPUT_XML, gold, ".//listBibl/bibl")
        self.assertEqual([p.input.index for p in alignment.pairs], [0, 2, 3])
        self.assertEqual((alignment.unmatched_input, alignment.unmatched_gold), (1, 1))

    def test_full_alignment(self):
        alignment = align_documents(INPUT_XML, GOLD_XML, ".//listBibl/bibl")
        self.assertEqual(len(alignment.pairs), 4)


if __name__ == "__main__":
    unittest.main()
