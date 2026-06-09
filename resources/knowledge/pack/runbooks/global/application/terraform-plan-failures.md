---
id: "terraform-plan-failures"
title: "Terraform Plan Failures"
domain: application
service: terraform
symptom_class: [auth_failure, deployment_failure]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
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

### Step 1

Run plan with full output captured. This produces the exact error message and any partial plan output that preceded the failure.

```bash
terraform plan -out=tfplan 2>&1 | tee plan-output.txt
echo "Exit code: $?"
```

Expected output: a changeset summary ending with `Plan: X to add, Y to change, Z to destroy` and exit code 0. A non-zero exit code with an `Error:` block identifies the failure category — use the error text to select the relevant cause below.

### Step 2

Enable debug logging when the error from Step 1 is generic or truncated. Shows the full provider API interaction including raw HTTP request/response.

```bash
TF_LOG=DEBUG terraform plan 2>&1 | tee plan-debug.txt
grep -iE "(error|failed|HTTP [45][0-9]{2})" plan-debug.txt | head -40
```

Expected output: lines containing the specific API call that failed and the provider's raw error response, which is typically more specific than the user-facing error. HTTP 401/403 lines indicate authentication; HTTP 404 indicates a missing resource.

### Step 3

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

### Step 4

Detect state drift by comparing recorded state against live cloud resources.

```bash
terraform plan -refresh-only -detailed-exitcode
echo "Exit code: $?"
```

Expected output when no drift: `No changes. Your infrastructure matches the configuration.` with exit code 0. Exit code 2 means drift was detected — the diff output shows which resources were modified or deleted outside Terraform.

### Step 5

Validate HCL syntax, type constraints, and schema conformance without contacting any provider API.

```bash
terraform validate
```

Expected output: `Success! The configuration is valid.` If validation fails, the error names the file, line number, and the specific attribute or type problem.

### Step 6

Inspect the resource dependency graph for cycles.

```bash
terraform graph 2>&1 | grep -E "(Cycle|->)" | head -30
```

Expected output when no cycle: dot-format graph lines showing directed edges with no loops. If the plan error from Step 1 contains `Cycle:`, the graph output will show the circular edges. Run this step only when Step 1 reported a cycle error.

### Step 7

Check installed provider versions against the lock file and configuration constraints.

```bash
terraform providers
grep -A5 "provider" .terraform.lock.hcl | head -40
```

Expected output: each provider with its version constraint and the resolved version. When Step 1 reported `Unsupported argument` or `Unsupported block type`, compare the installed version against the provider changelog to confirm a breaking change.

### Step 8

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

**Mechanism:** Terraform communicates with cloud APIs to plan resource changes; without valid credentials the provider plugin cannot authenticate any API call and returns a 401 or 403 immediately. In CI pipelines, temporary tokens (AWS STS session tokens, OAuth access tokens) expire between pipeline stages and are silently absent in the environment.

**Indicator:**

- [Step 1] Error message contains `no valid credential sources found` or `AuthFailure` or `403 Forbidden`
- [Step 3] `aws sts get-caller-identity` exits non-zero or returns wrong account ID

<!-- match: {"step": 1, "predicate": "contains", "target": "no valid credential sources found"} -->
<!-- match: {"step": 3, "predicate": "exit_code", "target": 1} -->

**Mitigation:**

- **Risk:** Refreshing SSO or re-assuming a role may change the IAM principal briefly; verify the refreshed identity matches the expected account before retrying plan.
- **Command:**

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

- **Duration:** Credential refresh lasts for the token TTL (AWS STS default 1 hour, SSO default 8 hours).

**Resolution:**

```bash
# Verify correct identity after refresh
aws sts get-caller-identity

# Re-run plan once credentials are confirmed valid
terraform plan -out=tfplan
```

- **Impact:** Single Terraform run; credential change does not affect other users or CI pipelines.
- **Rollback:** No rollback needed; credential refresh is non-destructive.

**Verification:** `aws sts get-caller-identity` returns the expected account ID and ARN; `terraform plan` completes without authentication errors.

---

### Cause B: State Drift — Resource Deleted Outside Terraform

**Statement:** A resource recorded in the Terraform state file was deleted or modified directly in the cloud provider, causing plan to fail when it attempts to refresh the now-absent resource.

**Mechanism:** During `terraform plan`, Terraform refreshes every resource in state by querying the cloud API. When a resource no longer exists, the API returns a 404; Terraform surfaces this as a state error and cannot produce a plan. Manual deletions via the cloud console or CLI, expired auto-delete policies, or cloud-side resource replacement all cause this.

**Indicator:**

- [Step 1] Error message contains `Error refreshing state` and a resource address
- [Step 4] Exit code 2 and output shows resources marked `(deleted)` or `must be replaced`

<!-- match: {"step": 1, "predicate": "contains", "target": "Error refreshing state"} -->
<!-- match: {"step": 4, "predicate": "exit_code", "target": 2} -->

**Mitigation:**

- **Risk:** `apply -refresh-only` updates the state file without touching cloud resources; safe to run, but deleted resources will appear as "to be created" in subsequent plans if still in config.
- **Command:**

  ```bash
  terraform apply -refresh-only -auto-approve
  ```

