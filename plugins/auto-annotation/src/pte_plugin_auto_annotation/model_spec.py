"""
Resolving a model name as a user writes it to a (provider id, model id) pair.

Accepted forms, matched case-insensitively:

- `default`: the configured default model
- `<provider>`: the provider's only model, or the default model if it belongs to that provider
- `<provider>/<model>`: a model of the provider, by id or label; model ids may contain `/` themselves

`<provider>` is a provider id (`endpoint-ollama`, `kisski`), its label (an inference endpoint's
name, e.g. `Ollama`), or an inference endpoint's id without the `endpoint-` prefix.

Pure module: the server passes in what the provider registry lists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ENDPOINT_PREFIX = "endpoint-"


@dataclass(frozen=True)
class ProviderInfo:
    """A provider as listed by the registry: ids and labels of it and of its models (id -> label)."""

    id: str
    label: str
    models: dict[str, str] = field(default_factory=dict)

    def names(self) -> set[str]:
        """The lowercased names the provider can be referred to by."""
        names = {self.id.lower(), self.label.lower()}
        if self.id.startswith(ENDPOINT_PREFIX):
            names.add(self.id[len(ENDPOINT_PREFIX):].lower())
        return names


class ModelSpecError(ValueError):
    """The name matches no model, or more than one."""


def _models_hint(provider: ProviderInfo) -> str:
    return ", ".join(sorted(provider.models)) or "(no models listed)"


def resolve_model_spec(
    spec: str, providers: list[ProviderInfo], default: tuple[str, str] | None = None
) -> tuple[str, str]:
    """
    Resolve spec to (provider id, model id).

    Raises:
        ModelSpecError: no match, an ambiguous match, or no default where one is needed.
    """
    wanted = spec.strip()
    if not wanted:
        raise ModelSpecError("Empty model name")
    if wanted.lower() == "default":
        if default is None:
            raise ModelSpecError("No default model is configured")
        return default

    lowered = wanted.lower()
    candidates: list[tuple[ProviderInfo, str]] = []
    for provider in providers:
        for name in provider.names():
            if lowered == name:
                candidates.append((provider, ""))
            elif lowered.startswith(name + "/"):
                candidates.append((provider, wanted[len(name) + 1:]))
    if not candidates:
        known = ", ".join(sorted(p.id for p in providers)) or "none"
        raise ModelSpecError(f'No provider matches "{spec}" (providers: {known})')
    # the longest provider name wins, so a label containing "/" beats a shorter prefix
    candidates.sort(key=lambda c: len(c[1]))
    provider, model = candidates[0]
    if len({c[0].id for c in candidates if c[1] == model}) > 1:
        raise ModelSpecError(f'"{spec}" matches more than one provider; use the provider id')

    if not model:
        if default is not None and default[0] == provider.id:
            return default
        if len(provider.models) == 1:
            return provider.id, next(iter(provider.models))
        raise ModelSpecError(f'Provider "{provider.id}" has several models; name one: {_models_hint(provider)}')

    if model in provider.models:
        return provider.id, model
    matches = [mid for mid, label in provider.models.items() if model.lower() in (mid.lower(), label.lower())]
    if len(matches) == 1:
        return provider.id, matches[0]
    if not provider.models:
        # providers that cannot list their models still accept a model id
        return provider.id, model
    raise ModelSpecError(f'Provider "{provider.id}" has no model "{model}"; models: {_models_hint(provider)}')
