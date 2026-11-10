# pte-plugins/mpcdf-llm — MPCDF LLM endpoints in pdf-tei-editor

Date: 2026-11-10 · Status: draft for review

## 1. Intent

The `mpcdf-llm` library (repo `https://gitlab.mpcdf.mpg.de/cboul/llm-mpcdf`,
merged at `ed3ed6b`) starts LLM endpoints on the MPCDF LLM Inference Service on
demand. It handles presets, login, reuse, renewal and idle cancel, and exposes
FastAPI routers. Read its README and its spec
(`docs/superpowers/specs/2026-11-10-mpcdf-llm-client-design.md` in that repo).

This plugin brings that library into pdf-tei-editor:

- **Admins** get one dialog where they:
  - log in to MPCDF,
  - manage presets (create, edit, delete),
  - start endpoints from presets and watch them come up,
  - see running endpoints and cancel them.
- **Every authenticated user** sees each ready endpoint's model in the inference
  menu as a **free** model and can use it like any other model.

The MPCDF account belongs to the admin who logs in (the user's decision in the
library design). Endpoints are free for the users of the app.

## 2. Goals

1. A pte-plugin `mpcdf-llm`, installable as an extra of pdf-tei-editor, that
   depends on the library from GitLab.
2. An admin-only dialog (login, presets, endpoints, start with a live status display).
3. Ready endpoints are registered as LLM providers whose models are `free=True`, in
   every worker process, and they disappear when the endpoint ends.
4. Model use reports activity to the library (`touch`), so idle cancel and renewal
   work as the library intends.
5. The library's supervisor runs inside pdf-tei-editor, exactly once across workers.

## 3. Non-goals and anti-goals

- No changes to pdf-tei-editor core. The core must not import the plugin (there is
  a guard test).
- No per-user MPCDF accounts, and no starting of endpoints by non-admins.
- No bundled copy of the library. It is a normal dependency.
- **Anti-goal:** an MPCDF password, OTP, session token or endpoint API key in any
  log, error message, HTTP response to a non-admin, or the browser of a non-admin.
  The API key is used server-side only, by the provider.
- **Anti-goal:** GPU time burned by the plugin itself (duplicate starts from several
  workers, endpoints kept alive without use). The library's guarantees must not be
  weakened by how the plugin wires it up.

## 4. Constraints (from pdf-tei-editor and pte-plugins)

- Python `>=3.13`. Use the plugin layout, `pyproject.toml` and lifecycle rules of
  `plugins/inference-endpoints` as the model:
  - `pte-plugin.json`, hatchling, `pte.plugins` entry point, tests excluded from
    the wheel;
  - `initialize`/`cleanup` must be symmetric and re-entrant (`check_lifecycle_conformance`).
- `routes.py` exports a module-level `router = APIRouter(prefix="/api/plugins/mpcdf-llm")`.
  It exists at import time, before `initialize`.
- Import from `fastapi_app.plugin_api` where possible. Type precisely.
- **Production runs 4 worker processes** (`bin/start-prod`). `initialize()` runs in
  each one. Plugin enable/disable or upgrade re-runs the hooks.
- Plugin settings go through `get_plugin_config(key, ENV_VAR, ..., masked=True)` and
  are documented in the plugin README.
- Plugin data lives in `get_settings().plugins_data_dir / "mpcdf-llm"`.
- Commits scoped `feat(mpcdf-llm): …` trigger a release; the scope must equal the
  plugin id.
- Dependency from git, following `plugins/llamore-extractor`: `"mpcdf-llm[fastapi]"`
  plus
  `[tool.uv.sources] mpcdf-llm = { git = "https://gitlab.mpcdf.mpg.de/cboul/llm-mpcdf.git" }`.
  The user said to assume it is installable from there. CI and Docker need HTTPS
  read access (see §9).

## 5. Architecture

### 5.1 One manager per worker

- `manager.py` (plugin) holds the current `ProvisioningManager(data_dir=plugins_data_dir/"mpcdf-llm", hf_token=lambda: get_config().get("plugin.mpcdf-llm.hf-token"), base_url=…)`.
  - **`initialize()` creates a fresh manager** in the running event loop, opens
    it and calls `start_supervisor()`.
  - `get_manager()` returns it and raises 503 when the plugin is not initialized.
  - The routers receive `get_manager` (library prerequisite §7.4), so they never
    hold a stale manager from an earlier loop.
