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
version: "2.0.0"
last_updated: "2026-06-25"
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

### Step 6: Check for a pooler in front

```bash
psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer -c "SHOW POOLS;"
psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer -c "SHOW STATS;"
```

Expected output: `cl_waiting` is 0 and `maxwait` is 0 under healthy load. `cl_waiting > 0` with `sv_idle = 0` means PgBouncer itself is the bottleneck (raise `default_pool_size` or `max_db_connections`); `cl_waiting = 0` with PostgreSQL saturated means there is no pooler, or applications bypass it.

### Step 7: Look for blocking / lock contention

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
**Chain:**
- root: application opens a transaction (`BEGIN`) then skips `COMMIT`/`ROLLBACK` on an external wait, user pause, or unhandled exception
- s1: the backend stays in `idle in transaction`, holding its connection slot plus any locks acquired, and blocks autovacuum on affected tables
- s2: held slots accumulate until they (plus other traffic) reach `max_connections`
- D: new client connections are refused with `FATAL: sorry, too many clients already` (see Symptom Recognition)
**Indicators:**
- root: [Step 4] one or more rows with `idle_duration` exceeding a few minutes; `last_query` names the offending code path
- s1: [Step 2] `idle in transaction` / `idle in transaction (aborted)` share of total connections is significantly elevated
  <!-- match: {"step": 2, "predicate": "contains", "target": "idle in transaction"} -->
**Interventions:**
- **remediation** (root): set a server-wide reclaim timeout and fix the application so every `BEGIN` reaches `COMMIT`/`ROLLBACK` via a context manager / `try-finally`; move external calls outside the transaction boundary.

  ```sql
  ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';
  SELECT pg_reload_conf();
  ```

  **Verification:** after 10 minutes of normal load, re-run Step 4 — expect zero rows with `idle_duration` greater than the configured timeout; `SHOW idle_in_transaction_session_timeout` returns the new value on all replicas.
- **mitigation** (s1): terminate sessions already stuck idle-in-transaction so their slots return immediately.

  ```sql
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
    AND state_change < now() - interval '5 minutes'
    AND pid <> pg_backend_pid();
  ```

  **Risk:** Low — terminating already-stuck sessions does no useful work; well-behaved clients reconnect and the next request fails fast instead of timing out. **Duration:** Slots return within seconds; safe for the remainder of the incident, but do not script as a steady-state job — fix the root cause. **Verification:** re-run Step 4 — the long-idle rows are gone.

### Cause B: Client-side connection pool is leaking connections
**Statement:** Application code acquires connections from its client pool but fails to release them on all code paths, so the pool grows monotonically until it hits its ceiling and the database's `max_connections`.
**Chain:**
- root: a request path calls `pool.acquire()` but skips `pool.release()` on early returns, raised exceptions, or cancelled futures
- s1: the pool tracks the connection as "in use" forever while PostgreSQL holds the TCP session as `idle` with `state_change` frozen (no SQL issued)
- s2: `pool_max_size × num_instances` grows until it exceeds `max_connections`; new acquires queue and latency rises
- D: new connect attempts fail with `FATAL: sorry, too many clients already` (see Symptom Recognition)
**Indicators:**
- root: [Step 3] one application/host dominates the total connection count despite low traffic
- s1: [Step 5] many rows from a single `application_name` with `idle_since` exceeding 30 minutes
  <!-- match: {"step": 5, "predicate": "threshold", "target": "idle_since_minutes", "op": ">", "value": 30} -->
**Interventions:**
- **remediation** (root): patch the offending service to release connections with a language-native scope guard (Python `with`, Java try-with-resources, Go `defer`, Node `try/finally`) and cap the client pool.

  ```python
  # psycopg / SQLAlchemy example — connection is returned on every path
  with engine.connect() as conn:
      with conn.begin():
          conn.execute(text("SELECT ..."))
  ```

  Set `pool_size + max_overflow` so `(pool_size + max_overflow) × num_instances` < `max_connections − superuser_reserved_connections`. **Verification:** re-run Step 5 thirty minutes after deploy — the `idle_since` distribution plateaus (no connection living past the pool `max_lifetime`); total connections from the patched `application_name` stop growing and track request volume.
- **mitigation** (s1): terminate the leaked idle backends from the offending app so their slots reclaim immediately.

  ```sql
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE state = 'idle'
    AND state_change < now() - interval '10 minutes'
    AND application_name = '<leaky_app>'
    AND pid <> pg_backend_pid();
  ```

  **Risk:** Low–medium — well-written pools recreate connections transparently, but a poorly configured pool treats it as a connection storm. **Duration:** Immediate slot reclaim; repeat once if the leak rate is slow — if you repeat more than twice in an hour the app is actively leaking, escalate to the service owner. **Verification:** re-run Step 5 — the leaked-app idle backends are gone.

