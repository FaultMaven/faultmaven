---
id: "eks-cluster-authentication"
title: "AWS EKS Cluster Authentication Failures"
domain: compute
service: aws-eks
symptom_class: [auth_failure]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [aws, eks, kubernetes, iam, aws-auth, oidc, kubectl, access-entries, irsa]
difficulty: intermediate
---

## Symptom Recognition

`kubectl` commands fail with one of the following errors:

```text
error: You must be logged in to the server (Unauthorized)
```

```text
could not get token: AccessDenied: Access denied
```

```text
error: the server doesn't have a resource type "svc"
```

Worker node kubelet logs show:

```text
Unable to register node "ip-10-40-175-122.ec2.internal" with API server: Unauthorized
Container runtime network not ready: NetworkReady=false reason:NetworkPluginNotReady
```

Pods using IAM Roles for Service Accounts (IRSA) fail with:

```text
An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity operation: The security token included in the request is invalid
```

## Applicability

Applies to Amazon EKS clusters in any AWS region running Kubernetes 1.23 or later. Required access: AWS CLI v2 with `sts:GetCallerIdentity` and `eks:DescribeCluster` permissions, `kubectl` 1.23+. For API-based access management (EKS platform version `eks.15`+ on Kubernetes 1.30+), also requires `eks:ListAccessEntries`, `eks:DescribeAccessEntry`, and `eks:CreateAccessEntry`. `eksctl` is needed for ConfigMap-based management. For OIDC/IRSA diagnosis, `iam:ListOpenIDConnectProviders` is required.

## Diagnostic Steps

### Step 1: Verify current AWS identity

```bash
aws sts get-caller-identity
```

Expected output: JSON with `UserId`, `Account`, and `Arn`. The `Arn` field shows the IAM principal (`iam::ACCOUNT:user/NAME` or `iam::ACCOUNT:role/NAME`). If this command itself fails with `AccessDenied`, the local credential chain is broken.

### Step 2: Check kubeconfig exec configuration

```bash
kubectl config current-context
kubectl config view --minify
```

Expected output: The `users[].user.exec` section should use `aws eks get-token` with `--cluster-name` and `--region` matching the target cluster. If `--role-arn` is present, that role must be assumable by the current identity from Step 1.

### Step 3: Test token generation

```bash
aws eks get-token --cluster-name my-cluster --region us-east-1
```

Expected output: JSON with a `token` field and `expirationTimestamp`. If this fails with `AccessDenied`, the current IAM identity lacks `eks:DescribeCluster` permission or STS is unavailable. If it succeeds but kubectl still returns `Unauthorized`, the IAM principal has no Kubernetes mapping.

### Step 4: Determine cluster authentication mode

```bash
aws eks describe-cluster --name my-cluster \
  --query 'cluster.accessConfig.authenticationMode' --output text
```

Expected output: One of `CONFIG_MAP`, `API`, or `API_AND_CONFIG_MAP`. `CONFIG_MAP` means only Step 5 applies. `API` means only Step 6 applies. `API_AND_CONFIG_MAP` means either method can grant access — check both.

### Step 5: Inspect aws-auth ConfigMap (CONFIG_MAP or API_AND_CONFIG_MAP clusters)

```bash
eksctl get iamidentitymapping --cluster my-cluster
```

Expected output: A table listing every mapped IAM ARN with its Kubernetes username and groups. If the command fails with an error, the ConfigMap may be missing. If the caller's ARN does not appear, they have no cluster access via this method. Role ARNs must not contain a path (e.g., `arn:aws:iam::111122223333:role/MyRole` not `arn:aws:iam::111122223333:role/path/MyRole`).

### Step 6: Inspect EKS access entries (API or API_AND_CONFIG_MAP clusters)

```bash
aws eks list-access-entries --cluster-name my-cluster --output text
aws eks describe-access-entry --cluster-name my-cluster \
  --principal-arn arn:aws:iam::111122223333:role/MyRole
```

Expected output: `list-access-entries` returns all principal ARNs with access entries. `describe-access-entry` shows the entry type and associated access policies. If the caller's ARN is absent, they have no API-based access.

### Step 7: Check node IAM role mapping

