---
id: prometheus-alertmanager-not-firing
title: "Prometheus Alerts Not Firing"
domain: application
service: prometheus
symptom_class:
  - service_unavailable
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: kb-researcher
status: draft
tags:
  - prometheus
  - alertmanager
  - alerting
  - notifications
  - observability
difficulty: intermediate
---

# Prometheus Alerts Not Firing

## Symptom Recognition

- A known production outage produces no PagerDuty page, Slack message, or email despite the underlying condition having been true for many minutes.
- The Prometheus UI shows an alert rule stuck in `inactive` or `pending` and never transitions to `firing`.
- `prometheus_notifications_errors_total` is non-zero or `rate(prometheus_notifications_errors_total[5m]) > 0` for one or more Alertmanager peers.
- `curl http://localhost:9090/api/v1/alertmanagers` returns an empty `activeAlertmanagers` array, or returns peers in `droppedAlertmanagers`.
- Alerts are visible in `curl http://localhost:9093/api/v2/alerts` but no notification arrives at the configured receiver.
- Alertmanager logs contain `msg="Notify for alerts failed"`, `context deadline exceeded`, `dial tcp ... connection refused`, or HTTP `401`/`403`/`429`/`5xx` from receiver endpoints.
- An active silence in `curl http://localhost:9093/api/v2/silences` matches the alert's labels, or `amtool config routes test` resolves a missing alert to an unintended receiver.
- A heartbeat / Watchdog alert that should fire continuously is missing from the receiver feed for longer than its `repeat_interval`.
- `promtool check rules` or `amtool check-config` fails after a recent rule or routing change.

## Applicability

- Prometheus 2.x or 3.x and Alertmanager 0.25+ (API v1 was removed in Alertmanager 0.27 — this runbook uses `/api/v2`).
- Self-hosted Prometheus, kube-prometheus-stack, Prometheus Operator, or managed Prometheus (Grafana Cloud, AMP, GMP) — managed offerings expose the same APIs but vendor consoles supersede some commands.
- HTTP access to Prometheus admin endpoints on port 9090 (`/api/v1/rules`, `/api/v1/alerts`, `/api/v1/alertmanagers`, `/api/v1/query`, `/-/reload`) and Alertmanager admin endpoints on port 9093 (`/api/v2/alerts`, `/api/v2/silences`, `/api/v2/status`, `/-/reload`).
- The `--web.enable-lifecycle` flag must be set on both Prometheus and Alertmanager to use `/-/reload`. Otherwise reload via `SIGHUP` to the process or a Kubernetes ConfigMap update plus pod restart.
- Write access to `prometheus.yml`, the alert rule files, and `alertmanager.yml`.
- `promtool` and `amtool` CLIs on the local host or in a debug pod.
- `curl` + `jq` for the diagnostic queries. `kubectl` if Prometheus/Alertmanager run on Kubernetes.

## Diagnostic Steps

### Step 1: Read the alert rule state and health from Prometheus

```bash
curl -s http://localhost:9090/api/v1/rules \
  | jq '.data.groups[].rules[] | select(.type=="alerting") | {name: .name, state: .state, health: .health, lastError: .lastError, duration: .duration}'
```

Expected output: one object per alerting rule with `state` in `{inactive, pending, firing}` and `health` of `ok`. A non-empty `lastError` or `health: "err"` means the PromQL expression failed to evaluate (syntax error, missing metric, type mismatch). A `pending` state that never reaches `firing` indicates the condition resolves before the rule's `for` duration elapses.

### Step 2: Evaluate the alert expression directly against current data

```bash
# Substitute the exact expr from the alert rule.
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=up{job="my-service"} == 0' \
  | jq '.data.result'
# Confirm the metric exists at all by dropping the threshold:
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=up{job="my-service"}' \
  | jq '.data.result | length'
```

Expected output: a non-empty `result` array when the alert condition is true; an empty array `[]` when the condition is not currently met. If the unfiltered query also returns 0 results, the metric is missing — the scrape is failing, the metric was renamed, or the label selector does not match actual label values.

### Step 3: Check Prometheus's view of its Alertmanager peers

```bash
curl -s http://localhost:9090/api/v1/alertmanagers \
  | jq '{active: .data.activeAlertmanagers, dropped: .data.droppedAlertmanagers}'
```

Expected output: `active` contains one entry per Alertmanager peer with a `url` like `http://alertmanager:9093/api/v2/alerts`; `dropped` is empty. An empty `active` array means Prometheus has no Alertmanager configured or service discovery resolved no peers. Entries in `dropped` indicate peers that failed health checks.

### Step 4: Check Prometheus notification delivery error metrics

```bash
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=prometheus_notifications_errors_total' \
  | jq '.data.result[] | {alertmanager: .metric.alertmanager, value: .value[1]}'
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=rate(prometheus_notifications_errors_total[5m])' \
  | jq '.data.result[] | {alertmanager: .metric.alertmanager, error_rate: .value[1]}'
curl -sG 'http://localhost:9090/api/v1/query' \
  --data-urlencode 'query=prometheus_notifications_dropped_total' \
  | jq '.data.result[] | {value: .value[1]}'
```

