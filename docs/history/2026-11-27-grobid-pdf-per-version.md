# GROBID plugin: one PDF per TEI version

pdf-tei-editor#944 (PR #945) attaches every TEI file to the PDF it was made from and drops archived PDFs (`superseded_by`). The GROBID plugin already looks up the PDF of a version with `FileRepository.get_pdf_for_file()` (`pdf_source.get_source_pdf()`), which returns the attached PDF in the new model, so feature-file reloads and the training download keep working. Only the archived-PDF part is removed:

- `pdf_source.is_archived_pdf()` and `ReloadTarget.from_archived_pdf` are gone, and so is the reload preview note "This version was made before text recognition replaced the PDF". In the new model the PDF of a version is never replaced, so nothing needs pointing out.
- The tests that were skipped on cores without `superseded_by` now run against the attached PDF (`pdf_stable_id`). `test_archived_pdf_source.py` is renamed to `test_pdf_source.py`.
- The README, docstrings and comments describe the attached PDF instead of archived ones.

A version whose `pdf-hash` label differs from its PDF can still exist, if text recognition changed the PDF in place before pdf-tei-editor#924. The reload refuses it and the training download only uses the cache for it, as before.
