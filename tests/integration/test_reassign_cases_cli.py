"""``fm-reassign-cases`` writes exactly what it claims, against a real database.

The unit tests cover the guards — this covers the transaction, because the three
things most worth pinning here are only observable as rows:

* the **``version`` bump**, without which an in-flight turn's optimistic-
  concurrency save silently restores the old owner (the versioned UPDATE in
  ``PostgreSQLHybridCaseRepository`` writes ``user_id`` back from its in-memory
  ``Case``);
* the **team share**, without which the migrated cases become the only Slack
  cases in the organization no human can see;
* the rows it must **not** touch — ``case_messages.author_id`` and
  ``uploaded_files.uploaded_by`` are attribution, which migration 037 and
  ADR-011 D5 require to outlive the account it describes.

A mocked session could assert the SQL string; only a database can assert that the
owner moved, the version went up by one, and the transcript still says who wrote
each turn.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.cli.reassign_cases import _apply, _Refused

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENTERPRISE = "ent-0000-1111-2222"
ORG = "0308aea6-30eb-44d8-b9ef-f16a6c1b1584"
OLD_OWNER = "45495d14-9436-40ec-9fde-4605016a71f0"
NEW_OWNER = "9f1c5d20-1111-4222-8333-444455556666"
BYSTANDER = "3123222b-1527-4326-a213-f70bfefc6037"
TEAM_A = "team-aaaa-1111"
TEAM_B = "team-bbbb-2222"

MOVED = ["case_aaa111", "case_bbb222", "case_ccc333"]
#: Owned by a different account in the same organization. Nothing this command
#: does may reach it — the sweep is by owner, not by organization.
UNRELATED = "case_zzz999"


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """A real SQLite database, seeded, with ``get_db_session`` pointed at it.

    Deliberately **touches no module-level state**. An earlier version set
    ``DATABASE_URL`` and reset the engine and settings singletons, the way the
    sibling integration tests do — and that leaks: the whole suite runs in one
    process, so nulling the shared engine strands every later test that relied
    on tables created through it. It surfaced as ``no such table: enterprises``
    in the composition-root tests, which pass in isolation and failed after this
    file.

    Patching the session factory instead keeps the real SQL, the real models and
    the real transaction semantics — all this file asserts — while leaving the
    process exactly as it found it. It also means the dev database
    (``data/faultmaven.db``, what the default ``DATABASE_URL`` resolves to) can
    never be reached from here.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'reassign.db'}"

    from faultmaven.infrastructure.persistence.models import Base

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        await conn.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES (:e, 'FaultMaven', 'faultmaven-ent')"
            ),
            {"e": ENTERPRISE},
        )
        await conn.execute(
            text(
                "INSERT INTO organizations "
                "(organization_id, enterprise_id, name, slug) "
                "VALUES (:o, :e, 'FaultMaven', 'faultmaven')"
            ),
            {"o": ORG, "e": ENTERPRISE},
        )
        for uid, name in (
            (OLD_OWNER, "slack-agent"),
            (NEW_OWNER, "slack-T0B9XNZDR44"),
            (BYSTANDER, "model-eval"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO users "
                    "(user_id, enterprise_id, username, email, display_name) "
                    "VALUES (:u, :ent, :n, :e, :n)"
                ),
                {
                    "u": uid,
                    "ent": ENTERPRISE,
                    "n": name,
                    "e": f"{name}@example.test",
                },
            )
        for case_id, owner in [(c, OLD_OWNER) for c in MOVED] + [
            (UNRELATED, BYSTANDER)
        ]:
            await conn.execute(
                text(
                    "INSERT INTO cases "
                    "(case_id, organization_id, user_id, title, source, version) "
                    "VALUES (:c, :o, :u, :t, 'slack', 7)"
                ),
                {"c": case_id, "o": ORG, "u": owner, "t": f"title {case_id}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO case_messages "
                    "(message_id, case_id, organization_id, turn_number, role, "
                    " content, author_id) "
                    "VALUES (:m, :c, :o, 1, 'user', 'help', :a)"
                ),
                {"m": f"msg_{case_id}", "c": case_id, "o": ORG, "a": owner},
            )
            await conn.execute(
                text(
                    "INSERT INTO uploaded_files "
                    "(file_id, case_id, organization_id, filename, size_bytes, "
                    " uploaded_by) "
                    "VALUES (:f, :c, :o, 'log.txt', 12, :u)"
                ),
                {"f": f"file_{case_id}", "c": case_id, "o": ORG, "u": owner},
            )
    await engine.dispose()

    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _session(database_url=None):
        """Stands in for ``get_db_session``, with its commit/rollback contract.

        The rollback on exception is load-bearing here — it is what the
        concurrent-change test asserts — so it is reproduced, not approximated.
        """
        session = sessions()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.get_db_session", _session
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _rows(engine, sql, params=None):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).fetchall()


