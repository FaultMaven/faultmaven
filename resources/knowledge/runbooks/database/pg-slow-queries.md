---
id: "pg-slow-queries"
title: "PostgreSQL Slow Query Diagnosis"
domain: database
service: postgresql
symptom_class: [latency]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags: [pg-stat-statements, explain-analyze, indexes, query-optimization, autovacuum, work-mem]
difficulty: intermediate
---

## Symptom Recognition

- Application endpoints backed by PostgreSQL show elevated p95/p99 latency or timeout errors.
- PostgreSQL log emits lines matching `LOG: duration: <N> ms  statement: SELECT ...` (requires `log_min_duration_statement`).
- `pg_stat_statements` shows individual queries with `mean_exec_time > 1000` ms or `total_exec_time` dominating the top-10 list.
- CPU or I/O utilization on the database host is high without a proportional increase in connection count.
- Connection pool exhaustion follows shortly after latency spike, as slow queries hold connections longer.

## Applicability

- PostgreSQL 10 and later.
- `pg_stat_statements` extension must be loaded (`shared_preload_libraries = 'pg_stat_statements'`) and enabled per database (`CREATE EXTENSION pg_stat_statements`). Requires a PostgreSQL restart if not already loaded.
- Requires superuser or `pg_monitor` role for full visibility into all users' queries.
- `EXPLAIN ANALYZE` runs the query — wrap mutations in a transaction and roll back; safe to run as-is for SELECT.
- I/O timing in `pg_stat_statements` (the `shared_blk_read_time` column) requires `track_io_timing = on` in `postgresql.conf`.

## Diagnostic Steps

### Step 1: Rank queries by cumulative execution time

```sql
SELECT
  queryid,
  calls,
  round(total_exec_time::numeric, 2)  AS total_time_ms,
  round(mean_exec_time::numeric, 2)   AS mean_time_ms,
  round(max_exec_time::numeric, 2)    AS max_time_ms,
  rows,
  left(query, 120)                    AS query_snippet
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

Expected output: ranked list of normalized queries. Focus on the top entries — high `total_time_ms` indicates cumulative load, high `mean_time_ms` indicates individually slow queries. Note the `queryid` of suspects for use in later steps.

### Step 2: Find queries with high block reads per call

```sql
SELECT
  queryid,
  calls,
  round(shared_blks_read::numeric / NULLIF(calls, 0), 2) AS blks_read_per_call,
  round(rows::numeric / NULLIF(calls, 0), 2)             AS rows_per_call,
  round(mean_exec_time::numeric, 2)                      AS mean_time_ms,
  left(query, 120)                                       AS query_snippet
FROM pg_stat_statements
WHERE calls > 100
ORDER BY blks_read_per_call DESC
LIMIT 20;
```

Expected output: queries with high `blks_read_per_call` relative to `rows_per_call` are likely performing sequential scans and are candidates for index creation.

### Step 3: Run EXPLAIN ANALYZE on a suspect query

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT ... ; -- paste the slow query here with representative literal values
```

Expected output: execution plan tree. Look for `Seq Scan` on large tables, `Sort Method: external merge Disk`, `Batches: N` greater than 1 on Hash nodes, `Rows Removed by Filter` much larger than actual rows, and significant divergence between estimated and actual row counts.

### Step 4: Check for missing indexes on large tables

```sql
SELECT
  schemaname,
  relname                                              AS table_name,
  seq_scan,
  idx_scan,
  seq_scan - idx_scan                                  AS excess_seq_scans,
  pg_size_pretty(pg_relation_size(relid))              AS table_size
FROM pg_stat_user_tables
WHERE seq_scan > idx_scan
  AND pg_relation_size(relid) > 10 * 1024 * 1024
ORDER BY excess_seq_scans DESC
LIMIT 20;
```

Expected output: tables where `excess_seq_scans` is large and `table_size` is significant are strong candidates for index creation.

### Step 5: Check for unused indexes

```sql
SELECT
  schemaname,
  relname          AS table_name,
  indexrelname     AS index_name,
  idx_scan         AS times_used,
  pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
WHERE idx_scan = 0
  AND indexrelname NOT LIKE '%pkey%'
  AND indexrelname NOT LIKE '%unique%'
ORDER BY pg_relation_size(indexrelid) DESC
LIMIT 20;
```

