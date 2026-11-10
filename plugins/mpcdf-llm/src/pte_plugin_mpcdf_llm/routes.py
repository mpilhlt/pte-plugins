"""
Routes of the MPCDF LLM plugin.

- GET /api/plugins/mpcdf-llm/manage   management page (admin only)
- /api/plugins/mpcdf-llm/admin/...    the library's admin router (admin only)

The library's read router is deliberately not mounted: it returns API keys.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from mpcdf_llm.web import admin_router

from fastapi_app.lib.core.dependencies import get_auth_manager
from fastapi_app.lib.permissions.acl_utils import user_is_admin
from fastapi_app.lib.plugins.plugin_tools import load_plugin_html
from fastapi_app.plugin_api import get_session_manager, get_settings, require_admin_user

from .manager import PLUGIN_ID, get_manager

router = APIRouter(prefix=f"/api/plugins/{PLUGIN_ID}", tags=[PLUGIN_ID])

router.include_router(admin_router(get_manager), prefix="/admin", dependencies=[Depends(require_admin_user)])


@router.get("/manage", response_class=HTMLResponse)
async def manage_page(
    session_id: str | None = Query(None),
    x_session_id: str | None = Header(None, alias="X-Session-ID"),
    session_manager=Depends(get_session_manager),
    auth_manager=Depends(get_auth_manager),
) -> HTMLResponse:
    """The management page; it talks to the admin routes."""
    session_value = x_session_id or session_id
    if not session_value or not session_manager.is_session_valid(session_value, get_settings().session_timeout):
        raise HTTPException(status_code=401, detail="Authentication required")
    user = auth_manager.get_user_by_session_id(session_value, session_manager)
    if not user or not user_is_admin(user):
        raise HTTPException(status_code=403, detail="Admin role required")
    return HTMLResponse(load_plugin_html(__file__, "manage.html"))
