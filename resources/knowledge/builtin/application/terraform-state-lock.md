---
id: terraform-state-lock
title: "Terraform State Lock"
domain: application
service: terraform
symptom_class:
  - timeout
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: "kb-researcher"
status: draft
tags:
  - terraform
  - state
  - lock
  - iac
  - infrastructure-as-code
  - dynamodb
difficulty: intermediate
---

# Terraform State Lock

## Problem Definition

Terraform acquires an exclusive lock on the state file before any write operation (`plan`, `apply`, `destroy`, `import`, `state mv`, `state rm`). When the lock cannot be acquired, Terraform blocks or fails immediately, preventing all infrastructure operations against that state. This runbook applies to Terraform v1.0 through v1.11+ (including OpenTofu v1.6+) using any backend that supports locking: S3+DynamoDB, Azure Blob Storage, GCS, Consul, Terraform Cloud/Enterprise, or local filesystem. Diagnosing lock issues requires shell access to the Terraform working directory, credentials for the state backend, and (for force-unlock) write access to the lock storage.

Lock failures manifest with the error `Error acquiring the state lock` followed by a `Lock Info` block. Common symptoms include:

- `ConditionalCheckFailedException: The conditional request failed` when using S3+DynamoDB backend and another process holds the lock
- `Error: Error locking state: Error acquiring the state lock` with a lock ID, path, operation type, owner identity (`Who`), Terraform version, and creation timestamp
- Terraform commands hanging indefinitely when `-lock-timeout` is not set and another operation holds the lock
- CI/CD pipelines timing out because a previous pipeline run was killed before releasing the lock
- `Error: Error releasing the state lock` after a successful operation, leaving an orphaned lock for the next run
- `Error: Backend initialization required: please run "terraform init"` when the DynamoDB lock table does not exist or is misconfigured

The `Lock Info` block in the error output contains fields critical for diagnosis: `ID` (the lock identifier needed for force-unlock), `Who` (the user and hostname that acquired the lock), `Operation` (the Terraform command holding the lock), and `Created` (when the lock was acquired).

## Diagnostic Steps

### Step 1. Record the Lock Info Fields

Extract the lock metadata from the error message. Every subsequent diagnostic and mitigation step depends on these values.

```bash
terraform plan 2>&1 | grep -A 10 "Lock Info:"
```

Record the `ID`, `Who`, `Operation`, and `Created` fields. The `ID` is required for force-unlock. The `Who` field identifies the machine and user that holds the lock. The `Created` timestamp determines whether the lock is likely orphaned.

### Step 2. Determine If the Lock Is Legitimate

Check whether the process identified in `Who` is still actively running a Terraform operation. A legitimate lock should not be force-unlocked.

```bash
# If the lock owner is a remote machine
ssh <hostname-from-who> "ps aux | grep '[t]erraform'"

# If the lock owner is a CI/CD pipeline
# GitHub Actions
gh run list --workflow=terraform.yml --status=in_progress

# GitLab CI
# Check the pipeline page for running jobs in the Terraform stage
```

If the command returns an active Terraform process or the CI pipeline shows a running job, the lock is legitimate. Wait for the operation to complete. If no Terraform process is running and no CI job is active, the lock is orphaned.

### Step 3. Calculate Lock Age

Compare the `Created` timestamp from Step 1 against the current time. Locks older than the maximum expected operation duration are almost certainly orphaned.

```bash
# Convert the lock creation time to epoch seconds
lock_created=$(date -d "2026-03-26 10:30:00 UTC" +%s)
now=$(date +%s)
age_minutes=$(( (now - lock_created) / 60 ))
echo "Lock age: ${age_minutes} minutes"
```

Most Terraform operations complete within 30 minutes. A lock aged over 60 minutes with no active process (confirmed in Step 2) is orphaned. Very large infrastructures (hundreds of resources) may take longer; adjust the threshold based on your environment.

### Step 4. Inspect the Backend Lock Entry Directly

Query the lock storage backend to confirm the lock exists and inspect its contents. This is useful when the Terraform error message is incomplete or when `terraform force-unlock` itself fails.

For S3+DynamoDB backend:

```bash
aws dynamodb get-item \
  --table-name terraform-locks \
  --key '{"LockID": {"S": "<state-path>/terraform.tfstate"}}' \
  --region <region>
```

For Azure Blob backend:

```bash
az storage blob show \
  --container-name tfstate \
  --name <state-blob-name> \
  --account-name <storage-account> \
  --query "properties.lease"
```

For GCS backend:

```bash
gsutil stat gs://<bucket>/<state-path>
```

