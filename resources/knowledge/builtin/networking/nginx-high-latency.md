---
id: nginx-high-latency
title: "NGINX Request Latency Spikes"
domain: networking
service: nginx
symptom_class:
  - latency
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: kb-researcher
status: draft
tags:
  - nginx
  - latency
  - slow-requests
  - upstream
  - reverse-proxy
  - keepalive
  - tls
  - buffering
difficulty: intermediate
---

# NGINX Request Latency Spikes

## Symptom Recognition

- NGINX access log shows `$request_time` (`rt=`) values well above the historical p50/p95 baseline while HTTP status codes remain 2xx/3xx — the requests complete successfully but slowly.
- Per-stage timing in the access log shows one of `$upstream_connect_time` (`uct=`), `$upstream_header_time` (`uht=`), `$upstream_response_time` (`urt=`), or `$upstream_queue_time` dominating `$request_time`.
- Downstream signals: load balancer reports HTTP 504, browser/SDK clients raise read or socket timeouts, and APM dashboards show p95/p99 spikes localised to routes that traverse NGINX.
- NGINX error log emits `upstream timed out (110: Connection timed out) while reading response header from upstream`, `upstream timed out ... while reading upstream`, or `an upstream response is buffered to a temporary file` (warn level).
- `stub_status` or NGINX Plus `/api/.../connections/` shows `Waiting` close to `worker_connections * worker_processes`, or `Active` rising without a corresponding throughput increase.
- TLS-terminating servers show elevated time-to-first-byte on new connections while reused (keep-alive) connections remain fast — a fingerprint of handshake-dominated latency.
- On the host: `ss -tln 'sport = :443'` reports a non-zero `Recv-Q` on the listen socket (accept-queue backlog), and `ss -s` shows TCP socket counts trending toward the per-process FD ceiling.

## Applicability

- NGINX OSS 1.18+ or NGINX Plus R25+ acting as a reverse proxy or load balancer via `proxy_pass`, `fastcgi_pass`, `uwsgi_pass`, or `grpc_pass`.
- Read access to the NGINX configuration tree (default `/etc/nginx/`), access log (default `/var/log/nginx/access.log`), and error log (default `/var/log/nginx/error.log`).
- Privilege to reload NGINX (`nginx -s reload` or `systemctl reload nginx`) after configuration changes.
- `curl`, `ss`, `dig`, `awk`, and (for TLS handshake measurement) `openssl s_client` available on the proxy host.
- `stub_status` module compiled in (`nginx -V 2>&1 | tr ' ' '\n' | grep stub_status`) and enabled on an internal listener, or NGINX Plus `/api/` endpoint exposed for metrics scraping.
- Access log format must include `$request_time`, `$upstream_connect_time`, `$upstream_header_time`, `$upstream_response_time`; this runbook's first step adds them if missing.
- For Kubernetes ingress-nginx deployments: `kubectl` with `get`, `logs`, and `exec` rights on the ingress-controller namespace.

## Diagnostic Steps

### Step 1: Confirm latency in the access log and add timing variables if missing

```bash
grep -E "log_format|access_log" /etc/nginx/nginx.conf /etc/nginx/conf.d/*.conf 2>/dev/null
tail -n 500 /var/log/nginx/access.log | awk '{print $NF}' | head
```

Expected output: at least one `log_format` definition containing the tokens `$request_time`, `$upstream_connect_time`, `$upstream_header_time`, and `$upstream_response_time`. If those variables are absent, add the following `log_format` and reload:

```bash
cat >/etc/nginx/conf.d/00-log-format.conf <<'EOF'
log_format timing '$remote_addr - $remote_user [$time_local] '
                  '"$request" $status $body_bytes_sent '
                  'rt=$request_time uct="$upstream_connect_time" '
                  'uht="$upstream_header_time" urt="$upstream_response_time" '
                  'uqt="$upstream_queue_time" ua="$upstream_addr"';
access_log /var/log/nginx/access.log timing;
EOF
nginx -t && nginx -s reload
```

### Step 2: Compute p50/p95/p99 of $request_time over the active window

```bash
tail -n 5000 /var/log/nginx/access.log \
  | grep -oE 'rt=[0-9.]+' | cut -d= -f2 | sort -n \
  | awk 'BEGIN{c=0} {a[c++]=$1} END{
      if(c==0){print "no_rt"; exit}
      printf "count=%d p50=%.3f p95=%.3f p99=%.3f max=%.3f\n",
        c, a[int(c*0.50)], a[int(c*0.95)], a[int(c*0.99)], a[c-1]}'
```

Expected output: a single line with `count=`, `p50=`, `p95=`, `p99=`, `max=`. Treat any sustained `p95 > 1.0` (1 s) or `p99 > 5.0` for a reverse-proxy workload as elevated; compare against the historical baseline if recorded.

### Step 3: Decompose request_time into client, connect, header, and response components

```bash
tail -n 5000 /var/log/nginx/access.log \
  | awk 'match($0,/rt=([0-9.]+).*uct="([0-9.-]+)".*uht="([0-9.-]+)".*urt="([0-9.-]+)"/,m){
      n++; rt+=m[1]+0;
      uc += (m[2]=="-")?0:m[2]+0;
      uh += (m[3]=="-")?0:m[3]+0;
      ur += (m[4]=="-")?0:m[4]+0
    } END{
      if(!n){print "no_samples"; exit}
      printf "avg_rt=%.3f avg_uct=%.3f avg_uht=%.3f avg_urt=%.3f client_overhead=%.3f\n",
        rt/n, uc/n, uh/n, ur/n, (rt-ur)/n}'
```

