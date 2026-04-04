---
id: terraform-plan-failures
title: "Terraform Plan Failures"
domain: application
service: terraform
symptom_class:
  - auth_failure
  - deployment_failure
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: "kb-researcher"
status: draft
tags:
  - terraform
  - plan
  - iac
  - infrastructure-as-code
  - provider
  - state-drift
difficulty: intermediate
---

# Terraform Plan Failures

## Problem Definition

Terraform plan failures block all infrastructure deployments because `terraform plan` is a prerequisite for `terraform apply`. This runbook applies to Terraform v1.0 through v1.11+ (including OpenTofu v1.6+) with any supported backend (S3, Azure Blob, GCS, Consul, Terraform Cloud). Diagnosing plan failures requires shell access to the Terraform working directory, credentials for the configured providers and remote backend, and read access to the state file.

Plan failures manifest as non-zero exit codes from `terraform plan` with an error message identifying the failure category. Common symptoms include:

- `Error: error configuring Terraform AWS Provider: no valid credential sources found` when provider credentials are expired, missing, or misconfigured
- `Error: Error refreshing state: resource "aws_instance.web" not found` when a resource was deleted outside Terraform
- `Error: Cycle: aws_security_group.a, aws_security_group.b` when circular references exist in the dependency graph
- `Error: Unsupported argument: An argument named "enable_dns" is not expected here` after a provider upgrade renames or removes attributes
- `Error: Failed to query available provider packages` when the provider registry is unreachable or version constraints are unsatisfiable
- `Error: Backend initialization required` or `Error: Failed to get existing workspaces` when the remote state backend is unreachable
- `Error: Invalid count argument` or `Error: Inconsistent conditional result types` when HCL expressions contain type errors
- Data source errors such as `Error: no matching AMI found` when a `data` block lookup returns zero results

## Diagnostic Steps

### Step 1. Run Plan and Capture the Error

Run `terraform plan` with output saved to a file. This captures the full error message and any partial plan output that preceded the failure.

```bash
terraform plan -out=tfplan 2>&1 | tee plan-output.txt
```

Expected output when the plan succeeds: a changeset summary ending with `Plan: X to add, Y to change, Z to destroy`. If the command exits non-zero, the error message in `plan-output.txt` indicates the failure category. Proceed to the step matching the error type.

### Step 2. Enable Debug Logging for Obscure Errors

If the error message from Step 1 is generic or truncated, enable Terraform debug logging to capture the full provider API interaction.

```bash
TF_LOG=DEBUG terraform plan 2>&1 | tee plan-debug.txt
```

Search the debug output for lines containing `error`, `failed`, or HTTP status codes (401, 403, 404, 500). The debug log shows the exact API call that failed and the provider's raw error response, which is often more specific than the user-facing error.

### Step 3. Check Provider Authentication

Verify that the credentials Terraform uses to communicate with cloud provider APIs are valid and correctly configured. This checks the most common plan failure cause.

```bash
# AWS
aws sts get-caller-identity

# Azure
az account show

# GCP
gcloud auth application-default print-access-token > /dev/null && echo "OK"

# List relevant environment variables
env | grep -E "^(AWS_|ARM_|GOOGLE_|TF_VAR_)" | sort
```

If `aws sts get-caller-identity` fails or returns an unexpected identity (wrong account, wrong role), provider authentication is the root cause. If it succeeds with the correct identity, the issue is not credentials and you should move to Step 4.

### Step 4. Detect State Drift

Compare the recorded state against actual cloud resources. State drift causes plan failures when Terraform attempts to refresh a resource that no longer exists or has changed incompatibly.

```bash
terraform plan -refresh-only
```

Expected output when no drift exists: `No changes. Your infrastructure matches the configuration.` If the output shows resources to update or destroy, those resources were modified or deleted outside Terraform. Resources marked with `must be replaced` or `(deleted)` confirm drift as the failure cause.

### Step 5. Validate Configuration Syntax

Run Terraform's built-in validation to catch HCL syntax errors, type mismatches, missing required arguments, and unsupported attributes without contacting any provider API.

```bash
terraform validate
```

Expected output: `Success! The configuration is valid.` If validation fails, the error message names the file, line number, and the specific syntax or type problem. This identifies configuration-level issues independent of state or credentials.

### Step 6. Inspect Dependency Graph for Cycles

Generate the resource dependency graph to identify circular references that prevent Terraform from determining a valid execution order.

