"""Regression set for #1207 — the engine minted a duplicate ``UploadedFile``.

``_process_response_structured`` appended a row for **every** attachment it was
handed. On a turn carrying a file the case already holds — which is every
byte-identical re-submission, because
``investigation_service._preprocess_attachment`` dedups upstream and hands the
engine the EXISTING row's ``file_id`` — that produced a second in-memory row for
one id.

The second row is a strict subset of the first: the attachment-metadata dict
carries ``file_id``/``filename``/``data_type``/``size``/``source_type``/
``summary``/``storage_ref`` and **not** ``content_hash``, ``content_type`` or
``uploaded_by``, so the duplicate carries ``None`` for all three and the CURRENT
turn for ``uploaded_at_turn``.

``_upsert_uploaded_files`` walks the aggregate in order, so the stripped row was
upserted second and won every column that is not ``COALESCE``d — nulling the
hash that per-case dedup matches on, destroying ``uploaded_by`` attribution, and
moving ``uploaded_at_turn`` to the re-upload's turn.

``known_ids`` was already being computed one line above the append; it was simply
never used to gate it. #1209 gated it — and because the gate is never open on the
production path, the minting was dead code, so #1210 removed it outright. These
pin the property either way: the engine adds no ``UploadedFile`` row of its own.
"""

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import InquiryResponse
from faultmaven.modules.case.contracts import Case, CaseState, UploadedFile

pytestmark = pytest.mark.unit

FILE_ID = "file_0e0e0e0e0e05"
ORIGINAL_TURN = 3
REUPLOAD_TURN = 5


@pytest.fixture
def engine():
    return MilestoneEngine.__new__(MilestoneEngine)