Expected output: indexes with `times_used = 0` since the last statistics reset. Verify reset time first:

```sql
SELECT stats_reset FROM pg_stat_bgwriter;
```

### Step 6: Check for stale table statistics

```sql
SELECT
  schemaname,
  relname                  AS table_name,
  n_live_tup,
  n_mod_since_analyze,
  last_autoanalyze,
  last_analyze
FROM pg_stat_user_tables
WHERE n_mod_since_analyze > n_live_tup * 0.10
ORDER BY n_mod_since_analyze DESC
LIMIT 20;
```

Expected output: tables where `n_mod_since_analyze` exceeds 10% of `n_live_tup` have stale statistics and the planner may choose suboptimal plans.

### Step 7: Check for table bloat

```sql
SELECT
  schemaname,
  relname                  AS table_name,
  n_live_tup,
  n_dead_tup,
  round(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
  last_autovacuum,
  pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_stat_user_tables
WHERE n_dead_tup > 10000
ORDER BY n_dead_tup DESC
LIMIT 20;
```

Expected output: tables with `dead_pct` above 20% are bloated — queries must read additional pages containing dead tuples, inflating I/O costs.

## Causes

### Cause A: Missing Index Forcing Sequential Scan

**Statement:** A required index is absent, forcing PostgreSQL to sequentially scan the entire table for every query execution.

**Chain:**
- root: No index matches the query's WHERE clause or JOIN condition on the target table.
- s1: The planner must read every data page of the table to find qualifying rows (sequential scan).
- s2: Per-execution I/O and CPU cost scales with table size, far exceeding an index scan; scans hold shared buffers longer under concurrency.
- D: Query latency is elevated and database host CPU/I/O is high (Symptom Recognition).

**Indicators:**
- root: [Step 4] Table appears with a high `excess_seq_scans` value.
- s1: [Step 3] Plan contains `Seq Scan on <table>` where `rows` is large (thousands or more).

**Interventions:**
- **remediation** (root): create the missing index so the planner can use an index scan. Use a partial index when queries filter on a common condition.

  ```sql
  -- Partial index when queries filter on a common condition
  CREATE INDEX CONCURRENTLY idx_orders_active
    ON orders (created_at)
    WHERE status = 'active';
  ```

  **Verification:** Re-run Step 3 on the same query; plan must show `Index Scan` or `Bitmap Index Scan` instead of `Seq Scan`. Confirm `mean_exec_time` in Step 1 dropped for the affected `queryid`.
- **mitigation** (root): build a straight index online to immediately restore an index path.

  ```sql
  CREATE INDEX CONCURRENTLY idx_table_column
    ON table_name (column_name);
  ```

  **Risk:** `CREATE INDEX CONCURRENTLY` is safe for production; without `CONCURRENTLY` the table is locked for writes during build. Rollback: `DROP INDEX CONCURRENTLY idx_table_column;`. **Duration:** Index build time scales with table size; a 10 GB table typically takes 1–5 minutes. **Verification:** Re-run Step 3; plan shows an index scan for the affected query.

---

### Cause B: Stale Table Statistics Causing Bad Plan Choice

**Statement:** Outdated row-count and histogram statistics cause the query planner to select an inefficient execution plan such as a nested loop over a hash join.

**Chain:**
- root: Table statistics in `pg_statistic` are stale because autovacuum hasn't analyzed or churn outpaces the default scale factors.
- s1: The planner's estimated row counts and selectivity diverge significantly from actual counts.
- s2: The planner picks an inefficient strategy (e.g. a nested loop expecting 10 rows when 50,000 are returned).
- D: Query latency is elevated due to the catastrophic plan choice (Symptom Recognition).

**Indicators:**
- root: [Step 6] Table appears with `n_mod_since_analyze` exceeding 10% of `n_live_tup`.
- s1: [Step 3] Estimated `rows` in plan differs from `actual rows` by more than 10x.

