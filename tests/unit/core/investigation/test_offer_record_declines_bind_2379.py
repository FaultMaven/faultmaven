"""#2379: a decline binds until a new solution or a new confirmation, whatever
else about the case changes (ruling 3, 2026-10-30).

A generated-state test over the public surface only: real ``process_turn``
turns with a stubbed generator (no LLM), ``derive_disposition_eligibility``,
the reply's follow-up labels and the feedback of a refused proposal. A decline
is recorded the way a user records one, by clicking "Not yet" on the offer the
reply carried; then each change outside the offer's premise is applied, by a
stubbed turn where a turn can produce it and written to the case between turns
where none can (a contested cause, a withdrawn solution, a driver change, a
status written directly, a disqualified row, a refuted hypothesis). After the
change the decline still binds: Close stays in the menu, the engine re-offers
nothing, and a proposal the model makes is refused.

``cause_identification_contested`` is re-derived from the causal graph at every
turn, so it holds only until the next turn; it is read at the state it makes.

Runs three declines: the engine's resolve offer, its deferred close, and its
false-alarm close. The resolve decline runs every single change and the pairs
the plan names; the two closes run the singles that apply to their case.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.transitions import (
    DECLINED_RESOLVE_FEEDBACK,
)
from faultmaven.core.investigation.schemas import (
    EvidenceToAdd,
    InvestigationResponse_Diagnosis,
    MilestoneUpdates,
    ProblemVerificationUpdate,
    ProposedTransition,
    SolutionToAdd,
)
from faultmaven.core.investigation.terminal_transitions import (
    derive_disposition_eligibility,
)
from faultmaven.modules.case.contracts import CaseState, ProblemStatus, UploadedFile
from faultmaven.modules.case.domain.models.causal import (
    CausalNode,
    NodeEvidenceLink,
    NodeType,
)
from faultmaven.modules.case.domain.models.conclusion import (
    ConfidenceLevel,
    RootCauseConclusion,
)
from faultmaven.modules.case.domain.models.evidence import (
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
)
from faultmaven.modules.case.domain.models.hypothesis import (
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
)
from faultmaven.modules.case.domain.models.solution import Solution, SolutionType
from tests.case_builders import investigating_case

pytestmark = pytest.mark.unit

_FILE_ID = "file_0123456789ab"
_DSU = InvestigationResponse_Diagnosis.DiagnosisStateUpdate

RESOLVE_PAIR = ["Yes, mark as resolved", "Not yet, continue investigating"]
#: The "Mark it resolved" chip, as the reply labels it.
REOPEN_CHIP = "Mark it resolved"
CLOSE_PAIR = ["Yes, close this case", "Not yet, continue investigating"]
_REVISED = (
    "AssumeRoleWithWebIdentity fails for the nightly batch pods only; the API "
    "pods assume the role normally"
)
_DEFERRED_FEEDBACK = "the user declined closing this case with the solution documented"
_FALSE_ALARM_FEEDBACK = "the user declined closing on this finding"


# ---------------------------------------------------------------------------
# Rows and turns
# ---------------------------------------------------------------------------


def _row(category: EvidenceCategory, turn: int, summary: str, **kw) -> Evidence:
    return Evidence(
        category=category,
        primary_purpose="observe",
        summary=summary,
        source_type=kw.pop("source_type", EvidenceSourceType.LOGS),
        source_file_id=kw.pop("source_file_id", _FILE_ID),
        collected_by=kw.pop("collected_by", "user"),
        collected_at_turn=turn,
        **kw,
    )


def _to_add(category: str, summary: str) -> dict:
    return {
        "summary": summary,
        "extract": summary,
        "category": category,
        "source_type": "logs",
        "source_file_id": _FILE_ID,
    }


def _engine() -> MilestoneEngine:
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine.kb_prefetcher.prefetch_kb_context = AsyncMock(return_value=None)
    return engine


async def _turn(engine, case, message: str, dsu=None, **kw) -> dict:
    """One real turn: the model answers ``dsu`` (nothing, when None)."""
    case.current_turn += 1
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates=dsu or _DSU()
        )
    )
    return await engine.process_turn(
        case=case, user_message=message, **kw, redaction_ctx=None
    )


def _labels(result) -> list:
    return [f["label"] for f in result["suggested_follow_ups"]]


def _feedback(result) -> str:
    return result["case_updated"].turn_history[-1].system_feedback or ""


async def _not_yet(engine, case, offered: dict) -> dict:
    """Click "Not yet" on the offer ``offered`` carried."""
    card = next(
        f
        for f in offered["suggested_follow_ups"]
        if (f.get("intent") or {}).get("confirmation_value") is False
    )
    return await _turn(
        engine,
        case,
        card["payload"],
        intent_type="confirmation",
        intent_data={"value": False, "proposal_id": card["intent"]["proposal_id"]},
    )


def _proposes(to_state: str, **fields) -> _DSU:
    return _DSU(proposed_transition=ProposedTransition(to_state=to_state), **fields)


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


def _base_case(*, problem_status=ProblemStatus.VERIFIED):
    case = investigating_case(
        title="Cross-account AssumeRole failures",
        enterprise_id="org_test",
        user_id="user_test",
        description="data-processor pods cannot assume the cross-account role",
        symptom="AssumeRoleWithWebIdentity fails for the pods",
        confirm_after_build=True,
        problem_status=problem_status,
    )
    case.uploaded_files.append(
        UploadedFile(
            file_id=_FILE_ID, filename="sts.log", size_bytes=512, uploaded_at_turn=0
        )
    )
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="The IAM OIDC provider is registered with the wrong client ID.",
        mechanism="AssumeRoleWithWebIdentity rejects the token's audience.",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.85,
    )
    case.solutions = [
        Solution(
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Correct the OIDC provider client ID",
            longterm_fix="Set the provider ClientIDList to sts.amazonaws.com.",
        )
    ]
    # The symptom as reported, before the fix: what a revision cites.
    case.evidence.append(
        _row(
            EvidenceCategory.SYMPTOM_EVIDENCE,
            0,
            "The batch pods' AssumeRole calls fail; the API pods' succeed.",
        )
    )
    return case


def _resolvable_case():
    """Gone => gone twice over: two cause re-checks and the problem gone after
    them, so one row can be disqualified and the case still confirms."""
    case = _base_case()
    case.evidence += [
        _row(
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            1,
            "The provider's ClientIDList now reads sts.amazonaws.com.",
        ),
        _row(
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            1,
            "The projected token's audience matches the provider's client ID.",
        ),
        _row(
            EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
            1,
            "AssumeRoleWithWebIdentity succeeds for the data-processor pods.",
        ),
    ]
    return case


async def _declined_resolve():
    """The backstop offers on turn 1; "Not yet" is clicked on turn 2."""
    case, engine = _resolvable_case(), _engine()
    offered = await _turn(engine, case, "the errors are gone now")
    assert _labels(offered) == RESOLVE_PAIR, "premise: the backstop offered"
    await _not_yet(engine, case, offered)
    assert derive_disposition_eligibility(case)["closed"] == "ready", "premise"
    return engine, case


async def _declined_deferred():
    """The deferred proposer offers its close on turn 1; "Not yet" on turn 2."""
    case, engine = _base_case(), _engine()
    offered = await _turn(
        engine,
        case,
        "the fix needs a change window next week",
        _DSU(milestones=MilestoneUpdates(solution_feasible="deferred")),
    )
    assert _labels(offered) == CLOSE_PAIR, "premise: the deferred close"
    await _not_yet(engine, case, offered)
    return engine, case


async def _declined_false_alarm():
    """The finding on turn 1, the engine's close with it; "Not yet" on turn 2."""
    case, engine = _base_case(problem_status=ProblemStatus.UNVERIFIED), _engine()
    case.root_cause_conclusion = None
    case.solutions = []
    offered = await _turn(
        engine,
        case,
        "the gateway log shows no errors at 14:00",
        _DSU(
            evidence_to_add=[
                EvidenceToAdd(
                    summary="No 5xx in the gateway log for the reported window.",
                    extract="0 errors 13:55-14:05",
                    category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
                    source_type=EvidenceSourceType.LOGS,
                    source_file_id=_FILE_ID,
                )
            ],
            verification_updates=ProblemVerificationUpdate(
                problem_invalidated=True,
                invalidation_evidence_ids=["new_index_0"],
                invalidation_basis="no 5xx in the gateway log for the window",
            ),
        ),
    )
    assert case.progress.problem_status == ProblemStatus.INVALIDATED, "premise"
    await _not_yet(engine, case, offered)
    return engine, case


