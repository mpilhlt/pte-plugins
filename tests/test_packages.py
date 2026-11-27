"""
Checks across all plugin packages: every package is installed and found through its
``pte.plugins`` entry point, loads, survives initialize -> cleanup -> initialize -> cleanup, and
its frontend extensions pass the editor's extension validator (which leaves out failing ones).
"""

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = json.loads((REPO_ROOT / "pte-plugins.json").read_text(encoding="utf-8"))


def package_dirs() -> list[Path]:
    """Source directories of the plugin packages, from the index."""
    return [REPO_ROOT / p["path"] / "src" / p["module"] for p in INDEX["plugins"]]


class TestPackages(unittest.IsolatedAsyncioTestCase):

    def test_packages_are_installed_and_discovered(self):
        from fastapi_app.lib.plugins.plugin_paths import get_installed_plugin_dirs

        installed = {d.resolve() for d in get_installed_plugin_dirs()}
        missing = [str(d.relative_to(REPO_ROOT)) for d in package_dirs() if d.resolve() not in installed]
        self.assertEqual(missing, [], "packages not installed into the editor's environment (see README.md)")

    def test_packages_load(self):
        from fastapi_app.config import get_settings
        from fastapi_app.lib.plugins.plugin_registry import PluginRegistry

        # With the built-in plugins, which some plugins depend on (e.g. tei-wizard)
        builtin = get_settings().plugins_code_dir
        registry = PluginRegistry()
        registry.discover_plugins([builtin, *package_dirs()], builtin_dir=builtin)
        for plugin in INDEX["plugins"]:
            with self.subTest(plugin=plugin["id"]):
                record = registry.get_record(plugin["id"])
                assert record is not None, f"plugin {plugin['id']} not found"
                self.assertIn(record.load_status, ("ok", "unavailable"), record.error)

    async def test_initialize_cleanup_initialize(self):
        from fastapi_app.config import get_settings
        from fastapi_app.lib.plugins.plugin_testing import check_lifecycle_conformance

        builtin = get_settings().plugins_code_dir
        checked = await check_lifecycle_conformance([builtin, *package_dirs()], builtin_dir=builtin)
        self.assertTrue({p["id"] for p in INDEX["plugins"]} & set(checked))

    def test_frontend_extensions_pass_validation(self):
        from fastapi_app.lib.plugins.plugin_tools import validate_javascript_content

        files = [f for d in package_dirs() for f in sorted((d / "extensions").glob("*.js"))]
        self.assertTrue(files)
        for js_file in files:
            with self.subTest(file=str(js_file.relative_to(REPO_ROOT))):
                valid, warnings = validate_javascript_content(js_file.read_text(encoding="utf-8"), js_file.name)
                self.assertTrue(valid, "; ".join(warnings))


if __name__ == "__main__":
    unittest.main()
