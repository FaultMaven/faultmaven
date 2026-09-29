"""The LLM usage ledger: persisted, tenant-attributed spend (#640).

The metering chokepoint (:func:`~faultmaven.infrastructure.llm.metering.
record_provider_call`) already counts every billed provider call into
Prometheus, but those counters carry no tenant (bounded labels, by design), live
per process and vanish on a restart. This module is the persisted half: every
billed call lands in ``llm_usage_daily``, and every engine turn that made one
lands in ``llm_turn_spend``.

Two ways a call reaches a row
-----------------------------
* **Inside an engine turn** it accrues to the turn's
  :class:`~faultmaven.infrastructure.llm.metering.TurnTokenTracker`, and the
  turn's end flushes the tracker once (:func:`flush_turn`): one daily increment
  per ``(provider, model, outcome)`` bucket plus the turn row, in one
  transaction, awaited inline in ``MilestoneEngine.process_turn``.
* **Anywhere else** — title generation, a KB suggestion, an out-of-band aside,
  tier-2 preprocessing, and a call made AFTER its turn flushed (the
  fire-and-forget runbook conversion a turn spawns) — it writes a row of its own
  (:func:`schedule_call_write`). ``record_provider_call`` is synchronous and must
  not block a call, so that write is a task on the running loop; tasks are held
  in a module set so they cannot be garbage-collected mid-flight, and
  :func:`drain_pending_usage_writes` awaits them at shutdown.

Attribution is captured when the spend is incurred, never later: the enterprise
from the tenant context (refused through :func:`usable_tenant_id`, so the
multi-tenant sentinel is never written as a tenant), the payer from
:func:`billing_subject_for` — the same function the turn cap charges with — and
the actor from the engine turn's user, else the request's
``current_actor_user_id``, else ``''``.

Failure direction: OPEN, stated once
------------------------------------
The turn cap's ledger fails closed — a failed write refuses the turn. This one
is the opposite, deliberately, which is why it is a separate table: a failed
write here must never fail a call or a turn. Every call that does not reach a
row is counted on ``llm_usage_unpersisted_calls_total{reason}`` instead, so the
gap between ``llm_provider_calls_total`` and the persisted calls is observable:

``store_error``   the write raised (also logged at WARNING, naming the reason
                  and the exception type, never the row)
``no_tenant``     no usable enterprise under multi-tenancy — RLS would refuse
                  the row anyway
``no_loop``       a call outside a turn was metered with no running event loop
``not_composed``  no ledger installed: the composition root did not run (a unit
                  test, a job). Counted without a log line.

This ledger and ``turn_usage`` do not reconcile. ``turn_usage`` counts turns,
is written before the model runs, and only under multi-tenancy for engine
turns; this ledger records spend for every billed call in both modes.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Set, Tuple

from faultmaven.config.tenant_context import (
    get_current_billing_organization_id,
    get_current_enterprise_id,
    usable_tenant_id,
)
from faultmaven.infrastructure.protection.tenant_turn_cap import (
    billing_subject_for,
    utc_day,
)
from faultmaven.infrastructure.shims import llm_usage_unpersisted_calls

if TYPE_CHECKING:  # the tracker imports this module; the reverse is types only
    from faultmaven.infrastructure.llm.metering import TurnTokenTracker

logger = logging.getLogger(__name__)

#: The third billing-subject kind, beside the turn cap's ``organization`` and
#: ``account``: spend with nobody to charge (a job). Metering records it rather
#: than refusing, which is the one place this ledger's vocabulary differs.
SUBJECT_NONE = "none"

#: ``llm_usage_unpersisted_calls_total`` reasons. See the module docstring.
REASON_STORE_ERROR = "store_error"
REASON_NO_TENANT = "no_tenant"
REASON_NO_LOOP = "no_loop"
REASON_NOT_COMPOSED = "not_composed"

#: Column widths of the key strings. A longer model id is truncated rather than
#: allowed to fail the write: a key that cannot be stored costs the whole row.
_PROVIDER_WIDTH = 64
_MODEL_WIDTH = 255


@dataclass(frozen=True)
class UsageAttribution:
    """Who a unit of spend belongs to, captured when it was incurred."""

    enterprise_id: str
    billing_subject_kind: str
    billing_subject_id: str
    actor_user_id: str


@dataclass
class CallBucket:
    """The summed figures of billed calls sharing one ``(provider, model, outcome)``.

    Token buckets are disjoint, as everywhere in metering. ``estimated_cost_usd``
    sums PRICED calls only; ``unpriced_calls`` counts the ones it leaves out.
    """

    provider: str
    model: str
    outcome: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: float = 0.0
    calls: int = 0
    unpriced_calls: int = 0

    def add(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        cost_usd: float,
        priced: bool,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cache_write_tokens += cache_write_tokens
        self.estimated_cost_usd += cost_usd
        self.calls += 1
        if not priced:
            self.unpriced_calls += 1

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.provider, self.model, self.outcome)


@dataclass(frozen=True)
class TurnSpend:
    """One engine turn's totals, addressed by the message clock."""

    case_id: str
    turn_number: int
    investigation_turn: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    spend_weighted_tokens: int
    calls: int
    low_confidence_calls: int
    unpriced_calls: int
    estimated_cost_usd: float
    occurred_at: datetime


