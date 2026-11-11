# MPCDF LLM Plugin

Lets every user start LLM endpoints on the [MPCDF LLM service](https://llm.mpcdf.mpg.de) (GPU jobs on the MPCDF clusters) with their own MPCDF account, and offers each of the user's ready endpoints as a model to that user wherever the core LLM provider registry is used (for example Tools > Default Model, the [Annotation Review](../annotation-review/README.md) plugin and the [LLamore extractor](../llamore-extractor/README.md)).

The MPCDF Terms of Use do not allow users to share a session, so the plugin keeps one session per user and never lets anyone use another user's session or endpoints.

The plugin is a thin wrapper around the [mpcdf-llm](https://gitlab.mpcdf.mpg.de/cboul/llm-mpcdf) library, which logs in to the service, submits and follows the jobs, renews them before they expire and cancels them when idle.

## Usage

Every user opens the management page from the plugin menu (Inference > My MPCDF endpoints). The dialog has these parts:

- **MPCDF session.** Log in with your own MPCDF user name, password and one-time code. The session is stored encrypted on the server (in the editor's secret store, see [Security](#security)), so every worker uses it and the server can renew your endpoints while you are offline, as long as the login stays valid. **Log out** ends it. "Remove all keys" in the API Keys dialog removes it as well. When the session is lost, the dialog asks for a login again.
- **Start.** Choose a preset and start it, optionally with a "keep alive until" time of day. The dialog shows the progress (queued, building, pulling the model, ready). Closing the dialog does not stop the start. If you have not stored a Hugging Face read token, the section says so (see [Hugging Face token](#hugging-face-token)).
- **My endpoints.** Lists your endpoints with their preset, status, remaining time and "until"; you can cancel them here.

Administrators also see:

- **Presets.** The installation-wide preset catalog that every user starts from. A preset describes one endpoint: framework (vLLM or Ollama), model, machine, GPU type and count, run time, queue time, extra arguments, environment, and the supervisor rules below. Presets are created, edited and duplicated in the dialog (**Duplicate** opens the form with a copy under a free name such as `<name>-copy`). Saving a preset checks it against the service's live options and needs the administrator's own MPCDF login. **Export** downloads all presets as a JSON file and **Import** reads such a file (see below).
- **All users' endpoints.** Per user, whether a session is stored and the endpoints the server manages for them (preset, ready or starting, "until", last activity), read from the users' state files without using their sessions. Administrators cannot cancel other users' endpoints, since that would act through another user's session. **Remove session** deletes a user's stored session: the server then no longer renews or uses that user's endpoints, which expire or idle out at MPCDF.

### Exporting and importing presets

Export writes `mpcdf-llm-presets-<date>.json` with all presets (`{"format": "mpcdf-llm-presets", "version": 1, "presets": [...]}`). Import also accepts a bare JSON array of presets. A file with a preset without a name, or with the same name twice, is rejected.

When presets already exist, the import asks how to combine them. Presets are identified by name:

- **Merge** adds only the presets whose name does not exist yet. Existing presets stay unchanged, and the imported presets of the same name are skipped.
- **Replace** saves all imported presets, overwriting those of the same name, and then deletes the existing presets that are not in the file. If an imported preset is rejected, nothing is deleted.

Importing needs no MPCDF login, because the catalog is stored in the plugin's data directory. The import therefore checks only the preset's fields and types, not whether the service offers its machine, GPU type and so on (which saving in the form does, and which needs a login). A preset the service does not support fails when it is started. The dialog lists the presets it could not import with the reason.

Per preset, the supervisor (one per user, see [Multiple workers](#multiple-workers)) applies these rules to the user's endpoints:

| Rule | Meaning |
| --- | --- |
| Run time | An endpoint stops when its run time at MPCDF ends |
| Renewal | While an endpoint's "until" time is not reached, a replacement is started before the old one expires (`min_remaining`), and the models switch over without interruption |
| Idle timeout | An endpoint nobody uses for the idle time is cancelled |
| Build timeout | An endpoint that does not serve its model within this time after it starts running (download and load) is cancelled and the start fails. Empty: 30 min. Large models need more on their first start, when the weights are not yet cached at MPCDF (a 155 GiB model took about 45 min) |

Using an endpoint counts as activity, so it stays alive while its user uses it.

## Models in the inference menu

The plugin registers one provider per catalog preset: provider ID `mpcdf-llm-<preset>`, label `MPCDF: <preset>`, one model (the preset's model), chat only (no image or PDF input). The IDs are the same for every user, so a saved model choice works for everybody who runs that preset.

- A user sees and uses the provider only while that user has a ready endpoint of the preset. Other users, and calls without a user, do not see it (the editor's `LLMProvider.for_user()` hook). Every worker checks the endpoints of all users with a stored session every 10 seconds.
- The user runs on their own MPCDF account, so the model counts like a model on the user's own API key: the model filter and the admin/free rules do not apply.
- Background jobs (auto-annotation, LLamore) run on the endpoint of the user who started them.
- The provider never stores the API key. For each request it takes the current credentials from the user's manager, refreshes the credentials once after a 401, a 503 or a failed connection and tries again, and reports activity afterwards.
- When the endpoint ends (expired, cancelled, replaced), the provider disappears for the user at the next check; a request that arrives in between fails with "MPCDF endpoint for <preset> is no longer running".
- A Default Model that points at an MPCDF provider resolves only for users with a ready endpoint of that preset.

## Installation

The library is not on PyPI, so the plugin depends on it by a direct git reference (`mpcdf-llm[fastapi,testing] @ git+https://gitlab.mpcdf.mpg.de/cboul/llm-mpcdf.git@<commit>` in [pyproject.toml](pyproject.toml)). A plain name would resolve to whatever package carries that name on PyPI (dependency confusion); the direct reference is part of the wheel metadata, so every installer fetches the library from the MPCDF GitLab at the pinned commit. Update the commit deliberately to take over library changes.

Installing the plugin therefore needs access to that git repository, and the editor's Plugin Manager can install it only if it permits direct references; otherwise install the plugin when the editor is built.

`mpcdf-llm[testing]` is a runtime dependency on purpose: the extra only adds the library's fake provider (`mpcdf_llm.testing`), which the plugin's own tests and the editor's API tests point `MPCDF_LLM_BASE_URL` at. It needs no extra packages.

## Configuration

| Config key | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `plugin.mpcdf-llm.base-url` | `MPCDF_LLM_BASE_URL` | `https://llm.mpcdf.mpg.de` | Base URL of the MPCDF LLM service. It exists for tests, which point it at a fake |

State lives in `<plugins data dir>/mpcdf-llm/`:

| Path | Content |
| --- | --- |
| `presets/` | The preset catalog |
| `users/<hex of the username>/` | One directory per user: endpoint state and locks. The name is the hexadecimal UTF-8 encoding of the editor username |

The users' MPCDF sessions are not in this directory; they are in the editor's secret store under the service `mpcdf-llm-session`.

### Hugging Face token

Endpoints of gated models (served by vLLM) need a Hugging Face read token. Each user stores their own in the API Keys dialog (user menu > API Keys) as "Hugging Face read token" (secret service `hf-read-token`). It is passed to the endpoints that user starts, and read again at every start, so the renewals by the supervisor use a changed token. Users without a token start endpoints without one; a gated model then fails to start with the service's message. Other plugins that need Hugging Face access can use the same secret service.

The plugin never uses a `HF_TOKEN` environment variable of the server, but unset it there anyway, so that no installation-wide token is lying around.

## Multiple workers

Each server worker owns one manager per user, on that user's directory. A worker creates a user's manager on the user's first request, and for every user with a stored session within one check interval, so the users' endpoints are supervised while they are offline. The library's file locks make this safe per user:

- Per user, only one worker runs the supervisor (idle cancel, renewal). The others are passive and take over when it exits.
- Starting a preset from two workers (two tabs routed to different workers) submits **one** job. The second start attaches to the first.
- The worker that starts an endpoint follows it until it is ready and holds a lease with a heartbeat. The supervisor leaves a followed endpoint alone. If the follower dies (for example a restart), the supervisor adopts the endpoint and follows it to the end; it never submits again.
- A login, logout or removed session in one worker reaches the others within one check interval: they read the session from the shared secret store and drop managers whose session is gone.
- Stopping or restarting the editor never cancels endpoints; they keep running at MPCDF and are found again at the next start.

## Security

- Every route that uses an MPCDF session resolves the **requesting user's** manager, so a user can only use their own session and endpoints. Nobody, administrators included, can act through another user's session.
- Sessions are stored Fernet-encrypted in the editor's secret store (`secrets.db`), never in a file of the plugin. The service `mpcdf-llm-session` is not registered as an API key, so it cannot be viewed or edited in the API Keys dialog.
- Changing the preset catalog is restricted to administrators (`require_admin_user` for the import, a check of the role for `PUT` and `DELETE` on `/my/presets/...`).
- **The library's read router is deliberately not mounted.** It returns the endpoints' API keys. Users reach the models only through the editor's provider registry, which keeps the keys on the server.
- `GET /managed` returns the preset and "until" of the user's own endpoints, `GET /overview` (admin) the users' endpoints from their state files. Neither returns keys, tokens or lease data.
- Error messages from the library are sanitised, so passwords, one-time codes and tokens do not appear in responses or logs.

## Routes

| Route | Access | Purpose |
| --- | --- | --- |
| `GET /api/plugins/mpcdf-llm/manage` | authenticated | The management page |
| `GET /api/plugins/mpcdf-llm/context` | authenticated | The page's context: `{username, is_admin, hf_token_stored}` |
| `GET /api/plugins/mpcdf-llm/managed` | authenticated | Preset and "until" of the user's managed endpoints |
| `/api/plugins/mpcdf-llm/my/...` | authenticated | The library's admin router on the user's own manager: session, presets (changes admin only), start (event stream), endpoints, stop |
| `POST /api/plugins/mpcdf-llm/presets/import` | admin | Save a list of presets into the catalog without an MPCDF session; returns `{saved: [names], errors: {name: reason}}` |
| `GET /api/plugins/mpcdf-llm/overview` | admin | All users with their stored-session flag and managed endpoints |
| `DELETE /api/plugins/mpcdf-llm/users/{username}/session` | admin | Remove a user's stored session |

## Manual live check

CI cannot log in to MPCDF, so check a release by hand with two real MPCDF accounts:

1. Log in to the dialog as editor user A with MPCDF account A, start the cheapest preset and wait until it is ready.
2. Find `MPCDF: <preset>` in A's inference menu and run a request.
3. Log in to the editor as user B (another browser profile): the model is not in B's menu. Log in with MPCDF account B, start the same preset; B now sees the model and requests run on B's endpoint.
4. As an administrator, check that "All users' endpoints" lists both users, and remove B's session: B's dialog asks for a login again.
5. Cancel the endpoints in the users' dialogs and check that the models disappear from their menus within a few seconds.

## Implementation

- **Plugin file**: [plugin.py](src/pte_plugin_mpcdf_llm/plugin.py) (lifecycle)
- **Managers**: [manager.py](src/pte_plugin_mpcdf_llm/manager.py) (per-user managers, encrypted session store, Hugging Face token, catalog, migration, configuration)
- **Providers**: [providers.py](src/pte_plugin_mpcdf_llm/providers.py)
- **Routes and page**: [routes.py](src/pte_plugin_mpcdf_llm/routes.py), [static/manage.html](src/pte_plugin_mpcdf_llm/static/manage.html)
- **Tests**: [tests/](src/pte_plugin_mpcdf_llm/tests/), against the library's fake provider with two MPCDF users; the two-worker tests run two managers of one user on that user's directory. API and E2E tests are in `tests/api` and `tests/e2e` and run with the editor's runners (see the repository README)

## Technical Details

- Category: `inference`
- Menu entry and API: all users; changing the preset catalog and the overview: admin role
- Requires Python 3.13, plugin API 1.3, and an own MPCDF account for every user who starts endpoints
