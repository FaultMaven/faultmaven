"""§7.8 / INV-36 (#656): hypothesis dedup on ``hypotheses_to_add``.

An LLM-emitted hypothesis whose statement duplicates a standing (non-terminal)
or same-batch hypothesis is not minted a second time. Duplicates spuriously
re-satisfy the ≥2-active work gate — the axis that separates
``INSUFFICIENT_EVIDENCE`` from ``NOT_YET_PRODUCTIVE`` — so a duplicate must never
inflate ``len(case.hypotheses)`` (observed live: an identical DNS hypothesis
minted twice, turns 10/11).

The dedup predicate (``hypothesis_statements_duplicate``) is STRICTER than the
§7.1.2 fold and FAILS OPEN because a dedup DROPS an LLM emission: a mutual mirror
at 0.8 (not the fold's 0.6), symmetric (not containment) so a more-SPECIFIC
elaboration survives, a negation-polarity guard so a dispute is never a
duplicate, a numeric-discriminator guard so "server 1" vs "server 2" stay
distinct, and standing-cause-only so a refuted/retired revival can re-enter.

Positional integrity: a skip records the CANONICAL existing id in
``hyp_emit_order`` (not ``hypotheses_generated``) so downstream ``new_index_N``
refs resolve to the kept hypothesis rather than shifting onto the wrong sibling,
while telemetry / turn-outcome progress do not count a dedup as generation.
"""

from unittest.mock import patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation import milestone_engine
from faultmaven.core.investigation.causal_graph import (
    find_duplicate_hypothesis,
    hypothesis_statements_duplicate,
)
from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import (
    HypothesisToAdd,
    HypothesisUpdate,
    InvestigationResponse_Diagnosis,
)
from faultmaven.core.investigation.verification_status import work_gate_passed
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    HypothesisCategory,
    HypothesisState,
    InquiryData,
    ProblemVerification,
)

pytestmark = pytest.mark.unit

_DSU = InvestigationResponse_Diagnosis.DiagnosisStateUpdate


# --------------------------------------------------------------------------
# Pure predicate calibration — the dedup ("duplicate statement") bar. The bar
# is STRICTER than the §7.1.2 fold and FAILS OPEN because a dedup DROPS an LLM
# emission for the turn.
# --------------------------------------------------------------------------


def test_predicate_exact_restatement_is_duplicate():
    s = "DNS resolution failing because resolv.conf lists too many nameservers"
    assert hypothesis_statements_duplicate(s, s) is True


def test_predicate_reordered_synonymless_restatement_is_duplicate():
    # Same content tokens, reordered — a mutual mirror at 1.0.
    a = "connection pool exhausted under load"
    b = "under load the connection pool exhausted"
    assert hypothesis_statements_duplicate(a, b) is True


def test_predicate_elaboration_is_distinct_not_duplicate():
    """The load-bearing calibration: a more-SPECIFIC elaboration of a standing
    hypothesis is a real refinement of the differential, NOT a duplicate. It
    scores high on one-way containment but below the mutual-mirror Jaccard bar,
    so it survives (never silently dropped)."""
    general = "DNS resolution failing"
    specific = (
        "DNS resolution failing because resolv.conf lists more than three "
        "nameservers so glibc silently truncates the list and the authoritative "
        "server is never queried"
    )
    assert hypothesis_statements_duplicate(general, specific) is False


def test_predicate_short_distinct_siblings_survive():
    """A DROP action must fail open: two genuinely-distinct short causes that
    differ by ONE substantive token (Jaccard 0.6, below the 0.8 dedup bar)
    survive as separate hypotheses. The looser §7.1.2 fold bar (0.6) would have
    wrongly collapsed them."""
    assert (
        hypothesis_statements_duplicate(
            "memory leak in connection pool", "memory leak in cache pool"
        )
        is False
    )


def test_predicate_negation_is_distinct():
    """A disputing hypothesis is never a duplicate of the claim it contradicts —
    even though 'not' is a stopword that makes the two tokenize identically. The
    polarity guard fails open."""
    assert (
        hypothesis_statements_duplicate(
            "connection pool is exhausted", "connection pool is not exhausted"
        )
        is False
    )


def test_predicate_numeric_discriminator_is_distinct():
    """Two hypotheses distinguished ONLY by a number the similarity tokenizer
    drops (single digits, or the stopword 'version') must stay distinct — the
    numeric-discriminator guard fails open."""
    assert (
        hypothesis_statements_duplicate("server 1 is down", "server 2 is down") is False
    )
    assert (
        hypothesis_statements_duplicate(
            "database version 5 is buggy", "database version 6 is buggy"
        )
        is False
    )
    # Same number → the guard does not block a genuine restatement.
    assert (
        hypothesis_statements_duplicate("server 1 is down", "down is server 1") is True
    )


def test_predicate_different_causes_are_distinct():
    assert (
        hypothesis_statements_duplicate(
            "DNS resolution failing intermittently",
            "database connection pool exhausted under peak load",
        )
        is False
    )


