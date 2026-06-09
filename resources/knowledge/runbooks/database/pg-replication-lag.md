---
id: pg-replication-lag
title: "PostgreSQL Streaming Replication Lag"
domain: database
service: postgresql
symptom_class:
  - replication_lag
  - latency
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags:
  - postgresql
  - streaming-replication
  - wal
  - replication-slots
  - hot-standby
  - replay-lag
difficulty: intermediate
---

## Symptom Recognition

Replicas serve stale data while the primary keeps committing. `pg_stat_replication` on the primary reports `replay_lag` above the SLO (seconds, minutes, or hours), `replay_lsn` falls progressively behind `pg_current_wal_lsn()`, and `pg_wal_lsn_diff(sent_lsn, replay_lsn)` grows into the tens of MB or more. On the replica side, `pg_last_xact_replay_timestamp()` is far in the past, and applications observe read-after-write inconsistencies when routed to a standby. The primary's `pg_wal/` directory grows when a slow or disconnected replica holds back WAL recycling. In the worst case the replica WAL receiver errors out with:

```text
FATAL: could not receive data from WAL stream: ERROR: requested WAL segment has already been removed
```

Synchronous replication setups expose the same condition as elevated `COMMIT` latency on the primary, because backends block waiting for `flush_lsn`/`replay_lsn` from the named standbys.

## Applicability

- PostgreSQL 12 or later in physical streaming replication (`wal_level = replica` or `logical`). Specific features called out below: `max_slot_wal_keep_size` (PG 13+), `wal_compression = lz4|zstd` (PG 15+), `idle_replication_slot_timeout` (PG 18+).
- `pg_monitor` role membership (or superuser) on the primary to read `pg_stat_replication`, `pg_replication_slots`, and `pg_settings`.
- Login to each standby to read `pg_stat_wal_receiver`, `pg_stat_database_conflicts`, and to run `pg_last_wal_receive_lsn()` / `pg_last_wal_replay_lsn()` / `pg_last_xact_replay_timestamp()`.
- Shell access to primary and replica hosts (or managed-service parameter groups for RDS / Cloud SQL / Azure) to change `postgresql.conf` and to inspect `pg_wal/` size, CPU, and disk I/O.
- Optional: `iostat`, `top`, and `iperf3` (or equivalent) for replica capacity and network measurements.

## Diagnostic Steps

### Step 1: Measure replication lag from the primary

```sql
SELECT
  application_name,
  client_addr,
  state,
  sync_state,
  pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn)   AS send_backlog_bytes,
  pg_wal_lsn_diff(sent_lsn,             flush_lsn)  AS flush_backlog_bytes,
  pg_wal_lsn_diff(flush_lsn,            replay_lsn) AS replay_backlog_bytes,
  write_lag, flush_lag, replay_lag
FROM pg_stat_replication
ORDER BY replay_lag DESC NULLS LAST;
```

Expected output: `state = 'streaming'`, `replay_lag` under 1 second, all `*_backlog_bytes` under 1 MB under steady load. `state = 'catchup'` means a freshly connected replica is still recovering. The largest of the three backlogs localises the bottleneck: `send_backlog_bytes` → primary or network can't ship WAL fast enough; `flush_backlog_bytes` → standby fsync / disk is the bottleneck; `replay_backlog_bytes` → standby is receiving but not applying.

### Step 2: Measure replication lag from the replica

```sql
SELECT
  pg_is_in_recovery()                           AS is_replica,
  now() - pg_last_xact_replay_timestamp()       AS replay_age,
  pg_last_wal_receive_lsn()                     AS received_lsn,
  pg_last_wal_replay_lsn()                      AS replayed_lsn,
  pg_wal_lsn_diff(pg_last_wal_receive_lsn(),
                  pg_last_wal_replay_lsn())     AS receive_to_replay_bytes;
```

Expected output: `replay_age` under a few seconds and `receive_to_replay_bytes` near 0. Large `replay_age` with `receive_to_replay_bytes ≈ 0` simply means the primary is idle (no new commits). Large `replay_age` AND large `receive_to_replay_bytes` means WAL is arriving but the standby is not applying it fast enough — usually CPU/I/O bound replay or replay paused by conflicts.

