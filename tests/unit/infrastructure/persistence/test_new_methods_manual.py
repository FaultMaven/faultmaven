"""Manual test script for new CaseRepository methods.

This can be run without pytest to validate basic functionality.
Run with: python3 tests/unit/infrastructure/persistence/test_new_methods_manual.py
"""

import asyncio
import sys
from uuid import UUID, uuid4

# Add project root to path
sys.path.insert(0, "/home/swhouse/product/faultmaven")

from faultmaven.modules.agent.domain.models.agent_execution import (
    AgentExecution,
    AgentToolCall,
    AgentType,
    ExecutionStatus,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.evidence.domain.models import EvidenceListFilter


async def test_standalone_evidence():
    """Test standalone evidence methods."""
    print("\n=== Testing Standalone Evidence Methods ===")
    repo = InMemoryCaseRepository()

    # Create standalone evidence
    uploaded_by = str(uuid4())
    evidence = await repo.create_standalone_evidence(
        filename="test.png",
        content_type="image/png",
        size_bytes=1024,
        storage_path="/evidence/test.png",
        uploaded_by=uploaded_by,
        description="Test evidence",
        tags=["test", "screenshot"],
    )
    print(f"✅ Created evidence: {evidence.evidence_id}")
    assert evidence.original_filename == "test.png"
    assert evidence.evidence_type.value == "screenshot"

    # Get standalone evidence
    retrieved = await repo.get_standalone_evidence(evidence.evidence_id)
    print(f"✅ Retrieved evidence: {retrieved.evidence_id}")
    assert retrieved is not None
    assert retrieved.evidence_id == evidence.evidence_id

    # List all evidence
    filters = EvidenceListFilter()
    results, total = await repo.list_standalone_evidence(filters)
    print(f"✅ Listed evidence: {total} total")
    assert total >= 1

    # Filter by tags
    filters = EvidenceListFilter(tags=["test"])
    results, total = await repo.list_standalone_evidence(filters)
    print(f"✅ Filtered by tags: {total} results")
    assert total >= 1
    assert all("test" in e.tags for e in results)

    # Link to case
    case_id = str(uuid4())
    linked = await repo.link_standalone_evidence_to_case(evidence.evidence_id, case_id)
    print(f"✅ Linked evidence to case: {case_id}")
    assert linked is not None
    assert case_id in linked.linked_case_ids

    # Filter by case_id (EvidenceListFilter expects str, not UUID)
    filters = EvidenceListFilter(case_id=case_id)
    results, total = await repo.list_standalone_evidence(filters)
    print(f"✅ Filtered by case_id (UUID): {total} results")
    # Should find the linked evidence
    evidence_ids = {e.evidence_id for e in results}
    assert (
        evidence.evidence_id in evidence_ids
    ), f"Expected {evidence.evidence_id} in results"

    # Delete evidence
    deleted = await repo.delete_standalone_evidence(evidence.evidence_id)
    print(f"✅ Deleted evidence: {deleted}")
    assert deleted is True

    retrieved_after_delete = await repo.get_standalone_evidence(evidence.evidence_id)
    assert retrieved_after_delete is None
    print("✅ Evidence deleted successfully")

    print("✅ All standalone evidence tests passed!\n")


async def test_agent_executions():
    """Test agent execution methods."""
    print("\n=== Testing Agent Execution Methods ===")
    repo = InMemoryCaseRepository()

    # Create agent execution
    execution = AgentExecution(
        execution_id=f"exec_{uuid4().hex[:12]}",
        case_id=f"case_{uuid4().hex[:12]}",
        agent_type=AgentType.INVESTIGATOR,
        agent_model="gpt-4",
        status=ExecutionStatus.QUEUED,
        prompt="Test prompt",
        tool_calls=[],
    )

    created = await repo.create_agent_execution(execution)
    print(f"✅ Created execution: {created.execution_id}")
    assert created.execution_id == execution.execution_id
    assert created.status == ExecutionStatus.QUEUED

    # Get execution
    retrieved = await repo.get_agent_execution(created.execution_id)
    print(f"✅ Retrieved execution: {retrieved.execution_id}")
    assert retrieved is not None
    assert retrieved.tool_calls == []

    # Create tool call
    tool_call = AgentToolCall(
        tool_call_id=f"tc_{uuid4().hex[:12]}",
        execution_id=created.execution_id,
        tool_name="web_search",
        tool_input={"query": "test"},
        status="pending",
    )
    created_tc = await repo.create_agent_tool_call(tool_call)
    print(f"✅ Created tool call: {created_tc.tool_call_id}")

    # Get execution with tool calls
    retrieved_with_tc = await repo.get_agent_execution(created.execution_id)
    print(
        f"✅ Retrieved execution with tool calls: {len(retrieved_with_tc.tool_calls)} tool calls"
    )
    assert len(retrieved_with_tc.tool_calls) == 1
    assert retrieved_with_tc.tool_calls[0].tool_call_id == tool_call.tool_call_id

    # List executions by case
    results, total = await repo.list_agent_executions_by_case(execution.case_id)
    print(f"✅ Listed executions by case: {total} total")
    assert total >= 1

    # Filter by status (enum)
    results, total = await repo.list_agent_executions_by_case(
        execution.case_id, status=ExecutionStatus.QUEUED
    )
    print(f"✅ Filtered by status (enum): {total} results")
    assert total >= 1
    assert all(e.status == ExecutionStatus.QUEUED for e in results)

    # Filter by agent_type (enum)
    results, total = await repo.list_agent_executions_by_case(
        execution.case_id, agent_type=AgentType.INVESTIGATOR
    )
    print(f"✅ Filtered by agent_type (enum): {total} results")
    assert total >= 1
    assert all(e.agent_type == AgentType.INVESTIGATOR for e in results)

    # Update execution
    created.status = ExecutionStatus.COMPLETED
    created.response = "Done"
    updated = await repo.update_agent_execution(created)
    print(f"✅ Updated execution: {updated.status}")
    assert updated.status == ExecutionStatus.COMPLETED
    assert len(updated.tool_calls) == 1  # Tool calls preserved

    # Count executions
    count = await repo.count_agent_executions_by_case(execution.case_id)
    print(f"✅ Counted executions: {count}")
    assert count >= 1

    # Get latest execution
    latest = await repo.get_latest_agent_execution(execution.case_id)
    print(f"✅ Got latest execution: {latest.execution_id}")
    assert latest is not None
    assert latest.execution_id == created.execution_id

    # Delete execution (should cascade to tool calls)
    deleted = await repo.delete_agent_execution(created.execution_id)
    print(f"✅ Deleted execution: {deleted}")
    assert deleted is True

    # Verify execution and tool calls are deleted
    retrieved_after_delete = await repo.get_agent_execution(created.execution_id)
    assert retrieved_after_delete is None

    tool_calls_after_delete = await repo.get_agent_tool_calls_for_execution(
        created.execution_id
    )
    assert len(tool_calls_after_delete) == 0
    print("✅ Execution and tool calls deleted (cascade)")

    print("✅ All agent execution tests passed!\n")


async def main():
    """Run all manual tests."""
    try:
        await test_standalone_evidence()
        await test_agent_executions()
        print("🎉 All manual tests passed!")
        return 0
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        return 1
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
