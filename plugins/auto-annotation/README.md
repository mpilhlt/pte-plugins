# Auto-annotation

Automated annotation of TEI documents produced by any extractor: an LLM corrects the extractor's output against the document's machine rules. The design is in the proposal `2026-11-05-auto-annotation-proposal.md` (project files; to be moved to `docs/history` in pdf-tei-editor with the implementation).

It contains:

- the **Correct annotations in new version** action in the editor's Tools menu, which corrects the open document with one model and saves the result as a new version;
- the **model probe**, which measures how well a single model corrects the annotation of gold documents, and how that changes with the number of review units sent in one request; in the editor as **Test model with current document** in the Tools menu, and as a command line tool;
- the **annotation pipeline**, which annotates documents or a whole collection with two models and an arbiter and saves the result as a new version.

## Correct annotations in new version

The Tools menu item (section "Annotation") is for annotators, reviewers and admins. It is enabled when the open document's `editorialDecl` has a machine-readable rule reference (`subtype="machine"`).

1. After a confirmation naming the model selected in **Tools > Inference**, unsaved changes are saved and the document becomes read-only.
2. The server takes over the document's lock from the user's session and holds it under the job's id (`auto-annotation:<job id>`), so the document opens read-only everywhere, also when the user returns to it in the same tab. The lock is refreshed with every request and every 30 seconds; if the browser tab is closed, it expires like any other lock. The action is refused while another user edits the document, and for gold versions.
3. The review units of the variant (its `reviewUnits` path) are sent to the model in batches of 16 units, one request per batch, with split-and-retry of failing batches and retries as in the pipeline. A progress widget shows how many units are done and changed; it can be minimized and cancelled.
4. Units take the model's proposal if it passes the checks (well-formed, same outermost element, text unchanged); otherwise they keep the original. For segmenting variants, text may move between neighbouring units of a batch.
5. The corrected document gets a `revisionDesc/change` by `#auto-annotation` (status `extraction`, label `Auto-annotation`). Its `desc` names the model as `<name type="model" subtype="<provider id>" key="<provider id>/<model id>">` with the model's label as text, and records the counts as `<measure type="corrected-units" subtype="changed|unchanged|invalid|error|unprocessed" commodity="<provider id>/<model id>" quantity="<n>">` and the duration as `<time dur="PT4M12S">`. These elements need a schema that allows them in `desc`.
6. The client saves the document as a new version of the open one through the editor's save API; the source version is then frozen, since a version was derived from it. If the source document is still open, the new version is loaded; otherwise a dialog asks whether to load it.

If the model changes nothing, no version is created. If the run is cancelled or a request fails (after retries), the user can save the corrections made so far; the change then records the unprocessed units. In every case that saves nothing, the job's lock is released and the source can be edited again.

## Test model with current document

Two items in the Tools menu (section "Inference") run the model probe in the editor. They are shown to reviewers and admins only.

**Test model with current document** tests a model on the open version. That version is the reference that the model's corrections are scored against, and the model corrects an earlier extraction of the same document, chosen as by the probe's `--doc` (see below). The item is disabled when no document is open, when the document has no machine-readable rule reference, or when no model is available.

1. If the open version is not a gold version, a warning asks for confirmation, since the scores are then measured against annotations that may contain errors.
2. A form asks for the model (preselected: the default model of Tools > Inference), the batch sizes (`units:1` to `units:32`, plus further `units:N` or `chars:N` specs), the output modes, the repetitions, the requests in parallel, the retries, the temperature, a limit to the first units for a quick test, the version to correct (automatic or another version of the same variant), and whether to start even if the model is reported as busy. It shows the number of runs: batch sizes × modes × repetitions, each sending all units once.
3. The server checks the parameters, the model (access, key, busy status) and the documents, and starts the test as a job of the worker that received the request. Errors open an error alert. A message confirms the start, and a minimized progress widget shows the runs done; its cancel button stops the test after the request under way. Each user can run one test at a time.
4. When the test is done or was cancelled after a finished run, its results open in the plugin result dialog. Errors (all requests of a run failed, the server restarted) open an error alert, and the results of the finished runs are kept.

**Model test results** opens the list of all tests, which can be filtered by model.

