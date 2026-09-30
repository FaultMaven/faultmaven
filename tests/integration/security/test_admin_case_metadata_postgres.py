"""The cross-enterprise operator case list on a real PostgreSQL (ADR-012 D9).

Under ``TENANT_PROVIDER=multi`` the list reads every enterprise through the
``SECURITY DEFINER`` functions revision 003 creates. What only a real database
can answer:

* **The bound is structural.** The page function's declared result columns are
  an exact, asserted set of ids, closed-vocabulary strings, timestamps,
  integers, booleans and arrays of those — adding a column fails here, and a
  case whose title and description are sentinels never surfaces them.
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

Everything runs as a role with the deployed ``faultmaven_app`` grants and no
ownership, because PostgreSQL exempts superusers and table owners from RLS; the
fixtures are written through the production case writer under the enterprise's
binding, and only the shapes that writer cannot produce (a stored duplicate, a
lagging clock, a missing key) are then set as the owner.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.modules.case.domain.models.lifecycle import CaseState
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

_ROLE = f"fm_casemeta_probe_{uuid.uuid4().hex[:8]}"
_PW = "fm_casemeta_probe_pw"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BASELINE_REVISION = "a1e0c17bd001"

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
        for team in (team_1, team_2):
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
        # Two team shares and an organization-scope one, which is not a team.
        for scope_type, scope_id in (
            ("team", team_2),
            ("team", team_1),
            ("organization", org_a),
        ):
            await _as_owner(
                owner,
                "INSERT INTO resource_shares (share_id, resource_type, resource_id, "
                "scope_type, scope_id, enterprise_id) "
                "VALUES (:s, 'case', :c, :t, :i, :e)",
                s=str(uuid.uuid4()),
                c=a["shared"].case_id,
                t=scope_type,
                i=scope_id,
                e=ent_a,
            )
        # Distinct, far-future update times: a deterministic newest-first order.
        ordered = list(a.values()) + list(b.values())
        for minutes, case in enumerate(reversed(ordered)):
            await _as_owner(
                owner,
                "UPDATE cases SET updated_at = :t WHERE case_id = :c",
                t=_EPOCH + timedelta(minutes=minutes),
                c=case.case_id,
            )

        yield SimpleNamespace(
            owner=owner,
            ent_a=ent_a,
            ent_b=ent_b,
            a=a,
            b=b,
            teams=sorted([team_1, team_2]),
            # Newest first.
            order=[case.case_id for case in ordered],
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
    are enabled, never forced — and what keeps a caller's schema out of them."""
    rows = await _as_owner(
        limited_role_env,
        "SELECT p.proname, p.prosecdef, p.proconfig, "
        "pg_get_userbyid(p.proowner) = t.tableowner, c.relforcerowsecurity "
        "FROM pg_proc p, pg_tables t, pg_class c "
        "WHERE p.proname IN ('admin_case_metadata_page', 'admin_case_metadata_count') "
        "AND t.tablename = 'cases' AND c.relname = 'cases' ORDER BY p.proname",
    )
    assert [tuple(row) for row in rows] == [
        (
            "admin_case_metadata_count",
            True,
            ["search_path=pg_catalog, public"],
            True,
            False,
        ),
        (
            "admin_case_metadata_page",
            True,
            ["search_path=pg_catalog, public"],
            True,
            False,
        ),
    ]


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
    assert rows[a["diagnosis"].case_id].organization_id is not None
    # The deleted owner's case is dropped by both paths' projection, not by the
    # read: the metadata read did return it.
    assert a["orphan"].case_id in {m.case_id for m in metadata}

    assert multi == single


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
        "admin_case_metadata_page(text, text, integer, integer)",
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


def _alembic(url: str, command: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, DATABASE_URL=url)
    env["PYTHONPATH"] = f"{_PROJECT_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *command.split()],
        cwd=_PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


async def test_revision_003_steps_down_and_up(limited_role_env):
    """In a database of its own: downgrading the shared one would pull the
    functions out from under every other test in the lane."""
    name = f"fm_casemeta_updown_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(
        limited_role_env, future=True, isolation_level="AUTOCOMMIT"
    )
    url = (
        make_url(limited_role_env)
        .set(database=name)
        .render_as_string(hide_password=False)
    )
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:

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

        result = _alembic(url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await functions_and_ledger() == (both, 1)

        result = _alembic(url, "downgrade -1")
        assert result.returncode == 0, result.stderr[-2000:]
        # Only what 003 added went; 002's ledger is still there.
        assert await functions_and_ledger() == (set(), 1)

        result = _alembic(url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await functions_and_ledger() == (both, 1)

        result = _alembic(url, f"downgrade {_BASELINE_REVISION}")
        assert result.returncode == 0, result.stderr[-2000:]
        assert await functions_and_ledger() == (set(), 0)
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
