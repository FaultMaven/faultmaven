"""INV-30 calibration home: the causal-absence trust discipline (#656).

Two coupled surfaces, pinned against realistic row content:

1. **Stamp bearing** (``cause_assurance._select_bearing_row``): which absence
   row the RESOLVED confirm-stamp may cite as a root's gone⇒gone proof —
   frame-bearing preferred, generic accepted (the user's handshake is the
   signal), bears-elsewhere refused.
2. **Gate qualification** (``cause_assurance.resolution_confirmation_rows``):
   which rows satisfy the resolution-readiness READY bar at all — non-engine,
   newer than the latest failed-fix disconfirmation.

Calibration figures (the ``_BEARING_MIN_SHARED_TOKENS`` floor, the accepted
lexical limits) live HERE as executable pins; methodology prose: §9.5.
Accepted limits, pinned deliberately:

- The check is lexical: a row about another chain phrased in synonyms escapes
  the elsewhere veto (reads generic → accepted). One layer of the #656
  defense, not the whole of it.
- A generic row is accepted even when a frame-bearing row is *possible* but
  absent — the handshake carries the confirmation; refusing generic rows was
  the stuck-loop / count-held-stranding shape (NO-COLLAPSE).
"""

import hashlib
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.cause_assurance import (
    CauseAssuranceGrade,
    confirm_root_from_resolution_absence,
    grade_cause_assurance,
    has_resolution_confirmation,
    resolution_confirmation_rows,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _absence_row(label, summary, *, turn=6, collected_by="llm") -> Evidence:
    return Evidence(
        evidence_id=_eid(label),
        summary=summary,
        primary_purpose="resolution verification",
        category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by=collected_by,
        collected_at_turn=turn,
        collected_at=datetime.now(timezone.utc),
    )


def _node(node_id, statement, node_type, *, validated=False) -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement=statement,
        node_type=node_type,
        node_state=NodeState.VALIDATED if validated else NodeState.CANDIDATE,
        validation_method=(
            ValidationMethod.EMPIRICAL if validated else ValidationMethod.NONE
        ),
        belief=0.9 if validated else 0.5,
        actionable=validated and node_type == NodeType.ROOT,
        evidence_links=[],
        generated_at_turn=1,
    )


