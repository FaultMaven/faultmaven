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
own, one that is not a checkout's top level, a commit other than
``MAIN_AT_B1C``, a modified ``faultmaven/``, or one whose ``NoticeCode`` holds
B1c's members, where the golden would compare the code with itself.
``test_golden_composers_2636.py`` runs them on this tree and compares.

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

B1c (#2636) adds ``B1C_SCENARIOS``: the remaining deciders whose notices
``main`` composed as text (the failed-window absence, the unbound re-checks,
the failed fix, the licence refusals, the ignored fix report, the untargeted
fix, the signals on an invalidated problem, the false-alarm and revision
refusals, the root-terminal refusals, the pending check and the declined
re-asks), each returning ``(case, metadata)``. ``capture_main`` adds their
texts, as ``main`` tells them to the model (after its narration step), to the
same file, from the top level of a scratch worktree of ``main``
(``8a619ae76``, the pinned ``MAIN_AT_B1C``, its ``faultmaven/`` unmodified)
holding this module, as
above. It keeps every B1a and B1b entry as stored. The ``turn_*`` scenarios
and the step-0/step-2 re-ask run a whole turn through ``process_turn``.

B1d (#2636) adds ``B1D_SCENARIOS``: each drives one of the offer writers
``main``'s deciders composed (the rca-infeasible, deferred, backstop,
false-alarm and needs-info-unmet closes and resolves, the needs-info request,
the step-0 request turned READY and the model's proposal under each of its
composers) and returns what the cycle published: each prose key and the open
offer's stored ``summary`` (``offer_texts``). The ``turn_*`` ones run a whole
turn through ``process_turn`` on a cause identified only on the
working-conclusion leg, which housekeeping ages under the bar after every
decider ran, and return the reply and the stored summary. ``capture_main_b1d``
adds them under ``cycle:offer:<name>`` at ``MAIN_AT_B1D`` (``21921b408``), the same
way, keeping every other entry as stored::

    cd <scratch> && python -c "from tests.unit.core.investigation import \\
        golden_cases_2636 as g; g.capture_main_b1d()"
"""

import asyncio
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from faultmaven.core.investigation.milestone_engine import stage_gates
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_assessment_state,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    _apply_stage_gate_signals,
    merge_decisions,
    merge_system_notices,
)
from faultmaven.core.investigation.milestone_engine.terminal_proposals import (
    _maybe_propose_confirmed_resolution,
)
from faultmaven.core.investigation.milestone_engine.transitions import (
    declined_resolution_reask,
)
from faultmaven.core.investigation.problem_status import invalidate_problem
from faultmaven.core.investigation.prompts.templates.assembly import (
    _resolve_declined_emphasis,
)
from faultmaven.core.investigation.schemas import (
    HypothesisUpdate,
    InvestigationResponse_Diagnosis,
    MilestoneUpdates,
    ProblemVerificationUpdate,
)
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
from tests.unit.core.investigation import fix_turns_2039 as fix_turns
from tests.unit.core.investigation import (
    test_declined_resolution_binds_the_model_1895 as declined,
)
from tests.unit.core.investigation import (
    test_fix_registration_lag_2427 as fix_report,
)
from tests.unit.core.investigation import (
    test_problem_statement_revision_and_false_alarm as revision,
)
from tests.unit.core.investigation.bound_rows import _row, bound_row
from tests.unit.core.investigation.narrated import narrated
from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
    _bare_case,
    _cause_leg_only,
    _eid,
    _failed_fix,
    _fallen_case,
    _identified_case,
    _interception_only,
    _invalidated,
    _no_symptom_row,
    _other_hypothesis,
    _relief_only,
)
from tests.unit.core.investigation.test_cause_leg_cited_basis_2557 import (
    _GONE,
    _PRESENT,
    _UNNAMED,
    _fixed,
    _mirror,
    _recheck,
)
from tests.unit.core.investigation.test_cause_leg_cited_basis_2557 import (
    _row as _basis_row,
)
from tests.unit.core.investigation.test_m5_recovery_1679 import (
    _make_engine as _m5_engine,
)
from tests.unit.core.investigation.test_m5_recovery_1679 import _meta as _m5_meta
from tests.unit.core.investigation.test_m5_recovery_1679 import _Updates
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _ROOT_ID,
    _accept,
    _problem_gone,
    _recompute_at,
    _still_failing,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _cause_gone as _m6_cause_gone,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _failed_fix as _m6_failed_fix,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _identified_case as _m6_identified_case,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _row as _m6_row,
)
from tests.unit.core.investigation.test_m6_relapse_disproves_nothing_2039 import (
    _relapsed_case,
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
from tests.unit.core.investigation.test_solution_attempts_2105 import (
    _relapsed as _fix_relapsed,
)
from tests.unit.core.investigation.test_terminal_verdict_cites_data_2036 import (
    _d6_case,
)
from tests.unit.core.investigation.test_terminal_verdict_cites_data_2036 import (
    _row as _d6_row,
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


def _reask(case):
    """The step-2 re-ask's text: ``main`` composed it; from B1c the decider
    returns its code's params, which the narration step renders."""
    reask = declined_resolution_reask(case)
    if not isinstance(reask, dict):
        return reask
    from faultmaven.core.investigation.narration.codes import NoticeCode
    from faultmaven.core.investigation.narration.notices import render_notice

    (text,) = render_notice(NoticeCode.DECLINED_RESOLUTION_REASK, reask, None, [])
    return text


def _declined_texts(case) -> dict:
    return {
        "reask": _reask(case),
        "prompt_line": _resolve_declined_emphasis(case),
    }


def _notices(case, metadata: dict) -> dict:
    """The notices after the cycle's narration step (B1c renders the pending
    check there; on ``main`` it finds nothing to render)."""
    return {
        "notices": list(narrated(case, metadata)),
        "needs_info": metadata.get("resolution_needs_info_message"),
    }


def _backstop(case) -> dict:
    return _notices(*_backstop_cycle(case))


def _backstop_cycle(case):
    metadata: dict = {}
    _maybe_propose_confirmed_resolution(case, metadata)
    return case, metadata


def _models_resolved(case) -> dict:
    return _notices(*_models_proposal(case, "resolved"))


def _models_proposal(case, to_state: str, message: str = "resolve it"):
    """Step 2 on the model's ``proposed_transition`` to ``to_state``."""
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    response = MagicMock()
    response.state_updates.proposed_transition = MagicMock(
        to_state=to_state, evidence_ids=[]
    )
    metadata: dict = {"response_obj": response}
    asyncio.run(engine.transitions.check_automatic_transitions(case, metadata, message))
    return case, metadata


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


# ---------------------------------------------------------------------------
# B1c: the remaining deciders, each rendered from what it read
# ---------------------------------------------------------------------------


def _absence(case, turn: int, metadata: dict | None = None):
    """An absence row at ``turn``, and the failed-window decider on it."""
    case.current_turn = turn
    case.evidence.append(_problem_gone(turn, label=f"fine_now_{turn}"))
    metadata = {} if metadata is None else metadata
    stage_gates._flag_absence_inside_a_failed_fix_window(case, metadata)
    return case, metadata


def _persisted():
    """The fix at 8, the cause re-checked gone and the problem present at 9:
    the window FAILED, disconfirming the cause."""
    case, _root, _hyp = _m6_identified_case()
    _m6_failed_fix(case)
    _recompute_at(case, 9)
    return case


def absence_with_no_row_this_turn():
    case = _persisted()
    case.current_turn = 10
    metadata: dict = {}
    stage_gates._flag_absence_inside_a_failed_fix_window(case, metadata)
    return case, metadata


def absence_after_a_persistence():
    return _absence(_persisted(), 10)


def absence_after_a_relapse():
    case, _root, _hyp = _relapsed_case()
    _recompute_at(case, 13)
    return _absence(case, 14)


def absence_after_window_zero_relapsed():
    """No fix recorded: the cited basis re-checked at 6, the problem seen gone
    at 7 and back at 8, window 0's relapse (#2501)."""
    case, _final, _relief, _login, ttl = _mirror()
    _recheck(case, ttl.evidence_id, turn=6)
    case.evidence += [
        _basis_row("gone0", _GONE, 7, "503s at 0.0/s"),
        _basis_row("back0", _PRESENT, 8, "503s at the gateway again"),
    ]
    case.current_turn = 9
    case.evidence.append(_basis_row("gone9", _GONE, 9, "503s at 0.0/s again"))
    metadata: dict = {}
    stage_gates._flag_absence_inside_a_failed_fix_window(case, metadata)
    return case, metadata


def absence_after_a_relapse_later_disconfirmed():
    """The critique's divergent cycle: the window failed on a relapse at 12;
    at 14 the cause re-checks still gone beside the problem, so the decider
    reads a disconfirmation the record's carried window does not hold."""
    case, _root, _hyp = _m6_identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 6)
    case.evidence += [_m6_cause_gone(7), _problem_gone(7, label="relief_7")]
    _recompute_at(case, 7)
    narrated(case, {})
    case.evidence.append(_still_failing(12, label="back_at_12"))
    _recompute_at(case, 12)
    narrated(case, {})
    case.evidence += [
        _m6_row(
            "cause_still_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            14,
            "max_connections still reads 100",
        ),
        _still_failing(14, label="still_503_at_14"),
    ]
    case.current_turn = 14
    metadata: dict = {}
    stage_gates._flag_absence_inside_a_failed_fix_window(case, metadata)
    _recompute_at(case, 14)
    return case, metadata


