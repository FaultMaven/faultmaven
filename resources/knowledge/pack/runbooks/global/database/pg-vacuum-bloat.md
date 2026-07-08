---
id: "pg-vacuum-bloat"
title: "PostgreSQL Table Bloat from Autovacuum Failure"
domain: database
service: postgresql
symptom_class: [disk_full, latency]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

### Step 1: Identify tables with highest dead-tuple accumulation

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

### Step 2: Check active autovacuum workers and progress

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

### Step 3: Find transactions holding back the vacuum horizon

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

### Step 4: Check transaction ID age and wraparound proximity

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

### Step 5: Review autovacuum configuration

```sql
SELECT name, setting, unit, short_desc
FROM pg_settings
WHERE name LIKE 'autovacuum%'
ORDER BY name;
```

Expected output: Key defaults to watch — `autovacuum_vacuum_scale_factor = 0.2` (triggers only after 20% of table is dead), `autovacuum_max_workers = 3`, `autovacuum_vacuum_cost_delay = 2` (milliseconds; controls I/O throttling). Overly high scale factors or low worker counts indicate misconfiguration.

### Step 6: Estimate physical bloat by size vs live rows

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

**Statement:** Autovacuum does not trigger on high-write tables because the default 20% scale factor is never reached before bloat accumulates faster than vacuum can recover.

**Chain:**
- root: the default `autovacuum_vacuum_scale_factor` (0.2) sets the dead-tuple trigger to 20% of `n_live_tup`, far too high for high-churn tables
- s1: on a large table the trigger sits at millions of dead tuples, so autovacuum rarely or never fires between heavy write bursts
- s2: dead tuples accumulate faster than the threshold is crossed under load, so the table is never cleaned in time
- D: heap pages inflate with uncollected dead tuples, growing table size and raising scan latency (Symptom)

**Indicators:**
- root: [Step 5] `autovacuum_vacuum_scale_factor` is 0.2 (default) and `autovacuum_vacuum_threshold` is 50 (default)
- s2: [Step 1] `dead_pct` above 20% on tables with many millions of live rows

**Interventions:**
- **remediation** (root): lower the per-table scale factor so vacuum triggers early, then clear existing bloat.

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
- **mitigation** (root): immediately tighten the per-table thresholds so the next autovacuum cycle picks up the table.

  ```sql
  ALTER TABLE high_churn_table SET (
    autovacuum_vacuum_threshold = 100,
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_analyze_threshold = 100,
    autovacuum_analyze_scale_factor = 0.02
  );
  ```

  **Risk:** Lower thresholds cause more frequent autovacuums, increasing I/O load during normal operations. Monitor for I/O saturation after applying. **Duration:** Permanent (per-table storage parameter, survives restarts); applies on the next autovacuum cycle. **Verification:** Re-run Step 1; `dead_pct` declines and `last_autovacuum` refreshes.

---

### Cause B: Idle-in-Transaction Sessions Blocking the Vacuum Horizon

**Statement:** A long-running or abandoned `idle in transaction` session holds a `backend_xmin` that prevents autovacuum from removing dead tuples still visible to it.

**Chain:**
- root: a session opened a transaction and went `idle in transaction`, pinning its `backend_xmin` open
- s1: MVCC requires retaining dead tuples visible to that open transaction, so the cleanup horizon (`xmin`) cannot advance
- s2: the horizon is blocked cluster-wide, so dead tuples accumulate across all tables regardless of autovacuum tuning
- D: bloat grows on many tables at once, inflating size and scan latency (Symptom)

**Indicators:**
- root: [Step 3] one or more rows with `state = 'idle in transaction'` and `xact_duration` exceeding several minutes
- s2: [Step 1] many tables showing rising `dead_pct` simultaneously (global horizon block, not a per-table tuning issue)

**Interventions:**
- **remediation** (root): auto-terminate idle-in-transaction sessions cluster-wide to stop recurrence.

  ```sql
  -- Prevent recurrence: auto-terminate idle-in-transaction sessions
  ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';
  SELECT pg_reload_conf();
  ```

  Cluster-wide; applies to all new transactions after `pg_reload_conf()`. Existing idle sessions are terminated only if they remain idle-in-transaction past the new timeout. Rollback: `ALTER SYSTEM SET idle_in_transaction_session_timeout = '0'; SELECT pg_reload_conf();` (0 disables it). **Verification:** Re-run Step 3; no row shows `idle in transaction` above 5 minutes. Re-run Step 1 after one autovacuum cycle; `dead_pct` on previously blocked tables declines.
- **mitigation** (root): terminate the specific blocking session identified in Step 3 to release the horizon now.

  ```sql
  -- Identify PID from Step 3, then terminate:
  SELECT pg_terminate_backend(12345);
  -- Verify horizon advanced:
  SELECT min(age(backend_xmin)) FROM pg_stat_activity WHERE backend_xmin IS NOT NULL;
  ```

  **Risk:** Low-Medium. The terminated session's application receives a connection error; apps with poor reconnect logic may surface errors to users. Identify the application owner before terminating. **Duration:** Immediate — autovacuum advances `xmin` and cleans dead tuples once the blocking session exits. **Verification:** the `min(age(backend_xmin))` query returns a smaller age.