Expected output: `prometheus_notifications_errors_total` flat per peer and `rate(...)` equal to `0`. Any sustained non-zero rate means Prometheus is failing to deliver fired alerts to that Alertmanager peer. `prometheus_notifications_dropped_total` increases when the notification queue overflows because Alertmanager is slow or unreachable.

### Step 5: Check whether alerts are reaching Alertmanager

```bash
curl -s http://localhost:9093/api/v2/alerts \
  | jq '[.[] | {labels, status, startsAt, endsAt}]'
curl -s 'http://localhost:9093/api/v2/alerts?active=true&silenced=true&inhibited=true' \
  | jq '[.[] | {labels: .labels, status: .status.state, silencedBy: .status.silencedBy, inhibitedBy: .status.inhibitedBy}]'
```

Expected output: every alert currently firing in Prometheus (Step 1) appears with matching labels in Alertmanager. Alerts present here with `status.state == "active"` but no notification mean the loss is downstream (routing, silence, inhibition, or receiver). Alerts missing entirely mean the loss is upstream (Steps 3–4).

### Step 6: List active silences and check whether any matches the alert

```bash
curl -s http://localhost:9093/api/v2/silences \
  | jq '[.[] | select(.status.state=="active") | {id, matchers, createdBy, startsAt, endsAt, comment}]'
# Or via amtool:
amtool silence query --alertmanager.url=http://localhost:9093 --active
```

Expected output: list of currently active silences, each with `matchers` (label match expressions), `createdBy`, and `endsAt`. An empty list means no silence is in play. Cross-reference each silence's `matchers` against the missing alert's labels (Step 5) — if a silence's matchers all match, that silence is suppressing the alert.

### Step 7: Test the Alertmanager routing tree with amtool

```bash
# Use the exact label set from the missing alert.
amtool config routes test --config.file=/etc/alertmanager/alertmanager.yml \
  severity=critical alertname=ServiceDown service=my-app
amtool config routes show --config.file=/etc/alertmanager/alertmanager.yml
```

Expected output: the first command prints the name(s) of the receiver(s) the labels would route to. The second prints the routing tree as a text outline. If the resolved receiver is `default` (or any receiver that does not deliver to the expected channel) when a specific receiver was expected, the routing tree's `matchers` / `match` / `match_re` do not match the alert's labels.

### Step 8: Check active mute or time intervals

```bash
curl -s http://localhost:9093/api/v2/status \
  | jq '.config.original' -r \
  | grep -A 30 -E 'time_intervals|mute_time_intervals|active_time_intervals'
date -u
```

Expected output: configured `time_intervals` blocks with `times`, `weekdays`, `months`, and `location`, plus the routes that reference them via `mute_time_intervals` or `active_time_intervals`. Compare the current UTC time and the configured `location` (IANA timezone) against each interval — if the current time falls inside a `mute_time_intervals` window or outside an `active_time_intervals` window, that route is intentionally muted.

### Step 9: Check inhibition rules

```bash
curl -s http://localhost:9093/api/v2/status \
  | jq '.config.original' -r \
  | grep -A 20 -E 'inhibit_rules'
curl -s 'http://localhost:9093/api/v2/alerts?inhibited=true' \
  | jq '[.[] | {labels: .labels, inhibitedBy: .status.inhibitedBy}]'
```

Expected output: configured inhibition rules with `source_matchers`, `target_matchers`, and `equal` lists; plus any alerts currently inhibited and the IDs of the source alerts inhibiting them. An inhibition rule with broad `target_matchers` (e.g., matching all `severity=warning`) suppresses every warning alert whenever any critical alert with the same `equal` labels is firing.

### Step 10: Validate Prometheus and Alertmanager configuration with promtool / amtool

```bash
promtool check config /etc/prometheus/prometheus.yml
promtool check rules /etc/prometheus/rules/*.yml
amtool check-config /etc/alertmanager/alertmanager.yml
```

Expected output: each command exits 0 and prints `SUCCESS` or the number of validated rules. Any non-zero exit indicates the on-disk config is invalid — the last successful reload may be far in the past, and the running config does not match the file on disk.

### Step 11: Read Alertmanager logs for receiver delivery failures

```bash
journalctl -u alertmanager --since "1 hour ago" --no-pager \
  | grep -iE 'notify|error|failed|timeout|deadline|refused|401|403|429|5[0-9][0-9]'
# Kubernetes:
kubectl logs -n monitoring -l app.kubernetes.io/name=alertmanager --tail=500 \
  | grep -iE 'notify|error|failed|timeout|deadline|refused|401|403|429|5[0-9][0-9]'
```

Expected output: ideally no error lines for the failing receiver. Errors include `msg="Notify for alerts failed" ... err="..."`, `context deadline exceeded`, `dial tcp ... connection refused`, `HTTP 401 Unauthorized`, `HTTP 403 Forbidden`, `HTTP 429 Too Many Requests`, or SMTP error codes. Each error line names the receiver and the underlying transport failure.

### Step 12: Verify the Prometheus → Alertmanager → receiver end-to-end path with a synthetic alert

