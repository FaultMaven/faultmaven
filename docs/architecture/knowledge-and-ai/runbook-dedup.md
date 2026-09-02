# Runbook Deduplication

How FaultMaven answers "does the knowledge base already hold a runbook like
this?" — the check behind the terminal-turn runbook suggestion and the
`GET /cases/{id}/report-recommendations` route. (fm#1030)

## What a runbook is, for dedup purposes

A runbook is a **published KB item**: a `knowledge_items` row whose ChromaDB
chunks carry `document_type == "runbook"`. All three publishers — the shipped
pack (`bootstrap/kb_init.py`), uploads/manual creation, and the
case-conversion flywheel (`ConversionService`) — reach ChromaDB through the
single live writer, `KnowledgeService.ingest_runbook` →
`_index_document_in_vector_store`, which writes N chunk rows per runbook
carrying `document_type`, `scope`, `owner_id`, `title`, `parent_document_id`
and chunk tracking.

Dedup (`RunbookKnowledgeBase`) **reads exactly those rows**. There is no
separate runbook collection, no parallel runbook index, and no
runbook-specific metadata schema:

- One collection (`faultmaven_kb`) holds KB documents and runbooks alike;
  `document_type` is the discriminator.
- One writer publishes; dedup is read-only. A second writer would be a second
  spelling of "published runbook", free to drift from the first — the previous
  design had exactly that (`index_runbook`, dead, stamping a `report_type` key
  no live row carried), so every dedup query structurally returned nothing
  while every resolution proposed a new runbook.
- Identity is the KB item's: results are honest references
  (`item_id` = `parent_document_id`, `title`, `scope`, `similarity_score`),
  never reconstructed report rows.

## Whose scope governs

A similarity query names no id and no owner, so the caller-supplied KB scope
filter (`build_kb_scope_filter`: global ∪ owned ∪ team-shared, ADR-011 D3 —
the same allowlist as every other KB read) is the only isolation on the path.
**Each call site scopes on the principal who will act on the answer:**

- **The API route scopes on the requester** (`current_user`): the
  recommendation answers "should *you* generate a runbook?", covering what the
  requester can read.
- **The engine scopes on the case owner** (`case.user_id`): the terminal-turn
  suggestion points at the owner's Dashboard and personal corpus.

Accepted consequence: the two call sites may legitimately disagree on the same
case.

Invariants:

- `search_runbooks`/`search_by_text` **refuse a falsy scope filter with a
  typed error** (`RUNBOOK_SEARCH_UNSCOPED`) rather than querying unscoped.
- A scope that cannot be **resolved** is a failed dedup, not a narrowed one.
  The engine's resolver does not swallow a team-arm failure (deliberate
  divergence from the engine KB pre-fetch, which degrades — correct for
  seeding, wrong for a "checked, nothing similar" claim); the route raises
  `RUNBOOK_SCOPE_RESOLUTION_FAILED`, rendered as its 503 refusal.
- Standalone is not a failure: without a team service, teams do not exist, and
  the empty team arm is the correct scope.

## The search

`{"$and": [{"document_type": "runbook"}, <scope_filter>]}` against
`faultmaven_kb`, with the scope filter composing as one operand whether it is
a bare condition (global-only) or an `$or` of arms. One runbook is N chunk
rows, so the search fetches `top_k × 3` chunks, collapses by
`parent_document_id` taking the **max** chunk similarity per runbook, sorts
descending and truncates to `top_k`.

Failure semantics (all typed `KnowledgeBaseError`s; none may be rendered as
"no similar runbooks found"):