Expected output: per-stage averages. The dominant term identifies the layer responsible: large `avg_uct` (upstream connect), large `avg_uht` (upstream is slow to send first byte), large `avg_urt - avg_uht` (upstream is slow to send body), large `client_overhead = avg_rt - avg_urt` (time spent on client read/write or in NGINX itself, not upstream).

### Step 4: Inspect NGINX connection counters and accept-queue backlog

```bash
curl -sS http://127.0.0.1/nginx_status
ss -tln 'sport = :80' 'sport = :443'
ss -s | head -5
NGINX_PID=$(cat /run/nginx.pid 2>/dev/null || pgrep -of "nginx: master")
cat /proc/$NGINX_PID/limits | grep -E "Max open files"
```

Expected output: from `stub_status`, `Active connections: N`, `Waiting: W`. A `Waiting` value approaching `worker_connections * worker_processes` indicates the worker connection pool is saturated. From `ss -tln`, the `Recv-Q` column on the listen socket should be 0; any non-zero value means new connections are queueing in the kernel accept-queue, which directly adds latency.

### Step 5: Check upstream keepalive configuration and reuse ratio

```bash
nginx -T 2>/dev/null | grep -E "upstream |keepalive|proxy_http_version|proxy_set_header.*Connection"
ss -tnp state established 'dport = :<upstream-port>' | wc -l
```

Expected output: an `upstream` block containing a `keepalive <N>;` line (NGINX 1.29.7+ defaults to `keepalive 32 local;`; on older builds it is unset and must be added explicitly), plus `proxy_http_version 1.1;` and `proxy_set_header Connection "";` at http/server/location scope. Missing keepalive or `proxy_http_version 1.0` forces a new TCP (and TLS) handshake per upstream request and is the single most common cause of inflated `$upstream_connect_time`.

### Step 6: Check whether responses are spilling to disk (proxy temp files)

```bash
grep -E "an upstream response is buffered to a temporary file" /var/log/nginx/error.log | tail -20
ls -la /var/cache/nginx/proxy_temp/ 2>/dev/null | head -20
nginx -T 2>/dev/null | grep -E "proxy_buffering|proxy_buffers|proxy_buffer_size|proxy_temp_path"
iostat -xz 1 3 2>/dev/null | tail -20
```

Expected output: `an upstream response is buffered to a temporary file` warnings indicate `proxy_buffers` capacity is too small for the response payload, so NGINX writes the response to `proxy_temp_path` on disk before delivery. `iostat` `%util` >70% or `await` >20 ms on the device backing `proxy_temp_path` confirms disk I/O is delaying responses.

### Step 7: Measure TLS handshake cost on new vs reused connections

```bash
HOST=<your-fqdn>
curl -o /dev/null -sS -w "new:    dns=%{time_namelookup}s connect=%{time_connect}s ssl=%{time_appconnect}s ttfb=%{time_starttransfer}s total=%{time_total}s\n" https://$HOST/ \
  --resolve $HOST:443:$(dig +short $HOST | head -1)
curl -o /dev/null -sS -w "reused: ttfb=%{time_starttransfer}s total=%{time_total}s\n" \
  --next https://$HOST/ --next https://$HOST/ \
  --resolve $HOST:443:$(dig +short $HOST | head -1)
grep -E "ssl_session_cache|ssl_session_tickets|ssl_session_timeout|ssl_protocols" /etc/nginx/nginx.conf /etc/nginx/conf.d/*.conf 2>/dev/null
```

Expected output: from `curl`, `ssl=` (TLS handshake) on a fresh connection vs `ttfb=` on subsequent reused connections. A handshake cost of >100 ms per new connection combined with absence of `ssl_session_cache shared:...` and `ssl_session_tickets on;` in the config indicates clients are paying a full handshake every connection.

### Step 8: Check upstream queue saturation and max_conns enforcement

```bash
tail -n 5000 /var/log/nginx/access.log \
  | grep -oE 'uqt="[0-9.-]+"' | cut -d'"' -f2 \
  | awk '{if($1!="-" && $1+0>0){n++; s+=$1; if($1+0>m)m=$1}} END{printf "queue_samples=%d avg=%.3f max=%.3f\n", n+0, (n?s/n:0), m+0}'
nginx -T 2>/dev/null | grep -E "max_conns|queue "
```

Expected output: `queue_samples=0` means no requests waited in the upstream queue. Non-zero `avg` or `max` indicates `max_conns` is set on the upstream and is being saturated; requests then wait in a per-upstream queue. The `queue` directive entry (`queue <N> timeout=<t>;`) confirms the bound.

### Step 9: Check the host-level network path and conntrack saturation