### Cause C: Aggregate client pools exceed max_connections (no pooler in front)
**Statement:** Each application instance opens a direct PostgreSQL pool and the product of pool sizes across all instances exceeds `max_connections`, so even correctly-released connections saturate the server during traffic spikes.
**Chain:**
- root: N application instances each configure a direct pool of size P with no pooler in front
- s1: under load the database sees up to N × P concurrent connections, which exceeds `max_connections − superuser_reserved_connections` (often triggered by autoscaling raising N)
- D: the (N × P + 1)-th client connect attempt receives `FATAL: sorry, too many clients already` (see Symptom Recognition)
**Indicators:**
- root: [Step 6] no PgBouncer/PgPool front end (`SHOW POOLS` returns nothing or the pooler is unreachable)
  <!-- match: {"step": 6, "predicate": "absent", "target": "pgbouncer"} -->
- s1: [Step 3] connection counts roughly proportional to instance count × per-instance pool size, with no single outlier
- s1: [Symptom] `FATAL: sorry, too many clients already` correlates with autoscaling events that increase instance count
**Interventions:**
- **remediation** (root): deploy PgBouncer (or an equivalent pooler) in transaction mode in front of PostgreSQL. Minimum viable `pgbouncer.ini`:

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

  Set `max_db_connections` strictly below PostgreSQL's `max_connections − superuser_reserved_connections` so administrative access remains possible. **Verification:** after cutover, Step 1 shows `pct_used` capped near `max_db_connections / max_connections` regardless of instance count; Step 6 returns rows in `SHOW POOLS` with `cl_waiting = 0` and `maxwait = 0` under normal load.
- **mitigation** (s1): temporarily shrink the per-instance pool size so total connections drop below the ceiling.

  ```bash
  # Example for a Kubernetes Deployment using an env-var pool size
  kubectl set env deployment/<app> DB_POOL_MAX=$(( $(kubectl get deploy <app> -o jsonpath='{.spec.replicas}') ))
  # Per-instance pool now equals 1; total ≈ replicas
  ```

  **Risk:** Medium — lowering per-instance concurrency may push queueing into the client; safer than rolling a restart but only buys time. **Duration:** Until a pooler is deployed; watch p95 latency — request queuing will rise. **Verification:** re-run Step 1 — `pct_used` drops below the saturation threshold.

### Cause D: Long-running queries occupy slots beyond budget
**Statement:** A handful of unbounded queries (analytical scans, missing indexes, accidental cross joins) run for minutes, holding both an active connection slot and locks against the tables they touch.
**Chain:**
- root: unbounded queries (analytical scans, missing indexes, accidental cross joins) enter `active` state and stay there for minutes
- s1: each long query pins its connection slot for the duration — even PgBouncer transaction-mode pooling cannot reuse it mid-transaction — and holds table locks
- s2: remaining short queries compete for the few free slots, queue on the client pool, and surface as connection-acquisition timeouts before `max_connections` is even reached
- D: latency rises and, at the limit, new connections fail with `FATAL: sorry, too many clients already` (see Symptom Recognition)
**Indicators:**
- root: [Step 4] no idle-in-transaction sessions, yet [Step 3] shows long `query_duration` concentrated in one application
- s1: [Step 2] `active` share dominates (rather than `idle in transaction` / `idle`)
  <!-- match: {"step": 2, "predicate": "threshold", "target": "active_pct", "op": ">", "value": 0.7} -->
- s2: [Symptom] latency rises before `FATAL: sorry, too many clients already` appears
**Interventions:**
- **remediation** (root): set bounded `statement_timeout` per application role and add the missing indexes / rewrites surfaced in `pg_stat_statements`.

  ```sql
  ALTER ROLE app_user      SET statement_timeout = '30s';
  ALTER ROLE reporting_user SET statement_timeout = '5min';
  -- Move analytical workloads to a read replica or a separate role with its own
  -- pool, so OLTP is never blocked behind a 5-minute report.
  ```

  **Verification:** Step 2 returns `active` share to its baseline (typically < 30 %) within one traffic cycle; `pg_stat_statements.mean_exec_time` for the slow queries drops below the configured `statement_timeout`; no `canceling statement due to statement timeout` errors appear on healthy code paths.
