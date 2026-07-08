"""App half of the AUTHORING -> PACK -> INGEST cross-repo cause-shape contract.

The kb-toolkit ships a per-Cause ``causes`` record in every pack; the app
ingests it verbatim onto the ``knowledge_items`` row (``metadata['causes']``,
written by the pack bootstrap and the case->runbook conversion path). The
toolkit half (``faultmaven-kb-toolkit`` ``tests/unit/test_pack_causes_contract.py``)
proves a real authored runbook packs into a ``causes`` record with exactly the
contract field set. THIS half proves that exact record flows lossless through
the app's real ``ingest_runbook`` into a real SQL row.

The two halves are joined by a shared golden fixture: this repo vendors a
byte-identical copy of the toolkit's ``expected_pack_causes.json`` (real
``build_pack`` output). See ``tests/fixtures/runbook_cause_lifecycle/PROVENANCE.md``.

Only the vector-store write is mocked; the SQL persistence and read-back are
real (in-memory SQLite).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    OrganizationModel,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import VerificationLevel
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
)

pytestmark = pytest.mark.integration

# The authored runbook's app-derived item_id (kb_<sha256(frontmatter id)[:12]>).
# Stable for ``cause-lifecycle-sample-runbook``.
RUNBOOK_ITEM_ID = "kb_62620ab62e3f"
DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"

GOLDEN_CAUSES = (
    Path(__file__).resolve().parents[3]  # .../tests
    / "fixtures"
    / "runbook_cause_lifecycle"
    / "expected_pack_causes.json"
)

# SHA-256 of the vendored golden. This file is a byte-identical copy of the
# kb-toolkit golden (real ``build_pack`` output) — the shared artifact that joins
# the two halves of the cross-repo cause-shape contract (see PROVENANCE.md).
# Nothing can compare the two repos' copies in one CI run, so this hash is the
# cross-repo pin: it MUST equal ``EXPECTED_GOLDEN_SHA256`` in the kb-toolkit
# ``test_pack_causes_contract.py``. Any edit to either copy fails its own repo's
# test until the hash is regenerated, forcing a conscious re-sync of both copies
# (the field-set checks below pin keys, not values — only this catches value
# drift between the two vendored copies). Regenerate via kb-toolkit
# ``regen_golden.py``, copy here, update this constant AND the kb-toolkit one.
EXPECTED_GOLDEN_SHA256 = (
    "59a183bd40dfaaae5b65529790b5a4ea502a8c5dda2b77eed47147faeb08432d"
)

# The per-Cause record's exact field set — the producer<->consumer contract.
# Must equal ``CAUSE_RECORD_FIELDS`` in the kb-toolkit ``test_pack_causes_contract.py``
# (the producer half pins the same literal). The app stores these records
# verbatim (``knowledge_items.metadata['causes']``); nothing may silently add or
# drop a key on either side.
CAUSE_RECORD_FIELDS = {
    "cause_letter",
    "cause_name",
    "cause_statement",
    "chain_nodes",
    "chain_edges",
    "rung_indicators",
    "interventions",
    "is_fallback_cause",
}


def _load_golden() -> list[dict]:
    return json.loads(GOLDEN_CAUSES.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Real-SQLite KnowledgeService (vector half mocked) — mirrors test_ingest_runbook
# --------------------------------------------------------------------------- #


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def seeded_service(session_factory):
    """A real KnowledgeService backed by in-memory SQLite (seeded enterprise +
    org so the knowledge_items FK is satisfied); only the ChromaDB write is
    mocked so we can assert SQL persistence of ``metadata['causes']``."""
    async with session_factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=DEFAULT_ENTERPRISE_ID, name="Default", slug="default"
            )
        )
        session.add(
            OrganizationModel(
                organization_id=DEFAULT_ORG_ID,
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                name="Default Org",
                slug="default-org",
            )
        )
        await session.commit()

    service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )
    # Load-bearing: ingest_runbook DELETES the just-written SQL row (incl.
    # metadata['causes']) and raises if the vector write returns <= 0 or raises.
    # A positive int is required for the row to survive — keep this an int >= 1.
    service._index_document_in_vector_store = AsyncMock(return_value=2)
    return service


@pytest.fixture
async def ingested(seeded_service, session_factory):
    """INGEST the golden causes through the real ingest_runbook, return the
    session factory + golden so each test can read back the persisted row."""
    golden = _load_golden()
    await seeded_service.ingest_runbook(
        document_id=RUNBOOK_ITEM_ID,
        title="Database connection pool exhaustion",
        content="# Database connection pool exhaustion\n\nRunbook body.",
        organization_id=DEFAULT_ORG_ID,
        scope="global",
        causes=golden,
        verification_level=VerificationLevel.COMMUNITY,
    )
    return session_factory, golden


async def _read_persisted_causes(session_factory, item_id: str):
    async with session_factory() as session:
        repo = DatabaseKnowledgeItemRepository(session)
        item = await repo.get_by_id(item_id)
    assert item is not None, f"ingested runbook {item_id} not found"
    return (item.metadata or {}).get("causes")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_golden_matches_cross_repo_hash():
    """The vendored golden must be byte-identical to the kb-toolkit copy. The
    field-set checks elsewhere pin keys, not values; this hash is the only guard
    against value drift between the two hand-copied golden files (it must equal
    the kb-toolkit ``EXPECTED_GOLDEN_SHA256``)."""
    actual = hashlib.sha256(GOLDEN_CAUSES.read_bytes()).hexdigest()
    assert actual == EXPECTED_GOLDEN_SHA256, (
        "Golden changed without re-sync. Regenerate via kb-toolkit "
        "regen_golden.py, copy the file here, and update EXPECTED_GOLDEN_SHA256 "
        "in BOTH repos' tests to this value: " + actual
    )


class TestIngestRoundTrip:
    @pytest.mark.asyncio
    async def test_causes_survive_ingest_losslessly(self, ingested):
        """The persisted row's ``metadata['causes']`` reads back exactly what
        was authored — the round-trip through the SQL JSON column loses
        nothing."""
        session_factory, golden = ingested
        persisted = await _read_persisted_causes(session_factory, RUNBOOK_ITEM_ID)
        assert persisted == golden

    @pytest.mark.asyncio
    async def test_persisted_causes_carry_exact_contract_field_set(self, ingested):
        """SHAPE: each persisted Cause has exactly the contract field set. This
        is the consumer-side drift guard — the toolkit half pins the same
        literal set on the producer side."""
        session_factory, _ = ingested
        persisted = await _read_persisted_causes(session_factory, RUNBOOK_ITEM_ID)

        for entry in persisted:
            keys = set(entry)
            assert keys == CAUSE_RECORD_FIELDS, (
                f"persisted Cause {entry.get('cause_letter')} drifted from the "
                f"contract: extra={keys - CAUSE_RECORD_FIELDS} "
                f"missing={CAUSE_RECORD_FIELDS - keys}"
            )
