"""
Review units of a TEI document: finding them, serializing them for a prompt, and comparing them.

The unit path syntax and the canonical form follow the editor's unit review mode
(`app/src/modules/unit-review.js` in pdf-tei-editor), so that a fingerprint computed here
matches the one the editor stores for the same unit. One known difference: the editor reads
entity and character references in their source form, lxml decodes them, and this module
re-escapes only `&`, `<` and `>` (and `"` in attributes). Units that use other references
(e.g. `&#160;`) get a different fingerprint.

Pure module: no imports from fastapi_app, so the probe CLI can use it outside the server.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from lxml import etree

TEI_NS = "http://www.tei-c.org/ns/1.0"
AUTO_UNIT_PATH = "auto"

_WS_RE = re.compile(r"\s+")
_STEP_RE = re.compile(r"(//?)([^/]+)")
_NAME_RE = re.compile(r"^(\*|[A-Za-z_][\w.-]*)$")
_TEI_XMLNS_RE = re.compile(r'\s+xmlns="' + re.escape(TEI_NS) + '"')


@dataclass(frozen=True)
class PathStep:
    """One step of a unit path: an element name or `*`, as child (`/`) or descendant (`//`)."""

    name: str
    descendant: bool


@dataclass(frozen=True)
class Unit:
    """A review unit: its position among the document's units and its serialized XML."""

    index: int
    xml: str
    fingerprint: str


def local_name(tag: object) -> str:
    """Local name of an lxml tag (`{ns}bibl` -> `bibl`); '' for comments and processing instructions."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def collapse(text: str) -> str:
    """Collapse whitespace runs to one space and trim."""
    return _WS_RE.sub(" ", text).strip()


def element_text(element: etree._Element) -> str:
    """All text of the element and its descendants."""
    return "".join(str(t) for t in element.itertext())


def attributes(element: etree._Element) -> list[tuple[str, str]]:
    """The element's attributes as (name, value), names as returned by attr_name()."""
    return [(attr_name(str(k)), str(v)) for k, v in element.attrib.items()]


def text_key(element: etree._Element) -> str:
    """The element's text with all whitespace removed: used to align units and check text invariance."""
    return _WS_RE.sub("", element_text(element))


def parse_unit_path(path: str | None) -> list[PathStep] | None:
    """
    Parse a unit path such as `.//listBibl/bibl` or `./*` into steps. Returns None for the automatic
    default (`None`, '' or `auto`).

    Raises:
        ValueError: the path is not valid.
    """
    if not path or path == AUTO_UNIT_PATH:
        return None
    rest = path.strip()
    if rest.startswith("."):
        rest = rest[1:]
    if not rest.startswith("/"):
        rest = "/" + rest
    steps: list[PathStep] = []
    consumed = 0
    for match in _STEP_RE.finditer(rest):
        if match.start() != consumed:
            break
        consumed = match.end()
        name = match.group(2).split(":")[-1]
        if not _NAME_RE.match(name):
            raise ValueError(f'Invalid unit path step "{match.group(2)}" in "{path}"')
        steps.append(PathStep(name=name, descendant=match.group(1) == "//"))
    if consumed != len(rest) or not steps:
        raise ValueError(f'Invalid unit path "{path}"')
    return steps


def _matches(steps: list[PathStep], chain: list[str]) -> bool:
    def match(s: int, c: int) -> bool:
        if s == len(steps):
            return c == len(chain)
        if c == len(chain):
            return False
        step = steps[s]
        if (step.name == "*" or step.name == chain[c]) and match(s + 1, c + 1):
            return True
        return step.descendant and match(s, c + 1)

    return match(0, 0)


def _child_elements(element: etree._Element) -> list[etree._Element]:
    return [child for child in element if isinstance(child.tag, str)]


def _has_own_text(element: etree._Element) -> bool:
    if element.text and element.text.strip():
        return True
    return any(child.tail and child.tail.strip() for child in element)


def _auto_units(text: etree._Element) -> list[etree._Element]:
    """The editor's default: step down while there is exactly one non-empty child element, then take the children."""
    current = text
    while True:
        children = _child_elements(current)
        filled = [c for c in children if _child_elements(c) or _has_own_text(c)]
        if len(filled) == 1 and not _has_own_text(current) and _child_elements(filled[0]):
            current = filled[0]
        else:
            return children


