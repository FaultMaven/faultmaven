from typing import Any

from faultmaven.core.investigation.prompts.fence import PromptFence
from faultmaven.modules.case.contracts import (
    Case,
    NeedPriority,
    is_server_written_assistant_row,
    is_server_written_user_row,
)

from .budget import (
    HISTORY_SUMMARY_MAX_TURNS,
    HISTORY_VERBATIM_TURNS,
    STATE_SUMMARY_DIGEST_CHARS,
    STATE_SUMMARY_MAX_EVIDENCE_DIGESTS,
    STATE_SUMMARY_MAX_HYPOTHESES,
)
from .text_shaping import _smart_truncate_agent_response


def _build_state_summary(case: Case) -> str:
    """
    Build compact state summary for long conversations (Gap #8: Section 11.5).

    Instead of full conversation history (~2000 tokens), provides compact summary (~200 tokens).

    Args:
        case: Current case

    Returns:
        Formatted state summary
    """
    # Problem description
    problem_desc = case.description[:100] if case.description else "Not specified"
    if case.problem_verification and case.problem_verification.symptom_statement:
        problem_desc = case.problem_verification.symptom_statement[:100]

    # Current stage
    stage = "INQUIRY"
    if case.state.value == "investigating" and case.current_stage:
        stage = case.current_stage.value.upper()
    elif case.state.value in ["resolved", "closed"]:
        stage = case.state.value.upper()

    # Verification status
    verified_items = []
    if case.progress:
        p = case.progress
        if p.symptom_verified:
            verified_items.append("symptom")

    verified = ", ".join(verified_items) if verified_items else "none"

    # Active hypotheses — every one (bounded), each with its id, so the agent
    # retains awareness of competing theories when the full hypothesis block is
    # absent AND can still address any of them by id in
    # hypothesis_evidence_links / hypotheses_to_update.
    active_h = [
        h for h in case.hypotheses.values() if h.state.value in ["active", "validated"]
    ]
    if active_h:
        sorted_h = sorted(active_h, key=lambda h: h.likelihood, reverse=True)
        hypothesis_lines = []
        for h in sorted_h[:STATE_SUMMARY_MAX_HYPOTHESES]:
            status_tag = " [VALIDATED]" if h.state.value == "validated" else ""
            hypothesis_lines.append(
                f"  - [{h.hypothesis_id}] {h.statement[:100]} "
                f"({h.likelihood * 100:.0f}%{status_tag})"
            )
        hypothesis_str = "\n".join(hypothesis_lines)
    else:
        hypothesis_str = "  None yet"

    # Evidence count + digest of diagnostic findings
    evidence_count = len(case.evidence)
    evidence_str = (
        f"{evidence_count} artifacts analyzed"
        if evidence_count > 0
        else "No evidence collected"
    )

    # Compact digest: retain key findings from diagnostic evidence so the agent
    # can still cite specifics after Tier A window eviction. Includes config/code
    # evidence which often contains root-cause clues.
    evidence_digests = []
    for ev in case.evidence:
        dt = ev.source_type.value.lower()
        if (
            "log" in dt
            or "metric" in dt
            or "trace" in dt
            or "error_report" in dt
            or "config" in dt
            or "code" in dt
        ) and ev.summary:
            evidence_digests.append(
                f"[{ev.source_type.value}] {ev.summary[:STATE_SUMMARY_DIGEST_CHARS]}"
            )
    evidence_digest = (
        "; ".join(evidence_digests[:STATE_SUMMARY_MAX_EVIDENCE_DIGESTS])
        if evidence_digests
        else ""
    )

    # Turn metrics
    turns_total = case.current_turn
    turns_since_progress = case.turns_without_progress

    summary = f"""<state_summary>
Investigation: {problem_desc}
Stage: {stage}
Verified: {verified}
Active Hypotheses (reference by [hyp_...] id in hypothesis_evidence_links / hypotheses_to_update):
{hypothesis_str}
Evidence: {evidence_str}
Turns: {turns_total} total, {turns_since_progress} since last progress
</state_summary>"""

    # Append evidence digest outside the compact summary block so it doesn't
    # inflate the base summary for cases with no diagnostic evidence
    if evidence_digest:
        summary += f"\n<evidence_digest>\n{evidence_digest}\n</evidence_digest>"

    return summary