- The library's file lock makes exactly one worker's supervisor active. The
  others retry each tick (§7.3).
- `cleanup()` unregisters this worker's providers, stops the supervisor (which
  releases the lock) and closes the manager. It **never cancels endpoints**: they
  keep running at MPCDF, and the next active supervisor takes them over through
  the follower lease (§7.1) (*my call*).
- All workers share the library's data dir: the session, presets, state and
  pending-submit markers are file-locked. So a login, a preset edit or a start in
  one worker is seen by the others.
- Each worker keeps its own websocket session to MPCDF. Concurrent sessions were
  verified to work.

### 5.2 Routes

- `router` includes the library's routers, with pdf-tei-editor's own access checks:
  - `admin_router(get_manager)` under `/admin`, with `Depends(require_admin_user)`;
  - `read_router(get_manager)` under `/llm`, with `Depends(require_authenticated_user)`.
- The routers receive the getter, not an instance, so they survive
  `initialize`/`cleanup`/`initialize`.
- Plus `GET /manage`, which serves the dialog HTML. It is admin only and checked
  manually with `user_is_admin`, as in `inference-endpoints`.
- Plugin metadata:
  - `category: "inference"`;
  - one endpoint `{"name": "manage", "label": "MPCDF LLM endpoints"}` returning
    `{"outputUrl": "/api/plugins/mpcdf-llm/manage"}`;
  - `required_roles: ["admin"]`.

### 5.3 Providers in the inference menu

- `providers.py`: each worker runs a sync task, `_watch()`, every 10 s (*my call*).
  It calls `manager.available()` and syncs `LLMProviderRegistry`:
  - **one provider per preset** (*my call*), id `mpcdf-llm-<preset>`, with
    exactly one model;
  - `available()` already leaves out a superseded endpoint once its replacement
    is ready, so a preset maps to one endpoint. During renewal the provider
    switches transparently: it resolves the credentials per request.
  - Presets sharing one Ollama endpoint are separate providers that use the same
    base URL.
- A provider is unregistered when its preset is no longer in `available()`.
- Label: `MPCDF: <preset name>` (*my call*).
- Model: `LLMModel(id=<model>, label="<preset description or name> (<model>)", capabilities={"chat"}, free=True)`.
- **Credentials:** the provider never stores the API key. On every request it looks
  up the current credentials for its preset from `manager.available()` (cached
  for ≤10 s), calls `/chat/completions` with them, and then calls
  `manager.touch(preset=…)`.
- **Usage limits:** none of the plugin's own. The host's existing rate limiting
  applies. Keeping an endpoint alive through use is intended: it's the activity
  signal (*my call*).
  - On 401 or a connection error it refreshes once from `available()`.
  - If the endpoint is gone it raises `LLMProviderError("MPCDF endpoint for <preset> is no longer running")`.
- The library's manager is async and provider calls run in the core's event loop.
  Use `httpx.AsyncClient` (already a dependency of the library), not `requests`.
- Image/PDF input: not supported in v1 (chat only).

### 5.4 The admin dialog (`static/manage.html`, iframe)

The same approach as `inference-endpoints/static/manage.html`: plain HTML/JS using
`sandbox.callPluginApi` and `sandbox.notify`. It has four sections.

1. **MPCDF session.**
   - The state comes from `GET /admin/session`. When `login_required`, a form
     appears: username, password and OTP, with
     `autocomplete="username|current-password|one-time-code"`.
   - Logout button. Shows when the session expires.
2. **Endpoints.**
   - A table from `GET /admin/endpoints`: preset (or "unmanaged"), model, status,
     time left, until, and a Cancel button (`DELETE /admin/endpoints/{id}`).
   - It refreshes every 10 s while open.
3. **Presets.**
   - A list with edit and delete. The edit form is built from `GET /admin/options`:
     framework, model, machine, GPU type, GPUs, time, queue time, image (optional),
     extra args, envs, min remaining, idle timeout.
   - `PUT /admin/presets/{name}` shows the 422 field errors next to their fields.