def _incident_case():
    """The realistic multi-chain shape: a validated target root with a
    mechanism rung, plus a refuted sibling chain the confirmation must never
    be attributed against.

    Target chain: resolv.conf nameserver overflow → DNS timeouts → D.
    Sibling chain (refuted): node memory pressure evictions → D.
    """
    d = _node(
        "cn_00000000000d",
        "Pods in the payments namespace are stuck in CrashLoopBackOff",
        NodeType.PROBLEM,
    )
    target = _node(
        "cn_0000000000aa",
        "resolv.conf in the pod spec lists five nameservers, exceeding the "
        "glibc resolver limit",
        NodeType.ROOT,
        validated=True,
    )
    rung = _node(
        "cn_0000000000ab",
        "DNS lookups inside the payment pods intermittently time out",
        NodeType.INTERMEDIATE,
    )
    sibling = _node(
        "cn_0000000000bb",
        "Memory pressure on the worker node is evicting the payment pods",
        NodeType.ROOT,
    )
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=7,
        inquiry=InquiryData(
            proposed_problem_statement="payments pods crashlooping",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="payments pods crashlooping",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.causal_nodes = {n.node_id: n for n in (d, target, rung, sibling)}
    case.causal_edges = [
        CausalEdge(cause_node_id=target.node_id, effect_node_id=rung.node_id),
        CausalEdge(cause_node_id=rung.node_id, effect_node_id=d.node_id),
        CausalEdge(cause_node_id=sibling.node_id, effect_node_id=d.node_id),
    ]
    hyp = Hypothesis(
        hypothesis_id="hyp_000000000001",
        statement="Too many nameservers in resolv.conf break glibc resolution",
        category=HypothesisCategory.CONFIG,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=target.node_id,
        generated_at_turn=1,
    )
    case.hypotheses = {hyp.hypothesis_id: hyp}
    case.progress.symptom_verified = True
    return case, target, sibling


def _stamped_row_id(case, root):
    supports = [
        link.evidence_id
        for link in root.evidence_links
        if link.stance == EvidenceStance.SUPPORTS
    ]
    assert len(supports) == 1
    return supports[0]


# ---------------------------------------------------------------------------
# Stamp bearing: frame-bearing rows (true positives)
# ---------------------------------------------------------------------------


def test_compliant_verify_row_naming_the_cause_is_cited():
    """The prompt-contract shape — names the removed cause — bears on the
    frame and is cited; the grade reaches CONFIRMED."""
    case, target, _ = _incident_case()
    row = _absence_row(
        "compliant",
        "Root cause no longer present after the fix: resolv.conf now lists "
        "two nameservers and pods are Running",
    )
    case.evidence.append(row)
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == row.evidence_id
    assert grade_cause_assurance(case) == CauseAssuranceGrade.CONFIRMED


def test_symptom_side_confirmation_bears_via_problem_anchor():
    """A confirmation phrased against the SYMPTOM ('the CrashLoopBackOff is
    gone') confirms gone⇒gone for the sole standing root without naming it."""
    case, target, _ = _incident_case()
    row = _absence_row(
        "symptom_side",
        "CrashLoopBackOff no longer observed on the payments namespace pods "
        "after the fix",
    )
    case.evidence.append(row)
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == row.evidence_id


def test_mechanism_rung_confirmation_is_frame_bearing():
    """A row about the target chain's RUNG (its mechanism) is the target's
    story, not another chain's — frame-bearing, never vetoed."""
    case, target, _ = _incident_case()
    row = _absence_row(
        "rung_side",
        "DNS lookups inside the payment pods no longer time out after the " "change",
    )
    case.evidence.append(row)
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == row.evidence_id


def test_row_mentioning_both_chains_counts_as_frame_bearing():
    """Frame wins when a row bears on BOTH (e.g. contrasts the fix with the
    ruled-out sibling): the elsewhere veto applies only below the frame bar."""
    case, target, _ = _incident_case()
    row = _absence_row(
        "both",
        "Fixed the resolv.conf nameservers issue (memory pressure eviction "
        "theory was ruled out); pods stable",
    )
    case.evidence.append(row)
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == row.evidence_id


# ---------------------------------------------------------------------------
# Stamp bearing: generic rows (accepted — the handshake is the signal)
# ---------------------------------------------------------------------------


def test_terse_generic_confirmation_is_accepted():
    """'User confirms it's working' bears on nothing — but the RESOLVED
    handshake is the confirmation signal and a terse row must not strand the
    case (NO-COLLAPSE; the INV-29 count-held rescue depends on this)."""
    case, target, _ = _incident_case()
    row = _absence_row("terse", "User confirms it's working now")
    case.evidence.append(row)
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == row.evidence_id


def test_boilerplate_only_row_is_accepted():
    """Pure prompt-contract boilerplate with no case-specific content is not
    vetoed (it bears on no OTHER chain), and as the newest survivor it is the
    citation — recency, not specificity, picks the row."""
    case, target, _ = _incident_case()
    older_specific = _absence_row(
        "older_specific_bp",
        "resolv.conf nameservers trimmed; CrashLoopBackOff gone",
        turn=5,
    )
    boilerplate = _absence_row(
        "boilerplate", "Root cause no longer present after the fix", turn=6
    )
    case.evidence.extend([older_specific, boilerplate])
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == boilerplate.evidence_id


def _frame_tokens(case, target):
    from faultmaven.core.investigation.cause_assurance import (
        content_tokens,
        problem_anchor_statements,
    )

    frame = set()
    for anchor in problem_anchor_statements(case):
        frame |= content_tokens(anchor)
    frame |= content_tokens(target.statement)
    for n in case.causal_nodes.values():
        if n.node_id == "cn_0000000000ab":  # the target's mechanism rung
            frame |= content_tokens(n.statement)
    for h in case.hypotheses.values():
        if h.root_node_id == target.node_id:
            frame |= content_tokens(h.statement)
    return frame


def test_bearing_floor_boundary_one_frame_token_gives_no_veto_immunity():
    """_BEARING_MIN_SHARED_TOKENS boundary, low side: a row sharing ≥2 tokens
    with a SIBLING and exactly ONE with the frame has no immunity — vetoed."""
    from faultmaven.core.investigation.cause_assurance import content_tokens

    case, target, sibling = _incident_case()
    boundary_text = (
        "Memory pressure gone after maintenance; one nameserver responded slowly"
    )
    row_tokens = content_tokens(boundary_text)
    assert len(row_tokens & _frame_tokens(case, target)) == 1
    assert len(row_tokens & content_tokens(sibling.statement)) >= 2
    case.evidence.append(_absence_row("boundary_one", boundary_text))
    assert confirm_root_from_resolution_absence(case) is False


def test_bearing_floor_boundary_two_frame_tokens_give_veto_immunity():
    """_BEARING_MIN_SHARED_TOKENS boundary, high side: EXACTLY TWO shared
    frame tokens clear the floor — the row is immune to the elsewhere veto
    even though it also bears on the sibling."""
    from faultmaven.core.investigation.cause_assurance import content_tokens

    case, target, sibling = _incident_case()
    boundary_text = "Memory pressure gone; nameserver handling by glibc now succeeds"
    row_tokens = content_tokens(boundary_text)
    assert len(row_tokens & _frame_tokens(case, target)) == 2
    assert len(row_tokens & content_tokens(sibling.statement)) >= 2
    row = _absence_row("boundary_two", boundary_text)
    case.evidence.append(row)
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == row.evidence_id


def test_elsewhere_floor_boundary_one_shared_token_is_not_refused():
    """The elsewhere veto has the same floor: ONE shared token with another
    chain is a coincidence — the row is accepted."""
    from faultmaven.core.investigation.cause_assurance import content_tokens

    case, target, sibling = _incident_case()
    boundary_text = "Pressure test of the loader completed with no issues"
    row_tokens = content_tokens(boundary_text)
    assert len(row_tokens & _frame_tokens(case, target)) == 0
    assert len(row_tokens & content_tokens(sibling.statement)) == 1
    row = _absence_row("elsewhere_one", boundary_text)
    case.evidence.append(row)
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == row.evidence_id


def test_newest_survivor_cited_over_older_specific_row():
    """Recency beats specificity (reviewed): the cited confirmation is the row
    temporally closest to the RESOLVED handshake. An older frame-echoing row —
    premature rows echo frame tokens by construction — must never outrank the
    user's actual latest confirmation, however terse."""
    case, target, _ = _incident_case()
    specific = _absence_row(
        "older_specific",
        "resolv.conf nameservers trimmed to two; DNS resolution stable",
        turn=5,
    )
    generic = _absence_row("newer_generic", "User confirms all good now", turn=6)
    case.evidence.extend([specific, generic])
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == generic.evidence_id


# ---------------------------------------------------------------------------
# Stamp bearing: bears-elsewhere rows (refused)
# ---------------------------------------------------------------------------


def test_row_about_the_sibling_chain_is_refused():
    """A row affirmatively about the SIBLING chain must not be cited as the
    target root's counterfactual proof: no stamp, grade stays MECHANISTIC,
    and the refusal is metered."""
    case, target, _ = _incident_case()
    row = _absence_row(
        "sibling_row",
        "Memory pressure on the worker node returned to normal after "
        "eviction tuning",
    )
    case.evidence.append(row)
    with patch(
        "faultmaven.core.investigation.cause_assurance."
        "absence_confirmation_bearing_rejected_total"
    ) as counter:
        assert confirm_root_from_resolution_absence(case) is False
    assert counter.inc.call_count == 1
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC
    # The refusal never blocks the resolution itself: the readiness bar still
    # sees a qualifying (metadata-level) confirmation row.
    assert has_resolution_confirmation(case) is True


def test_elsewhere_refusal_falls_back_to_a_generic_candidate():
    """With one bears-elsewhere row and one generic row, the generic one is
    cited and only the elsewhere row is refused."""
    case, target, _ = _incident_case()
    elsewhere = _absence_row(
        "sibling_row_2",
        "Worker node memory pressure evictions have stopped",
        turn=6,
    )
    generic = _absence_row("terse_2", "User confirms resolved", turn=5)
    case.evidence.extend([elsewhere, generic])
    with patch(
        "faultmaven.core.investigation.cause_assurance."
        "absence_confirmation_bearing_rejected_total"
    ) as counter:
        assert confirm_root_from_resolution_absence(case) is True
    assert counter.inc.call_count == 1
    assert _stamped_row_id(case, target) == generic.evidence_id


def test_synonym_paraphrase_of_sibling_escapes_the_veto():
    """ACCEPTED LEXICAL LIMIT (do not 'fix' without moving to a semantic
    comparison): a row about the sibling phrased entirely in synonyms shares
    no tokens with it, reads generic, and is accepted."""
    case, target, _ = _incident_case()
    row = _absence_row(
        "synonym_sibling",
        "RAM starvation kicked containers off the host; that has ceased",
    )
    case.evidence.append(row)
    assert confirm_root_from_resolution_absence(case) is True


# ---------------------------------------------------------------------------
# Gate qualification (shared metadata bar): resolution_confirmation_rows
# ---------------------------------------------------------------------------


def test_engine_disconfirmation_row_never_qualifies():
    """The engine only mints absence rows as M6 failed-fix DISCONFIRMATIONS —
    they must not satisfy 'confirmation the problem is now resolved'."""
    case, _, _ = _incident_case()
    case.evidence.append(
        _absence_row(
            "m6_row",
            "Counterfactual disconfirmation (M6): the cause was addressed or "
            "confirmed correct, yet the problem persisted.",
            collected_by="engine",
        )
    )
    assert has_resolution_confirmation(case) is False


def test_premature_row_from_engine_known_failed_fix_never_qualifies():
    """A premature 'stable' row from a fix window the ENGINE saw fail (M6
    minted its disconfirmation row at a later turn) confirms nothing."""
    case, target, sibling = _incident_case()
    premature = _absence_row("premature", "Pods look stable after rollout", turn=4)
    m6_row = _absence_row(
        "m6_fail",
        "Counterfactual disconfirmation (M6): the cause was addressed, yet "
        "the problem persisted.",
        turn=5,
        collected_by="engine",
    )
    case.evidence.extend([premature, m6_row])
    assert resolution_confirmation_rows(case) == []
    assert has_resolution_confirmation(case) is False


def _engine_marker(target, case, *, turn, label="m6_marker"):
    """Attach the M6 engine disconfirmation row + REFUTES link to ``target``
    — the durable failed-fix marker the scoped disqualification keys on."""
    m6_row = _absence_row(
        label,
        "Counterfactual disconfirmation (M6): the cause was addressed, yet "
        "the problem persisted.",
        turn=turn,
        collected_by="engine",
    )
    case.evidence.append(m6_row)
    target.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=m6_row.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="failed treatment",
            linked_at_turn=turn,
        )
    )
    return m6_row


