---
id: "golang-goroutine-leak"
title: "Go goroutine and memory leaks: pprof and runtime diagnosis"
domain: application
service: golang
symptom_class: [oom, latency]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [goroutine-leak, pprof, heap-profile, oom, blocked-channel]
difficulty: advanced
---

## Symptom Recognition

- Process RSS grows monotonically and never returns to baseline after load subsides; eventually `fatal error: runtime: out of memory` or OOM-killed by the kernel/cgroup (`dmesg` shows `Killed process ... (<binary>) total-vm ... anon-rss`).
- `/debug/pprof/goroutine?debug=1` header `goroutine profile: total N` keeps climbing across scrapes and never plateaus.
- `GODEBUG=gctrace=1` lines show the live-heap term (`#->#-># MB`) trending up each GC, e.g. `gc 412 @980s 3%: ... 1801->1810->1805 MB, ...`.
- Request latency (p99) rises as GC works harder against a growing live set; GC CPU percentage in gctrace climbs.
- Expvar/Prometheus gauge for `runtime.NumGoroutine()` shows an upward ramp with no decay.

## Applicability

- Go 1.21+ services that import `net/http/pprof` (or register `runtime/pprof` profiles) and expose a debug HTTP listener.
- Required access: network reachability to the pprof endpoint (commonly `localhost:6060`), shell access to run `go tool pprof` / `go tool trace`, and permission to read kernel logs (`dmesg`) or container OOM events.
- Tools: the Go toolchain (`go tool pprof`, `go tool trace`), `curl`, `graphviz` (for `-svg`/`web` output).
- Assumes the binary was built with symbols (default) so pprof can resolve stack frames.

## Diagnostic Steps

### Step 1: Confirm goroutine count is growing
Scrape the goroutine profile header twice with a delay and compare totals.

```bash
curl -s "http://localhost:6060/debug/pprof/goroutine?debug=1" | head -1
sleep 60
curl -s "http://localhost:6060/debug/pprof/goroutine?debug=1" | head -1
```

Expected output: two lines like `goroutine profile: total 5821` and `goroutine profile: total 6240` (a healthy service holds roughly steady; a rising total indicates leaking goroutines).

### Step 2: Group leaked goroutines by their blocking call site
Aggregate goroutine stacks to find the dominant blocked location.

```bash
go tool pprof -top -lines "http://localhost:6060/debug/pprof/goroutine"
```

Expected output: a ranked list where one frame (e.g. `runtime.gopark` / `runtime.chanrecv` / `sync.runtime_Semacquire`) accounts for the bulk of goroutines, with a deep stack pointing at one application function.

### Step 3: Read full stacks and wait state of blocked goroutines
Dump human-readable stacks with the wait reason and how long each goroutine has been blocked.

```bash
curl -s "http://localhost:6060/debug/pprof/goroutine?debug=2" | grep -A1 -E "^goroutine [0-9]+ \[(chan receive|chan send|select|semacquire|IO wait)" | head -40
```

Expected output: blocks like `goroutine 4827 [chan receive, 32 minutes]:` followed by the application frame; minute-scale wait durations on many goroutines confirm they are stuck, not transiently busy.

### Step 4: Identify what holds the live heap
Collect a heap profile (live, in-use bytes) and rank allocation sites.

```bash
go tool pprof -inuse_space -top "http://localhost:6060/debug/pprof/heap?gc=1"
```

Expected output: a `flat/cum` table sorted by in-use bytes; a single allocation site (often a map/slice append or a cache `Set`) dominating `inuse_space` points at retained memory.

### Step 5: Confirm heap is retained across GC (true leak vs. churn)
Compare live heap reported by the GC over time.

```bash
GODEBUG=gctrace=1 <your-binary> 2>&1 | grep -oE "[0-9]+->[0-9]+->[0-9]+ MB" | tail -5
```

Expected output: the third number in `start->end->live MB` (the live set) increasing across successive GCs (e.g. `... 1805 MB` then `... 1860 MB`); a flat live term means the growth is transient allocation, not a leak.

## Causes

