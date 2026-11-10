"""
Tests for the MPCDF providers in the inference menu, wired to the library's FakeLlmis.

@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/providers.py
@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/plugin.py
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from mpcdf_llm import names
from mpcdf_llm.manager import ProvisioningManager
from mpcdf_llm.models import Framework
from mpcdf_llm.presets import Preset
from mpcdf_llm.testing import FakeLlmis

from fastapi_app.plugin_api import LLMModel, LLMProvider, LLMProviderError, LLMProviderRegistry
from pte_plugin_mpcdf_llm import manager as manager_module
from pte_plugin_mpcdf_llm import providers
from pte_plugin_mpcdf_llm.plugin import MpcdfLlmPlugin

PRESET = Preset(
    name="p", description="Big model", framework=Framework.VLLM, model="openai/gpt-oss-120b",
    machine="Viper", gpu_type="mi300a", gpus=1,
)
GONE = "MPCDF endpoint for p is no longer running"


class OtherProvider(LLMProvider):
    id = "other-plugin"
    label = "Other"

    def list_models(self) -> list[LLMModel]:
        return []

    async def chat_completion(self, model_id, system_prompt, user_prompt, temperature=0.2) -> str:
        return ""


def chat_transport(fake: FakeLlmis, calls: list[httpx.Request]) -> httpx.MockTransport:
    """Answer chat completions for the fake's endpoints (the fake itself does not)."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        slug = request.url.path.lstrip("/").partition("/")[0]
        ep = next((e for e in fake.endpoints.values() if e["url"].endswith("/" + slug)), None)
        if ep is None or ep["status"] != names.RUNNING:
            return httpx.Response(503)
        if request.headers.get("authorization") != f"Bearer {ep['key']}":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    return httpx.MockTransport(handler)


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for target, value in (
            ("get_settings", SimpleNamespace(plugins_data_dir=self.root)),
            ("get_plugin_config", lambda k, e, default=None, **kw: os.environ.get(e, default)),
        ):
            patcher = patch.object(manager_module, target, value if callable(value) else lambda v=value: v)
            patcher.start()
            self.addCleanup(patcher.stop)
        LLMProviderRegistry.reset_instance()
        providers._registered.clear()
        self.addCleanup(LLMProviderRegistry.reset_instance)
        self.addCleanup(providers._registered.clear)

        self.fake = FakeLlmis()
        await self.fake.start()
        self.addAsyncCleanup(self.fake.stop)
        self.calls: list[httpx.Request] = []
        http = httpx.AsyncClient(transport=self.fake.http_transport())
        self.addAsyncCleanup(http.aclose)
        self.manager = ProvisioningManager(
            self.root / "mgr", base_url=self.fake.base_url, http=http, poll_interval=0.01
        )
        await self.manager.open()
        self.addAsyncCleanup(self.manager.close)
        self.manager.presets.put(PRESET)
        patcher = patch.object(providers, "get_manager", lambda: self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(
            providers, "new_http_client", lambda: httpx.AsyncClient(transport=chat_transport(self.fake, self.calls))
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.registry = LLMProviderRegistry.get_instance()

    async def login(self):
        await self.manager.login("alice", "pw", "123456")

    async def start_ready(self):
        """Log in and bring the preset's endpoint to Running and ready."""
        await self.login()

        async def drive():
            while 1 not in self.fake.endpoints:
                await asyncio.sleep(0.005)
            for status in (names.BUILDING, names.RUNNING):
                await asyncio.sleep(0.05)
                self.fake.advance(1, status)

        driver = asyncio.create_task(drive())
        events = [e async for e in self.manager.start("p")]
        await driver
        self.assertEqual(events[-1].phase, "ready")

    async def settle(self):
        await asyncio.sleep(0.05)  # beyond the manager's endpoint cache (poll_interval)


class TestSync(_Base):
    async def test_sync_registers_ready_preset_as_free_model(self):
        await self.start_ready()
        await providers.sync_providers(self.manager)
        provider = self.registry.get_provider("mpcdf-llm-p")
        self.assertEqual(provider.label, "MPCDF: p")
        self.assertEqual(
            provider.list_models(),
            [
                LLMModel(
                    id="openai/gpt-oss-120b", label="Big model (openai/gpt-oss-120b)",
                    capabilities=frozenset({"chat"}), status=None, free=True,
                )
            ],
        )
        self.assertFalse(provider.supports_image_input("openai/gpt-oss-120b"))
        self.assertFalse(provider.supports_pdf_input("openai/gpt-oss-120b"))

    async def test_sync_unregisters_when_endpoint_ends(self):
        await self.start_ready()
        await providers.sync_providers(self.manager)
        self.fake.advance(1, names.CANCELED)
        await self.settle()
        await providers.sync_providers(self.manager)
        with self.assertRaises(KeyError):
            self.registry.get_provider("mpcdf-llm-p")

    async def test_sync_keeps_other_plugins_providers(self):
        self.registry.register(OtherProvider())
        await self.start_ready()
        await providers.sync_providers(self.manager)
        self.fake.advance(1, names.CANCELED)
        await self.settle()
        await providers.sync_providers(self.manager)
        providers.unregister_all()
        self.assertEqual([p.id for p in self.registry.list_providers()], ["other-plugin"])

    async def test_sync_without_login_registers_nothing(self):
        await providers.sync_providers(self.manager)
        self.assertEqual(self.registry.list_providers(), [])

    async def test_watch_survives_without_login_and_without_manager(self):
        with patch.object(providers, "SYNC_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(providers.watch())
            await asyncio.sleep(0.1)
            self.assertFalse(task.done())
            with patch.object(providers, "get_manager", side_effect=LookupError):
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.registry.list_providers(), [])


class TestChat(_Base):
    async def test_chat_uses_fresh_credentials_and_touches(self):
        await self.start_ready()
        await providers.sync_providers(self.manager)
        before = self.manager.state.managed()[1].last_activity
        await asyncio.sleep(0.01)
        provider = self.registry.get_provider("mpcdf-llm-p")
        answer = await provider.chat_completion("openai/gpt-oss-120b", "sys", "hi")
        self.assertEqual(answer, "hello")
        request = self.calls[-1]
        self.assertTrue(request.url.path.endswith("/v1/chat/completions"))
        self.assertEqual(request.headers["authorization"], f"Bearer {self.fake.endpoints[1]['key']}")
        self.assertGreater(self.manager.state.managed()[1].last_activity, before)

    async def test_chat_refreshes_once_after_401(self):
        await self.start_ready()
        await providers.sync_providers(self.manager)
        provider = self.registry.get_provider("mpcdf-llm-p")
        await provider.chat_completion("m", "s", "u")  # fills the credential cache
        self.fake.endpoints[1]["key"] = "n" * 32  # the endpoint's key changed
        await self.settle()
        self.assertEqual(await provider.chat_completion("m", "s", "u"), "hello")
        statuses = [r.headers["authorization"] for r in self.calls]
        self.assertEqual(len(statuses), 3)  # first ok, stale key (401), retried with the new key
        self.assertEqual(statuses[-1], f"Bearer {'n' * 32}")

    async def test_chat_raises_clear_error_when_endpoint_gone(self):
        await self.start_ready()
        await providers.sync_providers(self.manager)
        provider = self.registry.get_provider("mpcdf-llm-p")
        await provider.chat_completion("m", "s", "u")  # fills the credential cache
        url, key = self.fake.endpoints[1]["url"], self.fake.endpoints[1]["key"]
        self.fake.advance(1, names.CANCELED)
        await self.settle()
        with self.assertRaises(LLMProviderError) as ctx:
            await provider.chat_completion("m", "s", "u")
        self.assertEqual(str(ctx.exception), GONE)
        self.assertNotIn(url, str(ctx.exception))
        self.assertNotIn(key, str(ctx.exception))

    async def test_chat_raises_clear_error_without_login(self):
        provider = providers.MpcdfLLMProvider("p", "m")
        with self.assertRaises(LLMProviderError) as ctx:
            await provider.chat_completion("m", "s", "u")
        self.assertEqual(str(ctx.exception), GONE)

    async def test_other_http_errors_carry_status_only(self):
        await self.start_ready()
        await providers.sync_providers(self.manager)
        provider = self.registry.get_provider("mpcdf-llm-p")
        url = self.fake.endpoints[1]["url"]

        def handler(request):
            return httpx.Response(429, text=f"slow down {url}")

        with patch.object(providers, "new_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))):
            with self.assertRaises(LLMProviderError) as ctx:
                await provider.chat_completion("m", "s", "u")
        self.assertEqual(str(ctx.exception), "MPCDF: p returned 429")


class TestPluginLifecycle(_Base):
    async def test_cleanup_unregisters_all(self):
        self.registry.register(OtherProvider())
        await self.start_ready()
        plugin = MpcdfLlmPlugin()
        with patch.object(providers, "SYNC_INTERVAL_SECONDS", 0.01):
            await plugin.initialize(None)
            # the plugin's own manager is current for the manager module; providers use the test's
            await providers.sync_providers(self.manager)
            self.assertIn("mpcdf-llm-p", [p.id for p in self.registry.list_providers()])
            await plugin.cleanup()
        self.assertEqual([p.id for p in self.registry.list_providers()], ["other-plugin"])
        self.assertIsNone(plugin._watch_task)
