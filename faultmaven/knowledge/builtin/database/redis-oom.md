---
id: redis-oom
title: "Redis Out of Memory"
domain: database
service: redis
symptom_class:
  - oom
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - redis
  - oom
  - maxmemory
  - eviction
  - memory-fragmentation
  - big-keys
difficulty: intermediate
---

# Redis Out of Memory

## Problem Definition

Applies to Redis 6.0 and later (compatible with earlier versions; memory management commands are stable). Requires access to the Redis CLI (`redis-cli`) with permissions to run `INFO`, `CONFIG`, `MEMORY`, and `DEBUG` commands. ACL restrictions (Redis 6+) may limit access to administrative commands.

Redis out of memory (OOM) occurs when memory usage reaches the `maxmemory` limit and the configured eviction policy either rejects writes or cannot free sufficient memory. If `maxmemory` is not configured (default: 0, meaning no limit), Redis grows until the operating system's OOM killer terminates the process.

Clients receive write rejection errors when the `maxmemory` limit is reached and eviction cannot free enough space:

```text
OOM command not allowed when used memory > 'maxmemory'.
```

Symptoms include write operations failing with OOM errors while reads continue to succeed, applications experiencing cache write failures and falling back to the database, elevated latency as Redis performs eviction scans before each write, and in the worst case, the Linux OOM killer terminating the Redis process entirely (visible in `dmesg` or system logs).

Common causes include unbounded key growth (missing TTLs on cache keys), large keys consuming disproportionate memory (a single key with a multi-megabyte value), memory fragmentation causing the RSS to far exceed the logical data size, `maxmemory` set too low relative to the working dataset, eviction policy set to `noeviction` (rejects all writes when full), and RDB/AOF child processes doubling memory usage during persistence operations due to copy-on-write.

## Diagnostic Steps

### Step 1. Check memory usage and limits

Determine current memory consumption relative to the configured limit.

```bash
redis-cli INFO memory
```

Key fields:

- `used_memory_human` — logical memory used by Redis data structures.
- `used_memory_rss_human` — actual physical memory (RSS) from the OS perspective.
- `maxmemory_human` — configured memory limit (0 = no limit).
- `maxmemory_policy` — active eviction policy.
- `mem_fragmentation_ratio` — ratio of RSS to used_memory. Above 1.5 indicates significant fragmentation.
- `used_memory_peak_human` — peak memory usage since last restart.

If `used_memory` is at or near `maxmemory`, the instance is at capacity.

### Step 2. Check eviction metrics

Determine whether eviction is actively occurring and how many keys have been evicted.

```bash
redis-cli INFO stats | grep -E 'evicted_keys|keyspace_hits|keyspace_misses'
```

A high `evicted_keys` count means Redis is actively removing keys to make room. If eviction cannot keep up with write pressure, OOM errors occur. A rising `keyspace_misses` relative to `keyspace_hits` indicates eviction is removing keys that applications still need.

### Step 3. Identify large keys consuming the most memory

Find the biggest keys in each data type.

```bash
redis-cli --bigkeys
```

This performs a full keyspace scan and reports the largest key per type (string, hash, list, set, sorted set, stream). Keys exceeding 1 MB are primary targets for optimization.

For specific key memory analysis:

```bash
redis-cli MEMORY USAGE key_name SAMPLES 0
```

### Step 4. Analyze memory by data type

Get a breakdown of memory usage by key type and overhead.

```bash
redis-cli MEMORY STATS
```

Key categories in the output:

- `dataset.bytes` — memory used by actual data.
- `overhead.total` — memory used by Redis internals (client buffers, replication, AOF).
- `keys.count` — total number of keys.
- `clients.normal` — memory used by client output buffers.

If `overhead.total` is a significant fraction of `used_memory`, non-data overhead is the issue.

### Step 5. Check for keys without TTL

Keys without a TTL never expire and accumulate indefinitely.

```bash
# Sample random keys and check for missing TTLs
for i in $(seq 1 100); do
  key=$(redis-cli RANDOMKEY)
  ttl=$(redis-cli TTL "$key")
  if [ "$ttl" = "-1" ]; then
    echo "NO TTL: $key ($(redis-cli TYPE "$key"))"
  fi
done
```

A large proportion of keys with TTL = -1 (no expiry) suggests the application is not setting TTLs on cache entries.

### Step 6. Check for client output buffer memory

Client output buffers can consume significant memory, especially with pub/sub or large result sets.

