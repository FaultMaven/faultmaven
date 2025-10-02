"""
Unit tests for Token-Aware Context Manager

Tests the token-based context management system including:
- Token counting
- Context budget allocation
- Conversation summarization
- Token-aware context assembly
"""

import pytest
from datetime import datetime
from typing import List, Dict

from faultmaven.services.agentic.management.context_manager import (
    TokenCounter,
    ContextBudget,
    ConversationTurn,
    ConversationSummarizer,
    TokenAwareContextManager,
)


# =============================================================================
# Token Counter Tests
# =============================================================================

class TestTokenCounter:
    """Tests for TokenCounter utility"""

    def test_count_tokens_empty_string(self):
        """Test counting tokens in empty string"""
        assert TokenCounter.count_tokens("") == 0

    def test_count_tokens_simple_text(self):
        """Test counting tokens in simple text"""
        text = "Hello world"
        tokens = TokenCounter.count_tokens(text)
        # With 4 chars/token: 11 chars / 4 = 2.75 → 3 tokens
        assert tokens == 3

    def test_count_tokens_long_text(self):
        """Test counting tokens in longer text"""
        text = "This is a longer text message for testing token counting." * 10
        tokens = TokenCounter.count_tokens(text)
        # 580 chars / 4 = 145 tokens
        assert tokens > 140
        assert tokens < 150

    def test_count_tokens_multiline(self):
        """Test counting tokens in multiline text"""
        text = """Line 1
Line 2
Line 3"""
        tokens = TokenCounter.count_tokens(text)
        assert tokens > 0

    def test_estimate_tokens_batch(self):
        """Test batch token counting"""
        texts = ["Hello", "World", "Test message"]
        counts = TokenCounter.estimate_tokens_batch(texts)
        assert len(counts) == 3
        assert all(count > 0 for count in counts)


# =============================================================================
# Context Budget Tests
# =============================================================================

class TestContextBudget:
    """Tests for ContextBudget configuration"""

    def test_default_budget(self):
        """Test default budget allocation"""
        budget = ContextBudget()
        assert budget.max_total_tokens == 4000
        assert budget.reserved_for_recent == 2000
        assert budget.max_summary_tokens == 1500
        assert budget.min_recent_messages == 3

    def test_custom_budget(self):
        """Test custom budget configuration"""
        budget = ContextBudget(
            max_total_tokens=6000,
            reserved_for_recent=3000,
            max_summary_tokens=2500,
            min_recent_messages=5
        )
        assert budget.max_total_tokens == 6000
        assert budget.reserved_for_recent == 3000
        assert budget.max_summary_tokens == 2500
        assert budget.min_recent_messages == 5

    def test_available_for_summary_calculation(self):
        """Test available_for_summary property"""
        budget = ContextBudget(
            max_total_tokens=4000,
            reserved_for_recent=2000,
            max_summary_tokens=1500
        )
        # Should be min(1500, 4000 - 2000) = 1500
        assert budget.available_for_summary == 1500

    def test_available_for_summary_limited_by_total(self):
        """Test that available_for_summary respects total budget"""
        budget = ContextBudget(
            max_total_tokens=3000,
            reserved_for_recent=2000,
            max_summary_tokens=1500
        )
        # Should be min(1500, 3000 - 2000) = 1000
        assert budget.available_for_summary == 1000


# =============================================================================
# Conversation Turn Tests
# =============================================================================

class TestConversationTurn:
    """Tests for ConversationTurn dataclass"""

    def test_create_turn(self):
        """Test creating a conversation turn"""
        turn = ConversationTurn(
            timestamp=datetime(2025, 1, 1, 10, 30),
            role="user",
            content="Test message",
            tokens=3
        )
        assert turn.role == "user"
        assert turn.content == "Test message"
        assert turn.tokens == 3
        assert turn.timestamp.hour == 10

    def test_turn_with_long_content(self):
        """Test turn with longer content"""
        content = "This is a longer test message." * 10
        tokens = TokenCounter.count_tokens(content)
        turn = ConversationTurn(
            timestamp=datetime.utcnow(),
            role="assistant",
            content=content,
            tokens=tokens
        )
        assert turn.tokens > 50


