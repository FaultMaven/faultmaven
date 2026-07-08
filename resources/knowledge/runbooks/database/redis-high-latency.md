---
id: "redis-high-latency"
title: "Redis High Latency"
domain: database
service: redis
symptom_class: [latency]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags: [slowlog, latency-monitor, persistence, transparent-huge-pages, swap, big-keys, aof, rdb]
difficulty: intermediate
---

## Symptom Recognition

- Application p95/p99 latency spikes on endpoints backed by Redis (caching, session, rate limiting, pub/sub).
- Client-side timeout errors such as `ETIMEDOUT`, `redis.exceptions.TimeoutError`, or `connection reset by peer`.
- `SLOWLOG GET` entries with durations above 10,000 µs (10 ms).
- `LATENCY LATEST` reporting `command` or `fork` events above 100 ms.
- `redis-cli --latency` showing average round-trip above 1 ms on a local or same-AZ instance.
- System monitoring showing Redis process swap pages (`Swap:` rows in `/proc/<pid>/smaps` non-zero).
- Latency spikes correlating with scheduled RDB snapshots or AOF rewrites.

## Applicability

- Redis 6.0 and later (all commands used are available in Redis 5.0+; `LATENCY` subsystem requires Redis 2.8.13+).
- Requires `redis-cli` with admin privileges: `SLOWLOG`, `LATENCY`, `INFO`, `CONFIG GET/SET`, `MEMORY USAGE`, `DEBUG`.
- ACL restrictions (Redis 6+) may block some commands; confirm with `ACL WHOAMI` and `ACL LIST`.
- `--intrinsic-latency` must be run on the Redis server host, not remotely.
- Root-level OS access needed for `/proc/<pid>/smaps`, `vmstat`, and `sysctl` commands.

## Diagnostic Steps

### Step 1: Capture recent slow commands from SLOWLOG

```bash
redis-cli SLOWLOG GET 25
```

Each entry returns: `[id, timestamp, duration_microseconds, [command args], client-addr, client-name]`. Duration above 10,000 µs (10 ms) is problematic. Note command names for pattern analysis (`KEYS`, `SORT`, `SMEMBERS`, `HGETALL`, `LRANGE 0 -1`).

Check the current threshold and lower it for deeper investigation:

```bash
redis-cli CONFIG GET slowlog-log-slower-than
redis-cli CONFIG SET slowlog-log-slower-than 1000
```

Expected output: `slowlog-log-slower-than` currently `10000`. After the `SET`, re-run `SLOWLOG GET 25` to capture sub-10ms commands.

### Step 2: Run LATENCY DOCTOR and check event history

```bash
redis-cli LATENCY LATEST
redis-cli LATENCY HISTORY fork
redis-cli LATENCY HISTORY command
redis-cli LATENCY DOCTOR
```

`LATENCY LATEST` returns rows of `[event, timestamp, last_ms, max_ms]`. A `fork` event above 200 ms signals persistence overhead. A `command` event above 100 ms signals slow command execution. `LATENCY DOCTOR` produces a human-readable paragraph diagnosing the dominant cause.

Enable the monitor if not already active:

```bash
redis-cli CONFIG SET latency-monitor-threshold 100
```

### Step 3: Check for swap usage on the Redis process

```bash
REDIS_PID=$(pgrep -f redis-server)
awk '/^Swap:/ {sum+=$2} END {print sum " kB"}' /proc/${REDIS_PID}/smaps
```

Expected output: `0 kB`. Any non-zero value means Redis pages have been evicted to swap, causing order-of-magnitude latency spikes.

Also check system-level swap activity:

```bash
vmstat 1 5
```

Expected output: `si` and `so` columns both `0` across all rows.

### Step 4: Identify large keys with --bigkeys and MEMORY USAGE

```bash
redis-cli --bigkeys
```

Reports the largest key per data type. Strings above 10 KB or collections above 1,000 elements cause elevated serialization, transfer, and deletion latency. For a targeted check on a suspect key:

```bash
redis-cli MEMORY USAGE <key_name>
```

Expected output: size in bytes. Values above 10,240 (10 KB) are candidates for optimization.

### Step 5: Inspect persistence configuration and last fork duration

```bash
redis-cli INFO persistence
```

Key fields to check:

