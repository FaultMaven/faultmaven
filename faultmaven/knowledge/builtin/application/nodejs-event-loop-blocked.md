---
id: nodejs-event-loop-blocked
title: "Node.js Event Loop Blocked — Diagnosis and Resolution"
domain: application
service: nodejs
symptom_class:
  - latency
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - nodejs
  - event-loop
  - latency
  - performance
  - cpu
difficulty: intermediate
---

# Node.js Event Loop Blocked

## Problem Definition

Applies to Node.js 18+ (LTS) running on Linux. Requires process-level access for profiling (`--inspect`, `SIGUSR1`), access to application logs, and optionally Prometheus metrics. Chrome DevTools or Clinic.js is needed for CPU profile analysis.

A blocked event loop occurs when a synchronous CPU-bound operation runs on the main thread, preventing the event loop from advancing through its phases (timers, pending callbacks, poll, check, close). Because Node.js uses a single thread for JavaScript execution, any operation monopolizing the main thread causes all incoming HTTP requests to queue, timer callbacks to fire late, health check endpoints to stop responding (triggering load balancer or Kubernetes liveness probe failures), and WebSocket connections to drop due to missed heartbeats. HTTP response times spike from single-digit milliseconds to seconds. Monitoring shows event loop lag exceeding 100ms (healthy is under 10ms). Application logs typically show no errors -- throughput simply drops. Common blocking operations include: `JSON.parse()`/`JSON.stringify()` on multi-megabyte payloads (O(n), 1.3 seconds for 50MB), synchronous `fs.*Sync()` methods, synchronous `crypto.pbkdf2Sync()`/`crypto.randomFillSync()`, regular expressions with catastrophic backtracking (ReDoS), tight loops iterating over large datasets (especially O(n^2) patterns), synchronous `child_process.execSync()`, and V8 garbage collection pauses on large heaps (50-500ms).

## Diagnostic Steps

### 1. Measure event loop lag with perf_hooks

Quantifies how long the event loop is blocked, confirming the problem and establishing a baseline for measuring improvement.

```bash
node -e "
const { monitorEventLoopDelay } = require('perf_hooks');
const h = monitorEventLoopDelay({ resolution: 20 });
h.enable();
setInterval(() => {
  console.log('p50:', (h.percentile(50)/1e6).toFixed(2), 'ms',
              'p99:', (h.percentile(99)/1e6).toFixed(2), 'ms',
              'max:', (h.max/1e6).toFixed(2), 'ms');
  h.reset();
}, 5000);
"
```

If the application exposes Prometheus metrics:

```bash
curl -s http://localhost:9090/metrics | grep -i "event_loop\|loop_lag"
```

**Expected output:** p50 under 5ms and p99 under 20ms for a healthy event loop. Max should stay below 50ms.

**What this means:** p99 above 100ms confirms event loop blocking. If lag is intermittent (spikes every N seconds), the cause is likely periodic (GC, cron-like task, cache rebuild). If lag is constant under load, a request handler contains blocking code.

### 2. Profile CPU usage of the Node.js process

Determines whether the main thread is CPU-bound and for how long.

```bash
top -p $(pgrep -f "node") -H
# Or with pidstat for per-thread breakdown
pidstat -p $(pgrep -f "node") -t 1 10
```

**Expected output:** For a healthy Node.js process, CPU usage is low between requests and spikes briefly during processing. A single thread pinned at 100% for sustained periods indicates blocking work on the main thread.

**What this means:** If one thread shows 100% CPU continuously, the event loop is blocked by computation. If multiple threads show high CPU, the libuv worker pool is saturated (DNS, file I/O, crypto). Low CPU with high lag points to synchronous I/O waiting (e.g., `fs.readFileSync` on a network filesystem).

### 3. Capture a CPU profile with V8 inspector

Produces a flame graph showing exactly which functions consume the most CPU time on the main thread.

For a running process (no restart required):

