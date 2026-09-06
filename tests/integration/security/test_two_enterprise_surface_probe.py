"""Adversarial probe: walk the API surfaces as two real ENTERPRISES (ADR-017).

The successor to ``test_two_tenant_surface_probe.py``. That module walks every
tenant-scoped surface as two principals and asserts nothing of one is observable
by the other; it does so with the **organization** as the tenant, because that is
what the schema, the binder and the repositories key on today. ADR-017 moves the
isolation key one tier up: *the enterprise isolates, the organization bills, the
team shares.* This module is the same probe re-aimed at the boundary the ADR
declares, and it is written **before** the boundary exists — Phase 0 of the
campaign, red on main on purpose.

Posture, and everything else this inherits unchanged from its predecessor: the
app connects as a **non-superuser, non-owner role** with the deployed
``faultmaven_app`` grants (a superuser bypasses RLS, so a probe run as the
migration role would measure the application filters alone and call it
isolation); the real ``faultmaven.main`` application over a real PostgreSQL with
the migrations applied and a real ChromaDB; every attack paired with a **positive
control** issued by the party who owns the row; marker strings that name whose
data leaked; every response body scanned for the other party's markers *including
refusals*, because a 403 whose detail echoes a case title has already leaked; and
a route inventory **derived from the live OpenAPI document** rather than
remembered, so a route added tomorrow fails this module until someone classifies
it.

The three arms
--------------
Each arm is a distinct world, and the difference between them is the whole point:
they separate what the database enforces from what the application enforces.

``two_enterprises`` (fixture ``world``, param ``two_enterprises``)
    A in enterprise E_A, B in enterprise E_B. **No organizations exist** — beta
    runs without them (campaign brief D3). Each principal has a team of their
    own. Nothing of B's is observable by A on any surface, and vice versa. This
    is the arm PostgreSQL RLS guards, and it is the only arm in which operator
    confinement, break-glass grants and the user-administration surfaces are
    probed: those key on the enterprise, and inside one enterprise there is no
    operator boundary to test (fixture ``wall_world``).

``same_enterprise_no_team`` (fixture ``world``, param ``same_enterprise_no_team``)
    A and B both in enterprise E, no organizations, a team each, **no common
    team**. Same "nothing crosses" battery. This is the arm RLS no longer guards
    — both rows carry E, so every policy admits both — which makes it the proof
    that the **application share layer alone** holds inside one enterprise.
    ``test_a_tenant_admin_reaches_no_admin_route_at_all`` runs here too: it is
    about the platform role, not the wall.

``shared_team`` (fixture ``shared_world``)
    A and B both in enterprise E. Two organizations under E — X ∋ A, Y ∋ B, with
    ``organization_members`` rows and tokens carrying ``organization_id`` X / Y
    as **billing context only**. One team T ∋ {A, B}. A owns a case shared to T
    and a private one; a knowledge item shared to T and a personal one. B sees
    the shared sibling on every read surface and **nothing** of the private one
    on the same call — the shared row is the positive control for the private
    row's absence. This is the row ADR-017 adds to the matrix and that no probe
    could answer before it: *two organizations inside one enterprise, one shared
    team.* Sharing is read visibility, not ownership, so B can neither un-share,
    re-share nor mutate what A shared.

The contract this probe pins
----------------------------
Phases 1–3 build to these; the probe reads the **live catalog** and the **live
app**, never the ORM models.

Schema (Phase 1, one clean Alembic baseline):

1. ``enterprise_id VARCHAR(36) NOT NULL`` with an FK to ``enterprises`` on every
   RLS-enrolled table except ``team_members``, which is keyed by one hop through
   ``teams.enterprise_id``. The target set is :data:`ENTERPRISE_SCOPED_TABLES`,
   and every table with RLS enabled in the catalog must be in it — so a new
   tenant table cannot appear un-keyed.
2. Every policy references ``current_setting('app.current_enterprise_id', true)``
   and none references ``app.current_org_id``. The ``knowledge_items``
   global-write arms compare against ``STANDALONE_ENTERPRISE_ID``
   (``…0002``), never the organization sentinel ``…0001``.
3. ``teams`` has no ``organization_id``; ``organization_id`` on data rows is
   nullable billing attribution; ``users.enterprise_id`` is NOT NULL with an FK
   to ``enterprises`` — every account is anchored to exactly one enterprise
   (D3), and migration 052's widening dies with the personal-*organization*
   path it was written for (brief D6/D9; retirement is re-targeted in Phase 4
   and never leaves an account unanchored);
   ``operator_access_grants.target_enterprise_id`` NOT NULL and
   ``operator_access_audit.target_enterprise_id``, with no
   ``target_organization_id`` on either; ``enterprises.domain`` (nullable,
   unique); ``team_invitations`` exists; ``sso_personal_orgs`` and
   ``organization_turn_usage`` do not; ``turn_usage(enterprise_id NOT NULL,
   billing_subject_kind, billing_subject_id, usage_date, turn_count)`` does, with
   its primary key on the three subject/date columns.

Binder and tokens (Phase 2):

4. ``faultmaven.config.tenant_context`` exports ``set_current_enterprise_id``,
   ``get_current_enterprise_id``, ``get_current_tenant_id`` and the contextvar
   ``_current_enterprise_id``; the session GUC is ``app.current_enterprise_id``.
   The organization-keyed names are gone.
5. Access tokens carry an ``enterprise_id`` claim (isolation) and an optional
   ``organization_id`` claim (billing; absent in arms 1 and 3). A token without
   an ``enterprise_id`` claim is refused — there is no derive-from-the-user-row
   fallback (ADR-017, "No data migration"). Tokens here are forged with
   ``tests.utils.sign_claims_for`` and the claim set the live generators emit
   **plus** ``enterprise_id``; ``forge_access_token`` is deliberately not
   extended, because Phase 2 rewrites it and this module must not depend on it.

Repositories (Phase 3): the arm-2 and arm-3 assertions are the gate.

The phase ratchet
-----------------
``world``, ``wall_world`` and ``shared_world`` all call
:func:`_require_enterprise_schema` **first**, before importing a single Phase-2
name, and it ``pytest.fail``s with the catalog's own account of what is missing.
``test_the_schema_carries_the_enterprise_key`` makes the identical assertion as a
test, so the reason for the red is a FAIL row rather than only a fixture error.

Every test that cannot pass before its phase lands carries a **strict** xfail —
:data:`phase1_pending` on the schema test, :data:`phase3_pending` on every
world-backed test. Strict is what makes it a ratchet rather than a mute button:
the moment a phase makes one of these pass, it turns XPASS → FAIL, and that
phase's PR must remove the marker for exactly the tests it turned green. A
fixture ``pytest.fail`` under an xfail marker is reported *xfailed*, not error
(verified empirically, pytest ``_pytest/skipping.py`` applies the marker to any
phase's exception, not only the call phase), so a run on main has zero ``error``,
zero ``failed`` and zero ``skipped`` rows — which matters because CI's
``-m postgres`` gate greps for ``skipped`` and ``needs: test-postgres`` blocks
every other PR.

Two tests are deliberately **unmarked** because they pass on main today:
``test_every_tenant_scoped_route_is_in_the_inventory`` and
``test_the_inventory_states_a_reason_for_every_unprobed_surface``. They read the
OpenAPI document and :data:`SURFACE_INVENTORY` and nothing else, and the
inventory is carried over from the predecessor entry for entry — so when Phase 1
deletes that module, no coverage decision is lost with it.

Shown to fail against a broken boundary
---------------------------------------
Not yet, and the honest statement of why: **no mutation can be run on main,
because the boundary this module probes does not exist**. The predecessor's
mutation table was earned by breaking a guard and watching a named assertion go
red; here every world-backed assertion is red already, for the reason the schema
check prints, so a mutation would change nothing observable. The table below is
left for the phase that first turns these green to fill in, and it is not
optional: a probe that has only ever been red is exactly as uninformative as one
that has only ever been green.

===============================================  ==========================
Mutation (reverted from an in-memory copy)       Went red
===============================================  ==========================
*(Phase 3 fills this in, on the revision that    *(to be recorded)*
turns the world-backed tests green: the
enterprise arm of the case read filter, the
share allowlist's enterprise match, the KB
inventory clause, ``OperatorUserScope``'s
enterprise confinement, and the binder's
refusal of a claim-less token.)*
===============================================  ==========================
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.config.constants import (
    STANDALONE_ENTERPRISE_ID,
    STANDALONE_ORG_ID,
)
from tests.integration.security.conftest import (
    create_limited_role,
    drop_limited_role,
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

#: The schema test's ratchet. Removed by the Phase-1 PR, which is the PR that
#: makes it pass.
phase1_pending = pytest.mark.xfail(
    strict=True,
    reason=(
        "ADR-017 Phase 1 (schema): no table carries enterprise_id, every policy "
        "is keyed on app.current_org_id, and the tables the ADR retires are "
        "still present. Remove this marker in the Phase-1 PR."
    ),
)

#: Every world-backed test's ratchet. The world fixtures cannot even be built
#: until Phase 1 lands, and the assertions inside them cannot hold until the
#: binder (Phase 2) and the repositories (Phase 3) key on the enterprise. Strict,
#: so the phase that turns one of these green is *forced* to unmark it.
phase3_pending = pytest.mark.xfail(
    strict=True,
    reason=(
        "ADR-017 Phase 3 (repositories & sharing): the isolation key is still "
        "the organization, so the world fixtures refuse to build (Phase 1) and "
        "the surfaces below are not enterprise-scoped (Phases 2-3). Remove this "
        "marker for exactly the tests the landing phase turns green."
    ),
)


# =============================================================================
# The contract, as constants the schema check reads
# =============================================================================

#: Every table that must carry the enterprise key once Phase 1 lands.
#:
#: Derived, not invented: it is the RLS-enrolled set on main today (29 tables)
#: with ``organization_turn_usage`` replaced by ``turn_usage`` — the turn ledger
#: re-keyed on a billing subject (ADR-017 D5, "what the inventory settled"). It
#: is pinned here AND cross-checked against the catalog's own RLS enrolment
#: below, so a new tenant table cannot appear without an enterprise key.
#: ``team_invitations`` joins it because Phase 1 introduces it as tenant data
#: (D4's consent record): who was invited into which team is exactly the sort of
#: row the wall exists to keep on one side of it.
ENTERPRISE_SCOPED_TABLES = frozenset(
    {
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
        "knowledge_items",
        "knowledge_suggestions",
        "organization_members",
        "organizations",
        "reports",
        "resource_shares",
        "solutions",
        "team_invitations",
        "team_members",
        "teams",
        "turn_usage",
        "uploaded_files",
        "user_audit_log",
    }
)

#: The one table that carries no ``enterprise_id`` column of its own. Its policy
#: reaches the key by a single hop through ``teams.enterprise_id`` (ADR-017's
#: rule for the membership row), so "keyed" for it means the hop's target
#: exists and the policy body performs the hop.
ENTERPRISE_KEY_BY_HOP = "team_members"

#: Tables whose ``organization_id``, where it survives at all, becomes nullable
#: billing attribution (brief D2). ``organizations`` (the column is its primary
#: key) and ``organization_members`` (the billing roster's whole subject) are
#: excluded; ``teams`` is excluded because it loses the column outright.
NULLABLE_BILLING_ORG_TABLES = frozenset(
    ENTERPRISE_SCOPED_TABLES - {"organizations", "organization_members", "teams"}
)

#: The session GUC every policy must read, and the one none may.
TENANT_GUC = "app.current_enterprise_id"
RETIRED_GUC = "app.current_org_id"

#: The organization sentinel. ``knowledge_items``' global-write arms compare the
#: bound tenant against a sentinel; under ADR-017 that comparison moves to
#: ``STANDALONE_ENTERPRISE_ID`` and this value must appear in no policy body.
#: Taken from ``config.constants`` rather than spelled out, so a probe asserting
#: the sentinel is gone cannot go on passing after the constant is renumbered.
RETIRED_ORG_SENTINEL = STANDALONE_ORG_ID

#: Deleted, not deprecated (owner rule, 2026-09-06).
RETIRED_TABLES = ("organization_turn_usage", "sso_personal_orgs")

#: Introduced by the Phase-1 baseline. ``team_invitations`` is NOT here: it
#: carries tenant data (who was invited into which team) and therefore belongs
#: to :data:`ENTERPRISE_SCOPED_TABLES`, which already demands that it exist, be
#: keyed and be enrolled in RLS. Listing it in both would report its absence
#: twice and, worse, would let a Phase-1 baseline that created it *unkeyed*
#: satisfy this list.
REQUIRED_TABLES = ("turn_usage",)

#: The turn ledger's new subject key (ADR-017 D5): the organization when the
#: account has one, the account itself otherwise.
TURN_USAGE_COLUMNS = (
    "enterprise_id",
    "billing_subject_kind",
    "billing_subject_id",
    "usage_date",
    "turn_count",
)
TURN_USAGE_PRIMARY_KEY = frozenset(
    {"billing_subject_kind", "billing_subject_id", "usage_date"}
)


# =============================================================================
# The schema check — the live catalog's own account of what is missing
# =============================================================================
#
# Read from ``information_schema`` / ``pg_catalog``, never from the ORM models.
# The models are the application's belief about the schema; a probe that trusted
# them would pass against a database no migration had reached.

_ENTERPRISE_ID_COLUMN = "enterprise_id"
_ENTERPRISE_ID_LENGTH = 36

#: Computed once per database url. The schema cannot change inside one pytest
#: session — nothing here runs a migration — and the check is consulted by every
#: world fixture, i.e. roughly a hundred times per run. Caching the *facts*
#: changes no verdict: the failure is raised from the cached facts every time.
_SCHEMA_GAPS: dict[str, tuple[str, ...]] = {}


async def _catalog(conn) -> SimpleNamespace:
    """Everything the contract asks about, in one pass over the catalog."""
    tables = set(
        (
            await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            )
        ).scalars()
    )
    columns = {
        (table, column): (data_type, length, nullable == "YES")
        for table, column, data_type, length, nullable in (
            await conn.execute(
                text(
                    "SELECT table_name, column_name, data_type, "
                    "character_maximum_length, is_nullable "
                    "FROM information_schema.columns WHERE table_schema = 'public'"
                )
            )
        ).all()
    }
    # A constraint name is unique per TABLE, not per schema, so the joins are
    # qualified by ``table_name`` as well as by schema. Without that, two tables
    # that both name their FK ``fk_enterprise`` cross-credit each other and a
    # table with no FK at all is reported as having one — the check would then
    # pass on exactly the schema it exists to reject.
    enterprise_fks = {
        (table, column)
        for table, column in (
            await conn.execute(
                text(
                    "SELECT tc.table_name, kcu.column_name "
                    "FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu "
                    "  ON kcu.constraint_name = tc.constraint_name "
                    " AND kcu.constraint_schema = tc.constraint_schema "
                    " AND kcu.table_name = tc.table_name "
                    "JOIN information_schema.constraint_column_usage ccu "
                    "  ON ccu.constraint_name = tc.constraint_name "
                    " AND ccu.constraint_schema = tc.constraint_schema "
                    "WHERE tc.constraint_type = 'FOREIGN KEY' "
                    "  AND tc.table_schema = 'public' "
                    "  AND ccu.table_schema = 'public' "
                    "  AND ccu.table_name = 'enterprises'"
                )
            )
        ).all()
    }
    rls_tables = set(
        (
            await conn.execute(
                text(
                    "SELECT c.relname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND c.relrowsecurity"
                )
            )
        ).scalars()
    )
    # ``cmd`` is carried alongside the body because the body cannot be asked
    # which command it guards: ``qual`` and ``with_check`` are boolean
    # expressions, so a filter looking for the word "INSERT" in them matches
    # nothing and silently admits the read policy to a write-arm verdict.
    # ``pg_policies.cmd`` reports one of SELECT | INSERT | UPDATE | DELETE | ALL.
    policies = [
        (table, name, cmd, f"{qual or ''} {check or ''}")
        for table, name, cmd, qual, check in (
            await conn.execute(
                text(
                    "SELECT tablename, policyname, cmd, qual, with_check "
                    "FROM pg_policies WHERE schemaname = 'public' "
                    "ORDER BY tablename, policyname"
                )
            )
        ).all()
    ]
    turn_usage_pk = set(
        (
            await conn.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                    " AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = to_regclass('public.turn_usage') "
                    "  AND i.indisprimary"
                )
            )
        ).scalars()
    )
    domain_unique = bool(
        (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                    " AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = to_regclass('public.enterprises') "
                    "  AND i.indisunique AND a.attname = 'domain'"
                )
            )
        ).first()
    )
    return SimpleNamespace(
        tables=tables,
        columns=columns,
        enterprise_fks=enterprise_fks,
        rls_tables=rls_tables,
        policies=policies,
        turn_usage_pk=turn_usage_pk,
        domain_unique=domain_unique,
    )


def _enterprise_key_gaps(cat) -> list[str]:
    """One verdict per table in :data:`ENTERPRISE_SCOPED_TABLES`."""
    gaps: list[str] = []
    for table in sorted(ENTERPRISE_SCOPED_TABLES):
        if table == ENTERPRISE_KEY_BY_HOP:
            # No column of its own: the key arrives through ``teams``. If that
            # hop's target is missing, the membership row is unkeyed too.
            spec = cat.columns.get(("teams", _ENTERPRISE_ID_COLUMN))
            if spec is None:
                gaps.append(
                    f"{table}: keyed by one hop through teams.enterprise_id, "
                    "which does not exist"
                )
            elif spec[2]:
                gaps.append(f"{table}: the hop target teams.enterprise_id is nullable")
            continue
        if table not in cat.tables:
            gaps.append(f"{table}: the table does not exist")
            continue
        spec = cat.columns.get((table, _ENTERPRISE_ID_COLUMN))
        if spec is None:
            gaps.append(f"{table}: no enterprise_id column")
            continue
        data_type, length, nullable = spec
        problems = []
        if data_type != "character varying" or length != _ENTERPRISE_ID_LENGTH:
            problems.append(f"is {data_type}({length}), not varchar(36)")
        if nullable:
            problems.append("is nullable")
        if (table, _ENTERPRISE_ID_COLUMN) not in cat.enterprise_fks:
            problems.append("has no FK to enterprises")
        if problems:
            gaps.append(f"{table}.enterprise_id: " + ", ".join(problems))
    return gaps


def _policy_gaps(cat) -> tuple[list[str], list[str], list[str]]:
    """(still org-keyed, not enterprise-keyed, sentinel/hop defects)."""
    org_keyed = [
        f"{table}.{name}"
        for table, name, _cmd, body in cat.policies
        if RETIRED_GUC in body
    ]
    not_enterprise_keyed = [
        f"{table}.{name}"
        for table, name, _cmd, body in cat.policies
        if TENANT_GUC not in body
    ]
    defects: list[str] = []
    for table, name, _cmd, body in cat.policies:
        if RETIRED_ORG_SENTINEL in body:
            defects.append(
                f"{table}.{name}: compares the bound tenant against the "
                f"ORGANIZATION sentinel {RETIRED_ORG_SENTINEL}; ADR-017 keys "
                f"the global-write arm on STANDALONE_ENTERPRISE_ID "
                f"({STANDALONE_ENTERPRISE_ID})"
            )
        # The literal ``teams.enterprise_id``, not "teams" and "enterprise_id"
        # separately: the GUC is itself named ``app.current_enterprise_id``, so
        # the looser spelling was satisfied by any policy that read the GUC and
        # mentioned teams — which the org-keyed policy on main already does.
        if table == ENTERPRISE_KEY_BY_HOP and "teams.enterprise_id" not in body:
            defects.append(f"{table}.{name}: does not hop through teams.enterprise_id")
    # The WRITE arms only. Migration 033 split ``knowledge_items`` into four
    # per-command policies, and the read arm grants every tenant the global tier
    # unconditionally — so a sentinel named only there would satisfy nothing
    # about who may WRITE a global row, which is the invariant fm#850 protects.
    # Selected by ``cmd`` rather than by looking for a command keyword in the
    # body, because the body never contains one.
    knowledge_write_arms = [
        body
        for table, _name, cmd, body in cat.policies
        if table == "knowledge_items" and cmd != "SELECT"
    ]
    if knowledge_write_arms and not any(
        STANDALONE_ENTERPRISE_ID in body for body in knowledge_write_arms
    ):
        defects.append(
            "knowledge_items: no policy names STANDALONE_ENTERPRISE_ID "
            f"({STANDALONE_ENTERPRISE_ID}), so the global-write arm is not "
            "keyed on the standalone enterprise"
        )
    return org_keyed, not_enterprise_keyed, defects


def _structural_gaps(cat) -> list[str]:
    """Item 3 of the contract: what must exist, what must be gone, what shape."""
    gaps: list[str] = []

    if ("teams", "organization_id") in cat.columns:
        gaps.append(
            "teams.organization_id is still present; a team is parented by its "
            "enterprise and may span organizations (ADR-017 D4)"
        )
    still_not_null = sorted(
        table
        for table in NULLABLE_BILLING_ORG_TABLES
        if (table, "organization_id") in cat.columns
        and not cat.columns[(table, "organization_id")][2]
    )
    for table in still_not_null:
        gaps.append(
            f"{table}.organization_id is NOT NULL; billing attribution is "
            "nullable because an account may be in no organization (D2/D5)"
        )

    for table in RETIRED_TABLES:
        if table in cat.tables:
            gaps.append(f"{table} still exists; ADR-017 deletes it")
    for table in REQUIRED_TABLES:
        if table not in cat.tables:
            gaps.append(f"{table} does not exist")

    for table in ("operator_access_grants", "operator_access_audit"):
        if (table, "target_organization_id") in cat.columns:
            gaps.append(
                f"{table}.target_organization_id is still present; operator "
                "confinement and break-glass key on the enterprise (D2/ADR-012)"
            )
        spec = cat.columns.get((table, "target_enterprise_id"))
        if spec is None:
            gaps.append(f"{table}.target_enterprise_id does not exist")
        elif table == "operator_access_grants" and spec[2]:
            gaps.append(
                "operator_access_grants.target_enterprise_id is nullable; a "
                "grant with no enterprise is a grant over everything"
            )

    users_enterprise = cat.columns.get(("users", "enterprise_id"))
    if users_enterprise is None:
        gaps.append("users.enterprise_id does not exist")
    else:
        if users_enterprise[2]:
            gaps.append(
                "users.enterprise_id is nullable; every account is anchored to "
                "exactly one enterprise (ADR-017 D3); migration 052's widening "
                "dies with the personal-organization path"
            )
        if ("users", "enterprise_id") not in cat.enterprise_fks:
            gaps.append("users.enterprise_id has no FK to enterprises")

    domain = cat.columns.get(("enterprises", "domain"))
    if domain is None:
        gaps.append(
            "enterprises.domain does not exist; sign-up derives the enterprise "
            "from the verified email domain (D3)"
        )
    else:
        if not domain[2]:
            gaps.append(
                "enterprises.domain is NOT NULL; a personal enterprise has no "
                "domain (D3)"
            )
        if not cat.domain_unique:
            gaps.append("enterprises.domain carries no unique index")

    if "turn_usage" in cat.tables:
        present = {c for t, c in cat.columns if t == "turn_usage"}
        missing = [c for c in TURN_USAGE_COLUMNS if c not in present]
        if missing:
            gaps.append(f"turn_usage lacks {', '.join(missing)}")
        if cat.turn_usage_pk != TURN_USAGE_PRIMARY_KEY:
            gaps.append(
                "turn_usage's primary key is "
                f"{sorted(cat.turn_usage_pk) or 'absent'}, not "
                f"{sorted(TURN_USAGE_PRIMARY_KEY)}"
            )

    # Both directions, because either one alone can be satisfied by a schema
    # nobody would accept. RLS-enabled-but-unlisted catches a new tenant table
    # appearing without an enterprise key; listed-but-unenrolled catches the
    # mirror — a table that carries the key and is not actually protected by it,
    # which is the shape a baseline written column-first produces.
    #
    # ``operator_access_grants`` and ``operator_access_audit`` are deliberately
    # NOT in the scoped set and deliberately outside RLS: break-glass is a
    # cross-tenant mechanism (ADR-012 D9), and a grant row scoped to the tenant
    # it grants access to could not be read by the operator who needs it. So an
    # enterprise-keyed *column* on them is required (item 3 above) while RLS
    # enrolment is not — and if a future baseline enrols them anyway, the first
    # loop below flags it, which is the correct outcome rather than a false
    # alarm: enrolling them would silently break the audited escape hatch.
    for table in sorted(cat.rls_tables - ENTERPRISE_SCOPED_TABLES):
        gaps.append(
            f"{table} has RLS enabled but is not in ENTERPRISE_SCOPED_TABLES; "
            "a tenant table must not appear un-keyed"
        )
    for table in sorted(ENTERPRISE_SCOPED_TABLES - cat.rls_tables):
        absent = "" if table in cat.tables else " (the table does not exist)"
        gaps.append(f"{table} has no row-level security{absent}")

    # Per TABLE, where the policy list above is per POLICY. A table carrying
    # enterprise_id with no policy at all has nothing to appear in that list, so
    # it would pass every other check here while being readable by everyone.
    enterprise_keyed_tables = {
        table for table, _name, _cmd, body in cat.policies if TENANT_GUC in body
    }
    for table in sorted(ENTERPRISE_SCOPED_TABLES - enterprise_keyed_tables):
        gaps.append(f"{table} has no policy keyed on {TENANT_GUC}")
    return gaps


async def _compute_schema_gaps(superuser_url: str) -> tuple[str, ...]:
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.connect() as conn:
            cat = await _catalog(conn)
    finally:
        await engine.dispose()

    unkeyed = _enterprise_key_gaps(cat)
    org_keyed, not_enterprise_keyed, policy_defects = _policy_gaps(cat)
    structural = _structural_gaps(cat)

    if not (
        unkeyed or org_keyed or not_enterprise_keyed or policy_defects or structural
    ):
        return ()

    report = [
        "ADR-017 Phase 1 has not landed: the schema does not carry the enterprise key.",
        f"  {len(unkeyed)} of {len(ENTERPRISE_SCOPED_TABLES)} tenant tables lack "
        f"enterprise_id",
        f"  {len(org_keyed)} policies keyed on {RETIRED_GUC}; "
        f"{len(cat.policies) - len(not_enterprise_keyed)} of {len(cat.policies)} "
        f"keyed on {TENANT_GUC}",
        f"  {len(structural)} structural gaps (retired tables/columns still "
        "present, required ones absent)",
        "",
    ]
    for heading, lines in (
        ("tables lacking the enterprise key", unkeyed),
        (f"policies still keyed on {RETIRED_GUC}", org_keyed),
        (f"policies not keyed on {TENANT_GUC}", not_enterprise_keyed),
        (
            "policy bodies keyed on the wrong sentinel or missing the hop",
            policy_defects,
        ),
        ("structure", structural),
    ):
        if lines:
            report.append(f"{heading} ({len(lines)}):")
            report.extend(f"  - {line}" for line in lines)
            report.append("")
    return tuple(report)


async def _require_enterprise_schema(superuser_url: str) -> None:
    """Refuse to build a world the schema cannot represent.

    Called FIRST by every world fixture, before a single Phase-2 name is
    imported, so the failure a run on main reports is the missing schema rather
    than an ``ImportError`` for ``set_current_enterprise_id`` — a message that
    would name the symptom and hide the cause.
    """
    if superuser_url not in _SCHEMA_GAPS:
        _SCHEMA_GAPS[superuser_url] = await _compute_schema_gaps(superuser_url)
    gaps = _SCHEMA_GAPS[superuser_url]
    if gaps:
        pytest.fail("\n".join(gaps), pytrace=False)


#: What ``config.tenant_context`` must export once Phase 2 lands. The contextvar
#: is in the list because the seeding helpers bind through it directly, exactly
#: as the request front door will.
PHASE2_BINDER_NAMES = (
    "set_current_enterprise_id",
    "get_current_enterprise_id",
    "get_current_tenant_id",
    "_current_enterprise_id",
)


def _require_enterprise_binder() -> None:
    """Refuse to build a world whose binder does not exist yet, BY NAME.

    Runs immediately after the schema gate and before any Phase-2 import. Without
    it the first such import raises ``ImportError: cannot import name
    '_current_enterprise_id'`` from inside a seeding helper — a message that names
    a symptom four frames from the cause, and one that a later rename would turn
    into a fresh mystery for whoever reads the run. Here the reason is a sentence
    and the missing names are listed.

    It also draws the line the ratchet needs: after Phase 1 the schema gate goes
    quiet, and this is what then says which phase the world is still waiting on.
    """
    import faultmaven.config.tenant_context as tenant_context

    missing = [
        name for name in PHASE2_BINDER_NAMES if not hasattr(tenant_context, name)
    ]
    if missing:
        pytest.fail(
            "ADR-017 Phase 2 (binder) has not landed: "
            f"faultmaven.config.tenant_context exports none of {missing}. "
            "The isolation key is still the organization, so nothing here can "
            "bind an enterprise the way a request will.",
            pytrace=False,
        )


# =============================================================================
# The parties, and the strings that name them
# =============================================================================
#
# Ids are generated per session rather than fixed, so a run that leaves rows
# behind cannot make the next run's "A sees nothing of B" pass by looking at its
# own leftovers.

_RUN = uuid.uuid4().hex[:8]

#: Markers planted in every B-owned row (arms 1 and 3). An assertion names the
#: party whose secret must not appear, so a failure says WHOSE data leaked.
SECRET_B = f"BETA-SECRET-{_RUN}"
SECRET_B_TITLE = f"{SECRET_B}-acquisition-outage"
SECRET_B_TRANSCRIPT = f"{SECRET_B}-transcript-line"
SECRET_B_EVIDENCE = f"{SECRET_B}-payroll-dsn"
SECRET_B_FILE = f"{SECRET_B}-oncall-rota.log"
SECRET_B_REPORT = f"{SECRET_B}-postmortem"
SECRET_B_KB_PERSONAL = f"{SECRET_B}-runbook-personal"
SECRET_B_KB_TEAM = f"{SECRET_B}-runbook-team"
SECRET_B_DRAFT = f"{SECRET_B}-conversion-draft"
SECRET_B_SUGGESTION = f"{SECRET_B}-suggestion"

#: A's own row, so a control can show the same call working for A.
SECRET_A = f"ALPHA-OWN-{_RUN}"

#: Everything that must never appear in a body A receives, in arms 1 and 3.
#: Applied to EVERY attack response, including refusals: a 403 whose detail
#: echoes the title is a leak, and so is a 500 traceback carrying the row.
B_MARKERS = (
    SECRET_B_TITLE,
    SECRET_B_TRANSCRIPT,
    SECRET_B_EVIDENCE,
    SECRET_B_FILE,
    SECRET_B_REPORT,
    SECRET_B_KB_PERSONAL,
    SECRET_B_KB_TEAM,
    SECRET_B_DRAFT,
    SECRET_B_SUGGESTION,
)

#: Arm 2 splits A's content in two. The SHARED half is what B is *supposed* to
#: see — it is the positive control for the PRIVATE half's absence on the same
#: call, by the same actor. Without that pairing "B sees nothing private" would
#: pass against a share that never worked.
SHARED = f"TEAM-SHARED-{_RUN}"
SHARED_TITLE = f"{SHARED}-payments-outage"
SHARED_TRANSCRIPT = f"{SHARED}-transcript-line"
SHARED_KB = f"{SHARED}-runbook-team"

PRIVATE = f"OWNER-ONLY-{_RUN}"
PRIVATE_TITLE = f"{PRIVATE}-severance-review"
PRIVATE_TRANSCRIPT = f"{PRIVATE}-transcript-line"
PRIVATE_EVIDENCE = f"{PRIVATE}-payroll-dsn"
PRIVATE_FILE = f"{PRIVATE}-oncall-rota.log"
PRIVATE_REPORT = f"{PRIVATE}-postmortem"
PRIVATE_KB = f"{PRIVATE}-runbook-personal"

#: Everything of A's that a team share does NOT carry, and that must therefore
#: never appear in a body B receives in arm 2.
PRIVATE_MARKERS = (
    PRIVATE_TITLE,
    PRIVATE_TRANSCRIPT,
    PRIVATE_EVIDENCE,
    PRIVATE_FILE,
    PRIVATE_REPORT,
    PRIVATE_KB,
)

#: The signing key the probe's ``AuthService`` verifies with. Local (HS256)
#: mode: the tokens here are forged directly, so a mint path is not needed.
_JWT_SECRET = "two-enterprise-probe-secret-padded-to-32-bytes"

#: Every key the two ``POST /cases/search`` injection cases put in the body,
#: spelled once so :func:`test_the_search_injection_names_only_real_request_fields`
#: can check it against the live request model. A body field pydantic does not
#: declare is silently DROPPED (``CaseSearchRequest`` takes the default
#: ``extra="ignore"``), so an injection naming one asserts nothing at all — which
#: is what an ``enterprise_id`` in this body would have been, and why it is not
#: here: the enterprise reaches the endpoint through the verified claim only, and
#: there is no body field for it to be honoured from.
INJECTED_SEARCH_KEYS = ("query", "user_id", "organization_id", "team_id", "limit")


def _search_injection_body(*, query, user_id, organization_id, team_id):
    """The injection body. Its keys are exactly :data:`INJECTED_SEARCH_KEYS`."""
    body = {
        "query": query,
        "user_id": user_id,
        "organization_id": organization_id,
        "team_id": team_id,
        "limit": 50,
    }
    assert set(body) == set(INJECTED_SEARCH_KEYS), (
        "the injection body and INJECTED_SEARCH_KEYS have drifted, so the guard "
        "that checks them against CaseSearchRequest is checking the wrong keys"
    )
    return body


#: Statuses that count as "the surface refused". 401 is absent on purpose: the
#: attacker holds a VALID token, so a 401 would mean the probe failed to
#: authenticate and every assertion below it would be vacuous.
REFUSED = frozenset({403, 404, 409, 422})


# =============================================================================
# Environment: a limited role, and the app wired onto it
# =============================================================================

_LIMITED_ROLE = f"fm_2e_probe_{uuid.uuid4().hex[:8]}"
_LIMITED_PW = "fm_2e_probe_pw"

#: Environment the probe app is built under. Restored wholesale in teardown —
#: the ``-m postgres`` lane runs this module inside the same session as
#: ``test_rls_tenant_isolation.py``, which reads ``DATABASE_URL`` expecting the
#: SUPERUSER url. Leaking the limited-role url into that module would make it
#: measure RLS as the wrong role and quietly stop proving anything.
_PROBE_ENV_KEYS = (
    "DATABASE_URL",
    "DEPLOYMENT_MODE",
    "TENANT_PROVIDER",
    "AUTH_MODE",
    "JWT_SECRET_KEY",
    "SKIP_SERVICE_CHECKS",
    "OAUTH_ENABLED",
    "ENVIRONMENT",
)


def _fresh_chroma():
    """An in-process ChromaDB with PINNED settings and an EMPTY KB collection.

    chromadb caches one System per identifier and refuses a second client whose
    ``Settings`` differ, so an unpinned ``EphemeralClient`` here would sometimes
    be handed a store a sibling module seeded. The explicit drop is what keeps
    this module's corpus a function of its own fixture rather than of collection
    order.
    """
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KB_COLLECTION,
    )

    client = chromadb.EphemeralClient(
        settings=ChromaSettings(
            anonymized_telemetry=False,
            allow_reset=False,
            environment="",
            is_persistent=False,
        )
    )
    try:
        client.delete_collection(KB_COLLECTION)
    except Exception:  # noqa: BLE001 - absent on the first client of a session
        pass
    return client


class _Tripwire:
    """A service that fails loudly the moment anything touches it.

    Some routes resolve a heavyweight collaborator (the milestone engine, the
    LLM router) as a FastAPI dependency, before their own case check runs. With
    the slot empty the dependency 503s and the case check never executes, so the
    probe would score "refused" without the guard having been consulted — the
    exact vacuous green this module exists to avoid.

    Filling the slot with this object makes the two outcomes distinguishable: if
    the case gate holds, the request is refused before anything is read off it;
    if the gate is missing, the first attribute access raises and the response is
    a 500 that no ``assert_refused`` accepts. It is a tripwire, not a stand-in.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        raise AssertionError(
            f"the request reached {self._name}.{attribute} — the case-access "
            "check did not refuse it first"
        )


