# Model probe report

## anthropic/claude-sonnet-5-5 on 10.5771__2699-1284-2024-3-149

Reference version `38cs7r` (not the gold version), input version `gczy7r` (given), variant `grobid.training.references`, unit path `.//listBibl/bibl`, rules `33154192a7d2` (primary, data-correction, schema-fragment), prompt v1. 70 units aligned, 16 input and 25 reference units unmatched.

| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| - | - | 1 | 0 | 0 | 0.0 | 0.0 | 2.9 | – | 0.0 | – | 40.9 | – | 0.0 | 0 | 0 |
| units:8 | all | 2 | 18 | 0 | 0.0 | 1.4 | 16.4 | 79.7 | 79.0 | 79.3 | 78.0 | 82.8 / 81.5 / 65.4 / 87.6 | 197.7 | 662688 | 56466 |

## kisski/deepseek-v4.1-flash-1017 on 10.5771__2699-1284-2024-3-149

Reference version `38cs7r` (not the gold version), input version `gczy7r` (given), variant `grobid.training.references`, unit path `.//listBibl/bibl`, rules `33154192a7d2` (primary, data-correction, schema-fragment), prompt v1. 70 units aligned, 16 input and 25 reference units unmatched.

| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| - | - | 1 | 0 | 0 | 0.0 | 0.0 | 2.9 | – | 0.0 | – | 40.9 | – | 0.0 | 0 | 0 |
| units:8 | all | 2 | 18 | 0 | 0.0 | 0.7 | 18.6 | 77.6 | 84.1 | 80.7 | 78.5 | 87.5 / 87.0 / 77.6 / 85.0 | 1064.9 | 662688 | 58230 |

## google/gemini-3.5-flash-lite on 10.5771__2699-1284-2024-3-149

Reference version `38cs7r` (not the gold version), input version `gczy7r` (given), variant `grobid.training.references`, unit path `.//listBibl/bibl`, rules `33154192a7d2` (primary, data-correction, schema-fragment), prompt v1. 70 units aligned, 16 input and 25 reference units unmatched.

| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| - | - | 1 | 0 | 0 | 0.0 | 0.0 | 2.9 | – | 0.0 | – | 40.9 | – | 0.0 | 0 | 0 |
| units:8 | all | 2 | 18 | 0 | 0.0 | 5.0 | 11.4 | 66.6 | 54.1 | 59.7 | 59.7 | 59.7 / 53.9 / 42.0 / 61.3 | 52.7 | 662688 | 53854 |

Edit P/R: precision and recall of the changes made to the input, against the changes the reference version needs. Retries: requests repeated after a provider overload or rate limit. Errors: units whose request still failed; they say nothing about the model and are left out of all other columns. Failures: units the model answered without a usable proposal (missing, malformed, other outermost element, text changed); they are scored as unchanged input. The baseline row scores the input itself.
