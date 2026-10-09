# Investigation Lifecycle Logic

This document defines the state transitions, the unified opportunistic flow, and turn tracking logic for FaultMaven's evidence-driven investigation framework.

**Related Documents**:

- [Evidence-Driven Investigation Framework](./evidence-driven-investigation-framework.md) - Overview and philosophy
- [Investigation Data Models](./investigation-data-models.md) - Core data structures

§2 below is the canonical specification *and* design rationale for the unified opportunistic flow (the former `mitigation_first` / `root_cause` path fork, retired). It absorbs the shipped flow-redesign in full — Axis-A/Axis-B split, mitigation-as-insert, assessment-vs-gate variables, and the resolved design decisions (§2.5).

---

## Table of Contents

1. [Investigation Lifecycle](#1-investigation-lifecycle)
2. [Mitigation as an Insert](#2-mitigation-as-an-insert)
3. [Turn Progress Tracking](#3-turn-progress-tracking)
4. [Supported Case Lifecycles](#4-supported-case-lifecycles)

---

## 1. Investigation Lifecycle

### 1.1 Case Action Map

```text
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
       │                              │ Mitigating         │
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

1. The statement is presented — for Gate 1 the **engine** does this, composing
   the standing `proposed_problem_statement` into the reply beside the
   confirm/refine pair on every pending turn (INV-01). The LLM's job is to keep
   the field right; it does not present and does not ask.
2. User explicitly confirms with Yes/No buttons or typed response

**Natural flow (Section 1.2)**:

- Turn N: User says "let's investigate"
- Turn N response: Agent presents problem statement + [Yes/No]
- Turn N+1: User clicks [Yes] or types confirmation
- Turn N+1 response: Agent transitions status

**Consent arriving with a statement write** — two different shapes, two
different answers (INV-01):

- *First write + confirm in one shot.* Nothing stood for the user to have
  seen, so the consent is **refused** by `gate1_statement_is_confirmable`. The
  statement is kept, because the next turn needs something to present.
  Turn N+1 then does nothing special, which is the point: Gate 1 is still
  pending, so the engine composes the statement and offers the pair exactly as
  it does on every pending turn.
- *Revision + confirm in one shot.* A statement the user saw already stood, so
  the consent is honoured — against **that** wording. The revision is
  **dropped**, not applied. Refusing here instead would loop: the engine would
  re-present the reword, the user would say yes again, and a model that
  re-emits the field with cosmetic edits would reword again, forever. This is
  also what protects the DECIDE click, where section 0c commits Gate 1 *before*
  the LLM call and the same-turn rewording would otherwise reach
  `case.description` through `_transition_to_investigating`.

There is no recovery FLAG and no recovery turn. `handshake_deferred_at_turn`
existed to tell the following turn to re-present — a proxy for "the user has
not seen this", needed only while presentation was the LLM's job. It was
retired with the fork it selected (#1607).

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

    # Confirmation is two-tier (§1.5.3, INV-01). Gate 1 commits only on its
    # current click, or on a turn whose typed text is bare consent (#1794):
    #   1. Click path: a DECIDE confirmation-suggestion click arrives as
    #      intent_type="confirmation" + confirmation_value=True, and commits
    #      only when its proposal_id names the statement shown
    #      (gate1_offer_key; #1812). Any other click is refused.
    #   2. LLM path: the LLM sets user_confirmed_investigation=True in
    #      state_updates; accepted ONLY when proposed_problem_statement
    #      existed on a PRIOR turn (same-turn-confirmation guard, 13ff2eae)
    #      AND the typed text is one bare consent token (gate1_bare_consent).
    #      A resolver-minted confirmation is screened the same way.
    if confirmation_click_intent(intent) or llm_confirmation_accepted(updates, case):
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)

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
      1. Click path — DECIDE confirmation suggestions carry
         intent metadata. A click sends intent_type="confirmation"
         + confirmation_value=True + the card's proposal_id, and section
         0c commits Gate 1 only when that key names the statement shown
         (`gate1_offer_key`; #1812, see §1.5.3). A click is never screened
         by its payload text.
      2. LLM path — the LLM sets `user_confirmed_investigation=True`
         in `state_updates`. The engine accepts it ONLY when a
         `proposed_problem_statement` existed on a PRIOR turn (the
         same-turn-confirmation guard added in commit 13ff2eae, after
         the LLM was observed collapsing the two-step handshake on
         first-turn "please investigate" inputs), AND the turn's typed
         text is, as a whole, one bare consent token (`gate1_bare_consent`:
         `confirmation_token_class(text, to_state=None)`; #1794).

    There is no regex matcher here for free-typed text: the historical
    `user_confirms()` was removed in commit 06cfa834 (2026-03-17). The LLM
    reads typed text, and its flag is the honest reading; the bare screen
    decides whether it commits. On a flag that is not bare ("yes but it's
    the primary too", "ok, don't start yet") nothing commits, Gate 1 stays
    pending, the statement revision the turn carries is kept (the write
    guard keys on the screened consent), and the refusal is counted as
    `inquiry_handshake_deferred_total{reason="not_bare"}`. A mint the
    IntentResolver makes from typed text is screened the same way, at the
    adoption guard and at 0c. A bare typed consent with no intent is read by
    the engine itself (#1841, ruling (b)): while Gate 1 is pending, section
    0c commits it when `gate1_bare_consent` holds, through the same
    `_commit_gate1` the click uses, as the terminal gate reads a bare token
    itself (`pending_gate_verdict`; #1783). So a typed "yes" no longer waits
    on the flag, or on a card left in `last_suggestions` to mint from. The
    service never offers that reply to the out-of-band classifier: decorated
    ("looks good :ok_hand:"), it passes the four-word continuation gate.
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

**WHO OPENS THE HANDSHAKE (INV-43)**:

Step 1 has four openers, and the fourth is the engine's own. Three of them are
*someone asking*:

| Opener | Fires when |
|---|---|
| The model's `proposed_transition` | COMPLETION's co-emit rule: the fix is verified and the model emits the transition beside its backing `causal_absence_evidence` row |
| The user, *through the model* | Natural language ("mark this resolved", "the fix worked") reaches the state machine only by the model emitting `proposed_transition` — so this is not a fourth mechanism, it is opener 1 with a different trigger. There is no NL detector: `IntentResolver` matches typed text against suggestions already on screen, and with nothing standing it has nothing to match. NOT the status menu either — RESOLVED is not user-selectable, and a `status_transition` request for it is refused at the service boundary and again in the engine. The one exception is the engine's own declined offer re-presented: the **Mark it resolved** chip (#1895, below) |
| `_maybe_propose_deferred_close` | `solution_feasible == DEFERRED` — and on a confirmed case its SUGGEST_RESOLVE pivot offers RESOLVED rather than CLOSED |
| `_maybe_propose_confirmed_resolution` | **Backstop.** The case is resolution-READY and none of the above opened the handshake |

The backstop exists because prompt compliance is not a correctness mechanism —
the same reasoning that made Gate 1's presentation engine-owned in #1607. A
model that records the confirmation row and omits the transition leaves the case
READY, eligible, and un-offered; stalled, it then reaches the mid-investigation
correctives and is asked to restate a problem it has already confirmed resolved.

It is a backstop and not a fourth proposer by three rules:

- **Last.** It runs at step 4c of `_apply_turn_response` (the
  `_process_turn_impl` phase that applies the turn's response), after `_check_automatic_transitions`, and bails on any
  `pending_transition` an earlier opener left standing.
- **Same bar.** Its trigger is `assess_resolution_readiness` READY — a
  qualifying `causal_absence_evidence` row — not a looser reading of "looks
  finished". A stabilized case (symptom relieved, cause persists) and the
  engine's own M6 failed-fix rows do not trip it.
- **Silent inside a handshake.** A turn that *began* with a standing
  disposition offer is the user's to answer, even where a branch withdrew that
  offer mid-turn. Opening a different target into that channel is how a "yes"
  meant for the offer on screen lands on the one substituted underneath it.

A decline postpones it rather than counting against it: the refusal is recorded
against the `deferred_disposition_signature` that justified the offer, in the
same space the deferred proposer uses, so declining either silences both until a
premise moves (fm#1122). The refusal binds whoever opened the offer — an
LLM-opened one carries no signature of its own, so one is derived at decline
time; otherwise the backstop, which fires on readiness alone, re-proposes on the
next turn. A declined deferred close binds the model too (#1889): while its
signature stands, a model `closed` proposal that the closure check keeps at
CLOSED is refused (one it pivots to RESOLVED on a resolvable case is not), with
feedback saying the model may propose it only when the user directs it, and a
**Close with the solution documented** card appended to the turn's follow-ups.
(A false-alarm close is not in this signature space at all; its decline is
recorded on the finding, §1.4.1.)

**A declined RESOLVE binds the model too, until a NEW confirmation (#1895).** A
resolution is earned, not requested, so after a decline it is due back exactly
when the state that earned it moves — and the move a user most often brings
after "not yet" is a fresh confirmation that the fix held. The signature names
it: `verdict | solutions | cause leg | confirmation ids`, the fourth part the
sorted ids of the qualifying `causal_absence_evidence` rows. A decline stands
while an entry with the same first three parts holds every id that qualifies
now (a subset rule, read in one place, `covering_declined_signature`): a row
that stops qualifying, or is pruned and re-enters, moves nothing; elapsed turns
and repeated declines never do; one row the decline never saw does.

- **The decline turn covers what it records.** A deflection ("not yet, it has
  only been clean for an hour") falls through to the model, which may record the
  user's words as a confirmation row that same turn. Section 0b marks the turn,
  and the signature is re-stamped against the turn's rows immediately before
  `check_automatic_transitions`, on a SUGGEST_RESOLVE verdict only.
- **The model is refused.** While the decline stands, step 2 refuses a model
  `resolved` on a READY case and a model `closed` that the closure check pivots
  to RESOLVED (INV-37), with feedback. The user's own close pick still pivots to
  the resolve offer: this binds the model, never the user.
- **It returns on the turn it is earned.** A turn that records a new
  confirmation moves the signature before step 2 and the backstop, so the offer
  comes back on that turn, from the model or the backstop.
- **A user who changes their mind** is not asked to manufacture evidence (a
  request is never a confirmation row). Every turn that records a resolve
  decline, or refuses a re-proposal, carries the engine's **Mark it resolved**
  chip: a `status_transition` to `resolved` whose `proposal_id` is the reopen
  key, a digest of the declined entry the decline stands on. The service
  boundary and the engine admit that one request while the entry still covers
  the case; the engine then proposes on a READY case with the usual pair, and
  the user confirms (INV-03). A stale key (a new confirmation has since moved
  the state) keeps the 422, and by then the engine is offering the resolution
  itself. The bare "no" is answered with what brings the offer back and the
  chip. The prompt carries a conditional line while the decline stands: propose
  on a new verification, or when the user asks (the engine then attaches the
  chip), never unprompted.

**Why RESOLVED left the status menu.** It was listed in `USER_SELECTABLE_ACTIONS`
until the engine could see the readiness bar for itself, and the listing was
never the gate it looked like: the dict is consulted with no case content, so
`valid_next_states` advertised `resolved` on every investigating case, including
ones the readiness gate would have refused. One client reconciled that against
`disposition_eligibility` — a convention, not a rule, and the legacy fallback did
not follow it. The check now decides whether the offer is MADE rather than
arguing with a pick already taken, which also retired the arm that could confirm
a `needs_info` proposal without re-reading readiness.

One consequence worth stating plainly: on a resolution-ready case the status menu
is **empty**, and that is correct rather than a gap. The reopen chip (#1895) does
not change that: it is a follow-up the engine attaches after a decline, not a
menu entry, and `USER_SELECTABLE_ACTIONS` still lists only dispositions. `closed` reads
`suggests_alternative` there, which holds exactly when a qualifying
`causal_absence_evidence` row is on the case — so INV-37 pivots any close back to
a resolve proposal. A Close control on such a case could only ever produce "shall
I mark this resolved?". The case has one terminal destination and the engine is
already offering it.

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

**SUGGEST_CLOSE pivot for RESOLVED:** When resolution readiness returns `SUGGEST_CLOSE` (no root cause, no solution, no evidence), the LLM-emit path (the one way a RESOLVED proposal reaches the state machine besides the engine's own READY offer, INV-43) immediately pivots the pending proposal to CLOSED and present the close confirmation pair. The user sees the close prompt rather than a resolve prompt.

**SUGGEST_RESOLVE pivot for CLOSED (symmetric):** When closure readiness returns `SUGGEST_RESOLVE` (case has a qualifying `causal_absence` — the root cause is confirmed eliminated), both the UI-dropdown path and the LLM-emit path pivot the pending proposal to RESOLVED and present the resolve confirmation pair. Closing a resolution-grade case would discard the resolution attribution; the pivot reconciles loose user terminology ("close" vs "resolve") against actual case content. This is the close-side counterpart of the RESOLVED → SUGGEST_CLOSE pivot above — together they form a symmetric strategy: a thin case requested as resolved pivots to close; a rich case requested as closed pivots to resolve.

**Confirm-time resolve-preservation (INV-37, #656 P3.4).** The two pivots above fire at *proposal* time. But a qualifying `causal_absence` can land *after* a close was proposed (the fix is confirmed effective between the close proposal and its confirmation), so the SUGGEST_RESOLVE pivot is also enforced at *confirm* time: `confirm_pending_transition` re-runs `assess_closure_readiness` at the single execution chokepoint immediately before a CLOSE commits, and on `SUGGEST_RESOLVE` replaces the pending close with a RESOLVED proposal instead of closing (returning `False` so the caller re-presents the resolve confirmation). The governing principle is "resolve = close *with* resolution; close = close *without* resolution" — it is **always safe to resolve a case that can be resolved**, whichever terminal the user asked for, and users have no incentive to close a case that qualifies for resolution. This makes "a resolvable case is never terminally recorded as closed-unresolved" a hard invariant at the terminal boundary, not only a proposal-time nicety. Scoped to `INVESTIGATING` (RESOLVED is not a valid edge from INQUIRY). There is no symmetric counterpart in the other direction — every case is closable, so closing is always a valid disposition and needs no readiness gate (see INV-37).

**`disposition_eligibility` — denormalized read view for UI affordance gating.** The two pivots above kick in at *action time* (when the user has already requested a transition). The complementary preventive measure is to gate the affordances themselves: hide Resolve when the case isn't resolution-grade, and warn on Close when closing would silently discard a documented resolution. The per-case answer is computed by `derive_disposition_eligibility(case)` in `terminal_transitions.py` and persisted to the `cases.disposition_eligibility` column as `{"resolved": <verdict>, "closed": <verdict>}` where each verdict is one of:

- `ready` — disposition is appropriate; render enabled with the default confirm UX.
- `needs_info` — disposition is allowed but the case is partial; user must ADD information before transitioning. Currently only the Resolve side surfaces this.
- `suggests_alternative` — disposition is allowed but the system recommends the OTHER disposition for this case (e.g., resolution-grade case clicked-to-close). User is asked to RE-DIRECT, not to add data. Currently only the Close side surfaces this.
- `not_eligible` — disposition is not available; hide the affordance entirely.

`needs_info` and `suggests_alternative` are kept as distinct values rather than overloading one label, because they drive different UX patterns (add-data vs reconsider-action). The column is maintained at the **single chokepoint `CaseRepository.save()`** (pattern P3) — every save calls the derive helper and rewrites the column, so the value can never drift from current case content without per-mutation-site update burden. The UI adapter passes the column through to all three `CaseUIResponse_*` variants; the frontend renders the dropdown menu against `disposition_eligibility`, not just `valid_next_states`. Distinction: `valid_next_states` answers *which actions the user may select*; `disposition_eligibility` answers *which of those make sense given current content*. (Neither is the legality graph — that is `LEGAL_TRANSITIONS`, §1.3.)

**needs_info flag for RESOLVED:** When resolution readiness returns `NEEDS_INFO`, the system stores the pending transition with `needs_info=True`. This remembers the user's intent to resolve. On subsequent turns, the system re-evaluates readiness via `assess_resolution_readiness()`:

- **READY** → clears `needs_info`, appends the confirmation prompt to the LLM response
- **Still not ready** → cancels pending transition, suggests Close instead (no re-ask loop — the user was already asked once and couldn't provide the info). The resulting CLOSE proposal is terminal-clean: the root-cause analysis and full history are preserved.

**Gate prose composes; gate suggestions replace.** On every gate-override turn
(needs-info first pass, the READY/suggest-Close re-evaluations above, RCA-infeasible
closure), the engine's canned gate message is **appended below the LLM's
`agent_response`** via `_prose_with_gate_notice` — never substituted for it. The LLM's
`state_updates` on such turns are applied and persisted, so replacing the prose made the
transcript contradict the case record: in #656 turns 10-11 the model's configmap
analyses created hypotheses and solutions while the user saw only the canned resolution
ask ("did you see anything wrong?" — it had, twice, invisibly). Follow-up *suggestions*
on gate turns remain engine-owned **replacements** — that is the separate
suggestion-ownership decision (#428's "augment" was reverted by #430) and is
deliberately unchanged.

> **Loop-bound (the engine's same-turn offer stands).** The "suggests Close instead" pivot above is produced by the handshake block early in `_check_automatic_transitions`. But the user typically re-confirms ("yes, it's resolved") and the LLM dutifully re-emits `proposed_transition=resolved` on the **same** turn — and the later LLM-proposal block in the same method calls `propose_transition`, which **replaces `pending_transition` wholesale**, clobbering the CLOSE pivot. Unguarded, that re-arms RESOLVED+`needs_info` every turn → the gate loops to max_turns ([project-resolution-gate-stuck-loop]; Run 36). The guard is the general same-turn rule (INV-43, #1885): when an engine opener has left an offer standing this turn (`metadata["transition_proposed_this_turn"]` with a `pending_transition`), the LLM's same-turn `proposed_transition` is **ignored** so it cannot overwrite it. This is what makes "no re-ask loop" actually hold — independent of *why* the case is in resolution `NEEDS_INFO` (instant/index resolution, mitigation-first, or any desync that leaves 0 `Solution` records).
>
> **Gate strictness — absence-driven.** `assess_resolution_readiness` gates RESOLVED on a **qualifying** `causal_absence_evidence` row (`_has_causal_absence` → `cause_assurance.resolution_confirmation_rows`, INV-30): non-engine-authored (the engine only mints absence rows as M6 failed-fix *dis*confirmations), not itself a failed-fix disconfirmation (REFUTES-linked, on either belief axis, to the cause the engine marked disconfirmed — a sibling-scoped REFUTES is proof-by-exclusion and does not disqualify), and at-or-after the latest engine-known failed-fix disconfirmation — the same metadata bar the RESOLVED confirm-stamp's candidate filter uses. The root cause is confirmed *eliminated*, which is the only failure-proof, non-circular "the fix worked" signal. That row alone is sufficient — a separate `Solution` record is for documentation quality (and the higher runbook bar), not a resolution gate; an out-of-band fix the user reports verbally yields `causal_absence_evidence` (`source_type=user_description`) with no `solutions_to_add` and still resolves. A case that never confirms the cause is gone — stabilized, deferred, or symptom-only relief (`symptom_absence_evidence`) — converges to CLOSE instead. (This is the settled end-state of the [project-resolution-gate-stuck-loop] sequencing: the success flow now emits absence evidence on confirmed resolution and the gate keys on it. The earlier gate, which required a `Solution` record, produced the documented stuck-loop — it refused a clear "yes, it's resolved" and kept demanding a "documented solution" the user did not have, then closed.)

**Pending transition confirmation — deterministic answers, escape lane for everything
else:** When a `pending_transition` exists (not `needs_info`), the user's response is
classified before any LLM call:

Every pending proposal is terminal (RESOLVED or CLOSED), so consent is read
narrowly (#1783, ruling (a), 2026-09-29). One function,
`transition_consent.pending_gate_verdict`, reads the text and the turn's intent
together, first match wins:

- **A click** (the Yes card's `confirmation` intent, sent by the client rather than
  minted) → execute the transition; the Not-yet card → decline, as below. A card's
  intent names the offer it presents (`proposal_id`, the pending proposal's
  `proposed_at`), and a confirmation click
  executes or declines only when that is the offer standing when it arrives (#1812).
  Checked before any verdict is read: a click naming another offer, or none,
  **executes nothing, withdraws nothing and records nothing**, and is answered with
  "That button was for an earlier offer that's no longer open." and the standing
  offer's card again (`_refuse_offer_click`). The status pick names its target
  state, not an offer: a re-pick of the pending target is re-asked, below
- **Bare yes** → execute transition. The WHOLE reply must be one consent token
  valid for the proposal's target. Exactly, a bare reply is the token's words,
  with any whitespace, any listed positive decoration (emoji, Slack shortcode or
  emoticon) before, between or after the token's words, any emoji modifier (the
  presentation selectors U+FE0E and U+FE0F, the skin tones) on the emoji or symbol
  it follows, and only `.` `!` `,` trailing (`yes`, `ok!`, `lgtm 👍`, `👍🏽 ok`,
  `yes ✔︎`, `ok :+1:`). A modifier after a letter, a digit or a space, or at the
  start, modifies nothing anyone sees, so it stays and the reply is not bare
  (`ok🏽`, `yes` + U+FE0E; #1840 review). A decoration inside a word splits
  it (`o👍k` is not `ok`), and curly apostrophes read as straight
  (`that’s right`). `close it`
  consents only to a CLOSE, and `resolve it` / `mark (it) as resolved` only to a
  RESOLVE. An invisible character or a wrapping mark is not a decoration, so
  `yes` carrying U+200B, `**yes**` and `"yes"` are not bare and are re-asked
  (#1840; #1783's corpus). A bare token the intent resolver minted a confirmation
  from executes the same way; a minted *decline* on a bare consent token disagrees
  with its text, and is re-asked
- **Consent-shaped but not bare, and not substantive per `is_substantive_reply`**
  (the set the gate used to execute on: "ok go ahead", "ok, don't close it yet",
  "yes please close it", "ok 👎", "close it" on a pending RESOLVE), any reply
  whose text and minted intent disagree, a minted confirmation on text that is
  not a bare token ("that works"), and a **status-dropdown re-pick of the pending
  target** (#1838: the pick names a state, not the offer, so a double submit, a
  client retry or a second tab never executes it) → **re-ask**: re-present the
  confirmation with DECIDE suggestions (clickable Yes/No with intent metadata), and the line
  "To confirm, click **Yes** or reply with the single word yes." (#1814)
- **Bare no** — the WHOLE reply is one decline token (`no`, `nope`, `not yet`,
  `wait`, `cancel`, `don't`, `not ready`, `hold on`, `stop`) with the same
  decorations and trailing punctuation as a bare yes (#1813); or the Not-yet click;
  or a minted decline on question-free text — below the substantive bound, no
  upload → cancel transition, acknowledge deterministically. "no problem, go
  ahead", "no worries" and "no, not yet" are not declines (they are re-asked, or
  take the escape lane when substantive), and "note…"/"stopped…" do not read as
  "no"/"stop"
- **Decline carrying substance or an upload** (a Not-yet click, whose payload is
  over the bound; a minted decline on long question-free text; or a decline sent
  with a file) → cancel transition, then process the message as a normal turn so
  its content is not lost. A typed reply that opens with a decline token and says
  more is not a decline but a non-answer, below; so is a minted decline on a
  question (`is_question`; #1840), so a question
  is never recorded as a refusal
- **Short (≤40 characters) question-free non-answer** ("hmm maybe") and **blank
  input** (whitespace-only slips past the route's empty-payload guard) → re-ask,
  as above
- **A turn carrying an upload, or a non-answer over 40 characters or carrying a
  question** (`is_question`: a question mark in the scripts `QUESTION_MARKS`
  lists, from ASCII and fullwidth to Arabic, Greek, Armenian and Ethiopic, the
  question emoji, the interrobang and the double marks, or Slack's `:question:`,
  `:grey_question:` and `:interrobang:`; #1840) → the message is *not an answer
  to the gate*: the proposal is **withdrawn** (`cancel_pending_transition`) and
  the message processed as a normal investigation turn. It is recorded as a
  refusal only when the text is such a non-answer without a question that does
  not open with a consent token read loosely (`opens_with_consent_loosely`;
  #1808, #1840). **Two readers, two strengths** (#1840 review): whether the gate
  TAKES a turn is read strictly (`_consent_prefix`), because a turn it takes
  never reaches the LLM, and read loosely "`ok` is false in the /health response
  from node-3 again" was swallowed. Only whether a withdrawal is RECORDED is read
  loosely (`_shape_text`): invisible characters (Unicode Cf, and U+034F) and
  wrapping marks (`*` and `_` where they wrap a word, never inside an
  identifier; the straight, curly and low-9 double quotes and the guillemets;
  an apostrophe not inside a word) read as
  spaces, while a backtick (it quotes a word) and strikethrough's `~` (it
  negates) do not. So "*Yes*, …", "_Yes_, …" and a "Yes" behind an invisible
  character are processed and never recorded, as is "Yes, go ahead and close
  it. We verified …": consent in a sentence, not a deflection, so the offer may
  come back, and so may "ok but we need to wait for the weekend soak first" (the
  cost the ruling accepts). "`ok` is false in /health …" and "ok_status flag
  never flipped …" are processed and recorded. An upload alone records
  nothing. The engine can always re-propose later from fresher state.

So the gate's consumption rule is exact: without an LLM turn it answers only the
re-asks above (and a bare decline), and it **never** consumes a turn carrying an
upload, nor a non-answer over 40 characters or carrying a question. A re-ask repeats
**every time** it is earned and **never records a refusal or withdraws the
proposal**: a terminal proposal must not turn into a decline because the user
typed more than one word. The one-re-present cap #656 added (a second non-answer
withdrew the proposal and recorded it as a refusal) is gone.

The escape lane is load-bearing for NO-COLLAPSE: the earlier "re-present on anything
else" rule held the gate against substantive typed input indefinitely — no LLM call, no
state change, identical canned reply every turn (#656, `case_5db5417fe445` turns 12-13:
"I refuse to do that. you must continue to investigate" and "what is the root cause?"
were both swallowed). And the bare-confirmation rule is its confirm-side mirror: without
it, a confirm-prefixed substantive message ("ok but what is the root cause?") did not
merely swallow the input — it *executed* the terminal transition on it. The cheap
re-present answers only the replies listed above; a turn carrying an upload, and a
non-answer over 40 characters or carrying a question, are never consumed by the gate. The short-message re-present also preserves
the original motivation of the deterministic path — not sending a bare "hmm" through
the LLM tool loop. The IntentResolver's LLM classifier tier can map typed text to the
Yes suggestion: the adoption guard (#721) drops such a mint from substantive text, and
a mint that survives it executes only when its text is itself a bare consent token
(#1783), so the text, not the classifier, carries the guarantee. At Gate 1 the guard
drops a mint on any text that is not bare (#1794). A mint is not a click, so the
offer key it carries from the matched card is never read.

**Repeated status_transition intent:** If a user picks the same dropdown option again
after the transition was proposed, it is **not** consent (#1838, ruling (b)). The pick
names a state, not the standing offer, and a double submit, a client retry or a second
tab sends it as surely as a deliberate second pick. So the gate re-shows the standing
offer's card and records nothing; only the card's click or a bare typed consent
executes it. The re-ask is forced rather than read from the pick's text, which is over
the 40-character bound and would otherwise escape and be recorded as a refusal. A
`status_transition` the intent resolver MINTED from typed text is read by its text,
as any mint is.

**No LLM-written card stands in for consent (#1839, ruling (a)).** An LLM-authored
DECIDE card carries no intent, so its click arrives as its payload text alone, which
the server cannot tell from typing: an old "Proceed" card would answer whatever offer
stands when it is clicked. `_flatten_follow_ups`, the one site every LLM follow-up
passes through on the normal and the terminal path, never ships a DECIDE card whose
payload the gate reads as a bare reply (`is_bare_gate_reply`: a bare consent to
either terminal target, so every bare Gate-1 consent too, or a bare decline), as
written or as a client sends it (`card_reads_as_bare_reply`: invisible characters
removed and whitespace trimmed, since JavaScript's `trim()` strips U+FEFF). The card
sends its label instead. It is dropped when its label reads as a bare reply too, or
when the label, put through the payload's own safety nets, would not stay a DECIDE
payload (a command, a false results handoff; `_label_stays_decide`), counted on
`faultmaven_llm_decide_card_bare_payload_total{action}`. Engine-authored cards carry
an intent naming their offer and are outside the rule.

**Contradicting status_transition intent:** If a user clicks a *different* dropdown
option while a pending transition exists (e.g., "Close" is pending but user clicks
"Investigating"), the pending transition is cancelled and the new intent is processed
normally. This handles the case where the user changes their mind after requesting
a transition.

##### KB-Resolution Path (Milestone-Collapse Variant)

When a runbook from the KB applies cleanly to the case, INVESTIGATING's **state authoring** collapses into a single turn: all required state (`RootCauseConclusion`, `Solution`, gate milestones) is populated in one turn from the matched runbook Cause rather than across many investigation turns. This is **not** a separate transition edge, and it does **not** collapse the disposition handshake — the RESOLVED transition still requires the explicit user confirmation turn, exactly like the multi-turn path (#722). The user's "it worked" message is the *verification claim* (trusted as the truth of the resolution — `solution_verified` territory); it is not *consent* to the irreversible terminal transition. `KnowledgeResolution.user_confirmation` quotes it as an attribution/audit record, not as consent. *(Rejected alternative: the v3 same-turn confirm collapse, which executed RESOLVED in the proposal turn — removed because an LLM misread of "it worked" could irreversibly resolve a case with no explicit confirm turn, breaching the User-Agent Handshake invariant.)*

**Signal**: The LLM emits `knowledge_resolution` in `state_updates` when the user confirms that a runbook fix proposed in an earlier turn resolved their issue ("That fixed it", "It worked", "Yes, resolved").

```python
class KnowledgeResolution(BaseModel):
    """User-confirmed resolution via knowledge base match.

    Emitted by the LLM when the user confirms that a runbook fix proposed
    in an earlier turn resolved their issue. Triggers the milestone
    collapse: the engine populates RootCauseConclusion, creates Solution,
    and sets gate milestones from the attributed Cause's content in this
    one turn, then holds the RESOLVED proposal pending the standard
    confirmation turn. user_confirmation is an audit record, not consent.
    """
    match_id: str                # ID of the matched runbook
    match_type: str              # "runbook" | "past_case" | "documentation"
    solution_applied: str        # What the user actually did
    user_confirmation: str       # User's confirmation message
```

**Engine behavior on `knowledge_resolution`** (during INVESTIGATING turn processing):

1. **Attribute the active Cause.** The agent judges which `### Cause <X>` from the matched runbook applies by reasoning over the retrieved runbook content against current case state. If exactly one Cause fits, proceed. If several fit, defer the collapse: agent asks for a disambiguating Diagnostic Step finding before completing the transition. If none fits, the fallback Cause is selected.
2. **Populate `RootCauseConclusion`** by direct field copy from the attributed Cause's ChromaDB metadata (no LLM extraction call):
   - `root_cause` ← Cause `Statement` (≤300 chars)
   - `mechanism` ← Cause `Mechanism` (≤800 chars)
   - `evidence_basis` ← runbook ID + user's confirmation message reference
3. **Create `Solution`** from the attributed Cause's blocks:
   - `immediate_action` ← Cause `Mitigation` (with risk + duration metadata)
   - `longterm_fix` ← Cause `Resolution`
4. **Set gate milestones** in the standard order: `solution_proposed=True` (engine-derived from the standing SolutionToAdd), `solution_accepted=True` (LLM stage-gate signal). `solution_verified` is NOT set here — it is set by `_execute_resolved_transition` when the user confirms the disposition. There is no LLM-settable cause signal (INV-35): the engine derives `cause_state=IDENTIFIED` from the validated, uncontested chain root grounded by the runbook attribution.
5. **Fire the standard handshake.** With milestone state populated, the LLM's response on this same turn emits `ProposedTransition` to RESOLVED. The engine holds it pending and presents the canonical confirm/decline pair ("One click to confirm" is literal); the transition executes only after the user confirms on the next turn.

**Why the disposition does not collapse.** Two distinct things must never be conflated: (a) the *truth* of the user's resolution claim — FaultMaven trusts "it worked" by design, which is why the milestone state can be authored in one turn; and (b) *consent to the irreversible lifecycle action* — RESOLVED has no reopen path, so consent must be an explicit, bare confirmation on a turn where the user can see what they are confirming (INV-26). An LLM misread of "it worked" costs one wasted confirmation prompt under the handshake; under a same-turn confirm collapse it would cost an irreversible wrong terminal state.

**Why this is not a fast-track.** Earlier designs allowed an `INQUIRY → RESOLVED` edge that bypassed INVESTIGATING entirely, producing terminal cases with empty `RootCauseConclusion` / `Solution` / `evidence` records — the Resolution Summary report had nothing to render. The unified path eliminates that edge: every RESOLVED case flows through INVESTIGATING and produces complete bookkeeping. KB-driven cases are simply the variant where INVESTIGATING completes in 1–2 turns because the cause and fix come pre-packaged from a runbook Cause subsection.

**Authoring requirements upstream.** The milestone collapse depends on runbook Causes carrying structured `Statement`, `Mechanism`, `Mitigation`, `Resolution`, and `Verification` fields — see [runbook-content-architecture.md §3](../knowledge-and-ai/runbook-content-architecture.md#3-standardized-runbook-template). Runbooks not following the v3 template cannot drive the milestone collapse; cases retrieving them fall back to standard multi-turn investigation.

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
    # inquiry_only | closed_false_alarm | solution_deferred | closed_rca_infeasible | mitigation_sufficient | closed_restatement_held | closed_insufficient_evidence
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

There are **two** graphs, answering different questions. They are not copies of each other and must not be pinned equal.

**`LEGAL_TRANSITIONS`** (`modules/case/domain/models/lifecycle.py`) — every edge the state machine permits. `is_valid_action()` reads it directly, as the Pydantic model_validator on every `CaseAction` instantiation, and the INV-22 guard validates LLM-emitted `proposed_transition` targets against it.

```python
LEGAL_TRANSITIONS = {
    CaseState.INQUIRY: (
        CaseState.INVESTIGATING,   # Gate 1 performs this — see below
        CaseState.CLOSED,          # Inquiry-only, no investigation
    ),
    CaseState.INVESTIGATING: (
        CaseState.RESOLVED,        # Solution verified (terminal) — includes the KB-resolution milestone-collapse variant
        CaseState.CLOSED,          # Abandoned (terminal)
    ),
    CaseState.RESOLVED: (),        # DISPOSITION - no further case actions
    CaseState.CLOSED: (),          # DISPOSITION - no further case actions
}
```

**`USER_SELECTABLE_ACTIONS`** (`case_action_manager.py`) — what a user may pick from the status menu, and the source for `valid_next_states`. A strict **subset**:

```python
USER_SELECTABLE_ACTIONS = {
    CaseState.INQUIRY: (CaseState.CLOSED,),
    CaseState.INVESTIGATING: (CaseState.CLOSED,),
    CaseState.RESOLVED: (),
    CaseState.CLOSED: (),
}
```

They differ on two edges, and both are earned rather than picked. **INQUIRY → INVESTIGATING** is earned by a problem statement the user has confirmed — which Gate 1 performs and the DB CHECK `cases_description_required_for_investigation` makes structural — so a menu cannot honour it on demand. Requesting it is refused with a 422. **INVESTIGATING → RESOLVED** is earned by the readiness bar (a qualifying `causal_absence_evidence` row); the engine or the model offers it and the user confirms the offer (INV-43, and *Why RESOLVED left the status menu* above). Every entry that remains in the menu is a *disposition*: a user decision carrying information the engine cannot derive.

Both are frozen (`MappingProxyType` over tuples) so an importer cannot widen the gate at runtime. See the INV-04 notes in [investigation-invariants.md](./investigation-invariants.md) for the consolidation history.

There is no `INQUIRY → RESOLVED` edge. KB-driven cases route through INVESTIGATING via the KB-resolution milestone collapse documented under [INVESTIGATING → RESOLVED → KB-Resolution Path](#kb-resolution-path-milestone-collapse-variant) — confirming problem understanding is mandatory before any solution is proposed, including for runbook-matched cases.

**Case Action Diagram**:

```text
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
       │                             │ Mitigating         │
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

The full invariant registry — every load-bearing lifecycle rule with its enforcement-tier classification, pinning tests, and drift notes — lives in its own reference document: **[Investigation Invariant Enforcement Matrix](./investigation-invariants.md)**.

It was extracted from this section so the matrix can be maintained and audited independently. The invariants index the rules defined throughout §1–§4 of this document; the *Source* column there points back here by section number.

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
    #    a. Intent-based: DECIDE suggestion clicks carry
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
#  INVESTIGATING; see LEGAL_TRANSITIONS in §1.3.)


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
        closure_reason=reason,  # engine-derived; see derive_closure_reason()
    )
    # Note: a case stabilized by a verified mitigation closes as "mitigation_sufficient"
    # (the former "mitigation_sufficient" reason was folded in). The documented
    # mitigation is preserved on the closed case.
    case.action_history.append(CaseAction(
        from_state=CaseState.INVESTIGATING,
        to_state=CaseState.CLOSED,
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
        from_state=CaseState.INQUIRY,
        to_state=CaseState.CLOSED,
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
| `solution_state` / `solution_feasible` | Assessment (engine-derived / LLM-settable) | End of each INVESTIGATING turn / LLM output | `solution_state` mirrors the derived `solution_proposed` (`SELECTED` while it holds, else `UNKNOWN`); `solution_feasible` defaults NOW, LLM sets DEFERRED. |
| `solution_proposed` | Progress indicator (engine-derived) | End of each INVESTIGATING turn | Derived at the assessment recompute (INV-32): True iff a LIVE SOLUTION ProposedAction stands (`pending`/`accepted`) or the gate ladder advanced (`solution_accepted`/`solution_verified`, forward-only facts). A new SOLUTION offer supersedes prior pending ones (`reproposal`); a pending offer whose established-cause license falls — M6 demotion, conclusion retraction, MECE hold — is withdrawn (`license_lost`) with `system_feedback` to the LLM. Not a write-once latch. |
| `mitigation_accepted` | Mitigation gate signal | LLM structured output | User acknowledges executing the proposed mitigation → materializes `mitigation.accepted` |
| `mitigation_verified` | Mitigation gate signal | LLM structured output | User confirms the mitigation stabilized the situation → materializes `mitigation.verified` + `completed_at_turn` |
| `solution_accepted` | Gate milestone | LLM structured output | User acknowledges executing proposed solution |
| `solution_verified` | Gate milestone | After user confirms fix | User confirms problem resolved (User-Agent Handshake) |
| Disposition action | — | End of turn | After all other processing |

**Gate signals vs Progress indicators vs Assessment variables**:

- **Gate signals** (`mitigation_accepted`, `mitigation_verified`, `solution_accepted`, `solution_verified`): Drive the derived stage label + resolution handshake. Set by the LLM in structured output when it detects user compliance with a ProposedAction (Framework §4.1). The mitigation pair (`mitigation_accepted`/`mitigation_verified`) materializes into the single `progress.mitigation` record rather than booleans.
- **Progress indicators** (`symptom_verified`, `solution_proposed`): Provide LLM context and analytics. Do NOT drive stage transitions. `symptom_verified` is LLM-set; `solution_proposed` is engine-derived from live SOLUTION offers (INV-32) and can flip back to False when the standing offer leaves liveness.
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

### 1.4.1 Verifying the problem statement: three outcomes

Gate 1 confirms a problem *statement*; it does not confirm the problem. Checking
the statement against the evidence has three outcomes, and
`InvestigationProgress.problem_status` records which one the case is in. Its one
writer is `core/investigation/problem_status.py` (INV-44); `symptom_verified` is
its derived boolean view.

| Status | Meaning | Cause work | Close | Resolve |
|---|---|---|---|---|
| `unverified` | confirmed, not yet evidenced (Zone 1) | refused | yes | no |
| `verified` | the evidence shows the stated symptom | accepted | yes | per readiness |
| `revision_pending` | the problem is real, the statement inaccurate; a revision awaits the user | **staged** | the user's own close cancels the revision | no |
| `invalidated` | the reported symptom was never present: a false alarm | refused | engine-offered, `closed_false_alarm` | never |

**(a) Verified.** The LLM's justified `symptom_verified` claim, backed by cited
symptom evidence (the step-2b review), moves `unverified → verified`. Cause work
is accepted from then on, and the gates read the status the turn ENDS with, so
one turn can verify, form hypotheses, emit the chain and identify the cause.

**(b) Inaccurate statement.** The LLM sends
`verification_updates.revised_problem_statement` with the symptom evidence that
shows the problem as revised. The guard (`revision_refusal`) requires that
evidence and a basis, refuses a revision that restates the current statement or
a cause (the statement describes what is OBSERVED, never why), refuses wording
the user already declined, and refuses once the statement has been acted on — a
cause identified and uncontested, or a mitigation or fix *verified* (an accepted
fix that failed does not bar it: that is when a mis-statement surfaces). The
case moves to `revision_pending`, and the engine presents the revision on every
pending turn with a confirm/clarify pair keyed on its wording (INV-45). Cause
work arriving while it waits is staged on the revision with the evidence ids its
refs resolve against. On a click or a bare "yes" — read by the disposition
gate's own grammar, before the LLM call — the revision commits: description,
`symptom_statement` and the causal graph's PROBLEM node (re-texted in place, so
chains keep their anchor) change together, open symptom needs are superseded,
the KB pre-fetch re-runs, the status becomes `verified`, and
the staged work replays through the normal apply path. Any offer the replay
makes carries its same-turn guard and card into the confirmation turn, so the
"yes" that confirmed the statement never executes it. A decline returns the
case to where it was, records the wording, and tells the model which staged
work it discarded.

**(c) False alarm.** The LLM sends `verification_updates.problem_invalidated`
with `symptom_absence_evidence` from where and when the symptom was reported.
"Not happening right now" is not a false alarm, and neither is missing data. The
guard refuses it once the problem was acted on, or when a cause was confirmed
eliminated (`causal_absence`), which proves the problem existed. The case moves
to `invalidated`, the engine offers the close once (INV-46), and resolution is
not eligible. Declined, the case holds: no hypotheses, updates, chains,
solutions or mitigations, and no mitigation or solution signal is accepted;
housekeeping, repair patterns and the stall counter pause. The decline is a
fact about the finding and is recorded on it
(`ProblemInvalidation.close_declined_at_turn`, written by
`record_false_alarm_close_declined`), whoever opened the close: the engine, the
model, or the user's own status-menu pick, since each derives the same
`closed_false_alarm` reason from the same finding (#1889). A finding is
replaced only after it is cleared, so the record lasts exactly as long as the
premise it is about. While it stands, a transition the model proposes is
refused whatever its target (a `resolved` would pivot to this close), the model
is told the user declined at turn N and may propose a close only when the user
directs it, and the turn's follow-ups gain one **Close as false alarm** card:
the status menu's own `status_transition` intent, appended to the model's
suggestions, never replacing them. A bare "no" is answered with what the hold
is and what moves it, and the same card; the prompt's false-alarm block, which
renders on every stage, carries the declined line. Two exits: new
evidence of a different problem (a revision, which withdraws the engine's close
offer), or the user disputing the finding (`invalidation_withdrawn`, back to
where the problem stood before the finding — `verified` if it was, since nothing
refuted that verification). The dispute, like an edit, takes back a pending
false-alarm close whoever proposed it: the engine, the model, or the user's own
close from the status menu (`_close_on_explicit_intent`, which derives
`closed_false_alarm` on an invalidated case and carries no signature). A
revision is the exit limited to the engine's own close. On the turn the finding
is made, the engine's signed offer is the one the user answers even when the
model proposed a transition beside it (INV-43's same-turn rule, #1885): replaced
by the model's, it lost the signature, so a decline recorded nothing. (On a chat
turn section 0b withdraws a pending close before the model is called, so the
revision gate's refusal of an unsigned close is reached only where the apply
step meets the close still standing; it is the backstop there, not a chat-path
refusal.)

A `causal_absence` row is judged against the turn's own verification, after the
step-2b review and again after step 2c: on a problem not verified by then it is
recorded as `symptom_absence`.

**A user's edit.** Editing the description during an investigation goes
through the same writer (`edit_statement`), so the three stores stay aligned,
and supersedes the open symptom needs. An edit is the user's word, not
evidence: it never verifies, and a verified problem stays verified (the user
sharpened wording the evidence already showed). On a false alarm the finding
was about the old wording, so the edit clears it, returns the problem to
`unverified` and withdraws any pending false-alarm close, whoever proposed it. An empty edit, one longer
than the PROBLEM node holds (500 characters), or one made while a revision
waits is refused.

Every change is recorded in `problem_verification.statement_history`, opening
with the statement Gate 1 confirmed; the resolution and closure summaries show
"Originally reported as" when the statement was revised. The case read
(`GET /cases/{id}/ui`) carries the same `problem_verification` in every state
from INVESTIGATING on (contract 11.3.0), so a resolved or closed case still
says where its statement stood: `invalidated` with the finding on a false
alarm, and `original_problem_statement` once a revision or an edit changed
it. A case closed from INQUIRY confirmed no statement and carries none.

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
| INQUIRY       | Closed |
| INVESTIGATING | Closed |
| RESOLVED      | *(disabled - disposition)* |
| CLOSED        | *(disabled - disposition)* |

**API Support**: No direct API - uses existing query submission endpoint

---

#### 1.5.2 Request Flow

**Step 1: User Initiates Request**

User selects new status from dropdown → Frontend shows confirmation modal:

```text
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
    "from_state": "investigating",
    "to_state": "closed",
    "user_confirmed": true
  }'
```

**API Endpoint**: `POST /api/v1/cases/{case_id}/turns`

- **Purpose**: Submit a turn — query text, attachments, and/or structured intent.
- **Auth**: Requires Bearer token + X-Session-Id.
- **Returns**: `TurnResponse` with the agent's reply.

The structured-intent route was introduced in the 2026-02-09 bug fix
(`milestone_engine/engine.py` `MilestoneEngine._process_turn_impl`, `intent_type == "status_transition"`)
when `intent_type="status_transition"` was added as an explicit dispatch path. Earlier
versions used a plain-text system-generated message ("[User requested to change case
status to X]") submitted to `/queries`. That mechanism is deprecated for dropdown flows;
the structured payload is unambiguous and skips the LLM's intent classification.

---

##### Step 3: Engine Validates and Responds

The engine's `status_transition` handler (in `_process_turn_impl`) branches by target
status. Each branch honors the User-Agent Handshake — none of them auto-execute.

**→ INVESTIGATING (from INQUIRY)**: **refused.** INVESTIGATING is not a
user-selectable case action — it is legal, and Gate 1 performs it, but it is
earned by a problem statement the user has confirmed, which the DB CHECK
`cases_description_required_for_investigation` makes structural. A request
cannot make that true, so the engine raises rather than pretending.

The menu no longer offers it (`USER_SELECTABLE_ACTIONS`); the refusal closes
the same door to older clients and direct API callers. This branch previously
accepted the request and fell through to the LLM on an injected synthetic
message ("I want to start a formal investigation to find the root cause") —
which read to the model as established problem-solving intent and pulled a
problem statement out of a case that had none, while the reply correctly said
none could be stated.

Users still ask for an investigation the way §1.2's natural flow always had
them ask: by saying so, or by the agent proposing one. Gate 1 performs the edge.

**→ CLOSED (from INQUIRY or INVESTIGATING)**: the engine calls `propose_transition`
directly, returns a closure-readiness summary plus the canonical Yes/No confirmation
pair. The transition fires only when the user confirms on the next turn.

**→ RESOLVED (from INVESTIGATING)**: **refused**, like INVESTIGATING above and for
the same kind of reason: RESOLVED is earned, not picked. The readiness bar (a
qualifying `causal_absence_evidence` row) earns it, the engine or the model offers it
(INV-43), and the user confirms that offer. `earned_edge_refusal`, derived from
`USER_SELECTABLE_ACTIONS`, refuses the pick with a 422 at the service boundary and
again in the engine before any state is touched; see *Why RESOLVED left the status
menu*. (This branch used to run the three-way READY / SUGGEST_CLOSE / NEEDS_INFO
readiness check on the pick; the same readiness check now decides whether the offer
is made.)

Either accepted branch leaves the case in INVESTIGATING this turn with the proposal
held pending. The user has the next-turn confirmation step to accept, decline, or
refine.

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

```text
"Understood. Transitioning to formal investigation now.

Based on our discussion, the problem is:
'Database queries timing out in production, affecting 50% of requests
since 3 hours ago'

Let me start by checking that against the evidence. Can you share the
timeout errors from the application logs?"
```

The next step asks for the evidence that verifies the symptom. It does not ask
for the scope: which services and users are affected are facts the agent picks
up from the evidence as they appear, and it never waits on them (#1880).

**Backend updates**:

- `case.state = CaseState.INVESTIGATING`
- `case.problem_verification = ProblemVerification(symptom_statement=...)`
- `case.action_history.append(CaseAction(...))`

---

#### 1.5.3 Confirmation UI Pattern

**Visual Design** (in chat conversation):

```text
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

**Confirmation actions are rendered as DECIDE suggestions** with `intent` metadata:

```python
# Resolution confirmation suggestions carry intent for deterministic routing
{
    "label": "Yes, mark as resolved",
    "action_type": "DECIDE",
    "payload": "Yes, the issue is resolved. Please mark this case as resolved.",
    "intent": {
        "type": "confirmation",
        "confirmation_value": True,
        # The offer this card presents: the pending proposal's proposed_at
        # (a Gate-1 card carries gate1_offer_key(statement)).
        "proposal_id": "2026-09-30T11:57:20.123456+00:00",
    },
}
```

**Click flow**: Frontend sends `payload` as query text AND `intent` as `QueryIntent` metadata,
forwarded verbatim. This routes through `IntentType.CONFIRMATION` → deterministic
`pending_transition` handling, bypassing the tool loop and pattern matching entirely. The engine
executes the click only when its `proposal_id` names the offer standing when it arrives; any
other click is answered with that offer again and executes nothing (#1812).

**Typed responses** (user types instead of clicking) fall back to `confirmation_token_class()` (consent is "not `None`"),
read through `pending_gate_verdict()`: only a reply that is, as a whole, one bare consent token
for the proposal's target executes (#1783). A consent-shaped reply that says more is re-asked with these cards;
what the gate consumes and what it sends on as a normal turn is stated under *Pending transition confirmation* above.

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
- **SUGGEST_CLOSE** — No root cause, no solution, no evidence. The LLM-emit branch pivots the pending proposal to CLOSED and emit the close confirmation pair. If the issue was actually fixed, the user can provide root cause and solution to reopen the resolve path.

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
- Archive the case from Dashboard when you are done"""
# The engine derives closure_reason (derive_closure_reason); the user is not asked for one.
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

```text
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
- The template instructs the LLM: answer questions using existing case data, do not propose new actions or data requests
- Agent has read access to: messages, evidence, hypotheses, solutions, action_history, auto-generated summary
- Agent can NOT: accept new evidence, update milestones, propose transitions
- Agent CAN: explain what happened, clarify evidence, interpret timeline, extract lessons learned

**Three interaction scenarios**:

1. **User asks to regenerate the report** → Pattern matching triggers `_handle_report_regeneration`, which renders the summary with `ReportGenerationService.render_reports` **synchronously** (deterministic, from case fields — no LLM call) and embeds the rendered markdown inline in the chat reply. The substance gate is re-applied for CLOSED so low-substance cases can't be regenerated into existence by clicking around. The new version (up to `MAX_REGENERATIONS`) is marked current, and its row commits in the turn's one commit (#1882): a regenerate turn that fails consumes no slot.
2. **User accepts runbook suggestion** (RESOLVED only) → Pattern matching triggers `RunbookCreator.handle_runbook_creation()`: evaluates readiness + deduplication synchronously, then kicks off `ConversionService.convert_from_case()` as a **fire-and-forget background task** that first waits for the turn's commit (a gate in the turn's `TurnCommitPlan`; a turn that fails starts no conversion, #1882). The chat reply returns immediately ("Creating your runbook draft..."), and a `role="system"` completion message is appended to the case transcript when the background task finishes (success: names the new draft; no-drafts or exception: states that nothing was saved and points at manual authoring in the Knowledge Base). That row is invisible in the copilot, which drops system messages, so the Dashboard is its only reader — see §1.7.3. The Dashboard *Knowledge Base > Drafts* tab is the persistent surface.
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

**Report regeneration**: The summary report is auto-generated at closure time and rendered inline in the closure-turn chat reply. The DECIDE *"Regenerate &lt;type&gt; summary"* affordance is the only chat-side path — free-typed paraphrases like *"give me a recap"* route to Q&A and never produce a persisted Report. Each regeneration adds a version (up to `MAX_REGENERATIONS`) and marks it current. Where the affordance appears depends on whether initial generation succeeded; see *Where it's offered* in §1.7.3 below.

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
| RESOLVED | `RESOLUTION_SUMMARY` | What the problem was, root cause, the causal map (when established over a non-trivial graph — §4.5.0), solution applied, confirming evidence, timeline, milestones reached, whether a mitigation was inserted |
| CLOSED | `CLOSURE_SUMMARY` | What the problem was, investigation state at closure, approaches attempted, closure reason, leading hypotheses with confidence, mitigation status, recommendation for next investigator (closed_insufficient_evidence and closed_restatement_held closes) |

**Generation approach**:

- Rendered deterministically from case fields (hypotheses, solutions, evidence, milestones, timestamps) by `ReportGenerationService.render_reports` — no LLM call.
- Stored as `Report` with `auto_generated=True` (distinguishes from user-requested reports), **in the turn's one commit**: the row rides the same transaction as the CLOSED/RESOLVED state it summarises (#1882), so a summary row never exists for a terminal state that did not commit, and a turn whose commit fails leaves neither.
- **Synchronous**: the closure-turn agent reply waits for the render and then embeds the rendered markdown inline. A render failure is caught and the closure still commits (owner ruling on #1882, INV-13), but the chat reply embeds a status-aware failure note (*"Resolution summary generation did not complete..."* / *"Closure summary generation did not complete..."*) and the regen affordance is offered **on the same ack-turn** for immediate retry. The "regen would be noise next to the inline summary" rationale only applies on the success path; on the failure path there is no inline summary, so offering regen alongside the failure note is the right UX. See *Where it's offered* below.
- Each regeneration adds a version (up to `MAX_REGENERATIONS`), marked current.

**Substance gate** (`should_generate_terminal_summary()` in `terminal_transitions.py`):

RESOLVED transitions always generate — a confirmed solution is meaningful content by definition. CLOSED transitions are gated on **investigation substance**: at least one of `evidence > 0`, `hypotheses > 0`, or `completed_milestones > 0`.

The gate is intentionally **substance-only**. Conversation depth (`message_count`) is *not* a signal: terminal Q&A turns inflate it, so including it would let post-closure chat flip the verdict. The three substance signals are naturally frozen in CLOSED state (the API rejects new evidence/transitions), so the gate is stable across the terminal lifetime without needing a snapshot field. The case description is also excluded — creation-time metadata, not investigation output.

**Pre-close confirmation prompt**: the confirmation prompt that asks *"are you sure?"* before a terminal transition speaks only to the irreversibility of closing — it does not mention the summary. Conditional promises ("a summary will be generated *if*…") would muddy the decision; the summary is a downstream Dashboard artifact and the only chat-side reference to it is the DECIDE regen affordance offered after closure (when applicable).

**Skip-reason surfacing**: when a closed case fails the substance gate and has no Report row, `terminal_summary_skip_reason(case)` in `terminal_transitions.py` returns a human-readable note. The case UI adapter populates the Dashboard Report tab with `status="skipped"` and this derived note. The closure-turn chat reply also embeds the skip note inline so the user gets the explanation where they are.

**Regeneration**:

- **Where it's offered**:
  - *Success path*: regen is **not** on the closure-acknowledgment turn (the freshly-generated summary is rendered inline above; a regen card alongside it would be noise). It's offered on subsequent terminal Q&A turns via `_resolved_suggestions` (RESOLVED) or `_closed_suggestions` (CLOSED, when the substance gate would PASS).
  - *Failure path*: regen **is** offered on the closure-acknowledgment turn itself — synchronous generation raised an exception, no summary was rendered inline, so the "noise" rationale doesn't apply. `_select_ack_follow_ups` in `milestone_engine/terminal_replies.py` selects the right set: minimal suggestions on success (`_resolved_ack_suggestions` / `[]`), full Q&A-turn suggestions on failure (`_resolved_suggestions` / `_closed_suggestions`). For CLOSED, failure implies the substance gate had already PASSED (otherwise generation would have been skipped, not attempted), so `_closed_suggestions` reliably returns the regen affordance.
- **Strict gating**: regeneration re-applies the same substance check. Low-substance closures can't be regenerated into existence by clicking around; the gate is one-way and consistent.
- **Free text routes to Q&A**: the regen handler is reached only via the DECIDE suggestion's precomposed payload (exact-match). Free-typed paraphrases like *"give me a recap"* or *"new summary please"* route to terminal Q&A, where the prompt instructs the agent not to produce a competing summary and instead redirect to the existing summary + regen affordance. This keeps the rule clean: typing never produces a persisted Report side effect; clicking always does.

**Runbook generation — chat-side trigger + completion notification** (RESOLVED only):

The runbook affordance on the RESOLVED ack-turn is a separate downstream artifact, not a summary. Clicking it routes via the same exact-match dispatch (`_RUNBOOK_CREATION_PATTERNS`, read by `terminal_card_action` in `milestone_engine/terminal_turns.py`, which `TerminalTurnHandler` dispatches on) to `RunbookCreator.handle_runbook_creation` (`milestone_engine/runbook_creation.py`), which runs two pre-flight gates synchronously: content readiness (`assess_runbook_readiness` — does the case have a root cause + actionable solution?) and deduplication against the published-runbook corpus (`RunbookKnowledgeBase`, scoped to the case owner — see `runbook-dedup.md`). On `NOT_READY`, the chat reply explains why and no draft is created. On `SIMILAR_FOUND` (a ≥ 0.70 best-chunk match), the turn STOPS: the candidate is named by title and score — stated as overlap, never as coverage — no draft is created, and a *"Generate a new runbook anyway"* DECIDE affordance (`_RUNBOOK_CONFIRM_PATTERNS`, also read by `terminal_card_action` → `dedup_confirmed=True`) makes the choice answerable on the next turn. On proceed (no match, or explicit confirmation, or a dedup-failure caveat — the case is runbook-worthy and only the duplicate check is uncertain), the conversion runs as a **fire-and-forget background task** (`RunbookCreator._run_runbook_conversion`); the chat reply returns immediately ("Creating your runbook draft… It will appear in the Dashboard under **Knowledge Base > Drafts** once generation finishes. You can also create and edit runbooks there directly").

**Reachability rule.** Every action or place a message names must be reachable by its reader at the moment they read it. This is the rule the whole runbook flow's copy is written against, and it is enforced by a property test over both the turn responses and the notifications (`test_runbook_completion_and_summary_failure.py`): any affordance label appearing in a turn's text must appear in that same turn's `suggested_follow_ups`.

Applied to the kickoff reply: it promises **no in-chat notification**; it names **no chat affordance** — "Generate runbook from this case" is suppressed on this very turn (`runbook_already_exists=True`), and exact-match dispatch on the DECIDE payload means the label is not typeable either — and it draws **no failure inference from the draft's absence**, since `_persist_job` runs only after the pipeline finishes and nothing is written to Drafts while the conversion is in flight. The Dashboard create/edit path (`POST /knowledge/runbooks/create` plus the Drafts editor) is what the reply offers instead: reachable independently of any turn's suggestion set, and therefore still reachable after a conversion that failed silently. The suppression is per-turn — the runbook affordance returns on subsequent terminal Q&A turns, where the idempotence guard answers a repeat click with a clean "already exists" rather than a second draft.

When the background task finishes (success, no-drafts, or exception), it appends a `role="system"` message to `case.messages` with the outcome. The append is concurrency-safe (acquires the per-case lock from `_case_locks`) and best-effort (notification-write failures are logged but never propagate). **This notification is a durable record, not a delivered message.** The copilot's conversation loader keeps only `user`/`assistant` rows and drops `system` ones, and there is no push channel for case messages, so the notification — including the failure and no-drafts variants — is invisible there both on the turn and after a reload. A failed conversion is consequently silent in the copilot today; surfacing system turns is a client-side gap tracked against the copilot. Nothing in the chat-side flow may be designed as if the copilot user reads this message.

**The Dashboard is the notification's only reader**, and the reachability rule is applied against *that* surface, not against chat. The Dashboard renders the case transcript, but it has **no suggestion-chip UI at all** and **no case-to-runbook trigger of its own** — so a notification may name neither. What a Dashboard reader can reach is the Knowledge Base: the **Drafts** tab to view a draft, and the *write a runbook from the template* form to author one by hand. The three notices are written to that budget:

| Outcome | Notice |
|---|---|
| Success | *"Your runbook draft **X** is ready. View it in the Dashboard under **Knowledge Base > Drafts**."* |
| No drafts | *"Runbook generation finished without producing a draft, so nothing was saved for this case. You can write one yourself in the Dashboard under **Knowledge Base**."* |
| Exception | *"Runbook generation failed, so no draft was created for this case. You can write one yourself in the Dashboard under **Knowledge Base**."* |

The two unhappy notices say plainly that nothing was saved — "completed" would let the reader keep waiting — and offer manual authoring, which is a weaker remedy than the conversion they were promised but the only one on their screen. Note the nav label is **"Knowledge Base"**, not "Knowledge"; copy that says the latter names a section the reader will not find.

The chat-side dispatcher — the single live case-to-runbook trigger — uses the shared `CaseConversionRequest.from_case` factory for case-data extraction, so the case-to-runbook input shape is single-sourced. See `document-to-runbook-conversion.md` for the full conversion pipeline.

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

## 2. Mitigation as an Insert

There is **one opportunistic INVESTIGATING flow** — no prospective path fork, no
merge. The former `mitigation_first` vs `root_cause` path selection (Gate 2), the
post-mitigation choice (Gate 3), and the urgency-based path recommender are all
**removed**. This section is the canonical home for both the design rationale and
the resulting behavior; the resolved design decisions are in §2.5.

### 2.1 Two orthogonal axes (why the fork was wrong)

The single fork conflated two independent questions:

- **Axis A — Certainty.** Do we know the cause? the solution? Tracked by the
  engine-derived assessment variables `cause_state` and `solution_state`. Drives
  whether diagnostic *labor* (hypothesis formulation, causal evidence-needs) is
  needed.
- **Axis B — Mitigation gap.** Is something hurting *now* that we cannot fully
  resolve this session? Drives whether a **mitigation** is inserted.

The two axes are independent. The old fork forced an Axis-B answer ("mitigate
first") that wrongly *implied* an Axis-A answer ("cause unknown, RCA deferred") —
which is what trapped self-naming-error cases (the cause is in the log; forbidding
the agent from recording it left the case permanently pre-mitigation).

**Diagnostic-machinery rule (replaces the entire path-conditional RCA ban):** run
hypothesis formulation + evidence-needs **iff the cause is uncertain**
(`cause_state ∈ {UNKNOWN, CANDIDATES}`) — *not* because a mitigation was or
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
                                  ├─(Axis-B gap detected, any turn)─▶ [MITIGATION insert]
                                  │        propose → accept → verify → return to flow
                                  │
                                  └─(CLOSE available at ANY point: abandon, or data/impl. limit)
```

A **mitigation** is an *optional inserted sub-activity*
that buys time when an Axis-B gap exists. A case is described retrospectively as
**direct** (no mitigation) or **mitigated** (`progress.mitigation is not
None`) — descriptions of what happened, not paths chosen upfront. The
INQUIRY → INVESTIGATING transition requires **Gate 1 only** (problem-statement
confirmation); there is no second gate before investigating.

> **Methodology alignment.** In the
> [Two-Dimensional Hypothesis Methodology](./two-dimensional-hypothesis-methodology.md),
> a mitigation *is* a **temporary state interception** (R8) on the causal ladder,
> and the two axes here map directly onto its *why-stopping rule*: **Axis A
> (certainty / feasibility)** sets how deep diagnosis descends before a node is
> accepted as the stopping point (`rca_infeasible` caps the ladder at the deepest
> controllable rung); **Axis B (impact-now gap)** decides whether an intermediate
> rung is intercepted *temporarily* en route. See methodology §7.5.

### 2.3 Mitigation triggers and forwarding

A mitigation is proposed when an Axis-B gap exists. The first and most common
assessment point is immediately after `symptom_verified` (the same point the old
Gate 2 fired) — the agent asks "is there an impact-now gap that can't close this
session?" and, if so, *proposes* a mitigation (user accepts → insert; declines
→ continue). The assessment is **re-evaluable** — a mitigation can also be
proposed later (RCA stalls, situation deteriorates). It is never an irreversible
commitment.

The three triggering circumstances each leave a *different* thing unresolved,
which determines the forwarding path **after** the mitigation verifies (this is
the answer to "what happens after mitigation?" — it is **not** uniformly "continue
to RCA"; that was only row 1 of the old Gate-3 assumption):

| Trigger for inserting a mitigation | cause_state | solution_state | Forwarding after mitigation |
|---|---|---|---|
| **(1)** cause unknown / multiple candidates needing different fixes | UNKNOWN / CANDIDATES | UNKNOWN | **RCA** — hypothesis formulation + evidence-needs |
| **(2)** cause known, solution unclear / multiple complex options | IDENTIFIED | CANDIDATES (reserved) | **Solution deliberation** (follow-on; reuses hypothesis machinery) |
| **(3)** cause + solution known, implementation takes time | IDENTIFIED | SELECTED, `solution_feasible=DEFERRED` | **Handoff / schedule** — CLOSE-with-documented-solution, unless a gone⇒gone confirmation stands (then RESOLVED; see §Decision 2) |

If **no** Axis-B gap exists (cause known, solution known, implementable now), the
flow is **direct**: verify → propose solution → accept → verify → RESOLVED. No
mitigation, no hypothesis machinery. (This is the case the old model trapped.)

**Single insert, never a dead-end.** The engine models **one** mitigation per
investigation (forward-only). If the first mitigation doesn't stabilize, the
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

- `mitigation.accepted ∧ ¬mitigation.verified` → **"Mitigating"**
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

#### 2.4.3 What It Does: Post-Mitigation Behavior

The signal's effect is narrow and specific — when a mitigation has been verified but the cause remains uncertain, it changes whether the agent pushes RCA or offers closure:

| `rca_infeasible` | Post-mitigation agent behavior |
| --- | --- |
| `False` (default) | Agent pushes toward RCA: *"The mitigation is working. Now let's investigate the root cause to prevent recurrence."* |
| `True` | Agent proposes closure: *"The mitigation is verified. Since [rationale], shall we close this case?"* Uses User-Agent Handshake — user must confirm. |

The close is not offered on a case whose cause is confirmed eliminated (closure readiness SUGGEST_RESOLVE, a qualifying `causal_absence` row): there the resolve offer is the one the case warrants, made by the resolution backstop (INV-43) or by the model's own RESOLVED proposal. Every engine opener reads closure readiness before choosing its target (#1885).

If `rca_infeasible=True` but the user says "actually, let's dig deeper" — the agent proceeds with RCA. The signal is advisory, not binding.

#### 2.4.4 Terminal State

Cases closed via this path use the existing terminal state:

- `status = CLOSED`
- `closure_reason = "mitigation_sufficient"` (the documented mitigation is preserved on the closed case)

`RESOLVED` remains pristine — it always means a permanent fix with verified root cause. See §4.5.1 for runbook generation.

### 2.5 Design decisions and open follow-ons

The unified flow was ratified with the following decisions (resolved 2026-06-05):

1. **Solution-space deliberation reuses the hypothesis / evidence-needs machinery.** Candidate solutions are enumerated, compared, and selected through the same propose/evidence/converge loop that drives causal hypotheses, rather than a parallel structure — a dedicated structure is introduced only if a clear advantage emerges. This is the genuinely under-built surface today: the current SOLUTION stage assumes a *single obvious fix* to propose-and-verify and has no notion of deliberating across a solution space (trade-offs, workaround-vs-permanent, design choices). `solution_state = CANDIDATES` is the reserved hook for this; the deliberation-loop design (what "evidence" means for a solution choice, and how `solution_state` advances UNKNOWN→CANDIDATES→SELECTED) is the highest-value follow-on work.
2. **Deferred-implementation disposition is CLOSE-with-documented-solution — unless the cause is already counterfactually confirmed** (forwarding row 3 in §2.3) — no third terminal state unless analytics genuinely need to separate "resolved-pending-impl" from "abandoned." The documented root cause + selected fix are preserved on the closed case (`closure_reason = solution_deferred`).

   The exception is **resolve preservation** (INV-37). `solution_feasible` is only ever written by the LLM and is never reset by the engine, so the DEFERRED flag outlives the deferral: a case can carry it while a qualifying `causal_absence` row (gone⇒gone) arrives on a later turn. That case is resolution-grade, and closing it would record it unresolved and discard the attribution. The engine-side proposer therefore consults `assess_closure_readiness` and proposes **RESOLVED** on `SUGGEST_RESOLVE`, exactly as the LLM-proposal path and the confirm-time guard already did — deferred implementation says *when* the remaining work lands, not whether the cause was found. Trigger is `_has_causal_absence`, the same bar `assess_resolution_readiness` uses for READY; a merely stabilized case has `symptom_absence` and correctly does not pivot.
3. **One mitigation record for now** (forward-only), but the flow must stay open to user-led action so a non-mitigating insert is never a dead-end (§2.3; INV-24). Multiple structured mitigation records are a possible future extension.
4. **`cause_state = CANDIDATES` is derived** from the active-hypothesis count (≥2 ACTIVE hypotheses), not a second stored field — coupled to the prompt change that forces hypothesis emission under uncertainty (they ship together; see §1.4.1 and INV-22). A derived signal over an unreliable producer is worse than the boolean it replaced, so the derivation and the prompt mandate are not separable.
5. **Resolution-gate interaction** (`solution_verified` ↔ the absence-evidence end-state) is revisited *after* this redesign rather than folded in — see the resolution-gate notes and the loop-bound (same-turn offer) guard in §1.2.

Parse-time robustness for malformed LLM structured output (the general never-500 backstop) is a separate layer, specified in [error-handling-and-recovery.md §3.4](./error-handling-and-recovery.md#34-never-500-backstop-for-parse-time-validation-errors).

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
    reasoning: Optional[EvidenceTrail] = None,
) -> List[MilestoneValidationResult]:
    """
    Validate that LLM milestone claims are supported by cited evidence.

    This does NOT advance milestones. It checks whether the LLM's claims
    are justified by the evidence IDs cited in evidence_trail.

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
| `SYMPTOM_ABSENCE_EVIDENCE` | Confirmation the symptom is gone after a workaround (cause may persist) | MITIGATION |
| `CAUSAL_ABSENCE_EVIDENCE` | Confirmation the root cause is eliminated after the fix | TREATMENT |

```text

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
    # NOTE: the mitigation pair (mitigation_accepted/mitigation_verified) are
    # LLM emission symbols that materialize into the single progress.mitigation
    # record. cause_state is engine-derived, so it is not boolean-diffed here —
    # the recompute reports its own rising edge instead, and the engine appends
    # "root_cause_identified" to this turn's milestones on the turn cause_state
    # newly reaches IDENTIFIED. The name is the one the derived
    # CaseProgress.completed_milestones map already uses, so the per-turn list
    # and the case-level snapshot share one vocabulary. Without that append the
    # per-turn readers (the transparency counter above all) key on a name that
    # engine-derivation stopped producing — see INV-35.
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
    progress_made = check_if_progress_made(metadata)

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

**Scope note**: The requirement applies to all agent suggestions during INVESTIGATING state (mitigation proposals, hypothesis generation, diagnostic/solution suggestions, data requests). INQUIRY state (problem statement refinement) is exempt because investigation hasn't started yet.

---

## 4. Supported Case Lifecycles

This section outlines all possible case lifecycles and their associated milestones.

### 4.1 Inquiry-Only Lifecycle (No Investigation)

**User Goal**: Ask a quick question or get clarification without starting a formal investigation.
**Flow**: `INQUIRY` → `CLOSED`

#### Workflow Steps

1. **User Inquiry**: User asks a question (e.g., "How do I check logs?").
2. **Agent Response**: Agent answers the question.
3. **Closure**: User leaves or explicitly closes the session.

#### Milestones

- None (Investigation milestones do not start).

---

### 4.2 KB-Resolution Path (Same-Turn Collapse)

**User Goal**: Resolve a known issue quickly using a runbook match.
**Flow**: `INQUIRY` → `INVESTIGATING` → `RESOLVED` (with INVESTIGATING typically completing in 1–2 turns)

This is not a separate lifecycle edge — it is the standard `INQUIRY → INVESTIGATING → RESOLVED` flow where INVESTIGATING completes rapidly because the matched runbook Cause supplies the root cause, mechanism, and fix without requiring multi-turn evidence gathering. See [§1.2 INVESTIGATING → RESOLVED → KB-Resolution Path](#kb-resolution-path-milestone-collapse-variant) for the engine mechanism.

#### Workflow Steps

1. **Detection (during INQUIRY)**: Agent calls `kb_qa` for the symptom and identifies a high-confidence runbook match. KB match is held back from the user until problem statement is confirmed.
2. **Problem confirmation (INQUIRY → INVESTIGATING)**: Agent presents problem statement; user confirms. Standard INQUIRY → INVESTIGATING transition fires.
3. **Cause attribution (early INVESTIGATING)**: The agent attributes the active `### Cause <X>` from the retrieved runbook by reasoning over its content against current case state. If attribution is unambiguous, agent proposes the Cause's `Mitigation` + `Resolution` to the user.
4. **User applies the fix and confirms** ("That fixed it" / "It worked"). LLM emits `knowledge_resolution` in `state_updates`.
5. **Same-turn milestone collapse**: Engine populates `RootCauseConclusion` (Statement → `root_cause`, Mechanism → `mechanism`), creates `Solution` (Mitigation → `immediate_action`, Resolution → `longterm_fix`), and sets `solution_accepted`, `solution_verified`; `cause_state=IDENTIFIED` is engine-derived from the validated chain root (INV-35), never an emitted flag.
6. **Standard RESOLVED handshake**: LLM emits `ProposedTransition`; user's same confirmation message is recognized as the disposition acknowledgment; transition executes.

#### Milestones

- All standard INVESTIGATING milestones populated in the collapse turn: `symptom_verified`, `cause_state=IDENTIFIED` (engine-derived, INV-35), `solution_proposed`, `solution_accepted`, `solution_verified`.
- `knowledge_resolution` signal recorded for KB-attribution metrics.

---

### 4.3 Direct Investigation (no mitigation)

**User Goal**: Diagnose an issue, find the root cause, and fix it permanently — with no impact-now gap that requires a mitigation detour.
**Flow**: `INQUIRY` → `INVESTIGATING` → `RESOLVED`

This is the common case: cause known or discoverable, solution implementable now.

#### Workflow Steps & Milestones

**Phase 1: Inquiry**

- **Goal**: Establish problem statement.
- **Transition Trigger**: User confirms problem statement (Gate 1) and decides to investigate.

**Phase 2: Investigation** (one opportunistic flow)

- Agent verifies symptoms using evidence
  - Progress indicator: `symptom_verified` (LLM sets when symptoms confirmed)
- Diagnostic machinery runs **iff the cause is uncertain** (`cause_state ∈ {UNKNOWN, CANDIDATES}`):
  - Agent forms hypotheses, tests against evidence; when ≥2 plausible causes remain active the engine derives `cause_state=CANDIDATES`
  - When a single cause is established (or the error is self-naming), the engine records `cause_state=IDENTIFIED`. For self-naming errors this can happen as early as turn 1 — the engine records the cause it legitimately knows; it is never path-stripped.
  - Hypothesis-before-causal-evidence ordering is prompt guidance in the unified INVESTIGATION block (a causal claim presupposes a hypothesis to attach to)
- Agent proposes a concrete solution action
  - Progress indicator: `solution_proposed`; assessment: `solution_state=SELECTED` (`solution_feasible=NOW`)
- **Solution accepted → "Resolving"** (inference-based)
  - User complies with the proposed solution → gate milestone `solution_accepted`
  - If user questions or refuses → continues investigating, agent refines approach
- **Resolution verification** (iterative)
  - Agent verifies whether the fix worked from submitted data
  - If fix worked → agent proposes resolution via User-Agent Handshake
  - If fix failed → extended investigation: failure analysis → gap identification → targeted data request → new hypothesis → revised fix; escalation when no viable options remain

**Phase 3: Resolution**

- **Transition Trigger**: User confirms fix worked via User-Agent Handshake → gate milestone `solution_verified`
- **State**: `RESOLVED`.

---

### 4.4 Mitigated Investigation (impact-now gap)

**User Goal**: Stop active impact quickly, then continue the investigation.
**Trigger**: An Axis-B mitigation gap — something is hurting now that can't be fully resolved this session. The agent *proposes* a mitigation in-prompt (no path is chosen upfront); the user accepts or declines.

A mitigation is an **insert** into the same unified flow, not a separate path
(§2.3). After it verifies, forwarding depends on what is still unresolved (§2.3
forwarding table): cause uncertain → RCA; cause known / solution unclear →
solution deliberation; cause + solution known but deferred → CLOSE-with-documented-solution (or RESOLVED when a gone⇒gone confirmation already stands — §Decision 2).

#### Mitigated → RESOLVED

**Flow**: `INQUIRY` → `INVESTIGATING` (mitigation insert, then RCA + permanent fix) → `RESOLVED`

**Gate signals / record**:

- `mitigation_accepted` (LLM emission) → materializes `mitigation.accepted`: user acknowledged executing the proposed mitigation.
- `mitigation_verified` (LLM emission) → materializes `mitigation.verified` + `completed_at_turn`: the mitigation stabilized the situation. The case continues opportunistically (the "Mitigating" label clears).
- `solution_accepted`: user acknowledged executing the permanent solution → "Resolving".
- `solution_verified`: permanent fix validated (User-Agent Handshake) → RESOLVED.

#### Mitigated → CLOSED

**Flow**: `INQUIRY` → `INVESTIGATING` (mitigation insert) → `CLOSED`

The user decides the mitigation is sufficient (or RCA is infeasible, see §2.4) and does not pursue a permanent fix. Two paths lead here:

1. **Agent-proposed** (when `rca_infeasible=True` and the cause remains uncertain): after the mitigation verifies, the agent proposes closure via User-Agent Handshake instead of pushing RCA.
2. **User-initiated** (any case): the user closes via UI at any time (close-anytime, §2.3).

**Closure**: `CaseState.CLOSED` with `closure_reason="mitigation_sufficient"`. The documented mitigation (and any partial findings) is preserved on the closed case.

**Post-terminal**: agent offers runbook generation only for RESOLVED cases (§4.5.1); CLOSED cases get the closure summary only.

#### Mitigation Is Iterative, Forward-Only

A mitigation is not assumed one-shot. The agent may adjust its approach and propose multiple attempts until the user verifies stabilization; each accepted attempt is recorded in `action_attempts`. The `mitigation` record itself is **single and forward-only** (INV-24): `accepted` / `verified` are never reset, and `completed_at_turn` is stamped once when `verified` first flips True (the boundary for up-weighting pre-mitigation evidence in later RCA). If a mitigation fails to stabilize, the flow stays open to user-led action — the agent acknowledges it didn't work and may propose an alternative in prose, or the user closes (§2.3, the no-dead-end rule).

#### How the System Distinguishes Outcomes (Retrospectively)

The retrospective shape is **direct** vs **mitigated**, derived from
`progress.mitigation is None`:

| Field | Mitigated → RESOLVED | Mitigated → CLOSED | Direct → RESOLVED |
| ----- | ------------------- | ------------------- | ----------------- |
| `mitigation` present | Yes | Yes | No |
| `mitigation.accepted` / `.verified` | True / True | True / True | n/a |
| `solution_accepted` / `solution_verified` | True / True | False / False | True / True |
| `cause_state` | IDENTIFIED | May be UNKNOWN/CANDIDATES | IDENTIFIED |
| `CaseState` | RESOLVED | CLOSED | RESOLVED |
| `closure_reason` | None | `mitigation_sufficient` | None |
| **Knowledge artifact** | **Runbook** | **Closure Summary only** | **Runbook** |

`closure_reason` is `None` for all RESOLVED cases — resolution itself is the
categorization. Only CLOSED cases carry a `closure_reason` value (`inquiry_only`,
`closed_false_alarm`, `solution_deferred`, `closed_rca_infeasible`,
`mitigation_sufficient`, `closed_restatement_held`, or
`closed_insufficient_evidence`). `derive_closure_reason` (in
`terminal_transitions.py`) picks the most specific reason first: `inquiry_only`
when the case never left INQUIRY; `closed_false_alarm` when the evidence showed
the reported symptom was never present (`problem_status` INVALIDATED —
[§ Verifying the problem statement](#141-verifying-the-problem-statement-three-outcomes));
`solution_deferred`
when a fix is documented but was never applied; `closed_rca_infeasible` when RCA
was declared infeasible with a rationale; `mitigation_sufficient` when a
mitigation is verified; `closed_restatement_held` when the restatement guard held
every unsettled root (#1195); otherwise `closed_insufficient_evidence`. It no
longer keys on the `INSUFFICIENT_EVIDENCE` verification-status cell (see
[Insufficient-Evidence Handling §3.5](./insufficient-evidence-handling.md)),
which a case stuck at symptom verification never reaches.

---

### 4.5 Post-Terminal Operations

After a case reaches RESOLVED or CLOSED, the system auto-generates a terminal summary. For resolved cases, the user may additionally request runbook generation.

#### 4.5.0 Auto-Generated Terminal Summary

**Trigger**: Synchronous on terminal transition (both RESOLVED and CLOSED). The closure-turn agent reply waits for the render and embeds the rendered markdown inline. A render failure is caught — the state transition still commits — but the chat reply tells the user generation didn't complete and the regen affordance is offered on the same ack turn for retry.

**Implementation**: `TerminalTurnHandler.auto_generate_report()` (`milestone_engine/terminal_turns.py`) calls `ReportGenerationService.render_reports()` (deterministic, no write, no LLM call) and adds the row to the turn's `TurnCommitPlan`, so it commits with the terminal state in the turn's one commit (#1882). It returns either the rendered markdown (success), a skip note (gate FAIL), or a failure note (render error). The closure-turn reply is composed by `_compose_terminal_reply()` which appends the return value to the deterministic status line. Called from three places: the explicit-confirmation path, the dropdown-resolution path, and the end-of-turn LLM-driven transition path.

**Substance gate** (`should_generate_terminal_summary()` in `terminal_transitions.py`): RESOLVED always generates — a verified solution is meaningful content by definition. CLOSED requires `evidence > 0` OR `hypotheses > 0` OR `completed_milestones > 0`. The gate is substance-only by design — conversation depth (`message_count`) is intentionally not a signal, since terminal Q&A inflates it and would let the verdict flip after closure. The three substance signals are naturally frozen in CLOSED state, so the gate is stable across the terminal lifetime without a snapshot field.

**Summary types**:

| Case Status | Report Type | Content Structure |
|-------------|-------------|-------------------|
| RESOLVED | `RESOLUTION_SUMMARY` | Problem Statement, Root Cause (from validated hypotheses; lists the cause's co-necessary conditions when the graph carries an AND-set), Causal Map (gated), Solution Applied (the executed fix — see below), Confirming Evidence, Timeline, Milestones Reached, Mitigation (if any) |
| CLOSED | `CLOSURE_SUMMARY` | Problem Statement, Investigation State (milestones/evidence/hypotheses counts), Closure Reason, Leading Hypotheses (top 5 by confidence), Mitigation Status, Timeline, Recommendation (closed_insufficient_evidence and closed_restatement_held closes) |

**Solution Applied** reports what was *done*, not everything that was said. The engine mints one `Solution` per LLM fix proposal and never stamps its lifecycle fields, so `case.solutions` accumulates every re-proposal of one remediation — three paraphrases of the same fix in the case that prompted this (fm#1091), all rendered as applied. The section is therefore derived from each solution's co-created `ProposedAction` via `classify_solution_outcome`, the same signal the runbook boundary uses: `APPLIED` entries (the user executed them) render under **Solution Applied**; `FAILED` entries (superseded, rejected, or engine-downgraded — never run) are dropped, since asserting them to the user is the over-claim the runbook boundary already refuses. A **standing proposal** renders under **Proposed Solution** — with a note that no fix was recorded as executed — whenever no *permanent* fix was executed. That last condition is the load-bearing one: `classify_solution_outcome` reports APPLIED for an accepted MITIGATION too, and offer supersession covers SOLUTION offers only, so a pending SOLUTION beside an accepted stop-gap is the real fix still outstanding (it must be shown), while a pending SOLUTION beside an accepted SOLUTION is the model restating the executed fix (it must not). A case with **no `ProposedAction` rows at all** is un-instrumented rather than un-executed, and keeps the pre-existing surface-every-solution rendering — a legacy resolved case must not acquire a "no fix was executed" claim its record cannot support.

Summaries are built from case data fields (hypotheses, solutions, evidence, milestones, timestamps). Stored as `CaseReport` records with `auto_generated=True`. Duration is calculated from `created_at` to `resolved_at` or `closed_at`.

**Causal Map** (`core/investigation/causal_map.py`): a fenced mermaid flowchart serialized deterministically from the persisted causal graph (`CausalNode`/`CausalEdge` rows) — engine-derived, never LLM-authored, so it can only depict structure the investigation established. Node glyphs carry state (✓ validated, ○ not established, ✗ refuted); solid arrows leave only VALIDATED causes; the legend is exported alongside the renderer so diagram and key cannot drift. **Resolution summaries only** (product decision — a closed case's map would read as a conclusion the case never reached, even when a `solution_deferred` close carries an established cause). Gated: renders only when the cause is established (assurance MECHANISTIC or above, the same recomputed grade as the assurance note) over a non-trivial, connected graph — size thresholds and label rules live as constants in `causal_map.py`, and the rendered chain must reach the problem anchor. Below the gate the section is omitted entirely; a rendering error also omits the section (fail-closed) and never blocks generation. **Self-contradiction valve** (fm#1091): the map is also withheld when a node it would stamp ✓ validated is the chain root of a hypothesis the same report lists as **Refuted** — the map is the half that asserts a cause produced the problem, so a document must never draw as established a statement it refutes three sections later. The engine cannot tell at render time which axis is right, so it withholds the picture rather than pick one; `faultmaven_causal_map_suppressed_contradiction_total` counts it, and stays at zero while the attach-time 1:1 chain↔hypothesis rule (methodology §7.8.1) holds.

**Report type enum** (`ReportType` in `case/domain/owned_models/report.py`):

- `RESOLUTION_SUMMARY` — auto-generated for resolved cases (always generated)
- `CLOSURE_SUMMARY` — auto-generated for closed cases (subject to substance gate)
- `RUNBOOK` — user-requested via ConversionService (see §4.5.1)

**Dashboard**: `ReportTab` is view-only — displays auto-generated summaries with formatted markdown rendering and download. No manual generate button. If no summary was generated for a closed case (substance gate FAIL), the tab surfaces a derived skip-reason note (via `terminal_summary_skip_reason()` in `terminal_transitions.py`). If the gate PASSed but no Report row exists (generation failed), the tab surfaces a "regenerate from Copilot" note. RESOLVED cases always have a summary.

**Two views, one record**: The chat and the Dashboard show the same `CaseReport` row. The chat renders it once at the moment of generation (and again on each regeneration); the Dashboard renders it persistently. Each regeneration adds a version and marks it current; both views show the current one.

**API endpoints:**

- `GET /api/v1/cases/{case_id}/reports` — List generated reports
- `GET /api/v1/cases/{case_id}/reports/{report_id}/download` — Download report
- `POST /api/v1/cases/{case_id}/reports` — Regenerate (requires terminal state)

#### 4.5.1 Runbook Generation (Knowledge Flywheel)

**Eligibility**: RESOLVED cases only. CLOSED cases are not eligible regardless of `closure_reason` — they lack the confirmed root-cause-to-solution chain that a future investigator can apply. On subsequent terminal Q&A turns, a CLOSED case offers "Regenerate closure summary" when the substance gate would PASS (independent of whether the Report row currently exists — the same affordance handles both re-roll and failed-generation retry). For low-substance closures, no suggestion is offered — there's nothing to summarize.

**Design**: Suggest first, evaluate on acceptance. The agent always offers a DECIDE suggestion at resolution time. Readiness assessment and deduplication happen only when the user accepts — not upfront. This avoids wasted computation and gives the user a clear accept/decline choice.

**Trigger flow (Copilot)**:

```text
User confirms resolution
    → Agent offers DECIDE suggestion: "Would you like me to create a runbook?"
    → User accepts
        → System evaluates readiness + deduplication
        → Possible outcomes:
            SUCCESS           → Draft created, user redirected to Dashboard
            NOT_SUITABLE      → "Not enough data for a quality runbook" (no draft)
            SIMILAR_FOUND     → "Similar runbook exists: {title} ({score}% best-chunk
                                match)" — stated as overlap, not coverage. No draft;
                                a "Generate a new runbook anyway" affordance is
                                offered, and only that explicit confirmation creates
                                one
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

**Deduplication** (`RunbookKnowledgeBase` vector search over the published-runbook corpus, scoped to the case owner — see `runbook-dedup.md`):

| Best-chunk similarity | Verdict | Outcome |
|------------|---------|---------|
| ≥70% | `SIMILAR_FOUND` | No draft on this turn — the candidate is named by title and score (overlap, never a coverage claim); a "Generate a new runbook anyway" affordance creates one on explicit confirmation |
| <70% | No conflict | Draft generated normally |
| Dedup failed/skipped | `SUGGEST_WITH_CAVEATS` | Draft generated, with the "could not check for duplicates" caveat stated |

**Workflow** (canonical path via `ConversionService`, triggered after user accepts):

1. `POST /api/v1/knowledge/convert-from-case` — extracts case data (solutions, root cause, hypotheses, evidence, domain/service)
2. LLM generates canonical runbook (YAML frontmatter + 7 markdown sections) using `CONVERSION_SYSTEM_PROMPT`
3. `RunbookValidator` checks structure; `QualityScorer` evaluates completeness, clarity, actionability (0-100 score)
4. Draft created in `draft` status for user review
5. User edits draft → re-validates → verifies → ingests into ChromaDB vector DB
6. Verified runbook is chunked by `ContentChunker` (structure-aware markdown splits at every H1-H4 heading, 100-3000 chars, no fixed overlap — measured on the shipped pack: median 1726 chars), embedded (BGE-M3, 1024 dims), indexed for future similarity search. The "512 tokens with 50-token overlap" figure this line used to quote describes the *planned* evidence chunking, not KB chunking, which has never had a token budget or an overlap

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

1. **Investigation Starts**: Gates and progress indicators partially set.
2. **Stall/Escalation**:
    - Agent cannot find root cause (no viable options — communicates limitations and suggests escalation).
    - User stops responding.
    - User explicitly requests escalation.
    - User closes after a mitigation without pursuing RCA (the documented mitigation is preserved on the closed case).
3. **Closure**: Case marked `CLOSED` with an engine-derived `closure_reason` naming why it ended — a stabilized case closes as `mitigation_sufficient`, one that established nothing as `closed_insufficient_evidence`.

#### Milestones

- Partial progress: `symptom_verified`; `cause_state` may be UNKNOWN/CANDIDATES/IDENTIFIED; `solution_proposed`.
- The mitigation record may be present (`accepted`/`verified`) if a mitigation was performed.
- `working_conclusion`: Summary of findings up to the point of closure.
- `action_attempts`: Complete record of all mitigation and solution actions attempted.
