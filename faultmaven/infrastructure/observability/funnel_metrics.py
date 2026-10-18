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
  (``inquiry_only`` / ``closed_false_alarm`` / ``solution_deferred`` /
  ``closed_rca_infeasible`` /
  ``mitigation_sufficient`` / ``closed_restatement_held`` /
  ``closed_insufficient_evidence``
  / ``unknown`` for closes that reached terminal without a classified reason --
  the integrity gap D4 closes). ``none`` for non-closed states.
- ``faultmaven_case_resolution_turns_quantile_7d{to_state, quantile}`` -- p50/p95
  of turns-to-terminal over the investigation population (resolved +
  every close except inquiry_only; inquiry_only is
  excluded -- it did no investigation) closed in the trailing 7 days
  (``closed_at``). Effort, not wall-clock. Insufficient-
  evidence closes are included because they crossed the work gate (real
  diagnostic effort) before hitting the data wall. NaN when the window holds
  none: 0.0 would read as a 0-turn resolution.
- ``faultmaven_case_duration_seconds_quantile_7d{to_state, quantile}`` -- p50/p95
  of wall-clock seconds from creation to the terminal transition
  (``closed_at - created_at``) over the same population and window. A case with
  no usable timestamp pair contributes nothing; NaN when the window is empty.

The window filter has two stages because SQLite stores ``closed_at`` as text in
several shapes (space or ``T`` separator, offsets, the legacy ``+00:00Z``), so a
bound datetime compares wrongly inside the boundary day. SQL applies a coarse
prefilter one day wider than the window, which can only over-select and bounds
the fetch; the exact cut is made in Python through ``_as_utc``.

All cases are ONLINE today (no archive tier exists yet -- see the Case Data
Archival & Retention problem statement). When archival lands, scope the queries
to the online tier.
"""

import asyncio
import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Optional

from sqlalchemy import DateTime, bindparam, text

from faultmaven.infrastructure.shims.metrics import Gauge
from faultmaven.utils.datetime import parse_utc_timestamp

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 30

# Trailing window of the two quantile gauges, on closed_at (#1810 ruling, 2026-09-30).
QUANTILE_WINDOW = timedelta(days=7)
# The SQL prefilter is this much wider than the window, so it can only over-select.
_PREFILTER_MARGIN = timedelta(days=1)

# Bounded label domains, enumerated each refresh so a combination that drops to
# zero is reset (a gauge otherwise retains its last value for vanished series).
_OPEN_OR_RESOLVED = ("inquiry", "investigating", "resolved")
_CLOSED_REASONS = (
    "inquiry_only",
    "closed_false_alarm",
    "solution_deferred",
    "closed_rca_infeasible",
    "mitigation_sufficient",
    "closed_restatement_held",
    "closed_insufficient_evidence",
    "unknown",
)
_QUANTILES = (("0.5", 0.50), ("0.95", 0.95))
# Terminal outcomes that represent real diagnostic effort (turns-to-terminal).
# Only inquiry_only is excluded — no investigation ran, so there is no effort to
# measure. Every other close did the work, whatever it ended up establishing.
_EFFORT_TO_STATES = (
    "resolved",
    "closed_false_alarm",
    "solution_deferred",
    "closed_rca_infeasible",
    "mitigation_sufficient",
    "closed_restatement_held",
    "closed_insufficient_evidence",
)

cases_gauge = Gauge(
    "faultmaven_cases",
    "Online cases by workflow state; closure_reason sub-classifies CLOSED "
    "(inquiry_only / closed_false_alarm / solution_deferred / "
    "closed_rca_infeasible / "
    "mitigation_sufficient / closed_restatement_held / "
    "closed_insufficient_evidence / unknown).",
    labelnames=["state", "closure_reason"],
)

resolution_turns_quantile_7d = Gauge(
    "faultmaven_case_resolution_turns_quantile_7d",
    "Turns-to-terminal quantiles over the investigation population (every "
    "terminal outcome except inquiry_only, which ran no investigation) closed "
    "in the trailing 7 days (closed_at); NaN when none. "
    "Effort, not wall-clock.",
    labelnames=["to_state", "quantile"],
)

duration_seconds_quantile_7d = Gauge(
    "faultmaven_case_duration_seconds_quantile_7d",
    "Wall-clock seconds from creation to the terminal transition "
    "(closed_at - created_at), quantiles over the investigation population "
    "(every terminal outcome except inquiry_only) closed in the trailing 7 days "
    "(closed_at); NaN when none.",
    labelnames=["to_state", "quantile"],
)


def _as_utc(value: object) -> Optional[datetime]:
    """Read a ``cases`` timestamp as an aware UTC datetime, or None.

    A raw ``text()`` select returns ISO strings on SQLite, parsed with
    ``parse_utc_timestamp`` (which also reads the legacy ``+00:00Z`` shape), and
    aware datetimes on PostgreSQL. A naive value is taken as UTC; an aware one
    is converted to UTC. None or unparseable gives None.
    """
    if isinstance(value, str):
        try:
            value = parse_utc_timestamp(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of a numeric sample; NaN for an empty sample."""
    if not values:
        return math.nan
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return float(ordered[idx])


