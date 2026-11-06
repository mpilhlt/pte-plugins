"""
Server side: resolving the inputs of an evaluation from the editor's data.

Given a document id and a variant, finds the reference version (the document's gold version of the
variant, or any version given by its stable id), the version the model corrects (by default the
first version of the reference's lineage, i.e. the extraction it was derived from, else the
earliest version of the same document and variant), the
variant's unit path (from the extractors' `reviewUnits`) and the rules (the machine refs of the
reference version's editorialDecl, without user overrides, so that every model sees the same text).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from fastapi_app.lib.core.url_cache import UrlCache
from fastapi_app.lib.extraction.registry import ExtractorRegistry
from fastapi_app.lib.models.models import FileMetadata
from fastapi_app.lib.permissions.access_control import check_file_access
from fastapi_app.lib.repository.file_repository import FileRepository
from fastapi_app.lib.storage.file_storage import FileStorage
from fastapi_app.lib.utils.annotation_rules_utils import extract_annotation_rule_refs, fetch_rule_excerpt

logger = logging.getLogger(__name__)

DEFAULT_VARIANT = "grobid.training.references"


class SourceError(Exception):
    """An input of the evaluation cannot be resolved; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ResolvedSource:
    """The inputs of an evaluation (see evaluation.EvalSource)."""

    doc_id: str
    reference_stable_id: str
    reference_is_gold: bool
    input_stable_id: str
    input_choice: str  # how the input was chosen: "given", "lineage" or "earliest"
    variant: str | None
    unit_path: str | None
    gold_xml: str
    input_xml: str
    rules: list[tuple[str, str, str]]  # (category, text, url)


