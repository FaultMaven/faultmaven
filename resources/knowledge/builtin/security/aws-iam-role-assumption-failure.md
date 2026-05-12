---
id: "aws-iam-role-assumption-failure"
title: "AWS IAM Role Assumption Failure"
domain: security
service: aws-iam
symptom_class: [auth_failure]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [sts, assume-role, trust-policy, external-id, cross-account, role-chaining, irsa, scp]
difficulty: intermediate
---

## Symptom Recognition

`sts:AssumeRole` calls return `AccessDenied` with one of these messages:

```text
An error occurred (AccessDenied) when calling the AssumeRole operation:
User: arn:aws:iam::111111111111:user/deployer is not authorized to perform: sts:AssumeRole
on resource: arn:aws:iam::222222222222:role/TargetRole
```

```text
An error occurred (AccessDenied) when calling the AssumeRole operation:
The requested DurationSeconds exceeds the MaxSessionDuration set for this role.
```

```text
An error occurred (AccessDenied) when calling the AssumeRole operation:
Not authorized to assume role with MFA
```

```text
An error occurred (RegionDisabled) when calling the AssumeRole operation:
AWS STS is not activated in the requested region
```

Role chaining failures produce:

```text
An error occurred (AccessDenied) when calling the AssumeRole operation:
Role chaining is limited to a maximum of one hour
```

Applications receive `ExpiredTokenException` or `InvalidClientTokenId` when STS temporary credentials are not refreshed after a session expires. CloudTrail shows `AssumeRole` events with `errorCode: AccessDenied` and `errorMessage` fields that identify the specific gate that rejected the call.

## Applicability

AWS CLI v2+, all AWS SDKs, EKS IRSA (Pod Identity), EC2 instance profiles using chained roles, GitHub Actions OIDC federation, and any AWS service (Lambda, ECS task role, CodeBuild) that calls `sts:AssumeRole` or `sts:AssumeRoleWithWebIdentity`. Requires IAM read permissions (`iam:GetRole`, `iam:SimulatePrincipalPolicy`) in the caller's account, and CloudTrail Management Events enabled in both source and target accounts. AWS Organizations SCP analysis requires `organizations:ListPoliciesForTarget` access.

## Diagnostic Steps

### Step 1: Confirm active caller identity

```bash
aws sts get-caller-identity
```

Expected output: JSON with `Account`, `UserId`, and `Arn`. The ARN must match the principal listed in the target role's trust policy `Principal` element.

### Step 2: Retrieve the target role's trust policy

```bash
aws iam get-role --role-name TargetRole \
  --query 'Role.AssumeRolePolicyDocument' \
  --output json | python3 -m json.tool
```

Expected output: a JSON policy with `Statement` entries. Check that `Principal` includes the caller ARN or account, and that any `Condition` blocks (`sts:ExternalId`, `aws:MultiFactorAuthPresent`, date ranges, IP ranges) match what the caller is providing.

### Step 3: Check the role's MaxSessionDuration

```bash
aws iam get-role --role-name TargetRole \
  --query 'Role.[MaxSessionDuration,RoleId]' \
  --output json
```

Expected output: `[3600, "AROA..."]`. Default is 3600 s (1 h). Role chaining caps at 3600 s regardless of this value. If the caller's `DurationSeconds` exceeds the returned value the call fails.

### Step 4: Simulate whether the caller's identity-based policy permits AssumeRole

```bash
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::111111111111:role/CallerRole \
  --action-names sts:AssumeRole \
  --resource-arns arn:aws:iam::222222222222:role/TargetRole \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}'
```

Expected output: `"Decision": "allowed"`. `implicitDeny` means no identity policy grants the action. `explicitDeny` means a permissions boundary, SCP, or explicit Deny statement blocks it.

### Step 5: Look up the failed AssumeRole event in CloudTrail

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRole \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?contains(CloudTrailEvent,`AccessDenied`)].{Time:EventTime,Msg:CloudTrailEvent}' \
  --output json | python3 -c "import sys,json; [print(json.loads(r['Msg'])['errorMessage']) for r in json.load(sys.stdin)]"
