---
id: pg-vacuum-bloat
title: "PostgreSQL Table Bloat from Autovacuum Failure"
domain: database
service: postgresql
symptom_class:
  - disk_full
  - latency
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - postgresql
  - vacuum
  - autovacuum
  - bloat
  - dead-tuples
  - table-maintenance
difficulty: intermediate
---

# PostgreSQL Table Bloat from Autovacuum Failure

## Problem Definition

Applies to PostgreSQL 10 and later. Requires superuser or `pg_monitor` role for visibility into `pg_stat_user_tables` and autovacuum statistics. Access to PostgreSQL configuration (`postgresql.conf` or `ALTER SYSTEM`) is needed for tuning autovacuum parameters.

Table bloat occurs when PostgreSQL's MVCC (Multi-Version Concurrency Control) mechanism retains dead row versions (tuples) that are no longer visible to any active transaction, but have not been cleaned up by VACUUM. Over time, tables and indexes grow far larger than their live data warrants, degrading query performance and consuming disk space.

Symptoms include steadily growing table sizes despite stable or declining row counts, degraded query performance as sequential scans and index scans read dead tuples, increasing disk usage that may eventually trigger disk-full alerts, and autovacuum processes that appear to run but fail to reclaim space. The PostgreSQL log may show:

```text
WARNING: oldest xmin is far in the past
HINT: Close open transactions soon to avoid wraparound problems.
```

This warning indicates that long-running transactions are preventing dead tuple cleanup, and in extreme cases, the system approaches transaction ID wraparound, which forces PostgreSQL into a safety shutdown.

Common causes include autovacuum disabled or misconfigured (too conservative thresholds), long-running transactions that prevent dead tuple cleanup (MVCC requires retaining rows visible to any open transaction), idle-in-transaction sessions holding back the `xmin` horizon, heavy UPDATE/DELETE workloads that generate dead tuples faster than autovacuum can process them, and replication with `hot_standby_feedback` preventing vacuum from removing rows still visible on replicas.

## Diagnostic Steps

### Step 1. Identify tables with the most dead tuples

Find tables where dead tuples have accumulated, indicating vacuum is not keeping up.

```sql
SELECT
  schemaname,
  relname AS table_name,
  n_live_tup,
  n_dead_tup,
  round(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
  last_autovacuum,
  last_autoanalyze,
  pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_stat_user_tables
WHERE n_dead_tup > 1000
ORDER BY n_dead_tup DESC
LIMIT 20;
```

Expected output: `dead_pct` below 10% for healthy tables. Tables with `dead_pct` above 20% are significantly bloated. If `last_autovacuum` is NULL or far in the past, autovacuum has not processed the table.

### Step 2. Check autovacuum activity

Determine whether autovacuum workers are running and what they are processing.

```sql
SELECT
  pid,
  datname,
  relid::regclass AS table_name,
  phase,
  heap_blks_total,
  heap_blks_scanned,
  heap_blks_vacuumed,
  index_vacuum_count,
  num_dead_tuples
FROM pg_stat_progress_vacuum;
```

If no rows are returned, no vacuum is currently running. If a vacuum appears stuck in the same phase for an extended period, check for blocking transactions.

### Step 3. Check for transactions preventing vacuum

Vacuum cannot remove dead tuples that are still visible to any open transaction. Find the oldest open transaction.

```sql
SELECT
  pid,
  usename,
  application_name,
  state,
  backend_xmin,
  now() - xact_start AS xact_duration,
  left(query, 100) AS query
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY age(backend_xmin) DESC
LIMIT 10;
```

The transaction with the oldest `backend_xmin` is the one preventing vacuum from cleaning up dead tuples. If it is an `idle in transaction` session, it is the most likely culprit.

### Step 4. Check transaction ID age and wraparound risk

Monitor how close the database is to transaction ID wraparound, which forces an emergency vacuum.

```sql
SELECT
  datname,
  age(datfrozenxid) AS xid_age,
  current_setting('autovacuum_freeze_max_age')::bigint AS freeze_max_age,
  round(100.0 * age(datfrozenxid) / current_setting('autovacuum_freeze_max_age')::bigint, 1) AS pct_to_wraparound
FROM pg_database
ORDER BY xid_age DESC;
```

