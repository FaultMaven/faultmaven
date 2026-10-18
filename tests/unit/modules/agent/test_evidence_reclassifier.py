"""Phase 1.5 — EvidenceReclassifier

``reclassify_out_of_band`` (the PATCH route) and ``reclassify_in_turn`` (the
``reclassify_evidence`` tool) over ``apply_reclassification``: the reclassifier
the PATCH route and the tool share
(#1941). Covers the flow end-to-end: auth check, evidence lookup,
content-ref check, storage fetch, preprocessing re-run, and persistence.
The reclassifier runs on real objects (Case, Evidence). Most tests wire it
through MockCaseRepository, which hands back the stored object and stores
whatever is saved, so a test reads the persisted state back through it. The
out-of-band path applies to a deep copy of the case it loaded and saves that
copy; ``TestTheOutOfBandPathSavesACopy`` pins that on a real
``InMemoryCaseRepository``, whose ``get`` returns its stored object by
reference.
"""

import copy
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationException,
)
from faultmaven.models.api import DataType
from faultmaven.modules.agent.domain.services.investigation_service.reclassification import (  # noqa: E501
    EvidenceReclassifier,
    _handle_file_reclassification,
    _infer_source_type,
    _reclassified_collections,
)
from faultmaven.modules.case.domain.models.evidence import EvidenceSourceType
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)

from .conftest import (
    MockCaseRepository,
    create_sample_case,
    make_evidence,
    make_preprocessing_result,
    make_uploaded_file,
)


@pytest.fixture
def repo_with_case():
    repo = MockCaseRepository()
    case = create_sample_case(user_id="user_owner")
    case.evidence = [make_evidence()]
    case.uploaded_files = [make_uploaded_file()]
    repo._storage[case.case_id] = case
    return repo, case


@pytest.fixture
def preprocessing_service():
    svc = MagicMock()
    svc.reclassify_evidence = AsyncMock(
        return_value=make_preprocessing_result(
            new_data_type=DataType.LOGS_AND_ERRORS,
            metadata={
                "classification": {
                    "confidence": 1.0,
                    "source": "user_override",
                    "failed": False,
                    "suggested_types": [],
                },
                "extractor": {
                    "chosen_type": DataType.LOGS_AND_ERRORS.value,
                    "attempts": [
                        {
                            "data_type": DataType.METRICS_AND_PERFORMANCE.value,
                            "triggered_by": "initial",
                            "sanity_passed": True,
                            "duration_ms": 1,
                        },
                        {
                            "data_type": DataType.LOGS_AND_ERRORS.value,
                            "triggered_by": "user_override",
                            "sanity_passed": True,
                            "duration_ms": 2,
                        },
                    ],
                },
            },
        )
    )
    return svc


@pytest.fixture
def file_storage():
    svc = MagicMock()
    svc.retrieve_file = AsyncMock(return_value=b"line1\nline2 ERROR\nline3\n")
    return svc


@pytest.fixture
def reclassifier(repo_with_case, preprocessing_service, file_storage):
    repo, _ = repo_with_case
    return EvidenceReclassifier(
        case_repository=repo,
        file_storage_service=file_storage,
        preprocessing_service=preprocessing_service,
    )


