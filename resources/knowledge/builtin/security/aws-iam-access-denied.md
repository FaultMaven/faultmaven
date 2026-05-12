---
id: aws-iam-access-denied
title: "AWS IAM Access Denied Across AWS APIs"
domain: security
service: aws-iam
symptom_class:
  - auth_failure
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: kb-researcher
status: draft
tags:
  - aws
  - iam
  - access-denied
  - "403"
  - scp
  - permissions-boundary
  - vpc-endpoint
  - session-policy
  - passrole
  - cloudtrail
difficulty: intermediate
---

# AWS IAM Access Denied Across AWS APIs

## Symptom Recognition

- AWS SDK, CLI, or console returns an HTTP 403 with `errorCode=AccessDenied`, `Client.UnauthorizedOperation`, or `AccessDeniedException`; service-specific codes include `UnauthorizedOperation` (EC2), `AccessDeniedException` (Lambda, STS, Secrets Manager, ECS, EKS, KMS, SageMaker), and `AuthorizationError` (SNS).
- Same-account callers receive the enhanced-context message in the format `User <arn> is not authorized to perform: <action> on resource: <resource-arn> because <context>` or `... with an explicit deny in a <policy-type>`; the `<context>` clause names exactly which policy layer denied the request.
- The enhanced-context strings are stable and machine-matchable. Implicit-deny phrasings: `because no identity-based policy allows the <action> action`, `because no resource-based policy allows the <action> action`, `because no VPC endpoint policy allows the <action> action`, `because no permissions boundary allows the <action> action`, `because no service control policy allows the <action> action`, `because no session policy allows the <action> action`, `because no role trust policy allows the sts:AssumeRole action`.
- Explicit-deny phrasings: `with an explicit deny in an identity-based policy`, `with an explicit deny in a resource-based policy`, `with an explicit deny in a service control policy`, `with an explicit deny in a resource control policy`, `with an explicit deny in a VPC endpoint policy`, `with an explicit deny in a permissions boundary`, `with an explicit deny in a session policy`, `with an explicit deny in the role trust policy`. Each may optionally append `: <policy-arn>` naming the offending policy.
- `iam:PassRole` failures surface during service-configuration calls (e.g., `ec2:RunInstances` with `IamInstanceProfile`, `lambda:CreateFunction`/`lambda:UpdateFunctionConfiguration` with `Role`, `ecs:RegisterTaskDefinition` with `taskRoleArn`/`executionRoleArn`, `codepipeline:CreatePipeline`, `rds:CreateDBInstance` with monitoring role) with the message `User: <arn> is not authorized to perform: iam:PassRole on resource: <role-arn>`.
- CloudTrail records the failed call with `errorCode=AccessDenied` (or service-specific equivalent), `errorMessage` containing the enhanced context string, and `eventSource` identifying the called service (e.g., `ec2.amazonaws.com`, `lambda.amazonaws.com`, `ecs.amazonaws.com`, `sts.amazonaws.com`, `secretsmanager.amazonaws.com`); the `userIdentity` block names the calling principal.
- Cross-account or cross-organization callers receive a generic `Access Denied` with no `<context>` clause; the enhanced message is only emitted to the resource owner's account or organization, so external callers must coordinate with the resource owner to read the detailed CloudTrail entry.
- Intermittent denial immediately after a policy change is the signature of IAM eventual consistency — the same call succeeds 30–120 seconds later with no further intervention.

## Applicability

- All AWS commercial and GovCloud Regions, every AWS service that uses IAM authorization (effectively all services except a small set of anonymous-allowed S3 operations).
- AWS CLI v2.x, AWS SDK v2/v3 in any language, console, Cognito-federated and IAM Identity Center sessions, EKS IRSA/Pod Identity, ECS task roles, Lambda execution roles, and EC2 instance profiles.
- Diagnostic permissions required on the investigator: `iam:Get*`, `iam:List*`, `iam:SimulatePrincipalPolicy`, `iam:SimulateCustomPolicy`, `cloudtrail:LookupEvents`, `organizations:ListPoliciesForTarget` and `organizations:DescribePolicy` (only when SCP/RCP is suspected), `ec2:DescribeVpcEndpoints` (only when VPC endpoint is suspected), `sts:GetCallerIdentity` for the caller's session.
- Tooling: `aws` CLI (v2.x), `jq` for JSON parsing, `python3` for pretty-printing. Network reachability to `iam.amazonaws.com`, `sts.amazonaws.com`, and the called service's regional endpoint.
- Out of scope: S3-specific denial paths (Block Public Access, Object Ownership, bucket policies, KMS-on-S3) — see runbook `aws-s3-access-denied`. STS-specific assume-role trust-policy failures — see runbook `aws-iam-role-assumption-failure`.

## Diagnostic Steps

### Step 1: Capture the exact error message and CloudTrail entry

```bash
# Re-run the failing call with debug output and persist the full error to disk
aws <service> <operation> <args> 2>&1 | tee /tmp/aws-error.txt
# Locate the matching CloudTrail event in the last 15 minutes
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=<OperationName> \
  --start-time "$(date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --max-results 20 \
  --query 'Events[?contains(CloudTrailEvent, `AccessDenied`)].CloudTrailEvent' \
  --output text | head -200
```

Expected output: the CLI/SDK error string from the failing call, then one or more CloudTrail event JSON blobs whose `errorCode` is `AccessDenied`, `UnauthorizedOperation`, or `AccessDeniedException`. For same-account requests, the CloudTrail `errorMessage` contains the same enhanced-context string the SDK printed — the `because no <type> policy allows` or `with an explicit deny in a <type> policy` clause names the responsible policy layer. Save the entire error string verbatim; the substring after `because` or `with an explicit deny in` selects which Cause applies.

### Step 2: Confirm the caller identity and target action

```bash
aws sts get-caller-identity
echo "Action: <service:Operation>"
echo "Resource: <resource-arn>"
```

Expected output: `Account`, `UserId`, and `Arn` of the caller's current session. The `Arn` must match the principal that the diagnostic policies in subsequent steps will reference; a stale `AWS_PROFILE`, an unintended assumed-role session, or an instance/container metadata fallback pointing at the wrong role is the most common false start. Note whether the ARN is an IAM user (`arn:aws:iam::...:user/...`), an assumed role session (`arn:aws:sts::...:assumed-role/...`), or a federated session (`arn:aws:sts::...:federated-user/...`).

