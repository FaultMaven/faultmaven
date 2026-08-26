"""044_last_admin_constraint_trigger

Make "an organization never loses its last admin" a property of
``organization_members`` instead of something every writer has to remember
(fm#1161).

**The gap.** The Cloud org-management service refuses to demote or remove an
organization's last admin, but it reads the admin count and writes in *separate
transactions*: the count goes through the sessionless repository, which opens
its own session per call. With exactly two admins and two concurrent requests —
one demoting each — both read ``count == 2``, both pass the check, and the
organization ends with **zero** admins and nobody who can grant a role or invite
a member. Two removals, or one of each, do the same. Nothing backstopped it: no
CHECK, no trigger, no unique index, no lock, no serializable isolation.

**Why a constraint trigger.** The invariant spans rows, so a CHECK cannot state
it, and no application check can guarantee it — a check that cannot see a
concurrent writer is not a control. A trigger on the table also covers the
writers that never reach the service: the ``fm-remove-org-member`` operator
command and any raw SQL a runbook reaches for. This is how #874 and #1042 were
fixed — put the invariant in the substrate rather than in each caller's memory.

**Why DEFERRABLE INITIALLY DEFERRED.** The guard has to run once the transaction
has finished moving roles around, not after each row. Demoting one admin while
promoting another is legitimate and a per-statement trigger would reject it on
whichever row it saw first. Deferring to commit also means the cascade cases
below can be recognised: the parent row is already gone by then.

**How the race is closed.** Counting is not enough on its own — two commits can
count concurrently and both see one admin left. So before counting, the guard
issues a no-op self-update of the organization's own row::

    UPDATE organizations SET updated_at = updated_at WHERE organization_id = ...

That single statement does three things. It proves the organization still exists
(``ROW_COUNT``). It serialises every guard evaluation for that organization on
one row, so the second committer waits for the first and then counts a roster
that includes the first one's change. And because it is a real row version and
not merely a lock, a transaction running at REPEATABLE READ — where the count
would otherwise be taken from a snapshot predating the other committer — gets
``could not serialize access due to concurrent update`` instead of a stale pass.
It fails closed at every isolation level the deployment could be set to.

It is a *self*-update: the column is written back the value read in the same
statement, so it cannot revert a concurrent write to that row the way a
full-row write would. Guard evaluations cannot deadlock against each other —
they contend for one row, always the same one, always taken at commit after the
member rows the transaction already holds. (A transaction that wrote the
``organizations`` row *and* a membership row would order the two locks the other
way round and could deadlock with a plain demotion; PostgreSQL would abort one
of them. No path does that today — every repository method commits its own
write — and an aborted transaction is the safe direction anyway.)

**Two deliberate exemptions**, both cascades, both recognisable only because the
trigger is deferred:

* the organization row is gone — the whole roster is going with it, so there is
  nothing left to keep manageable;
* the *user* row is gone — ``organization_members`` cascades from ``users``, and
  refusing that would turn deleting an account into a failure with no path
  through it. Account deletion is a deliberate, far more destructive act than a
  demotion, and it is not the concurrency hazard this migration exists to close.

**Only fires when an admin is actually lost.** ``OLD.role_id`` is checked first:
if the row being changed was not an admin, the event cannot have reduced the
admin count and the guard returns immediately, before taking any lock.

**SECURITY DEFINER.** ``organization_members`` and ``organizations`` are
RLS-tenanted (migration 018) with a fail-closed policy — with no
``app.current_org_id`` bound, ``current_setting`` is NULL and every row is
filtered out. A guard that counted through RLS would see zero admins whenever
the GUC was unset and reject legitimate writes. Running as the function's owner
(the migration role, which owns the tables and is therefore exempt) makes the
count true regardless of what the session has bound. ``search_path`` is pinned,
as it must be for any SECURITY DEFINER function.

**INSERT is not covered**, on purpose. This guard is "you cannot demote or
remove the last admin", which is the invariant the service states and the one
#1161 is about. Firing on INSERT would additionally mean "an organization with
any member must have an admin", which would reject adding the first member to a
fresh organization before its admin row lands and is a larger claim than
anything here needs.

**PostgreSQL only.** SQLite (Standalone) is single-tenant and has no
organizations to orphan — the same line migration 018 draws for RLS. Cloud is
the only deployment with organizations.

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-08-26 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FUNCTION = "organization_members_last_admin_guard"
_TRIGGER = "organization_members_last_admin"

#: The ``admin`` role's stable id. A frozen snapshot of
#: ``faultmaven.models.rbac_seed.SYSTEM_ROLE_IDS[Role.ADMIN]``, mirroring how
#: migration 029 seeded it — a migration states the value it was written
#: against rather than importing runtime code that may move underneath it.
#: ``tests/unit/infrastructure/persistence/test_last_admin_guard_role_id.py``
#: asserts the two agree, so the snapshot cannot drift silently.
_ADMIN_ROLE_ID = "50551907-a02c-5bf7-9aa4-4a98f3c4eb64"

#: Raised as ``check_violation`` (SQLSTATE 23514): this is an integrity
#: constraint the table now carries, and callers that already handle constraint
#: violations handle it without learning a new error class.
_ERRCODE = "23514"

_CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {_FUNCTION}()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_org_id      text;
    v_org_rows    integer;
    v_admin_count integer;
BEGIN
    -- An UPDATE that moved neither the role nor the organization cannot have
    -- cost anyone their admin. Checked before anything else so routine column
    -- writes never reach the serialisation point.
    IF TG_OP = 'UPDATE'
       AND NEW.role_id = OLD.role_id
       AND NEW.organization_id = OLD.organization_id THEN
        RETURN NULL;
    END IF;

    -- The row being demoted or removed was not an admin, so this event cannot
    -- have reduced the admin count. Nothing to check, and nothing to lock.
    IF OLD.role_id IS DISTINCT FROM '{_ADMIN_ROLE_ID}' THEN
        RETURN NULL;
    END IF;

    v_org_id := OLD.organization_id;

    -- Cascade from `users`: the account itself is being deleted and the
    -- membership is going with it. See the migration docstring.
    IF NOT EXISTS (SELECT 1 FROM users WHERE user_id = OLD.user_id) THEN
        RETURN NULL;
    END IF;

    -- Existence check AND serialisation point in one statement. See the
    -- migration docstring for why this is a no-op self-update rather than a
    -- SELECT ... FOR UPDATE.
    UPDATE organizations
       SET updated_at = updated_at
     WHERE organization_id = v_org_id;
    GET DIAGNOSTICS v_org_rows = ROW_COUNT;

    -- Cascade from `organizations`: the organization is being deleted.
    IF v_org_rows = 0 THEN
        RETURN NULL;
    END IF;

    -- Taken after the serialisation point, so it sees every guard evaluation
    -- for this organization that has already committed.
    SELECT count(*) INTO v_admin_count
      FROM organization_members
     WHERE organization_id = v_org_id
       AND role_id = '{_ADMIN_ROLE_ID}';

    IF v_admin_count = 0 THEN
        RAISE EXCEPTION
            'organization % would be left with no admin', v_org_id
            USING ERRCODE = '{_ERRCODE}',
                  CONSTRAINT = '{_TRIGGER}',
                  HINT = 'Grant another member the admin role first.';
    END IF;

    RETURN NULL;
END;
$$;
"""

_CREATE_TRIGGER = f"""
CREATE CONSTRAINT TRIGGER {_TRIGGER}
AFTER UPDATE OR DELETE ON organization_members
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()
"""


def upgrade() -> None:
    """Install the last-admin constraint trigger (PostgreSQL only)."""
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite (Standalone) has no organizations to orphan.
    op.execute(_CREATE_FUNCTION)
    op.execute(_CREATE_TRIGGER)


def downgrade() -> None:
    """Drop the trigger and its function (PostgreSQL only)."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON organization_members")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
