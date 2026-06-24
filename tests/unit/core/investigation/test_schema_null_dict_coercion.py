"""Required-Dict response fields tolerate an explicit ``null`` from the LLM.

Regression for the gemini-3.5-flash 500s: the model emitted
``"milestone_justifications": null`` / ``"hypotheses_to_update": null`` instead
of omitting the field, and ``default_factory=dict`` only covers the *absent*
case — so a non-Optional ``Dict`` field rejected ``None`` and 500'd the turn.
``_NoneTolerantDict`` (BeforeValidator) coerces ``None`` -> ``{}`` so the shape
variant is tolerated provider-agnostically.
"""

import pytest

from faultmaven.core.investigation import schemas as s

pytestmark = pytest.mark.unit


def test_internal_reasoning_coerces_null_milestone_justifications():
    ir = s.InternalReasoning.model_validate({"milestone_justifications": None})
    assert ir.milestone_justifications == {}


def test_internal_reasoning_keeps_a_real_milestone_justifications_dict():
    # Coercion must not clobber a populated dict.
    ir = s.InternalReasoning.model_validate(
        {"milestone_justifications": {"symptom_verified": "per ev_abc123"}}
    )
    assert ir.milestone_justifications == {"symptom_verified": "per ev_abc123"}


@pytest.mark.parametrize(
    "response_model_name",
    [
        "InvestigationResponse_Diagnosis",
        "InvestigationResponse_Treatment",
        "InvestigationResponse_General",
    ],
)
def test_state_update_coerces_null_hypotheses_to_update(response_model_name):
    state_update_model = (
        getattr(s, response_model_name).model_fields["state_updates"].annotation
    )
    m = state_update_model.model_validate({"hypotheses_to_update": None})
    assert m.hypotheses_to_update == {}