```bash
# For nodes in error state, get the node IAM role ARN from AWS console or:
aws iam get-instance-profile --instance-profile-name MyNodeInstanceProfile \
  --query 'InstanceProfile.Roles[0].Arn' --output text
```

Expected output: A role ARN of the form `arn:aws:iam::111122223333:role/MyNodeRole`. Confirm this exact ARN (not the instance profile ARN) appears in the ConfigMap (`system:bootstrappers`, `system:nodes` groups) or as an `EC2_LINUX` type access entry.

### Step 8: Verify OIDC provider for IRSA

```bash
OIDC_URL=$(aws eks describe-cluster --name my-cluster \
  --query 'cluster.identity.oidc.issuer' --output text)
echo "Cluster OIDC: $OIDC_URL"
aws iam list-open-id-connect-providers \
  --query 'OpenIDConnectProviderList[*].Arn' --output text
```

Expected output: The cluster OIDC issuer URL (e.g., `https://oidc.eks.us-east-1.amazonaws.com/id/ABCDEF`) should correspond to one of the listed IAM OIDC provider ARNs. If no matching provider exists, all service accounts that reference IAM roles via annotations will fail to authenticate to AWS APIs.

### Step 9: Check verbose kubectl response code

```bash
kubectl get pods -v=6 2>&1 | grep -E 'Response Status|HTTP'
```

Expected output: `Response Status: 401 Unauthorized` confirms authentication failure (IAM identity not recognized). `Response Status: 403 Forbidden` confirms authentication succeeded but the mapped Kubernetes user lacks RBAC permissions — the cause is insufficient Kubernetes group bindings, not IAM mapping.

## Causes

### Cause A: Wrong IAM identity active in local credential chain

**Statement:** The AWS CLI is resolving credentials from the wrong profile, environment variable, or instance metadata endpoint, so the token is generated for an identity that has no cluster access.

**Mechanism:** EKS authentication uses `aws eks get-token` to generate a presigned STS URL embedded in the kubeconfig exec credential. If the resolved IAM identity differs from the one that was mapped in the ConfigMap or access entries, the Kubernetes API server returns `Unauthorized` even though the token itself is cryptographically valid.

**Indicator:**

- [Step 1] `Arn` in `get-caller-identity` output does not match any ARN in Step 5 or Step 6
- [Step 2] Kubeconfig `exec` section contains no `--role-arn` argument and ambient credentials are wrong

<!-- match: {"step": 1, "predicate": "contains", "target": "assumed-role"} -->

**Mitigation:**

- **Risk:** Low — regenerating kubeconfig only updates the local file.
- **Command:**

  ```bash
  # Identify active credential source
  aws configure list

  # Use explicit profile
  export AWS_PROFILE=eks-admin
  aws eks update-kubeconfig --name my-cluster --region us-east-1

  # Or pin to a specific role
  aws eks update-kubeconfig --name my-cluster --region us-east-1 \
    --role-arn arn:aws:iam::111122223333:role/EKSAdminRole
  ```

- **Duration:** Immediate.

**Resolution:** Same as Mitigation.

**Verification:**

```bash
aws sts get-caller-identity
kubectl auth whoami
```

`kubectl auth whoami` (Kubernetes 1.27+) returns the Kubernetes username and groups; it must match an authorized mapping. For older clusters use `kubectl get nodes`.

### Cause B: IAM principal missing from aws-auth ConfigMap

**Statement:** The caller's IAM role or user ARN has no entry in the `aws-auth` ConfigMap, so the Kubernetes API server cannot map the token to a Kubernetes identity.

**Mechanism:** After `aws eks get-token` succeeds, the AWS IAM Authenticator on the control plane decodes the STS presigned URL, resolves the IAM principal, and looks it up in the `aws-auth` ConfigMap. If no matching `mapRoles` or `mapUsers` entry exists, the authenticator returns an empty identity and Kubernetes denies the request with `Unauthorized`.

**Indicator:**

- [Step 3] Token generation succeeds but kubectl returns `Unauthorized`
- [Step 5] Caller's ARN does not appear in `eksctl get iamidentitymapping` output
- [Step 4] Authentication mode is `CONFIG_MAP` or `API_AND_CONFIG_MAP`

