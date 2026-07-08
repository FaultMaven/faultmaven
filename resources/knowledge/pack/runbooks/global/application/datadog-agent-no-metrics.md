---
id: "datadog-agent-no-metrics"
title: "Datadog Agent Running but No Metrics Reaching Datadog"
domain: application
service: datadog
symptom_class: [service_unavailable, data_loss]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [datadog-agent, no-metrics, invalid-api-key, dd-site, forwarder-dropped, missing-tags]
difficulty: intermediate
---

## Symptom Recognition

- Host is silent in Datadog: no data in **Metrics Explorer** for `system.cpu.*` / `system.mem.*`, host absent from the **Infrastructure List** or shown grey/`???`.
- `datadog-agent status` Forwarder section shows `Errors` incrementing and/or `DroppedOnInput` / `Dropped` greater than 0.
- Agent log (`/var/log/datadog/agent.log`) contains:
  - `API Key invalid, dropping transaction`
  - `error while sending transaction, rescheduling it`
  - `the forwarder dropped transactions`
- API Keys subsection of status: `API key ending with xxxxx: API Key invalid`.
- Integration is under "Running Checks" but its metrics never appear, or the check shows `[ERROR]` with a Python traceback.
- Metrics arrive but are missing expected `env`/`service`/`host`/container tags, so dashboards and monitors scoped by tag are empty.

## Applicability

- Datadog Agent v6 / v7 (host install, Docker, or Kubernetes/containerized).
- Access: shell on the host (or `kubectl exec` / `docker exec` into the Agent container) and permission to run `datadog-agent` and restart the service (`root` or `sudo`).
- Ability to edit `/etc/datadog-agent/datadog.yaml` and integration files under `/etc/datadog-agent/conf.d/`, or set Agent environment variables (`DD_API_KEY`, `DD_SITE`, `DD_TAGS`).
- Outbound HTTPS (443) egress from the host to the Datadog intake for the configured site.

## Diagnostic Steps

### Step 1: Show overall Agent status and the Forwarder section

```bash
sudo datadog-agent status
# Container: docker exec -it datadog-agent agent status
# Kubernetes: kubectl exec -it <datadog-agent-pod> -- agent status
```

Expected output: a `Forwarder` block listing `Transactions` (`Success`, `Errors`, `Dropped`, `DroppedOnInput`, `Retried`, `RetryQueueSize`) and an `API Keys` block showing each key's validity.

### Step 2: Check the configured site and API key validity

```bash
sudo datadog-agent config | grep -E "^(site|dd_url|api_key):"
sudo datadog-agent status | grep -A6 "API Keys"
```

Expected output: `site:` printing your org's site (default `datadoghq.com`) and an API Keys line such as `API key ending with xxxxx: API Key valid`.

### Step 3: Run the live connectivity test to the intake

```bash
sudo datadog-agent diagnose
sudo datadog-agent diagnose datadog-connectivity
```

Expected output: per-endpoint `PASS` lines (for example `Connectivity to https://api.<site> ... PASS`); failures print the endpoint plus the underlying network or TLS error.

### Step 4: Inspect a specific integration check for runtime errors

```bash
sudo datadog-agent configcheck
sudo datadog-agent check <CHECK_NAME>
```

Expected output: `configcheck` lists each loaded instance; `check` prints collected `Metrics`, an `Instance ID ... [OK]` line, and emitted series — or a Python traceback / `[ERROR]` if the check fails.

### Step 5: Inspect host and container tags attached to metrics

```bash
sudo datadog-agent status | grep -A20 "Hostname"
sudo datadog-agent tagger-list
```

Expected output: a `Host tags` list and, per container, the resolved tag set (`env`, `service`, `image_name`, custom labels). Missing expected tags appear as an empty or short list.

## Causes

### Cause A: Invalid or wrong-org API key
**Statement:** The `api_key` in `datadog.yaml` (or `DD_API_KEY`) is malformed, revoked, or belongs to a different Datadog organization, so the intake rejects every forwarder transaction.
**Chain:**
- root: API key is invalid or belongs to the wrong org
- s1: intake returns 403 and the forwarder marks the transaction as failed
- s2: forwarder Errors increment and payloads are dropped after retries
- D: no metrics reach Datadog
**Indicators:**
- root: [Step 2] API Keys section prints `API Key invalid` for the configured key
- s1: [Step 1] agent.log / status shows `API Key invalid, dropping transaction`
- s2: [Step 1] Forwarder `Errors` is greater than 0
- D: [Symptom] host absent from Infrastructure List
**Interventions:**
- **remediation** (root): replace the key with a valid one from the correct org's *Organization Settings > API Keys*, then restart.

  ```bash
  sudo sed -i 's/^api_key:.*/api_key: <VALID_API_KEY>/' /etc/datadog-agent/datadog.yaml
  sudo systemctl restart datadog-agent
  ```

  **Verification:** re-run Step 2; `API Keys` shows `API Key valid` and Step 1 Forwarder `Errors` stops climbing.

