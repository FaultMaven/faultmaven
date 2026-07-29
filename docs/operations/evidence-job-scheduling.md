# Evidence Processing Job Scheduling

**Date:** 2026-02-11
**Context:** Phase 7 - Failure Mode Handling & Retry Infrastructure
**Status:** Implementation Complete

---

## Overview

This document describes the scheduling configuration for evidence processing background jobs, including storage cleanup, retry monitoring, and metrics collection.

These jobs support the failure mode handling infrastructure documented in:
- `docs/architecture/data-processing/evidence-failure-modes.md`

---

## Job Definitions

### 1. Storage Cleanup Job

**Purpose:** Delete orphaned files >24h old with no evidence record

**Implementation:** `faultmaven/modules/agent/jobs/storage_cleanup.py`

**Execution:**
```bash
# Manual execution
python -m faultmaven.jobs.run storage_cleanup

# Dry run (preview without deletion)
python -m faultmaven.jobs.run storage_cleanup --dry-run

# Custom TTL
python -m faultmaven.jobs.run storage_cleanup --ttl-hours 48
```

**Schedule:** Daily at 2:00 AM (low-traffic period)

**Configuration:**

Cron:

```cron
0 2 * * * cd /app && python -m faultmaven.jobs.run storage_cleanup
```

Kubernetes CronJob:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: faultmaven-storage-cleanup
spec:
  schedule: "0 2 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: storage-cleanup
            image: ghcr.io/faultmaven/faultmaven:latest
            command:
              - python
              - -m
              - faultmaven.jobs.run
              - storage_cleanup
            envFrom:
              # Config (DB host, Redis host, storage backend) + the app-role
              # DATABASE_URL. CronJobs do not inherit the API Deployment's
              # envFrom patch, so both mounts are explicit.
              - configMapRef:
                  name: faultmaven-config
              - secretRef:
                  name: faultmaven-secrets
          restartPolicy: OnFailure
