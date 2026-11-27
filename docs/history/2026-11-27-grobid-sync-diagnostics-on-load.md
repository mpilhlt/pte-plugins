# GROBID sync diagnostics after loading a document

Christian reported (2026-11-27) that the diagnostics flagging differences between the TEI and the GROBID feature file did not appear when the app was reloaded with an out-of-sync document; they came back after the next edit.

## Cause

The `grobid-sync` lint source runs when the document is loaded and when the feature tokens have been fetched. CodeMirror keeps one diagnostic set, and `setDiagnostics()` replaces it as a whole. After a document is loaded, the XML editor's well-formedness check (`editorXmlWellFormed` in `app/src/plugins/xmleditor.js`) dispatches `setDiagnostics([])`, which removed the GROBID diagnostics. The linter only runs again on the next document change, so nothing restored them until the user edited the document. In addition, the diagnostics had no `source`, so `tei-validation` took them for its own after an edit.

## Fix

- The diagnostics carry the source `grobid-sync`.
- An update listener merges them in again (`applyMergedDiagnostics()` from `lint-utils`, outside the update) when a replacement of the diagnostic set leaves none of them, the pattern `unit-review` uses. A merge that changes nothing is not dispatched, so there is no loop with other sources doing the same.
- The last result is cached by CodeMirror document and feature tokens, so replacements without an edit do not diff the tokens again.
- Diagnostics dismissed in the diagnostics drawer are not merged in again until the next edit.

Tests: `plugins/grobid/src/pte_plugin_grobid/tests/grobid-sync-remerge.test.js`.
