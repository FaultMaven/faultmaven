"""What ``kb_qa`` answered from is cited in the turn's ``sources``.

The bar: a runbook section the model fetched with ``kb_qa`` and answered from
reaches the turn's ``sources`` with its document id (``trigger`` ``kb_qa``),
only when the reply the turn returns came from the tool loop that ran the
search, and each section once whichever channels carried it. The end-to-end
tests run the real ``KBToolAdapter`` inside the real tool loop, and the
fallback test asserts the search ran, so "cited nothing" cannot pass because
nothing was searched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.kb_push import TURN_METADATA_KB_PROMPTED
from faultmaven.core.investigation.kb_question import (
    KB_ENTRY_ALSO_PUSHED,
    KB_QA_TRIGGER,
    KB_QUESTION_EXCERPT_CHARS,
    KB_QUESTION_TRIGGER,
    take_over_pushed,
)
from faultmaven.core.investigation.kb_tool_citations import (
    KB_QA_MAX_ENTRIES,
    add_kb_qa_entries,
    kb_qa_entry,
)
from faultmaven.core.investigation.milestone_engine import turn_generation
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)
from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.domain.services.investigation_service.turn_bookkeeping import (
    _kb_sources,
    _record_turn_kb_sources,
)
from faultmaven.modules.agent.tools.base import KbCitations, ToolContext
from faultmaven.modules.agent.tools.document_qa_tool import DocumentQATool
from faultmaven.modules.agent.tools.kb_configs.unified_kb_config import (
    UnifiedKBConfig,
)
from faultmaven.modules.agent.tools.kb_tool_adapter import KBToolAdapter
from faultmaven.modules.case.contracts import MESSAGE_METADATA_KB_SOURCES, CaseState
from tests.case_builders import investigating_case
from tests.model_content import disabled_redaction
from tests.unit.core.investigation.test_terminal_qa_tools_1964 import (
    FakeRegistry,
    ScriptedProvider,
    answer,
    calls,
    run_terminal_qa,
    terminal_case,
    terminal_engine,
)
from tests.unit.modules.agent.tools.test_document_qa_noise_floor import (
    _llm_response,
)
from tests.utils import admit_every_parent

pytestmark = pytest.mark.unit


def _chunk(parent: str, score: float = 0.7, text: str = "Scale the consumers."):
    return {
        "content": text,
        "score": score,
        "metadata": {"title": f"Runbook {parent}", "parent_document_id": parent},
    }


def _entry(parent: str, trigger: str, text: str = "Scale the consumers."):
    return {
        "title": f"Runbook {parent}",
        "parent_document_id": parent,
        "summary": text,
        "score": 0.6,
        "trigger": trigger,
    }


# ---------------------------------------------------------------------------
# The entries and the merge
# ---------------------------------------------------------------------------


class TestTheEntry:
    def test_a_cited_chunk_becomes_a_kb_qa_entry(self):
        assert kb_qa_entry(_chunk("rb_kafka", 0.71)) == {
            "title": "Runbook rb_kafka",
            "parent_document_id": "rb_kafka",
            "summary": "Scale the consumers.",
            "score": 0.71,
            "trigger": KB_QA_TRIGGER,
        }

    def test_the_excerpt_is_bounded(self):
        entry = kb_qa_entry(_chunk("rb_kafka", text="x" * 5000))
        assert entry["summary"].startswith("x" * KB_QUESTION_EXCERPT_CHARS)
        assert len(entry["summary"]) < 5000


class TestTheMerge:
    def test_a_fetched_section_is_added(self):
        rendered: list = []
        add_kb_qa_entries(rendered, [_entry("rb_kafka", KB_QA_TRIGGER)])
        assert [(e["parent_document_id"], e["trigger"]) for e in rendered] == [
            ("rb_kafka", KB_QA_TRIGGER)
        ]

    def test_a_pushed_section_the_model_fetched_is_cited_once_as_kb_qa(self):
        rendered = [_entry("rb_kafka", "symptom"), _entry("rb_other", "symptom")]
        add_kb_qa_entries(rendered, [_entry("rb_kafka", KB_QA_TRIGGER)])
        assert [(e["parent_document_id"], e["trigger"]) for e in rendered] == [
            ("rb_other", "symptom"),
            ("rb_kafka", KB_QA_TRIGGER),
        ]

    def test_a_section_the_question_lookup_found_stays_the_lookups(self):
        rendered = [_entry("rb_kafka", KB_QUESTION_TRIGGER)]
        add_kb_qa_entries(rendered, [_entry("rb_kafka", KB_QA_TRIGGER)])
        assert [(e["parent_document_id"], e["trigger"]) for e in rendered] == [
            ("rb_kafka", KB_QUESTION_TRIGGER)
        ]

    def test_a_section_two_calls_fetched_is_cited_once(self):
        rendered: list = []
        add_kb_qa_entries(
            rendered,
            [_entry("rb_kafka", KB_QA_TRIGGER), _entry("rb_kafka", KB_QA_TRIGGER)],
        )
        assert len(rendered) == 1

    def test_two_sections_of_one_runbook_are_both_cited(self):
        rendered: list = []
        add_kb_qa_entries(
            rendered,
            [
                _entry("rb_kafka", KB_QA_TRIGGER, "Scale the consumers."),
                _entry("rb_kafka", KB_QA_TRIGGER, "Check the partition count."),
            ],
        )
        assert len(rendered) == 2

    def test_a_section_with_no_document_id_is_not_citable(self):
        rendered: list = []
        add_kb_qa_entries(rendered, [_entry("", KB_QA_TRIGGER)])
        assert rendered == []


class TestTheCollector:
    def test_entries_are_cited_only_once_confirmed(self):
        citations = KbCitations()
        citations.add([_entry("rb_kafka", KB_QA_TRIGGER)])
        citations.attribute("call_1")
        citations.sent(frozenset({"call_1"}))
        assert citations.confirmed == []
        citations.confirm()
        assert [e["parent_document_id"] for e in citations.confirmed] == ["rb_kafka"]

    def test_a_call_the_last_request_did_not_carry_is_not_confirmed(self):
        """Its exchange was elided to fit the loop's budget: the reply did not
        have it in front of it."""
        citations = KbCitations()
        citations.add([_entry("rb_elided", KB_QA_TRIGGER)])
        citations.attribute("call_1")
        citations.add([_entry("rb_kept", KB_QA_TRIGGER)])
        citations.attribute("call_2")
        citations.sent(frozenset({"call_2"}))
        citations.confirm()
        assert [e["parent_document_id"] for e in citations.confirmed] == ["rb_kept"]

    def test_a_new_loop_drops_what_an_earlier_attempt_fetched(self):
        citations = KbCitations()
        citations.add([_entry("rb_discarded", KB_QA_TRIGGER)])
        citations.attribute("call_1")
        citations.start_loop()
        citations.sent(frozenset({"call_1"}))
        citations.confirm()
        assert citations.confirmed == []

    def test_every_copy_of_a_context_shares_the_turns_collector(self):
        context = ToolContext(
            session_id="c",
            case_id="c",
            enterprise_id="e",
            user_id="u",
            redaction_ctx=disabled_redaction(),
        )
        assert context.with_execution_id("x").kb_citations is context.kb_citations


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


def _context() -> ToolContext:
    return ToolContext(
        session_id="case_1",
        case_id="case_1",
        enterprise_id="ent_1",
        user_id="user_1",
        kb_scope_filter={"scope": "global"},
        redaction_ctx=disabled_redaction(),
    )


def _held(citations: KbCitations) -> list[str]:
    """What the collector holds for the next call, by runbook."""
    citations.attribute("probe")
    citations.sent(frozenset({"probe"}))
    citations.confirm()
    return [e["parent_document_id"] for e in citations.confirmed]


class TestTheAdapter:
    async def test_what_the_answer_cites_is_held_for_the_turn(self):
        wrapped = MagicMock()
        wrapped.answer = AsyncMock(
            return_value=("answer\n\nSources: Runbook rb_kafka", [_chunk("rb_kafka")])
        )
        context = _context()
        result = await KBToolAdapter(
            wrapped, admit=admit_every_parent
        ).execute_with_context({"question": "consumer lag?"}, context)
        assert result.success
        assert context.kb_citations.confirmed == []  # held, not cited
        assert _held(context.kb_citations) == ["rb_kafka"]

    async def test_a_failed_search_holds_nothing(self):
        wrapped = MagicMock()
        wrapped.answer = AsyncMock(side_effect=RuntimeError("store down"))
        context = _context()
        result = await KBToolAdapter(
            wrapped, admit=admit_every_parent
        ).execute_with_context({"question": "consumer lag?"}, context)
        assert not result.success
        assert _held(context.kb_citations) == []

    async def test_a_chunk_that_cannot_be_cited_does_not_fail_the_answer(self):
        wrapped = MagicMock()
        wrapped.answer = AsyncMock(
            return_value=(
                "answer",
                [{"content": "x", "metadata": ["not", "a", "dict"]}, _chunk("rb_ok")],
            )
        )
        context = _context()
        result = await KBToolAdapter(
            wrapped, admit=admit_every_parent
        ).execute_with_context({"question": "consumer lag?"}, context)
        assert result.success
        assert result.data == "answer"
        assert _held(context.kb_citations) == ["rb_ok"]


class TestWhatTheAnswerCites:
    async def test_only_chunks_at_or_above_the_floor_are_cited(self):
        threshold = UnifiedKBConfig().relevance_threshold
        chunks = [
            _chunk("rb_used", threshold + 0.2),
            _chunk("rb_weak", threshold - 0.1),
        ]
        store = MagicMock()
        store.hybrid_search = AsyncMock(return_value=chunks)
        store.search = AsyncMock(return_value=chunks)
        router = MagicMock()
        router.route = AsyncMock(return_value=_llm_response("scale the consumers"))
        tool = DocumentQATool(store, router, UnifiedKBConfig())

        text, cited = await tool.answer_with_citations(
            "consumer lag?",
            filters={"scope": "global"},
            redaction_ctx=disabled_redaction(),
            admit=admit_every_parent,
        )

        assert "scale the consumers" in text
        assert [c["metadata"]["parent_document_id"] for c in cited] == ["rb_used"]

    async def test_a_refused_synthesis_cites_nothing(self):
        threshold = UnifiedKBConfig().relevance_threshold
        chunks = [_chunk("rb_weak", threshold - 0.1)]
        store = MagicMock()
        store.hybrid_search = AsyncMock(return_value=chunks)
        store.search = AsyncMock(return_value=chunks)
        tool = DocumentQATool(store, MagicMock(), UnifiedKBConfig())

        _text, cited = await tool.answer_with_citations(
            "consumer lag?",
            filters={"scope": "global"},
            redaction_ctx=disabled_redaction(),
            admit=admit_every_parent,
        )

        assert cited == []


# ---------------------------------------------------------------------------
# The tool loop, end to end
# ---------------------------------------------------------------------------


class _KbRegistry(FakeRegistry):
    """The fake registry, with the REAL kb_qa adapter behind its name."""

    def __init__(self, *names: str):
        super().__init__(*names)
        self.wrapped = MagicMock()
        self.wrapped.answer = AsyncMock(
            return_value=("Scale the consumers.", [_chunk("rb_kafka")])
        )
        self._kb_qa = KBToolAdapter(self.wrapped, admit=admit_every_parent)

    async def execute_tool(self, name: str, params: dict, context: Any) -> ToolResult:
        if name != "kb_qa":
            return await super().execute_tool(name, params, context)
        self.executed.append(name)
        return await self._kb_qa.execute_with_context(params, context)


class _FailingAfterTools(ScriptedProvider):
    """A provider that fails once the tools have run, so the loop falls back
    to the non-tool path, which it then answers with strict JSON."""

    async def route(self, **kwargs: Any):
        self.requests.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def get_structured_output_strategy(self, schema, model=None):
        return StructuredOutputStrategy(
            capability=StructuredOutputCapability.STRICT,
            mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
            include_schema_in_prompt=False,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "S", "strict": True, "schema": schema},
            },
        )


def _json_answer(text: str):
    return _llm_response(json.dumps({"agent_response": text, "state_updates": {}}))


def _cited(result) -> list[tuple[str, str]]:
    return [
        (e["parent_document_id"], e["trigger"])
        for e in result["metadata"][TURN_METADATA_KB_PROMPTED]
    ]


class TestTheTerminalTurn:
    async def test_a_section_kb_qa_answered_from_is_cited(self):
        provider = ScriptedProvider(
            calls(("kb_qa", {"question": "consumer lag?"})),
            answer("Scale the consumers, per the runbook."),
        )
        registry = _KbRegistry("kb_qa")
        engine, _ = terminal_engine(provider, registry)

        result = await run_terminal_qa(engine, terminal_case(CaseState.RESOLVED))

        assert registry.executed == ["kb_qa"]
        assert _cited(result) == [("rb_kafka", KB_QA_TRIGGER)]

    async def test_a_reply_from_the_no_tools_fallback_cites_nothing_it_fetched(self):
        provider = _FailingAfterTools(
            calls(("kb_qa", {"question": "consumer lag?"})),
            RuntimeError("provider failed mid-loop"),
            _json_answer("Answered without the tools."),
        )
        registry = _KbRegistry("kb_qa")
        engine, _ = terminal_engine(provider, registry)

        result = await run_terminal_qa(engine, terminal_case(CaseState.RESOLVED))

        assert registry.executed == ["kb_qa"]  # the search ran
        assert result["agent_response"] == "Answered without the tools."
        assert _cited(result) == []


class TestTheInvestigatingTurn:
    async def test_the_turn_reports_what_the_tool_loop_confirmed(self, monkeypatch):
        """The INVESTIGATING tool route adds the confirmed entries to the
        turn's report, and only those."""
        rendered: list = [_entry("rb_pushed", "symptom")]

        async def generate(prompt, schema, **kwargs):
            citations = kwargs["tool_context"].kb_citations
            citations.add([kb_qa_entry(_chunk("rb_kafka"))])
            citations.attribute("call_1")
            citations.sent(frozenset({"call_1"}))
            citations.confirm()
            citations.add([kb_qa_entry(_chunk("rb_unconfirmed"))])
            citations.attribute("call_2")
            return SimpleNamespace(agent_response="ok")

        generator = MagicMock()
        generator.generate_structured_output = AsyncMock(side_effect=generate)
        generator.build_da_tool_schemas = MagicMock(return_value=[{"t": 1}])
        generator.build_tool_context = AsyncMock(return_value=_context())
        generator.given_tools = MagicMock(return_value=frozenset({"kb_qa"}))
        monkeypatch.setattr(
            turn_generation, "get_prompt_for_case", lambda *a, **k: "prompt"
        )
        monkeypatch.setattr(
            turn_generation, "_should_force_tools", lambda *a, **k: False
        )
        monkeypatch.setattr(
            turn_generation,
            "_route_toolless_turn_single_shot",
            lambda *a, **k: False,
        )

        await turn_generation._generate_turn_response(
            MagicMock(),
            MagicMock(),
            MagicMock(),
            generator,
            case=investigating_case(),
            redaction_ctx=disabled_redaction(),
            intent_data={"query_mode": "directed_analysis"},
            user_id="user_1",
            user_message="what does the runbook say?",
            bare_resolve_decline=False,
            kb_rendered=rendered,
        )

        assert [(e["parent_document_id"], e["trigger"]) for e in rendered] == [
            ("rb_pushed", "symptom"),
            ("rb_kafka", KB_QA_TRIGGER),
        ]


