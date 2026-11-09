# Is the three-model auto-annotation pipeline worth the effort?

Date: 2026-11-08. Document `10.5771__2699-1284-2024-3-149`, collection `grobid-batch-2`, variant `grobid.training.references`, on a local instance of the editor.

## Result

On this document, the pipeline of two models and an arbiter is not worth its cost: DeepSeek alone scores about the same. All scores are against the reference `38cs7r` on the 70 units that could be aligned:

| Setup | Exact (of 70) | Edit P | Edit R | Edit F1 per run | Edit F1 pooled |
| --- | --- | --- | --- | --- | --- |
| Extraction `gczy7r`, unchanged | 2 | - | 0.0 | - | - |
| Pipeline (Gemini + DeepSeek + Sonnet) | 15, 14 | 79.1, 77.3 | 83.4, 85.9 | 81.2, 81.4 | - |
| DeepSeek alone | 12, 14 | 77.6 | 84.1 | 80.8, 80.7 | 80.7 |
| Sonnet alone | 11, 12 | 79.7 | 79.0 | 79.9, 78.7 | 79.3 |
| Gemini alone | 8, 8 | 66.6 | 54.1 | 57.1, 62.2 | 59.7 |

Precision and recall of the single models are pooled over their two repeats; those of the pipeline are given per run.

- The pipeline is about 0.5 F1 above DeepSeek alone, too little to count with two runs per setup.
- A frontier model is not what produces the score here: Sonnet alone is slightly below DeepSeek alone.
- A setup with Kisski models alone is therefore plausible. DeepSeek alone already reaches the pipeline's level; whether a Kisski arbiter adds anything has not been tested (see [Could it be done with Kisski alone?](#could-it-be-done-with-kisski-alone)).

Limits:

- One document and two runs per setup.
- The reference `38cs7r` is a usable gold version but not final.
- 16 of the 86 input units and 25 of the 95 reference units have no text-identical counterpart and are left out. The probe leaves them out of its batches as well, so its batches differ slightly from the pipeline's.

## Setup

| Item | Value |
| --- | --- |
| Model A | `google/gemini-3.5-flash-lite` |
| Model B | `kisski/deepseek-v4.1-flash-1017`, chosen by the probe below |
| Arbiter | `anthropic/claude-sonnet-5-5` |
| Input | `gczy7r`, a new GROBID extraction with 86 review units |
| Reference | `38cs7r` |
| Code | pte-plugins PR #31, commits `e131872` (first pipeline run) and `af76127` (second) |

`$OUT` stands for the output directory. Run logs and results are in [`2026-11-08-auto-annotation-pr31-local-test-artifacts/`](2026-11-08-auto-annotation-pr31-local-test-artifacts/). Commands are run from the `pdf-tei-editor` root. `UV_NO_SYNC=1` keeps the editable plugin installs.

## Commands and raw results

### Choosing the Kisski model

The Kisski flash models were compared on the first 8 units of the document:

```bash
UV_NO_SYNC=1 uv run python -m pte_plugin_auto_annotation.probe \
  --model kisski/deepseek-v4-flash-0731 \
  --model kisski/deepseek-v4.1-flash-1017 \
  --reference 38cs7r \
  --batches units:8 --modes all --max-units 8 \
  --out $OUT/probe

UV_NO_SYNC=1 uv run python -m pte_plugin_auto_annotation.probe \
  --model kisski/glm-5.3-flash \
  --reference 38cs7r \
  --batches units:8 --modes all --max-units 8 --retries 0 \
  --out $OUT/probe-glm
```

| Model | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 |
| --- | --- | --- | --- | --- | --- | --- |
| (input as baseline) | 0 | 0.0 | - | 0.0 | - | 43.2 |
| `deepseek-v4-flash-0731` | 25.0 | 0.0 | 54.9 | 57.0 | 55.9 | 57.0 |
| **`deepseek-v4.1-flash-1017`** | 0 | 12.5 | 77.9 | 93.7 | 85.1 | 85.2 |
| `glm-5.3-flash` | 0 | 0.0 | 68.6 | 91.1 | 78.3 | 77.3 |

