"""#2036 D2.3 on recorded facts: a fix's target and kind are read from two
records, both written when they held, and never rebuilt at the settle.

- The turn record (``TurnProgress.cause_at_start``): the cause pool at the
  start of the turn the fix ran in, taken once at the start of the turn's live
  apply, or as a promptless turn is recorded.
- The action (``ProposedAction.licence_ids``): the root(s) M5 licensed the fix
  on, recorded in the apply that proposed it, after its chain emission.

The shapes are PR #2553's last delta (F1-F5), the round 65 critique's, and the
two shapes today's ruling reads NEEDS_INFO until the fix names its node
(https://github.com/FaultMaven/faultmaven/issues/2036#issuecomment-6678007824).
Every turn enters through the real apply and writes its record as step 6
does (``recorded_turns._record``); the gate is the real one.
"""

from unittest.mock import AsyncMock

import pytest

from faultmaven.core.investigation.cause_assurance import (
    FIX_REMEDIATION,
    _cause_posited_turn,
    conclusion_node,
)
from faultmaven.core.investigation.milestone_engine.progress import (
    record_promptless_turn,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import fix_cause
from faultmaven.core.investigation.milestone_engine.turn_records import (
    _finish_deterministic_turn,
)
from faultmaven.core.investigation.schemas import (
    CausalNodeToAdd,
    HypothesisEvidenceLinkToAdd,
    HypothesisUpdate,
    InvestigationResponse_Diagnosis,
    MilestoneUpdates,
    NodeEvidenceLinkToAdd,
    RootCauseConclusionUpdate,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_bookkeeping import (
    _backfill_consumed_turn,
)
from faultmaven.modules.case.contracts import (
    Case,
    EvidenceStance,
    InvestigationActionType,
    NodeEvidenceLink,
    ProposedAction,
    RootCauseConclusion,
    TurnOutcome,
    TurnProgress,
)
from tests.unit.core.investigation import (
    test_problem_statement_revision_and_false_alarm as revision,
)
from tests.unit.core.investigation.bound_rows import VERIFYING_ID, upload
from tests.unit.core.investigation.fix_turns_2039 import _ev, _turn
from tests.unit.core.investigation.recorded_turns import _record, write_turn_record
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _engine as _full_engine,
)
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _turn as _full_turn,
)
from tests.unit.core.investigation.test_fix_registration_lag_2427 import (
    _PREVIOUS,
    _identified,
    _registered,
)
from tests.unit.core.investigation.test_fix_targets_2036 import (
    _attempts,
    _bare_candidate,
    _fix,
    _grounding,
    _intermediate,
    _ran,
    _recheck,
)
from tests.unit.core.investigation.test_fix_targets_review_2036 import (
    _B_HYP,
    _ground_b,
    _leak_rows,
)
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _CAUSAL,
    _CAUSE_GONE,
    _GONE,
    _SUPPORT,
    _obs,
)
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _record as _unrecorded,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _ROOT_ID,
    _add_sibling_chain,
    _identified_case,
    _row,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _engine
from tests.unit.core.investigation.test_replay_emission_turn_2537 import (
    _obs as _replay_obs,
)
from tests.unit.core.investigation.test_replay_emission_turn_2537 import (
    _replayed,
    _solution,
)

pytestmark = pytest.mark.unit

_READY = ResolutionReadiness.READY
_NEEDS_INFO = ResolutionReadiness.NEEDS_INFO
_HELD = "was held for the revised statement confirmed now"
_IGNORED = "accepted_fix_reported_in previous_message was ignored"


def _backdated(action_id) -> MilestoneUpdates:
    """The fix accepted by name, reported run in the previous message."""
    return MilestoneUpdates(
        solution_accepted=True,
        accepted_action_ref=action_id,
        accepted_fix_reported_in=_PREVIOUS,
    )


def _verdict(case):
    return assess_resolution_readiness(case).verdict


def _two_candidates(a, b):
    """No cause identified: A (root R1, bare) and B (its own root) stand at
    likelihoods ``a`` and ``b``."""
    case, root, hyp = _identified_case()
    _bare_candidate(case, root, hyp)
    hyp.likelihood = hyp.initial_likelihood = a
    sibling = _add_sibling_chain(case)
    case.hypotheses[_B_HYP].likelihood = case.hypotheses[_B_HYP].initial_likelihood = b
    return case, hyp, sibling


