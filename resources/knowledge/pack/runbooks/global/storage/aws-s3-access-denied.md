---
id: aws-s3-access-denied
title: "AWS S3 403 Access Denied"
domain: storage
service: aws-s3
symptom_class:
  - auth_failure
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags:
  - aws
  - s3
  - "403"
  - access-denied
  - bucket-policy
  - iam
  - kms
  - block-public-access
  - vpc-endpoint
difficulty: intermediate
---

# AWS S3 403 Access Denied

## Symptom Recognition

- AWS SDK or CLI returns `An error occurred (AccessDenied) when calling the <Operation> operation: Access Denied` (HTTP status `403 Forbidden`); the operation name is one of `GetObject`, `PutObject`, `ListObjectsV2`, `CopyObject`, `DeleteObject`, `HeadObject`, or `PutBucketPolicy`.
- Same-account or same-organization callers receive enhanced messages with the format `User <user-arn> is not authorized to perform: <action> on resource: "<resource-arn>" because <context>`; the `<context>` clause names the policy type that denied the request (for example `with an explicit deny in a resource-based policy`, `because no identity-based policy allows the s3:GetObject action`, `with an explicit deny in a VPC endpoint policy`).
- Common context strings that pinpoint the policy layer: `because no identity-based policy allows`, `because no resource-based policy allows`, `because no VPC endpoint policy allows`, `because no permissions boundary allows`, `because no service control policy allows`, `with an explicit deny in a resource-based policy`, `with an explicit deny in a service control policy`, `with an explicit deny in a resource control policy`, `with an explicit deny in a VPC endpoint policy`, `with an explicit deny in a permissions boundary`, `with an explicit deny in a session policy`, `because public ACLs are prevented by the BlockPublicAcls setting in S3 Block Public Access`, `because public policies are prevented by the BlockPublicPolicy setting in S3 Block Public Access`.
- Cross-account or cross-organization callers receive a generic `Access Denied` with no context; the enhanced message is only emitted within the bucket-owner's account or organization.
- CloudTrail records the failed call with `eventSource=s3.amazonaws.com`, `errorCode=AccessDenied`, and `errorMessage` containing `Access Denied`; for SSE-KMS denials a paired KMS event appears with `eventSource=kms.amazonaws.com`, `eventName=Decrypt` or `GenerateDataKey`, and `errorCode=AccessDenied`.
- For SSE-KMS encrypted objects the error message may explicitly mention KMS: `An error occurred (AccessDenied) when calling the GetObject operation: The ciphertext refers to a customer master key that does not exist, does not exist in this region, or you are not allowed to access`, or the surrounding CloudTrail trail shows `kms:Decrypt` denied milliseconds before the S3 403.
- Requester Pays buckets reject any unsigned/unparameterised request from non-owner principals with `Access Denied (403)`; the caller has not set `--request-payer requester` or the `x-amz-request-payer: requester` header.

## Applicability

- AWS S3 general-purpose buckets in any commercial or GovCloud region; directory buckets (`*--xa-s3`) follow the same policy model but do not emit enhanced denial messages.
- AWS CLI v2, AWS SDK v2/v3 (any language), and S3-compatible tools (`s3cmd`, `rclone`) that surface the standard `AccessDenied` error code.
- Diagnostic permissions required on the caller or an investigator: `s3:GetBucketPolicy`, `s3:GetBucketAcl`, `s3:GetBucketPublicAccessBlock`, `s3:GetBucketOwnershipControls`, `s3:GetBucketEncryption`, `s3:GetAccessPointPolicy`, `iam:SimulatePrincipalPolicy`, `iam:GetPolicy`, `kms:GetKeyPolicy`, `cloudtrail:LookupEvents`, `ec2:DescribeVpcEndpoints`, `organizations:ListPolicies` (only if SCP/RCP is suspected).
- Tooling: `aws` CLI (v2.x), `jq` for JSON parsing, `python3` for pretty-printing, network reachability to the relevant control-plane endpoints.

## Diagnostic Steps

### Step 1: Capture the exact error message and CloudTrail entry

```bash
# Re-run the failing call with debug logging and save the full error
aws s3api get-object --bucket <bucket> --key <key> /tmp/out 2>&1 | tee /tmp/s3-error.txt
# Locate the matching CloudTrail event in the last 15 minutes
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=GetObject \
  --start-time "$(date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --max-results 20 \
  --query 'Events[?contains(CloudTrailEvent, `AccessDenied`)].CloudTrailEvent' \
  --output text | head -200
```

Expected output: the error string from `aws s3api`, then one or more CloudTrail event JSON blobs whose `errorCode` is `AccessDenied`. The CloudTrail `errorMessage` contains the same enhanced-context string the SDK printed (for same-account requests), naming the policy type that denied access.

### Step 2: Confirm the caller identity and target ARN

```bash
aws sts get-caller-identity
echo "Target: arn:aws:s3:::<bucket>/<key>"
```

Expected output: `Account`, `UserId`, and `Arn` of the caller. The `Arn` must match the principal that the diagnostic policies/conditions in subsequent steps will reference; a stale shell, wrong `AWS_PROFILE`, or assumed-role session pointing at the wrong account is the most common false start.

