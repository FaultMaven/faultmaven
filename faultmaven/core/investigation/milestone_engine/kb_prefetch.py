import re

KB_PREFETCH_FETCH_LIMIT = 10
KB_CONTEXT_MAX_ENTRIES = 5

# How many chunks from ONE runbook are admitted BEFORE other runbooks get a
# turn (#1379). A preference, not a bound: the backfill below deliberately
# exceeds it rather than hand back budget, so lowering this does NOT cap how
# much of one runbook can reach the prompt when nothing else clears the
# floor. What it bounds is how far one runbook can crowd out ANOTHER.
#
# A chunk is a ``### Cause``, and the admission slice used to be a flat
# ``relevant[:KB_CONTEXT_MAX_ENTRIES]`` — blind to which runbook each chunk came
# from. Since a runbook carries 3-10 causes, the entries the model saw were
# routinely several causes of ONE runbook while a DIFFERENT runbook the query
# also needed sat in the pool unrendered. Measured over the labelled corpus in
# ``tests/eval/kb_retrieval/``: the median query drew 7 of the 10 pool slots from
# a single runbook, 5 of 16 drew all 10, and **22 of 23 expected runbooks
# reached the pool while only 17 reached the prompt**. The five that did not
# were not retrieval failures — they were discarded at the render step by
# another runbook's causes.
#
# The pair (5, 2) is a measured point, not a guess. Over that corpus:
#
#   render  cap   coverage   depth on the top runbook
#        3  none    17/23     2.44   <- what shipped
#        3     1    20/23     1.00
#        5  none    19/23     3.56
#        5     2    21/23     1.88   <- this
#
# Both levers are load-bearing. Widening alone reaches only 19/23 — the extra
# slots go to MORE causes of the same runbook. Capping alone buys the coverage
# but collapses depth on the runbook that matched best, which is the one the
# model most needs several causes from. Zero queries regress under (5, 2).
#
# Applied to the pool already fetched, so this costs no extra query, embedding
# or round trip. The prompt cost is 2 extra entries, each a ``title`` plus the
# search ``snippet``. NOT ``KB_MAX_SOLUTION_CHARS``: that truncation reads
# ``res.get("solution")`` and a pre-fetch entry has no ``solution`` key, so
# the 800-char bound is dead code for this channel and quoting it would give a
# future reader a number to re-size against that was never in effect.
KB_CONTEXT_MAX_PER_RUNBOOK = 2


_CHUNK_HEADING_RE = re.compile(r"^\s*(#{2,4})[ \t]+(.+?)[ \t]*$", re.MULTILINE)


def _chunk_label(snippet: str) -> str:
    """The markdown heading a retrieved chunk opens with, if it has one.

    Chunking is structure-aware, so a chunk IS a section — ``### Cause C: …``,
    ``## Symptom Recognition``, ``### Step 7: …``. The pre-fetch entry carries
    the runbook's TITLE, which is the same for every chunk of it, so once more
    than one chunk of a runbook can be rendered the prompt shows repeated
    identical ``MATCH n: <title>`` blocks distinguished only by their snippet.
    A model can read that as several corroborating sources rather than several
    parts of one document.

    Returns "" when the chunk does not open with a heading; the caller then
    renders the title alone, exactly as before.
    """
    match = _CHUNK_HEADING_RE.search(snippet or "")
    return match.group(2).strip() if match else ""


def _admit_diverse(ranked: list) -> list:
    """Take up to ``KB_CONTEXT_MAX_ENTRIES`` hits, preferring runbook diversity.

    Two passes over one ranked list. The first admits at most
    ``KB_CONTEXT_MAX_PER_RUNBOOK`` hits from any single runbook; the second
    BACKFILLS any slots still empty from the hits the cap skipped, in their
    original rank order.

    The backfill is what keeps the cap free. Without it, a query whose answer
    genuinely lives in ONE runbook — the whole pool from a single document, which
    is 5 of 16 queries on the labelled corpus — would render fewer entries than
    the budget allows and the model would see LESS than before the cap existed.
    Diversity is a preference between candidates, not a reason to hand back
    budget: the cap exists because a second relevant runbook was being crowded
    out, and where there is no second runbook there is nothing to protect.

    Rank order is preserved throughout — hits are skipped, never reordered — so
    a runbook that owns the answer still leads.

    Hits whose ``parent_document_id`` is absent are never grouped together. The
    id is what identifies a runbook, and treating "unknown" as a shared key
    would cap unrelated documents against one another — the same falsy-vs-absent
    trap ``_find_live_draft_owning`` states for its own id filter. Each such hit
    counts only against the total.
    """
    seen: dict = {}
    chosen: list = []
    deferred: list = []

    for index, hit in enumerate(ranked):
        if len(chosen) == KB_CONTEXT_MAX_ENTRIES:
            break
        parent = getattr(hit, "parent_document_id", None)
        if parent is not None:
            if seen.get(parent, 0) >= KB_CONTEXT_MAX_PER_RUNBOOK:
                deferred.append((index, hit))
                continue
            seen[parent] = seen.get(parent, 0) + 1
        chosen.append((index, hit))

    if len(chosen) < KB_CONTEXT_MAX_ENTRIES:
        chosen.extend(deferred[: KB_CONTEXT_MAX_ENTRIES - len(chosen)])

    # Re-sorted into rank order. The backfill appends what the cap skipped, so
    # without this the returned list is not monotonic in rank — and rank order
    # is not cosmetic here. ``context_builder`` head-truncates the KB section
    # under budget pressure ("rank-ordered best-first, so keep='head'"), so a
    # lower-ranked backfilled hit sitting early would survive while better ones
    # were cut; the prompt numbers the entries ``MATCH 1..n``, implying
    # descending relevance; and the pre-fetch log prints ``score=`` in list
    # order. Selection is the cap's job, ORDER is the reranker's.
    chosen.sort(key=lambda pair: pair[0])
    return [hit for _, hit in chosen]


# Cosine floor a pre-fetched runbook must clear to enter `case.kb_context`.
# Same scale, corpus and calibration as
# ``UnifiedKBConfig.relevance_threshold`` — this path reads the identical
# ``KnowledgeVectorStore.search`` score, so the two must move together; see that
# docstring for the measured distribution the number comes from.
#
# This was 0.3 and shared the #1072 defect: the score it filters was not cosine
# but ``2*cos - 1``, making it a cosine floor of 0.65 that silently dropped
# on-topic runbooks. Quieter than the QA-tool symptom the issue was opened on —
# nothing is logged and no message reaches the model, the prefetched context is
# simply thinner than it should be — and on this path that starves the
# symptom-verification context.
KB_PREFETCH_RELEVANCE_THRESHOLD = 0.5
