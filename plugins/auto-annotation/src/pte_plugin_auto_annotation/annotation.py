"""
Server side: annotating one stored version, and finding the versions of a collection to annotate.

The result is returned, not saved: the client saves it as a new version of the input through the
editor's save API, which takes care of version numbers, collections, locks and events.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

from fastapi_app.lib.core.url_cache import UrlCache
from fastapi_app.lib.models.models import FileMetadata
from fastapi_app.lib.permissions.access_control import check_file_access
from fastapi_app.lib.repository.file_repository import FileRepository
from fastapi_app.lib.storage.file_storage import FileStorage

from .correction import CorrectionRun, ProviderChatClient
from .document import CHANGE_WHO, write_result
from .models import resolve_model_for_user
from .pipeline import PipelineConfig, PipelineResult, annotate_units
from .sources import SourceError, gather_rules, is_segmenting_variant, read_tei, review_unit_path, tei_file
from .units import document_units

logger = logging.getLogger(__name__)

_MARKER = f'who="#{CHANGE_WHO}"'.encode()


@dataclass
class Target:
    """A document of a collection and the version the pipeline would annotate."""

    doc_id: str
    input_stable_id: str | None
    # "ready", or why the document is skipped: "annotated", "gold", "no-version"
    state: str
    annotated_stable_id: str | None = None


def is_auto_annotated(content: bytes) -> bool:
    """Whether a version records a change by the pipeline."""
    return _MARKER in content


def find_targets(
    repo: FileRepository, storage: FileStorage, user: dict[str, object], variant: str,
    collection: str | None = None, doc_ids: list[str] | None = None,
) -> list[Target]:
    """
    One target per document (of the collection, or the given ids) with a TEI version of the variant:
    the earliest version (normally the extraction), unless a version of the variant was already
    annotated by the pipeline ("annotated") or the document has a gold version of it ("gold").
    Versions the user cannot read are left out.
    """
    if doc_ids is None:
        files = repo.get_files_by_collection(collection) if collection else []
        doc_ids = sorted({f.doc_id for f in files if f.file_type == "tei" and f.variant == variant})
    targets: list[Target] = []
    for doc_id in doc_ids:
        files = [f for f in repo.get_files_by_doc_id(doc_id) if f.file_type == "tei" and f.variant == variant and check_file_access(f, user, "read")]
        versions = sorted((f for f in files if not f.is_gold_standard), key=lambda f: (f.version or 0, str(f.created_at)))
        if not versions:
            targets.append(Target(doc_id, None, "no-version"))
            continue
        annotated = next((v for v in versions if (c := storage.read_file(v.id, "tei")) is not None and is_auto_annotated(c)), None)
        if annotated is not None:
            targets.append(Target(doc_id, versions[0].stable_id, "annotated", annotated.stable_id))
        elif any(f.is_gold_standard for f in files):
            targets.append(Target(doc_id, versions[0].stable_id, "gold"))
        else:
            targets.append(Target(doc_id, versions[0].stable_id, "ready"))
    return targets


@dataclass
class AnnotationOutcome:
    """The annotated document and what happened to each unit."""

    input_stable_id: str
    doc_id: str
    variant: str | None
    unit_path: str | None
    xml: str
    models: dict[str, str]
    counts: dict[str, int]
    result: PipelineResult
    rules: list[tuple[str, str, str]]

    def log(self, include_raw: bool = False) -> dict[str, object]:
        """The run log: decisions per unit and the requests of each model."""
        def run_log(run: CorrectionRun) -> dict[str, object]:
            batches = [asdict(b) for b in run.batches]
            if not include_raw:
                for batch in batches:
                    batch.pop("raw_response", None)
            return {
                "calls": len(run.batches),
                "retries": sum(b.retries for b in run.batches),
                "seconds": round(sum(b.seconds for b in run.batches), 2),
                "prompt_chars": sum(b.prompt_chars for b in run.batches),
                "response_chars": sum(b.response_chars for b in run.batches),
                "batches": batches,
            }

        return {
            "input_stable_id": self.input_stable_id,
            "doc_id": self.doc_id,
            "variant": self.variant,
            "unit_path": self.unit_path,
            "models": self.models,
            "rules": [{"category": c, "url": u} for c, _, u in self.rules],
            "counts": self.counts,
            "units": [
                {
                    "index": d.unit.index, "indices": list(d.covered), "fingerprint": d.unit.fingerprint, "decision": d.decision.value,
                    "changed": d.changed, "statuses": {k: v.value for k, v in d.statuses.items()},
                    "arbiter_status": d.arbiter_status.value if d.arbiter_status else None,
                    "chosen": d.chosen, "comment": d.comment,
                }
                for d in self.result.decisions
            ],
            "runs": {label: run_log(run) for label, run in self.result.runs.items()},
        }


def load_input(repo: FileRepository, storage: FileStorage, stable_id: str, user: dict[str, object]) -> tuple[FileMetadata, str]:
    """
    The version to annotate and its content.

    Raises:
        SourceError: missing, not readable by the user, or a gold version.
    """
    file = tei_file(repo, stable_id)
    if not check_file_access(file, user, "read"):
        raise SourceError(f"No read access to {stable_id}", 403)
    if file.is_gold_standard:
        raise SourceError(f"{stable_id} is a gold version; annotate one of its versions instead")
    return file, read_tei(storage, file)


async def annotate_file(
    stable_id: str,
    user: dict[str, object],
    repo: FileRepository,
    storage: FileStorage,
    cache: UrlCache,
    model_a: str,
    model_b: str,
    arbiter: str | None,
    config: PipelineConfig,
    marks: bool = True,
    status: str = "extraction",
    label: str | None = "Auto-annotation",
) -> AnnotationOutcome:
    """
    Annotate a stored version with the pipeline and return the new document (not saved). Units are
    split and merged if the variant's extractor declares it segmenting (config.segmenting is
    overridden).

    Raises:
        SourceError: see load_input() and gather_rules(), or the version has no review units.
        ModelResolutionError: a model cannot be used.
    """
    file, xml = load_input(repo, storage, stable_id, user)
    unit_path = review_unit_path(file.variant)
    try:
        units = document_units(xml, unit_path)
    except ValueError as e:
        raise SourceError(f"{stable_id}: {e}") from e
    if not units:
        raise SourceError(f"{stable_id} has no review units (unit path {unit_path or 'auto'})")
    rules = gather_rules(xml, cache)

    resolved = {"A": model_a, "B": model_b, **({"arbiter": arbiter} if arbiter else {})}
    clients: dict[str, ProviderChatClient] = {}
    models: dict[str, str] = {}
    for label_, spec in resolved.items():
        provider, model_id = resolve_model_for_user(spec, user)
        clients[label_] = ProviderChatClient(provider, model_id)
        models[label_] = f"{provider.id}/{model_id}"

    config = replace(config, segmenting=is_segmenting_variant(file.variant))
    result = await annotate_units(
        units, [(c, t) for c, t, _ in rules], clients["A"], clients["B"], clients.get("arbiter"), config
    )
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_xml = write_result(xml, unit_path, result, models, when, status=status, label=label, with_marks=marks)
    logger.info(f"auto-annotation: {stable_id} annotated ({result.counts()})")
    return AnnotationOutcome(
        input_stable_id=file.stable_id, doc_id=file.doc_id, variant=file.variant, unit_path=unit_path,
        xml=new_xml, models=models, counts=result.counts(), result=result, rules=rules,
    )