```

Expected output: the exact `errorMessage` string that distinguishes trust-policy denial from SCP denial from session-duration rejection.

### Step 6: Check caller's identity-based policy includes sts:AssumeRole on the target ARN

```bash
aws iam list-attached-role-policies --role-name CallerRole
aws iam list-role-policies --role-name CallerRole
```

Expected output: at least one policy grants `sts:AssumeRole` on `arn:aws:iam::222222222222:role/TargetRole` or a wildcard `Resource`. If no policy grants it, same-account assumption also requires either an explicit trust entry or an identity policy.

### Step 7: Check for SCP restrictions in both accounts

```bash
# Source account OU
aws organizations list-policies-for-target \
  --target-id ou-xxxx-sourceid \
  --filter SERVICE_CONTROL_POLICY \
  --query 'Policies[].{Name:Name,Id:Id}'

# Target account OU
aws organizations list-policies-for-target \
  --target-id ou-xxxx-targetid \
  --filter SERVICE_CONTROL_POLICY \
  --query 'Policies[].{Name:Name,Id:Id}'
```

Expected output: policy names. Retrieve each policy with `aws organizations describe-policy --policy-id <id>` and inspect for `Deny` on `sts:AssumeRole`.

### Step 8: Validate OIDC provider for federated (IRSA/GitHub Actions) assumption

```bash
# List providers
aws iam list-open-id-connect-providers

# Inspect the provider
aws iam get-open-id-connect-provider \
  --open-id-connect-provider-arn arn:aws:iam::222222222222:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABCDEF1234 \
  --query '{URL:Url,Audiences:ClientIDList,Thumbprints:ThumbprintList}'