### Cause A: Goroutines blocked forever on an unbuffered/unread channel
**Statement:** Producer goroutines send on (or receive from) a channel whose counterpart never runs — the receiver returned early or was never started — so each goroutine parks permanently and its captured stack/closure memory is never freed.
**Chain:**
- root: a channel operation has no live counterpart (send with no receiver, or receive with no sender/close)
- s1: each affected goroutine parks in `chanrecv`/`chansend` and is never rescheduled
- s2: parked goroutines and everything their stacks/closures reference are pinned, so live heap and goroutine count grow with traffic
- D: RSS climbs until OOM and p99 latency rises (Symptom Recognition)
**Indicators:**
- root: [Step 3] stacks read `[chan receive, ...]` or `[chan send, ...]` with multi-minute durations at the same application frame
- s1: [Step 2] one channel call site (e.g. `runtime.chanrecv`) dominates the aggregated goroutine profile
- s2: [Step 1] goroutine `total` rises across the two scrapes
**Interventions:**
- **remediation** (root): Make the channel's lifecycle complete — buffer it adequately, guarantee a receiver, or have the producer honor a cancellation channel so the send/receive can never block forever.

  ```go
  // Producer obeys cancellation instead of blocking forever on send:
  select {
  case ch <- v:
  case <-ctx.Done():
      return ctx.Err()
  }
  ```
  **Verification:** Re-run Step 1; the goroutine `total` plateaus, and Step 3 no longer shows long-duration `chan receive`/`chan send` waits at that frame.
- **defensive_fix** (s1): Give every long-lived goroutine a `context.Context` deadline/cancel so a stuck channel op unblocks on timeout instead of parking permanently.

  ```go
  ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
  defer cancel()
  ```
  **Verification:** Trigger the missing-counterpart condition under test; goroutines exit within the timeout and `runtime.NumGoroutine()` returns to baseline.

### Cause B: HTTP/RPC requests issued without a timeout or context cancellation
**Statement:** Outbound calls (or handlers) are made with no client timeout and no request context, so when a dependency stalls the calling goroutine blocks indefinitely in I/O wait, accumulating one stuck goroutine per in-flight request.
**Chain:**
- root: client/handler created without a timeout or cancellable context (e.g. `&http.Client{}` with zero `Timeout`)
- s1: when the peer hangs, the calling goroutine blocks in `net` I/O wait and never returns
- s2: under sustained traffic, blocked goroutines and their request buffers pile up, inflating goroutine count and heap
- D: RSS climbs until OOM and p99 latency rises (Symptom Recognition)
**Indicators:**
- root: [Step 2] aggregated profile concentrates in `net/http.(*persistConn).roundTrip` or a `database/sql` acquire frame
- s1: [Step 3] stacks show `[IO wait, ...]` or `[select, ...]` at the call site with growing durations
- s2: [Step 1] goroutine `total` climbs in step with request rate
**Interventions:**
- **remediation** (root): Set an explicit client timeout and thread a request-scoped context through every outbound call so a stalled peer cannot pin the goroutine.

  ```go
  client := &http.Client{Timeout: 5 * time.Second}
  req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
  resp, err := client.Do(req)
  ```
  **Verification:** Re-run Step 1 under a slow/blackholed dependency; stuck goroutines now error out after the timeout and the `total` stays flat.
- **mitigation** (s1): Lower the kernel/load-balancer idle timeout fronting the service to forcibly close hung upstream connections, freeing blocked goroutines sooner.

  ```bash
  # Example: trim NGINX upstream read timeout in front of the service
  sed -i 's/proxy_read_timeout .*/proxy_read_timeout 10s;/' /etc/nginx/conf.d/app.conf && nginx -s reload
  ```
  **Risk:** Aborts legitimately slow requests and shifts the leak upstream; does not fix the missing client timeout. **Duration:** Until the code fix ships (hours, not days). **Verification:** Step 1 `total` growth slows; Step 3 shows fewer long `IO wait` durations.

### Cause C: Unbounded in-memory cache or map that is only ever written
**Statement:** A package-level map or cache is populated on every request/key but has no eviction, TTL, or size cap, so live entries accumulate forever and dominate the heap independent of goroutine count.
**Chain:**
- root: a long-lived map/cache grows without any eviction or size bound
- s1: every distinct key adds a permanently-retained entry the GC cannot reclaim (still reachable)
- s2: `inuse_space` for that allocation site grows linearly with unique keys/traffic
- D: live heap climbs until OOM and GC pressure raises p99 latency (Symptom Recognition)
**Indicators:**
- root: [Step 4] one allocation site (the map insert / cache `Set`) dominates `inuse_space` in the heap top
- s1: [Step 5] the live-heap term in gctrace rises monotonically across GCs while goroutine count stays flat
- s2: [Step 1] goroutine `total` is roughly stable (distinguishes this from a goroutine leak)
**Interventions:**
- **remediation** (root): Bound the cache — add an LRU with a max size or a per-entry TTL so retained memory is capped regardless of key cardinality.

  ```go
  // golang.org/x/sync or a bounded LRU; size cap evicts oldest entries:
  cache, _ := lru.New[string, []byte](10_000) // hard cap on live entries
  cache.Add(key, val)
  ```
  **Verification:** Re-run Step 4 after sustained traffic; the cache allocation site's `inuse_space` plateaus at the cap, and Step 5's live-heap term flattens.
