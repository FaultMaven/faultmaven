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

from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.contracts import Case, CaseState
from faultmaven.modules.case.domain.models import InvestigationProgress


async def test_intent_routing():
    """Test intent-based routing and heuristic detection."""
    print("\n🚀 Starting Intent Routing Tests...\n")

    # 1. Setup Mock Service
    mock_engine = AsyncMock()
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
        organization_id="org_1",
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

    greetings = ["Hello", "hi", "Hi FaultMaven", "Greetings.", "Help!", "hey"]
    non_greetings = [
        "Hello, the system is down",
        "Hi there, I have an error",
        "Help me fix this bug",
    ]

    for msg in greetings:
        intent = service._detect_intent_heuristic(msg)
        if intent == IntentType.GREETING:
            print(f"✅ Correctly detected GREETING for: '{msg}'")
        else:
            print(f"❌ FAILED to detect GREETING for: '{msg}' (Got: {intent})")

    for msg in non_greetings:
        intent = service._detect_intent_heuristic(msg)
        if intent is None:
            print(f"✅ Correctly ignored non-greeting: '{msg}'")
        else:
            print(f"❌ Incorrectly detected {intent} for: '{msg}'")

    # =========================================================================
    # Test 2: Heuristic Handler Execution
    # =========================================================================
    print("\n🧪 Test 2: Heuristic Handler Execution")

    # "Hi" should trigger _handle_greeting logic
    payload = TurnPayload(query="Hi")
    # Force default intent type (CONVERSATION) to test heuristic override

    result = await service.process_turn(case_id, user_id, payload)

    if "I'm FaultMaven" in result.agent_response:
        print(f"✅ Service returned static greeting: '{result.agent_response[:50]}...'")
    else:
        print(f"❌ Service returned unexpected response: '{result.agent_response}'")

    print(f"   Turn number: {result.turn_number}")

    # =========================================================================
    # Test 3: Explicit Intent (Status Transition)
    # =========================================================================
    print("\n🧪 Test 3: Explicit Status Transition")

    # Mock logic inside _handle_status_transition would need full implementation,
    # but we can verify it routes correctly by mocking the handler method on the instance
    # (Checking if it calls engine with correct intent data)

    # We will just verify it calls engine with structured intent for now,
    # since we mocked the engine.

    mock_case.messages = []  # Reset messages

    payload_transition = TurnPayload(
        query="Resolve this",
        intent=QueryIntent(
            type=IntentType.STATUS_TRANSITION, to_state=CaseState.RESOLVED
        ),
    )

    await service.process_turn(case_id, user_id, payload_transition)

    # Check if engine was called with correct intent_type
    call_args = mock_engine.process_turn.call_args
    if call_args:
        kwargs = call_args.kwargs
        if (
            kwargs.get("intent_type") == "status_transition"
            and kwargs.get("intent_data", {}).get("to_state") == "resolved"
        ):
            print("✅ Engine called with correct 'status_transition' intent and data")
        else:
            print(f"❌ Engine called with incorrect args: {kwargs}")
    else:
        print("❌ Engine was not called (or logic bypassed engine unexpectedly)")

    print("\nDONE")


if __name__ == "__main__":
    asyncio.run(test_intent_routing())
