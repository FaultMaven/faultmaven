"""Both clauses of the root-cause test reach the model (#2036).

The methodology's root cause passes (a) a specific action permanently
eliminates D AND (b) it is not the consequence of a controllable cause above
it (§2, R7, §7.5). The prompt stated only (a), at the one place it defined
``root``, and its worked example rooted an expiry, so the model concluded at an
expired credential and proposed resolving on a renewal.

These pins hold:

- the test is stated ONCE, in the investigation base, so every INVESTIGATING
  stage and mode carries it and no copy can drift;
- every site where the model decides a node is the root names it: the chain's
  root definition and re-root rule, runbook Cause attribution, proposing the
  fix, and both statements of what causal absence means;
- the worked example no longer stops at the expiry;
- the model's view of a deepened chain labels the old root a rung.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.milestone_engine.chain_emission import (
    _apply_chain_emission,
)
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    _build_causal_graph_block,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.blocks import (
    _ROOT_CAUSE_TEST_BLOCK,
)
from faultmaven.core.investigation.prompts.templates.diagnosis import (
    _CHAIN_EMISSION_BLOCK,
    rca_diagnosis_block,
)
from faultmaven.core.investigation.prompts.templates.investigation import (
    INVESTIGATION_BASE,
)
from faultmaven.core.investigation.prompts.templates.treatment import (
    treatment_instructions,
)
from faultmaven.core.investigation.schemas import ProblemVerificationUpdate
from faultmaven.infrastructure.llm.prompt_cache import CACHE_BOUNDARY
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    MitigationRecord,
    NodeType,
    ProblemStatus,
    ProblemVerification,
)
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN

pytestmark = pytest.mark.unit

_HEADING = "ROOT-CAUSE TEST (the why-stopping rule):"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _case(stage: str = "diagnosis") -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="Gateway calls to user-service fail",
            problem_statement_confirmed=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="api-gateway calls to user-service return 503",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.current_turn = 4
    case.progress.problem_status = ProblemStatus.VERIFIED
    if stage == "mitigation":
        case.progress.mitigation = MitigationRecord(proposed_at_turn=2, accepted=True)
    elif stage == "treatment":
        case.progress.solution_accepted = True
    return case


# ---------------------------------------------------------------------------
# One statement of the test, in every INVESTIGATING prompt
# ---------------------------------------------------------------------------


def test_the_test_states_both_clauses():
    text = _flat(_ROOT_CAUSE_TEST_BLOCK)
    assert "passes BOTH clauses" in text
    assert "eliminate the problem PERMANENTLY" in text
    assert "it is not the consequence of a cause above it" in text
    # A renewal intercepts a state; it is never the remediation.
    assert "INTERCEPTS it" in text and "never the remediation" in text


def test_a_cause_above_is_one_that_recreates_the_state():
    """The floor on the why-question: without it, (b) walks from a one-off
    config change into "what review should have caught it"."""
    text = _flat(_ROOT_CAUSE_TEST_BLOCK)
    assert "something will produce the state AGAIN after it is reset" in text
    assert (
        "A state set once by a person or by a one-off event, that nothing will "
        "re-create, passes (b): correcting it is the remediation" in text
    )
    # Listed states MAY have a cause above them; the list is not a verdict.
    assert "drifted configs are states that MAY have a cause above them" in text


def test_the_descent_has_an_exit_the_engine_can_read():
    """Gate 2: the rule cannot stall a case. A boundary stop is declared in
    the field the post-mitigation close offer reads; a data gap is not one
    (the engine refuses to let rca_infeasible excuse a contingent gap)."""
    text = _flat(_ROOT_CAUSE_TEST_BLOCK)
    gap = text.index("When the data to go further cannot be obtained")
    boundary = text.index("When nothing on the user's side can eliminate")
    assert "rca_infeasible" not in text[gap:boundary]
    assert "set `verification_updates.rca_infeasible` true" in text[boundary:]
    assert "`rca_infeasible_rationale` saying why" in text[boundary:]
    # The fields the block names exist on the schema the model fills.
    assert {"rca_infeasible", "rca_infeasible_rationale"} <= set(
        ProblemVerificationUpdate.model_fields
    )


def test_the_test_lives_in_the_cached_prefix_of_the_investigation_base():
    # Static text in the durable prefix (#613): it renders the same bytes on
    # every turn, so it costs the cache nothing after the first turn.
    prefix, _, _ = INVESTIGATION_BASE.partition(CACHE_BOUNDARY)
    assert _ROOT_CAUSE_TEST_BLOCK in prefix
    assert "{" not in _ROOT_CAUSE_TEST_BLOCK and "}" not in _ROOT_CAUSE_TEST_BLOCK


@pytest.mark.parametrize(
    "stage,mode",
    [
        ("diagnosis", "triage"),
        ("diagnosis", "directed_analysis"),
        ("mitigation", "triage"),
        ("treatment", "triage"),
    ],
)
def test_every_investigating_stage_carries_the_test_once(stage, mode):
    prompt = get_prompt_for_case(
        _case(stage), "what now?", processing_mode=mode, given_tools=DEFAULT_GIVEN
    )
    assert prompt.count(_HEADING) == 1
    # The half test the prompt used to give is gone from every stage.
    assert "remove it and the problem is gone" not in prompt


# ---------------------------------------------------------------------------
# Every site that decides "this is the root" names the test
# ---------------------------------------------------------------------------


def test_the_chain_root_definition_needs_both_clauses():
    text = _flat(_CHAIN_EMISSION_BLOCK)
    assert (
        "Positing it does not make it the ROOT CAUSE: that takes both clauses of "
        "the ROOT-CAUSE TEST" in text
    )


def test_the_why_question_deepens_the_chain_by_re_rooting():
    text = _flat(_CHAIN_EMISSION_BLOCK)
    assert "the ROOT-CAUSE TEST's why-question is how you find one" in text
    assert "emit the new root with `produces` set to the old root's `cn_...` id" in (
        text
    )


def test_the_worked_example_does_not_stop_at_the_expiry():
    text = _flat(_CHAIN_EMISSION_BLOCK)
    example = text[text.index("Example (TLS handshake failures)") :]
    example = example[: example.index("Build backward from D")]
    # The expiry is posited first, then the why-question deepens past it.
    assert example.index("cert expired at 02:00") < example.index(
        "fails clause (b) of the ROOT-CAUSE TEST"
    )
    assert 'produces:"cn_...certexpired"' in example
    assert "hypotheses_to_update" in example
    assert "Renewing the cert by hand is now the mitigation" in example
    # The new root passes the floor of clause (b) itself.
    assert "the change log shows a one-off manual edit" in example
    assert "nothing will disable it again and it passes clause b" in example


def test_a_runbook_cause_gets_the_test_too():
    # Shipped pack Statements root at states ("The client token has expired"),
    # and attribution adopts a Statement verbatim as the hypothesis.
    text = _flat(rca_diagnosis_block(DEFAULT_GIVEN))
    assert "A Cause's Statement gets the ROOT-CAUSE TEST like any root" in text
    assert (
        "its Interventions restore that state, so propose them as a mitigation "
        "(`solution_type: workaround`)" in text
    )


def test_proposing_a_fix_applies_the_test_first():
    text = _flat(rca_diagnosis_block(DEFAULT_GIVEN))
    step = text[text.index("4. **Propose Solution**") :]
    assert step.index("Apply the ROOT-CAUSE TEST first") < step.index(
        "State the root cause in one sentence"
    )
    # WORKAROUND is what routes a fix as a mitigation; quadrant is data.
    assert "`solution_type: workaround`, the engine's mitigation signal" in step
    assert "`quadrant`: `remediation` only for a cause that passes the" in step


def test_proposing_a_mitigation_asks_its_why_question():
    # The hold on fresh asks after a proposal would otherwise gag the one
    # question the test requires.
    text = _flat(rca_diagnosis_block(DEFAULT_GIVEN))
    hold = text[text.index("Having proposed the fix, do not pile") :]
    assert (
        "The one exception is a fix proposed as a mitigation under the "
        "ROOT-CAUSE TEST: name its why-question" in hold[:400]
    )


def test_the_single_shot_path_needs_the_test():
    text = _flat(rca_diagnosis_block(DEFAULT_GIVEN))
    option_a = text[text.index("**Option A: SINGLE-SHOT**") :]
    assert "and the cause passes the ROOT-CAUSE TEST" in option_a[:400]


def test_a_runbook_cause_that_fails_the_test_does_not_resolve():
    """The KB-resolution variant copies the Cause into the conclusion and
    records it gone; for a Cause that names an intercepted state, neither."""
    text = _flat(treatment_instructions(DEFAULT_GIVEN))
    rule = text[text.index("When the attributed Cause fails the ROOT-CAUSE TEST") :]
    rule = rule[: rule.index("REQUIRED EMISSIONS")]
    assert "emit items 1, 3 and 4 and the `symptom_absence_evidence` row" in rule
    assert "Skip item 2, the `causal_absence_evidence` row and item 6" in rule
    assert "ask why the state arose" in rule


def test_causal_absence_is_not_recorded_for_a_renewed_state():
    base = _flat(INVESTIGATION_BASE)
    assert (
        "whose cause sits above it (clause b of the ROOT-CAUSE TEST) leaves that "
        "cause in place: symptom_absence only" in base
    )
    treatment = _flat(treatment_instructions(DEFAULT_GIVEN))
    assert (
        "a state whose cause sits above it (clause b of the ROOT-CAUSE TEST) "
        "leaves that cause in place: record symptom_absence only" in treatment
    )
    # A true remediation usually renews the state too; it still records the
    # cause above it gone.
    for text in (base, treatment):
        assert "the causal_absence row records THAT cause gone" in text


# ---------------------------------------------------------------------------
# The model's view of a deepened chain
# ---------------------------------------------------------------------------


def _node(statement: str, produces: str, node_type=NodeType.ROOT):
    return SimpleNamespace(
        statement=statement, node_type=node_type, produces=produces, and_group=None
    )


def _emit(case: Case, hyp_id: str, nodes: list, *, created: bool) -> None:
    _apply_chain_emission(
        case,
        SimpleNamespace(
            causal_nodes_to_add=nodes, causal_edges_to_add=[], node_evidence_links=[]
        ),
        {
            "hypotheses_generated": [hyp_id] if created else [],
            "hyp_root_refs": {hyp_id: "new_index_0"},
        },
    )


def _hyp(statement: str) -> Hypothesis:
    return Hypothesis(
        statement=statement,
        category=HypothesisCategory.CONFIG,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=4,
        rationale="r",
        state=HypothesisState.ACTIVE,
    )


def _deepened(case: Case, hyp: Hypothesis) -> tuple[str, str]:
    """Root ``hyp`` at an expiry, then deepen past it; (old root, new root)."""
    _emit(
        case,
        hyp.hypothesis_id,
        [_node("the issuer's AppRole secret_id expired", "D")],
        created=True,
    )
    old_root = hyp.root_node_id
    _emit(
        case,
        hyp.hypothesis_id,
        [_node("nothing rotates the secret_id (fixed TTL, issued by hand)", old_root)],
        created=False,
    )
    return old_root, hyp.root_node_id


def _line(block: str, node_id: str) -> str:
    return next(line for line in block.splitlines() if node_id in line)


def test_a_root_the_chain_was_deepened_past_renders_as_a_rung():
    """Deepening keeps the old root's stored type (the cluster-origin
    machinery reads it), so the view must not show it as a second root."""
    case = _case()
    hyp = _hyp("cert-manager cannot renew the gateway's client cert")
    case.hypotheses = {hyp.hypothesis_id: hyp}
    old_root, new_root = _deepened(case, hyp)
    assert new_root != old_root
    # Storage is untouched: the old node is still a ROOT, now below the new one.
    assert case.causal_nodes[old_root].node_type == NodeType.ROOT
    assert hyp.path[:2] == [new_root, old_root]

    block = _build_causal_graph_block(case)
    assert "[root/" in _line(block, new_root)
    assert "[intermediate/" in _line(block, old_root)


def test_a_root_handed_to_another_hypothesis_stays_a_root():
    """A re-root may hand the old root to another hypothesis (the hand-off
    chain emission honours); it is that hypothesis's root on every path."""
    case = _case()
    deepened = _hyp("cert-manager cannot renew the gateway's client cert")
    case.hypotheses = {deepened.hypothesis_id: deepened}
    old_root, _new_root = _deepened(case, deepened)
    heir = _hyp("the secret_id expired")
    heir.root_node_id = old_root
    case.hypotheses[heir.hypothesis_id] = heir

    block = _build_causal_graph_block(case)
    assert block.count(old_root) == 2  # under both hypotheses
    assert all("[root/" in line for line in block.splitlines() if old_root in line)
