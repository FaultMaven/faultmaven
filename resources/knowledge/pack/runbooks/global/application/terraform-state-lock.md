---
id: "terraform-state-lock"
title: "Terraform State Lock Stuck or Orphaned"
domain: application
service: terraform
symptom_class: [timeout]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
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

Expected output: if `lsof` shows open file handles on the state file, a local zombie process is holding the lock. Note the PID for Cause D resolution.

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

**Mechanism:** Terraform acquires an exclusive lock before every write operation and releases it on exit. When the process is killed abruptly (SIGKILL or runner teardown), the release call never executes, and the lock record persists in the backend indefinitely. Subsequent runs hit `Error acquiring the state lock` until the orphaned entry is removed.

**Indicator:**

- [Step 2] no active Terraform process found on the lock owner host and no CI job is `in_progress`
- [Step 3] lock age exceeds 60 minutes
- [Symptom] `Who` field identifies a CI runner hostname, not a developer workstation

<!-- match: {"step": 2, "predicate": "absent", "target": "terraform"} -->

**Mitigation:**

- **Risk:** Force-unlocking while a legitimate operation is writing can corrupt state. Confirm Steps 2 and 3 before proceeding.
- **Command:**

  ```bash
  terraform force-unlock <lock-id-from-step-1>
  ```

- **Duration:** Seconds to execute; lock is released immediately.

**Resolution:**

```bash
terraform force-unlock <lock-id-from-step-1>
```

- **Impact:** Single workspace; no infrastructure changes, state metadata only.

- **Rollback:** Re-lock is automatic on the next Terraform operation. To restore a corrupted state use `terraform state push <backup.json>`.

**Verification:** Run `terraform plan` and confirm it completes without a lock error. Then run `aws dynamodb scan --table-name terraform-locks --region <region> --select COUNT` and confirm the count is 0.

---

### Cause B: Concurrent parallel Terraform runs against the same workspace

**Statement:** Multiple CI/CD jobs or developers triggered Terraform operations against the same state simultaneously, causing lock contention rather than an orphaned lock.

**Mechanism:** Terraform backends grant locks exclusively; a second `terraform apply` will block or immediately fail if the first run has not yet released the lock. In CI systems without concurrency controls, parallel pipeline triggers (e.g., two pushes in quick succession, matrix builds, or manual re-runs) produce simultaneous lock attempts. The second run either hangs until `-lock-timeout` expires or fails immediately.

**Indicator:**

- [Step 2] an active Terraform process or an `in_progress` CI job is found for the lock owner
- [Symptom] error appears during a period of high commit/deployment activity
- [Step 3] lock age is recent (under 30 minutes)

<!-- match: {"step": 2, "predicate": "contains", "target": "terraform"} -->

**Mitigation:**

- **Risk:** None — wait for the active operation to finish; do not force-unlock.
- **Command:**

  ```bash
  terraform plan -lock-timeout=10m
  ```

- **Duration:** Up to the specified timeout plus the plan/apply execution time; typically 1–30 minutes.

**Resolution:**

Add concurrency controls to the CI/CD pipeline to serialize Terraform operations:

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

- **Impact:** Pipeline-level change; affects all future runs in that repo.

- **Rollback:** Remove the `concurrency` / `resource_group` key from the pipeline config.

**Verification:** Trigger two pipeline runs simultaneously and confirm the second queues behind the first rather than failing with a lock error.

---

### Cause C: Missing or misconfigured DynamoDB lock table

**Statement:** The DynamoDB table configured as the Terraform state lock backend does not exist or has an incorrect key schema, causing every Terraform operation to fail before acquiring a lock.

**Mechanism:** The S3 backend requires a DynamoDB table with a `LockID` hash key of type String. If the table was never created, was deleted, or was created with a different key name, Terraform cannot write or read lock entries and raises `ResourceNotFoundException` or `ConditionalCheckFailedException`. This blocks all Terraform operations against the workspace, not just concurrent ones.

**Indicator:**

- [Step 6] `ResourceNotFoundException` returned by `aws dynamodb describe-table`
- [Symptom] error message contains `ResourceNotFoundException` or `Backend initialization required`

<!-- match: {"step": 6, "predicate": "absent", "target": "ACTIVE"} -->

**Mitigation:**

- **Risk:** Low — creates a new table; does not modify existing state files.
- **Command:**

  ```bash
  aws dynamodb create-table \
    --table-name terraform-locks \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region <region>
  ```

- **Duration:** Table becomes `ACTIVE` within 10–30 seconds.

**Resolution:** Same as Mitigation. Then verify the `dynamodb_table` field in the backend block matches the table name exactly:

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

