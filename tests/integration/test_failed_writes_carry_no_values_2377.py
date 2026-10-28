"""A failed write's exception text carries none of its values (#2377).

Three channels carried them into every handler that logs ``{e}`` or a traceback:
SQLAlchemy's ``[parameters: (…)]`` suffix, on every dialect; on PostgreSQL the
server's DETAIL ("Failing row contains (…)", "Key (email)=(…)") and CONTEXT;
and the primary message where it quotes its input, a class-22 error's on both
drivers and asyncpg's own encoding error. Each constraint case writes a row that
fails FK, UNIQUE, CHECK or NOT NULL on ``users`` (no RLS) and on
``knowledge_items``, every bound value carrying ``MARK``. Each input case sends
a value its type refuses. PostgreSQL runs as CI's superuser, the role the server
tells the most, with CONTEXT's parameter list switched on. The class, SQLSTATE,
constraint name and the refused type stay. The migration engine (psycopg2) is
built as ``alembic/env.py`` does.
"""

from __future__ import annotations

import os
import traceback
from uuid import uuid4

import pytest
from sqlalchemy import delete, engine_from_config, pool, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from faultmaven.config.tenant_context import _current_enterprise_id
from faultmaven.infrastructure.persistence import database
from faultmaven.infrastructure.persistence.db_errors import driver_error, sqlstate
from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    KnowledgeItemModel,
    OrganizationModel,
    UserModel,
)
from faultmaven.infrastructure.persistence.value_free_errors import (
    install_value_free_errors,
)

pytestmark = pytest.mark.integration

requires_postgresql = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
)

MARK = "mk2377"
RUN = uuid4().hex[:8]
ENTERPRISE = f"{MARK}-{RUN}"
SEED_EMAIL = f"{MARK}-{RUN}@example.com"
SEED_ITEM = f"{MARK}-item-{RUN}"

#: Each constraint case's (SQLSTATE, constraint name), as PostgreSQL reports them.
EXPECTED = {
    "users FK": ("23503", "users_enterprise_id_fkey"),
    "users UNIQUE": ("23505", "users_email_key"),
    "users CHECK": ("23514", "users_display_name_not_empty"),
    "users NOT NULL": ("23502", None),
    "knowledge_items FK": ("23503", "knowledge_items_owner_id_fkey"),
    "knowledge_items UNIQUE": ("23505", "knowledge_items_pkey"),
    "knowledge_items CHECK": ("23514", "knowledge_items_item_type_check"),
    "knowledge_items NOT NULL": ("23502", None),
}

#: Each input case's statement, refused value, SQLSTATE and the text that stays.
#: The value is cast on the server, so both drivers get PostgreSQL's class 22.
INPUTS = {
    "22P02": ("CAST(CAST(:v AS text) AS integer)", f'{MARK} "q" x', "22P02", "integer"),
    "22007": (
        "CAST(CAST(:v AS text) AS timestamptz)",
        f"{MARK} x",
        "22007",
        "time zone",
    ),
    "22008": ("CAST(CAST(:v AS text) AS date)", "2377-13-45", "22008", "out of range"),
    "22003": (
        "CAST(CAST(:v AS text) AS integer)",
        "23772377237",
        "22003",
        "type integer",
    ),
}
#: asyncpg encodes this argument itself, and refuses it before the server sees it.
ARGUMENT = {"argument": ("CAST(:v AS uuid)", f"{MARK} x", "22000", "argument $1")}


def _user(**fields) -> UserModel:
    n = uuid4().hex[:8]
    return UserModel(
        **{
            "user_id": f"{MARK}-{n}",
            "enterprise_id": ENTERPRISE,
            "username": f"{MARK}-{n}",
            "email": f"{MARK}-{n}@example.com",
            "display_name": f"{MARK} display",
            **fields,
        }
    )


def _item(**fields) -> KnowledgeItemModel:
    return KnowledgeItemModel(
        **{
            "item_id": f"{MARK}-{uuid4().hex[:8]}",
            "enterprise_id": ENTERPRISE,
            "scope": "personal",
            "title": f"{MARK} title",
            "content": f"---\n{MARK} body\nsecond line",
            "item_type": "runbook",
            **fields,
        }
    )


def _failing_rows() -> dict:
    return {
        "users FK": _user(enterprise_id=f"{MARK}-missing"),
        "users UNIQUE": _user(email=SEED_EMAIL),
        "users CHECK": _user(display_name="   "),
        "users NOT NULL": _user(username=None),
        "knowledge_items FK": _item(owner_id=f"{MARK}-missing"),
        "knowledge_items UNIQUE": _item(item_id=SEED_ITEM),
        "knowledge_items CHECK": _item(item_type="bogus"),
        "knowledge_items NOT NULL": _item(item_type=None),
    }


def _seed_rows() -> list:
    return [
        EnterpriseModel(enterprise_id=ENTERPRISE, name=MARK, slug=ENTERPRISE),
        _user(email=SEED_EMAIL),
        _item(item_id=SEED_ITEM),
    ]


