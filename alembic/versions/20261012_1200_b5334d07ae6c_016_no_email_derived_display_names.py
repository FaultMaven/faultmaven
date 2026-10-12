"""016_no_email_derived_display_names

No display name derived from an email address (#1924). A display name is shown
to people other than its owner: a shared case's creator and driver, the
driver-candidate list, a verified runbook's ``verified_by``. Owner ruling
(2026-10-12): an account with no real name gets ``Member`` plus the first four
characters of its user id (``Member 3f9a``); a data migration replaces the
email-derived names already stored; a name the IdP sends later overwrites it.

Which rows change
-----------------

No column records that a name was derived, so the rows are matched on the shape
the old writers left:

1. **SSO accounts** (``sso_provider IS NOT NULL``) whose ``display_name`` equals
   their ``username``. JIT provisioning stored the username when the IdP sent no
   name, and an SSO username is the email's sanitized local part.
2. **Local accounts** whose ``username`` contains ``@`` and whose
   ``display_name`` equals ``DatabaseUserStore._generate_display_name(username)``
   (frozen below): registration with an address as the username and no display
   name stored a title-cased rendering of the address.

Each becomes ``Member`` and the first four characters of its ``user_id``. Every
other row is untouched: an IdP-sent name, an explicit display name, and the
title-cased name of a plain (``@``-free) username, which its user or the
operator chose and which carries no address.

The Redis user store's form (title-casing the local part only) is NOT matched:
that store never writes these rows, and a name a user typed that happens to
equal it ("Alice Smith" for ``alice.smith@example.com``) would be relabelled
with no way back. Rows that match it but not the database store's form are
counted and logged, never changed.

Known misclassifications
------------------------

* **False positive (SSO):** an IdP name that equals the derived username (the
  IdP sends ``name = "alice"`` for ``alice@…``) is relabelled. It heals itself:
  the next sign-in's profile sync writes the IdP's name back.
* **False negative (SSO):** an account whose username was renamed after JIT
  keeps the old derived name, because it no longer equals the username.

Dialects
--------

The rows are matched in Python, not SQL: Python's ``str.title()`` has no SQL
spelling on either dialect, and the table is small. ``users`` is not enrolled in
row-level security (the login path reads it before any tenant is bound; see the
baseline's ``_TENANT_TABLES``), so the migrating role sees every row on
PostgreSQL with no ``row_security`` toggle. The same statements run on SQLite.

Offline (``--sql``) there are no rows to read, so the revision emits only a SQL
comment saying it was skipped; run it online to relabel.

``downgrade()``
---------------

A no-op. The derived names are not stored anywhere to restore from, and
restoring them would re-publish the address-derived text this revision removes.

Revision ID: b5334d07ae6c
Revises: 9b3e5c71d2a4
Create Date: 2026-10-12 12:00:00
"""

import logging
import re
from typing import Optional, Sequence, Union

from sqlalchemy import text

from alembic import op

revision: str = "b5334d07ae6c"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "9b3e5c71d2a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_log = logging.getLogger("alembic.runtime.migration")

SELECT_ACCOUNTS = text(
    "SELECT user_id, username, display_name, sso_provider FROM users"
)
RELABEL = text("UPDATE users SET display_name = :name WHERE user_id = :user_id")

#: ``RedisUserStore``'s email test, verbatim from the parent revision's code.
_REDIS_EMAIL = re.compile(r"^[^@]+@[^@]+\.[^@]+$")


def database_store_form(username: str) -> str:
    """``DatabaseUserStore._generate_display_name`` as the parent revision's code
    had it. Frozen here because migrations are history."""
    display = username.replace("_", " ").replace(".", " ").title()
    return display if display else username


def redis_store_form(username: str) -> str:
    """``RedisUserStore._generate_display_name`` as the parent revision's code had
    it. Matched only to count, never to change (see the module docstring)."""
    source = username.split("@")[0] if _REDIS_EMAIL.match(username) else username
    spaced = source.replace(".", " ").replace("_", " ").replace("-", " ")
    return " ".join(word.capitalize() for word in spaced.split())


def neutral_label(user_id: str) -> str:
    """The ruling's label, as ``auth.domain.display_name.neutral_display_name``
    spells it today. Frozen here for the same reason."""
    return f"Member {user_id[:4]}"


def is_email_derived(
    username: Optional[str], display_name: Optional[str], sso_provider: Optional[str]
) -> bool:
    """Whether a row's display name is one the old writers derived from an
    email (rules 1 and 2 of the module docstring)."""
    if not username or display_name is None:
        return False
    if sso_provider is not None:
        return display_name == username
    return "@" in username and display_name == database_store_form(username)


def upgrade() -> None:
    """Relabel every email-derived display name ``Member <4 chars of user_id>``."""
    if op.get_context().as_sql:
        op.execute(
            "-- 016_no_email_derived_display_names: rows are matched in Python; "
            "run this revision online to relabel them"
        )
        return
    bind = op.get_bind()
    relabelled = 0
    redis_form_only = 0
    for user_id, username, display_name, sso_provider in bind.execute(
        SELECT_ACCOUNTS
    ).fetchall():
        if is_email_derived(username, display_name, sso_provider):
            bind.execute(RELABEL, {"name": neutral_label(user_id), "user_id": user_id})
            relabelled += 1
        elif (
            sso_provider is None
            and username
            and "@" in username
            and display_name == redis_store_form(username)
        ):
            redis_form_only += 1
    _log.info("016: relabelled %d email-derived display name(s)", relabelled)
    if redis_form_only:
        _log.info(
            "016: left %d display name(s) that match only the Redis store's "
            "local-part form; that store never wrote these rows",
            redis_form_only,
        )


def downgrade() -> None:
    """Nothing to restore: the derived names are not kept, and restoring them
    would re-publish address-derived text."""
