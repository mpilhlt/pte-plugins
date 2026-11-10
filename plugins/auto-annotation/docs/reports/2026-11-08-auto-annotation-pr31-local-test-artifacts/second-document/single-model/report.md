# Model probe report

## anthropic/claude-sonnet-5-5 on 10.12775__clr.2013.008

Reference version `ba7zek` (gold), input version `qbb9sh` (given), variant `grobid.training.references`, unit path `.//listBibl/bibl`, rules `c8be2ca15650` (primary, data-correction, schema-fragment), prompt v1. 56 units aligned, 8 input and 12 reference units unmatched.

| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| - | - | 1 | 0 | 0 | 0.0 | 0.0 | 0.0 | – | 0.0 | – | 42.8 | – | 0.0 | 0 | 0 |
| units:8 | all | 2 | 14 | 0 | 0.0 | 0.9 | 33.9 | 87.0 | 79.2 | 82.9 | 82.8 | 76.6 / 79.1 / 78.7 / 81.8 | 139.9 | 526502 | 45553 |

## kisski/deepseek-v4.1-flash-1017 on 10.12775__clr.2013.008

Reference version `ba7zek` (gold), input version `qbb9sh` (given), variant `grobid.training.references`, unit path `.//listBibl/bibl`, rules `c8be2ca15650` (primary, data-correction, schema-fragment), prompt v1. 56 units aligned, 8 input and 12 reference units unmatched.

| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| - | - | 1 | 0 | 0 | 0.0 | 0.0 | 0.0 | – | 0.0 | – | 42.8 | – | 0.0 | 0 | 0 |
| units:8 | all | 2 | 14 | 1 | 0.0 | 0.9 | 33.0 | 81.9 | 86.8 | 84.3 | 83.4 | 87.4 / 89.1 / 86.1 / 84.9 | 1592.8 | 526502 | 47870 |

## google/gemini-3.5-flash-lite on 10.12775__clr.2013.008

Reference version `ba7zek` (gold), input version `qbb9sh` (given), variant `grobid.training.references`, unit path `.//listBibl/bibl`, rules `c8be2ca15650` (primary, data-correction, schema-fragment), prompt v1. 56 units aligned, 8 input and 12 reference units unmatched.

| Batching | Mode | Runs | Calls | Retries | Errors % | Failures % | Exact % | Edit P | Edit R | Edit F1 | Field F1 | Edit R by quarter | Seconds | Prompt chars | Answer chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| - | - | 1 | 0 | 0 | 0.0 | 0.0 | 0.0 | – | 0.0 | – | 42.8 | – | 0.0 | 0 | 0 |
| units:8 | all | 2 | 14 | 4 | 0.0 | 6.2 | 16.1 | 71.7 | 64.8 | 68.0 | 68.6 | 62.1 / 57.7 / 72.8 / 66.7 | 87.4 | 526502 | 46835 |

Edit P/R: precision and recall of the changes made to the input, against the changes the reference version needs. Retries: requests repeated after a provider overload or rate limit. Errors: units whose request still failed; they say nothing about the model and are left out of all other columns. Failures: units the model answered without a usable proposal (missing, malformed, other outermost element, text changed); they are scored as unchanged input. The baseline row scores the input itself.
