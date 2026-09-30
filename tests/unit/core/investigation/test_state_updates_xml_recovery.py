"""fm#1753: a ``state_updates`` that arrives as the model's leaked parameter form.

``claude-opus-5`` can return the schema tool's ``state_updates`` as the string
``<parameter name="K">VALUE``: one open tag, no closer, the value running to the
end. The provider ends the argument at the model's first inner
``</parameter>``, so any later state field arrives as a top-level argument. The
engine used to coerce the string to ``{}`` (the body then validated ``clean``)
and dropped the turn's state. It is now recovered on all three parse paths,
with leaked siblings lifted back in, unless the provider reported the response
cut at ``max_tokens``; every coercion is counted on
``faultmaven_schema_state_updates_repairs_total``.
"""

import ast
import inspect
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import structured_output as so
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.structured_output import (
    _normalize_state_updates,
    _parse_schema_tool_call,
    _parse_text_as_schema,
    _recover_leaked_parameter,
)
from faultmaven.core.investigation.reliability_metrics import STATE_UPDATES_REPAIRS
from faultmaven.core.investigation.schemas import (
    InvestigationResponse_Diagnosis,
    TerminalResponse,
)
from faultmaven.infrastructure.llm.providers import StopReason
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall

pytestmark = pytest.mark.unit

# args.state_updates of a real claude-opus-5 TerminalResponse capture,
# 2026-09-29. One tag, no closing </parameter>: the value runs to the end.
REAL_SAMPLE = '\n<parameter name="final_summary_update">RESOLVED — Disk exhaustion on api-3 /var volume.\n\nROOT CAUSE: An unrotated debug log on api-3 grew unbounded and filled the /var volume to 100%, causing write failures and the observed production error spike. The log path was not covered by logrotate, so the file was never truncated or aged out.\n\nRESOLUTION: The debug log was rotated and the oversized file deleted to reclaim space. Logrotate was then enabled for that log path to prevent unbounded growth going forward.\n\nVERIFICATION: /var utilization on api-3 returned to 41%. Error stream clean for 2 hours post-fix with no recurrence.\n\nOPEN FOLLOW-UPS (non-blocking):\n- Confirm the logrotate config persists across deploys / config-management runs (drift risk).\n- Evaluate whether debug-level logging should be enabled in production for this service at all.\n- Audit sibling API hosts for the same uncovered log path — if debug logging was set fleet-wide, they share the same failure trajectory.\n- Add a /var disk-utilization alert (~80% threshold) for earlier warning.'
SAMPLE_TEXT = REAL_SAMPLE.split('">', 1)[1].strip()

# args of a second real claude-opus-5 TerminalResponse capture, 2026-09-29:
# documentation_links, a state field, leaked to the TOP level of the arguments.
LEAKED_SIBLING_ARGS = {
    "agent_response": "Case closed. Here's the wrap-up for the record.\n\n**What happened:** `api-3` began returning errors because the `/var` volume hit 100% utilization. The culprit was a debug log that had been left enabled and was never registered with logrotate, so it grew unbounded until writes to `/var` failed.\n\n**What fixed it:** The oversized log was rotated and the stale copy deleted, reclaiming the volume down to **41% utilization**. A logrotate policy was then put in place for that log so it can't grow unbounded again.\n\n**Verification:** Disk sitting steady at 41% and the error stream has been clean for **2 hours** post-fix — long enough to confirm this wasn't a transient recovery.\n\n**Worth considering before you move on:** the fix addresses `api-3` specifically. If the same debug logging was enabled across other API hosts (or was turned on for a past investigation and never turned off fleet-wide), those nodes are on the same clock and will fail the same way. A quick check of `/var` usage and logrotate coverage on the sibling nodes, plus a disk-utilization alert threshold around 80%, would turn this from a fix into a prevention.",
    "state_updates": '\n<parameter name="final_summary_update">RESOLVED — Disk exhaustion on api-3 /var volume.\n\nROOT CAUSE: An unrotated debug log on api-3 grew unbounded and filled the /var volume to capacity, causing write failures and the resulting production error stream. The log had no logrotate policy covering it.\n\nRESOLUTION: Rotated the oversized debug log and deleted the stale file, reclaiming space. Enabled logrotate for the log to prevent unbounded growth going forward.\n\nVERIFICATION: /var volume utilization returned to 41% and held steady. Error stream clean for 2 hours following the fix, confirming sustained recovery rather than a transient dip.\n\nFOLLOW-UP (not performed, recommended): Audit sibling API hosts for the same debug logging and missing logrotate coverage — they may be on the same failure trajectory. Add a disk-utilization alert (~80% threshold) on /var to catch recurrence before exhaustion.',
    "documentation_links": [],
}
LEAKED_SIBLING_TEXT = LEAKED_SIBLING_ARGS["state_updates"].split('">', 1)[1].strip()

