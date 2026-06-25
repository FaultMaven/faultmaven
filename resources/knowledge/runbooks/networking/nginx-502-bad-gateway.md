---
id: nginx-502-bad-gateway
title: "NGINX 502 Bad Gateway"
domain: networking
service: nginx
symptom_class:
  - service_unavailable
  - connection_refused
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: kb-researcher
status: draft
tags:
  - nginx
  - "502"
  - bad-gateway
  - upstream
  - reverse-proxy
  - timeout
  - tls
  - dns
difficulty: intermediate
---

# NGINX 502 Bad Gateway

## Symptom Recognition

- Clients receive HTTP `502 Bad Gateway` responses; NGINX is the responding server (`Server: nginx` header) and the request reached the proxy successfully.
- NGINX access log shows status `502` with a non-zero `$upstream_response_time` or a literal `-` in the `$upstream_addr` field when no backend was reachable.
- NGINX error log lines mention `while connecting to upstream`, `while reading response header from upstream`, or `while sending request to upstream`.
- Common error-log strings: `connect() failed (111: Connection refused) while connecting to upstream`, `upstream prematurely closed connection while reading response header from upstream`, `upstream sent too big header while reading response header from upstream`, `no live upstreams while connecting to upstream`, `upstream timed out (110: Connection timed out) while connecting to upstream`, `SSL_do_handshake() failed`, `no resolver defined to resolve <hostname>`.
- For SELinux-enforced hosts, the audit log shows `type=AVC` denials with `comm="nginx"` and `name_connect` permission denied around the time of the 502.
- 502 rate spikes correlate with upstream process restarts, deploys, or network/firewall changes; correlated upstream CPU/memory exhaustion or worker-pool saturation may be visible in upstream-side telemetry.

## Applicability

- NGINX OSS 1.18+ or NGINX Plus R25+ running on Linux (bare metal, VM, or container) as a reverse proxy or load balancer (`proxy_pass`, `fastcgi_pass`, `uwsgi_pass`, `grpc_pass`).
- Read access to the NGINX configuration tree (default `/etc/nginx/`) and error log (default `/var/log/nginx/error.log`) on the proxy host.
- Privilege to reload NGINX (`nginx -s reload` or `systemctl reload nginx`) after configuration changes.
- `curl`, `nc` (or `ncat`), `ss`, `dig`, and `openssl s_client` available on the proxy host for connectivity testing.
- For SELinux investigations: `ausearch`, `getenforce`, `setsebool`, `semanage` (policycoreutils-python or equivalent package).
- For Kubernetes/ingress-nginx deployments: `kubectl` with `get`, `logs`, and `exec` on the ingress-controller namespace.

## Diagnostic Steps

### Step 1: Confirm 502 is served by NGINX and capture an error-log excerpt

```bash
curl -sS -o /dev/null -w "status=%{http_code}\nserver=%{header.server}\n" https://<host>/<path>
tail -n 200 /var/log/nginx/error.log | grep -E "upstream|SSL_do_handshake|resolver"
```

Expected output: `status=502` and `server=nginx/<version>` from `curl`, plus one or more error lines from the log naming the upstream and the failure mode (for example `connect() failed (111: Connection refused) while connecting to upstream`).

### Step 2: Identify the upstream definition and proxy_pass target

```bash
nginx -T 2>/dev/null | grep -E "upstream |server |proxy_pass|fastcgi_pass" | sed -n '1,200p'
```

Expected output: the `upstream` block(s), each `server <addr>:<port>` line inside them, and the `proxy_pass`/`fastcgi_pass` directives referencing them. Note the exact hostnames, ports, and Unix socket paths in use.

### Step 3: Test direct TCP connectivity from the NGINX host to each upstream

```bash
for endpoint in <host1>:<port> <host2>:<port>; do
  echo "=== $endpoint ==="
  nc -zv -w 3 ${endpoint%:*} ${endpoint##*:}
done
ss -tnp | grep -E "ESTAB|SYN-SENT" | grep -E "<upstream-port>"
```

Expected output: `nc -zv` should print `Connection ... succeeded`. `Connection refused`, `No route to host`, or a timeout indicates the upstream is down, the port is wrong, or a firewall is blocking the path. `ss` shows whether NGINX workers currently have any established sessions to the upstream port.

### Step 4: Verify the upstream process is listening on the expected address and port

```bash
# Run on the upstream host (or kubectl exec into the upstream pod)
ss -tlnp | grep -E ":<upstream-port>\b"
systemctl status <upstream-service> --no-pager | head -20
```

Expected output: a `LISTEN` line bound to `0.0.0.0:<port>`, `[::]:<port>`, `127.0.0.1:<port>`, or the Unix socket path referenced by `fastcgi_pass`. An empty result means the process is not running or is bound to a different address; `systemctl status` confirms whether the service is `active (running)`.

### Step 5: Resolve the upstream hostname from the NGINX host

```bash
dig +short <upstream-host>
getent hosts <upstream-host>
grep -E "resolver |resolver_timeout" /etc/nginx/nginx.conf /etc/nginx/conf.d/*.conf 2>/dev/null
```

