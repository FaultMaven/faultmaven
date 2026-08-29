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
    _classification_clarification_note,
    _PreprocessedAttachment,
)
from faultmaven.modules.case.domain.models import (
    CaseState,
    EvidenceSourceType,
    UploadedFile,
)

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
    upload_source: str = "file_upload",
    filename: str = "server.log",
    file_id: str = "file_aaaaaaaaaaaa",
) -> _PreprocessedAttachment:
    return _PreprocessedAttachment(
        uploaded_file=make_uploaded_file(
            file_id=file_id, filename=filename, upload_source=upload_source
        ),
        classification_failed=True,
        suggested_types=(
            suggested_types
            if suggested_types is not None
            else ["logs_and_errors", "structured_config"]
        ),
        attachment_filename=filename,
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

    def test_paste_offers_war_room_seeds_first(self):
        """Pasted text in an incident thread is usually command output or
        logs (product guidance); a paste that reached clarification has weak
        content signals by definition, so those choices lead — before the
        classifier's sub-threshold guesses — and every choice is actionable
        (DECIDE with a routable intent)."""
        suggestions = _build_classification_clarification_suggestions(
            [
                _clarification_target(
                    upload_source="text_paste",
                    filename="Untitled",
                    suggested_types=["documentation"],
                )
            ]
        )
        assert [s.intent["data_type"] for s in suggestions] == [
            "command_output",
            "logs_and_errors",
            "documentation",
            "unstructured_text",
        ]
        assert [s.label for s in suggestions] == [
            "Command output",
            "Application logs",
            "Documentation",
            "Something else",
        ]

    def test_paste_copy_never_names_the_synthetic_snippet(self):
        """'Untitled' / 'pasted-content-…' refer to the transport format,
        not anything the user recognizes — the copy says 'the text you
        pasted' instead."""
        for src, name in (
            ("text_paste", "Untitled"),
            ("paste", "Untitled"),
            ("file_upload", "pasted-content-20260713T083214.txt"),
        ):
            suggestions = _build_classification_clarification_suggestions(
                [_clarification_target(upload_source=src, filename=name)]
            )
            for s in suggestions:
                assert "the text you pasted" in s.payload
                assert name not in s.payload

    def test_paste_seeds_deduplicate_against_classifier_types(self):
        suggestions = _build_classification_clarification_suggestions(
            [
                _clarification_target(
                    upload_source="text_paste",
                    suggested_types=["command_output", "metrics_and_performance"],
                )
            ]
        )
        data_types = [s.intent["data_type"] for s in suggestions]
        assert data_types.count("command_output") == 1
        assert data_types == [
            "command_output",
            "logs_and_errors",
            "metrics_and_performance",
            "unstructured_text",
        ]

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


class TestEveryFailedAttachmentIsClarified:
    """#1222: the emitter clarified ``failed[0]`` only, reasoning from the
    per-turn file limit. That limit is on ``files`` ALONE — ``pasted_content``
    is a separate form field that legitimately rides alongside a file, and the
    paste/capture arm reaches ``classification_failed`` on its own. A turn
    where both fell below threshold left the second attachment with no
    choices, no intent, and no recovery path.
    """

    @staticmethod
    def _paste_and_file() -> list[_PreprocessedAttachment]:
        """The shipped paste+file turn, in the order the route builds it:
        the file attachment first, then ``pasted_content``."""
        return [
            _clarification_target(
                file_id="file_f11ef11ef11e",
                filename="mystery.txt",
                upload_source="file_upload",
                suggested_types=["documentation"],
            ),
            _clarification_target(
                file_id="file_0a570a570a57",
                filename="pasted-content-20260713T083214.txt",
                upload_source="text_paste",
                suggested_types=["documentation"],
            ),
        ]

    def test_both_attachments_get_choices_and_an_intent(self):
        suggestions = _build_classification_clarification_suggestions(
            self._paste_and_file()
        )
        by_file: dict[str, list] = {}
        for s in suggestions:
            assert s.type == "DECIDE"
            assert s.intent["type"] == IntentType.FILE_RECLASSIFICATION.value
            QueryIntent(**s.intent)
            by_file.setdefault(s.intent["file_id"], []).append(s)

        # Neither attachment is silently left without a recovery path.
        assert set(by_file) == {"file_f11ef11ef11e", "file_0a570a570a57"}
        # ...and each set ends in its own "Something else" escape hatch.
        for file_id, group in by_file.items():
            assert group[-1].intent["data_type"] == "unstructured_text"
            assert group[-1].label.startswith("Something else")

    def test_each_attachment_keeps_its_own_choice_budget(self):
        """Dedup and the 3-choice cap are per attachment: the file's choices
        must not consume the paste's, and a type offered for one must still
        be offered for the other."""
        suggestions = _build_classification_clarification_suggestions(
            self._paste_and_file()
        )
        by_file: dict[str, list[str]] = {}
        for s in suggestions:
            by_file.setdefault(s.intent["file_id"], []).append(s.intent["data_type"])

        # The file: its one classifier guess + the fallback.
        assert by_file["file_f11ef11ef11e"] == ["documentation", "unstructured_text"]
        # The paste: war-room seeds lead, then the classifier's guess (the
        # same `documentation` the file already used), then the fallback.
        assert by_file["file_0a570a570a57"] == [
            "command_output",
            "logs_and_errors",
            "documentation",
            "unstructured_text",
        ]

    def test_labels_disambiguate_so_a_typed_answer_lands_on_the_right_file(self):
        """Two cards reading "Documentation" are indistinguishable on screen,
        and ``IntentResolver._exact_match`` matches a typed label against the
        choices in order — it would resolve the paste's answer onto the file,
        turning a missing option into a wrong action."""
        suggestions = _build_classification_clarification_suggestions(
            self._paste_and_file()
        )
        labels = [s.label for s in suggestions]
        assert len(labels) == len(set(labels)), f"ambiguous labels: {labels}"
        assert "Documentation (mystery.txt)" in labels
        assert "Documentation (pasted text)" in labels

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
        matched = resolver._exact_match("Documentation (pasted text)", choices)
        assert matched is not None
        assert matched["file_id"] == "file_0a570a570a57"
        matched_file = resolver._exact_match("Documentation (mystery.txt)", choices)
        assert matched_file["file_id"] == "file_f11ef11ef11e"

    def test_qualifiers_are_unambiguous_because_one_synthetic_name_per_turn(self):
        """#1198: ``pasted_content`` is a single form field, so a turn carries
        one paste or one capture — never two — and everything else is a
        user-chosen filename. That is what makes naming both attachments in
        one clarification unambiguous."""
        results = self._paste_and_file()
        synthetic = [r for r in results if r.uploaded_file.has_synthetic_filename]
        assert len(synthetic) == 1
        assert {r.uploaded_file.display_name for r in results} == {
            "mystery.txt",
            "pasted text (turn 0)",
        }

        suggestions = _build_classification_clarification_suggestions(results)
        qualifiers = {s.label.split(" (", 1)[1].rstrip(")") for s in suggestions}
        assert qualifiers == {"mystery.txt", "pasted text"}

    def test_single_failure_is_byte_identical_to_before(self):
        """The common path — one attachment, one failure — must be untouched:
        bare labels, same payload, same body, same order."""
        suggestions = _build_classification_clarification_suggestions(
            [_clarification_target()]
        )
        assert [
            (s.label, s.payload, s.body, s.intent["data_type"]) for s in suggestions
        ] == [
            (
                "Application logs",
                'Treat the file you shared ("server.log") as application logs.',
                "Treat as application logs.",
                "logs_and_errors",
            ),
            (
                "Configuration",
                'Treat the file you shared ("server.log") as configuration.',
                "Treat as configuration.",
                "structured_config",
            ),
            (
                "Something else",
                'Treat the file you shared ("server.log") as unstructured text.',
                "Treat as unstructured text.",
                "unstructured_text",
            ),
        ]


class TestNarrationBridgeNamesEveryFailure:
    """The bridge used to ``next(...)`` the first failed attachment, so a
    paste+file turn offered choices for two things while naming one."""

    def test_one_failure_keeps_the_shipped_wording(self):
        note = _classification_clarification_note([_clarification_target()])
        assert note == (
            "\n\nOne more thing — I couldn't confidently classify the file "
            'you shared ("server.log"), so I haven\'t analyzed it yet. '
            "How should I treat it?"
        )

    def test_two_failures_name_both_and_go_plural(self):
        note = _classification_clarification_note(
            TestEveryFailedAttachmentIsClarified._paste_and_file()
        )
        assert note == (
            "\n\nOne more thing — I couldn't confidently classify the file "
            'you shared ("mystery.txt") or the text you pasted, so I haven\'t '
            "analyzed them yet. How should I treat them?"
        )

    def test_no_failure_emits_no_note(self):
        ok = _PreprocessedAttachment(uploaded_file=make_uploaded_file())
        assert _classification_clarification_note([ok]) is None


class TestPasteAndFileTurnEndToEnd:
    """The whole shape, through ``process_turn``: one file plus a paste, both
    below the classifier's threshold — the request shape the route explicitly
    permits (``maxItems: 1`` on ``files`` alone)."""

    @staticmethod
    def _failed_classification(content_hash: str):
        result = MagicMock()
        result.summary = "preview summary"
        result.structural_index = "index"
        result.data_type = UnifiedDataType.TEXT
        result.detailed_data_type = DataType.UNSTRUCTURED_TEXT
        result.content_hash = content_hash
        result.extraction_method = "classification_failed"
        result.extraction_metadata = {"suggested_types": ["documentation"]}
        result.coverage_start_ts = None
        result.coverage_end_ts = None
        return result

    @pytest.mark.asyncio
    async def test_both_attachments_are_recoverable(
        self, repo_with_case, preprocessing_service, file_storage
    ):
        repo, case = repo_with_case
        case.uploaded_files = []

        hashes = iter(["a" * 64, "b" * 64])
        preprocessing_service.classify_and_extract = AsyncMock(
            side_effect=lambda *a, **k: self._failed_classification(next(hashes))
        )
        blobs = iter(
            [
                {"file_path": "evidence/case_x/blob1.txt"},
                {"file_path": "evidence/case_x/blob2.txt"},
            ]
        )
        file_storage.store_file = AsyncMock(side_effect=lambda *a, **k: next(blobs))
        file_storage.mark_linked = AsyncMock(return_value=True)

        service = InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )

        payload = TurnPayload(
            query="here's the dump and what I pasted from the terminal",
            attachments=[
                # Route order: the file first, then `pasted_content`.
                Attachment(
                    content=b"ambiguous file bytes",
                    filename="mystery.txt",
                    content_type="text/plain",
                    source_metadata={"source_type": "file_upload"},
                ),
                Attachment(
                    content=b"NAME READY STATUS\nkube-proxy 1/1 Running",
                    filename="pasted-content-20260713T083214.txt",
                    content_type="text/plain",
                    source_metadata={"source_type": "text_paste"},
                ),
            ],
        )
        response = await service.process_turn(
            case_id=case.case_id, user_id="user_owner", payload=payload
        )

        saved = await repo.get(case.case_id)
        assert len(saved.uploaded_files) == 2
        every_file_id = {f.file_id for f in saved.uploaded_files}

        clarified = {
            a.intent["file_id"]
            for a in response.suggested_actions
            if a.intent
            and a.intent.get("type") == IntentType.FILE_RECLASSIFICATION.value
        }
        assert clarified == every_file_id, "an attachment was left unclarified"

        # The narration bridge names both, each in the user's own terms.
        assert '"mystery.txt"' in response.agent_response
        assert "the text you pasted" in response.agent_response
        assert "pasted-content-" not in response.agent_response
        assert "analyzed them yet" in response.agent_response

        # Persisted for the intent resolver, so a TYPED answer next turn
        # resolves for either attachment.
        stored = {
            s["intent"]["file_id"]
            for s in saved.last_suggestions
            if s.get("intent")
            and s["intent"].get("type") == IntentType.FILE_RECLASSIFICATION.value
        }
        assert stored == every_file_id

        # #1198 holds through the real pipeline: exactly one synthetic name.
        synthetic = [f for f in saved.uploaded_files if f.has_synthetic_filename]
        assert len(synthetic) == 1
        assert len({f.display_name for f in saved.uploaded_files}) == 2


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

        # Narration bridge: the reply must SAY why the choices are there —
        # the LLM's own text doesn't know about the classification failure,
        # and bare "Treat as documentation." bullets under an unrelated
        # investigation reply read as nonsense (observed on staging,
        # case_10c847556276).
        assert "couldn't confidently classify" in response.agent_response
        assert '"mystery.txt"' in response.agent_response
        # The note also lands in the persisted transcript.
        saved_for_note = await repo.get(case.case_id)
        assert "couldn't confidently classify" in saved_for_note.messages[-1]["content"]

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

    @pytest.mark.asyncio
    async def test_paste_turn_speaks_in_the_users_terms(
        self, repo_with_case, preprocessing_service, file_storage
    ):
        """A pasted snippet's synthetic name means nothing to the user: the
        bridge note and the choices must say 'the text you pasted', and the
        war-room seeds (command output / logs) must lead the choices
        (observed on staging, case_10c847556276: 'Untitled' + 'Documentation'
        read as nonsense)."""
        repo, case = repo_with_case

        classify_result = MagicMock()
        classify_result.summary = "preview summary"
        classify_result.structural_index = "index"
        classify_result.data_type = UnifiedDataType.TEXT
        classify_result.detailed_data_type = DataType.UNSTRUCTURED_TEXT
        classify_result.content_hash = "c" * 64
        classify_result.extraction_method = "classification_failed"
        classify_result.extraction_metadata = {"suggested_types": ["documentation"]}
        classify_result.coverage_start_ts = None
        classify_result.coverage_end_ts = None
        preprocessing_service.classify_and_extract = AsyncMock(
            return_value=classify_result
        )
        file_storage.store_file = AsyncMock(
            return_value={"file_path": "evidence/case_x/blob2.txt"}
        )
        file_storage.mark_linked = AsyncMock(return_value=True)

        service = InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )

        payload = TurnPayload(
            query="alert is firing, see paste",
            attachments=[
                Attachment(
                    content=b"NAME READY STATUS RESTARTS AGE\nkube-proxy 1/1 Running",
                    filename="pasted-content-20260713T083214.txt",
                    content_type="text/plain",
                    source_metadata={"source_type": "text_paste"},
                )
            ],
        )
        response = await service.process_turn(
            case_id=case.case_id, user_id="user_owner", payload=payload
        )

        assert "the text you pasted" in response.agent_response
        assert "pasted-content-" not in response.agent_response

        clar = [
            a
            for a in response.suggested_actions
            if a.intent
            and a.intent.get("type") == IntentType.FILE_RECLASSIFICATION.value
        ]
        assert [a.intent["data_type"] for a in clar] == [
            "command_output",
            "logs_and_errors",
            "documentation",
            "unstructured_text",
        ]
        for a in clar:
            assert "the text you pasted" in a.payload
            assert "pasted-content-" not in a.payload

        # #666: the attachment chip the Copilot renders for this very turn.
        # `file_id` is the handle the frontend references an attachment by,
        # so `filename` is display-only — and the minted name was going
        # straight back to the user on the primary channel.
        assert len(response.attachments_processed) == 1
        assert response.attachments_processed[0].filename == "pasted text (turn 1)"
        assert "pasted-content-" not in response.attachments_processed[0].filename
        assert response.attachments_processed[0].file_id


