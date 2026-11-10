# MPCDF LLM Plugin

Lets administrators start LLM endpoints on the [MPCDF LLM service](https://llm.mpcdf.mpg.de) (GPU jobs on the MPCDF clusters) from the editor, and offers each endpoint that is ready as a free model wherever the core LLM provider registry is used (for example Tools > Default Model, the [Annotation Review](../annotation-review/README.md) plugin and the [LLamore extractor](../llamore-extractor/README.md)).

The plugin is a thin wrapper around the [mpcdf-llm](https://gitlab.mpcdf.mpg.de/cboul/llm-mpcdf) library, which logs in to the service, submits and follows the jobs, renews them before they expire and cancels them when idle.

## Usage

Administrators open the management page from the plugin menu (Inference > MPCDF LLM endpoints). The dialog has these parts:

- **Session.** Log in with the MPCDF user name, password and one-time code. The session is stored on the server, so every worker uses it, and it is renewed as long as the login stays valid. When it is lost, the dialog asks for a login again.
- **Presets.** A preset describes one endpoint: framework (vLLM or Ollama), model, machine, GPU type and count, run time, queue time, extra arguments, environment, and the supervisor rules below. Presets are created and edited in the dialog. **Export** downloads all presets as a JSON file and **Import** reads such a file (see below).
- **Start.** Choose a preset and start it, optionally with a "keep alive until" time of day. The dialog shows the progress (queued, building, pulling the model, ready). Closing the dialog does not stop the start.
- **Endpoints.** Lists the endpoints with their preset, status, remaining time and "until"; they can be cancelled here.

### Exporting and importing presets

Export writes `mpcdf-llm-presets-<date>.json` with all presets (`{"format": "mpcdf-llm-presets", "version": 1, "presets": [...]}`). Import also accepts a bare JSON array of presets. A file with a preset without a name, or with the same name twice, is rejected.

When presets already exist, the import asks how to combine them. Presets are identified by name:

- **Merge** adds only the presets whose name does not exist yet. Existing presets stay unchanged, and the imported presets of the same name are skipped.
- **Replace** saves all imported presets, overwriting those of the same name, and then deletes the existing presets that are not in the file. If an imported preset is rejected, nothing is deleted.

Importing needs an MPCDF login, because saving a preset checks it against the options of the service (machines, GPU types and so on). The dialog lists the presets it could not import with the reason.

Per preset, the supervisor (one per installation) applies these rules:

| Rule | Meaning |
| --- | --- |
| Run time | An endpoint stops when its run time at MPCDF ends |
| Renewal | While an endpoint's "until" time is not reached, a replacement is started before the old one expires (`min_remaining`), and the models switch over without interruption |
| Idle timeout | An endpoint nobody uses for the idle time is cancelled |

Using an endpoint counts as activity, so it stays alive while people use it.

## Models in the inference menu

Every worker checks the endpoints every 10 seconds. For each preset with a ready endpoint it registers one provider:

- Provider ID `mpcdf-llm-<preset>`, label `MPCDF: <preset>`, one model (the preset's model), chat only (no image or PDF input).
- The model is **free**: all users may use it, not only administrators. Starting endpoints is admin only; using them is not.
- The provider never stores the API key. For each request it takes the current credentials from the library, refreshes the credentials once after a 401, a 503 or a failed connection and tries again, and reports activity to the library afterwards.
- When the endpoint ends (expired, cancelled, replaced), the provider disappears at the next check; a request that arrives in between fails with "MPCDF endpoint for <preset> is no longer running".

## Installation

The library is not on PyPI, so the plugin depends on it by a direct git reference (`mpcdf-llm[fastapi,testing] @ git+https://gitlab.mpcdf.mpg.de/cboul/llm-mpcdf.git@<commit>` in [pyproject.toml](pyproject.toml)). A plain name would resolve to whatever package carries that name on PyPI (dependency confusion); the direct reference is part of the wheel metadata, so every installer fetches the library from the MPCDF GitLab at the pinned commit. Update the commit deliberately to take over library changes.

Installing the plugin therefore needs access to that git repository, and the editor's Plugin Manager can install it only if it permits direct references; otherwise install the plugin when the editor is built.

`mpcdf-llm[testing]` is a runtime dependency on purpose: the extra only adds the library's fake provider (`mpcdf_llm.testing`), which the plugin's own tests and the editor's API tests point `MPCDF_LLM_BASE_URL` at. It needs no extra packages.

## Configuration

| Config key | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `plugin.mpcdf-llm.hf-token` | `HF_TOKEN` | none | Hugging Face token (masked). Only needed for gated models served by vLLM; it is passed to the endpoint when it starts |
| `plugin.mpcdf-llm.base-url` | `MPCDF_LLM_BASE_URL` | `https://llm.mpcdf.mpg.de` | Base URL of the MPCDF LLM service. It exists for tests, which point it at a fake |

State (session, presets, endpoints, locks) lives in `<plugins data dir>/mpcdf-llm/`.

## Multiple workers

Each server worker owns a manager on that shared directory. The library's file locks make this safe:

- Only one worker runs the supervisor (idle cancel, renewal). The others are passive and take over when it exits.
- Starting a preset from two workers (two admins, or two tabs routed to different workers) submits **one** job. The second start attaches to the first.
- The worker that starts an endpoint follows it until it is ready and holds a lease with a heartbeat. The supervisor leaves a followed endpoint alone. If the follower dies (for example a restart), the supervisor adopts the endpoint and follows it to the end; it never submits again.
- Providers converge in every worker within one check interval.
- Stopping or restarting the editor never cancels endpoints; they keep running at MPCDF and are found again at the next start.

## Security

- The management page and the admin API are restricted to administrators. The page is checked in the route, the admin API with the editor's `require_admin_user`.
- **The library's read router is deliberately not mounted.** It returns the endpoints' API keys. Users reach the models only through the editor's provider registry, which keeps the keys on the server.
- `GET /api/plugins/mpcdf-llm/managed` (admin) tells the dialog which preset each endpoint belongs to and its "until" time. It returns nothing else (no keys, tokens or lease data).
- Error messages from the library are sanitised, so passwords, one-time codes and tokens do not appear in responses or logs.

## Routes

| Route | Access | Purpose |
| --- | --- | --- |
| `GET /api/plugins/mpcdf-llm/manage` | admin | The management page |
| `GET /api/plugins/mpcdf-llm/managed` | admin | Preset and "until" of managed endpoints |
| `/api/plugins/mpcdf-llm/admin/...` | admin | The library's admin router: session, presets, start (event stream), endpoints, stop |

## Manual live check

CI cannot log in to MPCDF, so check a release by hand:

1. Log in to the dialog with a real MPCDF account.
2. Start the cheapest preset and wait until it is ready.
3. Log in as another (non-admin) user and find `MPCDF: <preset>` in the inference menu; run a request.
4. Cancel the endpoint in the dialog and check that the model disappears from the menu within a few seconds.

## Implementation

- **Plugin file**: [plugin.py](src/pte_plugin_mpcdf_llm/plugin.py) (lifecycle)
- **Manager**: [manager.py](src/pte_plugin_mpcdf_llm/manager.py) (per-worker manager, configuration)
- **Providers**: [providers.py](src/pte_plugin_mpcdf_llm/providers.py)
- **Routes and page**: [routes.py](src/pte_plugin_mpcdf_llm/routes.py), [static/manage.html](src/pte_plugin_mpcdf_llm/static/manage.html)
- **Tests**: [tests/](src/pte_plugin_mpcdf_llm/tests/); the two-worker tests run two managers on one data directory against the library's fake provider. API and E2E tests are in `tests/api` and `tests/e2e` and run with the editor's runners (see the repository README)

## Technical Details

- Category: `inference`
- Menu entry and API: admin role. The registered models are free
- Requires Python 3.13 and the MPCDF account of the administrator
