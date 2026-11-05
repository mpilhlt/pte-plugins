"""
Server side: resolving the inputs of an evaluation from the editor's data.

Given a gold version's stable id, finds the version the model corrects (by default the earliest
version of the same document and variant, which is normally the extraction), the variant's unit
path (from the extractors' `reviewUnits`) and the rules (the machine refs of the gold version's
editorialDecl, without user overrides, so that every model sees the same text).
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


class SourceError(Exception):
    """An input of the evaluation cannot be resolved; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ResolvedSource:
    """The inputs of an evaluation (see evaluation.EvalSource)."""

    gold_stable_id: str
    input_stable_id: str
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
        raise SourceError("No annotation rules could be resolved from the gold version's editorialDecl")
    return rules


def _read(storage: FileStorage, file: FileMetadata) -> str:
    content = storage.read_file(file.id, "tei")
    if content is None:
        raise SourceError(f"File content of {file.stable_id} not found", 404)
    return content.decode("utf-8")


def resolve_source(
    gold_stable_id: str,
    input_stable_id: str | None,
    user: dict[str, object],
    repo: FileRepository,
    storage: FileStorage,
    cache: UrlCache,
    with_rules: bool = True,
) -> ResolvedSource:
    """
    Resolve the gold version, the input version, the unit path and the rules.

    Raises:
        SourceError: a file is missing, not readable by the user, not a gold version, or no input
            version or rule can be found.
    """
    gold = repo.get_file_by_stable_id(gold_stable_id)
    if gold is None or gold.file_type != "tei":
        raise SourceError(f"No TEI file with stable id {gold_stable_id}", 404)
    if not check_file_access(gold, user, "read"):
        raise SourceError(f"No read access to {gold_stable_id}", 403)
    if not gold.is_gold_standard:
        raise SourceError(f"{gold_stable_id} is not a gold version")

    if input_stable_id:
        source = repo.get_file_by_stable_id(input_stable_id)
        if source is None or source.file_type != "tei":
            raise SourceError(f"No TEI file with stable id {input_stable_id}", 404)
        if source.doc_id != gold.doc_id:
            raise SourceError(f"{input_stable_id} belongs to another document than {gold_stable_id}")
    else:
        versions = repo.get_all_versions(gold.doc_id, gold.variant)
        if not versions:
            raise SourceError(
                f"Document {gold.doc_id} has no version of variant {gold.variant!r} besides the gold one; pass the input version explicitly",
                404,
            )
        source = versions[0]
    if not check_file_access(source, user, "read"):
        raise SourceError(f"No read access to {source.stable_id}", 403)

    gold_xml = _read(storage, gold)
    return ResolvedSource(
        gold_stable_id=gold.stable_id,
        input_stable_id=source.stable_id,
        variant=gold.variant,
        unit_path=review_unit_path(gold.variant),
        gold_xml=gold_xml,
        input_xml=_read(storage, source),
        rules=gather_rules(gold_xml, cache) if with_rules else [],
    )
