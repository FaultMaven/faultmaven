---
id: redis-oom
title: "Redis Out of Memory (maxmemory exceeded)"
domain: database
service: redis
symptom_class:
  - oom
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: kb-researcher
status: draft
tags:
  - redis
  - oom
  - maxmemory
  - eviction
  - fragmentation
  - bigkeys
difficulty: intermediate
---

# Redis Out of Memory (maxmemory exceeded)

## Symptom Recognition

- Write commands return the error string `OOM command not allowed when used memory > 'maxmemory'.` to clients; reads (`GET`, `SMEMBERS`, etc.) continue to succeed.
- Application logs surface elevated cache-write failure rates, cache-miss-driven database load, and `redis.exceptions.OutOfMemoryError` (or equivalent in the client library).
- `INFO memory` shows `used_memory` at or just below `maxmemory`, with `evicted_keys` in `INFO stats` either climbing rapidly (eviction policy active) or flat at zero (`noeviction`).
- `INFO memory` reports `mem_fragmentation_ratio` significantly above 1.5, or `used_memory_rss` materially exceeding `used_memory`, indicating fragmentation pressure even when logical usage looks healthy.
- Redis server log contains entries such as `Background saving terminated by signal 9`, `WARNING overcommit_memory is set to 0!`, or `Asynchronous AOF fsync is taking too long (disk is busy?)` correlated with the OOM window.
- When `maxmemory` is unset (default 0), the Linux OOM killer terminates `redis-server` instead — `dmesg` shows `Out of memory: Killed process <pid> (redis-server)` (this is the distinct `linux-oom-killer` failure mode; this runbook covers Redis's internal ceiling).

## Applicability

- Redis Open Source 6.0+ (commands and metrics referenced below are stable through 7.x; `MEMORY USAGE`, `MEMORY STATS`, `MEMORY DOCTOR`, and `UNLINK` require 4.0+).
- Requires `redis-cli` access (TCP or Unix socket) with permissions for `INFO`, `CONFIG GET/SET`, `MEMORY USAGE`, `MEMORY STATS`, `MEMORY DOCTOR`, `CLIENT LIST`, `SLOWLOG`, and `--bigkeys`. If Redis ACLs are enabled (6.0+), the diagnostic user needs `+@admin +@slow +@connection` or equivalent.
- Host shell access on the Redis node is required for `dmesg`, `/proc/<pid>/smaps`, `vmstat`, and `sysctl vm.overcommit_memory`.
- Persistence-related diagnostics assume RDB and/or AOF is enabled; skip Step 7 for cache-only deployments with `save ""` and `appendonly no`.
- Managed Redis services (AWS ElastiCache, GCP Memorystore, Azure Cache for Redis) restrict `CONFIG SET` and `DEBUG` commands — use the cloud console to change `maxmemory`/`maxmemory-policy` and a parameter group restart for any setting flagged "modifiable: false at runtime".

## Diagnostic Steps

### Step 1: Confirm the OOM error and current memory pressure

```bash
redis-cli INFO memory | grep -E '^(used_memory|used_memory_human|used_memory_rss_human|used_memory_peak_human|maxmemory_human|maxmemory_policy|mem_fragmentation_ratio|mem_allocator):'
```

Expected output: one line per field. Compare `used_memory_human` against `maxmemory_human`; a ratio above 0.95 means the instance is at or against the ceiling. `maxmemory_policy` shows the active eviction policy (one of `noeviction`, `allkeys-lru`, `allkeys-lfu`, `allkeys-random`, `volatile-lru`, `volatile-lfu`, `volatile-random`, `volatile-ttl`).

### Step 2: Check eviction and rejection metrics

```bash
redis-cli INFO stats | grep -E '^(evicted_keys|expired_keys|keyspace_hits|keyspace_misses|rejected_connections):'
```

Expected output: cumulative counters since last restart. A non-zero and rapidly increasing `evicted_keys` means the policy is actively reclaiming. A flat `evicted_keys=0` while clients see OOM means the policy is `noeviction` or `volatile-*` with no TTL keys to evict. Re-run after 60 seconds to compute the delta rate.

### Step 3: Capture Redis's own memory advisory

```bash
redis-cli MEMORY DOCTOR
```

Expected output: a free-form advisory string. Phrases such as `high allocator fragmentation`, `high total RSS`, `high allocator RSS overhead`, or `Sam, I detected a few issues in this Redis instance memory implants` indicate fragmentation; `Peak memory: in the past this instance used more than 150% the memory that is currently using` indicates the working set has shrunk and RSS is held by the allocator. The literal `Sam, I detected a few issues` substring is emitted when at least one issue is detected; `Sam, I think there is no need to fix any memory issue` indicates a clean check.

### Step 4: Identify the largest keys per data type

```bash
redis-cli --bigkeys
```

Expected output: a scan summary listing the biggest key found for each Redis data type (string, hash, list, set, sorted set, stream) along with size in bytes or element count. Keys whose `MEMORY USAGE` exceeds 1 MiB, hashes/sets/zsets with more than ~10,000 elements, or lists with more than ~10,000 entries are candidates for restructuring.

### Step 5: Probe specific suspect keys for byte-level usage

```bash
redis-cli MEMORY USAGE <key-name> SAMPLES 0
redis-cli MEMORY STATS | head -40
```

Expected output: `MEMORY USAGE` returns the total number of bytes the key and its value occupy (sampling all elements when `SAMPLES 0`). `MEMORY STATS` reports `dataset.bytes`, `overhead.total`, `keys.count`, `clients.normal`, `clients.slaves`, `aof.buffer`, and `lua.caches`. If `overhead.total` is a large fraction of `used_memory`, the issue is non-dataset memory (client output buffers, replication backlog, AOF buffer) rather than user data.

### Step 6: Inspect client output buffers for slow consumers

```bash
redis-cli CLIENT LIST | awk -F'[ =]' '{omem=0; addr=""; for(i=1;i<=NF;i++){if($i=="addr")addr=$(i+1); if($i=="omem")omem=$(i+1)} if(omem+0 > 1048576) print "addr="addr"  omem="omem}'
```

Expected output: zero lines on a healthy system. Any client with `omem` (output buffer memory) over 1 MiB is a slow consumer; pub/sub subscribers and replicas that lag are the usual culprits. Sum of `omem` across all clients should appear in `MEMORY STATS` as `clients.normal` + `clients.slaves` + `pubsub.clients`.

### Step 7: Check persistence (fork / copy-on-write) overhead

```bash
redis-cli INFO persistence | grep -E '^(rdb_bgsave_in_progress|rdb_last_bgsave_status|aof_rewrite_in_progress|aof_last_rewrite_time_sec|aof_current_size|aof_base_size|latest_fork_usec):'
```

Expected output: `rdb_bgsave_in_progress=1` or `aof_rewrite_in_progress=1` means a child process forked from `redis-server` is currently active; copy-on-write makes peak RSS temporarily approach 2× the dataset under heavy write load. `latest_fork_usec` (in microseconds) over `~250000` per GB of dataset indicates fork is itself slow and amplifies the COW window.

### Step 8: Sample keys for missing TTLs

```bash
redis-cli --no-raw EVAL "local n=0; local s=0; for i=1,200 do local k=redis.call('RANDOMKEY'); if k then s=s+1; if redis.call('TTL',k)==-1 then n=n+1 end end end; return {s,n}" 0
```

Expected output: a two-element array `[sampled, no_ttl]`. If `no_ttl / sampled > 0.5` on a cache-style deployment, half or more of sampled keys never expire — the keyspace will grow unbounded until `maxmemory` is hit regardless of policy.

### Step 9: Check Linux memory overcommit configuration

```bash
cat /proc/sys/vm/overcommit_memory
grep -i swap /proc/$(pgrep -of redis-server)/smaps_rollup 2>/dev/null || awk '/^Swap:/{s+=$2} END{print "Swap: "s" kB"}' /proc/$(pgrep -of redis-server)/smaps
```

Expected output: `vm.overcommit_memory` should be `1`. A value of `0` causes `fork()` for BGSAVE/AOF rewrite to fail under memory pressure with `Can't save in background: fork: Cannot allocate memory` in the Redis log even when free RAM is sufficient. The `Swap: 0 kB` line should remain zero — any swap usage by `redis-server` directly translates to multi-millisecond latency per page touch.

## Causes

### Cause A: maxmemory is unset or larger than physical RAM headroom

**Statement:** `maxmemory` is set to `0` (no limit) or to a value that, combined with fork/COW and allocator fragmentation, exceeds the headroom on the host, so memory growth either trips the kernel OOM killer or pushes RSS into swap.

**Mechanism:** Redis allocates from the system allocator with no ceiling when `maxmemory=0`. Resident memory grows with every write. BGSAVE and AOF rewrite fork the process; copy-on-write briefly doubles working memory for pages the parent modifies during the snapshot. When total RSS plus the COW spike exceeds physical RAM, the kernel either OOM-kills `redis-server` (if `vm.overcommit_memory=1`) or fails the fork with `ENOMEM` (if `vm.overcommit_memory=0`) and the parent process continues to grow until it is killed.

**Indicator:**

- [Step 1] `maxmemory_human:0B` or `maxmemory` greater than `(physical_RAM - other_processes) * 0.7`
<!-- match: {"step": 1, "predicate": "contains", "target": "maxmemory_human:0B"} -->
- [Step 9] `/proc/<pid>/smaps` shows non-zero `Swap:` for the redis-server process
- [Symptom] kernel `dmesg` contains `Out of memory: Killed process` referencing `redis-server`, OR Redis log contains `Can't save in background: fork: Cannot allocate memory`

**Mitigation:**

- **Risk:** Setting `maxmemory` too low under live traffic immediately triggers eviction or `OOM command not allowed` errors; clients must tolerate cache misses and write failures during the change.
- **Command:**

  ```bash
  redis-cli CONFIG SET maxmemory $(( $(awk '/MemTotal/{print $2}' /proc/meminfo) * 1024 * 60 / 100 ))
  redis-cli CONFIG SET maxmemory-policy allkeys-lru
  ```

- **Duration:** Permanent once sized from observed peak. Re-evaluate after any node-size change.

**Resolution:**

```bash
# Size maxmemory to leave at least 30% of physical RAM for fork/COW, allocator overhead, and OS page cache.
# Example for a 16 GiB host: cap Redis at ~10 GiB (60% of physical RAM).
redis-cli CONFIG SET maxmemory 10gb
redis-cli CONFIG SET maxmemory-policy allkeys-lru
redis-cli CONFIG REWRITE
```

**Impact:** Single-instance scope; takes effect immediately without restart. Existing clients are not disconnected. New writes that would push usage above the cap start triggering eviction (or `OOM command not allowed` if policy is `noeviction`).

**Rollback:**

```bash
# Restore previous limit (substitute the prior value captured from CONFIG GET maxmemory before the change).
redis-cli CONFIG SET maxmemory <previous-bytes>
redis-cli CONFIG REWRITE
```

**Verification:** After 5 minutes of representative traffic, `redis-cli INFO memory | grep used_memory_human` should stabilize at least 20% below `maxmemory_human`, and `redis-cli INFO stats | grep evicted_keys` delta should be bounded (not growing unboundedly).

### Cause B: Eviction policy is `noeviction` (or `volatile-*` with no TTL keys)

**Statement:** The active `maxmemory-policy` is `noeviction`, or it is one of the `volatile-*` policies but the keyspace contains no keys with TTLs, so when `used_memory` reaches `maxmemory` Redis rejects every write with `OOM command not allowed when used memory > 'maxmemory'`.

**Mechanism:** When a write would push `used_memory` beyond `maxmemory`, Redis consults `maxmemory-policy`. `noeviction` returns the `OOM command not allowed when used memory > 'maxmemory'` error immediately. `volatile-lru`, `volatile-lfu`, `volatile-random`, and `volatile-ttl` consider only keys with an associated TTL; if no such keys exist, they behave identically to `noeviction` and return the same error. Reads continue to succeed because they do not allocate.

**Indicator:**

- [Step 1] `maxmemory_policy:noeviction`
<!-- match: {"step": 1, "predicate": "contains", "target": "maxmemory_policy:noeviction"} -->
- [Step 2] `evicted_keys` is exactly `0` and not increasing despite `used_memory` at the ceiling
<!-- match: {"step": 2, "predicate": "contains", "target": "evicted_keys:0"} -->
- [Symptom] client error logs contain `OOM command not allowed when used memory > 'maxmemory'`

**Mitigation:**

- **Risk:** Switching to `allkeys-lru` immediately starts evicting application data, including keys the application assumed were durable. Use `volatile-lru` if any keys must never be evicted and the application reliably sets TTLs on the rest.
- **Command:**

  ```bash
  redis-cli CONFIG SET maxmemory-policy allkeys-lru
  ```

- **Duration:** Permanent for cache workloads. For mixed cache + durable-data deployments, switch to `volatile-lru` and audit TTL coverage with Step 8.

**Resolution:**

```bash
# Cache workload: evict any key by recency
redis-cli CONFIG SET maxmemory-policy allkeys-lru
# Or, for cache workload with hot-set skew, frequency-based
redis-cli CONFIG SET maxmemory-policy allkeys-lfu
# Or, mixed cache + durable data with TTLs set on cache entries only
redis-cli CONFIG SET maxmemory-policy volatile-lru
redis-cli CONFIG REWRITE
```

**Impact:** Instance-wide. Eviction starts immediately and continues whenever `used_memory` approaches `maxmemory`. Applications relying on key durability without TTLs will see cache-miss behavior for evicted keys.

**Rollback:**

```bash
redis-cli CONFIG SET maxmemory-policy noeviction
redis-cli CONFIG REWRITE
```

**Verification:** Write a probe key (`redis-cli SET fm_oom_probe ok EX 60`) and observe `OK`. After 5 minutes of traffic, `redis-cli INFO stats | grep evicted_keys` should show a non-zero, bounded delta — confirming eviction is now keeping `used_memory` below `maxmemory`.

### Cause C: Unbounded key growth from missing TTLs

**Statement:** The application stores cache-style data with no `EX`/`PX`/`EXPIRE`, so the keyspace grows monotonically and eventually fills `maxmemory` regardless of eviction policy choice.

**Mechanism:** Redis only expires keys lazily (on access) and via active expiration cycles; both rely on the key having a TTL. Keys written with bare `SET`, `HSET`, `LPUSH`, `SADD`, etc., have `TTL = -1` (no expiry). Under a `volatile-*` eviction policy, these keys are never eligible for eviction; under `allkeys-*` they are evicted by access pattern, but only after `used_memory` reaches the ceiling, with the steady-state working set roughly equal to `maxmemory`. Either way the ceiling is hit continuously, eviction pressure is constant, and any burst that outruns eviction returns `OOM command not allowed`.

**Indicator:**

- [Step 8] sampled key TTL distribution shows more than 50% of randomly sampled keys returning `TTL = -1`
- [Step 2] `evicted_keys` is high and growing alongside `keyspace_misses`, indicating eviction is removing keys applications still want
- [Symptom] code review shows `SET`/`HSET`/`LPUSH` calls without `EX`/`EXPIRE` for entries intended as cache

**Mitigation:**

- **Risk:** Adding TTLs via `EXPIRE` on existing keys is safe but applies only to keys touched by the cleanup script. Bulk `EXPIRE` over `SCAN` against a hot keyspace adds command load — pace it.
- **Command:**

  ```bash
  # Assign a 1-hour TTL to every key that currently has none. Paces 100 keys per pipeline.
  redis-cli --scan | while read -r k; do
    [ "$(redis-cli TTL "$k")" = "-1" ] && redis-cli EXPIRE "$k" 3600
  done
  ```

- **Duration:** Stopgap until application code is fixed. Without the code fix, newly written keys re-enter the keyspace without TTLs.

**Resolution:**

```python
# Application-side fix (Python redis-py example). Apply equivalent in all client codebases.
import redis
r = redis.Redis(host="redis", port=6379)

# WRONG: no TTL -> contributes to unbounded growth
# r.set("cache:user:123", payload)

# RIGHT: every cache write declares a TTL
r.set("cache:user:123", payload, ex=3600)              # 1 hour
r.hset("session:abc", mapping=session_data)
r.expire("session:abc", 1800)                          # 30 minutes
```

```bash
# Defense in depth at the server: use volatile-lru as a safety net once the app sets TTLs reliably
redis-cli CONFIG SET maxmemory-policy volatile-lru
redis-cli CONFIG REWRITE
```

**Verification:** After deploying the application fix, re-run Step 8 once an hour for 24 hours; the no-TTL fraction must trend toward 0 as keys without TTLs age out. `redis-cli INFO memory | grep used_memory_human` should stabilize below `maxmemory_human`.

### Cause D: Large keys (big keys) consuming disproportionate memory

**Statement:** A small number of individual keys — large strings, oversized hashes, lists, sets, or sorted sets — account for a large fraction of `used_memory`, so deleting or restructuring them frees substantial headroom even if the rest of the keyspace is healthy.

**Mechanism:** Redis stores all data in RAM; a single 100 MiB string occupies 100 MiB. Hashes, sets, and zsets above the `hash-max-listpack-entries` / `set-max-listpack-entries` / `zset-max-listpack-entries` thresholds are promoted from the compact listpack representation to hashtable/skiplist representations, which roughly doubles per-element overhead. Lists with millions of entries hold all entries in memory simultaneously. Big keys also stall the event loop on `DEL` (synchronous free), making OOM remediation itself a latency event unless `UNLINK` is used.

**Indicator:**

- [Step 4] `--bigkeys` reports at least one key whose size dominates the type's total
- [Step 5] `MEMORY USAGE <key> SAMPLES 0` returns a value greater than 1048576 (1 MiB) for individual keys
<!-- match: {"step": 5, "predicate": "threshold", "target": "memory_usage_bytes", "op": ">", "value": 1048576} -->
- [Step 3] `MEMORY DOCTOR` advisory mentions a high ratio between `used_memory_peak` and current `used_memory`, consistent with a small set of large keys

**Mitigation:**

- **Risk:** `UNLINK` is non-blocking but the freed memory is returned by the background thread asynchronously; immediate `used_memory` may not drop for several seconds on multi-GiB keys. `DEL` on a multi-GiB key blocks the event loop for hundreds of milliseconds — never use it on a hot instance.
- **Command:**

  ```bash
  # Non-blocking deletion of a known large key
  redis-cli UNLINK <large-key-name>
  # Or for a pattern of disposable scratch keys
  redis-cli --scan --pattern 'scratch:*' | xargs -L 100 redis-cli UNLINK
  ```

- **Duration:** Immediate relief; freed memory is reclaimed asynchronously. Without a data-model fix the same keys will be re-created.

**Resolution:**

```bash
# Cap collections at the application level so they cannot grow without bound:
# Lists: trim after each push
redis-cli LPUSH events:user:123 "$payload"
redis-cli LTRIM events:user:123 0 999          # keep most-recent 1000 entries

# Hashes/sets/zsets: shard by hashing the logical id into buckets
# e.g. instead of one 10M-field hash 'users', use 256 hashes 'users:{0..255}' keyed by id mod 256

# Strings: store large blobs in object storage (S3/GCS); keep only the URL or hash in Redis
```

**Impact:** Per-key change has bounded blast radius. Data-model refactors require code deploys and a backfill plan; coordinate with the team that owns the keys.

**Rollback:** Application code change; revert by deploying the previous image. Unsharding is a separate migration — design the shard scheme to be one-way.

**Verification:** After the fix is deployed, `redis-cli --bigkeys` must no longer report any single key over 1 MiB (or the team's agreed threshold). `redis-cli INFO memory | grep used_memory_human` should drop by at least the previously reported big-key footprint within 30 seconds of `UNLINK` completion.

### Cause E: Memory fragmentation inflates RSS far above logical usage

**Statement:** Allocator fragmentation has driven resident set size (RSS) well above the logical `used_memory` such that the OS sees the process at or above `maxmemory` while Redis reports headroom, causing eviction or `OOM command not allowed` even though dataset size is moderate.

**Mechanism:** Redis defaults to jemalloc. Long-lived instances with churn (many writes plus many deletes/expirations) leave the allocator holding pages whose objects have been freed but whose pages cannot be returned to the OS because some other live object still occupies them. `mem_fragmentation_ratio = used_memory_rss / used_memory` quantifies this; ratios above 1.5 are problematic. `maxmemory` is enforced against `used_memory` (logical), but the OS perceives `used_memory_rss` — so a 10 GiB `maxmemory` with ratio 2.0 means the process holds 20 GiB RSS, and fork-time COW or other host processes are starved.

**Indicator:**

- [Step 1] `mem_fragmentation_ratio` is greater than `1.5`
<!-- match: {"step": 1, "predicate": "threshold", "target": "mem_fragmentation_ratio", "op": ">", "value": 1.5} -->
- [Step 1] `mem_allocator:jemalloc-*` (active defrag is jemalloc-only)
<!-- match: {"step": 1, "predicate": "contains", "target": "mem_allocator:jemalloc"} -->
- [Step 3] `MEMORY DOCTOR` advisory contains the substring `high allocator fragmentation`
<!-- match: {"step": 3, "predicate": "contains", "target": "high allocator fragmentation"} -->

**Mitigation:**

- **Risk:** Active defragmentation does live page-copying work on the event loop. Aggressive settings (`active-defrag-cycle-max` near 75) can add millisecond-scale latency to commands. Start conservative.
- **Command:**

  ```bash
  redis-cli CONFIG SET activedefrag yes
  redis-cli CONFIG SET active-defrag-ignore-bytes 100mb
  redis-cli CONFIG SET active-defrag-threshold-lower 10
  redis-cli CONFIG SET active-defrag-threshold-upper 100
  redis-cli CONFIG SET active-defrag-cycle-min 5
  redis-cli CONFIG SET active-defrag-cycle-max 25
  ```

- **Duration:** Continuous, on by default. Tune `cycle-min`/`cycle-max` to balance defrag throughput against added latency.

**Resolution:**

```bash
# Make active defragmentation permanent
redis-cli CONFIG SET activedefrag yes
redis-cli CONFIG REWRITE

# If fragmentation ratio is extreme (>2.0), a controlled failover compacts memory.
# Procedure for a primary/replica pair:
#   1) Confirm replica is in sync (INFO replication: master_repl_offset == replica's offset)
#   2) Promote replica: REPLICAOF NO ONE on the replica
#   3) Restart the old primary; it rejoins as a replica and resyncs from scratch with a clean RSS
```

**Impact:** Instance-wide. Active defrag costs CPU and modest event-loop latency. Failover triggers a brief write outage during the role swap (typically <5 seconds with Sentinel/Cluster).

**Rollback:**

```bash
redis-cli CONFIG SET activedefrag no
redis-cli CONFIG REWRITE
```

**Verification:** Track `mem_fragmentation_ratio` from `INFO memory` every 5 minutes for an hour after enabling defrag; the value should trend toward `1.0`-`1.3` and stay there. `redis-cli MEMORY DOCTOR` should no longer report `high allocator fragmentation`.

### Cause F: Client output buffers consume memory (slow consumers or replica lag)

**Statement:** One or more clients (typically slow pub/sub subscribers or a lagging replica) have accumulated large output buffers, and that buffered memory is counted against `used_memory`, leaving less room for the dataset.

**Mechanism:** Redis writes to a per-client output buffer when a client cannot drain replies fast enough. Pub/sub subscribers receive every message published to subscribed channels; if the network or the subscriber is slow, messages queue in the buffer. Replicas receive a stream of write commands; if a replica is slow or disconnected, the master holds the replication backlog plus per-replica output buffers. Both are accounted in `used_memory`. When buffers grow into hundreds of MiB they push the instance to `maxmemory` even with a small logical dataset.

**Indicator:**

- [Step 6] `CLIENT LIST` shows at least one client with `omem` greater than 1048576 (1 MiB)
- [Step 5] `MEMORY STATS` shows `clients.normal` or `clients.slaves` or `pubsub.clients` is a large fraction of `used_memory`
- [Symptom] Redis log contains `Client ... scheduled to be closed ASAP for overcoming of output buffer limits`
<!-- match: {"step": 6, "predicate": "contains", "target": "omem="} -->

**Mitigation:**

- **Risk:** Killing a pub/sub subscriber drops in-flight messages — the subscriber must reconnect and re-subscribe; messages published while disconnected are lost (pub/sub is at-most-once). For replicas, disconnecting a replica triggers a full resync if the replication backlog has rolled over.
- **Command:**

  ```bash
  # Kill the worst-offending normal client by address (replace with output from Step 6)
  redis-cli CLIENT KILL ADDR <ip>:<port>
  # Or kill all pub/sub clients with output buffer overflow
  redis-cli CLIENT KILL TYPE pubsub
  ```

- **Duration:** Immediate. Clients reconnect via normal pool behavior.

**Resolution:**

```bash
# Bound output buffers permanently so a single slow consumer cannot starve the instance.
# Format: <hard-limit> <soft-limit> <soft-seconds> (0 = no limit)
redis-cli CONFIG SET client-output-buffer-limit "normal 0 0 0"
redis-cli CONFIG SET client-output-buffer-limit "replica 256mb 64mb 60"
redis-cli CONFIG SET client-output-buffer-limit "pubsub 32mb 8mb 60"
redis-cli CONFIG REWRITE
```

**Impact:** Instance-wide. Slow consumers above the limits are disconnected; well-behaved clients are unaffected. Tune `replica` carefully — too low forces full resyncs under load.

**Rollback:**

```bash
# Restore Redis defaults
redis-cli CONFIG SET client-output-buffer-limit "normal 0 0 0"
redis-cli CONFIG SET client-output-buffer-limit "replica 256mb 64mb 60"
redis-cli CONFIG SET client-output-buffer-limit "pubsub 32mb 8mb 60"
```

**Verification:** Re-run Step 6 after 5 minutes; no client should report `omem > 1048576`. `MEMORY STATS` should show `clients.normal + clients.slaves + pubsub.clients` well below 10% of `used_memory` under steady-state traffic.

### Cause G: BGSAVE / AOF rewrite copy-on-write pushes RSS over maxmemory

**Statement:** A child process forked for RDB snapshot or AOF rewrite triggers copy-on-write page duplication for every write the parent performs during the snapshot window, briefly inflating RSS toward 2× the dataset and crossing `maxmemory` even when steady-state usage is healthy.

**Mechanism:** `BGSAVE` and the AOF rewrite both `fork()` `redis-server`. Linux's copy-on-write means the parent and child initially share all pages; when the parent (still serving writes) modifies a page, the kernel copies it. Under write-heavy load most pages are touched during the snapshot, so the parent's RSS approaches the dataset size plus the dataset size — effectively doubling. If `maxmemory` was sized close to physical RAM, this transient peak hits the ceiling. Additionally, `vm.overcommit_memory=0` can refuse the fork outright, logged as `Can't save in background: fork: Cannot allocate memory`.

**Indicator:**

- [Step 7] `rdb_bgsave_in_progress:1` or `aof_rewrite_in_progress:1` at the time symptoms appeared
<!-- match: {"step": 7, "predicate": "contains", "target": "rdb_bgsave_in_progress:1"} -->
- [Step 7] `latest_fork_usec` greater than 250000 microseconds per GiB of dataset (slow fork amplifies COW window)
- [Symptom] Redis log contains `Can't save in background: fork: Cannot allocate memory` OR `Background saving terminated by signal 9`
<!-- match: {"step": 9, "predicate": "contains", "target": "vm.overcommit_memory") -->

**Mitigation:**

- **Risk:** Disabling persistence makes data non-durable; suitable only for pure cache deployments. Lowering write rate (rate-limit producers) is non-trivial application work.
- **Command:**

  ```bash
  # Suspend new BGSAVEs and AOF rewrites until the immediate pressure is gone
  redis-cli CONFIG SET save ""
  redis-cli CONFIG SET auto-aof-rewrite-percentage 0
  ```

- **Duration:** Minutes to hours, only as a holding action. Persistence must be re-enabled before the next planned restart or data is lost.

**Resolution:**

```bash
# 1) Enable overcommit so fork() does not fail allocation pre-check.
sudo sysctl -w vm.overcommit_memory=1
echo 'vm.overcommit_memory = 1' | sudo tee /etc/sysctl.d/99-redis.conf

# 2) Right-size maxmemory to leave at least 50% headroom for COW. Formula:
#    maxmemory = physical_RAM * 0.5  (for write-heavy workloads with persistence)
#    maxmemory = physical_RAM * 0.7  (for read-heavy or cache-only with brief persistence)
redis-cli CONFIG SET maxmemory <bytes-half-of-physical-ram>

# 3) Optionally offload RDB to a dedicated replica (set save "" on primary; let replica snapshot).
redis-cli CONFIG SET save ""
redis-cli CONFIG REWRITE
```

**Impact:** Host-wide for the `sysctl` change (persists across reboots after editing `/etc/sysctl.d`). Redis config changes are instance-scoped. Offloading snapshots to a replica leaves the primary without local recovery data — ensure the replica's AOF/RDB is shipped off-host.

**Rollback:**

```bash
sudo sysctl -w vm.overcommit_memory=0   # default Linux behavior
redis-cli CONFIG SET save "3600 1 300 100 60 10000"   # restore default RDB schedule
redis-cli CONFIG SET auto-aof-rewrite-percentage 100
redis-cli CONFIG REWRITE
```

**Verification:** Trigger a manual snapshot with `redis-cli BGSAVE`; watch `redis-cli INFO memory | grep used_memory_rss_human` during the rewrite. Peak RSS should stay below physical RAM. `redis-cli INFO persistence | grep rdb_last_bgsave_status` must report `ok`.

### Cause H: Replication backlog or full-resync buffer enlarged for a lagging replica

**Statement:** `repl-backlog-size` is large (or the primary is buffering a full-resync RDB for a slow replica) and that memory is accounted against `used_memory`, pushing the instance into OOM territory.

**Mechanism:** The replication backlog is a ring buffer of recent write commands kept on the primary so a briefly disconnected replica can resume via partial resync. Default is 1 MiB but is commonly raised to hundreds of MiB on busy clusters. When a replica disconnects long enough that its offset falls out of the backlog, the primary forks a snapshot, buffers it in memory while transmitting, and then replays the in-flight write stream from the per-replica output buffer. Under heavy writes plus a slow replica link, the per-replica buffer can grow to multi-GiB and counts against `used_memory`. `MEMORY STATS` reports this as `replication.backlog` and `clients.slaves`.

**Indicator:**

- [Step 5] `MEMORY STATS` field `replication.backlog` or `clients.slaves` is a large fraction of `used_memory`
- [Symptom] `INFO replication` shows at least one replica with `lag` greater than 10 seconds OR `state=send_bulk` (full resync in progress)
- [Step 6] `CLIENT LIST` shows a client with `flags` containing `S` (replica) and large `omem`

**Mitigation:**

- **Risk:** Resizing the backlog smaller forces affected replicas into full resync; full resync forks the primary again and amplifies the original problem during the resync window. Schedule for a quiet period.
- **Command:**

  ```bash
  # Temporarily reduce the per-replica buffer ceiling. Affected replicas will full-resync.
  redis-cli CONFIG SET client-output-buffer-limit "replica 128mb 32mb 60"
  ```

- **Duration:** Until the replication link is fixed (network bandwidth, replica-side disk I/O) or the replica is decommissioned.

**Resolution:**

```bash
# 1) Fix the slow link: replicas should be on the same low-latency network as the primary.
#    If the replica is in another region, use redis-cli --latency to measure RTT.
redis-cli --latency -h <replica-host> -p 6379

# 2) Right-size repl-backlog-size to ~60s of write traffic, not "as big as possible".
#    Estimate bytes/sec via:  redis-cli INFO stats | grep instantaneous_input_kbps
redis-cli CONFIG SET repl-backlog-size 256mb
redis-cli CONFIG REWRITE

# 3) Cap per-replica output buffer with non-zero hard limit
redis-cli CONFIG SET client-output-buffer-limit "replica 512mb 128mb 60"
redis-cli CONFIG REWRITE
```

**Impact:** Cluster-wide. Replicas that exceed the new buffer limit are disconnected and full-resync. Plan a maintenance window if multiple replicas are affected simultaneously.

**Rollback:**

```bash
redis-cli CONFIG SET repl-backlog-size 1mb
redis-cli CONFIG SET client-output-buffer-limit "replica 256mb 64mb 60"
redis-cli CONFIG REWRITE
```

**Verification:** After the link fix, `redis-cli INFO replication` should show each replica with `lag=0` (or low single digits) and `state=online`. `MEMORY STATS` field `replication.backlog` should drop to the configured `repl-backlog-size`; `clients.slaves` should drop to tens of MiB per replica.

### Cause Z: Unidentified

**Statement:** Symptoms confirm an OOM event in Redis (`OOM command not allowed` errors or `used_memory` at `maxmemory`), but Steps 1-9 do not match the indicators for Causes A-H.

**Mechanism:** Memory has crossed `maxmemory`, but the gathered evidence does not isolate which growth driver is responsible — diagnostics may be ambiguous (multiple suspect signals at moderate intensity), the workload may have a less common pattern (e.g., Lua script keeping a large local table, module-allocated memory not tracked by `MEMORY STATS`, or a Redis bug/regression), or `MEMORY DOCTOR` may report no specific advisory while `used_memory` remains pinned.

**Indicator:**

- [Default] OOM is confirmed via Step 1 (`used_memory` at `maxmemory`) or Step 2 (`evicted_keys` climbing) but no Cause A-H indicator matches the gathered evidence

**Mitigation:**

- **Risk:** A blind `FLUSHDB`/`FLUSHALL` destroys live data. Restarting reclaims fragmented memory but causes a brief outage and triggers a full resync on replicas. Use only after exporting `INFO ALL`, `MEMORY STATS`, `MEMORY DOCTOR`, `CLIENT LIST`, and `SLOWLOG GET 100` for post-incident analysis.
- **Command:**

  ```bash
  # Capture full diagnostic bundle before any destructive action
  redis-cli INFO ALL > /tmp/redis-info-$(date +%s).txt
  redis-cli MEMORY STATS >> /tmp/redis-info-$(date +%s).txt
  redis-cli MEMORY DOCTOR >> /tmp/redis-info-$(date +%s).txt
  redis-cli CLIENT LIST >> /tmp/redis-info-$(date +%s).txt
  redis-cli SLOWLOG GET 100 >> /tmp/redis-info-$(date +%s).txt
  ```

- **Duration:** Capture immediately; engage Redis owner with the bundle.

**Resolution:** Out of runbook scope. Hand the diagnostic bundle to the Redis owner or platform on-call, and reference upstream Redis support (`https://github.com/redis/redis/issues`) if the pattern resembles a known bug.

**Verification:** Hand-off acknowledged; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

## Prevention

- Always set `maxmemory` and `maxmemory-policy` explicitly in `redis.conf`; never run production with `maxmemory=0`. Size `maxmemory` at 50-70% of physical RAM to leave headroom for fork/COW, allocator fragmentation, OS page cache, and other host processes.
- Default `maxmemory-policy` to `allkeys-lru` for cache-only deployments or `volatile-lru` when the keyspace mixes durable data with TTL'd cache entries. Treat `noeviction` as opt-in for use cases that genuinely require write-failure-over-data-loss semantics.
- Enforce TTLs on every cache-style write in application code. Add a CI check that flags `SET`/`HSET`/`LPUSH`/`SADD` calls without an accompanying `EX`/`EXPIRE`.
- Enable `activedefrag yes` on jemalloc builds (the default). Combine with monitoring on `mem_fragmentation_ratio` to alert when defrag is not keeping up.
- Set `vm.overcommit_memory=1` on the host (`/etc/sysctl.d/99-redis.conf`) and disable transparent huge pages (`echo never > /sys/kernel/mm/transparent_hugepage/enabled`) to keep `fork()` cheap during BGSAVE and AOF rewrite.
- Cap `client-output-buffer-limit` for `replica` and `pubsub` clients with non-zero hard limits so a single slow consumer cannot exhaust the buffer budget.
- Alert when `used_memory / maxmemory > 0.85` for 5 minutes, when `evicted_keys` rate-of-change exceeds the baseline by 5x, or when `mem_fragmentation_ratio > 1.5`. Sample `MEMORY USAGE` on top-N keys weekly and alert when any key crosses 1 MiB.
- Periodically run `redis-cli --bigkeys` (off-peak; it scans the keyspace) and review against the data-model owners. Reject collections expected to exceed ~10,000 elements in code review.
- After any `CONFIG SET` change made during incident response, run `CONFIG REWRITE` so the change survives the next restart. Track config drift between running Redis and the source-controlled `redis.conf` in CI.

## Sources

- [Redis Memory Optimization](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/memory-optimization/) — Priority 1, official. `maxmemory` semantics, allocator behavior, peak-memory provisioning, fragmentation ratio formula, `noeviction` write-error guarantee.
- [Redis Key Eviction Policies](https://redis.io/docs/latest/develop/reference/eviction/) — Priority 1, official. Exhaustive list of `maxmemory-policy` values (`noeviction`, `allkeys-lru`, `allkeys-lfu`, `allkeys-random`, `volatile-lru`, `volatile-lfu`, `volatile-random`, `volatile-ttl`), trigger semantics, `evicted_keys` metric, `volatile-*` fallback-to-`noeviction` behavior when no TTL keys exist, `maxmemory-samples` tuning.
- [Redis Latency Diagnosis](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/latency/) — Priority 1, official. Fork/COW measurement, transparent-huge-page disablement, `/proc/<pid>/smaps` swap inspection, `latency-monitor-threshold`, AOF fsync impact, intrinsic-latency benchmarking.
- [Redis INFO Command Reference](https://redis.io/docs/latest/commands/info/) — Priority 1, official. Field definitions for `INFO memory` (`used_memory`, `used_memory_rss`, `used_memory_peak`, `used_memory_overhead`, `used_memory_dataset`, `maxmemory`, `maxmemory_policy`, `mem_fragmentation_ratio`, `mem_fragmentation_bytes`, `mem_allocator`, `evicted_keys`, `expired_keys`) and `INFO stats` (`keyspace_hits`, `keyspace_misses`, `rejected_connections`).
- [Redis Troubleshooting Guide](https://redis.io/docs/latest/operate/oss_and_stack/management/troubleshooting/) — Priority 1, official. RAM testing with `redis-server --test-memory`, debugging entrypoints, references to latency and crash sub-guides.
