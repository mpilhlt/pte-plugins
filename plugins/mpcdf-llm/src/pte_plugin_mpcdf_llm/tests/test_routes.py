"""
Tests for the admin routes and the management page route, run against the library's FakeLlmis.

@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/routes.py
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from mpcdf_llm import names
from mpcdf_llm.manager import ProvisioningManager
from mpcdf_llm.testing import FakeLlmis

from fastapi_app.lib.core.dependencies import get_auth_manager, require_authenticated_user
from fastapi_app.plugin_api import get_session_manager
from pte_plugin_mpcdf_llm import manager as manager_module
from pte_plugin_mpcdf_llm import routes as routes_module
from pte_plugin_mpcdf_llm.routes import router
from pte_plugin_mpcdf_llm.tests.test_providers import PRESET

ADMIN = {"username": "admin", "roles": ["admin"]}
USER = {"username": "jane", "roles": ["user"]}
BASE = "/api/plugins/mpcdf-llm"


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
        patcher = patch.object(routes_module, "get_settings", lambda: SimpleNamespace(session_timeout=3600))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.fake = FakeLlmis()
        await self.fake.start()
        self.addAsyncCleanup(self.fake.stop)
        http = httpx.AsyncClient(transport=self.fake.http_transport())
        self.addAsyncCleanup(http.aclose)
        self.manager = ProvisioningManager(
            self.root / "mgr", base_url=self.fake.base_url, http=http, poll_interval=0.01
        )
        await self.manager.open()
        self.addAsyncCleanup(self.manager.close)
        # the routers resolve the manager through manager_module.get_manager
        patcher = patch.object(manager_module, "_manager", self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.user: dict | None = ADMIN
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[require_authenticated_user] = lambda: self.user
        self.session_valid = True
        self.app.dependency_overrides[get_session_manager] = lambda: SimpleNamespace(
            is_session_valid=lambda sid, timeout: self.session_valid and sid == "sid"
        )
        self.app.dependency_overrides[get_auth_manager] = lambda: SimpleNamespace(
            get_user_by_session_id=lambda sid, sm: self.user
        )
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.addAsyncCleanup(client.aclose)
        self.client = client

    async def start_ready(self) -> list[dict]:
        """Log in, then start the preset through the route; returns the parsed SSE frames."""
        await self.manager.login("alice", "pw", "123456")
        await self.manager.put_preset(PRESET)

        async def drive():
            while 1 not in self.fake.endpoints:
                await asyncio.sleep(0.005)
            for status in (names.BUILDING, names.RUNNING):
                await asyncio.sleep(0.05)
                self.fake.advance(1, status)

        driver = asyncio.create_task(drive())
        frames: list[dict] = []
        event = None
        async with self.client.stream("POST", f"{BASE}/admin/presets/p/start") as response:
            self.assertEqual(response.status_code, 200)
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line.partition(":")[2].strip()
                elif line.startswith("data:"):
                    frames.append({"event": event, "data": line.partition(":")[2].strip()})
        await driver
        return frames


class TestRoutes(_Base):
    async def test_admin_routes_forbidden_for_non_admin(self):
        self.user = USER
        self.assertEqual((await self.client.get(f"{BASE}/admin/presets")).status_code, 403)

    async def test_manage_requires_admin(self):
        self.assertEqual((await self.client.get(f"{BASE}/manage")).status_code, 401)
        self.user = USER
        self.assertEqual((await self.client.get(f"{BASE}/manage", params={"session_id": "sid"})).status_code, 403)
        self.user = ADMIN
        response = await self.client.get(f"{BASE}/manage", params={"session_id": "sid"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("MPCDF LLM", response.text)

    async def test_admin_presets_roundtrip(self):
        await self.manager.login("alice", "pw", "123456")  # saving validates against the live options
        body = PRESET.model_dump(mode="json")
        self.assertEqual((await self.client.put(f"{BASE}/admin/presets/p", json=body)).status_code, 200)
        listed = (await self.client.get(f"{BASE}/admin/presets")).json()
        self.assertEqual([p["name"] for p in listed], ["p"])
        self.assertEqual((await self.client.delete(f"{BASE}/admin/presets/p")).status_code, 204)
        self.assertEqual((await self.client.get(f"{BASE}/admin/presets")).json(), [])

    async def test_start_stream_reaches_ready(self):
        frames = await self.start_ready()
        self.assertTrue(frames)
        self.assertEqual(frames[-1]["event"], "ready")

    async def test_no_secrets_in_admin_responses(self):
        await self.start_ready()
        ep = self.fake.endpoints[1]
        secrets = [ep["key"], ep["token"], self.manager.client.session.token.get_secret_value()]
        for path in ("session", "endpoints", "presets"):
            response = await self.client.get(f"{BASE}/admin/{path}")
            self.assertEqual(response.status_code, 200, path)
            for secret in secrets:
                self.assertNotIn(secret, response.text, path)

    async def test_503_when_not_initialized(self):
        with patch.object(manager_module, "_manager", None):
            self.assertEqual((await self.client.get(f"{BASE}/admin/presets")).status_code, 503)

    async def test_no_llm_available_route(self):
        self.assertEqual((await self.client.get(f"{BASE}/llm/available")).status_code, 404)
