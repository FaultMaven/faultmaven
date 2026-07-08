---
id: "rds-storage-full"
title: "Amazon RDS instance reaches allocated storage (storage-full)"
domain: database
service: aws-rds
symptom_class: [disk_full, service_unavailable]
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [storage-full, free-storage-space, rds-event-0089, binlog, wal, ibtmp1, autoscaling]
difficulty: advanced
---

## Symptom Recognition

- DB instance status in the RDS console / API shows `storage-full`; the instance is unreachable and cannot be restarted.
- RDS event notifications:
  - `RDS-EVENT-0007` — "The free storage capacity for DB instance: <name> is low ..." (low storage warning).
  - `RDS-EVENT-0089` — "The free storage capacity for DB instance <name> is low at X% of the provisioned storage ..." (storage low / approaching full).
- CloudWatch `AWS/RDS` metric `FreeStorageSpace` (bytes) drops toward 0; instance using > 90% of allocated storage.
- Application errors from the engine when tablespaces can no longer extend:
  - MySQL/MariaDB: `ERROR 1114 (HY000): The table '<x>' is full` and `OS errno 28 - No space left on device`.
  - PostgreSQL: `ERROR: could not extend file ...: No space left on device` (DiskFull / `53100`).

## Applicability

- Engines: RDS for MySQL, MariaDB, PostgreSQL (Single-AZ or Multi-AZ DB instance). Not Aurora (decoupled storage).
- Access required:
  - IAM: `rds:DescribeDBInstances`, `rds:ModifyDBInstance`, `rds:DescribeEvents`, `cloudwatch:GetMetricStatistics`.
  - A DB master/admin login for the in-engine SQL diagnostics and the `mysql.rds_*` stored procedures.
- Tools: AWS CLI v2 configured for the instance's region; a `mysql` or `psql` client with network reachability to the instance (note: when fully `storage-full`, you cannot connect — scale storage first, then diagnose).

## Diagnostic Steps

### Step 1: Confirm storage state and allocated vs. autoscale ceiling

```bash
aws rds describe-db-instances --db-instance-identifier <db-id> \
  --query 'DBInstances[0].{Status:DBInstanceStatus,Allocated:AllocatedStorage,MaxAlloc:MaxAllocatedStorage,Type:StorageType,Engine:Engine}' \
  --output table
```

Expected output: a row showing `Status` (e.g. `storage-full` or `available`), `Allocated` (GiB), and `MaxAlloc` (the autoscaling ceiling, or empty/null if autoscaling is off).

### Step 2: Read the free-storage trend from CloudWatch

```bash
aws cloudwatch get-metric-statistics --namespace AWS/RDS \
  --metric-name FreeStorageSpace \
  --dimensions Name=DBInstanceIdentifier,Value=<db-id> \
  --start-time "$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 --statistics Minimum --output table
```

Expected output: `Minimum` `FreeStorageSpace` values in bytes over the last 6h; a steep monotonic decline toward 0 indicates active growth (logs/temp), a flat-near-0 line indicates a steady-state overflow.

### Step 3: Check recent RDS storage events

```bash
aws rds describe-events --source-identifier <db-id> --source-type db-instance \
  --duration 1440 --query 'Events[].{Time:Date,Msg:Message}' --output table
```

Expected output: event rows; look for `low storage` / `storage-full` messages and the matching `RDS-EVENT-0007` / `RDS-EVENT-0089` notifications.

### Step 4: Measure binlog, table, and temp-tablespace consumption (MySQL/MariaDB)

```sql
-- binary logs and their sizes
SHOW BINARY LOGS;
-- largest tables (GiB)
SELECT table_schema, table_name,
       ROUND((data_length+index_length)/1024/1024/1024, 2) AS size_gb
  FROM information_schema.tables
 ORDER BY (data_length+index_length) DESC LIMIT 10;
-- global temp tablespace (ibtmp1) size
SELECT file_name, total_extents, extent_size,
       ROUND(total_extents*extent_size/1024/1024/1024, 2) AS size_gb
  FROM information_schema.files
 WHERE file_name LIKE '%ibtmp%';
```

