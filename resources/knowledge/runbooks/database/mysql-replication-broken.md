---
id: mysql-replication-broken
title: "MySQL Replication Broken"
domain: database
service: mysql
symptom_class:
  - replication_lag
  - service_unavailable
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags:
  - mysql
  - replication
  - gtid
  - relay-log
  - innodb
difficulty: advanced
---

## Symptom Recognition

One or both replication threads on the replica stop:

```text
Replica_IO_Running: No
Replica_SQL_Running: No
```

`Seconds_Behind_Master` returns `NULL` (replica has stopped replicating entirely) or a value that is growing without bound rather than catching up. Application reads from the replica return stale or inconsistent data, and alerts on replication lag fire. Additional indicators in the MySQL error log include:

```text
Got fatal error 1236 from source: 'The slave is connecting using CHANGE MASTER TO
MASTER_AUTO_POSITION = 1, but the master has purged binary logs containing GTIDs
that the slave requires.'
```

```text
Error 'Duplicate entry '123' for key 'PRIMARY'' on query.
```

```text
relay log read failure: Could not parse relay log event entry.
```

The source's `SHOW PROCESSLIST` shows no `Binlog Dump` thread for the replica when the IO thread is down.

## Applicability

- MySQL 8.0+ on self-managed instances, Amazon RDS for MySQL, Aurora MySQL read replicas, and Google Cloud SQL for MySQL.
- `REPLICATION CLIENT` and `SUPER` (or `REPLICATION_SLAVE_ADMIN`) privileges on the replica.
- Ability to connect to the source server with `REPLICATION CLIENT` privilege.
- Read access to the MySQL error log on the replica (typically `/var/log/mysql/error.log` or `/var/log/mysqld.log`).
- For GTID gap injection: ability to execute `SET GTID_NEXT` on the source.
- Tools: MySQL client (`mysql`), optional Percona Toolkit (`pt-table-checksum`, `pt-table-sync`).

## Diagnostic Steps

### Step 1: Check replication thread status

```bash
mysql -e "SHOW REPLICA STATUS\G"
```

