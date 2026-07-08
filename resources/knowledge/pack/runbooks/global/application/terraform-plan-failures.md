---
id: "terraform-plan-failures"
title: "Terraform Plan Failures"
domain: application
service: terraform
symptom_class: [auth_failure, deployment_failure]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags: [terraform, plan, iac, infrastructure-as-code, provider, state-drift]
difficulty: intermediate
---

## Symptom Recognition

- `Error: error configuring Terraform AWS Provider: no valid credential sources found` — provider credentials absent or expired
- `Error: Error refreshing state: resource "aws_instance.web" not found` — resource deleted outside Terraform (state drift)
- `Error: Cycle: aws_security_group.a, aws_security_group.b` — circular dependency in the resource graph
- `Error: Unsupported argument: An argument named "enable_dns" is not expected here` — provider upgrade renamed or removed an attribute
- `Error: Failed to query available provider packages` — provider registry unreachable or version constraints unsatisfiable
- `Error: Backend initialization required` / `Error: Failed to get existing workspaces` — remote state backend unreachable
- `Error: Invalid count argument` / `Error: Inconsistent conditional result types` — HCL type or expression errors
- `Error: no matching AMI found` — data source lookup returns zero results
- `terraform plan` exits with a non-zero code and prints an error block; CI pipeline stage fails

## Applicability

- Terraform v1.0 through v1.11+ (including OpenTofu v1.6+)
- All remote backend types: S3, Azure Blob, GCS, Consul, Terraform Cloud
- Requires: shell access to the Terraform working directory, credentials for the configured providers and remote backend, read access to the state file
- Tools needed: `terraform` CLI, cloud provider CLI (`aws`, `az`, or `gcloud`) for credential verification

## Diagnostic Steps

### Step 1: Run plan with full output captured

Run plan with full output captured. This produces the exact error message and any partial plan output that preceded the failure.

```bash
terraform plan -out=tfplan 2>&1 | tee plan-output.txt
echo "Exit code: $?"
```

Expected output: a changeset summary ending with `Plan: X to add, Y to change, Z to destroy` and exit code 0. A non-zero exit code with an `Error:` block identifies the failure category — use the error text to select the relevant cause below.

### Step 2: Enable debug logging

Enable debug logging when the error from Step 1 is generic or truncated. Shows the full provider API interaction including raw HTTP request/response.

```bash
TF_LOG=DEBUG terraform plan 2>&1 | tee plan-debug.txt
grep -iE "(error|failed|HTTP [45][0-9]{2})" plan-debug.txt | head -40
```

Expected output: lines containing the specific API call that failed and the provider's raw error response, which is typically more specific than the user-facing error. HTTP 401/403 lines indicate authentication; HTTP 404 indicates a missing resource.

### Step 3: Verify provider credentials

Verify provider credentials are valid and scoped to the correct account or project.

```bash
# AWS
aws sts get-caller-identity

# Azure
az account show --output json | jq '{subscriptionId, tenantId, user}'

# GCP
gcloud auth application-default print-access-token > /dev/null && echo "GCP credentials OK"

# Show relevant credential env vars (values masked)
env | grep -E "^(AWS_|ARM_|GOOGLE_|TF_VAR_)" | sed 's/=.*/=<set>/' | sort
```

Expected output: correct account/subscription/project identity for the environment being planned. If `aws sts get-caller-identity` returns an error or a different account than expected, credentials are the root cause.

### Step 4: Detect state drift

Detect state drift by comparing recorded state against live cloud resources.

```bash
terraform plan -refresh-only -detailed-exitcode
echo "Exit code: $?"
```

Expected output when no drift: `No changes. Your infrastructure matches the configuration.` with exit code 0. Exit code 2 means drift was detected — the diff output shows which resources were modified or deleted outside Terraform.

### Step 5: Validate HCL syntax and schema

Validate HCL syntax, type constraints, and schema conformance without contacting any provider API.

```bash
terraform validate
```

Expected output: `Success! The configuration is valid.` If validation fails, the error names the file, line number, and the specific attribute or type problem.

### Step 6: Inspect the dependency graph for cycles

Inspect the resource dependency graph for cycles.

```bash
terraform graph 2>&1 | grep -E "(Cycle|->)" | head -30
```

Expected output when no cycle: dot-format graph lines showing directed edges with no loops. If the plan error from Step 1 contains `Cycle:`, the graph output will show the circular edges. Run this step only when Step 1 reported a cycle error.

### Step 7: Check installed provider versions

Check installed provider versions against the lock file and configuration constraints.

