---
id: "haproxy-503-no-server"
title: "HAProxy 503 Service Unavailable: No backend server available"
domain: networking
service: haproxy
symptom_class: [service_unavailable, connection_refused]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [http-503, no-server-available, health-check, maxconn-queue, timeout-connect]
difficulty: intermediate
---

## Symptom Recognition

- HTTP response: `HTTP/1.1 503 Service Unavailable`
- Default HAProxy error body: `503 Service Unavailable` / `No server is available to handle this request.`
- haproxy log termination state codes seen on the failing requests:
  - `SC` — server refused the TCP connection / no server in the backend was usable.
  - `sC` — server-side `timeout connect` expired while connecting to the backend.
  - `sQ` — request waited in the backend queue and `timeout queue` expired before a slot freed up.
  - `sH` — server-side timeout while waiting for the connection/handshake to complete.
- `show stat` reports backend servers with status `DOWN`, `MAINT`, or `NOLB`; backend `status` column is `DOWN`.
- Health-check alert lines such as: `Server be_app/srv1 is DOWN, reason: Layer4 connection problem` or `Layer7 wrong status, code: 503`.
- Stats page / metrics: backend `qcur` (queued requests) rising; server `scur` pinned at `slim` (server `maxconn`).

## Applicability

- HAProxy 2.0+ (community `docs.haproxy.org`) and HAProxy Enterprise; log/state semantics identical.
- Requires read access to the haproxy log destination (syslog target, e.g. `/var/log/haproxy.log`).
- Requires access to the Runtime API stats socket (`stats socket /run/haproxy/admin.sock`) and `socat` installed.
- Requires permission to read `haproxy.cfg` and to reload haproxy (`systemctl reload haproxy` or `haproxy -c -f`).

## Diagnostic Steps

### Step 1: Confirm the 503 and capture the termination state from the access log

```bash
grep -E ' 503 | (SC|sC|sQ|sH)[A-Z-]{3} ' /var/log/haproxy.log | tail -n 20
```

Expected output: log lines whose status field is `503` and whose 4-char termination state begins with `SC`, `sC`, `sQ`, or `sH`, e.g. `... be_app/<NOSRV> 0/-1/-1/-1/0 503 212 - - SC-- 5/5/0/0/3 0/0 ...` (a literal `<NOSRV>` server name means no server was selected).

### Step 2: List backend/server health status via the Runtime API

```bash
echo "show stat" | socat stdio unix-connect:/run/haproxy/admin.sock | \
  awk -F, 'NR==1 || $2!="" {print $1","$2","$18","$5","$6","$7","$36}'
```

Expected output: CSV with `# pxname,svname,status,scur,smax,slim,qcur`. Healthy servers show `status=UP`; an outage shows backend rows and servers with `status` of `DOWN`, `MAINT`, `DRAIN`, or `NOLB`.

### Step 3: Dump full server operational and admin state

```bash
echo "show servers state" | socat stdio unix-connect:/run/haproxy/admin.sock
```

Expected output: one line per server. Field `srv_op_state` (column 6): `2`=running/UP, `0`=stopped/DOWN. Field `srv_admin_state` (column 7) is a bitmask: `0`=ready, `0x01`=FMAINT (forced maintenance), `0x08`=FDRAIN (forced drain). A backend with every server at `srv_op_state=0` cannot serve requests.

### Step 4: Inspect the active config for health-check, maxconn, and timeout settings

```bash
haproxy -c -f /etc/haproxy/haproxy.cfg && \
  grep -nE 'maxconn|timeout (connect|queue|server)|option httpchk|server .* check|inter |rise |fall ' /etc/haproxy/haproxy.cfg
```

Expected output: config validates ("Configuration file is valid"), followed by the effective `maxconn`, `timeout connect|queue|server`, `option httpchk`, and per-server `check inter <ms> rise <n> fall <n>` directives.

### Step 5: Reproduce the health check from the HAProxy host

