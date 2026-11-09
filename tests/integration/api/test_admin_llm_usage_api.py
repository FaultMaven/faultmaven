"""``GET /api/v1/admin/llm/usage`` over the rows the ledger actually writes (#1764).

The real app and route, a real SQLite database, and rows seeded through the
shipped ``SqlUsageLedger.record_turn`` / ``record_call``, so what is read is
what the writer wrote. The clock is pinned through ``usage_report._utc_now``,
so the window's days are fixed. The PostgreSQL/RLS half is
``tests/integration/test_admin_llm_usage_postgres.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from faultmaven.api.models import LLMHeavyTurn
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.exceptions import DatabaseTimeout
from faultmaven.infrastructure.llm import pricing, usage_report
from faultmaven.infrastructure.llm.usage_ledger import (
    CallBucket,
    SqlUsageLedger,
    TurnSpend,
    UsageAttribution,
)
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from tests.utils import reset_settings_singleton, seed_enterprises, seed_users

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("uncomposed_main_app_auth_service"),
]

ENT = STANDALONE_ENTERPRISE_ID
OTHER = "ent_other_1764"
USER = "user_usage_1764"
CASE = "case_usage_1764a"
OTHER_CASE = "case_usage_1764b"
NOW = datetime(2026, 11, 9, 15, 30, tzinfo=timezone.utc)
TODAY = NOW.date()
URL = "/api/v1/admin/llm/usage"


def _user(*roles: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="admin_1764",
        enterprise_id=ENT,
        email="admin@example.com",
        roles=list(roles),
        permissions=[],
    )


@pytest.fixture
async def usage_db(tmp_path, monkeypatch):
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        get_db_session,
        get_engine,
        reset_engine,
    )
    from faultmaven.infrastructure.persistence.models import Base

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'u.db'}")
    monkeypatch.setenv("SANITIZE_PII", "false")
    monkeypatch.delenv("TENANT_PROVIDER", raising=False)
    monkeypatch.delenv("PROMPT_TURN_TOKEN_BUDGET", raising=False)
    monkeypatch.delenv("PROMPT_TURN_TOKEN_CEILING", raising=False)
    monkeypatch.setattr(usage_report, "_utc_now", lambda: NOW)
    reset_settings_singleton()
    reset_engine()
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_db_session() as session:
        await seed_enterprises(session, [OTHER])
        await seed_users(session, [USER])
        for case_id, enterprise in ((CASE, ENT), (OTHER_CASE, OTHER)):
            await session.execute(
                text(
                    "INSERT INTO cases (case_id, enterprise_id, user_id, title, "
                    "created_at, updated_at) VALUES (:c, :e, :u, 'a title', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"c": case_id, "e": enterprise, "u": USER},
            )
    yield
    await close_database()
    reset_engine()
    monkeypatch.undo()
    reset_settings_singleton()


@pytest.fixture
def as_user():
    from faultmaven.api.middleware.auth import get_current_user
    from faultmaven.main import app

    def _set(user: AuthenticatedUser | None):
        if user is not None:

            async def _get():
                return user

            app.dependency_overrides[get_current_user] = _get
        return app

    yield _set
    app.dependency_overrides.clear()


async def _get(app, **params):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(URL, params=params)


async def _admin_get(as_user, **params):
    response = await _get(as_user(_user("platform_admin")), **params)
    assert response.status_code == 200, response.text
    return response.json()


def _who(enterprise=ENT, kind="account", subject=USER, actor=USER):
    return UsageAttribution(enterprise, kind, subject, actor)


async def _call(
    day: date,
    *,
    provider="anthropic",
    model="claude-sonnet-4-6",
    outcome="kept",
    who=None,
    calls=1,
    unpriced=0,
    cost=0.01,
    input_tokens=100,
    output_tokens=10,
    cache_read=0,
    cache_write=0,
):
    bucket = CallBucket(
        provider,
        model,
        outcome,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        estimated_cost_usd=cost,
        calls=calls,
        unpriced_calls=unpriced,
    )
    await SqlUsageLedger().record_call(who or _who(), bucket, day)


async def _turn(turn_number: int, spend: int, *, case_id=CASE, who=None, at=NOW):
    turn = TurnSpend(
        case_id=case_id,
        turn_number=turn_number,
        investigation_turn=turn_number,
        input_tokens=spend,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        spend_weighted_tokens=spend,
        calls=1,
        low_confidence_calls=0,
        unpriced_calls=0,
        estimated_cost_usd=0.02,
        occurred_at=at,
    )
    bucket = CallBucket("anthropic", "claude-sonnet-4-6", "kept", calls=1)
    await SqlUsageLedger().record_turn(who or _who(), turn, [bucket], at.date())


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def test_unauthenticated_is_401(usage_db, as_user):
    assert (await _get(as_user(None))).status_code == 401


async def test_not_a_platform_admin_is_403(usage_db, as_user):
    response = await _get(as_user(_user("admin")))
    assert response.status_code == 403


@pytest.mark.parametrize(
    "params", [{"window": "14d"}, {"window": ""}, {"group_by": "case"}]
)
async def test_an_unknown_parameter_value_is_422(usage_db, as_user, params):
    response = await _get(as_user(_user("platform_admin")), **params)
    assert response.status_code == 422


async def test_group_by_user_is_refused_under_multi(usage_db, as_user, monkeypatch):
    """#640 Q3 (a): per-user figures go to the operator in standalone only."""
    from faultmaven.api.routes import admin_usage

    monkeypatch.setattr(admin_usage, "requested_tenant_provider", lambda: "multi")
    app = as_user(_user("platform_admin"))

    refused = await _get(app, group_by="user")
    served = await _get(app, group_by="billing_subject")

    assert refused.status_code == 422
    assert refused.json()["detail"] == admin_usage.USER_GROUPING_REFUSED_DETAIL
    assert served.status_code == 200
    assert served.json()["scope"]["tenancy"] == "multi"


