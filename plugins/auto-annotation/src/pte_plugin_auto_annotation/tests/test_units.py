"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/units.py
"""

import unittest

from pte_plugin_auto_annotation.units import (
    PathStep,
    document_units,
    parse_unit,
    parse_unit_path,
    serialize_unit,
)

from .fixtures import GOLD_UNITS, INPUT_UNITS, INPUT_XML, tei


class TestUnitPath(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_unit_path(".//listBibl/bibl"), [PathStep("listBibl", True), PathStep("bibl", False)])
        self.assertEqual(parse_unit_path("./*"), [PathStep("*", False)])
        self.assertEqual(parse_unit_path("tei:bibl"), [PathStep("bibl", False)])
        self.assertIsNone(parse_unit_path("auto"))
        self.assertIsNone(parse_unit_path(None))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_unit_path(".//bibl[1]")


class TestDocumentUnits(unittest.TestCase):
    def test_finds_bibls_and_serializes_without_namespace(self):
        units = document_units(INPUT_XML, ".//listBibl/bibl")
        self.assertEqual([u.xml for u in units], INPUT_UNITS)
        self.assertEqual([u.index for u in units], [0, 1, 2, 3])

    def test_auto_path_finds_the_same_units(self):
        self.assertEqual([u.xml for u in document_units(INPUT_XML, None)], INPUT_UNITS)

    def test_fingerprint_ignores_pretty_printing(self):
        compact = document_units(tei(GOLD_UNITS), ".//listBibl/bibl")
        pretty = document_units(tei(GOLD_UNITS, pretty=True), ".//listBibl/bibl")
        self.assertEqual([u.fingerprint for u in compact], [u.fingerprint for u in pretty])

    def test_fingerprint_changes_with_markup_and_numbers_duplicates(self):
        units = document_units(tei(["<bibl>Ibid.</bibl>", "<bibl>Ibid.</bibl>", "<bibl><title>Ibid.</title></bibl>"]), ".//bibl")
        self.assertEqual(units[1].fingerprint, units[0].fingerprint + ".2")
        self.assertNotEqual(units[2].fingerprint.split(".")[0], units[0].fingerprint)

    def test_known_fingerprint(self):
        # sha256('<bibl>Ibid.</bibl>')[:8], the same as the editor's canonicalForm/fingerprintOf
        import hashlib
        units = document_units(tei(["<bibl>\n  Ibid.\n</bibl>"]), ".//bibl")
        self.assertEqual(units[0].fingerprint, hashlib.sha256(b"<bibl>Ibid.</bibl>").hexdigest()[:8])

    def test_malformed_document(self):
        with self.assertRaises(ValueError):
            document_units("<TEI><text>", None)


class TestParseUnit(unittest.TestCase):
    def test_round_trip(self):
        content = parse_unit(INPUT_UNITS[1])
        self.assertEqual(len(content), 1)
        element = content[0]
        assert not isinstance(element, str)
        self.assertEqual(serialize_unit(element), INPUT_UNITS[1])

    def test_text_outside_element(self):
        self.assertEqual(parse_unit("x <bibl/>")[0], "x")


if __name__ == "__main__":
    unittest.main()