# ---------------------------------------------------------------------------
# The changes outside the premise
# ---------------------------------------------------------------------------


async def _revision(engine, case):
    symptom = next(
        e for e in case.evidence if e.category == EvidenceCategory.SYMPTOM_EVIDENCE
    )
    await _turn(
        engine,
        case,
        "it is only the batch pods, actually",
        _DSU(
            verification_updates=ProblemVerificationUpdate(
                revised_problem_statement=_REVISED,
                revision_evidence_ids=[symptom.evidence_id],
                revision_basis="the failures are confined to the batch pods",
            )
        ),
    )
    assert case.progress.problem_status == ProblemStatus.REVISION_PENDING, "premise"


async def _contested(engine, case):
    case.progress.cause_identification_contested = True


async def _unverified(engine, case):
    case.progress.problem_status = ProblemStatus.UNVERIFIED


async def _solution_withdrawn(engine, case):
    case.solutions = case.solutions[:-1]


async def _ten_turns(engine, case):
    for _ in range(10):
        await _turn(engine, case, "what else is in that log")


async def _driver_changed(engine, case):
    case.driver_id = "user_other"


async def _non_confirmation_row(engine, case):
    await _turn(
        engine,
        case,
        "here is the role's trust policy",
        _DSU(
            evidence_to_add=[
                EvidenceToAdd(
                    summary="The role trusts the cluster's OIDC provider.",
                    extract='"Federated": "arn:aws:iam::1:oidc-provider/eks"',
                    category=EvidenceCategory.CAUSAL_EVIDENCE,
                    source_type=EvidenceSourceType.CONFIGURATION,
                    source_file_id=_FILE_ID,
                )
            ]
        ),
    )