Expected output: one or more A/AAAA records from `dig`; a populated `getent` line; and an existing `resolver <dns-ip> valid=<ttl>;` directive in the NGINX config when variables are used in `proxy_pass`. Empty `dig` output, `NXDOMAIN`, or a missing `resolver` directive (combined with a `no resolver defined to resolve` error in Step 1) points to DNS as the root cause.

### Step 6: Validate the TLS handshake to HTTPS upstreams

```bash
openssl s_client -connect <upstream-host>:<port> -servername <upstream-host> -showcerts </dev/null 2>&1 | \
  grep -E "Verify return code|subject=|issuer=|verify error|alert"
```

Expected output: `Verify return code: 0 (ok)` and certificate `subject=` matching the `proxy_ssl_name`/`$proxy_host` NGINX uses. Any `verify error`, `alert handshake failure`, `tlsv1 alert protocol version`, or `wrong version number` indicates a TLS protocol, cipher, SNI, or certificate-chain mismatch between NGINX and the upstream.

### Step 7: Measure the 502 rate and correlate with upstream health

```bash
awk '$9==502 {c++} END {print "502_count="c}' /var/log/nginx/access.log
awk '{print $9}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head
# If Prometheus + nginx-exporter is in place:
# sum(rate(nginx_http_requests_total{status="502"}[5m])) / sum(rate(nginx_http_requests_total[5m]))
```

Expected output: a 502 count for the current log window and the relative share of 502 vs. 2xx/3xx. A 502 ratio above ~1% sustained for >5 minutes is operationally significant; a brief spike aligned with an upstream restart is a deploy artefact rather than a chronic fault.

### Step 8: Inspect SELinux denials when running on RHEL/CentOS/Rocky/Alma

```bash
getenforce
ausearch -m AVC -ts recent | grep -i nginx | tail -20
```

Expected output: `Enforcing` from `getenforce` plus AVC lines such as `denied { name_connect } for ... comm="nginx" ... scontext=...:httpd_t:s0 tcontext=...:http_port_t:s0` referencing the upstream port. Empty `ausearch` output rules SELinux out as the proximal cause.

### Step 9: Check NGINX worker file-descriptor and connection limits

```bash
NGINX_PID=$(cat /run/nginx.pid 2>/dev/null || pgrep -of "nginx: master")
cat /proc/$NGINX_PID/limits | grep -E "Max open files|Max processes"
ss -s
grep -E "worker_connections|worker_rlimit_nofile" /etc/nginx/nginx.conf
```

Expected output: `Max open files` (soft and hard) at or above `worker_connections * 2` for the master process; `ss -s` shows TCP socket count well below the limit. A `Max open files` value of `1024` combined with `worker_connections 4096;` in the config means NGINX runs out of FDs under load, which surfaces as 502 with `socket() failed (24: Too many open files)` in the error log.

### Step 10: Capture upstream response-header size when "too big header" is suspected

```bash
curl -sI -o /dev/null -D - http://<upstream-host>:<port>/<path> | wc -c
curl -sI http://<upstream-host>:<port>/<path> | awk 'END{print "header_count="NR}'
```

Expected output: total response-header bytes and number of header fields. Header payloads >4 KB (the default `proxy_buffer_size`) or >8 KB (the typical `large_client_header_buffers` slot) cause `upstream sent too big header`. Common offenders are Set-Cookie storms, OAuth bearer/refresh tokens echoed back, and CSP headers.

## Causes

### Cause A: Upstream process not listening on the expected address or port

**Statement:** The backend service that NGINX targets via `proxy_pass`/`fastcgi_pass` is not running or is not bound to the address and port NGINX is configured to connect to.

**Chain:**
- root: the upstream process is down or bound to the wrong address/port, so no socket is in `LISTEN` state for the target port.
- s1: NGINX initiates a TCP connection and the upstream kernel returns `RST`.
- s2: NGINX logs `connect() failed (111: Connection refused) while connecting to upstream` and, with no live retry target, responds 502 to the client.
- D: clients receive HTTP 502 Bad Gateway (Symptom Recognition).

**Indicators:**
- s2: [Step 1] error log contains `connect() failed (111: Connection refused) while connecting to upstream`
  <!-- match: {"step": 1, "predicate": "contains", "target": "connect() failed (111: Connection refused) while connecting to upstream"} -->
- s1: [Step 3] `nc -zv` to the upstream `host:port` prints `Connection refused` or returns non-zero
  <!-- match: {"step": 3, "predicate": "contains", "target": "Connection refused"} -->
- root: [Step 4] `ss -tlnp` on the upstream host has no `LISTEN` line for the expected port

**Interventions:**
- **mitigation** (root): restart the upstream to restore the `LISTEN` socket while investigating why it stopped.

  ```bash
  systemctl restart <upstream-service>
  systemctl status <upstream-service> --no-pager
  ```

  **Risk:** Masks why the service stopped; a reproducible crash will fail again within seconds, so dig into the upstream logs in parallel. **Duration:** Minutes — replace with a fix within the same incident. **Verification:** `systemctl status` shows `active (running)` and Step 4 `ss -tlnp` now lists the expected port.
