"""Real cases whose outcome text ``main`` composed, for the golden table (#2636).

Each scenario builds a case through the test builders and runs a decider that
composes outcome text: ``assess_resolution_readiness`` through each
``ResolutionReason``, ``assess_closure_readiness`` and ``assess_close_summary``,
the resolve-declined actions (the step-2 refusal and the prompt line) and the
pending-check feedback (the backstop's and step 2's). Only APIs that exist on
``main`` and on this tree are used, so the same scenarios run on both.

``main``'s output was captured by running these scenarios on ``main``
(``1f7095a7c``) into ``golden_main_2636.json``, from a scratch worktree of it
holding this module::

    git worktree add --detach <scratch> 1f7095a7c
    cp tests/unit/core/investigation/golden_cases_2636.py \\
        <scratch>/tests/unit/core/investigation/
    cd <scratch> && python -c "from tests.unit.core.investigation import \\
        golden_cases_2636 as g; g.capture_main()"

``capture_main`` refuses any other tree: one whose ``faultmaven`` is not its
own, or one holding ``narration/codes.py``, where the golden would compare
the code with itself. ``test_golden_composers_2636.py`` runs them on this
tree and compares.

B1b (#2636) adds ``CYCLE_SCENARIOS``: each runs a decider whose notice B1b
renders after the cycle's record (the withdrawal, the mitigation verify and
disposition refusals, the refutation refusal) and returns ``(case,
metadata)``. On ``main`` the decider composed the text into
``system_notices``; on this tree the narration step renders it
(``test_turn_reorder_2636.py``). Ids are normalised (``cycle_text``). They
were captured on ``main`` (``b1969f821``) the same way, into the same file::

    git worktree add --detach <scratch> b1969f821
    cp tests/unit/core/investigation/golden_cases_2636.py \\
        <scratch>/tests/unit/core/investigation/
    cd <scratch> && python -c "from tests.unit.core.investigation import \\
        golden_cases_2636 as g; g.capture_main()"

Re-capturing leaves every B1a scenario's entry unchanged.
"""

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_assessment_state,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _apply_stage_gate_signals,
)
from faultmaven.core.investigation.milestone_engine.terminal_proposals import (
    _maybe_propose_confirmed_resolution,
)
from faultmaven.core.investigation.milestone_engine.transitions import (
    declined_resolution_reask,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    _resolve_declined_emphasis,
)
from faultmaven.core.investigation.schemas import MilestoneUpdates
from faultmaven.core.investigation.terminal_transitions import (
    assess_close_summary,
    assess_closure_readiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    CauseState,
    ConfidenceLevel,
    EvidenceCategory,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InvestigationActionType,
    MitigationRecord,
    NeedState,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemStatus,
    ProposedAction,
    RootCauseConclusion,
    Solution,
    SolutionType,
    ValidationMethod,
)
from tests.case_builders import investigating_case, with_declined_offer
from tests.unit.core.investigation.bound_rows import _row, bound_row
from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
    _bare_case,
    _cause_leg_only,
    _eid,
    _failed_fix,
    _identified_case,
    _interception_only,
    _invalidated,
    _no_symptom_row,
    _relief_only,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _ROOT_ID,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _identified_case as _m6_identified_case,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _row as _m6_row,
)
from tests.unit.core.investigation.test_mitigation_record_2100 import (
    _pending as _mitigation_pending,
)
from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
    _held_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _DSU,
    _link,
    _meta,
    _on_record,
    _refute,
    _Response,
    _standing,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _case as _refutation_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _engine as _apply_engine,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _confirmed_case,
    _make_repo,
)

GOLDEN = Path(__file__).with_name("golden_main_2636.json")

_NEED_ID = "eneed_00000000c0c0"


def _readiness(case) -> dict:
    r = assess_resolution_readiness(case)
    return {
        "verdict": r.verdict,
        "reason": str(r.reason),
        "missing": list(r.missing),
        "gaps": [str(g) for g in r.confirmation_gaps],
        "message": r.message,
    }


