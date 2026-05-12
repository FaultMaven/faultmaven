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
version: "1.0.0"
last_updated: "2026-05-12"
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

**Mechanism:** When NGINX initiates a TCP connection to the upstream, the kernel on the upstream host has no socket in `LISTEN` state for the target port and returns `RST`. NGINX surfaces this as `connect() failed (111: Connection refused) while connecting to upstream` in the error log and responds 502 to the client. If `proxy_next_upstream` is exhausted or the upstream group has only one server, no retry succeeds.

**Indicator:**

- [Step 1] error log contains the string `connect() failed (111: Connection refused) while connecting to upstream`
<!-- match: {"step": 1, "predicate": "contains", "target": "connect() failed (111: Connection refused) while connecting to upstream"} -->
- [Step 3] `nc -zv` to the upstream `host:port` prints `Connection refused` or returns non-zero
<!-- match: {"step": 3, "predicate": "contains", "target": "Connection refused"} -->
- [Step 4] `ss -tlnp` on the upstream host has no `LISTEN` line for the expected port

**Mitigation:**

- **Risk:** Restarting the upstream restores traffic but masks why it stopped; if the crash is reproducible, the service will fail again within seconds and the operator must dig into the upstream logs in parallel.
- **Command:**

  ```bash
  systemctl restart <upstream-service>
  systemctl status <upstream-service> --no-pager
  ```

- **Duration:** Minutes. Acceptable while root cause is being investigated; replace with a fix in the upstream service within the same incident.

**Resolution:**

```bash
# Identify the failure mode in the upstream's own logs and fix it there.
journalctl -u <upstream-service> -n 200 --no-pager
# Common follow-ups: configure the service to bind to the address NGINX expects, raise its memory_limit / heap, fix a config error, or correct the systemd unit's ExecStart.
systemctl enable --now <upstream-service>
```

**Verification:** Run `curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/<path>` five times; every response is 2xx/3xx. `awk '$9==502 {c++} END {print c+0}' /var/log/nginx/access.log` shows no new 502 entries for the next 10 minutes.

### Cause B: Connect timeout to upstream because the network path is slow or partitioned

**Statement:** NGINX cannot complete the TCP handshake to the upstream within `proxy_connect_timeout` because a firewall, security group, network policy, or routing failure silently drops `SYN` packets.

**Mechanism:** A connection-refused error returns immediately, but a dropped `SYN` packet causes NGINX to wait until `proxy_connect_timeout` (default 60 s) elapses, then logs `upstream timed out (110: Connection timed out) while connecting to upstream` and responds 502. Unlike Cause A, the upstream process itself is healthy; the path from NGINX to it is broken, often after a security-group change, NetworkPolicy update, or routing-table edit.

**Indicator:**

- [Step 1] error log contains `upstream timed out (110: Connection timed out) while connecting to upstream`
<!-- match: {"step": 1, "predicate": "contains", "target": "upstream timed out (110: Connection timed out) while connecting to upstream"} -->
- [Step 3] `nc -zv` hangs until its own `-w` timeout, then exits non-zero (no `refused` text in output)
- [Step 4] the upstream's `ss -tlnp` does show a `LISTEN` socket on the expected port

**Mitigation:**

- **Risk:** Raising `proxy_connect_timeout` hides the partition behind longer client latency but does not restore connectivity; only use this if the upstream is genuinely slow to accept connections (rare on TCP).
- **Command:**

  ```bash
  curl -v --max-time 10 telnet://<upstream-host>:<port>
  ```

- **Duration:** Diagnostic only — do not leave timeout adjustments in place once the network path is restored.

**Resolution:**

```bash
# Restore the network path. Validate from the NGINX host outward.
ip route get <upstream-ip>
sudo iptables -L -n -v | grep -E "<upstream-port>|<upstream-ip>"
# For AWS: re-add the SG ingress rule for the proxy SG on the upstream port.
# For Kubernetes: confirm a matching NetworkPolicy egress rule and a corresponding ingress rule on the upstream pod.
kubectl get networkpolicy -A
```

**Impact:** Restoring a firewall/SG/NetworkPolicy rule re-opens the full path between the proxy tier and the upstream pool — every request currently 502-ing on this route recovers. The change is durable; no NGINX reload is required.

**Rollback:** Re-apply the previous firewall/security-group/NetworkPolicy state with the change-management tool (Terraform, Helm, kubectl apply) used for the original mutation that broke the path.

