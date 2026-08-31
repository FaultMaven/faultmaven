"""Unit tests for InvestigationService.

Tests the InvestigationService which manages milestone-based troubleshooting workflow.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.exceptions import (
    NotFoundError,
    PermissionDeniedException,
    ServiceException,
)
from faultmaven.models.api_models import (
    AttachmentResult,
    IntentType,
    QueryIntent,
    TurnResponse,
)
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseSeverity,
    CaseState,
    InquiryData,
    ProblemVerification,
)

from .conftest import (
    MockCaseRepository,
    MockMilestoneEngine,
    create_sample_case,
    make_preprocessing_result,
    mock_case_repository,
    mock_milestone_engine,
    sample_case,
    sample_turn_payload,
    sample_user_id,
)


class TestEvidenceBearingRerouteToEngine:
    """#708: the reroute must reach the engine.

    The two predicates are unit-tested in isolation elsewhere; this pins the
    propagation linchpin — a generic cover message + an evidence-bearing
    attachment ends up threading ``query_mode="directed_analysis"`` in the
    ``intent_data`` the engine receives. A regression that moved the
    intent_data build above the reroute, or reassigned ``classification``
    in between, would slip past the isolated predicate tests but fail here.

    The reroute is gated to INVESTIGATING (on INQUIRY a fresh upload is
    characterized, not forced into directed analysis), so these cases build
    an INVESTIGATING case; the INQUIRY control pins that gate.
    """

    def _service(self):
        engine = MockMilestoneEngine()
        preprocessing = AsyncMock()
        preprocessing.classify_and_extract = AsyncMock(
            return_value=make_preprocessing_result()
        )
        service = InvestigationService(
            milestone_engine=engine,
            case_repository=MockCaseRepository(),
            preprocessing_service=preprocessing,
            file_storage_service=None,
        )
        return service, engine

    def _investigating_case(self, user_id: str) -> Case:
        """An INVESTIGATING case (the reroute's scope). INVESTIGATING requires
        a confirmed problem statement + investigation commitment."""
        return Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id=user_id,
            organization_id="org_test123",
            title="Test Case",
            description="Pods are crashing",
            state=CaseState.INVESTIGATING,
            current_turn=2,
            inquiry=InquiryData(
                proposed_problem_statement="pods crashing",
                problem_statement_confirmed=True,
                decided_to_investigate=True,
            ),
            problem_verification=ProblemVerification(
                symptom_statement="pods crashing", severity=CaseSeverity.HIGH
            ),
        )

    async def _run(self, service, case, user_id, query, attachments):
        await service.repository.save(case)
        payload = TurnPayload(query=query, attachments=attachments)
        await service.process_turn(
            case_id=case.case_id, user_id=user_id, payload=payload
        )
        return service.engine.process_turn.call_args.kwargs["intent_data"]

    @pytest.mark.asyncio
    async def test_triage_cover_message_plus_attachment_threads_da(self):
        """The bug shape: 'here's the logs' + a log upload on an INVESTIGATING
        turn → the engine is called with query_mode=directed_analysis."""
        service, engine = self._service()
        user_id = str(uuid4())
        case = self._investigating_case(user_id)
        attachment = Attachment(
            content=b"07:40 ERROR 503 SSLException: Connection reset",
            filename="user-service.log",
            content_type="text/plain",
        )

        intent_data = await self._run(
            service, case, user_id, "here's the logs", [attachment]
        )

        engine.process_turn.assert_awaited_once()
        assert intent_data["query_mode"] == "directed_analysis"

    @pytest.mark.asyncio
    async def test_knowledge_cover_message_plus_attachment_threads_da(self):
        """#708 finding #3: a knowledge-phrased cover ('what causes connection
        resets?') over a fresh upload also reroutes — otherwise the agent
        answers from general knowledge and skips the uploaded evidence."""
        service, engine = self._service()
        user_id = str(uuid4())
        case = self._investigating_case(user_id)
        attachment = Attachment(
            content=b"07:40 ERROR 503 SSLException: Connection reset",
            filename="user-service.log",
            content_type="text/plain",
        )

        intent_data = await self._run(
            service, case, user_id, "what causes connection resets?", [attachment]
        )

        engine.process_turn.assert_awaited_once()
        assert intent_data["query_mode"] == "directed_analysis"

    @pytest.mark.asyncio
    async def test_cover_message_without_attachment_stays_triage(self):
        """Control: the same cover message with no attachment stays TRIAGE —
        proving it is the attachment, not the text, that flips the route."""
        service, engine = self._service()
        user_id = str(uuid4())
        case = self._investigating_case(user_id)

        intent_data = await self._run(service, case, user_id, "here's the logs", [])

        engine.process_turn.assert_awaited_once()
        assert intent_data["query_mode"] == "triage"

    @pytest.mark.asyncio
    async def test_inquiry_upload_is_not_rerouted(self):
        """#708 finding #1: on INQUIRY a fresh upload is NOT forced into DA —
        the reroute is scoped to INVESTIGATING so problem-framing is not
        pre-empted."""
        service, engine = self._service()
        user_id = str(uuid4())
        case = create_sample_case(user_id=user_id)  # default INQUIRY
        assert case.state == CaseState.INQUIRY
        attachment = Attachment(
            content=b"07:40 ERROR 503 SSLException: Connection reset",
            filename="user-service.log",
            content_type="text/plain",
        )

        intent_data = await self._run(
            service, case, user_id, "here's the logs", [attachment]
        )

        engine.process_turn.assert_awaited_once()
        assert intent_data["query_mode"] == "triage"


class TestUploadDurabilityIsIndependentOfTheTurn:
    """An upload must survive a turn that fails after it.

    The bytes are written by ``store_file`` during preprocessing, and
    ``mark_linked`` then exempts them from TTL reclaim. While the
    ``UploadedFile`` row waited for the end-of-turn ``save(case)``, a turn that
    raised left those bytes stored, exempt from reclaim, and referenced by
    nothing — a permanent orphan — and the retry stored a second copy, because
    ``find_uploaded_file_by_content_hash`` cannot match a row that was never
    written. The row is now committed by its own scoped write during
    preprocessing.
    """

    def _service(self, repository):
        engine = MockMilestoneEngine()
        preprocessing = AsyncMock()
        preprocessing.classify_and_extract = AsyncMock(
            return_value=make_preprocessing_result()
        )
        storage = AsyncMock()
        storage.store_file = AsyncMock(return_value={"storage_key": "blob/abc123"})
        storage.mark_linked = AsyncMock(return_value=True)
        service = InvestigationService(
            milestone_engine=engine,
            case_repository=repository,
            preprocessing_service=preprocessing,
            file_storage_service=storage,
        )
        return service, engine, storage

    @pytest.mark.asyncio
    async def test_upload_is_committed_even_when_the_turn_fails(self):
        """The scoped commit lands BEFORE the engine runs, not merely at some point.

        ⚠️ Asserted on the repository call, not on ``case.uploaded_files``.
        The service appends the row to the in-memory case regardless, and
        ``MockCaseRepository`` stores by reference — so inspecting the case
        would report success with the scoped commit deleted. The call is the
        only signal that distinguishes committed from merely-appended.

        ⚠️ Ordering is asserted explicitly via a call log. Asserting only that
        the commit happened would pass for an implementation that committed in a
        ``finally`` AFTER the engine — which is a different (and weaker)
        property: it would leave the upload uncommitted for the whole duration
        of the LLM call, so a crash or timeout mid-turn still orphans the bytes.
        The property is "durable before anything can fail", not "durable
        eventually".
        """
        repository = MockCaseRepository()
        calls: list[str] = []

        async def _record_commit(*_args, **_kwargs):
            calls.append("commit")

        repository.add_uploaded_file = AsyncMock(side_effect=_record_commit)
        service, engine, storage = self._service(repository)

        user_id = str(uuid4())
        case = create_sample_case(user_id=user_id)
        await repository.save(case)

        async def _failing_engine(*_args, **_kwargs):
            calls.append("engine")
            raise RuntimeError("LLM provider exploded")

        engine.process_turn.side_effect = _failing_engine

        with pytest.raises(ServiceException):
            await service.process_turn(
                case_id=case.case_id,
                user_id=user_id,
                payload=TurnPayload(
                    query="here are the logs",
                    attachments=[
                        Attachment(
                            content=b"07:40 ERROR 503 SSLException: Connection reset",
                            filename="user-service.log",
                            content_type="text/plain",
                        )
                    ],
                ),
            )

        repository.add_uploaded_file.assert_awaited_once()
        call_case_id, committed_file, call_org = (
            repository.add_uploaded_file.await_args.args
        )
        assert call_case_id == case.case_id
        assert call_org == case.organization_id
        # The committed row must carry the storage pointer — a row that does not
        # reference the stored bytes leaves them orphaned just the same.
        assert committed_file.storage_ref == "blob/abc123"
        assert committed_file.filename == "user-service.log"

        # The ordering itself. "commit" must precede "engine" — a commit that
        # only happens after the engine (or in a finally) fails here.
        assert calls == [
            "commit",
            "engine",
        ], f"expected the upload to be committed before the engine ran, got {calls}"
        storage.store_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_service_calls_the_contract_method_name(self):
        """Pin the method NAME against a rename that silently disables the fix.

        The call site resolves the method with ``getattr(self.repository,
        "add_uploaded_file", None)`` and degrades when it is absent. That guard
        protects test doubles, but it also means renaming the contract method
        without updating the call site reverts every upload to the orphaning
        behaviour — with only a log line to show for it. This asserts the name
        the service looks up still exists on the abstract base and the Protocol,
        so the rename is caught here instead of in production.
        """
        from faultmaven.modules.case.contracts import ICaseRepository
        from faultmaven.modules.case.infrastructure.case_repository import (
            CaseRepository,
        )

        assert hasattr(CaseRepository, "add_uploaded_file"), (
            "CaseRepository lost add_uploaded_file — the service's getattr "
            "lookup now silently degrades every upload"
        )
        assert hasattr(ICaseRepository, "add_uploaded_file")

    @pytest.mark.asyncio
    async def test_scoped_commit_failure_degrades_but_does_not_break_the_upload(self):
        """A repository that cannot do the scoped write must not fail the turn.

        The fallback is the previous behaviour — the row rides the end-of-turn
        save — which is a durability regression, not a broken upload. It is
        logged rather than raised, so the turn still completes.
        """
        repository = MockCaseRepository()
        repository.add_uploaded_file = AsyncMock(
            side_effect=RuntimeError("db unavailable")
        )
        service, engine, storage = self._service(repository)

        user_id = str(uuid4())
        case = create_sample_case(user_id=user_id)
        await repository.save(case)

        response = await service.process_turn(
            case_id=case.case_id,
            user_id=user_id,
            payload=TurnPayload(
                query="here are the logs",
                attachments=[
                    Attachment(
                        content=b"07:40 ERROR 503 SSLException: Connection reset",
                        filename="user-service.log",
                        content_type="text/plain",
                    )
                ],
            ),
        )

        assert response is not None
        repository.add_uploaded_file.assert_awaited_once()
        # The upload still reached the case aggregate, so the end-of-turn save
        # persists it on this (successful) turn.
        stored = await repository.get(case.case_id)
        assert any(f.filename == "user-service.log" for f in stored.uploaded_files)


class TestAuthenticatedPrincipalReachesTheEngine:
    """The turn's reader must arrive at the engine as its own argument.

    The engine keys the agent's KB read allowlist (owner + team arms, ADR-013
    §D4) on this value. It cannot be recovered downstream: ``intent_data`` is
    assembled from the client-supplied intent payload, so a principal taken
    from there would be client-settable, and the engine has no other handle on
    the authenticated caller. The service, which holds ``current_user.user_id``,
    is the only place it can come from, and it must travel outside
    ``intent_data``.

    Before this was threaded, the engine defaulted to the ``"system"``
    sentinel on every live turn: it owns no KB items and belongs to no team, so
    both arms of the filter collapsed and the agent could read only the global
    corpus — silently, with no error anywhere.
    """

    def _service(self):
        engine = MockMilestoneEngine()
        preprocessing = AsyncMock()
        preprocessing.classify_and_extract = AsyncMock(
            return_value=make_preprocessing_result()
        )
        return (
            InvestigationService(
                milestone_engine=engine,
                case_repository=MockCaseRepository(),
                preprocessing_service=preprocessing,
                file_storage_service=None,
            ),
            engine,
        )

    @pytest.mark.asyncio
    async def test_authenticated_principal_is_threaded_to_the_engine(self):
        service, engine = self._service()
        user_id = str(uuid4())
        case = create_sample_case(user_id=user_id)
        await service.repository.save(case)

        await service.process_turn(
            case_id=case.case_id,
            user_id=user_id,
            payload=TurnPayload(query="what does this error mean?", attachments=[]),
        )

        kwargs = engine.process_turn.await_args.kwargs
        assert kwargs.get("user_id") == user_id, (
            "the engine did not receive the turn's authenticated principal, so "
            "its KB tool falls back to 'system' and reads global-only"
        )
        assert "user_id" not in kwargs["intent_data"], (
            "the principal must not ride in intent_data — that dict is built "
            "from the client-supplied intent payload"
        )


class TestInvestigationServiceProcessTurn:
    """Tests for InvestigationService.process_turn()."""

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        """Create InvestigationService with mocked dependencies."""
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.mark.asyncio
    async def test_process_turn_success(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
        sample_turn_payload,
    ):
        """Test successful turn processing."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        assert isinstance(response, TurnResponse)
        assert (
            response.agent_response == f"Agent response to: {sample_turn_payload.query}"
        )
        # Mock engine increments turn from 0 to 1, so response should have turn_number = 1
        assert (
            response.turn_number == 1
        ), f"Expected turn_number=1, got {response.turn_number} (initial was {sample_case.current_turn})"
        assert mock_milestone_engine.process_turn.called
        assert mock_case_repository.save.called

    @pytest.mark.asyncio
    async def test_process_turn_case_not_found(
        self, service, mock_case_repository, sample_user_id, sample_turn_payload
    ):
        """Test turn processing with non-existent case."""
        non_existent_case_id = f"case_{uuid4().hex[:12]}"

        with pytest.raises(NotFoundError, match="Case"):
            await service.process_turn(
                case_id=non_existent_case_id,
                user_id=sample_user_id,
                payload=sample_turn_payload,
            )

    @pytest.mark.asyncio
    async def test_process_turn_permission_denied(
        self, service, mock_case_repository, sample_case, sample_turn_payload
    ):
        """Test turn processing with unauthorized user."""
        # Pre-populate repository with case owned by different user
        await mock_case_repository.save(sample_case)
        unauthorized_user_id = str(uuid4())

        with pytest.raises(PermissionDeniedException, match="not authorized"):
            await service.process_turn(
                case_id=sample_case.case_id,
                user_id=unauthorized_user_id,
                payload=sample_turn_payload,
            )

    @pytest.mark.asyncio
    async def test_process_turn_saves_user_message(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
        sample_turn_payload,
    ):
        """The committed case carries the user message and the agent reply.

        The docstring used to say "saved before processing", which described
        the pre-#184 ordering and had been false since the save was deferred to
        the end of the turn. This asserts post-success state only; the
        deferral itself is pinned by the failure-path test below.
        """
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)
        initial_message_count = sample_case.message_count

        await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        # Verify case was saved with user message and agent response
        saved_case = await mock_case_repository.get(sample_case.case_id)
        assert (
            saved_case.message_count == initial_message_count + 2
        )  # User message + agent response
        assert (
            len(saved_case.messages) >= 2
        ), f"Expected at least 2 messages, got {len(saved_case.messages)}"
        user_messages = [m for m in saved_case.messages if m.get("role") == "user"]
        assert len(user_messages) >= 1, "User message should be saved"
        assert any(
            m["content"] == sample_turn_payload.query for m in user_messages
        ), f"User message '{sample_turn_payload.query}' not found in saved messages"

    @pytest.mark.asyncio
    async def test_failed_turn_commits_nothing_on_the_straight_line_path(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
        sample_turn_payload,
    ):
        """An engine failure must not commit the user message or the turn bump.

        This is the property the deferred save exists for, and nothing asserted
        it: `test_process_turn_saves_user_message` only checks post-success
        state, so moving the save back above the engine call shipped green.

        ⚠️ Asserted on what reached ``save()``, NEVER on the stored case.
        ``MockCaseRepository`` stores BY REFERENCE
        (``_storage[case.case_id] = case``) and ``_get`` hands the same object
        back, so the service's in-memory mutations are visible through the
        repository whether or not anything was ever saved. Measured, not
        assumed: after a failed turn ``(await repo.get(...)).message_count``
        reads 1 BOTH with the deferral and with a ``save`` restored above the
        engine call — identical, so an assertion on it cannot tell the two
        apart. Only the save calls differ (1 vs 2). Hence the snapshot taken at
        each save; this test was confirmed to go red with the deferral removed.

        Scope is a failure IN the engine call, which is all the deferral covers.
        It does NOT extend to a failure after the engine returns: the real
        ``MilestoneEngine`` saves the case unconditionally at its Step 7, before
        the service appends the agent reply, so a post-engine failure leaves the
        user message durable and the reply missing. The mock engine raises, so
        that save never happens and this test sees the deferral in isolation.
        See the STEP-2 comment in ``process_turn`` for the full ordering.
        """
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        # Snapshot state AS COMMITTED, at the moment of each save.
        committed: list[tuple[int, int]] = []
        underlying_save = mock_case_repository.save.side_effect

        async def _snapshotting_save(case):
            committed.append((case.message_count, case.current_turn))
            return await underlying_save(case)

        mock_case_repository.save.side_effect = _snapshotting_save

        mock_milestone_engine.process_turn.side_effect = RuntimeError(
            "LLM provider exploded"
        )

        with pytest.raises(ServiceException):
            await service.process_turn(
                case_id=sample_case.case_id,
                user_id=sample_user_id,
                payload=sample_turn_payload,
            )

        assert committed == [], (
            "A failed turn reached the database: save() was called with "
            f"(message_count, current_turn)={committed!r}. The user message "
            "and/or the turn bump were committed for a turn that produced no "
            "agent reply."
        )

    @pytest.mark.asyncio
    async def test_process_turn_persists_engine_metadata_on_assistant_message(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
        sample_turn_payload,
    ):
        """Regression: rich turn metadata emitted by the engine must
        propagate to the persisted assistant message.

        Before fix: investigation_service hardcoded ``metadata={}`` on the
        assistant message, dropping all engine metadata fields. This made
        post-hoc diagnosis from persisted turn data impossible.
        """
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        # Self-consistent on purpose (#1270): a fired ``milestones_completed``
        # beside ``progress_made: False`` is the shape the predicate cannot
        # produce and the telemetry reference calls unemittable, so asserting
        # that it round-trips would pin a row the engine must never write. The
        # milestone is what makes the turn a progress turn, so the fixture says
        # so, and propagation is still measured across all three keys.
        engine_metadata = {
            "milestones_completed": ["symptom_verified"],
            "progress_made": True,
            "next_steps": ["check db connection pool size"],
        }

        async def _engine_that_recorded_its_turn(*, case, **_kwargs):
            """Record the turn the way the real engine's Step 6 does.

            Without it ``_backfill_consumed_turn`` treats this as one of the
            three engine-bypassing routes and re-scores the dict (#1270), which
            would make this test measure the backstop rather than propagation.
            """
            from faultmaven.core.investigation.turn_outcome import TurnOutcome
            from faultmaven.modules.case.domain.models import TurnProgress

            case.turn_history.append(
                TurnProgress(
                    turn_number=case.current_turn,
                    timestamp=datetime.now(timezone.utc),
                    milestones_completed=["symptom_verified"],
                    progress_made=True,
                    outcome=TurnOutcome.CONVERSATION,
                )
            )
            return {
                "case_updated": case,
                "agent_response": "agent response text",
                "metadata": engine_metadata,
            }

        mock_milestone_engine.process_turn = AsyncMock(
            side_effect=_engine_that_recorded_its_turn
        )

        await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        saved_case = await mock_case_repository.get(sample_case.case_id)
        assistant_messages = [
            m for m in saved_case.messages if m.get("role") == "assistant"
        ]
        assert assistant_messages, "expected at least one assistant message"
        persisted = assistant_messages[-1].get("metadata") or {}

        assert persisted.get("milestones_completed") == ["symptom_verified"]
        assert persisted.get("progress_made") is True
        assert persisted.get("next_steps") == ["check db connection pool size"]

    @pytest.mark.asyncio
    async def test_process_turn_saves_agent_response(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
        sample_turn_payload,
    ):
        """Test that agent response is saved after processing."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        # Verify case was saved with agent response.
        # Role is "assistant" (OpenAI/Anthropic convention) — the legacy
        # "agent" role was renamed in investigation_service so the persisted
        # transcript stays compatible with provider message-role norms.
        saved_case = await mock_case_repository.get(sample_case.case_id)
        agent_messages = [
            m for m in saved_case.messages if m.get("role") == "assistant"
        ]
        assert len(agent_messages) >= 1
        assert agent_messages[-1]["content"] == response.agent_response

    @pytest.mark.asyncio
    async def test_process_turn_increments_turn_number(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
        sample_turn_payload,
    ):
        """Test that turn number is incremented."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        initial_turn = sample_case.current_turn  # Should be 0 from fixture
        await mock_case_repository.save(sample_case)

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        # Service flow:
        # 1. Gets case (current_turn = initial_turn, e.g., 0)
        # 2. Preprocesses any attachments (Step 1)
        # 3. Adds user message with turn_number = current_turn + 1 (e.g., 1)
        # 4. Engine processes (Step 2 - LLM inference)
        # 5. Service commits turn increment: current_turn = next_turn (e.g., 1)
        # 6. Response has turn_number = updated_case.current_turn (e.g., 1)
        expected_turn = initial_turn + 1
        assert (
            response.turn_number == expected_turn
        ), f"Expected turn_number={expected_turn}, got {response.turn_number} (initial was {initial_turn})"

        saved_case = await mock_case_repository.get(sample_case.case_id)
        assert (
            saved_case.current_turn == expected_turn
        ), f"Expected saved_case.current_turn={expected_turn}, got {saved_case.current_turn}"

    @pytest.mark.asyncio
    async def test_process_turn_with_attachments(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
    ):
        """Test turn processing with file attachments (Step 1 preprocessing)."""
        from unittest.mock import AsyncMock

        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        # Mock preprocessing service for attachment processing
        mock_preprocessing = AsyncMock()
        mock_preprocessing.classify_and_extract = AsyncMock(
            return_value=AsyncMock(
                summary="Test log file summary",
                structural_index="ERROR line 42: connection refused",
                data_type=AsyncMock(value="logs"),
                content_hash="abc123",
                extraction_method="structure_extraction",
            )
        )
        service.preprocessing_service = mock_preprocessing

        attachments = [
            Attachment(
                content=b"error log content here",
                filename="app.log",
                content_type="text/plain",
            ),
        ]

        payload = TurnPayload(
            query="Check this log file",
            attachments=attachments,
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        response = await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=payload,
        )

        assert response.agent_response is not None
        assert len(response.attachments_processed) == 1
        assert response.attachments_processed[0].filename == "app.log"
        assert response.attachments_processed[0].processing_status == "completed"
        # Verify preprocessing was called
        mock_preprocessing.classify_and_extract.assert_called_once()

    @pytest.mark.asyncio
    async def test_attachment_metadata_includes_required_fields(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
    ):
        """The dict handed to ``engine.process_turn`` carries the whole set of
        keys the engine reads by name — ``file_id``, ``filename``, ``size``,
        ``source_type``, ``data_type``, ``summary``, ``storage_ref``,
        ``is_novel`` — sourced from the persisted ``UploadedFile`` row plus the
        preprocessing result's dedup answer.

        The original reason was narrower and no longer exists: the engine used
        to mint its own ``UploadedFile`` from this dict, and a missing ``size``
        defaulted ``size_bytes=0`` and violated the
        ``uploaded_files_size_positive`` CHECK. That minting was deleted with
        #1210 (the service owns the rows). The dict is still the dispatch
        contract, so the shape stays pinned here — see
        ``test_engine_attachment_provenance_1201.py`` for the per-key
        derivations and ``test_novel_upload_thread_1210.py`` for ``is_novel``.
        """
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        mock_preprocessing = AsyncMock()
        mock_preprocessing.classify_and_extract = AsyncMock(
            return_value=AsyncMock(
                summary="Pasted text summary",
                structural_index="Some extracted content",
                data_type=AsyncMock(value="text"),
                content_hash="hash123",
                extraction_method="structure_extraction",
            )
        )
        service.preprocessing_service = mock_preprocessing

        pasted_content = b"some pasted log data with enough bytes"
        attachments = [
            Attachment(
                content=pasted_content,
                filename="pasted-content-20260223T120000.txt",
                content_type="text/plain",
            ),
        ]

        payload = TurnPayload(
            query="Analyze this",
            attachments=attachments,
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=payload,
        )

        # Verify engine.process_turn was called with enriched attachment metadata
        call_kwargs = mock_milestone_engine.process_turn.call_args
        metadata_list = call_kwargs.kwargs.get("attachments") or call_kwargs[1].get(
            "attachments"
        )
        assert metadata_list is not None, "attachments should be passed to engine"
        assert len(metadata_list) == 1

        meta = metadata_list[0]
        assert meta["filename"] == "pasted-content-20260223T120000.txt"
        assert meta["size"] == len(
            pasted_content
        ), "size must match original content bytes"
        assert meta["size"] > 0, "size must be > 0 (CHECK constraint)"
        assert meta["source_type"] == "text_paste", (
            # Was "paste" until #1201. The value is now derived from the ROW
            # (``UploadedFile.input_origin``) rather than from the shape of the
            # submitted filename, and it reports the CANONICAL spelling the
            # turns route writes. Both spellings live in
            # ``_PASTE_UPLOAD_SOURCES``, so every consumer reads either; this
            # asserts the one the row actually carries.
            "pasted content should have source_type='text_paste'"
        )
        # Post-010: attachment metadata no longer carries evidence_id —
        # Evidence is born only when the LLM extracts a claim-anchored
        # slice during INVESTIGATING. The UploadedFile is the file-of-
        # record for intake.
        assert "evidence_id" not in meta
        assert "file_id" in meta
        # file_id must match UploadedFile pattern: ^(file_|data_)[a-f0-9]{12,16}$
        assert meta["file_id"].startswith(
            "file_"
        ), "uploaded file ids use the file_ prefix"

    @pytest.mark.asyncio
    async def test_file_upload_attachment_metadata_source_type(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
    ):
        """File uploads should have source_type='file_upload' in attachment metadata."""
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        mock_preprocessing = AsyncMock()
        mock_preprocessing.classify_and_extract = AsyncMock(
            return_value=AsyncMock(
                summary="Log file summary",
                structural_index="ERROR at line 42",
                data_type=AsyncMock(value="logs"),
                content_hash="hash456",
                extraction_method="structure_extraction",
            )
        )
        service.preprocessing_service = mock_preprocessing

        file_content = b"2024-01-01 ERROR connection refused\n" * 10
        attachments = [
            Attachment(
                content=file_content,
                filename="server.log",
                content_type="text/plain",
            ),
        ]

        payload = TurnPayload(
            query="Check this log",
            attachments=attachments,
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )

        await service.process_turn(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
            payload=payload,
        )

        call_kwargs = mock_milestone_engine.process_turn.call_args
        metadata_list = call_kwargs.kwargs.get("attachments") or call_kwargs[1].get(
            "attachments"
        )
        assert metadata_list is not None
        meta = metadata_list[0]
        assert meta["source_type"] == "file_upload"
        assert meta["filename"] == "server.log"
        assert meta["size"] == len(file_content)
        assert meta["size"] > 0
        assert meta["file_id"].startswith(
            "file_"
        ), "file upload file_id should use file_ prefix"


