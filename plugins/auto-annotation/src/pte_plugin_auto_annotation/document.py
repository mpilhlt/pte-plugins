"""
Writing a pipeline result into the TEI document (server side, uses the editor's TEI utilities):

- the units are replaced by their results; the rest of the document keeps its formatting
- a `revisionDesc/change` by `#auto-annotation` records the models and the counts
- the decisions become unit review marks of the reviewer `llm-auto-annotation`, in
  `standOff/listAnnotation[@type="unit-review"]` as the editor's unit review mode writes them:
  agreed units are OK, arbitrated and unresolved units are not OK, with a comment
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from lxml import etree

from fastapi_app.lib.utils.tei_utils import add_revision_change, serialize_tei_like_source

from .pipeline import Decision, PipelineResult, UnitDecision
from .units import TEI_NS, document_units, find_unit_elements, parse_document, parse_unit

REVIEWER_ID = "llm-auto-annotation"
CHANGE_WHO = "auto-annotation"
CHANGE_NAME = "Auto-annotation"
LIST_TYPE = "unit-review"
FP_ALGORITHM = "fp-sha256-8-v1"
XML_NS = "http://www.w3.org/XML/1998/namespace"


def _tei(name: str) -> str:
    return f"{{{TEI_NS}}}{name}"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def replace_units(root: etree._Element, unit_path: str | None, replacements: Mapping[int, str]) -> None:
    """
    Replace the units with the given indices (positions in find_unit_elements() order) by new XML.

    Raises:
        ValueError: an index is out of range, or a replacement is not exactly one element.
    """
    elements = find_unit_elements(root, unit_path)
    for index, xml in replacements.items():
        if not 0 <= index < len(elements):
            raise ValueError(f"No unit with index {index}")
        content = parse_unit(xml)
        if len(content) != 1 or isinstance(content[0], str):
            raise ValueError(f"Replacement for unit {index} is not exactly one element")
        old, new = elements[index], content[0]
        new.tail = old.tail
        parent = old.getparent()
        assert parent is not None
        parent.replace(old, new)
    etree.cleanup_namespaces(root)


def _annotation_xml(marks: Sequence[tuple[str, UnitDecision]], software_name: str, when: str, indent: str, unit: str) -> str:
    i1 = indent + unit
    ok = [fp for fp, d in marks if d.decision == Decision.AGREED]
    lines = [
        f'{indent}<annotation xml:id="ur-{REVIEWER_ID}" motivation="assessing" target="#xpath(//text)">',
        f'{i1}<respStmt><resp>LLM Reviewer</resp><name type="software">{_escape(software_name)}</name></respStmt>',
        f'{i1}<revisionDesc><change when="{_escape(when)}"/></revisionDesc>',
    ]
    if ok:
        lines.append(f'{i1}<note type="ok">{" ".join(ok)}</note>')
    for fp, decision in marks:
        if decision.decision != Decision.AGREED:
            lines.append(f'{i1}<note type="not-ok" n="{_escape(fp)}">{_escape(decision.comment)}</note>')
    lines.append(f"{indent}</annotation>")
    return "\n".join(lines)


def add_review_marks(
    root: etree._Element, unit_path: str | None, marks: Sequence[tuple[str, UnitDecision]], software_name: str, when: str
) -> None:
    """
    Add (or replace) the pipeline's annotation in the unit review list, creating `standOff` after the
    `teiHeader` and the list if needed. The list's summary note is written only for a new list (the
    editor recomputes it whenever it saves the marks).
    """
    standoff = next((c for c in root if c.tag == _tei("standOff")), None)
    review_list = None
    if standoff is not None:
        review_list = next((c for c in standoff if c.tag == _tei("listAnnotation") and c.get("type") == LIST_TYPE), None)

    if review_list is None:
        ok = sum(d.decision == Decision.AGREED for _, d in marks)
        summary = f"units={len(marks)} ok={ok} not-ok={len(marks) - ok} disputed=0 open=0"
        path_note = f'\n      <note type="unit-xpath">{_escape(unit_path)}</note>' if unit_path else ""
        list_xml = (
            f'<listAnnotation xmlns="{TEI_NS}" type="{LIST_TYPE}" subtype="{FP_ALGORITHM}">{path_note}\n'
            f'      <note type="summary">{summary}</note>\n'
            f"{_annotation_xml(marks, software_name, when, '      ', '  ')}\n    </listAnnotation>"
        )
        review_list = etree.fromstring(list_xml)
        if standoff is None:
            standoff = etree.Element(_tei("standOff"))
            header = next((c for c in root if c.tag == _tei("teiHeader")), None)
            position = list(root).index(header) + 1 if header is not None else 0
            sibling_tail = header.tail if header is not None and header.tail else "\n  "
            standoff.text = "\n    "
            standoff.tail = sibling_tail
            if header is not None:
                header.tail = sibling_tail
            root.insert(position, standoff)
            review_list.tail = "\n  "
            standoff.append(review_list)
        else:
            last = standoff[-1] if len(standoff) else None
            review_list.tail = last.tail if last is not None else "\n  "
            if last is not None:
                last.tail = "\n    "
            else:
                standoff.text = "\n    "
            standoff.append(review_list)
    else:
        for old in [c for c in review_list if c.tag == _tei("annotation") and c.get(f"{{{XML_NS}}}id") == f"ur-{REVIEWER_ID}"]:
            review_list.remove(old)
        annotation = etree.fromstring(
            _annotation_xml(marks, software_name, when, "", "  ").replace("<annotation ", f'<annotation xmlns="{TEI_NS}" ', 1)
        )
        last = review_list[-1] if len(review_list) else None
        annotation.tail = last.tail if last is not None else "\n    "
        if last is not None:
            last.tail = "\n      "
        review_list.append(annotation)
    etree.cleanup_namespaces(root)


def _indent_appended(element: etree._Element, unit: str = "  ") -> None:
    """Indent an element that was appended as last child without whitespace, as pretty-printing would."""
    parent = element.getparent()
    if parent is None:
        return
    depth = sum(1 for _ in element.iterancestors())
    etree.indent(element, space=unit, level=depth)
    previous = element.getprevious()
    if previous is not None:
        previous.tail = "\n" + unit * depth
    else:
        parent.text = "\n" + unit * depth
    element.tail = "\n" + unit * (depth - 1)


def models_description(models: Mapping[str, str]) -> str:
    """`A: x, B: y, arbiter: z`."""
    return ", ".join(f"{label}: {model}" for label, model in models.items())


def write_result(
    xml: str,
    unit_path: str | None,
    result: PipelineResult,
    models: Mapping[str, str],
    when: str,
    status: str = "extraction",
    label: str | None = "Auto-annotation",
    with_marks: bool = True,
) -> str:
    """
    The document with the results of all changed units, a revision change and (optionally) the
    unit review marks.

    Raises:
        ValueError: the document is not well-formed, or a result cannot be inserted.
    """
    root = parse_document(xml)
    replace_units(root, unit_path, {d.unit.index: d.result_xml for d in result.decisions if d.changed})
    counts = result.counts()
    desc = (
        f"Automated first annotation ({models_description(models)}): {len(result.decisions)} units, "
        f"{counts['agreed']} agreed, {counts['arbitrated']} arbitrated, {counts['unresolved']} unresolved, {counts['changed']} changed"
    )
    has_title_stmt_parent = root.find(f"{_tei('teiHeader')}/{_tei('fileDesc')}") is not None
    # the respStmt that names the pipeline needs a fileDesc; without one the change is still recorded
    revision_desc_path = f"{_tei('teiHeader')}/{_tei('revisionDesc')}"
    had_revision_desc = root.find(revision_desc_path) is not None
    had_resp_stmt = root.find(f".//{_tei('persName')}[@{{{XML_NS}}}id='{CHANGE_WHO}']") is not None
    add_revision_change(root, when, status, CHANGE_WHO, desc, full_name=CHANGE_NAME if has_title_stmt_parent else None, label=label)
    person = root.find(f".//{_tei('persName')}[@{{{XML_NS}}}id='{CHANGE_WHO}']")
    if person is not None and not had_resp_stmt and person.getparent() is not None:
        _indent_appended(person.getparent())  # type: ignore[arg-type]
    revision_desc = root.find(revision_desc_path)
    if revision_desc is not None and len(revision_desc):
        _indent_appended(revision_desc[-1] if had_revision_desc else revision_desc)
    if with_marks:
        fingerprints = {u.index: u.fingerprint for u in document_units(etree.tostring(root, encoding="unicode"), unit_path)}
        marks = [(fingerprints[d.unit.index], d) for d in result.decisions]
        add_review_marks(root, unit_path, marks, f"{CHANGE_NAME} ({models_description(models)})", when)
    return serialize_tei_like_source(root, xml)