def _supports(h_id, e_ref) -> HypothesisEvidenceLinkToAdd:
    return HypothesisEvidenceLinkToAdd(
        hypothesis_id_ref=h_id,
        evidence_id_ref=e_ref,
        stance=EvidenceStance.SUPPORTS,
        reasoning="the pool saturates at the burst",
        stance_confidence=1.0,
    )


def _new_root(statement, produces, hyp_id) -> dict:
    """A ROOT emitted at new_index_0 with two supports (the turn's rows 0 and
    1), ``hyp_id`` re-rooted onto it, terminal."""
    return dict(
        causal_nodes_to_add=[
            CausalNodeToAdd(statement=statement, node_type="root", produces=produces)
        ],
        node_evidence_links=[
            NodeEvidenceLinkToAdd(
                node_ref="new_index_0",
                evidence_id_ref=f"new_index_{i}",
                stance="supports",
                reasoning="bears on the new root",
            )
            for i in (0, 1)
        ],
        hypotheses_to_update=[
            HypothesisUpdate(
                hypothesis_id=hyp_id,
                root_node_ref="new_index_0",
                root_is_terminal=True,
                terminal_basis_evidence_ids=["new_index_1"],
            )
        ],
    )


def _rcc_naming(node_id, root_cause="a memory leak in the gunicorn worker"):
    return RootCauseConclusionUpdate(
        root_cause=root_cause,
        mechanism="the worker leaks until OOM and the pod 503s",
        evidence_ids=["new_index_0"],
        likelihood=0.9,
        names_root_node_id=node_id,
    )


# ---------------------------------------------------------------------------
# The records
# ---------------------------------------------------------------------------


async def test_the_records_round_trip_and_a_case_stored_before_them_reads_legacy():
    """Done-when 1 and 5: both records survive the case's JSON (the
    ``cases.metadata`` column) and the ``turn_history`` validator; a record or
    an action stored before they existed loads with neither, which the settle
    reads as unkinded (ruling 1)."""
    case, _root, _hyp = _identified_case()
    await _record(
        _engine(),
        case,
        6,
        solutions_to_add=[_fix("raise max_connections to 100")],
        milestones=_ran(),
    )
    (record,), (fix,) = case.turn_history, _attempts(case)
    assert record.cause_at_start.root_ids == [_ROOT_ID]
    assert (fix.licence_ids, fix.target_ids, fix.fix_kind) == (
        [_ROOT_ID],
        [_ROOT_ID],
        FIX_REMEDIATION,
    )
    again = Case.model_validate_json(case.model_dump_json())
    assert again.turn_history == case.turn_history
    assert again.proposed_actions == case.proposed_actions

    stored = record.model_dump(mode="json")
    del stored["cause_at_start"]
    assert TurnProgress(**stored).cause_at_start is None
    stored = fix.model_dump(mode="json")
    for key in ("licence_ids", "licence_in_words", "fix_kind", "target_ids"):
        del stored[key]
    legacy = ProposedAction(**stored)
    assert (legacy.licence_ids, legacy.licence_in_words, legacy.fix_kind) == (
        [],
        False,
        None,
    )


@pytest.mark.parametrize("writer", ["promptless", "deterministic", "backstop"])
def test_a_turn_with_no_apply_records_the_cause_as_its_record_is_written(writer):
    """``record_promptless_turn`` serves the deterministic branches and the
    consumed-turn backstop: no apply ran, so nothing moved the cause."""
    case, _root, _hyp = _identified_case()
    case.current_turn = 6
    if writer == "promptless":
        record_promptless_turn(
            case, user_message="hi", agent_response="ok", progress_made=False
        )
    elif writer == "deterministic":
        _finish_deterministic_turn(case, "yes", "ok", {})
    else:
        _backfill_consumed_turn(
            case, user_message="hi", agent_response="Hello!", metadata={}
        )
    (record,) = case.turn_history
    assert record.cause_at_start == fix_cause(case)
    assert record.cause_at_start.root_ids == [_ROOT_ID]


