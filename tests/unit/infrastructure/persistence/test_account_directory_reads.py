"""The operator account list's two reads, on SQLite and in memory.

``list_account_metadata`` spans every enterprise with an ordinary query —
``users`` is outside row-level security — so what bounds it is what it selects
and who calls it. ``get_many_in_enterprise`` reads the manageable rows' roles,
confined to one enterprise. The PostgreSQL half (the statement on the wire,
parity with the confined listing, ``bigint`` offsets) is in
``tests/integration/security/test_admin_account_metadata_postgres.py``.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    UserModel,
)
from faultmaven.infrastructure.persistence.user_repository import (
    ACCOUNT_METADATA_COLUMNS,
    InMemoryUserRepository,
    PostgreSQLUserRepository,
    User,
)

pytestmark = pytest.mark.unit

_A, _B = "ent_dir_a", "ent_dir_b"
_AT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Columns of ``users`` the list must never select.
_NEVER_SELECTED = {
    "hashed_password",
    "sso_provider",
    "sso_provider_id",
    "dev_roles",
    "avatar_url",
    "timezone",
    "locale",
    "deleted_at",
}


def _user(user_id, enterprise_id, minutes=0, **fields):
    values = dict(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        enterprise_id=enterprise_id,
        hashed_password="SENTINEL-hash",
        sso_provider="workos",
        sso_provider_id=f"SENTINEL-subject-{user_id}",
        roles=["admin"],
        created_at=_AT + timedelta(minutes=minutes),
        updated_at=_AT + timedelta(minutes=minutes),
    )
    values.update(fields)
    return User(**values)


_FIXTURE = [
    _user("a_1", _A, 1),
    _user("a_2", _A, 2, display_name="100% uptime bot"),
    _user("a_3", _A, 3, is_active=False),
    _user("b_1", _B, 4, account_kind="service", service_channel="slack"),
    _user("b_2", _B, 5, display_name="Mixed CASE Name"),
]


@pytest.fixture
async def sqlite_repository(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dir.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[EnterpriseModel.__table__, UserModel.__table__],
        )
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        for enterprise in (_A, _B):
            session.add(
                EnterpriseModel(
                    enterprise_id=enterprise, name=enterprise, slug=enterprise
                )
            )
        await session.commit()
        repository = PostgreSQLUserRepository(session)
        for user in _FIXTURE:
            await repository.create(user)
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        repository.statements = statements
        yield repository
    await engine.dispose()


@pytest.fixture
async def memory_repository():
    repository = InMemoryUserRepository()
    for user in _FIXTURE:
        await repository.create(user)
    return repository


@pytest.fixture(params=["sqlite", "memory"])
def repository(request, sqlite_repository, memory_repository):
    return sqlite_repository if request.param == "sqlite" else memory_repository


async def _read(repository, **filters):
    return await repository.list_account_metadata(
        is_active=filters.get("is_active"),
        search=filters.get("search"),
        enterprise_id=filters.get("enterprise_id"),
        limit=filters.get("limit", 100),
        offset=filters.get("offset", 0),
    )


async def test_the_read_spans_enterprises_newest_first(repository):
    accounts, total = await _read(repository)
    assert [a.user_id for a in accounts] == ["b_2", "b_1", "a_3", "a_2", "a_1"]
    assert total == 5
    assert {a.enterprise_id for a in accounts} == {_A, _B}


async def test_the_page_carries_the_total_of_every_match(repository):
    page, total = await _read(repository, limit=2, offset=1)
    assert ([a.user_id for a in page], total) == (["b_1", "a_3"], 5)
    beyond, beyond_total = await _read(repository, limit=2, offset=40)
    assert (beyond, beyond_total) == ([], 5)


@pytest.mark.parametrize(
    "filters, expected",
    [
        ({"enterprise_id": _A}, ["a_3", "a_2", "a_1"]),
        ({"is_active": False}, ["a_3"]),
        ({"search": "mixed case"}, ["b_2"]),
        ({"search": "B_1@EXAMPLE"}, ["b_1"]),
        # Literal: as LIKE wildcards these would match every account.
        ({"search": "%"}, ["a_2"]),
        ({"search": "a_"}, ["a_3", "a_2", "a_1"]),
        ({"search": "nobody"}, []),
    ],
)
async def test_the_filters(repository, filters, expected):
    accounts, total = await _read(repository, **filters)
    assert [a.user_id for a in accounts] == expected
    assert total == len(expected)


async def test_the_row_is_the_eleven_fields_and_no_credential(repository):
    accounts, _ = await _read(repository, search="b_1")
    (account,) = accounts
    assert set(vars(account)) == set(ACCOUNT_METADATA_COLUMNS)
    assert (account.account_kind, account.service_channel) == ("service", "slack")
    assert "SENTINEL" not in repr(accounts)


async def test_the_statement_selects_only_the_eleven_columns(sqlite_repository):
    sqlite_repository.statements.clear()
    await _read(sqlite_repository)
    (statement,) = [
        s for s in sqlite_repository.statements if re.search(r"\bFROM users\b", s)
    ]
    select_list = re.split(r"\sFROM\s", statement, maxsplit=1)[0]
    columns = {column.name for column in UserModel.__table__.columns}
    selected = {
        name for name in columns if re.search(rf"\busers\.{name}\b", select_list)
    }
    assert selected == set(ACCOUNT_METADATA_COLUMNS)
    assert _NEVER_SELECTED <= columns and not (_NEVER_SELECTED & selected)


async def test_a_nul_search_is_an_empty_page_without_a_query(sqlite_repository):
    """No stored text contains NUL — and PostgreSQL's driver refuses to bind
    one — so the answer is known without asking the database."""
    sqlite_repository.statements.clear()
    assert await _read(sqlite_repository, search="a\x00") == ([], 0)
    assert sqlite_repository.statements == []


async def test_an_offset_past_bigint_is_an_empty_page_with_the_true_total(
    sqlite_repository,
):
    """Only the count reaches the database; the offset is never bound."""
    sqlite_repository.statements.clear()
    accounts, total = await _read(sqlite_repository, enterprise_id=_A, offset=2**63)
    assert (accounts, total) == ([], 3)
    assert all("OFFSET" not in s.upper() for s in sqlite_repository.statements)


async def test_the_roles_read_answers_only_the_named_enterprise(repository):
    users = await repository.get_many_in_enterprise(_A, ["a_1", "b_1", "nobody"])
    assert [user.user_id for user in users] == ["a_1"]
    assert users[0].roles == ["admin"]
    assert await repository.get_many_in_enterprise(_A, []) == []


def test_the_cross_enterprise_read_has_exactly_one_caller():
    """``users`` is outside row-level security, so where this read is called
    from is part of what bounds it: only the account list's multi arm, which
    sits behind the operator role and records the access first."""
    callers = []
    for path in (_REPO_ROOT / "faultmaven").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "list_account_metadata"
                ):
                    callers.append((str(path.relative_to(_REPO_ROOT)), function.name))
    outside_the_repository = {
        caller
        for caller in callers
        if caller[0] != "faultmaven/infrastructure/persistence/user_repository.py"
    }
    assert outside_the_repository == {
        ("faultmaven/api/routes/admin.py", "_list_across_enterprises")
    }
