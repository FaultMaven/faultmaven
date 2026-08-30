"""#1142, service half — one telemetry row per CONSUMED turn, on every route.

The row is emitted from ``InvestigationService.process_turn`` rather than from
the engine, and that placement is the point of this file. ``case.current_turn``
is advanced in the service, so the service is where "a turn was consumed" is
decided; several routes then answer WITHOUT reaching
``MilestoneEngine.process_turn`` at all (GREETING, FILE_RECLASSIFICATION), and a
terminal case short-circuits inside the engine before its turn bookkeeping. An
event emitted per engine path leaves those turns as gaps — and a gap is not a
harmless missing row: every streak a consumer computes over the stream silently
shortens, so a correct multi-turn confirmation handshake reads as an engine-dry
run, which is the exact misattribution the stream exists to prevent.

The engine double is ``create_autospec``'d for the reason
``test_intent_handler_attachments_1229`` gives: a bare ``Mock`` advertises
``(*args, **kwargs)`` and would accept a call shape the real engine rejects,
making these assertions unfailable.
"""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from faultmaven.core.investigation.case_telemetry import (
    PROGRESS_ARM_KEYS as PROGRESS_ARM_KEYS_FOR_TEST,
)
from faultmaven.core.investigation.case_telemetry import (
    TELEMETRY_HANDOFF_KEY,
    TELEMETRY_LOGGER_NAME,
    TurnPath,
)
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.models.api import DataType
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import CaseState

pytestmark = pytest.mark.unit

CONTENT = b"2026-08-28T10:00:00Z ERROR pod restart loop\n"
CONTENT_HASH = "e" * 64
# Non-zero on purpose: a counter asserted at its default proves nothing.
STANDING_STALL = 4


class _PreprocessingDouble:
    async def classify_and_extract(self, content, filename, source_metadata=None):
        return SimpleNamespace(
            summary="Pod restart loop.",
            structural_index="ERROR x 42",
            detailed_data_type=DataType.LOGS_AND_ERRORS,
            content_hash=CONTENT_HASH,
            coverage_start_ts=None,
            coverage_end_ts=None,
            coverage_source=None,
            extraction_method="structure_extraction",
            extraction_metadata={},
        )


@pytest.fixture
def repo(mock_case_repository):
    mock_case_repository.find_uploaded_file_by_content_hash = AsyncMock(
        return_value=None
    )
    return mock_case_repository


@pytest.fixture
def engine():
    """Returns the shape the real generation path returns, INCLUDING the
    telemetry handoff — which is what the service lifts the progress arms off."""
    double = create_autospec(MilestoneEngine, instance=True)
    double.llm_provider = MagicMock()

    async def spy(
        *,
        case,
        user_message: str,
        attachments: Optional[list] = None,
        intent_type: Optional[str] = None,
        intent_data: Optional[dict[str, Any]] = None,
        user_id: Optional[str] = None,
    ) -> dict[str, Any]:
        case.updated_at = datetime.now(timezone.utc)
        return {
            "case_updated": case,
            "agent_response": "ack",
            "metadata": {
                "milestones_completed": [],
                "progress_made": True,
                "outcome": "data_requested",
                TELEMETRY_HANDOFF_KEY: {
                    "path": TurnPath.LLM,
                    "arms": {"novel_evidence_added": 2, "novel_files_uploaded": 0},
                    "gate_name": "insufficient_evidence",
                    "validation_repairs": 1,
                    "repair_pattern": None,
                },
            },
        }

    double.process_turn = AsyncMock(side_effect=spy)
    return double


@pytest.fixture
def service(engine, repo):
    svc = InvestigationService(milestone_engine=engine, case_repository=repo)
    svc.preprocessing_service = _PreprocessingDouble()
    return svc


@pytest.fixture
def case(sample_case, sample_user_id):
    sample_case.user_id = sample_user_id
    sample_case.inquiry.proposed_problem_statement = "etcd connectivity"
    sample_case.inquiry.problem_statement_confirmed = True
    sample_case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    sample_case.inquiry.decided_to_investigate = True
    sample_case.inquiry.decision_made_at = datetime.now(timezone.utc)
    sample_case.state = CaseState.INVESTIGATING
    sample_case.turns_without_progress = STANDING_STALL
    return sample_case


