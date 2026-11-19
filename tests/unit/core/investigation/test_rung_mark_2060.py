"""#2060: a root a chain was deepened past is a rung, and a node validated as a
rung re-earns the ROOT bar before any reader counts it as a root.

Deepening keeps the old root's stored ``NodeType.ROOT`` (#2036). The ruling
(2026-11-17, option (a), as amended 2026-11-19): a ROOT-typed node on a
STANDING hypothesis's path that no standing hypothesis is rooted at is a rung
to every position reader (the bar, the label, the notes, the grade); a node
that entered VALIDATED that way carries ``validated_as_rung`` and is no
validated or identified root to any reader until the derive judges it as a
root, whatever moved it there (a re-root, a retirement, a refutation, a path
change, in either order of a turn's root refs).

Every test asserts outcomes: node states, the grade, the cause pool, fix
kinds, window readings and rendered lines.
"""

import hashlib
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.core.investigation.causal_graph.derivation import derive_node_states
from faultmaven.core.investigation.causal_graph.disconfirmation import failed_fix_of
from faultmaven.core.investigation.causal_graph.projection import (
    project_hypothesis_states_from_roots,
)
from faultmaven.core.investigation.causal_graph.pruning import attach_root
from faultmaven.core.investigation.causal_map import render_causal_map
from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_INTERCEPTION,
    FIX_REMEDIATION,
    _pool_legs,
    _validated_roots,
    fix_windows,
    grade_cause_assurance,
    latest_decided_fix_window,
)
from faultmaven.core.investigation.hypothesis_manager import (
    STAGNATION_HORIZON_ITERATIONS,
    HypothesisManager,
)
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_assessment_state,
)
from faultmaven.core.investigation.milestone_engine.hypothesis_updates import (
    _apply_hypothesis_action_intent,
)
from faultmaven.core.investigation.milestone_engine.response_application import (
    FIX_CAUSE_AT_START_KEY,
    _settle_fix_attempts,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    FIXES_ACCEPTED_KEY,
    fix_cause,
    licence_leg_ids,
)
from faultmaven.core.investigation.problem_status import revision_refusal
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    _build_causal_graph_block,
)
from faultmaven.core.investigation.schemas import (
    CausalNodeToAdd,
    HypothesisUpdate,
    MilestoneUpdates,
    NodeEvidenceLinkToAdd,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    CauseAssuranceGrade,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InvestigationActionType,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProposedAction,
    ValidationMethod,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    SQLiteCaseRepository,
)
from tests.case_builders import investigating_case
from tests.unit.core.investigation.bound_rows import VERIFYING_ID
from tests.unit.core.investigation.recorded_turns import _record
from tests.unit.core.investigation.test_fix_targets_2036 import (
    _attempts,
    _deepen,
    _fix,
    _intermediate,
    _ran,
)
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _CAUSAL,
    _CAUSE_GONE,
    _PRESENT,
    _SUPPORT,
    _obs,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _failed_fix as _m6_failed_fix,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _identified_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _engine,
)

pytestmark = pytest.mark.unit

_PROBLEM = "checkout requests fail with 502 under load"
_D, _X, _Y = "cn_0000000000d0", "cn_0000000000e1", "cn_0000000000f1"
_HYP, _HYP2 = "hyp_000000000001", "hyp_000000000002"
_COUNTER = (
    "faultmaven.core.investigation.causal_graph.derivation."
    "root_validation_blocked_support_count_total"
)
_NO_ROOT = CauseAssuranceGrade.NO_ROOT
_MECHANISTIC = CauseAssuranceGrade.MECHANISTIC


# ---------------------------------------------------------------------------
# Builders: a hypothesis rooted at X through attach_root, Y above X
# ---------------------------------------------------------------------------


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _evidence(label: str, category=EvidenceCategory.CAUSAL_EVIDENCE) -> Evidence:
    return Evidence(
        evidence_id=_eid(label),
        summary=f"fact-{label} metric-{label} reading-{label}",
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_0123456789ab",
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def _supports(node: CausalNode, case, *labels, confidence: float = 1.0) -> None:
    for label in labels:
        case.evidence.append(_evidence(label))
        node.evidence_links.append(
            NodeEvidenceLink(
                evidence_id=_eid(label),
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on the node",
                linked_at_turn=2,
                stance_confidence=confidence,
            )
        )


def _node(node_id, statement, node_type=NodeType.ROOT) -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement=statement,
        node_type=node_type,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=node_type == NodeType.ROOT,
        generated_at_turn=1,
    )


def _hyp(hypothesis_id=_HYP, statement="leaked connections exhaust the pool"):
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        statement=statement,
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="posited",
        generated_at_turn=1,
    )


