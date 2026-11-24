"""Every ``Case`` field survives a save and a load (#2945): the guard both case
repositories run, SQLite in ``test_case_field_roundtrip.py`` and PostgreSQL in
``tests/integration/test_postgresql_repository_roundtrip.py``.

Each repository lists the fields it persists by hand, in its writer
(``_case_record_params``, and the UPDATE's own SET list) and its reader
(``_row_to_case``). That enumeration lost ``last_suggestions`` (#914),
PostgreSQL's ``message_count`` and the investigation journal (#2945), each
silently: the field kept its value for the rest of the request and came back
empty on the next one. So the guard walks ``Case.model_fields``, not a list of
its own: a new field fails it until it is persisted or exempted here.

The path is production's: a case is created with only its identity (the
INSERT), loaded, given the fixture's values and saved again (the versioned
UPDATE every turn takes), then loaded on a fresh session with ``repo.get()``.
Each field is compared with a deep copy of the fixture taken before the save,
so a save that clears a field in memory cannot hide its loss.

Scope: every top-level ``Case`` field, compared whole, so every member of the
cases row's JSON columns (the metadata blob's entries, and ``inquiry`` /
``progress`` / ... compared as whole values) is compared too. A child-table
collection (files, evidence, needs, the causal graph, hypotheses, solutions,
messages, the action history) is compared whole for the one or two rows a
fixture gives it, but the guard does not set every column of a child row: the
child tables have their own hand-enumerated writers, pinned by their own tests.

Exempt fields (``EXEMPT``) are not skipped: a save-stamped one must load as
what the save stamped, a normalized one as its normalized value.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

from pydantic import TypeAdapter

from faultmaven.modules.case.contracts import (
    ActionAttempt,
    Case,
    CaseAction,
    CaseState,
    CausalEdge,
    CausalNode,
    ChatDataOffer,
    ClosureReason,
    ConfidenceLevel,
    DocumentationData,
    EscalationState,
    EscalationType,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    InquiryData,
    InvestigationActionType,
    InvestigationProgress,
    InvestigationStrategy,
    JournalEntry,
    NeedPurpose,
    NodeType,
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

#: The fields a save itself writes (``CaseRepository.SAVE_STAMPED_FIELDS``).
#: Each must load as the value the saved case was stamped with.
SAVE_STAMPED = "save-stamped"
#: Stored as a normalized value. Each must load as that value.
NORMALIZED = "normalized"

#: Field -> (class, reason). The reason names the code that writes it.
EXEMPT: Dict[str, Tuple[str, str]] = {
    "version": (SAVE_STAMPED, "bumped by the versioned UPDATE"),
    "updated_at": (SAVE_STAMPED, "set to now by save()"),
    "last_activity_at": (SAVE_STAMPED, "set to now by _upsert_case_record"),
    "disposition_eligibility": (SAVE_STAMPED, "derived by prepare_for_save"),
    "offer_record": (SAVE_STAMPED, "lapsed by prepare_for_save"),
    "current_turn": (
        NORMALIZED,
        "stored as Case.effective_current_turn (_case_record_params)",
    ),
}

#: Child rows whose ``updated_at`` column the save stamps with now (the needs
#: and causal-node upserts): compared without that one column.
_ROW_STAMPED = ("evidence_needs", "causal_nodes")

#: What a normalized field must load as, from the fixture before its save.
_NORMALIZED: Dict[str, Callable[[Case], Any]] = {
    "current_turn": lambda before: before.effective_current_turn,
}

_ANY: TypeAdapter[Any] = TypeAdapter(Any)
_T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _investigating(
    enterprise_id: str, organization_id: str, user_id: str, driver_id: str
) -> Case:
    """Every field set to a non-default value, a row in each child table."""
    case_id, file_id = f"case_{uuid4().hex[:12]}", f"file_{uuid4().hex[:12]}"
    problem = CausalNode(
        statement="Checkout times out", node_type=NodeType.PROBLEM, generated_at_turn=0
    )
    root = CausalNode(
        statement="pool too small", node_type=NodeType.ROOT, generated_at_turn=2
    )
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
        case_id=case_id,
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
                file_id=file_id,
                filename="app.log",
                size_bytes=2048,
                uploaded_at_turn=1,
                uploaded_at=_T0,
                uploaded_by=user_id,
            )
        ],
        evidence=[
            Evidence(
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                primary_purpose="symptom_verified",
                summary="p99 above 5s",
                source_type=EvidenceSourceType.LOGS,
                source_file_id=file_id,
                collected_by=user_id,
                collected_at=_T0,
                collected_at_turn=1,
            )
        ],
        evidence_needs=[
            EvidenceNeed(
                case_id=case_id,
                purpose=NeedPurpose.CAUSAL_VERIFICATION,
                request_text="the pool settings",
                rationale="is the pool capped",
                created_at_turn=2,
                created_at=_T0,
                updated_at=_T0,
            )
        ],
        causal_nodes={problem.node_id: problem, root.node_id: root},
        causal_edges=[
            CausalEdge(cause_node_id=root.node_id, effect_node_id=problem.node_id)
        ],
        messages=[
            {
                "message_id": f"msg_{uuid4().hex[:12]}",
                "turn_number": 1,
                "role": "user",
                "content": "checkout is slow",
                "created_at": _T0.isoformat(),
                "metadata": {},
                "token_count": None,
                "author_id": user_id,
            }
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
                # INV-49 (#3039): the report a refused gate still records.
                reported_done_in_turn=3,
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
        # No turn history, so ``current_turn`` is read from its own column: a
        # load that lost it cannot be re-derived from the history.
        Case(
            state=CaseState.CLOSED,
            closure_reason=ClosureReason.SOLUTION_DEFERRED,
            current_turn=4,
            **terminal,
        ),
    ]


def assert_fixtures_cover_every_field(fixtures: List[Case]) -> None:
    """Every field but a save-stamped one holds a non-default value in some
    fixture, so a new field fails here until a fixture sets it or it is
    exempted. A save-stamped field is set by the save itself."""
    stale = set(EXEMPT) - set(Case.model_fields)
    assert not stale, f"EXEMPT names fields Case no longer has: {stale}"
    unset = [
        name
        for name, info in Case.model_fields.items()
        if not info.is_required()
        and EXEMPT.get(name, (None,))[0] != SAVE_STAMPED
        and all(
            _jsonable(getattr(f, name))
            == _jsonable(info.get_default(call_default_factory=True))
            for f in fixtures
        )
    ]
    assert not unset, f"no round-trip fixture sets {unset}: set it or exempt it"


def _created(fixture: Case) -> Case:
    """The case as ``create_case`` inserts it: its identity, nothing else."""
    identity = ("case_id", "user_id", "driver_id", "enterprise_id")
    return Case(
        **{name: getattr(fixture, name) for name in identity},
        organization_id=fixture.organization_id,
        source=fixture.source,
        title=fixture.title,
        created_at=fixture.created_at,
    )


async def assert_every_field_round_trips(
    save: Callable[[Case], Awaitable[Case]],
    get_fresh: Callable[[str], Awaitable[Optional[Case]]],
    fixtures: List[Case],
) -> None:
    """Insert each fixture's identity, then write its values by the versioned
    UPDATE, load it on a fresh session and compare field by field."""
    assert_fixtures_cover_every_field(fixtures)
    for fixture in fixtures:
        created = await get_fresh((await save(_created(fixture))).case_id)
        assert created is not None
        before = fixture.model_copy(deep=True)
        values = fixture.model_copy(deep=True)
        case = created.model_copy(
            update={n: getattr(values, n) for n in Case.model_fields if n != "version"}
        )
        saved = await save(case)
        assert saved.version == 2, "the second save is the versioned UPDATE"
        loaded = await get_fresh(saved.case_id)
        assert loaded is not None
        lost = []
        for name in Case.model_fields:
            kind = EXEMPT.get(name, (None,))[0]
            if kind == SAVE_STAMPED:
                expected = getattr(saved, name)
            elif kind == NORMALIZED:
                expected = _NORMALIZED[name](before)
            else:
                expected = getattr(before, name)
            if _rows(name, expected) != _rows(name, getattr(loaded, name)):
                lost.append(name)
        assert (
            not lost
        ), f"{before.state.value} case: {lost} did not survive save and get"


def _jsonable(value: Any) -> Any:
    return _ANY.dump_python(value, mode="json")


def _rows(name: str, value: Any) -> Any:
    dumped = _jsonable(value)
    if name not in _ROW_STAMPED:
        return dumped
    rows = dumped.values() if isinstance(dumped, dict) else dumped
    return sorted(
        (json.dumps({k: v for k, v in r.items() if k != "updated_at"}, sort_keys=True))
        for r in rows
    )
