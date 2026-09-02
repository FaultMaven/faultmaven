#!/usr/bin/env python3
"""#1144 corroboration measurement — offline, LLM-free, retrieval-level.

The re-runnable artifact behind ``KB_SEED_MIN_CORROBORATING_CHUNKS``. The seeder
asserts a retrieved cause to the user as a **candidate root cause**, and #1144 is
what happens when that assertion rests on rank alone: a thin problem statement
flattens the score distribution and whichever runbook owns a cause-heading chunk
in the band gets promoted. This driver is how the guard that replaced rank was
chosen, and it is the thing to re-run before re-sizing the threshold.

**It measures a guard, not a model.** No server, no provider key, no LLM — it
needs only an ingested KB (ChromaDB collection + the ``knowledge_items`` rows),
so it is deterministic and re-runnable offline. That also bounds what it can
say: it measures ADMISSION (which runbooks' causes are allowed to seed), never
whether the investigation that follows is any good. The live flag-ON driver
(``run_seed_eval.py``) is what covers the latter.

Not a CI test — it depends on the ingested corpus, and its numbers are corpus
facts rather than invariants. The invariants it motivated ARE pinned in CI, in
``tests/unit/core/investigation/test_kb_cause_seeder_seams.py``.

Usage:
    python run_corroboration_eval.py <mode> [--chroma DIR] [--db PATH]
                                     [--statements PATH] [--json PATH]

Modes:
    guards   compare candidate admission guards head to head over every
             candidate seed — the table that ruled out a score floor and the
             kb_context cross-check, and selected corroboration.
    sweep    sweep a minimum-score floor and report what each value keeps and
             drops, per population. This is the evidence that NO floor separates
             them; keep it runnable so the claim can be re-checked, not
             re-asserted.
    grounding compare pure-vector, hybrid, and hybrid + the #1272 grounding
             gate over the same statements, applying the ENGINE's
             ``kb_hit_grounding`` rather than a copy. Prints the per-verdict
             decision rate with its denominator, so an arm that decides nothing
             is visible as such. Re-run it, in BOTH term-index states
             (``--no-term-index``), before changing what grounds a seed.
             Guarded against the vacuous zero: the corpus is asserted loaded
             and a positive control must fire in the same run.
    e2e      drive the REAL wrapper (_prefetch_kb_context ->
             _seed_candidate_causes_from_kb) with live retrieval and print which
             runbooks actually seed, with the guard off vs on.

Reading the output: a corpus where every runbook is long (the shipped pack's
smallest is 9 chunks) cannot exercise the length-relative half of the rule —
that is exactly the blind spot that let the first cut of #1144 ship a flat
threshold which would have made compact personal runbooks permanently
unseedable. If you extend the corpus, extend it with short documents.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

DEFAULT_CHROMA = os.path.join(REPO_ROOT, "data", "chroma-kb")
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "faultmaven.db")
DEFAULT_STATEMENTS = os.path.join(
    os.path.dirname(__file__), "corroboration-statements.json"
)
KB_COLLECTION_NAME = "faultmaven_kb"


# ---------------------------------------------------------------------------
# Corpus access
# ---------------------------------------------------------------------------
class Corpus:
    """Titles and ``metadata["causes"]`` for the ingested KB, read once."""

    def __init__(self, db_path):
        db = sqlite3.connect(db_path)
        self.titles = {
            i: t for i, t in db.execute("select item_id, title from knowledge_items")
        }
        self.causes = {}
        for item_id, meta in db.execute(
            "select item_id, metadata from knowledge_items"
        ):
            try:
                self.causes[item_id] = (json.loads(meta or "{}") or {}).get(
                    "causes"
                ) or []
            except (TypeError, ValueError):
                self.causes[item_id] = []

    def title(self, item_id):
        return self.titles.get(item_id, "?")


async def _store(chroma_dir):
    """A warmed KnowledgeVectorStore over the local persistent KB.

    The embedder is warmed FIRST: BGE-M3 takes ~15s to load and
    ``embedding_guard`` times out at 10s, so an unwarmed first search fails with
    KNOWLEDGE_EMBEDDER_TIMEOUT rather than returning anything.
    """
    import chromadb

    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )
    from faultmaven.infrastructure.model_cache import model_cache

    await model_cache.aembed_query("warmup")
    return KnowledgeVectorStore(chromadb.PersistentClient(path=chroma_dir))


async def retrieve(store, query):
    """The turn's relevance-filtered hits, exactly as the prefetch produces them."""
    from faultmaven.core.investigation.milestone_engine import (
        KB_PREFETCH_FETCH_LIMIT,
        KB_PREFETCH_RELEVANCE_THRESHOLD,
    )
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KB_COLLECTION,
        _read_stamped_cause_letters,
        _read_total_chunks,
        _strip_chunk_suffix,
    )

    raw = await store.search(
        collection_name=KB_COLLECTION,
        query=query,
        k=KB_PREFETCH_FETCH_LIMIT,
        where={"scope": "global"},
    )
    hits = []
    for hit in raw:
        if hit.get("score", 0.0) < KB_PREFETCH_RELEVANCE_THRESHOLD:
            continue
        meta = hit.get("metadata") or {}
        hits.append(
            SimpleNamespace(
                chunk_id=hit.get("id"),
                score=hit["score"],
                # Metadata first, else the chunk id with its "_chunk_N" suffix
                # stripped — as knowledge_service resolves it. `candidates`
                # drops parentless hits, so reading only the metadata key
                # would under-count against production here too.
                parent_document_id=(
                    meta.get("parent_document_id") or _strip_chunk_suffix(hit.get("id"))
                ),
                total_chunks=_read_total_chunks(meta),
                letters=_read_stamped_cause_letters(meta, hit.get("content") or ""),
            )
        )
    return hits


