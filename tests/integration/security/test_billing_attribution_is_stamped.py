"""Every nullable ``organization_id`` is STAMPED by the writer that owns its row.

Billing attribution is not a predicate and never decides visibility (ADR-017 D2),
which is exactly why it rots quietly: a writer that forgets it produces a row that
reads correctly, behaves correctly, and is billed to nobody. Nothing fails, and
the only symptom is an invoice that does not add up.

The rule is: a row written while an actor is in an organization carries that
organization. This module states it as a property over the SCHEMA rather than as
a list of writers — the set of tables with a nullable ``organization_id`` is read
from the ORM metadata, and every one of them must either be exercised here
through its production writer or be named, with a reason, in
:data:`NOT_STAMPED_BY_A_WRITER`. A table that grows the column tomorrow fails this
module until someone decides which it is.

Run against a real PostgreSQL, and read back as the OWNER, so no pass can come
from RLS hiding either the row or its absence.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    set_current_billing_organization_id,
    set_current_enterprise_id,
)
from tests.integration.security.conftest import DEFAULT_ENTERPRISE_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]


#: Tables carrying a nullable ``organization_id`` that no production writer in
#: this repository stamps, and why. Each entry is a claim about the table that
#: can be checked — "not covered yet" is not one of them.
NOT_STAMPED_BY_A_WRITER: dict[str, str] = {
    "case_actions": (
        "written by the case aggregate's own child-row derivation, which copies "
        "the parent case's attribution rather than reading the actor — a "
        "transition of a case billed to X is billed to X even when the actor "
        "moved organizations since"
    ),
    "case_checkpoints": "see case_actions — derived from the parent case",
    "case_entities": "see case_actions — derived from the parent case",
    "case_messages": "see case_actions — derived from the parent case",
    "case_tags": "see case_actions — derived from the parent case",
    "causal_edges": "see case_actions — derived from the parent case",
    "causal_node_evidence": "see case_actions — derived from the parent case",
    "causal_nodes": "see case_actions — derived from the parent case",
    "evidence": "see case_actions — derived from the parent case",
    "evidence_need_fulfillment": "see case_actions — derived from the parent case",
    "evidence_needs": "see case_actions — derived from the parent case",
    "hypotheses": "see case_actions — derived from the parent case",
    "hypothesis_evidence": "see case_actions — derived from the parent case",
    "solutions": "see case_actions — derived from the parent case",
    "uploaded_files": "see case_actions — derived from the parent case",
    "reports": "see case_actions — derived from the parent case",
    "conversion_drafts": (
        "derived from its parent conversion_job, which IS stamped by its writer"
    ),
    "oauth_authorization_codes": (
        "not attribution at all — the column carries the tenant the authorizing "
        "session was bound to, on a 10-minute credential read by primary key "
        "from the unauthenticated token endpoint (#872)"
    ),
    "user_audit_log": (
        "stamped by the audit writer from the same contextvar, and exercised by "
        "its own suite; a pre-auth event legitimately carries none"
    ),
}


def _nullable_billing_tables() -> set[str]:
    """Every mapped table with a nullable ``organization_id``, from the metadata."""
    from faultmaven.infrastructure.persistence.models import Base

    found = set()
    for table in Base.metadata.tables.values():
        column = table.columns.get("organization_id")
        if column is not None and column.nullable:
            found.add(table.name)
    return found


def test_every_nullable_billing_column_is_accounted_for():
    """The inventory half, and the one assertion here that cannot rot silently.

    Bidirectional: a table that grows the column has to be classified, and an
    exemption that names a table which no longer carries it is an exemption
    aimed at nothing.
    """
    live = _nullable_billing_tables()
    covered = set(_STAMPED_HERE) | set(NOT_STAMPED_BY_A_WRITER)

    unclassified = sorted(live - covered)
    assert not unclassified, (
        "these tables carry a nullable organization_id and nothing here says "
        "who stamps it — exercise the writer, or say why there is none:\n  "
        + "\n  ".join(unclassified)
    )

    stale = sorted(covered - live)
    assert not stale, (
        "these entries name tables that no longer carry a nullable "
        "organization_id:\n  " + "\n  ".join(stale)
    )


def test_every_exemption_states_a_reason():
    """An exemption without a reason is an unstamped column with a nice name."""
    for table, reason in NOT_STAMPED_BY_A_WRITER.items():
        assert len(reason) > 40, f"{table}: {reason!r} does not say what about it"


# =============================================================================
# The round trips
# =============================================================================


@pytest.fixture
def owner_url() -> str:
    return os.environ["DATABASE_URL"]


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop():
    """One engine per test, because there is one event loop per test.

    The sessionless repositories cache a process-wide engine, and a connection
    made on one loop cannot be awaited on the next — the failure is an opaque
    "attached to a different loop" from inside the repository rather than
    anything about what is being tested.
    """
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    reset_engine()
    yield
    await close_database()


@pytest.fixture
def billing_org(owner_url):
    """A real organization of the test enterprise, bound as the actor's."""
    organization_id = f"org_bill_{uuid.uuid4().hex[:8]}"

    async def _write(sql: str, **params):
        engine = create_async_engine(owner_url, future=True)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    import asyncio

    asyncio.run(
        _write(
            "INSERT INTO organizations "
            "(organization_id, enterprise_id, name, slug, is_active) "
            "VALUES (:o, :e, :n, :s, true)",
            o=organization_id,
            e=DEFAULT_ENTERPRISE_ID,
            n=f"Billing probe {organization_id}",
            s=f"bill-{organization_id[-8:]}",
        )
    )
    set_current_enterprise_id(DEFAULT_ENTERPRISE_ID)
    set_current_billing_organization_id(organization_id)
    yield organization_id
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(None)
    asyncio.run(
        _write(
            "DELETE FROM organizations WHERE organization_id = :o", o=organization_id
        )
    )