---

### Cause C: Insufficient Autovacuum Workers for Database Size

**Statement:** The default three autovacuum workers cannot keep pace with the number and churn rate of tables in the database.

**Chain:**
- root: `autovacuum_max_workers` is 3 (default), too few for a database with hundreds of moderate-to-high-churn tables
- s1: the launcher cannot service every queued table within the naptime window, and large tables monopolise workers for many minutes
- s2: smaller high-churn tables are queued but starved of a worker, so their dead tuples are never collected in time
- D: multiple tables bloat simultaneously, inflating size and scan latency (Symptom)

**Indicators:**
- root: [Step 5] `autovacuum_max_workers` equals 3 (default)
- s1: [Step 2] all three (or configured maximum) vacuum worker slots are occupied for extended periods
- s2: [Step 1] multiple tables simultaneously showing high `dead_pct` without a single obvious large-volume offender

**Interventions:**
- **remediation** (root): raise the worker count so more tables are serviced concurrently.

  ```sql
  ALTER SYSTEM SET autovacuum_max_workers = 6;
  SELECT pg_reload_conf();
  ```

  Cluster-wide; new workers start on the next launcher cycle (within `autovacuum_naptime`, default 1 minute). No restart required in PostgreSQL 14+. Rollback: `ALTER SYSTEM SET autovacuum_max_workers = 3; SELECT pg_reload_conf();` **Verification:** Re-run Step 2 over the next 5 minutes; more concurrent vacuum rows appear. Re-run Step 1 after 30 minutes; `dead_pct` declines across previously bloated tables.

---

### Cause D: Autovacuum I/O Throttling Too Aggressive

**Statement:** The autovacuum cost-delay throttle is set so high that vacuum workers cannot process dead tuples fast enough to keep up with the write workload.

**Chain:**
- root: `autovacuum_vacuum_cost_delay` (2ms+) with cost limit -1 (inheriting `vacuum_cost_limit = 200`) throttles vacuum I/O aggressively, even on fast SSD/NVMe storage
- s1: each vacuum worker pauses when its cumulative I/O cost hits the limit, capping vacuum throughput well below the write rate
- s2: autovacuum runs often on the table but never finishes before new dead tuples arrive, so dead tuples are never fully cleared
- D: bloat grows even while autovacuum appears to run regularly, inflating size and scan latency (Symptom)

**Indicators:**
- root: [Step 5] `autovacuum_vacuum_cost_delay` is 2 or higher and `autovacuum_vacuum_cost_limit` is -1
- s1: [Step 2] autovacuum worker for a table shows `heap_blks_scanned` advancing very slowly relative to `heap_blks_total`
- s2: [Step 1] `last_autovacuum` is recent (autovacuum runs often) but `dead_pct` remains high

**Interventions:**
- **remediation** (root): relax global throttling for SSD-backed instances and override per-table for the busiest tables.

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
- **mitigation** (root): lift the throttle on specific high-churn tables only, avoiding global I/O impact.

  ```sql
  -- Increase throughput on specific high-churn tables without global impact
  ALTER TABLE high_churn_table SET (
    autovacuum_vacuum_cost_delay = '0ms',
    autovacuum_vacuum_cost_limit = 800
  );
  ```

  **Risk:** Reducing cost delay increases vacuum disk I/O; on spinning-disk storage this may impact query latency. Start with modest changes. **Duration:** Permanent per-table setting; apply immediately. **Verification:** Re-run Step 2; `heap_blks_vacuumed` advances faster, and Step 1 `dead_pct` declines after 30 minutes.

---

### Cause E: Replication Slot or Hot Standby Feedback Blocking Vacuum

**Statement:** A replication slot with a lagging consumer, or a replica with `hot_standby_feedback = on`, holds back the primary's `xmin` horizon and prevents dead tuple cleanup.

**Chain:**
- root: a replication slot with a lagging consumer (or a replica reporting its oldest XID via `hot_standby_feedback`) pins a live `xmin` on the primary
- s1: the primary must retain dead tuples still needed by the replica, so its cleanup horizon cannot advance
- s2: vacuum cannot remove those tuples, and the block cannot be resolved by autovacuum tuning alone
- D: dead tuples accumulate indefinitely on the primary, inflating size and scan latency (Symptom)

**Indicators:**
- root: [Step 3] the blocking `backend_xmin` belongs to a `walsender` process (application_name matches a replica name)
- D: [Symptom] bloat grows despite healthy autovacuum configuration and no idle-in-transaction sessions

