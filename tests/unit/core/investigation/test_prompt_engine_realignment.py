"""§7.7 / INV-35 (#656) — prompt/engine realignment for cause identification.

Three pinned disciplines:

  * **No self-certification signal.** Identification (``cause_state=IDENTIFIED``)
    is engine-derived from a validated, uncontested chain root (§9.2). There is
    no LLM-settable ``root_cause_identified`` boolean, and the DIAGNOSIS prompt no
    longer teaches the LLM to set one. This is the *composition-seam* guard: the
    schema and the prompt cannot silently re-diverge (the drift that left the
    boolean instruction alive after the engine stopped reading it).
  * **The conclusion names its cause.** An LLM ``RootCauseConclusion`` carries
    ``names_root_node_id`` (the ``cn_`` root node it emits during chain
    construction); ``link_llm_rcc_to_cause`` attributes it to the hypothesis
    rooted there authoritatively (tier 1), falling back to the lexical scan
    (tier 2) — both guarded by the SAME standing-hypothesis + overlap discipline,
    so neither links a valid conclusion to a refuted or unrelated cause.
  * **KB-remediation warm-up fires on the cause_state→IDENTIFIED edge.**
"""

from unittest.mock import DEFAULT, patch

import pytest

from faultmaven.core.investigation.causal_graph import link_llm_rcc_to_cause
from faultmaven.core.investigation.milestone_engine import (
    _kb_prefetch_query_on_identification,
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
    CauseState,
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

# A cause statement / conclusion text that lexically matches itself (STRONG) and
# shares no substantive tokens with the "disk full" decoy below.
_POOL_LEAK = (
    "the deploy removed the connection release call so pool connections "
    "leak until exhaustion"
)
_DISK_FULL = "the data volume disk filled to one hundred percent"


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
    assert "root_cause_identified=True" not in templates.TREATMENT_INSTRUCTIONS


def test_rcc_schemas_carry_names_root_node_id():
    """Both the LLM-emitted update and the stored domain model expose the
    authoritative attribution hint, and TREATMENT teaches it."""
    assert "names_root_node_id" in RootCauseConclusionUpdate.model_fields
    assert "names_root_node_id" in RootCauseConclusion.model_fields
    assert "names_root_node_id" in templates.TREATMENT_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Authoritative link (tier 1) + lexical fallback (tier 2), one guarded function
# ---------------------------------------------------------------------------


def _hyp(
    root_node_id,
    hypothesis_id,
    *,
    statement=_POOL_LEAK,
    state=HypothesisState.ACTIVE,
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        statement=statement,
        category=HypothesisCategory.DATABASE,
        state=state,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=root_node_id,
        generated_at_turn=1,
        refutation_reason=("disproven" if state == HypothesisState.REFUTED else None),
    )


def _case(hyps=None) -> Case:
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        enterprise_id="o",
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


def _llm_rcc(
    *, root_cause=_POOL_LEAK, names_root_node_id=None, vhid=None, determined_by="agent"
):
    return RootCauseConclusion(
        root_cause=root_cause,
        mechanism="how it produced the symptom",
        likelihood=0.85,
        confidence_level=ConfidenceLevel.from_score(0.85),
        validated_hypothesis_id=vhid,
        names_root_node_id=names_root_node_id,
        determined_by=determined_by,
    )


def _counters():
    return patch.multiple(
        "faultmaven.core.investigation.causal_graph",
        llm_rcc_cause_named_total=DEFAULT,
        llm_rcc_cause_linked_total=DEFAULT,
    )


def test_tier1_named_node_links_authoritatively():
    hyp = _hyp("cn_root", "hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(names_root_node_id="cn_root")
    with _counters() as m:
        assert link_llm_rcc_to_cause(case) is True
    assert case.root_cause_conclusion.validated_hypothesis_id == hyp.hypothesis_id
    assert m["llm_rcc_cause_named_total"].inc.call_count == 1
    assert m["llm_rcc_cause_linked_total"].inc.call_count == 0


def test_tier1_disambiguates_where_lexical_cannot():
    """Two standing hypotheses with the SAME statement: the lexical scan sees two
    ≥AMBIGUOUS contenders and declines, but the named root node picks THE one."""
    h1 = _hyp("cn_a", "hyp_0000000000aa")
    h2 = _hyp("cn_b", "hyp_0000000000bb")  # identical statement, different root
    case = _case(hyps=[h1, h2])
    case.root_cause_conclusion = _llm_rcc(names_root_node_id="cn_a")
    with _counters() as m:
        assert link_llm_rcc_to_cause(case) is True
    assert case.root_cause_conclusion.validated_hypothesis_id == "hyp_0000000000aa"
    assert m["llm_rcc_cause_named_total"].inc.call_count == 1


def test_tier1_refuted_named_hypothesis_declines_no_false_link():
    """The critical guard: a named node rooting a REFUTED hypothesis must NOT
    link — else retract_disconfirmed_rcc would wipe the just-authored valid
    conclusion the same recompute (NO-COLLAPSE)."""
    hyp = _hyp("cn_root", "hyp_0000000000aa", state=HypothesisState.REFUTED)
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(names_root_node_id="cn_root")
    assert link_llm_rcc_to_cause(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id is None


def test_tier1_stale_or_wrong_id_no_overlap_declines_then_tier2_recovers():
    """A named id pointing at a standing-but-UNRELATED hypothesis fails the
    overlap rail (tier 1 declines); the lexical fallback then links the
    text-matched hypothesis correctly."""
    h_named = _hyp("cn_x", "hyp_0000000000cc", statement=_DISK_FULL)  # wrong id target
    h_text = _hyp("cn_y", "hyp_0000000000dd", statement=_POOL_LEAK)  # text match
    case = _case(hyps=[h_named, h_text])
    case.root_cause_conclusion = _llm_rcc(
        root_cause=_POOL_LEAK, names_root_node_id="cn_x"
    )
    with _counters() as m:
        assert link_llm_rcc_to_cause(case) is True
    assert case.root_cause_conclusion.validated_hypothesis_id == "hyp_0000000000dd"
    assert m["llm_rcc_cause_named_total"].inc.call_count == 0
    assert m["llm_rcc_cause_linked_total"].inc.call_count == 1


def test_tier1_multi_match_declines():
    """Two hypotheses rooted at the same node is degenerate — decline (T1)."""
    h1 = _hyp("cn_root", "hyp_0000000000aa")
    h2 = _hyp("cn_root", "hyp_0000000000bb")
    case = _case(hyps=[h1, h2])
    case.root_cause_conclusion = _llm_rcc(names_root_node_id="cn_root")
    # tier 1 multi-match declines; tier 2 also declines (two ≥AMBIGUOUS) → no link
    assert link_llm_rcc_to_cause(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id is None


def test_tier2_id_less_conclusion_uses_lexical_fallback():
    hyp = _hyp("cn_root", "hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(names_root_node_id=None)
    with _counters() as m:
        assert link_llm_rcc_to_cause(case) is True
    assert case.root_cause_conclusion.validated_hypothesis_id == hyp.hypothesis_id
    assert m["llm_rcc_cause_linked_total"].inc.call_count == 1
    assert m["llm_rcc_cause_named_total"].inc.call_count == 0


def test_engine_authored_rcc_is_never_linked():
    hyp = _hyp("cn_root", "hyp_0000000000aa")
    case = _case(hyps=[hyp])
    case.root_cause_conclusion = _llm_rcc(
        names_root_node_id="cn_root", determined_by=_ENGINE_RCC_AUTHOR
    )
    assert link_llm_rcc_to_cause(case) is False


def test_existing_live_link_is_left_stable():
    hyp = _hyp("cn_root", "hyp_0000000000aa")
    other = _hyp("cn_other", "hyp_0000000000bb", statement=_DISK_FULL)
    case = _case(hyps=[hyp, other])
    case.root_cause_conclusion = _llm_rcc(
        names_root_node_id="cn_other", vhid="hyp_0000000000aa"
    )
    assert link_llm_rcc_to_cause(case) is False
    assert case.root_cause_conclusion.validated_hypothesis_id == "hyp_0000000000aa"


# ---------------------------------------------------------------------------
# KB-remediation prefetch fires on the cause_state→IDENTIFIED edge
# ---------------------------------------------------------------------------


class _RCC:
    def __init__(self, root_cause):
        self.root_cause = root_cause


class _WC:
    def __init__(self, statement):
        self.statement = statement


def test_kb_prefetch_query_fires_on_rising_edge():
    q = _kb_prefetch_query_on_identification(
        CauseState.UNKNOWN, CauseState.IDENTIFIED, _RCC("bad pool config"), None
    )
    assert q == "bad pool config"


def test_kb_prefetch_query_none_when_already_identified():
    """No re-fire when cause_state was already IDENTIFIED last turn."""
    q = _kb_prefetch_query_on_identification(
        CauseState.IDENTIFIED, CauseState.IDENTIFIED, _RCC("bad pool config"), None
    )
    assert q is None


def test_kb_prefetch_query_none_when_not_identified():
    q = _kb_prefetch_query_on_identification(
        CauseState.UNKNOWN, CauseState.CANDIDATES, _RCC("bad pool config"), None
    )
    assert q is None


def test_kb_prefetch_query_prefers_rcc_then_working_conclusion():
    # RCC wins when present
    q = _kb_prefetch_query_on_identification(
        CauseState.CANDIDATES, CauseState.IDENTIFIED, _RCC("rcc cause"), _WC("wc cause")
    )
    assert q == "rcc cause"
    # falls back to working conclusion when no RCC
    q2 = _kb_prefetch_query_on_identification(
        CauseState.CANDIDATES, CauseState.IDENTIFIED, None, _WC("wc cause")
    )
    assert q2 == "wc cause"
    # None when neither carries cause text
    q3 = _kb_prefetch_query_on_identification(
        CauseState.CANDIDATES, CauseState.IDENTIFIED, None, None
    )
    assert q3 is None
