"""
GROBID extractor plugin.

Registers the GrobidTrainingExtractor with the extraction registry.
Provides endpoints for reviewers to download and import complete training packages.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from fastapi_app.plugin_api import (
    ExtractorRegistry,
    Plugin,
    PluginContext,
    register_document_rules_provider,
    register_gc_task,
    register_schema_config,
    unregister_document_rules_provider,
    unregister_gc_task,
    unregister_schema_config,
)
from pte_plugin_grobid.annotation_rules import GrobidRulesProvider
from .config import has_layout_api, init_plugin_config
from .layout import SEGMENTATION, segmentation_layout

from .cache_gc import collect_cache_garbage
from .extractor import GrobidTrainingExtractor

if TYPE_CHECKING:
    from fastapi_app.plugins.tei_wizard.plugin import TeiWizardPlugin

logger = logging.getLogger(__name__)

GROBID_RNG_URL = "https://raw.githubusercontent.com/kermitt2/grobid/refs/heads/master/grobid-home/schemas/rng/Grobid.rng"


def register_corrected_pdf_handler_if_available(plugin_id: str) -> bool:
    """Register the corrected-PDF handler of segmentation documents if the editor has the API (plugin API 1.8); whether it has."""
    try:
        from fastapi_app.plugin_api import register_corrected_pdf_handler
    except ImportError:
        return False
    from .text_corrections import corrected_pdf_handler

    register_corrected_pdf_handler(SEGMENTATION, plugin_id, corrected_pdf_handler)
    return True


def unregister_corrected_pdf_handler_if_available() -> None:
    """Unregister the corrected-PDF handler of segmentation documents if the editor has the API (plugin API 1.8)."""
    try:
        from fastapi_app.plugin_api import unregister_corrected_pdf_handler
    except ImportError:
        return
    unregister_corrected_pdf_handler(SEGMENTATION)


class GrobidPlugin(Plugin):
    """Plugin that provides GROBID-based extraction."""

    # the TEI Wizard the enhancements are registered with
    _wizard: "TeiWizardPlugin | None" = None

    def __init__(self) -> None:
        init_plugin_config()

    @property
    def metadata(self) -> dict[str, Any]:
        """Return plugin metadata."""
        return {
            "id": "grobid",
            "name": "GROBID Extractor",
            "description": "Extract training data using GROBID server",
            "category": "extractor",
            "version": "1.0.0",
            "required_roles": ["user"],
            "endpoints": [
                {
                    "name": "download_training",
                    "label": "Download GROBID Training Data",
                    "description": "Download complete GROBID training package for a collection",
                    "category": "grobid",
                    "icon": "download",
                    "state_params": ["collection"],
                    "required_roles": ["reviewer"],
                },
                {
                    "name": "import_training",
                    "label": "Import GROBID Training Data",
                    "description": "Import GROBID training data (annotations, raw feature files and PDFs) into a collection",
                    "category": "grobid",
                    "icon": "upload",
                    "state_params": ["collection"],
                    "required_roles": ["reviewer"],
                },
                {
                    "name": "reload_feature_file",
                    "label": "Reload Feature File",
                    "description": (
                        "Re-fetch the raw GROBID training package for the current document "
                        "and refresh the cached feature file used by the sync-check lint, "
                        "without re-extracting (and overwriting) the gold-standard annotation. "
                        "Use this after retraining/swapping a GROBID model."
                    ),
                    "category": "grobid",
                    "icon": "arrow-repeat",
                    "state_params": ["xml"],
                    "required_roles": ["reviewer"],
                },
            ],
            "dependencies": ["tei-wizard"],
        }

    def get_endpoints(self) -> dict[str, Callable]:
        """Return available endpoints."""
        return {
            "download_training": self.download_training,
            "import_training": self.import_training,
            "reload_feature_file": self.reload_feature_file,
        }

    @classmethod
    def is_available(cls) -> bool:
        """Check if GROBID server URL is configured."""
        from fastapi_app.lib.utils.config_utils import get_config
        return bool(get_config().get("plugin.grobid.server.url"))

    async def initialize(self, context: PluginContext) -> None:
        """Register the GROBID extractor and event handlers."""
        from fastapi_app.lib.plugins.frontend_extension_registry import FrontendExtensionRegistry
        ext_registry = FrontendExtensionRegistry.get_instance()
        extension_file = Path(__file__).parent / "extensions" / "grobid-sync.js"
        logger.debug("DEBUG grobid-sync extension file path: %s (exists=%s)", extension_file, extension_file.exists())
        if extension_file.exists():
            ext_registry.register_extension(extension_file, self.metadata["id"])
            logger.info("Registered grobid-sync frontend extension")
        else:
            logger.warning("grobid-sync extension file not found: %s", extension_file)

        registry = ExtractorRegistry.get_instance()
        registry.register(GrobidTrainingExtractor)
        register_document_rules_provider("GROBID", "grobid", GrobidRulesProvider())
        register_schema_config(GROBID_RNG_URL, timeout=10, reason="Grobid RelaxNG schema is complex and may be slow")
        # Garbage collection deletes only the cache entries no training document refers to
        register_gc_task(
            f"plugin:{self.metadata['id']}", "GROBID training-data cache (unreferenced entries)",
            collect_cache_garbage, order=850,
        )
        if has_layout_api():
            from fastapi_app.plugin_api import register_layout_provider

            register_layout_provider(SEGMENTATION, self.metadata["id"], segmentation_layout)
        # regenerates the feature file when the editor writes text corrections into the PDF (pdf-tei-editor#931)
        register_corrected_pdf_handler_if_available(self.metadata["id"])
        self._register_enhancements(context)

        logger.info("GROBID extractor plugin initialized")

    def _register_enhancements(self, context: PluginContext) -> None:
        """Register the TEI Wizard enhancement "Insert page breaks"."""
        from fastapi_app.plugins.tei_wizard.plugin import TeiWizardPlugin

        tei_wizard = context.get_dependency("tei-wizard")
        if not isinstance(tei_wizard, TeiWizardPlugin):
            logger.debug("tei-wizard dependency not available")
            return
        self._wizard = tei_wizard
        tei_wizard.register_enhancement(Path(__file__).parent / "enhancements" / "insert-page-breaks.js", self.metadata["id"])

    async def cleanup(self) -> None:
        """Unregister the GROBID extractor and event handlers."""
        registry = ExtractorRegistry.get_instance()
        registry.unregister("grobid")
        unregister_document_rules_provider("GROBID")
        unregister_schema_config(GROBID_RNG_URL)
        unregister_gc_task(f"plugin:{self.metadata['id']}")
        if has_layout_api():
            from fastapi_app.plugin_api import unregister_layout_provider

            unregister_layout_provider(SEGMENTATION)
        unregister_corrected_pdf_handler_if_available()
        if self._wizard is not None:
            self._wizard.unregister_enhancements(self.metadata["id"])
            self._wizard = None

        logger.info("GROBID extractor plugin cleaned up")

    async def download_training(
        self, context: PluginContext, params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Generate download URL for GROBID training package.

        Args:
            context: Plugin context
            params: Parameters including 'collection' (collection ID)

        Returns:
            downloadUrl pointing to the download route
        """
        collection = params.get("collection")
        if not collection:
            return {
                "error": "No collection selected",
                "message": "Please select a collection first.",
            }

        # Build download URL with optional parameters
        # no_progress=false enables SSE progress events for UI usage
        download_url = f"/api/plugins/grobid/download?collection={collection}&no_progress=false"

        # Add optional parameters if provided
        if params.get("force_refresh"):
            download_url += "&force_refresh=true"
        if params.get("flavor"):
            download_url += f"&flavor={params['flavor']}"

        return {
            "downloadUrl": download_url,
            "collection": collection,
        }

    async def import_training(
        self, context: PluginContext, params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Open the upload form of the training data import (#68).

        Args:
            context: Plugin context
            params: May include 'collection' (preselected target collection)

        Returns:
            outputUrl of the upload form
        """
        from urllib.parse import quote

        collection = params.get("collection") or ""
        return {"outputUrl": f"/api/plugins/grobid/import-training/view?collection={quote(collection)}"}

    async def reload_feature_file(
        self, context: PluginContext, params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Trigger the reviewer-confirmed "reload feature file" flow.

        Re-fetching training data from GROBID and possibly patching the
        document's revision label is a destructive-ish, infrequently-needed
        action that's easy to trigger by accident from a menu, so it follows
        the preview-then-execute pattern: this only returns URLs for a
        confirmation page (`outputUrl`) and the actual operation
        (`executeUrl`); nothing happens until the user reviews the
        confirmation and clicks Execute. See `reload_feature_file.py` for the
        precondition checks and business logic, and `routes.py` for the two
        HTTP routes.

        Args:
            context: Plugin context (used for the reviewer-role check)
            params: Must include 'xml' (stable_id of the open gold TEI file)

        Returns:
            Dict with 'outputUrl'/'executeUrl' on success, or 'error'.
        """
        from urllib.parse import quote

        from fastapi_app.lib.permissions.acl_utils import user_has_role

        if not user_has_role(context.user, ["reviewer", "admin"]):
            return {"error": "Reviewer role required."}

        stable_id = params.get("xml")
        if not stable_id:
            return {"error": "No TEI document open. Open a GROBID training file first."}

        query = f"xml={quote(stable_id)}"
        return {
            "outputUrl": f"/api/plugins/grobid/reload-feature-file/preview?{query}",
            "executeUrl": f"/api/plugins/grobid/reload-feature-file/execute?{query}",
        }
