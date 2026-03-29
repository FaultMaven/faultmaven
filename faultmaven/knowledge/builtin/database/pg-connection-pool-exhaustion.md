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
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - postgresql
  - connection-pool
  - pgbouncer
  - max-connections
  - idle-in-transaction
difficulty: intermediate
---

# PostgreSQL Connection Pool Exhaustion

## Problem Definition

Applies to PostgreSQL 10 and later (all currently supported versions). Requires superuser or `pg_monitor` role for full diagnostic visibility. Connection via `psql` or any SQL client to the affected instance is needed; if all slots are consumed, use the superuser-reserved slots (`superuser_reserved_connections`, default 3).

Connection pool exhaustion occurs when active connections reach the `max_connections` limit (default: 100). New connection attempts fail with:

```
FATAL: sorry, too many clients already
```

Applications may also observe connection acquisition timeouts, elevated latency as the pool queue saturates, or upstream HTTP 5xx errors when web servers cannot obtain a database connection. Each PostgreSQL backend process consumes 5-10 MB of resident memory, so exhaustion often coincides with elevated memory pressure on the database host.

Common causes include idle-in-transaction sessions that hold connections indefinitely, application-side connection leaks (missing `finally` blocks or unclosed cursors), undersized pooler configuration relative to the number of application instances, absence of a connection pooler such as PgBouncer, and long-running queries that occupy slots for minutes or hours.

## Diagnostic Steps

### Step 1. Confirm connection saturation

Check current connections against the configured maximum to determine whether the server is at or near capacity.

```sql
SELECT
  count(*) AS total_connections,
  (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS max_connections,
  round(100.0 * count(*) / (SELECT setting::int FROM pg_settings WHERE name = 'max_connections'), 1) AS pct_used
FROM pg_stat_activity;
```

Expected output: `pct_used` below 70% under normal load. Values above 90% indicate imminent exhaustion. At 100%, new connections are refused.

### Step 2. Break down connections by state

Identify where connection slots are being consumed by examining the state distribution.

```sql
SELECT
  state,
  count(*) AS count,
  round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM pg_stat_activity
GROUP BY state
ORDER BY count DESC;
```

Expected output: majority of connections in `active` or `idle` state. If `idle in transaction` or `idle in transaction (aborted)` dominate, abandoned transactions are the primary consumer. A high `idle` count suggests connection leaks or oversized application pools.

### Step 3. Identify top connection consumers by application and host

Determine which application, user, or client host is consuming the most slots.

```sql
SELECT
  usename,
  application_name,
  client_addr,
  state,
  count(*) AS connections
FROM pg_stat_activity
GROUP BY usename, application_name, client_addr, state
ORDER BY connections DESC;
```

Expected output: connections distributed across known applications. A single application or host holding a disproportionate share points to a leak or misconfigured pool size in that service.

### Step 4. Find idle-in-transaction sessions

Idle-in-transaction sessions are the most common cause of exhaustion. Identify them and measure how long they have been stuck.

```sql
SELECT
  pid,
  usename,
  application_name,
  client_addr,
  state,
  now() - xact_start AS transaction_duration,
  now() - state_change AS idle_duration,
  left(query, 100) AS last_query
FROM pg_stat_activity
WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
ORDER BY xact_start ASC NULLS LAST;
```

Sessions with `transaction_duration` exceeding a few minutes are almost always bugs or abandoned connections. The `last_query` column reveals the application code path responsible.

### Step 5. Find long-idle connections

Connections idle for extended periods without an open transaction may be leaked by the application.

```sql
SELECT
  pid,
  usename,
  application_name,
  client_addr,
  backend_start,
  state_change,
  now() - state_change AS idle_since
FROM pg_stat_activity
WHERE state = 'idle'
  AND state_change < now() - interval '30 minutes'
ORDER BY state_change ASC;
```

A large number of long-idle connections from a single application indicates the pool is not reclaiming connections properly.

### Step 6. Check for blocked queries

When connections are exhausted, remaining active connections may also be blocking each other, compounding the problem.