async def retrieve_hybrid(store, query):
    """The same turn, through the HYBRID path the prefetch now uses (#1272).

    Carries the grounding evidence the seeding gate reads — ``term_coverage``
    and ``identity_terms_in_query`` — which only the reranker can compute. The
    field NAMES are the engine's, not this driver's, so ``kb_hit_grounding``
    can be applied to these objects directly instead of re-implemented here.
    """
    from faultmaven.core.investigation.milestone_engine import (
        KB_PREFETCH_FETCH_LIMIT,
        KB_PREFETCH_RELEVANCE_THRESHOLD,
    )
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KB_COLLECTION,
        _read_stamped_cause_letters,
        _read_total_chunks,
        _strip_chunk_suffix,
    )

    raw = await store.hybrid_search(
        collection_name=KB_COLLECTION,
        query=query,
        k=KB_PREFETCH_FETCH_LIMIT,
        where={"scope": "global"},
        min_score=KB_PREFETCH_RELEVANCE_THRESHOLD,
    )
    hits = []
    for hit in raw:
        meta = hit.get("metadata") or {}
        hits.append(
            SimpleNamespace(
                chunk_id=hit.get("id"),
                score=hit["score"],
                # Metadata first, else the chunk id with its "_chunk_N" suffix
                # stripped — as knowledge_service resolves it. `candidates`
                # drops parentless hits, so reading only the metadata key
                # would under-count against production here too.
                parent_document_id=(
                    meta.get("parent_document_id") or _strip_chunk_suffix(hit.get("id"))
                ),
                total_chunks=_read_total_chunks(meta),
                letters=_read_stamped_cause_letters(meta, hit.get("content") or ""),
                term_coverage=hit.get("term_coverage"),
                identity_terms_in_query=hit.get("identity_terms_in_query") or [],
            )
        )
    return hits


def candidates(hits):
    """Per cause-naming runbook: best matched-cause score and the guard inputs.

    Mirrors the wrapper's fold. Kept separate from the engine so a candidate that
    the guard REJECTS is still visible — the cost of a guard cannot be measured
    from its survivors.
    """
    from faultmaven.core.investigation.milestone_engine import KB_CONTEXT_MAX_ENTRIES

    in_context = {h.parent_document_id for h in hits[:KB_CONTEXT_MAX_ENTRIES]}
    chunks, best, length = {}, {}, {}
    for hit in hits:
        parent = hit.parent_document_id
        if not parent:
            continue
        chunks.setdefault(parent, set()).add(hit.chunk_id)
        if hit.total_chunks:
            length[parent] = hit.total_chunks
        for letter in hit.letters:
            per = best.setdefault(parent, {})
            per[letter] = max(per.get(letter, -1.0), hit.score)
    out = []
    for parent, per_letter in best.items():
        out.append(
            SimpleNamespace(
                parent=parent,
                score=max(per_letter.values()),
                chunks=len(chunks[parent]),
                total_chunks=length.get(parent),
                in_context=parent in in_context,
            )
        )
    out.sort(key=lambda c: c.score, reverse=True)
    return out


