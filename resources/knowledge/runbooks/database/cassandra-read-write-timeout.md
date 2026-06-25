---
id: "cassandra-read-write-timeout"
title: "Cassandra read/write timeouts: tombstones, compaction, wide partitions, or GC"
domain: database
service: cassandra
symptom_class: [timeout, latency]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [readtimeoutexception, tombstone-overwhelming, compaction-backlog, wide-partition, gc-pause]
difficulty: advanced
---

## Symptom Recognition

- Client driver raises `ReadTimeoutException`/`WriteTimeoutException`, e.g. `Cassandra timeout during read query at consistency QUORUM (2 responses were required but only 1 replica responded)`.
- `cqlsh` reports `OperationTimedOut` on a `SELECT`/`INSERT`.
- Server `system.log` tombstone warning: `Read 5000 live rows and 100000 tombstone cells for query SELECT ... (see tombstone_warn_threshold)`.
- Server aborts query: `org.apache.cassandra.db.filter.TombstoneOverwhelmingException: Scanned over 100001 tombstones during query`.
- `GCInspector.java` log line: `GC for ParNew: 256 ms for 1 collections` or `G1 Young Generation GC in 1392ms` (pause > 200 ms).
- `system.log` warning: `Writing large partition <keyspace>/<table>:<key> (<N> bytes)`.
- `nodetool tpstats` shows non-zero dropped `READ` or `MUTATION` messages.

## Applicability

- Apache Cassandra 3.x, 4.x, 5.x (single node or multi-DC cluster).
- Requires shell access to a cluster node and permission to run `nodetool` (JMX) and `cqlsh`.
- Read access to `system.log`/`debug.log` (default `/var/log/cassandra/`).
- Tools: `nodetool`, `cqlsh`, `grep`, `sstablemetadata` (ships with Cassandra tools).

## Diagnostic Steps

### Step 1: Check cluster/replica availability for the coordinator
```bash
nodetool status <keyspace>
```
Expected output: every node line begins with `UN` (Up/Normal). `DN` or `DL` lines, or fewer Up replicas than the query's consistency level requires, indicate a replica shortfall.

### Step 2: Find dropped requests and GC/compaction pressure
```bash
nodetool tpstats
```
Expected output: a `Dropped Messages` section. `READ` or `MUTATION` rows with a non-zero count indicate the node is shedding load (the direct cause of a timeout); `CompactionExecutor`/`MutationStage` pending tasks show backlog.

### Step 3: Inspect compaction backlog
```bash
nodetool compactionstats -H
```
Expected output: `pending tasks: 0` when healthy. A large, persistently rising `pending tasks` count plus active compactions stuck at low `%` indicate a compaction backlog.

### Step 4: Inspect per-table partition size and SSTables-per-read
```bash
nodetool tablehistograms <keyspace> <table>
```
Expected output: a percentile table. A `99%` `Partition Size` in the hundreds of MB, or `99%` `SSTables` per read well above 1, indicates wide partitions or read amplification.

### Step 5: Scan logs for tombstone, GC, and large-partition signatures
```bash
grep -E "tombstone cells for query|TombstoneOverwhelmingException|GCInspector|Writing large partition" /var/log/cassandra/system.log
```
Expected output: no matches when healthy. Tombstone, GC, or large-partition lines pinpoint which root cause is active.

### Step 6: Measure coordinator vs local read latency
```bash
nodetool proxyhistograms
```
Expected output: coordinator-side `Read Latency` percentiles in microseconds. A `99%` read latency near or above the configured `read_request_timeout_in_ms` (default 5000 ms) confirms reads are timing out at the coordinator.

## Causes