async def _row_disqualified(engine, case):
    """A failed fix's disconfirmation REFUTES one cause re-check; the other
    still qualifies, so the case still confirms its resolution."""
    marker = _row(
        EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        0,
        "The pod restart did not remove the cause; the failures returned.",
        source_type=EvidenceSourceType.ENGINE_INFERENCE,
        source_file_id=None,
        collected_by="engine",
    )
    case.evidence.append(marker)
    first = next(
        e
        for e in case.evidence
        if e.category == EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
        and e.collected_by == "user"
    )
    node = CausalNode(
        statement="The pod restart cleared the stale token cache",
        node_type=NodeType.INTERMEDIATE,
        generated_at_turn=0,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=eid, stance=EvidenceStance.REFUTES, reasoning="refuted"
            )
            for eid in (marker.evidence_id, first.evidence_id)
        ],
    )
    case.causal_nodes[node.node_id] = node


async def _hypothesis_refuted(engine, case):
    hyp = Hypothesis(
        statement="The STS regional endpoint is throttling the pods",
        category=HypothesisCategory.CONFIG,
        state=HypothesisState.REFUTED,
        refutation_reason="no throttling responses in the STS log",
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        generated_at_turn=case.current_turn,
    )
    case.hypotheses[hyp.hypothesis_id] = hyp


#: name -> (change, puts the case on hold)
CHANGES = {
    "revision_proposed": (_revision, True),
    "cause_contested": (_contested, False),
    "problem_unverified": (_unverified, False),
    "solution_withdrawn": (_solution_withdrawn, False),
    "ten_more_turns": (_ten_turns, False),
    "driver_changed": (_driver_changed, False),
    "non_confirmation_row": (_non_confirmation_row, False),
    "confirmation_row_disqualified": (_row_disqualified, False),
    "hypothesis_refuted": (_hypothesis_refuted, False),
}

#: The pairs: ten more turns and then each change the probe found un-answering
#: the decline on ``main``, and the revision with the contested cause. Applied
#: in order, so the change is the newest thing on the case.
PAIRS = [
    ("ten_more_turns", "cause_contested"),
    ("ten_more_turns", "problem_unverified"),
    ("ten_more_turns", "solution_withdrawn"),
    ("ten_more_turns", "revision_proposed"),
    ("revision_proposed", "cause_contested"),
]

RESOLVE_SCENARIOS = [(name,) for name in CHANGES] + PAIRS


async def _apply(engine, case, names) -> bool:
    on_hold = False
    for name in names:
        change, hold = CHANGES[name]
        await change(engine, case)
        on_hold = on_hold or hold
    return on_hold


# ---------------------------------------------------------------------------
# A declined resolution
# ---------------------------------------------------------------------------