- `rdb_last_bgsave_time_sec` — last RDB snapshot wall-clock duration. Above 5 s is a latency risk.
- `aof_last_rewrite_time_sec` — last AOF rewrite duration. Above 5 s is a latency risk.
- `aof_rewrite_in_progress` — `1` means a rewrite is active right now.
- `rdb_last_bgsave_status` — should be `ok`; any other value indicates a failed save.

Check fork time from INFO stats:

```bash
redis-cli INFO stats | grep latest_fork_usec
```

Expected output: `latest_fork_usec:N`. Values above 1,000,000 µs (1 s) for large datasets indicate platform-level fork overhead.

### Step 6: Inspect memory fragmentation and maxmemory headroom

```bash
redis-cli INFO memory
```

Key fields:

- `mem_fragmentation_ratio` — RSS ÷ used_memory. Above 1.5 = significant fragmentation. Below 1.0 = swap in use (critical).
- `used_memory_human` vs `maxmemory_human` — if used_memory is within 10% of maxmemory, active eviction is running.
- `maxmemory_policy` — eviction policy. `noeviction` causes write errors when limit is hit; `allkeys-lru` silently drops keys.

### Step 7: Measure network round-trip and intrinsic system latency

```bash
redis-cli --latency -h <redis-host> -p 6379
```

Expected output: `min: X, max: Y, avg: Z, samples: N`. Average above 1 ms on same-AZ TCP indicates network overhead.

On the Redis server host only:

```bash
redis-cli --intrinsic-latency 30
```

Expected output: max latency below 1,000 µs on bare metal, below 10,000 µs on VMs. If `--latency` is much higher than `--intrinsic-latency`, the network is the bottleneck, not Redis itself.

## Causes

### Cause A: Blocking O(N) command against large keyspace

**Statement:** A command with O(N) complexity (`KEYS`, `SMEMBERS`, `HGETALL`, `SORT`, `LRANGE 0 -1`) ran against a large dataset and blocked Redis's single-threaded event loop for its full duration.

**Chain:**
- root: an O(N) command (`KEYS`, `SMEMBERS`, `HGETALL`, `SORT`, `LRANGE 0 -1`) executes against a keyspace or collection with thousands of entries.
- s1: the single-threaded event loop is held for the full scan, queuing every other client request behind it.
- D: clients backed by Redis see p95/p99 latency spikes and timeout errors (Symptom Recognition).

**Indicators:**
- root: [Step 1] `SLOWLOG GET` shows `KEYS`, `SORT`, `SMEMBERS`, `HGETALL`, or `LRANGE 0 -1` with duration above 10,000 µs.
- s1: [Step 2] `LATENCY HISTORY command` shows spikes correlated with the timestamps of those SLOWLOG entries.

**Interventions:**
- **remediation** (root): replace O(N) commands with cursor-based or paginated equivalents in application code.

  ```bash
  # Replace KEYS with cursor-based SCAN
  redis-cli SCAN 0 MATCH "pattern:*" COUNT 100

  # Replace SMEMBERS with SSCAN
  redis-cli SSCAN myset 0 COUNT 100

  # Replace HGETALL with HSCAN
  redis-cli HSCAN myhash 0 COUNT 100

  # Replace LRANGE 0 -1 with paginated reads
  redis-cli LRANGE mylist 0 99

  # Use UNLINK instead of DEL for large keys (async, Redis 4.0+)
  redis-cli UNLINK large_key
  ```

  **Verification:** After code change deploys, `SLOWLOG GET 25` returns no entries with O(N) command names above 1,000 µs; `LATENCY LATEST` shows `command` event max below 10 ms.
- **mitigation** (root): block dangerous O(N) commands at the access-control layer to stop the bleeding before code ships.

  ```bash
  redis-cli ACL SETUSER app-user -@dangerous
  ```

  **Risk:** ACL takes effect immediately for existing connections; `rename-command` requires a restart. **Duration:** immediate for ACL; permanent after restart for `rename-command`. **Verification:** re-run Step 1; the blocked O(N) commands no longer appear in SLOWLOG.

---

### Cause B: Fork latency from RDB snapshot or AOF rewrite

**Statement:** A `BGSAVE` or `BGREWRITEAOF` forked the Redis process, and OS copy-on-write page-table duplication caused a latency spike lasting hundreds of milliseconds to several seconds.