def absence_decided_twice_in_one_cycle():
    """Two applications in one cycle decide the same notice (A9)."""
    case, metadata = absence_after_a_persistence()
    stage_gates._flag_absence_inside_a_failed_fix_window(case, metadata)
    return case, metadata


def absence_decided_by_a_bundle_and_the_live_apply():
    """A replayed bundle and the live apply decide the same notice, each in its
    own accumulator, merged as the replay merges them."""
    case, bundle = absence_after_a_persistence()
    live: dict = {}
    stage_gates._flag_absence_inside_a_failed_fix_window(case, live)
    merge_system_notices(bundle, live)
    merge_decisions(bundle, live)
    return case, bundle


def _unbound(case):
    metadata: dict = {}
    stage_gates._flag_unbound_post_fix_rows(case, metadata)
    return case, metadata


def unbound_none_on_the_cited_basis():
    case, final, _relief, _login, ttl = _mirror()
    _fixed(case, final)
    _recheck(case, ttl.evidence_id)
    return _unbound(case)


def unbound_uncited_after_a_fix():
    case, final, relief, _login, _ttl = _mirror()
    _fixed(case, final)
    _recheck(case, relief.evidence_id)
    return _unbound(case)


def unbound_unnamed_after_a_fix():
    case, final, _relief, _login, _ttl = _mirror()
    _fixed(case, final)
    _recheck(case, _UNNAMED, label="unnamed")
    return _unbound(case)


