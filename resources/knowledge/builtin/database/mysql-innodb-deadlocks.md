---
id: mysql-innodb-deadlocks
title: "MySQL InnoDB Deadlocks — Detection, Diagnosis, and Resolution"
domain: database
service: mysql
symptom_class:
  - latency
  - timeout
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - mysql
  - innodb
  - deadlock
  - locking
  - transactions
difficulty: intermediate
---

# MySQL InnoDB Deadlocks — Detection, Diagnosis, and Resolution

## Problem Definition

This runbook covers MySQL 8.0+ deployments using the InnoDB storage engine that are experiencing deadlocks. It applies to self-managed instances, Amazon RDS for MySQL, Aurora MySQL, Google Cloud SQL for MySQL, and MariaDB with InnoDB. You need a MySQL client with privileges to run `SHOW ENGINE INNODB STATUS`, query `information_schema.innodb_trx`, query `performance_schema.data_locks` and `performance_schema.data_lock_waits`, set global variables, and `KILL` threads. Access to the MySQL error log is also required.

A deadlock is a situation in which two or more transactions hold locks that the other transactions need, creating a circular wait dependency where none can proceed. InnoDB automatically detects deadlocks (when `innodb_deadlock_detect = ON`, the default) and rolls back one transaction (the "victim" -- typically the one with the fewest row modifications). If deadlock detection is disabled, InnoDB relies on `innodb_lock_wait_timeout` (default 50 seconds) to break the cycle. Frequent deadlocks are not dangerous to the database but indicate application-level issues that degrade performance and reliability.

**Common symptoms:**

- Application receives error 1213: `Deadlock found when trying to get lock; try restarting transaction`
- Increased transaction retry rates in application metrics
- Elevated query latency during peak write traffic
- Timeouts on write-heavy workloads
- `SHOW ENGINE INNODB STATUS` shows a populated LATEST DETECTED DEADLOCK section
- `Innodb_deadlocks` status variable increasing over time
- Error log entries containing deadlock details (when `innodb_print_all_deadlocks` is enabled)

**Common root causes:**

- Inconsistent lock ordering across transactions (Transaction A locks row 1 then row 2, Transaction B locks row 2 then row 1)
- Long-running transactions holding locks while waiting for additional locks
- Missing or suboptimal indexes causing full table scans that acquire excessive row locks
- Gap locks in `REPEATABLE READ` isolation level (the default) creating phantom lock conflicts
- Bulk INSERT/UPDATE/DELETE operations competing with concurrent transactions
- Foreign key constraint checks acquiring shared locks on parent tables
- `SELECT ... FOR UPDATE` or `SELECT ... FOR SHARE` acquiring unnecessary locks
- Next-key locks (record lock + gap lock) on secondary indexes blocking concurrent inserts

## Diagnostic Steps

### Step 1: View the most recent deadlock

**What this checks:** The full details of the last detected deadlock, including both transactions, the locks held and waited for, the SQL statements, and which transaction was chosen as the victim.

```bash
mysql -e "SHOW ENGINE INNODB STATUS\G" | sed -n '/LATEST DETECTED DEADLOCK/,/^---/p'
```

**Expected output:** A section showing two transactions (TRANSACTION 1 and TRANSACTION 2) with their lock information, SQL statements, and the rollback decision indicating which was rolled back.

**What the finding means:** The output shows the exact lock types involved (record lock, gap lock, next-key lock), the index used, and the SQL statements. The `lock_mode` field reveals whether the conflict involves shared (S) or exclusive (X) locks, and whether gap locks (indicated by `lock_mode X,GAP` or `lock_mode S,GAP`) are a factor. The transaction with "WE ROLL BACK" is the victim.

### Step 2: Enable logging of all deadlocks

**What this checks:** Configures MySQL to write every deadlock event to the error log, not just the most recent one, enabling historical analysis and frequency tracking.

```bash
mysql -e "SET GLOBAL innodb_print_all_deadlocks = ON;"
```

Review deadlocks in the error log:

```bash
grep -i "deadlock" /var/log/mysql/error.log | tail -50
```

**Expected output:** Deadlock entries in the error log with timestamps and full transaction details identical to the LATEST DETECTED DEADLOCK section.

