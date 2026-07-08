---
id: aws-iam-access-denied
title: "AWS IAM Access Denied Across AWS APIs"
domain: security
service: aws-iam
symptom_class:
  - auth_failure
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

### Cause A: Identity-based policy implicit deny

**Statement:** No identity-based policy attached to the calling IAM user, role, group, or session grants the action on the target resource, so AWS defaults to deny because no policy explicitly allows it.

**Chain:**
- root: no identity-based policy attached to the caller (managed, inline, or group) carries an Allow whose Action and Resource match the request.
- s1: IAM evaluates the union of attached identity-based policies and finds no matching Allow statement.
- s2: deny-by-default applies — the absence of any matching Allow is an implicit deny.
- D: AWS returns HTTP 403 `AccessDenied` with `because no identity-based policy allows the <action> action` (Symptom Recognition).

**Indicators:**
- root: [Step 4] no attached policy contains an `"Effect": "Allow"` statement whose `Action` and `Resource` match the failing call.
- s2: [Step 3] `simulate-principal-policy` returns `Decision: implicitDeny` for the action.
- D: [Step 1] error message contains `because no identity-based policy allows`.

**Interventions:**
- **remediation** (root): author a targeted inline Allow scoped to the action and resource.

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

  **Verification:** `aws iam simulate-principal-policy --policy-source-arn <caller-arn> --action-names <service:Operation> --resource-arns <resource-arn>` returns `Decision: allowed`; re-running the original call succeeds with HTTP 200 and CloudTrail records it with no `errorCode`.
- **mitigation** (root): attach a broad managed policy while the targeted Allow is authored.

  ```bash
  aws iam attach-role-policy \
    --role-name <caller-role-name> \
    --policy-arn arn:aws:iam::aws:policy/<ServiceFullAccessPolicy>
  ```

  **Risk:** `<service>FullAccess` (e.g., `AmazonEC2FullAccess`, `AWSLambda_FullAccess`) grants every action in the service, exceeding least-privilege. **Duration:** Up to 24 hours, removed once the targeted policy is in place (`aws iam detach-role-policy --role-name <caller-role-name> --policy-arn arn:aws:iam::aws:policy/<ServiceFullAccessPolicy>`). **Verification:** the original call succeeds; remove and confirm the targeted Allow alone still returns `Decision: allowed`.

### Cause B: Identity-based policy explicit deny

**Statement:** A `Deny` statement in one of the caller's identity-based policies matches the action or resource and overrides any Allow elsewhere, so the request is blocked even when other policies permit it.

**Chain:**
- root: an attached identity-based policy carries a `Deny` statement whose Action and Resource match the request (e.g., an un-removed incident guardrail, an `iam:DeleteVirtualMFADevice` deny, or a `Deny` on a specific resource ARN paired with a `*FullAccess` allow).
- s1: IAM evaluates the policies and an explicit `Deny` always wins over any explicit `Allow`, regardless of ordering or source.
- D: AWS returns HTTP 403 with `with an explicit deny in an identity-based policy` (optionally `: <policy-arn>`) (Symptom Recognition).

**Indicators:**
- root: [Step 4] an attached policy contains an `"Effect": "Deny"` statement whose `Action` and `Resource` match the failing call.
- s1: [Step 3] `simulate-principal-policy` returns `Decision: explicitDeny` and `MatchedStatements` names an identity-policy ARN (not SCP, not permissions boundary).
- D: [Step 1] error message contains `with an explicit deny in an identity-based policy`.

**Interventions:**
- **remediation** (root): publish a corrected policy version with the Deny removed or its Condition narrowed so the caller no longer matches.

  ```bash
  # Edit /tmp/policy-fixed.json: delete the Deny statement, or narrow its Condition
  # so the caller no longer matches (e.g., add the caller's ARN to a NotPrincipal list).
  aws iam create-policy-version \
    --policy-arn <deny-policy-arn> \
    --policy-document file:///tmp/policy-fixed.json \
    --set-as-default
  # Old version remains available for rollback until manually deleted.
  aws iam list-policy-versions --policy-arn <deny-policy-arn>
  ```

  **Verification:** `aws iam simulate-principal-policy ...` now returns `Decision: allowed`; the original call succeeds; `aws iam get-policy --policy-arn <deny-policy-arn> --query 'Policy.DefaultVersionId'` shows the new version is in effect. Rollback with `aws iam set-default-policy-version --policy-arn <deny-policy-arn> --version-id <prior-version-id>`.