# =============================================================================
# Conversation Summarizer Tests
# =============================================================================

class TestConversationSummarizer:
    """Tests for ConversationSummarizer"""

    @pytest.mark.asyncio
    async def test_extractive_summary_without_llm(self):
        """Test extractive summary fallback (no LLM)"""
        summarizer = ConversationSummarizer(llm_provider=None)

        turns = [
            ConversationTurn(
                timestamp=datetime.utcnow(),
                role="user",
                content="My application is crashing with OOMKilled errors",
                tokens=10
            ),
            ConversationTurn(
                timestamp=datetime.utcnow(),
                role="assistant",
                content="Let's check your memory limits",
                tokens=6
            ),
        ]

        summary = await summarizer.summarize_conversation(turns, max_tokens=500)

        assert len(summary) > 0
        assert "Problem:" in summary or "crashing" in summary

    @pytest.mark.asyncio
    async def test_extractive_summary_with_existing_summary(self):
        """Test building on existing summary"""
        summarizer = ConversationSummarizer(llm_provider=None)

        existing = "Previous: User reported database connection issues"
        turns = [
            ConversationTurn(
                timestamp=datetime.utcnow(),
                role="user",
                content="Now the application is also showing timeout errors",
                tokens=10
            ),
        ]

        summary = await summarizer.summarize_conversation(
            turns,
            max_tokens=500,
            existing_summary=existing
        )

        assert "Previous:" in summary or "database" in summary

    @pytest.mark.asyncio
    async def test_extractive_summary_truncates_to_max_tokens(self):
        """Test that extractive summary respects token limit"""
        summarizer = ConversationSummarizer(llm_provider=None)

        # Create many turns to exceed limit
        turns = [
            ConversationTurn(
                timestamp=datetime.utcnow(),
                role="user",
                content="Test message " * 100,  # Long content
                tokens=100
            )
            for _ in range(10)
        ]

        summary = await summarizer.summarize_conversation(turns, max_tokens=50)

        # Summary should be truncated
        summary_tokens = TokenCounter.count_tokens(summary)
        assert summary_tokens <= 60  # Allow 20% buffer

    def test_extractive_summary_private_method(self):
        """Test _extractive_summary private method"""
        summarizer = ConversationSummarizer()

        turns = [
            ConversationTurn(
                timestamp=datetime.utcnow(),
                role="user",
                content="Application crashed",
                tokens=5
            ),
            ConversationTurn(
                timestamp=datetime.utcnow(),
                role="assistant",
                content="Let me help diagnose",
                tokens=5
            ),
        ]

        summary = summarizer._extractive_summary(turns, max_tokens=100)

        assert len(summary) > 0
        assert "Problem:" in summary or "Application" in summary


# =============================================================================
# Token Aware Context Manager Tests
# =============================================================================

