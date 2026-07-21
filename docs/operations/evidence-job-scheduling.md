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
```yaml
# Cron
0 2 * * * cd /app && python -m faultmaven.jobs.run storage_cleanup

# Kubernetes CronJob
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
            env:
              - name: DATABASE_URL
                valueFrom:
                  secretKeyRef:
                    name: faultmaven-secrets
                    key: database-url
          restartPolicy: OnFailure
```

**Metrics:**
- `evidence.orphaned_files_found` (gauge)
- `evidence.orphaned_files_cleaned` (gauge)
- `evidence.orphaned_files_failed` (counter)

**Alerts:**
- Orphaned files >50: Warning (systematic processing failures)
- Deletion failures >5/day: Warning (storage backend issues)

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
| `cross_tenant` | Needs all organizations' rows (e.g. `case_cleanup`, which diffs the DB case-id set against non-partitioned ChromaDB collections) | **Refused** — RLS scopes every DB transaction to the single org bound in the tenant context; a partial view would delete other tenants' data. Do not schedule in cloud (see faultmaven#629) |

The runner also runs the same boot gates as the API lifespan: the deployment
coherence gate, and (under multi) the RLS role guard — a CronJob with a
misprovisioned RLS-exempt DB role refuses to run. The in-process scheduler
(`RUN_SCHEDULER=true`) likewise refuses to start the case-cleanup task under
multi. Single-tenant (standalone) behavior is unchanged.

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

**File:** `k8s/cronjobs/storage-cleanup.yaml`

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: faultmaven-storage-cleanup
  namespace: faultmaven
  labels:
    app: faultmaven
    component: jobs
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
            app: faultmaven
            component: storage-cleanup
        spec:
          restartPolicy: OnFailure

          # Service account with storage permissions
          serviceAccountName: faultmaven-jobs

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
              - name: DATABASE_URL
                valueFrom:
                  secretKeyRef:
                    name: faultmaven-secrets
                    key: database-url

              - name: REDIS_URL
                valueFrom:
                  secretKeyRef:
                    name: faultmaven-secrets
                    key: redis-url

              - name: STORAGE_BACKEND
                value: "s3"

              - name: S3_BUCKET
                value: "faultmaven-evidence"

              - name: AWS_REGION
                value: "us-east-1"

            resources:
              requests:
                memory: "256Mi"
                cpu: "100m"
              limits:
                memory: "512Mi"
                cpu: "500m"

            volumeMounts:
              - name: aws-credentials
                mountPath: /root/.aws
                readOnly: true

          volumes:
            - name: aws-credentials
              secret:
                secretName: faultmaven-aws-credentials
```

**Deploy:**
```bash
kubectl apply -f k8s/cronjobs/storage-cleanup.yaml

# Check status
kubectl get cronjobs -n faultmaven
kubectl get jobs -n faultmaven

# View logs
kubectl logs -n faultmaven job/faultmaven-storage-cleanup-<timestamp>
```

---

## Docker Compose Configuration

For local development with Docker Compose:

**File:** `docker-compose.jobs.yml`

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

**File:** `prometheus/prometheus.yml`

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

> **Note**: `faultmaven/infrastructure/observability/evidence_metrics.py` was removed during codebase cleanup. Evidence metrics are now tracked via the general observability stack (`infrastructure/observability/`).

**Copy to Prometheus:**
```bash
cp evidence_alerts.yml /etc/prometheus/rules/
sudo systemctl reload prometheus
```

### Grafana Dashboard

**Import Dashboard:**
1. Login to Grafana
2. Navigate to Dashboards → Import
3. Upload `evidence_dashboard.json`
4. Select Prometheus data source
5. Click Import

**Dashboard Panels:**
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
```yaml
# Loki automatically scrapes pod logs
# Query in Grafana Explore:
{namespace="faultmaven", app="faultmaven", component="storage-cleanup"}
```

---

## Troubleshooting

### High Orphaned File Rate

**Symptoms:**
- `evidence.orphaned_files_found` >50
- Alert: "High number of orphaned files in storage"

**Investigation:**
1. Check LLM timeout rate: `rate(evidence_llm_timeouts[1h])`
2. Check DB insert failure rate: `rate(evidence_db_insert_failures[1h])`
3. Review retry job logs for failures
4. Check storage backend health (S3/filesystem)

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
**Next Steps:** Wait for Phase 4 (evidence classification) to integrate error handling in milestone_engine.py