class IUsageLedger(ABC):
    """Where usage rows go. Every write is an INCREMENT, never a replace."""

    @abstractmethod
    async def record_turn(
        self,
        attribution: UsageAttribution,
        turn: TurnSpend,
        buckets: Sequence[CallBucket],
        usage_date: date,
    ) -> None:
        """Add one turn's buckets to the daily rows and upsert its turn row,
        in one transaction."""

    @abstractmethod
    async def record_call(
        self, attribution: UsageAttribution, bucket: CallBucket, usage_date: date
    ) -> None:
        """Add one call made outside a live turn to its daily row."""


_DailyKey = Tuple[str, date, str, str, str, str, str, str]
_TurnKey = Tuple[str, str, int]


def _daily_key(
    attribution: UsageAttribution, bucket: CallBucket, usage_date: date
) -> _DailyKey:
    return (
        attribution.enterprise_id,
        usage_date,
        attribution.billing_subject_kind,
        attribution.billing_subject_id,
        attribution.actor_user_id,
        bucket.provider[:_PROVIDER_WIDTH],
        bucket.model[:_MODEL_WIDTH],
        bucket.outcome,
    )


_DAILY_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "estimated_cost_usd",
    "calls",
    "unpriced_calls",
)
_TURN_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "spend_weighted_tokens",
    "calls",
    "low_confidence_calls",
    "unpriced_calls",
    "estimated_cost_usd",
)


@dataclass
class InMemoryUsageLedger(IUsageLedger):
    """A ledger for tests: the SQL ledger's increment semantics, in dicts."""

    daily: Dict[_DailyKey, Dict[str, float]] = field(default_factory=dict)
    turns: Dict[_TurnKey, Dict[str, object]] = field(default_factory=dict)
    call_writes: int = 0
    turn_writes: int = 0

    def _add_daily(
        self, attribution: UsageAttribution, bucket: CallBucket, usage_date: date
    ) -> None:
        row = self.daily.setdefault(
            _daily_key(attribution, bucket, usage_date),
            {name: 0 for name in _DAILY_COUNTERS},
        )
        for name in _DAILY_COUNTERS:
            row[name] += getattr(bucket, name)

    async def record_turn(self, attribution, turn, buckets, usage_date) -> None:
        self.turn_writes += 1
        for bucket in buckets:
            self._add_daily(attribution, bucket, usage_date)
        row = self.turns.setdefault(
            (attribution.enterprise_id, turn.case_id, turn.turn_number),
            {
                "investigation_turn": turn.investigation_turn,
                "actor_user_id": attribution.actor_user_id,
                "billing_subject_kind": attribution.billing_subject_kind,
                "billing_subject_id": attribution.billing_subject_id,
                "occurred_at": turn.occurred_at,
                **{name: 0 for name in _TURN_COUNTERS},
            },
        )
        for name in _TURN_COUNTERS:
            row[name] += getattr(turn, name)
        row["occurred_at"] = max(row["occurred_at"], turn.occurred_at)

    async def record_call(self, attribution, bucket, usage_date) -> None:
        self.call_writes += 1
        self._add_daily(attribution, bucket, usage_date)


