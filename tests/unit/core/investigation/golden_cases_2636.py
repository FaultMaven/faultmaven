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

``test_golden_composers_2636.py`` runs them on this tree and compares.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.terminal_proposals import (
    _maybe_propose_confirmed_resolution,
)
from faultmaven.core.investigation.milestone_engine.transitions import (
    declined_resolution_reask,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    _resolve_declined_emphasis,
)
from faultmaven.core.investigation.terminal_transitions import (
    assess_close_summary,
    assess_closure_readiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    CauseState,
    ConfidenceLevel,
    EvidenceCategory,
    NeedState,
    ProblemStatus,
    RootCauseConclusion,
    Solution,
    SolutionType,
)
from tests.case_builders import investigating_case, with_declined_offer
from tests.unit.core.investigation.bound_rows import _row
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
from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
    _held_case,
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


def capture_main() -> None:  # pragma: no cover - run once, on main
    import faultmaven

    root = Path(__file__).resolve().parents[4]
    assert (
        Path(faultmaven.__file__).resolve().is_relative_to(root)
    ), f"faultmaven imported from {faultmaven.__file__}, not {root}"
    print(f"faultmaven: {faultmaven.__file__}")
    GOLDEN.write_text(
        json.dumps(
            {name: build() for name, build in SCENARIOS.items()},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    print(f"captured {len(SCENARIOS)} scenarios into {GOLDEN}")
