# Following the annotation progress

The annotation progress view lists all documents of a collection with the versions of their annotation, so that you can see which documents still need work and who worked on them last.

## Opening the view

1. Choose the collection in **Collection** and, if you want to see one variant only, the variant in **Model Variant** in the toolbar.
2. Choose **Show Annotation Progress** in the **Collection** section of the plugins menu (plug icon).

If the dashboard shows the box **Annotation progress**, you can also click one of its progress rings: each ring stands for one variant of a collection and shows the share of documents that are done, and clicking it opens the view for that collection and variant. From which status on a document counts as done is configured by the administrator, by default from "approved" on.

## Reading the table

![The Annotation Progress view of the collection "default" and the variant grobid.training.segmentation: the summary with 2 documents, the badges "1 draft" and "1 no status", and the table with the columns Document, Document ID, Version History, Last Change, Last Annotator, Status and Date](images/progress-view.png)

*The progress of a collection with two documents.*

The summary at the top counts the documents and shows how many are at each stage of their annotation, by the most advanced status of their versions.

The table has one row for each version history of a document. A version history is a gold version or a version without a parent, together with the versions copied from it, as in the **Target XML** menu. The gold version is marked with a medal.

- **Document** and **Document ID**: the document, shown in its first row only. Click the ID to open the PDF.
- **Version History**: the current versions of the history. Click a label to open the version. **(see history)** after the labels shows the earlier versions of the history as well.
- **Last Change**, **Last Annotator**, **Status**, **Date**: the last change of the most recently edited version of the history. The dots before the status show how far it is in the order of statuses.

Documents that have no annotation yet show "No annotations". The search box finds a document with all its rows if any of them matches.

## Filtering by stage

Each badge in the summary, such as **1 draft** or **1 no status**, is a switch: click it to hide the documents of this stage from the table, and click it again to show them. The filter works together with the search. For example, hiding "no status" lists only the documents that have been annotated.

## Sharing the view

**Share link** copies a link that opens this view for the same collection and variant. Send it to colleagues: after logging in, they see the view directly.

**Export** downloads the table as a CSV file, and **Open in new window** shows it in a window of its own.
