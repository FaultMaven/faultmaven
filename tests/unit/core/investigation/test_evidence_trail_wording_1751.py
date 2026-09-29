"""The structured response schemas ask for evidence, never for reasoning (fm#1751).

**The rule (2026-09-28, fm#1751).** Anthropic's ``reasoning_extraction``
classifier refuses a schema that asks the model to write out its reasoning, by
name or by wording (measured on ``claude-opus-5``, ``claude-opus-5-5`` and
``claude-fable-5-1``, 2026-09-28, #1751). The refusal is HTTP 200 with
``stop_reason: "refusal"`` and no content, so every INVESTIGATING turn on those
models failed while INQUIRY and TERMINAL turns went through. The trigger was the
``internal_reasoning`` field (``InternalReasoning``, ``ReasoningConclusion``,
"Step-by-step reasoning from evidence to conclusions"); it is now
``evidence_trail``, the same mechanism worded as evidence.

The classifier is not something a unit test can call, so this file pins the
wording; the live INVESTIGATING turns recorded on the pull request prove it.

**What the census walks.** Every structured response schema the engine sends
as a tool — ``InquiryResponse``, ``TerminalResponse`` and every
``InvestigationResponse_*``, discovered from where the engine chooses a turn's
schema (``turn_generation``) — through BOTH tool converters,
``pydantic_to_openai_tools`` and ``pydantic_to_strict_openai_tools``. It reads
every key (split on ``_``) and every string value: the tool's name and
description, property names, titles, descriptions, enum values, defaults,
``examples`` and anything ``json_schema_extra`` adds. Text is normalised
(NFKC, then U+2010-U+2015 and U+2212 to ``-``, then lowercase) and matched
against one vocabulary naming the idiom class, not the three bisected strings.

**Today's hits are declared** in ``ALLOWLIST`` with their counts per converter
and their measured basis. A new hit, a changed count or a stale entry fails.
The remedy is never to widen the allowlist blind: measure the new wording live
on the three models, or reword it.

**What it deliberately does not walk.**

- The json_object / prompt-only path (``_schema_prompt_instruction``) and the
  ``json_schema`` response_format path. Both carry the same model text, but
  they reach only non-Claude providers: Anthropic, and OpenRouter's Claude
  routes, run FUNCTION_CALLING with ``include_schema_in_prompt=False``, so a
  Claude model receives the schema only as a tool.
- Prompt vocabulary. The template constants are pinned below for the OLD
  names and the three bisected phrases only. That prompt text alone triggers
  the classifier is unmeasured, and the live DIAGNOSIS turns passed with
  today's blocks, ``_DIAGNOSTIC_REASONING_BLOCK`` included.

The package source is pinned separately for the old names, because a rename
that misses a model-facing string (a feedback message, a prompt block) leaves
the schema clean and the prompt asking for a field that no longer exists.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import pkgutil
import re
import types
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import pytest

import faultmaven
import faultmaven.core.investigation.prompts.templates as templates_pkg
from faultmaven.core.investigation import schemas
from faultmaven.core.investigation.milestone_engine import turn_generation
from faultmaven.core.investigation.prompts.templates.investigation import (
    INVESTIGATION_BASE,
    SCHEMA_INSTRUCTIONS,
)
from faultmaven.modules.case.contracts import InvestigationStage
from faultmaven.utils.schema_converter import (
    pydantic_to_openai_tools,
    pydantic_to_strict_openai_tools,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Shared vocabulary
# ---------------------------------------------------------------------------

#: The idiom class the classifier refused: a request to write out reasoning.
VOCABULARY = re.compile(
    r"reason|rationale|chain[- ]of[- ]thought|thought|thinking|\bthink\b"
    r"|step[- ]by[- ]step|show your work|deliberat|internal analysis|explain your"
)

#: The names this PR retired. Nothing in the package may carry them.
OLD_NAMES = (
    "internal_reasoning",
    "InternalReasoning",
    "ReasoningConclusion",
    "REASONING-FIRST",
    "REASONING VALIDATION",
)

#: The three strings #1751's bisection measured as refused.
BISECTED_PHRASES = (
    re.compile(r"internal[\s_-]*reasoning"),
    re.compile(r"step[\s_-]*by[\s_-]*step[\s_-]*reasoning"),
    re.compile(r"reasoning[\s_-]*step"),
)

_HYPHENS = {cp: "-" for cp in [*range(0x2010, 0x2016), 0x2212]}


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_HYPHENS).lower()


# ---------------------------------------------------------------------------
# The schema census
# ---------------------------------------------------------------------------

CONVERTERS = (pydantic_to_openai_tools, pydantic_to_strict_openai_tools)

#: The six schemas the rule is known to cover. Discovery must find them all.
KNOWN_SCHEMAS = {
    "InquiryResponse",
    "TerminalResponse",
    "InvestigationResponse_Diagnosis",
    "InvestigationResponse_Mitigation",
    "InvestigationResponse_Treatment",
    "InvestigationResponse_General",
}


def _discovered() -> list[type]:
    """Every schema ``turn_generation`` can choose for a turn."""
    found = {turn_generation.InquiryResponse, turn_generation.TerminalResponse}
    found |= {
        turn_generation.get_schema_for_stage(stage)
        for stage in [*InvestigationStage, None]
    }
    found |= {
        obj
        for name, obj in vars(schemas).items()
        if name.startswith("InvestigationResponse_") and isinstance(obj, type)
    }
    return sorted(found, key=lambda cls: cls.__name__)


_COMBINATORS = {"anyOf", "oneOf", "allOf"}


def _walk_schema(node: Any, path: str) -> Iterator[tuple[str, str, str]]:
    """Every key and string value under a JSON schema node.

    Yields ``(path, kind, text)``. ``path`` names properties (``[]`` for array
    items; combinator branches share their parent's path). ``kind`` is
    ``key`` for a property name, ``key:<k>`` for any other key, and the key
    whose value the text is otherwise (``description``, ``enum[]``, ...).
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for prop, sub in value.items():
                    here = f"{path}.{prop}" if path else prop
                    yield here, "key", prop
                    yield from _walk_schema(sub, here)
            elif key in _COMBINATORS and isinstance(value, list):
                for branch in value:
                    yield from _walk_schema(branch, path)
            elif key == "items":
                yield from _walk_schema(value, f"{path}[]")
            else:
                yield path, f"key:{key}", key
                if isinstance(value, str):
                    yield path, key, value
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            yield path, f"{key}[]", item
                        else:
                            yield from _walk_schema(item, path)
                elif isinstance(value, dict):
                    yield from _walk_schema(value, f"{path}.@{key}")


def _walk_tool(tool: dict[str, Any]) -> Iterator[tuple[str, str, str]]:
    """Every key and string value of a tool definition, schema included."""
    for key, value in tool.items():
        if key == "function":
            continue
        yield "", f"key:{key}", key
        if isinstance(value, str):
            yield "", key, value
    for key, value in tool["function"].items():
        if key == "parameters":
            yield from _walk_schema(value, "")
            continue
        yield "", f"function.key:{key}", key
        if isinstance(value, str):
            yield "", f"function.{key}", value


def _probe(kind: str, text: str) -> str:
    """The text the vocabulary is matched against: keys split on ``_``."""
    if kind == "key" or kind.startswith(("key:", "function.key:")):
        text = text.replace("_", " ")
    return _normalise(text)


def _digest(probe: str) -> str:
    """The first 12 hex digits of SHA-256 over a normalised full string."""
    return hashlib.sha256(probe.encode("utf-8")).hexdigest()[:12]


def _census(tool: dict[str, Any], schema_name: str) -> dict[tuple, int]:
    """Hits keyed by where they are, the token, and the WHOLE string they sit in.

    The digest is what binds an allowlisted hit to the text that was measured:
    a count alone lets a measured-safe string be rewritten into an extraction
    request that keeps its token count (the round-20 delta review did exactly
    that to ``suggested_follow_ups[].body``). Any edit to such a string now
    reports a new hit and a stale entry.
    """
    counts: dict[tuple, int] = {}
    for path, kind, text in _walk_tool(tool):
        probe = _probe(kind, text)
        for match in VOCABULARY.finditer(probe):
            key = (schema_name, path, kind, match.group(0), _digest(probe))
            counts[key] = counts.get(key, 0) + 1
    return counts


@lru_cache(maxsize=1)
def _real_tools() -> dict[tuple[str, int], dict[str, Any]]:
    return {
        (cls.__name__, i): convert(cls)[0]
        for cls in _discovered()
        for i, convert in enumerate(CONVERTERS)
    }


def _tools() -> dict[tuple[str, int], dict[str, Any]]:
    """A private deep copy, so a control's injection cannot leak."""
    return copy.deepcopy(_real_tools())


def _table(tools: dict[tuple[str, int], dict[str, Any]]) -> dict[tuple, tuple]:
    table: dict[tuple, list[int]] = {}
    for (name, i), tool in tools.items():
        for key, n in _census(tool, name).items():
            table.setdefault(key, [0, 0])[i] += n
    return {key: tuple(v) for key, v in table.items()}


def _drift(table: dict[tuple, tuple]) -> list[str]:
    out = []
    for key in sorted(set(table) | set(ALLOWLIST)):
        got, want = table.get(key, (0, 0)), ALLOWLIST.get(key, (0, 0))
        if got == want:
            continue
        if key not in ALLOWLIST:
            what = "new hit"
        elif key not in table:
            what = "stale entry"
        else:
            what = "count changed"
        out.append(f"{what}: {key} (plain, strict) = {got}, allowlisted {want}")
    return out


# Today's hits, measured safe. A ``# <schema> (n): <basis>`` line opens a
# schema's rows; each row is ``path | kind | matched text | digest | plain
# strict``. The digest is ``_digest`` of the normalised full string the hit sits
# in (for a key, the key itself), so the entry allowlists that string as it was
# measured and no other; the counts are under pydantic_to_openai_tools and
# pydantic_to_strict_openai_tools.
# ``required[]`` rows are strict-only where the plain schema leaves the field
# optional. The bases: #1768's live check sent InquiryResponse and
# InvestigationResponse_Diagnosis to the three models (Diagnosis reached only
# claude-opus-5 and claude-opus-5-5; claude-fable-5-1's credit ran out), and
# #1751's bisection sent InquiryResponse, TerminalResponse, Diagnosis's
# state_updates alone, and the full Diagnosis schema with only the old subtree
# reworded. Mitigation, Treatment and General were not sent; every hit in them
# is Diagnosis's text at the same path (test_unmeasured_schemas_carry_only_
# measured_text holds that true).
_ALLOWLIST_TABLE = """
# InquiryResponse (2): tool_use on claude-opus-5, claude-opus-5-5 and claude-fable-5-1 in #1768's live check (setup turns); on claude-opus-5-5 in #1751's bisection
state_updates.proposed_transition                           | description | reason       | 9e2de6d7389b | 1 1
suggested_follow_ups[].body                                 | description | reason       | b9369e54eb23 | 1 1
# TerminalResponse (1): tool_use on claude-opus-5-5 in #1751's bisection
suggested_follow_ups[].body                                 | description | reason       | b9369e54eb23 | 1 1
# InvestigationResponse_Diagnosis (36): tool_use on claude-opus-5 and claude-opus-5-5 in #1768's live check, and in #1751's bisection
state_updates.causal_edges_to_add[]                         | required[]  | reason       | 0c4d01e81bb3 | 0 1
state_updates.causal_edges_to_add[].reasoning               | key         | reason       | 0c4d01e81bb3 | 1 1
state_updates.causal_edges_to_add[].reasoning               | title       | reason       | 0c4d01e81bb3 | 1 1
state_updates.deductive_validations[]                       | required[]  | rationale    | 609bcec6c9a3 | 1 1
state_updates.deductive_validations[].exhaustive_rationale  | key         | rationale    | 98273c1a80c5 | 1 1
state_updates.deductive_validations[].exhaustive_rationale  | title       | rationale    | 98273c1a80c5 | 1 1
state_updates.evidence_need_updates[]                       | required[]  | rationale    | f350895acb59 | 0 1
state_updates.evidence_need_updates[]                       | required[]  | reason       | eec86fb17b2e | 0 1
state_updates.evidence_need_updates[].rationale             | description | rationale    | 8ba2dcb32a9c | 1 1
state_updates.evidence_need_updates[].rationale             | key         | rationale    | f350895acb59 | 1 1
state_updates.evidence_need_updates[].rationale             | title       | rationale    | f350895acb59 | 1 1
state_updates.evidence_need_updates[].state                 | description | reason       | 13a9c6522149 | 1 1
state_updates.evidence_need_updates[].superseded_reason     | key         | reason       | 05c55fa70218 | 1 1
state_updates.evidence_need_updates[].superseded_reason     | title       | reason       | 05c55fa70218 | 1 1
state_updates.evidence_to_add[]                             | description | deliberat    | c8328800d4a9 | 1 1
state_updates.evidence_to_add[].category                    | description | deliberat    | a226544e1e9f | 1 1
state_updates.hypotheses_to_add[]                           | required[]  | rationale    | f350895acb59 | 1 1
state_updates.hypotheses_to_add[].rationale                 | key         | rationale    | f350895acb59 | 1 1
state_updates.hypotheses_to_add[].rationale                 | title       | rationale    | f350895acb59 | 1 1
state_updates.hypotheses_to_update[]                        | description | reason       | 4c53faaa6606 | 2 2
state_updates.hypotheses_to_update[]                        | required[]  | reason       | 1e7baccbb097 | 0 1
state_updates.hypotheses_to_update[].refutation_reason      | description | reason       | 2cf222ce0a69 | 2 2
state_updates.hypotheses_to_update[].refutation_reason      | key         | reason       | ee73da7edee0 | 1 1
state_updates.hypotheses_to_update[].refutation_reason      | title       | reason       | ee73da7edee0 | 1 1
state_updates.hypothesis_evidence_links[]                   | required[]  | reason       | 0c4d01e81bb3 | 1 1
state_updates.hypothesis_evidence_links[].reasoning         | key         | reason       | 0c4d01e81bb3 | 1 1
state_updates.hypothesis_evidence_links[].reasoning         | title       | reason       | 0c4d01e81bb3 | 1 1
state_updates.node_evidence_links[]                         | description | step-by-step | 862ce8401aa4 | 1 1
state_updates.node_evidence_links[]                         | required[]  | reason       | 0c4d01e81bb3 | 1 1
state_updates.node_evidence_links[].reasoning               | key         | reason       | 0c4d01e81bb3 | 1 1
state_updates.node_evidence_links[].reasoning               | title       | reason       | 0c4d01e81bb3 | 1 1
state_updates.proposed_transition                           | description | reason       | 9e2de6d7389b | 1 1
state_updates.verification_updates                          | required[]  | rationale    | 620a5898364c | 0 1
state_updates.verification_updates.rca_infeasible_rationale | key         | rationale    | b28c2f0137cc | 1 1
state_updates.verification_updates.rca_infeasible_rationale | title       | rationale    | b28c2f0137cc | 1 1
suggested_follow_ups[].body                                 | description | reason       | b9369e54eb23 | 1 1
# InvestigationResponse_Mitigation (12): not sent live; each hit is Diagnosis's text at the same path
state_updates.evidence_need_updates[]                       | required[]  | rationale    | f350895acb59 | 0 1
state_updates.evidence_need_updates[]                       | required[]  | reason       | eec86fb17b2e | 0 1
state_updates.evidence_need_updates[].rationale             | description | rationale    | 8ba2dcb32a9c | 1 1
state_updates.evidence_need_updates[].rationale             | key         | rationale    | f350895acb59 | 1 1
state_updates.evidence_need_updates[].rationale             | title       | rationale    | f350895acb59 | 1 1
state_updates.evidence_need_updates[].state                 | description | reason       | 13a9c6522149 | 1 1
state_updates.evidence_need_updates[].superseded_reason     | key         | reason       | 05c55fa70218 | 1 1
state_updates.evidence_need_updates[].superseded_reason     | title       | reason       | 05c55fa70218 | 1 1
state_updates.evidence_to_add[]                             | description | deliberat    | c8328800d4a9 | 1 1
state_updates.evidence_to_add[].category                    | description | deliberat    | a226544e1e9f | 1 1
state_updates.proposed_transition                           | description | reason       | 9e2de6d7389b | 1 1
suggested_follow_ups[].body                                 | description | reason       | b9369e54eb23 | 1 1
# InvestigationResponse_Treatment (30): not sent live; each hit is Diagnosis's text at the same path
state_updates.causal_edges_to_add[]                         | required[]  | reason       | 0c4d01e81bb3 | 0 1
state_updates.causal_edges_to_add[].reasoning               | key         | reason       | 0c4d01e81bb3 | 1 1
state_updates.causal_edges_to_add[].reasoning               | title       | reason       | 0c4d01e81bb3 | 1 1
state_updates.evidence_need_updates[]                       | required[]  | rationale    | f350895acb59 | 0 1
state_updates.evidence_need_updates[]                       | required[]  | reason       | eec86fb17b2e | 0 1
state_updates.evidence_need_updates[].rationale             | description | rationale    | 8ba2dcb32a9c | 1 1
state_updates.evidence_need_updates[].rationale             | key         | rationale    | f350895acb59 | 1 1
state_updates.evidence_need_updates[].rationale             | title       | rationale    | f350895acb59 | 1 1
state_updates.evidence_need_updates[].state                 | description | reason       | 13a9c6522149 | 1 1
state_updates.evidence_need_updates[].superseded_reason     | key         | reason       | 05c55fa70218 | 1 1
state_updates.evidence_need_updates[].superseded_reason     | title       | reason       | 05c55fa70218 | 1 1
state_updates.evidence_to_add[]                             | description | deliberat    | c8328800d4a9 | 1 1
state_updates.evidence_to_add[].category                    | description | deliberat    | a226544e1e9f | 1 1
state_updates.hypotheses_to_add[]                           | required[]  | rationale    | f350895acb59 | 1 1
state_updates.hypotheses_to_add[].rationale                 | key         | rationale    | f350895acb59 | 1 1
state_updates.hypotheses_to_add[].rationale                 | title       | rationale    | f350895acb59 | 1 1
state_updates.hypotheses_to_update[]                        | description | reason       | 4c53faaa6606 | 2 2
state_updates.hypotheses_to_update[]                        | required[]  | reason       | 1e7baccbb097 | 0 1
state_updates.hypotheses_to_update[].refutation_reason      | description | reason       | 2cf222ce0a69 | 2 2
state_updates.hypotheses_to_update[].refutation_reason      | key         | reason       | ee73da7edee0 | 1 1
state_updates.hypotheses_to_update[].refutation_reason      | title       | reason       | ee73da7edee0 | 1 1
state_updates.hypothesis_evidence_links[]                   | required[]  | reason       | 0c4d01e81bb3 | 1 1
state_updates.hypothesis_evidence_links[].reasoning         | key         | reason       | 0c4d01e81bb3 | 1 1
state_updates.hypothesis_evidence_links[].reasoning         | title       | reason       | 0c4d01e81bb3 | 1 1
state_updates.node_evidence_links[]                         | description | step-by-step | 862ce8401aa4 | 1 1
state_updates.node_evidence_links[]                         | required[]  | reason       | 0c4d01e81bb3 | 1 1
state_updates.node_evidence_links[].reasoning               | key         | reason       | 0c4d01e81bb3 | 1 1
state_updates.node_evidence_links[].reasoning               | title       | reason       | 0c4d01e81bb3 | 1 1
state_updates.proposed_transition                           | description | reason       | 9e2de6d7389b | 1 1
suggested_follow_ups[].body                                 | description | reason       | b9369e54eb23 | 1 1
# InvestigationResponse_General (33): not sent live; each hit is Diagnosis's text at the same path
state_updates.causal_edges_to_add[]                         | required[]  | reason       | 0c4d01e81bb3 | 0 1
state_updates.causal_edges_to_add[].reasoning               | key         | reason       | 0c4d01e81bb3 | 1 1
state_updates.causal_edges_to_add[].reasoning               | title       | reason       | 0c4d01e81bb3 | 1 1
state_updates.evidence_need_updates[]                       | required[]  | rationale    | f350895acb59 | 0 1
state_updates.evidence_need_updates[]                       | required[]  | reason       | eec86fb17b2e | 0 1
state_updates.evidence_need_updates[].rationale             | description | rationale    | 8ba2dcb32a9c | 1 1
state_updates.evidence_need_updates[].rationale             | key         | rationale    | f350895acb59 | 1 1
state_updates.evidence_need_updates[].rationale             | title       | rationale    | f350895acb59 | 1 1
state_updates.evidence_need_updates[].state                 | description | reason       | 13a9c6522149 | 1 1
state_updates.evidence_need_updates[].superseded_reason     | key         | reason       | 05c55fa70218 | 1 1
state_updates.evidence_need_updates[].superseded_reason     | title       | reason       | 05c55fa70218 | 1 1
state_updates.evidence_to_add[]                             | description | deliberat    | c8328800d4a9 | 1 1
state_updates.evidence_to_add[].category                    | description | deliberat    | a226544e1e9f | 1 1
state_updates.hypotheses_to_add[]                           | required[]  | rationale    | f350895acb59 | 1 1
state_updates.hypotheses_to_add[].rationale                 | key         | rationale    | f350895acb59 | 1 1
state_updates.hypotheses_to_add[].rationale                 | title       | rationale    | f350895acb59 | 1 1
state_updates.hypotheses_to_update[]                        | description | reason       | 4c53faaa6606 | 2 2
state_updates.hypotheses_to_update[]                        | required[]  | reason       | 1e7baccbb097 | 0 1
state_updates.hypotheses_to_update[].refutation_reason      | description | reason       | 2cf222ce0a69 | 2 2
state_updates.hypotheses_to_update[].refutation_reason      | key         | reason       | ee73da7edee0 | 1 1
state_updates.hypotheses_to_update[].refutation_reason      | title       | reason       | ee73da7edee0 | 1 1
state_updates.hypothesis_evidence_links[]                   | required[]  | reason       | 0c4d01e81bb3 | 1 1
state_updates.hypothesis_evidence_links[].reasoning         | key         | reason       | 0c4d01e81bb3 | 1 1
state_updates.hypothesis_evidence_links[].reasoning         | title       | reason       | 0c4d01e81bb3 | 1 1
state_updates.node_evidence_links[]                         | description | step-by-step | 862ce8401aa4 | 1 1
state_updates.node_evidence_links[]                         | required[]  | reason       | 0c4d01e81bb3 | 1 1
state_updates.node_evidence_links[].reasoning               | key         | reason       | 0c4d01e81bb3 | 1 1
state_updates.node_evidence_links[].reasoning               | title       | reason       | 0c4d01e81bb3 | 1 1
state_updates.proposed_transition                           | description | reason       | 9e2de6d7389b | 1 1
state_updates.verification_updates                          | required[]  | rationale    | 620a5898364c | 0 1
state_updates.verification_updates.rca_infeasible_rationale | key         | rationale    | b28c2f0137cc | 1 1
state_updates.verification_updates.rca_infeasible_rationale | title       | rationale    | b28c2f0137cc | 1 1
suggested_follow_ups[].body                                 | description | reason       | b9369e54eb23 | 1 1
"""


def _parse_allowlist(
    table: str,
) -> dict[tuple[str, str, str, str, str], tuple[int, int]]:
    """``# <schema> (...)`` opens a schema's rows; a row is
    ``path | kind | matched text | digest | plain strict``."""
    parsed: dict[tuple[str, str, str, str, str], tuple[int, int]] = {}
    schema = ""
    for line in table.strip().splitlines():
        if line.startswith("# "):
            schema = line[2:].split(" ", 1)[0]
            continue
        path, kind, text, digest, counts = (cell.strip() for cell in line.split("|"))
        plain, strict = (int(n) for n in counts.split())
        key = (schema, path, kind, text, digest)
        assert key not in parsed, key
        parsed[key] = (plain, strict)
    return parsed


ALLOWLIST = _parse_allowlist(_ALLOWLIST_TABLE)

_MEASURED = {
    "InquiryResponse",
    "TerminalResponse",
    "InvestigationResponse_Diagnosis",
}


def test_discovery_covers_the_six_known_schemas():
    names = {cls.__name__ for cls in _discovered()}
    assert KNOWN_SCHEMAS <= names, sorted(KNOWN_SCHEMAS - names)


def test_the_walk_reaches_every_part_of_the_tool():
    """Guard the guard: text behind a ``$ref`` would be invisible to the walk,
    and a walk that skipped the subtree, the tool description or a key kind
    would pass vacuously."""
    tool = _real_tools()[("InvestigationResponse_Diagnosis", 0)]
    assert "$ref" not in repr(tool) and "$defs" not in repr(tool)
    seen = {(kind, text) for _, kind, text in _walk_tool(tool)}
    assert ("key", "evidence_trail") in seen
    assert ("key", "observation") in seen  # array items inside anyOf
    assert ("title", "EvidenceConclusion") in seen
    assert ("function.name", "InvestigationResponse_Diagnosis") in seen
    assert any(kind == "function.description" for kind, _ in seen)
    assert any(kind == "enum[]" for kind, _ in seen)
    strict = _real_tools()[("InvestigationResponse_Diagnosis", 1)]
    assert any(kind == "required[]" for _, kind, _ in _walk_tool(strict))


def test_every_allowlist_entry_names_a_discovered_schema():
    names = {cls.__name__ for cls in _discovered()}
    assert {key[0] for key in ALLOWLIST} <= names


def test_vocabulary_census_matches_the_allowlist():
    drift = _drift(_table(_tools()))
    assert not drift, (
        "The structured response schemas' reasoning vocabulary moved. A new or "
        "changed hit must be measured live on claude-opus-5, claude-opus-5-5 "
        "and claude-fable-5-1 (tool_use, not refusal) before it is "
        "allowlisted, or reworded; a stale entry is removed.\n" + "\n".join(drift)
    )


def test_unmeasured_schemas_carry_only_measured_text():
    """The basis of the Mitigation / Treatment / General entries: each hit is
    the identical text at the same path as a hit in a schema sent live."""
    texts: dict[str, dict[tuple[str, str], set[str]]] = {}
    for (name, _), tool in _real_tools().items():
        for path, kind, text in _walk_tool(tool):
            if VOCABULARY.search(_probe(kind, text)):
                texts.setdefault(name, {}).setdefault((path, kind), set()).add(text)
    measured = texts["InvestigationResponse_Diagnosis"]
    for name, found in texts.items():
        if name in _MEASURED:
            continue
        for where, strings in found.items():
            assert strings <= measured.get(where, set()), (name, where, strings)


# ---------------------------------------------------------------------------
# Positive controls: one per shape the first pin missed (defeat pass, round 20)
# ---------------------------------------------------------------------------


def _props(tool: dict[str, Any]) -> dict[str, Any]:
    return tool["function"]["parameters"]["properties"]


def _branch(prop: dict[str, Any], marker: str) -> dict[str, Any]:
    return next(m for m in prop["anyOf"] if marker in m)


def _trail(tool):
    return _branch(_props(tool)["evidence_trail"], "properties")


def _conclusion_items(tool):
    return _branch(_trail(tool)["properties"]["conclusions"], "items")["items"]


def _diagnosis(tools):
    return tools[("InvestigationResponse_Diagnosis", 0)]


def _set_trail_description(text):
    return lambda tools: _trail(_diagnosis(tools)).__setitem__("description", text)


def _inject_sibling_description(tools):
    _props(_diagnosis(tools))["agent_response"][
        "description"
    ] = "Write out your reasoning in full."


def _inject_tool_description(tools):
    _diagnosis(tools)["function"]["description"] = "Think it through first."


def _inject_top_level_rationale(tools):
    _props(_diagnosis(tools))["rationale"] = {"type": "string"}


def _inject_nested_thinking(tools):
    _conclusion_items(_diagnosis(tools))["properties"]["thinking"] = {"type": "string"}


def _inject_schema_extra(tools):
    _props(_diagnosis(tools))["agent_response"]["x-guidance"] = "explain your answer"


def _inject_nonbreaking_hyphens(tools):
    _trail(_diagnosis(tools))["properties"]["conclusions"][
        "description"
    ] = "Work step‑by‑step."


#: The delta review's rewrite of a measured-safe string: the ``reason`` count is
#: unchanged, and a count-only census passed it.
_MEASURED_BODY = "Reasoning text shown on card (why the user should take this action)"
_REWRITTEN_BODY = "Write out your full reasoning for this action, in detail"


def _follow_up_body(tool):
    items = _branch(_props(tool)["suggested_follow_ups"], "items")["items"]
    return items["properties"]["body"]


def _rewrite_measured_body(tools):
    """The edit as a source change would make it: under both converters."""
    for converter in range(len(CONVERTERS)):
        body = _follow_up_body(tools[("InvestigationResponse_Diagnosis", converter)])
        assert body["description"] == _MEASURED_BODY
        body["description"] = _REWRITTEN_BODY


def _inject_inquiry_internal_reasoning(tools):
    _props(tools[("InquiryResponse", 0)])["internal_reasoning"] = {"type": "string"}


_CONTROLS = {
    "synonym: chain of thought": _set_trail_description("Your chain of thought."),
    "synonym: thought process": _set_trail_description("Your thought process."),
    "synonym: explain your thinking": _set_trail_description("Explain your thinking."),
    "synonym: show your work": _set_trail_description("Show your work."),
    "sibling description": _inject_sibling_description,
    "tool description": _inject_tool_description,
    "top-level key rationale": _inject_top_level_rationale,
    "nested key thinking": _inject_nested_thinking,
    "json_schema_extra string": _inject_schema_extra,
    "U+2011 step-by-step": _inject_nonbreaking_hyphens,
    "InquiryResponse gains internal_reasoning": _inject_inquiry_internal_reasoning,
    "rewrite of a measured-safe string, count kept": _rewrite_measured_body,
}


@pytest.mark.parametrize("inject", _CONTROLS.values(), ids=_CONTROLS.keys())
def test_the_census_catches_each_missed_shape(inject):
    """Measured against the real census, not an assumed-clean one, so each
    control stays about its own injection whatever the tree holds."""
    tools = _tools()
    before = set(_drift(_table(tools)))
    inject(tools)
    assert set(_drift(_table(tools))) - before


def test_a_count_preserving_rewrite_reads_as_new_hit_and_stale_entry():
    """The rewrite keeps the token count, so only the digest can see it."""
    tokens = [m.group(0) for m in VOCABULARY.finditer(_normalise(_MEASURED_BODY))]
    assert tokens == [
        m.group(0) for m in VOCABULARY.finditer(_normalise(_REWRITTEN_BODY))
    ]
    tools = _tools()
    before = set(_drift(_table(tools)))
    _rewrite_measured_body(tools)
    added = set(_drift(_table(tools))) - before
    assert any(line.startswith("new hit") for line in added), added
    assert any(line.startswith("stale entry") for line in added), added


def test_normalisation_is_what_catches_the_nonbreaking_hyphen():
    raw = "work step‑by‑step."
    assert not VOCABULARY.search(raw)
    assert VOCABULARY.search(_normalise(raw))


def test_a_stale_or_miscounted_entry_is_reported():
    table = _table(_tools())
    key = next(iter(sorted(ALLOWLIST)))
    miscounted = dict(table)
    plain, strict = miscounted[key]
    miscounted[key] = (plain + 1, strict)
    assert any(line.startswith("count changed") for line in _drift(miscounted))
    stale = {k: v for k, v in table.items() if k != key}
    assert any(line.startswith("stale entry") for line in _drift(stale))


# ---------------------------------------------------------------------------
# The package source carries no old name
# ---------------------------------------------------------------------------


def _old_name_hits(sources: dict[str, str]) -> list[str]:
    return [
        f"{path}: {name} x{text.count(name)}"
        for path, text in sorted(sources.items())
        for name in OLD_NAMES
        if name in text
    ]


def _package_sources() -> dict[str, str]:
    root = Path(faultmaven.__file__).parent
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
    }