def test_a_skipped_placeholder_records_no_cause():
    case, _root, _hyp = _identified_case()
    for turn in (6, 8):
        case.current_turn = turn
        record_promptless_turn(
            case, user_message="hi", agent_response="ok", progress_made=False
        )
    case.reconcile_turn_sequence()
    assert [
        (t.turn_number, t.outcome, t.cause_at_start is None) for t in case.turn_history
    ] == [
        (6, TurnOutcome.CONVERSATION, False),
        (7, TurnOutcome.SKIPPED, True),
        (8, TurnOutcome.CONVERSATION, False),
    ]


async def test_the_generation_path_records_the_cause_its_apply_started_with():
    """Step 6 of a real engine turn writes the live apply's start-of-turn
    cause, not the cause as the turn leaves it: A leads at 0.7 (M5's
    working-conclusion leg) when the turn starts, and the turn lowers it
    below the bar."""
    case, root, hyp = _identified_case()
    _bare_candidate(case, root, hyp)
    hyp.likelihood = hyp.initial_likelihood = 0.7
    engine = _full_engine()
    await _full_turn(engine, case, "here is the pool config")
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.",
            state_updates={
                "hypotheses_to_update": [
                    {"hypothesis_id": hyp.hypothesis_id, "likelihood": 0.3}
                ]
            },
        )
    )
    case.current_turn += 1
    result = await engine.process_turn(
        case=case, user_message="the pool never saturates", redaction_ctx=None
    )
    updated = result["case_updated"]
    record = updated.turn_history[-1]
    assert record.turn_number == updated.current_turn
    assert record.cause_at_start.licence_ids == [_ROOT_ID]
    assert fix_cause(updated).licence_ids == []


async def test_a_fix_backdated_onto_a_turn_with_no_record_stays_unkinded():
    """Decision (ii): a turn recorded before the field existed holds no
    cause, so the fix reads as before D2.3, silently."""
    case, _root, _hyp = _identified_case()
    eng = _engine()
    await _unrecorded(
        eng, case, 6, solutions_to_add=[_fix("raise max_connections to 100")]
    )
    await _unrecorded(eng, case, 7)
    _rows, meta = await _record(
        eng, case, 8, milestones=_backdated(case.proposed_actions[0].action_id)
    )
    (fix,) = _attempts(case)
    assert (fix.accepted_in_turn, fix.fix_kind, fix.target_ids) == (7, None, [])
    assert "without a target" not in (meta.get("system_feedback") or "")


@pytest.mark.parametrize("with_hypothesis", [True, False], ids=["re-rooted", "bare"])
async def test_a_fix_proposed_and_run_with_its_conclusions_new_root_takes_its_licence(
    with_hypothesis,
):
    """Critique point 1 (A1): with no cause at the start of the turn, one
    response emits a ROOT, a conclusion naming it as ``new_index_0``, and the
    unnamed fix the user ran. The licence is read after chain emission, so
    the placeholder has resolved: the fix is a remediation of the new root
    when a hypothesis is rooted there (licensed, #2485), else of nothing."""
    case, root, hyp = _identified_case()
    _bare_candidate(case, root, hyp)
    hyp.likelihood = hyp.initial_likelihood = 0.3
    eng = _engine()
    await _record(eng, case, 6)
    statement = "pgbouncer default_pool_size 20 caps checkout connections"
    fields = _new_root(statement, "D", hyp.hypothesis_id)
    if not with_hypothesis:
        del fields["hypotheses_to_update"]
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSAL, "pgbouncer SHOW POOLS: cl_waiting 40 at the burst"),
        _obs(_CAUSAL, "pgbouncer default_pool_size = 20"),
        root_cause_conclusion=_rcc_naming("new_index_0", statement),
        solutions_to_add=[_fix("raise default_pool_size to 50")],
        milestones=_ran(),
        **fields,
    )
    new = next(n for n in case.causal_nodes.values() if n.statement == statement)
    (fix,) = _attempts(case)
    expected = [new.node_id] if with_hypothesis else []
    assert (fix.licence_ids, fix.target_ids) == (expected, expected)
    await _recheck(eng, case, 8, new)
    assert _verdict(case) == (_READY if with_hypothesis else _NEEDS_INFO)


