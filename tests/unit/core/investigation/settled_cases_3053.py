"""When a mitigation is settled, read by each of its readers: the fixture
corpus (#3053 S3d-2, ruling Q2).

Every ``MitigationRecord`` state the validator admits (accepted, verified,
disposition), and none, crossed with the solution gates; and a bound relapse
(the verify registered, then the problem seen present again), with and without
a disposition. Every fixture is built from the suite's builders.

``render`` reads each reader of "settled" on ``main``'s APIs: the stage
(``current_stage``), ``cause_assurance.mitigation_settled`` and
``mitigation_unsettled``, the operator list's stage (``CaseMetadata.
from_stored`` on the stored gates, under INVESTIGATING) and the monitor's
mitigation test (``_detect_fix_failure_cycle``, at a threshold of one
accepted MITIGATION action). Its output on ``main`` is
``settled_oracle_3053.json``, captured by running ``render_all`` with this
file copied into a ``git archive`` extract of ``main`` at babd5b944, never
typed by hand.
"""

from __future__ import annotations

from faultmaven.core.investigation.cause_assurance import (
    mitigation_settled,
    mitigation_unsettled,
)
from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.modules.case.contracts import CaseState, EvidenceCategory
from faultmaven.modules.case.domain.models.metadata import CaseMetadata
from tests.unit.core.investigation.bound_rows import bound_row, mitigated
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _case
from tests.unit.modules.case.test_case_metadata_derivation import _stored

#: (accepted, verified, disposition): every state the validator admits.
MITIGATIONS = {
    "none": None,
    "proposed": (False, False, None),
    "accepted": (True, False, None),
    "verified": (True, True, None),
    "declared_stable": (True, False, "declared_stable"),
    "proceed_unmitigated": (True, False, "proceed_unmitigated"),
    "verified_declared_stable": (True, True, "declared_stable"),
}
#: (solution_accepted, solution_verified).
SOLUTIONS = {
    "no_solution": (False, False),
    "solution_accepted": (True, False),
    "solution_verified": (True, True),
}


def _build(mitigation, solution, *, relapse: bool = False):
    case = _case()
    if mitigation is not None:
        accepted, verified, disposition = mitigation
        shown = mitigated(case, attempt=6, relief=7 if verified else None)
        record = case.progress.mitigation
        record.accepted, record.verified = accepted, verified
        record.completed_at_turn = 7 if verified else None
        if disposition is not None:
            record.disposition, record.disposition_at_turn = disposition, 7
        if relapse:
            bound_row(case, EvidenceCategory.SYMPTOM_EVIDENCE, turn=8, rechecks=shown)
    case.progress.solution_accepted, case.progress.solution_verified = solution
    return case


#: Every fixture, by name.
CASES = {
    f"{m}:{s}": (lambda mm=mitigation, ss=solution: _build(mm, ss))
    for m, mitigation in MITIGATIONS.items()
    for s, solution in SOLUTIONS.items()
}
for _s, _solution in SOLUTIONS.items():
    CASES[f"relapse:{_s}"] = lambda ss=_solution: _build(
        (True, True, None), ss, relapse=True
    )
    CASES[f"relapse_declared_stable:{_s}"] = lambda ss=_solution: _build(
        (True, True, "declared_stable"), ss, relapse=True
    )


def gates(case) -> dict:
    """The operator list's five stored gate inputs."""
    record = case.progress.mitigation
    return dict(
        mitigation_accepted=bool(record and record.accepted),
        mitigation_verified=bool(record and record.verified),
        mitigation_disposed=bool(record and record.disposition is not None),
        solution_accepted=case.progress.solution_accepted,
        solution_verified=case.progress.solution_verified,
    )


def render(case) -> dict:
    """What each reader of "settled" says of ``case``."""
    stored = _stored(state=CaseState.INVESTIGATING.value, **gates(case))
    return {
        "current_stage": case.progress.current_stage.value,
        "mitigation_settled": mitigation_settled(case),
        "mitigation_unsettled": mitigation_unsettled(case),
        "operator_stage": CaseMetadata.from_stored(**stored).stage.value,
        "monitor_fix_failure": ProgressMonitor(
            fix_failure_threshold=1
        )._detect_fix_failure_cycle(case, "mitigation"),
    }


def render_all() -> dict[str, dict]:
    """Each fixture's readings, by name."""
    return {name: render(build()) for name, build in CASES.items()}