**Verification:** Run `aws dynamodb describe-table --table-name terraform-locks --region <region> --query "Table.TableStatus"` and confirm `"ACTIVE"`. Then run `terraform plan` and confirm it completes without a lock or initialization error.

---

### Cause D: Zombie Terraform process on local machine holding the lock

**Statement:** A previously interrupted Terraform process on the local machine is still alive with open file descriptors on the state file, preventing the lock from being released even after the terminal session appears idle.

**Mechanism:** When a Terraform process is suspended (Ctrl-Z), backgrounded, or left in a frozen shell, it retains the state lock without making progress. The process appears in `ps` output but is not associated with any active terminal. `lsof` shows open handles on the state file. `terraform force-unlock` will succeed at removing the backend lock record, but if the zombie process resumes, it may attempt to write with a stale lock, corrupting state.

**Indicator:**

- [Step 5] `lsof` shows open file handles on `terraform.tfstate`
- [Step 2] `ps aux` finds a Terraform process on the local machine with no associated terminal
- [Step 1] `Who` field contains the local machine hostname

<!-- match: {"step": 5, "predicate": "contains", "target": "terraform.tfstate"} -->

**Mitigation:**

- **Risk:** Medium — killing a process mid-apply can leave partial state. Take a state backup first.
- **Command:**

  ```bash
  terraform state pull > state-backup-$(date +%Y%m%d%H%M%S).json
  kill <pid-from-step-5>
  terraform force-unlock <lock-id-from-step-1>
  ```

- **Duration:** Under 1 minute.

**Resolution:** Same as Mitigation. After killing the zombie, verify the backup matches expectations before re-running Terraform.

**Verification:** Run `terraform plan` to confirm no lock error. Run `terraform state list | wc -l` and compare the resource count against the backup to confirm no resources were lost.

---

### Cause E: Backend lock release failed after a successful apply

**Statement:** The Terraform apply completed successfully but a transient backend error prevented the state lock from being released, leaving an orphaned lock entry despite the infrastructure changes being applied.

**Mechanism:** After writing the new state, Terraform makes a second call to delete the lock record from the backend. If this call fails due to a network blip, rate limit, or backend timeout, the lock persists even though the apply is fully recorded in the state file. The next `terraform plan` or `apply` hits `Error acquiring the state lock` but the state itself is intact and consistent.

**Indicator:**

- [Symptom] `Error: Error releasing the state lock` appears in the prior run's output immediately after `Apply complete!`
- [Step 4] lock entry exists in the backend but its `Operation` field shows `OperationTypeApply`
- [Step 3] lock age matches the time of the last successful apply

<!-- match: {"step": 4, "predicate": "contains", "target": "OperationTypeApply"} -->

**Mitigation:**

- **Risk:** Low — the state is already consistent; force-unlock simply removes a stale record.
- **Command:**

  ```bash
  terraform force-unlock <lock-id-from-step-1>
  ```

- **Duration:** Seconds.

**Resolution:** Same as Mitigation. After unlocking, confirm state integrity:

```bash
terraform state list
terraform show -json | jq '.terraform_version, .format_version'
```

**Verification:** Run `terraform plan` and confirm it reports no changes (since the apply already completed) and no lock error. Confirm `aws dynamodb scan --table-name terraform-locks --region <region> --select COUNT` returns 0.

---

### Cause Z: Unidentified lock failure

**Statement:** The state lock failure does not match any of the recognized patterns above. [Default]

**Mechanism:** Terraform state lock failures can originate from backend-specific bugs, IAM permission changes mid-operation, network partitions affecting only the lock table, or unsupported backend configurations. These cases require manual investigation of the specific backend's access logs and Terraform debug output.

**Indicator:**

- [Default] none of Causes A–E indicators match the observed evidence

**Mitigation:**

- **Risk:** Low — gathering debug information only, no state changes.
- **Command:**

  ```bash
  TF_LOG=DEBUG terraform plan 2>&1 | tee terraform-debug.log
  grep -i "lock\|error\|fatal" terraform-debug.log
  ```

- **Duration:** 5–10 minutes for log collection.

**Resolution:** Out of runbook scope. Escalate to the team with the debug log and the full `Lock Info` block. For Terraform Cloud/Enterprise, open a support ticket with the workspace ID and the lock timestamp.

**Verification:** Resolution depends on the escalation outcome. Confirm with `terraform plan` once the underlying issue is addressed.

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
- [Terraform Language: S3 Backend](https://developer.hashicorp.com/terraform/language/settings/backends/s3) — Priority 1. DynamoDB lock table schema requirements, IAM permissions, and backend configuration fields (`dynamodb_table`, `encrypt`, etc.).
- [Terraform Language: Backend Configuration](https://developer.hashicorp.com/terraform/language/settings/backends/configuration) — Priority 1. General backend initialization, reconfiguration, and locking support across all backends.
