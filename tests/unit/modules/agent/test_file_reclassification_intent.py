"""FILE_RECLASSIFICATION intent — structured resolution for classification_failed.

The cross-client resolution contract for a ``classification_failed`` upload
(faultmaven-slack-agent#27): the clarification DECIDE suggestions carry an
engine-owned ``file_reclassification`` intent (file_id + target DataType);
clients forward the intent on click (or the intent resolver matches a typed
choice), and the SERVICE handler re-runs preprocessing mechanically — no LLM
call, so the choice can never be misread as an analysis request.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.preprocessing.models import UnifiedDataType
from faultmaven.exceptions import NotFoundError, ValidationException
from faultmaven.models.api import DataType
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    _DATA_TYPE_TO_SOURCE_TYPE,
    InvestigationService,
    _build_classification_clarification_suggestions,
    _PreprocessedAttachment,
)
from faultmaven.modules.case.domain.models import CaseState, EvidenceSourceType

from .conftest import (
    MockCaseRepository,
    MockMilestoneEngine,
    create_sample_case,
    make_evidence,
    make_preprocessing_result,
    make_uploaded_file,
)


def _clarification_target(
    suggested_types: list[str] | None = None,
) -> _PreprocessedAttachment:
    return _PreprocessedAttachment(
        uploaded_file=make_uploaded_file(),
        classification_failed=True,
        suggested_types=(
            suggested_types
            if suggested_types is not None
            else ["logs_and_errors", "structured_config"]
        ),
        attachment_filename="server.log",
    )


@pytest.fixture
def repo_with_case():
    repo = MockCaseRepository()
    case = create_sample_case(user_id="user_owner")
    case.uploaded_files = [make_uploaded_file()]
    case.evidence = [make_evidence()]
    repo._storage[case.case_id] = case
    return repo, case


@pytest.fixture
def preprocessing_service():
    svc = MagicMock()
    svc.reclassify_evidence = AsyncMock(return_value=make_preprocessing_result())
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


class TestClarificationEmitterIntent:
    """The emitted suggestions must carry everything a client needs to
    resolve the choice structurally — no client-side special-casing."""

    def test_every_suggestion_carries_reclassification_intent(self):
        suggestions = _build_classification_clarification_suggestions(
            [_clarification_target()]
        )
        assert len(suggestions) == 3  # 2 typed + fallback
        for s in suggestions:
            assert s.type == "DECIDE"
            assert s.intent is not None
            assert s.intent["type"] == IntentType.FILE_RECLASSIFICATION.value
            assert s.intent["file_id"] == "file_aaaaaaaaaaaa"
            # Each intent round-trips through QueryIntent (the shape clients
            # replay as intent_type/intent_data on click).
            QueryIntent(**s.intent)
        assert [s.intent["data_type"] for s in suggestions] == [
            "logs_and_errors",
            "structured_config",
            "unstructured_text",
        ]

    def test_fallback_targets_unstructured_text(self):
        suggestions = _build_classification_clarification_suggestions(
            [_clarification_target(suggested_types=[])]
        )
        assert len(suggestions) == 1
        assert suggestions[0].label == "Something else"
        assert suggestions[0].intent["data_type"] == "unstructured_text"

    def test_no_failure_emits_nothing(self):
        ok = _PreprocessedAttachment(uploaded_file=make_uploaded_file())
        assert _build_classification_clarification_suggestions([ok]) == []

    def test_typed_choice_matches_via_intent_resolver_fast_path(self):
        """A user who *types* the label instead of clicking must resolve to
        the same structured intent (bounded choice matching)."""
        suggestions = _build_classification_clarification_suggestions(
            [_clarification_target()]
        )
        choices = [
            {
                "label": s.label,
                "action_type": s.type,
                "payload": s.payload,
                "body": s.body,
                "intent": s.intent,
            }
            for s in suggestions
        ]
        resolver = IntentResolver(MagicMock())
        matched = resolver._exact_match("Application logs", choices)
        assert matched is not None
        assert matched["type"] == IntentType.FILE_RECLASSIFICATION.value
        assert matched["data_type"] == "logs_and_errors"


class TestQueryIntentValidation:
    def test_file_reclassification_requires_fields(self):
        with pytest.raises(ValueError, match="file_id and data_type"):
            QueryIntent(type=IntentType.FILE_RECLASSIFICATION)

    def test_valid_intent_parses(self):
        qi = QueryIntent(
            type=IntentType.FILE_RECLASSIFICATION,
            file_id="file_aaaaaaaaaaaa",
            data_type="logs_and_errors",
        )
        assert qi.file_id == "file_aaaaaaaaaaaa"


class TestHandlerValidation:
    @pytest.mark.asyncio
    async def test_missing_fields_raise_validation(self, service, repo_with_case):
        _, case = repo_with_case
        with pytest.raises(ValidationException):
            await service._handle_file_reclassification(
                case=case, file_id=None, data_type_value=None
            )

    @pytest.mark.asyncio
    async def test_unknown_data_type_raises_validation(self, service, repo_with_case):
        _, case = repo_with_case
        with pytest.raises(ValidationException, match="Unknown data_type"):
            await service._handle_file_reclassification(
                case=case,
                file_id="file_aaaaaaaaaaaa",
                data_type_value="not_a_type",
            )

    @pytest.mark.asyncio
    async def test_unknown_file_raises_not_found(self, service, repo_with_case):
        _, case = repo_with_case
        with pytest.raises(NotFoundError):
            await service._handle_file_reclassification(
                case=case,
                file_id="file_zzzzzzzzzzzz",
                data_type_value="logs_and_errors",
            )

    @pytest.mark.asyncio
    async def test_file_without_storage_ref_raises_validation(
        self, service, repo_with_case
    ):
        _, case = repo_with_case
        case.uploaded_files = [make_uploaded_file(storage_ref=None)]
        with pytest.raises(ValidationException, match="no stored raw content"):
            await service._handle_file_reclassification(
                case=case,
                file_id="file_aaaaaaaaaaaa",
                data_type_value="logs_and_errors",
            )

    @pytest.mark.asyncio
    async def test_missing_blob_propagates_not_found(
        self, service, repo_with_case, file_storage
    ):
        """Storage blob gone (e.g. the replica that serves the click never
        had the file — the #27 secondary bug): NotFoundError must pass
        through process_turn unwrapped as 404, never a 5xx."""
        _, case = repo_with_case
        file_storage.retrieve_file = AsyncMock(
            side_effect=NotFoundError("File", "evidence/case_x/server.log")
        )
        payload = TurnPayload(
            query='Treat the previously uploaded file ("server.log") as application logs.',
            intent=QueryIntent(
                type=IntentType.FILE_RECLASSIFICATION,
                file_id="file_aaaaaaaaaaaa",
                data_type="logs_and_errors",
            ),
        )
        with pytest.raises(NotFoundError):
            await service.process_turn(
                case_id=case.case_id, user_id="user_owner", payload=payload
            )


class TestHandlerHappyPath:
    @pytest.mark.asyncio
    async def test_click_reclassifies_without_llm(
        self, service, repo_with_case, preprocessing_service
    ):
        """A clarification click routes to mechanical reclassification: the
        engine (LLM) is never invoked, the file's artifacts update, and any
        Evidence backed by the file is re-aligned."""
        repo, case = repo_with_case
        payload = TurnPayload(
            query='Treat the previously uploaded file ("server.log") as application logs.',
            intent=QueryIntent(
                type=IntentType.FILE_RECLASSIFICATION,
                file_id="file_aaaaaaaaaaaa",
                data_type="logs_and_errors",
            ),
        )

        response = await service.process_turn(
            case_id=case.case_id, user_id="user_owner", payload=payload
        )

        # No LLM turn: deterministic handler only.
        service.engine.process_turn.assert_not_called()
        preprocessing_service.reclassify_evidence.assert_awaited_once()
        kwargs = preprocessing_service.reclassify_evidence.call_args.kwargs
        assert kwargs["user_override"] == DataType.LOGS_AND_ERRORS

        saved = await repo.get(case.case_id)
        uf = next(f for f in saved.uploaded_files if f.file_id == "file_aaaaaaaaaaaa")
        assert uf.data_type == "logs"
        assert uf.summary == "new summary"
        assert uf.structural_index == "new index content"

        # Evidence backed by the file re-aligned; claim content untouched.
        ev = saved.evidence[0]
        assert ev.source_type == EvidenceSourceType.LOGS
        assert ev.summary == "Old summary"

        # User + agent messages persisted atomically like any other turn.
        assert saved.message_count == 2
        assert saved.messages[-2]["role"] == "user"
        assert saved.messages[-1]["role"] == "assistant"

        assert "server.log" in response.agent_response
        assert "application logs" in response.agent_response
        assert any(a.label == "Analyze it now" for a in response.suggested_actions)


class TestClarificationTurnWiring:
    """A classification_failed upload turn must (a) prepend the clarification
    suggestions to the response and (b) persist them in last_suggestions so a
    typed answer next turn resolves to the same intent."""

    @pytest.mark.asyncio
    async def test_last_suggestions_carry_clarification_intent(
        self, repo_with_case, preprocessing_service, file_storage
    ):
        repo, case = repo_with_case

        classify_result = MagicMock()
        classify_result.summary = "preview summary"
        classify_result.structural_index = "index"
        classify_result.data_type = UnifiedDataType.TEXT
        classify_result.detailed_data_type = DataType.UNSTRUCTURED_TEXT
        classify_result.content_hash = "b" * 64
        classify_result.extraction_method = "classification_failed"
        classify_result.extraction_metadata = {
            "suggested_types": ["logs_and_errors", "structured_config"]
        }
        classify_result.coverage_start_ts = None
        classify_result.coverage_end_ts = None
        preprocessing_service.classify_and_extract = AsyncMock(
            return_value=classify_result
        )
        file_storage.store_file = AsyncMock(
            return_value={"file_path": "evidence/case_x/blob.txt"}
        )
        file_storage.mark_linked = AsyncMock(return_value=True)

        service = InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )

        payload = TurnPayload(
            query="what is this file?",
            attachments=[
                Attachment(
                    content=b"ambiguous content",
                    filename="mystery.txt",
                    content_type="text/plain",
                )
            ],
        )
        response = await service.process_turn(
            case_id=case.case_id, user_id="user_owner", payload=payload
        )

        # Response leads with the clarification choices.
        intents = [a.intent for a in response.suggested_actions if a.intent]
        assert any(i["type"] == IntentType.FILE_RECLASSIFICATION.value for i in intents)

        # Persisted for the intent resolver (typed answers).
        saved = await repo.get(case.case_id)
        assert saved.last_suggestions, "clarification must persist for typed answers"
        stored_intents = [
            s["intent"] for s in saved.last_suggestions if s.get("intent")
        ]
        assert any(
            i["type"] == IntentType.FILE_RECLASSIFICATION.value for i in stored_intents
        )
        # The stored intent targets the file created this turn.
        target = next(
            i
            for i in stored_intents
            if i["type"] == IntentType.FILE_RECLASSIFICATION.value
        )
        assert any(f.file_id == target["file_id"] for f in saved.uploaded_files)


class TestTerminalCaseGuard:
    @pytest.mark.asyncio
    async def test_reclassification_refused_on_terminal_case(
        self, service, repo_with_case
    ):
        """A stale clarification click (or direct POST) on a resolved case
        must not rewrite its files/evidence. The other SERVICE intents
        inherit terminal protection by delegating to engine.process_turn;
        this handler never reaches the engine, so it must refuse on its
        own."""
        _, case = repo_with_case
        now = datetime.now(UTC)
        terminal = case.model_copy(
            update={
                "state": CaseState.RESOLVED,
                "resolved_at": now,
                "closed_at": now,
                "closure_reason": "resolved",
            }
        )
        with pytest.raises(ValidationException, match="closed case"):
            await service._handle_file_reclassification(
                case=terminal,
                file_id="file_aaaaaaaaaaaa",
                data_type_value="logs_and_errors",
            )
        # Refused before any re-extraction was attempted.
        service.preprocessing_service.reclassify_evidence.assert_not_called()


class TestSourceTypeMapExhaustiveness:
    @pytest.mark.parametrize("data_type", list(DataType))
    def test_every_data_type_is_mapped(self, data_type):
        """_infer_source_type silently defaults to TEXT on a lookup miss —
        the exact mechanism that hid the issue-27 misclassification (every
        upload landed as 'text'). Pin the map exhaustive over DataType so a
        new enum member cannot silently classify as text; the runtime
        default stays (a miss must not crash a turn), this pin moves the
        failure to CI."""
        assert data_type in _DATA_TYPE_TO_SOURCE_TYPE
