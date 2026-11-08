# Auto-annotation PR #31: local end-to-end test

Date: 2026-11-08. Instance: `http://localhost:8000`, authenticated with `API_TOKEN` from `.env`.

Document `10.5771__2699-1284-2024-3-149`, collection `grobid-batch-2`, variant `grobid.training.references`.

## Result

The pipeline works end to end on real models. A new extraction (`gczy7r`) was annotated and saved as `yqtump`, with text, revision entry, review marks, schema validity and lock release all correct.

Against the reference `38cs7r`, on the 70 units that could be aligned:

| Version | Units | Exact | Edit P | Edit R | Edit F1 | Field F1 | Edits made/needed |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Extraction `gczy7r` (baseline) | 70 | 2 (2.9%) | - | 0.0 | - | 40.9 | 0/580 |
| Auto-annotation `yqtump` | 70 | 15 (21.4%) | 79.1 | 83.4 | 81.2 | 79.4 | 612/580 |

Limits of this comparison:

- `38cs7r` ("v3 (cboulanger)") is **not flagged gold**. No `grobid.training.references` version of this document is. It was used as the reference by decision for this test.
- 16 of the 86 input units and 25 of the 95 reference units have no text-identical counterpart and are left out. `38cs7r` was derived from the older extraction `bfn4g7`, not from `gczy7r`, and its annotator split and corrected units.
- This is one document, one run.

## Setup

