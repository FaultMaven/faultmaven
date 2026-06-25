---
id: "aws-iam-role-assumption-failure"
title: "AWS IAM Role Assumption Failure"
domain: security
service: aws-iam
symptom_class: [auth_failure]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

### Step 4: Simulate the caller's identity-based policy

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

### Step 6: Check caller's identity policy grants AssumeRole

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

### Step 8: Validate OIDC provider for federated assumption

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

### Cause A: Caller principal not in trust policy

**Statement:** The calling IAM principal (user, role, or service) is absent from the `Principal` element of the target role's trust policy.

**Chain:**
- root: the caller's ARN or account is missing from the target role's trust-policy `Principal` element (or its prior unique-ID was invalidated by principal delete/re-create).
- s1: AWS evaluates the trust policy (resource-based) first and finds no matching `Principal`, so `AssumeRole` is denied before any identity policy is consulted.
- D: `sts:AssumeRole` returns `AccessDenied` (points at Symptom Recognition).

**Indicators:**
- root: [Step 2] `Principal` in the trust policy does not contain the caller's ARN or account returned by Step 1
- s1: [Step 5] `errorMessage` contains `is not authorized to perform: sts:AssumeRole`
  <!-- match: {"step": 5, "predicate": "contains", "target": "is not authorized to perform: sts:AssumeRole"} -->

**Interventions:**
- **remediation** (root): add the specific caller principal to the trust policy.

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

  **Verification:** Run `aws sts assume-role --role-arn arn:aws:iam::222222222222:role/TargetRole --role-session-name verify` and confirm `Credentials.AccessKeyId` is returned with no error.
- **mitigation** (root): temporarily trust the whole source account to unblock while the scoped entry is prepared.

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

  **Risk:** Trusting `arn:aws:iam::111111111111:root` allows any principal in that account to assume the role during the window. **Duration:** Up to 4 hours; restore the scoped principal before leaving. **Verification:** Run `aws sts assume-role --role-arn ... --role-session-name verify` and confirm credentials are returned.

### Cause B: Missing or incorrect ExternalId

**Statement:** The target role's trust policy requires an `sts:ExternalId` condition but the caller's `AssumeRole` request omits it or supplies the wrong value.

**Chain:**
- root: the trust policy's `Condition` block requires `StringEquals: {sts:ExternalId: "expected-id"}` for confused-deputy protection.
- s1: the caller omits `--external-id` or passes a different string, so AWS evaluates the condition and it fails even though the `Principal` matches.
- D: `sts:AssumeRole` is denied with `AccessDenied` (points at Symptom Recognition).

**Indicators:**
- root: [Step 2] Trust policy `Condition` block contains `"sts:ExternalId"` key
  <!-- match: {"step": 2, "predicate": "contains", "target": "sts:ExternalId"} -->
- s1: [Step 5] `errorMessage` contains `is not authorized to perform: sts:AssumeRole` while Step 2 confirms the `sts:ExternalId` condition is present

**Interventions:**
- **remediation** (root): update all callers to pass the correct external ID on every `AssumeRole` call.

  ```bash
  aws sts assume-role \
    --role-arn arn:aws:iam::222222222222:role/TargetRole \
    --role-session-name test-session \
    --external-id "your-external-id-value"
  ```

  **Verification:** Run `aws sts assume-role --role-arn ... --external-id correct-external-id --role-session-name verify` and confirm temporary credentials are returned.
