"""FILE_RECLASSIFICATION intent — structured resolution for classification_failed.

The cross-client resolution contract for a ``classification_failed`` upload
(faultmaven-slack-agent#27): the clarification DECIDE suggestions carry an
engine-owned ``file_reclassification`` intent (file_id + target DataType);
clients forward the intent on click (or the intent resolver matches a typed
choice), and the SERVICE handler re-runs preprocessing mechanically — no LLM
call, so the choice can never be misread as an analysis request.
"""

import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.intent_resolver import IntentResolver
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.core.investigation.suggestion_liveness import (
    CLARIFICATION_CARRY_TURNS,
    CLARIFICATION_SPAN_CAP,
    entry_file_id,
    live_suggestions,
)
from faultmaven.core.preprocessing.models import UnifiedDataType
from faultmaven.exceptions import NotFoundError, ValidationException
from faultmaven.models.api import DataType
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    _DATA_TYPE_TO_SOURCE_TYPE,
    InvestigationService,
    _admit_clarification_entries,
    _build_classification_clarification,
    _carry_forward_unresolved_clarifications,
    _PreprocessedAttachment,
    _sanitize_label_fragment,
)
from faultmaven.modules.case.domain.models import (
    CaseState,
    EvidenceSourceType,
    TurnOutcome,
    TurnProgress,
    UploadedFile,
)

