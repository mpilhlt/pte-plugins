"""
Unit tests for the manager lifecycle of the MPCDF LLM plugin.

@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/plugin.py
@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/manager.py
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mpcdf_llm.manager import ProvisioningManager
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
        async def fail(*args, **kwargs):
            raise AssertionError("stop() must not be called on cleanup")

        async def run():
            plugin = MpcdfLlmPlugin()
            await plugin.initialize(None)
            await asyncio.sleep(0.05)
            await plugin.cleanup()

        with patch.object(ProvisioningManager, "stop", fail):
            asyncio.run(run())

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
            with patch.object(manager_module, "get_plugin_config", lambda k, e, default=None, **kw: os.environ.get(e, default)):
                self.assertEqual(base_url(), DEFAULT_BASE_URL)

    def test_base_url_from_env(self):
        with patch.dict(os.environ, {"MPCDF_LLM_BASE_URL": "http://example.test"}):
            with patch.object(manager_module, "get_plugin_config", lambda k, e, default=None, **kw: os.environ.get(e, default)):
                self.assertEqual(base_url(), "http://example.test")

    def test_data_dir_under_plugins_data_dir(self):
        self.assertEqual(data_dir(), self.root / "mpcdf-llm")
