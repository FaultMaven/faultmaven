---
id: "pg-vacuum-bloat"
title: "PostgreSQL Table Bloat from Autovacuum Failure"
domain: database
service: postgresql
symptom_class: [disk_full, latency]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [autovacuum, vacuum, dead-tuples, bloat, mvcc, wraparound, pg-repack]
difficulty: intermediate
---

## Symptom Recognition

- Tables grow continuously despite stable or declining row counts; `pg_total_relation_size()` increases while `n_live_tup` does not
- `n_dead_tup` in `pg_stat_user_tables` exceeds 20% of total tuples; `last_autovacuum` is NULL or more than 30 minutes old for high-churn tables
- Sequential scan latency on large tables rises as dead tuples inflate heap pages
- Disk usage alerts fire without a corresponding write workload spike
- PostgreSQL log emits wraparound warnings:

  ```text
  WARNING: database "mydb" must be vacuumed within 39985967 transactions
  HINT: To avoid XID assignment failures, execute a database-wide VACUUM in that database.
  ```

- At critical threshold: `ERROR: database is not accepting commands that assign new XIDs to avoid wraparound data loss in database "mydb"`

## Applicability

- PostgreSQL 10 and later (all editions including AWS RDS, Google Cloud SQL, Azure Database for PostgreSQL)
- Requires `pg_monitor` role or superuser for `pg_stat_user_tables`, `pg_stat_progress_vacuum`, and `pg_stat_activity`
- `ALTER SYSTEM` or access to `postgresql.conf` required for persistent autovacuum tuning
- `pg_repack` extension must be pre-installed for online compaction (optional step)

## Diagnostic Steps

### Step 1:

Identify tables with the highest dead tuple accumulation.

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
WHERE n_dead_tup > 1000
ORDER BY n_dead_tup DESC
LIMIT 20;
```

Expected output: `dead_pct` below 10% for healthy tables. Values above 20% indicate significant bloat. NULL `last_autovacuum` means autovacuum has never processed the table.

### Step 2:

Check whether autovacuum workers are currently running and their progress.

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

Expected output: One row per active vacuum. Empty result means no vacuum is running. A worker stuck in the same phase for many minutes suggests a blocking transaction.

### Step 3:

Find transactions holding back the vacuum horizon (`xmin`).

```sql
SELECT
  pid,
  usename,
  application_name,
  state,
  backend_xmin,
  now() - xact_start AS xact_duration,
  left(query, 120) AS query
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY age(backend_xmin) DESC
LIMIT 10;
```

Expected output: The row with the oldest `backend_xmin` is blocking dead-tuple cleanup for all tables. `state = 'idle in transaction'` is the most common culprit.

### Step 4:

Check transaction ID age and proximity to wraparound.

```sql
SELECT
  datname,
  age(datfrozenxid) AS xid_age,
  current_setting('autovacuum_freeze_max_age')::bigint AS freeze_max_age,
  round(100.0 * age(datfrozenxid)
        / current_setting('autovacuum_freeze_max_age')::bigint, 1) AS pct_to_wraparound
