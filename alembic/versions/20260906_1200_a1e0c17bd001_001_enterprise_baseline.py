"""001_enterprise_baseline

The whole schema, in one migration: the enterprise is the isolation boundary
(ADR-017).

This replaces the 001–053 chain outright. Moving the isolation key one tier up
touches every tenant-scoped table, every RLS policy and the two lookup tables the
SSO callback reads, so a stacked migration would have been a re-statement of the
chain with a dual-key period in the middle — and the owner's rule for this
campaign is that the system is pre-user: **no backward compatibility, no data
preservation**. Existing deployments are wiped (``fm-wipe-deployment --wipe``)
and re-provisioned on this baseline. There is therefore nothing to migrate
*from*, and a single ``CREATE`` is the honest shape.

What the model says, and what that means here
---------------------------------------------

* **Isolation = enterprise.** Every tenant-scoped table carries
  ``enterprise_id VARCHAR(36) NOT NULL`` with a foreign key to ``enterprises``,
  and every policy keys on ``current_setting('app.current_enterprise_id', true)``.
  ``team_members`` is the one exception: it is a pure ``(user_id, team_id)`` join,
  so its policy reaches the key by one hop through ``teams.enterprise_id``.
* **Billing = organization.** ``organization_id`` survives on data rows as
  *nullable attribution*, stamped from the actor's organization at write time and
  never read for visibility. Its foreign key is ``ON DELETE SET NULL``, not
  CASCADE: losing a cost centre must not destroy the data it paid for.
* **Sharing = team.** ``teams`` is parented by its enterprise and carries no
  ``organization_id``, so one team may span two cost centres of one company.
  ``team_invitations`` records the consent that forms it.

Fail-closed, unchanged from migration 018: with no ``app.current_enterprise_id``
bound, ``current_setting(...)`` is NULL, every comparison is NULL, and no row
matches. The binder sets it once per transaction.

⚠️ INFRA REQUIREMENT, unchanged: RLS only takes effect for a connection that is
NEITHER a superuser NOR the table owner — PostgreSQL exempts both. The
application's production role must be the limited ``faultmaven_app`` role, or
these policies are silently bypassed. Migrations run as the owner and are
(intentionally) exempt.

Retired here, deleted rather than deprecated
--------------------------------------------

``sso_personal_orgs`` (→ ``sso_personal_enterprises``, keyed on the subject and
carrying the retirement state migration 052 introduced), ``organization_turn_usage``
(→ ``turn_usage``, keyed on a billing subject), ``teams.organization_id``,
``sso_org_mappings.organization_id`` (→ ``enterprise_id``),
``target_organization_id`` on both operator-access tables (→
``target_enterprise_id``), and ``users.account_kind = 'slack'`` (→ ``'service'``
with ``service_channel``).

Carried forward verbatim in behaviour
--------------------------------------

Every CHECK, index, partial unique index and trigger the chain built:
the append-only guards on ``operator_access_audit`` (035/036), the four
immutability guards on ``operator_access_grants`` (036), the deferred last-admin
constraint trigger on ``organization_members`` (044), the four per-command
``knowledge_items`` policies with the platform-tier read exemption (033) — with
their global-write arms now comparing the bound tenant against
``STANDALONE_ENTERPRISE_ID`` rather than the organization sentinel — and the
seeds: the default enterprise (006), the default team, and the RBAC roles,
permissions and grants (029).

One deliberate relaxation: ``knowledge_items_global_org_check`` kept its
"a global row is billed to no organization" half and dropped its converse. Under
ADR-017 D3/D5 an account may be in **no** organization, so "a personal or team
row is always org-stamped" became false; asserting it would make every beta row
unwritable.

SQLite (standalone) gets the tables, the CHECKs, the partial indexes and the
SQLite spellings of the append-only/immutability triggers. It gets no RLS (it is
single-tenant, and SQLite has none) and no last-admin trigger (it has no
organizations to orphan) — the same line every migration in the chain drew.

Revision ID: a1e0c17bd001
Revises:
Create Date: 2026-09-06 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1e0c17bd001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The cross-dialect shape of ``models._TagsArrayType``: ``VARCHAR(50)[]`` on
#: PostgreSQL, comma-separated ``TEXT`` on SQLite. Spelled out rather than
#: imported — a migration is history and must not depend on a live application
#: type that may be redefined underneath it.
_TAGS_ARRAY = sa.Text().with_variant(
    postgresql.ARRAY(sa.String(length=50)), "postgresql"
)

#: Frozen copies of ``faultmaven.config.constants``. Migrations are history; they
#: state the values they were written against rather than importing runtime code.
_STANDALONE_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"
_STANDALONE_ENTERPRISE_SLUG = "default"
_STANDALONE_ENTERPRISE_NAME = "Default Enterprise"
_STANDALONE_TEAM_ID = "00000000-0000-0000-0000-000000000003"
_STANDALONE_TEAM_NAME = "Default Team"

#: The session GUC every policy reads. Set once per transaction by the engine's
#: ``begin`` listener from the request's bound enterprise.
_TENANT_GUC = "app.current_enterprise_id"
_ENTERPRISE_MATCHES_SESSION = f"enterprise_id = current_setting('{_TENANT_GUC}', true)"

#: Every table that carries ``enterprise_id`` and takes the plain policy. No
#: ``FOR`` clause, which in PostgreSQL means ``FOR ALL`` with the USING
#: expression ALSO applied as the WITH CHECK on writes — so inserting or updating
#: a row into another enterprise is rejected, not merely hidden.
#:
#: ``knowledge_items`` and ``team_members`` are absent because they need their
#: own policy shapes (below). ``operator_access_audit`` and
#: ``operator_access_grants`` are absent because break-glass is a *cross*-tenant
#: mechanism (ADR-012 D9): a grant row scoped to the tenant it grants access to
#: could not be read by the operator who needs it. ``users``,
#: ``sso_org_mappings`` and ``sso_personal_enterprises`` are absent because they
#: are read on the *unauthenticated* login path, before any tenant is bound —
#: binding the tenant is what those lookups decide.
_TENANT_TABLES = (
    "case_actions",
    "case_checkpoints",
    "case_entities",
    "case_messages",
    "case_tags",
    "cases",
    "causal_edges",
    "causal_node_evidence",
    "causal_nodes",
    "conversion_drafts",
    "conversion_jobs",
    "evidence",
    "evidence_need_fulfillment",
    "evidence_needs",
    "hypotheses",
    "hypothesis_evidence",
    "investigation_sessions",
    "knowledge_suggestions",
    "organization_members",
    "organizations",
    "reports",
    "resource_shares",
    "solutions",
    "team_invitations",
    "teams",
    "turn_usage",
    "uploaded_files",
    "user_audit_log",
)

# --- knowledge_items: the platform tier (migration 033, re-keyed) ------------
#
# The ``global`` scope is the platform-seeded corpus, readable by every tenant.
# Putting that read exemption into a FOR ALL policy's USING would let any tenant
# UPDATE or DELETE platform rows, so reads and writes split into four
# per-command policies. The global-write arm is valid only when the session is
# bound to the STANDALONE enterprise — which standalone and cloud+single always
# are, and which no tenant session under multi ever is (fail-closed binder), so
# platform seeding runs there on the audited maintenance path instead.

#: Existing-row arm for UPDATE/DELETE USING.
_GLOBAL_ROW_SENTINEL_SESSION = (
    "(scope = 'global' "
    f"AND current_setting('{_TENANT_GUC}', true) = '{_STANDALONE_ENTERPRISE_ID}')"
)
#: New-row arm for INSERT/UPDATE WITH CHECK: additionally pins the incoming row
#: shape (billed to no organization) rather than relying on the CHECK alone.
_GLOBAL_NEW_ROW_SENTINEL_SESSION = (
    "(scope = 'global' AND organization_id IS NULL "
    f"AND current_setting('{_TENANT_GUC}', true) = '{_STANDALONE_ENTERPRISE_ID}')"
)

_KNOWLEDGE_POLICIES = (
    (
        "knowledge_items_tenant_read",
        "FOR SELECT USING " f"({_ENTERPRISE_MATCHES_SESSION} OR scope = 'global')",
    ),
    (
        "knowledge_items_tenant_insert",
        "FOR INSERT WITH CHECK "
        f"({_ENTERPRISE_MATCHES_SESSION} OR {_GLOBAL_NEW_ROW_SENTINEL_SESSION})",
    ),
    (
        "knowledge_items_tenant_update",
        "FOR UPDATE USING "
        f"({_ENTERPRISE_MATCHES_SESSION} OR {_GLOBAL_ROW_SENTINEL_SESSION}) "
        "WITH CHECK "
        f"({_ENTERPRISE_MATCHES_SESSION} OR {_GLOBAL_NEW_ROW_SENTINEL_SESSION})",
    ),
    (
        "knowledge_items_tenant_delete",
        "FOR DELETE USING "
        f"({_ENTERPRISE_MATCHES_SESSION} OR {_GLOBAL_ROW_SENTINEL_SESSION})",
    ),
)

#: ``team_members`` carries no ``enterprise_id`` of its own — it is a pure
#: ``(user_id, team_id)`` join — so its policy scopes via a subquery over
#: ``teams``. That subquery is itself RLS-scoped by the same predicate, so the
#: two policies agree by construction.
_TEAM_MEMBERS_POLICY = (
    "team_id IN (SELECT team_id FROM teams "
    f"WHERE teams.enterprise_id = current_setting('{_TENANT_GUC}', true))"
)


# ---------------------------------------------------------------------------
# Append-only and immutability guards (migrations 035 / 036), verbatim in
# behaviour. Enforced by the database, not by convention: the threat is the
# audited operator themselves, and an operator who can amend or erase their own
# access record leaves the table with no evidentiary value.
# ---------------------------------------------------------------------------

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

#: The columns a revoke/approve must never touch. Changing any of them would
#: rewrite the justification an audit row was taken under.
#: ``target_enterprise_id`` replaces ``target_organization_id`` here because the
#: tenant a break-glass read rebinds to is now the enterprise.
_IMMUTABLE_COLUMNS = (
    "grant_id",
    "operator_user_id",
    "operator_username",
    "target_case_id",
    "target_enterprise_id",
    "reason",
    "created_at",
    "expires_at",
    "deployment_mode",
)

#: Mutable exactly once, NULL -> set. They cannot be pinned outright (revoking
#: would be impossible), but leaving them freely mutable makes the ONE update
#: that *widens* access — clearing ``revoked_at`` to bring a revoked grant back
#: to life for the rest of its TTL — the one the database permits.
_REVOCATION_COLUMNS = ("revoked_at", "revoked_by")

#: A denial is terminal for the same reason. ``pending -> approved`` is the
#: legitimate widening the approval seam exists to perform, so ``approval_state``
#: cannot simply be pinned; the guard names the direction it refuses.
_DENIED_STATE = "denied"

#: The ``admin`` role's stable id — a frozen snapshot of
#: ``faultmaven.models.rbac_seed.SYSTEM_ROLE_IDS[Role.ADMIN]``, the same value
#: this baseline seeds below.
#: ``tests/unit/infrastructure/persistence/test_last_admin_guard_role_id.py``
#: asserts the two agree, so the snapshot cannot drift silently.
_ADMIN_ROLE_ID = "50551907-a02c-5bf7-9aa4-4a98f3c4eb64"

_LAST_ADMIN_FUNCTION = "organization_members_last_admin_guard"
_LAST_ADMIN_TRIGGER = "organization_members_last_admin"

#: Raised as ``check_violation`` (SQLSTATE 23514): this is an integrity
#: constraint the table carries, and callers that already handle constraint
#: violations handle it without learning a new error class.
_LAST_ADMIN_ERRCODE = "23514"

_CREATE_LAST_ADMIN_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {_LAST_ADMIN_FUNCTION}()
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
    -- membership is going with it.
    IF NOT EXISTS (SELECT 1 FROM users WHERE user_id = OLD.user_id) THEN
        RETURN NULL;
    END IF;

    -- Existence check AND serialisation point in one statement: a no-op
    -- self-update, so it cannot revert a concurrent write to that row the way
    -- a full-row write would, while still producing a real row version that
    -- makes a REPEATABLE READ transaction fail rather than count a stale
    -- roster.
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
            USING ERRCODE = '{_LAST_ADMIN_ERRCODE}',
                  CONSTRAINT = '{_LAST_ADMIN_TRIGGER}',
                  HINT = 'Grant another member the admin role first.';
    END IF;

    RETURN NULL;
END;
$$;
"""