def test_llm_row_refuting_the_engine_marked_cause_never_qualifies():
    """An LLM failed-fix row co-targeting the node the ENGINE marked
    disconfirmed is itself a disconfirmation, at ANY link confidence — it
    never reads as a confirmation, even at the same turn as the marker
    (the >= window alone would admit it)."""
    case, target, _ = _incident_case()
    _engine_marker(target, case, turn=5)
    llm_fail = _absence_row(
        "llm_fail", "Not sure the fix helped, errors may remain", turn=5
    )
    case.evidence.append(llm_fail)
    target.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=llm_fail.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="uncertain failed fix",
            linked_at_turn=5,
            stance_confidence=0.3,
        )
    )
    assert has_resolution_confirmation(case) is False


def test_sibling_exclusion_link_does_not_disqualify_the_row():
    """The dual-use emission (reviewed stuck-loop shape): ONE row records the
    confirmation AND rules out a sibling ('the fix worked — so it wasn't the
    eviction theory'). A REFUTES link to a node the engine never marked is
    proof-by-exclusion, not a failed fix — the row stays confirmable and the
    gate must not regress right after the user confirmed."""
    case, target, sibling = _incident_case()
    dual_use = _absence_row(
        "dual_use",
        "resolv.conf corrected and CrashLoopBackOff gone — memory pressure "
        "theory ruled out",
        turn=8,
    )
    case.evidence.append(dual_use)
    sibling.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=dual_use.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="fix on the other chain resolved D — this candidate excluded",
            linked_at_turn=8,
        )
    )
    assert has_resolution_confirmation(case) is True
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == dual_use.evidence_id


