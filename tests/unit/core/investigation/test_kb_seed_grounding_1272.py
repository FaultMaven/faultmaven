"""#1272 — the seeding path: hybrid retrieval, the grounding gate, and the
identity a retrieved runbook arrives with.

The failure this closes is the CONFIDENT WRONG ANSWER. On the query the issue
was opened on, eight runbooks cleared the similarity floor, three of them on
several chunks each, and every one was about a different platform — because the
only word identifying the system (``qemu``) appears in none of the 91 shipped
runbooks, so every candidate matched on "failed", "start" and "file" alone.
Neither the floor nor #1144's corroboration guard can see that, because both
ask about a runbook's match and neither asks whether the query was about it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.milestone_engine import (
    KB_PREFETCH_FETCH_LIMIT,
    KB_PREFETCH_RELEVANCE_THRESHOLD,
    KB_SEED_MIN_TERM_COVERAGE,
    MilestoneEngine,
)
from faultmaven.models.common import SearchResult


def _result(
    doc_id,
    parent,
    score=0.7,
    coverage=None,
    named=None,
    letters=("A",),
    total_chunks=8,
    title="Some Runbook",
):
    return SearchResult(
        document_id=doc_id,
        title=title,
        document_type="runbook",
        tags=[],
        score=score,
        snippet="...",
        parent_document_id=parent,
        total_chunks=total_chunks,
        matched_cause_letters=list(letters),
        term_coverage=coverage,
        identity_terms_in_query=list(named or []),
    )


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


class TestSeedingGroundingGate:
    """A runbook may seed only if the query NAMED it or it COVERS the query."""

    async def _seed(self, hits, monkeypatch):
        seen = {}
        service = MagicMock()
        service.get_runbook_causes = AsyncMock(
            side_effect=lambda p: [{"cause_letter": "A", "statement": "s"}]
        )
        engine = _engine(service)

        def _capture(case, runbooks, *a, **k):
            seen["runbooks"] = runbooks
            return SimpleNamespace(seeded_anything=True)

        monkeypatch.setattr(
            "faultmaven.core.investigation.kb_cause_seeder.seed_candidate_causes",
            _capture,
        )
        monkeypatch.setattr(
            "faultmaven.config.settings.get_settings",
            lambda: SimpleNamespace(
                features=SimpleNamespace(kb_cause_seeder_enabled=True)
            ),
        )
        await engine._seed_candidate_causes_from_kb(_case(), hits)
        return seen.get("runbooks", [])

    @pytest.mark.asyncio
    async def test_ungrounded_runbook_does_not_seed(self, monkeypatch):
        # #1272 exactly: two chunks of a Kubernetes runbook, well above the
        # similarity floor, corroborating each other — and the query is about
        # QEMU.
        hits = [
            _result("k8s_chunk_1", "kb_k8s", coverage=0.53, named=[]),
            _result("k8s_chunk_2", "kb_k8s", coverage=0.49, named=[]),
        ]
        assert await self._seed(hits, monkeypatch) == []

    @pytest.mark.asyncio
    async def test_a_named_runbook_seeds(self, monkeypatch):
        hits = [
            _result("nginx_1", "kb_nginx", coverage=0.21, named=["nginx", "502"]),
            _result("nginx_2", "kb_nginx", coverage=0.19, named=["nginx"]),
        ]
        seeded = await self._seed(hits, monkeypatch)
        assert [r.item_id for r in seeded] == ["kb_nginx"]

    @pytest.mark.asyncio
    async def test_a_covering_runbook_seeds_even_when_unnamed(self, monkeypatch):
        # The symptom-phrased query: the runbook states the symptom verbatim
        # and the query never names the platform.
        hits = [
            _result("ldf_1", "kb_ldf", coverage=1.0, named=[]),
            _result("ldf_2", "kb_ldf", coverage=0.4, named=[]),
        ]
        seeded = await self._seed(hits, monkeypatch)
        assert [r.item_id for r in seeded] == ["kb_ldf"]

    @pytest.mark.asyncio
    async def test_grounding_is_per_runbook_not_per_chunk(self, monkeypatch):
        """One grounded chunk admits the runbook; its siblings then corroborate.

        Judged per chunk, this runbook is admitted on its covering chunk and
        then declined by #1144 for want of a second — grounding would be doing
        corroboration's job a second time.
        """
        hits = [
            _result("ldf_1", "kb_ldf", coverage=1.0, named=[], letters=("A",)),
            _result("ldf_2", "kb_ldf", coverage=0.3, named=[], letters=("B",)),
        ]
        seeded = await self._seed(hits, monkeypatch)
        assert [r.item_id for r in seeded] == ["kb_ldf"]

    @pytest.mark.asyncio
    async def test_coverage_just_below_the_bar_is_not_grounded(self, monkeypatch):
        below = KB_SEED_MIN_TERM_COVERAGE - 0.01
        hits = [
            _result("x_1", "kb_x", coverage=below, named=[]),
            _result("x_2", "kb_x", coverage=below, named=[]),
        ]
        assert await self._seed(hits, monkeypatch) == []

    @pytest.mark.asyncio
    async def test_unmeasured_hits_are_not_judged(self, monkeypatch):
        """No term index / pure-vector path: absent evidence must not gate.

        An absent measurement must not authorise what the gate withholds, but
        it must not silently disable seeding either.
        """
        hits = [
            _result("y_1", "kb_y", coverage=None, named=[]),
            _result("y_2", "kb_y", coverage=None, named=[]),
        ]
        seeded = await self._seed(hits, monkeypatch)
        assert [r.item_id for r in seeded] == ["kb_y"]

    @pytest.mark.asyncio
    async def test_a_grounded_runbook_survives_beside_an_ungrounded_one(
        self, monkeypatch
    ):
        hits = [
            _result("k8s_1", "kb_k8s", score=0.80, coverage=0.5, named=[]),
            _result("k8s_2", "kb_k8s", score=0.79, coverage=0.5, named=[]),
            _result("ldf_1", "kb_ldf", score=0.60, coverage=1.0, named=[]),
            _result("ldf_2", "kb_ldf", score=0.59, coverage=0.2, named=[]),
        ]
        seeded = await self._seed(hits, monkeypatch)
        assert [r.item_id for r in seeded] == ["kb_ldf"], (
            "the gate is per runbook, so a higher-ranked ungrounded runbook "
            "must not take the grounded one's slot"
        )