- **mitigation** (root): back up the current default version before editing the live policy.

  ```bash
  POLICY_ARN=<deny-policy-arn>
  VER=$(aws iam get-policy --policy-arn "$POLICY_ARN" --query 'Policy.DefaultVersionId' --output text)
  aws iam get-policy-version --policy-arn "$POLICY_ARN" --version-id "$VER" \
    --query 'PolicyVersion.Document' > /tmp/policy-backup.json
  ```

  **Risk:** Removing a Deny may re-enable access the security team intentionally restricted; coordinate with the policy owner before editing the live default version. **Duration:** Backup only — keep until the corrected version is verified. **Verification:** `/tmp/policy-backup.json` contains the pre-edit document and can restore it.

### Cause C: Permissions boundary excludes the action

**Statement:** A permissions boundary attached to the caller's IAM user or role caps the effective permissions and does not include the action, so the intersection of identity policies and boundary excludes the action even when the identity policies permit it.

**Chain:**
- root: a permissions boundary (a managed policy capping maximum permissions) is attached to the caller and either omits the action from any Allow or carries a `Deny` for it.
- s1: the effective permissions are the intersection of identity-based policies and the boundary; an action must be allowed by both, and an explicit Deny in the boundary overrides any Allow.
- s2: the action is excluded from the intersection (or explicitly denied by the boundary), so it is denied regardless of how permissive the identity policies are.
- D: AWS returns HTTP 403 with `because no permissions boundary allows` or `with an explicit deny in a permissions boundary` (Symptom Recognition).

**Indicators:**
- root: [Step 5] the caller has a non-null `PermissionsBoundary` whose policy document omits the action from any Allow or includes a Deny for it.
- s2: [Step 3] `simulate-principal-policy` returns `PermissionsBoundaryDecisionDetail.AllowedByPermissionsBoundary: false`.
- D: [Step 1] error message contains `permissions boundary`.

