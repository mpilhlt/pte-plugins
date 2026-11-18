# GROBID cache per PDF

Prerequisite of mpilhlt/pdf-tei-editor#822 (re-applying annotation decisions to a new extraction after text recognition).

## Problem

Text recognition (mpilhlt/pdf-tei-editor#821) replaces the PDF of a document and keeps its `stable_id` and `doc_id`. The GROBID plugin cached training packages by `doc_id`, GROBID revision and flavor only, so:

1. a new extraction after text recognition was served the cached training data of the earlier PDF, with the text of the old text layer;
2. refreshing the entry (cache disabled, reload feature file) removed the feature file that the versions extracted from the earlier PDF need: segmentation documents lost their page layout, the sync check compared against the wrong tokens, and the training download paired old gold files with the new PDF's feature files.

## Solution

- The cache key gets the first 16 characters of the PDF's SHA-256 (the editor's file id of the PDF): `{doc_id}_{revision}[_{flavor}]_pdf-{hash}`.
- Every training extraction records the full hash as `<label type="pdf-hash">` after the `revision` label of the extractor application in `encodingDesc`.
- Readers of the cache for a stored document (layout provider, feature-token route of the sync check) find the entry from the document's labels (`cache.get_cache_zip()`); documents without `pdf-hash` use the entry without hash, as before.
- The training download pairs each gold file with the package of its PDF (`cache.package_lookups()`): for a gold file of a replaced PDF, only the cache is used (current revision, then the recorded one); without an entry, the file is left out and listed in `skipped-documents.txt`.
- "Reload feature file" refuses documents whose `pdf-hash` differs from the current PDF, and adds the label to documents that have none.
- Old entries are kept until the document is deleted (`delete_cache_for_doc()` removes all entries of a document).

## Not changed

Documents extracted after text recognition but before this change may carry the text of the earlier PDF (served from the cache) without a `pdf-hash` label; they need a fresh extraction.
