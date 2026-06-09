---
id: "redis-high-latency"
title: "Redis High Latency"
domain: database
service: redis
symptom_class: [latency]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
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

### Cause A: Blocking O(N) command executed against large keyspace or collection

**Statement:** A command with O(N) complexity — `KEYS`, `SMEMBERS`, `HGETALL`, `SORT`, or `LRANGE 0 -1` — ran against a large dataset and blocked Redis's single-threaded event loop for the full duration.

**Mechanism:** Redis processes commands sequentially in a single thread. An O(N) command that scans thousands of keys or elements holds the event loop for tens to hundreds of milliseconds, queuing every other client's request behind it. The larger the dataset, the longer the block; `KEYS *` on a keyspace with 1 million entries can take 500 ms or more.

**Indicator:**

- [Step 1] `SLOWLOG GET` shows `KEYS`, `SORT`, `SMEMBERS`, `HGETALL`, or `LRANGE 0 -1` with duration above 10,000 µs.
- [Step 2] `LATENCY HISTORY command` shows spikes correlated with the timestamps of those SLOWLOG entries.

<!-- match: {"step": 1, "predicate": "contains", "target": "KEYS"} -->
<!-- match: {"step": 1, "predicate": "contains", "target": "SMEMBERS"} -->
<!-- match: {"step": 1, "predicate": "contains", "target": "HGETALL"} -->

**Mitigation:**

- **Risk:** ACL approach takes effect immediately for existing connections; `rename-command` requires a restart.
- **Command:**

  ```bash
  redis-cli ACL SETUSER app-user -@dangerous
  ```

- **Duration:** Immediate for ACL; permanent after restart for `rename-command`.

**Resolution:**

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

**Verification:** After code change deploys, `SLOWLOG GET 25` returns no entries with O(N) command names and duration above 1,000 µs. `LATENCY LATEST` shows `command` event max below 10 ms.

---

### Cause B: Fork latency from RDB snapshot or AOF rewrite

**Statement:** A `BGSAVE` or `BGREWRITEAOF` operation forked the Redis process, and the OS's copy-on-write page table duplication caused a latency spike lasting hundreds of milliseconds to several seconds.

**Mechanism:** Fork duplicates the Redis process's page table, which is proportional to dataset size (roughly 2–10 ms per GB on physical hardware, but 200–400 ms/GB on Xen-based VMs). While the child writes the snapshot, every write in the parent triggers a copy-on-write page fault, adding CPU and memory bus pressure. Transparent Huge Pages (THP) compound this: a 2 MB huge page must be fully copied when any byte within it changes, multiplying copy-on-write cost by 512× compared to 4 KB pages.

**Indicator:**

- [Step 2] `LATENCY HISTORY fork` shows values above 200 ms.
- [Step 5] `rdb_last_bgsave_time_sec` above 5 s or `aof_last_rewrite_time_sec` above 5 s.
- [Symptom] Latency spikes occur on a predictable schedule matching RDB `save` trigger thresholds.

<!-- match: {"step": 5, "predicate": "threshold", "target": "rdb_last_bgsave_time_sec", "op": ">", "value": 5} -->
<!-- match: {"step": 2, "predicate": "contains", "target": "fork"} -->

**Mitigation:**

- **Risk:** Disabling persistence creates a data-loss window. Only use while actively investigating.
- **Command:**

  ```bash
  redis-cli CONFIG SET save ""
  redis-cli CONFIG SET appendonly no
  ```

- **Duration:** Until root cause is confirmed and persistence is re-enabled with tuned settings.

**Resolution:**

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

- **Impact:** THP change is system-wide and takes effect immediately without Redis restart. AOF config changes are in-memory; persist to `redis.conf` for durability after restart.
- **Rollback:** `echo madvise | sudo tee /sys/kernel/mm/transparent_hugepage/enabled`