**What the finding means:** Multiple deadlocks on the same tables/indexes in a short period indicate a systematic application-level lock ordering problem. Deadlocks on different tables suggest broader contention patterns. Disable this setting after debugging to avoid filling the error log: `SET GLOBAL innodb_print_all_deadlocks = OFF;`

### Step 3: Monitor lock waits in real time

**What this checks:** Which transactions are currently waiting for locks and which transactions are blocking them, showing the live lock-wait graph.

```bash
mysql -e "
SELECT
  r.trx_id AS waiting_trx_id,
  r.trx_mysql_thread_id AS waiting_thread,
  r.trx_query AS waiting_query,
  b.trx_id AS blocking_trx_id,
  b.trx_mysql_thread_id AS blocking_thread,
  b.trx_query AS blocking_query,
  TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS wait_seconds
FROM performance_schema.data_lock_waits w
JOIN information_schema.innodb_trx b ON b.trx_id = w.BLOCKING_ENGINE_TRANSACTION_ID
JOIN information_schema.innodb_trx r ON r.trx_id = w.REQUESTING_ENGINE_TRANSACTION_ID;
"
```

**Expected output:** Rows showing each waiting transaction paired with its blocker, including the SQL statement and wait duration.

**What the finding means:** If the same blocking transaction appears repeatedly, it is holding locks for too long. If `wait_seconds` is high, the blocking transaction may be idle-in-transaction (the application opened a transaction but is not currently executing a statement). If `blocking_query` is NULL, the blocking connection is idle inside an open transaction.

### Step 4: Examine current lock holders

**What this checks:** All currently granted locks in the InnoDB engine, showing which transactions hold which locks on which tables and indexes.

```bash
mysql -e "
SELECT
  engine_lock_id,
  engine_transaction_id,
  lock_type,
  lock_mode,
  lock_status,
  object_schema,
  object_name,
  index_name
FROM performance_schema.data_locks
WHERE lock_status = 'GRANTED'
ORDER BY engine_transaction_id;
"
```

**Expected output:** Rows listing each granted lock with its type (TABLE or RECORD), mode (S, X, IS, IX, S+GAP, X+GAP), and the table/index involved.

**What the finding means:** A transaction holding many record locks on a table is likely performing a scan without a precise index. Gap locks (`S,GAP` or `X,GAP`) in the output indicate `REPEATABLE READ` isolation level gap locking, which is a common deadlock contributor. Intent locks (IS, IX) are table-level indicators of row-level lock intent and are generally harmless.

### Step 5: Check transaction isolation level

**What this checks:** The currently configured isolation level, which directly determines the lock types InnoDB uses.

```bash
mysql -e "SELECT @@GLOBAL.transaction_isolation, @@SESSION.transaction_isolation;"
```

**Expected output:** The global and session isolation levels (typically `REPEATABLE-READ`).

**What the finding means:** `REPEATABLE READ` (the MySQL default) uses next-key locks (record + gap) that increase deadlock probability compared to `READ COMMITTED`, which uses only record locks for most operations. Gap locks prevent phantom rows but create contention on index ranges. `READ COMMITTED` reads from a fresh snapshot for each consistent read within the same transaction.

### Step 6: Identify long-running transactions

**What this checks:** Transactions that have been running for an extended period and may be holding locks that contribute to deadlock chains.

```bash
mysql -e "
SELECT
  trx_id,
  trx_state,
  trx_started,
  TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS duration_seconds,
  trx_rows_locked,
  trx_rows_modified,
  trx_query
FROM information_schema.innodb_trx
WHERE trx_state = 'RUNNING'
ORDER BY trx_started ASC;
"
```

**Expected output:** A list of running transactions with their age, number of locked rows, and current query.

**What the finding means:** Transactions with high `duration_seconds` and high `trx_rows_locked` are holding locks for extended periods, increasing the probability that another transaction will need one of those locks and create a deadlock cycle. Transactions with `trx_query = NULL` are idle-in-transaction (the application opened a transaction but is not currently executing a statement).

### Step 7: Analyze query execution plans for lock-heavy queries

**What this checks:** Whether the queries involved in deadlocks are using indexes efficiently or are scanning excessive rows (and therefore locking excessive rows).

```bash
mysql -e "EXPLAIN SELECT ... FOR UPDATE;"
```

Replace the SELECT with the actual query from the deadlock output in Step 1.

