"""
Tests for the routes and the management page route, run against the library's FakeLlmis.

Every user (alice, bob: plain users; admin) acts on their own manager; the editor user "alice"
logs in to the fake as MPCDF user "alice", and so on.

@testCovers plugins/mpcdf-llm/src/pte_plugin_mpcdf_llm/routes.py
"""

import asyncio

import httpx
from fastapi import FastAPI
from mpcdf_llm import names

from fastapi_app.plugin_api import require_authenticated_user
from pte_plugin_mpcdf_llm import manager as manager_module
from pte_plugin_mpcdf_llm.manager import HF_TOKEN_SECRET, SESSION_SECRET
from pte_plugin_mpcdf_llm.routes import router

from .support import PASSWORDS, PRESET, PluginTestCase, until

ADMIN = {"username": "admin", "roles": ["admin"]}
ALICE = {"username": "alice", "roles": ["user"]}
BOB = {"username": "bob", "roles": ["user"]}
BASE = "/api/plugins/mpcdf-llm"
PASSWORDS_WITH_ADMIN = {**PASSWORDS, "admin": ("pw3", "111111")}


class _Base(PluginTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake.users.update(PASSWORDS_WITH_ADMIN)
        self.user: dict | None = ALICE
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[require_authenticated_user] = lambda: self.user
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.addAsyncCleanup(client.aclose)
        self.client = client

    async def route_login(self, user: dict) -> None:
        self.user = user
        password, otp = PASSWORDS_WITH_ADMIN[user["username"]]
        body = {"username": user["username"], "password": password, "otp": otp}
        response = await self.client.post(f"{BASE}/my/session/login", json=body)
        self.assertEqual(response.status_code, 200, response.text)

    async def route_start_ready(self, user: dict) -> list[dict]:
        """Log the user in and start the preset through the route; returns the parsed SSE frames."""
        await self.route_login(user)
        endpoint_id = max(self.fake.endpoints, default=0) + 1

        async def drive():
            await until(lambda: endpoint_id in self.fake.endpoints)
            for status in (names.BUILDING, names.RUNNING):
                await asyncio.sleep(0.05)
                self.fake.advance(endpoint_id, status)

        driver = asyncio.create_task(drive())
        frames: list[dict] = []
        event = None
        async with self.client.stream("POST", f"{BASE}/my/presets/p/start") as response:
            self.assertEqual(response.status_code, 200)
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event = line.partition(":")[2].strip()
                elif line.startswith("data:"):
                    frames.append({"event": event, "data": line.partition(":")[2].strip()})
        await driver
        return frames


class TestOwnSession(_Base):
    async def test_every_user_logs_in_to_their_own_session(self):
        await self.route_login(ALICE)
        self.user = BOB
        self.assertEqual((await self.client.get(f"{BASE}/my/session")).json()["state"], "login_required")
        self.user = ALICE
        session = (await self.client.get(f"{BASE}/my/session")).json()
        self.assertEqual((session["state"], session["username"]), ("ok", "alice"))
        self.assertTrue(self.store.has("alice", SESSION_SECRET))
        self.assertFalse(self.store.has("bob", SESSION_SECRET))

    async def test_start_stream_reaches_ready_for_plain_user(self):
        frames = await self.route_start_ready(ALICE)
        self.assertEqual(frames[-1]["event"], "ready")
        self.assertEqual(self.fake.endpoints[1]["user"], "alice")

    async def test_users_see_and_cancel_only_their_own_endpoints(self):
        await self.route_start_ready(ALICE)
        await self.route_login(BOB)
        self.assertEqual((await self.client.get(f"{BASE}/my/endpoints")).json(), [])
        self.assertEqual((await self.client.get(f"{BASE}/managed")).json(), [])
        await self.client.delete(f"{BASE}/my/endpoints/1")
        self.assertEqual(self.fake.endpoints[1]["status"], names.RUNNING)
        self.user = ALICE
        self.assertEqual([e["id"] for e in (await self.client.get(f"{BASE}/my/endpoints")).json()], [1])

    async def test_no_secrets_in_responses(self):
        await self.route_start_ready(ALICE)
        ep = self.fake.endpoints[1]
        session = manager_module.existing_manager("alice").client.session.token.get_secret_value()
        for path in ("my/session", "my/endpoints", "my/presets", "managed", "context"):
            response = await self.client.get(f"{BASE}/{path}")
            self.assertEqual(response.status_code, 200, path)
            for secret in (ep["key"], ep["token"], session):
                self.assertNotIn(secret, response.text, path)

    async def test_managed_lists_preset_and_until_only(self):
        await self.route_start_ready(ALICE)
        rows = (await self.client.get(f"{BASE}/managed")).json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"endpoint_id", "preset", "presets", "until"})
        self.assertEqual((rows[0]["endpoint_id"], rows[0]["preset"]), (1, "p"))

    async def test_503_when_not_initialized(self):
        await manager_module.stop()
        self.assertEqual((await self.client.get(f"{BASE}/my/presets")).status_code, 503)
        self.assertEqual((await self.client.get(f"{BASE}/managed")).status_code, 503)

    async def test_no_llm_available_route(self):
        self.assertEqual((await self.client.get(f"{BASE}/llm/available")).status_code, 404)
        self.assertEqual((await self.client.get(f"{BASE}/my/available")).status_code, 404)

    async def test_context(self):
        context = (await self.client.get(f"{BASE}/context")).json()
        self.assertEqual(context, {"username": "alice", "is_admin": False, "hf_token_stored": False})
        self.store.set("alice", HF_TOKEN_SECRET, "hf_x")
        self.user = ADMIN
        self.assertTrue((await self.client.get(f"{BASE}/context")).json()["is_admin"])
        self.user = ALICE
        self.assertTrue((await self.client.get(f"{BASE}/context")).json()["hf_token_stored"])

    async def test_manage_page_for_every_user(self):
        for user in (ALICE, ADMIN):
            self.user = user
            response = await self.client.get(f"{BASE}/manage")
            self.assertEqual(response.status_code, 200)
            self.assertIn("My MPCDF endpoints", response.text)


