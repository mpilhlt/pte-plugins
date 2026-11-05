"""
pytest setup: the plugin tests run in the Python environment of a PDF-TEI-Editor checkout,
with the plugin packages installed into it (see README.md).

PTE_ROOT points to the checkout (default: ../pdf-tei-editor); it is put on sys.path so that
``fastapi_app`` can be imported. The editor's data directory (DATA_ROOT, relative "data" by
default) is set to a temporary directory, so that tests do not write into this repository.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
PTE_ROOT = Path(os.environ.get("PTE_ROOT", REPO_ROOT.parent / "pdf-tei-editor")).resolve()

if str(PTE_ROOT) not in sys.path:
    sys.path.insert(0, str(PTE_ROOT))

os.environ.setdefault("DATA_ROOT", tempfile.mkdtemp(prefix="pte-plugins-test-data-"))
