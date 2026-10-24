import asyncio
import logging
import sys
from datetime import datetime, timezone
from uuid import uuid4

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Mock dependencies
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakeredis import aioredis as fake_aioredis

from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.exceptions import ValidationException
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.case.contracts import Case, CaseState
from faultmaven.modules.case.domain.models.progress import InvestigationProgress


async def test_intent_routing():
    """Test intent-based routing and heuristic detection."""
    print("\n🚀 Starting Intent Routing Tests...\n")

    # 1. Setup Mock Service
    mock_engine = AsyncMock()
    # The turn's redaction context is loaded from these two (#1956). A bare
    # AsyncMock answers ``hgetall`` with another mock, whose ``.items()`` is a
    # coroutine nobody awaits: no sanitizer, as when none is configured, and a
    # real (fake) Redis.
    mock_engine.deps.sanitizer = None
    mock_engine.deps.redis_client = fake_aioredis.FakeRedis()
    mock_repo = AsyncMock()

    # Mock engine process_turn response - WILL BE UPDATED AFTER mock_case CREATION
    # Moved initialization below mock_case creation

    service = InvestigationService(mock_engine, mock_repo)

    # 2. Setup Mock Case
    case_id = f"case_{uuid4().hex[:12]}"
    user_id = "user_123"

    mock_case = Case(
        case_id=case_id,
        user_id=user_id,
        enterprise_id="org_1",
        title="Test Case",
        description="Test Description",
        state=CaseState.INQUIRY,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        last_activity_at=datetime.now(timezone.utc),
        current_turn=1,
        message_count=0,
        current_stage="inquiry",
        path_selection=None,
        progress=InvestigationProgress(),
        evidence=[],
        hypotheses={},
        solutions=[],
        action_history=[],
        participants=[],
        messages=[],
    )

    mock_repo.get.return_value = mock_case
    mock_repo.save.return_value = mock_case

    # Set engine return value using the created mock_case
    mock_engine.process_turn.return_value = {
        "agent_response": "Mock LLM Response",
        "case_updated": mock_case,
        "metadata": {"progress_made": True},
    }

    # =========================================================================
    # Test 1: Heuristic Greeting Detection (Logic Check)
    # =========================================================================
    print("🧪 Test 1: Heuristic Greeting Detection")

    from faultmaven.modules.agent.domain.services.orientation import (
        OrientationKind,
        detect_orientation,
    )

    def _heuristic(msg):
        kind = detect_orientation(msg)
        return IntentType.GREETING if kind and kind != OrientationKind.EMPTY else None

    greetings = ["Hello", "hi", "Hi FaultMaven", "Greetings.", "Help!", "hey"]
    non_greetings = [
        "Hello, the system is down",
        "Hi there, I have an error",
        "Help me fix this bug",
    ]

    for msg in greetings:
        assert _heuristic(msg) == IntentType.GREETING, msg

    for msg in non_greetings:
        assert _heuristic(msg) is None, msg

    # =========================================================================
    # Test 2: Heuristic Handler Execution
    # =========================================================================
    print("\n🧪 Test 2: Heuristic Handler Execution")

    # "Hi" should trigger _handle_greeting logic
    payload = TurnPayload(query="Hi")
    # Force default intent type (CONVERSATION) to test heuristic override

    result = await service.process_turn(case_id, user_id, payload)

    assert "I'm FaultMaven" in result.agent_response, result.agent_response

    # =========================================================================
    # Test 3: Explicit Intent (Status Transition)
    # =========================================================================
    print("\n🧪 Test 3: Explicit Status Transition")

    # CLOSED, not RESOLVED. RESOLVED stopped being a user-selectable case
    # action (contract 9.0.0) — it is earned by a confirmed root-cause
    # elimination and offered by the agent — so the service refuses it at the
    # boundary and the engine is never reached. Routing is still the subject
    # here, so it is exercised on the one disposition a user may still pick.
    mock_case.messages = []  # Reset messages

    payload_transition = TurnPayload(
        query="Close this",
        intent=QueryIntent(
            type=IntentType.STATUS_TRANSITION, to_state=CaseState.CLOSED
        ),
    )

    await service.process_turn(case_id, user_id, payload_transition)

    # The engine was called with the transition's intent and data
    call_args = mock_engine.process_turn.call_args
    assert call_args is not None, "the engine was not called"
    assert call_args.kwargs.get("intent_type") == "status_transition"
    assert call_args.kwargs.get("intent_data", {}).get("to_state") == "closed"

    # =========================================================================
    # Test 4: The refused edges never reach the engine
    # =========================================================================
    print("\n🧪 Test 4: Earned edges are refused at the boundary")

    for refused in (CaseState.RESOLVED, CaseState.INVESTIGATING):
        mock_engine.process_turn.reset_mock()
        mock_case.messages = []
        with pytest.raises(
            ValidationException, match="not a user-selectable case action"
        ):
            await service.process_turn(
                case_id,
                user_id,
                TurnPayload(
                    query=f"Set {refused.value}",
                    intent=QueryIntent(
                        type=IntentType.STATUS_TRANSITION, to_state=refused
                    ),
                ),
            )
        assert not mock_engine.process_turn.called


if __name__ == "__main__":
    asyncio.run(test_intent_routing())