**Verification:** `nc -zv -w 3 <upstream-host> <upstream-port>` from the NGINX host now prints `succeeded`. `tail -f /var/log/nginx/error.log` shows no new `Connection timed out` entries for at least 10 minutes under load.

### Cause C: Upstream prematurely closes the connection (crashes or kills the worker mid-response)

**Statement:** The upstream accepts the TCP connection but its worker process or request handler aborts before sending complete response headers, causing NGINX to log `upstream prematurely closed connection while reading response header from upstream` and return 502.

**Mechanism:** An application-tier crash, OOM kill, signal-driven graceful shutdown without connection draining, or a worker pool that recycles mid-request leaves NGINX with a half-read response. NGINX cannot recover the in-flight request body (especially when `proxy_request_buffering off` is in effect) and propagates the failure as 502. Common triggers are PHP-FPM segfaults, Python/Ruby worker memory limits, Node.js unhandled rejections, and `pm.max_requests` recycling under load.

**Indicator:**

- [Step 1] error log contains `upstream prematurely closed connection while reading response header from upstream`
<!-- match: {"step": 1, "predicate": "contains", "target": "upstream prematurely closed connection while reading response header from upstream"} -->
- [Step 4] upstream service status shows recent restarts, OOM kills (`dmesg | grep -i killed`), or `pm.max_children`/`pm.max_requests` recycling warnings in its log
- [Step 7] 502 spikes coincide with upstream deploy/restart events or memory-pressure alerts on the upstream tier

**Mitigation:**

- **Risk:** Adding upstream retries can amplify load on an already-struggling backend; combine with `proxy_next_upstream_tries` to bound the blast radius.
- **Command:**

  ```bash
  # Allow NGINX to retry on the most common transient upstream failures
  cat >/etc/nginx/conf.d/upstream-retry.conf <<'EOF'
  proxy_next_upstream error timeout http_502 http_503 http_504;
  proxy_next_upstream_tries 3;
  proxy_next_upstream_timeout 10s;
  EOF
  nginx -t && nginx -s reload
  ```

- **Duration:** Hours to days. Retries hide intermittent upstream instability but do not fix it; track and resolve the upstream crashes within the next sprint.

**Resolution:**

```bash
# Fix the upstream. Examples follow the failure type observed:
# 1) PHP-FPM segfault or OOM
sed -i 's/^memory_limit = .*/memory_limit = 256M/' /etc/php.ini
systemctl restart php-fpm
# 2) Node.js/Python worker crash — patch the offending handler, ship a new image, re-roll
kubectl rollout restart deployment/<upstream-deployment> -n <namespace>
# 3) Worker recycling under load — raise pm.max_children and pm.max_requests
sed -i 's/^pm.max_children = .*/pm.max_children = 50/' /etc/php-fpm.d/www.conf
sed -i 's/^pm.max_requests = .*/pm.max_requests = 1000/' /etc/php-fpm.d/www.conf
systemctl restart php-fpm
```

**Impact:** Each fix lands on the upstream tier, not on NGINX. The retry-on-error mitigation above is cluster-wide for the proxy and reduces 502s visible to clients during transient upstream failures, but adds up to 3x request load to the upstream pool during incidents.

**Rollback:** Revert the upstream config change (`git checkout HEAD~1 -- <config>` then redeploy). Remove the retry mitigation by `rm /etc/nginx/conf.d/upstream-retry.conf && nginx -t && nginx -s reload`.

**Verification:** Over a 15-minute window, `awk '$9==502 {c++} END {print c+0}' /var/log/nginx/access.log` returns 0 (or pre-incident baseline). `grep "upstream prematurely closed" /var/log/nginx/error.log | wc -l` shows no growth.

### Cause D: Read or send timeout because the upstream is slow to respond

**Statement:** The upstream eventually responds but takes longer than `proxy_read_timeout` (or `proxy_send_timeout`), so NGINX closes the connection and returns 502.

**Mechanism:** `proxy_read_timeout` (default 60 s) bounds the time between two successive reads from the upstream — not the total request duration. A backend that streams partial response chunks, performs a long database query, or invokes a slow third-party API can be alive yet silent past the timeout, at which point NGINX terminates the upstream connection and logs `upstream timed out (110: Connection timed out) while reading response header from upstream`. Long-running PHP scripts, large file generations, and synchronous SaaS callbacks are the canonical triggers.

