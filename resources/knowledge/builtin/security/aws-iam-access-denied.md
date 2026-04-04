---
id: aws-iam-access-denied
title: "AWS IAM Access Denied Errors"
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
  - access-denied
  - policy
  - cloudtrail
  - scp
  - permissions-boundary
difficulty: intermediate
---

# AWS IAM Access Denied Errors

## Problem Definition

Applies to all AWS accounts using IAM for access control. Requires IAM read permissions (`iam:Get*`, `iam:List*`), CloudTrail read access, and access to the AWS Organizations API if SCPs are in scope. Relevant across all AWS SDK versions and CLI v2+.

AWS IAM Access Denied errors occur when an API call is rejected by the IAM policy evaluation engine. The caller receives an HTTP 403 with an error such as:

```
User: arn:aws:iam::123456789012:user/deploy-bot is not authorized to perform: s3:PutObject
on resource: arn:aws:s3:::my-bucket/* with an explicit deny in a service control policy
```

There are two denial types:

- **Explicit deny** — a `Deny` statement in any applicable policy matches the request. An explicit deny always overrides any `Allow` in any other policy.
- **Implicit deny** — no `Deny` exists, but no `Allow` exists either. IAM defaults to deny.

AWS evaluates policies in layers. All of the following must permit the action for it to succeed:

1. **Service Control Policies (SCPs)** — organization-level guardrails on member accounts.
2. **Resource Control Policies (RCPs)** — organization-level controls on resource access.
3. **VPC Endpoint Policies** — restrict which actions are allowed through a VPC endpoint.
4. **Identity-based policies** — managed or inline policies attached to the IAM user, group, or role.
5. **Resource-based policies** — policies on the target resource (S3 bucket policy, SNS topic policy, etc.).
6. **Permissions boundaries** — maximum permission envelope for an IAM entity.
7. **Session policies** — inline or managed policies passed during `sts:AssumeRole` or federation.

Key combination rules:

- Identity-based + resource-based (same account) = **union** (either can grant).
- Identity-based + permissions boundary = **intersection** (both must allow).
- Identity-based + SCP = **intersection** (both must allow).
- Cross-account access = **both** identity-based in the calling account AND resource-based in the target account must allow.

## Diagnostic Steps

### Step 1. Identify the caller and the denied action

Extracts the principal ARN, action, and resource from the error message. Confirms which credentials are actually in use, catching cases where an unexpected role or user is active.

```bash
aws sts get-caller-identity
```

Expected output shows Account, UserId, and ARN. If the ARN does not match the expected principal, the wrong credentials are in use (common with chained role assumptions or environment variable overrides).

### Step 2. Look up the denied event in CloudTrail

Retrieves the full API event record including the exact `errorCode` and `errorMessage` from CloudTrail, which names the specific policy type responsible for the denial.

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=PutObject \
  --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?contains(CloudTrailEvent, `AccessDenied`)].{Time:EventTime,Event:CloudTrailEvent}' \
  --output json
```

Expected output contains events with `errorCode: AccessDenied` or `Client.UnauthorizedAccess`. The `errorMessage` field often names the specific policy type and ARN responsible. If no events appear, widen the time range or check the correct region.

For high-volume accounts, use Athena over the CloudTrail S3 bucket:

```bash
# Run in Athena console or via aws athena start-query-execution
# SELECT eventtime, eventsource, eventname, errorcode, errormessage,
#        useridentity.arn, requestparameters
# FROM cloudtrail_logs
# WHERE errorcode = 'AccessDenied'
#   AND eventtime > '2026-03-26T00:00:00Z'
# ORDER BY eventtime DESC LIMIT 50;
```

### Step 3. List and inspect all policies attached to the caller

Enumerates every identity-based policy (inline and managed) on the caller to find missing Allow statements or unexpected Deny statements.

For an IAM role:

```bash
aws iam list-attached-role-policies --role-name MyRole
aws iam list-role-policies --role-name MyRole
aws iam get-role-policy --role-name MyRole --policy-name InlinePolicy
```

For an IAM user:

```bash
aws iam list-attached-user-policies --user-name deploy-bot
aws iam list-user-policies --user-name deploy-bot
```

Expected output lists policy ARNs and inline policy names. If the required action and resource are not present in any Allow statement, the identity-based policy is the cause.

### Step 4. Check the permissions boundary

Determines whether a permissions boundary is restricting the effective permissions below what the identity-based policies allow.

```bash
aws iam get-role --role-name MyRole --query 'Role.PermissionsBoundary'
```

If a boundary ARN is returned, retrieve and inspect it:

```bash
aws iam get-policy-version \
  --policy-arn arn:aws:iam::123456789012:policy/BoundaryPolicy \
  --version-id v1 \
  --query 'PolicyVersion.Document'