```

**Metrics:**

Declared in `faultmaven/infrastructure/observability/evidence_metrics.py` and emitted by the job
(canonical reference: [`docs/operations/monitoring/evidence-metrics.md`](monitoring/evidence-metrics.md)):

- `faultmaven_evidence_orphan_files_found_total` (counter)
- `faultmaven_evidence_orphan_files_deleted_total` (counter)

There is no per-file deletion-failure counter; failures are logged
(`Failed to delete orphan …`) but not currently emitted as a metric.

**Alerts:**

- Orphaned files >50: Warning (systematic processing failures) — alert on
  `increase(faultmaven_evidence_orphan_files_found_total[24h])`
- A large found/deleted gap: Warning (storage backend rejecting deletes) — compare
  `faultmaven_evidence_orphan_files_found_total` against
  `faultmaven_evidence_orphan_files_deleted_total`; there is no direct
  failure counter to alert on

**Expected Behavior:**
- Typical run: 0-5 orphaned files (transient failures)
- Normal orphaned rate: <1% of total uploads
- High orphaned rate (>10 files): Investigate LLM timeout/DB failure rates

---

### 2. Retry Queue Monitoring (Future)

**Purpose:** Monitor retry queue depth and success rates

**Implementation:** TBD - depends on job queue backend (Celery, Redis Queue, APScheduler)

**Schedule:** Every 5 minutes (monitoring only)

**Metrics to Track:**
- `evidence.retry_queue_depth` (gauge)
- `evidence.retry_queue_age_max` (gauge) - Oldest job in queue
- `evidence.retry_processing_time` (histogram)

**Alerts:**
- Queue depth >100: Warning (retries backing up)
- Oldest job >1 hour: Warning (retries stalled)

---

### 3. Metrics Aggregation (Optional)

**Purpose:** Pre-calculate metrics for dashboards (if using Prometheus)

**Implementation:** N/A - Prometheus handles this automatically

**Alternative (if not using Prometheus):**
- Calculate hourly/daily aggregates
- Store in TimeSeries database (InfluxDB, TimescaleDB)
- Schedule: Hourly

---

## Tenant Scope (Multi-Tenant / Cloud)

The CLI runner (`faultmaven.jobs.run`) enforces a declared tenant scope per job
(ADR-010 P3). Each job module declares `JOB_TENANT_SCOPE`:

| Scope | Meaning | Under `TENANT_PROVIDER=multi` |
|-------|---------|-------------------------------|
| `tenant_neutral` | No tenanted DB access (e.g. `storage_cleanup`, a sidecar-driven filesystem sweep) | Runs as-is |
| `org` | Operates on one organization's rows | Requires explicit `--organization-id`; the runner binds it to the tenant context so all DB access is RLS-scoped to that org |
| `cross_tenant` | Touches all organizations' data (e.g. `case_cleanup`, which diffs the DB case-id set against non-partitioned ChromaDB collections; `kb_seed`, which writes the org-free global KB tier served to every tenant, #770) | **Refused by default** — RLS scopes every DB transaction to the single org bound in the tenant context; a partial view would delete other tenants' data. Runs ONLY on the audited maintenance path below |

The runner also runs the same boot gates as the API lifespan: the deployment
coherence gate, and (under multi) the RLS role guard — a CronJob with a
misprovisioned RLS-exempt DB role refuses to run. The in-process scheduler
(`RUN_SCHEDULER=true`) likewise refuses to start the case-cleanup task under
multi (it runs inside the API process, which holds app-role credentials).
Single-tenant (standalone) behavior is unchanged.

### Audited maintenance path (cross-tenant jobs under multi)

A `cross_tenant` job runs under multi only when **both** of these hold:

1. The invocation passes `--cross-tenant-maintenance` — the operator's (or the
   CronJob manifest's) explicit acknowledgment.
2. The process connects as the **dedicated maintenance DB role**
   (`faultmaven_maintenance`, provisioned by `faultmaven-enterprise-infra`):
   `BYPASSRLS` + non-superuser + non-owner, SELECT-only grants plus a single
   explicit write surface — `INSERT, DELETE ON knowledge_items` for `kb_seed`
   (#770). The runner probe-verifies the role and refuses anything else —
   including the regular app role, whose RLS-scoped *partial* view is exactly
   the delete-other-tenants hazard.

#### Sourcing the maintenance DSN

> **Prerequisite — the infra#123 Secret split.** Everything below reads the DSN
> from `faultmaven-db-privileged`, a Secret created by
> `scripts/apps/bootstrap-faultmaven-secrets.sh` in `faultmaven-enterprise-infra`
> (PR #153). Until that change has landed **and** the script has been re-run
> against the cluster, that Secret does not exist: the only DB DSN present is
> `faultmaven-secrets` / `DATABASE_URL`, which is the limited app role — the one
> the runner's role probe refuses for cross-tenant work. Check with
> `kubectl -n faultmaven get secret faultmaven-db-privileged` before following
> this procedure.

The maintenance DSN lives in the **`faultmaven-db-privileged`** Secret, under the
key `MAINTENANCE_DATABASE_URL`. That Secret is deliberately mounted by *nothing*
via `envFrom` (infra#123): it also carries the owner/migrator DSN, and `envFrom`
is blanket — it would put both RLS-defeating credentials into the environment of
every container that mounted it. **It is therefore NOT present as an environment
variable in the API pod**, so a stale `DATABASE_URL="$MAINTENANCE_DATABASE_URL"`
expands to the empty string.

An empty `DATABASE_URL` fails closed, before any database engine is built: the
runner (`faultmaven/jobs/run.py`) runs the same boot gates as the API lifespan
*ahead of* container initialization. Under `DEPLOYMENT_MODE=cloud` the deployment
coherence gate names the variable directly and exits 1:

```text
CRITICAL - Refusing to run job: deployment configuration is incoherent
Error: DEPLOYMENT_MODE=cloud is incoherent with the running configuration:
  - DATABASE_URL must be PostgreSQL for cloud (got a non-postgresql URL — likely the SQLite default). SQLite is single-writer and standalone-only.
```

There is no silent SQLite fallback and no connection attempt. The diagnosis is
already top-level, so no wrapper script is needed to interpret it.

Consume the DSN with a key-scoped `secretKeyRef`, the same way the schema-migration
Job consumes `MIGRATION_DATABASE_URL`. In a Job or CronJob spec:

```yaml
      containers:
        - name: kb-seed
          image: ghcr.io/faultmaven/faultmaven:<pinned-sha>
          command: ["python", "-m", "faultmaven.jobs.run", "kb_seed", "--cross-tenant-maintenance"]
          env:
            # The BYPASSRLS maintenance role, read key-by-key. Deliberately NOT
            # `optional: true`: a missing Secret or key must fail the pod with
            # CreateContainerConfigError rather than silently leaving the job on
            # whatever DATABASE_URL the envFrom Secret supplies (the LIMITED app
            # role, whose partial RLS-scoped view is the delete-other-tenants
            # hazard the runner's role probe exists to refuse).
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: faultmaven-db-privileged
                  key: MAINTENANCE_DATABASE_URL
          envFrom:
            - configMapRef:
                name: faultmaven-config
            - secretRef:
                name: faultmaven-secrets