**Indicator:**

- [Step 1] error log contains `upstream timed out` followed by `while reading response header from upstream` or `while reading upstream`
<!-- match: {"step": 1, "predicate": "contains", "target": "upstream timed out (110: Connection timed out) while reading"} -->
- [Step 7] access-log entries for affected routes show `$upstream_response_time` near or equal to the configured `proxy_read_timeout` value
- [Step 4] upstream service is healthy (`active (running)`) and listening; CPU is high or single requests are observed taking >30 s in upstream-side traces

**Mitigation:**

- **Risk:** Increasing `proxy_read_timeout` raises client wait time on slow paths and ties up worker connections longer; combine with bounded `proxy_next_upstream_timeout` to cap total client wait.
- **Command:**

  ```bash
  # Per-location override for an endpoint known to be slow
  proxy_connect_timeout 30s;
  proxy_send_timeout 300s;
  proxy_read_timeout 300s;
  ```

- **Duration:** Hours, while the upstream is being optimised. Long-term, push slow work into a job queue and return 202 from the synchronous path.

**Resolution:**

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

**Verification:** Run a representative slow request: `time curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/<slow-path>`; the response is 2xx and `$upstream_response_time` (visible if logged) is well below the new `proxy_read_timeout`. The error log shows no `upstream timed out ... while reading` entries for the next 30 minutes.

### Cause E: TLS handshake to the upstream fails

**Statement:** NGINX cannot complete the TLS handshake with an HTTPS upstream because of a protocol-version mismatch, cipher mismatch, missing or incorrect SNI, or certificate-verification failure.

**Mechanism:** When `proxy_pass https://...` is used, NGINX initiates a TLS handshake against the upstream. If `proxy_ssl_protocols` excludes the upstream's supported versions, if `proxy_ssl_ciphers` does not overlap with what the upstream offers, if `proxy_ssl_server_name on;` is missing on a server that requires SNI, or if `proxy_ssl_verify on;` is paired with an outdated `proxy_ssl_trusted_certificate` CA bundle, OpenSSL returns an error mid-handshake. NGINX logs `SSL_do_handshake() failed` (often with `tlsv1 alert protocol version`, `wrong version number`, or `certificate verify failed`) and responds 502.

**Indicator:**

- [Step 1] error log contains `SSL_do_handshake() failed` referencing the upstream
<!-- match: {"step": 1, "predicate": "contains", "target": "SSL_do_handshake() failed"} -->
- [Step 6] `openssl s_client` to the upstream prints `Verify return code: <non-zero>` or an `alert handshake failure`
- [Step 6] certificate `subject=` does not match the hostname NGINX uses (mismatch between `proxy_pass`/`proxy_ssl_name` and the certificate SAN)

**Mitigation:**

- **Risk:** Temporarily disabling `proxy_ssl_verify` removes the warning but accepts man-in-the-middle risk; only use as a diagnostic step and revert within the same change window.
- **Command:**

  ```bash
  openssl s_client -connect <upstream-host>:443 -servername <upstream-host> -showcerts </dev/null \
    | openssl x509 -noout -subject -issuer -dates
  ```

- **Duration:** Minutes. The TLS mismatch is a config issue, not a runtime condition; fix at the resolution step.

**Resolution:**

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
# Refresh the CA bundle when the issuer cert chain rotated:
update-ca-certificates    # Debian/Ubuntu
# or:
update-ca-trust extract   # RHEL/CentOS/Rocky/Alma
nginx -t && nginx -s reload
```

**Impact:** Affects every `location` block sharing this upstream and every client routed through NGINX. A reload is graceful (workers finish in-flight requests) so no client connections are dropped.

**Rollback:** Revert the modified server/location block with `git checkout` (or restore `/etc/nginx/conf.d/<route>.conf.bak`) and reload: `nginx -t && nginx -s reload`.

**Verification:** `openssl s_client -connect <upstream-host>:443 -servername <upstream-host> </dev/null 2>&1 | grep "Verify return code"` returns `Verify return code: 0 (ok)`. `curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/api/<path>` returns 2xx and the NGINX error log shows no new `SSL_do_handshake() failed` entries.

### Cause F: Upstream hostname cannot be resolved by NGINX

**Statement:** NGINX cannot resolve the upstream's DNS name at request time because the `resolver` directive is missing, the configured DNS server is unreachable, or the hostname does not exist.

**Mechanism:** NGINX resolves upstream names declared in `upstream { server ... }` blocks once at config load. When `proxy_pass` contains a variable (for example `proxy_pass http://$backend`), or when an `upstream` server has the `resolve` parameter, NGINX performs runtime DNS lookups and requires an explicit `resolver` directive. Without it, the error log shows `no resolver defined to resolve <hostname>` and the request returns 502. If the resolver is configured but the DNS server is unreachable or the record does not exist, NGINX logs `<hostname> could not be resolved` and returns 502.

