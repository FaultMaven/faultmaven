"""Checkpoint Service for Investigation Engine

Centralizes checkpoint creation logic for case state snapshots.
Checkpoints are created after turn completion and before status transitions.

Usage:
    service = CheckpointService(case_repo)
    await service.create_checkpoint(case, trigger="turn_complete")
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from faultmaven.modules.case.contracts import CaseCheckpoint

logger = logging.getLogger(__name__)


class CheckpointService:
    """Creates and manages case checkpoints (immutable state snapshots)."""

    def __init__(self, case_repo: Any):
        """Initialize with a case repository that supports create_checkpoint()."""
        self.case_repo = case_repo

    async def create_checkpoint(
        self,
        case: Any,
        trigger: str = "turn_complete",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[CaseCheckpoint]:
        """
        Create an immutable snapshot of the case.

        Args:
            case: The Case object to snapshot
            trigger: Event that triggered the checkpoint
            metadata: Additional context (e.g., old_status, new_status)

        Returns:
            CaseCheckpoint if created, None if failed
        """
        try:
            case_snapshot = (
                case.model_dump() if hasattr(case, "model_dump") else case.__dict__
            )

            snapshot_json = (
                case.model_dump_json()
                if hasattr(case, "model_dump_json")
                else str(case_snapshot)
            )
            snapshot_hash = hashlib.sha256(snapshot_json.encode()).hexdigest()

            checkpoint = CaseCheckpoint(
                checkpoint_id=f"{case.case_id}:turn:{case.current_turn}:{trigger}",
                case_id=case.case_id,
                turn_number=case.current_turn,
                case_snapshot=case_snapshot,
                snapshot_hash=snapshot_hash,
                trigger=trigger,
                created_at=datetime.now(timezone.utc),
                metadata=metadata or {},
            )

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
