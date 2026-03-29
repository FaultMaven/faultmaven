---
id: aws-s3-access-denied
title: "AWS S3 403 Access Denied — Diagnosis and Resolution"
domain: storage
service: aws-s3
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
  - s3
  - access-denied
  - bucket-policy
  - public-access-block
  - kms
  - acl
difficulty: intermediate
---

# AWS S3 403 Access Denied — Diagnosis and Resolution

## Problem Definition

Applies to all AWS accounts using S3 for object storage. Requires `s3:GetBucketPolicy`, `s3:GetBucketAcl`, `s3:GetBucketPublicAccessBlock`, and `iam:SimulatePrincipalPolicy` permissions for diagnosis. Relevant across all AWS SDK versions, CLI v2+, and S3-compatible tools.

S3 403 Access Denied errors occur when an S3 API request is rejected by the combined policy evaluation of IAM policies, bucket policies, S3 Access Points, Public Access Block settings, ACLs, and KMS key policies. The caller receives:

```
An error occurred (AccessDenied) when calling the GetObject operation: Access Denied
```

```
An error occurred (AccessDenied) when calling the PutObject operation: Access Denied
```

S3 Access Denied is more complex than general IAM Access Denied because multiple layers of S3-specific controls interact:

- **Bucket policy** — resource-based policy attached to the bucket. Can grant or deny access to any principal.
- **IAM identity-based policy** — must grant the action on the S3 resource ARN.
- **S3 Block Public Access** — account-level and bucket-level settings that override ACLs and bucket policies granting public access.
- **Object ACLs** — legacy access control that can grant object-level permissions. Overridden by Block Public Access.
- **S3 Access Points** — delegated access policies that can restrict or extend bucket policy permissions.
- **VPC endpoint policy** — restricts which S3 actions are allowed through a Gateway or Interface endpoint.
- **KMS key policy** — for SSE-KMS encrypted objects, the caller must have `kms:Decrypt` (for reads) or `kms:GenerateDataKey` (for writes) on the KMS key.
- **Object ownership** — if the bucket uses BucketOwnerEnforced, ACLs are disabled and only policies control access.

Same-account access: identity-based OR bucket policy can grant (union). Cross-account access: identity-based AND bucket policy must both grant (intersection).

## Diagnostic Steps

### Step 1. Confirm the caller identity and the denied operation

Verifies which credentials are in use and ensures they match what is expected. Wrong credentials are the most common cause.

```bash
aws sts get-caller-identity
```

Expected output shows Account, UserId, and ARN. If the account or role does not match expectations, resolve the credential issue first.

### Step 2. Check the bucket policy

Reads the bucket policy to identify explicit denials or missing grants for the caller's principal.

```bash
aws s3api get-bucket-policy --bucket my-bucket \
  --query 'Policy' --output text | python3 -m json.tool
```

Examine each statement. An explicit `Deny` that matches the caller, action, and resource blocks access regardless of any Allow elsewhere. Common deny patterns include IP restrictions (`aws:SourceIp`), VPC restrictions (`aws:SourceVpce`), and MFA requirements (`aws:MultiFactorAuthPresent`).

If the command returns `NoSuchBucketPolicy`, the bucket has no resource-based policy — access depends entirely on identity-based policies and ACLs.

### Step 3. Check S3 Block Public Access settings

Determines whether account-level or bucket-level Block Public Access settings are overriding ACLs or bucket policy grants.

```bash
# Account-level
aws s3control get-public-access-block --account-id 123456789012

# Bucket-level
aws s3api get-public-access-block --bucket my-bucket
```

If `BlockPublicPolicy` is true, any bucket policy that grants public access is ignored. If `RestrictPublicBuckets` is true, cross-account access via bucket policy is limited to AWS services and authorized users only. If `BlockPublicAcls` or `IgnorePublicAcls` is true, ACL-based public grants are blocked.

### Step 4. Check object ownership and ACLs

Determines whether ACLs are disabled (BucketOwnerEnforced) and, if enabled, whether the object ACL grants the caller access.

```bash
aws s3api get-bucket-ownership-controls --bucket my-bucket

# If ACLs are enabled, check the object ACL
aws s3api get-object-acl --bucket my-bucket --key path/to/object.txt
```

If ownership is `BucketOwnerEnforced`, ACLs are disabled and only bucket/IAM policies apply. If an object was uploaded by a different account with `bucket-owner-full-control` ACL absent, the bucket owner may not have access to it (pre-BucketOwnerEnforced behavior).

