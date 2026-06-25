---
id: "gitlab-pipeline-failure"
title: "GitLab CI pipeline failures: stuck jobs, rules/needs errors, artifacts, and timeouts"
domain: application
service: gitlab-ci
symptom_class: [deployment_failure, timeout]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [job-stuck, no-active-runners, gitlab-runner, needs-error, job-timeout, artifacts-too-large]
difficulty: intermediate
---

## Symptom Recognition

- Job badge in the pipeline view shows **stuck** (pending, never starts), with the message:
  `This job is stuck because you don't have any active runners online or available with any of these tags assigned to it`
- Or: `This job is stuck because the project doesn't have any runners online assigned to it`
- Pipeline shows a `yaml invalid` badge; the pipeline does not run at all.
- Job log ends with: `config contains unknown keys: <key-name>`
- Job log ends with: `<job-name> job needs <other-job> job, but it was not added to the pipeline` (DAG/`needs` error).
- Job fails after running: `ERROR: Job failed: execution took longer than <N>m<S>s seconds`.
- Artifact upload fails: `Uploading artifacts as "archive" to coordinator... too large` or `WARNING: ... no files to upload` / `No files to upload`.
- Downstream job: `This job could not start because it could not retrieve the needed artifacts.`

## Applicability

- GitLab 16.x–17.x (self-managed or GitLab.com); GitLab Runner whose major.minor version matches the GitLab version.
- Required access: Maintainer/Owner on the project or group (to view runner status, edit CI/CD settings, change timeouts/limits); shell access on the runner host for `gitlab-runner` commands.
- Tools: GitLab web UI (Pipeline editor, CI Lint, Runners page), `gitlab-runner` CLI on the runner host, `journalctl`/`systemctl`, `git`.

## Diagnostic Steps

### Step 1: Validate `.gitlab-ci.yml` syntax with CI Lint

Use the CI Lint tool (Project > Build > Pipeline editor > Validate tab, or `/-/ci/lint`) with "Simulate pipeline creation" enabled, or check the badge:

```bash
# Confirm the file parses locally before pushing (schema-aware editors / yamllint as a pre-check)
yamllint .gitlab-ci.yml
git show HEAD:.gitlab-ci.yml | head -c 3 | xxd   # detect a leading UTF-8 BOM (ef bb bf)
```

Expected output: `yamllint` reports no errors; `xxd` does NOT show `efbb bf` at byte 0. CI Lint reports "Simulation completed successfully".

### Step 2: Confirm at least one runner is online and matches the job

On the project/group Runners page (Settings > CI/CD > Runners), check for a runner with a green "online" dot, then verify tags. On the runner host:

```bash
sudo gitlab-runner list
sudo gitlab-runner verify
sudo gitlab-runner status
```

Expected output: `gitlab-runner list` shows the runner; `gitlab-runner verify` prints `Verifying runner... is alive   runner=<id>` for each runner; `gitlab-runner status` prints `gitlab-runner: Service is running` and exits 0.

### Step 3: Compare job tags against runner tags and untagged setting

Inspect the job's `tags:` in `.gitlab-ci.yml` and the runner's tags / "Run untagged jobs" flag in `config.toml`:

```bash
grep -nE '^\s*tags:' .gitlab-ci.yml
sudo grep -E 'tags|run_untagged' /etc/gitlab-runner/config.toml
```

Expected output: For the job to be picked up, the runner must carry ALL tags listed on the job; an untagged job requires `run_untagged = true` on the runner.

### Step 4: Read the failing job log tail and check timeout settings

Open the job log (job page > raw, or download). Note the final error line. Check effective timeout:

```bash
# Project timeout: Settings > CI/CD > General pipelines > Timeout (default 60m)
# Runner timeout (lower of the two wins):
sudo grep -nE 'maximum_timeout|RUNNER_SCRIPT_TIMEOUT' /etc/gitlab-runner/config.toml
```

