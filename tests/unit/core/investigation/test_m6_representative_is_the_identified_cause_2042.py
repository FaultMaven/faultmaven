"""#2042 — M6's representative cause is a hypothesis on the identified root.

With no conclusion naming its cause, M6 used to take the strongest hypothesis
by ``initial_likelihood`` over every hypothesis, refuted ones included. At the
0.5 default every hypothesis ties and ``max`` returns the first, so in both
gateway g2 runs a hypothesis refuted at turn 4 was "the identified cause" at
turn 11: M6's evidence arm fired, minted "the identified cause no longer
stands" and retracted the conclusion, while another hypothesis's root stood
validated and identified.

The representative now reads the one definition of the cause (#2038) as the
last recompute recorded it (``identified_cause_root_ids``): a hypothesis on a
root that identification rested on, a standing one first. A cause refuted in
the turn itself is on that record, so M6 still demotes it; a verdict this turn
or a root whose hypothesis fell long ago does not move it.
"""

import pytest

from faultmaven.core.investigation.causal_graph.rcc import (
    _representative_cause_hypothesis,
)
from faultmaven.modules.case.contracts import (
    CausalNode,
    CauseState,
    ConfidenceLevel,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    NodeState,
    NodeType,
    RootCauseConclusion,
    ValidationMethod,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _assert_demoted,
    _engine_rows,
    _failed_fix,
    _identified_case,
    _recompute_at,
)

pytestmark = pytest.mark.unit


def _refuted_earlier(*, initial_likelihood=0.5, root_id="cn_0000000000a1", on=None):
    """A hypothesis the model refuted before the cause was identified. Its own
    root (``on`` None) never validated; ``on`` puts it on another root."""
    root = None
    if on is None:
        root = CausalNode(
            node_id=root_id,
            statement="an expired intermediate certificate in the served chain",
            node_type=NodeType.ROOT,
            node_state=NodeState.REFUTED,
            refutation_reason="the intermediate is valid until 2029",
            validation_method=ValidationMethod.NONE,
            belief=0.0,
            actionable=False,
            generated_at_turn=3,
        )
    hyp = Hypothesis(
        hypothesis_id="hyp_0000000000a1",
        statement="an expired intermediate certificate in the served chain",
        category=HypothesisCategory.SECURITY,
        state=HypothesisState.REFUTED,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        rationale="initial",
        root_node_id=root.node_id if root is not None else on,
        initial_likelihood=initial_likelihood,
        likelihood=0.0,
        refutation_reason="the intermediate is valid until 2029",
        generated_at_turn=3,
    )
    return hyp, root


def _with_first(case, hyp, root):
    """Put ``hyp`` FIRST in the case's hypotheses: ``max`` breaks a tie toward
    the first, which is what made the old proxy pick it."""
    if root is not None:
        case.causal_nodes[root.node_id] = root
    case.hypotheses = {hyp.hypothesis_id: hyp, **case.hypotheses}


def _unlinked_conclusion(case):
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="max_connections is set to 20, below the checkout pool size",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
        mechanism="the pool waits on connections the server never grants",
        evidence_basis=[],
        determined_at_turn=case.current_turn,
    )
    assert case.root_cause_conclusion.validated_hypothesis_id is None


def test_the_incident_tie_keeps_the_identified_cause_and_its_conclusion():
    case, root, hyp = _identified_case()
    hyp.initial_likelihood = 0.5
    early, early_root = _refuted_earlier()
    _with_first(case, early, early_root)
    _unlinked_conclusion(case)
    # The case's own link pass must not rescue it: the conclusion text above is
    # the root's, not the hypothesis statement, as in the incident.
    case.root_cause_conclusion.root_cause = "a pool the server starves"

    assert _representative_cause_hypothesis(case) is hyp
    _recompute_at(case, 11)

    assert _engine_rows(case) == []
    assert case.root_cause_conclusion is not None
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert hyp.state != HypothesisState.REFUTED


def test_a_cause_refuted_in_the_turn_itself_is_still_demoted():
    """The case the old proxy's ``initial_likelihood`` choice protected: the
    believed cause is refuted this turn with no conclusion naming it. Its root
    is still the identified one until the next derive."""
    case, root, hyp = _identified_case()
    _unlinked_conclusion(case)
    case.root_cause_conclusion.root_cause = "a pool the server starves"
    hyp.state = HypothesisState.REFUTED
    hyp.refutation_reason = "the pool never saturates in the captured window"

    assert _representative_cause_hypothesis(case) is hyp
    _recompute_at(case, 11)

    assert len(_engine_rows(case)) == 1
    assert case.root_cause_conclusion is None
    assert case.progress.cause_state != CauseState.IDENTIFIED


