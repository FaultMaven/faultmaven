---
id: pg-replication-lag
title: "PostgreSQL Replication Lag"
domain: database
service: postgresql
symptom_class:
  - replication_lag
  - latency
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - postgresql
  - replication
  - streaming-replication
  - wal
  - standby
  - replica-lag
difficulty: intermediate
---

# PostgreSQL Replication Lag

## Problem Definition

Applies to PostgreSQL 10 and later with streaming replication configured (physical replication). Requires superuser or `pg_monitor` role on the primary for `pg_stat_replication` visibility, and access to replica instances for `pg_stat_wal_receiver` and recovery status checks.

Replication lag occurs when standby replicas fall behind the primary server in applying WAL (Write-Ahead Log) records. Applications reading from replicas observe stale data, read-after-write inconsistencies, or missing recently committed rows.

Symptoms include application queries returning outdated results from replica connections, monitoring alerts on replication delay metrics, growing WAL file accumulation on the primary (visible as disk usage increase in `pg_wal/`), and replica queries returning data that is seconds, minutes, or hours behind the primary. In extreme cases, the primary may run out of disk space due to retained WAL segments if `wal_keep_size` or replication slots prevent cleanup.

Error messages on the replica side may include:

```text
FATAL: could not receive data from WAL stream: ERROR: requested WAL segment has already been removed
```

This indicates the replica has fallen so far behind that the primary has already recycled the WAL segments it needs.

Common causes include network bandwidth limitations between primary and replica, replica under-provisioned for CPU or I/O relative to write volume, long-running queries on the replica that conflict with WAL replay (when `hot_standby_feedback` is off), high write throughput on the primary exceeding the replica's apply rate, and replication slot retention preventing WAL cleanup while the replica is disconnected.

## Diagnostic Steps

### Step 1. Check replication status on the primary

Query the primary to see the current state of all connected replicas and their lag.

```sql
SELECT
  client_addr,
  application_name,
  state,
  sent_lsn,
  write_lsn,
  flush_lsn,
  replay_lsn,
  pg_wal_lsn_diff(sent_lsn, replay_lsn) AS replay_lag_bytes,
  pg_size_pretty(pg_wal_lsn_diff(sent_lsn, replay_lsn)) AS replay_lag_pretty,
  write_lag,
  flush_lag,
  replay_lag
FROM pg_stat_replication;
```

Expected output: `replay_lag_bytes` near 0 and `replay_lag` under 1 second for healthy replication. If `replay_lag_bytes` is in the megabytes or gigabytes, the replica is significantly behind. The `state` column should be `streaming`; if it shows `catchup`, the replica is actively trying to recover.

### Step 2. Check replication lag on the replica

Query the replica directly to measure its own view of the delay.

```sql
SELECT
  now() - pg_last_xact_replay_timestamp() AS replication_delay,
  pg_is_in_recovery() AS is_replica,
  pg_last_wal_receive_lsn() AS last_received,
  pg_last_wal_replay_lsn() AS last_replayed,
  pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn()) AS receive_replay_gap_bytes;
```

If `replication_delay` is large but `receive_replay_gap_bytes` is small, the replica has received WAL but the primary is idle (no new transactions to generate timestamps). If both are large, the replica is genuinely behind in applying WAL.

### Step 3. Check WAL generation rate on the primary

Determine how much WAL the primary is generating to assess whether the replica can keep up.

```sql
SELECT
  pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0') AS total_wal_generated,
  pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')) AS total_wal_pretty;
```

Compare WAL generation over a time interval:

```bash
# Run on primary, 60 seconds apart
psql -c "SELECT pg_current_wal_lsn();" && sleep 60 && psql -c "SELECT pg_current_wal_lsn();"
```

Then compute the difference. If the primary generates WAL faster than the replica can apply it, the lag will grow indefinitely.

### Step 4. Check for replication slot WAL retention

Replication slots prevent WAL cleanup, which can fill the primary's disk.

```sql
SELECT
  slot_name,
  slot_type,
  active,
  pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes,
  pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_pretty
FROM pg_replication_slots;
```