FROM pg_database
ORDER BY xid_age DESC;
```

Expected output: `pct_to_wraparound` below 50% is healthy. Above 75% is high risk. Above 95% PostgreSQL will refuse write transactions until an emergency anti-wraparound vacuum completes.

### Step 5:

Review current autovacuum configuration for overly conservative settings.

```sql
SELECT name, setting, unit, short_desc
FROM pg_settings
WHERE name LIKE 'autovacuum%'
ORDER BY name;
```

Expected output: Key defaults to watch — `autovacuum_vacuum_scale_factor = 0.2` (triggers only after 20% of table is dead), `autovacuum_max_workers = 3`, `autovacuum_vacuum_cost_delay = 2` (milliseconds; controls I/O throttling). Overly high scale factors or low worker counts indicate misconfiguration.

### Step 6:

Estimate physical table bloat by comparing actual size to live-row count.

```sql
SELECT
  schemaname,
  relname AS table_name,
  pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
  pg_size_pretty(pg_relation_size(relid)) AS heap_size,
  pg_size_pretty(pg_indexes_size(relid)) AS index_size,
  n_live_tup,
  n_dead_tup
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 20;
```

Expected output: Cross-reference total size against `n_live_tup`. A table with 500k live rows consuming 10 GB is bloated; the same table at 200 MB is healthy. High `index_size` relative to `heap_size` often indicates index bloat from dead tuple references.

## Causes

### Cause A: Autovacuum Thresholds Too Conservative for High-Churn Tables

**Statement:** Autovacuum does not trigger on high-write tables because the default scale factor (20% dead tuples) is never reached before the table is vacuumed by a full-table scan.

**Mechanism:** Autovacuum fires when dead tuples exceed `autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor × n_live_tup`. On a 50-million-row table the default threshold is 10 million dead tuples before any vacuum runs, allowing enormous bloat to accumulate between runs. Tables receiving millions of UPDATE/DELETE operations per day can generate dead tuples faster than the threshold is ever crossed under load, so autovacuum never catches up.

**Indicator:**

- [Step 1] `dead_pct` above 20% on tables with many millions of live rows
- [Step 5] `autovacuum_vacuum_scale_factor` is 0.2 (default) and `autovacuum_vacuum_threshold` is 50 (default)

<!-- match: {"step": 5, "predicate": "contains", "target": "autovacuum_vacuum_scale_factor"} -->
<!-- match: {"step": 1, "predicate": "threshold", "target": "dead_pct", "op": ">", "value": 20} -->

**Mitigation:**

- **Risk:** None — lowering thresholds causes more frequent autovacuums, which increases I/O load during normal operations. Monitor for I/O saturation after applying.

- **Command:**

  ```sql
  ALTER TABLE high_churn_table SET (
    autovacuum_vacuum_threshold = 100,
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_analyze_threshold = 100,
    autovacuum_analyze_scale_factor = 0.02
  );
  ```

- **Duration:** Permanent (per-table storage parameter, survives restarts). Apply immediately; the next autovacuum cycle picks up the new threshold.

**Resolution:**

```sql
-- Per-table tuning for high-churn tables (preferred: no restart required)
ALTER TABLE high_churn_table SET (
  autovacuum_vacuum_threshold = 100,
  autovacuum_vacuum_scale_factor = 0.02,
  autovacuum_analyze_threshold = 100,
  autovacuum_analyze_scale_factor = 0.02
);
-- Run initial manual vacuum to clear existing bloat
VACUUM (VERBOSE, ANALYZE) high_churn_table;
```

**Verification:** Re-run Step 1 within 30 minutes. `dead_pct` for the tuned table should drop below 10% and `last_autovacuum` should refresh more frequently.

---

### Cause B: Idle-in-Transaction Sessions Blocking the Vacuum Horizon

**Statement:** Long-running or abandoned `idle in transaction` sessions hold a `backend_xmin` that prevents autovacuum from removing dead tuples visible to that transaction.

**Mechanism:** PostgreSQL's MVCC model requires retaining dead tuples that are still visible to any open transaction. An `idle in transaction` session that opened a transaction hours ago prevents vacuum from advancing the cleanup horizon (`xmin`) for all tables, causing system-wide dead tuple accumulation regardless of how aggressively autovacuum is tuned. Even a single idle session can cause unbounded bloat growth.

**Indicator:**

- [Step 3] One or more rows with `state = 'idle in transaction'` and `xact_duration` exceeding several minutes
- [Step 1] Many tables showing rising `dead_pct` simultaneously (global horizon block, not per-table tuning issue)

<!-- match: {"step": 3, "predicate": "contains", "target": "idle in transaction"} -->

**Mitigation:**

- **Risk:** Low-Medium. The terminated session's application receives a connection error. Applications with poor reconnect logic may surface errors to end users. Identify the application owner before terminating.

- **Command:**

  ```sql
  -- Identify PID from Step 3, then terminate:
  SELECT pg_terminate_backend(12345);
  -- Verify horizon advanced:
  SELECT min(age(backend_xmin)) FROM pg_stat_activity WHERE backend_xmin IS NOT NULL;
  ```

- **Duration:** Immediate. Autovacuum can advance `xmin` and clean dead tuples once the blocking session exits.

**Resolution:**

```sql
-- Prevent recurrence: auto-terminate idle-in-transaction sessions
ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';
SELECT pg_reload_conf();
```

- **Impact:** Cluster-wide; applies to all new transactions after `pg_reload_conf()`. Existing idle sessions are not immediately terminated — they are terminated only if they remain idle-in-transaction past the new timeout.
- **Rollback:** `ALTER SYSTEM SET idle_in_transaction_session_timeout = '0'; SELECT pg_reload_conf();` (0 disables the timeout).

**Verification:** Re-run Step 3. No rows should show `state = 'idle in transaction'` with `xact_duration` above 5 minutes. Re-run Step 1 after one autovacuum cycle; `dead_pct` on previously blocked tables should decline.

---

### Cause C: Insufficient Autovacuum Workers for Database Size

**Statement:** The default three autovacuum workers are insufficient to keep pace with the number and churn rate of tables in the database.

**Mechanism:** PostgreSQL's autovacuum launcher distributes work across at most `autovacuum_max_workers` concurrent processes. When a database has hundreds of tables with moderate-to-high churn, the three default workers cannot service all tables within the naptime window. Tables that are queued but not yet serviced accumulate dead tuples, and large tables that take many minutes to vacuum can monopolise all workers, starving smaller high-churn tables.

**Indicator:**

- [Step 2] All three (or configured maximum) vacuum worker slots are occupied for extended periods
- [Step 1] Multiple tables simultaneously showing high `dead_pct` without a single obvious large-volume offender
- [Step 5] `autovacuum_max_workers` equals 3 (default)

<!-- match: {"step": 5, "predicate": "contains", "target": "autovacuum_max_workers"} -->
<!-- match: {"step": 2, "predicate": "threshold", "target": "active_workers", "op": ">=", "value": 3} -->

**Mitigation:**

- **Risk:** Each additional worker consumes memory and I/O. On I/O-constrained systems, increasing workers can degrade query performance. Monitor `iostat` after applying.

- **Command:**

  ```sql
  -- Takes effect on next autovacuum launcher cycle (no restart)
  ALTER SYSTEM SET autovacuum_max_workers = 6;
  SELECT pg_reload_conf();
  ```

- **Duration:** Persistent. Requires no restart in PostgreSQL 14+.

**Resolution:**

```sql
ALTER SYSTEM SET autovacuum_max_workers = 6;
SELECT pg_reload_conf();
```

- **Impact:** Cluster-wide. New workers start on the next launcher cycle (within `autovacuum_naptime`, default 1 minute).
- **Rollback:** `ALTER SYSTEM SET autovacuum_max_workers = 3; SELECT pg_reload_conf();`

**Verification:** Re-run Step 2 over the next 5 minutes. More concurrent vacuum rows should appear. Re-run Step 1 after 30 minutes to confirm `dead_pct` is declining across previously bloated tables.

---

### Cause D: Autovacuum I/O Throttling Too Aggressive

**Statement:** The autovacuum cost-delay throttle is set so high that vacuum workers cannot process dead tuples fast enough to keep up with the write workload.

**Mechanism:** PostgreSQL's cost-based vacuum delay pauses autovacuum workers when their cumulative I/O cost reaches `autovacuum_vacuum_cost_limit`, preventing vacuum from monopolising disk I/O. On modern NVMe/SSD storage the default 2 ms delay and cost limit of -1 (inheriting `vacuum_cost_limit = 200`) is still conservative enough that vacuum throughput can lag far behind high-write workloads, leading to growing bloat even when autovacuum appears to be running on the table regularly.

**Indicator:**

- [Step 2] Autovacuum worker for a table shows `heap_blks_scanned` advancing very slowly relative to `heap_blks_total`
- [Step 1] `last_autovacuum` is recent (autovacuum runs often) but `dead_pct` remains high (vacuum doesn't finish before new dead tuples accumulate)
- [Step 5] `autovacuum_vacuum_cost_delay` is 2 or higher and `autovacuum_vacuum_cost_limit` is -1

<!-- match: {"step": 5, "predicate": "threshold", "target": "autovacuum_vacuum_cost_delay", "op": ">=", "value": 2} -->

**Mitigation:**

- **Risk:** Reducing cost delay increases disk I/O consumption by vacuum. Monitor disk utilisation; on spinning-disk storage this may impact query latency. Start with modest changes.

- **Command:**

  ```sql
  -- Increase throughput on specific high-churn tables without global impact
  ALTER TABLE high_churn_table SET (
    autovacuum_vacuum_cost_delay = '0ms',
    autovacuum_vacuum_cost_limit = 800
  );
  ```

- **Duration:** Permanent per-table setting. Apply immediately.

**Resolution:**

```sql
-- Global tuning (suitable for SSD-backed instances):
ALTER SYSTEM SET autovacuum_vacuum_cost_delay = '2ms';
ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 400;
SELECT pg_reload_conf();
-- Per-table override for the most critical tables:
ALTER TABLE high_churn_table SET (
  autovacuum_vacuum_cost_delay = '0ms',
  autovacuum_vacuum_cost_limit = 1000
);
```

**Verification:** Re-run Step 2 during the next autovacuum cycle. `heap_blks_vacuumed` should advance noticeably faster. After 30 minutes re-run Step 1; `dead_pct` should be declining.

---

### Cause E: Replication Slot or Hot Standby Feedback Blocking Vacuum

**Statement:** A replication slot with a lagging consumer, or a replica with `hot_standby_feedback = on`, is holding back the primary's `xmin` horizon and preventing dead tuple cleanup.

**Mechanism:** PostgreSQL replication slots retain WAL and prevent the primary from advancing the cleanup horizon until the subscriber consumes up to that point. Separately, `hot_standby_feedback` on a replica causes the replica to report its oldest open transaction XID back to the primary, which the primary treats as a live `xmin`. Both mechanisms prevent vacuum from cleaning tuples still needed by the replica, causing indefinite dead tuple accumulation on the primary that cannot be resolved by autovacuum tuning alone.

**Indicator:**

- [Step 3] The blocking `backend_xmin` belongs to a `walsender` process (application_name matches a replica name)
- [Symptom] Bloat grows despite healthy autovacuum configuration and no idle-in-transaction sessions

**Mitigation:**

- **Risk:** Dropping a replication slot disconnects the subscriber; the subscriber must re-sync from scratch. Do not drop production slots without coordinating with the downstream consumer.

- **Command:**

  ```sql
  -- List slots and their lag:
  SELECT slot_name, active, age(xmin) AS xmin_age, age(catalog_xmin) AS catalog_xmin_age
  FROM pg_replication_slots
  ORDER BY age(xmin) DESC;
  -- Drop an inactive/lagging slot (irreversible):
  SELECT pg_drop_replication_slot('slot_name');
  ```

- **Duration:** Immediate once the slot is dropped or the replica reconnects.

**Resolution:**

```sql
-- On the replica postgresql.conf, disable feedback to stop blocking primary vacuum:
-- hot_standby_feedback = off
-- Reload replica config:
-- SELECT pg_reload_conf();  -- run on the replica