async def _drop_usage_table():
    from faultmaven.infrastructure.persistence.database import get_db_session

    async with get_db_session() as session:
        await session.execute(text("DROP TABLE llm_usage_daily"))


def _sessions_raising(monkeypatch, error: BaseException):
    from faultmaven.infrastructure.persistence import database

    @asynccontextmanager
    async def _raising():
        raise error
        yield  # pragma: no cover

    monkeypatch.setattr(database, "get_db_session", _raising)


@pytest.mark.parametrize("failure", ["missing_table", "timeout", "refused"])
async def test_an_unreadable_store_is_503_never_zeros(
    usage_db, as_user, monkeypatch, failure
):
    await _call(TODAY)
    if failure == "missing_table":
        await _drop_usage_table()
    elif failure == "timeout":
        _sessions_raising(monkeypatch, DatabaseTimeout("did not answer in time"))
    else:
        # What pool checkout raises, unwrapped, when PostgreSQL refuses the
        # connection (``get_db_session`` does not name it: #2670).
        _sessions_raising(
            monkeypatch, ConnectionRefusedError(111, "Connect call failed 10.0.0.5")
        )

    response = await _get(as_user(_user("platform_admin")))

    assert response.status_code == 503
    assert response.headers["x-error-code"] == "DATABASE_UNAVAILABLE"
    assert response.headers["retry-after"] == "5"
    body = response.text
    assert "totals" not in body
    assert "llm_usage_daily" not in body and "SELECT" not in body
    assert "10.0.0.5" not in body


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


