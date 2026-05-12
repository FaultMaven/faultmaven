---
id: pg-connection-pool-exhaustion
title: "PostgreSQL Connection Pool Exhaustion"
domain: database
service: postgresql
symptom_class:
  - connection_refused
  - latency
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags:
  - postgresql
  - pgbouncer
  - max-connections
  - idle-in-transaction
  - connection-pool
difficulty: intermediate
---

## Symptom Recognition

New client connections fail with the PostgreSQL server error:

```
FATAL: sorry, too many clients already
```

Application logs simultaneously show connection-acquisition timeouts from the client-side pool (HikariCP, psycopg2, pgx, node-postgres) and elevated request latency as in-flight requests queue on the pool. Web tier returns HTTP 5xx (503/504) when handlers cannot acquire a connection within the request budget. CloudWatch / Prometheus shows `pg_stat_activity` row count at or above `max_connections` for the affected instance, and PgBouncer (if deployed) reports rising `cl_waiting` and `maxwait` in `SHOW POOLS`. Memory pressure on the database host typically rises in lockstep because every PostgreSQL backend consumes 5–10 MB of resident memory.

## Applicability

- PostgreSQL 10 or later, including PostgreSQL 14+ for `idle_session_timeout` and 17+ for `transaction_timeout`.
- Read access to `pg_stat_activity` and `pg_settings` (`pg_monitor` role or superuser).
- `pg_terminate_backend` privilege for mitigation (superuser or `pg_signal_backend` role member).
- Shell access to the database host or a managed-service console (RDS/Cloud SQL/Azure) for `postgresql.conf` changes or parameter-group edits.
- Reserved-slot access: keep `superuser_reserved_connections` (default 3) so administrative `psql` can still connect when application slots are full.
- Tools: `psql`, optional `pgbouncer` admin console on port 6432.

## Diagnostic Steps

### Step 1: Confirm connection saturation

```sql
SELECT
  count(*) AS total_connections,
  (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS max_connections,
  round(100.0 * count(*) /
        (SELECT setting::int FROM pg_settings WHERE name = 'max_connections'), 1) AS pct_used
FROM pg_stat_activity;
```

Expected output: `pct_used` < 70 under steady state. Values ≥ 90 mean imminent exhaustion; 100 means new application connections are already being refused.

### Step 2: Break down connections by state

```sql
SELECT state, count(*) AS count,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY state
ORDER BY count DESC;
```

Expected output: majority `active` or `idle` under normal load. Dominance of `idle in transaction` / `idle in transaction (aborted)` points to abandoned transactions; large `idle` count without transactions points to client-pool leaks.

### Step 3: Identify top consumers by application, user, and host

```sql
SELECT usename, application_name, client_addr, state,
       count(*) AS connections
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY usename, application_name, client_addr, state
ORDER BY connections DESC
LIMIT 20;
```

Expected output: connections distributed across known services proportional to their pool sizes. A single `application_name` or `client_addr` holding a disproportionate share indicates an oversized client pool or a leak in that service.

### Step 4: Find idle-in-transaction sessions

```sql
SELECT pid, usename, application_name, client_addr,
       now() - xact_start  AS transaction_duration,
       now() - state_change AS idle_duration,
       left(query, 120) AS last_query
FROM pg_stat_activity
WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
ORDER BY xact_start ASC NULLS LAST;
```

Expected output: zero rows under healthy operation. Any session with `idle_duration` over a few minutes is an abandoned transaction; `last_query` identifies the application code path holding the slot.

### Step 5: Find long-idle connections without an open transaction

```sql
SELECT pid, usename, application_name, client_addr,
       backend_start, state_change,
       now() - state_change AS idle_since
FROM pg_stat_activity
WHERE state = 'idle'
  AND state_change < now() - interval '30 minutes'
ORDER BY state_change ASC;
```

Expected output: zero or few rows. A large number from one `application_name` indicates the client-side pool is creating connections faster than it reclaims them, or `server_idle_timeout` on the pooler is too high.

### Step 6: Check if PgBouncer (or another pooler) is in front

```bash
psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer -c "SHOW POOLS;"
psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer -c "SHOW STATS;"
```

Expected output: `cl_waiting` is 0 and `maxwait` is 0 under healthy load. `cl_waiting > 0` with `sv_idle = 0` means PgBouncer itself is the bottleneck (raise `default_pool_size` or `max_db_connections`); `cl_waiting = 0` with PostgreSQL saturated means there is no pooler, or applications bypass it.

### Step 7: Look for blocking / lock contention amplifying the problem