4. **Start.**
   - Preset select, optional "keep alive until" (HH:MM), and a Start button.
   - Reads the library's existing `POST /admin/presets/{name}/start?until=` SSE
     response with **`fetch` and a streaming body reader**, not `EventSource`
     (*my call*). That way no state-changing GET is needed, and the session goes
     in the `X-Session-ID` header the sandbox already uses, not in the URL.
     - It renders the phases (queued, building, verifying, pulling with progress,
       ready, failed).
   - The stream is tied to the worker that runs the job. A second tab, or a
     dropped stream, falls back to the 10 s table refresh, which shows status
     from the shared state. That is acceptable (*my call*).

No secrets are shown. The endpoints table never contains key or token (the library
route already omits them), and the page never calls `/llm/available`.

### 5.5 Configuration

- `plugin.mpcdf-llm.hf-token` / `HF_TOKEN`: masked. Only needed for gated
  Hugging Face models with vLLM.
- `plugin.mpcdf-llm.base-url` / `MPCDF_LLM_BASE_URL`: default
  `https://llm.mpcdf.mpg.de`. It exists for tests.

No other settings in v1. Idle timeout, renewal and the like are per preset.

## 6. Multi-worker behaviour (must hold)

- Only one supervisor is active. A worker restart must not start a second one
  (library lock).
- **No duplicate submit when two admins, or two tabs routed to different workers,
  start the same preset.** The library's pending-submit marker and state are
  file-locked. Its in-flight scan reads the shared state, so the second start
  attaches or follows instead of submitting.
  - Build a test that runs two managers on one data dir against the fake provider
    and starts the same preset from both. Expect exactly one SUBMIT.
  - If the library fails this test, fix the library (in its repo) rather than
    working around it in the plugin.
- Providers converge in every worker within the sync interval.

## 7. Prerequisites in the library repo (do these first)

Library checkout: `/Users/cboulanger/Code/llm-mpcdf-client`, remote
`git@gitlab.mpcdf.mpg.de:cboul/llm-mpcdf.git`, branch from `main` at `ed3ed6b`.
All of N1, N2, N3 below are **open at that HEAD**. N1 was introduced by commit
`efb5c00` (the final-review fix wave). Each item needs tests in the library's
suite (`uv run pytest`), using its fake `tests/fake_llmis.py`.

1. **Follower lease (fixes N1 and N3, and makes multi-worker safe).**
   - Today the supervisor decides whether an unready managed endpoint is being
     followed by looking at the job channels *in its own process* (`m._jobs`).
     With several workers, a start in worker A is invisible to the supervisor in
     worker B. B then adopts it as an orphan and follows it a second time, and
     the C1 "abort unready" logic in B can cancel a healthy endpoint that A is
     still following.
   - The same check is why N1 happens. After a restart, the `_stuck` rule counts
     from `submitted_at` and cancels a healthy Building endpoint more than
     `build_timeout` after submit, instead of adopting it.
   - Fix: add `follower_heartbeat_at: datetime | None` (and `follower_id`: pid plus
     a random token) to `ManagedEndpoint`.
     - Every job that follows an endpoint (start, adopt, renewal) refreshes it at
       each poll.
     - The supervisor treats an endpoint as followed if the heartbeat is younger
       than `3 × poll_interval` (minimum 30 s), in any process. It adopts only
       when the heartbeat is stale or missing.
     - The stuck/abort-unready rules apply only to an endpoint that has no live
       follower **and** whose own follow attempt has already failed. An endpoint
       that was never adopted is always adopted first (that is the N1 fix).
     - Shared Ollama endpoints are checked by endpoint, not by owner preset
       (the N3 fix).
   - Acceptance tests (library):
     - `test_restart_during_long_build_adopts_not_cancels`: submitted 40 min ago,
       Building, no heartbeat; one tick adopts it and leaves it running.
     - `test_supervisor_skips_endpoint_followed_by_other_process`: fresh heartbeat
       written by a second manager on the same data dir; the tick neither adopts
       nor cancels it.
     - `test_dead_follower_is_taken_over`: heartbeat stale; the tick adopts.
     - `test_two_managers_same_preset_one_submit`: two managers, one data dir,
       concurrent `start` → exactly one SUBMIT, and both report ready.
2. **N2:** a guest preset's failed pull cancels the owner's shared endpoint
   (`manager._abort_unready`). Fix: abort only when the failing job's preset is
   the owner.
   - Test: `test_guest_pull_failure_does_not_cancel_owner_endpoint`.
