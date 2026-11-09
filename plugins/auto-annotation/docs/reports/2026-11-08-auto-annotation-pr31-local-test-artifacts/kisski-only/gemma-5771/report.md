# Model probe report

## kisski/gemma-4-31b-it on 10.5771__2699-1284-2024-3-149

Reference version `38cs7r` (gold), input version `gczy7r` (given), variant `grobid.training.references`, unit path `.//listBibl/bibl`, rules `33154192a7d2` (primary, data-correction, schema-fragment), prompt v1. 70 units aligned, 16 input and 25 reference units unmatched.

| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| - | - | 1 | 0 | 0 | 0.0 | 0.0 | 2.9 | – | 0.0 | – | 40.9 | – | 0.0 | 0 | 0 |
| units:8 | all | 2 | 18 | 0 | 0.0 | 11.4 | 5.7 | 71.2 | 72.0 | 71.6 | 69.7 | 78.4 / 75.6 / 70.5 / 62.8 | 458.1 | 662688 | 60295 |

Edit P/R: precision and recall of the changes made to the input, against the changes the reference version needs. Retries: requests repeated after a provider overload or rate limit. Errors: units whose request still failed; they say nothing about the model and are left out of all other columns. Failures: units the model answered without a usable proposal (missing, malformed, other outermost element, text changed); they are scored as unchanged input. The baseline row scores the input itself.
