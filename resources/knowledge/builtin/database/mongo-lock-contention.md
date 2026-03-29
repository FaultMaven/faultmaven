---
id: mongo-lock-contention
title: "MongoDB Lock Contention and Slow Operations"
domain: database
service: mongodb
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
  - mongodb
  - lock-contention
  - slow-queries
  - profiling
  - wiredtiger
  - performance
difficulty: intermediate
---

# MongoDB Lock Contention and Slow Operations

## Problem Definition

This runbook covers MongoDB deployments (versions 4.4 through 7.x) using the WiredTiger storage engine that are experiencing lock contention and slow operations. It applies to standalone, replica set, and sharded cluster topologies, including MongoDB Atlas and self-managed deployments. You need `mongosh` access with privileges to run `db.serverStatus()`, `db.currentOp()`, `db.setProfilingLevel()`, and `db.killOp()`. Access to the MongoDB log file (`mongod.log`) and FTDC diagnostic data is also required.

MongoDB lock contention occurs when multiple operations compete for locks on the same resources, causing operations to queue and latency to spike. WiredTiger uses document-level concurrency control for write operations, allowing multiple clients to modify different documents in the same collection simultaneously. However, collection-level and database-level exclusive locks still occur for certain DDL operations (such as `createIndex` in foreground mode, `renameCollection`, and `dropDatabase`). WiredTiger also uses a ticket-based concurrency control system (default 128 read and 128 write tickets) that limits simultaneous operations processed by the storage engine -- when tickets drop to zero, new operations must queue regardless of lock availability.

**Common symptoms:**

- Elevated query response times (p99 latency spikes)
- `db.currentOp()` shows many operations in `waitingForLock` state
- `serverStatus.globalLock.currentQueue.total` is consistently non-zero
- Application timeouts or connection pool exhaustion due to slow responses
- Write operations queue behind long-running reads or vice versa
- `mongotop` shows high lock percentages on specific collections
- Slow query warnings in the MongoDB log (operations exceeding `slowms` threshold)
- WiredTiger read or write tickets drop to zero (`serverStatus.wiredTiger.concurrentTransactions`)
- High `timeAcquiringMicros` values in `serverStatus.locks` indicating operations waiting for locks

**Common root causes:**

- Missing indexes causing collection scans (COLLSCAN) that hold locks longer
- Long-running aggregation pipelines or map-reduce operations consuming tickets
- Large bulk write operations that monopolize write tickets
- DDL operations (foreground `createIndex`, `renameCollection`, `dropDatabase`) holding exclusive locks
- Unoptimized queries reading excessive documents (`docsExamined` >> `nreturned`)
- Write-heavy workloads on a single collection without sharding
- WiredTiger cache pressure causing application-thread eviction stalls
- Schema design issues (large documents, unbounded arrays, excessive embedding)
- Ticket exhaustion in WiredTiger concurrency control (read/write tickets at zero)
- Multi-document transactions held open too long, preventing lock release

## Diagnostic Steps

### Step 1: Check global lock state and queue depth

**What this checks:** The number of active clients and queued operations at the global lock level, indicating whether operations are waiting for resources.

```bash
mongosh --eval '
  const status = db.serverStatus();
  printjson({
    activeClients: status.globalLock.activeClients,
    currentQueue: status.globalLock.currentQueue,
    totalTime: status.globalLock.totalTime
  });
'
```

**Expected output:** A JSON object showing `activeClients.readers`, `activeClients.writers`, `currentQueue.readers`, and `currentQueue.writers` counts.

**What the finding means:** If `currentQueue.readers` or `currentQueue.writers` is consistently above 0, operations are waiting for locks. A high `activeClients.total` with a corresponding high queue indicates the server is saturated. The `totalTime` field (in microseconds) shows total time the global lock has been held since server start.

### Step 2: Examine operations waiting for locks

**What this checks:** Which specific operations are blocked and which namespace (database.collection) they are targeting, revealing the contention hotspot.

```bash
mongosh --eval '
  db.currentOp({
    "waitingForLock": true
  }).inprog.forEach(function(op) {
    printjson({
      opid: op.opid,
      type: op.type,
      op: op.op,
      ns: op.ns,
      secs_running: op.secs_running,
      waitingForLock: op.waitingForLock,
      desc: op.desc
    });
  });
'
```