**Chain:**
- root: a `BGSAVE` or `BGREWRITEAOF` triggers `fork()`, duplicating a page table proportional to dataset size (2–10 ms/GB bare metal, 200–400 ms/GB on Xen VMs).
- s1: while the child writes the snapshot, every parent write triggers a copy-on-write page fault, adding CPU and memory-bus pressure.
- s2: Transparent Huge Pages amplify this — a 2 MB huge page is fully copied on any byte change, up to 512× the cost of 4 KB pages.
- D: latency spikes lasting hundreds of ms to seconds appear on a schedule matching persistence triggers (Symptom Recognition).

**Indicators:**
- root: [Step 5] `rdb_last_bgsave_time_sec` above 5 s or `aof_last_rewrite_time_sec` above 5 s.
- s1: [Step 2] `LATENCY HISTORY fork` shows values above 200 ms.
- D: [Symptom] latency spikes occur on a predictable schedule matching RDB `save` trigger thresholds.

**Interventions:**
- **remediation** (s2): disable Transparent Huge Pages and tune AOF/jemalloc to cut copy-on-write fork cost.

  ```bash
  # Disable Transparent Huge Pages on the server (persist across reboots via rc.local or systemd)
  echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled

  # Tune AOF to avoid per-write fsync
  redis-cli CONFIG SET appendfsync everysec
  redis-cli CONFIG SET no-appendfsync-on-rewrite yes

  # Enable jemalloc background threads to accelerate fork (Redis 6+)
  redis-cli CONFIG SET jemalloc-bg-thread yes

  # Space out RDB saves to reduce fork frequency
  redis-cli CONFIG SET save "3600 1 300 1000"
  ```

  **Verification:** After THP disabled and AOF tuned, `LATENCY HISTORY fork` shows no events above 200 ms across two full RDB/AOF cycles; `latest_fork_usec` drops 50%+ from baseline. Rollback: `echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled`.
- **mitigation** (root): temporarily disable persistence to eliminate fork while investigating.

  ```bash
  redis-cli CONFIG SET save ""
  redis-cli CONFIG SET appendonly no
  ```

  **Risk:** disabling persistence creates a data-loss window; only use while actively investigating. **Duration:** until root cause is confirmed and persistence is re-enabled with tuned settings. **Verification:** re-run Step 2; no `fork` events appear in `LATENCY LATEST` while persistence is off.

---

### Cause C: Redis process swapping to disk

**Statement:** The Redis process has been partially or fully swapped to disk, so every memory access hitting a swapped page incurs milliseconds of disk I/O instead of nanoseconds of RAM access.

**Chain:**
- root: physical RAM runs low (often because `maxmemory` is unset and the dataset grew unconstrained), so the kernel swaps Redis pages to disk.
- s1: because Redis is single-threaded, one swapped page during command execution blocks the whole event loop until the page fault resolves via disk I/O.
- D: even a few KB of swap produces 50–500 ms latency spikes visible to clients (Symptom Recognition).

**Indicators:**
- root: [Step 3] `awk '/^Swap:/ {sum+=$2}' /proc/<pid>/smaps` returns a non-zero value.
- s1: [Step 3] `vmstat 1` shows non-zero `si` (swap-in) or `so` (swap-out) columns.
- s1: [Step 6] `mem_fragmentation_ratio` below 1.0.

**Interventions:**
- **remediation** (root): set `maxmemory` with headroom and an eviction policy so the dataset never exceeds physical RAM.

  ```bash
  # Set maxmemory to leave 20-30% of physical RAM for OS and fork operations
  redis-cli CONFIG SET maxmemory 6gb
  redis-cli CONFIG SET maxmemory-policy allkeys-lru

  # Persist to redis.conf
  echo "maxmemory 6gb" | sudo tee -a /etc/redis/redis.conf
  echo "maxmemory-policy allkeys-lru" | sudo tee -a /etc/redis/redis.conf
  ```

  **Verification:** `awk '/^Swap:/ {sum+=$2}' /proc/$(pgrep -f redis-server)/smaps` returns `0 kB`; `vmstat 1 5` shows `si`/`so` at `0`; `redis-cli --latency` average below 1 ms. Rollback: `redis-cli CONFIG SET maxmemory 0`.
- **mitigation** (s1): reduce swappiness and (only with confirmed free RAM) swap pages back to memory.

  ```bash
  # Reduce system swappiness to discourage swap use
  sudo sysctl vm.swappiness=1

  # Check available RAM before disabling swap
  free -h

  # Disable swap only if sufficient free RAM exists
  sudo swapoff -a
  ```

  **Risk:** `swapoff -a` needs enough free RAM to absorb all swapped pages, else the kernel OOM-kills Redis. **Duration:** `sysctl` change is immediate but non-persistent; swap-off may take 1–5 minutes. **Verification:** re-run Step 3; smaps swap sum returns to `0 kB`.

