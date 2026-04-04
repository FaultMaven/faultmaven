---
id: mysql-replication-broken
title: "MySQL Replication Broken: Replica Stops Replicating"
domain: database
service: mysql
symptom_class:
  - replication_lag
  - service_unavailable
severity: critical
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - mysql
  - replication
  - gtid
  - relay-log
  - failover
difficulty: advanced
---

# MySQL Replication Broken: Replica Stops Replicating

## Problem Definition

This runbook covers MySQL 8.0+ deployments using asynchronous or semi-synchronous replication where the replica has stopped replicating from the source. It applies to self-managed instances, Amazon RDS for MySQL, Aurora MySQL read replicas, and Google Cloud SQL for MySQL. You need a MySQL client with `REPLICATION CLIENT` and `SUPER` (or `REPLICATION_SLAVE_ADMIN`) privileges on the replica, the ability to connect to the source server, and access to the MySQL error log on both servers. For GTID-based replication, you need the ability to execute `SET GTID_NEXT`.

MySQL replication breaks when the replica stops applying changes from the source. This manifests as one or both replication threads stopping: the IO thread (receiver thread, which fetches binary log events from the source) and the SQL thread (applier thread, which applies those events to the replica). When replication is broken, the replica falls behind or serves stale data, and failover to the replica is unsafe.

**Common symptoms:**

- `SHOW REPLICA STATUS` shows `Replica_IO_Running: No` or `Replica_SQL_Running: No`
- `Seconds_Behind_Master` is increasing, extremely large, or NULL (NULL means not replicating at all)
- Application reads from the replica return stale or inconsistent data
- Alerts on replication lag exceeding thresholds
- Error messages in the MySQL error log referencing relay log corruption, GTID gaps, or SQL apply errors
- The source's `SHOW PROCESSLIST` shows no `Binlog Dump` thread for the replica
- Error 1236: `The slave is connecting using CHANGE MASTER TO MASTER_AUTO_POSITION = 1, but the master has purged binary logs containing GTIDs that the slave requires`

**Common root causes:**

- Duplicate key errors (error 1062) on the replica from data drift or errant transactions
- Missing row errors (error 1032) on the replica from data deleted directly on the replica
- GTID gaps caused by errant transactions on the replica or purged binary logs on the source
- Relay log corruption after unclean replica shutdown or disk failure
- Network interruption between source and replica causing IO thread disconnect
- Schema changes (DDL) applied out of order or incompatibly
- Disk full on replica preventing relay log writes
- `max_allowed_packet` mismatch between source and replica causing large events to fail
- Binary log format incompatibility (STATEMENT vs ROW vs MIXED) between source and replica
- Multithreaded replication (MTS) with GTID gaps when `slave_preserve_commit_order` is off

## Diagnostic Steps

### Step 1: Check replication status

**What this checks:** The complete state of both replication threads, including any error messages, GTID positions, and replication lag.

```bash
mysql -e "SHOW REPLICA STATUS\G"
```

**Expected output:** A single-row result with approximately 60 fields describing the replication state.

**What the finding means:** Key fields to examine:

- `Replica_IO_Running` -- should be `Yes`. If `No`, the IO thread cannot fetch binary logs from the source (network, auth, or binary log issue). If `Connecting`, the replica is attempting to reconnect.
- `Replica_SQL_Running` -- should be `Yes`. If `No`, the SQL thread cannot apply events (data conflict, schema mismatch, or corruption).
- `Last_IO_Error` / `Last_SQL_Error` -- the specific error message explaining why the thread stopped.
- `Last_IO_Errno` / `Last_SQL_Errno` -- numeric error code (1062 = duplicate key, 1032 = missing row, 1236 = binary log position error).
- `Seconds_Behind_Master` -- replication lag in seconds. `NULL` means the replica is not replicating at all.
- `Retrieved_Gtid_Set` / `Executed_Gtid_Set` -- GTID positions showing what has been fetched versus applied.
- `Auto_Position` -- `1` if using GTID auto-positioning, `0` if using file+position.

### Step 2: Check the MySQL error log on the replica

**What this checks:** Detailed error messages and stack traces that provide more context than `SHOW REPLICA STATUS` alone.

```bash
tail -100 /var/log/mysql/error.log
```

**Expected output:** Log entries with timestamps and error details related to replication threads.