def _rows(caplog):
    return [r for r in caplog.records if r.name == TELEMETRY_LOGGER_NAME]


async def _run(service, repo, case, payload: TurnPayload):
    await repo.save(case)
    return await service.process_turn(
        case_id=case.case_id, user_id=case.user_id, payload=payload
    )


class TestOneRowPerConsumedTurn:
    async def test_an_engine_routed_turn_emits_exactly_one_row(
        self, service, repo, case, caplog
    ):
        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            await _run(service, repo, case, TurnPayload(query="what does this mean?"))

        rows = _rows(caplog)
        assert len(rows) == 1
        assert rows[0].path == TurnPath.LLM.value
        assert rows[0].turn == case.current_turn

    async def test_a_greeting_is_a_row_and_not_a_gap(
        self, service, repo, case, engine, caplog
    ):
        """``_handle_greeting`` answers from a static string without ever
        calling the engine, so an engine-side emitter would record nothing for a
        turn the case definitely consumed."""
        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            await _run(service, repo, case, TurnPayload(query="hi"))

        engine.process_turn.assert_not_awaited()
        rows = _rows(caplog)
        assert len(rows) == 1
        assert rows[0].path == TurnPath.GREETING.value
        # The route never runs the progress predicate, so every arm is honestly 0
        # rather than absent — an absent arm and a zero arm read differently to
        # a rule keyed on "progress was claimed and every arm was 0", which an
        # absent arm would make unevaluable rather than false.
        from faultmaven.core.investigation.case_telemetry import PROGRESS_ARM_KEYS

        assert set(rows[0].arms) == set(PROGRESS_ARM_KEYS)
        assert all(v == 0 for v in rows[0].arms.values())

    async def test_the_row_carries_the_arms_the_engine_decided_on(
        self, service, repo, case, caplog
    ):
        """The four arms that only exist on the engine's working dict are the
        whole reason for the handoff: without them ``progress_made=True`` with
        every stored artifact list empty is indistinguishable from a counter
        that lied."""
        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            await _run(service, repo, case, TurnPayload(query="what does this mean?"))

        row = _rows(caplog)[0]
        assert row.progress_made is True
        assert row.arms["novel_evidence_added"] == 2
        # Arms the engine did not report are 0, not missing — the row shape is
        # the same whichever route produced it.
        assert set(row.arms) == set(PROGRESS_ARM_KEYS_FOR_TEST)
        assert row.engine_advanced is True
        assert row.user_supplied_new is False
        assert row.gate_name == "insufficient_evidence"
        assert row.validation_repairs == 1

    async def test_the_counter_is_the_settled_post_turn_value(
        self, service, repo, case, caplog
    ):
        """Emitted after the save, unlike the pre-existing grounding trace which
        runs inside response application and reports the PREVIOUS turn's
        counter."""
        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            await _run(service, repo, case, TurnPayload(query="what does this mean?"))

        assert _rows(caplog)[0].turns_without_progress == case.turns_without_progress


class TestTheHandoffIsNotProductSurface:
    async def test_the_handoff_key_never_reaches_the_persisted_message(
        self, service, repo, case
    ):
        """``result["metadata"]`` is persisted onto the assistant
        ``case_messages`` row, which is readable through the transcript API.
        This is monitoring data and must not ride there."""
        await _run(service, repo, case, TurnPayload(query="what does this mean?"))

        saved = await repo.get(case.case_id)
        assistant = [m for m in saved.messages if m.get("role") == "assistant"]
        assert assistant, "no assistant message was persisted"
        for message in assistant:
            assert TELEMETRY_HANDOFF_KEY not in (message.get("metadata") or {})

    async def test_the_handoff_key_is_not_returned_to_the_caller(
        self, service, repo, case
    ):
        response = await _run(
            service, repo, case, TurnPayload(query="what does this mean?")
        )
        assert TELEMETRY_HANDOFF_KEY not in response.model_dump()