def _case(x_statement="pool connections leak until the pool is exhausted"):
    """H1 rooted at X (``path = [X, D]``) through ``attach_root``; Y,
    ROOT-typed, sits above X with nothing linked."""
    nodes = [
        _node(_D, _PROBLEM, NodeType.PROBLEM),
        _node(_X, x_statement),
        _node(_Y, "a config push halved the pool's connection lifetime"),
    ]
    hyp = _hyp()
    case = investigating_case(
        case_id="case_000000002060",
        current_turn=5,
        statement=_PROBLEM,
        symptom=_PROBLEM,
        causal_nodes={n.node_id: n for n in nodes},
        causal_edges=[
            CausalEdge(cause_node_id=_Y, effect_node_id=_X),
            CausalEdge(cause_node_id=_X, effect_node_id=_D),
        ],
        evidence=[],
        hypotheses={hyp.hypothesis_id: hyp},
    )
    attach_root(case, hyp, _X, [_X, _D], 1)
    return case, case.causal_nodes[_X], hyp


def _deepen_to_y(case, hyp, turn=6) -> None:
    attach_root(case, hyp, _Y, [_Y, _X, _D], turn)


def _second(case) -> Hypothesis:
    hyp = _hyp(_HYP2, "the pool is the culprit")
    case.hypotheses[hyp.hypothesis_id] = hyp
    return hyp


def _settle(case) -> None:
    """The recompute's order: derive, then project (#695)."""
    derive_node_states(case)
    project_hypothesis_states_from_roots(case)


_MANAGER = HypothesisManager()


def _stop_standing(case, hyp, end: HypothesisState) -> None:
    """``hyp`` leaves standing through its lifecycle writer: a refutation on a
    refuting row (``refute_hypothesis``), the user's retire
    (``_apply_hypothesis_action_intent``), or the soft transition at the end
    of a stagnation arc in the gray zone (``check_state_transition``)."""
    if end == HypothesisState.REFUTED:
        row = _evidence(f"rules-out-{hyp.hypothesis_id}")
        case.evidence.append(row)
        _MANAGER.refute_hypothesis(
            hyp, case.current_turn, [row.evidence_id], "the config push never happened"
        )
    elif end == HypothesisState.RETIRED:
        _apply_hypothesis_action_intent(
            _MANAGER,
            case,
            {"hypothesis_id": hyp.hypothesis_id, "action": "retire"},
            "set that one aside",
            {},
        )
    else:
        hyp.likelihood = 0.4
        hyp.iterations_without_progress = STAGNATION_HORIZON_ITERATIONS
        _MANAGER.check_state_transition(hyp, case.current_turn)
    assert hyp.state == end


def _refute(case, node: CausalNode, label: str) -> None:
    """A confident refuting row on ``node``; the next derive refutes it."""
    case.evidence.append(_evidence(label))
    node.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=_eid(label),
            stance=EvidenceStance.REFUTES,
            reasoning="rules the node out",
            linked_at_turn=2,
            stance_confidence=1.0,
        )
    )


def _line(block: str, node_id: str) -> str:
    return next(line for line in block.splitlines() if node_id in line)


def _rung_validated_restating_x():
    """X restates the problem and carries two independent causal supports.
    Deepened past, it validates as a rung: outside the restatement guard."""
    case, x, hyp = _case(x_statement=_PROBLEM)
    _deepen_to_y(case, hyp)
    _supports(x, case, "x1", "x2")
    _settle(case)
    assert x.node_state == NodeState.VALIDATED
    assert grade_cause_assurance(case) == _NO_ROOT
    return case, x, hyp


# ---------------------------------------------------------------------------
# 1. The regression: the bar, the label and the note agree
# ---------------------------------------------------------------------------


def test_a_root_deepened_past_validates_on_one_support_as_the_rung_it_is_shown_as():
    """The issue's fixture. On main X stayed INCONCLUSIVE, shown
    ``[intermediate/inconclusive]`` beside the ROOT bar's SECOND INDEPENDENT
    note."""
    case, x, hyp = _case()
    _supports(x, case, "x1")
    _deepen_to_y(case, hyp)
    with patch(_COUNTER) as counter:
        _settle(case)
    block = _build_causal_graph_block(case)
    assert x.node_state == NodeState.VALIDATED
    assert "[intermediate/validated]" in _line(block, _X)
    assert "SECOND INDEPENDENT" not in block
    assert not counter.labels.called  # no ROOT hold to count


def test_a_root_standing_as_one_keeps_the_root_bar_its_hold_counted():
    """The control: Y is H1's root on another path, X an orphan root with one
    hedged-free support. The ROOT bar holds it, and the hold is counted and
    noted."""
    case, x, hyp = _case()
    _supports(x, case, "x1")
    case.causal_edges.append(CausalEdge(cause_node_id=_Y, effect_node_id=_D))
    attach_root(case, hyp, _Y, [_Y, _D], 6)
    with patch(_COUNTER) as counter:
        _settle(case)
    assert x.node_state == NodeState.INCONCLUSIVE
    assert counter.labels.called
    assert "SECOND INDEPENDENT" in _line(_build_causal_graph_block(case), _X)


# ---------------------------------------------------------------------------
# Amendment 1: deepening past a validated root drops the grade
# ---------------------------------------------------------------------------


