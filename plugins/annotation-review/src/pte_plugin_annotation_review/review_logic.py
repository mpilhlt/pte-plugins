"""
Rule-excerpt gathering and review orchestration for the annotation-review
backend plugin.

See docs/superpowers/specs/2026-09-24-llm-annotation-review-design.md
(Part C) for the design rationale.
"""

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Callable, Optional
from urllib.parse import urlparse

from lxml import etree

from fastapi_app.lib.core.url_cache import UrlCache
from fastapi_app.lib.doc_rules.resource_key import normalize_resource_key
from fastapi_app.lib.doc_rules.storage import DocumentRulesStore
from fastapi_app.plugin_api import LLMProvider
from fastapi_app.lib.utils.annotation_rules_utils import (
    AnnotationRuleRef,
    extract_annotation_rule_refs,
    fetch_rule_excerpt,
)

from .chunking import split_into_chunks
from .prompts import (
    Finding,
    UnusableResponseError,
    build_retry_note,
    build_system_prompt,
    build_user_prompt,
    validate_findings,
)

try:
    from fastapi_app.lib.doc_rules.interpretation_ref_kind import selected_rule_override_text
except ImportError:  # editor older than the two-layer document rules (pdf-tei-editor#666)
    from fastapi_app.lib.doc_rules.interpretation_ref_kind import representative_ref

    def selected_rule_override_text(
        store: DocumentRulesStore,
        entry: AnnotationRuleRef,
        owner: str,
        is_safe_url: Optional[Callable[[str], bool]] = None,
    ) -> Optional[str]:
        """Pre-#666 lookup: the override stored under the representative ref's key."""
        rep_ref = representative_ref(entry["refs"])
        if rep_ref is None:
            return None
        return store.get_selected_override_text("interpretation-ref", normalize_resource_key(rep_ref["target"]), owner)

logger = logging.getLogger(__name__)

TEI_NS = "http://www.tei-c.org/ns/1.0"

_TEXT_ELEMENT_RE = re.compile(r"<text\b[^>]*>.*</text\s*>", re.DOTALL)


class NoRuleExcerptsError(Exception):
    """Raised when no rule excerpt could be resolved for any category — nothing to review against."""


