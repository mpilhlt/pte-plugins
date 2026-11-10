"""
Unit tests for the manager lifecycle of the MPCDF LLM plugin.

@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/plugin.py
@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/manager.py
"""

import asyncio
import logging
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mpcdf_llm.manager import ProvisioningManager
from mpcdf_llm.storage import ManagedEndpoint
from pte_plugin_mpcdf_llm import manager as manager_module
from pte_plugin_mpcdf_llm.manager import DEFAULT_BASE_URL, base_url, data_dir, get_manager
from pte_plugin_mpcdf_llm.plugin import MpcdfLlmPlugin


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        settings = SimpleNamespace(plugins_data_dir=self.root)
        patcher = patch.object(manager_module, "get_settings", return_value=settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        # never call the real config writer
        patcher = patch.object(
            manager_module, "get_plugin_config", lambda k, e, default=None, **kw: os.environ.get(e, default)
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class TestManagerLifecycle(_Base):
    def test_get_manager_raises_lookup_error_before_initialize(self):
        with self.assertRaises(LookupError):
            get_manager()

    def test_initialize_creates_manager_and_cleanup_clears_it(self):
        async def run():
            plugin = MpcdfLlmPlugin()
            await plugin.initialize(None)
            self.assertIsInstance(get_manager(), ProvisioningManager)
            await asyncio.sleep(0)  # let the open task run
            await plugin.cleanup()
            with self.assertRaises(LookupError):
                get_manager()

        asyncio.run(run())

    def test_initialize_quiets_debug_logging(self):
        # the state file is read under a file lock every few seconds; filelock logs each
        # acquire and release at DEBUG, which floods a debug-level host log
        filelock_logger = logging.getLogger("filelock")
        protocol_logger = logging.getLogger("mpcdf_llm.protocol")
        for lg in (filelock_logger, protocol_logger):
            self.addCleanup(lg.setLevel, lg.level)
            lg.setLevel(logging.NOTSET)

        async def run():
            plugin = MpcdfLlmPlugin()
            await plugin.initialize(None)
            await plugin.cleanup()

        asyncio.run(run())
        self.assertEqual(filelock_logger.level, logging.WARNING)
        self.assertEqual(protocol_logger.level, logging.INFO)

    def test_reinitialize_in_new_event_loop_gets_fresh_manager(self):
        managers = []

        async def run():
            plugin = MpcdfLlmPlugin()
            await plugin.initialize(None)
            managers.append(get_manager())
            await asyncio.sleep(0.05)
            await plugin.cleanup()

        asyncio.run(run())
        asyncio.run(run())
        self.assertIsNot(managers[0], managers[1])

    def test_start_manager_twice_replaces_manager(self):
        async def run():
            first = await manager_module.start_manager()
            second = await manager_module.start_manager()
            self.assertIsNot(first, second)
            self.assertIs(get_manager(), second)
            await manager_module.stop_manager()

        asyncio.run(run())

    def test_cleanup_does_not_cancel_endpoints(self):
        calls = []

        async def fail_stop(*args, **kwargs):
            calls.append("stop")

        async def fail_cancel(*args, **kwargs):
            calls.append("cancel")

        now = datetime.now(timezone.utc)
        entry = ManagedEndpoint(
            endpoint_id=42, preset="p", served={"p": "m"}, until=None, submitted_at=now, last_activity=now
        )

        async def run():
            plugin = MpcdfLlmPlugin()
            await plugin.initialize(None)
            manager = get_manager()
            manager.client.cancel = fail_cancel
            manager.state.upsert(entry)
            await asyncio.sleep(0.05)
            await plugin.cleanup()
            return manager

        with patch.object(ProvisioningManager, "stop", fail_stop):
            manager = asyncio.run(run())
        self.assertEqual(calls, [])
        self.assertIn(42, manager.state.managed())

    def test_supervisor_starts_even_if_open_fails(self):
        started = []

        async def bad_open(self):
            raise RuntimeError("secret-token-123")

        def fake_start(self, *a, **kw):
            started.append(True)

        async def run():
            with self.assertLogs(manager_module.logger, "WARNING") as logs:
                await manager_module.start_manager()
                await asyncio.sleep(0.05)
            await manager_module.stop_manager()
            return logs

        with patch.object(ProvisioningManager, "open", bad_open), patch.object(
            ProvisioningManager, "start_supervisor", fake_start
        ):
            logs = asyncio.run(run())
        self.assertEqual(started, [True])
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("secret-token-123", "\n".join(logs.output))

    def test_metadata_and_manage_endpoint(self):
        plugin = MpcdfLlmPlugin()
        meta = plugin.metadata
        self.assertEqual(meta["id"], "mpcdf-llm")
        self.assertEqual(meta["category"], "inference")
        self.assertEqual(meta["required_roles"], ["admin"])
        self.assertEqual(meta["endpoints"][0]["name"], "manage")
        result = asyncio.run(plugin.manage(None, {}))
        self.assertEqual(result, {"outputUrl": "/api/plugins/mpcdf-llm/manage"})


class TestConfig(_Base):
    def test_base_url_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MPCDF_LLM_BASE_URL", None)
            self.assertEqual(base_url(), DEFAULT_BASE_URL)

    def test_base_url_from_env(self):
        with patch.dict(os.environ, {"MPCDF_LLM_BASE_URL": "http://example.test"}):
            self.assertEqual(base_url(), "http://example.test")

    def test_data_dir_under_plugins_data_dir(self):
        self.assertEqual(data_dir(), self.root / "mpcdf-llm")
