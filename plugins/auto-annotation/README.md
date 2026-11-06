# Auto-annotation

Building blocks for an automated first annotation of GROBID training data: cheap LLMs correct GROBID's output against the document's machine rules, a stronger model arbitrates where they disagree. The design is in the proposal `2026-11-05-auto-annotation-proposal.md` (project files; to be moved to `docs/history` in pdf-tei-editor with the implementation).

It contains:

- the **model probe**, which measures how well a single model corrects the annotation of gold documents, and how that changes with the number of review units sent in one request;
- the **annotation pipeline**, which annotates documents or a whole collection with two models and an arbiter and saves the result as a new version.

## Model probe

```bash
uv run python -m pte_plugin_auto_annotation.probe \
  --model kisski/llama-3.3-70b-instruct --model endpoint-ollama/qwen2.5:14b \
  --doc 10.1234__abc --doc 10.1234__def \
  --batches units:1,units:4,units:8,units:16,units:32 --modes all,changed --repeats 2
```

The installed package also provides the command `pte-model-probe` with the same options. Run `--help` for all options.

- `--model`: a model as configured in the app: `default`, `<provider>`, or `<provider>/<model>`. `<provider>` is a provider id (`kisski`, `endpoint-ollama`), its label (an inference endpoint's name) or an endpoint id without the `endpoint-` prefix; `<model>` is a model id or label. With `<provider>` alone, the provider's only model or the default model is used.
- `--doc`: a document id. Its gold version of `--variant` (default `grobid.training.references`) is the reference the model's corrections are scored against. The version the model corrects is the extraction the reference was derived from (the oldest extraction among the versions' parents), else the earliest extraction of the same document and variant (ties by creation time); without an extraction, the probe stops and asks for `--input`. A version counts as an extraction if its revision history starts with the extraction and has only metadata changes after it (renamed, document rules updated); label and status are not reliable (`versions.py`). The report says how the input was chosen, and marks a given `--input` that is not an extraction.
- `--reference`: the stable id of a version to score against instead of the gold version, e.g. for a document without a gold version yet; the report marks it as "not the gold version". Its document and variant are used, so `--doc` and `--variant` can be left out.
- `--batches`: batch specs, `units:N` (at most N units per request) or `chars:N` (at most N characters of unit XML).
- `--modes`: `all` (the model returns every unit) or `changed` (only the units it changed).
- `--retries`: how often a request is repeated after a provider overload or rate limit (HTTP 429/503), with exponential backoff from 2 s (default 4). Requests that still fail are reported as errors and left out of the scores, since they say nothing about the model.
- `--rules-file`: pin the rules to a local file instead of the reference version's rule refs.

It runs against a running editor and logs in like `bin/batch-extract.js`: `--base-url`, `--user`, `--password`, or `API_BASE_URL`, `API_USER`, `API_PASSWORD` from the environment or `--env` (default `./.env`). The user needs the reviewer or admin role and access to the models.

### What it measures

