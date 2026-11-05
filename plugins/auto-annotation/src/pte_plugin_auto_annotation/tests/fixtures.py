"""Shared test documents and mock models."""

from __future__ import annotations

from pte_plugin_auto_annotation.correction import TransientError
from pte_plugin_auto_annotation.prompts import parse_unit_answer

HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <encodingDesc>
      <editorialDecl>
        <interpretation type="references">
          <p><ref target="https://raw.example.org/guidelines-llm.md#L1-L5" subtype="machine"/></p>
        </interpretation>
      </editorialDecl>
    </encodingDesc>
  </teiHeader>
"""

INPUT_UNITS = [
    "<bibl><author>Smith, J.</author> Contracts, 2001, p. 5.</bibl>",
    "<bibl><author>Doe, A.</author> <title>Torts</title>, 1999.</bibl>",
    "<bibl>Ibid., p. 7.</bibl>",
    "<bibl><author>Roe, B.</author> Property, 2010.</bibl>",
]

GOLD_UNITS = [
    '<bibl><author>Smith, J.</author> <title level="m">Contracts</title>, <date>2001</date>, <biblScope unit="page">p. 5</biblScope>.</bibl>',
    '<bibl><author>Doe, A.</author> <title level="m">Torts</title>, <date>1999</date>.</bibl>',
    '<bibl>Ibid., <citedRange unit="page">p. 7</citedRange>.</bibl>',
    '<bibl><author>Roe, B.</author> <title level="m">Property</title>, <date>2010</date>.</bibl>',
]


def tei(units: list[str], pretty: bool = False) -> str:
    sep = "\n        " if pretty else ""
    body = sep.join(units)
    return f"{HEADER}  <text><back><listBibl>{sep}{body}{sep}</listBibl></back></text>\n</TEI>\n"


INPUT_XML = tei(INPUT_UNITS)
GOLD_XML = tei(GOLD_UNITS, pretty=True)

def prompt_units(user_prompt: str) -> dict[int, str]:
    """The units of a user prompt by id."""
    return parse_unit_answer(user_prompt.split("## Units", 1)[1])


class OracleClient:
    """Answers every unit with its gold version (looked up by the input unit's XML)."""

    def __init__(self, mapping: dict[str, str], drop_after: int | None = None, only_changed: bool = False):
        self.mapping = mapping
        self.drop_after = drop_after
        self.only_changed = only_changed
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        parts = []
        for uid, xml in prompt_units(user_prompt).items():
            if self.drop_after is not None and uid > self.drop_after:
                break
            answer = self.mapping.get(xml, xml)
            if self.only_changed and answer == xml:
                continue
            parts.append(f"### Unit {uid}\n{answer}")
        return "```xml\n" + "\n\n".join(parts) + "\n```" if parts else "NO CHANGES"


class EchoClient:
    """Returns every unit unchanged."""

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        return "\n\n".join(f"### Unit {uid}\n{xml}" for uid, xml in prompt_units(user_prompt).items())


class FailingClient:
    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("provider down")


class FlakyClient:
    """Fails with TransientError for the first `failures` requests, then answers like the wrapped client."""

    def __init__(self, client: object, failures: int):
        self.client = client
        self.failures = failures
        self.calls = 0

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise TransientError("503 This model is currently experiencing high demand")
        return await self.client.complete(system_prompt, user_prompt)  # type: ignore[attr-defined]