### Cause B: Wrong Datadog site (DD_SITE)
**Statement:** The `site` / `DD_SITE` value does not match the org's actual Datadog site (e.g. EU org left on the default `datadoghq.com`), so the Agent ships to an intake where its key is unknown.
**Chain:**
- root: configured site does not match the org's real site
- s1: forwarder targets the wrong intake URL whose org rejects the key
- s2: transactions fail authentication and are dropped
- D: no metrics reach Datadog
**Indicators:**
- root: [Step 2] `site:` value differs from the org's site (e.g. `datadoghq.com` for an EU `datadoghq.eu` org)
- s1: [Step 3] connectivity diagnose targets `api.<wrong-site>` and the key fails there
- D: [Symptom] no data in Metrics Explorer
**Interventions:**
- **remediation** (root): set the correct site and restart.

  ```bash
  sudo sed -i 's/^# *site:.*/site: datadoghq.eu/' /etc/datadog-agent/datadog.yaml
  sudo systemctl restart datadog-agent
  ```

  **Verification:** re-run Step 2 (`site:` matches org) and Step 3 (connectivity `PASS`); metrics appear within a few minutes.

### Cause C: Network egress to intake blocked (forwarder dropping transactions)
**Statement:** A firewall, proxy, or DNS failure prevents outbound HTTPS to the Datadog intake, so forwarder workers stay busy and the forwarder drops transactions.
**Chain:**
- root: outbound 443 to the Datadog intake is blocked or unreachable
- s1: forwarder workers block waiting on connections and the retry queue fills
- s2: forwarder logs `the forwarder dropped transactions` and DroppedOnInput rises
- D: no metrics reach Datadog
**Indicators:**
- root: [Step 3] connectivity test prints a `FAIL` / connection-refused / timeout for the intake endpoint
- s1: [Step 1] Forwarder `RetryQueueSize` greater than 0 and `Retried` climbing
- s2: [Step 1] status/log contains `the forwarder dropped transactions`
- D: [Symptom] host shown grey in Infrastructure List
**Interventions:**
- **remediation** (root): open egress / configure the Agent proxy so the intake is reachable, then restart.

  ```bash
  curl -v "https://api.$(datadog-agent config | awk '/^site:/{print $2}')/api/v1/validate" -H "DD-API-KEY: $DD_API_KEY"
  # If a proxy is required, set proxy.https in datadog.yaml, then:
  sudo systemctl restart datadog-agent
  ```

  **Verification:** re-run Step 3 (connectivity `PASS`) and Step 1 (`Dropped`/`RetryQueueSize` return to 0).
- **mitigation** (s1): raise the retry queue to buffer metrics while the network is fixed.

  ```bash
  sudo sed -i 's/^# *forwarder_retry_queue_payloads_max_size:.*/forwarder_retry_queue_payloads_max_size: 50000000/' /etc/datadog-agent/datadog.yaml
  sudo systemctl restart datadog-agent
  ```

  **Risk:** higher Agent memory use; buffered points are still lost if egress is not restored before the queue overflows. **Duration:** until egress is restored. **Verification:** Step 1 shows `DroppedOnInput` holding at 0 while `RetryQueueSize` drains.

### Cause D: Integration check failing or misconfigured
**Statement:** A specific integration's instance config is wrong (bad endpoint, missing credential, unreachable target), so that check errors and emits no metrics even though the Agent and forwarder are healthy.
**Chain:**
- root: the integration instance config is invalid or its target is unreachable
- s1: the check raises an exception and is marked `[ERROR]`
- s2: the check emits 0 metric samples for that integration
- D: that integration's metrics never appear in Datadog
**Indicators:**
- root: [Step 4] `datadog-agent check <CHECK_NAME>` prints a Python traceback or `[ERROR]`
- s1: [Step 1] status `Running Checks` shows the check with `Errors: 1` / a warning
- s2: [Step 4] check output reports `Metrics: 0` / no series collected
- D: [Symptom] integration's metrics missing while system metrics still report
**Interventions:**
- **remediation** (root): fix the instance YAML under `conf.d/<CHECK_NAME>.d/conf.yaml` (endpoint, credentials, port), then reload.

  ```bash
  sudoedit /etc/datadog-agent/conf.d/<CHECK_NAME>.d/conf.yaml
  sudo systemctl restart datadog-agent
  ```

  **Verification:** re-run Step 4; instance shows `[OK]` with `Metrics` greater than 0 and the metrics appear in Metrics Explorer.

