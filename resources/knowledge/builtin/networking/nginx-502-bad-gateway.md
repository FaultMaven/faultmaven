---
id: nginx-502-bad-gateway
title: "NGINX 502 Bad Gateway: Upstream Unreachable"
domain: networking
service: nginx
symptom_class:
  - service_unavailable
  - connection_refused
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: "kb-researcher"
status: draft
tags:
  - nginx
  - "502"
  - bad-gateway
  - upstream
  - reverse-proxy
  - timeout
difficulty: intermediate
---

## Problem Definition

This runbook covers NGINX 502 Bad Gateway errors when NGINX is operating as a reverse proxy or load balancer. It applies to NGINX OSS 1.18+ and NGINX Plus R25+, running on Linux (bare metal, VM, or container). Diagnosis requires read access to NGINX error logs (default `/var/log/nginx/error.log`), NGINX configuration files, and the ability to test connectivity to upstream servers. For SELinux-related issues, `ausearch` and `setsebool` permissions are needed.

A 502 Bad Gateway error indicates that NGINX received an invalid or no response from the upstream (backend) server it forwarded the request to. The root cause is never NGINX itself — it is always a failure in the communication path between NGINX and the upstream. The error manifests as an HTTP 502 response to clients and appears in the NGINX error log as one of several distinct messages depending on the underlying cause.

Common causes include:

- **Upstream process not running** — the backend application has crashed, is not started, or was terminated by the OOM killer.
- **Upstream socket exhaustion** — the backend ran out of file descriptors or connection slots.
- **Upstream timeout** — the backend takes longer to respond than NGINX is willing to wait.
- **DNS resolution failure** — NGINX cannot resolve the upstream hostname.
- **Firewall or network partition** — a security group, iptables rule, or network policy blocks the connection.
- **Unix socket permission error** — NGINX cannot connect to a local Unix domain socket due to file permissions.
- **SELinux or AppArmor denial** — mandatory access control prevents NGINX from initiating outbound connections.
- **Upstream returned malformed headers** — the backend sent an HTTP response NGINX could not parse.

## Diagnostic Steps

### Step 1: Check NGINX error logs

Checks the error log for the specific reason NGINX returned 502. Each distinct error message points to a different root cause category.

```bash
grep -i "502\|upstream\|connect()\|failed\|refused" /var/log/nginx/error.log | tail -50
```

Expected output: one or more error lines with a specific failure message. Key error messages and their meaning:

| Error Message | Root Cause |
|---|---|
| `connect() failed (111: Connection refused)` | Upstream process is not listening on the expected port/socket |
| `connect() failed (110: Connection timed out)` | Upstream host is unreachable or firewalled |
| `connect() failed (113: No route to host)` | Network routing failure to upstream |
| `upstream prematurely closed connection` | Upstream crashed or killed the connection mid-response |
| `recv() failed (104: Connection reset by peer)` | Upstream forcibly closed the TCP connection |
| `no live upstreams` | All servers in the upstream block are marked down |
| `upstream timed out (110: Connection timed out) while reading response header` | Backend accepted connection but did not respond in time |
| `no resolver defined to resolve` | DNS resolver not configured for dynamic upstream hostnames |

The error message directly determines which subsequent diagnostic step to follow.

### Step 2: Verify upstream process is running

Checks whether the backend application is running and listening on the expected port or socket.

```bash
# For a TCP upstream (e.g., application on port 8080)
ss -tlnp | grep 8080

# For a Unix socket upstream (e.g., PHP-FPM, Gunicorn, uWSGI)
ls -la /var/run/php-fpm.sock
ss -xlnp | grep php-fpm

# Check if the upstream process is alive
systemctl status your-app-service
# Or for containers:
docker ps --filter "name=your-app"
```

Expected output: `ss` shows the process listening on the expected port with `LISTEN` state. If the process is not running, check why it stopped:

```bash
journalctl -u your-app-service --since "10 minutes ago" --no-pager
dmesg | grep -i "oom\|killed process" | tail -10
```

A missing listener confirms the upstream is down. OOM killer messages in `dmesg` indicate the process was killed due to memory pressure.

### Step 3: Test upstream connectivity from NGINX host

Checks whether the NGINX host can reach the upstream server, isolating network-layer issues from application issues.