**Interventions:**
- **remediation** (root): tune autovacuum to analyze high-churn tables more aggressively so statistics stay fresh.

  ```sql
  -- Tune autovacuum to analyze more aggressively on high-churn tables
  ALTER TABLE high_churn_table SET (
    autovacuum_analyze_threshold    = 100,
    autovacuum_analyze_scale_factor = 0.02
  );
  ```

  **Verification:** Re-run Step 6 — `n_mod_since_analyze` should be near 0. Re-run Step 3 — estimated rows must align within 2x of actual rows, and the plan must not show an unexpectedly expensive join strategy. Rollback: `ALTER TABLE high_churn_table RESET (autovacuum_analyze_threshold, autovacuum_analyze_scale_factor);`.
- **mitigation** (root): refresh statistics immediately on the affected table.

  ```sql
  ANALYZE table_name;
  ```

  **Risk:** Low. `ANALYZE` holds only a brief `ShareUpdateExclusiveLock` and does not block reads or writes. **Duration:** Seconds to minutes depending on table size and `default_statistics_target`; statistics drift again as churn continues. **Verification:** Re-run Step 6 — `n_mod_since_analyze` near 0 for the table.

---

### Cause C: Sort or Hash Operation Spilling to Disk

**Statement:** Insufficient `work_mem` causes sort and hash join operations to spill intermediate data to temporary disk files, adding seconds of latency.

**Chain:**
- root: `work_mem` is too small for the query's sort and hash join working sets.
- s1: When a dataset exceeds `work_mem`, PostgreSQL writes temporary files to disk (external merge sorts, extra hash batches).
- s2: Temp-file disk I/O is orders of magnitude slower than RAM and contends with regular table I/O; multiple sort/hash nodes multiply the demand.
- D: Query execution time is disproportionately high (Symptom Recognition).

**Indicators:**
- root: [Step 1] Query has high `mean_exec_time` while `shared_blks_read` is only moderate — execution time is disproportionate to block reads.
- s1: [Step 3] Plan shows `Sort Method: external merge Disk` or `Batches: N` greater than 1 on a Hash node.

**Interventions:**
- **remediation** (root): raise `work_mem` persistently for the workload's role.

  ```sql
  ALTER ROLE analytics_user SET work_mem = '512MB';
  ```

  **Verification:** Re-run `EXPLAIN (ANALYZE, BUFFERS)` on the affected query. Sort nodes must show `Sort Method: quicksort` (in-memory) and Hash Join nodes must show `Batches: 1`. Rollback: `ALTER ROLE analytics_user RESET work_mem;`.
- **mitigation** (root): raise `work_mem` for the current session (or single role) to test the effect quickly.

  ```sql
  -- Per-session (immediate, for testing)
  SET work_mem = '256MB';

  -- Per-role (persistent for specific workload)
  ALTER ROLE analytics_user SET work_mem = '256MB';
  ```

  **Risk:** Low-medium. Setting `work_mem` too high globally causes out-of-memory if many connections use sort/hash concurrently. Prefer per-role or per-session setting. **Duration:** Per-session change is immediate and reverts at session close; per-role takes effect at next connection. **Verification:** Re-run `EXPLAIN (ANALYZE, BUFFERS)` — sort nodes show `quicksort`, hash nodes show `Batches: 1`.

---

### Cause D: Table Bloat Inflating Scan Costs

**Statement:** Dead tuples from un-vacuumed rows force scans to read additional data pages, increasing I/O cost even when row counts are unchanged.

**Chain:**
- root: Autovacuum is misconfigured or disabled, so VACUUM does not reclaim dead tuples.
- s1: Dead tuple versions accumulate inside the table's data pages (bloat).
- s2: Sequential scans read every page including those dominated by dead tuples, and index scans must visit dead heap pages — inflating effective I/O.
- D: Query latency is elevated and host I/O is high without a row-count increase (Symptom Recognition).

**Indicators:**
- root: [Step 7] Table has `dead_pct` above 20% and appears in top results.
- s1: [Step 3] Plan shows higher-than-expected cost for a table whose row count alone does not justify it.

**Interventions:**
- **remediation** (root): tune autovacuum to reclaim dead tuples more aggressively on the bloated table.

  ```sql
  -- Tune autovacuum to reclaim dead tuples more aggressively
  ALTER TABLE bloated_table SET (
    autovacuum_vacuum_threshold    = 50,
    autovacuum_vacuum_scale_factor = 0.01
  );
  ```

  **Verification:** Re-run Step 7 — `dead_pct` must drop below 5% after VACUUM completes. Re-run Step 1 — `total_exec_time` for affected queries should decrease proportionally to the page reduction. Rollback: `ALTER TABLE bloated_table RESET (autovacuum_vacuum_threshold, autovacuum_vacuum_scale_factor);`.
