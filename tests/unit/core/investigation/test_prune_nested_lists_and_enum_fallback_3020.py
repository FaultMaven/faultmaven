"""A bad value costs only the entry that holds it (#3020, #3021).

- #3020: an error in a list nested in a list entry prunes the deepest entry
  that holds it, and a list path the prune rung cannot walk reports its errors
  as unhandled instead of losing them. A prune that fails the model holding
  the value (a fulfilled need left with no id) is pruned again, so the turn
  keeps every other update.
- #3021: a ``state_updates`` enum value with no close match is left as sent,
  so the prune rung takes its entry and the turn is marked ``PARTIAL``; it is
  never applied as the enum's first member. Outside ``state_updates`` the
  fallback is unchanged.

Everything runs through the real ladder; only the outcome counter is doubled.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from faultmaven.core.investigation.milestone_engine import (
    structured_output as so,
)
from faultmaven.core.investigation.milestone_engine.response_synthesis import (
    state_drop_of,
)
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
    StateDrop,
)

pytestmark = pytest.mark.unit

NEED_ID = "need_aaaaaaaaaaaa"
EV_ID = "ev_aaaaaaaaaaaa"
JOURNAL = {"entry_type": "decision", "content": "Check the audience claim next."}


def _ladder(body):
    """Validate *body* through the ladder; return (parsed, outcomes)."""
    with patch.object(so, "schema_validation_total") as outcomes:
        parsed = so._validate_with_degradation(body, InvestigationResponse_Diagnosis)
    return parsed, [c.kwargs["outcome"] for c in outcomes.labels.call_args_list]


def _parse_text(body, schema=InvestigationResponse_Diagnosis):
    """The text parser: the enum fixer, then the ladder (a real caller)."""
    with patch.object(so, "schema_validation_total") as outcomes:
        parsed = so._parse_text_as_schema(json.dumps(body), schema)
    return parsed, [c.kwargs["outcome"] for c in outcomes.labels.call_args_list]


# ---------------------------------------------------------------------------
# #3020: a list nested in a list entry.
# ---------------------------------------------------------------------------


def test_a_bad_id_in_a_need_update_costs_that_id_alone():
    parsed, outcomes = _ladder(
        {
            "agent_response": "Recorded.",
            "state_updates": {
                "evidence_need_updates": [
                    {"need_id": NEED_ID, "fulfilling_evidence_ids": [EV_ID, {"a": 1}]}
                ],
                "journal_entries": [JOURNAL],
            },
        }
    )
    assert outcomes == ["pruned"]
    assert state_drop_of(parsed) is StateDrop.PARTIAL
    (need,) = parsed.state_updates.evidence_need_updates
    assert need.need_id == NEED_ID
    assert need.fulfilling_evidence_ids == [EV_ID]
    assert [j.content for j in parsed.state_updates.journal_entries] == [
        JOURNAL["content"]
    ]


def test_a_fulfilled_need_whose_only_id_is_bad_costs_the_need_alone():
    """The issue's body: the id is pruned, the need then fails (fulfilled with
    no id) and is pruned on the next pass; the sibling update stays."""
    parsed, outcomes = _ladder(
        {
            "agent_response": "Recorded.",
            "state_updates": {
                "evidence_need_updates": [
                    {
                        "need_id": NEED_ID,
                        "state": "fulfilled",
                        "fulfilling_evidence_ids": [{"a": 1}],
                    }
                ],
                "journal_entries": [JOURNAL],
            },
        }
    )
    assert outcomes == ["pruned"]
    assert state_drop_of(parsed) is StateDrop.PARTIAL
    assert parsed.state_updates.evidence_need_updates == []
    assert [j.content for j in parsed.state_updates.journal_entries] == [
        JOURNAL["content"]
    ]


def test_an_entry_and_an_id_inside_a_later_entry_are_each_pruned_alone():
    """The inner prune runs first, so removing entry 0 never sends the inner
    path to the wrong entry."""
    parsed, outcomes = _ladder(
        {
            "agent_response": "Recorded.",
            "state_updates": {
                "evidence_need_updates": [
                    {"state": "fulfilled"},  # create with no purpose: invalid
                    {"need_id": NEED_ID, "fulfilling_evidence_ids": [{"a": 1}]},
                ]
            },
        }
    )
    assert outcomes == ["pruned"]
    assert state_drop_of(parsed) is StateDrop.PARTIAL
    (need,) = parsed.state_updates.evidence_need_updates
    assert need.need_id == NEED_ID
    assert need.fulfilling_evidence_ids == []


def test_a_list_path_it_cannot_walk_is_reported_as_unhandled():
    """A defensive guard: no engine schema reaches it today, since every error
    loc names a path in the body it came from. Its errors are never lost."""
    body = {
        "agent_response": "Recorded.",
        "state_updates": {
            "evidence_need_updates": [
                {"need_id": NEED_ID, "fulfilling_evidence_ids": [{"a": 1}]}
            ]
        },
    }
    with pytest.raises(ValidationError) as caught:
        InvestigationResponse_Diagnosis.model_validate_json(json.dumps(body))
    # The same error against a body whose entry is gone: nothing to walk.
    walked = {**body, "state_updates": {"evidence_need_updates": []}}

    _, dropped, unhandled = so._prune_invalid_sub_records(
        walked, caught.value, InvestigationResponse_Diagnosis
    )

    assert dropped == []
    assert [tuple(e["loc"]) for e in unhandled] == [
        ("state_updates", "evidence_need_updates", 0, "fulfilling_evidence_ids", 0)
    ]


# ---------------------------------------------------------------------------
# #3021: an enum value with no close match.
# ---------------------------------------------------------------------------


def test_a_far_off_state_enum_costs_its_entry_and_marks_the_turn():
    parsed, outcomes = _parse_text(
        {
            "agent_response": "Recorded.",
            "state_updates": {
                "journal_entries": [
                    {"entry_type": "qqqqzzz", "content": "Invented type."},
                    JOURNAL,
                ]
            },
        }
    )
    # Never recorded as the enum's first member ("finding").
    entries = parsed.state_updates.journal_entries
    assert [(j.entry_type, j.content) for j in entries] == [
        (JOURNAL["entry_type"], JOURNAL["content"])
    ]
    assert outcomes == ["pruned"]
    assert state_drop_of(parsed) is StateDrop.PARTIAL


def test_a_far_off_enum_in_an_optional_sub_object_nulls_it_and_marks_the_turn():
    parsed, outcomes = _parse_text(
        {
            "agent_response": "Recorded.",
            "state_updates": {
                "preliminary_urgency": {
                    "level": "qqqqzzz",
                    "is_ongoing": True,
                    "impact_assessment": "Checkout is down.",
                }
            },
        },
        InquiryResponse,
    )
    assert parsed.state_updates.preliminary_urgency is None
    assert outcomes == ["pruned"]
    assert state_drop_of(parsed) is StateDrop.PARTIAL


def test_a_far_off_enum_in_an_unapplied_field_is_pruned_without_a_mark():
    parsed, outcomes = _parse_text(
        {
            "agent_response": "Recorded.",
            "state_updates": {
                "evidence_quality_issues": [
                    {
                        "evidence_id": EV_ID,
                        "issue_type": "truncated",
                        "severity": "qqqqzzz",
                        "description": "The log stops mid-line.",
                    }
                ]
            },
        }
    )
    assert parsed.state_updates.evidence_quality_issues == []
    assert outcomes == ["pruned"]
    assert state_drop_of(parsed) is None


def test_a_close_state_enum_is_still_corrected():
    parsed, outcomes = _parse_text(
        {
            "agent_response": "Recorded.",
            "state_updates": {
                "journal_entries": [{"entry_type": "decisoin", "content": "Typo."}]
            },
        }
    )
    assert outcomes == ["clean"]
    assert state_drop_of(parsed) is None
    assert [j.entry_type for j in parsed.state_updates.journal_entries] == ["decision"]


def test_a_far_off_follow_up_action_type_still_takes_the_fallback():
    parsed, outcomes = _parse_text(
        {
            "agent_response": "Recorded.",
            "suggested_follow_ups": [
                {
                    "label": "Check the pods",
                    "action_type": "qqqqzzz",
                    "payload": "Check the pods.",
                }
            ],
        }
    )
    assert outcomes == ["clean"]
    assert [f.action_type for f in parsed.suggested_follow_ups] == ["DECIDE"]
