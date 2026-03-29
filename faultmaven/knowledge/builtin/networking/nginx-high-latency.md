---
id: nginx-high-latency
title: "NGINX Request Latency Spikes — Diagnosis and Resolution"
domain: networking
service: nginx
symptom_class:
  - latency
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: "kb-researcher"
status: draft
tags:
  - nginx
  - latency
  - slow-requests
  - upstream
  - reverse-proxy
  - performance
  - buffering
difficulty: intermediate
---

## Problem Definition

This runbook covers NGINX request latency spikes when NGINX operates as a reverse proxy or load balancer. It applies to NGINX OSS 1.18+ and NGINX Plus R25+ on Linux. Diagnosis requires read access to NGINX access and error logs, NGINX configuration files, and the `stub_status` module enabled for connection monitoring. For system-level analysis, access to `pidstat`, `iostat`, and `/proc` filesystem is needed. Upstream timing variables (`$upstream_response_time`, `$upstream_connect_time`, `$upstream_header_time`) must be added to the access log format if not already present.

NGINX request latency spikes occur when the total time from client request arrival to response delivery increases beyond normal operating thresholds. This manifests as slow page loads, API timeout errors at the caller, and degraded user experience. The `$request_time` variable in access logs captures the full request lifecycle from the first byte read from the client to the last byte sent. Common client-side symptoms include HTTP 504 errors from downstream load balancers, application-level timeout exceptions, and increased P95/P99 latency in monitoring dashboards.

Latency in an NGINX reverse-proxy deployment can originate from multiple layers:

- **Upstream slow response** — the backend application takes too long to process the request and return headers or body. This is the most common cause, measured by `$upstream_response_time`.
- **Worker connection exhaustion** — all NGINX worker connections are occupied, causing new requests to queue. Happens when `worker_connections` is too low or there are too many concurrent long-lived connections.
- **Proxy buffering contention** — when `proxy_buffering` is on (default), NGINX buffers the upstream response. If buffers are undersized, NGINX spills to disk, adding I/O latency.
- **Upstream connection establishment delay** — high `$upstream_connect_time` indicates TCP handshake delays, DNS resolution overhead, or upstream connection pool exhaustion.
- **Keepalive misuse** — without keepalive to upstream, every request pays the cost of a new TCP (and potentially TLS) handshake.
- **SSL/TLS handshake overhead** — expensive asymmetric crypto on every new connection when session caching or TLS 1.3 0-RTT is not configured.
- **Client slow read (client body buffering)** — a slow client sending a large POST body holds a worker connection open while NGINX reads the body.
- **Disk I/O from large response buffering** — when proxy buffers overflow, NGINX writes to `proxy_temp_path`, and slow disk I/O delays response delivery.
- **Logging I/O** — synchronous access log writes to a slow disk can block worker threads.

## Diagnostic Steps

### Step 1: Enable upstream timing in access logs

Checks the current log format and adds upstream timing variables if missing. Without these variables, determining where latency originates is guesswork.

```bash
# Check current log format
nginx -T 2>/dev/null | grep -A 5 "log_format"
```

If the log format does not include upstream timing, add a new format:

```nginx
# In nginx.conf or a conf.d file:
log_format timing '$remote_addr - $remote_user [$time_local] '
    '"$request" $status $body_bytes_sent '
    'rt=$request_time uct=$upstream_connect_time '
    'uht=$upstream_header_time urt=$upstream_response_time '
    'ua=$upstream_addr';

access_log /var/log/nginx/access.log timing;
```

```bash
nginx -t && nginx -s reload
```

Key timing variables:

| Variable | Meaning |
|---|---|
| `$request_time` | Total time from first client byte to last byte sent to client (full latency) |
| `$upstream_connect_time` | Time to establish TCP connection to upstream |
| `$upstream_header_time` | Time from connection to receiving first header byte from upstream |
| `$upstream_response_time` | Time from connection to receiving last byte from upstream |

After reload, new log entries will contain the timing fields needed for the remaining diagnostic steps.