### Step 5. Check KMS key access (for encrypted objects)

Determines whether the object is encrypted with SSE-KMS and whether the caller has permissions on the KMS key.

```bash
# Check object encryption
aws s3api head-object --bucket my-bucket --key path/to/object.txt \
  --query '{Encryption:ServerSideEncryption,KMSKeyId:SSEKMSKeyId}'

# If KMS-encrypted, check the key policy
aws kms get-key-policy --key-id arn:aws:kms:us-east-1:123456789012:key/key-id \
  --policy-name default --output text | python3 -m json.tool
```

For GetObject, the caller needs `kms:Decrypt`. For PutObject with SSE-KMS, the caller needs `kms:GenerateDataKey`. If the KMS key policy does not grant these actions to the caller, the S3 operation fails with Access Denied even if S3 policies allow the operation.

### Step 6. Check VPC endpoint policy (if applicable)

Verifies whether the VPC Gateway Endpoint for S3 restricts the allowed actions or buckets.

```bash
aws ec2 describe-vpc-endpoints \
  --filters Name=service-name,Values=com.amazonaws.us-east-1.s3 \
  --query 'VpcEndpoints[].{Id:VpcEndpointId,Policy:PolicyDocument}' --output json
```

The default VPC endpoint policy allows all S3 actions. Custom policies may restrict access to specific buckets or actions, causing Access Denied for operations not listed.

### Step 7. Simulate the S3 permission

Runs the IAM policy simulator to test the caller's identity-based policies against the specific S3 action and resource.

```bash
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::123456789012:role/MyRole \
  --action-names s3:GetObject \
  --resource-arns arn:aws:s3:::my-bucket/path/to/object.txt \
  --query 'EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}'
```

If the result is `implicitDeny`, no identity-based policy grants the action. If `explicitDeny`, a policy explicitly blocks it. Note: the simulator does not evaluate bucket policies or KMS key policies — those must be checked separately.

### Step 8. Check S3 Access Point policies (if using Access Points)

Reads the Access Point policy to ensure it permits the caller's action.

```bash
aws s3control get-access-point-policy \
  --account-id 123456789012 \
  --name my-access-point
```

When using Access Points, both the Access Point policy and the underlying bucket policy must permit the action (unless the bucket policy delegates to the Access Point).

## Mitigation

### Option 1: Temporarily grant broad S3 read access via IAM

**Risk**: AmazonS3ReadOnlyAccess grants read access to all S3 buckets in the account. Suitable only for diagnosing read failures.

**Command**:

```bash
aws iam attach-role-policy \
  --role-name MyRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess
```

**Verify**:

```bash
aws s3 cp s3://my-bucket/path/to/object.txt /tmp/test-download.txt
```

**Duration**: Remove within 24 hours after root cause is resolved.

### Option 2: Temporarily disable Block Public Access (if blocking cross-account access)

**Risk**: Disabling Block Public Access exposes the bucket to any public access grants in the bucket policy or ACLs. Only use if you are certain no public grants exist.

**Command**:

```bash
aws s3api put-public-access-block --bucket my-bucket \
  --public-access-block-configuration \
  BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false
```

**Verify**: Re-run the failing S3 operation and confirm it succeeds.

**Duration**: Re-enable within 1 hour. Identify the specific setting that was blocking and re-enable selectively.

## Root Cause Resolution

**If** the identity-based policy is missing the S3 action → attach a policy granting the specific permission:

```bash
aws iam put-role-policy --role-name MyRole --policy-name AllowS3GetObject \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::my-bucket", "arn:aws:s3:::my-bucket/*"]
    }]
  }'
```

**If** the bucket policy contains an explicit deny blocking the caller → remove or narrow the deny statement:

```bash
aws s3api put-bucket-policy --bucket my-bucket \
  --policy file://updated-bucket-policy.json
```

**If** cross-account access is needed → add the caller's principal to the bucket policy AND ensure the caller's identity-based policy allows the action:

```bash
aws s3api put-bucket-policy --bucket my-bucket --policy '{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::111122223333:role/CrossAccountRole"},
    "Action": ["s3:GetObject"],
    "Resource": "arn:aws:s3:::my-bucket/*"
  }]
}'
```

**If** KMS key access is blocking → grant the caller `kms:Decrypt` and/or `kms:GenerateDataKey` on the KMS key:

```bash
# Update the KMS key policy to include the caller
aws kms put-key-policy --key-id key-id --policy-name default \
  --policy file://updated-kms-key-policy.json
```

Alternatively, grant via IAM policy:

```bash
aws iam put-role-policy --role-name MyRole --policy-name AllowKMSDecrypt \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
      "Resource": "arn:aws:kms:us-east-1:123456789012:key/key-id"
    }]
  }'
```

**If** the VPC endpoint policy is restricting access → update the endpoint policy to include the bucket or action:

```bash
aws ec2 modify-vpc-endpoint --vpc-endpoint-id vpce-0123456789abcdef0 \
  --policy-document file://updated-vpce-policy.json
```

**If** object ownership is causing cross-account issues → migrate to BucketOwnerEnforced ownership:

```bash
aws s3api put-bucket-ownership-controls --bucket my-bucket \
  --ownership-controls '{"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}'
```

**If** Block Public Access is blocking a legitimate bucket policy grant → selectively disable only the specific setting that is blocking, rather than disabling all four controls.

## Verification

1. Re-run the original failing S3 operation and confirm it succeeds:

```bash
# For GetObject
aws s3 cp s3://my-bucket/path/to/object.txt /tmp/test-download.txt

# For PutObject
echo "test" | aws s3 cp - s3://my-bucket/path/to/test-upload.txt
```

2. If cross-account, test from the remote account's credentials:

```bash
# Assume the cross-account role and test
aws sts assume-role --role-arn arn:aws:iam::111122223333:role/CrossAccountRole \
  --role-session-name s3-test
# Use returned credentials
aws s3 ls s3://my-bucket/path/
```

3. Verify CloudTrail shows successful S3 operations (no AccessDenied):

```bash
aws cloudtrail lookup-events \
  --lookup-attributes AttributeKey=EventName,AttributeValue=GetObject \
  --start-time "$(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --query 'Events[?!contains(CloudTrailEvent, `AccessDenied`)].{Time:EventTime}'
```

4. Remove any temporary mitigation policies:

```bash
aws iam detach-role-policy --role-name MyRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess
```

## Prevention

1. **Use S3 Access Analyzer** to identify buckets shared with external accounts or public access:

```bash
aws accessanalyzer list-findings --analyzer-arn arn:aws:access-analyzer:us-east-1:123456789012:analyzer/my-analyzer \
  --filter '{"resourceType": {"eq": ["AWS::S3::Bucket"]}}'
```

2. **Enable S3 Block Public Access at the account level** and only disable selectively at the bucket level when required:

```bash
aws s3control put-public-access-block --account-id 123456789012 \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

3. **Migrate all buckets to BucketOwnerEnforced** to eliminate ACL-related access issues. This is the AWS-recommended default for new buckets.

4. **Use IAM Access Analyzer to validate bucket policies** before applying them:

```bash
aws accessanalyzer validate-policy \
  --policy-type RESOURCE_POLICY \
  --policy-document file://bucket-policy.json
```

5. **For SSE-KMS encrypted buckets, document the KMS key ARN** and required permissions in the bucket's documentation. Include `kms:Decrypt` and `kms:GenerateDataKey` in any IAM policy that grants S3 access to encrypted objects.

6. **Set up S3 server access logging** or CloudTrail S3 data events to capture all Access Denied errors for early detection:

```bash
aws s3api put-bucket-logging --bucket my-bucket \
  --bucket-logging-status '{"LoggingEnabled":{"TargetBucket":"my-log-bucket","TargetPrefix":"s3-logs/my-bucket/"}}'
```

7. **Use separate IAM policies for S3 and KMS** rather than combining them, making it easier to diagnose which layer is denying access.

## Sources

- [Troubleshoot Access Denied errors in Amazon S3 - AWS S3 User Guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/troubleshoot-403-errors.html)
- [Bucket policy examples - AWS S3 User Guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/example-bucket-policies.html)
- [Blocking public access to Amazon S3 - AWS S3 User Guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html)
- [Protecting data using SSE-KMS - AWS S3 User Guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html)
- [Controlling object ownership - AWS S3 User Guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/about-object-ownership.html)
- [S3 Access Points - AWS S3 User Guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-points.html)
- [Troubleshoot S3 Access Denied errors - AWS re:Post](https://repost.aws/knowledge-center/s3-troubleshoot-403)
- [VPC endpoints for Amazon S3 - AWS S3 User Guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/privatelink-interface-endpoints.html)