---

### Cause D: High memory fragmentation

**Statement:** Redis's allocator has fragmented physical memory so that RSS is much larger than logical used_memory, degrading allocator performance and increasing memory pressure.

**Chain:**
- root: workloads with highly variable key sizes or frequent delete/re-insert prevent jemalloc from reusing freed blocks efficiently.
- s1: the fragmentation ratio (RSS ÷ used_memory) climbs above 1.5, so Redis consumes 50%+ more RAM than the data requires.
- s2: wasted RAM raises swap-pressure likelihood and slows allocation paths as the allocator searches for suitable free blocks.
- D: allocation latency and swap risk surface as client-visible latency spikes (Symptom Recognition).

**Indicators:**
- s1: [Step 6] `mem_fragmentation_ratio` above 1.5.
- s1: [Step 6] `used_memory_rss_human` significantly larger than `used_memory_human`.

**Interventions:**
- **remediation** (root): enable active defragmentation so the allocator compacts freed blocks online.

  ```bash
  redis-cli CONFIG SET activedefrag yes
  redis-cli CONFIG SET active-defrag-threshold-lower 10
  redis-cli CONFIG SET active-defrag-cycle-min 1
  redis-cli CONFIG SET active-defrag-cycle-max 25
  ```

  **Verification:** Over 30–60 minutes, `redis-cli INFO memory | grep mem_fragmentation_ratio` trends toward 1.0–1.3 and `used_memory_rss_human` converges toward `used_memory_human`.
- **mitigation** (s1): run the same active-defrag settings as an immediate online interception while the durable config is persisted.

  ```bash
  redis-cli CONFIG SET activedefrag yes
  redis-cli CONFIG SET active-defrag-cycle-max 25
  ```

  **Risk:** active defragmentation consumes CPU cycles; tune cycle percentages to avoid impacting foreground latency. **Duration:** runs continuously in the background; ratio improves over minutes to hours. **Verification:** re-run Step 6; `mem_fragmentation_ratio` trends downward.

---

### Cause E: Large key causing serialization and transfer overhead

**Statement:** One or more Redis keys hold values too large (strings above 10 KB or collections above 1,000 elements) for efficient single-command serialization, network transfer, and client deserialization.

**Chain:**
- root: a key holds an oversized value — a string above 10 KB or a collection above 1,000 elements.
- s1: reading/writing it forces Redis to serialize the whole value, fill the output buffer, and await client acknowledgement; large-collection ops (`HGETALL`, `SMEMBERS`) block the loop for the full enumeration, and `DEL` blocks while freeing memory.
- D: each operation on the key adds ~1 ms serialization + ~1 ms transfer per MB, surfacing as client latency spikes (Symptom Recognition).

**Indicators:**
- root: [Step 4] `redis-cli --bigkeys` reports a key above 10,240 bytes (strings) or above 1,000 elements (collections).
- root: [Step 4] `redis-cli MEMORY USAGE <key>` returns a value above 10,240.
- s1: [Step 1] SLOWLOG shows `GET`, `HGETALL`, `SMEMBERS`, or `DEL` on the same key name with high duration.

**Interventions:**
- **remediation** (root): refactor oversized values via compression, bucketed sub-hashes, and paginated access.

  ```bash
  # Refactor string keys: compress values before storing
  # Application-side: use msgpack/zstd compression

  # Refactor large hashes into bucketed sub-hashes
  # Instead of: HSET user:1:data field1 val ... field100000 val
  # Use: HSET user:1:data:0 field1 val ... field999 val
  #      HSET user:1:data:1 field1000 val ... field1999 val

  # Refactor large sets/lists: use paginated access patterns
  redis-cli SSCAN myset 0 COUNT 100
  redis-cli LRANGE mylist 0 99
  ```

  **Verification:** `redis-cli --bigkeys` no longer reports keys above 10 KB or collections above 1,000 elements; `SLOWLOG GET 25` shows no GET/HGETALL/SMEMBERS above 10,000 µs.
