"""Reliability metrics for the two engine↔model contracts (A/B eval inputs).

A model A/B evaluation needs "tool-call success rate" and "schema-validity
rate" as first-class numbers. Both events already happened inside the engine
— a hallucinated tool name came back as a short error string, a malformed
response body went down the degradation ladder — but neither was counted, so
an eval run could only infer them from log archaeology.

These tests pin the counting, not the behavior: every dispatch and every
ladder disposition increments exactly one (label, outcome) pair, and the
engine relays/degrades exactly as before. Counters are NoOp shims in the unit
env, so they are patched and their label calls asserted — the same approach
as test_tool_result_truncation_observability.py.
"""

import json
from typing import Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation import milestone_engine as me
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.reliability_metrics import (
    SCHEMA_VALIDATION_OUTCOMES,
    TOOL_CALL_OUTCOMES,
)
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ToolCall
from faultmaven.models.interfaces import ToolResult

pytestmark = [pytest.mark.unit, pytest.mark.llm]

SEARCH_FILE_TOOL = [
    {"type": "function", "function": {"name": "search_file", "parameters": {}}}
]


class SampleResponse(BaseModel):
    agent_response: str = "test response"
    next_action: str = "continue"


def _response_with_calls(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        content="",
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=100,
        response_time_ms=500,
        tool_calls=list(calls),
    )


def _tool_call(name: str, arguments, call_id: str = "call_1") -> ToolCall:
    return ToolCall(
        id=call_id, type="function", function={"name": name, "arguments": arguments}
    )


def _schema_response() -> LLMResponse:
    return _response_with_calls(
        _tool_call(
            "SampleResponse",
            json.dumps({"agent_response": "done", "next_action": "continue"}),
            call_id="call_schema",
        )
    )


def _engine(first_response: LLMResponse, tool_result: ToolResult):
    provider = AsyncMock()
    provider.generate = AsyncMock(side_effect=[first_response, _schema_response()])
    registry = MagicMock()
    registry.get_all_tools.return_value = []
    registry.execute_tool = AsyncMock(return_value=tool_result)
    repo = MagicMock()
    repo.save = AsyncMock()
    return MilestoneEngine(
        llm_provider=provider, repository=repo, investigation_tools=registry
    )


async def _run(engine):
    return await engine._tool_augmented_generate(
        prompt="Search",
        schema_model=SampleResponse,
        investigation_tools=SEARCH_FILE_TOOL,
        tool_context=MagicMock(),
    )


def _attempt_labels(mock_counter):
    return [c.kwargs for c in mock_counter.labels.call_args_list]


@pytest.mark.asyncio
class TestToolCallAttempts:
    async def test_well_formed_call_counts_ok(self):
        engine = _engine(
            _response_with_calls(_tool_call("search_file", json.dumps({"q": "x"}))),
            ToolResult(success=True, data="found"),
        )
        with patch.object(me, "tool_call_attempts_total") as attempts:
            await _run(engine)
        assert _attempt_labels(attempts) == [{"tool": "search_file", "outcome": "ok"}]

    async def test_hallucinated_name_counts_unknown_tool_bounded_label(self):
        """A model-invented name must not mint a Prometheus label: the tool
        label folds to "unknown", the outcome says what happened."""
        engine = _engine(
            _response_with_calls(
                _tool_call("kubectl_delete_prod", json.dumps({"q": "x"}))
            ),
            ToolResult(success=False, data=None, error="Tool not found"),
        )
        with patch.object(me, "tool_call_attempts_total") as attempts:
            await _run(engine)
        assert _attempt_labels(attempts) == [
            {"tool": "unknown", "outcome": "unknown_tool"}
        ]

    async def test_malformed_arguments_count_invalid_args(self):
        engine = _engine(
            _response_with_calls(_tool_call("search_file", '{"q": broken')),
            ToolResult(success=True, data="ran with {} args"),
        )
        with patch.object(me, "tool_call_attempts_total") as attempts:
            await _run(engine)
        assert _attempt_labels(attempts) == [
            {"tool": "search_file", "outcome": "invalid_args"}
        ]

    async def test_tool_side_failure_counts_execution_error(self):
        """A well-formed call whose TOOL failed is infrastructure noise, not
        the model failing the contract — it must be distinguishable."""
        engine = _engine(
            _response_with_calls(_tool_call("search_file", json.dumps({"q": "x"}))),
            ToolResult(success=False, data=None, error="evidence store down"),
        )
        with patch.object(me, "tool_call_attempts_total") as attempts:
            await _run(engine)
        assert _attempt_labels(attempts) == [
            {"tool": "search_file", "outcome": "execution_error"}
        ]

    async def test_schema_tool_call_is_not_an_attempt(self):
        """The schema tool is the response channel, not an investigation tool;
        it is measured by the schema counter, never double-counted here."""
        provider = AsyncMock()
        provider.generate = AsyncMock(side_effect=[_schema_response()])
        registry = MagicMock()
        registry.get_all_tools.return_value = []
        registry.execute_tool = AsyncMock()
        repo = MagicMock()
        repo.save = AsyncMock()
        engine = MilestoneEngine(
            llm_provider=provider, repository=repo, investigation_tools=registry
        )
        with patch.object(me, "tool_call_attempts_total") as attempts:
            await _run(engine)
        attempts.labels.assert_not_called()


