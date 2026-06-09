---
id: "mongo-lock-contention"
title: "MongoDB Lock Contention and Slow Operations"
domain: database
service: mongodb
symptom_class: [latency, timeout]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [mongodb, wiredtiger, lock-contention, slow-queries, profiling, current-op, tickets]
difficulty: intermediate
---

## Symptom Recognition

- p99 query latency spikes while throughput holds steady or drops
- `db.currentOp()` returns operations with `"waitingForLock": true`
- `serverStatus().globalLock.currentQueue.total` is consistently non-zero
- Application connection pool exhaustion caused by slow upstream responses
- `mongotop` shows high lock percentage on one or more collections
- MongoDB log entries: `Slow query` warnings for operations exceeding `slowms` (default 100 ms)
- `serverStatus().wiredTiger.concurrentTransactions.read.available` or `.write.available` at `0`
- `serverStatus().locks.<type>.timeAcquiringMicros` grows rapidly between polls
- Multi-document transaction timeout errors: `"Transaction has been aborted"` or `"exceeded time limit"`

## Applicability

Applies to MongoDB 4.4–8.x using the WiredTiger storage engine (standalone, replica set, and sharded cluster topologies, including Atlas M10+). Requires `mongosh` access with privileges to run `db.serverStatus()`, `db.adminCommand()`, `db.currentOp()`, `db.setProfilingLevel()`, and `db.killOp()`. Access to `mongod.log` is required for slow-query log analysis. Atlas free/flex-tier clusters restrict some `serverStatus` fields.

## Diagnostic Steps

### Step 1: Check global lock queue depth

```javascript
mongosh --eval '
  const s = db.serverStatus();
  printjson({
    currentQueue: s.globalLock.currentQueue,
    activeClients: s.globalLock.activeClients
  });
'
```

Expected output: `currentQueue.readers` and `currentQueue.writers` both `0` when idle. Values consistently above `0` confirm operations are queuing for lock access.

### Step 2: Identify operations waiting for locks

```javascript
mongosh --eval '
  db.currentOp({ "waitingForLock": true }).inprog.forEach(function(op) {
    printjson({
      opid: op.opid,
      op: op.op,
      ns: op.ns,
      secs_running: op.secs_running,
      desc: op.desc
    });
  });
'
```

Expected output: List of blocked operations with namespace and wait duration. Multiple operations sharing the same `ns` identifies the contention hotspot collection.

### Step 3: Identify active slow operations and their query plans

```javascript
mongosh --eval '
  db.currentOp({ "active": true, "secs_running": { "$gt": 5 } }).inprog.forEach(function(op) {
    printjson({
      opid: op.opid,
      op: op.op,
      ns: op.ns,
      secs_running: op.secs_running,
      planSummary: op.planSummary,
      command: op.command
    });
  });
'
```

Expected output: Long-running operations with `planSummary` field. `"COLLSCAN"` confirms a full collection scan due to a missing index.

### Step 4: Check WiredTiger concurrency ticket availability

```javascript
mongosh --eval '
  const ct = db.serverStatus().wiredTiger.concurrentTransactions;
  printjson({ read: ct.read, write: ct.write });
'
```

Expected output: JSON with `totalTickets`, `available`, and `out` for read and write. Default is 128 read and 128 write tickets. `available: 0` for either category means the storage engine is saturated and new operations of that type must queue.

### Step 5: Check lock acquisition wait times by lock type

```javascript
mongosh --eval '
  const locks = db.serverStatus().locks;
  for (const [type, stats] of Object.entries(locks)) {
    if (stats.acquireWaitCount && Object.values(stats.acquireWaitCount).some(v => v > 0)) {
      printjson({ lockType: type, acquireWaitCount: stats.acquireWaitCount, timeAcquiringMicros: stats.timeAcquiringMicros });
    }
  }
'
```

Expected output: Lock types with non-zero wait counts. High `timeAcquiringMicros` under `Global` indicates server-wide bottleneck; under `Collection` indicates a specific collection hotspot.

### Step 6: Enable profiler and capture slow operations

