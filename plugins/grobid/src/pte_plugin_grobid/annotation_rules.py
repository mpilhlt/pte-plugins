"""
Resolves this plugin's annotation-guide config into editorialDecl-ready
entries, shared by extraction-time generation (extractor.py) and the core
"Refresh document rules" reviewer action (see
fastapi_app/lib/doc_rules/rules_refresh.py, dispatched to this plugin via
GrobidRulesProvider below) so both regenerate editorialDecl the same way.
See docs/superpowers/specs/2026-09-22-editorial-decl-annotation-rules-design.md
(Parts B and G) and
docs/superpowers/specs/2026-09-22-annotation-guide-dual-target-refs-design.md
(the variant_ids/"primary" schema and dual human/machine refs).
"""

from typing import Optional

from fastapi_app.lib.core.url_cache import UrlCache
from fastapi_app.lib.doc_rules.resource_key import normalize_resource_key
from fastapi_app.lib.utils.annotation_rules_utils import (
    AnnotationRuleRef,
    AnnotationRuleRefTarget,
    is_line_range_fragment,
    resolve_forge_permalink,
    translate_anchor_to_line_range,
)
from pte_plugin_grobid.config import (
    AnnotationGuide,
    SCHEMA_FRAGMENT_LABELS,
    get_annotation_guides,
    get_schema_fragment_url,
    get_schema_url,
)


def build_editorial_decl_entries(variant_id: str, cache: UrlCache) -> list[AnnotationRuleRef]:
    """
    Resolve this variant's configured annotation guides, plus its upstream
    schema-fragment source (if any), into editorialDecl entries.

    A guide matches `variant_id` if its `variant_ids` list contains that id
    or the wildcard "*" (applies to every variant). Each matching guide
    produces one or two refs depending on its configured url/type - see
    _build_refs_for_guide(). "primary" is the category sentinel the
    frontend drawer looks up by default. A variant with a dedicated
    fossil/schema/*.rng source file (see get_schema_fragment_url()) also
    gets one "schema-fragment" entry, appended last, pointing at that
    upstream, hand-editable source - distinct from get_schema_url()'s
    generated/validated-against schema. Returns [] if neither applies.
    """
    entries: list[AnnotationRuleRef] = []
    for guide in get_annotation_guides():
        if variant_id not in guide["variant_ids"] and "*" not in guide["variant_ids"]:
            continue
        refs = _build_refs_for_guide(guide, cache)
        entries.append({"category": guide["category"], "refs": refs, "n": guide["label"]})

    schema_fragment_url = get_schema_fragment_url(variant_id)
    if schema_fragment_url is not None:
        target = resolve_forge_permalink(schema_fragment_url, cache)
        entries.append({
            "category": "schema-fragment",
            "n": SCHEMA_FRAGMENT_LABELS[variant_id],
            "refs": [{"target": target, "content_type": "xml", "subtype": "human"}],
        })

    return entries


def _build_refs_for_guide(guide: AnnotationGuide, cache: UrlCache) -> list[AnnotationRuleRefTarget]:
    """
    Build the one or two refs a single configured guide produces, permalink-
    pinning every target. Which refs are generated depends on the
    configured url's fragment shape and content type:

    - No fragment (whole document): a single "human" ref, unchanged.
    - An already-machine-style line-range fragment (#L10-L50): a single
      "machine" ref, unchanged - this is how a purely machine-oriented
      topic (e.g. "footnote-annotation") stays configurable without ever
      needing a human-facing anchor.
    - A heading-anchor fragment with type "html": a single "human" ref -
      there's no heading/line-range concept for HTML.
    - A heading-anchor fragment with type "markdown": both a "human" ref
      (as configured) and an auto-derived "machine" ref (a line range
      covering that heading's section, computed by
      annotation_rules_utils.translate_anchor_to_line_range). If the anchor
      can't be found in the fetched document, only the "human" ref is
      returned - this never fails extraction/refresh, matching this
      feature's established error-handling philosophy.

    If the guide has a `machine_url` (a condensed, LLM-only version of the
    section), the "machine" ref is derived from it instead - a line-range
    fragment as is, a markdown heading anchor translated to a line range -
    and the ref derived from `url` is only the fallback when that fails.
    """
    _, _, fragment = guide["url"].partition("#")
    content_type = guide["type"]

    if not fragment:
        human_target = resolve_forge_permalink(guide["url"], cache)
        return [{"target": human_target, "content_type": content_type, "subtype": "human"}]

    if is_line_range_fragment(fragment):
        machine_target = resolve_forge_permalink(guide["url"], cache)
        return [{"target": machine_target, "content_type": content_type, "subtype": "machine"}]

    human_target = resolve_forge_permalink(guide["url"], cache)
    refs: list[AnnotationRuleRefTarget] = [
        {"target": human_target, "content_type": content_type, "subtype": "human"},
    ]
    if content_type != "markdown":
        return refs
    machine_target: Optional[str] = None
    if "machine_url" in guide:
        machine_target = _machine_target(resolve_forge_permalink(guide["machine_url"], cache), cache)
    if machine_target is None:
        machine_target = _machine_target(human_target, cache)
    if machine_target is not None:
        refs.append({"target": machine_target, "content_type": content_type, "subtype": "machine"})
    return refs


def _machine_target(pinned_url: str, cache: UrlCache) -> Optional[str]:
    """
    Line-range URL for an already permalink-pinned markdown url: the url
    itself if its fragment is a line range, else the range its heading
    anchor covers. None if it has no fragment or the anchor can't be
    resolved.
    """
    pinned_base, _, fragment = pinned_url.partition("#")
    if not fragment:
        return None
    if is_line_range_fragment(fragment):
        return pinned_url
    line_range = translate_anchor_to_line_range(pinned_base, fragment, cache)
    if line_range is None:
        return None
    start, end = line_range
    return f"{pinned_base}#L{start}-L{end}"


class GrobidRulesProvider:
    """
    Adapts this plugin's existing annotation-guide/schema config to the
    DocumentRulesProvider protocol so "Refresh document rules" (a core
    action, see fastapi_app/lib/doc_rules/rules_refresh.py) can dispatch to
    it. Registered once, in GrobidPlugin.initialize().
    """

    def build_editorial_decl_entries(self, variant_id: str, cache: UrlCache) -> list[AnnotationRuleRef]:
        return build_editorial_decl_entries(variant_id, cache)

    def get_schema_url(self, variant_id: str) -> Optional[str]:
        return get_schema_url(variant_id)

    def get_condense_prompt(self, guide_url: str) -> Optional[str]:
        """
        The `condense_prompt` of the first guide that sets one and whose `url`
        matches guide_url. Matching uses the human `url` only (not
        `machine_url`), with the fragment kept and the git ref normalized.
        """
        key = normalize_resource_key(guide_url)
        for guide in get_annotation_guides():
            if "condense_prompt" not in guide:
                continue
            if normalize_resource_key(guide["url"]) == key:
                return guide["condense_prompt"]
        return None
