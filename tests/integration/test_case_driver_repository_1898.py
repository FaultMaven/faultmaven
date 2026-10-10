"""``cases.driver_id`` through both SQL repositories (ADR-020, #1898).

The risk this module exists for: case saves are FULL-ROW writes from memory, so
a column the load or the save forgets is written back stale by the next turn —
a reassignment silently undone. Every check runs on SQLite (always) and on
PostgreSQL (when ``DATABASE_URL`` names one, as CI's postgres job does), against
the dialect repository's real SQL:

- ``driver_id`` round-trips through insert, update, ``get`` and ``list``;
- a reassignment is a compare-and-swap on ``version`` that bumps it, so a turn
  holding the case from before it fails with a version conflict instead of
  writing the old driver back;
- a release is conditional on the stored driver;
- each change writes its ``case_driver_changed`` audit row in the SAME
  transaction, and a refused change writes none;
- ``driven_only`` (``access=write``) narrows ``list`` and ``search`` with a
  ``total_count`` that agrees with the page;
- deleting the driver's account hands the case back through the key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.contracts import (
    Case,
    CaseDriverChange,
    CaseDriverChangeReason,
)
from faultmaven.modules.case.exceptions import StaleCaseException

pytestmark = [pytest.mark.integration]

_PG = os.environ.get("DATABASE_URL", "").startswith("postgresql")


def _sqlite_schema(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgresql",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    not _PG, reason="PostgreSQL arm: set DATABASE_URL to a PG instance"
                ),
            ],
        ),
    ]
)
async def world(request, tmp_path):
    """A dialect repository on a real database, an enterprise of its own and
    three accounts: CREATOR, DRIVER and TEAMMATE."""
    if request.param == "sqlite":
        path = tmp_path / "driver.db"
        _sqlite_schema(path)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

        @event.listens_for(engine.sync_engine, "connect")
        def _fk_on(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
            SQLiteCaseRepository as Repository,
        )
    else:
        engine = create_async_engine(os.environ["DATABASE_URL"])
        from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.repository import (
            PostgreSQLHybridCaseRepository as Repository,
        )

    enterprise = f"ent_{uuid4().hex[:8]}"
    people = {
        role: f"u_{role}_{uuid4().hex[:6]}" for role in ("creator", "driver", "mate")
    }
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        await session.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES (:e, :e, :e)"
            ),
            {"e": enterprise},
        )
        for uid in people.values():
            await session.execute(
                text(
                    "INSERT INTO users (user_id, enterprise_id, username, email, "
                    "display_name) VALUES (:u, :e, :u, :m, :u)"
                ),
                {"u": uid, "e": enterprise, "m": f"{uid}@example.com"},
            )
        await session.commit()

    def repository_in(session):
        return Repository(session)

    try:
        yield _World(Session, repository_in, enterprise, people)
    finally:
        async with Session() as session:
            await session.execute(
                text("DELETE FROM user_audit_log WHERE enterprise_id = :e"),
                {"e": enterprise},
            )
            await session.execute(
                text("DELETE FROM cases WHERE enterprise_id = :e"), {"e": enterprise}
            )
            await session.execute(
                text("DELETE FROM users WHERE enterprise_id = :e"), {"e": enterprise}
            )
            await session.execute(
                text("DELETE FROM enterprises WHERE enterprise_id = :e"),
                {"e": enterprise},
            )
            await session.commit()
        await engine.dispose()


class _World:
    def __init__(self, Session, repository_in, enterprise, people):
        self.Session = Session
        self.repository_in = repository_in
        self.enterprise = enterprise
        self.creator = people["creator"]
        self.driver = people["driver"]
        self.mate = people["mate"]

    async def run(self, method, *args, **kwargs):
        """One repository call in its own session, as the sessionless wrapper
        makes it."""
        async with self.Session() as session:
            return await getattr(self.repository_in(session), method)(*args, **kwargs)

    def case(self, title="Driver round trip", **fields) -> Case:
        return Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id=self.creator,
            enterprise_id=self.enterprise,
            title=title,
            **fields,
        )

    def change(self, case_id, frm, to, reason=CaseDriverChangeReason.REASSIGNED):
        return CaseDriverChange(
            case_id=case_id,
            enterprise_id=self.enterprise,
            from_driver_id=frm,
            to_driver_id=to,
            reason=reason,
            actor_user_id=self.creator,
        )

    async def audit_rows(self, case_id):
        async with self.Session() as session:
            rows = await session.execute(
                text(
                    "SELECT user_id, event_type, event_category, resource_type, "
                    "details FROM user_audit_log WHERE resource_id = :c"
                ),
                {"c": case_id},
            )
            return [tuple(r) for r in rows]


async def test_the_driver_round_trips_through_insert_update_get_and_list(world):
    case = world.case(driver_id=world.driver)
    await world.run("save", case)

    loaded = await world.run("get", case.case_id)
    assert loaded.driver_id == world.driver

    loaded.title = "Edited by a turn"
    await world.run("save", loaded)
    again = await world.run("get", case.case_id)
    assert again.driver_id == world.driver
    assert again.title == "Edited by a turn"

    listed, _ = await world.run("list", user_id=world.creator)
    assert [c.driver_id for c in listed if c.case_id == case.case_id] == [world.driver]


async def test_a_full_row_save_never_writes_the_driver(world):
    """The driver's one writer is the versioned reassign/release: a save
    carrying a different ``driver_id`` in memory leaves the stored one alone,
    so no turn's save can ever write a driver back (plan risk 1, closed
    structurally)."""
    case = world.case(driver_id=world.driver)
    await world.run("save", case)
    loaded = await world.run("get", case.case_id)

    loaded.driver_id = world.mate
    loaded.title = "Saved by a turn"
    await world.run("save", loaded)

    stored = await world.run("get", case.case_id)
    assert stored.title == "Saved by a turn"
    assert stored.driver_id == world.driver


async def test_the_kb_context_origin_round_trips(world):
    """Who the pre-fetched context was fetched for (ADR-020 D9) must survive a
    save, or every reloaded context would read as the creator's and stale
    context would reach the next driver's prompt."""
    case = world.case(driver_id=world.driver)
    case.kb_context = [{"title": "rb", "summary": "s", "parent_document_id": "rb1"}]
    case.kb_context_origin = {
        "driver_id": world.driver,
        "query": "etcd member",
        "trigger": "symptom",
    }
    await world.run("save", case)

    stored = await world.run("get", case.case_id)

    assert stored.kb_context_origin == case.kb_context_origin


