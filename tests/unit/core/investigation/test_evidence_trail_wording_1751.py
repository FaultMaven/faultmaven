"""The investigation tool schemas ask for evidence, never for reasoning (fm#1751).

**The rule (2026-09-28, fm#1751).** Anthropic's ``reasoning_extraction``
classifier refuses a schema that asks the model to write out its reasoning, by
name or by wording (measured on ``claude-opus-5``, ``claude-opus-5-5`` and
``claude-fable-5-1``, 2026-09-28, #1751). The ``reasoning`` keys under
``state_updates`` were measured safe and are deliberately not pinned.

The refusal is HTTP 200 with ``stop_reason: "refusal"`` and no content, so
every INVESTIGATING turn on those models failed while INQUIRY and TERMINAL
turns, whose schemas carry no such field, went through. The field that
triggered it was ``internal_reasoning`` (class ``InternalReasoning``, items
``ReasoningConclusion``, "Step-by-step reasoning from evidence to
conclusions"). It is now ``evidence_trail``: the same mechanism, worded as what
was observed, what it implies and which milestone the evidence justifies.

The classifier is not something a unit test can call, so this pins the
wording; the live INVESTIGATING turn recorded on the pull request proves it.
It walks exactly what is sent: ``pydantic_to_openai_tools(cls)`` for every
``InvestigationResponse_*`` class. A class docstring there becomes a schema
``description`` and a class name a ``title``, so both are model-facing.

- **(a)** no top-level property name matches ``reason|thought|thinking``;
- **(b)** inside the ``evidence_trail`` property's subtree, no key, ``title``
  or ``description`` matches ``reason|step[- ]by[- ]step``;
- **(c)** nowhere in the tool — keys, names, titles, descriptions — does
  "internal reasoning", "step-by-step reasoning" or "reasoning step" appear,
  in any case and with any separator.

Each predicate has a positive control that injects the defect into a copy of
the real schema and must be caught.
"""

from __future__ import annotations

import copy
import re
import string
from typing import Any, Iterator

import pytest

from faultmaven.core.investigation import schemas
from faultmaven.core.investigation.prompts.templates.investigation import (
    INVESTIGATION_BASE,
    SCHEMA_INSTRUCTIONS,
)
from faultmaven.utils.schema_converter import pydantic_to_openai_tools

pytestmark = pytest.mark.unit

_TOP_LEVEL = re.compile(r"reason|thought|thinking", re.IGNORECASE)
_SUBTREE = re.compile(r"reason|step[- ]by[- ]step", re.IGNORECASE)
_PHRASES = (
    re.compile(r"internal[\s_-]*reasoning", re.IGNORECASE),
    re.compile(r"step[\s_-]*by[\s_-]*step[\s_-]*reasoning", re.IGNORECASE),
    re.compile(r"reasoning[\s_-]*step", re.IGNORECASE),
)

#: The classes the rule is known to cover. Discovery below must find at least
#: these, so a rename that hides them from the prefix cannot empty the check.
_KNOWN = {
    "InvestigationResponse_Diagnosis",
    "InvestigationResponse_Mitigation",
    "InvestigationResponse_Treatment",
    "InvestigationResponse_General",
}

_DISCOVERED = sorted(
    name
    for name, obj in vars(schemas).items()
    if name.startswith("InvestigationResponse_") and isinstance(obj, type)
)


def _tool(cls_name: str) -> dict[str, Any]:
    """The tool definition exactly as the engine sends it."""
    return pydantic_to_openai_tools(getattr(schemas, cls_name))[0]


def _params(tool: dict[str, Any]) -> dict[str, Any]:
    return tool["function"]["parameters"]


def _subtree_texts(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Every property key, ``title`` and ``description`` under ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}"
            if key in ("title", "description") and isinstance(value, str):
                yield here, value
            if key == "properties" and isinstance(value, dict):
                for prop in value:
                    yield f"{here}[key]", prop
            yield from _subtree_texts(value, here)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _subtree_texts(value, f"{path}[{i}]")


