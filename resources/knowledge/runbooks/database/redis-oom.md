---
id: redis-oom
title: "Redis Out of Memory (maxmemory exceeded)"
domain: database
service: redis
symptom_class:
  - oom
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

### Cause A: maxmemory unset or oversized for host RAM headroom

**Statement:** `maxmemory` is `0` (no limit) or sized too close to physical RAM, so memory growth plus fork/COW exceeds host headroom and trips the kernel OOM killer or pushes RSS into swap.

**Chain:**
- root: `maxmemory` is unset (`0`) or sized too close to physical RAM, leaving no headroom for fork/COW and allocator overhead.
- s1: resident memory grows unbounded with every write because no internal ceiling is enforced.
- s2: a BGSAVE/AOF fork plus copy-on-write briefly drives RSS toward physical RAM.
- s3: total RSS exceeds physical RAM; the kernel OOM-kills `redis-server` or the fork fails with `ENOMEM` and the parent keeps growing.
- D: the instance crashes or the host starves (points at Symptom Recognition: `dmesg` OOM-kill / fork-failure log).

**Indicators:**
- root: [Step 1] `maxmemory_human:0B`, or `maxmemory` greater than `(physical_RAM - other_processes) * 0.7`.
- s2: [Step 9] `/proc/<pid>/smaps` shows non-zero `Swap:` for the `redis-server` process.
- D: [Symptom] kernel `dmesg` contains `Out of memory: Killed process` referencing `redis-server`, OR Redis log contains `Can't save in background: fork: Cannot allocate memory`.

**Interventions:**
- **remediation** (root): size `maxmemory` to leave at least 30% of physical RAM for fork/COW, allocator overhead, and OS page cache.

  ```bash
  # Example for a 16 GiB host: cap Redis at ~10 GiB (60% of physical RAM).
  redis-cli CONFIG SET maxmemory 10gb
  redis-cli CONFIG SET maxmemory-policy allkeys-lru
  redis-cli CONFIG REWRITE
  ```

  **Verification:** after 5 minutes of representative traffic, `redis-cli INFO memory | grep used_memory_human` stabilizes at least 20% below `maxmemory_human`, and the `evicted_keys` delta is bounded.
- **mitigation** (root): set a derived `maxmemory` immediately from live `/proc/meminfo` to install a ceiling under traffic.

  ```bash
  redis-cli CONFIG SET maxmemory $(( $(awk '/MemTotal/{print $2}' /proc/meminfo) * 1024 * 60 / 100 ))
  redis-cli CONFIG SET maxmemory-policy allkeys-lru
  ```

  **Risk:** sizing too low under live traffic immediately triggers eviction or `OOM command not allowed` errors; clients must tolerate cache misses and write failures during the change. **Duration:** permanent once sized from observed peak; re-evaluate after any node-size change. **Verification:** `redis-cli INFO memory | grep maxmemory_human` reflects the new non-zero ceiling.

### Cause B: Eviction policy noeviction (or volatile-* with no TTL keys)

**Statement:** The active `maxmemory-policy` is `noeviction`, or a `volatile-*` policy with no TTL-bearing keys to evict, so reaching `maxmemory` rejects every write with `OOM command not allowed when used memory > 'maxmemory'`.

**Chain:**
- root: `maxmemory-policy` is `noeviction`, or a `volatile-*` policy while the keyspace holds no keys with TTLs.
- s1: `used_memory` reaches `maxmemory` and no key is eligible for reclamation.
- s2: every allocating write is rejected immediately while reads (no allocation) keep succeeding.
- D: clients see `OOM command not allowed` on writes (points at Symptom Recognition).

**Indicators:**
- root: [Step 1] `maxmemory_policy:noeviction`.
- s1: [Step 2] `evicted_keys` is exactly `0` and not increasing despite `used_memory` at the ceiling.
- D: [Symptom] client error logs contain `OOM command not allowed when used memory > 'maxmemory'`.