**Verification:** After THP disabled and AOF tuned, `LATENCY HISTORY fork` shows no events above 200 ms across at least two full RDB/AOF cycles. `INFO persistence` field `latest_fork_usec` drops by 50%+ compared to baseline.

---

### Cause C: Redis process swapping to disk

**Statement:** The Redis process has been partially or fully swapped to disk, causing every memory access that hits a swapped page to incur milliseconds of disk I/O latency instead of nanoseconds of RAM access.

**Mechanism:** When the system runs low on physical RAM, the kernel swaps Redis pages to disk. Because Redis is single-threaded, a single swapped page during a command execution blocks the entire event loop while the page fault resolves via disk I/O. Even a few kilobytes of swap usage can produce 50–500 ms latency spikes. This is especially common when Redis `maxmemory` is not set, allowing the dataset to grow unconstrained until it exceeds available RAM.

**Indicator:**

- [Step 3] `awk '/^Swap:/ {sum+=$2}' /proc/<pid>/smaps` returns non-zero value.
- [Step 3] `vmstat 1` shows non-zero `si` (swap-in) or `so` (swap-out) columns.
- [Step 6] `mem_fragmentation_ratio` below 1.0.

<!-- match: {"step": 3, "predicate": "threshold", "target": "swap_kb", "op": ">", "value": 0} -->
<!-- match: {"step": 6, "predicate": "threshold", "target": "mem_fragmentation_ratio", "op": "<", "value": 1.0} -->

**Mitigation:**

- **Risk:** `swapoff -a` requires sufficient free RAM to absorb all swapped pages. If RAM is insufficient, the kernel will OOM-kill Redis.
- **Command:**

  ```bash
  # Reduce system swappiness to discourage swap use
  sudo sysctl vm.swappiness=1

  # Check available RAM before disabling swap
  free -h

  # Disable swap only if sufficient free RAM exists
  sudo swapoff -a
  ```

- **Duration:** `sysctl` change is immediate but non-persistent. Swap-off may take 1–5 minutes as pages are moved back to RAM.

**Resolution:**

```bash
# Set maxmemory to leave 20-30% of physical RAM for OS and fork operations
redis-cli CONFIG SET maxmemory 6gb
redis-cli CONFIG SET maxmemory-policy allkeys-lru

# Persist to redis.conf
echo "maxmemory 6gb" | sudo tee -a /etc/redis/redis.conf
echo "maxmemory-policy allkeys-lru" | sudo tee -a /etc/redis/redis.conf
```

- **Impact:** Setting `maxmemory` triggers eviction of excess keys under `allkeys-lru`. Verify that evicted keys are cache-safe before applying in production.
- **Rollback:** `redis-cli CONFIG SET maxmemory 0` removes the memory limit.

**Verification:** `awk '/^Swap:/ {sum+=$2}' /proc/$(pgrep -f redis-server)/smaps` returns `0 kB`. `vmstat 1 5` shows `si` and `so` columns at `0`. `redis-cli --latency` average returns below 1 ms.

---

### Cause D: High memory fragmentation

**Statement:** Redis's memory allocator has fragmented physical memory significantly, so the RSS (resident set size) is much larger than the logical used_memory, degrading allocator performance and increasing memory pressure.

