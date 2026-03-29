---
id: redis-high-latency
title: "Redis High Latency"
domain: database
service: redis
symptom_class:
  - latency
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - redis
  - latency
  - slowlog
  - persistence
  - keys-command
  - big-keys
difficulty: intermediate
---

# Redis High Latency

## Problem Definition

Applies to Redis 6.0 and later (compatible with earlier versions; commands used are stable across versions). Requires access to the Redis CLI (`redis-cli`) with permissions to run `SLOWLOG`, `LATENCY`, `INFO`, and `CONFIG` commands. ACL restrictions (Redis 6+) may limit access to these administrative commands.

Redis is designed for sub-millisecond response times. When latency increases to tens or hundreds of milliseconds, applications experience degraded performance across all Redis-dependent features including caching, session management, rate limiting, and pub/sub messaging.

Symptoms include elevated p95/p99 latency on application endpoints that use Redis, cache timeouts or slow cache responses, `SLOWLOG` entries showing commands taking more than a few milliseconds, and client-side connection timeout errors. The Redis `LATENCY` subsystem may also report spikes:

```text
redis-cli LATENCY LATEST
1) 1) "command"
   2) (integer) 1711574400
   3) (integer) 250
   4) (integer) 500
```

This shows the latest event name, timestamp, latency in milliseconds, and maximum latency.

Common causes include expensive O(N) commands (`KEYS`, `SMEMBERS` on large sets, `HGETALL` on large hashes, `SORT`), large key values (keys exceeding 10 KB cause increased serialization and network overhead), persistence operations (RDB snapshots and AOF rewrites forking the process), memory fragmentation forcing the allocator to work harder, insufficient memory causing active eviction overhead, network saturation between clients and the Redis server, and swap usage when Redis memory exceeds available physical RAM.

## Diagnostic Steps

### Step 1. Check the SLOWLOG for expensive commands

The SLOWLOG records commands that exceed a configurable execution time threshold.

```bash
redis-cli SLOWLOG GET 25
```

Each entry shows the command, execution duration in microseconds, and timestamp. Commands taking more than 10,000 microseconds (10 ms) are problematic. Look for patterns: `KEYS *`, `SMEMBERS` on large sets, `LRANGE 0 -1` on long lists, `HGETALL` on large hashes.

Check the current slowlog threshold:

```bash
redis-cli CONFIG GET slowlog-log-slower-than
```

Default is 10000 microseconds (10 ms). For latency investigation, temporarily lower it:

```bash
redis-cli CONFIG SET slowlog-log-slower-than 1000
```

### Step 2. Use the LATENCY subsystem for event analysis

Redis tracks latency-causing events in categories.

```bash
redis-cli LATENCY LATEST
redis-cli LATENCY HISTORY command
redis-cli LATENCY HISTORY fast-command
redis-cli LATENCY HISTORY fork
```

If `fork` events show high latency, persistence operations (RDB/AOF) are the cause. If `command` events dominate, slow commands are the issue.

For an automated diagnosis:

```bash
redis-cli LATENCY DOCTOR
```

This provides a human-readable analysis of latency sources.

### Step 3. Identify large keys

Large keys cause high latency during serialization, transfer, and deletion.

```bash
redis-cli --bigkeys
```

This scans the entire keyspace and reports the largest key in each data type. Keys exceeding 10 KB for strings or 1000 elements for collections are candidates for optimization.

For a more targeted scan:

```bash
redis-cli MEMORY USAGE key_name
```

### Step 4. Check persistence configuration and impact

RDB snapshots and AOF rewrites fork the Redis process, which can cause latency spikes proportional to the amount of memory used.

```bash
redis-cli INFO persistence
```

Key fields:

- `rdb_last_bgsave_time_sec` — duration of the last RDB snapshot.
- `aof_last_rewrite_time_sec` — duration of the last AOF rewrite.
- `rdb_last_bgsave_status` — `ok` or error.
- `aof_rewrite_in_progress` — 1 if a rewrite is happening now.
- `loading` — 1 if the server is loading data from disk.

If `rdb_last_bgsave_time_sec` or `aof_last_rewrite_time_sec` is high (more than a few seconds), fork operations are contributing to latency.

### Step 5. Check memory and fragmentation

Memory pressure and fragmentation degrade performance.

```bash
redis-cli INFO memory
```

Key fields:

- `used_memory_human` — total memory used by Redis.
- `used_memory_rss_human` — resident set size (actual physical memory).
- `mem_fragmentation_ratio` — ratio of RSS to used_memory. Values above 1.5 indicate significant fragmentation. Values below 1.0 indicate swap usage (critical).
- `maxmemory_human` — configured memory limit.
- `maxmemory_policy` — eviction policy in effect.