class TestCatalog(_Base):
    async def test_users_read_but_cannot_change_presets(self):
        await self.route_login(ALICE)
        self.assertEqual([p["name"] for p in (await self.client.get(f"{BASE}/my/presets")).json()], ["p"])
        body = PRESET.model_dump(mode="json")
        self.assertEqual((await self.client.put(f"{BASE}/my/presets/p", json=body)).status_code, 403)
        self.assertEqual((await self.client.delete(f"{BASE}/my/presets/p")).status_code, 403)
        self.assertEqual(manager_module.catalog().get("p").model, PRESET.model)

    async def test_admin_presets_roundtrip_with_own_session(self):
        await self.route_login(ADMIN)  # saving validates against the live options
        body = {**PRESET.model_dump(mode="json"), "name": "q"}
        self.assertEqual((await self.client.put(f"{BASE}/my/presets/q", json=body)).status_code, 200)
        self.user = BOB
        self.assertEqual([p["name"] for p in (await self.client.get(f"{BASE}/my/presets")).json()], ["p", "q"])
        self.user = ADMIN
        self.assertEqual((await self.client.delete(f"{BASE}/my/presets/q")).status_code, 204)

    async def test_import_presets_without_session(self):
        self.user = ADMIN
        body = [{**PRESET.model_dump(mode="json"), "name": "q"}, {"name": "bad", "gpus": "many"}, {"model": "x"}]
        result = (await self.client.post(f"{BASE}/presets/import", json=body)).json()
        self.assertEqual(result["saved"], ["q"])
        self.assertEqual(set(result["errors"]), {"bad", "#3"})
        self.assertEqual(manager_module.catalog().get("q").model, PRESET.model)

    async def test_import_presets_forbidden_for_non_admin(self):
        response = await self.client.post(f"{BASE}/presets/import", json=[PRESET.model_dump(mode="json")])
        self.assertEqual(response.status_code, 403)


class TestAdminOverview(_Base):
    async def test_overview_lists_users_without_secrets(self):
        await self.route_start_ready(ALICE)
        await self.route_login(BOB)
        self.user = ADMIN
        response = await self.client.get(f"{BASE}/overview")
        self.assertEqual(response.status_code, 200)
        users = {u["username"]: u for u in response.json()}
        self.assertEqual(set(users), {"alice", "bob"})
        self.assertTrue(users["alice"]["has_session"])
        self.assertEqual([(e["endpoint_id"], e["preset"], e["ready"]) for e in users["alice"]["endpoints"]], [(1, "p", True)])
        self.assertEqual(users["bob"]["endpoints"], [])
        ep = self.fake.endpoints[1]
        for secret in (ep["key"], ep["token"], self.store.get("alice", SESSION_SECRET)):
            self.assertNotIn(secret, response.text)

    async def test_overview_and_session_removal_admin_only(self):
        self.assertEqual((await self.client.get(f"{BASE}/overview")).status_code, 403)
        self.assertEqual((await self.client.delete(f"{BASE}/users/bob/session")).status_code, 403)

    async def test_admin_removes_a_users_session(self):
        await self.route_start_ready(ALICE)
        self.user = ADMIN
        self.assertEqual((await self.client.delete(f"{BASE}/users/alice/session")).status_code, 204)
        self.assertFalse(self.store.has("alice", SESSION_SECRET))
        self.assertIsNone(manager_module.existing_manager("alice"))
        self.assertEqual(self.fake.endpoints[1]["status"], names.RUNNING)  # never cancelled for the user
        self.user = ALICE
        self.assertEqual((await self.client.get(f"{BASE}/my/session")).json()["state"], "login_required")


class TestModelPicker(_Base):
    """The core's GET /api/v1/llm/providers lists a user's own MPCDF endpoints, and only to that user."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from fastapi_app.routers.llm import router as llm_router

        self.app.include_router(llm_router, prefix="/api/v1")

    async def providers_of(self, user: dict) -> list[str]:
        self.user = user
        response = await self.client.get("/api/v1/llm/providers")
        self.assertEqual(response.status_code, 200)
        return [p["label"] for p in response.json() if p["models"]]

    async def test_only_the_starting_user_sees_the_model(self):
        from pte_plugin_mpcdf_llm import providers

        await self.route_start_ready(ALICE)
        await self.route_login(BOB)
        await providers.sync_providers()
        self.assertIn("MPCDF: p", await self.providers_of(ALICE))
        self.assertNotIn("MPCDF: p", await self.providers_of(BOB))
        self.assertNotIn("MPCDF: p", await self.providers_of(ADMIN))