class TestADeclinedResolutionBinds:
    @pytest.mark.parametrize(
        "names", RESOLVE_SCENARIOS, ids=["+".join(n) for n in RESOLVE_SCENARIOS]
    )
    async def test_the_decline_binds_after(self, names):
        engine, case = await _declined_resolve()
        on_hold = await _apply(engine, case, names)

        # The menu shows Close: the declined resolution is not what it offers.
        assert derive_disposition_eligibility(case)["closed"] == "ready"

        # The engine re-offers nothing; the reopen chip stays unless on hold.
        quiet = await _turn(engine, case, "what does that line mean")
        assert not set(RESOLVE_PAIR) & set(_labels(quiet))
        assert (REOPEN_CHIP in _labels(quiet)) is not on_hold
        assert derive_disposition_eligibility(case)["closed"] == "ready"

        # The model's resolution is refused.
        refused = await _turn(engine, case, "so is it done", _proposes("resolved"))
        assert not set(RESOLVE_PAIR) & set(_labels(refused))
        assert case.state == CaseState.INVESTIGATING
        if not on_hold:
            assert DECLINED_RESOLVE_FEEDBACK in _feedback(refused)
        assert derive_disposition_eligibility(case)["closed"] == "ready"


class TestWhatLapsesIt:
    async def test_a_new_solution_lapses_it_and_the_offer_returns(self):
        engine, case = await _declined_resolve()
        result = await _turn(
            engine,
            case,
            "we also pinned the audience in the service account",
            _DSU(
                solutions_to_add=[
                    SolutionToAdd(
                        description="Pin the token audience on the service account",
                        solution_type="config_change",
                        estimated_impact="the batch pods assume the role",
                        risks="none",
                    )
                ]
            ),
        )
        assert _labels(result) == RESOLVE_PAIR

    async def test_a_new_confirmation_lapses_it_and_the_offer_returns(self):
        engine, case = await _declined_resolve()
        result = await _turn(
            engine,
            case,
            "the overnight batch ran clean",
            _DSU(
                evidence_to_add=[
                    _to_add(
                        "causal_absence_evidence",
                        "The nightly job's token audience matches the provider.",
                    ),
                    _to_add(
                        "symptom_absence_evidence",
                        "Zero AssumeRole failures in the overnight batch.",
                    ),
                ]
            ),
        )
        assert _labels(result) == RESOLVE_PAIR

    async def test_a_same_turn_confirmation_does_not_lapse_it(self):
        """The #1895 shape: a deflection declines and falls through, and the
        model records the user's words as a confirmation on that turn."""
        case, engine = _resolvable_case(), _engine()
        offered = await _turn(engine, case, "the errors are gone now")
        assert _labels(offered) == RESOLVE_PAIR
        result = await _turn(
            engine,
            case,
            "Not yet - it has been clean for an hour but I want to wait for "
            "the overnight batch before calling it",
            _proposes(
                "resolved",
                evidence_to_add=[
                    _to_add(
                        "causal_absence_evidence",
                        "The token audience matched for the last hour.",
                    ),
                    _to_add(
                        "symptom_absence_evidence",
                        "No AssumeRole failures in the last hour.",
                    ),
                ],
            ),
        )
        assert DECLINED_RESOLVE_FEEDBACK in _feedback(result)
        assert not set(RESOLVE_PAIR) & set(_labels(result))

    async def test_once_lapsed_a_disqualified_confirmation_does_not_revive_it(self):
        engine, case = await _declined_resolve()
        before = {e.evidence_id for e in case.evidence}
        reoffer = await _turn(
            engine,
            case,
            "the overnight batch ran clean",
            _DSU(
                evidence_to_add=[
                    _to_add(
                        "causal_absence_evidence",
                        "The nightly job's token audience matches the provider.",
                    )
                ]
            ),
        )
        assert _labels(reoffer) == RESOLVE_PAIR, "premise: the decline lapsed"
        # A question withdraws the offer, recording nothing.
        await _turn(engine, case, "does that cover the weekly job too?")
        # The new row is then disqualified by a failed fix.
        new = next(e for e in case.evidence if e.evidence_id not in before)
        marker = _row(
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            0,
            "A cache flush did not remove the cause.",
            source_type=EvidenceSourceType.ENGINE_INFERENCE,
            source_file_id=None,
            collected_by="engine",
        )
        case.evidence.append(marker)
        node = CausalNode(
            statement="The cache flush cleared the stale token",
            node_type=NodeType.INTERMEDIATE,
            generated_at_turn=0,
            evidence_links=[
                NodeEvidenceLink(
                    evidence_id=eid, stance=EvidenceStance.REFUTES, reasoning="r"
                )
                for eid in (marker.evidence_id, new.evidence_id)
            ],
        )
        case.causal_nodes[node.node_id] = node
        assert derive_disposition_eligibility(case)["closed"] == "suggests_alternative"
        result = await _turn(engine, case, "ok")
        assert _labels(result) == RESOLVE_PAIR


