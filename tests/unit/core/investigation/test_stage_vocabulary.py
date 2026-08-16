"""The backend carries ONE vocabulary for the investigation stage: the enum.

Prior to #1075 the stage was declared twice in the primary investigation
prompt, under the same tag name, with different values::

    STATE: INVESTIGATING                          # identity block
    CURRENT_STAGE: DIAGNOSIS                      # identity block, raw enum
    ...
    <current_stage>Investigating</current_stage>  # milestones block, display name

The second came from ``InvestigationProgress.stage_display_name``, which
mapped DIAGNOSIS to the string "Investigating" — colliding with
``CaseState.investigating``, a different axis entirely (a case's lifecycle
*state* vs its investigation *stage*). The model was therefore told the
stage was ``DIAGNOSIS`` and then, lower down, that it was ``Investigating``:
not merely ambiguous, but reading as self-contradiction.

Both guards fired together whenever the case was INVESTIGATING
(``Case.current_stage`` is never None there), so this was not an edge case.

The fix removed the display-name emission and deleted the property. Display
naming is owned by consumers — the Dashboard labels the raw enum
client-side, the same pattern ``CaseStateBadge`` uses for ``CaseState``.

These tests pin the invariant that made the bug possible: the stage must be
declared exactly once, in enum vocabulary, on both the primary and the
fallback path. A future re-introduction of a backend-side display mapping
fails here.

Run:
    pytest tests/unit/core/investigation/test_stage_vocabulary.py -v
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from faultmaven.core.investigation.prompts.context_builder import (
    build_investigation_context,
)
from faultmaven.core.investigation.prompts.templates import (
    get_fallback_prompt_for_case,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InquiryData,
    InvestigationProgress,
    InvestigationStage,
    MitigationRecord,
)

# The display strings the deleted mapping used to produce. None of them may
# reappear as a stage label anywhere the model can read.
RETIRED_DISPLAY_NAMES = ("Investigating", "Mitigating", "Resolving")


def _make_case(
    *,
    state: CaseState = CaseState.INVESTIGATING,
    stage: InvestigationStage = InvestigationStage.DIAGNOSIS,
) -> Case:
    """Build a Case whose derived stage is ``stage``.

    The stage is derived from the action-compliance gates, not set
    directly: MITIGATION when a mitigation is accepted-but-not-verified,
    TREATMENT when a solution is accepted-but-not-verified, else DIAGNOSIS.
    """
    inquiry = InquiryData()
    inquiry.proposed_problem_statement = "Checkout latency spike"
    inquiry.problem_statement_confirmed = True
    inquiry.decided_to_investigate = True

    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_test",
        organization_id="org_test",
        title="Test case",
        description="Checkout latency spike",
        state=state,
        inquiry=inquiry,
    )
    case.current_turn = 5
    if state == CaseState.INVESTIGATING:
        case.progress.symptom_verified = True
        if stage == InvestigationStage.MITIGATION:
            case.progress.mitigation = MitigationRecord(
                proposed_at_turn=case.current_turn, accepted=True
            )
        elif stage == InvestigationStage.TREATMENT:
            case.progress.solution_accepted = True
    return case


def _prompt_text(case: Case) -> str:
    """Everything the model can read on the primary path, as one string."""
    ctx = build_investigation_context(case, "user message", max_tokens=8000)
    return "\n".join(str(v) for v in ctx.values())


# ============================================================
# The stage is declared exactly once, in enum vocabulary
# ============================================================


@pytest.mark.unit
@pytest.mark.parametrize("stage", list(InvestigationStage), ids=lambda s: s.value)
def test_identity_block_declares_stage_as_enum(stage: InvestigationStage):
    """The identity block is the single declaration site, and it uses the
    raw enum value — the vocabulary the API also serves."""
    case = _make_case(stage=stage)
    assert case.current_stage == stage, "fixture did not produce the target stage"

    ctx = build_investigation_context(case, "user message", max_tokens=8000)
    assert f"CURRENT_STAGE: {stage.value.upper()}" in ctx["identity"]


@pytest.mark.unit
@pytest.mark.parametrize("stage", list(InvestigationStage), ids=lambda s: s.value)
def test_stage_declared_exactly_once_in_prompt(stage: InvestigationStage):
    """Regression for the duplicate emission (#1075).

    Two declarations under one tag name is the defect, independent of which
    words each one used. Counting them catches a re-introduction even if
    someone picks a non-colliding display string next time.
    """
    text = _prompt_text(_make_case(stage=stage))
    declarations = text.upper().count("CURRENT_STAGE")
    assert declarations == 1, (
        f"expected the stage to be declared once, found {declarations}. "
        f"The identity block owns this declaration; nothing else may repeat "
        f"it (see #1075)."
    )


@pytest.mark.unit
@pytest.mark.parametrize("stage", list(InvestigationStage), ids=lambda s: s.value)
def test_milestones_block_does_not_declare_stage(stage: InvestigationStage):
    """The milestones block carries gates and indicators, not the stage."""
    ctx = build_investigation_context(
        _make_case(stage=stage), "user message", max_tokens=8000
    )
    assert "current_stage" not in ctx["milestones"].lower()


# ============================================================
# No display vocabulary reaches the model
# ============================================================


@pytest.mark.unit
@pytest.mark.parametrize("stage", list(InvestigationStage), ids=lambda s: s.value)
def test_no_retired_display_name_in_stage_blocks(stage: InvestigationStage):
    """The retired display names must not label the stage.

    Scoped to the two blocks that describe case identity and progress:
    "Resolving" and friends are ordinary English and may legitimately
    appear in narrative prose elsewhere in the prompt.
    """
    ctx = build_investigation_context(
        _make_case(stage=stage), "user message", max_tokens=8000
    )
    blocks = ctx["identity"] + "\n" + ctx["milestones"]
    for name in RETIRED_DISPLAY_NAMES:
        assert name not in blocks, (
            f"{name!r} is a retired stage display name and must not label the "
            f"stage — it collides with the CaseState vocabulary (#1075)."
        )


@pytest.mark.unit
def test_state_and_stage_are_distinguishable():
    """The collision that opened #1075: a DIAGNOSIS case whose state is
    INVESTIGATING must not render both axes as the same word."""
    ctx = build_investigation_context(
        _make_case(stage=InvestigationStage.DIAGNOSIS),
        "user message",
        max_tokens=8000,
    )
    identity = ctx["identity"]
    assert "STATE: INVESTIGATING" in identity
    assert "CURRENT_STAGE: DIAGNOSIS" in identity


# ============================================================
# The fallback path uses the same vocabulary
# ============================================================


@pytest.mark.unit
@pytest.mark.parametrize("stage", list(InvestigationStage), ids=lambda s: s.value)
def test_fallback_prompt_uses_enum_vocabulary(stage: InvestigationStage):
    """A display name here would mean the model reads ``DIAGNOSIS`` on the
    primary path and something else on the fallback — two names for one
    fact, surfacing exactly when a turn has already degraded."""
    case = _make_case(stage=stage)
    prompt = get_fallback_prompt_for_case(case, "user message")

    assert f"STAGE: {stage.value.upper()}" in prompt
    for name in RETIRED_DISPLAY_NAMES:
        assert name not in prompt


# ============================================================
# The deleted property stays deleted
# ============================================================


@pytest.mark.unit
def test_stage_display_name_property_is_gone():
    """``stage_display_name`` was a backend-side display mapping that never
    reached the API wire — only prompts. Its docstring claimed it was
    "user-facing", which is very likely how the collision survived review.
    Consumers own display naming now; this must not come back."""
    assert not hasattr(InvestigationProgress(), "stage_display_name"), (
        "stage_display_name is back. The backend carries one stage "
        "vocabulary — the enum. Label it in the consumer instead (#1075)."
    )
