---
id: pg-lock-contention
title: "PostgreSQL Lock Contention and Deadlocks"
domain: database
service: postgresql
symptom_class:
  - latency
  - timeout
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags:
  - postgresql
  - locks
  - deadlocks
  - pg-locks
  - blocking-queries
  - idle-in-transaction
  - ddl
difficulty: intermediate
---

## Symptom Recognition

Application requests hang for seconds to minutes and then surface as upstream timeouts (HTTP 5xx, gRPC `DEADLINE_EXCEEDED`, JDBC `Query timed out`). PostgreSQL server log emits explicit deadlock messages:

```text
ERROR:  deadlock detected
DETAIL: Process 12345 waits for ShareLock on transaction 67890; blocked by process 54321.
Process 54321 waits for ShareLock on transaction 12345; blocked by process 12345.
HINT:  See server log for query details.
```

`pg_stat_activity` shows multiple `client backend` rows with `wait_event_type = 'Lock'` and rising `query_start` age. `pg_locks` contains rows with `granted = false`. `pg_stat_database.deadlocks` is non-zero and increasing for the affected database. When DDL is the trigger, a single session holding `AccessExclusiveLock` blocks every read and write against the target relation, queueing application traffic behind it.

## Applicability

- PostgreSQL 10 or later (all currently supported majors). `transaction_timeout` requires PostgreSQL 17+; `idle_session_timeout` requires PostgreSQL 14+.
- Read access to `pg_locks`, `pg_stat_activity`, `pg_stat_database` (`pg_monitor` role, `pg_read_all_stats` role, or superuser).
- `pg_terminate_backend` / `pg_cancel_backend` privilege (superuser or `pg_signal_backend` role).
- Shell access to the database host for server log inspection, or managed-service log export (RDS CloudWatch Logs, Cloud SQL Logs Explorer, Azure Log Analytics).
- Tools: `psql`, access to `postgresql.conf` or the managed-service parameter group for durable fixes.

## Diagnostic Steps

### Step 1: Confirm active lock waits

```sql
SELECT count(*) AS waiting_locks
FROM pg_locks
WHERE NOT granted;
```

Expected output: `0` under healthy conditions. Any non-zero count means at least one session is blocked waiting for a conflicting lock to be released.

### Step 2: Identify blocked and blocking sessions

```sql
SELECT blocked.pid                                  AS blocked_pid,
       blocked.usename                              AS blocked_user,
       blocked.application_name                     AS blocked_app,
       now() - blocked.query_start                  AS blocked_duration,
       blocked.wait_event_type, blocked.wait_event,
       blocking.pid                                 AS blocking_pid,
       blocking.usename                             AS blocking_user,
       blocking.state                               AS blocking_state,
       now() - blocking.xact_start                  AS blocking_xact_age,
       left(blocked.query, 120)                     AS blocked_query,
       left(blocking.query, 120)                    AS blocking_query
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking
  ON blocking.pid = ANY (pg_blocking_pids(blocked.pid))
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
ORDER BY blocked_duration DESC;
```

Expected output: zero rows under healthy operation. `pg_blocking_pids()` encodes the lock-mode conflict matrix and is more reliable than self-joining `pg_locks`. Note the `blocking_state` — `idle in transaction` means the blocker is no longer doing work but still holds locks.

### Step 3: Break down lock modes currently in use

```sql
SELECT mode,
       granted,
       count(*) AS lock_count
FROM pg_locks
WHERE locktype IN ('relation', 'tuple', 'transactionid')
GROUP BY mode, granted
ORDER BY granted, lock_count DESC;
```

Expected output: predominantly `granted = true` with `AccessShareLock`/`RowExclusiveLock`/`RowShareLock` from normal traffic. Presence of `AccessExclusiveLock` (especially `granted = true` alongside a queue of `granted = false` entries) indicates a DDL or maintenance operation is blocking everything else.

### Step 4: Find tables that are contention hotspots

```sql
SELECT c.relname        AS relation,
       l.mode,
       l.granted,
       count(*)         AS lock_count
FROM pg_locks l
JOIN pg_class c ON l.relation = c.oid
WHERE l.locktype = 'relation'
  AND c.relkind IN ('r', 'p', 'i')
GROUP BY c.relname, l.mode, l.granted
ORDER BY lock_count DESC
LIMIT 20;
```

Expected output: distributed across application tables proportional to write load. A single relation accumulating ungranted locks is the contention hotspot — its name identifies the schema/index/code path that needs the fix.