class TestAuthAndLookup:
    @pytest.mark.asyncio
    async def test_case_not_found_raises(self, reclassifier):
        with pytest.raises(NotFoundError):
            await reclassifier.reclassify_out_of_band(
                case_id="nonexistent",
                evidence_id="ev_aaaaaaaaaaaa",
                user_id="user_owner",
                data_type=DataType.LOGS_AND_ERRORS,
            )

    @pytest.mark.asyncio
    async def test_user_not_owner_raises_authorization_error(
        self, reclassifier, repo_with_case
    ):
        """Non-owner caller raises AuthorizationError → HTTP 403."""
        _, case = repo_with_case
        with pytest.raises(AuthorizationError):
            await reclassifier.reclassify_out_of_band(
                case_id=case.case_id,
                evidence_id="ev_aaaaaaaaaaaa",
                user_id="user_not_owner",
                data_type=DataType.LOGS_AND_ERRORS,
            )

    @pytest.mark.asyncio
    async def test_evidence_not_in_case_raises(self, reclassifier, repo_with_case):
        _, case = repo_with_case
        with pytest.raises(NotFoundError):
            await reclassifier.reclassify_out_of_band(
                case_id=case.case_id,
                evidence_id="ev_does_not_exist_zz",
                user_id="user_owner",
                data_type=DataType.LOGS_AND_ERRORS,
            )

    @pytest.mark.asyncio
    async def test_evidence_without_content_ref_raises_conflict(
        self, reclassifier, repo_with_case
    ):
        """Evidence whose source file is no longer on the case has no stored
        raw file — re-extraction is impossible. Raises ConflictError
        with ``conflict_reason="no_backing_file"`` so clients can
        branch on the structured field instead of parsing the detail
        string.
        """
        _, case = repo_with_case
        case.evidence = [
            make_evidence(
                source_file_id="file_deadbeef0000",
                source_type=EvidenceSourceType.LOGS,
            )
        ]
        case.uploaded_files = []
        with pytest.raises(ConflictError) as exc:
            await reclassifier.reclassify_out_of_band(
                case_id=case.case_id,
                evidence_id="ev_aaaaaaaaaaaa",
                user_id="user_owner",
                data_type=DataType.LOGS_AND_ERRORS,
            )

        # Structured metadata is what the HTTP body surfaces — clients
        # branch on these, not on the detail string.
        assert exc.value.resource_type == "evidence"
        assert exc.value.resource_id == "ev_aaaaaaaaaaaa"
        assert exc.value.conflict_reason == "no_backing_file"