- **remediation** (root): identify the failure mode in the upstream's own logs and fix it there so the service binds reliably.

  ```bash
  journalctl -u <upstream-service> -n 200 --no-pager
  # Common follow-ups: bind to the address NGINX expects, raise memory_limit/heap, fix a config error, or correct the systemd ExecStart.
  systemctl enable --now <upstream-service>
  ```

  **Verification:** Run `curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/<path>` five times; every response is 2xx/3xx. `awk '$9==502 {c++} END {print c+0}' /var/log/nginx/access.log` shows no new 502 entries (re-run Step 7) for the next 10 minutes.

### Cause B: Connect timeout to upstream because the network path is slow or partitioned

**Statement:** NGINX cannot complete the TCP handshake to a healthy upstream within `proxy_connect_timeout` because a firewall, security group, network policy, or routing failure silently drops `SYN` packets.

**Chain:**
- root: a firewall, security group, NetworkPolicy, or routing change silently drops `SYN` packets on the NGINX-to-upstream path.
- s1: NGINX waits the full `proxy_connect_timeout` (default 60 s) with no `RST` returned.
- s2: the timeout elapses and NGINX logs `upstream timed out (110: Connection timed out) while connecting to upstream` and responds 502.
- D: clients receive HTTP 502 Bad Gateway (Symptom Recognition).

**Indicators:**
- s2: [Step 1] error log contains `upstream timed out (110: Connection timed out) while connecting to upstream`
  <!-- match: {"step": 1, "predicate": "contains", "target": "upstream timed out (110: Connection timed out) while connecting to upstream"} -->
- s1: [Step 3] `nc -zv` hangs until its own `-w` timeout, then exits non-zero (no `refused` text in output)
- root: [Step 4] the upstream's `ss -tlnp` does show a `LISTEN` socket on the expected port (process is healthy; the path is broken)

**Interventions:**
- **mitigation** (s1): raise `proxy_connect_timeout` only if the upstream is genuinely slow to accept connections; otherwise use the telnet probe to confirm the path is dropping `SYN`.

  ```bash
  curl -v --max-time 10 telnet://<upstream-host>:<port>
  ```

  **Risk:** Raising the timeout hides the partition behind longer client latency but does not restore connectivity. **Duration:** Diagnostic only — do not leave timeout adjustments in place once the path is restored. **Verification:** the probe connects within the window, or confirms the hang isolates the path.
- **remediation** (root): restore the dropped network path (firewall/SG/NetworkPolicy/route) using the change-management tool that mutated it.

  ```bash
  ip route get <upstream-ip>
  sudo iptables -L -n -v | grep -E "<upstream-port>|<upstream-ip>"
  # AWS: re-add the SG ingress rule for the proxy SG on the upstream port.
  # Kubernetes: confirm a matching NetworkPolicy egress rule + a corresponding upstream ingress rule.
  kubectl get networkpolicy -A
  ```

  **Verification:** `nc -zv -w 3 <upstream-host> <upstream-port>` from the NGINX host now prints `succeeded` (re-run Step 3). `tail -f /var/log/nginx/error.log` shows no new `Connection timed out` entries for at least 10 minutes under load.

### Cause C: Upstream prematurely closes the connection mid-response

**Statement:** The upstream accepts the TCP connection but its worker process or request handler aborts before sending complete response headers, so NGINX returns 502 on the half-read response.

**Chain:**
- root: an application-tier fault (crash, OOM kill, signal shutdown without draining, or `pm.max_requests` recycling) terminates the upstream worker mid-request.
- s1: NGINX is left with a half-read response and cannot recover the in-flight request body (especially with `proxy_request_buffering off`).
- s2: NGINX logs `upstream prematurely closed connection while reading response header from upstream` and propagates the failure as 502.
- D: clients receive HTTP 502 Bad Gateway (Symptom Recognition).

**Indicators:**
- s2: [Step 1] error log contains `upstream prematurely closed connection while reading response header from upstream`
  <!-- match: {"step": 1, "predicate": "contains", "target": "upstream prematurely closed connection while reading response header from upstream"} -->
- root: [Step 4] upstream status shows recent restarts, OOM kills (`dmesg | grep -i killed`), or `pm.max_children`/`pm.max_requests` recycling warnings in its log
- root: [Step 7] 502 spikes coincide with upstream deploy/restart events or memory-pressure alerts on the upstream tier

**Interventions:**
- **defensive_fix** (s1): let NGINX retry the most common transient upstream failures so a single aborted connection does not surface as a client 502.

  ```bash
  cat >/etc/nginx/conf.d/upstream-retry.conf <<'EOF'
  proxy_next_upstream error timeout http_502 http_503 http_504;
  proxy_next_upstream_tries 3;
  proxy_next_upstream_timeout 10s;
  EOF
  nginx -t && nginx -s reload
  ```

  **Verification:** over a 15-minute window `awk '$9==502 {c++} END {print c+0}' /var/log/nginx/access.log` returns 0 or pre-incident baseline; transient aborts no longer reach clients.
- **mitigation** (s1): bound the retry blast radius while the upstream is still unstable.

  ```bash
  # Already bounded above; reduce tries if retries amplify load on a struggling backend
  proxy_next_upstream_tries 2;
  ```

  **Risk:** Retries add up to 3x request load to an already-struggling pool during incidents. **Duration:** Hours to days — resolve the upstream crashes within the next sprint. **Verification:** upstream CPU/memory pressure does not worsen after enabling retries.
