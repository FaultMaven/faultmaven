"""Integration tests for new CaseRepository features (standalone evidence and agent executions).

This test verifies that the new methods work correctly together and with existing services.
Run with: python3 -m pytest tests/unit/infrastructure/persistence/test_new_features_integration.py -v
Or run directly: python3 tests/unit/infrastructure/persistence/test_new_features_integration.py
"""

import asyncio
import sys
from datetime import datetime, timezone
from uuid import uuid4

# Add project root to path
sys.path.insert(0, "/home/swhouse/product/faultmaven")

from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.evidence.domain.models import (
    EvidenceArtifact,
    EvidenceArtifactType,
    EvidenceListFilter,
    StorageBackend,
)
from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)


def generate_case_id() -> str:
    """Generate a valid case ID."""
    return f"case_{uuid4().hex[:12]}"


def generate_execution_id() -> str:
    """Generate a valid execution ID."""
    return f"exec_{uuid4().hex[:12]}"


def generate_tool_call_id() -> str:
    """Generate a valid tool call ID."""
    return f"tc_{uuid4().hex[:12]}"


async def test_standalone_evidence_workflow():
    """Test complete standalone evidence workflow."""
    print("Test 1: Standalone Evidence Workflow")
    repo = InMemoryCaseRepository()

    user_id = str(uuid4())
    case_id = generate_case_id()

    # Create standalone evidence
    evidence = await repo.create_standalone_evidence(
        filename="test.log",
        content_type="text/plain",
        size_bytes=1024,
        storage_path="evidence/test.log",
        uploaded_by=user_id,
        description="Test evidence",
        tags=["test", "log"],
    )

    assert evidence is not None, "Evidence should be created"
    assert evidence.evidence_id is not None, "Evidence should have ID"
    assert evidence.user_id == user_id, "Evidence should have correct user_id"
    assert "test" in evidence.tags, "Evidence should have tags"
    print("  ✓ Create standalone evidence")

    # Get evidence
    retrieved = await repo.get_standalone_evidence(evidence.evidence_id)
    assert retrieved is not None, "Evidence should be retrievable"
    assert retrieved.evidence_id == evidence.evidence_id, "Should return same evidence"
    print("  ✓ Get standalone evidence")

    # List evidence
    filters = EvidenceListFilter(limit=10, offset=0)
    evidence_list, total = await repo.list_standalone_evidence(filters)
    assert total == 1, "Should have 1 evidence"
    assert len(evidence_list) == 1, "Should return 1 evidence"
    print("  ✓ List standalone evidence")

    # Filter by tags
    tag_filters = EvidenceListFilter(tags=["test"], limit=10, offset=0)
    filtered, count = await repo.list_standalone_evidence(tag_filters)
    assert count == 1, "Should filter by tags"
    print("  ✓ Filter by tags")

    # Link to case (use UUID for case_id)
    case_uuid = uuid4()
    linked = await repo.link_standalone_evidence_to_case(
        evidence.evidence_id, str(case_uuid)
    )
    assert linked is not None, "Should link successfully"
    assert (
        str(case_uuid) in linked.linked_case_ids
    ), "Should have case_id in linked_case_ids"
    print("  ✓ Link to case")

    # Filter by case_id (EvidenceListFilter expects str, not UUID)
    wrong_case_id = str(uuid4())
    case_filters = EvidenceListFilter(
        case_id=wrong_case_id, limit=10, offset=0
    )  # Wrong case
    filtered_wrong, count_wrong = await repo.list_standalone_evidence(case_filters)
    assert count_wrong == 0, "Should not find evidence for wrong case"

    case_filters_correct = EvidenceListFilter(
        case_id=str(case_uuid), limit=10, offset=0
    )
    filtered_correct, count_correct = await repo.list_standalone_evidence(
        case_filters_correct
    )
    assert count_correct == 1, "Should find evidence for correct case"
    print("  ✓ Filter by case_id")

    # Delete evidence
    deleted = await repo.delete_standalone_evidence(evidence.evidence_id)
    assert deleted is True, "Should delete successfully"

    retrieved_after = await repo.get_standalone_evidence(evidence.evidence_id)
    assert retrieved_after is None, "Should not find deleted evidence"
    print("  ✓ Delete standalone evidence")

    print("  ✅ All standalone evidence tests passed!\n")


