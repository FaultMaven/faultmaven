"""Revision 016 on a real PostgreSQL (#1924).

``016_no_email_derived_display_names`` relabels the display names the old
writers derived from an email. ``users`` is not enrolled in row-level security
(the login path reads it before any tenant is bound), so the role that runs the
migrations must see every enterprise's accounts with no ``row_security`` toggle.
The probe migrates as a NON-superuser owner, Cloud's shape: a superuser bypasses
row-level security whatever the policy says, so a probe run as one would pass
even if ``users`` were filtered.

The SQLite half, which pins exactly which row shapes change, is
``TestNoEmailDerivedNamesRevision`` in
``tests/integration/test_alembic_migrations.py``.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.security.conftest import (
    alembic_on,
    database_of_its_own,
    drop_role_sql,
    limited_url,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

#: ``016_no_email_derived_display_names``.
_REVISION = "b5334d07ae6c"  # pragma: allowlist secret
#: ``013_evidence_is_submitted_data``, the revision 016 parents onto.
_PARENT_REVISION = "9b3e5c71d2a4"  # pragma: allowlist secret
_PW = "fm_display_name_probe_pw"  # pragma: allowlist secret


async def _execute(url: str, *statements: str, params: dict | None = None) -> list:
    """Run ``statements`` in one transaction; return the last one's rows."""
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            rows: list = []
            for statement in statements:
                result = await conn.execute(text(statement), params or {})
                rows = list(result.fetchall()) if result.returns_rows else []
            return rows
    finally:
        await engine.dispose()


#: (username, email, display_name, sso_provider, relabelled?) per enterprise;
#: ``{n}`` makes each enterprise's values unique.
_SHAPES = (
    ("sam{n}", "sam{n}@acme.com", "sam{n}", "workos", True),
    ("jane{n}", "jane{n}@acme.com", "Jane Doe", "workos", False),
    ("frank{n}-2", "frank{n}@acme.com", "FRANK{n}", "workos", True),
    ("pat{n}", "pat{n}@acme.com", "pat{n}@example.com", "workos", True),
    (
        "alice.smith{n}@example.com",
        "alice.smith{n}@example.com",
        "Alice Smith{n}@Example Com",
        None,
        True,
    ),
    ("quinn{n}", "quinn{n}@example.com", "Quinn <quinn{n}@example.com>", None, True),
    ("erin{n}", "erin{n}@example.com", "erin{n}@example.com", None, True),
    ("samsmith{n}", "sam.smith{n}@example.com", "Sam Smith", None, False),
    ("samax{n}", "samAx{n}@example.com", "sam_x{n}", None, False),
)


async def _seed(url: str) -> dict:
    """Two enterprises, each holding every row shape of ``_SHAPES``. Returns
    user_id -> the display name 016 must leave."""
    tag = uuid.uuid4().hex[:8]
    expected: dict = {}
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            for index, enterprise in enumerate((f"ent_a_{tag}", f"ent_b_{tag}")):
                await conn.execute(
                    text(
                        "INSERT INTO enterprises (enterprise_id, name, slug) "
                        "VALUES (:e, :e, :e)"
                    ),
                    {"e": enterprise},
                )
                n = f"{index}{tag}"
                for username, email, name, provider, derived in _SHAPES:
                    user_id = str(uuid.uuid4())
                    await conn.execute(
                        text(
                            "INSERT INTO users (user_id, enterprise_id, username, "
                            "email, display_name, hashed_password, sso_provider, "
                            "sso_provider_id) VALUES (:u, :e, :username, :email, "
                            ":name, :password, :provider, :sub)"
                        ),
                        {
                            "u": user_id,
                            "e": enterprise,
                            "username": username.format(n=n),
                            "email": email.format(n=n),
                            "name": name.format(n=n),
                            "password": None if provider else "x",
                            "provider": provider,
                            "sub": f"sub_{user_id}" if provider else None,
                        },
                    )
                    expected[user_id] = (
                        f"Member {user_id[:4]}" if derived else name.format(n=n)
                    )
    finally:
        await engine.dispose()
    return expected


async def _names(url: str, user_ids) -> dict:
    rows = await _execute(url, "SELECT user_id, display_name FROM users")
    return {user_id: name for user_id, name in rows if user_id in user_ids}


async def test_the_owner_relabels_every_enterprises_derived_names():
    superuser_url = os.environ["DATABASE_URL"]
    role = f"fm_016_owner_{uuid.uuid4().hex[:8]}"
    await _execute(superuser_url, drop_role_sql(role))
    await _execute(
        superuser_url,
        f"CREATE ROLE {role} LOGIN PASSWORD '{_PW}' NOSUPERUSER NOBYPASSRLS",
    )
    try:
        async with database_of_its_own(
            superuser_url, "fm_016_owner", owner=role
        ) as su_url:
            owner_url = limited_url(su_url, role, _PW)
            result = alembic_on(owner_url, f"upgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            expected = await _seed(owner_url)

            result = alembic_on(owner_url, f"upgrade {_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert "016: relabelled 12 email-derived display name(s)" in (
                result.stderr
            ), result.stderr[-2000:]
            assert await _names(su_url, expected) == expected

            result = alembic_on(owner_url, f"downgrade {_PARENT_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert await _names(su_url, expected) == expected

            result = alembic_on(owner_url, f"upgrade {_REVISION}")
            assert result.returncode == 0, result.stderr[-2000:]
            assert "016: relabelled 0 email-derived display name(s)" in (
                result.stderr
            ), result.stderr[-2000:]
    finally:
        await _execute(superuser_url, drop_role_sql(role))
