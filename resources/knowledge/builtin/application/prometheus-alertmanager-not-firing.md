---
id: prometheus-alertmanager-not-firing
title: "Prometheus Alerts Not Firing"
domain: application
service: prometheus
symptom_class:
  - service_unavailable
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
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

## Problem Definition

Applies to Prometheus 2.x+ and Alertmanager 0.25+. Requires access to the Prometheus web UI or API (port 9090), Alertmanager web UI or API (port 9093), and `amtool` CLI for routing tests. Admin access to `prometheus.yml` and `alertmanager.yml` configuration files is needed for fixes.

Alerts fail to fire or notifications fail to deliver despite known outage conditions. The failure can occur at three stages in the pipeline: Prometheus alert rule evaluation (rule never transitions to `firing`), Prometheus-to-Alertmanager delivery (alerts fire in Prometheus but never reach Alertmanager), or Alertmanager notification delivery (alerts reach Alertmanager but receivers do not send notifications). Prometheus UI shows alert rules stuck in `inactive` state when the underlying expression should be true. The `prometheus_notifications_errors_total` counter incrementing indicates delivery failures to Alertmanager. Alertmanager logs show errors like `send notification failed`, `context deadline exceeded`, or HTTP 4xx/5xx from receiver endpoints. Silences or inhibition rules may suppress alerts without visible indication unless specifically checked. The `for` clause may be longer than the condition persists, causing the alert to resolve before transitioning from `pending` to `firing`.

## Diagnostic Steps

### 1. Check alert rule status and health in Prometheus

Determines whether alert rules are evaluating correctly and identifies rules with evaluation errors.

```bash
curl -s http://localhost:9090/api/v1/rules | \
  jq '.data.groups[].rules[] | select(.type=="alerting") | {name: .name, state: .state, health: .health, lastError: .lastError}'
```

**Expected output:** Rules should show `health: "ok"`. State should be `inactive` (condition not met), `pending` (condition met, waiting for `for` duration), or `firing` (condition met for `for` duration).

**What this means:** `health: "err"` with a `lastError` value indicates the PromQL expression has a syntax error or references a missing metric. State `inactive` when the condition is known to be true means the expression does not match current data -- test it directly (step 2). State `pending` that never transitions to `firing` means the condition resolves before the `for` duration elapses.

### 2. Test the alert expression directly in Prometheus

Confirms whether the alert condition's PromQL expression actually returns results against current data.

```bash
curl -s 'http://localhost:9090/api/v1/query?query=up{job="my-service"}==0' | jq '.data.result'
```

Replace the query with the exact `expr` from the alert rule.

**Expected output:** Non-empty result array if the condition is true. Empty array `[]` if the condition is not met.

**What this means:** If the query returns results but the alert is `inactive`, there may be a label mismatch between the rule file and the actual metric labels. If the query returns empty, the condition is genuinely not met -- verify the metric exists and has the expected labels using `up{job="my-service"}` without the `== 0` filter.

### 3. Check Prometheus notification error metrics

Determines whether Prometheus is failing to deliver fired alerts to Alertmanager.

```bash
curl -s 'http://localhost:9090/api/v1/query?query=prometheus_notifications_errors_total' | jq '.data.result[] | {alertmanager: .metric.alertmanager, value: .value[1]}'
curl -s 'http://localhost:9090/api/v1/query?query=rate(prometheus_notifications_errors_total[5m])' | jq '.data.result[0].value[1]'
```

**Expected output:** Error count should be 0 or the rate should be 0. Any non-zero rate means Prometheus is actively failing to send alerts.

**What this means:** Non-zero errors indicate a connectivity problem between Prometheus and Alertmanager. Check network policies, DNS resolution, and whether the Alertmanager endpoint in `prometheus.yml` is correct.

### 4. Verify Prometheus has active Alertmanager targets

Confirms that Prometheus has discovered and can reach at least one Alertmanager instance.

```bash
curl -s http://localhost:9090/api/v1/alertmanagers | jq '.data.activeAlertmanagers'
```

**Expected output:** An array with at least one entry containing the Alertmanager URL (e.g., `http://alertmanager:9093/api/v2/alerts`).

