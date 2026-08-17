---
id: "terraform-state-lock"
title: "Terraform State Lock Stuck or Orphaned"
domain: application
service: terraform
symptom_class: [timeout]
severity: high
scope: global
version: "2.0.1"
last_updated: "2026-08-17"
verified_by: "kb-researcher"
status: draft
tags: [terraform, state, lock, iac, infrastructure-as-code, dynamodb, s3-backend, terraform-cloud]
difficulty: intermediate
---

## Symptom Recognition

- `Error acquiring the state lock` with a `Lock Info` block containing `ID`, `Who`, `Operation`, `Created` fields
- `ConditionalCheckFailedException: The conditional request failed` during S3+DynamoDB backend lock acquisition
- Terraform commands hang indefinitely when `-lock-timeout` is not configured and another operation holds the lock
- CI/CD pipelines time out because a prior run was killed before releasing the lock
- `Error: Error releasing the state lock` after a completed operation, leaving an orphaned entry for the next run
- `ResourceNotFoundException` when the DynamoDB lock table does not exist
- `Error: Backend initialization required: please run "terraform init"` when the lock table is misconfigured

## Applicability

- Terraform v1.0 through v1.11+ and OpenTofu v1.6+
- Any backend that supports locking: S3+DynamoDB, Azure Blob Storage, GCS, Consul, Terraform Cloud/Enterprise, or local filesystem
- Requires shell access to the Terraform working directory, credentials for the state backend, and (for force-unlock) write access to the lock storage

## Diagnostic Steps

### Step 1: Capture the Lock Info block

Record the full lock metadata from the error output. All downstream steps depend on these values.

```bash
terraform plan 2>&1 | grep -A 10 "Lock Info:"
```

Expected output: a block with `ID`, `Who`, `Operation`, `Created` fields. Note the `ID` value (needed for force-unlock) and the `Who` field (hostname/user that holds the lock).

### Step 2: Check whether the locking process is still alive

Verify that the process identified in `Who` is actively running a Terraform operation before considering force-unlock.

```bash
# Remote machine (SSH to hostname from Who field)
ssh <hostname-from-who> "ps aux | grep '[t]erraform'"

# GitHub Actions — check in_progress runs
gh run list --workflow=terraform.yml --status=in_progress

# Local machine fallback
ps aux | grep '[t]erraform'
```

Expected output: if any Terraform process is listed or a CI job shows `in_progress`, the lock is legitimate — do not force-unlock. An empty result means the lock is orphaned.

### Step 3: Calculate lock age

Compare the `Created` timestamp from Step 1 to now. Locks older than 60 minutes with no active process are almost certainly orphaned.

```bash
lock_created=$(date -d "2026-05-12 10:30:00 UTC" +%s)
now=$(date +%s)
age_minutes=$(( (now - lock_created) / 60 ))
echo "Lock age: ${age_minutes} minutes"
```

Expected output: `Lock age: N minutes`. Values above 60 with no active process (Step 2) confirm an orphaned lock.

### Step 4: Inspect the backend lock entry directly

Query the lock storage to confirm the entry exists and retrieve its raw content. Useful when the Terraform error is incomplete or `force-unlock` itself fails to initialize.

```bash
# S3+DynamoDB
aws dynamodb get-item \
  --table-name terraform-locks \
  --key '{"LockID": {"S": "<state-path>/terraform.tfstate"}}' \
  --region <region>

# Azure Blob
az storage blob show \
  --container-name tfstate \
  --name <state-blob-name> \
  --account-name <storage-account> \
  --query "properties.lease"

# GCS
gsutil stat gs://<bucket>/<state-path>
```

Expected output: for DynamoDB, a JSON item with lock metadata if the lock is held; an empty response if already released. For Azure, a `leaseState: leased` value if held. An empty/missing result means the lock is already gone and the error may be transient.

### Step 5: Check for zombie Terraform processes on the local machine

If `Who` in Step 1 points to the current machine, look for orphaned Terraform processes holding file descriptors on the state.

```bash
ps aux | grep '[t]erraform'
lsof 2>/dev/null | grep terraform.tfstate
```

Expected output: if `lsof` shows open file handles on the state file, a local zombie process is holding the lock. Note the PID for resolution.

### Step 6: Check whether the DynamoDB lock table exists