_CREATE_LAST_ADMIN_TRIGGER = f"""
CREATE CONSTRAINT TRIGGER {_LAST_ADMIN_TRIGGER}
AFTER UPDATE OR DELETE ON organization_members
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION {_LAST_ADMIN_FUNCTION}()
"""

#: Every standalone plpgsql function this baseline creates. Dropping a table
#: takes its triggers with it but never these, so ``downgrade`` names them.
_PG_FUNCTIONS = (
    "operator_access_audit_append_only",
    "operator_access_audit_no_truncate_fn",
    "operator_access_grants_immutable",
    "operator_access_grants_no_delete_fn",
    "operator_access_grants_revocation_final",
    "operator_access_grants_denial_final",
    "operator_access_grants_no_truncate_fn",
    _LAST_ADMIN_FUNCTION,
)


# ---------------------------------------------------------------------------
# Seeds (migrations 006 and 029)
# ---------------------------------------------------------------------------

#: role name -> (role_id, description). IDs mirror ``rbac_seed.SYSTEM_ROLE_IDS``
#: so runtime callers can map a role name to its id without a query.
_ROLES = {
    "admin": (
        _ADMIN_ROLE_ID,
        "Full access to organization resources.",
    ),
    "member": (
        "5cb4c3f5-227c-5d73-95a5-d9d2e619ca72",
        "Standard investigator access.",
    ),
    "viewer": (
        "834b74a5-33a7-5248-9fd0-b040c12aef7b",
        "Read-only access.",
    ),
}

#: permission "resource:action" -> permission_id.
_PERMISSIONS = {
    "cases:read": "f3adedf0-09df-5151-9490-83da7cff6c34",
    "cases:write": "01fe79a0-a217-5998-9ab8-e7adde82ffde",
    "cases:delete": "91ed1dc0-2eb1-519f-b1e2-a1e93815cb59",
    "cases:assign": "818985d2-a051-5e55-86bb-1ba378835787",
    "cases:close": "5de3208e-7f06-5037-b8d9-8f5f90a0fbdf",
    "sessions:read": "f7c7be05-bf80-5504-832d-210827a0e450",
    "sessions:create": "cd3ba768-fd85-5c10-83da-830194251e29",
    "sessions:execute": "7123672b-6cb6-529d-87b1-6c74101c44a3",
    "sessions:manage": "7a9a6bde-3a6a-5086-b5c1-7703d524ce59",
    "evidence:read": "d06d9c54-a00e-58e3-ad38-19dbd32975ee",
    "evidence:upload": "0bd8e72e-a9ef-5ee4-9a67-32b8602c9d94",
    "evidence:delete": "a41fd5f5-f56b-5fb5-82f0-ff9c8f8ef2bf",
    "org:manage_users": "d3a93745-5f12-5622-a596-96e518669e90",
    "org:manage_settings": "31938541-ec6c-526f-a4ee-06e8d1b59252",
}

#: role name -> permissions it grants (snapshot of ``ROLE_PERMISSIONS``).
_GRANTS = {
    "admin": list(_PERMISSIONS.keys()),  # all permissions
    "member": [
        "cases:read",
        "cases:write",
        "cases:assign",
        "sessions:read",
        "sessions:create",
        "sessions:execute",
        "sessions:manage",
        "evidence:read",
        "evidence:upload",
    ],
    "viewer": [
        "cases:read",
        "sessions:read",
        "evidence:read",
    ],
}


def _enable_row_level_security() -> None:
    """Enrol every tenant-scoped table and key its policy on the enterprise."""
    for table in _TENANT_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
            f"USING ({_ENTERPRISE_MATCHES_SESSION})"
        )

    op.execute('ALTER TABLE "knowledge_items" ENABLE ROW LEVEL SECURITY')
    for name, clause in _KNOWLEDGE_POLICIES:
        op.execute(f'CREATE POLICY "{name}" ON "knowledge_items" {clause}')

    op.execute('ALTER TABLE "team_members" ENABLE ROW LEVEL SECURITY')
    op.execute(
        'CREATE POLICY "team_members_tenant_isolation" ON "team_members" '
        f"USING ({_TEAM_MEMBERS_POLICY})"
    )


def _create_operator_guards(dialect: str) -> None:
    """The append-only and immutability triggers (035 / 036)."""
    if dialect == "postgresql":
        op.execute(f"""
            CREATE OR REPLACE FUNCTION operator_access_audit_append_only()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION '{_AUDIT_APPEND_ONLY_MESSAGE}';
            END;
            $$ LANGUAGE plpgsql;
            """)
        for event in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER operator_access_audit_no_{event.lower()} "
                f"BEFORE {event} ON operator_access_audit "
                "FOR EACH ROW EXECUTE FUNCTION operator_access_audit_append_only()"
            )
        # Row triggers do not fire on TRUNCATE, so without this a role holding
        # the privilege could erase the whole trail trigger-free.
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
        return

    if dialect != "sqlite":
        return

    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER operator_access_audit_no_{event.lower()} "
            f"BEFORE {event} ON operator_access_audit "
            f"BEGIN SELECT RAISE(ABORT, '{_AUDIT_APPEND_ONLY_MESSAGE}'); END"
        )
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


