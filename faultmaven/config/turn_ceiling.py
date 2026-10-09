"""How long a turn may take, for the chat provider in force (#1905).

The one owner of two numbers every reader of the turn's timing needs:

* **The ceiling** — ``AGENT_REQUEST_TIMEOUT``, or the chat provider's
  ``AGENT_PROVIDER_TIMEOUT_OVERRIDES`` entry. The turn route bounds the turn's
  preparation at it and binds it as the deadline the retry ladder spends
  against (``core/investigation/turn_budget``).
* **The response bound** — NOMINAL: the ceiling plus the two steps that may
  still run after it, the turn's commit (``TURN_COMMIT_RESERVE_SECONDS``, kept
  back from the ceiling's own budget but run outside its ``wait_for``) and the
  auto-title (``AUTO_TITLE_TIMEOUT_SECONDS``). Not a hard guarantee: it leaves
  out the work before the deadline is bound (case lookup, the in-flight claim,
  the receipt lookup), the commit's actual duration (the reserve is a measured
  p99 x 10, not a timeout) and the auto-title's case read before its own
  ``wait_for``. A client's own timeout is this plus a network margin that also
  covers those.

Resolved per call, never cached: the chat provider is a dashboard override
(``llm_config_overrides``' ``primary_provider``) that an operator can switch on
a running deployment, and the switch changes which override applies. Readers:
the turn route (deadline and in-flight claim), ``GET /api/v1/meta/capabilities``
(``limits.turnCeilingSeconds`` / ``limits.turnResponseBoundSeconds``),
``GET /admin/config/status`` (``turn_timing``) and the retry-ladder report
(``retry_budget``).

Only the chat provider's override applies. A fallback provider the router
reaches mid-turn runs inside the deadline already bound for the turn, so its
own override never lengthens the turn.

Lives in the config layer for the reason ``retry_budget`` does: it composes
settings with core and infrastructure constants, and the API layer imports it
rather than those. The imports are function-local for the same reason too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TurnCeiling:
    """The turn's time bounds, in seconds, for one resolved chat provider."""

    #: The chat provider the ceiling was resolved for; ``None`` when no provider
    #: is configured anywhere (the global ``AGENT_REQUEST_TIMEOUT`` applies).
    provider: Optional[str]
    #: The bound on the turn's preparation, and the deadline bound for it.
    ceiling_seconds: float
    #: The NOMINAL bound on the turn route's answer: ceiling + commit reserve +
    #: auto-title bound (the module docstring lists what it leaves out).
    response_bound_seconds: float


def resolve_turn_ceiling(settings) -> TurnCeiling:
    """The ceiling and response bound for *settings*' current chat provider."""
    from faultmaven.core.investigation.turn_budget import (
        AUTO_TITLE_TIMEOUT_SECONDS,
        TURN_COMMIT_RESERVE_SECONDS,
    )
    from faultmaven.infrastructure.llm.router import resolve_chat_provider_name

    # The SAME resolver the LLM router uses for its per-call timeout: the two
    # sides of the turn budget must agree on which provider they are about.
    provider = resolve_chat_provider_name(settings)
    ceiling = float(settings.agent.timeout_for_provider(provider))
    return TurnCeiling(
        provider=provider,
        ceiling_seconds=ceiling,
        response_bound_seconds=ceiling
        + TURN_COMMIT_RESERVE_SECONDS
        + AUTO_TITLE_TIMEOUT_SECONDS,
    )
