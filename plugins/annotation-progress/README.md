# Annotation Progress Plugin

Backend plugin that provides an overview of annotation progress for documents in a collection.

## Features

- Displays all documents in the current collection
- Shows annotation labels with revision counts for each document
- Groups annotations by document ID
- Clickable annotation labels to open documents
- Includes documents without annotations
- Filters by variant when specified

## Usage

1. Select a collection in the application
2. Open the "Backend Plugins" menu
3. Select "Show Annotation Progress"

## Output

The plugin generates a DataTables page with one row per version history of each document, sorted by document label:

- **Document**: the document label (as in the app's PDF menu, shortened with an ellipsis; the full label is shown on hover) and **Document ID**, which opens the PDF. They are shown in the first row of the document only, so that its further histories look indented
- **Version History**: a version history is a group of current versions that were copied from the same version, as in the app's "Target XML" and "Compare with version" menus. Each gold standard (🥇) and each version without a parent forms a history of its own. Clicking **(see history)** after the labels expands the superseded versions of the history below them; every label opens its version.
- **Last Change**, **Last Annotator**, **Status**, **Date**: from the last `<change>` of the most recently edited current version of the history

Documents without any annotations show "No annotations". Sorting is disabled so that the rows of a document stay together; the search shows a document with all its histories if any of their rows match.

### Share link

The **Share link** button above the summary copies a link to the app that opens the view for the same collection and variant, e.g. `https://host/#plugin=annotation-progress/show_progress&collection=batch-1&variant=grobid`. Users who are not logged in see the view after logging in. This relies on the endpoint's `url_openable` flag, which requires a PDF-TEI-Editor version that supports opening backend plugin endpoints from the URL hash.

## Configuration

| Config key | Environment variable | Default | Description |
| --- | --- | --- | --- |
| `plugin.annotation-progress.dashboard-threshold` | `PLUGIN_ANNOTATION_PROGRESS_DASHBOARD_THRESHOLD` | `approved` | Lifecycle status (from `annotation.lifecycle.order`) from which a document counts as done in the dashboard's progress box |

The environment variable sets the initial value when the key is not yet in `config.json`.

## Implementation

- **Plugin file**: [plugin.py](src/pte_plugin_annotation_progress/plugin.py)
- **Routes file**: [routes.py](src/pte_plugin_annotation_progress/routes.py)
- **Tests**: [tests/test_annotation_progress.py](src/pte_plugin_annotation_progress/tests/test_annotation_progress.py)

## Technical Details

- Category: `collection` (appears under Collection menu)
- Requires: User role or higher
- State parameters: `collection`, `variant`
- Uses DataTables for a searchable table display
- Extracts edition title from TEI `<editionStmt>` or falls back to main title
- Counts all `<change>` elements in `<revisionDesc>` for revision count

## Dashboard box

If the [dashboard](../dashboard/README.md) plugin is active, this plugin contributes an "Annotation progress" box. For each project the user can see, it lists the project's collections by name, each with one progress ring per variant: the share of the collection's documents whose TEI file of that variant has a status at or after `plugin.annotation-progress.dashboard-threshold` (default `approved`) in `annotation.lifecycle.order`. Hovering a ring shows the variant and the counts; clicking it opens the progress view for that collection and variant. Each collection is listed once, under the most specific project; collections that belong only to archived projects are omitted. The data comes from `GET /api/plugins/annotation-progress/dashboard-summary`; the box is rendered by [extensions/annotation-progress-dashboard.js](src/pte_plugin_annotation_progress/extensions/annotation-progress-dashboard.js).
