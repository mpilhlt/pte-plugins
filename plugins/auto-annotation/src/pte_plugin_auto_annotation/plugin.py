"""
Auto-annotation plugin.

Building blocks for an automated first annotation of GROBID training data by LLMs (see
README.md). This version provides the model probe: routes the probe CLI (probe.py) uses to read
gold documents and to run completions with the models configured in the app.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi_app.plugin_api import Plugin, PluginContext


class AutoAnnotationPlugin(Plugin):
    """Routes for evaluating LLMs at correcting annotations against gold versions."""

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "id": "auto-annotation",
            "name": "Auto-annotation",
            "description": "Evaluates LLMs at correcting GROBID training annotations against gold versions (model probe)",
            "category": "annotation",
            "version": "0.1.0",
            "required_roles": ["reviewer"],
            "endpoints": [],
        }

    def get_endpoints(self) -> dict[str, Callable]:
        return {}

    async def initialize(self, context: PluginContext) -> None:
        pass

    async def cleanup(self) -> None:
        pass
