"""Real cases whose outcome text ``main`` composed, for the golden table (#2636).

Each scenario builds a case through the test builders and runs the decider
that composes its text. ``main``'s output was captured once, by running these
scenarios on ``main`` at ``056423e33`` (``capture_main``), into
``golden_main_2636.json``. ``test_narration_golden_2636.py`` runs the same
scenarios on this tree, through the record and the narration step, and
compares. Only APIs that exist on both trees are used here.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_assessment_state,
)
from faultmaven.core.investigation.milestone_engine.terminal_proposals import (
    _maybe_propose_confirmed_resolution,
    _maybe_propose_deferred_close,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    EvidenceCategory,
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
    SolutionFeasible,
    ValidationMethod,
)
from tests.unit.core.investigation.bound_rows import _row as bound_row
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _eid,
    _failed_fix,
    _identified_case,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _row as m6_row,
)

GOLDEN = Path(__file__).with_name("golden_main_2636.json")

#: The prose keys a scenario's offer publishes.
PROSE = (
    "deferred_solution_gate_message",
    "resolution_ready_gate_message",
    "resolution_needs_info_message",
)


def _pending_fix(case, turn=5):
    case.proposed_actions.append(
        ProposedAction(
            action_id="act_00000000f1f1",
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="raise max_connections to 100",
            proposed_in_turn=turn,
        )
    )


def _second_validated_root(case):
    """A second hypothesis whose root two causal supports validate, its why
    open (no terminal verdict)."""
    supports = [
        m6_row(
            label,
            EvidenceCategory.CAUSAL_EVIDENCE,
            turn,
            summary,
        )
        for label, turn, summary in (
            ("pgb_log", 3, "pgbouncer log: pool checkout is full, clients wait"),
            ("pgb_metric", 4, "pgbouncer SHOW POOLS: cl_waiting 40 for checkout"),
        )
    ]
    case.evidence += supports
    root = CausalNode(
        node_id="cn_00000000b0b0",
        statement="pgbouncer's pool for checkout is sized at 20",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=row.evidence_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on the root",
                linked_at_turn=4,
            )
            for row in supports
        ],
        generated_at_turn=1,
    )
    case.causal_nodes[root.node_id] = root
    problem = next(e.effect_node_id for e in case.causal_edges)
    case.causal_edges.append(
        CausalEdge(cause_node_id=root.node_id, effect_node_id=problem)
    )
    case.hypotheses["hyp_00000000b0b0"] = Hypothesis(
        hypothesis_id="hyp_00000000b0b0",
        statement="the pgbouncer pool starves checkout",
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=root.node_id,
        generated_at_turn=1,
    )


def withdrawn_held_open_beside_a_retired_validated_root():
    """Review item 2's case: the identified root's hypothesis is retired, its
    root stays VALIDATED; another standing root validates with its why open.
    The pending fix is withdrawn: its variant is the held-open one."""
    case, _root, hyp = _identified_case()
    _second_validated_root(case)
    hyp.state = HypothesisState.RETIRED
    _pending_fix(case)
    metadata: dict = {"milestones_completed": []}
    _recompute_assessment_state(case, metadata=metadata)
    return case, metadata


def withdrawn_once_the_cause_fell():
    case, _root, hyp = _identified_case()
    _pending_fix(case)
    hyp.state = HypothesisState.REFUTED
    metadata: dict = {"milestones_completed": []}
    _recompute_assessment_state(case, metadata=metadata)
    return case, metadata


def absence_inside_a_failed_window():
    case, _root, _hyp = _identified_case()
    _failed_fix(case)
    case.current_turn = 10
    bound_row(
        case,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        turn=10,
        evidence_id=_eid("quiet_10"),
        summary="checkout quiet after the restart",
    )
    metadata: dict = {"milestones_completed": []}
    _recompute_assessment_state(case, metadata=metadata)
    return case, metadata


def an_unnamed_recheck_after_the_fix():
    case, _root, _hyp = _identified_case()
    case.proposed_actions.append(
        ProposedAction(
            action_id="act_00000000f1f2",
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="raise max_connections to 100",
            proposed_in_turn=5,
            accepted_in_turn=6,
            state="accepted",
        )
    )
    case.progress.solution_accepted = True
    case.current_turn = 7
    bound_row(
        case,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        turn=7,
        evidence_id=_eid("unnamed_7"),
        summary="checkout answers 200",
        rechecks=None,
    )
    metadata: dict = {"milestones_completed": []}
    _recompute_assessment_state(case, metadata=metadata)
    return case, metadata


def the_backstop_offers_a_confirmed_case():
    from tests.unit.core.investigation.test_resolution_backstop_turn import (
        _confirmed_case,
    )

    case = _confirmed_case()
    metadata: dict = {}
    _maybe_propose_confirmed_resolution(case, metadata)
    return case, metadata


def the_backstop_held_by_a_pending_check():
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _held_case,
    )

    case = _held_case()
    case.evidence_needs[-1].need_id = "eneed_00000000c0c0"  # a stable id
    metadata: dict = {}
    _maybe_propose_confirmed_resolution(case, metadata)
    return case, metadata


def a_deferred_fix_on_a_confirmed_case():
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _held_case,
    )

    case = _held_case(asked=False)
    case.progress.solution_feasible = SolutionFeasible.DEFERRED
    case.progress.solution_proposed = True
    metadata: dict = {}
    _maybe_propose_deferred_close(case, metadata)
    return case, metadata


def the_models_resolved_on_an_unconfirmed_case():
    from faultmaven.core.investigation.milestone_engine.engine import (
        MilestoneEngine,
    )
    from tests.unit.core.investigation.test_resolution_backstop_turn import (
        _make_repo,
    )

    case, _root, _hyp = _identified_case()
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    response = MagicMock()
    response.state_updates.proposed_transition = MagicMock(
        to_state="resolved", evidence_ids=[]
    )
    metadata: dict = {"response_obj": response}
    asyncio.run(
        engine.transitions.check_automatic_transitions(case, metadata, "it works")
    )
    metadata.pop("response_obj")
    return case, metadata


SCENARIOS = {
    fn.__name__: fn
    for fn in (
        withdrawn_held_open_beside_a_retired_validated_root,
        withdrawn_once_the_cause_fell,
        absence_inside_a_failed_window,
        an_unnamed_recheck_after_the_fix,
        the_backstop_offers_a_confirmed_case,
        the_backstop_held_by_a_pending_check,
        a_deferred_fix_on_a_confirmed_case,
        the_models_resolved_on_an_unconfirmed_case,
    )
}


def text_of(case, metadata: dict) -> dict:
    """What the scenario's decider composed: the notices, the prose keys and
    the open offer's summary."""
    offer = case.offer_record.open
    return {
        "notices": list(metadata.get("system_notices") or []),
        "prose": {key: metadata[key] for key in PROSE if key in metadata},
        "summary": offer.summary if offer is not None else None,
    }


def capture_main() -> None:  # pragma: no cover - run once, on main
    GOLDEN.write_text(
        json.dumps(
            {name: text_of(*build()) for name, build in SCENARIOS.items()},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
