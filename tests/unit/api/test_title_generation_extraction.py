"""Tests for _extract_user_signals_from_context in title generation"""

import pytest

from faultmaven.modules.case.api.routes import _extract_user_signals_from_context


class TestExtractUserSignals:
    """Test user signal extraction for title generation"""

    def test_empty_context_returns_empty_string(self):
        """Empty context should return empty string"""
        result = _extract_user_signals_from_context("")
        assert result == ""

    def test_none_context_returns_empty_string(self):
        """None context should return empty string"""
        result = _extract_user_signals_from_context(None)
        assert result == ""

    def test_single_user_message_extracted(self):
        """Single user message should be extracted and sanitized"""
        context = """Previous conversation in this troubleshooting case:
1. [14:20] User: I'm having database connection issues with PostgreSQL timeout errors
2. [14:21] Assistant: Let me help you troubleshoot this issue"""

        result = _extract_user_signals_from_context(context)
        assert "database connection issues" in result
        assert "PostgreSQL timeout errors" in result
        # Should not include assistant response
        assert "Let me help" not in result

    def test_multiple_user_messages_concatenated(self):
        """Multiple user messages should be concatenated with spaces"""
        context = """Previous conversation in this troubleshooting case:
1. [14:20] User: Database connection timeout error
2. [14:21] Assistant: Let me investigate
3. [14:22] User: Error says authentication failed
4. [14:23] Assistant: Check your credentials
5. [14:24] User: Password is verified correct"""

        result = _extract_user_signals_from_context(context)

        # Should contain all three user messages
        assert "Database connection timeout error" in result
        assert "authentication failed" in result
        assert "Password is verified correct" in result

        # Should not contain assistant messages
        assert "Let me investigate" not in result
        assert "Check your credentials" not in result

        # Verify they're concatenated (spaces between messages)
        # Total length should be sum of all messages + spaces
        assert len(result) > 90  # All three messages combined (~95 chars)

    def test_last_short_message_doesnt_truncate_earlier_content(self):
        """Bug fix: Last short message should not cause earlier content to be ignored"""
        context = """Previous conversation in this troubleshooting case:
1. [14:20] User: I'm experiencing a critical production issue with our PostgreSQL database. The application is throwing connection timeout errors intermittently. We've checked network connectivity, firewall rules, and database logs.
2. [14:21] Assistant: Let me investigate this issue for you
3. [14:22] User: Thank you so much"""

        result = _extract_user_signals_from_context(context)

        # Should include the long first message
        assert "critical production issue" in result
        assert "PostgreSQL database" in result
        assert "connection timeout errors" in result

        # Should also include the short last message (now has 4 words)
        assert "Thank you so much" in result

        # Total length should be >200 chars (both messages combined)
        assert len(result) > 200, f"Expected >200 chars, got {len(result)}: {result}"

    def test_filters_out_system_messages(self):
        """System messages should be filtered out"""
        context = """Previous conversation in this troubleshooting case:
1. [14:20] System: Case created
2. [14:21] User: Database connection timeout
3. [14:22] System: Agent processing started"""

        result = _extract_user_signals_from_context(context)

        # Should only include user message
        assert "Database connection timeout" in result
        assert "Case created" not in result
        assert "Agent processing" not in result

    def test_filters_out_skip_patterns(self):
        """Should filter out metadata and headers"""
        context = """Previous conversation in this troubleshooting case:
Case status: Open
Created: 2026-02-03
Message count: 5
1. [14:20] User: Database connection timeout error
Current query:"""

        result = _extract_user_signals_from_context(context)

        # Should only include user message
        assert "Database connection timeout error" in result

        # Should not include metadata
        assert "Case status" not in result
        assert "Created:" not in result
        assert "Message count" not in result
        assert "Current query" not in result

    def test_extracts_meaningful_description(self):
        """Should extract description if it's meaningful (not 'No description')"""
        context = """Case: Database Issue
Description: PostgreSQL connection pooling exhausted under high load"""

        result = _extract_user_signals_from_context(context)

        # Should extract description
        assert "PostgreSQL connection pooling" in result
        assert "exhausted under high load" in result

    def test_filters_out_no_description(self):
        """Should filter out 'No description' placeholder"""
        context = """Case: Database Issue
Description: No description
1. [14:20] User: Connection timeout error"""

        result = _extract_user_signals_from_context(context)

        # Should not include "No description"
        assert "No description" not in result

        # Should include user message
        assert "Connection timeout error" in result

    def test_minimum_word_count_filter(self):
        """Should filter out messages with fewer than 3 words"""
        context = """Previous conversation in this troubleshooting case:
1. [14:20] User: Hi
2. [14:21] Assistant: Hello there
3. [14:22] User: Help me
4. [14:23] User: Database connection timeout error occurring"""

        result = _extract_user_signals_from_context(context)

        # Should not include messages with <3 words
        assert "Hi" not in result
        assert "Help me" not in result

        # Should include message with ≥3 words
        assert "Database connection timeout error occurring" in result

    def test_deduplication_of_identical_messages(self):
        """Should deduplicate near-identical user messages"""
        context = """Previous conversation in this troubleshooting case:
1. [14:20] User: Database connection timeout error
2. [14:21] Assistant: Let me check
3. [14:22] User: Database connection timeout error
4. [14:23] User: Still getting database connection timeout error"""

        result = _extract_user_signals_from_context(context)

        # Should only include unique messages
        # Count occurrences of the exact phrase
        count = result.count("Database connection timeout error")
        assert count == 1, f"Expected 1 occurrence of exact duplicate, got {count}"

        # Should include the variation
        assert "Still getting database connection timeout error" in result

    def test_caps_to_last_12_messages(self):
        """Should cap to last 12 meaningful user messages"""
        # Create 15 user messages with unique prefixes
        messages = []
        for i in range(1, 16):
            messages.append(
                f"{i}. [14:{i:02d}] User: Msg{i:02d} unique content for testing here"
            )

        context = "Previous conversation:\n" + "\n".join(messages)

        result = _extract_user_signals_from_context(context)

        # Should only include messages 4-15 (last 12 messages)
        assert "Msg01 unique" not in result
        assert "Msg02 unique" not in result
        assert "Msg03 unique" not in result
        assert "Msg04 unique" in result
        assert "Msg15 unique" in result

    def test_sanitization_applied(self):
        """Should sanitize PII and sensitive data from extracted content"""
        context = """Previous conversation:
1. [14:20] User: My email is test@example.com and phone is 555-123-4567 with IP 192.168.1.1"""

        result = _extract_user_signals_from_context(context)

        # Should replace PII with placeholders
        assert "test@example.com" not in result
        assert "555-123-4567" not in result
        assert "192.168.1.1" not in result

        # Should contain placeholders
        assert "[email]" in result or "[phone]" in result or "[ip]" in result

    def test_realistic_conversation_scenario(self):
        """Test realistic conversation with multiple turns and last short message"""
        context = """Previous conversation in this troubleshooting case:
1. [14:20] User: I'm experiencing database connection timeout errors when my application tries to connect to PostgreSQL. The error occurs intermittently, roughly every few minutes.
2. [14:21] Assistant: Let me help troubleshoot this. Can you check your connection pool settings?
3. [14:22] User: I've checked the pool settings and they look correct. Max connections is set to 100 and min idle is 10.
4. [14:23] Assistant: Have you checked if you're hitting PostgreSQL's max_connections limit?
5. [14:24] User: Good point let me check that now
6. [14:25] Assistant: Also check pg_stat_activity to see current active connections
7. [14:26] User: Found it the max connections was set too low
8. [14:27] Assistant: Great! What value was it set to?
9. [14:28] User: It was set to 50 and we were exceeding that limit
10. [14:29] Assistant: That explains the timeouts. Increase it to at least 150 for your workload.
11. [14:30] User: Done thank you very much"""

        result = _extract_user_signals_from_context(context)

        # Should include content from ALL user messages
        assert "database connection timeout errors" in result
        assert "PostgreSQL" in result
        assert "pool settings" in result
        assert "max connections was set too low" in result
        assert "set to 50" in result
        assert "Done thank you very much" in result

        # Should NOT include assistant messages
        assert "Let me help troubleshoot" not in result
        assert "pg_stat_activity" not in result
        assert "Increase it to at least 150" not in result

        # Total length should be substantial (all user messages combined)
        # Rough calculation: 6 user messages * ~50 chars average = 300+ chars
        assert (
            len(result) > 250
        ), f"Expected >250 chars for full conversation, got {len(result)}"

    def test_alternative_user_format(self):
        """Should handle 'User:' format without brackets"""
        context = """Previous conversation:
User: Database connection timeout error occurring
Assistant: Let me investigate
User: Error persists after restart"""

        result = _extract_user_signals_from_context(context)

        # Should extract both user messages
        assert "Database connection timeout error occurring" in result
        assert "Error persists after restart" in result
        assert "Let me investigate" not in result