**Interventions:**
- **remediation** (root): set an eviction policy that matches the workload so the ceiling self-regulates.

  ```bash
  # Cache workload: evict any key by recency
  redis-cli CONFIG SET maxmemory-policy allkeys-lru
  # Or, for cache workload with hot-set skew, frequency-based
  redis-cli CONFIG SET maxmemory-policy allkeys-lfu
  # Or, mixed cache + durable data with TTLs set on cache entries only
  redis-cli CONFIG SET maxmemory-policy volatile-lru
  redis-cli CONFIG REWRITE
  ```

  **Verification:** write a probe key (`redis-cli SET fm_oom_probe ok EX 60`) and observe `OK`; after 5 minutes `redis-cli INFO stats | grep evicted_keys` shows a non-zero, bounded delta.
- **mitigation** (root): switch to `allkeys-lru` immediately to start reclaiming under pressure.

  ```bash
  redis-cli CONFIG SET maxmemory-policy allkeys-lru
  ```

  **Risk:** `allkeys-lru` immediately evicts application data, including keys assumed durable; use `volatile-lru` if any keys must never be evicted and the rest reliably carry TTLs. **Duration:** permanent for cache workloads; for mixed deployments switch to `volatile-lru` and audit TTL coverage with Step 8. **Verification:** a probe write returns `OK` and `evicted_keys` begins to climb.

### Cause C: Unbounded key growth from missing TTLs

**Statement:** The application stores cache-style data with no `EX`/`PX`/`EXPIRE`, so the keyspace grows monotonically and eventually fills `maxmemory` regardless of eviction policy.

**Chain:**
- root: cache-style writes (`SET`/`HSET`/`LPUSH`/`SADD`) omit `EX`/`PX`/`EXPIRE`, so keys are created with `TTL = -1`.
- s1: no-TTL keys are never eligible for expiry and (under `volatile-*`) never eligible for eviction either.
- s2: the keyspace grows monotonically until `used_memory` pins at `maxmemory`, holding constant eviction pressure.
- D: any burst that outruns eviction returns `OOM command not allowed` (points at Symptom Recognition).

**Indicators:**
- root: [Symptom] code review shows `SET`/`HSET`/`LPUSH` calls without `EX`/`EXPIRE` for entries intended as cache.
- s1: [Step 8] sampled TTL distribution shows more than 50% of randomly sampled keys returning `TTL = -1`.
- s2: [Step 2] `evicted_keys` is high and growing alongside `keyspace_misses`, indicating eviction is removing keys applications still want.

**Interventions:**
- **remediation** (root): declare a TTL on every cache-style write in application code, with a server-side safety net.

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

  **Verification:** after deploying the fix, re-run Step 8 hourly for 24 hours; the no-TTL fraction trends toward 0 and `used_memory_human` stabilizes below `maxmemory_human`.
- **mitigation** (s1): assign a TTL to existing no-TTL keys by scanning the keyspace.

  ```bash
  # Assign a 1-hour TTL to every key that currently has none. Paces 100 keys per pipeline.
  redis-cli --scan | while read -r k; do
    [ "$(redis-cli TTL "$k")" = "-1" ] && redis-cli EXPIRE "$k" 3600
  done
  ```

  **Risk:** bulk `EXPIRE` over `SCAN` against a hot keyspace adds command load — pace it. **Duration:** stopgap until application code is fixed; without the code fix, newly written keys re-enter without TTLs. **Verification:** re-run Step 8; the no-TTL fraction drops immediately after the sweep.

### Cause D: Large keys (big keys) consuming disproportionate memory

**Statement:** A small number of individual keys — large strings, oversized hashes, lists, sets, or sorted sets — account for a large fraction of `used_memory`, so deleting or restructuring them frees substantial headroom.

**Chain:**
- root: one or a few keys carry an outsized payload (large string, or a collection above the listpack thresholds, or a multi-million-entry list).
- s1: those keys occupy a large fraction of `used_memory` (collections above thresholds also pay doubled per-element overhead after promotion to hashtable/skiplist).
- s2: total `used_memory` reaches `maxmemory` driven mostly by this small set of keys.
- D: writes are rejected or eviction churns even though the rest of the keyspace is healthy (points at Symptom Recognition).

**Indicators:**
- root: [Step 4] `--bigkeys` reports at least one key whose size dominates the type's total.
- s1: [Step 5] `MEMORY USAGE <key> SAMPLES 0` returns a value greater than 1048576 (1 MiB) for individual keys.
- s2: [Step 3] `MEMORY DOCTOR` advisory notes a high ratio between `used_memory_peak` and current `used_memory`, consistent with a small set of large keys.