### Step 2: Analyze access logs for latency patterns

Checks access logs to identify slow requests, their endpoints, and the time distribution.

```bash
# Find the slowest requests in the last 1000 lines
tail -1000 /var/log/nginx/access.log | awk '{print $NF, $7}' | sort -rn | head -20

# If using the timing log format, extract request_time and upstream_response_time
grep "rt=" /var/log/nginx/access.log | tail -500 | \
    sed 's/.*rt=\([^ ]*\).*urt=\([^ ]*\).*/rt=\1 urt=\2/' | \
    awk -F'[= ]' '{if ($2 > 2.0) print $0}' | head -20

# Count requests above latency thresholds
awk '/rt=/{match($0, /rt=([0-9.]+)/, a); if(a[1]>1.0) slow++; total++} END{printf "Slow (>1s): %d/%d (%.1f%%)\n", slow, total, slow/total*100}' /var/log/nginx/access.log
```

Expected output: a list of slow requests with their timing values. If a small number of endpoints dominate the slow requests, the problem is likely upstream-specific. If latency is spread across all endpoints, the problem is NGINX-layer or network-layer.

### Step 3: Determine if latency is upstream or NGINX

Compares `$request_time` to `$upstream_response_time` to determine whether latency originates from the upstream application or from NGINX itself.

```bash
# Compare request_time vs upstream_response_time
# If upstream_response_time ~ request_time -> upstream is the bottleneck
# If request_time >> upstream_response_time -> NGINX processing or client download is slow
grep "rt=" /var/log/nginx/access.log | tail -100 | \
    sed 's/.*rt=\([^ ]*\).*urt=\([^ ]*\).*/\1 \2/' | \
    awk '{diff=$1-$2; if(diff > 0.5) print "NGINX overhead:", diff, "rt:", $1, "urt:", $2}'
```

Expected output: minimal difference between `rt` and `urt` in most cases. If `urt` accounts for most of `rt`, the upstream is slow and NGINX is just waiting. If there is significant NGINX overhead (diff > 0.5s), investigate worker connections, proxy buffering, or client-side slow reads.

### Step 4: Check worker connection saturation

Checks whether NGINX has run out of available worker connections, causing new requests to queue.

```bash
# Current active connections (requires stub_status module)
curl -s http://localhost/nginx_status
# Output: Active connections: N
#         server accepts handled requests
#         Reading: N Writing: N Waiting: N

# Check worker_connections setting
nginx -T 2>/dev/null | grep "worker_connections"

# Check worker_processes
nginx -T 2>/dev/null | grep "worker_processes"

# Maximum simultaneous connections = worker_processes x worker_connections
# If Active connections approaches this limit, connections queue

# Check OS-level connection counts to NGINX
ss -s
ss -tn state established | grep ":80\|:443" | wc -l
```

Expected output: active connections well below `worker_processes * worker_connections`. A high `Waiting` count in stub_status is normal (idle keepalive connections). A high `Reading` or `Writing` count relative to total active connections indicates the workers are busy processing, which means capacity is tight.

### Step 5: Check proxy buffer configuration

Checks whether NGINX is spilling proxy response buffers to disk, adding I/O latency to response delivery.

```bash
# Current buffer settings
nginx -T 2>/dev/null | grep -E "proxy_buffer|proxy_busy_buffers|proxy_temp"

# Check if NGINX is writing to temp files (buffer overflow indicator)
ls -la /var/cache/nginx/proxy_temp/ 2>/dev/null || ls -la /tmp/nginx/ 2>/dev/null

# Monitor disk I/O on the proxy_temp partition
iostat -x 1 5 2>/dev/null | grep -A 1 "Device"
```

Expected output: no files in `proxy_temp` during normal operation. If files are present, the proxy buffers are too small for the upstream response sizes and NGINX is writing to disk. High `%util` or `await` values from `iostat` confirm disk I/O is adding latency.

### Step 6: Check upstream keepalive configuration