- **remediation** (root): fix the upstream fault that aborts the worker (segfault/OOM, handler bug, or recycling under load).

  ```bash
  # 1) PHP-FPM segfault or OOM
  sed -i 's/^memory_limit = .*/memory_limit = 256M/' /etc/php.ini
  systemctl restart php-fpm
  # 2) Node.js/Python worker crash — patch the handler, ship a new image, re-roll
  kubectl rollout restart deployment/<upstream-deployment> -n <namespace>
  # 3) Worker recycling under load — raise pm.max_children and pm.max_requests
  sed -i 's/^pm.max_children = .*/pm.max_children = 50/' /etc/php-fpm.d/www.conf
  sed -i 's/^pm.max_requests = .*/pm.max_requests = 1000/' /etc/php-fpm.d/www.conf
  systemctl restart php-fpm
  ```

  **Verification:** over 15 minutes `awk '$9==502 {c++} END {print c+0}' /var/log/nginx/access.log` returns 0 (or baseline) and `grep "upstream prematurely closed" /var/log/nginx/error.log | wc -l` shows no growth.

### Cause D: Read or send timeout because the upstream is slow to respond

**Statement:** The upstream eventually responds but takes longer than `proxy_read_timeout` (or `proxy_send_timeout`) between reads, so NGINX closes the connection and returns 502.

**Chain:**
- root: a slow backend operation (long DB query, partial chunk streaming, or a slow third-party API) leaves the upstream alive but silent past `proxy_read_timeout` (default 60 s).
- s1: NGINX exceeds the inter-read timeout and terminates the upstream connection.
- s2: NGINX logs `upstream timed out (110: Connection timed out) while reading response header from upstream` and returns 502.
- D: clients receive HTTP 502 Bad Gateway (Symptom Recognition).

**Indicators:**
- s2: [Step 1] error log contains `upstream timed out` followed by `while reading response header from upstream` or `while reading upstream`
  <!-- match: {"step": 1, "predicate": "contains", "target": "upstream timed out (110: Connection timed out) while reading"} -->
- root: [Step 7] access-log entries for affected routes show `$upstream_response_time` near or equal to the configured `proxy_read_timeout` value
- root: [Step 4] upstream service is healthy (`active (running)`) and listening; CPU is high or single requests take >30 s in upstream-side traces

**Interventions:**
- **mitigation** (s1): widen the per-location timeout for an endpoint known to be slow while the upstream is being optimised.

  ```nginx
  # Per-location override for an endpoint known to be slow
  proxy_connect_timeout 30s;
  proxy_send_timeout 300s;
  proxy_read_timeout 300s;
  ```

  **Risk:** Increasing `proxy_read_timeout` raises client wait time and ties up worker connections longer; combine with bounded `proxy_next_upstream_timeout`. **Duration:** Hours, while the upstream is optimised — long-term, push slow work into a job queue and return 202. **Verification:** the slow route returns 2xx and the inter-read timeout is no longer tripped.
- **defensive_fix** (s1): apply the widened timeout only to the slow route, not globally, so other routes keep tight failure detection.

  ```nginx
  # /etc/nginx/conf.d/<route>.conf — apply only to the slow route, not globally
  location /reports/ {
      proxy_pass http://reports_backend;
      proxy_connect_timeout 30s;
      proxy_send_timeout 300s;
      proxy_read_timeout 300s;
  }
  ```

  ```bash
  nginx -t && nginx -s reload
  ```

  **Verification:** `time curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/<slow-path>` returns 2xx with `$upstream_response_time` well below the new `proxy_read_timeout`; the error log shows no `upstream timed out ... while reading` entries for the next 30 minutes.
- **remediation** (root): remove the slow operation from the synchronous path so the upstream responds within the timeout (optimise the query, cache, or move long work to an async job returning 202).

  ```bash
  # Optimise the slow upstream operation, then confirm the synchronous path is fast.
  time curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/<slow-path>
  ```

  **Verification:** re-run the representative slow request (Step 7); `$upstream_response_time` is well below `proxy_read_timeout` even without the widened override.

### Cause E: TLS handshake to the upstream fails

**Statement:** NGINX cannot complete the TLS handshake with an HTTPS upstream because of a protocol-version mismatch, cipher mismatch, missing or incorrect SNI, or certificate-verification failure.

**Chain:**
- root: NGINX's `proxy_ssl_*` settings are incompatible with the upstream — excluded protocol/cipher, missing `proxy_ssl_server_name`, or an outdated `proxy_ssl_trusted_certificate` CA bundle.
- s1: OpenSSL returns an error mid-handshake (`tlsv1 alert protocol version`, `wrong version number`, or `certificate verify failed`).
- s2: NGINX logs `SSL_do_handshake() failed` referencing the upstream and responds 502.
- D: clients receive HTTP 502 Bad Gateway (Symptom Recognition).

**Indicators:**
- s2: [Step 1] error log contains `SSL_do_handshake() failed` referencing the upstream
  <!-- match: {"step": 1, "predicate": "contains", "target": "SSL_do_handshake() failed"} -->
