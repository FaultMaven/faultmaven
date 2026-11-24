"""The resolve confirmation and the closed reply: their fixture corpus (#2811
S2b-2b).

The parity corpus (``test_narrator_record_parity_2811.FIXTURES``), plus one
per text the replies render that none of them does: a confirmation on each
cause-identification leg and on none, a working conclusion behind the chain
leg (real, and the early-stage placeholder), a root cause past its bound, each
solution text (a title, a ``SolutionType.`` title with a fix and alone, the
long-term fix only, the immediate action only, none), evidence and commands
present or absent; and each closure reason with and without a mitigation line,
closed through the real transition (the record written at CLOSED) and as a
copy of the case with no terminal write (``_closed``'s shape, as the reply's
own tests close), and a resolve. Every fixture is hand-built from the suite's
builders: none makes a claim that a turn reaches it.

``render`` renders each through ``main``'s entry points:
``resolution_confirmation_params``, ``_build_resolution_confirmation``, and on
a terminal case ``_terminal_confirmation_response`` and
``_compose_terminal_reply`` (with a summary and without). Its output on
``main`` is ``reply_oracle_2811.json``, captured by running ``render_all``
with this file copied into a ``git archive`` extract of ``main`` at
a16a152f8, never typed by hand. #3046 S3a-2 (the deciders read the
record's carried FAILED verdicts) regenerated, with this generator on its
branch, the entry of the fixture it changes on purpose,
``2595_unkinded_failure_rebound``. Ids are deterministic and the clock is
frozen while a fixture is built and rendered.
"""

from __future__ import annotations

from faultmaven.core.investigation.terminal_transitions import (
    _execute_closed_transition,
    _execute_resolved_transition,
)
from faultmaven.modules.case.contracts import (
    CaseState,
    CauseState,
    ClosureReason,
    ConfidenceLevel,
    RootCauseConclusion,
    Solution,
    SolutionType,
    WorkingConclusion,
)
from tests.case_builders import investigating_case
from tests.unit.core.investigation import test_narrator_record_parity_2811 as parity
from tests.unit.core.investigation.prompts import prompt_state_cases_2811 as ps
from tests.unit.core.investigation.prompts.prompt_state_cases_2811 import frozen

#: The summary payload the terminal reply is composed with.
SUMMARY = "## Closure summary\n\nThe pool was resized."


def _held(*, asked: bool = False):
    from tests.unit.core.investigation.golden_cases_2636 import _held

    return _held(asked=asked)


def _chain():
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _identified_case,
    )

    case, _root, _hyp = _identified_case()
    return case


def _rcc(case, root_cause: str = "max_connections is 20"):
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause=root_cause,
        confidence_level=ConfidenceLevel.VERIFIED,
        likelihood=0.9,
        mechanism="the pool waits on a full server",
    )
    return case


def _wc(case, *, likelihood: float = 0.8, evidence=("ev_00000000aaaa",)):
    case.working_conclusion = WorkingConclusion(
        statement="the checkout pool is capped at 20 connections",
        likelihood=likelihood,
        reasoning="the strongest standing hypothesis on its supports",
        supporting_evidence_ids=list(evidence),
    )
    return case


def _backstop(case):
    """No chain leg, so a backstop leg decides."""
    case.progress.cause_state = CauseState.UNKNOWN
    case.root_cause_conclusion = None
    case.working_conclusion = None
    return case


def _solution(case, title="raise max_connections to 100", **fields):
    """A solution holding ``title`` and ``fields`` alone, as stored: past the
    model's bounds (a long title, no fix at all) included."""
    solution = Solution(
        solution_id=f"sol_{len(case.solutions):012d}",
        solution_type=SolutionType.CONFIG_CHANGE,
        title="untitled",
        longterm_fix="placeholder",
    )
    case.solutions.append(
        solution.model_copy(update={"title": title, "longterm_fix": None, **fields})
    )
    return case


async def _leg_none():
    return _backstop(_held())


async def _leg_rcc():
    return _rcc(_backstop(_held()))


async def _leg_wc():
    return _wc(_backstop(_held()))


async def _chain_beside(conclusion: str):
    case = _chain()
    case.root_cause_conclusion = None
    if conclusion == "real":
        _wc(case)
    elif conclusion == "early":
        _wc(case, likelihood=0.0, evidence=())
    else:
        case.working_conclusion = None
    return case


async def _long_root():
    return _rcc(_chain(), "the pool " + "waits on a full server " * 12)


async def _with_solutions(*specs):
    case = _chain()
    case.solutions = []
    for title, fields in specs:
        _solution(case, title, **fields)
    return case


