# Transfer annotations from another version (2026-11-18)

Implements mpilhlt/pdf-tei-editor#822 following the spec in the issue comment (https://github.com/mpilhlt/pdf-tei-editor/issues/822#issuecomment-6986627052), after the prerequisite #58 (GROBID cache keyed by the PDF hash).

## Decisions (Christian, 2026-11-18)

- Units with identical text are copied without the model.
- Unmatched units are kept as extracted.
- The result gets the status of the source's last revision change (the source had already been reviewed), not `in-review`.
- Doubts are recorded as not-OK marks under the LLM Reviewer's id `llm-<provider>-<model>`.
- Unit comments of the source are left behind.
- The menu acts on the open version as the target only.

## Implementation

- `alignment.py`: character alignment of the two `<text>` streams, words first (`SequenceMatcher` without autojunk), then characters inside unequal word runs; normalization by NFKC plus long s, dashes, quotes, soft hyphens and zero-width characters. 112k characters align in about 0.2 s.
- `transfer.py`: the plan per unit (`copied`, `model`, `unmatched`), prompts with the source's fragment (unit documents) or source lines prefixed `S` (zone documents), doubts (`DOUBT:` lines, `### Doubt n` blocks, `DOUBT <id>[-<id>]:` rows), batch runners with split-and-retry, checks of the results and the summary.
- `document.py`: `transfer_description_xml()` and `write_transfer()`; the source is recorded as `<name type="version" key="<stable id>">`, since the fossil schema allows only `time`, `measure` and `name` in `desc`.
- `correction_routes.py`: the correction job's routes take `source_stable_id`, `marks` and `mode`; new `GET correct/transfer-preview` for the dialog.
- `extensions/auto-annotation.js`: menu item "Transfer annotations from another version…", the dialog, the doubts pass and recording the marks through `unit-review.setMarks()`.

## Deviations from the spec

- The plan is not stored in `data/plugins/auto-annotation/transfer-jobs/`; it is deterministic and fast, so every request of the job computes it again.
- The preview route is `correct/transfer-preview`, next to the other job routes, instead of `transfer/preview`.

A unit copied by text alone could take over the source's whitespace inside words (`Tor ts` for `Torts`); copied units therefore also need an identical word sequence (`check_transferred()`).
