# Auto-annotation

Building blocks for an automated first annotation of GROBID training data: cheap LLMs correct GROBID's output against the document's machine rules, a stronger model arbitrates where they disagree. The design is in the proposal `2026-11-05-auto-annotation-proposal.md` (project files; to be moved to `docs/history` in pdf-tei-editor with the implementation).

This first version contains the **model probe**: it measures how well a single model corrects the annotation of gold documents, and how that changes with the number of review units sent in one request.

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

## Modules

The modules other than `plugin.py`, `routes.py` and `sources.py` do not import the editor's code and are meant to be reused by the pipeline:

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
| `versions.py` | Telling extractions from annotated versions by their revision history |
| `model_spec.py` | Resolving model names |
| `api_client.py` | HTTP client and `ServerChatClient` for the CLI |
| `probe.py` | The CLI |

## Routes

All for reviewers and admins:

- `GET /api/plugins/auto-annotation/eval-source?doc=<doc id>[&variant=<variant>][&reference=<id>][&input=<id>][&rules=false]`: reference (the document's gold version of the variant, or `reference`) and input XML, variant, unit path, and the rules (machine refs of the reference version's `editorialDecl`, else human refs, without user overrides).
- `POST /api/plugins/auto-annotation/resolve-model` `{"model": "<name>"}`: provider and model id, after checking model access.
- `POST /api/plugins/auto-annotation/complete` `{"provider_id", "model_id", "system_prompt", "user_prompt", "temperature"}`: one completion with the same model access rules as the LLM Reviewer.

## Tests

Unit tests with mock models in `src/pte_plugin_auto_annotation/tests`, run from the repository root as described in the main README.
