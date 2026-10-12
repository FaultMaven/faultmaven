r"""016_no_email_derived_display_names

No display name derived from an email address (#1924). A display name is shown
to people other than its owner: a shared case's creator and driver, the
driver-candidate list, a verified runbook's ``verified_by``. Owner ruling
(2026-10-12): an account with no real name gets ``Member`` plus the first four
characters of its user id (``Member 3f9a``); a data migration replaces the
email-derived names already stored; a name the IdP sends later overwrites it.

Which rows change
-----------------

No column records that a name was derived, so the rows are matched on what the
name holds, with the rule ``auth.domain.display_name.usable_display_name``
applies to every new write:

1. **SSO accounts** (``sso_provider IS NOT NULL``) whose ``display_name`` equals
   their ``username``. JIT provisioning stored the username when the IdP sent no
   name, and an SSO username is the email's sanitized local part.
2. **Any account whose display name contains ``@``.** This covers the local
   accounts registered with an address as the username and no display name
   (the database store stored a title-cased address, ``Alice Smith@Example
   Com``), an address the IdP sent as the ``name`` (``pat@example.com``,
   ``Quinn <quinn@example.com>``), and an address typed as the name.
3. **Any account whose display name equals its REAL email's local part**,
   ignoring case and surrounding spaces: an IdP ``name`` of ``Erin`` for
   ``erin@…``, or the database store's title-cased username ``Alice`` for
   username ``alice`` with ``alice@acme.com``. An email the store synthesized
   for an account registered without one (``<username>@faultmaven.example``,
   ``infrastructure/auth/synthesized_email.py``) is not real: it was derived
   from the username, so ``Bob`` with ``bob@faultmaven.example`` is kept. This
   is the predicate the writers apply (``usable_display_name`` judged against
   the real email only), frozen here as SQL.

Each becomes ``Member`` and the first four characters of its ``user_id``.

Rows left untouched: an SSO name that differs from the username, holds no
``@`` and is not the real email's local part (``Jane Doe``); a local name with
no ``@`` that is not the real email's local part, including the title-cased
form of a plain username (``Carol X`` for ``carol_x``), any name of an account
whose email is synthesized (``Bob``), and a name that renders the local part
with different separators (``Sam Smith`` for ``sam.smith@…``, a name a person
types: separators are not normalised); and the Redis store's local-part form
(below).

The Redis user store's old form (title-casing the local part: ``Alice Smith``
for ``alice.smith@example.com``) is NOT matched: that store never writes these
rows, and a typed name equal to it would be relabelled with no way back. Online,
rows that hold that form are counted and logged, never changed.

Known misclassifications
------------------------

* **False positive (SSO):** an IdP name that equals the derived username (the
  IdP sends ``name = "alice"`` for ``alice@…``) is relabelled. Profile sync
  now refuses that name too, so the label stays until the IdP sends a real one.
* **False negative (SSO):** an account whose username was renamed after JIT
  keeps the old derived name if it equals neither the new username nor the
  email's local part.

Dialects
--------

One ``UPDATE``, the same text on both dialects: ``||``, ``substr``, ``lower``,
``trim``, ``replace`` and ``LIKE ... ESCAPE`` mean the same thing in SQLite and
PostgreSQL. Rule 3 builds a ``LIKE`` pattern from the display name, so ``\``,
``%`` and ``_`` in the name are escaped first: a name ``sam_x`` must not match
``samAx@…``. SQLite's ``lower`` folds ASCII only; a non-ASCII local part that
differs from its name only in case is not matched there. SQL ``trim()`` strips
spaces only, where the writers' ``strip()`` strips all whitespace, so a legacy
name stored with a surrounding tab or newline is not matched by rule 3 (new
writes are stripped before they are stored).

``users`` is not enrolled in row-level security (the login path reads it before
any tenant is bound; see the baseline's ``_TENANT_TABLES``), so the migrating
role sees every row on PostgreSQL with no ``row_security`` toggle.

Offline (``--sql``) emits that same ``UPDATE``, so a script applied later
relabels exactly what an online run would. Only the two log lines (the relabel
count and the Redis-form count) need a connection.

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
from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

revision: str = "b5334d07ae6c"  # pragma: allowlist secret
down_revision: Union[str, Sequence[str], None] = "9b3e5c71d2a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_log = logging.getLogger("alembic.runtime.migration")

#: ``lower(trim(display_name))`` as a LIKE pattern matching itself literally.
_LITERAL_NAME = (
    "replace(replace(replace(lower(trim(display_name)), "
    "'\\', '\\\\'), '%', '\\%'), '_', '\\_')"
)

#: ``infrastructure/auth/synthesized_email.is_synthesized_email``, frozen.
_REAL_EMAIL = "lower(email) NOT LIKE '%@faultmaven.example'"

#: Rules 1-3 of the module docstring.
EMAIL_DERIVED = (
    "(sso_provider IS NOT NULL AND display_name = username) "
    "OR display_name LIKE '%@%' "
    f"OR ({_REAL_EMAIL} "
    f"AND lower(email) LIKE {_LITERAL_NAME} || '@%' ESCAPE '\\')"
)

RELABEL = (
    "UPDATE users SET display_name = 'Member ' || substr(user_id, 1, 4) "
    f"WHERE {EMAIL_DERIVED}"
)

#: Local accounts with an address username, for the Redis-form count.
SELECT_LOCAL_ADDRESS_ACCOUNTS = text(
    "SELECT username, display_name FROM users "
    "WHERE sso_provider IS NULL AND username LIKE '%@%'"
)

#: ``RedisUserStore``'s email test, verbatim from the parent revision's code.
_REDIS_EMAIL = re.compile(r"^[^@]+@[^@]+\.[^@]+$")


def redis_store_form(username: str) -> str:
    """``RedisUserStore._generate_display_name`` as the parent revision's code had
    it. Matched only to count, never to change (see the module docstring).
    Frozen here because migrations are history."""
    source = username.split("@")[0] if _REDIS_EMAIL.match(username) else username
    spaced = source.replace(".", " ").replace("_", " ").replace("-", " ")
    return " ".join(word.capitalize() for word in spaced.split())


def upgrade() -> None:
    """Relabel every email-derived display name ``Member <4 chars of user_id>``."""
    if op.get_context().as_sql:
        op.execute(RELABEL)
        return
    bind = op.get_bind()
    relabelled = bind.execute(text(RELABEL)).rowcount
    _log.info("016: relabelled %d email-derived display name(s)", relabelled)
    redis_form_only = sum(
        1
        for username, display_name in bind.execute(SELECT_LOCAL_ADDRESS_ACCOUNTS)
        if display_name == redis_store_form(username)
    )
    if redis_form_only:
        _log.info(
            "016: left %d display name(s) that match only the Redis store's "
            "local-part form; that store never wrote these rows",
            redis_form_only,
        )


def downgrade() -> None:
    """Nothing to restore: the derived names are not kept, and restoring them
    would re-publish address-derived text."""