```bash
redis-cli CLIENT LIST | awk -F'[ =]' '{for(i=1;i<=NF;i++) {if($i=="omem") omem=$(i+1); if($i=="addr") addr=$(i+1)}} omem+0 > 1048576 {print addr, "output_buffer="omem}' | sort -t= -k2 -rn
```

Clients with output buffer (`omem`) exceeding 1 MB are consuming memory for buffered responses. Slow consumers (especially pub/sub subscribers) are a common cause.

### Step 7. Check for persistence-related memory overhead

RDB snapshots and AOF rewrites fork the Redis process, which can temporarily double memory usage due to copy-on-write.

```bash
redis-cli INFO persistence | grep -E 'rdb_|aof_'
```

If `rdb_bgsave_in_progress` or `aof_rewrite_in_progress` is 1, a background persistence operation is running and may be consuming additional memory via copy-on-write.

```bash
# Check copy-on-write memory from the last fork
redis-cli INFO persistence | grep latest_fork_usec
```

## Mitigation

### Option 1. Switch eviction policy to allow writes

**Risk**: Medium. Evicting keys means some cache lookups will miss and fall through to the database. Choose the policy that best fits your workload.

**Command**:

```bash
# LRU eviction across all keys (most common for caching)
redis-cli CONFIG SET maxmemory-policy allkeys-lru

# Or volatile-lru (only evict keys with TTL set)
redis-cli CONFIG SET maxmemory-policy volatile-lru
```

**Verify**:

```bash
redis-cli CONFIG GET maxmemory-policy
redis-cli SET test_key test_value
# Should succeed instead of returning OOM
```

**Duration**: Immediate. Writes resume as eviction frees memory.

### Option 2. Delete large or unnecessary keys

**Risk**: Low-Medium. Removes specific keys. Applications must handle the missing key gracefully.

**Command**:

```bash
# Use UNLINK for async deletion of large keys (Redis 4.0+)
redis-cli UNLINK large_key_name

# For multiple keys matching a pattern (use SCAN, not KEYS)
redis-cli --scan --pattern "temp:*" | xargs -L 100 redis-cli UNLINK
```

**Verify**:

```bash
redis-cli INFO memory | grep used_memory_human
```

**Duration**: Immediate for `UNLINK`. Large key deletion happens asynchronously in the background.

### Option 3. Increase maxmemory

**Risk**: Low if sufficient physical RAM is available. High if it causes swap usage.

**Command**:

```bash
redis-cli CONFIG SET maxmemory 12gb
```

**Verify**:

```bash
redis-cli CONFIG GET maxmemory
redis-cli INFO memory | grep -E 'used_memory_human|maxmemory_human'
```

**Duration**: Immediate. Ensure the host has at least `maxmemory * 1.5` physical RAM to accommodate fork operations and fragmentation.

### Option 4. Flush expired keys aggressively

**Risk**: Low. Forces Redis to scan for and remove expired keys more aggressively. Only effective if many keys have expired but not yet been cleaned up by the lazy expiration mechanism.

**Command**:

```bash
# Trigger active expiry by scanning the keyspace
redis-cli DEBUG SET-ACTIVE-EXPIRE 1

# Or use SCAN to touch keys and trigger lazy expiry
redis-cli --scan --pattern "*" | head -10000 | xargs -L 100 redis-cli EXISTS
```

**Verify**:

```bash
redis-cli INFO keyspace
redis-cli INFO memory | grep used_memory_human
```

**Duration**: Minutes. Depends on how many expired keys exist.

## Root Cause Resolution

**If** keys are missing TTLs → set TTLs on all cache entries at the application level:

```python
# Python redis-py example
client.set('cache:user:123', data, ex=3600)  # 1 hour TTL
client.hset('session:abc', mapping=session_data)
client.expire('session:abc', 1800)  # 30 minute TTL
```

Audit the codebase to ensure every `SET`, `HSET`, `LPUSH`, etc. includes a TTL. Use `volatile-lru` or `volatile-ttl` eviction as a safety net for keys that accidentally miss TTLs.

**If** large keys consume disproportionate memory → restructure the data model:

- Split large strings into smaller chunks with a consistent naming convention.
- Replace large hashes (>1000 fields) with bucketed hashes.
- Replace large lists with capped lists using `LTRIM` after each push.
- Replace large sorted sets with time-partitioned sets.

```bash
# Cap a list to the most recent 1000 entries
redis-cli LPUSH mylist new_value
redis-cli LTRIM mylist 0 999
```

**If** memory fragmentation is high → enable active defragmentation:

```bash
redis-cli CONFIG SET activedefrag yes
redis-cli CONFIG SET active-defrag-threshold-lower 10
redis-cli CONFIG SET active-defrag-cycle-min 1
redis-cli CONFIG SET active-defrag-cycle-max 25
```

If fragmentation is extreme (ratio > 2.0), a Redis restart with the same dataset will compact memory.

**If** client output buffers consume excessive memory → set buffer limits:

```bash
# Limit normal client output buffers
redis-cli CONFIG SET client-output-buffer-limit "normal 256mb 128mb 60"

# Limit pub/sub client output buffers
redis-cli CONFIG SET client-output-buffer-limit "pubsub 64mb 32mb 60"
```

This disconnects clients whose output buffer exceeds the hard limit or stays above the soft limit for the specified duration.

**If** persistence fork operations cause temporary OOM → reduce the dataset size to ensure `maxmemory` leaves room for copy-on-write overhead. On Linux, set `overcommit_memory` to allow fork to succeed:

```bash
sudo sysctl vm.overcommit_memory=1
```

This prevents the kernel from refusing the fork due to apparent memory overcommit.

**If** `maxmemory` is set too low relative to the dataset → right-size based on actual data requirements. Calculate the minimum required memory as: live data size + 30% for fragmentation and overhead + copy-on-write headroom for persistence.

## Verification

After applying fixes, confirm the OOM condition is resolved.

1. Memory usage is below the limit:

```bash
redis-cli INFO memory | grep -E 'used_memory_human|maxmemory_human|mem_fragmentation_ratio'
```

Expect `used_memory` well below `maxmemory` and `mem_fragmentation_ratio` between 1.0 and 1.5.

1. Writes succeed:

```bash
redis-cli SET oom_test_key "test_value" EX 60
```

Expect `OK`.

1. Eviction is manageable:

```bash
redis-cli INFO stats | grep evicted_keys
```

Note the value and check again after 5 minutes. The rate of eviction should be stable and not increasing.

1. No swap usage:

```bash
cat /proc/$(pgrep -f redis-server)/smaps | grep -i swap | awk '{sum+=$2} END {print sum " kB"}'
```

Expect 0 kB.

1. No OOM errors in application logs. Check application error logs for the `OOM command not allowed` message.

## Prevention

1. **Always set maxmemory** — Never run Redis in production without a `maxmemory` limit. Without it, Redis grows until the OS OOM killer terminates it, causing complete data loss.

2. **Choose an appropriate eviction policy** — Use `allkeys-lru` for general caching, `volatile-lru` when mixing cache and persistent data, or `volatile-ttl` to evict keys closest to expiry first. Never use `noeviction` for cache workloads.

3. **Set TTLs on all cache keys** — Every cache entry should have a TTL. Treat missing TTLs as a bug in code review.

4. **Monitor used_memory and evicted_keys** — Alert when `used_memory` exceeds 80% of `maxmemory`. Alert on a sustained increase in `evicted_keys` rate.

5. **Size maxmemory with headroom** — Set `maxmemory` to leave at least 30% of physical RAM for the OS, fork operations, and fragmentation. Formula: `maxmemory = physical_RAM * 0.7 - other_process_memory`.

6. **Enable active defragmentation** — Reduces fragmentation without restarts on Redis 4.0+.

7. **Limit large keys** — Enforce key size guidelines in code review: strings under 100 KB, collections under 1000 elements. Use `--bigkeys` scans in periodic maintenance jobs.

8. **Set client output buffer limits** — Prevent slow consumers (especially pub/sub) from consuming unbounded memory with `client-output-buffer-limit`.

9. **Configure vm.overcommit_memory=1** — On Linux, set `sysctl vm.overcommit_memory=1` to allow Redis fork operations to succeed even when memory is near capacity.

10. **Persist configuration** — After using `CONFIG SET`, run `CONFIG REWRITE` to persist changes to the configuration file.

## Sources

- [Redis Documentation: Memory Optimization](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/memory-optimization/) — Official reference for memory management, fragmentation, and large key handling.
- [Redis Documentation: Eviction Policies](https://redis.io/docs/latest/develop/reference/eviction/) — Official reference for `maxmemory-policy` options and eviction behavior.
- [Redis Documentation: Server Configuration](https://redis.io/docs/latest/operate/oss_and_stack/management/config/) — Official reference for `maxmemory`, persistence, and memory-related configuration parameters.
- [Redis Documentation: MEMORY USAGE and MEMORY STATS](https://redis.io/docs/latest/commands/memory-usage/) — Official reference for per-key and aggregate memory analysis commands.
