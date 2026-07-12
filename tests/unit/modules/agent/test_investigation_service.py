"""Unit tests for InvestigationService.

Tests the InvestigationService which manages milestone-based troubleshooting workflow.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.exceptions import (
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
)
from faultmaven.models.api_models import (
    AttachmentResult,
    IntentType,
    QueryIntent,
    TurnResponse,
)
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import Case, CaseState

from .conftest import (
    MockCaseRepository,
    MockMilestoneEngine,
    create_sample_case,
    mock_case_repository,
    mock_milestone_engine,
    sample_case,
    sample_turn_payload,
    sample_user_id,
)


class TestInvestigationServiceProcessTurn:
    """Tests for InvestigationService.process_turn()."""

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        """Create InvestigationService with mocked dependencies."""
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.mark.asyncio
    async def test_process_turn_success(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
        sample_turn_payload,
    ):
        """Test successful turn processing."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        assert isinstance(response, TurnResponse)
        assert (
            response.agent_response == f"Agent response to: {sample_turn_payload.query}"
        )
        # Mock engine increments turn from 0 to 1, so response should have turn_number = 1
        assert (
            response.turn_number == 1
        ), f"Expected turn_number=1, got {response.turn_number} (initial was {sample_case.current_turn})"
        assert mock_milestone_engine.process_turn.called
        assert mock_case_repository.save.called

    @pytest.mark.asyncio
    async def test_process_turn_case_not_found(
        self, service, mock_case_repository, sample_user_id, sample_turn_payload
    ):
        """Test turn processing with non-existent case."""
        non_existent_case_id = f"case_{uuid4().hex[:12]}"

        with pytest.raises(NotFoundError, match="Case"):
            await service.process_turn(
                case_id=non_existent_case_id,
                user_id=sample_user_id,
                payload=sample_turn_payload,
            )

    @pytest.mark.asyncio
    async def test_process_turn_permission_denied(
        self, service, mock_case_repository, sample_case, sample_turn_payload
    ):
        """Test turn processing with unauthorized user."""
        # Pre-populate repository with case owned by different user
        await mock_case_repository.save(sample_case)
        unauthorized_user_id = str(uuid4())

        with pytest.raises(PermissionDeniedException, match="not authorized"):
            await service.process_turn(
                case_id=sample_case.case_id,
                user_id=unauthorized_user_id,
                payload=sample_turn_payload,
            )

    @pytest.mark.asyncio
    async def test_process_turn_saves_user_message(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
        sample_turn_payload,
    ):
        """Test that user message is saved before processing."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)
        initial_message_count = sample_case.message_count

        await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        # Verify case was saved with user message and agent response
        saved_case = await mock_case_repository.get(sample_case.case_id)
        assert (
            saved_case.message_count == initial_message_count + 2
        )  # User message + agent response
        assert (
            len(saved_case.messages) >= 2
        ), f"Expected at least 2 messages, got {len(saved_case.messages)}"
        user_messages = [m for m in saved_case.messages if m.get("role") == "user"]
        assert len(user_messages) >= 1, "User message should be saved"
        assert any(
            m["content"] == sample_turn_payload.query for m in user_messages
        ), f"User message '{sample_turn_payload.query}' not found in saved messages"

    @pytest.mark.asyncio
    async def test_process_turn_persists_engine_metadata_on_assistant_message(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
        sample_turn_payload,
    ):
        """Regression: rich turn metadata emitted by the engine must
        propagate to the persisted assistant message.

        Before fix: investigation_service hardcoded ``metadata={}`` on the
        assistant message, dropping all engine metadata fields. This made
        post-hoc diagnosis from persisted turn data impossible.
        """
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        engine_metadata = {
            "milestones_completed": ["symptom_verified"],
            "progress_made": False,
            "next_steps": ["check db connection pool size"],
        }
        mock_milestone_engine.process_turn = AsyncMock(
            return_value={
                "case_updated": sample_case,
                "agent_response": "agent response text",
                "metadata": engine_metadata,
            }
        )

        await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        saved_case = await mock_case_repository.get(sample_case.case_id)
        assistant_messages = [
            m for m in saved_case.messages if m.get("role") == "assistant"
        ]
        assert assistant_messages, "expected at least one assistant message"
        persisted = assistant_messages[-1].get("metadata") or {}

        assert persisted.get("milestones_completed") == ["symptom_verified"]
        assert persisted.get("progress_made") is False
        assert persisted.get("next_steps") == ["check db connection pool size"]

    @pytest.mark.asyncio
    async def test_process_turn_saves_agent_response(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
        sample_turn_payload,
    ):
        """Test that agent response is saved after processing."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        # Verify case was saved with agent response.
        # Role is "assistant" (OpenAI/Anthropic convention) — the legacy
        # "agent" role was renamed in investigation_service so the persisted
        # transcript stays compatible with provider message-role norms.
        saved_case = await mock_case_repository.get(sample_case.case_id)
        agent_messages = [
            m for m in saved_case.messages if m.get("role") == "assistant"
        ]
        assert len(agent_messages) >= 1
        assert agent_messages[-1]["content"] == response.agent_response

    @pytest.mark.asyncio
    async def test_process_turn_increments_turn_number(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
        sample_turn_payload,
    ):
        """Test that turn number is incremented."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        initial_turn = sample_case.current_turn  # Should be 0 from fixture
        await mock_case_repository.save(sample_case)

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        # Service flow:
        # 1. Gets case (current_turn = initial_turn, e.g., 0)
        # 2. Preprocesses any attachments (Step 1)
        # 3. Adds user message with turn_number = current_turn + 1 (e.g., 1)
        # 4. Engine processes (Step 2 - LLM inference)
        # 5. Service commits turn increment: current_turn = next_turn (e.g., 1)
        # 6. Response has turn_number = updated_case.current_turn (e.g., 1)
        expected_turn = initial_turn + 1
        assert (
            response.turn_number == expected_turn
        ), f"Expected turn_number={expected_turn}, got {response.turn_number} (initial was {initial_turn})"

        saved_case = await mock_case_repository.get(sample_case.case_id)
        assert (
            saved_case.current_turn == expected_turn
        ), f"Expected saved_case.current_turn={expected_turn}, got {saved_case.current_turn}"

    @pytest.mark.asyncio
    async def test_process_turn_with_attachments(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
    ):
        """Test turn processing with file attachments (Step 1 preprocessing)."""
        from unittest.mock import AsyncMock

        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        # Mock preprocessing service for attachment processing
        mock_preprocessing = AsyncMock()
        mock_preprocessing.classify_and_extract = AsyncMock(
            return_value=AsyncMock(
                summary="Test log file summary",
                structural_index="ERROR line 42: connection refused",
                data_type=AsyncMock(value="logs"),
                content_hash="abc123",
                extraction_method="structural_index",
            )
        )
        service.preprocessing_service = mock_preprocessing

        attachments = [
            Attachment(
                content=b"error log content here",
                filename="app.log",
                content_type="text/plain",
            ),
        ]

        payload = TurnPayload(
            query="Check this log file",
            attachments=attachments,
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=payload,
        )

        assert response.agent_response is not None
        assert len(response.attachments_processed) == 1
        assert response.attachments_processed[0].filename == "app.log"
        assert response.attachments_processed[0].processing_status == "completed"
        # Verify preprocessing was called
        mock_preprocessing.classify_and_extract.assert_called_once()

    @pytest.mark.asyncio
    async def test_attachment_metadata_includes_required_fields(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
    ):
        """Regression: attachment_metadata must include filename, size, source_type.

        Without these fields, milestone_engine._create_uploaded_file_from_attachment()
        defaults size_bytes=0, violating the uploaded_files_size_positive CHECK constraint.
        """
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        mock_preprocessing = AsyncMock()
        mock_preprocessing.classify_and_extract = AsyncMock(
            return_value=AsyncMock(
                summary="Pasted text summary",
                structural_index="Some extracted content",
                data_type=AsyncMock(value="text"),
                content_hash="hash123",
                extraction_method="structural_index",
            )
        )
        service.preprocessing_service = mock_preprocessing

        pasted_content = b"some pasted log data with enough bytes"
        attachments = [
            Attachment(
                content=pasted_content,
                filename="pasted-content-20260223T120000.txt",
                content_type="text/plain",
            ),
        ]

        payload = TurnPayload(
            query="Analyze this",
            attachments=attachments,
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=payload,
        )

        # Verify engine.process_turn was called with enriched attachment metadata
        call_kwargs = mock_milestone_engine.process_turn.call_args
        metadata_list = call_kwargs.kwargs.get("attachments") or call_kwargs[1].get(
            "attachments"
        )
        assert metadata_list is not None, "attachments should be passed to engine"
        assert len(metadata_list) == 1

        meta = metadata_list[0]
        assert meta["filename"] == "pasted-content-20260223T120000.txt"
        assert meta["size"] == len(
            pasted_content
        ), "size must match original content bytes"
        assert meta["size"] > 0, "size must be > 0 (CHECK constraint)"
        assert (
            meta["source_type"] == "paste"
        ), "pasted content should have source_type='paste'"
        # Post-010: attachment metadata no longer carries evidence_id —
        # Evidence is born only when the LLM extracts a claim-anchored
        # slice during INVESTIGATING. The UploadedFile is the file-of-
        # record for intake.
        assert "evidence_id" not in meta
        assert "file_id" in meta
        # file_id must match UploadedFile pattern: ^(file_|data_)[a-f0-9]{12,16}$
        assert meta["file_id"].startswith(
            "file_"
        ), "uploaded file ids use the file_ prefix"

    @pytest.mark.asyncio
    async def test_file_upload_attachment_metadata_source_type(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
    ):
        """File uploads should have source_type='file_upload' in attachment metadata."""
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        mock_preprocessing = AsyncMock()
        mock_preprocessing.classify_and_extract = AsyncMock(
            return_value=AsyncMock(
                summary="Log file summary",
                structural_index="ERROR at line 42",
                data_type=AsyncMock(value="logs"),
                content_hash="hash456",
                extraction_method="structural_index",
            )
        )
        service.preprocessing_service = mock_preprocessing

        file_content = b"2024-01-01 ERROR connection refused\n" * 10
        attachments = [
            Attachment(
                content=file_content,
                filename="server.log",
                content_type="text/plain",
            ),
        ]

        payload = TurnPayload(
            query="Check this log",
            attachments=attachments,
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=payload,
        )

        call_kwargs = mock_milestone_engine.process_turn.call_args
        metadata_list = call_kwargs.kwargs.get("attachments") or call_kwargs[1].get(
            "attachments"
        )
        assert metadata_list is not None
        meta = metadata_list[0]
        assert meta["source_type"] == "file_upload"
        assert meta["filename"] == "server.log"
        assert meta["size"] == len(file_content)
        assert meta["size"] > 0
        assert meta["file_id"].startswith(
            "file_"
        ), "file upload file_id should use file_ prefix"


class TestInvestigationServiceIntentDispatch:
    """Pins the dispatch wiring for intents.

    The investigation_service dispatcher must route every ``IntentType``
    enum value (either to ``engine.process_turn`` or to a ValidationException
    for not-yet-implemented intents), never hitting a 500-class
    ``raise ValueError("Unknown intent type: ...")``.

    NOTE (investigation-flow redesign): the PATH_SELECTION (Gate 2) and
    POST_MITIGATION_CHOICE (Gate 3) intents were removed with the path
    fork, so their dispatch tests are gone — the exhaustiveness test below
    is the surviving guard.
    """

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.mark.parametrize("intent_value", list(IntentType))
    @pytest.mark.asyncio
    async def test_every_intent_type_is_routed(
        self,
        intent_value,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
    ):
        """Parametrized exhaustiveness: every ``IntentType`` enum value must
        either dispatch without "Unknown intent type" error or raise a
        ValidationException (NOT_IMPLEMENTED). A 500-class ``ServiceException``
        wrapping an unknown-intent ``ValueError`` is the regression shape we
        are preventing.

        Adding a new IntentType without a dispatch route would either fail
        the boot check at service construction OR fall through to an
        unhandled branch — both surfaces caught here.
        """
        from faultmaven.exceptions import ServiceException, ValidationException

        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        # Build a minimally-valid QueryIntent for each intent value.
        # Each intent type has its own required-field constraints
        # (validated by QueryIntent.model_validator); we satisfy them with
        # stub data so the test focuses on dispatch wiring, not intent
        # validation.
        intent_kwargs: dict = {"type": intent_value}
        if intent_value == IntentType.STATUS_TRANSITION:
            intent_kwargs["to_state"] = "closed"
        elif intent_value == IntentType.CONFIRMATION:
            intent_kwargs["confirmation_value"] = True
        elif intent_value == IntentType.HYPOTHESIS_ACTION:
            intent_kwargs["hypothesis_id"] = "hyp_test"
            intent_kwargs["action"] = "accept"
        elif intent_value == IntentType.EVIDENCE_NEED:
            intent_kwargs["evidence_need_id"] = "eneed_test12345"
        elif intent_value == IntentType.FILE_RECLASSIFICATION:
            intent_kwargs["file_id"] = "file_test12345678"
            intent_kwargs["data_type"] = "logs_and_errors"

        payload = TurnPayload(query="test", intent=QueryIntent(**intent_kwargs))

        try:
            await service.process_turn(
                case_id=sample_case.case_id,
                user_id=sample_user_id,
                payload=payload,
            )
        except ValidationException:
            # NOT_IMPLEMENTED intents (e.g., EVIDENCE_NEED) raise this
            # — 422 to the client, contract gap surfaced honestly.
            pass
        except NotFoundError:
            # A contract-valid intent may reference a resource the sample
            # case lacks (FILE_RECLASSIFICATION's stub file_id) — 404 to
            # the client; the dispatch itself routed correctly.
            pass
        except ServiceException as e:
            # Two regression shapes surface as ServiceException (500):
            # the legacy elif-chain "Unknown intent type" error, and the
            # SERVICE-dispatch else-branch "has no handler method" (a
            # dispatch-table entry without a matching elif in
            # process_turn — the boot check can't see that gap).
            assert "Unknown intent type" not in str(e), (
                f"IntentType.{intent_value.name} surfaced as 500 with "
                f"'Unknown intent type'. Dispatch table is incomplete."
            )
            assert "has no handler method" not in str(e), (
                f"IntentType.{intent_value.name} is SERVICE-routed in "
                f"_INTENT_DISPATCH but has no handler branch in "
                f"process_turn."
            )

    def test_boot_check_rejects_incomplete_dispatch_table(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Service construction must fail if any IntentType lacks a dispatch
        route. Converts the silent-runtime-500 failure mode into a
        startup-time error caught by CI.
        """
        from unittest.mock import patch

        from faultmaven.modules.agent.domain.services import investigation_service

        # Simulate a developer who added a new IntentType without updating
        # the dispatch table.
        broken_dispatch = dict(investigation_service._INTENT_DISPATCH)
        del broken_dispatch[IntentType.CONFIRMATION]

        with patch.object(investigation_service, "_INTENT_DISPATCH", broken_dispatch):
            with pytest.raises(RuntimeError) as exc_info:
                InvestigationService(
                    milestone_engine=mock_milestone_engine,
                    case_repository=mock_case_repository,
                )
        assert "confirmation" in str(exc_info.value)
        assert "incomplete" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_not_implemented_intent_raises_validation_exception(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
    ):
        """Intents marked NOT_IMPLEMENTED in the dispatch table must raise
        ValidationException (422), not ServiceException (500). EVIDENCE_NEED
        is currently in this state — defined in the enum + QueryIntent
        validator but with no handler.
        """
        from faultmaven.exceptions import ValidationException

        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        payload = TurnPayload(
            query="test",
            intent=QueryIntent(
                type=IntentType.EVIDENCE_NEED, evidence_need_id="eneed_test12345"
            ),
        )

        with pytest.raises(ValidationException) as exc_info:
            await service.process_turn(
                case_id=sample_case.case_id,
                user_id=sample_user_id,
                payload=payload,
            )
        assert "not implemented" in str(exc_info.value).lower()


