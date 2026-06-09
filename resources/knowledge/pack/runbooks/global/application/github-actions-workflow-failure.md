---
id: "github-actions-workflow-failure"
title: "GitHub Actions Workflow Failure"
domain: application
service: github-actions
symptom_class: [timeout, auth_failure]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [github-actions, ci-cd, workflow, automation, pipelines]
difficulty: intermediate
---

## Symptom Recognition

- Workflow run shows red status in the Actions tab with a non-zero exit code.
- Exit code 137 in step logs (OOMKilled — process exceeded runner memory).
- Exit code 143 in step logs (SIGTERM — job timed out or was cancelled).
- Step output contains `SecretNotFound` or credential fields resolve to empty strings causing downstream 401/403 errors.
- Dependency install step fails with `ETIMEDOUT`, `ECONNREFUSED`, or HTTP 429 from package registry.
- Job stays in `queued` state for more than 10 minutes with no runner picking it up.
- Step output contains `No space left on device` during build, cache restore, or Docker layer write.
- Workflow run terminates at exactly 360 minutes with `The job running on runner … has exceeded the maximum execution time`.
- `actionlint` or GitHub's workflow parser reports YAML syntax or expression errors before any step executes.

## Applicability

Applies to GitHub Actions on GitHub-hosted runners (`ubuntu-latest`, `windows-latest`, `macos-latest`) and self-hosted runners across all GitHub plan tiers. Requires repository write access to view workflow run logs. Repository or organization admin access is required to manage secrets, runner registration, and runner labels. The `gh` CLI (version 2.x+) and `actionlint` (any recent version) are used in diagnostic steps. GitHub-hosted `ubuntu-latest` runners provide approximately 14 GB RAM and 25 GB usable SSD.

## Diagnostic Steps

### Step 1: Retrieve failed workflow run logs

```bash
gh run list --workflow=ci.yml --limit=5
gh run view <RUN_ID> --log-failed
```

Expected output: Log output scoped to the failed step showing an error message or exit code. An empty output means the job was cancelled externally or timed out before producing output.

### Step 2: Scan full log for high-signal error keywords

```bash
gh run view <RUN_ID> --log | grep -iE "error|fatal|exit code|oom|killed|timeout|denied|no space|etimedout|econnrefused|secretnotfound|rate.?limit"
```

Expected output: One or more lines with error keywords. `exit code 137` indicates OOM. `No space left on device` indicates disk exhaustion. `SecretNotFound` or empty credential strings indicate secret misconfiguration. `ETIMEDOUT` or `429` during install steps indicates registry or network issues.

### Step 3: Check runner disk and memory (self-hosted runners only)

```bash
ssh runner-host 'df -h /home/runner && free -m && docker system df'
```

Expected output: Disk usage below 80%, available memory above 2 GB, Docker image/container sizes within expected bounds. Values outside these ranges indicate resource exhaustion as the probable cause.

### Step 4: Validate workflow YAML syntax locally

```bash
actionlint .github/workflows/ci.yml
```

Expected output: No output for a valid file. Errors include line numbers and descriptions such as `unknown action`, `invalid expression`, or `undefined output`.

### Step 5: Verify secret availability and scope

```bash
gh secret list
gh secret list --env production
```

Expected output: A list of secret names (values never shown). Every `${{ secrets.X }}` reference in the workflow must have a corresponding entry in this list. Environment-scoped secrets appear only under the named environment.

### Step 6: Check GitHub platform status for Actions

```bash
curl -s https://www.githubstatus.com/api/v2/summary.json | jq '.components[] | select(.name | test("Actions")) | {name, status}'
```

Expected output: `"status": "operational"` for all Actions components. Any other status value means the failure is platform-side.

### Step 7: Check API rate limit remaining

```bash
gh api /rate_limit | jq '.resources.core | {limit, remaining, reset: (.reset | todate)}'
```

Expected output: `remaining` well above 0. The default rate limit for `GITHUB_TOKEN` inside Actions is 1,000 requests/hour. A `remaining` value at or near 0 indicates throttling as the cause.

### Step 8: Inspect queued jobs and runner label assignment