```bash
# Post a synthetic alert directly to Alertmanager.
curl -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"E2ETest","severity":"info","service":"runbook-test"},"annotations":{"summary":"End-to-end pipeline test"},"startsAt":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}]'
# Wait one group_wait, then check delivery:
sleep 60
curl -s http://localhost:9093/api/v2/alerts | jq '.[] | select(.labels.alertname=="E2ETest")'
```

Expected output: the synthetic alert appears in `/api/v2/alerts`, and within `group_wait` (default 30s) the configured receiver for that label set delivers a notification. No notification arriving despite the alert being present narrows the failure to receiver delivery (Step 11) or silences/inhibitions/time intervals (Steps 6, 8, 9).

## Causes

### Cause A: Alert rule expression returns empty against current data

**Statement:** The alert rule's PromQL expression evaluates to an empty result against current metrics, so the rule stays `inactive` and never transitions to `pending` or `firing`.

**Mechanism:** Prometheus evaluates the `expr` on each evaluation interval and only counts an alert as active when the expression returns a non-empty instant vector. Common causes of empty results are scrape failures that drop the underlying metric, label-value mismatches between the rule and the actual time series (case, spelling, or relabeling differences), or thresholds inverted relative to the metric's direction. The rule stays `inactive` until the expression matches — there is no log line announcing this, only the absence of state transitions.

**Indicator:**

- [Step 1] the rule's `state` is `inactive` and `health` is `ok` even when the underlying condition is known to be true
- [Step 2] the alert's `expr` returns an empty `result` array, and the same query without the threshold filter also returns 0 series

<!-- match: {"step": 2, "predicate": "contains", "target": "\"result\":[]"} -->

**Mitigation:**

- **Risk:** Loosening the expression temporarily can mask real label issues; document the change and revert once the metric path is fixed.
- **Command:**

  ```bash
  # Reproduce against the running TSDB and iterate label-by-label until the query returns results.
  curl -sG 'http://localhost:9090/api/v1/query' --data-urlencode 'query=up{job="my-service"}'
  curl -sG 'http://localhost:9090/api/v1/query' --data-urlencode 'query=count by (__name__) ({job="my-service"})'
  ```

- **Duration:** Diagnostic only; do not leave a loosened expression in place beyond the current shift.

**Resolution:**

```yaml
# rules/service-alerts.yml — correct the labels and threshold against actual metric data.
groups:
  - name: service-alerts
    rules:
      - alert: ServiceDown
        expr: up{job="my-service"} == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Service {{ $labels.instance }} is down"
```

```bash
promtool check rules /etc/prometheus/rules/service-alerts.yml
curl -X POST http://localhost:9090/-/reload
```

- **Impact:** Single rule file; takes effect on the next evaluation interval after reload. The rule will enter `pending` immediately if the condition is true and transition to `firing` after `for`.
- **Rollback:** `git revert` the rule-file change and reload Prometheus.

**Verification:** Re-run Step 2 and confirm the expression returns a non-empty `result`. Re-run Step 1 and confirm the rule moves to `pending` then `firing` within `for + scrape_interval`.

### Cause B: Alert stays in pending and never reaches firing because `for` is longer than the condition persists

**Statement:** The rule's `for` duration is longer than the underlying condition persists, so the alert moves to `pending` and then back to `inactive` without ever reaching `firing`.

**Mechanism:** Prometheus requires the expression to return a non-empty result on every evaluation throughout the `for` window before transitioning the alert to `firing`. If the metric flaps — for example, a probe that succeeds intermittently or a scrape that misses one cycle — the `pending` counter resets to zero and the alert never matures. The rule looks healthy in the UI (`health: "ok"`), but no notification ever leaves Prometheus.

**Indicator:**

- [Step 1] the rule's `state` repeatedly shows `pending` but never reaches `firing` across multiple polls
- [Step 2] the expression returns results some of the time but not on every evaluation
- [Symptom] dashboards confirm the underlying condition is real but transient on the scrape-interval timescale

<!-- match: {"step": 1, "predicate": "contains", "target": "\"state\":\"pending\""} -->

**Mitigation:**

- **Risk:** Shortening `for` increases sensitivity to flapping and may produce spurious pages; combine with `keep_firing_for` to smooth resolution.
- **Command:**

  ```yaml
  # Reduce for to match actual condition persistence; keep_firing_for prevents flapping at resolution.
  - alert: ServiceDown
    expr: up{job="my-service"} == 0
    for: 1m
    keep_firing_for: 5m
    labels:
      severity: critical
  ```

  ```bash
  promtool check rules /etc/prometheus/rules/service-alerts.yml
  curl -X POST http://localhost:9090/-/reload
  ```

- **Duration:** Permanent once tuned against observed scrape intervals.

**Resolution:**

```yaml
# rules/service-alerts.yml — tune for against observed condition persistence; pair with keep_firing_for.
- alert: ServiceDown
  expr: up{job="my-service"} == 0
  for: 1m
  keep_firing_for: 5m
  labels:
    severity: critical
```

```bash
promtool check rules /etc/prometheus/rules/service-alerts.yml
curl -X POST http://localhost:9090/-/reload
```