class TestFailureIsolation:
    async def test_a_broken_payload_build_does_not_fail_the_turn(
        self, service, repo, case, monkeypatch, caplog
    ):
        """A diagnostic must never break the turn it observes.

        The realistic failure is not the call site — it is the BUILDER, which
        reaches into a dozen model fields and would raise the day one of them is
        renamed underneath it. ``emit_case_turn`` swallows that; the turn must
        still answer, and the emitter must stay silent rather than emit a
        half-built row.
        """
        import faultmaven.core.investigation.case_telemetry as telemetry

        def explode(*_args, **_kwargs):
            raise AttributeError("Case.progress was renamed")

        monkeypatch.setattr(telemetry, "build_case_turn_event", explode)

        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            response = await _run(
                service, repo, case, TurnPayload(query="what does this mean?")
            )

        assert response.agent_response == "ack"
        assert _rows(caplog) == []


class TestTheErrorPathIsARowNotAGap:
    async def test_a_failed_turn_is_labelled_rather_than_missing(
        self, service, repo, case, engine, caplog
    ):
        """The turn number was consumed before the failure, so without a row the
        stream shows a gap on exactly the turns something went wrong — and a
        provider outage would then read as an idle engine."""
        engine.process_turn = AsyncMock(side_effect=RuntimeError("provider 503"))

        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            with pytest.raises(Exception):
                await _run(
                    service,
                    repo,
                    case,
                    TurnPayload(
                        query="here are the logs you asked for",
                        attachments=[
                            Attachment(
                                content=CONTENT,
                                filename="app.log",
                                content_type="text/plain",
                            )
                        ],
                    ),
                )

        rows = _rows(caplog)
        assert len(rows) == 1
        assert rows[0].path == TurnPath.ERROR.value
        assert rows[0].engine_advanced is False
        # The volume facts are known whatever failed, and omitting them reports
        # the user as having gone silent on a turn they supplied data — the
        # mirror image of the misattribution the ``error`` label exists to
        # prevent. A consumer reading a window of provider failures would see
        # the user go quiet.
        assert rows[0].user_message_chars == len("here are the logs you asked for")
        assert rows[0].attachment_count == 1

    async def test_a_failure_before_the_turn_is_consumed_emits_nothing(
        self, service, repo, case, caplog, monkeypatch
    ):
        """The row is gated on the turn being CONSUMED, not on ``case`` existing.

        A failure between the case load and the ``current_turn`` increment
        (attachment preprocessing blowing up on a storage or extractor error)
        leaves a bound ``case`` whose ``current_turn`` is still the PREVIOUS
        turn's. A row emitted there collides with that turn's real row on the
        documented (case_id, turn) dedup key — and on turn 1 invents a row for
        turn 0, which never existed.
        """
        import faultmaven.modules.agent.domain.services.investigation_service as mod

        async def explode(*_args, **_kwargs):
            raise RuntimeError("extractor died on the raw bytes")

        monkeypatch.setattr(mod.InvestigationService, "_preprocess_attachment", explode)

        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            with pytest.raises(Exception):
                await _run(
                    service,
                    repo,
                    case,
                    TurnPayload(
                        query="here",
                        attachments=[
                            Attachment(
                                content=CONTENT,
                                filename="app.log",
                                content_type="text/plain",
                            )
                        ],
                    ),
                )

        assert _rows(caplog) == [], (
            "emitted a row for a turn that was never consumed — it carries the "
            "previous turn's number and collides with that turn's real row"
        )

    async def test_a_stale_case_conflict_after_the_engine_saved_is_still_a_row(
        self, service, repo, case, caplog
    ):
        """``StaleCaseException`` is re-raised unwrapped for its 409, and it is
        the one contract error that can follow a DURABLY consumed turn: on an
        engine-routed turn the engine already committed the incremented
        ``current_turn`` at its own save, so an OCC conflict on the service's
        save leaves a consumed turn. No row there is a gap, and a gap shortens
        every streak computed over the stream.
        """
        from faultmaven.modules.case.exceptions import StaleCaseException

        await repo.save(case)  # seed BEFORE the conflict is installed

        async def conflict_on_the_service_save(_case_arg):
            raise StaleCaseException(case.case_id, 1, 2)

        repo.save.side_effect = conflict_on_the_service_save

        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            with pytest.raises(StaleCaseException):
                await service.process_turn(
                    case_id=case.case_id,
                    user_id=case.user_id,
                    payload=TurnPayload(query="what does this mean?"),
                )

        rows = _rows(caplog)
        assert len(rows) == 1
        assert rows[0].path == TurnPath.ERROR.value

    async def test_a_failure_after_the_row_does_not_emit_a_second_one(
        self, service, repo, case, monkeypatch, caplog
    ):
        """The success row is emitted before ``TurnResponse`` is assembled, so a
        failure in between would otherwise produce two rows for one turn."""
        import faultmaven.modules.agent.domain.services.investigation_service as mod

        def explode(*_args, **_kwargs):
            raise RuntimeError("response assembly failed")

        monkeypatch.setattr(mod, "TurnResponse", explode)

        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            with pytest.raises(Exception):
                await _run(
                    service, repo, case, TurnPayload(query="what does this mean?")
                )

        rows = _rows(caplog)
        assert len(rows) == 1
        assert rows[0].path == TurnPath.LLM.value


