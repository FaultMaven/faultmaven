"""
Integration tests for token-aware context management.

Tests the integration of TokenAwareContextManager with:
- SessionService
- AgentService
- Real conversation flows
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Dict, List

from faultmaven.services.agentic.management.context_manager import (
    TokenAwareContextManager,
    ContextBudget,
    ConversationSummarizer,
)
from faultmaven.services.domain.session_service import SessionService
from faultmaven.models.case import CaseStatus


@pytest.mark.integration
@pytest.mark.asyncio
class TestTokenAwareContextIntegration:
    """Integration tests for token-aware context management."""

    async def test_session_service_token_aware_context(self):
        """Test SessionService using token-aware context formatting."""
        # Create mock dependencies
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_redis.set = MagicMock()
        mock_redis.expire = MagicMock()
        mock_redis.delete = MagicMock()
        mock_redis.keys.return_value = []

        mock_llm = AsyncMock()
        mock_llm.generate_text = AsyncMock(return_value="Summary: User troubleshooting issue.")

        # Create SessionService with mocked dependencies
        session_service = SessionService(
            redis_client=mock_redis,
            llm_provider=mock_llm
        )

        # Create a test session and case
        session_id = "test-session-123"
        case_id = "test-case-456"

        # Mock session and case data
        session_data = {
            "session_id": session_id,
            "username": "test_user",
            "created_at": datetime.utcnow().isoformat(),
            "last_activity": datetime.utcnow().isoformat(),
        }

        case_data = {
            "case_id": case_id,
            "session_id": session_id,
            "title": "Database connection timeout",
            "status": CaseStatus.OPEN.value,
            "conversation_history": [
                {
                    "role": "user",
                    "content": "My application cannot connect to the database",
                    "timestamp": datetime.utcnow().isoformat()
                },
                {
                    "role": "assistant",
                    "content": "Let me help you troubleshoot the database connection issue. Can you provide the error message?",
                    "timestamp": datetime.utcnow().isoformat()
                },
                {
                    "role": "user",
                    "content": "Error: Connection timeout after 30 seconds. Host: db.example.com:5432",
                    "timestamp": datetime.utcnow().isoformat()
                }
            ]
        }

        # Mock Redis responses
        def redis_get_side_effect(key):
            if f"session:{session_id}" in key:
                import json
                return json.dumps(session_data).encode()
            elif f"case:{case_id}" in key:
                import json
                return json.dumps(case_data).encode()
            return None

        mock_redis.get.side_effect = redis_get_side_effect

        # Test token-aware context formatting
        context, metadata = await session_service.format_conversation_context_token_aware(
            session_id=session_id,
            case_id=case_id,
            max_tokens=1000,
            enable_summarization=True
        )

        # Assertions
        assert len(context) > 0, "Context should not be empty"
        assert "Database connection timeout" in context, "Case title should be in context"
        assert "database" in context.lower(), "Context should contain conversation content"
        assert metadata["total_tokens"] <= 1000 * 1.5, "Should respect token budget (with buffer)"
        assert metadata["recent_message_count"] >= 1, "Should include recent messages"
        assert metadata["context_type"] == "token_aware", "Should use token-aware context"

    async def test_context_manager_with_real_conversation_flow(self):
        """Test context manager with realistic conversation flow."""
        # Create mock LLM provider
        mock_llm = AsyncMock()
        mock_llm.generate_text = AsyncMock(
            return_value="Summary: User experiencing database timeout. Identified host: db.example.com:5432"
        )

        # Create summarizer and context manager
        summarizer = ConversationSummarizer(llm_provider=mock_llm)
        budget = ContextBudget(
            max_total_tokens=2000,
            reserved_for_recent=1000,
            max_summary_tokens=750,
            min_recent_messages=3
        )
        manager = TokenAwareContextManager(budget=budget, summarizer=summarizer)

        # Create realistic conversation history (15 turns)
        conversation = []
        base_time = datetime.utcnow()

        topics = [
            ("user", "My application cannot connect to the database"),
            ("assistant", "Let me help you troubleshoot. Can you provide the error message?"),
            ("user", "Error: Connection timeout after 30 seconds. Host: db.example.com:5432"),
            ("assistant", "This appears to be a network connectivity issue. Can you ping the database host?"),
            ("user", "Yes, I can ping it. Ping works fine with average 2ms latency"),
            ("assistant", "Good, network is reachable. Let's check if the database port is open. Try: telnet db.example.com 5432"),
            ("user", "Telnet connection refused. Port seems closed"),
            ("assistant", "Port 5432 is not accepting connections. Let's verify the database service is running."),
            ("user", "I checked with systemctl status postgresql. Service is active and running"),
            ("assistant", "Service is running but not accepting connections. Check postgresql.conf for listen_addresses setting."),
            ("user", "Found it! listen_addresses = 'localhost'. Should it be different?"),
            ("assistant", "Yes! That's the issue. PostgreSQL is only listening on localhost. Change to listen_addresses = '*' or specific IP."),
            ("user", "Changed to '*' and restarted PostgreSQL. Still getting timeout"),
            ("assistant", "Now check pg_hba.conf to ensure your application's IP is allowed to connect."),
            ("user", "Added entry: host all all 10.0.0.0/24 md5. Connection now works! Thank you!")
        ]

        for i, (role, content) in enumerate(topics):
            conversation.append({
                "role": role,
                "content": content,
                "timestamp": (base_time + timedelta(minutes=i*2)).isoformat()
            })

        # Build context
        context, metadata = await manager.build_context(
            conversation_history=conversation,
            case_title="Database connection timeout investigation"
        )

        # Assertions
        assert len(context) > 0, "Context should not be empty"
        assert "Database connection timeout investigation" in context, "Should include case title"
        assert metadata["total_tokens"] <= budget.max_total_tokens * 1.5, "Should respect token budget"
        assert metadata["recent_message_count"] >= budget.min_recent_messages, "Should include minimum recent messages"
        assert metadata["truncated"], "Long conversation should be truncated/summarized"

        # Verify recent messages are in full detail
        assert "pg_hba.conf" in context, "Recent technical details should be preserved"
        assert "10.0.0.0/24" in context, "Recent specific configurations should be preserved"
        assert "Connection now works" in context, "Resolution should be in recent context"

    async def test_context_continuity_across_multiple_queries(self):
        """Test that context builds incrementally across multiple queries."""
        mock_llm = AsyncMock()

        # First summary call
        mock_llm.generate_text = AsyncMock(
            return_value="Summary: Initial troubleshooting of database connection issue. Identified timeout problem."
        )

        summarizer = ConversationSummarizer(llm_provider=mock_llm)
        budget = ContextBudget(max_total_tokens=1000, reserved_for_recent=500, max_summary_tokens=400)
        manager = TokenAwareContextManager(budget=budget, summarizer=summarizer)

        # First query - short conversation
        conversation_v1 = [
            {"role": "user", "content": "Database connection failing", "timestamp": datetime.utcnow().isoformat()},
            {"role": "assistant", "content": "Let me help troubleshoot", "timestamp": datetime.utcnow().isoformat()},
        ]

        context_v1, metadata_v1 = await manager.build_context(conversation_v1)

        assert not metadata_v1["truncated"], "Short conversation should not be truncated"
        assert metadata_v1["summary_tokens"] == 0, "No summary needed for short conversation"

        # Second query - longer conversation requiring summary
        conversation_v2 = conversation_v1 + [
            {"role": "user", "content": "Timeout after 30 seconds. Host: db.example.com:5432", "timestamp": datetime.utcnow().isoformat()},
            {"role": "assistant", "content": "Check if port is open with telnet", "timestamp": datetime.utcnow().isoformat()},
            {"role": "user", "content": "Port appears closed", "timestamp": datetime.utcnow().isoformat()},
            {"role": "assistant", "content": "Verify PostgreSQL service status", "timestamp": datetime.utcnow().isoformat()},
            {"role": "user", "content": "Service is running but not accepting connections", "timestamp": datetime.utcnow().isoformat()},
            {"role": "assistant", "content": "Check postgresql.conf listen_addresses setting", "timestamp": datetime.utcnow().isoformat()},
        ]

        # Update mock for second summary (building on first)
        mock_llm.generate_text = AsyncMock(
            return_value="Summary: Database timeout issue. Service running but not accepting connections. Investigating postgresql.conf configuration."
        )

        context_v2, metadata_v2 = await manager.build_context(
            conversation_v2,
            existing_summary=None  # In real scenario, this would come from Redis
        )

        # Verify progressive context building
        assert len(context_v2) > len(context_v1), "Context should grow with conversation"
        assert metadata_v2["total_tokens"] <= budget.max_total_tokens * 1.5, "Should stay within budget"
        assert "postgresql.conf" in context_v2, "Recent technical details should be preserved"

    async def test_fallback_to_extractive_summary_on_llm_failure(self):
        """Test graceful fallback when LLM summarization fails."""
        # Create mock LLM that fails
        mock_llm = AsyncMock()
        mock_llm.generate_text = AsyncMock(side_effect=Exception("LLM service unavailable"))

        summarizer = ConversationSummarizer(llm_provider=mock_llm)
        budget = ContextBudget(max_total_tokens=1000, reserved_for_recent=500, max_summary_tokens=400)
        manager = TokenAwareContextManager(budget=budget, summarizer=summarizer)

        # Create long conversation
        conversation = []
        for i in range(20):
            conversation.append({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": f"This is message {i} with some important troubleshooting details about the system issue.",
                "timestamp": datetime.utcnow().isoformat()
            })

        # Should not raise exception, should use extractive fallback
        context, metadata = await manager.build_context(conversation)

        assert len(context) > 0, "Should produce context even with LLM failure"
        assert metadata["total_tokens"] <= budget.max_total_tokens * 1.5, "Should respect budget"
        assert metadata["truncated"], "Long conversation should be truncated"
        # With extractive fallback, we still get context but without LLM-generated summary

    async def test_empty_conversation_handling(self):
        """Test handling of edge case: empty conversation."""
        mock_llm = AsyncMock()
        summarizer = ConversationSummarizer(llm_provider=mock_llm)
        manager = TokenAwareContextManager(summarizer=summarizer)

        context, metadata = await manager.build_context([])

        assert context == "", "Empty conversation should produce empty context"
        assert metadata["total_tokens"] == 0
        assert metadata["recent_message_count"] == 0
        assert not metadata["truncated"]
