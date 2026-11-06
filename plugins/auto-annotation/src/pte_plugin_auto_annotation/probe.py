"""
Model probe: how well does a model correct GROBID's annotation of a document, compared with its
gold version, and how does that change with the number of units sent per request?

    uv run python -m pte_plugin_auto_annotation.probe --model kisski/llama-3.3-70b-instruct --doc 10.1234__abc

Runs against a running editor (credentials as for bin/batch-extract.js: --user/--password or
API_USER/API_PASSWORD/API_BASE_URL from .env). Each --model is evaluated on its own, on each
--doc, for every batch spec and output mode. Writes units.csv, batches.jsonl,
summary.json and report.md into --out.

No imports from fastapi_app.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import requests

from .api_client import ApiError, ApiSession, ServerChatClient
from .batching import BatchSpec
from .correction import RetryPolicy
from .evaluation import EvalSource, EvaluationResult, RunConfig, evaluate, sweep
from .prompts import OUTPUT_MODES, OutputMode
from .report import markdown_report, write_results

DEFAULT_BATCHES = "units:1,units:4,units:8,units:16,units:32"


def _load_env(path: Path) -> dict[str, str]:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pte_plugin_auto_annotation.probe",
        description="Evaluate LLMs at correcting the annotation of gold documents, across batch sizes.",
    )
    parser.add_argument("--model", action="append", required=True, help="Model name as configured in the app: default, <provider>, or <provider>/<model> (repeatable)")
    parser.add_argument("--doc", action="append", default=[], help="Document id; its gold version of --variant is the reference (repeatable)")
    parser.add_argument("--variant", help="Variant of the reference and input versions (default: grobid.training.references, or the variant of --reference)")
    parser.add_argument("--reference", help="Stable id of the version to score against instead of the document's gold version (only with at most one --doc)")
    parser.add_argument("--input", help="Stable id of the version to correct (only with a single document); default: the earliest version of the same variant")
    parser.add_argument("--batches", default=DEFAULT_BATCHES, help=f"Comma-separated batch specs units:N or chars:N (default: {DEFAULT_BATCHES})")
    parser.add_argument("--modes", default="all", help="Comma-separated output modes: all, changed (default: all)")
    parser.add_argument("--repeats", type=int, default=1, help="Repetitions of each configuration (default: 1)")
    parser.add_argument("--concurrency", type=int, default=1, help="Requests in flight per run (default: 1)")
    parser.add_argument("--retries", type=int, default=RetryPolicy.retries, help=f"Retries of a request after a provider overload or rate limit, with exponential backoff (default: {RetryPolicy.retries})")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--rules-file", type=Path, help="Use this file as the only rule text instead of the gold version's rule refs")
    parser.add_argument("--max-units", type=int, help="Evaluate only the first N aligned units")
    parser.add_argument("--out", type=Path, help="Output directory (default: ./probe-results/<timestamp>)")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="Path to .env file (default: ./.env)")
    parser.add_argument("--base-url", help="API base URL (default: API_BASE_URL or http://localhost:8000)")
    parser.add_argument("--user", help="Username (default: API_USER)")
    parser.add_argument("--password", help="Password (default: API_PASSWORD)")
    return parser


def parse_modes(value: str) -> list[OutputMode]:
    modes: list[OutputMode] = []
    for item in (v.strip() for v in value.split(",") if v.strip()):
        if item not in OUTPUT_MODES:
            raise ValueError(f'Unknown output mode "{item}" (expected one of {", ".join(OUTPUT_MODES)})')
        modes.append(item)  # type: ignore[arg-type]  # checked against OUTPUT_MODES above
    return modes


def _pin_rules(source: EvalSource, rules_text: str | None) -> EvalSource:
    if rules_text is None:
        return source
    return dataclasses.replace(source, rules=[("pinned", rules_text)])


async def run_probe(
    session: ApiSession, models: Sequence[str], docs: Sequence[str | None], variant: str | None, reference: str | None,
    input_id: str | None, configs: Sequence[RunConfig], concurrency: int, temperature: float, rules_text: str | None,
    max_units: int | None, retry: RetryPolicy | None = None,
) -> list[EvaluationResult]:
    """Evaluate every model on every document (a single None document with --reference)."""
    results: list[EvaluationResult] = []
    sources = [
        _pin_rules(session.eval_source(doc, variant, reference, input_id, with_rules=rules_text is None), rules_text)
        for doc in docs
    ]
    for spec in models:
        provider_id, model_id = session.resolve_model(spec)
        label = f"{provider_id}/{model_id}"
        client = ServerChatClient(session, provider_id, model_id, temperature)
        for source in sources:
            def progress(number: int, total: int, config: RunConfig, source: EvalSource = source) -> None:
                print(f"[{label} | {source.doc_id or source.reference_stable_id}] run {number}/{total}: {config.spec.label}, {config.mode}, #{config.repeat}", file=sys.stderr)
            results.append(await evaluate(client, label, source, configs, concurrency, progress, max_units=max_units, retry=retry))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.doc and not args.reference:
        parser.error("Pass --doc, or --reference")
    if (args.input or args.reference) and len(args.doc) > 1:
        parser.error("--input and --reference can only be used with a single document")
    try:
        specs = [BatchSpec.parse(s) for s in args.batches.split(",") if s.strip()]
        modes = parse_modes(args.modes)
    except ValueError as e:
        parser.error(str(e))
    env = _load_env(args.env)
    base_url = args.base_url or os.environ.get("API_BASE_URL") or env.get("API_BASE_URL") or "http://localhost:8000"
    user = args.user or os.environ.get("API_USER") or env.get("API_USER")
    password = args.password or os.environ.get("API_PASSWORD") or env.get("API_PASSWORD")
    if not user or not password:
        parser.error("No credentials: pass --user and --password, or set API_USER and API_PASSWORD")
    rules_text = args.rules_file.read_text(encoding="utf-8") if args.rules_file else None
    out = args.out or Path("probe-results") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    try:
        session = ApiSession.login(base_url, user, password)
        results = asyncio.run(run_probe(
            session, args.model, args.doc or [None], args.variant, args.reference, args.input, sweep(specs, modes, args.repeats),
            args.concurrency, args.temperature, rules_text, args.max_units, RetryPolicy(retries=max(0, args.retries)),
        ))
    except (ApiError, ValueError, requests.RequestException) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    paths = write_results(results, out)
    print(markdown_report(results))
    print("Written: " + ", ".join(str(p) for p in paths), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