def _seed(dialect: str) -> None:
    """The rows the deployment cannot start without.

    The standalone enterprise is the FK target every standalone write stamps;
    the standalone team is its default sharing unit; the RBAC rows are what
    ``organization_members.role_id`` (NOT NULL, ON DELETE RESTRICT) points at,
    so without them no member can be added and no permission check can pass.

    Written with raw SQL rather than ``bulk_insert`` for the two tenancy rows
    because ``enterprises.settings`` is JSONB on PostgreSQL: a bound text
    parameter would be rejected, while an inline ``'{}'`` literal casts from
    unknown as it did in migration 006.
    """
    now = "NOW()" if dialect == "postgresql" else "CURRENT_TIMESTAMP"
    op.execute(
        sa.text(f"""
            INSERT INTO enterprises (
                enterprise_id, name, slug, plan_tier, max_members,
                settings, created_at, updated_at
            ) VALUES (
                :id, :name, :slug, 'pro', 100,
                '{{}}', {now}, {now}
            )
            """).bindparams(
            id=_STANDALONE_ENTERPRISE_ID,
            name=_STANDALONE_ENTERPRISE_NAME,
            slug=_STANDALONE_ENTERPRISE_SLUG,
        )
    )
    op.execute(
        sa.text(f"""
            INSERT INTO teams (
                team_id, enterprise_id, name, created_at, updated_at
            ) VALUES (:id, :enterprise, :name, {now}, {now})
            """).bindparams(
            id=_STANDALONE_TEAM_ID,
            enterprise=_STANDALONE_ENTERPRISE_ID,
            name=_STANDALONE_TEAM_NAME,
        )
    )

    roles_tbl = sa.table(
        "roles",
        sa.column("role_id", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("scope", sa.String),
        sa.column("is_system_role", sa.Boolean),
    )
    permissions_tbl = sa.table(
        "permissions",
        sa.column("permission_id", sa.String),
        sa.column("resource", sa.String),
        sa.column("action", sa.String),
        sa.column("description", sa.Text),
    )
    role_permissions_tbl = sa.table(
        "role_permissions",
        sa.column("role_id", sa.String),
        sa.column("permission_id", sa.String),
    )

    op.bulk_insert(
        roles_tbl,
        [
            {
                "role_id": role_id,
                "name": name,
                "description": description,
                "scope": "organization",
                "is_system_role": True,
            }
            for name, (role_id, description) in _ROLES.items()
        ],
    )
    op.bulk_insert(
        permissions_tbl,
        [
            {
                "permission_id": permission_id,
                "resource": value.split(":", 1)[0],
                "action": value.split(":", 1)[1],
                "description": None,
            }
            for value, permission_id in _PERMISSIONS.items()
        ],
    )
    op.bulk_insert(
        role_permissions_tbl,
        [
            {
                "role_id": _ROLES[role_name][0],
                "permission_id": _PERMISSIONS[perm_value],
            }
            for role_name, perm_values in _GRANTS.items()
            for perm_value in perm_values
        ],
    )


def upgrade() -> None:
    """The whole schema: tables, policies, guards, seeds."""
    dialect = op.get_context().dialect.name

    op.create_table(
        "enterprises",
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column(
            "plan_tier", sa.String(length=20), server_default="free", nullable=False
        ),
        sa.Column("max_members", sa.Integer(), server_default="5", nullable=False),
        sa.Column("max_cases", sa.Integer(), nullable=True),
        sa.Column("billing_email", sa.String(length=255), nullable=True),
        sa.Column(
            "settings",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "plan_tier IN ('free', 'starter', 'pro', 'business')",
            name="enterprises_plan_tier_check",
        ),
        sa.CheckConstraint("LENGTH(TRIM(name)) > 0", name="enterprises_name_not_empty"),
        sa.PrimaryKeyConstraint("enterprise_id"),
    )
    op.create_index(
        "ix_enterprises_domain_live",
        "enterprises",
        ["domain"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_enterprises_slug", "enterprises", ["slug"], unique=False)
    op.create_index(
        "ix_enterprises_slug_live",
        "enterprises",
        ["slug"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "operator_access_audit",
        sa.Column("audit_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("operator_user_id", sa.String(length=36), nullable=True),
        sa.Column("operator_username", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("target_enterprise_id", sa.String(length=36), nullable=True),
        sa.Column("target_case_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("grant_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deployment_mode", sa.String(length=32), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('list', 'content_open', 'role_granted', 'role_revoked')",
            name="operator_access_audit_action_valid",
        ),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index(
        "ix_operator_access_audit_case",
        "operator_access_audit",
        ["target_case_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_operator_access_audit_created_at"),
        "operator_access_audit",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_operator_access_audit_grant",
        "operator_access_audit",
        ["grant_id"],
        unique=False,
    )
    op.create_index(
        "ix_operator_access_audit_operator",
        "operator_access_audit",
        ["operator_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_operator_access_audit_target_enterprise",
        "operator_access_audit",
        ["target_enterprise_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "operator_access_grants",
        sa.Column("grant_id", sa.String(length=36), nullable=False),
        sa.Column("operator_user_id", sa.String(length=36), nullable=False),
        sa.Column("operator_username", sa.String(length=255), nullable=True),
        sa.Column("target_case_id", sa.String(length=36), nullable=False),
        sa.Column("target_enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(length=36), nullable=True),
        sa.Column(
            "approval_state",
            sa.String(length=32),
            server_default="auto_approved",
            nullable=False,
        ),
        sa.Column("approved_by", sa.String(length=36), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deployment_mode", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "approval_state IN ('auto_approved', 'pending', 'approved', 'denied')",
            name="operator_access_grants_approval_state_valid",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="operator_access_grants_window_valid"
        ),
        sa.PrimaryKeyConstraint("grant_id"),
    )
    op.create_index(
        "ix_operator_access_grants_operator_case",
        "operator_access_grants",
        ["operator_user_id", "target_case_id", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_operator_access_grants_target_enterprise",
        "operator_access_grants",
        ["target_enterprise_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "permissions",
        sa.Column("permission_id", sa.String(length=36), nullable=False),
        sa.Column("resource", sa.String(length=50), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("permission_id"),
        sa.UniqueConstraint(
            "resource", "action", name="permissions_resource_action_unique"
        ),
    )
    op.create_table(
        "roles",
        sa.Column("role_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "scope", sa.String(length=20), server_default="organization", nullable=False
        ),
        sa.Column("is_system_role", sa.Boolean(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope IN ('system', 'enterprise', 'organization', 'team')",
            name="roles_scope_check",
        ),
        sa.PrimaryKeyConstraint("role_id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.String(length=36), nullable=False),
        sa.Column("permission_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["permissions.permission_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["role_id"], ["roles.role_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
    )
    op.create_table(
        "sso_org_mappings",
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_org_id", sa.String(length=255), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("provider", "provider_org_id"),
        sa.UniqueConstraint(
            "provider", "enterprise_id", name="uq_sso_org_mappings_enterprise"
        ),
    )
    op.create_table(
        "sso_personal_enterprises",
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("provider_org_id", sa.String(length=255), nullable=False),
        sa.Column(
            "membership_confirmed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retirement_state", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "retirement_state IS NULL OR retirement_state IN ('refuse', 'fresh_tenant')",
            name="sso_personal_enterprises_retirement_state_check",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("subject"),
        sa.UniqueConstraint(
            "enterprise_id", name="uq_sso_personal_enterprises_enterprise"
        ),
    )
    op.create_table(
        "teams",
        sa.Column("team_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("LENGTH(TRIM(name)) > 0", name="teams_name_not_empty"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("team_id"),
        sa.UniqueConstraint(
            "enterprise_id", "name", name="teams_enterprise_name_unique"
        ),
    )
    op.create_index(
        op.f("ix_teams_enterprise_id"), "teams", ["enterprise_id"], unique=False
    )
    op.create_table(
        "turn_usage",
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("billing_subject_kind", sa.String(length=20), nullable=False),
        sa.Column("billing_subject_id", sa.String(length=36), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column(
            "turn_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.CheckConstraint(
            "billing_subject_kind IN ('organization', 'account')",
            name="turn_usage_subject_kind_check",
        ),
        sa.CheckConstraint("turn_count >= 0", name="turn_usage_non_negative"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "billing_subject_kind", "billing_subject_id", "usage_date"
        ),
    )
    op.create_index(
        op.f("ix_turn_usage_enterprise_id"),
        "turn_usage",
        ["enterprise_id"],
        unique=False,
    )
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("avatar_url", sa.String(length=500), nullable=True),
        sa.Column(
            "timezone", sa.String(length=50), server_default="UTC", nullable=False
        ),
        sa.Column(
            "locale", sa.String(length=10), server_default="en-US", nullable=False
        ),
        sa.Column("hashed_password", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "is_email_verified", sa.Boolean(), server_default="0", nullable=False
        ),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sso_provider", sa.String(length=50), nullable=True),
        sa.Column("sso_provider_id", sa.String(length=255), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_password_change_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dev_roles", sa.Text(), nullable=True),
        sa.Column(
            "account_kind",
            sa.String(length=20),
            server_default="individual",
            nullable=False,
        ),
        sa.Column("service_channel", sa.String(length=20), nullable=True),
        sa.CheckConstraint(
            "account_kind IN ('individual', 'service')", name="users_account_kind_check"
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(display_name)) > 0", name="users_display_name_not_empty"
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("sso_provider", "sso_provider_id", name="users_sso_unique"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index(
        op.f("ix_users_enterprise_id"), "users", ["enterprise_id"], unique=False
    )
    op.create_index("ix_users_is_active", "users", ["is_active"], unique=False)
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_table(
        "config_overrides",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "category", sa.String(length=50), server_default="llm", nullable=False
        ),
        sa.Column(
            "source", sa.String(length=20), server_default="admin", nullable=False
        ),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "oauth_authorization_codes",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_index(
        "idx_auth_codes_expires_at",
        "oauth_authorization_codes",
        ["expires_at"],
        unique=False,
    )
    op.create_table(
        "organizations",
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "settings",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("daily_turn_cap", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "LENGTH(TRIM(name)) > 0", name="organizations_name_not_empty"
        ),
        sa.CheckConstraint("LENGTH(slug) > 0", name="organizations_slug_not_empty"),
        sa.CheckConstraint(
            "daily_turn_cap IS NULL OR daily_turn_cap >= 0",
            name="organizations_daily_turn_cap_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("organization_id"),
    )
    op.create_index(
        op.f("ix_organizations_enterprise_id"),
        "organizations",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organizations_owner_id"), "organizations", ["owner_id"], unique=False
    )
    op.create_index(
        "ix_organizations_slug_live",
        "organizations",
        ["enterprise_id", "slug"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "team_invitations",
        sa.Column("invitation_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("team_id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("invited_user_id", sa.String(length=36), nullable=True),
        sa.Column("invited_by", sa.String(length=36), nullable=True),
        sa.Column(
            "status", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'revoked', 'expired')",
            name="team_invitations_status_check",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(email)) > 0", name="team_invitations_email_not_empty"
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["invited_by"], ["users.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["invited_user_id"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("invitation_id"),
    )
    op.create_index(
        "ix_team_invitations_email", "team_invitations", ["email"], unique=False
    )
    op.create_index(
        op.f("ix_team_invitations_enterprise_id"),
        "team_invitations",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_team_invitations_status"), "team_invitations", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_team_invitations_team_id"),
        "team_invitations",
        ["team_id"],
        unique=False,
    )
    op.create_table(
        "team_members",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("team_id", sa.String(length=36), nullable=False),
        sa.Column("team_role", sa.String(length=50), nullable=True),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["team_id"], ["teams.team_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "team_id"),
    )
    op.create_index(
        "ix_team_members_team_id", "team_members", ["team_id"], unique=False
    )
    op.create_table(
        "cases",
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "state", sa.String(length=50), server_default="inquiry", nullable=False
        ),
        sa.Column(
            "source", sa.String(length=20), server_default="copilot", nullable=False
        ),
        sa.Column("investigation_strategy", sa.Text(), nullable=True),
        sa.Column("current_turn", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "turns_without_progress", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("closure_reason", sa.String(length=100), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disposition_eligibility", sa.Text(), nullable=True),
        sa.Column(
            "inquiry",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "problem_verification",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "working_conclusion",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "root_cause_conclusion",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "escalation_state",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "documentation",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "progress",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('inquiry', 'closed') OR LENGTH(TRIM(description)) > 0",
            name="cases_description_required_for_investigation",
        ),
        sa.CheckConstraint(
            "state IN ('inquiry', 'investigating', 'resolved', 'closed')",
            name="cases_state_check",
        ),
        sa.CheckConstraint("LENGTH(TRIM(title)) > 0", name="cases_title_not_empty"),
        sa.CheckConstraint("current_turn >= 0", name="cases_current_turn_nonnegative"),
        sa.CheckConstraint(
            "turns_without_progress >= 0",
            name="cases_turns_without_progress_nonnegative",
        ),
        sa.CheckConstraint("version >= 1", name="cases_version_positive"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("case_id"),
    )
    op.create_index(op.f("ix_cases_closed_at"), "cases", ["closed_at"], unique=False)
    op.create_index(op.f("ix_cases_created_at"), "cases", ["created_at"], unique=False)
    op.create_index(
        op.f("ix_cases_enterprise_id"), "cases", ["enterprise_id"], unique=False
    )
    op.create_index(
        op.f("ix_cases_last_activity_at"), "cases", ["last_activity_at"], unique=False
    )
    op.create_index(
        op.f("ix_cases_organization_id"), "cases", ["organization_id"], unique=False
    )
    op.create_index(op.f("ix_cases_source"), "cases", ["source"], unique=False)
    op.create_index(op.f("ix_cases_state"), "cases", ["state"], unique=False)
    op.create_index(op.f("ix_cases_user_id"), "cases", ["user_id"], unique=False)
    op.create_table(
        "knowledge_items",
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column(
            "scope", sa.String(length=20), server_default="global", nullable=False
        ),
        sa.Column("owner_id", sa.String(length=36), nullable=True),
        sa.Column("source_suggestion_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("item_type", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=True),
        sa.Column("tags", _TAGS_ARRAY, nullable=True),
        sa.Column(
            "embedding_model",
            sa.String(length=128),
            server_default="bge-m3",
            nullable=False,
        ),
        sa.Column("embedding_vector", sa.Text(), nullable=True),
        sa.Column(
            "embedding_version", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("language", sa.String(length=8), server_default="en", nullable=False),
        sa.Column(
            "verification_level", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("verification_reason", sa.String(length=512), nullable=True),
        sa.Column("verified_by", sa.String(length=36), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("view_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("helpful_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "not_helpful_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("last_retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_published", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "item_type IN ('troubleshooting_guide', 'error_pattern', 'solution_template', 'api_documentation', 'configuration_guide', 'best_practice', 'faq', 'runbook')",
            name="knowledge_items_item_type_check",
        ),
        sa.CheckConstraint(
            "scope <> 'global' OR organization_id IS NULL",
            name="knowledge_items_global_org_check",
        ),
        sa.CheckConstraint(
            "scope IN ('personal', 'team', 'global')",
            name="knowledge_items_scope_check",
        ),
        sa.CheckConstraint(
            "embedding_version >= 1", name="knowledge_items_embedding_version_check"
        ),
        sa.CheckConstraint(
            "helpful_count >= 0", name="knowledge_items_helpful_count_check"
        ),
        sa.CheckConstraint(
            "not_helpful_count >= 0", name="knowledge_items_not_helpful_count_check"
        ),
        sa.CheckConstraint(
            "verification_level >= 0 AND verification_level <= 2",
            name="knowledge_items_verification_level_check",
        ),
        sa.CheckConstraint("view_count >= 0", name="knowledge_items_view_count_check"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["verified_by"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("item_id"),
    )
    op.create_index(
        op.f("ix_knowledge_items_category"),
        "knowledge_items",
        ["category"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_items_created_at"),
        "knowledge_items",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_items_enterprise_id"),
        "knowledge_items",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_items_is_published"),
        "knowledge_items",
        ["is_published"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_items_item_type"),
        "knowledge_items",
        ["item_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_items_last_retrieved_at"),
        "knowledge_items",
        ["last_retrieved_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_items_organization_id"),
        "knowledge_items",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_items_owner_id"),
        "knowledge_items",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_items_scope"), "knowledge_items", ["scope"], unique=False
    )
    op.create_index(
        op.f("ix_knowledge_items_source_suggestion_id"),
        "knowledge_items",
        ["source_suggestion_id"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_items_tags",
        "knowledge_items",
        ["tags"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        op.f("ix_knowledge_items_verification_level"),
        "knowledge_items",
        ["verification_level"],
        unique=False,
    )
    op.create_table(
        "organization_members",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("role_id", sa.String(length=36), nullable=False),
        sa.Column("invited_by", sa.String(length=36), nullable=True),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invitation_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["invited_by"], ["users.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["role_id"], ["roles.role_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "organization_id"),
    )
    op.create_index(
        "ix_org_members_organization_id",
        "organization_members",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_org_members_role_id", "organization_members", ["role_id"], unique=False
    )
    op.create_index(
        op.f("ix_organization_members_enterprise_id"),
        "organization_members",
        ["enterprise_id"],
        unique=False,
    )
    op.create_table(
        "resource_shares",
        sa.Column("share_id", sa.String(length=36), nullable=False),
        sa.Column("resource_type", sa.String(length=20), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "resource_type IN ('case', 'knowledge_item', 'conversion_job')",
            name="resource_shares_resource_type_check",
        ),
        sa.CheckConstraint(
            "scope_type IN ('team', 'organization')",
            name="resource_shares_scope_type_check",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("share_id"),
        sa.UniqueConstraint(
            "resource_type",
            "resource_id",
            "scope_type",
            "scope_id",
            name="uq_resource_shares_target",
        ),
    )
    op.create_index(
        op.f("ix_resource_shares_enterprise_id"),
        "resource_shares",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_resource_shares_organization_id"),
        "resource_shares",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_resource_shares_scope",
        "resource_shares",
        ["scope_type", "scope_id", "resource_type", "resource_id"],
        unique=False,
    )
    op.create_table(
        "user_audit_log",
        sa.Column("audit_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("event_category", sa.String(length=50), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=True),
        sa.Column("resource_id", sa.String(length=50), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("success", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index(
        op.f("ix_user_audit_log_enterprise_id"),
        "user_audit_log",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_audit_log_event_type"),
        "user_audit_log",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        "ix_user_audit_log_organization_id",
        "user_audit_log",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_user_audit_log_user_id",
        "user_audit_log",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "case_actions",
        sa.Column("transition_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("from_state", sa.String(length=50), nullable=True),
        sa.Column("to_state", sa.String(length=50), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(length=50), nullable=False),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "transitioned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("transition_id"),
    )
    op.create_index(
        op.f("ix_case_actions_case_id"), "case_actions", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_case_actions_enterprise_id"),
        "case_actions",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_case_actions_organization_id"),
        "case_actions",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "case_checkpoints",
        sa.Column("checkpoint_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column(
            "case_snapshot",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=50), nullable=False),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(snapshot_hash)) > 0", name="case_checkpoints_hash_not_empty"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("checkpoint_id"),
    )
    op.create_index(
        op.f("ix_case_checkpoints_case_id"),
        "case_checkpoints",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        "ix_case_checkpoints_case_turn",
        "case_checkpoints",
        ["case_id", "turn_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_case_checkpoints_created_at"),
        "case_checkpoints",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_case_checkpoints_enterprise_id"),
        "case_checkpoints",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_case_checkpoints_organization_id"),
        "case_checkpoints",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "case_messages",
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("turn_number", sa.Integer(), server_default="0", nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("author_id", sa.String(length=36), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')", name="case_messages_role_check"
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(content)) > 0", name="case_messages_content_not_empty"
        ),
        sa.CheckConstraint("turn_number >= 0", name="case_messages_turn_nonnegative"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index(
        "ix_case_messages_case_created",
        "case_messages",
        ["case_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_case_messages_case_id"), "case_messages", ["case_id"], unique=False
    )
    op.create_index(
        "ix_case_messages_case_turn",
        "case_messages",
        ["case_id", "turn_number"],
        unique=False,
    )
    op.create_index(
        op.f("ix_case_messages_created_at"),
        "case_messages",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_case_messages_enterprise_id"),
        "case_messages",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_case_messages_organization_id"),
        "case_messages",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "case_tags",
        sa.Column("tag_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("tag", sa.String(length=50), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint("tag NOT LIKE '%,%'", name="case_tags_no_commas"),
        sa.CheckConstraint("LENGTH(TRIM(tag)) > 0", name="case_tags_tag_not_empty"),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("tag_id"),
        sa.UniqueConstraint("case_id", "tag", name="case_tags_unique"),
    )
    op.create_index(
        op.f("ix_case_tags_case_id"), "case_tags", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_case_tags_enterprise_id"), "case_tags", ["enterprise_id"], unique=False
    )
    op.create_index(
        op.f("ix_case_tags_organization_id"),
        "case_tags",
        ["organization_id"],
        unique=False,
    )
    op.create_index(op.f("ix_case_tags_tag"), "case_tags", ["tag"], unique=False)
    op.create_table(
        "causal_nodes",
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("node_type", sa.String(length=20), nullable=False),
        sa.Column(
            "node_state",
            sa.String(length=20),
            server_default="candidate",
            nullable=False,
        ),
        sa.Column(
            "validation_method",
            sa.String(length=20),
            server_default="none",
            nullable=False,
        ),
        sa.Column(
            "belief",
            sa.Numeric(precision=3, scale=2),
            server_default="0.5",
            nullable=True,
        ),
        sa.Column(
            "signature_consistent", sa.Boolean(), server_default="1", nullable=False
        ),
        sa.Column("actionable", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("state_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "generated_at_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "last_updated_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "last_progress_at_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "iterations_without_progress",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("refutation_reason", sa.String(length=200), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "proposed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(node_state = 'refuted' AND refutation_reason IS NOT NULL) OR (node_state <> 'refuted' AND refutation_reason IS NULL)",
            name="causal_nodes_refutation_pairing",
        ),
        sa.CheckConstraint(
            "NOT (node_type = 'root' AND node_state = 'validated' AND NOT actionable)",
            name="causal_nodes_validated_root_actionable",
        ),
        sa.CheckConstraint(
            "node_state <> 'validated' OR validation_method <> 'none'",
            name="causal_nodes_validated_requires_method",
        ),
        sa.CheckConstraint(
            "node_state IN ('candidate', 'validated', 'refuted', 'inconclusive')",
            name="causal_nodes_node_state_check",
        ),
        sa.CheckConstraint(
            "node_type IN ('problem', 'intermediate', 'root')",
            name="causal_nodes_node_type_check",
        ),
        sa.CheckConstraint(
            "validation_method IN ('none', 'empirical', 'deductive')",
            name="causal_nodes_validation_method_check",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(statement)) > 0", name="causal_nodes_statement_not_empty"
        ),
        sa.CheckConstraint(
            "belief IS NULL OR (belief >= 0 AND belief <= 1)",
            name="causal_nodes_belief_range",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_index(
        op.f("ix_causal_nodes_case_id"), "causal_nodes", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_causal_nodes_category"), "causal_nodes", ["category"], unique=False
    )
    op.create_index(
        op.f("ix_causal_nodes_enterprise_id"),
        "causal_nodes",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_causal_nodes_node_state"), "causal_nodes", ["node_state"], unique=False
    )
    op.create_index(
        op.f("ix_causal_nodes_node_type"), "causal_nodes", ["node_type"], unique=False
    )
    op.create_index(
        op.f("ix_causal_nodes_organization_id"),
        "causal_nodes",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "uq_causal_nodes_one_problem_per_case",
        "causal_nodes",
        ["case_id"],
        unique=True,
        sqlite_where=sa.text("node_type = 'problem'"),
        postgresql_where=sa.text("node_type = 'problem'"),
    )
    op.create_table(
        "evidence_needs",
        sa.Column("need_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("request_text", sa.String(length=500), nullable=False),
        sa.Column("rationale", sa.String(length=500), nullable=False),
        sa.Column(
            "priority", sa.String(length=10), server_default="medium", nullable=False
        ),
        sa.Column(
            "state", sa.String(length=20), server_default="pending", nullable=False
        ),
        sa.Column(
            "obtainability",
            sa.String(length=20),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column(
            "motivating_hypothesis_ids",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "surfaced_turns",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("engine_inferred", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("superseded_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at_turn", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(state = 'superseded' AND superseded_reason IS NOT NULL) OR (state != 'superseded' AND superseded_reason IS NULL)",
            name="evidence_needs_superseded_reason_invariant",
        ),
        sa.CheckConstraint(
            "priority IN ('high', 'medium', 'low')",
            name="evidence_needs_priority_check",
        ),
        sa.CheckConstraint(
            "purpose IN ('symptom_verification', 'causal_verification')",
            name="evidence_needs_purpose_check",
        ),
        sa.CheckConstraint(
            "obtainability IN ('unknown', 'obtainable', 'unobtainable')",
            name="evidence_needs_obtainability_check",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'partially_met', 'fulfilled', 'superseded')",
            name="evidence_needs_state_check",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(rationale)) > 0", name="evidence_needs_rationale_not_empty"
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(request_text)) > 0",
            name="evidence_needs_request_text_not_empty",
        ),
        sa.CheckConstraint(
            "created_at_turn >= 0", name="evidence_needs_created_at_turn_nonnegative"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("need_id"),
    )
    op.create_index(
        op.f("ix_evidence_needs_case_id"), "evidence_needs", ["case_id"], unique=False
    )
    op.create_index(
        "ix_evidence_needs_case_purpose",
        "evidence_needs",
        ["case_id", "purpose"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_needs_case_state",
        "evidence_needs",
        ["case_id", "state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_needs_enterprise_id"),
        "evidence_needs",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_needs_organization_id"),
        "evidence_needs",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_needs_state"), "evidence_needs", ["state"], unique=False
    )
    op.create_table(
        "investigation_sessions",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column(
            "state", sa.String(length=32), server_default="active", nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_duration_ms", sa.Integer(), nullable=True),
        sa.Column("session_goal", sa.Text(), nullable=True),
        sa.Column("findings_summary", sa.Text(), nullable=True),
        sa.Column(
            "total_token_usage", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "total_agent_executions", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("token_budget_limit", sa.Integer(), nullable=True),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('active', 'paused', 'completed', 'abandoned')",
            name="investigation_sessions_state_check",
        ),
        sa.CheckConstraint(
            "token_budget_limit IS NULL OR token_budget_limit >= 0",
            name="investigation_sessions_budget_check",
        ),
        sa.CheckConstraint(
            "total_agent_executions >= 0",
            name="investigation_sessions_executions_check",
        ),
        sa.CheckConstraint(
            "total_duration_ms IS NULL OR total_duration_ms >= 0",
            name="investigation_sessions_duration_check",
        ),
        sa.CheckConstraint(
            "total_token_usage >= 0", name="investigation_sessions_token_usage_check"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        op.f("ix_investigation_sessions_case_id"),
        "investigation_sessions",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investigation_sessions_created_at"),
        "investigation_sessions",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investigation_sessions_enterprise_id"),
        "investigation_sessions",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investigation_sessions_organization_id"),
        "investigation_sessions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investigation_sessions_state"),
        "investigation_sessions",
        ["state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_investigation_sessions_user_id"),
        "investigation_sessions",
        ["user_id"],
        unique=False,
    )
    op.create_table(
        "knowledge_suggestions",
        sa.Column("suggestion_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=True),
        sa.Column("knowledge_item_id", sa.String(length=36), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending_review",
            nullable=False,
        ),
        sa.Column("suggested_title", sa.String(length=512), nullable=False),
        sa.Column("suggested_content", sa.Text(), nullable=False),
        sa.Column(
            "suggested_type",
            sa.String(length=64),
            server_default="troubleshooting_guide",
            nullable=False,
        ),
        sa.Column("extracted_by", sa.String(length=36), nullable=True),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("include_messages", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("include_evidence", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "pii_scan_status",
            sa.String(length=32),
            server_default="not_scanned",
            nullable=False,
        ),
        sa.Column(
            "pii_scan_result",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column("pii_remediated_by", sa.String(length=36), nullable=True),
        sa.Column("pii_remediated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_case_title", sa.String(length=512), nullable=True),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("evidence_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reviewed_by", sa.String(length=36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("validation_passed", sa.Boolean(), nullable=True),
        sa.Column(
            "validation_errors",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "validation_warnings",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "pii_scan_status IN ('not_scanned', 'scanning', 'clean', 'pii_detected', 'remediated', 'scan_failed')",
            name="knowledge_suggestions_pii_scan_status_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'draft')",
            name="knowledge_suggestions_status_check",
        ),
        sa.CheckConstraint(
            "evidence_count >= 0", name="knowledge_suggestions_evidence_count_check"
        ),
        sa.CheckConstraint(
            "message_count >= 0", name="knowledge_suggestions_message_count_check"
        ),
        sa.CheckConstraint(
            "version >= 1", name="knowledge_suggestions_version_positive"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["extracted_by"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_item_id"], ["knowledge_items.item_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["pii_remediated_by"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("suggestion_id"),
    )
    op.create_index(
        op.f("ix_knowledge_suggestions_case_id"),
        "knowledge_suggestions",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_suggestions_created_at"),
        "knowledge_suggestions",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_suggestions_enterprise_id"),
        "knowledge_suggestions",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_suggestions_extracted_by"),
        "knowledge_suggestions",
        ["extracted_by"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_suggestions_knowledge_item_id"),
        "knowledge_suggestions",
        ["knowledge_item_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_suggestions_organization_id"),
        "knowledge_suggestions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_suggestions_pii_scan_status"),
        "knowledge_suggestions",
        ["pii_scan_status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_knowledge_suggestions_status"),
        "knowledge_suggestions",
        ["status"],
        unique=False,
    )
    op.create_table(
        "reports",
        sa.Column("report_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("generated_by", sa.String(length=36), nullable=True),
        sa.Column("report_type", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default="1", nullable=False),
        sa.Column(
            "linked_to_closure", sa.Boolean(), server_default="0", nullable=False
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "format", sa.String(length=20), server_default="markdown", nullable=False
        ),
        sa.Column("generation_status", sa.String(length=20), nullable=False),
        sa.Column("generation_time_ms", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "format IN ('markdown', 'html')", name="reports_format_check"
        ),
        sa.CheckConstraint(
            "generation_status IN ('generating', 'completed', 'failed')",
            name="reports_status_check",
        ),
        sa.CheckConstraint(
            "report_type IN ('resolution_summary', 'closure_summary')",
            name="reports_type_check",
        ),
        sa.CheckConstraint(
            "generation_time_ms >= 0 AND generation_time_ms <= 120000",
            name="reports_gen_time_check",
        ),
        sa.CheckConstraint(
            "version >= 1 AND version <= 5", name="reports_version_check"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["generated_by"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("report_id"),
    )
    op.create_index(
        "idx_reports_type_version", "reports", ["case_id", "report_type"], unique=False
    )
    op.create_index(op.f("ix_reports_case_id"), "reports", ["case_id"], unique=False)
    op.create_index(
        op.f("ix_reports_enterprise_id"), "reports", ["enterprise_id"], unique=False
    )
    op.create_index(
        op.f("ix_reports_organization_id"), "reports", ["organization_id"], unique=False
    )
    op.create_table(
        "uploaded_files",
        sa.Column("file_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=True),
        sa.Column("uploaded_by", sa.String(length=36), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("storage_ref", sa.String(length=1000), nullable=True),
        sa.Column(
            "upload_source",
            sa.String(length=50),
            server_default="file_upload",
            nullable=False,
        ),
        sa.Column("uploaded_at_turn", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("structural_index", sa.Text(), nullable=True),
        sa.Column("data_type", sa.String(length=50), nullable=True),
        sa.Column("coverage_start_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_end_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_source", sa.String(length=50), nullable=True),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(filename)) > 0", name="uploaded_files_filename_not_empty"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="uploaded_files_size_nonnegative"),
        sa.CheckConstraint(
            "uploaded_at_turn >= 0", name="uploaded_files_turn_nonnegative"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("file_id"),
    )
    op.create_index(
        op.f("ix_uploaded_files_case_id"), "uploaded_files", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_uploaded_files_content_hash"),
        "uploaded_files",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_uploaded_files_enterprise_id"),
        "uploaded_files",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_uploaded_files_organization_id"),
        "uploaded_files",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_uploaded_files_uploaded_by"),
        "uploaded_files",
        ["uploaded_by"],
        unique=False,
    )
    op.create_table(
        "causal_edges",
        sa.Column("edge_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("cause_node_id", sa.String(length=36), nullable=False),
        sa.Column("effect_node_id", sa.String(length=36), nullable=False),
        sa.Column("and_group", sa.String(length=64), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("created_at_turn", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cause_node_id <> effect_node_id", name="causal_edges_no_self_loop"
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["cause_node_id"], ["causal_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["effect_node_id"], ["causal_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("edge_id"),
    )
    op.create_index(
        op.f("ix_causal_edges_case_id"), "causal_edges", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_causal_edges_cause_node_id"),
        "causal_edges",
        ["cause_node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_causal_edges_effect_node_id"),
        "causal_edges",
        ["effect_node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_causal_edges_enterprise_id"),
        "causal_edges",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_causal_edges_organization_id"),
        "causal_edges",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "conversion_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=True),
        sa.Column("live_case_id", sa.String(length=36), nullable=True),
        sa.Column("source_file_id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="processing", nullable=False
        ),
        sa.Column(
            "source_type",
            sa.String(length=20),
            server_default="document",
            nullable=False,
        ),
        sa.Column(
            "failure_modes_detected", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("analysis_result", sa.JSON(), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope IN ('personal', 'team', 'global')",
            name="conversion_jobs_scope_check",
        ),
        sa.CheckConstraint(
            "source_type IN ('document', 'case')",
            name="conversion_jobs_source_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'partial', 'failed', 'cancelled')",
            name="conversion_jobs_status_check",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"], ["uploaded_files.file_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_conversion_jobs_case_id"), "conversion_jobs", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_conversion_jobs_enterprise_id"),
        "conversion_jobs",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversion_jobs_organization_id"),
        "conversion_jobs",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversion_jobs_source_file_id"),
        "conversion_jobs",
        ["source_file_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversion_jobs_user_id"), "conversion_jobs", ["user_id"], unique=False
    )
    op.create_index(
        "uq_conversion_jobs_live_case_id",
        "conversion_jobs",
        ["live_case_id"],
        unique=True,
    )
    op.create_table(
        "evidence",
        sa.Column("evidence_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("source_file_id", sa.String(length=36), nullable=True),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=True),
        sa.Column(
            "primary_purpose",
            sa.String(length=100),
            server_default="legacy",
            nullable=False,
        ),
        sa.Column("analysis", sa.Text(), nullable=True),
        sa.Column("processing_mode", sa.String(length=50), nullable=True),
        sa.Column("advances_milestones", _TAGS_ARRAY, nullable=True),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("extract", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("reliability_score", sa.Float(), nullable=True),
        sa.Column("tags", _TAGS_ARRAY, nullable=True),
        sa.Column("collected_at_turn", sa.Integer(), nullable=True),
        sa.Column("coverage_start_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_end_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("coverage_source", sa.String(length=50), nullable=True),
        sa.Column("vectorized", sa.Boolean(), server_default="0", nullable=False),
        sa.Column(
            "collected_by",
            sa.String(length=50),
            server_default="system",
            nullable=False,
        ),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_file_id IS NOT NULL OR source_type = 'user_description'",
            name="evidence_source_invariant",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(summary)) > 0", name="evidence_summary_not_empty"
        ),
        sa.CheckConstraint(
            "extract IS NULL OR LENGTH(TRIM(extract)) > 0",
            name="evidence_extract_not_empty",
        ),
        sa.CheckConstraint(
            "reliability_score IS NULL OR (reliability_score >= 0 AND reliability_score <= 1)",
            name="evidence_reliability_range",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"], ["uploaded_files.file_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("evidence_id"),
    )
    op.create_index(op.f("ix_evidence_case_id"), "evidence", ["case_id"], unique=False)
    op.create_index(
        "ix_evidence_case_is_primary",
        "evidence",
        ["case_id", "is_primary"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_category"), "evidence", ["category"], unique=False
    )
    op.create_index(
        op.f("ix_evidence_collected_at_turn"),
        "evidence",
        ["collected_at_turn"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_coverage",
        "evidence",
        ["case_id", "coverage_start_ts", "coverage_end_ts"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_enterprise_id"), "evidence", ["enterprise_id"], unique=False
    )
    op.create_index(
        op.f("ix_evidence_organization_id"),
        "evidence",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_source_file_id"), "evidence", ["source_file_id"], unique=False
    )
    op.create_index(
        "ix_evidence_tags", "evidence", ["tags"], unique=False, postgresql_using="gin"
    )
    op.create_table(
        "hypotheses",
        sa.Column("hypothesis_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("root_node_id", sa.String(length=36), nullable=True),
        sa.Column(
            "path",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column(
            "state", sa.String(length=20), server_default="captured", nullable=False
        ),
        sa.Column(
            "likelihood",
            sa.Numeric(precision=3, scale=2),
            server_default="0.5",
            nullable=True,
        ),
        sa.Column(
            "initial_likelihood",
            sa.Numeric(precision=3, scale=2),
            server_default="0.5",
            nullable=True,
        ),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column(
            "generation_mode",
            sa.String(length=20),
            server_default="systematic",
            nullable=False,
        ),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("retirement_reason", sa.Text(), nullable=True),
        sa.Column("refutation_reason", sa.String(length=200), nullable=True),
        sa.Column(
            "generated_at_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "last_updated_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "last_progress_at_turn", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "iterations_without_progress",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("concluded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "proposed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('captured', 'active', 'validated', 'refuted', 'inconclusive', 'retired')",
            name="hypotheses_state_check",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(statement)) > 0", name="hypotheses_statement_not_empty"
        ),
        sa.CheckConstraint(
            "likelihood IS NULL OR (likelihood >= 0 AND likelihood <= 1)",
            name="hypotheses_likelihood_range",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["root_node_id"], ["causal_nodes.node_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("hypothesis_id"),
    )
    op.create_index(
        op.f("ix_hypotheses_case_id"), "hypotheses", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_hypotheses_category"), "hypotheses", ["category"], unique=False
    )
    op.create_index(
        op.f("ix_hypotheses_created_by"), "hypotheses", ["created_by"], unique=False
    )
    op.create_index(
        op.f("ix_hypotheses_enterprise_id"),
        "hypotheses",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_hypotheses_organization_id"),
        "hypotheses",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_hypotheses_root_node_id"), "hypotheses", ["root_node_id"], unique=False
    )
    op.create_index(op.f("ix_hypotheses_state"), "hypotheses", ["state"], unique=False)
    op.create_table(
        "case_entities",
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_value", sa.String(length=255), nullable=False),
        sa.Column("evidence_id", sa.String(length=36), nullable=False),
        sa.Column("mention_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("in_error_context", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("first_seen_ts", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"], ["evidence.evidence_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint(
            "case_id", "entity_type", "entity_value", "evidence_id"
        ),
    )
    op.create_index(
        "idx_case_entities_by_evidence", "case_entities", ["evidence_id"], unique=False
    )
    op.create_index(
        "idx_case_entities_lookup",
        "case_entities",
        ["case_id", "entity_type", "entity_value"],
        unique=False,
    )
    op.create_index(
        op.f("ix_case_entities_enterprise_id"),
        "case_entities",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_case_entities_organization_id"),
        "case_entities",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "causal_node_evidence",
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("stance", sa.String(length=20), nullable=False),
        sa.Column("stance_confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("linked_at_turn", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stance IN ('supports', 'refutes', 'neutral')",
            name="causal_node_evidence_stance_check",
        ),
        sa.CheckConstraint(
            "stance_confidence IS NULL OR (stance_confidence >= 0 AND stance_confidence <= 1)",
            name="causal_node_evidence_confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"], ["evidence.evidence_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["causal_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("node_id", "evidence_id"),
    )
    op.create_index(
        op.f("ix_causal_node_evidence_enterprise_id"),
        "causal_node_evidence",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        "ix_causal_node_evidence_evidence",
        "causal_node_evidence",
        ["evidence_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_causal_node_evidence_organization_id"),
        "causal_node_evidence",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "conversion_drafts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("conversion_id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_item_id", sa.String(length=36), nullable=True),
        sa.Column("verified_by", sa.String(length=36), nullable=True),
        sa.Column("runbook_id", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default="draft", nullable=False
        ),
        sa.Column(
            "source_type",
            sa.String(length=20),
            server_default="document",
            nullable=False,
        ),
        sa.Column(
            "document_type",
            sa.String(length=50),
            server_default="runbook",
            nullable=True,
        ),
        sa.Column("domain", sa.String(length=50), nullable=True),
        sa.Column("service", sa.String(length=100), nullable=True),
        sa.Column("severity", sa.String(length=20), nullable=True),
        sa.Column("tags", _TAGS_ARRAY, nullable=True),
        sa.Column(
            "validation_passed", sa.Boolean(), server_default="1", nullable=False
        ),
        sa.Column("validation_errors", sa.JSON(), nullable=True),
        sa.Column("validation_warnings", sa.JSON(), nullable=True),
        sa.Column("quality_score", sa.Numeric(precision=5, scale=1), nullable=True),
        sa.Column("quality_details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "severity IS NULL OR severity IN ('low', 'medium', 'high', 'critical')",
            name="conversion_drafts_severity_check",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'verified', 'discarded')",
            name="conversion_drafts_status_check",
        ),
        sa.ForeignKeyConstraint(
            ["conversion_id"], ["conversion_jobs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_item_id"], ["knowledge_items.item_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["verified_by"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_conversion_drafts_conversion_id"),
        "conversion_drafts",
        ["conversion_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversion_drafts_enterprise_id"),
        "conversion_drafts",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversion_drafts_organization_id"),
        "conversion_drafts",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversion_drafts_tags",
        "conversion_drafts",
        ["tags"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "uq_conversion_drafts_enterprise_runbook_id",
        "conversion_drafts",
        ["enterprise_id", "runbook_id"],
        unique=True,
        sqlite_where=sa.text("status <> 'discarded'"),
        postgresql_where=sa.text("status <> 'discarded'"),
    )
    op.create_table(
        "evidence_need_fulfillment",
        sa.Column("need_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("linked_at_turn", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "linked_at_turn >= 0",
            name="evidence_need_fulfillment_linked_at_turn_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"], ["evidence.evidence_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["need_id"], ["evidence_needs.need_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("need_id", "evidence_id"),
    )
    op.create_index(
        op.f("ix_evidence_need_fulfillment_enterprise_id"),
        "evidence_need_fulfillment",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_need_fulfillment_evidence",
        "evidence_need_fulfillment",
        ["evidence_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_need_fulfillment_organization_id"),
        "evidence_need_fulfillment",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "hypothesis_evidence",
        sa.Column("hypothesis_id", sa.String(length=36), nullable=False),
        sa.Column("evidence_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("relationship_type", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("linked_at_turn", sa.Integer(), nullable=True),
        sa.Column("linked_by", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "relationship_type IN ('supports', 'refutes', 'related')",
            name="hypothesis_evidence_relationship_check",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="hypothesis_evidence_confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"], ["evidence.evidence_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"], ["hypotheses.hypothesis_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["linked_by"], ["users.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("hypothesis_id", "evidence_id"),
    )
    op.create_index(
        op.f("ix_hypothesis_evidence_enterprise_id"),
        "hypothesis_evidence",
        ["enterprise_id"],
        unique=False,
    )
    op.create_index(
        "ix_hypothesis_evidence_evidence",
        "hypothesis_evidence",
        ["evidence_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_hypothesis_evidence_organization_id"),
        "hypothesis_evidence",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "solutions",
        sa.Column("solution_id", sa.String(length=36), nullable=False),
        sa.Column("enterprise_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("hypothesis_id", sa.String(length=36), nullable=True),
        sa.Column("node_id", sa.String(length=36), nullable=True),
        sa.Column("quadrant", sa.String(length=20), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "solution_type",
            sa.String(length=30),
            server_default="other",
            nullable=False,
        ),
        sa.Column(
            "state", sa.String(length=20), server_default="proposed", nullable=False
        ),
        sa.Column("risk_level", sa.String(length=20), nullable=True),
        sa.Column("estimated_effort", sa.String(length=50), nullable=True),
        sa.Column("immediate_action", sa.Text(), nullable=True),
        sa.Column("longterm_fix", sa.Text(), nullable=True),
        sa.Column(
            "implementation_steps",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "commands",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "risks",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=True,
        ),
        sa.Column(
            "proposed_by", sa.String(length=50), server_default="agent", nullable=False
        ),
        sa.Column("applied_by", sa.String(length=50), nullable=True),
        sa.Column("verification_method", sa.String(length=500), nullable=True),
        sa.Column("verification_evidence_id", sa.String(length=36), nullable=True),
        sa.Column("effectiveness", sa.Float(), nullable=True),
        sa.Column("verification_result", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata",
            sa.Text().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "proposed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quadrant IS NULL OR quadrant IN ('remediation', 'defensive_fix', 'mitigation', 'loop_break')",
            name="solutions_quadrant_check",
        ),
        sa.CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high', 'critical')",
            name="solutions_risk_level_check",
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'accepted', 'rejected', 'implemented', 'verified')",
            name="solutions_state_check",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(description)) > 0", name="solutions_description_not_empty"
        ),
        sa.CheckConstraint(
            "effectiveness IS NULL OR (effectiveness >= 0 AND effectiveness <= 1)",
            name="solutions_effectiveness_range",
        ),
        sa.ForeignKeyConstraint(["case_id"], ["cases.case_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["enterprises.enterprise_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["hypothesis_id"], ["hypotheses.hypothesis_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["node_id"], ["causal_nodes.node_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.organization_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["verification_evidence_id"], ["evidence.evidence_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("solution_id"),
    )
    op.create_index(
        op.f("ix_solutions_case_id"), "solutions", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_solutions_enterprise_id"), "solutions", ["enterprise_id"], unique=False
    )
    op.create_index(
        op.f("ix_solutions_hypothesis_id"), "solutions", ["hypothesis_id"], unique=False
    )
    op.create_index(
        op.f("ix_solutions_node_id"), "solutions", ["node_id"], unique=False
    )
    op.create_index(
        op.f("ix_solutions_organization_id"),
        "solutions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(op.f("ix_solutions_state"), "solutions", ["state"], unique=False)

    # PostgreSQL only: SQLite (standalone) is single-tenant and has no RLS.
    if dialect == "postgresql":
        _enable_row_level_security()
    _create_operator_guards(dialect)
    # PostgreSQL only: SQLite (standalone) has no organizations to orphan.
    if dialect == "postgresql":
        op.execute(_CREATE_LAST_ADMIN_FUNCTION)
        op.execute(_CREATE_LAST_ADMIN_TRIGGER)
    _seed(dialect)


def downgrade() -> None:
    """Drop everything. One baseline, no history below it."""
    op.drop_index(op.f("ix_solutions_state"), table_name="solutions")
    op.drop_index(op.f("ix_solutions_organization_id"), table_name="solutions")
    op.drop_index(op.f("ix_solutions_node_id"), table_name="solutions")
    op.drop_index(op.f("ix_solutions_hypothesis_id"), table_name="solutions")
    op.drop_index(op.f("ix_solutions_enterprise_id"), table_name="solutions")
    op.drop_index(op.f("ix_solutions_case_id"), table_name="solutions")
    op.drop_table("solutions")
    op.drop_index(
        op.f("ix_hypothesis_evidence_organization_id"), table_name="hypothesis_evidence"
    )
    op.drop_index("ix_hypothesis_evidence_evidence", table_name="hypothesis_evidence")
    op.drop_index(
        op.f("ix_hypothesis_evidence_enterprise_id"), table_name="hypothesis_evidence"
    )
    op.drop_table("hypothesis_evidence")
    op.drop_index(
        op.f("ix_evidence_need_fulfillment_organization_id"),
        table_name="evidence_need_fulfillment",
    )
    op.drop_index(
        "ix_evidence_need_fulfillment_evidence", table_name="evidence_need_fulfillment"
    )
    op.drop_index(
        op.f("ix_evidence_need_fulfillment_enterprise_id"),
        table_name="evidence_need_fulfillment",
    )
    op.drop_table("evidence_need_fulfillment")
    op.drop_index(
        "uq_conversion_drafts_enterprise_runbook_id",
        table_name="conversion_drafts",
        sqlite_where=sa.text("status <> 'discarded'"),
        postgresql_where=sa.text("status <> 'discarded'"),
    )
    op.drop_index(
        "ix_conversion_drafts_tags",
        table_name="conversion_drafts",
        postgresql_using="gin",
    )
    op.drop_index(
        op.f("ix_conversion_drafts_organization_id"), table_name="conversion_drafts"
    )
    op.drop_index(
        op.f("ix_conversion_drafts_enterprise_id"), table_name="conversion_drafts"
    )
    op.drop_index(
        op.f("ix_conversion_drafts_conversion_id"), table_name="conversion_drafts"
    )
    op.drop_table("conversion_drafts")
    op.drop_index(
        op.f("ix_causal_node_evidence_organization_id"),
        table_name="causal_node_evidence",
    )
    op.drop_index("ix_causal_node_evidence_evidence", table_name="causal_node_evidence")
    op.drop_index(
        op.f("ix_causal_node_evidence_enterprise_id"), table_name="causal_node_evidence"
    )
    op.drop_table("causal_node_evidence")
    op.drop_index(op.f("ix_case_entities_organization_id"), table_name="case_entities")
    op.drop_index(op.f("ix_case_entities_enterprise_id"), table_name="case_entities")
    op.drop_index("idx_case_entities_lookup", table_name="case_entities")
    op.drop_index("idx_case_entities_by_evidence", table_name="case_entities")
    op.drop_table("case_entities")
    op.drop_index(op.f("ix_hypotheses_state"), table_name="hypotheses")
    op.drop_index(op.f("ix_hypotheses_root_node_id"), table_name="hypotheses")
    op.drop_index(op.f("ix_hypotheses_organization_id"), table_name="hypotheses")
    op.drop_index(op.f("ix_hypotheses_enterprise_id"), table_name="hypotheses")
    op.drop_index(op.f("ix_hypotheses_created_by"), table_name="hypotheses")
    op.drop_index(op.f("ix_hypotheses_category"), table_name="hypotheses")
    op.drop_index(op.f("ix_hypotheses_case_id"), table_name="hypotheses")
    op.drop_table("hypotheses")
    op.drop_index("ix_evidence_tags", table_name="evidence", postgresql_using="gin")
    op.drop_index(op.f("ix_evidence_source_file_id"), table_name="evidence")
    op.drop_index(op.f("ix_evidence_organization_id"), table_name="evidence")
    op.drop_index(op.f("ix_evidence_enterprise_id"), table_name="evidence")
    op.drop_index("ix_evidence_coverage", table_name="evidence")
    op.drop_index(op.f("ix_evidence_collected_at_turn"), table_name="evidence")
    op.drop_index(op.f("ix_evidence_category"), table_name="evidence")
    op.drop_index("ix_evidence_case_is_primary", table_name="evidence")
    op.drop_index(op.f("ix_evidence_case_id"), table_name="evidence")
    op.drop_table("evidence")
    op.drop_index("uq_conversion_jobs_live_case_id", table_name="conversion_jobs")
    op.drop_index(op.f("ix_conversion_jobs_user_id"), table_name="conversion_jobs")
    op.drop_index(
        op.f("ix_conversion_jobs_source_file_id"), table_name="conversion_jobs"
    )
    op.drop_index(
        op.f("ix_conversion_jobs_organization_id"), table_name="conversion_jobs"
    )
    op.drop_index(
        op.f("ix_conversion_jobs_enterprise_id"), table_name="conversion_jobs"
    )
    op.drop_index(op.f("ix_conversion_jobs_case_id"), table_name="conversion_jobs")
    op.drop_table("conversion_jobs")
    op.drop_index(op.f("ix_causal_edges_organization_id"), table_name="causal_edges")
    op.drop_index(op.f("ix_causal_edges_enterprise_id"), table_name="causal_edges")
    op.drop_index(op.f("ix_causal_edges_effect_node_id"), table_name="causal_edges")
    op.drop_index(op.f("ix_causal_edges_cause_node_id"), table_name="causal_edges")
    op.drop_index(op.f("ix_causal_edges_case_id"), table_name="causal_edges")
    op.drop_table("causal_edges")
    op.drop_index(op.f("ix_uploaded_files_uploaded_by"), table_name="uploaded_files")
    op.drop_index(
        op.f("ix_uploaded_files_organization_id"), table_name="uploaded_files"
    )
    op.drop_index(op.f("ix_uploaded_files_enterprise_id"), table_name="uploaded_files")
    op.drop_index(op.f("ix_uploaded_files_content_hash"), table_name="uploaded_files")
    op.drop_index(op.f("ix_uploaded_files_case_id"), table_name="uploaded_files")
    op.drop_table("uploaded_files")
    op.drop_index(op.f("ix_reports_organization_id"), table_name="reports")
    op.drop_index(op.f("ix_reports_enterprise_id"), table_name="reports")
    op.drop_index(op.f("ix_reports_case_id"), table_name="reports")
    op.drop_index("idx_reports_type_version", table_name="reports")
    op.drop_table("reports")
    op.drop_index(
        op.f("ix_knowledge_suggestions_status"), table_name="knowledge_suggestions"
    )
    op.drop_index(
        op.f("ix_knowledge_suggestions_pii_scan_status"),
        table_name="knowledge_suggestions",
    )
    op.drop_index(
        op.f("ix_knowledge_suggestions_organization_id"),
        table_name="knowledge_suggestions",
    )
    op.drop_index(
        op.f("ix_knowledge_suggestions_knowledge_item_id"),
        table_name="knowledge_suggestions",
    )
    op.drop_index(
        op.f("ix_knowledge_suggestions_extracted_by"),
        table_name="knowledge_suggestions",
    )
    op.drop_index(
        op.f("ix_knowledge_suggestions_enterprise_id"),
        table_name="knowledge_suggestions",
    )
    op.drop_index(
        op.f("ix_knowledge_suggestions_created_at"), table_name="knowledge_suggestions"
    )
    op.drop_index(
        op.f("ix_knowledge_suggestions_case_id"), table_name="knowledge_suggestions"
    )
    op.drop_table("knowledge_suggestions")
    op.drop_index(
        op.f("ix_investigation_sessions_user_id"), table_name="investigation_sessions"
    )
    op.drop_index(
        op.f("ix_investigation_sessions_state"), table_name="investigation_sessions"
    )
    op.drop_index(
        op.f("ix_investigation_sessions_organization_id"),
        table_name="investigation_sessions",
    )
    op.drop_index(
        op.f("ix_investigation_sessions_enterprise_id"),
        table_name="investigation_sessions",
    )
    op.drop_index(
        op.f("ix_investigation_sessions_created_at"),
        table_name="investigation_sessions",
    )
    op.drop_index(
        op.f("ix_investigation_sessions_case_id"), table_name="investigation_sessions"
    )
    op.drop_table("investigation_sessions")
    op.drop_index(op.f("ix_evidence_needs_state"), table_name="evidence_needs")
    op.drop_index(
        op.f("ix_evidence_needs_organization_id"), table_name="evidence_needs"
    )
    op.drop_index(op.f("ix_evidence_needs_enterprise_id"), table_name="evidence_needs")
    op.drop_index("ix_evidence_needs_case_state", table_name="evidence_needs")
    op.drop_index("ix_evidence_needs_case_purpose", table_name="evidence_needs")
    op.drop_index(op.f("ix_evidence_needs_case_id"), table_name="evidence_needs")
    op.drop_table("evidence_needs")
    op.drop_index(
        "uq_causal_nodes_one_problem_per_case",
        table_name="causal_nodes",
        sqlite_where=sa.text("node_type = 'problem'"),
        postgresql_where=sa.text("node_type = 'problem'"),
    )
    op.drop_index(op.f("ix_causal_nodes_organization_id"), table_name="causal_nodes")
    op.drop_index(op.f("ix_causal_nodes_node_type"), table_name="causal_nodes")
    op.drop_index(op.f("ix_causal_nodes_node_state"), table_name="causal_nodes")
    op.drop_index(op.f("ix_causal_nodes_enterprise_id"), table_name="causal_nodes")
    op.drop_index(op.f("ix_causal_nodes_category"), table_name="causal_nodes")
    op.drop_index(op.f("ix_causal_nodes_case_id"), table_name="causal_nodes")
    op.drop_table("causal_nodes")
    op.drop_index(op.f("ix_case_tags_tag"), table_name="case_tags")
    op.drop_index(op.f("ix_case_tags_organization_id"), table_name="case_tags")
    op.drop_index(op.f("ix_case_tags_enterprise_id"), table_name="case_tags")
    op.drop_index(op.f("ix_case_tags_case_id"), table_name="case_tags")
    op.drop_table("case_tags")
    op.drop_index(op.f("ix_case_messages_organization_id"), table_name="case_messages")
    op.drop_index(op.f("ix_case_messages_enterprise_id"), table_name="case_messages")
    op.drop_index(op.f("ix_case_messages_created_at"), table_name="case_messages")
    op.drop_index("ix_case_messages_case_turn", table_name="case_messages")
    op.drop_index(op.f("ix_case_messages_case_id"), table_name="case_messages")
    op.drop_index("ix_case_messages_case_created", table_name="case_messages")
    op.drop_table("case_messages")
    op.drop_index(
        op.f("ix_case_checkpoints_organization_id"), table_name="case_checkpoints"
    )
    op.drop_index(
        op.f("ix_case_checkpoints_enterprise_id"), table_name="case_checkpoints"
    )
    op.drop_index(op.f("ix_case_checkpoints_created_at"), table_name="case_checkpoints")
    op.drop_index("ix_case_checkpoints_case_turn", table_name="case_checkpoints")
    op.drop_index(op.f("ix_case_checkpoints_case_id"), table_name="case_checkpoints")
    op.drop_table("case_checkpoints")
    op.drop_index(op.f("ix_case_actions_organization_id"), table_name="case_actions")
    op.drop_index(op.f("ix_case_actions_enterprise_id"), table_name="case_actions")
    op.drop_index(op.f("ix_case_actions_case_id"), table_name="case_actions")
    op.drop_table("case_actions")
    op.drop_index("ix_user_audit_log_user_id", table_name="user_audit_log")
    op.drop_index("ix_user_audit_log_organization_id", table_name="user_audit_log")
    op.drop_index(op.f("ix_user_audit_log_event_type"), table_name="user_audit_log")
    op.drop_index(op.f("ix_user_audit_log_enterprise_id"), table_name="user_audit_log")
    op.drop_table("user_audit_log")
    op.drop_index("ix_resource_shares_scope", table_name="resource_shares")
    op.drop_index(
        op.f("ix_resource_shares_organization_id"), table_name="resource_shares"
    )
    op.drop_index(
        op.f("ix_resource_shares_enterprise_id"), table_name="resource_shares"
    )
    op.drop_table("resource_shares")
    op.drop_index(
        op.f("ix_organization_members_enterprise_id"), table_name="organization_members"
    )
    op.drop_index("ix_org_members_role_id", table_name="organization_members")
    op.drop_index("ix_org_members_organization_id", table_name="organization_members")
    op.drop_table("organization_members")
    op.drop_index(
        op.f("ix_knowledge_items_verification_level"), table_name="knowledge_items"
    )
    op.drop_index(
        "ix_knowledge_items_tags", table_name="knowledge_items", postgresql_using="gin"
    )
    op.drop_index(
        op.f("ix_knowledge_items_source_suggestion_id"), table_name="knowledge_items"
    )
    op.drop_index(op.f("ix_knowledge_items_scope"), table_name="knowledge_items")
    op.drop_index(op.f("ix_knowledge_items_owner_id"), table_name="knowledge_items")
    op.drop_index(
        op.f("ix_knowledge_items_organization_id"), table_name="knowledge_items"
    )
    op.drop_index(
        op.f("ix_knowledge_items_last_retrieved_at"), table_name="knowledge_items"
    )
    op.drop_index(op.f("ix_knowledge_items_item_type"), table_name="knowledge_items")
    op.drop_index(op.f("ix_knowledge_items_is_published"), table_name="knowledge_items")
    op.drop_index(
        op.f("ix_knowledge_items_enterprise_id"), table_name="knowledge_items"
    )
    op.drop_index(op.f("ix_knowledge_items_created_at"), table_name="knowledge_items")
    op.drop_index(op.f("ix_knowledge_items_category"), table_name="knowledge_items")
    op.drop_table("knowledge_items")
    op.drop_index(op.f("ix_cases_user_id"), table_name="cases")
    op.drop_index(op.f("ix_cases_state"), table_name="cases")
    op.drop_index(op.f("ix_cases_source"), table_name="cases")
    op.drop_index(op.f("ix_cases_organization_id"), table_name="cases")
    op.drop_index(op.f("ix_cases_last_activity_at"), table_name="cases")
    op.drop_index(op.f("ix_cases_enterprise_id"), table_name="cases")
    op.drop_index(op.f("ix_cases_created_at"), table_name="cases")
    op.drop_index(op.f("ix_cases_closed_at"), table_name="cases")
    op.drop_table("cases")
    op.drop_index("ix_team_members_team_id", table_name="team_members")
    op.drop_table("team_members")
    op.drop_index(op.f("ix_team_invitations_team_id"), table_name="team_invitations")
    op.drop_index(op.f("ix_team_invitations_status"), table_name="team_invitations")
    op.drop_index(
        op.f("ix_team_invitations_enterprise_id"), table_name="team_invitations"
    )
    op.drop_index("ix_team_invitations_email", table_name="team_invitations")
    op.drop_table("team_invitations")
    op.drop_index(
        "ix_organizations_slug_live",
        table_name="organizations",
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_organizations_owner_id"), table_name="organizations")
    op.drop_index(op.f("ix_organizations_enterprise_id"), table_name="organizations")
    op.drop_table("organizations")
    op.drop_index("idx_auth_codes_expires_at", table_name="oauth_authorization_codes")
    op.drop_table("oauth_authorization_codes")
    op.drop_table("config_overrides")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_index("ix_users_is_active", table_name="users")
    op.drop_index(op.f("ix_users_enterprise_id"), table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_index(op.f("ix_turn_usage_enterprise_id"), table_name="turn_usage")
    op.drop_table("turn_usage")
    op.drop_index(op.f("ix_teams_enterprise_id"), table_name="teams")
    op.drop_table("teams")
    op.drop_table("sso_personal_enterprises")
    op.drop_table("sso_org_mappings")
    op.drop_table("role_permissions")
    op.drop_table("roles")
    op.drop_table("permissions")
    op.drop_index(
        "ix_operator_access_grants_target_enterprise",
        table_name="operator_access_grants",
    )
    op.drop_index(
        "ix_operator_access_grants_operator_case", table_name="operator_access_grants"
    )
    op.drop_table("operator_access_grants")
    op.drop_index(
        "ix_operator_access_audit_target_enterprise", table_name="operator_access_audit"
    )
    op.drop_index(
        "ix_operator_access_audit_operator", table_name="operator_access_audit"
    )
    op.drop_index("ix_operator_access_audit_grant", table_name="operator_access_audit")
    op.drop_index(
        op.f("ix_operator_access_audit_created_at"), table_name="operator_access_audit"
    )
    op.drop_index("ix_operator_access_audit_case", table_name="operator_access_audit")
    op.drop_table("operator_access_audit")
    op.drop_index(
        "ix_enterprises_slug_live",
        table_name="enterprises",
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index("ix_enterprises_slug", table_name="enterprises")
    op.drop_index(
        "ix_enterprises_domain_live",
        table_name="enterprises",
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("enterprises")

    # Both engines drop a table's own policies and triggers with it; the
    # standalone plpgsql functions outlive their tables and need naming.
    if op.get_context().dialect.name == "postgresql":
        for function in _PG_FUNCTIONS:
            op.execute(f"DROP FUNCTION IF EXISTS {function}()")
