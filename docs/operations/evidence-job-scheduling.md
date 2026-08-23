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
# Manual execution — settings decide dry-run and TTL
python -m faultmaven.jobs.run storage_cleanup

# With debug logging (what the deployed CronJob passes)
python -m faultmaven.jobs.run storage_cleanup --verbose

# Per-invocation overrides (this run only)
python -m faultmaven.jobs.run storage_cleanup --dry-run
python -m faultmaven.jobs.run storage_cleanup --no-dry-run
python -m faultmaven.jobs.run storage_cleanup --ttl-hours 72
```

The runner's flags are the positional job name plus `--list`, `--verbose`/`-v`,
`--organization-id` and `--cross-tenant-maintenance`, which apply to whatever
job is named, plus two that belong to **this job only**: `--dry-run` /
`--no-dry-run` and `--ttl-hours` (`faultmaven/jobs/run.py`, #923). Passing
either to another job is refused rather than silently ignored. The same two
behaviours are configured deployment-wide by environment variable
(`faultmaven/config/settings.py`, `EvidenceStorageSettings`):

| Variable | Default | Effect | Per-run override |
|----------|---------|--------|------------------|
| `ORPHAN_CLEANUP_ENABLED` | `false` | Must be `true` before the job will delete anything | none — deliberately |
| `ORPHAN_CLEANUP_DRY_RUN` | `true` | Logs `[DRY RUN] would delete …` instead of deleting | `--dry-run` / `--no-dry-run` |
| `ORPHAN_FILE_TTL_HOURS` | `24` (min 1, max 720) | Age threshold; younger files are never deleted | `--ttl-hours` (same range; out-of-range exits 2) |

**Omitting a flag is not the same as passing the setting's current value.** An
omitted flag means "defer to settings", so the deployed `storage_cleanup
--verbose` behaves exactly as it did before these flags existed.

**`--no-dry-run` is a lever, not an enabler.** The gate in `run()` is
`ORPHAN_CLEANUP_ENABLED=true` **or** an effective dry-run (the flag if given,
else `ORPHAN_CLEANUP_DRY_RUN`); `--no-dry-run`
satisfies neither while cleanup is disabled, so the run returns
`status="skipped"` and touches nothing. Enabling reclamation is a config
change, and no CLI invocation substitutes for it.

**The M1 canary protocol needs no flag, because dry-run is the default
posture.** With both defaults in place the job enumerates and logs only. Run it
that way, read the `[DRY RUN] would delete` lines, then set
`ORPHAN_CLEANUP_ENABLED=true` and `ORPHAN_CLEANUP_DRY_RUN=false`. `--dry-run`
covers the case the defaults do not: forcing a single dry-run pass on a
deployment that has already gone live, without editing the CronJob's
environment for that run.

> **"Clean for 48h" is not the criterion — `found ≥ 1` is.** A sweep that
> reports `found=0` every night has not rehearsed deletion; it has only shown
> that nothing errored. On-prem ran three consecutive nights at
> `scanned=119, found=0, deleted=0, errors=0`, which satisfies the letter of a
> 48-hour clean run while the branch that decides *what to delete* never
> executed. Before flipping `ORPHAN_CLEANUP_DRY_RUN=false`, seed one known
> orphan (store a file, leave it unlinked, age its sidecar past the TTL) and
> confirm a dry run reports `found=1` against an object you can identify. That
> watches the selection path while deletion is still inert.

**Schedule:** Daily at 3:00 AM UTC. Deliberately **after** case-cleanup at
2:00 AM: a case deleted by that sweep has had its files unlinked by the time
this one runs, so the two do not race over the same evidence.

**Configuration:**

Cron:

```cron
0 3 * * * cd /app && python -m faultmaven.jobs.run storage_cleanup
```

Kubernetes CronJob: see [Kubernetes CronJob Configuration](#kubernetes-cronjob-configuration)
below — that section carries the single annotated illustration of the deployed
manifest. It is not repeated here, so there is only one copy to keep true.

**Metrics:**

Declared in `faultmaven/infrastructure/observability/evidence_metrics.py` and emitted by the job
(canonical reference: [`docs/operations/monitoring/evidence-metrics.md`](monitoring/evidence-metrics.md)):

- `faultmaven_evidence_orphan_files_found_total` (counter)
- `faultmaven_evidence_orphan_files_deleted_total` (counter)

There is no per-file deletion-failure counter; failures are logged
(`Failed to delete orphan …`) but not currently emitted as a metric.

> **⚠ These two counters are not scrapable from a CronJob run.** They are
> incremented inside the short-lived `python -m faultmaven.jobs.run` process,
> which starts no HTTP exporter and pushes to no Pushgateway. The Prometheus
> `/metrics` endpoint is mounted by `faultmaven/main.py` on the FastAPI app
> only, and only when `METRICS_EXPORTER=prometheus_http` — the CronJob never
> builds that app. So the counters rise and die with the pod, and any alert
> expression over them **cannot fire from the scheduled sweep**. Detect orphan
> problems from the job's logs instead (below). Making the counters alertable
> would take either a Pushgateway (`prometheus_pushgateway_url` already exists
> in settings but nothing in the jobs path pushes to it) or moving the sweep
> into a scraped long-lived process.

**Detection that does work (log-based):**

Every run ends with one summary line at INFO, whatever the outcome:

```text
Storage cleanup DRY RUN — scanned=N, found=N, deleted=N, skipped_linked=N,
skipped_within_ttl=N, skipped_no_sidecar=N, stray_sidecars=N, errors=N
```

Alert on these lines via the log pipeline (Loki queries below):

| Signal | Level | Meaning |
|--------|-------|---------|
| `found=` high in the summary line | INFO | Systematic link/store failure upstream |
| `Failed to delete orphan …` | ERROR | Storage backend rejecting deletes |
| `Unreadable sidecar for … — skipping` | WARNING | Corrupt or unreachable sidecar |
| `sidecar(s) have no corresponding file` | WARNING | Stray sidecars nothing will ever sweep |
| No summary line at all for a scheduled run | — | Job never reached the sweep (see boot gates) |

[`docs/operations/monitoring/evidence-metrics.md`](monitoring/evidence-metrics.md)
remains the canonical home for the metric and alert *definitions*; its
`evidence_orphan_file_rate_high` rule is subject to the same scrape gap
described above, so treat it as the intended rule rather than a live one.

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

> **Prerequisite — the `MAINTENANCE_DATABASE_URL` *key*, not the Secret.** The
> infra#123 split has landed (infra PR #153), and
> `scripts/apps/bootstrap-faultmaven-secrets.sh` in `faultmaven-enterprise-infra`
> now creates `faultmaven-db-privileged` **unconditionally** — it always carries
> `MIGRATION_DATABASE_URL`. What is conditional is the maintenance key: the
> script adds `MAINTENANCE_DATABASE_URL` only when
> `faultmaven-postgresql-credentials` already holds a `maintenance-password`,
> which `scripts/apps/provision-maintenance-role.sh` is what writes. So the
> Secret existing proves nothing; check for the key:
>
> ```bash
> kubectl -n faultmaven get secret faultmaven-db-privileged \
>   -o jsonpath='{.data.MAINTENANCE_DATABASE_URL}'
> ```
>
> Empty output means the maintenance role has not been provisioned. The only
> DB DSN then usable is `faultmaven-secrets` / `DATABASE_URL`, the limited app
> role — the one the runner's role probe refuses for cross-tenant work. See
> [If the maintenance role has not been provisioned yet](#one-off-operator-run)
> below for the two scripts, in order.

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
            # env[0]/env[1] = PROFILE/ENVIRONMENT, in that order, matching every
            # deployed CronJob (base/cronjobs/*.yaml). Neither key is in
            # faultmaven-config, so envFrom does not supply them.
            - name: PROFILE
              value: "enterprise"
            - name: ENVIRONMENT
              value: "production"

            # Storage-type selectors, also absent from faultmaven-config
            # (infra#149). Under DEPLOYMENT_MODE=cloud the coherence gate
            # refuses to run without SESSION_STORAGE_TYPE=redis.
            - name: SESSION_STORAGE_TYPE
              value: "redis"
            - name: VECTOR_STORAGE_TYPE
              value: "chromadb"
            - name: CASE_STORAGE_TYPE
              value: "postgres_hybrid"

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
                optional: true
```

