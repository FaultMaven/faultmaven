---
id: nginx-high-latency
title: "NGINX Request Latency Spikes"
domain: networking
service: nginx
symptom_class:
  - latency
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
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

### Step 1: Confirm latency and add timing variables if missing

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

### Step 2: Compute p50/p95/p99 of $request_time

```bash
tail -n 5000 /var/log/nginx/access.log \
  | grep -oE 'rt=[0-9.]+' | cut -d= -f2 | sort -n \
  | awk 'BEGIN{c=0} {a[c++]=$1} END{
      if(c==0){print "no_rt"; exit}
      printf "count=%d p50=%.3f p95=%.3f p99=%.3f max=%.3f\n",
        c, a[int(c*0.50)], a[int(c*0.95)], a[int(c*0.99)], a[c-1]}'
```

Expected output: a single line with `count=`, `p50=`, `p95=`, `p99=`, `max=`. Treat any sustained `p95 > 1.0` (1 s) or `p99 > 5.0` for a reverse-proxy workload as elevated; compare against the historical baseline if recorded.

### Step 3: Decompose request_time into per-stage components

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

### Step 4: Inspect connection counters and accept-queue backlog

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

### Step 6: Check whether responses are spilling to disk

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

### Step 8: Check upstream queue saturation and max_conns

```bash
tail -n 5000 /var/log/nginx/access.log \
  | grep -oE 'uqt="[0-9.-]+"' | cut -d'"' -f2 \
  | awk '{if($1!="-" && $1+0>0){n++; s+=$1; if($1+0>m)m=$1}} END{printf "queue_samples=%d avg=%.3f max=%.3f\n", n+0, (n?s/n:0), m+0}'
nginx -T 2>/dev/null | grep -E "max_conns|queue "
```

Expected output: `queue_samples=0` means no requests waited in the upstream queue. Non-zero `avg` or `max` indicates `max_conns` is set on the upstream and is being saturated; requests then wait in a per-upstream queue. The `queue` directive entry (`queue <N> timeout=<t>;`) confirms the bound.

### Step 9: Check host network path and conntrack saturation

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

### Cause A: Missing upstream keepalive forces a fresh handshake per request

**Statement:** The upstream block has no `keepalive` directive (or `proxy_http_version` is left at 1.0), so NGINX opens a new TCP connection to the upstream for every request, inflating `$upstream_connect_time`.

**Chain:**
- root: upstream block lacks `keepalive`/`proxy_http_version 1.1;`/`proxy_set_header Connection "";`, so NGINX closes the upstream socket after each response.
- s1: every subsequent request pays a fresh TCP three-way handshake (plus a TLS handshake on `proxy_pass https://`) before any application work begins.
- s2: under steady load the upstream RTT (and several extra RTTs for TLS) is added to every request's `$upstream_connect_time`.
- D: `$request_time` is inflated on every request while status stays 2xx/3xx (Symptom Recognition).

**Indicators:**
- root: [Step 5] `nginx -T | grep -E "keepalive|proxy_http_version"` shows no `keepalive` directive in the upstream block, or `proxy_http_version 1.0`, or the absence of `proxy_set_header Connection "";`.
- s2: [Step 3] decomposition shows `avg_uct` is the dominant term in `$request_time` (e.g., `uct >= 0.50 * rt`).

**Interventions:**
- **remediation** (root): add upstream keepalive and HTTP/1.1 with cleared Connection header.

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

  **Verification:** Repeat Step 3; `avg_uct` drops to near-zero on the second and subsequent requests against the same upstream. `ss -tnp 'dport = :<upstream-port>' | wc -l` shows a stable pool of `ESTAB` sockets rather than churning `TIME-WAIT`.
- **mitigation** (s1): stage the keepalive config and validate FD headroom before reload.

  ```bash
  cat >/etc/nginx/conf.d/upstream-keepalive.conf <<'EOF'
  # Inject into the relevant upstream block via include or edit in place
  # upstream <name> { ...; keepalive 32; keepalive_requests 1000; keepalive_timeout 60s; }
  EOF
  nginx -t
  ```

  **Risk:** Enabling upstream keepalive immediately increases the count of long-lived sockets held by NGINX workers; verify `worker_rlimit_nofile` is at least `2 * worker_connections` before reload. **Duration:** Permanent once verified; keepalive is the documented default-on configuration since NGINX 1.29.7. **Verification:** `nginx -t` passes and `worker_rlimit_nofile` confirmed ≥ `2 * worker_connections`.

