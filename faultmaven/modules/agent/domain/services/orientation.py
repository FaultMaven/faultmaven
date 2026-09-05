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

from faultmaven.modules.case.contracts import CaseState

_FM = r"(?:,?\s*faultmaven)?"
_TAIL = r"[\s.!?,]*$"

GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|greetings|howdy|yo|good (?:morning|afternoon|evening))"
    r"(?: there)?" + _FM + _TAIL,
    re.I,
)
HELP_RE = re.compile(
    r"^(?:help|help me|\?+|what can you do|how can you help(?: me)?|"
    r"what do you do|how does this work|what should i do)" + _FM + _TAIL,
    re.I,
)

#: How much of the last investigation ask is quoted back.
_ASK_CHARS = 220
_TITLE_CHARS = 120


class OrientationKind(str, Enum):
    GREETING = "greeting"
    HELP = "help"
    EMPTY = "empty"


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

_CAPABILITIES = (
    "I can investigate a problem you describe, analyze the logs, configs and "
    "metrics you share, search your team's runbooks and past fixes, and track "
    "hypotheses until a fix is verified."
)


def _title(case: Any) -> str:
    return str(getattr(case, "title", "") or "").strip()[:_TITLE_CHARS]


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
    """The newest evidence need still open, as the request the user was asked."""
    needs = getattr(case, "evidence_needs", None) or []
    for need in reversed(needs):
        state = getattr(getattr(need, "state", None), "value", None) or getattr(
            need, "state", None
        )
        if state in ("pending", "partially_met") and getattr(need, "request_text", ""):
            return _first_sentence(str(need.request_text), _ASK_CHARS)
    return None


def _last_investigation_ask(case: Any) -> Optional[str]:
    """The assistant's last investigation message — asides skipped (#1329)."""
    for msg in reversed(getattr(case, "messages", None) or []):
        if msg.get("role") != "assistant" or not msg.get("content"):
            continue
        if (msg.get("metadata") or {}).get("out_of_band"):
            continue
        return _first_sentence(str(msg["content"]), _ASK_CHARS)
    return None


def _opener(kind: OrientationKind, fresh: bool) -> str:
    if kind == OrientationKind.GREETING:
        return (
            "Hello! I'm FaultMaven, your AI troubleshooting copilot."
            if fresh
            else "Hello!"
        )
    if kind == OrientationKind.HELP:
        return (
            "I'm FaultMaven, an AI troubleshooting copilot. " + _CAPABILITIES
            if fresh
            else _CAPABILITIES
        )
    return ""


def build_orientation(case: Any, kind: OrientationKind) -> dict[str, Any]:
    """The reply and follow-ups for an orientation turn, from case state alone.

    Returns ``{"agent_response": str, "suggested_follow_ups": list}`` in the
    shape the service's deterministic handlers already return.
    """
    state = getattr(case, "state", None)
    title = _title(case)
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
        ask = need or _last_investigation_ask(case)
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
        opener = _opener(kind, fresh=False)
        body = (
            f"We were about to confirm the problem statement: “{proposed[:200]}”. "
            "Confirm it to start the investigation, or tell me what to change."
        )
        return {
            "agent_response": " ".join(p for p in (opener, body) if p),
            "suggested_follow_ups": [
                {
                    "label": "Confirm or refine the problem statement",
                    "action_type": "FREE_SPEECH",
                    "hints": ["yes, that's it", "what to change"],
                },
            ],
        }

    # ── Fresh inquiry ─────────────────────────────────────────────────
    opener = _opener(kind, fresh=True)
    if kind == OrientationKind.EMPTY:
        opener = "I'm FaultMaven, an AI troubleshooting copilot. " + _CAPABILITIES
    body = (
        "To start, describe the problem you're seeing — symptoms, error messages, "
        "when it started — or share logs, configs or metrics."
    )
    return {
        "agent_response": " ".join(p for p in (opener, body) if p),
        "suggested_follow_ups": [
            {
                "label": "Describe your issue",
                "action_type": "FREE_SPEECH",
                "hints": [
                    "symptoms",
                    "error messages",
                    "timeline",
                    "affected services",
                ],
            },
            {
                "label": "Share error logs from the affected service",
                "action_type": "EVIDENCE",
                "body": "Error logs will help identify the root cause faster.",
            },
        ],
    }
