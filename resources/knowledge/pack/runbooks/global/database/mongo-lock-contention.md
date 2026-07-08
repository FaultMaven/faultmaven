---
id: "mongo-lock-contention"
title: "MongoDB Lock Contention and Slow Operations"
domain: database
service: mongodb
symptom_class: [latency, timeout]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

**Statement:** A frequently executed query lacks a supporting index, forcing a full collection scan that holds a WiredTiger ticket for its entire duration.

**Chain:**
- root: A frequently executed query has no supporting index for its filter.
- s1: The query falls back to a COLLSCAN, reading every document in the collection.
- s2: The scan holds one WiredTiger read ticket for seconds on large collections.
- s3: Concurrent unindexed queries exhaust the ticket pool; new operations queue.
- D: p99 latency spikes and operations queue for lock/ticket access (Symptom).

**Indicators:**
- s1: [Step 3] `planSummary` contains `"COLLSCAN"` on the hot namespace
- s1: [Step 6] `docsExamined` greatly exceeds `nreturned` in profiler output

**Interventions:**
- **remediation** (root): create a compound index following the equality-sort-range (ESR) rule for the hot query pattern. No restart required; per-collection write overhead increases proportional to indexed-field count.

  ```javascript
  mongosh --eval '
    db.getSiblingDB("<DB>").<COLLECTION>.createIndex(
      { "<EQ_FIELD>": 1, "<SORT_FIELD>": -1, "<RANGE_FIELD>": 1 },
      { name: "idx_compound_esr" }
    );
  '
  ```

  **Verification:** Re-run `explain("executionStats")` on the slow query; confirm `planSummary` shows `IXSCAN` with `docsExamined` close to `nreturned`. Check profiler for `millis` reduction. Rollback: `dropIndex("idx_compound_esr")`.
- **mitigation** (s1): build a single-field index online to relieve the immediate COLLSCAN while the ESR index is designed.

  ```javascript
  mongosh --eval '
    db.getSiblingDB("<DB>").<COLLECTION>.createIndex(
      { "<FIELD>": 1 },
      { name: "idx_<FIELD>" }
    );
  '
  ```

  **Risk:** Background index builds (MongoDB 4.2+) do not block reads or writes but consume extra disk space and temporarily increase write overhead. **Duration:** Index build time depends on collection size; other operations continue normally. **Verification:** Re-run Step 6; confirm the query's `docsExamined`/`nreturned` ratio drops.

### Cause B: DDL operation holding exclusive collection lock

**Statement:** A DDL operation (`dropIndex`, `dropCollection`, `renameCollection`, or `reIndex`) holds an exclusive collection-level lock, blocking all concurrent reads and writes to that namespace.

**Chain:**
- root: A DDL command (dropIndex/dropCollection/renameCollection/reIndex) is running.
- s1: DDL acquires an exclusive W lock on the collection for its full duration.
- s2: All reads and writes against that namespace queue behind the W lock.
- s3: On large collections reIndex holds the lock for minutes, draining throughput.
- D: p99 latency spikes and operations queue for lock access (Symptom).

**Indicators:**
- s2: [Step 2] Multiple operations with `waitingForLock: true` targeting the same `ns`
- root: [Step 3] A `command` op with `command.dropIndexes` or `command.reIndex` and high `secs_running`
- s1: [Step 5] `Collection` lock type shows high `timeAcquiringMicros`

**Interventions:**
- **remediation** (root): schedule DDL during low-traffic maintenance windows. For `reIndex`, prefer running it on a secondary taken out of replica-set rotation rather than the primary.

  ```javascript
  mongosh --eval '
    db.getSiblingDB("<DB>").<COLLECTION>.dropIndex("<INDEX_NAME>");
  '
  ```

  **Verification:** Run Step 1 again; `currentQueue.total` should return to `0`. Confirm blocked operations resumed via application logs.
