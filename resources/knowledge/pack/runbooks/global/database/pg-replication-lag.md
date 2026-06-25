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
version: "2.0.0"
last_updated: "2026-06-25"
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
**Statement:** The standby's single-threaded recovery process cannot apply WAL as fast as the primary generates it because replay is CPU-bound or the data volume is I/O-saturated.
**Chain:**
- root: the replica's single `startup` (recovery) process is CPU-bound or its data volume is I/O-saturated
- s1: applied WAL bytes/sec on the standby drop below the primary's WAL generation rate
- s2: `replayed_lsn` falls behind `received_lsn`, so receive-to-replay backlog grows monotonically
- D: the standby serves stale data and `replay_lag` exceeds the SLO
**Indicators:**
- root: [Step 7] recovery / `startup` process at or near 100% on its core, or sustained `%iowait > 30%` on the data volume
  <!-- match: {"step": 7, "predicate": "threshold", "target": "iowait_pct", "op": ">", "value": 30} -->
- s1: [Step 1] `replay_backlog_bytes` grows while `flush_backlog_bytes` stays small
- s2: [Step 2] `receive_to_replay_bytes` increases over successive samples
**Interventions:**
- **remediation** (root): move the replica to faster storage and/or scale to a higher per-core clock; extra cores do not help single-threaded replay.

  ```bash
  # Move the replica to faster local storage (NVMe / provisioned-IOPS volume).
  # On AWS RDS / Cloud SQL this is an instance modify operation; on self-managed
  # PostgreSQL, rsync the data directory to the new device and point the symlink:
  sudo systemctl stop postgresql
  sudo rsync -aHX /var/lib/postgresql/16/main/ /mnt/nvme/pgdata/
  sudo ln -sfn /mnt/nvme/pgdata /var/lib/postgresql/16/main
  sudo systemctl start postgresql
  ```

  **Verification:** After 10 minutes of representative load, Step 2 shows `receive_to_replay_bytes` stable near 0 and `replay_age` under SLO; Step 7 shows the recovery process below 70% on its core with `%iowait` under 10%.
- **mitigation** (s1): drain read traffic off the lagging replica so it can spend all I/O budget on apply.

  ```bash
  # Drain the replica from the load balancer / pgpool / HAProxy backend pool
  # Example for HAProxy via runtime API:
  echo "disable server pgread/replica1" | socat stdio /run/haproxy/admin.sock
  ```

  **Risk:** Low — reduces read capacity until the replica catches up. **Duration:** Until `replay_lag` returns to baseline; watch `replay_backlog_bytes` shrink in Step 1. **Verification:** Step 1 `replay_backlog_bytes` shrinks while the replica is drained.

### Cause B: Inactive replication slot retains WAL on the primary
**Statement:** A replication slot for a disconnected or decommissioned standby still exists on the primary, and its `restart_lsn` pins WAL retention, growing `pg_wal/` until the slot is dropped, the standby reconnects, or the disk fills.
**Chain:**
- root: a physical replication slot for an offline/decommissioned consumer remains on the primary
- s1: the slot's `restart_lsn` prevents the primary recycling any newer WAL segment
- s2: with `max_slot_wal_keep_size = -1` (default) the primary keeps WAL indefinitely and `pg_wal/` grows
- D: primary `pg_wal/` fills toward the disk limit; any replica reusing the slot starts far behind (or the slot is invalidated and must be rebuilt)
**Indicators:**
- root: [Step 4] one or more rows with `active = false` and `retained_bytes` in the hundreds of MB or more
  <!-- match: {"step": 4, "predicate": "contains", "target": "active = false"} -->
- s2: [Step 4] `wal_status` is `'extended'` (or `'lost'` if already invalidated)
- D: [Symptom] primary `pg_wal/` directory growing without bound
**Interventions:**
- **remediation** (root): cap per-slot WAL retention and auto-invalidate idle slots so a dead consumer can never fill the disk.

  ```sql
  -- Cap WAL retained by any single slot (PostgreSQL 13+).
  ALTER SYSTEM SET max_slot_wal_keep_size = '20GB';
  SELECT pg_reload_conf();

  -- (PostgreSQL 18+) auto-invalidate idle slots after a grace period.
  ALTER SYSTEM SET idle_replication_slot_timeout = '24h';
  SELECT pg_reload_conf();
  ```

  **Verification:** `du -sh $PGDATA/pg_wal/` stabilises or shrinks; Step 4 returns either no inactive slots or `wal_status = 'reserved'` on the remaining ones; disk free stops decreasing.
