import logging
from typing import Any

from faultmaven.infrastructure.llm.providers import (
    StopReason,
    normalize_stop_reason,
)
from faultmaven.modules.case.contracts import Case, CaseState

logger = logging.getLogger("faultmaven.core.investigation.milestone_engine")


_DISPOSITION_GATE_ANSWERED_KEY = "disposition_gate_answered_this_turn"

#: Metadata key: the engine's OWN disposition offer was withdrawn during this
#: turn. Turn-scoped and never persisted — it says nothing about whether the
#: user refused, only that re-proposing the same offer later in the SAME turn
#: would be taking back an affordance the user just acted on.
_ENGINE_DISPOSITION_WITHDRAWN_KEY = "engine_disposition_withdrawn_this_turn"

#: How many refused deferred-disposition signatures a case carries. Bounds the
#: progress blob; large enough that an oscillation between a handful of
#: justifying states cannot evict a signature the user is still refusing.
_MAX_DECLINED_DISPOSITION_SIGNATURES = 8


def _note_engine_disposition_withdrawn(case: "Case", metadata: dict) -> None:
    """Mark the engine's own disposition offer as withdrawn for the rest of
    this turn.

    Withdrawal is not refusal — the durable record is a separate, narrower
    decision. This exists because the withdrawing branches fall through to
    normal processing, which reaches the engine proposers
    (``_maybe_propose_deferred_close``, ``_maybe_propose_confirmed_resolution``)
    again on the SAME turn and re-proposes the offer the user just moved past,
    re-taking the affordances with it (fm#1122). Scoped to the turn so an
    offer the user merely asked a question about is back on the next one.
    """
    if (getattr(case, "pending_transition", None) or {}).get("justifying_signature"):
        metadata[_ENGINE_DISPOSITION_WITHDRAWN_KEY] = True