Run this step only when the error message contains `ResourceNotFoundException` or `Backend initialization required`.

```bash
aws dynamodb describe-table \
  --table-name terraform-locks \
  --region <region> \
  --query "Table.TableStatus"
```

Expected output: `"ACTIVE"` if the table exists and is ready. A `ResourceNotFoundException` error means the table is absent.

## Causes

### Cause A: Orphaned lock from a killed CI/CD pipeline run

**Statement:** A prior CI/CD pipeline run was terminated (runner timeout, spot instance preemption, manual cancellation) before Terraform could release the state lock, leaving an abandoned lock entry in the backend.

**Chain:**
- root: a CI/CD pipeline run holding the exclusive state lock is killed abruptly (SIGKILL or runner teardown)
- s1: the lock-release call never executes, so the lock record persists in the backend indefinitely
- D: subsequent runs hit `Error acquiring the state lock` until the orphaned entry is removed (points at Symptom Recognition)

**Indicators:**
- root: [Symptom] `Who` field identifies a CI runner hostname, not a developer workstation
- s1: [Step 2] no active Terraform process found on the lock owner host and no CI job is `in_progress`
- s1: [Step 3] lock age exceeds 60 minutes

**Interventions:**
- **remediation** (root): force-unlock the orphaned lock record once Steps 2 and 3 confirm no active operation. The state metadata is the only thing modified; re-lock is automatic on the next operation, and a corrupted state can be restored with `terraform state push <backup.json>`.

  ```bash
  terraform force-unlock <lock-id-from-step-1>
  ```

  **Verification:** run `terraform plan` and confirm it completes without a lock error, then run `aws dynamodb scan --table-name terraform-locks --region <region> --select COUNT` and confirm the count is 0.
- **mitigation** (s1): clear the stale lock entry immediately to unblock the pipeline.

  ```bash
  terraform force-unlock <lock-id-from-step-1>
  ```

  **Risk:** force-unlocking while a legitimate operation is writing can corrupt state — confirm Steps 2 and 3 first. **Duration:** seconds to execute; lock is released immediately. **Verification:** re-run `terraform plan` and confirm no lock error.

---

### Cause B: Concurrent parallel Terraform runs against the same workspace

**Statement:** Multiple CI/CD jobs or developers triggered Terraform operations against the same state simultaneously, causing live lock contention rather than an orphaned lock.

**Chain:**
- root: two or more Terraform operations are triggered against the same state at once (parallel pipeline triggers, matrix builds, manual re-runs)
- s1: the backend grants the lock exclusively, so the second operation blocks until `-lock-timeout` expires or fails immediately
- D: the second run reports `Error acquiring the state lock` while the first run still holds it (points at Symptom Recognition)

**Indicators:**
- root: [Symptom] error appears during a period of high commit/deployment activity
- s1: [Step 2] an active Terraform process or an `in_progress` CI job is found for the lock owner
- s1: [Step 3] lock age is recent (under 30 minutes)

**Interventions:**
- **remediation** (root): add concurrency controls to the CI/CD pipeline to serialize Terraform operations so parallel triggers can never collide. Roll back by removing the `concurrency` / `resource_group` key.

  ```yaml
  # GitHub Actions
  concurrency:
    group: terraform-${{ github.ref }}
    cancel-in-progress: false
  ```

  ```yaml
  # GitLab CI
  resource_group: terraform-production
  ```

  **Verification:** trigger two pipeline runs simultaneously and confirm the second queues behind the first rather than failing with a lock error.
- **mitigation** (s1): wait out the active operation by extending the lock timeout instead of force-unlocking.

  ```bash
  terraform plan -lock-timeout=10m
  ```

  **Risk:** none — wait for the active operation to finish; do not force-unlock. **Duration:** up to the specified timeout plus plan/apply execution time; typically 1–30 minutes. **Verification:** confirm the command acquires the lock and completes once the first operation releases it.

---

### Cause C: Missing or misconfigured DynamoDB lock table

**Statement:** The DynamoDB table configured as the Terraform state lock backend does not exist or has an incorrect key schema, causing every Terraform operation to fail before acquiring a lock.