def test_no_old_name_anywhere_in_the_package_source():
    """A feedback message or prompt block that still names the old field asks
    the model for something the schema no longer has, and a reverted string
    in a model-facing block reintroduces the wording."""
    sources = _package_sources()
    # Where it must have looked: the schema, the prompt blocks, the engine.
    assert len(sources) > 300
    for required in (
        "core/investigation/schemas.py",
        "core/investigation/prompts/templates/blocks.py",
        "core/investigation/milestone_engine/milestone_inference.py",
        "core/investigation/milestone_engine/response_application.py",
    ):
        assert required in sources, required
    assert _old_name_hits(sources) == []


def test_the_source_scan_finds_a_planted_old_name():
    planted = {"x.py": 'FEEDBACK = "REASONING VALIDATION: provide internal_reasoning"'}
    assert _old_name_hits(planted) == [
        "x.py: internal_reasoning x1",
        "x.py: REASONING VALIDATION x1",
    ]


# ---------------------------------------------------------------------------
# Prompt template constants: the old names and the bisected phrases only
# ---------------------------------------------------------------------------

#: Blocks the scan must have read, so a rename of the discovery cannot empty it.
_REQUIRED_CONSTANTS = {
    "blocks._DIAGNOSTIC_REASONING_BLOCK",
    "blocks.KNOWLEDGE_QUERY_INSTRUCTIONS",
    "blocks.AGENT_META_INSTRUCTIONS",
    "diagnosis._RCA_DIAGNOSIS_BLOCK",
    "treatment.MITIGATION_INSTRUCTIONS",
    "treatment.TREATMENT_INSTRUCTIONS",
    "investigation.INVESTIGATION_BASE",
    "investigation.SCHEMA_INSTRUCTIONS",
    "inquiry.INQUIRY_TEMPLATE",
    "terminal.TERMINAL_TEMPLATE",
}


