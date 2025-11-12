"""Hierarchical Memory Manager - Token-Optimized Investigation Memory

This module implements the hierarchical memory system for FaultMaven's
investigation framework, providing hot/warm/cold memory tiers to optimize
token usage while maintaining investigation context.

Design Reference: docs/architecture/investigation-phases-and-ooda-integration.md

Token Budget: ~1,600 tokens total (vs 4,500+ unmanaged, 64% reduction)
- Hot Memory: ~500 tokens (last 2 iterations, full fidelity)
- Warm Memory: ~300 tokens (iterations 3-5, summarized)
- Cold Memory: ~100 tokens (older, key facts only)
- Persistent Insights: ~100 tokens (always accessible)

Compression Strategy:
- Triggered every 3 turns (64% reduction)
- LLM-powered warm memory summarization
- Graduated TTL in Redis storage
- Automatic promotion/demotion between tiers
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import json

from faultmaven.models.investigation import (
    HierarchicalMemory,
    MemorySnapshot,
    OODAIteration,
    InvestigationState,
)


logger = logging.getLogger(__name__)


# =============================================================================
# Memory Compression Engine
# =============================================================================


class MemoryCompressionEngine:
    """Compresses investigation memory using LLM-powered summarization

    Compression process:
    1. Select iterations to compress (older than 2 iterations back)
    2. Extract key facts, decisions, and confidence changes
    3. Use LLM to generate concise summary
    4. Create MemorySnapshot
    5. Move to appropriate tier (warm/cold)
    """

    def __init__(self, llm_provider=None):
        """Initialize compression engine

        Args:
            llm_provider: Optional LLM provider for summarization
        """
        self.llm_provider = llm_provider
        self.logger = logging.getLogger(__name__)

    async def compress_iterations(
        self,
        iterations: List[OODAIteration],
        target_tokens: int = 300,
    ) -> MemorySnapshot:
        """Compress multiple OODA iterations into snapshot

        Args:
            iterations: List of iterations to compress
            target_tokens: Target token count for summary (~300 for warm)

        Returns:
            Compressed MemorySnapshot
        """
        if not iterations:
            raise ValueError("Cannot compress empty iteration list")

        iteration_range = (
            iterations[0].iteration_number,
            iterations[-1].iteration_number,
        )

        # Extract key information
        key_facts = []
        decisions = []
        confidence_changes = {}
        evidence_ids = []

        for iteration in iterations:
            # Extract new insights
            key_facts.extend(iteration.new_insights)

            # Track confidence changes
            if iteration.confidence_delta != 0:
                confidence_changes[f"iter_{iteration.iteration_number}"] = iteration.confidence_delta

            # Collect evidence
            if iteration.new_evidence_collected > 0:
                evidence_ids.append(f"evidence_in_iter_{iteration.iteration_number}")

            # Track decisions
            if iteration.steps_completed:
                decisions.append(
                    f"Iter {iteration.iteration_number}: "
                    f"{len(iteration.steps_completed)} steps completed"
                )

        # Generate LLM summary if provider available
        if self.llm_provider:
            summary = await self._generate_llm_summary(
                iterations, key_facts, decisions, target_tokens
            )
        else:
            # Fallback: Simple concatenation
            summary = self._generate_simple_summary(iterations, key_facts, decisions)

        snapshot = MemorySnapshot(
            iteration_range=iteration_range,
            summary=summary,
            key_facts=key_facts[:5],  # Keep top 5 facts
            confidence_changes=confidence_changes,
            evidence_collected=evidence_ids,
            decisions_made=decisions,
        )

        self.logger.info(
            f"Compressed iterations {iteration_range[0]}-{iteration_range[1]} "
            f"into snapshot: {len(summary)} chars, {len(key_facts)} facts"
        )

        return snapshot

    async def _generate_llm_summary(
        self,
        iterations: List[OODAIteration],
        key_facts: List[str],
        decisions: List[str],
        target_tokens: int,
    ) -> str:
        """Generate LLM-powered summary of iterations

        Args:
            iterations: Iterations to summarize
            key_facts: Extracted key facts
            decisions: Decisions made
            target_tokens: Target token count

        Returns:
            Concise summary string
        """
        # Build prompt for LLM
        prompt = f"""Summarize these investigation iterations concisely (target: {target_tokens} tokens):

Iterations: {iterations[0].iteration_number} to {iterations[-1].iteration_number}
Key Facts: {json.dumps(key_facts, indent=2)}
Decisions: {json.dumps(decisions, indent=2)}

Provide a 2-3 sentence summary focusing on:
1. What evidence was collected
2. How hypotheses evolved
3. Key decisions or findings

