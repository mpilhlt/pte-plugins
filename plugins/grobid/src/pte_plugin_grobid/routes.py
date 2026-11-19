"""
Custom routes for GROBID plugin.

Provides download endpoint for GROBID training data packages and a diagnostics
endpoint for debugging connectivity issues with the GROBID server.
"""

import asyncio
import io
import logging
import os
import shutil
import zipfile
from datetime import datetime

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from fastapi_app.lib.core.dependencies import (
    get_db,
    get_file_storage,
    get_sse_service,
    require_session_id,
)
from fastapi_app.lib.core.database import DatabaseManager
from fastapi_app.plugin_api import (
    FileStorage,
    require_admin_user,
    require_authenticated_user,
    require_reviewer_or_admin,
)
from fastapi_app.lib.sse.sse_service import SSEService
from fastapi_app.lib.sse.sse_utils import ProgressBar, send_notification
from pte_plugin_grobid.cache import cache_training_data, check_cache, package_lookups
from pte_plugin_grobid.config import get_grobid_server_url, get_model_path, get_grobid_server_timeout, is_grobid_cache_disabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plugins/grobid", tags=["grobid"])


def _corpus_base_path(tei_content: str, variant: str, fallback_flavor: str) -> str:
    """
    Derive the corpus base path from TEI header labels.

    Reads model and flavor from encodingDesc/appInfo/application[@type='extractor'].
    Tries both namespaced and un-namespaced XPath because the actual namespace of
    encodingDesc elements depends on how the TEI was created and serialized.

    Falls back to parsing variant and fallback_flavor if labels are absent.
    For the default flavor the path is ``{model}/corpus``; otherwise
    ``{model}/{flavor}/corpus``.
    """
    from lxml import etree
    NS = "http://www.tei-c.org/ns/1.0"

    def _find_app(root: etree._Element) -> etree._Element | None:  # type: ignore[name-defined]
        """Find application[@type='extractor'] trying both namespace variants."""
        return (
            root.find(".//encodingDesc/appInfo/application[@type='extractor']")
            or root.find(
                f".//{{{NS}}}encodingDesc/{{{NS}}}appInfo"
                f"/{{{NS}}}application[@type='extractor']"
            )
        )

    def _find_label(app: etree._Element, label_type: str) -> str | None:  # type: ignore[name-defined]
        """Find label text by type, trying both namespace variants."""
        el = app.find(f"label[@type='{label_type}']") or app.find(
            f"{{{NS}}}label[@type='{label_type}']"
        )
        return el.text if el is not None and el.text else None

    try:
        parser = etree.XMLParser(recover=True)
        root = etree.fromstring(tei_content.encode("utf-8"), parser)
        app = _find_app(root)
        if app is not None:
            tei_flavor = _find_label(app, "flavor") or fallback_flavor
            variant_id_label = _find_label(app, "variant-id") or ""
            model = (
                _find_label(app, "model")
                or (get_model_path(variant_id_label) if variant_id_label else "")
                or get_model_path(variant)
            )
            if tei_flavor == "default":
                return f"{model}/corpus"
            return f"{model}/{tei_flavor}/corpus"
    except Exception:
        pass
    # Last-resort fallback: derive from variant + fallback_flavor
    model_path = get_model_path(variant)
    if fallback_flavor == "default":
        return f"{model_path}/corpus"
    return f"{model_path}/{fallback_flavor}/corpus"


# Cancellation tokens for in-progress downloads
_cancellation_tokens: dict[str, bool] = {}


class CancellationToken:
    """Simple cancellation token for cooperative cancellation."""

    def __init__(self, progress_id: str):
        self.progress_id = progress_id
        _cancellation_tokens[progress_id] = False

    def cancel(self):
        """Request cancellation."""
        _cancellation_tokens[self.progress_id] = True

    @property
    def is_cancelled(self) -> bool:
        """Check if cancellation was requested."""
        return _cancellation_tokens.get(self.progress_id, False)

    def cleanup(self):
        """Remove token from registry."""
        _cancellation_tokens.pop(self.progress_id, None)


