"""The one seam every reclassification crosses (#1465, #1470, #1471).

Two surfaces reclassify a file: the turn-seam clarification click
(``_handle_file_reclassification``) and the out-of-band
``PATCH /evidence/{id}/classification`` / ``reclassify_evidence`` agent tool
(``reclassify_evidence``). They shared ``_file_row_with_reclassification`` for
the file row and nothing else, and the three defects this suite pins are all
the same shape — a half of "reclassify" that only one of them did, or that
neither did:

* **#1470** the out-of-band path re-aligned ONE Evidence row; the turn seam
  looped every row backed by the file. One file, two claims, two contradictory
  source types.
* **#1471** neither refreshed the coverage window, so a reclassified file kept
  the span the extractor it just replaced had parsed.
* **#1465** the out-of-band path always load-mutate-saved, which is a lost
  update when the agent tool calls it from inside a turn — the turn's own
  end-of-turn aggregate save writes the pre-reclassification case back over it.

The first two are pinned by driving BOTH paths and asserting they agree, since
"each remembers" is the property that failed. The third is pinned through
``process_turn`` and the real tool, because it is an ORDERING defect: the write
is correct in isolation and only wrong relative to a save that happens
elsewhere, so a direct call to the service cannot see it.

All three ship behind ``FAULTMAVEN_RECLASSIFY_ENABLED``, off by default.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from faultmaven.core.investigation.coverage_trust import (
    CALLER_DECLARED_COVERAGE_SOURCE,
    is_vouched,
)
from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    _evidence_coverage,
)
from faultmaven.core.investigation.prompts.context_builder import _confidence_marker
from faultmaven.core.investigation.schemas import TurnPayload
from faultmaven.core.investigation.suggestion_liveness import is_clarification_entry
from faultmaven.exceptions import NotFoundError, ValidationException
from faultmaven.models.api import DataType
from faultmaven.models.api_models import IntentType
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
    _reclassified_collections,
)
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.reclassify_evidence_tool import (
    ReclassifyEvidenceTool,
)
from faultmaven.modules.case.domain.models import CaseState, EvidenceSourceType

from .conftest import (
    MockCaseRepository,
    create_sample_case,
    make_evidence,
    make_preprocessing_result,
    make_uploaded_file,
)

pytestmark = pytest.mark.unit

OWNER = "user_owner"
FILE_A = "file_aaaaaaaaaaaa"
EV_1 = "ev_aaaaaaaaaaaa"
EV_2 = "ev_bbbbbbbbbbbb"

#: The window the file carries BEFORE reclassification, and the one the new
#: extractor parses. They differ on purpose: a fixture whose file has no
#: parseable timestamps cannot tell a refreshed ``None`` from a stale one.
OLD_START = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
OLD_END = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
NEW_START = datetime(2026, 5, 5, 3, 0, tzinfo=timezone.utc)
NEW_END = datetime(2026, 5, 5, 4, 0, tzinfo=timezone.utc)


class _HydratingCaseRepository(MockCaseRepository):
    """A double whose ``get`` returns a fresh object, as the real ones do.

    ``MockCaseRepository`` hands back the very object it stored, so a caller
    that loads a case it is already holding gets the SAME instance — and an
    in-place write to one is an in-place write to the other. That identity is
    a property of the double, not of ``SQLiteCaseRepository`` or
    ``PostgreSQLHybridCaseRepository``, which hydrate a ``Case`` from rows on
    every read.

    It matters here because #1465 is a lost update between two handles on one
    case. Measured: with the plain double, deleting the line that binds the
    in-flight case still passed every assertion in this file — the load
    returned the in-flight object anyway. Against a repository that models the
    read, it does not.
    """

    async def _get(self, case_id):
        stored = self._storage.get(case_id)
        return stored.model_copy(deep=True) if stored is not None else None


def _clarification_entry(file_id=FILE_A):
    """A ``last_suggestions`` entry in the shape the write site persists."""
    return {
        "label": f"Application logs ({file_id})",
        "action_type": "DECIDE",
        "payload": f"Treat {file_id} as application logs.",
        "body": "Treat as application logs.",
        "intent": {
            "type": IntentType.FILE_RECLASSIFICATION.value,
            "file_id": file_id,
            "data_type": "logs_and_errors",
        },
        "offered_turn": 1,
        "offered_data_type": "metrics",
    }


def _build(
    *,
    evidence_rows=1,
    old_coverage=(OLD_START, OLD_END, "epoch_s"),
    new_coverage=(NEW_START, NEW_END, "iso8601"),
    new_data_type=DataType.LOGS_AND_ERRORS,
    armed=False,
):
    """A case whose one file backs ``evidence_rows`` Evidence rows.

    Two rows is the configuration #1470 is about and the one the pre-existing
    fixtures did not have: the ordinary case once the LLM has anchored two
    claims on the same upload.
    """
    repo = _HydratingCaseRepository()
    case = create_sample_case(user_id=OWNER)
    uploaded = make_uploaded_file(data_type="metrics")
    uploaded.coverage_start_ts, uploaded.coverage_end_ts, uploaded.coverage_source = (
        old_coverage
    )
    case.uploaded_files = [uploaded]
    case.evidence = [make_evidence(evidence_id=EV_1)]
    if evidence_rows > 1:
        case.evidence.append(make_evidence(evidence_id=EV_2))
    if armed:
        case.last_suggestions = [_clarification_entry()]
    repo._storage[case.case_id] = case

    start_ts, end_ts, source = new_coverage
    preprocessing = MagicMock()
    preprocessing.reclassify_evidence = AsyncMock(
        return_value=make_preprocessing_result(
            new_data_type=new_data_type,
            coverage_start_ts=start_ts,
            coverage_end_ts=end_ts,
            coverage_source=source,
        )
    )
    storage = MagicMock()
    storage.retrieve_file = AsyncMock(return_value=b"line1\nline2 ERROR\n")

    engine = create_autospec(MilestoneEngine, instance=True)
    engine.llm_provider = MagicMock()
    service = InvestigationService(
        milestone_engine=engine,
        case_repository=repo,
        preprocessing_service=preprocessing,
        file_storage_service=storage,
    )
    return SimpleNamespace(repo=repo, case=case, service=service, engine=engine)


async def _out_of_band(rig, evidence_id=EV_1):
    """The ``PATCH /evidence/{id}/classification`` path. Returns the saved case."""
    await rig.service.reclassify_evidence(
        case_id=rig.case.case_id,
        evidence_id=evidence_id,
        user_id=OWNER,
        data_type=DataType.LOGS_AND_ERRORS,
    )
    return await rig.repo.get(rig.case.case_id)


async def _turn_seam(rig):
    """The clarification-click path. Returns the case it hands back."""
    result = await rig.service._handle_file_reclassification(
        case=rig.case,
        file_id=FILE_A,
        data_type_value=DataType.LOGS_AND_ERRORS.value,
    )
    return result["case_updated"]


class TestEveryRowBehindTheFileIsRealigned:
    """#1470 — one file has one classification, however many claims cite it."""

    @pytest.mark.asyncio
    async def test_the_out_of_band_path_realigns_the_sibling_row(self):
        rig = _build(evidence_rows=2)
        # Positive control: both rows start on the type the file is leaving,
        # so an assertion that they end up equal cannot pass by standing still.
        assert {ev.source_type for ev in rig.case.evidence} == {
            EvidenceSourceType.METRICS
        }

        saved = await _out_of_band(rig)

        assert saved.uploaded_files[0].data_type == EvidenceSourceType.LOGS.value
        assert {ev.evidence_id: ev.source_type for ev in saved.evidence} == {
            EV_1: EvidenceSourceType.LOGS,
            EV_2: EvidenceSourceType.LOGS,
        }, (
            "the addressed row moved and the sibling did not — one file "
            "described by two contradictory source types (#1470)"
        )

    @pytest.mark.asyncio
    async def test_the_turn_seam_realigns_the_sibling_row(self):
        rig = _build(evidence_rows=2)
        updated = await _turn_seam(rig)
        assert {ev.source_type for ev in updated.evidence} == {EvidenceSourceType.LOGS}

    @pytest.mark.asyncio
    async def test_both_paths_leave_the_case_in_the_same_shape(self):
        """The property that failed is AGREEMENT, so it is what is asserted.

        Each path tested alone can be green while the two disagree — that is
        exactly how #1470 survived. Two identical cases, one path each, and
        the collections compared field by field.
        """
        oob = _build(evidence_rows=2)
        seam = _build(evidence_rows=2)

        saved = await _out_of_band(oob)
        updated = await _turn_seam(seam)

        # ``uploaded_at`` / ``collected_at`` are wall clocks stamped by the
        # fixtures' ``default_factory`` when each case was BUILT, so the two
        # rigs differ there for reasons that have nothing to do with
        # reclassification. Everything else is compared.
        assert [
            f.model_dump(exclude={"uploaded_at"}) for f in saved.uploaded_files
        ] == [f.model_dump(exclude={"uploaded_at"}) for f in updated.uploaded_files]
        # ``metadata`` is the one deliberate difference: the out-of-band path
        # stamps the addressed row with the re-extraction's evidence_metadata
        # (the classification confidence and extractor-attempt trail for the
        # request made about THAT row); the click answers a file-level
        # question and records nothing per claim.
        _skip = {"metadata", "collected_at"}
        assert [e.model_dump(exclude=_skip) for e in saved.evidence] == [
            e.model_dump(exclude=_skip) for e in updated.evidence
        ]
        # Positive control: the comparison still reaches the field the two
        # paths disagreed on, rather than having excluded its way to green.
        assert all("source_type" in e.model_dump(exclude=_skip) for e in saved.evidence)

    @pytest.mark.asyncio
    async def test_the_siblings_claim_content_is_untouched(self):
        """Re-aligning what a row was read FROM does not rewrite what it says."""
        rig = _build(evidence_rows=2)
        before = {e.evidence_id: (e.summary, e.extract) for e in rig.case.evidence}

        saved = await _out_of_band(rig)

        assert {e.evidence_id: (e.summary, e.extract) for e in saved.evidence} == before

    @pytest.mark.asyncio
    async def test_a_row_on_another_file_is_left_alone(self):
        """The fan-out is keyed on the file, not applied to the whole case."""
        rig = _build(evidence_rows=1)
        rig.case.evidence.append(
            make_evidence(
                evidence_id="ev_cccccccccccc",
                data_type="structured_config",
                source_file_id="file_bbbbbbbbbbbb",
            )
        )
        rig.case.uploaded_files.append(
            make_uploaded_file(
                file_id="file_bbbbbbbbbbbb",
                filename="config.yaml",
                storage_ref="evidence/case_x/config.yaml",
            )
        )

        saved = await _out_of_band(rig)

        other = next(e for e in saved.evidence if e.evidence_id == "ev_cccccccccccc")
        assert other.source_type == EvidenceSourceType.CONFIGURATION


