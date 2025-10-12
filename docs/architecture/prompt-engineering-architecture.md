# Prompt Engineering Architecture

**Version:** 2.0
**Date:** 2025-10-11
**Status:** ✅ ACTIVE - OODA Framework v3.2.0
**Supersedes:** Doctor/Patient Prompting Architecture v1.0

---

## Executive Summary

This document specifies FaultMaven's comprehensive prompt engineering architecture for the OODA Investigation Framework (v3.2.0). The architecture implements multi-layer prompt assembly, phase-aware selection, context optimization, and intelligent token management to deliver natural, effective AI-powered troubleshooting.

**Key Capabilities:**
- **Multi-layer prompt assembly** - Modular composition of system, phase, context, and task layers
- **Phase-aware selection** - Dynamic prompt adaptation across 7 investigation phases
- **Context optimization** - Hierarchical memory with 64% token reduction
- **Doctor-patient philosophy** - Natural conversation maintaining diagnostic rigor
- **Evidence-driven design** - Structured requests with acquisition guidance

**Token Budget:** ~1,900 tokens/turn (vs 4,500+ unoptimized, 58% reduction)

---

## Table of Contents

1. [Design Philosophy](#1-design-philosophy)
2. [Multi-Layer Prompt Assembly](#2-multi-layer-prompt-assembly)
3. [Phase-Aware Selection](#3-phase-aware-selection)
4. [Context Management](#4-context-management)
5. [Optimization Strategies](#5-optimization-strategies)
6. [Implementation Guide](#6-implementation-guide)
7. [Metrics and Monitoring](#7-metrics-and-monitoring)

---

## 1. Design Philosophy

### 1.1 Core Principles (Doctor-Patient Paradigm)

FaultMaven's prompt engineering preserves the **doctor-patient interaction model** - maintaining structured diagnostic methodology while presenting natural conversation to users.

#### The Medical Analogy Applied to AI

| Medical Concept | FaultMaven Implementation | Prompting Technique |
|----------------|---------------------------|---------------------|
| **Doctor meets patient** | Consultant Mode (Phase 0) | Reactive prompts, detect problem signals |
| **Doctor leads diagnosis** | Lead Investigator (Phases 1-6) | Proactive prompts, evidence requests |
| **Structured procedure** | 7 investigation phases | Phase-specific prompt templates |
| **Order lab tests** | OODA: Observe step | Evidence request generation |
| **Analyze results** | OODA: Orient step | Context-aware synthesis |
| **Make diagnosis** | OODA: Decide step | Hypothesis ranking |
| **Prescribe treatment** | OODA: Act step | Solution implementation |
| **Never mention procedure** | "No methodology jargon" | User sees guidance, not phases |

#### Key Prompting Principles

**1. No Classification Layer** ✅
```
Traditional:  User Query → Classifier LLM → Response LLM → Answer
FaultMaven:   User Query → Single Powerful LLM → Answer
```
- Single powerful LLM handles everything
- No intent detection overhead
- Natural conversation flow

**2. "Never Mention Phases"** ✅
```python
# Explicitly stated in prompts
"Never mention 'phases', 'OODA', or 'systematic investigation' unless the user asks"
```
- User sees natural guidance, not methodology
- Internal structure invisible to user
- Sounds like skilled colleague

**3. "Answer First, Guide Second"** ✅
```python
# Lead Investigator principle
"Always acknowledge what user provided before requesting more"
```
- Respect user's questions
- Address concerns immediately
- Guide after answering

**4. "Don't Assume Illness"** ✅
```python
# Consultant Mode principle
"Detect problem signals, only offer investigation if appropriate"
```
- Listen for error messages, failures
- Offer investigation, don't force it
- Respect non-diagnostic queries

**5. "Natural Conversation"** ✅
```python
"Be conversational and collaborative, like a skilled colleague would be"
```
- Sound human, not robotic
- Use contractions, natural language
- Avoid AI clichés

### 1.2 Anthropic Context Engineering Principles

Integrating [Anthropic's effective context engineering strategies](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents):

**A. Context as Finite Resource**
```
Principle: "Find the smallest possible set of high-signal tokens"

Implementation:
- Hierarchical memory (hot/warm/cold) - only load what's needed
- Phase-specific prompts - load current phase context only
- Just-in-time evidence loading - fetch when required
- Token budget per layer (system: 800, phase: 400, context: 500, history: 200)
```

**B. Clear, Organized System Prompts**
```
Principle: "Use distinct sections with clear structure"

Implementation:
<background_information>
    Role definition, engagement mode, investigation framework
</background_information>

<instructions>
    Phase objectives, evidence needs, OODA guidance
</instructions>

<context>
    Investigation state, hypotheses, evidence summary
</context>

<user_query>
    Current query with conversation history
</user_query>
```

**C. Just-in-Time Context Loading**
```
Principle: "Maintain lightweight identifiers, dynamically load at runtime"

Implementation:
- Phase-specific prompts loaded only when entering phase
- Evidence details loaded only when referenced
- Conversation history summarized (warm memory)
- Persistent insights always available (100 tokens)
```

**D. Progressive Context Discovery**
```
Principle: "Enable autonomous exploration with adaptive retrieval"

Implementation:
- Agent requests evidence as needed (not pre-loaded)
- Evidence acquisition guidance (commands, file paths, UI locations)
- Hypothesis-driven evidence collection
- OODA iterations discover context progressively
```

**E. Compaction and Summarization**
```
Principle: "Summarize near context limit, preserve critical details"

Implementation:
- Warm memory: LLM-summarized iterations (3-5 iterations back)
- Cold memory: Key facts only (older iterations)
- 64% token reduction vs unmanaged context
- Automatic compression every 3 turns
```

---

## 2. Multi-Layer Prompt Assembly

### 2.1 Prompt Layer Architecture

FaultMaven prompts are assembled from 6 modular layers, loaded dynamically based on context:

```
┌─────────────────────────────────────────────────────────┐
│ Layer 6: User Query + Conversation History (100-300t)  │ ← Always included
├─────────────────────────────────────────────────────────┤
│ Layer 5: Investigation State Context (200-400t)        │ ← If active investigation
├─────────────────────────────────────────────────────────┤
│ Layer 4: Phase-Specific Context (300-500t)             │ ← Lead Investigator only
├─────────────────────────────────────────────────────────┤
│ Layer 3: Engagement Mode Guidance (400-600t)           │ ← Consultant or Lead Investigator
├─────────────────────────────────────────────────────────┤
│ Layer 2: Investigation Framework (200-300t)            │ ← Lead Investigator only
├─────────────────────────────────────────────────────────┤
│ Layer 1: System Identity & Philosophy (300-500t)       │ ← Always included
└─────────────────────────────────────────────────────────┘

Total Token Budget: ~1,900 tokens (Consultant: ~1,000t | Lead Investigator: ~1,900t)
```

### 2.2 Layer Definitions

#### Layer 1: System Identity & Philosophy (300-500 tokens)

**Purpose:** Establish AI persona, core principles, response style

**Content:**
```python
SYSTEM_IDENTITY = """FaultMaven - Expert Technical Troubleshooting AI

Role: Your knowledgeable colleague helping solve technical problems

Core Principles:
- Answer questions thoroughly and accurately
- Guide investigation naturally without jargon
- Request specific evidence when needed
- Acknowledge user input before requesting more
- Sound like colleague, not chatbot

Never Mention: "phases", "OODA", "framework", or methodology terms
"""
```

**Token Budget:** 300-500 tokens
**Loaded:** Always
**Optimization:** Compressed, bullet points, no redundancy

#### Layer 2: Investigation Framework (200-300 tokens)

**Purpose:** Define 7-phase OODA framework structure (internal guide)

**Content:**
```python
INVESTIGATION_FRAMEWORK = """Investigation Structure (Internal):

7 Phases:
0. Intake - Problem confirmation, get consent
1. Blast Radius - Scope and impact
2. Timeline - When started, what changed
3. Hypothesis - Possible root causes
4. Validation - Test hypotheses systematically
5. Solution - Implement and verify fix
6. Document - Capture learnings

OODA Steps (within each phase):
- Observe: Request evidence
- Orient: Analyze data
- Decide: Choose next action
- Act: Execute test/solution

Intensity: Light (1-2 cycles) | Medium (2-4) | Full (3-6)
"""
```

**Token Budget:** 200-300 tokens
**Loaded:** Lead Investigator mode only
**Optimization:** Concise structure, no examples

#### Layer 3: Engagement Mode Guidance (400-600 tokens)

**Purpose:** Define interaction style for current mode

**A. Consultant Mode (Phase 0):**
```python
CONSULTANT_MODE = """Consultant Mode - Expert Colleague

You are answering questions and detecting problems.

Behavior:
- Reactive: Follow user's lead
- Answer thoroughly before suggesting next steps
- Detect problem signals (errors, failures, "not working")
- Offer systematic investigation ONCE if problem detected
- Respect user's choice (yes/no)

Problem Signal Detected:
"Would you like systematic investigation? I can guide:
- Scope assessment
- Timeline establishment
- Root cause testing
- Solution implementation"

If declined: Continue Q&A, offer again if new problem emerges
"""
```

**B. Lead Investigator Mode (Phases 1-6):**
```python
LEAD_INVESTIGATOR_MODE = """Lead Investigator - War Room Commander

You are leading this investigation.

Behavior:
- Proactive: Request specific evidence
- Evidence-driven: Back claims with data
- Focused: ONE evidence request at a time
- Acknowledge first: Address user input before requesting more
- Adaptive: Try different approaches if stuck

Evidence Request Format:
"I need [WHAT] to understand [WHY].

[HOW to get it - specific command/file/UI path]

Expected: [What they should see]

What do you find?"
"""
```

**Token Budget:** 400-600 tokens per mode
**Loaded:** Based on current engagement mode
**Optimization:** Examples removed, principles emphasized

#### Layer 4: Phase-Specific Context (300-500 tokens)

**Purpose:** Current phase objectives, questions, evidence needs

**Example - Phase 3: Hypothesis:**
```python
PHASE_3_HYPOTHESIS = """Current Phase: Hypothesis Generation (Phase 3/6)

Objective: Formulate 2-4 ranked root cause hypotheses

Key Questions:
- Based on symptoms and timeline, what could cause this?
- What changed that could trigger these symptoms?
- What are the most likely failure modes?

Evidence Needed:
- Configuration files
- Environment variables
- Dependency versions
- Recent deployment history

Completion Criteria:
- 2-4 hypotheses with likelihood rankings
- Testing strategy for each hypothesis
- Ready to enter Validation phase

Expected Iterations: 2-3 OODA cycles
"""
```

**Token Budget:** 300-500 tokens
**Loaded:** Lead Investigator mode, current phase only
**Optimization:** Bullet points, no prose

#### Layer 5: Investigation State Context (200-400 tokens)

**Purpose:** Current investigation progress, hypotheses, evidence

**Content (dynamically generated):**
```python
def get_investigation_context(investigation_state: InvestigationState) -> str:
    """Generate investigation state context"""
    return f"""Investigation Status:

Problem: {investigation_state.ooda_engine.anomaly_frame.statement}
Severity: {investigation_state.ooda_engine.anomaly_frame.severity}
Scope: {investigation_state.ooda_engine.anomaly_frame.affected_scope}

Active Hypotheses:
{format_top_hypotheses(investigation_state.ooda_engine.hypotheses, top_n=3)}

Evidence: {len(investigation_state.evidence.evidence_items)} collected, {len(investigation_state.evidence.evidence_requests)} pending

OODA Iteration: {investigation_state.ooda_engine.current_iteration}
"""
```

**Token Budget:** 200-400 tokens
**Loaded:** If active investigation exists
**Optimization:** Top 3 hypotheses only, summary format

#### Layer 6: User Query + Conversation History (100-300 tokens)

**Purpose:** Current query with relevant conversation context

**Content (dynamically generated):**
```python
def get_query_context(user_query: str, conversation_history: str) -> str:
    """Generate query with recent history"""

    # Use hierarchical memory - hot memory only (last 2 interactions)
    hot_history = get_hot_memory(conversation_history, max_turns=2)

    return f"""Recent Conversation:
{hot_history}

Current Query:
{user_query}

Your Response:
[Acknowledge user input, provide answer/guidance, request next evidence if appropriate]
"""
```

**Token Budget:** 100-300 tokens
**Loaded:** Always
**Optimization:** Hot memory only (2 most recent turns)

### 2.3 Assembly Logic

**Prompt Assembly Algorithm:**

```python
async def assemble_prompt(
    user_query: str,
    investigation_state: Optional[InvestigationState],
    conversation_history: str,
    engagement_mode: EngagementMode,
    current_phase: Optional[InvestigationPhase],
) -> str:
    """Assemble complete prompt from layers"""

    layers = []

    # Layer 1: Always include
    layers.append(SYSTEM_IDENTITY)

    # Layer 2: Lead Investigator only
    if engagement_mode == EngagementMode.LEAD_INVESTIGATOR:
        layers.append(INVESTIGATION_FRAMEWORK)

    # Layer 3: Engagement mode guidance
    if engagement_mode == EngagementMode.CONSULTANT:
        layers.append(CONSULTANT_MODE)
    else:
        layers.append(LEAD_INVESTIGATOR_MODE)

    # Layer 4: Phase-specific (Lead Investigator only)
    if engagement_mode == EngagementMode.LEAD_INVESTIGATOR and current_phase:
        phase_context = get_phase_context(current_phase)
        layers.append(phase_context)

    # Layer 5: Investigation state (if active)
    if investigation_state:
        state_context = get_investigation_context(investigation_state)
        layers.append(state_context)

    # Layer 6: Query + history (always)
    query_context = get_query_context(user_query, conversation_history)
    layers.append(query_context)

    # Join with clear separators
    prompt = "\n\n---\n\n".join(layers)

    # Log token usage
    estimated_tokens = estimate_tokens(prompt)
    logger.info(f"Assembled prompt: {estimated_tokens} tokens across {len(layers)} layers")

    return prompt
```

---

## 3. Phase-Aware Selection

### 3.1 Phase Transition Logic

Prompts adapt dynamically as investigation progresses through 7 phases:

```
Phase 0: Intake (Consultant Mode)
    ↓ User consents to investigation
Phase 1: Blast Radius (Lead Investigator - Light OODA)
    ↓ Scope defined
Phase 2: Timeline (Lead Investigator - Light OODA)
    ↓ Timeline established
Phase 3: Hypothesis (Lead Investigator - Medium OODA)
    ↓ Hypotheses formulated
Phase 4: Validation (Lead Investigator - Full OODA)
    ↓ Root cause validated
Phase 5: Solution (Lead Investigator - Medium OODA)
    ↓ Solution implemented and verified
Phase 6: Document (Lead Investigator - Light OODA)
    ↓ Learnings captured
```

### 3.2 Phase-Specific Prompt Variations

#### Phase 0: Intake (Consultant Mode)
```python
# No OODA, conversational, problem detection
Prompt Layers: [1, 3-Consultant, 6]
Token Budget: ~1,000 tokens
Focus: Answer questions, detect problems, offer investigation
```

#### Phase 1: Blast Radius (Light OODA: 1-2 cycles)
```python
# Understand scope quickly
Prompt Layers: [1, 2, 3-Lead, 4-Phase1, 5, 6]
Token Budget: ~1,800 tokens
Focus: Who/what/how many affected? Severity?
Evidence: Metrics, error counts, affected user segments
```

#### Phase 2: Timeline (Light OODA: 1-2 cycles)
```python
# When did it start, what changed?
Prompt Layers: [1, 2, 3-Lead, 4-Phase2, 5, 6]
Token Budget: ~1,800 tokens
Focus: First occurrence time, recent changes, gradual vs sudden
Evidence: Deployment logs, config changes, timeline correlation
```

#### Phase 3: Hypothesis (Medium OODA: 2-3 cycles)
```python
# Generate possible root causes
Prompt Layers: [1, 2, 3-Lead, 4-Phase3, 5, 6]
Token Budget: ~1,900 tokens
Focus: Formulate 2-4 ranked hypotheses with testing strategies
Evidence: Configuration, environment, dependencies
```

#### Phase 4: Validation (Full OODA: 3-6 cycles)
```python
# Systematically test hypotheses
Prompt Layers: [1, 2, 3-Lead, 4-Phase4, 5, 6]
Token Budget: ~1,900 tokens
Focus: Test each hypothesis, update confidence, prevent anchoring
Evidence: Test results, logs, metrics, configuration values
Special: Anchoring prevention after 3+ iterations
```

#### Phase 5: Solution (Medium OODA: 2-4 cycles)
```python
# Implement and verify fix
Prompt Layers: [1, 2, 3-Lead, 4-Phase5, 5, 6]
Token Budget: ~1,900 tokens
Focus: Specific fix, implementation steps, verification
Evidence: Implementation proof, post-fix metrics, symptom resolution
```

#### Phase 6: Document (Light OODA: 1 cycle)
```python
# Capture learnings
Prompt Layers: [1, 2, 3-Lead, 4-Phase6, 5, 6]
Token Budget: ~1,800 tokens
Focus: Offer case report and/or runbook creation
Evidence: None (synthesis phase)
```

### 3.3 OODA Intensity Adaptation

**Prompt guidance adapts to OODA intensity:**

| Intensity | OODA Cycles | Prompt Guidance | Use Cases |
|-----------|-------------|-----------------|-----------|
| **Light** | 1-2 | "Get key facts quickly, move forward" | Blast Radius, Timeline, Document |
| **Medium** | 2-4 | "Explore thoroughly, consider alternatives" | Hypothesis, Solution |
| **Full** | 3-6 | "Systematic testing, prevent anchoring" | Validation |

**Anchoring Prevention (Phase 4 - Validation):**

After 3+ iterations without confidence improvement:
```python
ANCHORING_PREVENTION_PROMPT = """You've completed 3 validation iterations.

Check for anchoring bias:
- Are we repeatedly testing the same hypothesis type?
- Has confidence in top hypothesis changed <5% in 2 iterations?
- Are we ignoring contradictory evidence?

If YES to any:
"Let's deliberately consider different angles:
- If it's NOT [current hypothesis category], what else could it be?
- What evidence would DISPROVE our top theory?
- What have we NOT checked yet?"
"""
```

---

## 4. Context Management

### 4.1 Hierarchical Memory System

**Token-Optimized 4-Tier Memory** (64% reduction vs unmanaged):

```
┌──────────────────────────────────────────────────────────────┐
│ HOT MEMORY (~500 tokens)                                     │
│ Last 2 OODA iterations - Full fidelity                       │
│ - Complete evidence details                                  │
│ - All hypothesis changes                                     │
│ - Verbatim user/agent exchanges                              │
│ Redis TTL: 24 hours                                          │
└──────────────────────────────────────────────────────────────┘
              ↓ After 2 iterations
┌──────────────────────────────────────────────────────────────┐
│ WARM MEMORY (~300 tokens)                                    │
│ Iterations 3-5 - LLM-summarized                              │
│ - Key facts extracted                                        │
│ - Confidence deltas                                          │
│ - Major decisions                                            │
│ - Evidence IDs (not full content)                            │
│ Redis TTL: 7 days                                            │
└──────────────────────────────────────────────────────────────┘
              ↓ After 5 iterations
┌──────────────────────────────────────────────────────────────┐
│ COLD MEMORY (~100 tokens)                                    │
│ Iterations 6+ - Key facts only                               │
│ - Critical findings                                          │
│ - Validated/refuted hypotheses                               │
│ - No intermediate details                                    │
│ Redis TTL: 30 days                                           │
└──────────────────────────────────────────────────────────────┘
              ↓ Persistent
┌──────────────────────────────────────────────────────────────┐
│ PERSISTENT INSIGHTS (~100 tokens)                            │
│ Always accessible                                            │
│ - Root cause (if validated)                                  │
│ - Solution applied                                           │
│ - Critical learnings                                         │
│ Redis TTL: Permanent (until case closed)                    │
└──────────────────────────────────────────────────────────────┘
```

**Implementation:**
```python
# File: faultmaven/core/investigation/memory_manager.py

class HierarchicalMemoryManager:
    """Manages 4-tier memory system with automatic promotion/demotion"""

    def __init__(self, llm_provider, state_manager):
        self.compression_engine = MemoryCompressionEngine(llm_provider)
        self.state_manager = state_manager

    async def get_memory_for_prompt(
        self,
        investigation_state: InvestigationState,
    ) -> str:
        """Get memory context for current prompt (just-in-time loading)"""

        memory = investigation_state.memory
        context_parts = []

        # Hot memory: Last 2 iterations (full fidelity)
        hot_iterations = memory.get_hot_memory()
        if hot_iterations:
            context_parts.append(self._format_hot_memory(hot_iterations))

        # Warm memory: Iterations 3-5 (summarized)
        warm_snapshots = memory.get_warm_memory()
        if warm_snapshots:
            context_parts.append(self._format_warm_memory(warm_snapshots))

        # Cold memory: Older iterations (key facts only)
        cold_snapshots = memory.get_cold_memory()
        if cold_snapshots:
            context_parts.append(self._format_cold_memory(cold_snapshots))

        # Persistent insights: Always include
        if memory.persistent_insights:
            context_parts.append(self._format_persistent_insights(memory.persistent_insights))

        return "\n\n".join(context_parts)

    async def compress_memory_if_needed(
        self,
        investigation_state: InvestigationState,
    ) -> InvestigationState:
        """Compress memory every 3 turns"""

        current_turn = investigation_state.metadata.current_turn

        # Trigger compression every 3 turns
        if current_turn % 3 == 0:
            await self._perform_compression(investigation_state)

        return investigation_state

    async def _perform_compression(
        self,
        investigation_state: InvestigationState,
    ):
        """Perform memory tier transitions"""

        memory = investigation_state.memory

        # Demote iterations 3-5 to warm memory (LLM summarization)
        iterations_to_warm = memory.get_iterations_for_warm_transition()
        if iterations_to_warm:
            warm_snapshot = await self.compression_engine.compress_iterations(
                iterations_to_warm,
                target_tokens=300,
            )
            memory.add_warm_snapshot(warm_snapshot)
            memory.remove_iterations_from_hot(iterations_to_warm)

        # Demote warm memory to cold (fact extraction)
        snapshots_to_cold = memory.get_snapshots_for_cold_transition()
        if snapshots_to_cold:
            cold_snapshot = self._extract_key_facts(snapshots_to_cold)
            memory.add_cold_snapshot(cold_snapshot)
            memory.remove_warm_snapshots(snapshots_to_cold)

        logger.info("Memory compression complete: "
                   f"hot={len(memory.hot_iterations)}, "
                   f"warm={len(memory.warm_snapshots)}, "
                   f"cold={len(memory.cold_snapshots)}")
```

### 4.2 Conversation History Summarization

**Strategy:** Keep only hot memory (last 2 turns) in full fidelity

```python
def get_conversation_history_for_prompt(
    session_id: str,
    max_turns: int = 2,
) -> str:
    """Get recent conversation history (hot memory only)

    Args:
        session_id: Session identifier
        max_turns: Number of recent turns (default: 2)

    Returns:
        Formatted conversation history (~200 tokens)
    """

    # Fetch last N messages from session
    messages = session_service.get_recent_messages(session_id, limit=max_turns * 2)

    history_parts = []
    for msg in messages:
        role = "User" if msg.role == "user" else "Assistant"
        history_parts.append(f"{role}: {msg.content}")

    return "\n\n".join(history_parts)
```

**Optimization:** Older conversation context comes from warm/cold memory (investigation state), not verbatim messages.

### 4.3 Evidence Loading Strategy

**Just-in-Time Evidence Loading** (Anthropic principle):

```python
class EvidenceLoader:
    """Load evidence details only when referenced"""

    async def get_evidence_summary_for_prompt(
        self,
        evidence_items: List[Evidence],
        max_items: int = 5,
    ) -> str:
        """Load evidence summary (lightweight identifiers only)

        Returns:
            Evidence summary with IDs and categories (~100 tokens)
        """

        summary_parts = []
        for evidence in evidence_items[:max_items]:
            # Store only metadata, not full content
            summary_parts.append(
                f"- [{evidence.category}] {evidence.source_type}: "
                f"{evidence.key_finding_summary}"
            )

        return "\n".join(summary_parts)

    async def get_evidence_details(
        self,
        evidence_id: str,
    ) -> Evidence:
        """Fetch full evidence details when agent requests it"""

        # Load from storage only when needed
        return await evidence_service.get_evidence(evidence_id)
```

**Benefit:** Evidence details (logs, config files) not loaded into prompt unless agent references them specifically.

### 4.4 Token Budget Allocation

**Per-Layer Token Budgets:**

| Layer | Component | Consultant Mode | Lead Investigator | Optimization |
|-------|-----------|-----------------|-------------------|--------------|
| 1 | System Identity | 400t | 400t | Compressed bullets |
| 2 | Investigation Framework | - | 250t | Structure only |
| 3 | Engagement Mode | 500t | 500t | Principles, no examples |
| 4 | Phase Context | - | 400t | Current phase only |
| 5 | Investigation State | - | 300t | Top 3 hypotheses |
| 6 | Query + History | 200t | 200t | Hot memory only (2 turns) |
| **Total** | | **~1,100t** | **~2,050t** | **58% reduction** |

**Unoptimized System (for comparison):**
- All conversation history: ~1,500 tokens
- All hypotheses: ~800 tokens
- All evidence details: ~1,200 tokens
- Full phase descriptions: ~600 tokens
- Verbose system prompts: ~800 tokens
- **Total: ~4,900 tokens** (2.4x larger)

---

## 5. Optimization Strategies

### 5.1 Prompt Compression Techniques

#### A. Bullet Points Over Prose

**Before (Verbose):**
```
You are FaultMaven, an expert technical troubleshooting consultant who helps engineers diagnose and resolve complex technical issues. Your role is to act as a knowledgeable colleague providing guidance, answering questions thoroughly, and offering expertise without being pushy or forcing methodology on users who may not want it.
```

**After (Compressed - 50% reduction):**
```
FaultMaven - Expert Technical Troubleshooting

Role: Knowledgeable colleague helping engineers
- Answer questions thoroughly
- Offer guidance naturally
- No forced methodology
```

#### B. Remove Redundant Examples

**Before:**
```
Example 1: User asks "How do I check API status?"
You: "You can check API status with: curl http://localhost:8080/health"

Example 2: User asks "How do I restart the service?"
You: "Restart with: sudo systemctl restart api-service"

Example 3: User asks "How do I check logs?"
You: "View logs with: journalctl -u api-service -n 100"
```

**After (Extract to Few-Shot Library):**
```
[Load 1-2 relevant examples from few-shot library based on query type]
```

**Benefit:** Examples loaded on-demand, not pre-loaded in every prompt.

#### C. Abbreviate Instructions

**Before:**
```
When requesting evidence from the user, always provide three components:
1. What specific information you need
2. Why that information is important for the diagnosis
3. How to obtain it with specific commands, file paths, or UI navigation

For example, if you need database connection counts...
```

**After:**
```
Evidence Request Format:
"I need [WHAT] to understand [WHY].
[HOW: command/file/UI path]
Expected: [result]"
```

### 5.2 Dynamic Layer Loading

**Load layers conditionally based on context:**

```python
async def assemble_optimized_prompt(
    user_query: str,
    investigation_state: Optional[InvestigationState],
    engagement_mode: EngagementMode,
) -> Tuple[str, int]:
    """Assemble prompt with minimal necessary layers"""

    layers = []

    # Layer 1: Always (400t)
    layers.append(get_system_identity())

    # Layer 2: Only if Lead Investigator (250t)
    if engagement_mode == EngagementMode.LEAD_INVESTIGATOR:
        layers.append(get_investigation_framework())

    # Layer 3: Engagement mode (500t)
    layers.append(get_engagement_mode_guidance(engagement_mode))

    # Layer 4: Only if in active phase (400t)
    if investigation_state and investigation_state.lifecycle.current_phase > 0:
        phase_context = get_phase_context(investigation_state.lifecycle.current_phase)
        layers.append(phase_context)

    # Layer 5: Only if hypotheses exist (300t)
    if investigation_state and len(investigation_state.ooda_engine.hypotheses) > 0:
        state_context = get_investigation_context(investigation_state)
        layers.append(state_context)

    # Layer 6: Always (200t)
    query_context = get_query_context(user_query, investigation_state)
    layers.append(query_context)

    prompt = "\n\n---\n\n".join(layers)
    token_count = estimate_tokens(prompt)

    logger.info(f"Assembled {len(layers)} layers, {token_count} tokens")

    return prompt, token_count
```

### 5.3 Few-Shot Example Strategy

**Dynamic Selection** (Anthropic principle: "Do the simplest thing that works"):

```python
class FewShotExampleSelector:
    """Intelligently select few-shot examples based on query"""

    def select_examples(
        self,
        user_query: str,
        current_phase: InvestigationPhase,
        max_examples: int = 2,
    ) -> List[Dict[str, str]]:
        """Select most relevant examples

        Strategy:
        1. Analyze query for intent (troubleshooting vs informational)
        2. Match to current phase
        3. Select 1-2 most similar examples from library

        Returns:
            List of example dicts with 'user' and 'assistant' keys
        """

        # Detect query intent
        if self._is_troubleshooting_query(user_query):
            # Load troubleshooting examples
            examples = self.library.get_by_category("troubleshooting")
        else:
            # Load informational examples
            examples = self.library.get_by_category("informational")

        # Filter by phase relevance
        phase_relevant = [
            ex for ex in examples
            if ex.get("phase") == current_phase.value
        ]

        # Return top N by similarity
        return self._rank_by_similarity(user_query, phase_relevant)[:max_examples]
```

**Token Savings:**
- Pre-loading 5 examples: ~400 tokens
- Dynamic 2 examples: ~150 tokens
- **Savings: 62%**

### 5.4 Prompt Versioning System

**Registry Pattern** for A/B testing and controlled rollout:

```python
from datetime import datetime
from typing import Dict, Callable

class PromptRegistry:
    """Centralized prompt version management"""

    def __init__(self):
        self._prompts: Dict[str, Dict[str, Callable]] = {}
        self._active_versions: Dict[str, str] = {}
        self._metrics: Dict[str, Dict] = {}

    def register(
        self,
        name: str,
        version: str,
        metadata: Optional[Dict] = None,
    ):
        """Decorator to register prompt version"""
        def decorator(func: Callable) -> Callable:
            if name not in self._prompts:
                self._prompts[name] = {}

            self._prompts[name][version] = func

            # Track registration
            if metadata:
                self._metrics[f"{name}:{version}"] = {
                    "registered_at": datetime.utcnow(),
                    "metadata": metadata,
                    "usage_count": 0,
                }

            return func
        return decorator

    def get(
        self,
        name: str,
        version: Optional[str] = None,
    ) -> Callable:
        """Get prompt by name and version"""

        if version is None:
            # Use active version
            version = self._active_versions.get(name, "latest")

        if name not in self._prompts or version not in self._prompts[name]:
            raise ValueError(f"Prompt {name}:{version} not found")

        # Track usage
        metric_key = f"{name}:{version}"
        if metric_key in self._metrics:
            self._metrics[metric_key]["usage_count"] += 1

        return self._prompts[name][version]

    def set_active(self, name: str, version: str):
        """Set active version for prompt"""
        self._active_versions[name] = version

    def get_metrics(self, name: str, version: str) -> Dict:
        """Get usage metrics for prompt version"""
        return self._metrics.get(f"{name}:{version}", {})

# Usage Example
registry = PromptRegistry()

@registry.register("consultant_mode", "2.0", metadata={"optimization": "compressed"})
def consultant_system_prompt_v2():
    return """FaultMaven - Expert Consultant

Role: Colleague helping peer
- Answer thoroughly
- Detect problems
- Offer investigation (once)
- Respect choice

Never mention: "phases", "OODA"
"""

@registry.register("consultant_mode", "1.0", metadata={"optimization": "original"})
def consultant_system_prompt_v1():
    return """You are FaultMaven, an expert technical troubleshooting consultant.

[Original verbose version...]
"""

# Get active version
prompt_func = registry.get("consultant_mode")  # Returns v2.0 if active
prompt = prompt_func()

# A/B test
registry.set_active("consultant_mode", "1.0")  # Switch to v1.0
```

---

## 6. Implementation Guide

### 6.1 Phase Handler Integration

**How phase handlers assemble prompts:**

```python
# File: faultmaven/services/agentic/phase_handlers/intake_handler.py

from faultmaven.prompts.investigation.consultant_mode import (
    get_consultant_mode_prompt,
)

class IntakeHandler(BasePhaseHandler):
    """Phase 0: Intake handler"""

    async def handle(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
    ) -> PhaseHandlerResult:
        """Handle Phase 0: Problem detection and consent"""

        # Analyze for problem signals
        analysis = self.engagement_manager.analyze_initial_query(user_query)
        signal_strength = analysis["signal_strength"]

        # Assemble prompt with appropriate layers
        system_prompt = get_consultant_mode_prompt(
            conversation_history=conversation_history,  # Layer 6
            user_query=user_query,                      # Layer 6
            problem_signals_detected=True,              # Layer 3 modifier
            signal_strength=signal_strength,            # Layer 3 modifier
        )

        # Generate response
        response = await self.generate_llm_response(
            system_prompt=system_prompt,
            user_query=user_query,
            max_tokens=400,
        )

        return PhaseHandlerResult(
            response_text=response,
            updated_state=investigation_state,
        )
```

### 6.2 Memory Manager Integration

**How memory is loaded into prompts:**

```python
# File: faultmaven/services/agentic/phase_handlers/validation_handler.py

from faultmaven.core.investigation.memory_manager import (
    get_memory_manager,
)

class ValidationHandler(BasePhaseHandler):
    """Phase 4: Validation handler"""

    async def handle(
        self,
        investigation_state: InvestigationState,
        user_query: str,
        conversation_history: str,
    ) -> PhaseHandlerResult:
        """Handle Phase 4: Hypothesis validation"""

        # Get memory manager
        memory_manager = get_memory_manager()

        # Load memory context (hot + warm + cold + persistent)
        memory_context = await memory_manager.get_memory_for_prompt(
            investigation_state
        )

        # Assemble Lead Investigator prompt
        system_prompt = get_lead_investigator_prompt(
            current_phase=InvestigationPhase.VALIDATION,
            investigation_strategy=investigation_state.lifecycle.investigation_strategy,
            investigation_state={
                "anomaly_frame": investigation_state.ooda_engine.anomaly_frame,
                "hypotheses": investigation_state.ooda_engine.hypotheses[:3],
                "current_iteration": investigation_state.ooda_engine.current_iteration,
            },
            conversation_history=memory_context,  # ← Hierarchical memory
            user_query=user_query,
        )

        # Generate response
        response = await self.generate_llm_response(
            system_prompt=system_prompt,
            user_query=user_query,
            max_tokens=600,
        )

        # Compress memory if needed (every 3 turns)
        investigation_state = await memory_manager.compress_memory_if_needed(
            investigation_state
        )

        return PhaseHandlerResult(
            response_text=response,
            updated_state=investigation_state,
        )
```

### 6.3 Configuration

**Environment Variables:**

```bash
# .env

# Prompt Configuration
PROMPT_VERSION=2.0                    # Use v2.0 prompts (compressed)
PROMPT_TIER=standard                  # minimal | brief | standard
FEW_SHOT_EXAMPLES_ENABLED=true        # Enable dynamic example loading
FEW_SHOT_MAX_EXAMPLES=2               # Max examples per prompt

# Memory Configuration
MEMORY_COMPRESSION_ENABLED=true       # Enable hierarchical memory
MEMORY_COMPRESSION_INTERVAL=3         # Compress every N turns
MEMORY_HOT_ITERATIONS=2               # Last N iterations in hot memory
MEMORY_WARM_ITERATIONS=3              # Iterations 3-5 in warm memory
MEMORY_USE_LLM_SUMMARIZATION=true     # Use LLM for warm memory compression

# Context Management
CONTEXT_MAX_TOKENS=2000               # Max prompt tokens
CONVERSATION_HISTORY_MAX_TURNS=2      # Max turns in history
EVIDENCE_SUMMARY_MAX_ITEMS=5          # Max evidence items in summary

# Optimization
TOKEN_BUDGET_ENFORCEMENT=true         # Enforce layer token budgets
DYNAMIC_LAYER_LOADING=true            # Load layers conditionally
```

---

## 7. Metrics and Monitoring

### 7.1 Prompt Performance Metrics

**Track per-prompt version:**

```python
class PromptMetrics:
    """Track prompt performance metrics"""

    def track_prompt_usage(
        self,
        prompt_name: str,
        prompt_version: str,
        token_count: int,
        response_quality: float,  # 0.0-1.0
        user_satisfaction: Optional[float] = None,
    ):
        """Track prompt usage and quality"""

        metric = {
            "prompt_name": prompt_name,
            "prompt_version": prompt_version,
            "token_count": token_count,
            "response_quality": response_quality,
            "user_satisfaction": user_satisfaction,
            "timestamp": datetime.utcnow(),
        }

        # Send to metrics system (Prometheus, Datadog, etc.)
        self.metrics_client.record(metric)
```

**Key Metrics:**

| Metric | Description | Target |
|--------|-------------|--------|
| **Token Usage** | Avg tokens per prompt version | <2,000t |
| **Response Quality** | LLM response relevance (0-1) | >0.85 |
| **User Satisfaction** | Feedback score (0-1) | >0.80 |
| **Phase Transition Success** | % phases completed successfully | >90% |
| **Memory Compression Rate** | Token reduction % | >60% |
| **Evidence Request Clarity** | User can fulfill request | >85% |

### 7.2 A/B Testing Framework

**Compare prompt versions:**

```python
class PromptABTest:
    """A/B test prompt versions"""

    def __init__(self, registry: PromptRegistry):
        self.registry = registry
        self.results = {}

    async def run_test(
        self,
        prompt_name: str,
        version_a: str,
        version_b: str,
        sample_queries: List[str],
        metric: str = "response_quality",
    ) -> Dict[str, float]:
        """Run A/B test comparing two versions

        Args:
            prompt_name: Name of prompt to test
            version_a: First version
            version_b: Second version
            sample_queries: Test queries
            metric: Metric to compare (response_quality, token_count, etc.)

        Returns:
            Comparison results
        """

        results_a = []
        results_b = []

        for query in sample_queries:
            # Test version A
            prompt_a = self.registry.get(prompt_name, version_a)()
            result_a = await self._evaluate_prompt(prompt_a, query, metric)
            results_a.append(result_a)

            # Test version B
            prompt_b = self.registry.get(prompt_name, version_b)()
            result_b = await self._evaluate_prompt(prompt_b, query, metric)
            results_b.append(result_b)

        # Compare results
        avg_a = sum(results_a) / len(results_a)
        avg_b = sum(results_b) / len(results_b)

        improvement = ((avg_b - avg_a) / avg_a) * 100

        return {
            "version_a": version_a,
            "version_b": version_b,
            "avg_a": avg_a,
            "avg_b": avg_b,
            "improvement_pct": improvement,
            "winner": version_b if avg_b > avg_a else version_a,
        }
```

### 7.3 Continuous Optimization Process

**Iterative improvement cycle:**

```
1. Baseline Measurement
   ↓
2. Identify Optimization Opportunity
   (High token usage, low quality, user feedback)
   ↓
3. Create Prompt Variation
   (Compress, reorganize, add examples)
   ↓
4. A/B Test
   (Compare with current version)
   ↓
5. Analyze Results
   (Token reduction, quality impact, user satisfaction)
   ↓
6. Deploy Winner
   (Rollout to production)
   ↓
7. Monitor Production Metrics
   ↓
[Repeat]
```

---

## Appendix A: Quick Reference

### Token Budgets by Layer

| Layer | Component | Tokens |
|-------|-----------|--------|
| 1 | System Identity | 400 |
| 2 | Investigation Framework | 250 |
| 3 | Engagement Mode | 500 |
| 4 | Phase Context | 400 |
| 5 | Investigation State | 300 |
| 6 | Query + History | 200 |
| **Total** | | **~2,050** |

### Memory Tier Specifications

| Tier | Iterations | Fidelity | Tokens | TTL |
|------|-----------|----------|--------|-----|
| Hot | Last 2 | Full | 500 | 24h |
| Warm | 3-5 | Summary | 300 | 7d |
| Cold | 6+ | Key facts | 100 | 30d |
| Persistent | All | Insights | 100 | Permanent |

### Phase-Specific Token Budgets

| Phase | Mode | Layers | Tokens |
|-------|------|--------|--------|
| 0: Intake | Consultant | 1,3,6 | ~1,100 |
| 1: Blast Radius | Lead | 1,2,3,4,5,6 | ~1,900 |
| 2: Timeline | Lead | 1,2,3,4,5,6 | ~1,900 |
| 3: Hypothesis | Lead | 1,2,3,4,5,6 | ~1,950 |
| 4: Validation | Lead | 1,2,3,4,5,6 | ~2,050 |
| 5: Solution | Lead | 1,2,3,4,5,6 | ~1,900 |
| 6: Document | Lead | 1,2,3,4,5,6 | ~1,800 |

---

## Appendix B: Implementation Checklist

### Phase 1: Core Infrastructure (Week 1)

- [ ] Implement PromptRegistry class
- [ ] Register all existing prompts with versions
- [ ] Set up prompt metrics tracking
- [ ] Configure environment variables

### Phase 2: Optimization (Week 2)

- [ ] Compress Consultant mode prompt (target: 30% reduction)
- [ ] Compress Lead Investigator prompts (target: 25% reduction)
- [ ] Implement dynamic layer loading
- [ ] A/B test compressed vs original

### Phase 3: Memory System (Week 3)

- [ ] Enhance memory compression engine
- [ ] Implement LLM-powered warm memory summarization
- [ ] Add automatic memory tier transitions
- [ ] Verify 64% token reduction

### Phase 4: Few-Shot System (Week 4)

- [ ] Build few-shot example library
- [ ] Implement dynamic example selection
- [ ] Integrate with phase handlers
- [ ] Measure token savings

### Phase 5: Monitoring (Week 5)

- [ ] Deploy prompt performance dashboard
- [ ] Set up A/B testing framework
- [ ] Configure alerting for quality degradation
- [ ] Begin continuous optimization cycle

---

## Document Status

**Version:** 2.0
**Status:** Active
**Last Updated:** 2025-10-11
**Next Review:** 2025-10-25

**Related Documents:**
- [Investigation Phases and OODA Integration](./investigation-phases-and-ooda-integration.md)
- [Evidence Collection and Tracking Design](./evidence-collection-and-tracking-design.md)
- [Context and Prompt Engineering Analysis](./CONTEXT_AND_PROMPT_ENGINEERING_ANALYSIS.md)
- ⚠️ [Response Format Specification Gap Analysis](./RESPONSE_FORMAT_SPECIFICATION_GAP_ANALYSIS.md) - **CRITICAL: Must Address**
- ✅ [Response Format Integration Specification](./RESPONSE_FORMAT_INTEGRATION_SPEC.md) - **COMPLETE SOLUTION**
- [Doctor-Patient Prompting Architecture (Legacy)](./legacy/DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md)