class SqlUsageLedger(IUsageLedger):
    """The shipped ledger: ``llm_usage_daily`` and ``llm_turn_spend``.

    Each write is ``INSERT … ON CONFLICT (key) DO UPDATE SET col = col +
    excluded.col``, so concurrent writers — two replicas, or a turn's flush and
    a late call — add rather than overwrite, atomically in the database. The
    pattern is ``SqlTurnLedger.reserve``'s, with ``enterprise_id`` leading the
    conflict target for the same RLS reason.
    """

    async def record_turn(self, attribution, turn, buckets, usage_date) -> None:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            for bucket in buckets:
                await session.execute(
                    self._daily_upsert(session, attribution, bucket, usage_date)
                )
            await session.execute(self._turn_upsert(session, attribution, turn))

    async def record_call(self, attribution, bucket, usage_date) -> None:
        from faultmaven.infrastructure.persistence.database import get_db_session

        async with get_db_session() as session:
            await session.execute(
                self._daily_upsert(session, attribution, bucket, usage_date)
            )

    @staticmethod
    def _daily_upsert(session, attribution, bucket, usage_date):
        from faultmaven.infrastructure.persistence.db_compat import dialect_insert
        from faultmaven.infrastructure.persistence.models import LlmUsageDailyModel

        table = LlmUsageDailyModel
        enterprise, day, kind, subject, actor, provider, model, outcome = _daily_key(
            attribution, bucket, usage_date
        )
        statement = dialect_insert(session, table).values(
            enterprise_id=enterprise,
            usage_date=day,
            billing_subject_kind=kind,
            billing_subject_id=subject,
            actor_user_id=actor,
            provider=provider,
            model=model,
            outcome=outcome,
            **{name: getattr(bucket, name) for name in _DAILY_COUNTERS},
        )
        return statement.on_conflict_do_update(
            index_elements=[
                "enterprise_id",
                "usage_date",
                "billing_subject_kind",
                "billing_subject_id",
                "actor_user_id",
                "provider",
                "model",
                "outcome",
            ],
            set_={
                name: getattr(table, name) + getattr(statement.excluded, name)
                for name in _DAILY_COUNTERS
            },
        )

    @staticmethod
    def _turn_upsert(session, attribution, turn):
        from sqlalchemy import case

        from faultmaven.infrastructure.persistence.db_compat import dialect_insert
        from faultmaven.infrastructure.persistence.models import LlmTurnSpendModel

        table = LlmTurnSpendModel
        statement = dialect_insert(session, table).values(
            enterprise_id=attribution.enterprise_id,
            case_id=turn.case_id,
            turn_number=turn.turn_number,
            investigation_turn=turn.investigation_turn,
            actor_user_id=attribution.actor_user_id,
            billing_subject_kind=attribution.billing_subject_kind,
            billing_subject_id=attribution.billing_subject_id,
            occurred_at=turn.occurred_at,
            **{name: getattr(turn, name) for name in _TURN_COUNTERS},
        )
        updates = {
            name: getattr(table, name) + getattr(statement.excluded, name)
            for name in _TURN_COUNTERS
        }
        # The one replaced column: the later of the two flush times.
        updates["occurred_at"] = case(
            (
                statement.excluded.occurred_at > table.occurred_at,
                statement.excluded.occurred_at,
            ),
            else_=table.occurred_at,
        )
        return statement.on_conflict_do_update(
            index_elements=["enterprise_id", "case_id", "turn_number"],
            set_=updates,
        )


