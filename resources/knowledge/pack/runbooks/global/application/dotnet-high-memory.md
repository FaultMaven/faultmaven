---
id: "dotnet-high-memory"
title: ".NET High Memory and GC Pressure: Managed Leaks, LOH Fragmentation, Excessive Gen2 GC"
domain: application
service: dotnet
symptom_class: [oom, latency]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [oom-killed, gc-pressure, loh-fragmentation, gen2-collections, managed-leak, dotnet-counters]
difficulty: advanced
---

## Symptom Recognition

- `System.OutOfMemoryException` thrown in application logs, often during a large allocation.
- Container OOM-killed: `OOMKilled` reason on the pod, exit code 137, kernel `Out of memory: Killed process` for the `dotnet` process.
- `dotnet.process.memory.working_set` climbs continuously and never returns to baseline after load subsides.
- `dotnet.gc.last_collection.heap.size` gen2 and/or loh buckets grow monotonically across collections.
- Request latency spikes correlated with frequent blocking gen2 GCs; `dotnet.gc.pause.time` elevated.
- `dotnet.gc.collections` gen2 count increments rapidly relative to gen0/gen1.

## Applicability

- .NET Core 3.1 SDK or later (commands and counters validated on .NET 8/9; counter names use the modern `dotnet.gc.*` schema introduced in .NET 9).
- Required tools (install as .NET global tools): `dotnet-counters`, `dotnet-dump` (bundles the SOS debugging extension), `dotnet-gcdump`.
- Access: ability to run the diagnostic tool as the same OS user as the target process (or root); on Linux/macOS the tool and target must share the same `TMPDIR`.
- For dump collection in containers: shared process namespace or the `--diagnostic-port` socket, plus enough headroom to absorb gcdump's event buffer (can grow to 256 MB).

## Diagnostic Steps

### Step 1: Confirm managed memory growth with live counters

```bash
dotnet tool install --global dotnet-counters
dotnet-counters ps
dotnet-counters monitor --refresh-interval 1 -p <PID>
```

Expected output: a live table including `dotnet.process.memory.working_set (By)`, `dotnet.gc.last_collection.heap.size (By)` broken into gen0/gen1/gen2/loh/poh, `dotnet.gc.last_collection.heap.fragmentation.size (By)`, and `dotnet.gc.collections` gen0/gen1/gen2 counts. Watch the heap size and working set over time under load.

### Step 2: Capture a managed heap dump

```bash
dotnet tool install --global dotnet-dump
dotnet-dump collect -p <PID>
```

Expected output: `Writing minidump with heap to ./core_<timestamp>` followed by `Complete`. For leak confirmation, collect a second dump a few minutes later so the two can be compared.

### Step 3: Analyze object statistics with SOS

```bash
dotnet-dump analyze ./core_<timestamp>
# at the SOS prompt:
> dumpheap -stat
> dumpheap -mt <MethodTable>
> gcroot <ObjectAddress>
```

Expected output: `dumpheap -stat` prints a `Count / TotalSize / Class Name` table sorted ascending; the heaviest rows name the dominating types. `gcroot` prints the full reference chain from a GC root down to the leaked instance.

### Step 4: Capture a GC object graph for root analysis

```bash
dotnet tool install --global dotnet-gcdump
dotnet-gcdump collect -p <PID>
dotnet-gcdump report ./<timestamp>_<PID>.gcdump
```

Expected output: `Writing gcdump to './<timestamp>_<PID>.gcdump'... Finished writing N bytes.` and a `report` heap-statistics table (`Size (Bytes) / Count / Type`). Note: `collect` forces a blocking gen2 GC, so avoid it on large heaps in latency-sensitive paths.

### Step 5: Inspect the runtime GC configuration

```bash
cat <app>.runtimeconfig.json
env | grep -iE 'DOTNET_(gcServer|GCHeapHardLimit|GCConserveMemory|gcConcurrent)'
```

Expected output: the `configProperties` block (e.g. `System.GC.Server`, `System.GC.HeapHardLimit`, `System.GC.HeapHardLimitPercent`) and any `DOTNET_*` overrides currently in effect for the process.

## Causes

