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

from typing import Any, Dict, List

__all__ = ["kb_push_enabled", "visible_kb_context"]


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
    entries = getattr(case, "kb_context", None) or []
    return [entry for entry in entries if isinstance(entry, dict)]