### Step 3: Simulate the call against the caller's identity-based policies

```bash
aws iam simulate-principal-policy \
  --policy-source-arn <caller-role-or-user-arn> \
  --action-names <service:Operation> \
  --resource-arns <resource-arn> \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision,MatchedStatements:MatchedStatements[].SourcePolicyId,Organizations:OrganizationsDecisionDetail.AllowedByOrganizations,Boundary:PermissionsBoundaryDecisionDetail.AllowedByPermissionsBoundary}' \
  --output json
```

Expected output: a JSON array with one entry per action. `Decision=allowed` means every policy layer the simulator can see permits the action. `Decision=implicitDeny` means no identity-based policy grants the action (no matching Allow); the request hits IAM's default-deny. `Decision=explicitDeny` means an attached identity-based policy, permissions boundary, or SCP explicitly blocks it; `MatchedStatements` names the policy. The `OrganizationsDecisionDetail.AllowedByOrganizations` flag is `false` when an SCP denies the call; `PermissionsBoundaryDecisionDetail.AllowedByPermissionsBoundary` is `false` when a boundary blocks it.

### Step 4: List and inspect every identity-based policy attached to the caller

```bash
# For an IAM role
aws iam list-attached-role-policies --role-name <role-name>
aws iam list-role-policies --role-name <role-name>
for p in $(aws iam list-role-policies --role-name <role-name> --query 'PolicyNames[]' --output text); do
  echo "=== inline: $p ==="
  aws iam get-role-policy --role-name <role-name> --policy-name "$p" --query 'PolicyDocument'
done
# For an IAM user
aws iam list-attached-user-policies --user-name <user-name>
aws iam list-user-policies --user-name <user-name>
aws iam list-groups-for-user --user-name <user-name>
```

Expected output: the full set of managed-policy ARNs, inline-policy names, and (for users) group memberships. Cross-reference each policy's `Action` and `Resource` against the failing call. Absence of any Allow statement matching `<service:Operation>` on `<resource-arn>` is the signature of Cause A (identity-policy implicit deny). Presence of a `Deny` statement matching the call is the signature of Cause B (identity-policy explicit deny).

### Step 5: Check whether a permissions boundary applies to the caller

```bash
# For a role
aws iam get-role --role-name <role-name> --query 'Role.PermissionsBoundary'
# For a user
aws iam get-user --user-name <user-name> --query 'User.PermissionsBoundary'
# If a boundary ARN is returned, retrieve its current default version
BOUNDARY_ARN=$(aws iam get-role --role-name <role-name> --query 'Role.PermissionsBoundary.PermissionsBoundaryArn' --output text)
DEFAULT_VERSION=$(aws iam get-policy --policy-arn "$BOUNDARY_ARN" --query 'Policy.DefaultVersionId' --output text)
aws iam get-policy-version --policy-arn "$BOUNDARY_ARN" --version-id "$DEFAULT_VERSION" --query 'PolicyVersion.Document'
```

Expected output: either `null` (no boundary, skip Cause C) or `{"PermissionsBoundaryType": "PermissionsBoundaryPolicy", "PermissionsBoundaryArn": "<arn>"}` followed by the boundary's policy document. The boundary must Allow the action; otherwise the effective permission is the intersection (empty) regardless of how permissive the identity-based policies are.

### Step 6: List and inspect Service Control Policies and Resource Control Policies on the account

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws organizations list-policies-for-target --target-id "$ACCOUNT_ID" --filter SERVICE_CONTROL_POLICY \
  --query 'Policies[].{Id:Id,Name:Name,AwsManaged:AwsManaged}'
aws organizations list-policies-for-target --target-id "$ACCOUNT_ID" --filter RESOURCE_CONTROL_POLICY \
  --query 'Policies[].{Id:Id,Name:Name,AwsManaged:AwsManaged}' 2>&1
# Retrieve each non-FullAWSAccess policy's content
for POLICY_ID in <scp-or-rcp-id>; do
  aws organizations describe-policy --policy-id "$POLICY_ID" --query 'Policy.Content' --output text \
    | python3 -m json.tool
done
```

Expected output: the list of SCPs and RCPs in effect for the account, then each policy document. `AWSFullAccess` (`p-FullAWSAccess`) is the default permissive SCP; any other policy may restrict. Look for `"Effect": "Deny"` statements whose `Action` and (for RCP) `Resource` match the failing call, or for `"Effect": "Allow"` lists that omit the action (implicit deny against an allow-list SCP). `AccessDeniedException` from `list-policies-for-target` means the caller lacks `organizations:*` permissions; ask an Organizations admin to run this step.

### Step 7: Check VPC endpoint policy when the caller is inside a VPC

```bash
# Resolve the endpoint that handled the failing call from CloudTrail
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=<OperationName> \
  --start-time "$(date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?contains(CloudTrailEvent, `AccessDenied`)].CloudTrailEvent' --output text \
  | python3 -m json.tool | grep -E '"vpcEndpointId"|"sourceIPAddress"'
# Inspect the endpoint's policy
aws ec2 describe-vpc-endpoints \
  --filters Name=service-name,Values=com.amazonaws.<region>.<service> \
  --query 'VpcEndpoints[].{Id:VpcEndpointId,Type:VpcEndpointType,Policy:PolicyDocument}' \
  --output json > /tmp/vpce.json
grep -nE '"Effect"\s*:\s*"Deny"|"Action"|"Resource"' /tmp/vpce.json || echo "no_explicit_deny"
```

Expected output: the `vpcEndpointId` of the endpoint that handled the call (from CloudTrail), and the JSON policy document of every same-service endpoint in the region. The default endpoint policy is `Action: "*"` on `Resource: "*"`; a custom policy that omits the resource ARN from `Resource` (implicit deny) or includes a `Deny` for the action (explicit deny) blocks any call routed through the endpoint. `null` `vpcEndpointId` in CloudTrail means the call went over the public internet — VPC endpoints are not in scope, skip Cause F.

### Step 8: Check for an active session policy on the caller's STS session

```bash
# Look up the AssumeRole event that minted the current session
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRole \
  --start-time "$(date -u -d '2 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --max-results 50 \
  --query 'Events[?contains(CloudTrailEvent, `<caller-session-name>`)].CloudTrailEvent' \
  --output text | python3 -m json.tool > /tmp/assume-role-event.json