If the DynamoDB item exists, the lock is held. If the Azure blob has an active lease, the lock is held. An empty response or missing item means the lock was already released and the error may be transient.

### Step 5. Check for Zombie Terraform Processes Locally

If the `Who` field points to the current machine, check for orphaned Terraform processes that may still hold file descriptors.

```bash
ps aux | grep '[t]erraform'
lsof 2>/dev/null | grep terraform.tfstate
```

If `ps` returns Terraform processes that are not part of any active terminal session or CI job, they are zombies. If `lsof` shows file handles on the state file, a local process is still holding the lock. Kill the zombie process before force-unlocking.

## Mitigation

### Option 1. Wait with Lock Timeout

If the lock is legitimate (another operation is in progress), retry with a timeout instead of failing immediately.

- **Risk**: None. This queues the operation behind the active one. No state corruption risk.
- **Command**:
  ```bash
  terraform plan -lock-timeout=10m
  ```
- **Verify**:
  ```bash
  terraform plan
  ```
  The plan should succeed without a lock error once the previous operation releases the lock.
- **Duration**: Up to the specified timeout plus the plan execution time. Typically 1-30 minutes.

### Option 2. Force-Unlock the State

If the lock is orphaned (confirmed by Steps 2-3), release it using the lock ID from the error message.

- **Risk**: High if the lock is legitimate. Force-unlocking while another operation is actively writing to the state can corrupt the state file. Confirm the locking process is dead before proceeding.
- **Command**:
  ```bash
  terraform force-unlock <lock-id>
  ```
  Terraform prompts for confirmation. Type `yes` to proceed.
- **Verify**:
  ```bash
  terraform plan
  ```
  The plan should succeed without a lock error. Review the plan output to confirm no state corruption.
- **Duration**: Seconds.

### Option 3. Remove the Lock Directly from the Backend

If `terraform force-unlock` itself fails (for example, Terraform cannot initialize or the provider plugin is broken), remove the lock entry directly from the backend storage.

- **Risk**: High. Same corruption risk as Option 2. Additionally, bypassing Terraform's lock management means Terraform does not verify the lock ID matches, so ensure no other operation is running.
- **Command**:

  For S3+DynamoDB:
  ```bash
  aws dynamodb delete-item \
    --table-name terraform-locks \
    --key '{"LockID": {"S": "<state-path>/terraform.tfstate"}}' \
    --region <region>
  ```

  For Azure Blob (break the lease):
  ```bash
  az storage blob lease break \
    --container-name tfstate \
    --blob-name <state-blob-name> \
    --account-name <storage-account>
  ```

  For Terraform Cloud/Enterprise:
  ```bash
  curl -s \
    --header "Authorization: Bearer $TFC_TOKEN" \
    --header "Content-Type: application/vnd.api+json" \
    --request POST \
    "https://app.terraform.io/api/v2/workspaces/<workspace-id>/actions/unlock"
  ```
- **Verify**:
  ```bash
  terraform plan
  ```
  The plan should succeed. Immediately run `terraform state list` to confirm state integrity.
- **Duration**: Seconds.

### Option 4. Kill the Zombie Process and Release

If a local zombie Terraform process is holding the lock (identified in Step 5), kill it first, then force-unlock.

- **Risk**: Medium. Killing a Terraform process mid-operation can leave partial state. The force-unlock afterward restores the ability to run new operations, but a state backup should be taken first.
- **Command**:
  ```bash
  # Back up the state first
  terraform state pull > state-backup-$(date +%Y%m%d%H%M%S).json

  # Kill the zombie process
  kill <pid>

  # Force-unlock
  terraform force-unlock <lock-id>
  ```
- **Verify**:
  ```bash
  terraform plan
  terraform state list | wc -l
  ```
  Compare the resource count against the backup to confirm no resources were lost.
- **Duration**: Under 1 minute.

## Root Cause Resolution

**If** the lock was orphaned because a CI pipeline was killed mid-operation (runner timeout, manual cancellation, spot instance termination), configure pipeline-level timeouts that exceed the expected Terraform operation duration and add graceful shutdown handling. For GitHub Actions, use `concurrency` groups to prevent parallel runs and `timeout-minutes` to set predictable limits:

```yaml
concurrency:
  group: terraform-${{ github.ref }}
  cancel-in-progress: false

jobs:
  terraform:
    timeout-minutes: 30
```

**If** multiple CI pipelines run Terraform against the same state simultaneously, causing lock contention, serialize all Terraform operations. For GitHub Actions, use the `concurrency` group shown above. For GitLab CI, use `resource_group: terraform-<env>`. For Jenkins, use `lock(resource: 'terraform-production')` or `disableConcurrentBuilds()`.