- **mitigation** (s1): after confirming no live consumer, drop the orphan slot to release pinned WAL.

  ```sql
  -- Confirm the slot belongs to no live consumer, then drop it
  SELECT slot_name, active, wal_status,
         pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained
  FROM pg_replication_slots
  WHERE NOT active;

  SELECT pg_drop_replication_slot('<orphan_slot_name>');
  ```

  **Risk:** Low if truly decommissioned; medium if a real standby is just temporarily offline — dropping its slot forces a base-backup rebuild when it returns. **Duration:** WAL cleanup runs at the next checkpoint (within `checkpoint_timeout = 5min`); disk usage drops in that window. **Verification:** Step 4 no longer lists the dropped slot; `du -sh $PGDATA/pg_wal/` shrinks after the next checkpoint.

### Cause C: WAL receiver timed out or disconnected (network instability)
**Statement:** The replica's WAL receiver loses its TCP connection to the primary faster than it can re-establish it, so the standby alternates between streaming and reconnect, accumulating lag during each gap.
**Chain:**
- root: a flaky/undersized network path or aggressive idle-timeout drops the WAL streaming TCP session
- s1: `wal_sender_timeout`/`wal_receiver_timeout` fire when keepalive replies stop, terminating the connection
- s2: the replica reconnects and replays the gap, then loses the link again — repeatedly
- D: `replay_age` on the replica grows during each gap and `pg_wal/` accumulates on the primary while the slot holds WAL
**Indicators:**
- s1: [Step 3] `status` not equal to `'streaming'`, or `since_last_message` greater than `wal_receiver_status_interval`
  <!-- match: {"step": 3, "predicate": "contains", "target": "status"} -->
- root: [Step 8] `iperf3` bitrate close to or below WAL generation rate, or non-zero TCP retransmits
- s2: [Symptom] replica log contains `terminating walreceiver due to timeout` or `could not receive data from WAL stream`
**Interventions:**
- **remediation** (root): identify and remove the network instability; raise idle-timeouts on middleboxes and cut WAL volume so a lossy link tolerates more loss.

  ```bash
  # Identify and remove the network instability.
  # 1. Confirm bandwidth headroom (must exceed peak WAL bytes/sec from Step 6):
  iperf3 -c <primary-host> -t 30 -p 5201
  # 2. Check the path for loss:
  mtr -rwzc 100 <primary-host>
  # 3. If the path is fine but disconnects persist, audit any stateful middleboxes
  #    (NAT gateway, firewall, AWS NLB) for idle-timeout under 600s and raise it,
  #    or set the PostgreSQL TCP keepalives below to keep the flow alive.
  ```

  **Verification:** Step 3 shows `status = 'streaming'` with `since_last_message` under 10 s for one hour; the replica log stops emitting `walreceiver` reconnect lines; Step 1 `send_backlog_bytes` returns to baseline.
- **defensive_fix** (s1): extend timeouts and enable TCP keepalives so short network blips no longer drop the stream.

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

  **Verification:** Step 3 `status` stays `'streaming'` through short blips that previously caused disconnects; no new `terminating walreceiver due to timeout` lines.

### Cause D: Replay paused by recovery conflicts with long replica queries
**Statement:** Long-running read queries on the standby conflict with VACUUM cleanup records or buffer pins in the incoming WAL, so replay pauses up to `max_standby_streaming_delay` before the WAL applies (cancelling the query) or replay continues to lag.
**Chain:**
- root: long-running read queries on the standby pin row versions / buffers that incoming cleanup WAL needs to apply
- s1: replay stalls up to `max_standby_streaming_delay` (default 30 s) per conflict before the query is cancelled
- s2: rapidly arriving conflicts push replay back faster than it can recover, even with healthy network and disk
- D: replay backlog grows and `replay_lag` exceeds the SLO
**Indicators:**
- root: [Step 5] `confl_snapshot` or `confl_bufferpin` increasing across samples
  <!-- match: {"step": 5, "predicate": "contains", "target": "confl_snapshot"} -->