**Chain:**
- root: the S3 backend's DynamoDB lock table is absent, was deleted, or was created with a key other than a `LockID` String hash key
- s1: Terraform cannot read or write lock entries, raising `ResourceNotFoundException` or `ConditionalCheckFailedException`
- D: every Terraform operation against the workspace fails before acquiring a lock (points at Symptom Recognition)

**Indicators:**
- root: [Step 6] `ResourceNotFoundException` returned by `aws dynamodb describe-table`
- s1: [Symptom] error message contains `ResourceNotFoundException` or `Backend initialization required`

**Interventions:**
- **remediation** (root): create the lock table with the correct `LockID` hash key, confirm the backend block's `dynamodb_table` matches exactly, then re-initialize.

  ```bash
  aws dynamodb create-table \
    --table-name terraform-locks \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region <region>
  ```

  ```hcl
  terraform {
    backend "s3" {
      bucket         = "my-terraform-state"
      key            = "production/terraform.tfstate"
      region         = "us-east-1"
      dynamodb_table = "terraform-locks"
      encrypt        = true
    }
  }
  ```

  Run `terraform init -reconfigure` after fixing the backend block.

  **Verification:** run `aws dynamodb describe-table --table-name terraform-locks --region <region> --query "Table.TableStatus"` and confirm `"ACTIVE"`, then run `terraform plan` and confirm it completes without a lock or initialization error.
- **mitigation** (root): create the table immediately to unblock operations while the backend block is being corrected.

  ```bash
  aws dynamodb create-table \
    --table-name terraform-locks \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region <region>
  ```

  **Risk:** low — creates a new table; does not modify existing state files. **Duration:** table becomes `ACTIVE` within 10–30 seconds. **Verification:** `aws dynamodb describe-table --table-name terraform-locks --region <region> --query "Table.TableStatus"` returns `"ACTIVE"`.

---

### Cause D: Zombie Terraform process on local machine holding the lock

**Statement:** A previously interrupted Terraform process on the local machine is still alive with open file descriptors on the state file, preventing the lock from being released even after the terminal session appears idle.

**Chain:**
- root: a Terraform process is suspended (Ctrl-Z), backgrounded, or left in a frozen shell, retaining the state lock without making progress
- s1: the process holds open file handles on the state file and keeps the backend lock record, while appearing detached from any active terminal
- D: new operations fail with `Error acquiring the state lock` despite an apparently idle session (points at Symptom Recognition)

**Indicators:**
- root: [Step 1] `Who` field contains the local machine hostname
- s1: [Step 5] `lsof` shows open file handles on `terraform.tfstate`
- s1: [Step 2] `ps aux` finds a Terraform process on the local machine with no associated terminal

**Interventions:**
- **remediation** (root): back up the state, kill the zombie process, then force-unlock. Verify the backup matches expectations before re-running Terraform, because a resumed zombie could write with a stale lock and corrupt state.

  ```bash
  terraform state pull > state-backup-$(date +%Y%m%d%H%M%S).json
  kill <pid-from-step-5>
  terraform force-unlock <lock-id-from-step-1>
  ```

  **Verification:** run `terraform plan` to confirm no lock error, then run `terraform state list | wc -l` and compare the resource count against the backup to confirm no resources were lost.
- **mitigation** (s1): take a state backup before killing the process to bound the blast radius of a mid-apply interruption.

  ```bash
  terraform state pull > state-backup-$(date +%Y%m%d%H%M%S).json
  kill <pid-from-step-5>
  terraform force-unlock <lock-id-from-step-1>
  ```

  **Risk:** medium — killing a process mid-apply can leave partial state; take the state backup first. **Duration:** under 1 minute. **Verification:** `terraform plan` reports no lock error and `terraform state list` matches the backup.

---

### Cause E: Backend lock release failed after a successful apply

**Statement:** The Terraform apply completed successfully but a transient backend error prevented the state lock from being released, leaving an orphaned lock entry despite the infrastructure changes being applied.

**Chain:**
- root: after writing the new state, Terraform's second call to delete the lock record fails (network blip, rate limit, or backend timeout)
- s1: the lock record persists even though the apply is fully recorded and the state is intact and consistent
- D: the next `terraform plan` or `apply` hits `Error acquiring the state lock` (points at Symptom Recognition)

**Indicators:**
- root: [Symptom] `Error: Error releasing the state lock` appears in the prior run's output immediately after `Apply complete!`
- s1: [Step 4] lock entry exists in the backend but its `Operation` field shows `OperationTypeApply`
- s1: [Step 3] lock age matches the time of the last successful apply

