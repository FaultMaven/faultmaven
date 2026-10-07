"""#1878 — an upload's ``uploaded_files`` row commits with the turn that carried it.

The invariant (owner ruling): a row exists only inside the commit that creates
the turn that carried it. So a file is listed, searchable (every file tool
resolves through ``case.uploaded_files``) and attributed to a committed turn
together, or none of these. A failed turn leaves no row, and its retry is a
fresh upload, never reported as a duplicate of its own failed attempt.

Driven through ``InvestigationService.process_turn`` on the REAL
``SQLiteCaseRepository`` over a file database, and read back through a separate
session — which sees only COMMITTED rows. A read on the writing session would
see its own uncommitted INSERT and pass with the commit deleted.

The engine double below models the engine route's Step 7 (``_persist_turn``):
it commits the aggregate before returning, as the real engine does, so
"after the commit" in these tests means after the engine's save AND the
service's final one.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import faultmaven
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.exceptions import ServiceException
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.storage.filesystem import FilesystemStorageBackend
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.agent.jobs.storage_cleanup import cleanup_orphaned_files
from faultmaven.modules.case.api.routes.evidence import list_uploaded_files
from faultmaven.modules.case.contracts import Case, CaseState
from faultmaven.modules.case.domain.models.problem import ProblemVerification
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.exceptions import StaleCaseException
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    SQLiteCaseRepository,
)
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    SIDECAR_SUFFIX,
    FileStorageService,
)

from .conftest import create_sample_case, make_preprocessing_result

pytestmark = pytest.mark.unit

LOG = b"07:40 ERROR 503 SSLException: Connection reset by peer"
OTHER_LOG = b"08:12 WARN pool exhausted: 64/64 connections in use"


def test_the_worktree_is_what_is_imported():
    """A probe that loads another checkout measures nothing."""
    assert (
        Path(faultmaven.__file__)
        .resolve()
        .is_relative_to(Path(__file__).resolve().parents[4])
    ), faultmaven.__file__


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _RecordingRepository(SQLiteCaseRepository):
    """The real repository, logging each COMMITTED save into ``calls``."""

    def __init__(self, session, calls: list):
        super().__init__(session)
        self._calls = calls

    async def save(self, case):
        result = await super().save(case)
        self._calls.append(("save", tuple(f.file_id for f in case.uploaded_files)))
        return result


class _Storage:
    """A storage double that records the order of its writes."""

    def __init__(self, calls: list, *, mark_linked_error: Optional[Exception] = None):
        self._calls = calls
        self._mark_linked_error = mark_linked_error
        self.stored: list[str] = []
        self.link_attempts: list[str] = []

    async def store_file(
        self, file_data, original_filename, enterprise_id, case_id, mime_type=None
    ):
        key = f"{enterprise_id}/{case_id}/{len(self.stored)}-{original_filename}"
        self.stored.append(key)
        self._calls.append(("store_file", key))
        return {"storage_key": key}

    async def mark_linked(self, storage_key: str) -> bool:
        self._calls.append(("mark_linked", storage_key))
        self.link_attempts.append(storage_key)
        if self._mark_linked_error is not None:
            raise self._mark_linked_error
        return True

    def marked(self) -> list[str]:
        """This turn's attempts — ``calls`` is shared by every turn."""
        return list(self.link_attempts)