```bash
ss -i 'state established' '( sport = :443 or sport = :80 )' | head -20
ss -tnp state time-wait | wc -l
nstat -az TcpRetransSegs Tcp_RetransSegs TcpExtListenDrops TcpExtListenOverflows 2>/dev/null \
  || cat /proc/net/netstat | awk '/TcpExt/{for(i=1;i<=NF;i++)h[i]=$i; getline; for(i=1;i<=NF;i++)print h[i]"="$i}' | grep -E "ListenDrop|ListenOverflow|TCPSynRetrans"
[ -f /proc/sys/net/netfilter/nf_conntrack_count ] && \
  awk 'NR==1{print "conntrack="$1}' /proc/sys/net/netfilter/nf_conntrack_count && \
  awk 'NR==1{print "conntrack_max="$1}' /proc/sys/net/netfilter/nf_conntrack_max
```

Expected output: `TcpExtListenDrops` and `TcpExtListenOverflows` increasing during the latency window indicates the kernel accept-queue is overflowing, which manifests as connection-establishment delay. `conntrack` approaching `conntrack_max` causes new connections to stall while old entries are reclaimed. `ss -i` shows per-connection `rtt`/`retrans` values; sustained `retrans:` non-zero indicates packet loss on the path.

### Step 10: Profile a single slow request end-to-end

```bash
HOST=<your-fqdn>
URL_PATH=/<slow-route>
curl -o /dev/null -sS -w "dns=%{time_namelookup}s connect=%{time_connect}s ssl=%{time_appconnect}s pretransfer=%{time_pretransfer}s ttfb=%{time_starttransfer}s total=%{time_total}s\n" "https://$HOST$URL_PATH"
tail -n 100 /var/log/nginx/access.log | awk -v p="$URL_PATH" '$0 ~ p'
```

Expected output: client-side timing from `curl` paired with the NGINX-side access log entry for the same request. A large gap between `time_starttransfer` (client TTFB) and `$upstream_header_time` is time spent in NGINX (buffering, FD wait, worker scheduling); a small gap means upstream is responsible.

## Causes

### Cause A: Missing or disabled upstream keepalive forces a fresh TCP handshake per request

**Statement:** NGINX opens a new TCP connection to the upstream for every request because the upstream block has no `keepalive` directive or `proxy_http_version` is left at 1.0, inflating `$upstream_connect_time` on every request.

**Mechanism:** Without `keepalive` and `proxy_http_version 1.1; proxy_set_header Connection "";`, NGINX closes the upstream socket after each response. Every subsequent request pays a fresh TCP three-way handshake (and a TLS handshake on `proxy_pass https://`) before any application work begins. Under steady load this adds the upstream RTT — and several extra RTTs for TLS — to every request's `$request_time`.

**Indicator:**

- [Step 3] decomposition shows `avg_uct` is the dominant term in `$request_time` (e.g., `uct >= 0.50 * rt`)
- [Step 5] `nginx -T | grep -E "keepalive|proxy_http_version"` shows no `keepalive` directive in the upstream block, or `proxy_http_version 1.0`, or the absence of `proxy_set_header Connection "";`
<!-- match: {"step": 5, "predicate": "absent", "target": "keepalive"} -->

**Mitigation:**

- **Risk:** Enabling upstream keepalive immediately increases the count of long-lived sockets held by NGINX workers; verify `worker_rlimit_nofile` is at least `2 * worker_connections` before reload.
- **Command:**

  ```bash
  cat >/etc/nginx/conf.d/upstream-keepalive.conf <<'EOF'
  # Inject into the relevant upstream block via include or edit in place
  # upstream <name> { ...; keepalive 32; keepalive_requests 1000; keepalive_timeout 60s; }
  EOF
  nginx -t
  ```

- **Duration:** Permanent once verified. Keepalive is the documented default-on configuration since NGINX 1.29.7.

**Resolution:**

```nginx
# /etc/nginx/conf.d/<route>.conf
upstream app_backend {
    server backend-1.internal:8080;
    server backend-2.internal:8080;
    keepalive 32;
    keepalive_requests 1000;
    keepalive_timeout 60s;
}

server {
    location / {
        proxy_pass http://app_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
```

```bash
nginx -t && nginx -s reload
```

**Impact:** Cluster-wide for the proxy instance. Each worker maintains up to `keepalive` idle sockets per upstream. Reload is graceful — in-flight requests finish on the old worker.

**Rollback:** Remove the `keepalive` line (or set to 0) and the `proxy_http_version`/`Connection ""` lines, then `nginx -t && nginx -s reload`.

**Verification:** Repeat Step 3; `avg_uct` drops to near-zero on the second and subsequent requests against the same upstream. `ss -tnp 'dport = :<upstream-port>' | wc -l` shows a stable pool of `ESTAB` sockets rather than churning `TIME-WAIT`.

### Cause B: Upstream application is slow to send response headers

**Statement:** The backend takes a long time to compute the response, so `$upstream_header_time` and `$upstream_response_time` dominate `$request_time` while NGINX itself is idle.

**Mechanism:** NGINX is bound by `proxy_read_timeout` (default 60 s) for the gap between successive reads from the upstream. A slow database query, blocking external API call, or CPU-bound handler keeps the upstream silent past normal latency targets but inside the timeout, so the request completes with elevated `$upstream_header_time`/`$upstream_response_time` and a 2xx status. The latency is real but originates in the application, not in NGINX.

**Indicator:**

