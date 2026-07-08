---
id: "traefik-routing-failure"
title: "Traefik returns 404/502 — router rule, service endpoint, and entrypoint routing failures"
domain: networking
service: traefik
symptom_class: [service_unavailable]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [404-not-found, 502-bad-gateway, ingressroute, router-priority, no-available-server]
difficulty: intermediate
---

## Symptom Recognition

- Clients receive `404 page not found` (plain text body) for a hostname/path that should be routed.
- Clients receive `502 Bad Gateway` from Traefik for a route that resolves but cannot reach a backend.
- Traefik access log shows `"DownstreamStatus":404` with empty `"RouterName"` and `"ServiceName"` (no matching router).
- Traefik access log shows `"OriginStatus":502` or `"OriginStatus":0` with a populated `"ServiceName"` but unreachable `"ServiceAddr"`.
- Application log (level DEBUG) emits `error while creating router`, `rule syntax`, or `field not found` while loading dynamic config.
- Traefik dashboard / `/api/rawdata` shows a router or service with `"status":"warning"` or `"status":"disabled"`.
- `/api/http/services/<name>` reports `loadBalancer.servers` empty or `serverStatus` entries with `DOWN`.

## Applicability

- **Versions:** Traefik v3.x (v3.0–v3.7). Router rule examples use v3 rule syntax (backticks, `&&`).
- **Providers:** Kubernetes CRD (IngressRoute), Docker labels, file provider.
- **Access required:** shell/exec on the Traefik pod or host; `kubectl get/describe` on IngressRoute, Service, EndpointSlice; read access to the Traefik API/dashboard (port 8080 when `api.insecure=true`, otherwise via an authenticated route).
- **Tools:** `curl`, `jq`, `kubectl`, access to Traefik access log and application log.

## Diagnostic Steps

### Step 1: Confirm whether a router matched the request

Replay the failing request and inspect the access log line. With JSON access logs enabled (`accessLog.format=json`):

```bash
curl -sv -H 'Host: example.com' http://<traefik-ip>/api/foo
# then read the matching access log line
tail -n 1 /var/log/traefik/access.log | jq '{DownstreamStatus, OriginStatus, RouterName, ServiceName, ServiceAddr, RequestHost, RequestPath}'
```

Expected output: a JSON object. A populated `RouterName` means a router matched; an empty/absent `RouterName` with `"DownstreamStatus":404` means no router matched.

### Step 2: List routers and their status from the Traefik API

```bash
curl -s http://localhost:8080/api/http/routers | jq '.[] | {name, rule, priority, status, entryPoints, service}'
```

Expected output: an array of routers. Each healthy router shows `"status":"enabled"`. A misconfigured router shows `"status":"warning"` or `"status":"disabled"`; `/api/rawdata` includes an `error` array for it.

### Step 3: Check whether the expected hostname/path is covered by any rule

```bash
curl -s http://localhost:8080/api/http/routers \
  | jq -r '.[] | "\(.priority)\t\(.status)\t\(.rule)\t-> \(.service)"' | sort -rn
```

Expected output: routers sorted by priority (highest first). Confirm exactly one rule matches the failing Host/Path; multiple equal-priority overlapping rules indicate a precedence conflict.

### Step 4: Inspect the target service and its backend servers

```bash
curl -s http://localhost:8080/api/http/services/<service-name>@kubernetes \
  | jq '{status, loadBalancer: .loadBalancer.servers, serverStatus}'
```

Expected output: `loadBalancer.servers` lists backend URLs and `serverStatus` maps each URL to `UP` or `DOWN`. Empty `servers` or all-`DOWN` `serverStatus` explains a 502.

### Step 5: Verify the entrypoint that received the request exists and is bound

```bash
curl -s http://localhost:8080/api/entrypoints | jq '.[] | {name, address}'
```

Expected output: a list of entrypoints (e.g. `web` on `:80`, `websecure` on `:443`). The entrypoint named on the failing router (Step 2 `entryPoints`) must appear here.

### Step 6: (Kubernetes) Inspect the IngressRoute and its backing Service/EndpointSlice

```bash
kubectl describe ingressroute <name> -n <ns>
kubectl get endpointslices -n <ns> -l kubernetes.io/service-name=<service-name>
kubectl get service <service-name> -n <ns> -o jsonpath='{.spec.ports[*].port}{"\n"}'
```

Expected output: IngressRoute events show no errors; the EndpointSlice lists ready backend IPs; the Service port matches the `port:` declared in the IngressRoute `services` block.

