---
id: redis-connection-limits
title: "Redis Connection Limit Reached"
domain: database
service: redis
symptom_class:
  - connection_refused
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - redis
  - connections
  - maxclients
  - connection-pooling
  - client-list
difficulty: intermediate
---

# Redis Connection Limit Reached

## Problem Definition

Applies to Redis 6.0 and later (compatible with earlier versions; command syntax is stable). Requires access to the Redis CLI (`redis-cli`) or any Redis client library with administrative command privileges. The `ACL` system (Redis 6+) may restrict access to `CLIENT LIST` and `CONFIG` commands; ensure the diagnostic user has appropriate permissions.

When the number of connected clients reaches the `maxclients` limit (default: 10000), Redis refuses new connections. Clients receive a connection error:

```text
ERR max number of clients reached
```

Applications experience connection timeouts, elevated error rates on Redis-dependent endpoints, cache misses that cascade to the database backend, and session or rate-limiting failures if Redis is used for those purposes. The operating system's file descriptor limit may also cap the effective `maxclients` value below the configured setting, since Redis reserves 32 file descriptors for internal use.

Common causes include application-side connection leaks (connections opened but never closed or returned to the pool), oversized or misconfigured connection pools across many application instances, pub/sub subscribers that accumulate without cleanup, monitoring or sidekick processes that open connections without pooling, and slow commands blocking the event loop causing client connections to pile up while waiting for responses.

## Diagnostic Steps

### Step 1. Confirm connection count against the limit

Check the current number of connected clients and the configured maximum.

```bash
redis-cli INFO clients
```

Key fields in the output:

- `connected_clients` — current number of client connections.
- `maxclients` — configured maximum (from `CONFIG GET maxclients`).
- `blocked_clients` — clients blocked on `BLPOP`/`BRPOP` or similar blocking commands.
- `rejected_connections` — cumulative count of connections rejected due to `maxclients`. If this is non-zero, the limit has been hit.

If `connected_clients` is at or near `maxclients`, the server is at capacity.

### Step 2. List connected clients and identify top consumers

Examine the client list to determine which applications, hosts, or users hold the most connections.

```bash
redis-cli CLIENT LIST | awk -F'[ =]' '{for(i=1;i<=NF;i++) if($i=="addr") addr=$(i+1); if($i=="name") name=$(i+1); if($i=="age") age=$(i+1); if($i=="idle") idle=$(i+1); if($i=="cmd") cmd=$(i+1)} {print addr, name, age, idle, cmd}'
```

For a summary by client IP address:

```bash
redis-cli CLIENT LIST | grep -oP 'addr=\K[^:]+' | sort | uniq -c | sort -rn | head -20
```

Expected output: connections distributed across known application hosts. A single host holding hundreds or thousands of connections indicates a leak or misconfigured pool.

### Step 3. Identify idle connections

Find connections that have been idle for a long time, suggesting they are leaked or abandoned.

```bash
redis-cli CLIENT LIST | awk -F'[ =]' '{for(i=1;i<=NF;i++) if($i=="idle") idle=$(i+1); if($i=="addr") addr=$(i+1)} idle+0 > 300 {print addr, "idle="idle"s"}' | sort -t= -k2 -rn | head -20
```

Connections idle for more than 300 seconds (5 minutes) without an active purpose are candidates for cleanup. In a healthy system, pooled connections should have recent activity.

### Step 4. Check for pub/sub subscribers consuming connections

Pub/sub subscribers hold persistent connections. If not managed, they can accumulate.

```bash
redis-cli PUBSUB CHANNELS '*'
redis-cli PUBSUB NUMSUB
```

Also check the INFO output for pub/sub metrics:

```bash
redis-cli INFO stats | grep pubsub
```

A high `pubsub_channels` or `pubsub_patterns` count with many subscribers indicates connection consumption by the messaging subsystem.

### Step 5. Verify the operating system file descriptor limit

