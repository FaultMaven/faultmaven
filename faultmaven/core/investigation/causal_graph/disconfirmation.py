from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from faultmaven.core.investigation.cause_assurance import (
    ENGINE_EVIDENCE_AUTHOR,
    cause_elimination_rows,
    counterfactual_link_decisive,
)
from faultmaven.core.investigation.cause_assurance import (
    ENGINE_RCC_AUTHOR as _ENGINE_RCC_AUTHOR,
)
from faultmaven.core.investigation.cause_assurance import (
    evidence_category_map as _evidence_category_map,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.lifecycle_metrics import (
    llm_rcc_retracted_disconfirmed_total,
    m6_demotion_refused_total,
)
from faultmaven.modules.case.contracts import (
    CausalNode,
    CauseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    HypothesisState,
    InvestigationActionType,
    NodeEvidenceLink,
    NodeState,
    RootCauseConclusion,
)

from .projection import _standing_hypotheses
from .queries import _state
from .rcc import _hypothesis_disconfirmed, _representative_cause_hypothesis

if TYPE_CHECKING:
    from faultmaven.modules.case.contracts import Case, Hypothesis


_DISCONFIRMATION_REASON = (
    "counterfactual disconfirmation: the cause was addressed or confirmed "
    "correct yet the problem persisted"
)


def _node_has_counterfactual_refute(
    node: CausalNode, cat_by_id: dict[str, EvidenceCategory | None]
) -> bool:
    """Does the node carry a DECISIVE REFUTES link backed by
    ``CAUSAL_ABSENCE_EVIDENCE`` — a counterfactual disconfirmation (the §7.2
    strongest grade)? Same confidence bar as the tally
    (``counterfactual_link_decisive``): a self-hedged counterfactual neither
    triggers the M6 node-side demotion here nor — via the idempotence check in
    ``_attach_engine_refutation`` — suppresses the engine from attaching its
    own decisive refutation when M6 does fire."""
    return any(
        link.stance == EvidenceStance.REFUTES
        and cat_by_id.get(link.evidence_id) == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        and counterfactual_link_decisive(link)
        for link in node.evidence_links
    )


def _disconfirmed_cause_trigger(
    case: Case, *, node_side: bool
) -> tuple[Hypothesis, str] | None:
    """Shared M6 trigger for both demote paths. Returns ``(hypothesis, reason)``
    when a grounded (``cause_state=IDENTIFIED``) case's representative cause is
    disconfirmed, else None. Disconfirmation is the hypothesis being REFUTED or
    net-refuted (``_net_refuted``); and — only when ``node_side`` (the chain-mode
    case the prompt mandates) — its ROOT node carrying a counterfactual
    (CAUSAL_ABSENCE) refutation even when the flat hypothesis was untouched.

    ``node_side`` is FALSE for the flat path so its behavior is exactly as
    before: a persisted root (reloaded regardless of the flag, e.g. after the
    flag is flipped off) with a counterfactual refute must NOT demote a healthy
    flat hypothesis — node-derived disconfirmation is a chain-mode concept.

    Returns ``(hypothesis, reason, kind)``. **``kind`` distinguishes two
    materially different claims that share this trigger (#987):**

    - ``"evidence"`` — the hypothesis is REFUTED or net-refuted by its OWN
      evidence links. This asserts nothing about any fix; it is grounded in the
      links themselves and needs no external precondition.
    - ``"counterfactual"`` — the FAILED-FIX claim: the root carries a
      counterfactual (CAUSAL_ABSENCE) refutation, i.e. "the cause was addressed
      yet the problem persisted". That claim is about events outside the graph,
      so it is the one M6 must ESTABLISH rather than infer.

    Conflating them is what made the first #987 fix over-broad: gating the whole
    trigger on fix-application evidence silently blocked ordinary evidence-based
    disconfirmation, leaving a net-refuted cause standing as IDENTIFIED with its
    conclusion intact. When BOTH hold, ``"evidence"`` wins — it is the live,
    unconditional path, and it lets the engine record what it can actually
    substantiate instead of the stronger failed-fix story."""
    p = case.progress
    if p.cause_state != CauseState.IDENTIFIED:
        return None
    hyp = _representative_cause_hypothesis(case)
    if hyp is None:
        return None
    root = case.causal_nodes.get(hyp.root_node_id) if hyp.root_node_id else None
    evidence_side = _hypothesis_disconfirmed(hyp)
    counterfactual_side = (
        node_side
        and root is not None
        and _node_has_counterfactual_refute(root, _evidence_category_map(case))
    )
    if not (evidence_side or counterfactual_side):
        return None
    return (
        hyp,
        (hyp.refutation_reason or _DISCONFIRMATION_REASON)[:200],
        "evidence" if evidence_side else "counterfactual",
    )


def any_chain_root_inconclusive(case: Case) -> bool:
    """Does some STANDING hypothesis's chain ROOT node read INCONCLUSIVE — i.e.
    a cause we have gathered bearing-but-indecisive evidence on (neither validated
    nor refuted)? Such a root is a live CANDIDATE, not nothing. Used by the
    cause_state soft floor: a once-grounded root that loses validation to an
    evidence tie (INCONCLUSIVE, NOT counterfactually REFUTED) holds the case at
    CANDIDATES rather than flapping down to UNKNOWN (finding-5 / NO-COLLAPSE). A
    truly REFUTED root is excluded here, so a real M6 disconfirmation still drops
    the case fully. (Also captures a never-yet-grounded root that has bearing-but-
    indecisive evidence — that too is a live candidate, so CANDIDATES over UNKNOWN
    is the honest read.)"""
    return any(
        h.root_node_id
        and _state(h.root_node_id, case.causal_nodes) == NodeState.INCONCLUSIVE
        for h in _standing_hypotheses(case)
    )


def _node_has_engine_counterfactual_refute(node: CausalNode, case: Case) -> bool:
    """Does the node already carry the ENGINE's own M6 refutation — a REFUTES
    link backed by an engine-authored ``CAUSAL_ABSENCE_EVIDENCE`` row?"""
    engine_absence_ids = {
        e.evidence_id
        for e in case.evidence
        if getattr(e, "category", None) == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        and getattr(e, "collected_by", None) == ENGINE_EVIDENCE_AUTHOR
    }
    return any(
        link.stance == EvidenceStance.REFUTES and link.evidence_id in engine_absence_ids
        for link in node.evidence_links
    )


def _fix_application_turn(case: Case) -> int | None:
    """The turn at which the case RECORDS that a fix was executed, or ``None``
    when no such record exists — M6's first precondition (#987).

    The authoritative record is a ``ProposedAction`` in state ``accepted`` whose
    type is **SOLUTION** — a MITIGATION is by definition not a fix of the cause
    (INV-42), so a failed workaround must never establish that the cause was
    addressed. Per ``classify_solution_outcome``, ``accepted`` means the user
    *executed* it, and the turn is read from ``accepted_in_turn`` (EXECUTION),
    never ``proposed_in_turn`` (the OFFER). The NEWEST such turn wins — a failed
    fix is disconfirmed by what happened after the LAST fix, not the first.

    Deliberately NO compliance-gate fallback: ``solution_accepted`` records
    THAT a fix was executed but not WHEN, and flooring the window at 0 made
    every pre-fix symptom row on the case read as a post-fix persistence
    observation. A precondition that cannot be dated cannot establish "what
    happened after the fix", so it establishes nothing. On the no-ProposedAction
    shape M6's counterfactual arm simply does not fire — the evidence-based arm
    is unaffected and still demotes a genuinely refuted cause.
    """
    turns = [
        a.accepted_in_turn
        for a in (getattr(case, "proposed_actions", None) or [])
        if getattr(a, "state", None) == "accepted"
        # SOLUTION only — a MITIGATION is by definition NOT a fix of the cause
        # (the prompt: a mitigation "does NOT eliminate the root cause, so the
        # cause is still present"). A workaround that failed to relieve the
        # symptom says nothing about whether the cause was addressed, so it must
        # never establish "the cause was addressed yet the problem persisted"
        # and refute the root at belief 0.
        #
        # Enum OR raw string, the same read ``classify_solution_outcome`` does
        # (its ``_action_type_value`` is private to the domain module, so the
        # one-line equivalent is inlined rather than crossing the contracts
        # boundary): reading only ``.value`` would silently miss a string-typed
        # action and refuse M6 forever on that deployment. Failing closed is the
        # right DIRECTION for this gate, but not by accident.
        and getattr(
            getattr(a, "action_type", None), "value", getattr(a, "action_type", None)
        )
        == InvestigationActionType.SOLUTION.value
        # ``accepted_in_turn`` (EXECUTION), never ``proposed_in_turn`` (the
        # OFFER): keying on the proposal turn let evidence recorded in the very
        # turn the fix was offered — before it was ever run — satisfy "the
        # problem persisted afterwards". Actions accepted before this field
        # existed carry None and simply do not establish the precondition,
        # which is the fail-closed direction.
        and getattr(a, "accepted_in_turn", None) is not None
    ]
    if turns:
        return max(turns)
    return None


def _problem_persistence_observed_after(case: Case, fix_turn: int) -> bool:
    """Does the case OBSERVE the problem still present at/after ``fix_turn``? —
    M6's second precondition (#987).

    The observation is a ``SYMPTOM_EVIDENCE`` row collected at/after the fix
    turn: that is exactly the encoding the prompt's TREATMENT FAILURE PATH
    mandates for a fix that did not hold ("symptom_evidence: New symptoms that
    emerge after a failed fix"). ``>=`` and not ``>`` because turn granularity
    cannot order within-turn events — the user's "I ran it, still failing"
    lands the executed fix and the persisting symptom in ONE turn.

    Deliberately a POSITIVE observation, not the absence of a resolution: M6 is
    a destructive transition (it refutes the standing cause, zeroes its root's
    belief, and retracts the conclusion), so it must be established from
    something the case actually recorded. "Nothing said it was fixed" is not an
    observation that it stayed broken.
    """
    return any(
        getattr(e, "category", None) == EvidenceCategory.SYMPTOM_EVIDENCE
        and (getattr(e, "collected_at_turn", 0) or 0) >= fix_turn
        for e in (getattr(case, "evidence", None) or [])
    )


def _evidence_disconfirmation_provenance(hyp) -> str:
    """Provenance for an EVIDENCE-based M6 demotion (#987).

    The honest record when the cause fell to its own links rather than to a
    failed fix: what the engine derived, and the tally it derived it from. It
    deliberately makes NO claim about a fix having been applied — that is the
    counterfactual arm's claim, and asserting it here unestablished is exactly
    the fabrication this campaign removed.
    """
    links = getattr(hyp, "evidence_links", None) or []
    refuting = sum(1 for link in links if link.stance == EvidenceStance.REFUTES)
    supporting = sum(1 for link in links if link.stance == EvidenceStance.SUPPORTS)
    if getattr(hyp, "state", None) == HypothesisState.REFUTED:
        detail = (
            f"the hypothesis was refuted "
            f"({hyp.refutation_reason or 'no reason recorded'})"
        )
    else:
        detail = (
            f"its evidence links net-refute it "
            f"({refuting} refuting vs {supporting} supporting)"
        )
    return f"the identified cause no longer stands — {detail}"[:400]


def _has_undatable_solution_acceptance(case: Case) -> bool:
    """An accepted SOLUTION carrying NO ``accepted_in_turn`` — a fix the case
    records as executed but cannot date (#987).

    Only reachable on acceptances stamped before ``accepted_in_turn`` existed,
    so this is a TRANSITION signal that drains as in-flight cases close, not a
    steady-state one. It exists purely so the refusal metric can separate "a
    fix ran, we just can't date it" from "nothing was ever tried" — the former
    is a real (bounded) suppression of legitimate failed-fix demotions and
    should be visible as such.
    """
    return any(
        getattr(a, "state", None) == "accepted"
        and getattr(
            getattr(a, "action_type", None), "value", getattr(a, "action_type", None)
        )
        == InvestigationActionType.SOLUTION.value
        and getattr(a, "accepted_in_turn", None) is None
        for a in (getattr(case, "proposed_actions", None) or [])
    )


def m6_disconfirmation_basis(case: Case) -> tuple[int, str] | None:
    """M6's ESTABLISHED preconditions, or ``None`` (metered by refusal reason).

    Returns ``(fix_turn, provenance)`` when the case record establishes BOTH
    halves of "the cause was addressed, yet the problem persisted":

    1. a RECORDED fix application (``_fix_application_turn``), and
    2. an OBSERVED persistence of the problem at/after it
       (``_problem_persistence_observed_after``), with
    3. NO qualifying cause-elimination row at/after the fix turn
       (``cause_elimination_rows``). KNOWN GAP (#1927): this was written when
       one causal_absence row was read as the whole gone⇒gone confirmation.
       Since #1906 that row records only the cause observed removed, which
       does not contradict "the problem persisted" — cause gone with the
       problem still present IS the counterfactual disconfirmation, and this
       precondition withholds it. Left as it was pending #1927, which owns the
       redesign of preconditions 2 and 3.

    Why this gate exists (#987): M6 previously fired on the mere presence of a
    counterfactual refute and then MINTED a row asserting "the cause was
    addressed or confirmed correct, yet the problem persisted" — a fact it had
    never checked. In the incident it was false: the fix had SUCCEEDED, the LLM
    had mis-linked its own success-confirmation row as a REFUTES, and M6
    laundered that into a fabricated observation which refuted the true root at
    belief 0 and retracted a correct conclusion.

    The rule, stated once: **constructive transitions may be derived from
    confirmation plus evidence with recorded provenance; DESTRUCTIVE
    transitions require established preconditions.** M6 is destructive, so it
    establishes. The category gate at ingest closes the specific #987 route;
    this closes the mechanism, which stays reachable from any future path that
    can put a refutation on a cause.

    **SCOPE — the counterfactual (failed-fix) arm ONLY.** This is a precondition
    for the CLAIM "a fix was applied and the problem persisted", which is about
    events outside the graph. It is NOT a precondition for demotion in general:
    an EVIDENCE-based disconfirmation (the hypothesis REFUTED or net-refuted by
    its own links) is grounded in the graph and demotes unconditionally — see
    ``_disconfirmed_cause_trigger``'s ``kind``. Gating both on fix-application
    evidence was the over-broad first cut of this fix, and it left a net-refuted
    cause standing as IDENTIFIED with its conclusion intact.

    Refusing the counterfactual arm does NOT leave a disproven cause standing:
    the hypothesis keeps whatever state the model gave it, a REFUTED hypothesis
    stops being STANDING (so ``any_chain_root_validated`` no longer grounds
    ``cause_state``), and ``retract_disconfirmed_rcc`` still clears a conclusion
    naming it. What is withheld is only the DURABLE engine refutation.
    """
    fix_turn = _fix_application_turn(case)
    if fix_turn is None:
        # Two different worlds, separately labeled: nothing was ever tried, vs
        # a fix WAS executed but carries no execution turn — an acceptance
        # stamped before ``accepted_in_turn`` existed. Both refuse (an undatable
        # precondition establishes nothing about "after the fix"), but only the
        # second is a TRANSITION artifact that drains as in-flight cases close.
        # Folding them into one series would teach operators to read a real
        # suppression window as the benign "nothing was tried" baseline.
        m6_demotion_refused_total.labels(
            reason=(
                "undatable_acceptance"
                if _has_undatable_solution_acceptance(case)
                else "no_fix_applied"
            )
        ).inc()
        return None
    if any(
        (getattr(row, "collected_at_turn", 0) or 0) >= fix_turn
        for row in cause_elimination_rows(case)
    ):
        m6_demotion_refused_total.labels(reason="resolution_confirmed").inc()
        return None
    if not _problem_persistence_observed_after(case, fix_turn):
        m6_demotion_refused_total.labels(reason="no_persistence").inc()
        return None
    return fix_turn, (
        f"a fix recorded as EXECUTED at turn {fix_turn} did not hold — symptom "
        f"evidence at/after that turn observes the problem still present, and "
        f"no resolution confirmation stands"
    )


def _attach_engine_refutation(
    case: Case, node_id: str, reason: str, provenance: str
) -> None:
    """Attach a DURABLE engine-authored REFUTES link (+ backing row) to
    ``node_id`` — the Option-(c) mechanism that makes M6 evidence-driven.
    Without a persisted refuting fact, ``derive_node_states`` would re-validate
    the root next turn from the stale supporting evidence and resurrect the
    disconfirmed cause (the turn-28 bug).

    Idempotent on the ENGINE's own refutation specifically: skips only when
    the node already carries an ENGINE-authored CAUSAL_ABSENCE refute. An
    LLM-recorded decisive refute on the node deliberately does NOT suppress
    the mint — the engine row is the durable failed-fix MARKER the
    disconfirmation window keys on (``cause_assurance``: authorship-keyed so
    pruning cannot collapse it), so "M6 mints exactly one engine row per
    disconfirmation" must hold even when the model already recorded the
    failure itself (reviewed: suppressing it left the window at -1 and let a
    stale premature 'stable' row re-qualify as the resolution confirmation).

    The row records an ENGINE INFERENCE WITH PROVENANCE, never an observation
    (#987). It is engine-authored (``collected_by=ENGINE_EVIDENCE_AUTHOR`` —
    the marker every reader already keys on, and the reason
    ``cause_elimination_rows`` excludes it from ever reading as a
    confirmation), and its text now states what the engine DERIVED and the
    case records it derived it FROM — supplied by ``m6_disconfirmation_basis``,
    which is the only caller and which fires only on ESTABLISHED preconditions.
    It previously asserted a first-person observation ("the cause was addressed
    ... yet the problem persisted") that no one had checked; that sentence was
    false in the #987 incident and is what made a fabrication durable.

    Residual, stated: the row still lives in the evidence table under
    ``CAUSAL_ABSENCE_EVIDENCE`` because the whole disconfirmation-window
    machinery (``_engine_absence_row_ids``, ``latest_disconfirmation_turn``,
    ``_disconfirmation_row_ids``) is keyed on that category × engine
    authorship. Its authorship already excludes it from every observation
    reader; giving engine inferences their own category is a larger,
    separately-reviewable change.
    """
    node = case.causal_nodes.get(node_id)
    if node is None or _node_has_engine_counterfactual_refute(node, case):
        return
    ev_id = f"ev_{uuid4().hex[:12]}"
    case.evidence.append(
        Evidence(
            evidence_id=ev_id,
            summary=f"Engine inference (M6), not an observation: {provenance}."[:500],
            primary_purpose="engine inference: failed-treatment disconfirmation",
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            # USER_DESCRIPTION is the only source_type the evidence_source_invariant
            # CHECK permits with no source_file_id (this engine-authored fact has no
            # file); collected_by marks the true author.
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by=ENGINE_EVIDENCE_AUTHOR,
            collected_at_turn=case.current_turn,
            collected_at=datetime.now(timezone.utc),
        )
    )
    node.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=ev_id,
            stance=EvidenceStance.REFUTES,
            reasoning=reason,
            linked_at_turn=case.current_turn,
        )
    )


