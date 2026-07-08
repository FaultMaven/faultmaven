---
id: "redis-connection-limits"
title: "Redis Connection Limit Reached"
domain: database
service: redis
symptom_class: [connection_refused]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags: [redis, maxclients, connection-pooling, client-list, file-descriptors, pubsub]
difficulty: intermediate
---

## Symptom Recognition

Clients receive the error `ERR max number of clients reached` when attempting to connect. Applications dependent on Redis show elevated error rates, cache misses, and request failures. Dashboards show `rejected_connections` counter increasing in `INFO stats`. Connection timeouts surface in application logs before the explicit Redis error message appears. Load balancers may report backend unhealthy if Redis is used for session validation. In Kubernetes environments, liveness probes that connect to Redis begin failing.

## Applicability

Applies to Redis 6.0 and later (command syntax is stable across versions). Requires `redis-cli` access with administrative privileges; the ACL system (Redis 6+) may restrict `CLIENT LIST`, `CLIENT KILL`, and `CONFIG SET` — ensure the diagnostic user holds the `@admin` ACL category. Operating system access (or Kubernetes `exec`) is required for Steps 5 and 6 to inspect file descriptors. Cloud-managed Redis (ElastiCache, Cloud Memorystore) may not expose `CONFIG SET` or process-level `/proc` paths — use the cloud console for parameter changes.

## Diagnostic Steps

### Step 1: Confirm connection count against the limit

```bash
redis-cli INFO clients && redis-cli INFO stats | grep rejected_connections
```

Expected output: `connected_clients` value, `maxclients` value, and `rejected_connections` count. If `connected_clients` is at or near `maxclients` and `rejected_connections` is non-zero, the limit has been hit.

### Step 2: Identify top connection holders by source IP

```bash
redis-cli CLIENT LIST | grep -oP 'addr=\K[^:]+' | sort | uniq -c | sort -rn | head -20
```

Expected output: a ranked list of client IPs with connection counts. A single host holding hundreds or thousands of connections indicates a connection leak or oversized pool.

### Step 3: Find long-idle connections

```bash
redis-cli CLIENT LIST | awk -F'[ =]' '{id=""; idle=0; addr=""; for(i=1;i<=NF;i++){if($i=="id")id=$(i+1); if($i=="idle")idle=$(i+1); if($i=="addr")addr=$(i+1)}} idle+0>300{print idle, addr, id}' | sort -rn | head -20
```

Expected output: connections with idle time exceeding 300 seconds. In a healthy system with active pooling, long-idle connections should be near zero.

### Step 4: Count pub/sub connections consuming slots

```bash
redis-cli INFO clients | grep pubsub_clients
redis-cli PUBSUB CHANNELS '*' | wc -l
redis-cli PUBSUB NUMSUB | awk 'NR%2==0{sum+=$1}END{print "total_subscribers="sum}'
```

Expected output: `pubsub_clients` count from INFO, total active channels, and total subscriber count. A `pubsub_clients` value disproportionate to expected application subscribers indicates subscriber leaks.

### Step 5: Check OS file descriptor limit against maxclients

```bash
REDIS_PID=$(pgrep -f redis-server | head -1)
cat /proc/${REDIS_PID}/limits | grep "Max open files"
ls /proc/${REDIS_PID}/fd | wc -l
redis-cli CONFIG GET maxclients
```

Expected output: the hard and soft fd limits, current fd usage, and configured `maxclients`. Effective maxclients is `min(configured_maxclients, os_fd_limit - 32)`. If the OS limit is the binding constraint, the configured `maxclients` value is never reachable.

### Step 6: Check for slow commands blocking the event loop

```bash
redis-cli SLOWLOG GET 20
redis-cli SLOWLOG LEN
```

Expected output: slow log entries with execution times in microseconds. Commands exceeding 10,000 µs (10 ms) block all waiting clients during execution, causing connections to queue and accumulate.

## Causes

### Cause A: Connection pool oversized relative to maxclients

**Statement:** Application connection pools are configured with a per-instance size that, multiplied by the number of running instances, exceeds the Redis `maxclients` limit.

**Chain:**
- root: aggregate pool capacity (`pool_size × instance_count`) is configured above `maxclients`
- s1: many instances each open and hold their full pool of persistent TCP connections
- s2: total connected clients approaches or reaches `maxclients` (default 10000)
- D: Redis rejects new connections with `ERR max number of clients reached` (Symptom)

**Indicators:**
- root: [Step 2] multiple source IPs each holding large connection counts (50+) with no single dominant host
- s2: [Step 1] `connected_clients` at or near `maxclients` with `rejected_connections` incrementing

