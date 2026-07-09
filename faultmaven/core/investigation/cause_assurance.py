"""Assurance grading for an identified cause (pure, contracts-only).

Lives apart from ``causal_graph`` deliberately: consumers include the terminal
runbook-harvest gate (``terminal_transitions``), and ``causal_graph`` already
pulls in ``hypothesis_manager`` which pulls in ``terminal_transitions`` — so
putting this in ``causal_graph`` and importing it back would close an import
cycle. Keeping it here (it needs nothing from ``causal_graph``, only the case
contracts) breaks that back-edge.

``grade_cause_assurance`` is the single source of truth: it classifies a case
into one of three mutually-exclusive assurance grades — the M2 confirmation
ladder — in one pass. The §7 harvest bar is ``CONFIRMED`` (counterfactual
confirmation, gone⇒gone). Validation method (empirical vs deductive) does NOT
raise the grade: both are mechanistic per M2/§7.1.1 — a deductive derivation
is itself assembled from LLM-mediated refutations plus an asserted-exhaustive
differential, so only the counterfactual outcome of actually removing the
cause clears the top bar.

The ``CauseAssuranceGrade`` enum lives in the domain layer
(``modules.case.contracts``) so it can be a persisted field on
``InvestigationProgress`` (the ``VerificationStatus`` precedent); this module
imports and re-exports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from faultmaven.modules.case.contracts import (
    CauseAssuranceGrade,
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceStance,
    HypothesisState,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    RootCauseConclusion,
)

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, CausalNode

__all__ = [
    "CONFIRMED_RCC_LIKELIHOOD_FLOOR",
    "ENGINE_RCC_AUTHOR",
    "MECHANISTIC_RCC_LIKELIHOOD",
    "CauseAssuranceGrade",
    "conclusion_overclaims",
    "confirm_root_from_resolution_absence",
    "evidence_category_map",
    "grade_cause_assurance",
    "root_counterfactually_confirmed",
]

# The marker on an engine-synthesized RootCauseConclusion (§9.3) — distinguishes
# the engine's faithful mirror (which may be refreshed/retired) from the LLM's
# own authored conclusion (which always wins and is never overwritten). Defined
# here (grade semantics, contracts-only) so both ``causal_graph`` (the mint) and
# the terminal confirm-stamp below can share it without an import cycle.
ENGINE_RCC_AUTHOR = "engine:chain_validation"

# M2 confidence vocabulary (§0 M2, §7.2): a validated-but-unconfirmed root is
# MECHANISTIC grade — the engine-synthesized conclusion reads CONFIDENT at a
# FIXED likelihood (a cap, not a floor: the LLM's own higher
# root_cause_likelihood must not leak a mechanistic cause into "verified").
# Only a counterfactually CONFIRMED root (causal_absence SUPPORTS on the root,
# gone⇒gone) reads VERIFIED, floored at 0.9.
MECHANISTIC_RCC_LIKELIHOOD = 0.8
CONFIRMED_RCC_LIKELIHOOD_FLOOR = 0.9

# §7.1 (INV-29): a stance the LLM itself declares at confidence below this bar
# is a hedge, not grounding. Read by BOTH belief axes so they cannot drift: the
# chain tally (``causal_graph._node_evidence_tally`` — a hedged SUPPORTS link
# is not CAUSAL grounding) and the flat prior cap
# (``hypothesis_manager.update_hypothesis_likelihood`` — a hedged link does not
# lift the evidence-free cap). Lives here (the shared leaf) because
# causal_graph imports hypothesis_manager: neither could import it from the
# other. Absent/None reads as unset -> full confidence; an EXPLICIT 0.0 stays
# filtered.
CAUSAL_STANCE_CONFIDENCE_MIN = 0.6

# Hypothesis states that keep a chain's root a STANDING cause (mirrors
# ``causal_graph._STANDING_HYP_STATES`` — kept literal here to avoid the
# import cycle; the two must not drift).
_STANDING_HYP_STATES = (HypothesisState.ACTIVE, HypothesisState.VALIDATED)


def evidence_category_map(case: "Case") -> dict:
    """The one ``evidence_id → category`` map every assurance/tally reader
    shares (this module and ``causal_graph`` import it from here), so the
    "dangling evidence_id is ignored, never assumed" discipline has a single
    owner — parallel hand-written comprehensions would let a future filter or
    key change reach some readers and not others, silently splitting the M2
    grade from node-state derivation."""
    return {e.evidence_id: getattr(e, "category", None) for e in case.evidence}


def _validated_roots(case: "Case") -> list["CausalNode"]:
    """The case's VALIDATED root nodes — the only harvest-relevant unit (§7 never
    harvests an intermediate rung or a candidate)."""
    return [
        n
        for n in case.causal_nodes.values()
        if n.node_type == NodeType.ROOT and n.node_state == NodeState.VALIDATED
    ]


def root_counterfactually_confirmed(
    node: "CausalNode", evidence_category_by_id: dict
) -> bool:
    """M2 counterfactual confirmation, per node: a SUPPORTS evidence link backed
    by a ``causal_absence_evidence`` row — the cause was removed and the problem
    went with it (gone⇒gone). The confirmation must be LINKED to this node: a
    case-level absence row with no bearing on the root does not confirm it (the
    same bearing discipline as ``_node_evidence_tally``'s counterfactual-refute
    arm). A dangling ``evidence_id`` is ignored, never assumed. The only live
    producer of such a link is the resolution confirm-stamp below — the LLM
    chain-emission ingest strips SUPPORTS-on-absence links (see
    ``causal_graph.ingest_emitted_chain``), so this predicate cannot be
    satisfied by an LLM self-claim."""
    return any(
        link.stance == EvidenceStance.SUPPORTS
        and evidence_category_by_id.get(link.evidence_id)
        == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        for link in node.evidence_links
    )


_CONFIRMATION_REASON = (
    "engine: user-confirmed resolution — the recorded causal-absence outcome "
    "bears on the sole standing validated root (M2 gone⇒gone)"
)

# Graph hooks — inversion seam. ``causal_graph`` (the higher layer: it imports
# this module) REGISTERS its primitives at import time so the confirm-stamp can
# consult the §7.1 count-held set and re-derive node states without an import
# back-edge (the architecture contract forbids the cycle, deferred or not).
_GRAPH_HOOKS: dict = {}


def register_graph_hooks(*, support_count_held_root_ids, derive_node_states) -> None:
    """Called once from ``causal_graph`` at module import."""
    _GRAPH_HOOKS["count_held"] = support_count_held_root_ids
    _GRAPH_HOOKS["derive"] = derive_node_states


def _graph_hooks() -> dict:
    """The registered hooks; cold-start fallback loads ``causal_graph`` by
    name (call-time, both modules long initialized — no init-order hazard)
    for any entry point that reaches the stamp without the engine stack."""
    if not _GRAPH_HOOKS:
        import importlib

        importlib.import_module("faultmaven.core.investigation.causal_graph")
    return _GRAPH_HOOKS


def confirm_root_from_resolution_absence(case: "Case") -> bool:
    """M2 confirm-side twin of the failed-fix refute stamp: make the
    ``CONFIRMED`` grade reachable from the live flow — called at RESOLVED
    **transition execution** (after the user's explicit confirmation), never
    on the mere appearance of an absence row.

    The prompt's verify-turn contract records the resolution-confirming
    ``causal_absence_evidence`` row as a STAND-ALONE audit row ("do NOT link
    it"), and the chain-emission ingest strips any LLM attempt to SUPPORTS-link
    absence evidence — so this stamp is the only producer of the counterfactual
    confirmation the grade requires. The row alone is an LLM self-claim: a
    premature "pods are stable" absence row emitted mid-rollout (observed live
    in the gate sims) must not confirm anything. The trigger is therefore the
    RESOLVED handshake — the user's explicit consent — which is strictly
    stronger evidence than the row's existence.

    Target root — exactly ONE, chosen conservatively (NO INCORRECT CONCLUSION):

    - Validated roots of STANDING hypotheses (ACTIVE/VALIDATED) when any exist:
      an orphan validated node whose hypothesis decayed to RETIRED without
      disproof must not veto the user's confirmation of the standing cause.
    - Otherwise all validated roots (the weak-model chain-without-hypothesis
      shape). With several candidates either way (an unarbitrated MECE
      violation) the engine never guesses which cause the fix removed — the
      case stays MECHANISTIC pending arbitration.
    - With NO validated root at all: the COUNT-HELD roots (§7.1/INV-29 —
      really causally supported and blocked only by the independent-support
      bar, ``causal_graph.support_count_held_root_ids``). The user's gone⇒gone
      handshake is the decisive second observation, so the count bar must not
      veto it; after linking, a re-derive validates the root via the
      confirmed-root bypass. The same sole-candidate discipline applies, and a
      root held for any OTHER reason (restating, net-refuted, AND-gate, no
      qualifying causal support) never qualifies.

    Row selection — order-independent and windowed:

    - Repositories load ``case.evidence`` newest-first, in-memory construction
      appends oldest-first — so selection keys on ``collected_at_turn`` (the
      NEWEST qualifying row), never on list position.
    - Engine-authored rows never qualify: the engine only mints absence rows
      as failed-fix disconfirmations (M6), and node pruning can orphan their
      REFUTES link — a laundered disconfirmation must not become confirmation.
    - Rows at or before the latest failed-fix disconfirmation never qualify: a
      premature "it's stable" row from a fix window that later FAILED must not
      confirm a different, later fix.
    - Only rows with NO existing node link anywhere qualify (an already-linked
      row's bearing is decided). Content-level bearing of the row on THIS root
      is the separate causal-absence bearing check tracked on #656.
    - Idempotent: a root already counterfactually confirmed is left alone.

    On success, an ENGINE-authored conclusion mirror naming this root is
    upgraded in place (VERIFIED + the confirming row cited) — terminal cases
    never recompute, so the mirror would otherwise stay frozen at the
    mechanistic grade beside a CONFIRMED case. Returns True if it attached a
    link; the caller re-persists grade, over-claim flag, and verification
    status so the terminal blob reflects the confirmation.
    """
    candidate_roots = _validated_roots(case)
    count_held_ids: set = set()
    if not candidate_roots:
        # §7.1 independent-support bar (INV-29): a sole root held from
        # VALIDATED ONLY by the count bar — really causally supported,
        # net-supporting, AND-gate satisfied, not restating — may still be
        # confirmed here: the user's explicit gone⇒gone handshake IS the
        # decisive second observation (strictly stronger than any empirical
        # count; M2). Without this, the count bar would VETO the confirmation
        # — the strongest evidence class yielding the weakest grade — and the
        # case would terminate NO_ROOT with harvest permanently blocked.
        count_held_fn = _graph_hooks().get("count_held")
        if count_held_fn is not None:
            count_held_ids = count_held_fn(case)
        candidate_roots = [
            case.causal_nodes[nid] for nid in count_held_ids if nid in case.causal_nodes
        ]
    if not candidate_roots:
        return False
    standing_root_ids = {
        h.root_node_id
        for h in case.hypotheses.values()
        if h is not None and h.state in _STANDING_HYP_STATES and h.root_node_id
    }
    targets = [n for n in candidate_roots if n.node_id in standing_root_ids]
    if not targets:
        targets = candidate_roots
    if len(targets) != 1:
        return False
    root = targets[0]
    cat_by_id = evidence_category_map(case)
    if root_counterfactually_confirmed(root, cat_by_id):
        return False

    linked_ids: set = set()
    refutes_linked: set = set()
    for node in case.causal_nodes.values():
        for link in node.evidence_links:
            linked_ids.add(link.evidence_id)
            if link.stance == EvidenceStance.REFUTES:
                refutes_linked.add(link.evidence_id)
    last_disconfirm_turn = max(
        (
            e.collected_at_turn or 0
            for e in case.evidence
            if e.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
            and e.evidence_id in refutes_linked
        ),
        default=-1,
    )
    candidates = [
        e
        for e in case.evidence
        if e.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        and e.evidence_id not in linked_ids
        and getattr(e, "collected_by", None) != "engine"
        and (e.collected_at_turn or 0) > last_disconfirm_turn
    ]
    if not candidates:
        return False
    absence_row = max(candidates, key=lambda e: e.collected_at_turn or 0)

    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=absence_row.evidence_id,
            stance=EvidenceStance.SUPPORTS,
            reasoning=_CONFIRMATION_REASON,
            linked_at_turn=case.current_turn,
        )
    )
    if root.node_id in count_held_ids:
        # The confirmation completes the empirical bar for a count-held root:
        # re-derive so the confirmed-root bypass VALIDATES it before the
        # caller re-reads the grade (grade_cause_assurance requires a
        # VALIDATED root) and before the mirror upgrade names it.
        derive_fn = _graph_hooks().get("derive")
        if derive_fn is not None:
            derive_fn(case)
    if case.root_cause_conclusion is None:
        # A count-held root was INCONCLUSIVE until this stamp, so the per-turn
        # mirror synthesis never minted a conclusion for it — and terminal
        # cases never recompute. Without a mint HERE, the case would freeze
        # as CONFIRMED (the sole harvest authority) with NO cause text for
        # the report/harvest layers to read. Mint the minimal faithful
        # mirror for the root the user just confirmed; an LLM-authored
        # conclusion, had one existed, is never touched.
        _mint_confirmed_mirror(case, root)
    else:
        _upgrade_engine_mirror(case, root, absence_row.evidence_id)
    return True


def _mint_confirmed_mirror(case: "Case", root: "CausalNode") -> None:
    """Mint the engine conclusion mirror for a just-confirmed root when NO
    conclusion exists (the count-held-at-RESOLVED shape). Mechanism text
    follows the standing hypothesis's chain when one names this root (same
    shape as the per-turn synthesis); the hypothesis-less fallback states the
    degenerate mechanism. Confidence is grade-derived: the root is
    counterfactually CONFIRMED, so VERIFIED at the floor."""
    hyp = next(
        (
            h
            for h in case.hypotheses.values()
            if h is not None
            and h.state in _STANDING_HYP_STATES
            and h.root_node_id == root.node_id
        ),
        None,
    )
    inter: list[str] = []
    if hyp is not None:
        inter = [
            case.causal_nodes[nid].statement
            for nid in (hyp.path or [])[1:-1]
            if nid in case.causal_nodes
        ]
    mechanism = (
        " → ".join(inter + ["the problem"])
        if inter
        else "Directly produces the observed problem."
    )[:2000]
    likelihood = CONFIRMED_RCC_LIKELIHOOD_FLOOR
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=root.statement[:1000],
        mechanism=mechanism,
        confidence_level=ConfidenceLevel.from_score(likelihood),
        likelihood=likelihood,
        validated_hypothesis_id=hyp.hypothesis_id if hyp is not None else None,
        evidence_basis=[
            link.evidence_id
            for link in root.evidence_links
            if link.stance == EvidenceStance.SUPPORTS
        ],
        determined_by=ENGINE_RCC_AUTHOR,
    )


def _upgrade_engine_mirror(
    case: "Case", root: "CausalNode", absence_evidence_id: str
) -> None:
    """Scoped re-mint at the stamp site: terminal cases never run the per-turn
    recompute (and ``terminal_transitions`` cannot import ``causal_graph``'s
    full mirror synthesis), so without this an ENGINE-authored conclusion would
    stay frozen at CONFIDENT/0.8 — and its ``evidence_basis``, minted pre-stamp,
    would never cite the gone⇒gone row — beside a CONFIRMED terminal grade. The
    upgrade keeps the mirror's text/root and raises only the grade-derived
    fields. LLM-authored conclusions are never touched (their retraction and
    refresh lifecycle is a separate correction tracked on #656)."""
    rcc = case.root_cause_conclusion
    if rcc is None or getattr(rcc, "determined_by", None) != ENGINE_RCC_AUTHOR:
        return
    hyp = case.hypotheses.get(getattr(rcc, "validated_hypothesis_id", None) or "")
    if hyp is None or hyp.root_node_id != root.node_id:
        return
    likelihood = max(rcc.likelihood or 0.0, CONFIRMED_RCC_LIKELIHOOD_FLOOR)
    evidence_basis = list(rcc.evidence_basis or [])
    if absence_evidence_id not in evidence_basis:
        evidence_basis.append(absence_evidence_id)
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=rcc.root_cause,
        mechanism=rcc.mechanism,
        confidence_level=ConfidenceLevel.from_score(likelihood),
        likelihood=likelihood,
        validated_hypothesis_id=rcc.validated_hypothesis_id,
        evidence_basis=evidence_basis,
        contributing_factors=list(rcc.contributing_factors or []),
        determined_by=ENGINE_RCC_AUTHOR,
    )


def conclusion_overclaims(rcc, grade: CauseAssuranceGrade) -> bool:
    """The M2 over-claim seam predicate — ONE definition shared by the WARNING
    in the per-turn recompute, the ``seam_overclaim`` flag in the DEBUG
    grounding trace, and the terminal re-grade, so the prod signal and the
    greppable trace can never disagree about the same turn."""
    return (
        rcc is not None
        and rcc.confidence_level == ConfidenceLevel.VERIFIED
        and grade != CauseAssuranceGrade.CONFIRMED
    )


def grade_cause_assurance(case: "Case") -> CauseAssuranceGrade:
    """Classify the case's identified cause into a single assurance grade, in one
    pass over its validated roots. The single source of truth for §7 gating.

    A confidently-wrong LLM must not turn an unverified cause into reusable
    knowledge, so only ``CONFIRMED`` — a validated root whose removal was
    observed to remove the problem — clears the bar.
    """
    validated_roots = _validated_roots(case)
    if not validated_roots:
        return CauseAssuranceGrade.NO_ROOT
    evidence_category_by_id = evidence_category_map(case)
    if any(
        root_counterfactually_confirmed(r, evidence_category_by_id)
        for r in validated_roots
    ):
        return CauseAssuranceGrade.CONFIRMED
    return CauseAssuranceGrade.MECHANISTIC