class TestTheCoverageWindowMovesWithTheExtractor:
    """#1471 — a reclassified file must not keep the span it no longer has."""

    @pytest.mark.asyncio
    async def test_the_out_of_band_path_refreshes_the_window(self):
        rig = _build()
        # Positive control: the two windows differ, or "it moved" is unfalsifiable.
        assert (rig.case.uploaded_files[0].coverage_start_ts, NEW_START) == (
            OLD_START,
            NEW_START,
        )
        assert OLD_START != NEW_START

        saved = await _out_of_band(rig)

        uploaded = saved.uploaded_files[0]
        assert (uploaded.coverage_start_ts, uploaded.coverage_end_ts) == (
            NEW_START,
            NEW_END,
        )
        assert uploaded.coverage_source == "iso8601", (
            "the span moved without its provenance — coverage_trust reads "
            "this to decide whether the instant may be STATED at all"
        )

    @pytest.mark.asyncio
    async def test_the_turn_seam_refreshes_the_window(self):
        rig = _build()
        updated = await _turn_seam(rig)
        uploaded = updated.uploaded_files[0]
        assert (
            uploaded.coverage_start_ts,
            uploaded.coverage_end_ts,
            uploaded.coverage_source,
        ) == (NEW_START, NEW_END, "iso8601")

    @pytest.mark.asyncio
    async def test_a_window_the_new_extractor_cannot_support_is_cleared(self):
        """``logs_and_errors`` → ``code``: a source file keeps no log window.

        Clearing is the honest read. Every consumer treats a present span as
        fact, so a window nothing supports any more is worse than none.
        """
        rig = _build(
            new_coverage=(None, None, None),
            new_data_type=DataType.STRUCTURED_CONFIG,
        )
        saved = await _out_of_band(rig)
        uploaded = saved.uploaded_files[0]
        assert (
            uploaded.coverage_start_ts,
            uploaded.coverage_end_ts,
            uploaded.coverage_source,
        ) == (None, None, None)

    @pytest.mark.asyncio
    async def test_a_caller_declared_instant_survives_a_re_extraction(self):
        """The one provenance a re-extraction may not overwrite.

        ``caller_declared`` was never read out of the content — it is the
        forwarding client's statement about when it SAW the content, seeded
        at intake precisely because the content parsed to nothing. Re-parsing
        the same bytes under a different data type cannot refute it, and
        clearing it would delete the only temporal signal an alert
        notification pasted from Slack has.
        """
        observed = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)
        rig = _build(
            old_coverage=(observed, observed, CALLER_DECLARED_COVERAGE_SOURCE),
            new_coverage=(None, None, None),
        )
        saved = await _out_of_band(rig)
        uploaded = saved.uploaded_files[0]
        assert (
            uploaded.coverage_start_ts,
            uploaded.coverage_end_ts,
            uploaded.coverage_source,
        ) == (observed, observed, CALLER_DECLARED_COVERAGE_SOURCE)

    @pytest.mark.asyncio
    async def test_parsed_content_still_beats_a_caller_declared_instant(self):
        """Same precedence intake applies, in the same order."""
        observed = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)
        rig = _build(old_coverage=(observed, observed, CALLER_DECLARED_COVERAGE_SOURCE))
        saved = await _out_of_band(rig)
        uploaded = saved.uploaded_files[0]
        assert (uploaded.coverage_start_ts, uploaded.coverage_source) == (
            NEW_START,
            "iso8601",
        )