- **Impact:** Single rule; affects only the timing of state transitions for the named alert.
- **Rollback:** Restore the original `for` value and reload.

**Verification:** Trigger the condition (or wait for the next real occurrence) and observe in Step 1 that the rule transitions `inactive → pending → firing` within `for + scrape_interval`. Confirm in Step 5 that Alertmanager receives the alert.

### Cause C: Prometheus has no active Alertmanager peer

**Statement:** Prometheus's `alerting.alertmanagers` configuration is empty, points at the wrong target, or resolves to no peers via service discovery, so fired alerts have nowhere to go.

**Mechanism:** Prometheus only sends alerts to peers in its `activeAlertmanagers` list. The list is built from `static_configs` or service discovery under `alerting.alertmanagers`. When the DNS name does not resolve, the Kubernetes Service has no endpoints, a NetworkPolicy blocks port 9093, or the section is missing entirely, the list is empty. Alerts fire inside Prometheus (visible in `/api/v1/alerts`) but never reach Alertmanager, so no notification is produced.

**Indicator:**

- [Step 3] `data.activeAlertmanagers` is an empty array, or all peers appear under `droppedAlertmanagers`
- [Step 5] `/api/v2/alerts` on Alertmanager shows no alerts despite Prometheus showing rules in `firing`

<!-- match: {"step": 3, "predicate": "contains", "target": "\"active\":[]"} -->

**Mitigation:**

- **Risk:** Updating the Alertmanager endpoint does not affect existing alert rules or in-flight alerts; it only redirects future deliveries.
- **Command:**

  ```yaml
  # prometheus.yml — point at the cluster Alertmanager Service for HA, list all peers explicitly.
  alerting:
    alertmanagers:
      - static_configs:
          - targets:
              - alertmanager-0.alertmanager.monitoring.svc:9093
              - alertmanager-1.alertmanager.monitoring.svc:9093
              - alertmanager-2.alertmanager.monitoring.svc:9093
  ```

  ```bash
  promtool check config /etc/prometheus/prometheus.yml
  curl -X POST http://localhost:9090/-/reload
  ```

- **Duration:** Permanent. List every Alertmanager peer individually so each receives a copy of the alert (Alertmanager deduplicates via gossip).

**Resolution:**

```yaml
# prometheus.yml — list every Alertmanager peer so Prometheus sends to all and Alertmanager gossip deduplicates.
alerting:
  alertmanagers:
    - static_configs:
        - targets:
            - alertmanager-0.alertmanager.monitoring.svc:9093
            - alertmanager-1.alertmanager.monitoring.svc:9093
            - alertmanager-2.alertmanager.monitoring.svc:9093
```

```bash
promtool check config /etc/prometheus/prometheus.yml
curl -X POST http://localhost:9090/-/reload
```

- **Impact:** Cluster-wide — restores notification delivery for every fired alert. Takes effect on the next evaluation after reload.
- **Rollback:** Revert `prometheus.yml` and reload.

**Verification:** Re-run Step 3; `activeAlertmanagers` must contain one entry per configured peer. Re-run Step 4; `rate(prometheus_notifications_errors_total[5m])` must be zero. Within one `group_wait`, an existing firing alert must appear in `/api/v2/alerts` (Step 5).

### Cause D: Active silence matches the alert's labels

**Statement:** An active silence's `matchers` all match the alert's labels, so Alertmanager accepts the alert but suppresses every notification until the silence expires.

**Mechanism:** When an alert arrives, Alertmanager checks every active silence's `matchers` against the alert's labels. If all matchers for any one silence match, the alert is marked `suppressed` and is not dispatched to any receiver. Silences are commonly created during maintenance windows and forgotten — the `endsAt` may be hours, days, or weeks in the future. The alert is visible in `/api/v2/alerts` with `status.state == "suppressed"` and `status.silencedBy` populated.

**Indicator:**

- [Step 5] the alert is present with `status.state == "suppressed"` and a non-empty `silencedBy` array
- [Step 6] at least one silence is in `active` state and its `matchers` match the alert's labels

<!-- match: {"step": 5, "predicate": "contains", "target": "\"suppressed\""} -->

**Mitigation:**

- **Risk:** Expiring a silence immediately re-enables notifications for every matched alert; if many alerts match, expect a notification burst.
- **Command:**

  ```bash
  amtool silence query --alertmanager.url=http://localhost:9093 --active
  amtool silence expire <SILENCE_ID> --alertmanager.url=http://localhost:9093
  ```

- **Duration:** Immediate.

**Resolution:**

```bash
# Same as Mitigation. If the silence is on a maintenance schedule, narrow the matchers or shorten endsAt instead of expiring.
amtool silence expire <SILENCE_ID> --alertmanager.url=http://localhost:9093
```

- **Impact:** Single silence; affects only alerts whose labels match the removed silence's matchers.
- **Rollback:** Re-create the silence with the same matchers via `amtool silence add` or `POST /api/v2/silences`.

**Verification:** Re-run Step 6 and confirm the silence ID is no longer in the active list. Re-run Step 5 and confirm the alert's `status.state` is `active` (not `suppressed`) and `silencedBy` is empty. The next `group_interval` after expiry must deliver a notification.

