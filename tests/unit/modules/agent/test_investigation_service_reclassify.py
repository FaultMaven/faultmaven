"""Phase 1.5 — InvestigationService.reclassify_evidence

Covers the service-layer flow end-to-end: auth check, evidence lookup,
content-ref check, storage fetch, preprocessing re-run, and persistence.
The service uses real objects (Case, Evidence) wired through
MockCaseRepository so we exercise model_copy semantics.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from faultmaven.core.preprocessing.models import PreprocessingResult, UnifiedDataType
from faultmaven.exceptions import (
    NotFoundError,
    PermissionDeniedException,
    ValidationException,
)
from faultmaven.models.api import DataType
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


def _evidence(
    evidence_id: str = "ev_aaaaaaaaaaaa",
    content_ref: str | None = "evidence/case_x/server.log",
    data_type: str = "metrics",
    metadata: dict | None = None,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        category=EvidenceCategory.CONTEXTUAL_EVIDENCE,
        primary_purpose="Test",
        summary="Old summary",
        preprocessed_content="old index",
        content_size_bytes=100,
        data_type=data_type,
        source_type=EvidenceSourceType.METRICS,
        form=EvidenceForm.DOCUMENT,
        preprocessing_method="crime_scene",
        extraction_method="crime_scene",
        content_ref=content_ref,
        collected_by="user",
        collected_at=datetime.now(UTC),
        collected_at_turn=0,
        metadata=metadata,
    )


def _preprocessing_result_for(
    new_data_type: DataType = DataType.LOGS_AND_ERRORS,
    metadata: dict | None = None,
) -> PreprocessingResult:
    unified_map = {
        DataType.LOGS_AND_ERRORS: UnifiedDataType.LOGS,
        DataType.METRICS_AND_PERFORMANCE: UnifiedDataType.METRICS,
        DataType.STRUCTURED_CONFIG: UnifiedDataType.CONFIGURATION,
    }
    return PreprocessingResult(
        data_type=unified_map.get(new_data_type, UnifiedDataType.LOGS),
        detailed_data_type=new_data_type,
        summary="new summary",
        structural_index="new index content",
        content_ref=None,
        content_size_bytes=100,
        content_type="text/plain",
        extraction_method="crime_scene",
        compression_ratio=0.1,
        extraction_metadata={"evidence_metadata": metadata or {}},
        content_hash="a" * 64,
        processing_time_ms=5,
    )


@pytest.fixture
def repo_with_case():
    repo = MockCaseRepository()
    case = create_sample_case(user_id="user_owner")
    case.evidence = [_evidence()]
    repo._storage[case.case_id] = case
    return repo, case


@pytest.fixture
def preprocessing_service():
    svc = MagicMock()
    svc.reclassify_evidence = AsyncMock(
        return_value=_preprocessing_result_for(
            new_data_type=DataType.LOGS_AND_ERRORS,
            metadata={
                "classification": {
                    "confidence": 1.0,
                    "source": "user_override",
                    "failed": False,
                    "suggested_types": [],
                },
                "extractor": {
                    "chosen_type": DataType.LOGS_AND_ERRORS.value,
                    "attempts": [
                        {
                            "data_type": DataType.METRICS_AND_PERFORMANCE.value,
                            "triggered_by": "initial",
                            "sanity_passed": True,
                            "duration_ms": 1,
                        },
                        {
                            "data_type": DataType.LOGS_AND_ERRORS.value,
                            "triggered_by": "user_override",
                            "sanity_passed": True,
                            "duration_ms": 2,
                        },
                    ],
                },
            },
        )
    )
    return svc


@pytest.fixture
def file_storage():
    svc = MagicMock()
    svc.retrieve_file = AsyncMock(return_value=b"line1\nline2 ERROR\nline3\n")
    return svc


@pytest.fixture
def service(repo_with_case, preprocessing_service, file_storage):
    repo, _ = repo_with_case
    return InvestigationService(
        milestone_engine=MockMilestoneEngine(),
        case_repository=repo,
        preprocessing_service=preprocessing_service,
        file_storage_service=file_storage,
    )


class TestAuthAndLookup:
    @pytest.mark.asyncio
    async def test_case_not_found_raises(self, service):
        with pytest.raises(NotFoundError):
            await service.reclassify_evidence(
                case_id="nonexistent",
                evidence_id="ev_aaaaaaaaaaaa",
                user_id="user_owner",
                data_type=DataType.LOGS_AND_ERRORS,
            )

    @pytest.mark.asyncio
    async def test_user_not_owner_raises_permission_denied(
        self, service, repo_with_case
    ):
        _, case = repo_with_case
        with pytest.raises(PermissionDeniedException):
            await service.reclassify_evidence(
                case_id=case.case_id,
                evidence_id="ev_aaaaaaaaaaaa",
                user_id="user_not_owner",
                data_type=DataType.LOGS_AND_ERRORS,
            )

    @pytest.mark.asyncio
    async def test_evidence_not_in_case_raises(self, service, repo_with_case):
        _, case = repo_with_case
        with pytest.raises(NotFoundError):
            await service.reclassify_evidence(
                case_id=case.case_id,
                evidence_id="ev_does_not_exist_zz",
                user_id="user_owner",
                data_type=DataType.LOGS_AND_ERRORS,
            )

    @pytest.mark.asyncio
    async def test_evidence_without_content_ref_raises_validation(
        self, service, repo_with_case
    ):
        """Pasted text evidence has no stored raw file — no re-extraction
        is possible. Must raise ValidationException (endpoint maps to 409)."""
        _, case = repo_with_case
        case.evidence = [_evidence(content_ref=None)]
        with pytest.raises(ValidationException):
            await service.reclassify_evidence(
                case_id=case.case_id,
                evidence_id="ev_aaaaaaaaaaaa",
                user_id="user_owner",
                data_type=DataType.LOGS_AND_ERRORS,
            )


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_reclassification_updates_evidence(
        self, service, repo_with_case, preprocessing_service
    ):
        _, case = repo_with_case
        previous_data_type = case.evidence[0].data_type

        updated = await service.reclassify_evidence(
            case_id=case.case_id,
            evidence_id="ev_aaaaaaaaaaaa",
            user_id="user_owner",
            data_type=DataType.LOGS_AND_ERRORS,
        )

        # Returned Evidence reflects the new unified data_type.
        # Note: Evidence.data_type stores the UnifiedDataType string
        # ("logs"), not the DataType string ("logs_and_errors") — this
        # matches the upload path's conventional storage. See
        # investigation_service._preprocess_attachment for the precedent.
        assert updated.data_type == UnifiedDataType.LOGS.value
        assert updated.preprocessed_content == "new index content"
        assert previous_data_type != updated.data_type

        # Preprocessing was called with user_override + previous metadata.
        kwargs = preprocessing_service.reclassify_evidence.call_args.kwargs
        assert kwargs["user_override"] == DataType.LOGS_AND_ERRORS

    @pytest.mark.asyncio
    async def test_metadata_contract_suppresses_low_confidence_marker(
        self, service, repo_with_case
    ):
        """After user_override, the new metadata has source='user_override'
        and confidence=1.0 — Phase 1's low-confidence marker must not
        fire on the updated row."""
        _, case = repo_with_case
        updated = await service.reclassify_evidence(
            case_id=case.case_id,
            evidence_id="ev_aaaaaaaaaaaa",
            user_id="user_owner",
            data_type=DataType.LOGS_AND_ERRORS,
        )
        assert updated.metadata is not None
        cls = updated.metadata["classification"]
        assert cls["source"] == "user_override"
        assert cls["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_attempts_history_preserved_on_row(self, service, repo_with_case):
        """The merged attempts list makes it onto the persisted row so
        observability can see the classification trail."""
        _, case = repo_with_case
        updated = await service.reclassify_evidence(
            case_id=case.case_id,
            evidence_id="ev_aaaaaaaaaaaa",
            user_id="user_owner",
            data_type=DataType.LOGS_AND_ERRORS,
        )
        attempts = updated.metadata["extractor"]["attempts"]
        assert len(attempts) == 2
        assert attempts[0]["triggered_by"] == "initial"
        assert attempts[-1]["triggered_by"] == "user_override"

    @pytest.mark.asyncio
    async def test_other_evidence_untouched(self, service, repo_with_case):
        """Reclassifying one evidence must not touch the others in the case.
        Pin per-evidence idempotency so batch-style bugs can't corrupt
        adjacent rows."""
        _, case = repo_with_case
        other = _evidence(evidence_id="ev_bbbbbbbbbbbb", data_type="structured_config")
        case.evidence.append(other)

        await service.reclassify_evidence(
            case_id=case.case_id,
            evidence_id="ev_aaaaaaaaaaaa",
            user_id="user_owner",
            data_type=DataType.LOGS_AND_ERRORS,
        )

        # Re-read via repo to see persisted state.
        saved = await service.repository.get(case.case_id)
        untouched = next(
            e for e in saved.evidence if e.evidence_id == "ev_bbbbbbbbbbbb"
        )
        assert untouched.data_type == "structured_config"
        assert untouched.preprocessed_content == "old index"
