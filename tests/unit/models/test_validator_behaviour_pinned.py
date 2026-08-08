"""Field validators must keep accepting what they accept and rejecting what they reject.

These validators guard auth input (`DevLoginRequest`) and the agent contract
models. A validator that quietly stops rejecting fails OPEN — the model
constructs, nothing errors, and the bad value travels on — so "the suite still
passes" is not evidence that one still works. Each case below therefore pins
the *outcome*: the accepted-and-possibly-transformed value, or the exact error
message, for a valid input AND an invalid one per validator.

Written while migrating V1 `@validator` to V2 `@field_validator`. The migration
looked mechanical — no `pre=`, `always=`, `each_item=`, no `values` argument on
any of the ten — but "looked mechanical" is what this file exists to stop
anyone relying on: the table was captured from the pre-migration code, run
against the post-migration code, and required to be identical. It stays because
the next person to touch these needs the same net, and because `@field_validator`
has its own silent-no-op modes (a typo'd field name in the decorator raises at
class-definition time, but a validator moved to the wrong `mode=` does not).

Transformations are pinned deliberately, not just accept/reject: both auth
validators lowercase their input, and losing that would collapse two distinct
usernames into one lookup without any test failing.
"""

import pytest
from pydantic import ValidationError

import faultmaven.models.api_auth as api_auth
import faultmaven.models.contracts.core_contracts as contracts

# Minimum required fields per model, so the field under test is the only thing
# that can fail. Without these the model errors on a missing sibling and every
# case looks "rejected" — which would pass this file while testing nothing.
_REQUIRED = {
    "DevLoginRequest": {"username": "seed.user"},
    "TurnContext": {"session_id": "s", "query": "q"},
    "DecisionRecord": {
        "session_id": "s",
        "turn_id": "t",
        "turn": 1,
        "selected_agents": ["a"],
        "routing_rationale": "r",
        "latency_ms": 1,
        "final_response": "f",
    },
    "RetrievalRequest": {"query": "q"},
    "ConfidenceRequest": {"features": {"a": 1.0}},
    "PolicyEvaluation": {
        "action_description": "d",
        "decision": "allow",
        "rationale": "r",
    },
    "LoopCheckRequest": {
        "session_id": "s",
        "history": ["h"],
        "confidence_history": [0.5],
    },
    "GatewayResult": {
        "processed_query": "p",
        "original_query": "o",
        "clarity_assessment": "vague",
        "reality_check": "plausible",
        "processing_time_ms": 1,
    },
}


def _base(cls):
    kwargs = dict(_REQUIRED[cls.__name__])
    if cls is contracts.DecisionRecord:
        kwargs["budget_allocated"] = contracts.Budget()
        kwargs["budget_used"] = contracts.Budget()
    if cls is contracts.PolicyEvaluation:
        kwargs["action_type"] = list(contracts.ActionType)[0]
        kwargs["risk_level"] = list(contracts.RiskLevel)[0]
    return kwargs


ACCEPTED = [
    # (model, field, input, value the model should hold afterwards)
    (api_auth.DevLoginRequest, "username", "John.Doe", "john.doe"),
    (api_auth.DevLoginRequest, "username", "a@b.co", "a@b.co"),
    (api_auth.DevLoginRequest, "username", "UPPER_case-1", "upper_case-1"),
    (api_auth.DevLoginRequest, "email", "X@Y.CO", "x@y.co"),
    (api_auth.DevLoginRequest, "email", None, None),
    (contracts.TurnContext, "priority", "high", "high"),
    # "pending" is NOT valid here — the set is started/processing/completed/
    # failed/timeout. Guessing it cost a red test, which is the point.
    (contracts.DecisionRecord, "status", "completed", "completed"),
    (contracts.RetrievalRequest, "enabled_sources", ["kb"], ["kb"]),
    (contracts.PolicyEvaluation, "decision", "deny", "deny"),
    (contracts.LoopCheckRequest, "confidence_history", [0.0, 1.0], [0.0, 1.0]),
    (contracts.GatewayResult, "clarity_assessment", "absurd", "absurd"),
    (contracts.GatewayResult, "reality_check", "verified", "verified"),
]

REJECTED = [
    # (model, field, input, fragment the error must contain)
    (
        api_auth.DevLoginRequest,
        "username",
        "bad user!",
        "Username must be a valid email",
    ),
    (api_auth.DevLoginRequest, "username", "a@@b", "Username must be a valid email"),
    (api_auth.DevLoginRequest, "email", "nope", "Invalid email format"),
    (contracts.TurnContext, "priority", "bogus", "Priority must be one of"),
    (contracts.DecisionRecord, "status", "BOGUS", "Status must be one of"),
    (contracts.RetrievalRequest, "enabled_sources", ["nope"], "Source"),
    (contracts.PolicyEvaluation, "decision", "nope", "Decision must be one of"),
    (contracts.LoopCheckRequest, "confidence_history", [1.5], "not in range"),
    (contracts.LoopCheckRequest, "confidence_history", [-0.1], "not in range"),
    (
        contracts.GatewayResult,
        "clarity_assessment",
        "nope",
        "Clarity assessment must be one of",
    ),
    (contracts.GatewayResult, "reality_check", "nope", "Reality check must be one of"),
]


@pytest.mark.unit
@pytest.mark.parametrize("cls,field,value,expected", ACCEPTED)
def test_validator_accepts_and_transforms(cls, field, value, expected):
    model = cls(**{**_base(cls), field: value})
    assert getattr(model, field) == expected


@pytest.mark.unit
@pytest.mark.parametrize("cls,field,value,fragment", REJECTED)
def test_validator_rejects(cls, field, value, fragment):
    with pytest.raises(ValidationError) as excinfo:
        cls(**{**_base(cls), field: value})

    # The error must come from the field under test. Asserting only that
    # *something* raised would pass if the model started failing for an
    # unrelated reason — which is how a dead validator hides behind a red test.
    errors = [e for e in excinfo.value.errors() if e["loc"][:1] == (field,)]
    assert (
        errors
    ), f"{cls.__name__}.{field} did not reject {value!r}; errors: {excinfo.value.errors()}"
    assert any(fragment in e["msg"] for e in errors), (
        f"{cls.__name__}.{field} rejected {value!r} but not for the expected reason; "
        f"got {[e['msg'] for e in errors]}"
    )