from .conftest import (
    MockCaseRepository,
    MockMilestoneEngine,
    RecordingCaseRepository,
    RecordingMilestoneEngine,
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


def _clarification_suggestions(results):
    """The choices half of the single derivation."""
    return _build_classification_clarification(results)[0]


def _clarification_note(results):
    """The narration half of the same derivation."""
    return _build_classification_clarification(results)[1]


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
        suggestions = _clarification_suggestions([_clarification_target()])
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
        suggestions = _clarification_suggestions(
            [_clarification_target(suggested_types=[])]
        )
        assert len(suggestions) == 1
        assert suggestions[0].label == "Something else (server.log)"
        assert suggestions[0].intent["data_type"] == "unstructured_text"

    def test_no_failure_emits_nothing(self):
        ok = _PreprocessedAttachment(uploaded_file=make_uploaded_file())
        assert _clarification_suggestions([ok]) == []

    def test_paste_offers_war_room_seeds_first(self):
        """Pasted text in an incident thread is usually command output or
        logs (product guidance); a paste that reached clarification has weak
        content signals by definition, so those choices lead — before the
        classifier's sub-threshold guesses — and every choice is actionable
        (DECIDE with a routable intent)."""
        suggestions = _clarification_suggestions(
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
            "Command output (pasted text)",
            "Application logs (pasted text)",
            "Documentation (pasted text)",
            "Something else (pasted text)",
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
            suggestions = _clarification_suggestions(
                [_clarification_target(upload_source=src, filename=name)]
            )
            for s in suggestions:
                assert "the text you pasted" in s.payload
                assert name not in s.payload

    def test_paste_seeds_deduplicate_against_classifier_types(self):
        suggestions = _clarification_suggestions(
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
        suggestions = _clarification_suggestions([_clarification_target()])
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
        matched = resolver._exact_match("Application logs (server.log)", choices)
        assert matched is not None
        assert matched["type"] == IntentType.FILE_RECLASSIFICATION.value
        assert matched["data_type"] == "logs_and_errors"

        # The payload is the other matchable channel, and the one the matcher
        # tries FIRST — it is the text a click sends, so it is the text a user
        # retypes. Round one qualified only the label and left this untested,
        # which is how two pastes came to share every payload they offered.
        by_payload = resolver._exact_match(
            'Treat the file you shared ("server.log") as application logs.', choices
        )
        assert by_payload == matched


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
        suggestions = _clarification_suggestions(self._paste_and_file())
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
        suggestions = _clarification_suggestions(self._paste_and_file())
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
        suggestions = _clarification_suggestions(self._paste_and_file())
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

        # Exact strings, not a reconstructed qualifier: an `in`-style check
        # passes vacuously on an empty list and a split() mis-parses a
        # filename containing parentheses.
        assert [s.label for s in _clarification_suggestions(results)] == [
            "Documentation (mystery.txt)",
            "Something else (mystery.txt)",
            "Command output (pasted text)",
            "Application logs (pasted text)",
            "Documentation (pasted text)",
            "Something else (pasted text)",
        ]

    def test_single_failure_names_its_subject_like_every_other_card(self):
        """The common path — one attachment, one failure — now carries the
        qualifier too. #1236 kept it bare and #1245 round one kept that,
        because the qualifier was understood as a way of separating cards
        from EACH OTHER, and a lone card has nothing to be separated from.

        What that missed is the bare label as a standing GENERIC. A question
        outlives its turn now, so "Documentation" minted on a lone turn 1 is
        still matchable on turn 5, and a user typing that shorthand while
        looking at turn 5's qualified cards resolved onto turn 1's file —
        oldest-wins, against every other ordering rule in this seam. A label
        that always names its subject has no generic form to be captured by.

        Payload, body and order are unchanged.
        """
        suggestions = _clarification_suggestions([_clarification_target()])
        assert [
            (s.label, s.payload, s.body, s.intent["data_type"]) for s in suggestions
        ] == [
            (
                "Application logs (server.log)",
                'Treat the file you shared ("server.log") as application logs.',
                "Treat as application logs.",
                "logs_and_errors",
            ),
            (
                "Configuration (server.log)",
                'Treat the file you shared ("server.log") as configuration.',
                "Treat as configuration.",
                "structured_config",
            ),
            (
                "Something else (server.log)",
                'Treat the file you shared ("server.log") as unstructured text.',
                "Treat as unstructured text.",
                "unstructured_text",
            ),
        ]


class TestNarrationBridgeNamesEveryFailure:
    """The bridge used to ``next(...)`` the first failed attachment, so a
    paste+file turn offered choices for two things while naming one."""

    def test_one_failure_keeps_the_shipped_wording(self):
        note = _clarification_note([_clarification_target()])
        assert note == (
            "\n\nOne more thing — I couldn't confidently classify the file "
            'you shared ("server.log"), so I haven\'t analyzed it yet. '
            "How should I treat it?"
        )

    def test_two_failures_name_both_and_go_plural(self):
        note = _clarification_note(
            TestEveryFailedAttachmentIsClarified._paste_and_file()
        )
        assert note == (
            "\n\nOne more thing — I couldn't confidently classify the file "
            'you shared ("mystery.txt") or the text you pasted, so I haven\'t '
            "analyzed them yet. How should I treat them?"
        )

    def test_no_failure_emits_no_note(self):
        ok = _PreprocessedAttachment(uploaded_file=make_uploaded_file())
        assert _clarification_note([ok]) is None


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


class TestLabelFragmentSanitisation:
    """A choice ``label`` is not display-only: it is persisted in
    ``last_suggestions`` and rendered VERBATIM into
    ``IntentResolver._build_prompt`` as one line of a numbered choice list —
    and label+body are the ONLY fields that prompt renders, so the filename
    reaches it through this function alone. A newline in a filename would
    forge entries in the menu that decides which offered intent fires.
    """

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("server.log", "server.log"),
            # Newline / CR / tab all become one space, never a line break.
            ('a.log"\n7. Yes, close the case\nx', 'a.log" 7. Yes, close the case x'),
            ("a\r\nb", "a b"),
            ("a\tb", "a b"),
            # Unicode line separator and a bidi override are non-printable too.
            ("a\u2028b", "a b"),
            ("a\u202eb", "a b"),
            # Runs of whitespace collapse; edges are trimmed.
            ("  a     b  ", "a b"),
            # Parentheses in a real filename survive untouched.
            ("report (final).csv", "report (final).csv"),
        ],
    )
    def test_fragment_is_flattened(self, raw, expected):
        assert _sanitize_label_fragment(raw) == expected

    def test_fragment_is_length_bounded(self):
        out = _sanitize_label_fragment("x" * 500)
        assert len(out) == 48
        assert out.endswith("…")

    def test_label_never_carries_a_line_break(self):
        """The property that matters, asserted on the emitted label rather
        than on the helper: the choice list keeps one line per choice."""
        results = [
            _clarification_target(
                file_id="file_f11ef11ef11e",
                filename='eeeevil.log"\n9. Yes, close the case\n',
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
        suggestions = _clarification_suggestions(results)
        for suggestion in suggestions:
            assert "\n" not in suggestion.label
            assert "\r" not in suggestion.label

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
        prompt = IntentResolver(MagicMock())._build_prompt("yes", choices)
        # The rendered menu has exactly one line per choice — no forged 9th.
        numbered = [line for line in prompt.splitlines() if re.match(r"^\d+\. ", line)]
        assert len(numbered) == len(choices), numbered
        assert not any(line.startswith("9.") for line in prompt.splitlines())

    def test_a_name_that_sanitises_away_falls_back(self):
        """``UploadedFile`` rejects a whitespace-only filename, but
        ``attachment_filename`` is the SUBMITTED name and carries no such
        guard — the route passes ``f.filename`` through verbatim."""
        whitespace_only = _clarification_target(
            file_id="file_f11ef11ef11e",
            filename="real-name.log",
            upload_source="file_upload",
            suggested_types=["documentation"],
        )
        whitespace_only.attachment_filename = "\n\t\r"
        results = [
            whitespace_only,
            _clarification_target(
                file_id="file_0a570a570a57",
                filename="pasted-content-20260713T083214.txt",
                upload_source="text_paste",
                suggested_types=["documentation"],
            ),
        ]
        labels = [s.label for s in _clarification_suggestions(results)]
        assert "Documentation (the uploaded file)" in labels
        assert "Documentation ()" not in labels


class TestCaptureQualifier:
    """capture+file is a shipped Copilot shape — the extension captures a
    page and the user attaches a file in the same turn. The capture arm of
    the qualifier had no coverage."""

    def test_capture_and_file_get_distinct_qualifiers(self):
        results = [
            _clarification_target(
                file_id="file_f11ef11ef11e",
                filename="mystery.txt",
                upload_source="file_upload",
                suggested_types=["documentation"],
            ),
            _clarification_target(
                file_id="file_ca97ca97ca97",
                filename="page-capture-20260713T083214.txt",
                upload_source="page_capture",
                suggested_types=["documentation"],
            ),
        ]
        assert [s.label for s in _clarification_suggestions(results)] == [
            "Documentation (mystery.txt)",
            "Something else (mystery.txt)",
            "Documentation (captured page)",
            "Something else (captured page)",
        ]
        # A capture is NOT seeded with the war-room paste priors.
        assert [
            s.intent["data_type"]
            for s in _clarification_suggestions(results)
            if s.intent["file_id"] == "file_ca97ca97ca97"
        ] == ["documentation", "unstructured_text"]
        # ...and the note names it the way the user knows it.
        note = _clarification_note(results)
        assert "the page you captured" in note
        assert "page-capture-" not in note


class TestNoteAndChoicesCannotDisagree:
    """Both halves come from one filter pass, so 'the note names exactly the
    attachments the choices target' is structural rather than prose."""

    def test_mixed_turn_names_only_the_failure(self):
        ok = _PreprocessedAttachment(
            uploaded_file=make_uploaded_file(
                file_id="file_0000000000aa", filename="clean.log"
            )
        )
        failed = _clarification_target(
            file_id="file_f11ef11ef11e", filename="mystery.txt"
        )
        suggestions, note = _build_classification_clarification([ok, failed])

        assert {s.intent["file_id"] for s in suggestions} == {"file_f11ef11ef11e"}
        assert '"mystery.txt"' in note
        assert "clean.log" not in note
        # The note is singular (one failure); the labels still name their
        # subject, which they now do unconditionally.
        assert [s.label for s in suggestions] == [
            "Application logs (mystery.txt)",
            "Configuration (mystery.txt)",
            "Something else (mystery.txt)",
        ]
        assert "analyzed it yet" in note


class TestRecoveryLoopSurvivesResolvingOneAttachment:
    """#1222's emitter hands out a recovery path per failed attachment; the
    system has to let the user walk ALL of them.

    ``last_suggestions`` is rebuilt every turn, and a reclassification turn
    carries no attachment so it builds no clarification of its own. The list
    therefore collapsed to ``None`` — which cost nothing when there was one
    failed attachment (its only question had just been answered) and deleted
    the second attachment's four choices once there were two.
    """

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

    @staticmethod
    def _clarified_file_ids(suggestions):
        return sorted(
            {
                s["intent"]["file_id"]
                for s in (suggestions or [])
                if s.get("intent", {}).get("type")
                == IntentType.FILE_RECLASSIFICATION.value
            }
        )

    @pytest.mark.asyncio
    async def test_turn1_both_fail_turn2_resolve_a_turn3_resolve_b(
        self, repo_with_case, preprocessing_service, file_storage
    ):
        repo, case = repo_with_case
        case.uploaded_files = []
        case.evidence = []

        hashes = iter(["a" * 64, "b" * 64])
        preprocessing_service.classify_and_extract = AsyncMock(
            side_effect=lambda *a, **k: self._failed_classification(next(hashes))
        )
        blobs = iter(
            [{"storage_key": f"evidence/case_x/blob{i}.txt"} for i in range(4)]
        )
        file_storage.store_file = AsyncMock(side_effect=lambda *a, **k: next(blobs))
        file_storage.mark_linked = AsyncMock(return_value=True)

        service = InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )

        # ---- turn 1: one file + one paste, both below threshold
        await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query="the dump plus what I pasted",
                attachments=[
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
            ),
        )
        saved = await repo.get(case.case_id)
        file_a = next(
            f.file_id for f in saved.uploaded_files if f.filename == "mystery.txt"
        )
        file_b = next(
            f.file_id for f in saved.uploaded_files if f.filename != "mystery.txt"
        )
        assert self._clarified_file_ids(saved.last_suggestions) == sorted(
            [file_a, file_b]
        )

        # ---- turn 2: resolve A. B's question is still open.
        await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query='Treat the file you shared ("mystery.txt") as application logs.',
                intent=QueryIntent(
                    type=IntentType.FILE_RECLASSIFICATION,
                    file_id=file_a,
                    data_type="logs_and_errors",
                ),
            ),
        )
        saved = await repo.get(case.case_id)
        assert self._clarified_file_ids(saved.last_suggestions) == [
            file_b
        ], "resolving one attachment must not delete the other's recovery path"
        # A is answered, so it is not re-offered.
        assert file_a not in self._clarified_file_ids(saved.last_suggestions)

        # ---- turn 3: B is still resolvable through the intent path, both
        #      by typing (bounded choice matching) and by clicking.
        resolver = IntentResolver(MagicMock())
        typed = resolver._exact_match(
            "Documentation (pasted text)",
            [s for s in saved.last_suggestions if s.get("intent")],
        )
        assert typed is not None
        assert typed["file_id"] == file_b
        assert typed["type"] == IntentType.FILE_RECLASSIFICATION.value

        response = await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query="Treat the text you pasted as command output.",
                intent=QueryIntent(**typed | {"data_type": "command_output"}),
            ),
        )
        assert "Got it" in response.agent_response

        # Both attachments resolved: nothing is left pending.
        saved = await repo.get(case.case_id)
        assert self._clarified_file_ids(saved.last_suggestions) == []

    @pytest.mark.asyncio
    async def test_single_failure_still_clears_on_resolution(
        self, repo_with_case, preprocessing_service, file_storage
    ):
        """The common path is unchanged: one failed attachment, resolved,
        leaves nothing pending — the carry-forward must not resurrect the
        question the user just answered."""
        repo, case = repo_with_case
        case.uploaded_files = []
        case.evidence = []

        preprocessing_service.classify_and_extract = AsyncMock(
            return_value=self._failed_classification("c" * 64)
        )
        file_storage.store_file = AsyncMock(
            return_value={"storage_key": "evidence/case_x/only.txt"}
        )
        file_storage.mark_linked = AsyncMock(return_value=True)

        service = InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )
        await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query="what is this?",
                attachments=[
                    Attachment(
                        content=b"ambiguous",
                        filename="mystery.txt",
                        content_type="text/plain",
                        source_metadata={"source_type": "file_upload"},
                    )
                ],
            ),
        )
        saved = await repo.get(case.case_id)
        only = saved.uploaded_files[0].file_id
        assert self._clarified_file_ids(saved.last_suggestions) == [only]

        await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query='Treat the file you shared ("mystery.txt") as application logs.',
                intent=QueryIntent(
                    type=IntentType.FILE_RECLASSIFICATION,
                    file_id=only,
                    data_type="logs_and_errors",
                ),
            ),
        )
        saved = await repo.get(case.case_id)
        assert self._clarified_file_ids(saved.last_suggestions) == []