class _Prep:
    reclassify_enabled = True
    confidence_marker_enabled = False


class _Settings:
    preprocessing = _Prep()


class TestAMidTurnReclassificationSurvivesTheTurn:
    """#1465 — driven through ``process_turn`` and the real tool.

    The defect is an ORDERING one: ``reclassify_evidence``'s write was correct
    and then overwritten by the turn's end-of-turn aggregate save. A direct
    call to the service cannot see that, and neither can a tool test with a
    mocked service — the save that does the damage is in ``process_turn``. So
    the whole path runs: service turn → engine → tool → service.
    """

    @staticmethod
    def _engine_that_calls_the_tool(rig, tool):
        async def process_turn(
            *,
            case,
            user_message: str,
            attachments: Optional[list] = None,
            intent_type: Optional[str] = None,
            intent_data: Optional[dict[str, Any]] = None,
            user_id: Optional[str] = None,
        ) -> dict[str, Any]:
            # The shape ``MilestoneEngine._build_tool_context`` produces: the
            # tool is handed the case object the turn is holding.
            context = ToolContext(
                session_id=case.case_id,
                case_id=case.case_id,
                enterprise_id=case.enterprise_id,
                user_id=OWNER,
                in_memory_case=case,
            )
            with patch(
                "faultmaven.modules.agent.tools."
                "reclassify_evidence_tool.get_settings",
                return_value=_Settings(),
            ):
                rig.tool_result = await tool.execute_with_context(
                    params={
                        "evidence_id": EV_1,
                        "data_type": DataType.LOGS_AND_ERRORS.value,
                    },
                    context=context,
                )
            return {
                "case_updated": case,
                "agent_response": "Reclassified.",
                "metadata": {"milestones_completed": [], "progress_made": True},
            }

        return process_turn

    async def _run_turn(self, rig):
        tool = ReclassifyEvidenceTool(investigation_service=rig.service)
        rig.engine.process_turn = AsyncMock(
            side_effect=self._engine_that_calls_the_tool(rig, tool)
        )
        await rig.service.process_turn(
            case_id=rig.case.case_id,
            user_id=OWNER,
            payload=TurnPayload(query="that's actually a log file"),
        )
        return await rig.repo.get(rig.case.case_id)

    @pytest.mark.asyncio
    async def test_the_row_the_tool_wrote_is_the_row_the_turn_saved(self):
        rig = _build(evidence_rows=2)
        saved = await self._run_turn(rig)

        # Positive control: the tool really ran and really reported success.
        # Without this the assertions below are satisfied by a turn where the
        # tool never fired and nothing was ever supposed to change.
        assert rig.tool_result.success is True, rig.tool_result.error
        # The tool answers the question it was ASKED, in that question's value
        # domain: ``data_type`` is its own parameter and its schema enum is
        # ``DataType``. Reporting the 6-value projection there told a model
        # that asked for ``command_output`` it got ``logs``.
        assert rig.tool_result.data["data_type"] == DataType.LOGS_AND_ERRORS.value
        assert rig.tool_result.data["source_type"] == EvidenceSourceType.LOGS.value

        uploaded = saved.uploaded_files[0]
        assert uploaded.data_type == EvidenceSourceType.LOGS.value, (
            "the end-of-turn aggregate save wrote the pre-reclassification "
            "case back over the tool's write (#1465)"
        )
        assert uploaded.summary == "new summary"
        assert uploaded.structural_index == "new index content"
        assert {ev.source_type for ev in saved.evidence} == {EvidenceSourceType.LOGS}

    @pytest.mark.asyncio
    async def test_the_clarification_drop_survives_the_turn(self):
        """fm#918 exposure 1 on ``trigger="agent_tool"``.

        The drop was written by the same clobbered save, so the question the
        reclassification had just answered stayed armed for the typed arm.
        That is #1465's stated consequence for fm#918, and it is the same fix.
        """
        rig = _build(armed=True)
        armed_before = [
            e for e in (rig.case.last_suggestions or []) if is_clarification_entry(e)
        ]
        # Positive control: there was a question to retire.
        assert len(armed_before) == 1

        saved = await self._run_turn(rig)

        assert rig.tool_result.success is True, rig.tool_result.error
        still_armed = [
            e for e in (saved.last_suggestions or []) if is_clarification_entry(e)
        ]
        assert still_armed == [], (
            "the file's clarification choices are still answerable after the "
            "reclassification that answered them"
        )

    @pytest.mark.asyncio
    async def test_nothing_is_persisted_mid_turn(self):
        """One save per turn — the write model the engine already has.

        The in-turn call hands persistence back to the turn. A save from
        inside the tool loop would commit a partial turn (the reclassification
        without the messages, the turn record or the progress the same turn
        produces), which is what the aggregate save exists to prevent.
        """
        rig = _build()
        tool = ReclassifyEvidenceTool(investigation_service=rig.service)
        saves_during_the_tool_call = []

        async def process_turn(*, case, user_message, **kwargs):
            context = ToolContext(
                session_id=case.case_id,
                case_id=case.case_id,
                enterprise_id=case.enterprise_id,
                user_id=OWNER,
                in_memory_case=case,
            )
            before = rig.repo.save.await_count
            with patch(
                "faultmaven.modules.agent.tools."
                "reclassify_evidence_tool.get_settings",
                return_value=_Settings(),
            ):
                rig.tool_result = await tool.execute_with_context(
                    params={
                        "evidence_id": EV_1,
                        "data_type": DataType.LOGS_AND_ERRORS.value,
                    },
                    context=context,
                )
            saves_during_the_tool_call.append(rig.repo.save.await_count - before)
            return {
                "case_updated": case,
                "agent_response": "Reclassified.",
                "metadata": {"milestones_completed": [], "progress_made": True},
            }

        rig.engine.process_turn = AsyncMock(side_effect=process_turn)
        await rig.service.process_turn(
            case_id=rig.case.case_id,
            user_id=OWNER,
            payload=TurnPayload(query="that's actually a log file"),
        )

        assert rig.tool_result.success is True, rig.tool_result.error
        assert saves_during_the_tool_call == [0]

    @pytest.mark.asyncio
    async def test_the_out_of_band_path_still_saves_for_itself(self):
        """No turn owns a ``PATCH``, so that path keeps its own write.

        The counterpart of the test above: handing persistence to the caller
        is conditional on there BEING a caller that will save.
        """
        rig = _build()
        before = rig.repo.save.await_count
        await _out_of_band(rig)
        assert rig.repo.save.await_count == before + 1

    @pytest.mark.asyncio
    async def test_an_in_flight_case_from_another_case_is_refused(self):
        """A turn may only write its own case."""
        from faultmaven.exceptions import ValidationException

        rig = _build()
        stranger = create_sample_case(user_id=OWNER)
        with pytest.raises(ValidationException):
            await rig.service.reclassify_evidence(
                case_id=rig.case.case_id,
                evidence_id=EV_1,
                user_id=OWNER,
                data_type=DataType.LOGS_AND_ERRORS,
                in_flight_case=stranger,
            )


