"""Contract 11.2.0 — the KB context says which turn fetched it, and history keeps it.

``case.kb_context`` stands in every prompt from the turn a pre-fetch writes it
until another pre-fetch replaces it, so ``TurnResponse.sources`` repeats it on
every turn. A client could tell which turn it was NEW on only by diffing turns,
and ``GET .../messages`` returned no sources at all, so history showed none of
what the live turn did. These pin the three pieces that close that: the
pre-fetch stamps ``fetched_turn``, the turn's save persists the sources fetched
on that turn with the assistant row, and the history read publishes them typed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.models.api_models import IntentType
from faultmaven.models.case_ui import ProblemVerificationData
from faultmaven.modules.agent.domain.services.investigation_service.turn_bookkeeping import (
    _kb_context_sources,
)
from faultmaven.modules.agent.domain.services.investigation_service.turn_messages import (
    _save_and_emit_turn,
)
from faultmaven.modules.case.contracts import MESSAGE_METADATA_KB_SOURCES
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.problem import ProblemStatus
from faultmaven.modules.case.domain.services.case_service import CaseService
from tests.unit.core.investigation.test_kb_prefetch import _case as _prefetch_case
from tests.unit.core.investigation.test_kb_prefetch import (
    _engine,
    _search_hit,
    _SearchRecordingStub,
)

pytestmark = pytest.mark.unit


def _entry(fetched_turn=3, doc="rb_disk"):
    return {
        "title": "Disk full runbook",
        "summary": "Rotate the logs before resizing the volume.",
        "score": 0.7,
        "type": "runbook",
        "parent_document_id": doc,
        "trigger": "symptom",
        "fetched_turn": fetched_turn,
    }


def _case(current_turn: int, kb_context=None) -> Case:
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
    return case


class TestThePreFetchStampsItsTurn:
    @pytest.mark.asyncio
    async def test_every_admitted_entry_records_the_turn_that_fetched_it(self):
        engine = _engine(
            _SearchRecordingStub([_search_hit(), _search_hit(parent_id="rb2")])
        )
        case = _prefetch_case()
        case.current_turn = 7

        await engine.kb_prefetcher.prefetch_kb_context(case, "disk full", "symptom")

        assert case.kb_context, "the stub's hits clear the floor"
        assert {entry["fetched_turn"] for entry in case.kb_context} == {7}


class TestTheSourceCarriesTheTurn:
    def test_fetched_turn_is_published_on_each_source(self):
        sources = _kb_context_sources(_case(5, [_entry(3), _entry(3, doc="rb_inode")]))
        assert [s.fetched_turn for s in sources] == [3, 3]

    @pytest.mark.parametrize("stored", [None, True, "3"], ids=["absent", "bool", "str"])
    def test_a_value_that_is_not_a_turn_is_published_as_null(self, stored):
        entry = _entry()
        if stored is None:
            entry.pop("fetched_turn")
        else:
            entry["fetched_turn"] = stored
        (source,) = _kb_context_sources(_case(5, [entry]))
        assert source.fetched_turn is None


async def _save(case: Case) -> dict:
    """Run the turn's save on ``case`` and return the assistant row it appended."""
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
        turn_meta={},
        turn_telemetry={},
        updated_case=case,
        was_terminal=False,
    )
    return case.messages[-1]


class TestTheRowKeepsWhatThisTurnFetched:
    @pytest.mark.asyncio
    async def test_the_fetch_turn_row_carries_the_sources(self):
        row = await _save(_case(3, [_entry(3)]))

        stored = row["metadata"][MESSAGE_METADATA_KB_SOURCES]
        assert [s["metadata"]["document_id"] for s in stored] == ["rb_disk"]
        assert stored[0]["fetched_turn"] == 3

    @pytest.mark.asyncio
    async def test_a_later_turn_with_the_same_standing_context_carries_none(self):
        # The context is still in this turn's prompt, and the turn response
        # still lists it; the row records only the turn that fetched it.
        row = await _save(_case(4, [_entry(3)]))
        assert MESSAGE_METADATA_KB_SOURCES not in row["metadata"]

    @pytest.mark.asyncio
    async def test_a_turn_with_no_context_carries_none(self):
        row = await _save(_case(4, None))
        assert MESSAGE_METADATA_KB_SOURCES not in row["metadata"]


def _rows(kb_sources=None):
    assistant_meta = {"progress_made": True}
    if kb_sources is not None:
        assistant_meta[MESSAGE_METADATA_KB_SOURCES] = kb_sources
    return [
        {
            "message_id": "m-3-u",
            "turn_number": 3,
            "role": "user",
            "content": "disk is full",
            "created_at": datetime.now(timezone.utc),
            "metadata": {},
        },
        {
            "message_id": "m-3-a",
            "turn_number": 3,
            "role": "assistant",
            "content": "Rotate the logs first.",
            "created_at": datetime.now(timezone.utc),
            "metadata": assistant_meta,
        },
    ]


async def _history(case: Case):
    repo = AsyncMock()
    repo.get = AsyncMock(return_value=case)
    service = CaseService(
        case_repository=repo, session_store=AsyncMock(), max_cases_per_user=50
    )
    response = await service.get_case_messages_enhanced(case_id=case.case_id, limit=100)
    return response.messages


class TestHistoryPublishesThem:
    @pytest.mark.asyncio
    async def test_the_stored_sources_come_back_typed_and_not_twice(self):
        stored = [
            s.model_dump(mode="json")
            for s in _kb_context_sources(_case(3, [_entry(3)]))
        ]
        case = _case(3)
        case.messages = _rows(kb_sources=stored)

        user, assistant = await _history(case)

        assert user.sources is None
        assert [s.metadata["document_id"] for s in assistant.sources] == ["rb_disk"]
        assert assistant.sources[0].fetched_turn == 3
        assert MESSAGE_METADATA_KB_SOURCES not in assistant.metadata
        assert assistant.metadata == {"progress_made": True}

    @pytest.mark.asyncio
    async def test_a_row_without_them_publishes_none(self):
        case = _case(3)
        case.messages = _rows()
        _, assistant = await _history(case)
        assert assistant.sources is None


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
