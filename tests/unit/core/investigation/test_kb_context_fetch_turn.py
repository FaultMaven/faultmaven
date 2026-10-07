"""Contract 11.2.0 — a turn's ``sources`` is what its prompt carried, and history keeps it.

Both pre-fetch triggers fire while a turn's response is APPLIED, after the answer
was generated, and ``TurnResponse.sources`` used to read ``case.kb_context``
after the turn: the turn listed runbooks its answer never saw. These pin the
pieces that fix that at the source and carry it into history:

- the engine captures the prompt's KB entries BEFORE generation, through the
  same selection the prompt builder renders;
- the save turns that capture into published sources, marks the excerpts the
  previous turn's prompt did not carry, and persists them with the assistant
  row; the turn response reads the same list;
- ``GET .../messages`` publishes them typed, and a stored source that no longer
  validates costs that citation, never the message.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.kb_push import (
    KB_PROMPT_MAX_ENTRIES,
    TURN_METADATA_KB_PROMPTED,
    prompt_kb_entries,
)
from faultmaven.core.investigation.milestone_engine import engine as engine_module
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.models.api_models import IntentType
from faultmaven.models.case_ui import ProblemVerificationData
from faultmaven.modules.agent.domain.services.investigation_service.turn_bookkeeping import (
    _kb_sources,
    _record_turn_kb_sources,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_messages import (
    _save_and_emit_turn,
)
from faultmaven.modules.case.contracts import MESSAGE_METADATA_KB_SOURCES
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.problem import (
    ProblemStatus,
    ProblemVerification,
)
from faultmaven.modules.case.domain.models.progress import InvestigationProgress
from faultmaven.modules.case.domain.services.case_service import CaseService

pytestmark = pytest.mark.unit


def _entry(doc: str, excerpt: str = "Rotate the logs before resizing.") -> dict:
    return {
        "title": f"Runbook {doc}",
        "summary": excerpt,
        "score": 0.7,
        "type": "runbook",
        "parent_document_id": doc,
        "trigger": "symptom",
    }


def _case(*, current_turn: int = 3, kb_context=None, messages=None) -> Case:
    case = Case(
        case_id="case_aabb11223344",
        title="Disk full",
        description="d",
        user_id="u",
        enterprise_id="ent_1",
        state=CaseState.INQUIRY,
        current_turn=current_turn,
    )
    case.kb_context = kb_context
    if messages is not None:
        case.messages = messages
    return case


def _assistant_row(turn: int, kb_sources=None) -> dict:
    metadata = {"progress_made": True}
    if kb_sources is not None:
        metadata[MESSAGE_METADATA_KB_SOURCES] = kb_sources
    return {
        "message_id": f"m-{turn}-a",
        "turn_number": turn,
        "role": "assistant",
        "content": f"answer {turn}",
        "created_at": datetime.now(UTC),
        "metadata": metadata,
    }


def _published(*entries: dict) -> list[dict]:
    return [s.model_dump(mode="json") for s in _kb_sources(list(entries))]


# ---------------------------------------------------------------------------
# The selection the prompt renders and the engine captures
# ---------------------------------------------------------------------------


class TestThePromptSelection:
    def test_it_caps_at_what_the_prompt_renders_and_copies(self):
        entries = [_entry(f"rb{i}") for i in range(KB_PROMPT_MAX_ENTRIES + 2)]
        case = _case(kb_context=entries)

        selected = prompt_kb_entries(case)

        assert [e["parent_document_id"] for e in selected] == [
            f"rb{i}" for i in range(KB_PROMPT_MAX_ENTRIES)
        ]
        selected[0]["title"] = "mutated"
        assert case.kb_context[0]["title"] == "Runbook rb0", "a capture is a copy"

    def test_the_prompt_builder_renders_through_it(self):
        import inspect

        from faultmaven.core.investigation.prompts.context_builder import assembly

        src = inspect.getsource(assembly.build_investigation_context)
        assert "prompt_kb_entries(" in src


def _investigating_case(kb_context) -> Case:
    case = Case(
        case_id="case_5db5417fe445",
        title="Disk full",
        state=CaseState.INQUIRY,
        user_id="user_test",
        enterprise_id="org_test",
        description="disk full on the data volume",
        problem_verification=ProblemVerification(
            symptom_statement="disk full on the data volume",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
    )
    case.inquiry.proposed_problem_statement = "disk full"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(UTC)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    case.current_turn = 7
    case.kb_context = kb_context
    return case


def _generating_engine() -> MilestoneEngine:
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response="Rotate the logs first.", state_updates={}
        )
    )
    return engine


class TestTheEngineCapturesBeforeGeneration:
    async def test_a_pre_fetch_during_application_is_not_this_turns(self, monkeypatch):
        """The Gate-1 and root-cause pre-fetches fire inside response
        application. What they write was not in this turn's prompt."""
        original_apply = engine_module._apply_turn_response

        async def apply_then_prefetch(*args, **kwargs):
            kwargs["case"].kb_context = [_entry("rb_fetched_after_the_answer")]
            return await original_apply(*args, **kwargs)

        monkeypatch.setattr(engine_module, "_apply_turn_response", apply_then_prefetch)
        engine = _generating_engine()
        case = _investigating_case([_entry("rb_in_the_prompt")])

        result = await engine.process_turn(case=case, user_message="still full")

        assert engine.generator.generate_structured_output.called
        captured = result["metadata"][TURN_METADATA_KB_PROMPTED]
        assert [e["parent_document_id"] for e in captured] == ["rb_in_the_prompt"]
        assert case.kb_context[0]["parent_document_id"] == "rb_fetched_after_the_answer"

    async def test_a_turn_with_no_context_captures_none(self):
        engine = _generating_engine()
        result = await engine.process_turn(
            case=_investigating_case(None), user_message="still full"
        )
        assert result["metadata"][TURN_METADATA_KB_PROMPTED] == []


