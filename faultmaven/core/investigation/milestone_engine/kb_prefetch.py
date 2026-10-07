"""The KB push: pre-fetching runbook matches into the turn's prompt ahead of generation, and the limits and relevance floor that bound it."""

import logging
import re

from faultmaven.modules.case.contracts import (
    Case,
)

logger = logging.getLogger(__name__)

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


class KbPrefetcher:
    """Pre-fetches KB context for the current turn — the deterministic push half of retrieval, run ahead of generation so the prompt carries relevant runbook matches even when the model never calls kb_qa."""

    def __init__(self, *, deps) -> None:
        self.deps = deps

    async def prefetch_kb_context(
        self,
        case: "Case",
        query: str,
        trigger: str,
    ) -> None:
        """Search KB for runbooks matching the query, store on case.

        Args:
            case: Case to update
            query: Search query (problem statement or root cause)
            trigger: What triggered this search ("symptom" or "root_cause")

        Side effect only: writes the top ``KB_CONTEXT_MAX_ENTRIES`` admitted
        hits to ``case.kb_context`` (or clears it on a miss) for the prompt
        builder. Nothing consumes a return value since the KB cause seeder
        was removed (fm#1295).
        """
        if not self.deps.knowledge_service:
            return None

        # Policy gate on the PUSH channel (fm#1360, Option B). Off means the
        # search does not run AT ALL — the cost this gate exists to control is
        # the hybrid retrieval as much as the prompt surface it produces.
        #
        # Clearing rather than merely returning: ``case.kb_context`` is
        # persisted (it must be, or the push can never reach a prompt — see the
        # repository metadata blob), so a case that accumulated context while
        # the push was enabled would otherwise keep standing runbooks in its
        # turn response and telemetry after the operator turned the push off.
        # "Off" has to mean off for the case, not only for new searches.
        #
        # The prompt is guarded independently in ``context_builder`` — that is
        # the seam that decides what the model actually sees, and it must hold
        # for a case reloaded with context already on it.
        from faultmaven.config.settings import get_settings

        if not get_settings().knowledge.kb_prefetch_enabled:
            case.kb_context = None
            return None

        try:
            # Owner-aware scope. The pre-fetch may
            # read only what the case OWNER can read: global (platform-curated)
            # plus the owner's own personal KB. This completes the flywheel
            # loop — a user's resolved cases, converted to personal runbooks,
            # seed that user's own future investigations — while preserving
            # strict cross-user isolation: the personal condition is keyed on
            # the owner's user_id, so user B's case can never surface user A's
            # personal runbooks. Without this filter search_knowledge defaults
            # to global-only, so personal (case-generated) runbooks never seed.
            #
            # The team arm resolves the case OWNER's shared-kb-id allowlist —
            # keyed on case.user_id, NOT the session user, so user B's case can
            # never surface user A's runbooks — via the same share table → id
            # allowlist the QA path uses (resolve_shared_kb_ids, ADR-013 §D4),
            # passed as the second arg to build_kb_scope_filter. It is inert in
            # practice until case→runbook conversion emits team-shared runbooks
            # (there are none to seed yet), and in standalone: team_service is
            # None, so the owner resolves an empty shared set and the scope
            # collapses to global ∪ owner-personal.
            from faultmaven.modules.knowledge.domain.services.knowledge_service import (
                build_kb_scope_filter,
                resolve_shared_kb_ids,
            )

            owner_id = getattr(case, "user_id", None)
            # team_service/share_repository are wired post-construction; use
            # None in standalone: the team arm then resolves empty.
            team_service = self.deps.team_service
            share_repository = self.deps.share_repository
            shared_kb_ids: list[str] = []
            if owner_id and team_service and share_repository:
                try:
                    owner_team_ids = await team_service.list_all_user_team_ids(owner_id)
                    shared_kb_ids = await resolve_shared_kb_ids(
                        share_repository,
                        owner_team_ids,
                        getattr(case, "enterprise_id", None),
                    )
                except Exception:  # noqa: BLE001
                    # Graceful degradation — global ∪ owner-personal still apply.
                    shared_kb_ids = []
            scope_filter = build_kb_scope_filter(owner_id, shared_kb_ids)
            # Fetch KB_PREFETCH_FETCH_LIMIT chunks — the reranker's candidate
            # pool, see the constant — and render only the top
            # KB_CONTEXT_MAX_ENTRIES into the prompt.
            #
            # HYBRID, not pure vector (#1272). An operator writes what they
            # SAW — "cannot write its PID file", "qemu failed to start" — and
            # those words are precisely what an embedding smooths into the
            # neighbourhood of every other "process won't start" runbook. On
            # the shipped pack that put the runbook covering the failure at
            # rank 70 of 91 while the top ten were all Kubernetes. Adding the
            # keyword-constrained arm and the IDF-weighted reranker moves it to
            # rank 1 for the same query, and pure vector search cannot: the
            # fetch limit is applied to CHUNKS before any floor, so no
            # threshold value can admit a chunk ranked 369th.
            results = await self.deps.knowledge_service.search_knowledge(
                query=query,
                limit=KB_PREFETCH_FETCH_LIMIT,
                filters=scope_filter,
                use_hybrid=True,
                # The floor goes in at ADMISSION, not after ranking. Hybrid
                # results are ordered by the reranker's blend, so the filter
                # below would thin this window from the middle: on a measured
                # query it left 2 hits where 10 were asked for. The filter
                # below stays as the
                # authority (and still governs the pure-vector fallback).
                min_score=KB_PREFETCH_RELEVANCE_THRESHOLD,
            )
            relevant = [
                r for r in results or [] if r.score >= KB_PREFETCH_RELEVANCE_THRESHOLD
            ]
            if relevant:
                # `results` is ordered by the reranker's blend; the floor below
                # is applied to `score`, which stays the raw cosine on every
                # path. Two quantities on purpose — an absolute one to admit
                # with, a relative one to order by — so this slice is the top of
                # the RANKING and the filter is a statement about ABSOLUTE
                # similarity. Ordering by cosine instead would discard the
                # keyword and term-overlap evidence that produced the ranking.
                case.kb_context = [
                    {
                        "title": r.title,
                        # Which SECTION of the runbook matched. Two chunks of one
                        # runbook are two entries with the same title, so without
                        # this the prompt cannot tell them apart (#1379 review).
                        "section": _chunk_label(r.snippet),
                        "summary": r.snippet,
                        "score": r.score,
                        "type": getattr(r, "document_type", "runbook"),
                        "parent_document_id": getattr(r, "parent_document_id", None),
                        "trigger": trigger,
                        # The turn this context was fetched on. It stands in
                        # every later prompt until a pre-fetch replaces it, and
                        # the turn response resends it each turn; this is what
                        # tells a client which turn it is NEW on.
                        "fetched_turn": case.current_turn,
                    }
                    for r in _admit_diverse(relevant)
                ]
                # Identity, not just a count (fm#1361). "3 matches" cannot
                # answer "which runbook informed this answer?" or "was
                # retrieval any good?" — both need to know WHICH documents were
                # admitted and at what score, and reconstructing that meant
                # re-running the case. Emitted twice on purpose: in the message
                # for a human reading a log, and under ``extra`` as separate
                # fields for a structured consumer (the root handler is
                # structlog's ProcessorFormatter with ExtraAdder, so these land
                # as top-level keys on the JSON line).
                _kb_ids = [
                    str(r.get("parent_document_id") or "") for r in case.kb_context
                ]
                _kb_scores = [float(r.get("score") or 0.0) for r in case.kb_context]
                logger.info(
                    "KB pre-fetch (%s): %d matches for case %s: %s",
                    trigger,
                    len(case.kb_context),
                    case.case_id,
                    "; ".join(
                        f"{r.get('title') or '(untitled)'}"
                        f" [{r.get('parent_document_id') or 'no-id'}]"
                        f" score={float(r.get('score') or 0.0):.3f}"
                        for r in case.kb_context
                    ),
                    extra={
                        "kb_prefetch_trigger": trigger,
                        "kb_prefetch_hits": len(case.kb_context),
                        "kb_prefetch_top_score": max(_kb_scores),
                        "kb_runbook_ids": _kb_ids,
                        "kb_runbook_titles": [
                            str(r.get("title") or "") for r in case.kb_context
                        ],
                    },
                )
            else:
                # Nothing usable this trigger → clear stale context, so a later
                # trigger's miss cannot leave an earlier trigger's runbooks
                # standing in the prompt as if they still matched.
                #
                # This used to read ``elif results:``, distinguishing "searched
                # and found only weak matches" (clear) from "searched and found
                # nothing at all" (leave alone, in case the search itself had
                # failed). Moving the floor to admission collapsed that
                # distinction — `results` is already floored, so `relevant` is
                # empty exactly when `results` is — which left the branch
                # unreachable and the stale context never cleared.
                #
                # `else` is the right resolution rather than a way to restore
                # the old shape: the hazard the old guard existed for is
                # already handled above. A search that genuinely FAILS raises
                # (the embedder guard turns an unavailable model into an
                # exception rather than an empty list), and the handler below
                # returns without touching `kb_context`. So reaching here means
                # the search ran and produced nothing worth showing, which is
                # precisely when stale context should go.
                case.kb_context = None
            return None
        except Exception:
            logger.warning(
                f"KB pre-fetch ({trigger}) failed for case {case.case_id}",
                exc_info=True,
            )
            return None
