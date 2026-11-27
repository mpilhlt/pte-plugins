# webdav-sync: one PDF per TEI version

pdf-tei-editor#944 (PR #945, migration 015) attaches every TEI file to its own PDF (`pdf_stable_id`) and drops the `superseded_by` column of archived PDFs. webdav-sync now:

- creates downloaded files with `parent_stable_id` and `pdf_stable_id` only;
- drops the fields of incoming ops that the local `FileMetadata` model does not have before it applies them. Ops written by an instance with another core version can carry such fields (e.g. `superseded_by` from a core before #944), and `FileRepository.apply_remote_metadata()` writes every field as a column, so the update failed with "no such column".

Syncing between instances on both sides of #944 does not convert the data: an archived PDF from an older instance arrives as one more PDF of the document, and TEI files without `pdf_stable_id` fall back to the canonical PDF. Instances that sync with each other should run core versions on the same side of #944.
