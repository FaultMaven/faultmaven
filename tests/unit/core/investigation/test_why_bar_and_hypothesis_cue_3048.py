"""The why-stopping bar is stated as what the record shows, at every site that
states it, and a verified case holding causal evidence but no hypothesis is
asked for one (#3048)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.narration.notices import NoticeCode, render_notice
from faultmaven.core.investigation.narration.prompt_state import focus_selection
from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    _RECOVERY_NOTES,
    _VERDICT_REFUSED,
    _WHY_OPEN,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    _focus_lines,
    _hypothesis_cue_emphasis,
    get_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.blocks import (
    _ROOT_CAUSE_TEST_BLOCK,
)
from faultmaven.core.investigation.prompts.templates.diagnosis import (
    _CHAIN_EMISSION_BLOCK,
    rca_diagnosis_block,
)
from faultmaven.core.investigation.schemas import (
    HypothesisToAdd,
    HypothesisUpdate,
)
from faultmaven.modules.case.contracts import (
    CausalNode,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisEvidenceLink,
    HypothesisGenerationMode,
    HypothesisState,
    NodeState,
    NodeType,
    ProblemStatus,
    ValidationMethod,
)
from tests.case_builders import investigating_case
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN
from tests.unit.core.investigation.prompt_state_reads import selections

pytestmark = pytest.mark.unit

KEY = "how the state came to be"
OLD = (
    "nothing renews",
    "re-creates it",
    "nothing will produce",
    "have not yet seen what produced",
    "nothing controllable",
    "nothing will disable",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _field(model, name: str) -> str:
    return model.model_fields[name].description


# ---------------------------------------------------------------------------
# C1: one wording, at every site
# ---------------------------------------------------------------------------


def test_the_investigation_base_states_the_bar_as_the_record():
    text = _flat(_ROOT_CAUSE_TEST_BLOCK)
    assert "The record decides (b)" in text
    assert "came to be" in text
    assert "no row on record shows a mechanism that produces it again" in text


def test_the_schema_descriptions_state_the_bar_as_the_record():
    for model in (HypothesisToAdd, HypothesisUpdate):
        for name in ("root_is_terminal", "terminal_basis_evidence_ids"):
            text = _field(model, name)
            assert not any(old in text for old in OLD)
    add = _field(HypothesisToAdd, "root_is_terminal")
    assert "how the state came to be" in add
    assert "no row on record shows a mechanism that produces it again" in add
    assert "reopens the verdict" in add
    assert "Proving that no other writer exists is not asked" in add
    assert "how this root state came to be" in _field(
        HypothesisToAdd, "terminal_basis_evidence_ids"
    )


def test_the_diagnosis_prompt_states_the_bar_as_the_record():
    for text in (
        _flat(_CHAIN_EMISSION_BLOCK),
        _flat(rca_diagnosis_block(DEFAULT_GIVEN)),
    ):
        assert not any(old in text for old in OLD)
    text = _flat(_CHAIN_EMISSION_BLOCK)
    assert KEY in text
    assert "no row on record shows a mechanism that produces it again" in text
    assert "no row shows a mechanism that repeats it" in text


def test_the_causal_graph_cues_state_the_bar_as_the_record():
    for cue in (_RECOVERY_NOTES[_WHY_OPEN], _RECOVERY_NOTES[_VERDICT_REFUSED]):
        assert KEY in _flat(cue)
        assert not any(old in cue for old in OLD)


def test_the_refused_notice_states_the_bar_as_the_record():
    notice = " ".join(
        render_notice(
            NoticeCode.ROOT_TERMINAL_REFUSED,
            {"node_id": "cn_1", "posited_turn": 2, "refused": []},
            None,
            [],
        )
    )
    assert "showing how its state came to be" in notice
    assert not any(old in notice for old in OLD)


def test_the_rendered_prompt_carries_no_old_wording():
    case = investigating_case(
        statement="Gateway calls fail",
        symptom="503s",
        current_turn=4,
        problem_status=ProblemStatus.VERIFIED,
    )
    prompt = _flat(get_prompt_for_case(case, "what now?", given_tools=DEFAULT_GIVEN))
    assert KEY in prompt
    assert not any(old in prompt for old in OLD)


# ---------------------------------------------------------------------------
# Item 7: the progress-transparency notice while a validated root is held open
# ---------------------------------------------------------------------------


def _stalled_case(*, held_open: bool, root: NodeState | None = None):
    """``root``: the state of the root of a standing hypothesis's chain."""
    case = investigating_case(
        statement="Gateway calls fail",
        symptom="503s",
        current_turn=4,
        problem_status=ProblemStatus.VERIFIED,
    )
    case.progress.cause_identification_held_open = held_open
    if root is not None:
        hyp = _hyp()
        node = CausalNode(
            node_id="cn_0000000000aa",
            statement="autoRenew was disabled",
            node_type=NodeType.ROOT,
            node_state=root,
            validation_method=(
                ValidationMethod.EMPIRICAL
                if root == NodeState.VALIDATED
                else ValidationMethod.NONE
            ),
            belief=0.5,
            actionable=True,
            generated_at_turn=1,
        )
        hyp.root_node_id = node.node_id
        case.causal_nodes = {node.node_id: node}
        case.hypotheses = {hyp.hypothesis_id: hyp}
    return case


def _injection(case) -> str:
    monitor = ProgressMonitor(transparency_threshold=3)
    return monitor._build_prompt_injection(
        stage=case.current_stage,
        milestone_name="root_cause_identified",
        milestone_info={
            "description": "Validate a hypothesis with supporting causal evidence"
        },
        investigative_turns=3,
        repair=None,
        pending_text=monitor._held_open_pending_text(case, "root_cause_identified"),
    )