```javascript
mongosh --eval 'db.setProfilingLevel(1, { slowms: 100 }); print("Profiler ON");'
```

After 2–5 minutes of production traffic, query profiled operations:

```javascript
mongosh --eval '
  db.system.profile.find({ millis: { $gt: 100 } })
    .sort({ ts: -1 }).limit(20)
    .forEach(function(doc) {
      printjson({
        op: doc.op, ns: doc.ns, millis: doc.millis,
        planSummary: doc.planSummary,
        docsExamined: doc.docsExamined, nreturned: doc.nreturned,
        keysExamined: doc.keysExamined
      });
    });
'
```

Expected output: Slow operations with per-query efficiency metrics. `docsExamined / nreturned` ratio above 10 indicates a missing or wrong index.

### Step 7: Examine index coverage on the hot collection

Replace `<DB>` and `<COLLECTION>` with the namespace from Steps 2–3:

```javascript
mongosh --eval '
  db.getSiblingDB("<DB>").<COLLECTION>.aggregate([{ $indexStats: {} }])
    .forEach(function(s) { printjson({ name: s.name, ops: s.accesses.ops, since: s.accesses.since }); });
'
```

Expected output: Index names with access counts since last restart. Indexes with `ops: 0` are unused. Missing compound index for the hot query pattern appears as a `COLLSCAN` in profiler.

### Step 8: Check WiredTiger cache pressure

```javascript
mongosh --eval '
  const c = db.serverStatus().wiredTiger.cache;
  printjson({
    used_bytes: c["bytes currently in the cache"],
    max_bytes: c["maximum bytes configured"],
    app_evictions: c["pages evicted by application threads"],
    dirty_bytes: c["tracked dirty bytes in the cache"]
  });
'
```

Expected output: Cache occupancy values. Rising `pages evicted by application threads` means application threads are doing eviction work, adding latency to every operation.

## Causes

### Cause A: Missing index causing full collection scan

**Statement:** A frequently executed query lacks a supporting index, forcing a full collection scan that holds a WiredTiger ticket for the entire scan duration.

**Mechanism:** WiredTiger uses a ticket-based concurrency system (default 128 read, 128 write tickets). An unindexed query must scan every document in the collection; on large collections this takes seconds and consumes one ticket throughout. When many such queries run concurrently, tickets exhaust and new queries must queue.

**Indicator:**

- [Step 3] `planSummary` contains `"COLLSCAN"` on the hot namespace
- [Step 6] `docsExamined` greatly exceeds `nreturned` in profiler output

<!-- match: {"step": 3, "predicate": "contains", "target": "COLLSCAN"} -->
<!-- match: {"step": 6, "predicate": "threshold", "target": "docsExamined_nreturned_ratio", "op": ">", "value": 10} -->

**Mitigation:**

- **Risk:** Background index builds (MongoDB 4.2+) do not block reads or writes but consume additional disk space and temporarily increase write overhead.
- **Command:**

  ```javascript
  mongosh --eval '
    db.getSiblingDB("<DB>").<COLLECTION>.createIndex(
      { "<FIELD>": 1 },
      { name: "idx_<FIELD>" }
    );
  '
  ```

- **Duration:** Index build time depends on collection size; other operations continue normally.

**Resolution:**

```javascript
mongosh --eval '
  db.getSiblingDB("<DB>").<COLLECTION>.createIndex(
    { "<EQ_FIELD>": 1, "<SORT_FIELD>": -1, "<RANGE_FIELD>": 1 },
    { name: "idx_compound_esr" }
  );
'
```

- **Impact:** Per-collection write overhead increase proportional to number of indexed fields. No restart required.
- **Rollback:** `db.getSiblingDB("<DB>").<COLLECTION>.dropIndex("idx_compound_esr")`

**Verification:** Re-run `explain("executionStats")` on the slow query and confirm `planSummary` shows `IXSCAN` with `docsExamined` close to `nreturned`. Check profiler for `millis` reduction.

### Cause B: DDL operation holding exclusive collection lock

**Statement:** A DDL operation (`dropIndex`, `dropCollection`, `renameCollection`, or `reIndex`) holds an exclusive collection-level lock, blocking all concurrent reads and writes to that namespace.

