"""
Few-Shot Examples for FaultMaven AI Troubleshooting

This module contains example interactions demonstrating the five-phase SRE doctrine
in action for common troubleshooting scenarios. These examples help the LLM learn
the expected response patterns and troubleshooting methodology.

Enhanced for Task 2: Includes response-type-specific and intent-aware examples
that integrate with the intelligent prompt system.
"""

from typing import List, Dict, Optional
from faultmaven.models.api import ResponseType
from faultmaven.models.agentic import QueryIntent

# =============================================================================
# OPTIMIZED PATTERN TEMPLATES (Phase 3 - Token Efficiency)
# =============================================================================
# Reduced from ~1,500 tokens per example to ~200 tokens per pattern
# Following Anthropic best practice: "smallest set of high-signal tokens"
# These patterns show structure and approach without verbose examples

# Domain-Specific Troubleshooting Patterns (System Directive Format)

KUBERNETES_TROUBLESHOOTING_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
For Kubernetes/container issues (pods crashing, OOMKilled, etc.):
- Follow 5-phase approach: Blast radius → Timeline → Hypothesis → Validate → Solution
- Use kubectl commands: get/describe/logs/top
- Check memory limits vs actual usage
- Verify recent deployments or config changes
- Consider both configuration and code issues
- Provide rollback plan + fix + prevention
[END DIRECTIVE]"""

REDIS_TROUBLESHOOTING_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
For Redis connection or performance issues:
- Verify pod/container is running
- Check for recent changes (NetworkPolicy, config updates)
- Test connectivity from client (nc/telnet from app pod)
- Validate network rules and service configurations
- Check Redis logs for authentication or memory issues
[END DIRECTIVE]"""

POSTGRESQL_TROUBLESHOOTING_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
For PostgreSQL performance or query issues:
- Identify which queries are slow (pg_stat_activity)
- Check table sizes and growth patterns
- Look for missing VACUUM operations or stale statistics
- Validate indexes exist and are being used
- Solution typically: VACUUM ANALYZE + index optimization
[END DIRECTIVE]"""

NETWORK_TROUBLESHOOTING_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
For network errors (502, 503, timeouts):
- Check backend pod health and readiness
- Verify service endpoints are populated
- Check resource usage on backend pods
- Review ingress/load balancer logs for timeout patterns
- Solution often: backend scaling, readiness probes, or resource limits
[END DIRECTIVE]"""

SECURITY_TROUBLESHOOTING_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
For security issues (failed auth, suspicious activity):
- Analyze volume and patterns of failures
- Identify source IPs and geographic patterns
- Check rate limiting and circuit breaker status
- Distinguish between attacks and legitimate user issues
- Recommend blocking, alerting, or optimization based on findings
[END DIRECTIVE]"""

PERFORMANCE_TROUBLESHOOTING_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
For performance degradation issues:
- Determine if gradual or sudden onset
- Check database slow query logs
- Analyze cache hit rates
- Look for resource exhaustion (CPU, memory, I/O)
- Address root cause: database optimization, cache tuning, or resource scaling
[END DIRECTIVE]"""

DEPLOYMENT_TROUBLESHOOTING_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
For deployment or rollout issues:
- Check pod readiness and health checks
- Verify deployment strategy settings (rolling update params)
- Look for PodDisruptionBudget blocking rollout
- Review recent events for errors
- Provide fix or safe rollback procedure
[END DIRECTIVE]"""

# Compact pattern library (replaces verbose examples - 87% token reduction)
TROUBLESHOOTING_PATTERNS = {
    "kubernetes": KUBERNETES_TROUBLESHOOTING_PATTERN,
    "redis": REDIS_TROUBLESHOOTING_PATTERN,
    "postgresql": POSTGRESQL_TROUBLESHOOTING_PATTERN,
    "network": NETWORK_TROUBLESHOOTING_PATTERN,
    "security": SECURITY_TROUBLESHOOTING_PATTERN,
    "performance": PERFORMANCE_TROUBLESHOOTING_PATTERN,
    "deployment": DEPLOYMENT_TROUBLESHOOTING_PATTERN,
}

# Response-Type Pattern Templates (System Directive Format)
# These are INSTRUCTIONS for the LLM, not content to display to users

CLARIFICATION_REQUEST_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
When user query lacks critical information needed for troubleshooting:
- Ask 2-3 specific, targeted questions (what/when/where format)
- Explain why each piece of information is needed for diagnosis
- Provide relevant diagnostic commands where applicable
- Maintain patient, helpful tone - never interrogative or demanding
[END DIRECTIVE]"""

