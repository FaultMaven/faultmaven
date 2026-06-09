---
id: "nodejs-event-loop-blocked"
title: "Node.js Event Loop Blocked"
domain: application
service: nodejs
symptom_class: [latency]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [nodejs, event-loop, latency, performance, cpu, worker-threads, clinic]
difficulty: intermediate
---

## Symptom Recognition

- HTTP response times spike from single-digit milliseconds to seconds under normal load.
- Event loop lag metric (`nodejs_event_loop_lag_seconds` p99) exceeds 100 ms.
- Health check endpoints stop responding, triggering Kubernetes liveness probe failures or load balancer health-check timeouts.
- WebSocket connections drop due to missed heartbeats.
- Monitoring shows CPU usage pinned at 100% on a single thread while other threads are idle.
- Application logs show no errors; throughput simply drops.
- Periodic lag spikes occurring at a fixed interval (e.g., every 30 s) suggest a scheduled task or GC cycle as the trigger.

## Applicability

Applies to Node.js 18 LTS and later running on Linux. Requires process-level access for profiling (`--inspect` flag or `SIGUSR1` signal). Chrome DevTools or the Clinic.js suite (`npx clinic`) must be available on the host or a developer machine with network access to the process inspector port. Access to application logs and, optionally, Prometheus metrics is required.

## Diagnostic Steps

### Step 1: Measure event loop lag with perf_hooks

```bash
node -e "
const { monitorEventLoopDelay } = require('perf_hooks');
const h = monitorEventLoopDelay({ resolution: 20 });
h.enable();
setInterval(() => {
  console.log(
    'p50:', (h.percentile(50)/1e6).toFixed(2), 'ms',
    'p99:', (h.percentile(99)/1e6).toFixed(2), 'ms',
    'max:', (h.max/1e6).toFixed(2), 'ms'
  );
  h.reset();
}, 5000);
"
```

Expected output: p50 below 5 ms and p99 below 20 ms for a healthy event loop. p99 above 100 ms confirms blocking. If Prometheus metrics are exposed:

```bash
curl -s http://localhost:9090/metrics | grep -i "event_loop\|loop_lag"
```

### Step 2: Profile CPU usage on the main thread

```bash
top -p $(pgrep -f "node") -H
```

Or for a per-thread breakdown:

```bash
pidstat -p $(pgrep -f "node") -t 1 10
```

Expected output: One thread pinned near 100% CPU for sustained periods indicates the main thread is CPU-bound. Low CPU with high lag suggests synchronous I/O waiting (e.g., `fs.readFileSync` on a slow filesystem).

### Step 3: Search codebase for known blocking patterns

```bash
grep -rn "Sync(" --include="*.js" --include="*.ts" src/
grep -rn "JSON\.parse\|JSON\.stringify" --include="*.js" --include="*.ts" src/
grep -rn "crypto\.\(pbkdf2Sync\|randomFillSync\|scryptSync\)" --include="*.js" --include="*.ts" src/
grep -rn "execSync\|spawnSync\|inflateSync\|deflateSync" --include="*.js" --include="*.ts" src/
```

Expected output: List of files and line numbers containing synchronous operations. Any `*Sync` call in a request handler path is a blocking risk.

### Step 4: Capture a CPU flame graph with V8 inspector

For a running process without restart:

```bash
kill -USR1 $(pgrep -f "node")
```

Then connect Chrome DevTools to the inspector URL printed to stderr. Navigate to Performance > Record, reproduce the load spike, then stop. Alternatively, start with inspector enabled:

```bash
node --inspect app.js
```

For command-line profiling without DevTools:

```bash
node --prof app.js
node --prof-process isolate-*.log > profile.txt
grep -A5 "Bottom up (heavy) profile" profile.txt | head -40
```

Expected output: The widest flame bar or highest tick-count entry in `profile.txt` identifies the blocking function.

### Step 5: Check V8 garbage collection pause durations

```bash
node --trace-gc app.js 2>&1 | grep -E "Mark-Compact|Scavenge" | head -20
```

Expected output: Scavenge (young generation) pauses under 5 ms; Mark-Compact (full GC) pauses under 50 ms. Mark-Compact pauses above 100 ms indicate heap pressure contributing to event loop lag.

### Step 6: Check libuv thread pool saturation

