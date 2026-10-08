"""The turn receipt's write path on PostgreSQL under RLS (#1888).

The SQLite half, through the mounted route:
``tests/unit/modules/case/test_turn_receipt_1888.py``. This one adds what
SQLite cannot show: the receipt is written as a non-superuser, non-owner role
(a superuser or the table owner bypasses RLS), inside the case's transaction,
under the tenant that transaction's BEGIN bound, and the lost-ack probe reads it
back in a FRESH session under the same task-local tenant. A VARCHAR too narrow
for a real key, a ``jsonb`` that reorders the stored response, or a policy that
refuses the INSERT all fail here and nowhere else.

What committed is read back as the superuser, which sees every row whatever
the policies say.

Run locally:

    docker run -d -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
    export DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/postgres
    .venv-cloud/bin/alembic upgrade head
    .venv-cloud/bin/pytest tests/integration/test_turn_receipts_postgres_1888.py -v
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from faultmaven.config.idempotency_key import IDEMPOTENCY_KEY_MAX_LENGTH
from faultmaven.modules.case.domain.owned_models.turn_receipt import TurnReceipt
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure import (
    sessionless_case_repository as sessionless_module,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.repository import (
    PostgreSQLHybridCaseRepository,
    RepositoryException,
)

# The #1882 fixtures: the superuser engine, two seeded enterprises, sessions as
# a limited role tenant-bound per transaction, and the production wrapper over
# them. Imported so both files exercise the one harness.
from tests.integration.test_turn_rows_commit_with_case_postgres_1882 import (  # noqa: F401
    _add_message,
    _case,
    enterprises,
    sessionless,
    superuser_engine,
    tenant,
    tenant_sessions,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

AUTHOR = "user_1888_pg"
# A real copilot key is short; the grammar admits 255, and the column must too.
LONG_KEY = "k" * IDEMPOTENCY_KEY_MAX_LENGTH
#: Key order a ``jsonb`` column would not keep (it sorts by length, then bytes).
RESPONSE = {
    "agent_response": "pool saturation",
    "turn_number": 2,
    "case_state": "investigating",
    "metadata": {"zeta": 1, "alpha": 2, "mid": 3},
}


def _receipt(case, *, key: str = LONG_KEY, turn: int = 1) -> TurnReceipt:
    return TurnReceipt(
        case_id=case.case_id,
        author_id=AUTHOR,
        idempotency_key=key,
        request_fingerprint="f" * 64,
        turn_number=turn,
        response=RESPONSE,
    )


async def _stored(superuser_engine, case_id: str) -> list:
    async with superuser_engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT enterprise_id, author_id, idempotency_key, turn_number, "
                    "response::text FROM turn_receipts WHERE case_id = :c"
                ),
                {"c": case_id},
            )
        ).fetchall()


async def _read(tenant_sessions, enterprise_id: str, case, key: str = LONG_KEY):
    async with tenant_sessions() as session:
        return await PostgreSQLHybridCaseRepository(session).get_turn_receipt(
            enterprise_id=case.enterprise_id,
            case_id=case.case_id,
            author_id=AUTHOR,
            idempotency_key=key,
        )


@pytest.mark.asyncio
async def test_the_receipt_commits_with_the_case_under_the_tenant(
    superuser_engine, enterprises, tenant_sessions
):
    ent_a, _ = enterprises
    case = _case(ent_a)
    with tenant(ent_a):
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(
                case, receipt=_receipt(case)
            )

    [(enterprise_id, author, key, turn, stored_json)] = await _stored(
        superuser_engine, case.case_id
    )
    assert (enterprise_id, author, key, turn) == (ent_a, AUTHOR, LONG_KEY, 1)
    # ``json`` keeps the text it was given: the replay is the bytes sent.
    assert stored_json == json.dumps(RESPONSE)


@pytest.mark.asyncio
async def test_the_receipt_reads_back_in_order_and_only_under_its_tenant(
    superuser_engine, enterprises, tenant_sessions
):
    ent_a, ent_b = enterprises
    case = _case(ent_a)
    with tenant(ent_a):
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(
                case, receipt=_receipt(case)
            )
        mine = await _read(tenant_sessions, ent_a, case)
    with tenant(ent_b):
        theirs = await _read(tenant_sessions, ent_b, case)

    assert mine is not None, "positive control: the owner's tenant reads it"
    assert list(mine.response) == list(RESPONSE)
    assert list(mine.response["metadata"]) == ["zeta", "alpha", "mid"]
    assert theirs is None


@pytest.mark.asyncio
async def test_a_stale_case_commits_no_receipt(
    superuser_engine, enterprises, tenant_sessions
):
    ent_a, _ = enterprises
    case = _case(ent_a)
    with tenant(ent_a):
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(case)
        async with tenant_sessions() as other:
            winner = await PostgreSQLHybridCaseRepository(other).get(case.case_id)
            winner.title = "Saved by another request"
            await PostgreSQLHybridCaseRepository(other).save(winner)

        _add_message(case, "this turn's message")
        async with tenant_sessions() as session:
            with pytest.raises(StaleCaseException):
                await PostgreSQLHybridCaseRepository(session).save(
                    case, receipt=_receipt(case)
                )

    assert await _stored(superuser_engine, case.case_id) == []


@pytest.mark.asyncio
async def test_a_second_receipt_under_the_key_commits_nothing(
    superuser_engine, enterprises, tenant_sessions
):
    """A plain INSERT: a second turn committing under a key that already has a
    receipt fails with its whole transaction rather than overwriting the
    answer the first one's retries are owed."""
    ent_a, _ = enterprises
    case = _case(ent_a)
    with tenant(ent_a):
        async with tenant_sessions() as session:
            await PostgreSQLHybridCaseRepository(session).save(
                case, receipt=_receipt(case, turn=1)
            )
        case.title = "Retitled by the second turn"
        async with tenant_sessions() as session:
            with pytest.raises(RepositoryException):
                await PostgreSQLHybridCaseRepository(session).save(
                    case, receipt=_receipt(case, turn=2)
                )

    [(_, _, _, turn, _)] = await _stored(superuser_engine, case.case_id)
    assert turn == 1
    async with superuser_engine.connect() as conn:
        title = (
            await conn.execute(
                text("SELECT title FROM cases WHERE case_id = :c"),
                {"c": case.case_id},
            )
        ).scalar()
    assert title == "Turn commit case"