- **mitigation** (s1): run a manual VACUUM to reclaim the accumulated dead tuples now.

  ```sql
  VACUUM (VERBOSE) bloated_table;
  ```

  **Risk:** Low. `VACUUM` holds `ShareUpdateExclusiveLock` — reads and writes proceed normally. `VACUUM FULL` locks the table exclusively; avoid in production unless absolutely necessary. **Duration:** Minutes to hours depending on table size and dead tuple count; bloat returns if autovacuum stays untuned. **Verification:** Re-run Step 7 — `dead_pct` drops below 5%.

---

### Cause E: Inefficient Query Pattern (Correlated Subquery or SELECT *)

**Statement:** The query itself is structurally inefficient — using a correlated subquery executed once per outer row or selecting all columns with SELECT *, causing unnecessary I/O.

**Chain:**
- root: The query uses a correlated subquery and/or `SELECT *` rather than a JOIN and an explicit column list.
- s1: A correlated subquery is re-executed once per outer row (e.g. 100,000 outer rows → 100,000 subquery runs); `SELECT *` transfers all column data regardless of need.
- s2: Per-row subquery execution and wide column transfer multiply I/O and shared buffer pressure regardless of indexing.
- D: Query mean execution time is elevated (Symptom Recognition).

**Indicators:**
- root: [Step 3] Plan shows `SubPlan` or `InitPlan` nodes with a high `loops` count equal to the outer row count.
- s1: [Step 2] Query has very high `rows_per_call` but the number does not match expected business logic result size.

**Interventions:**
- **remediation** (root): rewrite the query to use explicit JOINs instead of correlated subqueries, and list only required columns instead of `SELECT *`.

  ```sql
  -- Replace correlated subquery with a JOIN
  SELECT t.id, s.value
  FROM target_table t
  JOIN subquery_table s ON s.target_id = t.id
  WHERE t.status = 'active';
  ```

  **Verification:** Re-run Step 3 on the rewritten query — `SubPlan` nodes must be absent and total estimated cost must decrease materially. Confirm Step 1 shows reduced `mean_exec_time` for the normalized query.

---

### Cause F: Generic Prepared Statement Plan Performing Poorly

**Statement:** A prepared statement compiled with a generic plan performs poorly for specific parameter values where a custom plan would choose a different access path.

**Chain:**
- root: A prepared statement runs on a column with skewed value distribution, so one cached plan cannot suit all parameter values.
- s1: After five executions PostgreSQL caches a generic plan built without knowing actual parameter values, relying on average statistics.
- s2: For high-selectivity-variance values the generic plan uses a sequential scan where a custom plan would use an index scan.
- D: Queries with rare or common parameter values underperform, with variable latency across executions (Symptom Recognition).

**Indicators:**
- root: [Step 1] High `stddev_exec_time` relative to `mean_exec_time` for the same `queryid` (variable performance across executions).
- s1: [Step 3] Plan shows `Seq Scan` but adding `WHERE column = <literal>` produces an index scan in a separate EXPLAIN.

**Interventions:**
- **remediation** (root): force custom plans for the affected application role.

  ```sql
  -- Force custom plans for the affected application role
  ALTER ROLE app_user SET plan_cache_mode = 'force_custom_plan';
  ```

  **Verification:** Re-run Step 3 using `EXPLAIN (ANALYZE, BUFFERS)` after setting `force_custom_plan` — plan must show `Index Scan` for selective parameter values. Confirm `mean_exec_time` in Step 1 is stable with lower `stddev_exec_time`. Rollback: `ALTER ROLE app_user RESET plan_cache_mode;`.
- **mitigation** (root): force a custom plan for the current session to confirm the diagnosis.

  ```sql
  -- Force custom plan for current session
  SET plan_cache_mode = 'force_custom_plan';
  ```

  **Risk:** Low. Per-session setting reverts at session close; forcing custom plans increases planning time per query (acceptable for OLTP, avoid for high-frequency batch jobs). **Duration:** Effective immediately for the current session only. **Verification:** Re-run Step 3; plan shows `Index Scan` for selective values.