class TestATerminalCaseIsNotMutable:
    """B1 — the terminal short-circuit does not protect the service.

    ``_process_terminal_turn`` routes a closed case to ``_process_terminal_qa``,
    which runs a TOOL LOOP: it passes ``_build_da_tool_schemas()`` — every
    registered tool, with no name filter — hands the model a ``ToolContext``
    carrying ``in_memory_case=case``, and returns that same object as
    ``case_updated`` for ``process_turn`` to save.

    Before the in-flight write model this was accidentally harmless: the tool
    wrote a freshly-loaded copy and the terminal turn's aggregate save
    overwrote it — the #1465 lost update was protecting closed cases.
    Measured on ``origin/main``, the row came back unchanged; measured on the
    first draft of this branch, it came back reclassified and committed. So
    the guard is not tidy-up, it is the replacement for an accident this
    branch removed.
    """

    def test_the_tool_is_offered_on_the_terminal_path(self):
        """The premise, asserted rather than assumed.

        If ``reclassify_evidence`` were filtered out of the terminal tool
        menu the guard below would be unreachable and this suite would be
        proving nothing — which is exactly the shape of the claim this
        branch got wrong the first time.
        """
        from faultmaven.modules.agent.tools.base import AgentToolRegistry

        registry = AgentToolRegistry()
        registry.register(ReclassifyEvidenceTool(investigation_service=MagicMock()))
        schemas = MilestoneEngine._build_da_tool_schemas(
            SimpleNamespace(investigation_tools=registry)
        )
        assert "reclassify_evidence" in [s["function"]["name"] for s in schemas], (
            "_build_da_tool_schemas applies no name filter, so every "
            "registered tool reaches the terminal Q&A turn"
        )

    @staticmethod
    def _closed(rig):
        now = datetime.now(timezone.utc)
        closed = rig.case.model_copy(
            update={
                "state": CaseState.CLOSED,
                "closed_at": now,
                "closure_reason": "resolved",
            }
        )
        rig.repo._storage[closed.case_id] = closed
        assert closed.is_terminal
        return closed

    @pytest.mark.asyncio
    async def test_the_service_refuses_a_closed_case(self):
        rig = _build()
        closed = self._closed(rig)
        with pytest.raises(ValidationException) as exc:
            await rig.service.reclassify_evidence(
                case_id=closed.case_id,
                evidence_id=EV_1,
                user_id=OWNER,
                data_type=DataType.LOGS_AND_ERRORS,
            )
        assert exc.value.details.get("case_state") == CaseState.CLOSED.value

    @pytest.mark.asyncio
    async def test_the_refusal_precedes_any_re_extraction(self):
        """Nothing is fetched or parsed for a turn that will be refused."""
        rig = _build()
        self._closed(rig)
        with pytest.raises(ValidationException):
            await rig.service.reclassify_evidence(
                case_id=rig.case.case_id,
                evidence_id=EV_1,
                user_id=OWNER,
                data_type=DataType.LOGS_AND_ERRORS,
            )
        rig.service.file_storage_service.retrieve_file.assert_not_awaited()
        rig.service.preprocessing_service.reclassify_evidence.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_closed_case_is_not_mutated_through_the_terminal_turn(self):
        """The whole path: terminal turn -> tool loop -> service -> save.

        Driven through ``process_turn`` with an engine double shaped exactly
        like ``_process_terminal_qa`` — because a direct service call proves
        the guard's logic and nothing about the path that reaches it.
        """
        rig = _build()
        closed = self._closed(rig)
        tool = ReclassifyEvidenceTool(investigation_service=rig.service)

        async def terminal_qa_shaped(*, case, user_message, **kwargs):
            context = ToolContext(
                session_id=case.case_id,
                case_id=case.case_id,
                enterprise_id=case.enterprise_id,
                user_id=OWNER,
                in_memory_case=case,
            )
            with patch(
                "faultmaven.modules.agent.tools."
                "reclassify_evidence_tool.get_settings",
                return_value=_Settings(),
            ):
                rig.tool_result = await tool.execute_with_context(
                    params={
                        "evidence_id": EV_1,
                        "data_type": DataType.LOGS_AND_ERRORS.value,
                    },
                    context=context,
                )
            return {
                "case_updated": case,
                "agent_response": "that case is closed",
                "metadata": {"milestones_completed": [], "progress_made": False},
            }

        rig.engine.process_turn = AsyncMock(side_effect=terminal_qa_shaped)
        before = await rig.repo.get(closed.case_id)
        await rig.service.process_turn(
            case_id=closed.case_id,
            user_id=OWNER,
            payload=TurnPayload(query="that's actually a log file"),
        )
        after = await rig.repo.get(closed.case_id)

        # Positive control: the tool really ran on this turn.
        assert rig.tool_result is not None
        assert rig.tool_result.success is False
        assert "closed case" in rig.tool_result.error
        # And it refused CLEANLY — not through the catch-all, which logs a
        # stack trace and hands the model a retryable-sounding failure.
        assert "do not retry" in rig.tool_result.error
        assert not rig.tool_result.error.startswith("Reclassification failed")

        assert after.uploaded_files[0].data_type == before.uploaded_files[0].data_type
        assert after.uploaded_files[0].summary == before.uploaded_files[0].summary
        assert [e.source_type for e in after.evidence] == [
            e.source_type for e in before.evidence
        ]