- **mitigation** (root): if callers cannot be updated, relax the required external ID on the trust policy.

  ```bash
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

  **Risk:** Removing the ExternalId condition weakens confused-deputy protection; only do this for an internal account you fully control. **Duration:** Until callers are corrected; re-add the original `Condition` block to restore protection. **Verification:** Re-run `aws sts assume-role` with the corrected external ID and confirm credentials are returned.

### Cause C: Session duration exceeds MaxSessionDuration

**Statement:** The `DurationSeconds` value requested by the caller exceeds the `MaxSessionDuration` configured on the target role.

**Chain:**
- root: the caller requests a `DurationSeconds` larger than the role's `MaxSessionDuration` (valid range 900–43200 s; default 3600 s).
- s1: AWS validates `DurationSeconds` against `MaxSessionDuration` during parameter validation, before trust-policy evaluation, and rejects the call.
- D: `sts:AssumeRole` fails with `DurationSeconds exceeds the MaxSessionDuration` (points at Symptom Recognition).

**Indicators:**
- root: [Step 3] `MaxSessionDuration` value is less than the `DurationSeconds` the caller requests
- s1: [Step 5] `errorMessage` contains `DurationSeconds exceeds the MaxSessionDuration`
  <!-- match: {"step": 5, "predicate": "contains", "target": "DurationSeconds exceeds the MaxSessionDuration"} -->

**Interventions:**
- **remediation** (root): raise the role's `MaxSessionDuration` to cover the required duration (max 43200 s = 12 h).

  ```bash
  aws iam update-role --role-name TargetRole --max-session-duration 14400
  ```

  **Verification:** Run `aws sts assume-role ... --duration-seconds <new-value>` and confirm `Credentials.Expiration` reflects the requested duration.
- **mitigation** (root): reduce the caller's requested duration below the current `MaxSessionDuration` to unblock immediately.

  ```bash
  aws sts assume-role \
    --role-arn arn:aws:iam::222222222222:role/TargetRole \
    --role-session-name test-session \
    --duration-seconds 3600
  ```

  **Risk:** None to security; the caller simply gets a shorter session and must refresh sooner. **Duration:** Immediate; verify this unblocks the caller, then decide whether to increase MaxSessionDuration. **Verification:** Confirm credentials are returned with the requested shorter duration.

### Cause D: Caller's identity policy does not grant AssumeRole

**Statement:** For cross-account assumption the caller has no identity-based policy granting `sts:AssumeRole` on the target role ARN, and the trust policy alone is insufficient.

**Chain:**
- root: for cross-account assumption, BOTH the target trust policy must trust the caller AND the caller must have an identity policy granting `sts:AssumeRole` on the target ARN; the identity grant is missing.
- s1: with no identity policy granting the action, IAM evaluates the request as an implicit deny even when the trust policy is correct.
- D: `sts:AssumeRole` returns `AccessDenied` (points at Symptom Recognition).

**Indicators:**
- root: [Step 6] No attached or inline policy grants `sts:AssumeRole` on the target role ARN
- s1: [Step 4] `EvalDecision` is `implicitDeny`
  <!-- match: {"step": 4, "predicate": "contains", "target": "implicitDeny"} -->

**Interventions:**
- **remediation** (root): attach a scoped identity policy granting `sts:AssumeRole` on the target role ARN.

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

  **Verification:** Re-run Step 4 and confirm `EvalDecision` is `allowed`, then verify `aws sts assume-role` succeeds.

### Cause E: Service Control Policy blocks AssumeRole

**Statement:** An SCP attached to the source or target account's organizational unit explicitly denies `sts:AssumeRole`, overriding any identity-based or trust-policy allow.

**Chain:**
- root: an SCP with `Effect: Deny` on `sts:AssumeRole` is attached to an OU governing the source or target account.
- s1: in AWS Organizations an explicit SCP deny takes precedence over all identity-based and resource-based allows in the affected account.
- D: `sts:AssumeRole` is denied with `explicit deny in a service control policy` (points at Symptom Recognition).

**Indicators:**
- root: [Step 4] `EvalDecision` is `explicitDeny`
- s1: [Step 5] `errorMessage` contains `explicit deny in a service control policy`
  <!-- match: {"step": 5, "predicate": "contains", "target": "explicit deny in a service control policy"} -->

**Interventions:**
- **remediation** (root): have the Organizations admin add an exception to the SCP or move the target account to a less restrictive OU.

  ```bash
  aws organizations move-account \
    --account-id 222222222222 \
    --source-parent-id ou-xxxx-source \
    --destination-parent-id ou-xxxx-destination
  ```

  **Verification:** Re-run Step 4 and confirm `EvalDecision` changes to `allowed`, then verify `aws sts assume-role` succeeds.
- **mitigation** (root): read-only — identify the blocking SCP so the admin can act; this does not unblock by itself.

  ```bash
  aws organizations describe-policy --policy-id p-xxxxxxxxxxxx \
    --query 'Policy.Content' --output text | python3 -m json.tool
  ```

  **Risk:** None — read-only inspection; any actual SCP change affects every account attached to that OU and needs the Organizations admin. **Duration:** Read-only; escalate to the Organizations admin to modify or detach the SCP. **Verification:** Confirm the dumped policy content shows the `Deny` on `sts:AssumeRole`.

### Cause F: STS not activated in the target region

**Statement:** The `sts:AssumeRole` call is made against an STS endpoint in an AWS region where STS is not activated for the account.

**Chain:**
- root: the caller or SDK is configured to use a regional STS endpoint (`sts.<region>.amazonaws.com`) in a region not activated for the account.
- s1: STS rejects the regional request because that endpoint is inactive for the account, returning `RegionDisabled` (HTTP 403) rather than `AccessDenied`.
- D: the `AssumeRole` call fails with `STS is not activated in the requested region` (points at Symptom Recognition).

**Indicators:**
- root: [Step 5] `errorCode` is `RegionDisabled` or error message contains `STS is not activated in the requested region`
  <!-- match: {"step": 5, "predicate": "contains", "target": "STS is not activated in the requested region"} -->
- s1: [Symptom] Error class is `RegionDisabled` (HTTP 403), not `AccessDenied`

**Interventions:**
- **remediation** (root): activate STS in the target region account-wide (IAM console or CLI as account root).

  ```bash
  aws iam set-security-token-service-preferences \
    --global-endpoint-token-version v2Token
  # Or activate via IAM console: Account settings -> STS endpoints -> activate region
  ```

  **Verification:** Run `aws sts assume-role` without the `--endpoint-url` override and confirm it succeeds in the target region.
- **mitigation** (root): redirect the call to the global STS endpoint until the regional endpoint is activated.

  ```bash
  aws sts assume-role \
    --role-arn arn:aws:iam::222222222222:role/TargetRole \
    --role-session-name test-session \
    --endpoint-url https://sts.amazonaws.com
  ```

  **Risk:** Switching to the global endpoint removes regional isolation benefits; safe for most use cases. **Duration:** Immediate; use the global endpoint until the regional endpoint is activated. **Verification:** Confirm `aws sts assume-role` against the global endpoint returns credentials.

### Cause G: OIDC provider mismatch for federated assumption

**Statement:** The OIDC identity provider ARN, issuer URL, or audience registered in IAM does not match the token presented by the federated caller, rejecting `AssumeRoleWithWebIdentity`.

**Chain:**
- root: the registered OIDC provider URL, thumbprint, `ClientIDList` (audience), or the trust policy `sub`/`aud` condition does not match the token the federated caller (EKS IRSA / GitHub Actions) presents.
- s1: AWS validates the web-identity token against the registered provider and trust-policy condition; the mismatch fails validation.
- D: `AssumeRoleWithWebIdentity` is denied with `AccessDenied` (points at Symptom Recognition).

**Indicators:**
- root: [Step 8] `URL` in the OIDC provider does not match the token issuer, or `ClientIDList` does not include `sts.amazonaws.com`
  <!-- match: {"step": 8, "predicate": "absent", "target": "sts.amazonaws.com"} -->
- s1: [Step 2] Trust policy `Condition` `StringEquals` subject/audience values differ from what the provider token contains

**Interventions:**
- **remediation** (root): correct the OIDC provider thumbprint, or re-register the provider with the right URL/audience.

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

  **Verification:** Run `aws sts assume-role-with-web-identity --role-arn ... --web-identity-token file://token.jwt --role-session-name verify` and confirm credentials are returned. For EKS, verify the ServiceAccount annotation matches the updated role ARN and the pod can obtain credentials.