**What this means:** Empty array means Prometheus has no Alertmanager configured or cannot reach any. Check the `alerting.alertmanagers` section in `prometheus.yml`. For Kubernetes, verify the Alertmanager Service and Endpoints exist.

### 5. Check Alertmanager for received alerts

Determines whether alerts are reaching Alertmanager from Prometheus.

```bash
curl -s http://localhost:9093/api/v2/alerts | jq '.[].labels'
```

**Expected output:** If alerts are firing in Prometheus, they should appear here with matching labels.

**What this means:** Alerts present here but no notification received means the problem is in Alertmanager routing, silences, inhibitions, or receiver configuration. No alerts here despite Prometheus showing `firing` means the delivery pipeline is broken (step 3/4).

### 6. Check for active silences suppressing alerts

Identifies silences that may be muting the expected alert notifications.

```bash
curl -s http://localhost:9093/api/v2/silences | \
  jq '.[] | select(.status.state=="active") | {id: .id, matchers: .matchers, createdBy: .createdBy, startsAt: .startsAt, endsAt: .endsAt}'
```

**Expected output:** List of active silences with their matchers. An empty result means no silences are active.

**What this means:** If a silence matcher matches the alert's labels, the notification is suppressed. Silences are often created during maintenance windows and forgotten. Check whether the `endsAt` time is far in the future.

### 7. Test Alertmanager routing with amtool

Determines which receiver an alert with specific labels would be routed to, without sending a real notification.

```bash
amtool config routes test --config.file=/etc/alertmanager/alertmanager.yml \
  severity=critical alertname=ServiceDown service=my-app
```

**Expected output:** The name of the receiver that matches the given labels (e.g., `pagerduty-critical`).

**What this means:** If the output shows the `default` receiver when a specific receiver was expected, the routing labels do not match any child route. The alert falls through to the default. This is the most common routing misconfiguration.

### 8. Check Alertmanager logs for delivery failures

Identifies failures in the notification delivery to external receivers (Slack, PagerDuty, email, webhook).

```bash
journalctl -u alertmanager --since "1 hour ago" --no-pager | grep -iE "error|failed|timeout|rejected"
```

For Kubernetes:

```bash
kubectl logs -n monitoring -l app.kubernetes.io/name=alertmanager --tail=200 | grep -iE "error|failed|timeout|rejected"
```

**Expected output:** No error lines for healthy operation. Errors include `msg="notify retry" err="..."`, HTTP status codes from receiver APIs, SMTP errors, or connection timeouts.

**What this means:** `401 Unauthorized` or `403 Forbidden` from receiver APIs means credentials (webhook URL, API key, integration key) are invalid or expired. Connection timeout means the Alertmanager cannot reach the receiver endpoint (network, firewall, DNS). `429 Too Many Requests` means rate limiting by the receiver.

### 9. Check inhibition rules for unexpected suppression

Identifies inhibition rules that may be silencing alerts because a related higher-severity alert is active.

```bash
curl -s http://localhost:9093/api/v2/status | jq '.config.original' -r | grep -A 15 "inhibit_rules"
```

**Expected output:** List of inhibition rules with `source_matchers`, `target_matchers`, and `equal` fields.

**What this means:** An inhibition rule with broad `target_matchers` (e.g., matching all `severity=warning`) will suppress all warning alerts whenever any critical alert is active with the same `equal` labels. Overly broad inhibition rules are a common cause of missed alerts.

## Mitigation

### Option 1: Fix Prometheus-to-Alertmanager connectivity

**Risk:** Low. Updating the Alertmanager endpoint does not affect existing alert rules or state.

**Command:**

Verify and update the `alerting` section in `prometheus.yml`:

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets:
            - alertmanager:9093
```

Reload Prometheus:

```bash
curl -X POST http://localhost:9090/-/reload
```

**Verify:** `curl -s http://localhost:9090/api/v1/alertmanagers | jq '.data.activeAlertmanagers | length'` returns at least 1.

**Duration:** 1-2 minutes.

### Option 2: Fix alert rule expression or for duration

**Risk:** Low. Correcting an expression restores intended alerting behavior.

**Command:**

Edit the alert rule file:

```yaml
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

Validate syntax before reloading:

```bash
promtool check rules alert-rules.yml
curl -X POST http://localhost:9090/-/reload
```

**Verify:** `curl -s http://localhost:9090/api/v1/rules | jq '.data.groups[].rules[] | select(.name=="ServiceDown") | {state, health}'` shows `health: "ok"`.

**Duration:** Alert enters `pending` after reload and fires after the `for` duration (e.g., 2 minutes).

### Option 3: Fix Alertmanager routing configuration

**Risk:** Medium. Routing changes affect all alerts. Always test with `amtool` before applying.

**Command:**

Edit `alertmanager.yml`:

```yaml
route:
  receiver: 'default-slack'
  group_by: ['alertname', 'severity']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - match:
        severity: critical
      receiver: 'pagerduty-critical'
    - match:
        severity: warning
      receiver: 'slack-warnings'
```

Test before applying:

```bash
amtool config routes test --config.file=alertmanager.yml severity=critical alertname=ServiceDown
```

Reload:

```bash
curl -X POST http://localhost:9093/-/reload
```

**Verify:** `amtool config routes show --config.file=alertmanager.yml` displays the expected routing tree.

**Duration:** 1-2 minutes.

### Option 4: Remove accidental silences

**Risk:** Low. Removing a silence re-enables notifications for matched alerts.

**Command:**

```bash
amtool silence query --alertmanager.url=http://localhost:9093
amtool silence expire <SILENCE_ID> --alertmanager.url=http://localhost:9093
```

Or via API:

```bash
curl -X DELETE http://localhost:9093/api/v2/silence/<SILENCE_ID>
```

**Verify:** `curl -s http://localhost:9093/api/v2/silences | jq '[.[] | select(.status.state=="active")] | length'` shows the count decreased.

**Duration:** Immediate.

### Option 5: Fix receiver credentials (Slack, PagerDuty, email)

**Risk:** Low. Updating credentials restores notification delivery without affecting alert routing.

**Command:**

Update the receiver section in `alertmanager.yml`:

```yaml
receivers:
  - name: 'pagerduty-critical'
    pagerduty_configs:
      - routing_key: '<NEW_INTEGRATION_KEY>'
        severity: '{{ .CommonLabels.severity }}'
  - name: 'default-slack'
    slack_configs:
      - api_url: '<NEW_WEBHOOK_URL>'
        channel: '#alerts'
```

```bash
curl -X POST http://localhost:9093/-/reload
```

**Verify:** Send a test alert (see Verification step 4) and confirm the notification arrives at the receiver.

**Duration:** 2-5 minutes.

## Root Cause Resolution

**If** Prometheus cannot reach Alertmanager → fix the `alerting.alertmanagers` configuration in `prometheus.yml`. For Kubernetes, verify the Alertmanager Service exists (`kubectl get svc -n monitoring`), the pod is running, and no NetworkPolicy blocks port 9093. For HA Alertmanager clusters, configure Prometheus to send to all instances (not load-balanced).

**If** the alert rule expression never evaluates to true → review the PromQL expression against actual metric data in the Prometheus query UI. Common mistakes: wrong label values (case-sensitive), missing metrics due to relabeling or scrape failures, thresholds inverted (`> 0.9` instead of `< 0.1`), or the metric does not exist for the target job.

**If** alerts stay in `pending` and never reach `firing` → the `for` duration is longer than the condition persists. Reduce `for` to match the expected condition duration. For critical alerts, use `for: 1m` or `for: 2m`. For flapping services, increase scrape frequency rather than extending `for`.

**If** Alertmanager routing does not match alert labels → the `route` tree's matchers do not match the alert's labels, and the alert falls to the default receiver. Use `amtool config routes test` with the alert's exact labels. Ensure child routes use `match` or `match_re` that correspond to labels set on the alert rule.

**If** receivers fail to deliver → update credentials. For Slack: regenerate the webhook URL in Slack app settings. For PagerDuty: verify the integration key in the service's integration tab. For email: test SMTP connectivity with `curl --url smtp://server:587 --mail-from sender@example.com --mail-rcpt receiver@example.com`. For webhooks: `curl -X POST <url> -d '{"test": true}'` to verify the endpoint is reachable.

