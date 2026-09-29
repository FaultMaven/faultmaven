"""The usage ledger's retention deletes only rows past their horizon (#640, Q6).

Against a real SQLite schema: a row one day past its horizon goes, a row AT
its horizon stays, a row one day inside it stays — on both tables, so a ``<``
that became ``<=`` shows. The job reads the two ``*_DAYS`` knobs, is registered
with the runner as ``cross_tenant``, and the in-process scheduler is refused
under multi-tenancy.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.llm import usage_ledger
from faultmaven.infrastructure.llm.usage_ledger import prune_expired_usage
from tests.utils import reset_settings_singleton, seed_users

pytestmark = [pytest.mark.integration]

ENTERPRISE = STANDALONE_ENTERPRISE_ID
USER = "user_retention_640"
CASE_ID = "case_aabb0640dead"
NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
async def usage_db(tmp_path, monkeypatch):
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        get_db_session,
        get_engine,
        reset_engine,
    )
    from faultmaven.infrastructure.persistence.models import Base

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    reset_settings_singleton()
    reset_engine()
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_db_session() as session:
        await seed_users(session, [USER])
        await session.execute(
            text(
                "INSERT INTO cases (case_id, enterprise_id, user_id, title, "
                "created_at, updated_at) VALUES (:c, :e, :u, 't', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"c": CASE_ID, "e": ENTERPRISE, "u": USER},
        )
    yield get_db_session
    await close_database()
    reset_engine()
    monkeypatch.undo()
    reset_settings_singleton()


async def _seed(session_factory, *, days, turn_ages) -> None:
    """A daily row per ``days`` (a date each) and a turn row per age."""
    async with session_factory() as session:
        for day in days:
            await session.execute(
                text(
                    "INSERT INTO llm_usage_daily (enterprise_id, usage_date, "
                    "billing_subject_kind, billing_subject_id, actor_user_id, "
                    "provider, model, outcome, calls) VALUES (:e, :d, 'account', "
                    ":u, :u, 'anthropic', 'claude-sonnet-4-6', 'kept', 1)"
                ),
                {"e": ENTERPRISE, "d": day, "u": USER},
            )
        from faultmaven.infrastructure.persistence.models import LlmTurnSpendModel

        for number, occurred_at in enumerate(turn_ages, start=1):
            session.add(
                LlmTurnSpendModel(
                    enterprise_id=ENTERPRISE,
                    case_id=CASE_ID,
                    turn_number=number,
                    actor_user_id=USER,
                    billing_subject_kind="account",
                    billing_subject_id=USER,
                    calls=1,
                    occurred_at=occurred_at,
                )
            )


async def _left(session_factory) -> tuple[list[date], list[int]]:
    async with session_factory() as session:
        days = (
            await session.execute(
                text("SELECT usage_date FROM llm_usage_daily ORDER BY usage_date")
            )
        ).scalars()
        turns = (
            await session.execute(
                text("SELECT turn_number FROM llm_turn_spend ORDER BY turn_number")
            )
        ).scalars()
        return [
            d if isinstance(d, date) else date.fromisoformat(d) for d in days
        ], list(turns)


class TestTheHorizon:
    async def test_only_rows_past_the_horizon_go(self, usage_db):
        daily_days, turn_days = 30, 7
        daily_horizon = NOW.date() - timedelta(days=daily_days)
        turn_horizon = NOW - timedelta(days=turn_days)
        await _seed(
            usage_db,
            days=[
                daily_horizon - timedelta(days=1),
                daily_horizon,
                daily_horizon + timedelta(days=1),
            ],
            # turn 1 past, turn 2 AT, turn 3 inside
            turn_ages=[
                turn_horizon - timedelta(days=1),
                turn_horizon,
                turn_horizon + timedelta(days=1),
            ],
        )

        pruned = await prune_expired_usage(
            daily_retention_days=daily_days, turn_retention_days=turn_days, now=NOW
        )

        assert (pruned.daily_rows_deleted, pruned.turn_rows_deleted) == (1, 1)
        days, turns = await _left(usage_db)
        assert days == [daily_horizon, daily_horizon + timedelta(days=1)]
        assert turns == [2, 3]

    async def test_a_second_pass_deletes_nothing(self, usage_db):
        await _seed(
            usage_db,
            days=[NOW.date() - timedelta(days=500)],
            turn_ages=[NOW - timedelta(days=200)],
        )
        first = await prune_expired_usage(
            daily_retention_days=400, turn_retention_days=90, now=NOW
        )
        second = await prune_expired_usage(
            daily_retention_days=400, turn_retention_days=90, now=NOW
        )
        assert (first.daily_rows_deleted, first.turn_rows_deleted) == (1, 1)
        assert (second.daily_rows_deleted, second.turn_rows_deleted) == (0, 0)


class TestTheJob:
    async def test_the_job_reads_the_knobs(self, usage_db, monkeypatch):
        monkeypatch.setenv("LLM_USAGE_DAILY_RETENTION_DAYS", "10")
        monkeypatch.setenv("LLM_USAGE_TURN_RETENTION_DAYS", "3")
        reset_settings_singleton()
        from faultmaven.config.settings import get_settings
        from faultmaven.jobs import llm_usage_retention

        today = datetime.now(timezone.utc)
        await _seed(
            usage_db,
            days=[today.date() - timedelta(days=11), today.date() - timedelta(days=9)],
            turn_ages=[today - timedelta(days=4), today - timedelta(days=2)],
        )

        result = await llm_usage_retention.run(get_settings(), container=None)

        assert result["status"] == "completed", result
        assert (result["daily_retention_days"], result["turn_retention_days"]) == (
            10,
            3,
        )
        assert (result["daily_rows_deleted"], result["turn_rows_deleted"]) == (1, 1)
        days, turns = await _left(usage_db)
        assert days == [today.date() - timedelta(days=9)]
        assert turns == [2]

    async def test_a_failure_is_reported_not_raised(self, monkeypatch):
        from faultmaven.config.settings import get_settings
        from faultmaven.jobs import llm_usage_retention

        async def _broken(**_kwargs):
            raise RuntimeError("no such table: llm_usage_daily")

        monkeypatch.setattr(usage_ledger, "prune_expired_usage", _broken)
        result = await llm_usage_retention.run(get_settings(), container=None)
        assert result["status"] == "failed"

    def test_the_job_is_registered_and_cross_tenant(self):
        from faultmaven.jobs import llm_usage_retention
        from faultmaven.jobs.run import AVAILABLE_JOBS, TENANT_SCOPE_CROSS_TENANT

        assert AVAILABLE_JOBS["llm_usage_retention"] == llm_usage_retention.__name__
        assert llm_usage_retention.JOB_TENANT_SCOPE == TENANT_SCOPE_CROSS_TENANT

    @pytest.mark.parametrize(
        "name", ["LLM_USAGE_DAILY_RETENTION_DAYS", "LLM_USAGE_TURN_RETENTION_DAYS"]
    )
    def test_a_horizon_below_one_day_fails_startup(self, monkeypatch, name):
        from pydantic import ValidationError

        from faultmaven.config.settings import AuthSettings

        monkeypatch.setenv(name, "0")
        with pytest.raises(ValidationError):
            AuthSettings()


class TestTheInProcessScheduler:
    def test_refused_under_multi(self):
        from faultmaven.infrastructure.tasks.llm_usage_retention import (
            start_llm_usage_retention_scheduler,
        )

        assert start_llm_usage_retention_scheduler(is_multi_tenant=True) is None

    async def test_prunes_at_start_and_stops(self, monkeypatch):
        from faultmaven.infrastructure.tasks.llm_usage_retention import (
            start_llm_usage_retention_scheduler,
            stop_llm_usage_retention_scheduler,
        )

        passes: list[tuple[int, int]] = []

        async def _record(*, daily_retention_days, turn_retention_days):
            passes.append((daily_retention_days, turn_retention_days))
            return SimpleNamespace(daily_rows_deleted=0, turn_rows_deleted=0)

        monkeypatch.setattr(usage_ledger, "prune_expired_usage", _record)
        task = start_llm_usage_retention_scheduler(interval_hours=24)
        for _ in range(20):
            if passes:
                break
            await asyncio.sleep(0.01)
        await stop_llm_usage_retention_scheduler(task)

        assert passes == [(400, 90)], "one pass at start, at the default horizons"
        assert task.done()