def test_deepening_past_a_validated_root_drops_the_grade_until_the_new_root_validates():
    case, x, hyp = _case()
    _supports(x, case, "x1", "x2")
    _settle(case)
    assert grade_cause_assurance(case) == _MECHANISTIC

    _deepen_to_y(case, hyp)
    _settle(case)
    assert x.node_state == NodeState.VALIDATED  # a validated rung
    assert _validated_roots(case) == []
    assert grade_cause_assurance(case) == _NO_ROOT
    assert render_causal_map(case) is None

    _supports(case.causal_nodes[_Y], case, "y1", "y2")
    _settle(case)
    assert [n.node_id for n in _validated_roots(case)] == [_Y]
    assert grade_cause_assurance(case) == _MECHANISTIC
    assert hyp.state == HypothesisState.VALIDATED


@pytest.mark.parametrize("deepened", [True, False])
def test_the_report_map_draws_no_validated_rung_as_the_established_cause(deepened):
    """Z, a validated root off every chain into D, holds the grade. On the
    chain, X is validated: as a rung when deepened past (no map), as a root
    otherwise (the control draws one)."""
    case, x, hyp = _case()
    _supports(x, case, "x1", "x2")
    z = _node("cn_0000000000c3", "a noisy neighbour saturates the node's NIC")
    case.causal_nodes[z.node_id] = z
    _supports(z, case, "z1", "z2")
    if deepened:
        _deepen_to_y(case, hyp)
    _settle(case)
    assert x.node_state == z.node_state == NodeState.VALIDATED
    assert grade_cause_assurance(case) == _MECHANISTIC
    assert (render_causal_map(case) is None) is deepened


def test_a_root_identified_for_a_rival_is_identified_by_no_reader_once_deepened_past():
    """X is H2's identified root while H1's chain runs through it. H2
    retires: X is deepened past, still VALIDATED with a True verdict, and no
    cause pool or fix record counts it as the identified cause."""
    case, x, hyp = _case()
    rival = _second(case)
    attach_root(case, rival, _X, [_X, _D], 2)
    _deepen_to_y(case, hyp)
    _supports(x, case, "x1", "x2")
    x.record_root_terminal(True, [_eid("x1")], 3)
    _settle(case)
    assert _pool_legs(case)[0] == {_X}  # H2's identified root
    _stop_standing(case, rival, HypothesisState.RETIRED)
    _settle(case)
    assert x.node_state == NodeState.VALIDATED and x.root_terminal is True
    assert _pool_legs(case)[0] == set()
    assert fix_cause(case).root_ids == ()
    assert grade_cause_assurance(case) == _NO_ROOT


# ---------------------------------------------------------------------------
# 2-3. The deepening hypothesis stops standing: retired or refuted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("end", [HypothesisState.RETIRED, HypothesisState.REFUTED])
def test_a_rung_validated_restating_node_is_no_root_once_its_chain_stops_standing(
    end,
):
    """r2928 probe1 A and probe6: X validated as a rung restates the problem.
    The hypothesis that deepened past it stops standing, so X stands as a
    root. Before any derive no reader counts it; the derive then holds it."""
    case, x, hyp = _rung_validated_restating_x()
    _refute(case, case.causal_nodes[_Y], "y-ruled-out")
    _settle(case)
    assert case.causal_nodes[_Y].node_state == NodeState.REFUTED
    assert x.node_state == NodeState.VALIDATED
    _stop_standing(case, hyp, end)
    # No derive in between: a turn that retires without a recompute. X is the
    # one root not settled, and the next prompt says what holds it.
    assert _validated_roots(case) == []
    assert grade_cause_assurance(case) == _NO_ROOT
    line = _line(_build_causal_graph_block(case), _X)
    assert "[root/inconclusive]" in line
    assert "MORE SUPPORTING EVIDENCE WILL NOT VALIDATE IT" in line
    _settle(case)
    assert x.node_state == NodeState.INCONCLUSIVE
    assert grade_cause_assurance(case) == _NO_ROOT


@pytest.mark.parametrize("end", [HypothesisState.RETIRED, HypothesisState.REFUTED])
def test_refuting_and_retiring_the_deepening_hypothesis_grade_alike(end):
    """r2928 probe6's shape: X validated as a ROOT, then deepened past to Y,
    which is refuted. Only standing hypotheses confer position, so X stands
    as a root again either way and keeps its validation."""
    case, x, hyp = _case()
    _supports(x, case, "x1", "x2")
    _settle(case)
    _deepen_to_y(case, hyp)
    _settle(case)
    assert grade_cause_assurance(case) == _NO_ROOT
    _refute(case, case.causal_nodes[_Y], "y-ruled-out")
    _settle(case)
    _stop_standing(case, hyp, end)
    _settle(case)
    assert x.node_state == NodeState.VALIDATED
    assert grade_cause_assurance(case) == _MECHANISTIC


