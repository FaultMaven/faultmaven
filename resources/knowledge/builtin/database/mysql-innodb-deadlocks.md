---
id: "mysql-innodb-deadlocks"
title: "MySQL InnoDB Deadlocks"
domain: database
service: mysql
symptom_class: [latency, timeout]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [innodb, deadlock, locking, transactions, isolation-level]
difficulty: intermediate
---

## Symptom Recognition

- Application receives MySQL error 1213: `Deadlock found when trying to get lock; try restarting transaction`
- Transaction retry rates increase in application metrics; write throughput drops
- Elevated query latency on write-heavy tables during peak traffic
- `SHOW ENGINE INNODB STATUS` shows a non-empty `LATEST DETECTED DEADLOCK` section
- `Innodb_deadlocks` status counter is incrementing
- Error log entries containing `TRANSACTION` / `HOLDS THE LOCK` / `WAITING FOR THIS LOCK` (when `innodb_print_all_deadlocks = ON`)
- Connection pool exhaustion or client timeouts caused by retried transactions backing up

## Applicability

Applies to MySQL 8.0+ (self-managed), Amazon RDS for MySQL, Amazon Aurora MySQL, Google Cloud SQL for MySQL, and MariaDB 10.3+ using the InnoDB storage engine.

Required access: MySQL account with `PROCESS`, `REPLICATION CLIENT`, and `SUPER` (or `SYSTEM_VARIABLES_ADMIN`) privileges to run `SHOW ENGINE INNODB STATUS`, query `performance_schema`, set global variables, and issue `KILL` statements. Read access to the MySQL error log is required for Step 2.

Tools needed: `mysql` CLI client, access to the application source or ORM query log.

## Diagnostic Steps

### Step 1:

Capture the most recent deadlock in full detail.

```bash
mysql -e "SHOW ENGINE INNODB STATUS\G" | awk '/LATEST DETECTED DEADLOCK/,/^---/'
```

Expected output: A block containing `TRANSACTION 1` and `TRANSACTION 2` sections listing the SQL statements, lock types (`lock_mode X`, `lock_mode X,GAP`, `lock_mode S,GAP`), index names, and the `WE ROLL BACK TRANSACTION` line identifying the victim.

### Step 2:

Enable logging of all deadlock events and inspect frequency.

```bash
mysql -e "SET GLOBAL innodb_print_all_deadlocks = ON;"
grep -i "deadlock" /var/log/mysql/error.log | grep -c "TRANSACTION"
```

Expected output: An integer count of deadlock events in the error log. Zero means no historical data yet; any non-zero value with a timestamp within the last hour confirms active deadlock pressure. Disable this setting after debugging: `SET GLOBAL innodb_print_all_deadlocks = OFF;`

### Step 3:

Inspect the current lock-wait graph to identify blocking transactions.

```bash
mysql -e "
SELECT
  r.trx_id AS waiting_trx,
  r.trx_mysql_thread_id AS waiting_thread,
  r.trx_query AS waiting_query,
  b.trx_id AS blocking_trx,
  b.trx_mysql_thread_id AS blocking_thread,
  b.trx_query AS blocking_query,
  TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS wait_sec
FROM performance_schema.data_lock_waits w
JOIN information_schema.innodb_trx b ON b.trx_id = w.BLOCKING_ENGINE_TRANSACTION_ID
JOIN information_schema.innodb_trx r ON r.trx_id = w.REQUESTING_ENGINE_TRANSACTION_ID;
"
```

Expected output: Rows pairing each waiting transaction with its blocker, including the SQL text and wait duration in seconds. Empty result means no current lock waits.

### Step 4:

List all granted InnoDB locks to identify gap locks and excessive lock scope.

```bash
mysql -e "
SELECT engine_transaction_id, lock_type, lock_mode, object_schema, object_name, index_name
FROM performance_schema.data_locks
WHERE lock_status = 'GRANTED'
ORDER BY engine_transaction_id, object_name;
"
```

Expected output: TABLE-level intent locks (IS, IX) plus RECORD-level row locks. Rows showing `lock_mode` values of `X,GAP` or `S,GAP` indicate gap locking (present only under `REPEATABLE READ`).

### Step 5:

Check the transaction isolation level in effect.

```bash
mysql -e "SELECT @@GLOBAL.transaction_isolation, @@SESSION.transaction_isolation;"
```

Expected output: Both values typically `REPEATABLE-READ`. If either shows `READ-COMMITTED`, gap locking is already disabled for that scope.