class TestRecoveryLoopSurvivesAnIgnoredQuestion:
    """#1245: the user who does not answer the clarification.

    The commoner shape by far, and the one #1222 left open: a reply that is
    neither a choice nor a reclassification intent rebuilt
    ``last_suggestions`` from that turn's own output, so the standing
    question was gone by the time the user came back to it. The attachment
    stayed misclassified with no server-side recovery path.
    """

    _failed_classification = staticmethod(
        TestRecoveryLoopSurvivesResolvingOneAttachment._failed_classification
    )
    _clarified_file_ids = staticmethod(
        TestRecoveryLoopSurvivesResolvingOneAttachment._clarified_file_ids
    )

    @pytest.fixture
    def wired(self, repo_with_case, preprocessing_service, file_storage):
        repo, case = repo_with_case
        case.uploaded_files = []
        case.evidence = []
        preprocessing_service.classify_and_extract = AsyncMock(
            return_value=self._failed_classification("d" * 64)
        )
        file_storage.store_file = AsyncMock(
            return_value={"storage_key": "evidence/case_x/mystery.txt"}
        )
        file_storage.mark_linked = AsyncMock(return_value=True)
        service = InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )
        return service, repo, case

    @staticmethod
    async def _upload_that_fails(service, case):
        await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query="what is this?",
                attachments=[
                    Attachment(
                        content=b"ambiguous",
                        filename="mystery.txt",
                        content_type="text/plain",
                        source_metadata={"source_type": "file_upload"},
                    )
                ],
            ),
        )

    @staticmethod
    async def _unrelated(service, case, text):
        await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(query=text),
        )

    @pytest.mark.asyncio
    async def test_a_diverted_user_can_still_answer_two_turns_later(
        self, wired, preprocessing_service
    ):
        service, repo, case = wired

        # ---- turn 1: the upload fails classification, choices are offered.
        await self._upload_that_fails(service, case)
        saved = await repo.get(case.case_id)
        only = saved.uploaded_files[0].file_id
        assert self._clarified_file_ids(saved.last_suggestions) == [only]
        answer = next(
            s["payload"]
            for s in saved.last_suggestions
            if s["intent"]["data_type"] == "documentation"
        )

        # ---- turns 2 and 3: the user ignores the question entirely.
        await self._unrelated(service, case, "is the connection pool maxed out?")
        saved = await repo.get(case.case_id)
        assert self._clarified_file_ids(saved.last_suggestions) == [
            only
        ], "an ignored question must not delete the recovery path"

        await self._unrelated(service, case, "what about replication lag?")
        saved = await repo.get(case.case_id)
        assert self._clarified_file_ids(saved.last_suggestions) == [only]

        # ---- turn 4: they come back to it and type the choice.
        response = await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(query=answer),
        )
        assert "Got it" in response.agent_response
        preprocessing_service.reclassify_evidence.assert_awaited_once()
        assert (
            preprocessing_service.reclassify_evidence.call_args.kwargs["user_override"]
            == DataType.DOCUMENTATION
        )

        # Answered: nothing is left pending.
        saved = await repo.get(case.case_id)
        assert self._clarified_file_ids(saved.last_suggestions) == []

    @pytest.mark.asyncio
    async def test_the_question_is_off_the_menu_once_the_window_closes(
        self, wired, preprocessing_service
    ):
        """The bound has to BITE, or #1245's fix is just unbounded growth
        with a nicer docstring: a question nobody answered stops being
        answerable, and the typed reply flows on as an ordinary turn.

        Three diverting turns is a LITERAL, for the reason spelled out on
        ``test_the_question_expires_at_the_end_of_its_window``.
        """
        assert CLARIFICATION_CARRY_TURNS == 3
        service, repo, case = wired

        await self._upload_that_fails(service, case)
        saved = await repo.get(case.case_id)
        answer = next(
            s["payload"]
            for s in saved.last_suggestions
            if s["intent"]["data_type"] == "documentation"
        )

        for n in range(3):
            await self._unrelated(service, case, f"unrelated question {n}")
        saved = await repo.get(case.case_id)
        assert saved.last_suggestions is None

        response = await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(query=answer),
        )
        assert "Got it" not in response.agent_response
        preprocessing_service.reclassify_evidence.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_two_single_failure_turns_do_not_mint_the_same_wording_twice(
        self, wired
    ):
        """The emitter's disambiguation premise was "more than one attachment
        failed THIS TURN". Once a question outlives its turn that is the
        wrong question: two single-failure turns would each mint a bare
        "Documentation" for a different file, and ``_exact_match`` returns
        the first — resolving the newer answer onto the older file.

        Checked on BOTH matchable channels. Round one checked labels only and
        reported the set unambiguous while every payload collided underneath
        it, which is precisely the bug the check existed to catch — a probe
        aimed at the wrong channel is worse than no probe, because its green
        is evidence of nothing.
        """
        service, repo, case = wired

        await self._upload_that_fails(service, case)
        await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query="here is another one",
                attachments=[
                    Attachment(
                        content=b"also ambiguous",
                        filename="second.txt",
                        content_type="text/plain",
                        source_metadata={"source_type": "file_upload"},
                    )
                ],
            ),
        )

        saved = await repo.get(case.case_id)
        assert len(self._clarified_file_ids(saved.last_suggestions)) == 2
        for channel in ("label", "payload"):
            seen = [s[channel] for s in saved.last_suggestions]
            assert len(seen) == len(set(seen)), f"ambiguous {channel}s: {seen}"

        resolver = IntentResolver(MagicMock())
        first, second = (
            next(f.file_id for f in saved.uploaded_files if f.filename == name)
            for name in ("mystery.txt", "second.txt")
        )
        assert (
            resolver._exact_match("Documentation (second.txt)", saved.last_suggestions)[
                "file_id"
            ]
            == second
        )
        assert (
            resolver._exact_match(
                'Treat the file you shared ("mystery.txt") as documentation or notes.',
                saved.last_suggestions,
            )["file_id"]
            == first
        )

    @pytest.mark.asyncio
    async def test_two_pastes_cannot_both_be_on_offer(
        self, repo_with_case, preprocessing_service, file_storage
    ):
        """The qualifier's uniqueness rested on "a turn mints at most ONE
        synthetic name" (#1198) — true within a turn, and no longer the
        question once a question outlives its turn. Two pastes are both "the
        text you pasted" in every payload and "pasted text" in every label:
        the wording is OURS by design, the minted filename is a transport
        name the user never saw, and the turn number is not unique (#1264).
        Nothing available can separate them.

        So they are not both offered. Round one qualified the label with the
        turn and called it solved; the payloads still collided, and the turn
        was not an identity anyway. Admission refuses the older one instead —
        which is exactly where it was before #1245, so nothing regresses, and
        the set the resolver sees is unambiguous by construction rather than
        by an argument about wording.
        """
        repo, case = repo_with_case
        case.uploaded_files = []
        case.evidence = []
        hashes = iter(["e" * 64, "f" * 64])
        preprocessing_service.classify_and_extract = AsyncMock(
            side_effect=lambda *a, **k: self._failed_classification(next(hashes))
        )
        blobs = iter([{"storage_key": f"evidence/case_x/p{i}.txt"} for i in range(2)])
        file_storage.store_file = AsyncMock(side_effect=lambda *a, **k: next(blobs))
        file_storage.mark_linked = AsyncMock(return_value=True)
        service = InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )

        for turn in (1, 2):
            await service.process_turn(
                case_id=case.case_id,
                user_id="user_owner",
                payload=TurnPayload(
                    query=f"paste {turn}",
                    attachments=[
                        Attachment(
                            content=f"NAME READY\nsvc-{turn} 1/1".encode(),
                            filename=f"pasted-content-2026071{turn}T083214.txt",
                            content_type="text/plain",
                            source_metadata={"source_type": "text_paste"},
                        )
                    ],
                ),
            )

        saved = await repo.get(case.case_id)
        first, second = (f.file_id for f in saved.uploaded_files)
        assert self._clarified_file_ids(saved.last_suggestions) == [second], (
            "the older paste is indistinguishable from the newer, so it must "
            "not be on offer beside it"
        )
        for channel in ("label", "payload"):
            seen = [s[channel] for s in saved.last_suggestions]
            assert len(seen) == len(set(seen)), f"ambiguous {channel}s: {seen}"

        # The wording both pastes share resolves to the one attachment that
        # is on offer — the newest, which is the card the user just saw.
        resolver = IntentResolver(MagicMock())
        assert (
            resolver._exact_match(
                "Treat the text you pasted as documentation or notes.",
                saved.last_suggestions,
            )["file_id"]
            == second
        )
        assert first != second