### Cause B: Upstream application is slow to send response headers

**Statement:** The backend takes a long time to compute the response, so `$upstream_header_time` and `$upstream_response_time` dominate `$request_time` while NGINX itself is idle.

**Chain:**
- root: a slow database query, blocking external API call, or CPU-bound handler keeps the upstream silent past normal latency targets.
- s1: NGINX waits within `proxy_read_timeout` (default 60 s) for each read, so the request stays open and completes with a 2xx status.
- s2: the wait surfaces as elevated `$upstream_header_time`/`$upstream_response_time`; the latency is real but originates in the application, not NGINX.
- D: `$request_time` is elevated on routes traversing NGINX while NGINX counters are nominal (Symptom Recognition).

**Indicators:**
- root: [Symptom] upstream-side APM/tracing shows handler p95 elevated; NGINX CPU and connection counters are nominal.
- s2: [Step 3] `avg_uht` or `(avg_urt - avg_uht)` is the dominant term in `$request_time`.
- s2: [Step 10] client-side `time_starttransfer` is dominated by waiting for the first response byte from NGINX, matching the access log's high `$upstream_header_time`.

**Interventions:**
- **remediation** (root): fix the slow handler on the upstream tier, not on NGINX — optimise the query/dependency call, add a cache layer, or move synchronous work to a job queue and return 202.

  ```bash
  # Upstream-side examples — adapt to the stack in use
  # 1) Add a covering index for the dominant slow query
  # 2) Set a client timeout on the downstream dependency call (e.g., HTTP client read timeout)
  # 3) Cache the hot-path response in Redis with a short TTL
  kubectl rollout restart deployment/<upstream-deployment> -n <namespace>
  ```

  **Verification:** Re-run Step 3 after the upstream change; `avg_uht`/`avg_urt` returns to baseline. Step 10 against the same route shows `time_starttransfer` recovered.
- **mitigation** (s1): tighten `proxy_read_timeout` on the slow route to surface the problem as 504s rather than slow successes.

  ```bash
  # Per-location, override only for the slow route
  cat >/etc/nginx/conf.d/<route>-timeouts.conf <<'EOF'
  # location /reports/ { proxy_read_timeout 30s; proxy_send_timeout 30s; }
  EOF
  nginx -t
  ```

  **Risk:** Tightening `proxy_read_timeout` will convert slow but successful requests into 504s and surface the problem more loudly; only do so when you can absorb the error rate. **Duration:** Diagnostic only; push the durable fix into the upstream service. **Verification:** slow requests on the route now return 504 promptly instead of completing slowly with 2xx.

### Cause C: Worker connection pool saturated, requests queue in the accept-queue

**Statement:** Worker connections are fully consumed by in-flight requests and idle keepalives, so new connections wait in the kernel accept-queue before NGINX can handle them, adding constant latency before any application work.

**Chain:**
- root: each worker accepts up to `worker_connections` simultaneous client connections, and that ceiling is hit under load.
- s1: the kernel queues incoming `SYN`s in the listen-socket backlog (sized by `listen ... backlog=`, bounded by `net.core.somaxconn`); new clients wait until a worker drains a connection.
- s2: if the backlog overflows, the kernel increments `TcpExtListenOverflows` and drops `SYN`s, which clients retry after the SYN-retransmission timer.
- D: new connections incur constant establishment latency before any application work (Symptom Recognition).

**Indicators:**
- root: [Step 4] `stub_status` reports `Active connections` close to `worker_processes * worker_connections`, and `Waiting` similarly close to capacity.
- s1: [Step 4] `ss -tln 'sport = :443'` shows `Recv-Q > 0` on the NGINX listen socket.
- s2: [Step 9] `nstat`/`/proc/net/netstat` shows `TcpExtListenOverflows` or `TcpExtListenDrops` incrementing during the latency window.