- **mitigation** (root): read-only — retrieve the actual EKS OIDC issuer to compare against the registered provider before changing anything.

  ```bash
  aws eks describe-cluster --name my-cluster \
    --query 'cluster.identity.oidc.issuer' --output text
  ```

  **Risk:** None — read-only; later updates to trust policy `Condition` values may block other federated callers relying on the old value. **Duration:** Diagnostic only; use the output to correct the trust policy or OIDC provider registration. **Verification:** Confirm the returned issuer matches (or reveals the mismatch with) the `URL` from Step 8.

### Cause H: Role chaining session hard-capped at one hour

**Statement:** An assumed-role session is attempting to assume another role (role chaining), but the `DurationSeconds` parameter exceeds the AWS-enforced one-hour limit for chained sessions.

**Chain:**
- root: the caller is already operating with temporary credentials (its `Arn` contains `assumed-role`) and requests a chained session longer than 3600 s.
- s1: AWS imposes a hard 3600 s ceiling on role chaining regardless of the target role's `MaxSessionDuration`, to limit the blast radius of compromised chained credentials.
- D: the chained `AssumeRole` is rejected with `DurationSeconds exceeds the MaxSessionDuration` (points at Symptom Recognition).

**Indicators:**
- root: [Step 1] Caller `Arn` contains `assumed-role` (i.e., already a temporary credential session)
  <!-- match: {"step": 1, "predicate": "contains", "target": "assumed-role"} -->
- s1: [Step 5] `errorMessage` contains `DurationSeconds exceeds the MaxSessionDuration` while Step 3 shows `MaxSessionDuration` >= the requested duration

**Interventions:**
- **remediation** (root): request a chained session of at most 3600 s; the one-hour limit is non-configurable.

  ```bash
  aws sts assume-role \
    --role-arn arn:aws:iam::222222222222:role/TargetRole \
    --role-session-name chained-session \
    --duration-seconds 3600
  ```

  **Verification:** Confirm `Credentials.Expiration` is approximately one hour from now and no error is returned.

### Cause Z: Unidentified

**Statement:** The `AssumeRole` call fails but none of the preceding causes are confirmed by diagnostic output.

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot (full CloudTrail event with the exact `errorMessage`) and escalate to the IAM SME.

  ```bash
  aws cloudtrail lookup-events \
    --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRole \
    --start-time "$(date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
    --query 'Events[].CloudTrailEvent' \
    --output text | python3 -m json.tool | grep -A2 '"errorMessage"'
  ```

  **Risk:** Read-only snapshot capture; no resource change. Broadening the trust policy to test a condition block must be done only by the SME under change control. **Duration:** Read-only; escalate based on the `errorMessage` content. **Verification:** After targeted SME remediation, verify `aws sts assume-role` returns `Credentials` with no error.

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