def labelled(corpus, cand, expected):
    """True if this candidate is an on-domain seed for the statement."""
    title = corpus.title(cand.parent).lower()
    return any(fragment.lower() in title for fragment in expected)


async def collect(store, corpus, statements):
    """Every candidate seed the corpus produces, labelled ON/OFF domain.

    Only candidates that would actually be CONSULTED are collected
    (MAX_SEEDED_RUNBOOKS): a runbook ranked below the cap is not a seed under any
    guard, so counting it would flatter every guard equally and measure nothing.
    """
    from faultmaven.core.investigation.kb_cause_seeder import MAX_SEEDED_RUNBOOKS

    rows = []
    for query, expected in statements["positive"]:
        for cand in candidates(await retrieve(store, query))[:MAX_SEEDED_RUNBOOKS]:
            cand.on_domain = labelled(corpus, cand, expected)
            cand.query = query
            rows.append(cand)
    for query in statements["negative"]:
        for cand in candidates(await retrieve(store, query))[:MAX_SEEDED_RUNBOOKS]:
            cand.on_domain = False
            cand.query = query
            rows.append(cand)
    return rows


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
def mode_guards(rows, corpus):
    from faultmaven.core.investigation.milestone_engine import (
        KB_SEED_MIN_CORROBORATING_CHUNKS,
    )

    on = [r for r in rows if r.on_domain]
    off = [r for r in rows if not r.on_domain]
    print(f"\n{len(rows)} candidate seeds: {len(on)} on-domain, {len(off)} off-domain")
    print(
        f"  on-domain  score range {min(r.score for r in on):.3f}"
        f"-{max(r.score for r in on):.3f}"
    )
    print(
        f"  off-domain score range {min(r.score for r in off):.3f}"
        f"-{max(r.score for r in off):.3f}"
        "   <- overlapping ranges are why no score floor separates them"
    )

    def required(r):
        if r.total_chunks is None:
            return KB_SEED_MIN_CORROBORATING_CHUNKS
        return min(KB_SEED_MIN_CORROBORATING_CHUNKS, r.total_chunks)

    guards = [
        ("baseline (rank only — the #1144 defect)", lambda r: True),
        ("score >= 0.62", lambda r: r.score >= 0.62),
        ("score >= 0.66", lambda r: r.score >= 0.66),
        ("score >= 0.70", lambda r: r.score >= 0.70),
        ("parent also in kb_context/Sources", lambda r: r.in_context),
        ("CORROBORATION (shipped)", lambda r: r.chunks >= required(r)),
    ]
    print(f"\n{'guard':44} {'on-domain kept':>15} {'off-domain kept':>16}")
    for name, keep in guards:
        print(
            f"{name:44} {sum(map(keep, on)):>10}/{len(on):<4} "
            f"{sum(map(keep, off)):>11}/{len(off):<4}"
        )
    print("\nA guard is good when the two columns move APART, not down together.")


def mode_sweep(rows):
    on = [r for r in rows if r.on_domain]
    off = [r for r in rows if not r.on_domain]
    print(f"\n{'floor':>6} {'on-domain kept':>15} {'off-domain kept':>16}")
    for floor in (0.50, 0.55, 0.60, 0.62, 0.64, 0.66, 0.68, 0.70, 0.72, 0.74):
        print(
            f"{floor:6.2f} {sum(1 for r in on if r.score >= floor):>10}/{len(on):<4} "
            f"{sum(1 for r in off if r.score >= floor):>11}/{len(off):<4}"
        )
    print(
        "\nNo row separates the populations: every floor that drops most "
        "off-domain\nseeds drops most on-domain ones too. That is the finding, "
        "not a tuning exercise."
    )