def _closure(readiness) -> dict:
    return {"verdict": readiness.verdict, "message": readiness.message}


def _held(*, asked: bool = True, fulfilled: bool = False):
    """``_held_case`` with a stable need id; ``fulfilled``: the need marked
    FULFILLED on the row that showed the problem, from before the ask."""
    case = _held_case(asked=asked)
    if asked:
        need = case.evidence_needs[-1]
        need.need_id = _NEED_ID
        if fulfilled:
            need.state = NeedState.FULFILLED
            need.fulfilling_evidence_ids = list(
                case.problem_verification.verifying_evidence_ids
            )
    return case


def _with_evidence(case):
    _row(
        case,
        EvidenceCategory.SYMPTOM_EVIDENCE,
        turn=2,
        evidence_id=_eid("shown"),
        summary="checkout 503s",
    )
    return case


def _relief_unnamed():
    """The relief window's fix names a record id, so the ask is generic."""
    case = _relief_only()
    case.proposed_actions[-1].description = "apply act_0123456789ab"
    return case


def _relief_clipped_empty(statement: str, fix: str):
    """The relief window's names exist but clip to nothing: ``main`` picks the
    named ask on their presence, not on their clipped text."""
    case = _relief_only()
    case.causal_nodes[_ROOT_ID].statement = statement
    case.proposed_actions[-1].description = fix
    return case


def _with_solutions(case, *titles):
    """Solutions titled ``titles``; an empty title is a stored one without
    (the display falls back to "Solution N")."""
    for i, title in enumerate(titles):
        solution = Solution(
            solution_id=f"sol_00000000000{i}",
            solution_type=SolutionType.CONFIG_CHANGE,
            title=title or "untitled",
            longterm_fix="set max_connections to 100",
        )
        case.solutions.append(solution.model_copy(update={"title": title}))
    return case


def _chain_without_conclusion():
    """The chain identifies the cause; no conclusion names it."""
    case = _bare_case()
    case.progress.cause_state = CauseState.IDENTIFIED
    case.root_cause_conclusion = None
    return case


def _no_cause_leg_no_solutions():
    """A confirmed case with no cause-identification leg and no solution."""
    case = _held(asked=False)
    case.solutions = []
    case.progress.cause_state = CauseState.UNKNOWN
    case.root_cause_conclusion = None
    case.working_conclusion = None
    return case


def _concluded(case):
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="max_connections is 20",
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
        mechanism="the pool waits on a full server",
    )
    return case


def _declined(case, *, on_hold: bool = False):
    with_declined_offer(case, kind="resolve", basis="backstop")
    if on_hold:
        case.progress.problem_status = ProblemStatus.REVISION_PENDING
    return case


def _declined_texts(case) -> dict:
    return {
        "reask": declined_resolution_reask(case),
        "prompt_line": _resolve_declined_emphasis(case),
    }


def _notices(metadata: dict) -> dict:
    return {
        "notices": list(metadata.get("system_notices") or []),
        "needs_info": metadata.get("resolution_needs_info_message"),
    }


def _backstop(case) -> dict:
    metadata: dict = {}
    _maybe_propose_confirmed_resolution(case, metadata)
    return _notices(metadata)


def _models_resolved(case) -> dict:
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    response = MagicMock()
    response.state_updates.proposed_transition = MagicMock(
        to_state="resolved", evidence_ids=[]
    )
    metadata: dict = {"response_obj": response}
    asyncio.run(
        engine.transitions.check_automatic_transitions(case, metadata, "resolve it")
    )
    return _notices(metadata)


def _pending(feedback, *, declined: bool, fulfilled: bool) -> dict:
    case = _held(fulfilled=fulfilled)
    if declined:
        _declined(case)
    return feedback(case)