def test_a_held_open_validated_root_is_told_its_verdict_is_pending():
    text = _injection(_stalled_case(held_open=True, root=NodeState.VALIDATED))
    assert "why-stopping verdict is pending" in text
    assert "root_is_terminal true" in text
    assert "recorded since the root was posited" in text
    assert "Validate a hypothesis" not in text


def test_an_unvalidated_held_root_keeps_the_generic_notice():
    """The flag also holds for an active root the model marked False."""
    case = _stalled_case(held_open=True, root=NodeState.CANDIDATE)
    assert case.progress.cause_identification_held_open
    text = _injection(case)
    assert "verdict is pending" not in text
    assert "Validate a hypothesis with supporting causal evidence" in text


def test_a_case_without_a_held_root_keeps_the_generic_notice():
    text = _injection(_stalled_case(held_open=False, root=NodeState.VALIDATED))
    assert "Validate a hypothesis with supporting causal evidence" in text
    assert "verdict is pending" not in text


def test_the_pending_text_is_for_root_cause_identified_only():
    monitor = ProgressMonitor()
    case = _stalled_case(held_open=True, root=NodeState.VALIDATED)
    assert monitor._held_open_pending_text(case, "solution_proposed") is None


# ---------------------------------------------------------------------------
# A4: the hypothesis cue
# ---------------------------------------------------------------------------

CUE = "Causal evidence is on record but no hypothesis stands"


def _row(category, *, by="llm") -> Evidence:
    return Evidence(
        summary="the issuer config shows autoRenew=false",
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_0123456789ab",
        collected_by=by,
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def _hyp(state=HypothesisState.ACTIVE) -> Hypothesis:
    return Hypothesis(
        statement="autoRenew was disabled",
        category=HypothesisCategory.CONFIG,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        generated_at_turn=3,
        rationale="r",
        state=state,
    )


def _lines(case, *, steered=True, zoned=True) -> str:
    outcome, fresh = selections(case)
    return _focus_lines(
        focus_selection(outcome, fresh, 4, steered=steered, zoned=zoned)
    )


def _held_by(hyp: Hypothesis, row: Evidence, stance=EvidenceStance.REFUTES):
    hyp.evidence_links.append(
        HypothesisEvidenceLink(
            hypothesis_id=hyp.hypothesis_id,
            evidence_id=row.evidence_id,
            stance=stance,
            reasoning="r",
            stance_confidence=0.9,
        )
    )
    return hyp


def _case(status=ProblemStatus.VERIFIED, rows=(), hyps=()):
    case = investigating_case(
        statement="Gateway calls fail",
        symptom="503s",
        current_turn=4,
        problem_status=status,
    )
    case.evidence = list(rows)
    case.hypotheses = {h.hypothesis_id: h for h in hyps}
    return case


CAUSAL = EvidenceCategory.CAUSAL_EVIDENCE


@pytest.mark.parametrize(
    "case, cue",
    [
        (_case(rows=[_row(CAUSAL)]), True),
        (_case(status=ProblemStatus.UNVERIFIED, rows=[_row(CAUSAL)]), False),
        (_case(rows=[_row(CAUSAL)], hyps=[_hyp()]), False),
        (_case(rows=[_row(EvidenceCategory.SYMPTOM_EVIDENCE)]), False),
        (_case(rows=[_row(CAUSAL, by="engine")]), False),
        (_case(), False),
        # A hypothesis that no longer stands does not count as one.
        (_case(rows=[_row(CAUSAL)], hyps=[_hyp(HypothesisState.RETIRED)]), True),
    ],
    ids=[
        "verified-no-hyp-causal-row",
        "unverified",
        "hypothesis-stands",
        "symptom-rows-only",
        "engine-rows-only",
        "no-rows",
        "retired-hypothesis",
    ],
)
def test_the_cue_is_verified_no_hypothesis_and_a_causal_row(case, cue):
    assert (CUE in _lines(case)) is cue


def test_rows_that_refuted_a_hypothesis_do_not_trigger_the_cue():
    row = _row(CAUSAL)
    refuted = _held_by(_hyp(HypothesisState.RETIRED), row)
    assert CUE not in _lines(_case(rows=[row], hyps=[refuted]))
    # An unlinked causal row beside it does.
    assert CUE in _lines(_case(rows=[row, _row(CAUSAL)], hyps=[refuted]))


def test_the_cue_is_for_the_cause_zones_only():
    case = _case(rows=[_row(CAUSAL)])
    assert CUE in _lines(case)
    for milestone in ("root_cause_identified", "solution_proposed"):
        outcome, fresh = selections(case)
        outcome = outcome.model_copy(
            update={
                "milestones_shown": [
                    "symptom_verified",
                    "root_cause_identified",
                    *(
                        ["solution_proposed"]
                        if milestone == "solution_proposed"
                        else []
                    ),
                ]
            }
        )
        text = _focus_lines(
            focus_selection(outcome, fresh, 4, steered=True, zoned=True)
        )
        assert CUE not in text


def test_the_cue_is_for_a_steered_diagnosis_turn():
    case = _case(rows=[_row(CAUSAL)])
    assert CUE not in _lines(case, steered=False)
    assert CUE not in _lines(case, zoned=False)


def test_the_cue_names_the_fields_to_fill():
    text = _hypothesis_cue_emphasis(True)
    assert "hypotheses_to_add" in text and "root_node_ref" in text
    assert _hypothesis_cue_emphasis(False) == ""