# ---------------------------------------------------------------------------
# PR #2553's delta 2: F1-F5, and the older same-turn deepening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("conclusion", [False, True], ids=["F1", "older"])
async def test_a_same_turn_deepening_does_not_take_the_fix_run_in_that_turn(
    conclusion,
):
    """F1: A (R1) leads at 0.85, nothing identified. In one response the user
    runs an unnamed "raise max_connections" and A is deepened onto a new R2.
    The fix ran against R1, the start-of-turn licence, so R2's re-check does
    not confirm it. The older shape: a standing conclusion on A, naming no
    node, and the same turn."""
    case, root, hyp = _identified_case()
    _bare_candidate(case, root, hyp)
    hyp.likelihood = hyp.initial_likelihood = 0.85
    if conclusion:
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="the postgres connection limit starves the checkout pool",
            mechanism="m",
            validated_hypothesis_id=hyp.hypothesis_id,
            likelihood=0.8,
            confidence_level="confident",
        )
    eng = _engine()
    await _record(eng, case, 6)
    statement = "a nightly job re-renders postgresql.conf from a stale template"
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSAL, f"{statement}: the template pins max_connections=20"),
        _obs(_CAUSAL, f"{statement}: the cron log shows the render at 02:00"),
        solutions_to_add=[_fix("raise max_connections to 100")],
        milestones=_ran(),
        **_new_root(statement, _ROOT_ID, hyp.hypothesis_id),
    )
    r2 = case.causal_nodes[hyp.root_node_id]
    assert r2.node_id != _ROOT_ID
    (fix,) = _attempts(case)
    assert (fix.fix_kind, fix.target_ids) == (FIX_REMEDIATION, [_ROOT_ID])
    await _recheck(eng, case, 8, r2)
    assert _verdict(case) == _NEEDS_INFO


@pytest.mark.parametrize(
    "lift_turn, rechecked, verdict",
    [(7, "B", _READY), (7, "A", _NEEDS_INFO), (6, "B", _READY)],
    ids=["F2", "F2-reversed", "critique-2"],
)
async def test_a_pending_fix_keeps_the_licence_it_was_proposed_under(
    lift_turn, rechecked, verdict
):
    """F2 and critique point 2: B leads, and the unnamed fix is proposed under
    B (5). A row then lifts A over B, in the turn the user runs it (7) or the
    turn before. The fix ran against B: B's grounding and re-check confirm
    it, and A's do not."""
    case, hyp, b = _two_candidates(a=0.65, b=0.7)
    eng = _engine()
    await _record(eng, case, 4)
    await _record(eng, case, 5, solutions_to_add=[_fix("pin the leaking library")])
    action_id = case.proposed_actions[-1].action_id
    lifting = dict(
        hypothesis_evidence_links=[_supports(hyp.hypothesis_id, "new_index_0")]
    )
    burst = _obs(_CAUSAL, "pgbouncer SHOW POOLS: cl_waiting 40 at the burst")
    await _record(
        eng,
        case,
        6,
        *([burst] if lift_turn == 6 else []),
        **(lifting if lift_turn == 6 else {}),
    )
    await _record(
        eng,
        case,
        7,
        *([burst] if lift_turn == 7 else []),
        milestones=_ran(action_id),
        **(lifting if lift_turn == 7 else {}),
    )
    assert hyp.likelihood > case.hypotheses[_B_HYP].likelihood
    (fix,) = _attempts(case)
    assert (fix.licence_ids, fix.target_ids) == ([b.node_id], [b.node_id])
    if rechecked == "B":
        await _record(eng, case, 8, *_leak_rows(), **_ground_b(b))
        await _recheck(eng, case, 9, b)
    else:
        await _record(eng, case, 8, **_grounding(hyp))
        await _record(
            eng,
            case,
            9,
            _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
            _obs(_GONE, "checkout 200 again", VERIFYING_ID),
        )
    assert _verdict(case) == verdict


