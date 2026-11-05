# Batch-size probe: Gemini Flash-Lite on one gold document (2026-11-05)

First run of the model probe on a complete gold document: does the quality of a model's corrections change with the number of review units sent in one request?

## Setup

```bash
python -m pte_plugin_auto_annotation.probe \
  --model google/gemini-flash-lite-latest \
  --doc 10.5771__2699-1284-2024-3-149 \
  --batches units:1,units:4,units:8,units:16,units:32 --modes all --repeats 3 --retries 6
```

- Model: `gemini-flash-lite-latest` through the editor's Google provider, temperature 0. The alias is not resolved in the output; AI Studio's usage figures suggest it pointed to Gemini 3.5 Flash Lite (free tier: 15 requests/min, 250K input tokens/min, 500 requests/day).
- Document: `grobid.training.references` of `10.5771__2699-1284-2024-3-149`, reference = the gold version `fz3uhg`, input = the extraction `bfn4g7` it was derived from.
- Only the `all` output mode was run, to stay within the daily request limit.

## Report


### google/gemini-flash-lite-latest on 10.5771__2699-1284-2024-3-149

Reference version `fz3uhg` (gold), input version `bfn4g7` (the version the reference was derived from), variant `grobid.training.references`, unit path `.//listBibl/bibl`, rules `a0fe8452cd45` (primary, data-correction, schema-fragment), prompt v1. 52 units aligned, 18 input and 43 reference units unmatched.

| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| - | - | 1 | 0 | 0 | 0.0 | 0.0 | 1.9 | – | 0.0 | – | 36.4 | – | 0.0 | 0 | 0 |
| units:1 | all | 3 | 156 | 69 | 0.6 | 3.2 | 15.5 | 75.5 | 61.1 | 67.5 | 65.0 | 61.1 / – / – / – | 1574.1 | 5413164 | 54707 |
| units:4 | all | 3 | 39 | 24 | 0.0 | 4.5 | 7.7 | 66.6 | 56.9 | 61.4 | 57.2 | 41.4 / 59.6 / 60.7 / 65.0 | 295.4 | 1388247 | 56644 |
| units:8 | all | 3 | 21 | 24 | 0.0 | 7.1 | 5.1 | 69.0 | 60.8 | 64.7 | 60.6 | 41.4 / 57.3 / 76.1 / 66.3 | 138.6 | 769029 | 57199 |
| units:16 | all | 3 | 12 | 16 | 10.3 | 5.7 | 2.9 | 71.8 | 65.1 | 68.2 | 64.0 | 64.1 / 62.7 / 63.1 / 72.1 | 103.2 | 459483 | 52978 |
| units:32 | all | 3 | 6 | 7 | 0.0 | 9.6 | 5.8 | 74.1 | 71.4 | 72.7 | 68.4 | 74.0 / 88.5 / 56.4 / 65.4 | 80.7 | 253116 | 59109 |

Edit P/R: precision and recall of the changes made to the input, against the changes the reference version needs. Retries: requests repeated after a provider overload or rate limit. Errors: units whose request still failed; they say nothing about the model and are left out of all other columns. Failures: units the model answered without a usable proposal (missing, malformed, other outermost element, text changed); they are scored as unchanged input. The baseline row scores the input itself.

### Edit F1 per repetition

| Batching | Rep. 1 | Rep. 2 | Rep. 3 |
| --- | --- | --- | --- |
| units:1 | 64.6 | 68.0 | 69.8 |
| units:4 | 62.5 | 59.9 | 61.8 |
| units:8 | 60.7 | 67.0 | 66.3 |
| units:16 | 73.7 | 64.4 | 67.7 |
| units:32 | 71.4 | 73.9 | 73.0 |

Failure statuses (units over the three repetitions):

| Batching | ok | text_changed | malformed | wrong_root | error |
| --- | --- | --- | --- | --- | --- |
| units:1 | 150 | 3 | 1 | 1 | 1 |
| units:4 | 149 | 7 | 0 | 0 | 0 |
| units:8 | 145 | 10 | 1 | 0 | 0 |
| units:16 | 132 | 8 | 0 | 0 | 16 |
| units:32 | 141 | 15 | 0 | 0 | 0 |

## Observations

- Edit quality does not fall with batch size up to 32 units. units:32 has the highest edit F1 (72.7) and the smallest spread between repetitions (71.4–73.9). The spread at units:1 is about 5 points, so the differences between 1, 4, 8 and 16 units are within the noise. Edit recall by position in the batch shows no consistent decline towards the end of an answer.
- Structural failures rise with batch size, from 3.2% (units:1) to 9.6% (units:32), almost all of them changed text. The text check catches them; in the pipeline, split-and-retry would resend those batches in smaller parts. Whether some are typo corrections allowed by the data-correction rules has not been checked.
- Cost falls steeply: units:1 sends 21 times the prompt text of units:32 (5.4M vs. 0.25M characters; the rules, about 35K characters, are repeated in every request) and takes about 20 times as long. Answer length is about the same.
- Provider overloads and rate limits (HTTP 503/429) caused 140 retries; 2 requests still failed (one of them a 16-unit batch), and their units are left out of the scores. With retries, the run used about 374 requests of the daily limit of 500.
- Unit alignment: 52 of the extraction's 70 units match a gold unit by text; the gold version has 95 units, so annotators split or merged many references, and those units cannot be scored.

## Limits

One document, one model, one output mode. Exact unit matches are rare (3–16%), so the edit and field scores carry the result. Before choosing batch sizes for the pipeline, the probe should be run on more gold documents, in the `changed` mode, and with batch sizes above 32 to find where quality does break.