```bash
curl -sS -o /dev/null -w 'http=%{http_code} connect=%{time_connect}s total=%{time_total}s\n' \
  --max-time 5 http://<backend_ip>:<backend_port>/<check_uri>
```

Expected output: a 2xx/3xx `http=` and sub-second `connect=` if the backend is healthy; `connect` timeout, refused connection, or a 5xx `http=` reproduces the reason HAProxy marks the server DOWN.

## Causes

### Cause A: Backend servers failing health checks and marked DOWN
**Statement:** The backend application or its health-check endpoint is unhealthy (not listening, returning a non-expected status, or unreachable on the network), so HAProxy fails the configured `check` and marks every server DOWN, leaving no server to route to.
**Chain:**
- root: backend health-check endpoint fails (connection refused or non-2xx response)
- s1: HAProxy records `fall` consecutive failed checks and sets the server `srv_op_state` to DOWN
- s2: all servers in the backend are DOWN, so request selection finds no usable server (`<NOSRV>`)
- D: client receives 503 Service Unavailable
**Indicators:**
- root: [Step 5] direct probe to the backend returns refused/timeout/non-2xx
- s1: [Step 2] `show stat` shows server `status` `DOWN`
- s2: [Step 1] access log shows `SC--` with server name `<NOSRV>`
- D: [Symptom] response is `503 Service Unavailable`
**Interventions:**
- **remediation** (root): restore the backend so the health-check URI returns the expected status (restart the app / fix the failing dependency), then confirm HAProxy re-marks it UP.

  ```bash
  systemctl restart app.service
  echo "show stat" | socat stdio unix-connect:/run/haproxy/admin.sock | grep -E '<backend>,' 
  ```

  **Verification:** Re-run Step 2; the server `status` is `UP` and Step 1 shows new requests logging `200 ... ----` instead of `SC--`.
- **defensive_fix** (s1): make checks accept the right responses and recover faster — set an explicit health URI and expected codes so a transient blip does not down the pool.

  ```haproxy
  backend be_app
      option httpchk GET /healthz
      http-check expect status 200
      default-server check inter 2s fall 3 rise 2
      server srv1 10.0.0.11:8080 check
  ```

  **Verification:** Reload haproxy, then Step 4 shows the new `option httpchk`/`http-check expect`; Step 2 shows the server returning to `UP` within `inter * rise`.
- **mitigation** (s2): force a known-good server UP to restore traffic while the root cause is investigated.

  ```bash
  echo "set server be_app/srv1 state ready" | socat stdio unix-connect:/run/haproxy/admin.sock
  ```

  **Risk:** routes live traffic to a server that may still be unhealthy, returning errors to users. **Duration:** minutes, until the backend is actually fixed. **Verification:** Step 1 shows requests now reaching `srv1` with 2xx instead of `<NOSRV>`/`SC--`.

### Cause B: Server maxconn reached and the queue overflowed (timeout queue)
**Statement:** Per-server `maxconn` is set too low for the offered load, so new requests queue at the backend and exceed `timeout queue` before a connection slot frees, causing HAProxy to abort them with a 503.
**Chain:**
- root: per-server `maxconn` is smaller than sustained concurrent demand
- s1: server connection slots saturate (`scur` == `slim`) and excess requests enter the backend queue
- s2: queued requests wait longer than `timeout queue` and are dequeued as failures
- D: client receives 503 Service Unavailable
**Indicators:**
- root: [Step 4] config shows a low per-`server`/`default-server` `maxconn`
- s1: [Step 2] `scur` equals `slim` and `qcur` is non-zero
- s2: [Step 1] access log shows `sQ--` termination state
- D: [Symptom] response is `503 Service Unavailable`
**Interventions:**
- **remediation** (root): raise per-server `maxconn` to match capacity (and add backends/replicas if the servers themselves are the bottleneck).

  ```haproxy
  backend be_app
      server srv1 10.0.0.11:8080 check maxconn 200
      server srv2 10.0.0.12:8080 check maxconn 200
  ```

  **Verification:** Reload haproxy; under load Step 2 shows `scur` below `slim` and `qcur` returning to 0, and Step 1 no longer logs `sQ--`.
