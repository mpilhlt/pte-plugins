"""
Auto-annotation plugin.

Automated annotation of TEI documents by LLMs (see README.md): the "Correct annotations in new
version" action of the Tools menu (frontend extension, routes in correction_routes.py), the model
probe and the annotation pipeline CLIs (routes in routes.py).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi_app.plugin_api import FrontendExtensionRegistry, Plugin, PluginContext

logger = logging.getLogger(__name__)


class AutoAnnotationPlugin(Plugin):
    """Corrects the annotations of a document with an LLM; routes for evaluating and running LLM annotation."""

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "id": "auto-annotation",
            "name": "Auto-annotation",
            "description": "Corrects the annotations of a document with an LLM in a new version; model probe and annotation pipeline",
            "category": "annotation",
            "version": "0.1.0",
            "required_roles": ["annotator", "reviewer"],
            "endpoints": [],
        }

    def get_endpoints(self) -> dict[str, Callable]:
        return {}

    async def initialize(self, context: PluginContext) -> None:
        """Register the frontend extension."""
        extension_file = Path(__file__).parent / "extensions" / "auto-annotation.js"
        if extension_file.exists():
            FrontendExtensionRegistry.get_instance().register_extension(extension_file, self.metadata["id"])
        else:
            logger.warning("Auto-annotation frontend extension not found at %s", extension_file)

    async def cleanup(self) -> None:
        pass
