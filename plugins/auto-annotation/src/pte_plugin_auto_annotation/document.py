"""
Writing a pipeline result into the TEI document (server side, uses the editor's TEI utilities):

- the units are replaced by their results; the rest of the document keeps its formatting. For a
  segmenting variant a result may consist of several elements or none (a group's result replaces
  its first unit, the group's other units are removed)
- a `revisionDesc/change` by `#auto-annotation` records the models, the counts and the duration.
  In its `desc`, how often the arbiter chose each version (overall, and where both models gave a
  valid proposal) and how many proposals of each model failed the checks are `measure` elements (`@type`, `@subtype`, `@quantity`, and the model in
  `@commodity`), and the duration is a `time` element with `@dur`, for later analysis
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from lxml import etree

from fastapi_app.lib.utils.tei_utils import add_revision_change, serialize_tei_like_source

from .checks import ProposalStatus
from .pipeline import Decision, PipelineResult, UnitDecision
from .units import TEI_NS, find_unit_elements, parse_document, parse_unit

VALID_STATUSES = (ProposalStatus.OK, ProposalStatus.UNCHANGED)
CHANGE_WHO = "auto-annotation"
CHANGE_NAME = "Auto-annotation"
XML_NS = "http://www.w3.org/XML/1998/namespace"


def _tei(name: str) -> str:
    return f"{{{TEI_NS}}}{name}"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _remove_keeping_tail(element: etree._Element) -> None:
    """Remove an element; text in its tail (not whitespace) moves to the preceding node."""
    parent = element.getparent()
    assert parent is not None
    tail = element.tail or ""
    if tail.strip():
        previous = element.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(element)


def replace_units(root: etree._Element, unit_path: str | None, replacements: Mapping[int, str], segmenting: bool = False) -> None:
    """
    Replace the units with the given indices (positions in find_unit_elements() order) by new XML.
    With segmenting, a replacement may be several elements (separated by the unit's tail
    whitespace) or empty (the unit is removed).

    Raises:
        ValueError: an index is out of range, or a replacement is not exactly one element (with
            segmenting: has text outside its elements).
    """
    elements = find_unit_elements(root, unit_path)
    for index, xml in replacements.items():
        if not 0 <= index < len(elements):
            raise ValueError(f"No unit with index {index}")
        content = parse_unit(xml) if xml.strip() else []
        if any(isinstance(c, str) for c in content):
            raise ValueError(f"Replacement for unit {index} has text outside its elements")
        new = [c for c in content if not isinstance(c, str)]
        if not segmenting and len(new) != 1:
            raise ValueError(f"Replacement for unit {index} is not exactly one element")
        old = elements[index]
        parent = old.getparent()
        assert parent is not None
        if not new:
            _remove_keeping_tail(old)
            continue
        separator = old.tail if old.tail and not old.tail.strip() else "\n"
        position = parent.index(old)
        for offset, element in enumerate(new):
            element.tail = separator
            parent.insert(position + offset, element)
        new[-1].tail = old.tail
        old.tail = None
        parent.remove(old)
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


def iso_duration(seconds: float) -> tuple[str, str]:
    """The duration to the second, as xsd:duration and as text: (`PT4M12S`, `4 min 12 s`)."""
    hours, rest = divmod(round(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    parts = [p for p in ((hours, "H", "h"), (minutes, "M", "min"), (secs, "S", "s")) if p[0]] or [(0, "S", "s")]
    return "PT" + "".join(f"{value}{designator}" for value, designator, _ in parts), " ".join(f"{value} {unit}" for value, _, unit in parts)


def _measure(type_: str, subtype: str, quantity: int, commodity: str | None = None) -> str:
    model = f' commodity="{_escape(commodity)}"' if commodity else ""
    return f'<measure type="{type_}" subtype="{subtype}"{model} quantity="{quantity}">{subtype} {quantity}</measure>'


def description_xml(result: PipelineResult, models: Mapping[str, str], seconds: float) -> str:
    """The content of the change's `desc`: readable text with the machine-readable values as elements."""
    counts = result.counts()
    fallback = f"{counts['fallback']} fallback, " if counts["fallback"] else ""
    parts = [_escape(
        f"Automated first annotation ({models_description(models)}): {len(result.decisions)} units, "
        f"{counts['agreed']} agreed, {counts['arbitrated']} arbitrated, {fallback}{counts['unresolved']} unresolved, {counts['changed']} changed."
    )]
    if "arbiter" in models:
        arbitrated = [d for d in result.decisions if d.decision == Decision.ARBITRATED]
        both_valid = [d for d in arbitrated if all(d.statuses.get(label) in VALID_STATUSES for label in ("A", "B"))]
        choices = [("A", models.get("A")), ("B", models.get("B")), ("own", models["arbiter"]), ("original", None)]

        def tally(type_: str, decisions: list[UnitDecision]) -> str:
            chosen = Counter(d.chosen for d in decisions)
            return ", ".join(_measure(type_, label, chosen[label], model) for label, model in choices)

        parts.append(f"Arbiter chose {tally('arbiter-choice', arbitrated)}; with valid proposals from both models: {tally('arbiter-choice-both-valid', both_valid)}.")
    invalid = ", ".join(
        _measure("invalid-proposal", label, sum(d.statuses[label].is_failure for d in result.decisions if label in d.statuses), models.get(label))
        for label in ("A", "B")
    )
    parts.append(f"Invalid proposals: {invalid}.")
    iso, text = iso_duration(seconds)
    parts.append(f'Completed in <time dur="{iso}">{text}</time>.')
    return " ".join(parts)