### Step 3: Read the bucket policy and look for explicit denies

```bash
aws s3api get-bucket-policy --bucket <bucket> --query 'Policy' --output text \
  | python3 -m json.tool > /tmp/bucket-policy.json
grep -nE '"Effect"\s*:\s*"Deny"|aws:SourceIp|aws:SourceVpce|aws:MultiFactorAuthPresent|aws:SecureTransport|aws:PrincipalOrgID|s3:x-amz-server-side-encryption' /tmp/bucket-policy.json || echo "no_deny_or_condition_match"
```

Expected output: the parsed policy JSON in `/tmp/bucket-policy.json` and a grep listing of any `"Effect": "Deny"` statements or restrictive conditions. `NoSuchBucketPolicy` from the first command means no bucket policy exists and access depends entirely on identity-based policies and ACLs. A `Deny` whose `Condition` matches the caller's source IP, source VPC endpoint, MFA state, transport, or organization is the smoking gun for Cause B.

### Step 4: Simulate the IAM identity-based policy against the action

```bash
aws iam simulate-principal-policy \
  --policy-source-arn <caller-arn> \
  --action-names s3:GetObject s3:ListBucket s3:PutObject \
  --resource-arns "arn:aws:s3:::<bucket>" "arn:aws:s3:::<bucket>/<key>" \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision,MatchedStatements:MatchedStatements[].SourcePolicyId}' \
  --output json
```

Expected output: a JSON array with one entry per action. `Decision=allowed` means the identity-based policy permits the action. `Decision=implicitDeny` means no identity-based policy grants the action (no matching Allow). `Decision=explicitDeny` means an attached identity-based policy, permissions boundary, or SCP explicitly blocks it; `MatchedStatements` names the policy that produced the deny.

### Step 5: Check Block Public Access settings at account and bucket level

```bash
aws s3control get-public-access-block --account-id "$(aws sts get-caller-identity --query Account --output text)" \
  --query 'PublicAccessBlockConfiguration'
aws s3api get-public-access-block --bucket <bucket> --query 'PublicAccessBlockConfiguration' 2>&1
```

Expected output: a JSON object with the four boolean settings `BlockPublicAcls`, `IgnorePublicAcls`, `BlockPublicPolicy`, `RestrictPublicBuckets`. If any of these is `true` and the access path relies on a public bucket policy or public ACL, S3 strips that grant before evaluation; the original error message includes `because public ACLs are prevented by the BlockPublicAcls setting` or `because public policies are prevented by the BlockPublicPolicy setting`. `NoSuchPublicAccessBlockConfiguration` on the bucket call means no bucket-level block; the account-level block still applies.

### Step 6: Check the bucket encryption configuration and object KMS key

```bash
aws s3api get-bucket-encryption --bucket <bucket> --query 'ServerSideEncryptionConfiguration.Rules' 2>&1
aws s3api head-object --bucket <bucket> --key <key> \
  --query '{SSE:ServerSideEncryption, KMSKey:SSEKMSKeyId, BucketKey:BucketKeyEnabled}' 2>&1
```

Expected output: the bucket's default encryption rules (look for `SSEAlgorithm: aws:kms` plus `KMSMasterKeyID`), and the object's encryption headers. If `SSE=aws:kms` and `KMSKeyId` is populated, the caller needs `kms:Decrypt` (for GET) or `kms:GenerateDataKey` (for PUT) on that key. `head-object` returning `Access Denied` while `get-bucket-encryption` succeeds is a strong indicator that the failure is on the KMS layer, not the S3 layer.

### Step 7: Inspect the KMS key policy and grants for the encrypting key

```bash
KEY_ID="<kms-key-arn-from-step-6>"
aws kms get-key-policy --key-id "$KEY_ID" --policy-name default --output text \
  | python3 -m json.tool > /tmp/kms-policy.json
aws kms list-grants --key-id "$KEY_ID" --query 'Grants[].{Grantee:GranteePrincipal,Ops:Operations}'
grep -nE '"kms:Decrypt"|"kms:GenerateDataKey"|"kms:\*"|"Principal"' /tmp/kms-policy.json
```

Expected output: the full KMS key policy, the list of active grants, and a grep showing which statements mention `kms:Decrypt`, `kms:GenerateDataKey`, or wildcard. If the caller's account/role does not appear as a Principal in any Allow statement (and no grant covers it), KMS denies the cryptographic operation and S3 surfaces it as `AccessDenied`.

### Step 8: Inspect VPC endpoint policy when the caller is inside a VPC

```bash
aws ec2 describe-vpc-endpoints \
  --filters Name=service-name,Values=com.amazonaws.<region>.s3 \
  --query 'VpcEndpoints[].{Id:VpcEndpointId,Type:VpcEndpointType,Policy:PolicyDocument}' \
  --output json > /tmp/vpce.json
grep -nE '"Effect"\s*:\s*"Deny"|"Action"' /tmp/vpce.json || echo "no_explicit_deny"
```