### Cause A: Managed object leak via a long-lived reference (rooted cache/collection/event handler)
**Statement:** A long-lived root (a static collection, singleton cache, or undetached event handler) keeps accumulating object references so the GC can never reclaim them, growing gen2 without bound.
**Chain:**
- root: a long-lived root holds an ever-growing set of references (e.g. items added to a static `List`/`Dictionary`/cache and never evicted)
- s1: reachable gen2 object count and total size grow on every request
- s2: working set and committed heap rise monotonically; gen2 GCs reclaim nothing
- D: working set hits the container/process limit and OutOfMemoryException / OOMKilled occurs
**Indicators:**
- root: [Step 3] `gcroot` traces the leaked instance up through a static field or cache/collection root (e.g. `CustomerCache -> List<Customer> -> Customer`)
  <!-- match: {"step": 3, "predicate": "contains", "target": "gcroot"} -->
- s1: [Step 3] `dumpheap -stat` shows one or few application types with an enormous `Count` and `TotalSize`
- s2: [Step 1] `dotnet.gc.last_collection.heap.size` gen2 bucket increases across refreshes and never drops after load
- D: [Symptom] `OutOfMemoryException` or `OOMKilled` / exit code 137
**Interventions:**
- **remediation** (root): remove the unbounded retention — drop the static reference, detach the event handler, or bound the cache with a size/TTL policy (e.g. `MemoryCache` with `SizeLimit` + per-entry `Size`/`SlidingExpiration`). Compare two gcdumps to confirm the type count stops growing.

  ```bash
  dotnet-gcdump collect -p <PID> -o ./before.gcdump
  # ... apply fix, redeploy, drive load ...
  dotnet-gcdump collect -p <PID> -o ./after.gcdump
  ```

  **Verification:** open `before.gcdump` and `after.gcdump` in Visual Studio/PerfView and confirm the offending type's count is flat (not growing) under sustained load; re-run Step 1 and confirm gen2 heap size returns to baseline after load.
- **mitigation** (s2): restart the process to reclaim the leaked heap and clear the immediate OOM risk.

  ```bash
  kubectl rollout restart deployment/<app>   # or: systemctl restart <app>
  ```

  **Risk:** in-flight requests are dropped and the leak recurs after restart. **Duration:** hours, until the root fix ships. **Verification:** Step 1 shows working set back at startup baseline immediately after restart.

### Cause B: Large Object Heap fragmentation from churning large (>= 85 KB) buffers
**Statement:** The application repeatedly allocates and frees large objects (arrays/buffers >= 85,000 bytes), and because the LOH is swept but not compacted by default, free gaps accumulate and the committed LOH stays large even though live data is small.
**Chain:**
- root: frequent transient large-object allocations (>= 85 KB byte/char arrays) churn the LOH, which is not compacted by default
- s1: LOH free-space fragmentation accumulates; committed LOH size stays high while live large-object bytes are low
- s2: the GC cannot place a new large allocation in any contiguous free gap, forcing heap growth (and on a hard limit, failure)
- D: working set grows and/or a large allocation throws OutOfMemoryException despite apparent free space
**Indicators:**
- root: [Step 3] `dumpheap -stat` shows large `System.Byte[]` / `System.Char[]` / `System.Object[]` rows and a high `Free` count interleaved on the LOH
  <!-- match: {"step": 3, "predicate": "contains", "target": "Free"} -->
- s1: [Step 1] `dotnet.gc.last_collection.heap.fragmentation.size` loh bucket is large relative to the loh `heap.size`
- s2: [Step 1] loh `heap.size` stays high while live large-object bytes (from Step 4 report) are comparatively small
- D: [Symptom] `OutOfMemoryException` raised on a large allocation
**Interventions:**
- **remediation** (root): stop churning the LOH — pool and reuse large buffers (`ArrayPool<T>.Shared`) or stream data instead of allocating multi-hundred-KB arrays per request, so large objects are long-lived and stable rather than transient.

  ```bash
  # code change: replace `new byte[bigSize]` per call with ArrayPool<byte>.Shared rent/return
  dotnet-counters monitor --refresh-interval 1 -p <PID>
  ```

  **Verification:** re-run Step 1 and confirm the loh `heap.fragmentation.size` and loh `heap.size` stay flat under sustained load.