**Mechanism:** Unlike DML operations which acquire only intent locks, collection-level DDL commands require an exclusive `W` lock on the collection for their duration. All read and write operations against that namespace must queue until the DDL completes. On large collections `reIndex` can hold this lock for minutes.

**Indicator:**

- [Step 2] Multiple operations with `waitingForLock: true` targeting the same `ns`
- [Step 3] A `"command"` type operation with `op.command.dropIndexes` or `op.command.reIndex` present and `secs_running` high
- [Step 5] `Collection` lock type shows high `timeAcquiringMicros`

<!-- match: {"step": 3, "predicate": "contains", "target": "dropIndexes"} -->
<!-- match: {"step": 3, "predicate": "contains", "target": "reIndex"} -->

**Mitigation:**

- **Risk:** Killing a DDL operation leaves it partially complete (e.g., an index partially built). For `dropIndex` this may leave the index in an inconsistent state — verify afterward with `getIndexes()`.
- **Command:**

  ```javascript
  mongosh --eval '
    const op = db.currentOp({ "active": true, "op": "command" }).inprog
      .find(o => o.command && (o.command.dropIndexes || o.command.reIndex));
    if (op) { db.killOp(op.opid); print("Killed: " + op.opid); }
  '
  ```

- **Duration:** Immediate; queue drains within seconds.

**Resolution:**

```javascript
mongosh --eval '
  db.getSiblingDB("<DB>").<COLLECTION>.dropIndex("<INDEX_NAME>");
'
```

Schedule DDL operations during low-traffic maintenance windows. For `reIndex`, prefer `db.runCommand({ reIndex: "<COLLECTION>" })` on a secondary taken out of the replica set rotation.

**Verification:** Run Step 1 again; `currentQueue.total` should return to `0`. Confirm blocked operations resumed via application logs.

### Cause C: WiredTiger ticket pool exhausted

**Statement:** The WiredTiger concurrent-transaction ticket pool is fully consumed by long-running operations, blocking new read or write operations regardless of document-level lock availability.

**Mechanism:** WiredTiger limits simultaneous storage engine transactions to a configurable ticket count (default 128 read, 128 write). Each active read or write operation holds one ticket for its entire duration. When all tickets of one type are out, new operations of that type cannot enter the storage engine and must wait in a queue above the lock layer.

**Indicator:**

- [Step 4] `read.available: 0` or `write.available: 0`
- [Step 1] `currentQueue.readers` or `currentQueue.writers` non-zero while Step 4 shows zero available tickets

<!-- match: {"step": 4, "predicate": "threshold", "target": "read_available", "op": "=", "value": 0} -->
<!-- match: {"step": 4, "predicate": "threshold", "target": "write_available", "op": "=", "value": 0} -->

**Mitigation:**

- **Risk:** Increasing tickets raises CPU and I/O pressure. If the underlying bottleneck is I/O throughput, more tickets worsen latency by increasing I/O contention. Test the new value in staging first.
- **Command:**

  ```javascript
  mongosh --eval '
    db.adminCommand({
      setParameter: 1,
      wiredTigerConcurrentReadTransactions: 256,
      wiredTigerConcurrentWriteTransactions: 256
    });
  '
  ```

- **Duration:** Immediate; no restart required.

**Resolution:**

Add to `mongod.conf` for persistence across restarts:

```yaml
setParameter:
  wiredTigerConcurrentReadTransactions: 256
  wiredTigerConcurrentWriteTransactions: 256
```

- **Impact:** All `mongod` processes must have the setting added. Primary and secondaries must be updated. No rolling restart required for the runtime `setParameter`.
- **Rollback:** `db.adminCommand({ setParameter: 1, wiredTigerConcurrentReadTransactions: 128, wiredTigerConcurrentWriteTransactions: 128 })`

**Verification:** Re-run Step 4 and confirm `available` is greater than `0` under sustained load. Monitor `globalLock.currentQueue` via Step 1 for sustained decrease.

### Cause D: Long-running aggregation pipeline consuming tickets