### Step 5: Inspect deadlock counters

```sql
SELECT datname,
       deadlocks,
       stats_reset,
       now() - stats_reset AS counters_age
FROM pg_stat_database
WHERE datname = current_database();
```

Expected output: `deadlocks = 0` under healthy operation. A non-zero, increasing value means deadlock detection has fired since `stats_reset`. Re-run after 5–10 minutes; if the value grows, deadlocks are recurring and an application-side lock-ordering fix is required (not just a one-off kill).

### Step 6: Pull the deadlock detail from the server log

```bash
# Self-managed PostgreSQL
sudo grep -E "deadlock detected|process .* still waiting for" \
  /var/log/postgresql/postgresql-*.log | tail -40

# AWS RDS (via CLI)
aws rds describe-db-log-files --db-instance-identifier <id>
aws rds download-db-log-file-portion \
  --db-instance-identifier <id> --log-file-name error/postgresql.log.<date>
```

Expected output: one or more `ERROR: deadlock detected` blocks, each followed by `DETAIL:` lines naming the two processes, the conflicting lock modes, and the queries each was running. These query texts pinpoint the application code paths that acquire locks in inconsistent order.

### Step 7: Find long-running and idle-in-transaction sessions holding locks

```sql
SELECT pid,
       usename,
       application_name,
       state,
       now() - xact_start    AS xact_duration,
       now() - state_change  AS state_age,
       wait_event_type, wait_event,
       left(query, 200)      AS last_query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
  AND now() - xact_start > interval '1 minute'
  AND backend_type = 'client backend'
ORDER BY xact_duration DESC;
```

Expected output: zero rows under healthy operation. Rows with `state = 'idle in transaction'` and `state_age` greater than a minute are abandoned transactions still holding locks. Rows with `state = 'active'` and long `xact_duration` are long-running queries (likely missing index or sequential scan).

### Step 8: Look for active DDL or maintenance operations

```sql
SELECT pid,
       usename,
       application_name,
       state,
       now() - query_start AS query_duration,
       left(query, 300)    AS query
FROM pg_stat_activity
WHERE backend_type = 'client backend'
  AND state = 'active'
  AND query ~* '\m(alter table|create index|drop|reindex|cluster|vacuum full|refresh materialized view|truncate)\M'
ORDER BY query_start;
```

Expected output: zero rows during normal traffic. A row with `CREATE INDEX` (without `CONCURRENTLY`), `ALTER TABLE` rewrite, `REINDEX`, `CLUSTER`, `VACUUM FULL`, or `REFRESH MATERIALIZED VIEW` (without `CONCURRENTLY`) holds `AccessExclusiveLock` and blocks all DML on the target table.

## Causes

### Cause A: Idle-in-transaction session holds locks indefinitely

**Statement:** A session has issued `BEGIN` (or an implicit transaction) and acquired row or table locks, but the application path between the lock and the matching `COMMIT`/`ROLLBACK` never executes, so the locks persist and block every conflicting transaction.
**Mechanism:** The backend reaches `idle in transaction` state — typically because the application makes an external API call inside the transaction, hits an unhandled exception that skips the commit, or returns control to a user waiting on input. The acquired locks (row-level `FOR UPDATE`, table-level `RowExclusiveLock`, etc.) remain held until the client disconnects or `idle_in_transaction_session_timeout` fires. Other sessions waiting on those locks accumulate in the queue and surface as latency or timeouts.

**Indicator:**

- [Step 7] one or more rows with `state = 'idle in transaction'` and `state_age` exceeding a few minutes
- [Step 2] `blocking_state = 'idle in transaction'` for the dominant blocker

<!-- match: {"step": 2, "predicate": "contains", "target": "idle in transaction"} -->

**Mitigation:**

- **Risk:** Low. Terminating a stuck idle-in-transaction backend rolls back uncommitted work that the application has already abandoned; the client receives a connection error on its next query and reconnects.
- **Command:**

  ```sql
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
    AND now() - state_change > interval '5 minutes'
    AND pid <> pg_backend_pid();
  ```

- **Duration:** Locks release within seconds. Safe to leave running once but do not loop as a steady-state job — fix the application instead.

**Resolution:**

```sql
ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';
ALTER SYSTEM SET log_lock_waits = on;
SELECT pg_reload_conf();
```

