# Model probe report

## kisski/glm-5.3-flash on 10.5771__2699-1284-2024-3-149

Reference version `38cs7r` (gold), input version `bfn4g7` (the extraction the reference was derived from), variant `grobid.training.references`, unit path `.//listBibl/bibl`, rules `33154192a7d2` (primary, data-correction, schema-fragment), prompt v1. 8 units aligned, 18 input and 43 reference units unmatched.

| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| - | - | 1 | 0 | 0 | 0.0 | 0.0 | 0.0 | – | 0.0 | – | 43.2 | – | 0.0 | 0 | 0 |
| units:8 | all | 1 | 1 | 0 | 0.0 | 0.0 | 0.0 | 68.6 | 91.1 | 78.3 | 77.3 | 79.2 / 100.0 / 94.4 / 95.7 | 538.8 | 37734 | 4956 |

Edit P/R: precision and recall of the changes made to the input, against the changes the reference version needs. Retries: requests repeated after a provider overload or rate limit. Errors: units whose request still failed; they say nothing about the model and are left out of all other columns. Failures: units the model answered without a usable proposal (missing, malformed, other outermost element, text changed); they are scored as unchanged input. The baseline row scores the input itself.
