"""Rendering a decider's codes in a test that calls the decider directly
(#2636): the decision cycle's narration step, after the record."""

from faultmaven.core.investigation.milestone_engine.transitions import (
    declined_resolution_reask,
)
from faultmaven.core.investigation.milestone_engine.turn_narration import (
    OUTCOME_WRITTEN_KEY,
    mark_told,
    narrate_turn,
)
from faultmaven.core.investigation.milestone_engine.turn_records import (
    recorded_notices,
)
from faultmaven.core.investigation.narration.codes import NoticeCode
from faultmaven.core.investigation.narration.notices import render_notice
from faultmaven.core.investigation.outcome import record_case_outcome


def narrated(case, metadata: dict) -> list[str]:
    """Write the case's record, narrate ``metadata``'s decision log as the
    turn's narration step does, mark the unbound rows told on the notices the
    turn record would keep (#2555), and return its notices."""
    metadata[OUTCOME_WRITTEN_KEY] = record_case_outcome(case, at="cycle")
    narrate_turn(case, metadata)
    mark_told(case, metadata, recorded_notices(metadata.get("system_notices") or []))
    metadata.pop(OUTCOME_WRITTEN_KEY)
    return metadata.get("system_notices") or []


def reask_feedback(case) -> str | None:
    """The refusal ``declined_resolution_reask`` decides, rendered."""
    params = declined_resolution_reask(case)
    if params is None:
        return None
    (text,) = render_notice(NoticeCode.DECLINED_RESOLUTION_REASK, params, None, [])
    return text