**Interventions:**
- **remediation** (root): cap collection growth at the application level so keys cannot grow without bound.

  ```bash
  # Cap collections at the application level so they cannot grow without bound:
  # Lists: trim after each push
  redis-cli LPUSH events:user:123 "$payload"
  redis-cli LTRIM events:user:123 0 999          # keep most-recent 1000 entries

  # Hashes/sets/zsets: shard by hashing the logical id into buckets
  # e.g. instead of one 10M-field hash 'users', use 256 hashes 'users:{0..255}' keyed by id mod 256

  # Strings: store large blobs in object storage (S3/GCS); keep only the URL or hash in Redis
  ```

  **Verification:** after the fix deploys, `redis-cli --bigkeys` no longer reports any single key over 1 MiB (or the team's agreed threshold), and `used_memory_human` drops by the prior big-key footprint.
- **mitigation** (s1): free the offending key(s) non-blockingly with `UNLINK`.

  ```bash
  # Non-blocking deletion of a known large key
  redis-cli UNLINK <large-key-name>
  # Or for a pattern of disposable scratch keys
  redis-cli --scan --pattern 'scratch:*' | xargs -L 100 redis-cli UNLINK
  ```

  **Risk:** freed memory is reclaimed asynchronously, so `used_memory` may not drop for seconds on multi-GiB keys; never use blocking `DEL` on a hot instance. **Duration:** immediate relief; without a data-model fix the same keys will be re-created. **Verification:** `redis-cli INFO memory | grep used_memory_human` drops by the reported big-key footprint within 30 seconds of `UNLINK` completion.

### Cause E: Memory fragmentation inflates RSS far above logical usage

**Statement:** Allocator fragmentation has driven RSS well above logical `used_memory`, so the OS sees the process at or above `maxmemory` while Redis reports headroom, causing eviction or `OOM command not allowed` even at moderate dataset size.

**Chain:**
- root: long-lived churn (many writes plus many deletes/expirations) leaves the jemalloc allocator holding pages it cannot return to the OS.
- s1: `mem_fragmentation_ratio = used_memory_rss / used_memory` climbs above 1.5, so RSS far exceeds logical `used_memory`.
- s2: the OS-visible RSS approaches the ceiling and starves fork-time COW and other host processes, even though `used_memory` shows headroom.
- D: eviction churns or writes are rejected at moderate logical dataset size (points at Symptom Recognition).

**Indicators:**
- root: [Step 1] `mem_allocator:jemalloc-*` (active defrag is jemalloc-only).
- s1: [Step 1] `mem_fragmentation_ratio` is greater than `1.5`.
- s1: [Step 3] `MEMORY DOCTOR` advisory contains the substring `high allocator fragmentation`.

**Interventions:**
- **remediation** (root): make active defragmentation permanent so the allocator compacts pages continuously.

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

  **Verification:** track `mem_fragmentation_ratio` every 5 minutes for an hour; it trends to `1.0`-`1.3` and stays there, and `MEMORY DOCTOR` no longer reports `high allocator fragmentation`.
- **mitigation** (s1): enable active defrag at runtime with conservative cycle bounds.

  ```bash
  redis-cli CONFIG SET activedefrag yes
  redis-cli CONFIG SET active-defrag-ignore-bytes 100mb
  redis-cli CONFIG SET active-defrag-threshold-lower 10
  redis-cli CONFIG SET active-defrag-threshold-upper 100
  redis-cli CONFIG SET active-defrag-cycle-min 5
  redis-cli CONFIG SET active-defrag-cycle-max 25
  ```

  **Risk:** active defrag does live page-copying on the event loop; aggressive settings (`active-defrag-cycle-max` near 75) add millisecond-scale latency — start conservative. **Duration:** continuous; tune `cycle-min`/`cycle-max` to balance throughput against latency. **Verification:** `mem_fragmentation_ratio` begins trending toward `1.0` over the next hour.

### Cause F: Client output buffers consume memory (slow consumers or replica lag)

**Statement:** One or more clients (typically slow pub/sub subscribers or a lagging replica) have accumulated large output buffers, and that buffered memory counts against `used_memory`, leaving less room for the dataset.

**Chain:**
- root: a client cannot drain replies fast enough (slow pub/sub subscriber, or a lagging/disconnected replica).
- s1: Redis queues unsent replies in that client's per-client output buffer, which grows to hundreds of MiB.
- s2: buffered memory is accounted in `used_memory`, so the instance reaches `maxmemory` even with a small logical dataset.
- D: writes are rejected or eviction churns despite a small dataset (points at Symptom Recognition).

**Indicators:**
- root: [Symptom] Redis log contains `Client ... scheduled to be closed ASAP for overcoming of output buffer limits`.
- s1: [Step 6] `CLIENT LIST` shows at least one client with `omem` greater than 1048576 (1 MiB).
- s2: [Step 5] `MEMORY STATS` shows `clients.normal` or `clients.slaves` or `pubsub.clients` is a large fraction of `used_memory`.

**Interventions:**
- **defensive_fix** (s1): bound output buffers permanently so a single slow consumer cannot starve the instance.

  ```bash
  # Bound output buffers permanently so a single slow consumer cannot starve the instance.
  # Format: <hard-limit> <soft-limit> <soft-seconds> (0 = no limit)
  redis-cli CONFIG SET client-output-buffer-limit "normal 0 0 0"
  redis-cli CONFIG SET client-output-buffer-limit "replica 256mb 64mb 60"
  redis-cli CONFIG SET client-output-buffer-limit "pubsub 32mb 8mb 60"
  redis-cli CONFIG REWRITE
  ```

  **Verification:** re-run Step 6 after 5 minutes; no client reports `omem > 1048576`, and `clients.normal + clients.slaves + pubsub.clients` stays well below 10% of `used_memory`.
- **mitigation** (s1): kill the worst offender(s) to release buffered memory now.

  ```bash
  # Kill the worst-offending normal client by address (replace with output from Step 6)
  redis-cli CLIENT KILL ADDR <ip>:<port>
  # Or kill all pub/sub clients with output buffer overflow
  redis-cli CLIENT KILL TYPE pubsub
  ```

  **Risk:** killing a pub/sub subscriber drops in-flight messages (at-most-once); disconnecting a replica triggers a full resync if the backlog rolled over. **Duration:** immediate; clients reconnect via normal pool behavior. **Verification:** re-run Step 6; the offending client's `omem` is gone and `used_memory` drops.

### Cause G: BGSAVE / AOF rewrite copy-on-write pushes RSS over maxmemory

**Statement:** A child forked for RDB snapshot or AOF rewrite triggers copy-on-write page duplication for every write the parent performs during the snapshot, briefly inflating RSS toward 2× the dataset and crossing `maxmemory` even when steady-state usage is healthy.

**Chain:**
- root: a `BGSAVE` or AOF rewrite forks `redis-server` during write-heavy load (or `vm.overcommit_memory=0` refuses the fork outright).
- s1: copy-on-write duplicates each page the still-serving parent modifies during the snapshot window.
- s2: the parent's RSS approaches dataset-plus-dataset (effectively 2×), crossing a `maxmemory` sized close to physical RAM.
- D: the transient peak crosses the ceiling — OOM, or the fork is refused with `fork: Cannot allocate memory` (points at Symptom Recognition).

**Indicators:**
- root: [Step 7] `rdb_bgsave_in_progress:1` or `aof_rewrite_in_progress:1` at the time symptoms appeared.
- s1: [Step 7] `latest_fork_usec` greater than 250000 microseconds per GiB of dataset (slow fork amplifies the COW window).
- D: [Symptom] Redis log contains `Can't save in background: fork: Cannot allocate memory` OR `Background saving terminated by signal 9`.

**Interventions:**
- **remediation** (root): enable overcommit and right-size `maxmemory` so fork/COW has headroom.

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

  **Verification:** trigger `redis-cli BGSAVE`; `used_memory_rss_human` peak stays below physical RAM during the rewrite and `rdb_last_bgsave_status` reports `ok`.
- **mitigation** (root): suspend snapshots and AOF rewrites to remove the COW spike while pressure clears.

  ```bash
  # Suspend new BGSAVEs and AOF rewrites until the immediate pressure is gone
  redis-cli CONFIG SET save ""
  redis-cli CONFIG SET auto-aof-rewrite-percentage 0
  ```

  **Risk:** suspending persistence makes data non-durable; suitable only as a holding action (pure cache, or briefly). **Duration:** minutes to hours; persistence must be re-enabled before the next planned restart or data is lost. **Verification:** Step 7 shows `rdb_bgsave_in_progress:0` and `aof_rewrite_in_progress:0`, and RSS recedes.

### Cause H: Replication backlog or full-resync buffer enlarged for a lagging replica

**Statement:** `repl-backlog-size` is large (or the primary is buffering a full-resync RDB plus per-replica output for a slow replica) and that memory counts against `used_memory`, pushing the instance into OOM territory.

**Chain:**
- root: a replica lags or disconnects long enough that its offset falls out of the replication backlog.
- s1: the primary forks a snapshot and buffers it in memory while replaying the in-flight write stream from the per-replica output buffer.
- s2: under heavy writes plus a slow link, that per-replica buffer (plus an oversized backlog) grows to multi-GiB, all counted in `used_memory`.
- D: `used_memory` reaches `maxmemory` and writes are rejected (points at Symptom Recognition).

**Indicators:**
- root: [Symptom] `INFO replication` shows at least one replica with `lag` greater than 10 seconds OR `state=send_bulk` (full resync in progress).
- s2: [Step 5] `MEMORY STATS` field `replication.backlog` or `clients.slaves` is a large fraction of `used_memory`.
- s2: [Step 6] `CLIENT LIST` shows a client with `flags` containing `S` (replica) and large `omem`.

**Interventions:**
- **remediation** (root): fix the slow link and right-size the backlog and per-replica buffer.

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

  **Verification:** after the link fix, `INFO replication` shows each replica `lag=0` (or low single digits) and `state=online`; `replication.backlog` drops to the configured size and `clients.slaves` to tens of MiB per replica.
- **mitigation** (s2): reduce the per-replica buffer ceiling to reclaim memory now.

  ```bash
  # Temporarily reduce the per-replica buffer ceiling. Affected replicas will full-resync.
  redis-cli CONFIG SET client-output-buffer-limit "replica 128mb 32mb 60"
  ```

  **Risk:** resizing the buffer smaller forces affected replicas into full resync, which forks the primary again and amplifies the original problem during the resync window — schedule for a quiet period. **Duration:** until the replication link is fixed (network bandwidth, replica-side disk I/O) or the replica is decommissioned. **Verification:** `MEMORY STATS` `clients.slaves` drops immediately as the buffer is bounded.

### Cause Z: Unidentified

**Statement:** Symptoms confirm an OOM event in Redis (`OOM command not allowed` errors or `used_memory` at `maxmemory`), but Steps 1-9 do not match the indicators for Causes A-H.

**Chain:**
- root: memory has crossed `maxmemory` from a growth driver the gathered evidence does not isolate (ambiguous moderate signals, an uncommon pattern such as a large Lua local table or module-allocated memory untracked by `MEMORY STATS`, or a Redis bug/regression).
- D: OOM is confirmed but unattributed to Causes A-H (points at Symptom Recognition).

**Indicators:**
- root: [Default] OOM is confirmed via Step 1 (`used_memory` at `maxmemory`) or Step 2 (`evicted_keys` climbing) but no Cause A-H indicator matches the gathered evidence.

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot, then hand off to the Redis owner / platform on-call.

  ```bash
  # Capture full diagnostic bundle before any destructive action
  redis-cli INFO ALL > /tmp/redis-info-$(date +%s).txt
  redis-cli MEMORY STATS >> /tmp/redis-info-$(date +%s).txt
  redis-cli MEMORY DOCTOR >> /tmp/redis-info-$(date +%s).txt
  redis-cli CLIENT LIST >> /tmp/redis-info-$(date +%s).txt
  redis-cli SLOWLOG GET 100 >> /tmp/redis-info-$(date +%s).txt
  ```

  **Risk:** a blind `FLUSHDB`/`FLUSHALL` destroys live data and a restart causes a brief outage plus replica full resync — capture the bundle before any destructive action; reference upstream Redis support (`https://github.com/redis/redis/issues`) if the pattern resembles a known bug. **Duration:** capture immediately; engage the Redis owner with the bundle. **Verification:** hand-off acknowledged; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

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
</content>
</invoke>