- s1: [Step 6] `openssl s_client` to the upstream prints `Verify return code: <non-zero>` or an `alert handshake failure`
- root: [Step 6] certificate `subject=` does not match the hostname NGINX uses (mismatch between `proxy_pass`/`proxy_ssl_name` and the certificate SAN)

**Interventions:**
- **mitigation** (s1): inspect the offered certificate to isolate which TLS attribute mismatches; temporarily disabling `proxy_ssl_verify` is diagnostic only.

  ```bash
  openssl s_client -connect <upstream-host>:443 -servername <upstream-host> -showcerts </dev/null \
    | openssl x509 -noout -subject -issuer -dates
  ```

  **Risk:** Disabling `proxy_ssl_verify` accepts man-in-the-middle risk; only use as a diagnostic step and revert within the same change window. **Duration:** Minutes — the TLS mismatch is a config issue, fixed at remediation. **Verification:** the printed `subject`/`issuer`/`dates` identify the mismatched protocol, SNI, or cert.
- **remediation** (root): align `proxy_ssl_*` to the upstream and refresh the CA bundle.

  ```nginx
  # /etc/nginx/conf.d/<route>.conf
  location /api/ {
      proxy_pass https://api_backend;
      proxy_ssl_protocols TLSv1.2 TLSv1.3;
      proxy_ssl_ciphers HIGH:!aNULL:!MD5;
      proxy_ssl_server_name on;
      proxy_ssl_name api.example.com;
      proxy_ssl_verify on;
      proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
      proxy_ssl_verify_depth 2;
  }
  ```

  ```bash
  update-ca-certificates    # Debian/Ubuntu
  # or: update-ca-trust extract   # RHEL/CentOS/Rocky/Alma
  nginx -t && nginx -s reload
  ```

  **Verification:** `openssl s_client -connect <upstream-host>:443 -servername <upstream-host> </dev/null 2>&1 | grep "Verify return code"` returns `Verify return code: 0 (ok)` (re-run Step 6); `curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/api/<path>` returns 2xx and the error log shows no new `SSL_do_handshake() failed` entries.

### Cause F: Upstream hostname cannot be resolved by NGINX

**Statement:** NGINX cannot resolve the upstream's DNS name at request time because the `resolver` directive is missing, the configured DNS server is unreachable, or the hostname does not exist.

**Chain:**
- root: a variable `proxy_pass` (e.g. `proxy_pass http://$backend`) or a `server ... resolve` directive triggers runtime DNS, but no `resolver` is configured (or its DNS server is unreachable / the record is missing).
- s1: NGINX cannot perform the runtime lookup and logs `no resolver defined to resolve <hostname>` or `<hostname> could not be resolved`.
- s2: the request returns 502.
- D: clients receive HTTP 502 Bad Gateway (Symptom Recognition).

**Indicators:**
- s1: [Step 1] error log contains `no resolver defined to resolve` or `could not be resolved (3: Host not found)`
  <!-- match: {"step": 1, "predicate": "contains", "target": "could not be resolved"} -->
- root: [Step 5] `dig +short <upstream-host>` returns empty or `NXDOMAIN`
- root: [Step 5] `grep "resolver " /etc/nginx/nginx.conf /etc/nginx/conf.d/*.conf` returns no matches even though `proxy_pass` uses a variable

**Interventions:**
- **mitigation** (root): confirm which resolver to use before configuring it permanently; prefer the platform's internal DNS.

  ```bash
  getent hosts <upstream-host>
  cat /etc/resolv.conf
  ```

  **Risk:** Pointing `resolver` at an unauthenticated public DNS server (e.g. `8.8.8.8`) leaks internal hostname queries. **Duration:** Minutes — use only while validating which resolver to configure permanently. **Verification:** `getent hosts` resolves the name via the intended internal DNS.
- **remediation** (root): add the `resolver` directive (and zone for resolvable upstreams) so runtime DNS succeeds.

  ```nginx
  # /etc/nginx/nginx.conf — http block
  http {
      resolver 10.0.0.2 valid=30s ipv4=on ipv6=off;
      resolver_timeout 5s;

      upstream dynamic_backend {
          zone dynamic_backend 64k;
          server backend.internal.example.com resolve;
      }

      server {
          location / {
              proxy_pass http://dynamic_backend;
          }
      }
  }
  ```

  ```bash
  nginx -t && nginx -s reload
  ```

  **Verification:** `curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/<path>` returns 2xx for routes that proxy via the dynamic upstream; `tail -f /var/log/nginx/error.log` shows no further `could not be resolved` entries.

### Cause G: Upstream response headers exceed NGINX proxy/fastcgi buffer size

**Statement:** The upstream returns response headers larger than NGINX's `proxy_buffer_size`/`fastcgi_buffer_size`, so NGINX rejects the response as invalid and serves 502.

**Chain:**
- root: the upstream emits response headers larger than the single buffer sized by `proxy_buffer_size` (default 4k/8k) or `fastcgi_buffer_size` — large `Set-Cookie` chains, echoed JWT/OAuth tokens, or expansive CSP headers.
- s1: the status line plus header fields do not fit the first-chunk buffer, so NGINX treats the response as invalid.
- s2: NGINX logs `upstream sent too big header while reading response header from upstream` and returns 502.
- D: clients receive HTTP 502 Bad Gateway (Symptom Recognition).