```bash
node -e "
setInterval(() => {
  console.log('handles:', process._getActiveHandles().length,
              'requests:', process._getActiveRequests().length,
              'UV_THREADPOOL_SIZE:', process.env.UV_THREADPOOL_SIZE || '4');
}, 2000);
"
```

Expected output: Active requests consistently at or above `UV_THREADPOOL_SIZE` (default 4) means async operations that use the thread pool — including `dns.lookup()`, `fs.*`, and `crypto` — are queuing behind each other, which manifests as event loop lag despite a non-CPU-bound main thread.

### Step 7: Audit regular expressions for catastrophic backtracking

```bash
npx safe-regex-cli --pattern "$(grep -roh "'[^']*'" src/ | head -50)"
```

Or install the audit tool and scan:

```bash
npm install -g safe-regex
node -e "
const safe = require('safe-regex');
const patterns = [/(\/.+)+$/, /(a+)+/, /(a|a)*/];
patterns.forEach(p => console.log(p, safe(p) ? 'SAFE' : 'VULNERABLE'));
"
```

Expected output: Any pattern printed as `VULNERABLE` is a ReDoS risk when applied to untrusted user input.

## Causes

### Cause A: Synchronous blocking API in a request handler

**Statement:** A `*Sync` method (`fs.readFileSync`, `crypto.pbkdf2Sync`, `zlib.deflateSync`, `child_process.execSync`) is called inside a request handler, monopolizing the main thread for the duration of each call.

**Mechanism:** Node.js executes JavaScript on a single thread. Synchronous APIs block the event loop phases until they return, preventing all other callbacks — including timers, health checks, and other request handlers — from running. A single call to `fs.readFileSync` on a network-mounted filesystem can block for hundreds of milliseconds to seconds.

**Indicator:**

- [Step 3] `grep` output lists one or more `*Sync(` calls inside `src/` handler files
- [Step 4] Flame graph shows a wide bar for a `fs`, `crypto`, or `zlib` synchronous call originating from a route handler

<!-- match: {"step": 3, "predicate": "contains", "target": "Sync("} -->

**Mitigation:**

- **Risk:** None for replacing `*Sync` with async equivalents during a deployment; request handlers will continue processing.
- **Command:**

  ```bash
  # Identify all Sync call sites with file + line for targeted replacement
  grep -rn "Sync(" --include="*.js" --include="*.ts" src/ > /tmp/sync-calls.txt
  cat /tmp/sync-calls.txt
  ```

- **Duration:** Permanent code change; no temporary mitigation available without a restart.

**Resolution:**

```javascript
// Replace synchronous file reads with async equivalents
// BEFORE
const data = fs.readFileSync('/path/to/file', 'utf8');

// AFTER (promise-based)
const data = await fs.promises.readFile('/path/to/file', 'utf8');

// AFTER (streams for large files)
const stream = fs.createReadStream('/path/to/file');
stream.pipe(res);
```

**Verification:** Re-run Step 1 after deploying the fix. p99 event loop lag should drop below 20 ms. Confirm Step 3 grep returns no `*Sync` calls in request handler paths.

---

### Cause B: Large JSON serialization or deserialization on the main thread

**Statement:** Calling `JSON.parse()` or `JSON.stringify()` on payloads larger than approximately 10 MB blocks the main thread for hundreds of milliseconds to over a second.

**Mechanism:** The V8 JSON parser and serializer are synchronous and run on the main thread. Benchmarks show `JSON.stringify()` on a 50 MB object takes approximately 0.7 seconds and `JSON.parse()` takes approximately 1.3 seconds, blocking all other callbacks for the duration. Large request bodies passed without a size limit allow any client to trigger this block on demand.

**Indicator:**

- [Step 3] `grep` finds `JSON.parse` or `JSON.stringify` inside a request handler
- [Step 4] Flame graph shows a wide `JSON.parse` or `JSON.stringify` bar
- [Symptom] Lag spikes correlate with large-payload requests visible in access logs

<!-- match: {"step": 3, "predicate": "contains", "target": "JSON.parse"} -->

**Mitigation:**

- **Risk:** Low; adding a payload size limit rejects oversized requests with a 413 error before parsing occurs.
- **Command:**

  ```bash
  # For Express: set body size limit immediately to prevent further blocking
  # Add or update in app entry point:
  # app.use(express.json({ limit: '1mb' }));
  grep -rn "express.json\|bodyParser.json" --include="*.js" --include="*.ts" src/
  ```

- **Duration:** Permanent — apply the limit and deploy.