class _Engine:
    """The engine route: Step 7 commits the aggregate before returning.

    Records what each turn's ``case`` held on entry (the aggregate the prompt is
    built from) and the attachment metadata it was handed.
    """

    def __init__(
        self,
        repository,
        *,
        fail: Optional[Exception] = None,
        before_commit=None,
    ):
        self._repository = repository
        self._fail = fail
        self._before_commit = before_commit
        self.deps = SimpleNamespace(llm_provider=MagicMock())
        self.files_on_entry: list[list[str]] = []
        self.attachments: list[Optional[list[dict[str, Any]]]] = []
        self.process_turn = AsyncMock(side_effect=self._process_turn)

    async def _process_turn(
        self,
        case,
        user_message,
        attachments=None,
        intent_type=None,
        intent_data=None,
        user_id=None,
        typed=False,
    ):
        self.files_on_entry.append([f.filename for f in case.uploaded_files])
        self.attachments.append(attachments)
        if self._fail is not None:
            raise self._fail
        if self._before_commit is not None:
            await self._before_commit(case)
        await self._repository.save(case)  # Step 7, ``_persist_turn``
        return {
            "case_updated": case,
            "agent_response": f"Looked at: {user_message}",
            "metadata": {"milestones_completed": [], "progress_made": True},
        }


def _preprocessing():
    """Extraction whose content hash is the real hash of the bytes, so dedup
    tells different files apart and matches identical ones."""

    async def _classify(content, filename=None, source_metadata=None):
        result = make_preprocessing_result()
        result.content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return result

    service = MagicMock()
    service.classify_and_extract = AsyncMock(side_effect=_classify)
    return service


def _attachment(content: bytes = LOG, filename: str = "app.log") -> Attachment:
    return Attachment(content=content, filename=filename, content_type="text/plain")


# ---------------------------------------------------------------------------
# The world: one case on a file database
# ---------------------------------------------------------------------------


class _World:
    def __init__(self, sessions, case: Case):
        self.sessions = sessions
        self.case_id = case.case_id
        self.user_id = case.user_id
        self.calls: list = []

    async def turn(
        self,
        query: Optional[str] = None,
        attachments: Optional[list[Attachment]] = None,
        *,
        storage=None,
        fail: Optional[Exception] = None,
        before_commit=None,
        intent: Optional[QueryIntent] = None,
        engine_factory=None,
    ):
        """One request: a fresh session and repository, as the route has."""
        storage = storage if storage is not None else _Storage(self.calls)
        async with self.sessions() as session:
            repository = _RecordingRepository(session, self.calls)
            engine = (
                engine_factory(repository)
                if engine_factory is not None
                else _Engine(repository, fail=fail, before_commit=before_commit)
            )
            service = InvestigationService(
                milestone_engine=engine,
                case_repository=repository,
                preprocessing_service=_preprocessing(),
                file_storage_service=storage,
            )
            response = await service.process_turn(
                case_id=self.case_id,
                user_id=self.user_id,
                payload=TurnPayload(
                    query=query, attachments=attachments or [], intent=intent
                ),
            )
        return response, engine, storage

    async def committed(self) -> Case:
        async with self.sessions() as other:
            return await SQLiteCaseRepository(other).get(self.case_id)

    async def listed(self) -> list:
        """``GET /cases/{id}/uploaded-files``, through the route function."""

        async def _get_case(case_id, user_id):
            return await self.committed()

        page = await list_uploaded_files(
            self.case_id,
            Response(),
            limit=50,
            offset=0,
            sort_by="uploaded_at_turn",
            sort_order="desc",
            case_service=SimpleNamespace(get_case=_get_case),
            current_user=SimpleNamespace(user_id=self.user_id),
        )
        return page.files


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'fm-1878.db'}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _world(sessions, *, current_turn: int = 0, case: Optional[Case] = None):
    case = case or create_sample_case(current_turn=current_turn)
    async with sessions() as session:
        await SQLiteCaseRepository(session).save(case)
    return _World(sessions, case)


# ---------------------------------------------------------------------------
# A failed turn leaves nothing; its retry is a fresh upload
# ---------------------------------------------------------------------------