**Indicators:**
- s2: [Step 1] error log contains `upstream sent too big header while reading response header from upstream`
  <!-- match: {"step": 1, "predicate": "contains", "target": "upstream sent too big header while reading response header from upstream"} -->
- root: [Step 10] `curl -sI` to the upstream returns header payload >4 KB or >20 distinct header fields
  <!-- match: {"step": 10, "predicate": "threshold", "target": "header_bytes", "op": ">", "value": 4096} -->

**Interventions:**
- **defensive_fix** (s1): raise the proxy/fastcgi header buffers so large but legitimate headers fit.

  ```nginx
  # /etc/nginx/conf.d/<route>.conf
  location / {
      proxy_pass http://app_backend;
      proxy_buffer_size 32k;
      proxy_buffers 8 32k;
      proxy_busy_buffers_size 64k;
  }

  # For FastCGI (PHP-FPM):
  location ~ \.php$ {
      fastcgi_pass unix:/run/php-fpm/www.sock;
      include fastcgi_params;
      fastcgi_buffer_size 32k;
      fastcgi_buffers 8 32k;
      fastcgi_busy_buffers_size 64k;
  }
  ```

  ```bash
  nginx -t && nginx -s reload
  ```

  **Verification:** re-send the failing request — `curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/<path>` returns 2xx; `grep "upstream sent too big header" /var/log/nginx/error.log | wc -l` shows no new occurrences over 15 minutes.
- **mitigation** (s1): apply the larger buffers as a quick per-location patch while deciding whether the upstream is legitimately large or leaking debug headers.

  ```bash
  # Per-location quick fix
  proxy_buffer_size 32k;
  proxy_buffers 8 32k;
  proxy_busy_buffers_size 64k;
  ```

  **Risk:** Larger buffers raise per-connection memory; with high `worker_connections`, watch for memory pressure on the NGINX host. **Duration:** Permanent if the upstream legitimately needs large headers; temporary if the upstream is leaking debug headers — fix the upstream instead. **Verification:** the failing request returns 2xx and host memory stays within budget.
- **remediation** (root): fix the upstream to stop emitting oversized headers when the bloat is unintended (debug headers, duplicated `Vary`/CORS, token echo).

  ```bash
  # Inspect and trim the offending upstream headers at the source.
  curl -sI http://<upstream-host>:<port>/<path>
  ```

  **Verification:** re-run Step 10; header payload is back under `proxy_buffer_size` without needing the widened buffers.

### Cause H: All servers in the upstream group are marked unavailable

**Statement:** Every server in the `upstream` group has exceeded `max_fails` within `fail_timeout`, so NGINX has no live target and responds 502 with `no live upstreams while connecting to upstream`.

**Chain:**
- root: a brief cluster-wide upstream blip (deploy, network flap, dependency outage) trips `max_fails` (default 1) within `fail_timeout` (default 10 s) on every server near-simultaneously.
- s1: all servers are in the failure penalty box at once, so NGINX has no live target to forward to.
- s2: NGINX logs `no live upstreams while connecting to upstream` and returns 502 until at least one `fail_timeout` expires.
- D: clients receive HTTP 502 Bad Gateway (Symptom Recognition).

**Indicators:**
- s2: [Step 1] error log contains `no live upstreams while connecting to upstream`
  <!-- match: {"step": 1, "predicate": "contains", "target": "no live upstreams while connecting to upstream"} -->
- root: [Step 7] the 502 burst is broad (all routes sharing the upstream group return 502) and time-correlated with an upstream-tier event
- s1: [Step 4] every upstream server is in fact healthy now (`LISTEN` socket present, `nc -zv` succeeds), but NGINX has not yet retried them

**Interventions:**
- **mitigation** (s1): reload NGINX to clear the failure counters once Step 4 confirms every upstream is healthy.

  ```bash
  nginx -s reload
  ```

  **Risk:** Reload clears counters but resets keepalive pools; expect a brief spike of new TCP connections to the upstream tier. **Duration:** Seconds — appropriate only after confirming every upstream is healthy, else the counters re-trip. **Verification:** Step 1 shows no further `no live upstreams` entries immediately after reload.
- **remediation** (root): tune failure accounting to the pool's real failure rate (and add a backup) so a transient blip cannot black-hole the whole group.

  ```nginx
  # /etc/nginx/conf.d/upstream.conf — tune failure accounting to the real failure rate
  upstream app_backend {
      server backend-1.internal:8080 max_fails=3 fail_timeout=30s;
      server backend-2.internal:8080 max_fails=3 fail_timeout=30s;
      server backend-3.internal:8080 max_fails=3 fail_timeout=30s;
      server backend-backup.internal:8080 backup;
      keepalive 32;
  }
  ```

  ```bash
  nginx -t && nginx -s reload
  ```

  **Verification:** trigger requests across all backends and confirm `awk '{print $9}' /var/log/nginx/access.log | grep -c 502` is 0 (or baseline); `tail -f /var/log/nginx/error.log` shows no further `no live upstreams` for at least 15 minutes. On NGINX Plus, `/api/<n>/http/upstreams/` reports `state=up` for every server.

### Cause I: NGINX worker out of file descriptors or connection slots