- **mitigation** (root): kill the in-flight DDL operation to immediately drain the queue.

  ```javascript
  mongosh --eval '
    const op = db.currentOp({ "active": true, "op": "command" }).inprog
      .find(o => o.command && (o.command.dropIndexes || o.command.reIndex));
    if (op) { db.killOp(op.opid); print("Killed: " + op.opid); }
  '
  ```

  **Risk:** Killing a DDL operation leaves it partially complete (e.g. an index partially built); for `dropIndex` this may leave the index inconsistent — verify afterward with `getIndexes()`. **Duration:** Immediate; queue drains within seconds. **Verification:** Re-run Step 1; `currentQueue.total` returns to `0`.

### Cause C: WiredTiger ticket pool exhausted

**Statement:** The WiredTiger concurrent-transaction ticket pool is fully consumed by long-running operations, blocking new read or write operations regardless of document-level lock availability.

**Chain:**
- root: Long-running operations consume all WiredTiger tickets of one type.
- s1: WiredTiger caps simultaneous transactions (default 128 read, 128 write).
- s2: Each active operation holds one ticket for its entire duration.
- s3: With zero tickets available, new ops of that type cannot enter the engine and queue above the lock layer.
- D: p99 latency spikes and operations queue regardless of lock availability (Symptom).

**Indicators:**
- s3: [Step 4] `read.available: 0` or `write.available: 0`
- s3: [Step 1] `currentQueue.readers` or `currentQueue.writers` non-zero while Step 4 shows zero available tickets

**Interventions:**
- **remediation** (root): raise the ticket ceiling persistently by adding it to `mongod.conf` on every member.

  ```yaml
  setParameter:
    wiredTigerConcurrentReadTransactions: 256
    wiredTigerConcurrentWriteTransactions: 256
  ```

  **Verification:** Re-run Step 4 and confirm `available` is greater than `0` under sustained load. Monitor `globalLock.currentQueue` via Step 1 for a sustained decrease. Rollback: reset both parameters to `128`.
- **mitigation** (root): raise the ticket count at runtime via `setParameter` for immediate relief, no restart required.

  ```javascript
  mongosh --eval '
    db.adminCommand({
      setParameter: 1,
      wiredTigerConcurrentReadTransactions: 256,
      wiredTigerConcurrentWriteTransactions: 256
    });
  '
  ```

  **Risk:** More tickets raise CPU and I/O pressure; if the real bottleneck is I/O throughput, more tickets worsen latency. Test the new value in staging first. **Duration:** Immediate; no restart required. **Verification:** Re-run Step 4; `available` stays above `0` under load.

### Cause D: Long-running aggregation pipeline consuming tickets

**Statement:** An unbounded aggregation pipeline holds a WiredTiger ticket for minutes, starving concurrent operations of read capacity.

**Chain:**
- root: An aggregation pipeline runs without an early `$match` or `$limit` stage.
- s1: The pipeline scans every document in the source collection before transforming.
- s2: On multi-GB collections each run takes 30–120s, holding one read ticket throughout.
- s3: Concurrent analytical queries stack up until read tickets are exhausted.
- D: p99 latency spikes and read-side operations queue (Symptom).

**Indicators:**
- root: [Step 3] `op: "command"` with `command.aggregate` present and `secs_running` above 30
- s3: [Step 4] read tickets (`read.available`) trending toward `0` during batch/reporting windows

**Interventions:**
- **remediation** (root): add `$match` as the first stage, ensure a supporting index exists for that filter, and bound the pipeline with `$limit` and `maxTimeMS`.

  ```javascript
  mongosh --eval '
    db.getSiblingDB("<DB>").<COLLECTION>.aggregate([
      { $match: { "<FILTER_FIELD>": { "$gte": "<VALUE>" } } },
      { $limit: 10000 },
      /* ... remaining stages ... */
    ], { maxTimeMS: 30000, allowDiskUse: true });
  '
  ```

  **Verification:** Re-run Step 3 after optimization; `secs_running` for aggregate commands should drop below `5`. Monitor read-ticket availability via Step 4 during peak reporting windows.
