# Intent Resolution

How FaultMaven ensures that close-ended choices presented to the user result in unambiguous state changes, regardless of whether the user clicks a suggestion or types a response.

**Related Documents**:
- [Investigation Lifecycle Logic](./investigation-lifecycle-logic.md) - State transitions, path routing
- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) - Overview and philosophy

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Design Principles](#2-design-principles)
3. [Two-Path Architecture](#3-two-path-architecture)
4. [Bounded Choice Matching](#4-bounded-choice-matching)
5. [Hypothesis Action Routing](#5-hypothesis-action-routing)
6. [Implementation Details](#6-implementation-details)
7. [Edge Cases](#7-edge-cases)

---

## 1. Problem Statement

The system presents close-ended choices to users at critical decision points: investigation confirmation, resolution confirmation, hypothesis actions, ambiguous close disambiguation. Each choice carries structured `intent` metadata that drives deterministic state changes when clicked.

**The gap**: When a user types instead of clicking, no intent metadata is attached. The system must determine whether the typed message is an attempt to answer the presented choices or an unrelated conversational input.

### Scenarios Where This Matters

| Decision Point | Choices Offered | What Happens If User Types Instead |
|---|---|---|
| Problem confirmation | "Yes, let's investigate" / "Not quite, let me clarify" | `pending_transition` pattern matching (partial coverage) |
| Resolution confirmation | "Yes, mark as resolved" / "Not yet, continue investigating" | `pending_transition` pattern matching (partial coverage) |
| Close confirmation | "Yes, close this case" / "Not yet, continue investigating" | `pending_transition` pattern matching (partial coverage) |
| Ambiguous close | "Resolved" / "Closed" | Falls through to LLM (no matching) |
| Hypothesis action | "Validate" / "Refute" / "Retire" | **No handler exists** — silent fallthrough |

### What Goes Wrong

1. **Silent fallthrough**: User types "yeah that's not it" meaning to refute a hypothesis. No pattern matches. Message goes to LLM as conversation. LLM may *talk about* refuting but no state change happens.
2. **Brittle patterns**: "yep that did the trick" means resolution but doesn't match `"solution worked"` or `"issue fixed"`.
3. **Context-blind**: Pattern matching doesn't know what was just asked. "Sure" could be confirming anything or just agreeing conversationally.

---

## 2. Design Principles

### P1: Click-first for state changes

COOPERATIVE suggestions with intent metadata are the primary path for all close-ended decisions. Natural, descriptive labels — not mechanical "Yes/No" — make clicking the obvious choice.

### P2: Follow the user

If a user types instead of clicking, we follow them. We don't reject, block, or demand they click. Their message is always processed.

### P3: Bounded choice matching, not general classification

When the last turn offered choices with intent metadata and the user types a short response, we ask a narrow question: "Is this message a response to one of these N specific choices, or something else?" This is a bounded multiple-choice question for the classifier itself — not open-ended intent classification.

### P4: Cheap and fast classifier, only when needed

The classifier runs only when: (a) the last turn had COOPERATIVE suggestions with intent metadata, (b) the user typed text instead of clicking, and (c) the message is short (under 200 characters). Long messages are almost always conversational. The classifier uses `CLASSIFIER_PROVIDER` (Groq/Fireworks — fast, cheap).

### P5: Default to conversation

If the classifier is unsure, the message goes through normal LLM processing. False negatives (missed intent → conversation) are safe. False positives (misclassified conversation → state change) are dangerous. Optimize for precision.

### P6: No pending choice state

The case model does not track "pending choices." The agent's last response and its suggestions *are* the visible pending state. The system reads `last_suggestions` from the case to know what was offered, but no lock or gate is created.

---

## 3. Two-Path Architecture

```
User submits message
       |
       v
  Has intent metadata?  ----YES----> Deterministic routing
       |                              (existing: STATUS_TRANSITION,
       NO                             CONFIRMATION, HYPOTHESIS_ACTION)
       |
       v
  Case has last_suggestions
  with intent metadata?  ----NO-----> Normal LLM processing
       |
       YES
       |
       v
  Message short (<200 chars)?  --NO-> Normal LLM processing
       |
       YES
       |
       v
  ┌─────────────────────────────┐
  │  Bounded Choice Classifier  │
  │                             │
  │  Input:                     │
  │    - user message           │
  │    - N choices with labels  │
  │      and payloads           │
  │                             │
  │  Output:                    │
  │    - choice index (1..N)    │
  │    - OR "none"              │
  └─────────────────────────────┘
       |
       v
  Matched a choice?  ----NO---------> Normal LLM processing
       |
       YES
       |
       v
  Attach matched choice's intent
  metadata to the message and
  route through deterministic
  handler (same as if clicked)
```

---

## 4. Bounded Choice Matching

### Classifier Input

```
The assistant just offered these choices:

1. "Yes, let's investigate" - Confirm the problem statement and start the investigation.
2. "Not quite, let me clarify" - Refine the problem statement before starting.

The user typed: "sounds good, let's dig in"

Which choice (if any) is the user responding to?
Answer with the choice number, or "none" if the message is unrelated or unclear.
```

### Classifier Output

Single token: `1`, `2`, ..., `N`, or `none`.

### Why This Works

- **Tiny prompt**: Only the choices and the user message. No case history, no investigation context.
- **Trivial output**: One token. No structured JSON parsing needed.
- **Bounded**: The classifier picks from a known, small set — not open-ended classification.
- **Cheap**: `CLASSIFIER_PROVIDER` (Groq/Fireworks). Estimated <50 tokens total, <100ms latency.

### When NOT to Run the Classifier

- No `last_suggestions` with intent metadata on the case (nothing to match against)
- User message is >200 characters (conversational, not a short answer)
- User message has file attachments (evidence submission, not a choice answer)
- Intent metadata already present (user clicked — no classification needed)

---

## 5. Hypothesis Action Routing

### Current State (Gap)

`HYPOTHESIS_ACTION` intent is:
- Defined in `QueryIntent` (api_models.py)
- Dispatched by `investigation_service.py:229`
- Forwarded by `_handle_hypothesis_action` (investigation_service.py:577-612)
- **Not handled** in `milestone_engine.py` — falls through to normal LLM processing

The LLM may talk about refuting/validating a hypothesis in its response, but no actual `hypothesis_manager` method is called. The hypothesis stays in its current state.

### Fix

Add a handler in `milestone_engine.py` that:

1. Detects `intent_type == "hypothesis_action"` with `intent_data` containing `hypothesis_id` and `action`
2. Finds the hypothesis in `case.hypotheses`
3. Calls the appropriate `hypothesis_manager` method:
   - `action == "validate"` → set status to VALIDATED, update likelihood to 1.0
   - `action == "refute"` → call `refute_hypothesis()` with user message as reason
   - `action == "retire"` → set status to RETIRED with reason
4. Falls through to LLM processing so the agent can acknowledge and continue

The handler executes the state change *before* LLM processing, so the LLM sees the updated hypothesis state in its context.

---

## 6. Implementation Details

### 6.1 Storing Last Suggestions on Case

Add a `last_suggestions` field to the `Case` model:

```python
last_suggestions: Optional[List[Dict[str, Any]]] = Field(
    default=None,
    description="COOPERATIVE suggestions with intent metadata from the last agent turn. "
    "Used by the intent resolver to match typed responses to offered choices. "
    "Updated after each turn; only suggestions with intent metadata are stored.",
)
```

**Updated after each turn** in `investigation_service.py`, after building `suggested_actions`:

```python
# Store suggestions with intent for next turn's intent resolver
updated_case.last_suggestions = [
    s for s in raw_follow_ups
    if s.get("intent")
]
```

### 6.2 Intent Resolver Module

New file: `faultmaven/core/investigation/intent_resolver.py`

```python
class IntentResolver:
    """Resolves user intent when typed text might be answering offered choices."""

    def __init__(self, llm_router):
        self.llm_router = llm_router

    async def resolve(
        self,
        user_message: str,
        last_suggestions: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Match user message against last turn's suggestions.

        Returns the matched suggestion's intent dict, or None if no match.
        """
        # Guard: skip if message is too long (conversational, not a choice answer)
        if len(user_message.strip()) > 200:
            return None

        # Guard: need suggestions with intent to match against
        suggestions_with_intent = [s for s in last_suggestions if s.get("intent")]
        if not suggestions_with_intent:
            return None

        # Build classifier prompt
        prompt = self._build_prompt(user_message, suggestions_with_intent)

        # Call CLASSIFIER_PROVIDER
        response = await self.llm_router.route(
            messages=[{"role": "user", "content": prompt}],
            capability="CLASSIFIER_PROVIDER",
            max_tokens=10,
            temperature=0.0,
        )

        # Parse response
        return self._parse_response(response, suggestions_with_intent)
```

### 6.3 Integration Point

In `investigation_service.py`, after heuristic greeting detection and before intent routing:

```python
# Intent resolution: match typed text against last turn's suggestions
if (
    intent_type == IntentType.CONVERSATION
    and query
    and case.last_suggestions
):
    resolved_intent = await self.intent_resolver.resolve(
        user_message=query,
        last_suggestions=case.last_suggestions,
    )
    if resolved_intent:
        # Re-route through the matched intent's handler
        intent = QueryIntent(**resolved_intent)
        intent_type = intent.type
```

### 6.4 Hypothesis Action Handler

In `milestone_engine.py`, after the `confirmation` handler (line ~1693) and before pattern matching (line ~1703):

```python
elif intent_type == "hypothesis_action" and intent_data:
    hypothesis_id = intent_data.get("hypothesis_id")
    action = intent_data.get("action")

    if hypothesis_id and action:
        hypothesis = case.hypotheses.get(hypothesis_id)
        if hypothesis:
            if action == "refute":
                self.hypothesis_manager.refute_hypothesis(
                    hypothesis, case.current_turn, [], user_message or "User refuted"
                )
            elif action == "validate":
                hypothesis.status = HypothesisStatus.VALIDATED
                hypothesis.likelihood = 1.0
                hypothesis.last_updated_turn = case.current_turn
            elif action == "retire":
                hypothesis.status = HypothesisStatus.RETIRED
                hypothesis.retirement_reason = user_message or "User retired"
                hypothesis.last_updated_turn = case.current_turn

            metadata["hypothesis_action_applied"] = True

    # Fall through to LLM processing for acknowledgment
```

---

## 7. Edge Cases

### User ignores choices and asks something new

Classifier returns `"none"` → normal LLM processing. The stale suggestions sit in the UI as non-interactive (only current turn's suggestions are interactive). No problem.

### User's message is ambiguous

Classifier returns `"none"` (precision-first). LLM handles it as conversation. If the LLM understands the intent conversationally, it responds accordingly and may re-offer the choices.

### Multiple suggestions match

The classifier picks the single best match. If genuinely ambiguous between two choices, it should return `"none"` rather than guess.

### Classifier unavailable or times out

Default to `None` (no match) → normal LLM processing. The classifier is a best-effort optimization, not a gate.

### User types the exact payload text

This is the trivial case. The classifier easily matches it. But we also add a fast-path: exact string match against suggestion payloads before calling the classifier, avoiding the LLM call entirely.

---

## 8. Resolution Readiness Gate

A separate but related robustness issue: when the user requests "Resolved" but the case lacks required information (root cause + solution), the system asks for more detail. On the next turn, the system must **re-evaluate** whether requirements are now met — not blindly proceed to confirmation.

### Flow

```text
User requests "Resolved"
       |
       v
  assess_resolution_readiness(case)
       |
       ├── READY → propose transition, show confirmation
       ├── NEEDS_INFO → set pending_transition.needs_info=True,
       |                 tell user what's missing
       └── SUGGEST_CLOSE → set pending_transition.needs_info=True,
                           suggest Close instead
       
  ... next turn arrives ...
       |
       v
  Re-run assess_resolution_readiness(case)
       |
       ├── READY → clear needs_info, show confirmation prompt
       |            with "Yes, mark as resolved" / "Not yet" choices
       └── NOT READY (NEEDS_INFO or SUGGEST_CLOSE)
            → cancel pending_transition, suggest Close
              with "Yes, close this case" / "Not yet" choices
```

### Key Rules

1. The case **cannot** be marked as RESOLVED without root cause and resolution on record.
2. The system asks **once** for missing info. If the user can't provide it, the system does not loop — it pivots to offering Close instead.
3. "Not ready after being asked" always converges to suggest Close, regardless of whether the verdict is NEEDS_INFO or SUGGEST_CLOSE. The user already had their chance to provide info.

---

## 9. Pending Transition — Intent Routing Interface

Intent resolution bridges into `pending_transition` handling via the `CONFIRMATION` intent type. When the classifier resolves a user input to `{type: "confirmation", confirmation_value: true|false}`, that metadata feeds into the deterministic dispatch (`confirm_pending_transition()` / `cancel_pending_transition()`).

The full deterministic dispatch table (clear yes / clear no / anything else / repeated dropdown click), the User-Agent Handshake pattern, and the "no LLM tool loop fallthrough when pending_transition exists" invariant are canonical in:

See **[Investigation Lifecycle Logic §1.2 — Pending transition confirmation](./investigation-lifecycle-logic.md#investigating--resolved-disposition)** and the surrounding User-Agent Handshake section.

---

## 10. Post-Terminal Suggestions

After a case reaches terminal state, the agent offers appropriate actions:

| Case status | Suggestions |
|---|---|
| RESOLVED | "Regenerate resolution summary" + "Generate runbook" |
| CLOSED (mitigation_sufficient) | "Regenerate closure summary" + "Generate runbook" |
| CLOSED (other reasons) | "Regenerate closure summary" only |

Report viewing is via Dashboard link (in `ResolutionActionsCard`). Runbook generation is evaluated on click via `evaluate_runbook_suggestion` which checks readiness + deduplication.

---

## Files Changed

| File | Change |
|---|---|
| `faultmaven/core/investigation/intent_resolver.py` | **New** — bounded choice classifier |
| `faultmaven/modules/case/domain/models.py` | Add `last_suggestions` field to `Case` |
| `faultmaven/modules/agent/domain/services/investigation_service.py` | Wire intent resolver; store last_suggestions after each turn |
| `faultmaven/core/investigation/milestone_engine.py` | Add `hypothesis_action` handler; fix resolution readiness gate; deterministic pending_transition handling; post-terminal suggestions |
| `faultmaven/core/investigation/prompts/context_builder.py` | Add `label` attribute to evidence XML for user-facing references |
| `faultmaven/core/investigation/prompts/templates.py` | Refine O/A/C as internal scaffold; evidence label referencing rules |
| `faultmaven/modules/report/domain/services/report_generation_service.py` | Fix hypothesis dict iteration, field names, enum title leakage in reports |
| `faultmaven-copilot/src/shared/ui/components/ChatInterface.tsx` | Allow text Q&A on terminal cases |
| `faultmaven-copilot/src/shared/ui/components/UnifiedInputBar.tsx` | Add `disableAttachments` prop |
