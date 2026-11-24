"""
Import of existing GROBID training data (#68): gold TEI annotations, their raw feature files and
the PDFs they were made from.

The files are found anywhere in the unpacked upload by their names, which follow GROBID's
`createTraining` output: `{name}.{suffix}.tei.xml` is an annotation, `{name}.{suffix}` its feature
file (`suffix`: the variant id without `grobid.`), `{name}.pdf` the PDF. A document is skipped when
its PDF is missing or differs from the PDF the collection already has for its doc_id. The feature
files go into the extraction cache under the key of the imported documents (revision, flavor and
PDF hash from their encodingDesc), so that the sync check, the layout and the training download
use them. The annotations become gold files, or versions if the document already has a gold file of
the variant.
"""

import datetime
import io
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from fastapi_app.lib.models import FileCreate, FileMetadata, FileUpdate
from fastapi_app.lib.repository.file_repository import FileRepository
from fastapi_app.lib.storage.file_storage import FileStorage
from fastapi_app.lib.utils.doi_utils import encode_for_xml_id
from fastapi_app.lib.utils.hash_utils import generate_file_hash
from fastapi_app.lib.utils.tei_utils import (
    create_revision_desc_with_status,
    create_schema_processing_instruction,
    extract_tei_metadata,
    serialize_tei_with_formatted_header,
    update_pdf_metadata_from_tei,
)
from pte_plugin_grobid.cache import add_to_cache
from pte_plugin_grobid.config import get_schema_url, get_supported_variants
from pte_plugin_grobid.handlers.training import normalize_grobid_content
from pte_plugin_grobid.layout import SEGMENTATION

logger = logging.getLogger(__name__)

TEI_NS = "http://www.tei-c.org/ns/1.0"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"

# Revision label of imported documents when the GROBID revision of their feature files is not known
DEFAULT_REVISION = "imported"

# Description of the revisionDesc change of imported documents
CHANGE_DESCRIPTION = "Import"


@dataclass
class ImportDocument:
    """The files of one document found in the upload."""

    name: str
    pdf: Path | None = None
    tei: dict[str, Path] = field(default_factory=dict)  # variant id -> annotation
    raw: dict[str, Path] = field(default_factory=dict)  # variant id -> feature file


@dataclass
class ImportReport:
    """What the import did, for the upload form."""

    documents: int = 0
    files_imported: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)  # {"name", "reason"}
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, int | list[dict[str, str]] | list[str]]:
        """JSON response of the import route."""
        return {
            "documents": self.documents,
            "files_imported": self.files_imported,
            "skipped": self.skipped,
            "warnings": self.warnings,
        }


def _training_suffixes() -> list[tuple[str, str]]:
    """(variant id, file name suffix) of the training variants, longest suffix first."""
    variants = [v for v in get_supported_variants() if v.startswith("grobid.training.")]
    pairs = [(v, v.removeprefix("grobid.")) for v in variants]
    return sorted(pairs, key=lambda p: len(p[1]), reverse=True)


def find_documents(root: Path) -> tuple[dict[str, ImportDocument], list[str]]:
    """
    The documents in a directory tree, by name, and the files that belong to no variant.

    Hidden files and `__MACOSX` folders are ignored, and so are text and TSV files (README,
    mappings).
    """
    documents: dict[str, ImportDocument] = {}
    unrecognized: list[str] = []
    suffixes = _training_suffixes()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or any(part.startswith(".") or part == "__MACOSX" for part in rel.parts):
            continue
        name = path.name
        if name.lower().endswith((".txt", ".tsv", ".md")):
            continue
        if name.lower().endswith(".pdf"):
            documents.setdefault(name[:-4], ImportDocument(name[:-4])).pdf = path
            continue
        for variant, suffix in suffixes:
            if name.endswith(f".{suffix}.tei.xml"):
                stem = name[: -len(f".{suffix}.tei.xml")]
                documents.setdefault(stem, ImportDocument(stem)).tei[variant] = path
                break
            if name.endswith(f".{suffix}"):
                stem = name[: -len(f".{suffix}")]
                documents.setdefault(stem, ImportDocument(stem)).raw[variant] = path
                break
        else:
            unrecognized.append(str(rel))
    return documents, unrecognized


def _find(parent: etree._Element, tag: str) -> etree._Element | None:
    """Child of parent with the TEI or the plain tag name."""
    child = parent.find(f"{{{TEI_NS}}}{tag}")
    return child if child is not None else parent.find(tag)


