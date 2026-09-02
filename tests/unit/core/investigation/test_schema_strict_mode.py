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
    InvestigationResponse_General,
    InvestigationResponse_Mitigation,
    InvestigationResponse_Treatment,
    MilestoneJustifications,
    MilestoneUpdates,
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

#: Every response schema the engine sends. All six carry native enforcement as
#: of fm#1057; before it the four `InvestigationResponse_*` held free-form
#: `Dict[str, Any]` fields and fell back to an unenforced tool, which left the
#: whole INVESTIGATING flow — the bulk of the turns — on the fm#1051 failure
#: mode. Adding a schema here is how it earns enforcement.
STRICT_CAPABLE = (
    InquiryResponse,
    TerminalResponse,
    InvestigationResponse_Diagnosis,
    InvestigationResponse_Mitigation,
    InvestigationResponse_Treatment,
    InvestigationResponse_General,
)


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


@pytest.mark.parametrize("model", STRICT_CAPABLE, ids=lambda m: m.__name__)
def test_the_live_response_format_path_is_also_compliant(model):
    """The `response_format` half, exercised through the branch the engine
    actually reaches.

    `create_response_format_json_schema` has no production callers; the live
    single-shot path is `BaseLLMProvider.get_structured_output_strategy` ->
    `create_strategy_for_capability`. Fixing only the former would have left the
    false `strict: true` claim exactly where it runs.
    """
    from faultmaven.infrastructure.llm.structured_output_capability import (
        create_strategy_for_capability,
    )

    # The RAW Pydantic schema, `$defs` and all — exactly what
    # `_generate_structured_output_inner` passes. An earlier revision of this
    # test pre-inlined with `_inline_refs`, which is the one step production
    # skips, so it certified a `response_format` the API rejected.
    strategy = create_strategy_for_capability(
        StructuredOutputCapability.STRICT, model.model_json_schema()
    )
    json_schema = strategy.response_format["json_schema"]

    assert json_schema["strict"] is True
    for path, obj in _walk_objects(json_schema["schema"]):
        assert obj.get("additionalProperties") is False, path
        assert set(obj.get("required", [])) == set(obj.get("properties", {})), path


class _FreeForm(BaseModel):
    """Stands in for a schema outside the strict subset.

    Every production schema is strict-representable as of fm#1057, so the
    refusal valve has no real subject left. It still has to work: the valve is
    what keeps a future ``Dict[str, Any]`` field from being sent WITH a
    ``strict: true`` the API rejects outright. Testing it against a synthetic
    model keeps the property covered without wishing a free-form field back into
    the response schemas.
    """

    freeform: dict


def test_the_live_path_drops_the_strict_claim_it_cannot_honour():
    """A schema outside the subset must not be sent WITH `strict: true` — that
    is a 400, not a degraded response."""
    from faultmaven.infrastructure.llm.structured_output_capability import (
        create_strategy_for_capability,
    )

    strategy = create_strategy_for_capability(
        StructuredOutputCapability.STRICT, _FreeForm.model_json_schema()
    )

    assert strategy.response_format["json_schema"]["strict"] is False


def test_a_nullable_nested_object_keeps_its_properties_inside_the_union():
    """`_nullable` must move everything structural INTO the branch.

    Splitting on an allowlist of scalar keys left `properties`/`required`/
    `additionalProperties` as siblings of the union, so the object branch was a
    bare `{"type": "object"}` — an object with no declared properties, the one
    shape strict mode rejects. Any nested-model field with a default hit it.
    """

    class Inner(BaseModel):
        a: str

    class Outer(BaseModel):
        nested: Inner = Inner(a="x")

    from faultmaven.utils.schema_converter import _inline_refs

    schema = to_strict_schema(_inline_refs(Outer.model_json_schema()))
    branches = schema["properties"]["nested"]["anyOf"]
    obj = next(b for b in branches if b.get("type") == "object")

    assert "properties" in obj, "the object branch lost its properties"
    assert obj["additionalProperties"] is False
    assert set(obj["required"]) == set(obj["properties"])


INVESTIGATION_RESPONSES = (
    InvestigationResponse_Diagnosis,
    InvestigationResponse_Mitigation,
    InvestigationResponse_Treatment,
    InvestigationResponse_General,
)


def _unmarked_objects(schema) -> list[str]:
    """Paths of every object the strict subset would reject."""
    return [
        path
        for path, obj in _walk_objects(schema)
        if obj.get("additionalProperties") is not False
        or set(obj.get("required", [])) != set(obj.get("properties", {}))
    ]