Expected output: The job log's final line names the failure class (timeout, artifact upload, `needs`). The lower of project timeout and runner `maximum_timeout` is the effective cap.

### Step 5: Inspect runner service logs for registration/connection errors

```bash
sudo journalctl --unit=gitlab-runner.service -n 100 --no-pager
```

Expected output: Lines showing successful job acquisition (`Checking for jobs... received`) for a healthy runner; `Checking for jobs... nothing` repeatedly or auth/registration errors point to a runner-side fault.

## Causes

### Cause A: No runner is online/registered for the project
**Statement:** The project has zero active runners assigned (none registered, the runner service is stopped, or the authentication token was deleted/expired), so no executor ever claims the job.
**Chain:**
- root: no active runner is connected to GitLab for this project
- s1: the scheduler finds no runner to assign the pending job
- D: the job is stuck pending and the pipeline never progresses
**Indicators:**
- root: [Step 2] `gitlab-runner status` reports the service is not running, or `gitlab-runner verify` fails to reach GitLab; Runners page shows no online runner
  <!-- match: {"step": 2, "predicate": "absent", "target": "is alive"} -->
- s1: [Symptom] stuck message reads `the project doesn't have any runners online assigned to it`
  <!-- match: {"step": 4, "predicate": "contains", "target": "doesn't have any runners online"} -->
**Interventions:**
- **remediation** (root): start the runner service and (if the token was deleted) re-register it, then assign it to the project.

  ```bash
  sudo gitlab-runner start
  # If verify shows no runners, re-register with a project/group runner authentication token (glrt-...)
  sudo gitlab-runner register --url https://gitlab.example.com --token glrt-XXXXXXXX
  sudo gitlab-runner verify
  ```

  **Verification:** Re-run Step 2 — `gitlab-runner verify` prints `is alive`; the Runners page shows the runner online; retry the pipeline and the job leaves the pending state.
- **mitigation** (s1): assign an existing online instance/group runner to the project (Settings > CI/CD > Runners > Enable for this project) to clear the backlog while the dedicated runner is fixed.

  ```bash
  # No host command; toggle in UI. Then retry the stuck job:
  # Project > Build > Pipelines > <pipeline> > retry
  ```

  **Risk:** A shared runner may have a different environment/tags than intended, producing inconsistent builds. **Duration:** Until the project's own runner is restored. **Verification:** The job is picked up and runs to completion.

### Cause B: Job tags do not match any runner (or untagged job with no untagged runner)
**Statement:** The job's `tags:` set is not fully satisfied by any online runner's tags, or the job is untagged while every available runner has `run_untagged = false`, so no runner is eligible.
**Chain:**
- root: no online runner carries every tag the job requires (or accepts untagged jobs)
- s1: tag-matching excludes all candidate runners for this job
- D: the job is stuck pending despite runners being online
**Indicators:**
- root: [Step 3] the job's `tags:` include a value absent from `config.toml`, or the job has no tags and `run_untagged = false`
  <!-- match: {"step": 3, "predicate": "contains", "target": "run_untagged = false"} -->
- s1: [Symptom] stuck message reads `you don't have any active runners online or available with any of these tags`
  <!-- match: {"step": 4, "predicate": "contains", "target": "with any of these tags"} -->
**Interventions:**
- **remediation** (root): align the job tags with an available runner — either add the runner's tag to the job or add the required tag to the runner.

  ```yaml
  # .gitlab-ci.yml — set tags to ones the target runner actually has
  deploy:
    tags: [linux, docker]
    script: ./deploy.sh
  ```

  **Verification:** Re-run Step 3 — the job's tags are a subset of a runner's tags; retry the pipeline and the tagged job is assigned.
- **defensive_fix** (s1): if the job legitimately has no tags, enable untagged execution on the intended runner so future untagged jobs are picked up.

  ```toml
  # /etc/gitlab-runner/config.toml
  [[runners]]
    name = "shared-1"
    run_untagged = true
    tag_list = ["linux", "docker"]
  ```

  **Verification:** `sudo gitlab-runner verify` succeeds after `sudo gitlab-runner restart`; an untagged job is claimed.

