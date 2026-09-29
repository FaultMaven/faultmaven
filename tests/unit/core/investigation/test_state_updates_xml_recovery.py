"""fm#1753: a ``state_updates`` that arrives as the model's XML parameter form.

``claude-opus-5`` returns the schema tool's nested ``state_updates`` as a string
of ``<parameter name="k">v`` tags. The engine used to coerce it to ``{}`` (the
body then validated ``clean``) and dropped the turn's state. It is now parsed
into the object it encodes on all three parse paths, and every coercion is
counted on ``faultmaven_schema_state_updates_repairs_total``.
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
    _recover_xml_parameters,
)
from faultmaven.core.investigation.reliability_metrics import STATE_UPDATES_REPAIRS
from faultmaven.core.investigation.schemas import TerminalResponse
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall

pytestmark = pytest.mark.unit

# args.state_updates of a real claude-opus-5 TerminalResponse capture,
# 2026-09-29. One tag, no closing </parameter>: the value runs to the end.
REAL_SAMPLE = '\n<parameter name="final_summary_update">RESOLVED — Disk exhaustion on api-3 /var volume.\n\nROOT CAUSE: An unrotated debug log on api-3 grew unbounded and filled the /var volume to 100%, causing write failures and the observed production error spike. The log path was not covered by logrotate, so the file was never truncated or aged out.\n\nRESOLUTION: The debug log was rotated and the oversized file deleted to reclaim space. Logrotate was then enabled for that log path to prevent unbounded growth going forward.\n\nVERIFICATION: /var utilization on api-3 returned to 41%. Error stream clean for 2 hours post-fix with no recurrence.\n\nOPEN FOLLOW-UPS (non-blocking):\n- Confirm the logrotate config persists across deploys / config-management runs (drift risk).\n- Evaluate whether debug-level logging should be enabled in production for this service at all.\n- Audit sibling API hosts for the same uncovered log path — if debug logging was set fleet-wide, they share the same failure trajectory.\n- Add a /var disk-utilization alert (~80% threshold) for earlier warning.'
SAMPLE_TEXT = REAL_SAMPLE.split('">', 1)[1].strip()

SCHEMA = "TerminalResponse"


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            '<parameter name="final_summary_update">text</parameter>',
            {"final_summary_update": "text"},
        ),
        (
            '<parameter name="a">x</parameter>\n<parameter name="b">["l1","l2"]</parameter>',
            {"a": "x", "b": ["l1", "l2"]},
        ),
        (
            '<parameter name="milestones"><parameter name="symptom_verified">true</parameter></parameter>',
            {"milestones": {"symptom_verified": True}},
        ),
        (
            '<parameter name="milestones"><parameter name="symptom_verified">true',
            {"milestones": {"symptom_verified": True}},
        ),
        (
            '<parameter name="a">x</parameter><parameter name="b">y',
            {"a": "x", "b": "y"},
        ),
        ('\n\n  <parameter name="a">x</parameter>\n ', {"a": "x"}),
        ('<parameter name="a">x</parameter>\n</invoke>', {"a": "x"}),
        ('<parameter name="a"></parameter>', {"a": ""}),
        (
            '<parameter name="evidence_to_add">[{"summary":"s"}]</parameter>',
            {"evidence_to_add": [{"summary": "s"}]},
        ),
        (
            '<parameter name="a">x < y and </param> text</parameter>',
            {"a": "x < y and </param> text"},
        ),
    ],
)
def test_recovers_xml_parameters(text, expected):
    assert _recover_xml_parameters(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Root cause: disk exhaustion",
        '{"milestones": {"a": tr',
        'Here you go <parameter name="a">x',
        '<parameter name="a">x<parameter name="b">y',  # mixed
        '<parameter name="a">x</parameter><parameter name="a">y</parameter>',  # duplicate
        '<parameter name="a">x</parameter></parameter>',  # unbalanced
        "<parameter name='a'>x</parameter>",
        '<parameter name="a">see <parameter name="b"> in docs</parameter>',  # mixed
        '<parameter name="a">x</parameter> trailing words',
        "</parameter>",
        "",
        "<parameter>x</parameter>",
        '<parameters name="a">x</parameters>',
    ],
)
def test_declines_everything_else(text):
    assert _recover_xml_parameters(text) is None


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


def test_tool_call_other_string_is_dropped_counted_and_logged_without_content(
    counters, caplog
):
    secret = "not xml at all"
    with caplog.at_level(logging.WARNING, logger=so.logger.name):
        parsed = _parse_schema_tool_call(_tool_call(secret), TerminalResponse)
    assert parsed.state_updates.final_summary_update is None
    assert counters.repairs() == [_repair("string_dropped")]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert secret not in warnings[0].getMessage()
    assert "TerminalResponse" in warnings[0].getMessage()
    assert str(len(secret)) in warnings[0].getMessage()


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


def test_text_path_recovers_real_sample(counters):
    text = json.dumps({"agent_response": "Resolved.", "state_updates": REAL_SAMPLE})
    parsed = _parse_text_as_schema(text, TerminalResponse)
    assert parsed.state_updates.final_summary_update == SAMPLE_TEXT
    assert counters.repairs() == [_repair("xml_recovered")]


@pytest.mark.asyncio
async def test_single_shot_path_recovers_real_sample(counters):
    from faultmaven.infrastructure.llm.structured_output_capability import (
        StructuredOutputCapability,
        StructuredOutputMode,
        StructuredOutputStrategy,
    )

    content = json.dumps({"agent_response": "Resolved.", "state_updates": REAL_SAMPLE})
    provider = MagicMock()
    provider.generate = AsyncMock(
        return_value=LLMResponse(
            content=content,
            confidence=0.9,
            provider="test",
            model="test-model",
            tokens_used=10,
            response_time_ms=5,
        )
    )
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
    assert parsed.state_updates.final_summary_update == SAMPLE_TEXT
    assert counters.repairs() == [_repair("xml_recovered")]


def test_non_dict_body_is_returned_unchanged():
    assert _normalize_state_updates([1], TerminalResponse) == [1]


def test_every_state_updates_coercion_goes_through_the_helper():
    """State N = 3: the scan that found the duplicated ``isinstance(_su, str)``
    blocks. None may come back; each parse path calls the helper."""
    from faultmaven.core.investigation.milestone_engine import generation

    for module in (so, generation):
        assert "isinstance(_su, str)" not in inspect.getsource(module)
    for fn in (
        _parse_schema_tool_call,
        _parse_text_as_schema,
        generation.StructuredOutputGenerator._generate_structured_output_inner,
    ):
        assert "_normalize_state_updates(" in inspect.getsource(fn)


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
