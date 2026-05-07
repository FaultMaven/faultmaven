"""Unit tests for the two-step turn pipeline in InvestigationService.

Tests the unified ingestion pipeline (v4.1) two-step processing:
Step 1: _preprocess_attachment() — Tier 0+1 preprocessing before LLM
Step 2: MilestoneEngine LLM inference

Covers:
- Attachment preprocessing creates Evidence with form=DOCUMENT
- Structural index populated from preprocessing result
- generate_implicit_query() for single and multiple files
- Step 1 ordering: attachments preprocessed before LLM call
- Preprocessing failure propagation

Design Reference:
- docs/working/IMPLEMENTATION-unified-ingestion-pipeline.md (Phase 6.4)
"""

from unittest.mock import AsyncMock, MagicMock, call
from uuid import uuid4

import pytest

from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.investigation.turn_pipeline import generate_implicit_query
from faultmaven.models.api_models import IntentType, QueryIntent, TurnResponse
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import (
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
)

from .conftest import MockCaseRepository, MockMilestoneEngine, create_sample_case

# ============================================================
# Helpers
# ============================================================


def _make_preprocessing_result(
    summary="Log file summary",
    structural_index="ERROR line 42: connection refused",
    data_type_value="logs",
    content_hash="abc123def456",
    extraction_method="crime_scene",
):
    """Create a mock preprocessing result."""
    result = MagicMock()
    result.summary = summary
    result.structural_index = structural_index
    result.data_type = MagicMock(value=data_type_value)
    result.content_hash = content_hash
    result.extraction_method = extraction_method
    return result


def _make_service(
    mock_milestone_engine,
    mock_case_repository,
    preprocessing_result=None,
):
    """Create InvestigationService with mocked preprocessing service."""
    mock_preprocessing = AsyncMock()
    mock_preprocessing.classify_and_extract = AsyncMock(
        return_value=preprocessing_result or _make_preprocessing_result()
    )

    return InvestigationService(
        milestone_engine=mock_milestone_engine,
        case_repository=mock_case_repository,
        preprocessing_service=mock_preprocessing,
    )


# ============================================================
# Step 1: _preprocess_attachment() Evidence Creation
# ============================================================


