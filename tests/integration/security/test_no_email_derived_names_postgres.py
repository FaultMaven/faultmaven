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


async def _seed(url: str) -> dict:
    """Two enterprises, each with an SSO account named by its username, an
    SSO account the IdP named, and a local account whose name title-cases its
    address. Returns user_id -> the display name 016 must leave."""
    tag = uuid.uuid4().hex[:8]
    expected: dict = {}
    statements: list[str] = []
    for index, enterprise in enumerate((f"ent_a_{tag}", f"ent_b_{tag}")):
        statements.append(
            "INSERT INTO enterprises (enterprise_id, name, slug) "
            f"VALUES ('{enterprise}', 'Enterprise {enterprise}', '{enterprise}')"
        )
        derived, named, local = (str(uuid.uuid4()) for _ in range(3))
        rows = (
            (
                derived,
                f"sam{index}{tag}",
                f"sam{index}{tag}",
                "'workos'",
                f"Member {derived[:4]}",
            ),
            (named, f"jane{index}{tag}", "Jane Doe", "'workos'", "Jane Doe"),
            (
                local,
                f"alice.smith{index}@example.com",
                f"Alice Smith{index}@Example Com",
                "NULL",
                f"Member {local[:4]}",
            ),
        )
        for user_id, username, display_name, provider, after in rows:
            sso_id = f"'sub_{user_id}'" if provider != "NULL" else "NULL"
            password = "NULL" if provider != "NULL" else "'x'"
            statements.append(
                "INSERT INTO users (user_id, enterprise_id, username, email, "
                "display_name, hashed_password, sso_provider, sso_provider_id) "
                f"VALUES ('{user_id}', '{enterprise}', '{username}', "
                f"'{user_id}@example.com', '{display_name}', {password}, "
                f"{provider}, {sso_id})"
            )
            expected[user_id] = after
    await _execute(url, *statements)
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
            assert "016: relabelled 4 email-derived display name(s)" in (
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
