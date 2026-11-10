"""
Routes of the MPCDF LLM plugin.

- GET /api/plugins/mpcdf-llm/manage   management page (admin only)
- GET /api/plugins/mpcdf-llm/managed  preset and until of managed endpoints (admin only)
- POST /api/plugins/mpcdf-llm/presets/import  save imported presets without an MPCDF session (admin only)
- /api/plugins/mpcdf-llm/admin/...    the library's admin router (admin only)

The library's read router is deliberately not mounted: it returns API keys.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from mpcdf_llm.presets import Preset
from mpcdf_llm.web import admin_router
from pydantic import BaseModel, ValidationError

from fastapi_app.lib.core.dependencies import get_auth_manager
from fastapi_app.lib.permissions.acl_utils import user_is_admin
from fastapi_app.lib.plugins.plugin_tools import load_plugin_html
from fastapi_app.plugin_api import get_session_manager, get_settings, require_admin_user

from .manager import PLUGIN_ID, get_manager

router = APIRouter(prefix=f"/api/plugins/{PLUGIN_ID}", tags=[PLUGIN_ID])

router.include_router(admin_router(get_manager), prefix="/admin", dependencies=[Depends(require_admin_user)])


@router.get("/managed", dependencies=[Depends(require_admin_user)])
async def managed_endpoints() -> list[dict]:
    """Which preset each library-managed endpoint belongs to, and its keep-alive cutoff.

    Only these fields: no follower ids, keys or tokens."""
    try:
        manager = get_manager()
    except LookupError:
        raise HTTPException(status_code=503, detail="MPCDF LLM is not initialized") from None
    return [
        {
            "endpoint_id": m.endpoint_id,
            "preset": m.preset,
            "presets": sorted(m.served),
            "until": m.until.isoformat() if m.until else None,
        }
        for m in manager.state.managed().values()
    ]


class PresetImportResult(BaseModel):
    """Outcome of a preset import: the saved names and the rejected ones with the reason."""

    saved: list[str]
    errors: dict[str, str]


def _validation_message(error: ValidationError) -> str:
    """One line per invalid field, e.g. ``gpus: Input should be a valid integer``."""
    return "; ".join(
        f"{'.'.join(str(part) for part in e['loc']) or 'preset'}: {e['msg']}" for e in error.errors()
    )


@router.post("/presets/import", dependencies=[Depends(require_admin_user)])
async def import_presets(presets: list[dict[str, object]]) -> PresetImportResult:
    """Save presets from an export file.

    Presets live in the plugin's data directory, so this needs no MPCDF session. Unlike
    ``PUT /admin/presets/{name}``, it checks only the preset model, not the live options of the
    service (machines, GPU types); a preset the service does not support fails when it is started.
    Each preset is saved on its own; the invalid ones are reported in ``errors`` by name."""
    try:
        manager = get_manager()
    except LookupError:
        raise HTTPException(status_code=503, detail="MPCDF LLM is not initialized") from None
    saved: list[str] = []
    errors: dict[str, str] = {}
    for index, data in enumerate(presets):
        name = data.get("name")
        key = name if isinstance(name, str) and name else f"#{index + 1}"
        try:
            preset = Preset.model_validate(data)
        except ValidationError as e:
            errors[key] = _validation_message(e)
            continue
        manager.presets.put(preset)
        saved.append(preset.name)
    return PresetImportResult(saved=saved, errors=errors)


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