def _wire_services(app, chroma) -> None:
    """Put the REAL services on ``app.state``, over the limited-role engine.

    Not doubles. A double would answer from whatever the test seeded into it,
    which is exactly the assumption an isolation probe exists to test — the
    question is what the production read paths do when a foreign id reaches them.

    The one stand-in is ``ConversionService``'s LLM router: it is consulted only
    on the *generation* path, which this module never drives, and wiring a live
    provider would make a security gate depend on an API key.

    Written against the names that exist on main, deliberately: this fixture must
    build for the two inventory tests to run, and those are the only assertions
    here that pass today. Phase 2 renames some of these collaborators; the phase
    that renames them updates this function, exactly as it updates the module it
    supersedes.
    """
    from faultmaven.config.settings import get_settings
    from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )
    from faultmaven.infrastructure.observability.tracing import OpikTracer
    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.infrastructure.persistence.sessionless_operator_audit_repository import (  # noqa: E501
        SessionlessOperatorAuditRepository,
    )
    from faultmaven.infrastructure.persistence.sessionless_operator_grant_repository import (  # noqa: E501
        SessionlessOperatorGrantRepository,
    )
    from faultmaven.infrastructure.persistence.sessionless_organization_repository import (  # noqa: E501
        SessionlessOrganizationRepository,
    )
    from faultmaven.infrastructure.persistence.sessionless_share_repository import (
        SessionlessShareRepository,
    )
    from faultmaven.infrastructure.persistence.sessionless_team_repository import (
        SessionlessTeamRepository,
    )
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )
    from faultmaven.infrastructure.security.redaction import DataSanitizer
    from faultmaven.modules.auth.domain.services.auth_service import AuthService
    from faultmaven.modules.auth.domain.services.auth_session_service import (
        AuthSessionService,
    )
    from faultmaven.modules.auth.domain.services.team_service import TeamService
    from faultmaven.modules.auth.domain.services.user_service import UserService
    from faultmaven.modules.case.domain.services.case_service import CaseService
    from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
        SessionlessCaseRepository,
    )
    from faultmaven.modules.knowledge.domain.services.conversion_service import (
        ConversionService,
    )
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )
    from faultmaven.modules.knowledge.domain.services.suggestion_service import (
        SuggestionService,
    )
    from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
        DatabaseSuggestionRepository,
    )
    from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
    from tests.utils import InMemoryRevocationStore

    settings = get_settings()

    case_repository = SessionlessCaseRepository()
    share_repository = SessionlessShareRepository()
    team_service = TeamService(SessionlessTeamRepository())
    organization_repository = SessionlessOrganizationRepository()
    sanitizer = DataSanitizer(settings=settings)
    tracer = OpikTracer(settings=settings)

    knowledge_service = KnowledgeService(
        knowledge_ingester=None,
        sanitizer=sanitizer,
        tracer=tracer,
        vector_store=KnowledgeVectorStore(chroma),
        redis_client=None,
        settings=settings,
        llm_provider=None,
        db_session_factory=get_db_session,
        share_repository=share_repository,
    )

    auth_service = AuthService(revocation_store=InMemoryRevocationStore())
    app.state.auth_service = auth_service
    app.state.user_service = UserService(
        user_repo=SessionlessUserRepository(), auth_service=auth_service
    )
    app.state.user_store = DatabaseUserStore(SessionlessUserRepository())
    app.state.case_service = CaseService(
        case_repository=case_repository,
        settings=settings,
        team_service=team_service,
        share_repository=share_repository,
    )
    app.state.team_service = team_service
    app.state.session_service = AuthSessionService(settings=settings)
    app.state.investigation_service = _Tripwire("investigation_service")
    app.state.share_repository = share_repository
    app.state.knowledge_service = knowledge_service
    app.state.organization_repository = organization_repository
    app.state.tenant_provider = MultiTenantProvider(
        organization_repository=organization_repository
    )
    app.state.operator_audit_repository = SessionlessOperatorAuditRepository()
    app.state.operator_grant_repository = SessionlessOperatorGrantRepository()
    app.state.suggestion_service = SuggestionService(
        case_repository=case_repository,
        knowledge_service=knowledge_service,
        sanitizer=sanitizer,
        suggestion_repository=DatabaseSuggestionRepository(),
    )
    app.state.conversion_service = ConversionService(
        llm_router=AsyncMock(),
        settings=settings,
        db_session_factory=get_db_session,
        knowledge_service=knowledge_service,
        share_repository=share_repository,
        team_service=team_service,
    )
    # ``get_case_repository`` reads the repository off ``app.extra`` rather than
    # ``app.state`` — the reports read path is the only consumer. A namespace is
    # the whole of what that accessor uses (``getattr(container, name, None)``);
    # a real DIContainer would drag in the LLM stack for no additional coverage.
    app.extra["di_container"] = SimpleNamespace(
        case_repository=case_repository,
        case_vector_store=None,
        runbook_kb=None,
        llm_provider=None,
    )


@pytest.fixture(scope="module")
def probe_app():
    """The real application, built once, over the limited PostgreSQL role.

    Module-scoped and synchronous on purpose. Building the app is loop-free, and
    the environment it is built under has to be *restored* before any sibling
    module runs — ``test_rls_tenant_isolation.py`` reads ``DATABASE_URL``
    expecting the superuser url, and would silently measure RLS as the wrong role
    if this module leaked the limited one.

    This fixture does NOT check the schema. It must build on main so the two
    inventory tests can read the live OpenAPI document; the enterprise-schema
    gate belongs to the world fixtures and to the schema test.
    """
    superuser_url = os.environ["DATABASE_URL"]
    saved = {key: os.environ.get(key) for key in _PROBE_ENV_KEYS}

    asyncio.run(create_limited_role(superuser_url, _LIMITED_ROLE, _LIMITED_PW))

    os.environ["DATABASE_URL"] = limited_url(superuser_url, _LIMITED_ROLE, _LIMITED_PW)
    os.environ["DEPLOYMENT_MODE"] = "cloud"
    os.environ["TENANT_PROVIDER"] = "multi"
    os.environ["AUTH_MODE"] = "local"
    os.environ["JWT_SECRET_KEY"] = _JWT_SECRET
    os.environ["SKIP_SERVICE_CHECKS"] = "true"
    os.environ.pop("OAUTH_ENABLED", None)
    # Pinned, not inherited. ``ENVIRONMENT`` decides whether the debug router is
    # mounted, and the route inventory is computed from the app that is actually
    # built — so an ambient ``ENVIRONMENT=production`` would drop
    # ``/debug/cases/{case_id}/causal-graph`` from the live set and the
    # inventory's stale-entry half would fail for a reason that has nothing to do
    # with tenancy. Pinning it also fixes the protection preset, so the module
    # does not inherit a different rate-limit shape per machine.
    os.environ["ENVIRONMENT"] = "development"

    from faultmaven.config.settings import reset_settings
    from faultmaven.infrastructure.persistence.database import reset_engine
    from tests.integration._app_rebuild import rebuild_app

    reset_settings()
    reset_engine()
    app = rebuild_app()
    _wire_services(app, _fresh_chroma())

    yield SimpleNamespace(app=app, superuser_url=superuser_url)

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings()
    reset_engine()
    asyncio.run(drop_limited_role(superuser_url, _LIMITED_ROLE))