@router.get("/feature-tokens")
async def get_feature_tokens(
    stable_id: str = Query(..., description="Stable ID of the GROBID training TEI file"),
    user: dict[str, Any] = Depends(require_authenticated_user),
    db: DatabaseManager = Depends(get_db),
    file_storage: FileStorage = Depends(get_file_storage),
):
    """
    Return the ordered feature token list for a GROBID training file.

    Reads the gold-standard TEI to find the cached extraction (via encodingDesc
    labels), then opens the training ZIP and extracts the first column of each
    non-blank line from the feature file matching the variant's model.

    Used by the frontend sync-check linter so it can perform the comparison
    client-side without a round-trip on every keystroke.

    Returns:
        ``{"status": "ok", "tokens": [...], "count": N, "line_based": bool, "page_starts": [...]}``
        on success (page_starts: indices of the tokens whose row starts a page, segmentation
        documents only), or
        ``{"status": "no_feature_file", "tokens": [], "count": 0}`` when the
        cache is missing or the TEI lacks extractable labels.
    """
    from fastapi_app.lib.repository.file_repository import FileRepository
    from pte_plugin_grobid.cache import get_cache_zip
    from pte_plugin_grobid.config import get_feature_suffix, LINE_BASED_VARIANTS
    from pte_plugin_grobid.layout import SEGMENTATION, feature_page_starts
    from pte_plugin_grobid.sync import extract_feature_tokens, parse_encoding_labels

    file_repo = FileRepository(db)
    file_meta = file_repo.get_file_by_stable_id(stable_id)
    if not file_meta:
        raise HTTPException(status_code=404, detail="File not found")

    content_bytes = file_storage.read_file(file_meta.id, "tei")
    if not content_bytes:
        raise HTTPException(status_code=404, detail="File content not found")

    labels = parse_encoding_labels(content_bytes.decode("utf-8"))
    variant_id = labels.get("variant-id")
    revision = labels.get("revision")
    doc_id = file_meta.doc_id

    if not variant_id or not revision or not doc_id:
        return {"status": "no_feature_file", "tokens": [], "count": 0}

    zip_path = get_cache_zip(doc_id, labels)
    if zip_path is None:
        return {"status": "no_feature_file", "tokens": [], "count": 0}
    suffix = get_feature_suffix(variant_id)

    logger.debug(
        "feature-tokens: stable_id=%s variant_id=%s suffix=%s zip=%s exists=%s",
        stable_id, variant_id, suffix, zip_path, zip_path.exists()
    )

    tokens = extract_feature_tokens(zip_path, suffix)
    if tokens is None:
        return {"status": "no_feature_file", "tokens": [], "count": 0, "line_based": False}

    line_based = variant_id in LINE_BASED_VARIANTS
    page_starts = (feature_page_starts(zip_path, suffix) or []) if variant_id == SEGMENTATION else []
    return {"status": "ok", "tokens": tokens, "count": len(tokens), "line_based": line_based, "page_starts": page_starts}


class InsertPageBreaksRequest(BaseModel):
    """Request of the TEI Wizard's "Insert page breaks" enhancement."""

    stable_id: str = Field(..., description="Stable ID of the segmentation TEI file (identifies its extraction's cached feature file)")
    xml: str = Field(..., description="The document as the editor holds it")


class InsertPageBreaksResponse(BaseModel):
    """The document with page breaks, or why there are none."""

    status: str = Field(..., description="ok, not_segmentation or no_feature_file")
    xml: str | None = Field(default=None, description="The document with the inserted <pb/> elements (status ok)")
    inserted: int = Field(default=0, description="Number of <pb/> elements inserted")
    message: str = Field(default="", description="Explanation if status is not ok")