### Step 6:

Identify long-running transactions holding locks.

```bash
mysql -e "
SELECT trx_id, trx_state,
  TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS duration_sec,
  trx_rows_locked, trx_rows_modified, trx_query
FROM information_schema.innodb_trx
WHERE trx_state = 'RUNNING'
ORDER BY trx_started ASC;
"
```

Expected output: Running transactions with their age, locked row counts, and current SQL. Rows with `trx_query = NULL` and high `duration_sec` indicate idle-in-transaction connections holding locks without executing statements.

### Step 7:

Examine the execution plan of the queries from Step 1 to detect full-table-scan locking.

```bash
mysql -e "EXPLAIN <query_from_step1_deadlock_output>\G"
```

Replace `<query_from_step1_deadlock_output>` with the actual SQL from the deadlock output. Expected output: The `type` column shows `ALL` (full table scan), `range`, `ref`, or `eq_ref`. The `rows` column shows the estimated row count examined and therefore locked.

### Step 8:

Sample deadlock counter growth rate to quantify severity.

```bash
mysql -e "SHOW GLOBAL STATUS LIKE 'Innodb_deadlocks';"
mysql -e "SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_waits';"
mysql -e "SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_time_avg';"
```

Expected output: Cumulative counters. Record these values and re-run after 5 minutes. A rising `Innodb_deadlocks` counter confirms ongoing deadlocks. `Innodb_row_lock_time_avg` above 1000 ms indicates long lock contention even between deadlock events.

## Causes

### Cause A: Inconsistent Lock Acquisition Order

**Statement:** Two or more transactions acquire locks on the same rows or tables in opposite order, creating a circular wait.
**Mechanism:** Transaction A locks row 1 then attempts to lock row 2; Transaction B already holds a lock on row 2 and is waiting for row 1. InnoDB detects the cycle and rolls back the victim. This is the most common deadlock pattern and stems from uncoordinated lock ordering in application code.
**Indicator:**
- [Step 1] Deadlock output shows Transaction 1 holding a lock on table/index X and waiting on table/index Y, while Transaction 2 holds on Y and waits on X
- [Step 1] The two SQL statements access the same set of tables in a different sequence
<!-- match: {"step": 1, "predicate": "contains", "target": "HOLDS THE LOCK(S)"} -->
**Mitigation:**
- **Risk:** None — this is a code change only; no production data is affected until the new code is deployed
- **Command:**

  ```sql
  -- Enforce alphabetical table order and ascending PK order within each table
  BEGIN;
  UPDATE accounts SET balance = balance - 100 WHERE id = 1;  -- lower PK first
  UPDATE accounts SET balance = balance + 100 WHERE id = 2;  -- higher PK second
  COMMIT;
  ```

- **Duration:** Permanent after application deployment; no server restart required
**Resolution:**
```sql
-- Application-level change: ensure all code paths that write to multiple rows or tables
-- do so in the same deterministic order (e.g., alphabetical by table, ascending by PK).
-- Use stored procedures to enforce ordering when multiple code paths share the same data.
```

**Verification:** Monitor `Innodb_deadlocks` counter for 1 hour after deployment. Counter should stop incrementing or grow significantly slower.

### Cause B: Gap Locks Under REPEATABLE READ

**Statement:** InnoDB's default REPEATABLE READ isolation level uses next-key locks (record + gap) that conflict with concurrent inserts into the same index range.
**Mechanism:** A `SELECT ... FOR UPDATE` or `UPDATE` with a range predicate acquires next-key locks covering all index records and gaps in the scanned range. A concurrent `INSERT` into that gap acquires an insert-intention lock that conflicts with the held gap lock, producing a deadlock when two transactions each hold gap locks that the other's insert needs.
**Indicator:**
- [Step 4] `lock_mode` values of `X,GAP` or `S,GAP` appear in `performance_schema.data_locks`
- [Step 5] `@@GLOBAL.transaction_isolation` is `REPEATABLE-READ`
- [Step 1] Deadlock output contains `lock_mode X,GAP` or `lock_mode X locks gap before rec`
<!-- match: {"step": 4, "predicate": "contains", "target": "GAP"} -->
**Mitigation:**
- **Risk:** Moderate — `READ COMMITTED` allows phantom reads (rows inserted by other transactions become visible within the same transaction) and non-repeatable reads. Test application correctness before enabling in production.
- **Command:**

  ```bash
  mysql -e "SET GLOBAL transaction_isolation = 'READ-COMMITTED';"
  ```