**What the finding means:** Look for entries containing `Slave SQL`, `Slave I/O`, `Replica`, `relay log`, `GTID`, or specific error codes. Repeated errors on the same event indicate a persistent data conflict. Entries mentioning `relay log read failure` or relay log corruption indicate the relay log files are damaged and need to be reset. Entries about `Got fatal error 1236` indicate binary log position issues.

### Step 3: Verify source server health and binary log availability

**What this checks:** Whether the source is healthy, actively generating binary logs, and whether the binary logs the replica needs still exist on the source.

```bash
mysql -h SOURCE_HOST -e "SHOW MASTER STATUS\G"
mysql -h SOURCE_HOST -e "SHOW BINARY LOGS;"
```

**Expected output:** The current binary log file and position on the source, plus a list of all available binary log files with sizes. A nonzero `Position` confirms binary logging is enabled.

**What the finding means:** If the binary log file the replica needs (from `SHOW REPLICA STATUS` -> `Master_Log_File`) is no longer in the `SHOW BINARY LOGS` output, the source has purged the needed logs and the replica cannot catch up without a rebuild. If the source shows no binary logs, `log_bin` may be disabled or `--skip-log-bin` was used.

### Step 4: Check GTID consistency (GTID-based replication)

**What this checks:** Whether there are GTID gaps between the source and replica, and whether errant transactions exist on the replica.

```bash
mysql -e "SELECT @@GLOBAL.gtid_executed AS replica_executed;"
mysql -e "SELECT @@GLOBAL.gtid_purged AS replica_purged;"
mysql -h SOURCE_HOST -e "SELECT @@GLOBAL.gtid_executed AS source_executed;"
```

**Expected output:** GTID sets for both servers showing UUID:interval pairs.

**What the finding means:** The replica's `gtid_executed` should be a subset of the source's `gtid_executed`. If the replica has GTIDs with a UUID that does not match the source's server UUID, those are errant transactions (writes made directly to the replica) -- this is the most common cause of GTID-related replication breaks. If the source's `gtid_purged` contains GTIDs not in the replica's `gtid_executed`, the replica has a gap it cannot fill from the source's available binary logs.

### Step 5: Check server IDs and verify network connectivity

**What this checks:** Whether both servers have unique IDs and whether the replica can reach the source on the MySQL port with the replication user credentials.

```bash
mysql -e "SELECT @@server_id AS replica_server_id;"
mysql -h SOURCE_HOST -e "SELECT @@server_id AS source_server_id;"
mysql -h SOURCE_HOST -u repl_user -p -e "SELECT 1;"
```

**Expected output:** Two different `server_id` values (both non-zero), and a successful connection test returning `1`.

**What the finding means:** If `server_id` values are identical or zero, replication cannot function. If the connection test fails, investigate network issues: firewall rules blocking port 3306, DNS resolution failures, security group rules (in cloud environments), or the replication user lacking `REPLICATION SLAVE` privilege.

### Step 6: Check disk space on the replica

**What this checks:** Whether the replica has sufficient disk space for relay logs and data files.

```bash
df -h /var/lib/mysql
du -sh /var/lib/mysql/relay-log*
```

**Expected output:** Disk usage and available space for the MySQL data directory, plus the total size of relay log files.

**What the finding means:** If the filesystem is at 100%, the IO thread cannot write new relay log events, causing it to stop. Relay logs accumulate when the SQL thread is stopped or slow, so a stopped SQL thread can lead to disk exhaustion which then also stops the IO thread.

### Step 7: Check Performance Schema replication tables

**What this checks:** Thread-level diagnostics for multithreaded replication, showing per-worker status and errors.

```bash
mysql -e "
SELECT CHANNEL_NAME, SERVICE_STATE, LAST_ERROR_NUMBER, LAST_ERROR_MESSAGE
FROM performance_schema.replication_connection_status;
"
mysql -e "
SELECT CHANNEL_NAME, WORKER_ID, SERVICE_STATE, LAST_ERROR_NUMBER, LAST_ERROR_MESSAGE
FROM performance_schema.replication_applier_status_by_worker
WHERE LAST_ERROR_NUMBER != 0;
"
```

**Expected output:** Connection status showing SERVICE_STATE and any worker-level errors for multithreaded replication.

**What the finding means:** For multithreaded replication (MTS), individual workers can fail while others continue. The `replication_applier_status_by_worker` table shows which specific worker failed and on which transaction, providing more detail than `SHOW REPLICA STATUS` alone.

### Step 8: Examine processlist on both servers