def _stored_entry(
    file_id,
    *,
    offered_turn=1,
    offered_data_type=None,
    data_type="documentation",
    intent_type=None,
    label=None,
):
    """A ``last_suggestions`` entry in the shape the write site persists."""
    return {
        "label": label if label is not None else f"Documentation ({file_id})",
        "action_type": "DECIDE",
        "payload": f"Treat {file_id} as documentation.",
        "body": "Treat as documentation.",
        "intent": {
            "type": intent_type or IntentType.FILE_RECLASSIFICATION.value,
            "file_id": file_id,
            "data_type": data_type,
        },
        "offered_turn": offered_turn,
        "offered_data_type": offered_data_type,
    }


def _case_holding(*file_ids, current_turn=1, state=CaseState.INQUIRY, **kw):
    """A case whose ``uploaded_files`` are exactly ``file_ids``.

    The referent check reads ``UploadedFile.data_type``, so a case that does
    not hold the file makes every clarification for it dead — which is why
    every carry-forward test has to build one.
    """
    case = create_sample_case(current_turn=current_turn, state=state, **kw)
    case.uploaded_files = [make_uploaded_file(file_id=f) for f in file_ids]
    case.evidence = []
    return case


class TestAClarificationClickCostsAWindowTurn:
    """The one deliberate behaviour change in #1264, pinned.

    ``CLARIFICATION_CARRY_TURNS = 3`` bounds how long an unanswered
    classification question stays answerable. The window is measured on the
    PERSISTED clock, and before #1264 a route that did not reach the engine — a
    greeting, a clarification click, a terminal answer — consumed a turn number
    without recording one, so the persisted clock stood still across it. The
    effective reach was "3 engine turns, plus unlimited non-engine turns". It is
    now 3 turns of any kind.

    That is the constant meaning what it says. But it IS a behaviour change to
    #1263's recovery path, and the product question — should a turn spent
    elsewhere cost a question its window? — belongs to that lane's owner, not to
    a turn-accounting fix. If the old reach is wanted, the lever is
    ``CLARIFICATION_CARRY_TURNS``; the turn clock is not the place to buy it
    back.

    The two tests below differ by exactly one non-engine turn, so the delta
    between them IS the semantics. Written after review caught that the
    docstrings in ``suggestion_liveness`` and above
    ``test_a_clarification_click_does_not_over_age_the_other_question`` named a
    pinning test that did not exist: the change was documented, intended and
    unprotected.
    """

    _clarified_file_ids = staticmethod(
        TestRecoveryLoopSurvivesResolvingOneAttachment._clarified_file_ids
    )
    _upload_that_fails = staticmethod(
        TestRecoveryLoopSurvivesAnIgnoredQuestion._upload_that_fails
    )
    _unrelated = staticmethod(TestRecoveryLoopSurvivesAnIgnoredQuestion._unrelated)
    _failed_classification = staticmethod(
        TestRecoveryLoopSurvivesAnIgnoredQuestion._failed_classification
    )

    @pytest.fixture
    def wired(self, preprocessing_service, file_storage):
        """BOTH recording doubles, and neither is optional.

        ``MockCaseRepository`` stores the ``Case`` as handed to it, so the
        ``effective_current_turn`` projection the real repositories apply never
        happens. ``MockMilestoneEngine`` records no turn, so ``turn_history``
        stays empty and ``effective_current_turn`` falls back to
        ``current_turn`` — which makes the projection a no-op even when it IS
        applied. Either double alone leaves the two counters unable to diverge,
        and a window test built on them passes whether or not the turn clock
        works. Verified: with the plain pair, removing the backstop entirely
        left both tests below green.
        """
        repo = RecordingCaseRepository()
        case = create_sample_case(user_id="user_owner")
        case.uploaded_files = []
        case.evidence = []
        repo._storage[case.case_id] = case
        preprocessing_service.classify_and_extract = AsyncMock(
            return_value=self._failed_classification("d" * 64)
        )
        file_storage.store_file = AsyncMock(
            return_value={"storage_key": "evidence/case_x/mystery.txt"}
        )
        file_storage.mark_linked = AsyncMock(return_value=True)
        service = InvestigationService(
            milestone_engine=RecordingMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )
        return service, repo, case

    @pytest.mark.asyncio
    async def test_the_question_survives_three_engine_turns(
        self, wired, preprocessing_service
    ):
        """The control. Offered on turn 1, read on turn 4: age 3, the last turn
        in window."""
        assert CLARIFICATION_CARRY_TURNS == 3
        service, repo, case = wired

        await self._upload_that_fails(service, case)
        for n in range(2):
            await self._unrelated(service, case, f"unrelated question {n}")

        saved = await repo.get(case.case_id)
        assert saved.current_turn == 3
        assert self._clarified_file_ids(saved.last_suggestions), (
            "the control must leave the question live, or the comparison below "
            "shows nothing"
        )

    @pytest.mark.asyncio
    async def test_a_greeting_in_the_middle_spends_one_of_those_turns(
        self, wired, preprocessing_service
    ):
        """Same three turns after the offer, but one of them is a GREETING —
        answered in the service, never reaching the engine.

        Before #1264 that turn recorded nothing, the persisted clock stood still
        across it, and the question was read at age 3 and survived. Now the turn
        counts, the question is read at age 4, and it is gone. One non-engine
        turn is the entire difference between this test and the one above.
        """
        assert CLARIFICATION_CARRY_TURNS == 3
        service, repo, case = wired

        await self._upload_that_fails(service, case)
        await self._unrelated(service, case, "unrelated question 0")
        await self._unrelated(service, case, "unrelated question 1")
        await self._unrelated(service, case, "hi")  # GREETING: never reaches the engine

        saved = await repo.get(case.case_id)
        assert saved.current_turn == 4, (
            "the greeting must advance the persisted clock; if it does not, "
            "this test is measuring the same window as the control"
        )
        assert self._clarified_file_ids(saved.last_suggestions) == [], (
            "the greeting spent a turn of the question's window — that is the "
            "#1264 semantics change. If this fails, a non-engine turn has "
            "stopped costing window and the change has been reverted; the "
            "docstrings in suggestion_liveness are then also wrong."
        )