### Cause E: Routing tree does not match the alert's labels, alert falls through to an unintended receiver

**Statement:** The Alertmanager routing tree's `matchers` / `match` / `match_re` do not select any child route for this alert, so it falls back to the root route's receiver instead of the intended specific receiver.

**Mechanism:** Alertmanager walks the routing tree top-down. The first child route whose matchers match the alert's labels claims the alert (unless `continue: true` is set). If no child route matches, the alert is delivered to the root route's receiver. A common misconfiguration is a typo in a matcher label name or value (`servce` vs `service`, `Critical` vs `critical`), a child route using `match_re` with a pattern that misses the label format, or a missing label on the alert rule itself (the routing matcher expects `team=payments` but the rule only sets `service=payments`).

**Indicator:**

- [Step 7] `amtool config routes test` with the alert's exact labels resolves to a receiver other than the one expected (often the root/default)
- [Step 5] the alert is present in `/api/v2/alerts` with `status.state == "active"` but the wrong receiver gets notified (or the silent default receiver gets it)

<!-- match: {"step": 7, "predicate": "contains", "target": "default"} -->

**Mitigation:**

- **Risk:** Routing changes affect every alert in the system; always validate with `amtool config routes test` against representative label sets before reload.
- **Command:**

  ```yaml
  # alertmanager.yml — explicit matchers for each child route.
  route:
    receiver: 'default-slack'
    group_by: ['alertname', 'service', 'severity']
    group_wait: 30s
    group_interval: 5m
    repeat_interval: 4h
    routes:
      - matchers:
          - severity = "critical"
        receiver: 'pagerduty-critical'
        continue: false
      - matchers:
          - severity = "warning"
        receiver: 'slack-warnings'
        continue: false
  ```

  ```bash
  amtool check-config /etc/alertmanager/alertmanager.yml
  amtool config routes test --config.file=/etc/alertmanager/alertmanager.yml \
    severity=critical alertname=ServiceDown service=my-app
  curl -X POST http://localhost:9093/-/reload
  ```

- **Duration:** Permanent. Keep test cases for representative label sets in CI alongside the routing config.

**Resolution:**

```yaml
# alertmanager.yml — child routes with explicit matchers; validate before reload.
route:
  receiver: 'default-slack'
  group_by: ['alertname', 'service', 'severity']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: [severity = "critical"]
      receiver: 'pagerduty-critical'
    - matchers: [severity = "warning"]
      receiver: 'slack-warnings'
```

```bash
amtool check-config /etc/alertmanager/alertmanager.yml
amtool config routes test --config.file=/etc/alertmanager/alertmanager.yml severity=critical alertname=ServiceDown service=my-app
curl -X POST http://localhost:9093/-/reload
```

- **Impact:** Cluster-wide for the Alertmanager instance. Affects every alert routed through the changed subtree. Takes effect on next reload.
- **Rollback:** `git revert` the `alertmanager.yml` change and reload.

**Verification:** Re-run Step 7 with the same label set; the output must name the expected receiver. Send a synthetic alert via Step 12 with those exact labels and confirm delivery at the expected channel.

### Cause F: Inhibition rule with broad target matchers suppresses the alert

**Statement:** An active inhibition rule's `target_matchers` match the alert's labels while a source alert is firing, so the target alert is accepted but silenced for as long as the source persists.

**Mechanism:** Inhibition is configured as `source_matchers`, `target_matchers`, and an `equal` label list. When a source-matching alert is `firing`, every target-matching alert with the same `equal` label values is marked `inhibited` and is not dispatched. Inhibition is intentional for cases like "node down inhibits per-pod alerts on that node", but overly broad rules (e.g., a `severity=critical` source matching all critical alerts, with `target_matchers` matching all `severity=warning`) silently swallow large classes of warning alerts whenever any critical alert is firing.

**Indicator:**

- [Step 5] alerts appear with `status.state == "suppressed"` and a non-empty `inhibitedBy` array, but no `silencedBy`
- [Step 9] an `inhibit_rules` block has `target_matchers` that match the missing alert's labels, and at least one source alert is currently firing

<!-- match: {"step": 5, "predicate": "contains", "target": "\"inhibitedBy\""} -->

**Mitigation:**

- **Risk:** Narrowing inhibition can produce notification floods if the original broad rule was masking a known cascade; coordinate with the on-call team before tightening.
- **Command:**

  ```yaml
  # alertmanager.yml — narrow target_matchers and add labels to the equal list.
  inhibit_rules:
    - source_matchers:
        - severity = "critical"
        - alertname = "NodeDown"
      target_matchers:
        - severity = "warning"
        - alertname =~ "PodNotReady|PodRestart"
      equal: [cluster, node]
  ```

  ```bash
  amtool check-config /etc/alertmanager/alertmanager.yml
  curl -X POST http://localhost:9093/-/reload
  ```

- **Duration:** Permanent.

**Resolution:**

