"""
Run the annotation pipeline through the server's /complete route, with the rules of the reference
version instead of those of the input version. Nothing is saved on the server.
Usage: pipeline_goldrules.py <out dir> <model A> <model B> <arbiter> <reference> <input>
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pte_plugin_auto_annotation.annotation import AnnotationOutcome
from pte_plugin_auto_annotation.api_client import ApiSession, ServerChatClient
from pte_plugin_auto_annotation.batching import BatchSpec
from pte_plugin_auto_annotation.cli_common import Connection
from pte_plugin_auto_annotation.correction import RetryPolicy
from pte_plugin_auto_annotation.document import write_result
from pte_plugin_auto_annotation.pipeline import PipelineConfig, annotate_units
from pte_plugin_auto_annotation.units import document_units

out, spec_a, spec_b, spec_arb, reference, input_id = sys.argv[1:7]
session = ApiSession.connect(Connection("http://localhost:8000", token=os.environ["API_TOKEN"]), timeout=3600.0)
session.ensure_models_not_busy([spec_a, spec_b, spec_arb])
source = session.eval_source(None, None, reference, input_id, with_rules=True)
units = document_units(source.input_xml, source.unit_path)
models = {}
clients = {}
for label, spec in (("A", spec_a), ("B", spec_b), ("arbiter", spec_arb)):
    provider_id, model_id = session.resolve_model(spec)
    clients[label] = ServerChatClient(session, provider_id, model_id, 0.0)
    models[label] = f"{provider_id}/{model_id}"
config = PipelineConfig(spec=BatchSpec.parse("units:8"), mode="all", arbiter_spec=BatchSpec.parse("units:8"), split_on_failure=0.25, concurrency=2, retry=RetryPolicy(retries=4), segmenting=False)
started = time.monotonic()
result = asyncio.run(annotate_units(units, source.rules, clients["A"], clients["B"], clients["arbiter"], config))
seconds = time.monotonic() - started
when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
xml = write_result(source.input_xml, source.unit_path, result, models, when, seconds)
outcome = AnnotationOutcome(input_stable_id=input_id, doc_id=source.doc_id, variant=source.variant or "", unit_path=source.unit_path, xml=xml, models=models, counts=result.counts(), result=result, rules=[(c, t, "reference") for c, t in source.rules])
Path(out).mkdir(parents=True, exist_ok=True)
Path(out, "doc.xml").write_text(xml, encoding="utf-8")
Path(out, "doc.json").write_text(json.dumps(outcome.log(True), indent=2, ensure_ascii=False), encoding="utf-8")
print(result.counts(), f"{seconds:.0f} s")
