---
id: "github-actions-workflow-failure"
title: "GitHub Actions Workflow Failure"
domain: application
service: github-actions
symptom_class: [timeout, auth_failure]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

**Statement:** A memory-intensive workflow step exceeded the runner's available RAM, causing the Linux OOM killer to send SIGKILL and produce exit code 137.

**Chain:**
- root: A step's working set exceeds the runner's available RAM (≈14 GB shared on GitHub-hosted `ubuntu-latest`, often less on self-hosted hosts shared with other workloads).
- s1: The Linux OOM killer sends SIGKILL to the offending process (large Docker build, JVM test suite, or parallel webpack compilation).
- D: The step terminates with exit code 137 and the workflow run shows a red status (points at Symptom Recognition).

**Indicators:**
- root: [Step 3] On self-hosted runners, `free -m` shows available memory below 2 GB at the time of failure.
- s1: [Step 2] Log line contains `Killed` or `OOMKilled`.
- D: [Step 1] Log line contains `exit code 137`.
  <!-- match: {"step": 2, "predicate": "contains", "target": "exit code 137"} -->

**Interventions:**
- **mitigation** (s1): Reduce per-process memory pressure so the OOM killer is not triggered.

  ```yaml
  - name: Build with reduced parallelism
    run: |
      NODE_OPTIONS="--max-old-space-size=6144" npm run build
      # Or for Docker builds:
      # docker build --build-arg MAKEFLAGS="-j2" .
  ```

  **Risk:** Low. Reducing parallelism or setting memory flags does not affect correctness, but it lengthens build time. **Duration:** Effective immediately after pushing the workflow file change; safe to leave permanently. **Verification:** Re-run the workflow; the build step completes without a `Killed`/`OOMKilled` log line.
- **remediation** (root): Provision more RAM by switching to a larger runner class (requires GitHub Team or Enterprise plan).

  ```yaml
  # Switch to a larger runner class (requires GitHub Team or Enterprise plan)
  jobs:
    build:
      runs-on: ubuntu-latest-16-cores
  ```

  **Verification:** Re-run the workflow. `gh run view <NEW_RUN_ID> --log | grep "exit code 137"` returns no matches and the job completes successfully.

### Cause B: Runner Disk Space Exhaustion

**Statement:** The runner filesystem reached capacity, causing build steps, cache writes, or Docker layer operations to fail with "No space left on device."

**Chain:**
- root: Pre-installed toolchains (Android SDKs, .NET runtimes, Docker layers consuming ≈20–25 GB) plus accumulated build artifacts and caches consume the runner's finite disk.
- s1: A build step, cache write, or Docker layer write requests space that is no longer available.
- D: The step fails with `No space left on device` and the workflow run shows a red status (points at Symptom Recognition).

**Indicators:**
- root: [Step 3] On self-hosted runners, `df -h /home/runner` shows disk usage above 90%.
- D: [Step 2] Log line contains `No space left on device`.
  <!-- match: {"step": 2, "predicate": "contains", "target": "No space left on device"} -->

**Interventions:**
- **mitigation** (root): Reclaim disk by removing pre-installed toolchains the workflow does not need before the build runs.

  ```yaml
  - name: Free disk space
    run: |
      sudo rm -rf /usr/local/lib/android /usr/share/dotnet /opt/ghc /usr/local/share/boost
      docker system prune -af
      df -h
  ```

  **Risk:** Low. Removes pre-installed toolchains not needed by most workflows; a workflow that depends on a removed toolchain will break. **Duration:** Adds 1–3 minutes to workflow execution time; safe to leave permanently. **Verification:** The `df -h` output in the same step shows freed headroom and the previously failing step succeeds.
- **remediation** (root): For self-hosted runner pools, reclaim disk durably via a post-job hook or cron so layers and logs do not accumulate across runs.

  ```bash
  # For self-hosted runners: add to the runner's cron or post-job hook
  docker system prune -af --volumes
  find /home/runner/_work -name "*.log" -mtime +7 -delete
  ```

  **Verification:** `df -h /home/runner` shows usage below 70% after cleanup. The previously failing step succeeds on the next run. (Disk reclamation is non-destructive — no rollback needed.)

### Cause C: Missing or Incorrectly Scoped Secret

**Statement:** A referenced secret was absent, misspelled, or scoped to a different environment than the job, so it expanded to an empty string and downstream authentication failed.

**Chain:**
- root: A `${{ secrets.X }}` reference has no matching secret in the job's scope (missing, misspelled, or environment-scoped without the job declaring `environment:`).
- s1: GitHub resolves the reference at job startup to an empty string instead of raising an error.
- s2: The step passes the empty credential to a CLI tool, API call, or environment variable, which rejects it (HTTP 401/403 or tool-specific "invalid token").
- D: The authentication step fails and the workflow run shows a red status (points at Symptom Recognition).

**Indicators:**
- root: [Step 5] A secret name referenced in the workflow YAML is absent from `gh secret list` (or the env-scoped `gh secret list --env production`) output.
- s2: [Step 2] Log contains `SecretNotFound`, `invalid token`, or a 401/403 response from an external service.
  <!-- match: {"step": 2, "predicate": "contains", "target": "SecretNotFound"} -->

