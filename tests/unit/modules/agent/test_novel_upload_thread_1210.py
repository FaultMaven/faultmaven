"""Regression set for #1210, service half — novelty must reach the engine.

The engine cannot work out whether an upload brought data the case did not
already hold: ``_preprocess_attachment`` appends the authoritative
``UploadedFile`` to the same ``case`` object it will hand to ``process_turn``,
so by the time the engine runs, ``case.uploaded_files`` contains the row for a
brand-new upload exactly as it does for a deduped one. The engine's own test
(``file_id not in {f.file_id for f in case.uploaded_files}``) was therefore
False on every turn and #1136's upload progress arm never armed.

``_PreprocessedAttachment.duplicate_of`` is the answer, computed where it is
still knowable: the content-hash short-circuit sets it when it returns an
EXISTING row instead of creating one. These drive the real
``InvestigationService.process_turn`` and pin that the flag reaching the engine
agrees with it — and that the ordering which broke the engine's derivation is
still exactly what happens.

Engine-side behaviour lives in
``tests/unit/core/investigation/test_novel_upload_progress_1210.py``.
"""

import copy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.models.api import DataType
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import UploadedFile

pytestmark = pytest.mark.unit

CONTENT = b"2026-08-28T10:00:00Z ERROR pod restart loop\n"
CONTENT_HASH = "a" * 64
EXISTING_FILE_ID = "file_eeeeeeeeeeee"
EXISTING_TURN = 2


class _PreprocessingDouble:
    """Returns a real result object, not a Mock: the row is built by assigning
    these values onto a Pydantic ``UploadedFile``."""

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
def service(mock_milestone_engine, mock_case_repository):
    svc = InvestigationService(
        milestone_engine=mock_milestone_engine,
        case_repository=mock_case_repository,
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
        self, service, mock_case_repository, sample_case, sample_user_id, seen
    ):
        await _run(service, mock_case_repository, sample_case, sample_user_id)

        assert seen["attachments"][0]["is_novel"] is True

    async def test_the_row_is_already_on_the_aggregate_when_the_engine_runs(
        self, service, mock_case_repository, sample_case, sample_user_id, seen
    ):
        """The reason the engine cannot derive novelty for itself — stated as a
        measurement rather than as prose. The id it was handed is ALREADY in
        the set it used to test against, on the turn the file first arrived."""
        await _run(service, mock_case_repository, sample_case, sample_user_id)

        file_id = seen["attachments"][0]["file_id"]
        assert file_id in seen["aggregate_ids"], (
            "if this ever stops holding, the engine-side derivation removed in "
            "#1210 would have worked and this thread is redundant"
        )
        assert seen["attachments"][0]["is_novel"] is True

    async def test_the_response_does_not_call_it_a_duplicate(
        self, service, mock_case_repository, sample_case, sample_user_id, seen
    ):
        """Same fact, other surface: ``duplicate_of`` drives both."""
        response = await _run(
            service, mock_case_repository, sample_case, sample_user_id
        )

        assert response.attachments_processed[0].processing_status == "completed"
        assert response.attachments_processed[0].duplicate_of is None


class TestAByteIdenticalResubmission:
    @pytest.fixture
    def repo_with_the_file(self, mock_case_repository):
        mock_case_repository.find_uploaded_file_by_content_hash = AsyncMock(
            return_value=_existing_row()
        )
        return mock_case_repository

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
        self, service, mock_case_repository, sample_case, sample_user_id, seen
    ):
        """``attachment_metadata`` walks ``preprocess_results`` now rather than
        ``uploaded_files_this_turn`` — the same set, in the same order, one
        entry per attachment."""
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)
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
