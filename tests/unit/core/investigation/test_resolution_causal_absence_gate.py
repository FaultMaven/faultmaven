"""RESOLVED requires causal-absence (cause eliminated), not just a solution row.

A case where service was only STABILIZED (failover / workaround / traffic-shift
— which produce symptom_absence while the cause persists), or where the
permanent fix is deferred, must CLOSE with the findings documented, not RESOLVE.
The discriminator is a ``causal_absence_evidence`` row (the cause is confirmed
eliminated). See investigation-flow-redesign.md §11 / intent-resolution.md §8.

Motivated by the pg-primary-hw-failover scenario: failover restored writes
(symptom_absence) but the NVMe was still dead (no causal_absence) — yet the case
RESOLVED under the old ``has_solution``-on-any-row gate.
"""

from types import SimpleNamespace

from faultmaven.core.investigation.terminal_transitions import (
    ClosureReadiness,
    ResolutionReadiness,
    assess_closure_readiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import EvidenceCategory, EvidenceStance


def _case(*, cats=(), solutions=1, cause=True):
    return SimpleNamespace(
        problem_verification=SimpleNamespace(symptom_statement="writes failing"),
        root_cause_conclusion=(
            SimpleNamespace(root_cause="NVMe hardware failure") if cause else None
        ),
        working_conclusion=None,
        solutions=[SimpleNamespace(title="failover") for _ in range(solutions)],
        evidence=[SimpleNamespace(category=c) for c in cats],
        hypotheses={},
        progress=SimpleNamespace(completed_milestones=[]),
    )


class TestResolutionGate:
    def test_ready_only_when_causal_absence(self):
        r = assess_resolution_readiness(
            _case(cats=[EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE])
        )
        assert r.verdict == ResolutionReadiness.READY

    def test_stabilized_symptom_absence_only_is_not_ready(self):
        # The pg-failover shape: symptom restored, cause persists.
        r = assess_resolution_readiness(
            _case(
                cats=[
                    EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
                    EvidenceCategory.CAUSAL_EVIDENCE,
                ]
            )
        )
        assert r.verdict == ResolutionReadiness.NEEDS_INFO
        # asks the user to fill the documentation gap (confirmation it's
        # resolved) and offers Close as the fallback for a stabilized case
        assert "confirmation the problem is now resolved" in r.missing
        assert "close" in r.message.lower()

    def test_solution_row_without_absence_is_not_ready(self):
        # A solution on record but no causal_absence must NOT resolve (the old
        # has_solution gate would have said READY here).
        r = assess_resolution_readiness(
            _case(cats=[EvidenceCategory.CAUSAL_EVIDENCE], solutions=1)
        )
        assert r.verdict == ResolutionReadiness.NEEDS_INFO

    def test_thin_case_suggests_close(self):
        r = assess_resolution_readiness(_case(cats=[], solutions=0, cause=False))
        assert r.verdict == ResolutionReadiness.SUGGEST_CLOSE

    def test_substance_without_solution_still_asks(self):
        # Cause + evidence but no solution and no causal_absence -> ask (not close).
        r = assess_resolution_readiness(
            _case(cats=[EvidenceCategory.CAUSAL_EVIDENCE], solutions=0, cause=True)
        )
        assert r.verdict == ResolutionReadiness.NEEDS_INFO


class TestClosureSuggestResolveSymmetry:
    def test_suggest_resolve_only_with_causal_absence(self):
        # close request on a case with cause + solution + causal_absence -> pivot
        r = assess_closure_readiness(
            _case(
                cats=[
                    EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
                    EvidenceCategory.CAUSAL_EVIDENCE,
                ]
            )
        )
        assert r.verdict == ClosureReadiness.SUGGEST_RESOLVE

    def test_stabilized_case_does_not_pivot_to_resolve(self):
        # cause + solution but only symptom_absence -> closing is correct, no pivot
        r = assess_closure_readiness(
            _case(
                cats=[
                    EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
                    EvidenceCategory.CAUSAL_EVIDENCE,
                ]
            )
        )
        assert r.verdict != ClosureReadiness.SUGGEST_RESOLVE


class TestCauseStateAuthoritative:
    """The gate must read the authoritative engine-derived ``cause_state``
    (CauseState.IDENTIFIED), not only the ``root_cause_conclusion`` /
    ``working_conclusion`` proxies. The LLM may leave the documented conclusion
    empty even when the cause IS identified — the k8s-pvc gate failure: with
    cause_state=IDENTIFIED + causal_absence + a solution, the empty
    root_cause_conclusion made the gate read 'no root cause' and stall.
    """

    @staticmethod
    def _identified_case(cats):
        from faultmaven.modules.case.contracts import CauseState

        return SimpleNamespace(
            problem_verification=SimpleNamespace(symptom_statement="pvc pending"),
            root_cause_conclusion=None,  # LLM left the documented conclusion empty
            working_conclusion=SimpleNamespace(statement="x", likelihood=0.167),
            solutions=[SimpleNamespace(title="fix storageclass")],
            evidence=[SimpleNamespace(category=c) for c in cats],
            hypotheses={},
            progress=SimpleNamespace(
                completed_milestones=[], cause_state=CauseState.IDENTIFIED
            ),
        )

    def test_identified_with_causal_absence_is_ready(self):
        # k8s-pvc shape: cause known via cause_state, causal_absence recorded,
        # solution on record, but root_cause_conclusion empty -> READY.
        r = assess_resolution_readiness(
            self._identified_case([EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE])
        )
        assert r.verdict == ResolutionReadiness.READY

    def test_identified_without_absence_asks_only_confirmation(self):
        # cause known + solution but no causal_absence -> NEEDS_INFO, and the
        # ONLY missing essential is the confirmation (root cause is NOT missing).
        r = assess_resolution_readiness(
            self._identified_case([EvidenceCategory.CAUSAL_EVIDENCE])
        )
        assert r.verdict == ResolutionReadiness.NEEDS_INFO
        assert "root cause" not in r.missing
        assert "confirmation the problem is now resolved" in r.missing

    def test_closure_suggest_resolve_uses_cause_state(self):
        # close request on a resolution-grade case (cause_state IDENTIFIED +
        # solution + causal_absence) pivots to resolve even with empty
        # root_cause_conclusion.
        r = assess_closure_readiness(
            self._identified_case([EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE])
        )
        assert r.verdict == ClosureReadiness.SUGGEST_RESOLVE


class TestProposedTransitionCaseNormalization:
    """LLMs emit inconsistent case ('RESOLVED' vs 'resolved'); the engine's
    edge check compares against lowercase CaseState values, so to_state must be
    normalized at parse time or a valid resolve proposal is spuriously rejected.
    """

    def test_uppercase_to_state_normalized(self):
        from faultmaven.core.investigation.schemas import ProposedTransition

        assert ProposedTransition(to_state="RESOLVED").to_state == "resolved"
        assert ProposedTransition(to_state=" Closed ").to_state == "closed"


class TestCausalAbsenceIsSufficient:
    """causal_absence alone is the resolution bar. Requiring a separate
    SolutionToAdd record on top blocked the out-of-band path: the user reports a
    verbal fix -> agent records causal_absence (user_description) but no solution
    record -> gate said missing=['solution'] -> stuck-loop -> wrongly CLOSED
    (case_e5f5849b9e4d, the rate-limit out-of-band scenario).
    """

    def test_causal_absence_without_any_solution_record_is_ready(self):
        # Out-of-band: causal_absence recorded, cause known, but NO solution row.
        r = assess_resolution_readiness(
            _case(cats=[EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE], solutions=0)
        )
        assert r.verdict == ResolutionReadiness.READY

    def test_no_absence_still_asks_for_essentials(self):
        # Without causal_absence the documentation-gap-fill ask still fires.
        r = assess_resolution_readiness(
            _case(cats=[EvidenceCategory.CAUSAL_EVIDENCE], solutions=0)
        )
        assert r.verdict == ResolutionReadiness.NEEDS_INFO
        assert "confirmation the problem is now resolved" in r.missing


class TestClosurePivotMatchesResolutionBar:
    """SUGGEST_RESOLVE (close-request pivot) must use the SAME bar as
    assess_resolution_readiness READY: causal_absence alone. Otherwise a close
    request on an out-of-band case (causal_absence, no solution record) wrongly
    closes while a resolve request on the same case resolves — the asymmetry the
    'resolved is a safe special case of closed' rule forbids.
    """

    def test_close_pivots_to_resolve_with_causal_absence_no_solution(self):
        r = assess_closure_readiness(
            _case(cats=[EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE], solutions=0)
        )
        assert r.verdict == ClosureReadiness.SUGGEST_RESOLVE

    def test_close_does_not_pivot_without_causal_absence(self):
        # stabilized (symptom_absence, no causal_absence) -> close is correct
        r = assess_closure_readiness(
            _case(cats=[EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE], solutions=1)
        )
        assert r.verdict != ClosureReadiness.SUGGEST_RESOLVE


class TestConfirmationRowQualification:
    """INV-30 gate side (#656): the READY bar and the close→resolve pivot
    count only QUALIFYING absence rows (``resolution_confirmation_rows``) —
    non-engine-authored and newer than the latest failed-fix disconfirmation.
    Regression: before the shared predicate, the ENGINE's own M6 failed-fix
    DISCONFIRMATION row satisfied "confirmation the problem is now resolved",
    so a case whose fix had just FAILED read resolution-READY.
    """

    @staticmethod
    def _absence(*, collected_by="llm", turn=6, evidence_id="ev_a"):
        return SimpleNamespace(
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            collected_by=collected_by,
            collected_at_turn=turn,
            evidence_id=evidence_id,
        )

    def test_engine_m6_disconfirmation_row_is_not_ready(self):
        case = _case(cats=[])
        case.evidence = [self._absence(collected_by="engine")]
        r = assess_resolution_readiness(case)
        assert r.verdict == ResolutionReadiness.NEEDS_INFO
        assert "confirmation the problem is now resolved" in r.missing

    def test_engine_m6_disconfirmation_row_does_not_pivot_close(self):
        case = _case(cats=[])
        case.evidence = [self._absence(collected_by="engine")]
        r = assess_closure_readiness(case)
        assert r.verdict != ClosureReadiness.SUGGEST_RESOLVE

    @staticmethod
    def _with_failed_fix_window(premature_turn, fresh_turn=None):
        """A premature absence row, an ENGINE-known failed-fix disconfirmation
        (M6 row at turn 5, REFUTES-linked as minted), and optionally a fresh
        post-failure row."""
        case = _case(cats=[])
        disconfirm = SimpleNamespace(
            category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            collected_by="engine",
            collected_at_turn=5,
            evidence_id="ev_failed",
        )
        rows = [
            TestConfirmationRowQualification._absence(
                turn=premature_turn, evidence_id="ev_premature"
            ),
            disconfirm,
        ]
        if fresh_turn is not None:
            rows.append(
                TestConfirmationRowQualification._absence(
                    turn=fresh_turn, evidence_id="ev_fresh"
                )
            )
        case.evidence = rows
        case.causal_nodes = {
            "cn_1": SimpleNamespace(
                evidence_links=[
                    SimpleNamespace(
                        stance=EvidenceStance.REFUTES, evidence_id="ev_failed"
                    )
                ]
            )
        }
        return case

    def test_premature_row_from_failed_fix_window_is_not_ready(self):
        case = self._with_failed_fix_window(premature_turn=4)
        r = assess_resolution_readiness(case)
        assert r.verdict == ResolutionReadiness.NEEDS_INFO

    def test_fresh_confirmation_after_failed_fix_is_ready(self):
        case = self._with_failed_fix_window(premature_turn=4, fresh_turn=6)
        r = assess_resolution_readiness(case)
        assert r.verdict == ResolutionReadiness.READY

    def test_same_turn_confirmation_as_the_failure_is_ready(self):
        # The mixed "first fix failed, second fix worked" single turn: the
        # confirmation lands at the SAME turn as the M6 row and must qualify.
        case = self._with_failed_fix_window(premature_turn=4, fresh_turn=5)
        r = assess_resolution_readiness(case)
        assert r.verdict == ResolutionReadiness.READY
