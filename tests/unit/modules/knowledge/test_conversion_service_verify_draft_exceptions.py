"""Typed-exception contract tests for ConversionService.verify_draft.

Item 3 in the 2026-05-20 investigation-pipeline-followups handoff,
knowledge-module slice. Before the refactor, verify_draft raised raw
``ValueError`` for six different error shapes (not-found, conflict,
validation). The route caught ``ValueError`` and returned HTTP 400
indiscriminately. After the refactor each shape maps to a typed
exception:

  - Conversion job / draft not found       → ``NotFoundError`` (404)
  - Already verified / discarded / unexpected state → ``ConflictError`` (409)
  - Draft validation errors                → ``ValidationException`` (422)

These tests pin the type and the carried metadata (``resource_type``,
``resource_id``, ``conflict_reason``) so a regression to ValueError or
a drop of the structured fields fails loudly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.modules.knowledge.domain.models.conversion import DraftStatus
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    ConversionService,
)

# ---------------------------------------------------------------------------
# Test scaffolding: mock the SQLAlchemy session_factory + execute results
# ---------------------------------------------------------------------------


def _make_session_factory(job=None, draft=None):
    """Build an async session_factory whose ``execute(stmt).scalar_one_or_none()``
    returns ``job`` on the first call and ``draft`` on the second.

    verify_draft runs two SELECTs in sequence: ConversionJobModel, then
    ConversionDraftModel. Returning the two stubbed rows in order
    simulates the desired DB state (job present + draft missing, etc.).
    """
    execute_calls = {"n": 0}

    async def _execute(_stmt):
        execute_calls["n"] += 1
        result = MagicMock()
        result.scalar_one_or_none.return_value = (
            job if execute_calls["n"] == 1 else draft
        )
        return result

    session = AsyncMock()
    session.execute = _execute

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return session

        async def __aexit__(self, *_):
            return None

    return _Factory()


def _make_service(job=None, draft=None) -> ConversionService:
    return ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=_make_session_factory(job=job, draft=draft),
        knowledge_service=None,
    )


def _job(*, user_id: str = "user_x"):
    """A truthy stub for ConversionJobModel."""
    j = MagicMock()
    j.user_id = user_id
    return j


def _draft(*, status: str, validation_passed: bool = True):
    """A truthy stub for ConversionDraftModel with the requested
    status. ``status`` is the raw ``DraftStatus.value`` string the
    service compares against."""
    d = MagicMock()
    d.status = status
    d.validation_passed = validation_passed
    return d


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestVerifyDraftTypedExceptions:
    """Pins each failure shape's exception type and metadata."""

    async def test_missing_job_raises_not_found(self):
        service = _make_service(job=None, draft=None)
        with pytest.raises(NotFoundError) as exc:
            await service.verify_draft(
                conversion_id="conv_missing",
                draft_id="d_x",
                user_id="user_x",
                username="alice",
            )
        assert exc.value.resource_type == "conversion_job"
        assert exc.value.resource_id == "conv_missing"

    async def test_missing_draft_raises_not_found(self):
        service = _make_service(job=_job(), draft=None)
        with pytest.raises(NotFoundError) as exc:
            await service.verify_draft(
                conversion_id="conv_x",
                draft_id="d_missing",
                user_id="user_x",
                username="alice",
            )
        assert exc.value.resource_type == "draft"
        assert exc.value.resource_id == "d_missing"

    async def test_already_verified_raises_conflict(self):
        service = _make_service(
            job=_job(),
            draft=_draft(status=DraftStatus.VERIFIED.value),
        )
        with pytest.raises(ConflictError) as exc:
            await service.verify_draft(
                conversion_id="conv_x",
                draft_id="d_verified",
                user_id="user_x",
                username="alice",
            )
        assert exc.value.conflict_reason == "already_verified"
        assert exc.value.resource_type == "draft"

    async def test_discarded_raises_conflict(self):
        service = _make_service(
            job=_job(),
            draft=_draft(status=DraftStatus.DISCARDED.value),
        )
        with pytest.raises(ConflictError) as exc:
            await service.verify_draft(
                conversion_id="conv_x",
                draft_id="d_discarded",
                user_id="user_x",
                username="alice",
            )
        assert exc.value.conflict_reason == "discarded"

    async def test_unexpected_state_raises_conflict(self):
        # A status string the service didn't expect (anything that isn't
        # VERIFIED / DISCARDED / DRAFT).
        service = _make_service(
            job=_job(),
            draft=_draft(status="halfway-converted"),
        )
        with pytest.raises(ConflictError) as exc:
            await service.verify_draft(
                conversion_id="conv_x",
                draft_id="d_weird",
                user_id="user_x",
                username="alice",
            )
        assert exc.value.conflict_reason == "unexpected_state"
        assert "halfway-converted" in str(exc.value)

    async def test_validation_failed_raises_validation_exception(self):
        service = _make_service(
            job=_job(),
            draft=_draft(status=DraftStatus.DRAFT.value, validation_passed=False),
        )
        with pytest.raises(ValidationException) as exc:
            await service.verify_draft(
                conversion_id="conv_x",
                draft_id="d_invalid",
                user_id="user_x",
                username="alice",
            )
        assert "validation errors" in str(exc.value)