### Step 3: Inspect the WAL receiver on the replica

```sql
SELECT
  status,
  sender_host,
  slot_name,
  written_lsn,
  flushed_lsn,
  last_msg_send_time,
  last_msg_receipt_time,
  now() - last_msg_receipt_time AS since_last_message
FROM pg_stat_wal_receiver;
```

Expected output: `status = 'streaming'` and `since_last_message` < `wal_receiver_status_interval` (default 10 s). `status` in `('stopped', 'restarting', 'waiting')` or zero rows means the receiver has terminated; check the replica's PostgreSQL log for `FATAL` messages, especially `requested WAL segment has already been removed` and `terminating walreceiver due to timeout`.

### Step 4: Audit replication slots and WAL retention on the primary

```sql
SELECT
  slot_name, slot_type, active, wal_status, safe_wal_size,
  pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes,
  pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
FROM pg_replication_slots;
```

Expected output: every slot has `active = t` and `wal_status = 'reserved'` or `'extended'`. `active = f` with large `retained_bytes` is an orphan slot from a disconnected/decommissioned replica — it is the most common cause of primary `pg_wal/` disk fill. `wal_status = 'lost'` means `max_slot_wal_keep_size` has invalidated the slot and the replica must be rebuilt.

### Step 5: Check replay conflicts on the replica

```sql
SELECT datname, confl_tablespace, confl_lock, confl_snapshot,
       confl_bufferpin, confl_deadlock
FROM pg_stat_database_conflicts
WHERE datname = current_database();
```

Expected output: counters stable over time. Rising `confl_snapshot` or `confl_bufferpin` means VACUUM cleanup records or buffer pins on the primary conflict with read queries on the replica, and the standby is pausing replay up to `max_standby_streaming_delay` (default 30 s) before cancelling those queries — both directly elongate replay lag.

### Step 6: Quantify WAL generation rate on the primary

```bash
psql -At -c "SELECT pg_current_wal_lsn();" > /tmp/lsn1; date +%s > /tmp/t1
sleep 60
psql -At -c "SELECT pg_current_wal_lsn();" > /tmp/lsn2; date +%s > /tmp/t2
psql -At -c "SELECT pg_wal_lsn_diff('$(cat /tmp/lsn2)','$(cat /tmp/lsn1)') /
             ($(cat /tmp/t2) - $(cat /tmp/t1))::numeric AS bytes_per_sec;"
```

Expected output: sustained WAL byte rate. Compare against the replica's measured apply throughput from Step 1 (`replay_lag` × measured bytes/s converted). If the primary generates WAL faster than any single core on the replica can apply it (WAL replay is single-threaded), the lag will grow without bound regardless of network or storage.

### Step 7: Measure replica CPU and disk I/O

```bash
iostat -x 5 3
mpstat -P ALL 5 3
top -bn1 -c -p $(pgrep -d, -f 'postgres:.*startup recovering')
```

Expected output: the recovery / `startup` process (single-threaded WAL applier) should not be pegged at 100% on its core, and the data-volume `%iowait` should stay under 20%. CPU saturation on the single recovery process or sustained `%iowait > 30%` on the WAL/data device means the replica hardware is the apply bottleneck.

### Step 8: Verify network throughput between primary and replica

```bash
# From replica, measure raw TCP throughput to the primary
iperf3 -c <primary-host> -t 10 -p 5201
# Also check observed WAL receive rate from PostgreSQL:
psql -At -c "SELECT pg_size_pretty(pg_wal_lsn_diff(pg_last_wal_receive_lsn(), '0/0'));"
```

Expected output: measured `iperf3` bitrate well above the primary's WAL generation rate from Step 6, with no retransmits. Bitrate close to or below the WAL generation rate is the network bottleneck; high TCP retransmits indicate a lossy path that triggers `wal_receiver_timeout` disconnects.

## Causes

