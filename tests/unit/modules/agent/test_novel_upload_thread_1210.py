"""Regression set for #1210, service half — novelty must reach the engine.

The engine cannot work out whether an upload brought data the case did not
already hold: ``_preprocess_attachment`` appends the authoritative
``UploadedFile`` to the same ``case`` object it will hand to ``process_turn``,
so by the time the engine runs, ``case.uploaded_files`` contains the row for a
brand-new upload exactly as it does for a deduped one. The engine's own test
(``file_id not in {f.file_id for f in case.uploaded_files}``) was therefore
False on every turn and #1136's upload progress arm never armed.

``_PreprocessedAttachment`` holds the answer, computed where it is still
knowable: the content-hash short-circuit sets ``duplicate_of`` when it returns
an EXISTING row instead of creating one, and ``dedup_ran`` records whether that
lookup executed at all. The flag is therefore TRI-STATE — True / False /
undetermined — because ``duplicate_of is None`` alone conflates "ran and found
nothing" with "never ran", and reporting the latter as novel arms the stall net
on a byte-identical re-submission.

These drive the real ``InvestigationService.process_turn`` and pin that the flag
reaching the engine agrees with it — and that the ordering which broke the
engine's derivation is still exactly what happens.

Engine-side behaviour lives in
``tests/unit/core/investigation/test_novel_upload_progress_1210.py``.
"""

import copy
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import (
    Attachment,
    InquiryResponse,
    TurnPayload,
)
from faultmaven.models.api import DataType
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import Case, CaseState, UploadedFile

pytestmark = pytest.mark.unit

_SERVICE_LOGGER = "faultmaven.modules.agent.domain.services.investigation_service"

CONTENT = b"2026-08-28T10:00:00Z ERROR pod restart loop\n"
CONTENT_HASH = "a" * 64
EXISTING_FILE_ID = "file_eeeeeeeeeeee"
EXISTING_TURN = 2


class _PreprocessingDouble:
    """Returns a real result object, not a Mock: the row is built by assigning
    these values onto a Pydantic ``UploadedFile``.

    ``content_hash=""`` models an extractor that produced no hash — the input
    to one of the two paths on which dedup cannot run at all.
    """

    def __init__(self, content_hash: str = CONTENT_HASH):
        self.content_hash = content_hash

    async def classify_and_extract(self, content, filename, source_metadata=None):
        return SimpleNamespace(
            summary="Pod restart loop.",
            structural_index="ERROR x 42 between 10:00 and 10:05",
            detailed_data_type=DataType.LOGS_AND_ERRORS,
            content_hash=self.content_hash,
            coverage_start_ts=None,
            coverage_end_ts=None,
            extraction_method="structure_extraction",
            extraction_metadata={},
        )


def _existing_row() -> UploadedFile:
    """The row a byte-identical earlier turn already committed."""
    return UploadedFile(
        file_id=EXISTING_FILE_ID,
        filename="app.log",
        size_bytes=len(CONTENT),
        content_type="text/plain",
        content_hash=CONTENT_HASH,
        uploaded_at_turn=EXISTING_TURN,
        uploaded_at=datetime.now(timezone.utc),
        uploaded_by="user_123",
        upload_source="file_upload",
        storage_ref="ref/app.log",
        data_type="logs",
    )


@pytest.fixture
def repo(mock_case_repository):
    """The shared double, with a WORKING dedup lookup that finds nothing.

    A real repository implements ``find_uploaded_file_by_content_hash``; the
    bare double does not, and the service treats a missing lookup as "novelty
    undetermined" — correctly, but that is the *other* scenario. Every test
    below that means "a fresh upload" needs the lookup to have actually run,
    so it is installed here rather than per-test.
    """
    mock_case_repository.find_uploaded_file_by_content_hash = AsyncMock(
        return_value=None
    )
    return mock_case_repository


@pytest.fixture
def service(mock_milestone_engine, repo):
    svc = InvestigationService(
        milestone_engine=mock_milestone_engine,
        case_repository=repo,
    )
    svc.preprocessing_service = _PreprocessingDouble()
    return svc


@pytest.fixture
def seen(mock_milestone_engine):
    """What the engine was handed, captured AT CALL TIME.

    The aggregate is read here rather than off ``call_args`` afterwards because
    the whole defect is about *when* the row is on it.
    """
    captured: dict = {}
    delegate = mock_milestone_engine._process_turn

    async def spy(
        *,
        case,
        user_message,
        attachments=None,
        intent_type=None,
        intent_data=None,
        user_id=None,
    ):
        captured["attachments"] = copy.deepcopy(attachments)
        captured["aggregate_ids"] = [f.file_id for f in case.uploaded_files]
        return await delegate(
            case=case,
            user_message=user_message,
            attachments=attachments,
            intent_type=intent_type,
            intent_data=intent_data,
            user_id=user_id,
        )

    mock_milestone_engine.process_turn = AsyncMock(side_effect=spy)
    return captured