def build_document(
    xml: bytes, doc_id: str, variant_id: str, flavor: str, revision: str, pdf_hash: str, timestamp: str
) -> str:
    """
    The editor document of an imported annotation: its fileDesc (with an `xml:id` from the doc_id,
    as in extracted documents; GROBID's is the file name, which is no NCName) and text as imported, with the
    encodingDesc of an extraction (labels model, flavor, variant-id, revision, pdf-hash), a
    revisionDesc with the import as change, and the schema processing instruction.
    """
    from pte_plugin_grobid.extractor import GrobidTrainingExtractor

    content = normalize_grobid_content(xml.decode("utf-8"), variant_id)
    source = etree.fromstring(content.encode("utf-8"), etree.XMLParser(recover=True))
    source_header = _find(source, "teiHeader")
    source_text = _find(source, "text")
    if source_text is None:
        raise ValueError("no <text> element")

    tei_doc = etree.Element(f"{{{TEI_NS}}}TEI", nsmap={None: TEI_NS})  # type: ignore[dict-item]
    header = etree.SubElement(tei_doc, f"{{{TEI_NS}}}teiHeader")
    file_desc = _find(source_header, "fileDesc") if source_header is not None else None
    if file_desc is not None:
        header.append(file_desc)
    else:
        file_desc = etree.SubElement(header, f"{{{TEI_NS}}}fileDesc")
    file_desc.set(XML_ID, encode_for_xml_id(doc_id))
    header.append(
        GrobidTrainingExtractor()._build_encoding_desc(
            timestamp=timestamp,
            grobid_version="unknown",
            grobid_revision=revision,
            variant_id=variant_id,
            flavor=flavor,
            pdf_hash=pdf_hash,
        )
    )
    header.append(create_revision_desc_with_status(timestamp, "extraction", CHANGE_DESCRIPTION, label=CHANGE_DESCRIPTION))
    source_text.tail = None
    tei_doc.append(source_text)
    schema_pi = create_schema_processing_instruction(get_schema_url(variant_id))
    return serialize_tei_with_formatted_header(tei_doc, [schema_pi])


def _insert_page_breaks(xml: str, doc_id: str, report: ImportReport) -> str:
    """The segmentation document with page breaks from its imported feature file."""
    from pte_plugin_grobid.layout import insert_page_breaks_from_feature_file

    try:
        result = insert_page_breaks_from_feature_file(xml, doc_id)
    except Exception as e:  # noqa: BLE001 - page breaks are an addition, never a reason to fail the import
        result = None
        logger.warning(f"Page breaks not inserted into {doc_id}: {e}")
    if result is None:
        report.warnings.append(f"{doc_id}: no page breaks inserted, the feature file does not match the document's lines")
        return xml
    return result[0]


def _add_collection(repo: FileRepository, file_id: str, collections: list[str], collection: str) -> None:
    """Add the file to the collection if it is not in it."""
    if collection not in collections:
        repo.update_file(file_id, FileUpdate(doc_collections=[*collections, collection]))