Then patch the offending service: wrap every `BEGIN` in a scope guard (Python `with`, Java try-with-resources, Go `defer tx.Rollback()`, Node `try/finally`) so every code path reaches `COMMIT` or `ROLLBACK`, and move external API calls and long computations outside the transaction boundary.

- **Impact:** Cluster-wide. `idle_in_transaction_session_timeout` applies to every new session after reload; any application that legitimately holds an idle transaction longer than 5 minutes (rare) will start receiving `FATAL: terminating connection due to idle-in-transaction timeout`. `log_lock_waits` adds one log line per wait exceeding `deadlock_timeout` — bounded but non-zero log volume.
- **Rollback:**

  ```sql
  ALTER SYSTEM RESET idle_in_transaction_session_timeout;
  ALTER SYSTEM RESET log_lock_waits;
  SELECT pg_reload_conf();
  ```

**Verification:** Ten minutes after the reload, re-run Step 7 — expect zero rows with `state_age` greater than the configured timeout. `SHOW idle_in_transaction_session_timeout` returns `5min`. `pg_stat_database.deadlocks` stops growing.

### Cause B: DDL holds AccessExclusiveLock while online traffic queues

**Statement:** A migration or maintenance command (`CREATE INDEX` without `CONCURRENTLY`, table-rewriting `ALTER TABLE`, `REINDEX`, `CLUSTER`, `VACUUM FULL`, non-concurrent `REFRESH MATERIALIZED VIEW`) takes `AccessExclusiveLock` on a hot table and stalls every concurrent read and write against it.
**Mechanism:** `AccessExclusiveLock` is the strongest table-level lock and conflicts with every other mode — including `AccessShareLock` taken by plain `SELECT`. PostgreSQL enqueues incoming sessions in arrival order, so once DDL is waiting, even subsequent read-only queries pile up behind it (the "lock queue" problem). The DDL itself may also block waiting for an existing transaction to release a weaker lock, multiplying the blast radius.

**Indicator:**

- [Step 3] `AccessExclusiveLock` present with `granted = true` and a non-trivial count of `granted = false` rows
- [Step 8] one row in `active` state running `CREATE INDEX` (without `CONCURRENTLY`), `ALTER TABLE`, `REINDEX`, `CLUSTER`, `VACUUM FULL`, or `REFRESH MATERIALIZED VIEW`

<!-- match: {"step": 3, "predicate": "contains", "target": "AccessExclusiveLock"} -->

**Mitigation:**

- **Risk:** Medium. Cancelling a DDL command rolls back partial work — for an index build, the partially-built index is discarded; for a table rewrite, the new table file is dropped. Safe but wastes minutes-to-hours of build time.
- **Command:**

  ```sql
  -- Identify the DDL pid from Step 8, then cancel gracefully:
  SELECT pg_cancel_backend(<ddl_pid>);
  -- If cancel is ignored after 10 seconds:
  SELECT pg_terminate_backend(<ddl_pid>);
  ```

- **Duration:** Locks release within seconds of successful cancel. Queued application sessions drain in arrival order.

**Resolution:**

```sql
-- Always set a lock_timeout on DDL sessions so they fail fast instead of
-- camping at the head of the lock queue.
SET lock_timeout = '2s';

-- Use CONCURRENTLY for index DDL (no AccessExclusiveLock on the table):
CREATE INDEX CONCURRENTLY idx_orders_customer ON orders (customer_id);
REINDEX INDEX CONCURRENTLY idx_orders_customer;

-- For ALTER TABLE ADD COLUMN with default, PG 11+ stores the default in
-- catalog metadata and avoids the table rewrite:
ALTER TABLE orders ADD COLUMN created_at timestamptz DEFAULT now();

-- For constraints, use NOT VALID then VALIDATE in a separate transaction:
ALTER TABLE orders ADD CONSTRAINT chk_amount CHECK (amount > 0) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT chk_amount;   -- SHARE UPDATE EXCLUSIVE, non-blocking
```

Schedule the remaining unavoidable DDL during a maintenance window.

- **Impact:** Per-DDL session (`SET lock_timeout`) — only the DDL session is affected, no cluster-wide change. `CREATE INDEX CONCURRENTLY` runs longer (often 2–3×) and takes two scans of the table, but does not block readers/writers.
- **Rollback:** Not applicable — these are per-statement choices made by the operator running the DDL.

**Verification:** Re-run Step 3 after the DDL finishes — `AccessExclusiveLock` count returns to baseline (typically 0). Re-run Step 8 — no long-running DDL. Application p95 latency on the affected endpoints drops back to baseline within one traffic cycle.