**Interventions:**
- **remediation** (root): force-unlock the stale record, then confirm state integrity. The state is already consistent, so this only removes the leftover lock.

  ```bash
  terraform force-unlock <lock-id-from-step-1>
  ```

  ```bash
  terraform state list
  terraform show -json | jq '.terraform_version, .format_version'
  ```

  **Verification:** run `terraform plan` and confirm it reports no changes (since the apply already completed) and no lock error, and confirm `aws dynamodb scan --table-name terraform-locks --region <region> --select COUNT` returns 0.
- **mitigation** (root): remove the stale lock record immediately to unblock the next run.

  ```bash
  terraform force-unlock <lock-id-from-step-1>
  ```

  **Risk:** low — the state is already consistent; force-unlock simply removes a stale record. **Duration:** seconds. **Verification:** `terraform plan` completes without a lock error.

---

### Cause Z: Unidentified

**Statement:** The state lock failure does not match any of the recognized patterns above and originates from a backend-specific bug, IAM permission change mid-operation, network partition affecting only the lock table, or unsupported backend configuration.

**Chain:**
- root: an unrecognized backend, permission, or network condition prevents normal lock acquisition or release
- D: Terraform reports a state lock failure that matches none of Causes A–E (points at Symptom Recognition)

**Indicators:**
- root: [Default] none of Causes A–E indicators match the observed evidence

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the SME. For Terraform Cloud/Enterprise, open a support ticket with the workspace ID and the lock timestamp.

  ```bash
  TF_LOG=DEBUG terraform plan 2>&1 | tee terraform-debug.log
  grep -i "lock\|error\|fatal" terraform-debug.log
  ```

  **Risk:** low — gathering debug information only, no state changes. **Duration:** 5–10 minutes for log collection. **Verification:** confirm the debug log and full `Lock Info` block are attached to the escalation; re-run `terraform plan` once the underlying issue is addressed.

## Prevention

- **Serialize Terraform operations in CI/CD.** Never allow parallel runs against the same state. Use GitHub Actions `concurrency` groups, GitLab CI `resource_group`, Jenkins `disableConcurrentBuilds()`, or Terraform Cloud workspace queuing.

- **Set `-lock-timeout` on all CI Terraform commands** to handle brief contention from near-simultaneous pipeline triggers:

  ```bash
  terraform plan -lock-timeout=5m
  terraform apply -lock-timeout=5m -auto-approve
  ```

- **Set pipeline timeouts that exceed expected operation duration.** If a Terraform apply typically takes 15 minutes, set the CI job timeout to at least 30 minutes to prevent premature kills that leave orphaned locks.

- **Enable state file versioning for corruption recovery:**

  ```bash
  aws s3api put-bucket-versioning \
    --bucket my-terraform-state \
    --versioning-configuration Status=Enabled
  ```

- **Monitor for stale lock entries.** Create a scheduled job that scans the DynamoDB table for items older than 1 hour and alerts the team:

  ```bash
  aws dynamodb scan \
    --table-name terraform-locks \
    --region <region> \
    --select COUNT
  ```

- **Always use remote state with locking enabled.** Local state files offer no cross-machine protection. Configure S3+DynamoDB, Azure Blob, GCS, or Terraform Cloud as the backend.

## Sources

- [Terraform Language: State Locking](https://developer.hashicorp.com/terraform/language/state/locking) — Priority 1. Official documentation on lock behavior, backend support matrix, lock timeout configuration, and `Lock Info` field descriptions.
- [Terraform CLI: force-unlock Command](https://developer.hashicorp.com/terraform/cli/commands/force-unlock) — Priority 1. Syntax, flags, usage, safety warnings, and when force-unlock is appropriate.
- [Terraform Language: S3 Backend](https://developer.hashicorp.com/terraform/language/backend/s3) — Priority 1. DynamoDB lock table schema requirements (`LockID` string partition key), IAM/DynamoDB permissions, and backend configuration fields (`dynamodb_table`, `encrypt`, etc.).
- [Terraform Language: Backend Configuration](https://developer.hashicorp.com/terraform/language/backend) — Priority 1. General backend initialization, reconfiguration, and locking support across all backends.