def find_text_element(root: etree._Element) -> etree._Element | None:
    """The TEI `<text>` element of a document."""
    if local_name(root.tag) == "text":
        return root
    return next((el for el in root.iter() if local_name(el.tag) == "text"), None)


def find_unit_elements(root: etree._Element, unit_path: str | None) -> list[etree._Element]:
    """The unit elements below `<text>`, in document order. Units do not nest: below a match the walk stops."""
    text = find_text_element(root)
    if text is None:
        return []
    steps = parse_unit_path(unit_path)
    if steps is None:
        return _auto_units(text)
    found: list[etree._Element] = []

    def walk(element: etree._Element, chain: list[str]) -> None:
        for child in _child_elements(element):
            child_chain = [*chain, local_name(child.tag)]
            if _matches(steps, child_chain):
                found.append(child)
            else:
                walk(child, child_chain)

    walk(text, [])
    return found


def _escape_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def canonical_form(element: etree._Element) -> str:
    """Tag names, attributes sorted by name, text collapsed per text run, no comments or PIs (see module doc)."""
    name = local_name(element.tag)
    attrs = sorted((k, collapse(v)) for k, v in attributes(element))
    out = "<" + name + "".join(f' {k}="{_escape_text(v).replace(chr(34), "&quot;")}"' for k, v in attrs) + ">"
    run = element.text or ""
    for child in element:
        if isinstance(child.tag, str):
            if collapse(run):
                out += _escape_text(collapse(run))
            run = ""
            out += canonical_form(child)
        run += child.tail or ""
    if collapse(run):
        out += _escape_text(collapse(run))
    return out + "</" + name + ">"


def attr_name(qname: str) -> str:
    """Attribute name with the `xml:` prefix kept and other namespaces dropped."""
    if qname.startswith("{http://www.w3.org/XML/1998/namespace}"):
        return "xml:" + qname.rsplit("}", 1)[-1]
    return qname.rsplit("}", 1)[-1]


def fingerprint_of(element: etree._Element) -> str:
    """First 8 hex characters of the SHA-256 of the canonical form (without the duplicate suffix)."""
    return hashlib.sha256(canonical_form(element).encode("utf-8")).hexdigest()[:8]


def serialize_unit(element: etree._Element) -> str:
    """The unit as XML without its tail and without the TEI namespace declaration."""
    xml = etree.tostring(element, encoding="unicode", with_tail=False)
    return _TEI_XMLNS_RE.sub("", xml, count=1)


def parse_document(xml: str) -> etree._Element:
    """
    Parse a TEI document.

    Raises:
        ValueError: not well-formed.
    """
    try:
        return etree.fromstring(xml.encode("utf-8"), parser=etree.XMLParser(remove_blank_text=False, resolve_entities=False))
    except etree.XMLSyntaxError as e:
        raise ValueError(f"Document is not well-formed XML: {e}") from e


def parse_unit(xml: str) -> list[etree._Element | str]:
    """
    Parse a unit's XML (as produced by serialize_unit() or returned by a model) in the TEI namespace.
    Returns the top-level content: elements, and non-blank text outside any element as strings.

    Raises:
        ValueError: not well-formed.
    """
    wrapped = f'<wrap xmlns="{TEI_NS}" xmlns:xml="http://www.w3.org/XML/1998/namespace">{xml}</wrap>'
    try:
        wrapper = etree.fromstring(wrapped.encode("utf-8"), parser=etree.XMLParser(resolve_entities=False))
    except etree.XMLSyntaxError as e:
        raise ValueError(f"not well-formed: {e}") from e
    content: list[etree._Element | str] = []
    if wrapper.text and wrapper.text.strip():
        content.append(wrapper.text.strip())
    for child in wrapper:
        if isinstance(child.tag, str):
            content.append(child)
        if child.tail and child.tail.strip():
            content.append(child.tail.strip())
    return content


def document_units(xml: str, unit_path: str | None) -> list[Unit]:
    """
    The review units of a document, with fingerprints (`.2`, `.3` ... for repeated identical units).

    Raises:
        ValueError: not well-formed, or invalid unit path.
    """
    root = parse_document(xml)
    seen: dict[str, int] = {}
    units: list[Unit] = []
    for index, element in enumerate(find_unit_elements(root, unit_path)):
        base = fingerprint_of(element)
        seen[base] = seen.get(base, 0) + 1
        fp = base if seen[base] == 1 else f"{base}.{seen[base]}"
        units.append(Unit(index=index, xml=serialize_unit(element), fingerprint=fp))
    return units
