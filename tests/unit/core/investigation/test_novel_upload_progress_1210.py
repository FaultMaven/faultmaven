"""Regression set for #1210 — ``novel_files_uploaded`` was never populated.

#1136 narrowed the progress predicate to artifacts the case did not already
hold, and gave uploads an arm of their own: ``novel_files_uploaded``. The engine
computed it against the case aggregate::

    known_ids = {f.file_id for f in case.uploaded_files}
    ...
    if uploaded_file.file_id not in known_ids:
        metadata["novel_files_uploaded"] = ...

``investigation_service._preprocess_attachment`` has ALREADY appended the
authoritative row to that same ``case`` object before ``process_turn`` is
called, and there is no reload in between. So the id is in ``known_ids`` for a
brand-new upload exactly as much as for a deduped one, and the condition was
False on every turn.

The consequence is not cosmetic: a turn on which the user uploads a genuinely
new log file scored as NO progress whenever the LLM emitted nothing novel of its
own — a normal triage shape. ``turns_without_progress`` then incremented on
turns where real data arrived, which is the input ``is_stalled``, the exhaustion
detector, ``INSUFFICIENT_EVIDENCE``, ``TREATMENT_BLOCKED`` and the LOW/BLOCKED
momentum bands all key on.

The novelty answer exists upstream and only upstream, and it is threaded in as
a tri-state ``is_novel``: True (dedup ran, found nothing), False (dedup found
the bytes), None (dedup could not run — undetermined, scored conservatively).
These pin the engine half; see
``tests/unit/modules/agent/test_novel_upload_thread_1210.py`` for the service
half, including which of the three each path produces.
"""

import logging
from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import InquiryResponse
from faultmaven.modules.case.contracts import Case, CaseState, TurnOutcome, UploadedFile

pytestmark = pytest.mark.unit

FILE_ID = "file_0b0b0b0b0b0b"
TURN = 5


@pytest.fixture
def engine():
    return MilestoneEngine.__new__(MilestoneEngine)


def _row(file_id: str = FILE_ID, turn: int = TURN) -> UploadedFile:
    """The row ``_preprocess_attachment`` committed and appended."""
    return UploadedFile(
        file_id=file_id,
        filename="app.log",
        size_bytes=120,
        content_type="text/plain",
        content_hash="a" * 64,
        uploaded_at_turn=turn,
        uploaded_at=datetime.now(timezone.utc),
        uploaded_by="user_123",
        upload_source="file_upload",
        storage_ref="ref/app.log",
    )


def _case(rows) -> Case:
    case = Case(
        case_id="case_aabb11223344",
        enterprise_id="org_123",
        title="t",
        description="d",
        state=CaseState.INQUIRY,
    )
    case.uploaded_files = list(rows)
    case.current_turn = TURN
    return case


_ABSENT = object()


def _attachment(is_novel, file_id: str = FILE_ID) -> dict:
    """The dict ``_engine_attachment_metadata`` builds.

    ``is_novel`` is stated at every call site so each test names the turn shape
    it drives. It is tri-state: ``True`` (dedup ran, found nothing), ``False``
    (dedup found the bytes), ``None`` (dedup could not run — undetermined).
    ``_ABSENT`` is the fourth case: a caller that never set the key at all,
    which is a contract violation rather than a turn shape.
    """
    meta = {
        "file_id": file_id,
        "filename": "app.log",
        "data_type": "logs",
        "size": 120,
        "source_type": "file_upload",
        "summary": "Pod restart loop.",
        "storage_ref": "ref/app.log",
    }
    if is_novel is not _ABSENT:
        meta["is_novel"] = is_novel
    return meta


async def _run(engine, case, attachments):
    response = InquiryResponse(
        agent_response="ack",
        state_updates=InquiryResponse.InquiryStateUpdate(),
    )
    _, metadata = await engine._process_response_structured(
        case, "here are the logs", response, attachments
    )
    return metadata


class TestTheArmUnderTheProductionOrdering:
    """The row is on the aggregate BEFORE the engine runs — in every case
    below, exactly as in production. That is what made the engine's own
    novelty test unable to distinguish the two."""

    async def test_a_genuinely_new_upload_is_reported_novel(self, engine):
        case = _case([_row()])

        metadata = await _run(engine, case, [_attachment(True)])

        assert metadata["novel_files_uploaded"] == [FILE_ID], (
            "the id is already in the aggregate the engine would derive "
            "novelty from; the answer has to come from upstream"
        )

    async def test_a_genuinely_new_upload_is_progress(self, engine):
        """The point of the arm. Nothing else on this turn is progress: no
        milestones, no evidence, no hypotheses, a CONVERSATION outcome — the
        LLM said nothing novel and the user uploaded a new log file."""
        case = _case([_row()])

        metadata = await _run(engine, case, [_attachment(True)])

        assert metadata["outcome"] == TurnOutcome.CONVERSATION
        assert engine._check_if_progress_made(metadata) is True

    async def test_a_byte_identical_resubmission_is_not_reported_novel(self, engine):
        case = _case([_row()])

        metadata = await _run(engine, case, [_attachment(False)])

        assert metadata.get("novel_files_uploaded", []) == []

    async def test_a_byte_identical_resubmission_is_not_progress(self, engine):
        """The arm must stay shut for a re-submission — that is what #1136
        narrowed the predicate for."""
        case = _case([_row()])

        metadata = await _run(engine, case, [_attachment(False)])

        assert engine._check_if_progress_made(metadata) is False

    async def test_every_attachment_is_still_reported_either_way(self, engine):
        """``files_uploaded`` is the turn's record of what arrived, novel or
        not. It is not a progress arm and must not narrow."""
        case = _case([_row(), _row(file_id="file_0c0c0c0c0c0c")])

        metadata = await _run(
            engine,
            case,
            [_attachment(True), _attachment(False, file_id="file_0c0c0c0c0c0c")],
        )

        assert metadata["files_uploaded"] == [FILE_ID, "file_0c0c0c0c0c0c"]
        assert metadata["novel_files_uploaded"] == [FILE_ID]