```bash
terraform providers
grep -A5 "provider" .terraform.lock.hcl | head -40
```

Expected output: each provider with its version constraint and the resolved version. When Step 1 reported `Unsupported argument` or `Unsupported block type`, compare the installed version against the provider changelog to confirm a breaking change.

### Step 8: Verify the remote backend is reachable

Verify the remote backend is reachable and the state key exists.

```bash
# S3 backend
aws s3 ls s3://<bucket>/<state-key> --region <region>

# Azure Blob backend
az storage blob exists \
  --container-name tfstate \
  --name terraform.tfstate \
  --account-name <account>

# GCS backend
gsutil ls gs://<bucket>/<state-path>
```

Expected output: the state file listing (S3/GCS) or `{ "exists": true }` (Azure). A permission-denied or no-such-bucket error confirms backend connectivity as the root cause.

## Causes

### Cause A: Provider Authentication Failure

**Statement:** The cloud provider credentials Terraform uses are expired, missing, or scoped to the wrong account, causing all provider API calls to fail with authentication errors.

**Chain:**
- root: provider credentials are expired, missing, or scoped to the wrong account
- s1: the provider plugin cannot authenticate any cloud API call and receives a 401/403
- D: plan aborts with an authentication error before producing a changeset

**Indicators:**
- root: [Step 3] `aws sts get-caller-identity` exits non-zero or returns the wrong account ID
- s1: [Step 1] error message contains `no valid credential sources found`, `AuthFailure`, or `403 Forbidden`

**Interventions:**
- **remediation** (root): refresh the credentials so the provider authenticates against the expected account, then re-run plan.

  ```bash
  # Verify correct identity after refresh
  aws sts get-caller-identity

  # Re-run plan once credentials are confirmed valid
  terraform plan -out=tfplan
  ```

  **Verification:** `aws sts get-caller-identity` returns the expected account ID and ARN; `terraform plan` completes without authentication errors.
- **mitigation** (root): re-establish a short-lived session (SSO login or role assumption) to unblock the current run.

  ```bash
  # AWS SSO
  aws sso login --profile <profile>
  aws sts get-caller-identity --profile <profile>

  # AWS assumed role (CI)
  export $(aws sts assume-role \
    --role-arn <arn> \
    --role-session-name terraform \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' \
    --output text | awk '{print "AWS_ACCESS_KEY_ID="$1"\nAWS_SECRET_ACCESS_KEY="$2"\nAWS_SESSION_TOKEN="$3}')

  # Azure
  az login
  az account set --subscription <subscription-id>
  ```

  **Risk:** refreshing SSO or re-assuming a role may change the IAM principal briefly; verify the refreshed identity matches the expected account before retrying plan. **Duration:** credential refresh lasts for the token TTL (AWS STS default 1 hour, SSO default 8 hours). **Verification:** `aws sts get-caller-identity` returns the expected account ID; plan no longer reports authentication errors.

---

### Cause B: State Drift — Resource Deleted Outside Terraform

**Statement:** A resource recorded in the Terraform state file was deleted or modified directly in the cloud provider, causing plan to fail when it attempts to refresh the now-absent resource.

**Chain:**
- root: a state-tracked resource was deleted or modified directly in the cloud, outside Terraform
- s1: during refresh the cloud API returns a 404 for that resource address
- D: plan surfaces a state refresh error and cannot produce a changeset

**Indicators:**
- root: [Step 4] exit code 2 and output shows resources marked `(deleted)` or `must be replaced`
- s1: [Step 1] error message contains `Error refreshing state` and a resource address

**Interventions:**
- **remediation** (root): reconcile state with reality — remove a permanently-deleted resource from state and config, or re-import a manually-recreated one.

  ```bash
  # If the resource was deleted and should stay deleted, remove it from state AND config:
  terraform state rm <resource-address>
  # Then remove the corresponding resource block from the .tf file.

  # If the resource should be re-imported (was recreated manually):
  terraform import <resource-address> <cloud-resource-id>
  ```

  **Verification:** `terraform plan -refresh-only` exits 0 with `No changes`; subsequent full `terraform plan` completes without refresh errors.
- **mitigation** (s1): write the refreshed state without touching cloud resources to clear the blocking refresh error.

  ```bash
  terraform apply -refresh-only -auto-approve
  ```

  **Risk:** `apply -refresh-only` updates the state file without touching cloud resources; safe to run, but deleted resources will appear as "to be created" in subsequent plans if still in config. Restore the previous state file from backend versioning (S3/GCS object versioning) if the refresh was incorrect. **Duration:** 1–5 minutes depending on resource count; no cloud resources are modified. **Verification:** `terraform plan -refresh-only` no longer reports a refresh error for the affected resource.

