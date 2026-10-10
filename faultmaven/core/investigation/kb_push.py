"""The KB PUSH channel's policy gate, in one place (fm#1360).

Knowledge reaches the model two ways. The PULL channel is the ``kb_qa`` tool,
which the model elects. The PUSH channel is
``MilestoneEngine._prefetch_kb_context``: a deterministic search fired at two
case transitions whose admitted hits are written to ``case.kb_context``.
``KB_PREFETCH_ENABLED`` governs the push and nothing else.

**Why this module exists rather than a check at each reader.** The field has
three consumers — the prompt (``context_builder``), the turn response's
``sources`` (``investigation_service``) and the ``case_turn`` telemetry
(``case_telemetry``) — and the first version of #1360 gated only the prompt.
The other two kept reading ``case.kb_context`` directly, so with the push
disabled a turn rendered no ``<knowledge_context>`` block and then cited the
runbooks anyway: ``sources`` listed two documents the model never saw and
``kb_prefetch_hits`` reported 2. That is worse than not shipping the flag,
because the telemetry that exists to make the push's cost/benefit measurable
reported it active while it was off.

**Why the gate cannot live only in the pre-fetch.** ``_prefetch_kb_context``
clears ``case.kb_context`` when the push is disabled, but it is EDGE-triggered:
one call at the INQUIRY→INVESTIGATING transition and one behind the
cause-identification edge. A case already past both never re-enters it, so a
value persisted while the push was enabled is never cleared. The same staleness
reaches a deployment whose knowledge service is ``None``, because that guard
returns before the flag is even read. The clearing branch is still worth having
— it stops the value being rewritten — but the readers are where the invariant
has to hold.

The invariant, stated once: **with the push disabled, no consumer sees a
pre-fetched runbook.** Read the field through :func:`visible_kb_context` and
that is true by construction.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

__all__ = [
    "KB_PROMPT_MAX_ENTRIES",
    "TURN_METADATA_KB_PROMPTED",
    "kb_entries_rendered",
    "kb_push_enabled",
    "prompt_kb_entries",
    "visible_kb_context",
    "kb_context_is_stale",
]

#: How many pre-fetched entries a prompt renders.
KB_PROMPT_MAX_ENTRIES = 5

#: Engine turn-metadata key: the entries THIS turn's prompt rendered, as the
#: prompt build reports them (``kb_entries_rendered``). The turn response's ``sources`` and the assistant row are
#: built from it, never from ``case.kb_context`` after the turn: a pre-fetch
#: that fires while the turn's response is applied (Gate 1, the root-cause
#: edge) writes context the answer never saw, and only the NEXT prompt carries.
TURN_METADATA_KB_PROMPTED = "kb_prompted"


def kb_push_enabled() -> bool:
    """Is the KB push enabled for this deployment? (``KB_PREFETCH_ENABLED``)

    Settings are imported inside the call and the read is guarded, because the
    consumers include helpers that unit tests import without ever building a
    settings object.

    Falls back to ``True`` — the shipped default — when settings cannot be read.
    The direction is deliberate: this gate decides whether retrieved knowledge
    is REMOVED, so an unreadable configuration must leave behaviour as it was
    rather than silently strip a runbook out of a prompt.
    """
    try:
        from faultmaven.config.settings import get_settings

        return bool(get_settings().knowledge.kb_prefetch_enabled)
    except Exception:  # noqa: BLE001 - settings absent in some test contexts
        return True


def visible_kb_context(case: Any) -> List[Dict[str, Any]]:
    """The pre-fetched runbooks this turn is allowed to act on.

    ``[]`` when the push is disabled, whatever the case still carries — which
    is the whole point, since ``case.kb_context`` is persisted and outlives the
    flag being turned off.

    Returns a NEW list. Callers extend it, sort it and slice it; handing back
    the case's own list would let one consumer mutate what the next reads.
    """
    if not kb_push_enabled():
        return []
    if kb_context_is_stale(case):
        return []
    entries = getattr(case, "kb_context", None) or []
    return [entry for entry in entries if isinstance(entry, dict)]


def kb_context_is_stale(case: Any) -> bool:
    """Whether the case carries pre-fetched context fetched for a driver who
    no longer drives it (ADR-020 D9).

    Read through :func:`visible_kb_context` by every consumer, so stale context
    is hidden from the prompt, the turn's sources, telemetry and reports from
    the moment the driver changes — not from the next turn. Context with no
    recorded origin was fetched for the creator (every fetch before the origin
    existed was keyed on ``cases.user_id``).
    """
    if not getattr(case, "kb_context", None):
        return False
    origin = getattr(case, "kb_context_origin", None) or {}
    fetched_for = origin.get("driver_id") or getattr(case, "user_id", None)
    effective = getattr(case, "effective_driver_id", None) or getattr(
        case, "user_id", None
    )
    return fetched_for != effective


def prompt_kb_entries(case: Any) -> List[Dict[str, Any]]:
    """The pre-fetched entries a prompt built from ``case`` now renders.

    The ONE selection the prompt builder renders from; what survives the
    section budget is then reported by :func:`kb_entries_rendered`. Copies, so
    a report cannot be changed by a later write to ``case.kb_context``.
    """
    return [dict(entry) for entry in visible_kb_context(case)[:KB_PROMPT_MAX_ENTRIES]]


#: A rendered entry's header in ``<knowledge_context>`` (``MATCH 1: <title>``).
_MATCH_HEADER = re.compile(r"^MATCH (\d+):", re.MULTILINE)


def kb_entries_rendered(
    section: str, entries: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The ``entries`` whose header survived in a rendered KB ``section``.

    ``section`` is the allocated ``kb_results`` text — the KB block alone, after
    the section budget has truncated it, never the whole prompt, so no other
    section's text can read as a match. Under budget pressure the block keeps
    its head, so a trailing entry can be cut whole; one whose header survived
    was shown, if only in part.
    """
    shown = sorted({int(n) for n in _MATCH_HEADER.findall(section or "")})
    return [dict(entries[n - 1]) for n in shown if 1 <= n <= len(entries)]