def _record_deferred_disposition_decline(
    case: "Case", *, superseded_by: "str | None" = None
) -> None:
    """Persist that the user refused an ENGINE-proposed offer, against the
    state that justified it.

    Shared by both engine proposers (``_maybe_propose_deferred_close`` and
    ``_maybe_propose_confirmed_resolution``), which key the SAME signature
    space on purpose: both offer a disposition off the same justifying state,
    so one refusal has to silence both or the user is asked the settled
    question again on the next turn by the other one.

    **A declined RESOLVE binds regardless of who asked.** The offer carries a
    ``justifying_signature`` only when an ENGINE proposer wrote one, so a
    decline of an LLM- or user-opened offer used to record nothing — and
    ``_maybe_propose_confirmed_resolution``, which fires on readiness alone,
    re-proposed it on the very next turn. Measured: LLM opens RESOLVED, user
    types "no", next ordinary turn carries the offer again. That is fm#1122's
    shape arriving through a side door, and the backstop is what opened it —
    before INV-43 no engine proposer re-fired here. So for a RESOLVED target
    the signature is DERIVED from current state when the offer carries none:
    "not yet" is a statement about the CASE, not about who asked, and the
    proposer it must silence is keyed on the case.

    Scoped to a RESOLVED target on a SUGGEST_RESOLVE case. A declined CLOSE
    keeps the provenance rule — the deferred-close proposer's rationale ("the
    fix needs a change window") is not the LLM's rationale for closing, so one
    says nothing about the other, and no proposer re-fires on close from
    readiness alone.

    The two are not fully independent, and the honest statement is that they
    share one list: a derived SUGGEST_RESOLVE signature is also what
    ``_maybe_propose_deferred_close`` checks, so refusing a resolve on a
    DEFERRED case silences its close offer at that same justifying state too.
    That is the intended reading of the shared space — one refusal, one
    justifying state, every proposer keyed on it — not an accident to be
    designed around.

    Called from the withdrawal paths that constitute a REFUSAL, which is more
    than the explicit-decline branch: a contradicting status pick names a
    different target, and a long non-answer that is not a question is a
    deflection ("we'll do it in Friday's maintenance window"). Cancelling
    those unrecorded lets the proposer re-fire from unchanged state (fm#1122).

    NOT called for a question. ``message_is_substantive`` is true for ANY
    message containing "?" — "what happens to the runbook if I close this?"
    is a user deciding, not declining, and recording it would make the
    affordance vanish, unexplained, until a premise moved. The same-turn
    re-take those messages would otherwise cause is handled by
    ``_note_engine_disposition_withdrawn`` instead, which expires with the
    turn.
    """
    pending = getattr(case, "pending_transition", None) or {}
    if not getattr(case, "progress", None):
        return
    # A refusal the engine is about to OVERRIDE is not a refusal. When the
    # contradicting pick is CLOSE on a case the closure gate reads as
    # SUGGEST_RESOLVE, INV-37 pivots it straight back to a resolve proposal on
    # this same turn — so recording "the user refused resolve" would log
    # "not re-proposing until the justifying state changes" and then re-propose
    # in the next breath, while permanently poisoning the signature the
    # backstop keys on. Nothing was settled, so nothing is recorded.
    if (
        superseded_by == CaseState.CLOSED.value
        and pending.get("to_state") == CaseState.RESOLVED.value
    ):
        from faultmaven.core.investigation.terminal_transitions import (
            ClosureReadiness,
            closure_verdict,
        )

        if closure_verdict(case) == ClosureReadiness.SUGGEST_RESOLVE:
            return
    # Present only when an ENGINE proposer wrote it.
    signature = pending.get("justifying_signature")
    if not signature and pending.get("to_state") == CaseState.RESOLVED.value:
        # Derive it for an offer opened by the LLM or the user (see docstring).
        # Read at DECLINE time, which is the state the refusal is about: a
        # decline changes no evidence, so this is the same string the backstop
        # would compute next turn and it therefore suppresses exactly that
        # offer — while a later offer justified by a DIFFERENT state (another
        # solution, a cause established on another leg) still gets through.
        #
        # ONLY on SUGGEST_RESOLVE, which is the verdict the backstop's own
        # signature carries. A ``needs_info`` resolve pending can reach here
        # too — the contradicting-status-pick arm runs BEFORE 0b's
        # ``elif not needs_info`` — and on that case the verdict is
        # HAS_SUBSTANCE or TRIVIAL. Recording one of those suppresses no
        # resolve offer (no resolve proposer ever computes it) while landing
        # in the space ``_maybe_propose_deferred_close`` checks, and the list
        # is bounded at 8 with oldest-first eviction: an unusable signature
        # can push out a refusal that was still doing work.
        from faultmaven.core.investigation.terminal_transitions import (
            ClosureReadiness,
            assess_closure_readiness,
            deferred_disposition_signature,
        )

        verdict = assess_closure_readiness(case).verdict
        if verdict == ClosureReadiness.SUGGEST_RESOLVE:
            signature = deferred_disposition_signature(case, verdict)
    if not signature:
        return
    declined = case.progress.deferred_disposition_declined_signatures
    if signature in declined:
        return
    declined.append(signature)
    # Bounded: a case that oscillates between two justifying states could
    # otherwise grow the progress blob without limit. Oldest first — the
    # signatures most likely to recur are the recent ones.
    del declined[:-_MAX_DECLINED_DISPOSITION_SIGNATURES]
    logger.info(
        "Engine-proposed disposition refused for case %s; not "
        "re-proposing until the justifying state changes (signature=%s)",
        case.case_id,
        signature,
    )


# Placeholders the engine writes when the model gave no usable
# ``agent_response`` (#1442), one per failure shape the provider's NORMALISED
# stop reason can name. Each says what happened and none speaks as the agent:
# they are server text, and the row carrying one is flagged
# ``agent_response_synthesized`` so no prompt renderer quotes it back to the
# model as its own words (#1451).
RESPONSE_WITHHELD_TEXT = "[Response withheld by safety filter]"
RESPONSE_TRUNCATED_TEXT = "[Response truncated due to token limit]"
RESPONSE_EMPTY_TEXT = "[No response content generated by model]"
RESPONSE_NO_SIGNAL_TEXT = "[No response generated]"