**Expected output:** A list of operations with their operation ID, type, namespace, and wait duration.

**What the finding means:** If many operations target the same namespace, that collection is the contention hotspot. The `op` field shows the operation type (`query`, `insert`, `update`, `remove`, `command`) which helps identify whether reads or writes are blocked. Many operations queuing on the same collection typically points to a missing index or a long-running DDL operation.

### Step 3: Identify active slow operations

**What this checks:** Currently running operations that have been active for more than 5 seconds, including their query plan to identify missing indexes.

```bash
mongosh --eval '
  db.currentOp({
    "active": true,
    "secs_running": { "$gt": 5 }
  }).inprog.forEach(function(op) {
    printjson({
      opid: op.opid,
      op: op.op,
      ns: op.ns,
      secs_running: op.secs_running,
      command: op.command,
      planSummary: op.planSummary
    });
  });
'
```

**Expected output:** A list of long-running operations with their query plan summary.

**What the finding means:** Operations with `planSummary: "COLLSCAN"` are performing full collection scans due to missing indexes -- this is the single most common cause of lock contention. Operations with `planSummary: "IXSCAN"` but high `secs_running` may indicate suboptimal index choice or very large result sets. Long-running operations are the most common culprit for lock contention because each operation holds a ticket for its entire duration.

### Step 4: Check WiredTiger concurrency tickets

**What this checks:** Whether the WiredTiger ticket system is exhausted, which throttles new operations regardless of document-level lock availability.

```bash
mongosh --eval '
  const status = db.serverStatus();
  printjson({
    read: status.wiredTiger.concurrentTransactions.read,
    write: status.wiredTiger.concurrentTransactions.write
  });
'
```

**Expected output:** JSON showing `totalTickets`, `available`, and `out` for both read and write categories.

**What the finding means:** Default is 128 read tickets and 128 write tickets. If `available` is 0 for either category, all tickets are consumed and new operations of that type must wait. This is distinct from lock contention -- ticket exhaustion means the storage engine itself is at capacity. Each read/write operation uses a ticket for its entire duration, so long-running operations consume tickets disproportionately.

### Step 5: Check lock acquisition wait times

**What this checks:** How much time operations spend waiting to acquire locks, which is the direct measure of latency caused by lock contention.

```bash
mongosh --eval '
  const locks = db.serverStatus().locks;
  for (const [lockType, stats] of Object.entries(locks)) {
    if (stats.acquireWaitCount) {
      printjson({
        lockType: lockType,
        acquireWaitCount: stats.acquireWaitCount,
        timeAcquiringMicros: stats.timeAcquiringMicros
      });
    }
  }
'
```

**Expected output:** Lock types with their wait counts and total time spent waiting (in microseconds).

**What the finding means:** High `timeAcquiringMicros` values indicate significant lock contention at that level. `Global` lock waits indicate server-wide bottlenecks. `Database` lock waits suggest DDL operations or database-level commands. `Collection` lock waits point to specific collection hotspots.

### Step 6: Enable the database profiler and review slow operations

**What this checks:** Detailed per-operation metrics for any operation exceeding the configured threshold, including documents examined versus returned.

```bash
mongosh --eval '
  db.setProfilingLevel(1, { slowms: 100 });
  print("Profiler enabled. Slow operations (>100ms) will be logged.");
'
```

Review profiled operations:

```bash
mongosh --eval '
  db.system.profile.find().sort({ ts: -1 }).limit(20).forEach(function(doc) {
    printjson({
      op: doc.op,
      ns: doc.ns,
      millis: doc.millis,
      planSummary: doc.planSummary,
      keysExamined: doc.keysExamined,
      docsExamined: doc.docsExamined,
      nreturned: doc.nreturned,
      command: doc.command
    });
  });
'
```

**Expected output:** A list of slow operations with timing and document-scan metrics.

**What the finding means:** If `docsExamined` is much larger than `nreturned`, the query is scanning many documents unnecessarily -- a strong indicator of a missing or suboptimal index. A `docsExamined/nreturned` ratio above 10:1 warrants index investigation. The `millis` field is the total operation time including lock wait.

### Step 7: Check index usage on the hot collection

**What this checks:** Which indexes exist on the contended collection and whether they are actually being used by queries.

