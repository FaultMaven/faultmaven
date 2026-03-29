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
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - postgresql
  - locks
  - deadlocks
  - pg-locks
  - blocking-queries
  - contention
difficulty: intermediate
---

# PostgreSQL Lock Contention and Deadlocks

## Problem Definition

Applies to PostgreSQL 10 and later (all currently supported versions). Requires superuser or `pg_monitor` role for full visibility into `pg_locks` and `pg_stat_activity`. Access via `psql` or any SQL client to the affected instance is needed.

Lock contention occurs when multiple transactions compete for the same database resources, causing queries to wait rather than execute. Deadlocks occur when two or more transactions hold locks that the other needs, creating a circular dependency that PostgreSQL must break by aborting one transaction.

Symptoms include queries that hang for seconds or minutes before completing, application-side timeout errors, elevated response latency across endpoints that share database tables, and deadlock error messages in application logs:

```
ERROR: deadlock detected
DETAIL: Process 12345 waits for ShareLock on transaction 67890; blocked by process 54321.
Process 54321 waits for ShareLock on transaction 12345; blocked by process 12345.
HINT: See server log for query details.
```

Other indicators include growing `pg_stat_activity` rows in `active` state with `wait_event_type = Lock`, application connection pool exhaustion as connections queue behind locked queries, and autovacuum processes blocked by long-running transactions.

Common causes include long-running transactions that hold locks for extended periods, missing indexes forcing sequential scans that acquire broader locks, explicit `LOCK TABLE` statements, DDL operations (ALTER TABLE, CREATE INDEX without CONCURRENTLY) that acquire AccessExclusiveLock, and application code that acquires locks in inconsistent order across different code paths.

## Diagnostic Steps

### Step 1. Check for active lock waits

Identify sessions that are currently waiting to acquire locks and what is blocking them.

```sql
SELECT
  blocked.pid AS blocked_pid,
  blocked.usename AS blocked_user,
  blocked.application_name AS blocked_app,
  now() - blocked.query_start AS blocked_duration,
  blocking.pid AS blocking_pid,
  blocking.usename AS blocking_user,
  blocking.application_name AS blocking_app,
  blocking.state AS blocking_state,
  left(blocked.query, 100) AS blocked_query,
  left(blocking.query, 100) AS blocking_query
FROM pg_stat_activity blocked
JOIN pg_locks blocked_locks ON blocked.pid = blocked_locks.pid
JOIN pg_locks blocking_locks
  ON blocked_locks.locktype = blocking_locks.locktype
  AND blocked_locks.database IS NOT DISTINCT FROM blocking_locks.database
  AND blocked_locks.relation IS NOT DISTINCT FROM blocking_locks.relation
  AND blocked_locks.page IS NOT DISTINCT FROM blocking_locks.page
  AND blocked_locks.tuple IS NOT DISTINCT FROM blocking_locks.tuple
  AND blocked_locks.virtualxid IS NOT DISTINCT FROM blocking_locks.virtualxid
  AND blocked_locks.transactionid IS NOT DISTINCT FROM blocking_locks.transactionid
  AND blocked_locks.pid != blocking_locks.pid
JOIN pg_stat_activity blocking ON blocking_locks.pid = blocking.pid
WHERE NOT blocked_locks.granted
ORDER BY blocked_duration DESC;
```

Expected output: no rows under healthy conditions. Any rows indicate active contention. Pay attention to `blocked_duration` (how long the query has been waiting) and `blocking_state` (whether the blocker is `active`, `idle in transaction`, or `idle`).

### Step 2. Identify lock types being contested

Examine which lock types are involved to understand the nature of the contention.

```sql
SELECT
  locktype,
  mode,
  granted,
  count(*) AS lock_count
FROM pg_locks
GROUP BY locktype, mode, granted
ORDER BY lock_count DESC;
```

Key lock modes to watch: `AccessExclusiveLock` (blocks everything, typically DDL), `RowExclusiveLock` (normal for INSERT/UPDATE/DELETE), `ShareLock` (foreign key checks), `ExclusiveLock` (explicit locking). A high count of ungranted locks in any mode indicates contention.

### Step 3. Find the blocking chain root

In complex contention, there may be cascading blocks (A blocks B, B blocks C). Find the root blocker.

