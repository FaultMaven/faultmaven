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
from faultmaven.modules.case.contracts import EvidenceCategory


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