```

Expected output: `URL` matches the EKS cluster's OIDC issuer (`aws eks describe-cluster --name <name> --query 'cluster.identity.oidc.issuer'`). `Audiences` contains `sts.amazonaws.com`. Thumbprint must match the certificate currently served at the issuer endpoint.

## Causes

### Cause A: Caller principal not listed in the role's trust policy

**Statement:** The calling IAM principal (user, role, or service) is absent from the `Principal` element of the target role's trust policy.

**Mechanism:** AWS evaluates the trust policy as a resource-based policy attached to the role. If the `Principal` does not match the caller's ARN or account, `AssumeRole` is denied before any identity-based policy is consulted. A previously valid trust entry becomes invalid if the original principal is deleted and re-created, because IAM replaces the ARN with the principal's unique ID; re-creation generates a new unique ID that no longer matches the stored entry.

**Indicator:**

- [Step 2] `Principal` in the trust policy does not contain the caller's ARN or account returned by Step 1
- [Step 5] `errorMessage` contains `is not authorized to perform: sts:AssumeRole`

<!-- match: {"step": 5, "predicate": "contains", "target": "is not authorized to perform: sts:AssumeRole"} -->

**Mitigation:**

- **Risk:** Temporarily adding `arn:aws:iam::111111111111:root` as principal allows any principal in that account to assume the role during the window.
- **Command:**

  ```bash
  aws iam update-assume-role-policy --role-name TargetRole --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"AWS": "arn:aws:iam::111111111111:root"},
      "Action": "sts:AssumeRole"
    }]
  }'
  ```

- **Duration:** Up to 4 hours; restore the scoped principal before leaving.

**Resolution:**

```bash
aws iam update-assume-role-policy --role-name TargetRole --policy-document '{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::111111111111:role/CallerRole"},
    "Action": "sts:AssumeRole"
  }]
}'
```

- **Impact:** Trust policy change is effective within seconds; no restart required. Scoped to `TargetRole` only.
- **Rollback:** Re-run `update-assume-role-policy` replacing the ARN with the prior principal or reverting to the previous policy document retrieved in Step 2.

**Verification:** Run `aws sts assume-role --role-arn arn:aws:iam::222222222222:role/TargetRole --role-session-name verify` and confirm `Credentials.AccessKeyId` is returned with no error.

---

### Cause B: Missing or incorrect ExternalId in the AssumeRole call

**Statement:** The target role's trust policy requires an `sts:ExternalId` condition but the caller's `AssumeRole` request omits it or supplies the wrong value.

**Mechanism:** ExternalId is a confused-deputy countermeasure for cross-account third-party access. When `StringEquals: {sts:ExternalId: "expected-id"}` is present in the trust policy's `Condition` block, AWS evaluates that condition before granting assumption. If the caller omits `--external-id` or passes a different string, the condition fails and the request is denied even if the `Principal` matches.

**Indicator:**

- [Step 2] Trust policy `Condition` block contains `"sts:ExternalId"` key
- [Step 5] `errorMessage` contains `is not authorized to perform: sts:AssumeRole` and Step 2 confirms `sts:ExternalId` condition present

<!-- match: {"step": 2, "predicate": "contains", "target": "sts:ExternalId"} -->

**Mitigation:**

- **Risk:** Passing the correct external ID in the CLI call is safe; it does not change any AWS resource.
- **Command:**

  ```bash
  aws sts assume-role \
    --role-arn arn:aws:iam::222222222222:role/TargetRole \
    --role-session-name test-session \
    --external-id "your-external-id-value"
  ```

- **Duration:** Permanent fix — update all callers to include the external ID.

**Resolution:**

```bash
# If callers cannot be updated and the external ID requirement must be relaxed:
aws iam update-assume-role-policy --role-name TargetRole --policy-document '{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::111111111111:role/CallerRole"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "correct-external-id"}}
  }]
}'
```

- **Impact:** Removing the ExternalId condition weakens confused-deputy protection. Only do this if the caller is an internal account you fully control.
- **Rollback:** Re-add the `Condition` block with the original ExternalId value.

**Verification:** Run `aws sts assume-role --role-arn ... --external-id correct-external-id --role-session-name verify` and confirm temporary credentials are returned.

---

### Cause C: Session duration exceeds MaxSessionDuration

**Statement:** The `DurationSeconds` value requested by the caller exceeds the `MaxSessionDuration` configured on the target role, or role chaining limits the session to one hour.

**Mechanism:** AWS validates `DurationSeconds` against the role's `MaxSessionDuration` (900–43200 s; default 3600 s). When a role is assumed via role chaining (an assumed-role session calling AssumeRole again), AWS enforces a hard cap of 3600 s regardless of `MaxSessionDuration`. Either condition causes a parameter-validation rejection before trust-policy evaluation.

**Indicator:**

- [Step 3] `MaxSessionDuration` value is less than the `DurationSeconds` the caller requests
- [Step 5] `errorMessage` contains `DurationSeconds exceeds the MaxSessionDuration`

<!-- match: {"step": 5, "predicate": "contains", "target": "DurationSeconds exceeds the MaxSessionDuration"} -->

**Mitigation:**

- **Risk:** Increasing `MaxSessionDuration` means compromised credentials remain valid longer.
- **Command:**

  ```bash
  # Reduce the caller's requested duration below current MaxSessionDuration
  aws sts assume-role \
    --role-arn arn:aws:iam::222222222222:role/TargetRole \
    --role-session-name test-session \
    --duration-seconds 3600
  ```

- **Duration:** Immediate — verify this unblocks the caller, then decide whether to increase MaxSessionDuration.

**Resolution:**

```bash
# Increase MaxSessionDuration (max 43200 = 12 hours)
aws iam update-role --role-name TargetRole --max-session-duration 14400
```

- **Impact:** Affects all future sessions for this role; does not invalidate existing sessions.
- **Rollback:** `aws iam update-role --role-name TargetRole --max-session-duration 3600`

**Verification:** Run `aws sts assume-role ... --duration-seconds <new-value>` and confirm `Credentials.Expiration` reflects the requested duration.

---

### Cause D: Caller's identity-based policy does not grant sts:AssumeRole

**Statement:** The calling IAM principal has no identity-based policy that explicitly allows `sts:AssumeRole` on the target role ARN, and the trust policy alone is insufficient for cross-account assumption.

**Mechanism:** For cross-account assumption both gates must pass: the trust policy must list the caller as a trusted principal, AND the caller must have an identity-based (or inline) policy granting `sts:AssumeRole` on the target role's ARN. For same-account assumption either gate is sufficient. If the caller is in a different account than the role and no identity policy grants the action, the call is denied even when the trust policy is correct.

**Indicator:**

- [Step 4] `EvalDecision` is `implicitDeny`
- [Step 6] No attached or inline policy grants `sts:AssumeRole` on the target role ARN

<!-- match: {"step": 4, "predicate": "contains", "target": "implicitDeny"} -->

**Mitigation:**

- **Risk:** Attaching a wildcard resource (`"Resource": "*"`) to the policy allows assumption of any role; scope to the specific ARN.
- **Command:**

  ```bash
  aws iam put-role-policy \
    --role-name CallerRole \
    --policy-name AllowAssumeTargetRole \
    --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": "sts:AssumeRole",
        "Resource": "arn:aws:iam::222222222222:role/TargetRole"
      }]
    }'
  ```

- **Duration:** Permanent until the policy is removed or modified.

**Resolution:** **Same as Mitigation.**

- **Impact:** Grants the caller role permission to assume one specific role; scoped to `CallerRole` only.
- **Rollback:** `aws iam delete-role-policy --role-name CallerRole --policy-name AllowAssumeTargetRole`

**Verification:** Re-run Step 4 and confirm `EvalDecision` is `allowed`, then verify `aws sts assume-role` succeeds.

---

### Cause E: Service Control Policy (SCP) blocks sts:AssumeRole

**Statement:** An SCP attached to the source or target account's organizational unit explicitly denies `sts:AssumeRole`, overriding any identity-based or trust-policy allow.

**Mechanism:** SCPs act as guardrails on AWS accounts in AWS Organizations. An SCP with `Effect: Deny` on `sts:AssumeRole` takes precedence over all identity-based and resource-based policies in the affected account. The CloudTrail error message includes the phrase `with an explicit deny in a service control policy` when an SCP is responsible. The `simulate-principal-policy` command returns `explicitDeny` but does not identify SCPs by name.

**Indicator:**

- [Step 4] `EvalDecision` is `explicitDeny`
- [Step 5] `errorMessage` contains `explicit deny in a service control policy`

<!-- match: {"step": 5, "predicate": "contains", "target": "explicit deny in a service control policy"} -->

**Mitigation:**

- **Risk:** Modifying an SCP affects every account attached to that OU. Coordinate with your organization administrator.
- **Command:**

  ```bash
  # Identify the blocking SCP
  aws organizations describe-policy --policy-id p-xxxxxxxxxxxx \
    --query 'Policy.Content' --output text | python3 -m json.tool
  ```

- **Duration:** Read-only; escalate to the Organizations admin to modify or detach the SCP.

**Resolution:**

```bash
# Organizations admin: update the SCP to add an exception for the specific role ARN
# or move the target account to an OU with a less restrictive SCP:
aws organizations move-account \
  --account-id 222222222222 \
  --source-parent-id ou-xxxx-source \
  --destination-parent-id ou-xxxx-destination