```bash
# TCP connection test to upstream
curl -v http://127.0.0.1:8080/health 2>&1 | head -30

# If upstream is on a remote host
nc -zv upstream-host 8080 -w 5

# DNS resolution check (if upstream is a hostname)
dig +short upstream-hostname
nslookup upstream-hostname
```

Expected output: `curl` returns a response from the upstream. `nc` shows `Connection to upstream-host 8080 port [tcp/*] succeeded!`. If the connection is refused, the upstream is not listening. If it times out, a firewall or routing issue is blocking the path. DNS resolution failure means NGINX needs a `resolver` directive.

### Step 4: Check NGINX upstream configuration

Checks the NGINX configuration for the upstream block and proxy_pass directive to verify they point to the correct backend.

```bash
# Find which upstream block is involved
nginx -T 2>/dev/null | grep -A 20 "upstream"

# Check the proxy_pass directive
nginx -T 2>/dev/null | grep -B 5 -A 5 "proxy_pass"

# Validate NGINX configuration syntax
nginx -t
```

Expected output: `nginx -t` returns `syntax is ok` and `test is successful`. The `proxy_pass` directive should point to the correct upstream address and port. A typo in the upstream address or a missing upstream block causes 502. If `nginx -t` fails, a configuration error is preventing NGINX from reloading correctly.

### Step 5: Check resource limits and connection counts

Checks whether NGINX or the upstream has exhausted file descriptors or connection limits, which causes new connections to be refused.

```bash
# Current connections to upstream
ss -tn state established | grep ":8080" | wc -l

# NGINX worker connection limits
nginx -T 2>/dev/null | grep "worker_connections"

# File descriptor limits for NGINX worker processes
cat /proc/$(pgrep -f "nginx: worker" | head -1)/limits | grep "open files"

# Upstream process file descriptor usage
ls /proc/$(pgrep -f "your-app" | head -1)/fd | wc -l
```

Expected output: established connection count well below `worker_connections`. File descriptor usage well below the open files limit. If file descriptors are near the limit for either NGINX or the upstream, new connections fail with connection refused.

### Step 6: Check for SELinux or firewall interference

Checks whether mandatory access control or firewall rules are preventing NGINX from connecting to the upstream.

```bash
# SELinux denials
ausearch -m AVC -ts recent 2>/dev/null | grep nginx

# Check if SELinux is blocking NGINX network connections
getsebool httpd_can_network_connect

# iptables rules that might block traffic to upstream
iptables -L -n | grep 8080

# For Kubernetes environments, check network policies
kubectl get networkpolicy -A -o wide
```

Expected output: `httpd_can_network_connect` returns `on`. No iptables rules blocking the upstream port. If `httpd_can_network_connect` is `off`, SELinux is preventing NGINX from making outbound TCP connections — this is a common cause on RHEL/CentOS systems where NGINX proxies to a non-standard port.

### Step 7: Inspect upstream response headers

Checks whether the upstream sends malformed or oversized headers that NGINX cannot parse, causing 502.

```bash
# Bypass NGINX and call upstream directly to check response format
curl -sI http://127.0.0.1:8080/ 2>&1

# Check for oversized headers from upstream
nginx -T 2>/dev/null | grep "proxy_buffer_size\|proxy_buffers"
```

Expected output: valid HTTP response headers from the upstream. If the upstream sends headers larger than `proxy_buffer_size` (default 4k or 8k depending on platform), NGINX cannot parse them and returns 502. Common culprits: large Set-Cookie headers, oversized JWT tokens in headers, or excessive custom headers.

## Mitigation

### Option 1: Restart the upstream process

- **Risk**: Low. Brief downtime during restart; existing in-flight requests may fail.
- **Command**:

```bash
systemctl restart your-app-service
# Or for Docker:
docker restart your-app-container
# Or for Kubernetes:
kubectl rollout restart deployment/your-app -n your-namespace
```

- **Verify**:

```bash
curl -o /dev/null -s -w "%{http_code}" http://localhost/health
```

- **Duration**: 10-60 seconds depending on application startup time.

### Option 2: Increase NGINX upstream timeout values

- **Risk**: Low. May cause NGINX workers to hold connections longer, reducing capacity under load. Does not fix slow upstreams — only gives them more time.
- **Command**:

```bash
# Add or increase these directives in the server or location block:
cat >> /etc/nginx/conf.d/upstream-timeouts.conf << 'CONF'
# Temporary timeout increase — revert after root cause is fixed
proxy_connect_timeout 30s;
proxy_send_timeout    90s;
proxy_read_timeout    90s;
CONF

nginx -t && nginx -s reload
```

- **Verify**:

```bash
tail -5 /var/log/nginx/error.log
curl -o /dev/null -s -w "%{http_code}\n" http://localhost/
```

- **Duration**: Immediate after reload. Revert once root cause is resolved.

### Option 3: Remove unhealthy upstream from the pool

- **Risk**: Medium. Reduces backend capacity. Only applicable when you have multiple upstream servers and at least one is healthy.
- **Command**:

```bash
# Mark the failing server as down in the upstream block
# In /etc/nginx/conf.d/upstream.conf:
#   upstream backend {
#       server 10.0.1.10:8080;
#       server 10.0.1.11:8080 down;  # <-- temporarily mark down
#   }
nginx -t && nginx -s reload
```

- **Verify**:

```bash
for i in $(seq 1 10); do curl -s -o /dev/null -w "%{http_code} " http://localhost/; done
echo
```

- **Duration**: Immediate after reload. Re-enable the server once it recovers.

### Option 4: Fix Unix socket permissions

- **Risk**: Low. Only relevant when upstream uses a Unix domain socket (PHP-FPM, Gunicorn).
- **Command**:

```bash
# Check current socket ownership
ls -la /var/run/php-fpm.sock

# Fix permissions so NGINX can connect
chown www-data:www-data /var/run/php-fpm.sock
chmod 660 /var/run/php-fpm.sock

# Alternatively, ensure NGINX runs as the correct user
grep "^user" /etc/nginx/nginx.conf
```

- **Verify**:

```bash
curl -o /dev/null -s -w "%{http_code}" http://localhost/
```

- **Duration**: Immediate.

## Root Cause Resolution

**If** `connect() failed (111: Connection refused)` and the upstream process is not running → investigate why the process exited (OOM kill, unhandled exception, configuration error). Fix the application issue and ensure the process manager (systemd, supervisor, Kubernetes) is configured for automatic restarts.

```bash
systemctl edit your-app-service --force
# Add:
# [Service]
# Restart=on-failure
# RestartSec=5
```

**If** `upstream timed out` and the backend is slow → profile the backend application to identify the slow endpoint. Common causes: slow database queries, external API calls without timeouts, synchronous blocking in async workers. Tune `proxy_read_timeout` as a stopgap but fix the backend latency.

```bash
# Add upstream response time to NGINX access log for profiling
# log_format upstream_time '$remote_addr - $remote_user [$time_local] '
#     '"$request" $status $body_bytes_sent '
#     '"$http_referer" "$http_user_agent" '
#     'upstream_response_time=$upstream_response_time '
#     'upstream_addr=$upstream_addr';
# access_log /var/log/nginx/access.log upstream_time;
```

**If** `no live upstreams` and all backends are down → check the health check configuration. NGINX OSS uses passive health checks (marks servers as unavailable after `max_fails` within `fail_timeout`). Ensure the thresholds are not too aggressive.

```bash
# Example: allow 3 failures in 30 seconds before marking down
# upstream backend {
#     server 10.0.1.10:8080 max_fails=3 fail_timeout=30s;
#     server 10.0.1.11:8080 max_fails=3 fail_timeout=30s;
# }
```

**If** `no resolver defined to resolve` for dynamic upstream hostnames → add a resolver directive. This is required when upstream addresses are variables or come from DNS.

```bash
# Add DNS resolver (use your local DNS or a public resolver)
# resolver 127.0.0.53 valid=30s ipv6=off;
# resolver_timeout 5s;
```

**If** `upstream prematurely closed connection` or `recv() failed (104: Connection reset by peer)` → the backend is crashing mid-request or its keepalive settings are mismatched with NGINX. Ensure the backend's keepalive timeout exceeds the NGINX keepalive timeout.

```nginx
upstream backend {
    server 10.0.1.10:8080;
    keepalive 32;
}

location / {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
}
```