**Statement:** NGINX workers cannot open new sockets to the upstream because they have hit the per-process file-descriptor limit or `worker_connections` ceiling, so new requests fail with 502.

**Chain:**
- root: `worker_rlimit_nofile` is unset or below `worker_connections * 2` (or `worker_connections` itself is too small), so workers exhaust their kernel FD allowance under load.
- s1: a worker's `socket()`/`accept4()` call fails with `Too many open files` and cannot open a new upstream connection.
- s2: NGINX logs `socket() failed (24: Too many open files)` and the request receives 502.
- D: clients receive HTTP 502 Bad Gateway (Symptom Recognition).

**Indicators:**
- s2: [Step 1] error log contains `socket() failed (24: Too many open files)` or `accept4() failed (24: Too many open files)`
  <!-- match: {"step": 1, "predicate": "contains", "target": "Too many open files"} -->
- root: [Step 9] `cat /proc/<pid>/limits | grep "Max open files"` returns a soft limit below `worker_connections * 2`
- s1: [Step 9] `ss -s` shows TCP socket count near the per-process limit

**Interventions:**
- **mitigation** (s1): raise the running process FD limit to relieve pressure until the durable config/unit fix is in place.

  ```bash
  prlimit --nofile=65535:65535 --pid $(cat /run/nginx.pid)
  ```

  **Risk:** Reloading after raising limits drops idle keepalive connections; under high traffic expect a brief connection-establishment burst. **Duration:** Until the next NGINX restart — bake the durable fix into config and the systemd unit before the next reload cycle. **Verification:** Step 9 `cat /proc/<pid>/limits` shows the raised soft limit and the error log stops logging `Too many open files`.
- **remediation** (root): raise `worker_rlimit_nofile`/`worker_connections` and pin `LimitNOFILE` in the systemd unit so the ceiling cannot silently re-emerge.

  ```nginx
  # /etc/nginx/nginx.conf — top level
  worker_rlimit_nofile 65535;

  events {
      worker_connections 16384;
      multi_accept on;
  }
  ```

  ```bash
  mkdir -p /etc/systemd/system/nginx.service.d
  cat >/etc/systemd/system/nginx.service.d/limits.conf <<'EOF'
  [Service]
  LimitNOFILE=65535
  EOF
  systemctl daemon-reload
  systemctl restart nginx
  ```

  **Verification:** `cat /proc/$(cat /run/nginx.pid)/limits | grep "Max open files"` shows `65535` soft and hard (re-run Step 9); `grep "Too many open files" /var/log/nginx/error.log | wc -l` shows no growth for the next 30 minutes under representative load.

### Cause J: SELinux denying NGINX outbound network connection

**Statement:** SELinux is in `Enforcing` mode and the active policy does not permit the `httpd_t` domain to initiate outbound TCP to the upstream port, so NGINX `connect()` calls are blocked.

**Chain:**
- root: on a SELinux-enforcing host, the `httpd_t` domain is not allowed to reach the upstream port — `httpd_can_network_connect` is off, or the port is not labelled `http_port_t`.
- s1: the kernel denies the `name_connect` permission and emits a `type=AVC` denial; NGINX's `connect()` is blocked.
- s2: NGINX logs `connect() failed (13: Permission denied) while connecting to upstream` and returns 502.
- D: clients receive HTTP 502 Bad Gateway (Symptom Recognition).

**Indicators:**
- s2: [Step 1] error log contains `connect() failed (13: Permission denied) while connecting to upstream`
  <!-- match: {"step": 1, "predicate": "contains", "target": "connect() failed (13: Permission denied) while connecting to upstream"} -->
- s1: [Step 8] `ausearch -m AVC -ts recent | grep nginx` returns at least one `denied { name_connect }` line
  <!-- match: {"step": 8, "predicate": "contains", "target": "denied  { name_connect }"} -->
- root: [Step 8] `getenforce` returns `Enforcing`

**Interventions:**
- **mitigation** (root): set SELinux permissive only if a change-control window for the boolean/port fix is not immediately available.

  ```bash
  setenforce 0
  ```

  **Risk:** Switching SELinux permissive globally removes a layer of host hardening. **Duration:** Minutes — restore enforcing (`setenforce 1`) as soon as the targeted boolean/port fix is in place. **Verification:** Step 1 shows no further `Permission denied` while permissive, confirming SELinux as the cause.
- **remediation** (root): allow the confined NGINX domain to connect outbound, either broadly or by labelling the upstream port.

  ```bash
  # Allow NGINX/httpd to initiate outbound network connections
  setsebool -P httpd_can_network_connect 1

  # Or, more narrowly, label only the upstream port as http_port_t
  semanage port -a -t http_port_t -p tcp <upstream-port>
  semanage port -l | grep http_port_t
  ```

  **Verification:** `ausearch -m AVC -ts recent | grep nginx` shows no new denials within 10 minutes of load (re-run Step 8); `curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/<path>` returns 2xx and the error log shows no further `Permission denied` entries.

### Cause Z: Unidentified