def test_predicate_empty_statements_never_match():
    assert hypothesis_statements_duplicate("", "") is False
    assert hypothesis_statements_duplicate("DNS failing", "") is False


# --------------------------------------------------------------------------
# find_duplicate_hypothesis — standing (non-terminal) hypotheses
# --------------------------------------------------------------------------


def _case(symptom_verified: bool = True) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="orders failing",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="orders failing", severity=CaseSeverity.HIGH
        ),
    )
    case.current_turn = 5
    case.progress.symptom_verified = symptom_verified
    return case


def _add_hyp(case: Case, statement: str, category, state=HypothesisState.ACTIVE):
    mgr = HypothesisManager()
    h = mgr.create_hypothesis(
        statement=statement,
        category=category,
        initial_likelihood=0.4,
        current_turn=case.current_turn,
        state=state,
    )
    case.hypotheses[h.hypothesis_id] = h
    return h


def test_find_duplicate_matches_existing_active():
    case = _case()
    h = _add_hyp(
        case, "connection pool exhausted under load", HypothesisCategory.DATABASE
    )
    assert (
        find_duplicate_hypothesis("under load the connection pool exhausted", case)
        == h.hypothesis_id
    )


def test_find_duplicate_ignores_terminal_hypothesis():
    """A REFUTED/RETIRED cause is NOT a dedup target: those states are
    terminal-immutable and the update path instructs the LLM to open a NEW
    hypothesis to revive the theory — so deduping against them would deadlock the
    revival. A restatement of a retired cause is allowed to re-mint."""
    case = _case()
    _add_hyp(
        case,
        "connection pool exhausted under load",
        HypothesisCategory.DATABASE,
        state=HypothesisState.RETIRED,
    )
    assert (
        find_duplicate_hypothesis("under load the connection pool exhausted", case)
        is None
    )


def test_find_duplicate_returns_none_for_distinct():
    case = _case()
    _add_hyp(case, "connection pool exhausted under load", HypothesisCategory.DATABASE)
    assert find_duplicate_hypothesis("upstream DNS server unreachable", case) is None


# --------------------------------------------------------------------------
# Apply path — the wired behavior through _apply_investigation_updates
# --------------------------------------------------------------------------


def _engine() -> MilestoneEngine:
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.hypothesis_manager = HypothesisManager()
    return eng


def _meta() -> dict:
    return {
        "milestones_completed": [],
        "evidence_added": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "progress_made": False,
        "status_transitioned": False,
    }


def _h2a(statement, category=HypothesisCategory.DATABASE, likelihood=0.4):
    return HypothesisToAdd(
        statement=statement, category=category, likelihood=likelihood, rationale="r"
    )


async def test_duplicate_of_standing_hypothesis_not_minted():
    eng, case = _engine(), _case()
    existing = _add_hyp(
        case, "connection pool exhausted under load", HypothesisCategory.DATABASE
    )
    meta = _meta()
    # The counter is a no-op unless ENABLE_METRICS — assert the fire via a mock.
    with patch.object(milestone_engine, "hypothesis_dedup_skipped_total") as counter:
        await eng._apply_investigation_updates(
            case,
            _DSU(hypotheses_to_add=[_h2a("under load the connection pool exhausted")]),
            meta,
        )
        counter.inc.assert_called_once()
    # No second record minted; the gate axis is protected.
    assert len(case.hypotheses) == 1
    assert existing.hypothesis_id in case.hypotheses
    # Not counted as generation (a dedup is not diagnostic progress).
    assert meta["hypotheses_generated"] == []
    # Feedback names the standing id so the LLM can update it instead.
    assert existing.hypothesis_id in meta["system_feedback"]


async def test_intra_batch_duplicate_minted_once():
    eng, case = _engine(), _case()
    meta = _meta()
    await eng._apply_investigation_updates(
        case,
        _DSU(
            hypotheses_to_add=[
                _h2a("connection pool exhausted under load"),
                _h2a("under load the connection pool exhausted"),
            ]
        ),
        meta,
    )
    assert len(case.hypotheses) == 1
    assert len(meta["hypotheses_generated"]) == 1


async def test_elaboration_in_same_batch_survives_as_distinct():
    """Two genuinely distinct hypotheses (a general one and a specific
    elaboration) both mint — the dedup must not collapse a real refinement."""
    eng, case = _engine(), _case()
    meta = _meta()
    await eng._apply_investigation_updates(
        case,
        _DSU(
            hypotheses_to_add=[
                _h2a("DNS resolution failing"),
                _h2a(
                    "DNS resolution failing because resolv.conf lists more than "
                    "three nameservers so glibc silently truncates the list and "
                    "the authoritative server is never queried"
                ),
            ]
        ),
        meta,
    )
    assert len(case.hypotheses) == 2