## Causes

### Cause A: Router rule does not match the request (or has invalid v3 syntax)
**Statement:** The router `rule` does not match the incoming Host/Path — either the rule expression is wrong/too narrow, or it uses invalid v3 syntax (single quotes instead of backticks, deprecated v2 operators) and the router fails to build, leaving the request with no matching router.
**Chain:**
- root: router rule mismatch or invalid v3 rule syntax
- s1: no router is mounted for the requested Host/Path
- D: Traefik returns 404 page not found
**Indicators:**
- root: [Step 3] no rule in the priority-sorted list matches the failing Host/Path, or the intended rule uses single quotes / v2 operators
- s1: [Step 1] access log line has empty `RouterName` with `"DownstreamStatus":404`
- root: [Step 2] router `status` is `warning`/`disabled` when the rule failed to build
**Interventions:**
- **remediation** (root): Fix the rule to use v3 syntax with backticks and matchers that cover the request, e.g.:

  ```yaml
  routes:
    - kind: Rule
      match: Host(`example.com`) && PathPrefix(`/api`)
      services:
        - name: my-service
          port: 80
  ```

  **Verification:** Re-run Step 3 and confirm the rule appears with `status: enabled` and matches the failing Host/Path; re-run Step 1 and confirm `RouterName` is now populated.

### Cause B: Backend service has no ready endpoints (unhealthy/scaled-to-zero servers)
**Statement:** The router matches but its referenced service has zero ready backend endpoints — the Deployment is scaled to zero, all pods are failing readiness, or the EndpointSlice is empty — so Traefik has no available server to forward to.
**Chain:**
- root: backend Deployment/pods have no ready endpoints
- s1: the Traefik service load balancer has an empty server pool / all servers DOWN
- D: Traefik returns 502 Bad Gateway
**Indicators:**
- root: [Step 6] EndpointSlice for the service lists no ready addresses
- s1: [Step 4] `loadBalancer.servers` is empty or every `serverStatus` entry is `DOWN`
- s1: [Step 1] access log shows populated `ServiceName` with `"OriginStatus":502`
**Interventions:**
- **remediation** (root): Restore ready backend endpoints — scale up and/or fix the failing readiness probe.

  ```bash
  kubectl scale deployment/<deploy> -n <ns> --replicas=2
  kubectl rollout status deployment/<deploy> -n <ns>
  ```

  **Verification:** Re-run Step 6 and confirm the EndpointSlice lists ready IPs; re-run Step 4 and confirm `serverStatus` shows `UP`.
- **mitigation** (s1): Temporarily route the entrypoint to a maintenance/static backend so clients get a controlled page instead of 502.

  ```bash
  kubectl patch ingressroute <name> -n <ns> --type=json \
    -p='[{"op":"replace","path":"/spec/routes/0/services/0/name","value":"maintenance-svc"}]'
  ```

  **Risk:** Live traffic is diverted from the real backend; do not leave in place. **Duration:** Until backend endpoints recover (minutes–hours). **Verification:** `curl -H 'Host: example.com' http://<traefik-ip>/` returns the maintenance page, not 502.

### Cause C: Wrong service port or missing target Service (provider/IngressRoute misconfiguration)
**Statement:** The IngressRoute references a Service name or `port:` that does not exist or does not match the Kubernetes Service's exposed port, so Traefik builds the router but cannot resolve a valid backend address.
**Chain:**
- root: IngressRoute `services` name/port does not match an existing Service port
- s1: the Traefik service resolves to no valid backend address
- D: Traefik returns 502 Bad Gateway
**Indicators:**
- root: [Step 6] declared IngressRoute `port:` is absent from `kubectl get service ... .spec.ports[*].port`
- s1: [Step 4] service `status` is `warning` or `loadBalancer.servers` is empty
- root: [Step 2] router `service` points at a name with no corresponding `/api/http/services` entry
**Interventions:**
- **remediation** (root): Correct the `name`/`port` in the IngressRoute `services` block to match the live Service port.

  ```bash
  kubectl get service <service-name> -n <ns> -o jsonpath='{.spec.ports[*].port}{"\n"}'
  kubectl patch ingressroute <name> -n <ns> --type=json \
    -p='[{"op":"replace","path":"/spec/routes/0/services/0/port","value":8080}]'
  ```

  **Verification:** Re-run Step 4 and confirm the service shows `status: enabled` with non-empty `loadBalancer.servers`; replay the request and confirm a 200.