```yaml
# alertmanager.yml — narrow target_matchers and add labels to equal so inhibition only covers the intended cascade.
inhibit_rules:
  - source_matchers:
      - severity = "critical"
      - alertname = "NodeDown"
    target_matchers:
      - severity = "warning"
      - alertname =~ "PodNotReady|PodRestart"
    equal: [cluster, node]
```

```bash
amtool check-config /etc/alertmanager/alertmanager.yml
curl -X POST http://localhost:9093/-/reload
```

- **Impact:** Cluster-wide for the Alertmanager instance. Previously-inhibited target alerts will start delivering on the next dispatch cycle.
- **Rollback:** Revert the `inhibit_rules` block and reload.

**Verification:** Re-run Step 9; the previously inhibited alert must no longer appear under `inhibited=true`. Confirm via Step 5 that `status.state` is `active` and `inhibitedBy` is empty. A notification must arrive on the next `group_interval`.

### Cause G: Mute time interval is in effect for the route

**Statement:** The route handling the alert references a `mute_time_intervals` entry whose current window matches the local time, so all notifications on that route are silenced.

**Mechanism:** `time_intervals` define recurring time windows (by hour, weekday, day-of-month, month, year, IANA timezone). A route's `mute_time_intervals` lists intervals during which the route delivers nothing; `active_time_intervals` lists the only windows during which it delivers. Time-window mutes are commonly used to suppress non-urgent paging outside business hours. The alert is accepted, routed, and grouped — but the dispatcher drops it before sending. The alert is visible in `/api/v2/alerts` as `active`, and no log line announces the mute.

**Indicator:**

- [Step 5] the alert is `active` with no `silencedBy` and no `inhibitedBy`
- [Step 8] a `time_intervals` block matches the current time in the configured `location`, and a route uses it under `mute_time_intervals`

<!-- match: {"step": 8, "predicate": "contains", "target": "mute_time_intervals"} -->

**Mitigation:**

- **Risk:** Removing the mute restores out-of-hours paging; coordinate with the on-call team before applying.
- **Command:**

  ```yaml
  # alertmanager.yml — remove or narrow the mute_time_intervals reference for the affected route.
  route:
    routes:
      - matchers: [severity = "warning"]
        receiver: 'slack-warnings'
        # mute_time_intervals: ['weekends']   # remove or comment out to restore weekend delivery
  ```

  ```bash
  amtool check-config /etc/alertmanager/alertmanager.yml
  curl -X POST http://localhost:9093/-/reload
  ```

- **Duration:** Permanent (until the policy is revised).

**Resolution:**

```yaml
# alertmanager.yml — remove or narrow mute_time_intervals on the affected route.
route:
  routes:
    - matchers: [severity = "warning"]
      receiver: 'slack-warnings'
      # mute_time_intervals: ['weekends']   # remove or narrow to restore delivery
```

```bash
amtool check-config /etc/alertmanager/alertmanager.yml
curl -X POST http://localhost:9093/-/reload
```

- **Impact:** Affects only the specific route's delivery schedule.
- **Rollback:** Restore the `mute_time_intervals` reference and reload.

**Verification:** Re-run Step 8; the route must no longer reference the active interval, or the current time must fall outside any referenced mute interval. Step 12 synthetic alert must deliver during what was previously a muted window.

### Cause H: Receiver credentials are invalid or expired (Slack, PagerDuty, webhook, SMTP)

**Statement:** Alertmanager dispatches the alert to the receiver but the receiver endpoint rejects the request with an authentication error or unreachable status, so no notification reaches the destination channel.

**Mechanism:** Each receiver type (Slack, PagerDuty, OpsGenie, webhook, email) authenticates with a credential — webhook URL, integration key, SMTP password, API token. Credentials expire, get rotated, or are pasted with a typo. Alertmanager logs the failure as `msg="Notify for alerts failed" ... err="..."` with the underlying HTTP status (`401`, `403`, `429`) or transport error (`dial tcp ... connection refused`, `context deadline exceeded`). Alertmanager retries per `retry` config but eventually drops the notification; `alertmanager_notifications_failed_total` increments.

**Indicator:**

- [Step 11] Alertmanager logs show `Notify for alerts failed` for the affected receiver with `401`, `403`, `429`, a `5xx`, `connection refused`, or `context deadline exceeded`
- [Step 12] the synthetic alert reaches `/api/v2/alerts` but the receiver does not deliver a notification

<!-- match: {"step": 11, "predicate": "contains", "target": "Notify for alerts failed"} -->

**Mitigation:**

- **Risk:** Pasting credentials into the wrong receiver block routes alerts to the wrong destination; validate the receiver name before reload.
- **Command:**

  ```yaml
  # alertmanager.yml — update the credential for the failing receiver.
  receivers:
    - name: 'pagerduty-critical'
      pagerduty_configs:
        - routing_key: '<NEW_INTEGRATION_KEY>'
          severity: '{{ .CommonLabels.severity }}'
    - name: 'default-slack'
      slack_configs:
        - api_url_file: /etc/alertmanager/secrets/slack-webhook-url
          channel: '#alerts'
  ```

  ```bash
  amtool check-config /etc/alertmanager/alertmanager.yml
  curl -X POST http://localhost:9093/-/reload
  ```