If `pct_to_wraparound` exceeds 75%, the system is at risk. At 95%, PostgreSQL will refuse new write transactions until an emergency anti-wraparound vacuum completes.

### Step 5. Estimate actual table bloat

Compare the actual table size to the expected size based on live rows.

```sql
SELECT
  schemaname,
  relname AS table_name,
  pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
  pg_size_pretty(pg_relation_size(relid)) AS table_size,
  pg_size_pretty(pg_indexes_size(relid)) AS indexes_size,
  n_live_tup,
  n_dead_tup
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 20;
```

A table with 1 million live rows and a size of 10 GB is likely bloated compared to a similar table that is 500 MB. Cross-reference with `n_dead_tup` to confirm.

### Step 6. Check autovacuum configuration

Verify the current autovacuum settings to determine if they are too conservative for the workload.

```sql
SELECT name, setting, unit, short_desc
FROM pg_settings
WHERE name LIKE 'autovacuum%'
ORDER BY name;
```

Key settings to review: `autovacuum_vacuum_threshold` (default: 50 rows), `autovacuum_vacuum_scale_factor` (default: 0.2, meaning 20% of table must be dead), `autovacuum_max_workers` (default: 3), `autovacuum_vacuum_cost_delay` (throttles vacuum I/O speed).

## Mitigation

### Option 1. Run manual VACUUM on the most bloated table

**Risk**: Low. VACUUM does not lock the table for reads or writes. It reclaims dead tuple space for reuse within the table (does not return space to the OS).

**Command**:

```sql
VACUUM (VERBOSE) bloated_table;
```

**Verify**:

```sql
SELECT relname, n_dead_tup, last_vacuum
FROM pg_stat_user_tables
WHERE relname = 'bloated_table';
```

**Duration**: Minutes to hours depending on table size. A 10 GB table typically takes 5-15 minutes.

### Option 2. Terminate the transaction blocking vacuum

**Risk**: Low-Medium. The blocking application receives a connection error. Necessary when an idle-in-transaction session is preventing all vacuum progress.

**Command**:

```sql
-- Find the blocking PID from Step 3, then terminate it
SELECT pg_terminate_backend(12345);
```

**Verify**:

```sql
SELECT min(age(backend_xmin)) AS oldest_xmin_age
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL;
```

**Duration**: Immediate. Vacuum can now proceed to clean up previously pinned dead tuples.

### Option 3. Run VACUUM FULL to reclaim disk space (requires downtime)

**Risk**: High. VACUUM FULL rewrites the entire table and acquires an AccessExclusiveLock, blocking all reads and writes for the duration. Only use when disk space recovery is critical.

**Command**:

```sql
VACUUM FULL bloated_table;
```

**Verify**:

```sql
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_stat_user_tables
WHERE relname = 'bloated_table';
```

**Duration**: Proportional to table size. A 50 GB table may take 30-60 minutes. Requires free disk space equal to the table size for the rewrite.

### Option 4. Use pg_repack for online table compaction (no downtime)

**Risk**: Medium. Requires the `pg_repack` extension to be installed. Rebuilds the table online without blocking reads or writes. Uses additional disk space during the operation.

**Command**:

```bash
pg_repack -d your_database -t bloated_table
```

**Verify**:

```sql
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_stat_user_tables
WHERE relname = 'bloated_table';
```

**Duration**: Similar to VACUUM FULL but without downtime.

## Root Cause Resolution

**If** autovacuum thresholds are too conservative for high-churn tables → lower the scale factor and threshold for specific tables:

```sql
ALTER TABLE high_churn_table SET (
  autovacuum_vacuum_threshold = 100,
  autovacuum_vacuum_scale_factor = 0.02,
  autovacuum_analyze_threshold = 100,
  autovacuum_analyze_scale_factor = 0.02
);
```

This triggers autovacuum when 2% of rows are dead (instead of the default 20%).

**If** autovacuum workers are too slow due to I/O throttling → reduce the cost delay:

```sql
-- Globally (or per-table)
ALTER SYSTEM SET autovacuum_vacuum_cost_delay = '2ms';  -- default is 2ms in PG 12+, 20ms in older
ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 400;     -- default is -1 (uses vacuum_cost_limit = 200)
SELECT pg_reload_conf();
```

For specific high-churn tables:

```sql
ALTER TABLE high_churn_table SET (
  autovacuum_vacuum_cost_delay = '0ms',
  autovacuum_vacuum_cost_limit = 1000
);
```