class TestInvestigationServiceIntentDispatch:
    """Pins the dispatch wiring for intents.

    The investigation_service dispatcher must route every ``IntentType``
    enum value (either to ``engine.process_turn`` or to a ValidationException
    for not-yet-implemented intents), never hitting a 500-class
    ``raise ValueError("Unknown intent type: ...")``.

    NOTE (investigation-flow redesign): the PATH_SELECTION (Gate 2) and
    POST_MITIGATION_CHOICE (Gate 3) intents were removed with the path
    fork, so their dispatch tests are gone — the exhaustiveness test below
    is the surviving guard.
    """

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.mark.parametrize("intent_value", list(IntentType))
    @pytest.mark.asyncio
    async def test_every_intent_type_is_routed(
        self,
        intent_value,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
    ):
        """Parametrized exhaustiveness: every ``IntentType`` enum value must
        either dispatch without "Unknown intent type" error or raise a
        ValidationException (NOT_IMPLEMENTED). A 500-class ``ServiceException``
        wrapping an unknown-intent ``ValueError`` is the regression shape we
        are preventing.

        Adding a new IntentType without a dispatch route would either fail
        the boot check at service construction OR fall through to an
        unhandled branch — both surfaces caught here.
        """
        from faultmaven.exceptions import ServiceException, ValidationException

        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        # Build a minimally-valid QueryIntent for each intent value.
        # Each intent type has its own required-field constraints
        # (validated by QueryIntent.model_validator); we satisfy them with
        # stub data so the test focuses on dispatch wiring, not intent
        # validation.
        intent_kwargs: dict = {"type": intent_value}
        if intent_value == IntentType.STATUS_TRANSITION:
            intent_kwargs["to_state"] = "closed"
        elif intent_value == IntentType.CONFIRMATION:
            intent_kwargs["confirmation_value"] = True
        elif intent_value == IntentType.HYPOTHESIS_ACTION:
            intent_kwargs["hypothesis_id"] = "hyp_test"
            intent_kwargs["action"] = "accept"
        elif intent_value == IntentType.EVIDENCE_NEED:
            intent_kwargs["evidence_need_id"] = "eneed_test12345"
        elif intent_value == IntentType.FILE_RECLASSIFICATION:
            intent_kwargs["file_id"] = "file_test12345678"
            intent_kwargs["data_type"] = "logs_and_errors"

        payload = TurnPayload(query="test", intent=QueryIntent(**intent_kwargs))

        try:
            await service.process_turn(
                case_id=sample_case.case_id,
                user_id=sample_user_id,
                payload=payload,
            )
        except ValidationException:
            # NOT_IMPLEMENTED intents (e.g., EVIDENCE_NEED) raise this
            # — 422 to the client, contract gap surfaced honestly.
            pass
        except NotFoundError:
            # A contract-valid intent may reference a resource the sample
            # case lacks (FILE_RECLASSIFICATION's stub file_id) — 404 to
            # the client; the dispatch itself routed correctly.
            pass
        except ServiceException as e:
            # Two regression shapes surface as ServiceException (500):
            # the legacy elif-chain "Unknown intent type" error, and the
            # SERVICE-dispatch else-branch "has no handler method" (a
            # dispatch-table entry without a matching elif in
            # process_turn — the boot check can't see that gap).
            assert "Unknown intent type" not in str(e), (
                f"IntentType.{intent_value.name} surfaced as 500 with "
                f"'Unknown intent type'. Dispatch table is incomplete."
            )
            assert "has no handler method" not in str(e), (
                f"IntentType.{intent_value.name} is SERVICE-routed in "
                f"_INTENT_DISPATCH but has no handler branch in "
                f"process_turn."
            )

    def test_boot_check_rejects_incomplete_dispatch_table(
        self, mock_milestone_engine, mock_case_repository
    ):
        """Service construction must fail if any IntentType lacks a dispatch
        route. Converts the silent-runtime-500 failure mode into a
        startup-time error caught by CI.
        """
        from unittest.mock import patch

        from faultmaven.modules.agent.domain.services import investigation_service

        # Simulate a developer who added a new IntentType without updating
        # the dispatch table.
        broken_dispatch = dict(investigation_service._INTENT_DISPATCH)
        del broken_dispatch[IntentType.CONFIRMATION]

        with patch.object(investigation_service, "_INTENT_DISPATCH", broken_dispatch):
            with pytest.raises(RuntimeError) as exc_info:
                InvestigationService(
                    milestone_engine=mock_milestone_engine,
                    case_repository=mock_case_repository,
                )
        assert "confirmation" in str(exc_info.value)
        assert "incomplete" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_not_implemented_intent_raises_validation_exception(
        self,
        service,
        mock_case_repository,
        mock_milestone_engine,
        sample_case,
        sample_user_id,
    ):
        """Intents marked NOT_IMPLEMENTED in the dispatch table must raise
        ValidationException (422), not ServiceException (500). EVIDENCE_NEED
        is currently in this state — defined in the enum + QueryIntent
        validator but with no handler.
        """
        from faultmaven.exceptions import ValidationException

        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        payload = TurnPayload(
            query="test",
            intent=QueryIntent(
                type=IntentType.EVIDENCE_NEED, evidence_need_id="eneed_test12345"
            ),
        )

        with pytest.raises(ValidationException) as exc_info:
            await service.process_turn(
                case_id=sample_case.case_id,
                user_id=sample_user_id,
                payload=payload,
            )
        assert "not implemented" in str(exc_info.value).lower()


