# Choice-Response Resolution

How FaultMaven resolves a user's **response to an agent-offered choice** — whether the user clicks the suggestion or types a reply — into an unambiguous deterministic state change.

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

DECIDE suggestions with intent metadata are the primary path for all close-ended decisions. Natural, descriptive labels — not mechanical "Yes/No" — make clicking the obvious choice.

### P2: Follow the user

If a user types instead of clicking, we follow them. We don't reject, block, or demand they click. Their message is always processed.

### P3: Bounded choice matching, not general classification

When the last turn offered choices with intent metadata and the user types a short response, we ask a narrow question: "Is this message a response to one of these N specific choices, or something else?" This is a bounded multiple-choice question for the classifier itself — not open-ended intent classification.

### P4: Cheap and fast classifier, only when needed

The classifier runs only when: (a) the last turn had DECIDE suggestions with intent metadata, (b) the user typed text instead of clicking, and (c) the message is short (under 200 characters). Long messages are almost always conversational. The classifier uses `CLASSIFIER_PROVIDER`, which ships pinned to `gemini` on
`gemini-3.5-flash-lite` — fast and cheap, and a static pin, so it stays put
when `CHAT_PROVIDER` changes.

### P5: Default to conversation

If the classifier is unsure, the message goes through normal LLM processing. False negatives (missed intent → conversation) are safe. False positives (misclassified conversation → state change) are dangerous. Optimize for precision.

### P6: No pending choice state, but a bounded lifetime

No lock, no gate, no "awaiting answer" flag: the agent's last response and its suggestions *are* the visible pending state, and the system reads `last_suggestions` to know what was offered.

What the stored entries do carry is a **liveness stamp** — the turn that offered them (`offered_turn`), and for a clarification the target file's `data_type` at that moment (`offered_data_type`). Two things forced it (#1245, fm#918):

- A clarification has to outlive its turn. A user who ignores the question and comes back to it two turns later must still be able to answer, so the set cannot simply be "last turn's output" — but an offer that never expires accumulates, and this resolver picks among the stored choices, so an unbounded set is an unbounded chance of resolving an answer onto the wrong file.
- `last_suggestions` is rewritten **only** on `process_turn`'s success path. A mid-turn engine save that is never followed by the final one commits turn *N*'s state beside turn *N-1*'s suggestions; the standalone close/transition endpoints move the case without touching the list at all. "It is in the row" is therefore not evidence that a turn put it there *for now*.

So the adoption site matches against the **live** entries, not the raw field: a `file_reclassification` choice is live for 3 turns, capped at 3 attachments (newest first), and only while its file is still present, unreclassified, and the case is open; every other intent is live for exactly one turn. An entry with no stamp is not live — it was not written by the turn seam, so nothing knows what turn it belongs to. See `_live_suggestions` in `investigation_service.py`.

---

## 3. Two-Path Architecture

```text
User submits message
       |
       v
  Has intent metadata?  ----YES----> Deterministic routing
       |                              (existing: STATUS_TRANSITION,
       NO                             CONFIRMATION, HYPOTHESIS_ACTION)
       |
       v
  Case has LIVE suggestions
  with intent metadata?  ----NO-----> Normal LLM processing
  (in window, referent
   still open — see P6)
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

```text
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
- **Cheap**: `CLASSIFIER_PROVIDER` (ships on `gemini-3.5-flash-lite`). Estimated <50 tokens total, <100ms latency.

### When NOT to Run the Classifier

- No `last_suggestions` with intent metadata on the case (nothing to match against)
- User message is >200 characters (conversational, not a short answer)
- User message has file attachments (evidence submission, not a choice answer)
- Intent metadata already present (user clicked — no classification needed)

### Terminal-Consent Guard at the Adoption Site (#721, INV-26)

A resolver match is an **inference** from typed text, not a deterministic click — but the engine treats adopted intents as click-equivalent consent and consults them *before* its INV-26 bare-token guards. Unguarded, the classifier could match `"yes but what about the replication lag?"` to "Yes, mark as resolved" and irreversibly resolve the case — consuming substantive input as consent, exactly what INV-26 forbids.

