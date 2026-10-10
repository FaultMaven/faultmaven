from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from faultmaven.core.investigation.cause_assurance import (
    ENGINE_EVIDENCE_AUTHOR,
    cause_elimination_rows,
    counterfactual_link_decisive,
    fix_application_turn,
    has_resolution_confirmation,
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
    m6_record_disconfirmation_total,
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
) -> tuple[Hypothesis, str, str] | None:
    """Shared M6 trigger for both demote paths. Returns ``(hypothesis, reason,
    kind)`` when a grounded (``cause_state=IDENTIFIED``) case's representative
    cause is disconfirmed, else None. Disconfirmation is the hypothesis being
    REFUTED or net-refuted (``_net_refuted``); and — only when ``node_side``
    (the chain-mode case the prompt mandates) — its ROOT node carrying a
    counterfactual (CAUSAL_ABSENCE) refutation, or the case record
    establishing a failed fix of it, even when the flat hypothesis was
    untouched.

    ``node_side`` is FALSE for the flat path so its behavior is exactly as
    before: a persisted root (reloaded regardless of the flag, e.g. after the
    flag is flipped off) with a counterfactual refute must NOT demote a healthy
    flat hypothesis — node-derived disconfirmation is a chain-mode concept.

    **``kind`` distinguishes the materially different claims that share this
    trigger (#987):**

    - ``"evidence"`` — the hypothesis is REFUTED or net-refuted by its OWN
      evidence links. This asserts nothing about any fix; it is grounded in the
      links themselves and needs no external precondition.
    - ``"counterfactual"`` — the FAILED-FIX claim: the root carries a
      counterfactual (CAUSAL_ABSENCE) refutation, i.e. "the cause was addressed
      yet the problem persisted". That claim is about events outside the graph,
      so it is the one M6 must ESTABLISH rather than infer.

    - ``"record"`` — the same FAILED-FIX claim, read from the case record
      rather than from a link (#1927): the cause observed removed after an
      executed fix, the problem observed present after that
      (``_record_disconfirms_cause``). The ingest gate leaves the engine as the
      only author of a node-side counterfactual refute, so ``"counterfactual"``
      is the LATCH of a disconfirmation M6 already recorded; without this arm
      a fix that removed the cause but not the problem demoted nothing unless
      the model refuted the cause itself.

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
    reason = (hyp.refutation_reason or _DISCONFIRMATION_REASON)[:200]
    if _hypothesis_disconfirmed(hyp):
        return hyp, reason, "evidence"
    if not node_side or root is None:
        return None
    if _node_has_counterfactual_refute(root, _evidence_category_map(case)):
        return hyp, reason, "counterfactual"
    if _record_disconfirms_cause(case, hyp, root):
        return hyp, reason, "record"
    return None


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


def _problem_persistence_observed_after(
    case: Case, fix_turn: int, cause_removal_turn: int | None = None
) -> bool:
    """Does the case OBSERVE the problem still present after the fix? — M6's
    second precondition (#987), ordered after the cause's removal when one was
    observed (#1927).

    The observation is a ``SYMPTOM_EVIDENCE`` row: that is exactly the encoding
    the prompt's TREATMENT FAILURE PATH mandates for a fix that did not hold
    ("symptom_evidence: New symptoms that emerge after a failed fix").

    - No cause removal observed (``cause_removal_turn`` None): any such row
      at/after the fix turn. ``>=`` and not ``>`` because turn granularity
      cannot order within-turn events — the user's "I ran it, still failing"
      lands the executed fix and the persisting symptom in ONE turn.
    - The cause observed removed at ``cause_removal_turn``: the row must be at
      or after that observation, and in a turn AFTER the fix's execution turn.
      A row from before the cause row shows the problem present while the
      cause may still have been there (a mis-applied fix, then corrected), not
      the problem outliving the cause. A row in the fix's own turn may quote
      pre-fix lines from the same paste — a pasted journal carries the earlier
      failures — so it cannot be ordered after the fix. ``>=`` on the cause
      row, because "config reads 100, still 503" is one turn's observation.

    Deliberately a POSITIVE observation, not the absence of a resolution: M6 is
    a destructive transition (it refutes the standing cause, zeroes its root's
    belief, and retracts the conclusion), so it must be established from
    something the case actually recorded. "Nothing said it was fixed" is not an
    observation that it stayed broken.
    """
    for e in getattr(case, "evidence", None) or []:
        if getattr(e, "category", None) != EvidenceCategory.SYMPTOM_EVIDENCE:
            continue
        turn = getattr(e, "collected_at_turn", 0) or 0
        if cause_removal_turn is None:
            if turn >= fix_turn:
                return True
        elif turn >= cause_removal_turn and turn > fix_turn:
            return True
    return False


def _cause_removal_turn(case: Case, fix_turn: int) -> int | None:
    """The earliest turn at/after the fix at which a qualifying cause-leg row
    (``cause_elimination_rows``) observes the cause removed, or None."""
    turns = [
        getattr(row, "collected_at_turn", 0) or 0
        for row in cause_elimination_rows(case)
        if (getattr(row, "collected_at_turn", 0) or 0) >= fix_turn
    ]
    return min(turns, default=None)


@dataclass(frozen=True)
class _FailedFixRecord:
    """What the case record establishes about the latest fix: the execution
    turn, the earliest observation of the cause removed after it, and the
    refusal label when the record does NOT establish a failed fix (None when
    it does)."""

    fix_turn: int | None
    cause_removal_turn: int | None
    refusal: str | None


def _failed_fix_record(case: Case) -> _FailedFixRecord:
    """Read M6's counterfactual preconditions off the case record, unmetered.
    ``m6_disconfirmation_basis`` meters the refusal; the record trigger
    (``_record_disconfirms_cause``) evaluates this on every grounded recompute,
    where a refusal is not a refused demotion but the ordinary state of a case
    whose fix has not failed."""
    fix_turn = fix_application_turn(case)
    if fix_turn is None:
        # Two different worlds, separately labeled: nothing was ever tried, vs
        # a fix WAS executed but carries no execution turn — an acceptance
        # stamped before ``accepted_in_turn`` existed. Both refuse (an undatable
        # precondition establishes nothing about "after the fix"), but only the
        # second is a TRANSITION artifact that drains as in-flight cases close.
        # Folding them into one series would teach operators to read a real
        # suppression window as the benign "nothing was tried" baseline.
        refusal = (
            "undatable_acceptance"
            if _has_undatable_solution_acceptance(case)
            else "no_fix_applied"
        )
        return _FailedFixRecord(None, None, refusal)
    if has_resolution_confirmation(case):
        return _FailedFixRecord(fix_turn, None, "resolution_confirmed")
    removal_turn = _cause_removal_turn(case, fix_turn)
    if not _problem_persistence_observed_after(case, fix_turn, removal_turn):
        refusal = (
            "no_persistence"
            if removal_turn is None
            else "no_persistence_after_cause_removal"
        )
        return _FailedFixRecord(fix_turn, removal_turn, refusal)
    return _FailedFixRecord(fix_turn, removal_turn, None)


def _record_disconfirms_cause(case: Case, hyp: Hypothesis, root: CausalNode) -> bool:
    """Does the case RECORD establish that a fix of this identified cause
    removed the cause but not the problem? — the ``"record"`` trigger (#1927).

    All of: the hypothesis is still standing and its root VALIDATED (the cause
    the case identifies, not the representative proxy's guess); the fix ran
    at/after the hypothesis existed (a fix can only have addressed a cause
    that existed when it ran — a later cause is not disconfirmed by an earlier
    fix's record); the cause observed removed after the fix; the problem
    observed present after that; and no resolution confirmed
    (``_failed_fix_record``).

    The cause row is REQUIRED here, unlike in ``m6_disconfirmation_basis``: "I
    ran it, still failing" with no cause re-check may be an implementation
    error (the FAILURE PATH's first branch — the cause is still present),
    which disconfirms nothing. The cause observed removed is what makes the
    persistence a counterfactual."""
    if hyp.state.is_terminal or root.node_state != NodeState.VALIDATED:
        return False
    record = _failed_fix_record(case)
    return (
        record.refusal is None
        and record.cause_removal_turn is not None
        and (getattr(hyp, "generated_at_turn", 0) or 0) <= record.fix_turn
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

    1. a RECORDED fix application (``fix_application_turn``), and
    2. an OBSERVED persistence of the problem after it
       (``_problem_persistence_observed_after``) — ordered after the cause's
       removal when a cause-leg row (``cause_elimination_rows``) observes it
       at/after the fix, and
    3. NO resolution confirmed (``has_resolution_confirmation``): the problem
       not observed gone after the fix.

    Precondition 3 is the CONTRADICTION rule (#1927). It used to withhold on
    any cause-leg row at/after the fix, from when one causal_absence row was
    read as the whole gone⇒gone confirmation. Since #1906 that row records
    only the cause observed removed, which does not contradict "the problem
    persisted" — the cause gone with the problem still present IS the
    counterfactual disconfirmation, and the cause row is half of it. What
    contradicts persistence is the problem observed gone, which is the other
    leg. So a lone cause row followed by persistence is a FAILED fix: under
    the #1906 contract a lone cause row is the state in which the prompt asks
    for the symptom check, and a symptom row after it is the answer.

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
    record = _failed_fix_record(case)
    if record.refusal is not None:
        m6_demotion_refused_total.labels(reason=record.refusal).inc()
        return None
    if record.cause_removal_turn is None:
        return record.fix_turn, (
            f"a fix recorded as EXECUTED at turn {record.fix_turn} did not hold "
            f"— symptom evidence at/after that turn observes the problem still "
            f"present, and no resolution confirmation stands"
        )
    return record.fix_turn, (
        f"a fix recorded as EXECUTED at turn {record.fix_turn} did not hold — "
        f"the cause was observed removed at turn {record.cause_removal_turn}, "
        f"symptom evidence after that observes the problem still present, and "
        f"the problem is not observed gone"
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


def _mark_failed_fix_cause_rows(case: Case, hyp: Hypothesis, node: CausalNode) -> None:
    """Record the cause rows of the fix that just failed as part of its
    disconfirmation: an engine REFUTES link from each to the cause's root
    (#1927).

    A failed fix's cause row ("max_connections now reads 100") observes the
    cause removed, and after M6 the cause is disconfirmed — so the row must no
    longer stand as the cause leg of a resolution. The disconfirmation window
    (``latest_disconfirmation_turn``) cannot drop it when it shares M6's turn:
    the window's ``>=`` keeps same-turn rows, for the mixed single-turn shape.
    So without this, a later mitigation's problem-gone row completed both legs
    on it, and the case read resolution-READY on a cause its own fix had
    disproven. ``cause_elimination_rows`` already excludes a row REFUTES-linked
    to a cause the engine marked disconfirmed (``_disconfirmation_row_ids``);
    this records the link, it does not change the rule.

    Which cause: only the IDENTIFIED one — the caller passes the root only
    when it was VALIDATED at trigger time. With no conclusion naming its
    cause, M6's representative is a likelihood proxy that can point at a
    candidate no fix addressed; marking on its refutation would strip a
    SUCCEEDED fix's cause row and leave a resolved case unable to resolve.

    Which rows: the qualifying cause-leg rows at/after the latest fix — the
    fix whose failure disconfirmed this cause. Not when a resolution is
    confirmed (the rows then confirm something, the mixed shape), not when
    the fix ran before the hypothesis existed (its rows are about another
    cause), and not when no fix is recorded. Runs on either disconfirming arm:
    when the model refuted the cause in the cause row's turn, it has judged
    the fix failed, and the same row would otherwise survive.

    Engine-minted, as INV-42 requires of every link on an absence row: the
    model's own links on absence rows are refused at ingest."""
    fix_turn = fix_application_turn(case)
    if (
        fix_turn is None
        or (getattr(hyp, "generated_at_turn", 0) or 0) > fix_turn
        or has_resolution_confirmation(case)
    ):
        return
    linked = {link.evidence_id for link in node.evidence_links}
    for row in cause_elimination_rows(case):
        if (getattr(row, "collected_at_turn", 0) or 0) < fix_turn:
            continue
        if row.evidence_id in linked:
            continue
        node.evidence_links.append(
            NodeEvidenceLink(
                evidence_id=row.evidence_id,
                stance=EvidenceStance.REFUTES,
                reasoning=(
                    "failed fix: this row observed the cause removed after the "
                    "fix, and the cause was then disconfirmed"
                ),
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
    - ``counterfactual`` / ``record`` — the FAILED-FIX claim, about events
      outside the graph, reached through the root's engine marker (the latch)
      or read straight off the case record (#1927). Fires only on
      preconditions the case record ESTABLISHES (``m6_disconfirmation_basis``);
      otherwise the demotion is refused and metered. The engine must never
      assert a failed fix it did not establish — that assertion, minted as a
      durable row, is what made #987 permanent.

    Either way the hypothesis's own state still governs downstream, so a
    genuinely disproven cause stops grounding ``cause_state`` regardless, and
    the failed fix's cause rows stop standing as a resolution's cause leg
    (``_mark_failed_fix_cause_rows``).
    """
    p = case.progress
    # Chain path: a counterfactual refute on the root NODE also disconfirms the
    # cause. After #987 the only producer of such a link is the engine itself
    # (the category gate at ingest refuses model-authored links on absence
    # rows), so that arm is what keeps a prior M6 LATCHED across turns; the
    # fresh failed-fix entry point is the case record (``kind == "record"``).
    trigger = _disconfirmed_cause_trigger(case, node_side=True)
    if trigger is None:
        return False
    hyp, reason, kind = trigger
    # Read before anything below refutes it: whether the demoted cause is the
    # one the case identified, or the representative proxy's unvalidated pick.
    root = case.causal_nodes.get(hyp.root_node_id) if hyp.root_node_id else None
    identified_root = (
        root if root is not None and root.node_state == NodeState.VALIDATED else None
    )

    if kind == "evidence":
        provenance = _evidence_disconfirmation_provenance(hyp)
    else:
        basis = m6_disconfirmation_basis(case)
        if basis is None:
            return False
        provenance = basis[1]
        if kind == "record":
            m6_record_disconfirmation_total.inc()

    if hyp.state != HypothesisState.REFUTED:
        HypothesisManager().refute_hypothesis(hyp, case.current_turn, [], reason)
    if hyp.root_node_id:
        # Mark before the engine row is minted: its turn becomes the
        # disconfirmation window, and the rows to mark are read through it.
        if identified_root is not None:
            _mark_failed_fix_cause_rows(case, hyp, identified_root)
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
