"""042_operator_audit_role_actions

Widen ``operator_access_audit``'s action CHECK to admit the two operator-role
events, so ``platform_admin`` grants and revocations can be recorded (fm#1050).

**Why this table and not ``user_audit_log``.** Granting ``platform_admin`` is
the highest-privilege operation the deployment offers, and it wrote no audit row
at all: after the fm#819 cutover the table held exactly one entry, from SSO JIT
provisioning, and none for the promotion. ``user_audit_log`` cannot be the home
for it — that table is RLS-tenanted (migration 018) and ``platform_admin`` is
deployment-scoped (ADR-012 D9), so there is no organization to stamp the row
with. Under ``TENANT_PROVIDER=multi`` the standalone default organization does
not exist, so such a write fails its foreign key; naming a real tenant instead
would bury a deployment-wide privilege change inside one customer's trail.

``operator_access_audit`` already has exactly the properties the record needs:
no tenant policy, append-only by trigger (migration 035, reinforced by 036), and
identifiers deliberately unreferenced so the evidence outlives the account it
describes.

**Why the CHECK has to move.** 035 pinned ``action`` to the two data-access
values so "a typo cannot silently create a third, unaudited category". That
reasoning still holds; the set of legitimate categories is simply larger than it
was. The constraint is replaced rather than dropped, so an unrecognised action
is still rejected at the schema layer.

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-08-14 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, Sequence[str], None] = "f4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "operator_access_audit"
_CONSTRAINT = "operator_access_audit_action_valid"

#: Kept verbatim from migration 035 — the SQLite table rebuild below drops the
#: triggers with the old table, so they have to be recreated exactly.
_APPEND_ONLY_MESSAGE = "operator_access_audit is append-only"
_ROW_TRIGGER_EVENTS = ("UPDATE", "DELETE")

_OLD = "action IN ('list', 'content_open')"
_NEW = "action IN ('list', 'content_open', 'role_granted', 'role_revoked')"


def _recreate_sqlite_row_triggers() -> None:
    """Restore 035's per-row append-only triggers after a table rebuild.

    ``batch_alter_table`` implements an ALTER on SQLite by building a new table,
    copying the rows, dropping the original and renaming. The triggers belong to
    the table that gets dropped, and batch mode does not reflect or reissue them
    — so without this the migration would quietly leave the audit trail
    editable, which is the one property it exists to have.
    """
    for event in _ROW_TRIGGER_EVENTS:
        op.execute(
            f"CREATE TRIGGER {_TABLE}_no_{event.lower()} "
            f"BEFORE {event} ON {_TABLE} "
            f"BEGIN SELECT RAISE(ABORT, '{_APPEND_ONLY_MESSAGE}'); END"
        )


def _replace_check(condition: str) -> None:
    """Swap the action CHECK for one spelling the given condition."""
    dialect = op.get_bind().dialect.name

    if dialect == "postgresql":
        op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
        op.create_check_constraint(_CONSTRAINT, _TABLE, condition)
        return

    if dialect == "sqlite":
        # The triggers are dropped along with the old table; SQLite also cannot
        # reflect a CHECK, so the replacement is declared explicitly rather than
        # left to reflection (which would silently rebuild the table without any
        # action constraint at all).
        for event in _ROW_TRIGGER_EVENTS:
            op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_no_{event.lower()}")
        with op.batch_alter_table(
            _TABLE,
            schema=None,
            table_args=(sa.CheckConstraint(condition, name=_CONSTRAINT),),
        ) as batch:
            batch.drop_constraint(_CONSTRAINT, type_="check")
        _recreate_sqlite_row_triggers()
        return

    # Any other dialect: no CHECK was created for it either.
    return


def upgrade() -> None:
    """Admit ``role_granted`` / ``role_revoked``."""
    _replace_check(_NEW)


def downgrade() -> None:
    """Narrow back to the two data-access actions.

    Rows carrying the role actions would violate the restored constraint, so
    they are removed first. That is a deliberate exception to the append-only
    posture and the only place it is taken: a downgrade is a schema rollback,
    and leaving rows the schema forbids would strand the migration half-applied.
    The append-only triggers reject DELETE, so they are dropped for the
    statement and restored immediately after — on SQLite the rebuild in
    :func:`_replace_check` restores them, on PostgreSQL this does it directly.
    """
    dialect = op.get_bind().dialect.name
    delete_sql = (
        f"DELETE FROM {_TABLE} WHERE action IN ('role_granted', 'role_revoked')"
    )

    if dialect == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_no_delete ON {_TABLE}")
        op.execute(delete_sql)
        op.execute(
            f"CREATE TRIGGER {_TABLE}_no_delete BEFORE DELETE ON {_TABLE} "
            f"FOR EACH ROW EXECUTE FUNCTION {_TABLE}_append_only()"
        )
    elif dialect == "sqlite":
        op.execute(f"DROP TRIGGER IF EXISTS {_TABLE}_no_delete")
        op.execute(delete_sql)
        # _replace_check recreates both row triggers as part of the rebuild.

    _replace_check(_OLD)