---

### Cause Z: Unidentified

**Statement:** The slow query cause could not be identified from available diagnostic data and requires deeper investigation or escalation.

**Chain:**
- root: The root cause lies outside what `pg_stat_statements` and Steps 1–7 expose (e.g. lock waits, OS I/O scheduler pressure, storage network latency, or a planner edge case).
- D: The slow query persists with no known pattern matched (Symptom Recognition).

**Indicators:**
- root: [Default] None of the preceding causes matched the diagnostic output.

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot, then escalate to the database SME. Enable `auto_explain` to record plans of slow queries automatically.

  ```sql
  -- Enable auto_explain to capture plans of slow queries automatically
  LOAD 'auto_explain';
  SET auto_explain.log_min_duration = 1000;  -- ms
  SET auto_explain.log_analyze = true;
  SET auto_explain.log_buffers = true;
  ```

  **Risk:** Low. Enabling additional logging temporarily increases disk I/O for log writes. **Duration:** Per-session; disable when investigation is complete. **Verification:** Escalation ticket created with: (1) output of Steps 1–7, (2) full `EXPLAIN (ANALYZE, BUFFERS)` plan, (3) relevant PostgreSQL log lines with timestamps, (4) OS-level I/O and CPU metrics from the database host during the slow period.

## Prevention

1. **Enable `pg_stat_statements`** — Add `pg_stat_statements` to `shared_preload_libraries` and set `pg_stat_statements.track = all`. This is the foundational tool for identifying slow queries in production and must be present before an incident occurs.

2. **Configure `log_min_duration_statement`** — Set to 1000–5000 ms in `postgresql.conf` to log all queries exceeding the threshold. Review slow query logs weekly.

3. **Set `statement_timeout` per role** — Prevent runaway queries from monopolizing resources. Use shorter timeouts for OLTP roles and longer timeouts for analytics roles:

    ```sql
    ALTER ROLE app_user SET statement_timeout = '30s';
    ALTER ROLE analytics_user SET statement_timeout = '300s';
    ```

4. **Run ANALYZE after bulk data changes** — After large INSERT, UPDATE, DELETE, or COPY operations, immediately run `ANALYZE table_name` on affected tables to prevent planner regressions.

5. **Tune autovacuum for high-churn tables** — Lower `autovacuum_analyze_scale_factor` to 0.02 and `autovacuum_vacuum_scale_factor` to 0.01 for tables with high modification rates.

6. **Audit indexes on a schedule** — Weekly: query `pg_stat_user_indexes` to retire unused indexes (reduce write overhead) and `pg_stat_user_tables` to find tables missing indexes.

7. **Enable `track_io_timing`** — Setting `track_io_timing = on` populates `shared_blk_read_time` in `pg_stat_statements`, enabling I/O-heavy queries to be distinguished from CPU-heavy ones.

8. **Use connection pooling (PgBouncer)** — Transaction-mode pooling prevents slow queries from exhausting the connection limit, buying time to diagnose without a cascading pool failure.

9. **Partition large tables** — Tables exceeding 100 GB benefit from declarative partitioning; the planner performs partition pruning to limit scans to relevant partitions, reducing I/O proportionally.

## Sources

- [PostgreSQL Documentation: pg_stat_statements](https://www.postgresql.org/docs/current/pgstatstatements.html) — Priority 1. Used for column reference, configuration options, reset functions, and query patterns.
- [PostgreSQL Documentation: EXPLAIN](https://www.postgresql.org/docs/current/sql-explain.html) — Priority 1. Used for option syntax, output field definitions, and identification of Seq Scan, spill-to-disk, and estimate divergence patterns.
- [PostgreSQL Documentation: Index Types](https://www.postgresql.org/docs/current/indexes-types.html) — Priority 1. Used for index type selection guidance, CONCURRENTLY syntax, and partial index examples.
- [PostgreSQL Documentation: The Cumulative Statistics System](https://www.postgresql.org/docs/current/monitoring-stats.html) — Priority 1. Used for `pg_stat_user_tables` and `pg_stat_user_indexes` column definitions and diagnostic query patterns.