SCHEMA = "TerminalResponse"
DROP_EVENT = "structured_output_state_updates_dropped"

# A non-text field's value nested past the JSON decoder's recursion limit.
TOO_DEEP = '<parameter name="documentation_links">' + "[" * 1200


def _body(state_updates, **extra) -> dict:
    return {"agent_response": "x", "state_updates": state_updates, **extra}


@pytest.mark.parametrize(
    "schema, state_updates, expected",
    [
        (
            TerminalResponse,
            '\n<parameter name="final_summary_update">[1] disk full on api-3',
            {"final_summary_update": "[1] disk full on api-3"},
        ),
        (
            TerminalResponse,
            '<parameter name="final_summary_update">42',
            {"final_summary_update": "42"},
        ),
        (
            TerminalResponse,
            '<parameter name="final_summary_update">true',
            {"final_summary_update": "true"},
        ),
        (
            TerminalResponse,
            '<parameter name="final_summary_update">null',
            {"final_summary_update": "null"},
        ),
        (
            TerminalResponse,
            '\n<parameter name="final_summary_update">Fixed in server.xml:\n'
            '<Connector port="8080"/>\n</Service>\n</Server>',
            {
                "final_summary_update": 'Fixed in server.xml:\n<Connector port="8080"/>'
                "\n</Service>\n</Server>"
            },
        ),
        (
            TerminalResponse,
            '<parameter name="documentation_links">["https://x"]',
            {"documentation_links": ["https://x"]},
        ),
        (
            InvestigationResponse_Diagnosis,
            '<parameter name="milestones">{"symptom_verified": true}',
            {"milestones": {"symptom_verified": True}},
        ),
    ],
    ids=[
        "text-keeps-leading-json",
        "text-keeps-number",
        "text-keeps-true",
        "text-keeps-null",
        "text-keeps-trailing-closers",
        "list-field-decodes-json",
        "object-field-decodes-json",
    ],
)
def test_recovers_the_leaked_parameter(schema, state_updates, expected):
    assert _recover_leaked_parameter(_body(state_updates), schema) == expected


@pytest.mark.parametrize(
    "schema, state_updates",
    [
        (TerminalResponse, '<parameter name="final_summary_update">x</parameter>'),
        (TerminalResponse, '<parameter name="documentation_links">'),
        (TerminalResponse, '<parameter name="documentation_links">see wiki'),
        (TerminalResponse, '<parameter name="agent_response">x'),
        (
            InvestigationResponse_Diagnosis,
            '<parameter name="milestones"><parameter name="symptom_verified">true',
        ),
        (
            TerminalResponse,
            '<parameter name="final_summary_update">A'
            '<parameter name="documentation_links">[]',
        ),
        (TerminalResponse, 'Here: <parameter name="final_summary_update">x'),
        (TerminalResponse, "Root cause: disk"),
        (TerminalResponse, '{"final_summary_update": "x'),
        (TerminalResponse, "<parameter name='final_summary_update'>x"),
        (TerminalResponse, TOO_DEEP),
    ],
    ids=[
        "a-closer-never-occurs",
        "list-field-empty",
        "list-field-not-json",
        "not-a-state-field",
        "nested-tag",
        "nested-tag-on-a-text-field",
        "text-before-the-tag",
        "prose",
        "truncated-json",
        "single-quoted-name",
        "nested-too-deep",
    ],
)
def test_declines_everything_else(schema, state_updates):
    body = _body(state_updates, documentation_links=["https://y"])
    before = json.loads(json.dumps(body))
    assert _recover_leaked_parameter(body, schema) is None
    assert body == before  # a declined recovery lifts nothing