### Cause A: Replica hardware cannot keep up with WAL apply rate
**Statement:** The standby's recovery process cannot apply WAL as fast as the primary generates it because the single-threaded replay is CPU-bound or the data volume is I/O-saturated.
**Mechanism:** PostgreSQL WAL replay on a physical standby runs in a single `startup` process; it cannot parallelise across cores. When that core saturates or its writes queue on a slow disk, applied bytes per second drop below the primary's WAL generation rate. `received_lsn` keeps moving (Step 3) while `replayed_lsn` falls behind, so `receive_to_replay_bytes` (Step 2) and `replay_backlog_bytes` (Step 1) both grow monotonically until the workload subsides or the replica is upgraded.
**Indicator:**
- [Step 1] `replay_backlog_bytes` grows while `flush_backlog_bytes` stays small
- [Step 2] `receive_to_replay_bytes` increases over successive samples
- [Step 7] recovery / `startup` process at or near 100% on its core, or sustained `%iowait > 30%` on the data volume
<!-- match: {"step": 7, "predicate": "threshold", "target": "iowait_pct", "op": ">", "value": 30} -->
**Mitigation:**
- **Risk:** Low. Routing read traffic away from the lagging replica lets it spend all I/O budget on apply, but reduces read capacity until the replica catches up.

- **Command:**

  ```bash
  # Drain the replica from the load balancer / pgpool / HAProxy backend pool
  # Example for HAProxy via runtime API:
  echo "disable server pgread/replica1" | socat stdio /run/haproxy/admin.sock
  ```

- **Duration:** Until `replay_lag` returns to baseline. Watch `replay_backlog_bytes` shrink in Step 1.

**Resolution:**

```bash
# Move the replica to faster local storage (NVMe / provisioned-IOPS volume).
# On AWS RDS / Cloud SQL this is an instance modify operation; on self-managed
# PostgreSQL, rsync the data directory to the new device and point the symlink:
sudo systemctl stop postgresql
sudo rsync -aHX /var/lib/postgresql/16/main/ /mnt/nvme/pgdata/
sudo ln -sfn /mnt/nvme/pgdata /var/lib/postgresql/16/main
sudo systemctl start postgresql
```

For CPU-bound replay, scale the replica's instance class up so the single-core clock matches or exceeds the primary's, and prefer instance families with higher per-core frequency rather than higher core counts (extra cores do not help replay).

- **Impact:** Affects only the upgraded replica. Requires a brief restart (seconds to minutes) which disconnects existing read connections; the WAL stream resumes from `restart_lsn` automatically.
- **Rollback:** Revert the instance class change (or symlink the data directory back to the original device) and restart PostgreSQL.

**Verification:** After 10 minutes of representative load, Step 2 shows `receive_to_replay_bytes` stable near 0 and `replay_age` under your SLO. Step 7 shows the recovery process below 70% on its core with `%iowait` under 10%.

### Cause B: Inactive replication slot retains WAL on the primary
**Statement:** A replication slot still exists on the primary for a disconnected or decommissioned standby, and its `restart_lsn` pins WAL retention, causing `pg_wal/` to grow until either the slot is dropped, the standby reconnects, or the disk fills.
**Mechanism:** A physical replication slot guarantees that the primary will not recycle any WAL segment newer than the slot's `restart_lsn`, even while the consumer is offline. With `max_slot_wal_keep_size = -1` (the default, unlimited) the primary keeps WAL indefinitely. The growing `pg_wal/` directory shows up as rising disk usage on the primary; lag is not yet visible because there is no replica to be late, but reconnecting that replica (or any new replica that uses the slot) will start far behind and may never catch up. If the slot is invalidated by `max_slot_wal_keep_size`, the replica receives `requested WAL segment has already been removed` and must be rebuilt.
**Indicator:**
- [Step 4] one or more rows with `active = false` and `retained_bytes` in the hundreds of MB or more
- [Step 4] `wal_status` is `'extended'` (or `'lost'` if already invalidated)
- [Symptom] primary `pg_wal/` directory growing without bound
<!-- match: {"step": 4, "predicate": "contains", "target": "active = false"} -->
**Mitigation:**
- **Risk:** Low if the slot truly belongs to a decommissioned replica; medium if a real standby is just temporarily offline — dropping its slot forces a base-backup rebuild when it returns.

- **Command:**

  ```sql
  -- Confirm the slot belongs to no live consumer, then drop it
  SELECT slot_name, active, wal_status,
         pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
  FROM pg_replication_slots
  WHERE NOT active;

  SELECT pg_drop_replication_slot('<orphan_slot_name>');
  ```