class TestUploadTurnAttribution:
    async def test_a_novel_upload_is_attributed_to_the_user_side(
        self, service, repo, case, engine, caplog
    ):
        """The attribution that the single stall counter cannot make: the user
        supplied something new, so this turn is not an engine-dry turn even if
        the engine produced nothing."""

        async def spy(*, case, **_kwargs):
            case.updated_at = datetime.now(timezone.utc)
            return {
                "case_updated": case,
                "agent_response": "ack",
                "metadata": {
                    "milestones_completed": [],
                    "progress_made": True,
                    TELEMETRY_HANDOFF_KEY: {
                        "path": TurnPath.LLM,
                        "arms": {
                            "novel_files_uploaded": 1,
                            "files_uploaded": 1,
                            "novel_evidence_added": 0,
                        },
                        "gate_name": None,
                    },
                },
            }

        engine.process_turn = AsyncMock(side_effect=spy)

        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            await _run(
                service,
                repo,
                case,
                TurnPayload(
                    query="here are the logs",
                    attachments=[
                        Attachment(
                            content=CONTENT,
                            filename="app.log",
                            content_type="text/plain",
                        )
                    ],
                ),
            )

        row = _rows(caplog)[0]
        assert row.user_supplied_new is True
        assert row.engine_advanced is False
        assert row.attachment_count == 1


