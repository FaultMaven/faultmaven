"""INQUIRY's symptom search, run before the prompt is built.

The bar: an INQUIRY reply on a case with no files has the runbooks matched to
the reported symptom in front of it (it has no ``kb_qa``, and the pre-fetch
otherwise fires only when the case leaves INQUIRY), the turn cites them, and
the knowledge-base check step reads them. A later message that is not a
description of the symptom never clears what the description found. Every
"no search" assertion has a positive control that the same set-up searches.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.kb_push import TURN_METADATA_KB_PROMPTED
from faultmaven.core.investigation.kb_question import KB_QUESTION_INTENT_KEY
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.kb_prefetch import (
    INQUIRY_QUERY_MAX_CHARS,
    KbPrefetcher,
    inquiry_symptom_query,
)
from faultmaven.core.investigation.prompts.templates.assembly import get_prompt_for_case
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
)
from faultmaven.models.common import SearchResult
from tests.case_builders import inquiry_case
from tests.unit.core.investigation.test_kb_question_lookup import _investigating

pytestmark = pytest.mark.unit

#: The block itself, not the instructions that name it.
BLOCK = "<knowledge_context>\nThe following runbooks matched"


def _hit(parent: str = "rb_pg", score: float = 0.7):
    return SearchResult(
        document_id=f"{parent}_chunk_0",
        title="PostgreSQL Connection Pool Exhaustion",
        document_type="runbook",
        tags=[],
        score=score,
        snippet="Check max_connections and the pool size...",
        content="Check max_connections and the pool size.",
        parent_document_id=parent,
    )


def _case(statement: str | None = None, kb_context=None):
    case = inquiry_case(
        case_id="case_aabb11223344",
        title="New case",
        description="",
        enterprise_id="ent_1",
        current_turn=2,
        kb_context=kb_context,
    )
    if statement is not None:
        case.inquiry.proposed_problem_statement = statement
    return case


def _service(results=None):
    service = MagicMock()
    service.search_knowledge = AsyncMock(return_value=list(results or []))
    return service


def _prefetcher(service):
    deps = MagicMock()
    deps.knowledge_service = service
    deps.team_service = None
    deps.share_repository = None
    return KbPrefetcher(deps=deps)


def _queries(service) -> list[str]:
    return [c.kwargs["query"] for c in service.search_knowledge.await_args_list]


# ---------------------------------------------------------------------------
# What is searched
# ---------------------------------------------------------------------------


class TestTheQuery:
    def test_the_proposed_statement_is_the_symptom(self):
        case = _case(statement="Checkout API exhausts its Postgres connections")
        assert inquiry_symptom_query(case, "it started at 10am") == (
            "Checkout API exhausts its Postgres connections"
        )

    def test_before_a_statement_the_message_is(self):
        assert inquiry_symptom_query(_case(), "  postgres\n out of  connections ") == (
            "postgres out of connections"
        )

    def test_a_long_query_is_bounded(self):
        query = inquiry_symptom_query(_case(), "word " * 200)
        assert len(query) <= INQUIRY_QUERY_MAX_CHARS

    def test_a_later_message_is_not_searched_over_what_was_found(self):
        case = _case(kb_context=[{"title": "t", "parent_document_id": "rb"}])
        assert inquiry_symptom_query(case, "it started at 10am") == ""
        # Positive control: with nothing found yet, the same message is.
        assert inquiry_symptom_query(_case(), "it started at 10am")


class TestThePrefetch:
    async def test_it_searches_and_stores_the_symptoms_runbooks(self):
        service = _service([_hit()])
        case = _case()
        await _prefetcher(service).prefetch_for_inquiry(case, "postgres pool full")
        assert _queries(service) == ["postgres pool full"]
        assert [e["parent_document_id"] for e in case.kb_context] == ["rb_pg"]
        assert case.kb_context_origin["query"] == "postgres pool full"

    async def test_context_already_fetched_for_the_statement_is_reused(self):
        service = _service([_hit()])
        case = _case(statement="pool exhausted")
        prefetcher = _prefetcher(service)
        await prefetcher.prefetch_for_inquiry(case, "first")
        await prefetcher.prefetch_for_inquiry(case, "second")
        assert _queries(service) == ["pool exhausted"]

    async def test_a_revised_statement_is_searched_again(self):
        service = _service([_hit()])
        case = _case(statement="pool exhausted")
        prefetcher = _prefetcher(service)
        await prefetcher.prefetch_for_inquiry(case, "m")
        case.inquiry.proposed_problem_statement = "replica lag on the orders db"
        await prefetcher.prefetch_for_inquiry(case, "m")
        assert _queries(service) == ["pool exhausted", "replica lag on the orders db"]

    async def test_a_follow_up_message_keeps_what_the_description_found(self):
        service = _service([_hit()])
        case = _case()
        prefetcher = _prefetcher(service)
        await prefetcher.prefetch_for_inquiry(case, "postgres pool full")
        service.search_knowledge.return_value = []  # it would miss
        await prefetcher.prefetch_for_inquiry(case, "it started at 10am")
        assert _queries(service) == ["postgres pool full"]
        assert case.kb_context

    async def test_the_push_switch_turns_it_off(self, monkeypatch):
        from faultmaven.config.settings import get_settings

        monkeypatch.setattr(
            get_settings().knowledge, "kb_prefetch_enabled", False, raising=False
        )
        service = _service([_hit()])
        case = _case()
        await _prefetcher(service).prefetch_for_inquiry(case, "postgres pool full")
        service.search_knowledge.assert_not_awaited()
        assert not case.kb_context


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------


_PG = {
    "title": "PostgreSQL Connection Pool Exhaustion",
    "summary": "Check max_connections and the pool size...",
    "score": 0.7,
    "type": "runbook",
    "parent_document_id": "rb_pg",
    "trigger": "symptom",
}


class TestThePrompt:
    def test_inquiry_renders_the_runbooks_before_the_message(self):
        rendered: list = []
        prompt = get_prompt_for_case(
            _case(kb_context=[_PG]), "postgres pool full", kb_rendered=rendered
        )
        assert BLOCK in prompt
        assert "PostgreSQL Connection Pool Exhaustion" in prompt
        assert prompt.index(BLOCK) < prompt.index("CURRENT USER MESSAGE:")
        assert [e["parent_document_id"] for e in rendered] == ["rb_pg"]

    def test_no_runbooks_renders_no_block(self):
        prompt = get_prompt_for_case(_case(), "postgres pool full")
        assert BLOCK not in prompt
        assert "KNOWLEDGE BASE CHECK" in prompt  # control: the INQUIRY template

    def test_the_check_step_reads_the_block_and_asks_for_no_match_field(self):
        prompt = get_prompt_for_case(_case(kb_context=[_PG]), "postgres pool full")
        step = prompt[prompt.index("KNOWLEDGE BASE CHECK") :]
        step = step[: step.index("3. URGENCY CLASSIFICATION")]
        assert "<knowledge_context>" in step
        assert "without describing the fix" in step
        assert "Never tell the user a lookup found" in step
        assert "kb_qa" not in step
        assert "knowledge_match" not in prompt
        assert "knowledge_match" not in InquiryResponse.model_json_schema().__repr__()


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def _engine(response, results):
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(return_value=response)
    engine.deps.knowledge_service = _service(results)
    engine.deps.team_service = None
    engine.deps.share_repository = None
    return engine


def _inquiry_reply():
    return InquiryResponse(agent_response="ok", state_updates={})


class TestTheEngine:
    async def test_an_inquiry_turn_has_the_symptoms_runbooks_in_its_prompt(self):
        engine = _engine(_inquiry_reply(), [_hit()])
        result = await engine.process_turn(
            case=_case(),
            user_message="our checkout API ran out of postgres connections",
            intent_data={"query_mode": "triage"},
            redaction_ctx=None,
        )
        prompt = engine.generator.generate_structured_output.await_args.args[0]
        assert BLOCK in prompt
        assert "PostgreSQL Connection Pool Exhaustion" in prompt
        reported = result["metadata"][TURN_METADATA_KB_PROMPTED]
        assert [(e["parent_document_id"], e["trigger"]) for e in reported] == [
            ("rb_pg", "symptom")
        ]

    async def test_a_question_about_the_assistant_searches_nothing(self):
        engine = _engine(_inquiry_reply(), [_hit()])
        await engine.process_turn(
            case=_case(),
            user_message="how do you decide what to ask me?",
            intent_data={"query_mode": "agent_meta"},
            redaction_ctx=None,
        )
        engine.deps.knowledge_service.search_knowledge.assert_not_awaited()

    async def test_a_question_about_the_knowledge_base_is_the_lookups_alone(self):
        engine = _engine(_inquiry_reply(), [_hit()])
        await engine.process_turn(
            case=_case(),
            user_message="is there a runbook for postgres connection exhaustion?",
            intent_data={
                "query_mode": "knowledge_query",
                KB_QUESTION_INTENT_KEY: {
                    "kind": "lookup",
                    "topic": "postgres connection exhaustion",
                },
            },
            redaction_ctx=None,
        )
        # One search: the lookup's, for the topic; no symptom search beside it.
        assert _queries(engine.deps.knowledge_service) == [
            "postgres connection exhaustion"
        ]

    async def test_an_investigating_turn_does_not_run_it(self):
        engine = _engine(
            InvestigationResponse_Diagnosis(agent_response="ok", state_updates={}),
            [_hit()],
        )
        await engine.process_turn(
            case=_investigating(),
            user_message="the pool is still full",
            intent_data={"query_mode": "triage"},
            redaction_ctx=None,
        )
        engine.deps.knowledge_service.search_knowledge.assert_not_awaited()