- **Duration:** WAL cleanup runs at the next checkpoint (default within `checkpoint_timeout = 5min`). Disk usage drops within that window.

**Resolution:**

```sql
-- Cap WAL retained by any single slot (PostgreSQL 13+).
ALTER SYSTEM SET max_slot_wal_keep_size = '20GB';
SELECT pg_reload_conf();

-- (PostgreSQL 18+) auto-invalidate idle slots after a grace period.
ALTER SYSTEM SET idle_replication_slot_timeout = '24h';
SELECT pg_reload_conf();
```

Combine with monitoring on `pg_replication_slots.wal_status` so the on-call hears about slot invalidation before `pg_wal/` fills the disk.

- **Impact:** Cluster-wide setting. `max_slot_wal_keep_size` will cause replicas that fall behind by more than the limit to be invalidated and require rebuild — set the limit generously above your expected lag budget.
- **Rollback:** `ALTER SYSTEM RESET max_slot_wal_keep_size;` then `SELECT pg_reload_conf();` to restore unlimited retention.

**Verification:** `du -sh $PGDATA/pg_wal/` on the primary stabilises or shrinks. Step 4 returns either no inactive slots or `wal_status = 'reserved'` on the remaining ones. Disk free metric stops decreasing.

### Cause C: WAL receiver timed out or disconnected (network instability)
**Statement:** The replica's WAL receiver loses its TCP connection to the primary faster than it can re-establish it, so the standby alternates between streaming and reconnect, accumulating lag during each gap.
**Mechanism:** `wal_sender_timeout` on the primary (default 60 s) and `wal_receiver_timeout` on the replica (default 60 s) terminate connections when keepalive replies stop arriving. A flaky network path, undersized inter-region link, or aggressive firewall idle-timeout drops the TCP session; the replica reconnects, replays the gap, then loses the link again. Each disconnect leaves a window during which `pg_wal/` accumulates on the primary (if a slot is in use) and `replay_age` on the replica grows. Step 3 shows `status` flapping; Step 8 shows retransmits or insufficient bitrate.
**Indicator:**
- [Step 3] `status` not equal to `'streaming'`, or `since_last_message` greater than `wal_receiver_status_interval`
- [Step 8] `iperf3` bitrate close to or below WAL generation rate, or non-zero TCP retransmits
- [Symptom] replica log contains `terminating walreceiver due to timeout` or `could not receive data from WAL stream`
<!-- match: {"step": 3, "predicate": "contains", "target": "status"} -->
**Mitigation:**
- **Risk:** Low. Raising timeouts and adding TCP keepalives tolerates short network blips; they do not mask a real outage because the slot still retains WAL.

- **Command:**

  ```sql
  -- On primary: extend send-side timeout and enable keepalives
  ALTER SYSTEM SET wal_sender_timeout = '120s';
  ALTER SYSTEM SET tcp_keepalives_idle = 60;
  ALTER SYSTEM SET tcp_keepalives_interval = 10;
  ALTER SYSTEM SET tcp_keepalives_count = 6;
  SELECT pg_reload_conf();

  -- On replica: extend receive-side timeout
  ALTER SYSTEM SET wal_receiver_timeout = '120s';
  SELECT pg_reload_conf();
  ```

- **Duration:** Until the network path is fixed. These values are safe to leave in place long-term; revisit if failover RTO is sensitive to disconnect detection time.

**Resolution:**

```bash
# Identify and remove the network instability.
# 1. Confirm bandwidth headroom (must exceed peak WAL bytes/sec from Step 6):
iperf3 -c <primary-host> -t 30 -p 5201
# 2. Check the path for loss:
mtr -rwzc 100 <primary-host>
# 3. If the path is fine but disconnects persist, audit any stateful middleboxes
#    (NAT gateway, firewall, AWS NLB) for idle-timeout under 600s and raise it,
#    or set the PostgreSQL TCP keepalives shown above to keep the flow alive.
```

For chronically lossy paths, enable WAL compression to cut volume so the link tolerates more loss before queueing:

```sql
-- PostgreSQL 15+: lz4 / zstd are far cheaper than pglz
ALTER SYSTEM SET wal_compression = 'lz4';
SELECT pg_reload_conf();
```

- **Impact:** Cluster-wide. `wal_compression` costs CPU on the primary and any cascading sender; lz4/zstd are typically <5% on modern hardware. Affects all replicas and any base backup taken via `pg_basebackup`.
- **Rollback:** `ALTER SYSTEM RESET wal_compression;` and `pg_reload_conf();` to disable compression for new WAL.

**Verification:** Step 3 shows `status = 'streaming'` with `since_last_message` under 10 s for one hour. The replica's PostgreSQL log stops emitting `walreceiver` reconnect lines. Step 1 `send_backlog_bytes` returns to baseline.

### Cause D: Replay paused by recovery conflicts with long replica queries
**Statement:** Long-running read queries on the standby conflict with VACUUM cleanup records or buffer pins in the incoming WAL, so replay pauses up to `max_standby_streaming_delay` before either the WAL applies (cancelling the query) or replay continues to lag.
**Mechanism:** When the primary emits a cleanup WAL record for a row a standby query still considers visible, the standby cannot apply the record until the query finishes or `max_standby_streaming_delay` (default 30 s) elapses, at which point the query is cancelled. Each conflict event pushes replay back by up to the configured delay. If conflicts arrive in rapid succession, lag accumulates faster than replay can recover. `pg_stat_database_conflicts.confl_snapshot` and `confl_bufferpin` rise on the standby; replay backlog (Step 1, Step 2) grows even though network and disk are healthy.
**Indicator:**
- [Step 5] `confl_snapshot` or `confl_bufferpin` increasing across samples
- [Step 1] `replay_backlog_bytes` grows while `flush_backlog_bytes` stays near 0
- [Step 7] recovery process CPU is low (it is sleeping on the conflict, not working)
<!-- match: {"step": 5, "predicate": "contains", "target": "confl_snapshot"} -->
**Mitigation:**
- **Risk:** Medium. Cancelling the offending queries returns errors to the originating client; raising `max_standby_streaming_delay` reduces cancels but trades replay lag for query success.

- **Command:**

  ```sql
  -- On the replica: cancel old read queries blocking replay
  SELECT pid, now() - query_start AS duration, left(query, 120) AS query
  FROM pg_stat_activity
  WHERE state = 'active'
    AND backend_type = 'client backend'
    AND query_start < now() - interval '60 seconds'
  ORDER BY query_start ASC;

  SELECT pg_cancel_backend(<pid>);
  ```

- **Duration:** Immediate. Replay resumes within seconds of the conflicting query terminating.

**Resolution:**

```sql
-- On the replica: tell the primary to retain row versions for active replica queries.
ALTER SYSTEM SET hot_standby_feedback = on;
SELECT pg_reload_conf();
```

Trade-off: `hot_standby_feedback` prevents premature VACUUM on the primary, which can bloat tables that the standby keeps "pinned" via long queries. Pair it with a bounded `statement_timeout` on the replica role (e.g. `ALTER ROLE replica_reader SET statement_timeout = '5min';`) so a runaway analytics query cannot indefinitely block primary VACUUM.

- **Impact:** Cluster-wide effect on the primary's vacuuming behaviour. Hot-standby feedback can grow table bloat on the primary if replica queries run for hours; monitor `pg_stat_user_tables.n_dead_tup` after enabling.
- **Rollback:** `ALTER SYSTEM SET hot_standby_feedback = off; SELECT pg_reload_conf();` on the replica.

**Verification:** Step 5 conflict counters stop increasing across two consecutive 10-minute samples. Step 1 `replay_lag` returns under your SLO. Primary `pg_stat_user_tables.n_dead_tup` for hot tables is monitored and not growing without bound.