async def test_an_empty_window_is_zeros_with_coverage(usage_db, as_user):
    report = await _admin_get(as_user)

    assert report["window"] == {
        "from": str(TODAY - timedelta(days=6)),
        "to": str(TODAY),
        "days": 7,
        "timezone": "UTC",
    }
    assert report["coverage"] == {
        "metering_since": None,
        "window_fully_covered": False,
        "turns_since": str(TODAY - timedelta(days=6)),
        "turns_fully_covered": True,
    }
    assert report["totals"]["calls"] == {"total": 0, "kept": 0, "low_confidence": 0}
    assert report["totals"]["cache_hit_rate"] is None
    assert report["totals"]["cost_complete"] is True
    assert len(report["by_day"]) == 7
    assert report["groups"] == [] and report["unpriced"] == []
    assert report["turns"]["count"] == 0
    assert report["turns"]["spend_weighted_tokens"] == {
        "p50": None,
        "p95": None,
        "max": None,
    }
    assert report["scope"] == {
        "enterprise_id": ENT,
        "deployment": "standalone",
        "tenancy": "single",
    }


async def test_by_day_has_every_day_of_the_window(usage_db, as_user):
    await _call(TODAY - timedelta(days=5), calls=2)
    await _call(TODAY, outcome="low_confidence", calls=3)
    await _call(TODAY - timedelta(days=40))  # outside the window

    report = await _admin_get(as_user)

    days = [entry["date"] for entry in report["by_day"]]
    assert days == [str(TODAY - timedelta(days=n)) for n in range(6, -1, -1)]
    calls = [entry["calls"]["total"] for entry in report["by_day"]]
    assert calls == [0, 2, 0, 0, 0, 0, 3]
    assert report["totals"]["calls"] == {"total": 5, "kept": 2, "low_confidence": 3}
    assert report["coverage"]["metering_since"] == str(TODAY - timedelta(days=40))
    assert report["coverage"]["window_fully_covered"] is True


async def test_each_group_by_keys_its_groups(usage_db, as_user):
    await _call(TODAY, who=_who(kind="organization", subject="org_1", actor=USER))
    await _call(
        TODAY, model="claude-opus-4-7", who=_who(kind="none", subject="", actor="")
    )

    by_model = await _admin_get(as_user, group_by="provider_model")
    by_user = await _admin_get(as_user, group_by="user")
    by_subject = await _admin_get(as_user, group_by="billing_subject")

    assert {tuple(g["key"].items()) for g in by_model["groups"]} == {
        (("provider", "anthropic"), ("model", "claude-sonnet-4-6")),
        (("provider", "anthropic"), ("model", "claude-opus-4-7")),
    }
    assert all(g["rate_status"] == "priced" for g in by_model["groups"])
    assert {g["key"]["user_id"] for g in by_user["groups"]} == {USER, ""}
    assert {(g["key"]["kind"], g["key"]["id"]) for g in by_subject["groups"]} == {
        ("organization", "org_1"),
        ("none", ""),
    }
    assert all(
        g["rate_status"] is None for g in by_user["groups"] + by_subject["groups"]
    )


async def test_under_multi_account_subjects_are_one_group_with_no_id(
    usage_db, as_user, monkeypatch
):
    """#640 Q3: an account subject's id is a user id, standalone-only."""
    from faultmaven.api.routes import admin_usage

    await _call(TODAY, calls=2, who=_who(kind="account", subject=USER, actor=USER))
    await _call(TODAY, calls=3, who=_who(kind="account", subject="u2", actor="u2"))
    await _call(TODAY, calls=5, who=_who(kind="organization", subject="org_1"))
    standalone = await _admin_get(as_user, group_by="billing_subject")
    monkeypatch.setattr(admin_usage, "requested_tenant_provider", lambda: "multi")

    multi = await _admin_get(as_user, group_by="billing_subject")

    def keyed(report):
        return {
            (g["key"]["kind"], g["key"]["id"]): g["calls"]["total"]
            for g in report["groups"]
        }

    assert keyed(standalone) == {
        ("account", USER): 2,
        ("account", "u2"): 3,
        ("organization", "org_1"): 5,
    }
    assert keyed(multi) == {("account", None): 5, ("organization", "org_1"): 5}
    assert USER not in str(multi["groups"]) and "u2" not in str(multi["groups"])