**Interventions:**
- **remediation** (root): update the boundary policy to include the missing action.

  ```bash
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

  **Verification:** `aws iam simulate-principal-policy ...` returns `AllowedByPermissionsBoundary: true` and `Decision: allowed`; the original call succeeds; `aws iam get-policy --policy-arn "$BOUNDARY_ARN" --query 'Policy.DefaultVersionId'` shows the updated version. Rollback with `aws iam set-default-policy-version --policy-arn "$BOUNDARY_ARN" --version-id <prior-version-id>`.
- **mitigation** (root): capture the current boundary for restoration before changing it.

  ```bash
  aws iam get-role --role-name <caller-role-name> --query 'Role.PermissionsBoundary' \
    > /tmp/boundary-backup.json
  ```

  **Risk:** Detaching the boundary removes the entire guardrail and may expose other services it was restricting; never detach in production without security-team approval. **Duration:** Backup only — keep until the corrected boundary is applied. **Verification:** `/tmp/boundary-backup.json` holds the prior boundary reference for `aws iam put-role-permissions-boundary --role-name <caller-role-name> --permissions-boundary <prior-boundary-arn>`.

### Cause D: Policy Condition excludes the request context

**Statement:** A Condition clause on an otherwise-permissive identity-based or resource-based policy does not match the caller's request context (source VPC, source IP, requested region, MFA state, principal/resource tags, time-of-day), turning the Allow into an implicit deny for this specific request.

**Chain:**
- root: a matching Allow statement carries a `Condition` (e.g., `aws:SourceVpc`, `aws:SourceIp`, `aws:RequestedRegion`, `aws:MultiFactorAuthPresent`, `aws:RequestTag/*`, `aws:ResourceTag/*`, `aws:PrincipalOrgID`) whose required value the caller's request context does not satisfy.
- s1: the request arrives without the required context (no MFA, wrong region, missing tag), so the Condition evaluates false.
- s2: IAM evaluates as if the conditioned Allow were absent, producing an implicit deny for this specific request.
- D: AWS returns HTTP 403 with `because no identity-based policy allows the <action> action` even though the policies grammatically appear to permit the action (Symptom Recognition).

**Indicators:**
- root: [Step 9] grep shows a `Condition` block on the matching Allow statement referencing the caller's request context (`aws:SourceVpc`, `aws:SourceIp`, `aws:RequestedRegion`, `aws:MultiFactorAuthPresent`, `aws:RequestTag/*`, `aws:ResourceTag/*`, `aws:PrincipalOrgID`).
- s2: [Step 3] `simulate-principal-policy` returns `implicitDeny` despite Step 4 showing a matching Allow statement on paper.
- D: [Step 1] error message contains `because no identity-based policy allows`.

**Interventions:**
- **remediation** (root): satisfy the Condition by submitting the call from a matching context, adding the required tag, or (with security review) narrowing the Condition.

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

  **Verification:** re-run the original call with the corrected context; it returns HTTP 200. `aws iam simulate-principal-policy --policy-source-arn <caller> --action-names <action> --resource-arns <resource> --context-entries ContextKeyName=aws:SourceVpc,ContextKeyValues=<vpc-id>,ContextKeyType=string` returns `Decision: allowed` when the simulated context matches. Option C rollback: `aws iam set-default-policy-version --policy-arn <policy-arn> --version-id <prior-version-id>`.
- **mitigation** (s1): re-establish the request context the Condition expects without changing any policy.

  ```bash
  # Re-establish the request context the condition expects:
  # MFA-enabled session:
  aws sts get-session-token --serial-number <mfa-arn> --token-code <code>
  # Region:
  aws --region <required-region> <service> <operation>
  # Source VPC: run from an instance inside the VPC, or use an interface endpoint
  ```

  **Risk:** Satisfying the condition carries no IAM risk but may require operational changes (VPN, MFA-enabled session); do not weaken the condition without security review. **Duration:** Indefinite — this is the correct way to satisfy a Condition; no rollback required. **Verification:** the re-run from the matching context returns HTTP 200.

### Cause E: Session policy over-restricts the assumed role

**Statement:** A session policy supplied as the `Policy` or `PolicyArns` parameter to `sts:AssumeRole` (or to a federation call) does not allow the action, so the session's effective permissions — the intersection of the role's identity policies and the session policy — exclude the action.

**Chain:**
- root: a session policy was passed at AssumeRole time (commonly by CI/CD code or an SDK wrapper) that omits an action the workload needs.
- s1: the session's effective permissions are the intersection of the role's identity-based policies, the session policy, and any boundary — the missing action is excluded by the session policy.
- D: AWS returns HTTP 403 with `because no session policy allows the <action> action` (implicit) or `with an explicit deny in a session policy` (Symptom Recognition).

**Indicators:**
- root: [Step 8] the AssumeRole CloudTrail event for the current session has a non-empty `requestParameters.policy` or `requestParameters.policyArns`.
- s1: [Step 3] `simulate-principal-policy` against the role itself returns `allowed`, but the runtime call still fails — indicating a restriction beyond the role's identity policies.
- D: [Step 1] error message contains `session policy`.

**Interventions:**
- **remediation** (root): edit the caller (CI script, SDK wrapper, IAM Identity Center permission set) to drop the session policy or broaden it to include the missing action.

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

  **Verification:** a fresh AssumeRole produces a session whose decoded restrictions no longer include the missing action; re-run the failing call within that session — it succeeds with HTTP 200. Rollback by reverting the CI/SDK change; existing sessions pick up prior behaviour on next AssumeRole.
- **mitigation** (s1): re-assume the role with no session policy as a stopgap.

  ```bash
  # Re-assume the role with no session policy
  aws sts assume-role \
    --role-arn <role-arn> \
    --role-session-name <session-name> \
    --duration-seconds 3600
  # Export the returned credentials into the shell and retry the failing call
  ```

  **Risk:** Re-assuming without the session policy temporarily restores full role permissions, which may exceed what the workload should have for the session. **Duration:** One session (≤ role's `MaxSessionDuration`). Restore session-policy usage as soon as the durable fix lands. **Verification:** the retried call within the unrestricted session succeeds with HTTP 200.

### Cause F: VPC endpoint policy denies the request

**Statement:** The caller's request traversed a VPC interface or gateway endpoint whose policy does not allow the action on the target resource, so the endpoint rejects the call before it reaches the service.

**Chain:**
- root: the VPC endpoint that handled the call carries a customized policy that omits the action/resource from its allow-list (implicit deny) or carries a `Deny` matching the action.
- s1: the request is routed through that endpoint and gated by its policy before reaching the service.
- s2: the endpoint rejects the call because it falls outside the allow-list or matches the explicit `Deny`.
- D: AWS returns HTTP 403 with `because no VPC endpoint policy allows` or `with an explicit deny in a VPC endpoint policy` (Symptom Recognition).

**Indicators:**
- root: [Step 7] the endpoint's `PolicyDocument` omits the resource ARN from `Resource` or includes a `Deny` matching the action.
- s1: [Step 7] the CloudTrail event includes a `vpcEndpointId` field naming an endpoint with a non-default policy.
- D: [Step 1] error message contains `VPC endpoint policy`.

**Interventions:**
- **remediation** (root): add the action and resource to the endpoint policy.

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

  **Verification:** `aws ec2 describe-vpc-endpoints --vpc-endpoint-ids <vpce-id> --query 'VpcEndpoints[0].PolicyDocument'` shows the updated policy; a test call from inside the VPC succeeds and CloudTrail shows the same `vpcEndpointId` with no `errorCode`. Rollback with `aws ec2 modify-vpc-endpoint --vpc-endpoint-id <vpce-id> --policy-document file:///tmp/vpce-policy.backup.json`.
- **mitigation** (root): back up the endpoint's current policy before editing.

  ```bash
  aws ec2 describe-vpc-endpoints --vpc-endpoint-ids <vpce-id> \
    --query 'VpcEndpoints[0].PolicyDocument' --output text > /tmp/vpce-policy.backup.json
  ```

  **Risk:** Resetting an endpoint policy to allow-all temporarily exposes every resource reachable via the endpoint; only acceptable in a controlled change window with the network/security team. **Duration:** Backup only; do not roll forward without the corrected policy ready. **Verification:** `/tmp/vpce-policy.backup.json` holds the pre-edit document for rollback.

### Cause G: SCP or RCP denies the action

**Statement:** An AWS Organizations Service Control Policy (SCP) attached to the caller's account or organizational unit, or a Resource Control Policy (RCP) attached to the target resource's account, denies the action and overrides any same-account IAM Allow.

**Chain:**
- root: an SCP up the caller's OU hierarchy (or an RCP on the resource account) carries a `Deny` matching the action, or an allow-list SCP omits it.
- s1: SCPs cap the maximum permissions for every principal in the member account and RCPs scope what the resource side can permit; an action must clear every such layer.
- s2: the action is denied at the Organizations layer regardless of how permissive the identity-based policies are.
- D: AWS returns HTTP 403 with `because no service control policy allows` / `with an explicit deny in a service control policy` (or the `resource control policy` equivalent) (Symptom Recognition).

**Indicators:**
- root: [Step 6] an SCP or RCP on the account contains a `"Effect": "Deny"` matching the action, or an Allow-list SCP omits it.
- s2: [Step 3] `simulate-principal-policy` returns `OrganizationsDecisionDetail.AllowedByOrganizations: false`.
- D: [Step 1] error message contains `service control policy`.

**Interventions:**
- **remediation** (root): after the Organizations admin updates the policy content, apply and verify the corrected SCP/RCP.

  ```bash
  # After the Organizations admin updates the policy content:
  aws organizations update-policy --policy-id <scp-or-rcp-id> \
    --content file:///tmp/scp-fixed.json
  # Verify the policy is reattached and propagated
  aws organizations list-policies-for-target --target-id "$ACCOUNT_ID" \
    --filter SERVICE_CONTROL_POLICY
  ```

  **Verification:** `aws iam simulate-principal-policy ...` now returns `AllowedByOrganizations: true` and `Decision: allowed`; the original call succeeds and CloudTrail records no further `AccessDenied`. Rollback with `aws organizations update-policy --policy-id <id> --content file:///tmp/scp.backup.json`.
- **mitigation** (root): identify the offending non-default SCP and escalate to the Organizations admin (no safe live policy change exists).

  ```bash
  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
  aws organizations list-policies-for-target --target-id "$ACCOUNT_ID" \
    --filter SERVICE_CONTROL_POLICY \
    --query 'Policies[?Name!=`FullAWSAccess`].{Id:Id,Name:Name}'
  ```

  **Risk:** SCP/RCP changes apply organization-wide and must go through the security/governance team; do not bypass. **Duration:** Read-only command; no live mitigation appropriate — coordinate with the Organizations admin. **Verification:** the offending policy is named and a ticket is opened with the Organizations admin.

### Cause H: Caller lacks iam:PassRole for the role being attached

**Statement:** The failing call configures an AWS service with an IAM role (instance profile, Lambda execution role, ECS task role, CodePipeline service role, RDS monitoring role, etc.), but the caller does not have `iam:PassRole` on the role ARN, so AWS blocks the configuration call.

**Chain:**
- root: the caller has no Allow for `iam:PassRole` on the role ARN being passed (or a Deny matches it).
- s1: the service-configuration API (e.g. `ec2:RunInstances`, `lambda:CreateFunction`, `ecs:RegisterTaskDefinition`, `rds:CreateDBInstance`) requires `iam:PassRole` on the passed role in addition to the service's own action.
- D: the parent call fails with HTTP 403 `User: <arn> is not authorized to perform: iam:PassRole on resource: <role-arn>`; `iam:PassRole` records no CloudTrail event of its own — only the parent call logs the denial (Symptom Recognition).

**Indicators:**
- root: [Step 10] `simulate-principal-policy` for `iam:PassRole` against the role ARN returns `implicitDeny` or `explicitDeny`.
- s1: [Step 10] the failing parent call's `requestParameters` (in CloudTrail) references a role ARN via `iamInstanceProfile`, `roleArn`, `executionRoleArn`, `taskRoleArn`, or `monitoringRoleArn`.
- D: [Step 1] error message contains `iam:PassRole`.

**Interventions:**
- **remediation** (root): grant scoped `iam:PassRole` on the specific role ARN bound to the target service via `iam:PassedToService`.

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

  **Verification:** `aws iam simulate-principal-policy --policy-source-arn <caller-arn> --action-names iam:PassRole --resource-arns <role-arn> --context-entries ContextKeyName=iam:PassedToService,ContextKeyValues=<service>.amazonaws.com,ContextKeyType=string` returns `Decision: allowed`; the original `RunInstances`/`CreateFunction`/`RegisterTaskDefinition` call succeeds. Rollback with `aws iam delete-role-policy --role-name <caller-role-name> --policy-name AllowPassRoleToService`. Never grant `iam:PassRole` on `Resource: "*"` — it enables privilege escalation.

### Cause I: IAM eventual consistency — change not yet propagated

**Statement:** A policy was recently created, updated, or attached that would grant the action, but the change has not yet propagated to all IAM endpoints, so AWS still evaluates with the stale (pre-change) state and denies the request.

**Chain:**
- root: an `iam:*` change (CreatePolicy, AttachRolePolicy, CreatePolicyVersion, CreateRole, PutRolePolicy) that would grant the action ran within the last ~2 minutes.
- s1: IAM is globally distributed with eventual consistency; the change takes seconds to a few minutes to propagate to every regional endpoint and IAM-caching service.
- s2: the call is routed to a region/cache that has not yet seen the change and observes the old (pre-change) state.
- D: AWS returns HTTP 403 `AccessDenied`; the same call succeeds on retry after 30–120 seconds with no further change (Symptom Recognition).

**Indicators:**
- root: [Step 1] the failing call ran within ~2 minutes of an `iam:*` (CreatePolicy, AttachRolePolicy, CreatePolicyVersion, CreateRole, PutRolePolicy) call in CloudTrail.
- s2: [Step 3] `simulate-principal-policy` returns `Decision: allowed` (the simulator sees the new policy) but the runtime call still fails.
- D: [Symptom] the same call succeeds on retry after 30–120 seconds with no other change.

**Interventions:**
- **remediation** (root): add exponential-backoff retry to the caller so propagation delays do not surface as 403s.

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

  **Verification:** a scripted re-run after 60 seconds succeeds; `aws iam simulate-principal-policy` shows `allowed` both immediately and on retry, and the call's success on retry confirms propagation completed. Rollback by removing the retry loop or reverting `AWS_RETRY_MODE`; the underlying authorization is unchanged.
- **mitigation** (root): wait out the propagation window and retry once.

  ```bash
  # Wait and retry
  sleep 60 && aws <service> <operation> <args>
  ```

  **Risk:** Retrying with backoff carries no risk; do not add a broader policy to "work around" the propagation delay. **Duration:** ≤2 minutes typical; rarely up to 5 minutes for ABAC tag propagation. **Verification:** the retried call after the wait succeeds with HTTP 200.

### Cause Z: Unidentified

**Statement:** An HTTP 403 (`AccessDenied`/`AccessDeniedException`/`UnauthorizedOperation`) is confirmed against the AWS API, but no Cause A–I indicator matches the evidence (a less common path: cross-account policy denial, missing service-linked role, or disabled access keys).

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the SME / AWS Support.

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

  **Risk:** Capturing more diagnostic context is read-only and safe; CloudTrail data events may incur small storage costs when enabled for diagnostics. **Duration:** Minutes; bundle the artifacts for handoff. Package the `/tmp/diag-*.json`, `/tmp/aws-debug.log`, the CloudTrail JSON from Step 1, and both `x-amz-request-id` and `x-amzn-RequestId` values; open an AWS Support case (Business/Enterprise for production) with the caller ARN, action, resource ARN, error message verbatim, and request IDs. For cross-account paths, also escalate to the resource owner's security team. **Verification:** handoff acknowledged with a ticket number and an owner assigned; AWS Support replies referencing the request IDs.

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
