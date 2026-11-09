"""The read side of the LLM usage ledger: one enterprise's usage report (#640, #1764).

``usage_ledger`` writes ``llm_usage_daily`` and ``llm_turn_spend``; this module
reads them back for ``GET /api/v1/admin/llm/usage``. It returns the report as
plain data in the ``LLMUsageReport`` shape (``api/models.py``), which the route
validates.

* **Scope.** Every query carries ``enterprise_id = :bound``. Under
  multi-tenancy RLS backs the same predicate, through the session's ``begin``
  binding.
* **History and the running table (#640 Q7).** ``unpriced_calls`` and
  ``cost_complete`` are read from stored columns: what was unpriced when each
  call was made. ``rate_status`` and the ``unpriced`` banner are read now, from
  the running price table, so a model priced after its calls leaves the banner
  and keeps its ``unpriced_calls``.
* **The budget.** The histogram edges and ``budget`` are the in-effect settings,
  passed in at request time, so history re-buckets against the current budget.
* **Failure.** A database error raises :class:`UsageStoreUnavailable`; the route
  answers 503, never a zero-filled report. Nothing is held between requests.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func, literal, select
from sqlalchemy.exc import SQLAlchemyError

from faultmaven.exceptions import DatabaseTimeout, UsageStoreUnavailable
from faultmaven.infrastructure.llm.pricing import rate_status

logger = logging.getLogger(__name__)

#: ``window`` → whole UTC days ending today, inclusive.
WINDOW_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}
HEAVIEST_TURNS = 10
_SUMMED = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "estimated_cost_usd",
    "calls",
    "unpriced_calls",
)
#: A day with no rows.
_NOTHING = SimpleNamespace(**dict.fromkeys(_SUMMED + ("kept", "low_confidence"), 0))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sums(daily):
    """The summed daily columns, with the call count split by outcome."""
    return [func.sum(getattr(daily, name)).label(name) for name in _SUMMED] + [
        func.sum(case((daily.outcome == outcome, daily.calls), else_=0)).label(outcome)
        for outcome in ("kept", "low_confidence")
    ]


def _bucket(row: Any) -> Dict[str, Any]:
    """One ``LLMUsageBucket`` from a row of :func:`_sums`."""
    n = {
        name: int(getattr(row, name) or 0)
        for name in _SUMMED
        if name != "estimated_cost_usd"
    }
    tokens = [n["input_tokens"], n["output_tokens"]]
    tokens += [n["cache_read_tokens"], n["cache_write_tokens"]]
    hit_base = n["input_tokens"] + n["cache_read_tokens"]
    return {
        "calls": {
            "total": n["calls"],
            "kept": int(row.kept or 0),
            "low_confidence": int(row.low_confidence or 0),
        },
        "tokens": dict(
            zip(("input", "output", "cache_read", "cache_write"), tokens),
            total=sum(tokens),
        ),
        "cache_hit_rate": n["cache_read_tokens"] / hit_base if hit_base else None,
        "estimated_cost_usd": round(float(row.estimated_cost_usd or 0.0), 6),
        "unpriced_calls": n["unpriced_calls"],
        "cost_complete": n["unpriced_calls"] == 0,
    }


def histogram_edges(soft_budget: int, ceiling: int) -> List[int]:
    """``0 | budget/2 | budget | ceiling``, then open-ended (#1764 A7).

    The budget edges are dropped when the budget is 0 (disabled), and the edges
    are sorted and de-duplicated, so a budget at or above the ceiling still
    gives strictly increasing bins.
    """
    edges = {0, ceiling}
    if soft_budget > 0:
        edges.update({soft_budget // 2, soft_budget})
    return sorted(edges)


def nearest_rank(sorted_values: List[int], percentile: int) -> Optional[int]:
    """The nearest-rank percentile of an ascending list; ``None`` when empty."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(percentile / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def _as_utc(value: datetime) -> datetime:
    # SQLite reads a DateTime(timezone=True) back naive; it was written as UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _group_key(group_by: str, row: Any) -> Dict[str, str]:
    if group_by == "provider_model":
        return {"provider": row.provider, "model": row.model}
    if group_by == "user":
        return {"user_id": row.actor_user_id}
    return {"kind": row.billing_subject_kind, "id": row.billing_subject_id}


async def read_usage_report(
    *, enterprise_id: str, window: str, group_by: str, soft_budget: int, ceiling: int
) -> Dict[str, Any]:
    """One enterprise's usage over ``window``, in the ``LLMUsageReport`` shape
    less ``scope``, which is the caller's.

    Raises:
        UsageStoreUnavailable: the database did not answer or refused a read.
    """
    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.infrastructure.persistence.models import (
        LlmTurnSpendModel,
        LlmUsageDailyModel,
    )

    daily, turn = LlmUsageDailyModel, LlmTurnSpendModel
    now = _utc_now()
    days = WINDOW_DAYS[window]
    last = now.date()
    first = last - timedelta(days=days - 1)
    # Aware UTC bounds only: SQLite strips an offset before comparing.
    start = datetime.combine(first, time.min, tzinfo=timezone.utc)
    end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=timezone.utc)

    in_daily = (
        daily.enterprise_id == enterprise_id,
        daily.usage_date >= first,
        daily.usage_date <= last,
    )
    in_turns = (
        turn.enterprise_id == enterprise_id,
        turn.occurred_at >= start,
        turn.occurred_at < end,
    )
    group_columns = {
        "user": (daily.actor_user_id,),
        "billing_subject": (daily.billing_subject_kind, daily.billing_subject_id),
    }.get(group_by)

    soft = soft_budget if soft_budget > 0 else None
    spend = turn.spend_weighted_tokens

    def _sum_over(limit: Optional[int]):
        return func.coalesce(func.sum(case((spend > limit, 1), else_=0)), 0)

    try:
        async with get_db_session() as session:

            async def rows(statement):
                return (await session.execute(statement)).all()

            def by(*columns):
                return (
                    select(*columns, *_sums(daily)).where(*in_daily).group_by(*columns)
                )

            [(metering_since,)] = await rows(
                select(func.min(daily.usage_date)).where(
                    daily.enterprise_id == enterprise_id
                )
            )
            day_rows = await rows(by(daily.usage_date))
            model_rows = await rows(by(daily.provider, daily.model))
            group_rows = (
                model_rows
                if group_by == "provider_model"
                else await rows(by(*group_columns))
            )
            [total_row] = await rows(select(*_sums(daily)).where(*in_daily))
            # One column only: the percentiles and the histogram are computed here,
            # over rows bounded at 90 days by retention, with no dialect-specific SQL.
            spends = sorted(
                int(s) for (s,) in await rows(select(spend).where(*in_turns))
            )
            [(over_soft, over_ceiling)] = await rows(
                select(
                    _sum_over(soft) if soft else literal(0), _sum_over(ceiling)
                ).where(*in_turns)
            )
            heaviest = (
                (
                    await session.execute(
                        select(turn)
                        .where(*in_turns)
                        .order_by(spend.desc(), turn.occurred_at.desc(), turn.case_id)
                        .limit(HEAVIEST_TURNS)
                    )
                )
                .scalars()
                .all()
            )
    except (DatabaseTimeout, SQLAlchemyError) as exc:
        # The TYPE only: a driver message carries the statement and its
        # parameters.
        logger.warning(
            "llm_usage_report_unavailable: the usage tables could not be read "
            "(error=%s)",
            type(exc).__name__,
        )
        raise UsageStoreUnavailable() from exc

    by_date = {row.usage_date: _bucket(row) for row in day_rows}
    groups = sorted(
        (
            {
                "key": _group_key(group_by, row),
                # Defined for model groups only: a user or a payer has no rate.
                "rate_status": (
                    rate_status(row.provider, row.model)
                    if group_by == "provider_model"
                    else None
                ),
                **_bucket(row),
            }
            for row in group_rows
        ),
        key=lambda g: (-g["tokens"]["total"], sorted(g["key"].items())),
    )
    edges = histogram_edges(soft_budget, ceiling)
    return {
        "window": {"from": first, "to": last, "days": days, "timezone": "UTC"},
        "coverage": {
            "metering_since": metering_since,
            "window_fully_covered": metering_since is not None
            and metering_since <= first,
        },
        "as_of": now,
        "totals": _bucket(total_row),
        "by_day": [
            {"date": day, **(by_date.get(day) or _bucket(_NOTHING))}
            for day in (first + timedelta(days=n) for n in range(days))
        ],
        "groups": groups,
        "unpriced": [
            {"provider": r.provider, "model": r.model, "calls": int(r.unpriced_calls)}
            for r in sorted(model_rows, key=lambda r: (-r.unpriced_calls, r.model))
            if r.unpriced_calls and rate_status(r.provider, r.model) == "unpriced"
        ],
        "turns": {
            "count": len(spends),
            "spend_weighted_tokens": {
                "p50": nearest_rank(spends, 50),
                "p95": nearest_rank(spends, 95),
                "max": spends[-1] if spends else None,
            },
            "budget": {"soft": soft, "ceiling": ceiling},
            "over_soft": int(over_soft),
            "over_ceiling": int(over_ceiling),
            "histogram": [
                {
                    "lower": lower,
                    "upper": upper,
                    "count": sum(
                        lower <= s and (upper is None or s < upper) for s in spends
                    ),
                }
                for lower, upper in zip(edges, edges[1:] + [None])
            ],
            "heaviest": [
                {
                    "case_id": row.case_id,
                    "turn_number": row.turn_number,
                    "investigation_turn": row.investigation_turn,
                    "occurred_at": _as_utc(row.occurred_at),
                    "spend_weighted_tokens": row.spend_weighted_tokens,
                    "calls": row.calls,
                    "estimated_cost_usd": round(row.estimated_cost_usd, 6),
                    "unpriced_calls": row.unpriced_calls,
                }
                for row in heaviest
            ],
        },
    }


async def probe_usage_store() -> Optional[str]:
    """Why the usage tables cannot be read, as an exception TYPE; ``None`` if they can.

    A one-row read of each table, for ``features["llm_usage_metering"]``. Never
    raises: the status endpoint must answer whatever this finds.
    """
    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.infrastructure.persistence.models import (
        LlmTurnSpendModel,
        LlmUsageDailyModel,
    )

    try:
        async with get_db_session() as session:
            for model in (LlmUsageDailyModel, LlmTurnSpendModel):
                await session.execute(select(model.enterprise_id).limit(1))
    except Exception as exc:  # noqa: BLE001 - any failure to read is the finding
        logger.warning(
            "llm_usage_store_unreadable: the usage tables could not be read "
            "(error=%s)",
            type(exc).__name__,
        )
        return type(exc).__name__
    return None