```

If the boundary does not include the required action, the effective permission is denied regardless of what the identity-based policies allow.

### Step 5. Check Service Control Policies (SCPs)

Identifies whether an organization-level SCP is blocking the action. SCPs apply to the entire account and override identity-based policies.

```bash
aws organizations list-policies-for-target \
  --target-id ou-abc1-23456789 \
  --filter SERVICE_CONTROL_POLICY

aws organizations describe-policy --policy-id p-abcdef1234
```

An SCP must explicitly allow the action (unless using the default FullAWSAccess SCP), and must not explicitly deny it. If the SCP contains a Deny for the action or omits it from an Allow-list, this is the cause.

### Step 6. Check resource-based policies

Examines the policy on the target resource to find explicit denials or missing cross-account grants.

For S3:

```bash
aws s3api get-bucket-policy --bucket my-bucket --query 'Policy' --output text | python3 -m json.tool
```

For KMS:

```bash
aws kms get-key-policy --key-id alias/my-key --policy-name default --output text | python3 -m json.tool
```

If the caller is in a different account, the resource-based policy must explicitly grant access to the caller's principal ARN.

### Step 7. Simulate the permission with IAM Policy Simulator

Runs the full IAM policy evaluation engine against the caller's policies and returns the allow/deny decision with matched statements.

```bash
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::123456789012:role/MyRole \
  --action-names s3:PutObject \
  --resource-arns arn:aws:s3:::my-bucket/key.txt \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision,MatchedStatements:MatchedStatements}'
```

Expected output shows `allowed`, `implicitDeny`, or `explicitDeny` with matched statement details. An `implicitDeny` means no policy grants the action; an `explicitDeny` identifies the blocking statement.

### Step 8. Check VPC endpoint policy (if applicable)

Determines whether a VPC endpoint policy is restricting the actions that can pass through the endpoint.

```bash
aws ec2 describe-vpc-endpoints \
  --vpc-endpoint-ids vpce-0123456789abcdef0 \
  --query 'VpcEndpoints[].PolicyDocument' --output text | python3 -m json.tool
```

If the endpoint policy does not include the required action, traffic routed through the endpoint will be denied even if all other policies allow it.

### Step 9. Check session policies (for assumed roles)

Identifies whether a session policy passed during AssumeRole is over-restricting the effective permissions.

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRole \
  --start-time "$(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[].CloudTrailEvent' --output text | python3 -m json.tool | grep -A 20 '"policy"'
```

If a session policy is present, the effective permissions are the intersection of the role's identity-based policies and the session policy.

## Mitigation

### Option 1: Temporarily attach a broader managed policy

**Risk**: ReadOnlyAccess grants read permissions across all AWS services. This is broader than what the caller may need and violates least-privilege. Only suitable for unblocking read operations during investigation.

**Command**:

```bash
aws iam attach-role-policy \
  --role-name MyRole \
  --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess
```

**Verify**:

```bash
aws sts get-caller-identity
aws s3 ls s3://my-bucket/
```

**Duration**: Remove within 24 hours after root cause is resolved.

### Option 2: Move account out of restrictive SCP (if SCP is blocking)

**Risk**: Moving an account to a less restrictive OU removes all SCP guardrails from that OU. Other workloads in the account may gain unintended permissions.

**Command**:

```bash
aws organizations move-account \
  --account-id 123456789012 \
  --source-parent-id ou-restricted-abc \
  --destination-parent-id ou-unrestricted-def
```

**Verify**: Re-run the failing API call from the affected account and confirm it succeeds.

**Duration**: Move the account back to the original OU within 4 hours. Document the temporary move in your incident channel.

### Option 3: Replace permissions boundary (if boundary is blocking)

**Risk**: PowerUserAccess boundary allows all actions except IAM and Organizations management. This is significantly broader than most production boundaries.

**Command**:

```bash
aws iam put-role-permissions-boundary \
  --role-name MyRole \
  --permissions-boundary arn:aws:iam::aws:policy/PowerUserAccess
```

**Verify**: Re-run the failing API call and confirm it succeeds.

**Duration**: Restore the original permissions boundary within 24 hours after root cause is resolved.

## Root Cause Resolution

**If** the identity-based policy is missing the required action or resource:

```bash
aws iam create-policy \
  --policy-name AllowS3PutObject \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::my-bucket/*"
    }]
  }'

aws iam attach-role-policy \
  --role-name MyRole \
  --policy-arn arn:aws:iam::123456789012:policy/AllowS3PutObject
```

**If** an explicit deny in an identity-based policy is blocking → create a new policy version with the deny statement removed or narrowed:

```bash
aws iam create-policy-version \
  --policy-arn arn:aws:iam::123456789012:policy/RestrictivePolicy \
  --policy-document file://updated-policy.json \
  --set-as-default
```

**If** an SCP is blocking (implicit deny — missing Allow) → update the SCP to include the required action:

```bash
aws organizations update-policy \
  --policy-id p-abcdef1234 \
  --content file://updated-scp.json
```

**If** an SCP is blocking (explicit deny) → remove or narrow the Deny statement in the SCP:

```bash
aws organizations update-policy \
  --policy-id p-abcdef1234 \
  --content file://updated-scp-no-deny.json
```

**If** the permissions boundary does not include the required action → update the boundary policy:

```bash
aws iam create-policy-version \
  --policy-arn arn:aws:iam::123456789012:policy/BoundaryPolicy \
  --policy-document file://updated-boundary.json \
  --set-as-default
```

**If** a resource-based policy is blocking cross-account access → update the resource policy to grant the caller's principal:

```bash
aws s3api put-bucket-policy --bucket my-bucket --policy '{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::111122223333:role/CrossAccountRole"},
    "Action": "s3:PutObject",
    "Resource": "arn:aws:s3:::my-bucket/*"
  }]
}'
```

**If** a VPC endpoint policy is restricting access → update the endpoint policy:

```bash
aws ec2 modify-vpc-endpoint \
  --vpc-endpoint-id vpce-0123456789abcdef0 \
  --policy-document file://updated-vpce-policy.json
```

**If** a session policy is over-restricting assumed role permissions → update the application code or CI/CD pipeline to pass a broader session policy (or no session policy) when calling `sts:AssumeRole`.

**If** condition keys are mismatched (e.g., missing required tags) → ensure the request includes the tags or conditions required by the policy (for example, `aws:RequestTag/Environment` = `production`).

**If** IAM eventual consistency is the cause → wait 60 seconds and retry. IAM changes propagate globally but are eventually consistent.

## Verification

1. Re-run the original failing command and confirm it succeeds.

2. Simulate the permission to confirm the evaluation result:

```bash
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::123456789012:role/MyRole \
  --action-names s3:PutObject \
  --resource-arns arn:aws:s3:::my-bucket/key.txt \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}'
```

Expected output: `"Decision": "allowed"`.

3. Verify in CloudTrail that subsequent API calls succeed (no `AccessDenied` errorCode):

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=PutObject \
  --start-time "$(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?!contains(CloudTrailEvent, `AccessDenied`)].{Time:EventTime,User:Username}'
```

4. Remove any temporary mitigation policies that were applied:

```bash
aws iam detach-role-policy \
  --role-name MyRole \
  --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess
```

## Prevention

1. **Use IAM Access Analyzer to validate policies before deployment**:

```bash
aws accessanalyzer validate-policy \
  --policy-type IDENTITY_POLICY \
  --policy-document file://my-policy.json
```

Review all findings of type `ERROR` and `SECURITY_WARNING` before attaching.

2. **Adopt least-privilege with IAM Access Analyzer policy generation** — generate policies based on CloudTrail activity to avoid over-permissioning:

```bash
aws accessanalyzer start-policy-generation \
  --policy-generation-details '{"principalArn": "arn:aws:iam::123456789012:role/MyRole"}' \
  --cloud-trail-details '{"accessRole": "arn:aws:iam::123456789012:role/AccessAnalyzerRole", "trailArn": "arn:aws:cloudtrail:us-east-1:123456789012:trail/management-events"}'
```

3. **Test permission changes in a non-production OU first** before applying SCPs organization-wide.

4. **Use the IAM Policy Simulator in CI/CD** to validate that deployments have the required permissions before release:

```bash
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::123456789012:role/DeployRole \
  --action-names s3:PutObject s3:GetObject ecs:UpdateService \
  --resource-arns '*' \
  --query 'EvaluationResults[?EvalDecision!=`allowed`]'
```

5. **Set up CloudWatch alarms on AccessDenied events** for early detection:

```bash
aws cloudwatch put-metric-alarm \
  --alarm-name IAMAccessDeniedAlarm \
  --metric-name Errors \
  --namespace AWS/IAM \
  --statistic Sum \
  --period 300 \
  --threshold 10 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 1 \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:ops-alerts
```

6. **Use tag-based access control consistently** — apply `aws:RequestTag` and `aws:ResourceTag` conditions to avoid tag-related denials.

7. **Document all SCPs and permissions boundaries** in your organization's runbook repository so teams can self-diagnose without escalation.

## Sources

- [Troubleshoot access denied error messages - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot_access-denied.html)
- [Troubleshoot IAM - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot.html)
- [Troubleshoot IAM policies - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot_policies.html)
- [Policy evaluation logic - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_evaluation-logic.html)
- [How AWS enforcement code evaluates requests - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_evaluation-logic_policy-eval-denyallow.html)
- [Permissions boundaries for IAM entities - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html)
- [Service control policies (SCPs) - AWS Organizations](https://docs.aws.amazon.com/organizations/latest/userguide/orgs_manage_policies_scps.html)
- [Troubleshoot IAM permission errors - AWS re:Post](https://repost.aws/knowledge-center/troubleshoot-iam-permission-errors)
- [Troubleshoot IAM policy issues - AWS re:Post](https://repost.aws/knowledge-center/troubleshoot-iam-policy-issues)