**Interventions:**
- **remediation** (root): raise `worker_connections` and FD limits, enlarge the listen backlog, and widen `somaxconn`.

  ```nginx
  # nginx.conf — top level
  worker_processes auto;
  worker_rlimit_nofile 65535;

  events {
      worker_connections 16384;
      multi_accept on;
  }

  # Raise the per-server backlog above the kernel default (4096) if overflowing
  # server { listen 443 ssl backlog=8192; }
  ```

  ```bash
  # Widen somaxconn so accept() can drain the larger backlog
  sysctl -w net.core.somaxconn=8192
  echo 'net.core.somaxconn=8192' >/etc/sysctl.d/99-nginx.conf
  mkdir -p /etc/systemd/system/nginx.service.d
  cat >/etc/systemd/system/nginx.service.d/limits.conf <<'EOF'
  [Service]
  LimitNOFILE=65535
  EOF
  systemctl daemon-reload
  systemctl restart nginx
  ```

  **Verification:** Repeat Step 4; `Active connections` stays below the new ceiling under load, listen-socket `Recv-Q` stays at 0, and `nstat TcpExtListenOverflows` stops incrementing.
- **mitigation** (s1): lift the FD ceiling on the running master pending the restart.

  ```bash
  prlimit --nofile=65535:65535 --pid $(cat /run/nginx.pid)
  ```

  **Risk:** Raising `worker_connections` without `worker_rlimit_nofile` exhausts FDs (`socket() failed (24: Too many open files)`); raise both together. **Duration:** Until restart — `prlimit` does not persist; pair with the remediation in the same window. **Verification:** error log clears `Too many open files` and `Recv-Q` trends back toward 0.

### Cause D: Proxy response buffers undersized, NGINX spills to disk

**Statement:** `proxy_buffers` capacity is smaller than the upstream response payload, so NGINX writes responses to `proxy_temp_path` on the local disk and serves them from disk, adding I/O latency to every large response.

**Chain:**
- root: with `proxy_buffering on` (default), `proxy_buffers <count> <size>` is sized below the response payload for the affected route.
- s1: once the in-memory buffers fill and `proxy_busy_buffers_size` is reached, NGINX writes the overflow to a temp file under `proxy_temp_path` and emits `an upstream response is buffered to a temporary file`.
- s2: each overflowing request pays an extra disk write and read; on slow or contended storage this adds tens to hundreds of milliseconds.
- D: large responses carry extra I/O latency, inflating `client_overhead` in `$request_time` (Symptom Recognition).

**Indicators:**
- s1: [Step 6] error log contains `an upstream response is buffered to a temporary file`.
- s1: [Step 6] `ls /var/cache/nginx/proxy_temp/` shows files larger than zero accumulating during the latency window.
- s2: [Step 3] `client_overhead = avg_rt - avg_urt` is significant (>50 ms) while `avg_uct`/`avg_uht`/`avg_urt` are nominal.

**Interventions:**
- **remediation** (root): size `proxy_buffers` against the route's 95th-percentile payload and forbid disk spill at the location scope.

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

  **Verification:** Repeat Step 6; no new `an upstream response is buffered to a temporary file` lines appear and `/var/cache/nginx/proxy_temp/` stays empty under representative load. Step 3 shows `client_overhead` returning to single-digit ms.
- **defensive_fix** (s1): raise the buffer drop-in globally so overflow stops spilling to disk while route-level sizing is finalised.

  ```bash
  cat >/etc/nginx/conf.d/proxy-buffers.conf <<'EOF'
  proxy_buffer_size 16k;
  proxy_buffers 16 16k;
  proxy_busy_buffers_size 32k;
  EOF
  nginx -t && nginx -s reload
  ```

  **Verification:** Step 6 shows the temp-file warnings stop and `/var/cache/nginx/proxy_temp/` stays empty under representative load.

### Cause E: TLS handshake cost on every new client connection

**Statement:** Clients pay a full TLS handshake on every connection because session resumption is not configured, so new-connection latency is RTT-bound and `time_appconnect` dominates time-to-first-byte for short-lived clients.

