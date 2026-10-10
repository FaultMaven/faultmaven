"""Pre-fetched KB context never outlives a driver change (ADR-020 D9).

A case retrieves with its DRIVER's knowledge: global ∪ the driver's personal KB
∪ the driver's teams' runbooks. ``case.kb_context`` is that retrieval's cached
result, persisted between turns, and the pre-fetch that writes it fires only at
three edges. So without a check, context fetched while one account drove stands
in every later prompt after the case is handed on — the previous driver's
PERSONAL runbook quoted to the new driver's model.

The check lives at the consumer: ``kb_context_origin`` records who the context
was fetched for, ``kb_push.visible_kb_context`` hides it from every reader the
moment that is not the effective driver, and the turn start re-runs the stored
query under the current driver (``KbPrefetcher.refresh_for_driver``) — or
clears the context if it cannot.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from faultmaven.core.investigation.kb_push import (
    kb_context_is_stale,
    visible_kb_context,
)
from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher
from tests.unit.core.investigation.test_system_feedback_delivery_1688 import (
    SUBSTANTIVE,
    _engine,
    _investigating_case,
    _turn,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

DRIVER = "u_driver_1898"
PREVIOUS_MARKER = "MARKER::previous-driver-personal-runbook::"
CURRENT_MARKER = "MARKER::current-driver-runbook::"


def _context(marker: str) -> list:
    return [
        {
            "title": "Personal runbook",
            "section": "",
            "summary": f"{marker} restart the etcd member with --force",
            "score": 0.9,
            "type": "runbook",
            "parent_document_id": "rb_personal",
            "trigger": "symptom",
        }
    ]


class _Knowledge:
    """``search_knowledge``: records the scope it was asked under, and answers
    with the CURRENT driver's runbook — or raises."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    async def search_knowledge(
        self, query, limit=10, filters=None, use_hybrid=False, min_score=None
    ):
        self.calls.append(query)
        if self.fail:
            raise RuntimeError("vector store unavailable")
        return [
            SimpleNamespace(
                title="Team runbook",
                snippet=f"{CURRENT_MARKER} check the peer certificates",
                score=0.9,
                document_type="runbook",
                parent_document_id="rb_team",
            )
        ]


def _handed_back_case(origin):
    """A case DRIVER drove (its context was fetched for DRIVER) that is now
    back with its creator."""
    case = _investigating_case()
    case.driver_id = None
    case.kb_context = _context(PREVIOUS_MARKER)
    case.kb_context_origin = origin
    return case


def _wired(knowledge):
    engine = _engine()
    engine.deps.knowledge_service = knowledge
    engine.deps.team_service = None
    engine.deps.share_repository = None
    engine.kb_prefetcher = KbPrefetcher(deps=engine.deps)
    return engine


def _prompt(engine) -> str:
    return engine.generator.generate_structured_output.call_args[0][0]


class TestEveryReaderStopsSeeingItAtOnce:
    def test_context_fetched_for_a_previous_driver_is_stale_and_hidden(self):
        case = _handed_back_case({"driver_id": DRIVER, "query": "q"})

        assert kb_context_is_stale(case)
        assert visible_kb_context(case) == []

    def test_context_fetched_for_the_current_driver_is_shown(self):
        case = _handed_back_case({"driver_id": DRIVER, "query": "q"})
        case.driver_id = DRIVER

        assert not kb_context_is_stale(case)
        assert visible_kb_context(case) == case.kb_context

    def test_context_with_no_origin_counts_as_the_creators(self):
        case = _handed_back_case(None)
        assert not kb_context_is_stale(case)

        case.driver_id = DRIVER
        assert kb_context_is_stale(case), "the creator's context, now another drives"


class TestTheNextTurnsPrompt:
    async def test_after_a_hand_back_the_prompt_carries_the_new_drivers_context(
        self,
    ):
        knowledge = _Knowledge()
        engine = _wired(knowledge)
        case = _handed_back_case({"driver_id": DRIVER, "query": "etcd member"})

        assert await _turn(engine, case, SUBSTANTIVE)

        prompt = _prompt(engine)
        assert PREVIOUS_MARKER not in prompt
        assert CURRENT_MARKER in prompt, "control: the re-fetch reached the prompt"
        assert knowledge.calls == ["etcd member"], "re-run with the STORED query"
        assert case.kb_context_origin["driver_id"] == case.user_id

    async def test_a_failed_refetch_clears_rather_than_keeps_it(self):
        engine = _wired(_Knowledge(fail=True))
        case = _handed_back_case({"driver_id": DRIVER, "query": "etcd member"})

        assert await _turn(engine, case, SUBSTANTIVE)

        assert PREVIOUS_MARKER not in _prompt(engine)
        assert case.kb_context is None and case.kb_context_origin is None

    async def test_context_with_no_query_to_rerun_is_cleared(self):
        knowledge = _Knowledge()
        engine = _wired(knowledge)
        case = _handed_back_case({"driver_id": DRIVER})

        assert await _turn(engine, case, SUBSTANTIVE)

        assert PREVIOUS_MARKER not in _prompt(engine)
        assert knowledge.calls == []
        assert case.kb_context is None

    async def test_the_current_drivers_context_is_not_refetched(self):
        """The control: nothing changed hands, so the context stands and no
        search runs."""
        knowledge = _Knowledge()
        engine = _wired(knowledge)
        case = _handed_back_case({"driver_id": DRIVER, "query": "etcd member"})
        case.driver_id = DRIVER

        assert await _turn(engine, case, SUBSTANTIVE)

        assert PREVIOUS_MARKER in _prompt(engine)
        assert knowledge.calls == []


async def test_a_prefetch_records_who_it_fetched_for_and_how():
    engine = _wired(_Knowledge())
    case = _investigating_case()
    case.driver_id = DRIVER

    assert await engine.kb_prefetcher.prefetch_kb_context(case, "etcd", "symptom")

    assert case.kb_context_origin == {
        "driver_id": DRIVER,
        "query": "etcd",
        "trigger": "symptom",
    }


async def test_a_miss_clears_the_origin_with_the_context():
    knowledge = MagicMock()

    async def nothing(*a, **k):
        return []

    knowledge.search_knowledge = nothing
    engine = _wired(knowledge)
    case = _handed_back_case({"driver_id": DRIVER, "query": "q"})

    await engine.kb_prefetcher.prefetch_kb_context(case, "q", "symptom")

    assert case.kb_context is None and case.kb_context_origin is None
