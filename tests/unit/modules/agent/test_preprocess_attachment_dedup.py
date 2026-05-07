"""Unit tests for content-hash deduplication in `_preprocess_attachment`.

Covers the short-circuit path introduced by PLAN-content-hash-deduplication.md:
when an attachment's SHA-256 content hash already exists on the same case,
the service reuses the existing Evidence and does NOT call the file storage
adapter. The AttachmentResult in the TurnResponse carries `duplicate_of` +
`duplicate_turn` so the frontend can render a toast.

Run with:
    pytest tests/unit/modules/agent/test_preprocess_attachment_dedup.py -v
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import (
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
)

from .conftest import MockCaseRepository, MockMilestoneEngine, create_sample_case

# ============================================================
# Helpers
# ============================================================


def _make_preprocessing_result(content_hash: str = "abc123hash"):
    result = MagicMock()
    result.summary = "Log file summary"
    result.structural_index = "ERROR line 42: connection refused"
    result.data_type = MagicMock(value="logs")
    result.content_hash = content_hash
    result.extraction_method = "crime_scene"
    return result


def _make_existing_evidence(
    *, content_hash: str, evidence_id: str = "ev_abcdef123456", turn: int = 3
) -> Evidence:
    """Create existing Evidence linked to a backing UploadedFile by source_file_id.

    `content_hash` now lives on the UploadedFile (not Evidence), so the test
    helper sets `source_file_id` and the caller is expected to also append the
    matching UploadedFile to `case.uploaded_files`.
    """
    return Evidence(
        evidence_id=evidence_id,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="symptom_verified",
        summary="Previously uploaded evidence",
        extract="prior content",
        source_type=EvidenceSourceType.LOGS,
        form=EvidenceForm.DOCUMENT,
        collected_by="user_owner",
        collected_at_turn=turn,
        source_file_id=f"file_{content_hash[:12].ljust(12, '0')}"[:18],
    )


class _DedupCapableRepo(MockCaseRepository):
    """MockCaseRepository extended with configurable find_by_content_hash."""

    def __init__(self):
        super().__init__()
        # Default: no match (new-evidence path). Tests override.
        self.find_by_content_hash = AsyncMock(return_value=None)


def _make_service(
    case_repository,
    preprocessing_result,
    file_storage_service=None,
):
    mock_preprocessing = AsyncMock()
    mock_preprocessing.classify_and_extract = AsyncMock(
        return_value=preprocessing_result
    )
    return InvestigationService(
        milestone_engine=MockMilestoneEngine(),
        case_repository=case_repository,
        preprocessing_service=mock_preprocessing,
        file_storage_service=file_storage_service,
    )


def _make_payload(filename: str = "app.log", content: bytes = b"log data"):
    return TurnPayload(
        query="Analyze this",
        attachments=[
            Attachment(
                content=content,
                filename=filename,
                content_type="text/plain",
            )
        ],
        intent=QueryIntent(type=IntentType.CONVERSATION),
    )


# ============================================================
# Tests
# ============================================================


class TestPreprocessAttachmentDedup:
    """Dedup short-circuit behavior in `_preprocess_attachment`."""

    @pytest.mark.asyncio
    async def test_no_duplicate_creates_evidence_normally(self):
        """When find_by_content_hash returns None, new Evidence is created and
        file_storage_service.store_file is called."""
        repo = _DedupCapableRepo()
        repo.find_by_content_hash.return_value = None

        file_storage = AsyncMock()
        file_storage.store_file = AsyncMock(return_value={"file_path": "/stored/x"})

        service = _make_service(
            case_repository=repo,
            preprocessing_result=_make_preprocessing_result(content_hash="new_hash"),
            file_storage_service=file_storage,
        )

        case = create_sample_case()
        case.user_id = "user_owner"
        await repo.save(case)

        await service.process_turn(case.case_id, "user_owner", _make_payload())

        # New Evidence was appended to case; content_hash now lives on the
        # backing UploadedFile, reachable via source_file_id.
        saved = await repo.get(case.case_id)
        assert len(saved.evidence) == 1
        new_ev = saved.evidence[0]
        backing_file = saved.find_uploaded_file(new_ev.source_file_id)
        assert backing_file is not None
        assert backing_file.content_hash == "new_hash"
        # File was stored
        file_storage.store_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_duplicate_returns_existing_and_skips_storage(self):
        """When find_by_content_hash returns an Evidence, short-circuit:
        no new Evidence, no storage call, AttachmentResult carries duplicate_of."""
        repo = _DedupCapableRepo()
        existing = _make_existing_evidence(content_hash="dup_hash", turn=3)
        repo.find_by_content_hash.return_value = existing

        file_storage = AsyncMock()
        file_storage.store_file = AsyncMock()

        service = _make_service(
            case_repository=repo,
            preprocessing_result=_make_preprocessing_result(content_hash="dup_hash"),
            file_storage_service=file_storage,
        )

        case = create_sample_case()
        case.user_id = "user_owner"
        case.evidence.append(existing)  # existing evidence already loaded on case
        await repo.save(case)

        response = await service.process_turn(
            case.case_id, "user_owner", _make_payload()
        )

        # Storage was NOT called for the duplicate
        file_storage.store_file.assert_not_called()

        # case.evidence still has just the one existing row (no double-append)
        saved = await repo.get(case.case_id)
        assert len(saved.evidence) == 1
        assert saved.evidence[0].evidence_id == "ev_abcdef123456"

        # AttachmentResult carries duplicate metadata
        assert len(response.attachments_processed) == 1
        att_result = response.attachments_processed[0]
        assert att_result.evidence_id == "ev_abcdef123456"
        assert att_result.duplicate_of == "ev_abcdef123456"
        assert att_result.duplicate_turn == 3
        assert att_result.processing_status == "duplicate"

    @pytest.mark.asyncio
    async def test_repo_without_find_by_content_hash_falls_through(self):
        """Legacy repositories without find_by_content_hash must still work —
        the service catches AttributeError and falls through to new-evidence
        creation."""
        # Plain MockCaseRepository (no find_by_content_hash attribute)
        repo = MockCaseRepository()
        file_storage = AsyncMock()
        file_storage.store_file = AsyncMock(return_value={"file_path": "/stored/x"})

        service = _make_service(
            case_repository=repo,
            preprocessing_result=_make_preprocessing_result(content_hash="new_hash"),
            file_storage_service=file_storage,
        )

        case = create_sample_case()
        case.user_id = "user_owner"
        await repo.save(case)

        response = await service.process_turn(
            case.case_id, "user_owner", _make_payload()
        )

        # Falls through to normal path
        file_storage.store_file.assert_called_once()
        assert len(response.attachments_processed) == 1
        assert response.attachments_processed[0].duplicate_of is None
        assert response.attachments_processed[0].duplicate_turn is None

    @pytest.mark.asyncio
    async def test_empty_content_hash_skips_lookup(self):
        """If the preprocessing result has no content_hash (empty string),
        dedup lookup is skipped entirely — we never query for `""`."""
        repo = _DedupCapableRepo()
        file_storage = AsyncMock()
        file_storage.store_file = AsyncMock(return_value={"file_path": "/stored/x"})

        service = _make_service(
            case_repository=repo,
            preprocessing_result=_make_preprocessing_result(content_hash=""),
            file_storage_service=file_storage,
        )

        case = create_sample_case()
        case.user_id = "user_owner"
        await repo.save(case)

        await service.process_turn(case.case_id, "user_owner", _make_payload())

        # find_by_content_hash should NOT have been called with an empty hash
        repo.find_by_content_hash.assert_not_called()