### Cause A: Tombstone overload from high-delete / TTL / null-write workload
**Statement:** A delete-heavy, TTL-expiring, or null-writing access pattern accumulates tombstones in a queried partition faster than `gc_grace_seconds`/compaction can purge them, so a single read scans tombstones past `tombstone_warn_threshold` (1000) or `tombstone_failure_threshold` (100000).
**Chain:**
- root: workload generates excess tombstones in a hot partition
- s1: a single read scans tens of thousands of tombstone cells
- s2: coordinator exceeds `read_request_timeout_in_ms` (or aborts the query)
- D: client sees ReadTimeoutException / TombstoneOverwhelmingException
**Indicators:**
- s1: [Step 5] log line `Read 5000 live rows and 100000 tombstone cells for query SELECT ... (see tombstone_warn_threshold)`
  <!-- match: {"step": 5, "predicate": "contains", "target": "tombstone cells for query"} -->
- s2: [Step 5] abort line `TombstoneOverwhelmingException: Scanned over 100001 tombstones during query`
  <!-- match: {"step": 5, "predicate": "contains", "target": "TombstoneOverwhelmingException"} -->
- D: [Symptom] driver raises ReadTimeoutException / `OperationTimedOut` on the SELECT
**Interventions:**
- **remediation** (root): redesign the query/model to avoid scanning deleted ranges (e.g. add a time bucket to the partition key so reads target a live bucket), then purge existing tombstones by forcing compaction of the table.

  ```bash
  nodetool compact <keyspace> <table>
  ```
  **Verification:** re-run Step 5; the `tombstone cells for query` warning no longer appears for that table, and Step 4 `99%` cell count drops.
- **defensive_fix** (s1): lower `gc_grace_seconds` for the table so droppable tombstones are eligible for purge sooner (only if hinted-handoff/repair windows allow it).

  ```sql
  ALTER TABLE <keyspace>.<table> WITH gc_grace_seconds = 86400;
  ```
  **Verification:** after the next compaction, Step 4 shows reduced cell count; no `tombstone cells for query` warnings recur.

### Cause B: Compaction backlog driven by under-provisioned compaction throughput
**Statement:** Write/flush rate sustainably exceeds the node's compaction rate (throughput capped by `compaction_throughput_mb_per_sec` or too few `concurrent_compactors`), so SSTables accumulate, read amplification rises, and reads exceed `read_request_timeout_in_ms`.
**Chain:**
- root: compaction throughput is below the sustained flush rate
- s1: pending compactions and SSTable count climb without recovering
- s2: each read merges many SSTables, raising read latency
- D: reads exceed timeout → ReadTimeoutException
**Indicators:**
- s1: [Step 3] `nodetool compactionstats` shows a large, non-decreasing `pending tasks` count
  <!-- match: {"step": 3, "predicate": "absent", "target": "pending tasks: 0"} -->
- s2: [Step 4] `tablehistograms` `99%` `SSTables` per read is well above 1
- D: [Step 6] `proxyhistograms` `99%` Read Latency approaches `read_request_timeout_in_ms`
**Interventions:**
- **remediation** (root): raise compaction parallelism/throughput so it keeps pace with writes (set `concurrent_compactors` and `compaction_throughput_mb_per_sec` appropriately in `cassandra.yaml`, then apply live).

  ```bash
  nodetool setcompactionthroughput 128
  ```
  **Verification:** re-run Step 3; `pending tasks` trends down to a steady low value and Step 4 SSTables-per-read falls toward 1.
- **mitigation** (s1): trigger a manual major/keyspace compaction to drain the backlog for the hot table immediately.

  ```bash
  nodetool compact <keyspace> <table>
  ```
  **Risk:** major compaction creates one large SSTable and causes a transient I/O and disk-space spike (needs free space ~= table size). **Duration:** run during a low-traffic window; effect lasts until normal compaction resumes. **Verification:** Step 3 `pending tasks` drops and Step 6 read latency falls below timeout.

### Cause C: Wide partition exceeding safe size
**Statement:** A partition-key design that concentrates unbounded rows into one partition produces a partition large enough that reading/merging it blows past `read_request_timeout_in_ms` and triggers large-partition warnings.
**Chain:**
- root: partition key admits unbounded rows into a single partition
- s1: that partition grows to hundreds of MB across many SSTables
- s2: a read of the partition cannot complete within the read timeout
- D: client sees ReadTimeoutException
**Indicators:**
- s1: [Step 5] log line `Writing large partition <keyspace>/<table>:<key> (... bytes)`
  <!-- match: {"step": 5, "predicate": "contains", "target": "Writing large partition"} -->