```

#### One-off operator run

**Where this runs:** a workstation with `kubectl` and a kubecontext for the
cluster. Every command below is `kubectl` only.

**Not from inside the API pod.** The application image is `python:3.11-slim` plus
build tooling — it carries no `kubectl` — and the API pod's ServiceAccount has no
Secret-read RBAC, so reading the DSN from in-pod is not possible either. The
maintenance credential is never in that pod by design (that is the whole point of
infra#123); the way to use it is to start a pod that mounts it.

**Do not derive the Job from the scheduled CronJob.** `kubectl create job
--from=cronjob/faultmaven-case-cleanup` looks like the shortcut, but that CronJob
takes its DSN from the `faultmaven-secrets` `envFrom` mount — the limited app role
— and its `command` carries no `--cross-tenant-maintenance`. The derived Job would
be refused by the runner's role probe. Create the Job explicitly instead:

```bash
kubectl -n faultmaven create -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  generateName: faultmaven-case-cleanup-maint-
  labels:
    app.kubernetes.io/part-of: faultmaven
    app.kubernetes.io/component: maintenance
spec:
  # A partially-completed cross-tenant sweep should be inspected, not retried.
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels:
        # part-of=faultmaven is load-bearing, not decorative: it is what the
        # NetworkPolicies admit to the PostgreSQL primary and to ChromaDB.
        app.kubernetes.io/part-of: faultmaven
        app.kubernetes.io/component: maintenance
    spec:
      restartPolicy: Never
      containers:
        - name: case-cleanup
          image: ghcr.io/faultmaven/faultmaven:<pinned-sha>
          command:
            - python
            - -m
            - faultmaven.jobs.run
            - case_cleanup
            - --cross-tenant-maintenance
            - --verbose
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: faultmaven-db-privileged
                  key: MAINTENANCE_DATABASE_URL
          envFrom:
            - configMapRef:
                name: faultmaven-config
            - secretRef:
                name: faultmaven-secrets
EOF
```

Swap `case_cleanup` for `kb_seed` to seed or refresh the platform KB pack (the
multi-tenant replacement for the single-tenant web-startup KB bootstrap, which is
skipped under multi). Follow the run with:

```bash
kubectl -n faultmaven get jobs -l app.kubernetes.io/component=maintenance
kubectl -n faultmaven logs -l app.kubernetes.io/component=maintenance --tail=-1
```

**If the maintenance role has not been provisioned yet**, the Secret has no
`MAINTENANCE_DATABASE_URL` key, the kubelet cannot build the container, and the
pod sits in `CreateContainerConfigError` without ever starting — the loud, correct
failure (this is why the `secretKeyRef` is deliberately not `optional: true`). It
takes **both** of these, in order, in `faultmaven-enterprise-infra`:

```bash
./scripts/apps/provision-maintenance-role.sh    # creates the faultmaven_maintenance role
                                                # + the maintenance-password component key
./scripts/apps/bootstrap-faultmaven-secrets.sh  # derives MAINTENANCE_DATABASE_URL from that
                                                # password into faultmaven-db-privileged
```

`provision-maintenance-role.sh` on its own does **not** populate the Secret key —
it writes `maintenance-password` into `faultmaven-postgresql-credentials`, and the
bootstrap script is what turns that into the DSN. Running only the first script
leaves the Job failing exactly as before.

Every maintenance run emits a WARNING-level `AUDIT` log line (job, arguments,
posture), so cross-tenant sweeps are always attributable in the job logs; the
dedicated role also makes them attributable in PostgreSQL. The flag is
fail-closed everywhere it does not apply: on `org`/`tenant_neutral` jobs (the
maintenance role must never run tenant-scoped work) and in single-tenant
deployments (where it indicates a manifest copied from cloud). See
`docs/operations/rls-app-role.md` in `faultmaven-enterprise-infra` for role
provisioning.

---

## Cron Configuration

### Development (Local Machine)

Add to crontab:
```bash
# Edit crontab
crontab -e

# Add job (adjust path to your installation)
0 2 * * * cd /path/to/faultmaven && /path/to/.venv/bin/python -m faultmaven.jobs.run storage_cleanup >> /var/log/faultmaven/storage_cleanup.log 2>&1
```

### Production (systemd timers)

**Service Unit:** `/etc/systemd/system/faultmaven-storage-cleanup.service`
```ini
[Unit]
Description=FaultMaven Storage Cleanup Job
After=network.target

