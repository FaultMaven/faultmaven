---
id: github-actions-workflow-failure
title: "GitHub Actions Workflow Failures"
domain: application
service: github-actions
symptom_class:
  - timeout
  - auth_failure
severity: medium
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - github-actions
  - ci-cd
  - workflow
  - automation
  - pipelines
difficulty: intermediate
---

# GitHub Actions Workflow Failures

## Problem Definition

Applies to GitHub Actions (GitHub-hosted and self-hosted runners) across all plan tiers. Requires repository write access to view workflow logs, admin access to manage secrets and runner configuration. Familiarity with YAML workflow syntax and the `gh` CLI is assumed.

Workflow failures manifest as red status indicators in the Actions tab, with jobs reporting non-zero exit codes or timing out. Specific error signatures include exit code 137 (OOMKilled), exit code 143 (SIGTERM from timeout or cancellation), `SecretNotFound` or empty secret references causing downstream authentication failures, dependency installation failures from registry rate limits or network errors, and jobs queued indefinitely when `runs-on` labels do not match any available runner. Runners may report "No space left on device" when build artifacts, Docker layers, or tool caches exhaust disk. Scheduled workflows may skip or delay when triggered during high-load periods at the top of each hour. Path-filtered workflows silently skip when diffs exceed 300 files.

## Diagnostic Steps

### 1. Retrieve failed workflow run logs

Identifies which step failed and surfaces the immediate error message.

```bash
gh run list --workflow=ci.yml --limit=5
gh run view <RUN_ID> --log-failed
```

**Expected output:** Log output narrowed to the failed step with an error message or exit code. If the output is empty, the job was cancelled externally or timed out before producing output.

**What this means:** The error message in the failed step log is the starting point for all further diagnosis. Exit code 1 indicates a generic script failure; 137 indicates OOM; 143 indicates timeout/cancellation.

### 2. Search logs for known error patterns

Filters the full log stream for high-signal error keywords to quickly classify the failure mode.

```bash
gh run view <RUN_ID> --log | grep -iE "error|fatal|exit code|oom|killed|timeout|denied|no space"
```

**Expected output:** One or more lines containing error keywords. Multiple matches may indicate cascading failures (e.g., disk full causing a build failure which causes a test failure).

**What this means:** `exit code 137` points to OOM. `No space left on device` points to disk exhaustion. `SecretNotFound` or empty variable references point to secret misconfiguration. `ETIMEDOUT` or `ECONNREFUSED` during install steps points to registry/network issues.

### 3. Check runner disk and memory (self-hosted)

Determines whether the runner machine has sufficient resources to execute the job.

```bash
ssh runner-host 'df -h /home/runner && free -m && docker system df'
```

**Expected output:** Disk usage below 80%, available memory above 2GB, Docker images/containers within expected bounds. GitHub-hosted runners provide 14GB RAM and ~25GB usable SSD on `ubuntu-latest`.

**What this means:** If disk usage exceeds 90% or available memory is below 500MB, resource exhaustion is the root cause. Docker layer accumulation is the most common disk consumer on long-lived self-hosted runners.

### 4. Validate workflow syntax locally

Catches YAML structure errors, invalid expression syntax, and references to unknown actions before pushing.

```bash
actionlint .github/workflows/ci.yml
```

**Expected output:** No errors for a valid workflow. Errors include line numbers and descriptions such as "unknown action" or "invalid expression."

**What this means:** Syntax errors cause immediate job failure at the workflow parsing stage, before any step executes. The `actionlint` tool catches issues that the GitHub UI only surfaces at runtime.

### 5. Verify secret availability and scope

Confirms that all secrets referenced in the workflow exist and are accessible at the correct scope.

```bash
gh secret list
gh secret list --env production
```

**Expected output:** A list of secret names (values are never shown). Every `${{ secrets.X }}` reference in the workflow should have a corresponding entry.

**What this means:** Missing secrets resolve to empty strings at runtime, causing silent failures in authentication, API calls, or environment setup. Secrets are not available to workflows triggered from forked repositories (security restriction). Environment-scoped secrets require the job to specify the `environment:` key.

### 6. Check for GitHub platform incidents

Rules out platform-side outages affecting the Actions service.

```bash
curl -s https://www.githubstatus.com/api/v2/summary.json | jq '.components[] | select(.name | test("Actions")) | {name, status}'
```

**Expected output:** `"status": "operational"` for all Actions components.