# ---------------------------------------------------------------------------
# What the save records
# ---------------------------------------------------------------------------


def _new_flags(turn_meta: dict) -> dict[str, bool]:
    return {
        s["metadata"]["document_id"]: s["new_this_turn"]
        for s in turn_meta[MESSAGE_METADATA_KB_SOURCES]
    }


class TestTheTurnRecordsWhatItsPromptCarried:
    def test_the_capture_becomes_published_sources_and_leaves_turn_meta(self):
        turn_meta = {TURN_METADATA_KB_PROMPTED: [_entry("rb_a")]}
        _record_turn_kb_sources(turn_meta, _case())

        assert TURN_METADATA_KB_PROMPTED not in turn_meta
        (source,) = turn_meta[MESSAGE_METADATA_KB_SOURCES]
        assert source["type"] == "knowledge_base"
        assert source["metadata"]["document_id"] == "rb_a"

    def test_no_capture_records_nothing(self):
        for turn_meta in ({}, {TURN_METADATA_KB_PROMPTED: []}):
            _record_turn_kb_sources(turn_meta, _case())
            assert MESSAGE_METADATA_KB_SOURCES not in turn_meta

    def test_everything_is_new_when_no_earlier_row_recorded_sources(self):
        turn_meta = {TURN_METADATA_KB_PROMPTED: [_entry("rb_a"), _entry("rb_b")]}
        _record_turn_kb_sources(turn_meta, _case(messages=[_assistant_row(2)]))
        assert _new_flags(turn_meta) == {"rb_a": True, "rb_b": True}

    def test_standing_context_is_not_new_and_a_replacement_is(self):
        earlier = [_assistant_row(2, _published(_entry("rb_a")))]
        turn_meta = {TURN_METADATA_KB_PROMPTED: [_entry("rb_a"), _entry("rb_c")]}
        _record_turn_kb_sources(turn_meta, _case(messages=earlier))
        assert _new_flags(turn_meta) == {"rb_a": False, "rb_c": True}

    def test_a_re_fetch_of_the_same_runbooks_is_not_new(self):
        """A revised statement re-runs the symptom pre-fetch; the same hits are
        the same context, not news."""
        earlier = [_assistant_row(4, _published(_entry("rb_a"), _entry("rb_b")))]
        turn_meta = {TURN_METADATA_KB_PROMPTED: [_entry("rb_a"), _entry("rb_b")]}
        _record_turn_kb_sources(turn_meta, _case(messages=earlier))
        assert _new_flags(turn_meta) == {"rb_a": False, "rb_b": False}

    def test_an_aside_in_between_does_not_reset_it(self):
        """An out-of-band turn's prompt carries no KB context and its row records
        none; the comparison is with the last row that did."""
        earlier = [
            _assistant_row(4, _published(_entry("rb_a"))),
            _assistant_row(5),
        ]
        turn_meta = {TURN_METADATA_KB_PROMPTED: [_entry("rb_a")]}
        _record_turn_kb_sources(turn_meta, _case(messages=earlier))
        assert _new_flags(turn_meta) == {"rb_a": False}

    def test_a_turn_whose_row_was_never_written_leaves_the_next_one_new(self):
        """Nothing is stamped on the case: a turn that failed after its prompt
        was built recorded no row, so the retried turn still reports the
        context as new."""
        turn_meta = {TURN_METADATA_KB_PROMPTED: [_entry("rb_a")]}
        _record_turn_kb_sources(turn_meta, _case(messages=[]))
        assert _new_flags(turn_meta) == {"rb_a": True}


