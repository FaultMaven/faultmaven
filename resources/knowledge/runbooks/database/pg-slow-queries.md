---
id: "pg-slow-queries"
title: "PostgreSQL Slow Query Diagnosis"
domain: database
service: postgresql
symptom_class: [latency]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
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

### Step 1: Identify top queries by cumulative execution time

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

### Step 2: Identify queries with high block reads per call

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

**Mechanism:** Without an index matching the query's WHERE clause or JOIN condition, the planner must read every data page of the table to find qualifying rows. On tables larger than a few thousand rows, this produces dramatically higher I/O and CPU cost than an index scan. Each sequential scan locks out shared buffer pages longer, compounding latency under concurrent load.

**Indicator:**

- [Step 3] Plan contains `Seq Scan on <table>` where `rows` is large (thousands or more)
- [Step 4] Table appears with high `excess_seq_scans` value

<!-- match: {"step": 3, "predicate": "contains", "target": "Seq Scan"} -->
<!-- match: {"step": 4, "predicate": "threshold", "target": "excess_seq_scans", "op": ">", "value": 1000} -->

**Mitigation:**

- **Risk:** `CREATE INDEX CONCURRENTLY` is safe for production; without `CONCURRENTLY` the table is locked for writes during build.
- **Command:**

  ```sql
  CREATE INDEX CONCURRENTLY idx_table_column
    ON table_name (column_name);
  ```

- **Duration:** Index build time scales with table size; a 10 GB table typically takes 1–5 minutes.

**Resolution:**

```sql
-- Partial index when queries filter on a common condition
CREATE INDEX CONCURRENTLY idx_orders_active
  ON orders (created_at)
  WHERE status = 'active';
```

**Impact:** Immediate for new queries once the index is marked valid. Existing connections unaffected.

**Rollback:**

```sql
DROP INDEX CONCURRENTLY idx_table_column;
```

**Verification:** Re-run Step 3 on the same query; plan must show `Index Scan` or `Bitmap Index Scan` instead of `Seq Scan`. Confirm `mean_exec_time` in Step 1 dropped for the affected `queryid`.

---

### Cause B: Stale Table Statistics Causing Bad Plan Choice

**Statement:** Outdated row-count and histogram statistics cause the query planner to select an inefficient execution plan such as a nested loop over a hash join.

**Mechanism:** The planner relies on `pg_statistic` data (maintained by ANALYZE) to estimate row counts and selectivity. When statistics are stale — because autovacuum hasn't run or the table changes faster than the default scale factors — estimated row counts diverge significantly from actual counts. The planner may choose a nested loop expecting 10 rows when 50,000 are returned, producing catastrophic performance.

**Indicator:**

- [Step 3] Estimated `rows` in plan differs from `actual rows` by more than 10x
- [Step 6] Table appears with `n_mod_since_analyze` exceeding 10% of `n_live_tup`

<!-- match: {"step": 6, "predicate": "threshold", "target": "n_mod_since_analyze_ratio", "op": ">", "value": 0.10} -->

**Mitigation:**

- **Risk:** Low. `ANALYZE` holds only a brief `ShareUpdateExclusiveLock` and does not block reads or writes.
- **Command:**

  ```sql
  ANALYZE table_name;
  ```

- **Duration:** Seconds to minutes depending on table size and `default_statistics_target`.

**Resolution:**

```sql
-- Tune autovacuum to analyze more aggressively on high-churn tables
ALTER TABLE high_churn_table SET (
  autovacuum_analyze_threshold    = 100,
  autovacuum_analyze_scale_factor = 0.02
);
```

**Impact:** Per-table storage parameter change; takes effect at next autovacuum cycle. No restart needed.

**Rollback:**

```sql
ALTER TABLE high_churn_table RESET (
  autovacuum_analyze_threshold,
  autovacuum_analyze_scale_factor
);
```

**Verification:** Re-run Step 6 — `n_mod_since_analyze` should be near 0 for the affected table. Re-run Step 3 — estimated rows must align within 2x of actual rows, and plan must not show an unexpectedly expensive join strategy.

---

### Cause C: Sort or Hash Operation Spilling to Disk

**Statement:** Insufficient `work_mem` causes sort and hash join operations to spill intermediate data to temporary disk files, adding seconds of latency.