### Cause C: Long-running query holds locks past application timeouts

**Statement:** A query in `active` state runs long enough (missing index, accidental cross join, unbounded analytic scan) that the locks it holds — even ordinary `RowExclusiveLock` for an `UPDATE` — accumulate enough waiters to saturate the application's connection pool and surface as timeouts.
**Mechanism:** An `UPDATE`/`DELETE`/`SELECT ... FOR UPDATE` takes row-level locks (and the matching `RowExclusiveLock` or `RowShareLock` at the relation level). If the query also performs a sequential scan because no index supports the predicate, it touches and locks far more rows than necessary, holding them for the full duration of the scan. Other transactions hitting the same rows queue with `wait_event_type = 'Lock'`. The blocker is `active` (not idle), so `idle_in_transaction_session_timeout` does not help — only `statement_timeout` does.

**Indicator:**

- [Step 7] one or more rows with `state = 'active'`, long `xact_duration`, and identifiable scan-heavy `last_query`
- [Step 2] `blocking_state = 'active'` for the dominant blocker, with rising `blocked_duration`

<!-- match: {"step": 2, "predicate": "contains", "target": "active"} -->

**Mitigation:**

- **Risk:** Medium. Cancelling an active query returns an error to the originating request and may roll back partial work. Killing the wrong query (e.g., a long-running but legitimate batch import) can corrupt application state.
- **Command:**

  ```sql
  -- Inspect first, then cancel by pid (graceful):
  SELECT pid, now() - query_start AS dur, left(query, 200)
  FROM pg_stat_activity
  WHERE state = 'active'
    AND query_start < now() - interval '60 seconds';

  SELECT pg_cancel_backend(<pid>);
  -- Escalate to pg_terminate_backend only if cancel does not respond within ~10s.
  ```

- **Duration:** Immediate. Re-run Step 2 after 30 seconds to confirm the queue has drained.

**Resolution:**

```sql
-- Bound query duration per role so a single bad query cannot pin the cluster.
ALTER ROLE app_user       SET statement_timeout = '30s';
ALTER ROLE reporting_user SET statement_timeout = '5min';

-- Identify the missing index using pg_stat_statements:
SELECT query, calls, mean_exec_time, rows
FROM pg_stat_statements
WHERE query ILIKE '%<offending_table>%'
ORDER BY mean_exec_time DESC
LIMIT 10;

-- Add the supporting index (CONCURRENTLY to avoid stacking Cause B on top).
CREATE INDEX CONCURRENTLY idx_orders_status_created
  ON orders (status, created_at);
```

- **Impact:** Per-role. `statement_timeout` only affects sessions logging in as that role; existing sessions keep their old setting until reconnect. Concurrent index build runs longer than a blocking `CREATE INDEX` and writes WAL.
- **Rollback:**

  ```sql
  ALTER ROLE app_user       RESET statement_timeout;
  ALTER ROLE reporting_user RESET statement_timeout;
  DROP INDEX CONCURRENTLY idx_orders_status_created;
  ```

**Verification:** `pg_stat_statements.mean_exec_time` for the previously slow query drops to milliseconds. Step 7 shows no `active` queries with `xact_duration` greater than the configured `statement_timeout`. Application p95 latency returns to baseline.

### Cause D: Recurring deadlocks from inconsistent lock-acquisition order

**Statement:** Two or more application code paths modify the same set of tables or rows but acquire locks in different orders, so under concurrency they deadlock and PostgreSQL aborts one transaction with `ERROR: deadlock detected`.
**Mechanism:** Transaction T1 locks row R1 in table A, then attempts to lock row R2 in table B. Concurrently, T2 locks R2 in B first, then attempts R1 in A. Each is waiting on a lock the other holds; PostgreSQL's deadlock detector wakes after `deadlock_timeout` (default 1s), spots the cycle, and aborts the transaction with the smallest cost. The aborted session sees `ERROR: deadlock detected`; the surviving session proceeds. If the application retries the aborted transaction, the issue can recur immediately at the next concurrent collision.

**Indicator:**

- [Symptom] server log contains `ERROR: deadlock detected` lines
- [Step 5] `pg_stat_database.deadlocks` is non-zero and growing across successive checks
- [Step 6] log `DETAIL:` lines show the same pair of queries / tables involved

<!-- match: {"step": 5, "predicate": "threshold", "target": "deadlocks", "op": ">", "value": 0} -->

