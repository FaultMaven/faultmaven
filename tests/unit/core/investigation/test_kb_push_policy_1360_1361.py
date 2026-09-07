"""The KB PUSH channel: its policy gate (fm#1360) and its identity (fm#1361).

Two issues, one seam. Knowledge reaches the model two ways and only one of them
adapted. The PULL channel is the ``kb_qa`` directed-analysis tool, elected by
the model, priced by the model's own judgement. The PUSH channel is
``MilestoneEngine._prefetch_kb_context``: a deterministic hybrid search fired at
two case transitions whose admitted hits are written to ``case.kb_context`` and
rendered into the prompt as ``<knowledge_context>``. It had no control at all,
and it reported a count and nothing else.

The bar these tests are written to is deliberately narrow: **the push must be
absent from the RENDERED PROMPT when disabled**. A test that asserts the flag
was read proves nothing — the flag could be read into a variable nobody
consults, which is close to what the channel already was.

Every prompt assertion therefore carries a positive control. A prompt builder
that raised, or returned the minimal fallback, would satisfy "the runbook is
absent" for entirely the wrong reason, and a silent failure that reads as a pass
is the failure mode this file most needs to avoid.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.case_telemetry import (
    FIELD_ALLOWLIST,
    TurnPath,
    build_case_turn_event,
)
from faultmaven.core.investigation.prompts.templates import get_prompt_for_case
from tests.utils import reset_settings_singleton

#: The one entry every prompt assertion is written against. Title, excerpt and
#: id are distinct strings so a test cannot pass on the wrong one, and none of
#: them occurs anywhere else in an assembled prompt.
RUNBOOK_TITLE = "ENOSPC triage on a full root volume"
RUNBOOK_EXCERPT = "Look for deleted-but-open descriptors with lsof +L1"
RUNBOOK_ID = "rb_enospc_triage"

#: Two hits, ordered so that the FIRST is not the highest-scoring one. The
#: pre-fetch orders by the reranker's blend and floors on the raw cosine, so
#: "top score" and "first hit" are genuinely different quantities — an
#: implementation that reported ``entries[0]["score"]`` or the mean would pass a
#: single-entry fixture and fail here.
TWO_HITS = [
    {
        "title": RUNBOOK_TITLE,
        "summary": RUNBOOK_EXCERPT,
        "score": 0.62,
        "type": "runbook",
        "parent_document_id": RUNBOOK_ID,
        "trigger": "symptom",
    },
    {
        "title": "Kafka consumer lag runbook",
        "summary": "Check partition assignment before scaling consumers",
        "score": 0.88,
        "type": "runbook",
        "parent_document_id": "rb_kafka_lag",
        "trigger": "root_cause",
    },
]


def _case(kb_context=None):
    from tests.unit.core.investigation.test_solution_offer_liveness import _make_case

    case = _make_case()
    case.kb_context = kb_context
    return case


def _engine(knowledge_service=None):
    from tests.unit.core.investigation.test_solution_offer_liveness import _make_engine

    engine = _make_engine()
    engine.knowledge_service = knowledge_service
    engine.runbook_kb = None
    return engine


@pytest.fixture
def push(monkeypatch):
    """Set ``KB_PREFETCH_ENABLED`` through the environment and rebuild settings.

    Through the ENV, not by poking the settings object: the environment is what
    the field's ``validation_alias`` actually binds to, so this exercises the
    same path a deployment does. The singleton is cleared on both sides —
    leaving a doctored settings object behind would silently re-configure every
    test that runs after this module in the same process.
    """

    def _set(enabled: bool):
        monkeypatch.setenv("KB_PREFETCH_ENABLED", "true" if enabled else "false")
        reset_settings_singleton()

    reset_settings_singleton()
    yield _set
    monkeypatch.delenv("KB_PREFETCH_ENABLED", raising=False)
    reset_settings_singleton()


class TestTheKnobIsReallyBound:
    """Before anything else: the env var reaches the field it claims to."""

    def test_the_env_var_moves_the_setting(self, push):
        from tests.utils import get_live_settings

        push(False)
        assert get_live_settings().knowledge.kb_prefetch_enabled is False
        push(True)
        assert get_live_settings().knowledge.kb_prefetch_enabled is True

    def test_the_shipped_default_is_on(self, monkeypatch):
        """The push is the status quo; the flag is the way to turn it OFF."""
        monkeypatch.delenv("KB_PREFETCH_ENABLED", raising=False)
        reset_settings_singleton()
        try:
            from tests.utils import get_live_settings

            assert get_live_settings().knowledge.kb_prefetch_enabled is True
        finally:
            reset_settings_singleton()


class TestTheRenderedPrompt:
    """fm#1360's acceptance bar, asserted on the assembled prompt string."""

    def test_the_runbook_is_in_the_prompt_when_the_push_is_on(self, push):
        push(True)
        prompt = get_prompt_for_case(
            _case(TWO_HITS),
            "the volume filled again overnight",
            provider_name="openai",
            model_name="gpt-4o",
        )
        assert "<knowledge_context>" in prompt
        assert RUNBOOK_TITLE in prompt
        assert RUNBOOK_EXCERPT in prompt

    def test_the_runbook_is_absent_from_the_prompt_when_the_push_is_off(self, push):
        """The claim the flag makes, stated as bytes that are not in the prompt.

        The positive control is load-bearing: without it a prompt builder that
        blew up and returned the minimal fallback would pass every negative
        assertion below while proving nothing.
        """
        push(False)
        message = "the volume filled again overnight"
        prompt = get_prompt_for_case(
            _case(TWO_HITS),
            message,
            provider_name="openai",
            model_name="gpt-4o",
        )
        assert message in prompt, "positive control: the prompt was really built"
        assert "<knowledge_context>" not in prompt
        assert RUNBOOK_TITLE not in prompt
        assert RUNBOOK_EXCERPT not in prompt
        assert RUNBOOK_ID not in prompt

    def test_the_two_prompts_differ_by_the_block_and_nothing_else_matters(self, push):
        """Same case, same message, both settings — the ON prompt is strictly
        longer, so "absent" is a removal rather than a differently-built
        prompt."""
        case_on, case_off = _case(TWO_HITS), _case(TWO_HITS)
        push(True)
        on = get_prompt_for_case(
            case_on, "same words", provider_name="openai", model_name="gpt-4o"
        )
        push(False)
        off = get_prompt_for_case(
            case_off, "same words", provider_name="openai", model_name="gpt-4o"
        )
        assert on != off
        assert len(on) > len(off)
        assert RUNBOOK_TITLE in on and RUNBOOK_TITLE not in off

    def test_a_case_reloaded_with_context_already_on_it_still_renders_nothing(
        self, push
    ):
        """The reason the gate lives in the context builder and not only in the
        pre-fetch.

        ``case.kb_context`` is persisted, so a case that accumulated runbooks
        while the push was enabled arrives at the next turn carrying them. A
        producer-side guard alone never runs on that turn — the triggers fire at
        transitions, not every turn — and the block would keep rendering after
        the operator turned the push off.
        """
        push(False)
        reloaded = _case(TWO_HITS)  # exactly the shape repository.get() returns
        prompt = get_prompt_for_case(
            reloaded, "anything", provider_name="openai", model_name="gpt-4o"
        )
        assert "anything" in prompt
        assert "<knowledge_context>" not in prompt


