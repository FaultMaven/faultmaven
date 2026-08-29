"""Round-2 hardening for the suggestion service (#1214 review).

Three things the singleton made newly important, none of which had coverage:

1. **The store is unbounded.** Making the service a singleton gave its in-memory
   dict the lifetime of the process, and nothing ever removed an entry —
   approved, rejected and abandoned suggestions accumulated, each holding a full
   LLM-generated article. The store is capped, and the cap must never cost a
   reviewer work they have not seen.

Two things about that changed in #1227, and the tests below moved with them.

**The cap counts UNREVIEWED work and nothing is ever deleted.** The #1214 cap
bounded a process-local dict and made room by EVICTING approved and rejected
entries, on the reasoning that a decided suggestion "loses only history". Over a
durable table that reasoning fails: ``knowledge_items`` carries no back-pointer,
so an approved suggestion's ``knowledge_item_id`` is the ONLY link from a case to
the runbook it produced, and deleting the row destroys the provenance the
flywheel exists to accumulate. A process-memory bound would have become
permanent destruction, so the bound moved onto the queue that actually needs
bounding. The tests that asserted eviction now assert its opposite.

**The cap is scoped per organization**, because the durable store is one table
shared by every tenant and a deployment-wide count would let one tenant's
undrained inbox refuse another tenant's extraction.

These tests drive it through ``InMemorySuggestionRepository``, which — like the
database one — hands back a DETACHED COPY on every read: a suggestion mutated in
the service is not stored until it is saved, so an assertion about state has to
re-read the store rather than inspect the object it was handed.

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
from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
    InMemorySuggestionRepository,
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
        knowledge_service=MagicMock(),
        max_unreviewed_suggestions=capacity,
        suggestion_repository=InMemorySuggestionRepository(),
    )


async def _seed(svc: SuggestionService, suggestion: KnowledgeSuggestion) -> None:
    """Put a suggestion in the store the way the service would."""
    await svc._repository.save(suggestion)


async def _stored(svc: SuggestionService, suggestion_id: str):
    """Read one back OUT of the store — never off a returned object."""
    return await svc._repository.get(suggestion_id)


async def _stored_ids(svc: SuggestionService) -> set:
    page, _ = await svc._repository.list_for_organization(ORG, limit=1000)
    return {s.suggestion_id for s in page}


async def _stored_count(svc: SuggestionService) -> int:
    return await svc._repository.count_for_organization(ORG)


async def _extract(svc: SuggestionService, case_id: str = "case_aabb11223344"):
    return await svc.extract_knowledge_from_case(
        case_id=case_id, organization_id=ORG, extracted_by="user_extractor"
    )


# ---------------------------------------------------------------------------
# 1. The store is bounded — by refusing, never by deleting
# ---------------------------------------------------------------------------


class TestNothingIsEverDeleted:
    """The eviction policy #1214 shipped is gone, deliberately (#1227).

    Each test here is the inverse of one that used to assert a deletion. They
    are worth keeping in that shape: the old behaviour was reasonable for a
    dict and is destructive for a table, so the pins should say out loud that
    the deletion no longer happens.
    """

    async def test_a_decided_suggestion_is_kept_when_a_new_one_arrives(self):
        """Previously: the oldest APPROVED entry was evicted to make room."""
        svc = _service(capacity=3)
        for i in range(3):
            await _seed(
                svc,
                _suggestion(f"old_{i}", SuggestionStatus.APPROVED, age_seconds=100 - i),
            )

        created = await _extract(svc)

        assert await _stored_count(svc) == 4, "a decided suggestion was deleted"
        for i in range(3):
            assert await _stored(svc, f"old_{i}") is not None
        assert await _stored(svc, created.suggestion_id) is not None

    async def test_the_oldest_decision_is_not_evicted(self):
        svc = _service(capacity=3)
        await _seed(
            svc, _suggestion("oldest", SuggestionStatus.APPROVED, age_seconds=900)
        )
        await _seed(
            svc, _suggestion("newer", SuggestionStatus.REJECTED, age_seconds=10)
        )
        await _seed(
            svc, _suggestion("newest", SuggestionStatus.APPROVED, age_seconds=1)
        )

        await _extract(svc)

        assert await _stored(svc, "oldest") is not None
        assert await _stored(svc, "newer") is not None
        assert await _stored(svc, "newest") is not None

    async def test_an_approved_suggestion_keeps_its_knowledge_item_link(self):
        """The reason nothing is deleted, stated as an assertion.

        ``knowledge_items`` has no column pointing back at the suggestion, so
        this row IS the case → runbook provenance. Evicting it severed that link
        with no way to rebuild it.
        """
        svc = _service(capacity=1)
        approved = _suggestion("approved", SuggestionStatus.APPROVED, age_seconds=900)
        approved.knowledge_item_id = "kb_abcdef0123456789"
        await _seed(svc, approved)

        await _extract(svc)

        kept = await _stored(svc, "approved")
        assert kept is not None
        assert kept.knowledge_item_id == "kb_abcdef0123456789"

    async def test_a_rejected_suggestion_is_kept_too(self):
        svc = _service(capacity=1)
        await _seed(
            svc, _suggestion("rejected", SuggestionStatus.REJECTED, age_seconds=50)
        )

        await _extract(svc)

        assert await _stored(svc, "rejected") is not None


class TestTheUnreviewedQueueIsWhatIsCapped:
    """Only PENDING_REVIEW and DRAFT count toward the ceiling."""

    async def test_decided_entries_do_not_consume_the_quota(self):
        """A capacity of 1 with three decided rows still admits an extract:
        the queue of unreviewed work is empty."""
        svc = _service(capacity=1)
        for i in range(3):
            await _seed(svc, _suggestion(f"done_{i}", SuggestionStatus.APPROVED))

        suggestion = await _extract(svc)

        assert await _stored(svc, suggestion.suggestion_id) is not None

    async def test_a_full_queue_of_pending_reviews_refuses_the_extract(self):
        svc = _service(capacity=2)
        for i in range(2):
            await _seed(svc, _suggestion(f"pending_{i}"))

        with pytest.raises(ServiceUnavailableException, match="at capacity"):
            await _extract(svc)

        assert await _stored_ids(svc) == {"pending_0", "pending_1"}

    async def test_a_draft_counts_as_unreviewed(self):
        svc = _service(capacity=1)
        await _seed(svc, _suggestion("draft", SuggestionStatus.DRAFT))

        with pytest.raises(ServiceUnavailableException):
            await _extract(svc)

        assert await _stored(svc, "draft") is not None

    async def test_reviewing_one_entry_frees_the_slot(self):
        """The queue drains by being reviewed, which is the only remedy the
        503 offers — so it has to actually work."""
        svc = _service(capacity=1)
        await _seed(svc, _suggestion("pending"))

        with pytest.raises(ServiceUnavailableException):
            await _extract(svc)

        assert (
            await svc.reject_suggestion(
                suggestion_id="pending",
                reviewed_by="user_admin",
                rejection_reason="not reusable",
                organization_id=ORG,
            )
            is True
        )

        suggestion = await _extract(svc)
        assert await _stored(svc, suggestion.suggestion_id) is not None
        assert await _stored(svc, "pending") is not None

    async def test_the_refusal_is_logged_with_the_counts(self, caplog):
        svc = _service(capacity=1)
        await _seed(svc, _suggestion("pending"))

        with caplog.at_level("ERROR"):
            with pytest.raises(ServiceUnavailableException):
                await _extract(svc)

        messages = [r.getMessage() for r in caplog.records]
        assert any("Review queue is full" in m and "1/1" in m for m in messages)
        # And it says that nothing was evicted, so an operator reading the log
        # does not go looking for the rows the old policy would have removed.
        assert any("Nothing is evicted" in m for m in messages)


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
            knowledge_service=_knowledge_double(),
            sanitizer=sanitizer,
            suggestion_repository=InMemorySuggestionRepository(),
        )

        suggestion = await _extract(svc)

        assert suggestion.pii_scan_status is PIIScanStatus.SCAN_FAILED
        # ...and that is what was STORED, not only what was returned.
        stored = await _stored(svc, suggestion.suggestion_id)
        assert stored.pii_scan_status is PIIScanStatus.SCAN_FAILED

    async def test_approval_rescans_and_proceeds_once_the_engine_recovers(self):
        sanitizer = _FlakySanitizer(failures=1)
        knowledge = _knowledge_double()
        svc = SuggestionService(
            knowledge_service=knowledge,
            sanitizer=sanitizer,
            suggestion_repository=InMemorySuggestionRepository(),
        )
        suggestion = await _extract(svc)
        assert suggestion.pii_scan_status is PIIScanStatus.SCAN_FAILED

        result = await svc.approve_suggestion(
            suggestion_id=suggestion.suggestion_id,
            reviewed_by="user-admin",
            organization_id=ORG,
        )

        assert result is not None, "a recovered scan should let approval proceed"
        assert result["status"] == "approved"
        # Re-read: the recovered scan has to be PERSISTED, not just applied to
        # the copy the approval was working on. Without the save, the next read
        # still says SCAN_FAILED and the re-scan is spent again on every visit.
        stored = await _stored(svc, suggestion.suggestion_id)
        assert stored.pii_scan_status is PIIScanStatus.CLEAN
        assert stored.status is SuggestionStatus.APPROVED
        # Three sanitize calls, not two: a scan is now TWO calls (title, then
        # content — #1226 rework), and the first scan aborts on the title call
        # that raises, so it never reaches the content. 1 failed + 2 recovered.
        assert sanitizer.calls == 3, "the scan must actually be re-run"

    async def test_a_still_broken_engine_stays_not_ready(self):
        """The retry is honest: if the engine is still down the status stays
        SCAN_FAILED and the route's 400 is truthful again."""
        sanitizer = _FlakySanitizer(failures=5)
        knowledge = _knowledge_double()
        svc = SuggestionService(
            knowledge_service=knowledge,
            sanitizer=sanitizer,
            suggestion_repository=InMemorySuggestionRepository(),
        )
        suggestion = await _extract(svc)

        result = await svc.approve_suggestion(
            suggestion_id=suggestion.suggestion_id,
            reviewed_by="user-admin",
            organization_id=ORG,
        )

        assert result is None
        stored = await _stored(svc, suggestion.suggestion_id)
        assert stored.pii_scan_status is PIIScanStatus.SCAN_FAILED
        knowledge.upload_document.assert_not_awaited()

    async def test_detected_pii_is_not_rescanned_away(self):
        """Only SCAN_FAILED is retried. PII_DETECTED is a verdict about the
        content and needs a human, so re-scanning it would be a way around the
        HITL gate."""
        sanitizer = MagicMock()
        sanitizer.asanitize = AsyncMock(return_value="REDACTED")
        knowledge = _knowledge_double()
        svc = SuggestionService(
            knowledge_service=knowledge,
            sanitizer=sanitizer,
            suggestion_repository=InMemorySuggestionRepository(),
        )
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