**Mitigation:**

- **Risk:** Low. The deadlock detector self-resolves each individual deadlock by aborting one side. Application-side retry-with-backoff catches the abort cleanly. No operator command is needed during an in-flight deadlock — but each abort wastes the work done by the losing transaction.
- **Command:**

  ```sql
  -- Confirm the deadlock detector is active and the timeout is sane (default 1s):
  SHOW deadlock_timeout;
  -- Optionally raise it on heavily-loaded servers where deadlock checks
  -- themselves add measurable overhead — never below 100ms:
  SET deadlock_timeout = '1s';
  ```

- **Duration:** Per-session. Apply the application-side fix in the same incident — raising `deadlock_timeout` does not eliminate deadlocks, it only changes how quickly they are detected.

**Resolution:**

Fix the application so every code path that touches multiple objects acquires locks in the same canonical order. Document the order in the codebase and enforce it in code review.

```sql
-- Example: always lock account rows in ascending account_id order before transfer.
BEGIN;
SELECT * FROM accounts
WHERE id IN (:src, :dst)
ORDER BY id
FOR UPDATE;

UPDATE accounts SET balance = balance - :amount WHERE id = :src;
UPDATE accounts SET balance = balance + :amount WHERE id = :dst;
COMMIT;
```

For queue-like workloads, replace explicit `LOCK TABLE` and `SELECT ... FOR UPDATE` with `FOR UPDATE SKIP LOCKED` so workers do not contend on the same head-of-queue row.

- **Impact:** Application code change — durability depends on deploy. Holds across the cluster after deploy because the change is in the client code path, not server config.
- **Rollback:** Standard application revert (re-deploy the previous build). No server-side state to undo.

**Verification:** Server log stops emitting `deadlock detected` lines for the patched code path. `pg_stat_database.deadlocks` plateaus and stops growing within one traffic cycle. Application error rate for the affected endpoint drops to zero.

### Cause E: Explicit LOCK TABLE in application code serialises traffic

**Statement:** Application code issues `LOCK TABLE ... IN ACCESS EXCLUSIVE MODE` (or `EXCLUSIVE`/`SHARE`) to coordinate work, but the lock blocks every concurrent reader or writer of the table for the duration of the holding transaction.
**Mechanism:** `LOCK TABLE ... IN ACCESS EXCLUSIVE MODE` conflicts with every other table-level lock, including `AccessShareLock` taken by plain `SELECT`. While the lock is held, all DML and DDL against the table queues. Even weaker explicit modes (`EXCLUSIVE`, `SHARE ROW EXCLUSIVE`) block normal `UPDATE`/`DELETE`/`INSERT` because they conflict with `RowExclusiveLock`. The intent is usually application-level coordination (singleton job, leader election, cache rebuild) — for which a table-wide lock is the wrong tool.

**Indicator:**

- [Step 3] `AccessExclusiveLock` or `ExclusiveLock` present on `relation` lock types from an application backend
- [Step 8] no DDL statement is active, yet a long-running session holds the strong table lock

<!-- match: {"step": 3, "predicate": "contains", "target": "ExclusiveLock"} -->

**Mitigation:**

- **Risk:** Medium. Cancelling the lock-holding session releases the table but aborts whatever the application was coordinating; the application must be safe to retry.
- **Command:**

  ```sql
  -- Identify the holder from Step 2 / Step 3, then:
  SELECT pg_cancel_backend(<pid>);
  ```

- **Duration:** Immediate.

**Resolution:**

Replace table-level `LOCK TABLE` with one of the targeted patterns:

```sql
-- (1) Application-level singleton: use a transaction-scoped advisory lock.
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('job:cache-rebuild'));
-- ... do coordinated work, no other session can take the same key ...
COMMIT;   -- lock auto-released

-- (2) Queue / worker pattern: row-level lock that skips contended rows.
SELECT id, payload
FROM task_queue
WHERE status = 'pending'
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;

-- (3) Per-row coordination: row-level FOR UPDATE on a sentinel row.
SELECT * FROM job_leader WHERE name = 'cache-rebuild' FOR UPDATE;
```

Advisory locks live in the same `pg_locks` view (`locktype = 'advisory'`), but never conflict with DML on real tables — they are pure application-level coordination primitives.

- **Impact:** Application code change. Advisory locks are scoped to the session or transaction (depending on which API is used) and do not affect other workloads.
- **Rollback:** Standard application revert.