class TestPathCoverageIsExhaustive:
    def test_every_engine_bypassing_route_has_its_own_label(self):
        """A route that answers WITHOUT the engine has no handoff to take its
        label from, so it needs an explicit branch in the telemetry block — and
        without one it silently mislabels itself as ``llm``, which is worse than
        a gap: it attributes a turn to a path that never ran.

        Scoped to the TELEMETRY BLOCK, not to the whole method. Every SERVICE
        intent necessarily appears in ``process_turn`` — that is the dispatch
        if/elif chain — so asserting against the full source is vacuous and
        passes for a route with no label at all. The routes themselves are
        derived from the handlers' own bodies rather than listed here, so one
        added later fails this instead of shipping mislabelled.
        """
        import inspect

        from faultmaven.modules.agent.domain.services.investigation_service import (
            _INTENT_DISPATCH,
            _IntentDispatchKind,
        )

        handlers = {
            IntentType.STATUS_TRANSITION: InvestigationService._handle_status_transition,
            IntentType.CONFIRMATION: InvestigationService._handle_confirmation,
            IntentType.HYPOTHESIS_ACTION: InvestigationService._handle_hypothesis_action,
            IntentType.GREETING: InvestigationService._handle_greeting,
            IntentType.FILE_RECLASSIFICATION: InvestigationService._handle_file_reclassification,
        }
        service_routed = {
            intent
            for intent, kind in _INTENT_DISPATCH.items()
            if kind == _IntentDispatchKind.SERVICE
        }
        assert service_routed <= set(handlers), (
            "a SERVICE-routed intent has no entry here — add it, and decide "
            "whether it needs its own TurnPath label"
        )

        def _calls_engine(fn) -> bool:
            """Does the handler actually CALL ``self.engine.process_turn``?

            Parsed, not string-scanned. ``_handle_file_reclassification`` carries
            the line "protection by delegating to engine.process_turn" in a
            comment explaining that it does NOT do so, and a substring scan reads
            that as a call — silently dropping the route from this set and
            leaving it unguarded (found by #1264). Stripping full-line ``#``
            comments is not enough either: a trailing inline comment or a
            docstring mention slips straight back through. Walking the AST for a
            real attribute call is the only form that cannot be fooled by prose.
            """
            import ast
            import textwrap

            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "process_turn"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "engine"
                ):
                    return True
            return False

        bypasses_engine = {
            intent for intent in service_routed if not _calls_engine(handlers[intent])
        }
        # Both engine-bypassing routes pinned by name. ``assert bypasses_engine``
        # alone is satisfied by any non-empty set, so either route could vanish
        # from it undetected — which is the same silent set-shrink the AST walk
        # above exists to prevent.
        assert bypasses_engine == {
            IntentType.FILE_RECLASSIFICATION,
            IntentType.GREETING,
        }, (
            f"the set of engine-bypassing routes changed: {bypasses_engine}. "
            "Either a route was added/removed, or the detection above regressed "
            "and is reading prose again."
        )
        assert bypasses_engine, "expected at least GREETING to bypass the engine"

        # Just the emission block: from the label decision to the emit call.
        src = inspect.getsource(InvestigationService.process_turn)
        start = src.index("turn_arms = turn_telemetry.get")
        end = src.index("turn_row_emitted = True")
        telemetry_block = src[start:end]

        # The block must be narrow enough that the dispatch chain is not in it,
        # or this assertion is the vacuous one it replaced.
        assert "_IntentDispatchKind.SERVICE" not in telemetry_block

        for intent in bypasses_engine:
            assert f"IntentType.{intent.name}" in telemetry_block, (
                f"{intent.name} answers without the engine, so it carries no "
                "telemetry handoff, yet the emission block has no branch "
                "labelling its route — it would be recorded as an LLM turn"
            )

    async def test_a_handoff_less_result_still_attributes_its_uploads(
        self, service, repo, case, engine, caplog
    ):
        """A result with no handoff but with uploads on it must not read as
        "the user supplied nothing".

        This is the shape the TERMINAL route returns: the engine's terminal
        short-circuit fires above its turn bookkeeping, so it never builds a
        handoff — but ``upload_report`` was merged into the metadata above the
        path fork, so the upload keys are there. Defaulting the arms to all-zero
        in that case prints the exact opposite of the truth on an
        engine-dry-user-supplying turn, which is the one the whole stream exists
        to surface. (FILE_RECLASSIFICATION has the same shape; it needs stored
        raw content to run, which is orthogonal to what is being pinned here.)
        """

        async def terminal_shaped(*, case, **_kwargs):
            case.updated_at = datetime.now(timezone.utc)
            return {
                "case_updated": case,
                "agent_response": "that case is closed",
                "metadata": {
                    "milestones_completed": [],
                    "progress_made": False,
                    "files_uploaded": ["file_0123456789ab"],
                    "novel_files_uploaded": ["file_0123456789ab"],
                },
            }

        engine.process_turn = AsyncMock(side_effect=terminal_shaped)

        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            await _run(
                service,
                repo,
                case,
                TurnPayload(
                    query="any update?",
                    attachments=[
                        Attachment(
                            content=CONTENT,
                            filename="app.log",
                            content_type="text/plain",
                        )
                    ],
                ),
            )

        row = _rows(caplog)[0]
        assert row.arms["novel_files_uploaded"] == 1, (
            "the upload was on the returned metadata; defaulting the arms to "
            "all-zero reports the user as having supplied nothing"
        )
        assert row.user_supplied_new is True
        assert row.engine_advanced is False

    async def test_the_implicit_query_is_not_counted_as_user_text(
        self, service, repo, case, caplog
    ):
        """``user_message_chars`` measures what the USER typed.

        On an attachment-only turn the service replaces the empty payload query
        with ``generate_implicit_query``'s engine-composed sentence. Measuring
        that reports a paragraph on a turn the user typed nothing — the field's
        one job is telling that apart from "user wrote a paragraph that produced
        nothing".
        """
        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            await _run(
                service,
                repo,
                case,
                TurnPayload(
                    attachments=[
                        Attachment(
                            content=CONTENT,
                            filename="app.log",
                            content_type="text/plain",
                        )
                    ]
                ),
            )

        row = _rows(caplog)[0]
        assert row.user_message_chars == 0
        assert row.attachment_count == 1
