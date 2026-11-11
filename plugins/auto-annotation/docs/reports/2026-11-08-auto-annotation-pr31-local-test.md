# Is the three-model auto-annotation pipeline worth the effort?

Date: 2026-11-08, updated 2026-11-09 to 2026-11-11. Documents `10.5771__2699-1284-2024-3-149` and `10.12775__clr.2013.008`, variant `grobid.training.references`, on a local instance of the editor.

## Result

The pipeline of two models and an arbiter is not worth its cost as the default. DeepSeek alone is within about 1 F1 of the frontier pipeline on both documents, at a fraction of the effort. A pipeline with Kisski models alone is better than DeepSeek where its arbiter answers, but today that arbiter is too unreliable and slow to use.

Edit F1 against the gold versions (`38cs7r`, 70 aligned units; `ba7zek`, 56 aligned units). Two values are two runs:

| Setup | 10.5771 | clr |
| --- | --- | --- |
| Pipeline: Gemini + DeepSeek, Sonnet as arbiter | 81.2, 81.4 | 84.4, 85.9 |
| Pipeline: `qwen3.6` + DeepSeek, `glm-5.3` as arbiter | 55.6, 49.6 | 80.8, 81.1 |
| DeepSeek alone | 80.8, 80.7 | 85.0, 83.5 |
| Sonnet alone | 79.9, 78.7 | 82.5, 83.3 |
| `gemma-4-31b-it` alone | 71.9, 71.3 | 73.0, 73.0 |
| `qwen3.6-35b-a3b` alone | 62.4, 58.7 | 63.1, 62.2 |
| Gemini alone | 57.1, 62.2 | 66.0, 70.0 |