# ---------------------------------------------------------------------------
# 4. A path move, in both orders of a turn's root refs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order", ["move-first", "rival-first"])
def test_a_rung_validated_restating_node_rooted_by_a_rival_is_held(order):
    """r2928 probe1 B and the critique's S2: H1 moves off X's chain and H2
    roots at X, in either order. Between the attaches and the derive no
    reader sees a validated root; the derive holds X."""
    case, x, hyp = _rung_validated_restating_x()
    rival = _second(case)
    case.causal_edges.append(CausalEdge(cause_node_id=_Y, effect_node_id=_D))
    attaches = [
        lambda: attach_root(case, hyp, _Y, [_Y, _D], 7),
        lambda: attach_root(case, rival, _X, [_X, _D], 7),
    ]
    for attach in attaches if order == "move-first" else attaches[::-1]:
        attach()
        assert _validated_roots(case) == []
    assert grade_cause_assurance(case) == _NO_ROOT
    project_hypothesis_states_from_roots(case)  # the confirm-stamp's call
    assert rival.state == HypothesisState.ACTIVE
    x.record_root_terminal(True, [_eid("x1")], 7)  # a same-turn verdict
    assert not x.is_identified_root
    assert _pool_legs(case) == (set(), set(), set())
    _settle(case)
    assert x.node_state == NodeState.INCONCLUSIVE
    assert rival.state == HypothesisState.ACTIVE
    assert grade_cause_assurance(case) == _NO_ROOT


@pytest.mark.parametrize("order", ["deepen-first", "heir-first"])
def test_a_valid_heir_keeps_a_root_validated_as_a_root(order):
    """r2928 probe3: X validated as a ROOT, then the statement revised to its
    wording. H1 deepens past X while H2 takes it, in either order. X faced the
    guard as a root, so it keeps VALIDATED, as on main."""
    case, x, hyp = _case()
    _supports(x, case, "x1", "x2")
    _settle(case)
    case.problem_verification.symptom_statement = x.statement
    _settle(case)
    heir = _second(case)
    attaches = [
        lambda: attach_root(case, hyp, _Y, [_Y, _X, _D], 7),
        lambda: attach_root(case, heir, _X, [_X, _D], 7),
    ]
    for attach in attaches if order == "deepen-first" else attaches[::-1]:
        attach()
    _settle(case)
    assert x.node_state == NodeState.VALIDATED
    assert heir.state == HypothesisState.VALIDATED
    assert grade_cause_assurance(case) == _MECHANISTIC
    assert "[root/validated]" in _line(_build_causal_graph_block(case), _X)


# ---------------------------------------------------------------------------
# 5. The life of the mark
# ---------------------------------------------------------------------------


def test_a_node_validated_as_a_rung_then_as_a_root_stays_validated_when_re_rooted():
    """Validated as a rung, then re-rooted on two independent supports: it
    validates as a root, so the mark goes even though its state does not
    change. The statement is then revised to X's wording, X is deepened past
    and re-rooted again: it faced the guard as a root, so it stays."""
    case, x, hyp = _case()
    _deepen_to_y(case, hyp)
    _supports(x, case, "x1", "x2")
    _settle(case)
    attach_root(case, hyp, _X, [_X, _D], 7)
    _settle(case)
    assert grade_cause_assurance(case) == _MECHANISTIC

    case.problem_verification.symptom_statement = x.statement
    _deepen_to_y(case, hyp, 8)
    _settle(case)
    attach_root(case, hyp, _X, [_X, _D], 9)
    _settle(case)
    assert x.node_state == NodeState.VALIDATED
    assert grade_cause_assurance(case) == _MECHANISTIC


def test_a_rung_validated_on_one_support_re_rooted_faces_the_root_bar():
    case, x, hyp = _case()
    _deepen_to_y(case, hyp)
    _supports(x, case, "x1")
    _settle(case)
    assert x.node_state == NodeState.VALIDATED
    attach_root(case, hyp, _X, [_X, _D], 7)
    _settle(case)
    assert x.node_state == NodeState.INCONCLUSIVE
    assert "SECOND INDEPENDENT" in _line(_build_causal_graph_block(case), _X)


def test_a_rung_validated_on_one_support_whose_chain_retires_is_noted_at_once():
    """No derive between the retirement and the next prompt: X, the one root
    not settled, carries the ROOT bar's note."""
    case, x, hyp = _case()
    _deepen_to_y(case, hyp)
    _supports(x, case, "x1")
    _refute(case, case.causal_nodes[_Y], "y-ruled-out")
    _settle(case)
    _stop_standing(case, hyp, HypothesisState.RETIRED)
    line = _line(_build_causal_graph_block(case), _X)
    assert "[root/inconclusive]" in line and "SECOND INDEPENDENT" in line


@pytest.fixture
async def sqlite_repository():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield SQLiteCaseRepository(session)
    await engine.dispose()