- **mitigation** (root): kill the long-running aggregation to immediately release its read ticket.

  ```javascript
  mongosh --eval '
    db.currentOp({ "active": true, "op": "command", "secs_running": { "$gt": 30 } })
      .inprog.filter(o => o.command && o.command.aggregate)
      .forEach(o => { print("Killing " + o.opid); db.killOp(o.opid); });
  '
  ```

  **Risk:** Killing an analytics pipeline fails the consumer (reporting job, BI connector session); the caller receives `MongoError: operation was interrupted` and must retry. **Duration:** Immediate; use `maxTimeMS` on analytical queries to prevent recurrence. **Verification:** Re-run Step 4; read-ticket availability recovers.

### Cause E: Large bulk write monopolizing write tickets

**Statement:** A bulk write operation submitting thousands of documents in a single batch holds write tickets for an extended period, blocking concurrent writes.

**Chain:**
- root: A bulk write submits thousands of documents in one ordered batch.
- s1: `insertMany`/`bulkWrite` with `ordered: true` processes documents serially.
- s2: A 100,000-doc ordered batch holds write tickets for 10–30s on a loaded server.
- s3: Concurrent write operations queue behind the long-running batch.
- D: p99 latency spikes and writes queue during data-load windows (Symptom).

**Indicators:**
- s3: [Step 2] ops with `waitingForLock: true` and `op: "insert"`/`"update"` queued behind a long-running insert/bulkWrite
- s2: [Step 4] write tickets trending to `0` during known data-load windows
- s2: [Step 3] a single `insert` or `command` operation with `secs_running` above `10`

**Interventions:**
- **remediation** (root): batch bulk writes to 500–1000 documents with unordered mode and a small inter-batch pause (application-side change).

  ```javascript
  // Application pseudocode — implement in your driver
  // for each batch of 1000 documents:
  //   db.collection.insertMany(batch, { ordered: false })
  //   await sleep(10)  // 10ms pause to yield
  ```

  **Verification:** Monitor Step 4 write-ticket availability during the next data load; `write.available` should remain above `10` throughout. Check Step 1 queue depth remains near `0`.
- **mitigation** (s2): kill the in-flight bulk write to immediately release write tickets.

  ```javascript
  mongosh --eval '
    db.currentOp({ "active": true, "secs_running": { "$gt": 10 } }).inprog
      .filter(o => o.op === "insert" || (o.command && o.command.insert))
      .forEach(o => { print("Killing bulk insert " + o.opid); db.killOp(o.opid); });
  '
  ```

  **Risk:** Killing an in-flight bulk write rolls back the partial batch at the document level; written documents are retained, unwritten ones dropped. The application must handle deduplication on retry. **Duration:** Immediate. **Verification:** Re-run Step 4; write-ticket availability recovers.

### Cause F: Multi-document transaction held open too long

**Statement:** A multi-document transaction is held open beyond `transactionLifetimeLimitSeconds` (default 60 s) or blocked on a slow write, preventing lock release across all affected documents.

**Chain:**
- root: A multi-document transaction stays open beyond its expected lifetime.
- s1: The transaction holds intent locks on all touched collections for its duration.
- s2: Blocked on a network call or app-side delay, it retains those locks.
- s3: Concurrent writers to the same documents are blocked until lock release.
- D: Transaction timeout/abort errors and write queuing (Symptom).

**Indicators:**
- s1: [Step 2] ops with `desc` containing `"TxnCoordinator"`, or `type: "op"` with `waitingForLock: true` and high `secs_running`
- D: [Symptom] application logs contain `"Transaction has been aborted"` or `"exceeded time limit"`

**Interventions:**
- **remediation** (root): reduce `transactionLifetimeLimitSeconds` to enforce faster failure detection and move non-transactional reads outside transaction boundaries. Persist in `mongod.conf` for durability.

  ```javascript
  mongosh --eval '
    db.adminCommand({ setParameter: 1, transactionLifetimeLimitSeconds: 30 });
  '
  ```

  **Verification:** Re-run Step 2 after the change; no `TxnCoordinator` op should show `secs_running` above the new limit. Monitor application retry rates. Rollback: reset `transactionLifetimeLimitSeconds` to `60`.
