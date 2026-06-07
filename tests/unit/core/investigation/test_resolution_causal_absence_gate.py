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