```

- **Impact:** Moving an account changes all SCPs applied to it. Review all SCP effects before proceeding.
- **Rollback:** `aws organizations move-account` back to the original OU.

**Verification:** Re-run Step 4 and confirm `EvalDecision` changes to `allowed`, then verify `aws sts assume-role` succeeds.

---

### Cause F: STS not activated in the target region

**Statement:** The `sts:AssumeRole` call is made against an STS endpoint in an AWS region where STS is not activated for the account.

**Mechanism:** By default, STS is available in the global endpoint (`sts.amazonaws.com`) and a subset of regional endpoints. If the caller or SDK is configured to use a regional STS endpoint (`sts.<region>.amazonaws.com`) in a region that has not been activated for the account, the request returns `RegionDisabled`. This is common when `AWS_DEFAULT_REGION` is set to a newer region or when SDKs are pinned to a regional endpoint.

**Indicator:**

- [Step 5] `errorCode` is `RegionDisabled` or error message contains `STS is not activated in the requested region`
- [Symptom] Error class is `RegionDisabled` (HTTP 403), not `AccessDenied`

<!-- match: {"step": 5, "predicate": "contains", "target": "STS is not activated in the requested region"} -->

**Mitigation:**

- **Risk:** Switching to the global endpoint removes regional isolation benefits; safe for most use cases.
- **Command:**

  ```bash
  # Redirect the call to the global endpoint
  aws sts assume-role \
    --role-arn arn:aws:iam::222222222222:role/TargetRole \
    --role-session-name test-session \
    --endpoint-url https://sts.amazonaws.com
  ```

- **Duration:** Immediate; use the global endpoint until the regional endpoint is activated.

**Resolution:**

```bash
# Activate STS in the target region (requires IAM console or AWS CLI as account root)
aws iam set-security-token-service-preferences \
  --global-endpoint-token-version v2Token