### Cause E: Tagging gap — host/container tags not applied
**Statement:** Required tags are not configured (`DD_TAGS`/`host_tags` unset, or container/Kubernetes label-to-tag mapping like `DD_CONTAINER_LABELS_AS_TAGS` not defined), so metrics arrive untagged and tag-scoped dashboards and monitors read empty.
**Chain:**
- root: host or container tag configuration is missing/incomplete
- s1: the Agent tagger resolves an empty or partial tag set for the host/container
- s2: metrics are emitted without `env`/`service`/custom tags
- D: tag-scoped dashboards and monitors show no data
**Indicators:**
- root: [Step 2] `datadog.yaml` has no `tags:`/`DD_TAGS` and no `*_labels_as_tags` mapping
- s1: [Step 5] `tagger-list` / `Host tags` shows an empty or short tag set for the entity
- D: [Symptom] monitors/dashboards filtered by `env`/`service` return no series
**Interventions:**
- **remediation** (root): define host tags and, for containers, the label/env-to-tag mapping, then restart.

  ```bash
  # Host tags (whitespace-separated):
  export DD_TAGS="env:prod team:payments"
  # Container label mapping (Docker/Kubernetes):
  export DD_CONTAINER_LABELS_AS_TAGS='{"com.example.team":"team","com.example.env":"env"}'
  sudo systemctl restart datadog-agent
  ```

  **Verification:** re-run Step 5; `tagger-list` / `Host tags` now lists the expected tags and tag-scoped dashboards populate.

### Cause Z: Unidentified
**Statement:** The Agent is running and reachable, but the cause of missing metrics is not isolated by Steps 1–5.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic flare and escalate to the SME / Datadog support.

  ```bash
  sudo datadog-agent flare <CASE_ID>
  sudo datadog-agent status > /tmp/dd-status.txt
  sudo datadog-agent diagnose >> /tmp/dd-status.txt
  ```

  **Risk:** none beyond uploading sanitized config (flare strips API keys, passwords, and secrets). **Duration:** until SME review. **Verification:** flare archive uploaded and SME engaged with the status/diagnose snapshot.

## Prevention

- Provision the Agent via config management with `api_key` and `site` templated from a validated secret store; never rely on the default site for non-US orgs.
- Add a synthetic/heartbeat monitor on `datadog.agent.up` or metric staleness per host so a silent Agent pages within minutes instead of going unnoticed.
- Bake Unified Service Tagging (`DD_ENV`, `DD_SERVICE`, `DD_VERSION`) plus `DD_TAGS` into the base image / Helm values so tags are never optional.
- Allowlist outbound 443 to the Datadog intake for the configured site at the firewall and validate egress in CI for the deployment network.
- Alert on Agent forwarder health: monitor for `datadog.agent.transactions.dropped` / rising `Errors` to catch key, site, or network regressions early.

## Sources

- [Agent Troubleshooting](https://docs.datadoghq.com/agent/troubleshooting/) — top-level no-metrics flow: confirm API key matches the platform, check Metrics Explorer, status command per environment.
- [Agent Status Page](https://docs.datadoghq.com/agent/configuration/agent-status-page/) — exact Forwarder Transactions fields (`Success`, `Errors`, `Dropped`, `DroppedOnInput`, `Retried`, `RetryQueueSize`) and API Keys validity section.
- [Agent Site Issues](https://docs.datadoghq.com/agent/troubleshooting/site/) — verifying `site`/`DD_SITE` matches the org; default `datadoghq.com`.
- [Network Traffic](https://docs.datadoghq.com/agent/configuration/network/) — "the forwarder dropped transactions" cause (busy workers / network), `forwarder_num_workers`, `forwarder_timeout`, `forwarder_retry_queue_payloads_max_size`.
- [Agent Commands](https://docs.datadoghq.com/agent/configuration/agent-commands/) — `status`, `configcheck`, `config`, `diagnose`, `flare` sub-command syntax and platform variants.
- [Troubleshoot an Agent Check](https://docs.datadoghq.com/agent/troubleshooting/agent_check_status/) — `datadog-agent check <CHECK_NAME>`, `--check-rate`, interpreting `[OK]`/`[ERROR]` and metric output.
- [Getting Integrations Working](https://docs.datadoghq.com/agent/troubleshooting/integrations/) — integration under Running Checks but metrics not visible; confirm no errors/warnings.
- [Agent Flare](https://docs.datadoghq.com/agent/troubleshooting/send_a_flare/) — `datadog-agent flare <CASE_ID>` gathers configs/logs and strips secrets.
- [Assigning Tags](https://docs.datadoghq.com/getting_started/tagging/assigning_tags/) — `DD_TAGS` whitespace-separated host tags.
- [Docker Tag Extraction](https://docs.datadoghq.com/containers/docker/tag/) — `DD_CONTAINER_LABELS_AS_TAGS` (replaces `DD_DOCKER_LABELS_AS_TAGS`) label-to-tag mapping.