So the adoption site (`InvestigationService._minted_intent_swallows_terminal_consent`) rejects a minted intent when **all three** hold:

1. the case has a `pending_transition` (always terminal: `resolved`/`closed`),
2. the minted intent would confirm it (`confirmation` with `confirmation_value=True`, or a `status_transition` matching the pending target), and
3. the message is substantive per `terminal_transitions.is_substantive_reply` — the **same predicate** `_user_confirms_transition` uses (>100 chars, contains `?`, or a contrastive `" but "`), so the two confirm lanes cannot drift.

The rejected message falls back to conversation and flows through the pending-gate escape lane: the proposal is withdrawn, the message is processed as a normal turn, and the engine can re-propose from fresher state. Declines, non-pending confirmations (Gate 1), and contradicting status transitions adopt as before — none can execute a terminal transition. DECIDE clicks are untouched: a click is deterministic consent.

---

## 5. Hypothesis Action Routing

`HYPOTHESIS_ACTION` intent flows through four layers, ending in a state change applied **before** the LLM is consulted so the agent sees the updated hypothesis in its context and can acknowledge it.

### 5.1 Dispatch Chain

| Layer | Location | Responsibility |
|---|---|---|
| Type definition | `api_models.py` `QueryIntent` | Declares `hypothesis_action` as a valid intent type |
| Service dispatch | [`investigation_service.py:528`](../../../faultmaven/modules/agent/domain/services/investigation_service.py) | Routes typed intent to the per-intent handler |
| Service handler | [`investigation_service.py:1026`](../../../faultmaven/modules/agent/domain/services/investigation_service.py) (`_handle_hypothesis_action`) | Validates payload shape, forwards to the engine with `intent_data` |
| Engine handler | [`milestone_engine.py:2520`](../../../faultmaven/core/investigation/milestone_engine.py) (`elif intent_type == "hypothesis_action" and intent_data:`) | Applies the state change on `case.hypotheses[...]` and sets `metadata["hypothesis_action_applied"] = True` |

### 5.2 State Transitions Applied by the Engine

The engine handler (`milestone_engine.py:2520–2554`) dispatches on `intent_data["action"]`:

| Action | State change | Mechanism |
|---|---|---|
| `refute` | Hypothesis → `REFUTED`, refutation reason recorded | `hypothesis_manager.refute_hypothesis(hypothesis=…, current_turn=…, refuting_evidence_ids=[], reason=user_message or "User refuted")` |
| `validate` | Records a strong prior — `likelihood = 1.0`, `last_updated_turn` bumped, `system_feedback` appended — but **does not** set `VALIDATED` | Direct field assignment. VALIDATED is derived solely from the chain root (`project_hypothesis_states_from_roots`); a bare assertion cannot mint it, so the user's belief is realized once evidence validates the root. The definitive user confirmation is the RESOLVED handshake. |
| `retire` | Hypothesis → `RETIRED`, `retirement_reason` recorded, `last_updated_turn` bumped | Direct field assignment |

After the state change the handler **falls through** to normal LLM processing so the agent can acknowledge the action in its reply. The `hypothesis_action_applied` metadata flag lets downstream logging and telemetry distinguish "user explicitly acted on hypothesis" from "agent inferred a status change from prose."

### 5.3 Why "before LLM, not after"

The applied state lives in the case object that the prompt builder reads from on the same turn. If we deferred the state change to after-LLM, the agent would still see the old `ACTIVE` hypothesis on the very turn the user refuted it, and would either re-propose it or contradict the user. Applying first keeps the chat coherent without an extra round-trip.

---

## 6. Implementation Details

### 6.1 Storing Last Suggestions on Case

Add a `last_suggestions` field to the `Case` model:

```python
last_suggestions: Optional[List[Dict[str, Any]]] = Field(
    default=None,
    description="DECIDE suggestions with intent metadata from the last agent turn. "
    "Used by the intent resolver to match typed responses to offered choices. "
    "Updated after each turn; only suggestions with intent metadata are stored.",
)
```

**Updated after each turn** in `investigation_service.py`, after building `suggested_actions`. As shipped this is not a plain rebuild from the turn's own output — see P6 — but an assembly of this turn's clarification choices, the still-live ones carried from earlier turns, and this turn's engine follow-ups, each stamped with the offering turn:

```python
carried = _carry_forward_unresolved_clarifications(
    updated_case.last_suggestions, updated_case, resolved_file_id,
    as_of_turn=updated_case.current_turn + 1,   # what NEXT turn will accept
)
clarification, note = _build_classification_clarification(
    preprocess_results, {_entry_file_id(e) for e in carried},
)
updated_case.last_suggestions = _stored_suggestions(
    case=updated_case, clarification=clarification, carried=carried,
    follow_ups=raw_follow_ups, offered_turn=updated_case.current_turn,
) or None
```

`as_of_turn` is the **next** turn's number, so what is stored is exactly what the next read will accept — the write site and the adoption site apply one predicate and cannot drift.

The stamp rides inside each entry dict, which both repositories persist as opaque JSON (`cases.metadata`), so it needs no migration and no repository change. A per-*entry* key rather than an envelope around the list (`{"turn": N, "suggestions": [...]}`): once anything is carried the set is heterogeneous — this turn's choices and a two-turn-old one share the list and cannot share one stamp.

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
# Intent resolution: match typed text against the choices still on offer.
# ``_live_suggestions``, not the raw field — see P6.
live_suggestions = _live_suggestions(
    case.last_suggestions, case, as_of_turn=case.current_turn
)
if (
    intent_type == IntentType.CONVERSATION
    and query
    and live_suggestions
):
    resolved_intent = await self.intent_resolver.resolve(
        user_message=query,
        last_suggestions=live_suggestions,
    )
    if resolved_intent:
        # Re-route through the matched intent's handler
        intent = QueryIntent(**resolved_intent)
        intent_type = intent.type
```

### 6.4 Hypothesis Action Handler

The handler lives at [`milestone_engine.py:2520`](../../../faultmaven/core/investigation/milestone_engine.py) (`elif intent_type == "hypothesis_action" and intent_data:`). See [§5.1 Dispatch Chain](#51-dispatch-chain) for the four-layer dispatch path and [§5.2 State Transitions Applied by the Engine](#52-state-transitions-applied-by-the-engine) for the per-action state changes (`refute` → `hypothesis_manager.refute_hypothesis(...)`; `validate` → direct assignment with `likelihood = 1.0`; `retire` → direct assignment with `retirement_reason`). After the state change the handler falls through to normal LLM processing so the agent can acknowledge the action in its reply.

---

## 7. Edge Cases

### User ignores choices and asks something new

Classifier returns `"none"` → normal LLM processing. The cards sit in the transcript as non-interactive (only the current turn's suggestions are interactive).

For an engine follow-up that is the end of it — it was about the turn that produced it, and it expires with that turn. A **clarification** is different: the attachment is still misclassified and the user has been offered the only server-side way to fix it. Rebuilding the list from the new turn's output silently withdrew that offer, leaving the file misclassified with no recovery path (#1245). Those choices are carried instead, for the bounded lifetime in P6, so coming back to the question two turns later still works — and stops working after that, which is what keeps the classifier's choice list from growing without limit.

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
       └── SUGGEST_CLOSE → pivot the pending proposal to CLOSED,
                           emit the close confirmation pair
       
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
| CLOSED (with auto-generated summary) | "Regenerate closure summary" only |
| CLOSED (no summary generated — failed substance check) | No suggestions |

Report viewing is via Dashboard link (in `ResolutionActionsCard`). Runbook generation is evaluated on click via `evaluate_runbook_suggestion` which checks readiness + deduplication. Only RESOLVED cases are eligible for runbook generation.