def test_lifts_a_leaked_state_sibling_out_of_the_top_level():
    body = _body(
        '\n<parameter name="final_summary_update">S', documentation_links=["https://y"]
    )
    assert _recover_leaked_parameter(body, TerminalResponse) == {
        "final_summary_update": "S",
        "documentation_links": ["https://y"],
    }
    assert "documentation_links" not in body


def test_never_lifts_a_top_level_schema_field():
    body = _body('\n<parameter name="final_summary_update">S', suggested_follow_ups=[])
    assert _recover_leaked_parameter(body, TerminalResponse) == {
        "final_summary_update": "S"
    }
    assert body["suggested_follow_ups"] == []


class _Counters:
    """The metrics shim is a no-op unless prometheus is enabled, so read the
    increments off patched counters, as the neighbouring tests do."""

    def __init__(self, repairs, validations):
        self._repairs, self._validations = repairs, validations

    def repairs(self):
        return [c.kwargs for c in self._repairs.labels.call_args_list]

    def outcomes(self):
        return [c.kwargs for c in self._validations.labels.call_args_list]


@pytest.fixture
def counters(monkeypatch):
    repairs, validations = MagicMock(), MagicMock()
    monkeypatch.setattr(so, "schema_state_updates_repairs_total", repairs)
    monkeypatch.setattr(so, "schema_validation_total", validations)
    return _Counters(repairs, validations)


def _repair(name: str) -> dict:
    return {"schema": SCHEMA, "repair": name}


def _drop_warnings(caplog) -> list:
    return [r for r in caplog.records if r.getMessage().startswith(DROP_EVENT)]


def _tool_call(state_updates=..., **extra) -> ToolCall:
    args = {"agent_response": "Resolved.", **extra}
    if state_updates is not ...:
        args["state_updates"] = state_updates
    return ToolCall(
        id="c1",
        type="function",
        function={"name": "TerminalResponse", "arguments": json.dumps(args)},
    )


def test_tool_call_recovers_real_sample(counters):
    parsed = _parse_schema_tool_call(_tool_call(REAL_SAMPLE), TerminalResponse)
    assert parsed.state_updates.final_summary_update == SAMPLE_TEXT
    assert counters.repairs() == [_repair("xml_recovered")]
    assert counters.outcomes() == [{"schema": SCHEMA, "outcome": "clean"}]


def test_tool_call_lifts_the_real_leaked_sibling(counters, caplog):
    call = ToolCall(
        id="c1",
        type="function",
        function={
            "name": "TerminalResponse",
            "arguments": json.dumps(LEAKED_SIBLING_ARGS),
        },
    )
    with caplog.at_level(logging.WARNING, logger=so.logger.name):
        parsed = _parse_schema_tool_call(call, TerminalResponse)
    assert parsed.state_updates.final_summary_update == LEAKED_SIBLING_TEXT
    # The sample's leaked value is [], the field's default too: that it was SET
    # is what shows it landed in state_updates rather than being dropped.
    assert parsed.state_updates.documentation_links == []
    assert "documentation_links" in parsed.state_updates.model_fields_set
    assert not [
        r
        for r in caplog.records
        if r.getMessage() == "structured_output_dropped_field"
        and getattr(r, "field", None) == "documentation_links"
    ]
    assert counters.repairs() == [_repair("xml_recovered")]
    assert counters.outcomes() == [{"schema": SCHEMA, "outcome": "clean"}]


def test_tool_call_never_recovers_a_cut_response(counters, caplog):
    with caplog.at_level(logging.WARNING, logger=so.logger.name):
        parsed = _parse_schema_tool_call(
            _tool_call(REAL_SAMPLE), TerminalResponse, cut=True
        )
    assert parsed.state_updates.final_summary_update is None
    assert counters.repairs() == [_repair("string_dropped")]
    (warning,) = _drop_warnings(caplog)
    assert warning.cut is True
    assert SAMPLE_TEXT[:40] not in warning.getMessage()