# ---------------------------------------------------------------------------
# The installed ledger (the composition root sets it)
# ---------------------------------------------------------------------------

_installed_ledger: Optional[IUsageLedger] = None


def install_usage_ledger(ledger: Optional[IUsageLedger]) -> None:
    """Install the ledger every metered call writes to; ``None`` uninstalls."""
    global _installed_ledger
    _installed_ledger = ledger


def get_installed_usage_ledger() -> Optional[IUsageLedger]:
    return _installed_ledger


# ---------------------------------------------------------------------------
# Attribution and the fail-open accounting
# ---------------------------------------------------------------------------


def capture_attribution(actor_user_id: Optional[str]) -> Optional[UsageAttribution]:
    """Attribute spend incurred now, or ``None`` when there is no usable tenant.

    ``None`` is the multi-tenant sentinel case: a context that bound no
    enterprise. RLS would refuse the row, so it is not attempted.
    """
    enterprise_id = usable_tenant_id(get_current_enterprise_id())
    if enterprise_id is None:
        return None
    actor = actor_user_id or ""
    subject = billing_subject_for(get_current_billing_organization_id(), actor or None)
    if subject is None:
        return UsageAttribution(enterprise_id, SUBJECT_NONE, "", actor)
    return UsageAttribution(enterprise_id, subject.kind, subject.subject_id, actor)


def count_unpersisted(reason: str, calls: int = 1) -> None:
    """Count calls the ledger did not persist. Never raises."""
    try:
        llm_usage_unpersisted_calls.labels(reason=reason).inc(calls)
    except Exception:  # a metrics failure must not become a call failure
        pass