async def test_a_creator_driven_case_stores_null(world):
    case = world.case()
    await world.run("save", case)

    assert (await world.run("get", case.case_id)).driver_id is None


async def test_a_turn_holding_the_case_cannot_write_the_old_driver_back(world):
    """Risk 1 of the plan: the full-row save carries ``driver_id``, so it is
    the version bump that keeps a stale in-flight copy from undoing a
    reassignment."""
    case = world.case()
    await world.run("save", case)
    in_flight = await world.run("get", case.case_id)

    version = await world.run(
        "reassign_driver",
        case.case_id,
        driver_id=world.driver,
        expected_version=in_flight.version,
        change=world.change(case.case_id, world.creator, world.driver),
    )
    assert version == in_flight.version + 1

    in_flight.title = "written by the turn"
    with pytest.raises(StaleCaseException):
        await world.run("save", in_flight)
    stored = await world.run("get", case.case_id)
    assert stored.driver_id == world.driver
    assert stored.title == "Driver round trip"


async def test_a_reassignment_and_its_audit_row_commit_together(world):
    case = world.case()
    await world.run("save", case)

    await world.run(
        "reassign_driver",
        case.case_id,
        driver_id=world.driver,
        expected_version=1,
        change=world.change(case.case_id, world.creator, world.driver),
    )

    ((actor, event_type, category, resource, details),) = await world.audit_rows(
        case.case_id
    )
    assert (actor, event_type, category, resource) == (
        world.creator,
        "case_driver_changed",
        "authorization",
        "case",
    )
    assert json.loads(details) == {
        "from_driver_id": world.creator,
        "to_driver_id": world.driver,
        "reason": "reassigned",
    }