**Interventions:**
- **remediation** (root): right-size pools so `pool_size × instance_count < 0.8 × maxclients`, then roll out.

  ```python
  # Python redis-py: target pool_size * instance_count < 0.8 * maxclients
  import redis
  pool = redis.ConnectionPool(
      host='redis-host', port=6379,
      max_connections=10,        # per instance; adjust based on instance count
      socket_timeout=5,
      socket_connect_timeout=5,
  )
  client = redis.Redis(connection_pool=pool)
  ```

  **Verification:** after pool resize and rolling redeploy, re-run Step 1; `connected_clients` is well below 80% of `maxclients` and `rejected_connections` stops incrementing.
- **mitigation** (s2): temporarily raise `maxclients` to stop rejections while pools are corrected.

  ```bash
  # Temporarily raise maxclients to stop rejections while pool sizes are corrected
  redis-cli CONFIG SET maxclients 20000
  redis-cli CONFIG REWRITE
  ```

  **Risk:** reducing pool size on live instances later causes brief connection churn as pools shrink. **Duration:** hours to days; permanent fix is pool resize in application configuration. **Verification:** re-run Step 1; `rejected_connections` stops incrementing.

### Cause B: Connection leak — connections opened but never returned to pool

**Statement:** Application code opens Redis connections without returning them to the pool, causing connection count to grow monotonically until maxclients is reached.

**Chain:**
- root: code checks out connections via explicit `get_connection()` without a paired release (no context manager / `finally`)
- s1: leaked connections held outside the pool still occupy file descriptors on the Redis server
- s2: a single leaking host accumulates hundreds to thousands of long-idle connections over time
- s3: cumulative leak fills all available connection slots up to `maxclients`
- D: Redis rejects new connections with `ERR max number of clients reached` (Symptom)

**Indicators:**
- s2: [Step 2] a single host holding a very large count (hundreds to thousands) of connections
- s2: [Step 3] many connections from that host with idle times in the thousands of seconds

**Interventions:**
- **remediation** (root): guarantee connection return via context managers, or release in `finally`.

  ```python
  # Use context manager to guarantee connection return
  with client.pipeline() as pipe:
      pipe.get('key')
      result = pipe.execute()

  # For direct pool usage, always release in finally
  conn = pool.get_connection('GET')
  try:
      conn.send_command('GET', 'key')
      result = conn.read_response()
  finally:
      pool.release(conn)
  ```

  **Verification:** restart the leaking process and monitor `redis-cli CLIENT LIST | grep -c 'addr=<host_ip>:'` over 30 minutes; count stays stable (a growing count confirms the leak persists).
- **mitigation** (s2): kill the leaking host's accumulated connections to reclaim slots immediately.

  ```bash
  # Kill all connections from the leaking host (replace 10.0.1.50 with actual IP)
  redis-cli CLIENT KILL ADDR 10.0.1.50:0 SKIPME no
  # Or kill all connections idle more than 1 hour (Redis 7.4+)
  redis-cli CLIENT KILL MAXAGE 3600
  ```

  **Risk:** low — causes brief reconnection errors on the leaking host only. **Duration:** immediate; connections freed within one event loop tick, but recurs until the application is fixed. **Verification:** re-run Step 2; that host's connection count drops.

### Cause C: Pub/Sub subscriber accumulation without lifecycle cleanup

**Statement:** Pub/sub subscriber connections accumulate without unsubscribing, consuming connection slots that are never released.

**Chain:**
- root: the application creates new subscribers on events without tearing down old ones (no `UNSUBSCRIBE`)
- s1: each `SUBSCRIBE`/`PSUBSCRIBE` holds a dedicated long-lived connection in subscriber mode
- s2: subscriber count grows continuously; these connections persist until killed or the client process exits
- s3: accumulated pub/sub connections consume connection slots up to `maxclients`
- D: Redis rejects new connections with `ERR max number of clients reached` (Symptom)

**Indicators:**
- s2: [Step 4] `pubsub_clients` value in INFO clients is disproportionately high relative to expected subscriber count
- s2: [Step 4] `PUBSUB NUMSUB` shows subscriber counts far exceeding known application instances

**Interventions:**
- **remediation** (root): implement subscriber lifecycle (always `UNSUBSCRIBE` before destroy) and persist buffer limits.

  ```bash
  # Implement subscriber lifecycle in application:
  # - Always UNSUBSCRIBE before destroying subscriber objects
  # - Use connection pools with max_connections limits for pubsub clients
  # Set buffer limits persistently in redis.conf:
  redis-cli CONFIG SET client-output-buffer-limit "pubsub 256mb 128mb 60"
  redis-cli CONFIG REWRITE
  ```

  **Verification:** re-run Step 4; `pubsub_clients` reflects only expected active subscribers (typically 1–2 per application service type).
