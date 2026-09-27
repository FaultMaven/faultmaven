from faultmaven.core.investigation.causal_graph import (
    BLOCK_REASON_COUNT,
    BLOCK_REASON_HEDGED,
    BLOCK_REASON_MIRROR,
    BLOCK_REASON_RESTATEMENT,
    mece_contested_root_ids,
    restatement_held_root_ids,
    root_support_block_reasons,
)
from faultmaven.modules.case.contracts import Case


def _build_causal_graph_block(case: Case) -> str:
    """Render the causal graph (hypotheses ARE chains, methodology M3).

    Render the chain STRUCTURE with node ids — not flat statements — so the LLM
    EXTENDS the existing graph: it references an existing node's ``cn_...`` id
    (in produces / root_node_ref / node_evidence_links) instead of re-stating a
    cause as a fresh duplicate node. That cross-turn re-emission is what
    fragments grounding across duplicate roots and stalls cause_state at UNKNOWN
    (the node-identity loop was previously open: the engine assigned ids but
    never rendered them back, so the LLM could not reference them). REFUTED
    hypotheses keep their refutation_reason inline (anti-amnesia, Rule 8:
    prevents re-proposing a rejected theory); the pair-integrity invariant
    guarantees it is non-empty when state=REFUTED.

    Returns an empty string when the case has no active hypotheses and no
    causal nodes (nothing to render yet).
    """

    def _stmt(s: str) -> str:
        s = " ".join((s or "").split())
        return s if len(s) <= 140 else s[:137] + "..."

    nodes = case.causal_nodes or {}
    active_h = [h for h in case.hypotheses.values() if h.state.value != "retired"]
    if not (active_h or nodes):
        return ""

    # §7.1/INV-29 elicitation: a ROOT held from VALIDATED only by the
    # causal-grounding bar gets its REASON-SPECIFIC recovery action rendered
    # inline — without it the model sees a bare [root/inconclusive],
    # re-records the same datum (which the independence mirror collapses) or
    # re-hedges the same link, and stalls.
    block_reasons = root_support_block_reasons(case)
    recovery_notes = {
        BLOCK_REASON_COUNT: (
            " — needs a SECOND INDEPENDENT causal observation to validate "
            "(re-recording the same datum does not count)"
        ),
        BLOCK_REASON_MIRROR: (
            " — needs a SECOND INDEPENDENT causal observation to validate "
            "(re-recording the same datum does not count)"
        ),
        BLOCK_REASON_HEDGED: (
            " — its causal support is self-hedged (stance_confidence below "
            "0.6); record a CONFIDENT causal observation to ground it"
        ),
        # §7.1 restatement guard. Its own arm because its recovery is the
        # OPPOSITE of the grounding arms': this root's evidence bar is already
        # met, so more SUPPORTING observations move nothing. Without the note
        # the model sees a bare [root/inconclusive] beside confident causal
        # supports and keeps collecting (fm#1137: nine turns of it).
        #
        # BOTH recoveries are named because the held population is not one
        # shape. A root held by the problem ANCHORS alone is the symptom
        # restated and needs a new MECHANISM statement; a root held by frame
        # DILUTION — several different causes' hypotheses that between them
        # cover this one, the documented <=2% FP class — validates the moment
        # one of those alternatives is refuted or retired. (The third shape, a
        # standing DUPLICATE of the root's own hypothesis, was the fm#1137
        # known limit and is no longer held at all: fm#1122's §7.1 attribution
        # test releases a root whose whole overlap ONE standing explanation
        # accounts for.) The engine cannot tell the remaining two apart (that is
        # the fm#1137 known limit), so telling the model only the surgery half
        # would be categorically false for the dilution slice and would repeat,
        # in a new place, the wrong-recovery-advice failure this note exists to
        # fix.
        #
        # Spelled as ADD-and-relink, not "restate this node", because
        # state_updates carries no node-update field — causal_nodes_to_add /
        # causal_edges_to_add / node_evidence_links only, and
        # ingest_emitted_chain reuses a node solely on an exact normalised
        # statement match. A model told to "restate" either no-ops (the hold
        # persists) or mints a root carrying none of the held node's evidence
        # links, landing at CANDIDATE with zero supports. The produces edge is
        # named explicitly because ``_attach`` DECLINES a re-root whose new
        # chain does not reach D — omit it and the re-root is silently refused,
        # the hold persists, and the relinked evidence strands on a validated,
        # hypothesis-less orphan root.
        BLOCK_REASON_RESTATEMENT: (
            " — fully supported, but its statement adds no content beyond what "
            "the problem and the other hypotheses already say, so MORE "
            "SUPPORTING EVIDENCE WILL NOT VALIDATE IT. Two recoveries: if an "
            "overlapping ALTERNATIVE hypothesis is what this restates, REFUTE "
            "or RETIRE that alternative; if this node is the symptom restated, "
            "add a NEW root naming the specific MECHANISM (what is "
            "misconfigured/exhausted/failing), give it a produces edge so its "
            "chain reaches D, point this hypothesis's root_node_ref at it, and "
            "re-link this node's ev_ ids to it via node_evidence_links"
        ),
    }

    # §7.1.2 MECE arbitration: contested roots (several simultaneously-
    # validated, mutually-exclusive causes) get the discrimination ask
    # rendered inline — without it the model sees several [root/validated]
    # lines, reads the cause as settled, and never runs the test that
    # separates them. Rendered through the SAME per-node reason → note maps
    # as the §7.1 recovery notes; the overlay order makes the precedence
    # explicit (a contested VALIDATED root shows the discrimination ask even
    # if a future block reason ever annotates validated roots too).
    _MECE_CONTESTED = "mece_contested"
    recovery_notes[_MECE_CONTESTED] = (
        " — one of several simultaneously-validated MUTUALLY-EXCLUSIVE roots: "
        "cause identification is HELD until discriminating evidence refutes "
        "the alternatives (at most one can be the real cause)"
    )
    # The two hold populations are disjoint by construction —
    # root_support_block_reasons excludes restating roots, and
    # restatement_held_root_ids requires the grounding bar already MET — so
    # the merge order below cannot silently reclassify a root.
    node_reasons = {
        **block_reasons,
        **dict.fromkeys(restatement_held_root_ids(case), BLOCK_REASON_RESTATEMENT),
        **dict.fromkeys(mece_contested_root_ids(case), _MECE_CONTESTED),
    }

    def _node_line(indent: str, n) -> str:
        note = recovery_notes.get(node_reasons.get(n.node_id), "")
        return (
            f"{indent}{n.node_id} [{n.node_type.value}/{n.node_state.value}] "
            f"{_stmt(n.statement)}{note}"
        )

    on_path: set[str] = set()
    lines = [
        "<causal_graph>",
        "Chains built so far (D = the problem). REFERENCE these cn_... ids when "
        "extending — attach evidence or new rungs to an existing node rather "
        "than re-stating a cause already present as a new node. Reference a "
        "hypothesis by its [hyp_...] id in hypothesis_evidence_links and "
        "hypotheses_to_update.",
    ]
    for h in active_h:
        lines.append(
            f"- [{h.hypothesis_id}] {_stmt(h.statement)} "
            f"(Confidence: {h.likelihood * 100:.0f}%, State: {h.state.value})"
        )
        chain_ids = h.path or ([h.root_node_id] if h.root_node_id else [])
        if not chain_ids and h.state.value not in ("refuted", "retired"):
            # An unanchored hypothesis renders as a bare statement with no rungs
            # under it — legible only if you notice an absence. Say it, the same
            # way the block names every other recovery action inline. This is the
            # recovery path after the engine REFUSES a root_node_ref that named a
            # root another hypothesis already owns (fm#1091): without the ask
            # restated here, the model has no reason to revisit an anchoring it
            # believes it already made.
            lines.append(
                "    (no chain yet — emit this hypothesis's OWN root cause node "
                "and point root_node_ref at it; a root another hypothesis "
                "already owns cannot be reused)"
            )
        for nid in chain_ids:
            n = nodes.get(nid)
            if n is None or n.node_type.value == "problem":
                continue
            on_path.add(nid)
            lines.append(_node_line("    ", n))
        if h.state.value == "refuted" and h.refutation_reason:
            lines.append(f"    Refuted because: {_stmt(h.refutation_reason)}")
    # Standalone nodes on no hypothesis path — surface their ids so the LLM
    # attaches/extends them instead of re-emitting the same cause.
    orphans = [
        n
        for nid, n in nodes.items()
        if nid not in on_path and n.node_type.value != "problem"
    ]
    if orphans:
        lines.append(
            "  Unattached causes already in the graph — reference these ids, "
            "do not re-emit:"
        )
        for n in orphans:
            lines.append(_node_line("    ", n))
    lines.append("</causal_graph>")
    return "\n".join(lines)
