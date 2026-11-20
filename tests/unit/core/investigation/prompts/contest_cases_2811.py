"""The working conclusion's contest NOTE and the causal graph block: their
fixture corpus (#2811 S2b-2a).

The prompt-state corpus (``prompt_state_cases_2811.CASES``), plus one per
population no fixture there shows with a working conclusion: MECE-contested
roots (the NOTE), a held-open root, a refused verdict on a held-open root
and on a count-held one, the three populations together, and the declared
difference, a contest a user retire released with no recompute (the stored
flag still says contested; the graph shows none). Every added fixture is
hand-built from the suite's own builders: none makes a claim that a turn
reaches it.

``render`` renders each through ``main``'s entry point,
``build_investigation_context`` (no stage-specific loading, so the
``hypotheses`` section is the whole ``<causal_graph>`` block), and
``_build_causal_graph_block`` called alone. Its output on ``main`` is
``contest_oracle_2811.json``, captured at 0c4b76c6a by running
``render_all`` with this file copied into a ``git archive`` extract of that
commit, never typed by hand. The clock is frozen while a fixture is built and
rendered.
"""

from __future__ import annotations

from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_cause_state_from_chain,
)
from faultmaven.core.investigation.milestone_engine.hypothesis_updates import (
    _apply_hypothesis_action_intent,
)
from faultmaven.modules.case.contracts import WorkingConclusion
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN
from tests.unit.core.investigation.prompts import prompt_state_cases_2811 as ps
from tests.unit.core.investigation.prompts.prompt_state_cases_2811 import frozen
from tests.unit.core.investigation.test_mece_arbitration import (
    _SPECIMEN,
    _specimen_case,
)
from tests.unit.core.investigation.test_recovery_note_precedence_1141 import (
    _ONE_SUPPORT,
    _built,
)

#: The context budget rendered at: large enough that no section is cut.
BUDGET = 200_000


def _concluded(case, statement):
    case.working_conclusion = WorkingConclusion(
        statement=statement,
        likelihood=0.8,
        reasoning="the strongest standing hypothesis on its supports",
    )
    return case


async def _specimen(members, root_terminal="keep", refused=False):
    """``_built``'s specimen, a ``true`` verdict refused on its first root
    when ``refused``, concluded on that root."""
    case = _built(members, root_terminal=root_terminal)
    if refused:
        case.causal_nodes[members[0][0]].metadata["root_terminal_refused_turn"] = 5
    return _concluded(case, members[0][2])


async def _retired():
    """Two contested roots, one of whose hypotheses the user retires: the
    retire runs no recompute, so the stored flag stays contested while the
    standing set contests nothing (the declared difference)."""
    case = _specimen_case(members=_SPECIMEN[:2])
    _recompute_cause_state_from_chain(case)
    intent = {"hypothesis_id": _SPECIMEN[0][1], "action": "retire"}
    _apply_hypothesis_action_intent(None, case, intent, "set aside", {})
    return _concluded(case, _SPECIMEN[1][2])


#: The fixtures this corpus adds.
ADDED = {
    "C:contested": lambda: _specimen(_SPECIMEN),
    "C:held_open": lambda: _specimen(_SPECIMEN[:1], None),
    "C:refused_held_open": lambda: _specimen(_SPECIMEN[:1], None, True),
    "C:refused_count_held": lambda: _specimen([_ONE_SUPPORT], None, True),
    "C:all_three": lambda: _specimen(_SPECIMEN, None, True),
    "C:retired": _retired,
}

#: Every fixture, by name: the prompt-state corpus, then the added ones.
CASES = {**ps.CASES, **ADDED}

#: The fixtures whose output differs from ``main``'s, as declared:
#: ``retired``, where the stored flag says contested and the record (and the
#: graph) say not, so the NOTE is no longer rendered (critique point 1); and
#: ``omitted``, whose record cannot be read, so the graph block renders
#: ``RECOVERY_OMITTED`` in place of its contest notes.
DECLARED = {"retired": "C:retired", "omitted": "D:omitted"}


async def build(name: str):
    """The fixture ``name``, built with deterministic ids, the clock frozen."""
    with ps.fv._deterministic_ids(name), frozen():
        return await CASES[name]()


def render(case, fold=None) -> dict:
    """The ``working_conclusion`` and ``hypotheses`` sections
    ``build_investigation_context`` builds, with the assembly's ``fold``."""
    from faultmaven.core.investigation.prompts.context_builder.assembly import (
        build_investigation_context,
    )

    with frozen():
        ctx = build_investigation_context(
            case,
            "status",
            max_tokens=BUDGET,
            given_tools=DEFAULT_GIVEN,
            enable_stage_specific_loading=False,
            narration_fold=fold,
        )
    return {
        "conclusion": ctx.get("working_conclusion", ""),
        "graph": ctx.get("hypotheses", ""),
    }


def graph(case) -> str:
    """``_build_causal_graph_block`` called alone, as its tests call it."""
    from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
        _build_causal_graph_block,
    )

    with frozen():
        return _build_causal_graph_block(case)


async def render_all() -> dict[str, dict]:
    """Each fixture's two sections, by name; the block called alone renders
    what the assembly does."""
    out = {}
    for name in CASES:
        out[name] = render(await build(name))
        assert graph(await build(name)) == out[name]["graph"], name
    return out
