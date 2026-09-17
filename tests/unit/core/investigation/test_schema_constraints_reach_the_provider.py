"""The engine's value constraints must survive to the provider (fm#355).

The reported symptom was a 500: Gemini answered a ``likelihood`` field with
``95`` where the Pydantic model declares ``ge=0, le=1``, and the turn died at
validation. fm#355 proposed fixing it by routing the schema tool through
Gemini's ``response_schema`` instead of function calling, on the premise that
``response_schema`` carries "the full schema with all constraints" while
function calling strips them.

Measured against the live API on 2026-09-17, that premise is false in both
halves:

* The Gemini API uses **the same ``Schema`` message** for
  ``generationConfig.responseSchema`` and for
  ``FunctionDeclaration.parameters`` (v1beta discovery document, revision
  20260917). The two request shapes accept exactly the same keywords, so
  switching between them cannot enforce one constraint more.
* The constraints were being removed by FaultMaven, twice, on **both** paths:
  ``to_strict_schema`` dropped them as "descriptive" for OpenAI's benefit, and
  ``GeminiProvider._GEMINI_UNSUPPORTED_FIELDS`` dropped them again on the claim
  that Gemini accepts only ``type``/``description``/``properties``/
  ``required``/``items``/``enum``/``nullable``/``format``.

So the fix is not a routing change. It is: stop stripping what the target API
accepts. This module is the guard on that, and it is deliberately written
end-to-end over the *real* builders rather than over either strip list — a
third stripper introduced anywhere on either path fails it the same way.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
    InvestigationResponse_General,
    InvestigationResponse_Mitigation,
    InvestigationResponse_Treatment,
    TerminalResponse,
)
from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    create_strategy_for_capability,
)
from faultmaven.utils.schema_converter import _STRICT_UNSUPPORTED_KEYWORDS

pytestmark = pytest.mark.unit

#: Every response schema the engine sends.
ENGINE_SCHEMAS = [
    InquiryResponse,
    TerminalResponse,
    InvestigationResponse_Diagnosis,
    InvestigationResponse_Mitigation,
    InvestigationResponse_Treatment,
    InvestigationResponse_General,
]

#: Properties of the Gemini API's ``Schema`` message, from the v1beta discovery
#: document (revision 20260917):
#: ``https://generativelanguage.googleapis.com/$discovery/rest?version=v1beta``
#:
#: This is the authority for what may be sent, and it is the SAME message type
#: for ``responseSchema`` and for ``FunctionDeclaration.parameters``.
GEMINI_SCHEMA_PROPERTIES = frozenset(
    {
        "anyOf",
        "default",
        "description",
        "enum",
        "example",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "nullable",
        "pattern",
        "properties",
        "propertyOrdering",
        "required",
        "title",
        "type",
    }
)

#: Keywords that decide WHICH DOCUMENTS VALIDATE. Dropping one of these does not
#: merely lose documentation — it asks the model for a value the Python model
#: will then reject. That is the fm#355 failure, and none of them may appear in
#: a strip list.
NARROWING_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
    }
)

#: ``Schema`` accepts these, and FaultMaven drops them anyway. Neither narrows
#: what validates; ``title`` alone costs ~3 KB of schema budget against a
#: documented constrained-decoding ceiling
#: (``GeminiProvider._SCHEMA_CAPACITY_DENYLIST_PREFIXES``). Listed so that
#: adding a third is a deliberate act with a reason, not a silent regression.
GEMINI_DELIBERATE_EXTRA_STRIPS = frozenset({"title", "default"})


class _StrictProvider:
    """Stands in for the provider ``_build_schema_tool`` consults.

    Gemini reports STRICT, which is the branch that runs the OpenAI strict
    rewrite — the first of the two places the constraints used to vanish.
    """

    def get_structured_output_capability(self) -> StructuredOutputCapability:
        return StructuredOutputCapability.STRICT


def _count_keywords(node: Any, acc: Dict[str, int] | None = None) -> Dict[str, int]:
    """How many times each narrowing keyword appears anywhere in ``node``."""
    acc = {} if acc is None else acc
    if isinstance(node, dict):
        for key, value in node.items():
            if key in NARROWING_KEYWORDS:
                acc[key] = acc.get(key, 0) + 1
            _count_keywords(value, acc)
    elif isinstance(node, list):
        for value in node:
            _count_keywords(value, acc)
    return acc


def _tool_path_wire_schema(model: Any) -> Dict[str, Any]:
    """What the TOOL-AUGMENTED path puts on the wire for Gemini.

    ``_build_schema_tool`` (strict rewrite) → the Gemini adapter's own
    resolver, which is exactly what ``GeminiProvider.generate`` applies to
    ``FunctionDeclaration.parameters``.
    """
    tool = MilestoneEngine._build_schema_tool(model, _StrictProvider())[0]
    return GeminiProvider._resolve_refs_for_gemini(tool["function"]["parameters"])


def _response_schema_path_wire_schema(model: Any) -> Dict[str, Any]:
    """What the SINGLE-SHOT ``response_schema`` path puts on the wire."""
    strategy = create_strategy_for_capability(
        StructuredOutputCapability.STRICT, model.model_json_schema()
    )
    schema = strategy.response_format["json_schema"]["schema"]
    return GeminiProvider._resolve_refs_for_gemini(schema)


# ---------------------------------------------------------------------------
# The load-bearing guard: constraints survive the whole path, on both shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ENGINE_SCHEMAS, ids=lambda m: m.__name__)
def test_declared_constraints_reach_the_wire_on_both_request_shapes(model):
    """Every narrowing keyword the Pydantic model declares reaches Gemini.

    End-to-end over the real builders on purpose. The constraints used to be
    removed twice — once by ``to_strict_schema`` and once by the Gemini
    adapter — so a guard that inspected either strip list alone would have
    passed while the other still emptied the schema.

    Counts are compared with ``>=`` because both builders inline ``$defs``: a
    definition used twice contributes two copies to the wire payload and one to
    the source schema.
    """
    declared = _count_keywords(model.model_json_schema())

    for path_name, wire in (
        ("tool-augmented", _tool_path_wire_schema(model)),
        ("response_schema", _response_schema_path_wire_schema(model)),
    ):
        sent = _count_keywords(wire)
        for keyword, count in declared.items():
            assert sent.get(keyword, 0) >= count, (
                f"{model.__name__}: the {path_name} path sends "
                f"{sent.get(keyword, 0)} `{keyword}` where the model declares "
                f"{count}. Something on that path is stripping it, and the "
                f"model will answer out of range (fm#355: `likelihood` "
                f"declared ge=0/le=1 arrived stripped and came back as 95)."
            )


def test_the_parametrised_guard_is_watching_something():
    """Declare where the rule above CAN be violated, and fail if it is looking
    nowhere.

    Five of the six schemas carry value constraints;
    ``TerminalResponse`` carries none (it is a closing summary, all free text),
    so its parametrisation asserts vacuously and is expected to. Pinning the
    split here means a schema silently losing its ``Field(ge=/le=/max_length=)``
    bounds fails a test instead of quietly emptying the guard.
    """
    declaring = {
        m.__name__: _count_keywords(m.model_json_schema())
        for m in ENGINE_SCHEMAS
        if _count_keywords(m.model_json_schema())
    }
    assert set(declaring) == {
        "InquiryResponse",
        "InvestigationResponse_Diagnosis",
        "InvestigationResponse_Mitigation",
        "InvestigationResponse_Treatment",
        "InvestigationResponse_General",
    }, f"which schemas carry value constraints has changed: {sorted(declaring)}"
    assert all(
        "minimum" in counts and "maximum" in counts for counts in declaring.values()
    ), f"a schema lost its [0, 1] likelihood bounds: {declaring}"


def test_the_engine_schemas_actually_carry_the_constraint_this_bug_was_about():
    """A named, concrete instance, so the parametrised test above cannot pass
    vacuously on some future schema that happens to declare only one keyword.

    ``likelihood``/``confidence``/``stance_confidence`` are ``Field(ge=0,
    le=1)``; stripped, all three shipped Gemini models answer ``95`` when the
    prompt talks in percentages, and ``95`` is what the Python model rejects.
    """
    wire = _tool_path_wire_schema(InvestigationResponse_Diagnosis)

    bounded: List[str] = []

    def walk(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            if node.get("minimum") == 0 and node.get("maximum") == 1:
                bounded.append(path)
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(wire)
    assert any("likelihood" in p for p in bounded), (
        "no [0, 1]-bounded `likelihood` property survived to the wire; "
        f"bounded properties found: {bounded}"
    )
    assert any("confidence" in p for p in bounded), (
        "no [0, 1]-bounded `confidence` property survived to the wire; "
        f"bounded properties found: {bounded}"
    )


@pytest.mark.parametrize("model", ENGINE_SCHEMAS, ids=lambda m: m.__name__)
def test_the_two_request_shapes_send_the_same_schema(model):
    """fm#355's proposed fix was to route the schema tool through
    ``response_schema``. It cannot enforce anything more, because both shapes
    carry the same ``Schema`` message and go through the same resolver.

    The one permitted difference is where the model's docstring lands: the tool
    path carries it as ``function.description`` instead of on the schema root.
    """
    tool_wire = _tool_path_wire_schema(model)
    rs_wire = _response_schema_path_wire_schema(model)

    assert _count_keywords(tool_wire) == _count_keywords(rs_wire)
    assert {k: v for k, v in tool_wire.items() if k != "description"} == {
        k: v for k, v in rs_wire.items() if k != "description"
    }


# ---------------------------------------------------------------------------
# The strip lists, against the API's published vocabulary
# ---------------------------------------------------------------------------


def test_gemini_strips_only_what_the_api_rejects():
    """``_GEMINI_UNSUPPORTED_FIELDS`` may not hold a ``Schema`` property.

    The list once claimed Gemini accepts eight keywords and stripped seven the
    discovery document lists as ``Schema`` properties. Two deliberate
    exceptions remain, and they are named rather than assumed.
    """
    over_stripped = (
        GeminiProvider._GEMINI_UNSUPPORTED_FIELDS & GEMINI_SCHEMA_PROPERTIES
    ) - GEMINI_DELIBERATE_EXTRA_STRIPS
    assert not over_stripped, (
        f"{sorted(over_stripped)} are properties of the Gemini API's `Schema` "
        "message (v1beta discovery, revision 20260917) and are being stripped "
        "anyway. Either send them, or add them to "
        "GEMINI_DELIBERATE_EXTRA_STRIPS with the reason."
    )


def test_gemini_still_strips_everything_the_api_has_no_field_for():
    """The converse: the keywords Pydantic emits that ``Schema`` has no home
    for must stay stripped, or the request is rejected outright."""
    for keyword in (
        "additionalProperties",
        "$schema",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "uniqueItems",
        "const",
        "oneOf",
    ):
        assert keyword not in GEMINI_SCHEMA_PROPERTIES, (
            f"`{keyword}` is now a Schema property — re-measure before "
            "changing the strip list on the strength of this test."
        )
        assert keyword in GeminiProvider._GEMINI_UNSUPPORTED_FIELDS, (
            f"`{keyword}` is not a Gemini `Schema` property, so sending it is "
            "a 400. It must stay stripped."
        )


def test_no_narrowing_keyword_is_dropped_by_the_strict_rewrite():
    """``to_strict_schema`` is applied to BOTH engine paths, so a narrowing
    keyword in its drop list is removed before any provider sees it — which is
    why fixing only the Gemini adapter would have changed nothing."""
    dropped = _STRICT_UNSUPPORTED_KEYWORDS & NARROWING_KEYWORDS
    assert not dropped, (
        f"{sorted(dropped)} decide which documents validate. Dropping them "
        "asks the model for values the Pydantic model then rejects — the "
        "fm#355 500."
    )
