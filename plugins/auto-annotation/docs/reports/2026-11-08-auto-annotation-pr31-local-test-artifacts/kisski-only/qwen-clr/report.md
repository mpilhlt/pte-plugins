# Model probe report

## kisski/qwen3.6-35b-a3b on 10.12775__clr.2013.008

Reference version `ba7zek` (gold), input version `qbb9sh` (given), variant `grobid.training.references`, unit path `.//listBibl/bibl`, rules `c8be2ca15650` (primary, data-correction, schema-fragment), prompt v1. 56 units aligned, 8 input and 12 reference units unmatched.

| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| - | - | 1 | 0 | 0 | 0.0 | 0.0 | 0.0 | – | 0.0 | – | 42.8 | – | 0.0 | 0 | 0 |
| units:8 | all | 2 | 14 | 0 | 0.0 | 10.7 | 11.6 | 73.2 | 54.7 | 62.6 | 65.0 | 44.4 / 59.1 / 63.9 / 52.3 | 958.9 | 526502 | 45311 |

Edit P/R: precision and recall of the changes made to the input, against the changes the reference version needs. Retries: requests repeated after a provider overload or rate limit. Errors: units whose request still failed; they say nothing about the model and are left out of all other columns. Failures: units the model answered without a usable proposal (missing, malformed, other outermost element, text changed); they are scored as unchanged input. The baseline row scores the input itself.