**Chain:**
- root: `ssl_session_cache none` (default) and `ssl_session_tickets off` mean no session resumption is offered, so every client negotiates a full handshake on every connection.
- s1: a full TLS 1.2 handshake adds 2 RTTs plus asymmetric crypto (TLS 1.3 reduces that to 1 RTT); short-lived clients pay it on every request.
- s2: `time_appconnect` dominates time-to-first-byte on new connections, while reused keepalive connections are unaffected.
- D: latency p99 spikes on new-connection events (mobile roams, LB fail-over, scraper bursts) (Symptom Recognition).

**Indicators:**
- root: [Step 7] config shows neither `ssl_session_cache shared:...` nor `ssl_session_tickets on;`.
- s2: [Step 7] `curl -w "ssl=%{time_appconnect}s"` reports >100 ms on a new connection while reused connections show `ttfb` well below `ssl`.
- s2: [Symptom] latency p99 spikes correlate with connection-establishment events (mobile network roams, load-balancer fail-over, scraper bursts).

**Interventions:**
- **remediation** (root): enable a shared resumption cache and stateless tickets, and modernise the TLS protocol set.

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

  **Verification:** Repeat Step 7. The second and third `curl --next ...` runs against the same host now report `ttfb` well below the original `ssl=` figure; on TLS 1.3 capable clients the handshake cost on a new connection drops to a single RTT.

### Cause F: Upstream connection limit (max_conns) saturated, requests queue

**Statement:** Each upstream server has a `max_conns` cap that has been reached, so additional requests wait in NGINX's per-upstream queue and `$upstream_queue_time` becomes non-zero.

**Chain:**
- root: `max_conns=<N>` on an `upstream server` caps concurrent in-flight requests per member, and all members are at their cap.
- s1: additional requests park in the upstream queue declared by `queue <N> timeout=<t>;`; time spent there is reported as `$upstream_queue_time`.
- s2: queue time adds to `$request_time` even though the request never reached the application; if `queue timeout` expires before a slot opens, NGINX returns 502.
- D: `$upstream_queue_time` becomes non-zero and inflates `$request_time` (Symptom Recognition).

**Indicators:**
- root: [Step 8] `nginx -T | grep -E "max_conns|queue "` shows `max_conns=` on upstream servers and a `queue` directive in the upstream block.
- s1: [Step 8] `awk` summary over access logs reports `queue_samples > 0` with non-trivial `avg`/`max` queue time.
- s2: [Step 3] `avg_rt` is materially higher than `avg_urt + avg_uct + avg_uht`, with the difference matching `avg_uqt`.

**Interventions:**
- **remediation** (root): raise the per-server cap to match verified upstream capacity, or add upstream members to spread load.

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

  **Verification:** Step 8 reports `queue_samples=0` (or matches baseline) over a 10-minute window under representative load. Upstream-side metrics show in-flight request count below the new `max_conns` ceiling.
- **mitigation** (s1): widen `max_conns` on a single member while upstream capacity is validated.

  ```bash
  # Temporary widening on a single upstream member while validating
  # Edit the relevant upstream block:
  # upstream app_backend { server backend-1.internal:8080 max_conns=200; ... }
  nginx -t
  ```

  **Risk:** Raising `max_conns` without confirming upstream capacity shifts the bottleneck downstream and can collapse the application; confirm upstream-side concurrency headroom first. **Duration:** Hours, while upstream capacity is verified or scaled. **Verification:** Step 8 shows `queue_samples` falling toward 0 with no rise in upstream-side error rate.

### Cause G: Kernel conntrack table saturated, new connections stall

**Statement:** The kernel netfilter conntrack table is at or near its maximum, so new connections to or from NGINX wait for table slots to free, adding constant connection-establishment latency.

**Chain:**
- root: every stateful firewalled connection consumes one `nf_conntrack` entry, and `nf_conntrack_count` has reached `nf_conntrack_max`.
- s1: the kernel must reclaim a slot (typically by expiring a `TIME-WAIT` entry) before accepting a new connection, logging `nf_conntrack: table full, dropping packet`.
- s2: inbound `SYN`s are dropped or held; the client retries via TCP SYN retransmit at ~1 s intervals, surfacing as multi-second connection establishment.
- D: new connections incur constant establishment latency (Symptom Recognition).