def _complete_row(file_id: str = FILE_ID) -> UploadedFile:
    """The row ``_preprocess_attachment`` already committed, fully populated."""
    return UploadedFile(
        file_id=file_id,
        filename="app.log",
        size_bytes=120,
        content_type="text/plain",
        content_hash="a" * 64,
        uploaded_at_turn=ORIGINAL_TURN,
        uploaded_at=datetime.now(timezone.utc),
        upload_source="file_upload",
        storage_ref="ref/app.log",
        uploaded_by="user_123",
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
    case.current_turn = REUPLOAD_TURN
    return case


def _attachment(file_id: str = FILE_ID, *, is_novel: bool) -> dict:
    """Exactly the shape ``investigation_service`` builds — note the three
    columns it does NOT carry, which is what made the duplicate destructive.

    ``is_novel`` is required rather than defaulted: it is the turn's answer to
    "did the case already hold this?", threaded from
    ``_PreprocessedAttachment.duplicate_of`` (#1210), and every case below has
    a definite one.
    """
    return {
        "file_id": file_id,
        "filename": "app.log",
        "data_type": "logs",
        "size": 120,
        "source_type": "file_upload",
        "summary": "Pod restart loop.",
        "storage_ref": "ref/app.log",
        "is_novel": is_novel,
    }


async def _run(engine, case, attachments):
    response = InquiryResponse(
        agent_response="ack",
        state_updates=InquiryResponse.InquiryStateUpdate(),
    )
    _, metadata = await engine._process_response_structured(
        case, "msg", response, attachments
    )
    return metadata


class TestKnownFileIdIsNotAppendedAgain:
    async def test_the_aggregate_keeps_one_row_for_the_id(self, engine):
        case = _case([_complete_row()])

        await _run(engine, case, [_attachment(is_novel=False)])

        rows = [f for f in case.uploaded_files if f.file_id == FILE_ID]
        assert len(rows) == 1, (
            f"engine appended {len(rows)} rows for one file_id; the stripped "
            "one is upserted second and wins every non-COALESCE'd column"
        )

    async def test_no_row_for_the_id_is_a_stripped_one(self, engine):
        """Asserted over EVERY row for the id, not the first.

        ``_upsert_uploaded_files`` walks the aggregate in order and the LAST
        write wins, so picking the first match would find the intact original
        and pass with the duplicate sitting right behind it — the shape that
        makes this defect invisible in the first place.
        """
        case = _case([_complete_row()])

        await _run(engine, case, [_attachment(is_novel=False)])

        rows = [f for f in case.uploaded_files if f.file_id == FILE_ID]
        assert [r.content_hash for r in rows] == ["a" * 64], (
            "a row carrying content_hash=None is upserted over the real hash; "
            "per-case dedup matches on that column"
        )
        assert [r.uploaded_by for r in rows] == [
            "user_123"
        ], "a NULL here is indistinguishable from a system upload"
        assert [r.content_type for r in rows] == ["text/plain"]

    async def test_no_row_for_the_id_carries_the_reupload_turn(self, engine):
        """``uploaded_at_turn`` is the citable name's key (#1198): when it moves,
        every citation the model already wrote names an item no longer there.

        Asserted over every row for the same reason as above."""
        case = _case([_complete_row()])

        await _run(engine, case, [_attachment(is_novel=False)])

        rows = [f for f in case.uploaded_files if f.file_id == FILE_ID]
        assert [r.uploaded_at_turn for r in rows] == [ORIGINAL_TURN]


class TestTheMetadataContractIsUnchanged:
    """Skipping the append must not change what the turn REPORTS. ``files_uploaded``
    is every attachment on the turn; ``novel_files_uploaded`` (#1136) is the subset
    the case did not already hold, and drives stall-net arming."""

    async def test_files_uploaded_still_names_the_attachment(self, engine):
        case = _case([_complete_row()])

        metadata = await _run(engine, case, [_attachment(is_novel=False)])

        assert metadata["files_uploaded"] == [FILE_ID]

    async def test_a_known_id_is_not_reported_novel(self, engine):
        case = _case([_complete_row()])

        metadata = await _run(engine, case, [_attachment(is_novel=False)])

        assert metadata.get("novel_files_uploaded", []) == []


class TestTheProductionOrdering:
    """The ordering the engine actually runs under.

    ``investigation_service`` appends the authoritative row to the SAME ``case``
    object inside ``_preprocess_attachment``, then builds the attachment
    metadata from it and calls ``process_turn``. There is no reload in between.

    An earlier draft of these tests seeded the aggregate WITHOUT the row and
    handed the engine a "novel" id — a state production never reaches. That
    made the engine's own novelty test look reachable, when under the real
    sequence it is False for every attachment. These drive the real sequence
    instead, which is why the fix had to come from upstream (#1210).
    """

    async def test_a_brand_new_upload_produces_exactly_one_row(self, engine):
        """The row `_preprocess_attachment` committed, and no second one."""
        novel = "file_ffffffffff99"
        case = _case([])
        case.uploaded_files.append(
            _complete_row(file_id=novel).model_copy(
                update={"uploaded_at_turn": REUPLOAD_TURN}
            )
        )

        await _run(engine, case, [_attachment(file_id=novel, is_novel=True)])

        rows = [f for f in case.uploaded_files if f.file_id == novel]
        assert len(rows) == 1
        assert rows[0].content_hash == "a" * 64
        assert rows[0].uploaded_by == "user_123"

    async def test_files_uploaded_names_a_brand_new_upload(self, engine):
        novel = "file_ffffffffff99"
        case = _case([])
        case.uploaded_files.append(
            _complete_row(file_id=novel).model_copy(
                update={"uploaded_at_turn": REUPLOAD_TURN}
            )
        )

        metadata = await _run(engine, case, [_attachment(file_id=novel, is_novel=True)])

        assert metadata["files_uploaded"] == [novel]

    async def test_a_brand_new_upload_is_reported_novel(self, engine):
        """This pinned ``novel_files_uploaded is None`` until #1210.

        Under the real ordering ``known_ids`` already holds the id, so the
        engine's own novelty test was False for a genuinely new file and
        #1136's stall-net arm for uploads never saw one. The answer now comes
        from upstream (``_PreprocessedAttachment.duplicate_of`` →
        ``is_novel``), which is the only place it survives the append.
        """
        novel = "file_ffffffffff99"
        case = _case([])
        case.uploaded_files.append(
            _complete_row(file_id=novel).model_copy(
                update={"uploaded_at_turn": REUPLOAD_TURN}
            )
        )

        metadata = await _run(engine, case, [_attachment(file_id=novel, is_novel=True)])

        assert metadata["novel_files_uploaded"] == [novel]


class TestProvenanceSurvives:
    """``upload_source`` is the FIFTH column the duplicate destroyed, and the
    one other modules branch on.

    ``investigation_service`` fabricates the metadata dict's ``source_type``
    from the filename prefix — ``"paste" if filename.startswith(
    "pasted-content-") else "file_upload"`` — so a PAGE CAPTURE arrives tagged
    ``file_upload``. The engine's duplicate carried that fabrication into
    ``upload_source``, and the non-COALESCE'd upsert made it win over the
    genuine tag ``_preprocess_attachment`` had set from ``source_metadata``.

    Not appending the duplicate leaves the genuine value in place, which fixes
    the PERSISTED half of #1201. The derivation at source is still wrong and
    stays with that issue.
    """

    async def test_a_page_captures_tag_is_not_overwritten_by_the_fabrication(
        self, engine
    ):
        capture = _complete_row().model_copy(
            update={
                "filename": "page-capture-20260709T105531.txt",
                "upload_source": "page_capture",
            }
        )
        case = _case([capture])

        await _run(
            engine,
            case,
            [
                _attachment(is_novel=False)
                | {
                    "filename": "page-capture-20260709T105531.txt",
                    # What investigation_service actually sends for a capture.
                    "source_type": "file_upload",
                }
            ],
        )

        rows = [f for f in case.uploaded_files if f.file_id == FILE_ID]
        assert [r.upload_source for r in rows] == ["page_capture"], (
            "the fabricated file_upload tag is upserted over the genuine "
            "page_capture one; #1201's persisted half"
        )


class TestThePersistedConsequence:
    """The end-to-end pin: the aggregate the engine produced, upserted through a
    real repository, must leave the row intact.

    The in-memory tests above pin the cause (a second row for one id); this pins
    the consequence the issue was filed for. It matters that this is driven
    through ``_upsert_uploaded_files`` rather than asserted about it — the
    destruction is a property of the ``ON CONFLICT DO UPDATE`` column list, and
    that list is not COALESCE'd for ``content_hash``, ``content_type``,
    ``uploaded_by`` or ``uploaded_at_turn``.
    """

    async def test_a_reupload_turn_leaves_the_persisted_row_intact(self, engine):
        from sqlalchemy import text as sa_text
        from sqlalchemy.ext.asyncio import (
            AsyncSession,
            async_sessionmaker,
            create_async_engine,
        )

        from faultmaven.infrastructure.persistence.models import Base
        from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
            SQLiteCaseRepository,
        )

        case = _case([_complete_row()])
        await _run(engine, case, [_attachment(is_novel=False)])

        db = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with db.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(db, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as session:
                repo = SQLiteCaseRepository(session)
                await repo._upsert_uploaded_files(
                    case.case_id, case.uploaded_files, "org_123"
                )
                row = (
                    await session.execute(
                        sa_text(
                            "SELECT content_hash, content_type, uploaded_by, "
                            "uploaded_at_turn, COUNT(*) FROM uploaded_files "
                            "WHERE file_id = :f"
                        ),
                        {"f": FILE_ID},
                    )
                ).first()

            content_hash, content_type, uploaded_by, turn, count = row
            assert count == 1
            assert content_hash == "a" * 64, (
                "content_hash was nulled -- find_uploaded_file_by_content_hash "
                "returns None for a byte-identical re-submission, so per-case "
                "dedup is dead and identical_to_prior_upload_at_turn with it"
            )
            assert content_type == "text/plain"
            assert uploaded_by == "user_123"
            assert turn == ORIGINAL_TURN
        finally:
            await db.dispose()