def test_hypothesis_axis_refutes_link_also_disqualifies():
    """A failed-fix row the model linked only to the HYPOTHESIS of the
    engine-marked cause (never to a node) is still a disconfirmation — the
    scoped disqualification covers both belief axes."""
    from faultmaven.modules.case.contracts import HypothesisEvidenceLink

    case, target, _ = _incident_case()
    _engine_marker(target, case, turn=5)
    fail_row = _absence_row(
        "hyp_linked_fail", "Applied the fix but the problem persisted", turn=5
    )
    case.evidence.append(fail_row)
    hyp = next(iter(case.hypotheses.values()))  # attached to the target root
    hyp.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=hyp.hypothesis_id,
            evidence_id=fail_row.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="fix did not resolve",
            stance_confidence=0.9,
        )
    )
    assert has_resolution_confirmation(case) is False


def test_fresh_confirmation_after_a_failed_fix_qualifies():
    """A new confirmation row NEWER than the engine-known disconfirmation is
    the legitimate second-attempt success — it qualifies and stamps."""
    case, target, _ = _incident_case()
    m6_row = _absence_row(
        "m6_fail_2",
        "Counterfactual disconfirmation (M6): first fix did not help.",
        turn=5,
        collected_by="engine",
    )
    fresh = _absence_row(
        "fresh",
        "resolv.conf trimmed; CrashLoopBackOff gone on all payments pods",
        turn=6,
    )
    case.evidence.extend([m6_row, fresh])
    assert [r.evidence_id for r in resolution_confirmation_rows(case)] == [
        fresh.evidence_id
    ]
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == fresh.evidence_id
    # Readiness re-read AFTER the stamp: the now-SUPPORTS-linked row must
    # still qualify (only REFUTES links and engine authorship disqualify).
    assert has_resolution_confirmation(case) is True


