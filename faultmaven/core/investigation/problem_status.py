"""The single writer of ``InvestigationProgress.problem_status``.

``problem_status`` says where the confirmed problem statement stands against the
evidence. ``symptom_verified`` is its derived boolean view, so it cannot drift
from it. Every transition goes through a function here, which is what lets a
reader trust the value: four places used to assign the old boolean directly
(the milestone loop, the KB-resolution collapse, the evidence-citation revert
and the retraction), each with its own idea of what a change meant.
``tests/unit/core/investigation/test_problem_status.py`` scans the package and
fails on any other assignment.

``cause_work_accepted`` is the one predicate every cause-work gate reads. Cause
work — hypotheses, causal chains, a root-cause conclusion, its method — is
accepted only once the problem is verified. The gates read the status the turn
ENDS with: the milestone step and the evidence-citation revert both run before
them, so one turn that verifies the symptom and brings its cause can still form
hypotheses, ground a chain and identify the cause together. That is the
opportunistic flow, and nothing here may defer it.
"""

import logging

from faultmaven.modules.case.contracts import Case, ProblemStatus

logger = logging.getLogger(__name__)


def cause_work_accepted(case: Case) -> bool:
    """Whether this case accepts cause work: hypotheses, chains, a root-cause
    conclusion. True once the problem is verified."""
    return case.progress.problem_status == ProblemStatus.VERIFIED


def verify_problem(case: Case, *, via: str) -> bool:
    """Record that evidence shows the stated symptom.

    ``via`` names the writer for the log line. Returns whether the status
    changed, so a caller can record the milestone only on the edge.
    """
    return _move(case, ProblemStatus.VERIFIED, via=via)


def unverify_problem(case: Case, *, via: str) -> bool:
    """Withdraw the verification: the symptom claim was retracted, or the
    evidence it cited does not support it. Hypotheses already standing are
    left as they are; new cause work is refused until it is verified again.
    """
    return _move(case, ProblemStatus.UNVERIFIED, via=via)


def _move(case: Case, to: ProblemStatus, *, via: str) -> bool:
    progress = case.progress
    before = progress.problem_status
    if before == to:
        return False
    progress.problem_status = to
    logger.info(
        "Case %s: problem_status %s -> %s at turn %s (via %s)",
        case.case_id,
        before.value,
        to.value,
        case.current_turn,
        via,
    )
    return True