async def mode_e2e(store, corpus, statements):
    """Drive the REAL wrapper, guard off vs on."""
    import faultmaven.core.investigation.milestone_engine as engine_module
    from faultmaven.core.investigation.hypothesis_manager import (
        create_hypothesis_manager,
    )
    from faultmaven.core.investigation.kb_cause_seeder import SEEDED_FROM_RUNBOOK_KEY
    from faultmaven.core.investigation.kb_grounding import (
        KBSeedGrounding,
        kb_hit_grounding,
    )
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine
    from faultmaven.models.common import SearchResult
    from faultmaven.modules.case.contracts import (
        Case,
        CaseSeverity,
        CaseState,
        InquiryData,
        ProblemVerification,
    )
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KB_COLLECTION,
        _read_stamped_cause_letters,
        _read_total_chunks,
        _strip_chunk_suffix,
    )

    class _KS:
        """The two knowledge_service seams the engine path touches."""

        async def search_knowledge(
            self, query, limit=10, filters=None, use_hybrid=False, min_score=None
        ):
            # The engine's prefetch asks for hybrid retrieval and passes a
            # floor (#1272/#1282). A stub that could not accept those keywords
            # made every prefetch raise, the wrapper swallowed it as a failed
            # search, and this mode printed 0 seeds on BOTH arms — a clean
            # "no junk seeded" that measured nothing. Hybrid is what carries
            # the grounding evidence the gate reads, so it is honoured here,
            # not merely tolerated.
            if use_hybrid:
                raw = await store.hybrid_search(
                    collection_name=KB_COLLECTION,
                    query=query,
                    k=limit,
                    where={"scope": "global"},
                    min_score=min_score,
                )
            else:
                raw = await store.search(
                    collection_name=KB_COLLECTION,
                    query=query,
                    k=limit,
                    where={"scope": "global"},
                )
            out = []
            for hit in raw:
                meta = hit.get("metadata") or {}
                # Parent identity resolved as the real seam resolves it:
                # metadata first, else the chunk id with its "_chunk_N" suffix
                # stripped. Reading only the metadata key makes this driver
                # under-seed against production for any chunk indexed without
                # it — the driver-vs-production divergence this mode exists to
                # close, and one its own control cannot see.
                parent = meta.get("parent_document_id") or _strip_chunk_suffix(
                    hit.get("id")
                )
                out.append(
                    SearchResult(
                        document_id=hit.get("id", "unknown"),
                        title=corpus.title(parent),
                        document_type="runbook",
                        tags=[],
                        score=hit["score"],
                        snippet=(hit.get("content") or "")[:200],
                        parent_document_id=parent,
                        total_chunks=_read_total_chunks(meta),
                        matched_cause_letters=_read_stamped_cause_letters(
                            meta, hit.get("content") or ""
                        ),
                        rerank_score=hit.get("rerank_score"),
                        term_coverage=hit.get("term_coverage"),
                        identity_terms_in_query=list(
                            hit.get("identity_terms_in_query") or []
                        ),
                    )
                )
            return out

        async def get_runbook_causes(self, item_id):
            return corpus.causes.get(item_id) or None

    # The wrapper is flag-gated and the flag is OFF by default (fm#1295). This
    # mode exists to measure the seeding path, so it turns the flag on itself
    # rather than asking every operator to export it — and rather than printing
    # a clean zero on both arms when they forget.
    patch(
        "faultmaven.config.settings.get_settings",
        lambda: SimpleNamespace(features=SimpleNamespace(kb_cause_seeder_enabled=True)),
    ).start()

    async def seed_run(description):
        """One statement through the real path: what seeded, and the hits.

        The hits are returned as well as the seeds because the two guards below
        ask different questions of them — what seeded says the path ran, and
        only the hits say whether the GROUNDING gate judged anything.
        """
        engine = MilestoneEngine.__new__(MilestoneEngine)
        engine.knowledge_service = _KS()
        engine.hypothesis_manager = create_hypothesis_manager()
        engine.runbook_kb = None
        case = Case(
            case_id=f"case_{uuid4().hex[:12]}",
            user_id="eval",
            organization_id="eval",
            title="eval",
            description=description,
            state=CaseState.INVESTIGATING,
            inquiry=InquiryData(
                proposed_problem_statement=description,
                problem_statement_confirmed=True,
                decided_to_investigate=True,
            ),
            problem_verification=ProblemVerification(
                symptom_statement=description, severity=CaseSeverity.HIGH
            ),
            current_turn=1,
        )
        relevant = await engine._prefetch_kb_context(case, description, "symptom")
        await engine._seed_candidate_causes_from_kb(case, relevant)
        seeded = sorted(
            {
                corpus.title((n.metadata or {}).get(SEEDED_FROM_RUNBOOK_KEY))
                for n in case.causal_nodes.values()
                if (n.metadata or {}).get(SEEDED_FROM_RUNBOOK_KEY)
            }
        )
        return seeded, relevant

    async def seeded_for(description):
        seeded, _ = await seed_run(description)
        return seeded

    # --- guard: the path is LIVE and the GATE decided, in this run --------
    # TWO failures print the same clean table, and the seed columns tell them
    # apart from neither:
    #
    #   (a) the path does not run. A prefetch that raises is swallowed by the
    #       wrapper as a failed search, and a seeder handed no hits seeds
    #       nothing, so both arms read 0/16 and 0/8 — which looks like a guard
    #       working perfectly. That is how this mode ran from #1282 until the
    #       stub above learned ``use_hybrid``.
    #   (b) the path runs but GROUNDING does not decide. A hit that carries no
    #       ``term_coverage`` is UNMEASURED, and UNMEASURED passes through by
    #       design — so a prefetch that fell back to pure vector produces a
    #       full, healthy-looking table in which the gate this mode reports on
    #       admitted everything and corroboration alone did the work.
    #
    # A positive control must therefore seed with the corroboration guard off
    # AND show the gate reaching a real verdict on the same hits, before any
    # row is printed. Same shape as mode_grounding's guards, one statement
    # instead of the whole set.
    control = statements["positive"][0][0]
    with patch.object(engine_module, "KB_SEED_MIN_CORROBORATING_CHUNKS", 1):
        probe, probe_hits = await seed_run(control)
    verdicts = [kb_hit_grounding(h) for h in probe_hits]
    tally = ", ".join(
        f"{v.value} {verdicts.count(v)}" for v in KBSeedGrounding if v in verdicts
    )
    print(
        f"[guard] positive control {control[:48]!r}... seeds {probe} "
        f"from {len(probe_hits)} hits ({tally or 'no hits'})"
    )
    if not probe:
        sys.exit(
            "COULD NOT ASK: the positive control seeded nothing with the "
            "corroboration guard off, so every row below would read 0 on both "
            "arms and mean nothing. Any of these empties it, and the counts "
            "below cannot tell them apart: the KB at --chroma/--db is "
            "unseeded, empty or not scoped global; the embedder did not "
            "answer (an unwarmed BGE-M3 trips KNOWLEDGE_EMBEDDER_TIMEOUT); "
            "the stub above drifted from knowledge_service.search_knowledge, "
            "so every prefetch raised and the wrapper swallowed it as a "
            "failed search; the seeder flag is off and this mode's own override "
            "did not take; or the gate now refuses this statement's runbooks "
            "outright, in which case the path ran and this control is what is "
            "stale"
        )
    unmeasured = verdicts.count(KBSeedGrounding.UNMEASURED)
    if unmeasured:
        sys.exit(
            f"COULD NOT ASK: {unmeasured} of {len(verdicts)} control hits "
            f"carry no term_coverage, so the grounding gate judged nothing "
            f"about them — UNMEASURED passes through by design. Only the "
            f"reranker writes that field, so the prefetch took the "
            f"pure-vector path; the rows below would be decided by "
            f"corroboration alone while reading as evidence about grounding"
        )
    if KBSeedGrounding.NAMED not in verdicts:
        sys.exit(
            "COULD NOT ASK: the gate grounded NOTHING on the positive "
            "control, so nothing below was admitted BY grounding — whatever "
            "seeds is riding on corroboration alone, and the gate's cost in "
            "the rows below is zero by construction"
        )

    for label, threshold in (("GUARD OFF (#1144)", 1), ("GUARD ON", None)):
        print("\n" + "=" * 96)
        print(label)
        ctx = (
            patch.object(engine_module, "KB_SEED_MIN_CORROBORATING_CHUNKS", threshold)
            if threshold
            else patch.object(
                engine_module,
                "KB_SEED_MIN_CORROBORATING_CHUNKS",
                engine_module.KB_SEED_MIN_CORROBORATING_CHUNKS,
            )
        )
        on_domain = 0
        with ctx:
            for query, expected in statements["positive"]:
                got = await seeded_for(query)
                hit = any(any(f.lower() in g.lower() for f in expected) for g in got)
                on_domain += bool(hit)
                print(f"  {'ON ' if hit else 'off'} {query[:52]:54} -> {got}")
            junk = 0
            for query in statements["negative"]:
                got = await seeded_for(query)
                junk += bool(got)
                print(f"  {'JUNK' if got else 'none'} {query[:52]:54} -> {got}")
        print(
            f"  == on-domain {on_domain}/{len(statements['positive'])}; "
            f"content-free statements seeding junk {junk}/"
            f"{len(statements['negative'])}"
        )