class TestTheDriverGate:
    """Reclassification is an investigation write: the case's effective
    DRIVER's, and only while they still read it (ADR-020 D2)."""

    @pytest.mark.asyncio
    async def test_the_creator_is_refused_while_another_drives(
        self, reclassifier, repo_with_case
    ):
        _, case = repo_with_case
        case.driver_id = "user_driver"
        with pytest.raises(AuthorizationError):
            await reclassifier.reclassify_out_of_band(
                case_id=case.case_id,
                evidence_id="ev_aaaaaaaaaaaa",
                user_id="user_owner",
                data_type=DataType.LOGS_AND_ERRORS,
                caller_reads_case=True,
            )

    @pytest.mark.asyncio
    async def test_the_assigned_driver_who_reads_the_case_reclassifies(
        self, reclassifier, repo_with_case
    ):
        _, case = repo_with_case
        case.driver_id = "user_driver"
        updated = await reclassifier.reclassify_out_of_band(
            case_id=case.case_id,
            evidence_id="ev_aaaaaaaaaaaa",
            user_id="user_driver",
            data_type=DataType.LOGS_AND_ERRORS,
            caller_reads_case=True,
        )
        assert updated.evidence_id == "ev_aaaaaaaaaaaa"

    @pytest.mark.asyncio
    async def test_an_assigned_driver_who_no_longer_reads_is_refused(
        self, reclassifier, repo_with_case
    ):
        """The PATCH route has no read check ahead of this 403; without the
        read fact a driver whose every share was withdrawn — before any release
        reached the row — would be admitted. The gate split must not widen
        it."""
        _, case = repo_with_case
        case.driver_id = "user_driver"
        with pytest.raises(AuthorizationError):
            await reclassifier.reclassify_out_of_band(
                case_id=case.case_id,
                evidence_id="ev_aaaaaaaaaaaa",
                user_id="user_driver",
                data_type=DataType.LOGS_AND_ERRORS,
            )

    @pytest.mark.asyncio
    async def test_inside_a_turn_the_driver_needs_no_separate_read_fact(
        self, reclassifier, repo_with_case
    ):
        """The turn route resolved the case through the read allowlist, so an
        in-flight case is the read fact."""
        _, case = repo_with_case
        case.driver_id = "user_driver"
        updated = await reclassifier.reclassify_in_turn(
            case_id=case.case_id,
            case=case,
            evidence_id="ev_aaaaaaaaaaaa",
            user_id="user_driver",
            data_type=DataType.LOGS_AND_ERRORS,
        )
        assert updated.evidence_id == "ev_aaaaaaaaaaaa"


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_reclassification_updates_file_and_evidence_source_type(
        self, reclassifier, repo_with_case, preprocessing_service
    ):
        """Post-010: structural_index / summary / data_type land on the
        backing UploadedFile (file-level metadata). Evidence.source_type
        is re-aligned so the agent sees a consistent picture. The LLM's
        own summary/extract fields on Evidence are not touched —
        reclassifying a file doesn't rewrite the claim built on it."""
        _, case = repo_with_case
        previous_source_type = case.evidence[0].source_type.value
        previous_summary = case.evidence[0].summary
        previous_extract = case.evidence[0].extract

        updated = await reclassifier.reclassify_out_of_band(
            case_id=case.case_id,
            evidence_id="ev_aaaaaaaaaaaa",
            user_id="user_owner",
            data_type=DataType.LOGS_AND_ERRORS,
        )

        # Evidence.source_type re-aligned with new data_type
        assert previous_source_type != updated.source_type.value

        # LLM-authored claim fields untouched on Evidence
        assert updated.summary == previous_summary
        assert updated.extract == previous_extract

        # File-level preprocessing artifacts landed on UploadedFile
        saved = await reclassifier.repository.get(case.case_id)
        uf = next(f for f in saved.uploaded_files if f.file_id == "file_aaaaaaaaaaaa")
        assert uf.structural_index == "new index content"
        assert uf.summary == "new summary"
        # The file row holds the fine-grained ``DataType`` (#583); the
        # Evidence row stays on the 6-valued projection of it.
        assert uf.data_type == DataType.LOGS_AND_ERRORS.value
        assert updated.source_type.value == "logs"

        # Preprocessing was called with user_override + previous metadata.
        kwargs = preprocessing_service.reclassify_evidence.call_args.kwargs
        assert kwargs["user_override"] == DataType.LOGS_AND_ERRORS

    @pytest.mark.asyncio
    async def test_metadata_contract_suppresses_low_confidence_marker(
        self, reclassifier, repo_with_case
    ):
        """After user_override, the new metadata has source='user_override'
        and confidence=1.0 — Phase 1's low-confidence marker must not
        fire on the updated row."""
        _, case = repo_with_case
        updated = await reclassifier.reclassify_out_of_band(
            case_id=case.case_id,
            evidence_id="ev_aaaaaaaaaaaa",
            user_id="user_owner",
            data_type=DataType.LOGS_AND_ERRORS,
        )
        assert updated.metadata is not None
        cls = updated.metadata["classification"]
        assert cls["source"] == "user_override"
        assert cls["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_attempts_history_preserved_on_row(
        self, reclassifier, repo_with_case
    ):
        """The merged attempts list makes it onto the persisted row so
        observability can see the classification trail."""
        _, case = repo_with_case
        updated = await reclassifier.reclassify_out_of_band(
            case_id=case.case_id,
            evidence_id="ev_aaaaaaaaaaaa",
            user_id="user_owner",
            data_type=DataType.LOGS_AND_ERRORS,
        )
        attempts = updated.metadata["extractor"]["attempts"]
        assert len(attempts) == 2
        assert attempts[0]["triggered_by"] == "initial"
        assert attempts[-1]["triggered_by"] == "user_override"

    @pytest.mark.asyncio
    async def test_other_evidence_untouched(self, reclassifier, repo_with_case):
        """Reclassifying one evidence must not touch the others in the case.
        Pin per-evidence idempotency so batch-style bugs can't corrupt
        adjacent rows."""
        _, case = repo_with_case
        other = make_evidence(
            evidence_id="ev_bbbbbbbbbbbb",
            data_type="structured_config",
            source_file_id="file_bbbbbbbbbbbb",
        )
        case.evidence.append(other)
        case.uploaded_files.append(
            make_uploaded_file(
                file_id="file_bbbbbbbbbbbb",
                filename="config.yaml",
                storage_ref="evidence/case_x/config.yaml",
            )
        )

        await reclassifier.reclassify_out_of_band(
            case_id=case.case_id,
            evidence_id="ev_aaaaaaaaaaaa",
            user_id="user_owner",
            data_type=DataType.LOGS_AND_ERRORS,
        )

        # Re-read via repo to see persisted state.
        saved = await reclassifier.repository.get(case.case_id)
        untouched = next(
            e for e in saved.evidence if e.evidence_id == "ev_bbbbbbbbbbbb"
        )
        assert untouched.source_type.value == EvidenceSourceType.CONFIGURATION.value
        assert untouched.extract == "old index"
        # The unrelated UploadedFile must also be untouched.
        untouched_file = next(
            f for f in saved.uploaded_files if f.file_id == "file_bbbbbbbbbbbb"
        )
        assert untouched_file.structural_index is None
        assert untouched_file.summary is None


