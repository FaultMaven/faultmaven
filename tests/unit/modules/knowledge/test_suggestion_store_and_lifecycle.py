"""Round-2 hardening for the suggestion service (#1214 review).

Three things the singleton made newly important, none of which had coverage:

1. **The store is unbounded.** Making the service a singleton gave its in-memory
   dict the lifetime of the process, and nothing ever removed an entry —
   approved, rejected and abandoned suggestions accumulated, each holding a full
   LLM-generated article. The durable replacement is #1227; until then the store
   is capped and evicts, and it must never evict work a reviewer has not seen.
2. **SCAN_FAILED became reachable.** Every pre-#1214 service was built with
   ``sanitizer=None``, so every scan was marked CLEAN and a failed scan could not
   occur in production. Wiring the real sanitizer made it reachable — and it was
   a dead end: approve 400'd, ``mark_pii_remediated`` 409'd (only PII_DETECTED is
   remediable), and the only re-arm was an undocumented content edit.
3. **The state machine had no guards on the terminal states.** An APPROVED
   suggestion could be rejected, and could be edited into a not-ready state
   while still reporting ``approved`` and still linked to a published item.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.exceptions import ConflictError, ServiceUnavailableException
from faultmaven.modules.knowledge.domain.models.suggestion import (
    KnowledgeSuggestion,
    PIIScanStatus,
    SuggestionStatus,
)
from faultmaven.modules.knowledge.domain.services.suggestion_service import (
    SuggestionService,
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

ORG = "org_123"


def _suggestion(
    suggestion_id: str,
    status: SuggestionStatus = SuggestionStatus.PENDING_REVIEW,
    *,
    age_seconds: int = 0,
) -> KnowledgeSuggestion:
    s = KnowledgeSuggestion(
        suggestion_id=suggestion_id,
        organization_id=ORG,
        case_id="case_aabb11223344",
        status=status,
        suggested_title="Redis pool exhaustion",
        suggested_content="## Problem\nPool exhausted.\n",
        extracted_by="user_extractor",
        pii_scan_status=PIIScanStatus.CLEAN,
    )
    # updated_at is when the decision was taken — the eviction ordering key.
    s.updated_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return s


def _service(capacity: int = 3) -> SuggestionService:
    return SuggestionService(
        knowledge_service=MagicMock(), max_stored_suggestions=capacity
    )


async def _extract(svc: SuggestionService, case_id: str = "case_aabb11223344"):
    return await svc.extract_knowledge_from_case(
        case_id=case_id, organization_id=ORG, extracted_by="user_extractor"
    )


# ---------------------------------------------------------------------------
# 1. The store is bounded
# ---------------------------------------------------------------------------


class TestTheStoreIsBounded:
    async def test_it_stops_growing_at_the_cap(self):
        svc = _service(capacity=3)
        for i in range(3):
            svc._suggestions_store[f"old_{i}"] = _suggestion(
                f"old_{i}", SuggestionStatus.APPROVED, age_seconds=100 - i
            )

        await _extract(svc)

        assert len(svc._suggestions_store) == 3

    async def test_the_oldest_decision_is_evicted_first(self):
        svc = _service(capacity=3)
        svc._suggestions_store["oldest"] = _suggestion(
            "oldest", SuggestionStatus.APPROVED, age_seconds=900
        )
        svc._suggestions_store["newer"] = _suggestion(
            "newer", SuggestionStatus.REJECTED, age_seconds=10
        )
        svc._suggestions_store["newest"] = _suggestion(
            "newest", SuggestionStatus.APPROVED, age_seconds=1
        )

        await _extract(svc)

        assert "oldest" not in svc._suggestions_store
        assert "newer" in svc._suggestions_store
        assert "newest" in svc._suggestions_store

    async def test_rejected_suggestions_are_evictable_too(self):
        """Both terminal states are in the pool — a rejection has nothing left
        to publish, so dropping it from an in-memory inbox loses only history."""
        svc = _service(capacity=1)
        svc._suggestions_store["rejected"] = _suggestion(
            "rejected", SuggestionStatus.REJECTED, age_seconds=50
        )

        await _extract(svc)

        assert "rejected" not in svc._suggestions_store

    async def test_eviction_is_logged(self, caplog):
        svc = _service(capacity=1)
        svc._suggestions_store["done"] = _suggestion(
            "done", SuggestionStatus.APPROVED, age_seconds=50
        )

        with caplog.at_level("WARNING"):
            await _extract(svc)

        assert any(
            "cap" in r.getMessage() and "done" in r.getMessage() for r in caplog.records
        )


class TestPendingWorkIsNeverEvicted:
    """The one thing in this store that exists nowhere else."""

    async def test_a_full_queue_of_pending_reviews_refuses_the_extract(self):
        svc = _service(capacity=2)
        for i in range(2):
            svc._suggestions_store[f"pending_{i}"] = _suggestion(f"pending_{i}")

        with pytest.raises(ServiceUnavailableException, match="at capacity"):
            await _extract(svc)

        assert set(svc._suggestions_store) == {"pending_0", "pending_1"}

    async def test_a_draft_is_protected_like_a_pending_review(self):
        svc = _service(capacity=1)
        svc._suggestions_store["draft"] = _suggestion("draft", SuggestionStatus.DRAFT)

        with pytest.raises(ServiceUnavailableException):
            await _extract(svc)

        assert "draft" in svc._suggestions_store

    async def test_one_terminal_entry_is_enough_to_make_room(self):
        """The refusal is about having nothing evictable, not about being full."""
        svc = _service(capacity=2)
        svc._suggestions_store["pending"] = _suggestion("pending")
        svc._suggestions_store["done"] = _suggestion(
            "done", SuggestionStatus.APPROVED, age_seconds=50
        )

        suggestion = await _extract(svc)

        assert "pending" in svc._suggestions_store
        assert "done" not in svc._suggestions_store
        assert suggestion.suggestion_id in svc._suggestions_store

    async def test_the_refusal_is_logged_with_the_counts(self, caplog):
        svc = _service(capacity=1)
        svc._suggestions_store["pending"] = _suggestion("pending")

        with caplog.at_level("ERROR"):
            with pytest.raises(ServiceUnavailableException):
                await _extract(svc)

        assert any("awaiting review" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 2. SCAN_FAILED is no longer a dead end
# ---------------------------------------------------------------------------


class _FlakySanitizer:
    """Fails the first N scans, then succeeds — a PII engine coming back up."""

    def __init__(self, failures: int):
        self.remaining_failures = failures
        self.calls = 0

    async def asanitize(self, content: str) -> str:
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise RuntimeError("presidio unavailable")
        return content  # unchanged => CLEAN


def _knowledge_double():
    ks = MagicMock()
    ks.upload_document = AsyncMock(return_value={"document_id": "kb_abcdef0123456789"})
    return ks


class TestAFailedScanRecovers:
    async def test_the_first_scan_failing_leaves_scan_failed(self):
        sanitizer = _FlakySanitizer(failures=1)
        svc = SuggestionService(
            knowledge_service=_knowledge_double(), sanitizer=sanitizer
        )

        suggestion = await _extract(svc)

        assert suggestion.pii_scan_status is PIIScanStatus.SCAN_FAILED

    async def test_approval_rescans_and_proceeds_once_the_engine_recovers(self):
        sanitizer = _FlakySanitizer(failures=1)
        knowledge = _knowledge_double()
        svc = SuggestionService(knowledge_service=knowledge, sanitizer=sanitizer)
        suggestion = await _extract(svc)
        assert suggestion.pii_scan_status is PIIScanStatus.SCAN_FAILED

        result = await svc.approve_suggestion(
            suggestion_id=suggestion.suggestion_id,
            reviewed_by="user-admin",
            organization_id=ORG,
        )

        assert result is not None, "a recovered scan should let approval proceed"
        assert result["status"] == "approved"
        assert suggestion.pii_scan_status is PIIScanStatus.CLEAN
        # Three sanitize calls, not two: a scan is now TWO calls (title, then
        # content — #1226 rework), and the first scan aborts on the title call
        # that raises, so it never reaches the content. 1 failed + 2 recovered.
        assert sanitizer.calls == 3, "the scan must actually be re-run"

    async def test_a_still_broken_engine_stays_not_ready(self):
        """The retry is honest: if the engine is still down the status stays
        SCAN_FAILED and the route's 400 is truthful again."""
        sanitizer = _FlakySanitizer(failures=5)
        knowledge = _knowledge_double()
        svc = SuggestionService(knowledge_service=knowledge, sanitizer=sanitizer)
        suggestion = await _extract(svc)

        result = await svc.approve_suggestion(
            suggestion_id=suggestion.suggestion_id,
            reviewed_by="user-admin",
            organization_id=ORG,
        )

        assert result is None
        assert suggestion.pii_scan_status is PIIScanStatus.SCAN_FAILED
        knowledge.upload_document.assert_not_awaited()

    async def test_detected_pii_is_not_rescanned_away(self):
        """Only SCAN_FAILED is retried. PII_DETECTED is a verdict about the
        content and needs a human, so re-scanning it would be a way around the
        HITL gate."""
        sanitizer = MagicMock()
        sanitizer.asanitize = AsyncMock(return_value="REDACTED")
        knowledge = _knowledge_double()
        svc = SuggestionService(knowledge_service=knowledge, sanitizer=sanitizer)
        suggestion = await _extract(svc)
        assert suggestion.pii_scan_status is PIIScanStatus.PII_DETECTED
        scans_after_extraction = sanitizer.asanitize.await_count

        result = await svc.approve_suggestion(
            suggestion_id=suggestion.suggestion_id,
            reviewed_by="user-admin",
            organization_id=ORG,
        )

        assert result is None
        assert sanitizer.asanitize.await_count == scans_after_extraction
        knowledge.upload_document.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. Terminal-state guards