def _deletes() -> list:
    models = (KnowledgeItemModel, UserModel, EnterpriseModel)
    return [delete(m).where(m.enterprise_id == ENTERPRISE) for m in models]


async def _app_engine_failures(url: str, inputs: dict, *, context_params=False):
    """Each failing row and input, written through the app's ``get_db_session``."""
    async with database.get_db_session(url) as session:
        session.add_all(_seed_rows())
    failures = {}
    try:
        writes = [(case, row, None) for case, row in _failing_rows().items()]
        writes += [(case, None, spec) for case, spec in inputs.items()]
        for case, row, spec in writes:
            try:
                async with database.get_db_session(url) as session:
                    if context_params:
                        await session.execute(
                            text("SET LOCAL log_parameter_max_length_on_error = -1")
                        )
                    if row is not None:
                        session.add(row)
                    else:
                        await session.execute(text(f"SELECT {spec[0]}"), {"v": spec[1]})
            except Exception as exc:
                failures[case] = exc
    finally:
        async with database.get_db_session(url) as session:
            for statement in _deletes():
                await session.execute(statement)
    return failures


def _texts(exc) -> dict:
    return {
        "str(e)": str(exc),
        "str(e.orig)": str(exc.orig),
        "traceback": "".join(traceback.format_exception(exc)),
    }


def _wrong(failures: dict, fields=None, inputs: dict | None = None) -> dict:
    """Per case, what a logged ``{e}`` or traceback would leak, or what was lost."""
    found = {}
    cases = {c: (MARK, IntegrityError, f, None) for c, f in EXPECTED.items()}
    cases |= {
        c: (v, DBAPIError, (s, None), k) for c, (_, v, s, k) in (inputs or {}).items()
    }
    for case, (secret, cls, expected, kept) in cases.items():
        exc = failures.get(case)
        if not isinstance(exc, cls):
            found[case] = [f"raised {type(exc).__name__}"]
            continue
        wrong = [name for name, value in _texts(exc).items() if secret in value]
        if fields is not None and fields(exc) != expected:
            wrong.append(f"{fields(exc)} != {expected}")
        if kept is not None and kept not in str(exc):
            wrong.append(f"lost {kept!r}")
        if wrong:
            found[case] = wrong
    return found


@pytest.fixture
async def fresh_engine(monkeypatch):
    """``get_engine`` builds anew; the module's own engine is back afterwards."""
    for name in ("_engine", "_engine_bounds", "_session_factory"):
        monkeypatch.setattr(database, name, getattr(database, name))
    database.reset_engine()
    yield
    await database.close_database()


async def test_sqlite_failed_writes_carry_no_values(tmp_path, fresh_engine):
    url = f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    tables = [EnterpriseModel, OrganizationModel, UserModel, KnowledgeItemModel]
    async with database.get_engine(url).begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all, tables=[model.__table__ for model in tables]
        )

    failures = await _app_engine_failures(url, {})

    assert _wrong(failures) == {}


@pytest.mark.postgres
@requires_postgresql
async def test_postgresql_failed_writes_carry_no_values(fresh_engine):
    token = _current_enterprise_id.set(ENTERPRISE)
    try:
        failures = await _app_engine_failures(
            os.environ["DATABASE_URL"], INPUTS | ARGUMENT, context_params=True
        )
    finally:
        _current_enterprise_id.reset(token)

    def fields(exc):
        return sqlstate(exc), getattr(driver_error(exc), "constraint_name", None)

    assert _wrong(failures, fields, INPUTS | ARGUMENT) == {}
    # CONTEXT never reaches asyncpg's text; it is cleared from the error all the same.
    values = (MARK, *(spec[1] for spec in INPUTS.values()))
    held = {c: repr(vars(driver_error(e))) for c, e in failures.items()}
    assert [c for c, text_ in held.items() if any(v in text_ for v in values)] == []


@pytest.mark.postgres
@requires_postgresql
def test_the_migration_engine_failed_writes_carry_no_values():
    pytest.importorskip("psycopg2")
    url = make_url(os.environ["DATABASE_URL"]).set(drivername="postgresql+psycopg2")
    engine = engine_from_config(
        {"sqlalchemy.url": url.render_as_string(hide_password=False)},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    install_value_free_errors(engine)
    with Session(engine) as session, session.begin():
        session.add_all(_seed_rows())
    failures = {}
    try:
        for case, row in _failing_rows().items():
            try:
                with Session(engine) as session, session.begin():
                    session.add(row)
            except Exception as exc:
                failures[case] = exc
        for case, (cast, value, _, _) in INPUTS.items():
            try:
                with engine.connect() as conn:
                    conn.execute(text(f"SELECT {cast}"), {"v": value})
            except Exception as exc:
                failures[case] = exc
    finally:
        with engine.begin() as conn:
            for statement in _deletes():
                conn.execute(statement)
        engine.dispose()

    def fields(exc):
        return exc.orig.pgcode, exc.orig.diag.constraint_name

    assert _wrong(failures, fields, INPUTS) == {}