async def _save(case: Case, turn_meta: dict) -> dict:
    repository = MagicMock()
    repository.save = AsyncMock()
    await _save_and_emit_turn(
        repository,
        agent_response_text="Rotate the logs first.",
        attachment_metadata=[],
        intent=None,
        intent_type=IntentType.CONVERSATION,
        oob_kind=None,
        payload=SimpleNamespace(query="disk is full"),
        turn_meta=turn_meta,
        turn_telemetry={},
        updated_case=case,
        was_terminal=False,
    )
    return case.messages[-1]


class TestTheRowAndTheResponseShareOneList:
    async def test_the_row_carries_the_sources_its_prompt_carried(self):
        row = await _save(_case(), {TURN_METADATA_KB_PROMPTED: [_entry("rb_a")]})

        (stored,) = row["metadata"][MESSAGE_METADATA_KB_SOURCES]
        assert stored["metadata"]["document_id"] == "rb_a"
        assert stored["new_this_turn"] is True
        assert TURN_METADATA_KB_PROMPTED not in row["metadata"]

    async def test_a_turn_whose_prompt_carried_none_stores_none(self):
        # The case still holds context, as after a pre-fetch fired during
        # application; this turn's prompt did not carry it.
        row = await _save(_case(kb_context=[_entry("rb_late")]), {})
        assert MESSAGE_METADATA_KB_SOURCES not in row["metadata"]

    def test_the_turn_response_reads_the_recorded_list(self):
        import inspect

        from faultmaven.modules.agent.domain.services.investigation_service import (
            turn_response,
        )

        src = inspect.getsource(turn_response._build_turn_response)
        assert "turn_meta.get(MESSAGE_METADATA_KB_SOURCES)" in src
        assert (
            "_kb_sources(" not in src
            and "kb_context" not in src.split("turn_sources = [", 1)[1].split("]", 1)[0]
        )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def _user_row(turn: int) -> dict:
    return {
        "message_id": f"m-{turn}-u",
        "turn_number": turn,
        "role": "user",
        "content": "disk is full",
        "created_at": datetime.now(UTC),
        "metadata": {},
    }


async def _history(case: Case):
    repo = AsyncMock()
    repo.get = AsyncMock(return_value=case)
    service = CaseService(
        case_repository=repo, session_store=AsyncMock(), max_cases_per_user=50
    )
    response = await service.get_case_messages_enhanced(case_id=case.case_id, limit=100)
    return response.messages


class TestHistoryPublishesThem:
    async def test_the_stored_sources_come_back_typed_and_not_twice(self):
        stored = _published(_entry("rb_a"))
        stored[0]["new_this_turn"] = True
        case = _case(messages=[_user_row(3), _assistant_row(3, stored)])

        user, assistant = await _history(case)

        assert user.sources is None
        assert [s.metadata["document_id"] for s in assistant.sources] == ["rb_a"]
        assert assistant.sources[0].new_this_turn is True
        assert assistant.metadata == {"progress_made": True}

    async def test_a_row_without_them_publishes_none(self):
        case = _case(messages=[_user_row(3), _assistant_row(3)])
        _, assistant = await _history(case)
        assert assistant.sources is None

    async def test_an_unreadable_source_costs_the_citation_not_the_message(self):
        good = _published(_entry("rb_a"))[0]
        bad = {"type": "not-a-source-type", "content": "x"}
        case = _case(messages=[_user_row(3), _assistant_row(3, [bad, good])])

        messages = await _history(case)

        assert [m.role for m in messages] == ["user", "assistant"]
        assert [s.metadata["document_id"] for s in messages[1].sources] == ["rb_a"]


class TestProblemStatusIsAnEnum:
    def test_the_field_is_the_domain_enum_and_serializes_to_its_value(self):
        data = ProblemVerificationData(
            symptom_statement="disk full",
            severity="medium",
            problem_status=ProblemStatus.INVALIDATED,
        )
        assert data.problem_status is ProblemStatus.INVALIDATED
        assert data.model_dump(mode="json")["problem_status"] == "invalidated"

    def test_the_published_schema_names_the_four_values(self):
        schema = ProblemVerificationData.model_json_schema()
        ref = schema["properties"]["problem_status"]["anyOf"][0]["$ref"].rsplit("/", 1)[
            -1
        ]
        assert set(schema["$defs"][ref]["enum"]) == {
            "unverified",
            "verified",
            "revision_pending",
            "invalidated",
        }