# =============================================================================
# Seeding — production writers under the ENTERPRISE binding
# =============================================================================
#
# Everything below binds the enterprise the way the request front door will
# (Phase 2's ``_current_enterprise_id``) and then writes through the real code.
# Rows that have no production writer yet — teams, memberships, shares, KB items,
# the billing organization roster — are inserted as the LIMITED role under that
# binding, so PostgreSQL's ``WITH CHECK`` half of the policy validates every
# stamp. A superuser INSERT would bypass the policy and could seed a row the
# application is not able to produce, which proves nothing about the deployed
# system. Superuser inserts are confined to ``enterprises``, ``users`` and
# ``organizations`` — the three tables no tenant session may create.


@asynccontextmanager
async def _as_enterprise(enterprise_id: str):
    """Bind the enterprise contextvar the way the request binder will.

    Used only by the seeding helpers. Every *probe* binds its enterprise the real
    way — by presenting a token — because the binding is part of what is under
    test. The import is local: ``_current_enterprise_id`` is a Phase-2 name, and
    a module-level import of it would turn every collection of this file into an
    ImportError long before the schema check could say what is actually missing.
    """
    from faultmaven.config.tenant_context import _current_enterprise_id

    token = _current_enterprise_id.set(enterprise_id)
    try:
        yield
    finally:
        _current_enterprise_id.reset(token)


async def _seed_case_with_content(
    *, enterprise_id, organization_id, user_id, title, secret_prefix
):
    """Write one case and its child rows through the PRODUCTION writers.

    ``organization_id`` is **billing attribution** (ADR-017 D2) and is ``None``
    in arms 1 and 3, where no organization exists. It never decides visibility;
    that is what ``enterprise_id`` is for, and the two are passed separately here
    precisely so a Phase-3 call site that confuses them shows up as a failure
    rather than as a value that happens to be equal.
    """
    from faultmaven.modules.case.domain.models import (
        Case,
        Evidence,
        EvidenceCategory,
        EvidenceSourceType,
        UploadedFile,
    )
    from faultmaven.modules.case.domain.owned_models.report import (
        CaseReport,
        ReportStatus,
        ReportType,
    )
    from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
        SessionlessCaseRepository,
    )

    repository = SessionlessCaseRepository()
    case_id = f"case_{uuid.uuid4().hex[:12]}"
    file_id = f"file_{uuid.uuid4().hex[:12]}"
    evidence_id = f"ev_{uuid.uuid4().hex[:12]}"
    report_id = str(uuid.uuid4())

    uploaded_file = UploadedFile(
        file_id=file_id,
        filename=f"{secret_prefix}-oncall-rota.log",
        size_bytes=42,
        content_type="text/plain",
        uploaded_at_turn=1,
        uploaded_at=datetime.now(UTC),
        uploaded_by=user_id,
    )
    evidence = Evidence(
        evidence_id=evidence_id,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="M1",
        summary=f"{secret_prefix}-payroll-dsn",
        source_type=EvidenceSourceType.LOGS,
        source_file_id=file_id,
        collected_by=user_id,
        collected_at_turn=1,
    )
    case = Case(
        case_id=case_id,
        enterprise_id=enterprise_id,
        organization_id=organization_id,
        user_id=user_id,
        title=title,
        description=f"{secret_prefix}-description",
    )

    async with _as_enterprise(enterprise_id):
        await repository.save(case)
        await repository.add_message(
            case_id,
            {
                "role": "user",
                "content": f"{secret_prefix}-transcript-line",
                "turn_number": 1,
            },
        )
        # The uploaded file first: ``evidence.source_file_id`` is an FK to it, and
        # the aggregate save writes evidence in the same statement batch. The
        # third argument is the tenant the row is stamped with — passed
        # positionally so Phase 3's rename of the parameter does not break this.
        await repository.add_uploaded_file(case_id, uploaded_file, enterprise_id)
        case.uploaded_files = [uploaded_file]
        case.evidence = [evidence]
        await repository.save(case)
        await repository.add_report(
            CaseReport(
                report_id=report_id,
                case_id=case_id,
                report_type=ReportType.CLOSURE_SUMMARY,
                title=f"{secret_prefix}-postmortem summary",
                content=f"# {secret_prefix}-postmortem\n\nroot cause: rotated dsn",
                generation_status=ReportStatus.COMPLETED,
                generated_at=datetime.now(UTC).isoformat(),
                generation_time_ms=1,
            )
        )

    return SimpleNamespace(
        case_id=case_id,
        file_id=file_id,
        evidence_id=evidence_id,
        report_id=report_id,
    )


_KB_INSERT = text("""
    INSERT INTO knowledge_items
        (item_id, enterprise_id, organization_id, scope, owner_id, title, content,
         item_type, tags, is_published, metadata)
    VALUES (:item_id, :enterprise, :org, :scope, :owner, :title, :content,
            'runbook', ARRAY['probe']::varchar[], true, '{}'::jsonb)
    """)

_SHARE_INSERT = text("""
    INSERT INTO resource_shares
        (share_id, resource_type, resource_id, scope_type, scope_id,
         enterprise_id, organization_id, created_by)
    VALUES (:share_id, :resource_type, :resource_id, 'team', :team_id,
            :enterprise, :org, :by)
    """)

_TEAM_INSERT = text(
    "INSERT INTO teams (team_id, enterprise_id, name) VALUES (:t, :e, :n)"
)
_MEMBER_INSERT = text(
    "INSERT INTO team_members (user_id, team_id, team_role) VALUES (:u, :t, 'member')"
)

#: Arm 2 only: the BILLING roster. Under ADR-017 D5 an ``organization_members``
#: row says who pays for an account and grants no visibility whatsoever — which
#: is exactly what arm 2 measures, by putting A and B in different organizations
#: and one team.
_BILLING_MEMBERSHIP_INSERT = text("""
    INSERT INTO organization_members (user_id, organization_id, enterprise_id, role_id)
    SELECT :u, :o, :e, role_id FROM roles WHERE name = 'admin'
    """)

_CONVERSION_JOB_INSERT = text("""
    INSERT INTO conversion_jobs
        (id, enterprise_id, organization_id, user_id, case_id, source_file_id,
         scope, status, source_type)
    VALUES (:id, :enterprise, :org, :user, :case_id, :file_id, 'personal',
            'completed', 'case')
    """)

_CONVERSION_DRAFT_INSERT = text("""
    INSERT INTO conversion_drafts
        (id, enterprise_id, organization_id, conversion_id, runbook_id, title,
         file_path, status)
    VALUES (:id, :enterprise, :org, :conversion_id, :runbook_id, :title, :path,
            'draft')
    """)

_SUGGESTION_INSERT = text("""
    INSERT INTO knowledge_suggestions
        (suggestion_id, enterprise_id, organization_id, case_id, status,
         suggested_title, suggested_content, extracted_by, source_case_title)
    VALUES (:id, :enterprise, :org, :case_id, 'pending_review', :title, :content,
            :by, :title)
    """)


async def _seed_team(session_factory, *, enterprise_id, team_id, name, member_ids):
    """A team and its members, written as the enterprise through the limited role."""
    async with _as_enterprise(enterprise_id):
        async with session_factory() as session:
            await session.execute(
                _TEAM_INSERT, {"t": team_id, "e": enterprise_id, "n": name}
            )
            for member in member_ids:
                await session.execute(_MEMBER_INSERT, {"u": member, "t": team_id})
            await session.commit()


async def _seed_knowledge_item(
    session_factory,
    *,
    enterprise_id,
    organization_id,
    item_id,
    scope,
    owner_id,
    title,
    share_to_team=None,
):
    async with _as_enterprise(enterprise_id):
        async with session_factory() as session:
            await session.execute(
                _KB_INSERT,
                {
                    "item_id": item_id,
                    "enterprise": enterprise_id,
                    "org": organization_id,
                    "scope": scope,
                    "owner": owner_id,
                    "title": title,
                    "content": f"{title} body",
                },
            )
            if share_to_team:
                await session.execute(
                    _SHARE_INSERT,
                    {
                        "share_id": str(uuid.uuid4()),
                        "resource_type": "knowledge_item",
                        "resource_id": item_id,
                        "team_id": share_to_team,
                        "enterprise": enterprise_id,
                        "org": organization_id,
                        "by": owner_id,
                    },
                )
            await session.commit()


async def _share_case(
    session_factory, *, enterprise_id, organization_id, case_id, team_id, by
):
    async with _as_enterprise(enterprise_id):
        async with session_factory() as session:
            await session.execute(
                _SHARE_INSERT,
                {
                    "share_id": str(uuid.uuid4()),
                    "resource_type": "case",
                    "resource_id": case_id,
                    "team_id": team_id,
                    "enterprise": enterprise_id,
                    "org": organization_id,
                    "by": by,
                },
            )
            await session.commit()


async def _seed_conversion_and_suggestion(session_factory, party) -> None:
    """The conversion job, its draft, and one knowledge suggestion."""
    conversion_id = f"conv_{uuid.uuid4().hex[:12]}"
    draft_id = f"draft_{uuid.uuid4().hex[:12]}"
    suggestion_id = str(uuid.uuid4())

    async with _as_enterprise(party.enterprise_id):
        async with session_factory() as session:
            await session.execute(
                _CONVERSION_JOB_INSERT,
                {
                    "id": conversion_id,
                    "enterprise": party.enterprise_id,
                    "org": party.organization_id,
                    "user": party.user_id,
                    "case_id": party.case.case_id,
                    "file_id": party.case.file_id,
                },
            )
            await session.execute(
                _CONVERSION_DRAFT_INSERT,
                {
                    "id": draft_id,
                    "enterprise": party.enterprise_id,
                    "org": party.organization_id,
                    "conversion_id": conversion_id,
                    "runbook_id": f"rb-{uuid.uuid4().hex[:8]}",
                    "title": f"{party.secret}-conversion-draft",
                    "path": f"/tmp/{draft_id}.md",
                },
            )
            await session.execute(
                _SUGGESTION_INSERT,
                {
                    "id": suggestion_id,
                    "enterprise": party.enterprise_id,
                    "org": party.organization_id,
                    "case_id": party.case.case_id,
                    "title": f"{party.secret}-suggestion",
                    "content": f"{party.secret}-suggestion body",
                    "by": party.user_id,
                },
            )
            await session.commit()

    party.conversion_id = conversion_id
    party.draft_id = draft_id
    party.suggestion_id = suggestion_id


#: One embedding for every chunk and every query, so cosine similarity excludes
#: nothing and the metadata filter is the ONLY thing that can keep a row out of a
#: KB result set. A probe whose negative could be produced by a poor match is not
#: a probe of the filter.
_VEC = [0.5] * 8


async def _fixed_embedding(*_args: Any, **_kwargs: Any) -> list[float]:
    return list(_VEC)


async def _seed_kb_chunks(chroma_store, rows_spec) -> None:
    """Index runbooks into the REAL ChromaDB the app searches.

    Rows go in through ``KnowledgeVectorStore.add_documents``, including its
    ``VectorMetadata`` allowlist, so a metadata key production never stamps
    cannot be smuggled in here. #1168 is the reason this exists at all: the
    vector layer has no tenant dimension, so a SQL-surface pass says nothing
    about retrieval — and under ADR-017 it says even less, because in arms 2 and
    3 the two parties share an enterprise and RLS separates nothing.
    """
    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KB_COLLECTION,
    )

    rows = [
        {
            "id": f"{item_id}_chunk_0",
            "content": (
                f"# Runbook\n{title}\nconnection pool exhaustion in production."
            ),
            "metadata": {
                "document_type": "runbook",
                "scope": "personal",
                "title": title,
                "parent_document_id": item_id,
                "chunk_index": 0,
                "total_chunks": 1,
                "owner_id": owner_id,
                "domain": "database",
                "service": "postgres",
            },
        }
        for item_id, title, owner_id in rows_spec
    ]
    await chroma_store.add_documents(
        rows, embeddings=[list(_VEC) for _ in rows], collection_name=KB_COLLECTION
    )


# =============================================================================
# Tokens — the enterprise claim is the isolation input
# =============================================================================


def _forge_token(
    auth_service,
    *,
    user_id: str,
    enterprise_id: str | None,
    organization_id: str | None,
    email: str,
    roles: list[str],
) -> str:
    """An access token carrying the claim set the live generators emit, plus
    ``enterprise_id``.

    Written out here rather than by extending ``tests.utils.forge_access_token``
    on purpose (spec, item 6): Phase 2 rewrites that helper, and a probe whose
    tokens are minted by the code it is probing cannot show that the *claim* is
    what the binder reads. ``sign_claims_for`` is the only thing borrowed — it
    mirrors the service's own key selection, so the forgery passes signature
    validation and the test exercises what comes after it.

    ``enterprise_id=None`` omits the claim entirely, which is the shape
    :func:`test_a_token_without_an_enterprise_claim_is_refused` sends.
    ``organization_id`` is billing context and is ``None`` in arms 1 and 3.
    """
    from datetime import timedelta

    from tests.utils import LIVE_ACCESS_TOKEN_SCOPES, sign_claims_for

    now = datetime.now(UTC)
    expires = auth_service._settings.auth.jwt_access_token_expire_minutes
    claims: dict[str, Any] = {
        "sub": user_id,
        "username": user_id,
        "email": email,
        "organization_id": organization_id,
        "roles": roles,
        "scopes": list(LIVE_ACCESS_TOKEN_SCOPES),
        "exp": int((now + timedelta(minutes=expires)).timestamp()),
        "iat": int(now.timestamp()),
        "iss": auth_service._settings.security.jwt_issuer,
        "aud": auth_service._settings.security.jwt_audience,
        "jti": str(uuid.uuid4()),
        "type": "access",
        "auth_mode": "oauth" if auth_service._algorithm == "RS256" else "local",
    }
    if enterprise_id is not None:
        claims["enterprise_id"] = enterprise_id
    return sign_claims_for(auth_service, claims)


# =============================================================================
# The worlds
# =============================================================================


async def _delete_case_rows(conn, case_ids) -> None:
    for case_id in case_ids:
        for table in (
            "knowledge_suggestions",
            "reports",
            "evidence",
            "uploaded_files",
            "case_messages",
        ):
            await conn.execute(
                text(f"DELETE FROM {table} WHERE case_id = :c"), {"c": case_id}
            )
        await conn.execute(text("DELETE FROM cases WHERE case_id = :c"), {"c": case_id})


async def _delete_operator_rows(engine, enterprises) -> None:
    """Remove the append-only operator rows this module's own tests minted.

    The break-glass case and the grant-listing case both POST a grant, and every
    granted read writes an ``operator_access_audit`` row. The probe is the writer,
    so the probe has to be the remover — and these two tables are keyed on
    ``target_enterprise_id`` rather than ``enterprise_id``, because break-glass is
    cross-tenant by construction (ADR-012 D9), which is also why the main teardown
    loop cannot reach them.

    ``session_replication_role = 'replica'`` because migrations 035/036 make both
    tables append-only with ``BEFORE DELETE`` triggers that ``RAISE EXCEPTION``
    unconditionally — measured: the DELETE is rejected for the table-owning
    SUPERUSER too, so there is no owner exemption to lean on. Disabling user
    triggers for one transaction is the only way to clean up after ourselves, it
    is superuser-only (the deployed ``faultmaven_app`` role cannot set it, so the
    append-only guarantee this bypasses is untouched where it matters), and
    ``SET LOCAL`` scopes it to this transaction alone. Run BEFORE the enterprise
    rows go, in case Phase 1 gives ``target_enterprise_id`` a restricting FK.
    """
    async with engine.begin() as conn:
        await conn.exec_driver_sql("SET LOCAL session_replication_role = 'replica'")
        for enterprise_id in enterprises:
            for table in ("operator_access_grants", "operator_access_audit"):
                await conn.execute(
                    text(f"DELETE FROM {table} WHERE target_enterprise_id = :e"),
                    {"e": enterprise_id},
                )


async def _teardown_rows(engine, *, enterprises, users, case_ids, conversion_ids):
    """Delete everything the world seeded, explicitly.

    Explicitly rather than by leaning on ``ON DELETE CASCADE`` from
    ``enterprises``: the cascade behaviour of the enterprise FKs Phase 1 adds is
    not yet decided, and a teardown that silently depended on it would leave rows
    behind on whichever tables got a ``RESTRICT``. The ``-m postgres`` lane runs
    this module beside siblings that count rows, so residue is a defect in them,
    not only here.
    """
    await _delete_operator_rows(engine, enterprises)
    async with engine.begin() as conn:
        for conversion_id in conversion_ids:
            await conn.execute(
                text("DELETE FROM conversion_drafts WHERE conversion_id = :c"),
                {"c": conversion_id},
            )
            await conn.execute(
                text("DELETE FROM conversion_jobs WHERE id = :c"), {"c": conversion_id}
            )
        await _delete_case_rows(conn, case_ids)
        for enterprise_id in enterprises:
            # ``team_members`` is the one table with no enterprise_id column of
            # its own (ADR-017 keys it by one hop through ``teams``), so its
            # rows are deleted through that same hop, and before ``teams``.
            await conn.execute(
                text(
                    "DELETE FROM team_members WHERE team_id IN "
                    "(SELECT team_id FROM teams WHERE enterprise_id = :e)"
                ),
                {"e": enterprise_id},
            )
            for table in (
                "resource_shares",
                "knowledge_items",
                "teams",
                "organization_members",
                "organizations",
            ):
                await conn.execute(
                    text(f"DELETE FROM {table} WHERE enterprise_id = :e"),
                    {"e": enterprise_id},
                )
        for user_id in users:
            await conn.execute(
                text("DELETE FROM users WHERE user_id = :u"), {"u": user_id}
            )
        for enterprise_id in enterprises:
            await conn.execute(
                text("DELETE FROM enterprises WHERE enterprise_id = :e"),
                {"e": enterprise_id},
            )