class TestPreprocessAttachmentEvidenceCreation:
    """Test _preprocess_attachment() creates Evidence with correct fields."""

    @pytest.fixture
    def mock_case_repository(self):
        return MockCaseRepository()

    @pytest.fixture
    def mock_milestone_engine(self):
        return MockMilestoneEngine()

    @pytest.mark.asyncio
    async def test_attachment_creates_document_form_evidence(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Attachment preprocessing always produces Evidence with form=DOCUMENT."""
        service = _make_service(mock_milestone_engine, mock_case_repository)

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        payload = TurnPayload(
            query="Analyze this log",
            attachments=[
                Attachment(
                    content=b"2026-02-22 ERROR: timeout",
                    filename="app.log",
                    content_type="text/plain",
                )
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        response = await service.process_turn(case.case_id, "user_owner", payload)

        # Evidence should have been added to the case
        saved_case = await mock_case_repository.get(case.case_id)
        data_evidence = [
            e for e in saved_case.evidence if e.form == EvidenceForm.DOCUMENT
        ]
        assert len(data_evidence) == 1
        assert data_evidence[0].form == EvidenceForm.DOCUMENT

    @pytest.mark.asyncio
    async def test_attachment_evidence_has_structural_index(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Preprocessed evidence has structural_index from Tier 0+1 extraction."""
        preprocessing_result = _make_preprocessing_result(
            structural_index="============\nCRIME SCENE EXTRACTION\n============\nERROR: Connection timeout",
        )
        service = _make_service(
            mock_milestone_engine,
            mock_case_repository,
            preprocessing_result=preprocessing_result,
        )

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        payload = TurnPayload(
            query="Check this",
            attachments=[
                Attachment(
                    content=b"log content",
                    filename="error.log",
                    content_type="text/plain",
                )
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(case.case_id, "user_owner", payload)

        saved_case = await mock_case_repository.get(case.case_id)
        ev = saved_case.evidence[0]
        assert "CRIME SCENE EXTRACTION" in ev.extract
        assert "Connection timeout" in ev.extract

    @pytest.mark.asyncio
    async def test_attachment_evidence_has_data_type(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Preprocessed evidence carries a classified source_type.

        The DataType→EvidenceSourceType mapping is owned by production
        (`_infer_source_type`); we just assert the resulting evidence has a
        real EvidenceSourceType, not None or a raw mock.
        """
        from faultmaven.models.api import DataType

        preprocessing_result = _make_preprocessing_result()
        # Hand the production code a real DataType enum so the source_type
        # lookup resolves to METRICS rather than the TEXT fallback.
        preprocessing_result.data_type = DataType.METRICS_AND_PERFORMANCE
        service = _make_service(
            mock_milestone_engine,
            mock_case_repository,
            preprocessing_result=preprocessing_result,
        )

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        payload = TurnPayload(
            query="Check metrics",
            attachments=[
                Attachment(
                    content=b"cpu_usage: 99%",
                    filename="metrics.csv",
                    content_type="text/csv",
                )
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(case.case_id, "user_owner", payload)

        saved_case = await mock_case_repository.get(case.case_id)
        assert saved_case.evidence[0].source_type == EvidenceSourceType.METRICS
        assert saved_case.evidence[0].source_type.value == "metrics"

    @pytest.mark.asyncio
    async def test_attachment_evidence_has_content_size(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Backing UploadedFile.size_bytes matches original attachment size."""
        service = _make_service(mock_milestone_engine, mock_case_repository)
        raw_content = b"A" * 1234

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        payload = TurnPayload(
            query="Check this",
            attachments=[
                Attachment(
                    content=raw_content,
                    filename="data.txt",
                    content_type="text/plain",
                )
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(case.case_id, "user_owner", payload)

        saved_case = await mock_case_repository.get(case.case_id)
        ev = saved_case.evidence[0]
        backing_file = saved_case.find_uploaded_file(ev.source_file_id)
        assert backing_file is not None
        assert backing_file.size_bytes == 1234

    @pytest.mark.asyncio
    async def test_multiple_attachments_create_multiple_evidence(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Multiple attachments each create their own Evidence record."""
        service = _make_service(mock_milestone_engine, mock_case_repository)

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        payload = TurnPayload(
            query="Analyze these files",
            attachments=[
                Attachment(
                    content=b"log content",
                    filename="app.log",
                    content_type="text/plain",
                ),
                Attachment(
                    content=b"config content",
                    filename="config.yaml",
                    content_type="text/yaml",
                ),
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        response = await service.process_turn(case.case_id, "user_owner", payload)

        saved_case = await mock_case_repository.get(case.case_id)
        doc_evidence = [
            e for e in saved_case.evidence if e.form == EvidenceForm.DOCUMENT
        ]
        assert len(doc_evidence) == 2
        assert len(response.attachments_processed) == 2

    @pytest.mark.asyncio
    async def test_attachment_result_has_correct_filename(
        self, mock_milestone_engine, mock_case_repository
    ):
        """TurnResponse.attachments_processed includes correct filename."""
        service = _make_service(mock_milestone_engine, mock_case_repository)

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        payload = TurnPayload(
            query="Check log",
            attachments=[
                Attachment(
                    content=b"error logs",
                    filename="production.log",
                    content_type="text/plain",
                )
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        response = await service.process_turn(case.case_id, "user_owner", payload)

        assert response.attachments_processed[0].filename == "production.log"
        assert response.attachments_processed[0].processing_status == "completed"


# ============================================================
# generate_implicit_query()
# ============================================================


class TestGenerateImplicitQuery:
    """Test implicit query generation when attachments submitted without query."""

    def test_single_file_query_includes_filename(self):
        """Single file → query mentions filename."""
        attachment = Attachment(
            content=b"data", filename="app.log", content_type="text/plain"
        )
        evidence = MagicMock()
        evidence.source_type.value = "logs"

        result = generate_implicit_query([attachment], [evidence])

        assert "app.log" in result
        assert "logs" in result
        assert "Analyze" in result

    def test_single_file_query_includes_data_type(self):
        """Single file → query mentions classified data type."""
        attachment = Attachment(
            content=b"data", filename="metrics.csv", content_type="text/csv"
        )
        evidence = MagicMock()
        evidence.source_type.value = "metrics"

        result = generate_implicit_query([attachment], [evidence])

        assert "metrics" in result

    def test_multiple_files_query_includes_count(self):
        """Multiple files → query mentions file count."""
        attachments = [
            Attachment(content=b"a", filename="app.log", content_type="text/plain"),
            Attachment(content=b"b", filename="db.log", content_type="text/plain"),
            Attachment(content=b"c", filename="config.yaml", content_type="text/yaml"),
        ]
        evidence_list = [MagicMock(), MagicMock(), MagicMock()]

        result = generate_implicit_query(attachments, evidence_list)

        assert "3 files" in result
        assert "app.log" in result
        assert "db.log" in result
        assert "config.yaml" in result

    def test_single_file_no_attachments_fallback(self):
        """Single evidence with empty attachments → uses 'data' as filename."""
        evidence = MagicMock()
        evidence.source_type.value = "logs"

        result = generate_implicit_query([], [evidence])

        assert "data" in result
        assert "logs" in result


# ============================================================
# Implicit Query Integration (attachments without query)
# ============================================================


class TestImplicitQueryIntegration:
    """Test that implicit query is generated when no query text is provided."""

    @pytest.fixture
    def mock_case_repository(self):
        return MockCaseRepository()

    @pytest.fixture
    def mock_milestone_engine(self):
        return MockMilestoneEngine()

    @pytest.mark.asyncio
    async def test_attachment_only_generates_implicit_query(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Attachments without query → implicit query generated for LLM."""
        service = _make_service(mock_milestone_engine, mock_case_repository)

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        # Payload with attachments but NO query
        payload = TurnPayload(
            query=None,
            attachments=[
                Attachment(
                    content=b"error content",
                    filename="app.log",
                    content_type="text/plain",
                )
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        response = await service.process_turn(case.case_id, "user_owner", payload)

        # The engine should have received a generated implicit query
        engine_call = mock_milestone_engine.process_turn.call_args
        user_message = engine_call.kwargs.get("user_message") or engine_call.args[1]
        assert "app.log" in user_message
        assert "Analyze" in user_message


# ============================================================
# Step 1 Before Step 2 Ordering
# ============================================================


class TestPipelineOrdering:
    """Test that Step 1 (preprocessing) completes before Step 2 (LLM)."""

    @pytest.fixture
    def mock_case_repository(self):
        return MockCaseRepository()

    @pytest.fixture
    def mock_milestone_engine(self):
        return MockMilestoneEngine()

    @pytest.mark.asyncio
    async def test_evidence_available_to_engine(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Evidence from Step 1 is on the case before engine.process_turn()."""
        service = _make_service(mock_milestone_engine, mock_case_repository)

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        payload = TurnPayload(
            query="Check logs",
            attachments=[
                Attachment(
                    content=b"log data",
                    filename="app.log",
                    content_type="text/plain",
                )
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(case.case_id, "user_owner", payload)

        # Verify engine received the case with evidence already attached
        engine_call = mock_milestone_engine.process_turn.call_args
        case_arg = engine_call.kwargs.get("case") or engine_call.args[0]
        assert len(case_arg.evidence) >= 1
        assert case_arg.evidence[0].form == EvidenceForm.DOCUMENT

    @pytest.mark.asyncio
    async def test_attachment_metadata_passed_to_engine(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Engine receives attachment metadata from preprocessing.

        The engine-side `data_type` carries `evidence.source_type.value`, the
        canonical post-classification shape (logs/metrics/...). The
        preprocessing-side DataType drives the source_type via
        `_infer_source_type`, so we feed a real DataType here.
        """
        from faultmaven.models.api import DataType

        preprocessing_result = _make_preprocessing_result(
            summary="Application error log",
        )
        preprocessing_result.data_type = DataType.LOGS_AND_ERRORS
        service = _make_service(
            mock_milestone_engine,
            mock_case_repository,
            preprocessing_result=preprocessing_result,
        )

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        payload = TurnPayload(
            query="Check this log",
            attachments=[
                Attachment(
                    content=b"error data",
                    filename="app.log",
                    content_type="text/plain",
                )
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(case.case_id, "user_owner", payload)

        # Verify attachment metadata was passed to the engine
        engine_call = mock_milestone_engine.process_turn.call_args
        attachments_arg = engine_call.kwargs.get("attachments")
        assert attachments_arg is not None
        assert len(attachments_arg) == 1
        assert attachments_arg[0]["data_type"] == "logs"
        assert attachments_arg[0]["summary"] == "Application error log"

    @pytest.mark.asyncio
    async def test_preprocessing_called_for_each_attachment(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Preprocessing service called once per attachment."""
        service = _make_service(mock_milestone_engine, mock_case_repository)

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        payload = TurnPayload(
            query="Analyze files",
            attachments=[
                Attachment(
                    content=b"log1", filename="a.log", content_type="text/plain"
                ),
                Attachment(
                    content=b"log2", filename="b.log", content_type="text/plain"
                ),
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(case.case_id, "user_owner", payload)

        assert service.preprocessing_service.classify_and_extract.call_count == 2


# ============================================================
# Query-Only Turns (No Attachments)
# ============================================================


class TestQueryOnlyTurns:
    """Test that query-only turns skip Step 1 entirely."""

    @pytest.fixture
    def mock_case_repository(self):
        return MockCaseRepository()

    @pytest.fixture
    def mock_milestone_engine(self):
        return MockMilestoneEngine()

    @pytest.mark.asyncio
    async def test_query_only_skips_preprocessing(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Query without attachments does not call preprocessing."""
        service = _make_service(mock_milestone_engine, mock_case_repository)

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        payload = TurnPayload(
            query="What could be causing the timeouts?",
            attachments=[],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(case.case_id, "user_owner", payload)

        service.preprocessing_service.classify_and_extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_query_only_no_evidence_created(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Query-only turn creates no attachment evidence."""
        service = _make_service(mock_milestone_engine, mock_case_repository)

        case = create_sample_case()
        case.user_id = "user_owner"
        await mock_case_repository.save(case)

        payload = TurnPayload(
            query="What could be causing the timeouts?",
            attachments=[],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        response = await service.process_turn(case.case_id, "user_owner", payload)

        assert len(response.attachments_processed) == 0