```bash
terraform graph 2>&1 | head -50
```

If the plan error contains `Cycle:` followed by resource addresses, the graph output shows the circular edges. Look for security groups referencing each other inline, modules with bidirectional outputs, or `depends_on` loops. If no cycle error appeared in Step 1, skip this step.

### Step 7. Check Provider and Module Versions

Verify that installed provider and module versions are compatible with the configuration. A provider upgrade can rename attributes, change resource behavior, or require new arguments.

```bash
terraform providers
cat .terraform.lock.hcl | head -30
```

The `providers` output lists each provider and its version constraint. The lock file shows the exact installed version. If the plan error references an unsupported argument or changed attribute, compare the installed provider version against the provider changelog to confirm a breaking change.

### Step 8. Verify Backend Connectivity

Test whether Terraform can reach the remote state backend. Backend connectivity failures prevent plan from reading the state file.

```bash
# S3 backend
aws s3 ls s3://<bucket-name>/<state-key> --region <region>

# Azure Blob backend
az storage blob exists --container-name tfstate --name terraform.tfstate --account-name <account>

# GCS backend
gsutil ls gs://<bucket>/<state-path>
```

If the backend is unreachable (network error, permission denied, bucket does not exist), Terraform cannot read the state and plan fails immediately. A successful listing confirms backend connectivity and the issue lies elsewhere.

## Mitigation

### Option 1. Target Specific Resources to Unblock Deployment

Apply changes only to unaffected resources, bypassing the resource that causes the plan failure.

- **Risk**: Medium. Targeted applies skip dependency validation for excluded resources. If the targeted resource depends on the failing one, the apply may produce inconsistent infrastructure.
- **Command**:
  ```bash
  terraform plan -target=<resource-address>
  terraform apply -target=<resource-address>
  ```
- **Verify**:
  ```bash
  terraform plan
  ```
  A full plan afterward reveals remaining issues. If the full plan succeeds, the targeted apply resolved the blocker.
- **Duration**: 1-5 minutes depending on resource type.

### Option 2. Refresh State to Reconcile Drift

Update the state file to match actual cloud resources without modifying infrastructure.

- **Risk**: Low. Refresh-only modifies the state file but does not create, change, or destroy any cloud resource. However, resources deleted outside Terraform will be marked for recreation in subsequent plans.
- **Command**:
  ```bash
  terraform apply -refresh-only -auto-approve
  ```
- **Verify**:
  ```bash
  terraform plan
  ```
  The plan should show fewer unexpected changes. If a deleted resource now appears as "will be created," the refresh correctly detected the deletion.
- **Duration**: 1-5 minutes depending on resource count.

### Option 3. Remove a Deleted Resource from State

If a resource was deleted outside Terraform and refresh fails, remove it from state so Terraform stops trying to read it.

- **Risk**: Medium. The resource is no longer managed by Terraform. If it still exists in configuration, the next plan will propose creating it. If the resource was recreated manually, use `terraform import` instead.
- **Command**:
  ```bash
  terraform state rm <resource-address>
  ```
- **Verify**:
  ```bash
  terraform plan
  ```
  The resource should appear as "will be created" (if still in config) or disappear from the plan (if config was also removed).
- **Duration**: Seconds.

### Option 4. Pin Provider Version to Restore Compatibility

Roll back to the previous working provider version when an upgrade introduced breaking changes.

- **Risk**: Low. Pinning delays access to new features and security patches but restores immediate plan functionality.
- **Command**:
  ```hcl
  terraform {
    required_providers {
      aws = {
        source  = "hashicorp/aws"
        version = "= 5.30.0"  # Pin to last working version
      }
    }
  }
  ```
  Then reinitialize:
  ```bash
  terraform init -upgrade
  ```
- **Verify**:
  ```bash
  terraform providers
  terraform plan
  ```
  Providers output should show the pinned version and the plan should succeed.
- **Duration**: 1-2 minutes.

## Root Cause Resolution

**If** `aws sts get-caller-identity` fails or returns the wrong identity, the credential chain is broken. Refresh SSO credentials with `aws sso login --profile <profile>`, or for assumed roles, verify the trust policy allows the calling principal and run `aws sts assume-role --role-arn <arn> --role-session-name terraform`. For CI pipelines, verify the `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN` environment variables are set and not expired.

**If** Azure authentication fails with `az account show` returning an error, re-authenticate with `az login` and set the subscription with `az account set --subscription <id>`. For CI using a service principal, verify `ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_TENANT_ID`, and `ARM_SUBSCRIPTION_ID` are set and the client secret has not expired.

