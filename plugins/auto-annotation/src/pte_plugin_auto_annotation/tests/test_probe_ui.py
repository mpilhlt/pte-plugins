"""
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/probe_store.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/probe_job.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/probe_compare.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/probe_views.py
@testCovers plugins/auto-annotation/src/pte_plugin_auto_annotation/probe_routes.py
"""

import asyncio
import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_app.lib.core.dependencies import get_file_repository, get_file_storage, require_reviewer_or_admin
from fastapi_app.lib.llm import LLMModel, LLMProvider, LLMProviderRegistry
from pte_plugin_auto_annotation import models as models_module
from pte_plugin_auto_annotation import probe_routes
from pte_plugin_auto_annotation import sources as sources_module
from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.evaluation import EvalSource, sweep
from pte_plugin_auto_annotation.probe_compare import latest_complete_per_reference, pooled_summaries, runs_of_model, runs_on_reference
from pte_plugin_auto_annotation.probe_job import run_job
from pte_plugin_auto_annotation.probe_store import STALE_SECONDS, ProbeStatus, ProbeStore, UnknownRunError
from pte_plugin_auto_annotation.routes import router

from .fixtures import GOLD_XML, INPUT_XML, EchoClient, FailingClient, OracleClient, gold_mapping

SOURCE = EvalSource(
    reference_stable_id="gold1", input_stable_id="v1", variant="grobid.training.references",
    unit_path=".//listBibl/bibl", gold_xml=GOLD_XML, input_xml=INPUT_XML, rules=[("references", "rules")], doc_id="doc1",
)
PREFIX = "/api/plugins/auto-annotation/probe"


def _meta(model_id: str = "oracle", reference: str = "gold1", user: str = "rev", prompt_version: int = 1) -> dict[str, object]:
    return {"username": user, "provider_id": "mock", "model_id": model_id, "model_label": model_id.title(), "doc_id": "doc1",
            "doc_label": "A document", "reference_stable_id": reference, "reference_is_gold": True, "input_stable_id": "v1",
            "input_choice": "earliest", "aligned_units": 4, "prompt_version": prompt_version, "rules_hash": "abc",
            "params": {"batches": ["units:1", "units:4"], "modes": ["all"], "repeats": 1, "concurrency": 1, "temperature": 0.0}}


def _run(store: ProbeStore, client: object, model_id: str = "oracle", reference: str = "gold1", configs=None) -> str:
    configs = configs or sweep([BatchSpec(max_units=1), BatchSpec(max_units=4)], ["all"], 1)
    run_id = store.new_run_id()
    store.create(run_id, _meta(model_id, reference), ProbeStatus(state="running", planned_runs=len(configs)))
    source = SOURCE if reference == "gold1" else EvalSource(**{**SOURCE.__dict__, "reference_stable_id": reference})
    asyncio.run(run_job(store, run_id, client, f"mock/{model_id}", source, configs, poll_seconds=0.01))  # type: ignore[arg-type]
    return run_id


class SlowClient:
    """Answers after a delay, so that a cancel can arrive in between."""

    def __init__(self, store: ProbeStore, run_id_holder: list[str]):
        self.store = store
        self.holder = run_id_holder
        self.calls = 0

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        if self.calls == 2:
            self.store.request_cancel(self.holder[0])
        await asyncio.sleep(0.05)
        return await EchoClient().complete(system_prompt, user_prompt)