class TestThePreFetchItself:
    """The producer half: off means the search does not run."""

    @pytest.mark.asyncio
    async def test_disabled_skips_the_search_entirely(self, push):
        """Off is a cost control, not just a rendering one — the hybrid search
        is the expensive half."""
        push(False)
        service = MagicMock()
        service.search_knowledge = AsyncMock(return_value=[])
        await _engine(service)._prefetch_kb_context(_case(), "disk full", "symptom")
        service.search_knowledge.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_clears_context_the_case_arrived_with(self, push):
        """Off has to mean off for the case, not only for new searches.

        Otherwise the turn response and the telemetry keep citing runbooks from
        a search the operator has since disabled.
        """
        push(False)
        case = _case(TWO_HITS)
        await _engine(MagicMock())._prefetch_kb_context(case, "disk full", "symptom")
        assert case.kb_context is None

    @pytest.mark.asyncio
    async def test_enabled_still_searches_and_admits(self, push):
        """The converse, so "disabled skips" cannot pass by the search being
        broken for everyone."""
        push(True)
        hit = SimpleNamespace(
            title=RUNBOOK_TITLE,
            snippet=RUNBOOK_EXCERPT,
            score=0.9,
            document_type="runbook",
            parent_document_id=RUNBOOK_ID,
        )
        service = MagicMock()
        service.search_knowledge = AsyncMock(return_value=[hit])
        case = _case()
        await _engine(service)._prefetch_kb_context(case, "disk full", "symptom")
        service.search_knowledge.assert_awaited_once()
        assert [e["parent_document_id"] for e in case.kb_context] == [RUNBOOK_ID]