- **mitigation** (s1): force a one-time compacting gen2 collection to reclaim the fragmented LOH gaps.

  ```bash
  # in-process (e.g. an admin endpoint):
  # GCSettings.LargeObjectHeapCompactionMode = GCLargeObjectHeapCompactionMode.CompactOnce;
  # GC.Collect();
  echo "trigger admin GC-compact endpoint"
  ```

  **Risk:** LOH compaction is an expensive blocking full GC that pauses the app. **Duration:** safe as a one-off; do not run on a hot loop. **Verification:** Step 1 shows loh `heap.fragmentation.size` drops sharply right after the compaction.

### Cause C: Excessive Gen2 collections from a high mid-lifetime allocation rate (latency)
**Statement:** A high sustained allocation rate of objects that survive gen0/gen1 promotes too much into gen2, so the runtime runs frequent blocking gen2 collections whose long pauses dominate request latency.
**Chain:**
- root: code allocates heavily on the hot path and the objects live just long enough to be promoted into gen2
- s1: gen2 occupancy grows quickly, triggering frequent gen2 (full) collections
- s2: each blocking gen2 collection introduces a long stop-the-world pause
- D: request latency spikes line up with GC pauses (p99 tail latency degraded)
**Indicators:**
- root: [Step 4] `dotnet-gcdump report` shows high `Count` for short-lived-looking app types, indicating a high allocation rate funneled into gen2
- s1: [Step 1] `dotnet.gc.collections` gen2 count increments rapidly relative to gen0/gen1
  <!-- match: {"step": 1, "predicate": "contains", "target": "dotnet.gc.collections"} -->
- s2: [Step 1] `dotnet.gc.pause.time` is elevated and correlates with the gen2 count increments
- D: [Symptom] request latency spikes correlated with `dotnet.gc.pause.time`
**Interventions:**
- **remediation** (root): cut hot-path allocations — reuse buffers via `ArrayPool<T>`, prefer `struct`/`Span<T>`/`stackalloc` for transient data, and avoid per-request boxing/LINQ closures so fewer objects survive to gen2.

  ```bash
  dotnet-counters monitor --refresh-interval 1 -p <PID>
  ```

  **Verification:** re-run Step 1 and confirm gen2 collection count growth and `dotnet.gc.pause.time` drop materially under the same load.
- **defensive_fix** (s2): enable Server GC + concurrent (background) GC so collections are parallelized across cores and gen2 pauses are reduced.

  ```bash
  export DOTNET_gcServer=1
  export DOTNET_gcConcurrent=1
  # or in runtimeconfig.json: "System.GC.Server": true, "System.GC.Concurrent": true
  ```

  **Verification:** restart, then re-run Step 1 and confirm `dotnet.gc.pause.time` per gen2 collection decreases and latency tail improves.

### Cause D: GC heap hard limit set too low for the container memory budget
**Statement:** `System.GC.HeapHardLimit` (or its percent variant / container default of 75%) caps the managed heap below the application's real working-set need, so the GC refuses to grow the heap and throws OutOfMemoryException while the container still has RAM.
**Chain:**
- root: the configured GC heap hard limit (explicit or the container-derived default) is below the app's legitimate live-heap requirement
- s1: the GC reaches the hard limit and cannot commit more heap even though live data is genuinely needed
- D: OutOfMemoryException is thrown despite available container memory
**Indicators:**
- root: [Step 5] `runtimeconfig.json` / env shows `System.GC.HeapHardLimit` or `DOTNET_GCHeapHardLimit` set low, or container limit small with the 75% default in effect
  <!-- match: {"step": 5, "predicate": "contains", "target": "HeapHardLimit"} -->