@asynccontextmanager
async def _wall_world(probe_app, arm: str):
    """Arms 1 and 3: two principals who share nothing.

    ``two_enterprises`` — A in E_A, B in E_B: the wall PostgreSQL enforces.
    ``same_enterprise_no_team`` — both in E, a team each, no common team: the
    same battery with RLS admitting both rows, so only the application's own
    ``owned ∪ shared-to-my-teams`` resolution can refuse.
    """
    await _require_enterprise_schema(probe_app.superuser_url)
    _require_enterprise_binder()

    import httpx

    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        get_session_factory,
        reset_engine,
    )
    from faultmaven.utils.runbook_id import knowledge_root
    from tests.utils import seed_enterprises, seed_users

    app = probe_app.app
    reset_engine()

    superuser_engine = create_async_engine(probe_app.superuser_url, future=True)
    enterprise_a = f"ent_a_{uuid.uuid4().hex[:8]}"
    enterprise_b = (
        enterprise_a
        if arm == "same_enterprise_no_team"
        else f"ent_b_{uuid.uuid4().hex[:8]}"
    )
    user_a = f"user_a_{uuid.uuid4().hex[:8]}"
    user_b = f"user_b_{uuid.uuid4().hex[:8]}"
    operator_a = f"user_op_{uuid.uuid4().hex[:8]}"

    superuser_maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with superuser_maker() as session:
        await seed_enterprises(session, sorted({enterprise_a, enterprise_b}))
        await seed_users(session, [user_a, operator_a], enterprise_id=enterprise_a)
        await seed_users(session, [user_b], enterprise_id=enterprise_b)
        # ``seed_users`` derives ``<user_id>@test.local``, and ``.local`` is a
        # reserved special-use name ``email-validator`` refuses. The admin user
        # routes hydrate a ``User`` model on the way out, so a seeded row would
        # answer 500 — and a 500 is not a refusal, it is an assertion that never
        # got to run. Re-stamped to a domain the model accepts.
        await session.execute(
            text(
                "UPDATE users SET email = user_id || '@example.com' "
                "WHERE user_id IN (:a, :b, :c)"
            ),
            {"a": user_a, "b": user_b, "c": operator_a},
        )
        await session.commit()

    party_a = SimpleNamespace(
        enterprise_id=enterprise_a,
        organization_id=None,
        user_id=user_a,
        secret=SECRET_A,
        team_id=f"team_a_{uuid.uuid4().hex[:8]}",
        kb_personal_id=f"kb_a_{uuid.uuid4().hex[:12]}",
        kb_team_id=f"kb_at_{uuid.uuid4().hex[:12]}",
        enterprise_members=[user_a, operator_a],
    )
    party_b = SimpleNamespace(
        enterprise_id=enterprise_b,
        organization_id=None,
        user_id=user_b,
        secret=SECRET_B,
        team_id=f"team_b_{uuid.uuid4().hex[:8]}",
        kb_personal_id=f"kb_b_{uuid.uuid4().hex[:12]}",
        kb_team_id=f"kb_bt_{uuid.uuid4().hex[:12]}",
        enterprise_members=[user_b],
    )

    # The runbook-publish control writes a markdown file under the KB root.
    # Snapshotted BEFORE the try, so the finally can always diff against it.
    kb_root = knowledge_root()
    kb_entries_before = set(kb_root.iterdir()) if kb_root.exists() else set()

    # try/finally rather than try/except-and-re-raise, for one reason: the
    # normal path and the failure path have to run the SAME teardown. Once
    # the superuser commit above lands, enterprises and users EXIST; if the
    # first Phase-2 import below then raises — which is precisely what a name
    # mismatch during Phases 2-3 looks like — an unguarded fixture leaves them,
    # the engine undisposed, and the `-m postgres` siblings looking at residue.
    # `finally` needs no `raise`: the exception propagates on its own.
    try:
        session_factory = get_session_factory()
        # A's team also holds the operator, so the user-administration controls in
        # arm 1 have somebody in the operator's own enterprise to act on.
        await _seed_team(
            session_factory,
            enterprise_id=enterprise_a,
            team_id=party_a.team_id,
            name=f"team-a-{_RUN}",
            member_ids=[user_a, operator_a],
        )
        await _seed_team(
            session_factory,
            enterprise_id=enterprise_b,
            team_id=party_b.team_id,
            name=f"team-b-{_RUN}",
            member_ids=[user_b],
        )
        for party in (party_a, party_b):
            await _seed_knowledge_item(
                session_factory,
                enterprise_id=party.enterprise_id,
                organization_id=None,
                item_id=party.kb_personal_id,
                scope="personal",
                owner_id=party.user_id,
                title=f"{party.secret}-runbook-personal",
            )
            await _seed_knowledge_item(
                session_factory,
                enterprise_id=party.enterprise_id,
                organization_id=None,
                item_id=party.kb_team_id,
                scope="team",
                owner_id=party.user_id,
                title=f"{party.secret}-runbook-team",
                share_to_team=party.team_id,
            )

        party_b.case = await _seed_case_with_content(
            enterprise_id=enterprise_b,
            organization_id=None,
            user_id=user_b,
            title=SECRET_B_TITLE,
            secret_prefix=SECRET_B,
        )
        party_a.case = await _seed_case_with_content(
            enterprise_id=enterprise_a,
            organization_id=None,
            user_id=user_a,
            title=f"{SECRET_A}-own-incident",
            secret_prefix=SECRET_A,
        )
        await _seed_conversion_and_suggestion(session_factory, party_b)
        await _seed_conversion_and_suggestion(session_factory, party_a)

        # A FRESH ChromaDB per test, not the one the module fixture built: the corpus
        # must be a function of this test rather than of everything that ran before
        # it, which is the ordering dependence the sibling KB probe records as its
        # own first defect.
        vector_store = KnowledgeVectorStore(_fresh_chroma())
        app.state.knowledge_service._vector_store = vector_store
        await _seed_kb_chunks(
            vector_store,
            [
                (p.kb_personal_id, f"{p.secret}-runbook-personal", p.user_id)
                for p in (party_a, party_b)
            ]
            + [
                (p.kb_team_id, f"{p.secret}-runbook-team", p.user_id)
                for p in (party_a, party_b)
            ],
        )

        auth_service = app.state.auth_service
        token_a = _forge_token(
            auth_service,
            user_id=user_a,
            enterprise_id=enterprise_a,
            organization_id=None,
            email=f"{user_a}@example.com",
            roles=["user", "admin"],
        )
        token_b = _forge_token(
            auth_service,
            user_id=user_b,
            enterprise_id=enterprise_b,
            organization_id=None,
            email=f"{user_b}@example.com",
            roles=["user", "admin"],
        )
        # A platform operator whose *request* still binds enterprise A. The
        # cross-tenant role is the strongest principal the deployment mints, and the
        # question this module asks of it is whether the role alone reaches B's rows.
        token_operator_a = _forge_token(
            auth_service,
            user_id=operator_a,
            enterprise_id=enterprise_a,
            organization_id=None,
            email=f"{operator_a}@example.com",
            roles=["user", "platform_admin"],
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://probe", timeout=60.0
        ) as http:
            yield SimpleNamespace(
                arm=arm,
                app=app,
                http=http,
                a=party_a,
                b=party_b,
                token_a=token_a,
                token_b=token_b,
                token_operator_a=token_operator_a,
                superuser_engine=superuser_engine,
                superuser_maker=superuser_maker,
            )

    finally:
        await close_database()
        if kb_root.exists():
            for entry in set(kb_root.iterdir()) - kb_entries_before:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
        # `getattr`, because a failure part-way through seeding leaves some of
        # these unset and the teardown still has to remove what DID land.
        await _teardown_rows(
            superuser_engine,
            enterprises=sorted({enterprise_a, enterprise_b}),
            users=[user_a, user_b, operator_a],
            case_ids=[
                case.case_id
                for case in (
                    getattr(party_a, "case", None),
                    getattr(party_b, "case", None),
                )
                if case is not None
            ],
            conversion_ids=[
                conversion_id
                for conversion_id in (
                    getattr(party_a, "conversion_id", None),
                    getattr(party_b, "conversion_id", None),
                )
                if conversion_id is not None
            ],
        )
        await superuser_engine.dispose()


@pytest.fixture(params=["two_enterprises", "same_enterprise_no_team"])
async def world(probe_app, request):
    """The "nothing crosses" battery, run once per wall arm."""
    async with _wall_world(probe_app, request.param) as built:
        yield built


@pytest.fixture
async def wall_world(probe_app):
    """Arm 1 only — the surfaces whose boundary IS the enterprise.

    Operator confinement, break-glass and user administration key on the
    enterprise, so inside one enterprise (arm 3) there is nothing for them to
    refuse: the operator is confined to a scope that already contains both
    parties. Running them there would assert a boundary the design does not
    claim, and a test that cannot fail is worse than no test.
    """
    async with _wall_world(probe_app, "two_enterprises") as built:
        yield built


@pytest.fixture
async def shared_world(probe_app):
    """Arm 2 — one enterprise, two billing organizations, one shared team.

    The row ADR-017 adds to the matrix. A is in organization X, B in Y, and both
    tokens carry their organization as **billing context**; the isolation claim
    is the enterprise, which is the same for both. One team T holds them both. A
    owns a shared case and a private one, a shared runbook and a personal one.

    B must see exactly the shared halves. The shared sibling is the positive
    control for the private one's absence: same call, same actor, same instant,
    so "B sees nothing private" cannot be satisfied by a share that never worked
    or by a deployment whose case table is unreadable.
    """
    await _require_enterprise_schema(probe_app.superuser_url)
    _require_enterprise_binder()

    import httpx

    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        get_session_factory,
        reset_engine,
    )
    from tests.utils import seed_enterprises, seed_organizations, seed_users

    app = probe_app.app
    reset_engine()

    superuser_engine = create_async_engine(probe_app.superuser_url, future=True)
    enterprise = f"ent_s_{uuid.uuid4().hex[:8]}"
    org_x = f"org_x_{uuid.uuid4().hex[:8]}"
    org_y = f"org_y_{uuid.uuid4().hex[:8]}"
    user_a = f"user_sa_{uuid.uuid4().hex[:8]}"
    user_b = f"user_sb_{uuid.uuid4().hex[:8]}"

    superuser_maker = async_sessionmaker(superuser_engine, expire_on_commit=False)
    async with superuser_maker() as session:
        await seed_enterprises(session, [enterprise])
        await seed_organizations(session, [org_x, org_y], enterprise_id=enterprise)
        await seed_users(session, [user_a, user_b], enterprise_id=enterprise)
        await session.execute(
            text(
                "UPDATE users SET email = user_id || '@example.com' "
                "WHERE user_id IN (:a, :b)"
            ),
            {"a": user_a, "b": user_b},
        )
        await session.commit()

    team_shared = f"team_t_{uuid.uuid4().hex[:8]}"
    team_a_own = f"team_ao_{uuid.uuid4().hex[:8]}"
    team_b_own = f"team_bo_{uuid.uuid4().hex[:8]}"
    kb_shared_id = f"kb_sh_{uuid.uuid4().hex[:12]}"
    kb_private_id = f"kb_pv_{uuid.uuid4().hex[:12]}"

    # Bound before the try so the finally can name them whatever failed; see
    # `_wall_world` for why this is try/finally and not try/except.
    shared_case = None
    private_case = None
    try:
        session_factory = get_session_factory()
        async with _as_enterprise(enterprise):
            async with session_factory() as session:
                await session.execute(
                    _BILLING_MEMBERSHIP_INSERT,
                    {"u": user_a, "o": org_x, "e": enterprise},
                )
                await session.execute(
                    _BILLING_MEMBERSHIP_INSERT,
                    {"u": user_b, "o": org_y, "e": enterprise},
                )
                await session.commit()

        await _seed_team(
            session_factory,
            enterprise_id=enterprise,
            team_id=team_shared,
            name=f"team-shared-{_RUN}",
            member_ids=[user_a, user_b],
        )
        await _seed_team(
            session_factory,
            enterprise_id=enterprise,
            team_id=team_a_own,
            name=f"team-a-own-{_RUN}",
            member_ids=[user_a],
        )
        await _seed_team(
            session_factory,
            enterprise_id=enterprise,
            team_id=team_b_own,
            name=f"team-b-own-{_RUN}",
            member_ids=[user_b],
        )

        await _seed_knowledge_item(
            session_factory,
            enterprise_id=enterprise,
            organization_id=org_x,
            item_id=kb_shared_id,
            scope="team",
            owner_id=user_a,
            title=SHARED_KB,
            share_to_team=team_shared,
        )
        await _seed_knowledge_item(
            session_factory,
            enterprise_id=enterprise,
            organization_id=org_x,
            item_id=kb_private_id,
            scope="personal",
            owner_id=user_a,
            title=PRIVATE_KB,
        )

        shared_case = await _seed_case_with_content(
            enterprise_id=enterprise,
            organization_id=org_x,
            user_id=user_a,
            title=SHARED_TITLE,
            secret_prefix=SHARED,
        )
        private_case = await _seed_case_with_content(
            enterprise_id=enterprise,
            organization_id=org_x,
            user_id=user_a,
            title=PRIVATE_TITLE,
            secret_prefix=PRIVATE,
        )
        await _share_case(
            session_factory,
            enterprise_id=enterprise,
            organization_id=org_x,
            case_id=shared_case.case_id,
            team_id=team_shared,
            by=user_a,
        )

        vector_store = KnowledgeVectorStore(_fresh_chroma())
        app.state.knowledge_service._vector_store = vector_store
        await _seed_kb_chunks(
            vector_store,
            [(kb_shared_id, SHARED_KB, user_a), (kb_private_id, PRIVATE_KB, user_a)],
        )

        auth_service = app.state.auth_service
        token_a = _forge_token(
            auth_service,
            user_id=user_a,
            enterprise_id=enterprise,
            organization_id=org_x,
            email=f"{user_a}@example.com",
            roles=["user"],
        )
        token_b = _forge_token(
            auth_service,
            user_id=user_b,
            enterprise_id=enterprise,
            organization_id=org_y,
            email=f"{user_b}@example.com",
            roles=["user"],
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://probe", timeout=60.0
        ) as http:
            yield SimpleNamespace(
                app=app,
                http=http,
                enterprise_id=enterprise,
                org_x=org_x,
                org_y=org_y,
                user_a=user_a,
                user_b=user_b,
                team_shared=team_shared,
                team_a_own=team_a_own,
                team_b_own=team_b_own,
                shared_case=shared_case,
                private_case=private_case,
                kb_shared_id=kb_shared_id,
                kb_private_id=kb_private_id,
                token_a=token_a,
                token_b=token_b,
                superuser_engine=superuser_engine,
            )

    finally:
        await close_database()
        await _teardown_rows(
            superuser_engine,
            enterprises=[enterprise],
            users=[user_a, user_b],
            case_ids=[
                case.case_id for case in (shared_case, private_case) if case is not None
            ],
            conversion_ids=[],
        )
        await superuser_engine.dispose()


# =============================================================================
# The two moves every test makes: one party finds it, the other must not
# =============================================================================


async def _call(world, token: str, method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    return await world.http.request(method, path, headers=headers, **kwargs)


async def as_b(world, method: str, path: str, **kwargs):
    """The positive control: the same call, by the party who owns the row."""
    return await _call(world, world.token_b, method, path, **kwargs)


async def as_a(world, method: str, path: str, **kwargs):
    """The attack: A, holding a valid token for A, reaching for B."""
    return await _call(world, world.token_a, method, path, **kwargs)


def _assert_no_markers(response, markers, surface: str, owner: str) -> None:
    body = response.text
    leaked = [marker for marker in markers if marker in body]
    assert not leaked, (
        f"{surface}: the caller received {owner}'s content {leaked} "
        f"(status {response.status_code}): {body[:400]}"
    )


def assert_no_b_content(response, surface: str) -> None:
    """No marker of B's may appear in a body A received — at ANY status.

    Applied to refusals as well as successes on purpose. A 403 whose ``detail``
    echoes the case title, or a 500 whose traceback carries the row, has already
    leaked; reading only the status code would call both of those a pass.
    """
    _assert_no_markers(response, B_MARKERS, surface, "B")


def assert_no_private_content(response, surface: str) -> None:
    """Arm 2: nothing of A's UNSHARED content may appear in a body B received."""
    _assert_no_markers(response, PRIVATE_MARKERS, surface, "A (unshared)")


def assert_refused(response, surface: str) -> None:
    """A must be refused, and refused without confirming the row exists."""
    assert response.status_code in REFUSED, (
        f"{surface}: expected a refusal, got {response.status_code}: "
        f"{response.text[:400]}"
    )
    assert_no_b_content(response, surface)


def _ids(payload: Any, key: str) -> set[str]:
    """Every value of ``key`` anywhere in a JSON document.

    Walks the whole structure rather than a known path: the shapes differ per
    surface (``{"cases": [...]}, [...], {"results": {...}}``) and a probe that
    hard-codes one of them stops counting the moment a response model changes,
    which is silent under a "the list is empty" assertion.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            value = node.get(key)
            if isinstance(value, str):
                found.add(value)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return found


def _keys(payload: Any) -> set[str]:
    """Every key name anywhere in a JSON document (contract-shape assertions)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            found.update(node.keys())
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return found


# =============================================================================
# The schema gate, as a test
# =============================================================================


@phase1_pending
async def test_the_schema_carries_the_enterprise_key(probe_app):
    """The same assertion the world fixtures make, so the red is a FAIL row.

    A fixture that refuses to build reports the reason once per test as an xfail
    with no detail in the summary. This case puts the catalog's account of what
    is missing where a reader looks first, and it is the single test the Phase-1
    PR turns green.
    """
    await _require_enterprise_schema(probe_app.superuser_url)


# =============================================================================
# Surface 1 — cases: list, detail, transcript, search, analytics
# =============================================================================
#
# Everything from here to surface 7 runs TWICE: once across two enterprises (the
# wall PostgreSQL enforces) and once inside one enterprise with no common team
# (the wall only the application enforces). The assertions are identical because
# the guarantee is: a principal sees their own rows and the ones consented to,
# and nothing else — whichever layer happens to be doing the refusing.


@phase3_pending
async def test_the_case_list_shows_a_only_a_and_b_only_b(world):
    """The collection read. Both directions, because "empty" is not isolation."""
    mine = await as_a(world, "GET", "/api/v1/cases")
    theirs = await as_b(world, "GET", "/api/v1/cases")

    assert theirs.status_code == 200
    assert world.b.case.case_id in _ids(theirs.json(), "case_id"), (
        "the positive control failed: B cannot see B's own case, so every "
        "negative assertion in this module is vacuous"
    )
    assert mine.status_code == 200
    assert world.a.case.case_id in _ids(mine.json(), "case_id")
    assert world.b.case.case_id not in _ids(mine.json(), "case_id")
    assert_no_b_content(mine, "GET /api/v1/cases")


@phase3_pending
async def test_a_case_detail_read_by_the_other_party_is_refused(world):
    """The id-addressed read, which is where a missing predicate shows up.

    The list can look isolated while ``get(case_id)`` has no scope clause at all
    — the list filters by owner, the detail resolves by primary key.
    """
    control = await as_b(world, "GET", f"/api/v1/cases/{world.b.case.case_id}")
    attack = await as_a(world, "GET", f"/api/v1/cases/{world.b.case.case_id}")

    assert control.status_code == 200, "control: B cannot read B's own case"
    assert SECRET_B_TITLE in control.text
    assert_refused(attack, "GET /api/v1/cases/{case_id}")


@phase3_pending
async def test_the_case_ui_projection_is_refused_to_the_other_party(world):
    """``/ui`` is a second, independently written read of the same aggregate."""
    control = await as_b(world, "GET", f"/api/v1/cases/{world.b.case.case_id}/ui")
    attack = await as_a(world, "GET", f"/api/v1/cases/{world.b.case.case_id}/ui")

    assert control.status_code == 200, "control: B cannot read B's own case UI"
    assert_refused(attack, "GET /api/v1/cases/{case_id}/ui")


@phase3_pending
async def test_the_transcript_is_refused_to_the_other_party(world):
    """The transcript is the highest-value content on a case."""
    path = f"/api/v1/cases/{world.b.case.case_id}/messages"
    control = await as_b(world, "GET", path)
    attack = await as_a(world, "GET", path)

    assert control.status_code == 200
    assert SECRET_B_TRANSCRIPT in control.text, "control: B's transcript is missing"
    assert_refused(attack, "GET /api/v1/cases/{case_id}/messages")


@phase3_pending
async def test_case_search_does_not_match_the_other_partys_cases(world):
    """``POST /cases/search`` — a query aimed straight at B's marker string.

    Search is the surface where a missing predicate is least visible: the caller
    supplies the term, so a leak looks like a good result.
    """
    body = {"query": SECRET_B_TITLE, "limit": 50}
    control = await as_b(world, "POST", "/api/v1/cases/search", json=body)
    attack = await as_a(world, "POST", "/api/v1/cases/search", json=body)

    assert control.status_code == 200
    assert world.b.case.case_id in _ids(control.json(), "case_id"), (
        "control: B's own search does not find B's case, so A finding nothing "
        "proves nothing"
    )
    assert attack.status_code == 200
    assert world.b.case.case_id not in _ids(attack.json(), "case_id")
    assert_no_b_content(attack, "POST /api/v1/cases/search")


@phase3_pending
async def test_a_search_body_naming_the_other_party_does_not_widen_the_scope(world):
    """The body carries tenant-shaped fields. Are they honoured?

    ``CaseSearchRequest`` lets the caller name another principal, so the attack
    names B outright, in every DECLARED field that could select a row: the owner,
    the organization and the team. A filter field that *narrows* within the
    caller's own scope is harmless; one that *selects* is the boundary, and the
    scope must come from the verified claim rather than from anything in the body.

    ``organization_id`` is the retired key and the sharpest of the three under
    ADR-017: it is still a declared field, so a Phase-3 repository that kept
    honouring it as a selector would compile, pass every type check, and hand the
    caller rows chosen by a value the caller supplied. In these arms no
    organization exists, so a forged one is what an attacker would have; the
    shared arm sends a REAL organization that really does stamp the target row,
    which is the half that could actually leak.
    """
    attack = await as_a(
        world,
        "POST",
        "/api/v1/cases/search",
        json=_search_injection_body(
            query=SECRET_B_TITLE,
            user_id=world.b.user_id,
            organization_id=f"org_forged_{_RUN}",
            team_id=world.b.team_id,
        ),
    )

    assert attack.status_code in (200, 403, 422)
    assert world.b.case.case_id not in _ids(
        attack.json() if attack.status_code == 200 else [], "case_id"
    )
    assert_no_b_content(attack, "POST /api/v1/cases/search (foreign ids injected)")


@phase3_pending
async def test_case_analytics_are_refused_to_the_other_party(world):
    """Counts are inference: "how many messages does that case have" is content."""
    path = f"/api/v1/cases/{world.b.case.case_id}/analytics"
    control = await as_b(world, "GET", path)
    attack = await as_a(world, "GET", path)

    assert control.status_code == 200
    assert control.json()["message_count"] >= 1, "control: B's analytics are empty"
    assert_refused(attack, "GET /api/v1/cases/{case_id}/analytics")


# =============================================================================
# Surface 2 — evidence and uploaded files
# =============================================================================


@phase3_pending
async def test_evidence_is_refused_to_the_other_party(world):
    """Evidence carries the verbatim system output — the sharpest content."""
    listing = f"/api/v1/cases/{world.b.case.case_id}/evidence"
    item = f"{listing}/{world.b.case.evidence_id}"

    control_list = await as_b(world, "GET", listing)
    control_item = await as_b(world, "GET", item)
    assert control_list.status_code == 200
    assert SECRET_B_EVIDENCE in control_list.text, "control: B's evidence is missing"
    assert control_item.status_code == 200
    assert SECRET_B_EVIDENCE in control_item.text

    assert_refused(await as_a(world, "GET", listing), "GET .../evidence")
    assert_refused(await as_a(world, "GET", item), "GET .../evidence/{evidence_id}")


@phase3_pending
async def test_uploaded_files_are_refused_to_the_other_party(world):
    """Filenames alone are content: they name systems, tenants and people."""
    listing = f"/api/v1/cases/{world.b.case.case_id}/uploaded-files"
    item = f"{listing}/{world.b.case.file_id}"

    control_list = await as_b(world, "GET", listing)
    control_item = await as_b(world, "GET", item)
    assert control_list.status_code == 200
    assert SECRET_B_FILE in control_list.text, "control: B's uploaded file is missing"
    assert control_item.status_code == 200

    assert_refused(await as_a(world, "GET", listing), "GET .../uploaded-files")
    assert_refused(await as_a(world, "GET", item), "GET .../uploaded-files/{file_id}")


@phase3_pending
async def test_the_case_data_surface_is_refused_to_the_other_party(world):
    """``/cases/{id}/data`` — read AND delete, both gated on the same case.

    The control here proves only that B's request passes the case gate: the
    id-addressed read returns a placeholder payload rather than the stored file,
    so "B sees the content" is not assertable. What *is* assertable, and is the
    property at issue, is that A does not get past the gate at all — including on
    the DELETE, which is where a missing predicate destroys another party's row
    rather than merely reading it.
    """
    listing = f"/api/v1/cases/{world.b.case.case_id}/data"
    item = f"{listing}/{world.b.case.file_id}"

    control = await as_b(world, "GET", listing)
    assert control.status_code == 200, "control: B cannot reach its own case data"

    assert_refused(await as_a(world, "GET", listing), "GET .../data")
    assert_refused(await as_a(world, "GET", item), "GET .../data/{data_id}")
    assert_refused(await as_a(world, "DELETE", item), "DELETE .../data/{data_id}")


# =============================================================================
# Surface 3 — reports and conversion drafts
# =============================================================================


@phase3_pending
async def test_case_reports_are_refused_to_the_other_party(world):
    """Both report surfaces: the case-nested one and the report-id one."""
    nested = f"/api/v1/cases/{world.b.case.case_id}/reports"
    download = f"{nested}/{world.b.case.report_id}/download"
    by_case = f"/api/v1/reports/case/{world.b.case.case_id}"
    by_id = f"/api/v1/reports/{world.b.case.report_id}"

    for path in (nested, download, by_case, by_id, f"{by_id}/versions"):
        control = await as_b(world, "GET", path)
        assert control.status_code == 200, f"control: B cannot read {path}"
        assert SECRET_B_REPORT in control.text, f"control: {path} carried no marker"
        assert_refused(await as_a(world, "GET", path), f"GET {path}")


@phase3_pending
async def test_a_report_cannot_be_edited_or_deleted_by_the_other_party(world):
    """Mutation, not just reading. The destructive half of the same surface.

    Three writes, and the third is the one the predecessor listed as probed and
    never called: ``link-case`` takes only a report id and a closure note, and
    derives the case from ``report.case_id`` — so the attack is not "link B's
    report to A's case" (the route offers no such handle) but "make B's report
    the closure record of B's case, as A". It flips ``reports.linked_to_closure``
    and moves the case toward closure, which is why it belongs here rather than
    among the reads.

    Every one is asserted against the DATABASE afterwards, not against the
    response: a route that answers 404 and writes anyway would satisfy a
    status-code assertion while changing the row.
    """
    by_id = f"/api/v1/reports/{world.b.case.report_id}"

    assert_refused(
        await as_a(world, "PUT", by_id, json={"content": "PWNED"}), f"PUT {by_id}"
    )
    assert_refused(
        await as_a(world, "POST", f"{by_id}/link-case", json={"closure_note": "PWNED"}),
        f"POST {by_id}/link-case",
    )
    assert_refused(await as_a(world, "DELETE", by_id), f"DELETE {by_id}")

    async with world.superuser_engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT title, linked_to_closure FROM reports "
                    "WHERE report_id = :r"
                ),
                {"r": world.b.case.report_id},
            )
        ).first()
    assert row is not None, "A's refused DELETE removed B's report anyway"
    assert SECRET_B_REPORT in row[0], "A's refused PUT rewrote B's report anyway"
    assert row[1] is False, "A's refused link-case closed B's report anyway"