def write_result(
    xml: str,
    unit_path: str | None,
    result: PipelineResult,
    models: Mapping[str, str],
    when: str,
    seconds: float,
    status: str = "extraction",
    label: str | None = "Auto-annotation",
) -> str:
    """
    The document with the results of all changed units and a revision change. `seconds` is how
    long the annotation took.

    Raises:
        ValueError: the document is not well-formed, or a result cannot be inserted.
    """
    root = parse_document(xml)
    replacements: dict[int, str] = {}
    for d in result.decisions:
        if d.changed:
            first, *rest = d.covered
            replacements[first] = d.result_xml
            replacements.update({i: "" for i in rest})
    replace_units(root, unit_path, replacements, segmenting=result.segmenting)
    has_title_stmt_parent = root.find(f"{_tei('teiHeader')}/{_tei('fileDesc')}") is not None
    # the respStmt that names the pipeline needs a fileDesc; without one the change is still recorded
    revision_desc_path = f"{_tei('teiHeader')}/{_tei('revisionDesc')}"
    had_revision_desc = root.find(revision_desc_path) is not None
    had_resp_stmt = root.find(f".//{_tei('persName')}[@{{{XML_NS}}}id='{CHANGE_WHO}']") is not None
    add_revision_change(root, when, status, CHANGE_WHO, "", full_name=CHANGE_NAME if has_title_stmt_parent else None, label=label)
    person = root.find(f".//{_tei('persName')}[@{{{XML_NS}}}id='{CHANGE_WHO}']")
    if person is not None and not had_resp_stmt and person.getparent() is not None:
        _indent_appended(person.getparent())  # type: ignore[arg-type]
    revision_desc = root.find(revision_desc_path)
    if revision_desc is not None and len(revision_desc):
        _indent_appended(revision_desc[-1] if had_revision_desc else revision_desc)
        # add_revision_change() takes plain text; the description has elements, so it is set here
        change = revision_desc[-1]
        placeholder = change.find(_tei("desc"))
        if placeholder is not None:
            desc = etree.fromstring(f'<desc xmlns="{TEI_NS}">{description_xml(result, models, seconds)}</desc>')
            desc.tail = placeholder.tail
            change.replace(placeholder, desc)
            etree.cleanup_namespaces(root)
    return serialize_tei_like_source(root, xml)
