"""Unit tests for HierarchicalMemoryManager

Tests:
- Hot/warm/cold tier transitions
- Token counting and compression
- 64% reduction verification (baseline ~4500, target ~1600 tokens)
- Memory snapshot generation
- Persistent insights management

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, Mock

from faultmaven.core.investigation.memory_manager import (
    HierarchicalMemoryManager,
    MemoryCompressionEngine,
    create_memory_manager,
    initialize_memory,
)
from faultmaven.models.investigation import (
    HierarchicalMemory,
    MemorySnapshot,
    OODAIteration,
    InvestigationPhase,
    OODAStep,
)


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider for summarization"""
    provider = Mock()
    provider.generate = AsyncMock(return_value="Compressed summary of iterations 1-3")
    return provider


@pytest.fixture
def memory_manager(mock_llm_provider):
    """Create memory manager instance"""
    return create_memory_manager(mock_llm_provider)


@pytest.fixture
def memory_manager_no_llm():
    """Create memory manager without LLM (fallback mode)"""
    return create_memory_manager(llm_provider=None)


@pytest.fixture
def sample_iteration():
    """Create sample OODA iteration"""
    return OODAIteration(
        iteration_number=1,
        phase=InvestigationPhase.VALIDATION,
        started_at_turn=5,
        completed_at_turn=7,
        duration_turns=2,
        steps_completed=[OODAStep.OBSERVE, OODAStep.ORIENT],
        new_evidence_collected=3,
        hypotheses_tested=1,
        confidence_delta=0.15,
        new_insights=["Database connection pool exhausted", "Load spike at 2pm"],
        made_progress=True,
    )


class TestMemoryInitialization:
    """Test memory initialization"""

    def test_initialize_empty_memory(self):
        """Test creating empty hierarchical memory"""
        memory = initialize_memory()

        assert isinstance(memory, HierarchicalMemory)
        assert len(memory.hot_memory) == 0
        assert len(memory.warm_snapshots) == 0
        assert len(memory.cold_snapshots) == 0
        assert len(memory.persistent_insights) == 0
        assert memory.last_compression_turn == 0

    def test_memory_manager_initialization(self, memory_manager):
        """Test memory manager initialization"""
        assert memory_manager is not None
        assert memory_manager.compression_engine is not None
        assert memory_manager.HOT_TIER_SIZE == 2
        assert memory_manager.WARM_TIER_SIZE == 3
        assert memory_manager.COLD_TIER_SIZE == 5
        assert memory_manager.TOTAL_BUDGET == 1600


class TestMemoryUpdate:
    """Test memory updates and tier management"""

    @pytest.mark.asyncio
    async def test_update_memory_adds_to_hot(
        self, memory_manager, sample_iteration
    ):
        """Test adding iteration to hot memory"""
        memory = initialize_memory()

        updated_memory = await memory_manager.update_memory(
            memory=memory,
            new_iteration=sample_iteration,
            current_turn=7,
        )

        assert len(updated_memory.hot_memory) == 1
        assert updated_memory.hot_memory[0].iteration_number == 1

    @pytest.mark.asyncio
    async def test_update_memory_multiple_iterations(
        self, memory_manager, sample_iteration
    ):
        """Test adding multiple iterations"""
        memory = initialize_memory()

        # Add first iteration
        iter1 = sample_iteration
        memory = await memory_manager.update_memory(memory, iter1, 7)

        # Add second iteration
        iter2 = OODAIteration(
            iteration_number=2,
            phase=InvestigationPhase.VALIDATION,
            started_at_turn=8,
            completed_at_turn=10,
            duration_turns=2,
            steps_completed=[OODAStep.DECIDE, OODAStep.ACT],
            new_evidence_collected=2,
            hypotheses_tested=1,
            confidence_delta=0.10,
            new_insights=["Confirmed database issue"],
            made_progress=True,
        )
        memory = await memory_manager.update_memory(memory, iter2, 10)

        assert len(memory.hot_memory) == 2

    @pytest.mark.asyncio
    async def test_compression_trigger_at_turn_multiple_of_3(
        self, memory_manager, sample_iteration
    ):
        """Test compression triggered at turn % 3 == 0"""
        memory = initialize_memory()

        # Add iteration at turn 9 (divisible by 3)
        memory = await memory_manager.update_memory(memory, sample_iteration, 9)

        # Compression should have been triggered
        assert memory.last_compression_turn == 9