**Mechanism:** Each sort node and hash join in a query plan is allocated up to `work_mem` bytes of memory. When the dataset exceeds this limit, PostgreSQL writes temporary files to disk for merge passes (Sort) or additional batches (Hash Join). A single query plan with multiple sort/hash nodes multiplies memory demand. Disk I/O for temp files is orders of magnitude slower than RAM-based operations, and temp file creation contends with regular table I/O.

**Indicator:**

- [Step 3] Plan shows `Sort Method: external merge Disk` or `Batches: N` greater than 1 on a Hash node
- [Step 1] Query has high `mean_exec_time` and `shared_blks_read` is moderate but execution time is disproportionate

<!-- match: {"step": 3, "predicate": "contains", "target": "external merge Disk"} -->

**Mitigation:**

- **Risk:** Low-medium. Setting `work_mem` too high globally causes out-of-memory if many connections use sort/hash concurrently. Prefer per-role or per-session setting.
- **Command:**

  ```sql
  -- Per-session (immediate, for testing)
  SET work_mem = '256MB';

  -- Per-role (persistent for specific workload)
  ALTER ROLE analytics_user SET work_mem = '256MB';
  ```

- **Duration:** Per-session change is immediate; per-role takes effect at next connection.

**Resolution:**

```sql
ALTER ROLE analytics_user SET work_mem = '512MB';
```

**Impact:** Affects all new sessions for the role. Monitor total memory consumption if many concurrent analytics sessions are expected.

**Rollback:**

```sql
ALTER ROLE analytics_user RESET work_mem;
```

**Verification:** Re-run `EXPLAIN (ANALYZE, BUFFERS)` on the affected query. Sort nodes must show `Sort Method: quicksort` (in-memory) and Hash Join nodes must show `Batches: 1`.

---

### Cause D: Table Bloat Inflating Scan Costs

**Statement:** Dead tuples from un-vacuumed rows force sequential scans to read additional data pages, increasing I/O cost even when row counts are unchanged.

**Mechanism:** PostgreSQL's MVCC model retains old tuple versions as dead rows until VACUUM reclaims them. When autovacuum is misconfigured or disabled, dead tuples accumulate inside data pages. A sequential scan must read every page including those dominated by dead tuples, inflating the effective table size read from disk. Index scans are similarly degraded when index entries point to dead heap pages that must be visited before the dead tuple is skipped.

**Indicator:**

- [Step 7] Table has `dead_pct` above 20% and appears in top results
- [Step 3] Plan shows higher-than-expected cost for a table whose row count alone does not justify it

<!-- match: {"step": 7, "predicate": "threshold", "target": "dead_pct", "op": ">", "value": 20} -->

**Mitigation:**

- **Risk:** Low. `VACUUM` holds `ShareUpdateExclusiveLock` — reads and writes proceed normally. `VACUUM FULL` locks the table exclusively; avoid in production unless absolutely necessary.
- **Command:**

  ```sql
  VACUUM (VERBOSE) bloated_table;
  ```

- **Duration:** Minutes to hours depending on table size and dead tuple count.

**Resolution:**

```sql
-- Tune autovacuum to reclaim dead tuples more aggressively
ALTER TABLE bloated_table SET (
  autovacuum_vacuum_threshold    = 50,
  autovacuum_vacuum_scale_factor = 0.01
);
```

**Impact:** Per-table parameter; takes effect at next autovacuum cycle. Does not require restart.

**Rollback:**

```sql
ALTER TABLE bloated_table RESET (
  autovacuum_vacuum_threshold,
  autovacuum_vacuum_scale_factor
);
```

**Verification:** Re-run Step 7 — `dead_pct` must drop below 5% after VACUUM completes. Re-run Step 1 — `total_exec_time` for affected queries should decrease proportionally to the page reduction.

---

### Cause E: Inefficient Query Pattern (Correlated Subquery or SELECT *)

**Statement:** The query itself is structurally inefficient — using a correlated subquery executed once per outer row or selecting all columns with SELECT *, causing unnecessary I/O.

**Mechanism:** A correlated subquery references the outer query's columns and is re-executed for each row of the outer result set. With 100,000 outer rows, this produces 100,000 individual subquery executions regardless of indexing. SELECT * forces PostgreSQL to transfer all column data from storage and network even when only a few columns are needed, multiplying I/O proportional to row width and increasing shared buffer pressure.