```bash
gh run list --status=queued --limit=10
gh api /repos/{owner}/{repo}/actions/runners | jq '.runners[] | {name, status, busy, labels: [.labels[].name]}'
```

Expected output: Queued jobs should have `runs-on` labels that match at least one online runner's label set. `"status": "online"` and `"busy": false` means the runner is available.

## Causes

### Cause A: Runner Out of Memory

**Statement:** The workflow step was killed by the OS because the runner process exceeded available memory, producing exit code 137.

**Mechanism:** GitHub-hosted `ubuntu-latest` runners have approximately 14 GB RAM shared across all concurrent steps and Docker daemons. Memory-intensive steps such as large Docker builds, JVM-based test suites, or parallel webpack compilations exhaust available RAM, causing the Linux OOM killer to send SIGKILL (exit code 137) to the offending process. Self-hosted runners may have less RAM or be sharing the host with other workloads.

**Indicator:**

- [Step 1] Log line contains `exit code 137`
- [Step 2] Log line contains `Killed` or `OOMKilled`

<!-- match: {"step": 2, "predicate": "contains", "target": "exit code 137"} -->

**Mitigation:**

- **Risk:** Low. Reducing parallelism or setting memory flags does not affect correctness; upgrading runner size increases billing cost.
- **Command:**

  ```yaml
  - name: Build with reduced parallelism
    run: |
      NODE_OPTIONS="--max-old-space-size=6144" npm run build
      # Or for Docker builds:
      # docker build --build-arg MAKEFLAGS="-j2" .
  ```

- **Duration:** Immediate after pushing the workflow file change.

**Resolution:**

```yaml
# Switch to a larger runner class (requires GitHub Team or Enterprise plan)
jobs:
  build:
    runs-on: ubuntu-latest-16-cores
```

**Verification:** Re-run the workflow. `gh run view <NEW_RUN_ID> --log | grep "exit code 137"` returns no matches and the job completes successfully.

---

### Cause B: Runner Disk Space Exhaustion

**Statement:** The runner filesystem reached capacity, causing build steps, cache writes, or Docker layer operations to fail with "No space left on device."

**Mechanism:** GitHub-hosted `ubuntu-latest` runners include pre-installed Android SDKs, .NET runtimes, and Docker image layers that consume approximately 20–25 GB of the available disk before the workflow starts. Large Docker builds, npm/pip caches, or test artifact accumulation consume the remaining headroom. Self-hosted runners additionally accumulate Docker layers and tool caches across runs.

**Indicator:**

- [Step 2] Log line contains `No space left on device`
- [Step 3] `df -h` shows disk usage above 90% on self-hosted runners

<!-- match: {"step": 2, "predicate": "contains", "target": "No space left on device"} -->

**Mitigation:**

- **Risk:** Low. Removes pre-installed toolchains not needed by most workflows.
- **Command:**

  ```yaml
  - name: Free disk space
    run: |
      sudo rm -rf /usr/local/lib/android /usr/share/dotnet /opt/ghc /usr/local/share/boost
      docker system prune -af
      df -h
  ```

- **Duration:** 1–3 minutes added to workflow execution time; safe to leave permanently.

**Resolution:**

```bash
# For self-hosted runners: add to the runner's cron or post-job hook
docker system prune -af --volumes
find /home/runner/_work -name "*.log" -mtime +7 -delete
```

- **Impact:** Cluster-wide for self-hosted runner pools; single-run for GitHub-hosted runners.

- **Rollback:** Not applicable — disk reclamation is non-destructive.

**Verification:** `df -h /home/runner` shows usage below 70% after cleanup. The previously failing step succeeds on the next run.

---

### Cause C: Missing or Incorrectly Scoped Secret

**Statement:** A workflow step failed authentication because a referenced secret was absent, misspelled, or scoped to a different environment than the job.

**Mechanism:** GitHub resolves `${{ secrets.X }}` at job startup; missing secrets silently expand to empty strings rather than raising an error. Steps that pass these empty strings to CLI tools, API calls, or environment variables then fail with authentication errors (HTTP 401, 403, or tool-specific "invalid token" messages). Environment-scoped secrets require the job's `environment:` key to match the secret's scope; without it the secret is invisible to the job.

