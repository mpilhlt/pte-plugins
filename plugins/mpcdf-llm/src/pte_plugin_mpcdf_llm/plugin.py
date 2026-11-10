"""
MPCDF LLM plugin.

Lets administrators start MPCDF-hosted LLM endpoints. ``initialize()`` creates the
per-worker ProvisioningManager (see manager.py); ``cleanup()`` closes it without
cancelling any endpoint.
"""

import asyncio
import logging
from contextlib import suppress
from typing import Any, Callable

from fastapi_app.plugin_api import Plugin, PluginContext

from . import providers
from .manager import PLUGIN_ID, start_manager, stop_manager


class MpcdfLlmPlugin(Plugin):
    """Plugin that manages MPCDF LLM endpoints."""

    def __init__(self) -> None:
        super().__init__()
        self._watch_task: asyncio.Task[None] | None = None

    @property
    def metadata(self) -> dict[str, Any]:
        """Return plugin metadata."""
        return {
            "id": PLUGIN_ID,
            "name": "MPCDF LLM",
            "description": "Starts MPCDF-hosted LLM endpoints and offers them as free models",
            "category": "inference",
            "version": "1.0.0",
            "required_roles": ["admin"],
            "endpoints": [
                {
                    "name": "manage",
                    "label": "MPCDF LLM endpoints",
                    "description": "Log in to MPCDF and start or stop LLM endpoints",
                    "state_params": [],
                }
            ],
        }

    def get_endpoints(self) -> dict[str, Callable]:
        """Return available endpoints."""
        return {"manage": self.manage}

    async def manage(self, context: PluginContext, params: dict[str, Any]) -> dict[str, str]:
        """Open the management page."""
        return {"outputUrl": f"/api/plugins/{PLUGIN_ID}/manage"}

    async def initialize(self, context: PluginContext) -> None:
        """Create the manager (it opens in the background) and start offering ready endpoints."""
        # The library reads its state file under a file lock every few seconds; filelock
        # logs every acquire and release at DEBUG, which floods a debug-level log.
        logging.getLogger("filelock").setLevel(logging.WARNING)
        await start_manager()
        if self._watch_task is not None:
            self._watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watch_task
        self._watch_task = asyncio.create_task(providers.watch())

    async def cleanup(self) -> None:
        """Stop offering endpoints, close the manager. Endpoints keep running."""
        if self._watch_task is not None:
            self._watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None
        providers.unregister_all()
        await stop_manager()