def _template_modules() -> list[types.ModuleType]:
    return [
        importlib.import_module(f"{templates_pkg.__name__}.{info.name}")
        for info in pkgutil.iter_modules(templates_pkg.__path__)
    ]


def _module_constants(modules: list[types.ModuleType]) -> dict[str, str]:
    return {
        f"{module.__name__.rsplit('.', 1)[-1]}.{name}": value
        for module in modules
        for name, value in vars(module).items()
        if isinstance(value, str) and not name.startswith("__")
    }


def _template_hits(constants: dict[str, str]) -> list[str]:
    hits = []
    for where, text in sorted(constants.items()):
        hits += [f"{where}: {name}" for name in OLD_NAMES if name in text]
        normalised = _normalise(text)
        hits += [
            f"{where}: /{p.pattern}/" for p in BISECTED_PHRASES if p.search(normalised)
        ]
    return hits


def test_template_constants_carry_no_old_name_or_bisected_phrase():
    modules = _template_modules()
    files = {p.stem for p in Path(templates_pkg.__path__[0]).glob("*.py")}
    assert {m.__name__.rsplit(".", 1)[-1] for m in modules} == files - {"__init__"}
    constants = _module_constants(modules)
    assert _REQUIRED_CONSTANTS <= set(constants), sorted(
        _REQUIRED_CONSTANTS - set(constants)
    )
    assert _template_hits(constants) == []


def test_the_template_scan_finds_a_planted_phrase():
    synthetic = types.ModuleType("faultmaven.synthetic_templates")
    synthetic._BLOCK = "Write your internal\nreasoning first."
    constants = _module_constants([synthetic])
    assert _template_hits(constants) == [
        "synthetic_templates._BLOCK: /internal[\\s_-]*reasoning/"
    ]


def test_the_prompt_asks_for_the_evidence_trail_by_name():
    assert "CRITICAL: EVIDENCE-FIRST REQUIREMENT" in INVESTIGATION_BASE
    assert "you MUST provide evidence_trail BEFORE state_updates" in INVESTIGATION_BASE
    assert "- **evidence_trail**:" in SCHEMA_INSTRUCTIONS
