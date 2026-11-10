# Model probe report

## kisski/qwen3.6-35b-a3b on 10.5771__2699-1284-2024-3-149

Reference version `38cs7r` (gold), input version `gczy7r` (given), variant `grobid.training.references`, unit path `.//listBibl/bibl`, rules `33154192a7d2` (primary, data-correction, schema-fragment), prompt v1. 70 units aligned, 16 input and 25 reference units unmatched.

| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| - | - | 1 | 0 | 0 | 0.0 | 0.0 | 2.9 | – | 0.0 | – | 40.9 | – | 0.0 | 0 | 0 |
| units:8 | all | 2 | 18 | 0 | 0.0 | 10.7 | 5.0 | 72.7 | 51.9 | 60.6 | 62.5 | 68.1 / 44.9 / 38.1 / 55.1 | 1219.0 | 662688 | 55579 |

Edit P/R: precision and recall of the changes made to the input, against the changes the reference version needs. Retries: requests repeated after a provider overload or rate limit. Errors: units whose request still failed; they say nothing about the model and are left out of all other columns. Failures: units the model answered without a usable proposal (missing, malformed, other outermost element, text changed); they are scored as unchanged input. The baseline row scores the input itself.