_COUNTER = (
    "faultmaven.modules.agent.domain.services.investigation_service."
    "reclassification.EVIDENCE_RECLASSIFICATION_TOTAL"
)
_FILES_REFUSAL = (
    "Cannot reclassify files on a closed case — the investigation is "
    "terminal; only questions about the case are accepted."
)
_EVIDENCE_REFUSAL = (
    "Cannot reclassify evidence on a closed case — the investigation is "
    "terminal; only questions about the case are accepted."
)


def _close(repo, case):
    """The closed twin of *case*, stored in *repo* under the same id."""
    closed = case.model_copy(
        update={
            "state": CaseState.CLOSED,
            "closed_at": datetime.now(timezone.utc),
            "closure_reason": "closed_insufficient_evidence",
        }
    )
    repo._storage[closed.case_id] = closed
    assert closed.is_terminal
    return closed


class TestTheTerminalRefusalIsOneText:
    """R2: both seams refuse a closed case with a byte-exact message."""

    @pytest.mark.asyncio
    async def test_the_clarification_handler_says_files(
        self, repo_with_case, preprocessing_service, file_storage
    ):
        repo, case = repo_with_case
        case = _close(repo, case)
        with pytest.raises(ValidationException) as exc:
            await _handle_file_reclassification(
                file_storage,
                preprocessing_service,
                case=case,
                file_id="file_aaaaaaaaaaaa",
                data_type_value="logs_and_errors",
            )
        assert str(exc.value) == _FILES_REFUSAL
        assert exc.value.details == {"case_state": CaseState.CLOSED.value}

    @pytest.mark.asyncio
    async def test_the_out_of_band_path_says_evidence(
        self, reclassifier, repo_with_case
    ):
        repo, case = repo_with_case
        case = _close(repo, case)
        with pytest.raises(ValidationException) as exc:
            await reclassifier.reclassify_out_of_band(
                case.case_id, "ev_aaaaaaaaaaaa", "user_owner", DataType.LOGS_AND_ERRORS
            )
        assert str(exc.value) == _EVIDENCE_REFUSAL
        assert exc.value.details == {"case_state": CaseState.CLOSED.value}

    @pytest.mark.asyncio
    async def test_the_in_turn_path_says_evidence(self, reclassifier, repo_with_case):
        repo, case = repo_with_case
        case = _close(repo, case)
        with pytest.raises(ValidationException) as exc:
            await reclassifier.reclassify_in_turn(
                case.case_id,
                case,
                "ev_aaaaaaaaaaaa",
                "user_owner",
                DataType.LOGS_AND_ERRORS,
            )
        assert str(exc.value) == _EVIDENCE_REFUSAL