> **⛔ A `kb_seed` Job cannot reach ChromaDB today.** `allow-chromadb-ingress`
> (`kubernetes/platform/network-policies/faultmaven.yaml`) admits exactly two
> pod identities on :8000 — `app.kubernetes.io/name: faultmaven-api` and
> `app.kubernetes.io/name: faultmaven-case-cleanup` — and the name-scoping is
> deliberate (GHSA-f4j7-r4q5-qw2c blast-radius minimisation, infra#138). A Job
> created from the fragment above carries neither label and is firewalled from
> the vector store it exists to write. Under `TENANT_PROVIDER=multi` the
> web-startup KB bootstrap is skipped, so this is the *only* global-KB seeding
> path — and it has no admitted identity. **The NetworkPolicy must be amended
> first**: infra#150 tracks it and proposes a third `podSelector` for
> `app.kubernetes.io/name: faultmaven-kb-seed`, which the Job would then set.
> Do not borrow `faultmaven-case-cleanup`'s name label to get through — that
> label means a different workload, and the deliberate narrowness is the point.

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
be refused by the runner's role probe. Create the Job explicitly instead.

**Read this before you run it — two labels, two different jobs.** `part-of:
faultmaven` is what `allow-postgresql-primary-ingress` and
`allow-minio-ingress` admit, so it *is* load-bearing for the database and object
store. It is **not** what admits anything to ChromaDB:
`allow-chromadb-ingress` is name-scoped to `app.kubernetes.io/name:
faultmaven-api` or `faultmaven-case-cleanup` and nothing else, on purpose
(GHSA-f4j7-r4q5-qw2c, infra#138). This matters because `case_cleanup` diffs the
DB case-id set against ChromaDB collections and `kb_seed` writes chunks there —
a Job that cannot reach the vector store cannot do either job.

- **`case_cleanup`:** set `app.kubernetes.io/name: faultmaven-case-cleanup` on
  the **pod template** (done below). That is the identity the policy already
  admits for exactly this workload — same image, same code path, same ChromaDB
  operations as the scheduled CronJob — so net exposure is unchanged. Without
  it the sweep reads Postgres fine and is firewalled from the collections it is
  supposed to reconcile: a half-completed cross-tenant sweep.
- **`kb_seed`:** there is **no** admitted identity, and borrowing case-cleanup's
  name label would misrepresent a different workload. The NetworkPolicy has to
  be amended first — infra#150 tracks it and proposes a dedicated
  `app.kubernetes.io/name: faultmaven-kb-seed` source.

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
        # part-of=faultmaven is what allow-postgresql-primary-ingress and
        # allow-minio-ingress admit.
        app.kubernetes.io/part-of: faultmaven
        # name=faultmaven-case-cleanup is what allow-chromadb-ingress admits.
        # part-of does NOT reach ChromaDB — that policy is name-scoped. Drop
        # this label and the sweep silently cannot see the collections it
        # diffs against. Correct ONLY for case_cleanup; see the note above.
        app.kubernetes.io/name: faultmaven-case-cleanup
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
            # env[0]/env[1] = PROFILE/ENVIRONMENT, and the three storage-type
            # selectors — copied from base/cronjobs/case-cleanup.yaml. None of
            # these five keys is in faultmaven-config, so envFrom does not
            # supply them (infra#149). Without them the container initializes
            # against the wrong backends, and under DEPLOYMENT_MODE=cloud the
            # coherence gate refuses to start at all. Do not rely on the
            # runner's generic warning path to catch that: it would let a
            # CROSS-TENANT sweep proceed against a partially-initialized
            # container.
            - name: PROFILE
              value: "enterprise"
            - name: ENVIRONMENT
              value: "production"
            - name: SESSION_STORAGE_TYPE
              value: "redis"
            - name: VECTOR_STORAGE_TYPE
              value: "chromadb"
            - name: CASE_STORAGE_TYPE
              value: "postgres_hybrid"
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
                optional: true
          # Same sizing as the scheduled CronJob (base/cronjobs/case-cleanup.yaml):
          # the jobs bootstrap loads the BGE-M3 model (~1.3Gi) during DI init.
          # Declaring no requests would make this pod BestEffort — first to be
          # evicted — and with backoffLimit: 0 an eviction mid-sweep leaves a
          # PARTIAL cross-tenant delete that is never retried.
          resources:
            requests:
              memory: "1Gi"
              cpu: "250m"
            limits:
              memory: "2Gi"
              cpu: "1000m"
EOF
```

To seed or refresh the platform KB pack instead (the multi-tenant replacement
for the single-tenant web-startup KB bootstrap, which is skipped under multi),
swap `case_cleanup` for `kb_seed` — but read the ChromaDB note above first: that
variant is blocked by `allow-chromadb-ingress` until infra#150 lands. Follow the
run with:

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

# Add job (adjust path to your installation). 03:00 matches the deployed
# CronJob's schedule, which sits after case-cleanup's 02:00 sweep.
0 3 * * * cd /path/to/faultmaven && /path/to/.venv/bin/python -m faultmaven.jobs.run storage_cleanup >> /var/log/faultmaven/storage_cleanup.log 2>&1
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
# 03:00 to match the deployed CronJob (after case-cleanup at 02:00)
OnCalendar=03:00
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
  # 03:00 UTC, deliberately AFTER case-cleanup's 02:00 sweep: a case deleted
  # there is already unlinked when this runs, so the two do not race.
  schedule: "0 3 * * *"
  concurrencyPolicy: Forbid  # Don't run if previous job still running
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    metadata:
      labels:
        app: faultmaven-storage-cleanup
        app.kubernetes.io/name: faultmaven-storage-cleanup
        app.kubernetes.io/component: cronjob
        app.kubernetes.io/part-of: faultmaven
    spec:
      backoffLimit: 2  # Retry up to 2 times on failure
      activeDeadlineSeconds: 1800  # 30 minute timeout
      template:
        metadata:
          labels:
            app: faultmaven-storage-cleanup
            app.kubernetes.io/name: faultmaven-storage-cleanup
            app.kubernetes.io/component: cronjob
            # part-of=faultmaven is load-bearing, not decorative: it is what
            # allow-minio-ingress and allow-postgresql-primary-ingress admit.
            # It does NOT reach ChromaDB — that policy is name-scoped — but
            # this sweep has no ChromaDB dependency, so that is fine here.
            app.kubernetes.io/part-of: faultmaven
        spec:
          restartPolicy: OnFailure

          # No serviceAccountName: the job runs as the namespace `default`
          # ServiceAccount. It needs no Kubernetes API access — storage
          # credentials arrive as environment variables from the Secret below,
          # not from RBAC.

          containers:
          - name: storage-cleanup
            # The base pins `latest` + Always; the overlays repin the tag to
            # `sha-<commit>` via kustomize `images:`, so a deployed CronJob
            # shows a digest-pinned tag, not `latest`.
            image: ghcr.io/faultmaven/faultmaven:latest
            imagePullPolicy: Always

            command:
              - python
              - -m
              - faultmaven.jobs.run
              - storage_cleanup
              - --verbose

            env:
              # ORDER IS LOAD-BEARING: env[0]=PROFILE, env[1]=ENVIRONMENT. The
              # onprem and staging overlays patch
              # /spec/jobTemplate/spec/template/spec/containers/0/env/1/value
              # by JSON-pointer INDEX for every part-of=faultmaven CronJob.
              # Reorder or drop an entry and `kubectl apply -k overlays/onprem`
              # either fails or patches the wrong variable.
              - name: PROFILE
                value: "enterprise"
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
                  optional: true

            # Sized for the jobs bootstrap, which currently loads the BGE-M3
            # model (~1.3Gi) during DI init (infra#131). Anything smaller is
            # OOMKilled before the sweep starts.
            resources:
              requests:
                memory: "1Gi"
                cpu: "250m"
              limits:
                memory: "2Gi"
                cpu: "1000m"
```

> **⚠ Known gap in this manifest (infra#149).** Unlike its sibling
> `kubernetes/apps/faultmaven/base/cronjobs/case-cleanup.yaml` and the API
> Deployment, `storage-cleanup.yaml` does **not** set
> `SESSION_STORAGE_TYPE`, `VECTOR_STORAGE_TYPE` or `CASE_STORAGE_TYPE`, and none
> of the three is in `faultmaven-config`. Standalone is unaffected, but under
> `DEPLOYMENT_MODE=cloud` the deployment-coherence gate refuses the run
> ("Cloud requires real Redis sessions"), so scheduled storage cleanup would
> stop happening on every invocation. The illustration above matches the file as
> deployed; do not "fix" it here.

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
0 3 * * * cd /path/to/faultmaven && docker-compose -f docker-compose.jobs.yml up storage-cleanup
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
  # The API app is the only process that exposes /metrics, and only when
  # METRICS_EXPORTER=prometheus_http (set in the API Deployment). In-cluster it
  # listens on 8000; the local dev/compose port is 8090.
  - job_name: 'faultmaven'
    static_configs:
      - targets: ['faultmaven-api.faultmaven.svc.cluster.local:8000']
```

**This scrape target cannot see the CronJob counters.** The jobs run as separate
short-lived processes that expose no endpoint — see the metrics note under
[Storage Cleanup Job](#1-storage-cleanup-job). Only metrics emitted inside the
API process are scrapable.

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

**Symptoms:** detected from the job's logs, not from Prometheus — the orphan
counters are incremented in the CronJob process and never scraped (see the
metrics note under [Storage Cleanup Job](#1-storage-cleanup-job)):

- The run's summary line reports a high `found=` (Loki: `{namespace="faultmaven", app="faultmaven-storage-cleanup"} |= "Storage cleanup"`)
- Or `found=` stays well above `deleted=` across consecutive runs

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

### Retry Queue Backup (not yet applicable)

> **There is no retry queue to inspect.** The async-turn-retry plan is deferred
> (see [Retry Queue Monitoring](#2-retry-queue-monitoring-future) above and the
> "scaffolded only, no emit sites" rows in
> [`docs/operations/monitoring/evidence-metrics.md`](monitoring/evidence-metrics.md)):
> no queue backend is wired, no `job:retry_queue` Redis key exists, and the
> runner registers exactly three jobs — `case_cleanup`, `kb_seed`,
> `storage_cleanup` (`AVAILABLE_JOBS` in `faultmaven/jobs/run.py`). This section
> is a placeholder for when that lands; do not follow it as a procedure, and do
> not treat the key name above as real.

---

## Operational Runbooks

### Daily Health Check

**Morning checklist:**
1. Check overnight storage cleanup job: `journalctl -u faultmaven-storage-cleanup.service | tail -50`
2. Verify orphaned file count reasonable (`found=` <5 in the run summary line)
3. Steps 3–4 below are not yet actionable — the turn-retry metrics are
   scaffolded with no emit sites, so there are no permanent-failure alerts and
   no retry success rate to read:
   - Check for permanent failure alerts (critical)
   - Review retry success rate (should be >50%)

### Weekly Review

**Weekly tasks:**
1. Review evidence creation trends (Grafana dashboard)
2. Check for category fallback spikes (schema drift)
3. Review retry configuration (adjust if needed)
4. Validate alert rules are firing correctly

### Incident Response

**Permanent DB Failure Alert** (no such alert exists yet — the turn-retry
counters have no emit sites; keep this as the intended procedure):
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

| Job | Schedule | Executor | Log Location | Detection |
|-----|----------|----------|--------------|-----------|
| case_cleanup | Daily 2 AM UTC | k8s CronJob | kubectl logs / Loki | log-based |
| storage_cleanup | Daily 3 AM UTC (after case_cleanup) | cron/systemd/k8s | journalctl / kubectl logs / Loki | log-based — `found=` in the run summary; counters are not scrapable from a CronJob |
| retry_monitoring | Every 5min | (future) | journalctl / kubectl logs | (future) |

---

**Implementation Status:** Complete
**Next Steps:** Wait for Phase 4 (evidence classification) to integrate error handling in `faultmaven/core/investigation/milestone_engine.py`