```sql
SELECT blocked.pid    AS blocked_pid,
       blocked.usename AS blocked_user,
       blocking.pid   AS blocking_pid,
       blocking.usename AS blocking_user,
       left(blocked.query, 80)  AS blocked_query,
       left(blocking.query, 80) AS blocking_query
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking
  ON blocking.pid = ANY (pg_blocking_pids(blocked.pid))
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0;
```

Expected output: zero rows. Any returned rows mean active sessions are waiting on locks held by other sessions, compounding pool pressure — resolve the blocker before terminating consumers.

### Step 8: Verify reserved slots and emergency access

```bash
psql -U postgres -h <host> -c "SHOW superuser_reserved_connections;"
psql -U postgres -h <host> -c "SELECT count(*) FROM pg_stat_activity;"
```

Expected output: `superuser_reserved_connections` ≥ 3 and superuser `psql` returns successfully. If even superuser connections are refused, only an OS-level signal (`pg_ctl` / systemctl) or a managed-service reboot will restore administrative access.

## Causes

### Cause A: Idle-in-transaction sessions hold slots indefinitely
**Statement:** Application code begins transactions but neither commits nor rolls back, leaving sessions in `idle in transaction` state that hold connection slots and row locks until the client disconnects.
**Mechanism:** A `BEGIN` (or implicit transaction from a statement) takes a connection. The application returns control to the user, waits on an external API, or hits an unhandled exception that skips the `COMMIT`/`ROLLBACK`. The backend stays in `idle in transaction`, the connection slot remains allocated, locks acquired during the transaction are retained, autovacuum is blocked on affected tables, and the slot is never returned to the pool until the TCP session times out or `idle_in_transaction_session_timeout` fires.
**Indicator:**
- [Step 4] one or more rows with `idle_duration` exceeding a few minutes
- [Step 2] `idle in transaction` or `idle in transaction (aborted)` share of total connections is significantly elevated
<!-- match: {"step": 2, "predicate": "contains", "target": "idle in transaction"} -->
**Mitigation:**
- **Risk:** Low. Terminating sessions that are already stuck does no useful work; well-behaved clients reconnect and the next request fails fast instead of timing out.
- **Command:**
  ```sql
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
    AND state_change < now() - interval '5 minutes'
    AND pid <> pg_backend_pid();
  ```
- **Duration:** Slots return within seconds. Safe to leave in place for the remainder of the incident; do not script as a steady-state job — fix the root cause instead.
**Resolution:**
```sql
ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';
SELECT pg_reload_conf();
```
Then fix the application: wrap every `BEGIN` in a context manager / `try-finally` so every code path reaches `COMMIT` or `ROLLBACK`, and audit external calls inside transactions (move them outside the transaction boundary).
**Verification:** After 10 minutes of normal load, re-run Step 4 — expect zero rows with `idle_duration` greater than the configured `idle_in_transaction_session_timeout`. Confirm `SHOW idle_in_transaction_session_timeout` returns the new value on all replicas.

### Cause B: Client-side connection pool is leaking connections
**Statement:** Application code acquires connections from its pool but fails to release them on all code paths, so the pool grows monotonically until it hits its configured ceiling and the database's `max_connections`.
**Mechanism:** A request path calls `pool.acquire()` (or equivalent) but skips `pool.release()` on early returns, raised exceptions, or cancelled futures. The pool tracks the connection as "in use" forever, the underlying TCP session stays open in PostgreSQL as `idle`, and `state_change` does not advance because no SQL is being issued. Eventually `pool_max_size × num_instances` exceeds `max_connections`, new acquires queue, request latency rises, and new connect attempts fail.
**Indicator:**
- [Step 5] many rows from a single `application_name` with `idle_since` exceeding 30 minutes
- [Step 3] one application/host dominates the total count despite low traffic
<!-- match: {"step": 5, "predicate": "threshold", "target": "idle_since_minutes", "op": ">", "value": 30} -->
**Mitigation:**
- **Risk:** Low–medium. Terminating idle backends forces the client pool to recreate them; well-written pools handle this transparently, but a poorly configured pool will treat it as a connection storm.
- **Command:**
  ```sql
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE state = 'idle'
    AND state_change < now() - interval '10 minutes'
    AND application_name = '<leaky_app>'
    AND pid <> pg_backend_pid();
  ```