# Search the event for inline or managed session policies
grep -nE '"policy"|"policyArns"' /tmp/assume-role-event.json || echo "no_session_policy_passed"
```

Expected output: the AssumeRole CloudTrail event JSON, with any `requestParameters.policy` (inline session policy document) or `requestParameters.policyArns` (managed session policy ARNs) populated. Empty `no_session_policy_passed` means the caller's permissions are bounded only by the role's identity policies — skip Cause E. A populated `policy` field means the effective permissions are the intersection of the role's identity-based policies AND the session policy; any action missing from the session policy is implicitly denied.

### Step 9: Inspect condition keys on attached policies for context-based denials

```bash
# Dump every identity-based policy document and grep for restrictive conditions
for p in $(aws iam list-attached-role-policies --role-name <role-name> --query 'AttachedPolicies[].PolicyArn' --output text); do
  VER=$(aws iam get-policy --policy-arn "$p" --query 'Policy.DefaultVersionId' --output text)
  aws iam get-policy-version --policy-arn "$p" --version-id "$VER" --query 'PolicyVersion.Document'
done | python3 -m json.tool > /tmp/all-identity-policies.json
grep -nE 'aws:SourceVpc|aws:SourceVpce|aws:SourceIp|aws:RequestTag|aws:ResourceTag|aws:PrincipalOrgID|aws:PrincipalTag|aws:MultiFactorAuthPresent|aws:SecureTransport|aws:CurrentTime|aws:RequestedRegion' /tmp/all-identity-policies.json
```

Expected output: a grep listing of every condition key used in the caller's identity-based policies. Each match is a candidate for Cause D (condition mismatch). Compare each condition's expected value with the actual request context: source VPC/IP, current time, requested region, tags on the principal or request, MFA status, transport protocol. A `StringEquals` or `IpAddress` condition that does not match the request context turns an apparent Allow into an implicit deny.

### Step 10: Check for iam:PassRole when the failing call configures a service

```bash
# Look up the failing call and extract any role ARN it references
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=<OperationName> \
  --start-time "$(date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?contains(CloudTrailEvent, `AccessDenied`)].CloudTrailEvent' --output text \
  | python3 -m json.tool | grep -E '"roleArn"|"iamInstanceProfile"|"executionRoleArn"|"taskRoleArn"|"iam:PassRole"'
# Verify the caller has iam:PassRole on the referenced role
aws iam simulate-principal-policy \
  --policy-source-arn <caller-arn> \
  --action-names iam:PassRole \
  --resource-arns <role-arn-being-passed> \
  --context-entries ContextKeyName=iam:PassedToService,ContextKeyValues=<service>.amazonaws.com,ContextKeyType=string \
  --query 'EvaluationResults[].{Decision:EvalDecision,Matched:MatchedStatements[].SourcePolicyId}'
```

Expected output: the role ARN being passed (from CloudTrail `requestParameters`), and a simulator decision for `iam:PassRole`. If the failing CLI error mentions `iam:PassRole` or the simulator returns `implicitDeny`/`explicitDeny` for `iam:PassRole`, the caller is configuring a service (EC2 instance profile, Lambda execution role, ECS task role, RDS monitoring role, CodePipeline service role) but lacks the permission to attach the named role. CloudTrail does not log `iam:PassRole` as its own event — only the parent call (e.g., `CreateFunction`, `RunInstances`) records the denial.

## Causes

### Cause A: Caller's identity-based policy does not allow the action (implicit deny)

**Statement:** No identity-based policy attached to the calling IAM user, role, group, or session grants the action on the target resource, so AWS defaults to deny because no policy explicitly allows it.

**Mechanism:** IAM evaluates the union of all identity-based policies attached to the caller — managed policies, inline policies, group policies for users — and finds no `Allow` statement whose `Action` and `Resource` match the request. Because IAM is deny-by-default, the absence of any matching Allow is an implicit deny; AWS returns `AccessDenied` with the enhanced context `because no identity-based policy allows the <action> action`. This is the most common Access Denied cause and is the path taken whenever a new principal, action, or resource has not yet been added to the caller's permission set.

**Indicator:**

- [Step 1] error message contains `because no identity-based policy allows`
<!-- match: {"step": 1, "predicate": "contains", "target": "because no identity-based policy allows"} -->
- [Step 3] `simulate-principal-policy` returns `Decision: implicitDeny` for the action
<!-- match: {"step": 3, "predicate": "contains", "target": "implicitDeny"} -->
- [Step 4] no attached policy contains an `"Effect": "Allow"` statement whose `Action` and `Resource` match the failing call

**Mitigation:**

- **Risk:** Attaching a broad managed policy (e.g., `<service>FullAccess` such as `AmazonEC2FullAccess`, `AWSLambda_FullAccess`) grants every action in the service, exceeding least-privilege; use only while the targeted policy in Resolution is authored.
- **Command:**

  ```bash
  aws iam attach-role-policy \
    --role-name <caller-role-name> \
    --policy-arn arn:aws:iam::aws:policy/<ServiceFullAccessPolicy>
  ```

- **Duration:** Up to 24 hours, removed once the targeted policy in Resolution is in place.

**Resolution:**

```bash
cat > /tmp/targeted-allow.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["<service:Operation>"],
      "Resource": ["<resource-arn>"]
    }
  ]
}
EOF
aws iam put-role-policy \
  --role-name <caller-role-name> \
  --policy-name AllowTargetedAction \
  --policy-document file:///tmp/targeted-allow.json