def unbound_uncited_and_unnamed():
    case, final, relief, _login, _ttl = _mirror()
    _fixed(case, final)
    _recheck(case, _UNNAMED, label="unnamed")
    _recheck(case, relief.evidence_id, label="uncited")
    return _unbound(case)


def unbound_uncited_with_no_dated_fix():
    case, _final, _relief, login, _ttl = _mirror()
    _recheck(case, login.evidence_id, turn=6)
    return _unbound(case)


def unbound_on_an_uncited_pool_root():
    case, final, _relief, _login, ttl = _mirror(cited=False)
    _fixed(case, final)
    _recheck(case, ttl.evidence_id)
    return _unbound(case)


def unbound_with_no_pool():
    case, root, _hyp = _m6_identified_case()
    root.metadata = {}
    _recompute_at(case, 5)
    _accept(case, InvestigationActionType.SOLUTION, 6)
    _recheck(case, _UNNAMED)
    return _unbound(case)


def unbound_after_a_mitigation():
    case, _shown = _accepted_mitigation()
    _row(
        case,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        turn=7,
        evidence_id="ev_00000000a707",
        summary="orders answer in 200ms",
    )
    case.current_turn = 7
    return _unbound(case)


def unbound_before_housekeeping_retires_a_competitor():
    """The critique's divergent cycle: a competing hypothesis retired after
    the decider changes the confirmation roots the record reads."""
    case, _root = _fallen_case()
    _other_hypothesis(case)
    _pending_fix(case)
    (fix,) = [a for a in case.proposed_actions if a.action_id == "act_00000000f1f1"]
    fix.accepted_in_turn, fix.state = 6, "accepted"
    case.progress.solution_accepted = True
    case.current_turn = 7
    for label, category in (
        ("unnamed_7", EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE),
        ("unnamed_c7", EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE),
    ):
        bound_row(case, category, turn=7, evidence_id=_eid(label), rechecks=None)
    case, metadata = _unbound(case)
    case.hypotheses["hyp_00000000beef"].state = HypothesisState.RETIRED
    return case, metadata


def _bearer(action_type, state, **fields):
    return ProposedAction(
        action_id="act_00000000b0b1",
        case_id="case_000000001927",
        action_type=action_type,
        description="raise max_connections to 100",
        proposed_in_turn=5,
        state=state,
        **fields,
    )


def _registers(bearer, *, licensed, carried=False):
    case, _root, _hyp = _m6_identified_case()
    metadata: dict = {}
    stage_gates._bearer_registers(
        case, bearer, metadata, carried=carried, licensed=licensed
    )
    return case, metadata


def failed_fix_named_again():
    _eng, case = asyncio.run(_fix_relapsed())
    (first,) = [a for a in case.proposed_actions if a.state == "accepted"]
    metadata: dict = {}
    stage_gates._bearer_registers(case, first, metadata, carried=True, licensed=True)
    return case, metadata


def _withdrawn():
    return _bearer(
        InvestigationActionType.SOLUTION,
        "superseded",
        superseded_reason="license_lost",
        superseded_in_turn=5,
    )


def licence_withdrawn_once_it_holds():
    return _registers(_withdrawn(), licensed=True)


def licence_withdrawn_while_it_is_down():
    return _registers(_withdrawn(), licensed=False)


def licence_downgraded_once_it_holds():
    return _registers(
        _bearer(InvestigationActionType.DIAGNOSTIC, "pending"), licensed=True
    )


def licence_downgraded_while_it_is_down():
    return _registers(
        _bearer(InvestigationActionType.DIAGNOSTIC, "pending"), licensed=None
    )


def licence_carried_with_no_bearer():
    case, _root, hyp = _m6_identified_case()
    hyp.state = HypothesisState.REFUTED
    metadata: dict = {}
    carried = SimpleNamespace(held_fix="restart the pool", turn=6)
    stage_gates._register_carried_fix(case, carried, metadata, [])
    return case, metadata


def _reported(*turns_and_fields, setup=None):
    """``test_fix_registration_lag_2427``'s turns: the last one's metadata."""
    eng, case, _hyp = fix_report._identified()
    if setup is not None:
        setup(case)
    metadata: dict = {}
    for turn, fields in turns_and_fields:
        metadata = asyncio.run(fix_turns._turn(eng, case, turn, **fields))
    return case, metadata


