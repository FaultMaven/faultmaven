"""
Unit tests for few-shot examples module

Tests the enhanced few-shot example selection system including:
- Response-type-based example selection
- Intent-based example selection
- Intelligent example selection with priority
- Prompt formatting
- Example validation
"""

import pytest
from typing import Dict, List

from faultmaven.models.api import ResponseType
from faultmaven.models.agentic import QueryIntent
from faultmaven.prompts.few_shot_examples import (
    get_examples_by_response_type,
    get_examples_by_intent,
    select_intelligent_examples,
    format_intelligent_few_shot_prompt,
    get_few_shot_examples,
    format_few_shot_prompt,
    CLARIFICATION_REQUEST_EXAMPLES,
    PLAN_PROPOSAL_EXAMPLES,
    SOLUTION_READY_EXAMPLES,
    NEEDS_MORE_DATA_EXAMPLES,
    ESCALATION_REQUIRED_EXAMPLES,
    CONFIRMATION_REQUEST_EXAMPLES,
    BOUNDARY_RESPONSE_EXAMPLES,
    KUBERNETES_TROUBLESHOOTING_EXAMPLES,
    REDIS_TROUBLESHOOTING_EXAMPLES,
    POSTGRESQL_TROUBLESHOOTING_EXAMPLES,
)


# =============================================================================
# Test Example Counts
# =============================================================================

def test_response_type_examples_exist():
    """Test that all response type example sets exist and are non-empty"""
    assert len(CLARIFICATION_REQUEST_EXAMPLES) >= 3, "Should have at least 3 clarification examples"
    assert len(PLAN_PROPOSAL_EXAMPLES) >= 1, "Should have at least 1 plan proposal example"
    assert len(SOLUTION_READY_EXAMPLES) >= 1, "Should have at least 1 solution ready example"
    assert len(NEEDS_MORE_DATA_EXAMPLES) >= 1, "Should have at least 1 needs more data example"
    assert len(ESCALATION_REQUIRED_EXAMPLES) >= 2, "Should have at least 2 escalation examples"
    assert len(CONFIRMATION_REQUEST_EXAMPLES) >= 2, "Should have at least 2 confirmation examples"
    assert len(BOUNDARY_RESPONSE_EXAMPLES) >= 5, "Should have at least 5 boundary response examples"


def test_domain_examples_exist():
    """Test that domain-specific example sets exist"""
    assert len(KUBERNETES_TROUBLESHOOTING_EXAMPLES) >= 3, "Should have K8s examples"
    assert len(REDIS_TROUBLESHOOTING_EXAMPLES) >= 2, "Should have Redis examples"
    assert len(POSTGRESQL_TROUBLESHOOTING_EXAMPLES) >= 2, "Should have PostgreSQL examples"


def test_total_example_count():
    """Test that we have sufficient total examples"""
    total_response_examples = (
        len(CLARIFICATION_REQUEST_EXAMPLES) +
        len(PLAN_PROPOSAL_EXAMPLES) +
        len(SOLUTION_READY_EXAMPLES) +
        len(NEEDS_MORE_DATA_EXAMPLES) +
        len(ESCALATION_REQUIRED_EXAMPLES) +
        len(CONFIRMATION_REQUEST_EXAMPLES) +
        len(BOUNDARY_RESPONSE_EXAMPLES)
    )

    total_domain_examples = (
        len(KUBERNETES_TROUBLESHOOTING_EXAMPLES) +
        len(REDIS_TROUBLESHOOTING_EXAMPLES) +
        len(POSTGRESQL_TROUBLESHOOTING_EXAMPLES)
    )

    assert total_response_examples >= 15, f"Should have at least 15 response-type examples, got {total_response_examples}"
    assert total_domain_examples >= 7, f"Should have at least 7 domain examples, got {total_domain_examples}"


# =============================================================================
# Test Example Structure and Quality
# =============================================================================