async def test_a_fix_withdrawn_for_a_lost_licence_and_reproposed_takes_its_new_one():
    """Critique point 2's ``license_lost`` variant: the fix proposed under B
    is withdrawn when no hypothesis holds M5's bar (6); B is lifted again and
    the fix re-proposed and run in one response (7). No licence stood at the
    start of that turn, so the fix takes its own: B."""
    case, hyp, b = _two_candidates(a=0.4, b=0.7)
    eng = _engine()
    await _record(eng, case, 4)
    await _record(eng, case, 5, solutions_to_add=[_fix("pin the leaking library")])
    withdrawn = case.proposed_actions[-1]
    await _record(
        eng,
        case,
        6,
        hypotheses_to_update=[HypothesisUpdate(hypothesis_id=_B_HYP, likelihood=0.45)],
    )
    assert withdrawn.superseded_reason == "license_lost"
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSAL, "worker RSS grows 40MB/h until OOM"),
        hypothesis_evidence_links=[_supports(_B_HYP, "new_index_0")],
        hypotheses_to_update=[HypothesisUpdate(hypothesis_id=_B_HYP, likelihood=0.9)],
        solutions_to_add=[_fix("pin the leaking library")],
        milestones=_ran("new_index_0"),
    )
    assert case.turn_history[-1].cause_at_start.licence_ids == []
    (fix,) = _attempts(case)
    assert fix is not withdrawn
    assert (fix.licence_ids, fix.target_ids) == ([b.node_id], [b.node_id])
    await _record(eng, case, 8, *_leak_rows(), **_ground_b(b))
    await _recheck(eng, case, 9, b)
    assert _verdict(case) == _READY


@pytest.mark.parametrize("problem_gone", [False, True], ids=["F3", "F3-relief"])
async def test_a_backdated_fix_is_kinded_against_its_own_turns_record(problem_gone):
    """F3 (and delta 1's P2): the fix proposed at 6 while A is identified;
    in turn 7 the model's links refute A (and the user may report relief);
    at 8 the fix is declared run in the previous message. It is dated 7, not
    refused, and kinded against turn 7's record, which held A: B's grounding
    and re-check after it do not confirm it."""
    case, a, _hyp = _identified_case()
    b = _add_sibling_chain(case)
    case.hypotheses[_B_HYP].likelihood = case.hypotheses[_B_HYP].initial_likelihood = (
        0.9
    )
    eng = _engine()
    await _record(eng, case, 6, solutions_to_add=[_fix("raise max_connections to 100")])
    relief = [_obs(_GONE, "checkout returns 200", VERIFYING_ID)] if problem_gone else []
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSAL, "pg_settings: max_connections is 500"),
        _obs(_CAUSAL, "pg_stat_activity: 35 of 500 connections in use at the burst"),
        *relief,
        node_evidence_links=[
            NodeEvidenceLinkToAdd(
                node_ref=_ROOT_ID,
                evidence_id_ref=f"new_index_{i}",
                stance="refutes",
                reasoning="the limit is not reached",
            )
            for i in (0, 1)
        ],
    )
    assert case.turn_history[-1].cause_at_start.root_ids == [a.node_id]
    _rows, meta = await _record(
        eng, case, 8, milestones=_backdated(case.proposed_actions[0].action_id)
    )
    (fix,) = _attempts(case)
    assert (fix.accepted_in_turn, fix.fix_kind, fix.target_ids) == (
        7,
        FIX_REMEDIATION,
        [a.node_id],
    )
    assert _IGNORED not in (meta.get("system_feedback") or "")
    await _record(eng, case, 9, *_leak_rows(), **_ground_b(b))
    await _recheck(eng, case, 10, b)
    assert _verdict(case) == _NEEDS_INFO