class TestThePreFetchLogCarriesIdentity:
    """fm#1361: ``3 matches`` cannot answer which runbook informed an answer."""

    @pytest.mark.asyncio
    async def test_the_log_line_names_the_runbooks_and_their_scores(self, push, caplog):
        push(True)
        hits = [
            SimpleNamespace(
                title=RUNBOOK_TITLE,
                snippet=RUNBOOK_EXCERPT,
                score=0.62,
                document_type="runbook",
                parent_document_id=RUNBOOK_ID,
            ),
            SimpleNamespace(
                title="Kafka consumer lag runbook",
                snippet="Check partition assignment",
                score=0.88,
                document_type="runbook",
                parent_document_id="rb_kafka_lag",
            ),
        ]
        service = MagicMock()
        service.search_knowledge = AsyncMock(return_value=hits)
        with caplog.at_level(
            logging.INFO, logger="faultmaven.core.investigation.milestone_engine"
        ):
            await _engine(service)._prefetch_kb_context(_case(), "disk full", "symptom")

        records = [r for r in caplog.records if "KB pre-fetch" in r.getMessage()]
        assert len(records) == 1, "the pre-fetch logs once per firing"
        message = records[0].getMessage()
        assert RUNBOOK_TITLE in message
        assert RUNBOOK_ID in message
        assert "0.620" in message and "0.880" in message

    @pytest.mark.asyncio
    async def test_a_structured_reader_gets_fields_not_a_sentence(self, push, caplog):
        """Pinned the way a consumer actually reads it.

        The root handler is structlog's ``ProcessorFormatter`` with
        ``ExtraAdder``, so anything passed as ``extra`` lands as a top-level key
        on the JSON line. An aggregator keys on ``kb_runbook_ids``; it does not
        parse the English.

        ``kb_prefetch_top_score`` is asserted against a fixture whose first hit
        is NOT its best one, so reporting ``[0]`` or the mean fails here.
        """
        push(True)
        hits = [
            SimpleNamespace(
                title=RUNBOOK_TITLE,
                snippet=RUNBOOK_EXCERPT,
                score=0.62,
                document_type="runbook",
                parent_document_id=RUNBOOK_ID,
            ),
            SimpleNamespace(
                title="Kafka consumer lag runbook",
                snippet="Check partition assignment",
                score=0.88,
                document_type="runbook",
                parent_document_id="rb_kafka_lag",
            ),
        ]
        service = MagicMock()
        service.search_knowledge = AsyncMock(return_value=hits)
        with caplog.at_level(
            logging.INFO, logger="faultmaven.core.investigation.milestone_engine"
        ):
            await _engine(service)._prefetch_kb_context(_case(), "disk full", "symptom")

        record = [r for r in caplog.records if "KB pre-fetch" in r.getMessage()][0]
        assert getattr(record, "kb_prefetch_hits") == 2
        assert getattr(record, "kb_runbook_ids") == [RUNBOOK_ID, "rb_kafka_lag"]
        assert getattr(record, "kb_prefetch_top_score") == pytest.approx(0.88)
        assert getattr(record, "kb_prefetch_trigger") == "symptom"


