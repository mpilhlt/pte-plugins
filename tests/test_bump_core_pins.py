import importlib.util
import unittest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bump_core_pins", REPO_ROOT / "scripts" / "bump_core_pins.py")
assert spec is not None and spec.loader is not None
bump_core_pins = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump_core_pins)

OLD = "966284cb256b3d37de73df2b5166cf77a1901391"
NEW = "0123456789abcdef0123456789abcdef01234567"

PYPROJECT = f"""[project]
name = "pdf-tei-editor"

[tool.uv.sources]
pte-plugin-grobid = {{ git = "https://github.com/mpilhlt/pte-plugins", subdirectory = "plugins/grobid", rev = "{OLD}" }}
pte-plugin-llamore-extractor = {{ git = "https://github.com/mpilhlt/pte-plugins", subdirectory = "plugins/llamore-extractor", rev = "{NEW}" }}
other = {{ git = "https://github.com/someone/else", subdirectory = "plugins/grobid", rev = "{OLD}" }}
"""


class TestBumpCorePins(unittest.TestCase):

    def test_moves_all_pte_plugins_pins(self):
        text, bumped = bump_core_pins.bump_pins(PYPROJECT, NEW)
        lines = text.splitlines()
        self.assertIn(f'subdirectory = "plugins/grobid", rev = "{NEW}"', lines[4])
        self.assertIn(f'subdirectory = "plugins/llamore-extractor", rev = "{NEW}"', lines[5])
        self.assertIn(OLD, lines[6], "sources from other repositories must keep their pin")

    def test_reports_only_changed_pins(self):
        _, bumped = bump_core_pins.bump_pins(PYPROJECT, NEW)
        self.assertEqual(bumped, ["plugins/grobid"])

    def test_no_change_when_already_pinned(self):
        text, _ = bump_core_pins.bump_pins(PYPROJECT, NEW)
        again, bumped = bump_core_pins.bump_pins(text, NEW)
        self.assertEqual(bumped, [])
        self.assertEqual(again, text)

    def test_rejects_abbreviated_sha(self):
        with self.assertRaises(ValueError):
            bump_core_pins.bump_pins(PYPROJECT, "0123abc")


if __name__ == "__main__":
    unittest.main()