**Statement:** An unbounded aggregation pipeline holds a WiredTiger ticket for minutes, starving concurrent operations of read capacity.

**Mechanism:** Aggregation pipelines without early `$match` or `$limit` stages process every document in the source collection before applying transformations. On multi-GB collections this can take 30–120 seconds per execution. Each pipeline run holds one read ticket for its entire duration, and concurrent analytical queries stack up until all read tickets are consumed.

**Indicator:**

- [Step 3] `op: "command"` with `command.aggregate` present and `secs_running` above 30
- [Step 4] Read tickets (`read.available`) trending toward `0` during batch/reporting windows

<!-- match: {"step": 3, "predicate": "contains", "target": "aggregate"} -->
<!-- match: {"step": 4, "predicate": "threshold", "target": "read_available", "op": "<", "value": 10} -->

**Mitigation:**

- **Risk:** Killing an analytics pipeline query fails the consumer (reporting job, BI connector session). The caller receives a `MongoError: operation was interrupted` and must retry.
- **Command:**

  ```javascript
  mongosh --eval '
    db.currentOp({ "active": true, "op": "command", "secs_running": { "$gt": 30 } })
      .inprog.filter(o => o.command && o.command.aggregate)
      .forEach(o => { print("Killing " + o.opid); db.killOp(o.opid); });
  '
  ```

- **Duration:** Immediate; use `maxTimeMS` on analytical queries to prevent recurrence.

**Resolution:**

Add `$match` as the first stage and ensure a supporting index exists for that filter:

```javascript
mongosh --eval '
  db.getSiblingDB("<DB>").<COLLECTION>.aggregate([
    { $match: { "<FILTER_FIELD>": { "$gte": "<VALUE>" } } },
    { $limit: 10000 },
    /* ... remaining stages ... */
  ], { maxTimeMS: 30000, allowDiskUse: true });
'
```

**Verification:** Re-run Step 3 after query optimization; `secs_running` for aggregate commands should drop below `5`. Monitor read ticket availability via Step 4 during peak reporting windows.

### Cause E: Large bulk write monopolizing write tickets

**Statement:** A bulk write operation submitting thousands of documents in a single batch holds write tickets for an extended period, blocking concurrent writes.

**Mechanism:** `insertMany` and `bulkWrite` with `ordered: true` process documents serially within a single batch, holding write tickets for the batch duration. A 100,000-document ordered batch can take 10–30 seconds on a loaded server, consuming write tickets and causing concurrent write operations to queue.

**Indicator:**

- [Step 2] Operations with `waitingForLock: true` and `op: "insert"` or `op: "update"` queued behind a long-running `"insert"` or `"bulkWrite"` command
- [Step 4] Write tickets trending to `0` during known data-load windows
- [Step 3] A single `insert` or `command` operation with `secs_running` above `10`

<!-- match: {"step": 4, "predicate": "threshold", "target": "write_available", "op": "<", "value": 10} -->

**Mitigation:**

- **Risk:** Killing an in-flight bulk write rolls back the partial batch at the document level; already-written documents are retained, unwritten documents are dropped. The application must handle deduplication on retry.
- **Command:**

  ```javascript
  mongosh --eval '
    db.currentOp({ "active": true, "secs_running": { "$gt": 10 } }).inprog
      .filter(o => o.op === "insert" || (o.command && o.command.insert))
      .forEach(o => { print("Killing bulk insert " + o.opid); db.killOp(o.opid); });
  '
  ```

- **Duration:** Immediate.

**Resolution:**

Batch bulk writes to 500–1000 documents with unordered mode and a small inter-batch pause (application-side change):

```javascript
// Application pseudocode — implement in your driver
// for each batch of 1000 documents:
//   db.collection.insertMany(batch, { ordered: false })
//   await sleep(10)  // 10ms pause to yield
```

**Verification:** Monitor Step 4 write ticket availability during the next data load; `write.available` should remain above `10` throughout. Check Step 1 queue depth remains near `0`.

### Cause F: Multi-document transaction held open too long

