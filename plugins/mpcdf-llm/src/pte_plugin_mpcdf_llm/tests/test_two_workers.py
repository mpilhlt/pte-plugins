"""
Two workers (two managers of one user on that user's directory) against one FakeLlmis.

Worker A holds the supervisor lock; worker B is passive and handles the start. Both read the
user's session from the shared secret store.

@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/providers.py
@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/manager.py
"""

import asyncio

from mpcdf_llm import names
from mpcdf_llm.manager import ProvisioningManager
from mpcdf_llm.supervisor import Supervisor

from pte_plugin_mpcdf_llm import manager as manager_module
from pte_plugin_mpcdf_llm import providers

from .support import TIMEOUT, PluginTestCase, until


def T(awaitable):
    """Bound a wait so a regression fails the test instead of hanging it."""
    return asyncio.wait_for(awaitable, TIMEOUT)


class TestTwoWorkers(PluginTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.closers: dict[ProvisioningManager, object] = {}
        self.a = await self.make()
        await T(self.a.login("alice", "pw", "123456"))
        self.sup_a = Supervisor(self.a)  # takes the lock; never started: tests call tick()
        self.addCleanup(self.sup_a.release)
        self.b = await self.make()  # logged in through the stored session only
        self.sup_b = Supervisor(self.b)
        self.addCleanup(self.sup_b.release)
        self.assertTrue(self.sup_a.active)
        self.assertFalse(self.sup_b.active)

    async def make(self) -> ProvisioningManager:
        manager_module.user_dir("alice").mkdir(parents=True, exist_ok=True)
        manager = manager_module.new_manager("alice")
        await manager.open()
        closed = False

        async def close():
            nonlocal closed
            if not closed:
                closed = True
                await manager.close()

        self.closers[manager] = close
        self.addAsyncCleanup(close)
        return manager

    async def drive(self, steps=(names.BUILDING, names.RUNNING)):
        await until(lambda: 1 in self.fake.endpoints)
        for status in steps:
            await asyncio.sleep(0.05)
            self.fake.advance(1, status)

    def ready(self, manager: ProvisioningManager) -> bool:
        entry = manager.state.managed().get(1)
        return entry is not None and entry.ready_at is not None

    async def collect(self):
        return [e async for e in self.b.start("p")]

    async def start_in_b(self) -> asyncio.Task:
        task = asyncio.create_task(self.collect())
        await until(lambda: 1 in self.b.state.managed() and self.b.state.managed()[1].follower_id is not None)
        return task

    async def test_start_in_passive_worker_submits_once_and_ready(self):
        driver = asyncio.create_task(self.drive())
        events = await T(self.collect())
        await T(driver)
        self.assertEqual(events[-1].phase, "ready")
        self.assertEqual(self.fake.received.count(names.SUBMIT), 1)
        self.assertTrue(self.ready(self.a))  # the other worker sees the shared state

    async def test_active_supervisor_leaves_followed_endpoint_alone(self):
        task = await self.start_in_b()
        for _ in range(5):
            await T(self.sup_a.tick())
            await asyncio.sleep(0.01)
        entry = self.a.state.managed()[1]
        self.assertIsNone(entry.adopt_attempted_at)
        self.assertEqual(entry.follower_id, self.b.follower_id)
        self.assertNotIn(names.DELETE_ENDPOINT, self.fake.received)
        driver = asyncio.create_task(self.drive())
        events = await T(task)
        await T(driver)
        self.assertEqual(events[-1].phase, "ready")
        self.assertEqual(self.fake.received.count(names.SUBMIT), 1)
        self.assertNotIn(names.DELETE_ENDPOINT, self.fake.received)

    async def test_taken_over_after_starter_closes(self):
        task = await self.start_in_b()
        await asyncio.sleep(0.05)
        await T(self.closers[self.b]())
        await T(asyncio.gather(task, return_exceptions=True))
        self.assertFalse(self.ready(self.a))
        driver = asyncio.create_task(self.drive())
        deadline = asyncio.get_running_loop().time() + TIMEOUT
        while not self.ready(self.a) and asyncio.get_running_loop().time() < deadline:
            await T(self.sup_a.tick())
            await asyncio.sleep(0.02)
        await T(driver)
        self.assertTrue(self.ready(self.a))
        self.assertEqual(self.fake.received.count(names.SUBMIT), 1)
        self.assertNotIn(names.DELETE_ENDPOINT, self.fake.received)

    async def test_session_of_one_worker_used_by_the_other(self):
        self.assertEqual((await self.b.session_status()).state, "ok")
        self.assertEqual((await self.b.session_status()).username, "alice")

    async def test_endpoints_of_one_user_in_another_workers_cache(self):
        # The worker whose manager_for() map holds b sees the endpoint b started.
        driver = asyncio.create_task(self.drive())
        events = await T(self.collect())
        await T(driver)
        self.assertEqual(events[-1].phase, "ready")
        await T(providers.refresh_user("alice", self.a))
        self.assertEqual(providers.ready_presets("alice"), {"p": "openai/gpt-oss-120b"})
