"""The engine's value constraints must survive to the provider (fm#355).

The reported symptom was an out-of-range ``likelihood``: Gemini answered ``95``
where the Pydantic model declares ``ge=0, le=1``. What that actually costs is
covered in ``test_out_of_range_confidence_is_bounded_not_lost`` — it is **not**
a 500; the engine's backstop prunes the offending record or drops the whole
``state_updates``, so the turn returns 200 having advanced nothing.

fm#355 proposed fixing it by routing the schema tool through Gemini's
``response_schema`` instead of function calling, on the premise that
``response_schema`` carries "the full schema with all constraints" while
function calling strips them. Measured against the live API on 2026-09-17, that
premise is false in both halves:

* The Gemini API uses **the same ``Schema`` message** for
  ``generationConfig.responseSchema`` and for
  ``FunctionDeclaration.parameters`` (v1beta discovery document, revision
  20260917). The two request shapes accept exactly the same keywords, so
  switching between them cannot enforce one constraint more.
* The constraints were being removed by FaultMaven, twice, on **both** paths:
  ``to_strict_schema`` dropped them as "descriptive" for OpenAI's benefit, and
  the Gemini adapter dropped them again.

So the fix has two halves, and this module guards both:

1. **Decoder-level** — the constraints reach the wire, so a provider that
   enforces them does. Guarded end-to-end over the *real* builders rather than
   over either strip list, so a third stripper anywhere on either path fails it.
2. **Python-level** — a clamp bounds the value whatever the provider did.
   Half the providers FaultMaven ships (FUNCTION_CALLING, BEST_EFFORT) do not
   enforce a ``minimum``, so the decoder half alone leaves them exposed.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Tuple, Union

import pytest
from annotated_types import Ge, Le
from pydantic import BaseModel, BeforeValidator, Field

from faultmaven.core.investigation import schemas as engine_schemas
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

#: Keywords that decide WHICH DOCUMENTS VALIDATE. Dropping one does not merely
#: lose documentation — it asks the model for a value the Python model will then
#: reject. That is the fm#355 failure, and none may appear in a strip list that
#: runs before the provider boundary.
NARROWING_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
    }
)


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
    """What the TOOL-AUGMENTED path puts on the wire for Gemini."""
    tool = MilestoneEngine._build_schema_tool(model, _StrictProvider())[0]
    return GeminiProvider._resolve_refs_for_gemini(tool["function"]["parameters"])


def _response_schema_path_wire_schema(model: Any) -> Dict[str, Any]:
    """What the SINGLE-SHOT ``response_schema`` path puts on the wire."""
    strategy = create_strategy_for_capability(
        StructuredOutputCapability.STRICT, model.model_json_schema()
    )
    return GeminiProvider._resolve_refs_for_gemini(
        strategy.response_format["json_schema"]["schema"]
    )


# ---------------------------------------------------------------------------
# Half 2 first: the Python-level bound, because it is the one that covers
# every provider
# ---------------------------------------------------------------------------


def _annotation_metadata(annotation: Any) -> List[Any]:
    """Every ``Annotated[...]`` extra on *annotation*, including inside a union.

    ``Optional[UnitInterval]`` puts the metadata one level in, on the non-null
    branch. Reading only ``field.metadata`` therefore sees 6 of the 10 bounded
    fields and reports the other 4 as unbounded.
    """
    import typing

    out: List[Any] = []
    for extra in getattr(annotation, "__metadata__", ()):
        out.append(extra)
        # `Field(ge=..., le=...)` inside an Annotated lands as a FieldInfo whose
        # OWN `.metadata` holds the Ge/Le. Reading only the outer level finds
        # the BeforeValidator and misses the bounds entirely.
        out.extend(getattr(extra, "metadata", ()) or ())
    for arg in typing.get_args(annotation):
        if arg is type(None):
            continue
        out.extend(_annotation_metadata(arg))
    return out


def _unit_interval_fields() -> List[Tuple[type, str, Any]]:
    """Every ``[0, 1]``-bounded field in the engine schemas, from the PYTHON
    models.

    Deliberately NOT read off ``model_json_schema()``. The JSON emission is
    itself a thing that can break — annotating ``UnitInterval`` with the
    ``BeforeValidator`` *before* the ``Field`` makes pydantic emit ``ge``/``le``
    instead of ``minimum``/``maximum``, which silently empties the wire half of
    this fix. A guard that derived its expectation from the same emission it is
    checking would have passed straight through that.
    """
    import inspect

    found: List[Tuple[type, str, Any]] = []

    for _, obj in vars(engine_schemas).items():
        if not (
            inspect.isclass(obj)
            and issubclass(obj, BaseModel)
            and obj.__module__ == engine_schemas.__name__
        ):
            continue
        for field_name, field in obj.model_fields.items():
            # The bound may sit on the field, or inside the non-null branch of
            # an Optional union — walk both.
            metadata = _annotation_metadata(field.annotation) + list(field.metadata)
            lo = next((m.ge for m in metadata if isinstance(m, Ge)), None)
            hi = next((m.le for m in metadata if isinstance(m, Le)), None)
            if lo == 0.0 and hi == 1.0:
                found.append((obj, field_name, tuple(metadata)))
    return found


def test_every_unit_interval_field_is_bounded_in_python_not_only_on_the_wire():
    """The decoder-level fix only reaches providers that enforce constraints.

    Anthropic is FUNCTION_CALLING and every BEST_EFFORT provider is
    prompt-only, so on those the ``minimum`` is advisory and a model asked for
    confidence "on a 0-100 scale" answers ``95``. The clamp is what covers them,
    and it must be ``mode="before"``: ``ge``/``le`` are CORE constraints and run
    first, so an after-validator can never see the value it was written for.
    """
    fields = _unit_interval_fields()
    assert len(fields) >= 10, (
        f"only {len(fields)} [0, 1]-bounded fields found — the walker has "
        "stopped seeing them (Optional unions hide the metadata one level in), "
        "so this guard is watching nothing."
    )
    unclamped = [
        f"{cls.__name__}.{name}"
        for cls, name, metadata in fields
        if not any(isinstance(m, BeforeValidator) for m in metadata)
    ]
    assert not unclamped, (
        f"{unclamped} declare ge=0/le=1 with no before-clamp. On a provider "
        "that does not enforce the bound the value arrives out of range and "
        "the record is dropped (see the backstop test below)."
    )


@pytest.mark.parametrize(
    "model, payload, field",
    [
        (
            engine_schemas.EvidenceToAdd,
            {
                "summary": "x",
                "category": "symptom_evidence",
                "source_type": "user_description",
                "likelihood": 95,
            },
            "likelihood",
        ),
        (
            engine_schemas.ReasoningConclusion,
            {"observation": "o", "inference": "i", "confidence": 95},
            "confidence",
        ),
        (
            engine_schemas.MilestoneUpdates,
            {"root_cause_likelihood": 95},
            "root_cause_likelihood",
        ),
        (
            engine_schemas.HypothesisUpdate,
            {"hypothesis_id": "hyp_1", "likelihood": 95},
            "likelihood",
        ),
        (engine_schemas.WorkingConclusionUpdate, {"likelihood": 95}, "likelihood"),
        (
            engine_schemas.RootCauseConclusionUpdate,
            {"root_cause": "rc", "mechanism": "m", "likelihood": 95},
            "likelihood",
        ),
    ],
    ids=lambda a: a.__name__ if isinstance(a, type) else "",
)
def test_a_percentage_shaped_confidence_is_bounded_rather_than_rejected(
    model, payload, field
):
    """``95`` is what all three shipped Gemini models answer when the prompt
    talks in percentages and the constraint is not enforced.

    It clamps to ``1.0`` — an overstatement, deliberately not rescaled to
    ``0.95``: inferring intent from an out-of-range value is the
    post-generation-correction pattern ``agent-behavioral-rules.md`` rejects.
    The bound keeps the record; the decoder half gets the number right where a
    provider enforces it.
    """
    assert getattr(model.model_validate(payload), field) == 1.0


def test_a_non_numeric_confidence_still_raises():
    """The clamp bounds a number; it must not swallow a genuine type error."""
    with pytest.raises(Exception):
        engine_schemas.WorkingConclusionUpdate.model_validate({"likelihood": "high"})


def test_out_of_range_confidence_is_bounded_not_lost():
    """What the unbounded value actually costs, asserted rather than assumed.

    It is **not** a 500. ``milestone_engine`` has a never-500 backstop: an
    invalid list entry is PRUNED (the hypothesis is silently lost) and a
    non-list error drops **all** ``state_updates`` — including valid siblings —
    returning a conversational reply. That is harder to notice than a 500, and
    it is why "the turn 500s" was the wrong thing to write down.

    With the clamp, neither path is taken.
    """
    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine._record_schema_validation = lambda *a, **k: None

    payload = {
        "agent_response": "Looking into the pool.",
        "state_updates": {
            "milestones": {"root_cause_likelihood": 95},
            "hypotheses_to_add": [
                {
                    "statement": "pool exhausted",
                    "category": "environment",
                    "likelihood": 95,
                    "rationale": "r",
                }
            ],
        },
    }
    parsed = engine._validate_with_degradation(payload, InvestigationResponse_Diagnosis)

    assert parsed.agent_response == "Looking into the pool."
    assert parsed.state_updates.milestones.root_cause_likelihood == 1.0
    assert [h.likelihood for h in parsed.state_updates.hypotheses_to_add] == [1.0], (
        "the hypothesis was dropped instead of bounded — the backstop pruned "
        "it, which is the silent-loss failure this clamp exists to prevent"
    )


# ---------------------------------------------------------------------------
# Half 1: constraints survive the whole path, on both request shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", ENGINE_SCHEMAS, ids=lambda m: m.__name__)
def test_declared_constraints_reach_the_wire_on_both_request_shapes(model):
    """Every narrowing keyword the Pydantic model declares reaches Gemini.

    End-to-end over the real builders on purpose: the constraints used to be
    removed twice, so a guard on either strip list alone would have passed while
    the other still emptied the schema.

    ``>=`` because both builders inline ``$defs``: a definition used twice
    contributes two copies to the wire payload and one to the source schema.
    Gemini's ``Schema`` has no ``exclusiveMinimum``/``exclusiveMaximum``, so
    those are excluded from what the GEMINI wire is expected to carry — they are
    dropped at the provider boundary by design, not by a shared stripper.
    """
    declared = _count_keywords(model.model_json_schema())
    carryable = {
        k: v
        for k, v in declared.items()
        if k in GeminiProvider._GEMINI_SCHEMA_PROPERTIES
    }

    for path_name, wire in (
        ("tool-augmented", _tool_path_wire_schema(model)),
        ("response_schema", _response_schema_path_wire_schema(model)),
    ):
        sent = _count_keywords(wire)
        for keyword, count in carryable.items():
            assert sent.get(keyword, 0) >= count, (
                f"{model.__name__}: the {path_name} path sends "
                f"{sent.get(keyword, 0)} `{keyword}` where the model declares "
                f"{count}. Something on that path is stripping it."
            )


def test_the_parametrised_guard_is_watching_something():
    """Declare where the rule above CAN be violated, and fail if it looks
    nowhere.

    A SUBSET assertion, not an equality: pinning the exact set would fail on a
    strict improvement (a schema gaining a bound it did not have).
    ``TerminalResponse`` carries none — it is a closing summary, all free text.
    """
    declaring = {
        m.__name__: _count_keywords(m.model_json_schema())
        for m in ENGINE_SCHEMAS
        if _count_keywords(m.model_json_schema())
    }
    assert {
        "InquiryResponse",
        "InvestigationResponse_Diagnosis",
        "InvestigationResponse_Mitigation",
        "InvestigationResponse_Treatment",
        "InvestigationResponse_General",
    } <= set(declaring), f"a schema lost its value constraints: {sorted(declaring)}"
    assert all(
        "minimum" in counts and "maximum" in counts for counts in declaring.values()
    ), (
        f"a schema's [0, 1] bounds stopped being EMITTED as minimum/maximum: "
        f"{declaring}. Check the ordering inside `UnitInterval` — a "
        f"BeforeValidator ahead of the Field emits raw `ge`/`le` instead."
    )


def test_the_json_schema_emits_real_keywords_not_raw_field_kwargs():
    """``ge``/``le`` are pydantic kwargs, not JSON Schema.

    Emitting them means the bound left Python intact and vanished from the
    wire — and on a denylist adapter it would have been a hard Gemini 400,
    ``Unknown name "ge"``.
    """
    for model in ENGINE_SCHEMAS:
        raw = json.dumps(model.model_json_schema())
        assert '"ge"' not in raw and '"le"' not in raw, (
            f"{model.__name__} emits raw ge/le — see the ORDER note on "
            "`UnitInterval`."
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
# The provider boundary is an ALLOWLIST
# ---------------------------------------------------------------------------


class _ExoticShapes(BaseModel):
    """Ordinary pydantic types whose JSON Schema has no Gemini ``Schema``
    spelling. Every one of these was measured as a hard 400 on both request
    shapes (2026-09-17) before the allowlist."""

    class _Cat(BaseModel):
        kind: Literal["cat"]
        meow: bool

    class _Dog(BaseModel):
        kind: Literal["dog"]
        bark: bool

    step: int = Field(multiple_of=5)  # -> multipleOf
    pair: Tuple[int, str]  # -> prefixItems, and an array with no `items`
    pet: Union[_Cat, _Dog] = Field(discriminator="kind")  # -> oneOf/discriminator


def _walk(node: Any):
    """Yield every SCHEMA node in *node*.

    Deliberately not "every dict": the value of ``properties`` is a MAP whose
    keys are property names, not schema keywords, so a naive walk reports every
    field name as an illegal keyword. Recursion is down the keys that hold
    schemas.
    """
    if not isinstance(node, dict):
        return
    yield node
    for child in (node.get("properties") or {}).values():
        yield from _walk(child)
    for key in ("items", "additionalProperties"):
        yield from _walk(node.get(key))
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for child in node.get(key) or []:
            yield from _walk(child)
    for child in (node.get("$defs") or {}).values():
        yield from _walk(child)


def test_nothing_outside_geminis_schema_vocabulary_reaches_the_wire():
    """The class the old denylist could not close.

    It named ten keywords and still let ``multipleOf``, ``prefixItems`` and
    ``discriminator`` through — each ``400 Invalid JSON payload received.
    Unknown name "…"``. A denylist only removes what someone thought of; this
    asserts the complement.
    """
    wire = GeminiProvider._resolve_refs_for_gemini(_ExoticShapes.model_json_schema())
    for node in _walk(wire):
        extra = set(node) - GeminiProvider._GEMINI_ALLOWED_FIELDS
        assert not extra, (
            f"{sorted(extra)} is outside Gemini's `Schema` message and is a "
            f"400 on both request shapes. Node: {node}"
        )


def test_every_array_reaching_gemini_declares_its_item_type():
    """Gemini rejects an array without ``items`` — ``properties[a].items:
    missing field`` — and a bare ``{"type": "array"}`` for the same reason
    (both measured, both request shapes).

    Dropping ``prefixItems`` alone leaves exactly that shape, so the tuple is
    downgraded to a homogeneous array instead.
    """
    subjects = [_ExoticShapes.model_json_schema()] + [
        m.model_json_schema() for m in ENGINE_SCHEMAS
    ]
    for schema in subjects:
        wire = GeminiProvider._resolve_refs_for_gemini(schema)
        for node in _walk(wire):
            if node.get("type") == "array":
                assert "items" in node, f"array with no item type: {node}"


def test_the_allowlist_is_the_published_schema_vocabulary():
    """``_GEMINI_SCHEMA_PROPERTIES`` is the v1beta discovery document's
    ``Schema`` property list (revision 20260917). Two deliberate exceptions are
    subtracted, and they are named rather than assumed."""
    assert GeminiProvider._GEMINI_DELIBERATE_EXTRA_STRIPS == {"title", "default"}
    assert (
        GeminiProvider._GEMINI_ALLOWED_FIELDS
        == GeminiProvider._GEMINI_SCHEMA_PROPERTIES
        - GeminiProvider._GEMINI_DELIBERATE_EXTRA_STRIPS
    )
    # A spot-check that the list is the API's and not an invention: these are
    # the ones whose absence caused fm#355, and these are the ones the API has
    # no field for.
    assert NARROWING_KEYWORDS - {"exclusiveMinimum", "exclusiveMaximum"} <= (
        GeminiProvider._GEMINI_SCHEMA_PROPERTIES
    )
    for absent in (
        "additionalProperties",
        "$schema",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "uniqueItems",
        "const",
        "oneOf",
        "allOf",
        "multipleOf",
        "prefixItems",
        "discriminator",
    ):
        assert absent not in GeminiProvider._GEMINI_SCHEMA_PROPERTIES


def test_const_survives_the_reduction_as_an_enum():
    """``const`` has no ``Schema`` spelling, but its CONSTRAINT does. Dropping
    it without the rewrite lets a single-value ``Literal`` reach the model as a
    bare ``{"type": "string"}``."""

    class Pinned(BaseModel):
        kind: Literal["only_value"]

    wire = GeminiProvider._resolve_refs_for_gemini(Pinned.model_json_schema())
    assert wire["properties"]["kind"]["enum"] == ["only_value"]


# ---------------------------------------------------------------------------
# The strict rewrite is OpenAI's subset, not the intersection of all providers
# ---------------------------------------------------------------------------


def test_no_narrowing_keyword_is_dropped_by_the_strict_rewrite():
    """``to_strict_schema`` is applied to BOTH engine paths, so a narrowing
    keyword in its drop list is removed before any provider sees it — which is
    why fixing only the Gemini adapter would have changed nothing."""
    dropped = _STRICT_UNSUPPORTED_KEYWORDS & NARROWING_KEYWORDS
    assert not dropped, (
        f"{sorted(dropped)} decide which documents validate. Dropping them "
        "asks the model for values the Pydantic model then rejects."
    )


def test_the_strict_rewrite_still_drops_what_openai_refuses():
    """``uniqueItems`` is measured rejected by OpenAI strict — ``400 Invalid
    schema for response_format: … 'uniqueItems' is not permitted`` — so it must
    stay dropped. It was missing from the list before fm#355."""
    assert "uniqueItems" in _STRICT_UNSUPPORTED_KEYWORDS

    class WithUnique(BaseModel):
        tags: List[str] = Field(json_schema_extra={"uniqueItems": True})

    from faultmaven.utils.schema_converter import to_strict_schema

    out = json.dumps(to_strict_schema(WithUnique.model_json_schema()))
    assert '"uniqueItems"' not in out