class TestTokenAwareContextManager:
    """Tests for TokenAwareContextManager"""

    def create_sample_history(self, num_messages: int) -> List[Dict]:
        """Helper to create sample conversation history"""
        history = []
        for i in range(num_messages):
            history.append({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"Message {i + 1}: This is test content",
                "timestamp": datetime.utcnow().isoformat()
            })
        return history

    @pytest.mark.asyncio
    async def test_build_context_empty_history(self):
        """Test building context with empty history"""
        manager = TokenAwareContextManager()
        context, metadata = await manager.build_context([])

        assert context == ""
        assert metadata["total_tokens"] == 0
        assert metadata["recent_message_count"] == 0

    @pytest.mark.asyncio
    async def test_build_context_short_conversation(self):
        """Test building context with short conversation (all messages fit)"""
        manager = TokenAwareContextManager(
            budget=ContextBudget(
                max_total_tokens=4000,
                reserved_for_recent=2000,
                min_recent_messages=3
            )
        )

        history = self.create_sample_history(5)
        context, metadata = await manager.build_context(history)

        assert len(context) > 0
        assert metadata["recent_message_count"] == 5
        assert metadata["truncated"] == False
        assert "Recent conversation:" in context

    @pytest.mark.asyncio
    async def test_build_context_long_conversation(self):
        """Test building context with long conversation (requires summarization)"""
        manager = TokenAwareContextManager(
            budget=ContextBudget(
                max_total_tokens=1000,  # Small budget to force summarization
                reserved_for_recent=500,
                max_summary_tokens=400,
                min_recent_messages=2
            )
        )

        # Create long history that exceeds budget
        history = []
        for i in range(20):
            history.append({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": "This is a longer message with more content. " * 10,
                "timestamp": datetime.utcnow().isoformat()
            })

        context, metadata = await manager.build_context(history)

        assert len(context) > 0
        assert metadata["truncated"] == True  # Older messages summarized
        assert metadata["recent_message_count"] >= 2  # At least min_recent_messages
        assert "Recent conversation:" in context

    @pytest.mark.asyncio
    async def test_build_context_with_case_title(self):
        """Test building context with case title"""
        manager = TokenAwareContextManager()

        history = self.create_sample_history(3)
        context, metadata = await manager.build_context(
            history,
            case_title="Production API Latency Issue"
        )

        assert "Troubleshooting Case: Production API Latency Issue" in context
        assert metadata["total_tokens"] > 0

    @pytest.mark.asyncio
    async def test_build_context_respects_token_budget(self):
        """Test that context respects token budget"""
        budget = ContextBudget(
            max_total_tokens=500,
            reserved_for_recent=250,
            max_summary_tokens=200,
            min_recent_messages=2
        )
        manager = TokenAwareContextManager(budget=budget)

        # Create messages that would exceed budget
        history = []
        for i in range(10):
            history.append({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": "Test message content. " * 50,  # Long messages
                "timestamp": datetime.utcnow().isoformat()
            })

        context, metadata = await manager.build_context(history)

        # Total tokens should be within reasonable range of budget
        # Allow 50% buffer for formatting overhead and minimum message requirement
        assert metadata["total_tokens"] <= budget.max_total_tokens * 1.5

    def test_parse_conversation_history(self):
        """Test _parse_conversation_history method"""
        manager = TokenAwareContextManager()

        history = [
            {"role": "user", "content": "Test message 1", "timestamp": "2025-01-01T10:00:00Z"},
            {"role": "assistant", "content": "Test response 1", "timestamp": "2025-01-01T10:01:00Z"},
        ]

        turns = manager._parse_conversation_history(history)

        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[0].content == "Test message 1"
        assert turns[0].tokens > 0
        assert turns[1].role == "assistant"

    def test_split_by_budget_minimum_recent(self):
        """Test that _split_by_budget respects min_recent_messages"""
        budget = ContextBudget(
            max_total_tokens=1000,
            reserved_for_recent=100,  # Very small budget
            min_recent_messages=3
        )
        manager = TokenAwareContextManager(budget=budget)

        # Create turns that would exceed budget
        turns = [
            ConversationTurn(
                timestamp=datetime.utcnow(),
                role="user",
                content=f"Message {i}",
                tokens=50
            )
            for i in range(10)
        ]

        recent_turns, older_turns = manager._split_by_budget(turns)

        # Should include at least min_recent_messages despite budget
        assert len(recent_turns) >= budget.min_recent_messages
        assert len(older_turns) == len(turns) - len(recent_turns)

    def test_split_by_budget_fills_budget(self):
        """Test that _split_by_budget uses available budget"""
        budget = ContextBudget(
            max_total_tokens=1000,
            reserved_for_recent=500,
            min_recent_messages=2
        )
        manager = TokenAwareContextManager(budget=budget)

        # Create turns with known token counts
        turns = [
            ConversationTurn(
                timestamp=datetime.utcnow(),
                role="user",
                content="msg",
                tokens=50
            )
            for _ in range(20)
        ]

        recent_turns, older_turns = manager._split_by_budget(turns)

        # Should include more than minimum since budget allows
        assert len(recent_turns) > budget.min_recent_messages

        # Total tokens in recent should be <= reserved_for_recent
        total_recent_tokens = sum(turn.tokens for turn in recent_turns)
        assert total_recent_tokens <= budget.reserved_for_recent

    def test_format_recent_messages(self):
        """Test _format_recent_messages method"""
        manager = TokenAwareContextManager()

        turns = [
            ConversationTurn(
                timestamp=datetime(2025, 1, 1, 10, 30),
                role="user",
                content="First message",
                tokens=5
            ),
            ConversationTurn(
                timestamp=datetime(2025, 1, 1, 10, 31),
                role="assistant",
                content="First response",
                tokens=5
            ),
        ]

        formatted = manager._format_recent_messages(turns)

        assert "Recent conversation:" in formatted
        assert "User: First message" in formatted
        assert "Assistant: First response" in formatted
        assert "1." in formatted
        assert "2." in formatted