def _warn_store_error(unit: str, calls: int, exc: BaseException) -> None:
    # The exception's TYPE only: a database error's text carries the statement
    # parameters, which are the row.
    logger.warning(
        "llm_usage_unpersisted: the usage ledger write failed; %d billed call(s) "
        "not persisted (reason=%s, unit=%s, error=%s)",
        calls,
        REASON_STORE_ERROR,
        unit,
        type(exc).__name__,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

#: In-flight own-row writes. Held so a task cannot be garbage-collected before
#: it finishes (the loop keeps only a weak reference) and so shutdown can drain.
_pending_writes: Set["asyncio.Task[None]"] = set()


async def _write_call(
    ledger: IUsageLedger,
    attribution: UsageAttribution,
    bucket: CallBucket,
    usage_date: date,
) -> None:
    try:
        await ledger.record_call(attribution, bucket, usage_date)
    except Exception as exc:
        count_unpersisted(REASON_STORE_ERROR, bucket.calls)
        _warn_store_error("call", bucket.calls, exc)


def schedule_call_write(bucket: CallBucket, *, actor_user_id: str) -> None:
    """Write one call that no live turn will flush, as a row of its own.

    Synchronous, because its caller is: the write is a task on the running loop.
    ``usage_date`` is the UTC day now, when the call was metered.
    """
    ledger = _installed_ledger
    if ledger is None:
        count_unpersisted(REASON_NOT_COMPOSED, bucket.calls)
        return
    attribution = capture_attribution(actor_user_id)
    if attribution is None:
        count_unpersisted(REASON_NO_TENANT, bucket.calls)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        count_unpersisted(REASON_NO_LOOP, bucket.calls)
        return
    task = loop.create_task(_write_call(ledger, attribution, bucket, utc_day()))
    _pending_writes.add(task)
    task.add_done_callback(_pending_writes.discard)


async def flush_turn(
    tracker: "TurnTokenTracker",
    *,
    case_id: str,
    turn_number: int,
    investigation_turn: int,
) -> None:
    """Persist one engine turn's spend. Awaited at the turn's end; never raises.

    The tracker is marked flushed right after its buckets are copied, with no
    await in between: a call that completes while the write is in flight must
    see a flushed tracker and write its own row, not add to a copy already
    taken. A turn that made no billed call writes nothing. Any failure is
    counted (``store_error``) and logged, and the turn keeps its answer.
    """
    calls = tracker.total_calls
    try:
        buckets = [replace(bucket) for bucket in tracker.buckets.values()]
        tracker.flushed = True
        if not calls:
            return
        ledger = _installed_ledger
        if ledger is None:
            count_unpersisted(REASON_NOT_COMPOSED, calls)
            return
        attribution = tracker.attribution
        if attribution is None:
            count_unpersisted(REASON_NO_TENANT, calls)
            return
        now = datetime.now(timezone.utc)
        turn = TurnSpend(
            case_id=case_id,
            turn_number=turn_number,
            investigation_turn=investigation_turn,
            input_tokens=tracker.input_tokens,
            output_tokens=tracker.output_tokens,
            cache_read_tokens=tracker.cache_read_tokens,
            cache_write_tokens=tracker.cache_write_tokens,
            spend_weighted_tokens=tracker.spend_weighted_tokens,
            calls=calls,
            low_confidence_calls=tracker.low_confidence_calls,
            unpriced_calls=tracker.unpriced_calls,
            estimated_cost_usd=tracker.cost_usd,
            occurred_at=now,
        )
        await ledger.record_turn(attribution, turn, buckets, utc_day(now))
    except Exception as exc:
        tracker.flushed = True
        count_unpersisted(REASON_STORE_ERROR, calls)
        _warn_store_error("turn", calls, exc)


async def drain_pending_usage_writes(timeout_s: float = 5.0) -> int:
    """Await the in-flight own-row writes, up to ``timeout_s``. Never raises.

    Returns how many were still pending when the wait gave up; those are
    cancelled and counted as ``store_error``, since shutdown is what lost them.
    """
    pending: List["asyncio.Task[None]"] = [t for t in _pending_writes if not t.done()]
    if not pending:
        return 0
    try:
        _done, still = await asyncio.wait(pending, timeout=timeout_s)
    except Exception:
        return len(pending)
    for task in still:
        task.cancel()
        count_unpersisted(REASON_STORE_ERROR)
    return len(still)


# ---------------------------------------------------------------------------
# Retention (Q6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PruneResult:
    daily_rows_deleted: int
    turn_rows_deleted: int


async def prune_expired_usage(
    *,
    daily_retention_days: int,
    turn_retention_days: int,
    now: Optional[datetime] = None,
) -> PruneResult:
    """Delete usage rows past their horizon, and nothing else.

    Daily rows go when ``usage_date < today_utc - daily_retention_days``; turn
    rows when ``occurred_at < now_utc - turn_retention_days``. A row exactly AT
    its horizon stays. Cross-tenant by nature: under multi-tenancy only the
    maintenance role (BYPASSRLS) sees every enterprise's rows, so the job that
    calls this declares ``cross_tenant`` and the in-process scheduler refuses to
    run it there.
    """
    from sqlalchemy import delete

    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.infrastructure.persistence.models import (
        LlmTurnSpendModel,
        LlmUsageDailyModel,
    )

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    daily_horizon = now.date() - timedelta(days=daily_retention_days)
    turn_horizon = now - timedelta(days=turn_retention_days)
    async with get_db_session() as session:
        daily = await session.execute(
            delete(LlmUsageDailyModel).where(
                LlmUsageDailyModel.usage_date < daily_horizon
            )
        )
        turns = await session.execute(
            delete(LlmTurnSpendModel).where(
                LlmTurnSpendModel.occurred_at < turn_horizon
            )
        )
    return PruneResult(
        daily_rows_deleted=int(daily.rowcount or 0),
        turn_rows_deleted=int(turns.rowcount or 0),
    )