def _payload() -> TurnPayload:
    return TurnPayload(
        query="here are the logs",
        attachments=[
            Attachment(
                content=CONTENT,
                filename="app.log",
                content_type="text/plain",
            )
        ],
        intent=QueryIntent(type=IntentType.CONVERSATION),
    )


async def _run(service, repo, case, user_id) -> object:
    case.user_id = user_id
    await repo.save(case)
    return await service.process_turn(
        case_id=case.case_id, user_id=user_id, payload=_payload()
    )


class TestAFreshUpload:
    async def test_it_reaches_the_engine_marked_novel(
        self, service, repo, sample_case, sample_user_id, seen
    ):
        await _run(service, repo, sample_case, sample_user_id)

        assert seen["attachments"][0]["is_novel"] is True

    async def test_the_row_is_already_on_the_aggregate_when_the_engine_runs(
        self, service, repo, sample_case, sample_user_id, seen
    ):
        """The reason the engine cannot derive novelty for itself — stated as a
        measurement rather than as prose. The id it was handed is ALREADY in
        the set it used to test against, on the turn the file first arrived."""
        await _run(service, repo, sample_case, sample_user_id)

        file_id = seen["attachments"][0]["file_id"]
        assert file_id in seen["aggregate_ids"], (
            "if this ever stops holding, the engine-side derivation removed in "
            "#1210 would have worked and this thread is redundant"
        )
        assert seen["attachments"][0]["is_novel"] is True

    async def test_the_response_does_not_call_it_a_duplicate(
        self, service, repo, sample_case, sample_user_id, seen
    ):
        """Same fact, other surface: ``duplicate_of`` drives both."""
        response = await _run(service, repo, sample_case, sample_user_id)

        assert response.attachments_processed[0].processing_status == "completed"
        assert response.attachments_processed[0].duplicate_of is None


class TestAByteIdenticalResubmission:
    @pytest.fixture
    def repo_with_the_file(self, repo):
        """Depends on ``repo`` rather than the raw double so this override
        lands AFTER the working-lookup install, whatever order a test lists
        its fixtures in."""
        repo.find_uploaded_file_by_content_hash = AsyncMock(
            return_value=_existing_row()
        )
        return repo

    async def test_it_reaches_the_engine_marked_not_novel(
        self, service, repo_with_the_file, sample_case, sample_user_id, seen
    ):
        await _run(service, repo_with_the_file, sample_case, sample_user_id)

        assert seen["attachments"][0]["is_novel"] is False
        assert seen["attachments"][0]["file_id"] == EXISTING_FILE_ID

    async def test_the_dedup_lookup_is_the_one_that_decided_it(
        self, service, repo_with_the_file, sample_case, sample_user_id, seen
    ):
        """Pins the call the flag is derived from, so a renamed or re-signed
        lookup fails here rather than silently reporting everything novel."""
        await _run(service, repo_with_the_file, sample_case, sample_user_id)

        repo_with_the_file.find_uploaded_file_by_content_hash.assert_awaited_once_with(
            sample_case.case_id, CONTENT_HASH
        )

    async def test_the_response_calls_it_a_duplicate(
        self, service, repo_with_the_file, sample_case, sample_user_id, seen
    ):
        response = await _run(service, repo_with_the_file, sample_case, sample_user_id)

        assert response.attachments_processed[0].processing_status == "duplicate"
        assert response.attachments_processed[0].duplicate_of == EXISTING_FILE_ID
        assert seen["attachments"][0]["is_novel"] is False