Expected output: a binary-log list (many large `mysql-bin.NNNNNN` files signal binlog growth), the top tables by size, and the `ibtmp1` file size (large value signals temp-table bloat).

### Step 5: Measure WAL retention, replication slots, and temp usage (PostgreSQL)

```sql
-- inactive slots pinning WAL
SELECT slot_name, active,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_wal
  FROM pg_replication_slots ORDER BY 2;
-- temp file churn per database
SELECT datname, temp_files, pg_size_pretty(temp_bytes) AS temp_bytes
  FROM pg_stat_database WHERE temp_bytes > 0 ORDER BY temp_bytes DESC;
-- largest relations
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS sz
  FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;
```

Expected output: any slot with `active = f` and large `retained_wal` is pinning WAL; high `temp_bytes` signals spill-to-disk queries; the relation list flags bloated tables.

## Causes

### Cause A: Storage autoscaling is off (or its max ceiling was reached) while data grew
**Statement:** The instance has no headroom because `MaxAllocatedStorage` is unset (autoscaling off) or equal to `AllocatedStorage`, so normal data growth consumes all provisioned storage.
**Chain:**
- root: autoscaling disabled or capped at the current allocated size
- s1: organic data growth has no automatic storage expansion
- s2: free storage decays to ~0 and tablespaces cannot extend
- D: instance enters storage-full (Symptom Recognition)
**Indicators:**
- root: [Step 1] `MaxAlloc` is empty/null or equal to `Allocated`
- s1: [Step 2] `FreeStorageSpace` declines steadily over hours without a step-up
- s2: [Symptom] `RDS-EVENT-0089` low-storage notification fired
**Interventions:**
- **remediation** (root): enable autoscaling by setting a max ceiling above allocated storage (autoscaling turns on when `--max-allocated-storage` > `--allocated-storage`).

  ```bash
  aws rds modify-db-instance --db-instance-identifier <db-id> \
    --max-allocated-storage 500 --apply-immediately
  ```

  **Verification:** re-run Step 1; `MaxAlloc` now exceeds `Allocated`. RDS scales by the greatest of 10 GiB, 10% of allocated, or predicted 7h growth when free space is low.
- **mitigation** (s2): immediately raise allocated storage by at least 10% to exit storage-full and restore connectivity.

  ```bash
  aws rds modify-db-instance --db-instance-identifier <db-id> \
    --allocated-storage <current+10pct> --apply-immediately
  ```

  **Risk:** after a storage increase you cannot modify storage again for 6 hours or until storage optimization completes (whichever is longer). **Duration:** until root growth is addressed. **Verification:** re-run Step 1; `Status` returns to `available`.

### Cause B: MySQL/MariaDB binary logs are retained and accumulating
**Statement:** `binlog retention hours` is set to a non-trivial value (up to the 168h / 7-day max) so RDS keeps many binary logs on the volume, and they grow faster than they expire.
**Chain:**
- root: binlog retention configured to keep logs (e.g. 168 hours)
- s1: `mysql-bin.NNNNNN` files accumulate and are not purged
- s2: binlogs consume a large share of allocated storage
- D: instance reaches storage-full (Symptom Recognition)
**Indicators:**
- root: [Step 4] `SHOW BINARY LOGS` returns many large files spanning a long time window
- s1: [Step 2] `FreeStorageSpace` steps down in regular increments tracking log rotation
**Interventions:**
- **remediation** (root): reduce the retention window (or set NULL to purge as soon as possible) via the RDS stored procedure.

  ```sql
  CALL mysql.rds_set_configuration('binlog retention hours', 24);
  CALL mysql.rds_show_configuration;
  ```

  **Verification:** `mysql.rds_show_configuration` shows the new retention value; re-run `SHOW BINARY LOGS` (Step 4) after the next rotation — old files are gone.