class TestTheGuardOrder:
    """R3: 403, then 404, then 422, on both entry points."""

    @staticmethod
    async def _both(reclassifier, case, evidence_id, user_id):
        out = reclassifier.reclassify_out_of_band(
            case.case_id, evidence_id, user_id, DataType.LOGS_AND_ERRORS
        )
        in_turn = reclassifier.reclassify_in_turn(
            case.case_id, case, evidence_id, user_id, DataType.LOGS_AND_ERRORS
        )
        return out, in_turn

    @pytest.mark.asyncio
    async def test_a_terminal_case_with_a_missing_evidence_id_is_404(
        self, reclassifier, repo_with_case
    ):
        repo, case = repo_with_case
        case = _close(repo, case)
        for call in await self._both(reclassifier, case, "ev_missing_zz", "user_owner"):
            with pytest.raises(NotFoundError) as exc:
                await call
            assert exc.value.resource_type == "Evidence"

    @pytest.mark.asyncio
    async def test_a_non_driver_on_a_terminal_case_is_403_before_the_404(
        self, reclassifier, repo_with_case
    ):
        repo, case = repo_with_case
        case = _close(repo, case)
        for call in await self._both(reclassifier, case, "ev_missing_zz", "user_other"):
            with pytest.raises(AuthorizationError):
                await call

    @pytest.mark.asyncio
    async def test_a_terminal_case_with_no_stored_file_is_422_before_the_409(
        self, reclassifier, repo_with_case
    ):
        """The terminal refusal is the guard's, ahead of the backing-file
        check in ``apply_reclassification``: a closed case answers 422 for
        evidence with no stored file, where an open one answers 409."""
        repo, case = repo_with_case
        case.evidence = [make_evidence(source_file_id="file_deadbeef0000")]
        case.uploaded_files = []
        # Control: open, the same row is the 409 on both entry points.
        for call in await self._both(
            reclassifier, case, "ev_aaaaaaaaaaaa", "user_owner"
        ):
            with pytest.raises(ConflictError) as conflict:
                await call
            assert conflict.value.conflict_reason == "no_backing_file"

        closed = _close(repo, case)
        for call in await self._both(
            reclassifier, closed, "ev_aaaaaaaaaaaa", "user_owner"
        ):
            with pytest.raises(ValidationException) as refused:
                await call
            assert not isinstance(refused.value, ConflictError)
            assert str(refused.value) == _EVIDENCE_REFUSAL


class TestTheCounterLabel:
    """R4: the method, not a parameter, picks the ``trigger`` label."""

    @pytest.mark.asyncio
    async def test_out_of_band_counts_api_once(self, reclassifier, repo_with_case):
        _, case = repo_with_case
        with patch(_COUNTER) as counter:
            await reclassifier.reclassify_out_of_band(
                case.case_id, "ev_aaaaaaaaaaaa", "user_owner", DataType.LOGS_AND_ERRORS
            )
        counter.labels.assert_called_once()
        assert counter.labels.call_args.kwargs["trigger"] == "api"
        counter.labels.return_value.inc.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_in_turn_counts_agent_tool_once(self, reclassifier, repo_with_case):
        _, case = repo_with_case
        with patch(_COUNTER) as counter:
            await reclassifier.reclassify_in_turn(
                case.case_id,
                case,
                "ev_aaaaaaaaaaaa",
                "user_owner",
                DataType.LOGS_AND_ERRORS,
            )
        counter.labels.assert_called_once()
        assert counter.labels.call_args.kwargs["trigger"] == "agent_tool"
        counter.labels.return_value.inc.assert_called_once_with()