- [Step 3] `avg_uht` or `(avg_urt - avg_uht)` is the dominant term in `$request_time`
- [Step 10] client-side `time_starttransfer` is dominated by waiting for the first response byte from NGINX, which matches the access log's high `$upstream_header_time`
- [Symptom] upstream-side APM/tracing shows handler p95 elevated; NGINX CPU and connection counters are nominal

**Mitigation:**

- **Risk:** Tightening `proxy_read_timeout` will convert slow but successful requests into 504s and surface the problem more loudly; only do so when you can absorb the error rate.
- **Command:**

  ```bash
  # Per-location, override only for the slow route
  cat >/etc/nginx/conf.d/<route>-timeouts.conf <<'EOF'
  # location /reports/ { proxy_read_timeout 30s; proxy_send_timeout 30s; }
  EOF
  nginx -t
  ```

- **Duration:** Diagnostic only. Push the durable fix into the upstream service.

**Resolution:** The fix lands on the upstream tier, not on NGINX. Identify the slow handler via upstream APM, optimise the query/dependency call, add a cache layer, or move synchronous work to a job queue and have the handler return 202.

```bash
# Upstream-side examples — adapt to the stack in use
# 1) Add a covering index for the dominant slow query
# 2) Set a client timeout on the downstream dependency call (e.g., HTTP client read timeout)
# 3) Cache the hot-path response in Redis with a short TTL
kubectl rollout restart deployment/<upstream-deployment> -n <namespace>
```

**Verification:** Re-run Step 3 after the upstream change; `avg_uht`/`avg_urt` returns to baseline. Step 10 against the same route shows `time_starttransfer` recovered.

### Cause C: NGINX worker connection pool saturated, requests queue in the accept-queue

**Statement:** Worker connections are fully consumed by in-flight requests and idle keepalives, so new connections wait in the kernel accept-queue before NGINX can handle them, adding constant latency before any application work starts.

**Mechanism:** Each worker accepts up to `worker_connections` simultaneous client connections. When that ceiling is hit, the kernel queues incoming `SYN`s in the listen-socket backlog (sized by `listen ... backlog=` and bounded by `net.core.somaxconn`). New clients wait there until a worker drains a connection; if the backlog itself overflows the kernel increments `TcpExtListenOverflows` and drops `SYN`s, which clients retry after the kernel's SYN retransmission timer.

**Indicator:**

- [Step 4] `stub_status` reports `Active connections` close to `worker_processes * worker_connections`, and `Waiting` similarly close to capacity
- [Step 4] `ss -tln 'sport = :443'` shows `Recv-Q > 0` on the NGINX listen socket
<!-- match: {"step": 4, "predicate": "contains", "target": "Recv-Q"} -->
- [Step 9] `nstat`/`/proc/net/netstat` shows `TcpExtListenOverflows` or `TcpExtListenDrops` incrementing during the latency window
<!-- match: {"step": 9, "predicate": "contains", "target": "ListenOverflow"} -->

**Mitigation:**

- **Risk:** Raising `worker_connections` without raising `worker_rlimit_nofile` will exhaust file descriptors and surface as `socket() failed (24: Too many open files)` in the error log; raise both together.
- **Command:**

  ```bash
  # Temporary FD ceiling lift on the running master pending reload
  prlimit --nofile=65535:65535 --pid $(cat /run/nginx.pid)
  ```

- **Duration:** Until the next restart — `prlimit` does not persist. Pair with the resolution-step config change in the same change window.

**Resolution:**

```nginx
# /etc/nginx/nginx.conf — top level
worker_processes auto;
worker_rlimit_nofile 65535;

events {
    worker_connections 16384;
    multi_accept on;
}

# Per-server listen backlog — raise above the kernel default of 4096 if overflowing
# server { listen 443 ssl backlog=8192; }
```

```bash
# Kernel-side, so accept() can drain the larger backlog
sysctl -w net.core.somaxconn=8192
echo 'net.core.somaxconn=8192' >/etc/sysctl.d/99-nginx.conf
# systemd drop-in for LimitNOFILE on the nginx unit
mkdir -p /etc/systemd/system/nginx.service.d
cat >/etc/systemd/system/nginx.service.d/limits.conf <<'EOF'
[Service]
LimitNOFILE=65535
EOF
systemctl daemon-reload
systemctl restart nginx
```

**Impact:** Host-wide for the NGINX instance. `LimitNOFILE` is fixed at process start, so a full restart (not reload) is required; expect a single-digit-second blip while workers re-establish connections.

**Rollback:** Remove `/etc/systemd/system/nginx.service.d/limits.conf`, revert `worker_connections`/`worker_rlimit_nofile` in `nginx.conf`, restore the previous `net.core.somaxconn`, then `systemctl daemon-reload && systemctl restart nginx`.

**Verification:** Repeat Step 4; `Active connections` is well below the new ceiling under the same load and `Recv-Q` on the listen socket stays at 0. `nstat TcpExtListenOverflows` stops incrementing.

### Cause D: Proxy response buffers undersized, NGINX spills to disk

**Statement:** `proxy_buffers` capacity is smaller than the upstream response payload, so NGINX writes responses to `proxy_temp_path` on the local disk and serves them from disk, adding I/O latency to every large response.