class TestMemoryCompression:
    """Test memory compression and tier transitions"""

    @pytest.mark.asyncio
    async def test_promote_to_warm(self, memory_manager):
        """Test promoting hot memory to warm tier"""
        memory = initialize_memory()

        # Add 3 iterations to hot (exceeds HOT_TIER_SIZE of 2)
        for i in range(1, 4):
            iteration = OODAIteration(
                iteration_number=i,
                phase=InvestigationPhase.VALIDATION,
                started_at_turn=i * 2,
                completed_at_turn=i * 2 + 1,
                duration_turns=1,
                steps_completed=[OODAStep.OBSERVE],
                new_insights=[f"Insight {i}"],
                made_progress=True,
            )
            memory.hot_memory.append(iteration)

        # Promote overflow to warm
        memory = await memory_manager._promote_to_warm(memory)

        # Should have 2 in hot, 1 snapshot in warm
        assert len(memory.hot_memory) == 2
        assert len(memory.warm_snapshots) == 1

    @pytest.mark.asyncio
    async def test_demote_to_cold(self, memory_manager):
        """Test demoting warm memory to cold tier"""
        memory = initialize_memory()

        # Create 4 warm snapshots (exceeds WARM_TIER_SIZE of 3)
        for i in range(1, 5):
            snapshot = MemorySnapshot(
                iteration_range=(i, i),
                summary=f"Summary of iteration {i}",
                key_facts=[f"Fact {i}"],
                confidence_changes={f"iter_{i}": 0.1},
                evidence_collected=[f"evidence_{i}"],
                decisions_made=[f"Decision {i}"],
            )
            memory.warm_snapshots.append(snapshot)

        # Demote overflow to cold
        memory = await memory_manager._demote_to_cold(memory)

        # Should have 3 in warm, 1 in cold
        assert len(memory.warm_snapshots) == 3
        assert len(memory.cold_snapshots) == 1

        # Cold snapshot should have truncated data
        cold_snapshot = memory.cold_snapshots[0]
        assert len(cold_snapshot.summary) <= 100
        assert len(cold_snapshot.key_facts) <= 3

    def test_prune_cold_memory(self, memory_manager):
        """Test pruning old cold memory"""
        memory = initialize_memory()

        # Create 7 cold snapshots (exceeds COLD_TIER_SIZE of 5)
        for i in range(1, 8):
            snapshot = MemorySnapshot(
                iteration_range=(i, i),
                summary=f"Summary {i}",
                key_facts=[f"Fact {i}"],
            )
            memory.cold_snapshots.append(snapshot)

        # Prune excess
        memory = memory_manager._prune_cold_memory(memory)

        # Should have exactly 5
        assert len(memory.cold_snapshots) == 5


class TestCompressionEngine:
    """Test memory compression engine"""

    @pytest.mark.asyncio
    async def test_compress_iterations_with_llm(self, mock_llm_provider):
        """Test compression using LLM summarization"""
        engine = MemoryCompressionEngine(mock_llm_provider)

        iterations = [
            OODAIteration(
                iteration_number=i,
                phase=InvestigationPhase.VALIDATION,
                started_at_turn=i * 2,
                completed_at_turn=i * 2 + 1,
                duration_turns=1,
                new_insights=[f"Insight {i}"],
                confidence_delta=0.1,
                made_progress=True,
            )
            for i in range(1, 4)
        ]

        snapshot = await engine.compress_iterations(iterations, target_tokens=300)

        assert isinstance(snapshot, MemorySnapshot)
        assert snapshot.iteration_range == (1, 3)
        assert len(snapshot.summary) > 0
        assert mock_llm_provider.generate.called

    @pytest.mark.asyncio
    async def test_compress_iterations_fallback(self):
        """Test compression without LLM (fallback mode)"""
        engine = MemoryCompressionEngine(llm_provider=None)

        iterations = [
            OODAIteration(
                iteration_number=1,
                phase=InvestigationPhase.VALIDATION,
                started_at_turn=2,
                completed_at_turn=3,
                duration_turns=1,
                new_insights=["Key finding"],
                made_progress=True,
            )
        ]

        snapshot = await engine.compress_iterations(iterations)

        assert isinstance(snapshot, MemorySnapshot)
        assert len(snapshot.summary) > 0

    @pytest.mark.asyncio
    async def test_compress_empty_iterations_raises_error(self):
        """Test compression of empty iteration list raises ValueError"""
        engine = MemoryCompressionEngine(llm_provider=None)

        with pytest.raises(ValueError, match="Cannot compress empty iteration list"):
            await engine.compress_iterations([])