# ---------------------------------------------------------------------------
# The turn's sources
# ---------------------------------------------------------------------------


def _row(turn, entries):
    published = [s.model_dump(mode="json") for s in _kb_sources(list(entries))]
    return {
        "role": "assistant",
        "turn_number": turn,
        "metadata": {MESSAGE_METADATA_KB_SOURCES: published},
    }


def _flags(turn_meta):
    return {
        (s["metadata"]["document_id"], s["metadata"]["trigger"]): s["new_this_turn"]
        for s in turn_meta[MESSAGE_METADATA_KB_SOURCES]
    }


class TestTheTurnsSources:
    def test_a_kb_qa_source_is_always_new(self):
        fetched = _entry("rb_kafka", KB_QA_TRIGGER)
        earlier = [_row(2, [fetched])]
        turn_meta = {TURN_METADATA_KB_PROMPTED: [fetched]}
        _record_turn_kb_sources(turn_meta, SimpleNamespace(messages=earlier))
        assert _flags(turn_meta) == {("rb_kafka", KB_QA_TRIGGER): True}

    def test_a_row_with_only_kb_qa_sources_is_skipped_for_the_push(self):
        pushed = _entry("rb_pushed", "symptom")
        earlier = [_row(2, [pushed]), _row(3, [_entry("rb_kafka", KB_QA_TRIGGER)])]
        turn_meta = {TURN_METADATA_KB_PROMPTED: [pushed]}
        _record_turn_kb_sources(turn_meta, SimpleNamespace(messages=earlier))
        assert _flags(turn_meta) == {("rb_pushed", "symptom"): False}

    def test_a_pushed_section_kb_qa_also_fetched_is_shown_on_the_next_turn(self):
        """Turn 3 fetched the standing section with kb_qa; on turn 4 the push
        alone carries it again, and it is not new."""
        earlier = [
            _row(2, [_entry("rb_kafka", "symptom")]),
            _row(3, [_entry("rb_other", "symptom"), _entry("rb_kafka", KB_QA_TRIGGER)]),
        ]
        turn_meta = {TURN_METADATA_KB_PROMPTED: [_entry("rb_kafka", "symptom")]}
        _record_turn_kb_sources(turn_meta, SimpleNamespace(messages=earlier))
        assert _flags(turn_meta) == {("rb_kafka", "symptom"): False}