async def mode_grounding(store, corpus, statements, no_term_index=False):
    """#1272 grounding gate: what the hybrid path + gate seed, vs pure vector.

    Re-runnable evidence for the gate, and the thing to re-run before changing
    what grounds a seed. Same shape as ``guards``: every candidate is collected
    before the gate is applied, so the gate's COST is visible and not only its
    survivors.

    It applies the ENGINE's ``kb_hit_grounding``, never a copy of it. A driver
    that re-implements the predicate reports on a gate it does not share, which
    is how #1285 — an arm whose firings were 36:1 wrong — stayed invisible here
    while this mode said the gate was working.

    Prints the per-ARM decision RATE with its denominator, because "the gate
    turned nothing away" and "the gate is not applying" are the same number in
    the seed columns and different facts.

    ``--no-term-index`` runs the whole pipeline without corpus statistics. Read
    it as an END-TO-END comparison, NOT a controlled one: dropping the index
    also changes which keywords Stage 1 probes with
    (``_extract_search_keywords``) and which weight profile the reranker picks
    (``_query_has_identifier``), so the two runs differ in their candidate sets
    as well as in what ``term_coverage`` means. That is the right question for
    "does the gate still behave in a deployment with no index", and the wrong
    one for "does the index change a verdict". The controlled version of the
    latter holds the candidate set fixed and varies only ``stats``:
    ``test_kb_seed_grounding_reachability_1285.py``
    ::``TestATermIndexOutageCannotSwitchTheGateOff``.

    Guarded against the vacuous zero. A gate that turns everything away and a
    retrieval that returned nothing produce identical counts, so the corpus is
    asserted loaded and a positive control must fire in this same run before any
    number below is printed.
    """
    from faultmaven.core.investigation.kb_cause_seeder import MAX_SEEDED_RUNBOOKS
    from faultmaven.core.investigation.kb_grounding import (
        KBSeedGrounding,
        kb_hit_grounding,
    )
    from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
        KnowledgeVectorStore,
    )

    if no_term_index:
        KnowledgeVectorStore._corpus_term_stats = lambda self, c: None
        print("[mode] term index DISABLED for this run")

    # --- guard 1: the corpus is loaded -------------------------------------
    collection = store._get_or_create_collection(KB_COLLECTION_NAME)
    payload = collection.get(where={"scope": "global"}, include=["metadatas"])
    parents = {m.get("parent_document_id") for m in payload["metadatas"]}
    print(
        f"[guard] corpus: {collection.count()} chunks, {len(parents)} global runbooks"
    )
    if len(parents) < 2:
        sys.exit(
            "COULD NOT ASK: the corpus is empty or unscoped — no zero below means anything"
        )

    # --- guard 2: a positive control, in this run --------------------------
    control = statements["positive"][0][0]
    probe = await retrieve_hybrid(store, control)
    print(f"[guard] positive control {control[:48]!r}... -> {len(probe)} hits")
    if not probe:
        sys.exit("COULD NOT ASK: the positive control returned nothing")

    totals = {"vector": [0, 0], "hybrid": [0, 0], "hybrid+gate": [0, 0]}
    verdicts = {v: 0 for v in KBSeedGrounding}
    asked = 0
    pairs = [(q, e) for q, e in statements["positive"]]
    pairs += [(q, []) for q in statements["negative"]]
    for query, expected in pairs:
        asked += 1
        arms = {
            "vector": await retrieve(store, query),
            "hybrid": await retrieve_hybrid(store, query),
        }
        ok_parents = set()
        for hit in arms["hybrid"]:
            verdict = kb_hit_grounding(hit)
            verdicts[verdict] += 1
            if verdict is not KBSeedGrounding.UNGROUNDED:
                ok_parents.add(hit.parent_document_id)
        arms["hybrid+gate"] = [
            h for h in arms["hybrid"] if h.parent_document_id in ok_parents
        ]
        for name, hits in arms.items():
            for cand in candidates(hits)[:MAX_SEEDED_RUNBOOKS]:
                on = labelled(corpus, cand, expected) if expected else False
                totals[name][0 if on else 1] += 1

    print(f"\n[guard] evaluated {asked} statements (denominator, not assumed)")
    print(f"\n{'arm':<14}{'on-domain seeds':>18}{'off-domain seeds':>19}")
    for name, (ok, bad) in totals.items():
        print(f"{name:<14}{ok:>18}{bad:>19}")

    judged = sum(verdicts.values())
    print(f"\ngrounding verdicts over {judged} retrieved chunks (the denominator):")
    for verdict, n in verdicts.items():
        share = f"{100.0 * n / judged:.1f}%" if judged else "n/a"
        print(f"  {verdict.value:<12}{n:>6}{share:>9}")
    if not judged:
        sys.exit("COULD NOT ASK: no chunk reached the gate at all")
    if verdicts[KBSeedGrounding.UNMEASURED] == judged:
        sys.exit(
            "COULD NOT ASK: every chunk was UNMEASURED — no reranker ran, so the "
            "gate did not apply to anything and the seed columns above say "
            "nothing about it"
        )
    # --- guard 3: a positive control on the GATE ---------------------------
    # The two guards above prove the corpus loaded and that retrieval returned
    # something. Neither says the gate DECIDED anything: a gate that grounds
    # nothing prints a clean table of zeros and exits 0, which reads as "the
    # guard is working" and is the failure mode this whole mode exists to make
    # visible. The seed columns cannot distinguish it either — they fall to
    # zero for a gate that is broken and for a corpus that covers nothing.
    if not verdicts[KBSeedGrounding.NAMED]:
        sys.exit(
            "COULD NOT ASK: the gate grounded NOTHING across every statement. "
            "Either the corpus is unrelated to them or the gate is broken, and "
            "the numbers above cannot tell you which"
        )
    if not verdicts[KBSeedGrounding.UNGROUNDED]:
        sys.exit(
            "COULD NOT ASK: the gate turned NOTHING away, so it is not "
            "discriminating on this input and its 'cost' below is zero by "
            "construction"
        )
    print(
        "\nThe gate is sized on the on-domain column: it must not fall. A ground "
        "that decides nothing but UNGROUNDED->NAMED is not a ground; #1285 "
        "removed one that decided 37 chunks, 36 of them wrong."
    )


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=("guards", "sweep", "e2e", "grounding"))
    ap.add_argument("--chroma", default=DEFAULT_CHROMA)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--statements", default=DEFAULT_STATEMENTS)
    ap.add_argument("--json", dest="json_out")
    ap.add_argument(
        "--no-term-index",
        action="store_true",
        help="grounding mode: run the whole pipeline with the corpus term "
        "index forced off. End-to-end, not controlled — it also changes Stage-1 "
        "keyword probes and the reranker weight profile. A gate validated in "
        "only one of the two states is not validated, but attributing a "
        "difference between them to term_coverage alone is not supported by "
        "this flag.",
    )
    args = ap.parse_args()

    for path in (args.chroma, args.db, args.statements):
        if not os.path.exists(path):
            sys.exit(f"missing: {path} (needs an ingested KB; see the README)")

    statements = json.load(open(args.statements))
    corpus = Corpus(args.db)
    store = await _store(args.chroma)

    if args.mode == "e2e":
        await mode_e2e(store, corpus, statements)
        return

    if args.mode == "grounding":
        await mode_grounding(store, corpus, statements, args.no_term_index)
        return

    rows = await collect(store, corpus, statements)
    if args.mode == "guards":
        mode_guards(rows, corpus)
    else:
        mode_sweep(rows)
    if args.json_out:
        json.dump(
            [
                {
                    "query": r.query,
                    "runbook": corpus.title(r.parent),
                    "on_domain": r.on_domain,
                    "score": round(r.score, 4),
                    "chunks": r.chunks,
                    "total_chunks": r.total_chunks,
                    "in_context": r.in_context,
                }
                for r in rows
            ],
            open(args.json_out, "w"),
            indent=1,
        )
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    asyncio.run(main())
