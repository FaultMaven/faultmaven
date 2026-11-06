"""A turn through the real apply, then its turn record (#2036 D2.3).

``test_fix_verification_subjects_1906._record`` runs the apply alone, so the
case it builds has no ``turn_history``, and a fix backdated to an earlier turn
(#2427) would read that turn as unrecorded. ``_record`` here also writes the
turn's record the way the turn path's step 6 does
(``turn_records._create_turn_record``), carrying the cause the apply took when
it started (``TurnProgress.cause_at_start``).
"""

from faultmaven.core.investigation.milestone_engine.response_application import (
    FIX_CAUSE_AT_START_KEY,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _create_turn_record,
)
from faultmaven.modules.case.contracts import TurnOutcome
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _record as _apply_turn,
)


def write_turn_record(case, meta: dict) -> None:
    """Step 6's record of ``case.current_turn`` from its apply's ``meta``. A
    turn applied twice keeps its first record: the start of the turn."""
    if any(t.turn_number == case.current_turn for t in case.turn_history):
        return
    case.turn_history.append(
        _create_turn_record(
            turn_number=case.current_turn,
            cause_at_start=meta.get(FIX_CAUSE_AT_START_KEY),
            milestones_completed=list(meta.get("milestones_completed") or []),
            evidence_added=list(meta.get("evidence_added") or []),
            hypotheses_generated=list(meta.get("hypotheses_generated") or []),
            hypotheses_validated=list(meta.get("hypotheses_validated") or []),
            solutions_proposed=list(meta.get("solutions_proposed") or []),
            progress_made=bool(meta.get("progress_made")),
            outcome=TurnOutcome.CONVERSATION,
            user_message="",
            agent_response="",
        )
    )


async def _record(eng, case, turn, *rows, **fields):
    """``test_fix_verification_subjects_1906._record``, then the turn's record."""
    created, meta = await _apply_turn(eng, case, turn, *rows, **fields)
    write_turn_record(case, meta)
    return created, meta