class TrainingDataImporter:
    """Imports the documents of an unpacked upload into a collection."""

    def __init__(
        self, repo: FileRepository, storage: FileStorage, collection: str, flavor: str, revision: str, username: str | None
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.collection = collection
        self.flavor = flavor
        self.revision = revision or DEFAULT_REVISION
        self.username = username
        self.report = ImportReport()

    def import_directory(self, root: Path) -> ImportReport:
        """Import all documents found under root."""
        documents, unrecognized = find_documents(root)
        if unrecognized:
            self.report.warnings.append(f"Ignored {len(unrecognized)} files of no known variant: {', '.join(unrecognized[:5])}")
        for doc in documents.values():
            if not doc.tei:
                if not doc.raw:
                    self._skip(doc.name, "PDF without annotations")
                else:
                    self._skip(doc.name, "feature files without annotations")
                continue
            try:
                self._import_document(doc)
            except Exception as e:  # noqa: BLE001 - one broken document does not stop the import
                logger.exception(f"Import of {doc.name} failed")
                self._skip(doc.name, f"import failed: {e}")
        return self.report

    def _skip(self, name: str, reason: str) -> None:
        self.report.skipped.append({"name": name, "reason": reason})

    def _doc_id(self, doc: ImportDocument) -> str:
        """The doc_id of the document: from the DOI of its first annotation (encoded as by the core), else its name."""
        for path in doc.tei.values():
            # recover: GROBID's xml:id (the file name) is no NCName
            root = etree.fromstring(path.read_bytes(), etree.XMLParser(recover=True))
            if root is None:
                continue
            # without GROBID's xml:id, the doc_id comes from the DOI, as for documents uploaded with a DOI
            for file_desc in root.iter(f"{{{TEI_NS}}}fileDesc", "fileDesc"):
                if XML_ID in file_desc.attrib:
                    del file_desc.attrib[XML_ID]
            metadata = extract_tei_metadata(root)
            doc_id = metadata.get("doc_id")
            if doc_id:
                return doc_id
        return doc.name

    def _import_document(self, doc: ImportDocument) -> None:
        if doc.pdf is None:
            self._skip(doc.name, "no PDF")
            return
        doc_id = self._doc_id(doc)
        pdf_bytes = doc.pdf.read_bytes()
        pdf_hash = generate_file_hash(pdf_bytes)

        existing = [f for f in self.repo.get_files_by_doc_id(doc_id) if not f.deleted]
        existing_pdf = next((f for f in existing if f.file_type == "pdf"), None)
        if existing_pdf is not None and existing_pdf.id != pdf_hash:
            self._skip(doc.name, f"the collection has a different PDF for {doc_id}")
            return

        # The feature files first: the page breaks of segmentation documents come from them
        raw_files = {path.name: path.read_bytes() for path in doc.raw.values()}
        if raw_files:
            add_to_cache(doc_id, self.revision, self.flavor, pdf_hash, raw_files)
        for variant in doc.tei:
            if variant not in doc.raw:
                self.report.warnings.append(f"{doc_id}: no feature file for {variant}")

        if existing_pdf is None:
            pdf_file = self.repo.insert_file(
                FileCreate(
                    id=pdf_hash,
                    filename=doc.pdf.name,
                    doc_id=doc_id,
                    file_type="pdf",
                    file_size=len(pdf_bytes),
                    doc_collections=[self.collection],
                    file_metadata={"imported_at": datetime.datetime.now().isoformat()},
                    created_by=self.username,
                )
            )
            if not self.storage.file_exists(pdf_hash, "pdf"):
                self.storage.save_file(pdf_bytes, "pdf", increment_ref=False)
            self.report.files_imported += 1
        else:
            pdf_file = existing_pdf
            _add_collection(self.repo, existing_pdf.id, existing_pdf.doc_collections, self.collection)

        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        imported = 0
        for variant, path in doc.tei.items():
            xml = build_document(path.read_bytes(), doc_id, variant, self.flavor, self.revision, pdf_hash, timestamp)
            if variant == SEGMENTATION and variant in doc.raw:
                xml = _insert_page_breaks(xml, doc_id, self.report)
            if self._store_tei(doc_id, variant, xml, existing, pdf_file):
                imported += 1
        if imported:
            self.report.documents += 1

    def _store_tei(self, doc_id: str, variant: str, xml: str, existing: list[FileMetadata], pdf_file: FileMetadata) -> bool:
        """Store an annotation as gold file, or as version if the variant has a gold file; False if identical content exists."""
        content = xml.encode("utf-8")
        file_hash = generate_file_hash(content)
        same_variant = [f for f in existing if f.file_type == "tei" and f.variant == variant]
        if any(f.id == file_hash for f in same_variant):
            self.report.warnings.append(f"{doc_id}: {variant} already imported")
            return False
        has_gold = any(f.is_gold_standard for f in same_variant)
        metadata = extract_tei_metadata(etree.fromstring(content))
        if has_gold:
            self.report.warnings.append(f"{doc_id}: {variant} has a gold file, imported as version")
            versions = [f.version for f in same_variant if f.version is not None]
            version: int | None = max(versions, default=0) + 1
        else:
            version = None
        if not self.storage.file_exists(file_hash, "tei"):
            self.storage.save_file(content, "tei", increment_ref=False)
        self.repo.insert_file(
            FileCreate(
                id=file_hash,
                filename=f"{doc_id.replace('/', '_')}.{variant}.tei.xml",
                doc_id=doc_id,
                file_type="tei",
                file_size=len(content),
                label=f"{CHANGE_DESCRIPTION} ({self.revision})",
                status="extraction",
                last_revision=None,
                variant=variant,
                version=version,
                is_gold_standard=not has_gold,
                doc_collections=[self.collection],
                file_metadata={"imported_at": datetime.datetime.now().isoformat()},
                created_by=self.username,
            )
        )
        self.report.files_imported += 1
        if not has_gold and not pdf_file.doc_metadata:
            update_pdf_metadata_from_tei(pdf_file, metadata, self.repo, logger)
        return True


def unpack_upload(data: bytes, target: Path) -> None:
    """Unpack an uploaded ZIP into target (zipfile drops absolute paths and `..`)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(target)
    except zipfile.BadZipFile as e:
        raise ValueError("The upload is not a ZIP file") from e
