"""
Move all of pdf-tei-editor's pte-plugins pins to one commit.

Rewrites the ``rev`` of every ``[tool.uv.sources]`` entry in pdf-tei-editor's pyproject.toml that
points at this repository. All pins must move together: plugins that depend on each other declare
``{ workspace = true }`` sources, so a plugin installed from a git subdirectory resolves its
sibling plugins from the same commit, and differing pins for two coupled plugins cannot be locked.
Prints the plugin paths whose pin changed, one per line.

Usage:
    python scripts/bump_core_pins.py <pyproject.toml> <commit-sha>
"""

import re
import sys
from pathlib import Path

REPO_URL = "https://github.com/mpilhlt/pte-plugins"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def bump_pins(pyproject: str, sha: str) -> tuple[str, list[str]]:
    """Return ``pyproject`` with every pte-plugins pin moved to ``sha``, and the plugin paths that changed."""
    if not SHA_RE.match(sha):
        raise ValueError(f"Expected a 40-char commit sha, got {sha!r}")
    bumped: list[str] = []
    lines = pyproject.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if f'git = "{REPO_URL}"' not in line:
            continue
        new_line = re.sub(r'rev = "[0-9a-f]+"', f'rev = "{sha}"', line)
        if new_line != line:
            lines[i] = new_line
            match = re.search(r'subdirectory = "([^"]+)"', line)
            bumped.append(match.group(1) if match else line.split("=", 1)[0].strip())
    return "".join(lines), bumped


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    pyproject = Path(sys.argv[1])
    text, bumped = bump_pins(pyproject.read_text(encoding="utf-8"), sys.argv[2])
    pyproject.write_text(text, encoding="utf-8")
    for path in bumped:
        print(path)


if __name__ == "__main__":
    main()
