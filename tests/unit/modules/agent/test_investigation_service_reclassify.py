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
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from faultmaven.models.api import DataType
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    UploadedFile,
)

from .conftest import MockCaseRepository, MockMilestoneEngine, create_sample_case

_SOURCE_TYPE_MAP = {
    "logs": EvidenceSourceType.LOGS,
    "metrics": EvidenceSourceType.METRICS,
    "configuration": EvidenceSourceType.CONFIGURATION,
    "structured_config": EvidenceSourceType.CONFIGURATION,
    "code": EvidenceSourceType.CODE,
    "text": EvidenceSourceType.TEXT,
}


def _evidence(
    evidence_id: str = "ev_aaaaaaaaaaaa",
    content_ref: str | None = "evidence/case_x/server.log",
    data_type: str = "metrics",
    metadata: dict | None = None,
    source_file_id: str | None = "file_aaaaaaaaaaaa",
    source_type: EvidenceSourceType | None = None,
) -> Evidence:
    resolved_source_type = (
        source_type
        if source_type is not None
        else _SOURCE_TYPE_MAP.get(data_type, EvidenceSourceType.METRICS)
    )
    return Evidence(
        evidence_id=evidence_id,
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="Test",
        summary="Old summary",
        extract="old index",
        source_type=resolved_source_type,
        source_file_id=source_file_id,
        collected_by="user",
        collected_at=datetime.now(UTC),
        collected_at_turn=0,
        metadata=metadata,
    )


def _uploaded_file(
    file_id: str = "file_aaaaaaaaaaaa",
    filename: str = "server.log",
    storage_ref: str | None = "evidence/case_x/server.log",
) -> UploadedFile:
    return UploadedFile(
        file_id=file_id,
        filename=filename,
        size_bytes=100,
        storage_ref=storage_ref,
        uploaded_at_turn=0,
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
    case.uploaded_files = [_uploaded_file()]
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
    async def test_user_not_owner_raises_authorization_error(
        self, service, repo_with_case
    ):
        """Non-owner caller raises AuthorizationError → HTTP 403."""
        _, case = repo_with_case
        with pytest.raises(AuthorizationError):
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
    async def test_evidence_without_content_ref_raises_conflict(
        self, service, repo_with_case
    ):
        """Chat-extracted evidence (source_file_id=None) has no stored
        raw file — re-extraction is impossible. Raises ConflictError
        with ``conflict_reason="no_backing_file"`` so clients can
        branch on the structured field instead of parsing the detail
        string.
        """
        _, case = repo_with_case
        case.evidence = [
            _evidence(
                source_file_id=None,
                source_type=EvidenceSourceType.USER_DESCRIPTION,
            )
        ]
        case.uploaded_files = []
        with pytest.raises(ConflictError) as exc:
            await service.reclassify_evidence(
                case_id=case.case_id,
                evidence_id="ev_aaaaaaaaaaaa",
                user_id="user_owner",
                data_type=DataType.LOGS_AND_ERRORS,
            )

        # Structured metadata is what the HTTP body surfaces — clients
        # branch on these, not on the detail string.
        assert exc.value.resource_type == "evidence"
        assert exc.value.resource_id == "ev_aaaaaaaaaaaa"
        assert exc.value.conflict_reason == "no_backing_file"


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_reclassification_updates_file_and_evidence_source_type(
        self, service, repo_with_case, preprocessing_service
    ):
        """Post-010: structural_index / summary / data_type land on the
        backing UploadedFile (file-level metadata). Evidence.source_type
        is re-aligned so the agent sees a consistent picture. The LLM's
        own summary/extract fields on Evidence are not touched —
        reclassifying a file doesn't rewrite the claim built on it."""
        _, case = repo_with_case
        previous_source_type = case.evidence[0].source_type.value
        previous_summary = case.evidence[0].summary
        previous_extract = case.evidence[0].extract

        updated = await service.reclassify_evidence(
            case_id=case.case_id,
            evidence_id="ev_aaaaaaaaaaaa",
            user_id="user_owner",
            data_type=DataType.LOGS_AND_ERRORS,
        )

        # Evidence.source_type re-aligned with new data_type
        assert previous_source_type != updated.source_type.value

        # LLM-authored claim fields untouched on Evidence
        assert updated.summary == previous_summary
        assert updated.extract == previous_extract

        # File-level preprocessing artifacts landed on UploadedFile
        saved = await service.repository.get(case.case_id)
        uf = next(f for f in saved.uploaded_files if f.file_id == "file_aaaaaaaaaaaa")
        assert uf.structural_index == "new index content"
        assert uf.summary == "new summary"
        assert uf.data_type == updated.source_type.value

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
        other = _evidence(
            evidence_id="ev_bbbbbbbbbbbb",
            data_type="structured_config",
            source_file_id="file_bbbbbbbbbbbb",
        )
        case.evidence.append(other)
        case.uploaded_files.append(
            _uploaded_file(
                file_id="file_bbbbbbbbbbbb",
                filename="config.yaml",
                storage_ref="evidence/case_x/config.yaml",
            )
        )

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
        assert untouched.source_type.value == EvidenceSourceType.CONFIGURATION.value
        assert untouched.extract == "old index"
        # The unrelated UploadedFile must also be untouched.
        untouched_file = next(
            f for f in saved.uploaded_files if f.file_id == "file_bbbbbbbbbbbb"
        )
        assert untouched_file.structural_index is None
        assert untouched_file.summary is None