async def test_a_conclusion_the_recompute_links_to_a_hypothesis_names_its_root():
    """F4 (realistic): the conclusion names an intermediate X, and the
    recompute attributes it to A, rooted at R. ``conclusion_node`` reads the
    hypothesis's root first, as ``_cause_posited_turn`` does, so the unnamed
    fix is a remediation of R, which R's grounding and re-check confirm."""
    case, root, hyp = _identified_case()
    _bare_candidate(case, root, hyp)
    x = _intermediate(case)
    eng = _engine()
    await _record(
        eng,
        case,
        5,
        root_cause_conclusion=RootCauseConclusionUpdate(
            root_cause="the postgres connection limit starves the checkout pool",
            mechanism="the checkout pool opens more connections than postgres admits",
            evidence_ids=[_SUPPORT],
            likelihood=0.9,
            names_root_node_id=x,
        ),
    )
    rcc = case.root_cause_conclusion
    assert (rcc.names_root_node_id, rcc.validated_hypothesis_id) == (
        x,
        hyp.hypothesis_id,
    )
    await _record(
        eng,
        case,
        6,
        solutions_to_add=[_fix("raise max_connections to 100")],
        milestones=_ran(),
    )
    (fix,) = _attempts(case)
    assert (fix.fix_kind, fix.target_ids) == (FIX_REMEDIATION, [_ROOT_ID])
    await _record(eng, case, 7, **_grounding(hyp))
    await _record(
        eng,
        case,
        8,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    assert _verdict(case) == _READY


def test_the_posited_turn_and_the_fix_read_one_conclusion_node():
    """F4: one reader of the conclusion's node. With the conclusion's
    hypothesis rooted at R and its ``names_root_node_id`` at B, both read R;
    with no hypothesis, both read B."""
    case, root, hyp = _identified_case()
    _bare_candidate(case, root, hyp)
    b = _add_sibling_chain(case)
    b.metadata.update({"root_terminal": None, "root_attached_turn": 6})
    root.metadata["root_attached_turn"] = 3
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="c",
        mechanism="m",
        names_root_node_id=b.node_id,
        validated_hypothesis_id=hyp.hypothesis_id,
        likelihood=0.8,
        confidence_level="confident",
    )
    assert (conclusion_node(case), _cause_posited_turn(case)) == (_ROOT_ID, 3)
    case.root_cause_conclusion.validated_hypothesis_id = None
    assert (conclusion_node(case), _cause_posited_turn(case)) == (b.node_id, 6)


@pytest.mark.parametrize("relist", ["none", "re-listed", "first"])
async def test_the_2427_ready_shape_stays_ready_beside_a_refuting_link(relist):
    """F5: the #2427 shape (the cause and the problem seen gone at 10, the fix
    minted and declared run then at 11), with a REFUTES link on sibling B
    stored before, re-listed at 10, or first made at 10. Nothing refuses the
    backdate: the fix is dated 10 and kinded against turn 10's record."""
    eng, case, _hyp = _identified()
    b = _add_sibling_chain(case)
    flat = _row("leak_flat", _CAUSAL, 4, "worker RSS flat for 24h")
    case.evidence.append(flat)
    if relist != "first":
        b.evidence_links.append(
            NodeEvidenceLink(
                evidence_id=flat.evidence_id,
                stance=EvidenceStance.REFUTES,
                reasoning="no leak",
                linked_at_turn=4,
            )
        )
    refuting = NodeEvidenceLinkToAdd(
        node_ref=b.node_id,
        evidence_id_ref=flat.evidence_id,
        stance="refutes",
        reasoning="no leak",
    )
    meta = await _turn(
        eng,
        case,
        10,
        _ev(_CAUSE_GONE, "max_connections reads 100 after the change"),
        _ev(_GONE, "checkout returns 200, 503 rate 0.0/s"),
        **({"node_evidence_links": [refuting]} if relist != "none" else {}),
    )
    write_turn_record(case, meta)
    meta = await _registered(eng, case, _PREVIOUS)
    write_turn_record(case, meta)
    (fix,) = _attempts(case)
    assert (fix.accepted_in_turn, fix.fix_kind, fix.target_ids) == (
        10,
        FIX_REMEDIATION,
        [_ROOT_ID],
    )
    assert _verdict(case) == _READY


# ---------------------------------------------------------------------------
# A revision hold: the replay, the confirmation turn, and a backdate onto a hold
# ---------------------------------------------------------------------------


@pytest.fixture
def recorded_revision_turns(monkeypatch):
    """The revision helper's applies write their turn records too."""
    apply = revision._apply

    async def _apply_and_record(engine, case, dsu, response=None):
        meta = await apply(engine, case, dsu, response)
        write_turn_record(case, meta)
        return meta

    monkeypatch.setattr(revision, "_apply", _apply_and_record)