Checks whether keepalive connections to upstream are configured. Without keepalive, every request incurs TCP and potentially TLS handshake overhead.

```bash
# Check if keepalive is configured for upstream connections
nginx -T 2>/dev/null | grep -B 10 -A 10 "keepalive"

# Check proxy_http_version (must be 1.1 for keepalive to upstream)
nginx -T 2>/dev/null | grep "proxy_http_version"

# Count active connections to upstream — high churn without keepalive = latency
ss -tn state time-wait | grep ":8080" | wc -l
```

Expected output: `keepalive` directive present in the upstream block, `proxy_http_version 1.1` set in the location block, and `proxy_set_header Connection ""` clearing the Connection header. A high count of TIME_WAIT connections to the upstream port indicates connections are being torn down and recreated per request, adding handshake overhead.

### Step 7: Check for SSL/TLS overhead

Checks whether TLS handshake time is contributing to latency, and whether session caching is configured.

```bash
# Check if SSL session cache is configured
nginx -T 2>/dev/null | grep "ssl_session"

# Check TLS protocol versions and cipher suites
nginx -T 2>/dev/null | grep "ssl_protocols\|ssl_ciphers\|ssl_prefer_server_ciphers"

# Test TLS handshake time from a client
curl -so /dev/null -w "tcp_connect: %{time_connect}s\nssl_handshake: %{time_appconnect}s\nfirst_byte: %{time_starttransfer}s\ntotal: %{time_total}s\n" https://your-domain/
```

Expected output: `ssl_session_cache shared:SSL:10m` present in the configuration. TLS handshake time (`time_appconnect - time_connect`) under 50ms for repeated connections. If the handshake takes hundreds of milliseconds and session caching is not configured, every new connection pays the full handshake cost.

### Step 8: Check system-level resource constraints

Checks whether the NGINX host is resource-constrained (CPU, memory, file descriptors) which can cause latency across all requests.

```bash
# File descriptor limits for NGINX workers
cat /proc/$(pgrep -f "nginx: worker" | head -1)/limits 2>/dev/null | grep "open files"

# Current file descriptor usage
ls /proc/$(pgrep -f "nginx: worker" | head -1)/fd 2>/dev/null | wc -l

# Check for CPU saturation on NGINX workers
pidstat -p $(pgrep -f "nginx: worker" -d,) 1 5 2>/dev/null

# Check system memory pressure
free -h
vmstat 1 5
```

Expected output: file descriptor usage well below limits. CPU usage per worker below 80%. No significant memory swap activity in `vmstat` (`si` and `so` columns near zero). If NGINX workers are CPU-saturated, complex rewrite rules, gzip compression, or Lua scripts may be consuming too many cycles.

## Mitigation

### Option 1: Increase worker connections

- **Risk**: Low. Higher connection limits consume more memory per worker process but the increase is modest (a few KB per connection for event-driven workers).
- **Command**:

```bash
# Edit nginx.conf — increase worker_connections
# events {
#     worker_connections 4096;  # default is typically 1024
#     use epoll;
#     multi_accept on;
# }

nginx -t && nginx -s reload
```

- **Verify**:

```bash
nginx -T 2>/dev/null | grep "worker_connections"
curl -s http://localhost/nginx_status
```

- **Duration**: Immediate after reload.

### Option 2: Enable upstream keepalive connections

- **Risk**: Low. Reuses TCP connections to upstream, eliminating per-request handshake latency. Requires upstream to support HTTP/1.1 keepalive.
- **Command**:

```bash
# Add keepalive to the upstream block:
# upstream backend {
#     server 10.0.1.10:8080;
#     keepalive 64;
#     keepalive_timeout 60s;
#     keepalive_requests 1000;
# }
#
# In the location block:
# proxy_http_version 1.1;
# proxy_set_header Connection "";

nginx -t && nginx -s reload
```

- **Verify**:

```bash
# Check that TIME_WAIT connections to upstream decrease over time
ss -tn state time-wait | grep ":8080" | wc -l
for i in $(seq 1 10); do curl -so /dev/null -w "%{time_total}\n" http://localhost/; done
```