@pytest.mark.parametrize("model", INVESTIGATION_RESPONSES, ids=lambda m: m.__name__)
def test_every_object_under_defs_is_strict_in_the_single_shot_response_format(
    model,
):
    """The defect, counted rather than spot-checked.

    Pydantic puts every nested model under ``$defs``; the rewrite walked only
    the root's ``properties`` and left those definitions untouched — 23 unmarked
    objects in ``InvestigationResponse_Diagnosis``. The API's rule is stated in
    its own 400: ``'additionalProperties' is required to be supplied and to be
    false`` on EVERY object. This is the schema the engine's non-tool fallback
    (``ToolCallingUnsupportedError``, or no investigation tools registered)
    sends on an OpenAI STRICT provider, so an unmarked definition fails the
    whole turn, not just the enforcement.
    """
    from faultmaven.infrastructure.llm.structured_output_capability import (
        create_strategy_for_capability,
    )

    strategy = create_strategy_for_capability(
        StructuredOutputCapability.STRICT, model.model_json_schema()
    )
    json_schema = strategy.response_format["json_schema"]

    assert json_schema["strict"] is True
    assert _unmarked_objects(json_schema["schema"]) == []


def test_a_nested_model_reached_through_defs_is_marked_strict():
    """The minimal reproduction: one ``$ref`` into ``$defs``, fed RAW.

    Pre-inlining in the test is what let this slip — the converter was only ever
    exercised on a shape it was never given in production. The nested object
    must come out marked and required-complete, and the schema must leave with
    no ``$defs``/``$ref`` at all, so the same flat shape reaches the API from
    both the tool and the ``response_format`` converters.
    """

    class Leaf(BaseModel):
        note: str
        weight: float = 1.0

    class Root(BaseModel):
        leaf: Leaf
        leaves: list[Leaf] = []

    raw = Root.model_json_schema()
    assert "$defs" in raw, "the fixture must exercise the $defs path"

    strict = to_strict_schema(raw)

    assert "$defs" not in strict
    assert "$ref" not in str(strict)
    assert _unmarked_objects(strict) == []
    leaf = strict["properties"]["leaf"]
    assert leaf["additionalProperties"] is False
    assert set(leaf["required"]) == {"note", "weight"}
    assert "default" not in leaf["properties"]["weight"]


def test_a_recursive_definition_is_refused_not_leaked():
    """Inlining cannot flatten a self-reference. It must surface as the refusal
    the live caller already handles (``strict: false``), not as a bare
    ``ValueError`` that fails the turn — and never as a ``$defs`` block sent
    under ``strict: true``."""
    from faultmaven.infrastructure.llm.structured_output_capability import (
        create_strategy_for_capability,
    )

    class Node(BaseModel):
        label: str
        children: list["Node"] = []

    with pytest.raises(StrictSchemaUnsupported, match="Recursive"):
        to_strict_schema(Node.model_json_schema())

    strategy = create_strategy_for_capability(
        StructuredOutputCapability.STRICT, Node.model_json_schema()
    )
    assert strategy.response_format["json_schema"]["strict"] is False


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
    key. For ``milestone_justifications`` that would have structurally
    guaranteed every milestone arrives unjustified — worse than not enforcing at
    all, which is why the valve exists rather than a best-effort transform."""
    with pytest.raises(StrictSchemaUnsupported, match="free-form"):
        to_strict_schema(pydantic_to_openai_function(_FreeForm)["parameters"])


def test_a_refused_schema_still_produces_a_usable_unenforced_tool():
    """Refusal must not fail the turn — strict is an improvement where it is
    available, not a precondition for investigating."""
    function = pydantic_to_strict_openai_tools(_FreeForm)[0]["function"]
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

    Any field carrying a non-``None`` default, reachable from a strict-enabled
    schema, MUST live on a NullTolerantModel — strict mode sends it ``null`` and
    the default is what ``null`` means.

    The condition is "has a non-None default", NOT "is not Optional" (fm#1057).
    The narrower spelling only caught fields that would RAISE, which are the
    harmless half — a validation error is loud and gets fixed. It skipped every
    ``Optional[List[X]] = Field(default_factory=list)``, which accepts the null
    silently and lands as ``None`` where the code expects a list. Extending the
    four ``InvestigationResponse_*`` schemas exposed 47 of those at once, so the
    guard had to widen with them or it would have certified the change.
    """
    import typing

    offenders: list[str] = []

    def visit(model, path, seen):
        if model in seen:
            return
        seen.add(model)
        for name, field in model.model_fields.items():
            defaulted = (
                not field.is_required()
                and field.get_default(call_default_factory=True) is not None
            )
            if defaulted and not issubclass(model, NullTolerantModel):
                offenders.append(f"{path}.{name} on {model.__name__}")
            annotation = field.annotation
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