**Indicator:**

- [Step 1] error log contains `no resolver defined to resolve` or `could not be resolved (3: Host not found)`
<!-- match: {"step": 1, "predicate": "contains", "target": "could not be resolved"} -->
- [Step 5] `dig +short <upstream-host>` returns empty or `NXDOMAIN`
- [Step 5] `grep "resolver " /etc/nginx/nginx.conf /etc/nginx/conf.d/*.conf` returns no matches even though `proxy_pass` uses a variable

**Mitigation:**

- **Risk:** Pointing `resolver` at an unauthenticated public DNS server (e.g., `8.8.8.8`) leaks internal hostname queries; prefer the internal DNS the rest of the platform uses.
- **Command:**

  ```bash
  getent hosts <upstream-host>
  cat /etc/resolv.conf
  ```

- **Duration:** Minutes. Use only while validating which resolver to configure permanently.

**Resolution:**

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

**Impact:** Affects every variable `proxy_pass` and every `server ... resolve` directive in the NGINX instance. The `resolver` directive applies on reload; in-flight requests are unaffected.

**Rollback:** Comment out the new `resolver` line (or revert to the prior config) and `nginx -t && nginx -s reload`. Static (non-variable) `proxy_pass` declarations resolve at startup and continue working.

**Verification:** `curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/<path>` returns 2xx for routes that proxy via the dynamic upstream. `tail -f /var/log/nginx/error.log` shows no further `could not be resolved` entries.

### Cause G: Upstream response headers exceed NGINX proxy/fastcgi buffer size

**Statement:** The upstream returns response headers larger than NGINX's `proxy_buffer_size`/`fastcgi_buffer_size`, so NGINX rejects the response with `upstream sent too big header while reading response header from upstream` and serves 502.

**Mechanism:** NGINX reads the first chunk of an upstream response into a single buffer sized by `proxy_buffer_size` (default 4k/8k) or `fastcgi_buffer_size`. If the response headers (status line plus all header fields) do not fit, NGINX treats the response as invalid and returns 502. Common triggers are large `Set-Cookie` headers (session + CSRF + tracking), JWT or OAuth tokens echoed in response headers, expansive `Content-Security-Policy` declarations, or duplicated `Vary`/`Access-Control-Allow-*` headers.

**Indicator:**

- [Step 1] error log contains `upstream sent too big header while reading response header from upstream`
<!-- match: {"step": 1, "predicate": "contains", "target": "upstream sent too big header while reading response header from upstream"} -->
- [Step 10] `curl -sI` to the upstream returns header payload >4 KB or >20 distinct header fields
<!-- match: {"step": 10, "predicate": "threshold", "target": "header_bytes", "op": ">", "value": 4096} -->

**Mitigation:**

- **Risk:** Increasing buffer size raises per-connection memory; with high `worker_connections`, watch for memory pressure on the NGINX host.
- **Command:**

  ```bash
  # Per-location quick fix
  proxy_buffer_size 32k;
  proxy_buffers 8 32k;
  proxy_busy_buffers_size 64k;
  ```

- **Duration:** Permanent if the upstream legitimately needs large headers (auth tokens, CSP). Treat as a temporary patch if the upstream is leaking debug headers — fix the upstream instead.

**Resolution:**

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

**Verification:** Re-send the failing request: `curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/<path>` returns 2xx. `grep "upstream sent too big header" /var/log/nginx/error.log | wc -l` shows no new occurrences over 15 minutes.

### Cause H: All servers in the upstream group are marked unavailable

**Statement:** Every server in the `upstream` group has exceeded `max_fails` within `fail_timeout`, so NGINX has no live target and responds 502 with `no live upstreams while connecting to upstream`.