@router.post("/insert-page-breaks", response_model=InsertPageBreaksResponse)
async def insert_page_breaks(
    request: InsertPageBreaksRequest,
    user: dict[str, Any] = Depends(require_authenticated_user),
    db: DatabaseManager = Depends(get_db),
) -> InsertPageBreaksResponse:
    """
    Insert `<pb n="…"/>` at the page starts of the cached feature file into a segmentation
    document, except where a `<pb/>` already lies near a page start (TEI Wizard enhancement
    "Insert page breaks").
    """
    from fastapi_app.lib.repository.file_repository import FileRepository
    from pte_plugin_grobid.layout import SEGMENTATION, insert_page_breaks_from_feature_file
    from pte_plugin_grobid.sync import parse_encoding_labels

    file_meta = FileRepository(db).get_file_by_stable_id(request.stable_id)
    if not file_meta or not file_meta.doc_id:
        raise HTTPException(status_code=404, detail="File not found")
    if parse_encoding_labels(request.xml).get("variant-id") != SEGMENTATION:
        return InsertPageBreaksResponse(status="not_segmentation", message="Page breaks are only inserted into GROBID segmentation documents.")
    result = insert_page_breaks_from_feature_file(request.xml, file_meta.doc_id)
    if result is None:
        return InsertPageBreaksResponse(status="no_feature_file", message="The feature file of this document's extraction is missing or does not match its lines. \"Reload Feature File\" in the GROBID menu fetches it again.")
    xml, inserted = result
    return InsertPageBreaksResponse(status="ok", xml=xml, inserted=inserted)


@router.get("/reload-feature-file/preview", response_class=HTMLResponse)
async def reload_feature_file_preview(
    xml: str = Query(..., description="Stable ID of the open gold TEI file"),
    user: dict[str, Any] = Depends(require_reviewer_or_admin),
    db: DatabaseManager = Depends(get_db),
    file_storage: FileStorage = Depends(get_file_storage),
):
    """
    Render the reviewer confirmation page for reloading a feature file.

    Read-only: validates the target document and, if the GROBID server is
    reachable, checks its live revision so the page can say whether the
    document's revision label would change - but never calls GROBID's
    training endpoint or writes anything. Loaded in the plugin-result
    iframe (see the 'outputUrl' returned by GrobidPlugin.reload_feature_file).
    """
    from fastapi_app.lib.repository.file_repository import FileRepository
    from pte_plugin_grobid.reload_feature_file import (
        ReloadPreconditionError,
        check_grobid_revision,
        render_precondition_error_html,
        render_preview_html,
        resolve_reload_target,
    )

    file_repo = FileRepository(db)
    try:
        target = resolve_reload_target(file_repo, file_storage, xml, user)
    except ReloadPreconditionError as e:
        return HTMLResponse(content=render_precondition_error_html(str(e)))

    grobid_server_url = get_grobid_server_url()
    if not grobid_server_url:
        return HTMLResponse(
            content=render_preview_html(target, None, "GROBID server not configured.")
        )

    try:
        _, live_revision = check_grobid_revision(grobid_server_url)
        live_error = None
    except RuntimeError as e:
        live_revision = None
        live_error = str(e)

    return HTMLResponse(content=render_preview_html(target, live_revision, live_error))


@router.get("/reload-feature-file/execute", response_class=HTMLResponse)
async def reload_feature_file_execute(
    xml: str = Query(..., description="Stable ID of the open gold TEI file"),
    user: dict[str, Any] = Depends(require_reviewer_or_admin),
    db: DatabaseManager = Depends(get_db),
    file_storage: FileStorage = Depends(get_file_storage),
):
    """
    Perform the feature-file reload and render a result page.

    Called when the reviewer clicks Execute on the confirmation page. Never
    touches the gold-standard TEI's `<text>` content - see
    `reload_feature_file.perform_reload` for exactly what changes.
    """
    from fastapi_app.lib.repository.file_repository import FileRepository
    from pte_plugin_grobid.reload_feature_file import (
        ReloadPreconditionError,
        perform_reload,
        render_error_html,
        render_precondition_error_html,
        render_result_html,
        resolve_reload_target,
    )

    file_repo = FileRepository(db)
    try:
        target = resolve_reload_target(file_repo, file_storage, xml, user)
    except ReloadPreconditionError as e:
        return HTMLResponse(content=render_precondition_error_html(str(e)))

    grobid_server_url = get_grobid_server_url()
    if not grobid_server_url:
        return HTMLResponse(content=render_error_html("GROBID server not configured."))

    try:
        result = await perform_reload(target, grobid_server_url, file_repo, file_storage)
    except RuntimeError as e:
        return HTMLResponse(content=render_error_html(str(e)))

    return HTMLResponse(content=render_result_html(result))