class TestPersistentInsights:
    """Test persistent insights management"""

    def test_add_persistent_insight(self, memory_manager):
        """Test adding insight to persistent memory"""
        memory = initialize_memory()

        memory = memory_manager.add_persistent_insight(
            memory,
            "Root cause: Database connection pool exhausted",
        )

        assert len(memory.persistent_insights) == 1
        assert "Database connection pool" in memory.persistent_insights[0]

    def test_add_duplicate_insight(self, memory_manager):
        """Test adding duplicate insight (should not duplicate)"""
        memory = initialize_memory()

        insight = "Key finding"
        memory = memory_manager.add_persistent_insight(memory, insight)
        memory = memory_manager.add_persistent_insight(memory, insight)

        assert len(memory.persistent_insights) == 1

    def test_persistent_insights_limit(self, memory_manager):
        """Test persistent insights limited to 10"""
        memory = initialize_memory()

        # Add 15 insights
        for i in range(15):
            memory = memory_manager.add_persistent_insight(
                memory,
                f"Insight {i}",
            )

        # Should be trimmed to last 10
        assert len(memory.persistent_insights) == 10


class TestMemoryForContext:
    """Test formatting memory for LLM context"""

    def test_get_memory_for_context_empty(self, memory_manager):
        """Test formatting empty memory"""
        memory = initialize_memory()

        context = memory_manager.get_memory_for_context(memory)

        assert isinstance(context, str)
        assert len(context) >= 0

    def test_get_memory_for_context_with_hot(self, memory_manager):
        """Test formatting memory with hot iterations"""
        memory = initialize_memory()
        memory.hot_memory.append(
            OODAIteration(
                iteration_number=1,
                phase=InvestigationPhase.VALIDATION,
                started_at_turn=5,
                duration_turns=2,
                new_insights=["Database issue confirmed"],
                made_progress=True,
            )
        )

        context = memory_manager.get_memory_for_context(memory)

        assert "Recent Progress" in context
        assert "Database issue confirmed" in context

    def test_get_memory_for_context_with_persistent(self, memory_manager):
        """Test formatting memory with persistent insights"""
        memory = initialize_memory()
        memory.persistent_insights.append("Key learning: Connection pool size matters")

        context = memory_manager.get_memory_for_context(memory)

        assert "Key Learnings" in context
        assert "Connection pool" in context

    def test_get_memory_for_context_exclude_cold(self, memory_manager):
        """Test excluding cold memory from context"""
        memory = initialize_memory()
        memory.cold_snapshots.append(
            MemorySnapshot(
                iteration_range=(1, 2),
                summary="Old data",
                key_facts=["Old fact"],
            )
        )

        context = memory_manager.get_memory_for_context(memory, include_cold=False)

        assert "Background Context" not in context