def synthesized_agent_response(stop_reason: StopReason) -> str | None:
    """The placeholder for an unusable ``agent_response``, by stop reason (#1442).

    Keyed on :class:`StopReason` only — never on a provider's own spelling
    (``"length"``, ``"max_tokens"``, ``"MAX_TOKENS"``), which the enum exists to
    hide. Every member has its own arm, and the function raises on a member it
    does not know rather than defaulting, so a sixth reason added to the enum
    fails here instead of silently borrowing another row's text:

    - ``CONTENT_FILTER`` is kept apart from ``MAX_TOKENS`` because the remedies
      are opposite — a bigger budget fixes a cut and only pays to be refused
      again on a safety block.
    - ``UNKNOWN`` is *no signal*, not *finished normally* (HuggingFace as we
      call it reports none), so it does not share ``STOP``'s text.
    - ``TOOL_CALLS`` gets **no** placeholder: a response that stopped to hand
      control to a tool is not a failure shape — it has no answer *yet*.
      Note what that arm does NOT cover: a response whose tool call IS the
      answer. A structured answer delivered through the schema tool reports
      ``TOOL_CALLS`` as its normal completion on most providers, and the two
      synthesis sites translate it first — see
      :func:`schema_answer_stop_reason`.
    """
    if stop_reason is StopReason.CONTENT_FILTER:
        return RESPONSE_WITHHELD_TEXT
    if stop_reason is StopReason.MAX_TOKENS:
        return RESPONSE_TRUNCATED_TEXT
    if stop_reason is StopReason.STOP:
        return RESPONSE_EMPTY_TEXT
    if stop_reason is StopReason.UNKNOWN:
        return RESPONSE_NO_SIGNAL_TEXT
    if stop_reason is StopReason.TOOL_CALLS:
        return None
    raise ValueError(f"no synthesis arm for stop reason {stop_reason!r}")


def schema_answer_stop_reason(response: Any) -> StopReason:
    """The stop reason of a response whose body IS the structured answer.

    Both engine synthesis sites — the single-shot structured path and the tool
    loop's schema-tool call — read a response that has already delivered the
    answer, whatever it contains. When that answer came through the schema
    TOOL, the provider reports it the way it reports any tool call: OpenAI,
    Anthropic, Groq, Fireworks, OpenRouter and the local OpenAI-compatible
    transport say ``tool_calls`` / ``tool_use``, Cohere ``tool_call``, all of
    which normalise to ``TOOL_CALLS``; only Gemini says ``STOP``. So at these
    sites ``TOOL_CALLS`` means "finished answering", which is ``STOP``.

    Passing it through raw would read every schema-tool answer as a tool
    HANDOFF — the one arm that writes no placeholder — and leave a blank answer
    on seven of nine providers to the service's blind backstop, which is the
    layer #1442 took synthesis away from. ``synthesized_agent_response`` keeps
    its ``TOOL_CALLS`` arm for a genuine handoff; this translation belongs to
    the call site, because only the call site knows the tool call was the
    answer. Every other reason passes through unchanged — a cut or filtered
    schema call is still a cut or a filter.
    """
    reason = normalize_stop_reason(getattr(response, "stop_reason", None))
    return StopReason.STOP if reason is StopReason.TOOL_CALLS else reason


def is_agent_response_synthesized(response_obj: Any) -> bool:
    """Whether the engine wrote *response_obj*'s ``agent_response`` itself.

    ``is True`` rather than truthiness: a test double's auto-attribute is a
    truthy Mock, and a Mock must not read as a synthesized turn.
    """
    return getattr(response_obj, "_agent_response_synthesized", False) is True


