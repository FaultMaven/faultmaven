"""Strict structured-output enforcement for the investigation schemas (fm#1051).

The bug this covers was not a missing feature but a *believed* one. The engine
selected a STRICT provider, logged it, and then delivered the response schema as
a plain OpenAI function with no ``strict`` key — so nothing was enforced. The
model omitted a required field, the engine dropped the entire ``state_updates``
payload, and the turn advanced no state while returning 200.

Three properties keep that from coming back:

1. a schema that CAN be enforced is emitted in OpenAI's strict subset;
2. a schema that CANNOT be is refused rather than sent with a false
   ``strict: true`` claim, which the API rejects outright;
3. the Python models accept what a strict provider actually returns — every key
   present, ``null`` where there is nothing to say.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

import faultmaven.core.investigation.schemas as schemas
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
    NullTolerantModel,
    TerminalResponse,
)
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)
from faultmaven.utils.schema_converter import (
    StrictSchemaUnsupported,
    pydantic_to_openai_function,
    pydantic_to_strict_openai_tools,
    to_strict_schema,
)

pytestmark = pytest.mark.unit

#: Schemas whose tools are expected to carry native enforcement today. The other
#: four (`InvestigationResponse_*`) hold free-form Dict[str, Any] fields.
STRICT_CAPABLE = (InquiryResponse, TerminalResponse)


class _Provider:
    def __init__(self, capability):
        self._capability = capability

    def get_structured_output_capability(self, model=None):
        return self._capability


def _walk_objects(node, path=""):
    """Yield every ``(path, object_schema)`` in a JSON schema."""
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield path or "root", node
        for key, value in node.items():
            yield from _walk_objects(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_objects(item, f"{path}[{index}]")


# ---------------------------------------------------------------------------
# The transformed schema really is in OpenAI's strict subset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", STRICT_CAPABLE, ids=lambda m: m.__name__)
def test_strict_schema_satisfies_every_rule_openai_enforces(model):
    """All three rules, asserted structurally rather than by spot-check.

    Sending a schema that breaks any of them with ``strict: true`` is a 400, not
    a degraded response — so "it looked strict" is not a property worth having.
    """
    schema = pydantic_to_strict_openai_tools(model)[0]["function"]["parameters"]

    for path, obj in _walk_objects(schema):
        assert obj.get("additionalProperties") is False, path
        assert set(obj.get("required", [])) == set(obj.get("properties", {})), path


@pytest.mark.parametrize("model", STRICT_CAPABLE, ids=lambda m: m.__name__)
def test_the_tool_actually_asks_for_enforcement(model):
    """The whole defect in one assertion: the schema tool carried no ``strict``
    key, so a STRICT provider applied no constraint at all."""
    function = pydantic_to_strict_openai_tools(model)[0]["function"]
    assert function["strict"] is True


def test_a_formerly_optional_field_becomes_nullable_not_dropped():
    """Strict mode has no optional keys, so optionality is expressed as a null
    union. Dropping the field instead would silently narrow the schema."""
    schema = pydantic_to_strict_openai_tools(InquiryResponse)[0]["function"][
        "parameters"
    ]
    follow_ups = schema["properties"]["suggested_follow_ups"]
    assert {"type": "null"} in follow_ups["anyOf"]


def test_unsupported_keywords_are_stripped():
    """OpenAI's subset rejects them. They are descriptive, so removing them
    cannot let a wrong response validate."""

    class Bounded(BaseModel):
        ratio: float = 0.5

    schema = to_strict_schema(Bounded.model_json_schema())
    assert "default" not in schema["properties"]["ratio"]


# ---------------------------------------------------------------------------
# The refusal valve
# ---------------------------------------------------------------------------


def test_a_free_form_dict_is_refused_rather_than_emptied():
    """``Dict[str, Any]`` has no strict representation: strict mode would demand
    ``additionalProperties: false``, producing an object that can never hold a
    key. For ``milestone_justifications`` that would structurally guarantee
    every milestone arrives unjustified — worse than not enforcing at all."""
    with pytest.raises(StrictSchemaUnsupported, match="free-form"):
        to_strict_schema(
            pydantic_to_openai_function(InvestigationResponse_Diagnosis)["parameters"]
        )


def test_a_refused_schema_still_produces_a_usable_unenforced_tool():
    """Refusal must not fail the turn — strict is an improvement where it is
    available, not a precondition for investigating."""
    function = pydantic_to_strict_openai_tools(InvestigationResponse_Diagnosis)[0][
        "function"
    ]
    assert function.get("strict") is not True
    assert function["parameters"]["properties"], "the tool must still be usable"


def test_a_model_with_no_fields_is_not_mistaken_for_a_free_form_object():
    """``properties: {}`` (a model with no fields) is expressible — it accepts
    exactly ``{}``. Only a MISSING ``properties`` key is the free-form case."""

    class Empty(BaseModel):
        pass

    assert to_strict_schema(Empty.model_json_schema())["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Which provider gets enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "capability,expected",
    [
        (StructuredOutputCapability.STRICT, True),
        (StructuredOutputCapability.FUNCTION_CALLING, False),
        (StructuredOutputCapability.BEST_EFFORT, False),
        (StructuredOutputCapability.NONE, False),
    ],
)
def test_enforcement_is_requested_only_where_it_exists(capability, expected):
    tools = MilestoneEngine._build_schema_tool(InquiryResponse, _Provider(capability))
    assert tools[0]["function"].get("strict", False) is expected


def test_an_unusable_provider_falls_back_to_the_unenforced_tool():
    """Capability detection failing must not fail the turn: the unenforced tool
    is what this path always sent, so it cannot regress anything."""

    class Broken:
        def get_structured_output_capability(self, model=None):
            raise RuntimeError("registry unavailable")

    tools = MilestoneEngine._build_schema_tool(InquiryResponse, Broken())
    assert tools[0]["function"].get("strict", False) is False


# ---------------------------------------------------------------------------
# The Python side accepts what a strict provider returns
# ---------------------------------------------------------------------------


def test_the_engine_parses_a_fully_populated_strict_response():
    """What a strict provider actually returns: every key present, ``null``
    wherever the model had nothing. Before ``NullTolerantModel`` this raised on
    the non-Optional defaulted fields — turning schema enforcement into a fresh
    source of the validation failure it was added to prevent."""
    response = InquiryResponse.model_validate(
        {
            "agent_response": "Tell me more.",
            "suggested_follow_ups": None,
            "state_updates": {
                "problem_confirmation": None,
                "proposed_problem_statement": None,
                "preliminary_urgency": None,
                "knowledge_match": None,
                "knowledge_resolution": None,
                "user_confirmed_investigation": None,
            },
        }
    )

    # The null restored the default rather than being rejected...
    assert response.state_updates.user_confirmed_investigation is False
    # ...while a genuinely Optional field keeps None as a real value.
    assert response.state_updates.knowledge_match is None


def test_null_tolerance_does_not_swallow_a_real_value():
    """Only ``null`` means "absent". A supplied value must survive untouched, or
    the validator would be quietly discarding model output."""
    follow_up = schemas.SuggestedFollowUp.model_validate(
        {"label": "Check the logs", "action_type": "EVIDENCE"}
    )
    assert follow_up.action_type == "EVIDENCE"


def test_every_strict_enabled_schema_tolerates_the_nulls_it_will_receive():
    """The guard that makes the requirement enforced rather than remembered.

    Any non-Optional field carrying a default, reachable from a strict-enabled
    schema, MUST live on a NullTolerantModel — strict mode will send it ``null``
    and plain Pydantic rejects that. This fails when someone adds such a field,
    which is the only way that class of bug can reappear.
    """
    import typing

    offenders: list[str] = []

    def visit(model, path, seen):
        if model in seen:
            return
        seen.add(model)
        for name, field in model.model_fields.items():
            annotation = field.annotation
            nullable = type(None) in typing.get_args(annotation)
            if not field.is_required() and not nullable:
                if not issubclass(model, NullTolerantModel):
                    offenders.append(f"{path}.{name} on {model.__name__}")
            for candidate in (annotation, *typing.get_args(annotation)):
                for nested in (candidate, *typing.get_args(candidate)):
                    if isinstance(nested, type) and issubclass(nested, BaseModel):
                        visit(nested, f"{path}.{name}", seen)

    for schema_model in STRICT_CAPABLE:
        visit(schema_model, schema_model.__name__, set())

    assert offenders == [], (
        "these fields will receive an explicit null under strict mode and their "
        f"model does not tolerate it: {offenders}. Inherit NullTolerantModel."
    )