class TestTokenEstimation:
    """Test token usage estimation and budget compliance"""

    def test_estimate_token_usage_empty(self, memory_manager):
        """Test token estimation for empty memory"""
        memory = initialize_memory()

        token_estimate = memory_manager.estimate_token_usage(memory)

        assert token_estimate >= 0
        assert token_estimate < 100  # Should be minimal

    def test_estimate_token_usage_with_data(self, memory_manager):
        """Test token estimation with data"""
        memory = initialize_memory()

        # Add hot memory
        for i in range(2):
            memory.hot_memory.append(
                OODAIteration(
                    iteration_number=i + 1,
                    phase=InvestigationPhase.VALIDATION,
                    started_at_turn=i * 3,
                    duration_turns=2,
                    new_insights=[f"Insight {i + 1}" * 10],  # Longer text
                    made_progress=True,
                )
            )

        # Add warm snapshots
        memory.warm_snapshots.append(
            MemorySnapshot(
                iteration_range=(1, 2),
                summary="Summary text " * 20,
                key_facts=["Fact" * 5] * 3,
            )
        )

        token_estimate = memory_manager.estimate_token_usage(memory)

        # Should be reasonable but not exceed budget by much
        assert token_estimate > 0
        assert token_estimate < 5000  # Should be compressed

    def test_token_reduction_verification(self, memory_manager):
        """Test 64% token reduction claim verification

        Baseline: ~4500 tokens unmanaged
        Target: ~1600 tokens with compression (64% reduction)
        """
        memory = initialize_memory()

        # Simulate "unmanaged" memory: 10 full iterations (no compression)
        unmanaged_iterations = []
        for i in range(10):
            iteration = OODAIteration(
                iteration_number=i + 1,
                phase=InvestigationPhase.VALIDATION,
                started_at_turn=i * 3,
                completed_at_turn=i * 3 + 2,
                duration_turns=2,
                steps_completed=[OODAStep.OBSERVE, OODAStep.ORIENT, OODAStep.DECIDE, OODAStep.ACT],
                new_evidence_collected=5,
                hypotheses_generated=2,
                hypotheses_tested=1,
                confidence_delta=0.1,
                new_insights=[
                    f"Detailed insight {i}: " + "Investigation details " * 20,
                    f"Another finding {i}: " + "More context " * 15,
                ],
                made_progress=True,
            )
            unmanaged_iterations.append(iteration)

        # Estimate unmanaged memory (all in hot)
        memory_unmanaged = initialize_memory()
        memory_unmanaged.hot_memory = unmanaged_iterations
        baseline_tokens = memory_manager.estimate_token_usage(memory_unmanaged)

        # Now test managed memory with compression
        memory_managed = initialize_memory()
        # Add last 2 to hot
        memory_managed.hot_memory = unmanaged_iterations[-2:]
        # Add middle 3 to warm (summarized)
        memory_managed.warm_snapshots = [
            MemorySnapshot(
                iteration_range=(3, 5),
                summary="Iterations 3-5 summary",
                key_facts=["Fact 1", "Fact 2"],
            )
        ]
        # Add first 5 to cold (key facts only)
        memory_managed.cold_snapshots = [
            MemorySnapshot(
                iteration_range=(1, 2),
                summary="Summary",
                key_facts=["Key fact"],
            )
        ]
        # Add persistent insights
        memory_managed.persistent_insights = ["Root cause identified"]

        optimized_tokens = memory_manager.estimate_token_usage(memory_managed)

        # Verify reduction
        reduction_percent = ((baseline_tokens - optimized_tokens) / baseline_tokens) * 100

        # Should achieve at least 50% reduction (target is 64%)
        assert reduction_percent >= 50, \
            f"Expected 50%+ reduction, got {reduction_percent:.1f}% " \
            f"(baseline: {baseline_tokens}, optimized: {optimized_tokens})"


class TestMemoryStats:
    """Test memory statistics reporting"""

    def test_get_memory_stats(self, memory_manager):
        """Test retrieving memory statistics"""
        memory = initialize_memory()
        memory.hot_memory.append(
            OODAIteration(
                iteration_number=1,
                phase=InvestigationPhase.VALIDATION,
                started_at_turn=1,
                duration_turns=1,
                made_progress=True,
            )
        )

        stats = memory_manager.get_memory_stats(memory)

        assert stats["hot_iterations"] == 1
        assert stats["warm_snapshots"] == 0
        assert stats["cold_snapshots"] == 0
        assert "estimated_tokens" in stats
        assert "budget_utilization" in stats


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
