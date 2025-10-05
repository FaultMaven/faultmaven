# Query Classification & Prompt Engineering Architecture
## Complete Design Specification - Phase 0 Implementation ✅

**Status**: ✅ IMPLEMENTED (2025-10-03)  
**Test Coverage**: 28/28 passing (100%)  
**Version**: 3.0 - Response-Format-Driven Classification System

---

## Document Overview

This document consolidates the complete technical specification for FaultMaven's Query Classification and Prompt Engineering systems, representing the **Phase 0 foundation** of the FaultMaven AI troubleshooting platform.

**Contents:**
- **Part I**: Query Classification System (v3.0 Response-Format-Driven Design)
- **Part II**: Prompt Engineering System (Template Management & SRE Doctrine)

**Related Documents:**
- `IMPLEMENTATION_PLAN.md` - Master 8-phase roadmap
- `docs/architecture/SYSTEM_ARCHITECTURE.md` - Overall system architecture
- `TECHNICAL_SPECIFICATIONS.md` - Phases 1-7 technical specifications

---

# PART I: QUERY CLASSIFICATION SYSTEM (v3.0)


**Date:** 2025-10-03
**Status:** 🔄 DESIGN REVISION - Response-Format-Driven Taxonomy
**Version:** 3.0 - Consolidated Response-Format-Driven Classification System

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Design Principles](#design-principles)
3. [Response Format Architecture](#response-format-architecture)
4. [Response-Format-Driven Intent Taxonomy](#response-format-driven-intent-taxonomy)
5. [Multi-Dimensional Classification System](#multi-dimensional-classification-system)
6. [Enhanced Confidence Framework](#enhanced-confidence-framework)
7. [Token Optimization (81% Reduction)](#token-optimization)
8. [Intent-to-Response Mapping](#intent-to-response-mapping)
9. [Implementation Architecture](#implementation-architecture)
10. [Migration from v2.0](#migration-from-v20)
11. [Testing & Validation](#testing--validation)
12. [Monitoring & Metrics](#monitoring--metrics)

---

## Executive Summary

### Design Philosophy

FaultMaven v3.0 implements a **response-format-driven classification system** where the taxonomy is designed **backward from response requirements** rather than forward from semantic categories. This ensures every intent class maps to a distinct response format, eliminating redundancy and improving maintainability.

**Core Design Principle:**
> **"Classification exists to drive response formatting. Therefore, intent categories must align 1:N with ResponseType formats, not with arbitrary semantic groupings."**

### Key Improvements in v3.0

**From v2.0 → v3.0:**
- ✅ **Intent consolidation**: 20 intents → 17 intents (including UNKNOWN fallback, 15% reduction)
- ✅ **Perfect alignment**: ~1.55 intents per ResponseType (9 response formats)
- ✅ **Zero redundancy**: Every intent has distinct response format requirement
- ✅ **Clear mappings**: Explicit intent → ResponseType mapping rules
- ✅ **Unified urgency**: Standardized MEDIUM (not NORMAL) across all modules
- ✅ **Added DEPLOYMENT**: New structured action category

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  Step 1: Query Classification (Multi-Dimensional)                   │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Intent: 17 categories (incl. UNKNOWN) aligned with 11 ResponseType formats    │   │
│  │ Complexity: simple, moderate, complex, expert                │   │
│  │ Domain: 9 technical domains                                  │   │
│  │ Urgency: low, medium, high, critical                         │   │
│  │ Confidence: 0.0-1.0 (multi-dimensional scoring)             │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Step 2: Confidence-Based LLM Decision (3-Tier Framework)           │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ HIGH (≥0.7):   Skip LLM, use pattern-only (75% of queries)  │   │
│  │ MEDIUM (0.4-0.7): Call LLM, add self-correction prompt      │   │
│  │ LOW (<0.4):    Call LLM, force clarification response       │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Step 3: Intent → ResponseType Mapping                              │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ 10 Simple Intents   → ANSWER (natural prose)                │   │
│  │ 3 Action Intents    → PLAN_PROPOSAL (numbered steps)        │   │
│  │ 1 Diagnostic Intent → Dynamic (workflow-driven)             │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Step 4: Tiered Prompt Assembly (81% Token Reduction)               │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ MINIMAL (30 tokens):   ANSWER responses                      │   │
│  │ BRIEF (90 tokens):     Simple troubleshooting                │   │
│  │ STANDARD (210 tokens): Complex troubleshooting               │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### Impact Metrics

| Metric | v2.0 | v3.0 Target | Improvement |
|--------|------|-------------|-------------|
| **Intent Categories** | 20 inconsistent | 16 aligned | 20% reduction |
| **Response Formats** | 7 | 9 | +2 (visual formats) |
| **Intent-Response Mapping Clarity** | Ambiguous | 1:N explicit | 100% coverage |
| **Code Redundancy** | High | Zero | N/A |
| **Test Maintenance** | Complex | Simplified | -40% test cases |
| **Avg Prompt Tokens** | 1,165 | 1,165 | Maintained |
| **LLM Call Reduction** | 75% | 75% | Maintained |
| **Classification Accuracy** | ~95% | >95% | +5% target |

---

## Design Principles

### 1. Response-Format-First Design

**Traditional approach (v2.0):**
```
Semantic categories → Intent taxonomy → Map to ResponseType → Generate response
Problem: Many intents map to same ResponseType (redundancy)
```

**v3.0 approach (response-format-driven):**
```
Define ResponseType formats → Required intent categories → Pattern design → Classification
Benefit: Every intent has distinct response format requirement
```

### 2. Intent-ResponseType Alignment Rule

**Design constraint:** Each intent must either:
1. **Map uniquely to a ResponseType** (e.g., `CONFIGURATION` → `PLAN_PROPOSAL`)
2. **Map to same ResponseType as similar intents** (e.g., `INFORMATION`, `STATUS_CHECK` → `ANSWER`)
3. **Trigger dynamic ResponseType** based on workflow state (e.g., `TROUBLESHOOTING`)

**Anti-pattern:** Multiple intents with identical response formats AND similar semantics
- ❌ `EXPLANATION` + `INFORMATION` both → `ANSWER` with identical format
- ❌ `PROBLEM_RESOLUTION` + `ROOT_CAUSE_ANALYSIS` both → `PLAN_PROPOSAL` with same workflow

### 3. Separation of Concerns

**Intent dimension:** WHAT the user wants (semantic meaning)
**Complexity dimension:** HOW difficult the request is (computational complexity)
**Urgency dimension:** HOW quickly response needed (temporal priority)
**Domain dimension:** WHICH technical area (routing & expertise)

**Anti-pattern:** Encoding urgency/complexity in intent names
- ❌ `INCIDENT_RESPONSE` (urgency encoded) → Use `TROUBLESHOOTING` + `urgency=CRITICAL`
- ❌ `COMPLEX_TROUBLESHOOTING` (complexity encoded) → Use `TROUBLESHOOTING` + `complexity=COMPLEX`

### 4. Minimize Taxonomy Surface Area

**Occam's Razor for Classification:**
> "The number of intent categories should be the minimum required to support distinct response formats, and no fewer."

**Calculation:**
- 9 ResponseType formats defined
- ~1.8 intents per ResponseType (avg)
- **Target: 12-18 intent categories**
- **v3.0: 17 intents (including UNKNOWN fallback)** ✅

---

## Response Format Architecture

### Core ResponseType Definitions

FaultMaven supports **9 distinct response formats**, each with specific structural requirements:

```python
class ResponseType(str, Enum):
    """Response format types - defines HOW agent structures its response"""

    # ═══════════════════════════════════════════════════════════
    # FORMAT 1: Natural Prose Answer
    # ═══════════════════════════════════════════════════════════
    ANSWER = "ANSWER"
    """
    Format: Natural conversational prose
    Structure: Direct answer + explanation + examples (optional)
    Tone: Clear, concise, helpful
    Example:
      "Redis is an in-memory data structure store used as a database,
       cache, and message broker. It supports various data structures
       like strings, hashes, lists, and sets. For session storage,
       you'd typically use Redis with string or hash data types..."
    """

    # ═══════════════════════════════════════════════════════════
    # FORMAT 2: Structured Action Plan
    # ═══════════════════════════════════════════════════════════
    PLAN_PROPOSAL = "PLAN_PROPOSAL"
    """
    Format: Numbered steps with exact commands
    Structure:
      1. Goal statement
      2. Step-by-step plan with:
         - Action description
         - Exact command to run
         - Expected output
         - Verification step
      3. Success criteria
    Tone: Confident, structured, actionable
    Example:
      "Here's the plan to configure Redis for session storage:

       1. Install Redis
          $ sudo apt-get install redis-server
          Expected: Installation complete message

       2. Configure session timeout
          $ redis-cli CONFIG SET timeout 1800
          Expected: OK

       3. Verify configuration
          $ redis-cli CONFIG GET timeout
          Expected: 1800"
    """

    # ═══════════════════════════════════════════════════════════
    # FORMAT 3: Information Gathering Questions
    # ═══════════════════════════════════════════════════════════
    CLARIFICATION_REQUEST = "CLARIFICATION_REQUEST"
    """
    Format: 2-3 targeted questions with context
    Structure:
      - Brief acknowledgment of query
      - 2-3 specific questions (what/when/where format)
      - Explanation of why each piece of info is needed
      - Optional: Diagnostic commands to gather info
    Tone: Patient, helpful, not interrogative
    Example:
      "I'd like to help troubleshoot the performance issue. To diagnose
       effectively, I need a few more details:

       1. When did the slowness start? (specific date/time if possible)
       2. Which operations are slow? (queries, writes, connections?)
       3. Have you checked current memory usage?
          $ redis-cli INFO memory

       These details will help identify if it's a resource, configuration,
       or query optimization issue."
    """

    # ═══════════════════════════════════════════════════════════
    # FORMAT 4: Risk Confirmation Dialog
    # ═══════════════════════════════════════════════════════════
    CONFIRMATION_REQUEST = "CONFIRMATION_REQUEST"
    """
    Format: Proposed action + impact + yes/no question
    Structure:
      1. Proposed action summary
      2. Expected impact
      3. Potential risks
      4. Alternatives (if any)
      5. Clear yes/no question
    Tone: Cautious, respectful, informative
    Trigger: Destructive operations, production changes
    Example:
      "You're about to delete the production Redis instance.

       Impact:
       - All session data will be permanently lost
       - ~1,500 active user sessions will be terminated
       - Recovery time: ~10 minutes to restore from backup

       Risks:
       - User disruption during business hours
       - Potential data loss if backup is stale

       Alternative: Consider flushing specific keys instead:
       $ redis-cli --scan --pattern 'session:*' | xargs redis-cli DEL

       Do you want to proceed with full instance deletion?"
    """

    # ═══════════════════════════════════════════════════════════
    # FORMAT 5: Complete Solution Package
    # ═══════════════════════════════════════════════════════════
    SOLUTION_READY = "SOLUTION_READY"
    """
    Format: Root cause + solution + verification + prevention
    Structure:
      1. Root cause explanation
      2. Solution summary (what will be fixed)
      3. Implementation steps (detailed)
      4. Verification steps (how to confirm it worked)
      5. Prevention recommendations (avoid future occurrence)
    Tone: Confident, comprehensive, educational
    Trigger: Workflow state = solution identified
    Example:
      "Root Cause:
       Redis is evicting keys due to maxmemory policy set to 'volatile-lru'
       combined with insufficient memory allocation.

       Solution:
       Increase maxmemory limit and adjust eviction policy.

       Implementation:
       1. Increase maxmemory to 4GB...
       2. Change eviction policy to allkeys-lru...

       Verification:
       $ redis-cli INFO memory | grep maxmemory
       Expected: maxmemory:4294967296

       Prevention:
       - Set up memory usage alerts at 80% threshold
       - Review memory usage weekly
       - Consider Redis cluster for horizontal scaling"
    """

    # ═══════════════════════════════════════════════════════════
    # FORMAT 6: Diagnostic Data Request
    # ═══════════════════════════════════════════════════════════
    NEEDS_MORE_DATA = "NEEDS_MORE_DATA"
    """
    Format: Specific data/logs needed with commands
    Structure:
      1. List of required data
      2. Exact commands to gather each piece
      3. Explanation of why each is needed
      4. How to share safely (redaction guidance)
    Tone: Technical, specific, security-conscious
    Trigger: Workflow state = need diagnostic data
    Example:
      "To diagnose the Redis connection issue, I need:

       1. Redis server logs (last 100 lines)
          $ tail -n 100 /var/log/redis/redis-server.log
          Why: Check for connection errors or authentication failures

       2. Current Redis configuration
          $ redis-cli CONFIG GET '*'
          Why: Verify bind address and port settings

       3. Network connectivity test
          $ redis-cli -h <host> -p <port> PING
          Why: Confirm network path is open

       Note: Redact any password values before sharing."
    """

    # ═══════════════════════════════════════════════════════════
    # FORMAT 7: Escalation Handoff
    # ═══════════════════════════════════════════════════════════
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"
    """
    Format: Limitations + attempts + escalation path + summary
    Structure:
      1. Honest statement of limitations
      2. Summary of troubleshooting attempts
      3. Why escalation is needed
      4. Recommended escalation path (who/how)
      5. Summary package for next responder
    Tone: Honest, supportive, professional
    Trigger: Workflow state = exhausted options OR capability limit
    Example:
      "I've reached the limit of what I can diagnose remotely.

       Troubleshooting completed:
       - Verified Redis configuration (correct)
       - Checked network connectivity (successful)
       - Analyzed error logs (no clear cause)
       - Tested with different clients (same issue)

       Why escalation needed:
       The intermittent connection drops suggest a deeper infrastructure
       issue that requires server-level access to diagnose (kernel logs,
       network packet capture, hardware diagnostics).

       Recommended next steps:
       1. Contact infrastructure team for server diagnostics
       2. Request packet capture during next occurrence
       3. Review load balancer logs if Redis is behind LB

       Summary for infrastructure team:
       [Concise problem statement + key findings + timeline]"
    """

    # ═══════════════════════════════════════════════════════════
    # FORMAT 8: Visual Diagram/Architecture (NEW in v3.0)
    # ═══════════════════════════════════════════════════════════
    VISUAL_DIAGRAM = "VISUAL_DIAGRAM"
    """
    Format: Mermaid diagram code or ASCII art for visualization
    Structure:
      1. Brief context/description
      2. Diagram code (Mermaid syntax)
      3. Key components explanation
      4. Optional: How to render/use the diagram
    Tone: Technical, visual, structured
    Trigger: User requests architecture, flow, or visual explanation
    Example:
      "Here's the Redis Sentinel architecture:

       ```mermaid
       graph TB
           Client[Client Application]
           Sentinel1[Sentinel 1]
           Sentinel2[Sentinel 2]
           Sentinel3[Sentinel 3]
           Master[Redis Master]
           Slave1[Redis Slave 1]
           Slave2[Redis Slave 2]

           Client -->|Query| Sentinel1
           Sentinel1 -->|Monitor| Master
           Sentinel2 -->|Monitor| Master
           Sentinel3 -->|Monitor| Master
           Master -->|Replicate| Slave1
           Master -->|Replicate| Slave2
           Sentinel1 -.->|Failover Decision| Slave1
       ```

       Key Components:
       - **Sentinels (3)**: Monitor master health, coordinate failover
       - **Master**: Primary Redis instance handling writes
       - **Slaves (2)**: Replicas that can be promoted to master
       - **Client**: Queries Sentinel for current master location

       The Sentinels use quorum-based voting (2 of 3) to detect failures
       and elect a new master automatically."
    """

    # ═══════════════════════════════════════════════════════════
    # FORMAT 9: Comparison Table (NEW in v3.0)
    # ═══════════════════════════════════════════════════════════
    COMPARISON_TABLE = "COMPARISON_TABLE"
    """
    Format: Markdown table comparing options, features, or approaches
    Structure:
      1. Context of what's being compared
      2. Comparison table (markdown format)
      3. Key insights or recommendations
      4. When to choose each option
    Tone: Analytical, objective, decision-oriented
    Trigger: User asks to compare, evaluate alternatives, or choose between options
    Example:
      "Here's a comparison of Redis persistence strategies:

       | Feature | RDB (Snapshots) | AOF (Append-Only File) | RDB + AOF (Hybrid) |
       |---------|----------------|----------------------|-------------------|
       | **Durability** | Low (minutes of data loss) | High (1-2 seconds loss) | Highest (best of both) |
       | **Performance** | Fast (minimal overhead) | Slower (fsync overhead) | Moderate |
       | **File Size** | Compact | Large (grows continuously) | Both files maintained |
       | **Recovery Speed** | Fast | Slow (replay all ops) | Fast (RDB) with safety (AOF) |
       | **Best For** | Backup, testing | Production (critical data) | Production (optimal balance) |

       **Key Insights:**
       - RDB is fastest but risks data loss during crashes
       - AOF provides strong durability but larger files
       - Hybrid mode (RDB+AOF) combines benefits at cost of complexity

       **Recommendations:**
       - **Development/Testing**: RDB only (performance priority)
       - **Production (critical data)**: RDB + AOF hybrid (balanced)
       - **Production (can tolerate loss)**: RDB with frequent snapshots
       - **Session storage**: RDB (sessions are ephemeral)
       - **Financial transactions**: AOF with appendfsync=always (maximum safety)"
    """
```

### Response Format Selection Logic

```python
def select_response_format(
    intent: QueryIntent,
    workflow_state: WorkflowState,
    confidence: float,
    completeness: float,
    risk_level: str
) -> ResponseType:
    """
    Select response format based on intent and dynamic factors

    Priority order:
    1. Risk-based overrides (CONFIRMATION_REQUEST for destructive ops)
    2. Confidence-based overrides (CLARIFICATION_REQUEST if confidence < 0.4)
    3. Completeness-based overrides (CLARIFICATION_REQUEST if info incomplete)
    4. Workflow state (for TROUBLESHOOTING intent)
    5. Intent-based mapping (default behavior)
    """

    # Override 1: Risk confirmation
    if risk_level in ["high", "critical"]:
        return ResponseType.CONFIRMATION_REQUEST

    # Override 2: Low confidence → force clarification
    if confidence < 0.4:
        return ResponseType.CLARIFICATION_REQUEST

    # Override 3: Incomplete information
    if completeness < 0.3:
        return ResponseType.CLARIFICATION_REQUEST

    # Override 4: Workflow state (for TROUBLESHOOTING)
    if intent == QueryIntent.TROUBLESHOOTING:
        return _select_troubleshooting_response(workflow_state)

    # Default: Intent-based mapping
    return INTENT_TO_RESPONSE_MAPPING[intent]


def _select_troubleshooting_response(workflow_state: WorkflowState) -> ResponseType:
    """Dynamic response selection for troubleshooting workflow"""

    if workflow_state == WorkflowState.NEEDS_CLARIFICATION:
        return ResponseType.CLARIFICATION_REQUEST

    elif workflow_state == WorkflowState.NEEDS_DATA:
        return ResponseType.NEEDS_MORE_DATA

    elif workflow_state == WorkflowState.SOLUTION_IDENTIFIED:
        return ResponseType.SOLUTION_READY

    elif workflow_state == WorkflowState.EXHAUSTED_OPTIONS:
        return ResponseType.ESCALATION_REQUIRED

    else:  # Initial state
        return ResponseType.CLARIFICATION_REQUEST
```

---

## Response-Format-Driven Intent Taxonomy

### Design Process

**Step 1: Start with ResponseType formats (7 formats defined above)**

**Step 2: Identify required intent triggers for each format**

**Step 3: Consolidate intents with identical response requirements**

**Step 4: Validate no redundancy (semantic AND format overlap)**

### v3.0 Intent Taxonomy (16 Categories)

```python
class QueryIntent(str, Enum):
    """
    User query intent categories - designed to align with ResponseType formats

    Design constraint: Each intent must map to a distinct response format OR
    share a format with semantically different intents.
    """

    # ═══════════════════════════════════════════════════════════════════
    # GROUP 1: SIMPLE ANSWER INTENTS (10 intents → ResponseType.ANSWER)
    # Response format: Natural conversational prose
    # ═══════════════════════════════════════════════════════════════════

    INFORMATION = "information"
    """
    Informational questions: What/How/Why/Explain

    Merged from v2.0:
    - EXPLANATION ("How does X work?")
    - INFORMATION ("What is X?")
    - DOCUMENTATION ("Docs for X?")

    Rationale: All three mapped to ANSWER with identical format (natural prose
    explanation). Semantic difference not meaningful for response generation.

    Examples:
    - "What is Redis?"
    - "How does Redis persistence work?"
    - "Explain Redis Sentinel"
    - "Documentation for Redis clustering"

    Pattern weight: High (2.0) for question words + informational verbs
    """

    STATUS_CHECK = "status_check"
    """
    System status and health checks

    Merged from v2.0:
    - STATUS_CHECK ("Is service running?")
    - MONITORING ("Check metrics")

    Rationale: Both query current system state with simple answer format.
    Difference is scope (single check vs continuous monitoring), not response format.

    Examples:
    - "Is Redis running?"
    - "Check cluster health"
    - "Redis memory usage?"
    - "Show current connections"

    Pattern weight: High (1.8) for status/health/running/check keywords
    """

    PROCEDURAL = "procedural"
    """
    How-to and capability questions (simple guidance, not complex setup)

    Key distinction from CONFIGURATION:
    - PROCEDURAL: "Can I do X?" / "How do I Y?" (simple answer)
    - CONFIGURATION: "Set up X" / "Configure Y" (structured steps)

    Examples:
    - "Can I use Redis as a message queue?"
    - "How do I connect to Redis?"
    - "Is it possible to run Redis and MongoDB together?"
    - "Can I expire specific keys?"

    Pattern weight: Very high (2.0) for "can I", "how do I", "is it possible"
    Exclusion rules: Excludes active problems ("my X is broken")
    """

    VALIDATION = "validation"
    """
    Confirmation questions about specific approaches

    User has hypothesis/plan and wants validation.

    Key distinction from PROCEDURAL:
    - PROCEDURAL: "Can I do X?" (general capability)
    - VALIDATION: "Will doing X this way work?" (specific approach)

    Examples:
    - "This won't work, right?"
    - "Will this approach be correct?"
    - "Is this the right way to configure persistence?"
    - "Should I use AOF or RDB for backups?"

    Pattern weight: Very high (2.0) for "will this work", "is this correct"
    Exclusion rules: Excludes actual active problems
    """

    BEST_PRACTICES = "best_practices"
    """
    Recommendations and industry standards

    Examples:
    - "What's the recommended Redis configuration for production?"
    - "Best practices for Redis security?"
    - "Industry standard for Redis clustering?"

    Pattern weight: Very high (2.0) for "best practice", "recommended approach"
    """

    GREETING = "greeting"
    """
    Conversational greetings

    Examples: "Hi", "Hello", "Hey", "Good morning"

    Pattern weight: Very high (2.0) for greeting keywords at start of query
    """

    GRATITUDE = "gratitude"
    """
    Thank you messages

    Examples: "Thanks", "Thank you", "Appreciate it"

    Pattern weight: Very high (2.0) for thanks/gratitude keywords
    """

    OFF_TOPIC = "off_topic"
    """
    Non-technical queries outside FaultMaven scope

    Examples: "What's the weather?", "Recipe for cookies", "Movie recommendations"

    Response: Polite boundary with redirect to technical topics
    Pattern weight: Medium (1.5) for non-technical domains
    """

    META_FAULTMAVEN = "meta_faultmaven"
    """
    Questions about FaultMaven itself

    Examples:
    - "What can you do?"
    - "How do you work?"
    - "What are your capabilities?"
    - "Tell me about FaultMaven"

    Pattern weight: Very high (2.0) for "you", "faultmaven", "your capabilities"
    """

    CONVERSATION_CONTROL = "conversation_control"
    """
    Session management commands

    Examples: "Start over", "Reset", "Go back", "Skip", "Cancel"

    Pattern weight: Very high (2.0) for control keywords
    """

    # ═══════════════════════════════════════════════════════════════════
    # GROUP 2: STRUCTURED PLAN INTENTS (3 intents → ResponseType.PLAN_PROPOSAL)
    # Response format: Numbered steps with exact commands
    # ═══════════════════════════════════════════════════════════════════

    CONFIGURATION = "configuration"
    """
    System setup and configuration changes

    Requires step-by-step execution plan.

    Examples:
    - "Configure Redis for session storage"
    - "Set up Redis clustering"
    - "Enable Redis persistence"
    - "Configure Redis authentication"

    Pattern weight: High (1.8) for configure/setup/install keywords
    """

    OPTIMIZATION = "optimization"
    """
    Performance tuning and improvements

    Requires structured analysis + implementation plan.

    Examples:
    - "Optimize Redis memory usage"
    - "Improve Redis query performance"
    - "Speed up Redis response time"
    - "Tune Redis for high throughput"

    Pattern weight: High (1.8) for optimize/performance/speed/improve keywords
    """

    DEPLOYMENT = "deployment"
    """
    Deployment, rollout, and release operations (NEW in v3.0)

    Requires structured deployment plan with verification steps.

    Added because: Deployment is a distinct action category requiring
    careful sequencing, rollback planning, and health checks - different
    from configuration (one-time setup) and optimization (tuning existing).

    Examples:
    - "Deploy Redis to Kubernetes"
    - "Roll out new Redis version"
    - "Release Redis configuration changes"
    - "Migrate Redis to new cluster"

    Pattern weight: High (1.8) for deploy/rollout/release/migrate keywords
    """

    # ═══════════════════════════════════════════════════════════════════
    # GROUP 3: VISUAL RESPONSE INTENTS (2 intents → Specialized ResponseTypes)
    # Response formats: VISUAL_DIAGRAM and COMPARISON_TABLE
    # ═══════════════════════════════════════════════════════════════════

    VISUALIZATION = "visualization"
    """
    Architecture diagrams, flowcharts, system diagrams (NEW in v3.0)

    Requires visual representation using Mermaid or ASCII art.

    Added because: Some concepts (architecture, data flow, relationships)
    are best explained visually rather than in prose. This triggers
    structured diagram generation.

    Examples:
    - "Show me the Redis Sentinel architecture"
    - "Draw a diagram of Redis replication flow"
    - "Visualize how Redis clustering works"
    - "Architecture diagram for Redis with Kubernetes"

    Response: VISUAL_DIAGRAM with Mermaid code + component explanations
    Pattern weight: Very high (2.0) for "diagram", "visualize", "show me", "draw"
    """

    COMPARISON = "comparison"
    """
    Feature/option comparisons, trade-off analysis (NEW in v3.0)

    Requires structured comparison table with recommendations.

    Added because: Decision-making queries (which option to choose, pros/cons)
    benefit from tabular comparison format rather than narrative explanation.

    Examples:
    - "Compare Redis RDB vs AOF persistence"
    - "What's the difference between Redis and Memcached?"
    - "Redis Cluster vs Sentinel - which should I use?"
    - "Pros and cons of different Redis deployment modes"

    Response: COMPARISON_TABLE with markdown table + recommendations
    Pattern weight: Very high (2.0) for "compare", "vs", "difference between", "pros and cons"
    """

    # ═══════════════════════════════════════════════════════════════════
    # GROUP 4: DIAGNOSTIC INTENT (1 intent → Dynamic ResponseType)
    # Response format: Depends on workflow state
    # ═══════════════════════════════════════════════════════════════════

    TROUBLESHOOTING = "troubleshooting"
    """
    Active problem diagnosis and resolution

    Merged from v2.0:
    - TROUBLESHOOTING (diagnosis)
    - PROBLEM_RESOLUTION (solution implementation)
    - ROOT_CAUSE_ANALYSIS (deep investigation)
    - INCIDENT_RESPONSE (urgent issues)

    Rationale: All four are phases of the same diagnostic workflow:
    1. Identify problem (TROUBLESHOOTING)
    2. Investigate cause (ROOT_CAUSE_ANALYSIS)
    3. Implement solution (PROBLEM_RESOLUTION)
    4. Handle urgent issues (INCIDENT_RESPONSE)

    Urgency is handled via urgency dimension (low/medium/high/critical),
    not by creating separate intent categories.

    Response format varies by workflow state:
    - Initial state → CLARIFICATION_REQUEST (gather context)
    - Need data → NEEDS_MORE_DATA (request diagnostics)
    - Solution found → SOLUTION_READY (present fix)
    - Failed → ESCALATION_REQUIRED (handoff)

    Examples:
    - "Redis is crashing" (urgency=high)
    - "Intermittent connection errors" (urgency=medium)
    - "Production outage - Redis down!" (urgency=critical)
    - "Why is Redis slow?" (investigation)
    - "Fix the memory leak" (solution implementation)

    Pattern weight: Very high (2.0) for "broken", "failing", "error", "not working"
    Exclusion rules: Excludes hypothetical questions ("will this work?")
    """

    # ═══════════════════════════════════════════════════════════════════
    # GROUP 5: FALLBACK (1 intent → ResponseType.CLARIFICATION_REQUEST)
    # ═══════════════════════════════════════════════════════════════════

    UNKNOWN = "unknown"
    """
    Unable to classify query

    Response: Request clarification with interpretation options

    Triggered when:
    - Pattern confidence = 0 (no matches)
    - LLM fails to classify
    - Ambiguous query

    Pattern weight: N/A (fallback only)
    """
```

### Intent Group Summary

| Group | Count | ResponseType | Notes |
|-------|-------|--------------|-------|
| **Simple Answer** | 10 | `ANSWER` | Natural prose, no structure required |
| **Structured Plan** | 3 | `PLAN_PROPOSAL` | Numbered steps, commands, verification |
| **Visual Response** | 2 | `VISUAL_DIAGRAM`, `COMPARISON_TABLE` | Diagrams and comparison tables |
| **Diagnostic** | 1 | Dynamic | Workflow-state-driven response selection |
| **Fallback** | 1 | `CLARIFICATION_REQUEST` | Unable to classify |
| **TOTAL** | **16** | 9 formats | ~1.8 intents per ResponseType ✅ |

---

## Multi-Dimensional Classification System

Classification operates across **4 independent dimensions**, each serving a distinct purpose:

### Dimension 1: Intent (WHAT user wants)

**Purpose:** Determine semantic category to drive response format selection

**Values:** 14 categories (detailed above)

**Primary driver:** ResponseType selection

**Example:** `TROUBLESHOOTING` → Dynamic ResponseType based on workflow

---

### Dimension 2: Complexity (HOW difficult)

**Purpose:** Determine computational complexity and prompt tier selection

**Values:**
```python
class ComplexityLevel(str, Enum):
    SIMPLE = "simple"        # Single-step, well-defined
                            # Example: "What is Redis?"
                            # Prompt tier: MINIMAL (30 tokens)

    MODERATE = "moderate"    # Multi-step with clear dependencies
                            # Example: "Configure Redis clustering"
                            # Prompt tier: BRIEF (90 tokens)

    COMPLEX = "complex"      # Requires investigation and analysis
                            # Example: "Diagnose intermittent connection issues"
                            # Prompt tier: STANDARD (210 tokens)

    EXPERT = "expert"        # Multi-system, high expertise required
                            # Example: "Migrate multi-region Redis cluster with zero downtime"
                            # Prompt tier: STANDARD (210 tokens) + expert patterns
```

**Primary driver:** Token optimization (prompt tier selection)

**Assessment factors:**
- Query length (words, clauses)
- Technical depth (specific terms, version numbers)
- Scope breadth (single system vs multi-system)
- Conditional language ("if", "when", "unless")

**Independence from intent:** A `TROUBLESHOOTING` query can be SIMPLE ("Redis won't start") or EXPERT ("Distributed Redis cluster split-brain resolution")

---

### Dimension 3: Domain (WHICH technical area)

**Purpose:** Route to specialized knowledge and domain experts

**Values:**
```python
class TechnicalDomain(str, Enum):
    DATABASE = "database"           # SQL/NoSQL databases, Redis, MongoDB
    NETWORKING = "networking"       # TCP/IP, DNS, firewalls, load balancers
    APPLICATION = "application"     # App code, services, APIs
    INFRASTRUCTURE = "infrastructure"  # Servers, VMs, cloud resources
    SECURITY = "security"           # Auth, encryption, vulnerabilities
    PERFORMANCE = "performance"     # Optimization, profiling, bottlenecks
    MONITORING = "monitoring"       # Metrics, logs, observability
    DEPLOYMENT = "deployment"       # CI/CD, releases, rollouts
    GENERAL = "general"            # Cross-domain or unspecified
```

**Primary driver:**
- Knowledge base retrieval (domain-specific docs)
- Tool selection (domain-specific diagnostic tools)
- Escalation routing (domain experts)

**Pattern matching:** Keyword-based domain detection
```python
domain_patterns = {
    TechnicalDomain.DATABASE: [
        r'\b(database|db|sql|mysql|postgres|mongodb|redis|query)\b',
        r'\b(table|schema|index|connection)\b'
    ],
    # ... other domains
}
```

**Independence from intent:** `TROUBLESHOOTING` can span any domain (DATABASE troubleshooting vs NETWORKING troubleshooting)

---

### Dimension 4: Urgency (HOW quickly)

**Purpose:** Prioritize response and adjust tone/format for time-sensitive issues

**Values:**
```python
class UrgencyLevel(str, Enum):
    LOW = "low"              # No time pressure
                            # Example: "When you get a chance, explain Redis persistence"
                            # Response: Standard pace, educational tone

    MEDIUM = "medium"        # Normal business priority (DEFAULT)
                            # Example: "Redis is running slow"
                            # Response: Efficient troubleshooting, normal pace

    HIGH = "high"           # Important, blocking work
                            # Example: "Redis errors blocking deployment"
                            # Response: Prioritized, focused troubleshooting

    CRITICAL = "critical"   # Production impact, immediate action needed
                            # Example: "Production Redis down - users affected!"
                            # Response: Emergency mode, rapid triage
```

**Primary driver:**
- Response prioritization (queue ordering)
- Tone adjustment (urgent vs educational)
- Escalation thresholds (critical → faster escalation)

**Pattern matching:**
```python
urgency_patterns = {
    UrgencyLevel.CRITICAL: [
        r'\b(urgent|critical|emergency|production down|outage)\b',
        r'\b(asap|immediately|right now|critical issue)\b'
    ],
    UrgencyLevel.HIGH: [
        r'\b(important|high priority|blocking|stuck)\b',
        r'\b(customers affected|users complaining)\b'
    ]
}
```

**Tone adjustment example:**
```python
# LOW urgency
"Let me explain how Redis persistence works. There are two main approaches..."

# CRITICAL urgency
"Redis production outage detected. Immediate actions:
1. Check Redis process: systemctl status redis
2. Review logs: tail -100 /var/log/redis/redis.log
3. Verify connectivity: redis-cli PING
Execute now and report results."
```

**Independence from intent:** `CONFIGURATION` can be LOW urgency (planned setup) or HIGH urgency (emergency reconfiguration)

---

### Dimension Independence Validation

**Example query:** "Production Redis is slow - need to optimize ASAP"

```python
classification = {
    "intent": QueryIntent.OPTIMIZATION,        # WHAT: Performance tuning
    "complexity": ComplexityLevel.MODERATE,    # HOW difficult: Multi-step tuning
    "domain": TechnicalDomain.DATABASE,        # WHICH area: Database/Redis
    "urgency": UrgencyLevel.HIGH,             # HOW quickly: High priority
    "confidence": 0.85                        # Classification certainty
}

# Each dimension contributes independently:
# - Intent → ResponseType.PLAN_PROPOSAL (structured optimization plan)
# - Complexity → BRIEF prompt tier (90 tokens)
# - Domain → Load Redis-specific optimization patterns
# - Urgency → Adjust tone ("prioritized", "focus on quick wins")
```

**Anti-pattern (v2.0 redundancy):**
```python
# ❌ BAD: Encoding urgency in intent
intent = QueryIntent.INCIDENT_RESPONSE  # Urgency + intent mixed

# ✅ GOOD: Separate dimensions
intent = QueryIntent.TROUBLESHOOTING
urgency = UrgencyLevel.CRITICAL
```

---

## Enhanced Confidence Framework

### Multi-Dimensional Confidence Scoring

Confidence is calculated from **5 independent factors** beyond simple pattern matching:

```python
def calculate_confidence(
    pattern_score: float,           # Base pattern matching score
    query: str,
    all_intents: Dict[str, Any],
    entities: List[Dict],
    context: Dict[str, Any]
) -> float:
    """
    Calculate enhanced confidence score

    Formula:
    final_confidence = base_confidence + Σ(factor_boosts) - disambiguation_penalty
    clamped to [0.0, 1.0]
    """

    base_confidence = pattern_score  # From weighted pattern matching

    # Factor 1: Query structure analysis (-0.2 to +0.2)
    structure_boost = assess_query_structure(query)

    # Factor 2: Linguistic markers (0 to +0.15)
    linguistic_boost = assess_linguistic_markers(query)

    # Factor 3: Entity presence (0 to +0.15)
    entity_boost = min(len(entities) * 0.05, 0.15)

    # Factor 4: Conversation context (-0.1 to +0.1)
    context_boost = assess_conversation_context(context)

    # Factor 5: Cross-intent disambiguation (-0.3 to 0)
    disambiguation_penalty = assess_cross_intent_ambiguity(all_intents)

    # Combine all factors
    enhanced_confidence = (
        base_confidence
        + structure_boost
        + linguistic_boost
        + entity_boost
        + context_boost
        + disambiguation_penalty
    )

    # Clamp to valid range
    return max(0.0, min(1.0, enhanced_confidence))
```

### Confidence Factor Details

#### Factor 1: Query Structure Analysis (-0.2 to +0.2)

**Purpose:** Assess query completeness and clarity

**Positive indicators (+boost):**
- Ends with question mark (`+0.1`)
- Contains question word (what/how/why) (`+0.05`)
- Reasonable length (≥5 words) (`+0.05`)

**Negative indicators (-penalty):**
- Too short (≤2 words) (`-0.1`)
- Vague reference ("it", "this") at start (`-0.1`)

```python
def assess_query_structure(query: str) -> float:
    boost = 0.0

    if re.search(r'\?\s*$', query):  # Ends with ?
        boost += 0.1
    if len(query.split()) >= 5:      # Reasonable length
        boost += 0.05
    if re.search(r'\b(what|how|why|when|where|who)\b', query, re.IGNORECASE):
        boost += 0.05

    if len(query.split()) <= 2:      # Too short
        boost -= 0.1
    if re.match(r'^(it|this|that)\b', query, re.IGNORECASE):  # Vague reference
        boost -= 0.1

    return boost
```

**Example:**
- "Redis slow" → structure_boost = -0.1 (too short)
- "Why is Redis running slowly?" → structure_boost = +0.2 (question word + ?)

---

#### Factor 2: Linguistic Markers (0 to +0.15)

**Purpose:** Detect clear linguistic intent signals

**Indicators:**
- Action verbs: "troubleshoot", "fix", "configure" (`+0.05`)
- Technical specificity: "pod", "deployment", "cluster" (`+0.05`)
- Temporal markers: "since", "when", "started" (`+0.05`)

```python
def assess_linguistic_markers(query: str) -> float:
    boost = 0.0

    action_verbs = [
        r'\b(troubleshoot|fix|solve|resolve|diagnose|analyze)\b',
        r'\b(configure|setup|install|deploy|update)\b',
        r'\b(check|verify|validate|confirm|test)\b'
    ]
    for pattern in action_verbs:
        if re.search(pattern, query, re.IGNORECASE):
            boost += 0.05
            break

    if re.search(r'\b(pod|service|deployment|container|node|cluster)\b', query, re.IGNORECASE):
        boost += 0.05

    if re.search(r'\b(since|after|when|started|began|yesterday|today)\b', query, re.IGNORECASE):
        boost += 0.05

    return min(boost, 0.15)
```

---

#### Factor 3: Entity Presence (0 to +0.15)

**Purpose:** Technical entities indicate specific, well-formed queries

**Detected entities:**
- IP addresses: `192.168.1.100`
- Ports: `port 6379`
- URLs: `https://api.example.com`
- File paths: `/etc/redis/redis.conf`
- Error codes: `error 500`
- Versions: `v1.2.3`

```python
entity_boost = min(len(entities) * 0.05, 0.15)
```

**Example:**
- "Redis slow" → 0 entities → entity_boost = 0
- "Redis at 192.168.1.100:6379 returning error 500" → 3 entities → entity_boost = +0.15

---

#### Factor 4: Conversation Context (-0.1 to +0.1)

**Purpose:** Leverage conversation history for better classification

**Positive signals:**
- Has previous context (`+0.05`)
- Is follow-up question (`+0.05`)

**Negative signals:**
- Conflicting context (`-0.1`)

```python
def assess_conversation_context(context: Dict[str, Any]) -> float:
    boost = 0.0

    if context.get("has_previous_context", False):
        boost += 0.05

    if context.get("is_followup", False):
        boost += 0.05

    if context.get("context_conflict", False):
        boost -= 0.1

    return boost
```

**Example:**
```
User: "How do I configure Redis?"
Bot: "Here's how to configure Redis..."
User: "What about clustering?" (followup=True, context boost = +0.1)
```

---

#### Factor 5: Cross-Intent Disambiguation (-0.3 to 0)

**Purpose:** Penalize ambiguous queries matching multiple intents

**Calculation:** Compare top 2 intent scores

```python
def assess_cross_intent_ambiguity(all_intents: Dict[str, Any]) -> float:
    if not all_intents or len(all_intents) <= 1:
        return 0.0  # No ambiguity

    sorted_intents = sorted(
        all_intents.items(),
        key=lambda x: x[1].get("weighted_score", 0),
        reverse=True
    )

    if len(sorted_intents) < 2:
        return 0.0

    top_score = sorted_intents[0][1].get("weighted_score", 0)
    second_score = sorted_intents[1][1].get("weighted_score", 0)

    if top_score == 0:
        return -0.3  # No clear winner

    ratio = second_score / top_score

    # High ambiguity (scores are close)
    if ratio > 0.8:
        return -0.3
    elif ratio > 0.6:
        return -0.2
    elif ratio > 0.4:
        return -0.1

    return 0.0  # Clear winner
```

**Example:**
```python
# Clear classification
all_intents = {
    "troubleshooting": {"weighted_score": 2.5},
    "information": {"weighted_score": 0.3}
}
# ratio = 0.3 / 2.5 = 0.12 → penalty = 0 (clear winner)

# Ambiguous classification
all_intents = {
    "procedural": {"weighted_score": 1.2},
    "validation": {"weighted_score": 1.0}
}
# ratio = 1.0 / 1.2 = 0.83 → penalty = -0.3 (ambiguous)
```

---

### Three-Tier Confidence Framework

Based on final confidence score, queries are categorized into 3 tiers:

```python
class ConfidenceTier(str, Enum):
    HIGH = "high"      # ≥ 0.7: Strong pattern match
    MEDIUM = "medium"  # 0.4 - 0.7: Moderate confidence
    LOW = "low"        # < 0.4: Weak or ambiguous
```

#### Tier 1: HIGH Confidence (≥ 0.7)

**Characteristics:**
- Strong pattern match (weighted score high)
- Clear query structure
- Specific technical details
- No cross-intent ambiguity

**LLM Decision:** Skip LLM classification (pattern-only sufficient)

**Prompt Strategy:** Standard processing, no self-correction needed

**Example:**
```
Query: "My Redis pod is crashing with OOM error"
Confidence breakdown:
- Base (pattern): 0.65 (TROUBLESHOOTING patterns matched)
- Structure: +0.1 (clear statement)
- Linguistic: +0.1 (specific: "pod", "OOM error")
- Entity: +0.05 (1 entity: "OOM error")
- Context: 0
- Disambiguation: 0 (clear winner)
Final: 0.90 (HIGH tier)

LLM call: SKIPPED
Response: Proceed with troubleshooting workflow
```

**Performance:** ~75% of queries achieve HIGH confidence

---

#### Tier 2: MEDIUM Confidence (0.4 - 0.7)

**Characteristics:**
- Moderate pattern match
- Some ambiguity or vagueness
- Missing context or details

**LLM Decision:** Call LLM for enhanced classification

**Prompt Strategy:** Add self-correction instruction

**Self-Correction Prompt:**
```
⚠️ **Classification Uncertainty Detected** (Confidence: 55%)

The automated classification has moderate uncertainty about this query's intent.

**Before responding, validate:**
1. **Does this seem like a genuine {detected_intent} query?**
   - If it's actually a simple yes/no question → respond directly
   - If it's asking "will X work?" → provide brief validation
   - If it's reporting active problem → follow {response_type} format

2. **Self-Correction Protocol:**
   - If classification seems wrong → acknowledge briefly and respond appropriately
   - Example: "This appears to be a validation question. [Direct answer]"
   - If classification seems reasonable → proceed with {response_type} format

**Current classification**: {intent} → {response_type}
```

**Example:**
```
Query: "Redis performance"
Confidence breakdown:
- Base (pattern): 0.4 (vague, matches multiple)
- Structure: -0.1 (too short)
- Linguistic: 0
- Entity: 0
- Context: 0
- Disambiguation: -0.2 (could be STATUS_CHECK or OPTIMIZATION)
Final: 0.50 (MEDIUM tier)

LLM call: YES (enhanced classification)
Self-correction: Added to prompt
Response: LLM validates intent, then responds
```

**Performance:** ~20% of queries fall in MEDIUM tier

---

#### Tier 3: LOW Confidence (< 0.4)

**Characteristics:**
- Weak or no pattern match
- Highly ambiguous
- Incomplete information
- Vague references

**LLM Decision:** Call LLM for classification

**Response Override:** Force CLARIFICATION_REQUEST (regardless of intent)

**Clarification Prompt:**
```
🔴 **High Classification Uncertainty** (Confidence: 25%)

The system is uncertain about how to interpret this query.

**Response Strategy:**
1. **Acknowledge uncertainty**: "I want to make sure I understand correctly..."
2. **Ask targeted clarification**: Present 2-3 specific interpretation options
3. **Avoid assumptions**: Don't proceed with uncertain classification

**Detected intent**: {intent} (low confidence)
```

**Example:**
```
Query: "it doesn't work"
Confidence breakdown:
- Base (pattern): 0.1 (generic, no specific pattern)
- Structure: -0.1 (vague reference "it")
- Linguistic: 0
- Entity: 0
- Context: 0
- Disambiguation: -0.3 (could be anything)
Final: 0.20 (LOW tier)

LLM call: YES
Override: CLARIFICATION_REQUEST (forced)
Response: "I want to understand what isn't working. Could you clarify:
1. What component/service is failing?
2. What error messages are you seeing?
3. When did this start happening?"
```

**Performance:** ~5% of queries fall in LOW tier

---

### Conditional LLM Classification Logic

```python
class LLMClassificationMode(str, Enum):
    DISABLED = "disabled"        # Never call LLM (pattern-only always)
    FALLBACK = "fallback"        # Call LLM only if patterns fail (confidence=0)
    ENHANCEMENT = "enhancement"  # Call LLM if confidence < threshold (RECOMMENDED)
    ALWAYS = "always"           # Always call LLM (backward compatibility)


def should_call_llm(
    pattern_confidence: float,
    mode: LLMClassificationMode,
    threshold: float = 0.7
) -> bool:
    """Determine whether to call LLM based on confidence and mode"""

    if mode == LLMClassificationMode.DISABLED:
        return False

    if mode == LLMClassificationMode.ALWAYS:
        return True

    if mode == LLMClassificationMode.FALLBACK:
        return pattern_confidence == 0.0

    if mode == LLMClassificationMode.ENHANCEMENT:
        return pattern_confidence < threshold

    return False
```

**Expected LLM call distribution (ENHANCEMENT mode, threshold=0.7):**
- HIGH tier (≥0.7): 75% of queries → 0% LLM calls
- MEDIUM tier (0.4-0.7): 20% of queries → 100% LLM calls
- LOW tier (<0.4): 5% of queries → 100% LLM calls
- **Overall: ~25% LLM calls (75% reduction from v1.0)**

---

## Token Optimization

### Tiered Prompt System

FaultMaven uses **3 prompt tiers** based on response complexity:

```python
# Tier 0: MINIMAL (30 tokens) - For simple ANSWER responses
MINIMAL_PROMPT = """
You are FaultMaven, an SRE troubleshooting copilot. Provide clear,
concise answers to technical questions.
"""

# Tier 1: BRIEF (90 tokens) - For simple troubleshooting
BRIEF_PROMPT = """
You are FaultMaven, an SRE troubleshooting copilot. You follow a
structured 5-phase troubleshooting methodology:
1. Define blast radius
2. Establish timeline
3. Formulate hypothesis
4. Validate hypothesis
5. Propose solution

For simple issues, focus on rapid diagnosis and actionable solutions.
"""

# Tier 2: STANDARD (210 tokens) - For complex troubleshooting
STANDARD_PROMPT = """
You are FaultMaven, an SRE troubleshooting copilot specializing in
distributed systems and infrastructure issues.

**Troubleshooting Methodology (5-Phase SRE Doctrine):**
1. **Define Blast Radius**: Identify affected systems and users
2. **Establish Timeline**: Determine when the issue started
3. **Formulate Hypothesis**: Generate potential root causes
4. **Validate Hypothesis**: Test theories with evidence
5. **Propose Solution**: Recommend fixes with verification steps

**Key Principles:**
- Always gather context before proposing solutions
- Provide exact commands with expected outputs
- Consider security and safety implications
- Acknowledge uncertainty when appropriate
"""
```

### Prompt Selection Logic

```python
def get_tiered_prompt(
    response_type: ResponseType,
    complexity: ComplexityLevel
) -> str:
    """Select optimal prompt tier based on response type and complexity"""

    # Minimal prompt for simple information requests
    if response_type in [ResponseType.ANSWER]:
        return MINIMAL_PROMPT

    # Brief prompt for simple troubleshooting/actions
    if complexity == ComplexityLevel.SIMPLE:
        return BRIEF_PROMPT

    # Standard prompt for moderate/complex troubleshooting
    return STANDARD_PROMPT
```

### Token Reduction Results

| Prompt Tier | Tokens | Use Cases | % of Queries |
|-------------|--------|-----------|--------------|
| **MINIMAL** | 30 | ANSWER responses | ~40% |
| **BRIEF** | 90 | Simple actions/troubleshooting | ~35% |
| **STANDARD** | 210 | Complex troubleshooting | ~25% |
| **Weighted Average** | **~90 tokens** | - | 100% |

**Compared to v1.0 (no tiers):**
- v1.0: 6,050 tokens (full context + examples)
- v2.0: 1,165 tokens (tiered prompts)
- **Reduction: 81%**

### Additional Token Savings

**Pattern Template Conditional Loading:**
```python
def format_pattern_prompt(response_type: ResponseType, domain: str) -> str:
    """Load pattern templates just-in-time"""

    # Skip patterns for simple informational responses
    if response_type == ResponseType.ANSWER:
        return ""  # Save ~150 tokens

    # Load only relevant patterns for complex responses
    parts = []
    if response_type:
        response_pattern = get_response_pattern(response_type)
        if response_pattern:
            parts.append(response_pattern)

    if domain:
        domain_pattern = get_domain_pattern(domain)
        if domain_pattern:
            parts.append(domain_pattern)

    return "\n\n".join(parts) if parts else ""
```

**System Directive Format (Prevent Prompt Leakage):**
```python
# Wrap instructions in boundaries to prevent echo
"""
===== SYSTEM INSTRUCTIONS (DO NOT DISPLAY TO USER) =====
{pattern_instructions}
===== END SYSTEM INSTRUCTIONS =====
"""
```

---

## Intent-to-Response Mapping

### Complete Mapping Table

```python
INTENT_TO_RESPONSE_MAPPING = {
    # ═══════════════════════════════════════════════════════════
    # SIMPLE ANSWER INTENTS (10) → ResponseType.ANSWER
    # ═══════════════════════════════════════════════════════════
    QueryIntent.INFORMATION: ResponseType.ANSWER,
    QueryIntent.STATUS_CHECK: ResponseType.ANSWER,
    QueryIntent.PROCEDURAL: ResponseType.ANSWER,
    QueryIntent.VALIDATION: ResponseType.ANSWER,
    QueryIntent.BEST_PRACTICES: ResponseType.ANSWER,
    QueryIntent.GREETING: ResponseType.ANSWER,
    QueryIntent.GRATITUDE: ResponseType.ANSWER,
    QueryIntent.OFF_TOPIC: ResponseType.ANSWER,       # Polite boundary
    QueryIntent.META_FAULTMAVEN: ResponseType.ANSWER,
    QueryIntent.CONVERSATION_CONTROL: ResponseType.ANSWER,

    # ═══════════════════════════════════════════════════════════
    # STRUCTURED PLAN INTENTS (3) → ResponseType.PLAN_PROPOSAL
    # ═══════════════════════════════════════════════════════════
    QueryIntent.CONFIGURATION: ResponseType.PLAN_PROPOSAL,
    QueryIntent.OPTIMIZATION: ResponseType.PLAN_PROPOSAL,
    QueryIntent.DEPLOYMENT: ResponseType.PLAN_PROPOSAL,

    # ═══════════════════════════════════════════════════════════
    # VISUAL RESPONSE INTENTS (2) → Specialized ResponseTypes
    # ═══════════════════════════════════════════════════════════
    QueryIntent.VISUALIZATION: ResponseType.VISUAL_DIAGRAM,
    QueryIntent.COMPARISON: ResponseType.COMPARISON_TABLE,

    # ═══════════════════════════════════════════════════════════
    # DIAGNOSTIC INTENT (1) → Dynamic ResponseType
    # ═══════════════════════════════════════════════════════════
    QueryIntent.TROUBLESHOOTING: None,  # Workflow-driven selection:
                                        # WorkflowState.NEEDS_CLARIFICATION → CLARIFICATION_REQUEST
                                        # WorkflowState.NEEDS_DATA → NEEDS_MORE_DATA
                                        # WorkflowState.SOLUTION_IDENTIFIED → SOLUTION_READY
                                        # WorkflowState.EXHAUSTED_OPTIONS → ESCALATION_REQUIRED

    # ═══════════════════════════════════════════════════════════
    # FALLBACK (1) → ResponseType.CLARIFICATION_REQUEST
    # ═══════════════════════════════════════════════════════════
    QueryIntent.UNKNOWN: ResponseType.CLARIFICATION_REQUEST,
}
```

### Dynamic Response Selection Examples

#### Example 1: Simple Information Request

```python
Query: "What is Redis clustering?"

Classification:
    intent: QueryIntent.INFORMATION
    complexity: ComplexityLevel.SIMPLE
    domain: TechnicalDomain.DATABASE
    urgency: UrgencyLevel.LOW
    confidence: 0.88

Response Selection:
    base_response: ANSWER (from mapping)
    prompt_tier: MINIMAL (30 tokens)

Generated Response:
    "Redis clustering is a distributed implementation of Redis that
     automatically partitions data across multiple nodes. It provides
     high availability through master-slave replication and automatic
     failover. Each master node handles a subset of hash slots (0-16383),
     and data is distributed based on key hashing..."
```

---

#### Example 2: Configuration Request

```python
Query: "Configure Redis for session storage"

Classification:
    intent: QueryIntent.CONFIGURATION
    complexity: ComplexityLevel.MODERATE
    domain: TechnicalDomain.DATABASE
    urgency: UrgencyLevel.MEDIUM
    confidence: 0.92

Response Selection:
    base_response: PLAN_PROPOSAL (from mapping)
    prompt_tier: BRIEF (90 tokens)

Generated Response:
    "Here's the plan to configure Redis for session storage:

     **Goal:** Set up Redis as a persistent session store with
     appropriate timeouts and eviction policies.

     **Steps:**

     1. Install Redis (if not already)
        ```bash
        sudo apt-get update
        sudo apt-get install redis-server
        ```
        Expected: Installation complete message

     2. Configure session timeout
        ```bash
        redis-cli CONFIG SET timeout 1800
        ```
        Expected: OK

     3. Set eviction policy for session data
        ..."
```

---

#### Example 3: Troubleshooting (Dynamic Response)

```python
Query: "Redis is crashing intermittently"

Classification:
    intent: QueryIntent.TROUBLESHOOTING
    complexity: ComplexityLevel.COMPLEX
    domain: TechnicalDomain.DATABASE
    urgency: UrgencyLevel.HIGH
    confidence: 0.75
    completeness: 0.3  # Missing details

Workflow State: NEEDS_CLARIFICATION

Response Selection:
    base_response: None (workflow-driven)
    dynamic_response: CLARIFICATION_REQUEST (due to low completeness)
    prompt_tier: STANDARD (210 tokens)

Generated Response:
    "I'd like to help troubleshoot the Redis crashes. To diagnose
     effectively, I need a few more details:

     1. **When did the crashes start?**
        - Specific date/time if known
        - After any recent changes? (config, version, load)

     2. **How frequently are crashes occurring?**
        - Every few minutes, hours, or days?
        - Is there a pattern? (specific times, operations)

     3. **What do the logs show?**
        ```bash
        tail -100 /var/log/redis/redis-server.log
        ```
        Look for OOM errors, segfaults, or error messages

     These details will help identify if it's a memory, configuration,
     or corruption issue."
```

**After user provides details:**
```python
Workflow State: NEEDS_DATA

Response Selection:
    dynamic_response: NEEDS_MORE_DATA

Generated Response:
    "Thanks for the details. To diagnose the OOM errors, I need:

     1. Current memory configuration
        ```bash
        redis-cli INFO memory
        ```

     2. Memory usage over time
        ```bash
        redis-cli --stat
        ```
        (Run for 30 seconds during normal load)

     3. Eviction policy
        ```bash
        redis-cli CONFIG GET maxmemory-policy
        ```
     ..."
```

**After diagnosis complete:**
```python
Workflow State: SOLUTION_IDENTIFIED

Response Selection:
    dynamic_response: SOLUTION_READY

Generated Response:
    "**Root Cause:**
     Redis is hitting maxmemory limit (2GB) with eviction policy
     set to 'noeviction', causing crash when attempting to write
     new data.

     **Solution:**
     Increase maxmemory and change eviction policy.

     **Implementation:**
     1. Increase maxmemory to 4GB...
     2. Set eviction policy to allkeys-lru...
     ..."
```

---

#### Example 4: Low Confidence Override

```python
Query: "it's not working"

Classification:
    intent: QueryIntent.UNKNOWN (low pattern match)
    complexity: ComplexityLevel.MODERATE
    domain: TechnicalDomain.GENERAL
    urgency: UrgencyLevel.MEDIUM
    confidence: 0.25  # LOW tier

Response Selection:
    base_response: CLARIFICATION_REQUEST (from mapping for UNKNOWN)
    override: CLARIFICATION_REQUEST (confidence < 0.4)
    prompt_tier: BRIEF (90 tokens)

Generated Response:
    "I want to make sure I understand your issue correctly. Could
     you help me with some clarification?

     1. **What specific component isn't working?**
        - Redis server, client connection, specific command?

     2. **What behavior are you seeing?**
        - Error messages, timeouts, incorrect results?

     3. **What were you trying to do when it failed?**
        - Specific operation or command

     With these details, I can provide targeted troubleshooting."
```

---

## Implementation Architecture

### Component Overview

```
faultmaven/
├── models/
│   ├── agentic.py                          # QueryIntent enum (17 intents including UNKNOWN)
│   └── api.py                              # ResponseType enum (9 formats)
├── services/agentic/engines/
│   └── classification_engine.py            # Classification logic
│       ├── QueryClassificationEngine       # Main engine
│       ├── ConfidenceMetrics              # Metrics tracking
│       ├── ComplexityLevel                # 4 levels
│       ├── UrgencyLevel                   # 4 levels (MEDIUM not NORMAL)
│       ├── TechnicalDomain                # 9 domains
│       └── LLMClassificationMode          # 4 modes
├── services/agentic/orchestration/
│   └── response_type_selector.py          # Intent→ResponseType mapping
├── prompts/
│   ├── system_prompts.py                  # Tiered prompts (MINIMAL/BRIEF/STANDARD)
│   └── response_prompts.py                # ResponseType templates
└── config/
    └── settings.py                        # Configuration (11 Phase 0 settings)
```

### Classification Flow

```python
async def classify_query(query: str, context: Optional[Dict] = None) -> QueryClassification:
    """
    Multi-stage classification with confidence-based LLM decision

    Stages:
    1. Pattern-based classification (weighted patterns, exclusion rules)
    2. Multi-dimensional confidence calculation (5 factors)
    3. Conditional LLM classification (if confidence < threshold)
    4. Validation and enhancement
    5. Metrics recording
    """

    # Stage 1: Pattern classification
    normalized_query = normalize_query(query)
    pattern_results = await pattern_classify(normalized_query)

    # Stage 2: Multi-dimensional confidence
    confidence = calculate_confidence(
        pattern_score=pattern_results["intent_confidence"],
        query=normalized_query,
        all_intents=pattern_results["all_intents"],
        entities=pattern_results["entities"],
        context=context
    )

    # Stage 3: Conditional LLM classification
    should_call_llm = determine_llm_call(
        confidence=confidence,
        mode=settings.llm_classification_mode
    )

    if should_call_llm:
        llm_results = await llm_classify(query, context)
        final_results = merge_pattern_and_llm(pattern_results, llm_results)
    else:
        final_results = pattern_results

    # Stage 4: Validation
    final_results["confidence"] = confidence
    validated_results = validate_and_enhance(final_results)

    # Stage 5: Metrics
    metrics.record_classification(confidence, llm_called=should_call_llm)

    return QueryClassification(**validated_results)
```

### Weighted Pattern Matching

```python
def extract_intent(query: str) -> Dict[str, Any]:
    """Extract intent using weighted patterns with exclusion rules"""

    intent_scores = {}

    for intent, compiled_patterns in intent_patterns_compiled.items():
        # Check exclusion rules first
        excluded = False
        if intent in exclusion_rules_compiled:
            for exclusion_pattern in exclusion_rules_compiled[intent]:
                if exclusion_pattern.search(query):
                    excluded = True
                    break

        if excluded:
            continue

        # Calculate weighted score
        weighted_score = 0.0
        max_possible_weight = sum(weight for _, weight in compiled_patterns)

        for compiled_pattern, weight in compiled_patterns:
            if compiled_pattern.search(query):
                weighted_score += weight

        if weighted_score > 0:
            confidence = weighted_score / max_possible_weight
            intent_scores[intent.value] = {
                "weighted_score": weighted_score,
                "confidence": confidence
            }

    # Determine primary intent (highest weighted score)
    if intent_scores:
        primary_intent = max(intent_scores.keys(), key=lambda k: intent_scores[k]["weighted_score"])
        confidence = intent_scores[primary_intent]["confidence"]
    else:
        primary_intent = QueryIntent.UNKNOWN.value
        confidence = 0.0

    return {
        "primary_intent": primary_intent,
        "confidence": confidence,
        "all_intents": intent_scores
    }
```

### Pattern Weight Guidelines

```python
# Pattern specificity weights
WEIGHT_VERY_HIGH = 2.0   # Highly specific patterns (e.g., "this won't work, right?")
WEIGHT_HIGH = 1.8        # Specific patterns (e.g., "my pod is crashing")
WEIGHT_MEDIUM = 1.3      # Moderately specific (e.g., "configure redis")
WEIGHT_TYPICAL = 1.0     # Typical patterns (e.g., "broken", "error")
WEIGHT_LOW = 0.8         # Generic patterns (e.g., "problem", "issue")
WEIGHT_GENERIC = 0.5     # Very generic (e.g., "help")
```

**Example pattern definitions:**
```python
QueryIntent.TROUBLESHOOTING: [
    (r'\b(my|our|the) .* (is|are) (broken|crashing|failing|not working)\b', 2.0),  # Very specific
    (r'\b(i\'m|we\'re) (getting|seeing|experiencing) (error|issue|problem)\b', 2.0),
    (r'\b(troubleshoot|debug|diagnose)\b', 1.5),  # Specific verbs
    (r'\b(fix|resolve|solve)\b', 1.0),  # Typical
    (r'\b(issue|problem|error|bug)\b', 0.8),  # Generic
],

QueryIntent.VALIDATION: [
    (r'\bthis (won\'t|will not|can\'t|cannot) work,? (right|correct)\?\s*$', 2.0),  # Very specific
    (r'\b(would|will|should) this (work|be correct|be valid)\b', 1.5),  # Specific
],

QueryIntent.PROCEDURAL: [
    (r'\bhow (do|can|should) (i|we) (do|perform|execute)\b', 2.0),  # Very specific
    (r'\b(can|could) (i|we) (use|run|do)\b', 1.8),  # Specific
],
```

### Exclusion Rules

```python
exclusion_rules = {
    QueryIntent.TROUBLESHOOTING: [
        # Exclude hypothetical questions
        r'\bthis (won\'t|will not) work,? (right|correct)\?\s*$',
        r'\b(would|will|should) this (work|be correct)\b',
        r'\bjust (confirming|checking|verifying)\b',
        # Exclude capability questions
        r'\b(can|could) (i|we) (use|run|do)\b',
    ],
    QueryIntent.PROCEDURAL: [
        # Exclude active problems
        r'\b(broken|error|fail|not working|crashing)\b',
        r'\b(my|our|the) .* (is|are) (broken|failing)\b',
    ],
    QueryIntent.VALIDATION: [
        # Exclude actual problems
        r'\b(my|our|the) .* (is|are) (broken|crashing|failing)\b',
        r'\b(i\'m|we\'re) (getting|seeing) (error|issue)\b',
    ],
}
```

**Rationale:** Exclusion rules prevent false positives when queries contain keywords for multiple intents.

**Example:**
```
Query: "Can I use Redis if my app is broken?"

Without exclusion:
  - PROCEDURAL matches "Can I use" (weight 1.8)
  - TROUBLESHOOTING matches "app is broken" (weight 2.0)
  - Result: Ambiguous (both high scores)

With exclusion:
  - PROCEDURAL excluded (contains "broken")
  - TROUBLESHOOTING matches "app is broken" (weight 2.0)
  - Result: TROUBLESHOOTING (clear winner)
```

---

## Implementation Focus Areas

### Critical Success Factors

The v3.0 design is **architecturally sound** and ready for implementation. However, the following areas require exceptional attention during development to ensure the system performs as designed:

#### 1. Pattern Maintenance & Testing (CRITICAL)

**Challenge:** With 17 intents (down from 20, including UNKNOWN), each pattern set carries more weight. A single mismatched pattern can cause cascading classification errors.

**Required Actions:**

```python
# Pattern Quality Requirements
- Minimum 10 test cases per intent (160 total test cases)
- Exclusion rule validation for each intent pair
- Pattern specificity analysis (weight validation)
- Real-world query corpus testing (≥1000 queries)
- Continuous monitoring of pattern match rates
```

**Continuous Improvement Process:**

1. **Daily Monitoring**: Track intent distribution, confidence scores, pattern hit rates
2. **Weekly Review**: Analyze misclassifications, update patterns if accuracy <95%
3. **Monthly Audit**: Review new query types, add patterns for emerging use cases
4. **Quarterly Retraining**: Update pattern weights based on production data

**Anti-Pattern Detection:**

```python
# Patterns that should trigger review
- Any intent with <5% classification rate → May indicate over-specific patterns
- Any intent with >30% classification rate → May indicate over-generic patterns
- Cross-intent ambiguity >10% → Need stronger exclusion rules
- Confidence variance >0.3 for same intent → Inconsistent pattern quality
```

**Testing Strategy:**

```python
# Test Pyramid for Pattern Validation
Unit Tests (70%):
  - Each pattern matches expected queries
  - Exclusion rules prevent false positives
  - Weight calculations are correct

Integration Tests (20%):
  - Multi-intent queries resolve correctly
  - Confidence scoring works as expected
  - LLM fallback triggers appropriately

Production Validation (10%):
  - Real user queries classified accurately
  - Edge cases handled gracefully
  - Performance meets SLA (<100ms classification)
```

#### 2. Response Structure Enforcement (CRITICAL)

**Challenge:** LLMs are probabilistic and may not strictly adhere to structured formats. The system must be resilient to format violations.

**Required Validation Logic:**

```python
class ResponseValidator:
    """Validate LLM responses match expected ResponseType structure"""

    def validate_visual_diagram(self, response: str) -> ValidationResult:
        """Validate VISUAL_DIAGRAM format"""
        required_elements = [
            r'```mermaid',           # Mermaid code block
            r'```',                  # Closing code block
            r'(?:graph|flowchart)',  # Valid diagram type
        ]

        # Check for required elements
        for pattern in required_elements:
            if not re.search(pattern, response):
                return ValidationResult(
                    valid=False,
                    error=f"Missing required element: {pattern}",
                    retry_with_correction=True
                )

        # Validate Mermaid syntax
        mermaid_block = extract_mermaid_block(response)
        if not validate_mermaid_syntax(mermaid_block):
            return ValidationResult(
                valid=False,
                error="Invalid Mermaid syntax",
                retry_with_correction=True
            )

        return ValidationResult(valid=True)

    def validate_comparison_table(self, response: str) -> ValidationResult:
        """Validate COMPARISON_TABLE format"""
        # Must have markdown table
        if not re.search(r'\|.*\|.*\|', response):
            return ValidationResult(
                valid=False,
                error="No markdown table found",
                retry_with_correction=True
            )

        # Must have header row + separator + data rows
        table_rows = [line for line in response.split('\n') if '|' in line]
        if len(table_rows) < 3:  # Header + separator + at least 1 data row
            return ValidationResult(
                valid=False,
                error="Table incomplete (need header + data)",
                retry_with_correction=True
            )

        return ValidationResult(valid=True)

    def validate_plan_proposal(self, response: str) -> ValidationResult:
        """Validate PLAN_PROPOSAL format"""
        # Must have numbered steps
        if not re.search(r'^\s*\d+\.', response, re.MULTILINE):
            return ValidationResult(
                valid=False,
                error="No numbered steps found",
                retry_with_correction=True
            )

        # Should have command blocks
        if not re.search(r'```', response):
            # Warning, not error (commands optional for some plans)
            return ValidationResult(
                valid=True,
                warning="No command blocks found (unusual for plans)"
            )

        return ValidationResult(valid=True)
```

**Self-Correction Protocol:**

```python
def enforce_response_format(
    llm_response: str,
    expected_type: ResponseType,
    max_retries: int = 2
) -> str:
    """Enforce response format with automatic retry"""

    for attempt in range(max_retries):
        validation = validate_response(llm_response, expected_type)

        if validation.valid:
            return llm_response

        # Retry with correction prompt
        correction_prompt = f"""
        ⚠️ Response Format Error Detected

        Expected format: {expected_type.value}
        Error: {validation.error}

        Please regenerate the response following this exact structure:
        {get_format_template(expected_type)}

        Your previous response:
        {llm_response[:500]}...
        """

        llm_response = llm_provider.generate_response(correction_prompt)

    # Final fallback: Return with warning
    logger.error(f"Failed to enforce {expected_type} format after {max_retries} retries")
    return add_format_warning(llm_response, expected_type)
```

**Graceful Degradation:**

```python
# If VISUAL_DIAGRAM fails validation → fallback to ANSWER with text description
# If COMPARISON_TABLE fails validation → fallback to ANSWER with bullet points
# If PLAN_PROPOSAL fails validation → fallback to ANSWER with prose instructions

RESPONSE_TYPE_FALLBACKS = {
    ResponseType.VISUAL_DIAGRAM: ResponseType.ANSWER,
    ResponseType.COMPARISON_TABLE: ResponseType.ANSWER,
    ResponseType.PLAN_PROPOSAL: ResponseType.ANSWER,
    # No fallback for ANSWER (base format)
}
```

#### 3. TROUBLESHOOTING Workflow Robustness (CRITICAL)

**Challenge:** `TROUBLESHOOTING` intent uses dynamic `ResponseType` selection based on workflow state. State transitions must be deterministic and well-tested.

**State Machine Implementation:**

```python
class TroubleshootingWorkflowState(str, Enum):
    """Deterministic workflow states"""
    INITIAL = "initial"                      # Just received problem statement
    NEEDS_CLARIFICATION = "needs_clarification"  # Missing critical context
    NEEDS_DATA = "needs_data"                # Need logs/metrics/diagnostics
    INVESTIGATING = "investigating"          # Analyzing provided data
    SOLUTION_IDENTIFIED = "solution_identified"  # Root cause found, ready to fix
    EXHAUSTED_OPTIONS = "exhausted_options"  # Can't solve, need escalation


class TroubleshootingWorkflowEngine:
    """Manage troubleshooting workflow state transitions"""

    def determine_workflow_state(
        self,
        query: str,
        conversation_history: List[Dict],
        information_completeness: float,
        data_availability: Dict[str, bool]
    ) -> TroubleshootingWorkflowState:
        """Deterministically determine workflow state"""

        # State 1: Initial query with low completeness
        if not conversation_history and information_completeness < 0.5:
            return TroubleshootingWorkflowState.NEEDS_CLARIFICATION

        # State 2: Has context but missing diagnostic data
        if information_completeness >= 0.5 and not self._has_diagnostic_data(data_availability):
            return TroubleshootingWorkflowState.NEEDS_DATA

        # State 3: Has data, analyzing
        if self._has_diagnostic_data(data_availability) and not self._has_root_cause():
            return TroubleshootingWorkflowState.INVESTIGATING

        # State 4: Root cause identified
        if self._has_root_cause():
            return TroubleshootingWorkflowState.SOLUTION_IDENTIFIED

        # State 5: Exhausted options
        if self._max_iterations_exceeded() or self._explicit_escalation_requested():
            return TroubleshootingWorkflowState.EXHAUSTED_OPTIONS

        # Default: Initial state
        return TroubleshootingWorkflowState.INITIAL

    def select_response_type(
        self,
        workflow_state: TroubleshootingWorkflowState
    ) -> ResponseType:
        """Map workflow state → ResponseType (deterministic)"""

        STATE_TO_RESPONSE = {
            TroubleshootingWorkflowState.INITIAL: ResponseType.CLARIFICATION_REQUEST,
            TroubleshootingWorkflowState.NEEDS_CLARIFICATION: ResponseType.CLARIFICATION_REQUEST,
            TroubleshootingWorkflowState.NEEDS_DATA: ResponseType.NEEDS_MORE_DATA,
            TroubleshootingWorkflowState.INVESTIGATING: ResponseType.ANSWER,  # Interim findings
            TroubleshootingWorkflowState.SOLUTION_IDENTIFIED: ResponseType.SOLUTION_READY,
            TroubleshootingWorkflowState.EXHAUSTED_OPTIONS: ResponseType.ESCALATION_REQUIRED,
        }

        return STATE_TO_RESPONSE[workflow_state]
```

**State Transition Testing:**

```python
class TestTroubleshootingWorkflow:
    """Comprehensive workflow state testing"""

    def test_state_transitions_complete_workflow(self):
        """Test complete troubleshooting workflow path"""
        engine = TroubleshootingWorkflowEngine()

        # Scenario: Redis crash issue
        states = []

        # Turn 1: Initial vague query
        state = engine.determine_workflow_state(
            query="Redis is crashing",
            conversation_history=[],
            information_completeness=0.2,
            data_availability={}
        )
        assert state == TroubleshootingWorkflowState.NEEDS_CLARIFICATION
        states.append(state)

        # Turn 2: User provides context (when, how often)
        state = engine.determine_workflow_state(
            query="Started yesterday, crashes every 2 hours",
            conversation_history=[{"role": "assistant", "state": states[-1]}],
            information_completeness=0.6,
            data_availability={"timeline": True, "frequency": True}
        )
        assert state == TroubleshootingWorkflowState.NEEDS_DATA
        states.append(state)

        # Turn 3: User provides logs showing OOM errors
        state = engine.determine_workflow_state(
            query="Here are the logs: OOM error...",
            conversation_history=[...],
            information_completeness=0.8,
            data_availability={"logs": True, "errors": True}
        )
        assert state == TroubleshootingWorkflowState.INVESTIGATING
        states.append(state)

        # Turn 4: Root cause identified (memory limit too low)
        state = engine.determine_workflow_state(
            query="...",
            conversation_history=[...],
            information_completeness=0.9,
            data_availability={...},
            root_cause_identified=True
        )
        assert state == TroubleshootingWorkflowState.SOLUTION_IDENTIFIED
        states.append(state)

        # Verify response types for each state
        responses = [engine.select_response_type(s) for s in states]
        assert responses == [
            ResponseType.CLARIFICATION_REQUEST,
            ResponseType.NEEDS_MORE_DATA,
            ResponseType.ANSWER,  # Interim analysis
            ResponseType.SOLUTION_READY
        ]

    def test_premature_escalation(self):
        """Test escalation when user explicitly requests"""
        # User says "I give up, need help" → immediate ESCALATION_REQUIRED
        state = engine.determine_workflow_state(
            query="I give up, can someone else look at this?",
            conversation_history=[...],
            information_completeness=0.5,
            data_availability={},
            explicit_escalation_requested=True
        )
        assert state == TroubleshootingWorkflowState.EXHAUSTED_OPTIONS
        assert engine.select_response_type(state) == ResponseType.ESCALATION_REQUIRED
```

**Guardrails:**

```python
# Prevent infinite loops in troubleshooting
MAX_CLARIFICATION_REQUESTS = 3  # After 3 clarifications → force best-effort answer or escalate
MAX_DATA_REQUESTS = 2           # After 2 data requests → work with what we have
MAX_WORKFLOW_TURNS = 10         # After 10 turns → automatic escalation
```

---

## Migration from v2.0

### Breaking Changes

#### 1. QueryIntent Enum Reduction

**Removed intents (7):**
```python
# ❌ REMOVED - merged into INFORMATION
QueryIntent.EXPLANATION = "explanation"

# ❌ REMOVED - merged into INFORMATION
QueryIntent.DOCUMENTATION = "documentation"

# ❌ REMOVED - merged into STATUS_CHECK
QueryIntent.MONITORING = "monitoring"

# ❌ REMOVED - merged into TROUBLESHOOTING
QueryIntent.PROBLEM_RESOLUTION = "problem_resolution"
QueryIntent.ROOT_CAUSE_ANALYSIS = "root_cause_analysis"
QueryIntent.INCIDENT_RESPONSE = "incident_response"
```

**Added intents (3):**
```python
# ✅ ADDED - new structured action category
QueryIntent.DEPLOYMENT = "deployment"

# ✅ ADDED - visual diagram generation
QueryIntent.VISUALIZATION = "visualization"

# ✅ ADDED - comparison table generation
QueryIntent.COMPARISON = "comparison"
```

**Migration strategy:**
```python
# Old code
if intent == QueryIntent.EXPLANATION:
    return ResponseType.ANSWER

# New code
if intent == QueryIntent.INFORMATION:  # Merged
    return ResponseType.ANSWER
```

#### 2. UrgencyLevel Standardization

**Changed:**
```python
# ❌ OLD (inconsistent)
class QueryUrgency(str, Enum):
    LOW = "low"
    NORMAL = "normal"  # ← Changed to MEDIUM
    HIGH = "high"
    CRITICAL = "critical"
```

```python
# ✅ NEW (consistent)
class UrgencyLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"  # ← Standardized
    HIGH = "high"
    CRITICAL = "critical"
```

**Migration:**
```python
# Update all references from NORMAL to MEDIUM
urgency = UrgencyLevel.MEDIUM  # (was QueryUrgency.NORMAL)
```

#### 3. Intent-to-Response Mapping Updates

**Old mapping (v2.0):**
```python
{
    QueryIntent.EXPLANATION: ResponseType.ANSWER,
    QueryIntent.INFORMATION: ResponseType.ANSWER,
    QueryIntent.DOCUMENTATION: ResponseType.ANSWER,
    QueryIntent.MONITORING: ResponseType.ANSWER,
    QueryIntent.PROBLEM_RESOLUTION: ResponseType.PLAN_PROPOSAL,
    QueryIntent.ROOT_CAUSE_ANALYSIS: ResponseType.PLAN_PROPOSAL,
    QueryIntent.INCIDENT_RESPONSE: ResponseType.PLAN_PROPOSAL,
    # ... others
}
```

**New mapping (v3.0):**
```python
{
    QueryIntent.INFORMATION: ResponseType.ANSWER,        # Merged 3 intents
    QueryIntent.STATUS_CHECK: ResponseType.ANSWER,       # Merged MONITORING
    QueryIntent.TROUBLESHOOTING: None,                   # Dynamic (merged 3 intents)
    QueryIntent.DEPLOYMENT: ResponseType.PLAN_PROPOSAL,  # New
    # ... others
}
```

### Backward Compatibility

**Configuration compatibility:**
```python
# Old .env setting (v2.0)
ENABLE_LLM_CLASSIFICATION=true

# New .env settings (v3.0) - backward compatible
LLM_CLASSIFICATION_MODE=always  # true → "always", false → "disabled"
PATTERN_CONFIDENCE_THRESHOLD=0.7
```

**Auto-migration logic:**
```python
if hasattr(settings, "enable_llm_classification"):
    # Old boolean setting detected
    if settings.enable_llm_classification:
        mode = LLMClassificationMode.ALWAYS
    else:
        mode = LLMClassificationMode.DISABLED
else:
    # New mode setting
    mode = LLMClassificationMode(settings.llm_classification_mode)
```

### Migration Checklist

- [ ] Update `QueryIntent` enum references (7 removed, 3 added)
- [ ] Update `ResponseType` enum (add VISUAL_DIAGRAM, COMPARISON_TABLE)
- [ ] Update `UrgencyLevel` enum references (NORMAL → MEDIUM)
- [ ] Update intent-to-response mapping logic
- [ ] Add pattern definitions for 6 missing intents (OPTIMIZATION, CONFIGURATION, DEPLOYMENT, BEST_PRACTICES, PROCEDURAL, VALIDATION)
- [ ] Update configuration settings (.env and settings.py)
- [ ] Update tests (remove tests for deprecated intents)
- [ ] Update documentation and API contracts
- [ ] Run validation suite to ensure no regressions

---

## Testing & Validation

### Test Coverage Requirements

**Unit tests (classification_engine.py):**
- [ ] Weighted pattern matching (10 test cases per intent)
- [ ] Exclusion rules (5 test cases per rule)
- [ ] Multi-dimensional confidence (5 factors, 20 test cases total)
- [ ] LLM call decision logic (4 modes × 3 confidence tiers = 12 test cases)
- [ ] ConfidenceMetrics tracking (5 test cases)

**Integration tests:**
- [ ] Intent → ResponseType mapping (17 intents × 2 test cases = 34)
- [ ] Tiered prompt selection (3 tiers × 5 test cases = 15)
- [ ] Dynamic troubleshooting workflow (4 states × 3 test cases = 12)

**Validation tests:**
- [ ] Classification accuracy ≥95% on validation dataset
- [ ] LLM call rate ≤30% (target: 25%)
- [ ] Token optimization verified (avg ≤100 tokens per query)
- [ ] No intent ambiguity (no queries with >2 intents at high confidence)

### Example Test Cases

```python
class TestIntentClassification:
    """Test intent classification with v3.0 taxonomy"""

    def test_information_intent(self):
        """INFORMATION intent (merged EXPLANATION, DOCUMENTATION)"""
        test_cases = [
            ("What is Redis?", QueryIntent.INFORMATION),
            ("How does Redis persistence work?", QueryIntent.INFORMATION),
            ("Explain Redis Sentinel", QueryIntent.INFORMATION),
            ("Documentation for clustering", QueryIntent.INFORMATION),
        ]
        for query, expected_intent in test_cases:
            result = classifier.classify_query(query)
            assert result.intent == expected_intent

    def test_troubleshooting_merged(self):
        """TROUBLESHOOTING intent (merged PROBLEM_RESOLUTION, ROOT_CAUSE_ANALYSIS, INCIDENT_RESPONSE)"""
        test_cases = [
            ("Redis is crashing", QueryIntent.TROUBLESHOOTING, UrgencyLevel.MEDIUM),
            ("Why is Redis slow?", QueryIntent.TROUBLESHOOTING, UrgencyLevel.MEDIUM),
            ("Fix the memory leak", QueryIntent.TROUBLESHOOTING, UrgencyLevel.MEDIUM),
            ("Production outage!", QueryIntent.TROUBLESHOOTING, UrgencyLevel.CRITICAL),
        ]
        for query, expected_intent, expected_urgency in test_cases:
            result = classifier.classify_query(query)
            assert result.intent == expected_intent
            assert result.urgency == expected_urgency.value

    def test_deployment_intent_new(self):
        """DEPLOYMENT intent (new in v3.0)"""
        test_cases = [
            "Deploy Redis to K8s",
            "Roll out new Redis version",
            "Migrate Redis to new cluster",
        ]
        for query in test_cases:
            result = classifier.classify_query(query)
            assert result.intent == QueryIntent.DEPLOYMENT

    def test_exclusion_rules(self):
        """Exclusion rules prevent false positives"""
        # PROCEDURAL excluded when query contains "broken"
        result = classifier.classify_query("Can I use Redis if my app is broken?")
        assert result.intent == QueryIntent.TROUBLESHOOTING  # Not PROCEDURAL

        # TROUBLESHOOTING excluded for hypothetical questions
        result = classifier.classify_query("Will this work?")
        assert result.intent == QueryIntent.VALIDATION  # Not TROUBLESHOOTING


class TestConfidenceFramework:
    """Test multi-dimensional confidence calculation"""

    def test_high_confidence_tier(self):
        """HIGH confidence (≥0.7) → skip LLM"""
        result = classifier.classify_query("My Redis pod is crashing with OOM error")
        assert result.confidence >= 0.7
        assert result.metadata["llm_called"] == False

    def test_medium_confidence_tier(self):
        """MEDIUM confidence (0.4-0.7) → call LLM with self-correction"""
        result = classifier.classify_query("Redis performance")
        assert 0.4 <= result.confidence < 0.7
        assert result.metadata["llm_called"] == True
        assert "self_correction" in result.metadata

    def test_low_confidence_tier(self):
        """LOW confidence (<0.4) → force CLARIFICATION_REQUEST"""
        result = classifier.classify_query("it doesn't work")
        assert result.confidence < 0.4
        assert result.metadata["llm_called"] == True
        # Response type should be CLARIFICATION_REQUEST regardless of intent
        response_type = select_response_type(result)
        assert response_type == ResponseType.CLARIFICATION_REQUEST


class TestIntentResponseMapping:
    """Test intent → ResponseType mapping"""

    def test_simple_answer_intents(self):
        """10 simple intents → ANSWER"""
        simple_intents = [
            QueryIntent.INFORMATION,
            QueryIntent.STATUS_CHECK,
            QueryIntent.PROCEDURAL,
            QueryIntent.VALIDATION,
            QueryIntent.BEST_PRACTICES,
            QueryIntent.GREETING,
            QueryIntent.GRATITUDE,
            QueryIntent.OFF_TOPIC,
            QueryIntent.META_FAULTMAVEN,
            QueryIntent.CONVERSATION_CONTROL,
        ]
        for intent in simple_intents:
            response_type = INTENT_TO_RESPONSE_MAPPING[intent]
            assert response_type == ResponseType.ANSWER

    def test_structured_plan_intents(self):
        """3 structured intents → PLAN_PROPOSAL"""
        plan_intents = [
            QueryIntent.CONFIGURATION,
            QueryIntent.OPTIMIZATION,
            QueryIntent.DEPLOYMENT,
        ]
        for intent in plan_intents:
            response_type = INTENT_TO_RESPONSE_MAPPING[intent]
            assert response_type == ResponseType.PLAN_PROPOSAL

    def test_troubleshooting_dynamic(self):
        """TROUBLESHOOTING → dynamic based on workflow state"""
        # Should be None in mapping (dynamic selection)
        assert INTENT_TO_RESPONSE_MAPPING[QueryIntent.TROUBLESHOOTING] is None
```

---

## Monitoring & Metrics

### ConfidenceMetrics Tracking

```python
class ConfidenceMetrics:
    """Track confidence-based decisions for optimization and monitoring"""

    def __init__(self):
        self.tier_distribution = {"high": 0, "medium": 0, "low": 0}
        self.llm_classification_calls = 0
        self.llm_classification_skips = 0
        self.confidence_overrides = 0
        self.self_corrections = 0

    def record_classification(self, confidence: float, llm_called: bool):
        """Record classification metrics"""
        # Tier distribution
        if confidence >= 0.7:
            self.tier_distribution["high"] += 1
        elif confidence >= 0.4:
            self.tier_distribution["medium"] += 1
        else:
            self.tier_distribution["low"] += 1

        # LLM call tracking
        if llm_called:
            self.llm_classification_calls += 1
        else:
            self.llm_classification_skips += 1

    def get_statistics(self) -> Dict[str, Any]:
        """Get comprehensive statistics"""
        total = sum(self.tier_distribution.values())
        total_llm = self.llm_classification_calls + self.llm_classification_skips

        return {
            "tier_distribution": self.tier_distribution,
            "tier_percentages": {
                tier: (count / total * 100) if total > 0 else 0
                for tier, count in self.tier_distribution.items()
            },
            "llm_classification": {
                "calls": self.llm_classification_calls,
                "skips": self.llm_classification_skips,
                "skip_rate": (
                    self.llm_classification_skips / total_llm * 100
                    if total_llm > 0 else 0
                )
            },
            "confidence_overrides": self.confidence_overrides,
            "self_corrections": self.self_corrections,
            "total_classifications": total
        }
```

### Target Metrics (v3.0)

| Metric | Target | Monitoring |
|--------|--------|-----------|
| **Intent Distribution** | Balanced across 17 categories (incl. UNKNOWN) | Daily |
| **Confidence Tier Distribution** | HIGH: 70-80%, MEDIUM: 15-25%, LOW: <10% | Hourly |
| **LLM Call Skip Rate** | ≥70% | Real-time |
| **Classification Accuracy** | ≥95% | Weekly validation |
| **Avg Confidence Score** | ≥0.7 | Daily |
| **Ambiguous Queries (>2 intents)** | <5% | Daily |
| **Token Usage per Query** | ≤100 tokens avg | Real-time |

### Monitoring Dashboard

```python
async def get_classification_statistics():
    """Get comprehensive classification statistics"""

    stats = classification_engine.get_confidence_statistics()

    return {
        "classification": {
            "total_queries": stats["total_classifications"],
            "tier_distribution": stats["tier_distribution"],
            "tier_percentages": stats["tier_percentages"],
        },
        "llm_usage": {
            "calls": stats["llm_classification"]["calls"],
            "skips": stats["llm_classification"]["skips"],
            "skip_rate": stats["llm_classification"]["skip_rate"],
            "cost_savings": calculate_cost_savings(stats["llm_classification"]["skip_rate"])
        },
        "confidence": {
            "avg_confidence": calculate_avg_confidence(),
            "overrides": stats["confidence_overrides"],
            "self_corrections": stats["self_corrections"]
        },
        "intent_distribution": get_intent_distribution(),
        "token_usage": get_token_usage_stats()
    }
```

---

## Appendix

### Configuration Reference

**Complete .env settings for Phase 0:**

```bash
# ═══════════════════════════════════════════════════════════
# LLM Classification Mode (Phase 0)
# ═══════════════════════════════════════════════════════════
LLM_CLASSIFICATION_MODE=enhancement    # disabled, fallback, enhancement, always
PATTERN_CONFIDENCE_THRESHOLD=0.7       # Threshold for LLM enhancement (0.0-1.0)

# ═══════════════════════════════════════════════════════════
# Multi-Dimensional Confidence (Phase 0 - 5 Factors)
# ═══════════════════════════════════════════════════════════
ENABLE_MULTIDIMENSIONAL_CONFIDENCE=true    # Enable 5-factor confidence scoring
ENABLE_STRUCTURE_ANALYSIS=true             # Query structure quality
ENABLE_LINGUISTIC_ANALYSIS=true            # Linguistic markers
ENABLE_ENTITY_ANALYSIS=true                # Technical entity detection
ENABLE_CONTEXT_ANALYSIS=true               # Conversation context
ENABLE_DISAMBIGUATION_CHECK=true           # Cross-intent conflict detection

# ═══════════════════════════════════════════════════════════
# Weighted Pattern Matching (Phase 0)
# ═══════════════════════════════════════════════════════════
PATTERN_WEIGHTED_SCORING=true              # Enable weighted pattern scores (0.5-2.0)
PATTERN_EXCLUSION_RULES=true               # Enable exclusion patterns

# ═══════════════════════════════════════════════════════════
# Token Optimization (Phase 0 - 81% Reduction)
# ═══════════════════════════════════════════════════════════
ENABLE_TIERED_PROMPTS=true                 # Use tiered prompts (30/90/210 tokens)
ENABLE_PATTERN_TEMPLATES=true              # Load pattern templates conditionally

# ═══════════════════════════════════════════════════════════
# Confidence Thresholds (Phase 0)
# ═══════════════════════════════════════════════════════════
CONFIDENCE_OVERRIDE_THRESHOLD=0.4          # Force clarification below this
SELF_CORRECTION_MIN_CONFIDENCE=0.4         # Lower bound for self-correction
SELF_CORRECTION_MAX_CONFIDENCE=0.7         # Upper bound for self-correction
```

### Quick Reference Tables

**Intent → ResponseType Quick Reference:**

| Intent | ResponseType | Prompt Tier |
|--------|--------------|-------------|
| INFORMATION | ANSWER | MINIMAL (30) |
| STATUS_CHECK | ANSWER | MINIMAL (30) |
| PROCEDURAL | ANSWER | MINIMAL (30) |
| VALIDATION | ANSWER | MINIMAL (30) |
| BEST_PRACTICES | ANSWER | MINIMAL (30) |
| GREETING | ANSWER | MINIMAL (30) |
| GRATITUDE | ANSWER | MINIMAL (30) |
| OFF_TOPIC | ANSWER | MINIMAL (30) |
| META_FAULTMAVEN | ANSWER | MINIMAL (30) |
| CONVERSATION_CONTROL | ANSWER | MINIMAL (30) |
| CONFIGURATION | PLAN_PROPOSAL | BRIEF (90) |
| OPTIMIZATION | PLAN_PROPOSAL | BRIEF (90) |
| DEPLOYMENT | PLAN_PROPOSAL | BRIEF (90) |
| TROUBLESHOOTING | Dynamic | STANDARD (210) |
| VISUALIZATION | VISUAL_DIAGRAM | BRIEF (90) |
| COMPARISON | COMPARISON_TABLE | BRIEF (90) |
| UNKNOWN | CLARIFICATION_REQUEST | BRIEF (90) |

**Confidence Tier Actions:**

| Tier | Range | LLM Call | Prompt Modification | Response Override |
|------|-------|----------|---------------------|-------------------|
| HIGH | ≥0.7 | Skip | None | None |
| MEDIUM | 0.4-0.7 | Call | Add self-correction | None |
| LOW | <0.4 | Call | Add uncertainty notice | Force CLARIFICATION_REQUEST |

---

## Version History

**v3.0 (2025-10-03):**
- 🎯 Response-format-driven taxonomy redesign
- ✅ Consolidated 20 intents → 17 intents (including UNKNOWN fallback, 15% reduction)
- ✅ Added 3 new intents: DEPLOYMENT, VISUALIZATION, COMPARISON
- ✅ Added 2 new ResponseType formats: VISUAL_DIAGRAM, COMPARISON_TABLE
- ✅ Removed 6 redundant intents: EXPLANATION, DOCUMENTATION, MONITORING, PROBLEM_RESOLUTION, ROOT_CAUSE_ANALYSIS, INCIDENT_RESPONSE
- ✅ Standardized urgency levels (MEDIUM not NORMAL)
- ✅ Perfect intent-ResponseType alignment (1.8:1 ratio)

**v2.0 (2025-10-02):**
- Multi-dimensional confidence framework
- Weighted pattern matching with exclusion rules
- Conditional LLM classification (4 modes)
- Token optimization (81% reduction)
- ConfidenceMetrics tracking

**v1.0 (2025-09-15):**
- Initial classification system
- Basic pattern matching
- Always-on LLM classification
- Static prompts (6,050 tokens)

---

## License

Copyright © 2025 FaultMaven. All rights reserved.



---

# PART II: PROMPT ENGINEERING SYSTEM

## 4. Prompt Engineering System

### 4.1 Core Modules & Classes

#### Prompt Manager

**PromptManager** (`faultmaven/prompts/prompt_manager.py`)
```python
class PromptManager:
    """Manages prompt templates and generation"""

    def __init__(self):
        self.system_prompts = self._load_system_prompts()
        self.phase_prompts = self._load_phase_prompts()
        self.few_shot_examples = self._load_examples()

    def get_system_prompt(self, variant: str = "default") -> str:
        """Get system prompt with variant"""
        return self.system_prompts[variant]

    def get_phase_prompt(
        self,
        phase: Phase,
        query: str,
        context: Dict[str, Any]
    ) -> str:
        """Generate phase-specific prompt"""
        template = self.phase_prompts[phase]
        return template.format(
            query=query,
            **context
        )

    def add_few_shot_examples(
        self,
        prompt: str,
        task_type: str,
        num_examples: int = 3
    ) -> str:
        """Add few-shot examples to prompt"""
        examples = self.few_shot_examples[task_type][:num_examples]
        examples_text = "\n\n".join([
            f"Example {i+1}:\nInput: {ex['input']}\nOutput: {ex['output']}"
            for i, ex in enumerate(examples)
        ])
        return f"{prompt}\n\n## Examples\n{examples_text}"
```

#### System Prompt Template

**Primary System Prompt** (`faultmaven/prompts/system_prompts.py`)
```python
PRIMARY_SYSTEM_PROMPT = """You are FaultMaven, an expert Site Reliability Engineer with 10+ years of experience troubleshooting production systems.

## Your Expertise
- **Infrastructure**: Kubernetes, Docker, cloud platforms (AWS, Azure, GCP)
- **Databases**: PostgreSQL, MySQL, MongoDB, Redis, Elasticsearch
- **Networking**: TCP/IP, DNS, load balancing, firewalls, TLS/SSL
- **Languages & Frameworks**: Python, Java, Node.js, Go, React
- **Observability**: Prometheus, Grafana, ELK stack, distributed tracing
- **SRE Practices**: Incident response, post-mortems, chaos engineering

## Your Approach: Five-Phase SRE Doctrine

You follow a structured troubleshooting methodology:

### Phase 1: Define Blast Radius
**Objective**: Assess scope and impact of the issue
**Key Questions**:
- What systems/services are affected?
- How many users/customers are impacted?
- What is the severity level?
- Are there workarounds available?
- What is the business impact?

### Phase 2: Establish Timeline
**Objective**: Understand when and how the issue started
**Key Questions**:
- When did the issue first occur?
- What deployments/changes happened recently?
- Are there patterns in the timing?
- What was the sequence of events?
- Were there any warnings or early indicators?

### Phase 3: Formulate Hypothesis
**Objective**: Develop testable hypotheses about root cause
**Key Questions**:
- What are the most likely root causes?
- What evidence supports each hypothesis?
- How can we test these hypotheses?
- Are there similar past incidents?
- What would explain all observed symptoms?

### Phase 4: Validate Hypothesis
**Objective**: Test and validate the most promising hypotheses
**Key Questions**:
- Which hypothesis best explains the symptoms?
- What tests can confirm or refute each hypothesis?
- What additional data is needed?
- Can we reproduce the issue?
- What would disprove our leading hypothesis?

### Phase 5: Propose Solution
**Objective**: Develop and present a comprehensive solution plan
**Key Questions**:
- What are the possible solutions?
- What are the risks and trade-offs?
- What is the estimated time to implement?
- What resources are required?
- How do we prevent recurrence?

## Your Tools

You have access to the following tools:

- **knowledge_base_search**: Search internal documentation and runbooks
- **web_search**: Search external resources (use sparingly, prefer knowledge base)
- **log_analysis**: Analyze log files for errors and patterns
- **config_validation**: Validate YAML/JSON configurations
- **metrics_query**: Query Prometheus metrics (if available)

**Tool Usage Guidelines**:
- Always prefer knowledge_base_search over web_search
- Use log_analysis when user mentions logs or errors
- Use config_validation for configuration issues
- Use metrics_query to understand system performance

## Output Format

Structure your responses as:

### Current Status
[Briefly state what you understand about the issue]

### Analysis
[Your reasoning, findings, and thought process]

### Next Steps
[Specific, actionable steps the user should take]
[Number each step clearly]

### Questions
[Any clarifying questions you need answered]
[List each question on a new line]

## Communication Style

- **Be Direct**: Get to the point quickly
- **Be Specific**: Provide exact commands, file paths, line numbers
- **Be Actionable**: Always provide next steps
- **Be Honest**: If uncertain, say so and explain why
- **Be Educational**: Explain the "why" behind recommendations

## Examples

### Example 1: Kubernetes Pod Crashing

**User**: My Kubernetes pod keeps crashing with OOMKilled

**Your Response**:
### Current Status
Your pod is being killed due to Out of Memory (OOMKilled) errors. This means the container exceeded its memory limits.

### Analysis
OOMKilled typically occurs when:
1. Memory limits set too low for workload
2. Memory leak in application
3. Sudden spike in traffic/load

Based on the error, I need to check your pod's resource configuration and actual memory usage.

### Next Steps
1. Check current memory limits:
   ```bash
   kubectl describe pod <pod-name> | grep -A 5 "Limits:"
   ```

2. Check actual memory usage before crash:
   ```bash
   kubectl top pod <pod-name>
   ```

3. View recent pod events:
   ```bash
   kubectl get events --field-selector involvedObject.name=<pod-name>
   ```

### Questions
- What is your pod name?
- What type of application is running (web server, worker, etc.)?
- Has traffic increased recently?

---

Now, help the user with their issue following this methodology.
"""
```

### 4.2 Key Design Decisions

#### Decision 1: Structured vs Free-Form System Prompt
**Choice:** Highly structured with sections and examples
**Rationale:**
- **Consistency**: LLM follows format reliably
- **Quality**: Clear structure improves response quality
- **Training**: Examples teach desired behavior

#### Decision 2: Phase-Specific vs Generic Prompts
**Choice:** Phase-specific prompts for each doctrine phase
**Rationale:**
- **Focus**: Each phase has specific objectives
- **Completeness**: Ensures all phases covered
- **Learning**: Phase-specific patterns easier to optimize

#### Decision 3: Few-Shot vs Zero-Shot
**Choice:** Few-shot with 2-3 examples per task type
**Rationale:**
- **Quality**: Examples improve accuracy significantly
- **Consistency**: Examples establish patterns
- **Cost**: 2-3 examples don't add much token cost

### 4.3 Design Principles & Strategies

#### Principle 1: Context-Aware Prompting
**Strategy:**
- Include relevant memory context
- Reference previous findings
- Adapt tone based on user skill level

#### Principle 2: Progressive Refinement
**Strategy:**
- Start with broad system prompt
- Add phase-specific guidance
- Include task-specific examples
- Append user context

#### Principle 3: Output Structure Enforcement
**Strategy:**
- Explicit output format in prompt
- Examples demonstrate structure
- Validate output against schema

### 4.4 Prompt Library

#### Phase Prompt Templates

**Phase 1: Define Blast Radius**
```python
PHASE_1_PROMPT = """
# PHASE 1: DEFINE BLAST RADIUS

## Objective
Assess the scope and impact of the reported issue.

## Key Questions to Answer
- What systems or services are affected?
- How many users or customers are impacted?
- What is the severity level (P0-P4)?
- Are there any workarounds available?
- What is the business impact?

## Available Context
{memory_context}

## User's Report
{query}

## Instructions
Analyze the user's report and determine:
1. Affected systems/services
2. Impact scope (users, regions, features)
3. Severity assessment
4. Initial workarounds

If you need more information, ask specific questions.

## Output Format
Provide your analysis in this structure:
- **Affected Systems**: [List]
- **Impact Scope**: [Description]
- **Severity**: [P0/P1/P2/P3/P4 with justification]
- **Workarounds**: [If any]
- **Information Needed**: [Questions to ask user]
"""
```

---

## Recent Improvements (2025-10-04)

### 1. Conversation Context for LLM Classification

**Problem**: When pattern-based classification failed and LLM classification was triggered, the LLM only received the isolated current query without conversation history, causing misclassification of contextual queries.

**Solution**: Pass full conversation history to LLM classifier for context-aware classification.

**Implementation** ([intelligent_query_processor.py:237-243](../../faultmaven/services/agentic/orchestration/intelligent_query_processor.py), [classification_engine.py:1078-1105](../../faultmaven/services/agentic/engines/classification_engine.py)):
```python
classification = await self.classification_engine.classify_query(
    query, context={
        "session_id": session_id,
        "case_id": case_id,
        "conversation_history": conversation_context  # Full conversation history
    }
)
```

Prompt explicitly instructs: *"Classify based on the ENTIRE CONTEXT, not just the current question"*

**Benefits**:
- ✅ Context-aware classification of follow-up questions
- ✅ Correctly handles queries referencing previous messages
- ✅ Backward compatible (works with or without history)

### 2. Same-Provider Optimization (Avoid Redundant LLM Calls)

**Problem**: When pattern matching fails, system makes TWO LLM calls with the SAME context:
1. LLM call for classification
2. Same LLM call for response generation

This is wasteful when both use the same provider (currently `CHAT_PROVIDER=fireworks` for both).

**Solution**: Skip LLM classification when the same provider will handle response generation.

**Implementation** ([classification_engine.py:617-628](../../faultmaven/services/agentic/engines/classification_engine.py), [intelligent_query_processor.py:151-171](../../faultmaven/services/agentic/orchestration/intelligent_query_processor.py)):
```python
# Check if same provider will be used for response
same_provider_for_response = self._check_same_provider()  # Currently: True

# Skip LLM classification if same provider
if should_call_llm and same_provider_for_response:
    logger.info(f"Skipping LLM classification - same provider will handle both")
    should_call_llm = False
```

**Benefits**:
- ✅ Saves 1 LLM API call when pattern confidence < 0.7
- ✅ Reduces latency (single round trip vs two)
- ✅ Reduces cost (fewer tokens sent)
- ✅ Response LLM determines intent while generating answer

**Future**: When separate `CLASSIFIER_PROVIDER` is implemented, compare provider names and only skip if they match.

### 3. Visual Response Type Prompts

**Problem**: `VISUALIZATION` and `COMPARISON` intents existed but had no prompts for `VISUAL_DIAGRAM` and `COMPARISON_TABLE` response types. LLM fell back to default prompt, generating wrong format.

**Solution**: Added explicit prompt templates for visual response types ([response_prompts.py:30-32](../../faultmaven/prompts/response_prompts.py)):
```python
ResponseType.VISUAL_DIAGRAM: """Create a Mermaid diagram to visualize the architecture, flow, or system structure. Use appropriate diagram type (graph TD/LR for architecture, flowchart for processes, sequenceDiagram for interactions). Include clear node labels and relationship descriptions. Wrap the diagram in ```mermaid code block. Provide a brief 1-2 sentence explanation before the diagram describing what it shows, and optionally add key insights after."""

ResponseType.COMPARISON_TABLE: """Create a markdown comparison table to analyze options, features, or approaches. Use clear column headers (Feature/Aspect, Option A, Option B, etc.). Include relevant comparison dimensions (performance, complexity, use cases, pros/cons). Format as proper markdown table with | delimiters. Provide brief context (1-2 sentences) before the table explaining what's being compared, and add a recommendation or key takeaway after based on the comparison."""
```

**Benefits**:
- ✅ All 9 ResponseType values now have prompts
- ✅ Complete flow: Intent → ResponseType → Prompt → LLM
- ✅ VISUALIZATION and COMPARISON intents now work correctly

### 4. Data Analysis Intent Decision

**Question**: Should there be a dedicated `DATA_ANALYSIS` intent for when users submit logs/metrics/traces?

**Decision**: **NO new intent needed.**

**Reasoning**:
- Data patterns are **content indicators**, not **intent indicators**
- User intent determined by their question/implied question:
  - "What does this mean?" → `INFORMATION` → `ANSWER`
  - "What's wrong?" → `TROUBLESHOOTING` → Dynamic
  - "How to fix?" → `TROUBLESHOOTING` → Dynamic
  - "Is this normal?" → `VALIDATION` → `ANSWER`
  - "Optimize based on this" → `OPTIMIZATION` → `PLAN_PROPOSAL`
- Existing 17 intents cover all scenarios

**Data submission flow**:
- **≥10K chars**: Route to `/data/upload` → Preprocessing → `INSIGHTS_REPORT` response type
- **<10K chars**: Classify based on question → Use appropriate existing intent

### 5. INSIGHTS_REPORT Response Type (For Data Uploads)

**Purpose**: Structured data analysis reports for large data uploads (≥10K chars).

**Structure**:
```
**Summary**
Log shows high error rate (15% over 2h window) with memory allocation failures.

**Findings**
- 🔴 **Critical**: OutOfMemoryError occurred 47 times between 14:30-16:30
- ⚠️ **Pattern**: Error spike correlates with batch job execution
- 📊 **Metrics**: 312 total errors, 47 OOM (15%), 265 timeout (85%)

**Recommendations**
1. Increase JVM heap size (-Xmx parameter)
2. Investigate batch job memory consumption
3. Review garbage collection logs for tuning opportunities
```

**Usage**: Reserved for `/data/upload` endpoint ONLY (not for conversational queries).

**Intent-to-Response Ratio**: 18 intents → 10 response types = 1.80:1 ✅ (still <2:1 target)

### 6. Data Preprocessing Layer (TODO)

**Problem**: Large data files (logs, metrics, traces) cannot be sent directly to LLM due to context limits.

**Required**: Intelligent preprocessing layer that:
1. Extracts key information from uploaded data
2. Summarizes large datasets into LLM-digestible format (<8K chars)
3. Preserves critical details (errors, anomalies, patterns)
4. Enables AI to provide meaningful analysis

**See**: [DATA_SUBMISSION_DESIGN.md](./DATA_SUBMISSION_DESIGN.md#data-preprocessing-implementation-todo) for detailed implementation plan.

---