**Indicator:**

- [Step 5] Secret name referenced in workflow YAML is absent from `gh secret list` output
- [Step 2] Log contains `SecretNotFound`, `invalid token`, or a 401/403 response from an external service

<!-- match: {"step": 2, "predicate": "contains", "target": "SecretNotFound"} -->

**Mitigation:**

- **Risk:** Low. Updating a secret value does not affect workflow logic.
- **Command:**

  ```bash
  gh secret set MY_SECRET --body "$SECRET_VALUE"
  gh secret set DEPLOY_TOKEN --env production --body "$TOKEN_VALUE"
  ```

- **Duration:** Immediate; secret is available to the next triggered run.

**Resolution:**

```yaml
# Ensure the job declares the correct environment for environment-scoped secrets
jobs:
  deploy:
    environment: production
    steps:
      - run: ./deploy.sh
        env:
          DEPLOY_TOKEN: ${{ secrets.DEPLOY_TOKEN }}
```

**Verification:** Re-run the workflow. The previously failing authentication step completes without errors. Secrets appear as `***` in the logs when correctly loaded.

---

### Cause D: Dependency Installation Failure from Registry Throttling or Network Error

**Statement:** Package installation (npm, pip, Maven, etc.) failed due to rate limiting, transient network errors, or a registry outage.

**Mechanism:** Public registries (npm, PyPI, Maven Central) apply rate limits to unauthenticated or shared-IP requests. GitHub-hosted runners share IP address pools, so concurrent CI runs from many tenants can collectively exhaust registry rate limits, producing HTTP 429 or ETIMEDOUT errors. Transient DNS resolution failures and TLS handshake timeouts produce ECONNREFUSED or ETIMEDOUT errors that succeed on retry.

**Indicator:**

- [Step 2] Log contains `ETIMEDOUT`, `ECONNREFUSED`, or `HTTP 429`
- [Step 1] Failed step is a package install step (npm ci, pip install, mvn install, etc.)

<!-- match: {"step": 2, "predicate": "contains", "target": "ETIMEDOUT"} -->

**Mitigation:**

- **Risk:** Low. Adding retry logic and caching reduces network dependency without changing build output.
- **Command:**

  ```yaml
  - uses: actions/setup-node@v4
    with:
      node-version: '20'
      cache: 'npm'
  - name: Install with retry
    run: for i in 1 2 3; do npm ci && break || sleep 10; done
  ```

- **Duration:** 5 minutes for workflow file update; cache benefits appear on the second run.

**Resolution:**

```yaml
# Pin a specific registry mirror and use caching for all install steps
- uses: actions/cache@v4
  with:
    path: ~/.npm
    key: ${{ runner.os }}-node-${{ hashFiles('**/package-lock.json') }}
    restore-keys: |
      ${{ runner.os }}-node-
```

**Verification:** `gh run view <RUN_ID> --log | grep -i "cache hit"` returns matches. Subsequent runs do not show registry timeout errors.

---

### Cause E: Job Timeout — Step Hanging or Insufficient Timeout Configuration

**Statement:** The workflow job reached GitHub's maximum execution time (360 minutes default) because a step hung waiting for a network call, service health check, or external resource that never responded.

**Mechanism:** GitHub terminates any job that runs longer than `timeout-minutes` (default: 360). A hanging step — typically waiting for a TCP connection, HTTP response, or subprocess that deadlocked — consumes the full timeout budget. Without step-level timeouts, a single stuck step blocks all subsequent steps and ties up the runner slot for up to 6 hours.

**Indicator:**

- [Step 1] Log ends with `The job running on runner … has exceeded the maximum execution time`
- [Step 2] Log contains `timeout` or the last line before termination is a network or wait command

<!-- match: {"step": 1, "predicate": "contains", "target": "exceeded the maximum execution time"} -->

**Mitigation:**

- **Risk:** Low. Explicit timeouts cause faster failure without affecting successful runs.
- **Command:**

  ```yaml
  jobs:
    build:
      runs-on: ubuntu-latest
      timeout-minutes: 30
      steps:
        - name: Run tests
          timeout-minutes: 15
          run: npm test
  ```

