"""The knowledge base's log lines name an exception by its class and code,
never its text (#2889 S3c).

Principles §7, "What a log line may hold", rule 1: "A message that interpolates
an exception's text, a payload, or a user-supplied value is not metadata. A
first-party exception contributes its class and its ``error_code`` ..., not its
message." Stage S3 of #2886.

Each case drives one site with :data:`MARKER` in the exception's message,
captures every record at DEBUG, and renders each through the real chain's
formatter (``build_log_formatter``). The marker is on no rendered line, and the
class name (with its code, where first-party) is on the site's line. Each site
is driven through the method that holds it, its collaborator stubbed to raise.
"""

import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from faultmaven.infrastructure import worker_process
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.infrastructure.persistence.models import ConversionDraftModel
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.services import runbook_gate
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.exceptions import GateUnavailable
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
)
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)
from tests.unit.modules.knowledge.test_runbook_body_refused_not_rewritten_2012 import (  # noqa: E501
    _conversion,
    _knowledge,
    _template,
    session_factory,  # noqa: F401 - fixture
)
from tests.utils import admit_every_parent, make_org_knowledge_item

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _service(vector_store=None) -> KnowledgeService:
    tracer = MagicMock()
    tracer.trace = MagicMock(
        return_value=MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None)
    )
    return KnowledgeService(
        sanitizer=MagicMock(asanitize=AsyncMock(side_effect=lambda q: q)),
        tracer=tracer,
        vector_store=vector_store or MagicMock(),
        db_session_factory=MagicMock(),
    )


# ── infrastructure/knowledge/knowledge_vector_store.py ────────────────────


def test_a_failed_collection_get_or_create_is_named_by_class(lines):
    client = MagicMock()
    client.get_or_create_collection.side_effect = RuntimeError(f"chroma {MARKER}")
    store = KnowledgeVectorStore(client=client)
    with pytest.raises(RuntimeError):
        store._get_or_create_collection("kb_test")
    _named(lines(), "Failed to get/create KB collection", "RuntimeError")


# ── infrastructure/knowledge/runbook_kb.py ────────────────────────────────


async def test_a_failed_runbook_query_is_named_by_class(lines):
    """The raised ``KnowledgeBaseError`` is an exception message, not a log
    line, and is not asserted on."""
    store = MagicMock()
    store.query_by_embedding = AsyncMock(side_effect=RuntimeError(f"down {MARKER}"))
    kb = RunbookKnowledgeBase(vector_store=store, admit=admit_every_parent)
    with pytest.raises(KnowledgeBaseError):
        await kb.search_runbooks(
            query_embedding=[0.1] * 1024, scope_filter={"scope": "global"}
        )
    _named(lines(), "ChromaDB runbook query failed", "RuntimeError")


# ── modules/knowledge/domain/services/knowledge_service.py ────────────────


async def test_a_failed_search_is_named_by_class_and_code(lines):
    store = MagicMock()
    store.hybrid_search = AsyncMock(side_effect=KnowledgeBaseError(f"bad {MARKER}"))
    with pytest.raises(KnowledgeBaseError):
        await _service(store).search_knowledge("pod crashloop")
    _named(lines(), "Search failed", "KnowledgeBaseError[KNOWLEDGE_BASE_ERROR]")


async def test_a_failed_repair_encode_is_named_by_class(lines):
    """The recurrence site (``fa8fc53c0``): one encode fails during
    ``reindex_missing_vectors``."""
    service = _service()
    service._repairable_document = AsyncMock(
        return_value=(MagicMock(content="# T\n\nbody"), "ent_1", "1f" * 16)
    )
    service._chunks = MagicMock(return_value=["# T\n\nbody"])
    service._index_ticket = AsyncMock(side_effect=RuntimeError(f"encode {MARKER}"))
    assert await service.reindex_missing_vectors(["kb_1"]) == {"kb_1": 0}
    _named(lines(), "one encode failed", "RuntimeError")


# ── modules/knowledge/domain/services/conversion_service/service.py ───────


async def test_a_failed_draft_ingestion_is_named_by_class(
    lines, session_factory, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    knowledge = _knowledge(session_factory)
    conversion = _conversion(session_factory, knowledge)
    created = await conversion.create_runbook_from_template(**_template())
    async with session_factory() as session:
        row = await session.get(ConversionDraftModel, created["draft"].draft_id)
        row.validation_passed = True
        await session.commit()
    knowledge.ingest_runbook = AsyncMock(side_effect=RuntimeError(f"ingest {MARKER}"))
    with pytest.raises(RuntimeError):
        await conversion.verify_draft(
            created["conversion_id"], created["draft"].draft_id, "u1"
        )
    _named(lines(), "Ingestion failed for draft", "RuntimeError")


# ── modules/knowledge/domain/services/runbook_gate.py ─────────────────────


async def test_checks_that_do_not_answer_are_named_by_class(lines, monkeypatch):
    monkeypatch.setattr(runbook_gate, "_gate_slots_state", None)
    monkeypatch.setattr(
        worker_process,
        "run",
        AsyncMock(side_effect=worker_process.WorkerFailed(f"frame {MARKER}")),
    )
    with pytest.raises(GateUnavailable):
        await runbook_gate.avalidate_and_score("body", principal="user_1")
    _named(lines(), "did not answer", "WorkerFailed")


# ── modules/knowledge/infrastructure/persistence/knowledge_item_repository.py


async def test_a_foreign_key_violation_names_the_drivers_class(lines):
    """``create``'s FK-violation line (``knowledge_item_repository.py:549`` on
    the base), which named the driver's exception ``e.orig``."""
    db = MagicMock()
    db.commit = AsyncMock(
        side_effect=IntegrityError(
            "INSERT INTO knowledge_items",
            {},
            sqlite3.IntegrityError(f"FOREIGN KEY constraint failed {MARKER}"),
        )
    )
    db.rollback = AsyncMock()
    item = make_org_knowledge_item(item_id="kb_fk", enterprise_id="ent_1")
    with pytest.raises(Exception, match="foreign key violation"):
        await DatabaseKnowledgeItemRepository(db).create(item)
    _named(lines(), "FK violation", "FK violation: IntegrityError")