**What this checks:** Whether the replication threads are present on the replica and the binlog dump thread is active on the source.

```bash
mysql -e "SHOW PROCESSLIST\G" | grep -A5 -i "system user"
mysql -h SOURCE_HOST -e "SHOW PROCESSLIST\G" | grep -A5 -i "Binlog Dump"
```

**Expected output:** On the replica, two `system user` threads (IO and SQL). On the source, a `Binlog Dump` thread for each connected replica.

**What the finding means:** Missing `system user` threads on the replica confirm replication is stopped. A missing `Binlog Dump` thread on the source for this replica confirms the IO thread has disconnected. Multiple `Binlog Dump` threads for the same replica indicate a stale connection that should be killed on the source.

## Mitigation

### Option 1: Restart replication threads

Use when the IO thread disconnected due to a transient network issue and there are no SQL errors.

- **Risk:** Low. This simply restarts the connection to the source and resumes applying events from where it left off.
- **Command:**

```bash
mysql -e "STOP REPLICA; START REPLICA;"
```

- **Verify:**

```bash
mysql -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running|Seconds_Behind"
# Expected: both Running: Yes, Seconds_Behind_Master decreasing toward 0
```

- **Duration:** Seconds. The replica resumes fetching and applying events immediately.

### Option 2: Skip a single problematic event (SQL thread error)

Use when the SQL thread stopped on a single non-critical error (for example, a duplicate key error 1062 on a non-critical table where data drift is acceptable).

- **Risk:** High. Skipping events causes data drift between source and replica. Every skipped event is a row that differs between the two servers. Only use for known-safe errors after confirming the data impact is acceptable. For statements using `AUTO_INCREMENT` or `LAST_INSERT_ID()`, set the skip counter to 2 (these generate 2 binary log events).
- **Command:**

For file+position replication:

```bash
mysql -e "STOP REPLICA; SET GLOBAL sql_slave_skip_counter = 1; START REPLICA;"
```

For GTID-based replication (inject an empty transaction to fill the gap):

```bash
mysql -e "
SET GTID_NEXT='<source_uuid>:<next_gtid>';
BEGIN; COMMIT;
SET GTID_NEXT='AUTOMATIC';
START REPLICA;
"
```

- **Verify:**

```bash
mysql -e "SHOW REPLICA STATUS\G" | grep -E "Running|Error|Behind"
# Expected: both Running: Yes, no errors, Seconds_Behind_Master decreasing
```

- **Duration:** Immediate.

### Option 3: Fix relay log corruption

Use when the error log indicates relay log read errors or the SQL thread reports it cannot read the relay log.

- **Risk:** Moderate. `RESET REPLICA` deletes all relay log files and re-fetches from the source based on the last successfully applied position. If the source has already purged the needed binary logs, this will fail and a full rebuild is required.
- **Command:**

```bash
mysql -e "
STOP REPLICA;
RESET REPLICA;
START REPLICA;
"
```

- **Verify:**

```bash
mysql -e "SHOW REPLICA STATUS\G" | grep -E "Running|Error|Relay_Log"
# Expected: both Running: Yes, no errors, new relay log file
```

- **Duration:** Minutes. The IO thread re-fetches events from the source starting at the last applied position.

### Option 4: Rebuild replica from a fresh backup

Use when replication is unrecoverably broken: large GTID gaps, many skipped events causing unacceptable data drift, or the source has purged the binary logs the replica needs (error 1236).

- **Risk:** High. The replica is unavailable during the rebuild. All read traffic routed to this replica must be redirected to another replica or the source. For large databases, this can take hours.
- **Command:**

```bash
# On source: take a consistent backup with GTID information
mysqldump --all-databases --single-transaction --source-data=2 \
  --set-gtid-purged=ON --routines --triggers > /tmp/full_dump.sql

# On replica: stop replication and reset completely
mysql -e "STOP REPLICA; RESET REPLICA ALL;"

# Restore the backup on the replica
mysql < /tmp/full_dump.sql

# Configure replication using GTID auto-positioning and start
mysql -e "
CHANGE REPLICATION SOURCE TO
  SOURCE_HOST='<source_host>',
  SOURCE_USER='repl_user',
  SOURCE_PASSWORD='<repl_password>',
  SOURCE_AUTO_POSITION=1;
START REPLICA;
"
```

For large databases, consider using `xtrabackup` (Percona) or `mysqlsh` `dumpInstance` instead of `mysqldump` for faster backup and restore.