async def test_agent_execution_workflow():
    """Test complete agent execution workflow."""
    print("Test 2: Agent Execution Workflow")
    repo = InMemoryCaseRepository()

    case_id = generate_case_id()

    # Create execution
    execution = AgentExecution(
        execution_id=generate_execution_id(),
        case_id=case_id,
        agent_type=AgentType.INVESTIGATOR,
        agent_model="gpt-4",
        status=ExecutionStatus.QUEUED,
        prompt="Test prompt",
        tool_calls=[],
    )

    created = await repo.create_agent_execution(execution)
    assert created is not None, "Execution should be created"
    assert (
        created.execution_id == execution.execution_id
    ), "Should return same execution"
    print("  ✓ Create agent execution")

    # Get execution
    retrieved = await repo.get_agent_execution(execution.execution_id)
    assert retrieved is not None, "Execution should be retrievable"
    assert (
        retrieved.execution_id == execution.execution_id
    ), "Should return same execution"
    assert retrieved.tool_calls == [], "Should have empty tool calls initially"
    print("  ✓ Get agent execution")

    # Create tool call
    tool_call = AgentToolCall(
        tool_call_id=generate_tool_call_id(),
        execution_id=execution.execution_id,
        tool_name="web_search",
        tool_input={"query": "test"},
        status="pending",
    )

    created_tc = await repo.create_agent_tool_call(tool_call)
    assert created_tc is not None, "Tool call should be created"
    assert (
        created_tc.tool_call_id == tool_call.tool_call_id
    ), "Should return same tool call"
    print("  ✓ Create agent tool call")

    # Get execution with tool calls
    retrieved_with_tc = await repo.get_agent_execution(execution.execution_id)
    assert retrieved_with_tc is not None, "Execution should be retrievable"
    assert len(retrieved_with_tc.tool_calls) == 1, "Should have 1 tool call"
    assert (
        retrieved_with_tc.tool_calls[0].tool_call_id == tool_call.tool_call_id
    ), "Should have correct tool call"
    print("  ✓ Get execution with tool calls")

    # Get tool calls for execution
    tool_calls = await repo.get_agent_tool_calls_for_execution(execution.execution_id)
    assert len(tool_calls) == 1, "Should return 1 tool call"
    assert (
        tool_calls[0].tool_call_id == tool_call.tool_call_id
    ), "Should return correct tool call"
    print("  ✓ Get tool calls for execution")

    # Update tool call
    tool_call.status = "success"
    tool_call.result = {"data": "test result"}
    updated_tc = await repo.update_agent_tool_call(tool_call)
    assert updated_tc.status == "success", "Tool call should be updated"
    print("  ✓ Update agent tool call")

    # Update execution
    execution.status = ExecutionStatus.RUNNING
    execution.response = "Test response"
    updated_exec = await repo.update_agent_execution(execution)
    assert updated_exec.status == ExecutionStatus.RUNNING, "Execution should be updated"
    assert updated_exec.response == "Test response", "Response should be updated"
    print("  ✓ Update agent execution")

    # List executions by case
    executions, total = await repo.list_agent_executions_by_case(case_id)
    assert total == 1, "Should have 1 execution"
    assert len(executions) == 1, "Should return 1 execution"
    print("  ✓ List executions by case")

    # Filter by status
    executions_filtered, total_filtered = await repo.list_agent_executions_by_case(
        case_id, status=ExecutionStatus.RUNNING
    )
    assert total_filtered == 1, "Should filter by status"
    print("  ✓ Filter by status")

    # Filter by agent_type
    executions_type, total_type = await repo.list_agent_executions_by_case(
        case_id, agent_type=AgentType.INVESTIGATOR
    )
    assert total_type == 1, "Should filter by agent_type"
    print("  ✓ Filter by agent_type")

    # Get latest execution
    latest = await repo.get_latest_agent_execution(case_id)
    assert latest is not None, "Should return latest execution"
    assert (
        latest.execution_id == execution.execution_id
    ), "Should return correct execution"
    print("  ✓ Get latest execution")

    # Count executions
    count = await repo.count_agent_executions_by_case(case_id)
    assert count == 1, "Should count 1 execution"
    print("  ✓ Count executions")

    # Delete execution (should cascade to tool calls)
    deleted = await repo.delete_agent_execution(execution.execution_id)
    assert deleted is True, "Should delete successfully"

    retrieved_after = await repo.get_agent_execution(execution.execution_id)
    assert retrieved_after is None, "Should not find deleted execution"

    tool_calls_after = await repo.get_agent_tool_calls_for_execution(
        execution.execution_id
    )
    assert len(tool_calls_after) == 0, "Tool calls should be deleted (cascade)"
    print("  ✓ Delete execution (cascade to tool calls)")

    print("  ✅ All agent execution tests passed!\n")


