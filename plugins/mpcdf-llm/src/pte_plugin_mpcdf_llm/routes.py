"""
Routes of the MPCDF LLM plugin.

Every route that uses an MPCDF session acts on the requesting user's own manager; nobody can
use another user's session.

- GET /api/plugins/mpcdf-llm/manage    management page (authenticated)
- GET /api/plugins/mpcdf-llm/context   the page's user context (authenticated)
- GET /api/plugins/mpcdf-llm/managed   preset and until of the user's managed endpoints (authenticated)
- /api/plugins/mpcdf-llm/my/...        the library's admin router on the user's manager (authenticated;
                                       changing the preset catalog is admin only)
- POST /api/plugins/mpcdf-llm/presets/import  save imported presets into the catalog (admin only)
- GET /api/plugins/mpcdf-llm/overview  all users' endpoints from their state files (admin only)
- DELETE /api/plugins/mpcdf-llm/users/{username}/session  remove a user's stored session (admin only)

The library's read router is deliberately not mounted: it returns API keys.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from mpcdf_llm.manager import ProvisioningManager
from mpcdf_llm.presets import Preset
from mpcdf_llm.storage import FileStateStore
from mpcdf_llm.web import admin_router
from pydantic import BaseModel, ValidationError

from fastapi_app.lib.permissions.acl_utils import user_is_admin
from fastapi_app.lib.plugins.plugin_tools import load_plugin_html
from fastapi_app.lib.secrets import get_secret_store
from fastapi_app.plugin_api import require_admin_user, require_authenticated_user

from . import manager as manager_module
from .manager import PLUGIN_ID, SESSION_SECRET, get_user_manager

BASE = f"/api/plugins/{PLUGIN_ID}"

router = APIRouter(prefix=BASE, tags=[PLUGIN_ID])


def _catalog_writes_for_admins(request: Request, user: dict[str, Any] = Depends(require_authenticated_user)) -> None:
    """Presets are an installation-wide catalog: only admins change it."""
    if request.method in ("PUT", "DELETE") and request.url.path.startswith(f"{BASE}/my/presets/") and not user_is_admin(user):
        raise HTTPException(status_code=403, detail="Only administrators can change the presets")


router.include_router(
    admin_router(get_user_manager),
    prefix="/my",
    dependencies=[Depends(require_authenticated_user), Depends(_catalog_writes_for_admins)],
)


async def _own_manager(user: dict[str, Any] = Depends(require_authenticated_user)) -> ProvisioningManager:
    """The requesting user's manager for this module's routes (503 when not initialized)."""
    try:
        return await get_user_manager(user)
    except LookupError:
        raise HTTPException(status_code=503, detail="MPCDF LLM is not initialized") from None


class PageContext(BaseModel):
    """What the management page needs to know about the requesting user."""

    username: str
    is_admin: bool
    hf_token_stored: bool


@router.get("/context")
async def page_context(user: dict[str, Any] = Depends(require_authenticated_user)) -> PageContext:
    """The requesting user's name, role and whether they stored a Hugging Face token."""
    username = user.get("username") or ""
    return PageContext(
        username=username,
        is_admin=user_is_admin(user),
        hf_token_stored=bool(username) and manager_module.has_hf_token(username),
    )


@router.get("/managed")
async def managed_endpoints(manager: ProvisioningManager = Depends(_own_manager)) -> list[dict[str, Any]]:
    """Which preset each of the user's library-managed endpoints belongs to, and its keep-alive cutoff.

    Only these fields: no follower ids, keys or tokens."""
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
    """Save presets from an export file into the catalog.

    This needs no MPCDF session. Unlike ``PUT /my/presets/{name}``, it checks only the preset
    model, not the live options of the service (machines, GPU types); a preset the service does
    not support fails when it is started. Each preset is saved on its own; the invalid ones are
    reported in ``errors`` by name."""
    catalog = manager_module.catalog()
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
        catalog.put(preset)
        saved.append(preset.name)
    return PresetImportResult(saved=saved, errors=errors)


class OverviewEndpoint(BaseModel):
    """One managed endpoint of a user, as recorded in the user's state file."""

    endpoint_id: int
    preset: str
    ready: bool
    until: str | None
    submitted_at: str
    last_activity: str


class OverviewUser(BaseModel):
    """A user of the plugin with their managed endpoints."""

    username: str
    has_session: bool
    endpoints: list[OverviewEndpoint]


@router.get("/overview", dependencies=[Depends(require_admin_user)])
async def overview() -> list[OverviewUser]:
    """All users' managed endpoints, read from their state files without using their sessions."""
    result: list[OverviewUser] = []
    for username in manager_module.known_users():
        entries = FileStateStore(manager_module.user_dir(username)).managed().values()
        result.append(OverviewUser(
            username=username,
            has_session=manager_module.has_stored_session(username),
            endpoints=[
                OverviewEndpoint(
                    endpoint_id=m.endpoint_id,
                    preset=m.preset,
                    ready=m.ready_at is not None,
                    until=m.until.isoformat() if m.until else None,
                    submitted_at=m.submitted_at.isoformat(),
                    last_activity=m.last_activity.isoformat(),
                )
                for m in sorted(entries, key=lambda e: e.endpoint_id)
                if m.replaced_by is None
            ],
        ))
    return result


@router.delete("/users/{username}/session", status_code=204)
async def remove_user_session(username: str, admin: dict[str, Any] = Depends(require_admin_user)) -> Response:
    """Remove a user's stored MPCDF session: the server no longer acts for the user at MPCDF.

    The user's endpoints keep running until they expire or idle out at MPCDF; other workers drop
    their manager of the user within one watch interval."""
    get_secret_store().delete(username, SESSION_SECRET)
    await manager_module.drop_manager(username)
    return Response(status_code=204)


@router.get("/manage", response_class=HTMLResponse)
async def manage_page(
    user: dict[str, Any] = Depends(require_authenticated_user),
) -> HTMLResponse:
    """The management page; it talks to the routes above."""
    return HTMLResponse(load_plugin_html(__file__, "manage.html"))
