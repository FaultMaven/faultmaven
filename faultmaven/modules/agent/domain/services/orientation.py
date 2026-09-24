"""Orientation turns: greetings, "help", and the empty message.

Three inputs used to share one static reply — "Hello! I'm FaultMaven ... Please
describe the problem you're observing." — or worse:

* a greeting anywhere in the case's life got onboarding text, so "hi" at turn
  7 of an investigation asked the user what the problem was;
* ``help`` matched the greeting pattern, so a user asking for help got the
  same onboarding text;
* an empty message (a bare ``@FaultMaven`` in Slack) was refused by the route
  with a 400 the client had to swallow, so nothing happened at all.

This module answers all three from the case's own state, with no LLM call:
where the investigation stands, what was last asked for, and what the user
can do next. The service mints the intent from the text (or from its
absence); a client cannot send it — see ``InvestigationService.process_turn``.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional

from faultmaven.core.investigation.evidence_need_surfacing import is_ask_exhausted
from faultmaven.modules.case.contracts import (
    CaseState,
    is_default_case_title,
    is_server_written_assistant_row,
)

#: Marker on both message rows of a turn answered outside the investigation.
#: Shares the ``out_of_band`` key with #1329's asides so every reader that
#: hides or discounts an aside (history renderers, the investigation-turn
#: count) treats an orientation reply the same way without a second key.
OUT_OF_BAND_MARKER = "orientation"

_FM = r"(?:,?\s*faultmaven)?"
_TAIL = r"[\s.!?,]*$"

GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|greetings|howdy|yo|good (?:morning|afternoon|evening))"
    r"(?: there)?" + _FM + _TAIL,
    re.I,
)
# Whole-message requests for help ABOUT THE ASSISTANT. Deliberately not "?",
# "what should I do", "how does this work", "what do you do": mid-investigation
# those are next-step questions the engine must answer with reasoning (and
# "?"/blank over a pending gate has its own engine handling). Only phrasings
# that cannot be read as a question about the incident qualify.
HELP_RE = re.compile(
    r"^(?:help|help me|help please|what can you do|how can you help(?: me)?)"
    + _FM
    + _TAIL,
    re.I,
)

#: How much of the last investigation ask is quoted back.
_ASK_CHARS = 220
_TITLE_CHARS = 120


class OrientationKind(str, Enum):
    GREETING = "greeting"
    HELP = "help"
    EMPTY = "empty"


#: Transcript text for a turn carrying no message at all — a bare
#: ``@FaultMaven``. The row is the user's half of a turn that really happened:
#: it is charged against the tenant cap and it advances the message clock, so
#: it is not something to omit. But ``case_messages`` requires non-blank
#: content, and writing ``""`` made the whole aggregate save fail its CHECK
#: constraint — taking the case row, its evidence and its hypotheses with it
#: (#1420).
#:
#: A marker rather than first-person prose, because the user said nothing and
#: the row must not pretend otherwise. What the turn WAS is already recorded
#: beside it, in the message metadata (``orientation: "empty"``).
EMPTY_TURN_TEXT = "(no message)"

#: Transcript text for an assistant row that arrived with no content (#1433).
#:
#: Deliberately NOT the same marker as ``EMPTY_TURN_TEXT``: an empty USER
#: message means the user chose to send nothing, while an empty ASSISTANT
#: message means the turn produced no answer, and a transcript a human reads to
#: understand an incident must not present the second as the first.
#:
#: Equally deliberately, it names NO CAUSE. The write site it guards is reached
#: after every dispatch kind converges, and two SERVICE intents — GREETING and
#: FILE_RECLASSIFICATION — answer with no LLM call at all, so text blaming "the
#: model" would, on those paths, blame something that never ran. (The other
#: SERVICE intents do delegate to ``engine.process_turn``; an earlier version
#: of this comment claimed otherwise, which is the same kind of unchecked
#: claim it replaced.)
#:
#: It is stored rather than refused because the alternative is worse: the row
#: is part of an aggregate save, so a blank one aborts the whole thing and
#: takes the user's turn, the evidence and the hypotheses with it — for a turn
#: already charged against the tenant cap.
EMPTY_AGENT_RESPONSE_TEXT = "(this turn produced no answer)"


def detect_orientation(message: Optional[str]) -> Optional[OrientationKind]:
    """Which orientation a text-only message asks for, or ``None``.

    Whole-message matches only. "Hi, the db is down" and "help, nginx is
    returning 502s" are incident turns and must fall through.
    """
    text = (message or "").strip()
    if not text:
        return OrientationKind.EMPTY
    if GREETING_RE.match(text):
        return OrientationKind.GREETING
    if HELP_RE.match(text):
        return OrientationKind.HELP
    return None


# --------------------------------------------------------------------------
# State recap
# --------------------------------------------------------------------------

_STAGE_PHRASE = {
    "diagnosis": "diagnosing the cause",
    "mitigation": "working on a mitigation",
    "treatment": "working on the fix",
}


def _capabilities() -> str:
    """The answer to "what can you do", stated deterministically.

    This lane resolves BEFORE the aside lanes — ``HELP_RE`` whole-message
    matches "help", "what can you do", "how can you help me" — and answers with
    no LLM in the loop, so the scope rules written into the aside prompts never
    apply to it. It therefore has to carry the territory itself, or the one
    surface most likely to be asked the question is the one that cannot answer
    it.
    """
    from faultmaven.modules.knowledge.contracts import (
        describe_troubleshooting_domains,
    )

    return (
        "I can investigate a problem you describe, analyze the logs, configs and "
        "metrics you share, search your team's runbooks and past fixes, and track "
        "hypotheses until a fix is verified. I work on troubleshooting engineering "
        f"systems — {describe_troubleshooting_domains()} — and a technology like "
        "Kubernetes, Linux or Windows can fall in several of those at once."
    )


def case_subject(case: Any, limit: int = _TITLE_CHARS) -> str:
    """The case's subject, or "" while it still carries the placeholder title.

    Auto-titling runs after the reply is generated and needs ~200 characters of
    user content, titling is best-effort, and older cases kept the placeholder
    forever — so ``Case-260905-3`` is a normal thing to find here, and quoting
    it as the subject reads as nonsense.

    Public and shared, because every surface that names the case to a user owes
    the same answer: the orientation chips, the aside prompts and the aside
    fallback. A second copy of this is a second place the placeholder contract
    lives, and that regex already needs two date widths.
    """
    title = str(getattr(case, "title", "") or "").strip()
    if is_default_case_title(title):
        return ""
    return title[:limit]


def _first_sentence(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "? ", "! "):
        idx = cut.rfind(sep)
        if idx > limit // 2:
            return cut[: idx + 1]
    return cut.rstrip() + "…"


def _pending_need(case: Any) -> Optional[str]:
    """The open evidence need the user was most recently ASKED for, or ``None``.

    Not the list tail: symptom needs are created in bulk and hydrated in
    creation order, so the tail is whichever was created last, not what was
    put to the user. A need qualifies only if it is outstanding, has actually
    been surfaced, and the engine has not stopped asking for it
    (``is_ask_exhausted`` — the #1079 decay rule this must not undo). Among
    those, the one surfaced most recently is "the last thing I asked for".
    """
    needs = getattr(case, "evidence_needs", None) or []
    current_turn = int(getattr(case, "current_turn", 0) or 0)
    best = None
    for need in needs:
        if not getattr(need, "is_outstanding", False):
            continue
        last = getattr(need, "last_surfaced_turn", None)
        if last is None or not getattr(need, "request_text", ""):
            continue
        if is_ask_exhausted(need, current_turn):
            continue
        if best is None or last > best[0]:
            best = (last, need)
    if best is None:
        return None
    return _first_sentence(str(best[1].request_text), _ASK_CHARS)


def last_investigation_message(case: Any, limit: int = _ASK_CHARS) -> Optional[str]:
    """The newest investigation answer the MODEL wrote, or ``None``.

    Three kinds of assistant row are passed over:

    * asides (#1329) and earlier orientation replies, so two greetings in a row
      do not quote the first recap back as "where we left off";
    * a placeholder the server wrote because the model gave no usable answer
      (#1660) — "[Response withheld by safety filter]", "(this turn produced
      no answer)". Quoting one would put server text in front of the triage
      classifier as the assistant's words, which #1451 forbids, and tell the
      user "Where we left off: [Response withheld by safety filter]".

    Skipped rather than marked, because neither reader is a transcript the
    model continues from: the triage prompt uses this to disambiguate a short
    reply, which the last thing the model actually asked settles, and the
    greeting quotes it back to the user. That is the auto-titler's reading of
    #1451, not the history renderers'.

    Shared with ``out_of_band`` for the same reason; one predicate, one place.
    """
    for msg in reversed(getattr(case, "messages", None) or []):
        if msg.get("role") != "assistant" or not msg.get("content"):
            continue
        meta = msg.get("metadata") or {}
        if meta.get("out_of_band") or meta.get("orientation"):
            continue
        if is_server_written_assistant_row(msg):
            continue
        return _first_sentence(str(msg["content"]), limit)
    return None


def describe_issue_follow_up() -> dict[str, Any]:
    """The one "tell me what is wrong" chip, for every lane that has no
    investigation to point back to.

    Shared so a user does not meet two names for the same action one turn
    apart: the greeting lane offered this wording first, and the aside lane
    needs it whenever a case has no investigation history yet.
    """
    return {
        "label": "Describe your issue",
        "action_type": "FREE_SPEECH",
        "hints": ["symptoms", "error messages", "timeline", "affected services"],
    }


def back_to_investigation_follow_up(case: Any) -> dict[str, Any]:
    """The one "resume the investigation" chip both aside lanes offer."""
    title = case_subject(case)
    return {
        "label": f"Back to: {title[:60]}" if title else "Back to the investigation",
        "action_type": "FREE_SPEECH",
        "hints": ["new data", "what you tried", "next step"],
    }


def _opener(kind: OrientationKind, fresh: bool) -> str:
    if kind == OrientationKind.GREETING:
        return (
            "Hello! I'm FaultMaven, your AI troubleshooting copilot."
            if fresh
            else "Hello!"
        )
    if kind == OrientationKind.HELP:
        return (
            "I'm FaultMaven, an AI troubleshooting copilot. " + _capabilities()
            if fresh
            else _capabilities()
        )
    return ""


def build_orientation(case: Any, kind: OrientationKind) -> dict[str, Any]:
    """The reply and follow-ups for an orientation turn, from case state alone.

    Returns ``{"agent_response": str, "suggested_follow_ups": list}`` in the
    shape the service's deterministic handlers already return.
    """
    state = getattr(case, "state", None)
    title = case_subject(case)
    inquiry = getattr(case, "inquiry", None)
    proposed = (
        str(getattr(inquiry, "proposed_problem_statement", "") or "").strip()
        if inquiry
        else ""
    )
    confirmed = bool(getattr(inquiry, "problem_statement_confirmed", False))

    # ── Terminal ──────────────────────────────────────────────────────
    if state in (CaseState.RESOLVED, CaseState.CLOSED):
        word = "resolved" if state == CaseState.RESOLVED else "closed"
        opener = _opener(kind, fresh=False)
        body = (
            f"This case is {word}"
            + (f": “{title}”." if title else ".")
            + " Its summary "
            "is in the Report tab. Ask me anything about how it was handled, or "
            "open a new case for a new problem."
        )
        return {
            "agent_response": " ".join(p for p in (opener, body) if p),
            "suggested_follow_ups": [
                {"label": "Ask about this case", "action_type": "FREE_SPEECH"},
            ],
        }

    # ── Investigating ─────────────────────────────────────────────────
    if state == CaseState.INVESTIGATING:
        stage = getattr(getattr(case, "current_stage", None), "value", None)
        phrase = _STAGE_PHRASE.get(stage or "", "investigating")
        opener = _opener(kind, fresh=False)
        where = (
            f"We're investigating “{title}” — {phrase}."
            if title
            else f"We're {phrase}."
        )
        need = _pending_need(case)
        ask = need or last_investigation_message(case)
        if need:
            last = f"Last I asked for: {need}"
        elif ask:
            last = f"Where we left off: {ask}"
        else:
            last = ""
        close = (
            "Share new data or tell me what you tried, and we pick it up from there."
        )
        follow_ups: list[dict[str, Any]] = []
        if need:
            follow_ups.append(
                {
                    "label": need[:80].rstrip("."),
                    "action_type": "EVIDENCE",
                    "body": "This is the data the investigation is waiting on.",
                }
            )
        follow_ups.append(
            {
                "label": (
                    f"Back to: {title[:60]}" if title else "Back to the investigation"
                ),
                "action_type": "FREE_SPEECH",
                "hints": ["new data", "what you tried", "next step"],
            }
        )
        return {
            "agent_response": " ".join(p for p in (opener, where, last, close) if p),
            "suggested_follow_ups": follow_ups,
        }

    # ── Inquiry, problem statement proposed but not yet confirmed ─────
    if proposed and not confirmed:
        # Gate 1 is pending on this turn like any other, so it gets the SAME
        # presentation and the SAME clickable pair the engine composes
        # everywhere else. This branch used to render its own wording, truncate
        # the statement at 200 characters mid-sentence, and offer a FREE_SPEECH
        # suggestion — so an orientation turn was the one pending turn with no
        # clickable consent path, showing a different (and possibly cut-off)
        # rendering of the same text (#1607).
        from faultmaven.core.investigation.milestone_engine import (
            _gate1_statement_presentation,
            _investigation_confirmation_suggestions,
        )

        opener = _opener(kind, fresh=False)
        body = _gate1_statement_presentation(case)
        return {
            "agent_response": "\n\n".join(p for p in (opener, body) if p),
            "suggested_follow_ups": _investigation_confirmation_suggestions(),
        }

    # ── Fresh inquiry ─────────────────────────────────────────────────
    opener = _opener(kind, fresh=True)
    if kind == OrientationKind.EMPTY:
        opener = "I'm FaultMaven, an AI troubleshooting copilot. " + _capabilities()
    body = (
        "To start, describe the problem you're seeing — symptoms, error messages, "
        "when it started — or share logs, configs or metrics."
    )
    return {
        "agent_response": " ".join(p for p in (opener, body) if p),
        "suggested_follow_ups": [
            describe_issue_follow_up(),
            {
                "label": "Share error logs from the affected service",
                "action_type": "EVIDENCE",
                "body": "Error logs will help identify the root cause faster.",
            },
        ],
    }
