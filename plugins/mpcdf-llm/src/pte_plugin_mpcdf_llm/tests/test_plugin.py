"""
Unit tests for the plugin lifecycle and the per-user managers of the MPCDF LLM plugin.

@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/plugin.py
@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/manager.py
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from unittest.mock import patch

from mpcdf_llm.errors import MpcdfError
from mpcdf_llm.manager import ProvisioningManager
from mpcdf_llm.storage import ManagedEndpoint

from fastapi_app.lib.secrets import SecretServiceRegistry, SecretsError
from pte_plugin_mpcdf_llm import manager as manager_module
from pte_plugin_mpcdf_llm.manager import (
    DEFAULT_BASE_URL,
    HF_TOKEN_SECRET,
    LEGACY_HF_TOKEN_KEY,
    SESSION_SECRET,
    SecretSessionStore,
    data_dir,
    manager_for,
    user_dir,
    user_key,
    username_of,
)
from pte_plugin_mpcdf_llm.plugin import MpcdfLlmPlugin

from .support import ORIGINAL_BASE_URL, PRESET, PluginTestCase


class TestLifecycle(PluginTestCase):
    start_plugin = False

    async def test_manager_for_raises_lookup_error_before_start(self):
        with self.assertRaises(LookupError):
            manager_for("alice")

    async def test_initialize_registers_hf_token_service_and_cleanup_removes_it(self):
        plugin = MpcdfLlmPlugin()
        await plugin.initialize(None)
        try:
            service = SecretServiceRegistry.get(HF_TOKEN_SECRET)
            self.assertIsNotNone(service)
            self.assertEqual(service.help_url, "https://huggingface.co/settings/tokens")
            self.assertIsNone(SecretServiceRegistry.get(SESSION_SECRET))  # never editable as an API key
            self.assertIsInstance(manager_for("alice"), ProvisioningManager)
        finally:
            await plugin.cleanup()
        self.assertIsNone(SecretServiceRegistry.get(HF_TOKEN_SECRET))
        with self.assertRaises(LookupError):
            manager_for("alice")

    async def test_initialize_quiets_debug_logging(self):
        filelock_logger = logging.getLogger("filelock")
        protocol_logger = logging.getLogger("mpcdf_llm.protocol")
        for lg in (filelock_logger, protocol_logger):
            self.addCleanup(lg.setLevel, lg.level)
            lg.setLevel(logging.NOTSET)
        plugin = MpcdfLlmPlugin()
        await plugin.initialize(None)
        await plugin.cleanup()
        self.assertEqual(filelock_logger.level, logging.WARNING)
        self.assertEqual(protocol_logger.level, logging.INFO)

    async def test_restart_gives_fresh_managers(self):
        await manager_module.start()
        first = manager_for("alice")
        await manager_module.start()
        second = manager_for("alice")
        self.assertIsNot(first, second)
        await manager_module.stop()

    async def test_one_manager_per_user_with_own_directory(self):
        await manager_module.start()
        self.addAsyncCleanup(manager_module.stop)
        alice, bob = manager_for("alice"), manager_for("bob")
        self.assertIs(manager_for("alice"), alice)
        self.assertIsNot(alice, bob)
        self.assertEqual(alice.data_dir, user_dir("alice"))
        self.assertIs(alice.presets, bob.presets)  # the shared catalog

    async def test_cleanup_does_not_cancel_endpoints(self):
        calls = []

        async def fail_stop(*args, **kwargs):
            calls.append("stop")

        async def fail_cancel(*args, **kwargs):
            calls.append("cancel")

        now = datetime.now(timezone.utc)
        entry = ManagedEndpoint(
            endpoint_id=42, preset="p", served={"p": "m"}, until=None, submitted_at=now, last_activity=now
        )
        plugin = MpcdfLlmPlugin()
        with patch.object(ProvisioningManager, "stop", fail_stop):
            await plugin.initialize(None)
            manager = manager_for("alice")
            manager.client.cancel = fail_cancel
            manager.state.upsert(entry)
            await asyncio.sleep(0.05)
            await plugin.cleanup()
        self.assertEqual(calls, [])
        self.assertIn(42, manager.state.managed())

    async def test_supervisor_starts_even_if_open_fails(self):
        started = []

        async def bad_open(self):
            raise RuntimeError("secret-token-123")

        def fake_start(self, *a, **kw):
            started.append(True)

        with patch.object(ProvisioningManager, "open", bad_open), patch.object(
            ProvisioningManager, "start_supervisor", fake_start
        ):
            await manager_module.start()
            with self.assertLogs(manager_module.logger, "WARNING") as logs:
                manager_for("alice")
                await asyncio.sleep(0.05)
            await manager_module.stop()
        self.assertEqual(started, [True])
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("secret-token-123", "\n".join(logs.output))

    async def test_metadata_and_manage_endpoint(self):
        plugin = MpcdfLlmPlugin()
        meta = plugin.metadata
        self.assertEqual(meta["id"], "mpcdf-llm")
        self.assertEqual(meta["category"], "inference")
        self.assertEqual(meta["required_roles"], [])
        self.assertEqual(meta["endpoints"][0]["label"], "My MPCDF endpoints")
        result = await plugin.manage(None, {})
        self.assertEqual(result, {"outputUrl": "/api/plugins/mpcdf-llm/manage"})


class TestMigration(PluginTestCase):
    start_plugin = False

    async def test_moves_presets_discards_session_and_removes_hf_token(self):
        old = data_dir()
        old.mkdir(parents=True)
        (old / "presets.json").write_text(json.dumps({"presets": {"p": PRESET.model_dump(mode="json")}}))
        (old / "session.json").write_text('{"token": "secret"}')
        self.config.values[LEGACY_HF_TOKEN_KEY] = "hf_shared"
        self.config.values[LEGACY_HF_TOKEN_KEY + ".masked"] = True
        with self.assertLogs(manager_module.logger, "WARNING") as logs:
            await manager_module.start()
        self.addAsyncCleanup(manager_module.stop)
        self.assertEqual([p.name for p in manager_module.catalog().list()], ["p"])
        self.assertFalse((old / "presets.json").exists())
        self.assertFalse((old / "session.json").exists())
        self.assertNotIn(LEGACY_HF_TOKEN_KEY, self.config.values)
        self.assertNotIn(LEGACY_HF_TOKEN_KEY + ".masked", self.config.values)
        self.assertNotIn("hf_shared", "\n".join(logs.output))

    async def test_existing_catalog_is_kept(self):
        manager_module.catalog().put(PRESET)
        (data_dir() / "presets.json").write_text('{"presets": {}}')
        await manager_module.start()
        self.addAsyncCleanup(manager_module.stop)
        self.assertEqual([p.name for p in manager_module.catalog().list()], ["p"])


class TestSessions(PluginTestCase):
    async def test_login_stores_session_encrypted_and_survives_restart(self):
        await self.login("alice")
        self.assertTrue(self.store.has("alice", SESSION_SECRET))
        self.assertEqual(list(self.root.rglob("session.json")), [])
        raw = (self.root / "db" / "secrets.db").read_bytes()
        stored = json.loads(self.store.get("alice", SESSION_SECRET))
        self.assertNotIn(stored["token"].encode(), raw)
        await manager_module.start()  # restart: new managers, opened from the stored sessions
        await manager_module.sync_users()
        manager = manager_module.existing_manager("alice")
        self.assertIsNotNone(manager)
        self.assertEqual((await manager.session_status()).state, "ok")
        self.assertIsNone(manager_module.existing_manager("bob"))

    async def test_logout_clears_stored_session(self):
        manager = await self.login("alice")
        await manager.logout()
        self.assertFalse(self.store.has("alice", SESSION_SECRET))

    async def test_session_store_roundtrip_and_errors(self):
        await self.login("alice")
        store = SecretSessionStore("alice")
        self.assertEqual(store.load().username, "alice")
        self.assertIsNone(SecretSessionStore("bob").load())
        self.store.set("bob", SESSION_SECRET, "not json")
        with self.assertRaises(MpcdfError):
            SecretSessionStore("bob").load()
        with patch.object(self.store, "get", side_effect=SecretsError("key changed")):
            self.assertIsNone(store.load())

    async def test_removed_session_drops_manager_in_every_worker(self):
        manager = await self.login("alice")
        self.store.delete_all("alice")  # API Keys > Remove all, or an admin, possibly in another worker
        await manager_module.sync_users()
        self.assertIsNone(manager_module.existing_manager("alice"))
        self.assertEqual((await manager_for("alice").session_status()).state, "login_required")
        self.assertIsNot(manager_for("alice"), manager)

    async def test_manager_without_login_is_kept(self):
        manager = manager_for("alice")
        await manager_module.sync_users()
        self.assertIs(manager_module.existing_manager("alice"), manager)


class TestHuggingFaceToken(PluginTestCase):
    async def test_endpoint_started_with_users_token(self):
        self.store.set("alice", HF_TOKEN_SECRET, "hf_alice")
        await self.start_ready("alice")
        self.assertEqual([f["hf_token"] for f in self.fake.submitted], ["hf_alice"])

    async def test_no_token_never_falls_back_to_environment(self):
        with patch.dict(os.environ, {"HF_TOKEN": "hf_from_env"}):
            await self.start_ready("alice")
        self.assertEqual([f["hf_token"] for f in self.fake.submitted], [""])

    async def test_undecryptable_token_counts_as_none(self):
        with patch.object(self.store, "get", side_effect=SecretsError("key changed")):
            self.assertIsNone(manager_module.user_hf_token("alice"))


class TestConfig(PluginTestCase):
    start_plugin = False

    async def test_base_url_default_and_env(self):
        patcher = patch.object(manager_module, "get_plugin_config", lambda k, e, default=None, **kw: os.environ.get(e, default))
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MPCDF_LLM_BASE_URL", None)
            self.assertEqual(ORIGINAL_BASE_URL(), DEFAULT_BASE_URL)
        with patch.dict(os.environ, {"MPCDF_LLM_BASE_URL": "http://example.test"}):
            self.assertEqual(ORIGINAL_BASE_URL(), "http://example.test")

    async def test_user_directory_names_are_reversible_and_safe(self):
        for name in ("alice", "Alice", "a/b", "../x", "ü@example.org"):
            key = user_key(name)
            self.assertRegex(key, r"^[0-9a-f]+$")
            self.assertEqual(username_of(key), name)
        self.assertNotEqual(user_key("alice"), user_key("Alice").lower())
        self.assertIsNone(username_of("not-hex"))
        self.assertEqual(user_dir("alice"), self.root / "mpcdf-llm" / "users" / user_key("alice"))
