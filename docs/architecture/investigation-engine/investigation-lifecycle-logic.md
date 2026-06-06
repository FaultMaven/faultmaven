# Investigation Lifecycle Logic

This document defines the state transitions, the unified opportunistic flow, and turn tracking logic for FaultMaven's evidence-driven investigation framework.

**Related Documents**:
- [Investigation Flow Redesign](./investigation-flow-redesign.md) - Design rationale for the unified flow, assessment variables, and stabilization-as-insert (supersedes the former path fork)
- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) - Overview and philosophy
- [Investigation Data Models](./investigation-data-models.md) - Core data structures

---

## Table of Contents

1. [Investigation Lifecycle](#1-investigation-lifecycle)
2. [Stabilization as an Insert](#2-stabilization-as-an-insert)
3. [Turn Progress Tracking](#3-turn-progress-tracking)
4. [Supported Case Lifecycles](#4-supported-case-lifecycles)

---

## 1. Investigation Lifecycle

### 1.1 Case Action Map

```
┌──────────────┐
│    INQUIRY   │
│              │
│ Exploring    │
└──────┬───────┘
       │
       ├─────(User decides to investigate)────────┐
       │                                          │
       │                                          ▼
       │                              ┌────────────────────┐
       │                              │   INVESTIGATING    │
       │                              │                    │
       │                              │ Investigating      │
       │                              │ Stabilizing        │
       │                              │ Resolving          │
       │                              └─────────┬──────────┘
       │                                        │
       │                              ┌─────────┴──────────┐
       │                              │                    │
       │                   (solution_verified)    (no solution,
       │                              │            abandoned/escalated/
       │                              │            stabilized-then-closed)
       │                              ▼                    ▼
       │                      ┌──────────────┐    ┌──────────────┐
       │                      │   RESOLVED   │    │    CLOSED    │
       │                      │              │    │              │
       │                      │ DISPOSITION  │    │ DISPOSITION  │
       │                      │ With solution│    │ No solution  │
       │                      └──────────────┘    └──────────────┘
       │                                                  ▲
       └──(no investigation needed)──────────────────────┘
          (inquiry-only)
```

### 1.2 Case Actions

#### INQUIRY → INVESTIGATING

**Trigger**: User commits to formal investigation AND confirms problem statement

**CONFIRMATION PATTERN (Conditional, Based on Context)**:

Confirmations reduce errors but create friction. Use conditional logic:

**WHEN TO CONFIRM** (two-step required):

- Situation is CRITICAL/HIGH severity (alignment crucial before action)
- Problem description is ambiguous, inconsistent, or incomplete
- Key details changed that affect investigation direction
- User manually requests case action (via dropdown)
- First time transitioning to INVESTIGATING (establish shared understanding)

**WHEN TO SKIP CONFIRMATION** (natural progression):

- Problem already established and confirmed; user asks follow-up question
- Context is clear and user needs direct answer
- User provides information that refines (not changes) direction

**Two-Step Confirmation Flow** (when required):

1. Agent presents what will happen (problem statement, action, etc.)
2. User explicitly confirms with Yes/No buttons or typed response

**Natural flow (Section 1.2)**:
- Turn N: User says "let's investigate"
- Turn N response: Agent presents problem statement + [Yes/No]
- Turn N+1: User clicks [Yes] or types confirmation
- Turn N+1 response: Agent transitions status

**Deferred recovery (when the LLM tries to collapse the handshake)**:
- Turn N: LLM emits proposed_problem_statement AND `user_confirmed_investigation=True` in one shot
- Engine: same-turn-confirmation guard rejects (see INV-01); sets `handshake_deferred_at_turn = N`
- Turn N+1 context: `<inquiry_state>` switches from `NOT_YET_CONFIRMED` to `HANDSHAKE_DEFERRED` — LLM is told to re-present
- Turn N+1 response: engine deterministically attaches the [Yes/No] confirmation suggestions, so the user has a clickable path regardless of LLM compliance with the re-present instruction

**Manual flow (Section 1.5)**:
- User clicks status dropdown → modal
- User confirms modal → sends system message
- Agent receives system message → presents statement + [Yes/No]
- User confirms → Agent transitions status

Both flows converge at the confirmation step.

```python
async def handle_inquiry_turn(case: Case, user_message: str) -> str:
    """
    Process inquiry turn and manage problem statement workflow.

    ITERATIVE REFINEMENT PATTERN:
    1. Agent generates proposed_problem_statement from conversation
    2. Agent presents statement for confirmation
    3. User confirms OR provides corrections
    4. If corrections: Update proposed_problem_statement and repeat step 2
    5. If confirmed: Set problem_statement_confirmed = True
    """

    # Generate or update proposed_problem_statement
    if not case.inquiry.proposed_problem_statement or user_provides_corrections(user_message):
        case.inquiry.proposed_problem_statement = await llm_generate_problem_statement(
            conversation_history=case.messages,
            problem_confirmation=case.inquiry.problem_confirmation,
            user_corrections=extract_corrections(user_message)
        )

    # Check if user confirms statement
    if user_confirms(user_message):  # "Yes", "Yes, investigate", "That's right", etc.
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
        case.inquiry.decided_to_investigate = True
        case.inquiry.decision_made_at = datetime.now(timezone.utc)

        # Now can_start_investigation returns True
        return await transition_to_investigating(case)

    else:
        # Present statement for confirmation
        return f"""Based on our conversation, the problem is:

{case.inquiry.proposed_problem_statement}

Is this what you want me to investigate?

[✅ Yes]  [❌ No]

💡 Tip: Click a button or type to clarify"""


def _apply_inquiry_updates(case: Case, updates: Any, metadata: Dict[str, Any],
                           user_message: str = ""):
    """
    Handle structured updates during INQUIRY.

    Confirmation routing (two-tier):
      1. Click path — COOPERATIVE confirmation suggestions carry
         intent metadata. A click sends intent_type="confirmation"
         + confirmation_value=True, which the engine routes
         deterministically through IntentResolver (see §1.5.3).
      2. LLM path — the LLM sets `user_confirmed_investigation=True`
         in `state_updates`. The engine accepts it ONLY when a
         `proposed_problem_statement` existed on a PRIOR turn (the
         same-turn-confirmation guard added in commit 13ff2eae, after
         the LLM was observed collapsing the two-step handshake on
         first-turn "please investigate" inputs).

    Free-typed paraphrases ("yes", "proceed") do NOT route through this
    function. The historical word-boundary regex matcher
    (`user_confirms()`) was removed in commit 06cfa834 (2026-03-17)
    when intent-routing for explicit clicks became the canonical
    confirmation path. Typed responses that match a confirmation
    pattern only fire on a TERMINAL case via `_user_confirms_transition`
    (see terminal_transitions handling — disposition paths only).
    """

    # Capture pre-turn state for the same-turn-confirmation guard
    statement_existed_before_turn = bool(
        case.inquiry.proposed_problem_statement
        and case.inquiry.proposed_problem_statement.strip()
    )

    # 1. Capture problem statement
    if updates.proposed_problem_statement:
        case.inquiry.proposed_problem_statement = updates.proposed_problem_statement

    # 2. Check for transition (LLM path) — gated on prior-turn statement
    if (updates.user_confirmed_investigation
            and case.inquiry.proposed_problem_statement
            and statement_existed_before_turn):
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.decided_to_investigate = True
        # ... transition fires via _check_automatic_transitions
```

#### 1.2.1 Evidence Classification Lifecycle

The data model is a strict two-table separation (see
[Evidence-Driven Investigation Framework §5](./evidence-driven-investigation-framework.md#5-evidence-model)
for the canonical definition). Files are data; evidence is a
claim-anchored extract. Evidence is born only when the LLM
deliberately extracts a focused slice in support of a specific
claim — and that only happens during INVESTIGATING.

**Core principles:**

1. **Uploads create UploadedFile only.** File uploads (and pasted
   content / page captures, which are file-ified at intake) persist
   as an `UploadedFile` row with preprocessing artifacts attached
   (`summary`, `structural_index`, `data_type`, coverage
   timestamps). No Evidence row is created at intake.
2. **No evidence creation during INQUIRY.** Evidence presupposes a
   confirmed claim. During INQUIRY the claim is still being formed;
   the LLM may read uploaded files for context (via the structural
   index in the prompt) and respond conversationally, but does not
   emit `evidence_to_add`. The Pydantic `InquiryResponse.InquiryStateUpdate`
   schema does not carry an `evidence_to_add` field; the
   `_apply_inquiry_updates` evidence-creation branch was removed.
3. **Evidence is born during INVESTIGATING.** Once the case
   transitions to INVESTIGATING, the LLM reads the uploaded files'
   structural indexes from the prompt context and decides which
   slices to record as Evidence. It emits `evidence_to_add` entries,
   each carrying a `source_file_id` (copied verbatim from the
   `<evidence file_id="...">` or `<uploaded_file file_id="...">`
   attribute) plus the focused `extract`, `summary`, `category`, and
   `source_type`.
4. **The source invariant.** Every Evidence row has a known source.
   `evidence.source_file_id` is enforced by both a DB CHECK
   constraint (`evidence_source_invariant`) and Pydantic validators
   on `Evidence` and `EvidenceToAdd`. The only legal NULL case is
   `source_type=USER_DESCRIPTION` (the chat-quote case where the
   LLM extracted a verbatim system-output snippet from the user's
   short chat message; the source is the user message at
   `collected_at_turn`).
5. **Milestones from category.** Each of the four claim-anchored
   categories maps to a milestone via `CATEGORY_MILESTONE_MAP`:
   symptom_evidence → `symptom_verified`; causal_evidence →
   `root_cause_identified` (also `solution_proposed` when a
   ProposedAction is created). Mitigation and solution evidence
   advance gate milestones via compliance detection, not via
   category mapping.

**Data layers:**

```text
Upload time (any state): UploadedFile row (file metadata + summary,
                         structural_index, data_type, coverage_*)
INVESTIGATING turn:      Evidence rows born via evidence_to_add,
                         each referencing an UploadedFile via
                         source_file_id (or carrying
                         source_type=USER_DESCRIPTION for the
                         chat-quote case)
Transition INQUIRY → INVESTIGATING: just flips status. No
                         retroactive evidence creation or
                         milestone re-attribution.
```

**Validation:**

`validate_reasoning_first` requires the case to have at least one
actionable Evidence row (or one in `evidence_to_add`) when the LLM
attempts to complete milestones. Under the post-010 model all
Evidence rows are claim-anchored — there is no
`contextual_evidence` escape hatch — so this check is naturally
satisfied by the new model whenever Evidence exists.

#### INVESTIGATING → RESOLVED (Disposition)

**Trigger**: User-Agent Handshake (explicit user confirmation)

**User-Agent Handshake Pattern**:

Disposition actions are NEVER automatic. The agent proposes resolution, and the
user must explicitly confirm before the case action executes.

**Flow**:
1. Agent detects solution effectiveness → includes `ProposedTransition` in response
2. System stores `pending_transition` on case (does NOT execute)
3. Agent's response asks user: "Should I mark this case as resolved?"
4. Next turn: user confirms → system ensures milestone ordering (`solution_proposed` → `solution_accepted` → `solution_verified`) and transitions
5. If user declines → `pending_transition` cleared, investigation continues

**MULTIPLE SOLUTIONS HANDLING**:

If multiple solutions exist, the agent proposes resolution when AT LEAST ONE
solution appears effective. The user confirms which solution resolved the issue.

```python
def propose_transition(case, to_status, summary, evidence_ids=None):
    """Store a pending transition proposal. Does NOT execute.

    For CLOSED transitions, closure_reason is derived by the engine via
    derive_closure_reason() and stored in pending_transition automatically.
    The caller never passes closure_reason directly.
    """
    case.pending_transition = {
        "to_status": to_status,
        "summary": summary,
        "evidence_ids": evidence_ids or [],
        "proposed_at": datetime.now(UTC).isoformat(),
        "proposed_by": "agent",
    }
    if to_status == "closed":
        case.pending_transition["closure_reason"] = derive_closure_reason(case)

def confirm_pending_transition(case, user_id):
    """Execute transition after user confirms.

    Raises ValueError if case is in an invalid state for the requested
    transition (e.g., trying to resolve a case that is not INVESTIGATING).
    pending_transition is only cleared after successful execution.
    """
    if pending["to_status"] == "resolved":
        _execute_resolved_transition(case, user_id)
        # closure_reason is None for RESOLVED
    elif pending["to_status"] == "closed":
        _execute_closed_transition(case, user_id, pending["closure_reason"])
    case.pending_transition = None
    # DISPOSITION - no further case actions
```

**Why not automatic?** The LLM's interpretation of "it works" can be wrong.
The user might mean "this command works" not "the whole system is fixed."
Disposition actions are irreversible, so false positives are costly.

**CLOSED transitions also use the handshake.** Unlike RESOLVED, CLOSED transitions
don't need readiness checks, but `assess_closure_readiness(case)` produces a
meaningful investigation summary for the confirmation prompt. This gives the user a
chance to see what was accomplished before committing to an irreversible action.

**SUGGEST_CLOSE pivot for RESOLVED:** When resolution readiness returns `SUGGEST_CLOSE` (no root cause, no solution, no evidence), both the UI-dropdown path and the LLM-emit path immediately pivot the pending proposal to CLOSED and present the close confirmation pair. The user sees the close prompt rather than a resolve prompt.

**SUGGEST_RESOLVE pivot for CLOSED (symmetric):** When closure readiness returns `SUGGEST_RESOLVE` (case has root cause + solution on record), both the UI-dropdown path and the LLM-emit path pivot the pending proposal to RESOLVED and present the resolve confirmation pair. Closing a resolution-grade case would discard the resolution attribution; the pivot reconciles loose user terminology ("close" vs "resolve") against actual case content. This is the close-side counterpart of the RESOLVED → SUGGEST_CLOSE pivot above — together they form a symmetric strategy: a thin case requested as resolved pivots to close; a rich case requested as closed pivots to resolve.

**`disposition_eligibility` — denormalized read view for UI affordance gating.** The two pivots above kick in at *action time* (when the user has already requested a transition). The complementary preventive measure is to gate the affordances themselves: hide Resolve when the case isn't resolution-grade, and warn on Close when closing would silently discard a documented resolution. The per-case answer is computed by `derive_disposition_eligibility(case)` in `terminal_transitions.py` and persisted to the `cases.disposition_eligibility` column as `{"resolved": <verdict>, "closed": <verdict>}` where each verdict is one of:

- `ready` — disposition is appropriate; render enabled with the default confirm UX.
- `needs_info` — disposition is allowed but the case is partial; user must ADD information before transitioning. Currently only the Resolve side surfaces this.
- `suggests_alternative` — disposition is allowed but the system recommends the OTHER disposition for this case (e.g., resolution-grade case clicked-to-close). User is asked to RE-DIRECT, not to add data. Currently only the Close side surfaces this.
- `not_eligible` — disposition is not available; hide the affordance entirely.

`needs_info` and `suggests_alternative` are kept as distinct values rather than overloading one label, because they drive different UX patterns (add-data vs reconsider-action). The column is maintained at the **single chokepoint `CaseRepository.save()`** (pattern P3) — every save calls the derive helper and rewrites the column, so the value can never drift from current case content without per-mutation-site update burden. The UI adapter passes the column through to all three `CaseUIResponse_*` variants; the frontend renders the dropdown menu against `disposition_eligibility`, not just `valid_next_states` (the structural action graph). Distinction: `valid_next_states` answers *which edges exist*; `disposition_eligibility` answers *which edges make sense given current content*.

**needs_info flag for RESOLVED:** When resolution readiness returns `NEEDS_INFO`, the system stores the pending transition with `needs_info=True`. This remembers the user's intent to resolve. On subsequent turns, the system re-evaluates readiness via `assess_resolution_readiness()`:
- **READY** → clears `needs_info`, overrides LLM response with confirmation prompt
- **Still not ready** → cancels pending transition, suggests Close instead (no re-ask loop — the user was already asked once and couldn't provide the info). The resulting CLOSE proposal is terminal-clean: the root-cause analysis and full history are preserved.

> **Loop-bound (`resolution_suggest_close` guard).** The "suggests Close instead" pivot above is produced by the handshake block early in `_check_automatic_transitions`. But the user typically re-confirms ("yes, it's resolved") and the LLM dutifully re-emits `proposed_transition=resolved` on the **same** turn — and the later LLM-proposal block in the same method calls `propose_transition`, which **replaces `pending_transition` wholesale**, clobbering the CLOSE pivot. Unguarded, that re-arms RESOLVED+`needs_info` every turn → the gate loops to max_turns ([project-resolution-gate-stuck-loop]; Run 36). The invariant is enforced by a guard: when the handshake block has set `metadata["resolution_suggest_close"]` this turn, the LLM's same-turn `proposed_transition` is **ignored** so it cannot overwrite the escape. This is what makes "no re-ask loop" actually hold — independent of *why* the case is in resolution `NEEDS_INFO` (instant/index resolution, mitigation-first, or any desync that leaves 0 `Solution` records).
>
> **Gate strictness is intentionally unchanged here.** `assess_resolution_readiness` still gates the *solution* requirement on a `Solution` record (`bool(case.solutions)`); a substantively-resolved case without one (e.g. instant index-resolution that records `solution_evidence` but never `solutions_to_add`) therefore CLOSES rather than RESOLVES. Tightening the gate to require *absence evidence* (`symptom_absence`/`causal_absence` — the only failure-proof, non-circular "the fix worked" signal) is the agreed end-state, but is sequenced **after** the success flow reliably emits absence evidence on confirmed resolution (it currently records `solution_evidence`). See [project-resolution-gate-stuck-loop] for the step-2 (absence-emission) → step-3 (absence-only gate) sequencing and the tripwire that enforces the order.

**Pending transition confirmation — all paths deterministic:** When a `pending_transition`
exists (not `needs_info`), the user's response is handled without LLM involvement:
- **Clear yes** (pattern match or intent metadata) → execute transition
- **Clear no** (pattern match or intent metadata) → cancel transition, acknowledge
- **Anything else** (ambiguous, long message, unrelated question) → re-present the
  confirmation with COOPERATIVE suggestions (clickable Yes/No with intent metadata)

No message falls through to the LLM tool loop when a `pending_transition` exists. This
prevents crashes from the LLM failing to produce tool calls on short ambiguous messages.

**Repeated status_transition intent:** If a user clicks the same dropdown option again
after the agent already proposed the transition, this is treated as an implicit
confirmation (the intent's `to_status` matches the pending transition's `to_status`).

**Contradicting status_transition intent:** If a user clicks a *different* dropdown
option while a pending transition exists (e.g., "Close" is pending but user clicks
"Investigating"), the pending transition is cancelled and the new intent is processed
normally. This handles the case where the user changes their mind after requesting
a transition.

##### KB-Resolution Path (Same-Turn Variant)

When a runbook from the KB applies cleanly to the case, the INVESTIGATING → RESOLVED handshake collapses into a single confirmation turn. This is **not** a separate transition edge — it is the same INVESTIGATING → RESOLVED disposition with all required state (`RootCauseConclusion`, `Solution`, gate milestones) populated in one turn from the matched runbook Cause rather than across many investigation turns.

**Signal**: The LLM emits `knowledge_resolution` in `state_updates` when the user confirms that a runbook fix proposed in an earlier turn resolved their issue ("That fixed it", "It worked", "Yes, resolved").

```python
class KnowledgeResolution(BaseModel):
    """User-confirmed resolution via knowledge base match.

    Emitted by the LLM when the user confirms that a runbook fix proposed
    in an earlier turn resolved their issue. Triggers same-turn milestone
    collapse: the engine populates RootCauseConclusion, creates Solution,
    and sets gate milestones from the attributed Cause's content, then
    fires the standard RESOLVED transition.
    """
    match_id: str                # ID of the matched runbook
    match_type: str              # "runbook" | "past_case" | "documentation"
    solution_applied: str        # What the user actually did
    user_confirmation: str       # User's confirmation message
```

**Engine behavior on `knowledge_resolution`** (during INVESTIGATING turn processing):

1. **Attribute the active Cause.** Engine runs [Indicator resolution](./indicator-resolution.md) against current case state to identify which `### Cause <X>` from the matched runbook applies. If `verdict="single"`, proceed. If `verdict="multiple"`, defer the collapse: agent asks for a disambiguating Diagnostic Step finding before completing the transition. If `verdict="none"`, the fallback Cause is selected.
2. **Populate `RootCauseConclusion`** by direct field copy from the attributed Cause's ChromaDB metadata (no LLM extraction call):
   - `root_cause` ← Cause `Statement` (≤300 chars)
   - `mechanism` ← Cause `Mechanism` (≤800 chars)
   - `evidence_basis` ← runbook ID + user's confirmation message reference
3. **Create `Solution`** from the attributed Cause's blocks:
   - `immediate_action` ← Cause `Mitigation` (with risk + duration metadata)
   - `longterm_fix` ← Cause `Resolution`
4. **Set gate milestones** in the standard order: `solution_proposed=True`, `solution_accepted=True`, `solution_verified=True`. Progress indicator `root_cause_identified=True`.
5. **Fire the standard handshake.** With milestone state populated, the LLM's response on this same turn emits `ProposedTransition` to RESOLVED. The user's confirmation message that triggered `knowledge_resolution` is recognized as the disposition acknowledgment — no additional confirmation turn is required.

**Why the handshake collapses cleanly.** The user already confirmed the fix worked (that's what produced `knowledge_resolution`). The standard disposition invariant — explicit user confirmation — is satisfied by the same "it worked" message that serves as the `solution_verified` signal. The engine does not auto-resolve; it recognizes the user's existing confirmation as covering both signals.

**Why this is not a fast-track.** Earlier designs allowed an `INQUIRY → RESOLVED` edge that bypassed INVESTIGATING entirely, producing terminal cases with empty `RootCauseConclusion` / `Solution` / `evidence` records — the Resolution Summary report had nothing to render. The unified path eliminates that edge: every RESOLVED case flows through INVESTIGATING and produces complete bookkeeping. KB-driven cases are simply the variant where INVESTIGATING completes in 1–2 turns because the cause and fix come pre-packaged from a runbook Cause subsection.

**Authoring requirements upstream.** The same-turn collapse depends on runbook Causes carrying structured `Statement`, `Mechanism`, `Mitigation`, `Resolution`, and `Verification` fields — see [runbook-content-architecture.md §3](../knowledge-and-ai/runbook-content-architecture.md#3-standardized-runbook-template). Runbooks not following the v3 template cannot drive same-turn collapse; cases retrieving them fall back to standard multi-turn investigation.

#### INVESTIGATING → CLOSED (Disposition)

**Trigger**: User-Agent Handshake (same pattern as RESOLVED)

Both dropdown and NLP abandonment patterns propose a pending transition with a
closure readiness summary. The user must confirm before the transition executes.

`assess_closure_readiness(case)` summarizes what was accomplished (evidence count,
hypotheses explored, milestones completed, root cause, solutions) for the confirmation
prompt. Two verdicts: `HAS_SUBSTANCE` (shows summary) or `TRIVIAL` (minimal data warning).

```python
closure = assess_closure_readiness(case)
propose_transition(
    case=case,
    to_status="closed",
    summary=closure.message,
    # closure_reason derived by engine via derive_closure_reason():
    # "inquiry_only" | "closed_after_investigation"
)
# User confirms → _execute_closed_transition(case, user_id, closure_reason)
```

#### INQUIRY → CLOSED (Disposition)

**Trigger**: User-Agent Handshake (same pattern as above)

```python
closure = assess_closure_readiness(case)
propose_transition(
    case=case,
    to_status="closed",
    reason="User expressed close intent from INQUIRY",
    summary=closure.message,
    closure_reason="inquiry_only",
)
# User confirms → _execute_closed_transition(case, user_id, "inquiry_only")
```

### 1.3 Valid Transitions Summary

The valid-action graph below is realized in code as `ALLOWED_ACTIONS` in `case_action_manager.py` (UI-affordance source for the dropdown) and as the local `valid_actions` dict inside `is_valid_action()` in `models.py` (Pydantic model_validator on every `CaseAction` instantiation). Both surfaces currently agree; see the INV-04 drift notes below for the consolidation status.

```python
ALLOWED_ACTIONS = {
    CaseState.INQUIRY: [
        CaseState.INVESTIGATING,   # Start formal investigation (always required, even for KB-matched cases)
        CaseState.CLOSED           # Inquiry-only, no investigation
    ],
    CaseState.INVESTIGATING: [
        CaseState.RESOLVED,        # Solution verified (terminal) — includes the same-turn KB-resolution variant
        CaseState.CLOSED           # Abandoned (terminal)
    ],
    CaseState.RESOLVED: [],        # DISPOSITION - no further case actions
    CaseState.CLOSED: []           # DISPOSITION - no further case actions
}
```

There is no `INQUIRY → RESOLVED` edge. KB-driven cases route through INVESTIGATING via the same-turn milestone collapse documented under [INVESTIGATING → RESOLVED → KB-Resolution Path](#kb-resolution-path-same-turn-variant) — confirming problem understanding is mandatory before any solution is proposed, including for runbook-matched cases.

**Case Action Diagram**:

```
┌──────────────┐
│    INQUIRY   │
│              │
│ Exploring    │
└──────┬───────┘
       │
       ├─────(User confirms problem statement)───┐
       │                                         │
       │                                         ▼
       │                             ┌────────────────────┐
       │                             │   INVESTIGATING    │
       │                             │                    │
       │                             │ Investigating      │
       │                             │ Stabilizing        │
       │                             │ Resolving          │
       │                             │                    │
       │                             │ (collapses to 1–2  │
       │                             │  turns when a v3   │
       │                             │  runbook Cause     │
       │                             │  applies; standard │
       │                             │  multi-turn        │
       │                             │  otherwise)        │
       │                             └─────────┬──────────┘
       │                                       │
       │                             ┌─────────┴──────────┐
       │                             │                    │
       │                  (solution_verified)   (no solution)
       │                             │                    │
       │                             ▼                    ▼
       │                     ┌──────────────┐    ┌──────────────┐
       │                     │   RESOLVED   │    │    CLOSED    │
       │                     │              │    │              │
       │                     │ DISPOSITION  │    │ DISPOSITION  │
       │                     │ With solution│    │ No solution  │
       │                     └──────────────┘    └──────────────┘
       │                                                 ▲
       └──(inquiry-only)────────────────────────────────┘
```

### 1.3.1 Invariant Enforcement Matrix

Every load-bearing lifecycle rule has at least one enforcement surface: code, schema, API, or prompt. The categories below carry *decreasing* strength — what's *structural* cannot be violated by construction; what's *prompt-only* depends on LLM compliance, which is stochastic and prone to drift across model versions and prompt edits.

**What this matrix is.** A compact index of lifecycle invariants with their enforcement-tier classification and pinning tests. It serves three functions:

1. **Risk catalog** — the Enforcement column tells you at a glance which invariants are weakest (Prompt-only > Code-guarded > Schema > Structural). Weaker-tier invariants warrant more test investment and more careful PR review.
2. **Test registry** — every row commits to at least one mechanical pin. Writing a row forces the question "what test pins this?"; gaps are visible.
3. **Single-diff change surface** — when a refactor moves an invariant to a weaker tier, the diff is visible in one place. Code review can catch the regression risk explicitly.

**What this matrix is not.** It is not a drift detector. Tables don't fire — they index. Drift detection happens at test-execution time (mechanical pins), in PR review (when the matrix surfaces tier changes), and via runtime telemetry (separately scoped). Re-reading the matrix doesn't catch a system that has silently stopped achieving its design goal — see *dynamic drift* below.

**Dynamic drift.** A failure-mode class where the system as a whole stops achieving its design goal while every individual piece (matrix row, test, doc section, code, prompt) looks locally correct. It arises from cooperation between enforcement tiers — a code-guarded check depending on a prompt rule that has independently drifted, or two prompt rules with an implicit contract that one of them breaks. No static instrument catches this class: tests pass, the matrix stays consistent, code review is clean. Only runtime mitigations catch dynamic drift: real-LLM integration tests at PR time, outcome telemetry post-deploy (e.g., a *guard-fired-to-transition* ratio), and happy-path canary probes. At design time, the one static instrument that helps is **naming composition seams explicitly** — see the *Composition seam* line on rows where enforcement crosses tiers. Future audits can then verify both sides of the dependency rather than discovering it post-regression.

**Enforcement legend:**

- **Structural** — impossible to violate by construction (e.g., a state change that requires two separate function calls separated by case persistence and an LLM turn).
- **Code-guarded** — an explicit `if` / assertion in the engine blocks the bad path.
- **Schema** — Pydantic validator or DB CHECK constraint.
- **API-level** — middleware or route-handler rejects the bad request.
- **Prompt-only** — the LLM is instructed but the engine doesn't enforce. Weakest category; should be reserved for stylistic rules or rules with downstream code/structural backstops.

| # | Invariant | Source | Enforcement | Test |
|---|---|---|---|---|
| INV-01 | INQUIRY → INVESTIGATING requires the user to confirm a `proposed_problem_statement` that was presented on a **prior** turn. The LLM cannot collapse the handshake into a single turn. | §1.2 *Two-Step Confirmation Flow* (above); `INQUIRY_TEMPLATE` ("Never set `user_confirmed_investigation=True` on the same turn you first present the problem statement") | **Code-guarded at three layers.** (1) Same-turn guard: `_apply_inquiry_updates` captures `_statement_existed_before_turn` and gates the confirmation branch on it. Same-turn confirmations log a WARNING, set `case.inquiry.handshake_deferred_at_turn = current_turn`, and are deferred. (2) Context-side recovery: on the turn immediately after a guard fire, `context_builder` switches the inquiry_state block from `NOT_YET_CONFIRMED` to `HANDSHAKE_DEFERRED` (instructing the LLM to re-present + ask). (3) Engine-owned Gate 1 affordance: `engine_owned_affordances(case, metadata)` in `milestone_engine.py` returns `("gate1", _investigation_confirmation_suggestions())` on **every** Gate-1-pending turn — not only the handshake-deferred recovery turn. This is the architectural completion that makes Gate 1 symmetric with Gate 2 and Gate 3: the engine owns the canonical clickable affordance whenever a `proposed_problem_statement` awaits confirmation, regardless of whether the LLM emits its own confirmation-shaped suggestions or LLM provider drift affects optional output fields like `intent`. *Composition seam:* the prompt-side `HANDSHAKE_DEFERRED` re-present instruction and the engine-side affordance emission are tied — removing or weakening the prompt block does not break the invariant (buttons still emit, user can still click) but degrades UX: confirmation buttons appear next to a response that doesn't ask the question. Audit `context_builder.py:inquiry_state_str` and `milestone_engine.py:engine_owned_affordances` together when either changes. *Outcome telemetry:* `faultmaven_inquiry_handshake_deferred_total` paired with `faultmaven_inquiry_handshake_recovered_total` (recovery ratio for the same-turn-guard fire path); `faultmaven_engine_owned_affordance_served_total{gate="gate1"}` counts every turn where the engine substituted the Gate-1 affordance pair — a sustained zero on `gate1` with non-zero INQUIRY volume signals the predicate or consolidator has regressed silently. See [`docs/operations/monitoring/lifecycle-metrics.md`](../../operations/monitoring/lifecycle-metrics.md). | `test_inquiry_transition::test_same_turn_confirmation_is_rejected`; `::test_confirmation_accepted_when_statement_persisted_across_turns`; `TestHandshakeDeferredRecovery::test_handshake_deferred_block_injected_on_recovery_turn`; `::test_not_yet_confirmed_block_used_when_flag_is_stale`; `::test_deterministic_confirmation_suggestions_on_recovery_turn`; `TestEngineOwnedGate1OnFirstDetect::test_first_detect_turn_emits_deterministic_confirmation_pair`; `test_engine_owned_affordances::TestGate1Predicate::*`; `test_engine_owned_affordances::TestEngineOwnedAffordances::test_gate1_pending_returns_confirmation_pair` |
| INV-02 | INQUIRY → INVESTIGATING never auto-fires on CRITICAL/HIGH urgency alone — confirmation is still required regardless of severity. | §1.2 *Two-Step Confirmation Flow* — "Even for CRITICAL + ongoing issues" | **Code-guarded** — the urgency branch in `_apply_inquiry_updates` logs only; the transition gate still requires explicit `user_confirmed_investigation=True`. | `test_inquiry_transition::test_critical_outage_stays_inquiry_until_confirmed` |
| INV-03 | Disposition transitions (INVESTIGATING → RESOLVED, INVESTIGATING → CLOSED, INQUIRY → CLOSED) NEVER auto-fire. The agent emits `ProposedTransition`; the user confirms on a subsequent turn. | §1.2 *INVESTIGATING → RESOLVED (Disposition)*; §1.4 line 488 ("Disposition actions are NEVER automatic") | **Structural** — `propose_transition` writes `pending_transition`; only `confirm_pending_transition` executes the state change. The two functions cannot be called within the same `process_turn` invocation without an intervening case save and LLM turn. *Composition seam:* the structural propose+confirm split preserves the no-auto-fire invariant regardless of prompt behavior, but the user-facing confirmation UX depends on (a) the engine setting `metadata["override_suggestions"]` at `propose_transition` call sites and (b) the LLM surfacing a "please confirm" cue in `agent_response` per `_AMBIGUITY_FIRST_RULE`. Removing the prompt rule does not break the invariant (buttons still emit deterministically; users can still confirm by typing) but does degrade UX: the buttons appear next to a response that doesn't acknowledge them. Audit `propose_transition` call sites in `milestone_engine.py` and the `_AMBIGUITY_FIRST_RULE` block in `templates.py` together when either changes. | `test_inv03_*` in `test_lifecycle_invariants.py` (5 tests pin the function-level contract: propose writes pending only, confirm-without-propose is a no-op, full handshake executes only via explicit confirm, decline clears pending) |
| INV-04 | INQUIRY → RESOLVED has no direct edge. Every RESOLVED case flows through INVESTIGATING — even KB-matched cases. | §1.3 (line 442); `ALLOWED_ACTIONS` dict + `is_valid_action()` | **Schema** (`is_valid_action()` in `models.py` runs as a Pydantic model_validator on every `CaseAction` instantiation — `CaseAction` is `frozen=True` so the audit history cannot record the forbidden transition) + **Code-guarded** (`_execute_resolved_transition` raises `ValueError` on non-INVESTIGATING input — runtime backstop). `ALLOWED_ACTIONS` in `case_action_manager.py` is UI-affordance only (drives `get_allowed_transitions`). | `test_inv04_*` in `test_lifecycle_invariants.py` |
| INV-05 | The derived stage label within INVESTIGATING (Investigating / Stabilizing / Resolving) follows the action-compliance gates AUTOMATICALLY — NO User-Agent Handshake. The "Stabilizing" label is driven by the single `progress.stabilization` record (accepted ∧ ¬verified), not by `mitigation_*` booleans. This is the only place where LLM-emitted compliance signals change derived state without explicit user confirmation, by design. | §1.4; §2 | **Schema-derived** — `InvestigationProgress.current_stage` is a pure computed property over the stabilization record + `solution_accepted`/`solution_verified`, with no handshake plumbing. The stabilization record's own validator enforces `verified ⇒ accepted`. | `test_inv05_*` in `test_lifecycle_invariants.py` (initial stage is Investigating; an accepted-but-unverified stabilization shows Stabilizing with no pending_transition; solution_accepted shows Resolving; static guard that the property body contains no handshake tokens) |
| INV-06 | The KB-Resolution same-turn collapse (INVESTIGATING → RESOLVED in one turn for runbook-matched cases) goes through `propose_transition` + `confirm_pending_transition` — the same mechanism as the multi-turn path. The engine fires confirm in the same turn ONLY when both `transition_proposed_this_turn` and `knowledge_resolution_signalled` metadata flags are set; the user's runbook-confirmation message ("it worked") is the implicit disposition acknowledgment. Every other ProposedTransition emission still follows the standard 2-turn handshake. | §1.2 *KB-Resolution Path* (lines 380-385); §4.2 | **Structural** — uses the same `pending_transition` mechanism as the multi-turn path. The same-turn collapse is the only deviation, scoped via metadata-flag conjunction. *Composition seam:* the same-turn collapse trigger depends on the LLM emitting `knowledge_resolution` in `state_updates` when a runbook fix is acknowledged — the engine's conjunction in `_check_automatic_transitions` cannot fire without the prompt-driven signal. Removing the prompt instruction to emit `knowledge_resolution` does not break the invariant (multi-turn fallback works; cases reach RESOLVED via the standard 2-turn handshake) but defeats the KB-resolution UX optimization. Audit the `knowledge_resolution` emission rule in `INVESTIGATION_BASE` (`templates.py`) and the conjunction gate in `milestone_engine.py:_check_automatic_transitions` together when either changes. | `test_inv06_*` in `test_lifecycle_invariants.py` |
| INV-07 | Evidence rows are born only during INVESTIGATING. No Evidence creation during INQUIRY — `InquiryStateUpdate` has no `evidence_to_add` field. Uploads during INQUIRY persist as `UploadedFile` only. | §1.2.1 *Core principles* | **Schema** — `InquiryStateUpdate` Pydantic model does not declare `evidence_to_add`; engine `_apply_inquiry_updates` has no evidence-creation branch. The model uses `extra='ignore'` (Pydantic default) deliberately — LLM emissions of unknown fields are silently dropped to preserve graceful degradation rather than failing the turn; the invariant is enforced by field absence, not by rejecting strays. | `test_inv07_*` in `test_lifecycle_invariants.py` |
| INV-08 | Every Evidence row has a known source: `source_file_id` set, **or** `source_type=USER_DESCRIPTION` for chat-quote evidence. There is no escape hatch. | §1.2.1 lines 215-222 | **Schema + DB** — Pydantic validators on `Evidence` and `EvidenceToAdd`; DB CHECK constraint `evidence_source_invariant` (migration 010). | exhaustive evidence-model tests |
| INV-09 | Terminal cases (RESOLVED/CLOSED) are immutable: no new evidence, no transitions, no milestone updates. Only text Q&A, report regeneration, and runbook creation are permitted. | §1.7 *Terminal Mode* (lines 1039-1080) | **API-level** — `require_case_not_terminal()` helper function (used at the case-update endpoint; other write endpoints inline the `case.is_terminal` check). **Code-guarded** — `_process_turn_impl` short-circuits to `_process_terminal_turn` for terminal cases, bypassing the milestone-engine state-mutation pipeline. | `test_inv09_*` in `test_lifecycle_invariants.py` |
| INV-10 | `submit_turn` on a terminal case: text query → routed to terminal Q&A; files / pasted content → 409 Conflict; status-transition intent → 409 Conflict. | §1.7 *Terminal Mode* (lines 1072-1078) | **API-level** — `submit_turn` endpoint inspects payload kind. | `test_inv10_*` in `test_lifecycle_invariants.py` (3 static-source guards on `routes.submit_turn`: files/pasted_content + 409 rejection block; status_transition + 409 rejection block; no unconditional rejection of text queries) |
| INV-11 | Auto-generated `CLOSURE_SUMMARY` is gated on **investigation substance** (`evidence>0` OR `hypotheses>0` OR `completed_milestones>0`). The verdict is stable post-closure because all three signals are immutable in CLOSED state. `RESOLUTION_SUMMARY` always generates. | §1.7.3, §4.5.0 | **Code-guarded** — `should_generate_terminal_summary` in `terminal_transitions.py` | `test_milestone_engine::test_summary_guardrail_*`; `::test_skip_reason_*` |
| INV-12 | Free-typed paraphrases of regen/runbook intent ("recap", "summarize", "new runbook please") route to terminal Q&A and **never** produce a persisted Report or Runbook side effect. Only **exact-match** of the COOPERATIVE-suggestion payload triggers a persisted side effect. | §1.7.3 *Regeneration* (free-text routes to Q&A) | **Code-guarded** — `_REPORT_REGEN_PATTERNS` and `_RUNBOOK_CREATION_PATTERNS` use exact-match (`msg_lower in patterns`); paraphrases fall through to `_process_terminal_qa`. | `test_inv12_*` in `test_lifecycle_invariants.py` (4 tests: exact payload triggers regen; free-typed recap/summarize paraphrases route to Q&A; runbook paraphrases route to Q&A; pattern tuples stay in lockstep with COOPERATIVE-suggestion payload constants) |
| INV-13 | The closure-acknowledgment turn's suggestion set depends on whether summary generation succeeded. **Success path**: RESOLVED offers the runbook affordance only (no regen); CLOSED is silent. The freshly-rendered inline summary is right above; regen alongside would be noise. **Failure path**: regen IS offered on the ack-turn — `_select_ack_follow_ups` returns `_resolved_suggestions()` (regen + runbook) for RESOLVED and `_closed_suggestions(case)` (regen, since substance had to PASS for generation to be attempted) for CLOSED. Regen is otherwise offered on subsequent terminal Q&A turns when the substance gate would PASS. | §1.7.3 *Regeneration: Where it's offered* | **Code-guarded** — `_select_ack_follow_ups` in `milestone_engine.py` switches on the `summary_failed` flag returned by `_auto_generate_report`. All three ack-turn call sites (explicit confirm, dropdown second-click, LLM-driven transition) route through this single helper. | `test_inv13_*` in `test_lifecycle_invariants.py` (4 tests pin the helper contracts on the success path: resolved-ack offers runbook only with no regen; resolved Q&A offers both regen + runbook; closed Q&A offers regen only when substance gate passes, never runbook; ack ⊂ Q&A by construction) + `test_runbook_completion_and_summary_failure.py::TestAckTurnFollowUpsOnFailure` (4 tests pin the failure-path branch of `_select_ack_follow_ups`) |
| INV-14 | Manual case-action requests (status dropdown) flow through the same confirmation pattern as natural progression — they cannot bypass the User-Agent Handshake. | §1.5 *Core Principle* — "all case actions require explicit user confirmation" | **Structural** — the UI sends a system message that routes through `submit_turn` + the standard `pending_transition` mechanism. *Composition seam:* dropdown-driven dispositions inherit the same confirmation-pair UX seam as INV-03 — the dropdown's system message (from `CASE_ACTION_MESSAGES` in `case_action_manager.py`) is a fixed string, but the LLM's `agent_response` is expected to surface a "please confirm" cue per `_AMBIGUITY_FIRST_RULE` alongside the engine-emitted override suggestions. Audit the same surfaces as INV-03. | `test_inv14_*` in `test_lifecycle_invariants.py` (4 tests pin that dropdown-initiated INQUIRY→CLOSED / INVESTIGATING→CLOSED / INVESTIGATING→RESOLVED writes pending_transition and leaves `case.state` unchanged this turn); cross-references: `test_transition_alignment.py::test_ui_dropdown_*` (canonical confirmation-pair emission) and `test_investigation_lifecycle.py::test_explicit_ui_resolve_proposes_then_confirms` (two-turn end-to-end) |
| INV-15 | The agent is an **ADVISOR** — it never runs commands, accesses systems, or makes infrastructure changes. Stated in the agent's vocabulary constraint (banned/required phrase table). | §1.6 *Agent Role Constraints* | **Prompt-only** + light vocabulary check. The runtime scan (`_completion_phrases` at `milestone_engine.py:3186`, consumed at `:3246`) is deliberately narrow — it covers the highest-stakes drift (false completion claims like *"case closed"*, *"marking as resolved"*) where the signal/noise ratio is strongest. Action-claim phrases from `_ADVISOR_ROLE_CONSTRAINT` (*"Let me check"*, *"I will run"*) are not scanned — they have higher false-positive rates in legitimate context (quoting the user, hedge phrases). Broader advisor-role drift detection is deferred to a separate `advisor_role_compliance` telemetry signal if/when operational evidence warrants it. | `test_inv15_*` in `test_lifecycle_invariants.py` |
| INV-16 | The LLM is the **sole authority** for milestone advancement. `validate_milestone_claims` in the evidence processor is read-only — it returns validation results but never writes to `case.progress.*`. Milestone state changes flow ONLY from the LLM's structured output through the milestone engine. | §3.1 *Issue A* (keyword-discovery dual pathway removed) | **Code-guarded by construction** — `validate_milestone_claims` returns `List[MilestoneValidationResult]` and contains no `case.progress.<field> =` assignments. Static-source guard pins this so any future regression (a refactor reintroducing a write inside the evidence processor) breaks the test. | `test_inv16_*` in `test_lifecycle_invariants.py` |
| INV-18 | Runbook generation is RESOLVED-only. CLOSED cases are not eligible regardless of `closure_reason`. Both the chat-side dispatcher and the API endpoint reject non-RESOLVED cases. | §4.5.1 — *"Eligibility: RESOLVED cases only."* | **Code-guarded at two layers** — (1) Engine: `_process_terminal_turn` gates dispatch on `case.state == CaseState.RESOLVED`; non-RESOLVED falls through to terminal Q&A. (2) API: `POST /knowledge/convert-from-case` returns HTTP 400 when `case_status != "resolved"` (`conversion_routes.py:511`). | `test_inv18_*` in `test_lifecycle_invariants.py` |
| INV-22 | **`cause_state` is an engine-derived truth signal that is NEVER path/state-stripped.** It records *what the engine knows about the cause* — recomputed every INVESTIGATING turn from the LLM's grounded cause signal plus the active-hypothesis count, independent of stage or any (now-removed) path. There is no window in which a legitimately-known cause is suppressed; this is the linchpin that dissolved the old self-naming-error trap (a case that identified its cause at turn 5 but stayed UNRESOLVED because the path forbade RCA-side emissions). | §2; [investigation-flow-redesign.md §4.1, R1](./investigation-flow-redesign.md) | **Code-guarded** — `_recompute_assessment_state(case)` in `milestone_engine.py` is called at the end of `_apply_investigation_updates` on every turn and sets `cause_state` from `HypothesisManager.count_active_hypotheses` + the grounded signal. The reasoning-validation strip (INV-23) operates on emitted milestones, never on the recomputed assessment enum. The justification bar accepts a self-naming-error extract as a valid basis for IDENTIFIED. | `tests/unit/core/investigation/test_surgical_strip_and_cause_state.py` (cause_state derivation across 0/1/≥2 hypotheses + grounded signal; self-naming-error acceptance) |
| INV-23 | **The reasoning-validation strip is per-milestone surgical.** When `validate_reasoning_first` rejects an emission for missing justification, only the *offending* milestones are reset to None — co-emitted valid milestones survive. A single unjustified milestone never wipes the others. (Global failures — no `internal_reasoning`, no actionable evidence — still implicate every completed milestone; turn-reference format errors implicate none.) | §2; [investigation-flow-redesign.md §5](./investigation-flow-redesign.md) | **Code-guarded** — `validate_reasoning_first` returns the *set* of offending milestone names; the apply-path caller in `milestone_engine.py` strips only that set. Replaces the prior wholesale-wipe degradation branch that destroyed valid `mitigation_accepted` / `mitigation_verified` alongside one forbidden emission (the S1 collateral-wipe bug). | `tests/unit/core/investigation/test_surgical_strip_and_cause_state.py` (surgical strip preserves co-emitted valid gates) |
| INV-24 | **The stabilization record is a single forward-only insert.** There is one `progress.stabilization: StabilizationRecord` per investigation; its own validator enforces `verified ⇒ accepted`. Acceptance/verification are never reset (no MITIGATION re-entry). `completed_at_turn` is stamped once when `verified` first flips True (the boundary for up-weighting pre-stabilization evidence in later RCA). A non-stabilizing insert is not a dead-end — the flow stays open to user-led action (alternative fix in prose, escalate, new data, or CLOSE). | §2; [investigation-flow-redesign.md §3.2.1, R2](./investigation-flow-redesign.md) | **Schema** — `StabilizationRecord._verified_requires_accepted` model_validator. **Code-guarded** — the engine materializes the record from the LLM gate signals and stamps `completed_at_turn` idempotently; `solution_ordering` on `InvestigationProgress` keeps `solution_verified ⇒ solution_accepted`. | `test_inv05_*` / stabilization-lifecycle tests in `test_lifecycle_invariants.py`; `StabilizationRecord` `verified ⇒ accepted` validator tests in `tests/unit/core/investigation/test_milestone_order_rejection.py` |

**Drift notes (as of this writing):**

- **INV-01** historical drift: the design previously described a mechanical regex fallback (`user_confirms()`) inside `_apply_inquiry_updates` as the second confirmation path. That fallback was deliberately removed in commit `06cfa834` (2026-03-17) in favor of intent-routing for explicit clicks. The pseudocode at the top of §1.2 still references the old fallback and should be updated separately. The same-turn-confirmation guard documented in INV-01 was added in `13ff2eae` after the gap was observed in production.
- **INV-01** deferral-without-recovery gap *(resolved)*: when commit `13ff2eae` added the same-turn-confirmation guard, its commit message stated *"the transition is deferred to the next turn (where the LLM re-presents the statement and the user confirms explicitly)."* That deferral assumption was never code-backed. The recovery turn's context still carried the `NOT_YET_CONFIRMED` block from `16bb0912` (2026-04-23), which actively instructs the LLM *"Do NOT re-propose the same statement."* When the guard fired, the case stalled silently — statement persisted, no LLM re-present, no clickable affordance. Observed on case `case_bb917dcd5bb2` (turn 14 "Yes, let's investigate this"): guard fired, user moved on to other questions, case never transitioned. Fix: `case.inquiry.handshake_deferred_at_turn` flag set at the guard site; `context_builder` switches to `HANDSHAKE_DEFERRED` on the recovery turn; engine deterministically emits the canonical confirmation suggestions. The matrix row above now lists both Code-guarded layers (deferral + recovery affordance) so future audits surface the prompt-code dependency before a similar regression slips through.
- **INV-04** matrix-text drift *(resolved)*: the INV-04 row now names the real symbols (`ALLOWED_ACTIONS` + `is_valid_action()`), and §1.3's pseudocode block now uses `ALLOWED_ACTIONS` instead of the non-existent `VALID_TRANSITIONS`. The duplication-risk note below is the remaining open item — graph still lives in three places, gated by `test_inv04_valid_action_graphs_agree_across_definitions`.
- **INV-04** duplication risk: the valid-action graph is duplicated across **three** locations — `ALLOWED_ACTIONS` (case_action_manager.py), `valid_actions` (inside `is_valid_action()` in models.py), and implicit in the `_execute_*_transition` runtime preconditions in `terminal_transitions.py`. They currently agree, but no single source of truth means a future single-sided edit would let the forbidden edge slip through one enforcement surface while the others still reject it. `test_inv04_valid_action_graphs_agree_across_definitions` is the consistency guard until consolidation; the cleanup itself is separate work.
- **INV-04** dead code *(resolved)*: `CaseActionManager.validate_action` (and its `validate_transition` alias) had zero production callers and was deleted. `ALLOWED_ACTIONS` is now used only by the UI adapter for `get_allowed_transitions` (dropdown affordance), which was always its only real purpose.
- **INV-04** doc-doc contradiction *(resolved)*: `agent-stage-playbook.md` previously declared an `INQUIRY → TERMINAL (RESOLVED)` "Fast-track: KB match" edge in three places (§1 transition table, phase diagram, and INQUIRY-stage Gate Conditions table). The edge does not exist — INV-04's Pydantic validator + `_execute_resolved_transition`'s precondition reject it at construction time. The playbook was describing the **UX appearance** ("inquiry resolved fast") while the code implements INV-06's same-turn KB-Resolution collapse (INQUIRY → DIAGNOSIS → RESOLVED, one turn, but still passing through INVESTIGATING). Doc-behind-code drift; playbook updated to remove the fast-path with explanatory call-outs pointing readers at the canonical spec here. Surfaced by a separate design-check that streamed the finding but did not save it to the audit report — also a reminder that the saved report is not always a complete record of the streamed audit.
- **INV-05** (stage gates) intentionally relies on prompt-only enforcement. The blast radius is bounded — stage transitions within INVESTIGATING don't change disposition or commit anything irreversible. A premature gate at most miscategorizes the current investigation phase; it cannot leak a case into a terminal state. Documented here so the asymmetry with INV-03 is deliberate, not accidental.
- **INV-06** *(resolved)*: the prior drift between §1.2's claim of a same-turn user-side collapse and the engine's `transition_proposed_this_turn` guard has been closed. The engine now fires `confirm_pending_transition` in the same turn ONLY when both `transition_proposed_this_turn` and `knowledge_resolution_signalled` are set in the turn's metadata — the well-scoped KB-resolution path. Every other ProposedTransition emission still follows the standard 2-turn handshake. As a side effect, `metadata["knowledge_resolution_signalled"]` (previously dead) is now load-bearing as the conjunction gate. Decision history: Option B from the cluster-2 audit follow-up; recency of design (2026-05-11) over code (2026-02-12) + recoverability of the code change (no edge-case dead end) drove the choice to update code rather than weaken the design.
- **INV-07** schema permissiveness *(decided — keep current)*: `InquiryStateUpdate` uses Pydantic's default `extra='ignore'`. An LLM emitting `evidence_to_add` (or any other invalid field) in an INQUIRY response has the field silently dropped, not rejected. The invariant holds (no Evidence row created — the engine has no creation branch either). Switching to `extra='forbid'` was considered and rejected: the invariant is enforced by **field absence**, not by rejecting strays; switching to `forbid` would convert benign LLM drift into user-facing turn failures (the `_generate_structured_output` retry chain might recover, but each occurrence costs latency + cost or surfaces as an error) with no improvement to the invariant. If LLM drift becomes a concern, add a separate passive observability signal (e.g., log unknown fields at the structured-output parse step) — not a Pydantic config tightening.
- **INV-09** terminology drift *(resolved)*: the INV-09 row now describes the mechanism accurately as "`require_case_not_terminal()` helper function (used at the case-update endpoint; other write endpoints inline the `case.is_terminal` check)". A future consolidation pass via a FastAPI dependency-injection layer remains deferred — the protection is consistent in intent across endpoints today, only the mechanism varies.
- **INV-14** *(resolved)*: previously two drift notes lived here — (a) §1.5.2 still described the deprecated plain-text `/queries` mechanism, and (b) §1.5.2 didn't surface the RESOLVED dropdown's three-branch behavior (`READY` / `SUGGEST_CLOSE` / `NEEDS_INFO`). Both gaps are now closed: §1.5.2 Step 2 describes the structured-intent payload (`intent_type="status_transition"` + `intent_data`) and Step 3 carries the three-branch table.
- **INV-15** (advisor role) is acceptable as prompt-only because the worst-case symptom is the agent suggesting it ran a command (it didn't — there's no execution surface in the codebase to do so). The constraint is stylistic; violations are caught in compliance review, not at runtime.
- **INV-15** scan scope *(decided — keep narrow, defer broader signal)*: the runtime `_completion_phrases` scan (defined at `milestone_engine.py:3186`, consumed at `:3246`) covers the high-stakes transition-completion phrases (*"case closed"*, *"marking as resolved"*) where the signal/noise ratio is strongest. The broader `_ADVISOR_ROLE_CONSTRAINT` banned-phrase list (*"Let me check"*, *"I will run"*, *"Let me look at"*, *"I'll execute"*) is deliberately NOT in the runtime scan — these phrases are generic enough to appear in benign context (the agent quoting the user, hedge phrasing like *"Let me look at this from your angle..."*) where false positives would dilute the telemetry signal. If broader advisor-role drift detection becomes valuable, add a separately-tagged `advisor_role_compliance` signal alongside the existing `transition_compliance` scan — distinct log key so analytics can pivot on each independently. Deferred until operational evidence warrants it.
- **INV-17 / INV-19 / INV-20 / INV-21 retired (2026-06-05, flow redesign):** these rows enforced the path fork and its emission bans — INV-17 (hypothesis-before-causal-evidence, enforced across the path-conditional dispatch), INV-19 (Gate 2 path-selection commit), INV-20 (already removed), and INV-21 (Gate 3 post-mitigation RCA guard). The unified opportunistic flow removes `InvestigationPath` / `PathSelection`, Gate 2, Gate 3, and the three path-conditional emission bans entirely. **R6 tier shift (deliberate):** retiring INV-17 and INV-21 drops two *structural/code* guards. They are replaced by (a) the `cause_state` gate — run the diagnostic machinery iff `cause_state ∈ {UNKNOWN, CANDIDATES}` (a prompt-guided rule, no longer a hard engine ban on hypothesis/causal-evidence emission), (b) the per-milestone surgical strip (INV-23), and (c) the coupled prompt mandate that forces hypothesis emission under uncertainty (the producer of derived `CANDIDATES`). The self-naming simulation campaign is the safety net for this tier change. The hypothesis-before-causal-evidence ordering survives as prompt guidance in the unified INVESTIGATION block, not as an engine reject-and-resurface backstop.

**How to use this matrix:**

1. When a PR touches any code referenced under "Enforcement", verify the corresponding row's classification still holds — a row that moves to a weaker category is a deliberate design change requiring a doc update.
2. When writing tests, prefer pinning invariants over pinning behaviors. A test named after an INV-XX is robust against incidental refactors; a test named after the function under test is not.
3. When the design evolves, add or remove rows. The matrix is not a frozen artifact — but each change should be visible in a single doc diff.
4. When introducing a new cross-tier dependency (one tier's enforcement cooperates with another tier's behavior to achieve the design goal), add a `*Composition seam:*` annotation to the row's Enforcement column. The annotation names the dependency, states what removing/weakening one side would do (distinguishing invariant violation from design-goal degradation), and points to the symbols future audits should verify together. See INV-01, INV-03, INV-06, INV-14 for examples. Absence of the annotation on a row means the invariant was reviewed and found to be single-tier or to have intra-tier concerns captured in the drift notes.

### 1.4 Automatic Milestone Tracking and Stage Transitions

Stage transitions within INVESTIGATING (e.g., DIAGNOSIS → MITIGATION) are triggered automatically when the LLM sets the corresponding gate milestone. Disposition actions (RESOLVED, CLOSED) are NEVER automatic — they always require an explicit User-Agent Handshake (see §1.2).

```python
async def process_turn(case: Case, user_message: str) -> str:
    """
    Process one turn and update milestones.

    AUTOMATIC TRANSITIONS:
    - Checked AFTER agent response generation
    - Triggered by milestone completion (data-driven)
    - Disposition actions are irreversible
    """

    # Validate not terminal
    if case.is_terminal:
        return "Case is closed. No further updates allowed."

    # Capture state before
    progress_before = case.progress.dict()

    # Agent analyzes available data and completes tasks
    agent_response = await agent.process(case, user_message)

    # Capture state after
    progress_after = case.progress.dict()

    # Detect completed milestones
    milestones_completed = detect_milestone_completions(progress_before, progress_after)

    # Record turn
    record_turn(case, milestones_completed)

    # ============================================================
    # DISPOSITION CASE ACTION HANDLING (User-Agent Handshake)
    # ============================================================
    # Disposition case actions are NEVER automatic. The agent proposes
    # a transition via ProposedTransition, and the system holds it
    # pending until the user confirms in the next turn.

    # 1. Handle pending transition confirmation from previous turn
    #    Two detection paths (checked in order):
    #    a. Intent-based: COOPERATIVE suggestion clicks carry
    #       intent_type="confirmation" + confirmation_value (deterministic)
    #    b. Pattern-based: fallback for users who type instead of clicking
    if case.pending_transition:
        intent_confirms = (intent_type == "confirmation" and intent_data.get("value") is True)
        intent_declines = (intent_type == "confirmation" and intent_data.get("value") is False)
        if intent_confirms or user_confirms_transition(user_message):
            confirm_pending_transition(case, case.user_id)
        elif intent_declines or user_declines_transition(user_message):
            cancel_pending_transition(case)

    # 2. Handle ProposedTransition from LLM response
    proposed = getattr(response.state_updates, "proposed_transition", None)
    if proposed:
        propose_transition(
            case=case,
            to_status=proposed.to_status,
            reason=proposed.reason,
            summary=proposed.summary,
            evidence_ids=proposed.evidence_ids,
        )

    return agent_response


# Disposition case actions (all require user confirmation):
#
# INVESTIGATING → RESOLVED:
#   - Trigger: Agent proposes via ProposedTransition + user confirms
#   - Automatic: No (requires User-Agent Handshake)
#   - Disposition: Yes (irreversible)
#   - KB-resolution variant: same edge, milestone state populated in one
#     turn from the matched runbook Cause (see §1.2 INVESTIGATING → RESOLVED).
#
# INVESTIGATING → CLOSED:
#   - Trigger: User explicit action (force_close via UI or chat)
#   - Automatic: No (requires user intent)
#   - Disposition: Yes (irreversible)
#
# INQUIRY → CLOSED:
#   - Trigger: User explicit action (close_from_inquiry)
#   - Disposition: Yes (irreversible)
#
# (INQUIRY → RESOLVED is not a valid edge — KB-matched cases route through
#  INVESTIGATING; see ALLOWED_ACTIONS in §1.3.)


# ============================================================
# EXPLICIT USER-TRIGGERED TRANSITIONS (Non-Automatic)
# ============================================================

def force_close_investigation(case: Case, user_id: str, reason: str):
    """
    User explicitly abandons investigation without solution.

    Trigger: User action (not automatic)
    Disposition: Yes (irreversible)
    """
    if case.state != CaseState.INVESTIGATING:
        raise ValueError("Can only force-close from INVESTIGATING status")

    case.atomic_update(
        status=CaseState.CLOSED,
        closed_at=datetime.now(UTC),
        closure_reason=reason,  # engine-derived: "closed_after_investigation"
    )
    # Note: a case stabilized then closed is simply "closed_after_investigation"
    # (the former "mitigation_sufficient" reason was folded in). The documented
    # stabilization is preserved on the closed case.
    case.action_history.append(CaseAction(
        from_status=CaseState.INVESTIGATING,
        to_status=CaseState.CLOSED,
        triggered_at=datetime.now(UTC),
        triggered_by=user_id,
        reason=f"User force-closed: {reason}"
    ))
    # Caller invokes synchronous summary generation after this transition
    # (gated by should_generate_terminal_summary). See §1.7.3.
    # DISPOSITION - no further case actions


def close_from_inquiry(case: Case, user_id: str):
    """
    Close after inquiry without formal investigation.

    Trigger: User action (not automatic)
    Disposition: Yes (irreversible)
    """
    if case.state != CaseState.INQUIRY:
        raise ValueError("Can only close-from-inquiry when in INQUIRY status")

    case.atomic_update(
        status=CaseState.CLOSED,
        closed_at=datetime.now(UTC),
        closure_reason="inquiry_only",
    )
    case.action_history.append(CaseAction(
        from_status=CaseState.INQUIRY,
        to_status=CaseState.CLOSED,
        triggered_at=datetime.now(UTC),
        triggered_by=user_id,
        reason="User closed after inquiry only"
    ))
    # Caller invokes synchronous summary generation after this transition
    # (gated by should_generate_terminal_summary). See §1.7.3.
    # DISPOSITION - no further case actions
```

#### 1.4.1 State Update Timing

State updates occur at specific points within a turn to ensure consistency:

| Update Type | Category | When | Trigger |
|-------------|----------|------|---------|
| `proposed_problem_statement` | — | During INQUIRY turn | LLM generates from conversation |
| `problem_statement_confirmed` | — | After user confirmation | User says "Yes" or equivalent |
| `symptom_verified` | Progress indicator | After evidence processing | LLM sets in structured output when symptoms confirmed |
| `cause_state` | Assessment (engine-derived) | End of each INVESTIGATING turn | Engine recomputes via `_recompute_assessment_state`: IDENTIFIED if the LLM's grounded cause signal passes justification; else CANDIDATES if ≥2 ACTIVE hypotheses; else UNKNOWN. Replaces the boolean `root_cause_identified`. Never path-stripped. |
| `solution_state` / `solution_feasible` | Assessment (engine-derived / LLM-settable) | End of each INVESTIGATING turn / LLM output | `solution_state=SELECTED` once a permanent SOLUTION is proposed; `solution_feasible` defaults NOW, LLM sets DEFERRED. |
| `solution_proposed` | Progress indicator | After LLM proposes action | Set when ProposedAction with action_type=SOLUTION is created |
| `mitigation_accepted` | Stabilization gate signal | LLM structured output | User acknowledges executing the proposed stabilization → materializes `stabilization.accepted` |
| `mitigation_verified` | Stabilization gate signal | LLM structured output | User confirms the stabilization stabilized the situation → materializes `stabilization.verified` + `completed_at_turn` |
| `solution_accepted` | Gate milestone | LLM structured output | User acknowledges executing proposed solution |
| `solution_verified` | Gate milestone | After user confirms fix | User confirms problem resolved (User-Agent Handshake) |
| Disposition action | — | End of turn | After all other processing |

**Gate signals vs Progress indicators vs Assessment variables**:

- **Gate signals** (`mitigation_accepted`, `mitigation_verified`, `solution_accepted`, `solution_verified`): Drive the derived stage label + resolution handshake. Set by the LLM in structured output when it detects user compliance with a ProposedAction (Framework §4.1). The stabilization pair (`mitigation_accepted`/`mitigation_verified`) materializes into the single `progress.stabilization` record rather than booleans.
- **Progress indicators** (`symptom_verified`, `solution_proposed`): Provide LLM context and analytics. Do NOT drive stage transitions.
- **Assessment variables** (`cause_state`, `solution_state`, `solution_feasible`): Engine-derived truth signals recomputed each turn. `cause_state` (not a gate, not a path) is what drives whether the diagnostic machinery runs. Never path-stripped.

**Order of Operations Within a Turn**:

1. **Receive user message**
2. **LLM processes** and generates response + `state_updates`
3. **Apply state updates**: progress milestones, gate milestones, evidence, hypotheses (all from LLM structured output)
4. **Gate milestone side effects**: When a gate milestone is set, mark the corresponding ProposedAction as accepted; stage transition takes effect next turn
5. **Record turn progress** (detect what changed)
6. **Check disposition actions** (RESOLVED/CLOSED) if conditions met
7. **Return response to user**

**Rationale**: Disposition actions happen last to ensure all state is consistent before case becomes immutable. Gate milestones are applied from the LLM's structured output alongside progress milestones; the new stage's prompt takes effect on the next turn.

### 1.5 Manual Case Action Requests

**Purpose**: Allow users to manually request case actions for practical scenarios (urgent issues, external resolutions, etc.)

**Core Principle**: Manual case actions follow the same confirmation pattern as natural progression - **all case actions require explicit user confirmation**.

---

#### 1.5.1 UI Component: Case Action Dropdown

**Location**: Case header (collapsed view)

**Behavior**:
- Shows current status with dropdown indicator
- Displays only **forward transitions** (case actions are irreversible)
- Dispositions (RESOLVED, CLOSED) have dropdown disabled

**Available Options by Status**:

| Current Status | Dropdown Options |
|---------------|------------------|
| INQUIRY       | Investigating, Closed |
| INVESTIGATING | Resolved, Closed |
| RESOLVED      | *(disabled - disposition)* |
| CLOSED        | *(disabled - disposition)* |

**API Support**: No direct API - uses existing query submission endpoint

---

#### 1.5.2 Request Flow

**Step 1: User Initiates Request**

User selects new status from dropdown → Frontend shows confirmation modal:

```
⚠️ Request Case Action

This will ask the agent to transition the case to [NEW_STATUS].

Are you sure you want to proceed?

[Cancel]  [Continue]
```

**API Call**: None yet - just frontend modal

---

**Step 2: Submit Request via Chat (Structured Intent)**

User confirms modal → Frontend sends a turn submission carrying a structured intent payload (NOT plain text):

```typescript
POST /api/v1/cases/{case_id}/turns
Body (multipart/form-data):
  query: ""                          // empty — intent is in the structured fields
  intent_type: "status_transition"
  intent_data: '{
    "from_status": "inquiry",
    "to_status": "investigating",
    "user_confirmed": true
  }'
```

**API Endpoint**: `POST /api/v1/cases/{case_id}/turns`
- **Purpose**: Submit a turn — query text, attachments, and/or structured intent.
- **Auth**: Requires Bearer token + X-Session-Id.
- **Returns**: `TurnResponse` with the agent's reply.

The structured-intent route was introduced in the 2026-02-09 bug fix
([milestone_engine.py:1714](../../../faultmaven/core/investigation/milestone_engine.py#L1714))
when `intent_type="status_transition"` was added as an explicit dispatch path. Earlier
versions used a plain-text system-generated message ("[User requested to change case
status to X]") submitted to `/queries`. That mechanism is deprecated for dropdown flows;
the structured payload is unambiguous and skips the LLM's intent classification.

---

##### Step 3: Engine Validates and Responds

The engine's `status_transition` handler (in `_process_turn_impl`) branches by target
status. Each branch honors the User-Agent Handshake — none of them auto-execute.

**→ INVESTIGATING (from INQUIRY)**: falls through to the normal INQUIRY LLM pipeline.
The LLM presents the existing problem statement for confirmation. When the LLM sets
`user_confirmed_investigation=True` on a subsequent turn (gated by the same-turn
guard — the statement must have been presented on a prior turn), the transition
fires via `_check_automatic_transitions`.

**→ CLOSED (from INQUIRY or INVESTIGATING)**: the engine calls `propose_transition`
directly, returns a closure-readiness summary plus the canonical Yes/No confirmation
pair. The transition fires only when the user confirms on the next turn.

**→ RESOLVED (from INVESTIGATING)**: branches three ways based on
`assess_resolution_readiness(case)`:

| Verdict | Engine action |
|---|---|
| `READY` (root cause + actionable solution captured) | `propose_transition("resolved")`; returns Yes/No confirmation pair. User confirms on next turn. |
| `SUGGEST_CLOSE` (case is thin — no root cause / solution) | Pivots to `propose_transition("closed")`. The dropdown said *"mark resolved"* but the engine recognizes the case has nothing to mark as resolved; the user is offered close instead, with a readiness-message explaining why. |
| `NEEDS_INFO` (partial state — some criteria met, some not) | `propose_transition("resolved")` with `needs_info=True` flag set on the pending transition. The agent asks the user for the missing piece (root cause OR solution detail). The next turn re-evaluates readiness once the user replies. |

Whichever branch fires, the case stays in INVESTIGATING this turn and the transition
proposal is held pending. The user has the next-turn confirmation step to accept,
decline, or refine.

---

**Step 4: User Confirms (3 Options)**

**Option A: Click [✅ Yes]**
- Frontend sends system-generated message: `"Yes"`
- Agent immediately transitions status
- Agent responds with acknowledgment

**Option B: Click [❌ No]**
- Frontend sends system-generated message: `"No"`
- Agent cancels request, stays in current status
- Agent asks what user wants to do next

**Option C: Type qualified answer**
- User types: "Not 30%, more like 50%, and started 3 hours ago"
- Agent refines understanding
- Agent presents confirmation question again with updated context

**API Call for all options**:
```typescript
POST /api/v1/cases/{case_id}/queries
Body: {
  "message": "Yes"  // or "No" or user's typed message
}
```

---

**Step 5: Agent Executes Transition**

If user confirmed (Option A or refined via Option C), agent:

1. **Sets status** to new value
2. **Initializes required state** (e.g., creates `ProblemVerification` for INVESTIGATING)
3. **Records case action** in `action_history`
4. **Responds with acknowledgment** and next steps

**Example response** (INQUIRY → INVESTIGATING):

```
"Understood. Transitioning to formal investigation now.

Based on our discussion, the problem is:
'Database queries timing out in production, affecting 50% of requests
since 3 hours ago'

Let me start by verifying the scope and impact. What services are affected?"
```

**Backend updates**:
- `case.state = CaseState.INVESTIGATING`
- `case.problem_verification = ProblemVerification(symptom_statement=...)`
- `case.action_history.append(CaseAction(...))`

---

#### 1.5.3 Confirmation UI Pattern

**Visual Design** (in chat conversation):

```
┌─────────────────────────────────────────────────┐
│ Agent:                                   2:45 PM│
│                                                 │
│ You've requested to move to investigation.      │
│                                                 │
│ Based on our conversation, the problem is:      │
│ "Database queries timing out in production,     │
│ affecting 30% of requests"                      │
│                                                 │
│ Is this what you want me to investigate?        │
│                                                 │
│ ┌─────────┐  ┌─────────┐                       │
│ │ ✅ Yes  │  │ ❌ No   │                       │
│ └─────────┘  └─────────┘                       │
│                                                 │
│ 💡 Tip: Click a button or type to clarify      │
└─────────────────────────────────────────────────┘
```

**Confirmation actions are rendered as COOPERATIVE suggestions** with `intent` metadata:

```python
# Resolution confirmation suggestions carry intent for deterministic routing
{
    "label": "Yes, mark as resolved",
    "action_type": "COOPERATIVE",
    "cooperative_action": "query_submit",
    "payload": "Yes, the issue is resolved. Please mark this case as resolved.",
    "intent": {"type": "confirmation", "confirmation_value": True},
}
```

**Click flow**: Frontend sends `payload` as query text AND `intent` as `QueryIntent` metadata.
This routes through `IntentType.CONFIRMATION` → deterministic `pending_transition` handling,
bypassing the tool loop and pattern matching entirely.

**Typed responses** (user types instead of clicking) fall back to `_user_confirms_transition()`
pattern matching with a 100-char length guard.

---

#### 1.5.4 Case Action Confirmation Examples

**INQUIRY → INVESTIGATING**

```python
# Agent validation
if not case.inquiry.proposed_problem_statement:
    # Missing problem - ask first
    return "Before we can investigate, what problem are we trying to solve?"
else:
    # Present confirmation
    return f"""You've requested to move to investigation.

    The problem is: {case.inquiry.proposed_problem_statement}

    Is this what you want me to investigate?

    [✅ Yes]  [❌ No]"""
```

**INVESTIGATING → RESOLVED**

Before presenting the confirmation, the system runs `assess_resolution_readiness(case)` which checks for root cause + solution. Three outcomes:

- **READY** — Root cause and solution present. System shows what's on record and asks user to confirm.
- **NEEDS_INFO** — Partially ready (e.g., root cause but no solution). System asks user to provide the missing piece.
- **SUGGEST_CLOSE** — No root cause, no solution, no evidence. Both the UI-dropdown branch and the LLM-emit branch pivot the pending proposal to CLOSED and emit the close confirmation pair. If the issue was actually fixed, the user can provide root cause and solution to reopen the resolve path.

```python
readiness = assess_resolution_readiness(case)

if readiness.verdict == "suggest_close":
    # Pivot: propose CLOSED instead of RESOLVED, emit close confirmation pair
    propose_transition(case=case, to_status="closed", summary=readiness.message)
    return readiness.message

if readiness.verdict == "needs_info":
    return readiness.message  # Asks for missing root cause or solution

# READY — show what's on record and ask for confirmation
return f"""You've indicated this issue is resolved.

Here's what I have on record:
- **Root cause**: {case.root_cause_conclusion.root_cause}
- **Solution**: {case.solutions[-1].title}

Is this correct? Once you confirm, I'll mark the case as resolved.

What will happen:
- This is irreversible — the case becomes read-only
- No further evidence submission or investigation will be possible
- You can still ask questions about this case
- Archive the case from Dashboard when you are done

[✅ Yes, mark as resolved]  [❌ No, continue investigating]"""
```

**INVESTIGATING → CLOSED**

Before presenting the confirmation, the system runs `assess_closure_readiness(case)` which checks for resolution-grade content. Three outcomes:

- **SUGGEST_RESOLVE** — Case has root cause + solution on record (resolution-grade). Both the UI-dropdown branch and the LLM-emit branch pivot the pending proposal to RESOLVED and emit the resolve confirmation pair. Closing would discard resolution attribution; the engine reconciles intent against case content. Symmetric to `ResolutionReadiness.SUGGEST_CLOSE`.
- **HAS_SUBSTANCE** — Case has investigation work (evidence / hypotheses / partial findings) but is missing one of root cause / solution. Closing is the right disposition; the engine surfaces a summary of what was accomplished.
- **TRIVIAL** — Case has no investigation data. The engine confirms close with a minimal-data warning.

```python
closure = assess_closure_readiness(case)

if closure.verdict == "suggest_resolve":
    # Pivot: propose RESOLVED instead of CLOSED, emit resolve confirmation pair
    propose_transition(case=case, to_status="resolved", summary=closure.message)
    return closure.message  # "Case qualifies for resolved — mark resolved instead?"

# HAS_SUBSTANCE / TRIVIAL — propose CLOSED with the summary message
propose_transition(case=case, to_status="closed", summary=closure.message)
```

```python
# Agent confirms closure with consequences
return f"""You've requested to close this case without resolution.

Problem: {case.problem_verification.symptom_statement}
Current findings: {case.working_conclusion.summary if exists else "Limited data"}

Here's what will happen when I close this case:

- This is irreversible — the case becomes read-only
- No further evidence submission or investigation will be possible
- You can still ask questions about this case
- Archive the case from Dashboard when you are done

Please select a closure reason:

[Abandoned]  [Escalated]  [Mitigation Sufficient]  [Other]

Or type your reason."""
```

**INQUIRY → CLOSED**

```python
# Agent confirms inquiry-only closure with consequences
return f"""You've requested to close this case without investigation.

Here's what will happen:

- This is irreversible — the case becomes read-only
- The case will remain on your list until archived from the Dashboard

Close this case?

[✅ Yes, close]  [❌ No, keep open]"""
```

**Ambiguous "close this case" (NLP pattern)**

When a user types "close this case" during INVESTIGATING without specifying resolved or closed, the system asks for clarification. No `pending_transition` is set — we don't know the user's intent yet. Their next message routes through the standard pattern matching (resolve_patterns or abandonment_patterns).

```python
# No pending_transition set — just ask for clarification
return """You'd like to close this case. Before I do, I need to know:

- **Resolved** — The problem is fixed. I'll document the solution.
- **Closed** — The investigation is ending without a solution
  (abandoned, escalated, or mitigation was sufficient).

Which would you like?"""
```

---

#### 1.5.5 API Summary

All manual case actions use **existing endpoints** - no new APIs required:

| Action | Endpoint | Method | Body |
|--------|----------|--------|------|
| Submit case action request | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "[User requested to change case status to Investigating]"}` |
| User clicks Yes button | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "Yes"}` |
| User clicks No button | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "No"}` |
| User types qualified answer | `/api/v1/cases/{case_id}/queries` | POST | `{"message": "<user's typed message>"}` |

**All messages appear in conversation history** - full audit trail maintained.

---

#### 1.5.6 Design Rationale

**Why dropdown menu instead of pure chat?**
- **Discoverability**: Users see available case actions
- **Clarity**: Visual indicator of current status + forward-only options
- **Efficiency**: One click vs composing message
- **Removes ambiguity**: "Let's investigate" could mean many things

**Why agent confirmation instead of direct case action?**
- **Consistency**: Same pattern as natural progression (all case actions require confirmation)
- **Safety**: Agent can validate prerequisites and catch mistakes
- **Context**: Agent ensures mutual understanding before transition
- **Audit**: Full conversation record of why the case action occurred

**Why buttons + typed fallback?**
- **Efficiency**: Most cases are simple yes/no
- **Flexibility**: User can elaborate when needed
- **Natural**: Matches existing confirmation pattern in natural progression

---

### 1.6 Agent Role Constraints

The agent is an **ADVISOR**, not an executor — it suggests, asks, recommends, and explains, but never runs commands, accesses systems, or makes infrastructure changes itself. This constraint is enforced as a behavioral rule with a vocabulary constraint (banned/required phrase table).

See **[Agent Behavioral Rules — Rule 3: Advisor Role](./agent-behavioral-rules.md#rule-3-advisor-role)** for the full banned/required phrase table, rationale, and prompt-injection text.

---

### 1.7 Post-Terminal Lifecycle

When a case reaches a disposition (RESOLVED or CLOSED), the investigation engine stops but the case remains interactive until archived. The post-terminal lifecycle defines two **interaction modes** — no new database fields required.

#### 1.7.1 Case Interaction Modes

```
┌─────────────┐   terminal    ┌──────────────┐   user        ┌──────────┐
│   ACTIVE    │──transition──►│   TERMINAL   │──archives───► │ ARCHIVED │──retention──► removed
│             │               │              │               │          │   expires
└─────────────┘               └──────────────┘               └──────────┘
 Evidence ✓                    Evidence ✗                      No interaction
 Milestones ✓                  Q&A over case data ✓            Not in default list
 Agent turns ✓                 View/download reports ✓         Reports: viewable if
 Full investigation            Regenerate summary ✓              unarchived
                               Knowledge extraction ✓
                                 (RESOLVED only)
```

**Derivation logic** (no new stored field):

```python
@property
def is_terminal(self) -> bool:
    """Case has reached a disposition (RESOLVED or CLOSED)."""
    return self.status in [CaseState.RESOLVED, CaseState.CLOSED]
```

#### 1.7.2 Terminal Mode

**Purpose**: Allow users to ask questions about the completed investigation, manage the summary report, and generate runbooks. The agent answers from existing case data only — no new investigation. The summary report can be regenerated at any time before archival.

**Behavior**:

- `_process_turn_impl()` short-circuits before intent detection and milestone processing
- Routes to **TERMINAL_TEMPLATE** prompt with `TerminalResponse` schema
- The template instructs the LLM: answer questions using existing case data, do not propose new actions or evidence requests
- Agent has read access to: messages, evidence, hypotheses, solutions, action_history, auto-generated summary
- Agent can NOT: accept new evidence, update milestones, propose transitions
- Agent CAN: explain what happened, clarify evidence, interpret timeline, extract lessons learned

**Three interaction scenarios**:

1. **User asks to regenerate the report** → Pattern matching triggers `_handle_report_regeneration`, which calls `ReportService.generate_reports` **synchronously** (single LLM call using SYNTHESIS capability) and embeds the rendered markdown inline in the chat reply. The substance gate is re-applied for CLOSED so low-substance cases can't be regenerated into existence by clicking around. Overwrites the existing Report row — there is always exactly one summary per case.
2. **User accepts runbook suggestion** (RESOLVED only) → Pattern matching triggers `_handle_runbook_creation()`: evaluates readiness + deduplication synchronously, then kicks off `ConversionService.convert_from_case()` as a **fire-and-forget background task**. The chat reply returns immediately ("Creating your runbook draft..."), and a `role="system"` completion message is appended to the case transcript when the background task finishes (success: names the new draft; failure: includes a retry hint). The Dashboard *Knowledge > Drafts* tab is the persistent surface.
3. **User asks questions about the case** → Agent answers via the LLM with TERMINAL_TEMPLATE.

**Implementation in milestone engine**:

```python
async def _process_turn_impl(self, case, user_message, ...):
    ...
    # 0a. Terminal case handling — Q&A and report regeneration only
    if case.is_terminal:
        return await self._process_terminal_turn(case, user_message, metadata)

    # Normal investigation flow...
```

**Report regeneration**: The summary report is auto-generated at closure time and rendered inline in the closure-turn chat reply. The COOPERATIVE *"Regenerate &lt;type&gt; summary"* affordance is the only chat-side path — free-typed paraphrases like *"give me a recap"* route to Q&A and never produce a persisted Report. Regeneration overwrites the existing report — there is always exactly one summary per case. Where the affordance appears depends on whether initial generation succeeded; see *Where it's offered* in §1.7.3 below.

**API-level enforcement** (`submit_turn` endpoint):

| Input                    | Terminal case behavior           |
| ------------------------ | -------------------------------- |
| Text query only          | Allowed — routed to terminal Q&A |
| Files or pasted content  | Rejected — 409 Conflict          |
| Status transition intent | Rejected — 409 Conflict          |

**Archived cases**: All interaction rejected with 409 Conflict. Archived cases are hidden from default list but remain accessible via "Include archived" filter.

#### 1.7.3 Auto-Generated Terminal Summary

When a case reaches a terminal state, the system synchronously generates a lightweight summary report. There is exactly **one summary per case**, persisted as a `Report` row and viewed through two surfaces: the chat (rendered inline on the closure-confirmation turn) and the Dashboard `ReportTab` (persistent view). Both surfaces show the same record.

**Two summary types**:

| Case Status | Report Type | Content Focus |
|-------------|-------------|---------------|
| RESOLVED | `RESOLUTION_SUMMARY` | What the problem was, root cause, solution applied, confirming evidence, timeline, milestones reached, whether a stabilization was inserted |
| CLOSED | `CLOSURE_SUMMARY` | What the problem was, investigation state at closure, approaches attempted, closure reason, leading hypotheses with confidence, mitigation status, recommendation for next investigator (if escalated) |

**Generation approach**:

- Single LLM call using SYNTHESIS capability (Fireworks/Groq for speed and cost).
- Input assembled via `context_builder.py`: case messages, evidence list, hypothesis states, action_history, milestone progress.
- Stored as `Report` with `auto_generated=True` (distinguishes from user-requested reports).
- **Synchronous**: the closure-turn agent reply waits for generation to complete and then embeds the rendered markdown inline. The state transition itself does not depend on LLM availability — generation exceptions are caught and the closure still commits, but the chat reply embeds a status-aware failure note (*"Resolution summary generation did not complete..."* / *"Closure summary generation did not complete..."*) and the regen affordance is offered **on the same ack-turn** for immediate retry. The "regen would be noise next to the inline summary" rationale only applies on the success path; on the failure path there is no inline summary, so offering regen alongside the failure note is the right UX. See *Where it's offered* below.
- One report per case — regeneration overwrites the existing row.

**Substance gate** (`should_generate_terminal_summary()` in `terminal_transitions.py`):

RESOLVED transitions always generate — a confirmed solution is meaningful content by definition. CLOSED transitions are gated on **investigation substance**: at least one of `evidence > 0`, `hypotheses > 0`, or `completed_milestones > 0`.

The gate is intentionally **substance-only**. Conversation depth (`message_count`) is *not* a signal: terminal Q&A turns inflate it, so including it would let post-closure chat flip the verdict. The three substance signals are naturally frozen in CLOSED state (the API rejects new evidence/transitions), so the gate is stable across the terminal lifetime without needing a snapshot field. The case description is also excluded — creation-time metadata, not investigation output.

**Pre-close confirmation prompt**: the confirmation prompt that asks *"are you sure?"* before a terminal transition speaks only to the irreversibility of closing — it does not mention the summary. Conditional promises ("a summary will be generated *if*…") would muddy the decision; the summary is a downstream Dashboard artifact and the only chat-side reference to it is the COOPERATIVE regen affordance offered after closure (when applicable).

**Skip-reason surfacing**: when a closed case fails the substance gate and has no Report row, `terminal_summary_skip_reason(case)` in `terminal_transitions.py` returns a human-readable note. The case UI adapter populates the Dashboard Report tab with `status="skipped"` and this derived note. The closure-turn chat reply also embeds the skip note inline so the user gets the explanation where they are.

**Regeneration**:

- **Where it's offered**:
  - *Success path*: regen is **not** on the closure-acknowledgment turn (the freshly-generated summary is rendered inline above; a regen card alongside it would be noise). It's offered on subsequent terminal Q&A turns via `_resolved_suggestions` (RESOLVED) or `_closed_suggestions` (CLOSED, when the substance gate would PASS).
  - *Failure path*: regen **is** offered on the closure-acknowledgment turn itself — synchronous generation raised an exception, no summary was rendered inline, so the "noise" rationale doesn't apply. `_select_ack_follow_ups` in `milestone_engine.py` selects the right set: minimal suggestions on success (`_resolved_ack_suggestions` / `[]`), full Q&A-turn suggestions on failure (`_resolved_suggestions` / `_closed_suggestions`). For CLOSED, failure implies the substance gate had already PASSED (otherwise generation would have been skipped, not attempted), so `_closed_suggestions` reliably returns the regen affordance.
- **Strict gating**: regeneration re-applies the same substance check. Low-substance closures can't be regenerated into existence by clicking around; the gate is one-way and consistent.
- **Free text routes to Q&A**: the regen handler is reached only via the COOPERATIVE suggestion's precomposed payload (exact-match). Free-typed paraphrases like *"give me a recap"* or *"new summary please"* route to terminal Q&A, where the prompt instructs the agent not to produce a competing summary and instead redirect to the existing summary + regen affordance. This keeps the rule clean: typing never produces a persisted Report side effect; clicking always does.

**Runbook generation — chat-side trigger + completion notification** (RESOLVED only):

The runbook affordance on the RESOLVED ack-turn is a separate downstream artifact, not a summary. Clicking it routes via the same exact-match dispatch (`_RUNBOOK_CREATION_PATTERNS` in `milestone_engine.py`) to `_handle_runbook_creation`, which runs two pre-flight gates synchronously: content readiness (`assess_runbook_readiness` — does the case have a root cause + actionable solution?) and deduplication against the runbook KB (similarity ≥ 0.85 → an existing runbook covers this; 0.70–0.85 → suggest with caveats; < 0.70 → proceed). On `NOT_READY` or `EXISTING_COVERS`, the chat reply explains why and no draft is created. On proceed, the conversion runs as a **fire-and-forget background task** (`_run_runbook_conversion`); the chat reply returns immediately ("Creating your runbook draft… I'll let you know here when it's ready"). When the background task finishes (success, no-drafts, or exception), it appends a `role="system"` message to `case.messages` with the outcome — *"Your runbook draft 'X' is ready"* on success or a retry-hint on failure. The append is concurrency-safe (acquires the per-case lock from `_case_locks`) and best-effort (notification-write failures are logged but never propagate). Both call sites for runbook generation — the chat-side dispatcher and the `POST /knowledge/convert-from-case` API endpoint — use the shared `CaseConversionRequest.from_case` factory for case-data extraction, so the case-to-runbook input shape is single-sourced. See `document-to-runbook-conversion.md` for the full conversion pipeline.

#### 1.7.4 Session Cleanup on Terminal Transition

When a case transitions to a terminal state, all active sessions are gracefully completed:

```python
# In terminal_transitions.py, after case state update:
active_sessions = await session_repo.get_active_sessions(case.case_id)
for session in active_sessions:
    session.complete(
        findings_summary=f"Case {case.state.value}: {closure_reason}"
    )
    await session_repo.update(session)
```

Uses the existing `InvestigationSession.complete()` method. No new session statuses needed.

---

## 2. Stabilization as an Insert

There is **one opportunistic INVESTIGATING flow** — no prospective path fork, no
merge. The former `mitigation_first` vs `root_cause` path selection (Gate 2), the
post-mitigation choice (Gate 3), and the urgency-based path recommender are all
**removed**. See [investigation-flow-redesign.md](./investigation-flow-redesign.md)
for the full design rationale; this section describes the resulting behavior.

### 2.1 Two orthogonal axes (why the fork was wrong)

The single fork conflated two independent questions:

- **Axis A — Certainty.** Do we know the cause? the solution? Tracked by the
  engine-derived assessment variables `cause_state` and `solution_state`. Drives
  whether diagnostic *labor* (hypothesis formulation, causal evidence-needs) is
  needed.
- **Axis B — Stabilization gap.** Is something hurting *now* that we cannot fully
  resolve this session? Drives whether a **stabilization** is inserted.

The two axes are independent. The old fork forced an Axis-B answer ("mitigate
first") that wrongly *implied* an Axis-A answer ("cause unknown, RCA deferred") —
which is what trapped self-naming-error cases (the cause is in the log; forbidding
the agent from recording it left the case permanently pre-mitigation).

**Diagnostic-machinery rule (replaces the entire path-conditional RCA ban):** run
hypothesis formulation + evidence-needs **iff the cause is uncertain**
(`cause_state ∈ {UNKNOWN, CANDIDATES}`) — *not* because a stabilization was or
wasn't inserted. When `cause_state == IDENTIFIED`, skip straight to solution work.
This rule is prompt-guided in the unified INVESTIGATION block; the engine no longer
hard-rejects hypothesis / causal-evidence emission by stage or path.

### 2.2 The unified flow

```text
INQUIRY ──confirm problem (Gate 1)──▶ INVESTIGATING ─────────────────▶ RESOLVED / CLOSED
                                  │
                                  │  opportunistically record what we learn:
                                  │   symptom_verified, cause_state, solution_state, solution_feasible
                                  │
                                  ├─(Axis-B gap detected, any turn)─▶ [STABILIZATION insert]
                                  │        propose → accept → verify → return to flow
                                  │
                                  └─(CLOSE available at ANY point: abandon, or data/impl. limit)
```

A **stabilization** (formerly "mitigation") is an *optional inserted sub-activity*
that buys time when an Axis-B gap exists. A case is described retrospectively as
**direct** (no stabilization) or **stabilized** (`progress.stabilization is not
None`) — descriptions of what happened, not paths chosen upfront. The
INQUIRY → INVESTIGATING transition requires **Gate 1 only** (problem-statement
confirmation); there is no second gate before investigating.

### 2.3 Stabilization triggers and forwarding

A stabilization is proposed when an Axis-B gap exists. The first and most common
assessment point is immediately after `symptom_verified` (the same point the old
Gate 2 fired) — the agent asks "is there an impact-now gap that can't close this
session?" and, if so, *proposes* a stabilization (user accepts → insert; declines
→ continue). The assessment is **re-evaluable** — a stabilization can also be
proposed later (RCA stalls, situation deteriorates). It is never an irreversible
commitment.

The three triggering circumstances each leave a *different* thing unresolved,
which determines the forwarding path **after** the stabilization verifies (this is
the answer to "what happens after mitigation?" — it is **not** uniformly "continue
to RCA"; that was only row 1 of the old Gate-3 assumption):

| Trigger for inserting a stabilization | cause_state | solution_state | Forwarding after stabilization |
|---|---|---|---|
| **(1)** cause unknown / multiple candidates needing different fixes | UNKNOWN / CANDIDATES | UNKNOWN | **RCA** — hypothesis formulation + evidence-needs |
| **(2)** cause known, solution unclear / multiple complex options | IDENTIFIED | CANDIDATES (reserved) | **Solution deliberation** (follow-on; reuses hypothesis machinery) |
| **(3)** cause + solution known, implementation takes time | IDENTIFIED | SELECTED, `solution_feasible=DEFERRED` | **Handoff / schedule** — CLOSE-with-documented-solution |

If **no** Axis-B gap exists (cause known, solution known, implementable now), the
flow is **direct**: verify → propose solution → accept → verify → RESOLVED. No
stabilization, no hypothesis machinery. (This is the case the old model trapped.)

**Single insert, never a dead-end.** The engine models **one** stabilization per
investigation (forward-only). If the first stabilization doesn't stabilize, the
flow stays open to user-led action: the agent acknowledges it didn't work, may
propose an alternative *in prose / as a fresh proposed action*, and the case
continues opportunistically (or closes). The single-record constraint is a
data-model simplification, not a cap on remediation attempts (INV-24).

**Close-anytime.** CLOSE is always available from any point in the flow — the user
may abandon, or progress may be blocked by data limits (can't obtain the evidence)
or implementation limits (fix can't be applied here). This is the existing
INVESTIGATING → CLOSED disposition handshake, now reachable without a path gate.

### 2.3.1 Derived stage label

The UI stage label is a pure derived view over the action-compliance gates
(redesign R4), not a driver:

- `stabilization.accepted ∧ ¬stabilization.verified` → **"Stabilizing"**
- `solution_accepted ∧ ¬solution_verified` → **"Resolving"**
- else → **"Investigating"** (sub-phase distinguished by `symptom_verified` /
  `cause_state`, not by the stage enum)

### 2.4 Diagnostic Feasibility (Advisory Signal)

Root cause analysis is sometimes infeasible — not because of urgency, but because of **boundary constraints**: the system is a black box, is being decommissioned, or has a known intractable condition where a workaround is the accepted permanent strategy.

The `rca_infeasible` field on `ProblemVerification` captures this as an **advisory signal** — a boolean set by the LLM during verification, paired with a rationale string explaining why.

#### 2.4.1 How It's Set

The LLM evaluates diagnostic feasibility during INQUIRY/verification when it detects:

- **Uncontrollable external dependencies**: 3rd-party SaaS APIs where internal telemetry is inaccessible
- **Deprecated/EOL systems**: Systems scheduled for decommission where RCA engineering hours are wasted
- **Known intractable conditions**: Transient jitters, flaky behaviors where retry/workaround is accepted policy
- **User explicitly declines RCA**: User states "just need a workaround" or "don't want to debug this"

The LLM sets `rca_infeasible=True` and populates `rca_infeasible_rationale` with the reason.

#### 2.4.2 What It Does NOT Do

- **Does not select a path.** There is no path fork to influence (unified opportunistic flow).
- **Does not force closure.** The user can always request RCA even when the signal is set.
- **Does not skip hypothesis formulation.** Even for external dependencies, lightweight hypotheses have diagnostic value (e.g., "the 503s correlate with our request rate exceeding their undocumented limit" is testable).

#### 2.4.3 What It Does: Post-Stabilization Behavior

The signal's effect is narrow and specific — when a stabilization has been verified but the cause remains uncertain, it changes whether the agent pushes RCA or offers closure:

| `rca_infeasible` | Post-stabilization agent behavior |
| --- | --- |
| `False` (default) | Agent pushes toward RCA: *"The stabilization is working. Now let's investigate the root cause to prevent recurrence."* |
| `True` | Agent proposes closure: *"The stabilization is verified. Since [rationale], shall we close this case?"* Uses User-Agent Handshake — user must confirm. |

If `rca_infeasible=True` but the user says "actually, let's dig deeper" — the agent proceeds with RCA. The signal is advisory, not binding.

#### 2.4.4 Terminal State

Cases closed via this path use the existing terminal state:

- `status = CLOSED`
- `closure_reason = "closed_after_investigation"` (the documented stabilization is preserved on the closed case)

`RESOLVED` remains pristine — it always means a permanent fix with verified root cause. See §4.5.1 for runbook generation.

---

## 3. Turn Progress Tracking

### 3.1 Evidence Milestone Validation

The LLM structured output is the **sole authority** for milestone advancement.
When the LLM claims a milestone has been reached (via the `milestones` field in
its response schema), the evidence processor validates the claim against cited
evidence. It does NOT independently advance milestones.

**Design Decision (Issue A)**: The evidence processor was previously a
keyword-based discovery layer that parsed LLM-generated analysis text to find
milestones. This created a dual pathway for advancement and was fragile. It is
now validation-only.

```python
def validate_milestone_claims(
    case: Case,
    milestones_claimed: List[str],
    reasoning: Optional[InternalReasoning] = None,
) -> List[MilestoneValidationResult]:
    """
    Validate that LLM milestone claims are supported by cited evidence.

    This does NOT advance milestones. It checks whether the LLM's claims
    are justified by the evidence IDs cited in internal_reasoning.

    Called: After LLM sets milestones in structured output
    """

    for milestone in milestones_claimed:
        expectations = MILESTONE_EVIDENCE_EXPECTATIONS[milestone]

        # Count evidence in expected categories among cited IDs
        relevant = count_cited_evidence(case, reasoning, expectations)

        if relevant < expectations["min_evidence"]:
            log_warning(
                f"Milestone '{milestone}' claimed with {relevant} relevant evidence "
                f"(expected >= {expectations['min_evidence']})"
            )

# PROGRESS_MILESTONE_EVIDENCE_EXPECTATIONS and the gate-milestone triggers
# are canonical in investigation-data-models.md §1.2 "Progress Milestone
# Evidence Expectations". See that table for min_evidence counts, expected
# categories per progress milestone, and the four gate-milestone triggers.
```

**Evidence Classification**:

Evidence is created after LLM evaluation with a specific category assigned.
See [Evidence Model](./evidence-driven-investigation-framework.md#5-evidence-model) for the canonical specification.

| Category | Description | Used In Stage |
| --- | --- | --- |
| `SYMPTOM_EVIDENCE` | Data showing the problem exists (verifies symptoms, scope, timeline, changes) | DIAGNOSIS, TREATMENT |
| `CAUSAL_EVIDENCE` | Data explaining why the problem happened (requires hypothesis to exist) | DIAGNOSIS, TREATMENT |
| `MITIGATION_EVIDENCE` | Data showing whether the temporary fix worked | MITIGATION |
| `SOLUTION_EVIDENCE` | Data showing whether the permanent fix worked | TREATMENT |

```

### 3.2 Turn Recording and Progress Detection

```python
async def record_turn(
    case: Case,
    user_message: str,
    agent_response: str
) -> TurnProgress:
    """Record turn and detect progress"""

    # Capture state before
    progress_before = case.progress.dict()
    evidence_count_before = len(case.evidence)

    # Process turn (agent work happens here)

    # Capture state after
    progress_after = case.progress.dict()
    evidence_count_after = len(case.evidence)

    # Detect state changes (gate signals and progress indicators)
    # NOTE: the stabilization pair (mitigation_accepted/mitigation_verified) are
    # LLM emission symbols that materialize into the single progress.stabilization
    # record; cause_state is engine-derived (recomputed, not boolean-diffed here).
    STAGE_GATE_MILESTONES = {"mitigation_accepted", "mitigation_verified", "solution_accepted", "solution_verified"}
    PROGRESS_INDICATORS = {"symptom_verified", "solution_proposed"}

    all_changed = [
        key for key in progress_before
        if isinstance(progress_before[key], bool)
        and progress_before[key] == False
        and progress_after[key] == True
    ]

    # Gate milestone changes trigger stage recomputation
    stage_gate_completed = [k for k in all_changed if k in STAGE_GATE_MILESTONES]

    # Progress milestone changes are recorded but do NOT affect stage
    indicators_completed = [k for k in all_changed if k in PROGRESS_INDICATORS]

    milestones_completed = all_changed  # Both types are recorded in turn history

    # Detect evidence added
    evidence_added = []
    if evidence_count_after > evidence_count_before:
        new_evidence = case.evidence[evidence_count_before:]
        evidence_added = [e.evidence_id for e in new_evidence]

    # Detect hypotheses generated this turn
    hypotheses_count_before = len([h for h in case.hypotheses.values() if h.created_at < turn_start_time])
    hypotheses_count_after = len(case.hypotheses)
    hypotheses_generated = hypotheses_count_after - hypotheses_count_before

    # Detect solutions proposed this turn
    solutions_count_before = len([s for s in case.solutions if s.proposed_at < turn_start_time])
    solutions_count_after = len(case.solutions)
    solutions_proposed = solutions_count_after - solutions_count_before

    # Determine if progress made (broadened definition)
    progress_made = _check_if_progress_made(metadata)

    # ============================================================
    # PROGRESS DEFINITION (for turns_without_progress counter)
    # ============================================================
    #
    # Progress IS made when ANY of the following occur:
    #
    # STRUCTURAL ARTIFACTS:
    # - Gate milestone transitions False → True (e.g., solution_accepted)
    # - Progress milestone transitions False → True (e.g., symptom_verified)
    # - Evidence is added to the case
    # - New hypothesis is generated
    # - Hypothesis state changes (ACTIVE → VALIDATED/REFUTED)
    # - ProposedAction is created (agent proposed something actionable)
    # - User confirms problem statement or path selection
    # - Files uploaded
    # - Case action occurred (phase transition or disposition change)
    #
    # INVESTIGATIVE BEHAVIORS (a skilled troubleshooter gathering data IS progressing):
    # - TurnOutcome.DATA_REQUESTED — agent asking for specific data
    # - TurnOutcome.HYPOTHESIS_TESTED — hypothesis evaluated this turn
    # - TurnOutcome.DATA_PROVIDED — user responded with requested data
    # - hypothesis_evidence_links_applied > 0 — evidence linked to hypotheses
    #
    # Progress is NOT made when:
    # - Pure CONVERSATION with no structural or investigative activity
    # - Agent repeats previous information
    # - Conversation is off-topic or circular
    #
    # RATIONALE: The old definition only counted structural artifacts, causing
    # premature stagnation detection when the agent was actively investigating
    # (requesting data, testing hypotheses, linking evidence). A copilot that
    # is actively gathering information should not be penalized.

    # Create turn record
    turn = TurnProgress(
        turn_number=case.current_turn,
        milestones_completed=milestones_completed,
        evidence_added=evidence_added,
        progress_made=progress_made,
        # Updated logic: Robust outcome determination based on milestones, evidence, hypotheses
        outcome=self._determine_turn_outcome(case, metadata, outcome_override="conversation")
    )

    case.turn_history.append(turn)
    case.current_turn += 1

    # Track turns without progress
    if progress_made:
        case.turns_without_progress = 0
    else:
        case.turns_without_progress += 1

    # Progress monitoring (replaces old stagnation detection)
    # After N investigative turns without a milestone completing, the
    # ProgressMonitor activates transparent mode — surfacing what milestone
    # is pending and what evidence would advance it. Also checks for agent
    # state repair patterns (hypothesis deadlock, anchoring, etc.).
    # See: docs/architecture/investigation-engine/progress-transparency.md

    return turn


def determine_turn_outcome(case: Case, progress_made: bool) -> TurnOutcome:
    """
    Determine turn outcome classification.

    Checked AFTER milestone detection and evidence processing.
    Used for LLM observability and metrics (not workflow control).
    """

    # Disposition action
    if case.is_terminal:
        return TurnOutcome.CASE_RESOLVED if case.state == CaseState.RESOLVED else TurnOutcome.OTHER

    # Milestone completed
    if any(milestone_completed_this_turn(case)):
        return TurnOutcome.MILESTONE_COMPLETED

    # Hypothesis validated
    if any(h.tested_at == case.current_turn for h in case.hypotheses.values()):
        return TurnOutcome.HYPOTHESIS_TESTED

    # Evidence provided
    if any(e.collected_at_turn == case.current_turn for e in case.evidence):
        return TurnOutcome.DATA_PROVIDED

    # Agent requested data
    if agent_requested_data_this_turn(case):
        return TurnOutcome.DATA_REQUESTED

    # Conversation only
    return TurnOutcome.CONVERSATION
```

### 3.3 Diagnostic Reasoning Requirements

The agent must demonstrate context-specific diagnostic reasoning — grounded in this case's evidence — before suggesting any action, mitigation, or hypothesis during INVESTIGATING. The reasoning structure (OBSERVATION → ANALYSIS → CONCLUSION), prohibited/required patterns, and worked BAD/GOOD examples are canonical in:

See **[Agent Behavioral Rules — Rule 2: Evidence-Grounded](./agent-behavioral-rules.md#rule-2-evidence-grounded)**.

**Scope note**: The requirement applies to all agent suggestions during INVESTIGATING state (mitigation proposals, hypothesis generation, diagnostic/solution suggestions, evidence requests). INQUIRY state (problem statement refinement) is exempt because investigation hasn't started yet.

---

## 4. Supported Case Lifecycles

This section outlines all possible case lifecycles and their associated milestones.

### 4.1 Inquiry-Only Lifecycle (No Investigation)
**User Goal**: Ask a quick question or get clarification without starting a formal investigation.
**Flow**: `INQUIRY` → `CLOSED`

#### Workflow Steps
1.  **User Inquiry**: User asks a question (e.g., "How do I check logs?").
2.  **Agent Response**: Agent answers the question.
3.  **Closure**: User leaves or explicitly closes the session.

#### Milestones
*   None (Investigation milestones do not start).

---

### 4.2 KB-Resolution Path (Same-Turn Collapse)

**User Goal**: Resolve a known issue quickly using a runbook match.
**Flow**: `INQUIRY` → `INVESTIGATING` → `RESOLVED` (with INVESTIGATING typically completing in 1–2 turns)

This is not a separate lifecycle edge — it is the standard `INQUIRY → INVESTIGATING → RESOLVED` flow where INVESTIGATING completes rapidly because the matched runbook Cause supplies the root cause, mechanism, and fix without requiring multi-turn evidence gathering. See [§1.2 INVESTIGATING → RESOLVED → KB-Resolution Path](#kb-resolution-path-same-turn-variant) for the engine mechanism.

#### Workflow Steps

1.  **Detection (during INQUIRY)**: Agent calls `kb_qa` for the symptom and identifies a high-confidence runbook match. KB match is held back from the user until problem statement is confirmed.
2.  **Problem confirmation (INQUIRY → INVESTIGATING)**: Agent presents problem statement; user confirms. Standard INQUIRY → INVESTIGATING transition fires.
3.  **Cause attribution (early INVESTIGATING)**: Engine runs [Indicator resolution](./indicator-resolution.md) against current case state to attribute the active `### Cause <X>` from the retrieved runbook. If attribution is unambiguous, agent proposes the Cause's `Mitigation` + `Resolution` to the user.
4.  **User applies the fix and confirms** ("That fixed it" / "It worked"). LLM emits `knowledge_resolution` in `state_updates`.
5.  **Same-turn milestone collapse**: Engine populates `RootCauseConclusion` (Statement → `root_cause`, Mechanism → `mechanism`), creates `Solution` (Mitigation → `immediate_action`, Resolution → `longterm_fix`), and sets `root_cause_identified`, `solution_accepted`, `solution_verified`.
6.  **Standard RESOLVED handshake**: LLM emits `ProposedTransition`; user's same confirmation message is recognized as the disposition acknowledgment; transition executes.

#### Milestones

* All standard INVESTIGATING milestones populated in the collapse turn: `symptom_verified`, `root_cause_identified`, `solution_proposed`, `solution_accepted`, `solution_verified`.
* `knowledge_resolution` signal recorded for KB-attribution metrics.

---

### 4.3 Direct Investigation (no stabilization)
**User Goal**: Diagnose an issue, find the root cause, and fix it permanently — with no impact-now gap that requires a stabilization detour.
**Flow**: `INQUIRY` → `INVESTIGATING` → `RESOLVED`

This is the common case: cause known or discoverable, solution implementable now.

#### Workflow Steps & Milestones

**Phase 1: Inquiry**
*   **Goal**: Establish problem statement.
*   **Transition Trigger**: User confirms problem statement (Gate 1) and decides to investigate.

**Phase 2: Investigation** (one opportunistic flow)

*   Agent verifies symptoms using evidence
    *   Progress indicator: `symptom_verified` (LLM sets when symptoms confirmed)
*   Diagnostic machinery runs **iff the cause is uncertain** (`cause_state ∈ {UNKNOWN, CANDIDATES}`):
    *   Agent forms hypotheses, tests against evidence; when ≥2 plausible causes remain active the engine derives `cause_state=CANDIDATES`
    *   When a single cause is established (or the error is self-naming), the engine records `cause_state=IDENTIFIED`. For self-naming errors this can happen as early as turn 1 — the engine records the cause it legitimately knows; it is never path-stripped.
    *   Hypothesis-before-causal-evidence ordering is prompt guidance in the unified INVESTIGATION block (a causal claim presupposes a hypothesis to attach to)
*   Agent proposes a concrete solution action
    *   Progress indicator: `solution_proposed`; assessment: `solution_state=SELECTED` (`solution_feasible=NOW`)
*   **Solution accepted → "Resolving"** (inference-based)
    *   User complies with the proposed solution → gate milestone `solution_accepted`
    *   If user questions or refuses → continues investigating, agent refines approach
*   **Resolution verification** (iterative)
    *   Agent verifies whether the fix worked from submitted evidence
    *   If fix worked → agent proposes resolution via User-Agent Handshake
    *   If fix failed → extended investigation: failure analysis → gap identification → targeted evidence request → new hypothesis → revised fix; escalation when no viable options remain

**Phase 3: Resolution**
*   **Transition Trigger**: User confirms fix worked via User-Agent Handshake → gate milestone `solution_verified`
*   **State**: `RESOLVED`.

---

### 4.4 Stabilized Investigation (impact-now gap)
**User Goal**: Stop active impact quickly, then continue the investigation.
**Trigger**: An Axis-B stabilization gap — something is hurting now that can't be fully resolved this session. The agent *proposes* a stabilization in-prompt (no path is chosen upfront); the user accepts or declines.

A stabilization is an **insert** into the same unified flow, not a separate path
(§2.3). After it verifies, forwarding depends on what is still unresolved (§2.3
forwarding table): cause uncertain → RCA; cause known / solution unclear →
solution deliberation; cause + solution known but deferred → CLOSE-with-documented-solution.

#### Stabilized → RESOLVED
**Flow**: `INQUIRY` → `INVESTIGATING` (stabilization insert, then RCA + permanent fix) → `RESOLVED`

**Gate signals / record**:

*   `mitigation_accepted` (LLM emission) → materializes `stabilization.accepted`: user acknowledged executing the proposed stabilization.
*   `mitigation_verified` (LLM emission) → materializes `stabilization.verified` + `completed_at_turn`: the stabilization stabilized the situation. The case continues opportunistically (the "Stabilizing" label clears).
*   `solution_accepted`: user acknowledged executing the permanent solution → "Resolving".
*   `solution_verified`: permanent fix validated (User-Agent Handshake) → RESOLVED.

#### Stabilized → CLOSED
**Flow**: `INQUIRY` → `INVESTIGATING` (stabilization insert) → `CLOSED`

The user decides the stabilization is sufficient (or RCA is infeasible, see §2.4) and does not pursue a permanent fix. Two paths lead here:

1. **Agent-proposed** (when `rca_infeasible=True` and the cause remains uncertain): after the stabilization verifies, the agent proposes closure via User-Agent Handshake instead of pushing RCA.
2. **User-initiated** (any case): the user closes via UI at any time (close-anytime, §2.3).

**Closure**: `CaseState.CLOSED` with `closure_reason="closed_after_investigation"`. The documented stabilization (and any partial findings) is preserved on the closed case.

**Post-terminal**: agent offers runbook generation only for RESOLVED cases (§4.5.1); CLOSED cases get the closure summary only.

#### Stabilization Is Iterative, Forward-Only

A stabilization is not assumed one-shot. The agent may adjust its approach and propose multiple attempts until the user verifies stabilization; each accepted attempt is recorded in `action_attempts`. The `stabilization` record itself is **single and forward-only** (INV-24): `accepted` / `verified` are never reset, and `completed_at_turn` is stamped once when `verified` first flips True (the boundary for up-weighting pre-stabilization evidence in later RCA). If a stabilization fails to stabilize, the flow stays open to user-led action — the agent acknowledges it didn't work and may propose an alternative in prose, or the user closes (§2.3, the no-dead-end rule).

#### How the System Distinguishes Outcomes (Retrospectively)

The retrospective shape is **direct** vs **stabilized**, derived from
`progress.stabilization is None`:

| Field | Stabilized → RESOLVED | Stabilized → CLOSED | Direct → RESOLVED |
| ----- | ------------------- | ------------------- | ----------------- |
| `stabilization` present | Yes | Yes | No |
| `stabilization.accepted` / `.verified` | True / True | True / True | n/a |
| `solution_accepted` / `solution_verified` | True / True | False / False | True / True |
| `cause_state` | IDENTIFIED | May be UNKNOWN/CANDIDATES | IDENTIFIED |
| `CaseState` | RESOLVED | CLOSED | RESOLVED |
| `closure_reason` | None | `closed_after_investigation` | None |
| **Knowledge artifact** | **Runbook** | **Closure Summary only** | **Runbook** |

`closure_reason` is `None` for all RESOLVED cases — resolution itself is the
categorization. Only CLOSED cases carry a `closure_reason` value (`inquiry_only`
or `closed_after_investigation`). `derive_closure_reason` (in
`terminal_transitions.py`) returns `inquiry_only` when the case never left INQUIRY,
otherwise `closed_after_investigation` — the former `mitigation_sufficient` reason
was folded into the latter.

---

### 4.5 Post-Terminal Operations

After a case reaches RESOLVED or CLOSED, the system auto-generates a terminal summary. For resolved cases, the user may additionally request runbook generation.

#### 4.5.0 Auto-Generated Terminal Summary

**Trigger**: Synchronous on terminal transition (both RESOLVED and CLOSED). The closure-turn agent reply waits for generation to complete and embeds the rendered markdown inline. Generation exceptions are caught — the state transition still commits — but the chat reply tells the user generation didn't complete and the regen affordance is offered on the next terminal turn for retry.

**Implementation**: `MilestoneEngine._auto_generate_report()` calls `ReportGenerationService.generate_reports()` after the case is saved in terminal state, returns either the rendered markdown (success), a skip note (gate FAIL), or a failure note (LLM error). The closure-turn reply is composed by `_compose_terminal_reply()` which appends the return value to the deterministic status line. Called from three places: the explicit-confirmation path, the dropdown-resolution path, and the end-of-turn LLM-driven transition path.

**Substance gate** (`should_generate_terminal_summary()` in `terminal_transitions.py`): RESOLVED always generates — a verified solution is meaningful content by definition. CLOSED requires `evidence > 0` OR `hypotheses > 0` OR `completed_milestones > 0`. The gate is substance-only by design — conversation depth (`message_count`) is intentionally not a signal, since terminal Q&A inflates it and would let the verdict flip after closure. The three substance signals are naturally frozen in CLOSED state, so the gate is stable across the terminal lifetime without a snapshot field.

**Summary types**:

| Case Status | Report Type | Content Structure |
|-------------|-------------|-------------------|
| RESOLVED | `RESOLUTION_SUMMARY` | Problem Statement, Root Cause (from validated hypotheses), Solution Applied, Confirming Evidence, Timeline, Milestones Reached, Stabilization (if any) |
| CLOSED | `CLOSURE_SUMMARY` | Problem Statement, Investigation State (milestones/evidence/hypotheses counts), Closure Reason, Leading Hypotheses (top 5 by confidence), Mitigation Status, Timeline, Recommendation (for escalated/abandoned cases) |

Summaries are built from case data fields (hypotheses, solutions, evidence, milestones, timestamps). Stored as `CaseReport` records with `auto_generated=True`. Duration is calculated from `created_at` to `resolved_at` or `closed_at`.

**Report type enum** (`ReportType` in `case/domain/owned_models/report.py`):

- `RESOLUTION_SUMMARY` — auto-generated for resolved cases (always generated)
- `CLOSURE_SUMMARY` — auto-generated for closed cases (subject to substance gate)
- `RUNBOOK` — user-requested via ConversionService (see §4.5.1)

**Dashboard**: `ReportTab` is view-only — displays auto-generated summaries with formatted markdown rendering and download. No manual generate button. If no summary was generated for a closed case (substance gate FAIL), the tab surfaces a derived skip-reason note (via `terminal_summary_skip_reason()` in `terminal_transitions.py`). If the gate PASSed but no Report row exists (generation failed), the tab surfaces a "regenerate from Copilot" note. RESOLVED cases always have a summary.

**Two views, one record**: The chat and the Dashboard show the same `CaseReport` row. The chat renders it once at the moment of generation (and again on each regeneration); the Dashboard renders it persistently. There is exactly one summary per case — each regeneration overwrites the row.

**API endpoints:**

- `GET /api/v1/cases/{case_id}/reports` — List generated reports
- `GET /api/v1/cases/{case_id}/reports/{report_id}/download` — Download report
- `POST /api/v1/cases/{case_id}/reports` — Regenerate (requires terminal state)

#### 4.5.1 Runbook Generation (Knowledge Flywheel)

**Eligibility**: RESOLVED cases only. CLOSED cases are not eligible regardless of `closure_reason` — they lack the confirmed root-cause-to-solution chain that a future investigator can apply. On subsequent terminal Q&A turns, a CLOSED case offers "Regenerate closure summary" when the substance gate would PASS (independent of whether the Report row currently exists — the same affordance handles both re-roll and failed-generation retry). For low-substance closures, no suggestion is offered — there's nothing to summarize.

**Design**: Suggest first, evaluate on acceptance. The agent always offers a COOPERATIVE suggestion at resolution time. Readiness assessment and deduplication happen only when the user accepts — not upfront. This avoids wasted computation and gives the user a clear accept/decline choice.

**Trigger flow (Copilot)**:

```text
User confirms resolution
    → Agent offers COOPERATIVE suggestion: "Would you like me to create a runbook?"
    → User accepts
        → System evaluates readiness + deduplication
        → Four possible outcomes:
            SUCCESS           → Draft created, user redirected to Dashboard
            NOT_SUITABLE      → "Not enough data for a quality runbook" (no draft)
            EXISTING_COVERS   → "Similar runbook exists: {title} ({score}% match)"
            GENERATION_FAILED → "Generation failed, try again later"
    → User declines or ignores
        → No evaluation, no side effects
```

**Trigger flow (Dashboard)**:

Users can also generate runbooks from the copilot UI on resolved cases. The generated draft appears in the Dashboard KB page Drafts tab. The same readiness + dedup evaluation applies.

**Readiness assessment** (`assess_runbook_readiness()` in `terminal_transitions.py`):

Maps case data to the 7 canonical runbook sections and checks coverage.

| Verdict | Condition | Outcome |
|---------|-----------|---------|
| `READY` | Problem + root cause + actionable solution + at most 1 enrichment gap | Draft generated |
| `NEEDS_ENRICHMENT` | Critical sections OK, but 2+ enrichment sections thin | Draft generated with quality warning |
| `NOT_SUITABLE` | Missing problem definition or root cause with actionable fix | `NOT_SUITABLE` outcome — no draft |

**Deduplication** (`RunbookKnowledgeBase` vector search):

| Similarity | Verdict | Outcome |
|------------|---------|---------|
| ≥85% | `EXISTING_COVERS` | No new draft — link to existing runbook |
| 70-84% | `SUGGEST_WITH_CAVEATS` | Draft generated with note about similar runbook |
| <70% | No conflict | Draft generated normally |

**Workflow** (canonical path via `ConversionService`, triggered after user accepts):

1. `POST /api/v1/knowledge/convert-from-case` — extracts case data (solutions, root cause, hypotheses, evidence, domain/service)
2. LLM generates canonical runbook (YAML frontmatter + 7 markdown sections) using `CONVERSION_SYSTEM_PROMPT`
3. `RunbookValidator` checks structure; `QualityScorer` evaluates completeness, clarity, actionability (0-100 score)
4. Draft created in `draft` status for user review
5. User edits draft → re-validates → verifies → ingests into ChromaDB vector DB
6. Verified runbook is chunked (512 tokens, 50-token overlap), embedded (BGE-M3, 1024 dims), indexed for future similarity search

**Canonical runbook sections**: Problem Definition, Diagnostic Steps, Mitigation, Root Cause Resolution, Verification, Prevention, Sources.

**API endpoints:**

- `POST /api/v1/knowledge/convert-from-case` — Generate runbook from resolved case
- `PUT /api/v1/knowledge/conversions/{id}/drafts/{draft_id}` — Edit draft (re-validates)
- `POST /api/v1/knowledge/conversions/{id}/drafts/{draft_id}/verify` — Verify → ingest into vector DB
- `DELETE /api/v1/knowledge/conversions/{id}/drafts/{draft_id}` — Soft delete draft

#### 4.5.2 Knowledge Suggestion Extraction

**Eligibility**: RESOLVED cases only. This is a separate workflow from runbook generation — it produces structured knowledge articles (Problem, Root Cause, Solution, Prevention) rather than step-by-step runbooks.

**Trigger point**: Backend extraction API (`POST /knowledge/suggestions/extract`). Previously had a dedicated KnowledgeTab on the Dashboard; now managed through the KB page workflow.

**Workflow:**

1. User clicks "Extract Knowledge" → `POST /api/v1/knowledge/suggestions/extract`
2. LLM extracts structured article with automatic PII removal
3. Suggestion created in `PENDING_REVIEW` status with PII scan
4. Admin reviews: edit title/content, verify PII scan, approve or reject
5. On approval: creates `KnowledgeItem` in the knowledge base

**PII scan pipeline**: `NOT_SCANNED` → `SCANNING` → `CLEAN` | `PII_DETECTED` → `REMEDIATED`

**API endpoints:**

- `POST /api/v1/knowledge/suggestions/extract` — Extract from case
- `GET /api/v1/knowledge/suggestions?case_id={id}` — Get suggestion for case
- `PUT /api/v1/knowledge/suggestions/{id}` — Update title/content
- `POST /api/v1/knowledge/suggestions/{id}/approve` — Approve → create KnowledgeItem
- `POST /api/v1/knowledge/suggestions/{id}/reject` — Reject with reason
- `POST /api/v1/knowledge/suggestions/{id}/remediate-pii` — Auto-remediate PII

#### 4.5.3 Cross-Frontend Linking

The copilot links to dashboard for operations that require richer UI:

| Copilot Action                                | Dashboard URL                                      |
|-----------------------------------------------|----------------------------------------------------|
| "View in Dashboard" (after report generated)  | `{DASHBOARD_URL}/cases/{caseId}?tab=report`        |
| "Extract as knowledge article" nudge          | `{DASHBOARD_URL}/cases/{caseId}?tab=knowledge`     |

Dashboard `CaseTabs` reads the `tab` query parameter to auto-select the correct tab on load.

#### 4.5.5 Archival

Independent of post-terminal operations. User can archive any terminal case via the Dashboard case detail page. Archived cases are hidden from the default list but remain accessible via "Include archived" filter.

---

### 4.6 Abandoned / Escalated Investigation
**User Goal**: Investigation stalled or handed off to human expert.
**Flow**: `INQUIRY` → `INVESTIGATING` → `CLOSED`

#### Workflow Steps
1.  **Investigation Starts**: Gates and progress indicators partially set.
2.  **Stall/Escalation**:
    *   Agent cannot find root cause (no viable options — communicates limitations and suggests escalation).
    *   User stops responding.
    *   User explicitly requests escalation.
    *   User closes after a stabilization without pursuing RCA (the documented stabilization is preserved on the closed case).
3.  **Closure**: Case marked `CLOSED` with `closure_reason="closed_after_investigation"` (covers escalated, abandoned, and stabilized-then-closed alike).

#### Milestones

*   Partial progress: `symptom_verified`; `cause_state` may be UNKNOWN/CANDIDATES/IDENTIFIED; `solution_proposed`.
*   The stabilization record may be present (`accepted`/`verified`) if a stabilization was performed.
*   `working_conclusion`: Summary of findings up to the point of closure.
*   `action_attempts`: Complete record of all stabilization and solution actions attempted.