# =============================================================================
# Integration Tests
# =============================================================================

class TestTokenAwareContextIntegration:
    """Integration tests for complete context building workflow"""

    @pytest.mark.asyncio
    async def test_end_to_end_short_conversation(self):
        """Test complete workflow with short conversation"""
        manager = TokenAwareContextManager()

        history = [
            {"role": "user", "content": "My app is crashing", "timestamp": "2025-01-01T10:00:00Z"},
            {"role": "assistant", "content": "Let's check the logs", "timestamp": "2025-01-01T10:01:00Z"},
            {"role": "user", "content": "Logs show OOMKilled", "timestamp": "2025-01-01T10:02:00Z"},
        ]

        context, metadata = await manager.build_context(
            history,
            case_title="Production Crash Investigation"
        )

        # Verify context structure
        assert "Troubleshooting Case:" in context
        assert "Recent conversation:" in context
        assert "My app is crashing" in context
        assert "OOMKilled" in context

        # Verify metadata
        assert metadata["total_tokens"] > 0
        assert metadata["recent_message_count"] == 3
        assert metadata["truncated"] == False

    @pytest.mark.asyncio
    async def test_end_to_end_long_conversation_with_summary(self):
        """Test complete workflow with long conversation requiring summary"""
        budget = ContextBudget(
            max_total_tokens=800,
            reserved_for_recent=400,
            max_summary_tokens=300,
            min_recent_messages=2
        )
        manager = TokenAwareContextManager(budget=budget)

        # Create 15-turn conversation
        history = []
        for i in range(15):
            history.append({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"Turn {i + 1}: This is message content with diagnostics and analysis. " * 5,
                "timestamp": f"2025-01-01T10:{i:02d}:00Z"
            })

        context, metadata = await manager.build_context(
            history,
            existing_summary="Previous investigation found memory leak in cache",
            case_title="Memory Leak Investigation"
        )

        # Verify context includes all parts
        assert "Troubleshooting Case:" in context
        assert "Recent conversation:" in context

        # Verify metadata
        assert metadata["truncated"] == True  # Should have summarized older messages
        assert metadata["recent_message_count"] >= 2
        assert metadata["total_tokens"] > 0
        assert metadata["total_tokens"] <= budget.max_total_tokens * 1.2  # Within budget (+20% buffer)


# =============================================================================
# Performance Tests
# =============================================================================

class TestContextManagerPerformance:
    """Performance tests for context management"""

    def test_token_counting_performance(self):
        """Test token counting is fast"""
        import time

        text = "This is a test message. " * 100
        iterations = 1000

        start = time.time()
        for _ in range(iterations):
            TokenCounter.count_tokens(text)
        elapsed = time.time() - start

        # Should complete 1000 iterations in under 50ms
        assert elapsed < 0.05, f"Token counting too slow: {elapsed}s for {iterations} iterations"

    @pytest.mark.asyncio
    async def test_context_building_performance(self):
        """Test context building is reasonably fast"""
        import time

        manager = TokenAwareContextManager()

        # Create moderate-sized history
        history = []
        for i in range(20):
            history.append({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"Message {i}: Some content here",
                "timestamp": datetime.utcnow().isoformat()
            })

        start = time.time()
        context, metadata = await manager.build_context(history)
        elapsed = time.time() - start

        # Should complete in under 100ms (without LLM summarization)
        assert elapsed < 0.1, f"Context building too slow: {elapsed}s"
