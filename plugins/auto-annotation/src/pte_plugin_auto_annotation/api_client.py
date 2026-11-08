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

from .correction import TransientError
from .evaluation import EvalSource

# Completions of large batches on slow models take minutes
DEFAULT_TIMEOUT = 900.0


class ApiError(RuntimeError):
    """The server answered with an error status."""


class TransientApiError(ApiError, TransientError):
    """The server answered 429 or 503: retrying may help."""


TRANSIENT_STATUSES = frozenset({429, 503})


def _check(response: requests.Response) -> dict[str, object]:
    if not response.ok:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        error = TransientApiError if response.status_code in TRANSIENT_STATUSES else ApiError
        raise error(f"{response.request.method} {response.url}: {response.status_code} {detail}")
    return response.json()


@dataclass
class ApiSession:
    """A logged-in session."""

    base_url: str
    session_id: str
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def login(cls, base_url: str, username: str, password: str, timeout: float = DEFAULT_TIMEOUT) -> "ApiSession":
        """
        Log in with the plain text password, as the web client does; use HTTPS for remote servers.

        Servers from before mpilhlt/pdf-tei-editor#246 only accept the password's SHA-256 hash and reject the
        plain text password with 422, so retry with the hash then.
        """
        base = base_url.rstrip("/")
        url = f"{base}/api/v1/auth/login"
        response = requests.post(url, json={"username": username, "password": password}, timeout=60)
        if response.status_code == 422:
            passwd_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
            response = requests.post(url, json={"username": username, "passwd_hash": passwd_hash}, timeout=60)
        data = _check(response)
        return cls(base, str(data["sessionId"]), timeout)

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Session-ID": self.session_id}

    def eval_source(
        self, doc_id: str | None = None, variant: str | None = None, reference_stable_id: str | None = None,
        input_stable_id: str | None = None, with_rules: bool = True,
    ) -> EvalSource:
        """The reference version (the document's gold version, or the given one), the input version, the unit path and the rules."""
        params: dict[str, str] = {"rules": "true" if with_rules else "false"}
        for name, value in (("doc", doc_id), ("variant", variant), ("reference", reference_stable_id), ("input", input_stable_id)):
            if value:
                params[name] = value
        data = _check(requests.get(f"{self.base_url}/api/plugins/auto-annotation/eval-source", params=params, headers=self.headers, timeout=120))
        rules = data["rules"]
        assert isinstance(rules, list)
        return EvalSource(
            reference_stable_id=str(data["reference_stable_id"]), input_stable_id=str(data["input_stable_id"]),
            variant=data["variant"] if isinstance(data["variant"], str) else None,
            unit_path=data["unit_path"] if isinstance(data["unit_path"], str) else None,
            gold_xml=str(data["gold_xml"]), input_xml=str(data["input_xml"]),
            rules=[(str(r["category"]), str(r["text"])) for r in rules],
            doc_id=str(data["doc_id"]), reference_is_gold=bool(data["reference_is_gold"]), input_choice=str(data["input_choice"]), input_is_extraction=bool(data["input_is_extraction"]),
        )

    def resolve_model(self, spec: str) -> tuple[str, str]:
        """(provider id, model id) for a model name."""
        data = _check(requests.post(f"{self.base_url}/api/plugins/auto-annotation/resolve-model", json={"model": spec}, headers=self.headers, timeout=120))
        return str(data["provider_id"]), str(data["model_id"])

    def complete(self, provider_id: str, model_id: str, system_prompt: str, user_prompt: str, temperature: float) -> str:
        """One completion through the server; raises TransientError when retrying may help."""
        try:
            response = requests.post(
                f"{self.base_url}/api/plugins/auto-annotation/complete",
                json={"provider_id": provider_id, "model_id": model_id, "system_prompt": system_prompt, "user_prompt": user_prompt, "temperature": temperature},
                headers=self.headers, timeout=self.timeout,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            raise TransientError(f"{type(e).__name__}: {e}") from e
        return str(_check(response)["text"])


@dataclass
class ServerChatClient:
    """ChatClient that sends completions through the server's /complete route."""

    session: ApiSession
    provider_id: str
    model_id: str
    temperature: float = 0.0

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        return await asyncio.to_thread(self.session.complete, self.provider_id, self.model_id, system_prompt, user_prompt, self.temperature)