async def test_reassignment_moves_ownership_and_bumps_version(db):
    """The positive path: the owner moves, and every moved case's version goes up.

    The version bump is asserted as ``8`` (seeded at 7), not merely "changed":
    the OCC check the bump exists to trip is ``version = :expected_version``, so
    an off-by-anything is as good as no bump at all.
    """
    await _apply(
        organization_id=ORG,
        case_ids=MOVED,
        from_user_id=OLD_OWNER,
        to_user_id=NEW_OWNER,
        team_ids=[TEAM_A, TEAM_B],
        revoke_team_ids=[],
        actor="tester@host",
    )

    rows = await _rows(
        db,
        "SELECT case_id, user_id, version, organization_id FROM cases "
        "WHERE case_id IN (:a, :b, :c) ORDER BY case_id",
        {"a": MOVED[0], "b": MOVED[1], "c": MOVED[2]},
    )
    assert [r[0] for r in rows] == sorted(MOVED)
    assert {r[1] for r in rows} == {NEW_OWNER}
    assert {r[2] for r in rows} == {8}
    # The organization is the RLS key and was already correct; this is an owner
    # move, and rewriting it would be the "Organization move" ADR-012 D10
    # predicted and measurement disproved.
    assert {r[3] for r in rows} == {ORG}


async def test_reassignment_shares_each_case_to_every_team_of_the_new_owner(db):
    """One share row per (case, team) — what auto-share would have written."""
    await _apply(
        organization_id=ORG,
        case_ids=MOVED,
        from_user_id=OLD_OWNER,
        to_user_id=NEW_OWNER,
        team_ids=[TEAM_A, TEAM_B],
        revoke_team_ids=[],
        actor="tester@host",
    )

    rows = await _rows(
        db,
        "SELECT resource_type, resource_id, scope_type, scope_id, "
        "organization_id, created_by FROM resource_shares",
    )
    assert {(r[1], r[3]) for r in rows} == {
        (c, t) for c in MOVED for t in (TEAM_A, TEAM_B)
    }
    assert {r[0] for r in rows} == {"case"}
    assert {r[2] for r in rows} == {"team"}
    assert {r[4] for r in rows} == {ORG}
    # The new owner is the sharer, matching _share_case_with_team's created_by.
    assert {r[5] for r in rows} == {NEW_OWNER}


async def test_reassignment_records_one_audit_row_per_case(db):
    await _apply(
        organization_id=ORG,
        case_ids=MOVED,
        from_user_id=OLD_OWNER,
        to_user_id=NEW_OWNER,
        team_ids=[TEAM_A],
        revoke_team_ids=[],
        actor="tester@host",
    )

    rows = await _rows(
        db,
        "SELECT event_type, event_category, resource_type, resource_id, "
        "organization_id, details FROM user_audit_log ORDER BY resource_id",
    )
    assert [r[3] for r in rows] == sorted(MOVED)
    assert {r[0] for r in rows} == {"case_reassigned"}
    assert {r[1] for r in rows} == {"administration"}
    assert {r[2] for r in rows} == {"case"}
    assert {r[4] for r in rows} == {ORG}
    assert all(OLD_OWNER in r[5] and NEW_OWNER in r[5] for r in rows)


async def test_attribution_is_not_rewritten(db):
    """``author_id`` and ``uploaded_by`` still name the account that acted.

    Migration 037 makes ``author_id`` un-foreign-keyed precisely so attribution
    outlives the account; ADR-011 D5 calls the record un-backfillable. The turns
    were submitted by the old principal, and a reassignment that edited them
    would be falsifying history to tidy a migration.
    """
    await _apply(
        organization_id=ORG,
        case_ids=MOVED,
        from_user_id=OLD_OWNER,
        to_user_id=NEW_OWNER,
        team_ids=[TEAM_A],
        revoke_team_ids=[],
        actor="tester@host",
    )

    authors = await _rows(db, "SELECT DISTINCT author_id FROM case_messages")
    assert {r[0] for r in authors} == {OLD_OWNER, BYSTANDER}

    uploaders = await _rows(db, "SELECT DISTINCT uploaded_by FROM uploaded_files")
    assert {r[0] for r in uploaders} == {OLD_OWNER, BYSTANDER}