def _build_turn_summary(turn) -> str:
    """Build a compact summary from a TurnProgress record.

    Format: TURN {n}: {user_summary} → {structural_metadata} | Agent: {response_summary}

    Includes both structural metadata (milestones, evidence counts) AND the
    agent_response_summary so the LLM knows WHAT was analyzed, not just counts.
    """
    # An aside (#1329) is summarised as what it was, not as what was said: the
    # poem or the trivia answer is not investigation context, and rendering it
    # invites the model to treat the exchange as a thread to pick back up.
    if turn.is_out_of_band:
        return f"TURN {turn.turn_number}: {ASIDE_LINE}"
    # A placeholder the server backfilled for a turn it never recorded (#1666):
    # both of its summaries are server text, so neither is read — quoting them
    # would present the server's words as the user's (#1434) and the agent's
    # (#1451). EARLIER TURNS routes one to the message preview instead; this is
    # the function's own guard, so no caller can render one.
    if turn.is_skipped:
        return f"TURN {turn.turn_number}: {NOT_RECORDED_LINE}"

    parts = []

    # Milestones completed this turn
    if turn.milestones_completed:
        parts.append(", ".join(turn.milestones_completed))

    # Evidence and hypothesis counts
    if turn.evidence_added:
        parts.append(f"{len(turn.evidence_added)} evidence added")
    if turn.hypotheses_generated:
        parts.append(f"{len(turn.hypotheses_generated)} hypotheses proposed")
    if turn.hypotheses_validated:
        parts.append(f"{len(turn.hypotheses_validated)} hypotheses validated")
    if turn.solutions_proposed:
        parts.append(f"{len(turn.solutions_proposed)} solutions proposed")

    outcome_desc = ", ".join(parts) if parts else ""

    # Always include agent_response_summary when available — this tells the
    # LLM what it actually analyzed/concluded, not just structural counts.
    # Without this, summarized turns lose critical detail like "analyzed
    # nova-api logs, found VM lifecycle events" → just "1 evidence added".
    agent_part = ""
    if turn.agent_response_synthesized:
        # A placeholder the server wrote (#1451): say the turn went
        # unanswered, as a bare line — never as ``Agent: <placeholder>``.
        agent_part = f" | {NO_ANSWER_LINE}"
    elif turn.agent_response_summary:
        agent_part = f" | Agent: {turn.agent_response_summary[:200]}"
    elif not outcome_desc:
        # No structural metadata AND no agent summary — use outcome as fallback
        if turn.outcome:
            outcome_desc = str(turn.outcome.value)
        else:
            outcome_desc = "conversation"

    user_part = turn.user_message_summary or "User message"
    if outcome_desc:
        return f"TURN {turn.turn_number}: {user_part} → {outcome_desc}{agent_part}"
    return f"TURN {turn.turn_number}: {user_part}{agent_part}"


def _fence_conversation(body: str, fence: PromptFence) -> str:
    """Wrap a rendered transcript in the assembly's fenced ``<conversation_history>``.

    The conversation section is a caller-controlled channel (#1256): every
    prior USER turn is replayed here byte-verbatim from ``case.messages``,
    which persist what the reporter typed (``InvestigationService.process_turn``
    appends ``"content": query`` — ``payload.query`` verbatim, nothing rewrites
    it on the way in). Before #1256 it was left unfenced on the premise that
    "it carries user text, which passes through ``sanitize_user_input`` on its
    own path"; that premise was false — ``sanitize_user_input`` only ever saw
    THIS turn's message argument, never the replayed transcript.

    A LEAF element, not a container: the tag-shaped scaffolding the fidelities
    below emit inside the body (``<state_summary>``, ``<previous_turn>``,
    ``<current_turn>``, ``<evidence_digest>``) is deliberately left unfenced
    and therefore demoted to quoted data by the trust rule. That is what it is
    — every one of them wraps material quoted from an earlier turn, and the
    authoritative copy of the state they echo is ``<case_identity>`` and the
    milestone blocks, which stay outside the fence. Fencing them instead would
    nest fenced delimiters inside a body routed through :meth:`PromptFence.data`,
    which reads as a token collision and re-mints until it raises.

    The body is passed through UNMODIFIED — no ``strip``, not even of trailing
    newlines. This module's whole premise is that the renderer does not touch
    quoted bytes, and the collision corpus is only an exhaustive proof about
    the transcript if what it records IS the transcript (#1256 review).
    """
    return fence.element("conversation_history", body)


