"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/document.py
"""

import asyncio
import unittest

from lxml import etree

from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.document import REVIEWER_ID, replace_units, write_result
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
    def test_units_change_and_marks(self):
        out = write_result(INPUT_XML, PATH, _result(), MODELS, WHEN)
        self.assertTrue(out.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<TEI'))
        units = document_units(out, PATH)
        self.assertEqual([u.xml for u in units], GOLD_UNITS)
        root = parse_document(out)

        change = root.findall("t:teiHeader/t:revisionDesc/t:change", NS)[-1]
        self.assertEqual((change.get("who"), change.get("status"), change.get("when")), ("#auto-annotation", "extraction", WHEN))
        desc = change.findtext("t:desc", namespaces=NS) or ""
        self.assertIn("A: p/a, B: p/b, arbiter: p/c", desc)
        self.assertIn("3 agreed, 1 arbitrated, 0 unresolved, 4 changed", desc)
        self.assertEqual(change.findtext("t:note[@type='label']", namespaces=NS), "Auto-annotation")
        self.assertIsNotNone(root.find(".//t:titleStmt/t:respStmt/t:persName[@{http://www.w3.org/XML/1998/namespace}id='auto-annotation']", NS))

        children = [etree.QName(c).localname for c in root]
        self.assertEqual(children, ["teiHeader", "standOff", "text"])
        review = root.find("t:standOff/t:listAnnotation[@type='unit-review']", NS)
        assert review is not None
        self.assertEqual(review.findtext("t:note[@type='unit-xpath']", namespaces=NS), PATH)
        self.assertEqual(review.findtext("t:note[@type='summary']", namespaces=NS), "units=4 ok=3 not-ok=1 disputed=0 open=0")
        annotation = review.find("t:annotation", NS)
        assert annotation is not None
        self.assertEqual(annotation.get("{http://www.w3.org/XML/1998/namespace}id"), f"ur-{REVIEWER_ID}")
        self.assertIsNone(annotation.get("resp"))  # LLM reviewers have no @resp
        ok = (annotation.findtext("t:note[@type='ok']", namespaces=NS) or "").split()
        self.assertEqual(ok, [units[0].fingerprint, units[2].fingerprint, units[3].fingerprint])
        not_ok = annotation.find("t:note[@type='not-ok']", NS)
        assert not_ok is not None
        self.assertEqual(not_ok.get("n"), units[1].fingerprint)
        self.assertIn("arbiter chose A", not_ok.text or "")

    def test_without_marks_and_with_an_existing_revision_desc(self):
        xml = tei(INPUT_UNITS, changes='\n      <change when="2026-01-01" status="extraction" who="#grobid"/>\n    ')
        out = write_result(xml, PATH, _result(xml), MODELS, WHEN, status="draft", label=None, with_marks=False)
        self.assertNotIn("standOff", out)
        self.assertIn('who="#grobid"/>\n      <change when="2026-11-06T07:00:00Z" status="draft" who="#auto-annotation">', out)
        self.assertIn("</change>\n    </revisionDesc>\n  </teiHeader>", out)

    def test_existing_review_list_keeps_other_reviewers_and_replaces_ours(self):
        standoff = (
            '  <standOff>\n    <listAnnotation type="unit-review" subtype="fp-sha256-8-v1">\n'
            '      <note type="summary">units=4 ok=1 not-ok=0 disputed=0 open=3</note>\n'
            '      <annotation xml:id="ur-alice" motivation="assessing" target="#xpath(//text)" resp="#alice"><note type="ok">abc</note></annotation>\n'
            f'      <annotation xml:id="ur-{REVIEWER_ID}" motivation="assessing" target="#xpath(//text)"><note type="ok">old</note></annotation>\n'
            "    </listAnnotation>\n  </standOff>\n"
        )
        xml = INPUT_XML.replace("  <text>", standoff + "  <text>")
        root = parse_document(write_result(xml, PATH, _result(xml), MODELS, WHEN))
        annotations = root.findall("t:standOff/t:listAnnotation/t:annotation", NS)
        self.assertEqual([a.get("{http://www.w3.org/XML/1998/namespace}id") for a in annotations], ["ur-alice", f"ur-{REVIEWER_ID}"])
        self.assertNotIn("old", etree.tostring(annotations[1], encoding="unicode"))
        self.assertEqual(len(root.findall("t:standOff/t:listAnnotation", NS)), 1)

    def test_document_without_file_desc(self):
        xml = INPUT_XML.replace("    <fileDesc>\n      <titleStmt>\n        <title>Sample</title>\n      </titleStmt>\n    </fileDesc>\n", "")
        out = write_result(xml, PATH, _result(xml), MODELS, WHEN)
        self.assertIn('who="#auto-annotation"', out)
        self.assertNotIn("respStmt><persName", out)


if __name__ == "__main__":
    unittest.main()