**Interventions:**
- **remediation** (root): disable replica feedback and bound how far a slot may lag before it is invalidated.

  ```sql
  -- On the replica postgresql.conf, disable feedback to stop blocking primary vacuum:
  -- hot_standby_feedback = off
  -- Reload replica config:
  -- SELECT pg_reload_conf();  -- run on the replica

  -- On the primary, limit how long a slot can lag before autovacuum overrides it:
  ALTER SYSTEM SET max_slot_wal_keep_size = '10GB';
  SELECT pg_reload_conf();
  ```

  `max_slot_wal_keep_size` causes PostgreSQL to invalidate a slot that falls behind the limit; the subscriber must re-sync. `hot_standby_feedback = off` may cause query cancellations on the replica when vacuum runs. Rollback: `ALTER SYSTEM SET max_slot_wal_keep_size = -1; SELECT pg_reload_conf();` (disables the limit). **Verification:** Re-run Step 3; the walsender `backend_xmin` advances. Re-run Step 1 after one autovacuum cycle; previously blocked tables show decreasing `dead_pct`.
- **mitigation** (root): drop an inactive/lagging slot to release the horizon immediately.

  ```sql
  -- List slots and their lag:
  SELECT slot_name, active, age(xmin) AS xmin_age, age(catalog_xmin) AS catalog_xmin_age
  FROM pg_replication_slots
  ORDER BY age(xmin) DESC;
  -- Drop an inactive/lagging slot (irreversible):
  SELECT pg_drop_replication_slot('slot_name');
  ```

  **Risk:** Dropping a replication slot disconnects the subscriber; it must re-sync from scratch. Do not drop production slots without coordinating with the downstream consumer. **Duration:** Immediate once the slot is dropped or the replica reconnects. **Verification:** Re-run Step 3; the walsender `backend_xmin` advances.

---

### Cause F: Transaction ID Wraparound Emergency

**Statement:** The database has consumed more than 95% of available transaction IDs and PostgreSQL is refusing write transactions to force an emergency anti-wraparound vacuum.

**Chain:**
- root: `age(datfrozenxid)` has approached the ~2-billion XID limit because anti-wraparound vacuum was blocked or never completed
- s1: a forced anti-wraparound vacuum fires near `autovacuum_freeze_max_age` but is blocked by long-running transactions or starved by high workload
- s2: XID consumption crosses ~95% and the cluster enters safe mode — only the emergency vacuum may run
- D: write transactions are refused database-wide (`ERROR: database is not accepting commands that assign new XIDs`) — a full outage (Symptom)

**Indicators:**
- root: [Step 4] `pct_to_wraparound` above 90% for any database
- D: [Symptom] PostgreSQL log shows `ERROR: database is not accepting commands that assign new XIDs`

**Interventions:**
- **remediation** (root): after the emergency vacuum, tune freeze parameters and alerting to prevent recurrence.

  ```sql
  -- After emergency vacuum, tune freeze parameters to prevent recurrence:
  ALTER SYSTEM SET autovacuum_freeze_max_age = 150000000;
  SELECT pg_reload_conf();
  -- Alert on pg_database age(datfrozenxid) > 100 million (50% of new max).
  ```

  **Verification:** Re-run Step 4. `pct_to_wraparound` should fall below 50%. Write transactions resume once the emergency vacuum completes and `age(datfrozenxid)` drops below the critical threshold.
- **mitigation** (s2): clear blocking sessions and run the emergency anti-wraparound vacuum to exit safe mode.

  ```sql
  -- Terminate any idle-in-transaction sessions blocking vacuum:
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE state = 'idle in transaction'
    AND now() - xact_start > interval '5 minutes';
  -- Then run the emergency anti-wraparound vacuum (cannot be interrupted):
  VACUUM FREEZE;
  ```

  **Risk:** VACUUM FREEZE is I/O-intensive and runs to completion without interruption; it may degrade query performance. Do NOT use VACUUM FULL during wraparound recovery — it consumes additional XIDs. Coordinate with stakeholders before running on large databases. **Duration:** Minutes to hours depending on database size; monitor progress with Step 2. **Verification:** Re-run Step 4; `pct_to_wraparound` falls and writes resume.

---

### Cause Z: Unidentified Bloat Source

**Statement:** Table bloat is growing but none of the above causes match the observed diagnostic signals.

**Chain:**
- root: an uncommon mechanism (system-catalog bloat from DDL-heavy workloads, partial-index bloat, or extension-managed child tables hidden from `pg_stat_user_tables`) is driving growth
- D: bloat continues despite healthy autovacuum and no blocking sessions (Symptom)

**Indicators:**
- root: [Default] all standard causes ruled out; bloat continues despite healthy autovacuum and no blocking sessions

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot — catalog bloat and partitioned/child tables — then escalate to the database engineering SME.

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

  **Risk:** Investigation only — no risk. **Duration:** Investigation only; escalate findings to database engineering with PostgreSQL version, workload type, and autovacuum log excerpts. **Verification:** Escalation ticket opened with complete diagnostic output; monitor `n_dead_tup` trend in Step 1 for stabilisation after escalation actions.

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
