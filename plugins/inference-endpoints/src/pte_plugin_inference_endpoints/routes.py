"""
Routes of the inference endpoints plugin: the management page and its admin-only JSON API.

- GET    /api/plugins/inference-endpoints/manage            management page (shown in the plugin dialog)
- GET    /api/plugins/inference-endpoints/endpoints         list the endpoints
- POST   /api/plugins/inference-endpoints/endpoints         add an endpoint
- DELETE /api/plugins/inference-endpoints/endpoints/{id}    remove an endpoint and its shared key
- POST   /api/plugins/inference-endpoints/test              list the models of an unsaved endpoint
- POST   /api/plugins/inference-endpoints/endpoints/{id}/test  list the models of a stored endpoint
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from fastapi_app.lib.plugins.plugin_tools import load_plugin_html
from fastapi_app.lib.secrets import MissingSecretError, SecretsError, require_user_secret
from fastapi_app.plugin_api import LLMProviderError, require_admin_user

from .keys import delete_shared_key, get_shared_key, set_shared_key
from .llm_provider import EndpointLLMProvider
from .plugin import PLUGIN_ID, get_store, sync_providers
from .store import Endpoint, KeyMode, slugify
from .url_policy import EndpointURLError, check_url, normalize_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix=f"/api/plugins/{PLUGIN_ID}", tags=[PLUGIN_ID])


class EndpointInput(BaseModel):
    """An endpoint as entered in the form."""

    name: str = Field(..., min_length=1, max_length=80)
    base_url: str = Field(..., min_length=1, max_length=500)
    key_mode: KeyMode = "none"
    api_key: str | None = Field(None, description="The shared key (key mode 'shared'); never returned")
    models: list[str] = Field(default_factory=list, description="Model ids; empty lists them from the endpoint")
    vision: bool = False
    free: bool = False
    timeout: int = Field(600, ge=10, le=3600)


class EndpointResponse(BaseModel):
    """A stored endpoint."""

    id: str
    provider_id: str
    name: str
    base_url: str
    key_mode: KeyMode
    has_key: bool
    models: list[str]
    vision: bool
    free: bool
    timeout: int
    created_by: str


class TestResponse(BaseModel):
    """The result of a connection test."""

    ok: bool
    models: list[str] = []
    message: str = ""


def _response(endpoint: Endpoint) -> EndpointResponse:
    has_key = False
    if endpoint.key_mode == "shared":
        try:
            has_key = bool(get_shared_key(endpoint))
        except SecretsError:
            has_key = False
    return EndpointResponse(
        id=endpoint.id, provider_id=endpoint.provider_id, name=endpoint.name, base_url=endpoint.base_url,
        key_mode=endpoint.key_mode, has_key=has_key, models=list(endpoint.models), vision=endpoint.vision,
        free=endpoint.free, timeout=endpoint.timeout, created_by=endpoint.created_by,
    )


def _to_endpoint(body: EndpointInput, created_by: str = "") -> Endpoint:
    """Validate the input; raises HTTPException 400 for an invalid or forbidden URL."""
    try:
        base_url = normalize_base_url(body.base_url)
        check_url(base_url)
    except EndpointURLError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if body.key_mode == "shared" and not (body.api_key or "").strip():
        raise HTTPException(status_code=400, detail="Enter the API key, or choose another key mode")
    models = tuple(dict.fromkeys(m.strip() for m in body.models if m.strip()))
    return Endpoint(
        id=slugify(body.name), name=body.name.strip(), base_url=base_url, key_mode=body.key_mode,
        models=models, vision=body.vision, free=body.free, timeout=body.timeout, created_by=created_by,
    )


async def _test(provider: EndpointLLMProvider) -> TestResponse:
    try:
        models = await asyncio.to_thread(provider.fetch_models)
    except LLMProviderError as e:
        return TestResponse(ok=False, message=str(e))
    missing = [m for m in provider.endpoint.models if m not in models]
    if missing:
        return TestResponse(ok=False, models=models, message=f"The endpoint does not offer {', '.join(missing)}")
    return TestResponse(ok=True, models=models, message=f"Connected; {len(models)} model(s) available")


@router.get("/manage", response_class=HTMLResponse)
async def manage_page(
    user: dict[str, Any] = Depends(require_admin_user),
) -> HTMLResponse:
    """The management page; it loads the endpoints through the JSON routes."""
    return HTMLResponse(load_plugin_html(__file__, "manage.html"))


@router.get("/endpoints", response_model=list[EndpointResponse])
def list_endpoints(user: dict[str, Any] = Depends(require_admin_user)) -> list[EndpointResponse]:
    """List the configured endpoints (without keys)."""
    return [_response(e) for e in get_store().load()]


@router.post("/endpoints", response_model=EndpointResponse)
def add_endpoint(body: EndpointInput, user: dict[str, Any] = Depends(require_admin_user)) -> EndpointResponse:
    """Add an endpoint and register its provider; a shared key is stored in the secret store."""
    store = get_store()
    endpoint = store.add(_to_endpoint(body, created_by=str(user.get("username", ""))))
    if endpoint.key_mode == "shared":
        try:
            set_shared_key(endpoint, (body.api_key or "").strip())
        except SecretsError as e:
            store.remove(endpoint.id)
            raise HTTPException(status_code=500, detail=f"Could not store the API key: {e}") from e
    sync_providers(store)
    logger.info(f"Inference endpoint '{endpoint.name}' ({endpoint.base_url}) added by {user.get('username')}")
    return _response(endpoint)


@router.delete("/endpoints/{endpoint_id}")
def delete_endpoint(endpoint_id: str, user: dict[str, Any] = Depends(require_admin_user)) -> dict[str, str]:
    """Remove an endpoint, its shared key and its provider."""
    store = get_store()
    endpoint = store.remove(endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail=f"No inference endpoint '{endpoint_id}'")
    try:
        delete_shared_key(endpoint)
    except SecretsError as e:
        logger.warning(f"Could not delete the API key of inference endpoint '{endpoint.name}': {e}")
    sync_providers(store)
    logger.info(f"Inference endpoint '{endpoint.name}' removed by {user.get('username')}")
    return {"status": "deleted", "id": endpoint.id}


@router.post("/test", response_model=TestResponse)
async def test_new_endpoint(body: EndpointInput, user: dict[str, Any] = Depends(require_admin_user)) -> TestResponse:
    """List the models of an endpoint that is not saved yet, with the entered key."""
    endpoint = _to_endpoint(body.model_copy(update={"key_mode": "user" if body.api_key else "none"}))
    provider = EndpointLLMProvider(endpoint, (body.api_key or "").strip() or None)
    return await _test(provider)


@router.post("/endpoints/{endpoint_id}/test", response_model=TestResponse)
async def test_endpoint(endpoint_id: str, user: dict[str, Any] = Depends(require_admin_user)) -> TestResponse:
    """List the models of a stored endpoint, with the shared key or the admin's own key."""
    endpoint = get_store().get(endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail=f"No inference endpoint '{endpoint_id}'")
    provider = EndpointLLMProvider(endpoint)
    if endpoint.key_mode == "user":
        try:
            provider = provider.with_api_key(require_user_secret(user.get("username"), endpoint.secret_service))
        except MissingSecretError:
            return TestResponse(ok=False, message="This endpoint uses per-user keys; store your own key under API Keys to test it")
    return await _test(provider)