# --- schema-validation ladder dispositions -----------------------------------


class _Item(BaseModel):
    value: int


class _LadderSchema(BaseModel):
    agent_response: str
    state_updates: Dict[str, int] = {}
    items: List[_Item] = []
    title: str = "t"


def _bare_engine():
    repo = MagicMock()
    repo.save = AsyncMock()
    return MilestoneEngine(
        llm_provider=AsyncMock(), repository=repo, investigation_tools=MagicMock()
    )


class TestSchemaValidationLadder:
    def _validate(self, content_obj):
        engine = _bare_engine()
        with patch.object(me, "schema_validation_total") as validations:
            try:
                engine._validate_with_degradation(content_obj, _LadderSchema)
            except Exception:
                pass
        return [c.kwargs for c in validations.labels.call_args_list]

    def test_clean(self):
        labels = self._validate({"agent_response": "ok"})
        assert labels == [{"schema": "_LadderSchema", "outcome": "clean"}]

    def test_pruned_invalid_sub_record(self):
        labels = self._validate(
            {
                "agent_response": "ok",
                "items": [{"value": 1}, {"value": "not-an-int"}],
            }
        )
        assert labels == [{"schema": "_LadderSchema", "outcome": "pruned"}]

    def test_state_dropped_on_unrepairable_state_updates(self):
        labels = self._validate(
            {"agent_response": "ok", "state_updates": {"k": "not-an-int"}}
        )
        assert labels == [{"schema": "_LadderSchema", "outcome": "state_dropped"}]

    def test_response_synthesized_when_agent_response_missing(self):
        labels = self._validate({"state_updates": {"k": 1}})
        assert labels == [
            {"schema": "_LadderSchema", "outcome": "response_synthesized"}
        ]

    def test_response_synthesized_state_dropped_is_its_own_outcome(self):
        """A body that needed BOTH the placeholder AND a full state drop lost
        strictly more than one that only needed the placeholder. Recording it
        as plain ``response_synthesized`` (the log line already distinguishes
        the two) makes the state-loss rate under-report exactly the turns that
        lost the most."""
        labels = self._validate({"state_updates": {"k": "not-an-int"}})
        assert labels == [
            {
                "schema": "_LadderSchema",
                "outcome": "response_synthesized_state_dropped",
            }
        ]

    def test_failed_when_unrecoverable(self):
        """A non-prunable defect outside state_updates with a valid
        agent_response exhausts every rung — counted, then re-raised."""
        labels = self._validate({"agent_response": "ok", "title": 123})
        assert labels == [{"schema": "_LadderSchema", "outcome": "failed"}]

    def test_exactly_one_disposition_per_body(self):
        """The ladder must record its FINAL disposition once — a body that
        walks several rungs is still one body."""
        for body in (
            {"agent_response": "ok"},
            {"state_updates": {"k": 1}},
            {"state_updates": {"k": "not-an-int"}},
            {"agent_response": "ok", "title": 123},
        ):
            assert len(self._validate(body)) == 1


@pytest.mark.asyncio
class TestAttemptSurvivesDispatchFailure:
    """The attempt counter is the DENOMINATOR of the well-formed-invocation
    rate. Incrementing it after the dispatch dropped an invocation from both
    numerator and denominator whenever the dispatch raised — so the rate read
    cleaner the more the infrastructure failed."""

    async def test_attempt_counted_when_execute_tool_raises(self):
        engine = _engine(
            _response_with_calls(_tool_call("search_file", json.dumps({"q": "x"}))),
            ToolResult(success=True, data="found"),
        )
        engine.investigation_tools.execute_tool = AsyncMock(
            side_effect=RuntimeError("provider socket closed")
        )
        with patch.object(me, "tool_call_attempts_total") as attempts:
            with pytest.raises(RuntimeError):
                await _run(engine)
        assert _attempt_labels(attempts) == [
            {"tool": "search_file", "outcome": "execution_error"}
        ]

    async def test_contract_failure_is_not_relabelled_by_a_later_raise(self):
        """A hallucinated name failed the MODEL's contract before any tool ran;
        an exception from the dispatch must not overwrite that with the
        infrastructure-noise label."""
        engine = _engine(
            _response_with_calls(_tool_call("kubectl_delete_prod", "{}")),
            ToolResult(success=False, data=None, error="Tool not found"),
        )
        engine.investigation_tools.execute_tool = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        with patch.object(me, "tool_call_attempts_total") as attempts:
            with pytest.raises(RuntimeError):
                await _run(engine)
        assert _attempt_labels(attempts) == [
            {"tool": "unknown", "outcome": "unknown_tool"}
        ]


