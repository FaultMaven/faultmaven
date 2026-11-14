"""A user's question about the knowledge base itself: the lookup, its prompt
block, the turn's sources, and the engine wiring (§5.6 of
``orchestration-capabilities.md``).

The bar: a question about the knowledge base gets a search before the reply on
every route, and the reply reports what the search established — a miss as a
miss, a failure as a failure, never one dressed as the other. Each prompt
assertion carries a positive control, so a builder that silently dropped the
block cannot pass as "the block says the right thing".
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.kb_push import TURN_METADATA_KB_PROMPTED
from faultmaven.core.investigation.kb_question import (
    KB_LOOKUP_HEADING,
    KB_QUESTION_CANDIDATE_FLOOR,
    KB_QUESTION_EXCERPT_CHARS,
    KB_QUESTION_INTENT_KEY,
    KB_QUESTION_MAX_ENTRIES,
    KB_QUESTION_TRIGGER,
    KbLookup,
    KbLookupStatus,
    kb_question_of,
    render_kb_lookup_block,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.kb_question_lookup import (
    look_up_kb_question,
)
from faultmaven.core.investigation.prompts.fence import PromptFence, mint_token
from faultmaven.core.investigation.prompts.templates.assembly import get_prompt_for_case
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
)
from faultmaven.infrastructure.llm.prompt_cache import CACHE_BOUNDARY
from faultmaven.models.common import SearchResult
from faultmaven.modules.agent.domain.services.investigation_service.turn_bookkeeping import (
    _kb_sources,
    _record_turn_kb_sources,
)
from faultmaven.modules.case.contracts import MESSAGE_METADATA_KB_SOURCES, CaseState
from tests.case_builders import inquiry_case, investigating_case

pytestmark = pytest.mark.unit

LOOKUP_QUESTION = {"kind": "lookup", "topic": "kafka consumer lag"}


def _hit(parent: str, score: float, title: str | None = None, text: str = "x"):
    return SearchResult(
        document_id=f"{parent}_chunk_0",
        title=title or f"Runbook {parent}",
        document_type="runbook",
        tags=[],
        score=score,
        snippet=text[:200] + "...",
        content=text,
        parent_document_id=parent,
    )


def _deps(results=None, *, raises: Exception | None = None, team_raises=False):
    service = MagicMock()
    service.search_knowledge = AsyncMock(
        side_effect=raises, return_value=list(results or [])
    )
    team_service = None
    share_repository = None
    if team_raises:
        team_service = MagicMock()
        team_service.list_all_user_team_ids = AsyncMock(
            side_effect=RuntimeError("teams down")
        )
        share_repository = MagicMock()
    return SimpleNamespace(
        knowledge_service=service,
        team_service=team_service,
        share_repository=share_repository,
    )


def _case(description: str = "consumers fall behind on the orders topic"):
    return SimpleNamespace(
        case_id="case_aabbccddeeff",
        description=description,
        effective_driver_id="user_driver",
        user_id="user_creator",
        enterprise_id="ent_1",
    )


# ---------------------------------------------------------------------------
# The question the service recorded
# ---------------------------------------------------------------------------


class TestTheRecordedQuestion:
    def test_a_well_formed_question_is_read(self):
        data = {KB_QUESTION_INTENT_KEY: {"kind": "lookup", "topic": " lag "}}
        assert kb_question_of(data) == {"kind": "lookup", "topic": "lag"}

    @pytest.mark.parametrize(
        "raw",
        [None, "lookup", {"kind": "delete", "topic": "x"}, {"kind": "lookup"}, []],
    )
    def test_anything_else_is_no_question(self, raw):
        assert kb_question_of({KB_QUESTION_INTENT_KEY: raw}) is None

    def test_no_intent_data_is_no_question(self):
        assert kb_question_of(None) is None


# ---------------------------------------------------------------------------
# The lookup
# ---------------------------------------------------------------------------


class TestTheLookup:
    async def test_candidates_are_the_hits_at_or_above_the_floor(self):
        deps = _deps([_hit("rb_a", 0.66), _hit("rb_b", 0.52)])
        lookup = await look_up_kb_question(deps, _case(), LOOKUP_QUESTION)

        assert lookup.status is KbLookupStatus.CANDIDATES
        assert [e["parent_document_id"] for e in lookup.entries] == ["rb_a", "rb_b"]
        assert all(e["trigger"] == KB_QUESTION_TRIGGER for e in lookup.entries)
        call = deps.knowledge_service.search_knowledge.await_args.kwargs
        assert call["query"] == "kafka consumer lag"
        assert call["min_score"] == KB_QUESTION_CANDIDATE_FLOOR

    async def test_the_floor_holds_even_if_the_store_ignored_it(self):
        deps = _deps([_hit("rb_a", KB_QUESTION_CANDIDATE_FLOOR - 0.01)])
        lookup = await look_up_kb_question(deps, _case(), LOOKUP_QUESTION)
        assert lookup.status is KbLookupStatus.NOTHING
        assert lookup.entries == ()

    async def test_no_hits_is_nothing_found(self):
        lookup = await look_up_kb_question(_deps([]), _case(), LOOKUP_QUESTION)
        assert lookup.status is KbLookupStatus.NOTHING
        assert lookup.query == "kafka consumer lag"

    async def test_shown_entries_are_capped_and_diverse(self):
        hits = [_hit("rb_a", 0.7 - i / 100) for i in range(4)] + [
            _hit("rb_b", 0.6),
            _hit("rb_c", 0.59),
        ]
        lookup = await look_up_kb_question(_deps(hits), _case(), LOOKUP_QUESTION)
        parents = [e["parent_document_id"] for e in lookup.entries]
        assert len(parents) == KB_QUESTION_MAX_ENTRIES
        assert parents.count("rb_a") == 2
        assert "rb_b" in parents
        assert lookup.other_titles == ("Runbook rb_c",)

    async def test_an_excerpt_is_the_section_text_bounded(self):
        long_text = "## Symptom Recognition\n\n" + "lag " * 1000
        lookup = await look_up_kb_question(
            _deps([_hit("rb_a", 0.7, text=long_text)]), _case(), LOOKUP_QUESTION
        )
        (entry,) = lookup.entries
        assert entry["section"] == "Symptom Recognition"
        assert entry["summary"].startswith("## Symptom Recognition")
        assert len(entry["summary"]) <= KB_QUESTION_EXCERPT_CHARS + 20
        assert entry["summary"].endswith("[excerpt cut]")

    async def test_no_topic_searches_with_the_problem_statement(self):
        deps = _deps([])
        lookup = await look_up_kb_question(
            deps, _case(), {"kind": "lookup", "topic": ""}
        )
        assert lookup.query_source == "problem_statement"
        assert (
            deps.knowledge_service.search_knowledge.await_args.kwargs["query"]
            == "consumers fall behind on the orders topic"
        )

    async def test_no_topic_and_no_statement_searches_nothing(self):
        deps = _deps([_hit("rb_a", 0.9)])
        lookup = await look_up_kb_question(
            deps, _case(description=""), {"kind": "lookup", "topic": ""}
        )
        assert lookup.status is KbLookupStatus.NO_TOPIC
        deps.knowledge_service.search_knowledge.assert_not_awaited()

    async def test_a_listing_question_searches_nothing(self):
        deps = _deps([_hit("rb_a", 0.9)])
        lookup = await look_up_kb_question(
            deps, _case(), {"kind": "listing", "topic": ""}
        )
        assert lookup.status is KbLookupStatus.LISTING
        deps.knowledge_service.search_knowledge.assert_not_awaited()

    async def test_a_failed_search_is_unavailable_not_nothing(self):
        lookup = await look_up_kb_question(
            _deps(raises=RuntimeError("chroma down")), _case(), LOOKUP_QUESTION
        )
        assert lookup.status is KbLookupStatus.UNAVAILABLE

    async def test_a_failed_team_lookup_is_unavailable_not_a_narrowed_search(self):
        """A narrowed scope would back a "not found" it did not establish."""
        deps = _deps([], team_raises=True)
        lookup = await look_up_kb_question(deps, _case(), LOOKUP_QUESTION)
        assert lookup.status is KbLookupStatus.UNAVAILABLE
        deps.knowledge_service.search_knowledge.assert_not_awaited()

    async def test_no_knowledge_service_is_unavailable(self):
        deps = SimpleNamespace(
            knowledge_service=None, team_service=None, share_repository=None
        )
        lookup = await look_up_kb_question(deps, _case(), LOOKUP_QUESTION)
        assert lookup.status is KbLookupStatus.UNAVAILABLE

    async def test_the_push_flag_does_not_switch_it_off(self, monkeypatch):
        """``KB_PREFETCH_ENABLED`` governs the push; this is a read the user
        asked for."""
        from tests.utils import reset_settings_singleton

        monkeypatch.setenv("KB_PREFETCH_ENABLED", "false")
        reset_settings_singleton()
        try:
            deps = _deps([_hit("rb_a", 0.7)])
            lookup = await look_up_kb_question(deps, _case(), LOOKUP_QUESTION)
        finally:
            monkeypatch.delenv("KB_PREFETCH_ENABLED", raising=False)
            reset_settings_singleton()
        assert lookup.status is KbLookupStatus.CANDIDATES
        deps.knowledge_service.search_knowledge.assert_awaited_once()


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


def _render(lookup: KbLookup) -> str:
    return render_kb_lookup_block(lookup, PromptFence(mint_token()))


def _candidates() -> KbLookup:
    return KbLookup(
        status=KbLookupStatus.CANDIDATES,
        query="kafka consumer lag",
        query_source="question",
        entries=(
            {
                "title": "Kafka Consumer Lag",
                "summary": "Check partition assignment before scaling consumers.",
                "trigger": KB_QUESTION_TRIGGER,
                "parent_document_id": "rb_kafka",
            },
        ),
        other_titles=("Kafka Producer Timeout",),
    )


class TestTheBlock:
    def test_no_lookup_renders_nothing(self):
        assert render_kb_lookup_block(None, PromptFence(mint_token())) == ""

    def test_candidates_carry_the_runbooks_and_the_relevance_rule(self):
        block = _render(_candidates())
        assert block.startswith(KB_LOOKUP_HEADING)
        assert "Kafka Consumer Lag" in block
        assert "Check partition assignment" in block
        assert "Kafka Producer Timeout" in block
        assert "only if its title or" in block
        assert "couldn't find a runbook" in block
        assert "general guidance" in block

    def test_the_query_and_runbook_text_are_fenced(self):
        fence = PromptFence(mint_token())
        block = render_kb_lookup_block(_candidates(), fence)
        assert f"<kb_runbook_excerpt{fence.attr}>" in block
        assert f"<kb_lookup_query{fence.attr}>" in block
        assert "Check partition assignment before scaling consumers." in fence.seen

    def test_nothing_found_is_said_first_and_plainly(self):
        block = _render(
            KbLookup(status=KbLookupStatus.NOTHING, query="SAP HANA backups")
        )
        assert "couldn't find a runbook" in block
        assert "Never suggest that a runbook exists" in block
        assert "CANDIDATE" not in block

    def test_unavailable_never_claims_nothing_exists(self):
        block = _render(KbLookup(status=KbLookupStatus.UNAVAILABLE, query="x"))
        assert "could NOT run" in block
        assert "Do NOT say no runbook exists" in block
        assert "couldn't find a runbook" not in block

    def test_a_problem_statement_query_is_named_as_such(self):
        block = _render(
            KbLookup(
                status=KbLookupStatus.NOTHING,
                query="disk full",
                query_source="problem_statement",
            )
        )
        assert "problem statement" in block

    def test_no_topic_asks(self):
        block = _render(KbLookup(status=KbLookupStatus.NO_TOPIC))
        assert "Ask which topic" in block
        assert "kb_lookup_query" not in block

    def test_listing_points_to_the_dashboard_and_lists_nothing(self):
        block = _render(KbLookup(status=KbLookupStatus.LISTING))
        assert "Dashboard" in block
        assert "Never list runbook titles" in block


# ---------------------------------------------------------------------------
# The prompts
# ---------------------------------------------------------------------------


def _inquiry():
    return inquiry_case(
        case_id="case_aabb11223344",
        title="Kafka lag",
        description="consumers falling behind",
        enterprise_id="ent_1",
        current_turn=1,
    )


def _investigating(kb_context=None):
    return investigating_case(
        case_id="case_5db5417fe445",
        title="Kafka lag",
        user_id="user_test",
        enterprise_id="org_test",
        description="consumers falling behind",
        symptom="consumers falling behind",
        temporal_state="ongoing",
        urgency="high",
        confirm_after_build=True,
        statement="consumers falling behind",
        current_turn=7,
        kb_context=kb_context,
    )


_PUSHED = {
    "title": "Pushed runbook",
    "summary": "pushed excerpt",
    "score": 0.7,
    "type": "runbook",
    "parent_document_id": "rb_pushed",
    "trigger": "symptom",
}


class TestThePrompts:
    def test_inquiry_renders_the_block_before_the_message(self):
        prompt = get_prompt_for_case(
            _inquiry(), "is there a runbook for kafka lag?", kb_lookup=_candidates()
        )
        assert KB_LOOKUP_HEADING in prompt
        assert prompt.index(KB_LOOKUP_HEADING) < prompt.index("CURRENT USER MESSAGE:")

    def test_inquiry_without_a_lookup_has_no_block(self):
        prompt = get_prompt_for_case(_inquiry(), "hello there")
        assert "CURRENT USER MESSAGE:" in prompt  # positive control
        assert f"{KB_LOOKUP_HEADING} (" not in prompt

    def test_investigating_renders_it_below_the_cache_boundary(self):
        prompt = get_prompt_for_case(
            _investigating(),
            "is there a runbook for kafka lag?",
            kb_lookup=_candidates(),
        )
        assert CACHE_BOUNDARY in prompt
        assert prompt.index(CACHE_BOUNDARY) < prompt.index(f"{KB_LOOKUP_HEADING} (")

    def test_the_cached_prefix_is_unchanged_by_a_lookup(self):
        without = get_prompt_for_case(_investigating(), "is there a runbook?")
        with_lookup = get_prompt_for_case(
            _investigating(), "is there a runbook?", kb_lookup=_candidates()
        )
        assert without.split(CACHE_BOUNDARY)[0] == with_lookup.split(CACHE_BOUNDARY)[0]

    def test_terminal_renders_it(self):
        from tests.unit.core.investigation.test_terminal_qa_tools_1964 import (
            terminal_case,
        )

        case = terminal_case(CaseState.CLOSED)
        prompt = get_prompt_for_case(
            case, "is there a runbook for kafka lag?", kb_lookup=_candidates()
        )
        assert "This investigation is complete" in prompt  # the terminal template
        assert KB_LOOKUP_HEADING in prompt

    def test_the_no_mention_rules_name_their_exception(self):
        prompt = get_prompt_for_case(_inquiry(), "how do I rotate certs?")
        assert "KNOWLEDGE BASE LOOKUP section, when present" in prompt


class TestWhatThePromptReportsItCarried:
    def test_investigating_reports_push_then_lookup_entries(self):
        rendered: list = []
        get_prompt_for_case(
            _investigating(kb_context=[_PUSHED]),
            "is there a runbook?",
            kb_lookup=_candidates(),
            kb_rendered=rendered,
        )
        assert [e["parent_document_id"] for e in rendered] == ["rb_pushed", "rb_kafka"]

    def test_inquiry_reports_only_the_lookup_entries(self):
        """INQUIRY has no push slot; it does have the lookup's."""
        case = _inquiry()
        case.kb_context = [_PUSHED]
        rendered: list = []
        get_prompt_for_case(
            case, "is there a runbook?", kb_lookup=_candidates(), kb_rendered=rendered
        )
        assert [e["parent_document_id"] for e in rendered] == ["rb_kafka"]