# Or activate via IAM console: Account settings -> STS endpoints -> activate region
```

- **Impact:** STS activation is account-wide and cannot be scoped to individual roles.
- **Rollback:** Deactivate the regional endpoint via IAM console: Account settings -> STS endpoints.

**Verification:** Run `aws sts assume-role` without the `--endpoint-url` override and confirm it succeeds in the target region.

---

### Cause G: OIDC provider mismatch for federated assumption (IRSA/GitHub Actions)

**Statement:** The OIDC identity provider ARN, issuer URL, or audience registered in IAM does not match the token presented by the federated caller, causing `AssumeRoleWithWebIdentity` to be rejected.

**Mechanism:** EKS IRSA and GitHub Actions OIDC federation require an OIDC provider registered in IAM whose URL exactly matches the token issuer field. The trust policy's `Condition` block uses `StringEquals` on `token.actions.githubusercontent.com:sub` (GitHub) or `oidc.eks.{region}.amazonaws.com/id/{cluster-id}:sub` (EKS). A mismatch on any of: provider URL, thumbprint validity, `ClientIDList` (audience), or `sub`/`aud` claim values in the token causes `AccessDenied` on `AssumeRoleWithWebIdentity`.

**Indicator:**

- [Step 8] `URL` in the OIDC provider does not match the token issuer, or `ClientIDList` does not include `sts.amazonaws.com`
- [Step 2] Trust policy `Condition` `StringEquals` subject/audience values differ from what the provider token contains

<!-- match: {"step": 8, "predicate": "absent", "target": "sts.amazonaws.com"} -->

**Mitigation:**

- **Risk:** Updating trust policy `Condition` values may block other federated callers relying on the old value.
- **Command:**

  ```bash
  # Retrieve the actual EKS OIDC issuer to compare
  aws eks describe-cluster --name my-cluster \
    --query 'cluster.identity.oidc.issuer' --output text
  ```

- **Duration:** Diagnostic only; use the output to correct the trust policy or OIDC provider registration.

**Resolution:**

```bash
# Update OIDC provider thumbprint if the certificate has rotated
aws iam update-open-id-connect-provider-thumbprint \
  --open-id-connect-provider-arn arn:aws:iam::222222222222:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABCDEF1234 \
  --thumbprint-list NEWTHUMBPRINT1234567890

# Re-register the provider if the URL is wrong
aws iam delete-open-id-connect-provider \
  --open-id-connect-provider-arn arn:aws:iam::222222222222:oidc-provider/wrong.issuer.url

aws iam create-open-id-connect-provider \
  --url https://oidc.eks.us-east-1.amazonaws.com/id/ABCDEF1234 \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list THUMBPRINT1234567890
```

- **Impact:** OIDC provider deletion/re-creation immediately breaks all roles that reference the old provider ARN; update trust policies before deleting.
- **Rollback:** Re-create the original provider and revert trust policy `Condition` values.

**Verification:** Run `aws sts assume-role-with-web-identity --role-arn ... --web-identity-token file://token.jwt --role-session-name verify` and confirm credentials are returned. For EKS, verify the ServiceAccount annotation matches the updated role ARN and the pod can obtain credentials.

---

### Cause H: Role chaining session hard-capped at one hour

**Statement:** An assumed-role session is attempting to assume another role (role chaining), but the `DurationSeconds` parameter exceeds the AWS-enforced one-hour limit for chained sessions.

