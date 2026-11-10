# MPCDF LLM: preset export and import

Request (Christian, 2026-11-10): add export and import of presets to the mpcdf-llm plugin. When presets already exist, the import asks whether to replace them or to merge, which adds only new ones (no duplicates).

## Implementation

The management page reads presets with the library's `GET /admin/presets` and deletes them with `DELETE /admin/presets/{name}`. Imported presets are saved through a plugin route, `POST /api/plugins/mpcdf-llm/presets/import`, which validates each preset with the library's `Preset` model and stores it with `manager.presets.put()`.

The first version saved through the library's `PUT /admin/presets/{name}`, which checks a preset against the live options of the service and so needs an MPCDF login. Christian pointed out that presets are local, so the import should work without a session. The plugin route skips the live check, so an imported preset with a machine or GPU type the service does not offer fails only when it is started.

- `manage-helpers.js`: `exportPresets()` (file content), `parsePresetsImport()` (accepts the export format or a bare array; rejects missing or duplicate names), `planImport()` (which presets to save, delete or skip).
- `manage.html`: Export and Import buttons below the presets table, a `<dialog>` with Replace / Merge / Cancel that appears only when presets exist, and a result message.

Decisions:

- Duplicates are judged by preset name, since the name is the preset's key. Merge skips imported presets whose name exists, even when their settings differ.
- Replace saves first and deletes the leftover presets only when all imported presets were saved, so a rejected file never leaves the installation with fewer presets than before.
- Import needs no MPCDF session (see above). Creating, editing and duplicating in the form still needs one, because the form's choices come from the service.
- **Duplicate** (added on request) opens the form with a copy of the preset under a free name (`<name>-copy`, `<name>-copy-2`, ...). The name stays editable; saving a new or duplicated preset under an existing name is refused, so it never overwrites a preset by accident.

## Tests

- Unit tests for the helpers in `tests/manage.test.js`.
- `test_routes.py` tests the import route without a session (valid, invalid and nameless presets) and its admin check; `tests/api/mpcdf-llm.test.js` imports and deletes a preset against the real library without a session. Both run only in CI.
- A Playwright check of the page against a mocked admin API (export download, merge, replace, rejected preset, cancel); not committed.
- The Python and API tests need the `mpcdf-llm` library from gitlab.mpcdf.mpg.de, which the cloud session cannot reach, so they run only in CI.
