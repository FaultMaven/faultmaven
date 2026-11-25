"""One computation of the case outcome builds its shared state once (#2657).

``compute_case_outcome`` runs in one scope (``_outcome_computation``) in
which ``subject_index``, ``fix_windows`` and ``_cause_pool`` return their
first build for the case. Before it, one computation over a failed fix
rebuilt them 13, 8 and 22 times. The scope resets in ``finally``, and a
decider outside it builds as it always did. The memo is sound only while
the computation leaves the case as it found it, which a fingerprint pins.
``_mitigation_verify_refusal`` reads the mitigation's relief once, and none
on a branch that needs none. Builds are counted, never timed. No live LLM.
"""

import contextlib
import sys
from collections import Counter

import pytest

from faultmaven.core.investigation import cause_assurance as ca
from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _mitigation_verify_refusal,
)
from faultmaven.core.investigation.outcome import compute_case_outcome
from faultmaven.modules.case.contracts import (
    EvidenceCategory,
    MitigationRefusalReason,
    ProblemStatus,
    WritePoint,
)
from tests.unit.core.investigation.bound_rows import VERIFYING_ID, _row
from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
    _bare_case,
    _failed_fix,
    _fallen_case,
    _interception_only,
    _invalidated,
    _mitigated,
    _relief_only,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _eid,
    _identified_case,
)
from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
    _held_case,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _confirmed_case,
)

pytestmark = pytest.mark.unit

_MEMOIZED = {
    ca.subject_index.__wrapped__.__code__: "subject_index",
    ca.fix_windows.__wrapped__.__code__: "fix_windows",
    ca._cause_pool.__wrapped__.__code__: "_cause_pool",
}


@contextlib.contextmanager
def _builds():
    """Count each memoized derivation's builds (calls of its own body)."""
    counted: Counter = Counter()
    previous = sys.getprofile()

    def profile(frame, event, _arg):
        if event == "call" and frame.f_code in _MEMOIZED:
            counted[_MEMOIZED[frame.f_code]] += 1

    sys.setprofile(profile)
    try:
        yield counted
    finally:
        sys.setprofile(previous)


def _compute(case):
    return compute_case_outcome(None, case, at=WritePoint.CYCLE)


def test_one_computation_builds_each_shared_derivation_once(monkeypatch):
    case = _failed_fix()
    with _builds() as counted:
        record = _compute(case)
    assert counted == {"subject_index": 1, "fix_windows": 1, "_cause_pool": 1}

    # The premise: without the scope the same computation rebuilds each one
    # (13, 8 and 22 times on ``main``), and gives the same record.
    monkeypatch.setattr(outcome_module, "_outcome_computation", contextlib.nullcontext)
    with _builds() as unscoped:
        assert _compute(case) == record
    assert all(unscoped[name] > 1 for name in _MEMOIZED.values())


def test_a_computation_that_raises_leaves_no_memo(monkeypatch):
    case = _failed_fix()

    def boom(_case):
        raise RuntimeError("after the index was built")

    monkeypatch.setattr(outcome_module, "shown_milestones", boom)
    with pytest.raises(RuntimeError):
        _compute(case)
    assert ca._COMPUTATION.get() is None
    with _builds() as counted:
        ca.subject_index(case)
        ca.subject_index(case)
    assert counted["subject_index"] == 2


def test_a_mutation_after_a_computation_is_seen_by_the_next(monkeypatch):
    case = _failed_fix()
    first = _compute(case)
    _row(
        case,
        EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        turn=10,
        evidence_id=_eid("gone_later"),
        summary="max_connections still reads 100",
        rechecks=_eid("pool_log"),
    )
    second = _compute(case)
    assert second != first
    monkeypatch.setattr(outcome_module, "_outcome_computation", contextlib.nullcontext)
    assert _compute(case) == second


def test_only_the_scopes_own_index_reads_the_memoized_windows():
    case = _failed_fix()
    with _builds() as counted, ca._outcome_computation():
        own = ca.subject_index(case)
        assert ca.fix_windows(case, own) == ca.fix_windows(case)
        assert counted["fix_windows"] == 1
        foreign = ca.subject_index.__wrapped__(case)
        assert ca.fix_windows(case, foreign) == ca.fix_windows(case)
        assert counted["fix_windows"] == 2
        assert ca.subject_index(case) is own


@pytest.mark.parametrize(
    "build",
    [
        _failed_fix,
        lambda: _identified_case()[0],
        _confirmed_case,
        _held_case,
        _bare_case,
        lambda: _fallen_case()[0],
        _invalidated,
        _interception_only,
        _relief_only,
        _mitigated,
        lambda: _mitigated(relieved=True),
    ],
)
def test_a_computation_leaves_the_case_as_it_found_it(build):
    """The memo's premise: nothing the computation calls mutates the case."""
    case = build()
    before = case.model_dump(mode="json")
    _compute(case)
    assert case.model_dump(mode="json") == before


def _spy_relief(monkeypatch) -> list:
    reads = []
    real = ca.mitigation_relief

    def spy(*args, **kwargs):
        reads.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(ca, "mitigation_relief", spy)
    return reads


def test_the_records_mitigation_section_reads_its_relief_once(monkeypatch):
    case = _mitigated()
    reads = _spy_relief(monkeypatch)
    mitigation = outcome_module._mitigation(case, ca.subject_index(case))
    assert mitigation.refusal_reason == MitigationRefusalReason.NO_BOUND_RELIEF
    assert len(reads) == 1


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (lambda: _mitigated(dated=False), MitigationRefusalReason.ATTEMPT_UNDATED),
        (
            lambda: _mitigated(ProblemStatus.REVISION_PENDING),
            MitigationRefusalReason.REVISION_PENDING,
        ),
        (
            lambda: _mitigated(ProblemStatus.UNVERIFIED),
            MitigationRefusalReason.PROBLEM_UNVERIFIED,
        ),
    ],
)
def test_an_early_refusal_builds_no_index(monkeypatch, build, reason):
    case = build()
    reads = _spy_relief(monkeypatch)
    with _builds() as counted:
        refusal = _mitigation_verify_refusal(case)
    assert refusal["reason"] == reason
    assert counted["subject_index"] == 0 and reads == []


def test_the_mitigation_refusal_reads_its_relief_once(monkeypatch):
    case = _mitigated()
    _relief, present = ca.mitigation_relief(case)
    problem, _cause = ca.subject_index(case).bindable_rows()
    reads = _spy_relief(monkeypatch)
    assert _mitigation_verify_refusal(case) == {
        "reason": MitigationRefusalReason.NO_BOUND_RELIEF,
        "problem_row_ids": [e.evidence_id for e in problem[:5]],
        "attempt_turn": ca.mitigation_attempt_turn(case),
        "present_turn": present,
    }
    assert len(reads) == 1
