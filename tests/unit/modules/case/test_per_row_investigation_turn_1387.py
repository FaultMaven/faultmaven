"""#1387 — every conversation row carries its own investigation turn.

#1329 computed the investigation turn and published it on ``TurnResponse``
alone, which is the reply to a submitted turn and the one schema no history
read can reach. This pins the two things that make the number usable off that
reply: it is a per-row ORDINAL rather than the case-level total repeated, and
it is derived from the same source as ``Case.investigation_turn_count`` so the
label on the live row and the label on that row after a reload agree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock
from unittest.mock import AsyncMock

import pytest

from faultmaven.models.api import Message
from faultmaven.models.api_models import (
    AdminCaseMetadata,
    CaseDetail,
    CaseSummary,
)
from faultmaven.models.case_ui import (
    CaseUIResponse_Inquiry,
    CaseUIResponse_Investigating,
    CaseUIResponse_Resolved,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    TurnOutcome,
    TurnProgress,
)
from faultmaven.modules.case.domain.services import case_service as case_service_module
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.domain.services.case_ui_adapter import (
    transform_case_for_ui,
)

pytestmark = pytest.mark.unit


def _turn(n: int, outcome: TurnOutcome) -> TurnProgress:
    return TurnProgress(
        turn_number=n,
        timestamp=datetime.now(timezone.utc),
        progress_made=False,
        outcome=outcome,
        user_message_summary=f"u{n}",
        agent_response_summary=f"a{n}",
    )


def _case(*outcomes: TurnOutcome, messages=None) -> Case:
    """A case whose turn N has ``outcomes[N - 1]``."""
    case = Case(
        case_id="case_aabb11223344",
        title="OOM",
        description="d",
        user_id="u",
        enterprise_id="ent_1",
        state=CaseState.INQUIRY,
        current_turn=len(outcomes),
    )
    case.turn_history = [_turn(i, o) for i, o in enumerate(outcomes, start=1)]
    if messages is not None:
        case.messages = messages
    return case


def _rows(*turn_numbers: int) -> list[dict]:
    """One user row and one assistant row per turn, as stored on the case."""
    rows = []
    for t in turn_numbers:
        rows.append(
            {
                "message_id": f"m-{t}-u",
                "turn_number": t,
                "role": "user",
                "content": f"user text {t}",
                "created_at": datetime.now(timezone.utc),
                "metadata": {},
            }
        )
        rows.append(
            {
                "message_id": f"m-{t}-a",
                "turn_number": t,
                "role": "assistant",
                "content": f"agent text {t}",
                "created_at": datetime.now(timezone.utc),
                "metadata": {},
            }
        )
    return rows


# A case whose turn 2 and turn 5 are asides, and whose turn 4 was consumed
# without a record (``reconcile_turn_sequence`` fills it with SKIPPED — it ran
# the engine, so it IS investigation work).
_MIXED = (
    TurnOutcome.DATA_PROVIDED,
    TurnOutcome.OUT_OF_BAND,
    TurnOutcome.CONVERSATION,
    TurnOutcome.SKIPPED,
    TurnOutcome.OUT_OF_BAND,
)


class TestOrdinal:
    def test_an_aside_does_not_advance_the_ordinal(self):
        case = _case(*_MIXED)
        # Turn 2 is an aside: it carries the ordinal of the turn before it, so
        # a user watching the label sees "Turn 1" stay "Turn 1" (#1329).
        assert [case.investigation_turn_at(t) for t in range(1, 6)] == [
            1,
            1,
            2,
            3,
            3,
        ]

    def test_the_count_is_the_ordinal_at_the_clock(self):
        """The invariant that keeps the live label and the reloaded label equal.

        ``TurnResponse.investigation_turn`` is the count; the newest row's
        ``Message.investigation_turn`` is the ordinal. A client shows one while
        typing and the other after a refresh, so they must not be two
        computations that merely happen to agree.
        """
        for length in range(len(_MIXED) + 1):
            case = _case(*_MIXED[:length])
            assert case.investigation_turn_count == case.investigation_turn_at(
                case.current_turn
            )

    def test_a_turn_with_no_record_still_counts(self):
        """8 of 283 dev cases consumed turns without recording them (#500/#1264).

        They ran the engine, so they are investigation work; only a recorded
        OUT_OF_BAND outcome subtracts.
        """
        case = _case()
        case.current_turn = 20
        assert case.investigation_turn_at(20) == 20
        assert case.investigation_turn_count == 20

    def test_an_unreconciled_history_is_still_read_correctly(self):
        """``turn_history`` order is restored by ``reconcile_turn_sequence`` on
        load and before save, not by the validator — so an in-flight history can
        be out of order, and the lookup bisects a sorted list for that reason."""
        case = _case(*_MIXED)
        case.turn_history = list(reversed(case.turn_history))
        assert case.out_of_band_turns == [2, 5]
        assert [case.investigation_turn_at(t) for t in range(1, 6)] == [1, 1, 2, 3, 3]

    def test_precomputed_asides_give_the_same_answer(self):
        case = _case(*_MIXED)
        asides = case.out_of_band_turns
        for t in range(0, 7):
            assert case.investigation_turn_at(t, asides=asides) == (
                case.investigation_turn_at(t)
            )

    def test_the_ordinal_never_exceeds_the_case_level_count(self):
        """Review of #1389: a row's turn_number is NOT bounded by the clock.

        ``create_case(initial_message=...)`` stamps that row ``turn_number: 1``
        and leaves ``current_turn`` at 0, because no turn has been processed
        yet — so without the clamp the only row reports "Turn 1" while the
        header reports "Turn 0" on the same screen, and the invariant the
        design rests on is false on the very first case a client opens.
        """
        case = _case()
        case.messages = _rows(1)
        assert case.current_turn == 0
        assert case.investigation_turn_at(1) == 0
        assert case.investigation_turn_at(1) <= case.investigation_turn_count

    def test_a_duplicate_aside_record_subtracts_once(self):
        """A duplicate turn number means the CLOCK did not advance for the
        second record (the #1264 corpus: two user messages on one
        ``(case_id, turn_number)``), so counting it twice subtracts a turn that
        was never counted and shifts every later row down by one."""
        case = _case(
            TurnOutcome.DATA_PROVIDED,
            TurnOutcome.OUT_OF_BAND,
            TurnOutcome.CONVERSATION,
        )
        case.turn_history = [
            *case.turn_history,
            _turn(2, TurnOutcome.OUT_OF_BAND),  # same turn recorded twice
        ]
        assert case.out_of_band_turns == [2]
        assert case.investigation_turn_at(3) == 2
        assert case.investigation_turn_count == 2

    def test_the_aside_screen_is_the_shared_predicate(self):
        """``TurnProgress.is_out_of_band`` is the counterpart to ``is_skipped``:
        one screen, so a refinement cannot land in some readers and not others."""
        assert _turn(1, TurnOutcome.OUT_OF_BAND).is_out_of_band is True
        assert _turn(1, TurnOutcome.CONVERSATION).is_out_of_band is False
        assert _turn(1, TurnOutcome.SKIPPED).is_out_of_band is False

    def test_the_ordinal_never_goes_negative(self):
        case = _case(TurnOutcome.OUT_OF_BAND)
        assert case.investigation_turn_at(0) == 0
        assert case.investigation_turn_at(1) == 0


class TestMessageRows:
    """``GET /cases/{id}/messages`` labels every row it returns."""

    def _service(self, case) -> CaseService:
        repo = AsyncMock()
        repo.get = AsyncMock(return_value=case)
        return CaseService(
            case_repository=repo, session_store=AsyncMock(), max_cases_per_user=50
        )

    @pytest.mark.asyncio
    async def test_every_row_carries_its_own_ordinal(self):
        case = _case(*_MIXED, messages=_rows(1, 2, 3, 4, 5))
        response = await self._service(case).get_case_messages_enhanced(
            case_id=case.case_id, limit=100
        )
        labelled = [(m.turn_number, m.investigation_turn) for m in response.messages]
        # Both rows of a turn carry that turn's ordinal; the aside turns (2, 5)
        # repeat the ordinal of the investigation turn before them.
        assert labelled == [
            (1, 1),
            (1, 1),
            (2, 1),
            (2, 1),
            (3, 2),
            (3, 2),
            (4, 3),
            (4, 3),
            (5, 3),
            (5, 3),
        ]

    @pytest.mark.asyncio
    async def test_rows_do_not_all_carry_the_case_level_total(self):
        """The bug #1387 was corrected to avoid.

        Attaching ``Case.investigation_turn_count`` to every row — the obvious
        reading of "put investigation_turn on the message rows" — prints the
        case's current total on all of them. Pinned as a distinct assertion
        because the two designs agree on the NEWEST row, which is the row a
        hand-check looks at.
        """
        case = _case(*_MIXED, messages=_rows(1, 2, 3, 4, 5))
        response = await self._service(case).get_case_messages_enhanced(
            case_id=case.case_id, limit=100
        )
        values = {m.investigation_turn for m in response.messages}
        assert values != {case.investigation_turn_count}
        assert len(values) > 1

    @pytest.mark.asyncio
    async def test_a_page_does_not_depend_on_which_rows_it_contains(self):
        """The acceptance: a client can label a row without reading any other.

        The same row fetched in a later page must carry the same ordinal, or a
        paginating client renumbers its history as it scrolls.
        """
        case = _case(*_MIXED, messages=_rows(1, 2, 3, 4, 5))
        service = self._service(case)
        whole = await service.get_case_messages_enhanced(
            case_id=case.case_id, limit=100
        )
        tail = await service.get_case_messages_enhanced(
            case_id=case.case_id, limit=100, offset=6
        )
        by_id = {m.message_id: m.investigation_turn for m in whole.messages}
        assert tail.messages, "expected a non-empty tail page"
        for row in tail.messages:
            assert row.investigation_turn == by_id[row.message_id]

    @pytest.mark.asyncio
    async def test_a_notice_row_carries_no_investigation_turn(self):
        """A ``system`` row is a background-job notice stamped with whichever
        turn happened to be OPEN when the job finished, so it owns no turn and
        a number on it would assert membership in an exchange it had no part
        in. Both clients suppress that in their own code today; the null is
        what lets them stop."""
        rows = _rows(1, 2)
        rows.append(
            {
                "message_id": "m-notice",
                "turn_number": 2,
                "role": "system",
                "content": "Your runbook draft is ready.",
                "created_at": datetime.now(timezone.utc),
                "metadata": {"source": "runbook_conversion_complete"},
            }
        )
        case = _case(*_MIXED[:2], messages=rows)
        response = await self._service(case).get_case_messages_enhanced(
            case_id=case.case_id, limit=100
        )
        notice = next(m for m in response.messages if m.message_id == "m-notice")
        assert notice.investigation_turn is None
        # …while it keeps the message clock, which is what places it.
        assert notice.turn_number == 2
        assert all(
            m.investigation_turn is not None
            for m in response.messages
            if m.role != "system"
        )

    @pytest.mark.asyncio
    async def test_include_debug_returns_a_response_instead_of_raising(self):
        """Review of #1389: the debug envelope was built from fields the model
        does not declare and without the one it requires, so `include_debug=true`
        raised a ValidationError — and the handler rebuilt the same object and
        raised again from inside its own ``except``, turning a documented query
        parameter into a 500."""
        case = _case(*_MIXED, messages=_rows(1, 2, 3, 4, 5))
        response = await self._service(case).get_case_messages_enhanced(
            case_id=case.case_id, limit=100, include_debug=True
        )
        assert response.debug_info is not None
        assert response.debug_info.redis_operation_time_ms >= 0
        assert response.retrieved_count == 10

    @pytest.mark.asyncio
    async def test_only_the_page_is_converted(self):
        """Counting used to mean converting every message in the case — 2000
        pydantic models built to keep 50 — on every page a client scrolled."""
        case = _case(*_MIXED, messages=_rows(1, 2, 3, 4, 5))
        service = self._service(case)
        with mock.patch(
            "faultmaven.modules.case.domain.services.case_service._case_messages_from",
            wraps=case_service_module._case_messages_from,
        ) as convert:
            response = await service.get_case_messages_enhanced(
                case_id=case.case_id, limit=4
            )
        assert response.total_count == 10
        assert response.retrieved_count == 4
        # One call, and it was handed exactly the page.
        assert convert.call_count == 1
        assert len(convert.call_args.args[1]) == 4

    @pytest.mark.asyncio
    async def test_the_newest_row_agrees_with_the_case_level_count(self):
        """What a client sees after a reload equals what it saw while typing."""
        case = _case(*_MIXED, messages=_rows(1, 2, 3, 4, 5))
        response = await self._service(case).get_case_messages_enhanced(
            case_id=case.case_id, limit=100
        )
        assert response.messages[-1].investigation_turn == (
            case.investigation_turn_count
        )


class TestContract:
    def test_the_field_is_nullable_so_an_older_server_reads_as_absent(self):
        assert Message.model_fields["investigation_turn"].default is None
        row = Message(
            message_id="m-1",
            turn_number=3,
            role="user",
            content="hi",
            created_at="2026-09-13T00:00:00Z",
        )
        assert row.investigation_turn is None

    @pytest.mark.parametrize(
        "model",
        [CaseUIResponse_Inquiry, CaseUIResponse_Investigating, CaseUIResponse_Resolved],
    )
    def test_every_case_ui_phase_declares_it_nullable(self, model):
        assert model.model_fields["investigation_turn"].default is None


class TestCaseSchemas:
    """`GET /cases` and `GET /cases/{id}` publish the same count.

    Review of #1389: the first pass moved the three `CaseUIResponse_*` schemas
    and left these two, so the Dashboard's case export still counted the haiku
    (`exportMarkdown.ts` prints `**Turns:** ${caseDetail.current_turn}`). The
    argument for going past the issue text — shipping only the rows leaves the
    bug one line higher — applies to them too.
    """

    def _case(self) -> Case:
        case = _case(*_MIXED)
        case.inquiry.proposed_problem_statement = "DNS resolution failing on prod"
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.decided_to_investigate = True
        case.state = CaseState.INVESTIGATING
        return case

    def test_case_summary_reports_both_counters(self):
        summary = CaseSummary.from_case(self._case())
        assert summary.current_turn == 5
        assert summary.investigation_turn == 3

    def test_case_detail_reports_both_counters(self):
        detail = CaseDetail.from_case(self._case())
        assert detail.current_turn == 5
        assert detail.investigation_turn == 3

    def test_the_operator_list_shows_the_same_turn_as_everyone_else(self):
        """`AdminCaseMetadata.from_summary` names its fields deliberately so a
        new one is classified by a human (ADR-012 D9). This one is metadata: a
        count, carrying no text a user typed."""
        metadata = AdminCaseMetadata.from_summary(CaseSummary.from_case(self._case()))
        assert metadata.current_turn == 5
        assert metadata.investigation_turn == 3

    @pytest.mark.parametrize("model", [CaseSummary, CaseDetail, AdminCaseMetadata])
    def test_the_field_is_nullable_on_every_case_schema(self, model):
        assert model.model_fields["investigation_turn"].default is None


class TestCaseRead:
    """``GET /cases/{id}/ui`` reports the case-level count beside the clock.

    The per-row ordinal alone leaves the header and the resolution summary
    ("12 turns") reading ``current_turn``, which is the message clock — so an
    aside still moves the number the user is looking at, which is the symptom
    #1329 set out to remove.
    """

    def _investigating(self, *outcomes: TurnOutcome) -> Case:
        case = _case(*outcomes)
        case.inquiry.proposed_problem_statement = "DNS resolution failing on prod"
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.decided_to_investigate = True
        case.state = CaseState.INVESTIGATING
        return case

    def _terminal(self, state: CaseState, *outcomes: TurnOutcome) -> Case:
        """A terminal case the MODEL would actually emit.

        ``Case`` sets ``validate_assignment=True`` and its terminal validators
        are bidirectional — a terminal state requires ``closed_at`` and
        ``closed_at`` requires a terminal state — so no ORDER of assignments
        satisfies both, which is why the fixtures elsewhere in the suite reach
        for ``object.__setattr__``. Passing the pair to the CONSTRUCTOR runs
        the validators once with both present and needs no bypass, so the
        adapter under test is exercised against a shape the model admits
        rather than one only a test can build.
        """
        closed_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        case = Case(
            case_id="case_aabb11223344",
            title="OOM",
            description="Production DNS failing",
            user_id="u",
            enterprise_id="ent_1",
            state=state,
            closed_at=closed_at,
            resolved_at=closed_at if state is CaseState.RESOLVED else None,
            closure_reason=(
                None if state is CaseState.RESOLVED else "closed_insufficient_evidence"
            ),
            current_turn=len(outcomes),
        )
        case.inquiry.proposed_problem_statement = "DNS resolution failing on prod"
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.decided_to_investigate = True
        case.turn_history = [_turn(i, o) for i, o in enumerate(outcomes, start=1)]
        return case

    def test_inquiry_reports_the_count(self):
        case = _case(*_MIXED)
        ui = transform_case_for_ui(case)
        assert ui.current_turn == 5
        assert ui.investigation_turn == 3

    def test_investigating_reports_the_count(self):
        ui = transform_case_for_ui(self._investigating(*_MIXED))
        assert ui.current_turn == 5
        assert ui.investigation_turn == 3

    @pytest.mark.parametrize("state", [CaseState.RESOLVED, CaseState.CLOSED])
    def test_terminal_reports_the_count(self, state):
        ui = transform_case_for_ui(self._terminal(state, *_MIXED))
        assert ui.current_turn == 5
        assert ui.investigation_turn == 3

    def test_an_aside_leaves_the_case_level_number_alone(self):
        """The #1329 acceptance, read off the case: "Turn 3" stays "Turn 3"."""
        before = transform_case_for_ui(self._investigating(*_MIXED[:4]))
        after = transform_case_for_ui(
            self._investigating(*_MIXED[:4], TurnOutcome.OUT_OF_BAND)
        )
        assert after.current_turn == before.current_turn + 1
        assert after.investigation_turn == before.investigation_turn