def test_clarification_example_structure():
    """Test that clarification examples have required fields"""
    for example in CLARIFICATION_REQUEST_EXAMPLES:
        assert "response_type" in example
        assert example["response_type"] == ResponseType.CLARIFICATION_REQUEST
        assert "scenario" in example
        assert "user_query" in example
        assert "assistant_response" in example
        assert len(example["assistant_response"]) > 200, "Response should be substantial"


def test_escalation_example_structure():
    """Test that escalation examples have required fields and urgency markers"""
    for example in ESCALATION_REQUIRED_EXAMPLES:
        assert "response_type" in example
        assert example["response_type"] == ResponseType.ESCALATION_REQUIRED
        assert "scenario" in example
        assert "user_query" in example
        assert "assistant_response" in example

        # Check for urgency markers
        response = example["assistant_response"]
        assert any(marker in response for marker in ["🚨", "CRITICAL", "IMMEDIATE", "Escalat"]), \
            "Escalation example should contain urgency markers"


def test_confirmation_example_structure():
    """Test that confirmation examples have warning markers"""
    for example in CONFIRMATION_REQUEST_EXAMPLES:
        assert "response_type" in example
        assert example["response_type"] == ResponseType.CONFIRMATION_REQUEST

        response = example["assistant_response"]
        assert any(marker in response for marker in ["⚠️", "WARNING", "Confirmation", "DESTRUCTIVE"]), \
            "Confirmation example should contain warning markers"


def test_boundary_example_structure():
    """Test that boundary examples have intent field"""
    for example in BOUNDARY_RESPONSE_EXAMPLES:
        assert "response_type" in example
        assert "intent" in example
        assert example["intent"] in [
            QueryIntent.OFF_TOPIC,
            QueryIntent.GREETING,
            QueryIntent.GRATITUDE,
            QueryIntent.META_FAULTMAVEN,
            QueryIntent.CONVERSATION_CONTROL
        ]


def test_example_has_actionable_content():
    """Test that examples have actionable content (commands, steps, etc.)"""
    def has_actionable_content(response: str) -> bool:
        """Check if response has commands or numbered steps"""
        has_commands = "```" in response
        has_numbered_steps = any(f"{i}." in response for i in range(1, 6))
        has_bullet_points = "- " in response or "• " in response
        return has_commands or has_numbered_steps or has_bullet_points

    # Check clarification examples
    for example in CLARIFICATION_REQUEST_EXAMPLES:
        assert has_actionable_content(example["assistant_response"]), \
            f"Clarification example should have actionable content: {example['scenario']}"

    # Check solution examples
    for example in SOLUTION_READY_EXAMPLES:
        assert has_actionable_content(example["assistant_response"]), \
            "Solution example should have actionable content"


# =============================================================================
# Test Example Selection Functions
# =============================================================================

def test_get_examples_by_response_type():
    """Test response type filtering"""
    # Test CLARIFICATION_REQUEST
    examples = get_examples_by_response_type(ResponseType.CLARIFICATION_REQUEST, limit=2)
    assert len(examples) <= 2, "Should respect limit"
    assert len(examples) > 0, "Should return examples"
    assert all(ex["response_type"] == ResponseType.CLARIFICATION_REQUEST for ex in examples)

    # Test ESCALATION_REQUIRED
    examples = get_examples_by_response_type(ResponseType.ESCALATION_REQUIRED, limit=1)
    assert len(examples) == 1
    assert examples[0]["response_type"] == ResponseType.ESCALATION_REQUIRED

    # Test CONFIRMATION_REQUEST
    examples = get_examples_by_response_type(ResponseType.CONFIRMATION_REQUEST, limit=2)
    assert len(examples) == 2
    assert all(ex["response_type"] == ResponseType.CONFIRMATION_REQUEST for ex in examples)


def test_get_examples_by_intent():
    """Test intent filtering"""
    # Test OFF_TOPIC
    examples = get_examples_by_intent(QueryIntent.OFF_TOPIC, limit=1)
    assert len(examples) == 1
    assert examples[0]["intent"] == QueryIntent.OFF_TOPIC

    # Test GREETING
    examples = get_examples_by_intent(QueryIntent.GREETING, limit=1)
    assert len(examples) == 1
    assert examples[0]["intent"] == QueryIntent.GREETING

    # Test GRATITUDE
    examples = get_examples_by_intent(QueryIntent.GRATITUDE, limit=1)
    assert len(examples) == 1
    assert examples[0]["intent"] == QueryIntent.GRATITUDE


