# mpcdf-llm plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pte-plugin `mpcdf-llm` that brings the `mpcdf-llm` library into pdf-tei-editor: an admin dialog (MPCDF login, presets, start, endpoints) and every ready endpoint as a free model in the inference menu.

**Architecture:** One `ProvisioningManager` per worker, created in `initialize()` and reached through `get_manager()`. The library's admin router is mounted under the plugin router behind `require_admin_user`. A per-worker sync task turns `manager.available()` into one `LLMProvider` per preset. The dialog is a static HTML page in an iframe, like `inference-endpoints`.

**Tech Stack:** Python 3.13, FastAPI, httpx, `mpcdf-llm` (git), hatchling, node:test, Playwright (editor runners).

**Spec:** `docs/superpowers/specs/2026-11-10-mpcdf-llm-plugin-design.md`

## Global Constraints

- Plugin id `mpcdf-llm`, directory `plugins/mpcdf-llm/`, package `pte_plugin_mpcdf_llm`, distribution `pte-plugin-mpcdf-llm`, version `1.0.0` in `pte-plugin.json` (single source; copy `engines` from `plugins/inference-endpoints`).
- Layout, `pyproject.toml`, entry point, test exclusion from the wheel: as `plugins/inference-endpoints`.
- Dependency: `"mpcdf-llm[fastapi,testing]"`, with `[tool.uv.sources] mpcdf-llm = { git = "https://gitlab.mpcdf.mpg.de/cboul/llm-mpcdf.git", rev = "334a40ade43e79e4d67a8f65601e16667e5ced8a" }`.
- Import editor functionality from `fastapi_app.plugin_api` where it exists there.
- `router = APIRouter(prefix="/api/plugins/mpcdf-llm", tags=["mpcdf-llm"])` exists at import time.
- Data dir: `get_settings().plugins_data_dir / "mpcdf-llm"`.
- Config: `get_plugin_config("plugin.mpcdf-llm.hf-token", "HF_TOKEN", masked=True, …)` and `get_plugin_config("plugin.mpcdf-llm.base-url", "MPCDF_LLM_BASE_URL", default="https://llm.mpcdf.mpg.de", …)`.
- Never in a log, error, non-admin response or non-admin browser: MPCDF password, OTP, session token, endpoint API key. The provider keeps no API key between requests.
- `cleanup()` never cancels endpoints. `initialize()`/`cleanup()` are symmetric and re-entrant.
- Commits: `feat(mpcdf-llm): …` / `test(mpcdf-llm): …` / `docs(mpcdf-llm): …`.
- Tests run in the editor's environment: `PTE_ROOT=/Users/cboulanger/Code/pdf-tei-editor "$PTE_ROOT/.venv/bin/python" -m pytest plugins/mpcdf-llm`. The package must be installed into it first: `uv pip install --python "$PTE_ROOT/.venv/bin/python" -e plugins/mpcdf-llm` (from the worktree root).
- Library reference (read, never edit): `/Users/cboulanger/Code/llm-mpcdf-client` — `README.md`, `src/mpcdf_llm/manager.py`, `web.py`, `testing.py`, and `tests/conftest.py` for how a manager is wired to `FakeLlmis` (`base_url=fake.base_url`, `http=httpx.AsyncClient(transport=fake.http_transport())`, `poll_interval` small).

## Rulings made while planning (spec deviations)

- **The read router is not mounted.** `GET /llm/available` returns API keys; mounting it for every authenticated user breaks the spec's anti-goal. The provider touches activity server-side, so nothing needs it.
- **Pin by commit, not tag.** The repo is public, and a `rev` pin needs no outward tagging step. The user may later swap it for a tag.
- **The `testing` extra is a runtime dependency.** `aiohttp` is not in the editor environment, and `dev:install` and CI install plugins without extras. The plugin is opt-in, so the extra weight is acceptable.
- **The API test does not cover "provider appears".** The fake serves endpoint HTTP only as an in-process `httpx.MockTransport`, so a separately running editor cannot reach it. Python tests cover registration instead.
- **`initialize()` does not block on the network.** `manager.open()` may restore a session over the websocket. So `initialize()` creates the manager and starts a background task that runs `open()` and then `start_supervisor()`. `cleanup()` cancels that task before it closes the manager.

## Review Focus