### Cause E: Primary WAL generation rate exceeds replica apply ceiling (write-spike or bulk load)
**Statement:** A bulk write workload on the primary — `COPY`, large `UPDATE`/`DELETE`, `CREATE INDEX`, partition swap — produces WAL faster than the replica can replay even with healthy hardware, and the lag stays elevated until the workload finishes.
**Mechanism:** Bulk operations on the primary generate WAL at a rate proportional to the rows written and the indexes maintained, often an order of magnitude above OLTP steady state. Even a correctly sized replica with single-threaded replay cannot match a primary that uses many backends in parallel for the bulk job. The lag rises during the operation and decays exponentially once it ends; the danger is that long bulk jobs run past the replication slot's WAL retention budget or push `max_slot_wal_keep_size` past invalidation.
**Indicator:**
- [Step 6] primary `bytes_per_sec` step-changes well above its OLTP baseline
- [Step 1] `replay_backlog_bytes` rises sharply, correlated with a known migration or import on the primary
- [Step 7] replica recovery process is busy but bounded; `%iowait` is moderate, not pinned
<!-- match: {"step": 6, "predicate": "threshold", "target": "primary_wal_bytes_per_sec", "op": ">", "value": 10000000} -->
**Mitigation:**
- **Risk:** Medium. Throttling the bulk job extends its wall-clock time but keeps replicas usable; aborting it may leave partial work that must be cleaned up.

- **Command:**

  ```sql
  -- Pause / cancel the bulk job on the primary if it is producing WAL faster
  -- than the replica's apply ceiling. Inspect first, then cancel:
  SELECT pid, application_name, state, query_start,
         now() - query_start AS running_for, left(query, 200) AS query
  FROM pg_stat_activity
  WHERE state = 'active'
    AND query ~* '(COPY|INSERT|UPDATE|DELETE|CREATE INDEX|REINDEX|VACUUM FULL)'
  ORDER BY query_start ASC;

  SELECT pg_cancel_backend(<bulk_pid>);
  ```

- **Duration:** Replay backlog should start shrinking within minutes of the bulk job ending. Resume the workload in smaller batches.

**Resolution:**

Re-shape the bulk workload so its WAL generation rate stays under the replica's apply ceiling: chunk large `UPDATE`/`DELETE` into batches with `LIMIT` + sleep, use `CREATE INDEX CONCURRENTLY` (lower lock cost, but still WAL-heavy — pace via batched data prep), and prefer partitioned `ATTACH PARTITION` swaps over `INSERT … SELECT` for migrations. Where the OLTP and bulk workloads are inherently mismatched, run bulk on a dedicated logical-replication target rather than streaming to OLTP replicas. Enable `wal_compression`:

```sql
ALTER SYSTEM SET wal_compression = 'lz4';
SELECT pg_reload_conf();
```

to cut WAL volume on bulk-heavy workloads.

- **Impact:** Cluster-wide. `wal_compression` adds a small CPU cost on the primary; chunked rewrites extend the migration window. No restart required.
- **Rollback:** `ALTER SYSTEM RESET wal_compression;` and re-run the unbatched migration with replicas drained from read traffic.

**Verification:** Step 6 `bytes_per_sec` stabilises near baseline once bulk completes. Step 1 `replay_lag` falls below SLO within the expected decay window (lag_bytes ÷ measured apply bytes/sec). Future bulk jobs show smaller peak `replay_lag` after chunking.

### Cause F: Synchronous replication blocks commits when a named standby is slow
**Statement:** `synchronous_standby_names` lists a replica that is lagging or stalled, so primary backends block on `COMMIT` waiting for `flush_lsn` or `replay_lsn` from that standby, surfacing as primary commit latency rather than replica freshness lag.
**Mechanism:** Under `synchronous_commit = on` (and especially `remote_apply`), the primary waits at commit time until the named synchronous standby acknowledges the configured durability level. If that standby falls behind due to any of Causes A–E, every primary commit blocks for the duration of the lag. Backends accumulate, `pg_stat_activity` shows many sessions in state `idle in transaction (aborted)` or `active` waiting on `SyncRep`, and application p95 commit latency tracks the slow replica's lag — even though the asynchronous replicas are healthy.
**Indicator:**
- [Step 1] `sync_state` is `'sync'` or `'quorum'` on the lagging replica
- [Step 1] `flush_lag` or `replay_lag` on the synchronous standby is well above SLO
- [Symptom] primary commit latency p95 rises in lockstep with replica `flush_lag`
<!-- match: {"step": 1, "predicate": "contains", "target": "sync"} -->
**Mitigation:**
- **Risk:** High. Lowering `synchronous_commit` or removing the slow standby from `synchronous_standby_names` weakens durability — RPO for the affected commits drops to local-fsync only. Document the change and revert as soon as the standby catches up.

