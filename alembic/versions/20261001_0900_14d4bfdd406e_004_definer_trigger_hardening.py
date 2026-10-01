"""004_definer_trigger_hardening

The baseline's two ``SECURITY DEFINER`` trigger functions get the settings
revision 003 gave its definer functions:

* ``search_path = pg_catalog, public, pg_temp`` — ``pg_temp`` LAST. When it is
  not listed, PostgreSQL searches the session's temporary schema FIRST for
  relations, so a caller could shadow a table the body names unqualified with a
  temporary one and have the owner-privileged body read it.
  ``team_members_same_enterprise_guard`` reads ``teams`` and ``users`` to decide
  whether a membership crosses enterprises: a session that may create temporary
  tables could have answered that question for it.
* ``row_security = off`` — a body that relies on its owner being exempt from
  row-level security RAISES instead of reading a filtered set if that exemption
  is ever lost (a ``FORCE``d policy, a non-owner migrating role). Filtered, the
  membership guard finds no team (``users`` carries no policy; ``teams`` does)
  and admits the row, and the last-admin guard finds no organization and admits
  the change; raised, the write fails and says why.

The functions are altered in place rather than re-created: their bodies, owners,
grants and triggers are untouched. The baseline is not amended, because a
database stamped at a later revision would never receive the change.

SQLite has no definer functions (its triggers are plain DDL), so the revision is
a no-op there in both directions.

Revision ID: 14d4bfdd406e
Revises: baa28e79ebab
Create Date: 2026-10-01 09:00:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "14d4bfdd406e"
down_revision: Union[str, Sequence[str], None] = "baa28e79ebab"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The baseline's definer trigger functions, by signature.
TRIGGER_FUNCTIONS = (
    "organization_members_last_admin_guard()",
    "team_members_same_enterprise_guard()",
)

SEARCH_PATH = "pg_catalog, public, pg_temp"

#: What the baseline set, restored on downgrade.
_BASELINE_SEARCH_PATH = "pg_catalog, public"


def upgrade() -> None:
    """Pin ``pg_temp`` last and ``row_security`` off on both trigger functions."""
    if op.get_context().dialect.name != "postgresql":
        return
    for signature in TRIGGER_FUNCTIONS:
        op.execute(f"ALTER FUNCTION {signature} SET search_path = {SEARCH_PATH}")
        op.execute(f"ALTER FUNCTION {signature} SET row_security = off")


def downgrade() -> None:
    """Restore the baseline's settings."""
    if op.get_context().dialect.name != "postgresql":
        return
    for signature in TRIGGER_FUNCTIONS:
        op.execute(
            f"ALTER FUNCTION {signature} SET search_path = {_BASELINE_SEARCH_PATH}"
        )
        op.execute(f"ALTER FUNCTION {signature} RESET row_security")