**Mechanism:** NGINX tracks per-server failure counters. When a server records `max_fails` failures (default 1) within `fail_timeout` (default 10 s), it is marked unavailable for `fail_timeout`. If every server in the group is in this penalty box simultaneously, NGINX has nowhere to forward the request and logs `no live upstreams`. The most common precursor is a brief cluster-wide upstream blip (deploy, network flap, dependency outage) that trips all servers near-simultaneously; until at least one fail_timeout expires, every request returns 502.

**Indicator:**

- [Step 1] error log contains `no live upstreams while connecting to upstream`
<!-- match: {"step": 1, "predicate": "contains", "target": "no live upstreams while connecting to upstream"} -->
- [Step 7] the 502 burst is broad (all routes sharing the upstream group return 502) and time-correlated with an upstream-tier event
- [Step 4] every upstream server is in fact healthy now (`LISTEN` socket present, `nc -zv` succeeds), but NGINX has not yet retried them

**Mitigation:**

- **Risk:** Reloading NGINX clears the failure counters immediately but also resets keepalive pools; expect a brief spike of new TCP connections to the upstream tier.
- **Command:**

  ```bash
  nginx -s reload
  ```

- **Duration:** Seconds. Reload is appropriate only when you have already confirmed every upstream is healthy (Step 4); otherwise the counters will re-trip.

**Resolution:**

```nginx
# /etc/nginx/conf.d/upstream.conf — tune failure accounting to the real failure rate of the pool
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

**Verification:** Trigger several requests across all backends and confirm `awk '{print $9}' /var/log/nginx/access.log | grep -c 502` is 0 (or matches baseline). `tail -f /var/log/nginx/error.log` shows no further `no live upstreams` entries for at least 15 minutes. If running NGINX Plus, the `/api/<n>/http/upstreams/` endpoint reports `state=up` for every server.

### Cause I: NGINX worker out of file descriptors or connection slots

**Statement:** NGINX workers cannot open new sockets to the upstream because they have hit the per-process file-descriptor limit or `worker_connections` ceiling, so new requests fail with 502.

**Mechanism:** Every upstream connection consumes one FD per worker plus one FD per client connection. If `worker_rlimit_nofile` is unset (or below `worker_connections * 2`), workers exhaust their kernel FD allowance under load. The error log shows `socket() failed (24: Too many open files)` and downstream requests receive 502 (or, depending on which step fails, 500). The same surface symptom appears when `worker_connections` is too small or when a runaway upstream holds connections open past `keepalive_timeout`.

**Indicator:**

- [Step 1] error log contains `socket() failed (24: Too many open files)` or `accept4() failed (24: Too many open files)`
<!-- match: {"step": 1, "predicate": "contains", "target": "Too many open files"} -->
- [Step 9] `cat /proc/<pid>/limits | grep "Max open files"` returns a soft limit below `worker_connections * 2`
- [Step 9] `ss -s` shows TCP socket count near the per-process limit

**Mitigation:**

- **Risk:** Reloading NGINX after raising limits drops idle keepalive connections; under high traffic, expect a brief connection-establishment burst against the upstream.
- **Command:**

  ```bash
  prlimit --nofile=65535:65535 --pid $(cat /run/nginx.pid)
  ```

- **Duration:** Until the next NGINX restart. Bake the durable fix into the config and systemd unit before the next reload cycle.

**Resolution:**

```nginx
# /etc/nginx/nginx.conf — top level
worker_rlimit_nofile 65535;