```sql
WITH RECURSIVE lock_chain AS (
  SELECT
    blocked.pid AS blocked_pid,
    blocking_locks.pid AS blocking_pid,
    1 AS depth
  FROM pg_locks blocked
  JOIN pg_locks blocking_locks
    ON blocked.locktype = blocking_locks.locktype
    AND blocked.database IS NOT DISTINCT FROM blocking_locks.database
    AND blocked.relation IS NOT DISTINCT FROM blocking_locks.relation
    AND blocked.page IS NOT DISTINCT FROM blocking_locks.page
    AND blocked.tuple IS NOT DISTINCT FROM blocking_locks.tuple
    AND blocked.virtualxid IS NOT DISTINCT FROM blocking_locks.virtualxid
    AND blocked.transactionid IS NOT DISTINCT FROM blocking_locks.transactionid
    AND blocked.pid != blocking_locks.pid
  WHERE NOT blocked.granted AND blocking_locks.granted

  UNION ALL

  SELECT
    lc.blocked_pid,
    blocking_locks.pid,
    lc.depth + 1
  FROM lock_chain lc
  JOIN pg_locks blocked ON lc.blocking_pid = blocked.pid AND NOT blocked.granted
  JOIN pg_locks blocking_locks
    ON blocked.locktype = blocking_locks.locktype
    AND blocked.database IS NOT DISTINCT FROM blocking_locks.database
    AND blocked.relation IS NOT DISTINCT FROM blocking_locks.relation
    AND blocked.pid != blocking_locks.pid
  WHERE blocking_locks.granted AND lc.depth < 10
)
SELECT
  blocking_pid AS root_blocker,
  count(DISTINCT blocked_pid) AS sessions_blocked,
  max(depth) AS chain_depth
FROM lock_chain
WHERE blocking_pid NOT IN (SELECT blocked_pid FROM lock_chain)
GROUP BY blocking_pid
ORDER BY sessions_blocked DESC;
```

The `root_blocker` PID is the session that, if terminated, would unblock the most waiting sessions.

### Step 4. Check for recent deadlocks

Query the statistics view and server log for deadlock events.

```sql
SELECT
  datname,
  deadlocks,
  conflicts
FROM pg_stat_database
WHERE datname = current_database();
```

A non-zero `deadlocks` value indicates deadlocks have occurred since the last statistics reset. Check the PostgreSQL server log for full details:

```bash
sudo grep -i "deadlock detected" /var/log/postgresql/postgresql-16-main.log | tail -20
```

The log entry includes the full query text and lock details for both processes involved in the deadlock.

### Step 5. Identify tables with the most lock activity

Determine which tables are the contention hotspots.

```sql
SELECT
  c.relname AS table_name,
  l.mode,
  l.granted,
  count(*) AS lock_count
FROM pg_locks l
JOIN pg_class c ON l.relation = c.oid
WHERE l.locktype = 'relation'
GROUP BY c.relname, l.mode, l.granted
ORDER BY lock_count DESC
LIMIT 20;
```

Tables appearing frequently with ungranted locks are contention hotspots that may need schema changes, index optimization, or partitioning.

### Step 6. Check for long-running transactions holding locks

Long-running transactions prevent lock release and block other sessions.

```sql
SELECT
  pid,
  usename,
  application_name,
  state,
  now() - xact_start AS xact_duration,
  now() - query_start AS query_duration,
  left(query, 100) AS query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL
  AND now() - xact_start > interval '1 minute'
ORDER BY xact_duration DESC;
```

Transactions running longer than expected are candidates for investigation. Sessions in `idle in transaction` state have started a transaction but not committed or rolled back.

## Mitigation

### Option 1. Terminate the root blocking session

**Risk**: Low-Medium. Terminates one session. The affected application receives a connection error and should reconnect.

**Command**:

```sql
-- Replace 12345 with the blocking PID from Step 1 or Step 3
SELECT pg_terminate_backend(12345);
```

**Verify**:

```sql
SELECT count(*) FROM pg_locks WHERE NOT granted;
```

**Duration**: Immediate. Blocked sessions proceed within seconds.

### Option 2. Cancel a long-running query without terminating the connection

**Risk**: Low. Cancels only the current query, leaving the connection intact. The application receives a query cancellation error and can retry.

**Command**:

```sql
-- Replace 12345 with the PID of the long-running query
SELECT pg_cancel_backend(12345);
```

**Verify**:

```sql
SELECT pid, state, left(query, 80) FROM pg_stat_activity WHERE pid = 12345;
```

**Duration**: Immediate. If the query does not respond to cancel within a few seconds, escalate to `pg_terminate_backend`.

### Option 3. Set a lock timeout to prevent indefinite waits

**Risk**: Low. Queries that cannot acquire locks within the timeout fail fast instead of blocking indefinitely. Applications must handle the timeout error.

**Command**:

```sql
-- Set globally (applies to new sessions after reload)
ALTER SYSTEM SET lock_timeout = '10s';
SELECT pg_reload_conf();

-- Or per-session for immediate effect
SET lock_timeout = '10s';
```

**Verify**:

```sql
SHOW lock_timeout;
```

**Duration**: Immediate for per-session; new sessions pick up the global setting after reload.

### Option 4. Terminate all idle-in-transaction sessions older than a threshold

**Risk**: Medium. Terminates multiple sessions at once. Applications will see connection errors.

**Command**:

```sql
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND now() - xact_start > interval '5 minutes'
  AND pid != pg_backend_pid();
```

**Verify**:

```sql
SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction';
```

**Duration**: Immediate.

## Root Cause Resolution

**If** an idle-in-transaction session is the root blocker → fix the application code to commit or rollback transactions promptly. Set a server-side safety net:

```sql
ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';
SELECT pg_reload_conf();
```