- **mitigation** (s2): kill accumulated pub/sub connections and install an auto-disconnect buffer limit.

  ```bash
  # Kill all pubsub-type connections
  redis-cli CLIENT KILL TYPE pubsub SKIPME no
  # Set output buffer limit to auto-disconnect slow subscribers (survives restart)
  redis-cli CONFIG SET client-output-buffer-limit "pubsub 256mb 128mb 60"
  redis-cli CONFIG REWRITE
  ```

  **Risk:** medium — killing pubsub clients terminates all in-flight message deliveries to those subscribers. **Duration:** immediate kill; buffer limit protects against future accumulation. **Verification:** re-run Step 4; `pubsub_clients` drops to expected levels.

### Cause D: OS file descriptor limit lower than maxclients

**Statement:** The operating system file descriptor limit for the Redis process is lower than the configured `maxclients` value, making the OS limit the effective connection ceiling.

**Chain:**
- root: Redis was started without raising `LimitNOFILE` (systemd) or `ulimit -n` (init script)
- s1: the default OS fd limit (commonly 1024 or 4096) governs the process instead of `maxclients`
- s2: effective maximum is `min(maxclients, ulimit_nofile - 32)`, far below the configured value
- s3: connections fill the OS-imposed ceiling regardless of the `maxclients` setting
- D: Redis rejects new connections with `ERR max number of clients reached` (Symptom)

**Indicators:**
- s2: [Step 5] soft or hard fd limit in `/proc/<pid>/limits` is less than `maxclients + 32`
- s3: [Step 5] current fd usage is at or near the OS limit

**Interventions:**
- **remediation** (root): raise the process fd limit persistently (systemd override + limits.conf), then restart.

  ```bash
  # Systemd unit override (persistent)
  sudo mkdir -p /etc/systemd/system/redis.service.d
  sudo tee /etc/systemd/system/redis.service.d/override.conf <<'EOF'
  [Service]
  LimitNOFILE=65536
  EOF

  # /etc/security/limits.conf (for non-systemd or SSH sessions)
  echo "redis soft nofile 65536" | sudo tee -a /etc/security/limits.conf
  echo "redis hard nofile 65536" | sudo tee -a /etc/security/limits.conf

  sudo systemctl daemon-reload && sudo systemctl restart redis
  ```

  **Verification:** re-run Step 5; `/proc/<pid>/limits` shows 65536 and `CONFIG GET maxclients` matches the configured value without OS-imposed reduction. Rollback: remove the override file and restart.
- **mitigation** (s2): raise the live fd limit for the running process to buy time before a restart.

  ```bash
  # Temporary raise for current session (does not survive restart)
  sudo prlimit --pid $(pgrep -f redis-server) --nofile=65536:65536
  ```

  **Risk:** low — only takes effect after Redis restart; no impact on running connections. **Duration:** until the Redis process restarts; must be made permanent. **Verification:** re-run Step 5; the soft/hard fd limit reflects the raised value.

### Cause E: Monitoring or sidecar processes opening unshared connections

**Statement:** Monitoring agents, health-check scripts, or sidecar processes open individual Redis connections on each invocation without connection pooling or reuse, consuming connection slots at high frequency.

**Chain:**
- root: monitoring/health-check/sidecar tooling opens a new connection per invocation without pooling or reuse
- s1: at high polling frequency (every 5–10s per host) across many hosts these short-lived connections multiply
- s2: under load or when Redis is slow, connections accumulate faster than they tear down
- s3: the aggregate of many low-count sources consumes connection slots up to `maxclients`
- D: Redis rejects new connections with `ERR max number of clients reached` (Symptom)

**Indicators:**
- s1: [Step 2] many different source IPs each with low connection counts (1–3) but collectively summing to a large total
- s2: [Step 3] short-lived connections from non-application hosts (monitoring CIDR ranges, load balancer IPs)

**Interventions:**
- **remediation** (root): consolidate monitoring onto a single persistent pooled connection per tool, plus an idle timeout.

  ```bash
  # Use a single persistent monitoring connection per tool
  # Example: configure prometheus redis_exporter to use connection pooling
  # redis_exporter --redis.addr=redis://localhost:6379 --redis.pool-size=1

  # Set idle timeout so orphaned monitor connections self-clean
  redis-cli CONFIG SET timeout 60
  redis-cli CONFIG REWRITE
  ```

  **Verification:** re-run Step 2; unique source IPs (`grep -oP 'addr=\K[^:]+'| sort -u | wc -l`) decrease as agents consolidate and `rejected_connections` stops incrementing.
