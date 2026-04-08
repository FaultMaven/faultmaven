# Investigation Journal

A structured, append-only log of key decisions and findings that gives the FaultMaven agent durable long-term memory across an entire investigation. The journal ensures the agent stays sharp in long investigations by preserving distilled insights that would otherwise be lost when conversation history is evicted from context.

**Related Documents**:

- [Context Engineering Analysis](../../reference/deep-dives/context-engineering-analysis.md) — Token budget management and context assembly
- [Progress Transparency](./progress-transparency.md) — Progress monitoring and milestone dependencies
- [Agent Behavioral Rules](./agent-behavioral-rules.md) — Rules governing agent behavior (esp. Rule 2: Evidence-Grounded)
- [Investigation Data Models](./investigation-data-models.md) — Case domain model and turn tracking

---

## The Problem

The FaultMaven agent has persistent memory — the Case object stores all evidence, hypotheses, turn history, working conclusions, and milestones. This is why users can continue an investigation across sessions. However, the LLM that powers the agent is stateless. Every turn, it receives a fresh prompt assembled by the context builder from the agent's persistent state.

The context builder can only fit a fraction of the agent's memory into each LLM prompt. As investigations grow long, the compression becomes lossy — important details that exist in the agent's state don't make it into the prompt the LLM sees:

| What's lost | When | Why it matters |
|---|---|---|
| Specific details from early evidence | After Tier A eviction (~3 turns) | Agent can't cite specific error codes, timestamps, or patterns from early data |
| Conversation nuance | After turn 15 (state summary mode) | User's environment details, throwaway comments that matter later |
| Rejected reasoning | When conversation turns are summarized | Agent may re-propose a hypothesis it already refuted |
| User-provided context | When conversation turns are evicted | "We deployed last Tuesday," "this only happens in EU region" |

The existing durable artifacts (evidence summaries, hypothesis status, working conclusion) each carry part of the picture, but none provides a structured chronological record of what the investigation has established and why.

---

## The Solution: Investigation Journal

An append-only list of short, structured entries that capture the key decisions, findings, user context, and ruled-out paths at each significant turn. The journal is always included in full in the LLM prompt — it's a compact representation of the agent's accumulated knowledge that the LLM can access every turn without the context builder having to choose what to drop.

### Design Principles

1. **Distilled, not verbose.** Each entry is max 200 characters. The journal is a compressed signal, not a conversation transcript.
2. **Append-only.** Entries are never modified or deleted. The chronological record is the ground truth.
3. **Agent-generated.** The LLM produces journal entries as part of its structured output, like it produces evidence and hypotheses.
4. **Always in context.** The context builder includes the full journal in every prompt. At 200 chars per entry and ~1 entry per 2 turns, a 50-turn investigation produces ~25 entries = ~5 KB. Well within budget.
5. **Selective, not exhaustive.** Not every turn produces a journal entry. Only turns with significant findings, decisions, or user context.

### Entry Types

| Type | What it captures | Example |
|---|---|---|
| `finding` | A specific, concrete discovery from evidence | "142 OOM errors from service-A, 14:02-16:45 UTC, correlating with ChromaDB upgrade" |
| `decision` | An investigative direction chosen and why | "Focusing on ChromaDB connection pooling — memory growth matches upgrade timeline" |
| `user_context` | Important context the user provided that isn't evidence | "User deployed ChromaDB 0.4.22 on Feb 9; only EU region affected" |
| `ruled_out` | A hypothesis or direction that was eliminated and why | "Network hypothesis refuted: packet captures show no loss or latency anomalies" |
| `blocker` | Something that's blocking progress | "Cannot verify connection pool settings — user doesn't have access to ChromaDB config" |
| `milestone` | A milestone was reached with key supporting fact | "Root cause identified: ChromaDB 0.4.22 connection pooling disabled by default" |

### Data Model

```python
class JournalEntry(BaseModel):
    """A single entry in the investigation journal.
    
    Captures a distilled insight, decision, or context that the agent
    needs to remember across the entire investigation. Entries are
    append-only and always included in the LLM context.
    """
    
    turn: int = Field(
        description="Turn number when this entry was created"
    )
    
    entry_type: Literal[
        "finding", "decision", "user_context", 
        "ruled_out", "blocker", "milestone"
    ] = Field(
        description="Type of journal entry"
    )
    
    content: str = Field(
        description="The distilled insight (max 200 chars)",
        max_length=200
    )
    
    evidence_id: Optional[str] = Field(
        default=None,
        description="Evidence ID this entry relates to, if any"
    )
    
    hypothesis_id: Optional[str] = Field(
        default=None,
        description="Hypothesis ID this entry relates to, if any"
    )
```

Added to the Case model:

```python
class Case(BaseModel):
    ...
    investigation_journal: List[JournalEntry] = Field(
        default_factory=list,
        description="Structured log of key findings, decisions, and context. "
        "Append-only. Always included in full in LLM context."
    )
```