---

### Cause C: Dependency Cycle in Resource Graph

**Statement:** Two or more resources reference each other in a way that creates a circular dependency, preventing Terraform from determining a valid execution order.

**Chain:**
- root: two or more resources reference each other, forming a circular dependency in the config
- s1: Terraform's DAG construction detects the cycle and graph traversal fails
- D: plan aborts immediately with a `Cycle:` error and no changeset

**Indicators:**
- root: [Step 6] graph output shows circular edges between the named resources
- s1: [Step 1] error message contains `Cycle:` followed by two or more resource addresses

**Interventions:**
- **remediation** (root): break the cycle by replacing inline rules with standalone resources (or removing the looping `depends_on`), then validate and plan.

  ```bash
  # Replace inline security group rules with standalone resources:
  # aws_security_group_rule.a_to_b references sg_a and sg_b without creating a cycle
  # because the rule resources depend on both groups but the groups do not depend on each other.

  terraform validate   # confirms cycle is resolved
  terraform plan -out=tfplan
  ```

  **Verification:** `terraform validate` returns `Success! The configuration is valid.` and `terraform plan` completes without a Cycle error.
- **mitigation** (root): locate the cycle participants from the Step 1 error and inspect their cross-references to scope the fix.

  ```bash
  # Identify cycle participants from Step 1 error, then inspect their config:
  grep -n "resource\|ingress\|egress\|depends_on" <file>.tf | head -40
  ```

  **Risk:** low — inspection is read-only; refactoring inline rules to standalone resources changes Terraform resource addresses and will show a destroy+create for those rules at apply. **Duration:** immediate diagnostic; the structural fix lands once the cycle is broken in config (re-run `terraform init` only if module references changed). **Verification:** the offending cross-reference is identified in the named `.tf` files.

---

### Cause D: Provider Version Breaking Change

**Statement:** A provider upgrade renamed, removed, or type-changed an attribute that the configuration still references under the old name, causing schema validation to fail before any API call is made.

**Chain:**
- root: a provider upgrade renamed, removed, or type-changed an attribute the config still uses under the old name
- s1: Terraform validates the config against the new provider schema and the stale attribute fails validation
- D: plan aborts with an `Unsupported argument`/`Unsupported block type` error before any API call

**Indicators:**
- root: [Step 7] installed provider version is higher than the last version where the attribute existed per the changelog
- s1: [Step 1] error message contains `Unsupported argument` or `Unsupported block type` referencing an attribute name

**Interventions:**
- **remediation** (root): consult the provider changelog and update the config to the new attribute name/type, then validate and plan.

  ```bash
  # Consult the provider changelog for the migration path, then update the config:
  # Example: rename attribute "enable_dns" -> "enable_dns_support" per provider v5.0 changelog
  # After updating all affected resource blocks:
  terraform validate
  terraform plan -out=tfplan
  ```

  **Verification:** `terraform validate` returns success and `terraform plan` shows no schema errors for the previously failing resource types.
- **mitigation** (root): pin the provider back to the last working version to restore plan immediately.

  ```hcl
  terraform {
    required_providers {
      aws = {
        source  = "hashicorp/aws"
        version = "= 5.30.0"   # substitute last working version
      }
    }
  }
  ```

  Then reinitialize:

  ```bash
  terraform init -upgrade
  terraform providers   # confirm pinned version is installed
  ```

  **Risk:** low — pinning the provider version delays security patches but restores plan without any infrastructure change; roll back by re-pinning and re-running `terraform init -upgrade`. **Duration:** 1–2 minutes; no cloud resources are modified. **Verification:** `terraform providers` shows the pinned version installed and plan no longer reports the schema error.

---

### Cause E: Provider Registry or Network Unreachable

**Statement:** Terraform cannot download provider plugins from the registry or cannot reach the remote state backend, blocking plan initialization.

**Chain:**
- root: the provider registry or the remote state backend is unreachable (network policy, VPN drop, or outage)
- s1: implicit init cannot fetch uncached provider binaries, or the backend cannot load the state file
- D: plan aborts during initialization before any changeset is computed

**Indicators:**
- root: [Step 8] backend access command returns a network error or permission-denied
- s1: [Step 1] error message contains `Failed to query available provider packages`, `Backend initialization required`, or `Failed to get existing workspaces`