**Indicators:**
- root: [Step 9] `cat /proc/sys/net/netfilter/nf_conntrack_count` is within 5% of `nf_conntrack_max`.
- s1: [Step 9] `dmesg | grep nf_conntrack` shows `table full, dropping packet`.
- s2: [Step 3] `avg_uct` is elevated only on new connections and recovers when upstream keepalive is in use.

**Interventions:**
- **remediation** (root): raise `nf_conntrack_max`, shorten TIME-WAIT timeout, and resize the hash via a persistent sysctl drop-in.

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

  **Verification:** Step 9 reports `nf_conntrack_count` well below `nf_conntrack_max` under the same load, and `dmesg | grep nf_conntrack` shows no further table-full lines for at least 30 minutes.
- **mitigation** (s1): raise the conntrack ceiling live and shorten TIME-WAIT to free slots immediately.

  ```bash
  sysctl -w net.netfilter.nf_conntrack_max=1048576
  sysctl -w net.netfilter.nf_conntrack_tcp_timeout_time_wait=30
  ```

  **Risk:** Raising `nf_conntrack_max` increases kernel memory consumption (~300 bytes per entry); confirm host RAM headroom before raising. **Duration:** Until reboot; persist with a sysctl drop-in (remediation step) before the next restart. **Verification:** `nf_conntrack_count` drops below `nf_conntrack_max` and `dmesg` shows no further table-full lines.

### Cause H: Logging I/O blocks worker threads

**Statement:** Synchronous `access_log` writes to slow or contended storage block worker threads, adding latency proportional to disk write time to every request.

**Chain:**
- root: by default NGINX writes each access-log line synchronously inside the worker that handled the request, and `/var/log/nginx/` is on slow, contended, or full storage.
- s1: the worker stalls while waiting for each log write to complete (high `await` in `iostat`).
- s2: the per-request stall adds to `client_overhead` in `$request_time` independent of upstream latency.
- D: every request carries latency proportional to disk write time (Symptom Recognition).

**Indicators:**
- root: [Step 9] `df -h /var/log/nginx/` shows the partition >95% full, or `dmesg` shows EXT4/XFS errors on the log device.
- s1: [Step 6] `iostat -xz 1` shows the device backing `/var/log` with sustained `%util > 70` or `await > 20` during the latency window.
- s2: [Step 3] `client_overhead = avg_rt - avg_urt` is elevated and tracks log-write latency rather than upstream latency.

**Interventions:**
- **remediation** (root): enable buffered logging in the http block (and place `/var/log/nginx/` on suitable storage).

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

  **Verification:** Step 6 shows `iostat` `%util` on the log device drops below 30% under the same load. Step 3 reports `client_overhead` recovered to baseline.
- **defensive_fix** (s1): switch the access log to a buffered drop-in to amortise writes immediately.

  ```bash
  # Move logs to a faster volume or to syslog if the disk is the bottleneck
  cat >/etc/nginx/conf.d/access-log.conf <<'EOF'
  access_log /var/log/nginx/access.log timing buffer=64k flush=5s;
  EOF
  nginx -t && nginx -s reload
  ```

  **Verification:** Step 6 shows the log device `%util` drops below 30% and Step 3 shows `client_overhead` recovering to baseline.

### Cause Z: Unidentified

**Statement:** Diagnostic steps confirmed elevated `$request_time` in NGINX but did not match the indicators for Causes A through H.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the proxy owner / platform on-call.

  ```bash
  # Snapshot active config, recent error log, connection state, and a single slow trace
  nginx -T > /tmp/nginx-conf.dump 2>&1
  tail -n 1000 /var/log/nginx/error.log > /tmp/nginx-error.tail
  ss -tnp state established | head -100 > /tmp/nginx-ss.snap
  tail -n 5000 /var/log/nginx/access.log | awk '/rt=[5-9]\.|rt=[0-9]{2,}\./' > /tmp/nginx-slow.access
  ```

  **Risk:** Enabling `error_log ... debug;` on a busy proxy fills disk rapidly; prefer `error_log memory:32m debug;` (a memory ring buffer) on hot hosts. **Duration:** Minutes — collect, hand off, then revert any temporary debug logging. **Verification:** Package the captured artefacts plus the Step 3 timing summary and escalate with the affected route, latency window, and upstream identifier; hand-off acknowledged and an incident ticket opened with a follow-up owner assigned.

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