# ---------------------------------------------------------------------------
# The turn's sources
# ---------------------------------------------------------------------------


def _row(turn, sources=None):
    metadata = {}
    if sources is not None:
        metadata[MESSAGE_METADATA_KB_SOURCES] = sources
    return {"role": "assistant", "turn_number": turn, "metadata": metadata}


def _published(*entries):
    return [s.model_dump(mode="json") for s in _kb_sources(list(entries))]


def _flags(turn_meta):
    return {
        (s["metadata"]["document_id"], s["metadata"]["trigger"]): s["new_this_turn"]
        for s in turn_meta[MESSAGE_METADATA_KB_SOURCES]
    }


_ASKED = dict(_candidates().entries[0])


class TestTheTurnsSources:
    def test_a_lookup_entry_is_cited_as_a_question_and_always_new(self):
        earlier = [_row(2, _published(_ASKED))]
        turn_meta = {TURN_METADATA_KB_PROMPTED: [_ASKED]}
        _record_turn_kb_sources(turn_meta, SimpleNamespace(messages=earlier))
        assert _flags(turn_meta) == {("rb_kafka", "question"): True}

    def test_a_question_turn_does_not_make_standing_push_context_new(self):
        """Turn 3 asked a question; turn 4 carries the same push as turn 2."""
        earlier = [
            _row(2, _published(_PUSHED)),
            _row(3, _published(_PUSHED, _ASKED)),
        ]
        turn_meta = {TURN_METADATA_KB_PROMPTED: [_PUSHED]}
        _record_turn_kb_sources(turn_meta, SimpleNamespace(messages=earlier))
        assert _flags(turn_meta) == {("rb_pushed", "symptom"): False}

    def test_a_row_with_only_question_sources_is_skipped_for_the_push(self):
        earlier = [_row(2, _published(_PUSHED)), _row(3, _published(_ASKED))]
        turn_meta = {TURN_METADATA_KB_PROMPTED: [_PUSHED]}
        _record_turn_kb_sources(turn_meta, SimpleNamespace(messages=earlier))
        assert _flags(turn_meta) == {("rb_pushed", "symptom"): False}


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def _engine(response, results):
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(return_value=response)
    engine.deps.knowledge_service = _deps(results).knowledge_service
    engine.deps.team_service = None
    engine.deps.share_repository = None
    return engine