async def test_under_multi_tied_payer_groups_sort_with_the_null_id(
    usage_db, as_user, monkeypatch
):
    """Groups that record no tokens tie; the folded group's null id still sorts."""
    from faultmaven.api.routes import admin_usage

    no_tokens = {"input_tokens": 0, "output_tokens": 0}
    await _call(TODAY, who=_who(kind="account", subject=USER), **no_tokens)
    await _call(TODAY, who=_who(kind="organization", subject="org_1"), **no_tokens)
    monkeypatch.setattr(admin_usage, "requested_tenant_provider", lambda: "multi")

    report = await _admin_get(as_user, group_by="billing_subject")

    assert [g["key"] for g in report["groups"]] == [
        {"kind": "organization", "id": "org_1"},
        {"kind": "account", "id": None},
    ]


async def test_an_unpriced_call_makes_cost_incomplete_at_every_level(usage_db, as_user):
    await _call(TODAY, cost=0.05)
    await _call(
        TODAY,
        provider="openrouter",
        model="x-model-1764",
        unpriced=2,
        calls=2,
        cost=0.0,
    )

    report = await _admin_get(as_user)

    assert report["totals"]["unpriced_calls"] == 2
    assert report["totals"]["cost_complete"] is False
    assert report["totals"]["estimated_cost_usd"] == pytest.approx(0.05)
    assert report["by_day"][-1]["cost_complete"] is False
    unpriced_group = next(
        g for g in report["groups"] if g["key"]["provider"] == "openrouter"
    )
    assert unpriced_group["rate_status"] == "unpriced"
    assert unpriced_group["cost_complete"] is False
    assert report["unpriced"] == [
        {"provider": "openrouter", "model": "x-model-1764", "calls": 2}
    ]


async def test_a_self_hosted_provider_is_zero_cost_not_unpriced(usage_db, as_user):
    await _call(TODAY, provider="local", model="llama3.2", cost=0.0)

    report = await _admin_get(as_user)

    assert report["groups"][0]["rate_status"] == "zero_cost"
    assert report["groups"][0]["cost_complete"] is True
    assert report["unpriced"] == []


async def test_rate_status_follows_the_running_price_table(
    usage_db, as_user, monkeypatch
):
    """Q7: a model priced after its calls leaves the banner, keeps its history."""
    await _call(
        TODAY, provider="openrouter", model="x-model-1764", unpriced=1, cost=0.0
    )
    rates = {name: dict(models) for name, models in pricing._RATES.items()}
    rates.setdefault("openrouter", {})["x-model-1764"] = pricing.TokenRates(1.0, 2.0)
    monkeypatch.setattr(pricing, "_RATES", rates)

    report = await _admin_get(as_user)

    [group] = report["groups"]
    assert group["rate_status"] == "priced"
    assert group["unpriced_calls"] == 1 and group["cost_complete"] is False
    assert report["unpriced"] == []


@pytest.mark.parametrize(
    "budget, ceiling, soft, edges",
    [
        (40_000, 60_000, 40_000, [0, 20_000, 40_000, 60_000]),
        (0, 60_000, None, [0, 60_000]),
        (200_000, 150_000, 200_000, [0, 100_000, 150_000, 200_000]),
    ],
)
async def test_histogram_edges_follow_the_in_effect_budget(
    usage_db, as_user, monkeypatch, budget, ceiling, soft, edges
):
    monkeypatch.setenv("PROMPT_TURN_TOKEN_BUDGET", str(budget))
    monkeypatch.setenv("PROMPT_TURN_TOKEN_CEILING", str(ceiling))
    reset_settings_singleton()
    spends = [10_000, 30_000, 50_000, 70_000, 120_000, 180_000, 250_000]
    for number, spend in enumerate(spends, start=1):
        await _turn(number, spend)

    turns = (await _admin_get(as_user))["turns"]

    assert turns["budget"] == {"soft": soft, "ceiling": ceiling}
    bins = turns["histogram"]
    assert [b["lower"] for b in bins] == edges
    assert [b["upper"] for b in bins] == edges[1:] + [None]
    assert sum(b["count"] for b in bins) == len(spends)
    for b in bins:
        expected = sum(
            1
            for s in spends
            if s >= b["lower"] and (b["upper"] is None or s < b["upper"])
        )
        assert b["count"] == expected
    assert turns["over_soft"] == (sum(s > soft for s in spends) if soft else 0)
    assert turns["over_ceiling"] == sum(s > ceiling for s in spends)