**Resolution:**

```javascript
// Set a payload size limit in Express
app.use(express.json({ limit: '1mb' }));

// For payloads that must exceed 1 MB, use streaming JSON parsers:
const { createStream } = require('stream-json');
const { streamArray } = require('stream-json/streamers/StreamArray');

const pipeline = fs.createReadStream('large.json')
  .pipe(createStream())
  .pipe(streamArray());

pipeline.on('data', ({ value }) => processItem(value));
```

**Verification:** Re-run Step 1 under load with a payload at or below the new limit. Lag should remain below 20 ms p99. Confirm 413 responses for oversized payloads.

---

### Cause C: CPU-bound computation in a request handler (O(n²) or heavy transform)

**Statement:** A request handler performs a CPU-intensive synchronous computation — sorting, encryption, hashing, data transformation, or nested iteration — that takes more than 5 ms per call and monopolizes the main thread.

**Mechanism:** Any synchronous JavaScript that runs for more than a few milliseconds on the main thread prevents the event loop from advancing. Unlike `*Sync` I/O calls, these are pure-JavaScript algorithms where the only fix is to offload the work to a Worker Thread or restructure the algorithm. Common patterns are O(n²) nested loops over arrays received from user input and synchronous crypto operations on large data.

**Indicator:**

- [Step 1] p99 lag spikes correlate with request volume, not a fixed interval
- [Step 2] Main thread CPU stays near 100% during lag spikes
- [Step 4] Flame graph shows a wide bar for an application function (not a Node core function)

<!-- match: {"step": 2, "predicate": "threshold", "target": "cpu_pct", "op": ">", "value": 90} -->

**Mitigation:**

- **Risk:** Low; adding Worker Thread offloading is non-breaking and isolates the computation.
- **Command:**

  ```bash
  # Identify the blocking function name from Step 4 profile output
  grep -A20 "Bottom up" profile.txt | head -30
  # Scale horizontally to absorb immediate load while fix is prepared
  pm2 scale app +2
  # Or for Kubernetes:
  kubectl scale deployment <name> -n <namespace> --replicas=4
  ```

- **Duration:** Horizontal scale is a temporary measure; the Worker Thread fix is the permanent resolution.

**Resolution:**

```javascript
// Offload CPU-intensive work to a worker thread pool using piscina
const Piscina = require('piscina');
const pool = new Piscina({ filename: './worker.js', maxThreads: 4 });

// In request handler — now non-blocking
app.post('/compute', async (req, res) => {
  const result = await pool.run(req.body);
  res.json(result);
});

// worker.js — runs in separate thread
module.exports = ({ data }) => {
  // CPU-intensive work here — does NOT block the event loop
  return expensiveTransform(data);
};
```

**Verification:** Re-run Step 1 and Step 2 under load after deploying the worker pool. Main thread CPU should drop below 20%, and p99 lag should fall below 20 ms.

---

### Cause D: ReDoS — catastrophic regular expression backtracking

**Statement:** A regular expression with nested quantifiers or overlapping alternations is applied to untrusted user input, causing exponential backtracking that blocks the main thread indefinitely.

**Mechanism:** Patterns such as `/(a+)+/`, `/(\/.+)+$/`, or `/(a|a)*/` exhibit super-linear worst-case complexity. A carefully crafted input string (e.g., 100 forward slashes followed by a newline) can cause the V8 regex engine to spend seconds or minutes exploring exponentially many match paths, pinning the main thread. This is a denial-of-service vector exploitable by any client.

**Indicator:**

- [Step 7] `safe-regex` reports `VULNERABLE` for a pattern present in the codebase
- [Step 4] Flame graph shows a wide `RegExp.exec` or `String.match` bar
- [Symptom] Lag spikes occur only on specific input shapes, not uniformly under load

<!-- match: {"step": 7, "predicate": "contains", "target": "VULNERABLE"} -->

**Mitigation:**

- **Risk:** Low; replacing vulnerable patterns with `String.indexOf` or `re2` does not change externally observable behavior for valid inputs.
- **Command:**

  ```bash
  # Replace simple path checks with indexOf to eliminate regex exposure immediately
  # VULNERABLE: if (input.match(/(\/.+)+$/)) { ... }
  # SAFE:       if (input.indexOf('/') !== -1) { ... }
  npm install re2
  # Use re2 as a drop-in replacement for untrusted-input patterns
  ```