class TestStoreAndJob(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProbeStore(Path(self.tmp.name) / "probe-runs")

    def test_a_finished_run_keeps_its_results(self):
        run_id = _run(self.store, OracleClient(gold_mapping()))
        status = self.store.read_status(run_id)
        self.assertEqual((status.state, status.finished_runs, status.planned_runs, status.error), ("done", 2, 2, None))
        self.assertIsNotNone(status.finished_at)
        summary = self.store.read_summary(run_id)
        self.assertEqual([s["edit_f1"] for s in summary[0]["summaries"]], [1.0, 1.0])
        names = zipfile.ZipFile(io.BytesIO(self.store.zip_results(run_id))).namelist()
        self.assertEqual(sorted(names), sorted(f"{run_id}/{n}" for n in ("report.md", "summary.json", "units.csv", "batches.jsonl", "meta.json")))
        self.assertEqual([r.run_id for r in self.store.list_runs()], [run_id])

    def test_a_run_whose_requests_all_fail_stops(self):
        run_id = _run(self.store, FailingClient())
        status = self.store.read_status(run_id)
        self.assertEqual((status.state, status.finished_runs), ("failed", 1))
        self.assertIn("provider down", status.error or "")
        self.assertEqual(len(self.store.read_summary(run_id)[0]["summaries"]), 1)

    def test_cancel_keeps_the_finished_runs(self):
        holder: list[str] = []
        configs = sweep([BatchSpec(max_units=4), BatchSpec(max_units=1)], ["all"], 1)
        run_id = self.store.new_run_id()
        holder.append(run_id)
        self.store.create(run_id, _meta(), ProbeStatus(state="running", planned_runs=2))
        client = SlowClient(self.store, holder)
        asyncio.run(run_job(self.store, run_id, client, "mock/echo", SOURCE, configs, poll_seconds=0.01))
        status = self.store.read_status(run_id)
        self.assertEqual((status.state, status.finished_runs), ("cancelled", 1))
        self.assertLess(client.calls, 5)

    def test_a_run_without_heartbeat_is_interrupted(self):
        now = [time.time()]
        store = ProbeStore(self.store.root, clock=lambda: now[0])
        run_id = store.new_run_id()
        store.create(run_id, _meta(), ProbeStatus(state="running", planned_runs=2))
        self.assertEqual(store.read_status(run_id).state, "running")
        now[0] += STALE_SECONDS + 1
        self.assertEqual(store.read_status(run_id).state, "interrupted")

    def test_run_ids_are_checked(self):
        with self.assertRaises(UnknownRunError):
            self.store.run_dir("../etc")
        run_id = _run(self.store, EchoClient())
        with self.assertRaises(UnknownRunError):
            self.store.result_path(run_id, "status.json")

    def test_comparison(self):
        echo = _run(self.store, EchoClient(), model_id="echo")
        time.sleep(1.1)  # run ids have a resolution of seconds
        a = _run(self.store, OracleClient(gold_mapping()), model_id="oracle")
        time.sleep(1.1)
        b = _run(self.store, OracleClient(gold_mapping()), model_id="oracle", reference="gold2")
        runs = self.store.list_runs()
        same_model = runs_of_model(self.store, "mock/oracle", runs)
        self.assertEqual([o.run.run_id for o in same_model], [b, a])
        pooled = pooled_summaries(self.store, latest_complete_per_reference(same_model, 1))
        self.assertEqual({(s.batching, s.repeats, s.units) for s in pooled}, {("units:1", 2, 8), ("units:4", 2, 8)})
        on_gold1 = runs_on_reference(self.store, "gold1", runs)
        self.assertEqual([o.run.run_id for o in on_gold1], [a, echo])


class StubProvider(LLMProvider):
    def __init__(self, status: dict[str, str] | None = None):
        self.id = "mock"
        self.label = "Mock"
        self.status = status

    def list_models(self) -> list[LLMModel]:
        return [{"id": "oracle", "label": "Oracle", "capabilities": set(), "status": self.status, "free": True}]  # type: ignore[typeddict-item]

    async def chat_completion(self, model_id, system_prompt, user_prompt, temperature=0.2) -> str:
        return await OracleClient(gold_mapping()).complete(system_prompt, user_prompt)


def _file(stable_id: str, gold: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=f"hash-{stable_id}", stable_id=stable_id, doc_id="doc1", file_type="tei", variant="grobid.training.references",
                           is_gold_standard=gold, version=1, created_at="2026-01-01", parent_stable_id=None)


class FakeRepo:
    files = {"v1": _file("v1"), "gold1": _file("gold1", gold=True)}

    def get_file_by_stable_id(self, stable_id):
        return self.files.get(stable_id)

    def get_files_by_doc_id(self, doc_id):
        return list(self.files.values())

    def get_all_versions(self, doc_id, variant=None):
        return [f for f in self.files.values() if not f.is_gold_standard]


class FakeStorage:
    def read_file(self, file_hash, file_type):
        return {"hash-gold1": GOLD_XML, "hash-v1": INPUT_XML}.get(file_hash, "").encode() or None


class TestProbeRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProbeStore(Path(self.tmp.name) / "probe-runs")
        for target, name, value in (
            (models_module, "check_model_access", None), (sources_module, "check_file_access", True),
            (sources_module, "is_safe_fetch_url", True), (sources_module, "fetch_rule_excerpt", "RULES"),
            (sources_module, "review_unit_path", ".//listBibl/bibl"),
        ):
            patcher = mock.patch.object(target, name, return_value=value)
            self.addCleanup(patcher.stop)
            patcher.start()
        self.started: list[tuple] = []
        patcher = mock.patch.object(probe_routes, "start_job", side_effect=lambda *args: self.started.append(args))
        self.addCleanup(patcher.stop)
        patcher.start()
        self.user = {"username": "rev", "roles": ["reviewer"]}
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_reviewer_or_admin] = lambda: self.user
        app.dependency_overrides[get_file_repository] = FakeRepo
        app.dependency_overrides[get_file_storage] = FakeStorage
        app.dependency_overrides[probe_routes.get_probe_store] = lambda: self.store
        self.client = TestClient(app)
        LLMProviderRegistry.reset_instance()
        self.addCleanup(LLMProviderRegistry.reset_instance)
        self.provider = StubProvider()
        LLMProviderRegistry.get_instance().register(self.provider)

    def start(self, **body):
        return self.client.post(f"{PREFIX}/start", json={"reference_stable_id": "gold1", "provider_id": "mock", "model_id": "oracle",
                                                         "model_label": "Oracle", "doc_label": "A document", "batches": ["units:1", "units:4"], **body})

    def run_started_job(self) -> str:
        store, run_id, client, model, source, configs, concurrency, retry, max_units = self.started[-1]
        asyncio.run(run_job(store, run_id, client, model, source, configs, concurrency, retry, max_units, poll_seconds=0.01))
        return run_id

    def test_start_runs_and_views(self):
        response = self.start(modes=["all", "changed"], max_units=3)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["planned_runs"], body["units"], body["reference_is_gold"], body["input_stable_id"]), (4, 3, True, "v1"))
        meta = self.store.read_meta(body["run_id"])
        self.assertEqual((meta["username"], meta["model_label"], meta["params"]["max_units"]), ("rev", "Oracle", 3))
        self.assertEqual([j["run_id"] for j in self.client.get(f"{PREFIX}/jobs").json()], [body["run_id"]])
        # one running test per user
        self.assertEqual(self.start().status_code, 409)

        run_id = self.run_started_job()
        status = self.client.get(f"{PREFIX}/status", params={"run": run_id}).json()
        self.assertEqual((status["state"], status["finished_runs"], status["model"]), ("done", 4, "mock/oracle"))
        self.assertEqual(self.client.get(f"{PREFIX}/jobs").json(), [])
        page = self.client.get(f"{PREFIX}/view", params={"run": run_id})
        self.assertEqual(page.status_code, 200)
        self.assertIn("Model test: Oracle on A document", page.text)
        self.assertIn("This model has not been tested on other documents yet", page.text)
        self.assertIn('id="delete-run"', page.text)
        history = self.client.get(f"{PREFIX}/history")
        self.assertIn(f"/view?run={run_id}", history.text)
        report = self.client.get(f"{PREFIX}/export", params={"run": run_id, "file": "report.md"})
        self.assertEqual(report.status_code, 200)
        self.assertIn("# Model probe report", report.text)
        self.assertEqual(self.client.get(f"{PREFIX}/export", params={"run": run_id, "file": "status.json"}).status_code, 404)
        self.assertEqual(self.client.get(f"{PREFIX}/export", params={"run": run_id}).headers["content-type"], "application/zip")

        # others see the run, but cannot delete it
        self.user = {"username": "other", "roles": ["reviewer"]}
        self.assertNotIn('id="delete-run"', self.client.get(f"{PREFIX}/view", params={"run": run_id}).text)
        self.assertEqual(self.client.post(f"{PREFIX}/delete", json={"run_id": run_id}).status_code, 403)
        self.user = {"username": "admin", "roles": ["admin"]}
        self.assertEqual(self.client.post(f"{PREFIX}/delete", json={"run_id": run_id}).json(), {"status": "deleted"})
        self.assertEqual(self.client.get(f"{PREFIX}/status", params={"run": run_id}).status_code, 404)

    def test_cancel(self):
        run_id = self.start().json()["run_id"]
        self.user = {"username": "other", "roles": ["reviewer"]}
        self.assertEqual(self.client.post(f"{PREFIX}/cancel", json={"run_id": run_id}).status_code, 403)
        self.user = {"username": "rev", "roles": ["reviewer"]}
        self.assertEqual(self.client.post(f"{PREFIX}/delete", json={"run_id": run_id}).status_code, 409)
        self.assertEqual(self.client.post(f"{PREFIX}/cancel", json={"run_id": run_id}).status_code, 200)
        self.assertTrue(self.store.cancel_requested(run_id))

    def test_start_errors(self):
        self.assertEqual(self.start(batches=["units:0"]).status_code, 422)
        self.assertEqual(self.start(batches=[f"units:{n}" for n in range(1, 11)], modes=["all", "changed"], repeats=4).status_code, 422)
        self.assertEqual(self.start(provider_id="nope").status_code, 404)
        self.assertEqual(self.start(reference_stable_id="nope").status_code, 404)
        response = self.start(input_stable_id="gold1")
        self.assertEqual(response.status_code, 422)
        self.assertIn("current version itself", response.text)
        self.provider.status = {"availability": "very_busy", "detail": "demand 9"}
        response = self.start()
        self.assertEqual(response.status_code, 409)
        self.assertIn("very busy", response.text)
        self.assertEqual(self.start(force_busy=True).status_code, 200)
        self.assertEqual(len(self.started), 1)


if __name__ == "__main__":
    unittest.main()