def demote_disconfirmed_cause_via_evidence(case: Case) -> bool:
    """M6 (Option c): on counterfactual disconfirmation of the grounded cause,
    refute the flat hypothesis AND attach a DURABLE engine refutation to its
    root, then retract the conclusion. Rather than imperatively flipping
    ``node_state``, the root's refutation is recorded as EVIDENCE so the
    subsequent
    ``derive_node_states`` — this turn and every later turn — keeps the root
    REFUTED instead of re-validating it from the now-stale supporting evidence.

    Shares the M6 trigger with the flat path (``_disconfirmed_cause_trigger``),
    which ALSO fires when the disconfirmation lands on the root NODE rather than
    the flat hypothesis — so the conclusion is retracted even when only the node
    was refuted, closing the truth-split where ``cause_state`` drops but a stale
    ``RootCauseConclusion`` lingers. Returns True if it acted.

    Each disconfirmation KIND records only what the engine can substantiate
    (#987):

    - ``evidence`` — the hypothesis is refuted/net-refuted by its OWN links.
      Fires unconditionally (it was always evidence-grounded) and records an
      evidence-based inference. Gating THIS on fix-application evidence was the
      over-broad first cut of the #987 fix: it left a net-refuted cause standing
      as IDENTIFIED with its conclusion intact.
    - ``counterfactual`` — the FAILED-FIX claim, about events outside the graph.
      Fires only on preconditions the case record ESTABLISHES
      (``m6_disconfirmation_basis``); otherwise the demotion is refused and
      metered. The engine must never assert a failed fix it did not establish —
      that assertion, minted as a durable row, is what made #987 permanent.

    Either way the hypothesis's own state still governs downstream, so a
    genuinely disproven cause stops grounding ``cause_state`` regardless.
    """
    p = case.progress
    # Chain path: a counterfactual refute on the root NODE also disconfirms the
    # cause. After #987 the only producer of such a link is the engine's own
    # durable marker (the category gate at ingest refuses model-authored links
    # on absence rows), so this arm is what keeps a prior M6 LATCHED across
    # turns rather than a fresh LLM-driven entry point.
    trigger = _disconfirmed_cause_trigger(case, node_side=True)
    if trigger is None:
        return False
    hyp, reason, kind = trigger

    if kind == "counterfactual":
        basis = m6_disconfirmation_basis(case)
        if basis is None:
            return False
        provenance = basis[1]
    else:
        provenance = _evidence_disconfirmation_provenance(hyp)

    if hyp.state != HypothesisState.REFUTED:
        HypothesisManager().refute_hypothesis(hyp, case.current_turn, [], reason)
    if hyp.root_node_id:
        _attach_engine_refutation(case, hyp.root_node_id, reason, provenance)
    # Retract the conclusion so the disposition layer cannot keep treating the
    # cause as known; the cause_state itself is re-derived from the (now refuted)
    # root by the caller's derive + recompute.
    #
    # §7.6 / INV-34 refresh: for a LINKED conclusion the trigger's ``hyp`` IS the
    # conclusion's named cause — ``link_llm_rcc_to_cause`` runs earlier this
    # recompute, so ``_representative_cause_hypothesis`` resolves to the hypothesis
    # the LLM's conclusion names; a conclusion re-grounded onto a DIFFERENT
    # still-standing cause points the trigger there and this demotion never fires
    # on it (the refresh is delivered by the link). RESIDUAL, accepted: an
    # UNLINKABLE conclusion (ambiguous/paraphrased text, no vhid) leaves the
    # trigger on the max-``initial_likelihood`` proxy, so if that proxy is the
    # disconfirmed cause the conclusion is still cleared even when it named a
    # different cause — the pre-existing NO-INCORRECT-first behavior (never leave a
    # possibly-disproven conclusion standing on a guess; inferring a
    # different-cause link lexically would only trade this for the wrong-link
    # collapse the link bar guards against).
    rcc = case.root_cause_conclusion
    # Count only a genuine "named cause was disconfirmed" retraction: the cleared
    # conclusion was LINKED to the very hypothesis this demotion disconfirmed. A
    # collateral proxy-path wipe of an unlinkable conclusion is not that event, so
    # it must not inflate the failed-fix signal (lifecycle-metrics § INV-34).
    if (
        rcc is not None
        and getattr(rcc, "determined_by", None) != _ENGINE_RCC_AUTHOR
        and getattr(rcc, "validated_hypothesis_id", None) == hyp.hypothesis_id
    ):
        llm_rcc_retracted_disconfirmed_total.inc()
    case.root_cause_conclusion = None
    p.root_cause_likelihood = 0.0
    return True
