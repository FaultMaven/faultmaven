"""Checkpoint Service for Investigation Engine

Centralizes checkpoint creation logic for case state snapshots.
Checkpoints are taken before state transitions.

Two halves (#1882):

- ``capture`` builds the snapshot and touches no storage, so a turn can carry
  it to the turn's own commit (``ICaseRepository.save(case, checkpoints=...)``).
- ``create_checkpoint`` is ``capture`` plus a write of its own.

Usage:
    service = CheckpointService(case_repo)
    checkpoint = service.capture(case, trigger="pre_case_action", metadata=...)
    await service.create_checkpoint(case, trigger="pre_case_action")
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
    """Creates and manages case checkpoints (immutable state snapshots)."""

    def __init__(self, case_repo: Any):
        """Initialize with a case repository that supports create_checkpoint()."""
        self.case_repo = case_repo

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

    async def create_checkpoint(
        self,
        case: Any,
        trigger: str = "turn_complete",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[CaseCheckpoint]:
        """
        Capture a snapshot of the case and write it in its own transaction.

        Args:
            case: The Case object to snapshot
            trigger: Event that triggered the checkpoint
            metadata: Additional context (e.g., old_status, new_status)

        Returns:
            CaseCheckpoint if created, None if failed
        """
        try:
            checkpoint = self.capture(case, trigger, metadata)
            await self.case_repo.create_checkpoint(checkpoint)
            logger.debug(
                f"Checkpoint created: case={case.case_id} turn={case.current_turn} trigger={trigger}"
            )
            return checkpoint

        except Exception as e:
            logger.warning(
                f"Failed to create checkpoint for case {case.case_id}: {e}",
                exc_info=True,
                extra={"case_id": case.case_id, "trigger": trigger},
            )
            return None
