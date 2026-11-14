"""
Routes for the Metadata Extraction plugin.

Provides an endpoint to extract bibliographic metadata from a PDF
via DOI lookup or LLM-based extraction, and an admin endpoint to backfill
missing metadata for all documents of a collection.
"""

import asyncio
import logging
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse

from fastapi_app.lib.core.database import DatabaseManager
from fastapi_app.lib.core.dependencies import (
    get_db,
    get_file_storage,
    get_sse_service,
    require_session_id,
)
from fastapi_app.lib.sse.sse_service import SSEService
from fastapi_app.plugin_api import FileStorage, require_admin_user, require_authenticated_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plugins/metadata-extraction", tags=["metadata-extraction"])


@router.get("/extract")
async def extract_metadata(
    stable_id: str = Query(..., description="PDF stable ID"),
    doi: str | None = Query(None, description="DOI string (optional)"),
    user: dict[str, Any] = Depends(require_authenticated_user),
):
    """
    Extract bibliographic metadata for a PDF document.

    Tries DOI lookup first if doi is provided, otherwise falls back
    to LLM-based extraction via the extraction service.

    Returns:
        JSON with metadata fields (title, authors, date, publisher, etc.)
    """
    from fastapi_app.lib.services.metadata_extraction import get_metadata_for_document

    # Extract metadata
    metadata = await get_metadata_for_document(
        doi=doi,
        stable_id=stable_id,
    )

    return metadata


def _require_collection(collection: str) -> None:
    """
    Check that the collection exists.

    Raises:
        HTTPException: 404 if the collection does not exist
    """
    from fastapi_app.config import get_settings
    from fastapi_app.lib.utils.collection_utils import validate_collection

    if not validate_collection(collection, get_settings().db_dir):
        raise HTTPException(status_code=404, detail=f"Collection not found: {collection}")


@router.get("/backfill/options", response_class=HTMLResponse)
async def backfill_options(
    collection: str = Query(..., description="ID of the collection whose documents are backfilled"),
    user: dict[str, Any] = Depends(require_admin_user),
) -> HTMLResponse:
    """
    Options dialog for the collection backfill (admin only), opened from the plugin menu entry.
    """
    import html

    from fastapi_app.lib.plugins.plugin_tools import load_plugin_html

    _require_collection(collection)
    page = load_plugin_html(__file__, "backfill.html").replace("__COLLECTION__", html.escape(collection))
    return HTMLResponse(content=page)


@router.post("/backfill")
async def backfill_collection_metadata(
    collection: str = Query(..., description="ID of the collection whose documents are backfilled"),
    dry_run: bool = Query(False, description="Report what would be added without saving"),
    use_grobid: bool = Query(True, description="Use GROBID header extraction as fallback if available"),
    limit: int | None = Query(None, ge=1, description="Maximum number of documents to process"),
    user: dict[str, Any] = Depends(require_admin_user),
    session_id: str = Depends(require_session_id),
    sse_service: SSEService = Depends(get_sse_service),
    db: DatabaseManager = Depends(get_db),
    file_storage: FileStorage = Depends(get_file_storage),
) -> dict[str, Any]:
    """
    Backfill missing metadata for all documents of a collection (admin only).

    Adds data points that do not exist yet in the TEI headers (sourceDesc/biblStruct and
    profileDesc/langUsage) and the PDF's doc_metadata. Existing values are never overwritten.
    Metadata comes from CrossRef/DataCite if a DOI is known, otherwise from GROBID header
    extraction if the GROBID plugin is active and its server is ready. The document language
    is taken from the registry metadata or predicted from the PDF/TEI text.
    TEI files locked by another session are skipped.

    Returns:
        Report with keys collection, dry_run, summary, sources and documents
    """
    from fastapi_app.config import get_settings
    from fastapi_app.lib.repository.file_repository import FileRepository
    from fastapi_app.lib.sse.sse_utils import ProgressBar, send_notification
    from pte_plugin_metadata_extraction.backfill import backfill_collection
    from pte_plugin_metadata_extraction.grobid_header import (
        fetch_grobid_header_metadata,
        get_live_grobid_server_url,
    )

    _require_collection(collection)
    settings = get_settings()

    grobid_fetcher = None
    grobid_server_url = await asyncio.to_thread(get_live_grobid_server_url) if use_grobid else None
    if grobid_server_url:
        grobid_fetcher = partial(fetch_grobid_header_metadata, server_url=grobid_server_url)

    progress = ProgressBar(sse_service, session_id)
    progress.show(label=f"Backfilling metadata in collection {collection}...")
    await asyncio.sleep(0)

    def on_progress(current: int, total: int, label: str) -> None:
        progress.set_label(label)
        if total > 0:
            progress.set_value(int(current / total * 100))

    try:
        report = await backfill_collection(
            collection_id=collection,
            file_repo=FileRepository(db),
            file_storage=file_storage,
            session_id=session_id,
            db_dir=settings.db_dir,
            grobid_fetcher=grobid_fetcher,
            dry_run=dry_run,
            limit=limit,
            progress_callback=on_progress,
        )
    finally:
        progress.hide()

    report["grobid_available"] = grobid_fetcher is not None
    summary = report["summary"]
    if not dry_run:
        send_notification(
            sse_service,
            session_id,
            f"Backfilled metadata for {summary['documents_updated']} of {summary['documents']} documents in {collection}",
            "success" if summary["errors"] == 0 else "warning",
        )
    return report
