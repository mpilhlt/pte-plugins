"""
HTTP client for a running PDF-TEI-Editor: login, the evaluation inputs, and a ChatClient that
sends completions through the server, so the CLI uses the providers, keys and access rules
configured in the app.

No imports from fastapi_app.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

import requests

from .evaluation import EvalSource

# Completions of large batches on slow models take minutes
DEFAULT_TIMEOUT = 900.0


class ApiError(RuntimeError):
    """The server answered with an error status."""


def _check(response: requests.Response) -> dict[str, object]:
    if not response.ok:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise ApiError(f"{response.request.method} {response.url}: {response.status_code} {detail}")
    return response.json()


@dataclass
class ApiSession:
    """A logged-in session."""

    base_url: str
    session_id: str
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def login(cls, base_url: str, username: str, password: str, timeout: float = DEFAULT_TIMEOUT) -> "ApiSession":
        """Log in with the SHA-256 password hash, as the web client does."""
        base = base_url.rstrip("/")
        passwd_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        data = _check(requests.post(f"{base}/api/v1/auth/login", json={"username": username, "passwd_hash": passwd_hash}, timeout=60))
        return cls(base, str(data["sessionId"]), timeout)

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Session-ID": self.session_id}

    def eval_source(self, gold_stable_id: str, input_stable_id: str | None = None, with_rules: bool = True) -> EvalSource:
        """The gold version, the input version, the unit path and the rules."""
        params: dict[str, str] = {"gold": gold_stable_id, "rules": "true" if with_rules else "false"}
        if input_stable_id:
            params["input"] = input_stable_id
        data = _check(requests.get(f"{self.base_url}/api/plugins/auto-annotation/eval-source", params=params, headers=self.headers, timeout=120))
        rules = data["rules"]
        assert isinstance(rules, list)
        return EvalSource(
            gold_stable_id=str(data["gold_stable_id"]), input_stable_id=str(data["input_stable_id"]),
            variant=data["variant"] if isinstance(data["variant"], str) else None,
            unit_path=data["unit_path"] if isinstance(data["unit_path"], str) else None,
            gold_xml=str(data["gold_xml"]), input_xml=str(data["input_xml"]),
            rules=[(str(r["category"]), str(r["text"])) for r in rules],
        )

    def resolve_model(self, spec: str) -> tuple[str, str]:
        """(provider id, model id) for a model name."""
        data = _check(requests.post(f"{self.base_url}/api/plugins/auto-annotation/resolve-model", json={"model": spec}, headers=self.headers, timeout=120))
        return str(data["provider_id"]), str(data["model_id"])

    def complete(self, provider_id: str, model_id: str, system_prompt: str, user_prompt: str, temperature: float) -> str:
        """One completion through the server."""
        data = _check(requests.post(
            f"{self.base_url}/api/plugins/auto-annotation/complete",
            json={"provider_id": provider_id, "model_id": model_id, "system_prompt": system_prompt, "user_prompt": user_prompt, "temperature": temperature},
            headers=self.headers, timeout=self.timeout,
        ))
        return str(data["text"])


@dataclass
class ServerChatClient:
    """ChatClient that sends completions through the server's /complete route."""

    session: ApiSession
    provider_id: str
    model_id: str
    temperature: float = 0.0

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        return await asyncio.to_thread(self.session.complete, self.provider_id, self.model_id, system_prompt, user_prompt, self.temperature)