@phase3_pending
async def test_report_recommendations_do_not_confirm_the_other_partys_case(world):
    """A refusal must not distinguish "not yours" from "wrong state".

    B's own call gets 400 (the case is not RESOLVED) — it reached the state
    check. A's gets 404, having never resolved the case at all. The pair is the
    control: two different refusals, and only one of them tells the caller
    anything about the case.
    """
    path = f"/api/v1/cases/{world.b.case.case_id}/report-recommendations"

    control = await as_b(world, "GET", path)
    attack = await as_a(world, "GET", path)

    assert control.status_code == 400, (
        "control: B's own request no longer reaches the state check, so A's 404 "
        "no longer distinguishes anything"
    )
    assert attack.status_code == 404
    assert_no_b_content(attack, path)


@phase3_pending
async def test_conversion_jobs_and_drafts_are_refused_to_the_other_party(world):
    """The runbook-conversion surface: job listing, job detail, by-case, drafts."""
    listing = "/api/v1/knowledge/conversions"
    detail = f"{listing}/{world.b.conversion_id}"
    by_case = f"{listing}/by-case/{world.b.case.case_id}"
    drafts = "/api/v1/knowledge/drafts"

    control_list = await as_b(world, "GET", listing)
    assert control_list.status_code == 200
    assert world.b.conversion_id in _ids(control_list.json(), "conversion_id") | _ids(
        control_list.json(), "id"
    ), "control: B cannot list B's own conversion"

    control_drafts = await as_b(world, "GET", drafts)
    assert control_drafts.status_code == 200
    assert SECRET_B_DRAFT in control_drafts.text, "control: B's draft is missing"

    attack_list = await as_a(world, "GET", listing)
    assert attack_list.status_code == 200
    assert world.b.conversion_id not in _ids(
        attack_list.json(), "conversion_id"
    ) | _ids(attack_list.json(), "id")
    assert_no_b_content(attack_list, f"GET {listing}")

    attack_drafts = await as_a(world, "GET", drafts)
    assert attack_drafts.status_code == 200
    assert_no_b_content(attack_drafts, f"GET {drafts}")

    assert_refused(await as_a(world, "GET", detail), f"GET {detail}")
    assert_refused(await as_a(world, "GET", by_case), f"GET {by_case}")


@phase3_pending
async def test_a_conversion_draft_cannot_be_edited_or_discarded_across_the_wall(world):
    """The draft mutations, checked against the row rather than the status."""
    draft = (
        f"/api/v1/knowledge/conversions/{world.b.conversion_id}"
        f"/drafts/{world.b.draft_id}"
    )

    assert_refused(
        await as_a(world, "PUT", draft, json={"content": "PWNED"}), f"PUT {draft}"
    )
    assert_refused(await as_a(world, "DELETE", draft), f"DELETE {draft}")
    assert_refused(await as_a(world, "POST", f"{draft}/verify"), f"POST {draft}/verify")

    async with world.superuser_engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT title, status FROM conversion_drafts WHERE id = :d"),
                {"d": world.b.draft_id},
            )
        ).first()
    assert row is not None, "A's refused DELETE discarded B's draft anyway"
    assert SECRET_B_DRAFT in row[0]
    assert row[1] == "draft", "A's refused verify promoted B's draft anyway"


# =============================================================================
# Surface 4 — knowledge items, in every scope and through `resource_shares`
# =============================================================================


@phase3_pending
async def test_the_knowledge_inventory_shows_each_party_only_its_own(world):
    """Both scopes at once: the personal item and the team-shared one.

    The team item is the interesting half. It is reachable to its owner through
    the ``resource_shares`` arm, and that arm is an **allowlist the vector layer
    takes on trust** (#1168) — the only thing keeping another party's ids out of
    it is one SQL ``WHERE`` in the share lookup, which ADR-017 re-keys on the
    enterprise so a team can span organizations.
    """
    mine = await as_a(world, "GET", "/api/v1/knowledge/documents")
    theirs = await as_b(world, "GET", "/api/v1/knowledge/documents")

    assert theirs.status_code == 200
    listed_by_b = _ids(theirs.json(), "document_id")
    assert {world.b.kb_personal_id, world.b.kb_team_id} <= listed_by_b, (
        "control: B cannot list B's own knowledge items, so A listing none "
        "proves nothing"
    )

    assert mine.status_code == 200
    listed_by_a = _ids(mine.json(), "document_id")
    assert {world.a.kb_personal_id, world.a.kb_team_id} <= listed_by_a
    assert listed_by_a & {world.b.kb_personal_id, world.b.kb_team_id} == set()
    assert_no_b_content(mine, "GET /api/v1/knowledge/documents")


@phase3_pending
async def test_a_knowledge_item_is_not_readable_by_id_across_the_wall(world):
    """404, identical to an absent id: the refusal must not confirm existence."""
    for item_id in (world.b.kb_personal_id, world.b.kb_team_id):
        for suffix in ("", "/snippet"):
            path = f"/api/v1/knowledge/documents/{item_id}{suffix}"
            control = await as_b(world, "GET", path)
            attack = await as_a(world, "GET", path)

            assert control.status_code == 200, f"control: B cannot read {path}"
            assert attack.status_code == 404, (
                f"{path}: expected 404 (indistinguishable from absent), got "
                f"{attack.status_code}"
            )
            assert_no_b_content(attack, f"GET {path}")


@phase3_pending
async def test_a_knowledge_item_cannot_be_edited_or_deleted_across_the_wall(world):
    """The write half, checked against the row.

    Bulk delete is included because it takes a **list of ids** and reports per-id
    outcomes: a route that deletes what it can and reports the rest as "not
    found" would pass an aggregate status assertion.
    """
    item = f"/api/v1/knowledge/documents/{world.b.kb_team_id}"

    assert_refused(
        await as_a(world, "PUT", item, json={"title": "PWNED", "content": "x"}),
        f"PUT {item}",
    )
    assert_refused(await as_a(world, "DELETE", item), f"DELETE {item}")

    bulk_delete = await as_a(
        world,
        "POST",
        "/api/v1/knowledge/documents/bulk-delete",
        json={"document_ids": [world.b.kb_personal_id, world.b.kb_team_id]},
    )
    bulk_update = await as_a(
        world,
        "POST",
        "/api/v1/knowledge/documents/bulk-update",
        json={
            "document_ids": [world.b.kb_personal_id, world.b.kb_team_id],
            "updates": {"tags": ["pwned"]},
        },
    )
    assert bulk_delete.status_code in (200, 207, 403, 404)
    assert (
        bulk_delete.json().get("deleted_count", 0) == 0
    ), "bulk-delete removed another party's knowledge items"
    assert (
        bulk_update.json().get("updated_count", 0) == 0
    ), "bulk-update rewrote another party's knowledge items"
    assert_no_b_content(bulk_delete, "POST /knowledge/documents/bulk-delete")
    assert_no_b_content(bulk_update, "POST /knowledge/documents/bulk-update")

    async with world.superuser_engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT item_id, title, tags FROM knowledge_items "
                    "WHERE item_id IN (:p, :t)"
                ),
                {"p": world.b.kb_personal_id, "t": world.b.kb_team_id},
            )
        ).all()
    assert len(rows) == 2, "a refused write removed one of B's knowledge items"
    for _item_id, title, tags in rows:
        assert SECRET_B in title, "a refused write rewrote B's title"
        assert "pwned" not in (tags or []), "a refused bulk-update rewrote B's tags"


@phase3_pending
async def test_full_text_knowledge_search_does_not_reach_the_other_party(world):
    """The keyword arm, queried with B's marker string.

    The query itself is B's marker, so it is echoed in the response — which is
    exactly why this case asserts on the returned **document ids** rather than
    scanning the body.
    """
    body = {"query": SECRET_B_KB_TEAM, "limit": 50}
    control = await as_b(world, "POST", "/api/v1/knowledge/documents/search", json=body)
    attack = await as_a(world, "POST", "/api/v1/knowledge/documents/search", json=body)

    assert control.status_code == 200
    assert world.b.kb_team_id in _ids(
        control.json(), "document_id"
    ), "control: B's own full-text search does not find B's runbook"
    assert attack.status_code == 200
    assert (
        _ids(attack.json(), "document_id")
        & {world.b.kb_personal_id, world.b.kb_team_id}
        == set()
    )


@phase3_pending
async def test_semantic_kb_retrieval_does_not_reach_the_other_party(world):
    """The **vector** surface — the one #1168 says is derived, not enforced.

    ChromaDB carries no tenant dimension, so this cannot be waved through on the
    strength of the SQL surfaces above — and in the ``same_enterprise_no_team``
    arm there is no RLS separating the two parties at all, so the metadata filter
    is the entire boundary. Every chunk and every query is given the SAME
    embedding, so cosine similarity excludes nothing.
    """
    from faultmaven.infrastructure.knowledge import knowledge_vector_store as kvs

    body = {"query": "connection pool exhaustion in production", "limit": 50}
    with patch.object(kvs, "embed_query_or_raise", new=_fixed_embedding):
        control = await as_b(world, "POST", "/api/v1/knowledge/search", json=body)
        attack = await as_a(world, "POST", "/api/v1/knowledge/search", json=body)

    assert control.status_code == 200
    assert (
        "error" not in control.json()
    ), f"control: B's semantic search failed outright: {control.text[:300]}"
    assert SECRET_B_KB_PERSONAL in control.text or SECRET_B_KB_TEAM in control.text, (
        "control: the vector search found none of B's chunks even with a "
        "constant embedding, so A finding none proves nothing"
    )

    assert attack.status_code == 200
    assert_no_b_content(attack, "POST /api/v1/knowledge/search")


# =============================================================================
# Surface 5 — mutations and shares, asserted against the database
# =============================================================================
#
# Every case here checks the ROW after the refusal. A status code says what the
# caller was told; only the row says what happened. The specific failure this
# guards is a handler that answers 404 for the reader and still runs the write —
# exactly the shape a "delete is idempotent, absent is fine" refactor produces.


@phase3_pending
async def test_the_other_partys_case_survives_every_mutation_a_tries(world):
    """Update, close and delete, in that order, then read the row back."""
    case_id = world.b.case.case_id

    update = await as_a(
        world, "PUT", f"/api/v1/cases/{case_id}", json={"title": "PWNED"}
    )
    close = await as_a(
        world,
        "POST",
        f"/api/v1/cases/{case_id}/close",
        json={"closure_reason": "other"},
    )
    delete = await as_a(world, "DELETE", f"/api/v1/cases/{case_id}")

    assert_refused(update, f"PUT /api/v1/cases/{case_id}")
    assert_refused(close, f"POST /api/v1/cases/{case_id}/close")

    async with world.superuser_engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT title, state FROM cases WHERE case_id = :c"),
                {"c": case_id},
            )
        ).first()
    assert (
        row is not None
    ), f"A's DELETE (answered {delete.status_code}) removed B's case from the database"
    assert row[0] == SECRET_B_TITLE, "A's refused PUT renamed B's case"
    assert row[1] == "inquiry", "A's refused close moved B's case to terminal"

    control = await as_b(
        world,
        "PUT",
        f"/api/v1/cases/{case_id}",
        json={"title": "B renames its own case"},
    )
    assert (
        control.status_code == 200
    ), "control: B cannot update B's own case, so A's 404 proves nothing"


@phase3_pending
async def test_a_case_cannot_be_shared_into_the_other_partys_team(world):
    """``team-shares`` writes a ``resource_shares`` row — the KB allowlist arm.

    Two directions, and the second is the subtle one. Sharing B's case to A's
    team would be a straightforward theft; sharing **A's own** case into **B's**
    team plants a row in B's world, and #1168 records that any id reaching the
    shared arm is served by the vector layer verbatim. In the
    ``same_enterprise_no_team`` arm the planted row would even carry the right
    enterprise — consent, not the enterprise key, is what must refuse it.
    """
    steal = await as_a(
        world,
        "POST",
        f"/api/v1/cases/{world.b.case.case_id}/team-shares",
        json={"team_id": world.a.team_id},
    )
    plant = await as_a(
        world,
        "POST",
        f"/api/v1/cases/{world.a.case.case_id}/team-shares",
        json={"team_id": world.b.team_id},
    )
    unshare = await as_a(
        world,
        "DELETE",
        f"/api/v1/cases/{world.b.case.case_id}/team-shares/{world.b.team_id}",
    )

    assert_refused(steal, "POST .../team-shares (B's case -> A's team)")
    assert_refused(plant, "POST .../team-shares (A's case -> B's team)")
    assert_refused(unshare, "DELETE .../team-shares/{team_id}")

    async with world.superuser_engine.begin() as conn:
        planted = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM resource_shares "
                    "WHERE scope_id = :t AND resource_type = 'case'"
                ),
                {"t": world.b.team_id},
            )
        ).scalar()
    assert planted == 0, (
        "a refused share planted a resource_shares row in the other party's "
        "team; that row is an allowlist entry the vector layer serves verbatim"
    )


@phase3_pending
async def test_the_team_listing_never_names_the_other_partys_team(world):
    """``GET /teams`` — the input to every share-mediated read.

    If this listed another party's team, the share arm downstream would resolve
    ids from it, so this is upstream of the KB and case share paths rather than a
    surface of its own. In the ``same_enterprise_no_team`` arm both teams sit in
    the same enterprise, so membership — not the enterprise — is the whole of
    what keeps them apart.
    """
    mine = await as_a(world, "GET", "/api/v1/teams")
    theirs = await as_b(world, "GET", "/api/v1/teams")

    assert mine.status_code == 200 and theirs.status_code == 200
    assert _ids(mine.json(), "team_id") == {world.a.team_id}
    assert _ids(theirs.json(), "team_id") == {world.b.team_id}
    assert _ids(mine.json(), "enterprise_id") == {world.a.enterprise_id}


@phase3_pending
async def test_a_tenant_admin_reaches_no_admin_route_at_all(world):
    """``roles: ["user", "admin"]`` is an organization role. It buys nothing here.

    Run in BOTH wall arms on purpose: this is about the platform role, not about
    the wall. An organization admin inside the operator's own enterprise must be
    refused every platform route just as one across the wall is, and running it
    only in arm 1 would leave "admin means admin inside my own enterprise"
    untested.
    """
    routes = [
        ("GET", "/api/v1/admin/cases", None),
        ("GET", f"/api/v1/admin/cases/{world.b.case.case_id}", None),
        ("GET", f"/api/v1/admin/cases/{world.b.case.case_id}/messages", None),
        ("GET", "/api/v1/admin/grants", None),
        (
            "POST",
            "/api/v1/admin/grants",
            {
                "case_id": world.b.case.case_id,
                "enterprise_id": world.b.enterprise_id,
                "reason": "probe: an org admin asking for another party's case",
                "duration_minutes": 30,
            },
        ),
        ("GET", "/api/v1/admin/audit/operator-access", None),
        # The whole operator user-administration surface, not two of it. The
        # tenant predicate #1318 added narrows WHOSE accounts an operator
        # reaches; it must not be mistaken for the gate that decides WHO is an
        # operator, and a route that traded one for the other would still look
        # confined to every case in surface 5b.
        ("GET", "/api/v1/admin/users", None),
        ("GET", f"/api/v1/admin/users/{world.b.user_id}", None),
        ("POST", f"/api/v1/admin/users/{world.b.user_id}/deactivate", None),
        ("POST", f"/api/v1/admin/users/{world.b.user_id}/activate", None),
        ("POST", f"/api/v1/admin/users/{world.b.user_id}/roles", {"role": "admin"}),
        ("DELETE", f"/api/v1/admin/users/{world.b.user_id}/roles/member", None),
        ("GET", "/api/v1/auth/users", None),
        ("POST", f"/api/v1/auth/users/{world.b.user_id}/revoke-tokens", None),
        ("DELETE", f"/api/v1/auth/users/{world.b.user_id}", None),
    ]
    for method, path, body in routes:
        response = await as_a(world, method, path, json=body)
        assert response.status_code == 403, (
            f"{method} {path}: an organization admin was admitted to a platform "
            f"route ({response.status_code}): {response.text[:200]}"
        )
        assert_no_b_content(response, f"{method} {path}")


@phase3_pending
async def test_a_token_without_an_enterprise_claim_is_refused(world):
    """No claim, no tenant — and no fallback to the user row (ADR-017 D6).

    The ADR's "no data migration, no compatibility layer" rule makes this an
    assertion rather than an aspiration: the enterprise claim is the only
    isolation input from day one, and a binder that derived the enterprise from
    ``users.enterprise_id`` when the claim was absent would honour every token
    minted before the cutover — which is the whole of what the rule forbids, and
    reason enough on its own.

    Not a nullability argument. ``users.enterprise_id`` was widened to nullable
    by migration 052 for personal-*organization* retirement, and the Phase-1
    baseline restores it NOT NULL along with the path 052 served (the schema
    check pins that). So the column is sound under this contract; the fallback
    is still wrong, because what it honours is the token, not the row.

    The control is A's ordinary token on the identical call — otherwise a 403
    from a broken route would look like the guard working.
    """
    claimless = _forge_token(
        world.app.state.auth_service,
        user_id=world.a.user_id,
        enterprise_id=None,
        organization_id=None,
        email=f"{world.a.user_id}@example.com",
        roles=["user"],
    )

    control = await as_a(world, "GET", "/api/v1/cases")
    refused = await _call(world, claimless, "GET", "/api/v1/cases")

    assert control.status_code == 200, (
        "control: A's ordinary token is refused too, so the claim-less token's "
        f"refusal says nothing: {control.text[:300]}"
    )
    assert refused.status_code in (401, 403), (
        "a token carrying no enterprise_id claim was served "
        f"({refused.status_code}): {refused.text[:300]}"
    )
    assert_no_b_content(refused, "GET /api/v1/cases (no enterprise claim)")


# =============================================================================
# Surface 6 — the rest of the case-addressed operations, as one battery
# =============================================================================
#
# These are the operations whose only handle is ``{case_id}`` in the path. They
# are driven with bodies valid enough to get PAST request validation: a 422 for a
# missing field is not a refusal, it is an assertion that never ran.