class TestTheEvidenceRowsInheritedCoverageMovesToo:
    """B2 — #1470's divergence, re-created by #1471's fix.

    ``milestone_engine._evidence_coverage`` copies the FILE's span *and* its
    provenance onto an Evidence row at creation when the file span is a
    single instant. Refreshing only the file row therefore left the Evidence
    row asserting an instant nothing supports — read as fact by
    ``symptom_currency`` (``coverage_end_ts`` + ``is_vouched``) and published
    to the model by ``list_evidence_by_time_tool``.
    """

    @pytest.mark.asyncio
    async def test_an_inherited_instant_is_dropped_with_the_file_window(self):
        instant = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        rig = _build(
            old_coverage=(instant, instant, "epoch_s"),
            new_coverage=(None, None, None),
            new_data_type=DataType.STRUCTURED_CONFIG,
        )
        # Born the way the engine births it: inheriting the file's point span.
        start, end, source = _evidence_coverage(rig.case, FILE_A, None)
        # Positive control: the inheritance really happened, or the assertion
        # below passes against a row that never carried a span at all.
        assert (start, end, source) == (instant, instant, "epoch_s")
        rig.case.evidence[0] = rig.case.evidence[0].model_copy(
            update={
                "coverage_start_ts": start,
                "coverage_end_ts": end,
                "coverage_source": source,
            }
        )
        rig.repo._storage[rig.case.case_id] = rig.case

        saved = await _out_of_band(rig)

        uploaded, evidence = saved.uploaded_files[0], saved.evidence[0]
        assert uploaded.coverage_end_ts is None
        assert evidence.coverage_end_ts is None, (
            "the file no longer supports the instant and the row still "
            "asserts it — symptom_currency reads this as fact"
        )
        assert evidence.coverage_source is None
        assert not is_vouched(evidence.coverage_source)

    @pytest.mark.asyncio
    async def test_a_span_the_row_parsed_from_its_own_extract_survives(self):
        """The control that stops the fix becoming a different bug.

        ``_evidence_coverage`` resolves the row's OWN extract first, and that
        span is more authoritative than the file's. Re-deriving must not
        flatten it — which is why the fix routes through that function rather
        than clearing the fields.
        """
        file_instant = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        rig = _build(
            old_coverage=(file_instant, file_instant, "epoch_s"),
            new_coverage=(None, None, None),
            new_data_type=DataType.STRUCTURED_CONFIG,
        )
        rig.case.evidence[0] = rig.case.evidence[0].model_copy(
            update={
                "extract": (
                    "2026-07-04T03:11:00Z ERROR boom\n"
                    "2026-07-04T03:12:00Z ERROR again\n"
                ),
            }
        )
        rig.repo._storage[rig.case.case_id] = rig.case

        saved = await _out_of_band(rig)

        evidence = saved.evidence[0]
        assert saved.uploaded_files[0].coverage_end_ts is None
        assert evidence.coverage_start_ts == datetime(
            2026, 7, 4, 3, 11, tzinfo=timezone.utc
        )
        assert evidence.coverage_end_ts == datetime(
            2026, 7, 4, 3, 12, tzinfo=timezone.utc
        )
        assert evidence.coverage_source == "iso8601_t"

    @pytest.mark.asyncio
    async def test_both_paths_agree_on_the_evidence_window(self):
        instant = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)

        def _seeded():
            rig = _build(
                evidence_rows=2,
                old_coverage=(instant, instant, "epoch_s"),
                new_coverage=(None, None, None),
                new_data_type=DataType.STRUCTURED_CONFIG,
            )
            rig.case.evidence = [
                e.model_copy(
                    update={
                        "coverage_start_ts": instant,
                        "coverage_end_ts": instant,
                        "coverage_source": "epoch_s",
                    }
                )
                for e in rig.case.evidence
            ]
            rig.repo._storage[rig.case.case_id] = rig.case
            return rig

        saved = await _out_of_band(_seeded())
        updated = await _turn_seam(_seeded())
        assert [e.coverage_end_ts for e in saved.evidence] == [None, None]
        assert [e.coverage_end_ts for e in updated.evidence] == [None, None]