- **Duration:** Permanent (until the credential is rotated again).

**Resolution:**

```yaml
# alertmanager.yml — store credentials in *_file variants so secret rotation is a file write, not a config edit.
receivers:
  - name: 'pagerduty-critical'
    pagerduty_configs:
      - routing_key_file: /etc/alertmanager/secrets/pagerduty-routing-key
        severity: '{{ .CommonLabels.severity }}'
  - name: 'default-slack'
    slack_configs:
      - api_url_file: /etc/alertmanager/secrets/slack-webhook-url
        channel: '#alerts'
```

```bash
amtool check-config /etc/alertmanager/alertmanager.yml
curl -X POST http://localhost:9093/-/reload
```

- **Impact:** Affects only the named receiver; other receivers continue delivering.
- **Rollback:** Restore the previous credential and reload. If the previous credential is also invalid, fall through to the on-call escalation path.

**Verification:** Re-run Step 12 (synthetic alert) with labels routed to this receiver and confirm a notification arrives. Re-run Step 11 and confirm no further `Notify for alerts failed` entries for this receiver.

### Cause I: Configuration file is invalid and the running config does not match disk

**Statement:** A recent edit to `prometheus.yml`, an alert rule file, or `alertmanager.yml` failed validation, so the daemon kept its last good in-memory config and the on-disk changes never took effect.

**Mechanism:** `promtool check rules`, `promtool check config`, and `amtool check-config` exit non-zero on syntax or semantic errors. A `/-/reload` against a daemon with a broken on-disk config logs an error and leaves the running config unchanged. The operator believes the change is live (the file is committed and deployed) but the daemon still serves the previous rules, routes, and receivers. Subsequent fixes are also blocked because every reload attempts to load the same broken file.

**Indicator:**

- [Step 10] `promtool check rules`, `promtool check config`, or `amtool check-config` exits non-zero with a parse or schema error
- [Step 11] Alertmanager logs contain `error loading config` or `msg="Loading configuration file failed"`
- [Symptom] a recent commit to the rules or routing config does not appear to take effect in Step 1 or Step 7

<!-- match: {"step": 10, "predicate": "exit_code", "target": 1} -->

**Mitigation:**

- **Risk:** Reverting to the last good config rolls back any intentional changes in the broken commit; capture the diff before reverting.
- **Command:**

  ```bash
  # Revert the broken commit, validate, and reload.
  git -C /etc/prometheus log -n 5 --oneline
  git -C /etc/prometheus revert <BROKEN_SHA> --no-edit
  promtool check config /etc/prometheus/prometheus.yml
  promtool check rules /etc/prometheus/rules/*.yml
  curl -X POST http://localhost:9090/-/reload
  # Alertmanager equivalent:
  git -C /etc/alertmanager revert <BROKEN_SHA> --no-edit
  amtool check-config /etc/alertmanager/alertmanager.yml
  curl -X POST http://localhost:9093/-/reload
  ```

- **Duration:** Permanent until the original change is corrected.

**Resolution:**

```bash
# Revert broken commits on each side, validate, reload, then re-introduce the fix on a branch.
git -C /etc/prometheus revert <BROKEN_SHA> --no-edit
promtool check config /etc/prometheus/prometheus.yml
promtool check rules /etc/prometheus/rules/*.yml
curl -X POST http://localhost:9090/-/reload
git -C /etc/alertmanager revert <BROKEN_SHA> --no-edit
amtool check-config /etc/alertmanager/alertmanager.yml
curl -X POST http://localhost:9093/-/reload
```

- **Impact:** Cluster-wide; restores the last known-good config for the affected daemon.
- **Rollback:** Cherry-pick the reverted commit back once the syntax is fixed.

**Verification:** Re-run Step 10; all three commands must exit 0. Re-check Step 1 (Prometheus rules reflect the file on disk) and Step 7 (`amtool config routes show` matches the file on disk). The `prometheus_config_last_reload_successful` and `alertmanager_config_last_reload_successful` gauges must read `1`.

### Cause Z: Unidentified

**Statement:** Diagnostics confirm that an expected alert is not being delivered, but no Cause A–I indicator matches the gathered evidence.

**Mechanism:** The rule evaluates correctly (Steps 1–2), Prometheus has active Alertmanager peers (Step 3) with no error rate (Step 4), the alert reaches Alertmanager (Step 5), no silence (Step 6), routing (Step 7), mute interval (Step 8), or inhibition (Step 9) matches the alert's labels, configs validate (Step 10), and receiver logs (Step 11) show no errors — yet the synthetic alert (Step 12) does not produce a notification. The driver is outside the controlled vocabulary above (custom routing logic, a downstream receiver that accepts and drops, network gear between Alertmanager and the receiver, vendor-side incident filtering).

**Indicator:**

- [Default] symptom is confirmed but Causes A–I indicators do not match the evidence

**Mitigation:**