**Mechanism:** With `proxy_buffering on` (default), NGINX reads the upstream response into memory buffers sized by `proxy_buffers <count> <size>`. Once the in-memory buffers fill and `proxy_busy_buffers_size` is reached, NGINX writes the overflow to a temp file under `proxy_temp_path`. Each request that overflows pays an extra disk write and read; on slow or contended storage this adds tens to hundreds of milliseconds and emits the warning `an upstream response is buffered to a temporary file` to the error log.

**Indicator:**

- [Step 6] error log contains `an upstream response is buffered to a temporary file`
<!-- match: {"step": 6, "predicate": "contains", "target": "an upstream response is buffered to a temporary file"} -->
- [Step 6] `ls /var/cache/nginx/proxy_temp/` shows files larger than zero accumulating during the latency window
- [Step 3] `client_overhead = avg_rt - avg_urt` is significant (>50 ms) while `avg_uct`/`avg_uht`/`avg_urt` are nominal

**Mitigation:**

- **Risk:** Larger `proxy_buffers` raise per-connection memory; multiply by peak concurrent connections to size headroom. Setting `proxy_buffering off` removes disk spill but disables NGINX's slow-client protection — slow downloads then occupy a worker for the entire transfer.
- **Command:**

  ```bash
  cat >/etc/nginx/conf.d/proxy-buffers.conf <<'EOF'
  proxy_buffer_size 16k;
  proxy_buffers 16 16k;
  proxy_busy_buffers_size 32k;
  EOF
  nginx -t && nginx -s reload
  ```

- **Duration:** Permanent once sized correctly against the 95th-percentile response payload for the affected route.

**Resolution:**

```nginx
# /etc/nginx/conf.d/<route>.conf — apply at the location level so memory cost is scoped
location /api/ {
    proxy_pass http://app_backend;
    proxy_buffering on;
    proxy_buffer_size 16k;
    proxy_buffers 32 16k;        # 32 * 16k = 512k per connection for body
    proxy_busy_buffers_size 64k;
    proxy_max_temp_file_size 0;   # forbid disk spill; backpressure instead
}
```

```bash
nginx -t && nginx -s reload
```

**Impact:** Per-location memory footprint rises (`proxy_buffers` * concurrent requests on that location). Setting `proxy_max_temp_file_size 0` makes NGINX block reads from the upstream when buffers are full instead of spilling, which is preferable for low-latency APIs.

**Rollback:** Revert the location's buffer directives (or remove the `proxy-buffers.conf` drop-in) and reload: `nginx -t && nginx -s reload`.

**Verification:** Repeat Step 6; no new `an upstream response is buffered to a temporary file` lines appear and `/var/cache/nginx/proxy_temp/` stays empty under representative load. Step 3 shows `client_overhead` returning to single-digit ms.

### Cause E: TLS handshake cost on every new client connection

**Statement:** Clients pay a full TLS handshake on every connection because session resumption is not configured, so new-connection latency is RTT-bound and `time_appconnect` dominates time-to-first-byte for short-lived clients.

**Mechanism:** A full TLS 1.2 handshake adds 2 RTTs plus asymmetric crypto; TLS 1.3 reduces that to 1 RTT. With `ssl_session_cache none` (the default) and `ssl_session_tickets off`, every client renegotiates a full handshake on every connection. Short-lived clients (CLI tools, scrapers, mobile networks with frequent reconnects, load balancers that disable upstream keepalive) then pay the handshake on every request. The cost is visible only on new connections — reused keepalive connections are unaffected.

**Indicator:**

- [Step 7] `curl -w "ssl=%{time_appconnect}s"` reports >100 ms on a new connection while reused connections show `ttfb` well below `ssl`
- [Step 7] config shows neither `ssl_session_cache shared:...` nor `ssl_session_tickets on;`
<!-- match: {"step": 7, "predicate": "absent", "target": "ssl_session_cache shared"} -->
- [Symptom] latency p99 spikes correlate with connection-establishment events (mobile network roams, load-balancer fail-over, scraper bursts)

**Mitigation:**

- **Risk:** Session tickets stored without rotation become a weak point if the ticket key is exfiltrated; pin `ssl_session_tickets on;` with periodic key rotation via `ssl_session_ticket_key` in production.
- **Command:**

  ```bash
  # Snapshot the cipher cost contribution
  openssl speed -seconds 5 rsa2048 ecdsap256 2>/dev/null | tail -10
  ```

- **Duration:** Diagnostic only; the durable fix is the resolution-step config.

**Resolution:**

```nginx
# /etc/nginx/conf.d/ssl-tuning.conf
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers HIGH:!aNULL:!MD5;
ssl_prefer_server_ciphers off;

# Shared resumption cache and stateless tickets
ssl_session_cache shared:SSL:50m;     # ~200k sessions
ssl_session_timeout 1h;
ssl_session_tickets on;

# Lower TTFB for small responses
ssl_buffer_size 4k;
```

```bash
nginx -t && nginx -s reload
```

**Impact:** Cluster-wide for every HTTPS listener. The shared session cache is sized in shared memory (50 MB here ~ 200k sessions); resize per traffic profile. Reload is graceful.

**Rollback:** Remove or revert `ssl-tuning.conf` and reload: `nginx -t && nginx -s reload`. Existing sessions in the cache are discarded on reload.

