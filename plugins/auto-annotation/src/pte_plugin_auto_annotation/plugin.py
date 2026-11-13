"""
Auto-annotation plugin.

Automated annotation of TEI documents by LLMs (see README.md): the "Correct annotations in new
version" action of the Tools menu (frontend extension, routes in correction_routes.py), "Test
model with current document" of the Tools menu (inference category; routes in probe_routes.py), the
model probe and the annotation pipeline CLIs (routes in routes.py).
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
        # Not listed in metadata["endpoints"], so they add no menu items: the extension opens them through
        # backend-plugins.execute(), which shows the page in the plugin result dialog
        return {"probe_results": self.probe_results}

    async def probe_results(self, context: PluginContext, params: dict[str, Any]) -> dict[str, str]:
        """The page of a model test (`run`), or of all model tests; for the plugin result dialog."""
        from .probe_store import ProbeStore
        from .probe_views import export_url, history_url, run_url

        run_id = str(params.get("run") or "")
        if run_id:
            if not ProbeStore.is_run_id(run_id):
                return {"error": f"Unknown model test {run_id!r}"}
            return {"outputUrl": run_url(run_id), "exportUrl": export_url(run_id)}
        return {"outputUrl": history_url()}

    async def initialize(self, context: PluginContext) -> None:
        """Register the frontend extensions."""
        for name in ("auto-annotation.js", "auto-annotation-model-test.js"):
            extension_file = Path(__file__).parent / "extensions" / name
            if extension_file.exists():
                FrontendExtensionRegistry.get_instance().register_extension(extension_file, self.metadata["id"])
            else:
                logger.warning("Auto-annotation frontend extension not found at %s", extension_file)

    async def cleanup(self) -> None:
        """Stop the model tests running in this worker; they are marked as interrupted."""
        from .probe_job import stop_all_jobs

        await stop_all_jobs()