- **Duration:** Immediate for new connections; existing sessions retain their prior isolation level until they reconnect. Persist in `my.cnf` for durability across restarts.
**Resolution:**
```ini
# Add to /etc/mysql/my.cnf (or /etc/my.cnf) under [mysqld]:
[mysqld]
transaction-isolation = READ-COMMITTED
```

**Impact:** Cluster-wide — affects all new connections on this MySQL instance. Requires MySQL restart to take effect from the config file.
**Rollback:** `SET GLOBAL transaction_isolation = 'REPEATABLE-READ';` to revert at runtime; remove the `my.cnf` line and restart to revert persistently.
**Verification:** Re-run Step 4 after switching to `READ COMMITTED`. `X,GAP` and `S,GAP` entries should be absent from `performance_schema.data_locks` for non-FK range queries.

### Cause C: Missing or Suboptimal Index Causes Excessive Row Locking

**Statement:** A write query without a selective index performs a full or broad table scan, locking far more rows than the operation requires and increasing deadlock collision probability.
**Mechanism:** InnoDB locks every row it examines during query execution, not just the rows it modifies. A full table scan (`type: ALL` in EXPLAIN) locks every row in the table with an X or next-key lock. This massively expands the lock footprint, making it probable that a concurrent transaction needs one of the locked rows, creating a circular wait.
**Indicator:**
- [Step 7] `EXPLAIN` shows `type: ALL` or `type: index` for the query from the deadlock output
- [Step 7] `rows` estimate is a large fraction of the total table row count
<!-- match: {"step": 7, "predicate": "contains", "target": "ALL"} -->
**Mitigation:**
- **Risk:** Low — MySQL 8.0 online DDL with `ALGORITHM=INPLACE, LOCK=NONE` allows concurrent reads and writes during index creation on most table types. Adding an index increases write overhead proportionally.
- **Command:**

  ```bash
  mysql -e "ALTER TABLE <table> ADD INDEX idx_col (<column>) ALGORITHM=INPLACE, LOCK=NONE;"
  ```

- **Duration:** Seconds to minutes depending on table size; concurrent DML is not blocked.
**Resolution:**
```bash
mysql -e "ALTER TABLE <table> ADD INDEX idx_col (<column>) ALGORITHM=INPLACE, LOCK=NONE;"
```

**Verification:** Re-run `EXPLAIN <query>` from Step 7. `type` should change to `ref` or `eq_ref` and `rows` should drop to only the matching rows. Monitor `Innodb_deadlocks` over the next hour to confirm the reduction.

### Cause D: Long-Running or Idle-in-Transaction Sessions Holding Locks

**Statement:** A transaction that holds locks for an extended period — either running a slow query or sitting idle — blocks other transactions long enough to create deadlock cycles.
**Mechanism:** Every second a transaction holds a row lock is another second during which a concurrent transaction that needs the same row must wait. When two such transactions each hold a lock the other needs, and neither times out, InnoDB detects the cycle. Idle-in-transaction connections (opened a transaction but the application is waiting on external I/O, user input, or is stuck) are a common source of long-held locks.
**Indicator:**
- [Step 6] Rows show `duration_sec` above 5 seconds with non-zero `trx_rows_locked`
- [Step 6] Rows show `trx_query = NULL` (idle-in-transaction) with high `duration_sec`
- [Step 3] `blocking_query` is NULL for the blocking thread (idle-in-transaction)
<!-- match: {"step": 6, "predicate": "threshold", "target": "duration_sec", "op": ">", "value": 5} -->
**Mitigation:**
- **Risk:** Moderate — killing a transaction rolls it back. Confirm the session is safe to abort before issuing `KILL`. The client will receive a connection error and should reconnect.
- **Command:**

  ```bash
  # Identify blocking_thread from Step 3, then:
  mysql -e "KILL <blocking_thread_id>;"
  ```

- **Duration:** Immediate; the killed transaction is rolled back and its locks are released.
**Resolution:**
```sql
-- Set a global idle-in-transaction timeout to automatically close idle sessions:
SET GLOBAL wait_timeout = 60;
SET GLOBAL interactive_timeout = 60;
-- For MySQL 8.0+, also set transaction-specific idle limit:
SET GLOBAL innodb_rollback_on_timeout = ON;
```

