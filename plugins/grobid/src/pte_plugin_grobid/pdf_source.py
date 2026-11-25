"""
The PDF a TEI version was made from.

Text recognition (OCR) replaces the PDF of a document. Since pdf-tei-editor#924, the core keeps
the original as an archived PDF (`superseded_by` set) when the document has versions, and links
the versions made from it through `pdf_stable_id`; `FileRepository.get_pdf_for_file()` returns
it. Feature files must be computed from that PDF, since the current PDF's text layer does not
match the text of these versions. Older cores have no such method and no archived PDFs.
"""

from fastapi_app.lib.models.models import FileMetadata
from fastapi_app.plugin_api import FileRepository


def get_source_pdf(file_repo: FileRepository, tei_file: FileMetadata) -> FileMetadata | None:
    """
    The PDF a version was made from: its archived original PDF if text recognition has replaced
    the document's PDF since, otherwise the document's current PDF.

    Args:
        file_repo: File repository
        tei_file: The TEI version

    Returns:
        FileMetadata of the PDF, or None if the document has no PDF
    """
    get_pdf_for_file = getattr(file_repo, "get_pdf_for_file", None)
    if get_pdf_for_file is not None:
        return get_pdf_for_file(tei_file)
    return file_repo.get_pdf_for_document(tei_file.doc_id)


def is_archived_pdf(pdf_file: FileMetadata) -> bool:
    """Whether a PDF is an archived original, replaced by text recognition (core with #924)."""
    return bool(getattr(pdf_file, "superseded_by", None))
