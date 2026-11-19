"""
GROBID extractor plugin.

Registers the GrobidTrainingExtractor with the extraction registry.
Provides a download endpoint for reviewers to download complete training packages.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from fastapi_app.plugin_api import (
    clear_directory,
    ExtractorRegistry,
    get_event_bus,
    Plugin,
    PluginContext,
    register_document_rules_provider,
    register_gc_handler,
    register_schema_config,
    unregister_document_rules_provider,
    unregister_gc_handler,
    unregister_schema_config,
)
from pte_plugin_grobid.annotation_rules import GrobidRulesProvider
from .config import has_layout_api, has_page_break_api, init_plugin_config
from .layout import SEGMENTATION, segmentation_layout

from .cache import get_cache_dir
from .extractor import GrobidTrainingExtractor

if TYPE_CHECKING:
    from fastapi_app.plugins.tei_wizard.plugin import TeiWizardPlugin

logger = logging.getLogger(__name__)

GROBID_RNG_URL = "https://raw.githubusercontent.com/kermitt2/grobid/refs/heads/master/grobid-home/schemas/rng/Grobid.rng"


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
                    "category": "collection",
                    "icon": "download",
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
        register_gc_handler(self.metadata["id"], lambda: clear_directory(get_cache_dir()))
        if has_layout_api():
            from fastapi_app.plugin_api import register_layout_provider

            register_layout_provider(SEGMENTATION, self.metadata["id"], segmentation_layout)
        self._register_enhancements(context)

        # Register event handler for file deletion cache cleanup
        event_bus = get_event_bus()
        event_bus.on("file.deleted", self._on_file_deleted)
        
        logger.info("GROBID extractor plugin initialized")

    def _register_enhancements(self, context: PluginContext) -> None:
        """Register the TEI Wizard enhancement "Insert page breaks" (editors that read <pb/> in zone documents only)."""
        if not has_page_break_api():
            return
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
        unregister_gc_handler(self.metadata["id"])
        if has_layout_api():
            from fastapi_app.plugin_api import unregister_layout_provider

            unregister_layout_provider(SEGMENTATION)
        if self._wizard is not None:
            self._wizard.unregister_enhancements(self.metadata["id"])
            self._wizard = None

        # Unregister event handler
        event_bus = get_event_bus()
        event_bus.off("file.deleted", self._on_file_deleted)

        logger.info("GROBID extractor plugin cleaned up")

    async def _on_file_deleted(self, stable_id: str, **kwargs) -> None:
        """
        Clean up cached GROBID training data when a file is deleted.

        This handler is called when any file is deleted. It checks if the file
        was a PDF and removes any cached training data for that document.

        Args:
            stable_id: The stable_id of the deleted file
        """
        from fastapi_app.lib.core.dependencies import get_db
        from fastapi_app.lib.repository.file_repository import FileRepository

        try:
            # Get file info to check if it was a PDF
            db = get_db()
            file_repo = FileRepository(db)
            file_info = file_repo.get_file_by_stable_id(stable_id)

            if not file_info or file_info.file_type != "pdf":
                return

            doc_id = file_info.doc_id
            if not doc_id:
                return

            # Check if any other TEI files still exist for this doc_id
            doc_files = file_repo.get_files_by_doc_id(doc_id)
            other_teis = [f for f in doc_files if f.file_type == "tei" and f.stable_id != stable_id and not f.deleted]

            if other_teis:
                # Other PDFs exist, don't delete cache
                return

            # Delete cached training data for this document
            from pte_plugin_grobid.cache import delete_cache_for_doc

            if delete_cache_for_doc(doc_id):
                logger.info(f"Deleted cached GROBID training data for {doc_id}")

        except Exception as e:
            logger.warning(f"Failed to clean up GROBID cache for {stable_id}: {e}")

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