**Interventions:**
- **remediation** (root): restore the network path to the registry and backend, then re-run plan.

  ```bash
  # Verify network path to registry:
  curl -s https://registry.terraform.io/v1/providers/hashicorp/aws/versions | jq '.versions[-1].version'

  # Verify backend (e.g., S3):
  aws s3 ls s3://<bucket>/<state-key> --region <region>

  # Re-run plan once connectivity is restored:
  terraform plan -out=tfplan
  ```

  **Verification:** `terraform init` completes without errors; `terraform plan` proceeds past the initialization phase.
- **defensive_fix** (s1): source providers from a local filesystem mirror so init succeeds even when the registry is blocked.

  ```bash
  # Re-initialize (downloads providers if registry is reachable):
  terraform init

  # If registry is blocked, use a local filesystem mirror:
  terraform providers mirror /tmp/tf-mirror
  # Then set provider_installation in ~/.terraformrc:
  # filesystem_mirror { path = "/tmp/tf-mirror" }
  terraform init
  ```

  **Verification:** `terraform init` completes from the mirror without contacting the registry; plan proceeds past initialization.

---

### Cause F: HCL Expression or Type Error

**Statement:** The Terraform configuration contains a type mismatch, invalid expression, or unsatisfiable count/for_each argument that prevents the configuration from being evaluated.

**Chain:**
- root: the config has a type mismatch, invalid expression, or unsatisfiable `count`/`for_each` argument
- s1: HCL expression evaluation fails during the planning phase before any cloud API is contacted
- D: plan aborts with an expression/type error and no changeset

**Indicators:**
- root: [Step 5] `terraform validate` exits non-zero with a type or expression error
- s1: [Step 1] error message contains `Invalid count argument`, `Inconsistent conditional result types`, or `The "count" value depends on resource attributes that cannot be determined until apply`

**Interventions:**
- **remediation** (root): fix the type/expression error in the `.tf` file, then validate and plan.

  ```bash
  # Fix the type error in the .tf file, then validate:
  terraform validate

  # Common fix: convert a string to integer with tonumber():
  #   count = tonumber(var.instance_count)
  # Common fix: wrap unknown-at-plan-time values in a known condition:
  #   for_each = var.create_resource ? toset(["main"]) : toset([])

  terraform plan -out=tfplan
  ```

  **Verification:** `terraform validate` returns `Success! The configuration is valid.` and plan completes without expression evaluation errors.
- **mitigation** (s1): plan only the unaffected resources with `-target` to keep working while fixing the broken config.

  ```bash
  # Use -target to plan only the unaffected resources while fixing the broken config:
  terraform plan -target=<unaffected-resource-address>
  ```

  **Risk:** none — configuration changes do not affect cloud resources until apply; `-target` plans a partial graph and must not be used for apply. **Duration:** immediate; targeted plan bypasses the failing resource until the fix lands. **Verification:** the targeted plan completes for the unaffected resource address.

---

### Cause G: Data Source Lookup Returns No Results

**Statement:** A `data` block filter matches zero resources in the cloud provider, causing the plan to fail because downstream resources depend on the data source's output attributes.

**Chain:**
- root: a `data` block filter (AMI name/owner, VPC tag, subnet CIDR) matches zero resources in the cloud
- s1: the provider returns an error (not an empty list) because the config implies exactly one match is expected
- D: plan aborts because downstream resources cannot resolve the data source's `id`/attributes

**Indicators:**
- root: [Step 1] error message references a `data.` address and contains `no matching` followed by a resource type (e.g., `no matching AMI found`, `no matching VPC found`)

**Interventions:**
- **remediation** (root): update the data source filter to match an existing resource, then validate and plan.

  ```bash
  # Update the data source filter to match an existing resource, e.g.:
  #   filter { name = "name", values = ["updated-ami-name-*"] }
  #   owners = ["amazon"]
  # Or replace with a hardcoded ID for immediate unblocking:
  #   ami = "ami-0abcdef1234567890"

  terraform validate
  terraform plan -out=tfplan
  ```

  **Verification:** the data source lookup step in the plan output shows a resolved ID (not `(known after apply)`); plan completes without data source errors.
- **mitigation** (root): inspect what the filter currently matches to choose the correct values or a hardcoded ID.

  ```bash
  # Inspect what the filter currently matches:
  # AWS AMI example:
  aws ec2 describe-images \
    --filters "Name=name,Values=<name-pattern>" \
    --owners <owner-id> \
    --region <region> \
    --query 'Images[*].[ImageId,Name,CreationDate]' \
    --output table
  ```

  **Risk:** low — inspection is read-only; temporarily replacing the lookup with a hardcoded ID bypasses the filter failure but couples the config to a specific resource ID that may differ across environments. **Duration:** immediate diagnostic; fix duration depends on whether the resource must be recreated. **Verification:** the command lists at least one matching image, or the chosen hardcoded ID is confirmed valid for the target environment.