async def test_the_mark_survives_a_save_and_reload(sqlite_repository):
    """A marked node saved and loaded through the SQLite repository still
    re-earns the root bar once its chain stops standing."""
    case, _x, _hyp = _rung_validated_restating_x()
    await sqlite_repository.save(case)
    reloaded = await sqlite_repository.get(case.case_id)
    x, hyp = reloaded.causal_nodes[_X], reloaded.hypotheses[_HYP]
    assert x.node_state == NodeState.VALIDATED
    _stop_standing(reloaded, hyp, HypothesisState.RETIRED)
    assert grade_cause_assurance(reloaded) == _NO_ROOT
    _settle(reloaded)
    assert x.node_state == NodeState.INCONCLUSIVE


# ---------------------------------------------------------------------------
# The restatement guard and the hold, by position
# ---------------------------------------------------------------------------


def test_a_deepened_past_node_is_outside_the_restatement_guard_and_its_hold():
    """Y restates the problem and is held by the guard alone; X, below it,
    is a hedged rung. The hold governs the case, as the grade says, and only
    Y carries the restatement note."""
    from faultmaven.core.investigation.causal_graph.support import (
        summarize_restatement_hold,
    )

    case, x, hyp = _case()
    y = case.causal_nodes[_Y]
    y.statement = _PROBLEM
    _supports(y, case, "y1", "y2")
    _supports(x, case, "x1", confidence=0.3)
    _deepen_to_y(case, hyp)
    with patch(_COUNTER) as counter:
        _settle(case)
    assert not counter.labels.called  # a held rung is no ROOT hold
    hold = summarize_restatement_hold(case)
    assert hold is not None and hold.root_ids == {_Y}
    assert hold.is_sole_root_block
    assert grade_cause_assurance(case) == _NO_ROOT
    block = _build_causal_graph_block(case)
    assert _line(block, _X).endswith(
        "[intermediate/inconclusive] pool connections leak until the pool is exhausted"
    )


# ---------------------------------------------------------------------------
# 6. Proof by exclusion validates only a node standing as a root
# ---------------------------------------------------------------------------


def _excluded_sibling(case) -> None:
    """X's one OR-alternative into D, refuted counterfactually."""
    sibling = _node("cn_0000000000b2", "the pooler restarts every minute")
    absence = _evidence("pooler-uptime", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE)
    case.evidence.append(absence)
    sibling.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=absence.evidence_id,
            stance=EvidenceStance.REFUTES,
            reasoning="the pooler has run for 9 days",
            linked_at_turn=2,
            stance_confidence=1.0,
        )
    )
    case.causal_nodes[sibling.node_id] = sibling
    case.causal_edges.append(
        CausalEdge(cause_node_id=sibling.node_id, effect_node_id=_D)
    )


@pytest.mark.parametrize("deepened", [True, False])
def test_proof_by_exclusion_skips_a_deepened_past_survivor_and_says_why(deepened):
    case, x, hyp = _case()
    _excluded_sibling(case)
    if deepened:
        _deepen_to_y(case, hyp)
    metadata: dict = {}
    _recompute_assessment_state(case, exclusion_survivors={_X}, metadata=metadata)
    _recompute_assessment_state(
        case, exclusion_survivors={_X}, metadata=metadata, final=True
    )
    notices = [n for n in metadata.get("system_notices", []) if _X in n]
    if deepened:
        assert x.node_state != NodeState.VALIDATED
        assert notices == [
            f"SYSTEM: deductive_validations named {_X}, which a chain was "
            f"deepened past: {_Y} sits above it, so it is a rung, and proof by "
            "exclusion validates only a root. Assert the root above it, or "
            "re-root onto it."
        ]
    else:
        assert (x.node_state, x.validation_method) == (
            NodeState.VALIDATED,
            ValidationMethod.DEDUCTIVE,
        )
        assert notices == []


# ---------------------------------------------------------------------------
# 9. A revised statement may not restate a cause on the graph (row 25)
# ---------------------------------------------------------------------------


def test_a_statement_revised_to_a_deepened_past_root_is_refused_as_a_cause():
    """The critique's S1: admitting it, then retiring the deepening
    hypothesis, would grade a root that is the problem statement. Row 25
    stays on the stored type; its refusal no longer calls the rung a root."""
    case, x, hyp = _case()
    _supports(x, case, "x1", "x2")
    _settle(case)
    _deepen_to_y(case, hyp)
    _settle(case)
    hyp.statement = "a config push shortened the connection lifetime"
    symptom = _evidence("sym", EvidenceCategory.SYMPTOM_EVIDENCE)
    case.evidence.append(symptom)
    refusal = revision_refusal(
        case, x.statement, [symptom.evidence_id], "the logs show the pool leaking", "k"
    )
    assert refusal == (
        "it restates a cause on the graph; the problem statement describes "
        "what is observed, never why"
    )


