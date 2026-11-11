# pte-plugins

Plugins for the [PDF-TEI-Editor](https://github.com/mpilhlt/pdf-tei-editor). Each plugin in `plugins/<name>/` is a Python package (`pte-plugin-<name>`, module `pte_plugin_<name>`) that the editor discovers through the `pte.plugins` entry point once it is installed into the editor's environment.

## Plugins

| Plugin | Package | Version | Description |
| --- | --- | --- | --- |
| [annotation-history](plugins/annotation-history) | `pte-plugin-annotation-history` | 1.0.0 | Shows detailed annotation history for PDF documents |
| [annotation-progress](plugins/annotation-progress) | `pte-plugin-annotation-progress` | 1.0.0 | View annotation progress in the current collection |
| [annotation-review](plugins/annotation-review) | `pte-plugin-annotation-review` | 1.0.0 | Reviews a document's annotations against its own editorialDecl-linked rules using an LLM |
| [auto-annotation](plugins/auto-annotation) | `pte-plugin-auto-annotation` | 0.1.0 | Evaluates LLMs at correcting GROBID training annotations against gold versions (model probe) |
| [anthropic-llm](plugins/anthropic-llm) | `pte-plugin-anthropic-llm` | 1.0.0 | Registers Anthropic's Claude API as an LLM inference provider |
| [archival-edition](plugins/archival-edition) | `pte-plugin-archival-edition` | 0.1.0 | Extractor for photographed archival documents with a description form, schema and annotation guidelines |
| [collection-overview](plugins/collection-overview) | `pte-plugin-collection-overview` | 1.0.0 | Overview of gold-standard coverage and annotation progress across all accessible collections |
| [edit-history](plugins/edit-history) | `pte-plugin-edit-history` | 1.0.0 | View recent activity in the current collection |
| [google-llm](plugins/google-llm) | `pte-plugin-google-llm` | 1.0.0 | Registers Google's Gemini API as an LLM inference provider |
| [grobid](plugins/grobid) | `pte-plugin-grobid` | 1.0.0 | Extract training data using GROBID server |
| [iaa-analyzer](plugins/iaa-analyzer) | `pte-plugin-iaa-analyzer` | 1.0.0 | Compute agreement between annotation versions by comparing element positions in <text> |
| [inference-endpoints](plugins/inference-endpoints) | `pte-plugin-inference-endpoints` | 1.0.0 | Registers OpenAI-compatible inference endpoints (e.g. Ollama) as LLM providers |
| [kisski](plugins/kisski) | `pte-plugin-kisski` | 2.0.0 | Text and PDF processing using KISSKI Academic Cloud API |
| [llamore](plugins/llamore-extractor) | `pte-plugin-llamore-extractor` | 1.0.0 | Extract references using LLamore with the configured LLM model |
| [local-sync](plugins/local-sync) | `pte-plugin-local-sync` | 1.0.0 | Synchronize collection documents with local filesystem |
| [metadata-extraction](plugins/metadata-extraction) | `pte-plugin-metadata-extraction` | 1.1.0 | Extract bibliographic metadata from PDFs, enrich TEI headers and backfill missing metadata in collections |
| [mpcdf-llm](plugins/mpcdf-llm) | `pte-plugin-mpcdf-llm` | 1.0.0 | Lets every user start MPCDF-hosted LLM endpoints with their own MPCDF account and offers them as models |
| [revision-feed](plugins/revision-feed) | `pte-plugin-revision-feed` | 1.0.0 | Per-project Atom feeds of document revisions |
| [rng-converter](plugins/rng-converter) | `pte-plugin-rng-converter` | 1.0.0 | Generate RelaxNG schema from TEI document structure |
| [update-metadata](plugins/update-metadata) | `pte-plugin-update-metadata` | 1.0.0 | Update TEI documents with complete bibliographic metadata from DOI lookup |
| [webdav-sync](plugins/webdav-sync) | `pte-plugin-webdav-sync` | 1.0.0 | Synchronize files with a WebDAV server |

[pte-plugins.json](pte-plugins.json) lists the same information in machine-readable form. It is generated from the packages by `python scripts/build_index.py`; CI checks that it is up to date.

The [overview page](https://mpilhlt.github.io/pte-plugins/) on GitHub Pages lists the plugins with their metadata and links to their READMEs. The [Pages workflow](.github/workflows/pages.yml) rebuilds it with `python scripts/build_site.py` on every push to `main`. The site also publishes `pte-plugins.json` and each plugin's `docs/` directory at `https://mpilhlt.github.io/pte-plugins/<name>/` (e.g. the archival-edition schema and guidelines).

`archived/` contains plugins that are no longer maintained. They are not packaged and not tested.

## Installing plugins

The packages are installed into the Python environment of the editor. From a checkout of the editor:

```bash
uv pip install "pte-plugin-grobid @ git+https://github.com/mpilhlt/pte-plugins#subdirectory=plugins/grobid"
```

Plugins that depend on other plugins of this repository (see the "requires" field of the index) declare them as package dependencies; uv resolves them within this repository's workspace. Some plugins need configuration (server URLs, API keys) before they become available; see their READMEs.

If the operator enabled runtime install (`PTE_PLUGIN_INSTALL=1`), administrators can also install the released plugins in the editor's plugin manager ("Available" tab). This works for plugins whose Python dependencies (the "dependencies" field of the index) are already installed in the editor's environment; other plugins must be installed as above.

## Releasing a plugin

Releases are what the plugin manager installs, and they are triggered by commit messages, not by hand. A commit releases a plugin only when its Conventional Commits **scope is exactly that plugin's id** (the `id` field in `pte-plugins.json`, e.g. `grobid` or `archival-edition`):

```text
fix(grobid): handle a broken bibl split        -> patch release of grobid
feat(archival-edition): support TEI facsimile  -> minor release of archival-edition
```

A breaking change needs a `BREAKING CHANGE:` footer (not the `type(scope)!:` shorthand, which this project's parser configuration does not recognize) to trigger a major release:

```text
fix(grobid): rename the public extractor function

BREAKING CHANGE: `extract()` is now `extractBibl()`
```

Commits without a scope, or whose scope does not match any plugin id, release nothing. A commit scoped to one plugin is never counted for another, even one whose id is a prefix of it (`grobid` vs. a hypothetical `grobid-extra`).

On every push to `main`, [`.github/workflows/semantic-release.yml`](.github/workflows/semantic-release.yml) runs `semantic-release` once per plugin listed in `pte-plugins.json`, scoped to that plugin's own commits since its last release (see [`release.config.js`](release.config.js) and [`scripts/release/scoped-release-plugin.js`](scripts/release/scoped-release-plugin.js)). For a plugin with a qualifying commit, it: raises `version` in the plugin's `pte-plugin.json` (via [`scripts/bump_plugin_version.py`](scripts/bump_plugin_version.py)), regenerates `pte-plugins.json`, commits both as `chore(release): <plugin-id> <version>`, and tags the commit `<plugin-id>-v<version>`. That tag is what the [release workflow](.github/workflows/release.yml) listens for: it checks that the tag matches the manifest version, builds the wheel, and creates a GitHub release with the wheel, a `SHA256SUMS` file and a build provenance attestation. The plugin manager installs only wheels whose sha256 matches `SHA256SUMS`.

After a run that released at least one plugin, the workflow's `bump-core` job opens a PR against pdf-tei-editor's `devel` branch that moves all of the editor's plugin pins (`[tool.uv.sources]` in its `pyproject.toml`) to the last release commit of that run and relocks its `uv.lock`; all pins move together because plugins that depend on each other resolve their sibling plugins from the same commit (see [`scripts/bump_core_pins.py`](scripts/bump_core_pins.py)). The editor's Tests workflow runs the whole suite with all plugins on that PR and merges it when it passes; a failing PR stays open, since it means the editor and the latest plugin releases are incompatible. The job needs the secret `PDF_TEI_EDITOR_BUMP_TOKEN`, a fine-grained personal access token for `mpilhlt/pdf-tei-editor` with Contents and Pull requests read/write; without it, the job only logs a warning.

A manual `pte-plugin.json` version bump without a matching commit is not recommended: the next automated release recomputes the version from commit history and will overwrite it.

## Package layout

```text
plugins/<name>/
├── pyproject.toml              # package metadata, Python dependencies, pte.plugins entry point
├── README.md
└── src/pte_plugin_<name>/
    ├── pte-plugin.json         # version and compatible editor/plugin API versions
    ├── plugin.py               # Plugin subclass
    ├── ...
    └── tests/                  # not part of the wheel
        ├── test_*.py           # unit tests (pytest)
        ├── *.test.js           # JavaScript unit tests (node --test)
        ├── api/*.test.js       # API tests, run by the editor's backend test runner
        ├── e2e/*.spec.js       # Playwright tests, run by the editor's E2E runner
        └── integration/        # tests that need external services, run manually
```

`pte-plugin.json` is the single source of the version: the package version is read from it (`[tool.hatch.version]`), and the editor shows it in the plugin manager. `engines.pte` is the range of editor versions and `engines.pte-plugin-api` the range of plugin API versions (`fastapi_app.plugin_api`) a plugin works with; the editor does not load plugins outside these ranges. See the editor's [backend plugin guide](https://github.com/mpilhlt/pdf-tei-editor/blob/devel/docs/code-assistant/backend-plugins.md) for the plugin API.

## Development

The plugins run and are tested in the environment of an editor checkout, by default `../pdf-tei-editor` next to this repository (set `PTE_ROOT` otherwise). Install all plugins into it as editable packages, with their Python dependencies:

```bash
npm run dev:install
```

The editor then loads the plugins from `plugins/*/src`, so changes take effect without publishing or reinstalling. The editor's dev server (`npm run start:dev`) also reloads on changes in their directories; it determines them when it starts, so restart it after adding a plugin. `uv sync` in the editor removes the editable installs again, and `uv sync --extra <plugin>` replaces them with the released versions; run `npm run dev:install` again afterwards. `uv run` keeps them (set `UV_NO_SYNC=1` to be sure).

Run the tests:

```bash
export PTE_ROOT=$(realpath ../pdf-tei-editor)
"$PTE_ROOT/.venv/bin/python" -m pytest        # Python unit tests of all plugins, plus tests/
npm install && npm test                        # JavaScript unit tests

# API and E2E tests: the editor's runners stage the plugins' tests/api and tests/e2e files
cd "$PTE_ROOT"
node tests/backend-test-runner.js --test-dir tests/api/v1 --plugin-tests ../pte-plugins
node tests/e2e-runner.js --plugin-tests ../pte-plugins
```

JavaScript tests that need the editor's test helpers import them through [scripts/core.js](scripts/core.js).

CI ([.github/workflows/tests.yml](.github/workflows/tests.yml)) runs all of this against the editor's `devel` branch.

## Adding a plugin

1. Create `plugins/<name>/` with the layout above. Copy `pyproject.toml` from an existing plugin and adapt name, description, dependencies and the entry point (`<plugin-id> = "pte_plugin_<name>"`).
2. Import editor functionality from `fastapi_app.plugin_api` where possible; other core modules may change without notice.
3. Run `python scripts/build_index.py` and add the plugin to the table above.