class TestAFailedUploadTurnLeavesNoRow:
    async def test_1_the_failed_turn_commits_no_row_and_marks_nothing_linked(
        self, sessions
    ):
        world = await _world(sessions, current_turn=5)

        with pytest.raises(ServiceException):
            _, _, storage = await world.turn(
                "here are the logs",
                [_attachment()],
                fail=RuntimeError("LLM provider exploded"),
            )

        committed = await world.committed()
        assert committed.uploaded_files == []
        assert await world.listed() == []
        assert [k for k, _ in world.calls].count("store_file") == 1
        assert "mark_linked" not in [k for k, _ in world.calls], (
            "a blob marked linked with no row behind it is exempt from the "
            "orphan sweep for good"
        )
        assert committed.current_turn == 5

    async def test_2_a_later_text_turn_neither_lists_nor_prompts_the_file(
        self, sessions
    ):
        world = await _world(sessions, current_turn=5)
        with pytest.raises(ServiceException):
            await world.turn(
                "here are the logs",
                [_attachment()],
                fail=RuntimeError("LLM provider exploded"),
            )

        _, engine, _ = await world.turn("any idea what is wrong?")

        assert engine.files_on_entry == [
            []
        ], "the text turn's engine saw a file the user was told failed"
        assert engine.attachments == [None]
        committed = await world.committed()
        assert committed.current_turn == 6
        assert committed.uploaded_files == []
        assert await world.listed() == []

    async def test_3_the_retry_is_a_fresh_upload_at_its_own_turn(self, sessions):
        world = await _world(sessions, current_turn=5)
        with pytest.raises(ServiceException):
            await world.turn(
                "here are the logs",
                [_attachment()],
                fail=RuntimeError("LLM provider exploded"),
            )
        await world.turn("any idea what is wrong?")

        response, engine, _ = await world.turn("here are the logs", [_attachment()])

        [result] = response.attachments_processed
        assert result.duplicate_of is None
        assert result.duplicate_turn is None
        assert result.processing_status == "completed"
        assert engine.attachments[0][0]["is_novel"] is True
        [row] = (await world.committed()).uploaded_files
        assert row.file_id == result.file_id
        assert row.uploaded_at_turn == 7
        assert [k for k, _ in world.calls].count("store_file") == 2

    async def test_the_issue_reproduction(self, sessions):
        """#1878's own steps: committed turns 1-5, a failed upload, a text turn
        that commits as 6. The file used to be listed at turn 6 — a turn that
        did not carry it — and a re-upload was reported as its duplicate."""
        world = await _world(sessions, current_turn=5)

        with pytest.raises(ServiceException):
            await world.turn(
                "here are the logs",
                [_attachment()],
                fail=RuntimeError("LLM provider exploded"),
            )
        await world.turn("any idea what is wrong?")

        assert (await world.committed()).current_turn == 6
        assert await world.listed() == [], "a file listed against turn 6"

        response, _, _ = await world.turn("here are the logs again", [_attachment()])
        [result] = response.attachments_processed
        assert (result.duplicate_of, result.duplicate_turn) == (None, None)
        [listed] = await world.listed()
        assert listed.uploaded_at_turn == 7

    async def test_8_an_occ_conflict_at_the_commit_leaves_no_row(self, sessions):
        world = await _world(sessions, current_turn=2)

        async def _a_concurrent_writer_wins(case):
            async with sessions() as other:
                repo = SQLiteCaseRepository(other)
                concurrent = await repo.get(case.case_id)
                concurrent.title = "a concurrent writer won"
                await repo.save(concurrent)

        with pytest.raises(StaleCaseException):
            await world.turn(
                "here are the logs",
                [_attachment()],
                before_commit=_a_concurrent_writer_wins,
            )

        committed = await world.committed()
        assert committed.uploaded_files == []
        assert committed.current_turn == 2
        assert "mark_linked" not in [k for k, _ in world.calls]
        assert "store_file" in [
            k for k, _ in world.calls
        ], "positive control: the conflict happened after the bytes were stored"


# ---------------------------------------------------------------------------
# A successful turn: the row lands at N, and the sidecar flips only after
# ---------------------------------------------------------------------------