class TestCaseTurnTelemetryCarriesRetrieval:
    """fm#1361: the ``case_turn`` allowlist carried no retrieval signal at all."""

    def test_the_row_reports_hits_top_score_and_ids(self):
        event = build_case_turn_event(_case(TWO_HITS), path=TurnPath.LLM)
        assert event["kb_prefetch_hits"] == 2
        assert event["kb_prefetch_top_score"] == pytest.approx(0.88)
        assert event["kb_runbook_ids"] == [RUNBOOK_ID, "rb_kafka_lag"]

    def test_a_case_with_no_push_reports_zero_rather_than_nothing(self):
        """Absent and zero read differently to a rule computed over the stream,
        which is why every other field in this event is normalised too."""
        event = build_case_turn_event(_case(None), path=TurnPath.LLM)
        assert event["kb_prefetch_hits"] == 0
        assert event["kb_prefetch_top_score"] == 0.0
        assert event["kb_runbook_ids"] == []

    def test_titles_never_reach_the_stream(self):
        """The event is content-free by construction and stays that way.

        A runbook title is prose; an id is a token. The natural way to break
        this is to add the titles beside the ids because they read better in a
        dashboard.
        """
        event = build_case_turn_event(_case(TWO_HITS), path=TurnPath.LLM)
        rendered = repr(event)
        assert RUNBOOK_TITLE not in rendered
        assert RUNBOOK_EXCERPT not in rendered

    def test_the_new_fields_are_on_the_allowlist(self):
        """The allowlist is the gate: a field the builder emits but the
        allowlist does not name is dropped with a warning, and the column is
        simply missing downstream."""
        for field in ("kb_prefetch_hits", "kb_prefetch_top_score", "kb_runbook_ids"):
            assert field in FIELD_ALLOWLIST

    def test_a_prose_element_smuggled_into_the_id_list_is_dropped(self):
        """The guard, exercised at the element level.

        Dropping the whole list over one bad member would report "retrieval
        returned nothing" for a turn where retrieval returned two runbooks, so
        the good ids must survive alongside the rejection.
        """
        from faultmaven.core.investigation.case_telemetry import _sanitize

        clean = _sanitize(
            {
                "case_id": "case_1",
                "kb_runbook_ids": ["rb_ok", "a title with spaces", "rb_also_ok"],
            }
        )
        assert clean["kb_runbook_ids"] == ["rb_ok", "rb_also_ok"]

    def test_the_id_list_is_length_capped(self):
        from faultmaven.core.investigation.case_telemetry import (
            _MAX_SEQUENCE_LEN,
            _sanitize,
        )

        clean = _sanitize({"kb_runbook_ids": [f"rb_{i}" for i in range(100)]})
        assert len(clean["kb_runbook_ids"]) == _MAX_SEQUENCE_LEN

    def test_the_push_being_off_reports_zero_even_on_a_case_carrying_context(
        self, push
    ):
        """The hole the first version of this PR shipped.

        Only the prompt was gated. This reader took ``case.kb_context``
        straight off the case, so a deployment with the push disabled rendered
        no ``<knowledge_context>`` block and then reported
        ``kb_prefetch_hits=2`` — the telemetry that exists to make the push's
        cost/benefit measurable said it was active while it was off.

        The case here is the one that actually occurs: context persisted while
        the push was ON, still on the row after it was turned off. The
        pre-fetch's own clearing branch never runs for it — that function is
        edge-triggered at two case transitions, and this case is past both.
        """
        push(False)
        event = build_case_turn_event(_case(TWO_HITS), path=TurnPath.LLM)
        assert event["kb_prefetch_hits"] == 0
        assert event["kb_runbook_ids"] == []
        assert event["kb_prefetch_top_score"] == 0.0

    def test_the_push_being_on_still_reports_the_hits(self, push):
        """Positive control for the test above: the gate must not be a
        permanent zero."""
        push(True)
        event = build_case_turn_event(_case(TWO_HITS), path=TurnPath.LLM)
        assert event["kb_prefetch_hits"] == 2
        assert event["kb_runbook_ids"] == [RUNBOOK_ID, "rb_kafka_lag"]

    def test_hits_may_exceed_ids_and_the_inequality_is_the_contract(self, push):
        """An entry retrieval could not attribute to a parent document.

        The producer writes ``parent_document_id: None`` whenever the search
        result carried none, so this shape is reachable. ``hits`` counts what
        the model was shown; the id list counts what can be cited. Collapsing
        them would either understate the prompt surface or hide that retrieval
        is returning unattributable chunks. The invariant a consumer may rely
        on is the inequality, and it is pinned here so a later "cleanup" that
        equalises them has to argue with this test.
        """
        push(True)
        unattributable = [
            dict(TWO_HITS[0], parent_document_id=None),
            dict(TWO_HITS[1]),
        ]
        event = build_case_turn_event(_case(unattributable), path=TurnPath.LLM)
        assert event["kb_prefetch_hits"] == 2
        assert event["kb_runbook_ids"] == ["rb_kafka_lag"]
        assert len(event["kb_runbook_ids"]) <= event["kb_prefetch_hits"]

    def test_a_string_is_not_treated_as_a_sequence(self):
        """``str`` is a Sequence. If the branch tested for Sequence rather than
        list/tuple, every token-shaped string field in the event would come out
        as a list of characters."""
        from faultmaven.core.investigation.case_telemetry import _sanitize

        clean = _sanitize({"case_state": "investigating"})
        assert clean["case_state"] == "investigating"