Expected output: each S3 Gateway/Interface endpoint in the region, including its policy document. The default policy is `Action: "*"` on `Resource: "*"`; a custom policy that omits the bucket from `Resource` (implicit deny) or includes a `Deny` for the action (explicit deny) blocks any S3 call routed through the endpoint. The CloudTrail `vpcEndpointId` field on the failing event confirms which endpoint handled the request.

### Step 9: Check object ownership and ACLs when cross-account uploads are suspected

```bash
aws s3api get-bucket-ownership-controls --bucket <bucket> \
  --query 'OwnershipControls.Rules[].ObjectOwnership' 2>&1
aws s3api get-object-acl --bucket <bucket> --key <key> \
  --query '{Owner:Owner,Grants:Grants[].{Grantee:Grantee.ID,Perm:Permission}}' 2>&1
```

Expected output: the ownership setting (`BucketOwnerEnforced`, `BucketOwnerPreferred`, or `ObjectWriter`) and the object's ACL. If ownership is `BucketOwnerEnforced`, ACLs are disabled and only policies apply (ACLs cannot be the cause). If ownership is `ObjectWriter` or `BucketOwnerPreferred` and the `Owner` of the object is a different canonical ID than the bucket owner's, the bucket owner has no implicit access — the object was uploaded by another account without `bucket-owner-full-control`.

### Step 10: Check for Requester Pays on the bucket

```bash
aws s3api get-bucket-request-payment --bucket <bucket> --query 'Payer'
```

Expected output: `BucketOwner` (default) or `Requester`. When `Requester`, every non-owner caller must pass `--request-payer requester` (CLI) or set the `x-amz-request-payer: requester` header (SDK); without it S3 rejects the request as `Access Denied`.

## Causes

### Cause A: Caller's identity-based policy does not allow the S3 action

**Statement:** No identity-based policy attached to the calling IAM user, role, or session grants the S3 action on the target bucket or object, so AWS implicitly denies the request.

**Chain:**
- root: no Allow statement in any of the caller's identity-based policies (managed, inline, group, role) matches the action and resource ARN.
- s1: IAM is deny-by-default, so the absence of any matching Allow is an implicit deny for the action.
- D: S3 returns `403 AccessDenied` with context `because no identity-based policy allows the <action> action`.

**Indicators:**
- root: [Step 4] `simulate-principal-policy` returns `Decision: implicitDeny` for the action
  <!-- match: {"step": 4, "predicate": "contains", "target": "implicitDeny"} -->
- D: [Step 1] error message contains `because no identity-based policy allows`
  <!-- match: {"step": 1, "predicate": "contains", "target": "because no identity-based policy allows"} -->

**Interventions:**
- **remediation** (root): attach a least-privilege inline policy granting the needed S3 actions on the target ARNs.

  ```bash
  cat > /tmp/allow-s3-get.json <<'EOF'
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:ListBucket"],
        "Resource": [
          "arn:aws:s3:::<bucket>",
          "arn:aws:s3:::<bucket>/<prefix>/*"
        ]
      }
    ]
  }
  EOF
  aws iam put-role-policy \
    --role-name <caller-role-name> \
    --policy-name AllowS3GetOnBucket \
    --policy-document file:///tmp/allow-s3-get.json
  ```

  **Verification:** `aws iam simulate-principal-policy --policy-source-arn <caller-arn> --action-names s3:GetObject --resource-arns "arn:aws:s3:::<bucket>/<key>"` now returns `Decision: allowed`; re-running `aws s3api get-object ...` succeeds with HTTP 200.
- **mitigation** (s1): temporarily attach the AWS-managed broad read policy to unblock the caller while the least-privilege policy is authored.

  ```bash
  aws iam attach-role-policy \
    --role-name <caller-role-name> \
    --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess
  ```

  **Risk:** `AmazonS3ReadOnlyAccess` grants read access to every bucket in the account, including buckets the caller should not see. **Duration:** Up to 24 hours, removed once the targeted remediation policy is in place (`aws iam detach-role-policy --role-name <caller-role-name> --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess`). **Verification:** the original failing call now succeeds with HTTP 200.

### Cause B: Bucket policy contains an explicit Deny matching the request

**Statement:** A `Deny` statement in the bucket policy (or access-point policy) matches the caller, action, or request context and overrides any Allow elsewhere, blocking the request even when identity-based policies permit it.

**Chain:**
- root: the bucket (resource-based) policy contains a `Deny` whose Action and Resource match the failing call, often gated by a Condition.
- s1: the Condition (`aws:SourceIp`, `aws:SourceVpce`, `aws:MultiFactorAuthPresent`, `aws:SecureTransport`, `aws:PrincipalOrgID`, or `s3:x-amz-server-side-encryption`) evaluates true for this request, excluding the caller.
- s2: an explicit `Deny` always wins over any explicit `Allow`, regardless of where the Allow lives.
- D: S3 returns `403 AccessDenied` with context naming the offending resource-based policy.

**Indicators:**
- root: [Step 3] bucket policy contains an `"Effect": "Deny"` statement whose Action and Resource match the failing call
  <!-- match: {"step": 3, "predicate": "contains", "target": "\"Effect\": \"Deny\""} -->