@router.post("/cancel/{progress_id}")
async def cancel_progress(progress_id: str):
    """
    Cancel an in-progress download operation.

    Args:
        progress_id: The progress ID to cancel

    Returns:
        Status of the cancellation request
    """
    if progress_id in _cancellation_tokens:
        _cancellation_tokens[progress_id] = True
        return {"status": "cancelled"}
    return {"status": "not_found"}


@router.get("/diagnostics")
async def grobid_diagnostics(
    user: dict[str, Any] = Depends(require_admin_user),
):
    """
    Run connectivity diagnostics for the GROBID server.

    Performs layered checks to distinguish between general network outages,
    DNS failures, TLS issues, and GROBID-specific problems:

    1. Environment: proxy env vars and configured GROBID URL.
    2. DNS resolution of the GROBID hostname.
    3. TCP connection to port 443.
    4. TLS handshake.
    5. HTTPS GET to a neutral public URL (https://1.1.1.1) to verify general
       internet access independent of the GROBID host.
    6. GROBID /api/health request with the configured timeout.

    Admin role required.
    """
    import platform
    import socket
    import ssl
    import sys
    import time
    from urllib.parse import urlparse

    import requests as _requests

    def _check(label: str, fn):
        """Run fn(), return a result dict with status/duration/detail."""
        t0 = time.monotonic()
        try:
            detail = fn()
            return {"check": label, "status": "ok", "duration_ms": round((time.monotonic() - t0) * 1000), "detail": detail}
        except Exception as exc:
            return {"check": label, "status": "fail", "duration_ms": round((time.monotonic() - t0) * 1000), "detail": str(exc)}

    results: list[dict] = []

    # 1 – Environment
    grobid_url = get_grobid_server_url()
    timeout = get_grobid_server_timeout()
    proxy_vars = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy") if os.environ.get(k)}
    results.append({
        "check": "environment",
        "status": "ok",
        "detail": {
            "python": sys.version,
            "platform": platform.platform(),
            "grobid_url": grobid_url,
            "grobid_timeout_s": timeout,
            "proxy_env": proxy_vars,
        },
    })

    if not grobid_url:
        results.append({"check": "grobid_url_configured", "status": "fail", "detail": "GROBID_SERVER_URL is not set"})
        return {"checks": results}

    parsed = urlparse(grobid_url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # 2 – DNS
    def _dns():
        addrs = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        return [a[4][0] for a in addrs]

    dns_result = _check(f"dns_resolution:{host}", _dns)
    results.append(dns_result)

    # 3 – TCP (only if DNS succeeded)
    if dns_result["status"] == "ok":
        def _tcp():
            with socket.create_connection((host, port), timeout=10) as s:
                return f"connected to {s.getpeername()}"

        tcp_result = _check(f"tcp_connect:{host}:{port}", _tcp)
        results.append(tcp_result)

        # 4 – TLS (only if TCP succeeded and scheme is https)
        if tcp_result["status"] == "ok" and parsed.scheme == "https":
            def _tls():
                ctx = ssl.create_default_context()
                with socket.create_connection((host, port), timeout=10) as raw:
                    with ctx.wrap_socket(raw, server_hostname=host) as tls:
                        cert = tls.getpeercert()
                        subject = {k: v for rdn in ((cert or {}).get("subject") or ()) for k, v in rdn}
                        return {"cipher": tls.cipher(), "subject": subject}

            results.append(_check(f"tls_handshake:{host}:{port}", _tls))

    # 5 – General internet access (neutral URL, bypasses GROBID host issues)
    def _internet():
        resp = _requests.get("https://1.1.1.1", timeout=10, allow_redirects=False)
        return {"status_code": resp.status_code, "elapsed_ms": round(resp.elapsed.total_seconds() * 1000)}

    results.append(_check("internet_access:1.1.1.1", _internet))

    # 6 – GROBID health endpoint
    def _grobid_health():
        health_url = grobid_url.rstrip("/") + "/api/health"
        resp = _requests.get(health_url, timeout=timeout)
        return {"status_code": resp.status_code, "elapsed_ms": round(resp.elapsed.total_seconds() * 1000), "body": resp.text[:500]}

    results.append(_check("grobid_health", _grobid_health))

    return {"checks": results}


def _failed_documents_summary(failures: list[tuple[str, str]], limit: int = 5) -> str:
    """
    Describe the documents for which GROBID returned no training data, for a notification or error detail.

    Args:
        failures: (doc_id, reason) pairs
        limit: Maximum number of document ids to name

    Returns:
        One sentence naming the documents, ending with a period
    """
    doc_ids = [doc_id for doc_id, _ in failures]
    names = ", ".join(doc_ids[:limit])
    if len(doc_ids) > limit:
        names += f" and {len(doc_ids) - limit} more"
    count = len(doc_ids)
    noun = "document" if count == 1 else "documents"
    return f"GROBID could not create training data for {count} {noun} ({names})."


def _skipped_documents_report(failures: list[tuple[str, str]]) -> str:
    """
    Build the skipped-documents.txt report that is added to the training ZIP.

    Args:
        failures: (doc_id, reason) pairs

    Returns:
        Plain-text report with one line per document
    """
    lines = ["GROBID could not create training data for these documents, so they are missing from this package:", ""]
    lines += [f"{doc_id}\t{reason}" for doc_id, reason in failures]
    return "\n".join(lines) + "\n"


@router.get("/download")
async def download_training_package(
    collection: str = Query(..., description="Collection ID"),
    flavor: str = Query("default", description="GROBID processing flavor"),
    force_refresh: bool = Query(False, description="Force re-download from GROBID"),
    no_progress: bool = Query(True, description="Suppress SSE progress events (for programmatic API use)"),
    user: dict[str, Any] = Depends(require_reviewer_or_admin),
    session_id: str = Depends(require_session_id),
    sse_service: SSEService = Depends(get_sse_service),
):
    """
    Download GROBID training package for all documents in a collection as ZIP.

    Only documents with gold standard TEI files are included. The ZIP mirrors
    the grobid-trainer/resources/dataset/ folder structure and can be dropped
    directly into that directory.

    ZIP structure:
        {model}/{flavor}/corpus/tei/{doc_id}.{model_name}.tei.xml  ← gold TEI annotation
        {model}/{flavor}/corpus/raw/{doc_id}           ← GROBID raw feature file

    Args:
        collection: Collection ID to process
        flavor: GROBID processing flavor (e.g. "default", "article/footnotes-refs")
        force_refresh: Force re-download from GROBID (ignore cached data)
        user: Authenticated reviewer or admin
        session_id: Session that receives the SSE progress events

    Returns:
        ZIP file as streaming response
    """
    from fastapi_app.config import get_settings
    from fastapi_app.lib.repository.file_repository import FileRepository
    from fastapi_app.lib.permissions.user_utils import user_has_collection_access
    from pte_plugin_grobid.sync import parse_encoding_labels

    settings = get_settings()

    # Check collection access
    if not user_has_collection_access(user, collection, settings.db_dir):
        raise HTTPException(status_code=403, detail="Access denied to collection")

    try:
        db = get_db()
        file_repo = FileRepository(db)
        file_storage = get_file_storage()

        # Get all PDF files in the collection
        all_files = file_repo.get_files_by_collection(collection)
        pdf_files = [f for f in all_files if f.file_type == "pdf" and not f.deleted]

        if not pdf_files:
            raise HTTPException(status_code=404, detail="No PDF files in collection")

        # Get GROBID server URL
        grobid_server_url = get_grobid_server_url()
        if not grobid_server_url:
            raise HTTPException(status_code=503, detail="GROBID server not configured")

        # Get GROBID version info for cache key
        from pte_plugin_grobid.extractor import GrobidTrainingExtractor
        from pte_plugin_grobid.handlers.training import TrainingHandler
        from pte_plugin_grobid.handlers.training import denormalize_grobid_content
        extractor = GrobidTrainingExtractor()
        training_handler = TrainingHandler()
        _, grobid_revision = extractor._get_grobid_version(grobid_server_url)

        # Set up progress tracking with cancellation (only if progress is enabled)
        progress = None
        cancellation_token = None
        if not no_progress:
            progress = ProgressBar(sse_service, session_id)
            cancel_url = f"/api/plugins/grobid/cancel/{progress.progress_id}"
            cancellation_token = CancellationToken(progress.progress_id)

            progress.show(
                label=f"Processing {len(pdf_files)} documents...",
                cancellable=True,
                cancel_url=cancel_url
            )
            await asyncio.sleep(0)  # Yield to allow SSE event delivery

        try:
            # Generate filename before writing so it can be used as the top-level dir
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            zip_stem = f"{collection}-training-data-{timestamp}"
            zip_filename = f"{zip_stem}.zip"

            # Create output ZIP in memory
            zip_buffer = io.BytesIO()
            documents_processed = 0
            documents_skipped = 0
            # (doc_id, reason) of documents whose training data GROBID did not return
            grobid_failures: list[tuple[str, str]] = []

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, pdf_file in enumerate(pdf_files):
                    # Check for cancellation
                    if cancellation_token and cancellation_token.is_cancelled:
                        send_notification(
                            sse_service, session_id,
                            "Download cancelled", "warning"
                        )
                        progress.hide() # type:ignore
                        cancellation_token.cleanup()
                        raise HTTPException(status_code=499, detail="Download cancelled by user")

                    doc_id = pdf_file.doc_id
                    if not doc_id:
                        documents_skipped += 1
                        continue

                    if progress:
                        progress.set_label(f"Document {i+1}/{len(pdf_files)}: {doc_id[:20]}...")
                        progress.set_value(int((i / len(pdf_files)) * 100))
                        await asyncio.sleep(0)  # Yield to allow SSE event delivery

                    # Get PDF file path
                    pdf_path = file_storage.get_file_path(pdf_file.id, "pdf")
                    if not pdf_path:
                        documents_skipped += 1
                        continue

                    # Get all gold standard training TEI files for this document
                    doc_files = file_repo.get_files_by_doc_id(doc_id)
                    gold_files = [
                        f for f in doc_files
                        if f.file_type == "tei"
                        and f.is_gold_standard
                        and f.variant
                        and f.variant.startswith("grobid.training.")
                    ]

                    # Build map of variant -> gold file content
                    gold_by_variant: dict[str, str] = {}
                    for gold_file in gold_files:
                        content = file_storage.read_file(gold_file.id, "tei")
                        if content and gold_file.variant:
                            gold_by_variant[gold_file.variant] = content.decode("utf-8")

                    # Skip documents without any gold standard files
                    if not gold_by_variant:
                        documents_skipped += 1
                        continue
                    variants_to_process = list(gold_by_variant.keys())

                    # Determine the flavor this document was actually annotated
                    # with (all its variants share one flavor), falling back to
                    # the query-param default when the TEI has no flavor label.
                    # Using the raw query param here instead would silently fetch
                    # the wrong flavor's feature columns for documents annotated
                    # under a non-default flavor (#480).
                    doc_flavor = flavor
                    for variant_content in gold_by_variant.values():
                        tei_flavor = parse_encoding_labels(variant_content).get("flavor")
                        if tei_flavor:
                            doc_flavor = tei_flavor
                            break

                    # The training package of each gold file: the PDF may have been replaced
                    # (text recognition) after a gold file was extracted, whose feature files
                    # then only exist in the cache (see cache.package_lookups()).
                    current_pdf_hash = pdf_file.id  # the editor's file id is the SHA-256 of the content
                    refresh = force_refresh or is_grobid_cache_disabled()
                    packages: dict[tuple[tuple[str, str | None], ...], tuple[str, list[str]]] = {}
                    temp_dirs: list[str] = []
                    variants_written = 0
                    try:
                        for variant in variants_to_process:
                            lookups, may_fetch = package_lookups(grobid_revision, current_pdf_hash, parse_encoding_labels(gold_by_variant[variant]))
                            memo_key = tuple(lookups)
                            if memo_key not in packages:
                                cached = None
                                if not (refresh and may_fetch):
                                    for lookup_revision, lookup_hash in lookups:
                                        cached = check_cache(doc_id, lookup_revision, doc_flavor, pdf_hash=lookup_hash)
                                        if cached:
                                            break
                                if cached:
                                    packages[memo_key] = (cached["temp_dir"], cached["files"])
                                    temp_dirs.append(cached["temp_dir"])
                                elif not may_fetch:
                                    reason = f"{variant}: the PDF was replaced after this version was extracted (e.g. by text recognition), and the feature file of the earlier PDF is not cached"
                                    logger.warning(f"Training download: {doc_id}: {reason}")
                                    grobid_failures.append((doc_id, reason))
                                    continue
                                else:
                                    try:
                                        # Run blocking GROBID fetch in thread pool to allow SSE events
                                        fetched_dir, fetched_files = await asyncio.to_thread(
                                            training_handler._fetch_training_package,
                                            str(pdf_path), grobid_server_url, doc_flavor
                                        )
                                    except Exception as e:
                                        logger.warning(f"Failed to fetch training data for {doc_id}: {e}")
                                        grobid_failures.append((doc_id, str(e)))
                                        break
                                    cache_training_data(doc_id, grobid_revision, doc_flavor, fetched_dir, fetched_files, pdf_hash=current_pdf_hash)
                                    packages[memo_key] = (fetched_dir, fetched_files)
                                    temp_dirs.append(fetched_dir)
                            temp_dir, extracted_files = packages[memo_key]

                            grobid_suffix = variant.removeprefix("grobid.")
                            tei_content = denormalize_grobid_content(gold_by_variant[variant], variant)
                            base_path = _corpus_base_path(tei_content, variant, doc_flavor)
                            model_name = variant.removeprefix("grobid.")

                            # Write gold TEI to corpus/tei/
                            zf.writestr(
                                f"{zip_stem}/{base_path}/tei/{doc_id}.{model_name}.tei.xml",
                                tei_content
                            )

                            # Write raw feature file to corpus/raw/ (no extension)
                            raw_file = next(
                                (f for f in extracted_files
                                 if f.endswith(f".{grobid_suffix}") and not f.endswith(".tei.xml")),
                                None
                            )
                            if raw_file:
                                raw_path = os.path.join(temp_dir, raw_file)
                                if os.path.exists(raw_path):
                                    zf.write(raw_path, f"{zip_stem}/{base_path}/raw/{doc_id}.{grobid_suffix}")
                            variants_written += 1

                    finally:
                        # Clean up temp directories in production mode
                        if settings.application_mode == "production":
                            for temp_dir in temp_dirs:
                                shutil.rmtree(temp_dir, ignore_errors=True)

                    if variants_written:
                        documents_processed += 1
                    else:
                        documents_skipped += 1

                if grobid_failures and documents_processed > 0:
                    zf.writestr(f"{zip_stem}/skipped-documents.txt", _skipped_documents_report(grobid_failures))

            # Hide progress and clean up
            if progress:
                progress.hide()
            if cancellation_token:
                cancellation_token.cleanup()

            if documents_processed == 0:
                if grobid_failures:
                    raise HTTPException(
                        status_code=502,
                        detail=f"No training data could be downloaded: {_failed_documents_summary(grobid_failures)}"
                    )
                raise HTTPException(
                    status_code=404,
                    detail=f"No documents with gold standard training data in collection '{collection}' ({documents_skipped} skipped)."
                )

            zip_buffer.seek(0)

            # Send success notification (only if progress is enabled)
            if not no_progress:
                send_notification(
                    sse_service, session_id,
                    f"Processed {documents_processed} documents ({documents_skipped} skipped)",
                    "success"
                )
                if grobid_failures:
                    send_notification(
                        sse_service, session_id,
                        f"The download is missing documents: {_failed_documents_summary(grobid_failures)} The ZIP lists them in skipped-documents.txt.",
                        "warning"
                    )

            # Return ZIP as streaming response
            return StreamingResponse(
                zip_buffer,
                media_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="{zip_filename}"'
                },
            )

        except HTTPException:
            if progress:
                progress.hide()
            if cancellation_token:
                cancellation_token.cleanup()
            raise
        except Exception as e:
            if progress:
                progress.hide()
            if cancellation_token:
                cancellation_token.cleanup()
            raise e

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to generate GROBID training package: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