# ---------------------------------------------------------------------------
# The runtime overflow retry (#662) answers from the minimal prompt
# ---------------------------------------------------------------------------


class TestTheOverflowRetry:
    async def test_it_cites_only_what_the_minimal_prompt_carries(self):
        """The retry's prompt carries the question lookup's verdict and no
        pushed runbook, so the report keeps the lookup's entries only."""
        from faultmaven.core.investigation.milestone_engine.engine import (
            MilestoneEngine,
        )
        from faultmaven.core.investigation.milestone_engine.errors import (
            MilestoneEngineError,
        )
        from faultmaven.exceptions import TOKEN_LIMIT

        engine = MilestoneEngine(
            MagicMock(), MagicMock(), investigation_tools=MagicMock()
        )
        overflow = MilestoneEngineError("too long", error_code=TOKEN_LIMIT)
        inner = AsyncMock(side_effect=[overflow, MagicMock(name="degraded")])
        engine.generator._generate_structured_output_inner = inner
        rendered = [
            _entry("rb_pushed", "symptom"),
            _entry("rb_asked", KB_QUESTION_TRIGGER),
        ]
        # Positive control: the full prompt's report holds a pushed entry.
        assert any(e["trigger"] == "symptom" for e in rendered)

        await engine.generator.generate_structured_output(
            "huge",
            MagicMock(),
            case=investigating_case(),
            user_message="the consumers are still behind",
            redaction_ctx=disabled_redaction(),
            kb_rendered=rendered,
        )

        assert inner.await_count == 2  # the retry ran
        assert [(e["parent_document_id"], e["trigger"]) for e in rendered] == [
            ("rb_asked", KB_QUESTION_TRIGGER)
        ]

    async def test_a_reply_that_fit_keeps_the_full_report(self):
        from faultmaven.core.investigation.milestone_engine.engine import (
            MilestoneEngine,
        )

        engine = MilestoneEngine(
            MagicMock(), MagicMock(), investigation_tools=MagicMock()
        )
        engine.generator._generate_structured_output_inner = AsyncMock(
            return_value=MagicMock(name="answer")
        )
        rendered = [_entry("rb_pushed", "symptom")]

        await engine.generator.generate_structured_output(
            "fits",
            MagicMock(),
            case=investigating_case(),
            user_message="the consumers are still behind",
            redaction_ctx=disabled_redaction(),
            kb_rendered=rendered,
        )

        assert [e["parent_document_id"] for e in rendered] == ["rb_pushed"]