[Service]
Type=oneshot
User=faultmaven
WorkingDirectory=/opt/faultmaven
Environment=PYTHONPATH=/opt/faultmaven
ExecStart=/opt/faultmaven/.venv/bin/python -m faultmaven.jobs.run storage_cleanup
StandardOutput=journal
StandardError=journal
SyslogIdentifier=faultmaven-storage-cleanup
```

**Timer Unit:** `/etc/systemd/system/faultmaven-storage-cleanup.timer`
```ini
[Unit]
Description=FaultMaven Storage Cleanup Timer
Requires=faultmaven-storage-cleanup.service

[Timer]
OnCalendar=daily
OnCalendar=02:00
Persistent=true

[Install]
WantedBy=timers.target
```

**Enable and Start:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable faultmaven-storage-cleanup.timer
sudo systemctl start faultmaven-storage-cleanup.timer

# Check status
sudo systemctl status faultmaven-storage-cleanup.timer
sudo systemctl list-timers faultmaven-storage-cleanup.timer
```

---

## Kubernetes CronJob Configuration

**Deployed manifest:** `kubernetes/apps/faultmaven/base/cronjobs/storage-cleanup.yaml`
in the **`faultmaven-enterprise-infra`** repository — that file is authoritative.
The block below is an annotated illustration of it; label selectors and mount
names match, but check the real manifest before editing anything in-cluster.

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: faultmaven-storage-cleanup
  namespace: faultmaven
  labels:
    app: faultmaven-storage-cleanup
    app.kubernetes.io/name: faultmaven-storage-cleanup
    app.kubernetes.io/component: cronjob
    app.kubernetes.io/part-of: faultmaven
spec:
  schedule: "0 2 * * *"
  concurrencyPolicy: Forbid  # Don't run if previous job still running
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 5
  jobTemplate:
    spec:
      backoffLimit: 2  # Retry up to 2 times on failure
      template:
        metadata:
          labels:
            app: faultmaven-storage-cleanup
            app.kubernetes.io/name: faultmaven-storage-cleanup
            app.kubernetes.io/component: cronjob
            # part-of=faultmaven is load-bearing, not decorative: it is what the
            # NetworkPolicies admit to MinIO and to the PostgreSQL primary.
            app.kubernetes.io/part-of: faultmaven
        spec:
          restartPolicy: OnFailure

          # No serviceAccountName: the job runs as the namespace `default`
          # ServiceAccount. It needs no Kubernetes API access — storage
          # credentials arrive as environment variables from the Secret below,
          # not from RBAC.

          containers:
          - name: storage-cleanup
            image: ghcr.io/faultmaven/faultmaven:1.0.0
            imagePullPolicy: IfNotPresent

            command:
              - python
              - -m
              - faultmaven.jobs.run
              - storage_cleanup

            env:
              - name: ENVIRONMENT
                value: "production"

            envFrom:
              # Everything else comes from the two application mounts, which is
              # the ONLY supported wiring — do not hand-roll `secretKeyRef`
              # entries per variable. `faultmaven-config` supplies the DB/Redis
              # hosts and the storage settings (STORAGE_BACKEND, S3_BUCKET_NAME,
              # S3_REGION, S3_ENDPOINT_URL, S3_KEY_PREFIX); `faultmaven-secrets`
              # supplies DATABASE_URL (the limited app role), REDIS_PASSWORD and
              # AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY. Both are created by
              # `scripts/apps/bootstrap-faultmaven-secrets.sh` in
              # `faultmaven-enterprise-infra`; that script is the authority on
              # the key names.
              - configMapRef:
                  name: faultmaven-config
              - secretRef:
                  name: faultmaven-secrets

            resources:
              requests:
                memory: "256Mi"
                cpu: "100m"
              limits:
                memory: "512Mi"
                cpu: "500m"
```

**Deploy:** the CronJobs are part of the `faultmaven` kustomize base and are
applied by the CD pipeline, not by hand. To apply from a workstation, run this
from a `faultmaven-enterprise-infra` checkout:

```bash
kubectl apply -k kubernetes/apps/faultmaven/overlays/staging   # or onprem / flip-rehearsal

# Check status
kubectl get cronjobs -n faultmaven
kubectl get jobs -n faultmaven

