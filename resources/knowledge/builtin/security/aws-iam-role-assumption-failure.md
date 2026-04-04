---
id: aws-iam-role-assumption-failure
title: "AWS IAM Role Assumption Failures"
domain: security
service: aws-iam
symptom_class:
  - auth_failure
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - aws
  - iam
  - assume-role
  - trust-policy
  - cross-account
  - external-id
  - sts
difficulty: intermediate
---

# AWS IAM Role Assumption Failures

## Problem Definition

Applies to all AWS accounts using `sts:AssumeRole`, `sts:AssumeRoleWithSAML`, or `sts:AssumeRoleWithWebIdentity`. Requires IAM read permissions on the target role and CloudTrail access in both source and target accounts. Relevant across AWS CLI v2+, all AWS SDKs, and EKS IRSA/Pod Identity configurations.

Role assumption failures occur when an `sts:AssumeRole` call is rejected. The caller receives one of these errors:

```
An error occurred (AccessDenied) when calling the AssumeRole operation:
User: arn:aws:iam::111111111111:user/deployer is not authorized to perform: sts:AssumeRole
on resource: arn:aws:iam::222222222222:role/TargetRole
```

```
An error occurred (MalformedPolicyDocument) when calling the AssumeRole operation:
Invalid principal in policy: "AWS":"arn:aws:iam::111111111111:role/NonExistent"
```

```
An error occurred (AccessDenied) when calling the AssumeRole operation:
The requested DurationSeconds exceeds the MaxSessionDuration set for this role.
```

Common failure categories:

- **Trust policy misconfiguration** — the role's trust policy does not permit the calling principal.
- **Missing external ID** — the trust policy requires an `sts:ExternalId` condition that the caller did not supply.
- **Session duration exceeded** — the requested session length exceeds `MaxSessionDuration` on the target role.
- **Cross-account SCP blocking** — an SCP in either the source or target account blocks `sts:AssumeRole`.
- **Confused deputy** — the trust policy is too broad and the external ID condition is absent or incorrect.
- **OIDC/SAML provider mismatch** — the federated identity provider ARN or audience does not match the trust policy.

## Diagnostic Steps

### Step 1. Confirm the caller identity

Verifies which principal is actually making the AssumeRole call. Mismatched credentials are the most common cause of assumption failures.

```bash
aws sts get-caller-identity
```

Expected output shows `Account`, `UserId`, and `Arn`. If the ARN does not match the principal listed in the target role's trust policy, the wrong credentials are in use.

### Step 2. Retrieve the target role's trust policy

Reads the trust relationship document on the target role to identify which principals are allowed to assume it and under what conditions.

```bash
aws iam get-role --role-name TargetRole \
  --query 'Role.AssumeRolePolicyDocument' --output json | python3 -m json.tool
```

Expected output is a JSON policy document with `Statement` entries. Check that the `Principal` field includes the caller's ARN or account, and that any `Condition` blocks (ExternalId, StringEquals, etc.) match what the caller is providing.

### Step 3. Check the MaxSessionDuration on the target role

Determines whether the requested session duration exceeds the role's configured maximum.

```bash
aws iam get-role --role-name TargetRole \
  --query 'Role.MaxSessionDuration'
```

Default is 3600 seconds (1 hour). If the caller requests a `DurationSeconds` value greater than this, the call fails. Maximum configurable value is 43200 seconds (12 hours).

### Step 4. Look up the failed AssumeRole event in CloudTrail

Retrieves the full event record with the exact error message, which often specifies whether the failure is due to the trust policy, SCP, or session duration.

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRole \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?contains(CloudTrailEvent, `AccessDenied`)].{Time:EventTime,Event:CloudTrailEvent}' \
  --output json
```

The `errorMessage` field in the CloudTrail event differentiates between trust policy denials, SCP denials, and parameter validation failures.

### Step 5. Verify the caller has sts:AssumeRole permission

Checks whether the caller's identity-based policies grant `sts:AssumeRole` on the target role ARN. Even if the trust policy allows the caller, the caller must also have the action permitted.

```bash
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::111111111111:role/CallerRole \
  --action-names sts:AssumeRole \
  --resource-arns arn:aws:iam::222222222222:role/TargetRole \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}'
```

If the result is `implicitDeny` or `explicitDeny`, the caller needs an identity-based policy granting `sts:AssumeRole` on the target role ARN.

### Step 6. Check SCPs in both accounts

SCPs in the source account can block `sts:AssumeRole` outbound, and SCPs in the target account can block inbound role assumption.

```bash
# Source account SCPs
aws organizations list-policies-for-target \
  --target-id ou-source-abc \
  --filter SERVICE_CONTROL_POLICY

# Target account SCPs
aws organizations list-policies-for-target \
  --target-id ou-target-def \
  --filter SERVICE_CONTROL_POLICY
```

If either account's SCP denies `sts:AssumeRole`, the call fails regardless of identity-based and trust policies.

### Step 7. Validate OIDC/SAML provider configuration (if federated)

Checks that the identity provider ARN and audience/subject conditions in the trust policy match the actual token being presented.

For OIDC (e.g., EKS IRSA):

```bash
aws iam list-open-id-connect-providers
aws iam get-open-id-connect-provider \
  --open-id-connect-provider-arn arn:aws:iam::222222222222:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABCDEF1234