- **defensive_fix** (s2): widen `timeout queue` so brief bursts drain instead of 503-ing, while keeping it bounded so callers are not held indefinitely.

  ```haproxy
  defaults
      timeout queue 10s
  ```

  **Verification:** Step 4 shows the new `timeout queue`; during a burst, queued requests complete with 2xx instead of `sQ--` 503s.
- **mitigation** (s1): live-raise the server connection limit via the Runtime API to absorb the spike without a reload.

  ```bash
  echo "set server be_app/srv1 maxconn 200" | socat stdio unix-connect:/run/haproxy/admin.sock
  ```

  **Risk:** pushing more concurrency at an undersized backend can overload it and cause `sH`/5xx instead. **Duration:** until the next reload (runtime change is not persisted). **Verification:** Step 2 shows `slim` increased and `qcur` draining toward 0.

### Cause C: timeout connect too short for backend latency
**Statement:** `timeout connect` is shorter than the time the backend needs to accept a TCP connection under current network/load conditions, so HAProxy gives up establishing the connection and returns a 503.
**Chain:**
- root: `timeout connect` is set below actual backend connect latency
- s1: HAProxy abandons the connection attempt before the backend accepts it
- s2: with `retries` exhausted, no server connection can be established for the request
- D: client receives 503 Service Unavailable
**Indicators:**
- root: [Step 4] config shows a very small `timeout connect`
- s1: [Step 1] access log shows `sC--` termination state
- s2: [Step 5] direct probe shows `connect=` time exceeding the configured `timeout connect`
- D: [Symptom] response is `503 Service Unavailable`
**Interventions:**
- **remediation** (root): set `timeout connect` above the observed backend connect latency (typical baseline 5s), and fix the underlying network/backend slowness if connect time is abnormally high.

  ```haproxy
  defaults
      timeout connect 5s
      timeout server 30s
      retries 3
  ```

  **Verification:** Reload haproxy; Step 4 shows the new `timeout connect`, and Step 1 no longer logs `sC--` for the backend under the same load.
- **mitigation** (s1): add Layer 7 retries so a single slow connect is retried rather than surfaced as a 503.

  ```haproxy
  backend be_app
      retry-on conn-failure empty-response
      http-request disable-l7-retry if { method POST }
  ```

  **Risk:** retries can amplify load on an already-slow backend and must not replay non-idempotent requests. **Duration:** safe as a standing safeguard once non-idempotent methods are excluded. **Verification:** Step 1 shows transient connect failures absorbed as 2xx retries instead of `sC--` 503s.

### Cause D: All servers administratively disabled (MAINT/DRAIN)
**Statement:** Every server in the backend is in a forced maintenance or drain administrative state (left over from a deploy, a `disable server` Runtime API call, or a `server ... disabled` directive), so no server is eligible even though the application is healthy.
**Chain:**
- root: all backend servers carry a forced admin state (FMAINT or FDRAIN)
- s1: HAProxy excludes every server from load-balancing selection
- s2: request selection finds no eligible server in the backend
- D: client receives 503 Service Unavailable
**Indicators:**
- root: [Step 3] `show servers state` shows `srv_admin_state` non-zero (e.g. `1` FMAINT / `8` FDRAIN) on all servers
- s1: [Step 2] `show stat` shows server `status` `MAINT` or `DRAIN`
- s2: [Step 1] access log shows `SC--` with `<NOSRV>` despite the app being reachable in Step 5
- D: [Symptom] response is `503 Service Unavailable`
**Interventions:**
- **remediation** (root): clear the admin state by re-enabling the servers (and remove any stray `disabled` keyword from the config so a reload does not re-disable them).

  ```bash
  echo "enable server be_app/srv1" | socat stdio unix-connect:/run/haproxy/admin.sock
  echo "set server be_app/srv1 state ready" | socat stdio unix-connect:/run/haproxy/admin.sock
  ```

  **Verification:** Re-run Step 2; server `status` is `UP` (not `MAINT`/`DRAIN`) and Step 1 shows traffic routing to the server.