- **mitigation** (s3): raise `maxclients` to provide headroom while monitoring is consolidated.

  ```bash
  redis-cli CONFIG SET maxclients 20000
  redis-cli CONFIG REWRITE
  ```

  **Risk:** low — raising maxclients provides headroom while monitoring is consolidated. **Duration:** permanent until monitoring architecture is corrected. **Verification:** re-run Step 1; `rejected_connections` stops incrementing.

### Cause Z: Unidentified connection saturation cause

**Statement:** Connection slots are exhausted but the source does not match known patterns of pool misconfiguration, leaks, pub/sub accumulation, fd limits, or monitoring agents.

**Chain:**
- root: connections saturate from a source not matched by Causes A–E (e.g. cluster bus connections, replica connections under high replication load, or a third-party library holding undocumented persistent connections)
- D: Redis rejects new connections with `ERR max number of clients reached` (Symptom)

**Indicators:**
- root: [Default] Steps 1–6 completed without matching any of Causes A–E

**Interventions:**
- **mitigation** (D): raise `maxclients` for headroom, capture a full client-list snapshot, and escalate to the SME.

  ```bash
  redis-cli CONFIG SET maxclients 20000
  redis-cli CONFIG REWRITE
  # Capture full client list snapshot for offline analysis
  redis-cli CLIENT LIST > /tmp/redis-client-list-$(date +%s).txt
  ```

  **Risk:** medium — temporarily raising maxclients buys time for deeper investigation without resolving root cause. **Duration:** short-term; escalate to the application team with the captured `/tmp/redis-client-list-*.txt` snapshot for analysis of connection origins. **Verification:** escalation acknowledged and owning team engaged; re-run Step 1, `rejected_connections` not incrementing after the raise confirms headroom restored.

## Prevention

1. **Set a client idle timeout** — Configure `timeout 300` in `redis.conf` to auto-disconnect idle clients. This is the single most effective protection against connection leaks. Use `redis-cli CONFIG SET timeout 300 && redis-cli CONFIG REWRITE`.

2. **Right-size connection pools** — Target `pool_size × instance_count < 0.8 × maxclients`. For a Redis with `maxclients 10000` and 50 application instances, each pool should be at most 160 connections.

3. **Alert on connected_clients ratio** — Fire a warning alert when `connected_clients / maxclients > 0.8` and a critical alert at `> 0.95`. Source: `redis-cli INFO clients`.

4. **Alert on rejected_connections delta** — Any non-zero delta in `rejected_connections` between scrape intervals is a critical event. The counter is monotonically increasing; a delta means connections are actively being refused.

5. **Set OS file descriptor limits at deploy time** — Include `LimitNOFILE=65536` in the Redis systemd unit and verify with `redis-cli INFO server | grep maxclients`. Automate this check in infrastructure-as-code.

6. **Set pub/sub output buffer limits** — Configure `client-output-buffer-limit pubsub 256mb 128mb 60` to automatically disconnect slow or leaking pub/sub consumers.

7. **Persist all CONFIG SET changes** — Always follow `CONFIG SET` with `CONFIG REWRITE` so changes survive a restart.

8. **Use `tcp-keepalive 300`** — Enables OS-level keepalive probes so the kernel reclaims connections from crashed clients without waiting for the application idle timeout.

## Sources

- [Redis CLIENT LIST command](https://redis.io/docs/latest/commands/client-list/) — Official reference for client connection fields (addr, fd, id, age, idle, flags, sub, psub, omem, tot-mem). Priority 1.
- [Redis CLIENT KILL command](https://redis.io/docs/latest/commands/client-kill/) — Official reference for terminating connections by ID, ADDR, TYPE, USER, and MAXAGE filter. Priority 1.
- [Redis INFO command](https://redis.io/docs/latest/commands/info/) — Official reference for connected_clients, maxclients, rejected_connections, pubsub_clients, and blocked_clients fields. Priority 1.
- [Redis troubleshooting guide](https://redis.io/docs/latest/operate/oss_and_stack/management/troubleshooting/) — Official Redis operations troubleshooting landing page. Priority 1.
- [Redis server configuration (redis.conf)](https://redis.io/docs/latest/operate/oss_and_stack/management/config/) — Official reference for maxclients, timeout, tcp-keepalive, client-output-buffer-limit, and CONFIG REWRITE. Priority 1.
