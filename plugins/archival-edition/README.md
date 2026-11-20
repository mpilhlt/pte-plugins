# Archival Edition

A plugin for the [PDF-TEI-Editor](https://github.com/mpilhlt/pdf-tei-editor) for editing archival documents (letters, postcards, telegrams, conference programs, manuscripts of papers and talks, notes). It was developed in the separate repository `cboulanger/pte-edition-plugin` and is now part of [pte-plugins](../../README.md).

It contains:

- **an extractor** (`archival-edition`) for PDFs made of a filled-in [Document Description Form](src/pte_plugin_archival_edition/tests/fixtures/document-description-form.docx) followed by photographs of the document. The form is read into the TEI header. The photographs are transcribed page by page by Claude with the user's own API key, diplomatically, in all languages the model supports;
- **a schema**, [tei-archival.rng](docs/schema/tei-archival.rng): a lightweight RELAX NG schema written from scratch (no ODD), derived from the schemas of [fossil](https://github.com/mpilhlt/fossil) and extended for archival material (`msDesc`, `correspDesc`, `facsimile`, diplomatic transcription);
- **annotation guidelines**, [docs/guidelines.md](docs/guidelines.md), which also document how each form field maps to TEI.

The schema and guidelines are published at <https://mpilhlt.github.io/pte-plugins/archival-edition/>. Documents created before the move refer to the schema at `https://cboulanger.github.io/pte-edition-plugin/schema/tei-archival.rng` or `https://cboulanger.github.io/pte-plugins/archival-edition/schema/tei-archival.rng`; the plugin validates them with the bundled schema as well.

## Installation

Install the package into the editor's environment (see the [repository README](../../README.md#installing-plugins)); it also installs the `anthropic-llm` plugin it depends on:

```bash
uv pip install "pte-plugin-archival-edition @ git+https://github.com/mpilhlt/pte-plugins#subdirectory=plugins/archival-edition"
```

The package declares the editor versions and plugin API versions it works with in `pte-plugin.json`; an incompatible plugin is shown as "Incompatible" in the plugin manager and not loaded.

Configuration:

1. Each user supplies their own Anthropic API key, so the cost of the transcription is billed to the user and not to the maintainer of the editor. When a user starts an extraction without a stored key, the editor asks for it and stores it encrypted (it can also be managed under the user menu > API Keys). Administrators and scripts can store keys with `uv run python bin/user-secrets.py set anthropic --user <name>` in the editor's directory. The model is chosen in the extraction dialog; `ARCHIVAL_EDITION_MODEL` (or the config key `plugin.archival-edition.model`) sets the default.
2. Optional: set `ARCHIVAL_EDITION_AUTHORITY` (or the config key `plugin.archival-edition.authority`) to the project responsible for the edition; it goes into `publicationStmt/authority`.

After a restart, "Archival Edition" appears in the Plugin Manager, and "Archival edition (description form + transcription)" is offered in the extraction dialog.

Besides the editor's own dependencies, the plugin needs PyMuPDF, which the package declares.

## User manual

The plugin adds the page [Editing archival documents](src/pte_plugin_archival_edition/docs/archival-edition.md) to the Extraction Workflow of the editor's user manual (`manual` in `pte-plugin.json`), when `anthropic-llm` is active too. It describes preparing the PDF, the options of the extractor, the user's Anthropic key, the Autosearch fallback and the reading view. The screenshots in `docs/images/` were taken with [sample-letter.pdf](src/pte_plugin_archival_edition/tests/fixtures/sample-letter.pdf) and [examples/sample-letter.tei.xml](examples/sample-letter.tei.xml).

## How it works

```text
PDF ──► form pages (text layer) ──► form_parser.py ──► field values ──► header_builder.py ──► teiHeader
   └──► photo pages ──► transcriber.py (one model request per page, JPEG ≤ 2000 px,
                         with the form data and the end of the previous page as context)
                         ──► <pb/> + TEI blocks per page ──► text/body/div
```

| File | Purpose |
| --- | --- |
| [plugin.py](src/pte_plugin_archival_edition/plugin.py) | Registers the extractor and the document rules provider (schema URL, guidelines) |
| [extractor.py](src/pte_plugin_archival_edition/extractor.py) | Assembles the TEI document |
| [fields.py](src/pte_plugin_archival_edition/fields.py), [values.py](src/pte_plugin_archival_edition/values.py) | Form fields and their conventions (`[inferred]`, `?`, dates, names) |
| [form_parser.py](src/pte_plugin_archival_edition/form_parser.py) | Reads the form's tables from the text layer, or asks the model for scanned forms |
| [header_builder.py](src/pte_plugin_archival_edition/header_builder.py) | Maps form fields to the TEI header |
| [transcriber.py](src/pte_plugin_archival_edition/transcriber.py), [prompts/](src/pte_plugin_archival_edition/prompts) | Page-by-page transcription with one repair round for malformed XML |
| [extensions/pb-sync.js](src/pte_plugin_archival_edition/extensions/pb-sync.js) | Autosearch fallback: the photographs have no text layer, so the PDF viewer scrolls to the page of the nearest preceding `<pb/>` (after the form pages). Needs the `pdfSearch.fallback` extension point of the editor |
| [extensions/tei-html-view.js](src/pte_plugin_archival_edition/extensions/tei-html-view.js), [static/archival-edition-to-html.xsl](src/pte_plugin_archival_edition/static/archival-edition-to-html.xsl) | Reading view in the XML editor's XSL viewer ("Archival edition (pages)"): the transcription as HTML, one box per page delimited by `<pb/>` |
| [schema/](schema) | Composable RNG source of the schema |

[examples/sample-letter.tei.xml](examples/sample-letter.tei.xml) shows the output for the test PDF (with a canned transcription instead of a real model).

[schlote-letter-1906.pdf](src/pte_plugin_archival_edition/tests/fixtures/schlote-letter-1906.pdf) is an example of a real input PDF: the filled-in form followed by the scans of a handwritten letter from 1906 (German Kurrent script with Chinese numerals, CC0, from Wikimedia Commons; see the [fixtures README](src/pte_plugin_archival_edition/tests/fixtures/README.md)). Upload it to the editor to try the extractor.

## Development

The tests run with the tests of the other plugins (see the [repository README](../../README.md#development)). The commands below run from this directory, with `PTE_ROOT` pointing to the editor checkout whose environment the plugins are installed into.

After editing anything in `schema/`, regenerate the flattened schema (written to `docs/schema/` and the plugin's `static/schema/`):

```bash
"$PTE_ROOT/.venv/bin/python" scripts/build_schema.py
```

Regenerate the test PDF (needs LibreOffice and python-docx) and the example output:

```bash
uv run --project "$PTE_ROOT" --with python-docx python scripts/make_sample_pdf.py
"$PTE_ROOT/.venv/bin/python" scripts/make_example_tei.py
```

The schema and guidelines are published to GitHub Pages from the `docs/` directory on the `main` branch, by the repository's [pages workflow](../../.github/workflows/pages.yml).
