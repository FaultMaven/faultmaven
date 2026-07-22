"""Batch-verify classification tests.

``ConversionService.verify_batch`` verifies a list of drafts sequentially and
records a per-item status. It must classify each item's outcome from the TYPED
exception ``verify_draft`` raises — never from the exception's message string.
The exception-contract migration replaced the old ``ValueError("... already
been verified ...")`` signal with a typed ``ConflictError`` carrying a
structured ``conflict_reason``; an already-verified draft must therefore be
recorded ``skipped`` (idempotent no-op), not ``failed`` (regression guard for
#784). These tests drive ``verify_batch`` with a stubbed ``verify_draft`` that
raises the real typed exceptions, so they pin classification to the types, not
to strings.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionService,
)


def _make_service() -> ConversionService:
    """A ConversionService whose only exercised method is ``verify_batch``.

    ``verify_draft`` is replaced per-test, so the collaborators can be inert.
    """
    return ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=MagicMock(),
        knowledge_service=None,
    )


def _verify_response(knowledge_item_id: str = "ki_123"):
    resp = MagicMock()
    resp.knowledge_item_id = knowledge_item_id
    return resp


@pytest.mark.unit
@pytest.mark.asyncio
class TestVerifyBatchTypedClassification:
    """Each per-item status is derived from the typed exception, not a string."""

    async def test_already_verified_conflict_is_skipped(self):
        """Regression for #784: a typed ConflictError(already_verified) →
        ``skipped``, never ``failed``."""
        service = _make_service()
        service.verify_draft = AsyncMock(
            side_effect=ConflictError(
                "This runbook has already been verified and ingested",
                resource_type="draft",
                resource_id="d1",
                conflict_reason="already_verified",
            )
        )

        summary = await service.verify_batch(
            draft_refs=[("conv1", "d1")],
            user_id="user_x",
            username="alice",
        )

        assert summary["skipped"] == 1
        assert summary["failed"] == 0
        assert summary["verified"] == 0
        assert summary["forbidden"] == 0
        assert summary["total"] == 1
        assert summary["results"][0]["status"] == "skipped"
        assert summary["results"][0]["knowledge_item_id"] is None

    async def test_other_conflict_reason_is_failed(self):
        """A ConflictError that is NOT already-verified (e.g. discarded /
        unexpected state) genuinely cannot be verified → ``failed``."""
        service = _make_service()
        service.verify_draft = AsyncMock(
            side_effect=ConflictError(
                "This draft has been discarded",
                resource_type="draft",
                resource_id="d1",
                conflict_reason="discarded",
            )
        )

        summary = await service.verify_batch(
            draft_refs=[("conv1", "d1")],
            user_id="user_x",
            username="alice",
        )

        assert summary["failed"] == 1
        assert summary["skipped"] == 0
        assert summary["results"][0]["status"] == "failed"

    async def test_successful_verify_is_verified(self):
        service = _make_service()
        service.verify_draft = AsyncMock(return_value=_verify_response("ki_abc"))

        summary = await service.verify_batch(
            draft_refs=[("conv1", "d1")],
            user_id="user_x",
            username="alice",
        )

        assert summary["verified"] == 1
        assert summary["failed"] == 0
        assert summary["results"][0]["status"] == "verified"
        assert summary["results"][0]["knowledge_item_id"] == "ki_abc"

    async def test_authorization_denial_is_forbidden(self):
        """Global-tier authoring denial (R4/#770) → ``forbidden``, kept
        distinct from ``failed`` and never treated as retryable."""
        service = _make_service()
        service.verify_draft = AsyncMock(
            side_effect=AuthorizationError(
                "Global-scope knowledge requires the platform admin role."
            )
        )

        summary = await service.verify_batch(
            draft_refs=[("conv1", "d1")],
            user_id="user_x",
            username="alice",
        )

        assert summary["forbidden"] == 1
        assert summary["failed"] == 0
        assert summary["results"][0]["status"] == "forbidden"

    async def test_genuinely_broken_draft_is_failed(self):
        """A non-conflict, non-auth failure (here a NotFoundError, but any
        unexpected error) falls to the genuine catch-all → ``failed``."""
        service = _make_service()
        service.verify_draft = AsyncMock(
            side_effect=NotFoundError(
                resource_type="draft", resource_id="d1", message="Draft not found"
            )
        )

        summary = await service.verify_batch(
            draft_refs=[("conv1", "d1")],
            user_id="user_x",
            username="alice",
        )

        assert summary["failed"] == 1
        assert summary["results"][0]["status"] == "failed"

    async def test_runtime_error_is_failed(self):
        """A bare RuntimeError (e.g. ingestion contract violation) → ``failed``
        via the catch-all."""
        service = _make_service()
        service.verify_draft = AsyncMock(
            side_effect=RuntimeError("Vector indexing produced 0 chunks")
        )

        summary = await service.verify_batch(
            draft_refs=[("conv1", "d1")],
            user_id="user_x",
            username="alice",
        )

        assert summary["failed"] == 1
        assert summary["results"][0]["status"] == "failed"

    async def test_mixed_batch_counters_and_results_consistent(self):
        """A heterogeneous batch: counters sum to total and match the
        per-item results exactly (one of each classification)."""
        service = _make_service()

        outcomes = {
            ("conv", "verified"): _verify_response("ki_ok"),
            ("conv", "skipped"): ConflictError(
                "already verified",
                resource_type="draft",
                resource_id="skipped",
                conflict_reason="already_verified",
            ),
            ("conv", "forbidden"): AuthorizationError("nope"),
            ("conv", "failed_conflict"): ConflictError(
                "discarded",
                resource_type="draft",
                resource_id="failed_conflict",
                conflict_reason="discarded",
            ),
            ("conv", "failed_other"): RuntimeError("boom"),
        }

        async def _fake_verify(*, conversion_id, draft_id, user_id, username, **_):
            outcome = outcomes[(conversion_id, draft_id)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        service.verify_draft = _fake_verify

        refs = [("conv", d) for _, d in outcomes]
        summary = await service.verify_batch(
            draft_refs=refs, user_id="user_x", username="alice"
        )

        assert summary["total"] == 5
        assert summary["verified"] == 1
        assert summary["skipped"] == 1
        assert summary["forbidden"] == 1
        assert summary["failed"] == 2

        # Counters are consistent with the per-item results.
        status_counts: dict[str, int] = {}
        for item in summary["results"]:
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
        assert status_counts == {
            "verified": 1,
            "skipped": 1,
            "forbidden": 1,
            "failed": 2,
        }
        # Every counter equals its result-derived count, and they sum to total.
        assert summary["verified"] == status_counts["verified"]
        assert summary["skipped"] == status_counts["skipped"]
        assert summary["forbidden"] == status_counts["forbidden"]
        assert summary["failed"] == status_counts["failed"]
        assert (
            summary["verified"]
            + summary["skipped"]
            + summary["forbidden"]
            + summary["failed"]
            == summary["total"]
        )