**Statement:** A 502 is confirmed but the gathered evidence does not match any indicator for Causes A through J.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the proxy/platform SME.

  ```bash
  # Snapshot the active config, recent error log, and per-connection trace
  nginx -T > /tmp/nginx-conf.dump 2>&1
  tail -n 500 /var/log/nginx/error.log > /tmp/nginx-error.tail
  # For a single client IP, capture targeted debug:
  # events { debug_connection <client-ip>; }
  # error_log /var/log/nginx/debug.log debug;
  ss -tnp state established | head -50 > /tmp/nginx-ss.snap
  ```

  **Risk:** Capturing extra signal is read-only and safe, but enabling debug logging on a busy proxy can fill the disk quickly; use `error_log memory:32m debug;` (a memory ring buffer) for hot hosts instead of disk debug logging. **Duration:** Minutes — collect, hand off, then revert debug logging to `warn` to avoid disk-fill. **Verification:** the captured `nginx-conf.dump`, `nginx-error.tail`, `nginx-ss.snap`, upstream logs for the same window, and the 502 access-log entries are attached to an incident ticket and hand-off is acknowledged by the receiving engineer with a follow-up owner assigned.

## Prevention

- Use `proxy_next_upstream error timeout http_502 http_503 http_504;` with `proxy_next_upstream_tries 3;` and `proxy_next_upstream_timeout 10s;` so transient upstream failures are retried within the same client request rather than surfacing as 502.
- Set `keepalive 32;` in every `upstream` block and ensure `proxy_http_version 1.1;` is in effect so NGINX reuses upstream connections; this reduces 502s caused by half-closed connections after upstream restarts.
- Configure `proxy_buffer_size 16k; proxy_buffers 8 16k; proxy_busy_buffers_size 32k;` as the default for routes whose backends emit large headers (auth, CSP, Set-Cookie chains). Same for `fastcgi_buffer_size`/`fastcgi_buffers` on PHP-FPM routes.
- Add `resolver <internal-dns-ip> valid=30s ipv4=on ipv6=off;` and `resolver_timeout 5s;` in the `http` block whenever any `proxy_pass` uses a variable or any upstream server has `resolve`.
- Set `worker_rlimit_nofile` to at least `2 * worker_connections` and pin `LimitNOFILE` in the systemd unit drop-in so FD exhaustion cannot silently re-emerge after a service restart.
- Run health checks on the upstream tier (Kubernetes readiness probes, target-group health checks, `health_check` on NGINX Plus) so unhealthy backends are pulled out of rotation before NGINX trips `max_fails`.
- Page on the SLI `rate(nginx_http_requests_total{status="502"}[5m]) / rate(nginx_http_requests_total[5m]) > 0.01` sustained 5 minutes, and on `nginx_http_upstream_responses_total{status=~"5.."}` per upstream.
- For HTTPS upstreams, set `proxy_ssl_verify on;` with a managed CA bundle, enable `proxy_ssl_server_name on;`, and pin `proxy_ssl_protocols TLSv1.2 TLSv1.3;` so TLS misconfiguration is caught in `nginx -t`, not in production traffic.
- For RHEL-family hosts, bake `setsebool -P httpd_can_network_connect 1` (or per-port labelling) into the host provisioning role so SELinux denials never block the proxy in a fresh deployment.
- Test every config change with `nginx -t` in CI before deploy; use a canary host or canary deployment so config errors that pass `nginx -t` but fail at runtime do not roll out fleet-wide.

## Sources

- [NGINX — ngx_http_proxy_module reference](https://nginx.org/en/docs/http/ngx_http_proxy_module.html) — Priority 1. `proxy_connect_timeout`, `proxy_read_timeout`, `proxy_send_timeout`, `proxy_buffer_size`, `proxy_buffers`, `proxy_next_upstream`, `proxy_ssl_*`, `proxy_http_version` defaults and 502 risk per directive.
- [NGINX — ngx_http_upstream_module reference](https://nginx.org/en/docs/http/ngx_http_upstream_module.html) — Priority 1. `server`, `max_fails`, `fail_timeout`, `keepalive`, `keepalive_requests`, `zone`, `resolver`, `resolve` parameter; "no live upstreams" trigger conditions; DNS resolution semantics.
- [NGINX — ngx_http_core_module reference](https://nginx.org/en/docs/http/ngx_http_core_module.html) — Priority 1. `resolver`, `resolver_timeout`, `large_client_header_buffers`, `client_max_body_size`, `client_header_buffer_size` defaults and failure modes.
- [NGINX Admin Guide — Debugging](https://docs.nginx.com/nginx/admin-guide/monitoring/debugging/) — Priority 1. Enabling `error_log ... debug;` with file and memory ring-buffer outputs, `debug_connection`, core-dump capture for upstream-related crashes.
- [NGINX Admin Guide — Logging](https://docs.nginx.com/nginx/admin-guide/monitoring/logging/) — Priority 1. `error_log` directive scoping (http/server/location), severity levels, error-log line structure.
- [GetPageSpeed — NGINX 502 Bad Gateway: every cause and fix](https://www.getpagespeed.com/server-setup/nginx/nginx-502-bad-gateway) — Priority 2. Catalogue of exact error-log strings (`connect() failed (111: Connection refused)`, `connect() failed (13: Permission denied)`, `upstream prematurely closed`, `upstream sent too big header`, `no resolver defined to resolve`, `WARNING: [pool www] server reached pm.max_children`) paired with the directive change that fixes each.
