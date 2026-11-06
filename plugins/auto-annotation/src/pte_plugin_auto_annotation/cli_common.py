"""
Connection options shared by the CLIs: --base-url, --user, --password and --env, with the
environment variables API_BASE_URL, API_USER and API_PASSWORD as for bin/batch-extract.js.

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
    """Add --env, --base-url, --user and --password."""
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to .env file (default: ./.env)")
    parser.add_argument("--base-url", help=f"API base URL (default: API_BASE_URL or {DEFAULT_BASE_URL})")
    parser.add_argument("--user", help="Username (default: API_USER)")
    parser.add_argument("--password", help="Password (default: API_PASSWORD)")


@dataclass(frozen=True)
class Connection:
    base_url: str
    user: str
    password: str


def connection_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Connection:
    """The connection settings; exits through parser.error() without credentials."""
    env = load_env(args.env)
    base_url = args.base_url or os.environ.get("API_BASE_URL") or env.get("API_BASE_URL") or DEFAULT_BASE_URL
    user = args.user or os.environ.get("API_USER") or env.get("API_USER")
    password = args.password or os.environ.get("API_PASSWORD") or env.get("API_PASSWORD")
    if not user or not password:
        parser.error("No credentials: pass --user and --password, or set API_USER and API_PASSWORD")
    return Connection(base_url, user, password)