def test_tool_call_other_string_is_dropped_counted_and_logged_without_content(
    counters, caplog
):
    secret = "not xml at all"
    with caplog.at_level(logging.WARNING, logger=so.logger.name):
        parsed = _parse_schema_tool_call(_tool_call(secret), TerminalResponse)
    assert parsed.state_updates.final_summary_update is None
    assert counters.repairs() == [_repair("string_dropped")]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == _drop_warnings(caplog) and len(warnings) == 1
    (warning,) = warnings
    assert secret not in warning.getMessage()
    assert "TerminalResponse" in warning.getMessage()
    assert (warning.schema, warning.length, warning.cut) == (
        "TerminalResponse",
        len(secret),
        False,
    )


def test_tool_call_too_deep_value_is_dropped_not_raised(counters):
    parsed = _parse_schema_tool_call(_tool_call(TOO_DEEP), TerminalResponse)
    assert parsed.state_updates.final_summary_update is None
    assert parsed.state_updates.documentation_links == []
    assert parsed.state_updates.model_fields_set == set()
    assert counters.repairs() == [_repair("string_dropped")]


def test_tool_call_absent_state_updates_is_defaulted_and_counted(counters):
    parsed = _parse_schema_tool_call(_tool_call(), TerminalResponse)
    assert parsed.state_updates.final_summary_update is None
    assert counters.repairs() == [_repair("absent_defaulted")]


def test_tool_call_dict_state_updates_unchanged_and_uncounted(counters):
    parsed = _parse_schema_tool_call(
        _tool_call({"final_summary_update": "done"}), TerminalResponse
    )
    assert parsed.state_updates.final_summary_update == "done"
    assert counters.repairs() == []


@pytest.mark.parametrize(
    "state_updates, expected, repair",
    [(REAL_SAMPLE, SAMPLE_TEXT, "xml_recovered"), ("not xml", None, "string_dropped")],
    ids=["recovered", "dropped"],
)
def test_text_path(counters, state_updates, expected, repair):
    text = json.dumps({"agent_response": "Resolved.", "state_updates": state_updates})
    parsed = _parse_text_as_schema(text, TerminalResponse)
    assert parsed.state_updates.final_summary_update == expected
    assert counters.repairs() == [_repair(repair)]


def _llm(content: str = "", *, stop_reason: StopReason, tool_calls=None):
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=10,
        response_time_ms=5,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state_updates, stop_reason, expected, repair",
    [
        (REAL_SAMPLE, StopReason.STOP, SAMPLE_TEXT, "xml_recovered"),
        ("not xml at all", StopReason.STOP, None, "string_dropped"),
        (REAL_SAMPLE, StopReason.MAX_TOKENS, None, "string_dropped"),
    ],
    ids=["recovered", "dropped", "cut-never-recovered"],
)
async def test_single_shot_path(counters, state_updates, stop_reason, expected, repair):
    from faultmaven.infrastructure.llm.structured_output_capability import (
        StructuredOutputCapability,
        StructuredOutputMode,
        StructuredOutputStrategy,
    )

    content = json.dumps(
        {"agent_response": "Resolved.", "state_updates": state_updates}
    )
    provider = MagicMock()
    provider.generate = AsyncMock(return_value=_llm(content, stop_reason=stop_reason))
    provider.get_structured_output_strategy = MagicMock(
        return_value=StructuredOutputStrategy(
            capability=StructuredOutputCapability.BEST_EFFORT,
            mode=StructuredOutputMode.JSON_OBJECT,
            include_schema_in_prompt=True,
            response_format={"type": "json_object"},
        )
    )
    repo = MagicMock()
    repo.save = AsyncMock()
    engine = MilestoneEngine(
        llm_provider=provider, repository=repo, investigation_tools=MagicMock()
    )
    parsed = await engine.generator._generate_structured_output_inner(
        "p", TerminalResponse
    )
    assert parsed.state_updates.final_summary_update == expected
    assert counters.repairs() == [_repair(repair)]