Redis's effective `maxclients` is capped by the OS file descriptor limit minus 32.

```bash
# Check the Redis process file descriptor limit
cat /proc/$(pgrep -f redis-server)/limits | grep "Max open files"

# Check current file descriptor usage
ls /proc/$(pgrep -f redis-server)/fd | wc -l
```

If the file descriptor limit is lower than `maxclients + 32`, the OS is the actual bottleneck, not the Redis configuration.

### Step 6. Check for slow commands blocking the event loop

Slow commands can cause connections to queue up while waiting for the single-threaded Redis event loop.

```bash
redis-cli SLOWLOG GET 20
```

If many slow commands appear, each one blocks all other clients during execution, causing connections to accumulate as clients wait for responses.

## Mitigation

### Option 1. Kill idle client connections

**Risk**: Low. Disconnects clients that have been idle beyond a threshold. Applications with proper connection pooling will reconnect automatically.

**Command**:

```bash
# Kill all clients idle for more than 300 seconds
redis-cli CLIENT NO-EVICT ON
redis-cli CLIENT LIST | awk -F'[ =]' '{id=""; idle=0; for(i=1;i<=NF;i++) {if($i=="id") id=$(i+1); if($i=="idle") idle=$(i+1)}} idle+0 > 300 {print id}' | while read id; do redis-cli CLIENT KILL ID "$id"; done
```

**Verify**:

```bash
redis-cli INFO clients | grep connected_clients
```

**Duration**: Immediate. Connections are freed within seconds.

### Option 2. Set a client idle timeout

**Risk**: Low. Automatically disconnects clients that are idle beyond the specified number of seconds. Applications must handle reconnection.

**Command**:

```bash
redis-cli CONFIG SET timeout 300
```

**Verify**:

```bash
redis-cli CONFIG GET timeout
```

**Duration**: Immediate. Existing idle connections are closed at the next event loop cycle after exceeding the timeout.

### Option 3. Increase maxclients

**Risk**: Low-Medium. Allows more connections but increases memory usage (each connection uses approximately 10-20 KB of memory). Also requires the OS file descriptor limit to be raised.

**Command**:

```bash
# Increase Redis maxclients
redis-cli CONFIG SET maxclients 20000

# Increase OS file descriptor limit (must also update /etc/security/limits.conf for persistence)
ulimit -n 65536
```

**Verify**:

```bash
redis-cli CONFIG GET maxclients
redis-cli INFO clients | grep connected_clients
```

**Duration**: Immediate. No restart required for `CONFIG SET`.

### Option 4. Disconnect all connections from a specific misbehaving host

**Risk**: Medium. All connections from that host are terminated. The application on that host will experience errors until it reconnects.

**Command**:

```bash
# Replace 10.0.1.50 with the offending client IP
redis-cli CLIENT LIST | grep 'addr=10.0.1.50:' | grep -oP 'id=\K[0-9]+' | while read id; do redis-cli CLIENT KILL ID "$id"; done
```

**Verify**:

```bash
redis-cli CLIENT LIST | grep -c 'addr=10.0.1.50:'
```

**Duration**: Immediate.

## Root Cause Resolution

**If** application connection pools are oversized → right-size the pool. Each application instance should use a pool size that, multiplied by the number of instances, stays well below `maxclients`. A typical pool size is 5-20 connections per instance:

```python
# Python redis-py example
import redis
pool = redis.ConnectionPool(
    host='redis-host',
    port=6379,
    max_connections=10,  # per instance
    socket_timeout=5,
    socket_connect_timeout=5,
)
client = redis.Redis(connection_pool=pool)
```

**If** connection leaks exist in application code → audit connection handling. Ensure connections are returned to the pool after use. In languages without automatic resource management, use `try/finally`:

```python
conn = pool.get_connection('GET')
try:
    conn.send_command('GET', 'key')
    result = conn.read_response()
finally:
    pool.release(conn)
```

**If** the OS file descriptor limit is the bottleneck → increase it permanently:

```bash
# /etc/security/limits.conf
redis soft nofile 65536
redis hard nofile 65536
```

```bash
# /etc/systemd/system/redis.service.d/override.conf
[Service]
LimitNOFILE=65536
```

Then restart Redis.

**If** pub/sub subscribers accumulate without cleanup → implement subscriber lifecycle management. Ensure subscribers unsubscribe when they are no longer needed, and set `client-output-buffer-limit` for pub/sub clients:

```bash
redis-cli CONFIG SET client-output-buffer-limit "pubsub 256mb 128mb 60"
```

This disconnects pub/sub clients whose output buffer exceeds 256 MB hard limit or stays above 128 MB for 60 seconds.

**If** monitoring or sidecar processes open many connections → consolidate monitoring into a single connection per tool, or use a Redis proxy (such as Twemproxy or Redis Cluster Proxy) to multiplex connections.

## Verification

After applying fixes, confirm the connection issue is resolved.

1. Connection count is healthy:

```bash
redis-cli INFO clients | grep -E 'connected_clients|rejected_connections|maxclients'
```

Expect `connected_clients` well below `maxclients` and `rejected_connections` not increasing.

1. Applications can connect:

```bash
redis-cli PING
```

Expect `PONG`.

1. No excessive idle connections:

```bash
redis-cli CLIENT LIST | awk -F'[ =]' '{for(i=1;i<=NF;i++) if($i=="idle") idle=$(i+1)} idle+0 > 300' | wc -l
```

Expect a low count (near 0).

1. Client timeout is configured:

```bash
redis-cli CONFIG GET timeout
```

1. File descriptor headroom is adequate:

```bash
ls /proc/$(pgrep -f redis-server)/fd | wc -l
cat /proc/$(pgrep -f redis-server)/limits | grep "Max open files"
```

Expect current fd count well below the limit.

## Prevention

1. **Set a client idle timeout** — Configure `timeout` to 300 seconds to automatically disconnect idle clients. This is the single most effective protection against connection leaks.

2. **Right-size connection pools** — Calculate the maximum total connections as `pool_size * num_instances` across all applications. Keep this total below 80% of `maxclients`.

3. **Monitor connected_clients** — Alert when `connected_clients` exceeds 80% of `maxclients`. Use the `INFO clients` output as the metric source.

4. **Monitor rejected_connections** — Alert on any increase in `rejected_connections` in `INFO stats`. A non-zero delta means the limit has been reached.

5. **Set OS file descriptor limits** — Configure the Redis process to have at least `maxclients + 32` file descriptors. Set this in systemd unit overrides and `/etc/security/limits.conf`.

6. **Use connection pooling in all clients** — Never create a new connection per request. Use a connection pool library appropriate for your language (redis-py `ConnectionPool`, Jedis `JedisPool`, ioredis built-in pooling).

7. **Audit pub/sub subscriber lifecycle** — Ensure subscribers are cleaned up when no longer needed. Set `client-output-buffer-limit` for pub/sub clients to automatically disconnect slow consumers.

8. **Persist configuration changes** — After using `CONFIG SET`, write changes to the configuration file with `CONFIG REWRITE` to ensure they survive a restart.

## Sources

- [Redis Documentation: CLIENT LIST](https://redis.io/docs/latest/commands/client-list/) — Official reference for inspecting connected clients, their state, and connection metadata.
- [Redis Documentation: Server Configuration (maxclients)](https://redis.io/docs/latest/operate/oss_and_stack/management/config/) — Official reference for `maxclients`, `timeout`, and connection-related configuration.
- [Redis Documentation: CLIENT KILL](https://redis.io/docs/latest/commands/client-kill/) — Official reference for terminating client connections by ID, address, or filter.
- [Redis Documentation: Pub/Sub](https://redis.io/docs/latest/develop/interact/pubsub/) — Official reference for pub/sub connection behavior and subscriber management.