**Impact:** Instance-wide — `wait_timeout` affects all connections. Reducing from the default (8 hours) to 60 seconds may close legitimate long-running application sessions; test with application connection pooler behavior first.
**Rollback:** `SET GLOBAL wait_timeout = 28800; SET GLOBAL interactive_timeout = 28800;`
**Verification:** Re-run Step 6 after applying the timeout. No rows should show `duration_sec` above the configured `wait_timeout`. Confirm `Innodb_deadlocks` stops incrementing over the next hour.

### Cause E: Foreign Key Constraint Checks Acquiring Parent-Table Locks

**Statement:** InnoDB acquires shared locks on parent-table rows during foreign key constraint validation, which can conflict with concurrent exclusive locks on those same parent rows.
**Mechanism:** When a child row is inserted or updated, InnoDB reads the referenced parent row to verify the constraint and acquires a shared lock (S lock) on it. If another transaction simultaneously holds or is waiting for an exclusive lock (X lock) on that parent row, the shared-lock attempt blocks and can complete a circular wait with the other transaction's waiting lock, producing a deadlock.
**Indicator:**
- [Step 1] Deadlock output shows one transaction waiting on a shared lock (`lock_mode S`) on the parent table while another holds an exclusive lock on the same row
- [Step 4] Parent table appears in granted locks with both `S` and `IX` mode entries from different transactions
<!-- match: {"step": 1, "predicate": "contains", "target": "lock_mode S"} -->
**Mitigation:**
- **Risk:** Low — ordering inserts so parent rows precede child rows is an application-level change with no data risk.
- **Command:**

  ```sql
  BEGIN;
  INSERT INTO parent_table (id, ...) VALUES (42, ...);  -- parent first
  INSERT INTO child_table (parent_id, ...) VALUES (42, ...);  -- child second
  COMMIT;
  ```

- **Duration:** Permanent after application code change; no server configuration needed.
**Resolution:**
```sql
-- Ensure all application code paths insert parent rows before child rows within
-- the same transaction. For bulk loads only (not normal application flow):
SET FOREIGN_KEY_CHECKS = 0;
-- <bulk insert child rows>
SET FOREIGN_KEY_CHECKS = 1;
```

**Verification:** Monitor `Innodb_deadlocks` for 1 hour after deploying the ordering fix. Run Step 3 during peak load to confirm parent tables no longer appear as contested resources.

### Cause F: Bulk DML Operations Competing With Concurrent Transactions

**Statement:** A single large INSERT, UPDATE, or DELETE affecting thousands of rows holds row locks across a wide range for the full transaction duration, colliding with concurrent transactions that need any of those rows.
**Mechanism:** A bulk operation executed in a single transaction acquires and holds all its row locks until commit. The longer this takes (seconds to minutes for large tables), the larger the window in which a concurrent transaction can acquire a subset of the same locks in a different order, completing a circular wait. Row-level locking means every row examined in a full-table-scan bulk operation carries a lock.
**Indicator:**
- [Step 6] A transaction shows very high `trx_rows_modified` (thousands or more) and elevated `duration_sec`
- [Step 1] Deadlock output involves a query with no `WHERE` clause or a very broad range predicate
<!-- match: {"step": 6, "predicate": "threshold", "target": "trx_rows_modified", "op": ">", "value": 1000} -->
**Mitigation:**
- **Risk:** Low — batching writes into smaller transactions does not change the final data state; it only allows other transactions to interleave between batches.
- **Command:**

  ```sql
  -- Batch deletes in chunks of 1000 rows with explicit COMMIT between batches
  SET @deleted = 1;
  WHILE @deleted > 0 DO
    DELETE FROM large_table WHERE status = 'expired' LIMIT 1000;
    SET @deleted = ROW_COUNT();
    COMMIT;
  END WHILE;
  ```

- **Duration:** Permanent after application or script change; no server configuration needed.
**Resolution:**
```sql
-- Same batch approach as Mitigation — break bulk operations into 500-1000 row
-- transactions with COMMIT between batches to release locks and allow interleaving.
```

**Verification:** Run Step 6 during the next bulk operation. `trx_rows_modified` for any single transaction should stay below 1000. Confirm `Innodb_deadlocks` does not spike during bulk jobs.

### Cause Z: Unidentified Deadlock Pattern