def test_same_turn_disconfirm_and_confirm_qualifies():
    """NO-COLLAPSE pin (review F1): the mixed single-turn shape — 'the restart
    didn't fix it, but correcting resolv.conf did' — stamps the M6 engine row
    AND the legitimate confirmation at the SAME turn; turn granularity cannot
    order within-turn events, so the confirmation must qualify (>=, not >)."""
    case, _, _ = _incident_case()
    m6_row = _absence_row(
        "m6_same_turn",
        "Counterfactual disconfirmation (M6): restart did not help.",
        turn=7,
        collected_by="engine",
    )
    confirm = _absence_row(
        "confirm_same_turn",
        "resolv.conf corrected; pods stable, CrashLoopBackOff gone",
        turn=7,
    )
    case.evidence.extend([m6_row, confirm])
    assert has_resolution_confirmation(case) is True


def test_late_sibling_exclusion_note_does_not_mask_confirmation():
    """NO-COLLAPSE pin (review F2): a proof-by-exclusion absence-REFUTES on a
    SIBLING recorded AFTER the legitimate confirmation ('FYI we'd ruled out
    the eviction theory — reverting changed nothing') must not retroactively
    regress READY — the window is keyed on ENGINE-authored rows, and a
    sibling-scoped REFUTES link is not a disconfirmation. At the STAMP the
    newer exclusion note is bearing-vetoed (it talks about the sibling), so
    the actual confirmation is still the citation."""
    case, target, sibling = _incident_case()
    confirm = _absence_row(
        "confirmed_first",
        "resolv.conf corrected; CrashLoopBackOff no longer observed",
        turn=8,
    )
    exclusion = _absence_row(
        "late_exclusion",
        "Reverting the eviction tuning changed nothing — memory pressure "
        "theory ruled out",
        turn=9,
    )
    case.evidence.extend([confirm, exclusion])
    sibling.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=exclusion.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="counterfactual exclusion of the sibling",
            linked_at_turn=9,
        )
    )
    qualified = {r.evidence_id for r in resolution_confirmation_rows(case)}
    assert confirm.evidence_id in qualified
    assert has_resolution_confirmation(case) is True
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == confirm.evidence_id