#: One line for a turn that was answered outside the investigation: an
#: off-topic exchange (#1329) or an orientation reply — greeting, "help", an
#: empty message (#1343). Neither is investigation context, and quoting either
#: invites the model to pick the tangent, or its own recap, back up.
ASIDE_LINE = "(aside — not part of the investigation)"

#: One line for an ASSISTANT turn whose text the server wrote because the model
#: gave no usable answer (#1442, #1451) — the assistant counterpart of
#: ``ASIDE_LINE``, and rendered the same way: a bare line, never
#: ``ASSISTANT: <line>``. The row is not skipped, because a turn the model failed
#: to answer is information: eliding it leaves two user turns back to back, so
#: the model reads its previous turn as answered and a user's "you didn't
#: answer" arrives with no context. Nor is the placeholder quoted, because it is
#: server text and would read as the model's own words (#1434's rule). One line
#: whatever the stop reason; the reason chose the placeholder, not this.
NO_ANSWER_LINE = "(no answer — the assistant produced no usable reply this turn)"

#: One line for a turn RECORD the server backfilled because the turn was never
#: recorded (``Case.reconcile_turn_sequence``, ``TurnProgress.is_skipped``) —
#: rendered like ``ASIDE_LINE`` and ``NO_ANSWER_LINE``: a bare line, never
#: ``User:`` or ``Agent:``. Both of the placeholder's summaries are server
#: text, so quoting either would read as the user's or the agent's own words
#: (#1434, #1451, #1666).
NOT_RECORDED_LINE = "(turn not recorded — recovered after an interrupted turn)"

#: Truncation for a turn's one-line EARLIER TURNS preview. ONE value: the two
#: call sites used 100 and 150, so the same turn rendered at two lengths
#: depending on which branch fired.
_PREVIEW_CAP = 150


def _preview_turn_from_messages(
    messages: list, turn_num: Any, asides: set, cap: int = _PREVIEW_CAP
) -> str:
    """Name a turn by its first user message, for the EARLIER TURNS summary.

    ONE implementation with ONE cap, because there are two call sites and they
    had drifted on both: different truncation lengths (100 vs 150) and the
    marker guard applied to only one of them. A turn's preview must not depend
    on whether ``turn_history`` happened to carry a record for that turn.

    Two kinds of row are never quoted here, and both rules already existed
    elsewhere in this module — which is exactly why they have to be applied
    here too:

    - An OUT-OF-BAND turn renders as ``ASIDE_LINE``, the same collapse
      ``_build_turn_summary`` applies to a recorded aside and the RECENT window
      applies via ``_aside_turns`` (#1329). Quoting the tangent hands it back
      as investigation context and invites the model to pick it up.
    - A row the SERVER wrote is skipped: naming a turn by a marker tells the
      model the user said something they did not (#1434).
    """
    if turn_num in asides:
        return ASIDE_LINE

    user_msgs = [
        m
        for m in messages
        if m.get("turn_number") == turn_num
        and m.get("role") == "user"
        and not is_server_written_user_row(m)
    ]
    return user_msgs[0].get("content", "")[:cap] if user_msgs else "..."


def _aside_turns(messages: list) -> set:
    """Turn numbers whose rows are tagged out-of-band (#1329).

    Read off the persisted user row's metadata — the service tags both rows
    of an aside — so every fidelity of the history agrees with the
    ``turn_history`` outcome without needing the record in hand.
    """
    return {
        m.get("turn_number")
        for m in messages
        if (m.get("metadata") or {}).get("out_of_band")
    }


