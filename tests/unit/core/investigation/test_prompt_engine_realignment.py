"""§7.7 / INV-35 (#656 P2.4) — prompt/engine realignment for cause identification.

Two pinned disciplines:

  * **No self-certification signal.** Identification (``cause_state=IDENTIFIED``)
    is engine-derived from a validated, uncontested chain root (§9.2). There is
    no LLM-settable ``root_cause_identified`` boolean, and the DIAGNOSIS prompt no
    longer teaches the LLM to set one. This is the *composition-seam* guard: the
    schema and the prompt cannot silently re-diverge (the drift that left the
    boolean instruction alive after the engine stopped reading it).
  * **The conclusion names its cause.** An LLM ``RootCauseConclusion`` carries
    ``names_root_node_id`` (the ``cn_`` root node it emits during chain
    construction); ``link_llm_rcc_by_named_node`` attributes it to the hypothesis
    rooted there exactly, the authoritative link that runs BEFORE the lexical
    fallback (``link_llm_rcc_to_cause``).
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.causal_graph import (
    link_llm_rcc_by_named_node,
    link_llm_rcc_to_cause,
)
from faultmaven.core.investigation.prompts import templates
from faultmaven.core.investigation.schemas import (
    MilestoneUpdates,
    RootCauseConclusionUpdate,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    ConfidenceLevel,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    ProblemVerification,
    RootCauseConclusion,
)

pytestmark = pytest.mark.unit

_ENGINE_RCC_AUTHOR = "engine:chain_validation"


# ---------------------------------------------------------------------------
# Composition-seam contract: no self-certification signal (schema + prompt)
# ---------------------------------------------------------------------------


def test_milestone_updates_has_no_root_cause_identified_field():
    """The decommissioned self-certification boolean is gone from the LLM
    schema — identification is engine-derived (INV-35)."""
    assert "root_cause_identified" not in MilestoneUpdates.model_fields


def test_diagnosis_prompt_does_not_teach_setting_root_cause_identified():
    """The prompt must not instruct the LLM to set the removed boolean; if it
    did, the schema/prompt would re-diverge (the split-brain this closes)."""
    for block in (
        templates._HYPOTHESIS_EVIDENCE_ORDERING_BLOCK,
        templates._DIAGNOSIS_ZONES_PREAMBLE,
    ):
        assert "root_cause_identified" not in block
    # The assembled TREATMENT stage must not carry the self-certification cue.
    assert "root_cause_identified=True" not in templates.TREATMENT_INSTRUCTIONS


def test_rcc_schemas_carry_names_root_node_id():
    """Both the LLM-emitted update and the stored domain model expose the
    authoritative attribution hint."""
    assert "names_root_node_id" in RootCauseConclusionUpdate.model_fields
    assert "names_root_node_id" in RootCauseConclusion.model_fields
    # Treatment prompt teaches it.
    assert "names_root_node_id" in templates.TREATMENT_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Authoritative link — names_root_node_id → validated_hypothesis_id
# ---------------------------------------------------------------------------


def _hyp(root_node_id, hypothesis_id, *, state=HypothesisState.ACTIVE) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        statement="the deploy leaked pool connections until exhaustion",
        category=HypothesisCategory.DATABASE,
        state=state,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=root_node_id,
        generated_at_turn=1,
    )


def _case(hyps=None) -> Case:
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="checkout orders failing with 500s",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout orders failing with 500s",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.hypotheses = {h.hypothesis_id: h for h in (hyps or [])}
    case.progress.symptom_verified = True
    return case


def _llm_rcc(*, names_root_node_id=None, vhid=None, determined_by="agent"):
    likelihood = 0.85
    return RootCauseConclusion(
        root_cause="a wholly unrelated phrasing that would not lexically match",
        mechanism="how it produced the symptom",
        likelihood=likelihood,
        confidence_level=ConfidenceLevel.from_score(likelihood),
        validated_hypothesis_id=vhid,
        names_root_node_id=names_root_node_id,
        determined_by=determined_by,
    )


def test_named_node_links_authoritatively():
    hyp = _hyp("cn_root", "hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(names_root_node_id="cn_root")
    with patch(
        "faultmaven.core.investigation.causal_graph.llm_rcc_cause_named_total"
    ) as counter:
        assert link_llm_rcc_by_named_node(case) is True
    assert case.root_cause_conclusion.validated_hypothesis_id == hyp.hypothesis_id
    assert counter.inc.call_count == 1


def test_named_node_no_match_declines():
    """A named node matching no hypothesis links nothing (falls to fallback)."""
    hyp = _hyp("cn_root", "hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(names_root_node_id="cn_absent")
    assert link_llm_rcc_by_named_node(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id is None


def test_named_node_ambiguous_multi_match_declines():
    """Two hypotheses rooted at the same node is degenerate — decline (T1)."""
    h1 = _hyp("cn_root", "hyp_0000000000aa")
    h2 = _hyp("cn_root", "hyp_0000000000bb")
    case = _case(hyps=[h1, h2])
    case.root_cause_conclusion = _llm_rcc(names_root_node_id="cn_root")
    assert link_llm_rcc_by_named_node(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id is None


def test_named_link_precedes_and_short_circuits_lexical_scan():
    """When the named link resolves, the lexical fallback is a no-op — the RCC
    text here deliberately does NOT lexically match the hypothesis."""
    hyp = _hyp("cn_root", "hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(names_root_node_id="cn_root")
    assert link_llm_rcc_by_named_node(case) is True
    linked = case.root_cause_conclusion.validated_hypothesis_id
    # Fallback keeps the existing link (already points at a present hypothesis).
    assert link_llm_rcc_to_cause(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id == linked


def test_id_less_conclusion_declines_named_link():
    """No names_root_node_id → the authoritative pass is a no-op; the id-less
    conclusion is left for the lexical fallback / stays the residual."""
    hyp = _hyp("cn_root", "hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(names_root_node_id=None)
    assert link_llm_rcc_by_named_node(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id is None


def test_engine_authored_rcc_is_never_named_linked():
    hyp = _hyp("cn_root", "hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(
        names_root_node_id="cn_root", determined_by=_ENGINE_RCC_AUTHOR
    )
    assert link_llm_rcc_by_named_node(case) is False


def test_existing_live_link_is_left_stable():
    """An RCC already linked to a present hypothesis is not re-pointed."""
    hyp = _hyp("cn_root", "hyp_0000000000aa")
    other = _hyp("cn_other", "hyp_0000000000bb")
    case = _case(hyps=[hyp, other])
    case.root_cause_conclusion = _llm_rcc(
        names_root_node_id="cn_other", vhid="hyp_0000000000aa"
    )
    assert link_llm_rcc_by_named_node(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id == "hyp_0000000000aa"
