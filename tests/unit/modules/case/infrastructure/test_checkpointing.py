"""Unit tests for State Checkpointing."""

from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo

import pytest

from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InvestigationStrategy,
)
from faultmaven.modules.case.domain.owned_models.checkpoint import CaseCheckpoint
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)


@pytest.fixture
def case_checkpoint() -> CaseCheckpoint:
    """Create a sample checkpoint."""
    return CaseCheckpoint(
        checkpoint_id="case_123:turn:1",
        case_id="case_123",
        turn_number=1,
        case_snapshot={"state": "inquiry", "title": "Test Case"},
        snapshot_hash="abc123hash",
        trigger="turn_complete",
        created_at=datetime.now(ZoneInfo("UTC")),
        metadata={"version": "1.0"},
    )


@pytest.mark.asyncio
async def test_create_and_get_checkpoint(case_checkpoint: CaseCheckpoint):
    """Test creating and retrieving a single checkpoint."""
    repo = InMemoryCaseRepository()

    # Create
    saved = await repo.create_checkpoint(case_checkpoint)
    assert saved == case_checkpoint

    # Get by ID
    retrieved = await repo.get_checkpoint(case_checkpoint.checkpoint_id)
    assert retrieved is not None
    assert retrieved.checkpoint_id == case_checkpoint.checkpoint_id
    assert retrieved.case_id == case_checkpoint.case_id
    assert retrieved.snapshot_hash == case_checkpoint.snapshot_hash


@pytest.mark.asyncio
async def test_get_checkpoints_for_case(case_checkpoint: CaseCheckpoint):
    """Test retrieving all checkpoints for a case."""
    repo = InMemoryCaseRepository()

    # Create two checkpoints for the same case
    cp1 = case_checkpoint
    cp2 = CaseCheckpoint(
        checkpoint_id="case_123:turn:2",
        case_id="case_123",
        turn_number=2,
        case_snapshot={"state": "investigating"},
        snapshot_hash="def456hash",
        trigger="turn_complete",
        created_at=datetime.now(ZoneInfo("UTC")),
    )

    await repo.create_checkpoint(cp1)
    await repo.create_checkpoint(cp2)

    # Create a checkpoint for a different case
    other_cp = CaseCheckpoint(
        checkpoint_id="other_case:turn:1",
        case_id="other_case",
        turn_number=1,
        case_snapshot={},
        snapshot_hash="ghi789hash",
        trigger="manual",
        created_at=datetime.now(ZoneInfo("UTC")),
    )
    await repo.create_checkpoint(other_cp)

    # Get checkpoints for case_123
    checkpoints = await repo.get_checkpoints("case_123")

    assert len(checkpoints) == 2
    assert checkpoints[0].turn_number == 1
    assert checkpoints[1].turn_number == 2
    assert checkpoints[0].checkpoint_id == cp1.checkpoint_id
    assert checkpoints[1].checkpoint_id == cp2.checkpoint_id


@pytest.mark.asyncio
async def test_checkpoint_immutability():
    """Test that CaseCheckpoint models are frozen (immutable)."""
    cp = CaseCheckpoint(
        checkpoint_id="test:1",
        case_id="test",
        turn_number=1,
        case_snapshot={},
        snapshot_hash="hash",
        trigger="manual",
        created_at=datetime.now(ZoneInfo("UTC")),
    )

    with pytest.raises(
        Exception
    ):  # Validation error or TypeError depending on pydantic version
        cp.turn_number = 2
