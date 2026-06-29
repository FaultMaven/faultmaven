"""Tests for the structured runbook-Cause matcher entry point
(``AnswerFromKB.aget_cause_matches``) — increment 3 of the matcher wiring.

Covers retrieve → rank distinct runbooks → resolve causes → build CauseRecords →
run the rung-level evaluator. Uses a mock vector store and the REAL
``IndicatorEvaluator`` so the path is exercised end-to-end.
"""

from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.indicator_evaluator import IndicatorEvaluator
from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _chunk(item_id: str, idx: int = 0, score: float = 0.9, *, with_parent=True):
    meta = {"title": f"Doc {item_id}"}
    if with_parent:
        meta["parent_document_id"] = item_id
    return {
        "id": f"{item_id}_chunk_{idx}",
        "content": f"chunk {idx} of {item_id}",
        "metadata": meta,
        "score": score,
    }


def _cause_dict(
    letter: str,
    name: str,
    rung_indicators: Dict[str, List[str]],
    predicates: Optional[List[dict]] = None,
    *,
    fallback: bool = False,
):
    refs = list(rung_indicators.keys()) or ["root", "D"]
    return {
        "cause_letter": letter,
        "cause_name": name,
        "cause_statement": "stmt",
        "chain_nodes": [{"ref": r, "node_type": "rung", "statement": r} for r in refs],
        "rung_indicators": rung_indicators,
        "match_predicates": predicates or [],
        "is_fallback_cause": fallback,
    }


def _make_tool(chunks):
    vector_store = MagicMock()
    vector_store.hybrid_search = AsyncMock(return_value=chunks)
    vector_store.search = AsyncMock(return_value=chunks)
    llm_router = MagicMock()
    return AnswerFromKB(vector_store=vector_store, llm_router=llm_router)


def _evaluator(*step_pairs):
    table = dict(step_pairs)
    return IndicatorEvaluator(step_output_resolver=table.get)


def _matching_cause():
    return _cause_dict(
        "A",
        "real",
        {"root": ["[Step 1] cond"]},
        [{"step": 1, "predicate": "contains", "target": "x"}],
    )