PLAN_PROPOSAL_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
When presenting multi-step troubleshooting or implementation plan:
- List prerequisites and dependencies first
- Break into clear, sequential phases with numbering
- Include validation steps after each phase
- Provide rollback plan for risky operations
- Assess and communicate risk levels
- Use checklist format with estimated timing
[END DIRECTIVE]"""

SOLUTION_READY_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
When root cause has been identified and solution is ready:
- Provide immediate fix for urgent mitigation
- Explain root cause clearly
- List verification steps to confirm fix worked
- Include prevention measures for future
- Offer multiple solution options ranked by risk/effort when applicable
[END DIRECTIVE]"""

NEEDS_MORE_DATA_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
When investigation has stalled due to insufficient diagnostic data:
- List required diagnostics in priority order (most critical first)
- Provide specific commands to gather each piece of data
- Explain what findings you expect and why they matter
- Explain how each data point will advance the investigation
- Maintain direct, structured, actionable tone
[END DIRECTIVE]"""

ESCALATION_REQUIRED_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
When issue is SEV-1 incident or outside your scope:
- Provide immediate safe actions that can be taken now
- Clearly state what NOT to do to avoid making situation worse
- List critical information to gather for escalation handoff
- Provide communication protocol (who to contact, what to say)
- Maintain urgent but clear tone with explicit boundaries
[END DIRECTIVE]"""