Inactive slots (`active = false`) with large `retained_bytes` are preventing WAL cleanup. These are often from disconnected replicas or decommissioned standby servers.

### Step 5. Check for replay conflicts on the replica

Long-running queries on the replica can conflict with WAL replay, pausing replay and increasing lag.

```sql
-- Run on the replica
SELECT
  datname,
  confl_tablespace,
  confl_lock,
  confl_snapshot,
  confl_bufferpin,
  confl_deadlock
FROM pg_stat_database_conflicts
WHERE datname = current_database();
```

Non-zero values in `confl_snapshot` or `confl_lock` indicate that read queries on the replica have been canceled due to WAL replay conflicts, or that replay has been paused to avoid canceling them.

### Step 6. Check replica I/O and CPU capacity

Insufficient hardware resources on the replica prevent it from keeping up with WAL apply.

```bash
# Check I/O wait and CPU utilization on the replica
iostat -x 5 3
top -bn1 | head -20
```

High `%iowait` or sustained CPU above 90% on the replica indicates that the hardware is the bottleneck. WAL replay is single-threaded in PostgreSQL, so a single slow CPU core can become the limiting factor.

## Mitigation

### Option 1. Drop inactive replication slots

**Risk**: Low if the slot's replica is decommissioned. The dropped slot cannot be recovered, so ensure the associated replica is no longer needed.

**Command**:

```sql
-- Identify inactive slots
SELECT slot_name, active FROM pg_replication_slots WHERE NOT active;

-- Drop a specific inactive slot
SELECT pg_drop_replication_slot('old_standby_slot');
```

**Verify**:

```sql
SELECT slot_name, active,
  pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
FROM pg_replication_slots;
```

**Duration**: Immediate. WAL cleanup begins at the next checkpoint.

### Option 2. Cancel conflicting queries on the replica

**Risk**: Low. Read queries on the replica are interrupted, but no data is lost.

**Command**:

```sql
-- Run on the replica: terminate queries older than 1 minute
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'active'
  AND now() - query_start > interval '1 minute'
  AND pid != pg_backend_pid();
```

**Verify**:

```sql
SELECT now() - pg_last_xact_replay_timestamp() AS replication_delay;
```

**Duration**: Lag reduction begins immediately as WAL replay resumes.

### Option 3. Temporarily increase max_standby_streaming_delay

**Risk**: Medium. Allows read queries on the replica to run longer without being canceled, but this increases replication lag.

**Command**:

```sql
-- Run on the replica
ALTER SYSTEM SET max_standby_streaming_delay = '5min';
SELECT pg_reload_conf();
```

**Verify**:

```sql
SHOW max_standby_streaming_delay;
```

**Duration**: Immediate after reload. Revert after resolving the conflicting query issue.

### Option 4. Restart WAL receiver on the replica

**Risk**: Low. Briefly disconnects and reconnects the replication stream. Useful when the connection has stalled.

**Command**:

```bash
# On the replica, restart the PostgreSQL service
sudo systemctl restart postgresql
```

**Verify**:

```sql
-- On the primary
SELECT client_addr, state, replay_lag FROM pg_stat_replication;
```

**Duration**: Restart takes 5-30 seconds. The replica reconnects and resumes streaming automatically.

## Root Cause Resolution

**If** the replica cannot keep up with WAL generation rate due to I/O limitations → upgrade the replica's storage to faster disks (SSD/NVMe) or increase IOPS. WAL replay is write-intensive:

```bash
# Check current disk throughput on replica
iostat -x 5 3 | grep -E 'Device|sda|nvme'
```

**If** network bandwidth between primary and replica is the bottleneck → enable WAL compression to reduce transfer volume:

```sql
-- On the primary
ALTER SYSTEM SET wal_compression = on;
SELECT pg_reload_conf();
```

Also verify network throughput between primary and replica:

```bash
iperf3 -c replica_host -t 10
```

**If** long-running queries on the replica conflict with WAL replay → enable `hot_standby_feedback` so the primary knows about the replica's oldest running query and preserves needed row versions:

```sql
-- On the replica
ALTER SYSTEM SET hot_standby_feedback = on;
SELECT pg_reload_conf();
```