- **Duration**: Immediate after reload. Keep permanently.

### Option 3: Increase proxy buffer sizes

- **Risk**: Low to medium. Larger buffers consume more memory per connection. Oversizing wastes memory under high concurrency.
- **Command**:

```bash
# Increase buffer sizes to avoid disk spill
# proxy_buffer_size    16k;
# proxy_buffers        8 16k;
# proxy_busy_buffers_size 32k;
#
# Alternatively, disable buffering for streaming/SSE endpoints:
# location /api/stream {
#     proxy_buffering off;
#     proxy_pass http://backend;
# }

nginx -t && nginx -s reload
```

- **Verify**:

```bash
ls -la /var/cache/nginx/proxy_temp/ 2>/dev/null | wc -l
curl -so /dev/null -w "total: %{time_total}s\n" http://localhost/large-response-endpoint
```

- **Duration**: Immediate after reload.

### Option 4: Enable SSL session caching

- **Risk**: Low. Caches TLS session parameters so repeated clients skip the full handshake. Consumes shared memory (1 MB holds approximately 4000 sessions).
- **Command**:

```bash
# Add to http block:
# ssl_session_cache shared:SSL:10m;
# ssl_session_timeout 1h;
# ssl_session_tickets on;

nginx -t && nginx -s reload
```

- **Verify**:

```bash
# Test TLS handshake time — second request should be faster
curl -so /dev/null -w "ssl: %{time_appconnect}s total: %{time_total}s\n" https://your-domain/
curl -so /dev/null -w "ssl: %{time_appconnect}s total: %{time_total}s\n" https://your-domain/
```

- **Duration**: Immediate after reload.

## Root Cause Resolution

**If** `$upstream_response_time` is high and accounts for most of `$request_time` → the backend application is slow. Profile the backend to identify slow endpoints, database queries, or external API calls. NGINX configuration changes only mask the symptom.

```bash
# Identify the slowest endpoints by upstream response time
awk '/urt=/{match($0, /urt=([0-9.]+)/, a); match($0, /"(GET|POST|PUT|DELETE) ([^ ]+)/, b); if(a[1]>1.0) print a[1], b[2]}' /var/log/nginx/access.log | sort -rn | head -20
```

**If** `$upstream_connect_time` is high but `$upstream_header_time - $upstream_connect_time` is low → TCP connection establishment is the bottleneck. Enable upstream keepalive as a permanent fix (Option 2 above).

**If** active connections approach `worker_processes * worker_connections` → increase `worker_connections` or add more worker processes. For long-lived connections (WebSockets, SSE), consider dedicating separate upstream blocks with higher connection limits.

```nginx
# Set worker_processes to auto (matches CPU cores)
worker_processes auto;
events {
    worker_connections 4096;
}
```

**If** NGINX is spilling proxy buffers to disk (`proxy_temp` directory has files) → right-size `proxy_buffers` based on your typical response sizes, or disable buffering for endpoints that stream large responses.

**If** TLS handshake time (measured via `curl`) is high → enable SSL session caching, upgrade to TLS 1.3 (supports 0-RTT), and use ECDSA certificates (faster than RSA for handshakes).

```nginx
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;  # TLS 1.3 handles cipher selection
```

**If** the NGINX host itself is CPU-saturated → the worker processes cannot process requests fast enough. Check if `gzip` compression, complex `rewrite` rules, or Lua scripts are consuming CPU. Offload SSL termination to a hardware accelerator or move static file serving to a CDN.

**If** high `$request_time` but low `$upstream_response_time` → the overhead is in client-side transfer (slow clients downloading large responses) or client body upload. Enable `proxy_request_buffering on` (default) so NGINX absorbs the slow client upload before forwarding to upstream, freeing the upstream connection.

## Verification

After applying a fix, verify the latency improvement:

1. Measure end-to-end request latency:

```bash
for i in $(seq 1 10); do
    curl -so /dev/null -w "connect=%{time_connect} ttfb=%{time_starttransfer} total=%{time_total}\n" http://localhost/
done
```

2. Check that upstream timing shows improvement:

```bash
tail -f /var/log/nginx/access.log | grep --line-buffered "rt=" | head -20
```

3. Verify no connection saturation:

```bash
curl -s http://localhost/nginx_status
echo "---"
echo "Limit: $(nginx -T 2>/dev/null | grep worker_connections | awk '{print $2}' | tr -d ';') x $(nginx -T 2>/dev/null | grep worker_processes | awk '{print $2}')"
```

4. Confirm no proxy buffer disk spill:

```bash
ls -la /var/cache/nginx/proxy_temp/ 2>/dev/null
```

5. Monitor for latency regression over 30 minutes:

```bash
awk '/rt=/{match($0, /rt=([0-9.]+)/, a); if(a[1]>2.0) count++} END{print "Slow requests (>2s):", count+0}' /var/log/nginx/access.log
```

## Prevention

1. **Always include upstream timing in access logs** — Configure `$request_time`, `$upstream_connect_time`, `$upstream_header_time`, and `$upstream_response_time` in your log format from day one. Without these, latency diagnosis is guesswork.

2. **Enable upstream keepalive by default** — Set `keepalive` in every upstream block with `proxy_http_version 1.1` and `proxy_set_header Connection ""`. This eliminates per-request TCP and TLS handshake overhead.

3. **Right-size worker connections for your traffic** — Calculate peak concurrent connections and set `worker_connections` with headroom. Monitor active connections against the limit using `stub_status`.

4. **Set up latency alerting** — Use a log shipper (Filebeat, Fluentd, Vector) to parse access logs and alert when P95 or P99 `$request_time` exceeds your SLO threshold.

5. **Enable SSL session caching** — Configure `ssl_session_cache shared:SSL:10m` and `ssl_session_timeout` to reduce TLS handshake overhead for returning clients.

6. **Right-size proxy buffers** — Measure your typical upstream response sizes and set `proxy_buffer_size` and `proxy_buffers` to hold the response in memory. Monitor `proxy_temp_path` for disk spill.

7. **Use connection draining during deploys** — When restarting backend services, use NGINX upstream health checks or manual `server ... down` directives to drain connections gracefully, avoiding latency spikes during deployments.

8. **Tune the OS network stack** — Increase `net.core.somaxconn` (listen backlog), `net.ipv4.tcp_tw_reuse` (TIME_WAIT reuse), and file descriptor limits (`nofile`) for high-traffic deployments.

9. **Separate slow and fast endpoints** — Use different upstream blocks or `location` blocks for streaming/long-polling endpoints versus fast API calls, with distinct timeout and buffer configurations.

10. **Load test regularly** — Run periodic load tests that target your P99 latency SLO under peak traffic. Identify buffer, connection, and timeout limits before they cause production incidents.

## Sources

- [NGINX Admin Guide: Debugging](https://docs.nginx.com/nginx/admin-guide/monitoring/debugging/) — Official NGINX debugging guide covering error log analysis, debug-level logging, and diagnostic techniques for performance issues.
- [NGINX Admin Guide: Logging](https://docs.nginx.com/nginx/admin-guide/monitoring/logging/) — Official guide on access and error log configuration, including upstream timing variables (`$request_time`, `$upstream_response_time`, `$upstream_connect_time`, `$upstream_header_time`).
- [NGINX Documentation: ngx_http_proxy_module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) — Authoritative reference for `proxy_pass`, `proxy_buffering`, `proxy_buffer_size`, `proxy_buffers`, `proxy_connect_timeout`, `proxy_read_timeout`, and keepalive configuration.
- [NGINX Documentation: ngx_http_upstream_module](https://nginx.org/en/docs/http/ngx_http_upstream_module.html) — Reference for upstream block configuration including `keepalive`, `keepalive_timeout`, `keepalive_requests`, and server health parameters.