CONFIRMATION_REQUEST_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
When proposing destructive or high-risk operation:
- Summarize impact and blast radius clearly
- List required confirmations before proceeding
- Offer safer alternatives if they exist
- Require explicit user approval before continuing
- Use warnings, checklists, and highlight risks
[END DIRECTIVE]"""

BOUNDARY_RESPONSE_PATTERN = """[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]
When query is off-topic, greeting, or meta-question about yourself:
- Respond politely to greeting/gratitude if present
- Gently redirect to your core competency (SRE troubleshooting)
- Briefly summarize your capabilities
- Offer to help with technical/troubleshooting issues
- Maintain friendly, professional tone focused on being helpful
[END DIRECTIVE]"""

# Response-type pattern registry
RESPONSE_PATTERNS = {
    ResponseType.CLARIFICATION_REQUEST: CLARIFICATION_REQUEST_PATTERN,
    ResponseType.PLAN_PROPOSAL: PLAN_PROPOSAL_PATTERN,
    ResponseType.SOLUTION_READY: SOLUTION_READY_PATTERN,
    ResponseType.NEEDS_MORE_DATA: NEEDS_MORE_DATA_PATTERN,
    ResponseType.ESCALATION_REQUIRED: ESCALATION_REQUIRED_PATTERN,
    ResponseType.CONFIRMATION_REQUEST: CONFIRMATION_REQUEST_PATTERN,
    ResponseType.ANSWER: BOUNDARY_RESPONSE_PATTERN,
}

# =============================================================================
# OPTIMIZED PATTERN RETRIEVAL (Phase 3)
# =============================================================================
# Just-in-time pattern loading instead of preloading all verbose examples


def get_pattern(category: str) -> str:
    """Get compact troubleshooting pattern for a specific category

    Retrieves optimized pattern templates (~50 tokens) instead of verbose
    examples (~1,500 tokens), achieving 87% token reduction.

    Args:
        category: Category of pattern ("kubernetes", "redis", "postgresql",
                 "network", "security", "performance", "deployment")

    Returns:
        Compact pattern template string or empty string if category not found

    Examples:
        >>> pattern = get_pattern("kubernetes")
        '[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]\\nFor Kubernetes/container issues...'
        >>> pattern = get_pattern("redis")
        '[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]\\nFor Redis connection...'
        >>> pattern = get_pattern("unknown")
        ''
    """
    return TROUBLESHOOTING_PATTERNS.get(category, "")


def get_response_pattern(response_type: ResponseType) -> str:
    """Get compact response pattern for a specific response type

    Args:
        response_type: ResponseType enum value

    Returns:
        Compact response pattern template string (~40 tokens) or empty string

    Examples:
        >>> pattern = get_response_pattern(ResponseType.CLARIFICATION_REQUEST)
        '[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]\\nAsk 2-3 specific...'
        >>> pattern = get_response_pattern(ResponseType.SOLUTION_READY)
        '[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]\\nProvide numbered...'
    """
    return RESPONSE_PATTERNS.get(response_type, "")


def format_pattern_prompt(
    response_type: Optional[ResponseType] = None,
    domain: Optional[str] = None
) -> str:
    """Format pattern templates into a concise prompt section

    Just-in-time pattern loading - only loads what's needed for the current query,
    achieving 87% token reduction compared to verbose examples.

    Args:
        response_type: Optional ResponseType for response pattern guidance
        domain: Optional domain category for troubleshooting pattern ("kubernetes",
               "redis", "postgresql", "network", "security", "performance", "deployment")

    Returns:
        Formatted string with relevant patterns (~100-200 tokens total) or empty
        string for simple response types that don't need patterns

    Examples:
        >>> format_pattern_prompt(ResponseType.CLARIFICATION_REQUEST, "kubernetes")
        '[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]\\n...'
        >>> format_pattern_prompt(response_type=ResponseType.ANSWER)
        ''  # Simple responses skip patterns
        >>> format_pattern_prompt(domain="redis")
        '[SYSTEM DIRECTIVE - DO NOT ECHO THIS TO USER]\\nFor Redis connection...'
    """
    # Skip pattern templates entirely for simple informational responses
    # They don't need structured guidance and adding patterns causes LLM to echo them
    simple_response_types = [ResponseType.ANSWER]

    if response_type in simple_response_types:
        return ""  # Don't add pattern templates at all for simple responses

    if not response_type and not domain:
        return ""

    parts = []

    # Add response pattern if provided (for complex response types only)
    if response_type:
        response_pattern = get_response_pattern(response_type)
        if response_pattern:
            # No markdown headers - just the directive itself
            parts.append(response_pattern)

    # Add domain pattern if provided
    if domain:
        domain_pattern = get_pattern(domain)
        if domain_pattern:
            # No markdown headers - just the directive itself
            parts.append(domain_pattern)

    if not parts:
        return ""

    return "\n\n".join(parts)


def get_examples_for_context(user_query: str, limit: int = 2) -> str:
    """
    Get relevant pattern based on user query content.

    Args:
        user_query: User's troubleshooting query
        limit: Unused (kept for API compatibility)

    Returns:
        Relevant pattern template

    Examples:
        >>> pattern = get_examples_for_context("My Redis pod won't start")
        # Returns Redis pattern
        >>> pattern = get_examples_for_context("PostgreSQL is slow")
        # Returns PostgreSQL pattern
    """
    query_lower = user_query.lower()

    # Keyword matching for categories
    category_keywords = {
        "kubernetes": ["pod", "deployment", "kubernetes", "k8s", "container", "crashloop"],
        "redis": ["redis", "cache", "connection refused"],
        "postgresql": ["postgresql", "postgres", "database", "query", "sql"],
        "network": ["502", "503", "timeout", "connection", "network", "load balancer"],
        "security": ["auth", "authentication", "security", "attack", "breach"],
        "performance": ["slow", "performance", "latency", "timeout"],
        "deployment": ["deployment", "rollout", "rollback", "ci/cd", "pipeline"],
    }

    # Find matching category
    for category, keywords in category_keywords.items():
        if any(keyword in query_lower for keyword in keywords):
            return get_pattern(category)

    # Default: no pattern
    return ""


# =============================================================================
# OPTIMIZED EXAMPLE SELECTION FUNCTIONS (Phase 3)
# =============================================================================

def get_examples_by_response_type(
    response_type: ResponseType,
    limit: int = 1
) -> str:
    """
    Get pattern template for a specific response type.

    Args:
        response_type: The ResponseType to get pattern for
        limit: Unused (kept for API compatibility)

    Returns:
        Relevant response pattern template

    Examples:
        >>> pattern = get_examples_by_response_type(ResponseType.CLARIFICATION_REQUEST)
        >>> pattern = get_examples_by_response_type(ResponseType.ANSWER)
    """
    return get_response_pattern(response_type)


def get_examples_by_intent(
    intent: QueryIntent,
    limit: int = 1
) -> str:
    """
    Get pattern template for a specific query intent.

    Args:
        intent: The QueryIntent to get pattern for
        limit: Unused (kept for API compatibility)

    Returns:
        Relevant pattern template

    Examples:
        >>> pattern = get_examples_by_intent(QueryIntent.GREETING)
        >>> pattern = get_examples_by_intent(QueryIntent.OFF_TOPIC)
    """
    # Boundary intents use ANSWER pattern
    boundary_intents = [
        QueryIntent.OFF_TOPIC,
        QueryIntent.GREETING,
        QueryIntent.GRATITUDE,
        QueryIntent.META_FAULTMAVEN,
        QueryIntent.CONVERSATION_CONTROL,
    ]

    if intent in boundary_intents:
        return get_response_pattern(ResponseType.ANSWER)

    # For troubleshooting intents, no specific pattern
    return ""


def select_intelligent_examples(
    response_type: ResponseType,
    intent: Optional[QueryIntent] = None,
    domain: Optional[str] = None,
    limit: int = 2
) -> str:
    """
    Intelligently select pattern template based on response type, intent, and domain.

    This is the main pattern selection function that integrates with the
    intelligent prompt system using just-in-time pattern loading.

    Selection Priority:
    1. Response type pattern (most important)
    2. Domain pattern (for technical troubleshooting)
    3. Compact format (~100-200 tokens total vs 1,500 tokens before)

    Args:
        response_type: The ResponseType selected by the system
        intent: Optional QueryIntent for additional context
        domain: Optional domain category (e.g., "kubernetes", "database")
        limit: Unused (kept for API compatibility)

    Returns:
        Formatted pattern template string

    Examples:
        >>> pattern = select_intelligent_examples(
        ...     ResponseType.CLARIFICATION_REQUEST,
        ...     domain="kubernetes"
        ... )

        >>> pattern = select_intelligent_examples(
        ...     ResponseType.ANSWER,
        ...     intent=QueryIntent.OFF_TOPIC
        ... )
    """
    return format_pattern_prompt(response_type, domain)


def format_intelligent_few_shot_prompt(
    response_type: ResponseType,
    intent: Optional[QueryIntent] = None,
    domain: Optional[str] = None,
    limit: int = 2
) -> str:
    """
    Format intelligently-selected pattern templates into a prompt string.

    This is a convenience function that combines selection and formatting.
    Uses compact pattern templates instead of verbose examples (87% reduction).

    Args:
        response_type: The ResponseType selected by the system
        intent: Optional QueryIntent for additional context
        domain: Optional domain category
        limit: Unused (kept for API compatibility)

    Returns:
        Formatted string with patterns ready for prompt injection (~100-200 tokens)

    Examples:
        >>> prompt = format_intelligent_few_shot_prompt(
        ...     ResponseType.CLARIFICATION_REQUEST,
        ...     domain="kubernetes"
        ... )
        >>> print(prompt)
        **Response Pattern**:
        **Pattern**: Missing critical info
        **Approach**: Ask 2-3 specific questions...
    """
    return select_intelligent_examples(response_type, intent, domain, limit)