```

Verify that the provider URL matches the EKS cluster's OIDC issuer and that the `ClientIDList` includes `sts.amazonaws.com`. For SAML, verify the provider ARN and audience restriction match the trust policy.

## Mitigation

### Option 1: Temporarily broaden the trust policy

**Risk**: Adding a wildcard account principal (`"AWS": "arn:aws:iam::111111111111:root"`) allows any principal in that account to assume the role. This should only be used to unblock while diagnosing the specific principal issue.

**Command**:

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

**Verify**:

```bash
aws sts assume-role \
  --role-arn arn:aws:iam::222222222222:role/TargetRole \
  --role-session-name test-session
```

**Duration**: Restore the original trust policy within 4 hours. The broad principal should never remain in production.

### Option 2: Increase MaxSessionDuration (if duration is the issue)

**Risk**: Longer sessions mean credentials remain valid longer if compromised. Evaluate whether the workload genuinely requires extended sessions.

**Command**:

```bash
aws iam update-role --role-name TargetRole --max-session-duration 14400
```

**Verify**: Re-run the AssumeRole call with the original DurationSeconds value and confirm it succeeds.

**Duration**: Permanent if the workload requires it. Otherwise, reduce after the immediate need passes.

## Root Cause Resolution

**If** the trust policy does not list the caller's principal → add the specific principal ARN to the trust policy:

```bash
aws iam update-assume-role-policy --role-name TargetRole --policy-document '{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::111111111111:role/CallerRole"},
    "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"sts:ExternalId": "UniqueExternalId123"}}
  }]
}'
```

**If** the external ID is missing or incorrect → update the caller's AssumeRole call to include the correct ExternalId, or update the trust policy condition to match the ID the caller provides.

**If** the caller's identity-based policy does not grant sts:AssumeRole → attach a policy granting the action:

```bash
aws iam put-role-policy --role-name CallerRole --policy-name AllowAssumeTarget \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::222222222222:role/TargetRole"
    }]
  }'
```

**If** the session duration exceeds MaxSessionDuration → either reduce the requested DurationSeconds or increase the role's MaxSessionDuration:

```bash
aws iam update-role --role-name TargetRole --max-session-duration 7200
```

**If** an SCP blocks sts:AssumeRole → update the SCP to allow the action, or move the account to an OU with a less restrictive SCP.

**If** the OIDC provider URL or audience is wrong → update the provider or the trust policy to match:

```bash
aws iam update-open-id-connect-provider-thumbprint \
  --open-id-connect-provider-arn arn:aws:iam::222222222222:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABCDEF1234 \
  --thumbprint-list NEWTHUMBPRINT1234
```

**If** the trust policy references a deleted or renamed principal → IAM replaces the principal ARN with a unique ID. Recreate the principal with the same name, or update the trust policy to reference the new ARN.

## Verification

1. Re-run the AssumeRole call and confirm temporary credentials are returned:

```bash
aws sts assume-role \
  --role-arn arn:aws:iam::222222222222:role/TargetRole \
  --role-session-name verify-session \
  --query 'Credentials.{AccessKeyId:AccessKeyId,Expiration:Expiration}'
```

Expected output includes an `AccessKeyId` and `Expiration` timestamp.

2. Use the assumed role credentials to perform the downstream action that originally failed:

```bash
export AWS_ACCESS_KEY_ID=<from-above>
export AWS_SECRET_ACCESS_KEY=<from-above>
export AWS_SESSION_TOKEN=<from-above>
aws sts get-caller-identity
```

The ARN should show the assumed role.

3. Verify CloudTrail shows successful AssumeRole events (no `AccessDenied`):

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRole \
  --start-time "$(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?!contains(CloudTrailEvent, `AccessDenied`)].{Time:EventTime}'
```

4. Remove any temporary trust policy broadening applied during mitigation.

## Prevention

1. **Always use external IDs for cross-account roles** to prevent confused deputy attacks. Generate unique external IDs per trust relationship.

2. **Use specific principal ARNs in trust policies** instead of account-wide roots. For example, `arn:aws:iam::111111111111:role/SpecificRole` instead of `arn:aws:iam::111111111111:root`.

3. **Set MaxSessionDuration to the minimum required** for the workload. Default 1 hour is appropriate for most CI/CD and automation use cases.

4. **Monitor AssumeRole failures with CloudWatch Metrics filters**:

```bash
aws logs put-metric-filter \
  --log-group-name CloudTrail/ManagementEvents \
  --filter-name AssumeRoleFailures \
  --filter-pattern '{ $.eventName = "AssumeRole" && $.errorCode = "AccessDenied" }' \
  --metric-transformations metricName=AssumeRoleAccessDenied,metricNamespace=Security,metricValue=1
```

5. **Use IAM Access Analyzer to identify overly broad trust policies** — Access Analyzer flags roles that are accessible from outside your organization.

6. **For EKS IRSA, validate the OIDC provider** matches the cluster's issuer URL and that ServiceAccount annotations reference the correct role ARN.

7. **Version-control trust policies** in Terraform or CloudFormation to track changes and enable rollback.

## Sources

- [Troubleshoot IAM roles - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot_roles.html)
- [AssumeRole API reference - AWS STS](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html)
- [IAM role trust policy - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_terms-and-concepts.html)
- [Cross-account access with roles - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/tutorial_cross-account-with-roles.html)
- [The confused deputy problem - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html)
- [IAM roles for service accounts (IRSA) - AWS EKS User Guide](https://docs.aws.amazon.com/eks/latest/userguide/iam-roles-for-service-accounts.html)
- [Troubleshoot STS errors - AWS re:Post](https://repost.aws/knowledge-center/iam-assume-role-error)