- s2: [Step 1] `replay_backlog_bytes` grows while `flush_backlog_bytes` stays near 0
- s1: [Step 7] recovery process CPU is low (it is sleeping on the conflict, not working)
**Interventions:**
- **remediation** (root): enable `hot_standby_feedback` so the primary retains row versions active replica queries still need, paired with a bounded reader `statement_timeout`.

  ```sql
  -- On the replica: tell the primary to retain row versions for active replica queries.
  ALTER SYSTEM SET hot_standby_feedback = on;
  SELECT pg_reload_conf();
  ```

  **Verification:** Step 5 conflict counters stop increasing across two consecutive 10-minute samples; Step 1 `replay_lag` returns under SLO; primary `pg_stat_user_tables.n_dead_tup` for hot tables is monitored and not growing without bound.
- **mitigation** (root): cancel the old read queries that are blocking replay to release the conflict immediately.

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

  **Risk:** Medium — cancelling returns errors to the originating client. **Duration:** Immediate; replay resumes within seconds of the conflicting query terminating. **Verification:** Step 5 conflict counters stop rising and Step 1 `replay_backlog_bytes` starts shrinking after the cancel.

### Cause E: Primary WAL generation rate exceeds replica apply ceiling (write-spike or bulk load)
**Statement:** A bulk write workload on the primary — `COPY`, large `UPDATE`/`DELETE`, `CREATE INDEX`, partition swap — produces WAL faster than the replica can replay even with healthy hardware, and the lag stays elevated until the workload finishes.
**Chain:**
- root: a bulk operation on the primary generates WAL an order of magnitude above OLTP steady state
- s1: many parallel primary backends outpace the replica's single-threaded replay, which cannot match the rate
- s2: replay backlog rises during the operation and only decays exponentially once it ends
- D: `replay_lag` stays elevated and risks pushing the slot past `max_slot_wal_keep_size` invalidation
**Indicators:**
- root: [Step 6] primary `bytes_per_sec` step-changes well above its OLTP baseline
  <!-- match: {"step": 6, "predicate": "threshold", "target": "primary_wal_bytes_per_sec", "op": ">", "value": 10000000} -->
- s2: [Step 1] `replay_backlog_bytes` rises sharply, correlated with a known migration or import on the primary
- s1: [Step 7] replica recovery process is busy but bounded; `%iowait` is moderate, not pinned
**Interventions:**
- **remediation** (root): re-shape the bulk workload so its WAL rate stays under the replica's apply ceiling, and cut WAL volume.

  ```sql
  -- Reduce WAL volume on bulk-heavy workloads (PostgreSQL 15+: lz4 / zstd).
  ALTER SYSTEM SET wal_compression = 'lz4';
  SELECT pg_reload_conf();
  ```

  Chunk large `UPDATE`/`DELETE` into batches with `LIMIT` + sleep, use `CREATE INDEX CONCURRENTLY`, prefer partitioned `ATTACH PARTITION` swaps over `INSERT … SELECT`, and where OLTP and bulk are inherently mismatched, run bulk on a dedicated logical-replication target rather than streaming to OLTP replicas.

  **Verification:** Step 6 `bytes_per_sec` stabilises near baseline once bulk completes; Step 1 `replay_lag` falls below SLO within the expected decay window; future bulk jobs show smaller peak `replay_lag` after chunking.
- **mitigation** (s1): pause or cancel the in-flight bulk job if it is producing WAL faster than the replica can apply.

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

  **Risk:** Medium — aborting may leave partial work that must be cleaned up. **Duration:** Replay backlog should start shrinking within minutes of the bulk job ending; resume the workload in smaller batches. **Verification:** Step 1 `replay_backlog_bytes` shrinks within minutes of the job stopping.