class TestPageCaptureClarification:
    """A capture whose classification falls below threshold reaches the
    clarification card BEFORE the page_capture passthrough runs, so the copy
    has to name it too — it read `the file you shared
    ("page-capture-…txt")` until the #1198 review caught it."""

    @pytest.mark.asyncio
    async def test_capture_turn_speaks_in_the_users_terms(
        self, repo_with_case, preprocessing_service, file_storage
    ):
        repo, case = repo_with_case

        classify_result = MagicMock()
        classify_result.summary = "preview summary"
        classify_result.structural_index = "index"
        classify_result.data_type = UnifiedDataType.TEXT
        classify_result.detailed_data_type = DataType.UNSTRUCTURED_TEXT
        classify_result.content_hash = "d" * 64
        classify_result.extraction_method = "classification_failed"
        classify_result.extraction_metadata = {"suggested_types": ["documentation"]}
        classify_result.coverage_start_ts = None
        classify_result.coverage_end_ts = None
        preprocessing_service.classify_and_extract = AsyncMock(
            return_value=classify_result
        )
        file_storage.store_file = AsyncMock(
            return_value={"file_path": "evidence/case_x/blob3.txt"}
        )
        file_storage.mark_linked = AsyncMock(return_value=True)

        service = InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )

        payload = TurnPayload(
            query="grabbed the runbook page, is this relevant?",
            attachments=[
                Attachment(
                    content=b"## Runbook\nStep 1: restart the pod\n",
                    filename="page-capture-20260713T083214.txt",
                    content_type="text/plain",
                    source_metadata={"source_type": "page_capture"},
                )
            ],
        )
        response = await service.process_turn(
            case_id=case.case_id, user_id="user_owner", payload=payload
        )

        assert "the page you captured" in response.agent_response
        assert "page-capture-" not in response.agent_response

        clar = [
            a
            for a in response.suggested_actions
            if a.intent
            and a.intent.get("type") == IntentType.FILE_RECLASSIFICATION.value
        ]
        assert clar, "expected clarification suggestions for a failed capture"
        for a in clar:
            assert "the page you captured" in a.payload
            assert "page-capture-" not in a.payload

        # A capture is NOT seeded with the war-room paste priors (command
        # output / logs) — that prior is about pasted terminal text.
        assert [a.intent["data_type"] for a in clar][:1] != ["command_output"]

        assert response.attachments_processed[0].filename == "captured page (turn 1)"


