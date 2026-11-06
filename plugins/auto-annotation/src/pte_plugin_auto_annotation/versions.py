"""
Telling extractions from annotated versions by their revision history.

The editor records every saved change of a version in `teiHeader/revisionDesc`. An extraction
starts with a `<change status="extraction">`; a version copied from it and then annotated keeps
that first change and adds its own. Neither the label nor the version's status tell the two
apart reliably (extractions are renamed, annotators correct a copy that still says "Extraction"),
so the revision history decides.

Pure module.
"""

from __future__ import annotations

from lxml import etree

from .units import TEI_NS

# Descriptions of changes the editor records for metadata-only edits, which leave the annotation
# as it is: renaming a version (app/src/plugins/xmleditor.js) and updating its document rules
# (fastapi_app/lib/doc_rules/rules_refresh.py).
METADATA_CHANGE_PREFIXES = ("Renamed to ", "Updated document rules")


def is_extraction(xml: str) -> bool:
    """
    Whether a TEI document is an extraction: its first change has status "extraction" and all
    later changes are metadata-only edits (see METADATA_CHANGE_PREFIXES).

    Args:
        xml: The TEI document

    Returns:
        False for documents that cannot be parsed or have no revision history
    """
    try:
        root = etree.fromstring(xml.encode("utf-8"), etree.XMLParser(recover=False, resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return False
    changes = root.findall(f".//{{{TEI_NS}}}revisionDesc/{{{TEI_NS}}}change")
    if not changes or changes[0].get("status") != "extraction":
        return False
    for change in changes[1:]:
        desc = change.find(f"{{{TEI_NS}}}desc")
        text = " ".join("".join(str(t) for t in desc.itertext()).split()) if desc is not None else ""
        if not text.startswith(METADATA_CHANGE_PREFIXES):
            return False
    return True