**If** the DynamoDB lock table does not exist (error message contains `ResourceNotFoundException`), create it with the correct schema:

```bash
aws dynamodb create-table \
  --table-name terraform-locks \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region <region>
```

Then confirm the backend configuration references this table name in the `dynamodb_table` field.

**If** the backend configuration references the wrong table name, bucket, or container, fix the backend block to match the actual infrastructure:

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

**If** the lock is on a Terraform Cloud/Enterprise workspace and the locking run is no longer active, unlock it via the Terraform Cloud UI (Settings > Locking > Unlock) or via the API as shown in Mitigation Option 3.

**If** the lock release fails after a successful apply (the operation completed but the lock was not released due to a transient backend error), force-unlock the state and verify the apply was recorded correctly by running `terraform state list` and comparing against the expected resource count.

## Verification

After resolving the lock issue, confirm that Terraform operations work correctly and the state is intact.

1. Run a plan to confirm no lock errors remain:

```bash
terraform plan
```

The plan should complete without any lock-related errors. Review the output for unexpected changes that might indicate state corruption.

2. Verify state integrity by listing all managed resources and checking the state version:

```bash
terraform state list
terraform show -json | jq '.terraform_version, .format_version'
```

The resource list should match expectations. The Terraform version should match your installed version.

3. Confirm no orphaned lock entries remain in the backend:

```bash
# S3+DynamoDB
aws dynamodb scan \
  --table-name terraform-locks \
  --region <region> \
  --select COUNT
```

The item count should be 0 when no Terraform operations are actively running.

4. Run a no-op apply to confirm the full write path works:

```bash
terraform apply -auto-approve
```

If no changes are pending, this should report `Apply complete! Resources: 0 added, 0 changed, 0 destroyed.`

## Prevention

- **Serialize Terraform operations in CI/CD.** Never run parallel Terraform operations against the same state file. Use GitHub Actions `concurrency` groups, GitLab CI `resource_group`, Jenkins `disableConcurrentBuilds()`, or Terraform Cloud workspace queuing.

- **Set `-lock-timeout` on all CI Terraform commands.** This handles brief lock contention from near-simultaneous pipeline triggers gracefully rather than failing immediately:

```bash
terraform plan -lock-timeout=5m
terraform apply -lock-timeout=5m -auto-approve
```

- **Always use remote state with locking enabled.** Local state files offer weaker locking guarantees and no protection across machines. Configure S3+DynamoDB, Azure Blob, GCS, or Terraform Cloud as the backend.

- **Set pipeline timeouts that exceed expected operation duration.** If a Terraform apply typically takes 15 minutes, set the pipeline timeout to at least 30 minutes. This prevents premature kills that leave orphaned locks.

- **Enable state file versioning for corruption recovery.** Enable versioning on the S3 bucket, Azure Blob container, or GCS bucket so that a corrupted state can be recovered from a previous version:

```bash
aws s3api put-bucket-versioning \
  --bucket my-terraform-state \
  --versioning-configuration Status=Enabled
```

- **Monitor for stale locks.** Create a scheduled job (cron, CloudWatch Events, GitHub Actions schedule) that scans the DynamoDB lock table for entries older than a threshold (e.g., 1 hour) and alerts the team.

- **Document the force-unlock procedure.** Ensure all team members know how to identify orphaned locks and safely force-unlock. Include the procedure in your team's incident runbook and the CI/CD pipeline documentation.

## Sources

- [Terraform Language: State Locking](https://developer.hashicorp.com/terraform/language/state/locking) -- Official documentation on lock behavior, backend support matrix, and lock timeout configuration.
- [Terraform CLI: force-unlock Command](https://developer.hashicorp.com/terraform/cli/commands/force-unlock) -- Syntax, usage, and safety warnings for force-unlocking state.
- [Terraform Language: S3 Backend](https://developer.hashicorp.com/terraform/language/settings/backends/s3) -- S3 backend configuration including DynamoDB lock table setup and IAM permissions.
- [Terraform Language: Backend Configuration](https://developer.hashicorp.com/terraform/language/settings/backends/configuration) -- General backend setup for all supported backends with locking details.
- [AWS DynamoDB: Working with Tables](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithTables.html) -- DynamoDB table creation, schema, and billing modes for Terraform lock tables.
- [Terraform Cloud: Workspaces Locking](https://developer.hashicorp.com/terraform/cloud-docs/workspaces/settings#locking) -- Workspace lock management in Terraform Cloud/Enterprise.