class TestCarryForwardSelection:
    """Direct coverage of which stored entries survive into the next turn.

    The end-to-end flow above exercises the common shape; these pin the
    selection rules themselves.
    """

    _A, _B = "file_aaaaaaaaaaaa", "file_bbbbbbbbbbbb"

    def test_an_ignored_question_survives_a_turn_that_resolved_nothing(self):
        """#1245: a reply that is neither a choice nor a reclassification
        used to rebuild the list from that turn's own output, dropping the
        standing question and leaving the attachment misclassified with no
        server-side recovery path."""
        case = _case_holding(self._A, current_turn=1)
        stored = [_stored_entry(self._A, offered_turn=1)]
        assert _carry_forward_unresolved_clarifications(
            stored, case, None, as_of_turn=2
        ) == [stored[0]]

    def test_the_question_expires_at_the_end_of_its_window(self):
        """The bound, from both sides: still on offer on the last turn of the
        window, gone on the next. Without it an ignored question never
        expires and the resolver's choice list grows without limit.

        The turn numbers are LITERAL. Deriving them from
        ``CLARIFICATION_CARRY_TURNS`` makes the test agree with whatever the
        constant says, so widening the window to 10_000 would still pass —
        which is not a bound, it is a restatement.
        """
        assert CLARIFICATION_CARRY_TURNS == 3, (
            "the window is part of the contract — changing it is a deliberate "
            "act that must update these turn numbers too"
        )
        case = _case_holding(self._A, current_turn=1)
        stored = [_stored_entry(self._A, offered_turn=1)]

        # Offered on turn 1: answerable on turns 2, 3 and 4.
        for as_of in (2, 3, 4):
            assert (
                _carry_forward_unresolved_clarifications(
                    stored, case, None, as_of_turn=as_of
                )
                == stored
            ), f"still within the window at turn {as_of}"
        assert (
            _carry_forward_unresolved_clarifications(stored, case, None, as_of_turn=5)
            == []
        )

    def test_the_resolved_file_is_dropped_and_the_rest_kept(self):
        case = _case_holding(self._A, self._B)
        a, b = _stored_entry(self._A), _stored_entry(self._B)
        assert _carry_forward_unresolved_clarifications(
            [a, b], case, self._A, as_of_turn=2
        ) == [b]

    def test_a_file_reclassified_out_of_band_stops_being_offered(self):
        """fm#918 exposure 1: nothing outside ``process_turn`` rewrites
        ``last_suggestions``, so a file resolved through another path would
        keep its choices armed. The file's ``data_type`` is the marker —
        only reclassification writes it after intake."""
        case = _case_holding(self._A)
        case.uploaded_files = [make_uploaded_file(file_id=self._A, data_type="logs")]
        armed = _stored_entry(self._A, offered_data_type=None)
        assert (
            _carry_forward_unresolved_clarifications([armed], case, None, as_of_turn=2)
            == []
        )
        # Positive control: unchanged since the offer, it IS still carried.
        still_open = _stored_entry(self._A, offered_data_type="logs")
        assert _carry_forward_unresolved_clarifications(
            [still_open], case, None, as_of_turn=2
        ) == [still_open]

    def test_a_file_the_case_no_longer_holds_is_dropped(self):
        case = _case_holding(self._B)
        assert (
            _carry_forward_unresolved_clarifications(
                [_stored_entry(self._A)], case, None, as_of_turn=2
            )
            == []
        )

    def test_a_terminal_case_offers_no_clarification(self):
        """The handler refuses to reclassify on a closed case (422), so
        minting the intent would turn an ordinary typed message into an
        error response."""
        case = _case_holding(self._A).model_copy(
            update={"state": CaseState.CLOSED, "closed_at": datetime.now(UTC)}
        )
        assert case.is_terminal
        assert (
            _carry_forward_unresolved_clarifications(
                [_stored_entry(self._A)], case, None, as_of_turn=2
            )
            == []
        )

    def test_non_clarification_intents_do_not_outlive_their_turn(self):
        """An engine follow-up was about the turn that produced it."""
        case = _case_holding(self._B)
        follow_up = _stored_entry(
            self._B, intent_type=IntentType.CONFIRMATION.value, offered_turn=1
        )
        assert (
            _carry_forward_unresolved_clarifications(
                [follow_up], case, self._A, as_of_turn=2
            )
            == []
        )

    def test_entries_without_intent_or_file_id_are_skipped(self):
        case = _case_holding(self._A, self._B)
        malformed = [
            {"label": "no intent", "action_type": "DECIDE", "offered_turn": 1},
            {
                "label": "no file",
                "intent": {"type": "file_reclassification"},
                "offered_turn": 1,
            },
        ]
        assert (
            _carry_forward_unresolved_clarifications(
                malformed, case, self._A, as_of_turn=2
            )
            == []
        )

    def test_empty_history_is_safe(self):
        case = _case_holding(self._A)
        assert (
            _carry_forward_unresolved_clarifications(None, case, "f", as_of_turn=2)
            == []
        )
        assert (
            _carry_forward_unresolved_clarifications([], case, "f", as_of_turn=2) == []
        )


