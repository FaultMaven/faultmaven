"""Case Checkpoint Model.

Represents an immutable snapshot of a case at a specific point in time (turn).
Used for time-travel debugging, drift detection, and undo functionality.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class CaseCheckpoint(BaseModel):
    """
    Immutable snapshot of a case at a specific turn.
    """

    checkpoint_id: str = Field(
        ...,
        description="Unique identifier for the checkpoint. Format: {case_id}:turn:{turn_number}",
    )
    case_id: str = Field(..., description="ID of the case being checkpointed")
    turn_number: int = Field(..., description="Turn number this checkpoint represents")
    case_snapshot: Dict[str, Any] = Field(
        ..., description="Full serialized state of the case"
    )
    snapshot_hash: str = Field(
        ...,
        description="SHA256 hash of the snapshot content for efficient deduplication/drift detection",
    )
    trigger: str = Field(
        ...,
        description="Event that triggered the checkpoint (e.g., 'turn_complete', 'pre_case_action')",
    )
    created_at: datetime = Field(..., description="When the checkpoint was created")
    metadata: Dict[str, Any] = Field(
        default_factory=dict, description="Additional context metadata"
    )

    model_config = ConfigDict(
        frozen=True,  # Checkpoints are immutable
    )