async def test_a_held_fix_is_kinded_against_its_replayed_bundles_start(
    recorded_revision_turns,
):
    """The cause staged at 4, the fix run at 5 (held, unnamed), both replayed
    at the confirmation (6). Turn 5's record holds the live pool, without the
    staged cause; the replayed bundle reads its own start, which holds it."""
    fix = revision._DSU(
        solutions_to_add=[_solution("config_change", "raise the pool to 50")],
        milestones=MilestoneUpdates(solution_accepted=True),
    )
    engine, case, _hyp, root, (shown, support, _), _ = await _replayed(held=[fix])
    held = next(t for t in case.turn_history if t.turn_number == 5)
    assert root.node_id not in held.cause_at_start.reference_ids
    (attempt,) = _attempts(case)
    assert (attempt.accepted_in_turn, attempt.fix_kind, attempt.target_ids) == (
        5,
        FIX_REMEDIATION,
        [root.node_id],
    )
    case.current_turn = 7
    file_id = upload(case, 7)
    await revision._apply(
        engine,
        case,
        revision._DSU(
            evidence_to_add=[
                _replay_obs(_CAUSE_GONE, "pool reads 50", support, file_id),
                _replay_obs(_GONE, "orders answer in 40ms", shown, file_id),
            ]
        ),
    )
    assert _verdict(case) == _READY


async def _confirmed_with(fields, pending_before_hold=False):
    """The revision staged at 4 is confirmed at 5 (the replay ran), and turn
    5's live response carries ``fields``."""
    original = revision._case

    def _case_with_a_pending_fix():
        case = original()
        case.proposed_actions.append(
            ProposedAction(
                case_id=case.case_id,
                action_type=InvestigationActionType.SOLUTION,
                description="raise the orders pool to 50",
                proposed_in_turn=3,
            )
        )
        return case

    if pending_before_hold:
        revision._case = _case_with_a_pending_fix
    try:
        engine, case, _hyp, root, _rows, _file = await _replayed()
    finally:
        revision._case = original
    meta = await revision._apply(engine, case, revision._DSU(**fields))
    return case, root, meta


async def test_a_fix_run_in_the_confirmation_turn_takes_the_replayed_root():
    """Critique point 7 (A4): the confirmation turn's live apply starts after
    the replay, so a fix the user reports in that message acts on the cause
    the hold turns showed them."""
    case, root, _meta = await _confirmed_with(
        dict(solutions_to_add=[_fix("raise the orders pool to 50")], milestones=_ran())
    )
    (fix,) = _attempts(case)
    assert (fix.accepted_in_turn, fix.fix_kind, fix.target_ids) == (
        5,
        FIX_REMEDIATION,
        [root.node_id],
    )


@pytest.mark.parametrize("pending", [False, True], ids=["minted-now", "pending-before"])
async def test_a_backdate_onto_a_hold_turn_is_dated_now(pending):
    """Critique point 3 (A3): "yes, and I ran it" on the confirmation turn,
    declared run in the previous message, a hold turn whose record lacks the
    replayed cause work. The backdate is refused and the fix dated this
    turn; only its date changes."""
    case, root, meta = await _confirmed_with(
        dict(
            solutions_to_add=[_fix("raise the orders pool to 50")],
            milestones=MilestoneUpdates(
                solution_accepted=True,
                accepted_action_ref="new_index_0",
                accepted_fix_reported_in=_PREVIOUS,
            ),
        ),
        pending_before_hold=pending,
    )
    (fix,) = _attempts(case)
    assert (fix.accepted_in_turn, fix.fix_kind, fix.target_ids) == (
        5,
        FIX_REMEDIATION,
        [root.node_id],
    )
    assert _HELD in meta["system_feedback"]


# ---------------------------------------------------------------------------
# The licence is never changed after
# ---------------------------------------------------------------------------


async def test_a_restatement_keeps_the_licence_the_fix_was_proposed_under():
    """A restatement under a new licence (A now leads) keeps the pending
    action, and its recorded licence: B."""
    case, hyp, b = _two_candidates(a=0.65, b=0.7)
    eng = _engine()
    await _record(eng, case, 4)
    await _record(eng, case, 5, solutions_to_add=[_fix("pin the leaking library")])
    pending = case.proposed_actions[-1]
    await _record(
        eng,
        case,
        6,
        _obs(_CAUSAL, "pgbouncer SHOW POOLS: cl_waiting 40 at the burst"),
        hypothesis_evidence_links=[_supports(hyp.hypothesis_id, "new_index_0")],
        solutions_to_add=[_fix("pin the leaking library")],
    )
    assert case.proposed_actions[-1] is pending
    assert pending.licence_ids == [b.node_id]
    assert case.turn_history[-1].cause_at_start.licence_ids == [b.node_id]
    assert fix_cause(case).licence_ids == [_ROOT_ID]