CASE_ADDRESSED_OPERATIONS = [
    # ``turns`` is a multipart form, not JSON. Sent as ``data=`` so the query
    # field actually binds and the request reaches the case check instead of
    # failing its own validation.
    pytest.param(
        "POST",
        "/api/v1/cases/{case_id}/turns",
        {"__form__": {"query": "probe"}},
        id="turns",
    ),
    pytest.param("POST", "/api/v1/cases/{case_id}/title", {}, id="title"),
    pytest.param(
        "POST", "/api/v1/cases/{case_id}/extract-knowledge", {}, id="extract-knowledge"
    ),
    pytest.param(
        "POST",
        "/api/v1/cases/{case_id}/reports",
        {"report_type": "closure_summary"},
        id="reports-generate",
    ),
    pytest.param("POST", "/api/v1/cases/{case_id}/sessions", {}, id="session-create"),
    pytest.param("GET", "/api/v1/cases/{case_id}/sessions", None, id="session-list"),
    pytest.param(
        "GET", "/api/v1/cases/{case_id}/sessions/active", None, id="session-active"
    ),
    pytest.param(
        "GET",
        "/api/v1/cases/{case_id}/sessions/sess_probe_0001",
        None,
        id="session-get",
    ),
    pytest.param(
        "PATCH",
        "/api/v1/cases/{case_id}/sessions/sess_probe_0001",
        {},
        id="session-patch",
    ),
    pytest.param(
        "POST",
        "/api/v1/cases/{case_id}/sessions/sess_probe_0001/pause",
        {},
        id="session-pause",
    ),
    pytest.param(
        "POST",
        "/api/v1/cases/{case_id}/sessions/sess_probe_0001/resume",
        {},
        id="session-resume",
    ),
    pytest.param(
        "POST",
        "/api/v1/cases/{case_id}/sessions/sess_probe_0001/complete",
        {},
        id="session-complete",
    ),
    pytest.param(
        "POST",
        "/api/v1/cases/sessions/sess_probe_0001/resume/{case_id}",
        {},
        id="session-resume-case",
    ),
]


@phase3_pending
@pytest.mark.parametrize("method,template,body", CASE_ADDRESSED_OPERATIONS)
async def test_every_case_addressed_operation_refuses_the_other_party(
    world, method, template, body
):
    """One battery over the remaining ``{case_id}`` operations.

    ``app.state.investigation_service`` is a tripwire (see :class:`_Tripwire`), so
    a route that resolved the engine before checking the case would answer 500
    rather than a refusal — which this assertion rejects. That is what keeps the
    battery honest for the write paths whose collaborators are not wired here:
    they cannot pass by being unavailable.

    The control is deliberately weak, and weak is what it can be: B's identical
    call on B's OWN case must not be 422. Nothing stronger is assertable — most
    of these operations need collaborators this module does not wire, so their
    owner-side status is unpredictable (200, 400, 404, 500 are all legitimate
    here) and pinning one would make the battery a test of the engine. But 422 is
    different: it means the BODY never validated, so the request died before the
    tenant check and A's refusal above measured request parsing rather than the
    boundary. That is the exact way a battery like this rots — a response model
    gains a required field and every parametrisation quietly starts asserting
    nothing.
    """
    # One path: B's case is both the attacker's target and the control's own
    # case, so the two calls differ ONLY in who makes them.
    path = template.format(case_id=world.b.case.case_id)
    if body is not None and "__form__" in body:
        response = await as_a(world, method, path, data=body["__form__"])
        control = await as_b(world, method, path, data=body["__form__"])
    else:
        response = await as_a(world, method, path, json=body)
        control = await as_b(world, method, path, json=body)

    assert control.status_code != 422, (
        f"control: {method} {path} answered 422 to its OWN case's owner, so the "
        f"body never reached the tenant check and the refusal below is a "
        f"validation error rather than the boundary: {control.text[:300]}"
    )
    assert_refused(response, f"{method} {path}")


@phase3_pending
async def test_a_refused_cross_wall_turn_charges_nobodys_daily_allowance(world):
    """The per-tenant turn cap must not be spendable by probing (ADR-016 D5.3).

    A's turn at B's case is refused by the case check. The cap is charged inside
    ``InvestigationService.process_turn``, *after* that check, so nothing should
    be written for either party — and this is the case that says so, because when
    the cap was a route dependency it ran BEFORE the check and the battery above
    silently spent a unit of A's day on every parametrisation.

    Read back as the OWNER, so "no row" cannot be RLS hiding one. Under ADR-017
    the ledger is keyed on a **billing subject** rather than an organization
    (D5), and in these arms neither party is in an organization at all, so the
    subject is the account.
    """
    path = f"/api/v1/cases/{world.b.case.case_id}/turns"
    response = await as_a(world, "POST", path, data={"query": "probe"})
    assert_refused(response, f"POST {path}")

    async with world.superuser_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT billing_subject_kind, billing_subject_id, turn_count "
                    "FROM turn_usage WHERE billing_subject_id IN (:a, :b)"
                ),
                {"a": world.a.user_id, "b": world.b.user_id},
            )
        ).fetchall()

    assert rows == [], (
        "a refused cross-wall turn wrote a usage row — the cap is being charged "
        f"before the case check: {rows}"
    )


@phase3_pending
async def test_report_generation_by_case_id_refuses_the_other_party(world):
    """``POST /reports/generate?case_id=...`` — the case id rides in the query.

    The control is the *difference*: B's call reaches the generation service (and
    fails there, unwired), A's never resolves the case at all. Two different
    failures, and only one of them is the boundary.
    """
    path = f"/api/v1/reports/generate?case_id={world.b.case.case_id}"
    body = {"report_types": ["closure_summary"]}

    control = await as_b(world, "POST", path, json=body)
    attack = await as_a(world, "POST", path, json=body)

    assert control.status_code != 404, (
        "control: B's own generate request now 404s too, so A's 404 no longer "
        "distinguishes the boundary from a broken route"
    )
    assert attack.status_code == 404
    assert_no_b_content(attack, f"POST {path}")


@phase3_pending
async def test_the_debug_causal_graph_answers_content_to_one_party_only(world):
    """A development-only route that is NOT in the published contract.

    It answers 200 to any AUTHENTICATED caller, so the status code says little —
    which is why this case reads the body. B gets the graph; A gets ``case not
    found`` in a 200 envelope. Included precisely because the generator excludes
    debug endpoints from ``openapi.json``: a route absent from the contract is
    still a route, and this is the one that carries a ``{case_id}``.
    """
    path = f"/debug/cases/{world.b.case.case_id}/causal-graph"

    control = await as_b(world, "GET", path)
    attack = await as_a(world, "GET", path)

    assert control.status_code == 200
    assert (
        control.json().get("error") is None
    ), f"control: B cannot read B's own causal graph: {control.text[:300]}"
    assert "causal_nodes" in control.json()

    assert attack.status_code == 200
    assert attack.json().get("error") == "case not found"
    assert "causal_nodes" not in attack.json()
    assert_no_b_content(attack, f"GET {path}")


@phase3_pending
async def test_a_case_created_by_a_lands_in_as_enterprise_and_no_organization(world):
    """Creation, the one write where the tenant is chosen rather than checked.

    Two assertions, and they are about two different columns. ``enterprise_id``
    is isolation and must be A's — anything else is a row in someone else's
    world, including the Standalone sentinel. ``organization_id`` is billing
    attribution, and in these arms no organization exists, so it must be NULL
    rather than back-filled from a sentinel or from the enterprise: a NOT NULL
    left over from the old schema would force a value nobody is paying.
    """
    created = await as_a(
        world,
        "POST",
        "/api/v1/cases",
        json={
            "title": f"{SECRET_A}-created-through-the-api",
            "description": "probe",
            "severity": "medium",
        },
    )
    assert (
        created.status_code == 201
    ), f"control: A cannot create a case: {created.text[:300]}"
    case_id = created.json()["case_id"]

    try:
        async with world.superuser_engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT enterprise_id, organization_id FROM cases "
                        "WHERE case_id = :c"
                    ),
                    {"c": case_id},
                )
            ).first()
        assert row[0] == world.a.enterprise_id, (
            f"a case created by A was stamped enterprise {row[0]!r}; anything "
            "but A's own enterprise is a row in someone else's world"
        )
        assert row[1] is None, (
            f"a case created by A in no organization was stamped {row[1]!r} for "
            "billing; the column is nullable attribution, not a tenant key"
        )
    finally:
        async with world.superuser_engine.begin() as conn:
            await _delete_case_rows(conn, [case_id])


@phase3_pending
async def test_the_case_list_team_filter_cannot_name_the_other_partys_team(world):
    """``GET /cases?team_id=`` — a caller-supplied team id on a read.

    The team arm of the case allowlist resolves through ``resource_shares``. A
    filter that narrows within the caller's own teams is a feature; one that
    *selects* a team by id is a read across the wall, so the attacker names B's.
    """
    attack = await as_a(world, "GET", f"/api/v1/cases?team_id={world.b.team_id}")

    assert attack.status_code in (200, 403, 422)
    if attack.status_code == 200:
        assert world.b.case.case_id not in _ids(attack.json(), "case_id")
    assert_no_b_content(attack, "GET /api/v1/cases?team_id=<B's team>")


@phase3_pending
async def test_a_runbook_cannot_be_published_into_the_other_partys_team(world):
    """``POST /knowledge/runbooks/create`` with ``team_id`` naming B's team.

    A publish, not a read — and the dangerous direction. The team id becomes a
    ``resource_shares`` row, and #1168 records that any id reaching the shared arm
    is served by the vector layer verbatim; a row planted in B's team would put
    A's content inside B's KB reads. Verified against the rows, not the status,
    and the same guard covers ``POST /knowledge/convert``, whose ``team_id``
    travels the identical path.
    """
    body = {
        "title": f"{SECRET_A} planted runbook",
        "domain": "database",
        "service": "postgres",
        "symptom_class": ["timeout"],
        "severity": "medium",
        "scope": "team",
        "team_id": world.b.team_id,
        "symptom_recognition": "connections time out under load",
        "applicability": "postgres 16 on bare metal",
        "diagnostic_steps": "1. check pool saturation",
        "causes": "pool exhausted",
        "prevention": "raise the pool ceiling",
    }
    attack = await as_a(world, "POST", "/api/v1/knowledge/runbooks/create", json=body)

    assert attack.status_code in REFUSED, (
        f"a runbook was published into another party's team "
        f"({attack.status_code}): {attack.text[:300]}"
    )

    # Both halves ask "what could this call have written?", which is why neither
    # is a bare count. A's two SEEDED runbooks are owned by A and carry SECRET_A
    # in their titles, so a title-and-owner count can never reach zero and would
    # be a green assertion measuring nothing; the item ids are excluded instead.
    # The share half is scoped to knowledge_item shares so the case-share the
    # neighbouring test plants (or fails to) cannot move this number.
    async with world.superuser_engine.begin() as conn:
        planted = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM resource_shares "
                    "WHERE scope_id = :t AND resource_type = 'knowledge_item'"
                ),
                {"t": world.b.team_id},
            )
        ).scalar()
        foreign_items = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM knowledge_items "
                    "WHERE owner_id = :o AND item_id NOT IN (:seeded_p, :seeded_t)"
                ),
                {
                    "o": world.a.user_id,
                    "seeded_p": world.a.kb_personal_id,
                    "seeded_t": world.a.kb_team_id,
                },
            )
        ).scalar()
    assert planted == 1, (
        "a refused publish added a knowledge_item share to the other party's team "
        f"(expected only the seeded one, found {planted})"
    )
    assert foreign_items == 0, (
        "a refused publish wrote a knowledge item anyway: A owns "
        f"{foreign_items} item(s) beyond the two this world seeded"
    )

    control = await as_b(
        world,
        "POST",
        "/api/v1/knowledge/runbooks/create",
        json={**body, "title": f"{SECRET_B} own runbook", "team_id": world.b.team_id},
    )
    assert control.status_code in (200, 201), (
        "control: B cannot publish into B's own team either, so A's refusal is "
        f"not attributable to the boundary: {control.text[:300]}"
    )


# =============================================================================
# Surface 7 — the operator surfaces (arm 1 only)
# =============================================================================
#
# ``Role.ADMIN`` is an ORGANIZATION role — under ADR-017 a *billing* management
# role, which by D2 grants nothing about data. ``platform_admin`` is the
# deployment-wide operator role, and ADR-012 D9 (as amended) gives it a
# deliberate, audited path to tenant content through a break-glass grant keyed on
# the ENTERPRISE. So the invariant on these routes is not "nobody sees B": it is
# that an organization admin never does, and that an operator does so only
# through a grant naming exactly one case, with a row in the append-only audit
# trail to show for it.
#
# All of this runs in arm 1 only. Operator confinement is an enterprise
# predicate; inside one enterprise (arm 3) it admits both parties by design, and
# a test asserting otherwise would be asserting a boundary the ADR does not
# claim.


@phase3_pending
async def test_knowledge_suggestions_are_scoped_to_the_operators_own_enterprise(
    wall_world,
):
    """The review inbox. The operator role says WHAT you may do, not WHOSE."""
    world = wall_world
    path = "/api/v1/knowledge/suggestions"

    org_admin = await as_a(world, "GET", path)
    assert org_admin.status_code == 403
    assert_no_b_content(org_admin, f"GET {path} (organization admin)")

    operator = await _call(world, world.token_operator_a, "GET", path)
    assert operator.status_code == 200
    assert world.b.suggestion_id not in _ids(operator.json(), "suggestion_id")
    assert_no_b_content(operator, f"GET {path} (platform operator bound to E_A)")

    # Bodies are filled in so each call passes its own validation and actually
    # reaches the tenant resolution. A 400 for a missing field would "refuse" the
    # attack without ever consulting the boundary.
    id_addressed = (
        ("GET", "", None),
        ("PUT", "", {"suggested_title": "PWNED", "suggested_content": "PWNED"}),
        ("POST", "/approve", {}),
        ("POST", "/reject", {"rejection_reason": "probe"}),
        ("POST", "/remediate-pii", {}),
    )
    for method, suffix, json_body in id_addressed:
        target = f"{path}/{world.b.suggestion_id}{suffix}"
        response = await _call(
            world, world.token_operator_a, method, target, json=json_body
        )
        assert response.status_code in REFUSED, (
            f"{method} {target}: a platform operator bound to E_A reached B's "
            f"suggestion ({response.status_code}): {response.text[:300]}"
        )
        assert_no_b_content(response, f"{method} {target}")

    async with world.superuser_engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status FROM knowledge_suggestions WHERE suggestion_id = :s"
                ),
                {"s": world.b.suggestion_id},
            )
        ).first()
    assert (
        row is not None and row[0] == "pending_review"
    ), "a refused approve/reject changed B's suggestion anyway"


@phase3_pending
async def test_the_cross_tenant_case_listing_is_refused_under_multi_tenant(wall_world):
    """``/admin/cases`` refuses rather than serving an RLS-truncated answer.

    The failure this guards is not a leak but its mirror: a list that claims to
    span every tenant while RLS silently scopes it to the operator's own. An
    operator triaging "which tenant is stuck" would be misled precisely when the
    endpoint matters.
    """
    world = wall_world
    response = await _call(world, world.token_operator_a, "GET", "/api/v1/admin/cases")

    assert response.status_code == 403
    assert world.b.case.case_id not in response.text
    assert_no_b_content(response, "GET /api/v1/admin/cases")


@phase3_pending
async def test_an_operator_without_a_grant_cannot_open_another_enterprises_case(
    wall_world,
):
    """No grant, no content — and the refusal must not confirm the case exists.

    A 403 that reads "you need a grant for THIS case" is the same text whether
    the case exists or not; what would leak is a 404 for absent ids and a 403 for
    present ones. Both ids are tried here for exactly that reason.
    """
    world = wall_world
    real = f"/api/v1/admin/cases/{world.b.case.case_id}"
    absent = f"/api/v1/admin/cases/case_{'0' * 12}"

    for path in (real, f"{real}/messages", absent, f"{absent}/messages"):
        response = await _call(world, world.token_operator_a, "GET", path)
        assert response.status_code == 403, (
            f"{path}: content was served without a live break-glass grant "
            f"({response.status_code})"
        )
        assert_no_b_content(response, f"GET {path}")


@phase3_pending
async def test_a_break_glass_grant_unlocks_exactly_the_case_it_names(wall_world):
    """The audited escape hatch — and its bound.

    ADR-012 D9 *intends* a platform operator to reach tenant content through a
    grant, so "the operator read B's case" is not the finding here. The finding
    would be a grant that reaches further than the case it names: this asserts the
    granted case opens, a SECOND case in the SAME enterprise stays shut, and the
    access is recorded in the append-only audit trail — which ADR-017 re-keys on
    ``target_enterprise_id``.
    """
    world = wall_world
    second = await _seed_case_with_content(
        enterprise_id=world.b.enterprise_id,
        organization_id=None,
        user_id=world.b.user_id,
        title=f"{SECRET_B}-second-case",
        secret_prefix=f"{SECRET_B}-second",
    )
    try:
        minted = await _call(
            world,
            world.token_operator_a,
            "POST",
            "/api/v1/admin/grants",
            json={
                "case_id": world.b.case.case_id,
                "enterprise_id": world.b.enterprise_id,
                "reason": "probe: exercising the audited break-glass path",
                "duration_minutes": 30,
            },
        )
        assert minted.status_code == 201, (
            "control: the grant could not be minted, so the bound this case "
            f"asserts is untested ({minted.status_code}): {minted.text[:300]}"
        )

        granted = await _call(
            world,
            world.token_operator_a,
            "GET",
            f"/api/v1/admin/cases/{world.b.case.case_id}",
        )
        assert granted.status_code == 200, (
            "control: a live grant did not open the case it names, so the "
            "refusal below cannot be attributed to scoping"
        )
        assert SECRET_B_TITLE in granted.text

        other = await _call(
            world,
            world.token_operator_a,
            "GET",
            f"/api/v1/admin/cases/{second.case_id}",
        )
        assert other.status_code == 403, (
            "a break-glass grant for one case opened a DIFFERENT case in the "
            f"same enterprise ({other.status_code}): {other.text[:300]}"
        )
        assert f"{SECRET_B}-second-case" not in other.text

        async with world.superuser_engine.begin() as conn:
            trail = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM operator_access_audit "
                        "WHERE target_case_id = :c"
                    ),
                    {"c": world.b.case.case_id},
                )
            ).scalar()
        assert trail and trail >= 1, (
            "the break-glass read left no row in operator_access_audit; the "
            "escape hatch is only acceptable because it is recorded"
        )
    finally:
        async with world.superuser_engine.begin() as conn:
            await _delete_case_rows(conn, [second.case_id])


@phase3_pending
async def test_an_operator_cannot_read_another_enterprises_user(wall_world):
    """``GET /admin/users/{user_id}`` resolves inside the operator's enterprise.

    The refusal is compared against the one an id naming NOBODY gets. A 404 that
    reads differently for a real account in another enterprise is an existence
    oracle, and the id the caller sent is the only thing allowed to differ.
    """
    world = wall_world
    absent = f"user_absent_{_RUN}"

    foreign = await _call(
        world, world.token_operator_a, "GET", f"/api/v1/admin/users/{world.b.user_id}"
    )
    nobody = await _call(
        world, world.token_operator_a, "GET", f"/api/v1/admin/users/{absent}"
    )

    assert_refused(foreign, f"GET /api/v1/admin/users/{world.b.user_id}")
    assert foreign.status_code == nobody.status_code
    assert foreign.text.replace(world.b.user_id, "<id>") == nobody.text.replace(
        absent, "<id>"
    ), (
        "the out-of-enterprise refusal is distinguishable from the absent-id "
        f"one: {foreign.text[:200]} vs {nobody.text[:200]}"
    )
    assert f"{world.b.user_id}@example.com" not in foreign.text

    # And still no audit row. The refusal means there is nothing to audit; the
    # audited break-glass model for this surface is ADR-012 D9's option A, which
    # #1318 deliberately did not half-build.
    async with world.superuser_engine.begin() as conn:
        trail = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM operator_access_audit "
                    "WHERE target_enterprise_id = :e"
                ),
                {"e": world.b.enterprise_id},
            )
        ).scalar()
    assert trail == 0


@phase3_pending
async def test_an_operator_cannot_mutate_another_enterprises_user_account(wall_world):
    """The same surface writes, too — and none of the writes land.

    Asserted against the ROW, not the response: a route that answers 404 after
    mutating is a worse bug than one that answers 200, and only the database can
    tell them apart. Every id-addressed operation on the surface is swept, so a
    predicate applied to six of seven cannot pass here.
    """
    world = wall_world
    operations = [
        ("POST", f"/api/v1/admin/users/{world.b.user_id}/deactivate", None),
        ("POST", f"/api/v1/admin/users/{world.b.user_id}/activate", None),
        ("POST", f"/api/v1/admin/users/{world.b.user_id}/roles", {"role": "admin"}),
        ("DELETE", f"/api/v1/admin/users/{world.b.user_id}/roles/member", None),
        ("POST", f"/api/v1/auth/users/{world.b.user_id}/revoke-tokens", None),
        ("DELETE", f"/api/v1/auth/users/{world.b.user_id}", None),
    ]

    for method, path, body in operations:
        response = await _call(world, world.token_operator_a, method, path, json=body)
        assert_refused(response, f"{method} {path}")

    async with world.superuser_engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT is_active, dev_roles FROM users WHERE user_id = :u"),
                {"u": world.b.user_id},
            )
        ).first()

    assert row is not None, "the cross-enterprise DELETE removed B's account"
    is_active, dev_roles = row
    assert is_active is True, "the cross-enterprise deactivation landed"
    assert "admin" not in (dev_roles or ""), "the cross-enterprise role change landed"