- s1: [Step 1] `dotnet.gc.last_collection.heap.size` total plateaus exactly at the configured limit just before the failure
- D: [Symptom] `OutOfMemoryException` with the working set well under the container memory limit
**Interventions:**
- **remediation** (root): raise the limit to match real demand — increase the container memory request/limit and/or set `System.GC.HeapHardLimitPercent` (or remove an over-tight explicit `HeapHardLimit`) so the GC can commit the heap the app legitimately needs.

  ```bash
  # runtimeconfig.json: "configProperties": { "System.GC.HeapHardLimitPercent": 80 }
  # and raise the pod limit:
  kubectl set resources deployment/<app> --limits=memory=2Gi
  ```

  **Verification:** re-run Step 1 under peak load and confirm `dotnet.gc.last_collection.heap.size` stabilizes below the new ceiling with no OutOfMemoryException.

### Cause Z: Unidentified
**Statement:** Memory growth or GC pressure is confirmed but none of the known roots above is established by the diagnostics gathered.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot (two spaced heap dumps + two gcdumps + a counter trace) and escalate to the .NET runtime/SME on call.

  ```bash
  dotnet-counters collect --refresh-interval 1 -p <PID> -o ./counters.csv
  dotnet-dump collect -p <PID> -o ./snap1.dmp
  dotnet-gcdump collect -p <PID> -o ./snap1.gcdump
  sleep 180
  dotnet-dump collect -p <PID> -o ./snap2.dmp
  dotnet-gcdump collect -p <PID> -o ./snap2.gcdump
  ```

  **Risk:** dump/gcdump collection triggers a blocking gen2 GC and may briefly pause the process. **Duration:** one-off during the incident window. **Verification:** confirm both dump pairs and the counter CSV are written, then hand off for diff analysis.

## Prevention

- Add always-on alerts on `dotnet.gc.last_collection.heap.size` (gen2 + loh) trend and `dotnet.process.memory.working_set` slope; page when the post-collection heap fails to return to baseline.
- Alert on `dotnet.gc.pause.time` and gen2 collection rate to catch GC-pressure latency regressions before they breach SLOs.
- Pool large buffers (`ArrayPool<T>`) and stream large payloads to keep allocations off the LOH; treat any per-request allocation >= 85 KB as a code-review flag.
- Bound every cache with a size/TTL eviction policy; never use unbounded static collections as caches.
- Set `System.GC.Server=true` for throughput services and size `System.GC.HeapHardLimitPercent` deliberately against the container memory limit (the default is 75%); load-test at the configured ceiling.
- Run a periodic gcdump diff in CI/staging soak tests to catch leaks (flat-vs-growing type counts) before production.

## Sources

- [.NET diagnostics tools overview](https://learn.microsoft.com/en-us/dotnet/core/diagnostics/) — tool inventory (dotnet-counters/dump/gcdump/trace) and which tool fits memory-leak diagnosis.
- [Tutorial: Debug a memory leak in .NET](https://learn.microsoft.com/en-us/dotnet/core/diagnostics/debug-memory-leak) — exact `dotnet-counters monitor`, `dotnet-dump collect`/`analyze`, and SOS `dumpheap -stat` / `dumpheap -mt` / `gcroot` commands and outputs.
- [dotnet-gcdump diagnostic tool](https://learn.microsoft.com/en-us/dotnet/core/diagnostics/dotnet-gcdump) — `dotnet-gcdump collect`/`report`/`ps` syntax, gen2-GC warning, 256 MB buffer caveat, heapstat report format.
- [Large object heap (LOH)](https://learn.microsoft.com/en-us/dotnet/standard/garbage-collection/large-object-heap) — 85,000-byte LOH threshold and the sweep-not-compact fragmentation behavior.
- [GCLargeObjectHeapCompactionMode / LargeObjectHeapCompactionMode](https://learn.microsoft.com/en-us/dotnet/api/system.runtime.gcsettings.largeobjectheapcompactionmode) — `CompactOnce` one-time LOH compaction semantics.
- [Garbage collector config settings](https://learn.microsoft.com/en-us/dotnet/core/runtime-config/garbage-collector) — `System.GC.Server`, `System.GC.Concurrent`, `System.GC.HeapHardLimit`/`HeapHardLimitPercent`, and the 75% container default.
- [.NET runtime metrics](https://learn.microsoft.com/en-us/dotnet/core/diagnostics/built-in-metrics-runtime) — `dotnet.gc.*` counter schema (heap size, fragmentation size, pause time, collections).
