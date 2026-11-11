"""
Shared test setup: plugin state in a temporary directory, a real secret store, and managers wired
to the library's FakeLlmis (users alice and bob).
"""

import asyncio
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

from fastapi_app.lib.secrets import SecretStore
from fastapi_app.plugin_api import LLMProviderRegistry
from pte_plugin_mpcdf_llm import manager as manager_module
from pte_plugin_mpcdf_llm import providers
from pte_plugin_mpcdf_llm import routes as routes_module

PRESET = Preset(
    name="p", description="Big model", framework=Framework.VLLM, model="openai/gpt-oss-120b",
    machine="Viper", gpu_type="mi300a", gpus=1,
)
ORIGINAL_BASE_URL = manager_module.base_url  # PluginTestCase replaces it with the fake's URL
GONE = "MPCDF endpoint for p is no longer running"
PASSWORDS = {"alice": ("pw", "123456"), "bob": ("pw2", "654321")}
TIMEOUT = 10


class FakeConfig:
    """Stands in for the core config (get/delete)."""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def delete(self, key: str, with_metadata: bool = False) -> tuple[bool, str]:
        found = self.values.pop(key, None) is not None
        if with_metadata:
            for suffix in (".type", ".values", ".description"):
                self.values.pop(key + suffix, None)
        return found, ""


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
        return httpx.Response(200, json={"choices": [{"message": {"content": f"hello {ep['user']}"}}]})

    return httpx.MockTransport(handler)


async def until(condition) -> None:
    """Wait (bounded) until the condition holds."""
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.005)


class PluginTestCase(unittest.IsolatedAsyncioTestCase):
    """Plugin state under a temporary directory; managers are started (manager.start())."""

    start_plugin = True

    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = FakeConfig()
        self.store = SecretStore(self.root / "db")
        self.fake = FakeLlmis()
        self.fake.users.update(PASSWORDS)
        await self.fake.start()
        self.addAsyncCleanup(self.fake.stop)
        self.http = httpx.AsyncClient(transport=self.fake.http_transport())
        self.addAsyncCleanup(self.http.aclose)
        for module, target, value in (
            (manager_module, "get_settings", lambda: SimpleNamespace(plugins_data_dir=self.root)),
            (manager_module, "get_config", lambda: self.config),
            (manager_module, "base_url", lambda: self.fake.base_url),
            (manager_module, "get_secret_store", lambda: self.store),
            (routes_module, "get_secret_store", lambda: self.store),
            (manager_module, "_manager_options", {"http": self.http, "poll_interval": 0.01}),
        ):
            patcher = patch.object(module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        LLMProviderRegistry.reset_instance()
        providers._registered.clear()
        providers._ready.clear()
        self.addCleanup(LLMProviderRegistry.reset_instance)
        self.addCleanup(providers._registered.clear)
        self.addCleanup(providers._ready.clear)
        self.calls: list[httpx.Request] = []
        patcher = patch.object(
            providers, "new_http_client", lambda: httpx.AsyncClient(transport=chat_transport(self.fake, self.calls))
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.registry = LLMProviderRegistry.get_instance()
        if self.start_plugin:
            await manager_module.start()
            self.addAsyncCleanup(manager_module.stop)
            manager_module.catalog().put(PRESET)

    async def login(self, username: str) -> ProvisioningManager:
        """The user's manager, logged in to the fake."""
        manager = manager_module.manager_for(username)
        password, otp = PASSWORDS[username]
        await asyncio.wait_for(manager.login(username, password, otp), TIMEOUT)
        return manager

    async def start_ready(self, username: str, preset: str = "p") -> int:
        """Log the user in and bring a new endpoint of the preset to ready; returns its id."""
        manager = await self.login(username)
        endpoint_id = max(self.fake.endpoints, default=0) + 1

        async def drive():
            await until(lambda: endpoint_id in self.fake.endpoints)
            for status in (names.BUILDING, names.RUNNING):
                await asyncio.sleep(0.05)
                self.fake.advance(endpoint_id, status)

        driver = asyncio.create_task(drive())
        events = [e async for e in manager.start(preset)]
        await asyncio.wait_for(driver, TIMEOUT)
        self.assertEqual(events[-1].phase, "ready")
        return endpoint_id

    async def settle(self):
        await asyncio.sleep(0.05)  # beyond the manager's endpoint cache (poll_interval)
