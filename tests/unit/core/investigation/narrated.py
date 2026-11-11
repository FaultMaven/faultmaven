"""Rendering a decider's codes in a test that calls the decider directly
(#2636): the decision cycle's narration step, after the record."""

from faultmaven.core.investigation.milestone_engine.turn_narration import (
    OUTCOME_WRITTEN_KEY,
    narrate_turn,
)
from faultmaven.core.investigation.outcome import record_case_outcome


def narrated(case, metadata: dict) -> list[str]:
    """Write the case's record, narrate ``metadata``'s decision log as the
    turn's narration step does, and return its notices."""
    metadata[OUTCOME_WRITTEN_KEY] = record_case_outcome(case, at="cycle")
    narrate_turn(case, metadata)
    metadata.pop(OUTCOME_WRITTEN_KEY)
    return metadata.get("system_notices") or []