- **mitigation** (s1): delete the offending large key asynchronously to avoid a blocking `DEL`.

  ```bash
  # Delete a large key asynchronously (non-blocking, Redis 4.0+)
  redis-cli UNLINK <large_key_name>
  ```

  **Risk:** splitting keys requires application changes; interim state may cause partial reads if not handled atomically. **Duration:** `UNLINK` returns immediately; background deletion completes within seconds. **Verification:** re-run Step 4; the large key no longer appears in `--bigkeys`.

---

### Cause F: Mass key expiration blocking the event loop

**Statement:** A large number of keys share the same or near-identical TTL, so the active expiration cycle scans and deletes many keys at once, blocking the event loop.

**Chain:**
- root: thousands of keys are created with the same TTL (e.g. all sessions set to expire at the top of the hour).
- s1: at the expiry moment the active expiration cycle (10×/sec, samples 20 keys, repeats without yielding when >25% expired) enters continuous repeat loops.
- s2: those repeat loops saturate CPU and block command processing for hundreds of milliseconds.
- D: brief latency spikes appear at the predictable TTL boundary (Symptom Recognition).

**Indicators:**
- s1: [Step 2] `LATENCY LATEST` shows `expire-cycle` events above 100 ms.
- s1: [Step 2] `LATENCY HISTORY expire-cycle` shows spikes at predictable intervals (hourly, at session TTL boundaries).
- D: [Symptom] latency spikes are brief (seconds) and correlate with traffic patterns or deployment times.

**Interventions:**
- **remediation** (root): add random TTL jitter in application code so expiries spread across the window.

  ```bash
  # Application-side: add random jitter when setting TTL
  # Instead of: EXPIRE key 3600
  # Use: EXPIRE key $((3600 + RANDOM % 600))  # +/- 10 minutes jitter
  # In Python: redis.expire(key, 3600 + random.randint(0, 600))
  ```

  **Verification:** `LATENCY HISTORY expire-cycle` shows no events above 100 ms; spikes at the previous expiry boundary no longer occur over two full TTL cycles.
- **mitigation** (s1): on Redis 7.0+, lower expiration-cycle aggressiveness to interrupt the repeat loop until jitter ships.

  ```bash
  # No immediate mitigation available without application code changes.
  # Temporarily lower the expiration cycle aggressiveness if Redis 7.0+:
  redis-cli CONFIG SET active-expire-enabled 0
  ```

  **Risk:** disabling active expiration grows memory until keys are lazily expired on access; only viable on Redis 7.0+. **Duration:** temporary; re-enable after deploying jitter in the application. **Verification:** re-run Step 2; `expire-cycle` events above 100 ms disappear.

---

### Cause G: Network round-trip or client connection overhead

**Statement:** The dominant latency component is network round-trip time between client and server, or per-command connection overhead from non-pooled connections.

**Chain:**
- root: commands cross a slow path — across AZs/regions (1–10 ms RTT each) or over non-pooled connections that pay TCP handshake (~200 µs–1 ms) per command.
- s1: applications issuing many sequential commands without pipelining multiply this per-command cost.
- D: all commands are uniformly slow (not just specific types), surfacing as elevated client latency (Symptom Recognition).

**Indicators:**
- root: [Step 7] `redis-cli --latency` average is significantly higher than `redis-cli --intrinsic-latency` on the server.
- root: [Step 7] `--intrinsic-latency` on the server shows below 1,000 µs, confirming Redis itself is healthy.
- D: [Symptom] all commands are uniformly slow (not just specific command types), ruling out O(N) commands.

**Interventions:**
- **remediation** (root): co-locate via Unix domain socket and enable client-side connection pooling.

  ```bash
  # Configure Unix domain socket in redis.conf (requires restart)
  # unixsocket /var/run/redis/redis.sock
  # unixsocketperm 777

  # Client-side: enable connection pooling
  # Python (redis-py): ConnectionPool(max_connections=50)
  # Java (Lettuce/Jedis): configure pool size matching thread count

  # Enable pipelining for bulk operations:
  # Send multiple commands in one network round-trip before reading responses
  ```

  **Verification:** `redis-cli --latency -h <host>` average drops within 2× of `--intrinsic-latency`; application p99 on Redis-backed endpoints returns to pre-incident levels.