- s1: [Step 3] policy Conditions reference `aws:SourceIp`, `aws:SourceVpce`, `aws:MultiFactorAuthPresent`, `aws:SecureTransport`, or `aws:PrincipalOrgID` that exclude the caller
- D: [Step 1] error message contains `with an explicit deny in a resource-based policy`
  <!-- match: {"step": 1, "predicate": "contains", "target": "with an explicit deny in a resource-based policy"} -->

**Interventions:**
- **remediation** (root): delete the offending Deny statement or narrow its Condition (e.g. add the caller's CIDR to the allowed `aws:SourceIp`, or use a `StringNotEquals` exception list) and re-apply the corrected policy.

  ```bash
  # Edit /tmp/bucket-policy.fixed.json: either delete the Deny statement, or
  # narrow the Condition (e.g., add caller's CIDR to aws:SourceIp's Allow side,
  # or use StringNotEquals with an exception list per Example 7 in the S3 docs).
  aws s3api put-bucket-policy --bucket <bucket> \
    --policy file:///tmp/bucket-policy.fixed.json
  ```

  **Verification:** re-run the failing call; the CloudTrail event for the new call has no `errorCode`, and `aws s3api get-bucket-policy --bucket <bucket> | python3 -m json.tool` shows the corrected statement.
- **mitigation** (root): snapshot the live policy before any edit so the original Deny can be restored.

  ```bash
  aws s3api get-bucket-policy --bucket <bucket> --query 'Policy' --output text > /tmp/bucket-policy.backup.json
  ```

  **Risk:** editing the policy may re-enable access from networks or principals the security team intentionally excluded; coordinate with the policy owner before editing. **Duration:** backup only — keep until the corrected policy is verified, then restore with `aws s3api put-bucket-policy --bucket <bucket> --policy file:///tmp/bucket-policy.backup.json` if needed. **Verification:** `python3 -m json.tool /tmp/bucket-policy.backup.json` shows the original Deny is captured.

### Cause C: S3 Block Public Access strips a grant the bucket policy or ACL relied on

**Statement:** Account-level or bucket-level S3 Block Public Access settings remove the public Allow grant in the bucket policy or ACL before evaluation, so callers relying on the public grant are denied.

**Chain:**
- root: the access path depends on a public grant — a wildcard-principal Allow (`"Principal": "*"`) in the bucket policy or a public ACL.
- s1: a Block Public Access setting (`BlockPublicAcls`/`IgnorePublicAcls` for ACLs, `BlockPublicPolicy`/`RestrictPublicBuckets` for policies) is `true` at account or bucket level, applying the most restrictive combination.
- s2: S3 strips the public grant before evaluation, leaving no remaining Allow for the caller.
- D: S3 returns `403 AccessDenied`, naming the BlockPublicAcls/BlockPublicPolicy setting (or `with an explicit deny in a resource-based policy` under `RestrictPublicBuckets`).

**Indicators:**
- root: [Step 3] the bucket policy contains `"Principal": "*"` or `"Principal": {"AWS": "*"}` Allow statements
  <!-- match: {"step": 3, "predicate": "contains", "target": "\"Principal\": \"*\""} -->
- s1: [Step 5] account or bucket Block Public Access shows `BlockPublicAcls=true`, `IgnorePublicAcls=true`, `BlockPublicPolicy=true`, or `RestrictPublicBuckets=true`
- D: [Step 1] error message contains the BlockPublicAcls setting hint
  <!-- match: {"step": 1, "predicate": "contains", "target": "BlockPublicAcls setting in S3 Block Public Access"} -->

**Interventions:**
- **remediation** (root): replace the wildcard-principal Allow with a principal-scoped Allow (which Block Public Access does not strip) and re-tighten BPA to the secure default.

  ```bash
  # Preferred durable fix: replace the wildcard-principal Allow with a principal-scoped Allow,
  # which Block Public Access does not strip.
  cat > /tmp/bucket-policy-scoped.json <<'EOF'
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": {"AWS": ["arn:aws:iam::<consumer-account>:role/<consumer-role>"]},
        "Action": ["s3:GetObject"],
        "Resource": "arn:aws:s3:::<bucket>/*"
      }
    ]
  }
  EOF
  aws s3api put-bucket-policy --bucket <bucket> \
    --policy file:///tmp/bucket-policy-scoped.json
  # Re-tighten Block Public Access to the secure default
  aws s3api put-public-access-block --bucket <bucket> \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  ```

  **Verification:** `aws s3api get-public-access-block --bucket <bucket>` shows all four flags `true`; `aws accessanalyzer validate-policy --policy-type RESOURCE_POLICY --policy-document file:///tmp/bucket-policy-scoped.json` returns no `ERROR` or `SECURITY_WARNING` findings; the intended caller re-runs the operation and succeeds.
- **mitigation** (s1): loosen only the single BPA control causing the deny while validating the policy is the right vehicle.

  ```bash
  # Loosen ONLY the specific control causing the deny, after confirming
  # no unintended public grant exists.
  aws s3api put-public-access-block --bucket <bucket> \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=true
  ```

  **Risk:** disabling a BPA setting exposes any latent public grant; relax only the single setting strictly needed (never all four). **Duration:** minutes — re-enable the disabled setting and switch to an authenticated IAM-based grant. **Verification:** the intended caller's request succeeds while the relaxed setting is in effect; re-enable and confirm the remediation grant holds.

### Cause D: Caller lacks KMS permissions for an SSE-KMS encrypted object

**Statement:** The object is encrypted with SSE-KMS and S3 permissions allow the call, but the caller lacks `kms:Decrypt` (reads) or `kms:GenerateDataKey` (writes) on the encrypting KMS key.

**Chain:**
- root: neither the KMS key policy nor any IAM policy/grant allows the caller's cryptographic action (`kms:Decrypt`/`kms:GenerateDataKey`) on the encrypting key.
- s1: S3 asks KMS to decrypt/generate the per-object data key, and KMS evaluates its key policy plus IAM and grants.
- s2: KMS returns `AccessDenied` for the cryptographic operation, so S3 cannot fulfil the request.
- D: the SDK surfaces `403 AccessDenied` on the S3 operation, with a paired KMS `AccessDenied` event in CloudTrail.

**Indicators:**
- root: [Step 7] the KMS key policy has no Allow with `kms:Decrypt`/`kms:GenerateDataKey` whose Principal includes the caller, and `list-grants` returns no covering grant
- s1: [Step 6] `head-object` reports `ServerSideEncryption: aws:kms` and a `SSEKMSKeyId`
  <!-- match: {"step": 6, "predicate": "contains", "target": "aws:kms"} -->
- s2: [Step 1] CloudTrail shows a `kms.amazonaws.com` event with `errorCode=AccessDenied` and `eventName=Decrypt` or `GenerateDataKey` correlated with the S3 403
  <!-- match: {"step": 1, "predicate": "contains", "target": "kms:Decrypt"} -->

**Interventions:**
- **remediation** (root): grant the caller's role `kms:Decrypt`/`kms:GenerateDataKey` on the key via both the key policy and an identity-based policy (defense in depth).

  ```bash
  # Update the KMS key policy to grant the caller's role kms:Decrypt and kms:GenerateDataKey
  cat > /tmp/kms-policy.fixed.json <<'EOF'
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "EnableRootPermissions",
        "Effect": "Allow",
        "Principal": {"AWS": "arn:aws:iam::<key-owner-account>:root"},
        "Action": "kms:*",
        "Resource": "*"
      },
      {
        "Sid": "AllowS3CallerToUseKey",
        "Effect": "Allow",
        "Principal": {"AWS": "arn:aws:iam::<caller-account>:role/<caller-role>"},
        "Action": ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
        "Resource": "*"
      }
    ]
  }
  EOF
  aws kms put-key-policy --key-id <key-arn> --policy-name default \
    --policy file:///tmp/kms-policy.fixed.json
  # Also grant the same actions on the caller's identity-based policy
  aws iam put-role-policy --role-name <caller-role> --policy-name AllowKMSForS3 \
    --policy-document '{
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
        "Resource": "<key-arn>"
      }]
    }'
  ```

  **Verification:** `aws kms encrypt --key-id <key-arn> --plaintext "test" --query CiphertextBlob` succeeds when run as the caller; the original S3 GET succeeds and CloudTrail shows no further `AccessDenied` on `s3.amazonaws.com` or `kms.amazonaws.com` for 15 minutes.
- **mitigation** (root): identify which principals already use the key to scope the grant before editing (read-only; no live mitigation possible — move directly to remediation).

  ```bash
  # Inspect which principals already use the key
  aws cloudtrail lookup-events \
    --lookup-attributes AttributeKey=ResourceName,AttributeValue=<key-arn> \
    --max-results 20 \
    --query 'Events[].{User:Username,Event:EventName,Time:EventTime}'
  ```

  **Risk:** adding a broad `kms:Decrypt` Allow expands the key's blast radius; prefer granting to a specific role ARN. **Duration:** read-only command; no mitigation in place — apply remediation immediately. **Verification:** the returned principal list confirms the role to scope into the remediation grant.

### Cause E: Object owned by a different account than the bucket and no policy grants the bucket owner access

**Statement:** An external account uploaded the object into a bucket with `ObjectWriter`/`BucketOwnerPreferred` ownership without `bucket-owner-full-control`, so the bucket owner cannot read it.

**Chain:**
- root: an external account uploaded the object without `--acl bucket-owner-full-control` into a bucket whose Object Ownership is `ObjectWriter` or `BucketOwnerPreferred`.
- s1: the uploader's account retains object ownership, so the object's `Owner.ID` differs from the bucket owner's canonical ID.
- s2: bucket-policy Allow statements apply only to bucket-owned objects, so the bucket owner has no grant on this object despite full S3 bucket permissions.
- D: the bucket owner's call returns `403 AccessDenied` even though IAM simulation shows `allowed`.

**Indicators:**
- root: [Step 9] `get-bucket-ownership-controls` returns `ObjectWriter` or `BucketOwnerPreferred`
  <!-- match: {"step": 9, "predicate": "contains", "target": "ObjectWriter"} -->
- s1: [Step 9] `get-object-acl` shows the `Owner.ID` is a different canonical ID than the bucket owner's canonical ID
- D: [Step 4] `simulate-principal-policy` shows `allowed` for the action against the bucket but the runtime call still returns `AccessDenied`

**Interventions:**
- **remediation** (root): migrate the bucket to `BucketOwnerEnforced` ownership (disables ACLs; bucket policy alone controls access) and grant cross-account uploaders via policy.

  ```bash
  # Durable fix: migrate the bucket to BucketOwnerEnforced ownership.
  # This disables ACLs entirely; bucket policy alone controls access.
  aws s3api put-bucket-ownership-controls --bucket <bucket> \
    --ownership-controls '{"Rules":[{"ObjectOwnership":"BucketOwnerEnforced"}]}'
  # Ensure any cross-account uploaders are now granted via bucket policy or
  # identity-based policy, not via ACLs.
  aws s3api put-bucket-policy --bucket <bucket> --policy file:///tmp/bucket-policy.fixed.json
  ```

  **Verification:** `aws s3api get-bucket-ownership-controls --bucket <bucket>` returns `BucketOwnerEnforced`; the bucket-owner re-runs the failing GET and succeeds; for previously-stuck objects, the bucket-owner runs an in-place `copy-object` then GETs the new object.
- **mitigation** (s1): have the object owner re-copy the object in place with the `bucket-owner-full-control` canned ACL.

  ```bash
  # Run as the object owner's account
  aws s3api copy-object --bucket <bucket> --key <key> \
    --copy-source <bucket>/<key> --acl bucket-owner-full-control \
    --metadata-directive REPLACE
  ```

  **Risk:** a one-shot per-object workaround that does not prevent recurrence on future uploads. **Duration:** per-object — keep applying until the `BucketOwnerEnforced` remediation lands. **Verification:** the bucket owner GETs the re-copied object successfully.

### Cause F: VPC endpoint policy implicitly or explicitly denies the request

**Statement:** The request is routed through a VPC endpoint whose policy does not allow the bucket or action, so S3 rejects the call with a VPC endpoint-policy denial.

**Chain:**
- root: the S3 VPC endpoint's policy is non-default and either omits the bucket from `Resource` (implicit deny) or contains a `Deny` matching the action.
- s1: the caller's request is routed through that endpoint (CloudTrail `vpcEndpointId` identifies it), so the endpoint policy applies.
- D: S3 returns `403 AccessDenied` regardless of IAM or bucket-policy state, with context `because no VPC endpoint policy allows` or `with an explicit deny in a VPC endpoint policy`.

**Indicators:**
- root: [Step 8] the active endpoint's `PolicyDocument` is non-default and either omits the bucket from `Resource` or contains a `Deny` matching the action
- s1: [Step 8] the failing CloudTrail event includes a `vpcEndpointId` field that maps to a custom-policy endpoint
- D: [Step 1] error message contains `VPC endpoint policy`
  <!-- match: {"step": 1, "predicate": "contains", "target": "VPC endpoint policy"} -->

**Interventions:**
- **remediation** (root): update the endpoint policy to allow the required actions on the bucket ARNs.

  ```bash
  cat > /tmp/vpce-policy.fixed.json <<'EOF'
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Principal": "*",
        "Action": ["s3:GetObject", "s3:ListBucket", "s3:PutObject"],
        "Resource": [
          "arn:aws:s3:::<bucket>",
          "arn:aws:s3:::<bucket>/*"
        ]
      }
    ]
  }
  EOF
  aws ec2 modify-vpc-endpoint --vpc-endpoint-id <vpce-id> \
    --policy-document file:///tmp/vpce-policy.fixed.json
  ```

  **Verification:** `aws ec2 describe-vpc-endpoints --vpc-endpoint-ids <vpce-id> --query 'VpcEndpoints[0].PolicyDocument'` shows the updated policy; a test S3 call from an instance in the same VPC succeeds with HTTP 200 and CloudTrail confirms the request still carries the endpoint's `vpcEndpointId`.
- **mitigation** (root): snapshot the current endpoint policy before editing so it can be restored.

  ```bash
  aws ec2 describe-vpc-endpoints --vpc-endpoint-ids <vpce-id> \
    --query 'VpcEndpoints[0].PolicyDocument' --output text > /tmp/vpce-policy.backup.json
  ```

  **Risk:** rolling forward to an allow-all endpoint policy temporarily re-opens every bucket reachable via the endpoint; acceptable only in a controlled change window. **Duration:** backup only — do not roll forward until the corrected policy is ready; restore with `aws ec2 modify-vpc-endpoint --vpc-endpoint-id <vpce-id> --policy-document file:///tmp/vpce-policy.backup.json`. **Verification:** `python3 -m json.tool /tmp/vpce-policy.backup.json` confirms the original policy is captured.

### Cause G: AWS Organizations SCP/RCP or permissions boundary denies the action

**Statement:** An SCP, RCP, or IAM permissions boundary upstream of the caller's account or principal denies the S3 action regardless of what local IAM and bucket policies allow.

**Chain:**
- root: an upstream control (SCP, RCP, permissions boundary, or session policy) is missing the Allow or contains a Deny for the S3 action.
- s1: an action must be allowed by every applicable SCP/RCP/permissions-boundary/session policy in addition to IAM and bucket policies — these caps can only remove permissions, never grant them.
- D: S3 returns `403 AccessDenied` naming the upstream layer (`because no service control policy allows`, or `with an explicit deny in a service control policy / resource control policy / permissions boundary / session policy`).

**Indicators:**
- root: [Step 4] `simulate-principal-policy` returns `Decision: explicitDeny` with `MatchedStatements` pointing to an SCP/RCP/permissions-boundary/session policy, or `OrganizationsDecisionDetail.AllowedByOrganizations=false`
- D: [Step 1] error message contains `service control policy`
  <!-- match: {"step": 1, "predicate": "contains", "target": "service control policy"} -->

**Interventions:**
- **remediation** (root): have the SCP/RCP/permissions-boundary owner edit the policy to allow the action, then re-apply it.

  ```bash
  # After the SCP/RCP/permissions-boundary owner edits the policy:
  aws organizations update-policy --policy-id <scp-id> \
    --content file:///tmp/scp.fixed.json
  # Or, for permissions boundary:
  aws iam put-user-permissions-boundary --user-name <caller> \
    --permissions-boundary arn:aws:iam::<account>:policy/<updated-boundary>
  ```

  **Verification:** `aws iam simulate-principal-policy ...` now returns `allowed` for the action; the original S3 call succeeds and CloudTrail records no further `AccessDenied` for the same principal-action pair.
- **mitigation** (root): identify which SCPs/RCPs are attached to the caller's account so the right policy can be routed to its owner (read-only; no live bypass appropriate).

  ```bash
  # Identify which SCPs/RCPs are attached to the caller's account
  aws organizations list-policies-for-target --target-id <account-id> --filter SERVICE_CONTROL_POLICY
  aws organizations list-policies-for-target --target-id <account-id> --filter RESOURCE_CONTROL_POLICY
  ```

  **Risk:** SCP/RCP changes apply organization-wide and must go through the security/governance team; do not bypass. **Duration:** read-only — used to identify which policy needs editing; no live mitigation is appropriate. **Verification:** the listing names the attached SCP/RCP to escalate to its owner.

### Cause H: Requester Pays bucket called without the requester-pays request parameter

**Statement:** The bucket is configured for Requester Pays but the caller did not pass `--request-payer requester` (CLI) or the `x-amz-request-payer: requester` header (SDK), so S3 rejects the unparameterised request.

**Chain:**
- root: the bucket's Requester Pays is enabled (`Payer=Requester`), requiring every non-owner request to signal cost acceptance.
- s1: the caller's request omits the `x-amz-request-payer: requester` header / `--request-payer requester` flag, so S3 sees no acceptance of charges.
- D: S3 returns `403 AccessDenied` for the unparameterised non-owner request.

**Indicators:**
- root: [Step 10] `get-bucket-request-payment` returns `Requester`
  <!-- match: {"step": 10, "predicate": "contains", "target": "Requester"} -->
- s1: [Step 1] the failing call has no `x-amz-request-payer` request header (visible in CloudTrail `requestParameters`, or absent in the CLI invocation)
- D: [Step 2] the caller's account differs from the bucket owner's account

**Interventions:**
- **remediation** (s1): always pass the requester-pays parameter on every call to this bucket.

  ```bash
  # AWS CLI: add the flag to every command
  aws s3 cp s3://<bucket>/<key> /local/path --request-payer requester
  # AWS SDK (boto3): set request payer on the client call
  python3 -c "import boto3; boto3.client('s3').get_object(Bucket='<bucket>', Key='<key>', RequestPayer='requester')"
  ```

  **Verification:** the CLI/SDK call returns HTTP 200 and the object payload; CloudTrail `requestParameters.x-amz-request-payer` is `requester` on the successful event.
- **mitigation** (s1): pass `--request-payer requester` on the single failing call to unblock immediately.

  ```bash
  aws s3api get-object --bucket <bucket> --key <key> --request-payer requester /tmp/out
  ```

  **Risk:** passing `--request-payer requester` accepts charges on every call; verify cost expectations with the bucket owner before bulk operations. **Duration:** indefinite — this is the correct calling pattern; dropping the flag reverts to failing (the safe, no-surprise-charges default). **Verification:** the single call returns HTTP 200 with the object payload.

### Cause Z: Unidentified

**Statement:** A 403 Access Denied is confirmed but none of the indicators for Causes A through H match the gathered evidence (e.g. Object Lock, Access Point network-origin restriction, expired/cross-region presigned URL, CloudFront OAC/OAI misconfiguration, or a cross-organization generic denial).

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the SME / AWS Support.

  ```bash
  # Capture full request/response context
  AWS_DEBUG=1 aws s3api get-object --bucket <bucket> --key <key> /tmp/out --debug 2> /tmp/aws-debug.log
  # Snapshot all relevant configuration in one bundle
  aws s3api get-bucket-policy --bucket <bucket> > /tmp/diag-bucket-policy.json 2>&1
  aws s3api get-bucket-acl --bucket <bucket> > /tmp/diag-bucket-acl.json 2>&1
  aws s3api get-public-access-block --bucket <bucket> > /tmp/diag-bpa.json 2>&1
  aws s3api get-bucket-ownership-controls --bucket <bucket> > /tmp/diag-ownership.json 2>&1
  aws s3api get-bucket-encryption --bucket <bucket> > /tmp/diag-encryption.json 2>&1
  aws s3api get-bucket-versioning --bucket <bucket> > /tmp/diag-versioning.json 2>&1
  aws s3api get-object-lock-configuration --bucket <bucket> > /tmp/diag-objlock.json 2>&1
  # Retrieve the S3 request ID from the failing call for AWS Support
  aws s3api get-object --bucket <bucket> --key <key> /tmp/out --debug 2>&1 \
    | grep -E "x-amz-request-id|x-amz-id-2" | head -10
  ```

  **Risk:** capturing more diagnostic context is read-only and safe; enabling S3 server-access logging or CloudTrail data events may incur small storage costs. **Duration:** minutes — bundle the `/tmp/diag-*.json` files, `aws-debug.log`, the CloudTrail event JSON, and both `x-amz-request-id`/`x-amz-id-2` values, then open an AWS Support case (Premium Support for production) or escalate to the bucket owner's security team with the bucket ARN, caller ARN, action, and request IDs. **Verification:** handoff acknowledged with a ticket number and an owner assigned; AWS Support replies referencing the request IDs to confirm receipt.

## Prevention

- Enable account-level S3 Block Public Access for every account that does not host explicitly-public assets: `aws s3control put-public-access-block --account-id <account> --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true`. Block Public Access is on by default for buckets created after April 2023; verify legacy buckets explicitly.
- Set `ObjectOwnership: BucketOwnerEnforced` on every new bucket. This disables ACLs, eliminates the cross-account-uploader trap (Cause E), and removes one entire policy layer from the troubleshooting matrix.
- Use IAM Access Analyzer continuously: `aws accessanalyzer create-analyzer --analyzer-name account-analyzer --type ACCOUNT` flags buckets shared cross-account or publicly so the security team sees them before users hit 403s. Run `aws accessanalyzer validate-policy --policy-type RESOURCE_POLICY --policy-document file://policy.json` in CI before every bucket-policy change.
- Page on the CloudWatch metric `AWS/S3/4xxErrors` and on a CloudTrail metric filter for `{ ($.errorCode = "AccessDenied") && ($.eventSource = "s3.amazonaws.com") }`; a 5-minute sustained increase above baseline indicates a fresh denial-of-access regression.
- For SSE-KMS buckets, attach an inline IAM policy to every S3 consumer role that explicitly grants `kms:Decrypt` and `kms:GenerateDataKey` on the specific KMS key ARN, in addition to the S3 actions. Do not rely solely on the KMS key policy; defense in depth.
- Avoid AWS managed `aws/s3` for cross-account SSE-KMS — it is not shareable. Use a customer-managed KMS key with an explicit key policy listing every consumer account/role.
- Require all bucket policies to include a `Deny` on `aws:SecureTransport: false` so unencrypted requests fail closed; surface this requirement in policy validation in CI.
- When deploying SCPs/RCPs, document which S3 actions they restrict and notify owning teams before rollout. Test changes in a staging organization unit first.
- Lock down VPC endpoint policies to the minimum set of buckets the workloads in that VPC need, but include all required buckets explicitly to avoid surprise implicit denies.
- Set IAM permissions-boundary expiry/rotation cadence so stale boundaries are removed; periodically audit which principals have boundaries attached.
- Configure server access logging or S3 Access Logs into a separate logging bucket (encrypted with SSE-S3, not SSE-KMS, since the log delivery service cannot use customer-managed KMS keys) so every 403 is captured for post-hoc analysis.

## Sources

- [Troubleshoot access denied (403 Forbidden) errors in Amazon S3](https://docs.aws.amazon.com/AmazonS3/latest/userguide/troubleshoot-403-errors.html) - Priority 1. Enhanced-context error message formats for identity-based / resource-based / VPC endpoint / SCP / RCP / permissions boundary / session policy denials, including verbatim error strings; BlockPublicAcls / IgnorePublicAcls / BlockPublicPolicy / RestrictPublicBuckets behaviour; Requester Pays parameterisation; Object Ownership and ACL precedence; S3 Object Lock retention/legal-hold denial paths.
- [Required permissions for Amazon S3 API operations / How Amazon S3 works with IAM](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-actions.html) - Priority 1. S3 action-to-API-operation mapping (one-to-one, one-to-many), bucket vs object ARN formats, Access Point ARN format with `/object/` suffix, identity-based vs resource-based policy semantics, principal element rules.
- [Using server-side encryption with AWS KMS keys (SSE-KMS)](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html) - Priority 1. `kms:GenerateDataKey` / `kms:Decrypt` requirements for PUT/GET/multipart, cross-account constraint preventing the AWS managed `aws/s3` key from being shared, encryption context default of `aws:s3:arn`, SSE-KMS request signing requirements (TLS, SigV4).
- [AWS IAM access denied troubleshooting](https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot_access-denied.html) - Priority 1. `simulate-principal-policy` Decision values (`allowed`, `implicitDeny`, `explicitDeny`), MatchedStatements interpretation, SCP/RCP/permissions-boundary evaluation order, organisation policy types.
