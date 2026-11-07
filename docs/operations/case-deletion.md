# Case deletion

What `DELETE /api/v1/cases/{case_id}` does, when it refuses, and what it can
still miss, and how the knowledge base's draft and document deletes purge their
LLM traces the same way. The full contract of each answer is in the generated
[API reference](../reference/api/README.md).

## What a delete clears

Only the case's creator may delete it. The delete clears every store that
holds the case's data before it deletes the row:

- its LLM traces in Opik, where a trace backend is configured (#2102);
- the session pointers to the case;
- its runbook conversion jobs and drafts (a published runbook is kept);
- its uploaded files;
- its evidence search index (the `case_{case_id}` ChromaDB collection);
- its PII redaction registry.

Then the row goes, in one transaction with its team shares and a
`case_deleted` audit row. If a store cannot be cleared, the delete answers
**500** `DELETE_CASE_ERROR` and keeps the row, so a retry can finish it (#1016).

### LLM traces

Each trace and span of a model call for a case is tagged with the case and its
enterprise, and the delete finds them by those tags. The traces clear first, so
a trace backend failure leaves the case untouched. The delete sends what this
replica holds for the backend, including traces it parked after a connection
failure, then searches traces and spans, then deletes. No purge is queued for
later, so the delete fails when:

- the backend cannot be reached, or the queued traces are not sent in time;
- the backend answers an error, or pages that disagree with their total;
- `OPIK_ENABLED` and a backend URL are set but tracing did not initialize, or
  the Opik SDK is not installed;
- the erase takes longer than 60 s.

With `OPIK_TRACK_DISABLE=true` a delete still purges: the flag stops new spans,
and the traces already recorded can still be deleted.

Without `OPIK_ENABLED` and a backend URL, no process records a trace, and the
audit row names `llm_traces` under `not_configured`.

## Draft and document deletes (#2575)

Each model call that carries an uploaded document's text (its triage, its
analysis, and each failure mode's generation) is tagged with the document's
conversion and its enterprise. Only a document or case conversion makes model
calls, and its job records the failure modes they worked on; a template, a
directory scan and a direct upload (`POST /api/v1/knowledge/documents`) record
none, have no traces, and their deletes purge nothing. Three operations purge a
conversion's traces, with the same steps and refusals as a case delete:

- `DELETE /api/v1/knowledge/conversions/{conversion_id}/drafts/{draft_id}`
  purges the draft's conversion. A refusal answers **500** "Failed to delete
  draft" and leaves the draft as it was.
- `DELETE /api/v1/knowledge/documents/{document_id}` purges the conversion of
  each draft that names the document, read under the caller's row-level
  security. A refusal answers **500** "Failed to delete document" and leaves the
  document as it was. The document's `source_url` plays no part, as the
  uploader writes it. A document that no draft names purges nothing: a built-in,
  whose delete only unpublishes it, or a runbook published from a case whose
  drafts that case's delete removed.
  `POST /api/v1/knowledge/documents/bulk-delete` purges each conversion once
  for the whole batch. After the first refusal, a document whose conversion is
  not yet purged is reported in `errors`, unchanged, without contacting the
  backend again; one with nothing left to purge is still deleted.
- `fm-reset-kb` purges the conversion of each draft naming a runbook, and with
  `--all-drafts` of every draft, before it writes anything. A refusal exits 1
  with nothing written. It then deletes only what that purge covered: a
  runbook or draft written meanwhile, from a conversion it did not purge, is
  kept and reported.

The traces go before any row changes, and no database transaction is open
while the backend is called. The unit is the conversion, not the draft: every
failure mode's prompt holds the whole document, so deleting one of a
conversion's drafts also deletes the traces of the drafts beside it.

These deletes need no in-flight gate: a conversion's model calls all finish
before its draft rows exist, so none of its traces can land after a delete
that found them. They write no audit row, so nothing records a delete made
while tracing is not configured. A knowledge service composed without an
eraser fails every purge rather than skip it.

## Work in flight holds a delete off (#2096)

A turn on the case holds the case's **in-flight lease** from before it writes
anything until it has answered. So does every task a turn starts that outlives
it: the turn's commit, its upload links, evidence vectorization and a runbook
draft. The lease is shared by every API replica: a Redis sorted set,
`case-inflight:{enterprise_id}:{case_id}`, of one member per process, renewed
every 10 s and lapsing 30 s after its last renewal.

| Answer | `x-error-code` | `Retry-After` | Meaning | Client action |
|---|---|---|---|---|
| Delete **409** | `CASE_WORK_IN_PROGRESS` | yes | A turn on the case, or work one started, is still running, or another delete of the case is. Nothing was cleared. | Retry after `Retry-After`; the work may run longer, so the retry may be refused again. |
| Turn **409** | `CASE_DELETE_IN_PROGRESS` | no | A delete of the case is running. Nothing of the turn ran. | Reload the case. While it reads `deleting: true`, show it read-only and read it again (from 2 s, at most every 10 s, for at most 60 s) until it is gone (404) or `deleting` clears. |
| Delete **503** | `CASE_DELETE_UNAVAILABLE` | yes | Whether work is in flight could not be read: Redis is unreachable. Nothing was cleared. | Retry after `Retry-After`. |

How the two sides avoid a race: the delete first writes a tombstone,
`case-deleting:{enterprise_id}:{case_id}`, and only then reads the lease; a
turn first takes the lease, and only then reads the tombstone. Each writes
before it reads, so they cannot both proceed. Both can refuse at once, which
is safe: nothing runs and nothing is cleared, and a retry decides. The
tombstone is renewed while the delete runs, and the delete removes it when it
is refused or fails. Otherwise (a successful delete, or a removal that failed
or never ran) it lapses 30 s after it was written or last renewed, and holds
the case's turns off until then.

While the tombstone stands, `GET /cases/{case_id}` reads `deleting: true`
for every reader of the case (#2510). It reads `false` once the tombstone is
removed or lapses, and the case answers 404 once a delete succeeds. No other
read reports it.

## Residuals

- **Traces the backend has not finished with.** The backend deletes
  asynchronously, so a trace can stay visible for a while after the delete
  answers. It also ingests asynchronously, so a trace sent just before the
  search may not be found by it.
- **Traces on another replica (#2573).** After a connection failure the SDK
  parks traces in a SQLite database on that pod. A parked trace replays only
  when a delete runs on that pod, or after `init_opik_tracing`'s ping interval of
  999999 s, which is about 11.6 days. A trace parked or still batched on another
  replica is invisible to the delete and can land after it.
- **The title route (#2580).** It holds no lease, so its trace can land after
  the delete.
- **Traces recorded before #2102.** They carry no case tag, so they cannot be
  attributed to a case and a delete does not find them.
- **Tracing turned off later (#2581).** With `OPIK_ENABLED` off or no backend
  URL, a delete does not contact the backend, so traces recorded while tracing
  was on stay there, and the audit row names `llm_traces` under `not_configured`.
- **Document traces recorded before #2575.** They carry no conversion tag, so
  a draft or document delete does not find them.
- **A conversion that left no draft (#2604).** A conversion refused or failed
  before it wrote a draft, or one whose receipt was lost, sent the document's
  text and left no row whose delete would purge it.
- **A runbook quoted into a case.** Knowledge base text that a case's prompt
  carried is that case's trace: the case delete purges it, a document delete
  does not.
- **Draft and document deletes** are subject to the backend's asynchronous
  delete, to other replicas' parked traces (#2573), and to tracing turned off
  later (#2581), as above, with no audit row to name the last.
- **Redis unreachable (ruled 2026-11-04).** The delete fails closed with 503,
  but a turn whose lease write or tombstone read fails runs unguarded. It is
  logged once per process and counted on `case_unleased_turns_total{reason}`
  (`no_store`, `store_error`). A delete that passes meanwhile can race that
  turn, and its vectors or upload can land after the delete. Standalone cannot
  reach this: its Redis runs in-process.
- **A stalled holder.** A process that stops renewing for longer than 30 s
  (a frozen event loop, a paused pod) loses its lease, and a delete can then
  proceed under its work.
- **A lost member.** From a member's loss from Redis (an eviction, a failover
  that loses data) until the holder's next successful renewal (every 10 s), a
  delete can pass under running holders. The renewal, or the case's next turn,
  writes the member again.
- **`deleting` is best-effort.** `false` means no delete is *known* to run:
  with no lease store, a failed tombstone read (logged once per process), or
  a tombstone that lapsed under a stalled delete, a read reports `false` while
  a delete may still run. A client still handles the turn's 409
  `CASE_DELETE_IN_PROGRESS` and the 404 as before.
- **`deleting: true` with no delete running.** `deleting` reports the
  delete's tombstone, which a delete writes before it checks for work in
  flight and which lapses 30 s after it was written or last renewed unless it
  is removed. So it can read `true` for up to 30 s with no delete running, and
  turns are refused for the same span. A client reading again sees it clear.
- **A resume.** Resuming a session on the case
  (`POST /cases/sessions/{session_id}/resume/{case_id}`) is not a turn and
  holds no lease. One that passed its access check before the delete can
  re-write the session pointer.

## At shutdown

The same record of in-flight work is drained when the process stops: the
container's last release waits up to 10 s for it, cancels what may be
cancelled, and waits for a turn's commit to end, before it closes Redis,
ChromaDB and the database (#2490). See `faultmaven/container/README.md`,
"Ownership and shutdown".