**If** inhibition rules are too broad → narrow `source_matchers` and `target_matchers` to specific services or alert groups. Add more labels to the `equal` list to prevent cross-service inhibition. Review inhibitions with `amtool config routes show`.

## Verification

1. **Confirm alert rules evaluate correctly:**

```bash
curl -s http://localhost:9090/api/v1/rules | \
  jq '.data.groups[].rules[] | select(.type=="alerting") | {name, state, health}'
```

All rules show `health: "ok"`. Rules for known conditions show `firing`.

2. **Confirm Prometheus reaches Alertmanager with zero errors:**

```bash
curl -s http://localhost:9090/api/v1/alertmanagers | jq '.data.activeAlertmanagers | length'
curl -s 'http://localhost:9090/api/v1/query?query=rate(prometheus_notifications_errors_total[5m])' | jq '.data.result[0].value[1]'
```

Active Alertmanagers >= 1. Error rate = 0.

3. **Confirm no unexpected silences:**

```bash
curl -s http://localhost:9093/api/v2/silences | jq '[.[] | select(.status.state=="active")] | length'
```

Should be 0 (or only intentional maintenance silences).

4. **End-to-end test with a synthetic alert:**

```bash
curl -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"TestAlert","severity":"info"},"annotations":{"summary":"End-to-end alerting pipeline test"}}]'
```

Verify the notification arrives at the configured receiver. Then resolve:

```bash
curl -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"TestAlert","severity":"info"},"endsAt":"2026-03-26T00:00:00Z"}]'
```

## Prevention

- **Implement a dead-man's switch (Watchdog alert).** Configure an alert that always fires (e.g., `expr: vector(1)`). Route it to a heartbeat monitoring service (Healthchecks.io, PagerDuty heartbeat, Dead Man's Snitch). If the heartbeat stops, the alerting pipeline is broken.
- **Validate alert rules in CI with promtool.** Run `promtool check rules rules.yml` and `promtool test rules test.yml` in the CI pipeline before deploying rule changes. Write unit tests for critical alert expressions.
- **Monitor the monitoring system.** Alert on `rate(prometheus_notifications_errors_total[5m]) > 0` and `rate(prometheus_rule_evaluation_failures_total[5m]) > 0` to detect pipeline failures before they cause missed incidents.
- **Require silence policies.** All silences must have a reason, a maximum duration (e.g., 4 hours), and a responsible person. Audit active silences weekly. Automate expiry enforcement.
- **Version control Alertmanager configuration.** Store `alertmanager.yml` in Git. Review routing changes in pull requests using `amtool config routes show` output as a PR comment.
- **Test routing changes with `amtool config routes test`** before deploying. Verify that each alert severity routes to the expected receiver.
- **Configure receiver redundancy.** Add a fallback receiver (e.g., email) in case the primary receiver (Slack webhook, PagerDuty) fails. Use `webhook_configs` as a secondary path.
- **Review `group_wait`, `group_interval`, and `repeat_interval`** quarterly to match SLA response requirements. Default values may be too slow for critical alerts.

## Sources

- [Prometheus — Alertmanager Overview](https://prometheus.io/docs/alerting/latest/alertmanager/) — Grouping, inhibition, silences, and HA clustering architecture
- [Prometheus — Alertmanager Configuration](https://prometheus.io/docs/alerting/latest/configuration/) — Routing tree, receiver definitions, inhibition rules, and silence matchers
- [Prometheus — Alerting Rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/) — Alert rule syntax, `for` clause behavior, and evaluation semantics
- [Prometheus — Alerting Best Practices](https://prometheus.io/docs/practices/alerting/) — Official guidance on designing reliable and actionable alert rules
- [Troubleshooting Alertmanager: Common Issues and Debugging Techniques](https://dohost.us/index.php/2025/09/28/troubleshooting-alertmanager-common-issues-and-debugging-techniques/) — Step-by-step diagnostic procedures including amtool, log analysis, and routing debugging
- [DrDroid — Alertmanager Not Receiving Alerts](https://drdroid.io/stack-diagnosis/prometheus-alertmanager-not-receiving-alerts) — Connectivity diagnosis between Prometheus and Alertmanager