async def test_a_lost_version_race_writes_nothing(world):
    case = world.case()
    await world.run("save", case)

    version = await world.run(
        "reassign_driver",
        case.case_id,
        driver_id=world.driver,
        expected_version=7,
        change=world.change(case.case_id, world.creator, world.driver),
    )

    assert version is None
    stored = await world.run("get", case.case_id)
    assert (stored.driver_id, stored.version) == (None, 1)
    assert await world.audit_rows(case.case_id) == []


async def test_a_release_is_conditional_on_the_stored_driver(world):
    case = world.case(driver_id=world.driver)
    await world.run("save", case)

    refused = await world.run(
        "release_driver",
        case.case_id,
        driver_id=world.mate,
        change=world.change(
            case.case_id, world.mate, world.creator, CaseDriverChangeReason.UNSHARED
        ),
    )
    assert refused is False
    assert await world.audit_rows(case.case_id) == []

    released = await world.run(
        "release_driver",
        case.case_id,
        driver_id=world.driver,
        change=world.change(
            case.case_id, world.driver, world.creator, CaseDriverChangeReason.UNSHARED
        ),
    )
    assert released is True
    stored = await world.run("get", case.case_id)
    assert (stored.driver_id, stored.version) == (None, 2)
    ((_, event_type, _, _, details),) = await world.audit_rows(case.case_id)
    assert event_type == "case_driver_changed"
    assert json.loads(details)["reason"] == "unshared"


async def test_cases_driven_by_an_account_are_the_assigned_ones(world):
    driven = world.case(driver_id=world.driver)
    own = world.case()
    await world.run("save", driven)
    await world.run("save", own)

    rows = await world.run("list_cases_driven_by", world.driver)
    assert [(r.case_id, r.creator_id, r.driver_id) for r in rows] == [
        (driven.case_id, world.creator, world.driver)
    ]
    assert await world.run("list_cases_driven_by", world.creator) == []


async def test_driven_only_narrows_list_and_search_with_a_matching_total(world):
    handed = world.case(title="gadget handed", driver_id=world.driver)
    kept = world.case(title="gadget kept")
    await world.run("save", handed)
    await world.run("save", kept)

    read, read_total = await world.run("list", user_id=world.creator)
    write, write_total = await world.run(
        "list", user_id=world.creator, driven_only=True
    )
    assert {c.case_id for c in read} == {handed.case_id, kept.case_id}
    assert read_total == 2
    assert [c.case_id for c in write] == [kept.case_id]
    assert write_total == 1

    shared = [handed.case_id]
    as_driver, total = await world.run(
        "list", user_id=world.driver, shared_case_ids=shared, driven_only=True
    )
    assert [c.case_id for c in as_driver] == [handed.case_id] and total == 1

    found, found_total = await world.run(
        "search", query="gadget", user_id=world.creator, driven_only=True
    )
    assert [c.case_id for c in found] == [kept.case_id] and found_total == 1


async def test_a_driver_who_is_not_a_reader_lists_nothing(world):
    """``driven_only`` narrows the read scope, never widens it."""
    handed = world.case(driver_id=world.driver)
    await world.run("save", handed)

    rows, total = await world.run("list", user_id=world.driver, driven_only=True)

    assert rows == [] and total == 0


async def test_deleting_the_drivers_account_hands_the_case_back(world):
    case = world.case(driver_id=world.driver)
    await world.run("save", case)

    async with world.Session() as session:
        await session.execute(
            text("DELETE FROM users WHERE user_id = :u"), {"u": world.driver}
        )
        await session.commit()

    stored = await world.run("get", case.case_id)
    assert stored.driver_id is None
    assert stored.effective_driver_id == world.creator