class TestNoTwoAttachmentsAnswerToTheSameTyping:
    """The wrong-file guarantee, stated on the channel the matcher reads.

    Round one made the LABELS unique and left ``payload`` alone.
    ``IntentResolver._exact_match`` tests payload FIRST — it is the text a
    click sends, so it is the text a user retypes — and the payload names the
    attachment through ``_clarification_subject``, which is "the text you
    pasted" for every paste and the filename for every file. Two pastes, or
    two uploads sharing a name, therefore offered byte-identical payloads for
    different files, and the first hit won.

    The fix is not more wording. It is that such a pair is never on offer
    together (``_admit_clarification_entries``), and that a string which
    would resolve two ways resolves to neither (``_exact_match``).
    """

    @staticmethod
    def _entries(*specs):
        """specs: (file_id, label, payload) triples, newest first."""
        return [
            {
                "label": label,
                "action_type": "DECIDE",
                "payload": payload,
                "body": "Treat as documentation.",
                "intent": {
                    "type": IntentType.FILE_RECLASSIFICATION.value,
                    "file_id": file_id,
                    "data_type": "documentation",
                },
                "offered_turn": turn,
                "offered_data_type": None,
            }
            for file_id, label, payload, turn in specs
        ]

    def test_an_attachment_sharing_only_a_payload_is_not_admitted(self):
        """Payload alone, with DISTINCT labels — round one's exact shape.

        It qualified the label ("…, turn 2") and left the payload untouched,
        so a label-only check reports the set unambiguous while the channel
        the matcher tries FIRST still resolves two ways.
        """
        shared = "Treat the text you pasted as documentation or notes."
        entries = self._entries(
            ("file_bbbbbbbbbbbb", "Documentation (pasted text, turn 2)", shared, 2),
            ("file_aaaaaaaaaaaa", "Documentation (pasted text, turn 1)", shared, 1),
        )
        assert len({e["label"] for e in entries}) == 2, "labels must NOT collide here"
        admitted = _admit_clarification_entries(entries)
        assert [entry_file_id(e) for e in admitted] == ["file_bbbbbbbbbbbb"]

    def test_an_attachment_sharing_only_a_label_is_not_admitted(self):
        """The mirror image: distinct payloads, colliding labels. The matcher
        reads both channels, so either one is enough to refuse."""
        entries = self._entries(
            ("file_bbbbbbbbbbbb", "Documentation (a.log)", "payload B", 2),
            ("file_aaaaaaaaaaaa", "Documentation (a.log)", "payload A", 1),
        )
        assert len({e["payload"] for e in entries}) == 2, "payloads must NOT collide"
        admitted = _admit_clarification_entries(entries)
        assert [entry_file_id(e) for e in admitted] == ["file_bbbbbbbbbbbb"]

    def test_distinguishable_attachments_are_both_admitted(self):
        """The positive control: refusal is about collisions, not about being
        second. Without this the rule above passes for a cap of one."""
        entries = self._entries(
            ("file_bbbbbbbbbbbb", "Documentation (b.log)", "payload B", 2),
            ("file_aaaaaaaaaaaa", "Documentation (a.log)", "payload A", 1),
        )
        admitted = _admit_clarification_entries(entries)
        assert [entry_file_id(e) for e in admitted] == [
            "file_bbbbbbbbbbbb",
            "file_aaaaaaaaaaaa",
        ]

    def test_a_collision_refuses_the_whole_attachment_not_one_choice(self):
        """Dropping the colliding choice alone would leave a menu that looks
        complete and silently no longer offers that option."""
        entries = self._entries(
            ("file_bbbbbbbbbbbb", "Documentation (x)", "shared", 2),
            ("file_aaaaaaaaaaaa", "Documentation (x)", "shared", 1),
            ("file_aaaaaaaaaaaa", "Something else (y)", "unique to a", 1),
        )
        admitted = _admit_clarification_entries(entries)
        assert {entry_file_id(e) for e in admitted} == {"file_bbbbbbbbbbbb"}

    def test_the_matcher_refuses_a_string_that_would_resolve_two_ways(self):
        """The guard behind admission. Admission should make this
        unreachable for clarifications; this is what makes it TRUE rather
        than merely intended, and it also covers the pairs admission does not
        arbitrate — a clarification and an engine follow-up sharing wording.
        """
        resolver = IntentResolver(MagicMock())
        ambiguous = self._entries(
            ("file_bbbbbbbbbbbb", "Yes", "Yes", 2),
        ) + [
            {
                "label": "Yes",
                "action_type": "DECIDE",
                "payload": "Yes",
                "body": "",
                "intent": {"type": IntentType.CONFIRMATION.value},
                "offered_turn": 2,
            }
        ]
        assert resolver._exact_match("yes", ambiguous) is None
        # Positive control: drop the second, and the same typing resolves.
        assert resolver._exact_match("yes", ambiguous[:1]) is not None

    def test_two_entries_carrying_the_same_intent_are_not_ambiguous(self):
        """Ambiguity is about the ANSWER, not about the number of hits: a
        duplicate row for one attachment resolves normally."""
        resolver = IntentResolver(MagicMock())
        duplicated = self._entries(
            ("file_bbbbbbbbbbbb", "Documentation", "Documentation", 2),
            ("file_bbbbbbbbbbbb", "Documentation", "Documentation", 2),
        )
        matched = resolver._exact_match("documentation", duplicated)
        assert matched is not None and matched["file_id"] == "file_bbbbbbbbbbbb"