### Cause C: Invalid `.gitlab-ci.yml` rules/needs/keyword configuration
**Statement:** The pipeline configuration is invalid — an unknown/misspelled keyword, a leading UTF-8 BOM, or a `needs:` reference to a job that `rules` excluded from the pipeline — so GitLab rejects the config or builds a broken DAG.
**Chain:**
- root: `.gitlab-ci.yml` contains an unknown key, a BOM, or a `needs` target not present in the pipeline
- s1: GitLab fails YAML validation or DAG resolution for the pipeline
- s2: the pipeline is marked invalid, or the dependent job is never added
- D: the pipeline does not run (or the deploy job is missing), so the deployment fails
**Indicators:**
- root: [Step 1] CI Lint fails, or `xxd` shows a leading `efbbbf` BOM; [Step 4] log shows `config contains unknown keys`
  <!-- match: {"step": 4, "predicate": "contains", "target": "unknown keys"} -->
- s2: [Step 4] log shows `job needs ... job, but it was not added to the pipeline`
  <!-- match: {"step": 4, "predicate": "contains", "target": "was not added to the pipeline"} -->
**Interventions:**
- **remediation** (root): fix the keyword/BOM and make the `needs` target reliably present, or mark the dependency optional.

  ```yaml
  # Make a conditionally-present dependency non-fatal
  deploy:
    needs:
      - job: build
        optional: true
    script: ./deploy.sh
  # And correct typos: 'paths' not 'path', 'rules' not 'only/except' mixing
  ```

  **Verification:** Re-run Step 1 — CI Lint "Simulation completed successfully" with no invalid badge; the `needs` error no longer appears in the new pipeline.
- **mitigation** (s2): strip a stray BOM in place to immediately re-enable a config silently dropping jobs.

  ```bash
  sed -i '1s/^\xEF\xBB\xBF//' .gitlab-ci.yml && git commit -am "fix: strip BOM from .gitlab-ci.yml"
  ```

  **Risk:** Only addresses BOM corruption, not logical rule errors. **Duration:** Until the full rules/needs review in the remediation. **Verification:** `xxd` of byte 0 no longer shows `efbbbf`; missing jobs reappear in the pipeline.

### Cause D: Job exceeds the effective timeout, or artifacts exceed the size limit
**Statement:** The job runs but is terminated because its runtime exceeds the lower of the project and runner `maximum_timeout`, or the artifact archive exceeds the instance/project Maximum artifacts size, so the job is force-failed.
**Chain:**
- root: the job's runtime or artifact archive exceeds a configured limit (timeout or max artifact size)
- s1: the runner aborts the job (timeout) or rejects the artifact upload (size)
- D: the job fails and the deployment does not complete
**Indicators:**
- root: [Step 4] effective timeout is lower than the job's real runtime, or the artifact archive is larger than the configured max
- s1: [Step 4] log ends with `Job failed: execution took longer than` (timeout) OR `Uploading artifacts as "archive" to coordinator... too large` (size)
  <!-- match: {"step": 4, "predicate": "contains", "target": "execution took longer than"} -->
**Interventions:**
- **remediation** (root): set an appropriate per-job timeout and shrink/scope artifacts to stay under the limit (raise the limit only if genuinely required).

  ```yaml
  build:
    timeout: 2h            # min 10m, max 1 month; lower of this and runner max wins
    artifacts:
      paths: [dist/]       # narrow paths; avoid node_modules/, build caches
      expire_in: 1 week
      exclude: ["**/*.log"]
  ```

  **Verification:** Re-run Step 4 — effective timeout exceeds the job's measured runtime; the artifact archive size is under Maximum artifacts size (Settings > CI/CD > General pipelines); the job succeeds and uploads artifacts.