- **mitigation** (s2): kill the stuck transaction coordinator to release its intent locks immediately.

  ```javascript
  mongosh --eval '
    db.currentOp({ "active": true, "secs_running": { "$gt": 30 } }).inprog
      .filter(o => o.desc && o.desc.includes("Transaction"))
      .forEach(o => { print("Killing transaction op " + o.opid); db.killOp(o.opid); });
  '
  ```

  **Risk:** Killing the transaction coordinator aborts the entire transaction; all writes within it roll back atomically. The application must retry. **Duration:** Immediate; transaction rolled back. **Verification:** Re-run Step 2; no long-running transaction ops remain.

### Cause G: WiredTiger cache pressure causing application-thread eviction

**Statement:** The WiredTiger internal cache is undersized relative to the active working set, forcing application threads to evict dirty pages before they can complete their operations.

**Chain:**
- root: The WiredTiger cache is undersized relative to the active working set.
- s1: Dirty data exceeds the eviction trigger (default 20% of cache), running background eviction.
- s2: Dirty data exceeds the hard limit (default 80%), co-opting application threads to evict.
- s3: Every operation pays eviction latency proportional to the eviction workload.
- D: p99 latency spikes across all operations (Symptom).

**Indicators:**
- s2: [Step 8] `pages evicted by application threads` is non-zero and increasing between polls
- s1: [Step 8] `used_bytes / max_bytes` ratio above 0.95

**Interventions:**
- **remediation** (root): set `storage.wiredTiger.engineConfig.cacheSizeGB` in `mongod.conf` to 50–60% of available RAM. Requires a `mongod` restart; perform as a rolling restart on replica-set members.

  ```yaml
  storage:
    wiredTiger:
      engineConfig:
        cacheSizeGB: 8
  ```

  **Verification:** Re-run Step 8 after tuning; `pages evicted by application threads` should drop to `0` or near `0` under normal load. Monitor p99 latency for sustained improvement. Rollback: reduce `cacheSizeGB` and rolling-restart.
- **mitigation** (root): raise the cache size at runtime for immediate relief, no restart required.

  ```javascript
  mongosh --eval '
    db.adminCommand({
      setParameter: 1,
      "wiredTigerEngineRuntimeConfig": "cache_size=8G"
    });
  '
  ```

  **Risk:** Increasing cache size reduces RAM for the OS page cache that WiredTiger relies on for out-of-cache data; on memory-constrained hosts this can cause OS-level memory pressure. **Duration:** Applied immediately at runtime; no restart required. **Verification:** Re-run Step 8; application-thread evictions trend toward `0`.

### Cause Z: Unidentified lock contention source

**Statement:** Lock contention or ticket exhaustion is confirmed but none of the specific causes above match the diagnostic output.

**Chain:**
- root: Contention is confirmed but matches no specific cause above (schema anti-patterns, write-skew, shard-key hotspots, time-series bucket locking).
- D: p99 latency spikes and operations queue for lock/ticket access (Symptom).

**Indicators:**
- root: [Default] Steps 1–8 confirm contention but no cause above matches the specific symptom pattern

**Interventions:**
- **mitigation** (D): capture a full FTDC diagnostic snapshot and escalate to MongoDB support or Atlas Advisor with the FTDC archive plus `mongod.log` covering the incident window.

  ```javascript
  mongosh --eval '
    db.adminCommand({ getDiagnosticData: 1 });
  '
  ```

  **Risk:** Low. Diagnostic-only actions. **Duration:** Collect 10–15 minutes of FTDC data for MongoDB support analysis. **Verification:** Confirmed by resolution of the latency spike and drop of `globalLock.currentQueue.total` to `0` after the support-recommended fix is applied.

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