**What this means:** If status is `degraded_performance` or `major_outage`, the failure is platform-side. Wait for resolution and re-run. GitHub experienced Actions degradations in February 2026, so platform issues are not uncommon.

### 7. Check API rate limits

Determines whether workflows making GitHub API calls are being throttled.

```bash
gh api /rate_limit | jq '.resources.core | {limit, remaining, reset: (.reset | todate)}'
```

**Expected output:** `remaining` well above 0. The default rate limit is 1,000 requests/hour for `GITHUB_TOKEN` in Actions.

**What this means:** If `remaining` is 0 or near 0, API calls within the workflow fail with 403 status. This commonly affects workflows that create many comments, update statuses, or interact with the GitHub API in loops.

### 8. Inspect queued self-hosted runner jobs

Identifies jobs that cannot be assigned to any runner due to label mismatches or offline runners.

```bash
gh run list --status=queued --limit=10
gh api /repos/{owner}/{repo}/actions/runners | jq '.runners[] | {name, status, busy, labels: [.labels[].name]}'
```

**Expected output:** Queued jobs should have matching runner labels. Runner status should be `online`.

**What this means:** Jobs queue indefinitely when no runner matches the `runs-on` labels. This happens when runner labels are changed without updating workflows, runners go offline, or custom label names have typos.

## Mitigation

### Option 1: Free disk space on GitHub-hosted runners

**Risk:** Low. Removes pre-installed SDKs and cached images that are not needed for most workflows.

**Command:**

```yaml
- name: Free disk space
  run: |
    sudo rm -rf /usr/local/lib/android /usr/share/dotnet /opt/ghc /usr/local/share/boost
    docker system prune -af
    df -h
```

**Verify:** The `df -h` output at the end of the step shows at least 30GB free on `/`.

**Duration:** 1-3 minutes added to workflow execution.

### Option 2: Set explicit job and step timeouts

**Risk:** Low. Prevents runaway jobs from consuming 6 hours of runner time. Does not affect successful runs.

**Command:**

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

**Verify:** Re-run the workflow. Jobs that previously hung now fail fast at the configured timeout, freeing runners for other work.

**Duration:** Immediate after pushing workflow file update.

### Option 3: Restore missing or expired secrets

**Risk:** Low. Updates secret values without affecting workflow logic.

**Command:**

```bash
gh secret set MY_SECRET --body "$SECRET_VALUE"
gh secret set DEPLOY_TOKEN --env production --body "$TOKEN_VALUE"
```

**Verify:** Re-run the workflow. Steps that previously failed with empty credentials now authenticate successfully. Secrets appear as `***` in logs when correctly loaded.

**Duration:** 1-2 minutes.

### Option 4: Add dependency caching and retry logic

**Risk:** Low. Caching reduces network dependency; retries handle transient registry errors.

**Command:**

```yaml
- uses: actions/setup-node@v4
  with:
    node-version: '20'
    cache: 'npm'
- name: Install with retry
  run: for i in 1 2 3; do npm ci && break || sleep 10; done
```

**Verify:** Subsequent runs show `Cache restored successfully` in logs. `gh run view <RUN_ID> --log | grep -i "cache hit"` returns matches.

**Duration:** 5 minutes for workflow file update; cache benefits appear on second run.

### Option 5: Resolve OOM by reducing memory pressure

**Risk:** Medium. Changing runner size increases cost. Build optimization may require code changes.

**Command:**

For Node.js heap issues:

```yaml
- run: NODE_OPTIONS="--max-old-space-size=6144" npm run build
```

For Docker builds:

```yaml
- run: docker build --build-arg MAKEFLAGS="-j2" .
```

For persistent OOM on GitHub-hosted runners, switch to a larger runner:

```yaml
runs-on: ubuntu-latest-16-cores
```

**Verify:** `gh run view <RUN_ID> --log | grep "exit code 137"` returns no matches.

**Duration:** Immediate after workflow file update.

## Root Cause Resolution

**If** logs show exit code 137 or OOMKilled → the runner lacks sufficient memory. Profile the build to identify memory-hungry steps. Reduce parallelism (`-j2`), increase `--max-old-space-size`, split into smaller parallel jobs using matrix strategy, or upgrade to a larger runner class.