**If** `terraform plan -refresh-only` shows resources modified outside Terraform, reconcile by accepting the drift (`terraform apply -refresh-only -auto-approve`) or overwriting manual changes (`terraform apply -auto-approve`). For deleted resources, remove from state (`terraform state rm <address>`) or re-import (`terraform import <address> <cloud-id>`).

**If** the plan reports `Cycle:` errors naming two or more resources, break the cycle by extracting inline rules into standalone resources. For example, replace inline `ingress`/`egress` blocks in `aws_security_group` with separate `aws_security_group_rule` resources that reference both security groups without creating a circular dependency.

**If** a provider upgrade renamed or removed a resource attribute (identified by `Unsupported argument` or `Unsupported block type`), consult the provider changelog for the migration path. Update the configuration to use the new attribute name, or pin the provider to the previous version while migrating incrementally.

**If** a `data` source lookup fails with `no matching resource found`, verify the filter parameters match an existing resource. Common causes include incorrect AMI owner, wrong region, deleted VPC, or tag filters that no longer match. Fix the filter or replace the data source with a hard-coded resource ID as a temporary workaround.

**If** module version constraints are unsatisfiable, update the version constraint in the module block to a range that includes available versions, then run `terraform init -upgrade` to resolve.

## Verification

After resolving the plan failure, confirm the full Terraform workflow operates correctly.

1. Run a full plan without targeting:

```bash
terraform plan
```

The plan should complete without errors and show only expected changes or `No changes`.

2. Verify state consistency by confirming the resource count matches expectations and no drift exists:

```bash
terraform state list | wc -l
terraform plan -refresh-only
```

The refresh-only plan should report `No changes`.

3. Run configuration validation:

```bash
terraform validate
```

Expected: `Success! The configuration is valid.`

4. Apply in a non-production environment first to confirm the full cycle works:

```bash
terraform workspace select staging
terraform apply
```

The apply should succeed and create/modify only the expected resources.

## Prevention

- **Pin provider versions with pessimistic constraints.** Use `~> 5.30` (allow patch updates) rather than `>= 5.0` (allow any major/minor). Commit `.terraform.lock.hcl` to version control so all team members and CI use identical provider versions. Run `terraform init -upgrade` deliberately when adopting new versions.

- **Add `terraform validate` as an early CI step.** Validate catches syntax, type, and schema errors before plan runs, providing faster feedback:

```yaml
- name: Validate
  run: |
    terraform init -backend=false
    terraform validate
```

- **Run scheduled drift detection.** Execute `terraform plan -refresh-only -detailed-exitcode` on a daily schedule in CI. Exit code 2 indicates drift. Alert the team before drift causes plan failures:

```yaml
- name: Drift Detection
  run: terraform plan -refresh-only -detailed-exitcode
```

- **Enforce Terraform-only changes with cloud policies.** Use AWS SCPs, Azure Policy, or GCP Organization Policies to restrict manual modifications to Terraform-managed resources. Tag all managed resources with `ManagedBy = "terraform"`.

- **Use pre-commit hooks to catch issues locally.** Install `terraform_fmt`, `terraform_validate`, and `terraform_tflint` pre-commit hooks to catch formatting, validation, and linting errors before code reaches CI.

- **Separate state files by environment and component.** Avoid monolithic state files that increase plan time and blast radius. Use workspaces or separate backend keys per environment.

## Sources

- [Terraform CLI: plan Command](https://developer.hashicorp.com/terraform/cli/commands/plan) -- Official reference for plan flags, exit codes, and output format.
- [Terraform Language: State](https://developer.hashicorp.com/terraform/language/state) -- State purpose, locking, drift detection, and remote storage.
- [Terraform CLI: State Commands](https://developer.hashicorp.com/terraform/cli/state) -- State manipulation including import, rm, mv, and show.
- [Terraform Language: Backend Configuration](https://developer.hashicorp.com/terraform/language/settings/backends/configuration) -- Backend setup for S3, Azure Blob, GCS, and Consul with authentication.
- [Terraform Language: Provider Requirements](https://developer.hashicorp.com/terraform/language/providers/requirements) -- Version constraints, lock files, and provider installation.
- [Terraform CLI: Debugging](https://developer.hashicorp.com/terraform/internals/debugging) -- TF_LOG levels, trace output, and provider debug logging.