- `RUNBOOK_SEARCH_FAILED` — the vector query itself failed.
- `RUNBOOK_RESULTS_UNREADABLE` — rows matched but an unreadable candidate
  outranks every readable one. Identity keys are read strictly
  (`metadata[...]`, no defaults); `title` is truthiness-gated at write, so an
  empty-titled document's chunks genuinely carry no readable identity. A tie
  is not a refusal, and an unreadable row below the best readable match
  changes nothing. The remedy is re-indexing, never retrying — both consumers
  (`terminal_transitions`, the route's 503 detail) branch on the code.
- The parse runs **outside** the retry/circuit-breaker wrapper: a
  deterministic parse failure is not retried and not charged to the breaker
  the read path shares.

## The match verdict: no coverage claim, no silent creation

Best-chunk-max similarity detects **overlap**, not whole-runbook equivalence,
and the retired ≥0.85 thresholds (`action="reuse"`, the engine's
`EXISTING_COVERS`) never fired against any real distribution. Two invariants
follow, and they pull in opposite directions on purpose:

- **A match is never asserted to cover the case.** Auto-suppressing generation
  on an uncalibrated overlap signal risks a false "already covered" — an
  incorrect conclusion, which FaultMaven does not accept. The wording always
  states overlap, never coverage.
- **A match is never silently created past.** Preventing duplicate runbooks is
  what dedup is *for*; naming a likely duplicate and creating the draft on the
  same turn would make the question rhetorical.

So there is a **single band**, and it stops: a best-chunk match ≥ 0.70 yields
the engine's `SIMILAR_FOUND` verdict — the turn names the candidate by title
and score, creates **nothing**, and offers a *"Generate a new runbook anyway"*
DECIDE affordance whose payload routes back with `dedup_confirmed=True`; only
that explicit confirmation creates the draft. On the API route the same band is
`action="review_or_generate"` with the candidate as an honest KB-item ref (the
route never creates anything itself). Dedup-*failure* caveats do not stop the
engine's creation — the case is runbook-worthy and only the duplicate check is
uncertain, so the draft proceeds with the caveat stated (#944).

The top similarity score is emitted as a log metric
(`runbook.dedup_top_similarity`) so the band can be calibrated against real
distributions later. The one auto-suppression that remains is provenance-based
(engine Step 0): a case resolved by *applying* runbook X needs no new runbook
— a direct signal, not a similarity guess.

## Wiring

- **One reader, bound to the writer's collection by construction.** The
  container builds a single `RunbookKnowledgeBase`
  (`create_runbook_dedup_kb`) that both call sites use. The production KB
  writer is `KnowledgeVectorStore` (`knowledge_vector_store or vector_store`
  in the container), whose `add_documents` targets the hardcoded
  `KB_COLLECTION` — so the reader is built over the same KB ChromaDB client
  bound to that same constant (`RunbookKnowledgeBase.over_kb_collection`),
  NOT over the settings-named `container.vector_store`, which diverges the
  moment `CHROMADB_COLLECTION` is overridden. When the writer is the fallback
  `vector_store`, the reader is that same object; when the reader cannot be
  bound to the writer's collection, it is `None` (dedup honestly skipped)
  rather than mis-bound. A reader/writer collection split would silently
  reinstate the empty-result dedup this design removes.
- The engine receives that reader by **explicit constructor injection**
  (`MilestoneEngine(runbook_kb=...)`). `None` is legitimate — local dev
  without ChromaDB — and yields the honest "dedup did not run" caveat, as
  does a missing scope resolver. There is no attribute probing: the previous
  `hasattr(knowledge_service, "runbook_kb")` was permanently False and
  silently disabled dedup.
- The route reads the same container instance (`container.runbook_kb`) and
  refuses with 503 when the search cannot run or cannot be read
  (`RESULTS_UNREADABLE_CODE` selects the re-index remediation text).
  `GET /cases/{case_id}/report-recommendations` is the only recommendation
  surface: the reports-module twin (`GET /reports/recommendations/{case_id}`),
  which was mounted with its service unconditionally `None`, was removed
  (fm#1036).
- Dedup never routes through `KnowledgeService.search_documents`: that method
  swallows `KnowledgeBaseError` into `{"total_results": 0, "results": []}`,
  which a dedup caller would read as "checked, nothing similar" (#944's
  fail-open, wholesale).