SCENARIOS = {
    # assess_resolution_readiness, one per ResolutionReason and confirmation gap
    "readiness_false_alarm": lambda: _readiness(_invalidated()),
    "readiness_pending_check": lambda: _readiness(_held()),
    "readiness_confirmed": lambda: _readiness(_held(asked=False)),
    "readiness_nothing_on_record": lambda: _readiness(_bare_case()),
    "readiness_fix_off_cause": lambda: _readiness(_interception_only()),
    "readiness_fix_failed": lambda: _readiness(_failed_fix()),
    "readiness_gaps_no_cause_no_fix": lambda: _readiness(_with_evidence(_bare_case())),
    "readiness_gaps_identified": lambda: _readiness(_identified_case()[0]),
    "readiness_gaps_relief_named": lambda: _readiness(_relief_only()),
    "readiness_gaps_relief_unnamed": lambda: _readiness(_relief_unnamed()),
    "readiness_gaps_relief_names_clip_empty": lambda: _readiness(
        _relief_clipped_empty(".", "...")
    ),
    "readiness_gaps_relief_cause_clips_empty": lambda: _readiness(
        _relief_clipped_empty(".", "raise max_connections to 100")
    ),
    "readiness_gaps_relief_fix_clips_empty": lambda: _readiness(
        _relief_clipped_empty("max_connections is set to 20", "...")
    ),
    "readiness_gaps_cause_leg": lambda: _readiness(_cause_leg_only()),
    "readiness_gaps_no_symptom_row": lambda: _readiness(
        _no_symptom_row(_identified_case()[0])
    ),
    "readiness_gaps_pair": lambda: _readiness(_no_symptom_row(_cause_leg_only())),
    # assess_closure_readiness: the pivot, with and without a named cause and
    # solutions, and the summary it answers with on an unconfirmed case
    "closure_pivot": lambda: _closure(assess_closure_readiness(_held(asked=False))),
    "closure_pivot_no_cause_leg_no_solutions": lambda: _closure(
        assess_closure_readiness(_no_cause_leg_no_solutions())
    ),
    "closure_pivot_named_with_solutions": lambda: _closure(
        assess_closure_readiness(
            _with_solutions(_concluded(_confirmed_case()), "raise the limit", "")
        )
    ),
    "closure_unconfirmed": lambda: _closure(
        assess_closure_readiness(_identified_case()[0])
    ),
    # assess_close_summary
    "summary_trivial": lambda: _closure(assess_close_summary(investigating_case())),
    "summary_one_row": lambda: _closure(
        assess_close_summary(_with_evidence(_bare_case()))
    ),
    "summary_named_cause_with_solutions": lambda: _closure(
        assess_close_summary(
            _with_solutions(_concluded(_identified_case()[0]), "", "raise the limit")
        )
    ),
    "summary_unnamed_cause": lambda: _closure(
        assess_close_summary(_chain_without_conclusion())
    ),
    "summary_on_a_confirmed_case": lambda: _closure(
        assess_close_summary(_confirmed_case())
    ),
    # the resolve-declined actions: both chips, held by a check, on hold
    "declined_both_chips": lambda: _declined_texts(_declined(_held(asked=False))),
    "declined_held_by_a_check": lambda: _declined_texts(_declined(_held())),
    "declined_on_hold": lambda: _declined_texts(
        _declined(_confirmed_case(), on_hold=True)
    ),
}
# the pending-check feedback: the backstop's and step 2's, with a decline
# standing or not, the need pending or FULFILLED on a row from before the ask
for _who, _feedback in (("backstop", _backstop), ("step2", _models_resolved)):
    for _declined_stands in (False, True):
        for _fulfilled in (False, True):
            SCENARIOS[
                f"pending_check_{_who}"
                f"{'_declined' if _declined_stands else ''}"
                f"{'_fulfilled' if _fulfilled else ''}"
            ] = lambda f=_feedback, d=_declined_stands, ff=_fulfilled: _pending(
                f, declined=d, fulfilled=ff
            )


# ---------------------------------------------------------------------------
# B1b: the deciders whose notices are rendered after the cycle's record
# ---------------------------------------------------------------------------

