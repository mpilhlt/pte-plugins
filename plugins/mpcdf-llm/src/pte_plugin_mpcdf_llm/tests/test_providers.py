"""
Tests for the MPCDF providers in the inference menu, wired to the library's FakeLlmis.

@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/providers.py
@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/plugin.py
"""

import asyncio
from unittest.mock import patch

import httpx
from mpcdf_llm import names
from mpcdf_llm.errors import LoginRequired, ProtocolError

from fastapi_app.plugin_api import LLMModel, LLMProvider, LLMProviderError, ProviderNotAvailableError
from pte_plugin_mpcdf_llm import manager as manager_module
from pte_plugin_mpcdf_llm import providers
from pte_plugin_mpcdf_llm.plugin import MpcdfLlmPlugin

from .support import GONE, PRESET, PluginTestCase


class OtherProvider(LLMProvider):
    id = "other-plugin"
    label = "Other"

    def list_models(self) -> list[LLMModel]:
        return []

    async def chat_completion(self, model_id, system_prompt, user_prompt, temperature=0.2) -> str:
        return ""


class _Base(PluginTestCase):
    async def ready_provider(self, username: str = "alice"):
        """Start alice's endpoint, sync and return her bound provider."""
        await self.start_ready(username)
        await providers.sync_providers()
        self.manager = manager_module.existing_manager(username)
        return self.registry.get_provider_for_user("mpcdf-llm-p", username)