- s1: [Step 4] `tablehistograms` `99%` `Partition Size` in the hundreds of MB
- D: [Step 6] `proxyhistograms` `99%` Read Latency near/above the read timeout
**Interventions:**
- **remediation** (root): re-model the schema to bound partition size by adding a bucketing component (e.g. a date/hash) to the partition key, then backfill into the new table.

  ```sql
  CREATE TABLE <keyspace>.<table>_v2 (
    pk_id text, bucket int, ck timestamp, val text,
    PRIMARY KEY ((pk_id, bucket), ck)
  );
  ```
  **Verification:** re-run Step 4 on the new table; `99%` `Partition Size` is well under 100 MB and Step 5 emits no `Writing large partition` lines.
- **mitigation** (s1): page the large read with a small fetch size / `LIMIT` so each request returns before the timeout while re-modeling is pending.

  ```sql
  SELECT * FROM <keyspace>.<table> WHERE pk_id = ? LIMIT 1000;
  ```
  **Risk:** callers must handle paging and may see partial views; does not shrink the partition. **Duration:** until the schema migration ships. **Verification:** the bounded query returns without `OperationTimedOut`.

### Cause D: Replica shortfall vs requested consistency level
**Statement:** One or more replicas for the queried token range are down or overloaded, so the coordinator cannot collect enough responses to satisfy the requested consistency level (e.g. QUORUM) within `read_request_timeout_in_ms`.
**Chain:**
- root: a replica owning the queried range is DOWN or shedding load
- s1: coordinator gathers fewer responses than the CL requires
- s2: coordinator waits out the read/write timeout
- D: client sees ReadTimeoutException / WriteTimeoutException naming the CL
**Indicators:**
- root: [Step 1] `nodetool status` shows a `DN` (Down/Normal) node owning the range
  <!-- match: {"step": 1, "predicate": "contains", "target": "DN"} -->
- s1: [Step 2] `nodetool tpstats` shows non-zero dropped `READ`/`MUTATION` messages on a live replica
  <!-- match: {"step": 2, "predicate": "absent", "target": "READ                             0"} -->
- D: [Symptom] driver message `... at consistency QUORUM (2 responses were required but only 1 replica responded)`
**Interventions:**
- **remediation** (root): restore the down/overloaded replica and re-sync data so the full replica set serves the range again.

  ```bash
  nodetool repair -pr <keyspace>
  ```
  **Verification:** re-run Step 1; all replicas show `UN` and the consistency-level timeout no longer occurs.
- **mitigation** (s1): temporarily lower the read consistency level for affected queries (e.g. QUORUM → LOCAL_ONE) so the coordinator can answer from the available replicas.

  ```sql
  CONSISTENCY LOCAL_ONE;
  ```
  **Risk:** weaker consistency may return stale data and break read-your-writes guarantees. **Duration:** only until the down replica is restored. **Verification:** the query returns without timeout; revert to QUORUM after Step 1 shows all `UN`.

### Cause E: Long stop-the-world GC pauses
**Statement:** Heap pressure (often from large reads, wide partitions, or an undersized/mis-tuned heap) drives stop-the-world GC pauses long enough that in-flight read/write requests exceed their timeout while the JVM is paused.
**Chain:**
- root: JVM heap pressure forces long stop-the-world GC pauses
- s1: the node is unresponsive for hundreds of ms to seconds per pause
- s2: in-flight requests on that node exceed their timeout and are dropped
- D: client sees Read/WriteTimeoutException
**Indicators:**
- s1: [Step 5] `GCInspector.java` line `GC for ParNew: 256 ms for 1 collections` or `G1 Young Generation GC in 1392ms`
  <!-- match: {"step": 5, "predicate": "contains", "target": "GCInspector"} -->