- **Duration:** 1–5 minutes depending on resource count; no cloud resources are modified.

**Resolution:**

```bash
# If the resource was deleted and should stay deleted, remove it from state AND config:
terraform state rm <resource-address>
# Then remove the corresponding resource block from the .tf file.

# If the resource should be re-imported (was recreated manually):
terraform import <resource-address> <cloud-resource-id>
```

- **Impact:** State-only change; no cloud resources are created, modified, or destroyed.
- **Rollback:** Restore the previous state file from backend versioning (S3 versioning, GCS object versioning) if the state rm was incorrect.

**Verification:** `terraform plan -refresh-only` exits 0 with `No changes`; subsequent full `terraform plan` completes without refresh errors.

---

### Cause C: Dependency Cycle in Resource Graph

**Statement:** Two or more resources reference each other in a way that creates a circular dependency, preventing Terraform from determining a valid execution order.

**Mechanism:** Terraform builds a directed acyclic graph (DAG) before planning; a cycle causes graph traversal to fail immediately with no plan produced. Cycles most commonly arise from inline `ingress`/`egress` blocks in `aws_security_group` resources that reference each other, bidirectional module outputs, or explicit `depends_on` loops added incrementally.

**Indicator:**

- [Step 1] Error message contains `Cycle:` followed by two or more resource addresses
- [Step 6] Graph output shows circular edges between the named resources

<!-- match: {"step": 1, "predicate": "contains", "target": "Cycle:"} -->

**Mitigation:**

- **Risk:** Low. Refactoring inline rules to standalone resources changes the Terraform resource addresses; existing infrastructure is not modified, but plan will show a destroy+create for the affected rules if they had inline config.
- **Command:**

  ```bash
  # Identify cycle participants from Step 1 error, then inspect their config:
  grep -n "resource\|ingress\|egress\|depends_on" <file>.tf | head -40
  ```

- **Duration:** Immediate once the cycle is broken in config; requires `terraform init` only if module references changed.

**Resolution:**

```bash
# Replace inline security group rules with standalone resources:
# aws_security_group_rule.a_to_b references sg_a and sg_b without creating a cycle
# because the rule resources depend on both groups but the groups do not depend on each other.

terraform validate   # confirms cycle is resolved
terraform plan -out=tfplan
```

- **Impact:** Config-level change only; no immediate cloud resource modification until apply.

**Verification:** `terraform validate` returns `Success! The configuration is valid.` and `terraform plan` completes without a Cycle error.

---

### Cause D: Provider Version Breaking Change

**Statement:** A provider upgrade renamed, removed, or type-changed an attribute that the configuration still references under the old name, causing schema validation to fail before any API call is made.

**Mechanism:** Each provider version ships its own resource schema; Terraform validates the configuration against the installed provider schema before contacting cloud APIs. When an attribute is renamed or removed in a new provider version, every resource block using that attribute fails schema validation. The error appears at plan time even with valid credentials and healthy state.

**Indicator:**

- [Step 1] Error message contains `Unsupported argument` or `Unsupported block type` referencing an attribute name
- [Step 7] Installed provider version is higher than the last version where the attribute existed per the changelog

<!-- match: {"step": 1, "predicate": "contains", "target": "Unsupported argument"} -->

**Mitigation:**

- **Risk:** Low. Pinning the provider version delays security patches but restores plan immediately without any infrastructure change.
- **Command:**

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

- **Duration:** 1–2 minutes; no cloud resources are modified.

**Resolution:**

```bash
# Consult the provider changelog for the migration path, then update the config:
# Example: rename attribute "enable_dns" -> "enable_dns_support" per provider v5.0 changelog
# After updating all affected resource blocks:
terraform validate
terraform plan -out=tfplan
```

- **Impact:** Config-level change; blast radius is every resource block using the renamed attribute.
- **Rollback:** Re-pin to the previous version in `required_providers` and re-run `terraform init -upgrade`.

**Verification:** `terraform validate` returns success and `terraform plan` shows no schema errors for the previously failing resource types.

---

### Cause E: Provider Registry or Network Unreachable

**Statement:** Terraform cannot download provider plugins from the registry or cannot reach the remote state backend, blocking plan initialization.

**Mechanism:** `terraform plan` requires the lock-file-pinned provider binaries to be present in `.terraform/providers/`; if the registry is unreachable and providers are not cached, init (which plan invokes implicitly) fails. Similarly, the remote state backend must be reachable to load the state file before planning begins. Network policies, VPN disconnects, or backend outages cause this class of failure.

**Indicator:**

- [Step 1] Error message contains `Failed to query available provider packages` or `Backend initialization required` or `Failed to get existing workspaces`
- [Step 8] Backend access command returns a network error or permission-denied

<!-- match: {"step": 1, "predicate": "contains", "target": "Failed to query available provider packages"} -->
<!-- match: {"step": 8, "predicate": "exit_code", "target": 1} -->

**Mitigation:**