def _commit_fault(monkeypatch, *, lands: bool) -> dict:
    """Arm the next save's ``db.commit()`` to raise, after (``lands``) or
    instead of reaching the database, as a connection lost mid-COMMIT does."""
    armed = {"on": True}
    real_factory = sessionless_module.get_repository_for_session

    def _factory(session):
        repo = real_factory(session)
        real_save = repo.save

        async def save(*args, **kwargs):
            if armed["on"]:
                armed["on"] = False
                real_commit = repo.db.commit

                async def commit():
                    if lands:
                        await real_commit()
                    raise ConnectionError("connection lost mid-COMMIT")

                repo.db.commit = commit
            return await real_save(*args, **kwargs)

        repo.save = save
        return repo

    monkeypatch.setattr(sessionless_module, "get_repository_for_session", _factory)
    return armed


@pytest.mark.asyncio
async def test_a_commit_that_landed_and_raised_is_answered_by_the_receipt(
    superuser_engine, enterprises, sessionless, monkeypatch
):
    """6b under RLS: the probe reads the receipt back in a FRESH session, under
    the tenant the task carries, and the save returns normally."""
    ent_a, _ = enterprises
    case = _case(ent_a)
    armed = _commit_fault(monkeypatch, lands=True)
    with tenant(ent_a):
        await sessionless.save(case, receipt=_receipt(case))

    assert not armed["on"], "positive control: the fault fired"
    assert len(await _stored(superuser_engine, case.case_id)) == 1


@pytest.mark.asyncio
async def test_a_commit_that_did_not_land_still_raises(
    superuser_engine, enterprises, sessionless, monkeypatch
):
    ent_a, _ = enterprises
    case = _case(ent_a)
    _commit_fault(monkeypatch, lands=False)
    with tenant(ent_a):
        with pytest.raises(RepositoryException):
            await sessionless.save(case, receipt=_receipt(case))

    assert await _stored(superuser_engine, case.case_id) == []


@pytest.mark.asyncio
async def test_an_older_receipt_under_the_key_does_not_vouch(
    superuser_engine, enterprises, sessionless, monkeypatch
):
    """6c under RLS: the key matches, the turn number does not."""
    ent_a, _ = enterprises
    case = _case(ent_a)
    with tenant(ent_a):
        await sessionless.save(case, receipt=_receipt(case, turn=1))
        _commit_fault(monkeypatch, lands=False)
        case.title = "Retitled by a later turn"
        with pytest.raises(RepositoryException):
            await sessionless.save(case, receipt=_receipt(case, turn=2))


@pytest.mark.asyncio
async def test_through_the_wrapper_a_raise_closing_the_session_stands_the_save(
    superuser_engine, enterprises, tenant_sessions, monkeypatch
):
    """6a under PostgreSQL: committed, then the session's close raised."""
    from contextlib import asynccontextmanager

    from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
        SessionlessCaseRepository,
    )

    @asynccontextmanager
    async def _get_db_session():
        session = tenant_sessions()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
            raise ConnectionResetError("connection reset while closing")

    monkeypatch.setattr(sessionless_module, "get_db_session", _get_db_session)
    ent_a, _ = enterprises
    case = _case(ent_a)
    with tenant(ent_a):
        saved = await SessionlessCaseRepository().save(
            case, receipt=_receipt(case, key=f"opt_msg_{uuid4().hex}")
        )

    assert saved is case
    assert len(await _stored(superuser_engine, case.case_id)) == 1