**Interventions:**
- **mitigation** (root): Set or correct the secret value (including its environment scope) so the next run resolves it.

  ```bash
  gh secret set MY_SECRET --body "$SECRET_VALUE"
  gh secret set DEPLOY_TOKEN --env production --body "$TOKEN_VALUE"
  ```

  **Risk:** Low. Updating a secret value does not affect workflow logic, but a wrong value will still fail auth. **Duration:** Immediate; secret is available to the next triggered run. **Verification:** Re-run; the previously failing authentication step completes and secrets appear as `***` in the logs.
- **remediation** (root): Declare the correct `environment:` on the job so environment-scoped secrets become visible to it.

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

### Cause D: Dependency Installation Failure from Registry Throttling or Network Error

**Statement:** Package installation failed because a public registry rate-limited the shared runner IP pool or a transient network error interrupted the request.

**Chain:**
- root: A public registry (npm, PyPI, Maven Central) rate-limits the shared GitHub-hosted runner IP pool, or a transient DNS/TLS network fault occurs.
- s1: An install step's request returns HTTP 429 / `ETIMEDOUT` / `ECONNREFUSED` instead of the package.
- D: The install step exits non-zero and the workflow run shows a red status (points at Symptom Recognition).

**Indicators:**
- root: [Step 7] `gh api /rate_limit` shows `remaining` at or near 0 (the `GITHUB_TOKEN` default is 1,000 requests/hour), implicating throttling.
- s1: [Step 2] Log contains `ETIMEDOUT`, `ECONNREFUSED`, or `HTTP 429`.
  <!-- match: {"step": 2, "predicate": "contains", "target": "ETIMEDOUT"} -->
- D: [Step 1] The failed step is a package install step (`npm ci`, `pip install`, `mvn install`, etc.).

**Interventions:**
- **defensive_fix** (s1): Add bounded retries plus dependency caching at the install rung so a transient registry fault no longer fails the step.

  ```yaml
  - uses: actions/setup-node@v4
    with:
      node-version: '20'
      cache: 'npm'
  - name: Install with retry
    run: for i in 1 2 3; do npm ci && break || sleep 10; done
  ```

  **Verification:** Re-run the workflow; the install step succeeds despite a transient error, and subsequent runs do not show registry timeout errors.
- **remediation** (root): Cache dependencies across runs so most installs no longer hit the public registry at all.

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

### Cause E: Job Timeout — Step Hanging or Insufficient Timeout Configuration

**Statement:** A step deadlocked waiting for a network call, service health check, or subprocess that never responded, so the job ran until GitHub's maximum execution time (360 minutes default).

**Chain:**
- root: A step blocks indefinitely on an external dependency (TCP connection, HTTP response, or deadlocked subprocess) that never returns.
- s1: With no step-level `timeout-minutes`, the hung step consumes the full job timeout budget and ties up the runner slot.
- D: The job is terminated at the maximum execution time with `The job running on runner … has exceeded the maximum execution time` (points at Symptom Recognition).

**Indicators:**
- s1: [Step 2] Log contains `timeout`, or the last line before termination is a network or wait command with no further output.
- D: [Step 1] Log ends with `The job running on runner … has exceeded the maximum execution time`.
  <!-- match: {"step": 1, "predicate": "contains", "target": "exceeded the maximum execution time"} -->

**Interventions:**
- **defensive_fix** (s1): Add explicit job- and step-level `timeout-minutes` so a hung step fails fast instead of consuming the full budget.

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

  **Verification:** Re-run the workflow. Jobs that previously hung now fail fast at the configured timeout; `gh run view <RUN_ID> --log-failed` shows the specific hanging step within the timeout window.
- **remediation** (root): Identify the step where the log stream stops and fix the underlying hang.

  ```bash
  # Identify the hanging step by checking where the log stream stops:
  gh run view <RUN_ID> --log | tail -50
  # Then add timeout-minutes to that specific step and investigate the underlying hang
  ```

  **Verification:** Re-run the workflow. The previously hanging step now completes normally and the job finishes well within its timeout.

### Cause F: No Runner Matches the runs-on Label

**Statement:** No online runner carries the label specified in the job's `runs-on` field, so the job queued indefinitely waiting for a runner that never appears.

**Chain:**
- root: A label typo, a runner going offline/crashing without re-registration, or a runner label change without a matching workflow update leaves no online runner whose label set satisfies `runs-on`.
- s1: GitHub finds no eligible runner and holds the job in `queued` state.
- D: The job stays queued for more than 10 minutes with no runner picking it up (points at Symptom Recognition).

**Indicators:**
- root: [Step 8] No runner in the `gh api .../actions/runners` response has a label matching the job's `runs-on` value (or no runner is `"status": "online"`).
- D: [Step 8] `gh run list --status=queued` shows the job stuck for more than 10 minutes.
  <!-- match: {"step": 8, "predicate": "contains", "target": "queued"} -->