def _asked(topic="kafka consumer lag"):
    return {
        "query_mode": "directed_analysis",
        KB_QUESTION_INTENT_KEY: {"kind": "lookup", "topic": topic},
    }


class TestTheEngine:
    async def test_a_question_reaches_the_single_shot_prompt(self):
        """A case with no files takes the tool-less route; the lookup needs no
        tool, so the block is there anyway."""
        engine = _engine(
            InvestigationResponse_Diagnosis(agent_response="ok", state_updates={}),
            [_hit("rb_kafka", 0.68, title="Kafka Consumer Lag")],
        )
        result = await engine.process_turn(
            case=_investigating(),
            user_message="is there a runbook for kafka consumer lag?",
            intent_data=_asked(),
            redaction_ctx=None,
        )
        call = engine.generator.generate_structured_output.await_args
        assert "investigation_tools" not in call.kwargs  # the single-shot route
        prompt = call.args[0]
        assert KB_LOOKUP_HEADING in prompt
        assert "Kafka Consumer Lag" in prompt
        captured = result["metadata"][TURN_METADATA_KB_PROMPTED]
        assert [e["trigger"] for e in captured] == [KB_QUESTION_TRIGGER]

    async def test_no_question_means_no_search(self):
        engine = _engine(
            InvestigationResponse_Diagnosis(agent_response="ok", state_updates={}),
            [_hit("rb_kafka", 0.9)],
        )
        await engine.process_turn(
            case=_investigating(),
            user_message="the consumers are still behind",
            intent_data={"query_mode": "directed_analysis"},
            redaction_ctx=None,
        )
        engine.deps.knowledge_service.search_knowledge.assert_not_awaited()
        prompt = engine.generator.generate_structured_output.await_args.args[0]
        assert f"{KB_LOOKUP_HEADING} (" not in prompt

    async def test_inquiry_reports_a_miss(self):
        engine = _engine(InquiryResponse(agent_response="ok", state_updates={}), [])
        await engine.process_turn(
            case=_inquiry(),
            user_message="is there a runbook for SAP HANA backups?",
            intent_data=_asked("SAP HANA backups"),
            redaction_ctx=None,
        )
        prompt = engine.generator.generate_structured_output.await_args.args[0]
        assert "couldn't find a runbook" in prompt
        assert "SAP HANA backups" in prompt


