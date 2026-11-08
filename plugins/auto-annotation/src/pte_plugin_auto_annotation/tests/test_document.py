"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/document.py
"""

import asyncio
import unittest

from lxml import etree

from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.checks import ProposalStatus
from pte_plugin_auto_annotation.document import iso_duration, replace_units, write_result
from pte_plugin_auto_annotation.pipeline import PipelineConfig, PipelineResult, annotate_units
from pte_plugin_auto_annotation.units import TEI_NS, document_units, parse_document

from .fixtures import GOLD_UNITS, INPUT_UNITS, INPUT_XML, ArbiterOracle, OracleClient, gold_mapping, tei

PATH = ".//listBibl/bibl"
NS = {"t": TEI_NS}
MODELS = {"A": "p/a", "B": "p/b", "arbiter": "p/c"}
WHEN = "2026-11-06T07:00:00Z"


def _result(xml: str = INPUT_XML) -> PipelineResult:
    units = document_units(xml, PATH)
    mapping = {u.xml: g for u, g in zip(units, GOLD_UNITS)}
    mapping_b = dict(mapping)
    mapping_b[units[1].xml] = units[1].xml.replace("<title>", '<title level="a">')
    config = PipelineConfig(spec=BatchSpec(max_units=4), retry=None)
    return asyncio.run(annotate_units(units, [("r", "R")], OracleClient(mapping), OracleClient(mapping_b), ArbiterOracle(mapping), config))


class TestReplaceUnits(unittest.TestCase):
    def test_replaces_only_the_given_units_without_namespace_noise(self):
        root = parse_document(INPUT_XML)
        replace_units(root, PATH, {2: GOLD_UNITS[2]})
        out = etree.tostring(root, encoding="unicode")
        self.assertEqual(out.count("xmlns="), 1)
        self.assertEqual([u.xml for u in document_units(out, PATH)], [INPUT_UNITS[0], INPUT_UNITS[1], GOLD_UNITS[2], INPUT_UNITS[3]])

    def test_keeps_the_tail_whitespace(self):
        xml = tei(INPUT_UNITS, pretty=True)
        root = parse_document(xml)
        replace_units(root, PATH, {0: GOLD_UNITS[0]})
        self.assertIn(GOLD_UNITS[0] + "\n        " + INPUT_UNITS[1], etree.tostring(root, encoding="unicode"))

    def test_errors(self):
        with self.assertRaises(ValueError):
            replace_units(parse_document(INPUT_XML), PATH, {9: GOLD_UNITS[0]})
        with self.assertRaises(ValueError):
            replace_units(parse_document(INPUT_XML), PATH, {0: "<bibl/><bibl/>"})


class TestWriteResult(unittest.TestCase):
    def test_units_and_change(self):
        out = write_result(INPUT_XML, PATH, _result(), MODELS, WHEN, 252.4)
        self.assertTrue(out.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<TEI'))
        self.assertEqual([u.xml for u in document_units(out, PATH)], GOLD_UNITS)
        root = parse_document(out)

        change = root.findall("t:teiHeader/t:revisionDesc/t:change", NS)[-1]
        self.assertEqual((change.get("who"), change.get("status"), change.get("when")), ("#auto-annotation", "extraction", WHEN))
        desc = change.find("t:desc", NS)
        assert desc is not None
        text = "".join(desc.itertext())
        self.assertIn("A: p/a, B: p/b, arbiter: p/c", text)
        self.assertIn("3 agreed, 1 arbitrated, 0 unresolved, 4 changed.", text)
        self.assertIn("Arbiter chose A 1, B 0, own 0, original 0; with valid proposals from both models: A 1, B 0, own 0, original 0.", text)
        self.assertIn("Invalid proposals: A 0, B 0.", text)
        self.assertTrue(text.endswith("Completed in 4 min 12 s."))
        self.assertEqual(change.findtext("t:note[@type='label']", namespaces=NS), "Auto-annotation")
        self.assertIsNotNone(root.find(".//t:titleStmt/t:respStmt/t:persName[@{http://www.w3.org/XML/1998/namespace}id='auto-annotation']", NS))

        measures = [(m.get("type"), m.get("subtype"), m.get("commodity"), m.get("quantity")) for m in desc.findall("t:measure", NS)]
        self.assertEqual(measures, [
            ("arbiter-choice", "A", "p/a", "1"), ("arbiter-choice", "B", "p/b", "0"), ("arbiter-choice", "own", "p/c", "0"), ("arbiter-choice", "original", None, "0"),
            ("arbiter-choice-both-valid", "A", "p/a", "1"), ("arbiter-choice-both-valid", "B", "p/b", "0"), ("arbiter-choice-both-valid", "own", "p/c", "0"), ("arbiter-choice-both-valid", "original", None, "0"),
            ("invalid-proposal", "A", "p/a", "0"), ("invalid-proposal", "B", "p/b", "0"),
        ])
        time = desc.find("t:time", NS)
        assert time is not None
        self.assertEqual((time.get("dur"), time.text), ("PT4M12S", "4 min 12 s"))
        self.assertEqual(out.count("xmlns="), 1)
        self.assertEqual([etree.QName(c).localname for c in root], ["teiHeader", "text"])
        self.assertNotIn("unit-review", out)

    def test_without_arbiter_there_are_no_arbiter_choices(self):
        models = {"A": "p/a", "B": "p/b"}
        out = write_result(INPUT_XML, PATH, _result(), models, WHEN, 3)
        self.assertNotIn("arbiter-choice", out)
        self.assertIn("Invalid proposals:", out)
        self.assertIn('<time dur="PT3S">3 s</time>', out)

    def test_a_choice_with_an_invalid_opponent_is_not_counted_as_both_valid(self):
        result = _result()
        arbitrated = next(d for d in result.decisions if d.chosen == "A")
        arbitrated.statuses["B"] = ProposalStatus.TEXT_CHANGED
        root = parse_document(write_result(INPUT_XML, PATH, result, MODELS, WHEN, 1))
        desc = root.findall("t:teiHeader/t:revisionDesc/t:change/t:desc", NS)[-1]
        quantity = lambda type_: desc.find(f"t:measure[@type='{type_}'][@subtype='A']", NS).get("quantity")  # type: ignore[union-attr]
        self.assertEqual((quantity("arbiter-choice"), quantity("arbiter-choice-both-valid")), ("1", "0"))

    def test_invalid_proposals_are_counted_per_model(self):
        result = _result()
        result.decisions[0].statuses["A"] = ProposalStatus.TEXT_CHANGED
        result.decisions[1].statuses["B"] = ProposalStatus.ERROR  # a failed request is no proposal to judge
        root = parse_document(write_result(INPUT_XML, PATH, result, MODELS, WHEN, 1))
        invalid = root.findall("t:teiHeader/t:revisionDesc/t:change/t:desc/t:measure[@type='invalid-proposal']", NS)
        self.assertEqual([m.get("quantity") for m in invalid], ["1", "0"])

    def test_with_an_existing_revision_desc(self):
        xml = tei(INPUT_UNITS, changes='\n      <change when="2026-01-01" status="extraction" who="#grobid"/>\n    ')
        out = write_result(xml, PATH, _result(xml), MODELS, WHEN, 1, status="draft", label=None)
        self.assertIn('who="#grobid"/>\n      <change when="2026-11-06T07:00:00Z" status="draft" who="#auto-annotation">', out)
        self.assertIn("</change>\n    </revisionDesc>\n  </teiHeader>", out)

    def test_existing_stand_off_is_kept(self):
        standoff = '  <standOff>\n    <listAnnotation type="unit-review"><annotation xml:id="ur-alice"/></listAnnotation>\n  </standOff>\n'
        xml = INPUT_XML.replace("  <text>", standoff + "  <text>")
        self.assertIn('xml:id="ur-alice"', write_result(xml, PATH, _result(xml), MODELS, WHEN, 1))

    def test_iso_duration(self):
        for seconds, expected in ((0, ("PT0S", "0 s")), (59.6, ("PT1M", "1 min")), (252, ("PT4M12S", "4 min 12 s")), (3723, ("PT1H2M3S", "1 h 2 min 3 s")), (3600, ("PT1H", "1 h"))):
            self.assertEqual(iso_duration(seconds), expected)

    def test_document_without_file_desc(self):
        xml = INPUT_XML.replace("    <fileDesc>\n      <titleStmt>\n        <title>Sample</title>\n      </titleStmt>\n    </fileDesc>\n", "")
        out = write_result(xml, PATH, _result(xml), MODELS, WHEN, 1)
        self.assertIn('who="#auto-annotation"', out)
        self.assertNotIn("respStmt><persName", out)


if __name__ == "__main__":
    unittest.main()