class TestTheClassifierVerdictIsAFileLevelFact:
    """F2 — the fan-out moved ``source_type`` and left the verdict behind."""

    _LOW = {
        "classification": {
            "confidence": 0.42,
            "source": "classifier",
            "failed": False,
            "suggested_types": [],
        },
        "extractor": {"chosen_type": "metrics", "attempts": []},
    }
    _HIGH = {
        "classification": {
            "confidence": 1.0,
            "source": "user_override",
            "failed": False,
            "suggested_types": [],
        },
        "extractor": {"chosen_type": "logs_and_errors", "attempts": []},
    }

    @pytest.mark.asyncio
    async def test_no_sibling_still_advertises_the_superseded_verdict(self):
        """A corrected row must not keep telling the model the type is doubtful.

        ``context_builder._confidence_marker`` renders ``confidence="low"``
        plus a "treat the file extract as tentative" advisory off
        ``metadata.classification``, and the prompt acts on it by re-asking —
        about a file the user has just classified by hand.
        """
        rig = _build(evidence_rows=2)
        rig.case.evidence = [
            e.model_copy(update={"metadata": dict(self._LOW)})
            for e in rig.case.evidence
        ]
        rig.repo._storage[rig.case.case_id] = rig.case
        rig.service.preprocessing_service.reclassify_evidence = AsyncMock(
            return_value=make_preprocessing_result(
                new_data_type=DataType.LOGS_AND_ERRORS, metadata=self._HIGH
            )
        )

        class _Marker:
            reclassify_enabled = True
            confidence_marker_enabled = True

        saved = await _out_of_band(rig)

        with patch(
            "faultmaven.config.settings.get_settings",
            return_value=SimpleNamespace(preprocessing=_Marker()),
        ):
            markers = {e.evidence_id: _confidence_marker(e)[0] for e in saved.evidence}
        assert markers == {EV_1: "", EV_2: ""}, (
            f"a row the user just corrected still renders a low-confidence "
            f"marker: {markers}"
        )

    @pytest.mark.asyncio
    async def test_the_extractor_trail_is_not_fanned_out(self):
        """``attempts`` records what was asked of THAT row, and only that row.

        The verdict is a file-level fact and moves; the trail is per-request
        and must not, or the observability record claims a request nobody
        made.
        """
        rig = _build(evidence_rows=2)
        sibling_trail = {
            "classification": dict(self._LOW["classification"]),
            "extractor": {"chosen_type": "metrics", "attempts": [{"n": "sibling"}]},
        }
        rig.case.evidence = [
            rig.case.evidence[0].model_copy(update={"metadata": dict(self._LOW)}),
            rig.case.evidence[1].model_copy(update={"metadata": sibling_trail}),
        ]
        rig.repo._storage[rig.case.case_id] = rig.case
        rig.service.preprocessing_service.reclassify_evidence = AsyncMock(
            return_value=make_preprocessing_result(
                new_data_type=DataType.LOGS_AND_ERRORS, metadata=self._HIGH
            )
        )

        saved = await _out_of_band(rig)

        sibling = next(e for e in saved.evidence if e.evidence_id == EV_2)
        assert sibling.metadata["extractor"]["attempts"] == [{"n": "sibling"}]
        assert sibling.metadata["classification"]["source"] == "user_override"