With 8 units and one run each, this supports only a coarse ranking.

### Extraction

```bash
node bin/batch-extract.js --extract-only \
  --collection grobid-batch-2 \
  --doc 10.5771__2699-1284-2024-3-149 \
  --extractor grobid \
  --option variant_id=grobid.training.references \
  --option flavor=article/footnotes-refs
```

### Pipeline, two runs

```bash
UV_NO_SYNC=1 uv run python -m pte_plugin_auto_annotation.annotate \
  --model-a google/gemini-3.5-flash-lite \
  --model-b kisski/deepseek-v4.1-flash-1017 \
  --arbiter anthropic/claude-sonnet-5-5 \
  --version gczy7r \
  --batch units:8 --concurrency 2 --raw \
  --out $OUT/annotate

# Comparison with the reference, using the plugin's own scoring
UV_NO_SYNC=1 uv run python compare.py gczy7r <annotated version> 38cs7r \
  $OUT/annotate/10.5771__2699-1284-2024-3-149.json
```

The runs produced the versions `yqtump` and `4uhyhb`. The logs of the second are under `rerun/`.

| | Run 1 | Run 2 |
| --- | --- | --- |
| Agreed / arbitrated / unresolved (of 86) | 17 / 69 / 0 | 10 / 76 / 0 |
| Exact (of 70 aligned) | 15 | 14 |
| Edit P / R / F1 | 79.1 / 83.4 / 81.2 | 77.3 / 85.9 / 81.4 |
| Field F1 | 79.4 | 78.9 |
| Edits made/needed | 612/580 | 644/580 |
| Invalid proposals, Gemini / DeepSeek | 7 / 0 | 8 / 0 |
| Arbiter chose Gemini / DeepSeek / own / original | 5 / 39 / 23 / 2 | 4 / 40 / 30 / 2 |

In run 2, among the 68 arbitrated units with valid proposals from both models, the arbiter chose Gemini 4 times, DeepSeek 33 times, its own correction 29 times and the original twice.

Quality by decision, on the 70 aligned units:

| Decision | Units, run 1 / 2 | Exact | Edit P | Edit R |
| --- | --- | --- | --- | --- |
| Agreed | 15 / 8 | 9 / 4 | 94.6 / 84.5 | 94.6 / 93.8 |
| Arbitrated, chose Gemini | 3 / 2 | 1 / 0 | 91.7 / 81.8 | 91.7 / 64.3 |
| Arbitrated, chose DeepSeek | 31 / 31 | 4 / 5 | 72.8 / 76.8 | 85.5 / 88.1 |
| Arbitrated, own correction | 19 / 27 | 1 / 5 | 76.1 / 75.8 | 74.4 / 84.7 |
| Arbitrated, kept original | 2 / 2 | 0 / 0 | - / - | 0.0 / 0.0 |

### Single models

Each model corrected the same extraction alone, with the pipeline's batch settings and two repeats, to match the two pipeline runs:

```bash
UV_NO_SYNC=1 uv run python -m pte_plugin_auto_annotation.probe \
  --model anthropic/claude-sonnet-5-5 \
  --model kisski/deepseek-v4.1-flash-1017 \
  --model google/gemini-3.5-flash-lite \
  --reference 38cs7r --input gczy7r \
  --batches units:8 --modes all --repeats 2 --concurrency 2 \
  --out $OUT/single-model
```