**Verification:** Re-run Step 3 — no more `ExclusiveLock`/`AccessExclusiveLock` from application backends. Re-run Step 2 — no blocked sessions caused by the coordination path. Application throughput for the affected endpoint rises as parallelism is restored.

### Cause F: Sequential scan on large table widens lock footprint

**Statement:** A query lacking an index forces PostgreSQL into a sequential scan, which acquires row-level locks (under `FOR UPDATE` or implicit `UPDATE`) on every row visited rather than just the matching rows, multiplying the lock footprint and the contention surface.
**Mechanism:** An `UPDATE`/`DELETE` with a `WHERE` predicate that has no supporting index runs as a sequential scan. Each visited row receives a tuple-level lock during the scan; if the table is large, the scan touches millions of rows and holds locks against all of them until commit. Concurrent transactions trying to update any of those rows wait on `wait_event = tuple`. The same `UPDATE` with a usable index acquires locks only on the rows that actually match the predicate.

**Indicator:**

- [Step 4] one relation shows a disproportionately large count of `RowExclusiveLock` rows compared to its expected write rate
- [Step 2] multiple blocked sessions are waiting on tuple-level locks against the same relation

<!-- match: {"step": 4, "predicate": "contains", "target": "RowExclusiveLock"} -->

**Mitigation:**

- **Risk:** Low. Adding a `LIMIT` and a tighter `WHERE` to the offending query reduces lock footprint immediately if the application can be patched. Otherwise, cancelling the scan via `pg_cancel_backend` is the only short-term lever.
- **Command:**

  ```sql
  -- Confirm the scan via EXPLAIN (do NOT use EXPLAIN ANALYZE on a live UPDATE):
  EXPLAIN UPDATE orders SET status = 'shipped' WHERE order_date < now() - interval '30 days';
  -- Look for "Seq Scan on orders" with a high estimated row count.
  ```

- **Duration:** EXPLAIN is read-only — safe to run on production.

**Resolution:**

```sql
-- Identify sequential-scan-heavy tables:
SELECT schemaname, relname,
       seq_scan, idx_scan,
       seq_tup_read, idx_tup_fetch
FROM pg_stat_user_tables
WHERE seq_scan > idx_scan
  AND seq_tup_read > 100000
ORDER BY seq_tup_read DESC
LIMIT 20;

-- Add the supporting index without blocking:
CREATE INDEX CONCURRENTLY idx_orders_status_order_date
  ON orders (status, order_date);

-- Verify the new plan uses Index Scan / Index Only Scan, not Seq Scan:
EXPLAIN UPDATE orders SET status = 'shipped' WHERE order_date < now() - interval '30 days';
```

- **Impact:** New index consumes disk (roughly 30–60% of base-table size for the indexed columns) and adds write amplification on every `INSERT`/`UPDATE` to the indexed columns. Affects only the indexed table — no cluster-wide change.
- **Rollback:**

  ```sql
  DROP INDEX CONCURRENTLY idx_orders_status_order_date;
  ```

**Verification:** `pg_stat_user_tables.seq_scan` for the table plateaus while `idx_scan` rises after the index is built. Re-run Step 4 — the relation no longer dominates the lock count. Concurrent `UPDATE` throughput on the table rises.

### Cause G: Foreign-key check serialises updates to the parent row

**Statement:** An `UPDATE`/`INSERT` on a child table triggers a foreign-key check that acquires `FOR KEY SHARE` on the referenced parent row; many concurrent child writes against the same parent row queue on that shared lock.
**Mechanism:** When a child row is inserted or its FK column is updated, PostgreSQL acquires `FOR KEY SHARE` on the referenced parent row to prevent the parent from being deleted or having its key changed mid-transaction. `FOR KEY SHARE` is compatible with other `FOR KEY SHARE` and `FOR SHARE` locks, but conflicts with `FOR UPDATE` and `FOR NO KEY UPDATE`. If a long transaction does `SELECT ... FOR UPDATE` on the parent, every concurrent child write blocks until that transaction commits. Symptoms look like "random writes to unrelated child rows are slow" — but the FK to a hot parent is the actual coupling.

**Indicator:**

- [Step 2] blocked sessions are running INSERT/UPDATE on a child table; the blocker holds a lock on the parent
- [Step 3] `RowShareLock` or `ShareLock` counts are elevated against the parent relation

<!-- match: {"step": 3, "predicate": "contains", "target": "ShareLock"} -->