- **Command:**

  ```sql
  -- Temporarily downgrade to local-only durability to unblock the primary.
  -- Do this only with explicit operator approval; you are accepting RPO > 0.
  ALTER SYSTEM SET synchronous_commit = local;
  SELECT pg_reload_conf();
  ```

- **Duration:** Until the synchronous standby catches up (Step 1 `replay_lag` near 0). Restore before declaring incident resolved.

**Resolution:**

Either repair the slow standby via the Cause A–E path that matches its bottleneck, or change the sync topology so a single weak replica cannot stall commits:

```sql
-- Use quorum-based synchronous replication so any N-of-M ack the commit;
-- one slow standby no longer blocks the primary.
ALTER SYSTEM SET synchronous_standby_names = 'ANY 1 (s1, s2, s3)';
SELECT pg_reload_conf();
```

If commit RPO requirements allow it, set `synchronous_commit = remote_write` instead of `on` / `remote_apply`: backends wait only for OS-buffered receipt, which removes the apply-lag contribution to commit latency.

- **Impact:** Cluster-wide change to durability semantics. Switching from priority (`FIRST n`) to quorum (`ANY n`) syntax does not change RPO if the same number of acknowledgements is required, but does change which standbys can be promoted with zero data loss. Update failover runbooks accordingly.
- **Rollback:** `ALTER SYSTEM SET synchronous_commit = on; ALTER SYSTEM SET synchronous_standby_names = '<prior value>'; SELECT pg_reload_conf();`.

**Verification:** Primary commit latency p95 returns to baseline. Step 1 shows `sync_state = 'sync'` (or `'quorum'`) on the intended standby(s) with `flush_lag` under SLO. No application requests time out on commit.

### Cause Z: Unidentified
**Statement:** Diagnostic steps do not converge on a single cause above, or the evidence is conflicting, and a confident root cause cannot be assigned to the observed replication lag.
**Mechanism:** Replication lag can also stem from interactions that the steps above do not directly probe: cascading replication where an intermediate standby is the actual bottleneck, logical replication conflicts on a separately decoded publication, timeline divergence after a partial promotion, kernel-level packet drops invisible to `iperf3`, or a managed-service throttle (RDS storage IOPS credits exhausted, Cloud SQL CPU throttle). Without a clear signal from `pg_stat_replication` backlog distribution, slot retention, recovery conflicts, primary WAL rate, or sync-rep state, applying any Cause A–F fix risks masking the real driver.
**Indicator:**
- [Default]
**Mitigation:**
- **Risk:** Diagnostic only. The goal is to gather evidence safely, not to fix.

- **Command:**

  ```bash
  # Snapshot full primary replication state
  psql -h <primary> -U postgres -c "\copy (
    SELECT now() AS captured_at, * FROM pg_stat_replication
  ) TO '/tmp/pg_stat_replication.csv' CSV HEADER"

  psql -h <primary> -U postgres -c "\copy (
    SELECT now() AS captured_at, * FROM pg_replication_slots
  ) TO '/tmp/pg_replication_slots.csv' CSV HEADER"

  # Snapshot replica state
  psql -h <replica> -U postgres -c "\copy (
    SELECT now() AS captured_at, * FROM pg_stat_wal_receiver
  ) TO '/tmp/pg_stat_wal_receiver.csv' CSV HEADER"

  psql -h <replica> -U postgres -c "\copy (
    SELECT now() AS captured_at, * FROM pg_stat_database_conflicts
  ) TO '/tmp/pg_stat_database_conflicts.csv' CSV HEADER"

  # Capture host-level evidence
  iostat -x 5 6     > /tmp/replica_iostat.txt
  mpstat -P ALL 5 6 > /tmp/replica_mpstat.txt
  ss -tnpi          > /tmp/replica_sockets.txt
  ```

- **Duration:** Diagnostic only — does not change system state.