def _all_strings(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Every string in the tool, dict keys included."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}[key]", str(key)
            yield from _all_strings(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _all_strings(value, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def _top_level_violations(tool: dict[str, Any]) -> list[str]:
    """(a): top-level property names that name reasoning or thinking."""
    return [name for name in _params(tool)["properties"] if _TOP_LEVEL.search(name)]


def _subtree_violations(tool: dict[str, Any]) -> list[str]:
    """(b): reasoning wording anywhere in the ``evidence_trail`` subtree."""
    subtree = _params(tool)["properties"]["evidence_trail"]
    return [
        f"{path}: {text!r}"
        for path, text in _subtree_texts(subtree, "evidence_trail")
        if _SUBTREE.search(text)
    ]


def _phrase_violations(tool: dict[str, Any]) -> list[str]:
    """(c): the bisected trigger phrases anywhere in the tool."""
    return [
        f"{path}: {text[:120]!r}"
        for path, text in _all_strings(tool)
        if any(p.search(text) for p in _PHRASES)
    ]


def _object_branch(prop: dict[str, Any]) -> dict[str, Any]:
    """The object member of an ``Optional[Model]`` property's ``anyOf``."""
    return next(m for m in prop["anyOf"] if "properties" in m)


def _array_branch(prop: dict[str, Any]) -> dict[str, Any]:
    """The array member of an ``Optional[List[...]]`` property's ``anyOf``."""
    return next(m for m in prop["anyOf"] if "items" in m)


# ---------------------------------------------------------------------------
# Where the rule looks
# ---------------------------------------------------------------------------


def test_discovery_covers_every_known_investigation_schema():
    assert _KNOWN <= set(_DISCOVERED), sorted(_KNOWN - set(_DISCOVERED))


@pytest.mark.parametrize("cls_name", _DISCOVERED)
def test_the_walk_reaches_the_evidence_trail_subtree(cls_name):
    """Guard the guard: (b) would pass vacuously on a missing subtree, and the
    walk would miss text left behind a ``$ref`` it does not follow."""
    tool = _tool(cls_name)
    assert "evidence_trail" in _params(tool)["properties"]
    assert "$ref" not in repr(tool) and "$defs" not in repr(tool)
    trail = _params(tool)["properties"]["evidence_trail"]
    seen = {text for _, text in _subtree_texts(trail)}
    assert {"conclusions", "milestone_justifications", "observation"} <= seen
    assert "EvidenceTrail" in seen and "EvidenceConclusion" in seen


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls_name", _DISCOVERED)
def test_no_top_level_property_names_reasoning(cls_name):
    assert _top_level_violations(_tool(cls_name)) == []


@pytest.mark.parametrize("cls_name", _DISCOVERED)
def test_evidence_trail_subtree_has_no_reasoning_wording(cls_name):
    assert _subtree_violations(_tool(cls_name)) == []


@pytest.mark.parametrize("cls_name", _DISCOVERED)
def test_no_trigger_phrase_anywhere_in_the_tool(cls_name):
    assert _phrase_violations(_tool(cls_name)) == []


def test_the_prompt_names_the_evidence_trail_not_internal_reasoning():
    """The prompt asks for the field by name, so it must use the new one, and
    its heading must not ask for reasoning first."""
    names = {f[1] for f in string.Formatter().parse(INVESTIGATION_BASE) if f[1]}
    rendered = INVESTIGATION_BASE.format(**{n: "" for n in names})
    for text in (rendered, SCHEMA_INSTRUCTIONS):
        assert "internal_reasoning" not in text
        assert "reasoning-first" not in text.lower()
        assert not any(p.search(text) for p in _PHRASES)
    assert "CRITICAL: EVIDENCE-FIRST REQUIREMENT" in rendered
    assert "provide evidence_trail BEFORE state_updates" in rendered
    assert "- **evidence_trail**:" in SCHEMA_INSTRUCTIONS


# ---------------------------------------------------------------------------
# Positive controls: each predicate catches the defect it exists for
# ---------------------------------------------------------------------------


def _diagnosis() -> dict[str, Any]:
    return copy.deepcopy(_tool("InvestigationResponse_Diagnosis"))


def _trail(tool: dict[str, Any]) -> dict[str, Any]:
    return _params(tool)["properties"]["evidence_trail"]


def _conclusion_items(tool: dict[str, Any]) -> dict[str, Any]:
    conclusions = _object_branch(_trail(tool))["properties"]["conclusions"]
    return _array_branch(conclusions)["items"]


def _inject_field_description(tool):
    _trail(tool)["description"] = "Step-by-step reasoning"


def _inject_class_description(tool):
    _object_branch(_trail(tool))["description"] = "Step-by-step reasoning"


def _inject_items_description(tool):
    _conclusion_items(tool)["description"] = "Step-by-step reasoning"


def _inject_items_title(tool):
    _conclusion_items(tool)["title"] = "ReasoningConclusion"


def _inject_nested_key(tool):
    props = _object_branch(_trail(tool))["properties"]
    props["reasoning_steps"] = props.pop("conclusions")


@pytest.mark.parametrize(
    "inject",
    [
        _inject_field_description,
        _inject_class_description,
        _inject_items_description,
        _inject_items_title,
        _inject_nested_key,
    ],
)
def test_subtree_predicate_catches_injected_reasoning_wording(inject):
    tool = _diagnosis()
    assert _subtree_violations(tool) == []
    inject(tool)
    assert _subtree_violations(tool) != []


def test_top_level_predicate_catches_an_internal_reasoning_key():
    tool = _diagnosis()
    props = _params(tool)["properties"]
    props["internal_reasoning"] = copy.deepcopy(props["evidence_trail"])
    assert _top_level_violations(tool) == ["internal_reasoning"]
    assert _phrase_violations(tool) != []


def test_phrase_predicate_reads_the_whole_tool_across_line_breaks():
    """The old docstring spanned lines; a phrase broken by a newline outside
    the ``evidence_trail`` subtree is still caught."""
    tool = _diagnosis()
    _params(tool)["properties"]["state_updates"][
        "description"
    ] = "Internal\nreasoning that must be completed BEFORE state_updates."
    assert _subtree_violations(tool) == []
    assert _phrase_violations(tool) != []