class TestTheSeamRefusesRatherThanPartiallyApplying:
    """F1 — an id with no file row would move every Evidence row anyway."""

    def test_a_file_id_absent_from_uploaded_files_raises(self):
        rig = _build(evidence_rows=2)
        with pytest.raises(NotFoundError):
            _reclassified_collections(
                rig.case,
                "file_zzzzzzzzzzzz",
                make_preprocessing_result(DataType.LOGS_AND_ERRORS),
                EvidenceSourceType.LOGS,
            )
        # Nothing was applied on the way to the refusal.
        assert {e.source_type for e in rig.case.evidence} == {
            EvidenceSourceType.METRICS
        }


class TestTheQuestionIsRetiredAtTheSeam:
    """F3 — retirement was a thing each path remembered separately."""

    @pytest.mark.asyncio
    async def test_the_turn_seam_retires_through_the_seam_too(self):
        rig = _build(armed=True)
        result = await rig.service._handle_file_reclassification(
            case=rig.case,
            file_id=FILE_A,
            data_type_value=DataType.LOGS_AND_ERRORS.value,
        )
        # Positive control: there was a question to retire.
        assert any(is_clarification_entry(e) for e in (rig.case.last_suggestions or []))
        carried = result["case_updated"].last_suggestions or []
        assert [e for e in carried if is_clarification_entry(e)] == []

    @pytest.mark.asyncio
    async def test_the_out_of_band_path_retires_the_same_way(self):
        rig = _build(armed=True)
        saved = await _out_of_band(rig)
        carried = saved.last_suggestions or []
        assert [e for e in carried if is_clarification_entry(e)] == []

    def test_the_seam_returns_the_retirement_with_the_collections(self):
        """One call answers all three, so a third caller cannot get two."""
        rig = _build(armed=True)
        files, evidence, retired = _reclassified_collections(
            rig.case,
            FILE_A,
            make_preprocessing_result(DataType.LOGS_AND_ERRORS),
            EvidenceSourceType.LOGS,
        )
        assert files and evidence
        assert retired is None or not [e for e in retired if is_clarification_entry(e)]