class TestTheWriterStoresWhatTheReaderWillAccept:
    """One predicate, one clock — the invariant round one asserted and broke.

    Both repositories persist ``effective_current_turn``, which does not
    advance across a SERVICE-dispatched turn (#1264). The write site filtered
    at ``current_turn + 1`` — the IN-FLIGHT counter — so on any turn the engine
    did not record, it aged entries one further than the next read would, and
    dropped questions that read would have accepted.
    """

    _failed_classification = staticmethod(
        TestRecoveryLoopSurvivesResolvingOneAttachment._failed_classification
    )
    _clarified_file_ids = staticmethod(
        TestRecoveryLoopSurvivesResolvingOneAttachment._clarified_file_ids
    )

    @pytest.mark.asyncio
    async def test_a_clarification_click_does_not_over_age_the_other_question(
        self, repo_with_case, preprocessing_service, file_storage
    ):
        """Drives the real seam, at the window boundary, over a SERVICE turn.

        Three things have to line up or this passes vacuously, and each was
        wrong in an earlier draft:

        1. It goes through ``process_turn``. Calling the helper with
           hand-picked ``as_of_turn`` values tests the arithmetic, and the
           arithmetic was never in doubt — the call site's choice of clock was.
        2. The repository projects the counter (``_RecordingRepository``) and
           the engine records turns (``_RecordingEngine``). Without both, the
           two counters are equal by construction and nothing can diverge.
        3. The question is at the LAST turn of its window when the SERVICE
           turn writes. The mutation is a constant +1, so it only bites at the
           boundary; anywhere else both clocks keep the entry and the test is
           green either way.

        Turns 1-3 are engine turns, ageing the paste's question to 3. Turn 4
        is a clarification click — SERVICE-dispatched, recording no turn — and
        the paste's question must survive it, because the next read computes
        age 3 and 3 is still in window.
        """
        _, case = repo_with_case
        repo = RecordingCaseRepository()
        case.uploaded_files = []
        case.evidence = []
        case.turn_history = []
        repo._storage[case.case_id] = case

        hashes = iter(["a" * 64, "b" * 64])
        preprocessing_service.classify_and_extract = AsyncMock(
            side_effect=lambda *a, **k: self._failed_classification(next(hashes))
        )
        blobs = iter([{"storage_key": f"evidence/case_x/b{i}.txt"} for i in range(2)])
        file_storage.store_file = AsyncMock(side_effect=lambda *a, **k: next(blobs))
        file_storage.mark_linked = AsyncMock(return_value=True)
        service = InvestigationService(
            milestone_engine=RecordingMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )

        # ---- turn 1 (engine): a file and a paste, both below threshold.
        await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query="the dump plus what I pasted",
                attachments=[
                    Attachment(
                        content=b"ambiguous file bytes",
                        filename="mystery.txt",
                        content_type="text/plain",
                        source_metadata={"source_type": "file_upload"},
                    ),
                    Attachment(
                        content=b"NAME READY STATUS",
                        filename="pasted-content-20260713T083214.txt",
                        content_type="text/plain",
                        source_metadata={"source_type": "text_paste"},
                    ),
                ],
            ),
        )
        saved = await repo.get(case.case_id)
        file_a = next(
            f.file_id for f in saved.uploaded_files if f.filename == "mystery.txt"
        )
        file_b = next(
            f.file_id for f in saved.uploaded_files if f.filename != "mystery.txt"
        )
        assert sorted(self._clarified_file_ids(saved.last_suggestions)) == sorted(
            [file_a, file_b]
        )

        # ---- turn 2 (engine): unrelated, ageing both questions to 2.
        #
        # One diverting turn, not two. Since #1264 a clarification click records
        # a turn like every other consuming route, so the click below COSTS a
        # turn of the surviving question's window — where before it was free.
        # The claim this test makes is unchanged (answering one question must
        # not drop another that is still in window); only the budget moved.
        # ``TestAClarificationClickCostsAWindowTurn`` (this file) pins the new
        # semantics directly.
        for text in ("is the pool maxed out?",):
            await service.process_turn(
                case_id=case.case_id,
                user_id="user_owner",
                payload=TurnPayload(query=text),
            )
        saved = await repo.get(case.case_id)
        assert saved.current_turn == 2, "engine turns advance the persisted counter"
        assert sorted(self._clarified_file_ids(saved.last_suggestions)) == sorted(
            [file_a, file_b]
        )

        # ---- turn 3: a clarification click. SERVICE-dispatched, and since
        #      #1264 it records a turn like every other consuming route, so the
        #      persisted counter advances to 3. The paste's question is age 3 to
        #      the next read — the last turn in window.
        #
        #      Before #1264 the counter froze here, and this assertion read
        #      ``== 3`` with the note "a SERVICE turn records none". That was
        #      pinning the defect as a precondition. The claim the test actually
        #      exists to make — the surviving question is not over-aged out — is
        #      unchanged and still holds; only the arithmetic became honest.
        await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query="treat it as logs",
                intent=QueryIntent(
                    type=IntentType.FILE_RECLASSIFICATION,
                    file_id=file_a,
                    data_type="logs_and_errors",
                ),
            ),
        )
        saved = await repo.get(case.case_id)
        assert saved.current_turn == 3, "every consumed turn records one (#1264)"
        assert self._clarified_file_ids(saved.last_suggestions) == [file_b], (
            "the writer aged the surviving question on the in-flight counter, "
            "which the next read does not share, and dropped a question that "
            "was still answerable"
        )