```

**Impact:** Affects only the named role/user. The grant propagates to all regional STS/IAM endpoints within seconds (typically <10 s, occasionally up to 60 s for IAM eventual consistency).

**Rollback:** `aws iam delete-role-policy --role-name <caller-role-name> --policy-name AllowTargetedAction` removes the inline policy. If the temporary mitigation managed policy was attached, also run `aws iam detach-role-policy --role-name <caller-role-name> --policy-arn arn:aws:iam::aws:policy/<ServiceFullAccessPolicy>`.

**Verification:** `aws iam simulate-principal-policy --policy-source-arn <caller-arn> --action-names <service:Operation> --resource-arns <resource-arn>` returns `Decision: allowed`. Re-running the original failing call succeeds with HTTP 200, and CloudTrail records the new call without an `errorCode`.

### Cause B: Identity-based policy contains an explicit Deny matching the request

**Statement:** A `Deny` statement in one of the caller's identity-based policies matches the action or resource and overrides any Allow elsewhere, so the request is blocked even when other policies permit it.

**Mechanism:** When AWS evaluates the caller's identity-based policies, an explicit `Deny` always wins over an explicit `Allow`, regardless of policy ordering or source. Common patterns: a "guardrail" inline policy added during an incident that was never removed; an `iam:DeleteVirtualMFADevice` deny added to prevent self-service MFA removal; a `*FullAccess` allow paired with a `Deny` on a specific resource ARN. The enhanced error message reads `with an explicit deny in an identity-based policy` and may include the offending policy ARN.

**Indicator:**

- [Step 1] error message contains `with an explicit deny in an identity-based policy`
<!-- match: {"step": 1, "predicate": "contains", "target": "with an explicit deny in an identity-based policy"} -->
- [Step 3] `simulate-principal-policy` returns `Decision: explicitDeny` and `MatchedStatements` names an identity-policy ARN (not SCP, not permissions boundary)
<!-- match: {"step": 3, "predicate": "contains", "target": "explicitDeny"} -->
- [Step 4] an attached policy contains an `"Effect": "Deny"` statement whose `Action` and `Resource` match the failing call

**Mitigation:**

- **Risk:** Removing a Deny statement may re-enable access that was intentionally restricted by the security team; coordinate with the policy owner before editing the live default version.
- **Command:**

  ```bash
  POLICY_ARN=<deny-policy-arn>
  VER=$(aws iam get-policy --policy-arn "$POLICY_ARN" --query 'Policy.DefaultVersionId' --output text)
  aws iam get-policy-version --policy-arn "$POLICY_ARN" --version-id "$VER" \
    --query 'PolicyVersion.Document' > /tmp/policy-backup.json
  ```

- **Duration:** Backup only — keep until the corrected policy version in Resolution is verified.

**Resolution:**

```bash
# Edit /tmp/policy-backup.json: delete the Deny statement, or narrow its Condition
# so the caller no longer matches (e.g., add the caller's ARN to a NotPrincipal list).
aws iam create-policy-version \
  --policy-arn <deny-policy-arn> \
  --policy-document file:///tmp/policy-fixed.json \
  --set-as-default
# Old version remains available for rollback until manually deleted.
aws iam list-policy-versions --policy-arn <deny-policy-arn>
```

**Impact:** Every principal that has the modified policy attached is affected. The new default version takes effect within seconds of IAM propagation; existing sessions pick up the change on the next call.

**Rollback:** `aws iam set-default-policy-version --policy-arn <deny-policy-arn> --version-id <prior-version-id>` restores the previous default. Optionally `aws iam delete-policy-version --policy-arn <deny-policy-arn> --version-id <new-version-id>` removes the broken version.

**Verification:** `aws iam simulate-principal-policy ...` now returns `Decision: allowed`. The original failing call succeeds. `aws iam get-policy --policy-arn <deny-policy-arn> --query 'Policy.DefaultVersionId'` shows the new version ID is in effect.

### Cause C: Permissions boundary on the caller does not allow the action

**Statement:** A permissions boundary attached to the caller's IAM user or role caps the effective permissions and does not include the action, so the intersection of identity policies and boundary excludes the action even when the identity policies permit it.

**Mechanism:** A permissions boundary is a managed policy that sets the maximum permissions an IAM entity can have. The effective permissions for a principal are the intersection of identity-based policies and the permissions boundary. An action must be allowed by both; otherwise it is implicitly denied. An explicit Deny in the boundary also overrides any Allow. Permissions boundaries are commonly used to delegate user creation while preventing privilege escalation, and to enforce account-wide allow-lists per team or workload. Boundaries do not constrain resource-based policies that grant directly to an IAM-user ARN (within the same account), but they do constrain access granted to an IAM-role ARN or an STS session.

**Indicator:**

- [Step 1] error message contains `permissions boundary` (either `because no permissions boundary allows` or `with an explicit deny in a permissions boundary`)
<!-- match: {"step": 1, "predicate": "contains", "target": "permissions boundary"} -->
- [Step 3] `simulate-principal-policy` returns `PermissionsBoundaryDecisionDetail.AllowedByPermissionsBoundary: false`
<!-- match: {"step": 3, "predicate": "contains", "target": "AllowedByPermissionsBoundary"} -->
- [Step 5] the caller has a non-null `PermissionsBoundary` whose policy document omits the action from any Allow or includes a Deny for it

**Mitigation:**

- **Risk:** Detaching the permissions boundary removes the entire guardrail and may unintentionally expose other services the boundary was restricting; never detach in production without security-team approval.
- **Command:**

  ```bash
  # Capture the current boundary for restoration
  aws iam get-role --role-name <caller-role-name> --query 'Role.PermissionsBoundary' \
    > /tmp/boundary-backup.json
  ```

- **Duration:** Backup only — keep until the corrected boundary version in Resolution is applied.

**Resolution:**

```bash
# Preferred durable fix: update the boundary policy to include the missing action.
BOUNDARY_ARN=$(aws iam get-role --role-name <caller-role-name> \
  --query 'Role.PermissionsBoundary.PermissionsBoundaryArn' --output text)
cat > /tmp/boundary-updated.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["<existing-allowed-actions>", "<service:Operation>"],
      "Resource": "*"
    }
  ]
}
EOF
aws iam create-policy-version \
  --policy-arn "$BOUNDARY_ARN" \
  --policy-document file:///tmp/boundary-updated.json \
  --set-as-default
