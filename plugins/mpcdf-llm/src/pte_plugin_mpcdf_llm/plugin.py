"""
MPCDF LLM plugin.

Lets administrators start MPCDF-hosted LLM endpoints. ``initialize()`` creates the
per-worker ProvisioningManager (see manager.py); ``cleanup()`` closes it without
cancelling any endpoint.
"""

from typing import Any, Callable

from fastapi_app.plugin_api import Plugin, PluginContext

from .manager import PLUGIN_ID, start_manager, stop_manager


class MpcdfLlmPlugin(Plugin):
    """Plugin that manages MPCDF LLM endpoints."""

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
        """Create the manager; it opens in the background."""
        await start_manager()

    async def cleanup(self) -> None:
        """Close the manager. Endpoints keep running."""
        await stop_manager()
