"""A case's ambient metadata, read without loading the case (ADR-012 D9).

The cross-enterprise operator list reads cases it may not load: under
``TENANT_PROVIDER=multi`` row-level security hides every enterprise but the
bound one, and the one path that spans them all returns primitives only — ids,
timestamps, counters, booleans, id arrays and three closed-vocabulary strings —
and no column sourced from a title, a description or anything else a user
typed. Of those strings the database enforces ``state``; ``source`` and
``closure_reason`` are closed by the ``Case`` model that writes them.

Two of the fields an operator sees are not columns. ``stage`` is derived from
four gate milestones inside the ``progress`` blob, and ``investigation_turn``
from the out-of-band entries of the turn history inside ``metadata``. The read
path therefore returns their primitive inputs, and :meth:`CaseMetadata.from_stored`
applies the same rules a loaded :class:`~.case.Case` applies — the module-level
functions its properties delegate to — so each rule exists exactly once. That
includes the turn-sequence repair every case load performs
(:func:`~.case.reconcile_turn_numbers`): a stored history with a duplicate or a
gap reads back renumbered, and the clock with it, so reading the raw numbers
would disagree with a loaded case on exactly those rows.
"""

from datetime import datetime, timezone
from typing import List, Optional, Sequence

from pydantic import BaseModel, ConfigDict

from .case import (
    distinct_turns,
    investigation_turn_at,
    reconcile_turn_numbers,
    stage_while_investigating,
)
from .lifecycle import CaseState
from .problem import InvestigationStage
from .progress import investigation_stage


class CaseMetadataUnavailableError(Exception):
    """The cross-enterprise metadata read cannot run in this database.

    Raised when the database function the read goes through does not exist —
    the database has not been migrated to the revision that creates it. A
    caller fails closed on it; it never substitutes a read that row-level
    security has narrowed to one enterprise.
    """


class CaseMetadataNotGrantedError(CaseMetadataUnavailableError):
    """The functions exist, but the connected role may not execute them.

    ``EXECUTE`` is granted to the runtime role explicitly, never to ``PUBLIC``;
    a deployment whose runtime role was not granted it lands here. Same
    fail-closed answer as a missing function, with a different fix.
    """


class CaseMetadata(BaseModel):
    """One case as metadata only: system ids, closed vocabularies, timestamps
    and counts. No field is sourced from user free text."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    enterprise_id: str
    organization_id: Optional[str]
    #: ``None`` once the owning account is deleted (the column is
    #: ``ON DELETE SET NULL``).
    user_id: Optional[str]
    state: CaseState
    source: str
    closure_reason: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime
    resolved_at: Optional[datetime]
    closed_at: Optional[datetime]
    current_turn: int
    investigation_turn: int
    stage: Optional[InvestigationStage]
    turns_without_progress: int
    is_terminal: bool
    shared_team_ids: List[str]

    @classmethod
    def from_stored(
        cls,
        *,
        case_id: str,
        enterprise_id: str,
        organization_id: Optional[str],
        user_id: Optional[str],
        state: str,
        source: str,
        closure_reason: Optional[str],
        created_at: datetime,
        updated_at: datetime,
        last_activity_at: Optional[datetime],
        resolved_at: Optional[datetime],
        closed_at: Optional[datetime],
        current_turn: Optional[int],
        turns_without_progress: Optional[int],
        mitigation_accepted: bool,
        mitigation_verified: bool,
        solution_accepted: bool,
        solution_verified: bool,
        turn_numbers: Sequence[int],
        turn_is_out_of_band: Sequence[bool],
        shared_team_ids: Sequence[str],
    ) -> "CaseMetadata":
        """Derive the metadata from a case's stored primitives.

        ``turn_numbers`` / ``turn_is_out_of_band`` describe ``turn_history`` in
        stored order, one element per entry. The four gate booleans are
        ``False`` for a gate the stored progress does not record, including a
        mitigation that was never recorded.
        """
        if len(turn_numbers) != len(turn_is_out_of_band):
            raise ValueError(
                f"case {case_id}: {len(turn_numbers)} turn numbers but "
                f"{len(turn_is_out_of_band)} out-of-band flags"
            )
        # The repair a case load applies (``Case.reconcile_turn_sequence``):
        # it can renumber entries and move the clock.
        slots, clock = reconcile_turn_numbers(list(turn_numbers), current_turn or 0)
        asides = distinct_turns(
            number
            for source_index, number in slots
            if source_index is not None and turn_is_out_of_band[source_index]
        )
        lifecycle_state = CaseState(state)
        return cls(
            case_id=case_id,
            enterprise_id=enterprise_id,
            organization_id=organization_id,
            user_id=user_id,
            state=lifecycle_state,
            source=source,
            closure_reason=closure_reason,
            created_at=created_at,
            updated_at=updated_at,
            # A NULL column loads as the model's default on a full case read,
            # so it does here too.
            last_activity_at=last_activity_at or datetime.now(timezone.utc),
            resolved_at=resolved_at,
            closed_at=closed_at,
            current_turn=clock,
            investigation_turn=investigation_turn_at(
                clock, current_turn=clock, asides=asides
            ),
            stage=stage_while_investigating(
                lifecycle_state,
                investigation_stage(
                    mitigation_accepted=mitigation_accepted,
                    mitigation_verified=mitigation_verified,
                    solution_accepted=solution_accepted,
                    solution_verified=solution_verified,
                ),
            ),
            turns_without_progress=turns_without_progress or 0,
            is_terminal=lifecycle_state.is_terminal,
            shared_team_ids=list(shared_team_ids),
        )