<!-- match: {"step": 5, "predicate": "absent", "target": "arn:aws:iam"} -->

**Mitigation:**

- **Risk:** Medium — grants Kubernetes API access to the specified IAM identity. Use the least-privilege group; reserve `system:masters` for admin roles only.
- **Command:**

  ```bash
  eksctl create iamidentitymapping --cluster my-cluster \
    --arn arn:aws:iam::111122223333:role/MyRole \
    --group system:masters \
    --username admin-user
  ```

- **Duration:** Immediate — effective within seconds.

**Resolution:** Same as Mitigation.

**Impact:** Grants cluster-level Kubernetes access to the specified IAM principal. The access level is determined by the Kubernetes group (e.g., `system:masters` = full admin). Rollback is immediate.

**Rollback:**

  ```bash
  eksctl delete iamidentitymapping --cluster my-cluster \
    --arn arn:aws:iam::111122223333:role/MyRole
  ```

**Verification:**

```bash
eksctl get iamidentitymapping --cluster my-cluster
kubectl get nodes
```

### Cause C: IAM principal missing from EKS access entries

**Statement:** The cluster uses API-based authentication mode and the caller's IAM principal has no access entry, so the EKS API returns no Kubernetes identity for the token.

**Mechanism:** On clusters with `API` or `API_AND_CONFIG_MAP` authentication mode, identity resolution uses the EKS access entries API instead of (or alongside) the ConfigMap. If the principal ARN has no access entry and no ConfigMap entry, the IAM Authenticator finds no match and the API server rejects the request.

**Indicator:**

- [Step 4] Authentication mode is `API` or `API_AND_CONFIG_MAP`
- [Step 6] Caller's ARN absent from `aws eks list-access-entries` output
- [Step 3] Token generation succeeds but kubectl returns `Unauthorized`

<!-- match: {"step": 4, "predicate": "contains", "target": "API"} -->

**Mitigation:**

- **Risk:** Medium — grants EKS cluster access at the API level. The access scope (cluster or namespace) and policy (e.g., `AmazonEKSClusterAdminPolicy`) control blast radius.
- **Command:**

  ```bash
  aws eks create-access-entry --cluster-name my-cluster \
    --principal-arn arn:aws:iam::111122223333:role/MyRole \
    --type STANDARD

  aws eks associate-access-policy --cluster-name my-cluster \
    --principal-arn arn:aws:iam::111122223333:role/MyRole \
    --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
    --access-scope type=cluster
  ```

- **Duration:** Immediate.

**Resolution:** Same as Mitigation.

**Impact:** Grants cluster-wide access if `access-scope type=cluster` is used. For least-privilege, use `type=namespace` with a specific namespace.

**Rollback:**

  ```bash
  aws eks delete-access-entry --cluster-name my-cluster \
    --principal-arn arn:aws:iam::111122223333:role/MyRole
  ```

**Verification:**

```bash
aws eks list-access-entries --cluster-name my-cluster
kubectl get nodes
```

### Cause D: IAM role ARN contains a path segment unsupported by aws-auth

**Statement:** The aws-auth ConfigMap entry uses an IAM role ARN with a path (e.g., `/division/team/`) that the AWS IAM Authenticator does not support, causing identity lookup to fail silently.

**Mechanism:** The AWS IAM Authenticator only matches `mapRoles` entries against ARNs in the form `arn:aws:iam::ACCOUNT:role/ROLENAME` with no path prefix. When the STS-resolved ARN for a session includes a path (e.g., `arn:aws:iam::111122223333:role/ops/MyRole`), the string does not match the pathless entry and authentication fails.

**Indicator:**

- [Step 5] ConfigMap entry contains `rolearn` with a slash-separated path segment such as `arn:aws:iam::111122223333:role/ops/MyRole`
- [Step 1] Caller ARN includes a path in its role segment

<!-- match: {"step": 5, "predicate": "contains", "target": "role/ops/"} -->

**Mitigation:**