- **Duration:** Immediate slot reclaim. Repeat once if the leak rate is slow; if you have to repeat more than twice in an hour, the application is actively leaking — escalate to the service owner.
**Resolution:**
Patch the offending service to release connections with a language-native scope guard (Python `with`, Java try-with-resources, Go `defer`, Node `try/finally`) and set a hard upper bound on the client pool:
```python
# psycopg / SQLAlchemy example — connection is returned on every path
with engine.connect() as conn:
    with conn.begin():
        conn.execute(text("SELECT ..."))
```
Set `pool_size + max_overflow` such that `(pool_size + max_overflow) × num_instances` < `max_connections − superuser_reserved_connections`.
**Verification:** Re-run Step 5 thirty minutes after deploy — expect the `idle_since` distribution to plateau (no single connection living longer than the configured pool `max_lifetime`). Total connections from the patched `application_name` should stop growing and track request volume.

### Cause C: Aggregate client pools exceed max_connections (no pooler in front)
**Statement:** Each application instance opens a direct PostgreSQL pool, and the product of pool sizes across all instances exceeds `max_connections`, so even correctly-released connections still saturate the server during traffic spikes.
**Mechanism:** N application instances each configure a pool of size P. Under load, each pool grows toward P; the database sees N × P concurrent connections. When N × P exceeds `max_connections − superuser_reserved_connections`, the (N × P + 1)-th client connect attempt receives `FATAL: sorry, too many clients already`. PostgreSQL backends are processes (5–10 MB RSS each), so raising `max_connections` is bounded by RAM, not by a configuration value.
**Indicator:**
- [Step 3] connection counts roughly proportional to instance count × per-instance pool size, with no single outlier
- [Step 6] no PgBouncer/PgPool front end (`SHOW POOLS` returns nothing or pooler is unreachable)
- [Symptom] `FATAL: sorry, too many clients already` correlates with autoscaling events that increase instance count
<!-- match: {"step": 6, "predicate": "absent", "target": "pgbouncer"} -->
**Mitigation:**
- **Risk:** Medium. Reducing per-instance pool size temporarily lowers concurrency per instance and may push queueing into the client; safer than rolling a restart but only buys time.
- **Command:**
  ```bash
  # Example for a Kubernetes Deployment using an env-var pool size
  kubectl set env deployment/<app> DB_POOL_MAX=$(( $(kubectl get deploy <app> -o jsonpath='{.spec.replicas}') ))
  # Per-instance pool now equals 1; total ≈ replicas
  ```
- **Duration:** Until a pooler is deployed. Watch p95 latency — request queuing will rise.
**Resolution:**
Deploy PgBouncer (or an equivalent pooler) in transaction mode in front of PostgreSQL. Minimum viable `pgbouncer.ini`:
```ini
[databases]
appdb = host=127.0.0.1 port=5432 dbname=appdb

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
default_pool_size = 20
max_client_conn = 1000
max_db_connections = 80
reserve_pool_size = 5
reserve_pool_timeout = 3
server_idle_timeout = 600
server_lifetime = 3600
query_wait_timeout = 120
```
Set `max_db_connections` strictly below PostgreSQL's `max_connections − superuser_reserved_connections` so administrative access remains possible.
**Verification:** After cutover, Step 1 shows PostgreSQL `pct_used` capped near `max_db_connections / max_connections` regardless of application instance count. Step 6 returns rows in `SHOW POOLS` with `cl_waiting = 0` and `maxwait = 0` under normal load.

### Cause D: Long-running queries occupy slots beyond budget
**Statement:** A handful of unbounded queries (analytical scans, missing indexes, accidental cross joins) run for minutes, holding both an active connection slot and locks against the tables they touch.
**Mechanism:** A query enters `active` state and stays there until completion. While active, its connection cannot be reused by another transaction even under PgBouncer transaction-mode pooling, because the pool tracks per-transaction reuse. Under load, a small number of long queries occupy a large fraction of slots; remaining short queries compete for the rest, queue on the client pool, and eventually surface as connection-acquisition timeouts even though `max_connections` is not yet hit.
**Indicator:**
- [Step 2] `active` share dominates (rather than `idle in transaction` / `idle`)
- [Step 4] no idle-in-transaction sessions but Step 3 shows long `query_duration` from one application
- [Symptom] latency rises before `FATAL: sorry, too many clients already` appears
<!-- match: {"step": 2, "predicate": "threshold", "target": "active_pct", "op": ">", "value": 0.7} -->
**Mitigation:**
- **Risk:** Medium. Cancelling running queries returns an error to the originating request and may roll back partial work; killing the wrong query (e.g., a deploy migration) can corrupt application state.
- **Command:**
  ```sql
  -- Inspect first, then cancel
  SELECT pid, usename, application_name,
         now() - query_start AS duration, left(query, 200) AS query
  FROM pg_stat_activity
  WHERE state = 'active'
    AND query_start < now() - interval '60 seconds'
  ORDER BY query_start ASC;

  -- Cancel (graceful), only escalate to pg_terminate_backend if cancel is ignored
  SELECT pg_cancel_backend(<pid>);
  ```