def _fix_and_accept(description=fix_report._FIX, reported_in=None) -> dict:
    return dict(
        solutions_to_add=[fix_report._fix(description)],
        milestones=fix_report._accepted(reported_in),
    )


_PREVIOUS = "previous_message"


def fix_report_on_the_first_turn():
    return _reported((1, _fix_and_accept(reported_in=_PREVIOUS)))


def fix_report_without_an_acceptance():
    return _reported(
        (6, dict(milestones=MilestoneUpdates(accepted_fix_reported_in=_PREVIOUS)))
    )


def fix_report_on_a_hold_turn():
    return _reported(
        (6, _fix_and_accept(reported_in=_PREVIOUS)), setup=fix_report._revise
    )


def fix_report_after_a_fix_not_seen_failing():
    return _reported(
        (10, _fix_and_accept()),
        (11, _fix_and_accept(fix_report._SECOND, _PREVIOUS)),
    )


def fix_report_moving_a_disconfirmation():
    eng, case, _hyp = fix_report._identified()
    asyncio.run(fix_report._relapsed_at_8(eng, case))
    asyncio.run(
        fix_turns._turn(
            eng,
            case,
            10,
            fix_turns._ev(fix_turns._CAUSE_GONE, "max_connections still reads 100"),
            fix_turns._ev(fix_turns._PRESENT, "checkout still returns 503"),
        )
    )
    return case, asyncio.run(fix_report._second_fix_declared(eng, case))


def fix_report_after_a_held_turn():
    from tests.unit.core.investigation import test_fix_records_2036 as records

    apply = revision._apply

    async def _apply_and_record(engine, case, dsu, response=None):
        meta = await apply(engine, case, dsu, response)
        records.write_turn_record(case, meta)
        return meta

    revision._apply = _apply_and_record
    try:
        case, _root, metadata = asyncio.run(
            records._confirmed_with(
                dict(
                    solutions_to_add=[records._fix("raise the orders pool to 50")],
                    milestones=MilestoneUpdates(
                        solution_accepted=True,
                        accepted_action_ref="new_index_0",
                        accepted_fix_reported_in=_PREVIOUS,
                    ),
                ),
                hold_turns=1,
            )
        )
    finally:
        revision._apply = apply
    return case, metadata


def signals_on_an_invalidated_problem():
    def _invalidate(case):
        case.progress.problem_status = ProblemStatus.INVALIDATED

    eng, case, _hyp = fix_report._identified()
    asyncio.run(fix_turns._turn(eng, case, 6, solutions_to_add=[fix_report._fix()]))
    _invalidate(case)
    metadata = asyncio.run(
        fix_turns._turn(eng, case, 7, milestones=fix_report._accepted(_PREVIOUS))
    )
    return case, metadata


def fix_untargeted():
    from tests.unit.core.investigation import test_fix_records_2036 as records

    case, root, hyp = _m6_identified_case()
    records._bare_candidate(case, root, hyp)
    eng = records._engine()
    asyncio.run(records._record(eng, case, 4))
    _rows, metadata = asyncio.run(
        records._record(
            eng,
            case,
            5,
            root_cause_conclusion=records.RootCauseConclusionUpdate(
                root_cause=hyp.statement,
                mechanism="the checkout pool opens more connections than postgres "
                "admits",
                evidence_ids=[records._SUPPORT],
                likelihood=0.9,
                names_root_node_id="new_index_0",
            ),
            solutions_to_add=[records._fix("restart the checkout pods")],
            milestones=records._ran(),
        )
    )
    return case, metadata


def _verification(case, **fields):
    engine = revision._engine()
    metadata = revision._meta()
    asyncio.run(
        engine.responses._apply_investigation_updates(
            case,
            revision._DSU(verification_updates=ProblemVerificationUpdate(**fields)),
            metadata,
            revision._Response(),
        )
    )
    return case, metadata


def false_alarm_refused():
    return _verification(
        revision._case(ProblemStatus.VERIFIED),
        problem_invalidated=True,
        invalidation_basis="the user says it was never broken",
    )


def revision_refused():
    return _verification(
        revision._case(ProblemStatus.VERIFIED),
        revised_problem_statement=revision.REVISED,
        revision_basis="differs",
    )


def _mark_terminal(case, hyp, cited):
    metadata = _m5_meta()
    asyncio.run(
        _m5_engine().responses._apply_investigation_updates(
            case,
            _Updates(
                hypotheses_to_update=[
                    HypothesisUpdate(
                        hypothesis_id=hyp.hypothesis_id,
                        root_is_terminal=True,
                        terminal_basis_evidence_ids=cited,
                    )
                ]
            ),
            metadata,
        )
    )
    return case, metadata


def root_terminal_refused_with_none_cited():
    case, _root, hyp = _d6_case()
    return _mark_terminal(case, hyp, [])