- **Risk:** Low — replaces an incorrect mapping with a correct one. Existing sessions using the old ARN lose access until the fix propagates (seconds).
- **Command:**

  ```bash
  # Remove the path-containing entry
  eksctl delete iamidentitymapping --cluster my-cluster \
    --arn arn:aws:iam::111122223333:role/ops/MyRole

  # Add with path-free ARN
  eksctl create iamidentitymapping --cluster my-cluster \
    --arn arn:aws:iam::111122223333:role/MyRole \
    --group system:masters \
    --username admin
  ```

- **Duration:** Immediate.

**Resolution:** Same as Mitigation.

**Verification:**

```bash
eksctl get iamidentitymapping --cluster my-cluster | grep MyRole
kubectl get nodes
```

Confirm no path segment appears in the `ARN` column.

### Cause E: Worker node IAM role not mapped — nodes cannot register

**Statement:** Worker nodes fail to join the cluster because the node IAM role ARN is not present in the aws-auth ConfigMap or as an EKS access entry, blocking kubelet registration.

**Mechanism:** When a node boots, kubelet generates a TLS bootstrap token using `aws eks get-token` with the node's IAM role (via instance profile). The Kubernetes API server calls the AWS IAM Authenticator, which looks for a `mapRoles` entry matching the node role ARN. Without a mapping assigning the `system:bootstrappers` and `system:nodes` groups, the API server returns `Unauthorized` and the node never transitions to `Ready`.

**Indicator:**

- [Step 7] Node IAM role ARN absent from ConfigMap `mapRoles` entries or from `aws eks list-access-entries`
- [Symptom] Kubelet logs show `Unable to register node ... with API server: Unauthorized`

<!-- match: {"step": 7, "predicate": "absent", "target": "system:bootstrappers"} -->

**Mitigation:**

- **Risk:** Low — restores the minimum required node-to-cluster trust. Uses least-privilege groups `system:bootstrappers` and `system:nodes`.
- **Command:**

  ```bash
  # For CONFIG_MAP clusters
  eksctl create iamidentitymapping --cluster my-cluster \
    --arn arn:aws:iam::111122223333:role/MyNodeRole \
    --group system:bootstrappers,system:nodes \
    --username system:node:{{EC2PrivateDNSName}}

  # For API-mode clusters
  aws eks create-access-entry --cluster-name my-cluster \
    --principal-arn arn:aws:iam::111122223333:role/MyNodeRole \
    --type EC2_LINUX
  ```

- **Duration:** Nodes re-register within 2–5 minutes.

**Resolution:** Same as Mitigation.

**Verification:**

```bash
kubectl get nodes -o wide
```

All nodes should appear in `Ready` status within 5 minutes. If nodes remain `NotReady`, check kubelet logs via SSM: `aws ssm start-session --target INSTANCE_ID` then `sudo journalctl -u kubelet -f`.

### Cause F: Cluster creator IAM principal deleted with no other admin mapping

**Statement:** The original cluster-creator IAM identity has been deleted and no other admin mapping exists, leaving the cluster without any administrative kubectl access.

**Mechanism:** The IAM principal that creates an EKS cluster has implicit admin access when `CONFIG_MAP` mode is used — they are the first and only authorized user. If this account or role is deleted and no other principal was added to the ConfigMap or access entries, there is no kubectl-accessible identity. Recovery requires IAM-level AWS API calls (not kubectl), which remain available as long as the AWS account has `eks:UpdateClusterConfig` permission.

**Indicator:**

- [Step 5] `eksctl get iamidentitymapping` returns empty output or the command fails entirely
- [Step 6] `aws eks list-access-entries` returns no admin-level entries
- [Symptom] All kubectl commands return `Unauthorized` regardless of which IAM identity is used

<!-- match: {"step": 5, "predicate": "absent", "target": "system:masters"} -->

**Mitigation:**