```bash
kill -USR1 $(pgrep -f "node")
# Then connect Chrome DevTools to the inspector URL shown in stderr
# Navigate to Performance tab > Record > Reproduce the issue > Stop
```

Or start with inspector enabled:

```bash
node --inspect app.js
# Connect Chrome DevTools to chrome://inspect
```

For command-line profiling:

```bash
node --prof app.js
# Reproduce load, then stop
node --prof-process isolate-*.log > profile.txt
```

**Expected output:** The flame graph shows a tall, wide column for the blocking function. The `profile.txt` output ranks functions by "ticks" (CPU time samples).

**What this means:** The widest bar in the flame graph is the blocking operation. Common findings: `JSON.parse` (large payload), `RegExp.exec` (catastrophic backtracking), application-specific compute functions, or `v8::internal::MarkCompactCollector` (GC pause).

### 4. Run Clinic.js for automated diagnosis

Provides an automated analysis that explicitly flags event loop blocking, I/O issues, and GC problems.

```bash
npx clinic doctor -- node app.js
# Generate load against the application, then Ctrl+C
npx clinic flame -- node app.js
# Generate load, then Ctrl+C for CPU flame graph
```

**Expected output:** Clinic Doctor opens an HTML report with a clear diagnosis: "Event loop is blocked" (red), "I/O issue" (orange), or "Healthy" (green). Clinic Flame shows the hot path in the flame graph.

**What this means:** Doctor correlates event loop delay, CPU, memory, and active handles to diagnose the category of problem. Flame pinpoints the exact function. This is the fastest path to root cause for most event loop blocking issues.

### 5. Search codebase for known blocking patterns

Identifies synchronous APIs and dangerous patterns that are the most common sources of event loop blocking.

```bash
grep -rn "Sync(" --include="*.js" --include="*.ts" src/
grep -rn "JSON\.parse\|JSON\.stringify" --include="*.js" --include="*.ts" src/
grep -rn "crypto\.\(pbkdf2Sync\|randomFillSync\|scryptSync\)" --include="*.js" --include="*.ts" src/
grep -rn "execSync\|spawnSync" --include="*.js" --include="*.ts" src/
```

**Expected output:** List of files and line numbers containing synchronous operations. Any `*Sync` call in a request handler path is a blocking risk.

**What this means:** `fs.*Sync` calls are safe during startup (module loading) but dangerous in request handlers. `JSON.parse` is safe for small payloads (under 1MB) but blocks at larger sizes. Synchronous crypto operations should always use their async counterparts under load.

### 6. Check V8 garbage collection pauses

Determines whether GC is contributing to event loop lag, especially on large heaps.

```bash
node --trace-gc app.js 2>&1 | grep -E "Mark-Compact|Scavenge" | head -20
```

**Expected output:** Scavenge (young generation GC) pauses under 5ms. Mark-Compact (full GC) pauses under 50ms. Frequency: Scavenge every few seconds, Mark-Compact every few minutes.

**What this means:** Mark-Compact pauses above 100ms indicate heap pressure. The heap may be too large (increasing GC scan time) or the application may be creating too many long-lived objects. Frequent Scavenge indicates high allocation rate.

### 7. Check libuv thread pool saturation

Determines whether the default 4-thread worker pool is a bottleneck for async operations that use it (DNS lookup, file I/O, crypto).

```bash
node -e "console.log('UV_THREADPOOL_SIZE:', process.env.UV_THREADPOOL_SIZE || '4 (default)')"
```

Monitor active handles and requests:

```bash
node -e "
setInterval(() => {
  console.log('handles:', process._getActiveHandles().length,
              'requests:', process._getActiveRequests().length);
}, 2000);
"
```

**Expected output:** Active requests should stay below `UV_THREADPOOL_SIZE`. If requests consistently exceed the pool size, operations are queuing.