A `mem_fragmentation_ratio` below 1.0 means Redis is using swap, which causes order-of-magnitude latency degradation.

### Step 6. Check for swap usage

Swap is the most severe latency cause for Redis. Even minor swap usage causes massive latency spikes.

```bash
# Check if the Redis process is swapping
cat /proc/$(pgrep -f redis-server)/smaps | grep -i swap | awk '{sum+=$2} END {print sum " kB"}'
```

Any non-zero swap usage for the Redis process is a critical issue.

### Step 7. Check network latency between client and server

Network latency adds to every Redis operation.

```bash
# Measure baseline round-trip time
redis-cli --latency -h redis-host -p 6379

# Measure intrinsic latency (system-level, run on Redis server)
redis-cli --intrinsic-latency 10
```

The `--latency` test shows the round-trip time including network. The `--intrinsic-latency` test (run on the server itself) shows the minimum latency achievable by the system. If `--latency` is much higher than `--intrinsic-latency`, the network is the bottleneck.

## Mitigation

### Option 1. Rename or disable dangerous commands

**Risk**: Low. Prevents O(N) commands like `KEYS` from being executed in production. Applications using these commands will receive errors and must be updated.

**Command**:

```bash
# In redis.conf (requires restart)
rename-command KEYS ""
rename-command FLUSHALL ""
rename-command FLUSHDB ""
```

For immediate effect without restart, use ACLs (Redis 6+):

```bash
redis-cli ACL SETUSER app-user -@dangerous
```

**Verify**:

```bash
redis-cli KEYS '*'
# Should return an error
```

**Duration**: Immediate for ACL changes; restart required for rename-command.

### Option 2. Temporarily disable persistence

**Risk**: Medium. Data loss occurs if Redis crashes during this period. Only use during active latency investigation when persistence is identified as the cause.

**Command**:

```bash
# Disable RDB snapshots
redis-cli CONFIG SET save ""

# Disable AOF
redis-cli CONFIG SET appendonly no
```

**Verify**:

```bash
redis-cli CONFIG GET save
redis-cli CONFIG GET appendonly
```

**Duration**: Immediate. Re-enable persistence after resolving the root cause.

### Option 3. Move Redis off swap

**Risk**: High if done incorrectly. If Redis is actively swapping, the server must have more physical RAM allocated or the dataset must be reduced.

**Command**:

```bash
# Check current swap usage
free -h

# If other processes are swappable, reduce swappiness
sudo sysctl vm.swappiness=1

# If Redis is the only consumer, disable swap entirely (requires sufficient RAM)
sudo swapoff -a
```

**Verify**:

```bash
cat /proc/$(pgrep -f redis-server)/smaps | grep -i swap | awk '{sum+=$2} END {print sum " kB"}'
```

**Duration**: Immediate for sysctl changes. Swap-off may take minutes as pages are moved back to RAM.

### Option 4. Increase maxmemory and adjust eviction policy

**Risk**: Low-Medium. Requires sufficient physical RAM. An inappropriate eviction policy may evict important keys.

**Command**:

```bash
redis-cli CONFIG SET maxmemory 8gb
redis-cli CONFIG SET maxmemory-policy allkeys-lru
```

**Verify**:

```bash
redis-cli INFO memory | grep -E 'maxmemory|evicted_keys'
```

**Duration**: Immediate.

## Root Cause Resolution

**If** expensive O(N) commands are the cause → replace them with efficient alternatives:

- Replace `KEYS pattern` with `SCAN 0 MATCH pattern COUNT 100` (cursor-based, non-blocking).
- Replace `SMEMBERS large_set` with `SSCAN large_set 0 COUNT 100`.
- Replace `HGETALL large_hash` with `HSCAN large_hash 0 COUNT 100` or `HMGET` for specific fields.
- Replace `LRANGE list 0 -1` with paginated `LRANGE list 0 99`.
- Replace `DEL large_key` with `UNLINK large_key` (async deletion, Redis 4.0+).

**If** large keys cause serialization overhead → break large keys into smaller chunks:

```bash
# Instead of one hash with 100K fields:
# HSET user:data field1 val1 field2 val2 ... field100000 val100000

# Use bucketed hashes:
# HSET user:data:0 field1 val1 ... field1000 val1000
# HSET user:data:1 field1001 val1001 ...
```

**If** persistence fork operations cause latency spikes → schedule RDB snapshots during off-peak hours and tune AOF:

```bash
# Reduce RDB snapshot frequency
redis-cli CONFIG SET save "3600 1 300 100"

# Use AOF with everysec fsync (default, good balance)
redis-cli CONFIG SET appendonly yes
redis-cli CONFIG SET appendfsync everysec

# Enable jemalloc background threads for faster fork (Redis 6+)
redis-cli CONFIG SET jemalloc-bg-thread yes
```

On Linux, enable Transparent Huge Pages (THP) disable for Redis to reduce fork latency:

```bash
echo never > /sys/kernel/mm/transparent_hugepage/enabled
```

**If** memory fragmentation is high → enable active defragmentation (Redis 4.0+):

```bash
redis-cli CONFIG SET activedefrag yes
redis-cli CONFIG SET active-defrag-enabled yes
redis-cli CONFIG SET active-defrag-threshold-lower 10
redis-cli CONFIG SET active-defrag-cycle-min 1
redis-cli CONFIG SET active-defrag-cycle-max 25
```

**If** the Redis process is using swap → provision more physical RAM or reduce the dataset size. Redis must never use swap in production. Set `maxmemory` to leave at least 20-30% of physical RAM for the OS and fork operations.

**If** network latency is the bottleneck → co-locate Redis with application servers in the same availability zone, use Unix domain sockets for co-located deployments, or enable pipelining in the client library to amortize round-trip costs.

## Verification

After applying fixes, confirm latency has returned to acceptable levels.

1. SLOWLOG shows no recent entries:

```bash
redis-cli SLOWLOG GET 10
```

Expect no entries with durations exceeding 10 ms, or very few.

1. Latency baseline is healthy:

```bash
redis-cli --latency -h redis-host -p 6379
```

Expect average latency under 1 ms for a local or same-AZ Redis instance.

1. LATENCY DOCTOR reports no issues:

```bash
redis-cli LATENCY DOCTOR
```

1. No swap usage:

```bash
cat /proc/$(pgrep -f redis-server)/smaps | grep -i swap | awk '{sum+=$2} END {print sum " kB"}'
```

Expect 0 kB.

1. Memory fragmentation is acceptable:

```bash
redis-cli INFO memory | grep mem_fragmentation_ratio
```

Expect a value between 1.0 and 1.5.

## Prevention

1. **Ban dangerous commands in production** — Use ACLs or `rename-command` to disable `KEYS`, `FLUSHALL`, `FLUSHDB`, and `DEBUG` in production environments.

2. **Use SCAN instead of KEYS** — All keyspace iteration must use cursor-based `SCAN` commands to avoid blocking the event loop.

3. **Set slowlog-log-slower-than** — Configure to 10000 microseconds (10 ms) and monitor SLOWLOG regularly. Lower to 1000 microseconds during latency investigations.

4. **Monitor latency continuously** — Use `redis-cli --latency` or the `LATENCY` subsystem. Alert when p99 latency exceeds 5 ms.

5. **Disable Transparent Huge Pages** — THP causes latency spikes during fork operations. Disable with `echo never > /sys/kernel/mm/transparent_hugepage/enabled` and persist in boot scripts.

6. **Set maxmemory with headroom** — Configure `maxmemory` to leave 20-30% of physical RAM for the OS, fork operations, and output buffers. Never let Redis use swap.

7. **Enable active defragmentation** — On Redis 4.0+, enable `activedefrag` to reduce memory fragmentation without restarts.

8. **Break large keys** — Design data models to avoid keys larger than 10 KB (strings) or 1000 elements (collections). Use key bucketing for large datasets.

9. **Use pipelining and connection pooling** — Pipeline multiple commands in a single round trip to reduce network overhead. Always use connection pooling.

10. **Schedule persistence during off-peak hours** — Configure RDB snapshot intervals to avoid peak traffic periods. Use `appendfsync everysec` for AOF as a balance between durability and latency.

## Sources

- [Redis Documentation: SLOWLOG](https://redis.io/docs/latest/commands/slowlog-get/) — Official reference for the slow query log used to identify expensive commands.
- [Redis Documentation: LATENCY subsystem](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/latency/) — Official reference for the latency monitoring framework, LATENCY DOCTOR, and event tracking.
- [Redis Documentation: Persistence (RDB and AOF)](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/) — Official reference for RDB snapshots, AOF, and their performance implications.
- [Redis Documentation: Memory Optimization](https://redis.io/docs/latest/operate/oss_and_stack/management/optimization/memory-optimization/) — Official reference for memory management, fragmentation, and large key handling.