**If** secrets resolve to empty strings → the secret name is misspelled (case-sensitive), scoped to a different environment, or unavailable because the workflow was triggered from a fork. Verify exact names with `gh secret list`, ensure the job declares `environment:` for environment-scoped secrets, and use `GITHUB_TOKEN` with OIDC for cloud provider authentication instead of long-lived tokens.

**If** dependency installation fails with ETIMEDOUT or 429 errors → the package registry is rate-limiting or experiencing an outage. Add `actions/cache` or built-in setup action caching, implement retry loops with backoff, pin exact dependency versions to avoid resolution overhead, and consider a private registry mirror for critical dependencies.

**If** self-hosted runner jobs queue indefinitely → runner labels do not match `runs-on`. Verify labels with `gh api /repos/{owner}/{repo}/actions/runners`, fix label mismatches, and ensure the runner service is running (`sudo systemctl status actions.runner.*`).

**If** the workflow times out at 360 minutes (default) → the job is doing too much work in a single job or a step is hanging. Break into smaller jobs using `needs:` dependencies, add step-level `timeout-minutes`, use matrix strategies for test parallelism, and investigate hanging steps (often waiting for a service or network call that never completes).

**If** `actionlint` reports syntax errors → fix the YAML structure. Common issues: incorrect indentation, `${{ }}` expression errors (missing quotes around strings with special characters), referencing outputs from steps that did not run, and using `always()` instead of `!cancelled()` for conditional cleanup steps.

## Verification

1. **Re-run the failed workflow and monitor completion:**

```bash
gh run rerun <RUN_ID> && gh run watch <RUN_ID>
```

Final status should be `completed` with conclusion `success`.

2. **Confirm no recurring failures across recent runs:**

```bash
gh run list --workflow=ci.yml --limit=10 --json conclusion | jq '.[].conclusion'
```

All entries should show `"success"`. Any `"failure"` entries require individual investigation.

3. **Validate required status checks pass on open PRs:**

```bash
gh pr checks <PR_NUMBER>
```

All required checks show green. If branch protection requires status checks, PRs become mergeable.

4. **Confirm resource utilization is stable (self-hosted):**

```bash
ssh runner-host 'df -h /home/runner && free -m'
```

Disk below 70% and memory above 2GB after a full workflow run indicates stable resource headroom.

## Prevention

- **Enforce `actionlint` in pre-commit hooks** to catch workflow syntax errors before they reach the default branch. Install via `brew install actionlint` or run as a CI step in the workflow itself.
- **Pin action versions to full commit SHA** (`uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683`) instead of tags to prevent supply chain attacks and ensure reproducibility.
- **Set `timeout-minutes` on every job** with values 2-3x the observed p95 duration. Never rely on the 360-minute default.
- **Cache all dependency installation steps** using `actions/cache` or built-in caching in setup actions (`setup-node`, `setup-python`, `setup-java`).
- **Use `concurrency` groups** to prevent resource contention from parallel runs on the same branch: `concurrency: { group: "${{ github.workflow }}-${{ github.ref }}", cancel-in-progress: true }`.
- **Rotate secrets before expiry** and prefer short-lived OIDC tokens for cloud provider authentication over stored long-lived credentials.
- **Monitor workflow metrics** via the GitHub API: track p50/p95 duration, failure rate, and queue time. Alert on failure rate exceeding 10% over a rolling 24-hour window.
- **Schedule workflows away from the top of the hour** to avoid contention during GitHub's peak load periods.

## Sources

- [GitHub Actions — Troubleshooting Workflows](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows) — Official diagnostic procedures for workflow failures, debug logging, and runner issues
- [GitHub Actions — Workflow Syntax Reference](https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions) — Complete YAML syntax including `timeout-minutes`, `concurrency`, and expression syntax
- [GitHub Actions — Self-Hosted Runners](https://docs.github.com/en/actions/hosting-your-own-runners) — Runner setup, label management, monitoring, and troubleshooting
- [GitHub Actions — Security Hardening](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions) — Secret management, OIDC authentication, and supply chain security
- [actionlint — Static Analyzer for GitHub Actions](https://github.com/rhysd/actionlint) — Workflow file linting including expression validation and action reference checking
- [GitHub Actions Timeout Configuration](https://graphite.com/guides/github-actions-timeouts) — Job-level and step-level timeout best practices
- [Freeing Disk Space on GitHub Actions Runners](https://www.dzombak.com/blog/2024/09/freeing-disk-space-on-github-actions-runners/) — Strategies for reclaiming disk on ubuntu-latest runners