**Verification:** Repeat Step 7. The second and third `curl --next ...` runs against the same host now report `ttfb` well below the original `ssl=` figure; on TLS 1.3 capable clients the handshake cost on a new connection drops to a single RTT.

### Cause F: Upstream connection limit (max_conns) saturated, requests queue

**Statement:** Each upstream server has a `max_conns` cap that has been reached, so additional requests wait in NGINX's per-upstream queue and `$upstream_queue_time` becomes non-zero.

**Mechanism:** `max_conns=<N>` on an `upstream server` caps concurrent in-flight requests per upstream member. When all members are at their cap, requests park in the upstream queue declared by `queue <N> timeout=<t>;`. Time spent in the queue is reported as `$upstream_queue_time` and adds to `$request_time` even though the request never reached the application yet. If the `queue timeout` expires before a slot opens, NGINX returns 502 to the client.

**Indicator:**

- [Step 8] `awk` summary over access logs reports `queue_samples > 0` with non-trivial `avg`/`max` queue time
<!-- match: {"step": 8, "predicate": "contains", "target": "queue_samples="} -->
- [Step 8] `nginx -T | grep -E "max_conns|queue "` shows `max_conns=` on upstream servers and a `queue` directive in the upstream block
<!-- match: {"step": 8, "predicate": "contains", "target": "max_conns"} -->
- [Step 3] `avg_rt` is materially higher than `avg_urt + avg_uct + avg_uht`, with the difference matching `avg_uqt`

**Mitigation:**

- **Risk:** Raising `max_conns` without confirming upstream capacity shifts the bottleneck downstream and can collapse the application. Confirm upstream-side concurrency headroom first.
- **Command:**

  ```bash
  # Temporary widening on a single upstream member while validating
  # Edit the relevant upstream block:
  # upstream app_backend { server backend-1.internal:8080 max_conns=200; ... }
  nginx -t
  ```

- **Duration:** Hours, while upstream capacity is verified or scaled.

**Resolution:**

```nginx
# Option A - raise the per-server cap to match upstream capacity
upstream app_backend {
    zone app_backend 64k;          # required for shared-memory max_conns counting
    server backend-1.internal:8080 max_conns=200;
    server backend-2.internal:8080 max_conns=200;
    keepalive 32;
}

# Option B - add more upstream members and let max_conns stay tight per-instance
upstream app_backend {
    zone app_backend 64k;
    server backend-1.internal:8080 max_conns=100;
    server backend-2.internal:8080 max_conns=100;
    server backend-3.internal:8080 max_conns=100;
    server backend-4.internal:8080 max_conns=100;
    keepalive 32;
}
```

```bash
nginx -t && nginx -s reload
```

**Impact:** Cluster-wide for the upstream group. With `zone` declared, `max_conns` accounting is shared across all workers and the cap is exact; without `zone` it is per-worker. Reload is graceful.

**Rollback:** Restore the previous `max_conns` values (and/or remove the new upstream members) and reload: `nginx -t && nginx -s reload`.

**Verification:** Step 8 reports `queue_samples=0` (or matches baseline) over a 10-minute window under representative load. Upstream-side metrics show in-flight request count below the new `max_conns` ceiling.

### Cause G: Kernel conntrack table saturated, new connections stall

**Statement:** The kernel netfilter conntrack table is at or near its maximum, so new connections to or from NGINX wait for table slots to free, adding constant connection-establishment latency.

**Mechanism:** Every stateful firewalled connection consumes one `nf_conntrack` entry. When `nf_conntrack_count` reaches `nf_conntrack_max`, the kernel must reclaim a slot (typically by expiring a `TIME-WAIT` entry) before accepting a new connection. Until a slot opens, the inbound `SYN` is dropped or held, and the kernel logs `nf_conntrack: table full, dropping packet` to dmesg. The client retries via TCP SYN retransmit at ~1 s intervals, which surfaces as multi-second connection establishment.

**Indicator:**

- [Step 9] `cat /proc/sys/net/netfilter/nf_conntrack_count` is within 5% of `nf_conntrack_max`
<!-- match: {"step": 9, "predicate": "threshold", "target": "conntrack_utilization_pct", "op": ">", "value": 95} -->
- [Step 9] `dmesg | grep nf_conntrack` shows `table full, dropping packet`
<!-- match: {"step": 9, "predicate": "contains", "target": "nf_conntrack: table full"} -->
- [Step 3] `avg_uct` is elevated only on new connections and recovers when upstream keepalive is in use

**Mitigation:**

- **Risk:** Raising `nf_conntrack_max` increases kernel memory consumption (~300 bytes per entry); confirm host RAM headroom before raising.
- **Command:**

  ```bash
  sysctl -w net.netfilter.nf_conntrack_max=1048576
  sysctl -w net.netfilter.nf_conntrack_tcp_timeout_time_wait=30
  ```

- **Duration:** Until reboot. Persist with a sysctl drop-in (resolution step) before the next restart.

**Resolution:**

```bash
cat >/etc/sysctl.d/99-nginx-conntrack.conf <<'EOF'
net.netfilter.nf_conntrack_max=1048576
# Default hashsize is nf_conntrack_max/8; size accordingly
net.netfilter.nf_conntrack_tcp_timeout_time_wait=30
net.netfilter.nf_conntrack_tcp_timeout_established=600
EOF
sysctl --system
# Bump hashsize (resizes the bucket array; cheap)
echo 131072 > /sys/module/nf_conntrack/parameters/hashsize
```