def root_terminal_refused_on_a_row_before_the_root():
    case, _root, hyp = _d6_case()
    return _mark_terminal(case, hyp, [_d6_row(case, 3, "the secret_id expired")])


def root_terminal_not_qualifying_beside_its_verdict():
    case, _root, hyp = _d6_case()
    basis = _d6_row(case, 4, "no rotation job renews the secret_id")
    _mark_terminal(case, hyp, [basis])
    case.current_turn = 6
    return _mark_terminal(case, hyp, [_d6_row(case, 3, "the secret_id expired")])


def pending_check_backstop_declined():
    return _backstop_cycle(_declined(_held()))


def pending_check_step2_declined():
    return _models_proposal(_declined(_held()), "resolved")


def declined_resolution_reask_with_chips():
    return _models_proposal(_declined(_held(asked=False)), "resolved")


def declined_resolution_reask_held_by_a_check():
    return _models_proposal(_declined(_held()), "closed")


def declined_resolution_reask_without_chips():
    return _models_proposal(_declined(_confirmed_case(), on_hold=True), "resolved")


def _declined_close(case, basis: str):
    """The user declined the engine's ``basis`` close on the turn before."""
    with_declined_offer(case, kind="close", basis=basis)
    case.current_turn += 1
    return _models_proposal(case, "closed", "close it")


def declined_close_reask_on_a_false_alarm():
    """The model's ``resolved`` on a false-alarm finding whose close the user
    declined (it pivots to that close)."""
    case = revision._case(ProblemStatus.VERIFIED)
    invalidate_problem(case, evidence_ids=[], basis="the user says it never broke")
    with_declined_offer(case, kind="close", basis="false_alarm")
    case.current_turn += 1
    return _models_proposal(case, "resolved", "it's fixed")


def declined_close_reask_on_the_deferred_close():
    return _declined_close(_bare_case(), "deferred")


def _through_a_turn(case, state_updates, message: str = "ok"):
    """One turn through ``process_turn`` on the model's ``state_updates``: the
    case, and the turn record's notices (narrated, on this tree, by the turn
    itself)."""
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates=state_updates
        )
    )
    case.current_turn += 1
    asyncio.run(
        engine.process_turn(case=case, user_message=message, redaction_ctx=None)
    )
    return case, {"system_notices": list(case.turn_history[-1].system_notices)}


def turn_stage_gates_fix_report_without_an_acceptance():
    _eng, case, _hyp = fix_report._identified()
    return _through_a_turn(
        case,
        _DSU(milestones=MilestoneUpdates(accepted_fix_reported_in=_PREVIOUS)),
    )


def turn_response_application_false_alarm_refused():
    return _through_a_turn(
        revision._case(ProblemStatus.VERIFIED),
        _DSU(
            verification_updates=ProblemVerificationUpdate(
                problem_invalidated=True,
                invalidation_basis="the user says it was never broken",
            )
        ),
    )


def turn_chain_emission_root_terminal_refused():
    case, _root, hyp = _d6_case()
    case.current_turn -= 1
    return _through_a_turn(
        case,
        _DSU(
            hypotheses_to_update=[
                HypothesisUpdate(
                    hypothesis_id=hyp.hypothesis_id,
                    root_is_terminal=True,
                    terminal_basis_evidence_ids=[],
                )
            ]
        ),
    )


def turn_transitions_declined_close_reask():
    case = _bare_case()
    with_declined_offer(case, kind="close", basis="deferred")
    return _through_a_turn(
        case, _DSU(proposed_transition={"to_state": "closed"}), "close it"
    )


def turn_terminal_proposals_pending_check():
    return _through_a_turn(_held(), _DSU())


def declined_resolution_reask_at_step_0_and_step_2():
    """A resolution request (step 0) re-checked READY on rows the user
    declined, and the model's ``resolved`` (step 2), in one turn: each decides
    the same re-ask (A9)."""

    async def _run():
        engine, case, _ = await declined._offered_then_declined()
        node_id = declined._refute(case, declined._qualifying(case))
        await declined._turn(engine, case, "can we resolve it", propose="resolved")
        del case.causal_nodes[node_id]
        result = await declined._turn(
            engine, case, "it is resolved now", propose="resolved"
        )
        return case, result["case_updated"].turn_history[-1].system_notices

    case, notices = asyncio.run(_run())
    return case, {"system_notices": list(notices)}