# View logs
kubectl logs -n faultmaven job/faultmaven-storage-cleanup-<timestamp>
```

---

## Docker Compose Configuration

For local development with Docker Compose:

**File to create:** `docker-compose.jobs.yml` — not shipped in this repository;
the committed compose files are `docker-compose.yml` plus the `*-build.yml`
layers. Create it yourself from the template below if you want a compose-driven
job runner.

```yaml
version: '3.8'

services:
  storage-cleanup:
    image: ghcr.io/faultmaven/faultmaven:latest
    command: python -m faultmaven.jobs.run storage_cleanup
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - REDIS_URL=${REDIS_URL}
      - STORAGE_BACKEND=filesystem
    volumes:
      - ./data:/app/data
    depends_on:
      - redis
    restart: "no"  # Run once, don't restart

# Use external scheduler (cron, Windows Task Scheduler) to trigger:
# docker-compose -f docker-compose.jobs.yml up storage-cleanup
```

**Schedule with cron:**
```bash
0 2 * * * cd /path/to/faultmaven && docker-compose -f docker-compose.jobs.yml up storage-cleanup
```

---

## Monitoring and Alerting

### Prometheus Scrape Configuration

Example `prometheus.yml` — this repository ships no Prometheus configuration; the
deployed scrape config is owned by the monitoring stack in
`faultmaven-enterprise-infra`.

```yaml
global:
  scrape_interval: 30s
  evaluation_interval: 30s

rule_files:
  - "evidence_alerts.yml"

scrape_configs:
  - job_name: 'faultmaven'
    static_configs:
      - targets: ['faultmaven:8090']
```

### Alert Rules

Alert rules are defined in the infrastructure monitoring layer.

> **Canonical alert definitions:** [`docs/operations/monitoring/evidence-metrics.md`](monitoring/evidence-metrics.md).
> Metric *definitions* live in `faultmaven/infrastructure/observability/evidence_metrics.py`
> (Prometheus `Counter`/`Histogram` objects, all `faultmaven_`-prefixed); write
> alert expressions against the names it declares. Alert *rules* are not in this
> repository (there is no `evidence_alerts.yml` here) — they live in the
> Grafana/Prometheus config maintained by the infrastructure team.

### Grafana Dashboard

No dashboard JSON is shipped in this repository — there is no
`evidence_dashboard.json` to import. Build the dashboard in Grafana against the
metric names declared in `evidence_metrics.py`, or import one exported from an
existing environment.

**Panels worth having:**
- Evidence creation success rate
- LLM timeout/error rates
- DB insert failures
- Retry attempts and successes
- Permanent failures (critical)
- Category fallback (schema drift)
- Orphaned files
- Retry success percentage

---

## Alert Destinations

### Slack Integration

**Alertmanager Configuration:**

```yaml
route:
  group_by: ['alertname', 'severity']
  group_wait: 10s
  group_interval: 5m
  repeat_interval: 3h
  receiver: 'slack-critical'

  routes:
    - match:
        severity: critical
      receiver: 'slack-critical'
      continue: true

    - match:
        severity: critical
        page: 'true'
      receiver: 'pagerduty'

    - match:
        severity: warning
      receiver: 'slack-warnings'

receivers:
  - name: 'slack-critical'
    slack_configs:
      - api_url: 'https://hooks.slack.com/services/XXX/YYY/ZZZ'
        channel: '#faultmaven-alerts'
        title: '🔥 {{ .GroupLabels.alertname }}'
        text: '{{ range .Alerts }}{{ .Annotations.description }}{{ end }}'

  - name: 'slack-warnings'
    slack_configs:
      - api_url: 'https://hooks.slack.com/services/XXX/YYY/ZZZ'
        channel: '#faultmaven-warnings'
        title: '⚠️  {{ .GroupLabels.alertname }}'
        text: '{{ range .Alerts }}{{ .Annotations.description }}{{ end }}'

  - name: 'pagerduty'
    pagerduty_configs:
      - service_key: '<pagerduty-integration-key>'
```

---

## Job Execution Logs

### Log Locations

**Local Development:**
- Console output (stdout/stderr)
- Optional: `./logs/jobs/storage_cleanup.log`

**Production (systemd):**
```bash
# View logs
sudo journalctl -u faultmaven-storage-cleanup.service

# Follow logs
sudo journalctl -u faultmaven-storage-cleanup.service -f

# Last 100 lines
sudo journalctl -u faultmaven-storage-cleanup.service -n 100
```

**Kubernetes:**
```bash
# List jobs
kubectl get jobs -n faultmaven

# View logs
kubectl logs -n faultmaven job/faultmaven-storage-cleanup-<timestamp>