**If** too few autovacuum workers are available → increase the worker count:

```sql
-- Requires restart
ALTER SYSTEM SET autovacuum_max_workers = 6;  -- default is 3
```

**If** idle-in-transaction sessions repeatedly block vacuum → set the server-side timeout:

```sql
ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';
SELECT pg_reload_conf();
```

**If** replication with `hot_standby_feedback` prevents vacuum on the primary → evaluate whether the replicas truly need long-running queries. Set `max_standby_streaming_delay` on replicas to limit how long replay is paused, and consider routing analytics queries to a dedicated replica.

**If** the database is approaching transaction ID wraparound → run an emergency anti-wraparound vacuum:

```sql
-- This vacuum cannot be interrupted; let it complete
VACUUM FREEZE;
```

Monitor progress with `pg_stat_progress_vacuum`.

## Verification

After applying fixes, confirm bloat has been addressed.

1. Dead tuple counts have decreased:

```sql
SELECT relname, n_dead_tup, dead_pct
FROM (
  SELECT relname, n_dead_tup,
    round(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 1) AS dead_pct
  FROM pg_stat_user_tables
) t
WHERE n_dead_tup > 1000
ORDER BY n_dead_tup DESC
LIMIT 10;
```

Expect `dead_pct` below 10%.

1. Autovacuum is running regularly:

```sql
SELECT relname, last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
WHERE last_autovacuum IS NOT NULL
ORDER BY last_autovacuum DESC
LIMIT 10;
```

1. Transaction ID age is healthy:

```sql
SELECT datname, age(datfrozenxid) AS xid_age,
  round(100.0 * age(datfrozenxid) / current_setting('autovacuum_freeze_max_age')::bigint, 1) AS pct
FROM pg_database
ORDER BY xid_age DESC;
```

Expect `pct` below 50%.

1. Table sizes are stable or decreasing:

```sql
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 10;
```

1. No blocking transactions with old xmin:

```sql
SELECT count(*) FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND now() - xact_start > interval '5 minutes';
```

Expect 0.

## Prevention

1. **Tune autovacuum per table** — High-churn tables need lower `autovacuum_vacuum_scale_factor` (0.01-0.05) and lower thresholds. Do not rely on global defaults for tables with millions of modifications per day.

2. **Set idle_in_transaction_session_timeout** — Configure to 5 minutes to automatically terminate sessions that block vacuum progress.

3. **Monitor dead tuple ratios** — Alert when any table exceeds 20% dead tuples. Use `pg_stat_user_tables.n_dead_tup` as the metric.

4. **Monitor transaction ID age** — Alert when `age(datfrozenxid)` exceeds 500 million (roughly 25% of the wraparound limit).

5. **Increase autovacuum_max_workers** — For databases with many large tables, increase from the default of 3 to 5-8 to ensure all tables receive timely maintenance.

6. **Reduce autovacuum_vacuum_cost_delay** — On modern SSDs, the default throttling is unnecessarily conservative. Reduce to 0-2ms for faster vacuum throughput.

7. **Schedule VACUUM ANALYZE during low-traffic windows** — Supplement autovacuum with manual maintenance during off-peak hours for the largest tables.

8. **Use pg_repack for periodic compaction** — Schedule monthly `pg_repack` runs for tables that accumulate bloat despite regular vacuuming, to reclaim disk space without downtime.

9. **Audit replication feedback impact** — If using `hot_standby_feedback`, monitor for vacuum delays on the primary. Consider using `max_slot_wal_keep_size` to limit the impact.

## Sources

- [PostgreSQL Documentation: Routine Vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html) — Official reference for VACUUM, autovacuum, and transaction ID wraparound prevention.
- [PostgreSQL Documentation: Autovacuum Configuration](https://www.postgresql.org/docs/current/runtime-config-autovacuum.html) — Official reference for all autovacuum tuning parameters.
- [PostgreSQL Documentation: pg_stat_user_tables](https://www.postgresql.org/docs/current/monitoring-stats.html#MONITORING-PG-STAT-ALL-TABLES-VIEW) — Official reference for table-level statistics including dead tuple counts and vacuum timestamps.
- [pg_repack Documentation](https://reorg.github.io/pg_repack/) — Official documentation for the pg_repack extension for online table compaction.
