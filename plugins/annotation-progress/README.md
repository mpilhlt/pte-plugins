# Annotation Progress Plugin

Backend plugin that provides an overview of annotation progress for documents in a collection.

## Features

- Displays all documents in the current collection
- Hides or shows the documents of a lifecycle stage by clicking its badge in the summary
- Shows annotation labels with revision counts for each document
- Groups annotations by document ID
- Clickable annotation labels to open documents
- Includes documents without annotations
- Filters by variant when specified

## User manual

The page [Following the annotation progress](src/pte_plugin_annotation_progress/docs/annotation-progress.md) in the Review Workflow of the editor's user manual (`manual` in `pte-plugin.json`) describes opening the view, the columns, the version histories, the stage filter and the share link. The screenshot in `docs/images/` was taken with the demo data of the editor (`docker/demo-data`).

## Output

The view is a DataTables page with one row per version history of each document, sorted by document label. A version history is a group of current versions that were copied from the same version, as in the app's "Target XML" and "Compare with version" menus; each gold standard and each version without a parent forms a history of its own. **Last Change**, **Last Annotator**, **Status** and **Date** come from the last `<change>` of the most recently edited current version of the history. Sorting is disabled so that the rows of a document stay together.

The lifecycle stage of a document in the summary is the most advanced status of its versions in `annotation.lifecycle.order`, or "no status" if the document has no annotations or none of its versions has a status from that list.

**Share link** copies a link such as `https://host/#plugin=annotation-progress/show_progress&collection=batch-1&variant=grobid`. This relies on the endpoint's `url_openable` flag, which requires a PDF-TEI-Editor version that supports opening backend plugin endpoints from the URL hash.

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