#: B1c's (#2636): ``main`` composed each of these as text in its decider.
B1C_SCENARIOS = {
    fn.__name__: fn
    for fn in (
        absence_with_no_row_this_turn,
        absence_after_a_persistence,
        absence_after_a_relapse,
        absence_after_window_zero_relapsed,
        absence_after_a_relapse_later_disconfirmed,
        absence_decided_twice_in_one_cycle,
        absence_decided_by_a_bundle_and_the_live_apply,
        unbound_none_on_the_cited_basis,
        unbound_uncited_after_a_fix,
        unbound_unnamed_after_a_fix,
        unbound_uncited_and_unnamed,
        unbound_uncited_with_no_dated_fix,
        unbound_on_an_uncited_pool_root,
        unbound_with_no_pool,
        unbound_after_a_mitigation,
        unbound_before_housekeeping_retires_a_competitor,
        failed_fix_named_again,
        licence_withdrawn_once_it_holds,
        licence_withdrawn_while_it_is_down,
        licence_downgraded_once_it_holds,
        licence_downgraded_while_it_is_down,
        licence_carried_with_no_bearer,
        fix_report_on_the_first_turn,
        fix_report_without_an_acceptance,
        fix_report_on_a_hold_turn,
        fix_report_after_a_fix_not_seen_failing,
        fix_report_moving_a_disconfirmation,
        fix_report_after_a_held_turn,
        signals_on_an_invalidated_problem,
        fix_untargeted,
        false_alarm_refused,
        revision_refused,
        root_terminal_refused_with_none_cited,
        root_terminal_refused_on_a_row_before_the_root,
        root_terminal_not_qualifying_beside_its_verdict,
        pending_check_backstop_declined,
        pending_check_step2_declined,
        declined_resolution_reask_with_chips,
        declined_resolution_reask_held_by_a_check,
        declined_resolution_reask_without_chips,
        declined_close_reask_on_a_false_alarm,
        declined_close_reask_on_the_deferred_close,
        declined_resolution_reask_at_step_0_and_step_2,
        turn_stage_gates_fix_report_without_an_acceptance,
        turn_response_application_false_alarm_refused,
        turn_chain_emission_root_terminal_refused,
        turn_transitions_declined_close_reask,
        turn_terminal_proposals_pending_check,
    )
}

#: The commit ``capture_main`` captured B1c's entries at: ``main`` with B1b.
MAIN_AT_B1C = "8a619ae76f361e84e77f38061f4334d2048b7716"


def _main_tree(commit: str) -> None:
    """Refuse any tree but the top level of a checkout at ``commit`` with an
    unmodified ``faultmaven/``, imported from that tree: on this branch the
    golden would compare the renderers with themselves, and a peer branch, a
    dirty checkout or a copy of the tree inside another checkout is not main."""
    import faultmaven

    root = Path(__file__).resolve().parents[4]
    assert (
        Path(faultmaven.__file__).resolve().is_relative_to(root)
    ), f"faultmaven imported from {faultmaven.__file__}, not {root}"

    def _git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    top = Path(_git("rev-parse", "--show-toplevel")).resolve()
    assert top == root, f"{root} is not a checkout's top level (git sees {top})"
    head = _git("rev-parse", "HEAD")
    assert head == commit, f"HEAD is {head}, not {commit}"
    dirty = _git("status", "--porcelain", "--", "faultmaven/")
    assert not dirty, f"faultmaven/ is modified:\n{dirty}"
    print(f"faultmaven: {faultmaven.__file__}")


def capture_main() -> None:  # pragma: no cover - run once, on main
    """Add ``B1C_SCENARIOS``' texts to the golden, as ``main`` tells them to
    the model: its deciders compose them, and its narration step renders
    B1b's codes beside them. Refused on any tree but the top level of a
    checkout at ``MAIN_AT_B1C`` with an unmodified ``faultmaven/``. Additions
    only: B1a's scenarios must re-run to
    their stored entries, and B1b's (captured at ``b1969f821``, before their
    deciders recorded codes) are kept as stored."""
    from faultmaven.core.investigation.narration.codes import NoticeCode, is_token

    _main_tree(MAIN_AT_B1C)
    members = {m.value for m in NoticeCode}
    assert "absence_in_failed_window" not in members, "capture on main only"
    golden = json.loads(GOLDEN.read_text())
    changed = [n for n, build in SCENARIOS.items() if golden.get(n) != build()]
    assert not changed, f"B1a's entries no longer re-run to main's: {changed}"
    added = 0
    for name, build in B1C_SCENARIOS.items():
        text = cycle_text(narrated(*build()))
        assert not any(is_token(n) for n in text), f"{name}: a token left"
        key = f"cycle:{name}"
        assert golden.get(key, text) == text, f"{key} differs from its capture"
        added += key not in golden
        golden[key] = text
    GOLDEN.write_text(json.dumps(golden, indent=2, ensure_ascii=False) + "\n")
    print(f"added {added} of {len(B1C_SCENARIOS)} B1c scenarios to {GOLDEN}")


# ---------------------------------------------------------------------------
# B1d: the offers' prose and summaries, rendered after the cycle's record
# ---------------------------------------------------------------------------

#: The metadata keys an offer's (or a request's) prose is published under.
PROSE_KEY_NAMES = (
    "rca_infeasible_closure_message",
    "deferred_solution_gate_message",
    "resolution_ready_gate_message",
    "false_alarm_closure_message",
    "resolution_readiness_message",
    "resolution_needs_info_message",
)


def offer_texts(case, metadata: dict) -> dict:
    """What the cycle published for its offer: each prose key, and the open
    offer's stored summary, after the cycle's record and narration step (on
    ``main`` the deciders wrote both, and the narration step adds none)."""
    narrated(case, metadata)
    offer = case.offer_record.open
    return {
        "prose": {
            key: cycle_text([metadata[key]])[0]
            for key in PROSE_KEY_NAMES
            if key in metadata
        },
        "summary": None if offer is None else cycle_text([offer.summary])[0],
        "basis": None if offer is None else str(offer.basis),
    }