- **Risk:** Low. Using a provider mirror or filesystem cache is non-destructive and does not affect cloud resources.
- **Command:**

  ```bash
  # Re-initialize (downloads providers if registry is reachable):
  terraform init

  # If registry is blocked, use a local filesystem mirror:
  terraform providers mirror /tmp/tf-mirror
  # Then set provider_installation in ~/.terraformrc:
  # filesystem_mirror { path = "/tmp/tf-mirror" }
  terraform init
  ```

- **Duration:** 1–10 minutes depending on provider binary sizes.

**Resolution:**

```bash
# Verify network path to registry:
curl -s https://registry.terraform.io/v1/providers/hashicorp/aws/versions | jq '.versions[-1].version'

# Verify backend (e.g., S3):
aws s3 ls s3://<bucket>/<state-key> --region <region>

# Re-run plan once connectivity is restored:
terraform plan -out=tfplan
```

**Verification:** `terraform init` completes without errors; `terraform plan` proceeds past the initialization phase.

---

### Cause F: HCL Expression or Type Error

**Statement:** The Terraform configuration contains a type mismatch, invalid expression, or unsatisfiable count/for_each argument that prevents the configuration from being evaluated.

**Mechanism:** Terraform evaluates HCL expressions during the planning phase; errors such as `Invalid count argument` (a non-integer used where an integer is required), `Inconsistent conditional result types` (ternary branches returning incompatible types), or unknown values used in `count` or `for_each` cause evaluation to fail before any cloud API is contacted. These errors are surfaced by `terraform validate` and are configuration-only issues.

**Indicator:**

- [Step 1] Error message contains `Invalid count argument` or `Inconsistent conditional result types` or `The "count" value depends on resource attributes that cannot be determined until apply`
- [Step 5] `terraform validate` exits non-zero with a type or expression error

<!-- match: {"step": 1, "predicate": "contains", "target": "Invalid count argument"} -->
<!-- match: {"step": 5, "predicate": "exit_code", "target": 1} -->

**Mitigation:**

- **Risk:** None. Configuration changes do not affect cloud resources until apply.
- **Command:**

  ```bash
  # Use -target to plan only the unaffected resources while fixing the broken config:
  terraform plan -target=<unaffected-resource-address>
  ```

- **Duration:** Immediate; targeted plan bypasses the failing resource.

**Resolution:**

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

---

### Cause G: Data Source Lookup Returns No Results

**Statement:** A `data` block filter matches zero resources in the cloud provider, causing the plan to fail because downstream resources depend on the data source's output attributes.

**Mechanism:** Data sources execute read API calls during plan to retrieve resource attributes; when a filter (AMI owner/name pattern, VPC tag, subnet CIDR) matches zero results, the provider returns an error rather than an empty list, because the configuration implies exactly one matching resource is expected. Downstream resources that reference the data source's `id` or other attributes cannot be planned.

**Indicator:**

- [Step 1] Error message contains `no matching` followed by a resource type (e.g., `no matching AMI found`, `no matching VPC found`)
- [Step 1] Error message references a `data.` address

<!-- match: {"step": 1, "predicate": "contains", "target": "no matching"} -->

**Mitigation:**

- **Risk:** Low. Temporarily replacing the data source lookup with a hardcoded ID bypasses the filter failure but couples the config to a specific resource ID that may differ across environments.
- **Command:**

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

- **Duration:** Immediate diagnostic; fix duration depends on whether the resource must be recreated.

**Resolution:**

```bash
# Update the data source filter to match an existing resource, e.g.:
#   filter { name = "name", values = ["updated-ami-name-*"] }
#   owners = ["amazon"]
# Or replace with a hardcoded ID for immediate unblocking:
#   ami = "ami-0abcdef1234567890"

terraform validate
terraform plan -out=tfplan
```

**Verification:** The data source lookup step in the plan output shows a resolved ID (not `(known after apply)`); plan completes without data source errors.

---

### Cause Z: Unidentified Plan Failure

**Statement:** The plan failure does not match any of the documented causes after completing all diagnostic steps.

**Mechanism:** Some plan failures are caused by transient provider API errors, Terraform CLI bugs, corrupted `.terraform` plugin cache, or environment-specific issues not covered by the standard diagnostic flow. These require escalation or provider-specific investigation.

**Indicator:**

- [Default] No cause identified after completing Steps 1–8

**Mitigation:**

- **Risk:** Low. Clearing the plugin cache and reinitializing is non-destructive.
- **Command:**

  ```bash
  # Clear the plugin cache and reinitialize:
  rm -rf .terraform
  terraform init

  # Run plan with maximum verbosity for escalation:
  TF_LOG=TRACE terraform plan 2>&1 | tee plan-trace.txt
  ```

- **Duration:** 5–15 minutes for provider re-download and trace capture.

**Resolution:** Out of runbook scope. Escalate to the platform/infrastructure team with `plan-trace.txt` and the full `plan-output.txt` from Step 1. File a GitHub issue on the relevant provider repository if the trace shows a provider panic or unexpected API response.

**Verification:** Escalation ticket created with trace logs attached; platform team confirms root cause.

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