The results are in the table under [Result](#result) and in the artifacts under `single-model/`.

## What the pipeline costs and what it returns

| Setup | Units | Calls per run | Prompt characters per run | Summed request seconds per run |
| --- | --- | --- | --- | --- |
| DeepSeek alone | 70 | 9 | 331,344 | 532 |
| Sonnet alone | 70 | 9 | 331,344 | 99 |
| Gemini alone | 70 | 9 | 331,344 | 26 |
| Pipeline, run 2 | 86 | 34 | 1,330,997, of which 437,894 to Sonnet | 1,045 |

The idea of the pipeline is to spend the frontier model only where the small models disagree. On this document that does not save anything:

- Models agreed on only 10 and 17 of 86 units, so the arbiter still handled 80–88% of the document.
- Its share is larger than a Sonnet-only pass would have been: scaled to 86 units, about 407,000 prompt characters against 437,894 in run 2. The candidates shown to the arbiter make each of its prompts longer.
- The pipeline's gain over DeepSeek alone is 0.4–0.7 F1 and at most two or three exact units per run.
- Gemini contributes little. It is far weaker alone (59.7), the arbiter chose it 4 or 5 times per run, and it was the only model with invalid proposals in the pipeline runs.

The agreement signal is the one thing a single model cannot give, but it is not stable. Identical settings gave 17 and 10 agreed units, and their edit precision fell from 94.6 to 84.5.

## Could it be done with Kisski alone?

Yes, in part. The measured result already supports dropping the frontier model for the correction itself. What is still unknown is whether a second Kisski opinion is useful.

**What the data supports.** The frontier model is not the source of the quality:

- DeepSeek alone reaches 80.7, against 79.3 for Sonnet alone.
- When the arbiter wrote its own correction, the result was no better than when it took DeepSeek's: edit precision 76.1 and 75.8 against 72.8 and 76.8, edit recall 74.4 and 84.7 against 85.5 and 88.1.
- Removing the arbiter costs at most the 0.4–0.7 F1 measured above, which is within what two runs can show.

The simplest Kisski-only setup is therefore DeepSeek alone. All 14 models that Kisski lists in the instance's provider overview are marked as free.

**What it would give up.** A single model provides no agreement signal, so no hint about which units a reviewer should check first. To keep that signal with Kisski alone, a second model of similar quality is needed. `deepseek-v4-flash-0731` is not good enough for that: it reached 55.9 on the 8-unit probe, with 25% failures. Gemini agreed with DeepSeek on only 12–20% of units, which suggests that a much weaker second model mostly generates disagreement. `glm-5.3-flash` is the best candidate so far, with 78.3 on the same 8 units and no failures, but its precision is lower than DeepSeek's (68.6 against 77.9), and 8 units are too few to rely on.

**What has not been tested.** Kisski also offers larger models that could act as arbiter or as the second opinion, among them `glm-5.3`, `mistral-large-4-1t-a52b` and `qwen3.5-397b-a17b`. No score exists for them. A larger Kisski model as arbiter is the setup most likely to reproduce the pipeline at no model cost, but its value depends on the same weak point: an arbiter has to add more than DeepSeek already delivers.

**Practical constraints of Kisski.**

- Speed: DeepSeek needs about 5 times Sonnet's request time on the same units (532 s against 99 s per run, summed).
- `glm-5.3-flash` is much slower: its single 8-unit call took 539 s, summed over attempts, against 9–59 s for a normal DeepSeek call. Scaled to this document's 11 batches, that would be about 100 minutes of request time. The figure is consistent with two timed-out attempts at 180 s and a third answering just within that limit.
- Reliability: 7 of 40 DeepSeek calls took 162–180 s instead of 9–59 s, with nothing in between. This fits a stalled request followed by a normal retry. It slows a run but did not lose units.
- Rate limits, availability under load, and the data-protection conditions of the service were not assessed.

**Where the pipeline could still pay off.** A pipeline only saves arbiter effort if the small models agree often and are right when they do. Better or fine-tuned small models should raise both, which is where the case for two models and an arbiter could return.

## Recommendation

On the evidence of this document:

- Use `deepseek-v4.1-flash-1017` alone as the default for first annotations, and treat it as the baseline any pipeline has to beat.
- Do not use Gemini as a second opinion for this task.
- Do not invest further in the three-model setup with a frontier arbiter unless a further document shows a clear gain.

## Next steps

- Repeat the comparison on the second gold document, `10.12775__clr.2013.008` (gold version `ba7zek`).
- Test a Kisski-only pipeline against DeepSeek alone: DeepSeek v4.1 with `glm-5.3-flash` as second model, if its speed is acceptable, and one of the larger Kisski models as arbiter.
- Repeat this comparison once fine-tuned models are available.
