"""Checkpoint Service for Investigation Engine

Case state snapshots, taken before state transitions.

``capture`` builds the snapshot and touches no storage. The turn carries it to
its own commit (``TurnCommitPlan.add_checkpoint``, then
``ICaseRepository.save(case, checkpoints=...)``), so a checkpoint commits with
the transition it precedes, or not at all (#1882). There is no write of its
own: a checkpoint committed separately is a mid-turn commit, which outlives a
turn that then fails.

Usage:
    plan.add_checkpoint(
        CheckpointService().capture(case, trigger="pre_case_action", metadata=...)
    )
"""

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from faultmaven.modules.case.contracts import CaseCheckpoint

logger = logging.getLogger(__name__)

#: Namespace of the checkpoint id (a UUIDv5). Fixed: changing it re-keys every
#: future checkpoint, so the same (case, turn, trigger, target) would no longer
#: collide with a row written before the change.
_CHECKPOINT_ID_NAMESPACE = uuid.UUID("5f1d7a52-3c1e-4b8e-9a7e-2f0c8d4b1e63")


def checkpoint_id_for(
    case_id: str, turn_number: int, trigger: str, target: Optional[str]
) -> str:
    """The id of the checkpoint a site takes at one turn, for one target.

    Deterministic, so the same site firing twice for the same transition in one
    turn produces the same id and the second write fails on the primary key
    rather than storing a second snapshot (#1882 R6). ``target`` is what makes
    two different sites in one turn distinct: the transition's ``to_state``, or
    the action a non-transition site names.

    A UUIDv5 because ``case_checkpoints.checkpoint_id`` is VARCHAR(36): the
    readable ``{case_id}:turn:{n}:{trigger}`` it replaces was 40 characters at
    the shortest, so PostgreSQL refused every checkpoint write
    (StringDataRightTruncation) and the service logged and dropped it. The turn
    and trigger stay readable in their own columns.
    """
    key = f"{case_id}:turn:{turn_number}:{trigger}:{target or ''}"
    return str(uuid.uuid5(_CHECKPOINT_ID_NAMESPACE, key))


class CheckpointService:
    """Takes case checkpoints (immutable state snapshots)."""

    @staticmethod
    def capture(
        case: Any,
        trigger: str = "turn_complete",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CaseCheckpoint:
        """Snapshot ``case`` as it stands now. Pure: no storage is touched.

        Args:
            case: The Case object to snapshot
            trigger: Event that triggered the checkpoint
            metadata: Additional context (e.g., from_state, to_state)

        Returns:
            The CaseCheckpoint, not yet written anywhere.
        """
        metadata = metadata or {}
        case_snapshot = (
            case.model_dump() if hasattr(case, "model_dump") else case.__dict__
        )

        snapshot_json = (
            case.model_dump_json()
            if hasattr(case, "model_dump_json")
            else str(case_snapshot)
        )
        snapshot_hash = hashlib.sha256(snapshot_json.encode()).hexdigest()

        return CaseCheckpoint(
            checkpoint_id=checkpoint_id_for(
                case.case_id,
                case.current_turn,
                trigger,
                metadata.get("to_state") or metadata.get("action"),
            ),
            case_id=case.case_id,
            turn_number=case.current_turn,
            case_snapshot=case_snapshot,
            snapshot_hash=snapshot_hash,
            trigger=trigger,
            created_at=datetime.now(timezone.utc),
            metadata=metadata,
        )
