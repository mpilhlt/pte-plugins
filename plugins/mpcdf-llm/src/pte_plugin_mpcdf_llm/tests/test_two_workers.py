"""
Two workers (two managers on one data dir) against one FakeLlmis.

Worker A holds the supervisor lock; worker B is passive and handles the start.

@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/providers.py
@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/manager.py
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx
from mpcdf_llm import names
from mpcdf_llm.manager import ProvisioningManager
from mpcdf_llm.supervisor import Supervisor
from mpcdf_llm.testing import FakeLlmis

from fastapi_app.plugin_api import LLMProviderRegistry
from pte_plugin_mpcdf_llm import providers

from .test_providers import PRESET


class TestTwoWorkers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name) / "mgr"
        LLMProviderRegistry.reset_instance()
        providers._registered.clear()
        self.addCleanup(LLMProviderRegistry.reset_instance)
        self.addCleanup(providers._registered.clear)
        self.fake = FakeLlmis()
        await self.fake.start()
        self.addAsyncCleanup(self.fake.stop)

        self.a = await self.make()
        await self.a.login("alice", "pw", "123456")
        self.a.presets.put(PRESET)
        self.sup_a = Supervisor(self.a)  # takes the lock; never started: tests call tick()
        self.addCleanup(self.sup_a.release)
        self.b = await self.make()
        self.sup_b = Supervisor(self.b)
        self.addCleanup(self.sup_b.release)
        self.assertTrue(self.sup_a.active)
        self.assertFalse(self.sup_b.active)

    async def make(self) -> ProvisioningManager:
        http = httpx.AsyncClient(transport=self.fake.http_transport())
        manager = ProvisioningManager(self.data, base_url=self.fake.base_url, http=http, poll_interval=0.01)
        await manager.open()
        closed = False

        async def close():
            nonlocal closed
            if not closed:
                closed = True
                await manager.close()
                await http.aclose()

        manager.close_once = close
        self.addAsyncCleanup(close)
        return manager

    async def drive(self, steps=(names.BUILDING, names.RUNNING)):
        while 1 not in self.fake.endpoints:
            await asyncio.sleep(0.005)
        for status in steps:
            await asyncio.sleep(0.05)
            self.fake.advance(1, status)

    def ready(self, manager: ProvisioningManager) -> bool:
        entry = manager.state.managed().get(1)
        return entry is not None and entry.ready_at is not None

    async def start_in_b(self) -> asyncio.Task:
        async def consume():
            return [e async for e in self.b.start("p")]

        task = asyncio.create_task(consume())
        while 1 not in self.b.state.managed() or self.b.state.managed()[1].follower_id is None:
            await asyncio.sleep(0.005)
        return task

    async def test_start_in_passive_worker_submits_once_and_ready(self):
        driver = asyncio.create_task(self.drive())
        events = [e async for e in self.b.start("p")]
        await driver
        self.assertEqual(events[-1].phase, "ready")
        self.assertEqual(self.fake.received.count(names.SUBMIT), 1)
        self.assertTrue(self.ready(self.a))  # the other worker sees the shared state

    async def test_active_supervisor_leaves_followed_endpoint_alone(self):
        task = await self.start_in_b()
        for _ in range(5):
            await self.sup_a.tick()
            await asyncio.sleep(0.01)
        entry = self.a.state.managed()[1]
        self.assertIsNone(entry.adopt_attempted_at)
        self.assertEqual(entry.follower_id, self.b.follower_id)
        self.assertNotIn(names.DELETE_ENDPOINT, self.fake.received)
        driver = asyncio.create_task(self.drive())
        events = await task
        await driver
        self.assertEqual(events[-1].phase, "ready")
        self.assertEqual(self.fake.received.count(names.SUBMIT), 1)
        self.assertNotIn(names.DELETE_ENDPOINT, self.fake.received)

    async def test_taken_over_after_starter_closes(self):
        task = await self.start_in_b()
        await asyncio.sleep(0.05)
        await self.b.close_once()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.ready(self.a))
        driver = asyncio.create_task(self.drive())
        for _ in range(300):
            await self.sup_a.tick()
            if self.ready(self.a):
                break
            await asyncio.sleep(0.02)
        await driver
        self.assertTrue(self.ready(self.a))
        self.assertEqual(self.fake.received.count(names.SUBMIT), 1)
        self.assertNotIn(names.DELETE_ENDPOINT, self.fake.received)

    async def test_providers_converge_in_both_workers(self):
        # One process, one registry: sync worker by worker and look after each.
        driver = asyncio.create_task(self.drive())
        events = [e async for e in self.b.start("p")]
        await driver
        self.assertEqual(events[-1].phase, "ready")
        registry = LLMProviderRegistry.get_instance()
        for manager in (self.a, self.b):
            providers.unregister_all()
            with self.assertRaises(KeyError):
                registry.get_provider("mpcdf-llm-p")
            await providers.sync_providers(manager)
            self.assertEqual(registry.get_provider("mpcdf-llm-p").label, "MPCDF: p")
