"""The cross-enterprise operator case list on a real PostgreSQL (ADR-012 D9).

Under ``TENANT_PROVIDER=multi`` the list reads every enterprise through the
``SECURITY DEFINER`` functions revision 003 creates. What only a real database
can answer:

* **The bound is structural.** The page function's declared result columns are
  an exact, asserted set of ids, closed-vocabulary strings, timestamps,
  integers, booleans and arrays of those — adding a column fails here, and a
  case whose title and description are sentinels never surfaces them. The
  ``search_path`` lists ``pg_temp`` last, so a caller's temporary table cannot
  shadow ``cases``.
* **The grant is the other half.** No role may execute either function without
  an explicit grant — PUBLIC holds none — and a role without it gets a 503 that
  says so, never a partial list.
* **Parity.** One fixture set — every investigation stage, asides including a
  duplicate turn number, a terminal case, team shares, an empty case, a null
  and a missing mitigation, a clock behind its history, a deleted owner — read
  through the multi path equals the single-tenant path's
  ``AdminCaseMetadata.from_summary`` rows, team enrichment included, field for
  field.
* **Isolation is unchanged.** As a limited, RLS-subject role, the read returns
  rows from two enterprises while a plain ``SELECT`` on ``cases`` in the same
  session still sees only the bound one.
* **Same semantics as the single-tenant list.** ``state``/``source`` filters,
  ``limit``/``offset``, newest update first, a total that counts every match in
  every enterprise, empty cases included.
* **Failure direction.** Without the functions the read raises
  ``CaseMetadataUnavailableError`` — which the route answers with a 503 — and
  revision 003 steps down and back up.

Everything runs as a role with the deployed ``faultmaven_app`` grants —
including ``EXECUTE`` on both functions, which revision 003 grants that role by
name — and no ownership, because PostgreSQL exempts superusers and table owners
from RLS; the
fixtures are written through the production case writer under the enterprise's
binding, and only the shapes that writer cannot produce (a stored duplicate, a
lagging clock, a missing key) are then set as the owner.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from tests.integration.security.conftest import (
    CASE_METADATA_FUNCTIONS,
    RUNTIME_ROLE,
    alembic_on,
    create_limited_role,
    database_of_its_own,
    drop_limited_role,
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

_ROLE = f"fm_casemeta_probe_{uuid.uuid4().hex[:8]}"
_PW = "fm_casemeta_probe_pw"
_BASELINE_REVISION = "a1e0c17bd001"
_LEDGER_REVISION = "65913afe773c"  # 002_llm_usage_ledger, 003's parent

#: Every fixture case is updated "in 2099", so the fixtures lead the
#: newest-first order ahead of any row another module left in the shared
#: database, in an order this module decides.
_EPOCH = datetime(2099, 1, 1, tzinfo=timezone.utc)

#: The page function's declared result, in order. This list is the bound: a
#: column added to the function fails the structural test until it is added
#: here, by a person deciding it is metadata.
_PAGE_COLUMNS = [
    ("case_id", "text"),
    ("enterprise_id", "text"),
    ("organization_id", "text"),
    ("user_id", "text"),
    ("state", "text"),
    ("source", "text"),
    ("closure_reason", "text"),
    ("created_at", "timestamp with time zone"),
    ("updated_at", "timestamp with time zone"),
    ("last_activity_at", "timestamp with time zone"),
    ("resolved_at", "timestamp with time zone"),
    ("closed_at", "timestamp with time zone"),
    ("current_turn", "integer"),
    ("turns_without_progress", "integer"),
    ("mitigation_accepted", "boolean"),
    ("mitigation_verified", "boolean"),
    ("solution_accepted", "boolean"),
    ("solution_verified", "boolean"),
    ("turn_numbers", "integer[]"),
    ("turn_is_out_of_band", "boolean[]"),
    ("shared_team_ids", "text[]"),
]


# =============================================================================
# Environment
# =============================================================================


@pytest.fixture(scope="module")
def limited_role_env():
    """Point the persistence layer at a limited role for this module only —
    restored wholesale in teardown (the ``-m postgres`` lane runs sibling
    modules that expect the superuser url)."""
    superuser_url = os.environ["DATABASE_URL"]
    saved = {
        key: os.environ.get(key)
        for key in ("DATABASE_URL", "DEPLOYMENT_MODE", "TENANT_PROVIDER")
    }
    asyncio.run(create_limited_role(superuser_url, _ROLE, _PW))
    os.environ["DATABASE_URL"] = limited_url(superuser_url, _ROLE, _PW)
    os.environ["DEPLOYMENT_MODE"] = "cloud"
    os.environ["TENANT_PROVIDER"] = "multi"

    from faultmaven.infrastructure.persistence.database import reset_engine
    from tests.utils import reset_settings_singleton

    reset_settings_singleton()
    reset_engine()
    yield superuser_url
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings_singleton()
    reset_engine()
    asyncio.run(drop_limited_role(superuser_url, _ROLE))


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop(limited_role_env):
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    reset_engine()
    yield
    await close_database()
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


async def _as_owner(superuser_url: str, sql: str, **params):
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return result.fetchall() if result.returns_rows else []
    finally:
        await engine.dispose()


# =============================================================================
# Fixtures — written through the production writer, then shaped as the owner
# =============================================================================


def _turns(*entries):
    """``(turn_number, is_aside)`` pairs as stored ``turn_history`` JSON."""
    from faultmaven.modules.case.domain.models.turn import TurnOutcome, TurnProgress

    return [
        TurnProgress(
            turn_number=number,
            outcome=TurnOutcome.OUT_OF_BAND if aside else TurnOutcome.CONVERSATION,
            progress_made=not aside,
        ).model_dump(mode="json")
        for number, aside in entries
    ]


def _case(enterprise_id: str, user_id: str, label: str, **fields):
    from faultmaven.modules.case.domain.models.case import Case
    from faultmaven.modules.case.domain.models.problem import InquiryData

    state = fields.pop("state", CaseState.INQUIRY)
    # Before any terminal timestamp a fixture sets.
    fields.setdefault("created_at", datetime.now(timezone.utc) - timedelta(days=1))
    if state in (CaseState.INVESTIGATING, CaseState.RESOLVED):
        fields.setdefault(
            "inquiry",
            InquiryData(
                proposed_problem_statement=f"SENTINEL-STATEMENT-{label}",
                problem_statement_confirmed=True,
            ),
        )
    return Case(
        case_id=f"case_{uuid.uuid4().hex[:12]}",
        enterprise_id=enterprise_id,
        user_id=user_id,
        title=f"SENTINEL-TITLE-{label}",
        description=f"SENTINEL-DESCRIPTION-{label}",
        state=state,
        **fields,
    )


@pytest.fixture
async def world(limited_role_env):
    """Two enterprises. A holds one case of every shape the parity test needs;
    B holds two, so a read spanning enterprises has something to span."""
    from faultmaven.modules.case.domain.models.progress import (
        InvestigationProgress,
        MitigationRecord,
    )
    from faultmaven.modules.case.domain.models.turn import TurnOutcome, TurnProgress
    from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
        SessionlessCaseRepository,
    )

    owner = limited_role_env
    suffix = uuid.uuid4().hex[:8]
    ent_a, ent_b = f"ent_meta_a_{suffix}", f"ent_meta_b_{suffix}"
    user_a, user_b = f"user_meta_a_{suffix}", f"user_meta_b_{suffix}"
    org_a = f"org_meta_a_{suffix}"
    team_1, team_2 = f"team_meta_1_{suffix}", f"team_meta_2_{suffix}"
    # Codepoint order puts "B" before "a"; a linguistic collation (en_US, the
    # usual database default) puts "a" first. The two paths must agree.
    team_lower, team_upper = f"team_meta_a_{suffix}", f"team_meta_B_{suffix}"

    # Cleanup is armed before the first insert: a setup that fails half way
    # must not leave enterprises behind for the rest of the lane to count.
    try:
        for enterprise, user in ((ent_a, user_a), (ent_b, user_b)):
            await _as_owner(
                owner,
                "INSERT INTO enterprises (enterprise_id, name, slug) VALUES (:e, :e, :e)",
                e=enterprise,
            )
            await _as_owner(
                owner,
                "INSERT INTO users (user_id, enterprise_id, username, email, "
                "display_name) VALUES (:u, :e, :u, :m, :u)",
                u=user,
                e=enterprise,
                m=f"{user}@example.com",
            )
        await _as_owner(
            owner,
            "INSERT INTO organizations (organization_id, enterprise_id, name, slug, "
            "is_active) VALUES (:o, :e, :o, :o, true)",
            o=org_a,
            e=ent_a,
        )
        for team in (team_1, team_2, team_lower, team_upper):
            await _as_owner(
                owner,
                "INSERT INTO teams (team_id, enterprise_id, name) VALUES (:t, :e, :t)",
                t=team,
                e=ent_a,
            )

        def turns(*entries):
            return [
                TurnProgress(
                    turn_number=number,
                    outcome=(
                        TurnOutcome.OUT_OF_BAND if aside else TurnOutcome.CONVERSATION
                    ),
                    progress_made=not aside,
                )
                for number, aside in entries
            ]

        now = datetime.now(timezone.utc)
        investigating = CaseState.INVESTIGATING
        a = {
            "diagnosis": _case(
                ent_a,
                user_a,
                "diagnosis",
                state=investigating,
                source="slack",
                organization_id=org_a,
                turn_history=turns((1, False), (2, False)),
                current_turn=2,
                turns_without_progress=2,
            ),
            "mitigation": _case(
                ent_a,
                user_a,
                "mitigation",
                state=investigating,
                progress=InvestigationProgress(
                    mitigation=MitigationRecord(accepted=True, verified=False)
                ),
                turn_history=turns((1, False)),
                current_turn=1,
            ),
            "treatment": _case(
                ent_a,
                user_a,
                "treatment",
                state=investigating,
                progress=InvestigationProgress(
                    mitigation=MitigationRecord(accepted=True, verified=True),
                    solution_accepted=True,
                ),
                turn_history=turns((1, False), (2, False), (3, False)),
                current_turn=3,
            ),
            # Shaped below: a duplicate aside turn number, as stored.
            "duplicate_aside": _case(
                ent_a,
                user_a,
                "duplicate_aside",
                state=investigating,
                turn_history=turns((1, False), (2, True), (3, False)),
                current_turn=3,
            ),
            "closed": _case(
                ent_a,
                user_a,
                "closed",
                state=CaseState.CLOSED,
                closed_at=now,
                closure_reason="inquiry_only",
                turn_history=turns((1, False), (2, True)),
                current_turn=2,
            ),
            "resolved": _case(
                ent_a,
                user_a,
                "resolved",
                state=CaseState.RESOLVED,
                resolved_at=now,
                closed_at=now,
                progress=InvestigationProgress(
                    solution_accepted=True, solution_verified=True
                ),
                turn_history=turns((1, False), (2, False)),
                current_turn=2,
            ),
            "shared": _case(
                ent_a,
                user_a,
                "shared",
                state=investigating,
                source="api",
                turn_history=turns((1, False)),
                current_turn=1,
            ),
            # Shaped below: shared to two teams whose ids differ only in case.
            "shared_mixed_case": _case(
                ent_a,
                user_a,
                "shared_mixed_case",
                turn_history=turns((1, False)),
                current_turn=1,
            ),
            "empty": _case(ent_a, user_a, "empty"),
            # Shaped below: an explicit null mitigation beside an accepted solution.
            "null_mitigation": _case(
                ent_a,
                user_a,
                "null_mitigation",
                state=investigating,
                progress=InvestigationProgress(solution_accepted=True),
                turn_history=turns((1, False)),
                current_turn=1,
            ),
            # Shaped below: a progress blob with none of the gate keys at all.
            "missing_keys": _case(
                ent_a,
                user_a,
                "missing_keys",
                state=investigating,
                turn_history=turns((1, False)),
                current_turn=1,
            ),
            # Shaped below: the clock column behind the stored history.
            "lagging_clock": _case(
                ent_a,
                user_a,
                "lagging_clock",
                turn_history=turns((1, False), (2, True), (3, False)),
                current_turn=3,
            ),
            # Shaped below: the owning account was deleted (FK SET NULL).
            "orphan": _case(
                ent_a,
                user_a,
                "orphan",
                turn_history=turns((1, False)),
                current_turn=1,
            ),
        }
        b = {
            "b_investigating": _case(
                ent_b,
                user_b,
                "b_investigating",
                state=investigating,
                source="slack",
                turn_history=turns((1, False)),
                current_turn=1,
            ),
            "b_empty": _case(ent_b, user_b, "b_empty"),
        }
        # Six cases updated at ONE instant, as a bulk statement stamps them —
        # the newest rows, so they lead both paths' order and a page boundary
        # can fall among them. Saved in DESCENDING case_id order, so a read
        # that dropped the case_id tiebreaker would most likely return them in
        # an order other than the one both paths promise.
        tied = sorted(
            (_case(ent_a, user_a, f"tie_{n}") for n in range(6)),
            key=lambda case: case.case_id,
            reverse=True,
        )
        for n, case in enumerate(tied):
            a[f"tie_{n}"] = case

        repository = SessionlessCaseRepository()
        for enterprise, cases in ((ent_a, a), (ent_b, b)):
            set_current_enterprise_id(enterprise)
            for case in cases.values():
                await repository.save(case)
        set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)

        # The shapes the production writer never produces — it repairs a history
        # before saving it — and that a stored row can still hold.
        await _as_owner(
            owner,
            "UPDATE cases SET metadata = jsonb_set(metadata, '{turn_history}', "
            "CAST(:history AS jsonb)), current_turn = 3 WHERE case_id = :c",
            history=json.dumps(_turns((1, False), (2, True), (2, True), (3, False))),
            c=a["duplicate_aside"].case_id,
        )
        await _as_owner(
            owner,
            "UPDATE cases SET progress = jsonb_set(progress, '{mitigation}', "
            "'null'::jsonb) WHERE case_id = :c",
            c=a["null_mitigation"].case_id,
        )
        await _as_owner(
            owner,
            "UPDATE cases SET progress = '{}'::jsonb WHERE case_id = :c",
            c=a["missing_keys"].case_id,
        )
        await _as_owner(
            owner,
            "UPDATE cases SET current_turn = 1 WHERE case_id = :c",
            c=a["lagging_clock"].case_id,
        )
        await _as_owner(
            owner,
            "UPDATE cases SET user_id = NULL WHERE case_id = :c",
            c=a["orphan"].case_id,
        )
        # Two team shares and an organization-scope one, which is not a team;
        # and two teams whose ids differ only in case, on a second case.
        for case_key, scope_type, scope_id in (
            ("shared", "team", team_2),
            ("shared", "team", team_1),
            ("shared", "organization", org_a),
            ("shared_mixed_case", "team", team_lower),
            ("shared_mixed_case", "team", team_upper),
        ):
            await _as_owner(
                owner,
                "INSERT INTO resource_shares (share_id, resource_type, resource_id, "
                "scope_type, scope_id, enterprise_id) "
                "VALUES (:s, 'case', :c, :t, :i, :e)",
                s=str(uuid.uuid4()),
                c=a[case_key].case_id,
                t=scope_type,
                i=scope_id,
                e=ent_a,
            )
        # Distinct, far-future update times: a deterministic newest-first order
        # — and, newer still, the tied six at one instant, ordered by case_id.
        tied_ids = {case.case_id for case in tied}
        distinct = [
            case
            for case in list(a.values()) + list(b.values())
            if case.case_id not in tied_ids
        ]
        for minutes, case in enumerate(reversed(distinct)):
            await _as_owner(
                owner,
                "UPDATE cases SET updated_at = :t WHERE case_id = :c",
                t=_EPOCH + timedelta(minutes=minutes),
                c=case.case_id,
            )
        await _as_owner(
            owner,
            "UPDATE cases SET updated_at = :t WHERE case_id = ANY(:ids)",
            t=_EPOCH + timedelta(days=1),
            ids=sorted(tied_ids),
        )
        ordered = sorted(tied, key=lambda case: case.case_id) + distinct

        yield SimpleNamespace(
            owner=owner,
            ent_a=ent_a,
            ent_b=ent_b,
            a=a,
            b=b,
            teams=sorted([team_1, team_2]),
            mixed_case_teams=[team_upper, team_lower],
            # Newest first; the tied six lead, ascending by case_id.
            order=[case.case_id for case in ordered],
            tied=sorted(tied_ids),
        )
    finally:
        for enterprise in (ent_a, ent_b):
            await _as_owner(
                owner, "DELETE FROM enterprises WHERE enterprise_id = :e", e=enterprise
            )


async def _read_everything(**filters):
    """One page large enough to hold every row in the shared database."""
    from faultmaven.modules.case.infrastructure.case_metadata_reader import (
        SessionlessCaseMetadataReader,
    )

    return await SessionlessCaseMetadataReader().list_case_metadata(
        state=filters.get("state"),
        source=filters.get("source"),
        limit=filters.get("limit", 100_000),
        offset=filters.get("offset", 0),
    )


# =============================================================================
# The role and the function, as the catalog states them
# =============================================================================


async def test_the_role_under_test_is_subject_to_rls(limited_role_env):
    """If RLS were bypassed, the isolation assertions below would be vacuous."""
    engine = create_async_engine(limited_url(limited_role_env, _ROLE, _PW), future=True)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT r.rolsuper, r.rolbypassrls, "
                        "pg_has_role(current_user, t.tableowner, 'USAGE') "
                        "FROM pg_roles r, pg_tables t "
                        "WHERE r.rolname = current_user AND t.tablename = 'cases'"
                    )
                )
            ).one()
            assert tuple(row) == (False, False, False)
    finally:
        await engine.dispose()


async def test_the_page_function_returns_exactly_the_metadata_columns(
    limited_role_env,
):
    """I1: the bound, read off the catalog. Adding any column fails here."""
    rows = await _as_owner(
        limited_role_env,
        "SELECT u.name, format_type(u.type, NULL) "
        "FROM pg_proc p, unnest(p.proargnames, p.proallargtypes, "
        "p.proargmodes::text[]) WITH ORDINALITY AS u(name, type, mode, position) "
        "WHERE p.proname = 'admin_case_metadata_page' AND u.mode = 't' "
        "ORDER BY u.position",
    )
    assert [tuple(row) for row in rows] == _PAGE_COLUMNS

    (count_result,) = await _as_owner(
        limited_role_env,
        "SELECT pg_get_function_result(oid) FROM pg_proc "
        "WHERE proname = 'admin_case_metadata_count'",
    )
    assert tuple(count_result) == ("bigint",)


async def test_both_functions_run_as_the_table_owner_with_a_pinned_search_path(
    limited_role_env,
):
    """What makes them span every enterprise — and only because the policies
    are enabled, never forced — and what keeps a caller's schema out of them.

    ``pg_temp`` must be listed, and LAST: left out, PostgreSQL searches the
    caller's temporary schema FIRST for relations, so a caller's temporary
    ``cases`` would shadow the real table inside the owner's rights.
    ``row_security`` must be ``off``, so that the day the owner stops being
    exempt the read raises instead of returning one enterprise's cases.
    """
    rows = await _as_owner(
        limited_role_env,
        "SELECT p.proname, p.prosecdef, p.proconfig, "
        "pg_get_userbyid(p.proowner) = t.tableowner "
        "FROM pg_proc p, pg_tables t "
        "WHERE p.proname IN ('admin_case_metadata_page', 'admin_case_metadata_count') "
        "AND t.tablename = 'cases' ORDER BY p.proname",
    )
    assert [row.proname for row in rows] == [
        "admin_case_metadata_count",
        "admin_case_metadata_page",
    ]
    for name, definer, config, owned_by_table_owner in rows:
        assert (definer, owned_by_table_owner) == (True, True), name
        (setting,) = [entry for entry in config if entry.startswith("search_path=")]
        path = [part.strip() for part in setting.split("=", 1)[1].split(",")]
        assert path[0] == "pg_catalog", (name, path)
        assert "pg_temp" in path and path[-1] == "pg_temp", (
            f"{name}: pg_temp must be listed last, or a caller's temporary "
            f"table shadows the real one: {path}"
        )
        assert "row_security=off" in config, (
            f"{name}: without row_security=off a lost RLS exemption filters "
            f"silently instead of raising: {config}"
        )

    # Both tables the functions read: neither policy may be FORCEd, or the
    # owner is no longer exempt (and, with row_security off, the read raises).
    forced = await _as_owner(
        limited_role_env,
        "SELECT relname, relforcerowsecurity FROM pg_class "
        "WHERE relname IN ('cases', 'resource_shares') ORDER BY relname",
    )
    assert [tuple(row) for row in forced] == [
        ("cases", False),
        ("resource_shares", False),
    ]


async def test_an_owner_subject_to_rls_raises_rather_than_filters(world):
    """The RLS exemption, lost for real: in one transaction rolled back at the
    end, both tables and both functions are handed to a NON-superuser owner and
    ``cases`` is FORCEd, so the policies now apply to the functions' owner.

    With ``row_security = off`` the read raises — and since the caller still
    holds EXECUTE, it is classified as a refusal other than the grant, never
    served as the bound enterprise's cases alone. Without it the functions would
    quietly answer with enterprise A only.
    """
    from faultmaven.modules.case.domain.models.metadata import (
        CaseMetadataRefusedError,
    )
    from faultmaven.modules.case.infrastructure.case_metadata_reader import (
        PostgreSQLCaseMetadataReader,
    )

    owner = f"fm_casemeta_owner_{uuid.uuid4().hex[:8]}"
    await _as_owner(world.owner, drop_role_sql(owner))
    await _as_owner(world.owner, f"CREATE ROLE {owner} NOLOGIN NOSUPERUSER")
    engine = create_async_engine(world.owner, future=True)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            try:
                for table in ("cases", "resource_shares"):
                    await conn.execute(text(f"ALTER TABLE {table} OWNER TO {owner}"))
                for signature in CASE_METADATA_FUNCTIONS:
                    await conn.execute(
                        text(f"ALTER FUNCTION {signature} OWNER TO {owner}")
                    )
                await conn.execute(text("ALTER TABLE cases FORCE ROW LEVEL SECURITY"))
                # Call as the limited, granted role, bound to enterprise A.
                await conn.execute(text(f"SET LOCAL ROLE {_ROLE}"))
                await conn.execute(
                    text("SELECT set_config('app.current_enterprise_id', :e, true)"),
                    {"e": world.ent_a},
                )
                session = AsyncSession(bind=conn)
                with pytest.raises(CaseMetadataRefusedError):
                    await PostgreSQLCaseMetadataReader(session).list_case_metadata(
                        state=None, source=None, limit=100_000, offset=0
                    )
                await session.close()
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
        await _as_owner(world.owner, drop_role_sql(owner))

    # The rollback restored ownership and the non-forced policy.
    ((forced,),) = await _as_owner(
        world.owner,
        "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'cases'",
    )
    assert forced is False


async def test_a_callers_temporary_table_cannot_shadow_cases(world):
    """The pinned ``search_path``, exercised: a limited role creates a temporary
    ``cases`` with no rows, and the function still counts the real table."""
    engine = create_async_engine(limited_url(world.owner, _ROLE, _PW), future=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("CREATE TEMPORARY TABLE cases (state text, source text)")
            )
            shadowed = (
                await conn.execute(text("SELECT count(*) FROM pg_temp.cases"))
            ).scalar()
            counted = (
                await conn.execute(text("SELECT admin_case_metadata_count(NULL, NULL)"))
            ).scalar()
    finally:
        await engine.dispose()

    assert shadowed == 0
    assert counted >= len(
        world.order
    ), "the function read the caller's temporary table instead of cases"


async def test_no_returned_value_carries_case_content(world):
    """I1, behaviourally: every fixture's title, description and problem
    statement is a sentinel, and none of them reaches the read. The fixture
    ids are asserted present, so a read that returned nothing cannot pass."""
    set_current_enterprise_id(world.ent_a)
    engine = create_async_engine(limited_url(world.owner, _ROLE, _PW), future=True)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT * FROM admin_case_metadata_page(NULL, NULL, 100000, 0)"
                    )
                )
            ).fetchall()
    finally:
        await engine.dispose()

    ours = [row for row in rows if row.case_id in set(world.order)]
    assert {row.case_id for row in ours} == set(world.order)
    dumped = "\n".join(repr(tuple(row)) for row in ours)
    assert "SENTINEL" not in dumped


# =============================================================================
# I2 — parity with the single-tenant path
# =============================================================================


async def test_the_multi_path_serves_what_the_single_tenant_path_serves(world):
    """Field for field, on one fixture set, through both real read paths.

    The single-tenant path is ``CaseService.list_all_cases`` over the real
    PostgreSQL case repository — which loads each case and repairs its turn
    sequence — with team enrichment wired, projected by
    ``AdminCaseMetadata.from_summary``. It runs bound to enterprise A, as
    ``single`` runs bound to its one enterprise. The multi path is the
    metadata read, projected as the route projects it, narrowed to A's rows.
    """
    from faultmaven.api.routes.admin_cases import _project_case_metadata
    from faultmaven.infrastructure.persistence.sessionless_share_repository import (
        SessionlessShareRepository,
    )
    from faultmaven.models.api_models import AdminCaseMetadata, CaseListFilter
    from faultmaven.modules.case.domain.services.case_service import CaseService
    from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
        SessionlessCaseRepository,
    )

    service = CaseService(
        case_repository=SessionlessCaseRepository(),
        # Truthy is all the team gate reads; the share lookup is real.
        team_service=object(),
        share_repository=SessionlessShareRepository(),
    )
    set_current_enterprise_id(world.ent_a)
    summaries, single_total = await service.list_all_cases(CaseListFilter(limit=200))
    single = [AdminCaseMetadata.from_summary(summary) for summary in summaries]

    metadata, _ = await _read_everything()
    multi = _project_case_metadata(
        [m for m in metadata if m.enterprise_id == world.ent_a]
    )

    # The comparison is only as strong as its fixtures: every shape is here…
    assert [row.case_id for row in single] == [
        case_id
        for case_id in world.order
        if case_id in {c.case_id for c in world.a.values()}
        and case_id != world.a["orphan"].case_id
    ]
    assert single_total == len(world.a)
    # …and every shape reads as the loaded case says it does.
    rows = {row.case_id: row for row in single}
    a = world.a
    assert rows[a["diagnosis"].case_id].stage == "diagnosis"
    assert rows[a["mitigation"].case_id].stage == "mitigation"
    assert rows[a["treatment"].case_id].stage == "treatment"
    assert rows[a["null_mitigation"].case_id].stage == "treatment"
    assert rows[a["missing_keys"].case_id].stage == "diagnosis"
    assert rows[a["closed"].case_id].stage is None
    assert rows[a["closed"].case_id].is_terminal is True
    assert (
        rows[a["duplicate_aside"].case_id].current_turn,
        rows[a["duplicate_aside"].case_id].investigation_turn,
    ) == (4, 2)
    assert (
        rows[a["lagging_clock"].case_id].current_turn,
        rows[a["lagging_clock"].case_id].investigation_turn,
    ) == (3, 2)
    assert rows[a["empty"].case_id].current_turn == 0
    assert rows[a["shared"].case_id].shared_team_ids == world.teams
    # Codepoint order, stated by value: "B" (0x42) before "a" (0x61).
    assert rows[a["shared_mixed_case"].case_id].shared_team_ids == (
        world.mixed_case_teams
    )
    assert rows[a["diagnosis"].case_id].organization_id is not None
    # The deleted owner's case is dropped by both paths' projection, not by the
    # read: the metadata read did return it.
    assert a["orphan"].case_id in {m.case_id for m in metadata}

    assert multi == single


async def test_a_page_boundary_among_tied_updates_falls_in_the_same_place(world):
    """Six cases share one ``updated_at``. Both paths break the tie on
    ``case_id``, so paging through them — a boundary falling mid-tie — serves
    the same rows on the same pages, and each exactly once."""
    from faultmaven.infrastructure.persistence.sessionless_share_repository import (
        SessionlessShareRepository,
    )
    from faultmaven.models.api_models import CaseListFilter
    from faultmaven.modules.case.domain.services.case_service import CaseService
    from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
        SessionlessCaseRepository,
    )

    service = CaseService(
        case_repository=SessionlessCaseRepository(),
        team_service=object(),
        share_repository=SessionlessShareRepository(),
    )
    pages = [(0, 4), (4, 2)]

    set_current_enterprise_id(world.ent_a)
    single = []
    for offset, limit in pages:
        summaries, _ = await service.list_all_cases(
            CaseListFilter(limit=limit, offset=offset)
        )
        single.append([summary.case_id for summary in summaries])

    multi = []
    for offset, limit in pages:
        metadata, _ = await _read_everything(limit=limit, offset=offset)
        multi.append([m.case_id for m in metadata])

    assert single == [world.tied[:4], world.tied[4:]]
    assert multi == single


async def test_an_offset_past_32_bits_is_an_empty_page_with_the_true_total(world):
    """The API bounds neither limit nor offset to 32 bits, so neither does the
    function: an offset past 2**31 - 1 is an empty page, not a database error."""
    metadata, total = await _read_everything(limit=10, offset=2**31)
    ((expected,),) = await _as_owner(world.owner, "SELECT count(*) FROM cases")
    assert metadata == []
    assert total == expected >= len(world.order)


# =============================================================================
# Malformed stored JSON: served, never failing the page, never dropped
# =============================================================================

_WELL_FORMED_TURN = {"turn_number": 1, "outcome": "conversation", "progress_made": True}

#: label → (JSON path to overwrite, raw JSON value, expected (stage, turn)).
#: ``"derived"`` means the field is computed as for a well-formed case.
_MALFORMED = {
    "gate_as_string": ("{progress,solution_accepted}", '"yes"', (None, 1)),
    "gate_as_null": ("{progress,solution_verified}", "null", (None, 1)),
    "mitigation_as_number": ("{progress,mitigation}", "5", (None, 1)),
    "mitigation_gate_as_string": (
        "{progress,mitigation}",
        '{"accepted": "true", "verified": false}',
        (None, 1),
    ),
    "entry_not_an_object": ("{metadata,turn_history}", "[1, 2]", ("diagnosis", None)),
    "entry_without_turn_number": (
        "{metadata,turn_history}",
        '[{"outcome": "conversation", "progress_made": true}]',
        ("diagnosis", None),
    ),
    "turn_number_as_string": (
        "{metadata,turn_history}",
        '[{"turn_number": "1", "outcome": "conversation", "progress_made": true}]',
        ("diagnosis", None),
    ),
    "turn_number_fractional": (
        "{metadata,turn_history}",
        '[{"turn_number": 1.5, "outcome": "conversation", "progress_made": true}]',
        ("diagnosis", None),
    ),
    "outcome_missing": (
        "{metadata,turn_history}",
        '[{"turn_number": 1, "progress_made": true}]',
        ("diagnosis", None),
    ),
    "history_not_an_array": (
        "{metadata,turn_history}",
        '{"turn_number": 1}',
        ("diagnosis", None),
    ),
    # An integral number stored as a float reads as that integer — a plain
    # '1.0'::integer cast would have failed the whole page.
    "turn_number_integral_float": (
        "{metadata,turn_history}",
        '[{"turn_number": 1.0, "outcome": "conversation", "progress_made": true}]',
        ("diagnosis", 1),
    ),
}


@pytest.fixture
async def malformed_world(limited_role_env):
    """One enterprise of cases whose stored JSON is malformed in one way each,
    plus a well-formed control. Written well-formed through the production
    writer, then overwritten as the owner — the writer validates, so it cannot
    produce these shapes; a stored row still can."""
    from faultmaven.modules.case.domain.models.turn import TurnOutcome, TurnProgress
    from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
        SessionlessCaseRepository,
    )

    owner = limited_role_env
    suffix = uuid.uuid4().hex[:8]
    enterprise, user = f"ent_meta_m_{suffix}", f"user_meta_m_{suffix}"
    try:
        await _as_owner(
            owner,
            "INSERT INTO enterprises (enterprise_id, name, slug) VALUES (:e, :e, :e)",
            e=enterprise,
        )
        await _as_owner(
            owner,
            "INSERT INTO users (user_id, enterprise_id, username, email, "
            "display_name) VALUES (:u, :e, :u, :m, :u)",
            u=user,
            e=enterprise,
            m=f"{user}@example.com",
        )
        cases = {
            label: _case(
                enterprise,
                user,
                label,
                state=CaseState.INVESTIGATING,
                turn_history=[
                    TurnProgress(
                        turn_number=1,
                        outcome=TurnOutcome.CONVERSATION,
                        progress_made=True,
                    )
                ],
                current_turn=1,
            )
            for label in [*_MALFORMED, "control"]
        }
        set_current_enterprise_id(enterprise)
        repository = SessionlessCaseRepository()
        for case in cases.values():
            await repository.save(case)
        set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)

        for label, (path, value, _) in _MALFORMED.items():
            column, key = path.strip("{}").split(",", 1)
            await _as_owner(
                owner,
                f"UPDATE cases SET {column} = jsonb_set({column}, "
                f"CAST(:path AS text[]), CAST(:value AS jsonb)) WHERE case_id = :c",
                path=[key],
                value=value,
                c=cases[label].case_id,
            )
        yield SimpleNamespace(enterprise=enterprise, cases=cases)
    finally:
        await _as_owner(
            owner, "DELETE FROM enterprises WHERE enterprise_id = :e", e=enterprise
        )


async def test_a_malformed_case_is_served_not_dropped_and_not_fatal(
    malformed_world, caplog
):
    """One malformed case never fails the page, and is never dropped — the
    operator list is how a broken case gets found. It is served with its column
    data, the fields its inputs cannot derive left null, and a warning naming it
    and its enterprise. (The single-tenant list cannot load such a case at all;
    that is the one place the two paths differ.)"""
    from faultmaven.api.routes.admin_cases import _project_case_metadata

    with caplog.at_level("WARNING"):
        metadata, total = await _read_everything()
    ours = {
        m.case_id: m for m in metadata if m.enterprise_id == malformed_world.enterprise
    }
    rows = {row.case_id: row for row in _project_case_metadata(list(ours.values()))}

    cases = malformed_world.cases
    assert set(rows) == {case.case_id for case in cases.values()}, "a case was dropped"
    assert total >= len(cases)

    control = rows[cases["control"].case_id]
    assert (control.stage, control.investigation_turn) == ("diagnosis", 1)

    for label, (_, _, (stage, turn)) in _MALFORMED.items():
        case_id = cases[label].case_id
        row = rows[case_id]
        assert (row.state, row.enterprise_id, row.current_turn) == (
            "investigating",
            malformed_world.enterprise,
            1,
        ), label
        assert (row.stage, row.investigation_turn) == (stage, turn), label
        if None in (stage, turn):
            assert case_id in caplog.text and malformed_world.enterprise in caplog.text


# =============================================================================
# I3 — isolation is unchanged
# =============================================================================


async def test_the_read_spans_enterprises_while_the_session_stays_scoped(world):
    """In ONE session bound to enterprise A: the definer function returns A's
    and B's rows, and a plain query on ``cases`` still returns only A's."""
    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.modules.case.infrastructure.case_metadata_reader import (
        PostgreSQLCaseMetadataReader,
    )

    ids = world.order
    set_current_enterprise_id(world.ent_a)
    async with get_db_session() as session:
        metadata, _ = await PostgreSQLCaseMetadataReader(session).list_case_metadata(
            state=None, source=None, limit=100_000, offset=0
        )
        visible = set(
            (
                await session.execute(
                    text("SELECT case_id FROM cases WHERE case_id = ANY(:ids)"),
                    {"ids": ids},
                )
            )
            .scalars()
            .all()
        )
        bound = (
            await session.execute(
                text("SELECT current_setting('app.current_enterprise_id', true)")
            )
        ).scalar()

    assert bound == world.ent_a
    spanned = {m.enterprise_id for m in metadata if m.case_id in set(ids)}
    assert spanned == {world.ent_a, world.ent_b}
    assert visible == {case.case_id for case in world.a.values()}


# =============================================================================
# I4 — the single-tenant list's semantics
# =============================================================================


@pytest.mark.parametrize(
    "state, source",
    [
        (None, None),
        (CaseState.INVESTIGATING, None),
        (None, "slack"),
        (CaseState.INVESTIGATING, "slack"),
        (CaseState.CLOSED, "copilot"),
    ],
)
async def test_filters_and_the_total_count_every_enterprise(world, state, source):
    metadata, total = await _read_everything(state=state, source=source)

    where, params = ["TRUE"], {}
    if state:
        where.append("state = :state")
        params["state"] = state.value
    if source:
        where.append("source = :source")
        params["source"] = source
    ((expected_total,),) = await _as_owner(
        world.owner, f"SELECT count(*) FROM cases WHERE {' AND '.join(where)}", **params
    )

    assert total == expected_total == len(metadata)
    assert all(state is None or m.state is state for m in metadata)
    assert all(source is None or m.source == source for m in metadata)
    # Every fixture that matches is present, in both enterprises.
    expected = {
        case.case_id
        for case in list(world.a.values()) + list(world.b.values())
        if (state is None or case.state is state)
        and (source is None or case.source == source)
    }
    assert expected <= {m.case_id for m in metadata}


async def test_pages_are_newest_first_and_empties_are_included(world):
    everything, total = await _read_everything()
    updated = [m.updated_at for m in everything]
    assert updated == sorted(updated, reverse=True)
    assert [m.case_id for m in everything[: len(world.order)]] == world.order
    assert world.a["empty"].case_id in {m.case_id for m in everything}

    paged = []
    for offset in range(0, len(world.order), 3):
        page, page_total = await _read_everything(limit=3, offset=offset)
        assert page_total == total
        paged.extend(m.case_id for m in page)
    assert paged[: len(world.order)] == world.order

    beyond, beyond_total = await _read_everything(limit=3, offset=total + 10)
    assert beyond == [] and beyond_total == total


# =============================================================================
# I7 — failure direction, and the revision itself
# =============================================================================


@pytest.mark.parametrize(
    "dropped",
    [
        "admin_case_metadata_count(text, text)",
        "admin_case_metadata_page(text, text, bigint, bigint)",
    ],
)
async def test_a_database_without_the_functions_raises_rather_than_narrows(
    limited_role_env, dropped
):
    """Dropped inside a transaction that is rolled back, so the rest of the lane
    never sees it. Either missing function fails the read with the named error
    the route turns into a 503."""
    from faultmaven.modules.case.domain.models.metadata import (
        CaseMetadataUnavailableError,
    )
    from faultmaven.modules.case.infrastructure.case_metadata_reader import (
        PostgreSQLCaseMetadataReader,
    )

    engine = create_async_engine(limited_role_env, future=True)
    try:
        async with engine.connect() as conn:
            transaction = await conn.begin()
            await conn.execute(text(f"DROP FUNCTION {dropped}"))
            session = AsyncSession(bind=conn)
            with pytest.raises(CaseMetadataUnavailableError):
                await PostgreSQLCaseMetadataReader(session).list_case_metadata(
                    state=None, source=None, limit=1, offset=0
                )
            await session.close()
            await transaction.rollback()
        async with engine.connect() as conn:
            present = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_proc WHERE proname IN "
                        "('admin_case_metadata_page', 'admin_case_metadata_count')"
                    )
                )
            ).scalar()
        assert present == 2, "the rollback did not restore the functions"
    finally:
        await engine.dispose()


# =============================================================================
# Who may execute — the grant is the second half of the bound
# =============================================================================


async def test_no_role_holds_execute_without_an_explicit_grant(limited_role_env):
    """Left at the PostgreSQL default, a new function is executable by PUBLIC —
    and every login role holds CONNECT through PUBLIC — so any role able to
    connect could read every enterprise's case metadata. Revision 003 revokes
    PUBLIC; a fresh role with no grant of any kind must not be able to execute
    either function, while the role granted as the deployment grants its
    runtime role can."""
    fresh = f"fm_casemeta_nogrant_{uuid.uuid4().hex[:8]}"
    await _as_owner(limited_role_env, drop_role_sql(fresh))
    await _as_owner(limited_role_env, f"CREATE ROLE {fresh} NOLOGIN")
    try:
        for signature in CASE_METADATA_FUNCTIONS:
            ((fresh_may, granted_may, public_in_acl),) = await _as_owner(
                limited_role_env,
                "SELECT has_function_privilege(:fresh, CAST(:f AS regprocedure), "
                "'EXECUTE'), has_function_privilege(:granted, "
                "CAST(:f AS regprocedure), 'EXECUTE'), "
                "EXISTS (SELECT 1 FROM pg_proc p, aclexplode(COALESCE(p.proacl, "
                "acldefault('f', p.proowner))) a "
                "WHERE p.oid = CAST(:f AS regprocedure) AND a.grantee = 0)",
                fresh=fresh,
                granted=_ROLE,
                f=signature,
            )
            assert fresh_may is False, f"{signature} is executable without a grant"
            assert public_in_acl is False, f"{signature} grants PUBLIC"
            assert granted_may is True, f"{signature}: the granted role lost it"
    finally:
        await _as_owner(limited_role_env, drop_role_sql(fresh))


class _ReaderAs:
    """The production reader over a session of a given role."""

    def __init__(self, url: str):
        self.url = url

    async def list_case_metadata(self, **filters):
        from faultmaven.modules.case.infrastructure.case_metadata_reader import (
            PostgreSQLCaseMetadataReader,
        )

        engine = create_async_engine(self.url, future=True)
        try:
            async with AsyncSession(bind=engine) as session:
                return await PostgreSQLCaseMetadataReader(session).list_case_metadata(
                    **filters
                )
        finally:
            await engine.dispose()


async def test_a_role_without_the_grant_gets_a_503_not_a_partial_list(
    limited_role_env,
):
    """A deployment whose runtime role was never granted EXECUTE — a different
    role name, or a role created after the migration ran. The reader names the
    cause, and the route answers 503 without ever reaching the RLS-scoped list.
    """
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from faultmaven.api.middleware.auth import require_platform_admin
    from faultmaven.api.routes.admin_cases import (
        get_case_metadata_reader,
        get_case_service,
        get_operator_audit_repository,
        router,
    )
    from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
    from faultmaven.modules.case.domain.models.metadata import (
        CaseMetadataNotGrantedError,
    )

    role = f"fm_casemeta_ungranted_{uuid.uuid4().hex[:8]}"
    await create_limited_role(limited_role_env, role, _PW, grant_case_metadata=False)
    try:
        reader = _ReaderAs(limited_url(limited_role_env, role, _PW))
        with pytest.raises(CaseMetadataNotGrantedError):
            await reader.list_case_metadata(state=None, source=None, limit=10, offset=0)

        case_service = AsyncMock()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_platform_admin] = lambda: AuthenticatedUser(
            user_id="op-1",
            enterprise_id="ent-operator",
            email="operator@example.com",
            roles=["platform_admin"],
            permissions=[],
        )
        app.dependency_overrides[get_operator_audit_repository] = lambda: AsyncMock()
        app.dependency_overrides[get_case_service] = lambda: case_service
        app.dependency_overrides[get_case_metadata_reader] = lambda: reader
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/admin/cases")

        assert response.status_code == 503, response.text
        assert "EXECUTE" in response.json()["detail"]
        case_service.list_all_cases.assert_not_called()
    finally:
        await drop_limited_role(limited_role_env, role)


async def test_revision_003_steps_down_and_up(limited_role_env):
    """In a database of its own: downgrading the shared one would pull the
    functions out from under every other test in the lane.

    With the runtime role present, so the migration's grant branch runs — the
    shared database was migrated before any such role existed, which exercises
    the other one."""
    async with database_of_its_own(
        limited_role_env, "fm_casemeta_updown", runtime_role=True
    ) as url:

        async def runtime_role_may_execute():
            engine = create_async_engine(url, future=True)
            try:
                async with engine.connect() as conn:
                    return [
                        (
                            await conn.execute(
                                text(
                                    "SELECT has_function_privilege(:r, "
                                    "CAST(:f AS regprocedure), 'EXECUTE')"
                                ),
                                {"r": RUNTIME_ROLE, "f": signature},
                            )
                        ).scalar()
                        for signature in CASE_METADATA_FUNCTIONS
                    ]
            finally:
                await engine.dispose()

        async def functions_and_ledger():
            engine = create_async_engine(url, future=True)
            try:
                async with engine.connect() as conn:
                    functions = set(
                        (
                            await conn.execute(
                                text(
                                    "SELECT proname FROM pg_proc WHERE proname "
                                    "LIKE 'admin_case_metadata_%'"
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    ledger = (
                        await conn.execute(
                            text(
                                "SELECT count(*) FROM pg_tables "
                                "WHERE tablename = 'llm_usage_daily'"
                            )
                        )
                    ).scalar()
                    return functions, ledger
            finally:
                await engine.dispose()

        both = {"admin_case_metadata_page", "admin_case_metadata_count"}

        result = alembic_on(url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await functions_and_ledger() == (both, 1)
        assert await runtime_role_may_execute() == [True, True]

        # To 003's parent, stepping over whatever was added after it.
        result = alembic_on(url, f"downgrade {_LEDGER_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        # Only what 003 added went; 002's ledger is still there.
        assert await functions_and_ledger() == (set(), 1)

        result = alembic_on(url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await functions_and_ledger() == (both, 1)

        result = alembic_on(url, f"downgrade {_BASELINE_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await functions_and_ledger() == (set(), 0)
