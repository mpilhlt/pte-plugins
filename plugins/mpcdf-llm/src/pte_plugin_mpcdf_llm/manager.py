"""
Per-user MPCDF ProvisioningManagers of one worker.

The MPCDF Terms of Use do not allow users to share a session, so every editor user logs in
with their own MPCDF account and gets their own manager:

- data directory ``<plugins data dir>/mpcdf-llm/users/<hex of the username>/`` (endpoint
  state and locks, so the library's multi-worker guarantees hold per user),
- session in the core's encrypted secret store (``SecretSessionStore``), never in a file,
- Hugging Face token from the user's ``hf-read-token`` API key, read at every submit,
- presets from the installation-wide catalog in ``<plugins data dir>/mpcdf-llm/presets/``,
  maintained by admins.

A worker creates a user's manager on the user's first request and, through ``sync_users()``,
for every user with a stored session, so supervisors (renewal, idle cancel) keep running while
the user is offline. The library's routers receive ``get_user_manager`` and so always act on the
requesting user's manager.
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends
from mpcdf_llm.errors import MpcdfError
from mpcdf_llm.manager import ProvisioningManager
from mpcdf_llm.models import Session
from mpcdf_llm.presets import JsonFilePresetStore
from pydantic import SecretStr, ValidationError

from fastapi_app.lib.secrets import SecretsError, get_secret_store
from fastapi_app.plugin_api import get_config, get_plugin_config, get_settings, require_authenticated_user

logger = logging.getLogger(__name__)

PLUGIN_ID = "mpcdf-llm"
DEFAULT_BASE_URL = "https://llm.mpcdf.mpg.de"

#: Secret service holding a user's MPCDF session. Not registered, so it does not appear in the
#: API Keys dialog; "Remove all" there removes it too, which logs the user out of MPCDF.
SESSION_SECRET = "mpcdf-llm-session"
#: Secret service of a user's Hugging Face read token (registered by the plugin, see plugin.py).
HF_TOKEN_SECRET = "hf-read-token"
#: Config key of the installation-wide Hugging Face token that earlier versions read.
LEGACY_HF_TOKEN_KEY = "plugin.mpcdf-llm.hf-token"

_managers: dict[str, ProvisioningManager] = {}
_tasks: dict[str, asyncio.Task[None]] = {}
_catalog: JsonFilePresetStore | None = None
_running = False
#: Extra arguments of every new ProvisioningManager (tests pass the fake's HTTP client here)
_manager_options: dict[str, Any] = {}


def data_dir() -> Path:
    """The plugin's data directory."""
    return Path(get_settings().plugins_data_dir) / PLUGIN_ID


def catalog_dir() -> Path:
    """Directory of the preset catalog shared by all users."""
    return data_dir() / "presets"


def users_dir() -> Path:
    """Directory holding one directory per user."""
    return data_dir() / "users"


def user_key(username: str) -> str:
    """Filesystem-safe, reversible and case-preserving name of a user's directory."""
    return username.encode("utf-8").hex()


def username_of(key: str) -> str | None:
    """The username of a user directory name, or None if it is not one."""
    try:
        return bytes.fromhex(key).decode("utf-8") or None
    except ValueError:
        return None


def user_dir(username: str) -> Path:
    """Data directory of a user's manager (endpoint state and locks)."""
    return users_dir() / user_key(username)


def base_url() -> str:
    """Base URL of the MPCDF LLM service (config ``plugin.mpcdf-llm.base-url`` or MPCDF_LLM_BASE_URL)."""
    return get_plugin_config(
        "plugin.mpcdf-llm.base-url",
        "MPCDF_LLM_BASE_URL",
        default=DEFAULT_BASE_URL,
        description="Base URL of the MPCDF LLM service",
    )


def catalog() -> JsonFilePresetStore:
    """The preset catalog of this worker (all users' managers share it)."""
    global _catalog
    if _catalog is None or _catalog.data_dir != catalog_dir():
        _catalog = JsonFilePresetStore(catalog_dir())
    return _catalog


class SecretSessionStore:
    """The library's SessionStore on the core's encrypted secret store, for one user."""

    def __init__(self, username: str):
        self.username = username

    def load(self) -> Session | None:
        """The stored session, or None if there is none or it cannot be read."""
        try:
            value = get_secret_store().get(self.username, SESSION_SECRET)
        except SecretsError:
            logger.warning("The stored MPCDF session of a user cannot be decrypted; the user must log in again")
            return None
        if value is None:
            return None
        try:
            data = json.loads(value)
            return Session(
                token=SecretStr(data["token"]),
                username=data["username"],
                obtained_at=datetime.fromisoformat(data["obtained_at"]),
            )
        except (ValueError, KeyError, TypeError, ValidationError):
            raise MpcdfError("corrupt stored MPCDF session") from None

    def save(self, s: Session) -> None:
        """Store the session encrypted."""
        value = json.dumps(
            {"token": s.token.get_secret_value(), "username": s.username, "obtained_at": s.obtained_at.isoformat()}
        )
        get_secret_store().set(self.username, SESSION_SECRET, value)

    def clear(self) -> None:
        """Remove the stored session."""
        get_secret_store().delete(self.username, SESSION_SECRET)


def has_stored_session(username: str) -> bool:
    """Whether the user has a stored MPCDF session (without decrypting it)."""
    return get_secret_store().has(username, SESSION_SECRET)


