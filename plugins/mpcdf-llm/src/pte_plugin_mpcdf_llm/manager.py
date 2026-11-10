"""
Per-worker lifecycle of the MPCDF ProvisioningManager.

Each worker process owns one manager, created in ``initialize()`` and closed in
``cleanup()``. The routers of the library receive ``get_manager`` and so survive
initialize/cleanup/initialize cycles (also under a new event loop).
"""

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from mpcdf_llm.manager import ProvisioningManager

from fastapi_app.plugin_api import get_plugin_config, get_settings

logger = logging.getLogger(__name__)

PLUGIN_ID = "mpcdf-llm"
DEFAULT_BASE_URL = "https://llm.mpcdf.mpg.de"

_manager: ProvisioningManager | None = None
_task: asyncio.Task[None] | None = None


def data_dir() -> Path:
    """Directory holding the manager's state (session, presets, endpoints)."""
    return Path(get_settings().plugins_data_dir) / PLUGIN_ID


def base_url() -> str:
    """Base URL of the MPCDF LLM service (config ``plugin.mpcdf-llm.base-url`` or MPCDF_LLM_BASE_URL)."""
    return get_plugin_config(
        "plugin.mpcdf-llm.base-url",
        "MPCDF_LLM_BASE_URL",
        default=DEFAULT_BASE_URL,
        description="Base URL of the MPCDF LLM service",
    )


def hf_token() -> str | None:
    """Hugging Face token for gated models (config ``plugin.mpcdf-llm.hf-token`` or HF_TOKEN); read per call."""
    return (
        get_plugin_config(
            "plugin.mpcdf-llm.hf-token",
            "HF_TOKEN",
            masked=True,
            description="Hugging Face token passed to endpoints that serve gated models",
        )
        or None
    )


def get_manager() -> ProvisioningManager:
    """The current manager; raises LookupError when none is current (mapped to 503 by the library)."""
    if _manager is None:
        raise LookupError("MPCDF LLM manager is not initialized")
    return _manager


async def _run(manager: ProvisioningManager) -> None:
    """Restore a stored session (may use the network), then start the supervisor.

    The supervisor starts even if open() fails: the library restores lazily.
    Only the exception type is logged, as messages and tracebacks may carry secrets.
    """
    try:
        await manager.open()
    except Exception as e:
        logger.warning("Could not open the MPCDF LLM manager: %s", type(e).__name__)
    try:
        manager.start_supervisor()
    except Exception as e:
        logger.warning("Could not start the MPCDF LLM supervisor: %s", type(e).__name__)


async def start_manager() -> ProvisioningManager:
    """
    Create the manager, make it current and open it in a background task.

    Idempotent-safe: if a manager is already current it is stopped first, so
    exactly one manager (and one supervisor) exists per worker.
    """
    global _manager, _task
    if _manager is not None:
        await stop_manager()
    manager = ProvisioningManager(data_dir=data_dir(), base_url=base_url(), hf_token=hf_token)
    _manager = manager
    _task = asyncio.create_task(_run(manager))
    return manager


async def stop_manager() -> None:
    """Cancel the open task, close the manager and clear it. Never stops endpoints."""
    global _manager, _task
    task, manager = _task, _manager
    try:
        if task is not None:
            task.cancel()
            # gather (not suppress(CancelledError)) so cancellation of this coroutine propagates
            await asyncio.gather(task, return_exceptions=True)
        if manager is not None:
            await manager.close()
    finally:
        _task = None
        _manager = None