def test_select_intelligent_examples_priority():
    """Test selection priority (response type > intent > domain)"""
    # Test 1: Response type should take priority
    examples = select_intelligent_examples(
        ResponseType.CLARIFICATION_REQUEST,
        intent=QueryIntent.GREETING,
        domain="kubernetes",
        limit=1
    )
    assert len(examples) == 1
    assert examples[0]["response_type"] == ResponseType.CLARIFICATION_REQUEST

    # Test 2: For ANSWER with boundary intent, use intent-specific examples
    examples = select_intelligent_examples(
        ResponseType.ANSWER,
        intent=QueryIntent.OFF_TOPIC,
        domain="kubernetes",
        limit=1
    )
    assert len(examples) == 1
    assert examples[0]["intent"] == QueryIntent.OFF_TOPIC

    # Test 3: Domain fallback for technical queries
    examples = select_intelligent_examples(
        ResponseType.ANSWER,
        intent=QueryIntent.TROUBLESHOOTING,
        domain="kubernetes",
        limit=2
    )
    assert len(examples) <= 2


def test_select_intelligent_examples_escalation():
    """Test selection of escalation examples"""
    examples = select_intelligent_examples(
        ResponseType.ESCALATION_REQUIRED,
        limit=2
    )
    assert len(examples) == 2
    assert all(ex["response_type"] == ResponseType.ESCALATION_REQUIRED for ex in examples)


def test_select_intelligent_examples_confirmation():
    """Test selection of confirmation examples"""
    examples = select_intelligent_examples(
        ResponseType.CONFIRMATION_REQUEST,
        limit=1
    )
    assert len(examples) == 1
    assert examples[0]["response_type"] == ResponseType.CONFIRMATION_REQUEST


def test_select_intelligent_examples_respects_limit():
    """Test that limit parameter is respected"""
    examples = select_intelligent_examples(
        ResponseType.CLARIFICATION_REQUEST,
        limit=1
    )
    assert len(examples) == 1

    examples = select_intelligent_examples(
        ResponseType.CLARIFICATION_REQUEST,
        limit=3
    )
    assert len(examples) <= 3


# =============================================================================
# Test Prompt Formatting
# =============================================================================

def test_format_intelligent_few_shot_prompt():
    """Test prompt formatting with intelligent selection"""
    # Test with CLARIFICATION_REQUEST
    prompt = format_intelligent_few_shot_prompt(
        ResponseType.CLARIFICATION_REQUEST,
        intent=QueryIntent.TROUBLESHOOTING,
        domain="kubernetes",
        limit=1
    )
    assert len(prompt) > 100, "Prompt should have substantial content"
    assert "Example" in prompt or "Interaction" in prompt or "User Query" in prompt

    # Test with boundary intent
    prompt = format_intelligent_few_shot_prompt(
        ResponseType.ANSWER,
        intent=QueryIntent.GREETING,
        limit=1
    )
    assert len(prompt) > 100
    assert "Hello" in prompt or "greeting" in prompt.lower()


def test_format_intelligent_few_shot_prompt_escalation():
    """Test prompt formatting for escalation examples"""
    prompt = format_intelligent_few_shot_prompt(
        ResponseType.ESCALATION_REQUIRED,
        limit=1
    )
    assert len(prompt) > 100
    assert "🚨" in prompt or "Escalat" in prompt


def test_format_intelligent_few_shot_prompt_confirmation():
    """Test prompt formatting for confirmation examples"""
    prompt = format_intelligent_few_shot_prompt(
        ResponseType.CONFIRMATION_REQUEST,
        limit=1
    )
    assert len(prompt) > 100
    assert "⚠️" in prompt or "Confirmation" in prompt