**Interventions:**
- **mitigation** (s1): Restart the self-hosted runner service so it re-registers and becomes eligible again.

  ```bash
  # Restart the runner service on the self-hosted host:
  sudo systemctl restart actions.runner.*
  # Or update the workflow to match the correct label:
  # runs-on: self-hosted  →  runs-on: [self-hosted, linux, x64]
  ```

  **Risk:** Low. Restarting the runner service has no side effects on a healthy host, but interrupts any in-progress job on that runner. **Duration:** Immediate after runner service restart; effective until the runner stops again. **Verification:** `gh run list --status=queued` no longer shows the stuck job and it transitions to `in_progress`.
- **remediation** (root): Inspect the available runner labels and correct the workflow `runs-on` value to match an online runner.

  ```bash
  # Verify available labels and fix the workflow runs-on value:
  gh api /repos/{owner}/{repo}/actions/runners \
    | jq '.runners[] | {name, status, labels: [.labels[].name]}'
  ```

  **Verification:** `gh run list --status=queued` shows no stuck jobs. The previously queued run is picked up by a runner and transitions to `in_progress` within 30 seconds.

### Cause G: Workflow YAML Syntax or Expression Error

**Statement:** The workflow YAML contains invalid structure, an unsupported expression, or a reference to an unknown action or step output, so it fails at the parsing stage before any runner is assigned.

**Chain:**
- root: The workflow file has a structural defect (bad indentation, invalid `${{ }}` expression, missing required field, unquoted YAML special characters, or a reference to an output from a step that may not have run).
- s1: GitHub parses the workflow before assigning a runner and rejects it with a workflow parse error.
- D: The entire run fails immediately, before any job is assigned a runner (points at Symptom Recognition).

**Indicators:**
- root: [Step 4] `actionlint` reports one or more errors with line numbers and descriptions (`unknown action`, `invalid expression`, `undefined output`).
  <!-- match: {"step": 4, "predicate": "contains", "target": "error"} -->
- D: [Step 1] The failed run shows an error before any job is assigned a runner.

**Interventions:**
- **remediation** (root): Fix the reported YAML/expression errors at the source.

  ```bash
  actionlint .github/workflows/ci.yml
  # Fix reported line numbers; common patterns:
  # - Quote strings with colons: run: "echo ${{ github.sha }}"
  # - Use if: always() or if: ${{ !cancelled() }} for cleanup steps
  ```

  **Verification:** `actionlint .github/workflows/ci.yml` exits 0 with no output. Re-triggering the workflow shows jobs assigned to runners rather than failing at parse time.

### Cause H: GitHub Platform Incident

**Statement:** A GitHub Actions platform outage or degraded performance — not any repository issue — is causing the workflow failures.

**Chain:**
- root: A GitHub Actions infrastructure incident degrades runner allocation, artifact storage, or the Actions API.
- s1: Affected workflows fail with errors indistinguishable from application-level failures in the log; re-running does not help until the incident clears.
- D: The workflow run shows a red status (points at Symptom Recognition).

**Indicators:**
- root: [Step 6] The GitHub status API returns a `"status"` other than `"operational"` for an Actions component.
  <!-- match: {"step": 6, "predicate": "absent", "target": "operational"} -->
- s1: [Symptom] Multiple unrelated workflows across the repository fail simultaneously with no recent code changes.

**Interventions:**
- **mitigation** (s1): Wait for platform resolution, monitoring the status page until Actions returns to operational.

  ```bash
  # Monitor until status returns to operational:
  watch -n 60 'curl -s https://www.githubstatus.com/api/v2/summary.json | jq ".components[] | select(.name | test(\"Actions\")) | {name, status}"'
  ```

  **Risk:** None. Waiting for platform resolution requires no change to the repository. **Duration:** Until GitHub resolves the incident (typically minutes to hours). **Verification:** The status API reports `operational` for all Actions components.
- **remediation** (root): After the incident clears, re-run the failed workflow.

  ```bash
  # After status returns to operational, re-run the failed workflow:
  gh run rerun <RUN_ID>
  ```

  **Verification:** `gh run watch <RUN_ID>` shows the re-run completing successfully with `conclusion: success`.

### Cause Z: Unidentified

**Statement:** The workflow failure cause could not be determined from available diagnostic output, due to a transient infrastructure issue, undocumented behavior, or interacting concurrent causes.

**Indicators:**
- [Default] None of the above causes matched the diagnostic output.

**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot by enabling step debug logging, re-run to gather additional signal, then escalate to the SME / GitHub Support with the run ID and workflow file.

  ```bash
  # Enable Actions step debug logging for the next run:
  gh secret set ACTIONS_STEP_DEBUG --body "true"
  gh secret set ACTIONS_RUNNER_DEBUG --body "true"
  # Then re-run:
  gh run rerun <RUN_ID> --debug
  # Escalate with the run ID and workflow file: https://support.github.com
  ```

  **Risk:** Low. Enabling debug logging may expose sensitive environment variable names in logs. **Duration:** Remove the debug secrets after investigation to avoid log noise. **Verification:** Debug logs from the re-run provide additional signal for root cause identification and escalation.

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