- **mitigation** (s1): purge binary logs older than a cutoff to reclaim space now (only if no external replica still needs them).

  ```sql
  PURGE BINARY LOGS BEFORE NOW() - INTERVAL 1 DAY;
  ```

  **Risk:** purging logs an external (non-RDS) replica still needs breaks that replication. **Duration:** one-off; recurs until retention is lowered. **Verification:** `SHOW BINARY LOGS` lists fewer files; `FreeStorageSpace` (Step 2) rises.

### Cause C: MySQL temp-table bloat in the global temporary tablespace (ibtmp1)
**Statement:** Large or sorted queries spilled internal temporary tables to disk, permanently growing the shared `ibtmp1` tablespace, which does not shrink while the engine runs.
**Chain:**
- root: heavy sort/group/join queries create large on-disk internal temp tables
- s1: the `ibtmp1` global temp tablespace grows and never auto-shrinks
- s2: `ibtmp1` occupies a large fixed share of allocated storage
- D: free storage exhausted → storage-full (Symptom Recognition)
**Indicators:**
- root: [Step 2] `FreeStorageSpace` drops sharply during query bursts then plateaus (no recovery)
- s1: [Step 4] `information_schema.files` shows a large `ibtmp1` `size_gb`
**Interventions:**
- **remediation** (root): reduce temp spill at the source — add covering indexes / rewrite the offending queries so they no longer build large on-disk temp tables (identify via the slow query log and `EXPLAIN`).

  ```sql
  -- example: index the column being sorted/grouped to avoid disk temp tables
  CREATE INDEX idx_<col> ON <schema>.<table> (<col>);
  ```

  **Verification:** re-run Step 4 after reboot — `ibtmp1` stays small under the same workload; `Created_tmp_disk_tables` stops climbing (`SHOW GLOBAL STATUS LIKE 'Created_tmp_disk_tables';`).
- **mitigation** (s1): reboot the instance to reclaim the space held by `ibtmp1` (the tablespace is recreated at its initial size on restart).

  ```bash
  aws rds reboot-db-instance --db-instance-identifier <db-id>
  ```

  **Risk:** reboot causes a brief outage (and a failover on Multi-AZ). **Duration:** until the next large temp-table query refills `ibtmp1`. **Verification:** re-run Step 4; `ibtmp1` `size_gb` is back at its small initial value.

### Cause D: PostgreSQL WAL pinned by an inactive replication slot
**Statement:** An inactive logical/physical replication slot (e.g. an orphaned DMS CDC task or a dead subscriber) has no consumer, so PostgreSQL retains all WAL since the slot's `restart_lsn`, filling `pg_wal`.
**Chain:**
- root: an inactive replication slot with no consumer pins WAL from its restart_lsn
- s1: WAL segments accumulate in `pg_wal` and are not recycled
- s2: transaction-log disk usage rises and free storage falls
- D: DiskFull / storage-full (Symptom Recognition)
**Indicators:**
- root: [Step 5] `pg_replication_slots` shows a slot with `active = f` and large `retained_wal`
- s1: [Step 2] `FreeStorageSpace` declines continuously even with no data growth
**Interventions:**
- **remediation** (root): drop the orphaned inactive slot so PostgreSQL can recycle the retained WAL.

  ```sql
  SELECT pg_drop_replication_slot('<slot_name>');
  ```

  **Verification:** re-run Step 5 — the slot is gone; `FreeStorageSpace` (Step 2) rises as WAL is recycled.
- **defensive_fix** (s1): bound future WAL retention per slot so a stalled consumer can never fill the volume.

  ```sql
  -- set via DB parameter group (e.g. 10GB); -1 (default) means unlimited
  -- aws rds modify-db-parameter-group ... max_slot_wal_keep_size = 10240 (MB)
  SHOW max_slot_wal_keep_size;
  ```

  **Verification:** `SHOW max_slot_wal_keep_size;` reflects the new bound; a stalled slot is now invalidated past the limit instead of growing WAL without end.

