# Testing a model with the current document

Before you let a language model correct many documents, you can measure how well it corrects one whose annotation you know is right. The test lets the model correct an earlier extraction of the open document and compares its corrections with the open version. Reviewers and administrators find the entries in the **Inference** section of the **Tools** menu.

## Starting a test

1. Open the version to compare with, ideally the gold version of a document. If it is not a gold version, a warning asks for confirmation, since the scores are then measured against annotations that may contain errors.
2. Choose **Tools** > **Test model with current document**. The entry is disabled if the document links to no machine-readable rules or no model is available.
3. Fill in the form and click **Start test**.

![The form "Test model with current document": the model KISSKI/llama-3.3-70b-instruct, the batch sizes 1, 4, 8, 16 and 32 checked with a field for more sizes, the output mode "all units" checked and "changed units only" unchecked, the fields Repetitions, In parallel, Retries, Temperature, "Only the first units" and "Version to correct" set to Automatic, the option to start even if the model is busy, and the line "5 run(s); each run sends all units of the document to the model once"](images/model-test-form.png)

*The settings of a model test.*

| Field | Meaning |
| --- | --- |
| Model | The model to test. Preselected: the model selected under **Tools** > **Inference** |
| Batch sizes | How many units (for segmentation documents: pages) are sent in one request. Every checked size is tested |
| Output modes | Whether the model sends back all units or only the units it changed. Every checked mode is tested |
| Repetitions | How often each combination is run. Models do not answer the same way every time |
| In parallel | How many requests are sent at the same time |
| Retries | How often a request is repeated when the model is overloaded |
| Temperature | How freely the model answers; lower values give more predictable answers |
| Only the first units | Sends only the first units of the document, for a quick test |
| Version to correct | **Automatic** takes the extraction the open version was derived from, else the earliest extraction |
| Start even if the model is reported as busy | Most requests to a busy model time out, so a test normally does not start then |

The form shows how many runs the test makes: batch sizes × output modes × repetitions, each sending the whole document once. A message confirms the start, and a minimized progress bar shows the runs done; its cancel button stops the test after the request under way. You can run one test at a time.

## Reading the results

When the test is done, its results open in a dialog. **Model test results** in the same menu lists all tests, which you can filter by model. All reviewers see all tests; the user who started a test and administrators can delete it.

The page of a test shows:

- its settings and the scores of every batch size and output mode. The first row is the version the model corrected, without the model's changes, so you see what the model added;
- the model's tests on other documents, and the tests of other models on the same document. Tests with another prompt, other rules or a reference that is not a gold version are marked, since their scores are not directly comparable;
- a section that explains how the scores are computed, and the result files for download.

The most telling scores are **edit precision** (how many of the model's changes were right) and **edit recall** (how many of the needed changes the model made). A good model for automatic correction has a high precision, so that it does not add errors that someone has to find later.