- **Risk:** Medium — changes the cluster authentication mode permanently (API mode cannot be disabled after enabling). Requires `eks:UpdateClusterConfig` and `eks:CreateAccessEntry` IAM permissions.
- **Command:**

  ```bash
  # Enable API-based auth mode to allow recovery without kubectl
  aws eks update-cluster-config --name my-cluster \
    --access-config authenticationMode=API_AND_CONFIG_MAP

  # Wait for update to complete
  aws eks describe-cluster --name my-cluster \
    --query 'cluster.accessConfig.authenticationMode' --output text

  # Create a recovery access entry
  aws eks create-access-entry --cluster-name my-cluster \
    --principal-arn arn:aws:iam::111122223333:role/RecoveryRole \
    --type STANDARD

  aws eks associate-access-policy --cluster-name my-cluster \
    --principal-arn arn:aws:iam::111122223333:role/RecoveryRole \
    --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
    --access-scope type=cluster
  ```

- **Duration:** 2–5 minutes for the mode update to propagate.

**Resolution:** Same as Mitigation.

**Impact:** Cluster-wide — permanently enables API authentication mode alongside ConfigMap. Cannot be reverted to `CONFIG_MAP`-only.

**Rollback:** Not applicable — mode change is one-way. Remove the recovery role after restoring the intended admin mappings.

**Verification:**

```bash
aws eks update-kubeconfig --name my-cluster --region us-east-1 \
  --role-arn arn:aws:iam::111122223333:role/RecoveryRole
kubectl auth whoami
kubectl get nodes
```

### Cause G: OIDC provider missing or mismatched — IRSA pods cannot authenticate

**Statement:** The EKS cluster's OIDC provider is not registered in IAM, so pods that use IAM Roles for Service Accounts cannot exchange service account tokens for AWS credentials.

**Mechanism:** IRSA works by annotating a Kubernetes service account with an IAM role ARN. The pod token volume projection issues a OIDC-format JWT signed by the cluster's issuer. IAM's `AssumeRoleWithWebIdentity` validates the JWT against the registered OIDC provider. If no matching provider exists in IAM, the STS call fails and the pod receives `InvalidClientTokenId` or `AccessDenied`.

**Indicator:**

- [Step 8] Cluster OIDC issuer URL does not match any ARN in `aws iam list-open-id-connect-providers` output
- [Symptom] Pods fail with `InvalidClientTokenId` or `An error occurred (AccessDenied) when calling AssumeRoleWithWebIdentity`

<!-- match: {"step": 8, "predicate": "absent", "target": "oidc.eks"} -->

**Mitigation:**

- **Risk:** Low — creates an IAM resource without modifying the cluster. If a provider already exists for the cluster but with a stale thumbprint, delete it first.
- **Command:**

  ```bash
  eksctl utils associate-iam-oidc-provider \
    --cluster my-cluster --approve
  ```

- **Duration:** Immediate — OIDC provider creation is instant; pods using IRSA may need to restart to pick up new tokens.

**Resolution:** Same as Mitigation.

**Verification:**

```bash
# Confirm provider is registered
aws iam list-open-id-connect-providers

# Test IRSA from a pod
kubectl run irsa-test --image=amazon/aws-cli --restart=Never \
  --overrides='{"spec":{"serviceAccountName":"my-service-account"}}' \
  -- sts get-caller-identity
kubectl logs irsa-test
kubectl delete pod irsa-test
```

IRSA is working if the output shows the IAM role ARN associated with the service account, not the node instance role.

### Cause H: STS regional endpoint disabled for the AWS region

**Statement:** The AWS STS regional endpoint for the cluster's region is not enabled, causing token generation to fail with `InvalidClientTokenId`.

**Mechanism:** `aws eks get-token` calls the STS regional endpoint (e.g., `sts.us-west-2.amazonaws.com`) to generate the presigned authentication token. If the regional endpoint is not activated in the AWS account settings, the STS call fails with `InvalidClientTokenId`. This failure is distinct from an IAM permission error — the credential itself is invalid, not unauthorized.

**Indicator:**

- [Step 3] `aws eks get-token` fails with `InvalidClientTokenId`
- [Symptom] Error message contains `The security token included in the request is invalid`

<!-- match: {"step": 3, "predicate": "contains", "target": "InvalidClientTokenId"} -->

**Mitigation:**

- **Risk:** Low — enabling a regional STS endpoint is an account-level setting with no security downside.
- **Command:**

  ```bash
  # Activate STS regional endpoint (AWS Console path):
  # IAM → Account settings → STS endpoints → Activate for your region
  # Or via CLI:
  aws iam set-security-token-service-preferences \
    --global-endpoint-token-version v2Token
  ```