Note: this prevents certain vacuums on the primary, so monitor for table bloat.

**If** inactive replication slots retain WAL indefinitely → set `max_slot_wal_keep_size` (PostgreSQL 13+) to cap WAL retention:

```sql
-- On the primary: limit slot WAL retention to 10 GB
ALTER SYSTEM SET max_slot_wal_keep_size = '10GB';
SELECT pg_reload_conf();
```

Slots exceeding this limit become invalidated, and the replica must be rebuilt from a base backup.

**If** a single replica handles too many read queries → add additional replicas to distribute read load, or route heavy analytics queries to a dedicated replica that can tolerate higher lag.

## Verification

After applying fixes, confirm replication is healthy.

1. Replication lag on primary is minimal:

```sql
SELECT
  client_addr,
  replay_lag,
  pg_size_pretty(pg_wal_lsn_diff(sent_lsn, replay_lsn)) AS lag_bytes
FROM pg_stat_replication;
```

Expect `replay_lag` under 1 second and `lag_bytes` under 1 MB for healthy streaming replication.

1. Replica timestamp delay is acceptable:

```sql
-- Run on replica
SELECT now() - pg_last_xact_replay_timestamp() AS delay;
```

Expect under 5 seconds for most workloads.

1. No inactive replication slots retaining excessive WAL:

```sql
SELECT slot_name, active,
  pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
FROM pg_replication_slots;
```

1. No replay conflicts accumulating:

```sql
-- Run on replica
SELECT confl_snapshot, confl_lock FROM pg_stat_database_conflicts WHERE datname = current_database();
```

Note the values and check again after 10 minutes to confirm they are not increasing.

1. Primary WAL directory size is stable:

```bash
du -sh /var/lib/postgresql/16/main/pg_wal/
```

Expect the size to remain stable rather than growing continuously.

## Prevention

1. **Monitor replication lag continuously** — Alert when `replay_lag` exceeds 10 seconds or `replay_lag_bytes` exceeds 100 MB. Use the primary's `pg_stat_replication` view as the source of truth.

2. **Set max_slot_wal_keep_size** — On PostgreSQL 13+, configure to cap WAL retention from replication slots (recommended: 10-50 GB depending on write volume) to prevent disk exhaustion.

3. **Right-size replica hardware** — Replicas must have I/O and CPU capacity to match the primary's write throughput. WAL replay is single-threaded, so single-core performance matters.

4. **Enable wal_compression** — Reduces WAL transfer volume over the network at the cost of minor CPU overhead on the primary and replica.

5. **Use hot_standby_feedback judiciously** — Enable on replicas that serve long-running read queries to prevent replay conflicts. Monitor for increased bloat on the primary as a side effect.

6. **Limit query duration on replicas** — Set `statement_timeout` on replica connections to prevent queries from blocking WAL replay for extended periods.

7. **Clean up unused replication slots** — Regularly audit `pg_replication_slots` and drop slots for decommissioned replicas. Automate this check in your monitoring system.

8. **Plan for network capacity** — Ensure network bandwidth between primary and replica can handle peak WAL generation rates with headroom. Measure with `iperf3` during load tests.

## Sources

- [PostgreSQL Documentation: Monitoring Replication (pg_stat_replication)](https://www.postgresql.org/docs/current/monitoring-stats.html#MONITORING-PG-STAT-REPLICATION-VIEW) — Official reference for replication monitoring views and lag measurement.
- [PostgreSQL Documentation: Streaming Replication](https://www.postgresql.org/docs/current/warm-standby.html#STREAMING-REPLICATION) — Official reference for configuring and managing streaming replication.
- [PostgreSQL Documentation: Replication Slots](https://www.postgresql.org/docs/current/warm-standby.html#STREAMING-REPLICATION-SLOTS) — Official reference for replication slots, WAL retention, and `max_slot_wal_keep_size`.
- [PostgreSQL Documentation: Hot Standby Conflicts](https://www.postgresql.org/docs/current/hot-standby.html#HOT-STANDBY-CONFLICT) — Official reference for query conflicts during WAL replay and `hot_standby_feedback`.
