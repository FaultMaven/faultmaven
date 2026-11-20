"""Every ``Case`` field survives a save and a load (#2945): the guard both case
repositories run, SQLite in ``test_case_field_roundtrip.py`` and PostgreSQL in
``tests/integration/test_postgresql_repository_roundtrip.py``.

Each repository lists the fields it persists by hand, in its writer
(``_case_record_params``) and its reader (``_row_to_case``). That enumeration
lost ``last_suggestions`` (#914), PostgreSQL's ``message_count`` and the
investigation journal (#2945), each silently: the field kept its value for the
rest of the request and came back empty on the next one. So the guard walks
``Case.model_fields``, not a list of its own: a new field fails it until it is
persisted or exempted here.

Scope: every top-level ``Case`` field, compared whole after ``repo.get()`` on a
fresh session, so every member of the cases row's JSON columns (the metadata
blob's entries, and ``inquiry`` / ``progress`` / ... compared as whole values)
is compared too. A child-table collection (hypotheses, solutions, uploaded
files, the action history) is compared whole for the one row a fixture gives
it, but the guard does not set every column of a child row: the child tables
have their own hand-enumerated writers, pinned by their own tests.

Exempt fields (``EXEMPT``) are not skipped unless their class says so: a
save-stamped one must load as what the save stamped, a normalized one as its
normalized value, and one loaded outside ``_row_to_case`` is its own table's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from pydantic import TypeAdapter

from faultmaven.modules.case.contracts import (
    ActionAttempt,
    Case,
    CaseAction,
    CaseState,
    ChatDataOffer,
    ClosureReason,
    ConfidenceLevel,
    DocumentationData,
    EscalationState,
    EscalationType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    InquiryData,
    InvestigationActionType,
    InvestigationProgress,
    InvestigationStrategy,
    JournalEntry,
    OfferRecord,
    ProblemStatus,
    ProblemVerification,
    ProposedAction,
    ResolveRequest,
    RootCauseConclusion,
    Solution,
    SolutionType,
    TurnOutcome,
    TurnProgress,
    UploadedFile,
    WorkingConclusion,
)

#: The fields a save itself writes. Each is asserted against what the save
#: stamped, never skipped.
SAVE_STAMPED = "save-stamped"
#: Stored as a normalized value. Each is asserted against that value.
NORMALIZED = "insert-only or normalized"
#: A child table read by its own loader, not by ``_row_to_case``'s row: out of
#: the guard's scope (see the module docstring).
LOADED_OUTSIDE = "loaded outside _row_to_case"

#: Field -> (class, reason). The reason names the code that writes or loads it.
EXEMPT: Dict[str, Tuple[str, str]] = {
    "version": (SAVE_STAMPED, "bumped by the versioned UPDATE (SAVE_STAMPED_FIELDS)"),
    "updated_at": (SAVE_STAMPED, "set to now by save() (SAVE_STAMPED_FIELDS)"),
    "disposition_eligibility": (
        SAVE_STAMPED,
        "derived by CaseRepository.prepare_for_save (SAVE_STAMPED_FIELDS)",
    ),
    "offer_record": (
        SAVE_STAMPED,
        "lapsed by CaseRepository.prepare_for_save (SAVE_STAMPED_FIELDS)",
    ),
    "last_activity_at": (
        SAVE_STAMPED,
        "the save's own now(), bound by _upsert_case_record (saving.py)",
    ),
    "driver_id": (
        NORMALIZED,
        "written by the INSERT only; its one later writer is case_driver_sql",
    ),
    "current_turn": (
        NORMALIZED,
        "stored as Case.effective_current_turn (_case_record_params)",
    ),
    "message_count": (
        NORMALIZED,
        "PostgreSQL's metadata filter drops 0; the reader defaults it to 0",
    ),
    "evidence": (LOADED_OUTSIDE, "_load_evidence_for_case"),
    "evidence_needs": (LOADED_OUTSIDE, "_load_evidence_needs_for_case"),
    "causal_nodes": (LOADED_OUTSIDE, "_load_causal_graph_for_case"),
    "causal_edges": (LOADED_OUTSIDE, "_load_causal_graph_for_case"),
    "messages": (
        LOADED_OUTSIDE,
        "the case_messages rows (_upsert_messages, _load_messages)",
    ),
}

#: What each exempt field must load as, from the case after its save returned.
_EXPECTED: Dict[str, Callable[[Case, Case], Any]] = {
    "version": lambda saved, fixture: saved.version,
    "updated_at": lambda saved, fixture: saved.updated_at,
    "disposition_eligibility": lambda saved, fixture: saved.disposition_eligibility,
    "offer_record": lambda saved, fixture: saved.offer_record,
    "driver_id": lambda saved, fixture: fixture.driver_id,
    "current_turn": lambda saved, fixture: saved.effective_current_turn,
    "message_count": lambda saved, fixture: saved.message_count,
}

_ANY: TypeAdapter[Any] = TypeAdapter(Any)
_T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _investigating(
    enterprise_id: str, organization_id: str, user_id: str, driver_id: str
) -> Case:
    """Every field the cases row carries, set to a non-default value."""
    turns = [
        TurnProgress(
            turn_number=n,
            timestamp=_T0 + timedelta(minutes=n),
            progress_made=True,
            outcome=TurnOutcome.DATA_PROVIDED,
            milestones_completed=["symptom_verified"],
            user_message_summary=f"turn {n}",
        )
        for n in (1, 2, 3)
    ]
    return Case(
        user_id=user_id,
        driver_id=driver_id,
        enterprise_id=enterprise_id,
        organization_id=organization_id,
        source="slack",
        title="Round-trip guard case",
        description="Checkout requests time out under load",
        state=CaseState.INVESTIGATING,
        action_history=[
            CaseAction(
                from_state=CaseState.INQUIRY,
                to_state=CaseState.INVESTIGATING,
                triggered_at=_T0,
                triggered_by=user_id,
                reason="problem statement confirmed",
            )
        ],
        offer_record=OfferRecord(
            resolve_request=ResolveRequest(missing=("verification",), at_turn=2)
        ),
        last_suggestions=[{"label": "Upload the pool log", "intent": "provide_data"}],
        chat_data_offers=[ChatDataOffer(turn=2, blocks=["ERROR pool exhausted"])],
        kb_context=[{"document_id": "doc_1", "title": "Pool exhaustion"}],
        kb_context_origin={"driver_id": driver_id, "query": "pool", "trigger": "x"},
        progress=InvestigationProgress(
            problem_status=ProblemStatus.VERIFIED,
            root_cause_likelihood=0.6,
        ),
        current_turn=3,
        turns_without_progress=2,
        turn_history=turns,
        message_count=6,
        investigation_strategy=InvestigationStrategy.ACTIVE_INCIDENT,
        inquiry=InquiryData(
            proposed_problem_statement="Checkout requests time out under load",
            problem_statement_confirmed=True,
            problem_statement_confirmed_at=_T0,
            inquiry_turns=1,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="p99 latency above 5s", severity="high"
        ),
        uploaded_files=[
            UploadedFile(
                filename="app.log",
                size_bytes=2048,
                uploaded_at_turn=1,
                uploaded_at=_T0,
                uploaded_by=user_id,
            )
        ],
        hypotheses={
            h.hypothesis_id: h
            for h in [
                Hypothesis(
                    statement="The connection pool is too small",
                    category=HypothesisCategory.CONFIG,
                    generated_at_turn=2,
                    generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
                    rationale="pool exhaustion errors in the log",
                )
            ]
        },
        solutions=[
            Solution(
                solution_type=SolutionType.CONFIG_CHANGE,
                title="Raise the pool size",
                immediate_action="set pool_size=50",
                proposed_at=_T0,
            )
        ],
        proposed_actions=[
            ProposedAction(
                case_id="case_000000000000",
                action_type=InvestigationActionType.DIAGNOSTIC,
                description="Show the pool settings",
                proposed_at=_T0,
                proposed_in_turn=2,
            )
        ],
        action_attempts=[
            ActionAttempt(action_id="act_1", user_message="ran it", submitted_at=_T0)
        ],
        working_conclusion=WorkingConclusion(
            statement="Pool exhaustion",
            likelihood=0.6,
            reasoning="the log",
            updated_at=_T0,
        ),
        root_cause_conclusion=RootCauseConclusion(
            root_cause="pool too small",
            confidence_level=ConfidenceLevel.PROBABLE,
            likelihood=0.6,
            mechanism="requests wait for a connection",
            determined_at=_T0,
        ),
        investigation_journal=[
            JournalEntry(turn=2, entry_type="blocker", content="no DB access"),
            JournalEntry(turn=3, entry_type="finding", content="pool at 10/10"),
        ],
        escalation_state=EscalationState(
            escalation_type=EscalationType.NO_PROGRESS,
            reason="stalled",
            context_summary="no access",
            escalated_at=_T0,
        ),
        documentation=DocumentationData(lessons_learned=["size the pool"]),
        created_at=_T0,
    )


def roundtrip_fixtures(
    enterprise_id: str, organization_id: str, user_id: str, driver_id: str
) -> List[Case]:
    """INVESTIGATING, RESOLVED and CLOSED: ``validate_state_timestamp_consistency``
    allows ``resolved_at`` only when RESOLVED and ``closure_reason`` only when
    CLOSED, so no one case can set every field."""
    base = _investigating(enterprise_id, organization_id, user_id, driver_id)
    terminal = dict(
        user_id=user_id,
        enterprise_id=enterprise_id,
        title="Round-trip guard terminal case",
        description="Checkout requests time out under load",
        created_at=_T0,
        closed_at=_T0 + timedelta(hours=2),
    )
    return [
        base,
        Case(
            state=CaseState.RESOLVED, resolved_at=_T0 + timedelta(hours=1), **terminal
        ),
        Case(
            state=CaseState.CLOSED,
            closure_reason=ClosureReason.SOLUTION_DEFERRED,
            **terminal,
        ),
    ]


def assert_fixtures_cover_every_field(fixtures: List[Case]) -> None:
    """Every field the guard compares whole, and every normalized one, holds a
    non-default value in some fixture, so a new field fails here until a fixture
    sets it or it is exempted. A save-stamped field is set by the save itself."""
    stale = set(EXEMPT) - set(Case.model_fields)
    assert not stale, f"EXEMPT names fields Case no longer has: {stale}"
    unset = [
        name
        for name, info in Case.model_fields.items()
        if not info.is_required()
        and EXEMPT.get(name, (None,))[0] not in (SAVE_STAMPED, LOADED_OUTSIDE)
        and all(
            _jsonable(getattr(f, name))
            == _jsonable(info.get_default(call_default_factory=True))
            for f in fixtures
        )
    ]
    assert not unset, f"no round-trip fixture sets {unset}: set it or exempt it"


async def assert_every_field_round_trips(
    save: Callable[[Case], Awaitable[Case]],
    get_fresh: Callable[[str], Awaitable[Optional[Case]]],
    fixtures: List[Case],
) -> None:
    """Save each fixture, load it on a fresh session, compare field by field."""
    assert_fixtures_cover_every_field(fixtures)
    for fixture in fixtures:
        before = datetime.now(UTC)
        saved = await save(fixture.model_copy(deep=True))
        after = datetime.now(UTC)
        loaded = await get_fresh(saved.case_id)
        assert loaded is not None
        lost = []
        for name in Case.model_fields:
            kind = EXEMPT.get(name, (None,))[0]
            if kind == LOADED_OUTSIDE:
                continue
            if name == "last_activity_at":
                if not before <= loaded.last_activity_at <= after:
                    lost.append(name)
                continue
            if kind is not None:
                expected = _EXPECTED[name](saved, fixture)
                got = getattr(loaded, name)
            else:
                expected, got = getattr(saved, name), getattr(loaded, name)
            if _jsonable(expected) != _jsonable(got):
                lost.append(name)
        assert (
            not lost
        ), f"{saved.state.value} case: {lost} did not survive save and get"


def _jsonable(value: Any) -> Any:
    return _ANY.dump_python(value, mode="json")