- **Duration:** Takes effect within minutes of activation.

**Resolution:** Same as Mitigation.

**Verification:**

```bash
aws sts get-caller-identity --region us-west-2
aws eks get-token --cluster-name my-cluster --region us-west-2
kubectl get nodes
```

### Cause Z: Unidentified authentication failure

**Statement:** The authentication failure does not match any of the above causes after completing all diagnostic steps.

**Mechanism:** EKS authentication involves multiple layers — AWS credential chain, STS token generation, IAM Authenticator identity lookup, ConfigMap/access-entry resolution, and Kubernetes RBAC — and edge cases exist that require AWS Support or deeper network-level investigation.

**Indicator:**

- [Default] All Steps 1–9 completed but root cause not identified

**Mitigation:**

- **Risk:** Low — collecting diagnostic data is non-destructive.
- **Command:**

  ```bash
  # Collect node-level diagnostics
  sudo bash /etc/eks/log-collector-script/eks-log-collector.sh

  # Check cluster health issues via AWS API
  aws eks describe-cluster --name my-cluster \
    --query 'cluster.health.issues'

  # Run AWS-provided SSM automation
  # AWSSupport-TroubleshootEKSWorkerNode via Systems Manager console
  ```

- **Duration:** Escalate to AWS Support if no resolution within 30 minutes.

**Resolution:** Out of runbook scope — escalate to AWS Support with the log bundle from `eks-log-collector.sh` and the output of `aws eks describe-cluster --name my-cluster`.

**Verification:** Confirmed by AWS Support case resolution or successful `kubectl get nodes` after remediation.

## Prevention

Use API-based access management for all clusters at or above EKS platform version `eks.15` on Kubernetes 1.30+. API-based access entries are managed via IAM-auditable AWS APIs (CloudTrail), eliminate ConfigMap corruption risk, and support least-privilege namespace-scoped policies:

```bash
aws eks update-cluster-config --name my-cluster \
  --access-config authenticationMode=API_AND_CONFIG_MAP
```

Always maintain at least two admin-level mappings — one for the operations team role and one for a break-glass emergency role. Never rely on a single IAM principal for cluster administrative access.

Map IAM roles, not IAM users. Roles can be assumed by multiple team members, are easier to rotate, and support IRSA patterns for workloads.

Audit access mappings quarterly and after any team changes:

```bash
eksctl get iamidentitymapping --cluster my-cluster
aws eks list-access-entries --cluster-name my-cluster
```

In CI/CD pipelines, always pin the kubeconfig to a specific IAM role using `--role-arn` so access does not depend on ambient credentials:

```bash
aws eks update-kubeconfig --name my-cluster --region us-east-1 \
  --role-arn arn:aws:iam::111122223333:role/CI-EKSRole
```

Enable CloudTrail logging for `eks:*` and `sts:AssumeRole` API calls to enable retroactive diagnosis of future authentication failures.

Set up an alerting rule on CloudWatch for `aws eks describe-cluster` returning cluster health issues of type `ASSUME_ROLE_ACCESS_DENIED` or `PERMISSION_ACCESS_DENIED` — these indicate the cluster IAM role is broken before users hit authentication failures.

## Sources

- [AWS EKS: Troubleshoot problems with Amazon EKS clusters and nodes](https://docs.aws.amazon.com/eks/latest/userguide/troubleshooting.html) — Official EKS troubleshooting guide covering `Unauthorized`/`AccessDenied` errors, node registration failures, aws-auth ConfigMap management, access entries, STS regional endpoint issues, and cluster health codes.
- [AWS EKS: Grant IAM users and roles access to Kubernetes APIs](https://docs.aws.amazon.com/eks/latest/userguide/grant-k8s-access.html) — Authentication mode reference (`CONFIG_MAP`, `API`, `API_AND_CONFIG_MAP`), access entries vs ConfigMap comparison, migration guidance.
- [AWS EKS: Troubleshooting IAM](https://docs.aws.amazon.com/eks/latest/userguide/security_iam_troubleshoot.html) — IAM-specific EKS troubleshooting for role assumption and token generation failures.
