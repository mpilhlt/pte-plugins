"""
WebDAV sync plugin custom routes.

Provides HTTP endpoints for sync status, execution, conflict listing,
and conflict resolution at /api/plugins/webdav-sync/*.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Depends

from fastapi_app.lib.core.dependencies import (
    get_db,
    get_file_storage,
    get_sse_service,
)
from fastapi_app.plugin_api import FileRepository, require_authenticated_user
from fastapi_app.lib.sync.models import (
    SyncStatusResponse,
    SyncRequest,
    SyncSummary,
    ConflictListResponse,
    ConflictResolution,
)

from .config import get_webdav_config, is_configured
from .service import SyncService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/plugins/webdav-sync", tags=["webdav-sync"])


def _get_sync_service(user: dict, db, file_storage, sse_service) -> SyncService:
    """Build a SyncService instance using plugin config."""
    file_repo = FileRepository(db)
    webdav_config = get_webdav_config()
    return SyncService(
        file_repo=file_repo,
        file_storage=file_storage,
        webdav_config=webdav_config,
        sse_service=sse_service,
        logger=logger,
    )


@router.get("/status", response_model=SyncStatusResponse)
def get_sync_status(
    user: dict[str, Any] = Depends(require_authenticated_user),
    db=Depends(get_db),
    file_storage=Depends(get_file_storage),
    sse_service=Depends(get_sse_service),
) -> SyncStatusResponse:
    """Check if synchronization is needed (O(1) operation)."""
    if not is_configured():
        return SyncStatusResponse(
            needs_sync=False,
            local_version=0,
            remote_version=0,
            unsynced_count=0,
            sync_in_progress=False,
        )

    try:
        sync_service = _get_sync_service(user, db, file_storage, sse_service)
        return sync_service.check_status()
    except Exception as e:
        logger.error(f"Failed to check sync status: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to check sync status: {str(e)}")


@router.post("/sync", response_model=SyncSummary)
def perform_sync(
    request: SyncRequest,
    user: dict[str, Any] = Depends(require_authenticated_user),
    db=Depends(get_db),
    file_storage=Depends(get_file_storage),
    sse_service=Depends(get_sse_service),
) -> SyncSummary:
    """Perform WebDAV synchronization with SSE progress updates."""
    if not is_configured():
        return SyncSummary(skipped=True, message="WebDAV not configured")

    try:
        sync_service = _get_sync_service(user, db, file_storage, sse_service)
        file_repo = FileRepository(db)
        client_id = user.get('username')

        file_repo.set_sync_metadata('sync_in_progress', '1')
        try:
            summary = sync_service.perform_sync(client_id=client_id, force=request.force)
            file_repo.set_sync_metadata('last_sync_summary', summary.model_dump_json())
        finally:
            file_repo.set_sync_metadata('sync_in_progress', '0')

        return summary
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")


@router.get("/conflicts", response_model=ConflictListResponse)
def list_conflicts(
    user: dict[str, Any] = Depends(require_authenticated_user),
    db=Depends(get_db),
    file_storage=Depends(get_file_storage),
    sse_service=Depends(get_sse_service),
) -> ConflictListResponse:
    """List files with sync conflicts."""
    if not is_configured():
        return ConflictListResponse(conflicts=[], total=0)

    try:
        sync_service = _get_sync_service(user, db, file_storage, sse_service)
        return sync_service.get_conflicts()
    except Exception as e:
        logger.error(f"Failed to list conflicts: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list conflicts: {str(e)}")


@router.post("/resolve")
def resolve_conflict(
    resolution: ConflictResolution,
    user: dict[str, Any] = Depends(require_authenticated_user),
    db=Depends(get_db),
    file_storage=Depends(get_file_storage),
    sse_service=Depends(get_sse_service),
) -> dict:
    """Resolve a sync conflict."""
    if not is_configured():
        raise HTTPException(status_code=400, detail="WebDAV not configured")

    try:
        sync_service = _get_sync_service(user, db, file_storage, sse_service)
        return sync_service.resolve_conflict(resolution)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to resolve conflict: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to resolve conflict: {str(e)}")