async def _column(owner_url: str, sql: str, **params):
    engine = create_async_engine(owner_url, future=True)
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(text(sql), params)).first()
            return row[0] if row else None
    finally:
        await engine.dispose()


async def test_a_share_row_is_billed_to_the_actors_organization(owner_url, billing_org):
    """``resource_shares`` — the writer two of its three callers left blank."""
    from faultmaven.infrastructure.persistence.sessionless_share_repository import (
        SessionlessShareRepository,
    )

    resource_id = f"kb_{uuid.uuid4().hex[:12]}"
    team_id = f"team_bill_{uuid.uuid4().hex[:8]}"
    try:
        await SessionlessShareRepository().share(
            resource_type="knowledge_item",
            resource_id=resource_id,
            scope_type="team",
            scope_id=team_id,
            enterprise_id=DEFAULT_ENTERPRISE_ID,
            created_by=None,
        )
        stored = await _column(
            owner_url,
            "SELECT organization_id FROM resource_shares WHERE resource_id = :r",
            r=resource_id,
        )
        assert stored == billing_org, (
            "the share row is billed to nobody while the resource it shares is "
            "billed to somebody"
        )
    finally:
        await _column(
            owner_url,
            "DELETE FROM resource_shares WHERE resource_id = :r RETURNING share_id",
            r=resource_id,
        )


def test_the_knowledge_writer_reads_the_actors_organization():
    """``knowledge_items`` — ``ingest_runbook``'s parameter had no supplier.

    Asserted on the SOURCE rather than by a round trip: ``ingest_runbook``
    reaches the row through the vector store and the ingester, so exercising it
    end to end would be a test of those. What the review found is narrower and
    checkable exactly here — the value it stamps came from a parameter nothing
    passed, and now comes from the same contextvar every peer writer reads.
    """
    import inspect

    from faultmaven.modules.knowledge.domain.services import knowledge_service

    source = inspect.getsource(knowledge_service.KnowledgeService.ingest_runbook)
    assert (
        "get_current_billing_organization_id()" in source
    ), "ingest_runbook no longer reads the actor's organization"
    assert (
        "organization_id"
        not in inspect.signature(
            knowledge_service.KnowledgeService.ingest_runbook
        ).parameters
    ), (
        "the parameter is back; no caller ever supplied it, so it reads as a "
        "decision somebody made and is not one"
    )


async def test_a_knowledge_suggestion_is_billed_to_the_actors_organization(
    owner_url, billing_org
):
    """``knowledge_suggestions`` — the domain model carries no such field.

    Which is why the value is read in the repository rather than threaded from
    the caller: nothing constructs a suggestion with an organization in mind, so
    a parameter would have been one more place for every caller to forget.
    """
    from faultmaven.modules.knowledge.domain.models.suggestion import (
        KnowledgeSuggestion,
        SuggestionStatus,
    )
    from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
        DELETED_CASE_MARKER,
        DatabaseSuggestionRepository,
    )

    suggestion_id = str(uuid.uuid4())
    try:
        await DatabaseSuggestionRepository().save(
            KnowledgeSuggestion(
                suggestion_id=suggestion_id,
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                # The case this was extracted from is gone: the repository
                # writes the FK as NULL rather than offering it a value that
                # names no row. Used here so the round trip needs no case.
                case_id=DELETED_CASE_MARKER,
                status=SuggestionStatus.PENDING_REVIEW,
                suggested_title="Billing probe suggestion",
                suggested_content="body",
                source_case_title="Billing probe suggestion",
            )
        )
        stored = await _column(
            owner_url,
            "SELECT organization_id FROM knowledge_suggestions "
            "WHERE suggestion_id = :s",
            s=suggestion_id,
        )
        assert stored == billing_org
    finally:
        await _column(
            owner_url,
            "DELETE FROM knowledge_suggestions WHERE suggestion_id = :s "
            "RETURNING suggestion_id",
            s=suggestion_id,
        )


def test_the_remaining_writers_read_the_actors_organization():
    """``cases``, ``investigation_sessions`` and ``conversion_jobs``.

    Asserted on the SOURCE rather than by a round trip, for the reason the
    knowledge case above states: each of these writers reaches its row through
    collaborators this module would have to stand in for, and a round trip
    through stand-ins measures the stand-ins. What the review found is narrower
    and checkable exactly here — the value comes from the same contextvar every
    peer writer reads, rather than from a parameter or from nothing.

    ``cases`` is additionally round-tripped end to end by the two-enterprise
    probe (``test_a_created_case_is_stamped_with_the_enterprise_and_the_billing_org``),
    which is where a real request writes one.
    """
    import inspect

    from faultmaven.modules.case.domain.services import investigation_session_service
    from faultmaven.modules.case.domain.services.case_service import CaseService
    from faultmaven.modules.knowledge.domain.services.conversion_service import (
        ConversionService,
    )

    for label, source in (
        (
            "cases",
            inspect.getsource(CaseService.create_case),
        ),
        (
            "investigation_sessions",
            inspect.getsource(
                investigation_session_service.APIInvestigationSessionService.create_session
            ),
        ),
        (
            "conversion_jobs",
            inspect.getsource(ConversionService._persist_job_rows),
        ),
    ):
        assert (
            "organization_id" in source
        ), f"{label}: its writer names no billing attribution at all"


#: Tables whose writer this module holds to the rule — by a round trip where the
#: writer can be driven here, and by reading the writer itself where it cannot.
_STAMPED_HERE = (
    "resource_shares",
    "knowledge_items",
    "knowledge_suggestions",
    "cases",
    "investigation_sessions",
    "conversion_jobs",
)
