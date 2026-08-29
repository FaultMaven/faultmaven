"""Approval publishes what it claims to, or refuses (#1214).

Three things the approval path used to get wrong, none of them covered:

1. **The fake-id branch.** With no ``knowledge_service`` the service minted
   ``authored_item_id()`` and returned ``{"status": "approved"}`` for a
   knowledge item that was never created. Because ``app.state.suggestion_service``
   was never written, the routes always built a collaborator-less service — so
   that branch was the one 100% of production approvals took, and no test
   touched it.
2. **The quality gate was route-only.** ``POST /knowledge/documents`` validated
   frontmatter and required sections and answered 422; ``approve_suggestion``
   called the service directly and skipped all of it, so LLM-shaped markdown
   would be published as ``KnowledgeItemType.RUNBOOK`` at the platform tier.
3. **No compensation.** ``suggestion.approve()`` re-checks readiness and a
   concurrent edit resets ``pii_scan_status``, so it can raise AFTER the publish
   succeeded — leaving a published runbook with no back-link while the client is
   told the approval failed.

The gate now lives inside ``upload_document``, so proving (2) needs a REAL
``KnowledgeService``; a double would report the call and skip the gate, which is
the very substitution that let this ship. ``tests/integration/modules/knowledge/
test_suggestion_flow_1214.py`` drives the same ground through the HTTP routes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
    SuggestionStatus,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookQualityError,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)
from tests.runbook_samples import valid_runbook

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

ORG = "org_123"
SUGGESTION_ID = "sug_0001"
ITEM_ID = "kb_abcdef0123456789"


def _ready_suggestion(
    content: str = "## Problem\nPool exhausted.\n",
) -> "KnowledgeSuggestion":
    return KnowledgeSuggestion(
        suggestion_id=SUGGESTION_ID,
        organization_id=ORG,
        case_id="case_aabb11223344",
        suggested_title="Redis pool exhaustion",
        suggested_content=content,
        suggested_type="troubleshooting_guide",
        extracted_by="user_extractor",
        pii_scan_status=PIIScanStatus.CLEAN,
    )


def _service(knowledge_service, content: str = "## Problem\nPool exhausted.\n"):
    svc = SuggestionService(knowledge_service=knowledge_service)
    svc._suggestions_store[SUGGESTION_ID] = _ready_suggestion(content)
    return svc


async def _approve(svc):
    return await svc.approve_suggestion(
        suggestion_id=SUGGESTION_ID,
        reviewed_by="user_admin",
        organization_id=ORG,
    )


# ---------------------------------------------------------------------------
# 1. No knowledge service → refuse, never a fake id
# ---------------------------------------------------------------------------


class TestApprovalWithoutAKnowledgeService:
    """The branch 100% of production traffic took, and the test that was
    missing."""

    async def test_it_raises_instead_of_reporting_success(self):
        svc = _service(knowledge_service=None)

        with pytest.raises(RuntimeError, match="no knowledge service"):
            await _approve(svc)

    async def test_the_suggestion_is_not_marked_approved(self):
        """The old branch also linked the suggestion to the invented id, so the
        review inbox showed a completed approval pointing at nothing."""
        svc = _service(knowledge_service=None)

        with pytest.raises(RuntimeError):
            await _approve(svc)

        stored = svc._suggestions_store[SUGGESTION_ID]
        assert stored.status is SuggestionStatus.PENDING_REVIEW
        assert stored.knowledge_item_id is None
        assert stored.reviewed_by is None


# ---------------------------------------------------------------------------
# 2. The quality gate reaches the approval path
# ---------------------------------------------------------------------------


def _real_knowledge_service() -> KnowledgeService:
    """Real service, ChromaDB and the DB stubbed to fail LOUDLY if reached.

    ``_db_session_factory`` raises, so any test in this class that gets past
    the gate blows up rather than silently proving nothing — the gate has to be
    what stopped it.
    """
    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = MagicMock(
        side_effect=AssertionError("the gate let content through to the DB write")
    )
    return svc


class TestTheRunbookQualityGate:
    async def test_llm_shaped_content_is_refused(self, tmp_path, monkeypatch):
        """``## Problem / ## Root Cause / ## Solution / ## Prevention`` — the
        shape the extraction prompt asks for — has no frontmatter and none of
        the six required sections."""
        monkeypatch.chdir(tmp_path)
        svc = _service(
            _real_knowledge_service(),
            content=(
                "## Problem\nX\n\n## Root Cause\nY\n\n## Solution\n1. Z\n\n"
                "## Prevention\n- W\n"
            ),
        )

        with pytest.raises(RunbookQualityError) as excinfo:
            await _approve(svc)

        assert "No YAML frontmatter found" in excinfo.value.errors
        assert "Missing required section: Causes" in excinfo.value.errors

    async def test_the_refusal_publishes_nothing(self, tmp_path, monkeypatch):
        """The gate runs before the first side effect: no file, no rows. The
        stub session factory above proves the DB was never reached; this proves
        the on-disk write was not either."""
        monkeypatch.chdir(tmp_path)
        svc = _service(_real_knowledge_service())

        with pytest.raises(RunbookQualityError):
            await _approve(svc)

        assert not list(tmp_path.rglob("*.md"))
        assert not (tmp_path / "data").exists()

    async def test_a_valid_runbook_passes_the_gate(self, tmp_path, monkeypatch):
        """The complement, and the thing that makes the two pins above mean
        something: gate-passing content reaches the write (and here trips the
        deliberately-exploding session factory), so the refusals are the gate's
        doing rather than an accident of the fixture."""
        monkeypatch.chdir(tmp_path)
        svc = _service(_real_knowledge_service(), content=valid_runbook())

        with pytest.raises(AssertionError, match="let content through"):
            await _approve(svc)

    async def test_the_error_names_what_to_fix(self):
        """``validation_exception_handler`` renders ``str(exc)``, so the flat
        message is what a reviewer actually sees."""
        error = RunbookQualityError(errors=["Missing required section: Sources"])

        assert "Missing required section: Sources" in str(error)
        assert error.details["errors"] == ["Missing required section: Sources"]


# ---------------------------------------------------------------------------
# 3. Compensation for a failure after the publish
# ---------------------------------------------------------------------------


def _publishing_double():
    """Autospecced, so the publish call must bind against the real signature
    (``spec=<function>`` does not enforce that — see the #1200 set)."""
    double = create_autospec(KnowledgeService, instance=True)
    double.upload_document.return_value = {"document_id": ITEM_ID}
    # The compensation entry point is rollback_uploaded_document, NOT
    # delete_document: deleting only the knowledge item leaves the draft/job/
    # upload rows and the on-disk file behind (measured), and the stale
    # verified draft row makes that residue permanent.
    double.rollback_uploaded_document.return_value = {
        "document_id": ITEM_ID,
        "residue": [],
    }
    return double


class TestTheCompensatingDelete:
    async def test_a_post_publish_failure_deletes_the_published_item(self):
        knowledge = _publishing_double()
        svc = _service(knowledge)
        svc._suggestions_store[SUGGESTION_ID].approve = MagicMock(
            side_effect=RuntimeError("concurrent edit reset the scan")
        )

        with pytest.raises(RuntimeError, match="concurrent edit"):
            await _approve(svc)

        knowledge.rollback_uploaded_document.assert_awaited_once_with(ITEM_ID)
        # Not the item-only delete — that is the partial rollback this replaces.
        knowledge.delete_document.assert_not_awaited()

    async def test_the_original_failure_is_what_the_caller_sees(self):
        """A rollback that raised would replace a truthful failure with an
        unrelated one AND still leave the orphan."""
        knowledge = _publishing_double()
        knowledge.rollback_uploaded_document.side_effect = RuntimeError(
            "chromadb unreachable"
        )
        svc = _service(knowledge)
        svc._suggestions_store[SUGGESTION_ID].approve = MagicMock(
            side_effect=ValueError("the real problem")
        )

        with pytest.raises(ValueError, match="the real problem"):
            await _approve(svc)

    async def test_reported_residue_is_logged_verbatim(self, caplog):
        """The rollback returns what it could NOT remove. Silence there would
        hide a real orphan from the operator, and a fixed sentence would
        describe the wrong store — a failed row delete and a failed vector
        delete leave opposite residue."""
        knowledge = _publishing_double()
        knowledge.rollback_uploaded_document.return_value = {
            "document_id": ITEM_ID,
            "residue": [
                f"ChromaDB chunks for {ITEM_ID} — the inventory row was deleted"
            ],
        }
        svc = _service(knowledge)
        svc._suggestions_store[SUGGESTION_ID].approve = MagicMock(
            side_effect=RuntimeError("boom")
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError):
                await _approve(svc)

        assert any(
            ITEM_ID in record.getMessage()
            and "ChromaDB chunks" in record.getMessage()
            and "needs manual cleanup" in record.getMessage()
            for record in caplog.records
        )

    async def test_a_rollback_that_blows_up_still_reports_residue(self, caplog):
        """Both branches emit the SAME sentence, so log-based alerting matches
        one string rather than two near-identical ones."""
        knowledge = _publishing_double()
        knowledge.rollback_uploaded_document.side_effect = RuntimeError("db down")
        svc = _service(knowledge)
        svc._suggestions_store[SUGGESTION_ID].approve = MagicMock(
            side_effect=RuntimeError("boom")
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError):
                await _approve(svc)

        assert any(
            "needs manual cleanup" in record.getMessage()
            and ITEM_ID in record.getMessage()
            for record in caplog.records
        )

    async def test_a_successful_approval_deletes_nothing(self):
        """The complement: compensation must not fire on the happy path."""
        knowledge = _publishing_double()
        svc = _service(knowledge)

        result = await _approve(svc)

        assert result["knowledge_item_id"] == ITEM_ID
        assert result["status"] == "approved"
        knowledge.rollback_uploaded_document.assert_not_awaited()
        knowledge.delete_document.assert_not_awaited()


# ---------------------------------------------------------------------------
# The route dependency hands out the composition root's instance
# ---------------------------------------------------------------------------


class TestTheRouteDependency:
    async def test_it_returns_the_app_state_instance(self):
        from faultmaven.modules.knowledge.api.routes import get_suggestion_service

        request = MagicMock()
        wired = object()
        request.app.state.suggestion_service = wired

        assert await get_suggestion_service(request) is wired

    async def test_it_refuses_rather_than_fabricating_one(self):
        """The fallback it replaces (`return SuggestionService()`) is the whole
        defect: a service with an empty store and no collaborators, handed out
        as though it worked."""
        from fastapi import HTTPException

        from faultmaven.modules.knowledge.api.routes import get_suggestion_service

        request = MagicMock()
        request.app.state = MagicMock(spec=[])  # no suggestion_service attribute

        with pytest.raises(HTTPException) as excinfo:
            await get_suggestion_service(request)

        assert excinfo.value.status_code == 503

    async def test_it_never_builds_a_suggestion_service(self, monkeypatch):
        """Belt and braces on the same rule, from the other side: constructing
        one inside the dependency must be impossible, not merely absent from
        the current source."""
        import faultmaven.modules.knowledge.domain.services.suggestion_service as mod
        from faultmaven.modules.knowledge.api.routes import get_suggestion_service

        def _explode(*_args, **_kwargs):
            raise AssertionError("the dependency built its own SuggestionService")

        monkeypatch.setattr(mod, "SuggestionService", _explode)

        request = MagicMock()
        request.app.state = MagicMock(spec=[])
        with pytest.raises(Exception) as excinfo:
            await get_suggestion_service(request)
        assert not isinstance(excinfo.value, AssertionError)


# ---------------------------------------------------------------------------
# The container composes one, with the knowledge service in it
# ---------------------------------------------------------------------------


def test_the_factory_injects_the_knowledge_service():
    """``create_suggestion_service`` is what makes the no-knowledge-service
    refusal above unreachable in a composed app."""
    from faultmaven.container.providers.services import create_suggestion_service

    knowledge = MagicMock()
    sanitizer = MagicMock()
    case_repository = MagicMock()
    llm = MagicMock()

    svc = create_suggestion_service(
        case_repository=case_repository,
        knowledge_service=knowledge,
        sanitizer=sanitizer,
        llm_provider=llm,
    )

    assert isinstance(svc, SuggestionService)
    assert svc._knowledge_service is knowledge
    assert svc._sanitizer is sanitizer
    assert svc._case_repository is case_repository
    assert svc._llm_provider is llm


async def test_the_service_still_scans_for_pii_when_wired():
    """The sanitizer the factory threads through is actually used — a wire that
    reaches the constructor and nothing else would look identical here without
    this.

    TITLE AND CONTENT ARE SCANNED SEPARATELY (#1226 rework), so this is two
    awaits, not one. It used to scan the concatenation ``title + content`` and
    assign the whole sanitized buffer back to ``suggested_content``, which put
    the title in front of the frontmatter and made every redacted draft fail
    the runbook gate on ``No YAML frontmatter found``. Asserting the two calls
    and their arguments is what pins that apart."""
    sanitizer = MagicMock()
    sanitizer.asanitize = AsyncMock(side_effect=lambda text: f"REDACTED::{text[:6]}")
    svc = SuggestionService(knowledge_service=MagicMock(), sanitizer=sanitizer)
    suggestion = _ready_suggestion()
    original_title = suggestion.suggested_title
    original_content = suggestion.suggested_content

    await svc._scan_for_pii(suggestion)

    assert sanitizer.asanitize.await_count == 2
    scanned = [c.args[0] for c in sanitizer.asanitize.await_args_list]
    assert scanned == [original_title, original_content]
    # Never the concatenation, in either direction.
    assert not any(
        "\n\n" in text and text not in (original_content,) for text in scanned
    )
    assert suggestion.pii_scan_status is PIIScanStatus.PII_DETECTED
    # Each redacted value lands in its OWN field.
    assert suggestion.suggested_title == f"REDACTED::{original_title[:6]}"
    assert suggestion.suggested_content == f"REDACTED::{original_content[:6]}"