class TestDedupHitChipNamesTheSubmittedFile:
    """Content-hash dedup matches on the hash ALONE. The chip the Copilot
    renders describes what the user JUST submitted, so it must carry the
    name they just gave — not the name on the row dedup happened to return
    (#1198 review)."""

    @pytest.mark.asyncio
    async def test_renamed_reupload_reports_the_new_name(
        self, repo_with_case, preprocessing_service, file_storage
    ):
        repo, case = repo_with_case

        # The row already on the case, submitted earlier under the OLD name.
        stored = UploadedFile(
            file_id="file_bbbbbbbbbbbb",
            filename="nginx-2026-07-09.log",
            size_bytes=64,
            content_type="text/plain",
            content_hash="e" * 64,
            uploaded_at_turn=1,
            uploaded_by="user_owner",
            upload_source="file_upload",
            storage_ref="evidence/case_x/old.log",
            data_type="logs",
            summary="nginx 502s",
            structural_index="ERROR upstream timed out",
        )
        case.uploaded_files.append(stored)
        repo.find_uploaded_file_by_content_hash = AsyncMock(return_value=stored)

        classify_result = make_preprocessing_result()
        classify_result.content_hash = "e" * 64
        preprocessing_service.classify_and_extract = AsyncMock(
            return_value=classify_result
        )

        service = InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )

        payload = TurnPayload(
            query="same logs again",
            attachments=[
                Attachment(
                    content=b"ERROR upstream timed out\n",
                    # The user named it differently THIS time.
                    filename="nginx-2026-07-10.log",
                    content_type="text/plain",
                    source_metadata={"source_type": "file_upload"},
                )
            ],
        )
        response = await service.process_turn(
            case_id=case.case_id, user_id="user_owner", payload=payload
        )

        chip = response.attachments_processed[0]
        # Dedup did fire — this is the path under test.
        assert chip.processing_status == "duplicate"
        assert chip.duplicate_of == "file_bbbbbbbbbbbb"
        # ...and the chip names what the user sent, not what dedup returned.
        assert chip.filename == "nginx-2026-07-10.log"
        assert chip.filename != stored.filename


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