### Cause D: Router bound to an undefined or wrong entrypoint
**Statement:** The router's `entryPoints` list names an entrypoint that is not defined in the static configuration (or names the wrong one), so the router never mounts on the entrypoint that actually receives the client's traffic.
**Chain:**
- root: router `entryPoints` references an entrypoint not bound by the static config
- s1: the router is not mounted on the entrypoint serving the request
- D: Traefik returns 404 page not found on that entrypoint
**Indicators:**
- root: [Step 5] the entrypoint named in the router's `entryPoints` is absent from `/api/entrypoints`
- s1: [Step 2] router lists `entryPoints` that do not include the one the client connects to
- s1: [Step 1] access log shows empty `RouterName` with `"DownstreamStatus":404` on the served entrypoint
**Interventions:**
- **remediation** (root): Point the IngressRoute `entryPoints` at an entrypoint that exists in the static config (verify names with Step 5).

  ```yaml
  spec:
    entryPoints:
      - web
      - websecure
  ```

  **Verification:** Re-run Step 5 to confirm the entrypoint exists, then re-run Step 2 and confirm the router's `entryPoints` includes the served entrypoint and `status: enabled`.
- **defensive_fix** (s1): Define the missing entrypoint in static config so future routes referencing it mount correctly.

  ```yaml
  entryPoints:
    websecure:
      address: ":443"
  ```

  **Verification:** Restart Traefik and confirm `/api/entrypoints` (Step 5) now lists the entrypoint.

### Cause Z: Unidentified
**Statement:** None of the above causes is confirmed by the diagnostics; the routing failure has an unestablished root cause.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot and escalate to the platform/SME owner.

  ```bash
  curl -s http://localhost:8080/api/rawdata > traefik-rawdata.json
  kubectl get ingressroute,svc,endpointslices -A -o yaml > traefik-routing-dump.yaml
  kubectl logs -n <ns> deploy/traefik --tail=500 > traefik-app.log
  ```

  **Risk:** No change to the system; failure persists until escalation resolves it. **Duration:** Until SME responds. **Verification:** Snapshot files exist and are attached to the incident ticket.

## Prevention

- Run Traefik with `accessLog.format=json` and `log.level=INFO` (raise to `DEBUG` only when debugging config loads); ship access logs so `RouterName`/`ServiceName`/`OriginStatus` are queryable.
- Validate IngressRoute manifests in CI: confirm `match` rules use v3 backtick syntax and that referenced Service `name`/`port` exist before merge.
- Alert on Traefik metrics: routers/services in non-`enabled` state, and `traefik_service_server_up == 0` for any service.
- Define all entrypoints in static config and lint that every IngressRoute `entryPoints` value is a known entrypoint.
- Set explicit `priority` on overlapping routers to make precedence deterministic rather than length-based.
- Tighten Kubernetes readiness probes so unready pods are removed from EndpointSlices before they can serve 502s.

## Sources

- [Traefik Logs & Access Logs (install-configuration/observability)](https://doc.traefik.io/traefik/reference/install-configuration/observability/logs-and-accesslogs/) — access log fields (RouterName, ServiceName, ServiceAddr, OriginStatus, DownstreamStatus, RequestHost, RequestPath), `accessLog.format=json`, `log.level` values/default.
- [Traefik Tracing (install-configuration/observability)](https://doc.traefik.io/traefik/reference/install-configuration/observability/tracing/) — tracing/OTLP keys, capturedRequestHeaders, addInternals for tracing internal routing.
- [Traefik API Documentation v3.4](https://doc.traefik.io/traefik/v3.4/operations/api/) — `/api/http/routers`, `/api/http/services`, `/api/entrypoints`, `/api/overview`, `/api/rawdata` paths; `api.insecure` :8080 dashboard.
- [Kubernetes IngressRoute CRD reference](https://doc.traefik.io/traefik/reference/routing-configuration/kubernetes/crd/http/ingressroute/) — IngressRoute YAML (entryPoints, routes, match, priority, services name/port/kind) and entrypoint-not-matching behavior; kubectl inspection commands.
- [Traefik HTTP Routers Rules & Priority](https://doc.traefik.io/traefik/reference/routing-configuration/http/routing/rules-and-priority/) — v3 rule syntax (backticks vs single quotes), priority disambiguation.
- [Traefik Getting Started FAQ](https://doc.traefik.io/traefik/getting-started/faq/) — 502 on backend error; why a missing router yields 404 rather than 503 (dynamic aggregated config).