**Mechanism:** When a principal calls `AssumeRole` using temporary credentials (i.e., the caller's `Arn` contains `assumed-role`), AWS imposes a hard ceiling of 3600 seconds regardless of the target role's `MaxSessionDuration`. This differs from Cause C (caller has long-lived credentials requesting a session longer than `MaxSessionDuration`). The restriction exists to limit the blast radius of compromised chained-role credentials.

**Indicator:**

- [Step 1] Caller `Arn` contains `assumed-role` (i.e., already a temporary credential session)
- [Step 5] `errorMessage` contains `DurationSeconds exceeds the MaxSessionDuration` and Step 3 shows `MaxSessionDuration` >= requested duration

<!-- match: {"step": 1, "predicate": "contains", "target": "assumed-role"} -->

**Mitigation:**

- **Risk:** None — reducing the requested `DurationSeconds` to <=3600 resolves the error without policy changes.
- **Command:**

  ```bash
  aws sts assume-role \
    --role-arn arn:aws:iam::222222222222:role/TargetRole \
    --role-session-name chained-session \
    --duration-seconds 3600
  ```

- **Duration:** Permanent — role chaining one-hour limit is non-configurable.

**Resolution:** **Same as Mitigation.**

**Verification:** Confirm `Credentials.Expiration` is approximately one hour from now and no error is returned.

---

### Cause Z: Unidentified role assumption failure

**Statement:** The `AssumeRole` call fails but none of the preceding causes are confirmed by diagnostic output.

**Mechanism:** Uncommon root causes include: trust policy date-range conditions (`DateGreaterThan`/`DateLessThan`) that have expired, MFA conditions (`aws:MultiFactorAuthPresent: true`) not satisfied by the caller, IP-restriction conditions (`aws:SourceIp`) that exclude the caller's egress IP, source-identity requirements, or a malformed trust policy document rejected at upload time. These require CloudTrail event inspection and manual policy-condition review.

**Indicator:**

- [Default] None of Causes A-H match diagnostic output

**Mitigation:**

- **Risk:** Broadening the trust policy temporarily is the safest path to confirm whether a condition block is the culprit.
- **Command:**

  ```bash
  # Pull the full CloudTrail event to see the exact errorMessage
  aws cloudtrail lookup-events \
    --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRole \
    --start-time "$(date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
    --query 'Events[].CloudTrailEvent' \
    --output text | python3 -m json.tool | grep -A2 '"errorMessage"'
  ```

- **Duration:** Read-only; escalate based on the `errorMessage` content.

**Resolution:** Out of runbook scope — resolve based on the specific condition identified in the CloudTrail `errorMessage`. Consult AWS IAM troubleshoot_roles documentation for condition-key-specific guidance.

**Verification:** After targeted remediation, verify `aws sts assume-role` returns `Credentials` with no error.

## Prevention

1. **Use specific principal ARNs in trust policies.** Prefer `arn:aws:iam::111111111111:role/SpecificRole` over `arn:aws:iam::111111111111:root`. Wildcard-account principals grant assumption to any future role in that account.

2. **Always require ExternalId for cross-account third-party roles.** Generate a UUID per trust relationship and add `"Condition": {"StringEquals": {"sts:ExternalId": "<uuid>"}}` to every cross-account trust policy where a vendor or partner assumes your role.

3. **Set MaxSessionDuration to the minimum required.** Default 3600 s is appropriate for CI/CD automation. Only increase for interactive human sessions (consult/break-glass scenarios up to 43200 s).

4. **Monitor AssumeRole failures with a CloudWatch Metric Filter:**

   ```bash
   aws logs put-metric-filter \
     --log-group-name CloudTrail/ManagementEvents \
     --filter-name AssumeRoleFailures \
     --filter-pattern '{ $.eventName = "AssumeRole" && $.errorCode = "AccessDenied" }' \
     --metric-transformations \
       metricName=AssumeRoleAccessDenied,metricNamespace=Security,metricValue=1
   ```

5. **Version-control trust policies in IaC.** Store trust policy documents in Terraform or CloudFormation to track changes and enable rollback. Enable IAM Access Analyzer to flag overly permissive trust policies (roles reachable from outside your organization).

6. **For EKS IRSA, validate OIDC provider health after cluster upgrades.** Run Step 8 after every EKS control-plane upgrade; thumbprint rotation can silently break pod-level role assumption.

7. **Avoid role chaining where possible.** Role chaining imposes a hard one-hour session ceiling. If a workload needs sessions longer than one hour, have it assume the final role directly using long-lived credentials or an instance profile rather than via an intermediate role.

## Sources

- [Troubleshoot IAM roles — AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot_roles.html) — primary troubleshooting reference; used for all "I can't assume a role" causes and role chaining limit
- [AssumeRole API Reference — AWS STS](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html) — DurationSeconds constraints, ExternalId parameter, error codes (ExpiredToken, MalformedPolicyDocument, PackedPolicyTooLarge, RegionDisabled), request/response structure
- [The confused deputy problem — AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html) — ExternalId design rationale, cross-account and cross-service confused deputy prevention patterns
- [Access to AWS accounts owned by third parties — AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-user_externalid.html) — ExternalId best practices, multi-tenant external ID uniqueness, when to require ExternalId
- [IAM roles for service accounts (IRSA) — AWS EKS User Guide](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html) — OIDC provider setup, ServiceAccount annotation, trust policy condition keys for EKS