- **Duration:** Immediate after pushing the workflow file change.

**Resolution:**

```bash
# Identify the hanging step by checking where the log stream stops:
gh run view <RUN_ID> --log | tail -50
# Then add timeout-minutes to that specific step and investigate the underlying hang
```

**Verification:** Re-run the workflow. Jobs that previously hung now fail fast at the configured timeout. `gh run view <RUN_ID> --log-failed` shows the specific hanging step within the timeout window.

---

### Cause F: No Runner Matches the runs-on Label

**Statement:** The workflow job queued indefinitely because no online runner has the label specified in the job's `runs-on` field.

**Mechanism:** GitHub compares the job's `runs-on` value against all registered runners' label sets. If no online runner carries the required label — due to a label typo, runner going offline, or a label change in the runner configuration without a corresponding workflow update — the job stays in `queued` state until manually cancelled or until the runner comes online. Self-hosted runner services that stop or crash without re-registration are a common source.

**Indicator:**

- [Step 8] `gh run list --status=queued` shows the job stuck for more than 10 minutes
- [Step 8] No runner in the API response has a label matching the job's `runs-on` value

<!-- match: {"step": 8, "predicate": "contains", "target": "queued"} -->

**Mitigation:**

- **Risk:** Low. Correcting the label or restarting the runner service has no side effects.
- **Command:**

  ```bash
  # Restart the runner service on the self-hosted host:
  sudo systemctl restart actions.runner.*
  # Or update the workflow to match the correct label:
  # runs-on: self-hosted  →  runs-on: [self-hosted, linux, x64]
  ```

- **Duration:** Immediate after runner service restart or workflow file push.

**Resolution:**

```bash
# Verify available labels and fix the workflow runs-on value:
gh api /repos/{owner}/{repo}/actions/runners \
  | jq '.runners[] | {name, status, labels: [.labels[].name]}'
```

**Verification:** `gh run list --status=queued` shows no stuck jobs. The previously queued run is picked up by a runner and transitions to `in_progress` within 30 seconds.

---

### Cause G: Workflow YAML Syntax or Expression Error

**Statement:** The workflow failed at the parsing stage because of invalid YAML structure, an unsupported expression, or a reference to an unknown action or step output.

**Mechanism:** GitHub parses workflow YAML before any runner is assigned. Structural errors (incorrect indentation, invalid `${{ }}` expressions, missing required fields) cause the entire run to fail immediately with a workflow parse error. Common mistakes include unquoted strings containing special YAML characters, referencing `steps.X.outputs.Y` from steps that may not have run, and using deprecated action versions.

**Indicator:**

- [Step 4] `actionlint` reports one or more errors with line numbers
- [Step 1] Failed run shows error before any job is assigned a runner

<!-- match: {"step": 4, "predicate": "contains", "target": "error"} -->

**Mitigation:**

- **Risk:** Low. Fixing YAML syntax is a safe change.
- **Command:**

  ```bash
  actionlint .github/workflows/ci.yml
  # Fix reported line numbers; common patterns:
  # - Quote strings with colons: run: "echo ${{ github.sha }}"
  # - Use if: always() or if: ${{ !cancelled() }} for cleanup steps
  ```

- **Duration:** Immediate after pushing the corrected workflow file.

**Resolution:** Same as Mitigation.

**Verification:** `actionlint .github/workflows/ci.yml` exits 0 with no output. Re-triggering the workflow shows jobs assigned to runners rather than failing at parse time.

---

### Cause H: GitHub Platform Incident

**Statement:** Workflow failures are caused by a GitHub Actions platform outage or degraded performance, not by any issue within the repository.

**Mechanism:** GitHub Actions is a distributed platform; infrastructure incidents affecting runner allocation, artifact storage, or the Actions API produce failures that are indistinguishable from application-level failures in the workflow log. When the platform status page shows `degraded_performance` or `major_outage` for Actions components, re-running workflows will not help until the incident is resolved.

**Indicator:**