1. The review units of the input and the reference version (the variant's `reviewUnits` path, e.g. `.//listBibl/bibl`) are aligned by their text. Units whose text the annotator changed, split or merged are left out and counted.
2. For every batch spec, mode and repetition, the input units are sent in batches, each unit under a `### Unit <n>` header; the answer uses the same format, so the model never escapes XML in JSON.
3. Every returned unit must be well-formed, keep its outermost element and keep its text (whitespace ignored). Units failing these checks, or missing from the answer, count as failures and are scored as the unchanged input. Units of requests that failed (provider errors, after retries) count as errors and are not scored.
4. Units are scored by fields, one (element path, attributes, text) tuple per element. **Edit precision/recall** compare the changes the model made to the input with the changes the reference version needs; field F1 and exact unit matches are reported too. The first row of each table is the input itself (baseline).
5. Edit recall by the unit's position in the batch (quarters) shows whether quality drops towards the end of long answers.

Output in `--out` (default `probe-results/<timestamp>/`): `report.md`, `summary.json`, `units.csv` (one row per unit and run) and `batches.jsonl` (one row per request, with the raw answer). The files are rewritten after every run (batch spec, mode and repetition), so a probe that is interrupted or fails keeps the finished runs; the report marks documents whose runs are incomplete.


#### Segmenting variants

For `grobid.training.references.referenceSegmenter`, correcting a unit means splitting or merging it, so the steps above change (`segmentation.py`):

- The prompt allows a unit to be returned as several `<bibl>` elements, or empty when its text moves into a neighbouring unit.
- A returned unit must consist of `<bibl>` elements with only whitespace between them. Its text is checked first per unit; a unit whose own text changed is accepted when the batch as a whole keeps its text and no unit of the batch failed otherwise.
- Units are not matched one-to-one. Input and reference version are read as one text stream (whitespace removed), each element becomes a field with its start and end position in that stream, and the reference's fields are mapped onto the input's text where the two agree. A field is credited to the input unit in which it starts; input units whose text the reference does not share entirely are left out and counted as unmatched. Edits are then counted as above.

## Annotation pipeline

```bash
uv run python -m pte_plugin_auto_annotation.annotate \
  --model-a kisski/llama-3.3-70b-instruct --model-b gemini/gemini-2.5-flash-lite --arbiter anthropic/<model> \
  --collection my-collection
```

The installed package also provides the command `pte-auto-annotate`. Run `--help` for all options; the connection options are those of the probe.

What to annotate (exactly one of):

- `--collection`: all documents of a collection that have a version of `--variant` (default `grobid.training.references`);
- `--doc`: given document ids (repeatable);
- `--version`: given stable ids (repeatable), annotated without the checks below.

For `--collection` and `--doc`, the earliest version of the variant (normally the extraction) is annotated. A document is skipped if one of its versions was already annotated by the pipeline (its `revisionDesc` has a change by `#auto-annotation`; `--force` annotates it again) or if it has a gold version (`--include-gold`). A repeated run therefore continues where an interrupted one stopped.

Per document, on the server:

1. Models A and B correct all review units independently, with batching, split-and-retry and retries as in the probe (`--batch`, default `units:16`; `--mode`, `--split-on-failure`, `--concurrency`, `--retries`). Every proposal is checked (well-formed, same outermost element, text unchanged).
2. Units on which both models return valid, canonically equal proposals are **agreed** and take that proposal.
3. The other units go to the arbiter (`--arbiter-batch`, default `units:8`), which sees the original and the distinct valid candidates and answers with a candidate, the original or its own correction (if both models failed, it corrects the unit itself). A valid answer makes the unit **arbitrated**.
4. Units without an arbiter, or whose arbiter answer fails the checks, are **unresolved** and keep the original.

The result is saved by the client as a new version of the input (through the editor's save API, so version numbers, collections and locks are handled as for a manual save). The new version has:

- the changed units replaced; the rest of the document keeps its formatting;
- a `revisionDesc/change` by `#auto-annotation` (status `--status`, default `extraction`; label `--label`, default `Auto-annotation`) naming the models and the counts, and a `respStmt` for `#auto-annotation`;
- unless `--no-marks`: unit review marks of the reviewer `llm-auto-annotation` in `standOff/listAnnotation[@type="unit-review"]`, as written by the editor's unit review mode: agreed units are OK, arbitrated and unresolved units are not OK, with a comment saying why. A reviewer can thus start with the not-OK units.

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
| `document.py` | Writing a pipeline result into the TEI document: units, revision change, review marks (server) |
| `annotation.py` | Annotating a stored version, finding the versions of a collection to annotate (server) |
| `cli_common.py` | Connection options of the CLIs |
| `api_client.py` | HTTP client and `ServerChatClient` for the CLI |
| `probe.py` | The model probe CLI |
| `annotate.py` | The annotation pipeline CLI |

## Routes

All for reviewers and admins:

- `GET /api/plugins/auto-annotation/eval-source?doc=<doc id>[&variant=<variant>][&reference=<id>][&input=<id>][&rules=false]`: reference (the document's gold version of the variant, or `reference`) and input XML, variant, unit path, and the rules (machine refs of the reference version's `editorialDecl`, else human refs, without user overrides).
- `POST /api/plugins/auto-annotation/resolve-model` `{"model": "<name>"}`: provider and model id, after checking model access.
- `POST /api/plugins/auto-annotation/complete` `{"provider_id", "model_id", "system_prompt", "user_prompt", "temperature"}`: one completion with the same model access rules as the LLM Reviewer.
- `GET /api/plugins/auto-annotation/targets?variant=<variant>&collection=<id>` or `&doc=<id>` (repeatable): per document, the version the pipeline would annotate and its state (`ready`, `annotated`, `gold`, `no-version`).
- `POST /api/plugins/auto-annotation/annotate` `{"stable_id", "model_a", "model_b", "arbiter", "batch", "arbiter_batch", "mode", "split_on_failure", "concurrency", "retries", "marks", "status", "label", "include_raw"}`: annotates a version and returns the new XML, the counts and the run log; it does not save.

## Tests

Unit tests with mock models in `src/pte_plugin_auto_annotation/tests`, run from the repository root as described in the main README.