**If** SELinux is blocking NGINX from connecting to the upstream → enable the `httpd_can_network_connect` boolean so NGINX (httpd) can make outbound TCP connections.

```bash
setsebool -P httpd_can_network_connect 1
```

**If** upstream returns oversized headers causing a parse failure → increase the proxy buffer size to accommodate large headers (common with applications that set many cookies or large JWT tokens).

```nginx
proxy_buffer_size       16k;
proxy_buffers           4 16k;
proxy_busy_buffers_size 16k;
```

## Verification

After applying a fix, verify the issue is resolved:

1. Confirm NGINX is serving requests without 502 errors:

```bash
for i in $(seq 1 20); do
    curl -o /dev/null -s -w "%{http_code}\n" http://localhost/
done | sort | uniq -c
```

2. Verify no new 502 errors appear in the error log:

```bash
timeout 60 tail -f /var/log/nginx/error.log | grep -i "502\|upstream"
```

3. Check upstream connectivity is stable:

```bash
for i in $(seq 1 10); do
    curl -s -w "HTTP %{http_code} in %{time_total}s\n" -o /dev/null http://127.0.0.1:8080/health
done
```

4. Confirm NGINX upstream health metrics (if using NGINX Plus or a monitoring exporter):

```bash
# NGINX Plus API (commercial)
curl -s http://localhost/api/8/http/upstreams/ | python3 -m json.tool

# Or check stub_status for active connections
curl -s http://localhost/nginx_status
```

5. Monitor for recurrence over the next 30 minutes using access logs:

```bash
awk '$9 == 502 {count++} END {print "502 count:", count+0}' /var/log/nginx/access.log
```

## Prevention

1. **Implement health checks** — Configure active health checks (NGINX Plus) or robust passive checks (NGINX OSS with `max_fails` and `fail_timeout`) so unhealthy upstreams are removed from rotation automatically.

2. **Set appropriate timeouts** — Configure `proxy_connect_timeout`, `proxy_send_timeout`, and `proxy_read_timeout` based on your application's expected response times. Avoid excessively long timeouts that mask backend performance issues.

3. **Enable keepalive connections to upstream** — Use `keepalive` in the upstream block with `proxy_http_version 1.1` and `proxy_set_header Connection ""` to reuse TCP connections and avoid connection storms.

4. **Monitor upstream response time** — Add `$upstream_response_time` and `$upstream_status` to your access log format. Set alerts when upstream response time exceeds your SLO threshold.

5. **Ensure automatic process restart** — Configure systemd, Kubernetes liveness probes, or a process supervisor to automatically restart crashed backend processes.

6. **Right-size connection limits** — Set `worker_connections` in NGINX and connection pool limits in the backend appropriately for your traffic volume. Monitor with `ss` or NGINX stub_status.

7. **Use DNS resolver with TTL** — When using hostnames in upstream blocks (common in container environments), always configure the `resolver` directive with a reasonable `valid` TTL to handle upstream IP changes.

8. **Implement circuit breakers** — In microservice architectures, use circuit breaker patterns (at the application layer or via service mesh) to prevent cascade failures when a downstream service becomes unhealthy.

9. **Load test timeout boundaries** — Periodically load test to verify that your timeout and buffer configurations hold under peak traffic. Adjust before production incidents force the issue.

10. **Centralize NGINX error log monitoring** — Ship error logs to a centralized logging system and create alerts for `connect() failed`, `upstream timed out`, and `no live upstreams` patterns.

## Sources

- [NGINX Admin Guide: Debugging](https://docs.nginx.com/nginx/admin-guide/monitoring/debugging/) — Official NGINX debugging guide covering error log analysis and debug-level logging configuration.
- [NGINX Admin Guide: Logging](https://docs.nginx.com/nginx/admin-guide/monitoring/logging/) — Official guide on access and error log configuration, including upstream response time variables.
- [NGINX Documentation: ngx_http_proxy_module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) — Authoritative reference for `proxy_pass`, `proxy_connect_timeout`, `proxy_read_timeout`, `proxy_send_timeout`, buffer directives, and upstream keepalive configuration.
- [NGINX Documentation: ngx_http_upstream_module](https://nginx.org/en/docs/http/ngx_http_upstream_module.html) — Reference for upstream block configuration including `max_fails`, `fail_timeout`, `keepalive`, and server weight parameters.