- **mitigation** (s1): inspect the long-running queries, then cancel them (graceful) to free their slots; escalate to `pg_terminate_backend` only if a cancel is ignored.

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

  **Risk:** Medium — cancelling returns an error to the originating request and may roll back partial work; killing the wrong query (e.g. a deploy migration) can corrupt application state. **Duration:** Immediate; do not loop without operator review — repeated cancels mask the underlying plan/index problem. **Verification:** re-run Step 2 — `active` share falls back toward baseline.

### Cause E: PgBouncer pool sizing is the bottleneck (not PostgreSQL itself)
**Statement:** PostgreSQL has free `max_connections` capacity, but PgBouncer's `default_pool_size` or `max_db_connections` is set too low, so clients queue at the pooler and observe the same connection-acquisition symptoms.
**Chain:**
- root: PgBouncer's `default_pool_size` (per db/user) or `max_db_connections` is set too low relative to demand, while PostgreSQL has free capacity
- s1: when all pooler server connections are busy, additional clients land in PgBouncer's wait queue — `cl_waiting` and `maxwait` rise
- s2: queued clients time out per `query_wait_timeout` (default 120 s) with `server connection timeout`, surfacing to apps as connection-acquisition failures even though `pg_stat_activity` is half empty
- D: applications report the same connection-failure symptoms as true exhaustion (see Symptom Recognition)
**Indicators:**
- root: [Step 1] PostgreSQL connection count is well below `max_connections` despite application reports of connection failures
- s1: [Step 6] `cl_waiting > 0` and `maxwait > 0` in `SHOW POOLS` while [Step 1] PostgreSQL `pct_used` is moderate (< 70)
  <!-- match: {"step": 6, "predicate": "threshold", "target": "cl_waiting", "op": ">", "value": 0} -->
**Interventions:**
- **remediation** (root): right-size PgBouncer so the sum of pool sizes (plus reserve capacity) stays comfortably below PostgreSQL's effective limit; scale the DB host (more RAM) or shard if aggregate demand legitimately exceeds the RAM-bounded ceiling, before raising PostgreSQL `max_connections` blindly.

  ```ini
  default_pool_size = 25          # per (db, user) pair
  reserve_pool_size = 5
  reserve_pool_timeout = 3
  max_db_connections = 80         # < max_connections − superuser_reserved_connections
  max_client_conn = 2000
  pool_mode = transaction
  ```

  **Verification:** re-run Step 6 — `cl_waiting` and `maxwait` stay at 0 under normal load and `SHOW STATS` shows `avg_wait_time` close to 0; application-side connection-acquisition timeouts disappear in logs.
- **mitigation** (s1): hot-reload a larger `default_pool_size` on PgBouncer to drain the wait queue while you gather headroom data.

  ```bash
  # Edit /etc/pgbouncer/pgbouncer.ini, then:
  psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer -c "RELOAD;"
  psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer -c "SHOW POOLS;"
  ```

  **Risk:** Low–medium — raising `default_pool_size` is hot-reloadable, but raising it above PostgreSQL's `max_connections − superuser_reserved_connections` is dangerous; verify headroom first. **Duration:** Until you have headroom data; re-check `cl_waiting` and `maxwait` for 15 minutes after the reload. **Verification:** re-run Step 6 — `cl_waiting` and `maxwait` return to 0.

### Cause Z: Unidentified
**Statement:** Diagnostic steps do not point to any single cause above, or the evidence is conflicting and a confident root cause cannot be assigned.
**Chain:**
- root: connection-exhaustion symptoms arise from interactions the steps above do not directly probe (kernel `nofile` limits, ELB idle-timeout mismatches, DNS-induced reconnect storms, RDS proxy misconfigurations)
- D: the symptoms in Symptom Recognition are present with no clear signal from state distribution, top-consumer breakdown, idle-in-transaction sessions, or pooler counters
**Indicators:**
- [Default] no Cause A–E indicator fires, or the evidence is conflicting
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot for offline analysis, then escalate to the database SRE/DBA on call — do not apply a Cause A–E fix blindly (it risks masking the real driver and recurring at the next peak).

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

  **Risk:** Diagnostic only — these commands collect evidence and do not change system state. **Duration:** Diagnostic only; safe to leave running until the SME responds. **Verification:** escalation acknowledged with snapshots attached (full `pg_stat_activity`, pooler state, the dominant `application_name`, and the `FATAL: sorry, too many clients already` time window); a follow-up runbook or incident review is opened to capture the new failure mode.

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
</content>
</invoke>