- **Duration:** Permanent code change.

**Resolution:**

```javascript
// Option 1: Replace with simple string methods
if (filePath.indexOf('/') !== -1) { /* safe */ }

// Option 2: Use Google RE2 (linear-time engine) as drop-in replacement
const RE2 = require('re2');
const safePattern = new RE2(/complex-but-valid-pattern/);
if (safePattern.test(userInput)) { /* safe */ }
```

**Verification:** Re-run Step 7 — `safe-regex` should report no `VULNERABLE` patterns. Apply input from Step 7 that triggered the vulnerability; response time should remain under 100 ms.

---

### Cause E: libuv thread pool saturation blocking async I/O

**Statement:** The default libuv thread pool size of 4 is insufficient for the workload, causing async operations (DNS lookups via `dns.lookup`, file I/O, crypto) to queue and appear as event loop lag even though the main thread is not CPU-bound.

**Mechanism:** Node.js delegates certain "async" operations — `dns.lookup()`, `fs.*` callbacks, and `crypto.randomBytes()` — to a pool of libuv worker threads. With the default pool size of 4, only 4 such operations can execute in parallel. Additional operations queue on the event loop until a thread is free, creating measurable lag without main-thread CPU saturation.

**Indicator:**

- [Step 6] Active requests consistently at or above `UV_THREADPOOL_SIZE` value
- [Step 2] Main thread CPU is low (below 30%) during lag spikes
- [Step 1] Lag is present but no blocking patterns found in Step 3 or Step 4

<!-- match: {"step": 6, "predicate": "threshold", "target": "active_requests", "op": ">=", "value": 4} -->

**Mitigation:**

- **Risk:** Low; each additional thread uses approximately 1 MB of stack memory.
- **Command:**

  ```bash
  export UV_THREADPOOL_SIZE=16
  node app.js
  ```

  For Kubernetes, patch the deployment environment:

  ```bash
  kubectl set env deployment/<name> UV_THREADPOOL_SIZE=16 -n <namespace>
  ```

- **Duration:** Requires application restart; effective immediately after restart.

**Resolution:**

```javascript
// For DNS-heavy workloads, switch from dns.lookup() (thread pool)
// to dns.resolve() (c-ares, bypasses thread pool entirely)
const dns = require('dns');

// BEFORE (uses thread pool)
dns.lookup('example.com', (err, address) => { /* ... */ });

// AFTER (bypasses thread pool)
dns.resolve4('example.com', (err, addresses) => { /* ... */ });
```

- **Impact:** `UV_THREADPOOL_SIZE` increase is process-wide; applies to all async I/O and crypto in the same process. Requires restart.
- **Rollback:** Set `UV_THREADPOOL_SIZE=4` and restart.

**Verification:** Re-run Step 6 after restart. Active requests should stay consistently below `UV_THREADPOOL_SIZE`. Re-run Step 1; p99 lag should fall below 20 ms.

---

### Cause F: V8 garbage collection pauses on oversized heap

**Statement:** V8 Mark-Compact (full GC) pauses exceed 100 ms because the application heap has grown too large or the allocation rate is too high, causing periodic event loop stalls.

**Mechanism:** V8's GC must pause the JavaScript thread during Mark-Compact collection to compact and free old-generation memory. As heap size grows, scan time increases proportionally. Applications with high object allocation rates or large caches held in memory produce frequent, long GC pauses that appear as periodic event loop lag spikes even when no application code is blocking.

**Indicator:**

- [Step 5] `--trace-gc` output shows `Mark-Compact` lines with pause times above 100 ms
- [Step 1] Lag spikes occur at a regular interval (every 30–120 s) rather than correlating with request volume
- [Step 2] Main thread CPU spikes briefly (5–15 s) and returns to baseline between spikes

<!-- match: {"step": 5, "predicate": "contains", "target": "Mark-Compact"} -->

**Mitigation:**

- **Risk:** Low; `--max-old-space-size` increase allows V8 to defer GC, which reduces pause frequency at the cost of higher memory usage.
- **Command:**

  ```bash
  # Increase old-space limit to reduce GC frequency (restart required)
  node --max-old-space-size=4096 app.js
  # Confirm current heap limit
  node -e "const v8=require('v8'); console.log(v8.getHeapStatistics())"
  ```

- **Duration:** Requires restart; effective immediately.

**Resolution:**