- **Duration:** Immediate. Do not loop this without operator review — repeated cancels mask the underlying query plan / index problem.
**Resolution:**
Set bounded `statement_timeout` per application role and add the missing indexes / rewrites that surface in `pg_stat_statements`:
```sql
ALTER ROLE app_user      SET statement_timeout = '30s';
ALTER ROLE reporting_user SET statement_timeout = '5min';
-- Move analytical workloads to a read replica or a separate role with its own
-- pool, so OLTP is never blocked behind a 5-minute report.
```
**Verification:** Step 2 returns `active` share to its baseline (typically < 30 %) within one traffic cycle. `pg_stat_statements.mean_exec_time` for the previously slow queries drops below the configured `statement_timeout`. No `canceling statement due to statement timeout` errors appear on healthy code paths.

### Cause E: PgBouncer pool sizing is the bottleneck (not PostgreSQL itself)
**Statement:** PostgreSQL has free `max_connections` capacity, but PgBouncer's `default_pool_size` or `max_db_connections` is set too low, so clients queue at the pooler and observe the same connection-acquisition symptoms.
**Mechanism:** PgBouncer maintains at most `default_pool_size` server connections per (db, user) pair and at most `max_db_connections` total per database. When all server connections are busy, additional clients land in the wait queue. `cl_waiting` rises, `maxwait` increases, and clients time out per `query_wait_timeout` (default 120 s) with `server_login_retry: server connection timeout` — observable to applications as a connection-acquisition failure even though PostgreSQL's `pg_stat_activity` is half empty.
**Indicator:**
- [Step 6] `cl_waiting > 0` and `maxwait > 0` in `SHOW POOLS` while Step 1 shows PostgreSQL `pct_used` is moderate (< 70)
- [Step 1] PostgreSQL connection count is well below `max_connections` despite application reports of "connection failures"
<!-- match: {"step": 6, "predicate": "threshold", "target": "cl_waiting", "op": ">", "value": 0} -->
**Mitigation:**
- **Risk:** Low–medium. Raising `default_pool_size` on PgBouncer is hot-reloadable. Raising it above `max_connections − superuser_reserved_connections` on PostgreSQL is dangerous — verify headroom first.
- **Command:**
  ```bash
  # Edit /etc/pgbouncer/pgbouncer.ini, then:
  psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer -c "RELOAD;"
  psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer -c "SHOW POOLS;"
  ```
- **Duration:** Until you have headroom data. Re-check `cl_waiting` and `maxwait` for 15 minutes after the reload.
**Resolution:**
Right-size PgBouncer: pick `default_pool_size` per (db, user) so the sum across all pools, plus reserve_pool capacity, stays comfortably below PostgreSQL's effective limit:
```ini
default_pool_size = 25          # per (db, user) pair
reserve_pool_size = 5
reserve_pool_timeout = 3
max_db_connections = 80         # < max_connections − superuser_reserved_connections
max_client_conn = 2000
pool_mode = transaction
```
If aggregate demand legitimately exceeds the PostgreSQL host's RAM-bounded ceiling, scale the database vertically (more RAM) or shard before raising PostgreSQL `max_connections` blindly.
**Verification:** Re-run Step 6 — `cl_waiting` and `maxwait` stay at 0 under normal load. `SHOW STATS` shows `avg_wait_time` close to 0. Application-side connection-acquisition timeouts disappear in logs.

### Cause Z: Unidentified
**Statement:** Diagnostic steps do not point to any single cause above, or the evidence is conflicting and a confident root cause cannot be assigned.
**Mechanism:** Connection-exhaustion symptoms can be triggered by interactions between layers (kernel `nofile` limits, ELB idle-timeout mismatches, DNS-induced reconnect storms, RDS proxy misconfigurations) that the steps above do not directly probe. Without a clear signal from `pg_stat_activity` state distribution, top-consumer breakdown, idle-in-transaction sessions, or pooler counters, applying any Cause A–E fix risks masking the actual driver and recurring at the next traffic peak.
**Indicator:**
- [Default]
**Mitigation:**
- **Risk:** Diagnostic only. The goal here is to collect evidence safely, not to fix anything.
- **Command:**
  ```bash
  # Snapshot full activity for offline analysis (run as superuser)
  psql -h <host> -U postgres -c "\copy (SELECT now() AS captured_at, * FROM pg_stat_activity) TO '/tmp/pg_stat_activity_snapshot.csv' CSV HEADER"

  # Capture pooler state if any pooler is in front
  psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer -c "SHOW POOLS;"   > /tmp/pgb_pools.txt
  psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer -c "SHOW CLIENTS;" > /tmp/pgb_clients.txt
  psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer -c "SHOW SERVERS;" > /tmp/pgb_servers.txt

  # Kernel / OS file-descriptor pressure on the DB host
  ss -s
  cat /proc/sys/fs/file-nr
  ```