- **mitigation** (s1): Restart the process on a rolling schedule to reclaim the unbounded heap before it reaches the OOM threshold.

  ```bash
  kubectl rollout restart deployment/<app>
  ```
  **Risk:** Drops in-flight work and only resets the clock; the cache refills and grows again. **Duration:** One growth cycle (e.g. until next restart window). **Verification:** RSS drops to baseline post-restart; Step 4 in-use bytes reset, then resume climbing without the code fix.

### Cause Z: Unidentified
**Statement:** The goroutine/heap growth does not match any known leak signature above (channel block, missing-timeout I/O wait, or unbounded cache), so the root cause is not yet identified and a full diagnostic snapshot is needed for SME review.
**Indicators:**
- [Default] None of the Cause A–C indicators match: no dominant blocking channel frame, no I/O-wait pileup, and no single allocation site dominating `inuse_space`.
**Interventions:**
- **mitigation** (D): Capture a complete diagnostic bundle (full goroutine dump, heap profile, allocs profile, and an execution trace) and escalate to the service owner / Go SME.

  ```bash
  curl -s "http://localhost:6060/debug/pprof/goroutine?debug=2" > goroutine_full.txt
  curl -s "http://localhost:6060/debug/pprof/heap?gc=1"        > heap.pprof
  curl -s "http://localhost:6060/debug/pprof/allocs"           > allocs.pprof
  curl -s "http://localhost:6060/debug/pprof/trace?seconds=5"  > trace.out
  go tool trace trace.out  # inspect goroutine creation/blocking timeline
  ```
  **Risk:** Snapshot collection adds brief CPU/latency overhead and does not stop the leak; pair with a restart if OOM is imminent. **Duration:** Until SME completes analysis. **Verification:** Bundle opens cleanly in `go tool pprof`/`go tool trace` and is handed to the SME with the Step 1–5 outputs.

## Prevention

- Add a `runtime.NumGoroutine()` gauge and `runtime.ReadMemStats().HeapInuse` gauge to your metrics, with an alert on sustained upward slope (e.g. NumGoroutine up >20% over 30m with no traffic increase).
- Enforce a non-zero `Timeout` on every `http.Client` and require `http.NewRequestWithContext` / context-aware DB calls via lint rules; never construct `&http.Client{}` with a zero timeout.
- Bound every long-lived cache/map with an LRU size cap or TTL; ban package-level maps that are only ever written.
- Keep `net/http/pprof` registered on an internal-only listener so profiles are always one `go tool pprof` away in production.
- Run `GODEBUG=gctrace=1` in staging load tests and fail the test if the live-heap term grows monotonically under steady load.
- Add goroutine-leak detection to integration tests (snapshot `runtime.NumGoroutine()` before/after each test and assert it returns to baseline).

## Sources

- [Diagnostics](https://go.dev/doc/diagnostics) — Go Diagnostics: profiling vs. tracing overview, heap profile semantics (`inuse_space`), `runtime.ReadMemStats` / `MemStats` fields (HeapAlloc, HeapInuse, HeapObjects, NumGC), `runtime.NumGoroutine()` for leak detection, and `GODEBUG=gctrace=1` output format.
- [Pprof](https://pkg.go.dev/net/http/pprof) — exact pprof HTTP endpoints (`/debug/pprof/goroutine`, `/debug/pprof/heap`, `/debug/pprof/allocs`, `/debug/pprof/trace`), handler registration (`import _ "net/http/pprof"`, `localhost:6060`), and the `debug=1`/`debug=2` and `gc=`/`seconds=` query parameters used in the diagnostic steps.
- [Pprof](https://pkg.go.dev/runtime/pprof) — runtime/pprof goroutine and heap profile types and the leaked-goroutine (blocked on channel/mutex/cond) definition used to frame the leak signatures.
- [Pprof](https://go.dev/blog/pprof) — `go tool pprof` `top`/`list` usage and heap profile interpretation referenced in Steps 2 and 4.