```bash
mongosh --eval '
  db.<COLLECTION>.getIndexes().forEach(function(idx) { printjson(idx); });
'
```

```bash
mongosh --eval '
  db.<COLLECTION>.aggregate([{ $indexStats: {} }]).forEach(function(stat) {
    printjson({
      name: stat.name,
      accesses_ops: stat.accesses.ops,
      accesses_since: stat.accesses.since
    });
  });
'
```

**Expected output:** A list of indexes with their key patterns and usage counts since last server restart.

**What the finding means:** Indexes with zero `accesses.ops` are unused and add write overhead without benefiting reads. Missing indexes on frequently queried field patterns force collection scans. The ESR (Equality, Sort, Range) rule should guide compound index design.

### Step 8: Check WiredTiger cache status

**What this checks:** Whether the WiredTiger internal cache is under pressure, causing application threads to perform eviction work that adds latency to every operation.

```bash
mongosh --eval '
  const status = db.serverStatus();
  const cache = status.wiredTiger.cache;
  printjson({
    "bytes currently in cache": cache["bytes currently in the cache"],
    "maximum bytes configured": cache["maximum bytes configured"],
    "pages evicted by app threads": cache["pages evicted by application threads"],
    "dirty bytes in cache": cache["tracked dirty bytes in the cache"]
  });
'
```

**Expected output:** Cache size metrics and eviction counts.

**What the finding means:** If `pages evicted by application threads` is high and increasing, WiredTiger cache is under pressure and application threads are being drafted to do eviction work. This adds latency to all operations because each operation must evict a page before it can proceed. The cache should be sized to hold the active working set.

## Mitigation

### Option 1: Kill long-running blocking operations

Use when a specific long-running operation is holding locks and blocking other operations.

- **Risk:** Moderate. The killed operation will not complete. If it is a write, the operation is rolled back at the document level. If it is part of a multi-document transaction, the entire transaction is aborted. The client will receive an error and should retry.
- **Command:**

```bash
mongosh --eval '
  db.currentOp({ "active": true, "secs_running": { "$gt": 30 } }).inprog.forEach(function(op) {
    print("Killing operation: " + op.opid + " running for " + op.secs_running + "s on " + op.ns);
    db.killOp(op.opid);
  });
'
```

- **Verify:**

```bash
mongosh --eval 'printjson(db.serverStatus().globalLock.currentQueue);'
# Expected: readers and writers back to 0 or near 0
```

- **Duration:** Immediate.

### Option 2: Add missing indexes for collection-scanning queries

Use when the profiler or `currentOp` shows COLLSCAN on frequently queried collections.

- **Risk:** Low. Background index builds (default in MongoDB 4.2+) do not block read/write operations. Adding an index increases write overhead proportional to the number of indexed fields and consumes additional disk space.
- **Command:**

```bash
mongosh --eval '
  db.<COLLECTION>.createIndex(
    { "<FIELD>": 1 },
    { background: true, name: "idx_<FIELD>" }
  );
'
```

For compound queries, follow the ESR rule (Equality first, Sort second, Range last):

```bash
mongosh --eval '
  db.<COLLECTION>.createIndex(
    { "<EQUALITY_FIELD>": 1, "<SORT_FIELD>": -1, "<RANGE_FIELD>": 1 },
    { background: true, name: "idx_compound" }
  );
'
```

- **Verify:**

```bash
mongosh --eval '
  db.<COLLECTION>.find({ "<FIELD>": "<VALUE>" }).explain("executionStats").executionStats;
'
# Expected: planSummary shows IXSCAN instead of COLLSCAN, docsExamined close to nreturned
```

- **Duration:** Seconds to minutes depending on collection size.

### Option 3: Increase WiredTiger concurrency tickets

Use when ticket exhaustion is confirmed (available tickets at 0 for read or write).

- **Risk:** Moderate. Higher concurrency increases CPU and memory pressure. If the underlying bottleneck is I/O, more tickets may worsen latency by increasing contention on the storage layer. Test in staging first.
- **Command:**

```bash
mongosh --eval '
  db.adminCommand({
    setParameter: 1,
    wiredTigerConcurrentReadTransactions: 256,
    wiredTigerConcurrentWriteTransactions: 256
  });
'
```

For persistence across restarts, add to `mongod.conf`:

```yaml
setParameter:
  wiredTigerConcurrentReadTransactions: 256
  wiredTigerConcurrentWriteTransactions: 256
```

- **Verify:**

```bash
mongosh --eval '
  const ct = db.serverStatus().wiredTiger.concurrentTransactions;
  printjson({ read: ct.read, write: ct.write });
'
# Expected: totalTickets increased to 256, available > 0
```

- **Duration:** Immediate (no restart needed for `setParameter` at runtime).

### Option 4: Reduce batch sizes for bulk operations

Use when large bulk write operations are monopolizing write tickets and blocking concurrent access.

- **Risk:** Low. Smaller batches take more total wall-clock time but allow interleaving with other operations, reducing queue depth and improving p99 latency for concurrent queries.
- **Command:**

Application-level change -- break bulk operations into batches of 500-1000 documents with a small pause between batches:

```bash
# Pseudocode — implement in your application driver
# for each batch of 1000 documents:
#   db.collection.insertMany(batch, { ordered: false })
#   sleep(10ms)
```

- **Verify:**

```bash
mongosh --eval 'printjson(db.serverStatus().globalLock.currentQueue);'
# Expected: reduced queue lengths during bulk operations
```

- **Duration:** Immediate after application code change is deployed.

### Option 5: Set operation time limits

Use when unbounded queries or aggregations risk holding tickets indefinitely.

- **Risk:** Low. Operations exceeding the time limit will fail with an error. The application must handle the failure gracefully.
- **Command:**

```bash
# Set a default operation timeout at the collection level
mongosh --eval '
  db.runCommand({
    profile: 1,
    slowms: 100,
    sampleRate: 1.0
  });
'

# Per-query timeout (application-side, using maxTimeMS)
# db.collection.find({...}).maxTimeMS(5000)
```

- **Verify:**

```bash
mongosh --eval '
  db.currentOp({ "active": true, "secs_running": { "$gt": 30 } }).inprog.length;
'
# Expected: 0 or very few long-running operations
```

- **Duration:** Immediate after application code change.

## Root Cause Resolution

**If** profiler shows COLLSCAN on high-traffic queries --> create indexes that cover the query pattern. Use `explain("executionStats")` to verify the index is selected and that `docsExamined` is close to `nreturned`. Follow the ESR rule for compound index field ordering.

**If** long-running aggregation pipelines block other operations --> add `$match` and `$limit` stages as early as possible in the pipeline to reduce the number of documents processed. Use `allowDiskUse: true` for large aggregations to avoid memory limits, but be aware this increases I/O load.

**If** WiredTiger cache is undersized --> increase `storage.wiredTiger.engineConfig.cacheSizeGB` to 50-60% of available RAM (default is 50% of RAM minus 1 GB). This requires a `mongod` restart.

**If** a write-heavy single collection is the bottleneck --> shard the collection to distribute writes across multiple shards. Choose a shard key with high cardinality that distributes writes evenly (for example, a hashed `_id` field).

**If** foreground index builds cause lock contention --> use background index builds (default in MongoDB 4.2+). For MongoDB 4.0 and earlier, explicitly specify `{ background: true }`.

**If** large documents (> 1 MB) slow down read/write operations --> redesign the schema. Break large embedded arrays into separate collections with references. Use the bucket pattern for time-series data.

**If** unoptimized regex queries scan entire collections --> anchor regex patterns with a `^` prefix (for example, `/^prefix/`) which allows index use. For full-text search, use Atlas Search or a text index.

**If** multi-document transactions are held open too long --> reduce transaction scope to the minimum necessary writes. Move read operations outside transaction boundaries where possible. Set `transactionLifetimeLimitSeconds` to prevent runaway transactions (default 60 seconds).

**If** connection pool exhaustion causes timeouts --> increase the connection pool size in the application driver configuration (default varies by driver, typically 100). Ensure pool size does not exceed MongoDB `net.maxIncomingConnections` (default 65536).

## Verification

After applying fixes, confirm lock contention has decreased:

```bash
# 1. Check global lock queue is clear
mongosh --eval 'printjson(db.serverStatus().globalLock.currentQueue);'
# Expected: { total: 0, readers: 0, writers: 0 }

# 2. Verify no operations waiting for locks
mongosh --eval 'print(db.currentOp({ "waitingForLock": true }).inprog.length + " operations waiting");'
# Expected: 0 operations waiting

# 3. Check WiredTiger tickets are available
mongosh --eval '
  const ct = db.serverStatus().wiredTiger.concurrentTransactions;
  print("Read tickets available: " + ct.read.available + "/" + ct.read.totalTickets);
  print("Write tickets available: " + ct.write.available + "/" + ct.write.totalTickets);
'
# Expected: available tickets > 0

# 4. Review profiler for improvement
mongosh --eval '
  const count = db.system.profile.countDocuments({ millis: { $gt: 100 } });
  print(count + " slow operations in the last profiling window");
'
# Expected: reduced count compared to before

# 5. Check operation latency via serverStatus
mongosh --eval '
  const metrics = db.serverStatus().opLatencies;
  printjson({
    reads_avg_us: Math.round(metrics.reads.latency / metrics.reads.ops),
    writes_avg_us: Math.round(metrics.writes.latency / metrics.writes.ops)
  });
'
# Expected: average latency within acceptable range for your workload
```

Disable the profiler after investigation to avoid ongoing performance overhead:

```bash
mongosh --eval 'db.setProfilingLevel(0); print("Profiler disabled.");'
```

Monitor for at least 24 hours to confirm sustained improvement across peak and off-peak periods.

## Prevention

1. **Create indexes for all query patterns** -- Use `explain()` on every query in your application. Every frequently executed query should have a supporting index. Review the `$indexStats` aggregation regularly to identify unused indexes that add write overhead.

2. **Monitor slow operations continuously** -- Set `operationProfiling.slowOpThresholdMs` in `mongod.conf` (default 100ms). Ingest slow query logs into your monitoring system and alert on spike frequency. Use FTDC diagnostic data for historical analysis.

3. **Keep transactions short** -- Multi-document transactions (MongoDB 4.0+) hold locks for their duration. Keep transaction scope minimal and move non-transactional work outside transaction boundaries.

4. **Use read preference for read-heavy workloads** -- Route analytical and reporting reads to secondaries with `readPreference: "secondaryPreferred"` to reduce load and lock contention on the primary.

5. **Shard collections before they hit capacity** -- Monitor collection size and operation throughput. Shard proactively before lock contention becomes a problem, rather than reactively.

6. **Right-size WiredTiger cache** -- Ensure the cache is sized to hold the active working set. If application thread eviction is consistently high, increase cache or add replica set members.

7. **Avoid schema anti-patterns** -- Do not use unbounded arrays, very large documents (> 16 MB limit, but performance degrades well before that), or excessive nesting. Follow the bucket pattern for time-series data and the subset pattern for frequently accessed subsets.

8. **Use connection pooling** -- Configure appropriate connection pool sizes in application drivers. Too many connections waste server resources; too few cause client-side queuing.

9. **Schedule DDL operations during maintenance windows** -- Index builds, collection renames, and database drops can acquire exclusive locks. Plan these during low-traffic periods even with background builds.

10. **Monitor WiredTiger ticket availability** -- Alert when available read or write tickets drop below 10% of total. This is an early warning of capacity exhaustion before operations start queuing.

11. **Set maxTimeMS on all queries** -- Configure a default operation timeout in the application driver to prevent unbounded queries from consuming tickets indefinitely.

## Sources

- [MongoDB Manual -- Analyzing MongoDB Performance](https://www.mongodb.com/docs/manual/administration/analyzing-mongodb-performance/)
- [MongoDB Manual -- FAQ: Concurrency](https://www.mongodb.com/docs/manual/faq/concurrency/)
- [MongoDB Manual -- FAQ: Diagnostics](https://www.mongodb.com/docs/manual/faq/diagnostics/)
- [MongoDB Manual -- Database Profiler](https://www.mongodb.com/docs/manual/tutorial/manage-the-database-profiler/)
- [MongoDB Manual -- Monitor Slow Queries](https://www.mongodb.com/docs/manual/tutorial/monitor-slow-queries/)
- [MongoDB Manual -- db.currentOp()](https://www.mongodb.com/docs/manual/reference/method/db.currentOp/)
- [MongoDB Manual -- WiredTiger Storage Engine](https://www.mongodb.com/docs/manual/core/wiredtiger/)
- [MongoDB Manual -- Indexing Strategies](https://www.mongodb.com/docs/manual/applications/indexes/)
