# Test model with current document: a UI for the model probe

Requested 2026-11-13. The model probe of the auto-annotation plugin (`probe.py`) was a command line tool only, whose results stayed in local files. This adds a UI for it to the editor, and stores the results on the server, so that results for a model accumulate over documents.

## Requirements

- A menu item "Test model with current document" in the Tools menu, category `inference`, for reviewers and admins.
- If the open version is not a gold version: a warning and a confirmation.
- A form for the parameters of the probe.
- On start, a toast; a minimized progress widget follows the test. Errors are shown in an error alert, not a toast.
- The results open in a plugin result page, which can be opened in a new window and exported.
- The page shows the test's results and compares them with previous tests, which are saved to the plugin data, so that results for a model on different documents accumulate.
- One model per test in this first iteration; testing several models in one test can follow.

## Decisions

- **Server job, not browser-driven batches.** The "Correct annotations in new version" action sends its batches from the browser because it holds a document lock. A probe runs much longer (all batch sizes × modes × repetitions) and needs no lock, so it runs as an asyncio task of the worker that receives the start request, reusing `evaluation.evaluate()` with a `ProviderChatClient`. It survives closing the tab, and a reloaded tab follows it again. Christian chose this on 2026-11-13.
- **State in files.** The production server runs several workers. The job writes `status.json` (with a heartbeat) and the result files after every run to `data/plugins/auto-annotation/probe-runs/<run id>/`; all other requests (status, cancel, pages) read these files, so any worker can answer. Cancelling writes a `cancel` file that the job checks every 2 seconds. A running test without a heartbeat for two minutes is reported as interrupted.
- **Stopping early.** A run whose requests all failed stops the test ("failed", with the provider's error), since the following runs would fail the same way.
- **Menu items hidden** from users without the reviewer or admin role (default of the plan, not objected to).
- **Pooled comparison.** Scores of a model over documents are pooled over the units of the latest finished test per reference version with the same prompt version, recomputed from the stored `units.csv`, so that documents count by their number of units.
- **Older editor versions.** The progress widget starts minimized through a new `minimized` option of the editor's `progress.show()`. Older editor versions ignore it and show the widget maximized until the editor is updated (Christian, 2026-11-13).

## Implementation

- `probe_store.py`, `probe_job.py`, `probe_compare.py`, `probe_views.py`, `probe_routes.py` (routes under `/api/plugins/auto-annotation/probe/`), the plugin endpoint `probe_results` (not in the menu) for the result dialog, and `extensions/auto-annotation-model-test.js`.
- `evaluation.align_source()` factored out of `evaluate()`, so that the start route can count the aligned units before starting the job.
- Editor: `progress.show(id, {minimized})` (separate pull request in pdf-tei-editor).

See the plugin README, section "Test model with current document".