-- On the primary, limit how long a slot can lag before autovacuum overrides it:
ALTER SYSTEM SET max_slot_wal_keep_size = '10GB';
SELECT pg_reload_conf();
```

- **Impact:** `max_slot_wal_keep_size` causes PostgreSQL to invalidate a slot that has fallen behind the limit; the subscriber must re-sync. Set to a value reflecting acceptable re-sync cost. `hot_standby_feedback = off` on the replica may cause query cancellations on the replica when vacuum runs.
- **Rollback:** `ALTER SYSTEM SET max_slot_wal_keep_size = -1; SELECT pg_reload_conf();` (disables the limit).

**Verification:** Re-run Step 3. The walsender `backend_xmin` should advance as the slot or feedback is resolved. Re-run Step 1 after one autovacuum cycle; previously blocked tables should show decreasing `dead_pct`.

---

### Cause F: Transaction ID Wraparound Emergency

**Statement:** The database has consumed more than 95% of available transaction IDs and PostgreSQL is refusing write transactions to force an emergency anti-wraparound vacuum.

**Mechanism:** PostgreSQL uses 32-bit transaction IDs with a usable range of approximately 2 billion before wraparound makes old data appear to be in the future. When `age(datfrozenxid)` approaches `autovacuum_freeze_max_age` (default 200 million), autovacuum fires a forced anti-wraparound vacuum. If that is blocked by long-running transactions or a high workload prevents it from completing, the system eventually enters safe mode: write transactions are refused and only the emergency vacuum can run. This is a database-wide outage condition.

**Indicator:**

- [Step 4] `pct_to_wraparound` above 90% for any database
- [Symptom] PostgreSQL log shows `ERROR: database is not accepting commands that assign new XIDs`

<!-- match: {"step": 4, "predicate": "threshold", "target": "pct_to_wraparound", "op": ">", "value": 90} -->

**Mitigation:**

- **Risk:** VACUUM FREEZE is I/O-intensive and runs to completion without interruption. It will consume significant I/O and may degrade query performance. Do not use VACUUM FULL during wraparound recovery — it consumes additional XIDs. Coordinate with stakeholders before running on large databases.

- **Command:**

  ```sql
  -- Terminate any idle-in-transaction sessions blocking vacuum:
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE state = 'idle in transaction'
    AND now() - xact_start > interval '5 minutes';
  -- Then run the emergency anti-wraparound vacuum (cannot be interrupted):
  VACUUM FREEZE;
  ```

- **Duration:** Minutes to hours depending on database size. Monitor progress with Step 2.

**Resolution:**

```sql
-- After emergency vacuum, tune freeze parameters to prevent recurrence:
ALTER SYSTEM SET autovacuum_freeze_max_age = 150000000;
SELECT pg_reload_conf();
-- Alert on pg_database age(datfrozenxid) > 100 million (50% of new max).
```

**Verification:** Re-run Step 4. `pct_to_wraparound` should fall below 50%. Write transactions should resume once the emergency vacuum completes and `age(datfrozenxid)` drops below the critical threshold.

---

### Cause Z: Unidentified Bloat Source

**Statement:** Table bloat is growing but none of the above causes match the observed diagnostic signals.

**Mechanism:** Less common causes include: DDL-heavy workloads that generate system-catalog bloat, partial indexes with high bloat that is not reflected in `pg_stat_user_tables`, or extension-managed tables (TimescaleDB chunks, partitioned tables) where autovacuum operates on child tables not visible in the top-level query.

**Indicator:**

- [Default] All standard causes ruled out; bloat continues despite healthy autovacuum and no blocking sessions.

**Mitigation:**

- **Risk:** Investigation only — no risk.

- **Command:**

  ```sql
  -- Check system catalog bloat:
  SELECT relname, n_dead_tup, pg_size_pretty(pg_total_relation_size(oid))
  FROM pg_class
  WHERE n_dead_tup > 10000
  ORDER BY n_dead_tup DESC
  LIMIT 20;
  -- List all tables including partitions:
  SELECT parent.relname AS parent, child.relname AS child,
         s.n_dead_tup, s.last_autovacuum
  FROM pg_inherits
  JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
  JOIN pg_class child  ON pg_inherits.inhrelid  = child.oid
  LEFT JOIN pg_stat_user_tables s ON s.relid = child.oid
  ORDER BY s.n_dead_tup DESC NULLS LAST
  LIMIT 20;
  ```

- **Duration:** Investigation only. Escalate findings to database engineering.

**Resolution:** Out of runbook scope. Collect output of all diagnostic steps and escalate to database engineering with PostgreSQL version, workload type, and autovacuum log excerpts.

**Verification:** Escalation ticket opened with complete diagnostic output. Monitor `n_dead_tup` trend in Step 1 for stabilisation after escalation actions.

## Prevention

1. **Tune autovacuum per high-churn table** — Apply `autovacuum_vacuum_scale_factor = 0.02` and `autovacuum_analyze_scale_factor = 0.02` to any table receiving more than 100k updates or deletes per day. Do not rely on the global default of 0.2 for high-write tables.

2. **Set `idle_in_transaction_session_timeout`** — Configure to `5min` globally. Prevents abandoned application sessions from holding back the vacuum horizon indefinitely.

3. **Alert on dead tuple ratio** — Fire a warning alert when any `pg_stat_user_tables` row shows `n_dead_tup / (n_live_tup + n_dead_tup) > 0.15` (15%). Page at 25%.

4. **Alert on transaction ID age** — Fire a warning when `age(datfrozenxid)` exceeds 100 million transactions (~50% of the default `autovacuum_freeze_max_age`). Page when it exceeds 150 million.

5. **Increase `autovacuum_max_workers`** — For databases with more than 50 active tables, raise to 5–8. Default of 3 cannot service large databases with concurrent high-churn tables.

6. **Tune cost-based delay on SSD-backed instances** — Set `autovacuum_vacuum_cost_delay = '2ms'` and `autovacuum_vacuum_cost_limit = 400` globally; set `autovacuum_vacuum_cost_delay = '0ms'` per-table for the busiest tables.

7. **Monitor replication slot lag** — Alert when any `pg_replication_slots.confirmed_flush_lsn` falls more than 1 GB behind `pg_current_wal_lsn()`. Set `max_slot_wal_keep_size` to prevent unbounded lag from blocking primary vacuum.

8. **Schedule periodic `pg_repack`** — Run monthly on tables that accumulate index bloat despite regular vacuuming; it rebuilds tables and indexes online without an `AccessExclusiveLock`.

9. **Enable `log_autovacuum_min_duration = 0`** in staging and during incident investigation — logs every autovacuum run with duration, pages scanned, and dead tuples reclaimed.

## Sources

- [PostgreSQL Documentation: Routine Vacuuming](https://www.postgresql.org/docs/current/routine-vacuuming.html) — Priority 1. VACUUM mechanics, MVCC dead tuple retention, transaction ID wraparound thresholds (150M/200M), emergency behavior, VACUUM FREEZE semantics, and autovacuum anti-wraparound trigger. Primary reference for Causes A, D, F.
- [PostgreSQL Documentation: Autovacuum Configuration](https://www.postgresql.org/docs/current/runtime-config-autovacuum.html) — Priority 1. All autovacuum GUC parameters with exact defaults: `autovacuum_max_workers=3`, `autovacuum_vacuum_scale_factor=0.2`, `autovacuum_vacuum_cost_delay=2ms`, `autovacuum_freeze_max_age=200M`. Used in Step 5 and Causes A, C, D.
- [PostgreSQL Documentation: Monitoring Statistics](https://www.postgresql.org/docs/current/monitoring-stats.html) — Priority 1. `pg_stat_user_tables` column definitions (`n_dead_tup`, `n_live_tup`, `last_autovacuum`, `autovacuum_count`) and `pg_stat_progress_vacuum` columns (`heap_blks_scanned`, `heap_blks_vacuumed`, `num_dead_tuples`). Used in Steps 1 and 2.
- [pg_repack Documentation](https://reorg.github.io/pg_repack/) — Priority 2. Online table and index compaction without `AccessExclusiveLock`. Referenced in Prevention and Cause Z mitigation.