**Indicator:**

- [Step 3] Plan shows `SubPlan` or `InitPlan` nodes with high `loops` count equal to outer row count
- [Step 2] Query has very high `rows_per_call` but the number does not match expected business logic result size

<!-- match: {"step": 3, "predicate": "contains", "target": "SubPlan"} -->

**Mitigation:**

- **Risk:** Low. Query rewrite is safe in a transaction and can be tested before deployment.
- **Command:**

  ```sql
  -- Replace correlated subquery with a JOIN
  SELECT t.id, s.value
  FROM target_table t
  JOIN subquery_table s ON s.target_id = t.id
  WHERE t.status = 'active';
  ```

- **Duration:** Immediate — no schema change required.

**Resolution:** Rewrite the query to use explicit JOINs instead of correlated subqueries, and list only required columns instead of SELECT *.

**Verification:** Re-run Step 3 on the rewritten query — `SubPlan` nodes must be absent and total estimated cost must decrease materially. Confirm Step 1 shows reduced `mean_exec_time` for the normalized query.

---

### Cause F: Generic Prepared Statement Plan Performing Poorly

**Statement:** A prepared statement compiled with a generic plan performs poorly for specific parameter values where a custom plan would choose a different access path.

**Mechanism:** PostgreSQL caches a generic execution plan for prepared statements after five executions with the same statement. The generic plan is built without knowing the actual parameter values, relying on average statistics. When parameter values have high selectivity variance — such as a column with skewed value distribution — the generic plan may use a sequential scan for a value that a custom plan would handle with an index scan. This causes individual queries with rare or common parameter values to underperform.

**Indicator:**

- [Step 3] Plan shows `Seq Scan` but adding `WHERE column = <literal>` produces an index scan in a separate EXPLAIN
- [Step 1] High `stddev_exec_time` relative to `mean_exec_time` for the same `queryid` (variable performance across executions)

<!-- match: {"step": 3, "predicate": "contains", "target": "Generic Plan"} -->

**Mitigation:**

- **Risk:** Low. Per-session setting reverts at session close. Per-role setting applies to future sessions only.
- **Command:**

  ```sql
  -- Force custom plan for current session
  SET plan_cache_mode = 'force_custom_plan';
  ```

- **Duration:** Effective immediately for the current session.

**Resolution:**

```sql
-- Force custom plans for the affected application role
ALTER ROLE app_user SET plan_cache_mode = 'force_custom_plan';
```

**Impact:** Increases planning time per query for the role; acceptable for OLTP but avoid for high-frequency batch jobs.

**Rollback:**

```sql
ALTER ROLE app_user RESET plan_cache_mode;
```

**Verification:** Re-run Step 3 using `EXPLAIN (ANALYZE, BUFFERS)` after setting `force_custom_plan` — plan must show `Index Scan` for selective parameter values. Confirm `mean_exec_time` in Step 1 is stable with lower `stddev_exec_time`.

---

### Cause Z: Unidentified Cause [Default]

**Statement:** The slow query cause could not be identified from available diagnostic data and requires deeper investigation or escalation.

**Mechanism:** Some slow query root causes require extended observability not captured by `pg_stat_statements` alone — for example, lock waits not yet visible, OS-level I/O scheduler pressure, network latency to storage, or bugs in query planner edge cases. The standard diagnostic steps above did not isolate the cause to a known pattern.

**Indicator:**

- [Default] None of the preceding causes matched the diagnostic output

**Mitigation:**

- **Risk:** Low. Enabling additional logging temporarily increases disk I/O for log writes.
- **Command:**

  ```sql
  -- Enable auto_explain to capture plans of slow queries automatically
  LOAD 'auto_explain';
  SET auto_explain.log_min_duration = 1000;  -- ms
  SET auto_explain.log_analyze = true;
  SET auto_explain.log_buffers = true;
  ```

- **Duration:** Per-session; disable when investigation is complete.

**Resolution:** Out of runbook scope. Escalate with: (1) output of Steps 1–7, (2) full `EXPLAIN (ANALYZE, BUFFERS)` plan, (3) relevant PostgreSQL log lines with timestamps, (4) OS-level I/O and CPU metrics from the database host during the slow period.

**Verification:** Escalation ticket created with full diagnostic artifacts attached.

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
