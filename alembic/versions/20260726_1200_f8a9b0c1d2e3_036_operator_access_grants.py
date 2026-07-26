"""036_operator_access_grants

Break-glass grants for operator access to Cloud tenant case **content**
(ADR-012 D8/D9, faultmaven#815). See
``docs/architecture/security/break-glass-content-access.md``.

A grant is one operator's time-boxed license to read **one case's** content. It
carries the justification (``reason``), the window (``expires_at``) and the
target (``target_case_id`` + ``target_organization_id``, the latter also naming
the organization the request rebinds its RLS scope to).

**Not append-only, but every widening mutation is refused.** Revocation and
approval are real UPDATEs, so a blanket no-UPDATE trigger would be wrong. Four
narrower guards do the job instead. The rule they collectively enforce is that
**access can only ever be narrowed in place**; widening it requires a new row,
with a fresh justification and a fresh audit trail.

- Every column except the two approval/revocation state pairs is **pinned
  outright** — who, which case, which tenant, why, when created, when it
  expires, and the two denormalised descriptors. An operator can end their own
  access early; they cannot rewrite why they took it or extend how long they
  were allowed to.
- Revocation is **monotonic**: once ``revoked_at`` is set it cannot be cleared
  or moved. Left freely mutable, the one update the database permits would be
  the one that brings a revoked grant back to life for the rest of its TTL.
- Denial is **final**: ``denied → approved`` is refused. ``approval_state``
  cannot simply be pinned — ``pending → approved`` is the legitimate widening
  the approval seam exists to perform — but a customer who declined an access
  request must not have that decision reversed in place.
- DELETE and TRUNCATE are both rejected. TRUNCATE needs its own statement
  trigger because row triggers do not fire on it, which would otherwise let the
  whole table — the evidence of why every recorded access was authorised — be
  erased trigger-free.

**Also completes two carry-overs on ``operator_access_audit`` (migration 035):**

- ``grant_id`` gets its index. 035 deliberately left it unindexed because the
  column was 100% NULL until break-glass existed; this migration gives it its
  first reader ("show me every access taken under grant X").
- A ``BEFORE TRUNCATE ... FOR EACH STATEMENT`` trigger. 035's row triggers do not
  fire on TRUNCATE, so the append-only claim held only "given the current
  GRANTs". PostgreSQL only — SQLite has no TRUNCATE statement.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-07-26 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, Sequence[str], None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_AUDIT_APPEND_ONLY_MESSAGE = "operator_access_audit is append-only"
_GRANT_IMMUTABLE_MESSAGE = (
    "operator_access_grants justification columns are immutable; "
    "create a new grant instead"
)
_GRANT_NO_DELETE_MESSAGE = "operator_access_grants rows cannot be deleted"
_GRANT_NO_TRUNCATE_MESSAGE = "operator_access_grants cannot be truncated"
_GRANT_REVOCATION_MONOTONIC_MESSAGE = (
    "operator_access_grants revocation is monotonic; a revoked grant "
    "cannot be un-revoked"
)
_GRANT_DENIAL_FINAL_MESSAGE = (
    "operator_access_grants denial is final; a denied grant cannot be approved"
)

# The columns a revoke/approve must never touch. Changing any of them would
# rewrite the justification an audit row was taken under. ``operator_username``
# and ``deployment_mode`` are here because they are set once at creation and
# never legitimately change — pinning them costs nothing and keeps "revocation
# and approval are the only permitted mutations" literally true rather than
# true-of-the-fields-that-matter.
_IMMUTABLE_COLUMNS = (
    "grant_id",
    "operator_user_id",
    "operator_username",
    "target_case_id",
    "target_organization_id",
    "reason",
    "created_at",
    "expires_at",
    "deployment_mode",
)

# Revocation columns are mutable exactly once, NULL → set. They cannot be in
# ``_IMMUTABLE_COLUMNS`` (revoking would then be impossible), but leaving them
# freely mutable makes the ONE update that *widens* access — clearing
# ``revoked_at`` to bring a grant back to life for the rest of its TTL — the one
# the database permits. The predicate below therefore pins them from the moment
# they are set, so revocation only ever moves in the safe direction.
_REVOCATION_COLUMNS = ("revoked_at", "revoked_by")

# A denial is terminal, for the same reason a revocation is. ``pending →
# approved`` is the legitimate widening the approval seam exists to perform, so
# ``approval_state`` cannot simply be pinned — but ``denied → approved`` is a
# widening the database must refuse: a customer who declined an access request
# must not have that decision reversed in place. Re-asking means a new grant
# with a fresh reason, which is the same rule the TTL follows.
#
# Enforced now rather than left to the approval workstream: shipping the state
# machine early is only worth it if adding customer approval stays a
# *transition* rather than a schema change, and a guard bolted on later would be
# exactly the schema change that claim promises to avoid.
_DENIED_STATE = "denied"


def upgrade() -> None:
    """Create the grants table, its triggers, and the 035 carry-overs."""
    op.create_table(
        "operator_access_grants",
        # Application-generated uuid4 rather than a sequence: it is quoted back
        # in audit rows and in API responses, and a guessable identifier there
        # invites probing for other operators' grants.
        sa.Column("grant_id", sa.String(length=36), nullable=False),
        # Deliberately NOT a foreign key to users.user_id, for the same reason
        # as operator_access_audit: this row is evidence of why an access was
        # taken and must outlive the account, and every ondelete action would be
        # executed as a write the triggers below reject.
        sa.Column("operator_user_id", sa.String(length=36), nullable=False),
        sa.Column("operator_username", sa.String(length=255), nullable=True),
        # One case, never a whole tenant (D9). The organization is carried
        # alongside because it is what the request rebinds its RLS scope to
        # under TENANT_PROVIDER=multi — the case's own row is unreadable until
        # after that rebind, so the org cannot be looked up from it.
        sa.Column("target_case_id", sa.String(length=36), nullable=False),
        sa.Column("target_organization_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=36), nullable=True),
        # The seam for customer-initiated approval (D9's stated ideal). Today an
        # operator's own grant is created 'auto_approved'; moving to a
        # customer-approved posture is a transition in this state machine plus a
        # tenant-admin surface to drive it, not a reshaping of the read path.
        sa.Column(
            "approval_state",
            sa.String(length=32),
            nullable=False,
            server_default="auto_approved",
        ),
        sa.Column("approved_by", sa.String(length=36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deployment_mode", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("grant_id"),
        # Pin the approval vocabulary at the schema layer so a typo cannot
        # create a state the liveness predicate silently treats as not-live
        # (or, worse, a new state nobody accounts for).
        sa.CheckConstraint(
            "approval_state IN ('auto_approved', 'pending', 'approved', 'denied')",
            name="operator_access_grants_approval_state_valid",
        ),
        # A grant that expires no later than it was created is not a window.
        sa.CheckConstraint(
            "expires_at > created_at",
            name="operator_access_grants_window_valid",
        ),
    )
    # The liveness lookup the content path runs on every read: "this operator's
    # live grants for this case". Ordered operator-first because the operator is
    # always known and always filtered; the case id is the selective tail.
    op.create_index(
        "ix_operator_access_grants_operator_case",
        "operator_access_grants",
        ["operator_user_id", "target_case_id", "expires_at"],
    )
    # The review query: "who holds access to this tenant, and until when".
    op.create_index(
        "ix_operator_access_grants_target_org",
        "operator_access_grants",
        ["target_organization_id", "created_at"],
    )

    # 035 carry-over: grant_id now has a reader ("every access under grant X").
    op.create_index(
        "ix_operator_access_audit_grant",
        "operator_access_audit",
        ["grant_id"],
    )

    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        # 035 carry-over: row triggers do not fire on TRUNCATE, so without this
        # a role holding the privilege could erase the whole trail trigger-free.
        op.execute(f"""
            CREATE OR REPLACE FUNCTION operator_access_audit_no_truncate_fn()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION '{_AUDIT_APPEND_ONLY_MESSAGE}';
            END;
            $$ LANGUAGE plpgsql;
            """)
        op.execute(
            "CREATE TRIGGER operator_access_audit_no_truncate "
            "BEFORE TRUNCATE ON operator_access_audit "
            "FOR EACH STATEMENT EXECUTE FUNCTION "
            "operator_access_audit_no_truncate_fn()"
        )

        immutable_predicate = " OR ".join(
            f"OLD.{col} IS DISTINCT FROM NEW.{col}" for col in _IMMUTABLE_COLUMNS
        )
        op.execute(f"""
            CREATE OR REPLACE FUNCTION operator_access_grants_immutable()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION '{_GRANT_IMMUTABLE_MESSAGE}';
            END;
            $$ LANGUAGE plpgsql;
            """)
        op.execute(f"""
            CREATE TRIGGER operator_access_grants_no_rewrite
            BEFORE UPDATE ON operator_access_grants
            FOR EACH ROW WHEN ({immutable_predicate})
            EXECUTE FUNCTION operator_access_grants_immutable()
            """)
        op.execute(f"""
            CREATE OR REPLACE FUNCTION operator_access_grants_no_delete_fn()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION '{_GRANT_NO_DELETE_MESSAGE}';
            END;
            $$ LANGUAGE plpgsql;
            """)
        op.execute(
            "CREATE TRIGGER operator_access_grants_no_delete "
            "BEFORE DELETE ON operator_access_grants "
            "FOR EACH ROW EXECUTE FUNCTION operator_access_grants_no_delete_fn()"
        )

        # Revocation is monotonic. Once ``revoked_at`` is set it cannot be
        # cleared or moved — otherwise the single update the database permits is
        # the one that brings a revoked grant back to life for the remainder of
        # its TTL, which is the only mutation here that *widens* access.
        revocation_predicate = " OR ".join(
            f"(OLD.{col} IS NOT NULL AND OLD.{col} IS DISTINCT FROM NEW.{col})"
            for col in _REVOCATION_COLUMNS
        )
        op.execute(f"""
            CREATE OR REPLACE FUNCTION operator_access_grants_revocation_final()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION '{_GRANT_REVOCATION_MONOTONIC_MESSAGE}';
            END;
            $$ LANGUAGE plpgsql;
            """)
        op.execute(f"""
            CREATE TRIGGER operator_access_grants_no_unrevoke
            BEFORE UPDATE ON operator_access_grants
            FOR EACH ROW WHEN ({revocation_predicate})
            EXECUTE FUNCTION operator_access_grants_revocation_final()
            """)

        # A denial is terminal too — see _DENIED_STATE.
        op.execute(f"""
            CREATE OR REPLACE FUNCTION operator_access_grants_denial_final()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION '{_GRANT_DENIAL_FINAL_MESSAGE}';
            END;
            $$ LANGUAGE plpgsql;
            """)
        op.execute(f"""
            CREATE TRIGGER operator_access_grants_no_undeny
            BEFORE UPDATE ON operator_access_grants
            FOR EACH ROW WHEN (
                OLD.approval_state = '{_DENIED_STATE}'
                AND NEW.approval_state IS DISTINCT FROM OLD.approval_state
            )
            EXECUTE FUNCTION operator_access_grants_denial_final()
            """)

        # Row triggers do not fire on TRUNCATE, so without this the whole grant
        # table — the evidence of why every recorded access was authorised —
        # could be erased trigger-free. Same hole this migration closes for
        # ``operator_access_audit``.
        op.execute(f"""
            CREATE OR REPLACE FUNCTION operator_access_grants_no_truncate_fn()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION '{_GRANT_NO_TRUNCATE_MESSAGE}';
            END;
            $$ LANGUAGE plpgsql;
            """)
        op.execute(
            "CREATE TRIGGER operator_access_grants_no_truncate "
            "BEFORE TRUNCATE ON operator_access_grants "
            "FOR EACH STATEMENT EXECUTE FUNCTION "
            "operator_access_grants_no_truncate_fn()"
        )
    elif dialect == "sqlite":
        # SQLite has no IS DISTINCT FROM; `IS NOT` is its null-safe inequality.
        # It also has no TRUNCATE statement, so no truncate guard is needed.
        immutable_predicate = " OR ".join(
            f"OLD.{col} IS NOT NEW.{col}" for col in _IMMUTABLE_COLUMNS
        )
        op.execute(f"""
            CREATE TRIGGER operator_access_grants_no_rewrite
            BEFORE UPDATE ON operator_access_grants
            FOR EACH ROW WHEN ({immutable_predicate})
            BEGIN SELECT RAISE(ABORT, '{_GRANT_IMMUTABLE_MESSAGE}'); END
            """)
        op.execute(f"""
            CREATE TRIGGER operator_access_grants_no_delete
            BEFORE DELETE ON operator_access_grants
            BEGIN SELECT RAISE(ABORT, '{_GRANT_NO_DELETE_MESSAGE}'); END
            """)
        revocation_predicate = " OR ".join(
            f"(OLD.{col} IS NOT NULL AND OLD.{col} IS NOT NEW.{col})"
            for col in _REVOCATION_COLUMNS
        )
        op.execute(f"""
            CREATE TRIGGER operator_access_grants_no_unrevoke
            BEFORE UPDATE ON operator_access_grants
            FOR EACH ROW WHEN ({revocation_predicate})
            BEGIN SELECT RAISE(ABORT, '{_GRANT_REVOCATION_MONOTONIC_MESSAGE}'); END
            """)
        op.execute(f"""
            CREATE TRIGGER operator_access_grants_no_undeny
            BEFORE UPDATE ON operator_access_grants
            FOR EACH ROW WHEN (
                OLD.approval_state = '{_DENIED_STATE}'
                AND NEW.approval_state IS NOT OLD.approval_state
            )
            BEGIN SELECT RAISE(ABORT, '{_GRANT_DENIAL_FINAL_MESSAGE}'); END
            """)


def downgrade() -> None:
    """Drop the grants table and undo the 035 carry-overs."""
    op.drop_index("ix_operator_access_audit_grant", table_name="operator_access_audit")
    # Dropping the table takes its own triggers with it on both engines; the
    # standalone plpgsql functions and the trigger on the *audit* table (which
    # survives this downgrade) need explicit teardown.
    op.drop_table("operator_access_grants")
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS operator_access_audit_no_truncate "
            "ON operator_access_audit"
        )
        op.execute("DROP FUNCTION IF EXISTS operator_access_audit_no_truncate_fn()")
        op.execute("DROP FUNCTION IF EXISTS operator_access_grants_immutable()")
        op.execute("DROP FUNCTION IF EXISTS operator_access_grants_no_delete_fn()")
        op.execute("DROP FUNCTION IF EXISTS operator_access_grants_revocation_final()")
        op.execute("DROP FUNCTION IF EXISTS operator_access_grants_denial_final()")
        op.execute("DROP FUNCTION IF EXISTS operator_access_grants_no_truncate_fn()")
