---
id: pg-slow-queries
title: "PostgreSQL Slow Query Diagnosis"
domain: database
service: postgresql
symptom_class:
  - latency
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - postgresql
  - slow-queries
  - pg-stat-statements
  - explain-analyze
  - indexes
  - query-optimization
difficulty: intermediate
---

# PostgreSQL Slow Query Diagnosis

## Problem Definition

Applies to PostgreSQL 10 and later. Requires `pg_stat_statements` extension enabled (recommended for all production deployments), superuser or `pg_monitor` role for full visibility, and the ability to run `EXPLAIN ANALYZE` on suspect queries. The `pg_stat_statements` extension must be added to `shared_preload_libraries` and requires a PostgreSQL restart if not already loaded.

Slow queries degrade application response times, increase connection hold duration (contributing to pool exhaustion), and consume disproportionate CPU, I/O, and memory resources on the database server.

Symptoms include elevated application latency on database-dependent endpoints, high CPU or I/O utilization on the PostgreSQL server, connection pool saturation as connections are held by long-running queries, and application timeout errors when queries exceed configured statement timeouts. The PostgreSQL log may show:

```text
LOG: duration: 15234.567 ms  statement: SELECT ...
```

This appears when `log_min_duration_statement` is configured (recommended for production).

Common causes include missing or unused indexes forcing sequential scans on large tables, inefficient query patterns (SELECT *, unnecessary joins, correlated subqueries), stale statistics causing the query planner to choose suboptimal execution plans, table bloat from insufficient vacuuming inflating scan costs, lock contention causing queries to wait rather than execute, and parameter sniffing with prepared statements leading to poor generic plans.

## Diagnostic Steps

### Step 1. Identify top queries by total execution time

Use `pg_stat_statements` to find the queries consuming the most cumulative time. These are the highest-impact targets for optimization.

```sql
SELECT
  queryid,
  calls,
  round(total_exec_time::numeric, 2) AS total_time_ms,
  round(mean_exec_time::numeric, 2) AS mean_time_ms,
  round(max_exec_time::numeric, 2) AS max_time_ms,
  rows,
  left(query, 120) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

Expected output: a ranked list of queries. Focus on queries with high `total_time_ms` (most cumulative impact) and high `mean_time_ms` (individually slow). Queries with millions of `calls` and moderate `mean_time_ms` may also be significant due to cumulative effect.

### Step 2. Identify queries with poor row-to-call efficiency

Find queries that scan many rows but return few, indicating missing indexes or inefficient filters.

```sql
SELECT
  queryid,
  calls,
  rows,
  round(rows::numeric / NULLIF(calls, 0), 2) AS rows_per_call,
  round(shared_blks_read::numeric / NULLIF(calls, 0), 2) AS blocks_read_per_call,
  round(mean_exec_time::numeric, 2) AS mean_time_ms,
  left(query, 120) AS query
FROM pg_stat_statements
WHERE calls > 100
ORDER BY shared_blks_read DESC
LIMIT 20;
```

Queries with high `blocks_read_per_call` but low `rows_per_call` are likely performing sequential scans and would benefit from index creation.

### Step 3. Run EXPLAIN ANALYZE on a suspect query

Obtain the actual execution plan for a slow query to understand which operations are expensive.

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT ... ; -- paste the slow query here
```

Key elements to look for in the output:

- **Seq Scan on large_table** — Sequential scan on a table with more than a few thousand rows indicates a missing index.
- **Nested Loop with high actual rows** — Nested loops over large sets without index support are extremely expensive.
- **Sort** with **external merge** — Indicates the sort spilled to disk because `work_mem` was insufficient.
- **Hash Join** with **batches > 1** — Hash join spilled to disk, also a `work_mem` issue.
- **Rows Removed by Filter** much larger than actual rows — The query reads many rows but discards most, suggesting a more selective index is needed.

### Step 4. Check for missing indexes

Identify tables where sequential scans dominate over index scans.

```sql
SELECT
  schemaname,
  relname AS table_name,
  seq_scan,
  idx_scan,
  seq_scan - idx_scan AS excess_seq_scans,
  pg_size_pretty(pg_relation_size(relid)) AS table_size
FROM pg_stat_user_tables
WHERE seq_scan > idx_scan
  AND pg_relation_size(relid) > 10 * 1024 * 1024  -- tables > 10 MB
ORDER BY excess_seq_scans DESC
LIMIT 20;
```

