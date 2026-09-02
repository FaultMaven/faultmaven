"""#1272 — the engine's KB prefetch: hybrid retrieval, the admission floor,
stale-context clearing, and the remediation-time edge.

The grounding gate #1272 added on top of this path served the KB cause seeder,
which was removed in fm#1295 (record: ``docs/architecture/knowledge-and-ai/retired/kb-cause-seeder/design.md``).
What remains here is the retrieval contract the prefetch itself owes.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    KB_PREFETCH_FETCH_LIMIT,
    KB_PREFETCH_RELEVANCE_THRESHOLD,
    MilestoneEngine,
)
from faultmaven.models.common import SearchResult


def _engine(knowledge_service=None):
    engine = MilestoneEngine.__new__(MilestoneEngine)
    engine.knowledge_service = knowledge_service
    engine.hypothesis_manager = MagicMock()
    return engine


def _case():
    return SimpleNamespace(
        case_id="case_test",
        user_id="u1",
        organization_id=None,
        kb_context=None,
        current_turn=1,
        description="Failed to start QEMU binary cannot create PID file",
    )


class TestPrefetchUsesHybridRetrieval:
    @pytest.mark.asyncio
    async def test_prefetch_asks_for_hybrid_and_floors_at_admission(self):
        service = MagicMock()
        service.search_knowledge = AsyncMock(return_value=[])
        engine = _engine(service)
        await engine._prefetch_kb_context(_case(), "disk full", "symptom")
        kwargs = service.search_knowledge.call_args.kwargs
        assert kwargs["use_hybrid"] is True, (
            "pure vector search puts the runbook covering #1272's incident at "
            "rank 70 of 91; no floor value can rescue a chunk ranked 369th "
            "because the fetch limit is applied to chunks before any floor"
        )
        assert kwargs["min_score"] == KB_PREFETCH_RELEVANCE_THRESHOLD
        assert kwargs["limit"] == KB_PREFETCH_FETCH_LIMIT


class TestStaleContextIsCleared:
    @pytest.mark.asyncio
    async def test_a_trigger_that_finds_nothing_clears_earlier_context(self):
        """A later trigger's miss must not leave the earlier one's runbooks up.

        The floor moved to admission, which made `relevant` identical to
        `results` and left the clearing branch — written as `elif results:` —
        unreachable. Stale runbooks then stood in the prompt as if they still
        matched.
        """
        service = MagicMock()
        service.search_knowledge = AsyncMock(return_value=[])
        engine = _engine(service)
        case = _case()
        case.kb_context = [{"title": "Something From An Earlier Turn"}]
        await engine._prefetch_kb_context(case, "root cause query", "root_cause")
        assert case.kb_context is None


class TestRemediationPrefetchOnTheIdentifiedEdge:
    """Runbooks reach the model at remediation time, unconditionally.

    fm#1295 removed the KB cause seeder — the engine's unasked assertion of a
    runbook's causes as hypotheses. What must survive it is the runbook
    reaching the model as prose at remediation time — the ``root_cause``
    prefetch the turn pipeline fires on the cause_state→IDENTIFIED edge —
    because that is where the corrective pattern has measured value.

    Driven through ``_apply_investigation_updates`` itself, not by calling
    ``_prefetch_kb_context`` directly: the thing being pinned is the CALL SITE,
    which a removal could delete or gate without any test on
    ``_prefetch_kb_context`` noticing. The rising edge is supplied by patching
    the edge helper (tested on its own in ``test_prompt_engine_realignment``),
    and a no-edge control shows the assertion is about the edge, not about a
    mock that always fires.
    """

    @staticmethod
    def _engine_on_edge(monkeypatch, edge_query):
        from tests.unit.core.investigation.test_solution_offer_liveness import (
            _make_engine,
        )

        monkeypatch.setattr(
            "faultmaven.core.investigation.milestone_engine."
            "_kb_prefetch_query_on_identification",
            lambda *a, **k: edge_query,
        )
        engine = _make_engine()
        engine._prefetch_kb_context = AsyncMock(return_value=[])
        return engine

    @pytest.mark.asyncio
    async def test_the_identified_edge_prefetches_remediation_runbooks(
        self, monkeypatch
    ):
        from tests.unit.core.investigation.test_solution_offer_liveness import (
            _make_case,
            _meta,
            _solution_updates,
        )

        engine = self._engine_on_edge(monkeypatch, "redis maxmemory reached")
        case = _make_case()
        await engine._apply_investigation_updates(case, _solution_updates(), _meta())
        calls = [
            c
            for c in engine._prefetch_kb_context.await_args_list
            if c.args[2:3] == ("root_cause",)
        ]
        assert len(calls) == 1, (
            "the remediation-time KB prefetch on the cause_state→IDENTIFIED edge "
            f"must fire; prefetch calls seen: "
            f"{engine._prefetch_kb_context.await_args_list}"
        )
        assert (
            calls[0].args[0] is case and calls[0].args[1] == "redis maxmemory reached"
        )

    @pytest.mark.asyncio
    async def test_no_edge_no_remediation_prefetch(self, monkeypatch):
        """Control: the assertion above is about the edge, not a mock that
        always fires."""
        from tests.unit.core.investigation.test_solution_offer_liveness import (
            _make_case,
            _meta,
            _solution_updates,
        )

        engine = self._engine_on_edge(monkeypatch, None)
        await engine._apply_investigation_updates(
            _make_case(), _solution_updates(), _meta()
        )
        assert not [
            c
            for c in engine._prefetch_kb_context.await_args_list
            if c.args[2:3] == ("root_cause",)
        ]