---

### Cause Z: Unidentified

**Statement:** The plan failure does not match any documented cause after completing all diagnostic steps, indicating a transient API error, CLI bug, corrupted plugin cache, or environment-specific issue.

**Chain:**
- root: the failure cause is outside the documented set (transient API error, CLI bug, corrupted `.terraform` cache, or environment-specific issue)
- D: plan fails with no matching cause after Steps 1–8

**Indicators:**
- root: [Default] no cause identified after completing Steps 1–8

**Interventions:**
- **mitigation** (D): capture a full TRACE diagnostic snapshot and escalate to the platform/infrastructure team.

  ```bash
  # Clear the plugin cache and reinitialize:
  rm -rf .terraform
  terraform init

  # Run plan with maximum verbosity for escalation:
  TF_LOG=TRACE terraform plan 2>&1 | tee plan-trace.txt
  ```

  Escalate to the platform/infrastructure team with `plan-trace.txt` and the full `plan-output.txt` from Step 1. File a GitHub issue on the relevant provider repository if the trace shows a provider panic or unexpected API response.

  **Risk:** low — clearing the plugin cache and reinitializing is non-destructive; the SME owns root-cause analysis from here. **Duration:** 5–15 minutes for provider re-download and trace capture. **Verification:** escalation ticket created with trace logs attached; platform team confirms root cause.

## Prevention

- **Pin provider versions with pessimistic constraints.** Use `~> 5.30` (allow patch updates only) rather than `>= 5.0` (allows major/minor upgrades). Commit `.terraform.lock.hcl` to version control so all team members and CI use identical provider versions. Run `terraform init -upgrade` deliberately when adopting new versions.

- **Add `terraform validate` as an early CI step before plan.** Validate catches syntax, type, and schema errors without provider API calls, providing faster feedback:

  ```yaml
  - name: Validate
    run: |
      terraform init -backend=false
      terraform validate
  ```

- **Run scheduled drift detection.** Execute `terraform plan -refresh-only -detailed-exitcode` daily in CI. Exit code 2 indicates drift; alert the team before drift accumulates into plan failures:

  ```yaml
  - name: Drift Detection
    run: |
      terraform plan -refresh-only -detailed-exitcode
  ```

- **Enforce Terraform-only changes with cloud policies.** Use AWS SCPs, Azure Policy, or GCP Organization Policies to restrict manual modifications to Terraform-managed resources. Tag all managed resources with `ManagedBy = "terraform"` to make drift detection reliable.

- **Separate state files by environment and component.** Avoid monolithic state files that increase plan time and blast radius. Use separate backend keys per environment (e.g., `env:/production/terraform.tfstate`).

- **Use pre-commit hooks to catch issues locally.** Install `terraform_fmt`, `terraform_validate`, and `terraform_tflint` hooks to catch formatting, validation, and linting errors before code reaches CI.

- **Enable S3 versioning (or GCS object versioning) on the state bucket.** State versioning allows rollback of state mutations and recovery from accidental `state rm` operations.

## Sources

- [Terraform CLI: plan Command](https://developer.hashicorp.com/terraform/cli/commands/plan) — Priority 1. Plan flags, exit codes, -refresh-only, -target, -detailed-exitcode, output format.
- [Terraform Internals: Debugging](https://developer.hashicorp.com/terraform/internals/debugging) — Priority 1. TF_LOG levels (TRACE, DEBUG, INFO, WARN, ERROR), provider crash logs, interpreting debug output.
- [Terraform Language: Provider Requirements](https://developer.hashicorp.com/terraform/language/providers/requirements) — Priority 1. Version constraints, lock file (.terraform.lock.hcl), provider installation, pessimistic constraint operator.
- [Terraform Language: State](https://developer.hashicorp.com/terraform/language/state) — Priority 1. State purpose, drift detection, remote storage, locking.
- [Terraform CLI: State Commands](https://developer.hashicorp.com/terraform/cli/state) — Priority 1. state rm, state mv, state import, state list — used in Cause B resolution.
- [Terraform Language: Backend Configuration](https://developer.hashicorp.com/terraform/language/settings/backends/configuration) — Priority 1. S3, Azure Blob, GCS backend setup, authentication, initialization errors.
