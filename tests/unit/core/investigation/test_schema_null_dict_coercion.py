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


@pytest.mark.parametrize(
    "emitted,expected",
    [
        (
            ["47 errors in ev_abc", "ev_def confirms"],
            "47 errors in ev_abc; ev_def confirms",
        ),
        (True, "True"),
        (3, "3"),
    ],
)
def test_a_non_string_justification_does_not_500_the_turn(emitted, expected):
    """The narrowing that made the schema strict-representable must not become a
    new unrecoverable shape failure.

    ``milestone_justifications`` used to be ``Dict[str, Any]``, under which any
    value validated. Declaring four ``Optional[str]`` fields narrowed that, and
    the resulting error is the one shape the never-500 backstop cannot repair:
    the loc (``internal_reasoning.milestone_justifications.<name>``) carries no
    list index, so nothing is prunable; blanking ``state_updates`` leaves
    ``internal_reasoning`` just as invalid; and the ``agent_response`` rung does
    not fire when the model DID answer. The turn 500s.
    """
    ir = s.InternalReasoning.model_validate(
        {"milestone_justifications": {"symptom_verified": emitted}}
    )
    assert ir.milestone_justifications.as_dict() == {"symptom_verified": expected}


def test_a_non_string_justification_survives_the_real_backstop():
    """The property at the layer that actually 500s — the guard above is a unit
    check on the coercion; this one proves the turn is preserved end to end."""
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    engine = MilestoneEngine.__new__(MilestoneEngine)
    parsed = engine._validate_with_degradation(
        {
            "agent_response": "Symptom confirmed.",
            "internal_reasoning": {
                "milestone_justifications": {
                    "symptom_verified": ["47 errors in ev_abc"]
                }
            },
            "state_updates": {
                "milestones": {"symptom_verified": True},
                "evidence_to_add": [
                    {
                        "summary": "47 connection errors",
                        "category": "symptom_evidence",
                        "source_type": "user_description",
                        "extract": "connection refused x47",
                        "likelihood": 0.9,
                    }
                ],
            },
        },
        s.InvestigationResponse_Diagnosis,
    )

    # Not merely "no exception": the turn's state must still be THERE. The
    # backstop's own fallback rung would have answered without raising while
    # discarding every state update.
    assert len(parsed.state_updates.evidence_to_add) == 1
    assert parsed.internal_reasoning.milestone_justifications.as_dict() == {
        "symptom_verified": "47 errors in ev_abc"
    }