- **Risk:** Capturing diagnostics is read-only and safe; raising alert verbosity may produce a brief notification burst.
- **Command:**

  ```bash
  # Capture diagnostic artefacts for handoff.
  curl -s http://localhost:9090/api/v1/rules > rules.json
  curl -s http://localhost:9090/api/v1/alertmanagers > alertmanagers.json
  curl -s http://localhost:9093/api/v2/alerts > am-alerts.json
  curl -s http://localhost:9093/api/v2/silences > silences.json
  curl -s http://localhost:9093/api/v2/status > am-status.json
  kubectl logs -n monitoring -l app.kubernetes.io/name=alertmanager --tail=1000 > am-logs.txt
  kubectl logs -n monitoring -l app.kubernetes.io/name=prometheus --tail=1000 > prom-logs.txt
  ```

- **Duration:** Hours, not days. Hold the captured artefacts while engaging the observability owner or vendor support.

**Resolution:** Out of runbook scope. Hand off the captured `rules.json`, `alertmanagers.json`, `am-alerts.json`, `silences.json`, `am-status.json`, and the two log files to the observability owner or vendor support. Open an incident ticket with the failure-mode summary, the synthetic-alert payload from Step 12, and a named follow-up owner.

**Verification:** Receiving engineer acknowledges the handoff; an incident ticket is opened with all captured artefacts attached and a named owner assigned for follow-up.

## Prevention

- Run a Watchdog / dead-man's switch: define an alert with `expr: vector(1)` that always fires, route it to a heartbeat endpoint (PagerDuty heartbeat, Healthchecks.io, Dead Man's Snitch). The heartbeat service pages when the heartbeat stops — that is the only reliable signal that the entire pipeline is healthy end-to-end.
- Validate every config change in CI before merge: `promtool check config prometheus.yml`, `promtool check rules rules/*.yml`, `amtool check-config alertmanager.yml`, plus `amtool config routes test` against a fixture of representative label sets.
- Alert on the alerting infrastructure: `rate(prometheus_notifications_errors_total[5m]) > 0`, `rate(prometheus_rule_evaluation_failures_total[5m]) > 0`, `prometheus_config_last_reload_successful == 0`, `alertmanager_config_last_reload_successful == 0`, `alertmanager_notifications_failed_total` increasing. Route these meta-alerts to a different receiver than the primary pipeline.
- Enforce silence hygiene: every silence must have a `comment` (reason + ticket), a `createdBy`, and an `endsAt` no further than 24 hours out. Audit active silences daily via a scheduled `amtool silence query --active` report.
- Store `prometheus.yml`, rule files, and `alertmanager.yml` in version control. Review routing and inhibition changes in pull requests with the `amtool config routes show` and `amtool config routes test` output as PR comments.
- Configure receiver redundancy: add a fallback `webhook_configs` or `email_configs` in critical receivers so a single vendor outage does not silence the pipeline. Use `*_file` variants for every credential so secret rotation is a file write, not a config edit.
- Review `group_wait`, `group_interval`, `repeat_interval`, and `for` values quarterly against the team's response-time SLAs. Defaults (30s / 5m / 4h) are appropriate for most teams but may be too slow for SEV1-only routes.
- Keep at least two Alertmanager peers in the `alerting.alertmanagers` list on the Prometheus side, configured to gossip — Prometheus sends each alert to every peer and Alertmanager deduplicates. A single peer is a single point of failure for the entire notification path.
- Pin Alertmanager and Prometheus minor versions in production and upgrade in a non-prod environment first. Alertmanager 0.27 removed API v1 — clients hitting `/api/v1/*` after upgrade silently fail.

## Sources

- [Prometheus — Alertmanager Configuration](https://prometheus.io/docs/alerting/latest/configuration/) — Priority 1. Routing tree semantics, `matchers` / `match` / `match_re` syntax, `group_by` / `group_wait` / `group_interval` / `repeat_interval` defaults, receiver definitions, `inhibit_rules`, `time_intervals`, `mute_time_intervals` vs `active_time_intervals`, and reload behaviour.
- [Prometheus — Alertmanager Overview](https://prometheus.io/docs/alerting/latest/alertmanager/) — Priority 1. Grouping, inhibition, and silence semantics; `--alerts.per-alertname-limit` flag; high-availability gossip and deduplication model.
- [Prometheus — Alerting Rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/) — Priority 1. Rule syntax (`expr`, `for`, `keep_firing_for`, `labels`, `annotations`), `inactive → pending → firing` state machine, and templating.
- [Prometheus — Alerting Best Practices](https://prometheus.io/docs/practices/alerting/) — Priority 1. Symptom-based alerting, metamonitoring of the alerting pipeline, and end-to-end blackbox tests preferred over per-hop checks.
- [Prometheus — Management API](https://prometheus.io/docs/prometheus/latest/management_api/) — Priority 1. `/-/reload`, `/-/healthy`, `/-/ready` endpoints; `--web.enable-lifecycle` flag requirement.
- [Alertmanager — README and amtool reference](https://github.com/prometheus/alertmanager/blob/main/README.md) — Priority 1. `amtool` subcommands (`alert query`, `silence add/query/expire`, `config routes show/test`, `check-config`, `template render`); `/api/v2/alerts`, `/api/v2/silences`, `/api/v2/status` endpoints; API v1 removal in 0.27.