def _fallback_cause():
    return _cause_dict("Z", "fallback", {"D": ["[Default]"]}, fallback=True)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_single_runbook_single_match(self):
        tool = _make_tool([_chunk("kb_rb1", 0), _chunk("kb_rb1", 1, 0.8)])
        causes = {"kb_rb1": [_matching_cause(), _fallback_cause()]}

        async def resolve(item_id):
            return causes.get(item_id)

        results = await tool.aget_cause_matches(
            "why is it failing?",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )

        assert len(results) == 1
        assert results[0].runbook_id == "kb_rb1"
        assert results[0].verdict == "single"
        assert results[0].selected_cause.cause_name == "real"
        # The selected Cause's full record (its chain) is threaded onto the
        # result so the engine can instantiate it without re-resolving.
        assert results[0].selected_record is not None
        assert results[0].selected_record.cause_letter == "A"

    @pytest.mark.asyncio
    async def test_none_verdict_has_no_selected_record(self):
        # A non-matching real cause + fallback → verdict 'none', nothing to act on.
        tool = _make_tool([_chunk("kb_rb1", 0)])
        nonmatch = _cause_dict(
            "A",
            "real",
            {"root": ["[Step 1] cond"]},
            [{"step": 1, "predicate": "contains", "target": "absent-token"}],
        )

        async def resolve(item_id):
            return [nonmatch, _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value present")),
        )
        assert results[0].verdict == "none"
        assert results[0].selected_record is None

    @pytest.mark.asyncio
    async def test_ranks_distinct_runbooks_and_caps(self):
        # 3 runbooks across 4 chunks; cap at 2 distinct runbooks, best-first.
        chunks = [
            _chunk("kb_a", 0, 0.95),
            _chunk("kb_a", 1, 0.90),
            _chunk("kb_b", 0, 0.85),
            _chunk("kb_c", 0, 0.80),
        ]
        tool = _make_tool(chunks)
        seen = []

        async def resolve(item_id):
            seen.append(item_id)
            return [_matching_cause(), _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
            max_runbooks=2,
        )

        assert [r.runbook_id for r in results] == ["kb_a", "kb_b"]
        assert seen == ["kb_a", "kb_b"]  # kb_c never resolved (capped)

    @pytest.mark.asyncio
    async def test_item_id_falls_back_to_chunk_id_suffix(self):
        # No parent_document_id metadata → derive item_id from the chunk id.
        tool = _make_tool([_chunk("kb_rb9", 0, with_parent=False)])

        async def resolve(item_id):
            assert item_id == "kb_rb9"
            return [_matching_cause(), _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert results[0].runbook_id == "kb_rb9"


# ---------------------------------------------------------------------------
# Skips, tolerance, and degradation (matcher is a prior, not a gate)
# ---------------------------------------------------------------------------


class TestDegradation:
    @pytest.mark.asyncio
    async def test_runbook_without_causes_is_skipped(self):
        tool = _make_tool([_chunk("kb_v3", 0), _chunk("kb_v4", 0, 0.8)])

        async def resolve(item_id):
            return (
                None if item_id == "kb_v3" else [_matching_cause(), _fallback_cause()]
            )

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert [r.runbook_id for r in results] == ["kb_v4"]

    @pytest.mark.asyncio
    async def test_empty_causes_list_is_skipped(self):
        tool = _make_tool([_chunk("kb_rb1", 0)])

        async def resolve(item_id):
            return []

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_malformed_cause_entry_skipped_runbook_still_matches(self):
        tool = _make_tool([_chunk("kb_rb1", 0)])

        async def resolve(item_id):
            # One junk entry (no cause_letter → CauseRecord rejects it), one good.
            return [{"oops": True}, "not-a-dict", _matching_cause(), _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert len(results) == 1
        assert results[0].verdict == "single"

    @pytest.mark.asyncio
    async def test_all_causes_malformed_skips_runbook(self):
        tool = _make_tool([_chunk("kb_rb1", 0)])

        async def resolve(item_id):
            return [{"oops": True}]  # no valid CauseRecord

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_resolve_causes_error_skips_that_runbook(self):
        tool = _make_tool([_chunk("kb_bad", 0, 0.9), _chunk("kb_ok", 0, 0.8)])

        async def resolve(item_id):
            if item_id == "kb_bad":
                raise RuntimeError("db blip")
            return [_matching_cause(), _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert [r.runbook_id for r in results] == ["kb_ok"]

    @pytest.mark.asyncio
    async def test_retrieval_failure_returns_empty(self):
        tool = _make_tool([])
        tool._vector_store.hybrid_search = AsyncMock(side_effect=RuntimeError("boom"))
        tool._vector_store.search = AsyncMock(side_effect=RuntimeError("boom"))

        called = False

        async def resolve(item_id):
            nonlocal called
            called = True
            return [_matching_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert results == []
        assert called is False  # no runbooks ranked → resolver never invoked

    @pytest.mark.asyncio
    async def test_no_chunks_returns_empty(self):
        tool = _make_tool([])

        async def resolve(item_id):
            return [_matching_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert results == []

    @pytest.mark.asyncio
    async def test_non_iterable_resolver_return_skips_not_raises(self):
        # A misbehaving resolver returning a non-iterable must not break the
        # turn — the guard covers build_cause_records, not just resolve.
        tool = _make_tool([_chunk("kb_bad", 0, 0.9), _chunk("kb_ok", 0, 0.8)])

        async def resolve(item_id):
            return 42 if item_id == "kb_bad" else [_matching_cause(), _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        assert [r.runbook_id for r in results] == ["kb_ok"]

    @pytest.mark.asyncio
    async def test_evaluator_error_skips_not_raises(self):
        tool = _make_tool([_chunk("kb_a", 0, 0.9), _chunk("kb_b", 0, 0.8)])

        class _BoomThenOk:
            def __init__(self, inner):
                self._inner = inner

            async def evaluate(self, runbook_id, causes):
                if runbook_id == "kb_a":
                    raise RuntimeError("evaluator blew up")
                return await self._inner.evaluate(runbook_id, causes)

        async def resolve(item_id):
            return [_matching_cause(), _fallback_cause()]

        evaluator = _BoomThenOk(_evaluator((1, "value x present")))
        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=evaluator,
        )
        assert [r.runbook_id for r in results] == ["kb_b"]


# ---------------------------------------------------------------------------
# Retrieval sizing + search-mode dispatch
# ---------------------------------------------------------------------------


class TestRetrieval:
    @pytest.mark.asyncio
    async def test_retrieval_pool_oversized_to_surface_distinct_runbooks(self):
        # k defaults to max_runbooks * fanout so one multi-chunk runbook can't
        # starve the others — the store must be asked for the larger pool.
        tool = _make_tool([_chunk("kb_a", 0)])

        async def resolve(item_id):
            return [_matching_cause(), _fallback_cause()]

        await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
            max_runbooks=3,
        )
        # hybrid is the unified KB's mode → assert the k passed to the store.
        _, kwargs = tool._vector_store.hybrid_search.call_args
        assert kwargs["k"] == 3 * 8  # _DEFAULT_MAX_RUNBOOKS * _RETRIEVAL_FANOUT

    @pytest.mark.asyncio
    async def test_single_dominant_runbook_yields_one_result(self):
        # All retrieved chunks belong to one runbook → only that one surfaces,
        # even with max_runbooks=3 (no phantom runbooks invented).
        chunks = [_chunk("kb_a", i, score=0.9 - i * 0.01) for i in range(8)]
        tool = _make_tool(chunks)

        async def resolve(item_id):
            return [_matching_cause(), _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
            max_runbooks=3,
        )
        assert [r.runbook_id for r in results] == ["kb_a"]

    @pytest.mark.asyncio
    async def test_uses_hybrid_search_by_default(self):
        tool = _make_tool([_chunk("kb_a", 0)])

        async def resolve(item_id):
            return [_matching_cause(), _fallback_cause()]

        await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        tool._vector_store.hybrid_search.assert_awaited_once()
        tool._vector_store.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_vector_search_when_no_hybrid(self):
        # A store without hybrid_search must use plain vector search.
        vector_store = MagicMock(spec=["search"])
        vector_store.search = AsyncMock(return_value=[_chunk("kb_a", 0)])
        llm_router = MagicMock()
        tool = AnswerFromKB(vector_store=vector_store, llm_router=llm_router)

        async def resolve(item_id):
            return [_matching_cause(), _fallback_cause()]

        results = await tool.aget_cause_matches(
            "q",
            user_id="u1",
            resolve_causes=resolve,
            evaluator=_evaluator((1, "value x present")),
        )
        vector_store.search.assert_awaited_once()
        assert [r.runbook_id for r in results] == ["kb_a"]