**Impact:** Host-wide. Conntrack memory budget grows roughly linearly with `nf_conntrack_max`. Changes are dynamic; no service restart is needed.

**Rollback:** Remove `/etc/sysctl.d/99-nginx-conntrack.conf`, run `sysctl --system`, and reset hashsize: `echo <previous-value> > /sys/module/nf_conntrack/parameters/hashsize`.

**Verification:** Step 9 reports `nf_conntrack_count` well below `nf_conntrack_max` under the same load, and `dmesg | grep nf_conntrack` shows no further table-full lines for at least 30 minutes.

### Cause H: Logging I/O blocks worker threads

**Statement:** Synchronous `access_log` writes to slow or contended storage block worker threads, adding latency proportional to disk write time to every request.

**Mechanism:** By default NGINX writes each access-log line synchronously inside the worker that handled the request. If `/var/log/nginx/` is on a slow, contended, or full disk (high `await` in `iostat`), the worker stalls while waiting for the write to complete. Buffered logging (`access_log <path> <format> buffer=<size> flush=<time>;`) batches writes and amortises the I/O, eliminating the per-request stall.

**Indicator:**

- [Step 6] `iostat -xz 1` shows the device backing `/var/log` with sustained `%util > 70` or `await > 20` during the latency window
- [Step 3] `client_overhead = avg_rt - avg_urt` is elevated and tracks log-write latency rather than upstream latency
- [Step 9] `df -h /var/log/nginx/` shows the partition >95% full, or `dmesg` shows EXT4/XFS errors on the log device

**Mitigation:**

- **Risk:** Disabling `access_log` entirely removes forensic data needed during the incident; buffered logging is the right balance.
- **Command:**

  ```bash
  # Move logs to a faster volume or to syslog if the disk is the bottleneck
  cat >/etc/nginx/conf.d/access-log.conf <<'EOF'
  access_log /var/log/nginx/access.log timing buffer=64k flush=5s;
  EOF
  nginx -t && nginx -s reload
  ```

- **Duration:** Permanent. Buffered logging is recommended for any production NGINX.

**Resolution:**

```nginx
# /etc/nginx/nginx.conf - http block
http {
    log_format timing 'rt=$request_time uct="$upstream_connect_time" '
                      'uht="$upstream_header_time" urt="$upstream_response_time" '
                      'ua="$upstream_addr" status=$status path="$request"';

    # Buffered logging: amortise disk writes, flush at most every 5s
    access_log /var/log/nginx/access.log timing buffer=64k flush=5s;
    error_log  /var/log/nginx/error.log warn;
}
```

```bash
# Verify the log mount is on suitable storage
mount | grep " /var/log "
nginx -t && nginx -s reload
```

**Impact:** Cluster-wide for the proxy instance. Log loss window is bounded by `flush=` (5 s here) on abrupt termination; for audit-grade logging keep `buffer=` small or stream to a syslog endpoint instead.

**Rollback:** Restore the previous unbuffered `access_log` line (remove `buffer=` and `flush=`) and reload: `nginx -t && nginx -s reload`.

**Verification:** Step 6 shows `iostat` `%util` on the log device drops below 30% under the same load. Step 3 reports `client_overhead` recovered to baseline.

### Cause Z: Unidentified

**Statement:** Diagnostic steps confirmed elevated `$request_time` in NGINX but did not match any of the indicators for Causes A through H.

**Mechanism:** Latency is real (Step 2 confirmed p95/p99 above baseline) but the per-stage decomposition does not localise the bottleneck to upstream connect, upstream header/body time, worker saturation, buffer spill, TLS, upstream queue, conntrack, or log I/O. The slow path may involve a less common module (`grpc_pass`, `uwsgi_pass`, `memcached_pass`), a third-party NGINX module, an unusual `auth_request` chain, or upstream-side issues invisible from the proxy host.

**Indicator:**

- [Default] Step 2 confirmed elevated p95/p99 but Causes A-H indicators did not match the gathered evidence

**Mitigation:**

- **Risk:** Enabling `error_log ... debug;` on a busy proxy fills disk rapidly; prefer `error_log memory:32m debug;` (a memory ring buffer) on hot hosts and extract with gdb if needed.
- **Command:**

  ```bash
  # Snapshot active config, recent error log, connection state, and a single slow trace
  nginx -T > /tmp/nginx-conf.dump 2>&1
  tail -n 1000 /var/log/nginx/error.log > /tmp/nginx-error.tail
  ss -tnp state established | head -100 > /tmp/nginx-ss.snap
  tail -n 5000 /var/log/nginx/access.log | awk '/rt=[5-9]\.|rt=[0-9]{2,}\./' > /tmp/nginx-slow.access
  ```

- **Duration:** Minutes. Collect, hand off, then revert any temporary debug logging.

**Resolution:** Out of runbook scope. Package the captured `nginx-conf.dump`, `nginx-error.tail`, `nginx-ss.snap`, `nginx-slow.access`, the upstream service's logs covering the same window, and the per-stage timing summary from Step 3; escalate to the proxy owner or platform on-call with the affected route, the latency window, and the upstream identifier.