async def test_a_case_owned_by_someone_else_is_untouched(db):
    """The sweep is by owner; another account's case in the same org must not move."""
    await _apply(
        organization_id=ORG,
        case_ids=MOVED,
        from_user_id=OLD_OWNER,
        to_user_id=NEW_OWNER,
        team_ids=[TEAM_A],
        revoke_team_ids=[],
        actor="tester@host",
    )

    rows = await _rows(
        db,
        "SELECT user_id, version FROM cases WHERE case_id = :c",
        {"c": UNRELATED},
    )
    assert rows[0][0] == BYSTANDER
    assert rows[0][1] == 7


async def test_a_case_that_changed_owner_underneath_rolls_the_whole_run_back(db):
    """A matched-zero UPDATE aborts everything — no partial reassignment.

    This is why the UPDATE carries ``AND user_id = :from_user_id``: without it
    the write would succeed against whatever the case had become, re-owning a
    case this run never checked.
    """
    async with db.begin() as conn:
        await conn.execute(
            text("UPDATE cases SET user_id = :u WHERE case_id = :c"),
            {"u": BYSTANDER, "c": MOVED[1]},
        )

    with pytest.raises(_Refused, match=MOVED[1]):
        await _apply(
            organization_id=ORG,
            case_ids=MOVED,
            from_user_id=OLD_OWNER,
            to_user_id=NEW_OWNER,
            team_ids=[TEAM_A],
            revoke_team_ids=[],
            actor="tester@host",
        )

    # The first case's UPDATE succeeded before the second one failed; the
    # rollback must have undone it too.
    rows = await _rows(
        db,
        "SELECT user_id, version FROM cases WHERE case_id = :c",
        {"c": MOVED[0]},
    )
    assert rows[0] == (OLD_OWNER, 7)
    assert await _rows(db, "SELECT * FROM resource_shares") == []
    assert await _rows(db, "SELECT * FROM user_audit_log") == []


async def test_rerunning_does_not_duplicate_share_rows(db):
    """The share upsert is idempotent, so a retry after a rollback is safe."""
    for _ in range(2):
        async with db.begin() as conn:
            await conn.execute(
                text("UPDATE cases SET user_id = :u WHERE case_id IN (:a, :b, :c)"),
                {"u": OLD_OWNER, "a": MOVED[0], "b": MOVED[1], "c": MOVED[2]},
            )
        await _apply(
            organization_id=ORG,
            case_ids=MOVED,
            from_user_id=OLD_OWNER,
            to_user_id=NEW_OWNER,
            team_ids=[TEAM_A],
            revoke_team_ids=[],
            actor="tester@host",
        )

    rows = await _rows(db, "SELECT resource_id FROM resource_shares")
    assert sorted(r[0] for r in rows) == sorted(MOVED)


# -- the cross-tenant guards, against a real database -------------------------
#
# These exist because a code review proved both could be deleted with the whole
# suite green: `_swept_case_ids` was AsyncMock'd in the unit tests and never
# reached from here, and nothing observed the RLS binding at all. A guard no
# test can see removed is a guard that will eventually be removed.

OTHER_ORG = "9999aaaa-0000-4111-8222-333344445555"


async def test_the_sweep_does_not_see_another_organizations_cases(db):
    """The redundant `organization_id` predicate, exercised.

    On SQLite there is no RLS at all, which is exactly the connection this
    predicate exists for: without it the sweep spans tenants and the run moves
    a stranger's cases while reporting success.
    """
    from faultmaven.cli.reassign_cases import _swept_case_ids

    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organizations "
                "(organization_id, enterprise_id, name, slug) "
                "VALUES (:o, :e, 'Other', 'other')"
            ),
            {"o": OTHER_ORG, "e": ENTERPRISE},
        )
        await conn.execute(
            text(
                "INSERT INTO cases "
                "(case_id, organization_id, user_id, title, source, version) "
                "VALUES ('case_other', :o, :u, 'theirs', 'slack', 7)"
            ),
            {"o": OTHER_ORG, "u": OLD_OWNER},
        )

    assert await _swept_case_ids(ORG, OLD_OWNER) == set(MOVED)


