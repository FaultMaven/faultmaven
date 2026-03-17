"""Unit tests for CaseReplayer service.

Tests validation of:
1. restore_at_turn (checkpoint retrieval and deserialization)
2. diff_turns (recursive diff logic)
"""

from typing import List
from unittest.mock import AsyncMock, Mock

import pytest

from faultmaven.modules.case.domain.models import Case
from faultmaven.modules.case.domain.owned_models.checkpoint import CaseCheckpoint
from faultmaven.modules.case.domain.services.replayer import CaseReplayer


@pytest.fixture
def mock_case_repo():
    return AsyncMock()


@pytest.fixture
def replayer(mock_case_repo):
    return CaseReplayer(mock_case_repo)


@pytest.fixture
def sample_case_snapshot():
    return {
        "case_id": "case_1234567890ab",  # Valid format (17 chars: "case_" + 12 hex)
        "user_id": "user-1",
        "organization_id": "org-1",
        "title": "Test Case",
        "description": "Test Description",
        "status": "inquiry",
        "current_turn": 5,
        "progress": {"symptom_verified": True},
    }


@pytest.mark.asyncio
async def test_restore_at_turn_success(replayer, mock_case_repo, sample_case_snapshot):
    # Setup
    checkpoint = CaseCheckpoint(
        checkpoint_id="chk_1",
        case_id="test-case-123",
        turn_number=5,
        case_snapshot=sample_case_snapshot,
        snapshot_hash="abc",
        trigger="turn_complete",
        created_at="2023-01-01T00:00:00Z",
    )
    mock_case_repo.get_checkpoints.return_value = [checkpoint]

    # Execute
    case = await replayer.restore_at_turn("test-case-123", 5)

    # Verify
    assert isinstance(case, Case)
    assert case.case_id == "case_1234567890ab"
    assert case.current_turn == 5
    # Verify nested model reconstruction
    assert case.progress.symptom_verified is True


@pytest.mark.asyncio
async def test_restore_at_turn_not_found(replayer, mock_case_repo):
    # Setup
    mock_case_repo.get_checkpoints.return_value = []

    # Execute & Verify
    with pytest.raises(ValueError, match="Checkpoint for case .* not found"):
        await replayer.restore_at_turn("case_1", 99)


@pytest.mark.asyncio
async def test_diff_turns(replayer, mock_case_repo):
    # Setup
    base_snapshot = {
        "case_id": "case_1234567890ab",
        "user_id": "u1",
        "organization_id": "o1",
        "title": "Old Title",
        "description": "Old Description",
        "status": "inquiry",
        "current_turn": 1,
    }

    snapshot_a = base_snapshot.copy()

    snapshot_b = base_snapshot.copy()
    snapshot_b.update({"current_turn": 2, "description": "New Description"})

    cp_a = CaseCheckpoint(
        checkpoint_id="1",
        case_id="case_1234567890ab",
        turn_number=1,
        case_snapshot=snapshot_a,
        snapshot_hash="a",
        trigger="t",
        created_at="2023-01-01",
    )
    cp_b = CaseCheckpoint(
        checkpoint_id="2",
        case_id="case_1234567890ab",
        turn_number=2,
        case_snapshot=snapshot_b,
        snapshot_hash="b",
        trigger="t",
        created_at="2023-01-02",
    )

    mock_case_repo.get_checkpoints.return_value = [cp_a, cp_b]

    # Execute
    diff = await replayer.diff_turns("case_1234567890ab", 1, 2)

    # Verify
    # current_turn changed
    assert diff["modified"]["current_turn"] == {"old": 1, "new": 2}

    # description changed
    assert diff["modified"]["description"] == {
        "old": "Old Description",
        "new": "New Description",
    }

    # title unchanged -> should not be in diff
    assert "title" not in diff.get("modified", {})


def test_recursive_diff_logic(replayer):
    # Unit test just for the private logic
    obj_a = {"a": 1, "b": [1, 2], "c": {"x": 1}}
    obj_b = {"a": 1, "b": [1, 3], "c": {"x": 2}, "d": 4}

    diff = replayer._compute_recursive_diff(obj_a, obj_b)

    assert diff["added"] == {"d": 4}
    assert diff["modified"]["c"]["modified"]["x"] == {"old": 1, "new": 2}
    # List change check
    assert diff["modified"]["b"]["old_value"] == [1, 2]
    assert diff["modified"]["b"]["new_value"] == [1, 3]
