# Correct annotations in new version

Date: 2026-11-11

## Goal

Experiments showed that the two-model agreement plus arbiter pipeline does not correct annotations better than a single model (DeepSeek, an open model, came close to Sonnet). The auto-annotation plugin therefore gets one editor action for a single model, "Correct annotations in new version" (Tools menu, section "Annotation"): it corrects the open document with the model selected in Tools > Inference and saves the result as a new version. The pipeline and the probe CLIs stay as they are.

## Design

- **Client-driven, one batch per request.** As in "Review document annotations", the browser sends the batches one after the other (`/correct/batch`) and shows "n of m units, k changed" in the progress widget, which can be cancelled. A server-side background job with SSE progress was rejected: production runs four workers, and SSE queues and in-memory job state live in one worker only.
- **Read-only source.** `/correct/start` releases the user's own lock and acquires it under `auto-annotation:<job id>`. The document then opens read-only everywhere (`services.load()` gets 423), also in the user's tab. The client dispatches `editorReadOnly: true` before the start, so the editor's heartbeat does not report a lost lock. The lock is refreshed by every request and by `/correct/heartbeat` every 30 s; it expires after 90 s like any lock if the tab is closed. Once the new version is saved, the editor freezes the source (a version was derived from it), so the lock is only needed during the run. No core change was needed.
- **Saving.** `/correct/finish` checks the corrections the client sends back (well-formed, same element, text unchanged; for segmenting variants the whole text) and returns the document with a `revisionDesc/change` by `#auto-annotation`. The client saves it with the editor's save API (`new_version`), then releases the job lock.
- **Machine-readable model.** The change's `desc` names the model as `<name type="model" subtype="<provider id>" key="<provider id>/<model id>">label</name>`, plus `measure type="corrected-units"` counts and the duration as `time/@dur`.
- **Loading the result.** If the source is still open, the new version is loaded; otherwise a dialog asks. The new version's lock, acquired by the save, is released first in that case.
- **Stops.** No changes: no version is created. Cancelled or failed request: the user may save the corrections so far. Otherwise the job lock is released and the source reloaded, which makes it editable again.

## Files

- `single.py`: batch correction with one model, checking returned corrections
- `correction_routes.py`: `/api/plugins/auto-annotation/correct/{start,batch,heartbeat,finish,release}`, included by `routes.router`
- `document.py`: `write_corrections()`, shared `_record_change()`
- `extensions/auto-annotation.js`: the menu action, `runCorrection()` loop

## Open points

- A user returning to the source during a run sees the editor's general notice "This document is being edited by another user". A specific message would need the core's lock API to say who holds the lock.