- **Frontier pipeline:** 0.5 F1 above DeepSeek alone on 10.5771 and 0.9 on clr (means of two runs). That is too little to count with two runs per setup.
- **Frontier model:** not what produces the score. Sonnet alone is below DeepSeek alone on both documents.
- **Kisski-only pipeline:** its low scores come from units the arbiter never answered, which keep their uncorrected text. The arbiter failed on 8 of 64 units on clr and 48 of 86 on 10.5771.
- **Where the Kisski arbiter answered:** F1 is 0.8–4.3 above DeepSeek on the same units, see [Could it be done with Kisski alone?](#could-it-be-done-with-kisski-alone).

Limits:

- Two documents and two runs per setup.
- The gold versions are usable but not final.
- Units with no text-identical counterpart are left out: 16 input and 25 reference units on 10.5771, 8 and 12 on clr.
- The clr pipelines use the rules of the gold version, because the rules recorded in that extraction are outdated (see [Rule text](#rule-text)).

## Setup

| Item | Value |
| --- | --- |
| 10.5771 | input `gczy7r` (new GROBID extraction, 86 units), gold `38cs7r` |
| clr | input `qbb9sh` (64 units), gold `ba7zek` |
| Frontier pipeline | A `google/gemini-3.5-flash-lite`, B `kisski/deepseek-v4.1-flash-1017`, arbiter `anthropic/claude-sonnet-5-5` |
| Kisski-only pipeline | A `kisski/qwen3.6-35b-a3b`, B `kisski/deepseek-v4.1-flash-1017`, arbiter `kisski/glm-5.3` |
| Batching | `units:8`, all units, concurrency 2 |

Commands are run from the `pdf-tei-editor` root, `UV_NO_SYNC=1` keeps the editable plugin installs and `$OUT` stands for the output directory. Run logs, results and the scoring scripts are in [`2026-11-08-auto-annotation-pr31-local-test-artifacts/`](2026-11-08-auto-annotation-pr31-local-test-artifacts/): `annotate/`, `rerun/`, `single-model/` for 10.5771, `second-document/` for clr, `kisski-only/` for all Kisski-only runs and the `qwen3.6` and `gemma-4` probes.

## Commands

Pipeline on 10.5771, with `--dry-run` for the Kisski-only runs:

```bash
UV_NO_SYNC=1 uv run python -m pte_plugin_auto_annotation.annotate \
  --model-a kisski/qwen3.6-35b-a3b \
  --model-b kisski/deepseek-v4.1-flash-1017 \
  --arbiter kisski/glm-5.3 \
  --version gczy7r \
  --batch units:8 --concurrency 2 --raw --dry-run \
  --out $OUT/pipeline
```

Pipeline on clr, with the rules of the gold version:

```bash
UV_NO_SYNC=1 uv run python pipeline_goldrules.py $OUT/pipeline \
  kisski/qwen3.6-35b-a3b kisski/deepseek-v4.1-flash-1017 kisski/glm-5.3 ba7zek qbb9sh
```

Single models:

```bash
UV_NO_SYNC=1 uv run python -m pte_plugin_auto_annotation.probe \
  --model kisski/deepseek-v4.1-flash-1017 \
  --reference ba7zek --input qbb9sh \
  --batches units:8 --modes all --repeats 2 --concurrency 2 \
  --out $OUT/single-model
```

Scoring of dry-run results, and comparison with DeepSeek on the same units:

```bash
UV_NO_SYNC=1 uv run python score_dry.py qbb9sh ba7zek $OUT/pipeline
UV_NO_SYNC=1 uv run python compare_resolved.py qbb9sh ba7zek $OUT/single-model/units.csv $OUT/pipeline
```

## Detailed results

### Precision, recall, exact units

| Setup | Document | Exact | Edit P | Edit R |
| --- | --- | --- | --- | --- |
| Frontier pipeline | 10.5771 | 15, 14 of 70 | 79.1, 77.3 | 83.4, 85.9 |
| Frontier pipeline | clr | 20, 19 of 56 | 81.9, 81.8 | 87.0, 90.4 |
| Kisski-only pipeline | 10.5771 | 14, 13 of 70 | 83.4, 82.1 | 41.7, 35.5 |
| Kisski-only pipeline | clr | 18, 19 of 56 | 84.6, 84.3 | 77.4, 78.1 |
| DeepSeek alone | 10.5771 | 12, 14 of 70 | 77.6 | 84.1 |
| DeepSeek alone | clr | 19, 18 of 56 | 81.9 | 86.8 |
| Sonnet alone | 10.5771 | 11, 12 of 70 | 79.7 | 79.0 |
| Sonnet alone | clr | 19, 19 of 56 | 87.0 | 79.2 |
| `gemma-4-31b-it` alone | 10.5771 | 5, 3 of 70 | 71.2 | 72.0 |
| `gemma-4-31b-it` alone | clr | 12, 12 of 56 | 79.5 | 67.6 |
| `qwen3.6-35b-a3b` alone | 10.5771 | 4, 3 of 70 | 72.7 | 51.9 |
| `qwen3.6-35b-a3b` alone | clr | 7, 6 of 56 | 73.2 | 54.7 |

Precision and recall of the single models are pooled over their two repeats. In the probes, 11–20% of the `gemma-4` units and 11% of the `qwen3.6` units failed, against about 1% for DeepSeek and Sonnet on clr.

### Agreement between the two correctors

| Pipeline | Document | Agreed units (of all) | Edit F1 of agreed units |
| --- | --- | --- | --- |
| Frontier | 10.5771 | 17, 10 of 86 | 94.6, 88.9 |
| Frontier | clr | 21, 23 of 64 | 89.9, 90.6 |
| Kisski-only | 10.5771 | 13, 8 of 86 | 78.0, 85.0 |
| Kisski-only | clr | 16, 19 of 64 | 92.0, 92.9 |

Agreement is the one thing a single model cannot provide, but it is neither frequent nor stable. On Kisski-only 10.5771, agreed units reach only 67.6 and 73.9 edit precision. It is good enough as a review hint, not as a reason to skip a check.

### What the arbiter chose

In the Kisski-only runs the arbiter took DeepSeek 109 times, its own correction 18 times and `qwen3.6` 5 times, of 132 decisions. `qwen3.6` had 30 invalid proposals against 1 for DeepSeek. In the frontier runs the picture is the same: DeepSeek 138 times, its own correction 75 times and Gemini 12 times over its four runs.

### Rule text

The rules sent to the models matter more than anything measured here. The frontier pipeline on clr was run twice with the rules recorded in the extraction, which point to the long `guidelines.md`, and twice with the rules of the gold version:

| Rules | Edit F1 | Prompt characters per corrector |
| --- | --- | --- |
| Recorded in the extraction (`guidelines.md`) | 71.1, 73.7 | 542,038 |
| Of the gold version | 84.4, 85.9 | 302,342 |

That is a gain of 12–13 F1, larger than any gap between the DeepSeek-based setups. 10.5771 is not affected: its recorded rules already point to `guidelines-llm.md`. Older extractions should be re-annotated with current rules, and `annotate` should warn when an extraction records an outdated rule source.

## What the pipeline costs and what it returns

Summed request seconds per run, which is the load on the services. Wall time of the Kisski-only pipelines in parentheses:

| Setup | 10.5771 | clr |
| --- | --- | --- |
| Gemini alone | 26 | 44 |
| Sonnet alone | 99 | 70 |
| `gemma-4-31b-it` alone | 229 | 163 |
| `qwen3.6-35b-a3b` alone | 610 | 479 |
| DeepSeek alone | 532 | 796 |
| Frontier pipeline | 1,045 | 938, 1,349 |
| Kisski-only pipeline | 4,449, 4,765 (33, 34 min) | 7,555, 5,365 (65, 41 min) |

The idea of the pipeline is to spend the strong model only where the small models disagree. It does not save anything:

- Models agreed on 9–36% of units, so the arbiter still handled most of each document.
- The frontier arbiter costs more prompt characters than a Sonnet-only pass would have: 437,894 against about 407,000 on 10.5771, because the candidates make each prompt longer.
- A weak first corrector mostly creates disagreement, so the arbiter has more work, and the arbiter then mostly picks DeepSeek.

## Could it be done with Kisski alone?

Yes for the correction itself: DeepSeek alone reaches the level of the frontier pipeline. Whether a Kisski arbiter is worth adding depends on the failures of `glm-5.3`.

**What the arbiter adds when it answers.** On the units that the Kisski-only pipeline resolved, compared with DeepSeek alone on exactly the same units:

| Run | Resolved aligned units | Pipeline F1 | DeepSeek F1, two repeats |
| --- | --- | --- | --- |
| clr, run 1 | 48 of 56 | 87.7 | 86.9, 83.4 |
| clr, run 2 | 49 of 56 | 86.8 | 83.7, 84.4 |
| 10.5771, run 1 | 34 of 70 | 88.0 | 85.5, 85.8 |
| 10.5771, run 2 | 31 of 70 | 85.7 | 83.2, 82.2 |

The pipeline is ahead in all 8 comparisons, by 0.8–4.3 F1 and 2–3 F1 on average. This is more than the frontier pipeline gains over DeepSeek on whole documents, although it is measured on fewer units.

**What the arbiter loses.** `glm-5.3` is the reason for the low totals. On clr, one arbiter batch per run failed after repeated retries, leaving 8 units unresolved each time. On 10.5771, 6 of 10 arbiter batches failed with a connection error in both runs, leaving 48 of 86 units unresolved. Unresolved units keep the extraction's text and contribute no edits.

**What a fallback would give.** Assigning DeepSeek's correction to unresolved units, estimated from a separate DeepSeek run on those units: 85.6–87.8 on clr (DeepSeek alone: 83.5–85.0) and 81.7–82.0 on 10.5771 (80.7–80.8). That is 1–2.5 F1 above DeepSeek alone at 7–10 times its request time. The pipeline already has DeepSeek's proposal for those units, so this needs no extra requests.

**The second corrector.** Neither candidate is equal to DeepSeek:

- `qwen3.6-35b-a3b`: 58.7–63.1, at 60–115% of DeepSeek's request time, with 11% failed units. Not worth using.
- `gemma-4-31b-it`: 71.3–73.0, at 20–43% of DeepSeek's request time, but with 11–20% failed units. It is the better candidate, but it was not tested in the pipeline.

Other Kisski models were probed only on the first 8 units of 10.5771, so this is a coarse ranking:

| Model | Failures % | Edit P | Edit R | Edit F1 |
| --- | --- | --- | --- | --- |
| `glm-5.3` | 0 | 79.6 | 93.7 | 86.0 |
| `deepseek-v4.1-flash-1017` | 0 | 77.9 | 93.7 | 85.1 |
| `glm-5.3-flash` | 0 | 68.6 | 91.1 | 78.3 |
| `qwen3.6-35b-a3b` | 0 | 66.0 | 83.5 | 73.7 |
| `gemma-4-31b-it` | 25.0 | 62.2 | 64.6 | 63.4 |
| `qwen3.5-397b-a17b` | 25.0 | 69.2 | 57.0 | 62.5 |
| `deepseek-v4-flash-0731` | 25.0 | 54.9 | 57.0 | 55.9 |

Over the full documents, `qwen3.6` fell to 60.6 from 73.7 and `gemma-4` rose to 71.6 from 63.4, so 8 units can be wrong by 10 points or more in either direction.

All runs started only while Kisski reported `glm-5.3` as free, and it failed all the same, so the availability status does not predict its reliability. Rate limits, data protection and capacity at scale were not assessed.

## Models tested and rejected

Models that need not be tested again for this task unless the model, the prompt or the rules change. The MPCDF runs used dedicated vLLM endpoints of the [MPCDF LLM plugin](../../../mpcdf-llm/README.md) (one or two MI300A per model on Viper), so their results are free of the timeouts and connection errors of a shared service. They were probed on clr (`ba7zek`, input `qbb9sh`), first 16 aligned units, `units:8`, one run:

```bash
UV_NO_SYNC=1 uv run python -m pte_plugin_auto_annotation.probe \
  --model mpcdf-llm-<preset> \
  --reference ba7zek --input qbb9sh \
  --batches units:8 --modes all --repeats 1 --concurrency 2 --max-units 16 \
  --out $OUT/mpcdf-smoke
```

| Model | Where | Edit F1 | Failures % | Reason |
| --- | --- | --- | --- | --- |
| `gemma-4-31b-it` | Kisski, full documents | 71.3–73.0 | 11–20 | Below 75 F1, and too many failed units for a second opinion |
| `google/gemma-4-31B-it` (BF16) | MPCDF, 16 units | 73.8 | 18.8 | Same score and failure rate as on Kisski. All 3 failed units changed the text, so the failures are the model's own, not the service's |
| `openai/gpt-oss-120b` | MPCDF, 16 units | 31.7 | 25.0 | Edit recall 22.0: it leaves most needed changes undone; 4 units changed the text |
| `Qwen/Qwen3.5-122B-A10B-FP8`, reasoning off | MPCDF, 16 units | 58.4 | 25.0 | Weak, and slow: 385 s for the two batches of 8, against 113 s for Gemma 4 31B. All 4 failed units changed the text |
| `qwen3.6-35b-a3b` | Kisski, full documents | 58.7–63.1 | 11 | Weak, and 60–115% of DeepSeek's request time |
| `gemini-3.5-flash-lite` | Gemini, full documents | 57.1–70.0 | – | Weak as a corrector; the arbiter rarely chose it |
| `glm-5.3` as arbiter | Kisski | – | – | Failed 8 of 64 and 48 of 86 units with timeouts and connection errors (see above); usable only with the fallback to a corrector |

Not tested:

- `google/gemma-4-26B-A4B-it`: the mixture-of-experts sibling of Gemma 4 31B (about 4B active parameters), expected to be weaker than the 31B model and not faster in a way that matters.
- `deepseek-ai/DeepSeek-V4-Flash-0731` on MPCDF (two MI300A): not running yet. vLLM 0.30 accepts its FP4 experts, but the first start was cancelled by the then fixed build timeout of 30 min, 25% into loading (23 min download of 155 GiB, about 19 min load). The second start, with the weights cached, ended on MPCDF's side after 23 min, about when loading should have finished. The job log (12962533) shows why: vLLM's sparse attention for this model on ROCm needs AITER or a gfx950/gfx11 GPU, and the MI300A is gfx942. Not pursued, since DeepSeek is available on Kisski.
- `glm-5.3` and `deepseek-v4.1-flash-1017` on MPCDF: their weights (about 750 GB and 510 GB) do not fit into two MI300A (2 × 128 GB). They stay on Kisski only.

## Recommendation

- Use `deepseek-v4.1-flash-1017` alone as the default for first annotations. It is the baseline any pipeline has to beat.
- Do not use a frontier arbiter by default.
- Do not use Gemini or `qwen3.6` as second correctors.
- Do not use `glm-5.3` as an arbiter until unresolved units fall back to a corrector's proposal and its failures are understood.
- If higher quality justifies the wait, the arbiter with DeepSeek as fallback is worth testing on more documents. It is the only setup here with a repeated gain.
- Check the rule source of older extractions before annotating them.

## Using model performance to route the work

A probe, run on demand and cached, could choose the route for each model instead of a fixed pipeline. The thresholds below are proposals to be calibrated, since two documents can't set them.

**Cached result.** Per model, provider, rule-text hash, prompt version, variant and batching: edit F1, precision, failure rate and seconds per unit. The result stays valid until one of those keys changes, or after a period such as 30 days. Busy state and rate limits are not cached with it: they are read at the start of each run, as `annotate` already does for busy models, and from the rate-limit state during the run.

**Probe size.** At least about 24 units, from the start, middle and end of a document, because 8 units misjudged two of the models by 10 points or more. For a new document without a gold version, the cached results of the same variant apply.

**Routes.**

| Probe result | Route |
| --- | --- |
| F1 at least about 80, failures below 5% | Corrects alone. One pass, with the disagreement step left out. |
| F1 about 65–80, at most half of the strong model's time per unit | Second opinion. Disagreement goes to the arbiter or to review. |
| F1 about 65–80 and slower than that | Not used. Waiting for a weak model buys nothing. |
| F1 below about 65, or failures above 20% | Not used. |

**Rules for a run.**

- A weak model never runs without a stronger corrector or arbiter: it can only be used for a second opinion.
- An arbiter counts as usable only if its failure rate and time are acceptable in the probe, on batches of its real size.
- Unresolved units always fall back to the stronger corrector's proposal and are marked for review.
- A time budget per document stops arbitration and applies the fallback when it runs out, which keeps a slow arbiter from costing hours.
- Before starting, `annotate` chooses the strongest models that are free. If only weak ones are free, it should say so and wait rather than silently produce a weaker result.

On the present numbers, DeepSeek is the only Kisski model that would correct alone, and `gemma-4` the only Kisski candidate for a second opinion. Sonnet alone also clears the threshold on clr and is about 5–11 times faster than DeepSeek, at the price of a paid frontier model.

## Next steps

- Measure the fallback to model B and the per-model time budget, added after this report, on these two documents. The failed batches were read timeouts: with retries, 5 of 6 clr batches succeeded, but one took up to 1,615 s, so retries need the budget.
- Test a pipeline with `gemma-4-31b-it` as second corrector and a reliable arbiter against DeepSeek alone on these two documents.
- Check whether turning off reasoning makes `gemma-4` more accurate. It stayed below the 75 F1 set as a condition for this work, so this has low priority.
- Add the per-model routing and cached probe once the fallback exists.
- Repeat on more documents, and once fine-tuned models are available.
