"""
Hard checks a proposed unit must pass before it counts as a model's proposal.

Pure module. Schema validation is not done here: it needs the variant's RelaxNG schema and is
added by the server-side pipeline.
"""

from __future__ import annotations

from enum import StrEnum

from .units import local_name, parse_unit, text_key


class ProposalStatus(StrEnum):
    """Outcome of checking one unit of a model's answer."""

    OK = "ok"  # a valid proposal (which may equal the input)
    UNCHANGED = "unchanged"  # not returned in "changed" mode, so the model keeps the input
    MISSING = "missing"  # not returned in "all" mode
    MALFORMED = "malformed"  # not well-formed XML
    WRONG_ROOT = "wrong_root"  # not exactly one element, or a different outermost element
    TEXT_CHANGED = "text_changed"  # the text (ignoring whitespace) differs from the input
    ERROR = "error"  # the request failed (provider error, also after retries): no answer to judge

    @property
    def is_failure(self) -> bool:
        """Structural failure: the model gave no usable proposal for the unit (not a failed request)."""
        return self not in (ProposalStatus.OK, ProposalStatus.UNCHANGED, ProposalStatus.ERROR)


def check_proposal(original_xml: str, proposal_xml: str) -> ProposalStatus:
    """Check one proposed unit against the original unit."""
    try:
        content = parse_unit(proposal_xml)
    except ValueError:
        return ProposalStatus.MALFORMED
    original = parse_unit(original_xml)[0]
    if len(content) != 1 or isinstance(content[0], str) or isinstance(original, str):
        return ProposalStatus.WRONG_ROOT
    proposal = content[0]
    if local_name(proposal.tag) != local_name(original.tag):
        return ProposalStatus.WRONG_ROOT
    if text_key(proposal) != text_key(original):
        return ProposalStatus.TEXT_CHANGED
    return ProposalStatus.OK