def test_a_standing_hypothesis_on_the_identified_root_outranks_a_refuted_one():
    """Two framings of one root: the refuted one carries the higher prior. The
    cause stands while its standing hypothesis does."""
    case, root, hyp = _identified_case()
    hyp.initial_likelihood = 0.5
    framing, _ = _refuted_earlier(initial_likelihood=0.7, on=root.node_id)
    _with_first(case, framing, None)
    _unlinked_conclusion(case)
    case.root_cause_conclusion.root_cause = "a pool the server starves"

    assert _representative_cause_hypothesis(case) is hyp
    _recompute_at(case, 11)

    assert _engine_rows(case) == []
    assert case.root_cause_conclusion is not None


def test_nothing_on_the_record_has_no_representative():
    case, root, hyp = _identified_case()
    early, early_root = _refuted_earlier()
    _with_first(case, early, early_root)
    case.progress.identified_cause_root_ids = []
    _unlinked_conclusion(case)

    assert _representative_cause_hypothesis(case) is None


def test_a_same_turn_not_terminal_verdict_does_not_hide_a_failed_fix():
    """Review finding 1: the turn's verdicts are applied before M6. The model
    says the cause's root is not terminal in the turn its fix is seen failing;
    the cause the last recompute identified is still the one demoted, as with
    a linked conclusion."""
    case, root, hyp = _identified_case()
    _unlinked_conclusion(case)
    case.root_cause_conclusion.root_cause = "a pool the server starves"
    _failed_fix(case, fix_turn=8, outcome_turn=9)
    root.record_root_terminal(False, "something upstream resets the limit", 9)

    assert _representative_cause_hypothesis(case) is hyp
    _recompute_at(case, 9)

    _assert_demoted(case, root, hyp)
    assert len(_engine_rows(case)) == 1


def test_an_orphaned_identified_root_never_stands_in_for_the_cause():
    """Review finding 2: a second root still reads VALIDATED and terminal,
    but its only hypothesis was refuted at turn 4, so it grounded no
    identification. When the identified cause is refuted this turn, the 0.5
    tie must not hand the demotion to the old hypothesis."""
    case, root, hyp = _identified_case()
    hyp.initial_likelihood = 0.5
    old_root = CausalNode(
        metadata={"root_terminal": True},
        node_id="cn_0000000000b2",
        statement="the checkout pod's clock runs ahead of the database",
        node_type=NodeType.ROOT,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.EMPIRICAL,
        belief=0.9,
        actionable=True,
        generated_at_turn=3,
    )
    old, _ = _refuted_earlier(on=old_root.node_id)
    _with_first(case, old, old_root)
    assert old_root.is_identified_root
    _unlinked_conclusion(case)
    case.root_cause_conclusion.root_cause = "a pool the server starves"
    hyp.state = HypothesisState.REFUTED
    hyp.refutation_reason = "the pool never saturates in the captured window"

    assert _representative_cause_hypothesis(case) is hyp
    _recompute_at(case, 11)

    (row,) = _engine_rows(case)
    assert "the pool never saturates" in row.summary
    assert "intermediate is valid until 2029" not in row.summary


def test_a_conclusion_naming_its_hypothesis_stays_authoritative():
    case, root, hyp = _identified_case()
    early, early_root = _refuted_earlier()
    _with_first(case, early, early_root)
    _unlinked_conclusion(case)
    case.root_cause_conclusion.validated_hypothesis_id = early.hypothesis_id

    assert _representative_cause_hypothesis(case) is early


def test_a_root_whose_only_hypothesis_fell_grounds_no_identification():
    """What the record is written from: a standing hypothesis's identified
    root. A VALIDATED, terminal root whose only hypothesis was refuted is not
    one, which is what keeps it off the record."""
    from faultmaven.core.investigation.causal_graph.projection import (
        identified_root_ids,
    )

    case, root, hyp = _identified_case()
    old_root = CausalNode(
        metadata={"root_terminal": True},
        node_id="cn_0000000000b3",
        statement="the checkout pod's clock runs ahead of the database",
        node_type=NodeType.ROOT,
        node_state=NodeState.VALIDATED,
        validation_method=ValidationMethod.EMPIRICAL,
        belief=0.9,
        actionable=True,
        generated_at_turn=3,
    )
    old, _ = _refuted_earlier(on=old_root.node_id)
    _with_first(case, old, old_root)

    assert identified_root_ids(case) == {root.node_id}
    assert case.progress.identified_cause_root_ids == [root.node_id]