_ID = re.compile(r"\b(ev|hyp|act|cn)_[0-9a-f]{12}\b")


def cycle_text(notices) -> list[str]:
    """The notices with each id replaced by its kind and the order it first
    appears in, so a random id compares equal across trees."""
    seen: dict[str, str] = {}

    def _name(match) -> str:
        return seen.setdefault(match.group(0), f"<{match.group(1)}:{len(seen)}>")

    return [_ID.sub(_name, n) for n in notices]


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
        _m6_row(label, EvidenceCategory.CAUSAL_EVIDENCE, turn, summary)
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
    """The identified root's hypothesis is retired, its root stays VALIDATED;
    another standing root validates with its why open. The pending fix is
    withdrawn: its variant is the held-open one."""
    case, _root, hyp = _m6_identified_case()
    _second_validated_root(case)
    hyp.state = HypothesisState.RETIRED
    _pending_fix(case)
    metadata: dict = {"milestones_completed": []}
    _recompute_assessment_state(case, metadata=metadata)
    return case, metadata


def withdrawn_once_the_cause_fell():
    case, _root, hyp = _m6_identified_case()
    _pending_fix(case)
    hyp.state = HypothesisState.REFUTED
    metadata: dict = {"milestones_completed": []}
    _recompute_assessment_state(case, metadata=metadata)
    return case, metadata


def _signals(case, turn: int, metadata: dict | None = None, **milestones):
    """The stage gates' mitigation step on ``milestones`` at ``turn``."""
    case.current_turn = turn
    metadata = {"milestones_completed": []} if metadata is None else metadata
    _apply_stage_gate_signals(case, MilestoneUpdates(**milestones), "", metadata)
    return case, metadata


def _accepted_mitigation():
    """A verified problem, a workaround proposed at 5 and accepted at 6, the
    row that showed the problem on record."""
    case, shown = _mitigation_pending()
    _signals(case, 6, mitigation_accepted=True)
    return case, shown


def mitigation_proceeded():
    case, _shown = _accepted_mitigation()
    _signals(case, 7, mitigation_disposition="proceed_unmitigated")
    return _signals(case, 8, mitigation_verified=True)


def mitigation_no_bound_relief():
    """A relief naming nothing it re-checks: no bound relief, settle named."""
    case, _shown = _accepted_mitigation()
    _row(
        case,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        turn=8,
        evidence_id="ev_00000000a808",
        summary="orders answer in 200ms",
    )
    return _signals(case, 8, mitigation_verified=True)


def mitigation_no_bound_relief_seen_present():
    """Relieved at 7, seen present again at 8: the line names the turn."""
    case, shown = _accepted_mitigation()
    bound_row(case, EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, turn=7, rechecks=shown)
    bound_row(case, EvidenceCategory.SYMPTOM_EVIDENCE, turn=8, rechecks=shown)
    return _signals(case, 8, mitigation_verified=True)


def mitigation_no_bound_relief_declared_stable():
    """The same response declares it stable: the line names no settle."""
    case, _shown = _accepted_mitigation()
    return _signals(
        case, 7, mitigation_verified=True, mitigation_disposition="declared_stable"
    )


def mitigation_no_bound_relief_beside_a_proceed():
    """#2603: the same response proceeds; ``main`` still asks for a re-check."""
    case, _shown = _accepted_mitigation()
    return _signals(
        case,
        7,
        mitigation_verified=True,
        mitigation_disposition="proceed_unmitigated",
    )


def mitigation_refused_twice_in_one_cycle():
    """Two applications in one cycle (a replayed bundle's, then the live
    response's) refuse the verify; the first records declared_stable (A9)."""
    case, _shown = _accepted_mitigation()
    case, metadata = _signals(
        case, 7, mitigation_verified=True, mitigation_disposition="declared_stable"
    )
    return _signals(case, 7, metadata, mitigation_verified=True)