class TestTheArmInIsolation:
    """``_check_if_progress_made`` is a disjunction, so a turn can be progress
    for reasons unrelated to the upload. These read the arm on its own."""

    def test_the_upload_arm_alone_is_progress(self, engine):
        assert (
            engine._check_if_progress_made(
                {
                    "novel_files_uploaded": [FILE_ID],
                    "outcome": TurnOutcome.CONVERSATION,
                }
            )
            is True
        )

    def test_the_raw_list_alone_is_not(self, engine):
        """``files_uploaded`` names every attachment including re-submissions,
        which is why it is not the arm (#1136)."""
        assert (
            engine._check_if_progress_made(
                {
                    "files_uploaded": [FILE_ID],
                    "outcome": TurnOutcome.CONVERSATION,
                }
            )
            is False
        )


class TestANoveltySignalThatIsNotAnAnswer:
    """Undetermined (``None``) and absent are both scored as NOT novel — and
    both are logged.

    Not novel because the alternative is worse: a turn whose dedup lookup could
    not run is exactly a turn that might be a re-submission, and calling it
    novel arms the stall net on data the case may already hold. #1210 in
    reverse. Logged because a silent answer either way is what made #1210
    invisible for as long as it was.

    ``None`` is the shape production actually produces — the service threads it
    when the content-hash lookup was skipped or raised. Absent is a caller that
    never set the key.
    """

    @pytest.mark.parametrize(
        "signal,label",
        [(None, "undetermined"), (_ABSENT, "absent")],
        ids=["undetermined", "absent"],
    )
    async def test_it_is_not_reported_novel(self, engine, signal, label):
        case = _case([_row()])

        metadata = await _run(engine, case, [_attachment(signal)])

        assert metadata.get("novel_files_uploaded") is None, label
        assert metadata["files_uploaded"] == [FILE_ID]

    @pytest.mark.parametrize("signal", [None, _ABSENT], ids=["undetermined", "absent"])
    async def test_it_is_not_progress(self, engine, signal):
        case = _case([_row()])

        metadata = await _run(engine, case, [_attachment(signal)])

        assert engine._check_if_progress_made(metadata) is False

    @pytest.mark.parametrize("signal", [None, _ABSENT], ids=["undetermined", "absent"])
    async def test_it_is_logged(self, engine, caplog, signal):
        case = _case([_row()])

        with caplog.at_level(
            logging.WARNING, logger="faultmaven.core.investigation.milestone_engine"
        ):
            await _run(engine, case, [_attachment(signal)])

        assert any(
            FILE_ID in r.getMessage() and "novelty" in r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.WARNING
        ), "an undetermined novelty signal must not pass silently"

    async def test_an_attachment_with_no_file_id_is_skipped_and_logged(
        self, engine, caplog
    ):
        """Previously a missing id was papered over with a freshly minted
        ``file_{uuid4}``, which reported an id no row anywhere carries."""
        case = _case([])
        nameless = _attachment(True)
        del nameless["file_id"]

        with caplog.at_level(
            logging.WARNING, logger="faultmaven.core.investigation.milestone_engine"
        ):
            metadata = await _run(engine, case, [nameless])

        assert metadata.get("files_uploaded") is None
        assert metadata.get("novel_files_uploaded") is None
        assert any(
            "no file_id" in r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.WARNING
        )


class TestTheEngineMintsNoRows:
    """#1207/#1209 removed the destructive append; #1210 removed the minting
    that fed it. The engine is a reader of ``case.uploaded_files`` now.

    Driven with an id the aggregate does NOT hold — the one shape under which
    the old code appended — so this bites if the minting comes back.
    """

    async def test_no_row_is_minted_for_an_unknown_id(self, engine):
        case = _case([])

        await _run(engine, case, [_attachment(True, file_id="file_ffffffffff99")])

        assert case.uploaded_files == []

    async def test_the_id_is_still_reported(self, engine):
        """Reporting does not depend on minting a row to read the id off."""
        case = _case([])

        metadata = await _run(
            engine, case, [_attachment(True, file_id="file_ffffffffff99")]
        )

        assert metadata["files_uploaded"] == ["file_ffffffffff99"]
        assert metadata["novel_files_uploaded"] == ["file_ffffffffff99"]

    def test_the_minting_helper_is_gone(self, engine):
        """It had exactly one call site and read ``content_hash`` /
        ``content_type`` / ``uploaded_by`` that the metadata never carries —
        the subset row #1207 was filed for."""
        assert not hasattr(engine, "_create_uploaded_file_from_attachment")