class TestATerminalTurnOffersNothing:
    """A turn can deliver an unclassifiable attachment AND close the case.

    ``_handle_file_reclassification`` refuses on a terminal case, so every
    card would be a button that answers 422 and every typed choice is dropped
    by the liveness rule. Round one still rendered the cards, appended the
    "How should I treat it?" note, and persisted the entries — making
    ``_stored_suggestions``'s own contract false for exactly the set where it
    mattered.
    """

    @pytest.mark.asyncio
    async def test_no_cards_no_note_no_stored_entries(
        self, repo_with_case, preprocessing_service, file_storage
    ):
        repo, case = repo_with_case
        case.uploaded_files = []
        case.evidence = []
        preprocessing_service.classify_and_extract = AsyncMock(
            return_value=(
                TestRecoveryLoopSurvivesResolvingOneAttachment._failed_classification(
                    "e" * 64
                )
            )
        )
        file_storage.store_file = AsyncMock(
            return_value={"storage_key": "evidence/case_x/mystery.txt"}
        )
        file_storage.mark_linked = AsyncMock(return_value=True)

        engine = MockMilestoneEngine()
        original = engine._process_turn

        async def close_the_case(case, *a, **kw):
            result = await original(case, *a, **kw)
            result["case_updated"] = case.model_copy(
                update={
                    "state": CaseState.CLOSED,
                    "closed_at": datetime.now(UTC),
                    "closure_reason": "resolved_elsewhere",
                }
            )
            return result

        engine.process_turn = AsyncMock(side_effect=close_the_case)

        service = InvestigationService(
            milestone_engine=engine,
            case_repository=repo,
            preprocessing_service=preprocessing_service,
            file_storage_service=file_storage,
        )
        response = await service.process_turn(
            case_id=case.case_id,
            user_id="user_owner",
            payload=TurnPayload(
                query="closing this out",
                attachments=[
                    Attachment(
                        content=b"ambiguous",
                        filename="mystery.txt",
                        content_type="text/plain",
                        source_metadata={"source_type": "file_upload"},
                    )
                ],
            ),
        )

        saved = await repo.get(case.case_id)
        assert saved.is_terminal
        assert [
            a for a in response.suggested_actions if (a.intent or {}).get("file_id")
        ] == []
        assert "How should I treat" not in response.agent_response
        assert saved.last_suggestions is None


class TestSuggestionLiveness:
    """The stamp, and what an absent or impossible one means."""

    _A = "file_aaaaaaaaaaaa"

    def test_an_unstamped_entry_is_not_live(self):
        """Rows persisted before the stamp existed, and anything a non-turn
        writer left behind, are the same epistemic position: nothing here
        knows what turn they belong to."""
        case = _case_holding(self._A)
        legacy = _stored_entry(self._A)
        del legacy["offered_turn"]
        assert live_suggestions([legacy], case, as_of_turn=2) == []
        # Positive control: the identical entry WITH a stamp is live.
        assert live_suggestions([_stored_entry(self._A)], case, as_of_turn=2) != []

    def test_a_boolean_is_not_a_turn_number(self):
        """``bool`` is an ``int`` subclass, so ``True`` would age as turn 1."""
        case = _case_holding(self._A)
        entry = _stored_entry(self._A)
        entry["offered_turn"] = True
        assert live_suggestions([entry], case, as_of_turn=2) == []

    def test_a_stamp_from_the_future_is_not_live(self):
        case = _case_holding(self._A)
        assert (
            live_suggestions(
                [_stored_entry(self._A, offered_turn=9)], case, as_of_turn=2
            )
            == []
        )

    def test_a_follow_up_dies_one_turn_after_it_was_offered(self):
        """fm#918 exposure 2: the engine appends ``turn_history`` at Step 6
        and saves at Step 7, so a row committed when the final assignment
        never ran carries turn N in the persisted counter beside a stamp of
        N-1. On the retry turn that ages to 2 — out of window — so a typed
        "yes" cannot consent to a proposal that no longer exists."""
        case = _case_holding(self._A)
        follow_up = _stored_entry(
            self._A, intent_type=IntentType.CONFIRMATION.value, offered_turn=5
        )
        assert live_suggestions([follow_up], case, as_of_turn=6) == [follow_up]
        assert live_suggestions([follow_up], case, as_of_turn=7) == []


class TestClarificationSpanCap:
    """The hard bound on the resolver's choice list."""

    def test_the_span_is_capped_and_the_oldest_offer_is_evicted(self):
        entries = [
            _stored_entry(f"file_{n}", offered_turn=turn)
            for n, turn in [("d", 4), ("c", 3), ("b", 2), ("a", 1)]
        ]
        kept = _admit_clarification_entries(entries)
        assert [entry_file_id(e) for e in kept] == ["file_d", "file_c", "file_b"]
        assert len(kept) == CLARIFICATION_SPAN_CAP

    def test_an_attachment_is_admitted_or_dropped_whole(self):
        """Keeping some of an attachment's choices leaves a menu that looks
        complete and silently no longer offers the dropped option."""
        entries = [
            _stored_entry("file_d", offered_turn=4, label="Logs (d)"),
            _stored_entry("file_d", offered_turn=4, label="Something else (d)"),
            _stored_entry("file_c", offered_turn=3),
            _stored_entry("file_b", offered_turn=2),
            _stored_entry("file_a", offered_turn=1),
        ]
        kept = _admit_clarification_entries(entries)
        assert [entry_file_id(e) for e in kept] == [
            "file_d",
            "file_d",
            "file_c",
            "file_b",
        ]

    def test_eviction_is_stable_for_offers_from_the_same_turn(self):
        entries = [_stored_entry(f"file_{n}", offered_turn=7) for n in "abcd"]
        kept = _admit_clarification_entries(entries)
        assert [entry_file_id(e) for e in kept] == ["file_a", "file_b", "file_c"]


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