**What this means:** When the thread pool is full, async operations that use it (particularly `dns.lookup()`) queue behind each other. This manifests as event loop lag even though the main thread is not CPU-bound. Increasing `UV_THREADPOOL_SIZE` or switching to `dns.resolve()` (uses c-ares, not the thread pool) resolves this.

## Mitigation

### Option 1: Restart the process

**Risk:** Low. If running behind a load balancer with multiple instances, impact is limited to in-flight requests on this instance.

**Command:**

```bash
pm2 restart app
# Or for Kubernetes:
kubectl delete pod <pod-name> -n <namespace>
```

**Verify:** `curl -w "\ntime_total: %{time_total}s\n" http://localhost:3000/health` responds in under 100ms.

**Duration:** Seconds to 1 minute.

### Option 2: Scale horizontally to absorb load

**Risk:** Low. Does not fix the root cause but distributes the impact across more instances.

**Command:**

```bash
pm2 scale app +2
# Or for Kubernetes:
kubectl scale deployment <name> -n <namespace> --replicas=4
```

**Verify:** `kubectl get pods` shows all replicas Running. Response times improve across the fleet.

**Duration:** 30 seconds to 2 minutes.

### Option 3: Increase libuv thread pool size

**Risk:** Low. Each additional thread consumes approximately 1MB of stack memory. Default of 4 is insufficient for I/O-heavy workloads.

**Command:**

```bash
export UV_THREADPOOL_SIZE=16
node app.js
```

For Kubernetes, add to the deployment env:

```yaml
env:
  - name: UV_THREADPOOL_SIZE
    value: "16"
```

**Verify:** Restart the application. DNS and file I/O operations complete faster. Event loop lag decreases if thread pool was the bottleneck.

**Duration:** Requires application restart.

### Option 4: Enable cluster mode for multi-core utilization

**Risk:** Low. Each worker is an independent process with its own event loop. One blocked worker does not block others.

**Command:**

```bash
pm2 start app.js -i max --name myapp
```

**Verify:** `pm2 list` shows all instances online. Load is distributed across workers.

**Duration:** Restart time (seconds).

## Root Cause Resolution

**If** CPU profile shows a long-running synchronous function (data transformation, sorting, encryption) → offload to a Worker Thread using `worker_threads` or a managed pool like `piscina`:

```javascript
const Piscina = require('piscina');
const pool = new Piscina({ filename: './worker.js', maxThreads: 4 });
app.get('/compute', async (req, res) => {
  const result = await pool.run(req.body);
  res.json(result);
});
```

Any operation expected to take more than 5ms of CPU time should be offloaded.

**If** code uses `fs.readFileSync` or other `*Sync` methods in request handlers → replace with async equivalents (`fs.promises.readFile`) or streams (`fs.createReadStream`) for large files. `*Sync` methods are only acceptable during application startup.

**If** `JSON.parse()`/`JSON.stringify()` on large payloads causes lag → use streaming JSON parsers (`stream-json`, `JSONStream`, `bfj`) for payloads over 1MB. Set request body size limits to prevent unbounded parsing: `express.json({ limit: '1mb' })`.

