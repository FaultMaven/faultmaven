"""Extract a v4 runbook's ``## Causes`` section into per-Cause graph records.

This is the **produce-side** counterpart of the KB pack builder's
``_extract_causes`` (``kb_toolkit/core/pack_builder.py``): it turns the markdown
an author/LLM wrote into the exact ``metadata["causes"]`` shape the Phase-4
investigation seeder (``core.investigation.kb_cause_seeder``) consumes. Built-in
runbooks get this record at build time (from the pack); a case→runbook
conversion gets it here, on human verification (``ConversionService.verify_draft``),
so a resolved case re-enters diagnosis as structured candidates — closing the
knowledge flywheel.

It is anchored on the shared parse grammar (:mod:`runbook_grammar`, a verbatim
mirror of the upstream producer grammar) so the two sides can never diverge on
what a well-formed Cause is; a golden cross-check test pins the output to the
records the kb-toolkit builder computes for the same markdown.

Best-effort by contract: malformed pieces are skipped, not raised — the
``RunbookValidator`` is the gate that runs first (on verify, the draft is already
validated); this parser is forward-investment for the engine.
"""

from typing import Any

from faultmaven.modules.knowledge.domain.services.cause_grammar import (
    FALLBACK_INDICATOR_TOKEN,
    OPTIONAL_CAUSE_SUBFIELDS,
    REQUIRED_CAUSE_SUBFIELDS,
)
from faultmaven.modules.knowledge.domain.services.runbook_grammar import (
    CHAIN_RUNG_RE,
    CONVERGES_REF,
    INTERVENTION_RE,
    iter_cause_blocks,
    parse_cause_subfields,
)

# The sub-field boundary set the parser splits on — the schema's required +
# optional labels, so it stays in lockstep with the validator's contract.
_CAUSE_SUBFIELDS: list[str] = list(REQUIRED_CAUSE_SUBFIELDS) + list(
    OPTIONAL_CAUSE_SUBFIELDS
)


def extract_causes(content: str) -> list[dict[str, Any]]:
    """Parse ``## Causes`` into per-Cause graph records for the engine seeder.

    Each record carries the structured data the seeder instantiates into a
    case's causal graph (chain nodes/edges, per-rung indicators, quadrant-tagged
    interventions). Mirrors ``kb_toolkit.core.pack_builder._extract_causes``.
    """
    # The walk is ``runbook_grammar.iter_cause_blocks`` — the SAME one the gate
    # uses, so the gate cannot pass a Cause shape this drops (#1241). It hands
    # back a comment-masked body, which is why nothing here re-strips comments.
    causes: list[dict[str, Any]] = []
    for cause in iter_cause_blocks(content):
        h_letter, h_name = cause.letter, cause.name
        body = cause.body.strip()
        fields = parse_cause_subfields(body, _CAUSE_SUBFIELDS)

        # Chain: linear cause->effect rungs. A `converges:` directive is NOT a
        # node (the validator skips it too) — it records that the prior rung maps
        # onto another Cause's node, captured as a convergence edge.
        chain_text = fields.get("Chain", "")
        chain_nodes: list[dict[str, Any]] = []
        refs: list[str] = []
        converge_edges: list[dict[str, Any]] = []
        for rung in CHAIN_RUNG_RE.finditer(chain_text):
            ref, stmt = rung.group(1), rung.group(2).strip()
            if ref == CONVERGES_REF:
                if refs:
                    converge_edges.append(
                        {"cause_ref": refs[-1], "effect_ref": stmt, "converges": True}
                    )
                continue
            node_type = (
                "problem"
                if ref == "D"
                else ("root" if ref == "root" else "intermediate")
            )
            chain_nodes.append({"ref": ref, "statement": stmt, "node_type": node_type})
            refs.append(ref)
        chain_edges = [
            {"cause_ref": refs[j], "effect_ref": refs[j + 1]}
            for j in range(len(refs) - 1)
        ]
        chain_edges.extend(converge_edges)

        # Per-rung indicators: ref -> list (a rung may carry more than one
        # observable; keep them all rather than overwrite).
        ind_text = fields.get("Indicators", "")
        rung_indicators: dict[str, list[str]] = {}
        for m in CHAIN_RUNG_RE.finditer(ind_text):
            ref = m.group(1)
            if ref == CONVERGES_REF:
                continue
            rung_indicators.setdefault(ref, []).append(m.group(2).strip())

        iv_text = fields.get("Interventions", "")
        iv_marks = list(INTERVENTION_RE.finditer(iv_text))
        interventions = []
        for k, ivm in enumerate(iv_marks):
            iv_end = iv_marks[k + 1].start() if k + 1 < len(iv_marks) else len(iv_text)
            interventions.append(
                {
                    "quadrant": ivm.group(1),
                    "ref": ivm.group(2).strip(),
                    "text": iv_text[ivm.start() : iv_end].strip(),
                }
            )

        causes.append(
            {
                "cause_letter": h_letter,
                "cause_name": h_name,
                "is_fallback_cause": FALLBACK_INDICATOR_TOKEN
                in fields.get("Indicators", ""),
                "cause_statement": fields.get("Statement", ""),
                "chain_nodes": chain_nodes,
                "chain_edges": chain_edges,
                "rung_indicators": rung_indicators,
                "interventions": interventions,
            }
        )
    return causes