- **defensive_fix** (s1): persist server state across reloads with a state file so a reload during maintenance does not silently leave servers disabled or re-enable the wrong ones.

  ```haproxy
  global
      server-state-file /run/haproxy/state-global
  defaults
      load-server-state-from-file global
  ```

  **Verification:** Save state with `echo "show servers state" | socat ... > /run/haproxy/state-global`, reload, then Step 3 shows the intended `srv_admin_state` preserved.

### Cause Z: Unidentified
**Statement:** The 503 / no-server condition does not match Causes A–D after running the diagnostic steps; the root cause is not yet identified and must be escalated with a full snapshot.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a complete diagnostic snapshot (config, runtime state, recent 503 log slice) and escalate to the HAProxy/SRE on-call.

  ```bash
  ts=$(date +%s); d=/tmp/haproxy-503-$ts; mkdir -p "$d"
  cp /etc/haproxy/haproxy.cfg "$d/"
  for c in "show info" "show stat" "show servers state" "show errors"; do \
    echo "== $c =="; echo "$c" | socat stdio unix-connect:/run/haproxy/admin.sock; \
  done > "$d/runtime.txt" 2>&1
  grep -E ' 503 | (SC|sC|sQ|sH)[A-Z-]{3} ' /var/log/haproxy.log | tail -n 200 > "$d/503.log"
  tar czf "$d.tgz" -C /tmp "haproxy-503-$ts"; echo "snapshot: $d.tgz"
  ```

  **Risk:** snapshot may contain hostnames/IPs/headers; redact before sharing externally. **Duration:** one-shot capture. **Verification:** `$d.tgz` exists and contains `haproxy.cfg`, `runtime.txt`, and `503.log`; attach it to the escalation ticket.

## Prevention

- Always define an explicit health check that matches real readiness: `option httpchk GET /healthz` + `http-check expect status 200`, with `check inter 2s fall 3 rise 2` so a single blip does not down the pool but a real outage is caught fast.
- Size per-server `maxconn` from measured backend capacity and set a bounded `timeout queue` (e.g. 10s) so bursts queue briefly instead of 503-ing.
- Set conservative defaults: `timeout connect 5s`, `timeout server 30s`, `retries 3`, and `retry-on conn-failure empty-response` for idempotent traffic.
- Alert on backend health, not just on 503 rate: page when `show stat` reports backend `status=DOWN` or active servers `< 1`, and when `qcur > 0` is sustained or `scur` reaches `slim`.
- Enable a `server-state-file` with `load-server-state-from-file global` so reloads preserve UP/MAINT/DRAIN state and never silently empty a backend.

## Sources

- [Haproxy configuration manual](https://www.haproxy.com/documentation/haproxy-configuration-manual/latest/) — directive syntax for `maxconn`, `timeout connect|server|queue`, `option httpchk`, `check`/`inter`/`rise`/`fall`, and the section 8.5 "Session state at disconnection" termination-state model.
- [docs.haproxy.org](https://docs.haproxy.org/) — canonical community Configuration Manual; termination state first/second-character semantics (S/P/R, C/Q/H) and health-check parameters.
- [Introduction to haproxy logging](https://www.haproxy.com/blog/introduction-to-haproxy-logging) — access-log field layout and example log line; meanings of termination codes (SC server-refused, PC proxy socket limit) and the location of the termination-state field.
- [Show servers state](https://www.haproxy.com/documentation/haproxy-runtime-api/reference/show-servers-state/) — `show servers state` purpose and exact `socat stdio unix-connect:<sock>` invocation; server IP/weight/drain/maint state preservation.
- [Show stat](https://www.haproxy.com/documentation/haproxy-runtime-api/reference/show-stat/) — `show stat` invocation and CSV column meanings (`scur`, `smax`, `slim`, `qcur`, `status`) and filtering by server state (up/no-maint).