- **defensive_fix** (s1): batch sequential commands via pipelining so many commands share one round-trip.

  ```bash
  # Test pipeline throughput (sends 100k SET commands in batches)
  redis-cli --pipe < /dev/null

  # Use Unix domain socket for co-located deployments
  redis-cli -s /var/run/redis/redis.sock PING
  ```

  **Verification:** re-run Step 7; per-command latency drops toward intrinsic baseline as round-trips collapse into batches. (Note: pipelining changes error handling — errors return per-command in the pipeline response.)

---

### Cause Z: Unidentified

**Statement:** The latency source does not match any pattern identified by SLOWLOG, LATENCY DOCTOR, swap checks, bigkeys, persistence metrics, or network measurement.

**Chain:**
- root: a platform-specific cause not covered by standard diagnostics — kernel bug, hypervisor scheduling jitter (Xen VMs), NTP clock adjustment, CPU frequency scaling (C-states/P-states), or NUMA topology.
- D: latency spikes persist with no matching diagnostic signal (Symptom Recognition).

**Indicators:**
- [Default] none of Causes A–G match findings from Steps 1–7.

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot, enable the software watchdog, and escalate to the SME.

  ```bash
  # Enable the software watchdog to capture stack traces on delays >500ms
  redis-cli CONFIG SET watchdog-period 500

  # Capture system-level scheduling jitter
  redis-cli --intrinsic-latency 100

  # Check for CPU frequency scaling on the server
  cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
  ```

  **Risk:** the software watchdog logs stack traces and slightly increases overhead. **Duration:** run watchdog 10–30 minutes to capture events, then disable with `CONFIG SET watchdog-period 0`. **Verification:** escalation engaged with `LATENCY DOCTOR` output, `--intrinsic-latency 100` results, `dmesg` from the latency window, Redis logs (`/var/log/redis/redis-server.log`), and OS/hypervisor versions; any watchdog log lines identify the exact blocking subsystem.

## Prevention

1. **Disable Transparent Huge Pages system-wide** — THP causes copy-on-write cost spikes after fork. Disable with `echo never > /sys/kernel/mm/transparent_hugepage/enabled` and persist via `/etc/rc.local` or a systemd unit.

2. **Set maxmemory with 20–30% headroom** — Configure `maxmemory` so Redis never approaches available physical RAM. Leave headroom for fork copy-on-write, output buffers, and OS overhead. Use `allkeys-lru` or `volatile-lru` eviction policy for cache workloads.

3. **Enable the latency monitor** — Set `latency-monitor-threshold 100` in `redis.conf`. Alerts when any event exceeds 100 ms and populates `LATENCY LATEST` for fast triage.

4. **Configure SLOWLOG with a meaningful threshold** — Set `slowlog-log-slower-than 10000` (10 ms) in production. Review `SLOWLOG GET 25` in runbooks and post-incident reviews.

5. **Replace all O(N) keyspace commands** — Ban `KEYS`, `SMEMBERS` (on large sets), `HGETALL` (on large hashes), and `SORT` in application code. Enforce with Redis ACLs (`-@dangerous`) or CI linting.

6. **Use `UNLINK` instead of `DEL` for large keys** — `UNLINK` performs asynchronous deletion (Redis 4.0+), preventing the event loop from blocking on memory reclaim.

7. **Enable active defragmentation** — Set `activedefrag yes` with `active-defrag-cycle-max 25` to automatically reduce fragmentation without restarts.

8. **Add TTL jitter for bulk-created keys** — When creating many keys with the same lifetime, add ±10% random jitter to distribute expiry load across the expiration window.

9. **Co-locate Redis and application servers** — Deploy Redis in the same availability zone and, when latency is critical, use Unix domain sockets for same-host communication to eliminate TCP overhead.

10. **Monitor `mem_fragmentation_ratio` and swap** — Alert when `mem_fragmentation_ratio` exceeds 1.5 or when `/proc/<pid>/smaps` swap sum is non-zero. Both conditions are early warnings of escalating latency.

## Sources

- [Redis Latency Diagnosis Guide](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/latency/) — Primary reference: causes table, fork timing benchmarks by platform, THP impact, AOF fsync options, intrinsic latency measurement, SLOWLOG and LATENCY commands. Priority 1.
- [Redis Latency Monitor Reference](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/latency-monitor/) — Complete LATENCY subcommand reference, monitored event types, time series data structure, configuration. Priority 1.
- [Redis Troubleshooting Overview](https://redis.io/docs/latest/operate/oss_and_stack/management/troubleshooting/) — Diagnostic tool index, crash vs latency triage split, RAM testing guidance. Priority 1.