async def test_turns_percentiles_and_the_heaviest_ten(usage_db, as_user):
    spends = [1_000 * n for n in range(1, 13)]  # 12 turns
    for number, spend in enumerate(spends, start=1):
        await _turn(number, spend)
    await _turn(100, 999_999, at=NOW - timedelta(days=30))  # outside the window

    turns = (await _admin_get(as_user))["turns"]

    assert turns["count"] == 12
    # Nearest rank: p50 is the 6th of 12, p95 the 12th.
    assert turns["spend_weighted_tokens"] == {
        "p50": 6_000,
        "p95": 12_000,
        "max": 12_000,
    }
    heaviest = turns["heaviest"]
    assert [h["spend_weighted_tokens"] for h in heaviest] == spends[::-1][:10]
    assert heaviest[0]["occurred_at"].endswith("Z")
    assert heaviest[0]["case_id"] == CASE


async def test_heaviest_carries_no_title_or_content_field():
    assert set(LLMHeavyTurn.model_fields) == {
        "case_id",
        "turn_number",
        "investigation_turn",
        "occurred_at",
        "spend_weighted_tokens",
        "calls",
        "estimated_cost_usd",
        "unpriced_calls",
    }


async def test_cache_hit_rate(usage_db, as_user):
    await _call(TODAY, input_tokens=0, output_tokens=5, cache_read=0)
    empty = await _admin_get(as_user)
    await _call(TODAY, input_tokens=300, cache_read=100, model="claude-opus-4-7")

    report = await _admin_get(as_user, group_by="provider_model")

    assert empty["totals"]["cache_hit_rate"] is None
    assert report["totals"]["cache_hit_rate"] == pytest.approx(0.25)
    sonnet = next(
        g for g in report["groups"] if g["key"]["model"] == "claude-sonnet-4-6"
    )
    assert sonnet["cache_hit_rate"] is None


async def test_turns_coverage_follows_turn_retention_through_the_prune(
    usage_db, as_user, monkeypatch
):
    """Turn rows are pruned sooner than daily rows: a 30-day window under a
    5-day turn retention has full daily coverage and five days of turns, and
    says the same whether or not the prune has run (a default standalone
    install never prunes)."""
    from faultmaven.infrastructure.llm.usage_ledger import prune_expired_usage

    for age in (0, 3, 10, 29):  # 29: the 30-day window's first day
        day = NOW - timedelta(days=age)
        await _call(day.date())
        await _turn(100 + age, 1_000 * (age + 1), at=day)
    monkeypatch.setenv("LLM_USAGE_TURN_RETENTION_DAYS", "5")
    # Spends 1k/4k in the horizon, 11k/30k before it: the budgets split them.
    monkeypatch.setenv("PROMPT_TURN_TOKEN_BUDGET", "2000")
    monkeypatch.setenv("PROMPT_TURN_TOKEN_CEILING", "10000")
    reset_settings_singleton()

    unpruned = await _admin_get(as_user, window="30d")
    pruned = await prune_expired_usage(
        daily_retention_days=400, turn_retention_days=5, now=NOW
    )
    month = await _admin_get(as_user, window="30d")
    today = await _admin_get(as_user, window="1d")

    assert pruned.turn_rows_deleted == 2
    for report in (unpruned, month):
        assert report["coverage"]["window_fully_covered"] is True
        assert report["coverage"]["turns_since"] == str(TODAY - timedelta(days=4))
        assert report["coverage"]["turns_fully_covered"] is False
        turns = report["turns"]
        assert turns["count"] == 2
        assert turns["spend_weighted_tokens"]["max"] == 4_000
        assert (turns["over_soft"], turns["over_ceiling"]) == (1, 0)
        assert sum(b["count"] for b in turns["histogram"]) == 2
        assert [h["spend_weighted_tokens"] for h in turns["heaviest"]] == [4_000, 1_000]
    assert unpruned["turns"] == month["turns"]
    assert today["coverage"]["turns_since"] == str(TODAY)
    assert today["coverage"]["turns_fully_covered"] is True