class TestTheTerminalPath:
    async def test_a_question_on_a_closed_case_is_looked_up_and_cited(self):
        from faultmaven.core.investigation.schemas import TerminalResponse
        from tests.unit.core.investigation.test_terminal_qa_tools_1964 import (
            FakeRegistry,
            ScriptedProvider,
            terminal_case,
            terminal_engine,
        )

        engine, _ = terminal_engine(ScriptedProvider(), FakeRegistry())
        engine.generator.generate_structured_output = AsyncMock(
            return_value=TerminalResponse(agent_response="ok", state_updates={})
        )
        engine.deps.knowledge_service = _deps(
            [_hit("rb_kafka", 0.68, title="Kafka Consumer Lag")]
        ).knowledge_service
        engine.deps.team_service = None
        engine.deps.share_repository = None

        result = await engine.process_turn(
            case=terminal_case(CaseState.CLOSED),
            user_message="is there a runbook for kafka consumer lag?",
            intent_data=_asked(),
            redaction_ctx=None,
        )

        prompt = engine.generator.generate_structured_output.await_args.args[0]
        assert "This investigation is complete" in prompt  # the terminal template
        assert KB_LOOKUP_HEADING in prompt
        captured = result["metadata"][TURN_METADATA_KB_PROMPTED]
        assert [e["parent_document_id"] for e in captured] == ["rb_kafka"]