class TestSync(_Base):
    async def test_catalog_preset_registered_without_endpoint_but_offered_to_nobody(self):
        await providers.sync_providers()
        provider = self.registry.get_provider("mpcdf-llm-p")
        self.assertEqual(provider.label, "MPCDF: p")
        for username in ("alice", None):
            self.assertIsNone(provider.for_user(username))
            with self.assertRaises(ProviderNotAvailableError):
                self.registry.get_provider_for_user("mpcdf-llm-p", username)

    async def test_ready_endpoint_offered_to_its_user_only(self):
        provider = await self.ready_provider()
        self.assertEqual(provider.username, "alice")
        self.assertTrue(provider.uses_user_key)
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
        await self.login("bob")
        await providers.sync_providers()
        for username in ("bob", None):
            with self.assertRaises(ProviderNotAvailableError):
                self.registry.get_provider_for_user("mpcdf-llm-p", username)

    async def test_each_user_gets_their_own_endpoint(self):
        alice = await self.ready_provider("alice")
        bob = await self.ready_provider("bob")
        self.assertEqual(await alice.chat_completion("m", "s", "u"), "hello alice")
        self.assertEqual(await bob.chat_completion("m", "s", "u"), "hello bob")
        self.assertEqual([f["hf_token"] for f in self.fake.submitted], ["", ""])

    async def test_removed_preset_unregistered(self):
        await providers.sync_providers()
        manager_module.catalog().delete("p")
        await providers.sync_providers()
        with self.assertRaises(KeyError):
            self.registry.get_provider("mpcdf-llm-p")

    async def test_endpoint_end_withdraws_offer(self):
        await self.ready_provider()
        self.fake.advance(1, names.CANCELED)
        await self.settle()
        await providers.sync_providers()
        self.assertIsNone(self.registry.get_provider("mpcdf-llm-p").for_user("alice"))

    async def test_sync_keeps_other_plugins_providers(self):
        self.registry.register(OtherProvider())
        await providers.sync_providers()
        providers.unregister_all()
        self.assertEqual([p.id for p in self.registry.list_providers()], ["other-plugin"])

    async def test_sync_with_invalid_session_does_not_call_available(self):
        await self.ready_provider()
        calls = []

        async def available():
            calls.append(1)
            return []

        await self.manager.logout()
        with patch.object(self.manager, "available", available):
            await providers.refresh_user("alice", self.manager)
        self.assertEqual(calls, [])
        self.assertEqual(providers.ready_presets("alice"), {})

    async def test_sync_keeps_offer_on_transient_error(self):
        await self.ready_provider()

        async def boom():
            raise ProtocolError("timeout")

        with patch.object(self.manager, "available", boom):
            await providers.sync_providers()
        self.assertEqual(providers.ready_presets("alice"), {"p": PRESET.model})

    async def test_sync_withdraws_offer_on_login_required(self):
        await self.ready_provider()

        async def boom():
            raise LoginRequired("expired")

        with patch.object(self.manager, "available", boom):
            await providers.sync_providers()
        self.assertEqual(providers.ready_presets("alice"), {})

    async def test_watch_survives_errors(self):
        with patch.object(providers, "SYNC_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(providers.watch())
            await asyncio.sleep(0.1)
            self.assertFalse(task.done())
            with patch.object(manager_module, "sync_users", side_effect=RuntimeError("boom")):
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestChat(_Base):
    async def test_chat_uses_fresh_credentials_and_touches(self):
        provider = await self.ready_provider()
        before = self.manager.state.managed()[1].last_activity
        await asyncio.sleep(0.01)
        answer = await provider.chat_completion("openai/gpt-oss-120b", "sys", "hi")
        self.assertEqual(answer, "hello alice")
        request = self.calls[-1]
        self.assertTrue(request.url.path.endswith("/v1/chat/completions"))
        self.assertEqual(request.headers["authorization"], f"Bearer {self.fake.endpoints[1]['key']}")
        self.assertGreater(self.manager.state.managed()[1].last_activity, before)

    async def test_chat_refreshes_once_after_401(self):
        provider = await self.ready_provider()
        await provider.chat_completion("m", "s", "u")  # fills the credential cache
        self.fake.endpoints[1]["key"] = "n" * 32  # the endpoint's key changed
        await self.settle()
        self.assertEqual(await provider.chat_completion("m", "s", "u"), "hello alice")
        statuses = [r.headers["authorization"] for r in self.calls]
        self.assertEqual(len(statuses), 3)  # first ok, stale key (401), retried with the new key
        self.assertEqual(statuses[-1], f"Bearer {'n' * 32}")

    async def test_chat_raises_clear_error_when_endpoint_gone(self):
        provider = await self.ready_provider()
        await provider.chat_completion("m", "s", "u")  # fills the credential cache
        url, key = self.fake.endpoints[1]["url"], self.fake.endpoints[1]["key"]
        self.fake.advance(1, names.CANCELED)
        await self.settle()
        with self.assertRaises(LLMProviderError) as ctx:
            await provider.chat_completion("m", "s", "u")
        self.assertEqual(str(ctx.exception), GONE)
        self.assertNotIn(url, str(ctx.exception))
        self.assertNotIn(key, str(ctx.exception))

    async def test_unbound_provider_never_chats(self):
        provider = providers.MpcdfLLMProvider("p", "m")
        with self.assertRaises(LLMProviderError) as ctx:
            await provider.chat_completion("m", "s", "u")
        self.assertEqual(str(ctx.exception), GONE)

    async def test_other_http_errors_carry_status_only(self):
        provider = await self.ready_provider()
        url = self.fake.endpoints[1]["url"]

        def handler(request):
            return httpx.Response(429, text=f"slow down {url}")

        with patch.object(providers, "new_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))):
            with self.assertRaises(LLMProviderError) as ctx:
                await provider.chat_completion("m", "s", "u")
        self.assertEqual(str(ctx.exception), "MPCDF: p returned 429")

    async def test_503_triggers_one_refresh_then_reports_status(self):
        provider = await self.ready_provider()
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(503)

        with patch.object(providers, "new_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))):
            with self.assertRaises(LLMProviderError) as ctx:
                await provider.chat_completion("m", "s", "u")
        self.assertEqual(len(seen), 2)
        self.assertEqual(str(ctx.exception), "MPCDF: p returned 503")

    async def test_connect_error_retried_once(self):
        provider = await self.ready_provider()
        seen = []

        def handler(request):
            seen.append(request)
            if len(seen) == 1:
                raise httpx.ConnectError("boom http://fake.llmis/x")
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        with patch.object(providers, "new_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))):
            self.assertEqual(await provider.chat_completion("m", "s", "u"), "ok")
        self.assertEqual(len(seen), 2)

    async def test_other_transport_errors_not_retried(self):
        provider = await self.ready_provider()
        for exc in (httpx.ReadError("boom http://fake.llmis/x"), httpx.RemoteProtocolError("boom http://fake.llmis/x")):
            seen = []

            def handler(request, exc=exc):
                seen.append(request)
                raise exc

            with patch.object(providers, "new_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))):
                with self.assertRaises(LLMProviderError) as ctx:
                    await provider.chat_completion("m", "s", "u")
            self.assertEqual(len(seen), 1)
            self.assertEqual(str(ctx.exception), "Could not reach MPCDF: p")
            self.assertIsNone(ctx.exception.__cause__)
            self.assertTrue(ctx.exception.__suppress_context__)

    async def test_bound_provider_without_manager_reports_gone(self):
        provider = await self.ready_provider()
        await manager_module.drop_manager("alice")
        with self.assertRaises(LLMProviderError) as ctx:
            await provider.chat_completion("m", "s", "u")
        self.assertEqual(str(ctx.exception), GONE)

    async def test_raw_error_from_available_is_sanitized(self):
        provider = await self.ready_provider()

        async def boom():
            raise httpx.ConnectError("cannot reach http://secret.example/v1")

        with patch.object(self.manager, "available", boom):
            with self.assertRaises(LLMProviderError) as ctx:
                await provider.chat_completion("m", "s", "u")
        self.assertEqual(str(ctx.exception), "Could not reach MPCDF: p")
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    async def test_touch_failure_does_not_discard_reply(self):
        provider = await self.ready_provider()

        async def boom(**kw):
            raise RuntimeError("transient")

        with patch.object(self.manager, "touch", boom):
            self.assertEqual(await provider.chat_completion("m", "s", "u"), "hello alice")


class TestPluginLifecycle(_Base):
    async def test_initialize_twice_cancels_previous_watch_task(self):
        plugin = MpcdfLlmPlugin()
        await plugin.initialize(None)
        first = plugin._watch_task
        await plugin.initialize(None)
        self.assertTrue(first.cancelled())
        self.assertIsNot(plugin._watch_task, first)
        await plugin.cleanup()

    async def test_cleanup_unregisters_all(self):
        self.registry.register(OtherProvider())
        await self.start_ready("alice")
        plugin = MpcdfLlmPlugin()
        with patch.object(providers, "SYNC_INTERVAL_SECONDS", 0.01):
            await plugin.initialize(None)
            await providers.sync_providers()
            self.assertIn("mpcdf-llm-p", [p.id for p in self.registry.list_providers()])
            await plugin.cleanup()
        self.assertEqual([p.id for p in self.registry.list_providers()], ["other-plugin"])
        self.assertIsNone(plugin._watch_task)
