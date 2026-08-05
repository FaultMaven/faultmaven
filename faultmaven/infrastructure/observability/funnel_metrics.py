"""Investigation funnel metrics as a projection of durable case state.

The case funnel (inquiry -> investigating -> resolved/closed) is fully persisted
and indexed in the ``cases`` table, so these metrics are collected by QUERYING
that source of truth on an interval -- NOT by incrementing counters at the
transition sites. A projection cannot drift, double-count, or miss a transition
path; it reports the actual outcome the case already records. See ADR 005
(case lifecycle terminology + measurement model) for the rationale that replaced
the earlier counter instrumentation.

Gauges (no-ops unless ``ENABLE_METRICS=true``; see ``shims/metrics.py``):

- ``faultmaven_cases{state, closure_reason}`` -- the funnel: count of ONLINE
  cases by workflow state. ``closure_reason`` sub-classifies CLOSED
  (``inquiry_only`` / ``solution_deferred`` / ``closed_rca_infeasible`` /
  ``mitigation_sufficient`` / ``closed_insufficient_evidence``
  / ``unknown`` for closes that reached terminal without a classified reason --
  the integrity gap D4 closes). ``none`` for non-closed states.
- ``faultmaven_case_resolution_turns_quantile{to_state, quantile}`` -- p50/p95
  of turns-to-terminal over the investigation population (resolved +
  every close except inquiry_only; inquiry_only is
  excluded -- it did no investigation). Effort, not wall-clock. Insufficient-
  evidence closes are included because they crossed the work gate (real
  diagnostic effort) before hitting the data wall.

All cases are ONLINE today (no archive tier exists yet -- see the Case Data
Archival & Retention problem statement). When archival lands, scope the queries
to the online tier.
"""

import asyncio
import logging
import math
from collections.abc import Sequence

from sqlalchemy import text

from faultmaven.infrastructure.shims.metrics import Gauge

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 30

# Bounded label domains, enumerated each refresh so a combination that drops to
# zero is reset (a gauge otherwise retains its last value for vanished series).
_OPEN_OR_RESOLVED = ("inquiry", "investigating", "resolved")
_CLOSED_REASONS = (
    "inquiry_only",
    "solution_deferred",
    "closed_rca_infeasible",
    "mitigation_sufficient",
    "closed_insufficient_evidence",
    "unknown",
)
_QUANTILES = (("0.5", 0.50), ("0.95", 0.95))
# Terminal outcomes that represent real diagnostic effort (turns-to-terminal).
# Only inquiry_only is excluded — no investigation ran, so there is no effort to
# measure. Every other close did the work, whatever it ended up establishing.
_EFFORT_TO_STATES = (
    "resolved",
    "solution_deferred",
    "closed_rca_infeasible",
    "mitigation_sufficient",
    "closed_insufficient_evidence",
)

cases_gauge = Gauge(
    "faultmaven_cases",
    "Online cases by workflow state; closure_reason sub-classifies CLOSED "
    "(inquiry_only / solution_deferred / closed_rca_infeasible / "
    "mitigation_sufficient / closed_insufficient_evidence / unknown).",
    labelnames=["state", "closure_reason"],
)

resolution_turns_quantile = Gauge(
    "faultmaven_case_resolution_turns_quantile",
    "Turns-to-terminal quantiles over the investigation population (every "
    "terminal outcome except inquiry_only, which ran no investigation). "
    "Effort, not wall-clock.",
    labelnames=["to_state", "quantile"],
)


def _percentile(values: Sequence[int], q: float) -> float:
    """Nearest-rank percentile of an integer sample; 0.0 for an empty sample."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return float(ordered[idx])


class FunnelMetricsCollector:
    """Refreshes the funnel gauges from the ``cases`` table on an interval."""

    async def refresh(self) -> None:
        """Query the source of truth and republish every gauge series."""
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            count_rows = (
                await session.execute(
                    text(
                        "SELECT state, closure_reason, COUNT(*) AS n "
                        "FROM cases GROUP BY state, closure_reason"
                    )
                )
            ).all()
            for (state, reason), n in self._normalize_counts(count_rows).items():
                cases_gauge.labels(state=state, closure_reason=reason).set(n)

            effort_rows = (
                await session.execute(
                    text(
                        "SELECT state, closure_reason, current_turn FROM cases "
                        "WHERE state = 'resolved' OR (state = 'closed' AND "
                        "closure_reason IN ('solution_deferred', "
                        "'closed_rca_infeasible', 'mitigation_sufficient', "
                        "'closed_insufficient_evidence'))"
                    )
                )
            ).all()
            self._set_quantiles(effort_rows)

    def _normalize_counts(self, rows: Sequence) -> dict:
        # Seed the full bounded matrix at 0 so vanished combinations reset.
        out: dict = {(s, "none"): 0 for s in _OPEN_OR_RESOLVED}
        for reason in _CLOSED_REASONS:
            out[("closed", reason)] = 0

        for state, reason, n in rows:
            state = (state or "").lower()
            if state == "closed":
                key = ("closed", reason if reason in _CLOSED_REASONS else "unknown")
            elif state in _OPEN_OR_RESOLVED:
                key = (state, "none")
            else:
                continue  # forward/unknown state value -- not part of the funnel
            out[key] = out.get(key, 0) + int(n)
        return out

    def _set_quantiles(self, rows: Sequence) -> None:
        buckets: dict = {to_state: [] for to_state in _EFFORT_TO_STATES}
        for state, reason, turn in rows:
            # Resolved keys on state; closes key on their closure_reason (the
            # effort query admits only the two effort-bearing close reasons).
            to_state = "resolved" if state == "resolved" else reason
            if to_state not in buckets:
                continue
            buckets[to_state].append(int(turn or 0))
        for to_state in _EFFORT_TO_STATES:
            for qlabel, q in _QUANTILES:
                resolution_turns_quantile.labels(
                    to_state=to_state, quantile=qlabel
                ).set(_percentile(buckets[to_state], q))

    async def run_periodic(self, interval: int = REFRESH_INTERVAL_SECONDS) -> None:
        """Background loop: refresh the gauges every ``interval`` seconds."""
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a query hiccup must not kill the loop
                logger.warning("Funnel metrics refresh failed (non-fatal): %s", e)
            await asyncio.sleep(interval)


collector = FunnelMetricsCollector()