# ---------------------------------------------------------------------------
# 10. The label: only standing hypotheses confer position
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("end", [HypothesisState.REFUTED, HypothesisState.INCONCLUSIVE])
def test_a_node_below_the_root_of_a_chain_that_no_longer_stands_renders_as_a_root(end):
    """Declared model-visible change: on a refuted or inconclusive
    hypothesis's chain, the ROOT-typed node below its root renders
    ``[root/…]`` with its posited turn and the ROOT notes, as an orphan
    does; the hypothesis's own root renders ``root``."""
    case, x, hyp = _case()
    _supports(x, case, "x1")
    _deepen_to_y(case, hyp)
    _stop_standing(case, hyp, end)
    _settle(case)
    block = _build_causal_graph_block(case)
    assert "[root/inconclusive]" in _line(block, _X)
    assert "(posited turn 1)" in _line(block, _X)
    assert "SECOND INDEPENDENT" in _line(block, _X)
    assert f"{_Y} [root/candidate]" in block


def test_a_refuted_hypothesis_root_a_standing_chain_runs_through_is_a_rung():
    """The critique's S4: H1 (refuted) is rooted at X; standing H2's path
    runs through X. The predicate wins: X shows as the rung it is to the
    bar."""
    case, x, hyp = _case()
    rival = _second(case)
    attach_root(case, rival, _Y, [_Y, _X, _D], 6)
    _stop_standing(case, hyp, HypothesisState.REFUTED)
    _supports(x, case, "x1")
    _settle(case)
    block = _build_causal_graph_block(case)
    assert x.node_state == NodeState.VALIDATED
    assert all(
        "[intermediate/validated]" in line
        for line in block.splitlines()
        if line.strip().startswith(_X)
    )
    assert grade_cause_assurance(case) == _NO_ROOT


# ---------------------------------------------------------------------------
# Row 24: a conclusion's licence leg keeps the stored type (#2485)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("end", [HypothesisState.RETIRED, HypothesisState.REFUTED])
def test_a_conclusion_naming_a_deepened_past_root_licenses_no_remediation_of_it(end):
    """A conclusion names X while a chain is deepened past it; the fix is
    licensed then. The deepening hypothesis then stops standing, so X is an
    orphan root, and a fix run at it is an interception: a conclusion naming
    an orphan root licenses no remediation of it (#2485)."""
    case, x, hyp = _case()
    _supports(x, case, "x1", "x2")
    _settle(case)
    _deepen_to_y(case, hyp)
    _settle(case)
    licence, _in_words = licence_leg_ids(case, "rcc", (None, _X))
    _refute(case, case.causal_nodes[_Y], "y-ruled-out")
    _settle(case)
    _stop_standing(case, hyp, end)
    _settle(case)
    fix = ProposedAction(
        case_id=case.case_id,
        action_type=InvestigationActionType.SOLUTION,
        description="drain the pool",
        proposed_in_turn=5,
        accepted_in_turn=case.current_turn,
        state="accepted",
        target_ids=[_X],
        target_named=True,
        licence_ids=list(licence),
    )
    case.proposed_actions.append(fix)
    _settle_fix_attempts(
        case,
        {FIX_CAUSE_AT_START_KEY: fix_cause(case), FIXES_ACCEPTED_KEY: [fix.action_id]},
    )
    assert (fix.fix_kind, fix.target_ids) == (FIX_INTERCEPTION, [_X])


# ---------------------------------------------------------------------------
# Engine-driven: the fallen leg, the fix kind and a re-root, through the apply
# ---------------------------------------------------------------------------


def _deepening(below: str) -> dict:
    """The fields of an update deepening the hypothesis past ``below``."""
    return dict(
        causal_nodes_to_add=[
            CausalNodeToAdd(
                statement="pgbouncer runs in session mode",
                node_type="root",
                produces=below,
            )
        ],
    )


async def test_an_unkinded_fix_seen_failing_as_its_cause_is_deepened_past_fails():
    """r2928 probe4 (row 18): the fallen leg stays on the stored type, so the
    window of an unkinded fix of A, seen failing in the very turn the chain is
    deepened past A, reads FAILED where M6 reads it, as on main (#2042)."""
    from faultmaven.core.investigation.milestone_engine import cause_state

    case, a, hyp = _identified_case()
    eng = _engine()
    await _record(
        eng,
        case,
        6,
        solutions_to_add=[_fix("raise max_connections to 100")],
        milestones=_ran(),
    )
    _attempts(case)[-1].fix_kind = None  # a fix recorded before kinds
    seen: list = []
    demote = cause_state.demote_disconfirmed_cause_via_evidence

    def reading_windows(case, **kwargs):
        seen.append({w.fix_turn: w.state for w in fix_windows(case)})
        return demote(case, **kwargs)

    with patch.object(
        cause_state, "demote_disconfirmed_cause_via_evidence", reading_windows
    ):
        await _record(
            eng,
            case,
            7,
            _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
            _obs(_PRESENT, "checkout still returns 503", VERIFYING_ID),
            **_deepening(a.node_id),
            hypotheses_to_update=[
                HypothesisUpdate(
                    hypothesis_id=hyp.hypothesis_id, root_node_ref="new_index_0"
                )
            ],
        )
    assert hyp.root_node_id != a.node_id and a.node_id in hyp.path
    assert seen and seen[0][6] == FIX_FAILED


