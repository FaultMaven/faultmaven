"""#1088 — the tool-result cut must be observable, per tool.

``MilestoneEngine.TOOL_RESULT_MAX_CHARS`` truncates every tool result before it
re-enters the model's context. That clip used to be completely silent: no log
line, no counter, nothing recorded that it had fired. So the clip rate was not
merely unmeasured, it was **unmeasurable** without arithmetic across two
unrelated log lines — which is why #1088 could argue about the ceiling but not
compute it.

These tests pin the observability, not the ceiling. Nothing here asserts that
8000 is right, and nothing here changes what the engine relays: a result under
the cap must still arrive whole, and one over it must still be cut in exactly
the way it was before. What must now also happen is that both events are
counted and the cut is logged with what it dropped.

The Prometheus counters are NoOp shims unless ``ENABLE_METRICS`` is set (they
have no readable value in the unit env), so they are patched with mocks and the
``inc``/``observe`` calls and their ``tool`` label asserted directly — the same
approach as ``test_work_gate_crossing.py``. The log line is asserted through
``caplog``, because that is the surface a standalone simulation run actually
has: it does not set ``ENABLE_METRICS``, so on that run the log IS the
instrument.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation import milestone_engine as me
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.models.interfaces import ToolResult

pytestmark = [pytest.mark.unit]

SEARCH_FILE_TOOL = [
    {"type": "function", "function": {"name": "search_file", "parameters": {}}}
]


class SampleResponse(BaseModel):
    agent_response: str = "test response"
    next_action: str = "continue"


def _tool_call_response(tool_name: str, call_id: str = "call_1") -> LLMResponse:
    return LLMResponse(
        content="",
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=100,
        response_time_ms=500,
        tool_calls=[
            ToolCall(
                id=call_id,
                type="function",
                function={"name": tool_name, "arguments": json.dumps({"query": "x"})},
            )
        ],
    )


def _schema_response() -> LLMResponse:
    return LLMResponse(
        content="",
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=200,
        response_time_ms=600,
        tool_calls=[
            ToolCall(
                id="call_schema",
                type="function",
                function={
                    "name": "SampleResponse",
                    "arguments": json.dumps(
                        {"agent_response": "done", "next_action": "continue"}
                    ),
                },
            )
        ],
    )


def _engine_returning(data: str, called_tool: str = "search_file"):
    """An engine whose single tool call returns ``data``, then the schema."""
    provider = AsyncMock()
    provider.generate = AsyncMock(
        side_effect=[_tool_call_response(called_tool), _schema_response()]
    )

    registry = MagicMock()
    registry.get_all_tools.return_value = []
    registry.execute_tool = AsyncMock(return_value=ToolResult(success=True, data=data))

    repo = MagicMock()
    repo.save = AsyncMock()
    return MilestoneEngine(
        llm_provider=provider,
        repository=repo,
        investigation_tools=registry,
    )


async def _run(engine):
    return await engine._tool_augmented_generate(
        prompt="Search",
        schema_model=SampleResponse,
        investigation_tools=SEARCH_FILE_TOOL,
        tool_context=MagicMock(),
    )


@pytest.mark.asyncio
async def test_truncation_is_logged_with_what_it_dropped(caplog):
    """The cut must name the tool, the original size, the cap and the overflow.

    Without these four fields the line answers "something was cut" and not
    "how much, from where" — and it is the second question the ceiling
    decision turns on.
    """
    overflow = 1500
    engine = _engine_returning("x" * (MilestoneEngine.TOOL_RESULT_MAX_CHARS + overflow))

    with caplog.at_level("WARNING", logger=me.__name__):
        await _run(engine)

    cut = [r for r in caplog.records if r.getMessage() == "tool_result_truncated"]
    assert len(cut) == 1, (
        "the tool-result truncation produced no log record; it is silent, so "
        "nothing on a standalone run can observe that it fired"
    )
    record = cut[0]
    assert record.tool == "search_file"
    assert record.original_chars == MilestoneEngine.TOOL_RESULT_MAX_CHARS + overflow
    assert record.cap_chars == MilestoneEngine.TOOL_RESULT_MAX_CHARS
    assert record.dropped_chars == overflow


@pytest.mark.asyncio
async def test_truncation_counts_against_the_relayed_denominator():
    """A clip rate needs both halves — the fire alone is not a rate.

    ``lifecycle_metrics``' house rule: pair every rule-fire with the outcome it
    should be read against. Here the pair is truncated-over-relayed, so the
    relayed counter and the size observation must fire on the SAME result that
    the truncation counter does.
    """
    engine = _engine_returning("x" * (MilestoneEngine.TOOL_RESULT_MAX_CHARS + 1000))

    with (
        patch.object(me, "tool_result_relayed_total") as relayed,
        patch.object(me, "tool_result_truncated_total") as truncated,
        patch.object(me, "tool_result_chars") as sizes,
    ):
        await _run(engine)

    relayed.labels.assert_called_once_with(tool="search_file")
    relayed.labels.return_value.inc.assert_called_once()
    truncated.labels.assert_called_once_with(tool="search_file")
    truncated.labels.return_value.inc.assert_called_once()
    # Observed PRE-cut: a distribution recorded after the cut would pile every
    # oversized result onto the cap value and hide the overflow entirely.
    sizes.labels.assert_called_once_with(tool="search_file")
    sizes.labels.return_value.observe.assert_called_once_with(
        MilestoneEngine.TOOL_RESULT_MAX_CHARS + 1000
    )


@pytest.mark.asyncio
async def test_a_result_under_the_cap_is_counted_but_not_reported_as_cut(caplog):
    """The denominator counts everything; the numerator counts only cuts.

    A clip rate that counted uncut results as cuts would read 100% forever.
    """
    engine = _engine_returning("x" * (MilestoneEngine.TOOL_RESULT_MAX_CHARS - 100))

    with caplog.at_level("WARNING", logger=me.__name__):
        with (
            patch.object(me, "tool_result_relayed_total") as relayed,
            patch.object(me, "tool_result_truncated_total") as truncated,
        ):
            await _run(engine)

    relayed.labels.assert_called_once_with(tool="search_file")
    relayed.labels.return_value.inc.assert_called_once()
    truncated.labels.assert_not_called()
    assert not [r for r in caplog.records if r.getMessage() == "tool_result_truncated"]


@pytest.mark.asyncio
async def test_an_unoffered_tool_name_cannot_mint_a_new_metric_label():
    """The tool name is model-supplied, so the label vocabulary must be bounded.

    A model that invents tool names would otherwise mint one Prometheus label
    per invention. Only names this call actually offered are real; everything
    else folds into ``unknown``.
    """
    engine = _engine_returning("x" * 100, called_tool="totally_made_up_tool")

    with patch.object(me, "tool_result_relayed_total") as relayed:
        await _run(engine)

    relayed.labels.assert_called_once_with(tool="unknown")


@pytest.mark.asyncio
async def test_instrumentation_does_not_change_what_the_model_receives():
    """Observability only — the relayed string must be byte-identical to before.

    #1088 is explicit that the ceiling is a separate, unmade decision. This
    pins that the instrumentation did not quietly make it.
    """
    engine = _engine_returning("x" * (MilestoneEngine.TOOL_RESULT_MAX_CHARS + 1000))
    await _run(engine)

    second_call = engine.llm_provider.generate.call_args_list[1]
    messages = second_call.kwargs["messages"]
    tool_msg = [m for m in messages if m.get("role") == "tool"][0]
    assert tool_msg["content"] == (
        "x" * MilestoneEngine.TOOL_RESULT_MAX_CHARS + "\n[truncated]"
    )


@pytest.mark.asyncio
async def test_kb_qa_formatter_trim_is_counted_as_a_clip():
    """kb_qa is clipped by the FORMATTER, not by the loop's cap — count it there.

    #1086 gave kb_qa an earlier cut: ``_format_tool_result`` trims the answer so
    the relay instructions survive, which means an oversized kb_qa answer
    normally arrives at the loop already under the cap. Measured only at the
    loop, kb_qa — the tool #1088 is actually about — would report a clip rate
    near zero while still being clipped. That is worse than not measuring it:
    the number looks honest and is wrong.

    The size is recorded on the WRAPPED, PRE-trim string so it is comparable
    with every other tool's, which is measured the same way.
    """
    answer = "A" * 20000
    with (
        patch.object(me, "tool_result_truncated_total") as truncated,
        patch.object(me, "tool_result_chars") as sizes,
    ):
        wrapped = MilestoneEngine._format_tool_result(
            ToolResult(success=True, data=answer), tool_name="kb_qa"
        )

    truncated.labels.assert_called_once_with(tool="kb_qa")
    truncated.labels.return_value.inc.assert_called_once()

    sizes.labels.assert_called_once_with(tool="kb_qa")
    observed = sizes.labels.return_value.observe.call_args[0][0]
    expected = len(me.KB_QA_RELAY_PREFIX) + len(answer) + len(me.KB_QA_RELAY_SUFFIX)
    assert observed == expected, (
        f"observed {observed}, expected the wrapped pre-trim size {expected}; "
        f"a post-trim size would report every clipped kb_qa answer as exactly "
        f"the cap and hide the overflow entirely"
    )
    # #1086's guarantee still holds: the relay instructions survive.
    assert wrapped.endswith(me.KB_QA_RELAY_SUFFIX)


@pytest.mark.asyncio
async def test_a_formatter_trimmed_kb_qa_result_is_not_counted_twice():
    """One relayed result, one observation — the clip rate must stay a rate.

    A kb_qa answer trimmed by the formatter can still cross the cap at the loop,
    because PII redaction runs in between and *expands* text. That is a second
    cut on the same result: worth a log line, but not a second increment, and
    not a second (smaller, post-trim) size observation.
    """
    engine = _engine_returning("A" * 20000, called_tool="kb_qa")

    with (
        patch.object(me, "tool_result_relayed_total") as relayed,
        patch.object(me, "tool_result_truncated_total") as truncated,
        patch.object(me, "tool_result_chars") as sizes,
    ):
        await engine._tool_augmented_generate(
            prompt="Search",
            schema_model=SampleResponse,
            investigation_tools=[
                {"type": "function", "function": {"name": "kb_qa", "parameters": {}}}
            ],
            tool_context=MagicMock(),
        )

    # Denominator counts the result exactly once...
    relayed.labels.assert_called_once_with(tool="kb_qa")
    relayed.labels.return_value.inc.assert_called_once()
    # ...and so do the numerator and the distribution, at the formatter only.
    assert truncated.labels.return_value.inc.call_count == 1
    assert sizes.labels.return_value.observe.call_count == 1


@pytest.mark.asyncio
async def test_an_answer_quoting_the_trim_marker_is_still_measured():
    """A kb_qa answer that merely *quotes* the marker must not read as trimmed.

    The loop detects "the formatter already counted this one" from the marker
    it appends. A substring search would also match an answer that happens to
    contain that literal — a runbook about this very truncation, say — and the
    result would silently lose its histogram sample, plus its truncation count
    if redaction then pushed it past the cap.

    The check is anchored to the tail instead: the formatter emits
    ``marker + suffix`` at the very end, and both are static instruction text
    the redactor does not rewrite, so a genuine trim always ends that way while
    a quotation does not.
    """
    quoting_answer = (
        "Runbooks sometimes end with " + me.KB_QA_ANSWER_TRUNCATED_MARKER + " inline."
    )
    engine = _engine_returning(quoting_answer, called_tool="kb_qa")

    with (
        patch.object(me, "tool_result_relayed_total") as relayed,
        patch.object(me, "tool_result_truncated_total") as truncated,
        patch.object(me, "tool_result_chars") as sizes,
    ):
        await engine._tool_augmented_generate(
            prompt="Search",
            schema_model=SampleResponse,
            investigation_tools=[
                {"type": "function", "function": {"name": "kb_qa", "parameters": {}}}
            ],
            tool_context=MagicMock(),
        )

    relayed.labels.assert_called_once_with(tool="kb_qa")
    # The answer is well under the cap, so it is measured here and never cut.
    sizes.labels.assert_called_once_with(tool="kb_qa")
    sizes.labels.return_value.observe.assert_called_once()
    truncated.labels.assert_not_called()
