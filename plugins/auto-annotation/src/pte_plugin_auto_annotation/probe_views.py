"""
HTML pages of the stored model tests: the page of a run (its scores and the comparison with other
runs) and the list of all runs. Rendered by probe_routes.py.

Pure module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote, urlencode

from .evaluation import Summary
from .probe_compare import RunOverview, model_key
from .probe_store import RESULT_FILES, ProbeStatus
from .report import INPUT_CHOICES

BASE = "/api/plugins/auto-annotation/probe"

NOT_GOLD = '<span class="badge warn">not gold</span>'

STATE_LABELS = {
    "running": "running",
    "done": "finished",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}

CSS = """
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 14px; color: #1f2933; margin: 16px; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 24px 0 6px; }
p.note { color: #52606d; margin: 4px 0 8px; max-width: 70em; }
table { border-collapse: collapse; margin: 6px 0 12px; }
th, td { border: 1px solid #d9e2ec; padding: 3px 8px; text-align: right; white-space: nowrap; }
th { background: #f0f4f8; font-weight: 600; }
td.text, th.text { text-align: left; }
tr.baseline td { color: #52606d; font-style: italic; }
tr.best td { background: #e3f8e8; }
tr.current td { font-weight: 600; }
dl.meta { display: grid; grid-template-columns: max-content auto; gap: 2px 16px; margin: 8px 0; }
dl.meta dt { color: #52606d; }
dl.meta dd { margin: 0; }
.badge { display: inline-block; padding: 0 6px; border-radius: 8px; font-size: 12px; background: #e4e7eb; }
.badge.warn { background: #fff3c4; }
.badge.danger { background: #ffe3e3; }
.badge.ok { background: #e3f8e8; }
.links a { margin-right: 12px; }
.error { color: #ab091e; }
button.delete { margin-left: 12px; }
"""


def _pct(value: object) -> str:
    return "–" if not isinstance(value, (int, float)) else f"{100 * value:.1f}"


def _text(value: object) -> str:
    return escape("" if value is None else str(value))


def _time(value: object) -> str:
    """An ISO timestamp as "2026-11-13 08:07 UTC"."""
    try:
        return datetime.fromisoformat(str(value)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return _text(value)


def _page(title: str, body: str, refresh_seconds: int | None = None) -> str:
    refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds else ""
    return (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">{refresh}<title>{escape(title)}</title>'
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def run_url(run_id: str) -> str:
    return f"{BASE}/view?run={quote(run_id)}"


def history_url(model: str | None = None) -> str:
    return f"{BASE}/history" + (f"?{urlencode({'model': model})}" if model else "")


def export_url(run_id: str, file: str = "zip") -> str:
    return f"{BASE}/export?{urlencode({'run': run_id, 'file': file})}"


def _state_badge(status: ProbeStatus) -> str:
    cls = {"done": "ok", "running": "", "cancelled": "warn", "interrupted": "warn", "failed": "danger"}[status.state]
    label = STATE_LABELS[status.state]
    if status.state != "done" and status.planned_runs:
        label += f", {status.finished_runs} of {status.planned_runs} runs"
    return f'<span class="badge {cls}">{escape(label)}</span>'


def _model_label(meta: Mapping[str, object]) -> str:
    return str(meta.get("model_label") or model_key(meta))


def _document_label(meta: Mapping[str, object]) -> str:
    return str(meta.get("doc_label") or meta.get("title") or meta.get("doc_id") or meta.get("reference_stable_id"))


SUMMARY_HEADERS = ["Batching", "Mode", "Runs", "Calls", "Retries", "Errors %", "Failures %", "Exact %", "Edit P", "Edit R", "Edit F1", "Field F1", "Edit R by quarter", "Seconds"]


def _summary_row(s: Mapping[str, object], css: str = "") -> str:
    quarters = s.get("edit_recall_by_quarter")
    quarter_text = " / ".join(_pct(q) for q in quarters) if isinstance(quarters, list) and s.get("calls") else "–"
    seconds = s.get("seconds")
    cells = [
        f'<td class="text">{_text(s.get("batching"))}</td>', f'<td class="text">{_text(s.get("mode"))}</td>',
        f"<td>{_text(s.get('repeats'))}</td>", f"<td>{_text(s.get('calls'))}</td>", f"<td>{_text(s.get('retries'))}</td>",
        f"<td>{_pct(s.get('error_rate'))}</td>", f"<td>{_pct(s.get('failure_rate'))}</td>", f"<td>{_pct(s.get('exact_rate'))}</td>",
        f"<td>{_pct(s.get('edit_precision'))}</td>", f"<td>{_pct(s.get('edit_recall'))}</td>", f"<td>{_pct(s.get('edit_f1'))}</td>",
        f"<td>{_pct(s.get('field_f1'))}</td>", f"<td>{quarter_text}</td>",
        f"<td>{seconds:.1f}</td>" if isinstance(seconds, (int, float)) else "<td>–</td>",
    ]
    return f'<tr class="{css}">{"".join(cells)}</tr>'


def _summary_table(rows: Sequence[Mapping[str, object]], baseline: Mapping[str, object] | None, best: Mapping[str, object] | None) -> str:
    head = "".join(f'<th class="{"text" if i < 2 else ""}">{h}</th>' for i, h in enumerate(SUMMARY_HEADERS))
    body = [_summary_row({**baseline, "batching": "GROBID (input)", "mode": "–"}, "baseline")] if baseline else []
    body += [_summary_row(s, "best" if best is not None and s is best else "") for s in rows]
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


POOLED_HEADERS = ["Batching", "Mode", "Tests", "Units", "Errors %", "Failures %", "Exact %", "Edit P", "Edit R", "Edit F1", "Field F1"]


def _pooled_table(pooled: Sequence[Summary]) -> str:
    """Scores pooled over several tests: no request counts or durations, which summarize() leaves empty."""
    head = "".join(f'<th class="{"text" if i < 2 else ""}">{h}</th>' for i, h in enumerate(POOLED_HEADERS))
    best = max((s for s in pooled if s.edit_f1 is not None), key=lambda s: s.edit_f1 or 0.0, default=None)
    rows = [
        f'<tr class="{"best" if s is best else ""}"><td class="text">{_text(s.batching)}</td><td class="text">{_text(s.mode)}</td>'
        f"<td>{s.repeats}</td><td>{s.units}</td><td>{_pct(s.error_rate)}</td><td>{_pct(s.failure_rate)}</td><td>{_pct(s.exact_rate)}</td>"
        f"<td>{_pct(s.edit_precision)}</td><td>{_pct(s.edit_recall)}</td><td>{_pct(s.edit_f1)}</td><td>{_pct(s.field_f1)}</td></tr>"
        for s in pooled
    ]
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _best_cells(item: RunOverview) -> str:
    best = item.best
    if best is None:
        return '<td class="text">–</td><td>–</td><td>–</td><td>–</td><td>–</td>'
    baseline = item.baseline or {}
    return (
        f'<td class="text">{_text(best.get("batching"))}, {_text(best.get("mode"))}</td>'
        f"<td>{_pct(best.get('edit_f1'))}</td><td>{_pct(best.get('field_f1'))}</td>"
        f"<td>{_pct(baseline.get('field_f1'))}</td><td>{_pct(best.get('failure_rate'))}</td>"
    )


def _comparability(item: RunOverview, current: Mapping[str, object], same_document: bool) -> str:
    notes = []
    if item.meta.get("prompt_version") != current.get("prompt_version"):
        notes.append(f"prompt v{_text(item.meta.get('prompt_version'))}")
    if same_document and item.meta.get("rules_hash") != current.get("rules_hash"):
        notes.append("other rules")
    if not item.meta.get("reference_is_gold"):
        notes.append("reference not gold")
    return f'<span class="badge warn">{", ".join(notes)}</span>' if notes else ""


def render_run_page(
    item: RunOverview,
    same_model: Sequence[RunOverview],
    pooled: Sequence[Summary],
    same_document: Sequence[RunOverview],
    can_delete: bool,
) -> str:
    """The page of a run: parameters, scores, and the comparison with the other stored runs."""
    meta = item.meta
    status = item.run.status
    run_id = item.run.run_id
    params = meta.get("params") if isinstance(meta.get("params"), dict) else {}
    assert isinstance(params, dict)
    title = f"{_model_label(meta)} on {_document_label(meta)}"
    reference_note = "gold version" if meta.get("reference_is_gold") else '<span class="badge warn">not a gold version</span>'
    parts = [
        f"<h1>Model test: {escape(title)}</h1>",
        f'<p>{_state_badge(status)} started {_time(status.started_at)} by {_text(meta.get("username"))}'
        + (f", finished {_time(status.finished_at)}" if status.finished_at else "") + "</p>",
    ]
    if status.error:
        parts.append(f'<p class="error">{escape(status.error)}</p>')
    parts.append(
        '<dl class="meta">'
        f"<dt>Model</dt><dd>{_text(_model_label(meta))} <code>{_text(model_key(meta))}</code></dd>"
        f"<dt>Document</dt><dd>{_text(_document_label(meta))} <code>{_text(meta.get('doc_id'))}</code>, variant <code>{_text(meta.get('variant'))}</code></dd>"
        f"<dt>Reference</dt><dd><code>{_text(meta.get('reference_stable_id'))}</code> ({reference_note})</dd>"
        f"<dt>Input</dt><dd><code>{_text(meta.get('input_stable_id'))}</code> ({_text(INPUT_CHOICES.get(str(meta.get('input_choice')), meta.get('input_choice')))})</dd>"
        f"<dt>Units</dt><dd>{_text(meta.get('aligned_units'))} aligned</dd>"
        f"<dt>Parameters</dt><dd>batches {_text(', '.join(map(str, params.get('batches', []))))}; modes {_text(', '.join(map(str, params.get('modes', []))))}; "
        f"{_text(params.get('repeats'))} repetition(s); concurrency {_text(params.get('concurrency'))}; temperature {_text(params.get('temperature'))}"
        + (f"; first {_text(params.get('max_units'))} units only" if params.get("max_units") else "") + "</dd>"
        f"<dt>Prompt, rules</dt><dd>prompt v{_text(meta.get('prompt_version'))}, rules <code>{_text(meta.get('rules_hash'))}</code></dd>"
        "</dl>"
    )
    links = [f'<a href="{export_url(run_id, name)}">{name}</a>' for name in RESULT_FILES if name != "meta.json"]
    parts.append(
        f'<p class="links">Download: <a href="{export_url(run_id)}">all files (zip)</a> {" ".join(links)} '
        f'· <a href="{history_url()}">All model tests</a>'
        + (f'<button class="delete" id="delete-run">Delete this test</button>' if can_delete and status.state != "running" else "")
        + "</p>"
    )

    parts.append("<h2>Scores of this test</h2>")
    if item.summaries:
        parts.append(_summary_table(item.summaries, item.baseline, item.best))
        parts.append(
            '<p class="note">Edit P/R: precision and recall of the changes the model made to the input, against the changes the reference needs. '
            "Errors: units whose request failed; left out of all other columns. Failures: units answered without a usable proposal, scored as unchanged input. "
            "The first row scores the input itself; the highlighted row has the highest edit F1.</p>"
        )
    else:
        parts.append('<p class="note">No run has finished yet.</p>')

    parts.append(f"<h2>{escape(_model_label(meta))} on other documents</h2>")
    others = [o for o in same_model if o.run.run_id != run_id]
    if pooled:
        parts.append(
            f'<p class="note">Pooled over the units of the latest finished test of this model on each of {max(s.repeats for s in pooled)} reference version(s), with prompt v{_text(meta.get("prompt_version"))}. '
            "Tests: number of tests pooled.</p>"
        )
        parts.append(_pooled_table(pooled))
    if others or same_model:
        head = '<th class="text">Started</th><th class="text">Document</th><th class="text">State</th><th class="text">Best configuration</th><th>Edit F1</th><th>Field F1</th><th>Input field F1</th><th>Failures %</th><th class="text"></th>'
        rows = []
        for o in same_model:
            css = "current" if o.run.run_id == run_id else ""
            rows.append(
                f'<tr class="{css}"><td class="text"><a href="{run_url(o.run.run_id)}">{_time(o.run.status.started_at)}</a></td>'
                f'<td class="text">{_text(_document_label(o.meta))}</td><td class="text">{_state_badge(o.run.status)}</td>'
                f'{_best_cells(o)}<td class="text">{_comparability(o, meta, False)}</td></tr>'
            )
        parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>")
    if not others:
        parts.append('<p class="note">This model has not been tested on other documents yet.</p>')

    parts.append("<h2>Other models on this document</h2>")
    models = [o for o in same_document if model_key(o.meta) != model_key(meta)]
    if models:
        head = '<th class="text">Model</th><th class="text">Started</th><th class="text">State</th><th class="text">Best configuration</th><th>Edit F1</th><th>Field F1</th><th>Input field F1</th><th>Failures %</th><th class="text"></th>'
        rows = [
            f'<tr><td class="text"><a href="{run_url(o.run.run_id)}">{_text(_model_label(o.meta))}</a></td><td class="text">{_time(o.run.status.started_at)}</td>'
            f'<td class="text">{_state_badge(o.run.status)}</td>{_best_cells(o)}<td class="text">{_comparability(o, meta, True)}</td></tr>'
            for o in models
        ]
        parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>")
        parts.append('<p class="note">The latest test of each model on the same reference version.</p>')
    else:
        parts.append('<p class="note">No other model has been tested on this reference version yet.</p>')

    if can_delete:
        parts.append(
            "<script>"
            "const button = document.getElementById('delete-run');"
            "if (button) button.addEventListener('click', async () => {"
            "  if (!window.confirm('Delete this model test and its results?')) return;"
            f"  const response = await fetch('{BASE}/delete', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{run_id: {run_id!r}}})}});"
            f"  if (response.ok) window.location.href = '{history_url()}';"
            "  else window.alert('The test could not be deleted: ' + (await response.text()));"
            "});"
            "</script>"
        )
    return _page(f"Model test: {title}", "".join(parts), refresh_seconds=10 if status.state == "running" else None)


def render_history_page(items: Sequence[RunOverview], model: str | None, models: Mapping[str, str]) -> str:
    """All stored tests, newest first, optionally of one model; `models` maps model keys to labels for the filter links."""
    parts = ["<h1>Model tests</h1>"]
    filters = [f'<a href="{history_url()}">{"<b>all</b>" if model is None else "all"}</a>']
    for key, label in sorted(models.items(), key=lambda kv: kv[1].lower()):
        text = escape(label)
        filters.append(f'<a href="{history_url(key)}">{f"<b>{text}</b>" if key == model else text}</a>')
    parts.append(f'<p class="links">Model: {" ".join(filters)}</p>')
    if not items:
        parts.append('<p class="note">No model test has been run yet. Start one with Tools → Inference → Test model with current document.</p>')
        return _page("Model tests", "".join(parts))
    head = '<th class="text">Started</th><th class="text">Model</th><th class="text">Document</th><th class="text">By</th><th class="text">State</th><th class="text">Best configuration</th><th>Edit F1</th><th>Field F1</th><th>Input field F1</th><th>Failures %</th><th class="text">Reference</th>'
    rows = [
        f'<tr><td class="text"><a href="{run_url(o.run.run_id)}">{_time(o.run.status.started_at)}</a></td>'
        f'<td class="text">{_text(_model_label(o.meta))}</td><td class="text">{_text(_document_label(o.meta))}</td>'
        f'<td class="text">{_text(o.meta.get("username"))}</td><td class="text">{_state_badge(o.run.status)}</td>{_best_cells(o)}'
        f'<td class="text">{"gold" if o.meta.get("reference_is_gold") else NOT_GOLD}</td></tr>'
        for o in items
    ]
    parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>")
    refresh = 10 if any(o.run.status.state == "running" for o in items) else None
    return _page("Model tests", "".join(parts), refresh_seconds=refresh)