**Mechanism:** Under workloads with highly variable key sizes or frequent key deletions and re-insertions, jemalloc (Redis's default allocator) cannot reuse freed memory blocks efficiently. The fragmentation ratio (RSS ÷ used_memory) climbs above 1.5, meaning Redis consumes 50%+ more RAM than the actual data requires. This wastes RAM that could be used for data, increases the likelihood of swap pressure, and slows allocation paths as the allocator searches for suitable free blocks.

**Indicator:**

- [Step 6] `mem_fragmentation_ratio` above 1.5.
- [Step 6] `used_memory_rss_human` significantly larger than `used_memory_human`.

<!-- match: {"step": 6, "predicate": "threshold", "target": "mem_fragmentation_ratio", "op": ">", "value": 1.5} -->

**Mitigation:**

- **Risk:** Active defragmentation consumes CPU cycles. Tune cycle percentages to avoid impacting foreground latency.
- **Command:**

  ```bash
  redis-cli CONFIG SET activedefrag yes
  redis-cli CONFIG SET active-defrag-threshold-lower 10
  redis-cli CONFIG SET active-defrag-cycle-min 1
  redis-cli CONFIG SET active-defrag-cycle-max 25
  ```

- **Duration:** Defragmentation runs continuously in the background; ratio improves over minutes to hours.

**Resolution:** Same as Mitigation.

**Verification:** Over 30–60 minutes, `redis-cli INFO memory | grep mem_fragmentation_ratio` trends downward toward 1.0–1.3. `used_memory_rss_human` converges closer to `used_memory_human`.

---

### Cause E: Large key causing serialization and transfer overhead

**Statement:** One or more Redis keys contain values too large (strings above 10 KB or collections above 1,000 elements) for efficient single-command serialization, network transfer, and client deserialization.

**Mechanism:** Reading or writing a large key requires Redis to serialize the entire value in memory, write it to the network output buffer, and wait for the client to acknowledge receipt. A 1 MB string value adds roughly 1 ms of serialization and ~1 ms of transfer latency per operation over a 1 Gbit/s link, and operations on large collections (`HGETALL`, `SMEMBERS`) block the event loop for the full enumeration time. Deletion of large keys (`DEL`) is also synchronous and blocks while freeing memory.

**Indicator:**

- [Step 4] `redis-cli --bigkeys` reports a key with size above 10,240 bytes (strings) or element count above 1,000 (collections).
- [Step 4] `redis-cli MEMORY USAGE <key>` returns value above 10,240.
- [Step 1] SLOWLOG shows `GET`, `HGETALL`, `SMEMBERS`, or `DEL` on the same key name with high duration.

<!-- match: {"step": 4, "predicate": "threshold", "target": "memory_usage_bytes", "op": ">", "value": 10240} -->

**Mitigation:**

- **Risk:** Splitting keys requires application code changes; interim state may cause partial reads if not handled atomically.
- **Command:**

  ```bash
  # Delete a large key asynchronously (non-blocking, Redis 4.0+)
  redis-cli UNLINK <large_key_name>
  ```

- **Duration:** `UNLINK` returns immediately; background deletion completes within seconds.

**Resolution:**

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

**Verification:** `redis-cli --bigkeys` no longer reports keys above 10 KB or collections above 1,000 elements. `SLOWLOG GET 25` shows no GET/HGETALL/SMEMBERS operations with duration above 10,000 µs.

---

### Cause F: Mass key expiration blocking the event loop

**Statement:** A large number of keys are configured with the same or near-identical TTL, causing the Redis expiration cycle to scan and delete many keys simultaneously, blocking the event loop.

**Mechanism:** Redis runs an active expiration cycle up to 10 times per second. Each cycle samples 20 keys and, if more than 25% have expired, immediately repeats the scan without yielding. When thousands of keys are created with the same TTL (e.g., all sessions set to expire at the top of the hour), the expiration cycle triggers continuous repeat loops at the expiry moment, driving CPU saturation and blocking command processing for hundreds of milliseconds.

**Indicator:**

- [Step 2] `LATENCY LATEST` shows `expire-cycle` events above 100 ms.
- [Step 2] `LATENCY HISTORY expire-cycle` shows spikes at predictable intervals (hourly, at session TTL boundaries).
- [Symptom] Latency spikes are brief (seconds) and correlate with traffic patterns or deployment times.

<!-- match: {"step": 2, "predicate": "contains", "target": "expire-cycle"} -->

**Mitigation:**

- **Risk:** None. Adding jitter to existing keys' TTL requires a one-time scan of the keyspace.
- **Command:**

  ```bash
  # No immediate mitigation available without application code changes.
  # Temporarily lower the expiration cycle aggressiveness if Redis 7.0+:
  redis-cli CONFIG SET active-expire-enabled 0
  ```

- **Duration:** Temporary; re-enable after deploying jitter in application.

**Resolution:**

```bash
# Application-side: add random jitter when setting TTL
# Instead of: EXPIRE key 3600
# Use: EXPIRE key $((3600 + RANDOM % 600))  # +/- 10 minutes jitter
# In Python: redis.expire(key, 3600 + random.randint(0, 600))
```

**Verification:** `LATENCY HISTORY expire-cycle` shows no events above 100 ms. Latency spikes correlated with the previous expiry boundary no longer occur over two full TTL cycles.

---

### Cause G: Network round-trip or client connection overhead

**Statement:** The dominant latency component is network round-trip time between the Redis client and server, or per-command connection overhead from non-pooled connections.

**Mechanism:** Each Redis command incurs at minimum one round-trip: client sends, server processes, server responds. At 1 Gbit/s within a data center, a TCP round-trip adds ~200 µs per command; across availability zones or regions, it adds 1–10 ms per command. Non-pooled connections add TCP handshake overhead (~200 µs–1 ms) on every command. Applications that issue many sequential commands without pipelining multiply this cost.

**Indicator:**

- [Step 7] `redis-cli --latency` average is significantly higher than `redis-cli --intrinsic-latency` on the server.
- [Step 7] `--intrinsic-latency` on the server shows below 1,000 µs, confirming Redis itself is healthy.
- [Symptom] All commands are uniformly slow (not just specific command types), ruling out O(N) commands.

<!-- match: {"step": 7, "predicate": "threshold", "target": "avg_latency_ms", "op": ">", "value": 1} -->

**Mitigation:**

- **Risk:** Pipelining changes command error handling semantics; errors are returned per-command in the pipeline response.
- **Command:**

  ```bash
  # Test pipeline throughput (sends 100k SET commands in batches)
  redis-cli --pipe < /dev/null

  # Use Unix domain socket for co-located deployments
  redis-cli -s /var/run/redis/redis.sock PING
  ```

- **Duration:** Immediate once client is configured to use pooling and pipelining.

**Resolution:**

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

**Verification:** `redis-cli --latency -h <host>` average drops to within 2× of `--intrinsic-latency` baseline. Application p99 latency on Redis-backed endpoints returns to pre-incident levels.

---

### Cause Z: Unidentified latency cause

**Statement:** The latency source does not match any pattern identified by SLOWLOG, LATENCY DOCTOR, swap checks, bigkeys, persistence metrics, or network measurement.

**Mechanism:** Redis latency can have platform-specific causes not covered by standard diagnostic commands, including kernel bugs, hypervisor scheduling jitter (especially on Xen-based VMs), NTP clock adjustments, CPU frequency scaling (C-states/P-states), and NUMA topology issues.

**Indicator:**

- [Default] None of the above causes match findings from Steps 1–7.

**Mitigation:**

- **Risk:** Software watchdog logs stack traces and slightly increases overhead.
- **Command:**

  ```bash
  # Enable the software watchdog to capture stack traces on delays >500ms
  redis-cli CONFIG SET watchdog-period 500

  # Capture system-level scheduling jitter
  redis-cli --intrinsic-latency 100

  # Check for CPU frequency scaling on the server
  cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor
  ```

- **Duration:** Run watchdog for 10–30 minutes to capture events, then disable with `CONFIG SET watchdog-period 0`.

**Resolution:** Out of runbook scope. Escalate with: `LATENCY DOCTOR` output, `redis-cli --intrinsic-latency 100` results, `dmesg` from the latency window, Redis server logs (`/var/log/redis/redis-server.log`), and OS/hypervisor version details.

**Verification:** Escalation path is engaged. If watchdog logs appear in Redis logs, they identify the exact blocking subsystem for the engineering team.

---

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
