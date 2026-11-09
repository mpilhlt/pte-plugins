"""
Automated first annotation of documents or a whole collection, against a running editor.

    uv run python -m pte_plugin_auto_annotation.annotate \\
      --model-a kisski/<model> --model-b google/<model> --arbiter anthropic/<model> \\
      --variant grobid.training.references --collection my-collection

For each document, the extraction of --variant (its version without a parent) is annotated on the
server (two models, arbiter for disagreements, see pipeline.py) and saved as a new version of it. If
a document to annotate has several extractions, nothing is annotated and --version must be used.
Documents already annotated by the pipeline, or with a gold version, are skipped unless --force or
--include-gold is given; so an interrupted run can be repeated. --version annotates given versions.
With --dry-run nothing is saved and the XML is written to --out. A JSON run log per document is
always written to --out.

No imports from fastapi_app.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import requests

from .api_client import ApiError, ApiSession, JsonScalar
from .batching import BatchSpec
from .cli_common import add_connection_args, connection_from_args
from .prompts import OUTPUT_MODES

# One request annotates a whole document, on slow models that takes much longer than one completion
DEFAULT_ANNOTATE_TIMEOUT = 3600.0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pte_plugin_auto_annotation.annotate",
        description="Annotate documents with two LLMs and an arbiter, and save the result as a new version.",
    )
    parser.add_argument("--model-a", required=True, help="First model: default, <provider>, or <provider>/<model>")
    parser.add_argument("--model-b", required=True, help="Second model")
    parser.add_argument("--arbiter", help="Model that decides where A and B disagree; without it, those units stay unchanged")
    target = parser.add_argument_group("what to annotate (one of)")
    target.add_argument("--collection", help="All documents of a collection")
    target.add_argument("--doc", action="append", default=[], help="Document id (repeatable)")
    target.add_argument("--version", action="append", default=[], help="Stable id of a version to annotate (repeatable); no skipping")
    parser.add_argument("--variant", help="Variant to annotate (required with --collection and --doc), e.g. grobid.training.references")
    parser.add_argument("--force", action="store_true", help="Also annotate documents the pipeline has annotated before")
    parser.add_argument("--force-busy", action="store_true", help="Start even if a model is reported as busy")
    parser.add_argument("--include-gold", action="store_true", help="Also annotate documents that have a gold version")
    parser.add_argument("--batch", default="units:16", help="Batch spec of models A and B (default: units:16)")
    parser.add_argument("--arbiter-batch", default="units:8", help="Batch spec of the arbiter (default: units:8)")
    parser.add_argument("--mode", default="all", choices=OUTPUT_MODES, help="Output mode of models A and B (default: all)")
    parser.add_argument("--split-on-failure", type=float, default=0.25, help="Share of failed units above which a batch is split and retried; negative: never (default: 0.25)")
    parser.add_argument("--concurrency", type=int, default=1, help="Requests in flight per model (default: 1)")
    parser.add_argument("--retries", type=int, default=4, help="Retries of a request after a provider overload or rate limit (default: 4)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_ANNOTATE_TIMEOUT, help=f"Seconds to wait for the server to annotate one document (default: {DEFAULT_ANNOTATE_TIMEOUT:.0f})")
    parser.add_argument("--status", default="extraction", help="Status of the revision change (default: extraction)")
    parser.add_argument("--label", default="Auto-annotation", help="Label of the revision change (default: Auto-annotation)")
    parser.add_argument("--dry-run", action="store_true", help="Do not save; write the annotated XML to --out")
    parser.add_argument("--raw", action="store_true", help="Include the raw model answers in the run logs")
    parser.add_argument("--out", type=Path, help="Directory for run logs (and XML with --dry-run); default: ./auto-annotation-runs/<timestamp>")
    add_connection_args(parser)
    return parser


class AmbiguousExtractionError(Exception):
    """A document to annotate has several extractions, so the version must be given explicitly."""


def select_targets(
    session: ApiSession, args: argparse.Namespace
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """
    (doc id or "-", stable id) pairs to annotate, and (doc id, reason) pairs that are skipped.

    Raises:
        AmbiguousExtractionError: a document that would be annotated has several extractions.
    """
    if args.version:
        return [("-", v) for v in args.version], []
    selected: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    ambiguous: list[str] = []
    for target in session.targets(args.variant, args.collection, args.doc or None):
        doc_id, state, input_id = str(target["doc_id"]), str(target["state"]), target.get("input_stable_id")
        allowed = state == "ready" or (state == "annotated" and args.force) or (state == "gold" and args.include_gold)
        extractions = target.get("extraction_stable_ids")
        if allowed and isinstance(extractions, list) and len(extractions) > 1:
            ambiguous.append(f"{doc_id} ({', '.join(str(e) for e in extractions)})")
        elif allowed and isinstance(input_id, str):
            selected.append((doc_id, input_id))
        else:
            reason = {"annotated": f"already annotated ({target.get('annotated_stable_id')}); --force to repeat",
                      "gold": "has a gold version; --include-gold to annotate", "no-version": f"no version of {args.variant}"}.get(state, state)
            skipped.append((doc_id, reason))
    if ambiguous:
        raise AmbiguousExtractionError(f"Several extraction versions, nothing was annotated: {'; '.join(ambiguous)}. Specify the exact version to annotate with --version.")
    return selected, skipped


def request_payload(args: argparse.Namespace, stable_id: str) -> dict[str, JsonScalar]:
    """The body of the annotate request."""
    return {
        "stable_id": stable_id, "model_a": args.model_a, "model_b": args.model_b, "arbiter": args.arbiter,
        "batch": args.batch, "arbiter_batch": args.arbiter_batch, "mode": args.mode,
        "split_on_failure": args.split_on_failure if args.split_on_failure >= 0 else None,
        "concurrency": args.concurrency, "retries": args.retries,
        "status": args.status, "label": args.label, "include_raw": args.raw,
    }


def _file_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in text)


def run(session: ApiSession, args: argparse.Namespace, out: Path) -> int:
    """Annotate the selected targets; returns the number of failed documents."""
    selected, skipped = select_targets(session, args)
    for doc_id, reason in skipped:
        print(f"skip {doc_id}: {reason}", file=sys.stderr)
    out.mkdir(parents=True, exist_ok=True)
    failures = 0
    for number, (doc_id, stable_id) in enumerate(selected, start=1):
        print(f"[{number}/{len(selected)}] {doc_id} ({stable_id}) ...", file=sys.stderr)
        try:
            result = session.annotate(request_payload(args, stable_id))
            name = _file_name(str(result.get("doc_id") or doc_id))
            saved: str | None = None
            if args.dry_run:
                (out / f"{name}.xml").write_text(str(result["xml"]), encoding="utf-8")
            else:
                saved = session.save_version(str(result["xml"]), stable_id)
                try:
                    session.release_lock(saved)
                except ApiError as e:
                    print(f"  could not release the lock on {saved}: {e}", file=sys.stderr)
            raw_log = result.get("log")
            log: dict[str, object] = dict(raw_log) if isinstance(raw_log, dict) else {}
            log["saved_stable_id"] = saved
            (out / f"{name}.json").write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
            counts = result.get("counts") or {}
            print(f"  {counts}" + (f" -> saved as {saved}" if saved else " (dry run)"), file=sys.stderr)
        except (ApiError, requests.RequestException) as e:
            failures += 1
            print(f"  failed: {e}", file=sys.stderr)
    print(f"{len(selected) - failures} annotated, {failures} failed, {len(skipped)} skipped; logs in {out}", file=sys.stderr)
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if sum(bool(x) for x in (args.collection, args.doc, args.version)) != 1:
        parser.error("Pass exactly one of --collection, --doc and --version")
    if not args.version and not args.variant:
        parser.error("Pass --variant with --collection or --doc")
    try:
        BatchSpec.parse(args.batch)
        BatchSpec.parse(args.arbiter_batch)
    except ValueError as e:
        parser.error(str(e))
    connection = connection_from_args(args, parser)
    out = args.out or Path("auto-annotation-runs") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        session = ApiSession.connect(connection, timeout=args.timeout)
        if not args.force_busy:
            session.ensure_models_not_busy([spec for spec in (args.model_a, args.model_b, args.arbiter) if spec])
        failures = run(session, args, out)
    except (ApiError, AmbiguousExtractionError, requests.RequestException) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
