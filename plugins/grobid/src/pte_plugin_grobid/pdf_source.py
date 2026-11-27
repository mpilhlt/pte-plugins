"""
The PDF a TEI version was made from.

Every TEI file is attached to the PDF it was made from (`pdf_stable_id`, pdf-tei-editor#944), and
PDFs never change: text recognition creates a new PDF of the document, to which only the version
it makes is attached. `FileRepository.get_pdf_for_file()` returns that PDF, or the canonical PDF of
the document for a file without one. Feature files must be computed from it, since the text layer
of another PDF of the document does not match the text of the version. Older cores have no such
method and one PDF per document.
"""

from fastapi_app.lib.models.models import FileMetadata
from fastapi_app.plugin_api import FileRepository


def get_source_pdf(file_repo: FileRepository, tei_file: FileMetadata) -> FileMetadata | None:
    """
    The PDF a version was made from: the PDF it is attached to, otherwise the document's PDF.

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