@pytest.mark.parametrize(
    "retention, count, fully_covered",
    [(29, 1, False), (30, 2, True), (36500, 2, True)],  # 36500: the setting's bound
)
async def test_a_turn_retention_equal_to_the_window_covers_it(
    usage_db, as_user, monkeypatch, retention, count, fully_covered
):
    """The horizon is ``today - retention + 1``: a retention equal to the
    window's days lands on its first day, which is read; one day less starts a
    day later. A retention up to the setting's bound computes its horizon."""
    first = NOW - timedelta(days=29)  # the 30-day window's first day
    for number, at in enumerate((first, NOW), start=1):
        await _turn(number, 1_000, at=at)
    monkeypatch.setenv("LLM_USAGE_TURN_RETENTION_DAYS", str(retention))
    reset_settings_singleton()

    report = await _admin_get(as_user, window="30d")

    expected_since = first.date() if fully_covered else first.date() + timedelta(1)
    assert report["coverage"]["turns_since"] == str(expected_since)
    assert report["coverage"]["turns_fully_covered"] is fully_covered
    assert report["turns"]["count"] == count


async def test_an_unscoped_request_under_multi_is_403(
    usage_db, monkeypatch, restore_tenant_context
):
    """The sentinel enterprise is no tenant under multi: refused, never read."""
    from fastapi import HTTPException

    from faultmaven.api.routes import admin_usage
    from faultmaven.config.tenant_context import UNSCOPED_REQUEST_MSG

    monkeypatch.setattr(
        "faultmaven.providers.tenancy.factory.requested_tenant_provider",
        lambda: "multi",
    )
    monkeypatch.setattr(admin_usage, "requested_tenant_provider", lambda: "multi")

    async def _never(**_kwargs):
        raise AssertionError("the store was read for an unscoped request")

    monkeypatch.setattr(admin_usage, "llm_usage_report", _never)
    # The request is bound to the Standalone sentinel, which multi refuses.
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)

    with pytest.raises(HTTPException) as refused:
        await admin_usage.get_llm_usage(
            window="7d",
            group_by="provider_model",
            current_user=_user("platform_admin"),
        )

    assert refused.value.status_code == 403
    assert refused.value.detail == UNSCOPED_REQUEST_MSG


# ---------------------------------------------------------------------------
# Tenancy and restart
# ---------------------------------------------------------------------------


async def test_another_enterprises_rows_are_excluded(usage_db, as_user):
    """Standalone has no RLS: the explicit predicate is all that excludes them."""
    await _call(TODAY, calls=1)
    other = _who(enterprise=OTHER)
    await _call(TODAY, calls=50, who=other)
    await _call(TODAY - timedelta(days=60), who=other)
    await _turn(1, 5_000)
    await _turn(1, 90_000, case_id=OTHER_CASE, who=other)

    report = await _admin_get(as_user)

    # One own call plus the own turn's call; none of the other enterprise's.
    assert report["totals"]["calls"]["total"] == 2
    assert report["coverage"]["metering_since"] == str(TODAY)
    assert report["turns"]["count"] == 1
    assert [h["case_id"] for h in report["turns"]["heaviest"]] == [CASE]


async def test_a_restart_reads_the_same_numbers(usage_db, as_user):
    from faultmaven.container import container
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    await _call(TODAY, calls=3, unpriced=1)
    await _turn(1, 42_000)
    before = await _admin_get(as_user)

    container.reset()
    await close_database()
    reset_engine()
    after = await _admin_get(as_user)

    assert after == before
    assert after["totals"]["calls"]["total"] == 4