# Stream logs
kubectl logs -n faultmaven job/faultmaven-storage-cleanup-<timestamp> -f
```

**Docker Compose:**
```bash
# View logs
docker-compose -f docker-compose.jobs.yml logs storage-cleanup

# Follow logs
docker-compose -f docker-compose.jobs.yml logs -f storage-cleanup
```

### Log Aggregation

**ELK Stack:**
```yaml
filebeat:
  inputs:
    - type: log
      paths:
        - /var/log/faultmaven/storage_cleanup.log
      fields:
        job: storage_cleanup
        app: faultmaven
      fields_under_root: true
```

**Loki (Kubernetes):**
```text
# Loki automatically scrapes pod logs.
# Query in Grafana Explore — `app` is the pod label set by the CronJob's
# jobTemplate (faultmaven-storage-cleanup), NOT a bare "faultmaven":
{namespace="faultmaven", app="faultmaven-storage-cleanup"}

# The case-cleanup sweep, same pattern:
{namespace="faultmaven", app="faultmaven-case-cleanup"}
```

---

## Troubleshooting

### High Orphaned File Rate

**Symptoms:**
- `increase(faultmaven_evidence_orphan_files_found_total[24h])` >50
- Alert: "High number of orphaned files in storage"

**Investigation:**
1. Review the job's own logs for `Failed to delete orphan` lines (see Loki queries above)
2. Check storage backend health (MinIO/S3 reachability, credentials, bucket policy)
3. Check database connectivity from the job pod

> LLM-timeout and DB-insert-failure rates are the usual upstream causes, but no
> Prometheus counters for them exist yet — `evidence_metrics.py` declares no such
> metrics. Investigate those via application logs until they are instrumented.

**Resolution:**
- If LLM timeouts high: Increase timeout threshold or switch provider
- If DB failures high: Check database health, connection pool
- If storage issues: Check S3 permissions, disk space

### Cleanup Job Failures

**Symptoms:**
- Job exits with error
- No orphaned files deleted despite high count

**Investigation:**
1. View job logs: `journalctl -u faultmaven-storage-cleanup.service`
2. Check storage backend permissions
3. Verify database connectivity
4. Check for disk space issues

**Common Issues:**
- S3 permissions: Ensure IAM role has `s3:ListBucket`, `s3:DeleteObject`
- Database connection: Verify `DATABASE_URL` environment variable
- Disk space: Check filesystem storage path has space

### Retry Queue Backup

**Symptoms:**
- Retry jobs not completing
- Evidence stuck in "processing" state

**Investigation:**
1. Check job queue depth (Redis): `LLEN job:retry_queue`
2. Check worker processes: `ps aux | grep faultmaven`
3. Review retry job logs for errors
4. Check LLM provider status

**Resolution:**
- Restart job workers if stalled
- Clear failed jobs from queue (after investigation)
- Scale up job workers if queue consistently high

---

## Operational Runbooks

### Daily Health Check

**Morning checklist:**
1. Check overnight storage cleanup job: `journalctl -u faultmaven-storage-cleanup.service | tail -50`
2. Verify orphaned file count reasonable (<5)
3. Check for permanent failure alerts (critical)
4. Review retry success rate (should be >50%)

### Weekly Review

**Weekly tasks:**
1. Review evidence creation trends (Grafana dashboard)
2. Check for category fallback spikes (schema drift)
3. Review retry configuration (adjust if needed)
4. Validate alert rules are firing correctly

### Incident Response

**Permanent DB Failure Alert:**
1. Page received: "Evidence DB insert failed permanently"
2. Check logs for case_id and content_ref
3. Verify database health: `psql -c "SELECT 1"`
4. Review LLM result in logs (may need manual evidence creation)
5. If database recovered, re-run retry job with preserved LLM result

**High LLM Timeout Rate:**
1. Check LLM provider status page
2. Switch to fallback provider if needed
3. Increase timeout threshold temporarily
4. Review recent LLM API changes

---

## Configuration Summary

| Job | Schedule | Executor | Log Location | Alerts |
|-----|----------|----------|--------------|--------|
| storage_cleanup | Daily 2 AM | cron/systemd/k8s | journalctl / kubectl logs | orphaned_files >50 |
| retry_monitoring | Every 5min | (future) | journalctl / kubectl logs | queue_depth >100 |

---

**Implementation Status:** Complete
**Next Steps:** Wait for Phase 4 (evidence classification) to integrate error handling in `faultmaven/core/investigation/milestone_engine.py`
