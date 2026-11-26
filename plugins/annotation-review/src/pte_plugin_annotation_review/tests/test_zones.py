"""
Unit tests for reviewing zone documents by page window.

@testCovers plugins/annotation-review/src/pte_plugin_annotation_review/zones.py
@testCovers plugins/annotation-review/src/pte_plugin_annotation_review/review_logic.py
@testCovers plugins/annotation-review/src/pte_plugin_annotation_review/routes.py
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from lxml import etree

from fastapi_app.lib.core.database import DatabaseManager
from fastapi_app.lib.core.dependencies import get_document_rules_store, get_file_repository, require_authenticated_user
from fastapi_app.lib.doc_rules.storage import DocumentRulesStore
from fastapi_app.lib.llm import LLMModel, LLMProvider, LLMProviderRegistry
import pytest

try:
    from fastapi_app.plugin_api import LineLayout, register_layout_provider, unregister_layout_provider, zone_lines
except ImportError:  # editor before plugin API 1.5
    pytest.skip("needs the editor's layout API (plugin API 1.5)", allow_module_level=True)

from pte_plugin_annotation_review import routes as routes_module
from pte_plugin_annotation_review import zones
from pte_plugin_annotation_review.review_logic import chunk_range, zone_source
from pte_plugin_annotation_review.routes import router

VARIANT = "grobid.training.segmentation"
SAMPLE = (Path(__file__).parent / "segmentation-sample.tei.xml").read_text(encoding="utf-8")
RULES = """<editorialDecl>
        <interpretation type="primary">
          <p><ref target="https://raw.example.org/rules.md" subtype="machine" type="markdown"/></p>
        </interpretation>
      </editorialDecl>
    </encodingDesc>"""
DOC = SAMPLE.replace("    </encodingDesc>", RULES, 1)
LABELS = [
    {"code": "titlePage", "element": "titlePage"}, {"code": "front", "element": "front"}, {"code": "body", "element": "body"},
    {"code": "listBibl", "element": "listBibl"}, {"code": "page", "element": "page"},
    {"code": "note:headnote", "element": "note", "attributes": {"place": "headnote"}},
    {"code": "note:footnote", "element": "note", "attributes": {"place": "footnote"}},
]
PAGES = [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3]


class ZoneTestCase(unittest.TestCase):
    def setUp(self):
        register_layout_provider(VARIANT, "test", lambda xml, doc_id: LineLayout(PAGES) if doc_id == "doc1" else None)
        self.addCleanup(unregister_layout_provider, VARIANT)
        patcher = mock.patch.object(zones, "get_zone_labels", return_value=zone_lines.zone_labels(LABELS))
        patcher.start()
        self.addCleanup(patcher.stop)


class TestWindows(ZoneTestCase):
    def test_page_windows(self):
        source = zone_source(DOC, "doc1")
        assert source is not None
        self.assertEqual([[p.number for p in w] for w in source.windows], [[1, 2], [3, 4]])

    def test_scope_reviews_the_pages_of_the_zone(self):
        scope = '<note place="headnote">Doe and Roe, A Sample Article <lb/></note>'
        source = zone_source(DOC, "doc1", scope)
        assert source is not None
        self.assertEqual([[p.number for p in w] for w in source.windows], [[3]])

    def test_scope_of_a_page_reviews_that_page(self):
        # the editor's range of a <pb/> milestone: up to the next <pb/>, without the whitespace before it
        doc, _ = zone_lines.insert_page_breaks(DOC, zone_lines.zone_labels(LABELS), PAGES)
        start = doc.index('<pb n="3"/>')
        scope = doc[start:doc.index('<pb n="4"/>')].rstrip()
        source = zone_source(doc, "doc1", scope)
        assert source is not None
        self.assertEqual([[p.number for p in w] for w in source.windows], [[3]])

    def test_scope_outside_text_is_reviewed_as_xml(self):
        self.assertIsNone(zone_source(DOC, "doc1", '<label type="flavor">default</label>'))

    def test_missing_layout(self):
        with self.assertRaises(zones.MissingLayoutError):
            zone_source(DOC, "other")

    def test_other_documents_are_not_zone_documents(self):
        with mock.patch.object(zones, "get_zone_labels", return_value=None):
            self.assertIsNone(zone_source(DOC, "doc1"))

    def test_chunk_range_is_the_window_range(self):
        source = zone_source(DOC, "doc1")
        assert source is not None
        position = chunk_range(DOC, 1, zone=source)
        assert position is not None
        start, end = position
        self.assertTrue(DOC[start:end].startswith("2</page>"))
        self.assertTrue(DOC[start:end].endswith("Third page. <lb/>"))


class TestPrompt(ZoneTestCase):
    def test_user_prompt(self):
        source = zone_source(DOC, "doc1")
        assert source is not None
        prompt = zones.build_user_prompt([("primary", "Rules here")], source, source.windows[1], (2, 2))
        self.assertIn("## Rule category: primary\n\nRules here", prompt)
        self.assertIn('note:headnote\t<note place="headnote">', prompt)
        self.assertIn("repeated\t", prompt)
        self.assertIn("## Document lines (part 2 of 2 of the document)", prompt)
        self.assertIn("=== Page 3 ===\n0011 | [page]2 [note:headnote]Doe and Roe, A Sample Article", prompt)
        self.assertNotIn("=== Context after", prompt)


class TestFindings(ZoneTestCase):
    def source(self):
        source = zone_source(DOC, "doc1")
        assert source is not None
        return source

    def apply(self, findings):
        """Each finding applied alone yields a well-formed document."""
        for finding in findings:
            self.assertEqual(DOC.count(finding["old"]), 1)
            etree.fromstring(DOC.replace(finding["old"], finding["new"]).encode("utf-8"))

    def test_changes_become_findings(self):
        source = self.source()
        raw = json.dumps([
            {"lines": "0016", "label": "listBibl", "rationale": "A reference."},
            {"line": "0011", "pieces": "[page]2 [note:headnote]Doe and Roe, [body]A Sample Article", "rationale": "Split."},
            {"lines": "0002-0003", "label": "body", "rationale": "Outside the window."},
            {"lines": "0012", "label": "nonsense", "rationale": "Unknown label."},
        ])
        changes, problems = zones.parse_changes(raw, source, source.windows[1])
        self.assertEqual(len(changes), 2)
        self.assertEqual(len(problems), 2)
        findings = zones.findings_from_changes(source, changes)
        self.assertEqual([f["rationale"] for f in findings], ["Split.", "A reference."])
        self.apply(findings)
        self.assertIn("stray text outside <lb/></listBibl>", findings[1]["new"])

    def test_touching_changes_are_one_finding(self):
        source = self.source()
        raw = json.dumps([
            {"lines": "0012-0013", "label": "listBibl", "rationale": "References."},
            {"lines": "0014", "label": "body", "rationale": "Body text."},
        ])
        changes, _ = zones.parse_changes(raw, source, source.windows[1])
        findings = zones.findings_from_changes(source, changes)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["rationale"], "References.; Body text.")
        self.apply(findings)

    def test_old_is_unique(self):
        source = self.source()
        # the headnote of page 4 is identical to the one of page 3
        changes, _ = zones.parse_changes(json.dumps([{"lines": "0017", "label": "body", "rationale": "x"}]), source, source.windows[1])
        findings = zones.findings_from_changes(source, changes)
        self.assertEqual(len(findings), 1)
        # the headnote alone occurs twice; with the zone it joins, the span is unique
        self.assertIn("Third page.", findings[0]["old"])
        self.apply(findings)


    def test_old_is_widened_until_unique(self):
        source = self.source()
        changes, _ = zones.parse_changes(json.dumps([{"lines": "0017", "label": "note:footnote", "rationale": "x"}]), source, source.windows[1])
        findings = zones.findings_from_changes(source, changes)
        self.assertEqual(len(findings), 1)
        # the headnote zone alone occurs twice in the document
        self.assertGreater(len(findings[0]["old"]), len('<note place="headnote">Doe and Roe, A Sample Article <lb/></note>'))
        self.apply(findings)


class _Provider(LLMProvider):
    def __init__(self, answer: str):
        self.id = "stub"
        self.label = "Stub"
        self.answer = answer

    def list_models(self) -> list[LLMModel]:
        return []

    async def chat_completion(self, model_id, system_prompt, user_prompt, temperature=0.2) -> str:
        return self.answer


class TestRoutes(ZoneTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(routes_module, "check_model_access")
        patcher.start()
        self.addCleanup(patcher.stop)
        for target in ("pte_plugin_annotation_review.review_logic._is_safe_fetch_url", "pte_plugin_annotation_review.review_logic.fetch_rule_excerpt"):
            patcher = mock.patch(target, return_value=True if target.endswith("url") else "Rules.")
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(routes_module, "check_file_access", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        store = DocumentRulesStore(DatabaseManager(Path(self.temp_dir.name) / "test.db"))
        repo = mock.MagicMock()
        repo.get_file_by_stable_id.side_effect = lambda sid: SimpleNamespace(doc_id="doc1" if sid == "s1" else "nolayout")
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_authenticated_user] = lambda: {"username": "testuser", "roles": ["admin"]}
        app.dependency_overrides[get_document_rules_store] = lambda: store
        app.dependency_overrides[get_file_repository] = lambda: repo
        self.client = TestClient(app)
        LLMProviderRegistry.reset_instance()
        LLMProviderRegistry.get_instance().register(_Provider(json.dumps([{"lines": "0016", "label": "listBibl", "rationale": "A reference."}])))
        self.addCleanup(LLMProviderRegistry.reset_instance)

    def test_plan(self):
        response = self.client.post("/api/plugins/annotation-review/plan", json={"xml": DOC, "stable_id": "s1"})
        self.assertEqual(response.json(), {"chunk_count": 2, "mode": "pages"})

    def test_plan_without_layout(self):
        response = self.client.post("/api/plugins/annotation-review/plan", json={"xml": DOC, "stable_id": "s2"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("layout information", response.json()["detail"])

    def test_review(self):
        response = self.client.post(
            "/api/plugins/annotation-review/review",
            json={"xml": DOC, "stable_id": "s1", "provider_id": "stub", "model_id": "m", "chunk_index": 1},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["chunk_count"], 2)
        self.assertEqual(len(body["findings"]), 1)
        self.assertTrue(DOC[body["chunk_start"]:body["chunk_end"]].startswith("2</page>"))


if __name__ == "__main__":
    unittest.main()