async def test_enum_filtering():
    """Test that enum filtering works correctly."""
    print("Test 3: Enum Filtering")
    repo = InMemoryCaseRepository()

    case_id = generate_case_id()

    # Create executions with different statuses
    exec1 = AgentExecution(
        execution_id=generate_execution_id(),
        case_id=case_id,
        agent_type=AgentType.INVESTIGATOR,
        agent_model="gpt-4",
        status=ExecutionStatus.COMPLETED,
        prompt="Test 1",
        tool_calls=[],
    )

    exec2 = AgentExecution(
        execution_id=generate_execution_id(),
        case_id=case_id,
        agent_type=AgentType.DEBUGGER,
        agent_model="gpt-4",
        status=ExecutionStatus.FAILED,
        prompt="Test 2",
        tool_calls=[],
    )

    await repo.create_agent_execution(exec1)
    await repo.create_agent_execution(exec2)

    # Filter by enum status
    completed, total_completed = await repo.list_agent_executions_by_case(
        case_id, status=ExecutionStatus.COMPLETED
    )
    assert total_completed == 1, "Should filter by ExecutionStatus enum"
    assert (
        completed[0].execution_id == exec1.execution_id
    ), "Should return correct execution"
    print("  ✓ Filter by ExecutionStatus enum")

    # Filter by enum agent_type
    investigator, total_investigator = await repo.list_agent_executions_by_case(
        case_id, agent_type=AgentType.INVESTIGATOR
    )
    assert total_investigator == 1, "Should filter by AgentType enum"
    assert (
        investigator[0].execution_id == exec1.execution_id
    ), "Should return correct execution"
    print("  ✓ Filter by AgentType enum")

    # Get latest with enum filter
    latest_investigator = await repo.get_latest_agent_execution(
        case_id, agent_type=AgentType.INVESTIGATOR
    )
    assert latest_investigator is not None, "Should return latest with enum filter"
    assert (
        latest_investigator.agent_type == AgentType.INVESTIGATOR
    ), "Should filter correctly"
    print("  ✓ Get latest with AgentType enum filter")

    print("  ✅ All enum filtering tests passed!\n")


async def run_all_tests():
    """Run all integration tests."""
    print("=" * 60)
    print("Integration Tests for New CaseRepository Features")
    print("=" * 60)
    print()

    try:
        await test_standalone_evidence_workflow()
        await test_agent_execution_workflow()
        await test_enum_filtering()

        print("=" * 60)
        print("✅ ALL TESTS PASSED!")
        print("=" * 60)
        return True
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback

        traceback.print_exc()
        return False
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