```sql
SELECT
  blocked.pid AS blocked_pid,
  blocked.usename AS blocked_user,
  blocking.pid AS blocking_pid,
  blocking.usename AS blocking_user,
  left(blocked.query, 80) AS blocked_query,
  left(blocking.query, 80) AS blocking_query
FROM pg_stat_activity blocked
JOIN pg_locks blocked_locks ON blocked.pid = blocked_locks.pid
JOIN pg_locks blocking_locks ON blocked_locks.locktype = blocking_locks.locktype
  AND blocked_locks.database IS NOT DISTINCT FROM blocking_locks.database
  AND blocked_locks.relation IS NOT DISTINCT FROM blocking_locks.relation
  AND blocked_locks.page IS NOT DISTINCT FROM blocking_locks.page
  AND blocked_locks.tuple IS NOT DISTINCT FROM blocking_locks.tuple
  AND blocked_locks.virtualxid IS NOT DISTINCT FROM blocking_locks.virtualxid
  AND blocked_locks.transactionid IS NOT DISTINCT FROM blocking_locks.transactionid
  AND blocked_locks.pid != blocking_locks.pid
JOIN pg_stat_activity blocking ON blocking_locks.pid = blocking.pid
WHERE NOT blocked_locks.granted;
```

Any rows returned indicate active lock contention. Resolve the blocking session first to unblock dependent queries.

### Step 7. Verify superuser reserved connections

PostgreSQL reserves slots for superuser access (default: 3). Confirm you can still connect for emergency intervention.

```bash
psql -U postgres -d your_database -c "SELECT count(*) FROM pg_stat_activity;"
```

If superuser connections also fail, the situation is critical and requires OS-level process termination.

## Mitigation

### Option 1. Terminate idle-in-transaction sessions

**Risk**: Low. These sessions are stuck and not performing useful work. Applications will see a connection error and should reconnect automatically.

**Command**:

```sql
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
  AND state_change < now() - interval '5 minutes'
  AND pid != pg_backend_pid();
```

**Verify**:

```sql
SELECT count(*) FROM pg_stat_activity
WHERE state IN ('idle in transaction', 'idle in transaction (aborted)');
```

**Duration**: Immediate. Connections are freed within seconds.

### Option 2. Terminate long-idle connections

**Risk**: Low-Medium. Idle connections are not actively used, but termination forces applications to reconnect. Well-behaved connection pools handle this gracefully.

**Command**:

```sql
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'idle'
  AND state_change < now() - interval '10 minutes'
  AND pid != pg_backend_pid();
```

**Verify**:

```sql
SELECT count(*) AS total_connections FROM pg_stat_activity;
```

**Duration**: Immediate.

### Option 3. Terminate connections from a specific misbehaving application

**Risk**: Medium. Kills all connections from one application. That application will experience errors until it reconnects.

**Command**:

```sql
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE application_name = 'leaky_app'
  AND pid != pg_backend_pid();
```

**Verify**:

```sql
SELECT application_name, count(*)
FROM pg_stat_activity
GROUP BY application_name
ORDER BY count DESC;
```

**Duration**: Immediate. Monitor for the application re-establishing excessive connections.

### Option 4. Temporarily increase max_connections (requires restart)

**Risk**: High. Requires a full PostgreSQL restart causing a brief outage. Each additional connection uses 5-10 MB of RAM. Only use when terminating connections is insufficient.

**Command**:

```bash
sudo sed -i 's/^max_connections = .*/max_connections = 200/' /etc/postgresql/16/main/postgresql.conf
sudo systemctl restart postgresql
```

**Verify**:

```sql
SHOW max_connections;
```

**Duration**: Restart takes 5-30 seconds depending on recovery requirements. This is a temporary measure.

## Root Cause Resolution

**If** idle-in-transaction sessions are the primary consumer → set a server-side timeout to automatically terminate stale transactions:

```sql
ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';
SELECT pg_reload_conf();
```

Available since PostgreSQL 9.6. Fix the application code to ensure all transactions are committed or rolled back in `finally` blocks.

**If** connection leaks exist in application code → audit connection acquisition and release patterns. Ensure connections are always released via context managers or `finally` blocks:

```python
# Python / psycopg2 example
with psycopg2.connect(dsn) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT ...")
```

**If** total connections from all application instances exceed `max_connections` → deploy PgBouncer as a connection pooler:

```ini
; /etc/pgbouncer/pgbouncer.ini
[databases]
your_database = host=127.0.0.1 port=5432 dbname=your_database

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = md5
auth_file = /etc/pgbouncer/userlist.txt
pool_mode = transaction
default_pool_size = 20
max_client_conn = 1000
max_db_connections = 80
reserve_pool_size = 5
reserve_pool_timeout = 3
server_idle_timeout = 600
server_lifetime = 3600
```