**If** DDL operations (ALTER TABLE, CREATE INDEX) are blocking DML → use non-blocking alternatives:

```sql
-- Instead of CREATE INDEX (acquires ShareLock on table):
CREATE INDEX CONCURRENTLY idx_name ON table_name (column);

-- For ALTER TABLE adding a column with a default (PG 11+, instant add):
ALTER TABLE table_name ADD COLUMN new_col integer DEFAULT 0;
```

Schedule DDL during low-traffic windows and set `lock_timeout` on DDL sessions to prevent them from waiting indefinitely.

**If** deadlocks occur repeatedly between the same queries → ensure all application code paths acquire locks in a consistent order. If two transactions both modify tables A and B, they must always modify A first, then B. Review the deadlock log details to identify the conflicting code paths.

**If** sequential scans on large tables cause broad lock contention → add appropriate indexes to enable index scans, which acquire narrower row-level locks:

```sql
SELECT
  schemaname, relname, seq_scan, idx_scan,
  seq_scan - idx_scan AS excess_seq_scans
FROM pg_stat_user_tables
WHERE seq_scan > idx_scan
ORDER BY excess_seq_scans DESC
LIMIT 10;
```

**If** explicit `LOCK TABLE` statements in application code cause contention → replace with advisory locks or `SELECT ... FOR UPDATE SKIP LOCKED`:

```sql
-- Advisory locks for application-level coordination:
SELECT pg_advisory_lock(hashtext('my_resource'));
-- ... do work ...
SELECT pg_advisory_unlock(hashtext('my_resource'));

-- SKIP LOCKED for queue-like patterns:
SELECT * FROM task_queue
WHERE status = 'pending'
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

## Verification

After applying fixes, confirm contention has been resolved.

1. No ungranted locks:

```sql
SELECT count(*) AS waiting_locks FROM pg_locks WHERE NOT granted;
```

Expect 0 under normal conditions.

1. No long-running blocking sessions:

```sql
SELECT count(*)
FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND now() - xact_start > interval '5 minutes';
```

Expect 0.

1. Lock timeout is configured:

```sql
SHOW lock_timeout;
SHOW idle_in_transaction_session_timeout;
```

1. Deadlock count is not increasing:

```sql
SELECT deadlocks FROM pg_stat_database WHERE datname = current_database();
```

Note the value and check again after 10-15 minutes. If the count is still rising, the application-level lock ordering fix has not been applied to all code paths.

1. Application response times have returned to baseline. Check application metrics (p95/p99 latency) to confirm the latency impact has been resolved.

## Prevention

1. **Set lock_timeout globally** — Configure `lock_timeout` (recommended: 10-30 seconds) to prevent queries from waiting indefinitely for locks. Applications should handle the timeout error with a retry.

2. **Set idle_in_transaction_session_timeout** — Configure to 5 minutes to automatically terminate abandoned transactions that hold locks.

3. **Use consistent lock ordering** — All application code paths that modify multiple tables must acquire locks in the same order to prevent deadlocks. Document the canonical table ordering for your schema.

4. **Prefer CONCURRENTLY for index and schema changes** — Use `CREATE INDEX CONCURRENTLY` and avoid long-holding DDL during peak traffic. Schedule schema migrations during maintenance windows.

5. **Use row-level locking patterns** — Replace `LOCK TABLE` with `SELECT ... FOR UPDATE`, advisory locks, or `FOR UPDATE SKIP LOCKED` for queue patterns.

6. **Monitor lock waits** — Alert when sessions wait on locks beyond a threshold:

```sql
SELECT count(*) AS lock_wait_count
FROM pg_stat_activity
WHERE wait_event_type = 'Lock'
  AND now() - query_start > interval '10 seconds';
```

7. **Keep transactions short** — Move non-database work (API calls, file I/O, computation) outside of transaction boundaries to minimize lock hold time.

8. **Add indexes to reduce lock scope** — Ensure queries use index scans rather than sequential scans. Index scans acquire row-level locks only on matched rows.

9. **Enable deadlock logging** — Set `log_lock_waits = on` and configure `deadlock_timeout` (default: 1 second) so all lock waits exceeding the threshold are logged for post-incident analysis.

## Sources

- [PostgreSQL Documentation: Explicit Locking](https://www.postgresql.org/docs/current/explicit-locking.html) — Official reference for lock modes, deadlock detection, and advisory locks.
- [PostgreSQL Documentation: pg_locks View](https://www.postgresql.org/docs/current/view-pg-locks.html) — Official reference for the `pg_locks` system view used in diagnostics.
- [PostgreSQL Documentation: The Cumulative Statistics System](https://www.postgresql.org/docs/current/monitoring-stats.html) — Official reference for `pg_stat_activity`, `pg_stat_database`, and wait event monitoring.
- [PostgreSQL Documentation: Lock Management Configuration](https://www.postgresql.org/docs/current/runtime-config-locks.html) — Official reference for `deadlock_timeout`, `lock_timeout`, and `max_locks_per_transaction`.