async def test_a_fix_run_as_its_root_is_deepened_past_keeps_its_remediation():
    """Row 23: the fix kind reads the cause recorded at the apply's start; a
    deepening made in the same apply cannot reclassify it."""
    case, a, hyp = _identified_case()
    eng = _engine()
    await _record(eng, case, 6, solutions_to_add=[_fix("raise max_connections to 100")])
    pending = case.proposed_actions[-1]
    assert pending.licence_ids == [a.node_id]
    await _record(
        eng,
        case,
        7,
        **_deepening(a.node_id),
        hypotheses_to_update=[
            HypothesisUpdate(
                hypothesis_id=hyp.hypothesis_id, root_node_ref="new_index_0"
            )
        ],
        milestones=_ran(pending.action_id),
    )
    assert a.node_id in hyp.path[1:]
    assert (pending.fix_kind, pending.target_ids) == (FIX_REMEDIATION, [a.node_id])


async def test_a_fix_named_at_a_root_run_after_it_was_deepened_past_intercepts():
    """Row 23: a fix named at A, run once a turn has deepened past A, acts on
    a rung: an interception, read from the run turn's record."""
    case, a, hyp = _identified_case()
    eng = _engine()
    await _record(
        eng, case, 6, solutions_to_add=[_fix("raise max_connections to 100", a.node_id)]
    )
    pending = case.proposed_actions[-1]
    await _deepen(eng, case, hyp, 7)
    await _record(eng, case, 8, milestones=_ran(pending.action_id))
    assert (pending.fix_kind, pending.target_ids) == (FIX_INTERCEPTION, [a.node_id])


async def test_a_fix_run_as_its_root_is_re_rooted_onto_reads_the_turn_start():
    """Row 23: the deepened ids are captured with the turn's cause at the
    apply's start (the one record a backdated fix reads too). Re-rooting the
    hypothesis back onto A in the same apply puts A back as a root before the
    fix is kinded, and the fix still acts on the rung it was run against.
    (A retirement cannot land inside the apply: the user's retire is applied
    before it, and a refutation after the fixes are kinded.)"""
    case, a, hyp = _identified_case()
    eng = _engine()
    await _record(
        eng, case, 6, solutions_to_add=[_fix("raise max_connections to 100", a.node_id)]
    )
    pending = case.proposed_actions[-1]
    await _deepen(eng, case, hyp, 7)
    await _record(
        eng,
        case,
        8,
        hypotheses_to_update=[
            HypothesisUpdate(hypothesis_id=hyp.hypothesis_id, root_node_ref=a.node_id)
        ],
        milestones=_ran(pending.action_id),
    )
    assert hyp.root_node_id == a.node_id
    assert (pending.fix_kind, pending.target_ids) == (FIX_INTERCEPTION, [a.node_id])


async def test_a_re_root_onto_a_rung_validated_node_with_a_verdict_identifies_nothing():
    """The critique's S2, through the real apply: in one update H1 re-roots
    onto the rung-validated, restating X with a terminal verdict, a
    conclusion naming X and a fix. Between the attach and the derive (where
    M6 reads the case) no reader sees a validated or identified root; the
    turn ends as the never-marked shape does on main: X held, nothing
    identified, no engine refutation, the fix licensed on the conclusion's
    own leg."""
    from faultmaven.core.investigation.causal_graph.projection import (
        any_chain_root_validated,
        held_open_root_ids,
        identified_root_ids,
    )
    from faultmaven.core.investigation.milestone_engine import cause_state
    from faultmaven.core.investigation.schemas import RootCauseConclusionUpdate
    from faultmaven.modules.case.contracts import CauseState, ProblemStatus

    case, x, hyp = _rung_validated_restating_x()
    case.progress.problem_status = ProblemStatus.VERIFIED
    seen: list = []
    demote = cause_state.demote_disconfirmed_cause_via_evidence

    def reading_roots(case, **kwargs):
        seen.append(
            (
                x.is_identified_root,
                identified_root_ids(case),
                any_chain_root_validated(case),
                held_open_root_ids(case),
                _validated_roots(case),
                _pool_legs(case),
            )
        )
        return demote(case, **kwargs)

    with patch.object(
        cause_state, "demote_disconfirmed_cause_via_evidence", reading_roots
    ):
        await _record(
            _engine(),
            case,
            7,
            hypotheses_to_update=[
                HypothesisUpdate(
                    hypothesis_id=hyp.hypothesis_id,
                    root_node_ref=_X,
                    root_is_terminal=True,
                    terminal_basis_evidence_ids=[_eid("x1")],
                )
            ],
            root_cause_conclusion=RootCauseConclusionUpdate(
                root_cause=x.statement,
                mechanism="the pool leaks",
                evidence_ids=[_eid("x1")],
                likelihood=0.9,
                names_root_node_id=_X,
            ),
            solutions_to_add=[_fix("drain the pool")],
        )
    assert hyp.root_node_id == _X and x.root_terminal is True
    assert seen and seen[0] == (False, set(), False, set(), [], (set(), set(), set()))
    assert x.node_state == NodeState.INCONCLUSIVE
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert grade_cause_assurance(case) == _NO_ROOT
    assert all(e.collected_by == "llm" for e in case.evidence)
    assert [a.licence_ids for a in case.proposed_actions if a.licence_ids] == [[_X]]