**Expected output:** An execution plan showing the access type (`ALL` for full scan, `range` for range scan, `ref`/`eq_ref` for index lookup) and estimated rows examined.

**What the finding means:** Queries with `type: ALL` (full table scan) lock every row in the table, massively increasing deadlock probability. Queries with `type: range` on a secondary index in `REPEATABLE READ` acquire next-key locks on the entire scanned range. Switching to an index lookup (`ref`/`eq_ref`) reduces the locked row set to only the matched rows.

### Step 8: Check the deadlock frequency trend

**What this checks:** The cumulative deadlock count to establish whether deadlocks are increasing, stable, or decreasing over time.

```bash
mysql -e "SHOW GLOBAL STATUS LIKE 'Innodb_deadlocks';"
mysql -e "SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_waits';"
mysql -e "SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_time_avg';"
```

**Expected output:** Counters showing total deadlocks since server start, total row lock waits, and average lock wait time in milliseconds.

**What the finding means:** Record these values and compare after an interval (e.g., 1 hour). A stable `Innodb_deadlocks` count means no new deadlocks are occurring. Rising `Innodb_row_lock_waits` with stable `Innodb_deadlocks` means contention exists but is not reaching circular-wait conditions. High `Innodb_row_lock_time_avg` indicates that lock waits are long even when deadlocks do not occur.

## Mitigation

### Option 1: Implement application-level retry logic

Deadlocks are expected in concurrent systems. The application should catch error 1213 and retry the transaction.

- **Risk:** None. This is the standard approach recommended by the MySQL documentation. Always prepare applications to re-issue transactions if they fail due to deadlock.
- **Command:**

```python
# Python pseudocode — implement in your application language
MAX_RETRIES = 3
for attempt in range(MAX_RETRIES):
    try:
        connection.begin()
        # execute_transaction_statements()
        connection.commit()
        break
    except MySQLError as e:
        connection.rollback()
        if e.errno == 1213 and attempt < MAX_RETRIES - 1:
            time.sleep(0.1 * (2 ** attempt))  # exponential backoff
            continue
        raise
```

- **Verify:**

```bash
mysql -e "SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_waits';"
# Expected: value stable or decreasing after retry logic absorbs transient deadlocks
```

- **Duration:** Immediate after application deployment.

### Option 2: Switch to READ COMMITTED isolation level

Use when gap locks are a primary deadlock contributor (visible as `lock_mode X,GAP` in `SHOW ENGINE INNODB STATUS`).

- **Risk:** Moderate. Phantom reads become possible (new rows inserted by other transactions become visible within a transaction). Non-repeatable reads occur. Each consistent read within the same transaction reads from its own fresh snapshot. Test application correctness thoroughly before changing in production.
- **Command:**

```bash
mysql -e "SET GLOBAL transaction_isolation = 'READ-COMMITTED';"
```

For persistence across restarts, add to `/etc/mysql/my.cnf`:

```ini
[mysqld]
transaction-isolation = READ-COMMITTED
```

- **Verify:**

```bash
mysql -e "SELECT @@GLOBAL.transaction_isolation;"
# Expected: READ-COMMITTED
```

- **Duration:** Immediate for the global runtime setting. Requires a MySQL restart for config file persistence. Existing sessions retain their previous isolation level until they reconnect.

### Option 3: Kill the blocking transaction

Use when a specific long-running or idle-in-transaction session is causing a lock-wait chain and cannot be resolved otherwise.

- **Risk:** Moderate. The killed transaction will be rolled back. Ensure it is safe to abort. The client application will receive an error and should handle reconnection.
- **Command:**

```bash
# Identify the blocking thread from Step 3 output, then:
mysql -e "KILL <blocking_thread_id>;"
```

- **Verify:**

```bash
mysql -e "SELECT COUNT(*) FROM information_schema.innodb_trx WHERE trx_state = 'LOCK WAIT';"
# Expected: 0 or significantly reduced count
```

- **Duration:** Immediate.

### Option 4: Add missing indexes to reduce lock scope

Use when `EXPLAIN` shows full table scans or broad range scans on queries involved in deadlocks.

- **Risk:** Low. Online DDL in MySQL 8.0 (`ALGORITHM=INPLACE, LOCK=NONE`) allows concurrent reads and writes during index creation. Adding an index increases write overhead proportional to the number of indexed fields.
- **Command:**