async def test_duplicate_across_categories_does_not_inflate_work_gate():
    """A duplicate cause re-emitted under a SECOND category would otherwise buy
    both a hypothesis count AND a category count — exactly the work-gate
    corruption vector. Dedup ignores category, so the gate stays honest."""
    eng, case = _engine(), _case()
    meta = _meta()
    await eng._apply_investigation_updates(
        case,
        _DSU(
            hypotheses_to_add=[
                _h2a(
                    "connection pool exhausted under load", HypothesisCategory.DATABASE
                ),
                _h2a(
                    "under load the connection pool exhausted",
                    HypothesisCategory.NETWORK,
                ),
            ]
        ),
        meta,
    )
    assert len(case.hypotheses) == 1
    # Only one hypothesis, one category — the ≥2 work gate is NOT spuriously met.
    assert work_gate_passed(case) is False


async def test_positional_integrity_new_index_resolves_past_skipped_dup():
    """A dedup skip at emitted index 0 must not shift ``new_index_1`` onto the
    wrong hypothesis: ``hyp_emit_order`` records the canonical id at position 0
    and the newly-minted hyp at position 1, and a same-turn ``new_index_1``
    update lands on the NEW hypothesis (not the standing duplicate)."""
    eng, case = _engine(), _case()
    existing = _add_hyp(
        case, "connection pool exhausted under load", HypothesisCategory.DATABASE
    )
    meta = _meta()
    await eng._apply_investigation_updates(
        case,
        _DSU(
            hypotheses_to_add=[
                _h2a("under load the connection pool exhausted"),  # index 0 → dup
                _h2a(
                    "upstream DNS server unreachable", HypothesisCategory.NETWORK
                ),  # index 1 → new
            ],
            # Reference the SECOND emitted hypothesis by position. REFUTED is
            # applied synchronously (unlike deferred RETIRED/likelihood), so it
            # is a clean observable for which hypothesis the ref resolved to.
            hypotheses_to_update=[
                HypothesisUpdate(
                    hypothesis_id="new_index_1",
                    state=HypothesisState.REFUTED,
                    refutation_reason="ruled out by DNS trace",
                )
            ],
        ),
        meta,
    )
    (new_id,) = [hid for hid in case.hypotheses if hid != existing.hypothesis_id]
    # Positional list preserved: canonical dup id, then the new hyp.
    assert meta["hyp_emit_order"] == [existing.hypothesis_id, new_id]
    # The new_index_1 update reached the NEW hypothesis, not the standing dup.
    assert case.hypotheses[new_id].state == HypothesisState.REFUTED
    assert case.hypotheses[existing.hypothesis_id].state == HypothesisState.ACTIVE


async def test_revival_of_refuted_cause_is_minted():
    """A restatement of a REFUTED cause is NOT deduped — it mints a fresh
    hypothesis so a revived theory can re-enter the differential (the update path
    refuses terminal-state changes and instructs exactly this)."""
    eng, case = _engine(), _case()
    # Build a REFUTED hypothesis directly (create_hypothesis rejects REFUTED
    # without a refutation_reason).
    mgr = HypothesisManager()
    refuted = mgr.create_hypothesis(
        statement="connection pool exhausted under load",
        category=HypothesisCategory.DATABASE,
        initial_likelihood=0.2,
        current_turn=case.current_turn,
        state=HypothesisState.ACTIVE,
    )
    refuted.state = HypothesisState.REFUTED
    refuted.refutation_reason = "pool metrics were flat"
    case.hypotheses[refuted.hypothesis_id] = refuted

    meta = _meta()
    await eng._apply_investigation_updates(
        case,
        _DSU(hypotheses_to_add=[_h2a("under load the connection pool exhausted")]),
        meta,
    )
    # A new record was minted (not deduped onto the terminal one).
    assert len(case.hypotheses) == 2
    assert len(meta["hypotheses_generated"]) == 1


async def test_dedup_does_not_reroot_canonical():
    """A deduped item's ``root_node_ref`` must NOT be recorded against the
    canonical hypothesis: the re-root pass has no anti-clobber guard, so re-rooting
    a (possibly validated) canonical onto the duplicate's fresh chain would GC its
    existing chain. The duplicate's chain is left to ``resolve_orphan_chains``,
    which re-attaches only FLAT hypotheses under its own guard."""
    eng, case = _engine(), _case()
    existing = _add_hyp(
        case, "connection pool exhausted under load", HypothesisCategory.DATABASE
    )
    meta = _meta()
    await eng._apply_investigation_updates(
        case,
        _DSU(
            hypotheses_to_add=[
                HypothesisToAdd(
                    statement="under load the connection pool exhausted",
                    category=HypothesisCategory.DATABASE,
                    likelihood=0.4,
                    rationale="r",
                    root_node_ref="cn_poolroot",
                )
            ]
        ),
        meta,
    )
    assert len(case.hypotheses) == 1  # deduped
    # The canonical is NOT re-rooted by the dedup (no clobber path).
    assert existing.hypothesis_id not in meta.get("hyp_root_refs", {})