### Cause Z: Unidentified
**Statement:** The storage-full condition does not match any known cause above (autoscaling, binlogs, temp-table bloat, or pinned WAL); the consuming object is unknown.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the database SME.

  ```bash
  aws rds describe-db-instances --db-instance-identifier <db-id> > rds-desc.json
  aws rds describe-events --source-identifier <db-id> --source-type db-instance \
    --duration 1440 > rds-events.json
  aws cloudwatch get-metric-statistics --namespace AWS/RDS \
    --metric-name FreeStorageSpace \
    --dimensions Name=DBInstanceIdentifier,Value=<db-id> \
    --start-time "$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
    --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --period 300 --statistics Minimum > freestorage.json
  ```

  **Risk:** none (read-only capture). **Duration:** N/A. **Verification:** the three JSON artifacts exist and are attached to the escalation ticket for SME review.

## Prevention

- Enable storage autoscaling with a sensible ceiling: `aws rds modify-db-instance --db-instance-identifier <db-id> --max-allocated-storage <N>`. Autoscaling fires when free space is low and grows by the greatest of 10 GiB, 10% of allocated, or predicted 7h growth.
- Create two CloudWatch alarms on `AWS/RDS` `FreeStorageSpace` (dimension `DBInstanceIdentifier`): a warning at ~25 GB and a critical at ~10 GB.
- Subscribe to RDS event notifications for the `low storage` category to receive `RDS-EVENT-0007` and `RDS-EVENT-0089`.
- MySQL/MariaDB: keep `binlog retention hours` as low as your replication needs allow (NULL to purge ASAP; max 168h). Monitor `SHOW BINARY LOGS` size.
- MySQL: reduce on-disk internal temp tables by indexing sort/group keys; watch `Created_tmp_disk_tables`.
- PostgreSQL: alert on inactive replication slots and set `max_slot_wal_keep_size` to bound per-slot WAL retention; run regular `VACUUM` to control table bloat and set `temp_file_limit` to cap runaway temp spill.

## Sources

- [CHAP Troubleshooting](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Troubleshooting.html) — RDS troubleshooting overview; storage-full state behavior and that the instance is unreachable when full.
- [USER PerfInsights](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PerfInsights.html) — Performance Insights for diagnosing I/O and resource pressure that drives temp/IO storage growth.
- [Rds out of storage](https://repost.aws/knowledge-center/rds-out-of-storage) — resolving RDS out-of-storage; increase allocated storage by ≥10%, use autoscaling.
- [Storage full rds cloudwatch alarm](https://repost.aws/knowledge-center/storage-full-rds-cloudwatch-alarm) — FreeStorageSpace CloudWatch alarms; 25 GB / 10 GB thresholds; subscribe to RDS-EVENT-0007 / RDS-EVENT-0089.
- [Rds mysql storage full issues](https://repost.aws/knowledge-center/rds-mysql-storage-full-issues) — MySQL storage-full: SHOW BINARY LOGS, information_schema.tables, ibtmp1 query, reboot to reclaim, binlog retention.
- [Mysql stored proc configuring](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/mysql-stored-proc-configuring.html) — `mysql.rds_set_configuration('binlog retention hours', N)` (max 168), `mysql.rds_show_configuration`, NULL = purge ASAP.
- [USER PIOPS.Autoscaling](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIOPS.Autoscaling.html) — autoscaling via `--max-allocated-storage`; growth = max(10 GiB, 10%, predicted 7h); 6h modify lockout.
- [USER PIOPS.ModifyingExisting.ScalingUp](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIOPS.ModifyingExisting.ScalingUp.html) — scaling up allocated storage with `modify-db-instance`.
- [Diskfull error rds postgresql](https://repost.aws/knowledge-center/diskfull-error-rds-postgresql) — PostgreSQL DiskFull; WAL pinned by inactive replication slots; pg_replication_slots query; pg_drop_replication_slot; max_slot_wal_keep_size; VACUUM for bloat.
- [USER Events.Messages](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_Events.Messages.html) — RDS event categories and exact low-storage event messages (RDS-EVENT-0007 / RDS-EVENT-0089).