# ---------------------------------------------------------------------------
# Review round (#2799)
# ---------------------------------------------------------------------------


class TestTheCap:
    def test_the_best_sections_are_kept_within_the_caps(self):
        cited = (
            [
                dict(_entry("rb_a", KB_QA_TRIGGER, f"a{i}"), score=0.9 - i / 100)
                for i in range(4)
            ]
            + [
                dict(_entry("rb_b", KB_QA_TRIGGER, f"b{i}"), score=0.7 - i / 100)
                for i in range(3)
            ]
            + [
                dict(_entry("rb_c", KB_QA_TRIGGER, f"c{i}"), score=0.5 - i / 100)
                for i in range(3)
            ]
        )
        rendered: list = []
        add_kb_qa_entries(rendered, list(reversed(cited)))  # worst first
        assert [(e["parent_document_id"], e["summary"]) for e in rendered] == [
            ("rb_a", "a0"),
            ("rb_a", "a1"),
            ("rb_b", "b0"),
            ("rb_b", "b1"),
            ("rb_c", "c0"),
        ]
        assert len(rendered) == KB_QA_MAX_ENTRIES


class TestATakeoverIsRecorded:
    """A turn's own search that takes over a pushed section marks it, so the
    next turn still compares its standing context against that row."""

    def test_the_kb_qa_entry_is_marked(self):
        rendered = [_entry("rb_kafka", "symptom")]
        add_kb_qa_entries(rendered, [_entry("rb_kafka", KB_QA_TRIGGER)])
        assert rendered[0][KB_ENTRY_ALSO_PUSHED] is True

    def test_a_section_only_the_search_found_is_not_marked(self):
        rendered = [_entry("rb_other", "symptom")]
        add_kb_qa_entries(rendered, [_entry("rb_kafka", KB_QA_TRIGGER)])
        assert KB_ENTRY_ALSO_PUSHED not in rendered[-1]

    @pytest.mark.parametrize("channel", [KB_QA_TRIGGER, KB_QUESTION_TRIGGER])
    def test_standing_context_a_search_took_over_is_not_new_next_turn(self, channel):
        """Turn N: the push carries A and this turn's search finds A too, so
        A is cited as the search's. Turn N+1: the push alone carries A."""
        rendered = [_entry("rb_a", "symptom")]
        found = [_entry("rb_a", channel)]
        rendered[:] = take_over_pushed(rendered, found) + found
        turn_n = {TURN_METADATA_KB_PROMPTED: rendered}
        _record_turn_kb_sources(turn_n, SimpleNamespace(messages=[]))
        rows = [
            {
                "role": "assistant",
                "metadata": {
                    MESSAGE_METADATA_KB_SOURCES: turn_n[MESSAGE_METADATA_KB_SOURCES]
                },
            }
        ]

        turn_next = {TURN_METADATA_KB_PROMPTED: [_entry("rb_a", "symptom")]}
        _record_turn_kb_sources(turn_next, SimpleNamespace(messages=rows))

        assert _flags(turn_n) == {("rb_a", channel): True}
        assert _flags(turn_next) == {("rb_a", "symptom"): False}