def _build_graduated_history(case: Case, fence: PromptFence) -> str:
    """Build graduated conversation history: recent turns verbatim, older summarized.

    Recent turns (last HISTORY_VERBATIM_TURNS): full user messages + smart-truncated
    agent responses. Older turns: one-line summaries from TurnProgress metadata.

    Falls back to verbatim-only if turn_history is unavailable.
    """
    messages = case.messages or []
    turn_records = case.turn_history or []

    if not messages:
        return _fence_conversation("No previous conversation.", fence)

    # Computed once and used by BOTH sections: the EARLIER summary collapses an
    # aside to one line, and the RECENT window elides it. They read the same
    # set, so the two fidelities cannot disagree about what an aside is.
    asides = _aside_turns(messages)

    # Determine the turn number boundary between "earlier" and "recent"
    # Get all unique turn numbers from messages, sorted
    all_turn_nums = sorted(
        {m.get("turn_number", 0) for m in messages if m.get("turn_number")}
    )

    if len(all_turn_nums) <= HISTORY_VERBATIM_TURNS:
        # Few enough turns — all verbatim, no summarization needed
        return _build_verbatim_history(messages, fence)

    # Split: recent turns get verbatim, older turns get summarized
    recent_turn_nums = set(all_turn_nums[-HISTORY_VERBATIM_TURNS:])
    earlier_turn_nums = all_turn_nums[:-HISTORY_VERBATIM_TURNS]

    result = ""

    # --- EARLIER TURNS (summarized from TurnProgress) ---
    if earlier_turn_nums and turn_records:
        # Index turn_records by turn_number for quick lookup
        turn_index = {t.turn_number: t for t in turn_records}
        summary_turns = earlier_turn_nums[-HISTORY_SUMMARY_MAX_TURNS:]

        result += "EARLIER TURNS:\n"
        for turn_num in summary_turns:
            record = turn_index.get(turn_num)
            if record is not None and not record.is_skipped:
                result += _build_turn_summary(record) + "\n"
            else:
                # Turn record missing — minimal fallback from messages. A
                # placeholder the server backfilled for a turn it never
                # recorded (#1666) is not a record either: its summaries are
                # server text, while the turn's real rows may still exist and
                # name it by what the user actually said.
                result += (
                    f"TURN {turn_num}: "
                    f"{_preview_turn_from_messages(messages, turn_num, asides)}\n"
                )
        result += "\n"

    elif earlier_turn_nums:
        # No turn_records available — summarize from messages directly
        result += "EARLIER TURNS:\n"
        summary_turns = earlier_turn_nums[-HISTORY_SUMMARY_MAX_TURNS:]
        for turn_num in summary_turns:
            result += (
                f"TURN {turn_num}: "
                f"{_preview_turn_from_messages(messages, turn_num, asides)}\n"
            )
        result += "\n"

    # --- RECENT TURNS (verbatim with smart agent truncation) ---
    result += "RECENT TURNS:\n"
    current_turn_num = None
    for msg in messages:
        turn_num = msg.get("turn_number")
        if turn_num not in recent_turn_nums:
            continue

        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if not content or is_server_written_user_row(msg):
            continue

        if turn_num != current_turn_num:
            if current_turn_num is not None:
                result += "\n"
            result += f"TURN {turn_num}:\n"
            current_turn_num = turn_num
            if turn_num in asides:
                # One line for the whole turn (#1329): the poem is not
                # investigation context, and quoting it invites the model to
                # pick the tangent back up.
                result += f"{ASIDE_LINE}\n"
        if turn_num in asides:
            continue
        if is_server_written_assistant_row(msg):
            result += f"{NO_ANSWER_LINE}\n"  # #1451
            continue

        if role == "ASSISTANT":
            content = _smart_truncate_agent_response(content)

        result += f"{role}: {content}\n"

    return _fence_conversation(result, fence)


def _build_verbatim_history(messages: list, fence: PromptFence) -> str:
    """Build full verbatim history for short conversations (≤3 turns)."""
    result = ""
    current_turn_num = None
    asides = _aside_turns(messages)

    for msg in messages[-20:]:
        turn_num = msg.get("turn_number", "?")
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if not content or is_server_written_user_row(msg):
            continue

        if turn_num != current_turn_num:
            if current_turn_num is not None:
                result += "\n"
            result += f"TURN {turn_num}:\n"
            current_turn_num = turn_num
            if turn_num in asides:
                result += f"{ASIDE_LINE}\n"  # #1329, see _build_graduated_history
        if turn_num in asides:
            continue
        if is_server_written_assistant_row(msg):
            result += f"{NO_ANSWER_LINE}\n"  # #1451, see _build_graduated_history
            continue

        result += f"{role}: {content}\n"

    return _fence_conversation(result, fence)


# =============================================================================
# Evidence-needs block (Phase 4 of evidence-needs rollout)
# =============================================================================

# Cap on rendered needs per section to keep token cost bounded. Each
# rendered need is ~80 chars of header (capped via
# ``_REQUEST_TEXT_RENDER_CAP``) + ~80 chars of motivator line.
# Single-section case (DIAGNOSIS, or MITIGATION/TREATMENT with one of
# outstanding/re-verification empty): ~600 tokens worst case at 15
# needs. Both-sections case (MITIGATION/TREATMENT with both populated):
# up to 30 needs total, ~1200 tokens worst case. Typical cases stay
# well under either bound because the LLM emits short request_text.
_EVIDENCE_NEEDS_RENDER_CAP = 15

