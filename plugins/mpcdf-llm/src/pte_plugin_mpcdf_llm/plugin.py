"""
MPCDF LLM plugin.

Lets every user start MPCDF-hosted LLM endpoints with their own MPCDF account.
``initialize()`` starts serving the users' ProvisioningManagers in this worker (see
manager.py); ``cleanup()`` closes them without cancelling any endpoint.
"""

import asyncio
import logging
from contextlib import suppress
from typing import Any, Callable

from fastapi_app.lib.secrets import register_secret_service, unregister_secret_service
from fastapi_app.plugin_api import Plugin, PluginContext

from . import manager, providers
from .manager import HF_TOKEN_SECRET, PLUGIN_ID


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
            "description": "Starts MPCDF-hosted LLM endpoints with the user's own MPCDF account and offers them as models",
            "category": "inference",
            "version": "1.0.0",
            "required_roles": [],
            "endpoints": [
                {
                    "name": "manage",
                    "label": "My MPCDF endpoints",
                    "description": "Log in to MPCDF with your own account and start or stop LLM endpoints",
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
        """Serve the users' managers (they open in the background) and offer their ready endpoints."""
        # The library reads its state file under a file lock every few seconds, and its
        # protocol module logs every websocket message; both flood a debug-level log.
        logging.getLogger("filelock").setLevel(logging.WARNING)
        logging.getLogger("mpcdf_llm.protocol").setLevel(logging.INFO)
        register_secret_service(
            HF_TOKEN_SECRET,
            "Hugging Face read token",
            description="Read access token of your Hugging Face account. Needed to start MPCDF endpoints that serve gated models.",
            help_url="https://huggingface.co/settings/tokens",
        )
        await manager.start()
        if self._watch_task is not None:
            self._watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watch_task
        self._watch_task = asyncio.create_task(providers.watch())

    async def cleanup(self) -> None:
        """Stop offering endpoints, close the managers. Endpoints keep running."""
        if self._watch_task is not None:
            self._watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None
        providers.unregister_all()
        await manager.stop()
        unregister_secret_service(HF_TOKEN_SECRET)
