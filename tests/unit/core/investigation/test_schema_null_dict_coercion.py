"""Response fields tolerate an explicit ``null`` from the LLM.

Originally a regression for gemini-3.5-flash 500s: the model emitted
``"milestone_justifications": null`` / ``"hypotheses_to_update": null`` instead
of omitting the field, and ``default_factory=dict`` only covers the *absent*
case — so a non-Optional ``Dict`` field rejected ``None`` and 500'd the turn.

Both fields were restructured out of ``Dict`` form for strict mode (fm#1057),
and the dedicated ``_NoneTolerantDict`` coercer went with them. The tolerance
itself did not go away — it moved into ``NullTolerantModel``, which now restores
the default for ANY defaulted field the model nulls. These tests hold the
property at its new home, on the new shapes: a provider that spells "nothing" as
``null`` must not lose a turn, and must not be mistaken for one that said
something.
"""

import pytest

from faultmaven.core.investigation import schemas as s

pytestmark = pytest.mark.unit


def test_internal_reasoning_tolerates_null_milestone_justifications():
    ir = s.InternalReasoning.model_validate({"milestone_justifications": None})
    assert ir.milestone_justifications.as_dict() == {}


def test_internal_reasoning_keeps_a_real_justification():
    """Tolerance must not clobber a populated value."""
    ir = s.InternalReasoning.model_validate(
        {"milestone_justifications": {"symptom_verified": "per ev_abc123"}}
    )
    assert ir.milestone_justifications.as_dict() == {
        "symptom_verified": "per ev_abc123"
    }


def test_a_nulled_milestone_is_not_reported_as_justified():
    """The strict wire shape: every key present, null where nothing was said.

    ``as_dict`` must report that as "no justifications" — reporting it as four
    justified milestones is what would make the reasoning gate stop firing.
    """
    ir = s.InternalReasoning.model_validate(
        {
            "milestone_justifications": {
                "symptom_verified": "per ev_abc123",
                "mitigation_accepted": None,
                "mitigation_verified": None,
                "solution_accepted": None,
            }
        }
    )
    assert ir.milestone_justifications.as_dict() == {
        "symptom_verified": "per ev_abc123"
    }


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_a_blank_justification_does_not_count_as_one(blank):
    """A model forced to emit a key for an untouched milestone reaches for ``""``.

    Before strict it simply omitted the key. Treating blank as absent keeps
    "justified" meaning what it meant.
    """
    ir = s.InternalReasoning.model_validate(
        {"milestone_justifications": {"symptom_verified": blank}}
    )
    assert ir.milestone_justifications.as_dict() == {}


@pytest.mark.parametrize(
    "response_model_name",
    [
        "InvestigationResponse_Diagnosis",
        "InvestigationResponse_Treatment",
        "InvestigationResponse_General",
    ],
)
def test_state_update_tolerates_null_hypotheses_to_update(response_model_name):
    state_update_model = (
        getattr(s, response_model_name).model_fields["state_updates"].annotation
    )
    m = state_update_model.model_validate({"hypotheses_to_update": None})
    assert m.hypotheses_to_update == []