**Statement:** A multi-document transaction is held open beyond `transactionLifetimeLimitSeconds` (default 60 s) or blocked on a slow write, preventing lock release across all affected documents.

**Mechanism:** Multi-document transactions (MongoDB 4.0+) hold intent locks on all touched collections for their entire duration. A transaction blocked on a network call or application-side processing delay retains those locks, preventing concurrent writers from modifying the same documents. Transactions that exceed `transactionLifetimeLimitSeconds` are forcibly aborted but the lock hold during the wait period degrades throughput.

**Indicator:**

- [Step 2] Operations with `desc` containing `"TxnCoordinator"` or `type: "op"` with `waitingForLock: true` and high `secs_running`
- [Symptom] Application logs contain `"Transaction has been aborted"` or `"exceeded time limit"`

<!-- match: {"step": 2, "predicate": "contains", "target": "TxnCoordinator"} -->

**Mitigation:**

- **Risk:** Killing the transaction coordinator aborts the entire transaction; all writes within it are rolled back atomically. The application must retry.
- **Command:**

  ```javascript
  mongosh --eval '
    db.currentOp({ "active": true, "secs_running": { "$gt": 30 } }).inprog
      .filter(o => o.desc && o.desc.includes("Transaction"))
      .forEach(o => { print("Killing transaction op " + o.opid); db.killOp(o.opid); });
  '
  ```

- **Duration:** Immediate; transaction rolled back.

**Resolution:**

Reduce `transactionLifetimeLimitSeconds` to enforce faster failure detection and move non-transactional reads outside transaction boundaries:

```javascript
mongosh --eval '
  db.adminCommand({ setParameter: 1, transactionLifetimeLimitSeconds: 30 });
'
```

- **Impact:** All in-flight transactions exceeding 30 s will be aborted on next checkpoint. Applied to the `mongod` instance at runtime; persist in `mongod.conf` for durability.
- **Rollback:** `db.adminCommand({ setParameter: 1, transactionLifetimeLimitSeconds: 60 })`

**Verification:** Re-run Step 2 after parameter change; no operations with `TxnCoordinator` in `desc` should show `secs_running` above the new limit. Monitor application retry rates.

### Cause G: WiredTiger cache pressure causing application-thread eviction

**Statement:** The WiredTiger internal cache is undersized relative to the active working set, forcing application threads to evict dirty pages before they can complete their operations.

**Mechanism:** WiredTiger maintains an in-memory cache (default: 50% of RAM minus 1 GB, minimum 256 MB). When dirty data in the cache exceeds the eviction trigger threshold (default 20% of cache), background eviction threads run. When dirty data exceeds the hard limit (default 80%), application threads are co-opted to perform eviction before proceeding, adding latency to every operation proportional to the eviction workload.

**Indicator:**

- [Step 8] `pages evicted by application threads` is non-zero and increasing between polls
- [Step 8] `used_bytes / max_bytes` ratio above 0.95

<!-- match: {"step": 8, "predicate": "threshold", "target": "cache_utilization_ratio", "op": ">", "value": 0.95} -->
<!-- match: {"step": 8, "predicate": "threshold", "target": "app_evictions", "op": ">", "value": 0} -->

**Mitigation:**

- **Risk:** Increasing cache size reduces memory available to the OS page cache, which WiredTiger relies on for data not in its own cache. On memory-constrained hosts this can cause OS-level memory pressure.
- **Command:**

  ```javascript
  mongosh --eval '
    db.adminCommand({
      setParameter: 1,
      "wiredTigerEngineRuntimeConfig": "cache_size=8G"
    });
  '
  ```

- **Duration:** Applied immediately at runtime; no restart required.

**Resolution:**

Set `storage.wiredTiger.engineConfig.cacheSizeGB` in `mongod.conf` to 50–60% of available RAM:

```yaml
storage:
  wiredTiger:
    engineConfig:
      cacheSizeGB: 8
```

- **Impact:** Requires `mongod` restart to apply from config file. Perform as a rolling restart on replica set members.
- **Rollback:** Reduce `cacheSizeGB` back to previous value and rolling-restart.