@phase3_pending
async def test_the_operator_still_administers_their_own_enterprises_users(wall_world):
    """The positive control. Refusing everything would pass the two above."""
    world = wall_world
    detail = await _call(
        world, world.token_operator_a, "GET", f"/api/v1/admin/users/{world.a.user_id}"
    )
    assert detail.status_code == 200, (
        "control: the operator was refused a user of their OWN enterprise "
        f"({detail.status_code}): {detail.text[:300]}"
    )
    assert detail.json()["email"] == f"{world.a.user_id}@example.com"

    deactivate = await _call(
        world,
        world.token_operator_a,
        "POST",
        f"/api/v1/admin/users/{world.a.user_id}/deactivate",
    )
    assert deactivate.status_code == 200, (
        "control: deactivating a user of the operator's OWN enterprise was "
        f"refused ({deactivate.status_code}): {deactivate.text[:300]}"
    )

    async with world.superuser_engine.begin() as conn:
        is_active = (
            await conn.execute(
                text("SELECT is_active FROM users WHERE user_id = :u"),
                {"u": world.a.user_id},
            )
        ).scalar()
    assert is_active is False, "control: the 200 did not deactivate the account"


@phase3_pending
async def test_neither_user_listing_names_or_counts_the_other_enterprise(wall_world):
    """The two listings, including the ``total`` — itself a disclosure.

    #1318 measured ``GET /auth/users`` answering a deployment-wide population
    count while ``/admin/users`` served another tenant's rows stamped with the
    CALLER's id. Under ADR-017 the confinement predicate is the enterprise, and
    membership in it is ``users.enterprise_id`` rather than a roster table.
    """
    world = wall_world
    admin_list = await _call(
        world, world.token_operator_a, "GET", "/api/v1/admin/users"
    )
    auth_list = await _call(world, world.token_operator_a, "GET", "/api/v1/auth/users")

    own_members = set(world.a.enterprise_members)
    for name, response in (("admin", admin_list), ("auth", auth_list)):
        assert response.status_code == 200, f"{name}: {response.text[:300]}"
        assert (
            world.b.user_id not in response.text
        ), f"{name} listing names a user of the other enterprise"
        # The control. An empty listing satisfies "does not name B" trivially,
        # and this deployment carries rows written by every other module in the
        # run — so what matters is that A's OWN user is still here.
        assert world.a.user_id in response.text, (
            f"control: the {name} listing does not name the operator's own "
            f"enterprise's user, so 'B is absent' proves nothing: "
            f"{response.text[:300]}"
        )
        assert response.json()["total"] <= len(own_members), (
            f"{name} listing reports a total larger than its own enterprise: "
            f"{response.text[:300]}"
        )

    # Every row the admin listing stamps with the caller's enterprise really is
    # in it.
    for row in admin_list.json()["users"]:
        assert row["enterprise_id"] == world.a.enterprise_id
        assert row["user_id"] in own_members


@phase3_pending
async def test_the_grant_listing_filter_cannot_name_another_enterprise(wall_world):
    """``GET /admin/grants?enterprise_id=...`` — a caller-supplied filter.

    The operator mints a grant over their OWN enterprise, then asks for grants in
    B's. A filter that *narrows* the operator's own rows is harmless; one that
    *selects* by enterprise is a read across the wall, and the control (the same
    call filtered to A) is what tells the two apart.
    """
    world = wall_world
    minted = await _call(
        world,
        world.token_operator_a,
        "POST",
        "/api/v1/admin/grants",
        json={
            "case_id": world.a.case.case_id,
            "enterprise_id": world.a.enterprise_id,
            "reason": "probe: a grant over the operator's own enterprise",
            "duration_minutes": 30,
        },
    )
    assert minted.status_code == 201, f"control: no grant minted: {minted.text[:300]}"
    grant_id = minted.json()["grant_id"]

    own = await _call(
        world,
        world.token_operator_a,
        "GET",
        f"/api/v1/admin/grants?enterprise_id={world.a.enterprise_id}",
    )
    other = await _call(
        world,
        world.token_operator_a,
        "GET",
        f"/api/v1/admin/grants?enterprise_id={world.b.enterprise_id}",
    )

    assert own.status_code == 200
    assert grant_id in _ids(own.json(), "grant_id"), (
        "control: the operator's own grant is not listed under its own filter, "
        "so an empty answer for B's enterprise proves nothing"
    )
    assert other.status_code == 200
    assert _ids(other.json(), "grant_id") == set()
    assert_no_b_content(other, "GET /api/v1/admin/grants?enterprise_id=<B>")


# =============================================================================
# Surface 8 — arm 2: one enterprise, two billing organizations, one shared team
# =============================================================================
#
# The row ADR-017 adds to the matrix, and the one no probe could answer before
# it: two organizations inside one enterprise, one shared team. Both principals
# carry the SAME enterprise claim, so RLS admits both rows to both sessions and
# every assertion below is a statement about the application share layer alone.
#
# Every case pairs the shared row with the private one on the SAME call by the
# SAME actor. The shared row's presence is the positive control; the private
# row's absence is the assertion. Neither is worth anything without the other:
# a share that never worked and a boundary that leaks look identical to a probe
# that only checks one of them.


async def as_owner(world, method: str, path: str, **kwargs):
    """A — the owner of both rows, and the one who consented to the share."""
    return await _call(world, world.token_a, method, path, **kwargs)


async def as_teammate(world, method: str, path: str, **kwargs):
    """B — same enterprise, DIFFERENT organization, same team as A."""
    return await _call(world, world.token_b, method, path, **kwargs)


@phase3_pending
async def test_the_case_list_shows_the_shared_case_and_not_the_private_one(
    shared_world,
):
    """The collection read, across a billing boundary that must not matter."""
    world = shared_world
    listing = await as_teammate(world, "GET", "/api/v1/cases")

    assert listing.status_code == 200
    listed = _ids(listing.json(), "case_id")
    assert world.shared_case.case_id in listed, (
        "control: the team share is invisible to a teammate, so the private "
        "case's absence proves nothing"
    )
    assert world.private_case.case_id not in listed
    assert_no_private_content(listing, "GET /api/v1/cases (teammate)")


@phase3_pending
async def test_detail_ui_and_transcript_follow_the_share(shared_world):
    """The id-addressed reads: shared opens, private is refused, per surface."""
    world = shared_world
    for suffix, marker in (
        ("", SHARED_TITLE),
        ("/ui", None),
        ("/messages", SHARED_TRANSCRIPT),
    ):
        shared = await as_teammate(
            world, "GET", f"/api/v1/cases/{world.shared_case.case_id}{suffix}"
        )
        private = await as_teammate(
            world, "GET", f"/api/v1/cases/{world.private_case.case_id}{suffix}"
        )
        assert shared.status_code == 200, (
            f"control: a teammate cannot read the SHARED case{suffix} "
            f"({shared.status_code}): {shared.text[:300]}"
        )
        if marker:
            assert marker in shared.text, f"control: {suffix} carried no marker"
        assert private.status_code in REFUSED, (
            f"a teammate reached A's UNSHARED case{suffix} "
            f"({private.status_code}): {private.text[:300]}"
        )
        assert_no_private_content(private, f"GET /api/v1/cases/<private>{suffix}")


@phase3_pending
async def test_case_search_finds_the_shared_case_and_not_the_private_one(shared_world):
    """Search: the caller supplies the term, so a leak looks like a good result."""
    world = shared_world
    found = await as_teammate(
        world, "POST", "/api/v1/cases/search", json={"query": SHARED_TITLE, "limit": 50}
    )
    hunted = await as_teammate(
        world,
        "POST",
        "/api/v1/cases/search",
        json={"query": PRIVATE_TITLE, "limit": 50},
    )

    assert found.status_code == 200
    assert world.shared_case.case_id in _ids(found.json(), "case_id"), (
        "control: a teammate's search does not find the shared case, so not "
        "finding the private one proves nothing"
    )
    assert hunted.status_code == 200
    assert world.private_case.case_id not in _ids(hunted.json(), "case_id")
    assert_no_private_content(hunted, "POST /api/v1/cases/search (teammate)")

    # The same query with every declared selector naming A: A's own id, A's
    # BILLING organization (which really does stamp the private case) and A's
    # own team (which B is not in). This is the arm where those values are real
    # rather than forged, so a repository that still selected on any of them
    # would hand B the private case here and nowhere else.
    injected = await as_teammate(
        world,
        "POST",
        "/api/v1/cases/search",
        json=_search_injection_body(
            query=PRIVATE_TITLE,
            user_id=world.user_a,
            organization_id=world.org_x,
            team_id=world.team_a_own,
        ),
    )
    assert injected.status_code in (200, 403, 422)
    assert world.private_case.case_id not in _ids(
        injected.json() if injected.status_code == 200 else [], "case_id"
    )
    assert_no_private_content(
        injected, "POST /api/v1/cases/search (teammate, owner ids injected)"
    )


@phase3_pending
async def test_the_team_filter_lists_the_shared_case_only(shared_world):
    """``GET /cases?team_id=T`` — the facet a share badge is rendered from."""
    world = shared_world
    filtered = await as_teammate(
        world, "GET", f"/api/v1/cases?team_id={world.team_shared}"
    )

    assert filtered.status_code == 200
    listed = _ids(filtered.json(), "case_id")
    assert world.shared_case.case_id in listed, (
        "control: filtering by the shared team returns nothing, so the private "
        "case's absence proves nothing"
    )
    assert world.private_case.case_id not in listed
    assert_no_private_content(filtered, "GET /api/v1/cases?team_id=T")


@phase3_pending
async def test_the_kb_inventory_and_by_id_read_follow_the_team_share(shared_world):
    """The knowledge surfaces, listing and id-addressed."""
    world = shared_world
    inventory = await as_teammate(world, "GET", "/api/v1/knowledge/documents")

    assert inventory.status_code == 200
    listed = _ids(inventory.json(), "document_id")
    assert world.kb_shared_id in listed, (
        "control: the shared runbook is not in a teammate's inventory, so the "
        "personal one's absence proves nothing"
    )
    assert world.kb_private_id not in listed
    assert_no_private_content(inventory, "GET /api/v1/knowledge/documents")

    for suffix in ("", "/snippet"):
        shared = await as_teammate(
            world, "GET", f"/api/v1/knowledge/documents/{world.kb_shared_id}{suffix}"
        )
        private = await as_teammate(
            world, "GET", f"/api/v1/knowledge/documents/{world.kb_private_id}{suffix}"
        )
        assert shared.status_code == 200, (
            f"control: a teammate cannot read the shared runbook{suffix}: "
            f"{shared.text[:300]}"
        )
        assert private.status_code == 404, (
            f"the personal runbook{suffix} answered {private.status_code} to a "
            "teammate; it must be indistinguishable from an absent id"
        )
        assert_no_private_content(
            private, f"GET /api/v1/knowledge/documents/<private>{suffix}"
        )


@phase3_pending
async def test_full_text_and_semantic_kb_search_follow_the_team_share(shared_world):
    """Both retrieval arms, keyword and vector.

    The vector arm is the one that matters most here: ChromaDB carries no tenant
    dimension at all, and in this arm it carries no *enterprise* separation
    either — both runbooks belong to one enterprise and one owner. The share
    allowlist resolved in SQL is the whole of the boundary. Every chunk and query
    is given the same embedding, so similarity excludes nothing.
    """
    from faultmaven.infrastructure.knowledge import knowledge_vector_store as kvs

    world = shared_world
    lexical_shared = await as_teammate(
        world,
        "POST",
        "/api/v1/knowledge/documents/search",
        json={"query": SHARED_KB, "limit": 50},
    )
    lexical_private = await as_teammate(
        world,
        "POST",
        "/api/v1/knowledge/documents/search",
        json={"query": PRIVATE_KB, "limit": 50},
    )
    assert lexical_shared.status_code == 200
    assert world.kb_shared_id in _ids(lexical_shared.json(), "document_id"), (
        "control: a teammate's full-text search does not find the shared "
        "runbook, so not finding the personal one proves nothing"
    )
    assert lexical_private.status_code == 200
    assert world.kb_private_id not in _ids(lexical_private.json(), "document_id")

    body = {"query": "connection pool exhaustion in production", "limit": 50}
    with patch.object(kvs, "embed_query_or_raise", new=_fixed_embedding):
        semantic = await as_teammate(
            world, "POST", "/api/v1/knowledge/search", json=body
        )

    assert semantic.status_code == 200
    assert SHARED_KB in semantic.text, (
        "control: the vector search returned none of the shared chunks even "
        "with a constant embedding, so the personal one's absence proves nothing"
    )
    assert_no_private_content(semantic, "POST /api/v1/knowledge/search (teammate)")


@phase3_pending
async def test_the_team_listing_is_enterprise_keyed_and_names_no_organization(
    shared_world,
):
    """``GET /teams`` — the contract change Phase 7 ships (ADR-017 D1/D4).

    Both principals list the shared team and their own, because a team is
    parented by the ENTERPRISE and may span organizations. And the response
    carries ``enterprise_id`` where it carries ``organization_id`` today: the old
    key must be *gone*, not merely accompanied, because "no backward
    compatibility" is the owner's rule for this campaign and a tolerated old
    field is what keeps a frontend reading it.
    """
    world = shared_world
    mine = await as_owner(world, "GET", "/api/v1/teams")
    theirs = await as_teammate(world, "GET", "/api/v1/teams")

    assert mine.status_code == 200 and theirs.status_code == 200
    assert _ids(mine.json(), "team_id") == {world.team_shared, world.team_a_own}
    assert _ids(theirs.json(), "team_id") == {world.team_shared, world.team_b_own}
    for response in (mine, theirs):
        assert _ids(response.json(), "enterprise_id") == {world.enterprise_id}
        assert "organization_id" not in _keys(response.json()), (
            "GET /teams still carries organization_id; a team belongs to an "
            f"enterprise and may span organizations: {response.text[:300]}"
        )


@phase3_pending
async def test_a_created_case_is_stamped_with_the_enterprise_and_the_billing_org(
    shared_world,
):
    """Creation: isolation from the enterprise claim, billing from the actor's org.

    The two columns come from two different places and mean two different things,
    which is the whole of ADR-017 D1/D2. A single stamp that filled both from one
    claim would pass a weaker assertion and be the exact conflation this campaign
    exists to undo.
    """
    world = shared_world
    created = await as_owner(
        world,
        "POST",
        "/api/v1/cases",
        json={
            "title": f"{SHARED}-created-through-the-api",
            "description": "probe",
            "severity": "medium",
        },
    )
    assert (
        created.status_code == 201
    ), f"control: A cannot create a case: {created.text[:300]}"
    case_id = created.json()["case_id"]

    try:
        async with world.superuser_engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT enterprise_id, organization_id FROM cases "
                        "WHERE case_id = :c"
                    ),
                    {"c": case_id},
                )
            ).first()
        assert row[0] == world.enterprise_id, (
            f"the case was stamped enterprise {row[0]!r}, not the enterprise "
            "the request is bound to"
        )
        assert row[1] == world.org_x, (
            f"the case was stamped organization {row[1]!r}; billing attribution "
            "comes from the actor's own organization"
        )
    finally:
        async with world.superuser_engine.begin() as conn:
            await _delete_case_rows(conn, [case_id])


@phase3_pending
async def test_only_the_owner_can_unshare_or_reshare(shared_world):
    """A share is read visibility, not ownership.

    B can read the case through T. That must not let B withdraw A's consent, nor
    extend it to a team A never chose — including B's own, which would move A's
    case into an audience A cannot see. Checked against ``resource_shares``
    afterwards, because a route that answers 404 and writes anyway is the bug
    worth catching.
    """
    world = shared_world
    unshare = await as_teammate(
        world,
        "DELETE",
        f"/api/v1/cases/{world.shared_case.case_id}/team-shares/{world.team_shared}",
    )
    reshare = await as_teammate(
        world,
        "POST",
        f"/api/v1/cases/{world.shared_case.case_id}/team-shares",
        json={"team_id": world.team_b_own},
    )

    assert unshare.status_code in REFUSED, (
        f"a teammate withdrew the owner's share ({unshare.status_code}): "
        f"{unshare.text[:300]}"
    )
    assert reshare.status_code in REFUSED, (
        f"a teammate re-shared the owner's case to a team the owner never chose "
        f"({reshare.status_code}): {reshare.text[:300]}"
    )

    async with world.superuser_engine.begin() as conn:
        scopes = set(
            (
                await conn.execute(
                    text(
                        "SELECT scope_id FROM resource_shares "
                        "WHERE resource_type = 'case' AND resource_id = :c"
                    ),
                    {"c": world.shared_case.case_id},
                )
            ).scalars()
        )
    assert scopes == {world.team_shared}, (
        "the case's share set changed under a refused call — the owner's "
        f"consent is not what decides it: {sorted(scopes)}"
    )

    # The control: the owner CAN withdraw it, so the refusals above are about
    # who asked rather than about a route that refuses everyone.
    owner_unshare = await as_owner(
        world,
        "DELETE",
        f"/api/v1/cases/{world.shared_case.case_id}/team-shares/{world.team_shared}",
    )
    assert owner_unshare.status_code in (200, 204), (
        "control: the owner cannot withdraw their own share either, so a "
        f"teammate's refusal is not attributable to ownership: "
        f"{owner_unshare.text[:300]}"
    )


@phase3_pending
async def test_a_share_grants_read_not_write(shared_world):
    """The mutation battery against a case B can legitimately READ.

    This is the arm-2 analogue of the cross-wall mutation battery, and the
    sharper test of the two: B is not a stranger here — every read above
    succeeds — so nothing about the enterprise or the team keeps B out. Only the
    owner check does. Asserted against the row, not the response.
    """
    world = shared_world
    case_id = world.shared_case.case_id

    update = await as_teammate(
        world, "PUT", f"/api/v1/cases/{case_id}", json={"title": "PWNED"}
    )
    close = await as_teammate(
        world,
        "POST",
        f"/api/v1/cases/{case_id}/close",
        json={"closure_reason": "other"},
    )
    delete = await as_teammate(world, "DELETE", f"/api/v1/cases/{case_id}")
    edit_report = await as_teammate(
        world,
        "PUT",
        f"/api/v1/reports/{world.shared_case.report_id}",
        json={"content": "PWNED"},
    )

    for label, response in (
        ("PUT /cases/{id}", update),
        ("POST /cases/{id}/close", close),
        ("PUT /reports/{id}", edit_report),
    ):
        assert response.status_code in REFUSED, (
            f"{label}: a teammate with READ access mutated the owner's case "
            f"({response.status_code}): {response.text[:300]}"
        )

    async with world.superuser_engine.begin() as conn:
        case_row = (
            await conn.execute(
                text("SELECT title, state FROM cases WHERE case_id = :c"),
                {"c": case_id},
            )
        ).first()
        report_title = (
            await conn.execute(
                text("SELECT title FROM reports WHERE report_id = :r"),
                {"r": world.shared_case.report_id},
            )
        ).scalar()
    assert case_row is not None, (
        f"a teammate's DELETE (answered {delete.status_code}) removed the "
        "owner's case from the database"
    )
    assert case_row[0] == SHARED_TITLE, "a refused PUT renamed the owner's case"
    assert case_row[1] == "inquiry", "a refused close moved the owner's case"
    assert SHARED in report_title, "a refused PUT rewrote the owner's report"


# =============================================================================
# The inventory — derived from the live app, not from memory
# =============================================================================
#
# CARRIED OVER FROM ``test_two_tenant_surface_probe`` ENTRY FOR ENTRY. Every line
# below is a coverage decision someone made about a live route, and Phase 1
# deletes the module those decisions were written in; moving them here is what
# stops the deletion from silently dropping them. The two tests at the end are
# the only assertions in this file that pass on main today, and they are
# deliberately UNMARKED: they read the OpenAPI document and this dictionary, and
# nothing about the enterprise key.
#
# A hand-written list of "the surfaces that carry tenant data" is wrong the day
# after it is written. So the set is COMPUTED from the running application's
# OpenAPI document, and this file only has to say, for each computed operation,
# whether it is probed or deliberately not. A new route carrying a ``{case_id}``
# fails the suite until someone decides which.
#
# The classifier is deliberately crude and over-inclusive: any operation whose
# path, request body or query string names an identifier that can address
# another tenant's row. Over-inclusion costs an inventory line; under-inclusion
# costs a surface nobody probed.

#: Path parameters that name a tenant-owned row. ``component_name`` and ``role``
#: are the two path parameters that do not (a health component, an RBAC role
#: name), and their absence here is the whole of the exclusion.
TENANT_SCOPED_PATH_PARAMS = frozenset(
    {
        "case_id",
        "conversion_id",
        "data_id",
        "document_id",
        "draft_id",
        "evidence_id",
        "file_id",
        "grant_id",
        "report_id",
        "session_id",
        "suggestion_id",
        "team_id",
        "user_id",
        "username",
    }
)

#: Request-body and query fields that can name someone else's row. ``client_id``
#: is excluded: it identifies an OAuth client, not a tenant resource.
#:
#: ``organization_id`` stays in the set even though ADR-017 demotes it to billing
#: attribution: it is the field name the live app publishes today, and dropping
#: it would silently narrow the derived surface set. ``enterprise_id`` is listed
#: beside it so the derivation keeps working the moment Phase 7 swaps the
#: published field — the classifier is meant to be over-inclusive, and an extra
#: name costs an inventory line where a missing one costs an unprobed surface.
TENANT_SCOPED_FIELDS = frozenset(
    {
        "case_id",
        "enterprise_id",
        "organization_id",
        "session_id",
        "team_id",
        "user_id",
        "draft_ids",
        "document_ids",
        "item_ids",
    }
)

_PROBED = "probed"
_FINDING = "finding"
_EXEMPT = "exempt"