**Verification:** Hand-off acknowledged by the receiving engineer; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

## Prevention

- Standardise the access-log format on `rt=$request_time uct=$upstream_connect_time uht=$upstream_header_time urt=$upstream_response_time uqt=$upstream_queue_time ua=$upstream_addr` so every incident starts with usable timing data; ship via a single included `log_format` snippet.
- In every `upstream` block, set `keepalive 32; keepalive_requests 1000; keepalive_timeout 60s;` and pair with `proxy_http_version 1.1; proxy_set_header Connection "";` at server or location scope. NGINX 1.29.7+ enables `keepalive 32 local;` by default; verify on older builds.
- Set `worker_processes auto;`, `worker_rlimit_nofile` to at least `2 * worker_connections`, and pin `LimitNOFILE` in the systemd unit drop-in so file-descriptor exhaustion cannot re-emerge after restart.
- Tune kernel listen-queue: `net.core.somaxconn=8192` and `listen ... backlog=8192;` on each server block. Page on `node_netstat_TcpExt_ListenOverflows > 0` sustained 5 minutes.
- Size `proxy_buffers` against the 95th-percentile response payload for each location; set `proxy_max_temp_file_size 0` on low-latency APIs to forbid disk spill (apply backpressure to upstream instead).
- Enable TLS session resumption: `ssl_session_cache shared:SSL:50m; ssl_session_timeout 1h; ssl_session_tickets on; ssl_protocols TLSv1.2 TLSv1.3;`. Rotate session-ticket keys on a schedule.
- Use buffered access logs (`buffer=64k flush=5s`) on production proxies and place `/var/log/nginx/` on a dedicated volume with monitored `%util`/`await`.
- Page on the latency SLI `histogram_quantile(0.95, sum by (le) (rate(nginx_request_duration_seconds_bucket[5m]))) > <baseline>` over 10 minutes, and on `nginx_upstream_response_time_seconds` per upstream so regressions are localised within minutes.
- Track `node_nf_conntrack_entries / node_nf_conntrack_entries_limit > 0.8` as a warning and `> 0.95` as a page; raise `nf_conntrack_max` ahead of the threshold rather than reactively during incidents.
- Add `health_check` (NGINX Plus) or Kubernetes readiness probes on every upstream so unhealthy backends are pulled out of rotation before they elevate `$upstream_connect_time` or `$upstream_response_time`.
- Validate every config change with `nginx -t` in CI and roll out via a canary host or canary deployment; config that passes `nginx -t` can still degrade p95 (e.g., buffer sizing) and needs traffic-shadowed verification.

## Sources

- [NGINX — ngx_http_proxy_module reference](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) — Priority 1. `proxy_buffering`, `proxy_buffer_size`, `proxy_buffers`, `proxy_busy_buffers_size`, `proxy_temp_path`, `proxy_max_temp_file_size`, `proxy_connect_timeout`, `proxy_read_timeout`, `proxy_send_timeout`, `proxy_http_version`. Defaults and latency-impact descriptions.
- [NGINX — ngx_http_upstream_module reference](https://nginx.org/en/docs/http/ngx_http_upstream_module.html) — Priority 1. `keepalive`, `keepalive_requests`, `keepalive_timeout`, `keepalive_time`, `server max_conns`, `queue`, `zone`. `$upstream_queue_time` variable semantics. Defaults including `keepalive 32 local;` since 1.29.7.
- [NGINX — ngx_http_core_module reference](https://nginx.org/en/docs/http/ngx_http_core_module.html) — Priority 1. `worker_connections` placement, `client_body_buffer_size`, `large_client_header_buffers`, `keepalive_timeout`, `send_timeout`, `sendfile`, `tcp_nodelay`, `tcp_nopush`, `aio`. Defaults for client-side timeouts and buffering.
- [NGINX — ngx_http_ssl_module reference](https://nginx.org/en/docs/http/ngx_http_ssl_module.html) — Priority 1. `ssl_session_cache`, `ssl_session_timeout`, `ssl_session_tickets`, `ssl_protocols`, `ssl_ciphers`, `ssl_buffer_size`. TLS handshake cost and resumption mechanics.
- [NGINX Admin Guide — Logging](https://docs.nginx.com/nginx/admin-guide/monitoring/logging/) — Priority 1. `log_format` directive, `$request_time`, `$upstream_connect_time`, `$upstream_header_time`, `$upstream_response_time` semantics; comma-separated values for multi-upstream paths; `-` and `0` sentinels for failed/cached responses.
- [NGINX Admin Guide — Debugging](https://docs.nginx.com/nginx/admin-guide/monitoring/debugging/) — Priority 1. `error_log ... debug`, memory ring buffer (`error_log memory:32m debug;`), `debug_connection <ip>;` for targeted tracing, core dump capture.
- [Brendan Gregg — Linux Performance](https://www.brendangregg.com/linuxperf.html) — Priority 2. USE-method framing for network subsystems; `tcpretrans`, `tcplife`, `tcpconnect`, `tcpaccept` bcc tools for kernel-level TCP latency analysis; reference for the `nstat` and `/proc/net/netstat` counters used in Step 9.