- s2: [Step 2] `nodetool tpstats` shows dropped `READ`/`MUTATION` messages coinciding with the pauses
- D: [Symptom] intermittent Read/WriteTimeoutException with no single down replica
**Interventions:**
- **remediation** (root): relieve heap pressure at the source — fix the wide-partition/large-read driver (Causes C/A) and right-size/retune the JVM heap and collector in `jvm.options`/`jvm-server.options` (e.g. G1 with an adequate `-Xmx`), then restart the node.

  ```bash
  nodetool drain && sudo systemctl restart cassandra
  ```
  **Verification:** re-run Step 5; GC pause lines drop below ~200 ms and Step 2 shows no new dropped messages.
- **mitigation** (s1): reduce per-node concurrency/heap demand by lowering `concurrent_reads` or capping page sizes on heavy clients until the heap is retuned.

  ```bash
  nodetool setconcurrentreads 16
  ```
  **Risk:** lower concurrency reduces throughput. **Duration:** until heap/collector is retuned and the node restarted. **Verification:** Step 5 shows shorter GC pauses and Step 2 dropped-message count stops climbing.

### Cause Z: Unidentified
**Statement:** Timeouts persist but none of the above roots are confirmed by the diagnostics.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the Cassandra SME.

  ```bash
  nodetool status > /tmp/cass-status.txt; \
  nodetool tpstats > /tmp/cass-tpstats.txt; \
  nodetool compactionstats -H > /tmp/cass-compaction.txt; \
  nodetool proxyhistograms > /tmp/cass-proxy.txt; \
  tail -n 2000 /var/log/cassandra/system.log > /tmp/cass-system.log
  ```
  **Risk:** none (read-only capture). **Duration:** n/a. **Verification:** snapshot files are non-empty and attached to the escalation ticket.

## Prevention

- Alert on log lines `tombstone cells for query`, `TombstoneOverwhelmingException`, and `Writing large partition`; treat any occurrence as actionable.
- Keep partitions under 100 MB; set `compaction_large_partition_warning_threshold_mb: 100` and design partition keys with explicit bucketing.
- Monitor `nodetool tpstats` dropped `READ`/`MUTATION` and `nodetool compactionstats` `pending tasks`; page when pending compactions stay high.
- Avoid high-volume deletes/null writes and overly long TTLs on read paths; tune `gc_grace_seconds` to the repair cadence.
- Alert on GCInspector pauses > 200 ms; right-size the heap and prefer G1 for large heaps.
- Track coordinator `nodetool proxyhistograms` `99%` read latency against `read_request_timeout_in_ms` and alert before it approaches the timeout.

## Sources

- [Troubleshooting](https://cassandra.apache.org/doc/latest/cassandra/troubleshooting/index.html) — troubleshooting section index (finding nodes, reading logs, using nodetool/tools).
- [Use nodetool](https://cassandra.apache.org/doc/latest/cassandra/troubleshooting/use_nodetool.html) — `nodetool status`, `tpstats` (dropped messages), `proxyhistograms`, `tablehistograms`, `compactionstats` syntax and output.
- [Reading logs](https://cassandra.apache.org/doc/latest/cassandra/troubleshooting/reading_logs.html) — GCInspector pause lines, CompactionTask/flush log entries.
- [Operating](https://cassandra.apache.org/doc/latest/cassandra/managing/operating/index.html) — operating index (compaction, tombstone/repair, logging, monitoring metrics).
- [Managing tombstones in cassandra](https://www.instaclustr.com/support/documentation/cassandra/using-cassandra/managing-tombstones-in-cassandra/) — `tombstone_warn_threshold` (1000) / `tombstone_failure_threshold` (100000) defaults and behavior.
- [CASSANDRA-8870](https://issues.apache.org/jira/browse/CASSANDRA-8870) — `TombstoneOverwhelmingException: Scanned over N tombstones during query` exact string.
- [10435](https://github.com/scylladb/scylladb/issues/10435) — exact `ReadTimeoutException` consistency-level/replica wording.
- [3534961](https://access.redhat.com/solutions/3534961) — `Writing large partition` warning and `compaction_large_partition_warning_threshold_mb`.
