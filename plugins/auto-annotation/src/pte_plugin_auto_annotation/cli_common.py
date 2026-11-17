"""
Connection options shared by the CLIs: --base-url, --token and --env, with the environment variables
API_BASE_URL and API_TOKEN as for bin/batch-extract.js. The CLIs log in with a personal access token only.

No imports from fastapi_app.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "http://localhost:8000"


def load_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a .env file (no interpolation); {} if it does not exist."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    return values


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    """Add --env, --base-url and --token."""
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to .env file (default: ./.env)")
    parser.add_argument("--base-url", help=f"API base URL (default: API_BASE_URL or {DEFAULT_BASE_URL})")
    parser.add_argument("--token", help="Personal access token (default: API_TOKEN)")


@dataclass(frozen=True)
class Connection:
    base_url: str
    token: str


def connection_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Connection:
    """The connection settings; exits through parser.error() without a token."""
    env = load_env(args.env)
    base_url = args.base_url or os.environ.get("API_BASE_URL") or env.get("API_BASE_URL") or DEFAULT_BASE_URL
    token = args.token or os.environ.get("API_TOKEN") or env.get("API_TOKEN")
    if not token:
        parser.error("No access token: pass --token or set API_TOKEN")
    return Connection(base_url, token=token)
