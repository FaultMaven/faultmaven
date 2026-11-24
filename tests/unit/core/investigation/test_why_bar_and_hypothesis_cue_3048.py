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
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    ProblemStatus,
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


def _stalled_case(*, held_open: bool):
    case = investigating_case(
        statement="Gateway calls fail",
        symptom="503s",
        current_turn=4,
        problem_status=ProblemStatus.VERIFIED,
    )
    case.progress.cause_identification_held_open = held_open
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


def test_a_held_open_root_is_told_its_verdict_is_pending():
    text = _injection(_stalled_case(held_open=True))
    assert "why-stopping verdict is pending" in text
    assert "root_is_terminal true" in text
    assert "Validate a hypothesis" not in text


def test_a_case_without_a_held_root_keeps_the_generic_notice():
    text = _injection(_stalled_case(held_open=False))
    assert "Validate a hypothesis with supporting causal evidence" in text
    assert "verdict is pending" not in text


def test_the_pending_text_is_for_root_cause_identified_only():
    monitor = ProgressMonitor()
    case = _stalled_case(held_open=True)
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


def test_the_cue_is_for_a_steered_diagnosis_turn():
    case = _case(rows=[_row(CAUSAL)])
    assert CUE not in _lines(case, steered=False)
    assert CUE not in _lines(case, zoned=False)


def test_the_cue_names_the_fields_to_fill():
    text = _hypothesis_cue_emphasis(True)
    assert "hypotheses_to_add" in text and "root_node_ref" in text
    assert _hypothesis_cue_emphasis(False) == ""