- **Duration:** Diagnostic only — does not change system state.
**Resolution:** Out of runbook scope. Escalate to the database SRE/DBA on call with the snapshots above, the application service that owns the dominant `application_name` (if any), and the time window of `FATAL: sorry, too many clients already` log lines.
**Verification:** Escalation acknowledged with snapshots attached; a follow-up runbook or incident review is opened to capture the new failure mode for future automation.

## Prevention

1. **Deploy a connection pooler in transaction mode.** Put PgBouncer (or an equivalent) in front of PostgreSQL for any deployment with more than one application instance. Set `pool_mode = transaction` and size `max_db_connections` below PostgreSQL's `max_connections − superuser_reserved_connections`.
2. **Set `idle_in_transaction_session_timeout`.** Configure to `5min` server-wide via `ALTER SYSTEM` and `pg_reload_conf()`. This automatically reclaims abandoned transactions that would otherwise hold slots and block autovacuum.
3. **Set `idle_session_timeout` on PostgreSQL 14+.** Configure to `30min` to reclaim slots from forgotten clients that are not inside a transaction. Apply per-role rather than globally if you have long-lived application sessions you want to preserve.
4. **Right-size client pools.** Use `pool_size_per_instance = (max_connections − reserved) / (max_instances + buffer)`. Pin the formula in your service template so autoscaling does not silently exceed the database ceiling.
5. **Set `statement_timeout` per role.** `ALTER ROLE app_user SET statement_timeout = '30s';` prevents a single runaway query from pinning a slot for minutes.
6. **Alert at 80 % of `max_connections`.** Connection saturation is a leading indicator — pages must fire before `FATAL: sorry, too many clients already` lands in application logs.
7. **Recycle backend connections.** Set PgBouncer `server_lifetime = 3600` and `server_idle_timeout = 600` to prevent stale-connection accumulation; set a similar `max_lifetime` on application-side pools (HikariCP `maxLifetime`, SQLAlchemy `pool_recycle`).
8. **Keep `superuser_reserved_connections >= 3`.** Guarantees emergency superuser access even when the application tier has fully saturated normal slots.
9. **Audit connection handling in code review.** Require scope guards (`with`, try-with-resources, `defer`, `finally`) for every database connection acquisition; ban "raw acquire" in lint rules where possible.
10. **Prefer pooling to raising `max_connections`.** Each backend uses 5–10 MB of RAM. Raising `max_connections` without adding RAM trades a connection error for an OOM kill.

## Sources

- [PostgreSQL Documentation: Connection Settings](https://www.postgresql.org/docs/current/runtime-config-connection.html) — Priority 1. Authoritative for `max_connections`, `superuser_reserved_connections`, `reserved_connections`, and their server-start constraints.
- [PostgreSQL Documentation: Client Connection Defaults](https://www.postgresql.org/docs/current/runtime-config-client.html) — Priority 1. Authoritative for `statement_timeout`, `idle_in_transaction_session_timeout`, `idle_session_timeout`, `lock_timeout`, and `transaction_timeout` semantics and units.
- [PostgreSQL Documentation: The Cumulative Statistics System](https://www.postgresql.org/docs/current/monitoring-stats.html) — Priority 1. Authoritative for `pg_stat_activity` columns and the `state` enum used in Steps 2–5.
- [PgBouncer Configuration Reference](https://www.pgbouncer.org/config.html) — Priority 1. Authoritative for `pool_mode`, `default_pool_size`, `max_client_conn`, `max_db_connections`, `reserve_pool_size`, `server_idle_timeout`, `server_lifetime`, and `query_wait_timeout`.
- [PgBouncer Usage and Admin Console](https://www.pgbouncer.org/usage.html) — Priority 1. Authoritative for `SHOW POOLS` / `SHOW STATS` / `SHOW CLIENTS` / `SHOW SERVERS` columns (`cl_waiting`, `sv_active`, `sv_idle`, `maxwait`) used in Step 6.