# ---------------------------------------------------------------------------
# Review round (#2797)
# ---------------------------------------------------------------------------


class TestWhatAQuestionWithNoTopicSearches:
    async def test_inquiry_searches_the_proposed_statement(self):
        """``description`` is empty throughout INQUIRY."""
        case = _case(description="")
        case.inquiry = SimpleNamespace(proposed_problem_statement="nginx 502s")
        deps = _deps([])
        lookup = await look_up_kb_question(deps, case, {"kind": "lookup", "topic": ""})
        assert lookup.query_source == "problem_statement"
        assert (
            deps.knowledge_service.search_knowledge.await_args.kwargs["query"]
            == "nginx 502s"
        )


class TestASearchThatCouldNotRun:
    async def test_a_service_with_no_store_raises_rather_than_answering_empty(self):
        from faultmaven.models.exceptions import KnowledgeBaseError
        from faultmaven.modules.knowledge.domain.services.knowledge_service import (
            KnowledgeService,
        )

        service = KnowledgeService.__new__(KnowledgeService)
        service._vector_store = None
        service._sanitizer = SimpleNamespace(asanitize=AsyncMock(return_value="q"))
        service._tracer = SimpleNamespace(trace=lambda *_: _NullContext())
        with pytest.raises(KnowledgeBaseError):
            await service.search_knowledge("q")

    async def test_teams_with_no_share_repository_cannot_narrow_the_scope(self):
        deps = _deps([_hit("rb_a", 0.9)])
        deps.team_service = MagicMock()
        deps.share_repository = None
        lookup = await look_up_kb_question(deps, _case(), LOOKUP_QUESTION)
        assert lookup.status is KbLookupStatus.UNAVAILABLE
        deps.knowledge_service.search_knowledge.assert_not_awaited()


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestAQuestionNoServiceRecorded:
    def test_the_engine_classifies_the_message_itself(self):
        """A typed intent's handler builds its own intent_data, with no key."""
        question = MilestoneEngine._kb_question(
            {"value": True}, "go ahead — is there a runbook for DNS failover?"
        )
        assert question == {"kind": "lookup", "topic": "DNS failover"}

    def test_a_recorded_none_is_respected(self):
        question = MilestoneEngine._kb_question(
            {KB_QUESTION_INTENT_KEY: None}, "is there a runbook for DNS failover?"
        )
        assert question is None


def test_the_block_is_counted_with_the_reserved_sections():
    """The starvation check subtracts the reserve; an uncounted reserved
    block overstates the room left for the variable sections."""
    from faultmaven.core.investigation.prompts.templates.assembly import _RESERVE_KEYS

    assert "kb_lookup" in _RESERVE_KEYS