events {
    worker_connections 16384;
    multi_accept on;
}
```

```bash
# Ensure the systemd unit does not cap LimitNOFILE below the new value
mkdir -p /etc/systemd/system/nginx.service.d
cat >/etc/systemd/system/nginx.service.d/limits.conf <<'EOF'
[Service]
LimitNOFILE=65535
EOF
systemctl daemon-reload
systemctl restart nginx
```

**Impact:** Cluster-wide for the NGINX instance. `systemctl restart` (not reload) is required because `LimitNOFILE` is set at process creation; expect a single-digit-second blip while workers re-establish connections.

**Rollback:** Remove `/etc/systemd/system/nginx.service.d/limits.conf` and revert `worker_rlimit_nofile`/`worker_connections` to their previous values, then `systemctl daemon-reload && systemctl restart nginx`.

**Verification:** `cat /proc/$(cat /run/nginx.pid)/limits | grep "Max open files"` shows `65535` soft and hard. `grep "Too many open files" /var/log/nginx/error.log | wc -l` shows no growth for the next 30 minutes under representative load.

### Cause J: SELinux denying NGINX outbound network connection

**Statement:** SELinux is in `Enforcing` mode and the active policy does not permit the `httpd_t` domain to initiate outbound TCP to the upstream port, so NGINX `connect()` calls are blocked.

**Mechanism:** On RHEL-derived hosts with SELinux enforcing, the `httpd_t` domain (which covers NGINX by default) is restricted by `httpd_can_network_connect` and the `http_port_t` port-type list. If the upstream listens on a port not labelled `http_port_t` (e.g., an application on `9000` or `8443`), the `name_connect` permission is denied. NGINX surfaces this as `connect() failed (13: Permission denied) while connecting to upstream` and returns 502. The kernel audit log carries the matching AVC denial.

**Indicator:**

- [Step 1] error log contains `connect() failed (13: Permission denied) while connecting to upstream`
<!-- match: {"step": 1, "predicate": "contains", "target": "connect() failed (13: Permission denied) while connecting to upstream"} -->
- [Step 8] `ausearch -m AVC -ts recent | grep nginx` returns at least one `denied { name_connect }` line
<!-- match: {"step": 8, "predicate": "contains", "target": "denied  { name_connect }"} -->
- [Step 8] `getenforce` returns `Enforcing`

**Mitigation:**

- **Risk:** Switching SELinux to permissive globally removes a layer of host hardening; only do this if a production change-control window for the boolean/port fix is not available immediately.
- **Command:**

  ```bash
  setenforce 0
  ```

- **Duration:** Minutes. Restore enforcing mode (`setenforce 1`) as soon as the targeted boolean or port fix is in place.

**Resolution:**

```bash
# Allow NGINX/httpd to initiate outbound network connections
setsebool -P httpd_can_network_connect 1

# Or, more narrowly, label only the upstream port as http_port_t
semanage port -a -t http_port_t -p tcp <upstream-port>
# Verify
semanage port -l | grep http_port_t
```

**Impact:** Host-local — applies to every confined NGINX process on this host. `setsebool -P` persists across reboots; `setenforce 0` does not.

**Rollback:** Revert with `setsebool -P httpd_can_network_connect 0` or `semanage port -d -t http_port_t -p tcp <upstream-port>`. Restore enforcing mode with `setenforce 1` if it was changed.

**Verification:** `ausearch -m AVC -ts recent | grep nginx` shows no new denials within 10 minutes of load. `curl -sS -o /dev/null -w "%{http_code}\n" https://<host>/<path>` returns 2xx and the NGINX error log shows no further `Permission denied` entries.

### Cause Z: Unidentified

**Statement:** Diagnostic steps confirmed NGINX returned 502 but did not match any of the indicators for Causes A through J.

**Mechanism:** A 502 was observed (the access log carries status 502 and the error log carries an `upstream` reference), but the gathered evidence does not isolate which path drove the failure. The error-log string may be unfamiliar, may name a less common module (`uwsgi_pass`, `grpc_pass`, `memcached_pass`), or the failure may be intermittent enough that no single Indicator matches.

**Indicator:**

- [Default] 502 is confirmed (Step 1) but Causes A–J indicators do not match the gathered evidence

**Mitigation:**

- **Risk:** Capturing extra signal is read-only and safe, but enabling debug logging on a busy proxy can fill the disk quickly; use `error_log memory:32m debug;` (a memory ring buffer) for hot hosts instead of disk debug logging.
- **Command:**

  ```bash
  # Snapshot the active config, recent error log, and per-connection trace
  nginx -T > /tmp/nginx-conf.dump 2>&1
  tail -n 500 /var/log/nginx/error.log > /tmp/nginx-error.tail
  # For a single client IP, capture targeted debug:
  # events { debug_connection <client-ip>; }
  # error_log /var/log/nginx/debug.log debug;
  ss -tnp state established | head -50 > /tmp/nginx-ss.snap
  ```

- **Duration:** Minutes. Collect, hand off, then revert debug logging to `warn` to avoid disk-fill.

**Resolution:** Out of runbook scope. Package the captured `nginx-conf.dump`, `nginx-error.tail`, `nginx-ss.snap`, the upstream service's logs covering the same window, and the access-log entries showing 502 responses; escalate to the proxy owner or platform on-call with the upstream identifier and the affected route.

**Verification:** Hand-off acknowledged by the receiving engineer; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

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