**Verification:** Re-run Step 8 after tuning; `pages evicted by application threads` should drop to `0` or near `0` under normal load. Monitor p99 latency via application metrics for sustained improvement.

### Cause Z: Unidentified lock contention source

**Statement:** Lock contention or ticket exhaustion is confirmed but none of the specific causes above match the diagnostic output.

**Mechanism:** MongoDB lock contention can arise from combinations of factors not individually identifiable via the steps above, including schema anti-patterns (unbounded arrays, documents exceeding 1 MB), write-skew in concurrent transactions, shard-key hotspots in sharded clusters, or time-series bucket locking. Further investigation with FTDC diagnostic data or MongoDB Atlas Advisor is required.

**Indicator:**

- [Default] Steps 1–8 confirm contention but no cause above matches the specific symptom pattern

**Mitigation:**

- **Risk:** Low. Diagnostic-only actions.
- **Command:**

  ```javascript
  mongosh --eval '
    db.adminCommand({ getDiagnosticData: 1 });
  '
  ```

- **Duration:** Collect 10–15 minutes of FTDC data for MongoDB support analysis.

**Resolution:** Out of runbook scope — escalate to MongoDB support or Atlas Advisor with FTDC diagnostic archive and `mongod.log` covering the incident window.

**Verification:** Confirmed by resolution of latency spike and drop of `globalLock.currentQueue.total` to `0` after support-recommended fix is applied.

## Prevention

1. **Index all production query patterns.** Run `explain("executionStats")` on every frequently executed query during development. Alert when `system.profile` shows `planSummary: COLLSCAN` on collections exceeding 100,000 documents.

2. **Monitor WiredTiger ticket availability.** Alert when `wiredTiger.concurrentTransactions.read.available` or `write.available` drops below 10% of `totalTickets` (`< 13` at default of 128). This is the earliest leading indicator of ticket exhaustion before latency spikes appear.

3. **Keep transactions short and focused.** Limit multi-document transactions to the minimum writes required. Move non-transactional reads outside transaction boundaries. Set `transactionLifetimeLimitSeconds` to `30` in production.

4. **Set `maxTimeMS` on all application queries.** Configure a default operation timeout in the application driver (recommended: 5000–30000 ms depending on SLA) to prevent unbounded queries from consuming tickets indefinitely.

5. **Schedule DDL during maintenance windows.** Index builds, collection drops, and `reIndex` acquire exclusive collection locks. Even background index builds increase write overhead; plan them for low-traffic periods.

6. **Route analytics reads to secondaries.** Configure `readPreference: "secondaryPreferred"` for reporting and aggregation queries to offload read ticket pressure from the primary.

7. **Right-size the WiredTiger cache.** Target `cacheSizeGB` at 50–60% of available RAM. Monitor `pages evicted by application threads` and scale up if non-zero under sustained load.

8. **Shard write-heavy collections proactively.** Choose a shard key with high cardinality that distributes writes evenly (e.g., hashed `_id`). Shard before the single-primary bottleneck emerges, not after.

9. **Limit bulk write batch sizes.** Cap `insertMany` and `bulkWrite` batches at 500–1000 documents. Use `ordered: false` for idempotent inserts to allow server-side parallelism.

10. **Disable the profiler after investigation.** The profiler adds write overhead to `system.profile` for every sampled operation. Run `db.setProfilingLevel(0)` immediately after capturing the data needed for diagnosis.

## Sources

- [MongoDB Manual — Analyzing MongoDB Performance](https://www.mongodb.com/docs/manual/administration/analyzing-mongodb-performance/) — lock contention metrics, globalLock, WiredTiger tickets, connection monitoring
- [MongoDB Manual — FAQ: Concurrency](https://www.mongodb.com/docs/manual/faq/concurrency/) — multi-granularity locking, DDL exclusive locks, intent lock types, lock-free reads (5.0+), sharding concurrency
- [MongoDB Manual — Manage the Database Profiler](https://www.mongodb.com/docs/manual/tutorial/manage-the-database-profiler/) — profiling levels, slowms, system.profile field reference, docsExamined/nreturned interpretation