**If** a regular expression causes exponential backtracking → rewrite the regex to avoid nested quantifiers (`(a+)+`), overlapping alternations (`(a|a)*`), and backreferences on untrusted input. Use `safe-regex` to audit patterns or replace with `re2` (Google's linear-time regex engine). For simple matching, use `String.indexOf()` instead of regex.

**If** GC pauses exceed 100ms → reduce heap pressure by reusing objects, reducing allocation rate, and avoiding patterns that create many short-lived large objects. Increase `--max-old-space-size` if the heap is undersized. Consider using `--max-semi-space-size=64` to reduce young-generation GC frequency for high-allocation workloads.

**If** DNS lookups saturate the libuv thread pool → switch from `dns.lookup()` (uses thread pool) to `dns.resolve()` (uses c-ares, independent of thread pool). Increase `UV_THREADPOOL_SIZE` to 16-128 for workloads with heavy file I/O.

**If** an O(n^2) algorithm processes large datasets → restructure to O(n) using Maps for lookups instead of nested iteration. For unavoidable long computations, partition into chunks yielding to the event loop with `setImmediate()`:

```javascript
async function processInChunks(items, chunkSize = 100) {
  for (let i = 0; i < items.length; i += chunkSize) {
    processChunk(items.slice(i, i + chunkSize));
    await new Promise(resolve => setImmediate(resolve));
  }
}
```

## Verification

1. **Measure event loop lag under load:**

```bash
npx autocannon -c 100 -d 30 http://localhost:3000/api/endpoint
```

p99 response time should be under 100ms. Event loop lag (from `monitorEventLoopDelay`) should stay below 20ms.

2. **Run Clinic Doctor and confirm healthy diagnosis:**

```bash
npx clinic doctor -- node app.js
# Generate load, then Ctrl+C
```

Report should show green "Healthy" status with no event loop blocking detected.

3. **Check Prometheus event loop metrics (if available):**

```bash
curl -s http://localhost:9090/metrics | grep event_loop
```

The event loop lag histogram should show the majority of samples below 10ms.

4. **Monitor in production for 24 hours:** Response times should remain consistent without periodic spikes. Alert on `nodejs_event_loop_lag_seconds` p99 exceeding 50ms for 5 minutes.

## Prevention

- **Enforce no synchronous I/O in request paths** with the ESLint `no-sync` rule (`eslint-plugin-node`). Allow `*Sync` calls only in startup/initialization code paths via inline disable comments.
- **Monitor event loop lag in every Node.js service** using `monitorEventLoopDelay` from `perf_hooks`, exported as a Prometheus histogram. Alert when p99 exceeds 50ms for 5 minutes.
- **Set request payload size limits** to prevent large JSON from blocking during parsing: `express.json({ limit: '1mb' })` or equivalent. For payloads that must be larger, use streaming parsers.
- **Audit regular expressions with `safe-regex` or `recheck`** in CI. Reject patterns with super-linear worst-case complexity. Consider `re2` as a drop-in replacement for untrusted input matching.
- **Run in cluster mode in production** (`pm2 start -i max` or Kubernetes with multiple replicas) so a single blocked worker does not take down all request handling.
- **Use Worker Threads for CPU-intensive operations.** Establish a convention that any synchronous operation expected to exceed 5ms must be offloaded to a worker pool (`piscina` or `workerpool`).
- **Set `UV_THREADPOOL_SIZE=16`** (or higher) for services with heavy file I/O or DNS lookups. The default of 4 is insufficient for most production workloads.
- **Bound all data processing.** Never iterate over unbounded datasets on the main thread. Implement pagination at the database query level and chunk-processing with `setImmediate()` yields for in-memory operations.

## Sources

- [Node.js — Don't Block the Event Loop](https://nodejs.org/en/learn/asynchronous-work/dont-block-the-event-loop) — Official guide covering dangerous APIs (JSON, regex, crypto, fs Sync), partitioning, offloading, and ReDoS prevention
- [Node.js — Worker Threads API](https://nodejs.org/api/worker_threads.html) — Official documentation for offloading CPU-intensive work to separate threads
- [Node.js — Performance Hooks (monitorEventLoopDelay)](https://nodejs.org/api/perf_hooks.html) — Built-in API for measuring event loop lag with histogram percentiles
- [Trigger.dev — How We Tamed Node.js Event Loop Lag](https://trigger.dev/blog/event-loop-lag) — Production case study diagnosing O(n^2) algorithms, payload size limits, and monitoring with OpenTelemetry
- [Clinic.js](https://clinicjs.org/) — Open-source Node.js performance profiling suite: Doctor (diagnosis), Flame (CPU profiling), Bubbleprof (async visualization)
- [NodeSource — Debugging the Event Loop](https://nodesource.com/blog/node-js-performance-monitoring-part-3-debugging-the-event-loop) — CPU profiling techniques and flame graph interpretation for event loop issues
