# Case deletion

What `DELETE /api/v1/cases/{case_id}` does, when it refuses, and what it can
still miss. The full contract of each answer is in the generated
[API reference](../reference/api/README.md).

## What a delete clears

Only the case's creator may delete it. The delete clears every store that
holds the case's data before it deletes the row:

- the session pointers to the case;
- its runbook conversion jobs and drafts (a published runbook is kept);
- its uploaded files;
- its evidence search index (the `case_{case_id}` ChromaDB collection);
- its PII redaction registry.

Then the row goes, in one transaction with its team shares and a
`case_deleted` audit row. If a store cannot be cleared, the delete answers
**500** `DELETE_CASE_ERROR` and keeps the row, so a retry can finish it (#1016).

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
tombstone is held while the delete runs and is removed on every outcome except
a deleted row, so a failed delete never holds the case's turns off. After a
successful delete it lapses on its own within 30 s.

While the tombstone stands, `GET /cases/{case_id}` reads `deleting: true`
for every reader of the case (#2510). It reads `false` again once a failed
delete has removed its tombstone, and the case answers 404 once a delete
succeeds. No other read reports it.

## Residuals

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
- **`deleting: true` with no delete running.** It reports the tombstone, so it
  reads `true` briefly while a delete is refused on work in flight (the
  tombstone is written before the lease is read), and for up to 30 s after a
  failed delete whose tombstone could not be removed (logged; turns are
  refused for that span too). A client reading again sees it clear.
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