3. **Passive supervisor takeover.** Today a supervisor that failed to get the file
   lock stays passive forever. Fix: a passive supervisor retries the non-blocking
   lock acquisition every tick. `stop_supervisor()` releases the lock.
   - Test: `test_passive_supervisor_takes_over_when_lock_released`.
   - Needed because pdf-tei-editor restarts workers on plugin changes and the
     worker that holds the lock may exit first.
4. **Routers accept a manager getter.** `admin_router` and `read_router` accept
   either a `ProvisioningManager` or a `Callable[[], ProvisioningManager]`, resolved
   per request. This lets the plugin create a fresh manager in each `initialize()`
   (bound to the running event loop) while its module-level `router` is built at
   import time. Test both forms.
5. **Ship the test fake** as `mpcdf_llm.testing` (move `tests/fake_llmis.py` into the
   package). Add a `[testing]` extra with `aiohttp`. The library's own tests import
   it from there.
6. Tag a release `v0.2.0`. **Pushing and tagging on GitLab is outward-facing: ask
   the user before doing it.** Until the tag exists, the plugin develops against the
   local checkout via a uv path source override (`mpcdf-llm = { path = "../../../llm-mpcdf-client", editable = true }`
   in a local, uncommitted override, or a dev-only `[tool.uv.sources]` the builder
   documents). The committed plugin pins the git tag.

Order: library items 1–5, then release, then the plugin. The plugin may start in
parallel against the path source.

## 8. Tests

- **Unit (pytest, `src/pte_plugin_mpcdf_llm/tests/`)** against `mpcdf_llm.testing.FakeLlmis`:
  - Provider sync registers and unregisters on ready, replaced and expired.
  - Models are `free=True`.
  - The provider fetches fresh credentials and touches activity.
  - It refreshes after a 401.
  - It raises a clear error when the endpoint is gone.
- **Routes:**
  - Admin routes give 403 for non-admins.
  - `/manage` is admin only.
  - The POST start stream works.
  - No key or token in any response.
- **Lifecycle:** `initialize`, then `cleanup`, then `initialize` under a *new*
  event loop works. A fresh manager is created each time.
- **Lifecycle:** `check_lifecycle_conformance`.
- **Two workers:** two plugin instances (two managers on one data dir), one with
  the active supervisor:
  - a start in the passive one yields one SUBMIT;
  - the endpoint ends `ready`;
  - the supervisor does not adopt or cancel it while the starter's heartbeat is
    fresh;
  - it is taken over once the starter is closed.
- **API test** (`tests/api/mpcdf-llm.test.js`, node:test):
  - admin vs. user access;
  - the provider appears in `GET /api/v1/llm/providers` with a free model once
    an endpoint is ready.

  This needs the fake provider running. The plugin reads `MPCDF_LLM_BASE_URL`, so
  the test server can point at the fake.
- **E2E:** one smoke spec. An admin opens the dialog and sees the login form. The
  real login cannot run in CI.
- **Live:** none in CI. Document a manual check in the README:
  1. log in;
  2. start the cheapest preset;
  3. see it in the inference menu as another user;
  4. cancel it.

## 9. Integration in pdf-tei-editor

- Add the extra `mpcdf-llm = ["pte-plugin-mpcdf-llm"]` and its `[tool.uv.sources]`
  entry (pte-plugins git, `subdirectory = "plugins/mpcdf-llm"`, pinned `rev`) to
  `pdf-tei-editor/pyproject.toml`.
- Update `uv.lock`.
- Not part of the `default` bundle (*my call*: it needs an MPCDF account).
- The library's GitLab repo will be **world-readable over HTTPS** (the user's
  decision, pending the MPCDF maintainer's consent), so no deploy token is planned.
  Until it is public, local development uses the path source (§7.6). The
  pdf-tei-editor extra is added only once the repo is readable.
- The runtime Plugin Manager cannot install git-only dependencies. Document that
  the plugin must be installed at build time.
- Add the plugin to the pte-plugins README table and regenerate the index.

## 10. Open for the user / left to the builder

- **Answered:** the library repo will be public over HTTPS (§9). Order: the
  library prerequisites (§7) come first, as the user asked.
- **Builder:**
  - sync interval;
  - the dialog's layout details within the four sections.