| Item | Value |
| --- | --- |
| pte-plugins | branch `claude/project-thread-kqqrnw` (PR #31), commit `e131872`, pushed |
| pdf-tei-editor | `devel` at `d9ae9cdf`; the `--doc` change used in the run was committed afterwards as `64cff68c` |
| Plugin install | Editable (`.venv` points at the pte-plugins checkout), so no reinstall is needed |
| Model A | `google/gemini-3.5-flash-lite` |
| Model B | `kisski/deepseek-v4.1-flash-1017` (chosen by the probe below) |
| Arbiter | `anthropic/claude-sonnet-5-5` |

`$OUT` stands for the output directory of the run. Run logs and outputs are in [`2026-11-08-auto-annotation-pr31-local-test-artifacts/`](2026-11-08-auto-annotation-pr31-local-test-artifacts/). Commands are run from the `pdf-tei-editor` root. `UV_NO_SYNC=1` keeps the editable plugin installs.

## Commands

### 1. Available models and starting state

```bash
node scripts/dev/debug-api.js GET /api/v1/llm/providers
node scripts/dev/debug-api.js GET /api/plugins/auto-annotation/targets \
  '{"variant":"grobid.training.references","doc":"10.5771__2699-1284-2024-3-149"}'
sqlite3 -header -column data/db/metadata.db \
  "select stable_id,version,is_gold_standard,deleted,label,parent_stable_id,created_at
   from files where doc_id='10.5771__2699-1284-2024-3-149' and variant='grobid.training.references'
   order by created_at"
```

Kisski offers three flash models: `deepseek-v4-flash-0731`, `deepseek-v4.1-flash-1017`, `glm-5.3-flash`.

### 2. Kisski probe (8 units)

```bash
UV_NO_SYNC=1 uv run python -m pte_plugin_auto_annotation.probe \
  --model kisski/deepseek-v4-flash-0731 \
  --model kisski/deepseek-v4.1-flash-1017 \
  --model kisski/glm-5.3-flash \
  --reference 38cs7r \
  --batches units:8 --modes all --max-units 8 \
  --out $OUT/probe
```

The probe selected the input itself: `bfn4g7`, "the extraction the reference was derived from".

| Model | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Seconds |
| --- | --- | --- | --- | --- | --- | --- | --- |
| (input as baseline) | 0 | 0.0 | - | 0.0 | - | 43.2 | - |
| `deepseek-v4-flash-0731` | 25.0 | 0.0 | 54.9 | 57.0 | 55.9 | 57.0 | 12.6 |
| **`deepseek-v4.1-flash-1017`** | 0 | 12.5 | 77.9 | 93.7 | 85.1 | 85.2 | 169.1 |
| `glm-5.3-flash` | no result | - | - | - | - | - | 2464.4 |

`deepseek-v4.1-flash-1017` was chosen. `glm-5.3-flash` could not be scored: all 5 attempts ended in a 503 from a 120 s read timeout on `chat-ai.academiccloud.de`. This says nothing about its quality. With 8 units and one run each, the probe only supports a coarse ranking.

### 3. New extraction

```bash
node bin/batch-extract.js --extract-only \
  --collection grobid-batch-2 \
  --doc 10.5771__2699-1284-2024-3-149 \
  --extractor grobid \
  --option variant_id=grobid.training.references \
  --option flavor=article/footnotes-refs
```

`--doc` is new (finding 2). The flavor matches the existing extraction `bfn4g7`. Result: `gczy7r`, label "Extraction (2026-11-08 13:29:20)", 86 review units, with the machine rule references to the fossil guidelines.

### 4. Auto-annotation

```bash
UV_NO_SYNC=1 uv run python -m pte_plugin_auto_annotation.annotate \
  --model-a google/gemini-3.5-flash-lite \
  --model-b kisski/deepseek-v4.1-flash-1017 \
  --arbiter anthropic/claude-sonnet-5-5 \
  --version gczy7r \
  --batch units:8 --concurrency 2 --raw \
  --out $OUT/annotate
```

`--version` is needed because `--doc` would select the earliest version, `bfn4g7` (finding 4). The run took about 7 minutes and saved `yqtump`, version 3, parent `gczy7r`.

| Run | Calls | Retries | Request seconds (sum) |
| --- | --- | --- | --- |
| A (Gemini) | 13 | 0 | 69.3 |
| B (DeepSeek) | 11 | 0 | 573.9 |
| Arbiter (Sonnet) | 9 | 0 | 113.0 |

Decisions on the 86 units: 17 agreed, 69 arbitrated, 0 unresolved, 82 changed. The arbiter chose B 39 times, its own correction 23 times, A 5 times and the original twice. Model A returned 7 units with changed text, which were rejected by the checks and sent to the arbiter; model B returned none.

### 5. Verification

```bash
# Comparison with the reference, using the plugin's own scoring
UV_NO_SYNC=1 uv run python compare.py gczy7r yqtump 38cs7r \
  $OUT/annotate/10.5771__2699-1284-2024-3-149.json

# Schema validation (body: the XML of yqtump)
node scripts/dev/debug-api.js POST /api/v1/validate '{"xml_string": "..."}'

# Locks on the new versions
sqlite3 data/db/locks.db "select * from locks where file_id in ('yqtump','gczy7r')"
```

| Check | Result |
| --- | --- |
| Text of all 86 units unchanged from `gczy7r` (whitespace ignored) | Pass |
| Unit count preserved (86), no group spanning several units | Pass |
| Differences outside the units: only `respStmt`, revision `change`, `standOff` marks | Pass |
| `revisionDesc/change` by `#auto-annotation` names models and counts | Pass |
| Review marks: 17 OK, 69 not-OK with a comment, reviewer `llm-auto-annotation` | Pass |
| Schema validation: `{"errors": []}` | Pass |
| Lock released after save | Pass |
| Saved in `grobid-batch-2` with parent `gczy7r` | Pass |

Quality by decision, on the 70 aligned units:

| Decision | Units | Exact | Edit P | Edit R |
| --- | --- | --- | --- | --- |
| Agreed | 15 | 9 | 94.6 | 94.6 |
| Arbitrated, chose A | 3 | 1 | 91.7 | 91.7 |
| Arbitrated, chose B | 31 | 4 | 72.8 | 85.5 |
| Arbitrated, own correction | 19 | 1 | 76.1 | 74.4 |
| Arbitrated, kept original | 2 | 0 | - | 0.0 |

Agreed units are clearly the most reliable, which supports marking them OK and sending reviewers to the not-OK units first.

## Findings

| # | Problem | Status |
| --- | --- | --- |
| 1 | The plugin CLIs had no access-token login, only username and password. `API_TOKEN` did not exist on any ref of pte-plugins, so neither merging upstream nor reinstalling could help. | Fixed in `e131872`: `--token` / `API_TOKEN`, preferred over the password, through `/api/v1/auth/token-login` |
| 2 | `bin/batch-extract.js` could not target a single document: `--extract-only` processes the whole collection, and upload mode skips PDFs already in it. | Fixed in pdf-tei-editor `64cff68c`: `--doc <id>`, repeatable |
| 3 | `POST /annotate` is one request per document, and the CLI limited it to 900 s, a value meant for one completion. | Fixed in `e131872`: `--timeout`, default 3600 s. A precaution: this run took about 7 minutes, so the old limit was not reached |
| 4 | `--collection` and `--doc` always annotate the earliest version, so a re-extraction is ignored unless `--version` is used. | Documented behaviour, unchanged. Worth a decision if re-extraction is a normal workflow |
| 5 | `glm-5.3-flash` hits the 120 s read timeout, and one failed request occupied the probe for 41 minutes because of repeated retries. | Not fixed. Outside PR #31; the place where the 120 s is set was not determined |

In the plugin tests, the CLI tests had to mock `ApiSession.connect` instead of `login`: importing `fastapi_app` during collection loads the editor's `.env`, so the real `API_TOKEN` would otherwise be picked up.

## Tests

| Suite | Result |
| --- | --- |
| Plugin tests (`auto-annotation`) | 115 passed, including new tests for token login and timeout |
| Pyright on the changed plugin modules | 0 errors |
| `npm run test:unit` (editor) | Passed; the FastAPI part ran 1589 tests |
| `npm run test:e2e` (editor) | 101 passed, 10 skipped, 1 failed |

The E2E failure is `plugin-admin.spec.js:110` ("confirming an untrusted repository"). It uses a mocked API, is unrelated to these changes, and passed twice when rerun alone, so it looks flaky. A first E2E run failed entirely because Playwright's Chromium was not installed; `npx playwright install chromium` fixed that.

## Open items

- Decide whether `annotate --doc` should keep selecting the earliest version (finding 4).
- Find where the 120 s Kisski read timeout is set (finding 5).
- `64cff68c` is committed on `devel` but not pushed.