def _is_safe_fetch_url(url: str) -> bool:
    """
    Reject rule-ref URLs that could be used for SSRF: only plain http/https
    is allowed, and the resolved host must be a globally routable address
    (rejects private, loopback, link-local, multicast, reserved,
    unspecified, and CGNAT/RFC 6598 space). Fails closed - any parse or
    DNS-resolution error is treated as unsafe.

    This check exists here (not in annotation_rules_utils.py) because this
    plugin is the first caller that resolves a URL taken directly from
    client-supplied XML rather than from server config or a write-time-pinned
    ref - see docs/superpowers/specs/2026-09-24-llm-annotation-review-design.md
    (Part C) for context; other callers of fetch_rule_excerpt/resolve_forge_permalink
    have a different, lower-risk trust model and are out of scope for this check.

    Known residual risk (accepted, not fixed here): this validates the
    hostname's resolved IP at check time, not at actual connect time - a
    DNS-rebinding attacker (a very short TTL record that resolves
    differently a moment later) could still bypass this. Closing that
    fully would require connect-time IP pinning (a custom transport
    adapter), which is disproportionate effort while this plugin has no
    UI trigger yet (see docs/superpowers/specs/2026-09-24-llm-annotation-review-design.md,
    Parts D/E, not yet built).
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False

    try:
        addr_info = socket.getaddrinfo(parsed.hostname, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return False

    for _family, _, _, _, sockaddr in addr_info:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            return False

    return True


def extract_text_content(xml_string: str) -> str:
    """
    Return the raw <text>...</text> substring of a TEI document, exactly as
    it appears in the original string (not re-serialized) - preserves quote
    style, entity encoding, and whitespace exactly, since Part E will later
    re-match each finding's "old" against the live editor's raw text and
    silently drop anything that doesn't match byte-for-byte.

    Raises:
        ValueError: if the document is not well-formed XML, or has no
            <text> element.
    """
    try:
        root = etree.fromstring(xml_string.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        raise ValueError(f"Document is not well-formed XML: {e}") from e

    text_el = root.find(f".//{{{TEI_NS}}}text")
    if text_el is None:
        raise ValueError("Document has no <text> element")

    match = _TEXT_ELEMENT_RE.search(xml_string)
    if match is None:
        # Degenerate case the regex isn't built for (e.g. self-closing <text/>) -
        # fall back to the serialized form rather than crashing.
        return etree.tostring(text_el, encoding="unicode", xml_declaration=False)
    return match.group(0)


def gather_rule_excerpts(
    xml_string: str,
    cache: UrlCache,
    store: DocumentRulesStore,
    owner: str,
) -> list[tuple[str, str]]:
    """
    Resolve one rule excerpt per editorialDecl category. `owner`'s selected
    override for the category's resource (if any) is used verbatim - no
    fetch, no SSRF check needed, since override text is already stored and
    trusted. Otherwise falls back to fetching the category's "machine"-
    subtype ref (a raw URL taken directly from client-supplied XML, so kept
    behind the existing SSRF/redirect hardening below), sliced to just the
    relevant excerpt. A category with no machine ref, or whose excerpt
    fails to fetch, is skipped (logged), not raised.

    The override is looked up by the editor's selected_rule_override_text(),
    which picks the layer the reviewer reads (the separate machine layer if
    the category has one, else the human layer) and its resource key. Do not
    derive the key here: it must match how the editor stores overrides.
    """
    excerpts: list[tuple[str, str]] = []
    for rule_ref in extract_annotation_rule_refs(xml_string):
        override_text = selected_rule_override_text(store, rule_ref, owner, is_safe_url=_is_safe_fetch_url)
        if override_text is not None:
            excerpts.append((rule_ref["category"], override_text))
            continue

        machine_ref = next((r for r in rule_ref["refs"] if r["subtype"] == "machine"), None)
        if machine_ref is None:
            continue
        if not _is_safe_fetch_url(machine_ref["target"]):
            logger.warning(
                f"annotation-review: skipping rule category '{rule_ref['category']}' "
                f"- unsafe fetch target: {machine_ref['target']!r}"
            )
            continue
        try:
            excerpt = fetch_rule_excerpt(machine_ref["target"], cache, allow_redirects=False)
        except Exception as e:  # noqa: BLE001 - any fetch failure for one category is skip-worthy, not fatal
            logger.warning(
                f"annotation-review: skipping rule category '{rule_ref['category']}' "
                f"- could not fetch excerpt from {machine_ref['target']}: {e}"
            )
            continue
        excerpts.append((rule_ref["category"], excerpt))
    return excerpts


def review_source(xml_string: str, scope: str | None = None) -> tuple[str, str]:
    """
    Return (text to review, text findings are validated against). Without a
    scope, both are the document's raw <text>. With a scope (the raw XML of a
    single node, e.g. the one selected in the editor's node navigation), only
    the scope is reviewed, and findings are validated against the whole
    document, since the node may lie outside <text> and the client locates
    findings in the whole document.

    Raises:
        ValueError: document is malformed, has no <text> element (unscoped),
            or the scope is empty or not part of the document.
    """
    if scope is None:
        text_content = extract_text_content(xml_string)
        return text_content, text_content
    if not scope.strip() or scope not in xml_string:
        raise ValueError("The node to review is not part of the document")
    try:
        etree.fromstring(xml_string.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        raise ValueError(f"Document is not well-formed XML: {e}") from e
    return scope, xml_string


def count_chunks(xml_string: str, scope: str | None = None) -> int:
    """
    Number of chunks the document's <text> (or the given scope) is reviewed in.

    Raises:
        ValueError: see review_source().
    """
    return len(split_into_chunks(review_source(xml_string, scope)[0]))


def chunk_range(xml_string: str, chunk_index: int, scope: str | None = None) -> tuple[int, int] | None:
    """
    Position of a chunk in the document string, as (start, end) offsets in UTF-16 code units, so
    that the editor can use them directly on the text it sent. The editor uses them to mark the
    review units inside the chunk as reviewed (unit review mode). Returns None if the reviewed text
    is not a substring of the document (the rare fallback in extract_text_content()) or the index
    is out of range.

    Raises:
        ValueError: see review_source().
    """
    review_text = review_source(xml_string, scope)[0]
    base = xml_string.find(review_text)
    chunks = split_into_chunks(review_text)
    if base == -1 or not 0 <= chunk_index < len(chunks):
        return None
    start = base + sum(len(chunk) for chunk in chunks[:chunk_index])
    end = start + len(chunks[chunk_index])

    def utf16(offset: int) -> int:
        return len(xml_string[:offset].encode("utf-16-le")) // 2

    return utf16(start), utf16(end)


async def run_review(
    xml_string: str,
    provider: LLMProvider,
    model_id: str,
    cache: UrlCache,
    store: DocumentRulesStore,
    owner: str,
    chunk_index: int = 0,
    scope: str | None = None,
) -> tuple[list[Finding], int, int]:
    """
    Review one chunk of the document's <text>: gather rule excerpts, build the
    prompt, call the LLM, and return (validated findings, total chunk count,
    number of findings discarded because their "old" does not occur exactly
    once). Findings are validated against the whole <text>, so "old" must be
    unique in the document, not just in the chunk. With a scope, only that
    node is reviewed, "old" must be unique in that node, and findings are
    anchored in the whole document (see review_source() and validate_findings()).

    If the model proposed findings but every one of them was discarded, the
    call is repeated once with the discarded snippets quoted back to it;
    smaller models often paraphrase "old" instead of copying it verbatim.

    Raises:
        ValueError: see review_source(), or chunk_index is out of range.
        NoRuleExcerptsError: no rule excerpt could be resolved for any category.
    """
    review_text, validation_text = review_source(xml_string, scope)
    chunks = split_into_chunks(review_text)
    if not 0 <= chunk_index < len(chunks):
        raise ValueError(f"chunk_index {chunk_index} out of range (document has {len(chunks)} chunks)")
    rule_excerpts = await asyncio.to_thread(gather_rule_excerpts, xml_string, cache, store, owner)
    if not rule_excerpts:
        raise NoRuleExcerptsError("No rule excerpts could be resolved for this document")

    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(
        rule_excerpts, chunks[chunk_index], (chunk_index + 1, len(chunks)), scoped=scope is not None
    )
    raw_response = await provider.chat_completion(model_id, system_prompt, user_prompt)
    findings, unmatched = validate_findings(raw_response, validation_text, scope)
    if not findings and unmatched:
        logger.info(f"annotation-review: all {len(unmatched)} finding(s) discarded, retrying once with feedback")
        retry_response = await provider.chat_completion(model_id, system_prompt, user_prompt + build_retry_note(unmatched))
        try:
            findings, unmatched = validate_findings(retry_response, validation_text, scope)
        except UnusableResponseError:
            logger.warning("annotation-review: retry returned no usable JSON list, keeping the first attempt's result")
    return findings, len(chunks), len(unmatched)