```

**Impact:** Every principal sharing this boundary is affected. The change propagates within seconds at the IAM control plane; existing sessions pick up the new boundary on the next authorization decision (no resign needed).

**Rollback:** `aws iam set-default-policy-version --policy-arn "$BOUNDARY_ARN" --version-id <prior-version-id>` reverts the boundary. Or, for a single principal: `aws iam put-role-permissions-boundary --role-name <caller-role-name> --permissions-boundary <prior-boundary-arn>`.

**Verification:** `aws iam simulate-principal-policy ...` returns `AllowedByPermissionsBoundary: true` and `Decision: allowed`. The original failing call succeeds. `aws iam get-policy --policy-arn "$BOUNDARY_ARN" --query 'Policy.DefaultVersionId'` shows the updated version.

### Cause D: Policy Condition keys exclude the caller's request context

**Statement:** A Condition clause on an otherwise-permissive identity-based or resource-based policy does not match the caller's request context (source VPC, source IP, requested region, MFA state, principal/resource tags, time-of-day), turning the Allow into an implicit deny for this specific request.

**Mechanism:** When an IAM policy statement carries a `Condition`, every key in the condition must evaluate true for the statement to apply. Common patterns: `aws:SourceVpc` restricts API calls to a specific VPC; `aws:SourceIp` restricts to corporate CIDR ranges; `aws:RequestedRegion` restricts to a region allow-list; `aws:MultiFactorAuthPresent: true` requires MFA on the session; `aws:RequestTag/<key>` enforces a tag on the request payload; `aws:ResourceTag/<key>` enforces a tag on the target resource; `aws:PrincipalOrgID` restricts to a specific AWS Organization. When a call arrives without the required context (no MFA, wrong region, missing tag), the Allow statement's condition fails and IAM evaluates as if the Allow were absent, producing an implicit deny.

**Indicator:**

- [Step 1] error message contains `because no identity-based policy allows` (the condition-failed Allow falls back to implicit deny) and the caller's policies grammatically appear to permit the action
- [Step 3] `simulate-principal-policy` returns `implicitDeny` despite Step 4 showing a matching Allow statement on paper
- [Step 9] grep shows a `Condition` block on the matching Allow statement that references the caller's request context (`aws:SourceVpc`, `aws:SourceIp`, `aws:RequestedRegion`, `aws:MultiFactorAuthPresent`, `aws:RequestTag/*`, `aws:ResourceTag/*`, `aws:PrincipalOrgID`)
<!-- match: {"step": 9, "predicate": "contains", "target": "aws:"} -->

**Mitigation:**

- **Risk:** Satisfying the condition (e.g., re-running the call from inside the required VPC, re-authenticating with MFA, adding the required tag) carries no IAM risk but may require operational changes (VPN, MFA-enabled session); do not weaken the condition without security review.
- **Command:**

  ```bash
  # Re-establish the request context the condition expects:
  # MFA-enabled session:
  aws sts get-session-token --serial-number <mfa-arn> --token-code <code>
  # Region:
  aws --region <required-region> <service> <operation>
  # Source VPC: run from an instance inside the VPC, or use an interface endpoint
  ```

- **Duration:** Indefinite — this is the correct way to satisfy a Condition; no rollback required.

**Resolution:**

```bash
# Option A: Submit the call from a context that matches the Condition (preferred).
aws --region <region-from-aws:RequestedRegion-allow-list> <service> <operation>
# Option B: Add a tag the policy requires.
aws <service> tag-resource --resource-arn <arn> --tags Key=Environment,Value=production
# Option C: If the Condition is over-restrictive for a legitimate use, narrow it
# via a policy version edit. Example: add a second CIDR to aws:SourceIp.
aws iam create-policy-version --policy-arn <policy-arn> \
  --policy-document file:///tmp/policy-with-broader-condition.json --set-as-default
```

**Impact:** Option A and B affect only the current call/resource. Option C is policy-wide: every caller using this policy gets the relaxed condition; treat as a security-team change.

**Rollback:** For Option C: `aws iam set-default-policy-version --policy-arn <policy-arn> --version-id <prior-version-id>`. Options A and B have no rollback because they did not change configuration.

**Verification:** Re-run the original call with the corrected context; it returns HTTP 200. `aws iam simulate-principal-policy --policy-source-arn <caller> --action-names <action> --resource-arns <resource> --context-entries ContextKeyName=aws:SourceVpc,ContextKeyValues=<vpc-id>,ContextKeyType=string` returns `Decision: allowed` when the simulated context matches the policy.

### Cause E: Session policy passed to AssumeRole over-restricts the role's permissions

**Statement:** A session policy supplied as the `Policy` or `PolicyArns` parameter to `sts:AssumeRole` (or to a federation call) does not allow the action, so the session's effective permissions — the intersection of the role's identity policies and the session policy — exclude the action.

**Mechanism:** Session policies are passed at AssumeRole time and further restrict an assumed role for the lifetime of the session. The session's effective permissions are the intersection of the role's identity-based policies and the session policy (and the permissions boundary, if any). A common cause is CI/CD code or an SDK wrapper that automatically adds a session policy intended to scope the session, but which omits actions the workload actually needs. The enhanced error reads `because no session policy allows the <action> action` (implicit) or `with an explicit deny in a session policy` (explicit).

**Indicator:**

- [Step 1] error message contains `session policy`
<!-- match: {"step": 1, "predicate": "contains", "target": "session policy"} -->
- [Step 8] the AssumeRole CloudTrail event for the current session has a non-empty `requestParameters.policy` or `requestParameters.policyArns`
<!-- match: {"step": 8, "predicate": "contains", "target": "policy"} -->
- [Step 3] `simulate-principal-policy` against the role itself returns `allowed`, but the runtime call still fails — indicating an additional restriction beyond the role's identity policies

**Mitigation:**

- **Risk:** Re-assuming the role without the session policy temporarily restores full role permissions, which may exceed what the workload should have for the duration of the session; use only as a stopgap.
- **Command:**

  ```bash
  # Re-assume the role with no session policy
  aws sts assume-role \
    --role-arn <role-arn> \
    --role-session-name <session-name> \
    --duration-seconds 3600
  # Export the returned credentials into the shell and retry the failing call
  ```

- **Duration:** One session (≤role's MaxSessionDuration). Restore session-policy usage as soon as the durable fix in Resolution lands.

**Resolution:**

```bash
# Edit the caller (CI script, SDK wrapper, IAM Identity Center permission set) to
# either drop the session policy or broaden it to include the missing action.
# Example boto3 fix:
python3 - <<'EOF'
import boto3, json
sts = boto3.client('sts')
session_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": ["<existing-actions>", "<service:Operation>"], "Resource": "*"}
    ]
}
sts.assume_role(
    RoleArn="<role-arn>",
    RoleSessionName="<session-name>",
    Policy=json.dumps(session_policy),
)
EOF
```

**Impact:** Affects only sessions newly minted with the updated session-policy code path. Existing sessions keep the old policy until they expire (≤ `MaxSessionDuration`, default 1 hour).

**Rollback:** Revert the CI/SDK change to restore the prior session policy. Existing sessions pick up the prior behaviour automatically on next AssumeRole.

**Verification:** A fresh AssumeRole produces a session whose `aws sts get-session-token` decoded message no longer includes the missing action under restrictions. Re-run the failing call within that session; it succeeds with HTTP 200.

### Cause F: VPC endpoint policy denies the request implicitly or explicitly

**Statement:** The caller's request traversed a VPC interface or gateway endpoint whose policy does not allow the action on the target resource, so the endpoint rejects the call before it reaches the service.

**Mechanism:** Every VPC endpoint carries a policy that gates which calls may pass through it. If the endpoint policy is left at the default `Action: "*"`, no restriction applies. If the policy is customized to allow only specific actions or resources, calls that fall outside the allow-list (implicit deny) or match an explicit `Deny` are blocked at the endpoint. The CloudTrail event for the failed call carries a `vpcEndpointId` field identifying which endpoint handled it; same-account callers see `because no VPC endpoint policy allows` or `with an explicit deny in a VPC endpoint policy` in the enhanced error.

**Indicator:**

- [Step 1] error message contains `VPC endpoint policy`
<!-- match: {"step": 1, "predicate": "contains", "target": "VPC endpoint policy"} -->
- [Step 7] the CloudTrail event includes a `vpcEndpointId` field naming an endpoint with a non-default policy
- [Step 7] the endpoint's `PolicyDocument` omits the resource ARN from `Resource` or includes a `Deny` matching the action

**Mitigation:**

- **Risk:** Resetting an endpoint policy to allow-all temporarily exposes every resource reachable via the endpoint; only acceptable in a controlled change window with the network/security team.
- **Command:**

  ```bash
  aws ec2 describe-vpc-endpoints --vpc-endpoint-ids <vpce-id> \
    --query 'VpcEndpoints[0].PolicyDocument' --output text > /tmp/vpce-policy.backup.json
  ```

- **Duration:** Backup only; do not roll forward without the corrected policy ready.

**Resolution:**

```bash
cat > /tmp/vpce-policy.fixed.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": "*",
      "Action": ["<service:Operation>", "<existing-allowed-actions>"],
      "Resource": ["<resource-arn>", "<existing-allowed-resources>"]
    }
  ]
}
EOF
aws ec2 modify-vpc-endpoint --vpc-endpoint-id <vpce-id> \
  --policy-document file:///tmp/vpce-policy.fixed.json
```

**Impact:** Affects every workload that sends traffic for this service through this endpoint. The change is applied by the endpoint within seconds; no client restart is needed.

**Rollback:** `aws ec2 modify-vpc-endpoint --vpc-endpoint-id <vpce-id> --policy-document file:///tmp/vpce-policy.backup.json`.

**Verification:** `aws ec2 describe-vpc-endpoints --vpc-endpoint-ids <vpce-id> --query 'VpcEndpoints[0].PolicyDocument'` shows the updated policy. A test call from an instance inside the VPC succeeds, and the CloudTrail entry shows the same `vpcEndpointId` with no `errorCode`.

### Cause G: Service Control Policy or Resource Control Policy denies the action

**Statement:** An AWS Organizations Service Control Policy (SCP) attached to the caller's account or organizational unit, or a Resource Control Policy (RCP) attached to the target resource's account, denies the action and overrides any same-account IAM Allow.

**Mechanism:** SCPs cap the maximum permissions for every principal in a member account; an action must be allowed by every SCP attached up the OU hierarchy. RCPs (introduced in 2024) apply at the resource side, scoping what the resource's resource-based policy and identity-based access can permit. An action denied at either layer fails regardless of how permissive the identity-based policies are. The enhanced error reads `because no service control policy allows` or `with an explicit deny in a service control policy` (similarly for `resource control policy`); the simulator returns `OrganizationsDecisionDetail.AllowedByOrganizations: false`.

**Indicator:**

- [Step 1] error message contains `service control policy` or `resource control policy`
<!-- match: {"step": 1, "predicate": "contains", "target": "service control policy"} -->
- [Step 3] `simulate-principal-policy` returns `OrganizationsDecisionDetail.AllowedByOrganizations: false`
<!-- match: {"step": 3, "predicate": "contains", "target": "AllowedByOrganizations"} -->
- [Step 6] an SCP or RCP on the account contains a `"Effect": "Deny"` matching the action, or an Allow-list SCP omits it

**Mitigation:**

- **Risk:** SCP/RCP changes apply organization-wide and must go through the security/governance team; do not bypass. The only safe live mitigation is to identify the offending policy and escalate.
- **Command:**

  ```bash
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  aws organizations list-policies-for-target --target-id "$ACCOUNT_ID" \
    --filter SERVICE_CONTROL_POLICY \
    --query 'Policies[?Name!=`FullAWSAccess`].{Id:Id,Name:Name}'
  ```

- **Duration:** Read-only command. No live mitigation appropriate; coordinate with the Organizations admin.

**Resolution:**

```bash
# After the Organizations admin updates the policy content:
aws organizations update-policy --policy-id <scp-or-rcp-id> \
  --content file:///tmp/scp-fixed.json
# Verify the policy is reattached and propagated
aws organizations list-policies-for-target --target-id "$ACCOUNT_ID" \
  --filter SERVICE_CONTROL_POLICY
```

**Impact:** Organization-wide for SCP; resource-owner-account-wide for RCP. Propagation to the IAM control plane is seconds; existing sessions pick up the new policy on the next authorization decision.

**Rollback:** `aws organizations update-policy --policy-id <id> --content file:///tmp/scp.backup.json` restores the previous content (must be backed up before the edit).

**Verification:** `aws iam simulate-principal-policy ...` now returns `AllowedByOrganizations: true` and `Decision: allowed`. The original call succeeds and CloudTrail records no further `AccessDenied` for the same action.

### Cause H: Caller lacks iam:PassRole for the role being attached to a service

**Statement:** The failing call configures an AWS service with an IAM role (instance profile, Lambda execution role, ECS task role, CodePipeline service role, RDS monitoring role, etc.), but the caller does not have `iam:PassRole` on the role ARN, so AWS blocks the configuration call.

**Mechanism:** Service-configuration APIs that accept a role ARN as input — `ec2:RunInstances` with `IamInstanceProfile`, `lambda:CreateFunction`/`lambda:UpdateFunctionConfiguration` with `Role`, `ecs:RegisterTaskDefinition` with `taskRoleArn`/`executionRoleArn`, `codepipeline:CreatePipeline` with the pipeline role, `rds:CreateDBInstance` with `MonitoringRoleArn` — require `iam:PassRole` on the passed role in addition to the service's own action. Without it, the parent call fails with `User: <arn> is not authorized to perform: iam:PassRole on resource: <role-arn>`. `iam:PassRole` itself does not appear in CloudTrail as an event — only the parent service call records the denial.

**Indicator:**

- [Step 1] error message contains `iam:PassRole`
<!-- match: {"step": 1, "predicate": "contains", "target": "iam:PassRole"} -->
- [Step 10] `simulate-principal-policy` for `iam:PassRole` against the role ARN returns `implicitDeny` or `explicitDeny`
<!-- match: {"step": 10, "predicate": "contains", "target": "implicitDeny"} -->
- [Step 10] the failing parent call's `requestParameters` (in CloudTrail) references a role ARN via `iamInstanceProfile`, `roleArn`, `executionRoleArn`, `taskRoleArn`, or `monitoringRoleArn`

**Mitigation:**

- **Risk:** Granting `iam:PassRole` on `Resource: "*"` allows the caller to pass any role to any service, enabling privilege escalation; always scope `Resource` to specific role ARNs and use the `iam:PassedToService` condition to bind it to the intended service.
- **Command:**

  ```bash
  # Inspect which roles the caller currently can pass
  aws iam simulate-principal-policy \
    --policy-source-arn <caller-arn> \
    --action-names iam:PassRole \
    --resource-arns <role-arn> \
    --query 'EvaluationResults[].Decision'
  ```

- **Duration:** Read-only; move directly to the scoped Resolution.

**Resolution:**

```bash
cat > /tmp/allow-passrole.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["iam:PassRole", "iam:GetRole"],
      "Resource": "<role-arn-being-passed>",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "<service>.amazonaws.com"
        }
      }
    }
  ]
}
EOF
aws iam put-role-policy \
  --role-name <caller-role-name> \
  --policy-name AllowPassRoleToService \
  --policy-document file:///tmp/allow-passrole.json
```

**Impact:** Affects only the named caller. The `iam:PassedToService` condition restricts the grant to the specific service principal (e.g., `ec2.amazonaws.com`, `lambda.amazonaws.com`, `ecs-tasks.amazonaws.com`), preventing the caller from passing the role to other services.

**Rollback:** `aws iam delete-role-policy --role-name <caller-role-name> --policy-name AllowPassRoleToService`.

**Verification:** `aws iam simulate-principal-policy --policy-source-arn <caller-arn> --action-names iam:PassRole --resource-arns <role-arn> --context-entries ContextKeyName=iam:PassedToService,ContextKeyValues=<service>.amazonaws.com,ContextKeyType=string` returns `Decision: allowed`. The original `RunInstances`/`CreateFunction`/`RegisterTaskDefinition` call succeeds.

### Cause I: IAM eventual consistency — the policy change has not propagated yet

**Statement:** A policy was recently created, updated, or attached that would grant the action, but the change has not yet propagated to all IAM endpoints, so AWS still evaluates with the stale (pre-change) state and denies the request.

**Mechanism:** IAM is a globally distributed service with eventual consistency. Policy creates, updates, version-switches, attach/detach operations, role-creation, and access-key updates take seconds to a few minutes to propagate to every regional endpoint and to every service that caches IAM state. During the propagation window, a call routed to a region that has not yet seen the change observes the old state and rejects with `AccessDenied`. The same call seconds later succeeds with no further intervention. This is most visible in CI/CD pipelines that create a role and immediately use it.

**Indicator:**

- [Step 1] the failing call ran within ~2 minutes of an `iam:*` (CreatePolicy, AttachRolePolicy, CreatePolicyVersion, CreateRole, PutRolePolicy) call in CloudTrail
- [Step 3] `simulate-principal-policy` returns `Decision: allowed` for the action (the simulator sees the new policy) but the runtime call still fails
- [Symptom] the same call succeeds on retry after 30–120 seconds with no other change

**Mitigation:**

- **Risk:** Retrying with exponential backoff is the canonical fix and carries no risk; do not add a broader policy to "work around" the propagation delay.
- **Command:**

  ```bash
  # Wait and retry
  sleep 60 && aws <service> <operation> <args>
  ```

- **Duration:** ≤2 minutes typical; rarely up to 5 minutes for ABAC tag propagation.

**Resolution:**

```bash
# Add exponential-backoff retry to the caller's code path so propagation delays
# do not surface as user-visible 403s. Example bash retry loop:
for i in 1 2 3 4 5; do
  aws <service> <operation> <args> && break
  sleep $((2 ** i))
done
# In application code, configure the AWS SDK's standard retry mode with adaptive retries.
export AWS_RETRY_MODE=adaptive
export AWS_MAX_ATTEMPTS=5
```

**Impact:** Per-call retry overhead during the propagation window only. No configuration change at the AWS side. After propagation completes, the call succeeds on the first attempt with no retry overhead.

**Rollback:** Remove the retry loop or revert `AWS_RETRY_MODE` if the retries cause unwanted latency; the underlying authorization is unchanged.

**Verification:** A scripted re-run after 60 seconds succeeds. `aws iam simulate-principal-policy` shows `allowed` both immediately after the change and on the retried call, confirming the policy is in place; the call's success on retry confirms propagation has completed.

### Cause Z: Unidentified

**Statement:** An HTTP 403 with `AccessDenied`/`AccessDeniedException`/`UnauthorizedOperation` is confirmed against the AWS API, but none of the indicators for Causes A through I match the gathered evidence.

**Mechanism:** The 403 originates from a path not enumerated above. Less common causes include resource-based policy denial when calling cross-account (S3 bucket, KMS key, Secrets Manager, SNS, Lambda function policies), missing service-linked role, trust-policy denial on `sts:AssumeRole` (see `aws-iam-role-assumption-failure`), disabled access keys, root-user-only actions, custom Lambda authorizer or API Gateway resource policy upstream, or cross-organization calls that suppress the enhanced message.

**Indicator:**

- [Default] HTTP 403 confirmed (Step 1) but none of the Causes A–I indicators match the error message, simulator output, or attached policies

**Mitigation:**

- **Risk:** Capturing more diagnostic context is read-only and safe; CloudTrail data events may incur small storage costs when enabled for diagnostic purposes.
- **Command:**

  ```bash
  # Capture the full SDK debug log and a configuration snapshot
  AWS_DEBUG=1 aws <service> <operation> <args> --debug 2> /tmp/aws-debug.log
  aws sts get-caller-identity > /tmp/diag-identity.json 2>&1
  aws iam list-attached-role-policies --role-name <caller-role-name> > /tmp/diag-attached.json 2>&1
  aws iam list-role-policies --role-name <caller-role-name> > /tmp/diag-inline.json 2>&1
  aws iam get-role --role-name <caller-role-name> > /tmp/diag-role.json 2>&1
  aws cloudtrail lookup-events \
    --lookup-attributes AttributeKey=Username,AttributeValue=<caller-name> \
    --start-time "$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)" \
    --max-results 50 > /tmp/diag-cloudtrail.json 2>&1
  grep -E "x-amz-request-id|x-amzn-RequestId" /tmp/aws-debug.log | head -5
  ```

- **Duration:** Minutes. Bundle the artifacts for handoff to AWS Support or the resource owner's security team.

**Resolution:** Out of runbook scope. Package the `/tmp/diag-*.json`, `/tmp/aws-debug.log`, CloudTrail JSON from Step 1, and both `x-amz-request-id` and `x-amzn-RequestId` values. Open an AWS Support case (Business or Enterprise plan required for production cases) with the caller ARN, action, resource ARN, error message verbatim, and request IDs. For cross-account paths, also escalate to the resource owner's security team with the same artifacts.

**Verification:** Handoff acknowledged with a ticket number; an owner assigned. AWS Support replies referencing the request IDs to confirm receipt and begin investigation.

## Prevention

- Use `aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY --policy-document file://policy.json` in CI for every IAM-policy change; gate merges on zero `ERROR` and `SECURITY_WARNING` findings. Validate the same way for `RESOURCE_POLICY`, `SERVICE_CONTROL_POLICY`, and `RESOURCE_CONTROL_POLICY` types.
- Enable IAM Access Analyzer at the organization level: `aws accessanalyzer create-analyzer --analyzer-name org-analyzer --type ORGANIZATION`. It flags cross-account and public access in resource-based policies before a regression reaches production.
- For high-traffic principals, run `aws accessanalyzer start-policy-generation --policy-generation-details '{"principalArn": "<role-arn>"}'` periodically and review the suggested least-privilege policy against the current grant to identify over-permissioning.
- Configure CloudWatch metric filters and alarms on CloudTrail for `{ ($.errorCode = "AccessDenied") || ($.errorCode = "AccessDeniedException") || ($.errorCode = "UnauthorizedOperation") }`; page on a 5-minute increase above baseline so denial regressions are caught before users open tickets.
- Require all production IAM roles to carry a permissions boundary; enforce via SCP (`Deny iam:CreateRole, iam:PutRolePolicy unless aws:RequestTag/PermissionsBoundary matches the org-standard boundary ARN`).
- For every service-configuration call (RunInstances, CreateFunction, RegisterTaskDefinition, CreatePipeline, CreateDBInstance), grant `iam:PassRole` scoped to a specific role ARN and bound to the target service via `iam:PassedToService`; never grant `iam:PassRole` on `Resource: "*"`.
- Configure SDK clients with `AWS_RETRY_MODE=adaptive` and `AWS_MAX_ATTEMPTS=5` so IAM eventual-consistency windows do not surface as production failures.
- For workloads that traverse VPC endpoints, document the endpoint policy alongside the bucket/service the workload depends on; add new resource ARNs to the endpoint policy as part of the same change that adds the IAM Allow.
- When deploying SCPs/RCPs, stage in a non-production OU first and validate every workload still functions before promoting. Use the AWS Organizations policy-simulator (preview) or `iam:SimulatePrincipalPolicy` from each workload account against the new SCP.
- Audit session-policy usage: search for `requestParameters.policy` and `requestParameters.policyArns` in CloudTrail AssumeRole events. Document each call site so changes to the role's identity policies are mirrored in the session policy.
- For cross-account access, prefer resource-based policies that grant directly to the foreign role's ARN over wide `aws:PrincipalOrgID` conditions; the explicit ARN form yields better error messages and is easier to audit.
- Tag every IAM role and resource with `Owner`, `Environment`, and `CostCenter`; use `aws:ResourceTag/<key>` conditions sparingly because they amplify the troubleshooting surface for Cause D.

## Sources

- [Troubleshoot access denied error messages - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot_access-denied.html) - Priority 1. Exact enhanced-context error string formats for identity-based / resource-based / VPC endpoint / SCP / RCP / permissions boundary / session policy / role-trust-policy denials (both implicit and explicit); the `User: <arn> is not authorized to perform: <action> on resource: <arn> because <context>` template; CloudTrail field guidance.
- [Policy evaluation logic - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_evaluation-logic.html) - Priority 1. The evaluation order across SCPs, RCPs, identity-based, resource-based, permissions boundaries, and session policies; explicit-deny precedence; intersection semantics for boundaries/SCPs and union semantics for identity-plus-resource policies within an account.
- [Troubleshoot IAM - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot.html) - Priority 1. IAM eventual consistency model (recommendations against critical-path IAM changes), the iam:DeleteVirtualMFADevice explicit-deny example, links to per-service troubleshooting guides for EC2, S3, and SAML.
- [Permissions boundaries for IAM entities - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries.html) - Priority 1. Permissions-boundary semantics, the intersection rule for boundary + identity policy, boundary interaction with resource-based policies (user-ARN vs role-ARN vs session-ARN grants), and the NotPrincipal/Deny pitfall.
- [Grant a user permissions to pass a role to an AWS service - AWS IAM User Guide](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_passrole.html) - Priority 1. iam:PassRole semantics, the iam:PassedToService condition key, the rule that PassRole is a permission (not an API call) so it has no dedicated CloudTrail event, and example scoped-PassRole policies for EC2 and RDS Enhanced Monitoring.