def test_stamp_refuses_row_at_or_before_a_target_root_refutation():
    """Per-root refutation window (reviewed): the CONFIRMED grade is never
    minted from a row at-or-before a refutation recorded against the very
    root being confirmed — a HEDGED self-claimed failed fix does not demote
    the root (§7.2), but it marks its fix window for the mint."""
    case, target, _ = _incident_case()
    hedged_fail = _absence_row(
        "hedged_fail_root", "Not sure the fix helped, errors may remain", turn=8
    )
    case.evidence.append(hedged_fail)
    target.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=hedged_fail.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="uncertain failed fix",
            linked_at_turn=8,
            stance_confidence=0.4,
        )
    )
    same_turn_confirm = _absence_row(
        "same_turn_confirm",
        "resolv.conf corrected; CrashLoopBackOff gone",
        turn=8,
    )
    case.evidence.append(same_turn_confirm)
    # Gate liveness untouched (the user can still resolve)...
    assert has_resolution_confirmation(case) is True
    # ...but the top-grade mint holds: no candidate is NEWER than the refute.
    assert confirm_root_from_resolution_absence(case) is False
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC


def test_stamp_accepts_confirmation_newer_than_the_root_refutation():
    """The strictly-newer confirmation after a hedged failed fix on the same
    root IS the legitimate second observation — the handshake plus a fresh
    post-refute row completes the grade."""
    case, target, _ = _incident_case()
    hedged_fail = _absence_row(
        "hedged_fail_root2", "Not sure the fix helped, errors may remain", turn=8
    )
    case.evidence.append(hedged_fail)
    target.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=hedged_fail.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="uncertain failed fix",
            linked_at_turn=8,
            stance_confidence=0.4,
        )
    )
    fresh = _absence_row(
        "fresh_after_refute",
        "resolv.conf corrected; CrashLoopBackOff gone on all pods",
        turn=10,
    )
    case.evidence.append(fresh)
    assert confirm_root_from_resolution_absence(case) is True
    assert _stamped_row_id(case, target) == fresh.evidence_id
    assert grade_cause_assurance(case) == CauseAssuranceGrade.CONFIRMED


# ---------------------------------------------------------------------------
# Structural pins
# ---------------------------------------------------------------------------


def test_chain_descendants_terminate_on_cyclic_graph():
    """The mechanism walk terminates on a malformed cyclic graph (docstring
    claim, pinned) and still returns the reachable rungs."""
    from faultmaven.core.investigation.cause_assurance import _chain_descendant_ids

    case, target, _ = _incident_case()
    case.causal_edges.append(
        CausalEdge(cause_node_id="cn_0000000000ab", effect_node_id=target.node_id)
    )  # rung -> root back-edge = cycle
    descendants = _chain_descendant_ids(case, target.node_id)
    assert "cn_0000000000ab" in descendants
    assert target.node_id not in descendants


def test_finalize_surface_tolerates_bearing_refusal():
    """The shared finalizer (chat executor + API close_case) survives a
    bearing refusal: no stamp, no exception, grade stays honest at
    MECHANISTIC (a validated root stands), gate metadata bar still satisfied."""
    from faultmaven.core.investigation.terminal_transitions import (
        finalize_resolution_truth_surface,
    )

    case, target, _ = _incident_case()
    case.evidence.append(
        _absence_row(
            "sibling_row_3",
            "Memory pressure on the worker node returned to normal",
        )
    )
    assert finalize_resolution_truth_surface(case) is False
    assert case.progress.cause_assurance == CauseAssuranceGrade.MECHANISTIC
    assert has_resolution_confirmation(case) is True