class TestInvestigationServiceGetProgress:
    """Tests for InvestigationService.get_progress()."""

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        """Create InvestigationService with mocked dependencies."""
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.mark.asyncio
    async def test_get_progress_success(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test successful progress retrieval."""
        # Pre-populate repository - ensure sample_case has user_id matching sample_user_id
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)

        progress = await service.get_progress(
            case_id=sample_case.case_id,
            user_id=sample_user_id,
        )

        assert progress["case_id"] == sample_case.case_id
        assert progress["state"] == sample_case.state.value
        assert "current_turn" in progress
        assert (
            "milestones_completed" in progress
        )  # InvestigationProgress.completed_milestones property
        assert (
            "pending_milestones" in progress
        )  # InvestigationProgress.pending_milestones property
        # Note: completion_percentage was removed from InvestigationProgress model

    @pytest.mark.asyncio
    async def test_get_progress_case_not_found(
        self, service, mock_case_repository, sample_user_id
    ):
        """Test progress retrieval with non-existent case."""
        non_existent_case_id = f"case_{uuid4().hex[:12]}"

        with pytest.raises(NotFoundError, match="Case"):
            await service.get_progress(
                case_id=non_existent_case_id,
                user_id=sample_user_id,
            )

    @pytest.mark.asyncio
    async def test_get_progress_permission_denied(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test progress retrieval with unauthorized user."""
        # Pre-populate repository - set sample_case to have sample_user_id as owner
        sample_case.user_id = sample_user_id
        await mock_case_repository.save(sample_case)
        unauthorized_user_id = str(uuid4())  # Different user (not the owner)

        with pytest.raises(PermissionDeniedException, match="not authorized"):
            await service.get_progress(
                case_id=sample_case.case_id,
                user_id=unauthorized_user_id,
            )


class TestInvestigationServiceTransitionToInvestigating:
    """Tests for InvestigationService.transition_to_investigating()."""

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        """Create InvestigationService with mocked dependencies."""
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @pytest.fixture
    def inquiry_case(self):
        """Create a case in INQUIRY state."""
        return create_sample_case(
            state=CaseState.INQUIRY,
        )

    @pytest.mark.asyncio
    async def test_transition_to_investigating_success(
        self, service, mock_case_repository, inquiry_case, sample_user_id
    ):
        """Test successful transition to INVESTIGATING."""
        # Pre-populate repository
        inquiry_case.user_id = sample_user_id
        await mock_case_repository.save(inquiry_case)

        confirmed_description = "Confirmed problem description"
        updated_case = await service.transition_to_investigating(
            case_id=inquiry_case.case_id,
            user_id=sample_user_id,
            confirmed_description=confirmed_description,
        )

        assert updated_case.state == CaseState.INVESTIGATING
        assert updated_case.description == confirmed_description
        assert mock_case_repository.save.called

    @pytest.mark.asyncio
    async def test_transition_to_investigating_case_not_found(
        self, service, mock_case_repository, sample_user_id
    ):
        """Test transition with non-existent case."""
        non_existent_case_id = f"case_{uuid4().hex[:12]}"

        with pytest.raises(NotFoundError, match="Case"):
            await service.transition_to_investigating(
                case_id=non_existent_case_id,
                user_id=sample_user_id,
                confirmed_description="Test description",
            )

    @pytest.mark.asyncio
    async def test_transition_to_investigating_permission_denied(
        self, service, mock_case_repository, inquiry_case
    ):
        """Test transition with unauthorized user."""
        # Pre-populate repository
        await mock_case_repository.save(inquiry_case)
        unauthorized_user_id = str(uuid4())

        with pytest.raises(PermissionDeniedException, match="not authorized"):
            await service.transition_to_investigating(
                case_id=inquiry_case.case_id,
                user_id=unauthorized_user_id,
                confirmed_description="Test description",
            )

    @pytest.mark.asyncio
    async def test_transition_to_investigating_invalid_status(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Test transition from non-INQUIRY state."""
        # Pre-populate repository with case in INVESTIGATING state (cannot transition from non-INQUIRY)
        # INVESTIGATING state requires: confirmed problem statement, decided to investigate, and description
        from datetime import datetime, timezone

        sample_case.user_id = sample_user_id
        sample_case.description = "Test description"  # Required for INVESTIGATING
        # Set up inquiry data required for INVESTIGATING state
        sample_case.inquiry.proposed_problem_statement = "Test problem statement"
        sample_case.inquiry.problem_statement_confirmed = True
        sample_case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
        sample_case.inquiry.decided_to_investigate = True
        sample_case.inquiry.decision_made_at = datetime.now(timezone.utc)

        # Now set state to INVESTIGATING (all requirements are met)
        sample_case.state = CaseState.INVESTIGATING
        await mock_case_repository.save(sample_case)

        # Verify the case is stored with INVESTIGATING state
        stored_case = await mock_case_repository.get(sample_case.case_id)
        assert stored_case is not None, "Case should be stored in repository"
        assert (
            stored_case.state == CaseState.INVESTIGATING
        ), f"Case state should be INVESTIGATING, got {stored_case.state}"
        assert (
            stored_case.state != CaseState.INQUIRY
        ), "Case should NOT be in INQUIRY state for this test"

        # The service should raise ServiceException when trying to transition from non-INQUIRY state
        with pytest.raises(ServiceException) as exc_info:
            await service.transition_to_investigating(
                case_id=sample_case.case_id,
                user_id=sample_user_id,
                confirmed_description="Test description",
            )

        # Verify the exception message contains "Cannot transition"
        assert "Cannot transition" in str(
            exc_info.value
        ), f"Exception message should contain 'Cannot transition', got: {str(exc_info.value)}"
        assert "investigating" in str(exc_info.value).lower() or "INVESTIGATING" in str(
            exc_info.value
        ), f"Exception message should mention the current state, got: {str(exc_info.value)}"


class TestBuildProgressTransparencyVerificationStatus:
    """Phase 3: the honest-partial verification_status must reach the live
    TurnResponse even when transparent mode (the time-stall detector) is not
    active — a declared data wall reaches INSUFFICIENT_EVIDENCE first."""

    def _service(self):
        from unittest.mock import MagicMock

        return InvestigationService(
            milestone_engine=MagicMock(),
            case_repository=MagicMock(),
        )

    def _case(self, status):
        from faultmaven.modules.case.domain.models import (
            InvestigationProgress,
            VerificationStatus,
        )

        case = create_sample_case()
        case.progress = InvestigationProgress(
            verification_status=getattr(VerificationStatus, status)
        )
        return case

    def test_insufficient_evidence_surfaced_without_transparent_mode(self):
        svc = self._service()
        case = self._case("INSUFFICIENT_EVIDENCE")
        # No progress_transparent flag → previously returned None, hiding the
        # honest partial. Now it must surface with active reflecting the stall.
        info = svc._build_progress_transparency({}, case)
        assert info is not None
        assert info.active is False
        assert info.verification_status == "insufficient_evidence"

    def test_non_insufficient_status_stays_silent_without_transparent_mode(self):
        svc = self._service()
        case = self._case("OPEN")
        # OPEN is not the honest-partial; without transparent mode, stay silent.
        assert svc._build_progress_transparency({}, case) is None

    def test_restatement_held_surfaced_without_transparent_mode(self):
        """#1195: ``RESTATEMENT_HELD`` is carved OUT of ``INSUFFICIENT_EVIDENCE``,
        so it must reach this channel for the same reason its parent does. Omit
        it and the carve-out silences, in the user-facing transparency block,
        exactly the cases that block used to (wrongly) report — suppression
        without replacement, which is the failure #1195 exists to avoid."""
        svc = self._service()
        case = self._case("RESTATEMENT_HELD")
        info = svc._build_progress_transparency({}, case)
        assert info is not None
        assert info.active is False
        assert info.verification_status == "restatement_held"

    def test_treatment_blocked_surfaced_without_transparent_mode(self):
        """#1136's honest partial, pinned alongside — a fix-blocked stall is
        conversational, so transparent mode may never activate on it."""
        svc = self._service()
        case = self._case("TREATMENT_BLOCKED")
        info = svc._build_progress_transparency({}, case)
        assert info is not None
        assert info.verification_status == "treatment_blocked"

    def test_transparent_mode_still_active_and_carries_status(self):
        svc = self._service()
        case = self._case("INSUFFICIENT_EVIDENCE")
        info = svc._build_progress_transparency(
            {
                "progress_transparent": True,
                "pending_milestone": "root_cause_identified",
            },
            case,
        )
        assert info is not None
        assert info.active is True
        assert info.verification_status == "insufficient_evidence"
        assert info.pending_milestone == "root_cause_identified"

    def test_cause_assurance_carried_alongside_status(self):
        # #572: the persisted assurance grade rides the same surfacing object so
        # the frontend can label a lower-assurance conclusion.
        from faultmaven.modules.case.domain.models import CauseAssuranceGrade

        svc = self._service()
        case = self._case("INSUFFICIENT_EVIDENCE")
        case.progress.cause_assurance = CauseAssuranceGrade.MECHANISTIC
        info = svc._build_progress_transparency({}, case)
        assert info is not None
        assert info.cause_assurance == "mechanistic"


class TestTurnResponseCauseAssurance:
    """#572 / INV-28 §3.5: the per-turn response carries the assurance grade
    beside the cause, so a narration-only client (Slack) can label the cause
    claim the LLM wrote into agent_response instead of forwarding it bare.

    The grade is recomputed from the causal graph (not the persisted field), so a
    resolution turn — which never recomputes the persisted progress field — still
    carries the true grade. Mechanical / LLM-agnostic: graph shape decides.
    """

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    def _case_with_confirmed_root(self, user_id: str) -> Case:
        from datetime import UTC

        from faultmaven.modules.case.domain.models import (
            CausalNode,
            ConfidenceLevel,
            Evidence,
            EvidenceCategory,
            EvidenceSourceType,
            EvidenceStance,
            NodeEvidenceLink,
            NodeState,
            NodeType,
            RootCauseConclusion,
            ValidationMethod,
        )

        # Non-terminal (process_turn rejects terminal cases) with an identified,
        # counterfactually-confirmed cause. State is immaterial to the grade — it
        # is recomputed from the graph — so the default INQUIRY state, which needs
        # no confirmed-problem-statement fixture, keeps the builder minimal.
        case = create_sample_case(user_id=user_id, current_turn=3)
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="Connection pool exhausted",
            mechanism="pool saturation queues requests past the timeout",
            confidence_level=ConfidenceLevel.CONFIDENT,
            likelihood=0.8,
        )

        def _ev(eid, cat):
            return Evidence(
                evidence_id=eid,
                summary="an observed fact",
                primary_purpose="diagnosis",
                category=cat,
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                collected_by="u",
                collected_at_turn=1,
                collected_at=datetime(2026, 7, 4, 11, 0, 0, tzinfo=UTC),
            )

        case.evidence = [
            _ev("ev_aaaaaaaaaaaa", EvidenceCategory.CAUSAL_EVIDENCE),
            _ev("ev_bbbbbbbbbbbb", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE),
        ]
        case.causal_nodes = {
            "cn_aaaaaaaaaaaa": CausalNode(
                node_id="cn_aaaaaaaaaaaa",
                statement="connection pool exhausted",
                node_type=NodeType.ROOT,
                node_state=NodeState.VALIDATED,
                validation_method=ValidationMethod.EMPIRICAL,
                actionable=True,
                belief=0.8,
                generated_at_turn=1,
                evidence_links=[
                    NodeEvidenceLink(
                        evidence_id="ev_aaaaaaaaaaaa",
                        stance=EvidenceStance.SUPPORTS,
                        reasoning="pool metrics",
                        linked_at_turn=1,
                    ),
                    NodeEvidenceLink(
                        evidence_id="ev_bbbbbbbbbbbb",
                        stance=EvidenceStance.SUPPORTS,
                        reasoning="removing the cause removed the problem",
                        linked_at_turn=2,
                    ),
                ],
            )
        }
        return case

    @pytest.mark.asyncio
    async def test_turn_with_identified_cause_carries_grade(
        self, service, mock_case_repository, sample_user_id, sample_turn_payload
    ):
        case = self._case_with_confirmed_root(sample_user_id)
        await mock_case_repository.save(case)

        response = await service.process_turn(
            case_id=case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        assert response.cause_assurance == "confirmed"
        assert response.cause_overclaim is False

    @pytest.mark.asyncio
    async def test_turn_without_cause_omits_grade(
        self, service, mock_case_repository, sample_user_id, sample_turn_payload
    ):
        # No root_cause_conclusion → nothing to label; fields stay None.
        case = create_sample_case(user_id=sample_user_id)
        assert case.root_cause_conclusion is None
        await mock_case_repository.save(case)

        response = await service.process_turn(
            case_id=case.case_id,
            user_id=sample_user_id,
            payload=sample_turn_payload,
        )

        assert response.cause_assurance is None
        assert response.cause_overclaim is None


class TestMintedIntentTerminalConsentAdoption:
    """#721 adoption-site pins: a classifier-minted confirmation must not
    dispatch as consent to a pending terminal transition when the typed
    message is substantive (INV-26). See
    tests/unit/core/investigation/test_terminal_confirmation_integrity.py
    for the predicate-level matrix — these tests drive the REAL adoption
    path in process_turn with the resolver mocked at its boundary."""

    # ``offered_turn`` is not decoration: the adoption site only lets the
    # resolver see LIVE entries (#1245), and an unstamped one is not live.
    # Without it these tests would pass the resolver an empty list, never
    # reach the INV-26 guard at all, and land on ``conversation`` — which is
    # what the substantive-message test asserts, so it would go green
    # vacuously while the guard it exists to pin went unexercised.
    # ``create_sample_case`` starts at turn 0, so the offer is turn 0 and the
    # turn under test is turn 1 — one turn old, the follow-up window.
    RESOLVE_SUGGESTIONS = [
        {
            "label": "Yes, mark as resolved",
            "payload": "Yes, mark as resolved",
            "intent": {"type": "confirmation", "confirmation_value": True},
            "offered_turn": 0,
        }
    ]

    def _pending_case(self, user_id: str) -> Case:
        case = create_sample_case(user_id=user_id)
        case.inquiry.proposed_problem_statement = "Test problem"
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.decided_to_investigate = True
        case.state = CaseState.INVESTIGATING
        case.pending_transition = {
            "to_state": "resolved",
            "summary": "Confirm resolution?",
            "evidence_ids": [],
            "proposed_at": datetime.now(timezone.utc).isoformat(),
        }
        case.last_suggestions = list(self.RESOLVE_SUGGESTIONS)
        return case

    async def _run_turn(self, query: str, user_id: str, case, engine, repo):
        service = InvestigationService(milestone_engine=engine, case_repository=repo)
        await repo.save(case)
        # Simulate the Tier-2 classifier semantically matching the typed
        # text to the resolve suggestion — the exact #721 scenario.
        service.intent_resolver.resolve = AsyncMock(
            return_value={"type": "confirmation", "confirmation_value": True}
        )
        await service.process_turn(
            case_id=case.case_id,
            user_id=user_id,
            payload=TurnPayload(query=query, attachments=[]),
        )
        return engine.process_turn.call_args.kwargs

    @pytest.mark.asyncio
    async def test_substantive_message_does_not_dispatch_confirmation(
        self, mock_milestone_engine, mock_case_repository, sample_user_id
    ):
        case = self._pending_case(sample_user_id)
        kwargs = await self._run_turn(
            "yes but what about the replication lag?",
            sample_user_id,
            case,
            mock_milestone_engine,
            mock_case_repository,
        )
        assert kwargs.get("intent_type") != "confirmation", (
            "#721: a substantive typed message reached the engine as a "
            "confirmation intent — the classifier mint swallowed it as "
            "consent to an irreversible transition."
        )
        assert kwargs.get("intent_type") == "conversation"

    @pytest.mark.asyncio
    async def test_bare_message_still_dispatches_minted_confirmation(
        self, mock_milestone_engine, mock_case_repository, sample_user_id
    ):
        """The classifier tier's legitimate value: a bare phrasing outside
        the engine's pattern list still confirms via the minted intent."""
        case = self._pending_case(sample_user_id)
        kwargs = await self._run_turn(
            "affirmative",
            sample_user_id,
            case,
            mock_milestone_engine,
            mock_case_repository,
        )
        assert kwargs.get("intent_type") == "confirmation"


class TestObservedAtSeedsFileCoverage:
    """`Attachment.observed_at` seeds the file's coverage window — but only
    when the content carries no timestamps of its own.

    These build a REAL ``PreprocessingResult`` rather than an ``AsyncMock``.
    With a mock, ``coverage_start_ts`` is a Mock object rather than None, so
    the `is None` precedence check is never actually exercised and a mutation
    that drops it passes.
    """

    @pytest.fixture
    def service(self, mock_milestone_engine, mock_case_repository):
        return InvestigationService(
            milestone_engine=mock_milestone_engine,
            case_repository=mock_case_repository,
        )

    @staticmethod
    def _result(*, start=None, end=None):
        from faultmaven.core.preprocessing.models import PreprocessingResult

        return PreprocessingResult(
            data_type="logs",
            detailed_data_type="logs_and_errors",
            summary="one alert line",
            structural_index="[FIRING:1] etcdInsufficientMembers",
            content_size_bytes=42,
            content_type="text/plain",
            extraction_method="structure_extraction",
            compression_ratio=1.0,
            content_hash="hash_observed_at",
            coverage_start_ts=start,
            coverage_end_ts=end,
        )

    async def _run(self, service, case, repo, user_id, result, observed_at):
        preprocessing = AsyncMock()
        preprocessing.classify_and_extract = AsyncMock(return_value=result)
        service.preprocessing_service = preprocessing

        case.user_id = user_id
        await repo.save(case)
        await service.process_turn(
            case_id=case.case_id,
            user_id=user_id,
            payload=TurnPayload(
                query="investigate",
                attachments=[
                    Attachment(
                        content=b"[FIRING:1] etcdInsufficientMembers kube-system",
                        filename="pasted-content-20260804T193617.txt",
                        content_type="text/plain",
                        observed_at=observed_at,
                    )
                ],
                intent=QueryIntent(type=IntentType.CONVERSATION),
            ),
        )
        stored = await repo.get(case.case_id)
        return stored.uploaded_files[-1]

    @pytest.mark.asyncio
    async def test_seeds_coverage_when_content_has_no_timestamps(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """The alert-notification case: one prose line, nothing to parse. The
        caller's observation time is the only temporal signal there is."""

        observed = datetime(2026, 8, 4, 17, 36, 17, tzinfo=timezone.utc)
        uploaded = await self._run(
            service,
            sample_case,
            mock_case_repository,
            sample_user_id,
            self._result(),
            observed,
        )
        assert uploaded.coverage_start_ts == observed
        assert uploaded.coverage_end_ts == observed

    @pytest.mark.asyncio
    async def test_parsed_content_wins_over_the_callers_claim(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """Parsed timestamps describe what the data actually spans;
        `observed_at` is only the caller's statement about when it saw the
        content. The data must win."""

        parsed_start = datetime(2026, 8, 4, 10, 0, 0, tzinfo=timezone.utc)
        parsed_end = datetime(2026, 8, 4, 11, 0, 0, tzinfo=timezone.utc)
        uploaded = await self._run(
            service,
            sample_case,
            mock_case_repository,
            sample_user_id,
            self._result(start=parsed_start, end=parsed_end),
            datetime(2026, 8, 4, 17, 36, 17, tzinfo=timezone.utc),
        )
        assert uploaded.coverage_start_ts == parsed_start
        assert uploaded.coverage_end_ts == parsed_end

    @pytest.mark.asyncio
    async def test_coverage_stays_unknown_when_nobody_declared_anything(
        self, service, mock_case_repository, sample_case, sample_user_id
    ):
        """No parsed timestamps and no caller claim must leave NULL, not now —
        a reader has to be able to tell "old" from "unknown"."""

        uploaded = await self._run(
            service,
            sample_case,
            mock_case_repository,
            sample_user_id,
            self._result(),
            None,
        )
        assert uploaded.coverage_start_ts is None
        assert uploaded.coverage_end_ts is None