class TestInvestigationServiceGetProgress:
    """Tests for InvestigationService.get_progress()."""

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        """Create InvestigationService with mocked dependencies."""
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.mark.asyncio
    async def test_get_progress_success(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test successful progress retrieval."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        progress = await service.get_progress(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
        )

        assert progress["case_id"] == sample_case.case_id
        assert progress["state"] == sample_case.state.value
        assert "current_turn" in progress
        assert (
            "milestones_completed" in progress
        )  # InvestigationProgress.completed_milestones property
        assert (
            "pending_milestones" in progress
        )  # InvestigationProgress.pending_milestones property
        # Note: completion_percentage was removed from InvestigationProgress model

    @pytest.mark.asyncio
    async def test_get_progress_case_not_found(
        self, service, mock_case_repository, sample_user_id
    ):
        """Test progress retrieval with non-existent case."""
        non_existent_case_id = f"case_{uuid4().hex[:12]}"

        with pytest.raises(NotFoundError, match="Case"):
            await service.get_progress(
                case_id=non_existent_case_id,
                user_id=sample_user_id,
            )

    @pytest.mark.asyncio
    async def test_get_progress_permission_denied(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test progress retrieval with unauthorized user."""
        # Pre-populate repository - set sample_case to have sample_user_id as owner
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)
        unauthorized_user_id = str(uuid4())  # Different user (not the owner)

        with pytest.raises(PermissionDeniedException, match="not authorized"):
            await service.get_progress(
                case_id=sample_case.case_id,
                user_id=unauthorized_user_id,
            )


class TestInvestigationServiceTransitionToInvestigating:
    """Tests for InvestigationService.transition_to_investigating()."""

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        """Create InvestigationService with mocked dependencies."""
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.fixture
    def inquiry_case(self):
        """Create a case in INQUIRY state."""
        return create_sample_case(
            state=CaseState.INQUIRY,
        )

    @pytest.mark.asyncio
    async def test_transition_to_investigating_success(
        self, service, mock_case_repository, inquiry_case, sample_user_id
    ):
        """Test successful transition to INVESTIGATING."""
        # Pre-populate repository
        inquiry_case.user_id = sample_user_id
        await mock_case_repository.save(inquiry_case)

        confirmed_description = "Confirmed problem description"
        updated_case = await service.transition_to_investigating(
            case_id=inquiry_case.case_id,
            user_id=sample_user_id,
            confirmed_description=confirmed_description,
        )

        assert updated_case.state == CaseState.INVESTIGATING
        assert updated_case.description == confirmed_description
        assert mock_case_repository.save.called

    @pytest.mark.asyncio
    async def test_transition_to_investigating_case_not_found(
        self, service, mock_case_repository, sample_user_id
    ):
        """Test transition with non-existent case."""
        non_existent_case_id = f"case_{uuid4().hex[:12]}"

        with pytest.raises(NotFoundError, match="Case"):
            await service.transition_to_investigating(
                case_id=non_existent_case_id,
                user_id=sample_user_id,
                confirmed_description="Test description",
            )

    @pytest.mark.asyncio
    async def test_transition_to_investigating_permission_denied(
        self, service, mock_case_repository, inquiry_case
    ):
        """Test transition with unauthorized user."""
        # Pre-populate repository
        await mock_case_repository.save(inquiry_case)
        unauthorized_user_id = str(uuid4())

        with pytest.raises(PermissionDeniedException, match="not authorized"):
            await service.transition_to_investigating(
                case_id=inquiry_case.case_id,
                user_id=unauthorized_user_id,
                confirmed_description="Test description",
            )

    @pytest.mark.asyncio
    async def test_transition_to_investigating_invalid_status(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test transition from non-INQUIRY state."""
        # Pre-populate repository with case in INVESTIGATING state (cannot transition from non-INQUIRY)
        # INVESTIGATING state requires: confirmed problem statement, decided to investigate, and description
        from datetime import datetime, timezone

        sample_case.user_id = sample_user_id
        sample_case.description = "Test description"  # Required for INVESTIGATING
        # Set up inquiry data required for INVESTIGATING state
        sample_case.inquiry.proposed_problem_statement = "Test problem statement"
        sample_case.inquiry.problem_statement_confirmed = True
        sample_case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
        sample_case.inquiry.decided_to_investigate = True
        sample_case.inquiry.decision_made_at = datetime.now(timezone.utc)

        # Now set state to INVESTIGATING (all requirements are met)
        sample_case.state = CaseState.INVESTIGATING
        await mock_case_repository.save(sample_case)

        # Verify the case is stored with INVESTIGATING state
        stored_case = await mock_case_repository.get(sample_case.case_id)
        assert stored_case is not None, "Case should be stored in repository"
        assert (
            stored_case.state == CaseState.INVESTIGATING
        ), f"Case state should be INVESTIGATING, got {stored_case.state}"
        assert (
            stored_case.state != CaseState.INQUIRY
        ), "Case should NOT be in INQUIRY state for this test"

        # The service should raise ServiceException when trying to transition from non-INQUIRY state
        with pytest.raises(ServiceException) as exc_info:
            await service.transition_to_investigating(
                case_id=sample_case.case_id,
                user_id=sample_user_id,
                confirmed_description="Test description",
            )

        # Verify the exception message contains "Cannot transition"
        assert "Cannot transition" in str(
            exc_info.value
        ), f"Exception message should contain 'Cannot transition', got: {str(exc_info.value)}"
        assert "investigating" in str(exc_info.value).lower() or "INVESTIGATING" in str(
            exc_info.value
        ), f"Exception message should mention the current state, got: {str(exc_info.value)}"


class TestInvestigationServiceCloseCase:
    """Tests for InvestigationService.close_case()."""

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        """Create InvestigationService with mocked dependencies."""
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.mark.asyncio
    async def test_close_case_success(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test successful case closure."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        closure_reason = "closed_after_investigation"
        updated_case = await service.close_case(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            closure_reason=closure_reason,
        )

        assert updated_case.state == CaseState.CLOSED
        assert updated_case.closure_reason == closure_reason
        assert updated_case.closed_at is not None
        assert mock_case_repository.save.called

    @pytest.mark.asyncio
    async def test_close_case_case_not_found(
        self, service, mock_case_repository, sample_user_id
    ):
        """Test case closure with non-existent case."""
        non_existent_case_id = f"case_{uuid4().hex[:12]}"

        with pytest.raises(NotFoundError, match="Case"):
            await service.close_case(
                case_id=non_existent_case_id,
                user_id=sample_user_id,
                closure_reason="closed_after_investigation",
            )

    @pytest.mark.asyncio
    async def test_close_case_permission_denied(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test case closure with unauthorized user."""
        # Pre-populate repository - set sample_case to have sample_user_id as owner
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)
        unauthorized_user_id = str(uuid4())  # Different user

        with pytest.raises(PermissionDeniedException, match="not authorized"):
            await service.close_case(
                case_id=sample_case.case_id,
                user_id=unauthorized_user_id,
                closure_reason="closed_after_investigation",
            )

    @pytest.mark.asyncio
    async def test_close_case_sets_closed_at(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test that closed_at timestamp is set."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        updated_case = await service.close_case(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            closure_reason="closed_after_investigation",
        )

        assert updated_case.closed_at is not None
        assert isinstance(updated_case.closed_at, datetime)


class TestBuildProgressTransparencyVerificationStatus:
    """Phase 3: the honest-partial verification_status must reach the live
    TurnResponse even when transparent mode (the time-stall detector) is not
    active — a declared data wall reaches INSUFFICIENT_EVIDENCE first."""

    def _service(self):
        from unittest.mock import MagicMock

        return InvestigationService(
            milestone_engine=MagicMock(),
            case_repository=MagicMock(),
        )

    def _case(self, status):
        from faultmaven.modules.case.domain.models import (
            InvestigationProgress,
            VerificationStatus,
        )

        case = create_sample_case()
        case.progress = InvestigationProgress(
            verification_status=getattr(VerificationStatus, status)
        )
        return case

    def test_insufficient_evidence_surfaced_without_transparent_mode(self):
        svc = self._service()
        case = self._case("INSUFFICIENT_EVIDENCE")
        # No progress_transparent flag → previously returned None, hiding the
        # honest partial. Now it must surface with active reflecting the stall.
        info = svc._build_progress_transparency({}, case)
        assert info is not None
        assert info.active is False
        assert info.verification_status == "insufficient_evidence"

    def test_non_insufficient_status_stays_silent_without_transparent_mode(self):
        svc = self._service()
        case = self._case("OPEN")
        # OPEN is not the honest-partial; without transparent mode, stay silent.
        assert svc._build_progress_transparency({}, case) is None

    def test_transparent_mode_still_active_and_carries_status(self):
        svc = self._service()
        case = self._case("INSUFFICIENT_EVIDENCE")
        info = svc._build_progress_transparency(
            {
                "progress_transparent": True,
                "pending_milestone": "root_cause_identified",
            },
            case,
        )
        assert info is not None
        assert info.active is True
        assert info.verification_status == "insufficient_evidence"
        assert info.pending_milestone == "root_cause_identified"

    def test_cause_assurance_carried_alongside_status(self):
        # #572: the persisted assurance grade rides the same surfacing object so
        # the frontend can label a lower-assurance conclusion.
        from faultmaven.modules.case.domain.models import CauseAssuranceGrade

        svc = self._service()
        case = self._case("INSUFFICIENT_EVIDENCE")
        case.progress.cause_assurance = CauseAssuranceGrade.MECHANISTIC
        info = svc._build_progress_transparency({}, case)
        assert info is not None
        assert info.cause_assurance == "mechanistic"