class TestTheEngineIsToldAboutEveryAttachment:
    async def test_one_metadata_dict_per_attachment(
        self, service, repo, sample_case, sample_user_id, seen
    ):
        """``attachment_metadata`` walks ``preprocess_results`` now rather than
        ``uploaded_files_this_turn`` — the same set, in the same order, one
        entry per attachment."""
        sample_case.user_id = sample_user_id
        await repo.save(sample_case)
        payload = TurnPayload(
            query="here are the logs",
            attachments=[
                Attachment(
                    content=CONTENT, filename="a.log", content_type="text/plain"
                ),
                Attachment(
                    content=CONTENT + b"x", filename="b.log", content_type="text/plain"
                ),
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(
            case_id=sample_case.case_id, user_id=sample_user_id, payload=payload
        )

        assert len(seen["attachments"]) == 2
        assert [a["filename"] for a in seen["attachments"]] == ["a.log", "b.log"]
        assert [a["is_novel"] for a in seen["attachments"]] == [True, True]


class TestWhenDedupCouldNotRun:
    """``duplicate_of is None`` has two causes, and only one of them is
    "novel". These drive the other one.

    Each test re-submits content the case ALREADY HOLDS — the row is seeded on
    the aggregate — so "novel" is demonstrably the wrong answer, not merely an
    unproven one. Reporting True here arms #1136's progress arm and resets
    ``turns_without_progress`` on a turn that brought nothing: #1210 inverted,
    and in the aggressive direction. Undetermined is the honest answer, and the
    engine scores it conservatively.
    """

    @pytest.fixture
    def case_already_holding_it(self, sample_case, sample_user_id):
        sample_case.user_id = sample_user_id
        sample_case.uploaded_files = [_existing_row()]
        return sample_case

    async def test_no_content_hash_is_undetermined_not_novel(
        self, service, repo, case_already_holding_it, sample_user_id, seen
    ):
        """Path 1: the extractor produced no hash, so the lookup is skipped
        outright and never consulted."""
        service.preprocessing_service = _PreprocessingDouble(content_hash="")

        await _run(service, repo, case_already_holding_it, sample_user_id)

        repo.find_uploaded_file_by_content_hash.assert_not_awaited()
        assert seen["attachments"][0]["is_novel"] is None

    async def test_no_content_hash_is_logged(
        self, service, repo, case_already_holding_it, sample_user_id, seen, caplog
    ):
        service.preprocessing_service = _PreprocessingDouble(content_hash="")

        with caplog.at_level(logging.WARNING, logger=_SERVICE_LOGGER):
            await _run(service, repo, case_already_holding_it, sample_user_id)

        assert any(
            "UNDETERMINED" in r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.WARNING
        ), "a permanently skipped dedup lookup must not be silent"

    async def test_a_lookup_raising_from_inside_is_undetermined_not_novel(
        self, service, repo, case_already_holding_it, sample_user_id, seen
    ):
        """Path 2: a REAL repository raising AttributeError from inside its own
        method body — not a double missing the attribute.

        The service catches AttributeError to tolerate minimal test doubles,
        and that same catch swallows a genuine bug inside an implementation.
        Either way dedup did not run, so the answer is undetermined.
        """

        async def broken_lookup(case_id, content_hash):
            row = None
            return row.file_id  # AttributeError, raised INSIDE the method

        repo.find_uploaded_file_by_content_hash = broken_lookup

        await _run(service, repo, case_already_holding_it, sample_user_id)

        assert seen["attachments"][0]["is_novel"] is None

    async def test_a_repository_without_the_lookup_is_undetermined_not_novel(
        self,
        mock_milestone_engine,
        mock_case_repository,
        sample_case,
        sample_user_id,
        seen,
    ):
        """Path 2b: the bare double, which has no such attribute at all.

        Uses ``mock_case_repository`` directly rather than the ``repo`` fixture
        — installing the lookup is exactly what this test must not do.
        """
        svc = InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )
        svc.preprocessing_service = _PreprocessingDouble()
        sample_case.uploaded_files = [_existing_row()]

        await _run(svc, mock_case_repository, sample_case, sample_user_id)

        assert seen["attachments"][0]["is_novel"] is None

    async def test_the_engine_scores_an_undetermined_turn_as_no_progress(
        self, service, repo, case_already_holding_it, sample_user_id, seen
    ):
        """End of the thread: the real engine, handed the real dict this turn
        produced, must not arm the upload progress arm.

        Driven through ``_process_response_structured`` rather than asserted
        about, because the engine reads the key itself — an explicit ``None``
        has to travel the same conservative path as an absent one.
        """
        service.preprocessing_service = _PreprocessingDouble(content_hash="")
        await _run(service, repo, case_already_holding_it, sample_user_id)

        engine = MilestoneEngine.__new__(MilestoneEngine)
        case = Case(
            case_id="case_aabb11223344",
            organization_id="org_123",
            title="t",
            description="d",
            state=CaseState.INQUIRY,
        )
        _, metadata = await engine._process_response_structured(
            case,
            "same file again",
            InquiryResponse(
                agent_response="ack",
                state_updates=InquiryResponse.InquiryStateUpdate(),
            ),
            seen["attachments"],
        )

        assert metadata.get("novel_files_uploaded") is None
        assert engine._check_if_progress_made(metadata) is False