def test_a_keyword_gemini_cannot_carry_is_dropped_at_the_provider_not_upstream():
    """``exclusiveMinimum`` is accepted by OpenAI strict and absent from
    Gemini's ``Schema``. It must survive the shared rewrite (so OpenAI enforces
    it) and be dropped by the Gemini adapter (so Gemini does not 400) — the
    difference between a per-provider boundary and an intersection."""

    class Exclusive(BaseModel):
        ratio: float = Field(gt=0.0)

    from faultmaven.utils.schema_converter import to_strict_schema

    strict = to_strict_schema(Exclusive.model_json_schema())
    assert '"exclusiveMinimum"' in json.dumps(
        strict
    ), "the shared rewrite dropped it, so OpenAI never gets to enforce it"
    assert '"exclusiveMinimum"' not in json.dumps(
        GeminiProvider._resolve_refs_for_gemini(strict)
    ), "Gemini has no field for it; sending it is a 400"


# ---------------------------------------------------------------------------
# The investigation TOOL declarations go through the same resolver
# ---------------------------------------------------------------------------


def test_investigation_tool_declarations_stay_within_the_allowlist():
    """The schema tool is not the only thing this resolver touches.

    Widening it also widened what the investigation tools send —
    ``list_evidence`` and ``search_knowledge`` now ship
    ``{"minimum": 1, "maximum": 100}`` on their ``limit`` where they used to
    ship it bare (measured accepted, ``limit: 3`` returned in range). The same
    allowlist has to hold for them, and nothing here may be a 400 either.
    """
    import importlib
    import inspect
    import pkgutil

    import faultmaven.modules.agent.tools as tools_pkg

    checked = 0
    for module_info in pkgutil.iter_modules(tools_pkg.__path__):
        try:
            module = importlib.import_module(f"{tools_pkg.__name__}.{module_info.name}")
        except Exception:
            continue
        for _, obj in vars(module).items():
            if not (inspect.isclass(obj) and obj.__module__ == module.__name__):
                continue
            if not hasattr(obj, "parameters_schema"):
                continue
            try:
                schema = obj.__new__(obj).parameters_schema
            except Exception:
                continue
            if not isinstance(schema, dict):
                continue
            checked += 1
            wire = GeminiProvider._resolve_refs_for_gemini(schema)
            for node in _walk(wire):
                extra = set(node) - GeminiProvider._GEMINI_ALLOWED_FIELDS
                assert not extra, f"{obj.__name__}: {sorted(extra)} in {node}"
                if node.get("type") == "array":
                    assert "items" in node, f"{obj.__name__}: {node}"

    assert checked >= 10, (
        f"only {checked} tool parameter schemas were read — `parameters_schema` "
        "is an INSTANCE property, so a class-attribute sweep silently reads "
        "zero and this guard passes vacuously."
    )