#: Every tenant-scoped operation, and what this module does about it.
#:
#: ``_PROBED`` — an attack and a positive control exist above.
#: ``_FINDING`` — the boundary does NOT hold; asserted as it behaves, with an
#:   issue reference, so a fix turns the case red.
#: ``_EXEMPT`` — deliberately not probed, with the reason. Every reason is a
#:   claim about the route, so it can be checked; "no time" is not one of them.
SURFACE_INVENTORY: dict[tuple[str, str], tuple[str, str]] = {
    # --- cases: read ------------------------------------------------------
    ("GET", "/api/v1/cases"): (_PROBED, "case list + the team_id filter"),
    ("POST", "/api/v1/cases"): (
        _PROBED,
        "creation stamps the caller's enterprise and billing organization",
    ),
    ("POST", "/api/v1/cases/search"): (
        _PROBED,
        "search, plus injected foreign identifiers",
    ),
    ("GET", "/api/v1/cases/{case_id}"): (_PROBED, "case detail"),
    ("GET", "/api/v1/cases/{case_id}/ui"): (_PROBED, "case UI projection"),
    ("GET", "/api/v1/cases/{case_id}/messages"): (_PROBED, "transcript"),
    ("GET", "/api/v1/cases/{case_id}/analytics"): (_PROBED, "counts are inference"),
    ("GET", "/api/v1/cases/{case_id}/report-recommendations"): (
        _PROBED,
        "refusal shape (404 vs the owner's 400)",
    ),
    # --- cases: write -----------------------------------------------------
    ("PUT", "/api/v1/cases/{case_id}"): (_PROBED, "mutation battery, row-checked"),
    ("DELETE", "/api/v1/cases/{case_id}"): (_PROBED, "mutation battery, row-checked"),
    ("POST", "/api/v1/cases/{case_id}/close"): (
        _PROBED,
        "mutation battery, row-checked",
    ),
    ("POST", "/api/v1/cases/{case_id}/title"): (_PROBED, "case-addressed battery"),
    ("POST", "/api/v1/cases/{case_id}/turns"): (_PROBED, "case-addressed battery"),
    ("POST", "/api/v1/cases/{case_id}/extract-knowledge"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/{case_id}/reports"): (_PROBED, "case-addressed battery"),
    ("POST", "/api/v1/cases/{case_id}/team-shares"): (
        _PROBED,
        "both directions, resource_shares checked",
    ),
    ("DELETE", "/api/v1/cases/{case_id}/team-shares/{team_id}"): (
        _PROBED,
        "unshare, resource_shares checked",
    ),
    # --- evidence, files, case data ---------------------------------------
    ("GET", "/api/v1/cases/{case_id}/evidence"): (_PROBED, "evidence listing"),
    ("GET", "/api/v1/cases/{case_id}/evidence/{evidence_id}"): (
        _PROBED,
        "evidence detail",
    ),
    ("GET", "/api/v1/cases/{case_id}/uploaded-files"): (_PROBED, "file listing"),
    ("GET", "/api/v1/cases/{case_id}/uploaded-files/{file_id}"): (
        _PROBED,
        "file detail",
    ),
    ("GET", "/api/v1/cases/{case_id}/data"): (_PROBED, "case data listing"),
    ("GET", "/api/v1/cases/{case_id}/data/{data_id}"): (_PROBED, "case data read"),
    ("DELETE", "/api/v1/cases/{case_id}/data/{data_id}"): (
        _PROBED,
        "case data delete",
    ),
    ("PATCH", "/api/v1/cases/{case_id}/evidence/{evidence_id}/classification"): (
        _EXEMPT,
        "the route is feature-disabled — it answers 404 "
        "'Reclassification endpoint is not enabled' to its OWNER, so an "
        "attacker-side 404 would be indistinguishable from the tenant check. "
        "Re-probe when the flag is turned on.",
    ),
    ("POST", "/api/v1/cases/{case_id}/queries"): (
        _EXEMPT,
        "410 Gone — removed in favour of POST /cases/{case_id}/turns, which is "
        "probed. The handler reads no case at all.",
    ),
    ("POST", "/api/v1/cases/{case_id}/data"): (
        _EXEMPT,
        "410 Gone — removed in favour of turn attachments; the handler reads "
        "no case at all.",
    ),
    # --- investigation sessions (case-nested) ------------------------------
    ("GET", "/api/v1/cases/{case_id}/sessions"): (_PROBED, "case-addressed battery"),
    ("POST", "/api/v1/cases/{case_id}/sessions"): (_PROBED, "case-addressed battery"),
    ("GET", "/api/v1/cases/{case_id}/sessions/active"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("GET", "/api/v1/cases/{case_id}/sessions/{session_id}"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("PATCH", "/api/v1/cases/{case_id}/sessions/{session_id}"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/{case_id}/sessions/{session_id}/pause"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/{case_id}/sessions/{session_id}/resume"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/{case_id}/sessions/{session_id}/complete"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/sessions/{session_id}/resume/{case_id}"): (
        _PROBED,
        "case-addressed battery",
    ),
    ("POST", "/api/v1/cases/sessions/{session_id}/case"): (
        _EXEMPT,
        "the handler resolves the session from the Redis-backed auth session "
        "store before touching a case; with no store it 500s for the OWNER "
        "too, so no positive control exists in-process. Its case-side effect "
        "is the same allowlist the probed /cases/{case_id}/sessions battery "
        "exercises.",
    ),
    # --- reports -----------------------------------------------------------
    ("GET", "/api/v1/cases/{case_id}/reports"): (_PROBED, "case-nested listing"),
    ("GET", "/api/v1/cases/{case_id}/reports/{report_id}/download"): (
        _PROBED,
        "report download",
    ),
    ("GET", "/api/v1/reports/case/{case_id}"): (_PROBED, "reports by case"),
    ("GET", "/api/v1/reports/{report_id}"): (_PROBED, "report by id"),
    ("PUT", "/api/v1/reports/{report_id}"): (_PROBED, "report edit, row-checked"),
    ("DELETE", "/api/v1/reports/{report_id}"): (
        _PROBED,
        "report delete, row-checked",
    ),
    ("GET", "/api/v1/reports/{report_id}/versions"): (_PROBED, "version history"),
    ("POST", "/api/v1/reports/generate"): (_PROBED, "case_id rides in the query"),
    ("POST", "/api/v1/reports/{report_id}/link-case"): (
        _PROBED,
        "closure link, row-checked (linked_to_closure)",
    ),
    # --- knowledge ---------------------------------------------------------
    ("GET", "/api/v1/knowledge/documents/{document_id}"): (_PROBED, "item read"),
    ("GET", "/api/v1/knowledge/documents/{document_id}/snippet"): (
        _PROBED,
        "item snippet",
    ),
    ("PUT", "/api/v1/knowledge/documents/{document_id}"): (
        _PROBED,
        "item edit, row-checked",
    ),
    ("DELETE", "/api/v1/knowledge/documents/{document_id}"): (
        _PROBED,
        "item delete, row-checked",
    ),
    ("POST", "/api/v1/knowledge/runbooks/create"): (
        _PROBED,
        "publish into another tenant's team, rows checked",
    ),
    ("POST", "/api/v1/knowledge/convert"): (
        _EXEMPT,
        "multipart upload whose conversion pass needs a live LLM provider. Its "
        "team_id travels the identical share-resolution path as "
        "POST /knowledge/runbooks/create, which IS probed for a planted share.",
    ),
    ("GET", "/api/v1/knowledge/conversions/{conversion_id}"): (
        _PROBED,
        "conversion detail",
    ),
    ("GET", "/api/v1/knowledge/conversions/by-case/{case_id}"): (
        _PROBED,
        "conversion by case",
    ),
    ("PUT", "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}"): (
        _PROBED,
        "draft edit, row-checked",
    ),
    ("DELETE", "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}"): (
        _PROBED,
        "draft discard, row-checked",
    ),
    (
        "POST",
        "/api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}/verify",
    ): (
        _PROBED,
        "draft verify, row-checked",
    ),
    ("POST", "/api/v1/knowledge/drafts/verify-batch"): (
        _EXEMPT,
        "no positive control in-process: the batch path answers 'Conversion "
        "job not found' to the draft's OWNER, so an attacker-side failure "
        "would be indistinguishable from the endpoint being broken. The same "
        "rows are probed one at a time through the per-draft verify above.",
    ),
    ("GET", "/api/v1/knowledge/suggestions/{suggestion_id}"): (
        _PROBED,
        "operator bound to A vs B's suggestion",
    ),
    ("PUT", "/api/v1/knowledge/suggestions/{suggestion_id}"): (
        _PROBED,
        "operator bound to A vs B's suggestion",
    ),
    ("POST", "/api/v1/knowledge/suggestions/{suggestion_id}/approve"): (
        _PROBED,
        "operator bound to A vs B's suggestion, row-checked",
    ),
    ("POST", "/api/v1/knowledge/suggestions/{suggestion_id}/reject"): (
        _PROBED,
        "operator bound to A vs B's suggestion, row-checked",
    ),
    ("POST", "/api/v1/knowledge/suggestions/{suggestion_id}/remediate-pii"): (
        _PROBED,
        "operator bound to A vs B's suggestion",
    ),
    # --- admin and break-glass --------------------------------------------
    ("GET", "/api/v1/admin/cases/{case_id}"): (
        _PROBED,
        "organization admin refused; operator needs a grant naming the case",
    ),
    ("GET", "/api/v1/admin/cases/{case_id}/messages"): (
        _PROBED,
        "organization admin refused; operator needs a grant naming the case",
    ),
    ("GET", "/api/v1/admin/grants"): (
        _PROBED,
        "the caller-supplied tenant list filter",
    ),
    ("POST", "/api/v1/admin/grants"): (
        _PROBED,
        "organization admin refused; operator mints",
    ),
    ("POST", "/api/v1/admin/grants/{grant_id}/revoke"): (
        _EXEMPT,
        "a grant row is operator-scoped, not tenant-scoped, and revoking one "
        "only REMOVES access — there is no cross-enterprise read to attack. The "
        "organization-admin arm is covered by the all-admin-routes battery.",
    ),
    ("GET", "/api/v1/admin/users/{user_id}"): (
        _PROBED,
        "organization admin refused; operator confined to their own "
        "enterprise (#1318), with the absent-id answer as the comparison",
    ),
    ("POST", "/api/v1/admin/users/{user_id}/deactivate"): (
        _PROBED,
        "operator mutation battery, row-checked (#1318)",
    ),
    ("POST", "/api/v1/admin/users/{user_id}/activate"): (
        _PROBED,
        "operator mutation battery, row-checked (#1318)",
    ),
    ("POST", "/api/v1/admin/users/{user_id}/roles"): (
        _PROBED,
        "operator mutation battery, row-checked (#1318)",
    ),
    ("DELETE", "/api/v1/admin/users/{user_id}/roles/{role}"): (
        _PROBED,
        "operator mutation battery, row-checked (#1318)",
    ),
    ("POST", "/api/v1/auth/users/{user_id}/revoke-tokens"): (
        _PROBED,
        "operator mutation battery, row-checked (#1318)",
    ),
    ("DELETE", "/api/v1/auth/users/{username}"): (
        _PROBED,
        "operator mutation battery — exercisable now that it refuses; the row "
        "check afterwards is what says the account survived (#1318)",
    ),
    # --- auth sessions (Redis-backed, not a SQL tenant surface) ------------
    ("GET", "/api/v1/sessions"): (
        _EXEMPT,
        "auth sessions live in the Redis session store, keyed per user and "
        "carrying no organization column; with no store the listing answers "
        "the same empty page to every caller, so no control exists.",
    ),
    ("POST", "/api/v1/sessions"): (_EXEMPT, "see GET /api/v1/sessions"),
    ("GET", "/api/v1/sessions/{session_id}"): (_EXEMPT, "see GET /api/v1/sessions"),
    ("PUT", "/api/v1/sessions/{session_id}"): (_EXEMPT, "see GET /api/v1/sessions"),
    ("DELETE", "/api/v1/sessions/{session_id}"): (_EXEMPT, "see GET /api/v1/sessions"),
    ("POST", "/api/v1/sessions/{session_id}/archive"): (
        _EXEMPT,
        "see GET /api/v1/sessions",
    ),
    ("POST", "/api/v1/sessions/{session_id}/heartbeat"): (
        _EXEMPT,
        "see GET /api/v1/sessions",
    ),
    ("POST", "/api/v1/sessions/{session_id}/restore"): (
        _EXEMPT,
        "see GET /api/v1/sessions",
    ),
    ("GET", "/api/v1/sessions/{session_id}/recovery-info"): (
        _EXEMPT,
        "see GET /api/v1/sessions",
    ),
    ("GET", "/api/v1/sessions/{session_id}/stats"): (
        _EXEMPT,
        "see GET /api/v1/sessions",
    ),
    ("GET", "/api/v1/sessions/{session_id}/cases"): (
        _EXEMPT,
        "the case half resolves through the same allowlist the probed case "
        "list uses; the session half is Redis (see GET /api/v1/sessions).",
    ),
    # --- development-only ---------------------------------------------------
    ("GET", "/debug/cases/{case_id}/causal-graph"): (
        _PROBED,
        "200 to everyone; the BODY is the boundary",
    ),
}


def tenant_scoped_operations(app) -> dict[tuple[str, str], str]:
    """Every operation in the LIVE app that can address a tenant-owned row."""
    spec = app.openapi()
    schemas = spec.get("components", {}).get("schemas", {})

    def properties(schema: dict) -> set:
        ref = schema.get("$ref")
        if ref:
            return set(schemas.get(ref.split("/")[-1], {}).get("properties", {}) or {})
        return set(schema.get("properties", {}) or {})

    found: dict[tuple[str, str], str] = {}
    for path, operations in spec["paths"].items():
        path_params = set(re.findall(r"\{([^}]+)\}", path))
        for method, operation in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            reasons = []
            if path_params & TENANT_SCOPED_PATH_PARAMS:
                reasons.append(
                    "path:" + ",".join(sorted(path_params & TENANT_SCOPED_PATH_PARAMS))
                )
            for content in (
                (operation.get("requestBody") or {}).get("content", {}).values()
            ):
                hit = properties(content.get("schema", {})) & TENANT_SCOPED_FIELDS
                if hit:
                    reasons.append("body:" + ",".join(sorted(hit)))
            query = sorted(
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "query"
                and parameter["name"] in TENANT_SCOPED_FIELDS
            )
            if query:
                reasons.append("query:" + ",".join(query))
            if reasons:
                found[(method.upper(), path)] = ";".join(sorted(set(reasons)))
    return found


def test_every_tenant_scoped_route_is_in_the_inventory(probe_app):
    """A new tenant-scoped route fails this module until someone classifies it.

    This is the only assertion here that cannot rot silently. Everything else
    describes surfaces that existed when it was written; this one asks the
    running application what surfaces exist now.

    It fails in both directions on purpose. An operation missing from the
    inventory is an unprobed surface. An inventory entry with no matching
    operation is a probe aimed at a route that no longer exists — which, left
    alone, is a green test asserting nothing about anything.
    """
    live = tenant_scoped_operations(probe_app.app)

    unclassified = {
        key: why for key, why in live.items() if key not in SURFACE_INVENTORY
    }
    assert not unclassified, (
        "these tenant-scoped operations are not in SURFACE_INVENTORY — probe "
        "them, or add an entry saying why not:\n"
        + "\n".join(
            f"  {m} {p}  ({why})" for (m, p), why in sorted(unclassified.items())
        )
    )

    stale = sorted(set(SURFACE_INVENTORY) - set(live))
    assert not stale, (
        "these SURFACE_INVENTORY entries name operations the app no longer "
        "exposes (or no longer classifies as tenant-scoped):\n"
        + "\n".join(f"  {m} {p}" for m, p in stale)
    )


def _resolve_reason(reason: str) -> str:
    """Follow a ``see <METHOD> <path>`` cross-reference to the stated reason.

    The Redis session routes share one reason between eleven entries. Repeating
    it eleven times invites the copies to drift; pointing at it keeps one text
    and still forces that text to exist — a dangling pointer resolves to itself
    and fails the length rule below.
    """
    if not reason.startswith("see "):
        return reason
    target = reason[len("see ") :].strip()
    method, _, path = target.partition(" ")
    referenced = SURFACE_INVENTORY.get((method, path))
    if referenced is None or referenced[0] != _EXEMPT:
        return reason
    return referenced[1]


def test_the_inventory_states_a_reason_for_every_unprobed_surface():
    """An exemption without a reason is an unprobed surface with a nice name."""
    for (method, path), (disposition, reason) in SURFACE_INVENTORY.items():
        assert disposition in (
            _PROBED,
            _FINDING,
            _EXEMPT,
        ), f"{method} {path}: unknown disposition {disposition!r}"
        assert reason.strip(), f"{method} {path}: no reason given"
        if disposition == _EXEMPT:
            resolved = _resolve_reason(reason)
            assert len(resolved) > 40, (
                f"{method} {path}: an exemption reason has to say what about "
                f"the route makes it unprobeable, not {reason!r}"
            )
        if disposition == _FINDING:
            assert (
                "#" in reason
            ), f"{method} {path}: a finding must name the issue that tracks it"


# =============================================================================
# The guards that run today — and the last word on residue
# =============================================================================


def test_the_search_injection_names_only_real_request_fields():
    """A body field the request model does not declare is an injection of nothing.

    ``CaseSearchRequest`` takes pydantic's default ``extra="ignore"``, so a key
    it has never heard of is dropped before any handler sees it: the request
    succeeds, the assertion that follows passes, and the probe has measured the
    parser. This runs today, unmarked, because it needs neither the enterprise
    schema nor the binder — only the live model — and it is the thing that would
    have caught an ``enterprise_id`` in that body.

    Bidirectional, for the same reason the route inventory is. Forward: every key
    the injection sends must be a declared field. Backward: every declared field
    that can name another principal's row must be one the injection sends, so a
    new selector cannot appear on the model without someone deciding whether it
    is attackable.
    """
    from faultmaven.models.api_models import CaseSearchRequest

    declared = set(CaseSearchRequest.model_fields)

    undeclared = sorted(set(INJECTED_SEARCH_KEYS) - declared)
    assert not undeclared, (
        "the search-injection body names fields CaseSearchRequest does not "
        f"declare, so pydantic drops them and the injection asserts nothing: "
        f"{undeclared}"
    )

    uninjected = sorted((declared & TENANT_SCOPED_FIELDS) - set(INJECTED_SEARCH_KEYS))
    assert not uninjected, (
        "CaseSearchRequest declares tenant-shaped fields the injection cases do "
        f"not send, so nothing shows whether they select: {uninjected}"
    )


#: The id prefixes every world builds its rows under. Kept beside the residue
#: check rather than inside it: the builders derive their ids from these, so a
#: prefix that changes in one place and not the other is a check aimed at
#: nothing.
PROBE_ENTERPRISE_PREFIX = "ent_"
PROBE_USER_PREFIXES = ("user_a_", "user_b_", "user_op_", "user_sa_", "user_sb_")
PROBE_ORG_PREFIXES = ("org_x_", "org_y_")
PROBE_TEAM_PREFIXES = ("team_a_", "team_b_", "team_t_", "team_ao_", "team_bo_")


def test_the_probe_left_no_rows_behind(probe_app):
    """Nothing this module seeded may outlive it. UNMARKED, and LAST.

    Unmarked because it is the one assertion here that must never be xfailed.
    Every world-backed test carries a strict xfail, and an xfail swallows a
    teardown failure exactly as it swallows the assertion failure it was written
    for — so once the worlds do build, a teardown that raised half way through
    would leave enterprises, users and teams in the database and every test in
    this file would still report ``xfailed``. The `-m postgres` lane runs
    siblings that count rows against this same database; they would go red, and
    the module that caused it would look green.

    Last in the file because pytest runs tests in definition order, so by the
    time this executes every world in the module has been built and torn down.
    It passes today for the honest reason that no world is built at all, and it
    starts doing real work on the day the schema gate goes quiet — which is the
    day the risk it guards against begins.

    Read as the SUPERUSER: a residue check under RLS would report "clean" for
    rows it simply could not see.

    One row is deliberately NOT counted: the default test enterprise
    ``seed_users`` creates as its parent (``tests.utils.DEFAULT_TEST_ENTERPRISE_ID``).
    It is idempotent scaffolding shared with every other module in the suite, and
    deleting it is how you break them. The probe's own enterprises all carry the
    ``ent_`` prefix, which is what this looks for.
    """

    async def leftovers() -> dict[str, list[str]]:
        engine = create_async_engine(probe_app.superuser_url, future=True)
        try:
            async with engine.connect() as conn:
                found: dict[str, list[str]] = {}
                for label, table, column, prefixes in (
                    (
                        "enterprises",
                        "enterprises",
                        "enterprise_id",
                        (PROBE_ENTERPRISE_PREFIX,),
                    ),
                    ("users", "users", "user_id", PROBE_USER_PREFIXES),
                    (
                        "organizations",
                        "organizations",
                        "organization_id",
                        PROBE_ORG_PREFIXES,
                    ),
                    ("teams", "teams", "team_id", PROBE_TEAM_PREFIXES),
                ):
                    rows: list[str] = []
                    for prefix in prefixes:
                        # ``LIKE :p`` with the wildcard bound into the VALUE, so
                        # the underscores in these prefixes stay literal without
                        # an ESCAPE clause to get wrong.
                        rows.extend(
                            (
                                await conn.execute(
                                    text(
                                        f"SELECT {column} FROM {table} "
                                        f"WHERE {column} LIKE :p"
                                    ),
                                    {"p": prefix.replace("_", r"\_") + "%"},
                                )
                            ).scalars()
                        )
                    if rows:
                        found[label] = sorted(rows)
                return found
        finally:
            await engine.dispose()

    residue = asyncio.run(leftovers())
    assert not residue, (
        "this module left rows behind; a world teardown did not complete, and "
        "the strict xfail on every world-backed test hid it: "
        + "; ".join(f"{table}: {ids}" for table, ids in sorted(residue.items()))
    )