class TestASuccessfulUploadTurn:
    async def test_4_row_at_n_and_mark_linked_after_the_commit(self, sessions):
        world = await _world(sessions, current_turn=3)

        response, _, storage = await world.turn("here are the logs", [_attachment()])

        [result] = response.attachments_processed
        [row] = (await world.committed()).uploaded_files
        assert row.file_id == result.file_id
        assert row.uploaded_at_turn == 4
        [listed] = await world.listed()
        assert listed.uploaded_at_turn == 4

        kinds = [k for k, _ in world.calls]
        assert storage.marked() == [row.storage_ref]
        last_save = max(i for i, k in enumerate(kinds) if k == "save")
        assert (
            kinds.index("mark_linked") > last_save
        ), f"mark_linked ran before the turn's final commit: {kinds}"
        first_save_with_row = next(
            i
            for i, (k, files) in enumerate(world.calls)
            if k == "save" and row.file_id in files
        )
        assert kinds.index("mark_linked") > first_save_with_row

    async def test_5_a_mark_linked_failure_does_not_fail_the_committed_turn(
        self, sessions, caplog
    ):
        world = await _world(sessions, current_turn=3)
        storage = _Storage(
            world.calls, mark_linked_error=RuntimeError("s3 timeout on the sidecar")
        )

        with caplog.at_level(logging.WARNING):
            response, _, _ = await world.turn(
                "here are the logs", [_attachment()], storage=storage
            )

        assert response.agent_response.startswith("Looked at")
        assert storage.marked() == storage.stored, "positive control: it was tried"
        assert any(
            "mark_linked failed" in r.getMessage() and "s3 timeout" in r.getMessage()
            for r in caplog.records
        )
        [row] = (await world.committed()).uploaded_files
        assert row.uploaded_at_turn == 4

    async def test_6_two_identical_files_in_one_submission_store_once(self, sessions):
        world = await _world(sessions, current_turn=1)

        response, engine, storage = await world.turn(
            "both copies", [_attachment(), _attachment(filename="app-copy.log")]
        )

        first, second = response.attachments_processed
        assert storage.stored and len(storage.stored) == 1
        assert first.duplicate_of is None
        assert second.duplicate_of == first.file_id
        assert second.duplicate_turn == 2
        assert [a["is_novel"] for a in engine.attachments[0]] == [True, False]
        [row] = (await world.committed()).uploaded_files
        assert (row.file_id, row.uploaded_at_turn) == (first.file_id, 2)
        assert storage.marked() == storage.stored

    async def test_7_a_reupload_on_a_later_turn_names_the_original(self, sessions):
        world = await _world(sessions, current_turn=1)
        response, _, _ = await world.turn("here are the logs", [_attachment()])
        [original] = response.attachments_processed
        await world.turn("anything else?")

        response, engine, storage = await world.turn(
            "sending it again", [_attachment()]
        )

        [again] = response.attachments_processed
        assert again.duplicate_of == original.file_id
        assert again.duplicate_turn == 2
        assert engine.attachments[0][0]["is_novel"] is False
        assert storage.stored == []
        assert storage.marked() == []
        assert [f.file_id for f in (await world.committed()).uploaded_files] == [
            original.file_id
        ]

    async def test_different_files_in_one_submission_are_both_stored(self, sessions):
        """The control for #6: the in-memory check matches on content, not on
        being in the same submission."""
        world = await _world(sessions, current_turn=1)

        response, _, storage = await world.turn(
            "two logs",
            [_attachment(), _attachment(OTHER_LOG, filename="pool.log")],
        )

        assert len(storage.stored) == 2
        assert all(r.duplicate_of is None for r in response.attachments_processed)
        assert len((await world.committed()).uploaded_files) == 2


# ---------------------------------------------------------------------------
# A deterministic route commits the row at its own (earlier) save
# ---------------------------------------------------------------------------