Tables with high `excess_seq_scans` and large `table_size` are prime candidates for index creation.

### Step 5. Check for unused indexes

Unused indexes waste disk space and slow down writes without providing read benefits.

```sql
SELECT
  schemaname,
  relname AS table_name,
  indexrelname AS index_name,
  idx_scan AS times_used,
  pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
WHERE idx_scan = 0
  AND indexrelname NOT LIKE '%pkey%'
  AND indexrelname NOT LIKE '%unique%'
ORDER BY pg_relation_size(indexrelid) DESC
LIMIT 20;
```

Indexes with `times_used = 0` since the last statistics reset can potentially be dropped. Verify the statistics reset timestamp first with `SELECT stats_reset FROM pg_stat_bgwriter;`.

### Step 6. Check table statistics freshness

Stale statistics cause the query planner to make poor decisions.

```sql
SELECT
  schemaname,
  relname AS table_name,
  last_autoanalyze,
  last_analyze,
  n_live_tup,
  n_dead_tup,
  n_mod_since_analyze
FROM pg_stat_user_tables
WHERE n_mod_since_analyze > n_live_tup * 0.1
ORDER BY n_mod_since_analyze DESC
LIMIT 20;
```

Tables where `n_mod_since_analyze` is more than 10% of `n_live_tup` have stale statistics. The planner may choose wrong join strategies or scan methods.

### Step 7. Check for table bloat

Bloated tables cause queries to read more pages than necessary, inflating I/O costs.

```sql
SELECT
  schemaname,
  relname AS table_name,
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

Tables with `dead_pct` above 20% are significantly bloated and need vacuuming.

## Mitigation

### Option 1. Create a missing index

**Risk**: Low with `CONCURRENTLY`. The index is built without blocking writes. Without `CONCURRENTLY`, the table is locked for writes during index creation.

**Command**:

```sql
-- Use CONCURRENTLY to avoid blocking writes
CREATE INDEX CONCURRENTLY idx_table_column ON table_name (column_name);
```

**Verify**:

```sql
EXPLAIN (ANALYZE) SELECT ... ; -- re-run the slow query
```

Confirm the plan now shows an Index Scan instead of a Seq Scan.

**Duration**: Index creation time depends on table size. A 10 GB table typically takes 1-5 minutes with CONCURRENTLY.

### Option 2. Update table statistics

**Risk**: Low. ANALYZE reads the table but does not lock it or modify data.

**Command**:

```sql
ANALYZE table_name;
```

**Verify**:

```sql
SELECT last_analyze, n_mod_since_analyze FROM pg_stat_user_tables WHERE relname = 'table_name';
```

**Duration**: Seconds to minutes depending on table size.

### Option 3. Increase work_mem for sort/hash-heavy queries

**Risk**: Low-Medium. Increasing `work_mem` reduces disk spills but uses more RAM per connection. Set per-session or per-query rather than globally to avoid memory pressure.

**Command**:

```sql
-- Per-session for a specific workload
SET work_mem = '256MB';
-- Then run the query

-- Or per-role for a specific application
ALTER ROLE analytics_user SET work_mem = '256MB';
```

**Verify**:

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT ... ;
```

Confirm Sort operations show "Sort Method: quicksort" (in-memory) instead of "external merge Disk".

**Duration**: Immediate for per-session changes.

### Option 4. Set statement_timeout to prevent runaway queries

**Risk**: Low. Long queries are terminated, preventing resource monopolization. Applications receive a timeout error and can retry or report the failure.

**Command**:

```sql
ALTER SYSTEM SET statement_timeout = '60s';
SELECT pg_reload_conf();

-- Or per-role
ALTER ROLE app_user SET statement_timeout = '30s';
```

**Verify**:

```sql
SHOW statement_timeout;
```

**Duration**: Immediate after reload.

## Root Cause Resolution

**If** sequential scans on large tables are the primary cause → create targeted indexes based on the WHERE clauses and JOIN conditions identified in EXPLAIN ANALYZE output. Use partial indexes when queries filter on a common condition:

```sql
-- Partial index: only indexes active orders
CREATE INDEX CONCURRENTLY idx_orders_active ON orders (created_at)
WHERE status = 'active';
```

**If** stale statistics cause poor query plans → tune autovacuum/autoanalyze to run more frequently on high-churn tables:

```sql
ALTER TABLE high_churn_table SET (
  autovacuum_analyze_threshold = 100,
  autovacuum_analyze_scale_factor = 0.02
);
```

**If** sort or hash operations spill to disk → increase `work_mem` for the affected workload. Set it per-role for analytics users rather than globally:

```sql
ALTER ROLE analytics_user SET work_mem = '512MB';
```

**If** table bloat inflates query costs → vacuum the bloated tables and tune autovacuum:

```sql
VACUUM (VERBOSE) bloated_table;
```

See the pg-vacuum-bloat runbook for detailed autovacuum tuning.

**If** correlated subqueries or SELECT * patterns are the root cause → rewrite the query. Replace correlated subqueries with JOINs or lateral joins, and replace SELECT * with explicit column lists to reduce I/O.

**If** prepared statement generic plans perform poorly → force custom plans:

```sql
-- Per-session
SET plan_cache_mode = 'force_custom_plan';
```

Or in PostgreSQL 16+, use `pg_stat_statements` to compare generic vs custom plan performance and selectively disable generic plans for problematic queries.

## Verification

After applying fixes, confirm query performance has improved.

1. Re-run EXPLAIN ANALYZE on the optimized query:

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) SELECT ... ;
```

Confirm execution time has decreased and the plan uses the new index.

1. Check pg_stat_statements for improved mean execution time:

```sql
-- Reset statistics to measure from a clean baseline
SELECT pg_stat_statements_reset();

-- Wait for representative traffic, then check
SELECT queryid, calls, round(mean_exec_time::numeric, 2) AS mean_ms,
  left(query, 100) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;
```

1. Application response times have returned to baseline. Check application APM metrics (p50, p95, p99 latency) for affected endpoints.

1. No excessive sequential scans on large tables:

```sql
SELECT relname, seq_scan, idx_scan
FROM pg_stat_user_tables
WHERE pg_relation_size(relid) > 10 * 1024 * 1024
ORDER BY seq_scan DESC
LIMIT 10;
```

1. Table statistics are fresh:

```sql
SELECT relname, last_analyze, n_mod_since_analyze
FROM pg_stat_user_tables
ORDER BY n_mod_since_analyze DESC
LIMIT 10;
```

## Prevention

1. **Enable pg_stat_statements** — Add to `shared_preload_libraries` and configure `pg_stat_statements.track = all`. This is the single most important tool for identifying slow queries in production.

2. **Set log_min_duration_statement** — Configure to 1-5 seconds to log all queries exceeding the threshold. Regularly review the slow query log.

3. **Run ANALYZE after bulk data changes** — After large INSERT, UPDATE, DELETE, or COPY operations, run `ANALYZE` on affected tables to update planner statistics.

4. **Tune autovacuum for high-churn tables** — Reduce `autovacuum_analyze_scale_factor` on tables with frequent modifications to keep statistics fresh.

5. **Review query plans during development** — Require EXPLAIN ANALYZE output in code review for new queries touching large tables.

6. **Monitor index usage** — Regularly audit `pg_stat_user_indexes` to identify unused indexes (waste write performance) and missing indexes (cause slow reads).

7. **Set statement_timeout per role** — Prevent runaway queries from monopolizing resources. Use different timeouts for OLTP (short) and analytics (longer) workloads.

8. **Use connection pooling** — PgBouncer in transaction mode prevents slow queries from exhausting the connection pool.

9. **Partition large tables** — Tables exceeding 100 GB benefit from partition pruning, which limits scans to relevant partitions only.

## Sources

- [PostgreSQL Documentation: pg_stat_statements](https://www.postgresql.org/docs/current/pgstatstatements.html) — Official reference for the query statistics tracking extension.
- [PostgreSQL Documentation: EXPLAIN](https://www.postgresql.org/docs/current/sql-explain.html) — Official reference for query execution plan analysis.
- [PostgreSQL Documentation: Index Types](https://www.postgresql.org/docs/current/indexes-types.html) — Official reference for B-tree, Hash, GiST, GIN, and BRIN index types and their use cases.
- [PostgreSQL Documentation: The Cumulative Statistics System](https://www.postgresql.org/docs/current/monitoring-stats.html) — Official reference for `pg_stat_user_tables`, `pg_stat_user_indexes`, and table access statistics.