def user_hf_token(username: str) -> str | None:
    """The user's Hugging Face read token, or None. Never falls back to HF_TOKEN in the environment."""
    try:
        return get_secret_store().get(username, HF_TOKEN_SECRET) or None
    except SecretsError:
        logger.warning("The stored Hugging Face token of a user cannot be decrypted")
        return None


def has_hf_token(username: str) -> bool:
    """Whether the user has stored a Hugging Face read token."""
    return get_secret_store().has(username, HF_TOKEN_SECRET)


def known_users() -> list[str]:
    """Users that have a directory (they used the plugin at least once)."""
    root = users_dir()
    if not root.is_dir():
        return []
    names = (username_of(p.name) for p in root.iterdir() if p.is_dir())
    return sorted(name for name in names if name)


def new_manager(username: str) -> ProvisioningManager:
    """A manager for the user."""
    return ProvisioningManager(
        data_dir=user_dir(username),
        base_url=base_url(),
        presets=catalog(),
        sessions=SecretSessionStore(username),
        # Always a callable: without one the library would read HF_TOKEN from the environment
        hf_token=lambda: user_hf_token(username),
        **_manager_options,
    )


async def _run(manager: ProvisioningManager) -> None:
    """Restore a stored session (may use the network), then start the supervisor.

    The supervisor starts even if open() fails: the library restores lazily.
    Only the exception type is logged, as messages and tracebacks may carry secrets.
    """
    try:
        await manager.open()
    except Exception as e:
        logger.warning("Could not open an MPCDF LLM manager: %s", type(e).__name__)
    try:
        manager.start_supervisor()
    except Exception as e:
        logger.warning("Could not start an MPCDF LLM supervisor: %s", type(e).__name__)


def existing_manager(username: str | None) -> ProvisioningManager | None:
    """The user's manager in this worker, if there is one."""
    return _managers.get(username) if username else None


def managers() -> dict[str, ProvisioningManager]:
    """A snapshot of this worker's managers by username."""
    return dict(_managers)


def manager_for(username: str) -> ProvisioningManager:
    """The user's manager, created (and opened in the background) on first use.

    Raises LookupError when the plugin is not running (mapped to 503 by the library).
    Must be called in the event loop the plugin was initialized in.
    """
    if not _running:
        raise LookupError("MPCDF LLM is not initialized")
    manager = _managers.get(username)
    if manager is None:
        user_dir(username).mkdir(parents=True, exist_ok=True)
        manager = new_manager(username)
        _managers[username] = manager
        _tasks[username] = asyncio.create_task(_run(manager))
    return manager


async def get_user_manager(user: dict[str, Any] = Depends(require_authenticated_user)) -> ProvisioningManager:
    """FastAPI dependency: the requesting user's manager."""
    username = user.get("username")
    if not username:
        raise LookupError("no username")
    return manager_for(username)


async def drop_manager(username: str) -> None:
    """Close and forget the user's manager in this worker. Never stops endpoints."""
    manager = _managers.pop(username, None)
    task = _tasks.pop(username, None)
    if task is not None:
        task.cancel()
        # gather (not suppress(CancelledError)) so cancellation of this coroutine propagates
        await asyncio.gather(task, return_exceptions=True)
    if manager is not None:
        try:
            await manager.close()
        except Exception as e:
            logger.warning("Could not close an MPCDF LLM manager: %s", type(e).__name__)


async def sync_users() -> None:
    """Converge this worker's managers with the stored sessions.

    Opens a manager for every user with a stored session (the login may have happened in
    another worker), and drops managers that still hold a session in memory although it is no
    longer stored: the user logged out in another worker, removed all their API keys, or an
    admin removed the session.
    """
    if not _running:
        return
    for username in known_users():
        if username not in _managers and has_stored_session(username):
            manager_for(username)
    for username, manager in list(_managers.items()):
        if has_stored_session(username):
            continue
        if manager.client.session is not None:
            await drop_manager(username)


def migrate() -> None:
    """Move the state of the single-session version: presets into the catalog, discard the shared session.

    Endpoints of the old shared session are left alone; without a session they expire at MPCDF.
    """
    root = data_dir()
    old_presets = root / "presets.json"
    new_presets = catalog_dir() / "presets.json"
    if old_presets.is_file() and not new_presets.exists():
        catalog_dir().mkdir(parents=True, exist_ok=True)
        old_presets.replace(new_presets)
        logger.info("Moved the MPCDF LLM presets into the shared catalog")
    old_session = root / "session.json"
    if old_session.exists():
        old_session.unlink()
        logger.warning(
            "Discarded the shared MPCDF session: every user now logs in with their own MPCDF account. Endpoints started with it keep running at MPCDF until they expire."
        )
    config = get_config()
    if config.get(LEGACY_HF_TOKEN_KEY) is not None:
        config.delete(LEGACY_HF_TOKEN_KEY, with_metadata=True)
        config.delete(f"{LEGACY_HF_TOKEN_KEY}.masked")
        logger.warning(
            "Removed the config key %s: each user now stores their own Hugging Face read token in the API Keys dialog",
            LEGACY_HF_TOKEN_KEY,
        )


async def start() -> None:
    """Start serving managers in this worker (in the current event loop)."""
    global _running
    if _running:
        await stop()
    try:
        migrate()
    except Exception as e:
        logger.warning("Could not migrate the MPCDF LLM data: %s", type(e).__name__)
    _running = True
    await sync_users()


async def stop() -> None:
    """Close all managers of this worker. Never stops endpoints."""
    global _running
    _running = False
    for username in list(_managers):
        await drop_manager(username)