# Per-need truncation cap for ``request_text``. The model attribute is
# capped at 500 chars by the schema, but in the rendered block we keep
# things scannable. The full text is preserved in the DB and surfaces
# via the EVIDENCE-suggestion side (Phase 6).
_REQUEST_TEXT_RENDER_CAP = 120

# Priority sort key — HIGH first so the LLM sees the urgent demand
# without scrolling. Keyed by enum member rather than ``.value`` so a
# future ``NeedPriority`` addition raises ``KeyError`` here instead of
# silently sinking the new bucket to the bottom of the list.
_PRIORITY_ORDER: dict[NeedPriority, int] = {
    NeedPriority.HIGH: 0,
    NeedPriority.MEDIUM: 1,
    NeedPriority.LOW: 2,
}


def _truncate_request_text(text: str) -> str:
    """Truncate ``request_text`` for rendering only — full text stays in
    the DB. Adds a single-char ellipsis when truncation actually
    occurs so the LLM knows the surfaced line is partial."""
    if len(text) <= _REQUEST_TEXT_RENDER_CAP:
        return text
    return text[: _REQUEST_TEXT_RENDER_CAP - 1].rstrip() + "…"


def _render_ask_history(need) -> str:
    """The ``asked N×`` fragment for a need's header line, or ``""`` if never.

    This is the stored counter the mention-decay rule used to tell the model to
    reconstruct "by scanning your prior turns in the conversation history"
    (#1079). That scan cannot work: ``HISTORY_VERBATIM_TURNS`` is 3, and older
    turns collapse to ``_build_turn_summary``, which records milestones,
    artifact counts and 200 chars of the reply — never what was asked for. Past
    three turns every repeat read as a first mention, so the ask never decayed.

    Stating the count as a fact removes the reconstruction entirely. The last
    turn is included because "asked 4×, last on turn 14" and "asked 4×, last on
    turn 6" call for opposite responses — the first is a live loop, the second
    is an old ask the user has moved past.
    """
    count = need.times_surfaced
    if count <= 0:
        return ""
    if count == 1:
        return f", asked once (turn {need.last_surfaced_turn})"
    return f", asked {count}× (last turn {need.last_surfaced_turn})"


def _build_compact_history(
    case: Case, user_message_safe: str, fence: PromptFence
) -> str:
    """State-summary + previous-turn + current-turn (the low-fidelity history).

    Extracted so the allocator can choose between this and the fuller graduated
    history by budget pressure. Crucially, this *always* includes the current
    turn (and the previous turn when available), so even at the lowest fidelity
    conversational continuity is preserved — this is what lets the allocator
    guarantee continuity via the conversation section's floor instead of a
    separately-reserved last exchange.

    Fenced under the assembly's token like the other fidelity (#1256): the
    state summary carries ``case.description`` and hypothesis statements, and
    ``<current_turn>`` carries the user's message verbatim.
    """
    recent_history = _build_state_summary(case)
    if case.turn_history:
        last_turn = case.turn_history[-1]
        recent_history += "\n\n<previous_turn>\n"
        if last_turn.is_skipped:
            # A placeholder the server backfilled (#1666): neither summary is
            # read. The writer inserts one only BETWEEN records, so today the
            # last record is never one — but the rule is per record, not per
            # position.
            recent_history += f"{NOT_RECORDED_LINE}\n"
        elif last_turn.is_out_of_band:
            recent_history += f"{ASIDE_LINE}\n"  # #1329
        else:
            if last_turn.evidence_added:
                recent_history += (
                    f"User provided: {len(last_turn.evidence_added)} "
                    "evidence artifacts\n"
                )
            if last_turn.agent_response_synthesized:
                recent_history += f"{NO_ANSWER_LINE}\n"  # #1451
            elif last_turn.agent_response_summary:
                recent_history += f"Agent: {last_turn.agent_response_summary[:200]}\n"
        recent_history += "</previous_turn>"
    recent_history += "\n\n<current_turn>\n"
    recent_history += f"User: {user_message_safe}\n"
    recent_history += "</current_turn>"
    return _fence_conversation(recent_history, fence)