async def test_the_reassignment_will_not_touch_another_organizations_case(db):
    """Same predicate on the write side. A case id named in the file but living
    in another tenant must not be re-owned — it is not this run's to move."""
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organizations "
                "(organization_id, enterprise_id, name, slug) "
                "VALUES (:o, :e, 'Other', 'other')"
            ),
            {"o": OTHER_ORG, "e": ENTERPRISE},
        )
        await conn.execute(
            text(
                "INSERT INTO cases "
                "(case_id, organization_id, user_id, title, source, version) "
                "VALUES ('case_other', :o, :u, 'theirs', 'slack', 7)"
            ),
            {"o": OTHER_ORG, "u": OLD_OWNER},
        )

    with pytest.raises(_Refused, match="case_other"):
        await _apply(
            organization_id=ORG,
            case_ids=[*MOVED, "case_other"],
            from_user_id=OLD_OWNER,
            to_user_id=NEW_OWNER,
            team_ids=[TEAM_A],
            revoke_team_ids=[],
            actor="tester@host",
        )

    rows = await _rows(
        db, "SELECT user_id, organization_id FROM cases WHERE case_id = 'case_other'"
    )
    assert rows[0] == (OLD_OWNER, OTHER_ORG)


async def test_the_old_owners_team_shares_are_revoked(db):
    """`case_scope_where` admits any team holding a share row, so a share to a
    team the OLD owner belonged to keeps that team reading a case that has been
    moved away from them."""
    async with db.begin() as conn:
        for case_id in MOVED:
            await conn.execute(
                text(
                    "INSERT INTO resource_shares "
                    "(share_id, resource_type, resource_id, scope_type, scope_id, "
                    " organization_id) "
                    "VALUES (:s, 'case', :c, 'team', :t, :o)"
                ),
                {"s": f"old_{case_id}", "c": case_id, "t": "team-old", "o": ORG},
            )

    await _apply(
        organization_id=ORG,
        case_ids=MOVED,
        from_user_id=OLD_OWNER,
        to_user_id=NEW_OWNER,
        team_ids=[TEAM_A],
        revoke_team_ids=["team-old"],
        actor="tester@host",
    )

    scopes = {r[0] for r in await _rows(db, "SELECT scope_id FROM resource_shares")}
    assert scopes == {TEAM_A}


async def test_a_team_both_owners_share_is_not_revoked(db):
    """The overlap is re-granted, not flapped: revoking it would drop access the
    new owner is entitled to, between two statements of one transaction."""
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO resource_shares "
                "(share_id, resource_type, resource_id, scope_type, scope_id, "
                " organization_id) VALUES ('keep', 'case', :c, 'team', :t, :o)"
            ),
            {"c": MOVED[0], "t": TEAM_A, "o": ORG},
        )

    await _apply(
        organization_id=ORG,
        case_ids=MOVED,
        from_user_id=OLD_OWNER,
        to_user_id=NEW_OWNER,
        team_ids=[TEAM_A],
        revoke_team_ids=[],
        actor="tester@host",
    )

    rows = await _rows(
        db,
        "SELECT scope_id FROM resource_shares WHERE resource_id = :c",
        {"c": MOVED[0]},
    )
    assert [r[0] for r in rows] == [TEAM_A]


async def test_the_reassignment_stamps_updated_at(db):
    """`onupdate=func.now()` does not fire for Core text SQL, so a row would be
    written while claiming it was not."""
    before = (
        await _rows(
            db, "SELECT updated_at FROM cases WHERE case_id = :c", {"c": MOVED[0]}
        )
    )[0][0]

    await _apply(
        organization_id=ORG,
        case_ids=MOVED,
        from_user_id=OLD_OWNER,
        to_user_id=NEW_OWNER,
        team_ids=[TEAM_A],
        revoke_team_ids=[],
        actor="tester@host",
    )

    after = (
        await _rows(
            db, "SELECT updated_at FROM cases WHERE case_id = :c", {"c": MOVED[0]}
        )
    )[0][0]
    assert after != before


async def test_the_audit_row_names_no_false_actor(db):
    """`user_id` is the row's SUBJECT. Attributing this to the new owner would
    read as the service account having moved its own cases; a CLI has no
    authenticated principal, so it claims none and records provenance instead."""
    await _apply(
        organization_id=ORG,
        case_ids=MOVED,
        from_user_id=OLD_OWNER,
        to_user_id=NEW_OWNER,
        team_ids=[TEAM_A],
        revoke_team_ids=["team-old"],
        actor="alice@ops-box",
    )

    rows = await _rows(db, "SELECT user_id, details FROM user_audit_log")
    assert {r[0] for r in rows} == {None}
    assert all("alice@ops-box" in r[1] for r in rows)
    assert all("team-old" in r[1] for r in rows)