```bash
mysql -e "ALTER TABLE <table_name> ADD INDEX idx_<column> (<column>) ALGORITHM=INPLACE, LOCK=NONE;"
```

- **Verify:**

```bash
mysql -e "EXPLAIN SELECT ... FOR UPDATE;"
# Expected: query uses the new index (type: ref or eq_ref) instead of full table scan (type: ALL)
```

- **Duration:** Seconds to minutes depending on table size.

### Option 5: Reorder lock acquisition in application code

Use when the deadlock output shows two transactions accessing the same rows/tables in opposite order.

- **Risk:** Low. This is a code change that does not affect data. Requires careful review to ensure all transaction paths follow the same ordering convention.
- **Command:**

Application-level change -- ensure all transactions access tables and rows in a consistent, deterministic order:

```sql
-- GOOD: Consistent order — always table1 before table2, ascending by PK
BEGIN;
UPDATE table1 SET ... WHERE id = 1;
UPDATE table2 SET ... WHERE id = 2;
COMMIT;

-- BAD: Inconsistent order causes deadlocks
-- Transaction A: UPDATE table1 then table2
-- Transaction B: UPDATE table2 then table1
```

- **Verify:**

```bash
mysql -e "SHOW GLOBAL STATUS LIKE 'Innodb_deadlocks';"
# Record value, wait 1 hour, check again
# Expected: deadlock count stops increasing
```

- **Duration:** Immediate after application code deployment.

## Root Cause Resolution

**If** deadlocks involve the same two tables accessed in opposite order --> rewrite application code to always access tables and rows in a consistent, deterministic order (for example, alphabetical by table name, ascending by primary key within a table). Use stored procedures to enforce ordering when multiple code paths access the same data.

**If** `SHOW ENGINE INNODB STATUS` shows gap locks (`lock_mode X,GAP` or next-key locks) --> switch to `READ COMMITTED` isolation level, which eliminates gap locking for most operations. Only `REPEATABLE READ` and `SERIALIZABLE` use gap locks.

**If** deadlocks occur on bulk operations (large INSERT/UPDATE/DELETE affecting thousands of rows) --> break bulk operations into smaller batches of 500-1000 rows per transaction with explicit `COMMIT` between batches, allowing other transactions to interleave.

**If** foreign key constraint checks cause shared lock escalation on parent tables --> ensure parent table rows are inserted before child rows within the same transaction. Consider deferring foreign key checks with `SET FOREIGN_KEY_CHECKS=0` only during controlled bulk data loads (never in normal application flow).

**If** `SELECT ... FOR UPDATE` acquires locks on more rows than needed --> add precise indexes so the `WHERE` clause matches an index, narrowing the locked row set to only the rows actually needed. Use plain `SELECT` (consistent read, no locks in InnoDB) when you only need to read data without preventing concurrent modifications.

**If** long-running transactions hold locks for many seconds --> refactor the application to keep transactions as short as possible. Move all non-transactional work (external API calls, file I/O, user interaction) outside the transaction boundaries. Commit immediately after completing related changes.

**If** deadlocks spike during peak hours but the lock ordering is already consistent --> implement connection pooling with transaction timeout limits (`innodb_lock_wait_timeout`, default 50 seconds). Consider read replicas to offload read traffic from the primary.

**If** deadlock detection overhead is a concern at very high concurrency (thousands of concurrent transactions) --> consider disabling `innodb_deadlock_detect` and relying on `innodb_lock_wait_timeout` instead. This trades faster deadlock resolution for timeout-based resolution but eliminates the O(n^2) detection overhead.

## Verification

After applying fixes, confirm deadlock frequency has decreased:

```bash
# 1. Check deadlock count (should stop increasing or grow more slowly)
mysql -e "SHOW GLOBAL STATUS LIKE 'Innodb_deadlocks';"
# Record the value and compare after 1 hour

# 2. Monitor average lock wait time
mysql -e "SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_time_avg';"
# Expected: decreasing average wait time in milliseconds

# 3. Verify no active lock waits
mysql -e "SELECT COUNT(*) FROM information_schema.innodb_trx WHERE trx_state = 'LOCK WAIT';"
# Expected: 0 during normal operation

# 4. Check deadlock frequency in the error log
grep -c "DEADLOCK" /var/log/mysql/error.log
# Compare to the count before applying fixes

# 5. Monitor lock wait events via Performance Schema
mysql -e "
SELECT EVENT_NAME, COUNT_STAR, SUM_TIMER_WAIT/1000000000 AS total_wait_ms
FROM performance_schema.events_waits_summary_global_by_event_name
WHERE EVENT_NAME LIKE '%innodb%lock%'
ORDER BY SUM_TIMER_WAIT DESC
LIMIT 10;
"
# Expected: lower COUNT_STAR and total_wait_ms compared to before
```

Monitor for at least 24 hours during normal traffic patterns to confirm the improvement is sustained and deadlocks do not resurface under peak load.

## Prevention

1. **Enforce consistent lock ordering** -- Establish a coding convention that all transactions access tables and rows in the same deterministic order. Document the ordering rule in development guidelines and enforce it in code review.

2. **Keep transactions short** -- Minimize the time between the first lock acquisition and `COMMIT`. Never include user interaction, external API calls, or file operations inside a transaction. Commit immediately after completing related changes.

3. **Use appropriate isolation level** -- Use `READ COMMITTED` unless your application specifically requires `REPEATABLE READ` guarantees. This eliminates gap locks and next-key locks, which are the most common deadlock contributors.

4. **Add indexes for all locked queries** -- Every `SELECT ... FOR UPDATE`, `UPDATE`, and `DELETE` should use an index in its `WHERE` clause. Regularly run `EXPLAIN` on write queries to verify index usage. Use `EXPLAIN SELECT` to determine which indexes MySQL regards as most appropriate.

5. **Implement retry logic in all database clients** -- All database client code should catch error 1213 and retry with exponential backoff (for example, 100ms, 200ms, 400ms). This is a best practice regardless of other prevention measures.

6. **Batch large writes** -- Break operations affecting thousands of rows into batches of 500-1000 rows, committing between batches to release locks and allow concurrent transactions to proceed.

7. **Monitor deadlock metrics** -- Track `Innodb_deadlocks` and `Innodb_row_lock_waits` in your monitoring system (Prometheus with `mysqld_exporter`, or Percona `pt-deadlock-logger`). Set alerts for anomalous spikes above the historical baseline.

8. **Avoid unnecessary locking** -- Use plain `SELECT` (consistent read, no locks in InnoDB) instead of `SELECT ... FOR UPDATE` when you only need to read data. Reserve locking reads for cases where you genuinely need to prevent concurrent modifications.

9. **Enable the slow query log** -- Queries that take long to execute hold locks longer, increasing deadlock probability. Identify and optimize slow queries proactively:

    ```bash
    mysql -e "SET GLOBAL slow_query_log = ON; SET GLOBAL long_query_time = 1;"
    ```

10. **Test under realistic concurrency** -- Stress-test write-heavy workflows with realistic concurrency levels before deployment. Tools like `sysbench` and `mysqlslap` can simulate concurrent transactions and expose deadlock-prone access patterns.

## Sources

- [MySQL 8.0 Reference Manual -- Deadlocks in InnoDB](https://dev.mysql.com/doc/refman/8.0/en/innodb-deadlocks.html)
- [MySQL 8.0 Reference Manual -- How to Minimize and Handle Deadlocks](https://dev.mysql.com/doc/refman/8.0/en/innodb-deadlocks-handling.html)
- [MySQL 8.4 Reference Manual -- Deadlock Detection](https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlock-detection.html)
- [MySQL 8.0 Reference Manual -- InnoDB Troubleshooting](https://dev.mysql.com/doc/refman/8.0/en/innodb-troubleshooting.html)
- [MySQL 8.0 Reference Manual -- SHOW ENGINE INNODB STATUS](https://dev.mysql.com/doc/refman/8.0/en/show-engine.html)
- [MySQL 8.0 Reference Manual -- InnoDB Transaction Isolation Levels](https://dev.mysql.com/doc/refman/8.0/en/innodb-transaction-isolation-levels.html)
- [MySQL 8.0 Reference Manual -- InnoDB Locking](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking.html)
- [Percona -- How to Deal with MySQL Deadlocks](https://www.percona.com/blog/how-to-deal-with-mysql-deadlocks/)