Expected output: a single-row result with approximately 60 fields. Key fields: `Replica_IO_Running` and `Replica_SQL_Running` (both should be `Yes`); `Last_IO_Errno` / `Last_IO_Error` and `Last_SQL_Errno` / `Last_SQL_Error` (should be 0 and empty); `Seconds_Behind_Master` (0 under steady state); `Auto_Position` (1 if GTID-based); `Retrieved_Gtid_Set` and `Executed_Gtid_Set` (replica's GTID state).

### Step 2: Read the replica error log

```bash
tail -200 /var/log/mysql/error.log | grep -E "Slave|Replica|relay|GTID|error"
```

Expected output: timestamped log entries containing `Slave SQL`, `Slave I/O`, `Replica`, `relay log`, or specific error codes. Repeated errors on the same GTID indicate a persistent data conflict. Entries mentioning `relay log read failure` or `relay log corruption` indicate relay log files are damaged.

### Step 3: Verify source binary logs are available

```bash
mysql -h SOURCE_HOST -e "SHOW BINARY LOGS;"
mysql -h SOURCE_HOST -e "SHOW MASTER STATUS\G"
```

Expected output: a list of binary log files and sizes, plus the current log file and position. The binary log file the replica needs (from `SHOW REPLICA STATUS` field `Master_Log_File`) must appear in this list. A nonzero `Position` confirms binary logging is active.

### Step 4: Identify GTID gaps and errant transactions

```bash
mysql -e "SELECT @@GLOBAL.gtid_executed AS replica_gtid_executed;"
mysql -h SOURCE_HOST -e "SELECT @@GLOBAL.gtid_executed AS source_gtid_executed;"
mysql -h SOURCE_HOST -e "SELECT @@GLOBAL.gtid_purged AS source_gtid_purged;"
```

Expected output: GTID sets for both servers in UUID:interval notation. The replica's `gtid_executed` should be a subset of the source's `gtid_executed`. Any GTID in the replica's set with the replica's own server UUID (not the source's UUID) is an errant transaction — a write made directly to the replica.

### Step 5: Confirm unique server IDs and network connectivity

```bash
mysql -e "SELECT @@server_id AS replica_server_id;"
mysql -h SOURCE_HOST -e "SELECT @@server_id AS source_server_id;"
mysql -h SOURCE_HOST -u repl_user -p -e "SELECT 1;"
```

Expected output: two distinct non-zero `server_id` values, and a successful `SELECT 1` returning `1`. Identical or zero server IDs prevent replication from functioning. A failed connection test means a network, firewall, or privilege issue with the replication user.

### Step 6: Check disk space on the replica

```bash
df -h /var/lib/mysql
du -sh /var/lib/mysql/relay-log*
```

Expected output: filesystem usage well below 100 % on the MySQL data directory, and a relay log footprint proportional to current replication lag. A filesystem at 100 % prevents the IO thread from writing new relay log events.

### Step 7: Examine Performance Schema for worker errors (MTS)

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

Expected output: `SERVICE_STATE = ON` for the connection, and zero rows from the worker query under healthy operation. For multithreaded replication (MTS), this shows which specific worker failed and which transaction it stopped on — more precise than `SHOW REPLICA STATUS` alone.

## Causes

### Cause A: Duplicate key error on replica (data drift)

**Statement:** A row was inserted directly on the replica that conflicts with an incoming replication event, causing the SQL thread to stop with error 1062.
**Mechanism:** Direct writes to the replica (without `super_read_only`) create rows that already exist on the source. When the source later replicates the original INSERT or UPDATE for those rows, InnoDB finds a primary key or unique key conflict. The SQL thread stops and reports error 1062. Every subsequent event from the source is blocked until the conflict is resolved or skipped.

**Indicator:**

- [Step 1] `Last_SQL_Errno: 1062` and `Last_SQL_Error` contains `Duplicate entry`
- [Step 1] `Replica_SQL_Running: No`, `Replica_IO_Running: Yes`

<!-- match: {"step": 1, "predicate": "contains", "target": "Duplicate entry"} -->

**Mitigation:**

- **Risk:** High. Skipping events causes permanent data drift between source and replica. Only skip if you can confirm the data impact is acceptable (e.g., the conflicting row is identical on both sides).
- **Command:**

  ```bash
  mysql -e "
  STOP REPLICA;
  SET GLOBAL sql_replica_skip_counter = 1;
  START REPLICA;
  "
  ```

- **Duration:** Immediate. Monitor `SHOW REPLICA STATUS` for additional errors on subsequent events.

**Resolution:**

```bash
# Set super_read_only to prevent future direct writes
mysql -e "SET GLOBAL super_read_only = ON;"
# Reconcile data differences with Percona Toolkit
pt-table-checksum --replicate=percona.checksums --host=SOURCE_HOST
pt-table-sync --replicate=percona.checksums --host=SOURCE_HOST --print
pt-table-sync --replicate=percona.checksums --host=SOURCE_HOST --execute
```

Persist in `my.cnf`:

```ini
[mysqld]
super_read_only = ON
```

**Verification:** After `pt-table-sync`, re-run `pt-table-checksum` and confirm zero diffs. Re-run Step 1 and confirm both threads show `Yes`, `Last_SQL_Errno: 0`, and `Seconds_Behind_Master` approaching 0.

### Cause B: Missing row error on replica (error 1032)

**Statement:** A row was deleted or modified directly on the replica that the source then tries to UPDATE or DELETE via replication, causing the SQL thread to stop with error 1032.
**Mechanism:** Direct deletes or updates on the replica remove rows that the source expects to exist. When the source replicates a DELETE or UPDATE targeting those rows, the replica's SQL thread cannot find the row by primary key, reports error 1032 (`Can't find record in '<table>'`), and stops. The gap persists until the event is skipped or the replica is resynchronized.

**Indicator:**

- [Step 1] `Last_SQL_Errno: 1032` and `Last_SQL_Error` contains `Can't find record`
- [Step 1] `Replica_SQL_Running: No`, `Replica_IO_Running: Yes`

<!-- match: {"step": 1, "predicate": "contains", "target": "Can't find record"} -->

**Mitigation:**

- **Risk:** High. Skipping the event removes the source's intended change from the replica, widening data drift.
- **Command:**

  ```bash
  mysql -e "
  STOP REPLICA;
  SET GLOBAL sql_replica_skip_counter = 1;
  START REPLICA;
  "
  ```

- **Duration:** Immediate.

**Resolution:**

```bash
# Prevent future writes that cause drift
mysql -e "SET GLOBAL super_read_only = ON;"
# Reconcile the affected table
pt-table-checksum --replicate=percona.checksums --tables=<db.table> --host=SOURCE_HOST
pt-table-sync --replicate=percona.checksums --tables=<db.table> --host=SOURCE_HOST --execute
```

**Verification:** Re-run `pt-table-checksum` and confirm zero differences on the affected table. Re-run Step 1 and confirm both threads are `Yes`, no SQL errors remain, and lag is decreasing.

### Cause C: GTID gap — source purged required binary logs (error 1236)

**Statement:** The source has purged binary logs that contain GTIDs the replica has not yet applied, making it impossible for the replica to catch up without a full rebuild.
**Mechanism:** MySQL automatically purges old binary logs based on `binlog_expire_logs_seconds`. If a replica was offline, paused, or lagging for longer than the retention period, the source deletes the logs before the replica can fetch them. When the IO thread reconnects with `SOURCE_AUTO_POSITION=1`, the source detects that the needed GTIDs are gone and returns error 1236. The IO thread stops immediately because there is no binary log to fetch.

**Indicator:**

- [Step 1] `Last_IO_Errno: 1236` and `Last_IO_Error` contains `purged binary logs`
- [Step 3] the binary log file referenced in `Master_Log_File` from Step 1 is absent from `SHOW BINARY LOGS` output on the source

<!-- match: {"step": 1, "predicate": "contains", "target": "purged binary logs"} -->

**Mitigation:**

- **Risk:** High. A full replica rebuild is required. The replica is unavailable during the rebuild. All read traffic must be rerouted to another replica or the source.
- **Command:**

  ```bash
  # On source: take a consistent backup including GTID positions
  mysqldump --all-databases --single-transaction --source-data=2 \
    --set-gtid-purged=ON --routines --triggers > /tmp/full_dump.sql

  # On replica: reset completely
  mysql -e "STOP REPLICA; RESET REPLICA ALL;"

  # Restore
  mysql < /tmp/full_dump.sql

  # Reconfigure and start
  mysql -e "
  CHANGE REPLICATION SOURCE TO
    SOURCE_HOST='<source_host>',
    SOURCE_USER='repl_user',
    SOURCE_PASSWORD='<repl_password>',
    SOURCE_AUTO_POSITION=1;
  START REPLICA;
  "
  ```

- **Duration:** Minutes to hours depending on database size and network throughput. For large databases, use `xtrabackup` or `mysqlsh dumpInstance` instead of `mysqldump`.

**Resolution:** Same as Mitigation. After the rebuild, increase binary log retention on the source to at least 7 days to reduce recurrence:

```sql
SET GLOBAL binlog_expire_logs_seconds = 604800;
```

Persist in `my.cnf`:

```ini
[mysqld]
binlog_expire_logs_seconds = 604800
```

**Verification:** After rebuild, re-run Step 1 and confirm both threads are `Yes`. Re-run Step 4 and confirm replica's `gtid_executed` is approaching source's `gtid_executed`. Monitor `Seconds_Behind_Master` for at least 30 minutes to confirm catch-up.

### Cause D: Errant transactions on the replica (GTID mismatch)

**Statement:** Writes made directly on the replica created GTIDs that are not present on the source, causing GTID set divergence that may break replication on reconnect or during failover.
**Mechanism:** Every transaction committed on the replica (not via replication) generates a GTID tagged with the replica's own server UUID. The source does not have these GTIDs. When a new source is promoted during failover, other replicas reject the errant GTIDs as unknown. Additionally, after a replica restart with `gtid_mode=ON`, the divergent GTID set causes auto-positioning to fail if the source cannot reconcile the gap.

**Indicator:**

- [Step 4] the replica's `gtid_executed` contains GTIDs with the replica's own server UUID (distinct from the source UUID)
- [Step 1] `Auto_Position: 1` and `Replica_IO_Running: No` with no error 1236 in `Last_IO_Error`

**Mitigation:**

- **Risk:** Moderate. Injecting empty transactions on the source covers the errant GTIDs so all replicas converge, but does not undo the data written by the errant transactions on the affected replica.
- **Command:**

  ```bash
  # Identify errant GTID range (replica UUID transactions not present on source)
  # Example errant GTID: <replica_uuid>:1-3
  mysql -h SOURCE_HOST -e "
  SET GTID_NEXT='<replica_uuid>:1';
  BEGIN; COMMIT;
  SET GTID_NEXT='<replica_uuid>:2';
  BEGIN; COMMIT;
  SET GTID_NEXT='<replica_uuid>:3';
  BEGIN; COMMIT;
  SET GTID_NEXT='AUTOMATIC';
  "
  ```

- **Duration:** Seconds for empty transaction injection. If errant GTIDs are numerous, a full rebuild (see Cause C) is faster.

**Resolution:**

```bash
# Prevent future writes to the replica
mysql -e "SET GLOBAL super_read_only = ON;"
```

Persist in `my.cnf`:

```ini
[mysqld]
super_read_only = ON
```

**Verification:** Re-run Step 4 — the replica's `gtid_executed` should now be a strict subset of the source's after empty transactions are applied and replication resumes. Re-run Step 1 and confirm both threads are `Yes`.

### Cause E: Relay log corruption

**Statement:** Relay log files on the replica are damaged after an unclean shutdown or disk failure, preventing the SQL thread from reading and applying events.
**Mechanism:** The SQL thread reads events sequentially from relay log files written by the IO thread. If the relay log is truncated, has a checksum mismatch, or contains an incomplete event block from a crash mid-write, the SQL thread raises a relay log read failure and stops. The IO thread may continue writing new events to a new relay log segment, but the corrupt file blocks the SQL thread from advancing past it.

**Indicator:**

- [Step 2] error log contains `relay log read failure` or `Could not parse relay log event entry`
- [Step 1] `Replica_SQL_Running: No`, `Last_SQL_Error` contains `relay log` or `corrupt`

<!-- match: {"step": 2, "predicate": "contains", "target": "relay log read failure"} -->

**Mitigation:**

- **Risk:** Moderate. `RESET REPLICA` deletes all relay log files and re-fetches from the source based on the last applied GTID. If the source has purged the needed binary logs, this will fail (see Cause C).
- **Command:**

  ```bash
  mysql -e "
  STOP REPLICA;
  RESET REPLICA;
  START REPLICA;
  "
  ```

- **Duration:** Minutes. The IO thread re-fetches events from the source starting at the last applied GTID position.

**Resolution:**

```bash
# Enable relay log durability to prevent future corruption
mysql -e "SET GLOBAL sync_relay_log = 1;"
mysql -e "SET GLOBAL relay_log_recovery = ON;"
```

Persist in `my.cnf`:

```ini
[mysqld]
sync_relay_log = 1
relay_log_recovery = ON
```

**Verification:** Re-run Step 1 — both threads should show `Yes`, and `Last_SQL_Error` should be empty. Re-run Step 2 and confirm no new relay log error entries appear. Monitor for 30 minutes to confirm the SQL thread does not stop again.

### Cause F: Disk full on replica stops IO thread

**Statement:** The replica's MySQL data directory filesystem has reached 100 % capacity, preventing the IO thread from writing new relay log events.
**Mechanism:** The IO thread writes incoming binary log events to relay log files in the MySQL data directory. When the filesystem is at capacity, the write fails with a disk-full error, the IO thread stops, and the replica falls progressively further behind. A stopped SQL thread (from any other cause) accelerates disk exhaustion because relay logs accumulate without being consumed and purged.

**Indicator:**

- [Step 6] `df -h /var/lib/mysql` shows the filesystem at or near 100 % used
- [Step 1] `Replica_IO_Running: No` and `Last_IO_Error` contains `disk` or `write` failure, or the error log shows `disk full`

<!-- match: {"step": 6, "predicate": "threshold", "target": "disk_pct_used", "op": ">", "value": 95} -->

**Mitigation:**

- **Risk:** Low. Freeing disk space by deleting old relay logs or expanding the volume does not alter data.
- **Command:**

  ```bash
  # Purge processed relay logs (MySQL tracks which are safe to delete)
  mysql -e "FLUSH RELAY LOGS;"

  # Check for other space consumers to free
  find /var/lib/mysql -name "*.log" -mtime +7 -exec ls -lh {} \;

  # After freeing space, restart the IO thread
  mysql -e "START REPLICA IO_THREAD;"
  ```

- **Duration:** Until disk is reclaimed. Set `relay_log_space_limit` to cap future relay log growth.

**Resolution:**

```bash
# Cap relay log disk usage (example: 10 GB)
mysql -e "SET GLOBAL relay_log_space_limit = 10737418240;"
```

Persist in `my.cnf`:

```ini
[mysqld]
relay_log_space_limit = 10737418240
```

**Verification:** Re-run Step 6 and confirm `df -h` shows available space. Re-run Step 1 and confirm `Replica_IO_Running: Yes` and `Seconds_Behind_Master` is decreasing. Alert threshold at 80 % disk should fire before the filesystem fills again.

### Cause G: Network interruption disconnected IO thread

**Statement:** A transient or persistent network failure between the replica and source caused the IO thread to disconnect, stopping binary log retrieval.
**Mechanism:** The IO thread maintains a persistent TCP connection to the source's MySQL port. A network interruption drops the TCP session. The IO thread detects the broken connection, waits per `replica_net_timeout` (default 60 seconds), then attempts to reconnect. If the network issue persists beyond the retry window, the IO thread stops with a connection error. Firewall rule changes, VPC reconfiguration, DNS failure, and security group changes are common triggers.

**Indicator:**

- [Step 5] connectivity test `mysql -h SOURCE_HOST -u repl_user -p -e "SELECT 1"` fails or times out
- [Step 1] `Replica_IO_Running: Connecting` or `No`, `Last_IO_Error` contains `Lost connection` or `Can't connect`

<!-- match: {"step": 5, "predicate": "contains", "target": "Can't connect"} -->

**Mitigation:**

- **Risk:** Low. Restarting replication threads simply resumes from where it left off.
- **Command:**

  ```bash
  mysql -e "STOP REPLICA; START REPLICA;"
  ```

- **Duration:** Immediate. The IO thread reconnects and resumes fetching binary log events.

**Resolution:**

```bash
# Verify network path is stable before considering resolved
mysql -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Seconds_Behind"

# Increase replica_net_timeout to tolerate brief interruptions (default: 60 seconds)
mysql -e "SET GLOBAL replica_net_timeout = 120;"
```

Persist in `my.cnf`:

```ini
[mysqld]
replica_net_timeout = 120
```

**Verification:** Re-run Step 1 after 5 minutes and confirm `Replica_IO_Running: Yes`. Re-run Step 5 to confirm stable connectivity. If the IO thread drops again within an hour, the network issue is persistent and must be resolved at the infrastructure level.

### Cause Z: Unidentified

**Statement:** Diagnostic steps do not point to any single cause above, or the error message does not match known patterns, and a confident root cause cannot be assigned.
**Mechanism:** MySQL replication can fail due to interactions not covered by Causes A–G: binary log format incompatibility (STATEMENT vs ROW vs MIXED), `max_allowed_packet` mismatch causing large events to fail, multithreaded replication GTID gaps when `slave_preserve_commit_order` is off, schema changes applied out of order, or third-party tools that alter GTID state. Without a clear signal, applying any targeted fix risks masking the actual driver.

**Indicator:**

- [Default]

**Mitigation:**

- **Risk:** Diagnostic only.
- **Command:**

  ```bash
  # Collect full replication state for offline analysis
  mysql -e "SHOW REPLICA STATUS\G" > /tmp/replica_status.txt
  mysql -e "SHOW GLOBAL VARIABLES LIKE '%replica%';" >> /tmp/replica_status.txt
  mysql -e "SHOW GLOBAL VARIABLES LIKE '%slave%';" >> /tmp/replica_status.txt
  mysql -e "SELECT * FROM performance_schema.replication_connection_status\G" \
    >> /tmp/replica_status.txt
  mysql -e "SELECT * FROM performance_schema.replication_applier_status_by_worker\G" \
    >> /tmp/replica_status.txt
  tail -500 /var/log/mysql/error.log >> /tmp/replica_status.txt
  ```

- **Duration:** Diagnostic only — does not change replication state.

**Resolution:** Out of runbook scope. Escalate to the database SRE/DBA on call with the diagnostic bundle above, the MySQL version (`SELECT VERSION();` on both servers), and the exact error messages from Steps 1 and 2.
**Verification:** Escalation acknowledged with diagnostic bundle attached; a follow-up runbook or incident review is opened to capture the new failure mode.

## Prevention

1. **Set `super_read_only = ON` on all replicas.** This is the single most effective measure — it prevents direct writes that cause data drift (Causes A, B, and D).

   ```bash
   mysql -e "SET GLOBAL super_read_only = ON;"
   ```

   Persist in `my.cnf`:

   ```ini
   [mysqld]
   super_read_only = ON
   ```

2. **Enable GTID-based replication.** GTIDs provide automatic positioning, simplify failover, and make errant transaction detection straightforward. Set `gtid_mode = ON` and `enforce_gtid_consistency = ON` on all servers.

3. **Retain binary logs for at least 7 days.** Set `binlog_expire_logs_seconds = 604800` on the source to give replicas time to catch up after extended outages before purging logs they still need.

4. **Monitor replication continuously.** Alert on `Seconds_Behind_Master > 30`, IO/SQL thread status changes, and error count increases. Use Prometheus with `mysqld_exporter` or `pt-heartbeat` for automated monitoring.

5. **Configure relay log durability.** Set `sync_relay_log = 1` and `relay_log_recovery = ON` to prevent relay log corruption after unclean shutdown.

6. **Set `relay_log_space_limit`.** Cap relay log disk usage to prevent runaway growth when the SQL thread is slow or stopped.

7. **Set `slave_preserve_commit_order = ON` for MTS.** When multithreaded replication is enabled, this setting (with `slave_parallel_type = LOGICAL_CLOCK`) prevents GTID gaps from forming during parallel apply.

8. **Run `pt-table-checksum` weekly.** Schedule a weekly data consistency check to detect silent drift before it causes a replication break.

9. **Match `max_allowed_packet` across all servers.** Ensure source and replicas have identical `max_allowed_packet` settings to prevent large-event apply failures.

10. **Keep MySQL versions compatible.** The replica may run the same or a newer minor version than the source, but never an older one. Test version upgrades on replicas before upgrading the source.

## Sources

- [MySQL 8.0 Reference Manual — Replication Troubleshooting](https://dev.mysql.com/doc/refman/8.0/en/replication-problems.html) — Priority 1. Diagnostic procedures, error codes, and skip-counter syntax.
- [MySQL 8.0 Reference Manual — SHOW REPLICA STATUS](https://dev.mysql.com/doc/refman/8.0/en/show-replica-status.html) — Priority 1. Complete field reference for `Replica_IO_Running`, `Replica_SQL_Running`, `Last_IO_Errno`, `Last_SQL_Errno`, `Seconds_Behind_Master`, GTID sets, and `Auto_Position`.
- [MySQL 8.0 Reference Manual — Using GTIDs for Failover and Scaleout](https://dev.mysql.com/doc/refman/8.0/en/replication-gtids-failover.html) — Priority 1. GTID provisioning methods, empty transaction injection, and `SOURCE_AUTO_POSITION` semantics.
- [MySQL 8.0 Reference Manual — Replication and Binary Logging Options](https://dev.mysql.com/doc/refman/8.0/en/replication-options.html) — Priority 1. Authoritative for `binlog_expire_logs_seconds`, `relay_log_space_limit`, `relay_log_recovery`, `sync_relay_log`, and `replica_net_timeout`.