def _investigating_case() -> Case:
    case = Case(
        case_id="case_1878deadbeef",
        title="Gate turn carrying a new file",
        state=CaseState.INQUIRY,
        user_id="user_1878",
        enterprise_id="00000000-0000-0000-0000-000000000002",
        description="etcdInsufficientMembers alerts",
        problem_verification=ProblemVerification(
            symptom_statement="recurring etcdInsufficientMembers alerts",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "etcd connectivity"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 7
    return case


class TestADeterministicRouteCommitsTheRowWithItsTurn:
    async def test_10_a_dropdown_close_carrying_a_file(self, sessions):
        """``status_transition`` → closed with nothing pending: the real engine
        proposes the close on a deterministic branch and saves the case there
        (``transition_turns._close_on_explicit_intent``), never reaching the
        LLM. The upload row rides that save."""
        world = await _world(sessions, case=_investigating_case())

        def _real_engine(repository):
            engine = MilestoneEngine(
                MagicMock(), repository, investigation_tools=MagicMock()
            )
            engine.generator.generate_structured_output = AsyncMock(
                side_effect=AssertionError("the deterministic branch reached the LLM")
            )
            return engine

        response, engine, storage = await world.turn(
            "closing this out",
            [_attachment()],
            intent=QueryIntent(type=IntentType.STATUS_TRANSITION, to_state="closed"),
            engine_factory=_real_engine,
        )

        assert not engine.generator.generate_structured_output.called
        [result] = response.attachments_processed
        saves = [files for kind, files in world.calls if kind == "save"]
        assert len(saves) >= 2, "the engine's own save and the service's"
        assert (
            result.file_id in saves[0]
        ), "the deterministic branch's save did not carry the turn's upload"
        [row] = (await world.committed()).uploaded_files
        assert (row.file_id, row.uploaded_at_turn) == (result.file_id, 8)
        assert storage.marked() == storage.stored


# ---------------------------------------------------------------------------
# The orphan sweep reclaims what a failed turn stored
# ---------------------------------------------------------------------------


def _age_sidecar(storage_root: Path, key: str, hours: int = 48) -> None:
    sidecar = storage_root / f"{key}{SIDECAR_SUFFIX}"
    payload = json.loads(sidecar.read_text())
    payload["uploaded_at"] = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    sidecar.write_text(json.dumps(payload))


class TestTheSweepReclaimsAFailedTurnsBytes:
    async def test_9_the_failed_turns_blob_is_deleted_and_the_committed_one_kept(
        self, sessions, tmp_path
    ):
        """End to end on the real ``FileStorageService``: one committed upload,
        one failed one, both past the TTL. The sweep reads its reference set
        from the real repository, as the job does."""
        storage_root = tmp_path / "blobs"
        storage_root.mkdir()
        storage = FileStorageService(
            backend=FilesystemStorageBackend(storage_root=str(storage_root))
        )
        world = await _world(sessions, current_turn=1)

        await world.turn(
            "pool log", [_attachment(OTHER_LOG, "pool.log")], storage=storage
        )
        [kept_row] = (await world.committed()).uploaded_files
        with pytest.raises(ServiceException):
            await world.turn(
                "here are the logs",
                [_attachment()],
                storage=storage,
                fail=RuntimeError("LLM provider exploded"),
            )

        keys, strays = await storage.survey_sidecars()
        assert strays == []
        doomed = [k for k in keys if k != kept_row.storage_ref]
        assert len(doomed) == 1, keys
        for key in keys:
            _age_sidecar(storage_root, key)
        assert (
            json.loads((storage_root / f"{doomed[0]}{SIDECAR_SUFFIX}").read_text())[
                "linked"
            ]
            is False
        )

        async with sessions() as other:
            referenced = set(await SQLiteCaseRepository(other).list_all_storage_refs())
        result = await cleanup_orphaned_files(
            storage=storage, ttl_hours=24, dry_run=False, referenced_refs=referenced
        )

        assert result["deleted"] == 1
        assert not (storage_root / doomed[0]).exists()
        assert (storage_root / kept_row.storage_ref).exists()