class FunnelMetricsCollector:
    """Refreshes the funnel gauges from the ``cases`` table on an interval."""

    async def refresh(self, *, now: Optional[datetime] = None) -> None:
        """Query the source of truth and republish every gauge series.

        ``now`` anchors the trailing quantile window; it defaults to the clock.
        It is normalised to UTC (a naive value is taken as UTC): SQLite compares
        the bound floor as wall-clock text, so any other offset would move it.
        """
        now = _as_utc(now) if now is not None else datetime.now(UTC)
        cutoff = now - QUANTILE_WINDOW
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
                        "SELECT state, closure_reason, current_turn, created_at, "
                        "closed_at FROM cases "
                        "WHERE (state = 'resolved' OR (state = 'closed' AND "
                        "closure_reason IN ('closed_false_alarm', "
                        "'solution_deferred', "
                        "'closed_rca_infeasible', 'mitigation_sufficient', "
                        "'closed_restatement_held', "
                        "'closed_insufficient_evidence'))) "
                        "AND closed_at >= :floor"
                    ).bindparams(bindparam("floor", type_=DateTime(timezone=True))),
                    {"floor": cutoff - _PREFILTER_MARGIN},
                )
            ).all()
            # Exact cut: the SQL floor is only a coarse over-selecting prefilter.
            windowed = [
                row
                for row in effort_rows
                if (closed := _as_utc(row[4])) is not None and closed >= cutoff
            ]
            self._set_quantiles(windowed)
            self._set_durations(windowed)

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
        for state, reason, turn, *_ in rows:
            # Resolved keys on state; closes key on their closure_reason (the
            # effort query admits every close reason except inquiry_only).
            to_state = "resolved" if state == "resolved" else reason
            if to_state not in buckets:
                continue
            buckets[to_state].append(int(turn or 0))
        for to_state in _EFFORT_TO_STATES:
            for qlabel, q in _QUANTILES:
                resolution_turns_quantile_7d.labels(
                    to_state=to_state, quantile=qlabel
                ).set(_percentile(buckets[to_state], q))

    def _set_durations(self, rows: Sequence) -> None:
        buckets: dict = {to_state: [] for to_state in _EFFORT_TO_STATES}
        for state, reason, _turn, created_at, closed_at in rows:
            to_state = "resolved" if state == "resolved" else reason
            if to_state not in buckets:
                continue
            start, end = _as_utc(created_at), _as_utc(closed_at)
            if start is None or end is None or end < start:
                continue
            buckets[to_state].append((end - start).total_seconds())
        for to_state in _EFFORT_TO_STATES:
            for qlabel, q in _QUANTILES:
                duration_seconds_quantile_7d.labels(
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