def _rca_infeasible_undated():
    from tests.unit.core.investigation.test_rca_infeasible_closure import _make_case

    case = _make_case(rca_infeasible=True, rationale="third-party API outage")
    metadata: dict = {}
    stage_gates._apply_stage_gate_side_effects(
        case, {"mitigation_verified"}, "mitigation worked", metadata
    )
    return case, metadata


def _rca_infeasible_relieved():
    """#2100's shape: the mitigation accepted at 6, a bound relief at 7."""
    from tests.unit.core.investigation.test_mitigation_record_2100 import (
        _accepted,
        _relief,
    )
    from tests.unit.core.investigation.test_mitigation_record_2100 import (
        _turn as _mitigation_turn,
    )

    async def _run():
        eng, case, shown = await _accepted()
        case.problem_verification.rca_infeasible = True
        case.problem_verification.rca_infeasible_rationale = "the vendor API is opaque"
        relief = _relief(case, 7, shown)
        meta = await _mitigation_turn(eng, case, 7, relief, mitigation_verified=True)
        return case, meta

    return asyncio.run(_run())


def _deferred(*, confirmed: bool):
    """A fix on record whose implementation is deferred out-of-band, its
    cause identified; ``confirmed``: gone⇒gone is on record (the resolve
    pivot)."""
    from faultmaven.core.investigation.milestone_engine.terminal_proposals import (
        _maybe_propose_deferred_close,
    )
    from faultmaven.modules.case.contracts import SolutionFeasible
    from tests.unit.core.investigation.test_resolution_backstop_turn import (
        _case as _backstop_case,
    )

    case = (
        _confirmed_case()
        if confirmed
        else _backstop_case(absence=None, problem_gone=False)
    )
    case.progress.cause_state = CauseState.IDENTIFIED
    case.progress.solution_feasible = SolutionFeasible.DEFERRED
    case.progress.solution_proposed = True
    metadata: dict = {}
    _maybe_propose_deferred_close(case, metadata)
    return case, metadata


def _false_alarm_close():
    async def _run():
        engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
        metadata = revision._meta()
        await engine.responses._apply_investigation_updates(
            case,
            _DSU(
                evidence_to_add=[
                    revision._row(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, "a1")
                ],
                verification_updates=ProblemVerificationUpdate(
                    problem_invalidated=True,
                    invalidation_evidence_ids=["new_index_0"],
                    invalidation_basis="no 5xx in the gateway log for the window",
                ),
            ),
            metadata,
            revision._Response(),
        )
        return case, metadata

    return asyncio.run(_run())


def _requested(case):
    """Step 0 on a resolution request the previous turn remembered."""
    from faultmaven.core.investigation.offers import remember_resolve_request

    case.offer_record = remember_resolve_request(case, ["the check"])
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    metadata: dict = {"response_obj": None}
    asyncio.run(engine.transitions.check_automatic_transitions(case, metadata, "done"))
    return case, metadata


def _wc_leg_case(*, confirmed: bool):
    """A cause identified only on the working-conclusion leg: no conclusion,
    no validated chain, a standing hypothesis at 0.68 that no turn has
    touched since turn 3, on a stalled case. Housekeeping ages it this turn
    (×0.85, under the 0.6 bar) and step 5.6 rebuilds the working conclusion
    without it, after every decider ran (critique 1)."""
    from tests.unit.core.investigation.test_resolution_backstop_turn import (
        _case as _backstop_case,
    )

    case = (
        _confirmed_case()
        if confirmed
        else _backstop_case(absence=None, problem_gone=False)
    )
    case.root_cause_conclusion = None
    h_id = _standing(case, "The OIDC provider registers the wrong client ID", 0.5)
    case.hypotheses[h_id].likelihood = 0.68
    case.current_turn = 12
    case.turns_without_progress = 20
    return case


def _whole_turn(case, state_updates, message: str = "it works now") -> dict:
    """One turn through ``process_turn``: the reply, and the offer it left."""
    engine = MilestoneEngine(MagicMock(), _make_repo(), investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates=state_updates
        )
    )
    result = asyncio.run(
        engine.process_turn(case=case, user_message=message, redaction_ctx=None)
    )
    offer = case.offer_record.open
    return {
        "reply": cycle_text([result["agent_response"]])[0],
        "summary": None if offer is None else cycle_text([offer.summary])[0],
        "basis": None if offer is None else str(offer.basis),
    }


def turn_model_proposal_confirmation_on_a_decaying_leg():
    return _whole_turn(
        _wc_leg_case(confirmed=True), _DSU(proposed_transition={"to_state": "resolved"})
    )


def turn_request_ready_on_a_decaying_leg():
    from faultmaven.core.investigation.offers import remember_resolve_request

    case = _wc_leg_case(confirmed=True)
    case.offer_record = remember_resolve_request(case, ["the check"])
    return _whole_turn(case, _DSU())