class TestTheOffStateIsCoherentAcrossEveryConsumer:
    """``KB_PREFETCH_ENABLED=false`` must mean the same thing everywhere.

    ``case.kb_context`` has three readers — the prompt, the turn response's
    ``sources`` and the ``case_turn`` telemetry — and the first version of
    fm#1360 gated one of them. This is the whole-turn assertion, so a fourth
    reader added later without the gate fails HERE rather than in production.
    """

    def test_no_consumer_sees_a_runbook_when_the_push_is_off(self, push):
        from faultmaven.modules.agent.domain.services.investigation_service import (
            _kb_context_sources,
        )

        push(False)
        case = _case(TWO_HITS)  # exactly what repository.get() returns
        prompt = get_prompt_for_case(
            case, "the volume filled again", provider_name="openai", model_name="gpt-4o"
        )
        event = build_case_turn_event(case, path=TurnPath.LLM)

        assert "the volume filled again" in prompt, "positive control"
        assert "<knowledge_context>" not in prompt
        assert RUNBOOK_ID not in prompt
        assert _kb_context_sources(case) == []
        assert event["kb_prefetch_hits"] == 0
        assert event["kb_runbook_ids"] == []

    def test_every_consumer_sees_the_runbooks_when_the_push_is_on(self, push):
        """The converse, so the test above cannot pass by everything being
        permanently empty."""
        from faultmaven.modules.agent.domain.services.investigation_service import (
            _kb_context_sources,
        )

        push(True)
        case = _case(TWO_HITS)
        prompt = get_prompt_for_case(
            case, "the volume filled again", provider_name="openai", model_name="gpt-4o"
        )
        event = build_case_turn_event(case, path=TurnPath.LLM)

        assert "<knowledge_context>" in prompt
        assert RUNBOOK_TITLE in prompt
        assert len(_kb_context_sources(case)) == 2
        assert event["kb_prefetch_hits"] == 2

    def test_the_shared_helper_is_what_every_consumer_reads(self, push):
        """Pins the mechanism, not just the outcome.

        The three readers agree because they call one function. A reader that
        reimplemented the predicate would pass the outcome tests above today
        and drift the first time the predicate changes.
        """
        import inspect

        from faultmaven.core.investigation import case_telemetry
        from faultmaven.core.investigation.prompts import context_builder
        from faultmaven.modules.agent.domain.services import investigation_service

        for module, func in (
            (context_builder, "build_investigation_context"),
            (case_telemetry, "_kb_retrieval"),
            (investigation_service, "_kb_context_sources"),
        ):
            src = inspect.getsource(getattr(module, func))
            assert "visible_kb_context(" in src, (
                f"{module.__name__}.{func} does not read the push through the "
                "shared gate"
            )