**Resolution:** Out of runbook scope. Escalate to the database SRE/DBA on call with the snapshots above, the timeline of `replay_lag` from monitoring, and any managed-service event log (RDS events, Cloud SQL logs). Open a follow-up to capture the new failure mode as a dedicated runbook.
**Verification:** Escalation acknowledged with snapshots attached; incident review opens a ticket to extend this runbook with the new failure mode.

## Prevention

1. **Alert on lag before users notice.** Page when `pg_stat_replication.replay_lag` exceeds 10 s or `pg_wal_lsn_diff(sent_lsn, replay_lsn)` exceeds 100 MB on any standby for 5 minutes. Use the primary as source of truth — it sees all replicas.
2. **Cap slot WAL retention.** Set `max_slot_wal_keep_size` (PG 13+) to a value that bounds the worst-case `pg_wal/` growth (e.g. 20–50 GB depending on disk headroom). Replicas that fall further behind are intentionally invalidated rather than allowed to fill the primary disk.
3. **Auto-invalidate idle slots (PG 18+).** Set `idle_replication_slot_timeout = '24h'` so slots from decommissioned consumers are reclaimed automatically; verify nothing critical relies on a long-idle slot first.
4. **Size replica hardware for single-thread apply.** Pick instance families with high per-core clock speed for replicas; extra cores do not accelerate WAL replay. Match or exceed the primary's per-core performance.
5. **Enable `wal_compression`.** Use `lz4` (PG 15+) or `zstd` for a cheap reduction in WAL bytes shipped over the network, especially across regions or VPN tunnels.
6. **Use `hot_standby_feedback` selectively.** Enable on replicas that serve long-running read queries; pair with bounded `statement_timeout` on reader roles and monitor primary table bloat (`pg_stat_user_tables.n_dead_tup`).
7. **Bound replica query duration.** `ALTER ROLE replica_reader SET statement_timeout = '5min';` prevents one analytics query from holding replay open via conflict.
8. **Use quorum-based sync replication.** `synchronous_standby_names = 'ANY n (...)'` prevents one slow standby from stalling all primary commits; pair with monitoring on `sync_state` so a degraded quorum is paged immediately.
9. **Test failover and replica rebuild quarterly.** Slot invalidation, timeline divergence, and base-backup rebuild paths atrophy if not exercised — verify your runbook works before the real incident.
10. **Watch primary `pg_wal/` disk free.** `pg_wal/` filling the volume causes PostgreSQL to halt all writes; alert when free space on the WAL volume drops below `max_wal_size + max_slot_wal_keep_size + 10 GB` of safety margin.

## Sources

- [PostgreSQL Documentation: Log-Shipping Standby Servers (Streaming Replication)](https://www.postgresql.org/docs/current/warm-standby.html) — Priority 1. Authoritative for `wal_level`, `max_wal_senders`, replication slots, `primary_conninfo`, and the standby startup / promotion sequence.
- [PostgreSQL Documentation: The Cumulative Statistics System](https://www.postgresql.org/docs/current/monitoring-stats.html) — Priority 1. Authoritative for `pg_stat_replication`, `pg_stat_wal_receiver`, `pg_replication_slots`, and `pg_stat_database_conflicts` columns and semantics used in Steps 1–5.
- [PostgreSQL Documentation: Hot Standby](https://www.postgresql.org/docs/current/hot-standby.html) — Priority 1. Authoritative for recovery conflicts, query cancellation, `max_standby_streaming_delay`, `max_standby_archive_delay`, and `hot_standby_feedback` behaviour and defaults (Cause D).
- [PostgreSQL Documentation: Replication Configuration Parameters](https://www.postgresql.org/docs/current/runtime-config-replication.html) — Priority 1. Authoritative for `wal_keep_size`, `max_slot_wal_keep_size`, `wal_sender_timeout`, `wal_receiver_timeout`, `wal_receiver_status_interval`, `synchronous_standby_names`, `synchronous_commit`, `idle_replication_slot_timeout`, and their defaults.
- [PostgreSQL Documentation: Write Ahead Log Parameters](https://www.postgresql.org/docs/current/runtime-config-wal.html) — Priority 1. Authoritative for `wal_compression` values (`off`, `pglz`, `lz4`, `zstd`), `wal_level`, `max_wal_size`, `min_wal_size`, and `checkpoint_timeout` (Causes C and E).