- **mitigation** (s1): for a one-off long run, raise the project timeout (Settings > CI/CD > General pipelines > Timeout) or split the slow work into a separate job.

  ```yaml
  # Move the slow stage to its own job so each stays under the cap
  slow_tests:
    stage: test
    timeout: 3h
    script: ./run-slow-tests.sh
  ```

  **Risk:** Longer timeouts let runaway jobs hold a runner slot; GitLab still kills any job inactive for 60 minutes. **Duration:** Until the job is optimized/parallelized. **Verification:** The job finishes within the new cap without the timeout error.

### Cause Z: Unidentified
**Statement:** None of the above root causes is confirmed by the indicators; the failure mechanism is not yet identified.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the GitLab/CI SME.

  ```bash
  {
    echo "== runner status =="; sudo gitlab-runner status; sudo gitlab-runner verify; sudo gitlab-runner list
    echo "== runner config =="; sudo cat /etc/gitlab-runner/config.toml
    echo "== runner logs =="; sudo journalctl --unit=gitlab-runner.service -n 200 --no-pager
    echo "== ci config =="; git show HEAD:.gitlab-ci.yml
  } > gitlab-ci-diagnostics-$(date +%Y%m%d-%H%M%S).txt
  ```

  **Risk:** `config.toml` and job logs may contain tokens/secrets — redact before sharing. **Duration:** N/A. **Verification:** Snapshot attached and escalation opened with the failing pipeline/job URL.

## Prevention

- Validate every change in the Pipeline editor (CI Lint with "Simulate pipeline creation") before merge; use a Schemastore-aware editor for inline schema validation.
- Pin runner versions to match the GitLab major.minor version; monitor runner online status with an alert on the Runners page / via the `/runners` API.
- Standardize runner tags and document which jobs require which tags; keep at least one runner with `run_untagged = true` for untagged jobs.
- Set realistic per-job `timeout:` values and a sensible runner `maximum_timeout`; alert when job duration approaches 80% of the cap.
- Scope `artifacts:paths` tightly with `expire_in` and `exclude`; set Maximum artifacts size deliberately and monitor for `too large` upload failures.
- Mark conditionally-present `needs:` dependencies with `optional: true` to avoid DAG breakage from `rules`-skipped jobs.

## Sources

- [Debugging](https://docs.gitlab.com/ee/ci/debugging.html) — CI Lint, Pipeline editor validation, BOM-corruption behavior, `config contains unknown keys`, rules vs only/except, jobs-not-running checklist.
- [Faq](https://docs.gitlab.com/runner/faq/) — runner-version-must-match-GitLab, `journalctl --unit=gitlab-runner.service`, `gitlab-runner --debug run`, debug-logging security caveat, stuck-job-from-tags behavior.
- [Commands](https://docs.gitlab.com/runner/commands/) — exact `gitlab-runner verify` / `verify --delete` / `list` / `status` / `register` / `run` / `restart` semantics and output, `config.toml` default locations.
- [Configure runners](https://docs.gitlab.com/ci/runners/configure_runners/) — tag matching ("must have all of the tags"), Run untagged jobs, timeout precedence (lower wins), `RUNNER_SCRIPT_TIMEOUT`/`RUNNER_AFTER_SCRIPT_TIMEOUT`, 60-minute inactivity kill, project timeout 10m–1month default 60m.
- [Job troubleshooting](https://docs.gitlab.com/ci/jobs/job_troubleshooting/) — job error strings, multiple-pipelines guidance, manual-job/authorization errors.
- [Job artifacts troubleshooting](https://docs.gitlab.com/ci/jobs/job_artifacts_troubleshooting/) — `No files to upload`, `This job could not start because it could not retrieve the needed artifacts`, dotenv BOM caveat, `artifacts:expire_in` remedy.
- [Needs](https://docs.gitlab.com/ci/yaml/needs/) — `needs ... was not added to the pipeline` error and `optional: true` fix.
- [Caching](https://docs.gitlab.com/ci/caching/) — cache-mismatch troubleshooting, fallback_keys, cache-vs-artifacts overwrite ordering.
