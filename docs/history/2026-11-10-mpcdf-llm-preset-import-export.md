# MPCDF LLM: preset export and import

Request (Christian, 2026-11-10): add export and import of presets to the mpcdf-llm plugin. When presets already exist, the import asks whether to replace them or to merge, which adds only new ones (no duplicates).

## Implementation

Frontend only, in the management page. It uses the library's existing admin routes (`GET /presets`, `PUT /presets/{name}`, `DELETE /presets/{name}`), so every imported preset goes through the same validation as a preset saved in the form. No backend route was added.

- `manage-helpers.js`: `exportPresets()` (file content), `parsePresetsImport()` (accepts the export format or a bare array; rejects missing or duplicate names), `planImport()` (which presets to save, delete or skip).
- `manage.html`: Export and Import buttons below the presets table, a `<dialog>` with Replace / Merge / Cancel that appears only when presets exist, and a result message.

Decisions:

- Duplicates are judged by preset name, since the name is the preset's key. Merge skips imported presets whose name exists, even when their settings differ.
- Replace saves first and deletes the leftover presets only when all imported presets were saved, so a rejected file never leaves the installation with fewer presets than before.
- Import requires an MPCDF login because the library validates presets against the live options of the service.

## Tests

- Unit tests for the helpers in `tests/manage.test.js`.
- A Playwright check of the page against a mocked admin API (export download, merge, replace, rejected preset, cancel); not committed.
- The Python and API tests need the `mpcdf-llm` library from gitlab.mpcdf.mpg.de, which the cloud session cannot reach; nothing on the Python side changed.