**Mitigation:**

- **Risk:** Low. Shortening the parent-side transaction is the only safe quick fix; do not drop the FK constraint as a panic measure (it changes data integrity guarantees).
- **Command:**

  ```sql
  -- Find the parent-side blocker (long FOR UPDATE / FOR NO KEY UPDATE transaction):
  SELECT pid, now() - xact_start AS dur, left(query, 200)
  FROM pg_stat_activity
  WHERE state IN ('active', 'idle in transaction')
    AND query ILIKE '%for update%'
    AND now() - xact_start > interval '30 seconds';

  -- Cancel the offender once identified:
  SELECT pg_cancel_backend(<pid>);
  ```

- **Duration:** Immediate.

**Resolution:**

Patch the parent-side transaction to commit before the FK-protected child writes start, or split the long parent transaction into smaller units. Where the FK is only needed for offline reporting (not real-time integrity), consider `DEFERRABLE INITIALLY DEFERRED`:

```sql
-- Make the FK deferred so the check runs at COMMIT rather than at statement time.
-- Child writes no longer block on the parent's FOR KEY SHARE during long transactions.
ALTER TABLE order_items
  DROP CONSTRAINT order_items_order_id_fkey;
ALTER TABLE order_items
  ADD CONSTRAINT order_items_order_id_fkey
    FOREIGN KEY (order_id) REFERENCES orders(id)
    DEFERRABLE INITIALLY DEFERRED;
```

- **Impact:** Schema change. Deferred FKs still enforce integrity at commit time but report violations later — application error handling must catch FK violations on `COMMIT`, not just on the offending `INSERT`/`UPDATE`. `ALTER TABLE ... DROP CONSTRAINT` takes `AccessExclusiveLock` briefly on the child table.
- **Rollback:**

  ```sql
  ALTER TABLE order_items
    DROP CONSTRAINT order_items_order_id_fkey;
  ALTER TABLE order_items
    ADD CONSTRAINT order_items_order_id_fkey
      FOREIGN KEY (order_id) REFERENCES orders(id);
  ```

**Verification:** Re-run Step 2 during the same workload — child INSERT/UPDATE no longer blocks on parent locks. Application throughput on the child table rises in proportion to the removed serialisation.

### Cause Z: Unidentified

**Statement:** Diagnostic steps do not produce a clear signal — no obvious blocker in `pg_stat_activity`, no DDL in flight, no recurring deadlocks, no sequential-scan hotspots — yet the application reports query timeouts and lock-related latency.
**Mechanism:** Lock-contention symptoms can be caused by interactions that the steps above do not directly probe: lightweight-lock (LWLock) contention on shared buffers under extreme write load, predicate locks under serializable isolation, replication-induced waits on standbys, autovacuum holding `ShareUpdateExclusiveLock` longer than expected, or kernel-level scheduler stalls masquerading as lock waits. Without a clear signal, applying any of the Cause A–G fixes risks masking the actual driver and recurring at the next peak.

**Indicator:**

- [Default]

**Mitigation:**

- **Risk:** Diagnostic only — no system-state change.
- **Command:**

  ```sql
  -- Snapshot the full lock and activity state for offline analysis:
  \copy (SELECT now() AS captured_at, * FROM pg_stat_activity) TO '/tmp/pg_stat_activity.csv' CSV HEADER
  \copy (SELECT now() AS captured_at, * FROM pg_locks)         TO '/tmp/pg_locks.csv' CSV HEADER

  -- Capture wait events over a short window to spot LWLock / IO contention:
  SELECT wait_event_type, wait_event, count(*)
  FROM pg_stat_activity
  WHERE backend_type = 'client backend'
    AND state = 'active'
  GROUP BY wait_event_type, wait_event
  ORDER BY count DESC;

  -- Inspect autovacuum activity (may hold ShareUpdateExclusiveLock):
  SELECT pid, now() - query_start AS dur, query
  FROM pg_stat_activity
  WHERE backend_type = 'autovacuum worker';
  ```

- **Duration:** Read-only; safe to leave running.

**Resolution:** Out of runbook scope. Escalate to the database SRE/DBA on call with the snapshots above, the time window of the application incident, and the dominant `wait_event_type` / `wait_event` pair observed. Add a follow-up runbook for the new failure mode once root cause is identified.
**Verification:** Escalation acknowledged with snapshots attached; an incident review captures the new pattern.

## Prevention