async def _bare(**solution):
    """A case with no evidence; with ``solution``, one solution of those
    fields."""
    case = investigating_case()
    case.evidence = []
    if solution:
        _solution(case, **solution)
    return case


_FIX = {"longterm_fix": "set max_connections to 100"}

#: The confirmation fixtures this corpus adds.
CONFIRMATIONS = {
    "C:leg_none": _leg_none,
    "C:leg_chain": lambda: _chain_beside("none"),
    "C:leg_rcc": _leg_rcc,
    "C:leg_working_conclusion": _leg_wc,
    "C:chain_beside_a_working_conclusion": lambda: _chain_beside("real"),
    "C:chain_beside_an_early_stage_conclusion": lambda: _chain_beside("early"),
    "C:long_root": _long_root,
    "C:solution_title": lambda: _with_solutions(("raise max_connections", _FIX)),
    "C:solution_enum_title": lambda: _with_solutions(
        ("Solution: SolutionType.CONFIG_CHANGE", _FIX)
    ),
    "C:solution_enum_title_alone": lambda: _with_solutions(
        ("Solution: SolutionType.CONFIG_CHANGE", {})
    ),
    "C:solution_longterm_only": lambda: _with_solutions(("", _FIX)),
    "C:solution_immediate_only": lambda: _with_solutions(
        ("", {"immediate_action": "restart pgbouncer"})
    ),
    "C:solution_latest_of_two": lambda: _with_solutions(
        ("lower the pool", {}), ("raise max_connections", _FIX)
    ),
    "C:solution_long_title": lambda: _with_solutions(("raise it " * 30, {})),
    "C:evidence_commands": lambda: _with_solutions(
        (
            "raise max_connections",
            {"commands": ["ALTER SYSTEM SET max_connections = 100"]},
        )
    ),
    "C:no_evidence_no_commands": lambda: _bare(),
    "C:no_evidence_commands": lambda: _bare(
        commands=["ALTER SYSTEM SET max_connections = 100"]
    ),
    "C:no_evidence_steps": lambda: _bare(implementation_steps=["edit the config"]),
}


def _mitigated():
    from tests.unit.core.investigation.test_mitigation_closure_2100 import (
        _mitigation,
    )

    case, _shown = _mitigation(attempt=6, relief=7)
    return case


def _unmitigated():
    from tests.unit.core.investigation.test_refutation_needs_data_1911 import _case

    return _case()


async def _closed(reason: ClosureReason, line: bool, written: bool):
    """Closed on ``reason``: through the real transition (``written``), or
    as a copy with no terminal write."""
    case = _mitigated() if line else _unmitigated()
    if written:
        _execute_closed_transition(case, "user_1", reason)
        return case
    return case.model_copy(update={"state": CaseState.CLOSED, "closure_reason": reason})


async def _resolved():
    from tests.unit.core.investigation.test_resolution_backstop_turn import (
        _confirmed_case,
    )

    case = _confirmed_case()
    _execute_resolved_transition(case, "user_1")
    return case


#: The terminal fixtures this corpus adds.
TERMINALS = {
    f"T:{reason.value}:{'line' if line else 'bare'}:"
    f"{'written' if written else 'copied'}": (
        lambda r=reason, li=line, w=written: _closed(r, li, w)
    )
    for reason in ClosureReason
    for line in (True, False)
    for written in (True, False)
}
TERMINALS["T:resolved"] = _resolved

#: Every fixture, by name: the parity corpus, then the added ones.
CASES = {
    **{name: build for name, (build, _declared) in parity.FIXTURES.items()},
    **CONFIRMATIONS,
    **TERMINALS,
}


async def build(name: str):
    """The fixture ``name``, built with deterministic ids, the clock frozen."""
    with ps.fv._deterministic_ids(name), frozen():
        return await CASES[name]()


def render(case) -> dict:
    """What ``main``'s entry points return for ``case``."""
    from faultmaven.core.investigation.milestone_engine.terminal_replies import (
        _build_resolution_confirmation,
        _compose_terminal_reply,
        _terminal_confirmation_response,
        resolution_confirmation_params,
    )

    with frozen():
        out = {
            "params": resolution_confirmation_params(case),
            "confirmation": _build_resolution_confirmation(case),
        }
        if case.is_terminal:
            out["terminal"] = _terminal_confirmation_response(case)
            out["composed"] = _compose_terminal_reply(case, SUMMARY)
            out["composed_bare"] = _compose_terminal_reply(case, None)
    return out


async def render_all() -> dict[str, dict]:
    """Each fixture's renders, by name."""
    return {name: render(await build(name)) for name in CASES}