### Cause F: Synchronous replication blocks commits when a named standby is slow
**Statement:** `synchronous_standby_names` lists a replica that is lagging or stalled, so primary backends block on `COMMIT` waiting for `flush_lsn` or `replay_lsn` from that standby, surfacing as primary commit latency rather than replica freshness lag.
**Chain:**
- root: a named synchronous standby in `synchronous_standby_names` is lagging or stalled
- s1: under `synchronous_commit = on`/`remote_apply` the primary blocks each commit until that standby acknowledges
- s2: backends accumulate waiting on `SyncRep`; primary p95 commit latency tracks the slow replica's lag
- D: application commit latency rises in lockstep even though asynchronous replicas are healthy
**Indicators:**
- root: [Step 1] `sync_state` is `'sync'` or `'quorum'` on the lagging replica
  <!-- match: {"step": 1, "predicate": "contains", "target": "sync"} -->
- s1: [Step 1] `flush_lag` or `replay_lag` on the synchronous standby is well above SLO
- s2: [Symptom] primary commit latency p95 rises in lockstep with replica `flush_lag`
**Interventions:**
- **remediation** (root): change the sync topology to quorum so one weak replica cannot stall commits, or repair the slow standby via its matching Cause A–E path.

  ```sql
  -- Use quorum-based synchronous replication so any N-of-M ack the commit;
  -- one slow standby no longer blocks the primary.
  ALTER SYSTEM SET synchronous_standby_names = 'ANY 1 (s1, s2, s3)';
  SELECT pg_reload_conf();
  ```

  If commit RPO requirements allow it, set `synchronous_commit = remote_write` instead of `on`/`remote_apply`: backends wait only for OS-buffered receipt, removing the apply-lag contribution to commit latency.

  **Verification:** Primary commit latency p95 returns to baseline; Step 1 shows `sync_state = 'sync'` (or `'quorum'`) on the intended standby(s) with `flush_lag` under SLO; no application requests time out on commit.
- **mitigation** (s1): temporarily downgrade to local-only durability to unblock the primary while the standby catches up.

  ```sql
  -- Temporarily downgrade to local-only durability to unblock the primary.
  -- Do this only with explicit operator approval; you are accepting RPO > 0.
  ALTER SYSTEM SET synchronous_commit = local;
  SELECT pg_reload_conf();
  ```

  **Risk:** High — weakens durability; RPO for affected commits drops to local-fsync only. Document the change and revert as soon as the standby catches up. **Duration:** Until the synchronous standby catches up (Step 1 `replay_lag` near 0); restore before declaring the incident resolved. **Verification:** Primary commit latency p95 drops immediately; restore `synchronous_commit = on` and confirm Step 1 `flush_lag` is under SLO before closing.

### Cause Z: Unidentified
**Statement:** The diagnostic steps do not converge on a single cause above, or the evidence is conflicting, so a confident root cause cannot be assigned to the observed replication lag.
**Chain:**
- root: the observed lag does not match any signal from backlog distribution, slot retention, conflicts, WAL rate, or sync-rep state
- D: replication lag persists with no confidently identified driver
**Indicators:**
- root: [Default] no Cause A–F indicator set is satisfied, or signals conflict (e.g. cascading replication, logical-replication conflicts, timeline divergence, kernel packet drops, or a managed-service throttle)
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot of primary and replica state, then escalate to the database SRE/DBA on call.

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

  **Risk:** Diagnostic only — does not change system state. **Duration:** Diagnostic only — does not change system state. **Verification:** Escalation acknowledged with snapshots attached; incident review opens a ticket to extend this runbook with the new failure mode.

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
- [PostgreSQL Documentation: Replication Configuration Parameters](https://www.postgresql.org/docs/current/runtime-config-replication.html) — Priority 1. Authoritative for `wal_keep_size`, `max_slot_wal_keep_size`, `wal_sender_timeout`, `wal_receiver_timeout`, `wal_receiver_status_interval`, `synchronous_standby_names`, `synchronous_commit`, and `idle_replication_slot_timeout` defaults.
- [PostgreSQL Documentation: Write Ahead Log Parameters](https://www.postgresql.org/docs/current/runtime-config-wal.html) — Priority 1. Authoritative for `wal_compression` values (`off`, `pglz`, `lz4`, `zstd`), `wal_level`, `max_wal_size`, `min_wal_size`, and `checkpoint_timeout` (Causes C and E).