1. **Set `idle_in_transaction_session_timeout` server-wide.** Configure to `5min` via `ALTER SYSTEM`. Automatically terminates abandoned transactions before they can pile up lock waiters.
2. **Set `lock_timeout` on DDL sessions and migration tooling.** A 2–5 second `SET lock_timeout` in every migration script means a stuck migration fails fast instead of head-of-queueing every reader on the table.
3. **Set `statement_timeout` per role.** `ALTER ROLE app_user SET statement_timeout = '30s'`; `ALTER ROLE reporting_user SET statement_timeout = '5min'`. Caps the blast radius of a single runaway query.
4. **Enable `log_lock_waits = on`.** Logs one line per lock wait exceeding `deadlock_timeout` (default 1 s). Cheap, low-volume, and indispensable for post-incident analysis.
5. **Enforce consistent lock-acquisition order in code review.** Document the canonical order (e.g., "always lock accounts in ascending `id` order before transfer"); require all multi-row update paths to use `ORDER BY ... FOR UPDATE`.
6. **Use `CREATE INDEX CONCURRENTLY` and `REINDEX CONCURRENTLY` for online schema changes.** Take the runtime penalty (2–3× slower) to keep readers and writers unblocked.
7. **Prefer `ADD CONSTRAINT ... NOT VALID` followed by `VALIDATE CONSTRAINT`.** Splits a single `AccessExclusiveLock` operation into a quick metadata-only catalog change plus a longer `SHARE UPDATE EXCLUSIVE` scan that does not block DML.
8. **Replace `LOCK TABLE` with advisory locks or `FOR UPDATE SKIP LOCKED`.** Application-level coordination should never reach for table-wide locks.
9. **Alert on lock waits.** Fire when `count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND now() - query_start > interval '10 seconds'` is non-zero for more than two consecutive minutes.
10. **Monitor `pg_stat_database.deadlocks`.** Any non-zero increment is worth a post-hoc review; sustained growth is an open bug in application lock ordering.

## Sources

- [PostgreSQL Documentation: Explicit Locking](https://www.postgresql.org/docs/current/explicit-locking.html) — Priority 1. Authoritative for the table-level lock conflict matrix (`AccessShareLock` through `AccessExclusiveLock`), row-level lock modes, advisory locks, and the consistent-ordering recommendation for deadlock prevention.
- [PostgreSQL Documentation: pg_locks View](https://www.postgresql.org/docs/current/view-pg-locks.html) — Priority 1. Authoritative for `pg_locks` columns (`locktype`, `mode`, `granted`, `pid`, `waitstart`) and the recommended use of `pg_blocking_pids()` over manual `pg_locks` self-joins.
- [PostgreSQL Documentation: The Cumulative Statistics System](https://www.postgresql.org/docs/current/monitoring-stats.html) — Priority 1. Authoritative for `pg_stat_activity` columns (`state`, `wait_event_type`, `wait_event`, `xact_start`, `state_change`, `backend_type`) and `pg_stat_database.deadlocks`.
- [PostgreSQL Documentation: Lock Management Configuration](https://www.postgresql.org/docs/current/runtime-config-locks.html) — Priority 1. Authoritative for `deadlock_timeout` (default 1 s), `max_locks_per_transaction` (default 64), and `log_lock_waits` interaction with `deadlock_timeout`.
- [PostgreSQL Documentation: Client Connection Defaults](https://www.postgresql.org/docs/current/runtime-config-client.html) — Priority 1. Authoritative for `lock_timeout`, `statement_timeout`, `idle_in_transaction_session_timeout`, `idle_session_timeout` (PG 14+), and `transaction_timeout` (PG 17+) semantics, defaults, and interaction rules.
- [PostgreSQL Documentation: ALTER TABLE](https://www.postgresql.org/docs/current/sql-altertable.html) — Priority 1. Authoritative for which `ALTER TABLE` clauses take `AccessExclusiveLock` vs `SHARE UPDATE EXCLUSIVE` vs `SHARE ROW EXCLUSIVE`, the `ADD COLUMN ... DEFAULT` metadata-only fast path, and `ADD CONSTRAINT ... NOT VALID` / `VALIDATE CONSTRAINT` split.
- [PostgreSQL Wiki: Lock Monitoring](https://wiki.postgresql.org/wiki/Lock_Monitoring) — Priority 2. Community-maintained collection of lock-monitoring queries, including the `pg_blocking_pids()` join used in Step 2 and notes on log-based detection via `log_lock_waits`.