class TestTheTurnResponseCitesItsSources:
    """fm#1361: the Copilot's citation components read ``item.sources``, which
    the backend never emitted, so they were unreachable code."""

    def test_sources_are_built_from_the_pre_fetched_runbooks(self):
        from faultmaven.modules.agent.domain.services.investigation_service import (
            _kb_context_sources,
        )

        sources = _kb_context_sources(_case(TWO_HITS))
        assert [s.metadata["document_id"] for s in sources] == [
            RUNBOOK_ID,
            "rb_kafka_lag",
        ]
        assert sources[0].content == RUNBOOK_EXCERPT
        assert sources[0].confidence == pytest.approx(0.62)
        assert sources[0].metadata["title"] == RUNBOOK_TITLE
        assert sources[0].metadata["trigger"] == "symptom"

    def test_no_pre_fetch_means_no_sources(self):
        from faultmaven.modules.agent.domain.services.investigation_service import (
            _kb_context_sources,
        )

        assert _kb_context_sources(_case(None)) == []

    def test_the_serialized_shape_is_the_one_the_frontend_reads(self):
        """Asserted through ``model_dump``, which is what crosses the wire.

        The Copilot's ``Source`` interface reads ``type``, ``content``,
        ``confidence`` and ``metadata.document_id`` / ``metadata.title``; the
        card keys its "view document" affordance on ``type === 'knowledge_base'``
        specifically, so the enum's WIRE VALUE is part of the contract and not
        an internal name.
        """
        from faultmaven.models.api_models import TurnResponse
        from faultmaven.modules.agent.domain.services.investigation_service import (
            _kb_context_sources,
        )
        from faultmaven.modules.case.domain.models import CaseState

        response = TurnResponse(
            agent_response="…",
            turn_number=3,
            milestones_completed=[],
            case_state=CaseState.INVESTIGATING,
            progress_made=True,
            sources=_kb_context_sources(_case(TWO_HITS)),
        )
        wire = response.model_dump(mode="json")
        assert wire["sources"][0]["type"] == "knowledge_base"
        assert wire["sources"][0]["metadata"]["document_id"] == RUNBOOK_ID
        assert wire["sources"][0]["metadata"]["title"] == RUNBOOK_TITLE
        assert wire["sources"][0]["content"] == RUNBOOK_EXCERPT

    def test_no_sources_are_cited_when_the_push_is_off(self, push):
        """A citation for a runbook the model was never shown is worse than no
        citation: it tells the user, and anyone measuring retrieval quality,
        that knowledge informed an answer it could not have informed.

        The case carries context persisted while the push was ON — the state
        the pre-fetch's edge-triggered clearing branch never reaches.
        """
        from faultmaven.modules.agent.domain.services.investigation_service import (
            _kb_context_sources,
        )

        push(False)
        assert _kb_context_sources(_case(TWO_HITS)) == []

    def test_sources_are_cited_when_the_push_is_on(self, push):
        """Positive control: the gate is not a permanent empty list."""
        from faultmaven.modules.agent.domain.services.investigation_service import (
            _kb_context_sources,
        )

        push(True)
        assert len(_kb_context_sources(_case(TWO_HITS))) == 2

    def test_the_field_defaults_to_empty_rather_than_missing(self):
        """Every existing caller builds a ``TurnResponse`` without it."""
        from faultmaven.models.api_models import TurnResponse
        from faultmaven.modules.case.domain.models import CaseState

        response = TurnResponse(
            agent_response="…",
            turn_number=1,
            milestones_completed=[],
            case_state=CaseState.INQUIRY,
            progress_made=False,
        )
        assert response.sources == []