def is_safe_fetch_url(url: str) -> bool:
    """
    Plain http(s) to a globally routable address only, failing closed. The rule refs come from
    document content, which users edit, so they are not fetched from internal addresses. Same check
    as the annotation-review plugin's; a candidate for the core.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def review_unit_path(variant: str | None, registry: ExtractorRegistry | None = None) -> str | None:
    """The unit path an extractor declares for the variant, or None for the automatic default."""
    if not variant:
        return None
    for info in (registry or ExtractorRegistry.get_instance()).list_extractors(available_only=False):
        path = (info.get("reviewUnits") or {}).get(variant)
        if path:
            return str(path)
    return None


def gather_rules(
    xml: str, cache: UrlCache, is_safe: Callable[[str], bool] | None = None
) -> list[tuple[str, str, str]]:
    """
    (category, text, url) for every editorialDecl category, from its machine ref, else its human ref.

    Raises:
        SourceError: no rule could be fetched.
    """
    is_safe = is_safe or is_safe_fetch_url
    rules: list[tuple[str, str, str]] = []
    for entry in extract_annotation_rule_refs(xml):
        refs = sorted(entry["refs"], key=lambda r: r["subtype"] != "machine")
        if not refs:
            continue
        url = refs[0]["target"]
        if not is_safe(url):
            logger.warning(f"auto-annotation: skipping rule category '{entry['category']}' - unsafe URL {url!r}")
            continue
        try:
            rules.append((entry["category"], fetch_rule_excerpt(url, cache, allow_redirects=False), url))
        except Exception as e:  # noqa: BLE001 - one unreachable category is skipped, not fatal
            logger.warning(f"auto-annotation: skipping rule category '{entry['category']}' - {url}: {e}")
    if not rules:
        raise SourceError("No annotation rules could be resolved from the reference version's editorialDecl")
    return rules


def _read(storage: FileStorage, file: FileMetadata) -> str:
    content = storage.read_file(file.id, "tei")
    if content is None:
        raise SourceError(f"File content of {file.stable_id} not found", 404)
    return content.decode("utf-8")


def _tei_file(repo: FileRepository, stable_id: str) -> FileMetadata:
    file = repo.get_file_by_stable_id(stable_id)
    if file is None or file.file_type != "tei":
        raise SourceError(f"No TEI file with stable id {stable_id}", 404)
    return file


def find_reference(
    repo: FileRepository, doc_id: str | None, variant: str | None, reference_stable_id: str | None
) -> FileMetadata:
    """
    The reference version: the one given by its stable id, else the document's gold version of the
    variant (default: DEFAULT_VARIANT).

    Raises:
        SourceError: neither a document nor a reference is given, the reference belongs to another
            document or variant, or the document has no or several gold versions of the variant.
    """
    if reference_stable_id:
        reference = _tei_file(repo, reference_stable_id)
        if doc_id and reference.doc_id != doc_id:
            raise SourceError(f"{reference_stable_id} belongs to document {reference.doc_id}, not {doc_id}")
        if variant and reference.variant != variant:
            raise SourceError(f"{reference_stable_id} is of variant {reference.variant!r}, not {variant!r}")
        return reference
    if not doc_id:
        raise SourceError("Pass a document id or the stable id of a reference version")
    variant = variant or DEFAULT_VARIANT
    golds = [
        f for f in repo.get_files_by_doc_id(doc_id)
        if f.file_type == "tei" and f.variant == variant and f.is_gold_standard
    ]
    if len(golds) != 1:
        found = "no gold version" if not golds else f"{len(golds)} gold versions ({', '.join(f.stable_id for f in golds)})"
        raise SourceError(f"Document {doc_id} has {found} of variant {variant!r}; pass the reference version explicitly", 404 if not golds else 422)
    return golds[0]


def lineage_root(repo: FileRepository, reference: FileMetadata) -> FileMetadata | None:
    """
    The first version the reference was derived from (following parent_stable_id) that has the
    same document and variant, or None if the reference has no such ancestor.
    """
    root: FileMetadata | None = None
    seen = {reference.stable_id}
    parent_id = reference.parent_stable_id
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = repo.get_file_by_stable_id(parent_id)
        if parent is None or parent.file_type != "tei" or parent.doc_id != reference.doc_id or parent.variant != reference.variant:
            break
        root = parent
        parent_id = parent.parent_stable_id
    return root


def resolve_source(
    doc_id: str | None,
    variant: str | None,
    reference_stable_id: str | None,
    input_stable_id: str | None,
    user: dict[str, object],
    repo: FileRepository,
    storage: FileStorage,
    cache: UrlCache,
    with_rules: bool = True,
) -> ResolvedSource:
    """
    Resolve the reference version (see find_reference), the input version, the unit path and the rules.
    The input version is the given one, else the root of the reference's lineage (see lineage_root),
    else the earliest version of the document and variant.

    Raises:
        SourceError: a file is missing or not readable by the user, or no reference, input version
            or rule can be found.
    """
    reference = find_reference(repo, doc_id, variant, reference_stable_id)
    if not check_file_access(reference, user, "read"):
        raise SourceError(f"No read access to {reference.stable_id}", 403)

    if input_stable_id:
        source = _tei_file(repo, input_stable_id)
        if source.doc_id != reference.doc_id:
            raise SourceError(f"{input_stable_id} belongs to another document than {reference.stable_id}")
        input_choice = "given"
    elif (root := lineage_root(repo, reference)) is not None:
        source = root
        input_choice = "lineage"
    else:
        # get_all_versions() leaves out gold versions; a non-gold reference is left out here
        versions = sorted(
            (v for v in repo.get_all_versions(reference.doc_id, reference.variant) if v.stable_id != reference.stable_id),
            key=lambda v: (v.version or 0, str(v.created_at)),
        )
        if not versions:
            raise SourceError(
                f"Document {reference.doc_id} has no version of variant {reference.variant!r} besides the reference; pass the input version explicitly",
                404,
            )
        source = versions[0]
        input_choice = "earliest"
    if not check_file_access(source, user, "read"):
        raise SourceError(f"No read access to {source.stable_id}", 403)

    gold_xml = _read(storage, reference)
    return ResolvedSource(
        doc_id=reference.doc_id,
        reference_stable_id=reference.stable_id,
        reference_is_gold=bool(reference.is_gold_standard),
        input_stable_id=source.stable_id,
        input_choice=input_choice,
        variant=reference.variant,
        unit_path=review_unit_path(reference.variant),
        gold_xml=gold_xml,
        input_xml=_read(storage, source),
        rules=gather_rules(gold_xml, cache) if with_rules else [],
    )