def mitigation_undated():
    case, _shown = _accepted_mitigation()
    (attempt,) = [a for a in case.proposed_actions if a.state == "accepted"]
    attempt.accepted_in_turn = None
    return _signals(case, 8, mitigation_verified=True)


def mitigation_revision_pending():
    case, _shown = _accepted_mitigation()
    case.progress.problem_status = ProblemStatus.REVISION_PENDING
    return _signals(case, 8, mitigation_verified=True)


def mitigation_problem_unverified():
    case, _shown = _accepted_mitigation()
    case.progress.problem_status = ProblemStatus.UNVERIFIED
    return _signals(case, 8, mitigation_verified=True)


def disposition_not_accepted():
    case, _shown = _mitigation_pending()
    return _signals(case, 6, mitigation_disposition="declared_stable")


def disposition_carries():
    case, _shown = _accepted_mitigation()
    _signals(case, 7, mitigation_disposition="declared_stable")
    return _signals(case, 8, mitigation_disposition="proceed_unmitigated")


def disposition_verified():
    case, _shown = _accepted_mitigation()
    case.progress.mitigation = MitigationRecord(
        **{**case.progress.mitigation.model_dump(), "verified": True}
    )
    return _signals(case, 8, mitigation_disposition="proceed_unmitigated")


def _refuted(likelihood=0.4, confidence=None):
    """The model refutes a competitor on a reason, with or without a REFUTES
    link it hedges at ``confidence``."""
    case = _refutation_case()
    h_id = _standing(case, likelihood=likelihood)
    links = []
    if confidence is not None:
        row = _on_record(case, "gateway rejects with token is not valid yet")
        links = [_link(h_id, row, confidence=confidence)]
    metadata = _meta()
    asyncio.run(
        _apply_engine().responses._apply_investigation_updates(
            case,
            _DSU(hypothesis_evidence_links=links, hypotheses_to_update=[_refute(h_id)]),
            metadata,
            _Response(),
        )
    )
    return case, metadata


CYCLE_SCENARIOS = {
    fn.__name__: fn
    for fn in (
        withdrawn_held_open_beside_a_retired_validated_root,
        withdrawn_once_the_cause_fell,
        mitigation_proceeded,
        mitigation_no_bound_relief,
        mitigation_no_bound_relief_seen_present,
        mitigation_no_bound_relief_declared_stable,
        mitigation_no_bound_relief_beside_a_proceed,
        mitigation_refused_twice_in_one_cycle,
        mitigation_undated,
        mitigation_revision_pending,
        mitigation_problem_unverified,
        disposition_not_accepted,
        disposition_carries,
        disposition_verified,
    )
}
CYCLE_SCENARIOS["refutation_refused_open"] = lambda: _refuted()
CYCLE_SCENARIOS["refutation_refused_self_hedged"] = lambda: _refuted(confidence=0.4)
CYCLE_SCENARIOS["refutation_refused_retired_by_the_link_pass"] = lambda: _refuted(
    likelihood=0.2, confidence=0.4
)


def capture_main() -> None:  # pragma: no cover - run once, on main
    import faultmaven

    root = Path(__file__).resolve().parents[4]
    assert (
        Path(faultmaven.__file__).resolve().is_relative_to(root)
    ), f"faultmaven imported from {faultmaven.__file__}, not {root}"
    # Only a tree without the decision log is main's: on this branch the
    # golden would compare the code with itself.
    extracted = root / "faultmaven/core/investigation/narration/codes.py"
    assert not extracted.exists(), f"{extracted} exists: capture on main only"
    print(f"faultmaven: {faultmaven.__file__}")
    captured = {name: build() for name, build in SCENARIOS.items()}
    for name, build in CYCLE_SCENARIOS.items():
        _case, metadata = build()
        captured[f"cycle:{name}"] = cycle_text(metadata.get("system_notices") or [])
    GOLDEN.write_text(json.dumps(captured, indent=2, ensure_ascii=False) + "\n")
    print(f"captured {len(SCENARIOS)} + {len(CYCLE_SCENARIOS)} scenarios into {GOLDEN}")