def test_format_few_shot_prompt_basic():
    """Test basic prompt formatting function"""
    examples = get_examples_by_response_type(ResponseType.CLARIFICATION_REQUEST, limit=1)
    formatted = format_few_shot_prompt(examples)

    assert len(formatted) > 0
    assert "User Query" in formatted or "user query" in formatted.lower()
    assert "FaultMaven Response" in formatted or "response" in formatted.lower()


def test_format_intelligent_few_shot_prompt_empty():
    """Test graceful handling when no examples available"""
    # Request examples for response type that might not have examples
    # (This should return empty string gracefully)
    prompt = format_intelligent_few_shot_prompt(
        ResponseType.ANSWER,
        intent=QueryIntent.UNKNOWN,  # No specific examples for UNKNOWN
        limit=1
    )
    # Should either return empty or fall back to domain examples
    assert isinstance(prompt, str)


# =============================================================================
# Test Legacy Functions (Backwards Compatibility)
# =============================================================================

def test_get_few_shot_examples_kubernetes():
    """Test legacy domain-based selection for Kubernetes"""
    examples = get_few_shot_examples("kubernetes", limit=2)
    assert len(examples) <= 2
    assert len(examples) > 0


def test_get_few_shot_examples_redis():
    """Test legacy domain-based selection for Redis"""
    examples = get_few_shot_examples("redis", limit=1)
    assert len(examples) == 1


def test_get_few_shot_examples_postgres():
    """Test legacy domain-based selection for PostgreSQL"""
    examples = get_few_shot_examples("postgresql", limit=1)
    assert len(examples) == 1


def test_get_few_shot_examples_unknown_domain():
    """Test legacy selection with unknown domain returns empty list"""
    examples = get_few_shot_examples("unknown_domain", limit=5)
    assert len(examples) == 0  # Unknown domain returns empty list


# =============================================================================
# Integration Tests
# =============================================================================

def test_end_to_end_clarification_flow():
    """Test complete flow for clarification request"""
    # Select examples
    examples = select_intelligent_examples(
        ResponseType.CLARIFICATION_REQUEST,
        intent=QueryIntent.TROUBLESHOOTING,
        domain="kubernetes",
        limit=1
    )

    # Format prompt
    prompt = format_few_shot_prompt(examples)

    # Verify
    assert len(examples) == 1
    assert len(prompt) > 100
    assert examples[0]["response_type"] == ResponseType.CLARIFICATION_REQUEST


def test_end_to_end_escalation_flow():
    """Test complete flow for escalation"""
    # Format prompt directly
    prompt = format_intelligent_few_shot_prompt(
        ResponseType.ESCALATION_REQUIRED,
        limit=1
    )

    # Verify
    assert len(prompt) > 100
    assert "🚨" in prompt or "Escalat" in prompt


def test_end_to_end_boundary_flow():
    """Test complete flow for boundary intent"""
    prompt = format_intelligent_few_shot_prompt(
        ResponseType.ANSWER,
        intent=QueryIntent.OFF_TOPIC,
        limit=1
    )

    assert len(prompt) > 100
    assert "FaultMaven" in prompt  # Should explain what FaultMaven does


# =============================================================================
# Performance Tests
# =============================================================================

def test_selection_performance():
    """Test that example selection is fast"""
    import time

    start = time.time()
    for _ in range(100):
        select_intelligent_examples(
            ResponseType.CLARIFICATION_REQUEST,
            intent=QueryIntent.TROUBLESHOOTING,
            domain="kubernetes",
            limit=2
        )
    elapsed = time.time() - start

    # Should complete 100 selections in under 100ms (1ms per selection)
    assert elapsed < 0.1, f"Selection too slow: {elapsed}s for 100 iterations"


def test_formatting_performance():
    """Test that prompt formatting is fast"""
    import time

    examples = select_intelligent_examples(ResponseType.CLARIFICATION_REQUEST, limit=2)

    start = time.time()
    for _ in range(100):
        format_few_shot_prompt(examples)
    elapsed = time.time() - start

    # Should complete 100 formatting operations in under 50ms
    assert elapsed < 0.05, f"Formatting too slow: {elapsed}s for 100 iterations"
