"""KB pre-fetch admission prefers runbook diversity without handing back budget.

#1379: the admission slice was a flat ``relevant[:KB_CONTEXT_MAX_ENTRIES]``,
blind to which runbook each chunk came from. A chunk is a ``### Cause`` and a
runbook carries 3-10 of them, so the entries the model saw were routinely
several causes of ONE runbook while a different runbook the query also needed
sat in the pool unrendered. Measured over ``tests/eval/kb_retrieval/``: 22 of 23
expected runbooks reached the pool, only 17 reached the prompt.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.milestone_engine import (
    KB_CONTEXT_MAX_ENTRIES,
    KB_CONTEXT_MAX_PER_RUNBOOK,
    KB_PREFETCH_FETCH_LIMIT,
    _admit_diverse,
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]


def hit(parent, title=""):
    return SimpleNamespace(parent_document_id=parent, title=title)


def parents(selected):
    return [h.parent_document_id for h in selected]


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


def test_a_second_runbook_is_not_crowded_out():
    """The defect, stated directly: B reaches the prompt over A's third cause."""
    admitted = _admit_diverse(
        [hit("A"), hit("A"), hit("A"), hit("B"), hit("B"), hit("C")]
    )

    assert parents(admitted) == ["A", "A", "B", "B", "C"]


def test_the_cap_binds_until_every_runbook_has_had_its_share():
    """The cap governs WHICH runbooks get in, not how the last slots are spent.

    With A*4 and B*4 and a budget of 5, the capped pass admits A, A, B, B — both
    runbooks represented — and the fifth slot is then filled from the skipped
    hits in rank order, so A takes it. That deliberately exceeds the per-runbook
    cap, and it is the right trade: backfill can only ever draw from runbooks
    ALREADY admitted (a hit is deferred only once its parent has filled its
    share), so it cannot cost coverage. It only decides how leftover budget is
    spent, and rank is the honest answer to that.
    """
    admitted = _admit_diverse([hit("A")] * 4 + [hit("B")] * 4)

    assert parents(admitted) == ["A", "A", "B", "B", "A"]
    # Both runbooks reached the prompt, which is the property that matters.
    assert set(parents(admitted)) == {"A", "B"}


def test_backfill_cannot_change_which_runbooks_are_covered():
    """The coverage claim rests on this, so it is asserted rather than argued.

    A hit is deferred only when its parent already hit the cap — so every
    deferred hit belongs to a runbook that is already admitted, and no backfill
    can introduce or remove a runbook.
    """
    pools = [
        [hit("A")] * 4 + [hit("B")] * 4,
        [hit("A"), hit("A"), hit("A"), hit("B"), hit("C")],
        [hit("A")] * 10,
    ]
    for pool in pools:
        capped_only = []
        seen: dict = {}
        for h in pool:
            p = h.parent_document_id
            if seen.get(p, 0) >= KB_CONTEXT_MAX_PER_RUNBOOK:
                continue
            seen[p] = seen.get(p, 0) + 1
            capped_only.append(h)
            if len(capped_only) == KB_CONTEXT_MAX_ENTRIES:
                break

        assert set(parents(_admit_diverse(pool))) == set(parents(capped_only))


def test_rank_order_is_preserved():
    """Hits are SKIPPED, never reordered — the reranker's blend still decides.

    Reordering would silently discard the keyword and term-overlap evidence that
    produced the ranking, which is the same reason the admission slice reads the
    ranked list rather than re-sorting by cosine.
    """
    admitted = _admit_diverse(
        [hit("A"), hit("B"), hit("A"), hit("C"), hit("A"), hit("D")]
    )

    assert parents(admitted) == ["A", "B", "A", "C", "D"]


# ---------------------------------------------------------------------------
# The backfill — the cap must not cost budget where there is nothing to protect
# ---------------------------------------------------------------------------


def test_a_single_runbook_owning_the_answer_still_fills_the_budget():
    """5 of 16 labelled queries draw the whole pool from one runbook.

    Without the backfill the cap would render 2 entries where the budget allows
    5 — strictly LESS context than before the cap existed, on exactly the
    queries where there is no second runbook to protect.
    """
    admitted = _admit_diverse([hit("A")] * KB_PREFETCH_FETCH_LIMIT)

    assert len(admitted) == KB_CONTEXT_MAX_ENTRIES
    assert parents(admitted) == ["A"] * KB_CONTEXT_MAX_ENTRIES


def test_backfill_takes_the_skipped_hits_in_rank_order():
    """Diversity first, then the best of what the cap skipped."""
    admitted = _admit_diverse([hit("A"), hit("A"), hit("A"), hit("A"), hit("B")])

    assert parents(admitted) == ["A", "A", "B", "A", "A"]


def test_a_short_pool_is_returned_whole():
    admitted = _admit_diverse([hit("A"), hit("B")])

    assert parents(admitted) == ["A", "B"]


def test_an_empty_pool_admits_nothing():
    assert _admit_diverse([]) == []


# ---------------------------------------------------------------------------
# Missing identity
# ---------------------------------------------------------------------------


def test_hits_without_a_parent_id_are_not_capped_against_each_other():
    """``None`` is "unknown", never a shared runbook.

    Grouping on a missing id would cap unrelated documents against one another
    — the falsy-vs-absent trap ``_find_live_draft_owning`` names for its own id
    filter. Each such hit counts only against the total.
    """
    # A pool SHORTER than the budget is what separates the two behaviours.
    # On a full pool the backfill refills to the budget either way, so counting
    # entries cannot tell "not grouped" from "grouped, then backfilled" — the
    # first version of this test asserted exactly that and was inert against the
    # mutation it existed to catch.
    admitted = _admit_diverse([hit(None), hit(None), hit(None), hit("A")])

    # Not grouped: every hit is admitted in rank order, A last.
    assert parents(admitted) == [None, None, None, "A"]


def test_a_missing_id_does_not_free_a_real_runbooks_share():
    admitted = _admit_diverse([hit("A"), hit(None), hit("A"), hit("A"), hit("B")])

    assert parents(admitted) == ["A", None, "A", "B", "A"]


# ---------------------------------------------------------------------------
# The budget itself
# ---------------------------------------------------------------------------


def test_never_renders_more_than_the_budget():
    for pool in ([hit("A")] * 20, [hit(str(i)) for i in range(20)]):
        assert len(_admit_diverse(pool)) == KB_CONTEXT_MAX_ENTRIES


def test_the_render_budget_is_reachable_from_the_pool():
    """A budget larger than the fetch would be unreachable by construction."""
    assert KB_CONTEXT_MAX_ENTRIES <= KB_PREFETCH_FETCH_LIMIT
    assert KB_CONTEXT_MAX_PER_RUNBOOK < KB_CONTEXT_MAX_ENTRIES


def test_the_renderer_can_show_every_admitted_entry():
    """``context_builder`` slices the combined list; the cap must fit inside it.

    If ``KB_CONTEXT_MAX_ENTRIES`` outgrew that slice, the extra entries would be
    admitted, logged and counted — and then silently dropped before the prompt.
    """
    import inspect

    from faultmaven.core.investigation.prompts import context_builder

    source = inspect.getsource(context_builder.build_investigation_context)
    assert (
        "all_kb_results[:5]" in source
    ), "the renderer's slice moved; re-check it against KB_CONTEXT_MAX_ENTRIES"
    assert KB_CONTEXT_MAX_ENTRIES <= 5