# ---------------------------------------------------------------------------
# A declined deferred close
# ---------------------------------------------------------------------------

DEFERRED_CHANGES = [
    name
    for name in CHANGES
    # The deferred case confirms nothing, so there is no row to disqualify.
    if name != "confirmation_row_disqualified"
]


class TestADeclinedDeferredCloseBinds:
    @pytest.mark.parametrize("name", DEFERRED_CHANGES)
    async def test_the_decline_binds_after(self, name):
        engine, case = await _declined_deferred()
        on_hold = await _apply(engine, case, (name,))
        assert derive_disposition_eligibility(case)["closed"] == "ready"

        quiet = await _turn(engine, case, "what does that line mean")
        assert CLOSE_PAIR[0] not in _labels(quiet), "the close was re-offered"

        refused = await _turn(engine, case, "fine, wrap it up", _proposes("closed"))
        assert CLOSE_PAIR[0] not in _labels(refused), "the model's close was offered"
        if not on_hold:
            assert _DEFERRED_FEEDBACK in _feedback(refused)
        assert case.state == CaseState.INVESTIGATING

    async def test_a_new_solution_lapses_it(self):
        engine, case = await _declined_deferred()
        result = await _turn(
            engine,
            case,
            "we can also pin the audience on the service account",
            _DSU(
                solutions_to_add=[
                    SolutionToAdd(
                        description="Pin the token audience on the service account",
                        solution_type="config_change",
                        estimated_impact="the batch pods assume the role",
                        risks="none",
                    )
                ]
            ),
        )
        assert _labels(result) == CLOSE_PAIR


# ---------------------------------------------------------------------------
# A declined false-alarm close
# ---------------------------------------------------------------------------

#: A revision or a status change is how a finding goes (the controls below);
#: a false alarm holds no solution and no confirmation.
FALSE_ALARM_CHANGES = [
    "cause_contested",
    "ten_more_turns",
    "driver_changed",
    "non_confirmation_row",
    "hypothesis_refuted",
]


class TestADeclinedFalseAlarmCloseBinds:
    @pytest.mark.parametrize("name", FALSE_ALARM_CHANGES)
    async def test_the_decline_binds_after(self, name):
        engine, case = await _declined_false_alarm()
        await _apply(engine, case, (name,))
        assert derive_disposition_eligibility(case)["closed"] == "ready"
        for to_state in ("closed", "resolved"):
            refused = await _turn(engine, case, "so is it done", _proposes(to_state))
            assert _FALSE_ALARM_FEEDBACK in _feedback(refused)
            assert CLOSE_PAIR[0] not in _labels(refused)

    async def test_the_finding_withdrawn_lapses_it(self):
        engine, case = await _declined_false_alarm()
        await _turn(
            engine,
            case,
            "we do have customer tickets from that hour",
            _DSU(
                verification_updates=ProblemVerificationUpdate(
                    invalidation_withdrawn=True,
                    withdrawal_basis="the user has customer tickets from that hour",
                )
            ),
        )
        result = await _turn(engine, case, "close it then", _proposes("closed"))
        assert _FALSE_ALARM_FEEDBACK not in _feedback(result)
        assert _labels(result) == CLOSE_PAIR

    async def test_the_finding_replaced_lapses_it(self):
        engine, case = await _declined_false_alarm()
        await _turn(
            engine,
            case,
            "we do have customer tickets from that hour",
            _DSU(
                verification_updates=ProblemVerificationUpdate(
                    invalidation_withdrawn=True,
                    withdrawal_basis="the user has customer tickets from that hour",
                )
            ),
        )
        result = await _turn(
            engine,
            case,
            "the second gateway shows nothing either",
            _DSU(
                evidence_to_add=[
                    EvidenceToAdd(
                        summary="No 5xx on the second gateway for the window.",
                        extract="0 errors on gw-2",
                        category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
                        source_type=EvidenceSourceType.LOGS,
                        source_file_id=_FILE_ID,
                    )
                ],
                verification_updates=ProblemVerificationUpdate(
                    problem_invalidated=True,
                    invalidation_evidence_ids=["new_index_0"],
                    invalidation_basis="no 5xx on either gateway for the window",
                ),
            ),
        )
        assert case.progress.problem_status == ProblemStatus.INVALIDATED
        assert _labels(result) == CLOSE_PAIR, "the replaced finding's close"