class TestAnElidedExchangeIsNotCited:
    async def test_a_kb_qa_result_elided_before_the_reply_is_not_cited(
        self, monkeypatch
    ):
        """The loop elides the oldest exchange, the kb_qa call, before the
        final request; the reply did not see it."""
        provider = ScriptedProvider(
            calls(("kb_qa", {"question": "consumer lag?"})),
            calls(("search_file", {"evidence_id": "ev_196400000001"})),
            answer("Answered from the log."),
        )
        registry = _KbRegistry("kb_qa", "search_file")
        engine, _ = terminal_engine(provider, registry)
        bound = engine.generator._bound_tool_loop_messages

        def eliding(messages, *args, **kwargs):
            out = bound(messages, *args, **kwargs)
            if not any(m.get("name") == "search_file" for m in out):
                return out
            return [
                m
                for m in out
                if m.get("tool_call_id") != "call_0_kb_qa"
                and not any(
                    tc.get("id") == "call_0_kb_qa" for tc in (m.get("tool_calls") or [])
                )
            ]

        monkeypatch.setattr(engine.generator, "_bound_tool_loop_messages", eliding)

        result = await run_terminal_qa(engine, terminal_case(CaseState.RESOLVED))

        assert registry.executed == ["kb_qa", "search_file"]  # the search ran
        assert _cited(result) == []


class TestAFailedOverflowRetry:
    async def test_it_leaves_the_report_as_it_was(self):
        from faultmaven.core.investigation.milestone_engine.engine import (
            MilestoneEngine,
        )
        from faultmaven.core.investigation.milestone_engine.errors import (
            MilestoneEngineError,
        )
        from faultmaven.exceptions import TOKEN_LIMIT

        engine = MilestoneEngine(
            MagicMock(), MagicMock(), investigation_tools=MagicMock()
        )
        overflow = MilestoneEngineError("too long", error_code=TOKEN_LIMIT)
        engine.generator._generate_structured_output_inner = AsyncMock(
            side_effect=[overflow, RuntimeError("retry failed too")]
        )
        rendered = [_entry("rb_pushed", "symptom")]

        with pytest.raises(RuntimeError):
            await engine.generator.generate_structured_output(
                "huge",
                MagicMock(),
                case=investigating_case(),
                user_message="the consumers are still behind",
                redaction_ctx=disabled_redaction(),
                kb_rendered=rendered,
            )

        assert [e["parent_document_id"] for e in rendered] == ["rb_pushed"]