```javascript
// Reduce allocation rate by reusing objects and capping in-memory caches
const LRU = require('lru-cache');
const cache = new LRU({ max: 500, ttl: 1000 * 60 * 5 });  // 500-item LRU, 5-min TTL

// Avoid patterns that create many short-lived large objects in hot paths
// BEFORE (new array each request)
const sorted = [...largeArray].sort(compareFn);

// AFTER (reuse a pre-allocated buffer when possible)
largeArray.sort(compareFn);  // in-place if mutation is acceptable
```

**Verification:** Re-run Step 5 after applying changes. Mark-Compact pause durations should drop below 50 ms. Re-run Step 1; periodic lag spikes should disappear.

---

### Cause Z: Unidentified event loop blocking

**Statement:** Event loop lag is confirmed but the root cause cannot be identified from the diagnostic steps above.

**Mechanism:** [Default]

**Indicator:**

- [Default] Steps 1–7 show elevated lag but no specific pattern, blocking call, or GC cause is identified

**Mitigation:**

- **Risk:** Low; Clinic.js Doctor performs non-invasive profiling and produces an HTML report without requiring code changes.
- **Command:**

  ```bash
  npx clinic doctor -- node app.js
  # Generate representative load, then Ctrl+C
  npx clinic flame -- node app.js
  # Generate load, then Ctrl+C
  ```

- **Duration:** Profile session (typically 60–120 s of load); no application changes required.

**Resolution:** Out of runbook scope. Escalate with the Clinic Doctor HTML report and flame graph to the Node.js maintainer or application team for deeper investigation.

**Verification:** The Clinic Doctor report diagnoses one of: "Event loop is blocked" (red), "I/O issue" (orange), "Memory issue" (orange), or "Healthy" (green). Use the identified category to return to the appropriate Cause subsection above.

## Prevention

- Enforce `no-sync` ESLint rule (`eslint-plugin-n`) across all source files; allow `*Sync` calls only in startup/initialization paths via inline `// eslint-disable-next-line` comments with a justification comment.
- Export event loop lag as a Prometheus histogram using `monitorEventLoopDelay` from `perf_hooks`. Alert when p99 exceeds 50 ms for 5 consecutive minutes.
- Set request body size limits to 1 MB (`express.json({ limit: '1mb' })`) as a default; require explicit justification for larger limits documented in code review.
- Run `npx safe-regex` or `recheck` in CI against all regular expression literals touching untrusted input; reject patterns that fail the linearity check.
- Set `UV_THREADPOOL_SIZE=16` (or higher) for services with heavy DNS, file I/O, or crypto usage; the default of 4 is insufficient for most production workloads.
- Establish a convention that any synchronous operation expected to exceed 5 ms must be offloaded to a worker pool (`piscina` or `workerpool`); enforce via code-review checklist.
- Run all Node.js services in cluster mode or with multiple replicas (`pm2 start -i max` or Kubernetes with at least 2 pods) so a single blocked worker does not take down all request handling.
- Cap all in-memory caches with LRU eviction and TTLs to control heap growth and reduce GC pause frequency.

## Sources

- [Node.js — Don't Block the Event Loop](https://nodejs.org/en/learn/asynchronous-work/dont-block-the-event-loop) — Official guide covering dangerous APIs, JSON thresholds (0.7 s / 1.3 s at 50 MB), ReDoS patterns, partitioning, and Worker Pool task-time bounding; Priority 1
- [Node.js — Performance Hooks: monitorEventLoopDelay](https://nodejs.org/api/perf_hooks.html) — Official API reference for `IntervalHistogram`, resolution parameter, percentile access, and nanosecond-to-millisecond conversion; Priority 1
- [Clinic.js](https://clinicjs.org/) — Open-source performance profiling suite for Node.js: Doctor (automated event loop / I/O / memory diagnosis), Flame (CPU flame graphs), Bubbleprof (async flow visualization); Priority 2
- [NodeSource — Debugging the Event Loop](https://nodesource.com/blog/node-js-performance-monitoring-part-3-debugging-the-event-loop) — CPU profiling techniques, `--prof` flag workflow, and flame graph interpretation for event loop issues; Priority 3
- [Trigger.dev — How We Tamed Node.js Event Loop Lag](https://trigger.dev/blog/event-loop-lag) — Production case study: O(n²) algorithm discovery, `monitorEventLoopDelay` instrumentation, payload size limits, and OpenTelemetry integration; Priority 3