**Statement:** The deadlock cannot be attributed to a known lock ordering, isolation level, index, session lifetime, foreign key, or bulk operation pattern.
**Mechanism:** InnoDB's deadlock detection relies on identifiable circular waits between known lock types. When the LATEST DETECTED DEADLOCK output does not match any of the documented patterns — possibly due to application-level connection pooling, unusual isolation level combinations, or undocumented InnoDB edge cases — manual escalation to a DBA or MySQL support is required.
**Indicator:**
- [Default] None of Causes A–F patterns match the deadlock output from Step 1
**Mitigation:**
- **Risk:** Low — enabling retry logic is always safe and is the primary recommended defense against any deadlock pattern.
- **Command:**

  ```python
  # Implement error-1213 retry with exponential backoff in the application:
  import time
  MAX_RETRIES = 3
  for attempt in range(MAX_RETRIES):
      try:
          conn.begin()
          # execute transaction statements
          conn.commit()
          break
      except Exception as e:
          conn.rollback()
          if getattr(e, 'errno', None) == 1213 and attempt < MAX_RETRIES - 1:
              time.sleep(0.1 * (2 ** attempt))
              continue
          raise
  ```

- **Duration:** Immediate after application deployment; covers all deadlock patterns regardless of root cause.
**Resolution:** Out of runbook scope — escalate with the full `SHOW ENGINE INNODB STATUS` output, the application query log, and `performance_schema.data_locks` snapshots during the deadlock window.
**Verification:** Confirm application error logs no longer surface unhandled error 1213 exceptions. Monitor `Innodb_deadlocks` trend; the counter may still increment but application-level impact should cease once retries absorb the deadlocks.

## Prevention

1. **Enforce consistent lock ordering** — Establish a coding convention that all transactions access tables and rows in the same deterministic order (e.g., alphabetical by table name, ascending by primary key). Enforce in code review and stored procedures.

2. **Keep transactions as short as possible** — Commit immediately after completing related changes. Move external API calls, file I/O, and user interaction outside transaction boundaries. Never hold an open transaction while waiting for user input.

3. **Implement retry logic for all database clients** — All client code must catch error 1213 and retry with exponential backoff (100 ms, 200 ms, 400 ms). This is mandatory regardless of other prevention measures, as some deadlock rate is unavoidable in concurrent systems.

4. **Index every locked column** — Every `SELECT ... FOR UPDATE`, `UPDATE`, and `DELETE` must use a selective index in its `WHERE` clause. Run `EXPLAIN` on all write queries regularly and add indexes where `type: ALL` appears.

5. **Use READ COMMITTED unless REPEATABLE READ is required** — `READ COMMITTED` eliminates gap locks and next-key locks, the most common deadlock contributors. Only switch to `REPEATABLE READ` when the application genuinely needs repeatable reads or phantom-row prevention.

6. **Batch large write operations** — Break any INSERT/UPDATE/DELETE affecting more than 1000 rows into smaller transactions with explicit COMMIT between batches (500–1000 rows per batch).

7. **Set idle-in-transaction timeouts** — Configure `wait_timeout` and `interactive_timeout` to 60–120 seconds to automatically close sessions that open transactions and then go idle.

8. **Monitor deadlock rate continuously** — Track `Innodb_deadlocks` and `Innodb_row_lock_time_avg` in Prometheus (`mysqld_exporter`) or CloudWatch. Alert on a deadlock rate above your historical baseline.

9. **Enable slow query log proactively** — Slow queries hold locks longer and increase deadlock probability:

   ```bash
   mysql -e "SET GLOBAL slow_query_log = ON; SET GLOBAL long_query_time = 1;"
   ```

10. **Stress-test write-heavy workloads before deployment** — Use `sysbench` or `mysqlslap` to simulate realistic concurrency levels and expose deadlock-prone access patterns in staging.

## Sources

- [MySQL 8.0 Reference Manual — InnoDB Deadlocks](https://dev.mysql.com/doc/refman/8.0/en/innodb-deadlocks.html) — deadlock detection mechanism, `innodb_deadlock_detect`, `innodb_print_all_deadlocks`, `innodb_lock_wait_timeout`; priority 1
- [MySQL 8.0 Reference Manual — How to Minimize and Handle Deadlocks](https://dev.mysql.com/doc/refman/8.0/en/innodb-deadlocks-handling.html) — retry logic recommendation, consistent lock ordering, transaction size, isolation level, index guidance; priority 1
- [MySQL 8.0 Reference Manual — InnoDB Locking](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking.html) — shared/exclusive locks, gap locks, next-key locks, insert-intention locks, lock compatibility matrix, isolation-level comparison; priority 1