def _prose_with_gate_notice(llm_text: str | None, gate_text: str) -> str:
    """Compose an engine gate message WITH the LLM's reply instead of over it.

    Engine-owned gate turns (resolution needs-info, close pivot,
    RCA-infeasible closure) override the response so the user sees the
    canonical gate prompt. But the LLM's ``agent_response`` on those turns
    often carries the substantive analysis the user just asked for — and its
    ``state_updates`` are already applied and persisted — so replacing the
    prose wholesale makes the engine appear to have ignored the question
    while the case record shows it answered (#656 turns 10-11: configmap
    analyses created hypotheses+solutions, yet the transcript showed only
    the canned resolution ask). The prose is therefore preserved and the
    gate message appended below a separator.

    Scope: prose ONLY. Follow-up *suggestions* on gate turns remain
    engine-owned and are still replaced outright — that is a separate,
    deliberate ownership decision (the #428 "augment" experiment was
    reverted by #430). Do not extend this composition to suggestions.
    """
    llm_text = (llm_text or "").strip()
    if not llm_text:
        return gate_text
    return f"{llm_text}\n\n---\n\n{gate_text}"


# INV-40 (§7.9) / INV-15 (§1.3.1): the disposition-completion phrases scanned by
# BOTH the narration-truth guard (``_narration_overclaim_notice``) and the
# ``transition_compliance`` telemetry. Module-level so the two read the SAME
# narrow list — PR #299 ratified keeping this scan narrow (only high-signal
# transition-completion claims; the broader advisor-role banned list is NOT here
# because it false-positives in benign context). Adding a phrase widens both the
# guard and the telemetry; do so deliberately.
_COMPLETION_PHRASES: tuple[str, ...] = (
    "case closed",
    "case is closed",
    "case is now closed",
    "marking as resolved",
    "marking this as resolved",
    "marking this resolved",
    "marked as resolved",
    "case resolved",
    "case is resolved",
    "case is now resolved",
    "i have resolved",
    "i've resolved",
    "i have closed",
    "i've closed",
)


def _narration_asserts_disposition(agent_text: str | None) -> bool:
    """True if the finalized narration contains a disposition-completion phrase.

    The detector half of INV-40 and the ``transition_compliance`` telemetry —
    the same narrow ``_COMPLETION_PHRASES`` scan, reused verbatim (INV-15).
    """
    lowered = (agent_text or "").lower()
    return any(p in lowered for p in _COMPLETION_PHRASES)


# INV-40 corrective notice. Appended (never substituted) below over-claiming
# prose. Both variants are true on a false positive too (conditional/quoted
# prose): the case IS non-terminal, so the worst case is a mildly-
# redundant-but-true notice (§7.9 graceful denial).
#
# Phase-neutral wording. The guard fires on any non-terminal turn — including
# INQUIRY (intake), which reaches the same response-composition block — so the
# notice must be true in both INQUIRY and INVESTIGATING. It therefore asserts
# only "not resolved/closed — still open" (true in every non-terminal phase),
# NOT "under investigation" (false during intake).
#
# Two wordings because the over-claim reaches the guard in two truth-shapes:
#   - no pending transition — the plain over-claim (#668's shape): nothing is
#     even on the table, so point at what resolution requires.
#   - a transition IS proposed (the LLM narrated "resolved" AND emitted
#     proposed_transition this turn, the suggestions-only override branch that
#     appends no gate prose): the claim is premature, not false-forever — the
#     confirm/decline affordances are right below, so point the user at them.
_NARRATION_OVERCLAIM_NOTICE = (
    "**Note:** this case has not been resolved or closed — it is still open. "
    "Resolving it requires a confirmed root cause and a verified fix; closing it "
    "requires an explicit decision to stop. I'll surface the confirm-to-resolve "
    "step when the case actually reaches it."
)
_NARRATION_OVERCLAIM_NOTICE_PENDING = (
    "**Note:** this case is not resolved or closed yet — a transition has only "
    "been *proposed*. It takes effect only when you confirm it using the options "
    "below."
)