Summary:"""

        try:
            # Call LLM (simplified - actual implementation would use proper LLM service)
            response = await self.llm_provider.generate(
                prompt=prompt,
                max_tokens=target_tokens // 2,  # Approximate conversion
                temperature=0.3,  # Low temperature for factual summary
            )
            return response.strip()
        except Exception as e:
            self.logger.error(f"LLM summarization failed: {e}, using fallback")
            return self._generate_simple_summary(iterations, key_facts, decisions)

    def _generate_simple_summary(
        self,
        iterations: List[OODAIteration],
        key_facts: List[str],
        decisions: List[str],
    ) -> str:
        """Generate simple summary without LLM

        Args:
            iterations: Iterations to summarize
            key_facts: Extracted key facts
            decisions: Decisions made

        Returns:
            Simple concatenated summary
        """
        parts = []

        # Summarize progress
        progress_count = sum(1 for i in iterations if i.made_progress)
        parts.append(
            f"Iterations {iterations[0].iteration_number}-{iterations[-1].iteration_number}: "
            f"{progress_count}/{len(iterations)} made progress."
        )

        # Add top facts
        if key_facts:
            parts.append(f"Key findings: {'; '.join(key_facts[:3])}")

        # Add major decisions
        if decisions:
            parts.append(f"Actions: {'; '.join(decisions[:2])}")

        return " ".join(parts)


# =============================================================================
# Hierarchical Memory Manager
# =============================================================================


class HierarchicalMemoryManager:
    """Manages token-optimized hierarchical memory system

    Memory Tiers:
    - Hot: Last 2 iterations, full fidelity (configurable via HOT_MEMORY_TOKENS)
    - Warm: Iterations 3-5, summarized (configurable via WARM_MEMORY_TOKENS)
    - Cold: Older iterations, key facts only (configurable via COLD_MEMORY_TOKENS)
    - Persistent: Always-available insights (configurable via PERSISTENT_MEMORY_TOKENS)

    Total budget: Configurable, recommended ~1,600 tokens (64% reduction from unmanaged ~4,500)
    """

    # Default token budget allocation (fallbacks if settings not provided)
    DEFAULT_HOT_MEMORY_TOKENS = 500
    DEFAULT_WARM_MEMORY_TOKENS = 300
    DEFAULT_COLD_MEMORY_TOKENS = 100
    DEFAULT_PERSISTENT_TOKENS = 100

    # Tier sizes
    HOT_TIER_SIZE = 2  # Last 2 iterations
    WARM_TIER_SIZE = 3  # Max 3 snapshots (iterations 3-5)
    COLD_TIER_SIZE = 5  # Max 5 snapshots (older iterations)

    def __init__(self, llm_provider=None, settings=None):
        """Initialize hierarchical memory manager

        Args:
            llm_provider: Optional LLM provider for summarization
            settings: Optional FaultMavenSettings instance for configuration
        """
        self.compression_engine = MemoryCompressionEngine(llm_provider)
        self.logger = logging.getLogger(__name__)
        
        # Load token budgets from settings or use defaults
        if settings and hasattr(settings, 'ooda'):
            self.hot_memory_tokens = settings.ooda.hot_memory_tokens
            self.warm_memory_tokens = settings.ooda.warm_memory_tokens
            self.cold_memory_tokens = settings.ooda.cold_memory_tokens
            self.persistent_tokens = settings.ooda.persistent_memory_tokens
        else:
            # Fallback to defaults
            self.hot_memory_tokens = self.DEFAULT_HOT_MEMORY_TOKENS
            self.warm_memory_tokens = self.DEFAULT_WARM_MEMORY_TOKENS
            self.cold_memory_tokens = self.DEFAULT_COLD_MEMORY_TOKENS
            self.persistent_tokens = self.DEFAULT_PERSISTENT_TOKENS
        
        # Calculate total budget
        self.total_budget = (self.hot_memory_tokens + self.warm_memory_tokens + 
                            self.cold_memory_tokens + self.persistent_tokens)
        
        self.logger.info(
            f"Memory Manager initialized with token budget: "
            f"HOT={self.hot_memory_tokens}, WARM={self.warm_memory_tokens}, "
            f"COLD={self.cold_memory_tokens}, PERSISTENT={self.persistent_tokens}, "
            f"TOTAL={self.total_budget}"
        )

    async def update_memory(
        self,
        memory: HierarchicalMemory,
        new_iteration: OODAIteration,
        current_turn: int,
    ) -> HierarchicalMemory:
        """Update memory with new iteration and compress if needed

        Args:
            memory: Current hierarchical memory
            new_iteration: New iteration to add
            current_turn: Current conversation turn

        Returns:
            Updated hierarchical memory
        """
        # Add to hot memory
        memory.hot_memory.append(new_iteration)

        # Check if compression needed
        if memory.should_compress(current_turn):
            self.logger.info(f"Compression triggered at turn {current_turn}")
            memory = await self._compress_memory(memory, current_turn)
            memory.last_compression_turn = current_turn

        # Trim hot memory if needed
        if len(memory.hot_memory) > self.HOT_TIER_SIZE:
            memory = await self._promote_to_warm(memory)

        return memory

    async def _compress_memory(
        self,
        memory: HierarchicalMemory,
        current_turn: int,
    ) -> HierarchicalMemory:
        """Compress memory tiers

        Process:
        1. Compress overflow from hot → warm
        2. Compress overflow from warm → cold
        3. Prune old cold snapshots

        Args:
            memory: Current memory
            current_turn: Current turn

        Returns:
            Compressed memory
        """
        self.logger.info("Starting memory compression")

        # Step 1: Compress hot overflow to warm
        if len(memory.hot_memory) > self.HOT_TIER_SIZE:
            memory = await self._promote_to_warm(memory)

        # Step 2: Compress warm overflow to cold
        if len(memory.warm_snapshots) > self.WARM_TIER_SIZE:
            memory = await self._demote_to_cold(memory)

        # Step 3: Prune old cold snapshots
        if len(memory.cold_snapshots) > self.COLD_TIER_SIZE:
            memory = self._prune_cold_memory(memory)

        self.logger.info(
            f"Compression complete: hot={len(memory.hot_memory)}, "
            f"warm={len(memory.warm_snapshots)}, cold={len(memory.cold_snapshots)}"
        )

        return memory

    async def _promote_to_warm(self, memory: HierarchicalMemory) -> HierarchicalMemory:
        """Promote oldest hot memory to warm tier

        Args:
            memory: Current memory

        Returns:
            Updated memory
        """
        # Take oldest iterations from hot
        overflow_count = len(memory.hot_memory) - self.HOT_TIER_SIZE
        if overflow_count <= 0:
            return memory

        iterations_to_compress = memory.hot_memory[:overflow_count]

        # Compress to snapshot
        snapshot = await self.compression_engine.compress_iterations(
            iterations_to_compress,
            target_tokens=self.warm_memory_tokens // self.WARM_TIER_SIZE,
        )

        # Add to warm tier
        memory.warm_snapshots.append(snapshot)

        # Remove from hot
        memory.hot_memory = memory.hot_memory[overflow_count:]

        self.logger.info(
            f"Promoted {overflow_count} iterations to warm memory "
            f"(iterations {snapshot.iteration_range[0]}-{snapshot.iteration_range[1]})"
        )

        return memory

    async def _demote_to_cold(self, memory: HierarchicalMemory) -> HierarchicalMemory:
        """Demote oldest warm memory to cold tier

        Args:
            memory: Current memory

        Returns:
            Updated memory
        """
        overflow_count = len(memory.warm_snapshots) - self.WARM_TIER_SIZE
        if overflow_count <= 0:
            return memory

        # Take oldest warm snapshots
        snapshots_to_demote = memory.warm_snapshots[:overflow_count]

        for snapshot in snapshots_to_demote:
            # Extract only key facts for cold storage
            cold_snapshot = MemorySnapshot(
                iteration_range=snapshot.iteration_range,
                summary=snapshot.summary[:100],  # Truncate summary
                key_facts=snapshot.key_facts[:3],  # Keep top 3 facts
                confidence_changes=snapshot.confidence_changes,
                evidence_collected=[],  # Drop evidence list in cold
                decisions_made=snapshot.decisions_made[:2],  # Keep top 2 decisions
            )
            memory.cold_snapshots.append(cold_snapshot)

        # Remove from warm
        memory.warm_snapshots = memory.warm_snapshots[overflow_count:]

        self.logger.info(f"Demoted {overflow_count} snapshots to cold memory")

        return memory

    def _prune_cold_memory(self, memory: HierarchicalMemory) -> HierarchicalMemory:
        """Prune oldest cold memory snapshots

        Args:
            memory: Current memory

        Returns:
            Updated memory
        """
        overflow_count = len(memory.cold_snapshots) - self.COLD_TIER_SIZE
        if overflow_count <= 0:
            return memory

        # Remove oldest cold snapshots
        pruned_snapshots = memory.cold_snapshots[:overflow_count]
        memory.cold_snapshots = memory.cold_snapshots[overflow_count:]

        self.logger.info(
            f"Pruned {overflow_count} old snapshots from cold memory "
            f"(iterations {pruned_snapshots[0].iteration_range[0]}-"
            f"{pruned_snapshots[-1].iteration_range[1]})"
        )

        return memory

    def add_persistent_insight(
        self,
        memory: HierarchicalMemory,
        insight: str,
    ) -> HierarchicalMemory:
        """Add insight to persistent memory

        Persistent insights are always available regardless of tier compression.
        Examples: validated root causes, key findings, critical decisions

        Args:
            memory: Current memory
            insight: Insight to persist

        Returns:
            Updated memory
        """
        if insight not in memory.persistent_insights:
            memory.persistent_insights.append(insight)
            self.logger.info(f"Added persistent insight: {insight[:50]}...")

        # Limit persistent insights to avoid token bloat
        if len(memory.persistent_insights) > 10:
            memory.persistent_insights = memory.persistent_insights[-10:]
            self.logger.warning("Trimmed persistent insights to last 10")

        return memory

    def get_memory_for_context(
        self,
        memory: HierarchicalMemory,
        include_cold: bool = True,
    ) -> str:
        """Get formatted memory for LLM context

        Args:
            memory: Hierarchical memory
            include_cold: Whether to include cold memory

        Returns:
            Formatted memory string for LLM context
        """
        parts = []

        # Persistent insights (always included)
        if memory.persistent_insights:
            parts.append("=== Key Learnings ===")
            parts.extend(f"• {insight}" for insight in memory.persistent_insights)
            parts.append("")

        # Hot memory (full fidelity)
        if memory.hot_memory:
            parts.append("=== Recent Progress ===")
            for iteration in memory.hot_memory:
                parts.append(
                    f"Iteration {iteration.iteration_number} "
                    f"({iteration.phase.name}, {iteration.duration_turns} turns):"
                )
                if iteration.new_insights:
                    parts.extend(f"  • {insight}" for insight in iteration.new_insights)
                if iteration.made_progress:
                    parts.append(f"  ✓ Progress made (Δconfidence: {iteration.confidence_delta:+.2f})")
                else:
                    parts.append(f"  ⚠ No progress in this iteration")
            parts.append("")

        # Warm memory (summarized)
        if memory.warm_snapshots:
            parts.append("=== Earlier Investigation ===")
            for snapshot in memory.warm_snapshots:
                parts.append(
                    f"Iterations {snapshot.iteration_range[0]}-{snapshot.iteration_range[1]}: "
                    f"{snapshot.summary}"
                )
            parts.append("")

        # Cold memory (key facts only)
        if include_cold and memory.cold_snapshots:
            parts.append("=== Background Context ===")
            all_cold_facts = []
            for snapshot in memory.cold_snapshots:
                all_cold_facts.extend(snapshot.key_facts)
            parts.extend(f"• {fact}" for fact in all_cold_facts[:5])  # Top 5 facts
            parts.append("")

        return "\n".join(parts)

    def estimate_token_usage(self, memory: HierarchicalMemory) -> int:
        """Estimate approximate token usage of memory

        Rough estimation: 1 token ≈ 4 characters

        Args:
            memory: Hierarchical memory

        Returns:
            Estimated token count
        """
        memory_str = self.get_memory_for_context(memory, include_cold=True)
        char_count = len(memory_str)
        token_estimate = char_count // 4  # Rough approximation

        return token_estimate

    def get_memory_stats(self, memory: HierarchicalMemory) -> Dict[str, Any]:
        """Get memory statistics for monitoring

        Args:
            memory: Hierarchical memory

        Returns:
            Statistics dictionary
        """
        return {
            "hot_iterations": len(memory.hot_memory),
            "warm_snapshots": len(memory.warm_snapshots),
            "cold_snapshots": len(memory.cold_snapshots),
            "persistent_insights": len(memory.persistent_insights),
            "estimated_tokens": self.estimate_token_usage(memory),
            "budget_utilization": f"{(self.estimate_token_usage(memory) / self.total_budget) * 100:.1f}%",
            "last_compression_turn": memory.last_compression_turn,
        }


# =============================================================================
# Utility Functions
# =============================================================================


def create_memory_manager(llm_provider=None) -> HierarchicalMemoryManager:
    """Factory function to create memory manager

    Args:
        llm_provider: Optional LLM provider for summarization

    Returns:
        Configured HierarchicalMemoryManager instance
    """
    return HierarchicalMemoryManager(llm_provider)


def initialize_memory() -> HierarchicalMemory:
    """Initialize empty hierarchical memory

    Returns:
        New HierarchicalMemory instance
    """
    return HierarchicalMemory()