def _backdated(action_id: str) -> MilestoneUpdates:
    return MilestoneUpdates(
        solution_accepted=True,
        accepted_action_ref=action_id,
        accepted_fix_reported_in="previous_message",
    )


@pytest.mark.parametrize("named_at_rung", [False, True])
async def test_a_fix_backdated_onto_a_record_without_deepened_ids_is_kinded_as_on_main(
    named_at_rung,
):
    """A turn record written before ``deepened_ids`` existed reads as none
    deepened past: a fix backdated onto it is kinded exactly as one onto a
    current record (r2930 p_legacy), never left unkinded, which would read it
    as a remediation."""

    async def kinded(legacy: bool):
        case, a, _hyp = _identified_case()
        eng = _engine()
        ref = None
        if named_at_rung:
            ref = _intermediate(case)
            case.causal_edges.append(
                CausalEdge(cause_node_id=a.node_id, effect_node_id=ref)
            )
        await _record(
            eng, case, 6, solutions_to_add=[_fix("restart the checkout pods", ref)]
        )
        await _record(eng, case, 7)
        if legacy:
            record = case.turn_history[-1]
            case.turn_history[-1] = record.model_copy(
                update={
                    "cause_at_start": record.cause_at_start.model_copy(
                        update={"deepened_ids": None}
                    )
                }
            )
        await _record(
            eng, case, 8, milestones=_backdated(case.proposed_actions[0].action_id)
        )
        (fix,) = _attempts(case)
        return fix.accepted_in_turn, fix.fix_kind, fix.target_ids

    current = await kinded(legacy=False)
    assert await kinded(legacy=True) == current
    assert current[1] == (FIX_INTERCEPTION if named_at_rung else FIX_REMEDIATION)


def test_the_exclusion_notice_is_not_given_for_a_survivor_the_final_recompute_validates():
    """r2930 p_excl: step 1 skips X, deepened past; housekeeping then retires
    the deepening hypothesis, and the final recompute validates X by
    exclusion. No notice says X was skipped."""
    case, x, hyp = _case()
    _excluded_sibling(case)
    _deepen_to_y(case, hyp)
    metadata: dict = {}
    _recompute_assessment_state(case, exclusion_survivors={_X}, metadata=metadata)
    assert x.node_state != NodeState.VALIDATED
    _stop_standing(case, hyp, HypothesisState.RETIRED)
    _recompute_assessment_state(
        case, exclusion_survivors={_X}, metadata=metadata, final=True
    )
    assert (x.node_state, x.validation_method) == (
        NodeState.VALIDATED,
        ValidationMethod.DEDUCTIVE,
    )
    assert not [n for n in metadata.get("system_notices", []) if _X in n]


def test_a_hypothesis_whose_path_has_no_root_yet_is_headed_by_its_first_node():
    """A standing hypothesis with a path and no root node: its path's first
    node is its head, a root to the bar and the label, never a rung."""
    case, x, hyp = _case()
    hyp.root_node_id = None
    _supports(x, case, "x1")
    _settle(case)
    assert x.node_state == NodeState.INCONCLUSIVE
    assert "SECOND INDEPENDENT" in _line(_build_causal_graph_block(case), _X)


def test_m6_reads_no_failed_fix_of_a_root_validated_as_a_rung():
    """M6's record arm (``failed_fix_of``) on a hypothesis re-rooted onto a
    node validated as a rung, before the derive judges it as a root: the
    latest fix window failed and binds the node, yet nothing is read as a
    failed fix of an identified cause."""
    case, a, hyp = _identified_case()
    problem = next(
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    above = _node(_Y, "a chart upgrade switched pgbouncer to session mode")
    case.causal_nodes[_Y] = above
    case.causal_edges.append(CausalEdge(cause_node_id=_Y, effect_node_id=a.node_id))
    attach_root(case, hyp, _Y, [_Y, a.node_id, problem.node_id], 6)
    _refute(case, a, "a-ruled-out-1")
    _refute(case, a, "a-ruled-out-2")
    _settle(case)  # a support/refute tie: A leaves VALIDATED
    _supports(a, case, "a-again")
    _settle(case)  # A re-enters VALIDATED as a rung
    attach_root(case, hyp, a.node_id, [a.node_id, problem.node_id], 7)
    _m6_failed_fix(case)
    case.current_turn = 9
    assert a.node_state == NodeState.VALIDATED
    assert latest_decided_fix_window(case).state == FIX_FAILED
    assert failed_fix_of(case, hyp) is None