1. **No MPCDF login:** every route except login/session/presets/options answers a clear `login_required` error, and the provider sync registers nothing without crashing the task. Test: sync with a manager that has no session leaves the registry empty, and the sync task keeps running.
2. **Endpoint ended mid-request:** chat raises `LLMProviderError("MPCDF endpoint for <preset> is no longer running")`, not a raw httpx error that names the URL. Test in Task 2.
3. **Non-admin access:** `/admin/*` and `/manage` give 403 to a plain user and 401 without a session. Test in Task 3.
4. **Plugin restarted under a new loop:** after `cleanup()`, a later `initialize()` in another event loop gets a new manager, and the routes use it. Test in Task 1.
5. **Secrets in responses:** no admin route response or log record during start/list carries `api_key`, `key` or `token` values. Test in Task 3 (the start stream's `ready` frame is the one exception the library makes for admins; the dialog must not render it).

---

### Task 1: Package skeleton, manager lifecycle, config

**Files:**
- Create: `plugins/mpcdf-llm/pyproject.toml`, `plugins/mpcdf-llm/README.md` (stub, filled in Task 5), `src/pte_plugin_mpcdf_llm/{__init__.py,pte-plugin.json,plugin.py,manager.py,routes.py}` (routes: only `router`)
- Test: `src/pte_plugin_mpcdf_llm/tests/{__init__.py,test_plugin.py}`

**Interfaces:**
- Produces, in `manager.py`:
  - `PLUGIN_ID = "mpcdf-llm"`
  - `def data_dir() -> Path`
  - `def get_manager() -> ProvisioningManager`, which raises `LookupError` when no manager is current (the library maps that to 503)
  - `async def start_manager() -> ProvisioningManager`
  - `async def stop_manager() -> None`
  - `def base_url() -> str`
  - `def hf_token() -> str | None`
- Produces, in `plugin.py`: `class MpcdfLlmPlugin(Plugin)` with metadata as in spec §5.2 (`category "inference"`, endpoint `manage`, `required_roles ["admin"]`), `manage()` returning `{"outputUrl": "/api/plugins/mpcdf-llm/manage"}`, and `initialize`/`cleanup`.
- `start_manager()` builds `ProvisioningManager(data_dir=data_dir(), base_url=base_url(), hf_token=hf_token)` and sets it current. It then spawns a task that runs `await m.open()` followed by `m.start_supervisor()`.
- `stop_manager()` cancels that task, closes the manager and clears it. It never calls `stop()` on endpoints.

- [ ] **Step 1: Tests (`test_plugin.py`)**
  - `test_get_manager_raises_lookup_error_before_initialize`
  - `test_initialize_creates_manager_and_cleanup_clears_it`: after `cleanup()`, `get_manager()` raises `LookupError`.
  - `test_reinitialize_in_new_event_loop_gets_fresh_manager`: two separate `asyncio.run(...)` calls; the second manager `is not` the first, and both open and close without error.
  - `test_cleanup_does_not_cancel_endpoints`: patch `ProvisioningManager.stop` to fail if called.
  - `test_base_url_from_env`: `MPCDF_LLM_BASE_URL` overrides the default.
  - `test_data_dir_under_plugins_data_dir`
  - All of them use a temp `DATA_ROOT`/data dir and no stored session, so nothing touches the network.
- [ ] **Step 2:** Run the tests and watch them fail. Implement. Install the package into the editor venv (see Global Constraints) and run the tests until they pass.
- [ ] **Step 3:** The full repo suite passes (`tests/test_packages.py` includes the lifecycle conformance check for every plugin): `"$PTE_ROOT/.venv/bin/python" -m pytest -q`. If `test_packages` requires an index entry, run `python scripts/build_index.py` (the README table comes in Task 5).
- [ ] **Step 4:** Commit `feat(mpcdf-llm): plugin skeleton with per-worker provisioning manager`.

### Task 2: Providers in the inference menu

**Files:**
- Create: `src/pte_plugin_mpcdf_llm/providers.py`
- Modify: `plugin.py` (start/stop the sync task; unregister on cleanup)
- Test: `tests/test_providers.py`

**Interfaces:**
- Consumes: `get_manager()` from Task 1, and `ProvisioningManager.available() -> list[Available]` (`Available.preset`, `.model`, `.credentials.base_url`, `.credentials.api_key: SecretStr`, `.credentials.endpoint_id`), `touch(preset=)`.
- Produces:
  - `SYNC_INTERVAL_SECONDS = 10.0`
  - `class MpcdfLLMProvider(LLMProvider)`: `id = f"mpcdf-llm-{preset}"`, `label = f"MPCDF: {preset}"`. `list_models()` returns one `LLMModel(id=model, label=f"{description or preset} ({model})", capabilities=frozenset({"chat"}), status=None, free=True)`. `supports_image_input` and `supports_pdf_input` return False.
  - `async def sync_providers(manager) -> None`: register a provider per preset in `available()`, unregister vanished ones, and leave other plugins' providers alone. `LoginRequired` and other `MpcdfError`s count as "nothing available".
  - `def unregister_all() -> None`
  - `async def watch() -> None`: the loop in `plugin.py`'s task, surviving errors like `inference-endpoints._watch`.
- Credentials per request: `chat_completion` gets the current `Available` for its preset from `manager.available()`, cached ≤10 s per provider. It POSTs `{base_url}/chat/completions` with `httpx.AsyncClient` (`Authorization: Bearer <key>`, non-streaming, response as in `inference-endpoints._chat_completion_sync`). On success it calls `await manager.touch(preset=…)`; it ignores `NotFound` from touch.
  - **401 or a connection error:** drop the cache, fetch again once, and retry.
  - **Preset not available (at first or after the refresh):** raise `LLMProviderError(f"MPCDF endpoint for {preset} is no longer running")`.
  - **Other HTTP errors:** `LLMProviderError(f"{label} returned {status}")`, with no URL and no key in the message.
- The manager in tests is wired to `FakeLlmis` (see Global Constraints) with a logged-in session. Read how the library's tests log in and bring an endpoint to Running: `fake.advance(id, "Running")`, plus Ollama pulls or vLLM served models via `http_transport`. `get_manager` is patched to return it.

- [ ] **Step 1: Tests (`test_providers.py`)**
  - `test_sync_registers_ready_preset_as_free_model`
  - `test_sync_unregisters_when_endpoint_ends`
  - `test_sync_keeps_other_plugins_providers`
  - `test_sync_without_login_registers_nothing`
  - `test_chat_uses_fresh_credentials_and_touches`: `last_activity` advances in `manager.state`.
  - `test_chat_refreshes_once_after_401`: the fake's endpoint key changes, or the first `Available` is stale.
  - `test_chat_raises_clear_error_when_endpoint_gone`: the message equals the spec text and contains no URL.
  - `test_cleanup_unregisters_all`
- [ ] **Step 2:** Watch them fail, implement, watch them pass. Run the full suite.
- [ ] **Step 3:** Commit `feat(mpcdf-llm): ready endpoints as free models in the inference menu`.

### Task 3: Routes

**Files:**
- Modify: `routes.py`
- Test: `tests/test_routes.py`

**Interfaces:**
- `router.include_router(admin_router(get_manager), prefix="/admin", dependencies=[Depends(require_admin_user)])`, where `admin_router` comes from `mpcdf_llm.web`.
- `GET /manage` serves `static/manage.html` through `load_plugin_html`. It is admin only, checked by hand exactly as in `inference-endpoints/routes.py:manage_page`. The file is a placeholder until Task 4.
- No read router (see Rulings).

- [ ] **Step 1: Tests (`test_routes.py`)**
  - Use a FastAPI app containing `router`, with `dependency_overrides` for the user, as in `inference-endpoints/tests/test_routes.py`.
  - `test_admin_routes_forbidden_for_non_admin`: `GET /admin/presets` gives 403.
  - `test_manage_requires_admin`: 401 without a session, 403 for a user.
  - `test_admin_presets_roundtrip`: PUT, GET, DELETE a preset as admin.
  - `test_start_stream_reaches_ready`: run against the fake, as admin. Read the SSE frames up to `ready`.
  - `test_no_secrets_in_admin_responses`: the bodies of `/admin/session`, `/admin/endpoints` and `/admin/presets` after a ready start contain neither the fake endpoint's `key` nor `token`, nor the session's auth token.
  - `test_503_when_not_initialized`: with no current manager, `/admin/presets` gives 503.
  - `test_no_llm_available_route`: `/llm/available` gives 404.
- [ ] **Step 2:** Watch them fail, implement, watch them pass. Run the full suite.
- [ ] **Step 3:** Commit `feat(mpcdf-llm): admin routes and management page route`.

### Task 4: Admin dialog `static/manage.html`

**Files:**
- Create: `src/pte_plugin_mpcdf_llm/static/manage.html`
- Test: `tests/manage.test.js` (node:test + jsdom, if other plugins test their static pages that way; look for an existing `*.test.js` under `plugins/*/src/*/tests/` that loads an HTML page, and follow it. Otherwise test the page's pure helpers in a `<script type="module">` split into `static/manage.js` with exports.)

**Requirements:** spec §5.4, with the same sandbox use as `inference-endpoints/static/manage.html` (`sandbox.callPluginApi`, `sandbox.notify`; read how it gets the session id for raw `fetch`). Four sections:

1. **Session.**
   - The form's fields carry `autocomplete="username"`, `"current-password"` and `"one-time-code"`.
   - Clear the password and OTP fields after submit.
   - Show the expiry time. Logout button.
2. **Endpoints table.**
   - Columns: preset or "unmanaged", model, status, time left, until, Cancel.
   - Refresh every 10 s, but only while the page is visible.
3. **Presets.**
   - List with edit and delete.
   - The form fields come from `/admin/options`.
   - Show 422 `detail[].loc`/`msg` next to the field each one names.
4. **Start.**
   - Preset select, optional HH:MM "keep alive until" (converted to an ISO datetime for today, or tomorrow if the time has passed), Start button.
   - POST with `fetch` and read the SSE body with a stream reader. Render each phase and the pull progress.
   - **Never render `result` or any api key from the `ready` frame.**

- [ ] **Step 1:** Tests for the testable helpers:
  - SSE frame parsing across chunk boundaries;
  - `until` conversion for a time that has passed (tomorrow) and one that hasn't (today);
  - the `ready` frame renders without its `result`.
- [ ] **Step 2:** Implement. `npm test` passes. Load the page by hand only if an editor dev server is running; otherwise say so in the report.
- [ ] **Step 3:** Commit `feat(mpcdf-llm): admin dialog for session, presets, start and endpoints`.

### Task 5: Multi-worker test, API/E2E tests, docs, index

**Files:**
- Test: `tests/test_two_workers.py`, `tests/api/mpcdf-llm.test.js`, `tests/e2e/mpcdf-llm.spec.js`
- Modify: `plugins/mpcdf-llm/README.md`, root `README.md` (plugin table), `pte-plugins.json` (via `python scripts/build_index.py`)

- [ ] **Step 1: `test_two_workers.py`.** Two `ProvisioningManager`s on one data dir, both against the same `FakeLlmis`. A is the active supervisor; B is passive and does the start.
  - `test_start_in_passive_worker_submits_once_and_ready`: exactly one SUBMIT in `fake.received`.
  - `test_active_supervisor_leaves_followed_endpoint_alone`: ticks of A while B follows neither adopt nor cancel.
  - `test_taken_over_after_starter_closes`: close B before the endpoint is ready; A's tick adopts it and it ends ready.
  - `test_providers_converge_in_both_workers`: `sync_providers` on each manager registers the preset.
  - The library already has similar tests in `/Users/cboulanger/Code/llm-mpcdf-client/tests/`; reuse their setup.
  - If a test exposes a library bug, stop and report it (`BLOCKED`). Do not work around it in the plugin.
- [ ] **Step 2: API test** (node:test, the style of `inference-endpoints/tests/api/inference-endpoints.test.js`). As a basic user: 403 on `/admin/presets` and on `/manage`. As admin: 200 on `/admin/presets` and `/manage`. `GET /admin/session` returns `state: "login_required"`. Never call routes that reach MPCDF.
- [ ] **Step 3: E2E smoke** (follow an existing `tests/e2e/*.spec.js`). An admin opens the plugin dialog and sees the login form fields. Run both through the editor's runners if they work locally (README "Development"); otherwise report that they were not run.
- [ ] **Step 4: Docs.**
  - Plugin README, following `inference-endpoints/README.md`: what it does, the settings (`hf-token`, `base-url`), install at build time only (git dependency; the runtime Plugin Manager cannot install it), the multi-worker notes, and the manual live check from spec §8.
  - Add the README table row and run `python scripts/build_index.py`.
- [ ] **Step 5:** The full suite and `npm test` pass. Commit `docs(mpcdf-llm): README, index; test(mpcdf-llm): two-worker, API and E2E tests` (split as you see fit).