The page of a test shows its parameters, its scores (as in the probe's report, with the input as the first row), download links, a section explaining how the scores are computed for its result files and a zip of all of them, and two comparisons: the model's tests on other documents, with the scores per batch size and mode pooled over the units of the latest finished test of the model on each reference version, and the latest tests of other models on the same reference version. Tests with another prompt version, other rules (on the same document) or a reference that is not a gold version are marked, since their scores are not directly comparable. All reviewers see all tests; the user who started a test and admins can delete it.

The tests are stored in `data/plugins/auto-annotation/probe-runs/<run id>/`: `meta.json` (user, model, document, versions, parameters), `status.json` (state and progress, rewritten while the test runs), and the probe's result files (`units.csv`, `batches.jsonl`, `summary.json`, `report.md`), written after every run. Every worker answers status requests and serves results from these files. A running test writes a heartbeat every 15 seconds; a test without a heartbeat for two minutes stopped with its worker and is shown as interrupted.

The progress widget starts minimized with editor versions whose progress widget supports the `minimized` option; with older versions it starts maximized.

## Model probe

```bash
uv run python -m pte_plugin_auto_annotation.probe \
  --model kisski/llama-3.3-70b-instruct --model endpoint-ollama/qwen2.5:14b \
  --variant grobid.training.references --doc 10.1234__abc --doc 10.1234__def \
  --batches units:1,units:4,units:8,units:16,units:32 --modes all,changed --repeats 2
```

The installed package also provides the command `pte-model-probe` with the same options. Run `--help` for all options.

- `--model`: a model as configured in the app: `default`, `<provider>`, or `<provider>/<model>`. `<provider>` is a provider id (`kisski`, `endpoint-ollama`), its label (an inference endpoint's name) or an endpoint id without the `endpoint-` prefix; `<model>` is a model id or label. With `<provider>` alone, the provider's only model or the default model is used.
- `--doc`: a document id. Its gold version of `--variant` (required with `--doc`, e.g. `grobid.training.references`) is the reference the model's corrections are scored against. The version the model corrects is the extraction the reference was derived from (the oldest extraction among the versions' parents), else the earliest extraction of the same document and variant (ties by creation time); without an extraction, the probe stops and asks for `--input`. A version counts as an extraction if its revision history starts with the extraction and has only metadata changes after it (renamed, document rules updated); label and status are not reliable (`versions.py`). The report says how the input was chosen, and marks a given `--input` that is not an extraction.
- `--reference`: the stable id of a version to score against instead of the gold version, e.g. for a document without a gold version yet; the report marks it as "not the gold version". Its document and variant are used, so `--doc` and `--variant` can be left out.
- `--batches`: batch specs, `units:N` (at most N units per request) or `chars:N` (at most N characters of unit XML).
- `--modes`: `all` (the model returns every unit) or `changed` (only the units it changed).
- `--retries`: how often a request is repeated after a provider overload or timeout (HTTP 503), with exponential backoff from 2 s (default 4). Rate limits are handled by the editor: it waits out a wait of up to a minute, and a longer wait or an exhausted quota ends the request with HTTP 429 and the time to try again, which then appears in the batch's error. Until that time, all requests of that model fail at once, without calling the service. Requests that still fail are reported as errors and left out of the scores, since they say nothing about the model.
- `--force-busy`: start even if a model is reported as busy. Without it, `probe` and `annotate` refuse to start while one of their models is `busy` or `very_busy` in the provider's status, because most requests to a busy model time out. Models whose provider reports no status count as free. In `annotate`, `--force` is unrelated: it re-annotates documents annotated before.
- `--rules-file`: pin the rules to a local file instead of the reference version's rule refs.

It runs against a running editor and logs in like `bin/batch-extract.js`: `--base-url`, `--token` (a personal access token, preferred) or `--user` and `--password`, or `API_BASE_URL`, `API_TOKEN`, `API_USER`, `API_PASSWORD` from the environment or `--env` (default `./.env`). The user needs the reviewer or admin role and access to the models.

### What it measures

1. The review units of the input and the reference version (the variant's `reviewUnits` path, e.g. `.//listBibl/bibl`) are aligned by their text. Units whose text the annotator changed, split or merged are left out and counted.
2. For every batch spec, mode and repetition, the input units are sent in batches, each unit under a `### Unit <n>` header; the answer uses the same format, so the model never escapes XML in JSON.
3. Every returned unit must be well-formed, keep its outermost element and keep its text (whitespace ignored). Units failing these checks, or missing from the answer, count as failures and are scored as the unchanged input. Units of requests that failed (provider errors, after retries) count as errors and are not scored.
4. Units are scored by fields, one (element path, attributes, text) tuple per element. **Edit precision/recall** compare the changes the model made to the input with the changes the reference version needs; field F1 and exact unit matches are reported too. The first row of each table is the input itself (baseline).
5. Edit recall by the unit's position in the batch (quarters) shows whether quality drops towards the end of long answers.

Output in `--out` (default `probe-results/<timestamp>/`): `report.md`, `summary.json`, `units.csv` (one row per unit and run) and `batches.jsonl` (one row per request, with the raw answer). The files are rewritten after every run (batch spec, mode and repetition), so a probe that is interrupted or fails keeps the finished runs; the report marks documents whose runs are incomplete.


#### Segmenting variants

For variants that their extractor declares as segmenting (`segmentingVariants` in the extractor info, next to `reviewUnits`; the GROBID plugin declares `grobid.training.references.referenceSegmenter`), correcting a unit means splitting or merging it, so the steps above change (`segmentation.py`):

- The prompt allows a unit to be returned as several `<bibl>` elements, or empty when its text moves into a neighbouring unit.
- A returned unit must consist of `<bibl>` elements with only whitespace between them. Its text is checked first per unit; a unit whose own text changed is accepted when the batch as a whole keeps its text and no unit of the batch failed otherwise.
- Units are not matched one-to-one. Input and reference version are read as one text stream (whitespace removed), each element becomes a field with its start and end position in that stream, and the reference's fields are mapped onto the input's text where the two agree. A field is credited to the input unit in which it starts; input units whose text the reference does not share entirely are left out and counted as unmatched. Edits are then counted as above.

## Annotation pipeline

```bash
uv run python -m pte_plugin_auto_annotation.annotate \
  --model-a kisski/llama-3.3-70b-instruct --model-b gemini/gemini-2.5-flash-lite --arbiter anthropic/<model> \
  --variant grobid.training.references --collection my-collection
```

The installed package also provides the command `pte-auto-annotate`. Run `--help` for all options; the connection options are those of the probe.

The plugin does not depend on a particular extractor or schema: the review units are those of the variant's `reviewUnits` path in the editor's configuration. `--variant` (e.g. `grobid.training.references`) has no default and is required with `--collection` and `--doc`.

What to annotate (exactly one of):

- `--collection`: all documents of a collection that have a version of `--variant`;
- `--doc`: given document ids (repeatable);
- `--version`: given stable ids (repeatable), annotated without the checks below.

For `--collection` and `--doc`, the extraction of the variant (its version without a parent) is annotated. If a document that would be annotated has several extractions, the command stops before annotating anything and names them; choose one with `--version`. A document is skipped if one of its versions was already annotated by the pipeline (its `revisionDesc` has a change by `#auto-annotation`; `--force` annotates it again) or if it has a gold version (`--include-gold`). A repeated run therefore continues where an interrupted one stopped.

Per document, on the server:

1. Models A and B correct all review units independently, with batching, split-and-retry and retries as in the probe (`--batch`, default `units:16`; `--mode`, `--split-on-failure`, `--concurrency`, `--retries`). Every proposal is checked (well-formed, same outermost element, text unchanged). Each model has a time budget per document (`--time-budget`, default 1800 s, 0 for none): once it is used up, its requests are no longer sent or repeated and their units count as errors.
2. Units on which both models return valid, canonically equal proposals are **agreed** and take that proposal.
3. The other units go to the arbiter (`--arbiter-batch`, default `units:8`), which sees the original and the distinct valid candidates and answers with a candidate, the original or its own correction (if both models failed, it corrects the unit itself). A valid answer makes the unit **arbitrated**.
4. Units whose arbiter answer fails the checks, or whose arbiter request failed, take the proposal of model B (`--fallback`, default `B`; `none` keeps the original) and are recorded as **fallback** in the run log and the revision change, so that a reviewer can find them. Units without an arbiter or without a fallback proposal are **unresolved** and keep the original.

For segmenting variants (see above), the models may split and merge units, and text may move between neighbouring units. Decisions are then made per group: the shortest run of consecutive units after which both models' proposals have the same text as the original. Usually a group is one unit; text that a model moved into the next unit joins the two. A group's proposals are compared element by element; a disputed group goes to the arbiter as one unit (the original then consists of several elements), and its answer may have any number of elements with the group's text. In the saved version, a group's result replaces its first unit and its other units are removed; every resulting unit gets the group's review mark.

The result is saved by the client as a new version of the input (through the editor's save API, so version numbers, collections and locks are handled as for a manual save). The new version has:

- the changed units replaced; the rest of the document keeps its formatting;
- a `revisionDesc/change` by `#auto-annotation` (status `--status`, default `extraction`; label `--label`, default `Auto-annotation`) naming the models and the counts, and a `respStmt` for `#auto-annotation`;
- in the change's `desc`, in addition to the readable text, machine-readable values for later analysis: how often the arbiter chose each version (`<measure type="arbiter-choice" subtype="A|B|own|original" commodity="<model>" quantity="<n>">`, without `commodity` for the original), the same for the units where both models gave a valid proposal (`type="arbiter-choice-both-valid"`, the fair comparison of A and B), how many proposals of each model failed the checks (`type="invalid-proposal"`, `subtype="A|B"`), and the duration (`<time dur="PT4M12S">4 min 12 s</time>`). In `arbiter-choice`, a count for a model includes units where the other model's proposal was invalid. These elements need a schema that allows them in `desc`.

The server annotates a whole document in one request; `--timeout` (default 3600 s) is how long the client waits for it.

`--dry-run` saves nothing and writes the annotated XML to `--out`. A JSON run log per document (decisions per unit, which candidate the arbiter chose, requests, retries, timings, rules; with `--raw` the raw answers) is always written to `--out` (default `auto-annotation-runs/<timestamp>/`).

## Modules

The modules `plugin.py`, `routes.py`, `sources.py`, `models.py`, `annotation.py` and `document.py` run on the server and import the editor's code; the others do not:

| Module | Purpose |
| --- | --- |
| `units.py` | Review units: unit paths, canonical form and fingerprints as in the editor's unit review mode, serialization |
| `batching.py` | Batch specs and batching |
| `prompts.py` | Prompts for correcting a batch of units, parsing the answer |
| `checks.py` | Checks of a proposed unit (well-formed, same outermost element, text unchanged) |
| `correction.py` | Running one model over units: `correct_units()`, with optional split-and-retry of failing batches; `ChatClient` protocol and `ProviderChatClient` for registered providers |
| `scoring.py` | Fields, edit and field scores, alignment of input and gold units |
| `evaluation.py` | Sweeps of batch specs and modes on one gold document, summaries |
| `report.py` | CSV, JSONL, JSON and Markdown output |
| `segmentation.py` | Scoring of segmenting variants on text positions; which variants are segmenting |
| `versions.py` | Telling extractions from annotated versions by their revision history |
| `arbitration.py` | Comparing proposals, candidates and prompt of the arbiter |
| `pipeline.py` | The pipeline: `annotate_units()` runs A and B, then the arbiter; a decision per unit |
| `model_spec.py` | Resolving model names |
| `models.py` | Resolving a model for a user, with key and access checks (server) |
| `document.py` | Writing a pipeline result or a single model's corrections into the TEI document: units, revision change (server) |
| `annotation.py` | Annotating a stored version, finding the versions of a collection to annotate (server) |
| `single.py` | Correcting one batch of units with a single model, checking the corrections a client sends back |
| `correction_routes.py` | Routes of the "Correct annotations in new version" action, the job's lock (server) |
| `extensions/auto-annotation.js` | The Tools menu action (frontend extension) |
| `probe_store.py` | Storage of the editor's model tests: run directories, status, heartbeat, result files |
| `probe_job.py` | Running a model test as a server job, cancelling it |
| `probe_compare.py` | Comparing stored tests: other documents of the model (pooled scores), other models on the document |
| `probe_views.py` | Result pages of the model tests |
| `probe_routes.py` | Routes of "Test model with current document" (server) |
| `extensions/auto-annotation-model-test.js` | The Tools menu items "Test model with current document" and "Model test results" (frontend extension) |
| `cli_common.py` | Connection options of the CLIs |
| `api_client.py` | HTTP client and `ServerChatClient` for the CLI |
| `probe.py` | The model probe CLI |
| `annotate.py` | The annotation pipeline CLI |

## Routes

For annotators, reviewers and admins, used by the Tools menu action (`<job>` is `{"job_id", "stable_id"}`):

- `POST /api/plugins/auto-annotation/correct/start` `{"stable_id", "provider_id", "model_id", "batch"}`: checks the version and the model, takes over the lock and returns the job id, the number of units and the unit indices of each batch; 423 if another session holds the lock.
- `POST /api/plugins/auto-annotation/correct/batch` `{<job>, "provider_id", "model_id", "units", "retries"}`: corrects the given units; per unit its status and the corrected XML if it changed. A failed request is reported in `error`. 409 if the job no longer holds the lock.
- `POST /api/plugins/auto-annotation/correct/heartbeat` `<job>`: refreshes the lock (409 if it was lost).
- `POST /api/plugins/auto-annotation/correct/finish` `{<job>, "provider_id", "model_id", "model_label", "corrections": [{"index", "xml"}], "processed", "invalid", "errors", "seconds"}`: checks the corrections and returns the document with them and the revision change; it does not save.
- `POST /api/plugins/auto-annotation/correct/release` `<job>`: releases the lock and sends `lockReleased` to the other sessions.

For reviewers and admins, used by "Test model with current document" (`<run>` is `{"run_id"}`):

- `POST /api/plugins/auto-annotation/probe/start` `{"reference_stable_id", "input_stable_id", "provider_id", "model_id", "model_label", "doc_label", "batches", "modes", "repeats", "concurrency", "retries", "temperature", "max_units", "force_busy"}`: checks the parameters, the model and the documents and starts the test; returns the run id, the number of runs and of units. 409 if the user has a test running or the model is busy, 422 for invalid parameters or documents without aligned units.
- `GET /api/plugins/auto-annotation/probe/status?run=<id>`: state (`running`, `done`, `failed`, `cancelled`, `interrupted`), runs finished and planned, the run under way, the error.
- `GET /api/plugins/auto-annotation/probe/jobs`: the user's running tests.
- `POST /api/plugins/auto-annotation/probe/cancel` `<run>` and `POST /api/plugins/auto-annotation/probe/delete` `<run>`: for the user who started the test and admins.
- `GET /api/plugins/auto-annotation/probe/view?run=<id>` and `GET /api/plugins/auto-annotation/probe/history[?model=<provider id>/<model id>]`: the pages; `GET /api/plugins/auto-annotation/probe/export?run=<id>[&file=<name>]`: a result file, or all as a zip.

The plugin endpoint `probe_results` (not in the menu) returns the URL of a test's page, or of the list, for the plugin result dialog.

For reviewers and admins, used by the CLIs:

- `GET /api/plugins/auto-annotation/eval-source?doc=<doc id>&variant=<variant>[&reference=<id>][&input=<id>][&rules=false]`: reference (the document's gold version of the variant, or `reference`) and input XML, variant, unit path, and the rules; with `reference`, `doc` and `variant` may be left out. Rules are the machine refs of the reference version's `editorialDecl`, else human refs, without user overrides.
- `POST /api/plugins/auto-annotation/resolve-model` `{"model": "<name>"}`: provider and model id, after checking model access.
- `POST /api/plugins/auto-annotation/complete` `{"provider_id", "model_id", "system_prompt", "user_prompt", "temperature"}`: one completion with the same model access rules as the LLM Reviewer.
- `GET /api/plugins/auto-annotation/targets?variant=<variant>&collection=<id>` or `&doc=<id>` (repeatable): per document, the version the pipeline would annotate and its state (`ready`, `annotated`, `gold`, `no-version`).
- `POST /api/plugins/auto-annotation/annotate` `{"stable_id", "model_a", "model_b", "arbiter", "batch", "arbiter_batch", "mode", "split_on_failure", "concurrency", "retries", "status", "label", "include_raw"}`: annotates a version and returns the new XML, the counts and the run log; it does not save.

## Tests

Unit tests with mock models (Python) and of the frontend extension (`*.test.js`) in `src/pte_plugin_auto_annotation/tests`, run from the repository root as described in the main README.