async def _tool_loop(*responses: LLMResponse):
    """Drive ``_tool_augmented_generate`` the way test_response_synthesis_1442
    does. A cut response is answered again by the truncation retry, so the
    last response repeats for as long as the loop asks."""
    queue = list(responses)

    async def _generate(**_kwargs):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    provider = AsyncMock()
    provider.generate = AsyncMock(side_effect=_generate)
    repo = MagicMock()
    repo.save = AsyncMock()
    engine = MilestoneEngine(
        llm_provider=provider, repository=repo, investigation_tools=MagicMock()
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_file",
                "description": "Search files",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    return await engine.generator._tool_augmented_generate(
        prompt="p",
        schema_model=TerminalResponse,
        investigation_tools=tools,
        tool_context=MagicMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_reason, expected, repair",
    [
        (StopReason.TOOL_CALLS, SAMPLE_TEXT, "xml_recovered"),
        (StopReason.MAX_TOKENS, None, "string_dropped"),
    ],
    ids=["recovered", "cut-never-recovered"],
)
async def test_tool_loop_schema_call(counters, stop_reason, expected, repair):
    parsed = await _tool_loop(
        _llm(stop_reason=stop_reason, tool_calls=[_tool_call(REAL_SAMPLE)])
    )
    assert parsed.state_updates.final_summary_update == expected
    assert counters.repairs() == [_repair(repair)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_reason, expected, repair",
    [
        (StopReason.STOP, SAMPLE_TEXT, "xml_recovered"),
        (StopReason.MAX_TOKENS, None, "string_dropped"),
    ],
    ids=["recovered", "cut-never-recovered"],
)
async def test_tool_loop_forced_text_parse(counters, stop_reason, expected, repair):
    """The loop's third parse site: after the nudge, the model still answers in
    text, and that text is parsed as the schema."""
    body = json.dumps({"agent_response": "Resolved.", "state_updates": REAL_SAMPLE})
    parsed = await _tool_loop(
        _llm("I will answer in prose.", stop_reason=StopReason.STOP),
        _llm(body, stop_reason=stop_reason),
    )
    assert parsed.state_updates.final_summary_update == expected
    assert counters.repairs() == [_repair(repair)]


@pytest.mark.parametrize(
    "state_updates",
    [[], 0, False, "[]", [{}]],
    ids=["empty-list", "zero", "false", "string-decoded-to-list", "list-of-object"],
)
def test_tool_call_non_object_state_updates_is_coerced_counted_not_raised(
    counters, caplog, state_updates
):
    with caplog.at_level(logging.WARNING, logger=so.logger.name):
        parsed = _parse_schema_tool_call(_tool_call(state_updates), TerminalResponse)
    assert parsed.state_updates.model_fields_set == set()
    assert parsed.state_updates.final_summary_update is None
    assert counters.repairs() == [_repair("non_object_dropped")]
    assert len(_drop_warnings(caplog)) == 1


_LEAK_S = '<parameter name="final_summary_update">S'
_LEAK_LINKS = '<parameter name="documentation_links">["https://x"]'


@pytest.mark.parametrize(
    "state_updates, sibling, expected",
    [
        (
            _LEAK_S,
            {"documentation_links": "https://x"},
            {"final_summary_update": "S"},
        ),
        (
            _LEAK_LINKS,
            {"final_summary_update": "[1] disk full on api-3"},
            {"documentation_links": ["https://x"]},
        ),
        (
            _LEAK_S,
            {"documentation_links": ["https://x"]},
            {"final_summary_update": "S", "documentation_links": ["https://x"]},
        ),
        (
            _LEAK_LINKS,
            {"final_summary_update": "disk full on api-3"},
            {
                "documentation_links": ["https://x"],
                "final_summary_update": "disk full on api-3",
            },
        ),
    ],
    ids=["bad-links-sibling", "bad-summary-sibling", "good-links", "good-summary"],
)
def test_tool_call_lifts_only_siblings_their_field_accepts(
    counters, state_updates, sibling, expected
):
    parsed = _parse_schema_tool_call(
        _tool_call(state_updates, **sibling), TerminalResponse
    )
    assert (
        parsed.state_updates.model_dump(exclude_none=True, exclude_defaults=True)
        == expected
    )
    assert counters.repairs() == [_repair("xml_recovered")]
    assert counters.outcomes() == [{"schema": SCHEMA, "outcome": "clean"}]


def test_non_dict_body_is_returned_unchanged():
    assert _normalize_state_updates([1], TerminalResponse) == [1]


def test_repair_vocabulary_is_pinned():
    tree = ast.parse(inspect.getsource(_normalize_state_updates))
    recorded = {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "repair" for t in node.targets)
        and isinstance(node.value, ast.Constant)
    }
    assert recorded, "found no repair literals to pin"
    assert recorded == set(STATE_UPDATES_REPAIRS)