### LLM Schema Addition

The structured output schema for INVESTIGATING turns adds:

```python
class StateUpdates(BaseModel):
    ...
    journal_entries: Optional[List[JournalEntryOutput]] = Field(
        default=None,
        description="Key findings or decisions to record in the investigation journal. "
        "Only include entries for significant insights — not every turn needs one."
    )

class JournalEntryOutput(BaseModel):
    entry_type: Literal["finding", "decision", "user_context", "ruled_out", "blocker", "milestone"]
    content: str = Field(max_length=200)
    evidence_id: Optional[str] = None
    hypothesis_id: Optional[str] = None
```

### Prompt Injection

The journal is presented to the LLM in the context as a structured block:

```xml
<investigation_journal>
[T3] FINDING: 142 OOM errors from service-A, 14:02-16:45 UTC
[T5] USER_CONTEXT: Deployed ChromaDB 0.4.22 on Feb 9; EU region only
[T7] RULED_OUT: Network hypothesis — no packet loss in captures
[T9] DECISION: Focus on ChromaDB connection pooling (memory growth matches upgrade timeline)
[T12] BLOCKER: User cannot access ChromaDB config to verify pool settings
[T15] FINDING: kubectl top shows ChromaDB pods at 92% memory, growing 5MB/min
[T18] MILESTONE: Root cause — ChromaDB 0.4.22 disabled connection pooling by default
</investigation_journal>
```

The instruction to the LLM:

```text
INVESTIGATION JOURNAL:
The journal below records key findings, decisions, and context from this
investigation. Use it to maintain continuity — do not re-discover what
is already recorded, do not re-propose directions that were ruled out.

If this turn produces a significant finding, decision, or context, add
a journal entry via state_updates.journal_entries. Not every turn needs
an entry — only record what future turns would need to know.
```

### Context Builder Integration

In `build_investigation_context()`, the journal is added as a new section between hypotheses and working conclusion — high in the priority order because it's compact and critical:

```python
# 5b. Investigation Journal (durable long-term memory)
journal_str = ""
if case.investigation_journal:
    journal_str = "<investigation_journal>\n"
    for entry in case.investigation_journal:
        tag = entry.entry_type.upper()
        journal_str += f"[T{entry.turn}] {tag}: {entry.content}\n"
    journal_str += "</investigation_journal>"
```

Budget impact: ~5 KB for a 50-turn investigation (25 entries × 200 chars). This is less than a single Tier A evidence structural index (4 KB cap).

---

## Complementary Improvements

The journal is the primary architectural change. These improvements strengthen the existing mechanisms that work alongside it.

### 1. Richer Evidence Summaries

**Problem:** Evidence summaries are generated at creation time and often vague ("Log file with errors"). Once the structural index falls out of Tier A, only the summary remains. A vague summary means permanent information loss.

**Fix:** Update the evidence creation prompt to require specific summaries:

```text
When creating evidence records, the summary MUST include specific values:
- Counts: "142 errors" not "multiple errors"
- Entity names: "service-A, host-B" not "several services"  
- Time ranges: "14:02-16:45 UTC" not "afternoon"
- Error identifiers: "OOM killed, exit code 137" not "crash errors"
```

This is a prompt change in `templates.py` — no model changes needed. The TRIAGE SUMMARY QUALITY section in the INQUIRY template already has this guidance; it should be replicated in the INVESTIGATING template for evidence creation.

**Implementation:** Add to INVESTIGATION_BASE after the CREATING EVIDENCE RECORDS section:

```text
EVIDENCE SUMMARY QUALITY:
Summaries are the long-term memory for evidence — they persist after the
structural index is evicted from context. Be SPECIFIC:
- BAD: "Log file showing errors from the service"
- GOOD: "142 OOM errors from service-A between 14:02-16:45 UTC (chromadb 0.4.22)"
Include: counts, entity names, time ranges, error codes, version numbers.
```

### 2. Increase Working Conclusion Reasoning Cap

**Problem:** Working conclusion reasoning is truncated to 500 chars in the context builder. For complex investigations with multiple competing hypotheses, 500 chars can't capture the reasoning chain.

**Fix:** Increase from 500 to 1000 chars in `context_builder.py`:

```python
# Current:
conclusion_str += f"REASONING: {wc.reasoning[:500]}\n"

# Change to:
conclusion_str += f"REASONING: {wc.reasoning[:1000]}\n"
```

Budget impact: +500 chars (~125 tokens) in the worst case. Negligible compared to the 8K-32K total budget.

### 3. Hypothesis Refutation Reason

**Problem:** When a hypothesis is refuted, its status changes to REFUTED but the *reasoning* is in a conversation turn that gets evicted. The agent may re-propose a similar hypothesis because it can't see why the original was refuted.

**Fix:** Add `refutation_reason` to the Hypothesis model:

```python
class Hypothesis(BaseModel):
    ...
    refutation_reason: Optional[str] = Field(
        default=None,
        description="Why this hypothesis was refuted (set when status → REFUTED)",
        max_length=200
    )
```

The context builder includes it in the hypothesis block:

```python
if h.status.value == "refuted" and h.refutation_reason:
    hypothesis_str += f"- [REFUTED] {h.statement} — Reason: {h.refutation_reason}\n"
```

The LLM schema adds `refutation_reason` to hypothesis updates:

```python
class HypothesisUpdate(BaseModel):
    ...
    refutation_reason: Optional[str] = Field(
        default=None,
        description="Why this hypothesis is being refuted (required when setting status to REFUTED)"
    )
```

---

## Implementation Plan

### Phase 1: Quick Wins (prompt + config changes only) — DONE

No model changes, no schema changes. Implemented in commit `a4b8924d`.

| Change | File | Status |
|---|---|---|
| Evidence summary quality prompt | `templates.py` (INVESTIGATION_BASE) | Done |
| Working conclusion reasoning cap 500→1000 | `context_builder.py` | Done |

### Phase 2: Investigation Journal

The core feature. Requires model change, schema change, context builder change, and prompt change.

| Change | File | Effort |
|---|---|---|
| `JournalEntry` model | `modules/case/domain/models.py` | Small |
| `investigation_journal` field on Case | `modules/case/domain/models.py` | Small |
| Export from contracts | `modules/case/contracts.py` | Trivial |
| `JournalEntryOutput` in LLM schema | `core/investigation/schemas.py` | Small |
| Journal extraction in milestone engine | `core/investigation/milestone_engine.py` | Medium |
| Journal section in context builder | `core/investigation/prompts/context_builder.py` | Small |
| Journal prompt instructions | `core/investigation/prompts/templates.py` | Small |
| Persistence (metadata blob) | `infrastructure/persistence/database_case_repository.py` | Medium |
| Tests | `tests/unit/core/investigation/` | Medium |

### Phase 3: Hypothesis Refutation Reason

Requires model change and schema change.

| Change | File | Effort |
|---|---|---|
| `refutation_reason` on Hypothesis | `modules/case/domain/models.py` | Small |
| Schema update for hypothesis updates | `core/investigation/schemas.py` | Small |
| Context builder: show refutation reason | `core/investigation/prompts/context_builder.py` | Small |
| Prompt: require reason when refuting | `core/investigation/prompts/templates.py` | Small |
| Tests | `tests/` | Small |

---

## Budget Analysis

Context budget impact of all changes combined, for a 50-turn investigation:

| Component | Current | After changes | Delta |
|---|---|---|---|
| Investigation journal (25 entries) | 0 | ~5 KB | +5 KB |
| Working conclusion reasoning | ~500 chars | ~1000 chars | +500 chars |
| Refutation reasons (~4 refuted hypotheses) | 0 | ~800 chars | +800 chars |
| Evidence summaries | Same size, better quality | Same | 0 |
| **Total additional context** | | | **~6.3 KB (~1600 tokens)** |

Against a typical 8K token budget (32K chars), this adds ~5% utilization. Against a 32K token budget (Anthropic/OpenAI), it adds ~1.2%. Well within budget.

The journal replaces information that was previously in conversation history turns — it's not net-new context, it's a more efficient encoding of the same information.

---

## What This Achieves

| Scenario | Without journal | With journal |
|---|---|---|
| Turn 20: agent needs to reference a finding from turn 3 | Falls back to vague evidence summary, may lose specifics | Journal entry preserves the key fact at 200 chars |
| Turn 15: agent considers a hypothesis similar to one refuted at turn 7 | May re-propose it — refutation reasoning is in evicted history | Journal entry says "RULED_OUT: Network hypothesis — no packet loss" |
| Turn 25: agent needs user context mentioned at turn 4 | Lost — "deployed last Tuesday" was in a conversation turn | Journal entry says "USER_CONTEXT: Deployed ChromaDB 0.4.22 on Feb 9" |
| Turn 30: new team member takes over the investigation | Must read entire transcript to understand state | Journal provides a 25-line summary of everything significant |

The journal is to the LLM what notes are to a human investigator — you don't re-read the entire conversation, you check your notes. The agent remembers everything; the journal ensures the LLM sees what matters.

---

## What This Does NOT Do

- **Does not replace conversation history.** The journal is a complement, not a replacement. Graduated history and state summary still provide recent conversational context.
- **Does not replace evidence.** Evidence artifacts with their structural indexes remain the primary data source. The journal records insights derived from evidence, not the evidence itself.
- **Does not replace the working conclusion.** The working conclusion is the agent's current best answer. The journal is the trail of reasoning that led there.
- **Does not guarantee the agent never repeats itself.** The journal improves recall but the LLM may still miss entries. The journal makes repetition less likely, not impossible.