def test_outcome_vocabularies_match_the_engine_call_sites():
    """The tuples are the label contract, and nothing enforced it: every
    outcome is re-spelled as a literal at its call site, so a typo mints a new
    Prometheus label silently and the tuples drift stale unnoticed. Read the
    literals out of the engine's AST and require exact agreement in both
    directions."""
    import ast
    import pathlib as _pathlib

    tree = ast.parse(_pathlib.Path(me.__file__).read_text())

    recorded: set[str] = set()
    attempted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ("_record", "_record_schema_validation"):
                for arg in node.args:
                    for sub in ast.walk(arg):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            recorded.add(sub.value)
        elif isinstance(node, ast.Assign):
            if any(
                isinstance(t, ast.Name) and t.id == "_attempt_outcome"
                for t in node.targets
            ) and isinstance(node.value, ast.Constant):
                attempted.add(node.value.value)

    # Non-empty first: a refactor that renames the recorder or the variable
    # must fail loudly here rather than make this test vacuously true.
    assert recorded, "found no schema-validation outcome literals to pin"
    assert attempted, "found no tool-call outcome literals to pin"
    assert recorded == set(SCHEMA_VALIDATION_OUTCOMES)
    assert attempted == set(TOOL_CALL_OUTCOMES)


class TestNonToolStructuredPathIsCounted:
    """The engine validates in TWO places. ``_validate_with_degradation`` (the
    ladder) was counted; ``_generate_structured_output_inner`` validates
    directly with ``model_validate_json`` and was not — so on any turn served
    by the non-tool structured path (a tool-incapable model, the
    ToolCallingUnsupportedError fallback, a FUNCTION_CALLING single shot) the
    A/B schema-validity rate was read over a denominator that excluded the very
    calls being measured."""

    @staticmethod
    def _provider(content: str):
        from faultmaven.infrastructure.llm.structured_output_capability import (
            StructuredOutputCapability,
            StructuredOutputMode,
            StructuredOutputStrategy,
        )

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
        return provider

    def _engine_for(self, content: str) -> MilestoneEngine:
        repo = MagicMock()
        repo.save = AsyncMock()
        return MilestoneEngine(
            llm_provider=self._provider(content),
            repository=repo,
            investigation_tools=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_valid_body_counts_clean(self):
        engine = self._engine_for(json.dumps({"agent_response": "hi"}))
        with patch.object(me, "schema_validation_total") as validations:
            await engine._generate_structured_output_inner("p", SampleResponse)
        assert [c.kwargs for c in validations.labels.call_args_list] == [
            {"schema": "SampleResponse", "outcome": "clean"}
        ]

    @pytest.mark.asyncio
    async def test_repairable_violation_counts_its_degradation_outcome(self):
        # fm#1116: the non-tool path now validates through the same ladder as
        # the tool path. A non-string agent_response is repaired with the
        # placeholder and counted as such — one increment, under the ladder's
        # own label — instead of failing the turn.
        engine = self._engine_for(json.dumps({"agent_response": 123}))
        with patch.object(me, "schema_validation_total") as validations:
            parsed = await engine._generate_structured_output_inner("p", SampleResponse)
        assert isinstance(parsed, SampleResponse)
        assert [c.kwargs for c in validations.labels.call_args_list] == [
            {"schema": "SampleResponse", "outcome": "response_synthesized"}
        ]

    @pytest.mark.asyncio
    async def test_unrepairable_body_still_counts_failed_and_raises(self):
        # A body that is not an object survives no rung of the ladder.
        engine = self._engine_for(json.dumps([1, 2, 3]))
        with patch.object(me, "schema_validation_total") as validations:
            with pytest.raises(Exception):
                await engine._generate_structured_output_inner("p", SampleResponse)
        assert [c.kwargs for c in validations.labels.call_args_list] == [
            {"schema": "SampleResponse", "outcome": "failed"}
        ]