- [Step 6] GitHub status API returns `"status"` other than `"operational"` for an Actions component
- [Symptom] Multiple unrelated workflows across the repository fail simultaneously with no recent code changes

<!-- match: {"step": 6, "predicate": "absent", "target": "operational"} -->

**Mitigation:**

- **Risk:** None. Waiting for platform resolution requires no action.
- **Command:**

  ```bash
  # Monitor until status returns to operational:
  watch -n 60 'curl -s https://www.githubstatus.com/api/v2/summary.json | jq ".components[] | select(.name | test(\"Actions\")) | {name, status}"'
  ```

- **Duration:** Until GitHub resolves the incident (typically minutes to hours).

**Resolution:**

```bash
# After status returns to operational, re-run the failed workflow:
gh run rerun <RUN_ID>
```

**Verification:** `gh run watch <RUN_ID>` shows the re-run completing successfully with `conclusion: success`.

---

### Cause Z: Unidentified Workflow Failure

**Statement:** The workflow failure cause could not be determined from available diagnostic output. [Default]

**Mechanism:** Some failures involve transient infrastructure issues, undocumented GitHub Actions behavior, or interactions between multiple concurrent causes that do not produce a clear single error signature. Further investigation using debug logging and GitHub Support is required.

**Indicator:**

- [Default] None of the above causes matched diagnostic output

**Mitigation:**

- **Risk:** Low. Enabling debug logging may expose sensitive environment variable names in logs.
- **Command:**

  ```bash
  # Enable Actions step debug logging for the next run:
  gh secret set ACTIONS_STEP_DEBUG --body "true"
  gh secret set ACTIONS_RUNNER_DEBUG --body "true"
  # Then re-run:
  gh run rerun <RUN_ID> --debug
  ```

- **Duration:** Remove debug secrets after investigation to avoid log noise.

**Resolution:** Out of runbook scope. Escalate to [GitHub Support](https://support.github.com) with the run ID and workflow file.

**Verification:** Debug logs from the re-run provide additional signal for root cause identification.

## Prevention

- Enforce `actionlint` in pre-commit hooks or as a CI step to catch workflow syntax errors before they reach the default branch.
- Pin action references to full commit SHAs (`uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683`) to prevent supply-chain attacks and unintended version changes.
- Set `timeout-minutes` on every job at 2–3x observed p95 duration; never rely on the 360-minute default.
- Use `actions/cache` or built-in caching in setup actions (`setup-node`, `setup-python`, `setup-java`) to reduce registry dependency on every run.
- Use `concurrency` groups to prevent resource contention from parallel runs on the same branch:

  ```yaml
  concurrency:
    group: "${{ github.workflow }}-${{ github.ref }}"
    cancel-in-progress: true
  ```

- Prefer short-lived OIDC tokens for cloud provider authentication over long-lived stored credentials; rotate long-lived secrets before expiry.
- Monitor workflow metrics via the GitHub API: track p50/p95 duration, failure rate, and queue time. Alert when failure rate exceeds 10% over a rolling 24-hour window.
- Add a disk-cleanup step as the first step of builds that use Docker or large language runtimes.
- Schedule cron-triggered workflows away from the top of the hour to avoid GitHub's peak load periods.

## Sources

- [GitHub Actions — Monitoring and Troubleshooting Workflows](https://docs.github.com/en/actions/monitoring-and-troubleshooting-workflows) — Official troubleshooting procedures, debug logging, and runner diagnostics (Priority 1)
- [GitHub Actions — Workflow Syntax Reference](https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions) — Complete YAML syntax including `timeout-minutes`, `concurrency`, and expression syntax (Priority 1)
- [GitHub Actions — Self-Hosted Runners](https://docs.github.com/en/actions/hosting-your-own-runners) — Runner setup, label management, and troubleshooting (Priority 1)
- [GitHub Actions — Security Hardening](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions) — Secret management, OIDC authentication, and supply chain security (Priority 1)
- [actionlint — Static Analyzer for GitHub Actions](https://github.com/rhysd/actionlint) — Workflow YAML linting including expression validation and action reference checking (Priority 2)