class TestTheWriteMode:
    """R5: in-turn persists nothing and mutates the object it was given;
    out-of-band saves once, and what it saves is what the seam computes."""

    @pytest.mark.asyncio
    async def test_in_turn_touches_no_repository_and_mutates_in_place(
        self, reclassifier, repo_with_case
    ):
        repo, case = repo_with_case
        repo.get = AsyncMock(side_effect=AssertionError("in-turn must not load"))
        repo.save = AsyncMock(side_effect=AssertionError("in-turn must not save"))
        assert case.evidence[0].source_type is not EvidenceSourceType.LOGS
        assert case.uploaded_files[0].summary != "new summary"

        returned = await reclassifier.reclassify_in_turn(
            case.case_id,
            case,
            "ev_aaaaaaaaaaaa",
            "user_owner",
            DataType.LOGS_AND_ERRORS,
        )

        repo.get.assert_not_awaited()
        repo.save.assert_not_awaited()
        # The change is on the object passed in: the row the call returns IS
        # the row that object now holds, not a row of some copy.
        assert case.evidence[0] is returned
        assert case.evidence[0].source_type is EvidenceSourceType.LOGS
        assert case.uploaded_files[0].summary == "new summary"

    @pytest.mark.asyncio
    async def test_out_of_band_saves_once_what_the_seam_computes(
        self, reclassifier, repo_with_case, preprocessing_service
    ):
        repo, case = repo_with_case
        result = preprocessing_service.reclassify_evidence.return_value
        expected_files, expected_evidence, expected_suggestions = (
            _reclassified_collections(
                copy.deepcopy(case),
                "file_aaaaaaaaaaaa",
                result,
                _infer_source_type(result.detailed_data_type),
            )
        )
        real_save = repo.save
        repo.save = AsyncMock(side_effect=real_save)

        await reclassifier.reclassify_out_of_band(
            case.case_id, "ev_aaaaaaaaaaaa", "user_owner", DataType.LOGS_AND_ERRORS
        )

        repo.save.assert_awaited_once()
        saved = repo.save.await_args.args[0]
        assert saved.uploaded_files == expected_files
        assert saved.last_suggestions == expected_suggestions
        # The addressed row additionally takes the re-extraction's metadata
        # block; every other field of every row is the seam's.
        assert [e.model_dump(exclude={"metadata"}) for e in saved.evidence] == [
            e.model_dump(exclude={"metadata"}) for e in expected_evidence
        ]
        assert saved.evidence[0].metadata["classification"]["source"] == "user_override"


class TestTheOutOfBandPathSavesACopy:
    """The PATCH path applies to a deep copy of the case it loaded and saves
    that copy, never the object ``get`` returned. On a repository whose
    ``get`` hands back its stored object by reference, as
    ``InMemoryCaseRepository``'s does, that object may be a turn's, and a
    save that fails must leave the store as it was."""

    @staticmethod
    async def _stored(preprocessing_service, file_storage):
        repo = InMemoryCaseRepository()
        case = create_sample_case(user_id="user_owner")
        case.evidence = [make_evidence()]
        case.uploaded_files = [make_uploaded_file()]
        await repo.save(case)
        reclassifier = EvidenceReclassifier(
            case_repository=repo,
            file_storage_service=file_storage,
            preprocessing_service=preprocessing_service,
        )
        return repo, reclassifier, case.case_id

    @pytest.mark.asyncio
    async def test_a_reference_held_from_get_is_unchanged(
        self, preprocessing_service, file_storage
    ):
        repo, reclassifier, case_id = await self._stored(
            preprocessing_service, file_storage
        )
        held = await repo.get(case_id)
        before = held.model_dump()

        await reclassifier.reclassify_out_of_band(
            case_id, "ev_aaaaaaaaaaaa", "user_owner", DataType.LOGS_AND_ERRORS
        )

        assert held.model_dump() == before
        # Control: the reclassification was stored, as another object.
        stored = await repo.get(case_id)
        assert stored is not held
        assert stored.evidence[0].source_type is EvidenceSourceType.LOGS
        assert stored.uploaded_files[0].summary == "new summary"

    @pytest.mark.asyncio
    async def test_a_failed_save_leaves_the_stored_case_unchanged(
        self, preprocessing_service, file_storage
    ):
        repo, reclassifier, case_id = await self._stored(
            preprocessing_service, file_storage
        )
        stored = await repo.get(case_id)
        before = stored.model_dump()
        repo.save = AsyncMock(side_effect=RuntimeError("save failed"))

        with pytest.raises(RuntimeError, match="save failed"):
            await reclassifier.reclassify_out_of_band(
                case_id, "ev_aaaaaaaaaaaa", "user_owner", DataType.LOGS_AND_ERRORS
            )

        # Control: the call reached the save with the reclassification applied.
        repo.save.assert_awaited_once()
        attempted = repo.save.await_args.args[0]
        assert attempted.evidence[0].source_type is EvidenceSourceType.LOGS
        assert await repo.get(case_id) is stored
        assert stored.model_dump() == before