def turn_backstop_on_a_decaying_leg():
    return _whole_turn(_wc_leg_case(confirmed=True), _DSU())


def turn_model_proposal_closure_on_a_decaying_leg():
    return _whole_turn(
        _wc_leg_case(confirmed=False),
        _DSU(proposed_transition={"to_state": "closed"}),
        "close it",
    )


#: B1d's (#2636): ``main``'s deciders composed each offer's prose and summary.
B1D_SCENARIOS = {
    "rca_infeasible_undated": lambda: offer_texts(*_rca_infeasible_undated()),
    "rca_infeasible_relieved": lambda: offer_texts(*_rca_infeasible_relieved()),
    "deferred_resolve": lambda: offer_texts(*_deferred(confirmed=True)),
    "deferred_close": lambda: offer_texts(*_deferred(confirmed=False)),
    "backstop_cause_on_record": lambda: offer_texts(
        *_backstop_cycle(_confirmed_case())
    ),
    "backstop_no_cause_on_record": lambda: offer_texts(
        *_backstop_cycle(_no_cause_leg_no_solutions())
    ),
    "false_alarm_close": lambda: offer_texts(*_false_alarm_close()),
    "unmet_request_close": lambda: offer_texts(*_requested(_identified_case()[0])),
    "unmet_request_readiness_close": lambda: offer_texts(*_requested(_bare_case())),
    "request_ready": lambda: offer_texts(*_requested(_confirmed_case())),
    "needs_info_gaps": lambda: offer_texts(
        *_models_proposal(_identified_case()[0], "resolved")
    ),
    "needs_info_off_cause": lambda: offer_texts(
        *_models_proposal(_interception_only(), "resolved")
    ),
    "needs_info_relief_named": lambda: offer_texts(
        *_models_proposal(_relief_only(), "resolved")
    ),
    "model_readiness": lambda: offer_texts(*_models_proposal(_bare_case(), "resolved")),
    "model_readiness_false_alarm": lambda: offer_texts(
        *_models_proposal(_invalidated(), "resolved")
    ),
    "model_confirmation": lambda: offer_texts(
        *_models_proposal(_confirmed_case(), "resolved")
    ),
    "model_confirmation_no_cause": lambda: offer_texts(
        *_models_proposal(_no_cause_leg_no_solutions(), "resolved")
    ),
    "model_closure_summary": lambda: offer_texts(
        *_models_proposal(_identified_case()[0], "closed", "close it")
    ),
    "model_closure_trivial": lambda: offer_texts(
        *_models_proposal(investigating_case(), "closed", "close it")
    ),
    "model_closure_pivot": lambda: offer_texts(
        *_models_proposal(_confirmed_case(), "closed", "close it")
    ),
    "model_closure_pivot_named_with_solutions": lambda: offer_texts(
        *_models_proposal(
            _with_solutions(_concluded(_confirmed_case()), "raise the limit", ""),
            "closed",
            "close it",
        )
    ),
    "turn_model_proposal_confirmation_on_a_decaying_leg": (
        turn_model_proposal_confirmation_on_a_decaying_leg
    ),
    "turn_request_ready_on_a_decaying_leg": turn_request_ready_on_a_decaying_leg,
    "turn_backstop_on_a_decaying_leg": turn_backstop_on_a_decaying_leg,
    "turn_model_proposal_closure_on_a_decaying_leg": (
        turn_model_proposal_closure_on_a_decaying_leg
    ),
}


#: The commit ``capture_main_b1d`` captured B1d's entries at: ``main`` with B1c.
MAIN_AT_B1D = "21921b408f12e9fe57baf7768c35e7d4cac06f27"


def capture_main_b1d() -> None:  # pragma: no cover - run once, on main
    """Add ``B1D_SCENARIOS``' offers to the golden under ``cycle:offer:<name>``, as
    ``main``'s deciders composed them. Refused on any tree but the top level
    of a checkout at ``MAIN_AT_B1D`` with an unmodified ``faultmaven/``, and
    on one that has ``OfferCode``, where the golden would compare the
    renderers with themselves. Additions only: B1a's scenarios must re-run to
    their stored entries, and every other entry is kept as stored."""
    from faultmaven.core.investigation.narration import codes

    _main_tree(MAIN_AT_B1D)
    assert not hasattr(codes, "OfferCode"), "capture on main only"
    golden = json.loads(GOLDEN.read_text())
    changed = [n for n, build in SCENARIOS.items() if golden.get(n) != build()]
    assert not changed, f"B1a's entries no longer re-run to main's: {changed}"
    added = 0
    for name, build in B1D_SCENARIOS.items():
        texts = build()
        key = f"cycle:offer:{name}"
        assert golden.get(key, texts) == texts, f"{key} differs from its capture"
        added += key not in golden
        golden[key] = texts
    GOLDEN.write_text(json.dumps(golden, indent=2, ensure_ascii=False) + "\n")
    print(f"added {added} of {len(B1D_SCENARIOS)} B1d scenarios to {GOLDEN}")