# ---------------------------------------------------------------------------


class TestAnApprovedSuggestionIsFinished:
    def test_it_cannot_be_rejected(self):
        """Rejecting after approval flipped the status while leaving
        ``knowledge_item_id`` set and the item live in the corpus — the inbox
        said "rejected" about content every tenant could still retrieve."""
        suggestion = _suggestion("sug_1")
        suggestion.approve(reviewed_by="admin", knowledge_item_id="kb_1")

        with pytest.raises(ConflictError) as excinfo:
            suggestion.reject(reviewed_by="admin", rejection_reason="changed my mind")

        assert excinfo.value.conflict_reason == "already_approved"
        assert suggestion.status is SuggestionStatus.APPROVED
        assert suggestion.knowledge_item_id == "kb_1"

    def test_it_cannot_be_edited(self):
        """``update_content`` resets the PII scan, which made an approved
        suggestion simultaneously approved and not-ready, still linked to a
        published item whose content no longer matched the inbox."""
        suggestion = _suggestion("sug_1")
        suggestion.approve(reviewed_by="admin", knowledge_item_id="kb_1")

        with pytest.raises(ConflictError) as excinfo:
            suggestion.update_content(title="new", content="new body")

        assert excinfo.value.conflict_reason == "already_approved"
        assert suggestion.pii_scan_status is PIIScanStatus.CLEAN
        assert suggestion.suggested_title == "Redis pool exhaustion"

    def test_a_pending_suggestion_is_still_rejectable_and_editable(self):
        """The complement — the guards must bite only on the terminal state, or
        they would break the review workflow they exist to protect."""
        suggestion = _suggestion("sug_1")

        suggestion.update_content(title="edited", content="edited body")
        assert suggestion.suggested_title == "edited"
        assert suggestion.pii_scan_status is PIIScanStatus.NOT_SCANNED

        suggestion.reject(reviewed_by="admin", rejection_reason="not reusable")
        assert suggestion.status is SuggestionStatus.REJECTED

    def test_a_rejected_suggestion_is_still_editable(self):
        """Only APPROVED is guarded: a rejection published nothing, so reworking
        it is legitimate."""
        suggestion = _suggestion("sug_1")
        suggestion.reject(reviewed_by="admin", rejection_reason="thin")

        suggestion.update_content(title="reworked", content="better body")

        assert suggestion.suggested_title == "reworked"