Set `pool_mode = transaction` for best connection reuse. Set `max_db_connections` below PostgreSQL's `max_connections` to leave room for superuser and direct connections.

**If** long-running queries hold connections → set a statement timeout:

```sql
ALTER SYSTEM SET statement_timeout = '60s';
SELECT pg_reload_conf();
-- Or per-role:
ALTER ROLE app_user SET statement_timeout = '30s';
```

**If** idle sessions accumulate without transactions (PostgreSQL 14+) → set an idle session timeout:

```sql
ALTER SYSTEM SET idle_session_timeout = '30min';
SELECT pg_reload_conf();
```

## Verification

After applying fixes, confirm the system has recovered.

1. Connection utilization is healthy:

```sql
SELECT
  count(*) AS total,
  (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS max,
  round(100.0 * count(*) / (SELECT setting::int FROM pg_settings WHERE name = 'max_connections'), 1) AS pct_used
FROM pg_stat_activity;
```

Expect `pct_used` below 70% under normal load.

2. No stuck idle-in-transaction sessions:

```sql
SELECT count(*)
FROM pg_stat_activity
WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
  AND state_change < now() - interval '5 minutes';
```

Expect 0.

3. Applications can connect:

```bash
psql -U app_user -d your_database -c "SELECT 1;"
```

4. If PgBouncer was deployed, verify pooler health:

```bash
psql -p 6432 -U pgbouncer pgbouncer -c "SHOW POOLS;"
psql -p 6432 -U pgbouncer pgbouncer -c "SHOW STATS;"
```

Check that `sv_active` stays well below `max_db_connections`.

5. Timeout settings are active:

```sql
SHOW idle_in_transaction_session_timeout;
SHOW idle_session_timeout;
SHOW statement_timeout;
```

## Prevention

1. **Deploy a connection pooler** — Use PgBouncer or PgPool-II in front of PostgreSQL for any deployment with multiple application instances. Transaction-mode pooling allows hundreds of client connections to share a small number of database connections.

2. **Set idle-in-transaction timeout** — Configure `idle_in_transaction_session_timeout` to 5 minutes to automatically terminate abandoned transactions.

3. **Set idle session timeout** — On PostgreSQL 14+, configure `idle_session_timeout` to 30 minutes to reclaim connections from forgotten clients.

4. **Right-size application connection pools** — Each application instance pool size, multiplied by the number of instances, must stay below `max_connections`. Formula: `pool_size_per_instance = max_connections / (num_instances + buffer)`.

5. **Monitor connection utilization** — Alert at 80% of `max_connections`:

```sql
SELECT CASE
  WHEN count(*) > 0.8 * (SELECT setting::int FROM pg_settings WHERE name = 'max_connections')
  THEN 1 ELSE 0
END AS connection_alert
FROM pg_stat_activity;
```

6. **Use connection lifetime limits** — Configure PgBouncer `server_lifetime` to 1 hour to recycle backend connections and prevent stale connection accumulation.

7. **Audit application connection handling** — Ensure every connection acquisition has a corresponding release in a `finally` block, context manager, try-with-resources, or `defer` statement.

8. **Set statement_timeout per role** — Prevent long-running queries from holding connections by setting appropriate timeouts on application database roles.

9. **Reserve superuser connections** — Keep `superuser_reserved_connections` at 3 or higher to ensure emergency administrative access.

10. **Prefer pooling over raising max_connections** — Increasing `max_connections` beyond what the server can handle in memory just delays the problem. Each connection uses 5-10 MB. Connection pooling is the correct solution.

## Sources

- [PostgreSQL Documentation: The Cumulative Statistics System (pg_stat_activity)](https://www.postgresql.org/docs/current/monitoring-stats.html) — Official reference for connection monitoring views and backend state definitions.
- [PostgreSQL Documentation: Connection Settings](https://www.postgresql.org/docs/current/runtime-config-connection.html) — Official reference for `max_connections`, `superuser_reserved_connections`, `idle_in_transaction_session_timeout`, `idle_session_timeout`, and TCP keepalive settings.
- [PostgreSQL Documentation: Routine Vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html) — Explains how idle-in-transaction sessions block autovacuum and cause table bloat.
- [PgBouncer Documentation: Configuration](https://www.pgbouncer.org/config.html) — Official reference for PgBouncer pool modes, connection limits, and tuning parameters.