- **Verify:**

```bash
mysql -e "SHOW REPLICA STATUS\G" | grep -E "Running|Error|Behind"
# Expected: both Running: Yes, Seconds_Behind_Master decreasing toward 0
```

- **Duration:** Minutes to hours depending on database size and network speed between servers.

### Option 5: Resolve errant transactions on the replica

Use when GTID comparison shows the replica has transactions not present on the source (errant GTIDs with the replica's own UUID).

- **Risk:** Moderate. Injecting empty transactions on the source ensures all replicas converge, but does not undo the effect of the errant transaction on the affected replica.
- **Command:**

```bash
# Identify errant GTIDs (replica UUID transactions not on source)
# Compare replica gtid_executed with source gtid_executed

# Option A: Inject empty transactions on source to cover the errant GTIDs
mysql -h SOURCE_HOST -e "
SET GTID_NEXT='<replica_uuid>:<errant_gtid>';
BEGIN; COMMIT;
SET GTID_NEXT='AUTOMATIC';
"

# Option B: If too many errant transactions, rebuild the replica (see Option 4)
```

- **Verify:**

```bash
mysql -e "SHOW REPLICA STATUS\G" | grep -E "Running|Error"
# Expected: both Running: Yes, no errors
```

- **Duration:** Seconds for empty transaction injection. Hours if rebuild is needed.

## Root Cause Resolution

**If** the error is a duplicate key (error 1062) --> a row was inserted directly on the replica that conflicts with a source event. Skip the event, then investigate and prevent direct writes. Run `pt-table-checksum` and `pt-table-sync` from Percona Toolkit to reconcile data differences. Set `super_read_only=ON` on the replica.

**If** the error is a missing row (error 1032) --> a row was deleted or updated on the replica but not the source. Skip the event and reconcile with `pt-table-sync`. Set `super_read_only=ON` on the replica to prevent future direct writes.

**If** GTID gaps exist because the source purged binary logs the replica needs (error 1236) --> rebuild the replica from a fresh backup with `--set-gtid-purged=ON`. Increase `binlog_expire_logs_seconds` (or `expire_logs_days` on older versions) on the source to retain binary logs longer. Alternatively, extract missing transactions from another up-to-date replica using `mysqlbinlog --exclude-gtids`.

**If** GTID gaps exist due to errant transactions on the replica --> identify errant GTIDs by comparing `gtid_executed` sets. Inject empty transactions on the source to fill the gap so all replicas converge, or reset the affected replica entirely.

**If** relay log corruption occurred after a crash --> use `RESET REPLICA` to discard corrupt relay logs and re-fetch from the source. Set `sync_relay_log=1` in `my.cnf` to prevent future corruption (at a performance cost). For multithreaded replication, also set `relay_log_recovery=ON` for automatic recovery on restart.

**If** `max_allowed_packet` mismatch causes large-event failures --> set the replica's `max_allowed_packet` to be equal to or greater than the source value in `my.cnf`, then restart MySQL on the replica.

**If** disk full on the replica prevents relay log writes --> free disk space by purging old relay logs, removing old backups, or expanding the volume. Then restart the IO thread with `START REPLICA IO_THREAD`. Configure `relay_log_space_limit` to prevent future runaway relay log growth.

**If** a network partition caused the IO thread to disconnect --> verify network connectivity, check firewall rules and security groups, and restart the IO thread. For unstable networks, increase `replica_net_timeout` (default 60 seconds) to tolerate brief interruptions.

**If** multithreaded replication has GTID gaps --> set `slave_preserve_commit_order=1` which requires `slave_parallel_type=LOGICAL_CLOCK` and that `log-bin` and `log-slave-updates` are enabled. This prevents gaps from forming during MTS.

## Verification

After applying any fix, confirm replication is healthy:

```bash
# 1. Both replication threads are running
mysql -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running"
# Expected: both "Yes"

# 2. No replication errors
mysql -e "SHOW REPLICA STATUS\G" | grep -E "Last_IO_Error|Last_SQL_Error"
# Expected: empty strings (no errors)

# 3. Replication lag is decreasing toward zero
watch -n 5 'mysql -e "SHOW REPLICA STATUS\G" | grep Seconds_Behind_Master'
# Expected: value decreasing toward 0

# 4. GTID sets are converging (GTID replication only)
mysql -e "SELECT @@GLOBAL.gtid_executed;"
mysql -h SOURCE_HOST -e "SELECT @@GLOBAL.gtid_executed;"
# Expected: replica gtid_executed is approaching source gtid_executed

# 5. No errant transactions on the replica
mysql -e "
SELECT GTID_SUBTRACT(@@GLOBAL.gtid_executed, (SELECT @@GLOBAL.gtid_executed FROM performance_schema.replication_connection_status WHERE CHANNEL_NAME='')) AS errant_gtids;
"
# Or compare manually: replica GTIDs with replica UUID should not exist

# 6. Data consistency check (run after lag reaches 0)
pt-table-checksum --replicate=percona.checksums --host=SOURCE_HOST
# Expected: no differences reported
```

Monitor for at least 30 minutes to confirm the replica stays in sync and no new errors appear. For rebuilt replicas, monitor for 24 hours.

## Prevention

1. **Enable GTID-based replication** -- GTIDs provide automatic positioning and simplify failover. Set `gtid_mode=ON` and `enforce_gtid_consistency=ON` on all servers in the topology.

2. **Set `super_read_only=ON` on all replicas** -- Prevents accidental direct writes to replicas that cause data drift. This is the single most effective prevention measure.

    ```bash
    mysql -e "SET GLOBAL read_only = ON; SET GLOBAL super_read_only = ON;"
    ```

    Persist in `my.cnf`:

    ```ini
    [mysqld]
    read_only = ON
    super_read_only = ON
    ```

3. **Monitor replication continuously** -- Alert on `Seconds_Behind_Master > threshold` (for example, 30 seconds), IO/SQL thread status changes, and error counts. Use Prometheus with `mysqld_exporter` or Percona `pt-heartbeat` for automated monitoring.

4. **Match `max_allowed_packet` across all servers** -- Ensure source and all replicas have identical `max_allowed_packet` settings to prevent large-event apply failures.

5. **Configure relay log durability** -- Set `sync_relay_log=1` to flush relay logs to disk on every write, preventing corruption after unclean shutdown. Use `sync_relay_log=100` as a compromise. For MTS, set `relay_log_recovery=ON`.

6. **Use semi-synchronous replication** -- Ensures at least one replica acknowledges each transaction before the source commits, reducing the window for data loss during failover.

7. **Run periodic consistency checks** -- Schedule `pt-table-checksum` weekly to detect silent data drift before it causes replication breaks. Fix any differences with `pt-table-sync`.

8. **Ensure adequate disk space on replicas** -- Monitor disk usage on replicas and alert at 80% capacity. Configure `relay_log_space_limit` to cap relay log disk usage and prevent runaway growth when the SQL thread is slow.

9. **Retain binary logs on the source** -- Set `binlog_expire_logs_seconds` to retain binary logs for at least 7 days (604800 seconds), giving replicas time to catch up after extended outages.

10. **Keep MySQL versions compatible** -- The replica can run the same or a newer minor version than the source, but never an older one. Test version upgrades on replicas before upgrading the source.

11. **Configure multithreaded replication safely** -- When using MTS, set `slave_preserve_commit_order=1` with `slave_parallel_type=LOGICAL_CLOCK` to prevent GTID gaps. Enable `log-bin` and `log-slave-updates` on the replica.

## Sources

- [MySQL 8.0 Reference Manual -- Replication Troubleshooting](https://dev.mysql.com/doc/refman/8.0/en/replication-problems.html)
- [MySQL 8.0 Reference Manual -- SHOW REPLICA STATUS](https://dev.mysql.com/doc/refman/8.0/en/show-replica-status.html)
- [MySQL 8.0 Reference Manual -- GTID Failover and Scaleout](https://dev.mysql.com/doc/refman/8.0/en/replication-gtids-failover.html)
- [MySQL 8.0 Reference Manual -- Replication and Binary Logging Options](https://dev.mysql.com/doc/refman/8.0/en/replication-options.html)
- [MySQL 5.7 Reference Manual -- Handling an Unexpected Halt of a Replica](https://dev.mysql.com/doc/refman/5.7/en/replication-solutions-unexpected-replica-halt.html)
- [Severalnines -- Troubleshooting MySQL Replication](https://severalnines.com/blog/mysql-tutorial-troubleshooting-mysql-replication-part-1/)
- [Percona Toolkit -- pt-table-checksum](https://docs.percona.com/percona-toolkit/pt-table-checksum.html)
- [Percona Toolkit -- pt-table-sync](https://docs.percona.com/percona-toolkit/pt-table-sync.html)