def test_a_nulled_list_field_arrives_as_a_list_not_none():
    """The silent half of the null problem, asserted end to end.

    Every list field in the state update was reachable by this: the model sends
    ``"evidence_to_add": null``, Pydantic accepts it because the field is
    Optional, and the engine iterates ``None``.
    """
    state = InvestigationResponse_Diagnosis.DiagnosisStateUpdate.model_validate(
        {
            "evidence_to_add": None,
            "hypotheses_to_add": None,
            "hypotheses_to_update": None,
            "outcome": None,
        }
    )

    assert state.evidence_to_add == []
    assert state.hypotheses_to_add == []
    assert state.hypotheses_to_update == []
    # A non-list default is restored the same way.
    assert state.outcome is schemas.TurnOutcome.CONVERSATION


def test_a_genuinely_optional_field_keeps_its_none():
    """Where the default IS None, the value and the absence coincide — there is
    nothing to restore, and inventing something would be a fabrication."""
    state = InvestigationResponse_Diagnosis.DiagnosisStateUpdate.model_validate(
        {"milestones": None, "verification_updates": None}
    )

    assert state.milestones is None
    assert state.verification_updates is None


# ---------------------------------------------------------------------------
# The justification channel survives being made strict
# ---------------------------------------------------------------------------


def test_milestone_justifications_covers_every_settable_milestone():
    """The drift guard.

    ``MilestoneJustifications`` declares one field per milestone because strict
    mode cannot express an open map. That makes it a SECOND list of milestone
    names, and a second list drifts. A gate milestone added to
    ``MilestoneUpdates`` without a matching justification field would be
    impossible to justify — and the reasoning gate strips exactly the milestones
    it finds unjustified, so it would be silently unsettable in production while
    every test still passed.
    """
    settable = {
        name
        for name, field in MilestoneUpdates.model_fields.items()
        if field.annotation is not None and bool is _bool_of(field.annotation)
    }
    declared = set(MilestoneJustifications.model_fields)

    assert settable == declared, (
        "MilestoneUpdates' boolean milestones and MilestoneJustifications' "
        f"fields have drifted: only in MilestoneUpdates={settable - declared}, "
        f"only in MilestoneJustifications={declared - settable}"
    )


def _bool_of(annotation):
    """The bool inside ``Optional[bool]``, or None for any other annotation."""
    import typing

    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if len(args) == 1:
        return args[0]
    return annotation


def test_the_reasoning_gate_still_rejects_an_unjustified_milestone():
    """The fail-open guard — the property most at risk from this change.

    The gate is a MEMBERSHIP test, and strict mode makes the model send every
    justification key with ``null`` where it had nothing. Reading the model
    directly (``model_dump()`` without ``exclude_none``) reports all four
    milestones as justified, so the gate runs, logs, and never fires again.
    Nothing else in the suite would notice: no exception, no changed shape, just
    milestones landing unjustified forever.

    This asserts the gate FIRES, which is the direction that can regress.
    """
    from faultmaven.core.investigation.milestone_engine import validate_reasoning_first

    case = _case_in_investigating_with_evidence()
    response = InvestigationResponse_Diagnosis.model_validate(
        {
            "agent_response": "Symptom confirmed.",
            "internal_reasoning": {
                # Exactly the strict wire shape: every key, null where silent.
                "milestone_justifications": {
                    "symptom_verified": None,
                    "mitigation_accepted": None,
                    "mitigation_verified": None,
                    "solution_accepted": None,
                },
            },
            "state_updates": {"milestones": {"symptom_verified": True}},
        }
    )

    is_valid, errors, offending = validate_reasoning_first(response, case)

    assert is_valid is False, "an unjustified milestone must not pass the gate"
    assert offending == {"symptom_verified"}
    assert any("without justification" in e for e in errors)


def test_the_reasoning_gate_accepts_a_justified_milestone():
    """The other half: the gate must not reject a turn that DID justify itself,
    or strict mode would strip every milestone instead of none."""
    from faultmaven.core.investigation.milestone_engine import validate_reasoning_first

    case = _case_in_investigating_with_evidence()
    response = InvestigationResponse_Diagnosis.model_validate(
        {
            "agent_response": "Symptom confirmed.",
            "internal_reasoning": {
                "milestone_justifications": {
                    "symptom_verified": "47 connection errors in ev_abc123",
                    "mitigation_accepted": None,
                    "mitigation_verified": None,
                    "solution_accepted": None,
                },
            },
            "state_updates": {"milestones": {"symptom_verified": True}},
        }
    )

    is_valid, errors, offending = validate_reasoning_first(response, case)

    assert is_valid is True, f"a justified milestone must pass: {errors}"
    assert offending == set()


def _case_in_investigating_with_evidence():
    """A case the gate will actually evaluate: INVESTIGATING, not terminal, no
    pending transition, and carrying evidence so the no-evidence check passes."""
    from unittest.mock import MagicMock

    from faultmaven.modules.case.contracts import CaseState

    case = MagicMock()
    case.case_id = "case_test"
    case.state = CaseState.INVESTIGATING
    case.is_terminal = False
    case.pending_transition = None
    case.progress.solution_verified = False
    case.evidence = [MagicMock()]
    return case
