---
id: "eks-cluster-authentication"
title: "AWS EKS Cluster Authentication Failures"
domain: compute
service: aws-eks
symptom_class: [auth_failure]
severity: high
scope: global
version: "2.0.1"
last_updated: "2026-08-17"
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

### Step 5: Inspect aws-auth ConfigMap

```bash
eksctl get iamidentitymapping --cluster my-cluster
```

Expected output: A table listing every mapped IAM ARN with its Kubernetes username and groups. If the command fails with an error, the ConfigMap may be missing. If the caller's ARN does not appear, they have no cluster access via this method. Role ARNs must not contain a path (e.g., `arn:aws:iam::111122223333:role/MyRole` not `arn:aws:iam::111122223333:role/path/MyRole`). Applies to `CONFIG_MAP` or `API_AND_CONFIG_MAP` clusters.

### Step 6: Inspect EKS access entries

```bash
aws eks list-access-entries --cluster-name my-cluster --output text
aws eks describe-access-entry --cluster-name my-cluster \
  --principal-arn arn:aws:iam::111122223333:role/MyRole
```

Expected output: `list-access-entries` returns all principal ARNs with access entries. `describe-access-entry` shows the entry type and associated access policies. If the caller's ARN is absent, they have no API-based access. Applies to `API` or `API_AND_CONFIG_MAP` clusters.

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

**Statement:** The AWS CLI resolves credentials from the wrong profile, environment variable, or instance metadata endpoint, so the token is generated for an identity that has no cluster access.

**Chain:**
- root: the resolved IAM identity differs from the one mapped in the cluster (wrong profile/env var/instance metadata)
- s1: `aws eks get-token` mints a cryptographically valid token bound to that wrong identity
- s2: the API server's IAM Authenticator finds no mapping for that identity
- D: kubectl is rejected with `Unauthorized`

**Indicators:**
- root: [Step 1] `Arn` in `get-caller-identity` output does not match any ARN in Step 5 or Step 6
- s1: [Step 2] kubeconfig `exec` section contains no `--role-arn` argument and ambient credentials are wrong
- D: [Symptom] kubectl returns `You must be logged in to the server (Unauthorized)`

**Interventions:**
- **remediation** (root): pin the credential source to the correct identity so the token is generated for a mapped principal.

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

  **Verification:** re-run Step 1 and `kubectl auth whoami` (Kubernetes 1.27+); the returned Kubernetes username/groups must match an authorized mapping. For older clusters use `kubectl get nodes`.

### Cause B: IAM principal missing from aws-auth ConfigMap

**Statement:** The caller's IAM role or user ARN has no entry in the `aws-auth` ConfigMap, so the Kubernetes API server cannot map the token to a Kubernetes identity.

**Chain:**
- root: no `mapRoles`/`mapUsers` entry in the `aws-auth` ConfigMap matches the caller's ARN
- s1: the IAM Authenticator decodes the valid STS token but resolves an empty Kubernetes identity
- D: kubectl is denied with `Unauthorized`

**Indicators:**
- root: [Step 5] caller's ARN does not appear in `eksctl get iamidentitymapping` output
- root: [Step 4] authentication mode is `CONFIG_MAP` or `API_AND_CONFIG_MAP`
- s1: [Step 3] token generation succeeds but kubectl returns `Unauthorized`

**Interventions:**
- **remediation** (root): add a least-privilege ConfigMap mapping for the caller's ARN.

  ```bash
  eksctl create iamidentitymapping --cluster my-cluster \
    --arn arn:aws:iam::111122223333:role/MyRole \
    --group system:masters \
    --username admin-user
  ```

  **Verification:** re-run Step 5 — the ARN now appears in the mapping table — and `kubectl get nodes` succeeds. Rollback if needed: `eksctl delete iamidentitymapping --cluster my-cluster --arn arn:aws:iam::111122223333:role/MyRole`.
- **mitigation** (root): grant a temporary broad mapping to restore access immediately, narrowing it later.

  ```bash
  eksctl create iamidentitymapping --cluster my-cluster \
    --arn arn:aws:iam::111122223333:role/MyRole \
    --group system:masters \
    --username admin-user
  ```

  **Risk:** Grants cluster-level Kubernetes access; `system:masters` is full admin — reserve it for admin roles only. **Duration:** Effective within seconds; replace with a least-privilege group before long-term use. **Verification:** re-run Step 5 and `kubectl get nodes`.

### Cause C: IAM principal missing from EKS access entries

**Statement:** The cluster uses API-based authentication mode and the caller's IAM principal has no access entry, so the EKS API returns no Kubernetes identity for the token.

**Chain:**
- root: the caller's principal ARN has no EKS access entry (and no ConfigMap entry)
- s1: on `API`/`API_AND_CONFIG_MAP` clusters, identity resolution via access entries finds no match
- D: the API server rejects kubectl with `Unauthorized`

**Indicators:**
- root: [Step 6] caller's ARN absent from `aws eks list-access-entries` output
- root: [Step 4] authentication mode is `API` or `API_AND_CONFIG_MAP`
- s1: [Step 3] token generation succeeds but kubectl returns `Unauthorized`

**Interventions:**
- **remediation** (root): create an access entry and associate a scoped access policy for the principal.

  ```bash
  aws eks create-access-entry --cluster-name my-cluster \
    --principal-arn arn:aws:iam::111122223333:role/MyRole \
    --type STANDARD

  aws eks associate-access-policy --cluster-name my-cluster \
    --principal-arn arn:aws:iam::111122223333:role/MyRole \
    --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
    --access-scope type=cluster
  ```

  **Verification:** re-run Step 6 — the ARN now appears — and `kubectl get nodes` succeeds. For least privilege, use `--access-scope type=namespace`. Rollback: `aws eks delete-access-entry --cluster-name my-cluster --principal-arn arn:aws:iam::111122223333:role/MyRole`.

### Cause D: IAM role ARN contains a path segment unsupported by aws-auth

**Statement:** The aws-auth ConfigMap entry uses an IAM role ARN with a path (e.g., `/division/team/`) that the AWS IAM Authenticator does not support, causing identity lookup to fail silently.

**Chain:**
- root: the ConfigMap `mapRoles` entry carries a path-prefixed ARN (e.g., `role/ops/MyRole`)
- s1: the STS-resolved session ARN string does not match the pathless form the authenticator requires
- D: identity lookup fails silently and kubectl is denied with `Unauthorized`

**Indicators:**
- root: [Step 5] ConfigMap entry contains `rolearn` with a slash-separated path such as `arn:aws:iam::111122223333:role/ops/MyRole`
- s1: [Step 1] caller ARN includes a path in its role segment

**Interventions:**
- **remediation** (root): replace the path-containing mapping with a path-free ARN.

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

  **Verification:** `eksctl get iamidentitymapping --cluster my-cluster | grep MyRole` shows no path segment in the `ARN` column, and `kubectl get nodes` succeeds. Existing sessions using the old ARN lose access until the fix propagates (seconds).

### Cause E: Worker node IAM role not mapped — nodes cannot register

**Statement:** Worker nodes fail to join the cluster because the node IAM role ARN is not present in the aws-auth ConfigMap or as an EKS access entry, blocking kubelet registration.

**Chain:**
- root: the node IAM role ARN has no `mapRoles` entry (or `EC2_LINUX` access entry) granting `system:bootstrappers`/`system:nodes`
- s1: kubelet's TLS bootstrap token (minted via the node instance-profile role) resolves to no authorized node identity
- s2: the API server returns `Unauthorized` for node registration
- D: the node never transitions to `Ready`

**Indicators:**
- root: [Step 7] node IAM role ARN absent from ConfigMap `mapRoles` entries or from `aws eks list-access-entries`
- s2: [Symptom] kubelet logs show `Unable to register node ... with API server: Unauthorized`

**Interventions:**
- **remediation** (root): map the node role with the least-privilege node groups (or create the `EC2_LINUX` access entry).

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

  **Verification:** `kubectl get nodes -o wide` shows all nodes `Ready` within 5 minutes (nodes re-register within 2–5 minutes). If nodes remain `NotReady`, check kubelet via SSM: `aws ssm start-session --target INSTANCE_ID` then `sudo journalctl -u kubelet -f`.

### Cause F: Cluster creator IAM principal deleted with no other admin mapping

**Statement:** The original cluster-creator IAM identity has been deleted and no other admin mapping exists, leaving the cluster without any administrative kubectl access.

**Chain:**
- root: the cluster-creator principal (implicit admin under `CONFIG_MAP` mode) is deleted and no other admin mapping was ever added
- s1: no kubectl-accessible identity remains in the ConfigMap or access entries
- D: every kubectl command returns `Unauthorized` regardless of identity

**Indicators:**
- root: [Step 5] `eksctl get iamidentitymapping` returns empty output or the command fails entirely
- root: [Step 6] `aws eks list-access-entries` returns no admin-level entries
- D: [Symptom] all kubectl commands return `Unauthorized` regardless of which IAM identity is used

**Interventions:**
- **remediation** (root): enable API auth mode (recoverable without kubectl) and create a recovery admin access entry.

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

  **Verification:** re-point kubeconfig to the recovery role and confirm access:

  ```bash
  aws eks update-kubeconfig --name my-cluster --region us-east-1 \
    --role-arn arn:aws:iam::111122223333:role/RecoveryRole
  kubectl auth whoami
  kubectl get nodes
  ```

  Note: enabling API mode is one-way (cannot revert to `CONFIG_MAP`-only); takes 2–5 minutes to propagate. Remove the recovery role after restoring intended admin mappings.

### Cause G: OIDC provider missing or mismatched — IRSA pods cannot authenticate

**Statement:** The EKS cluster's OIDC provider is not registered in IAM, so pods that use IAM Roles for Service Accounts cannot exchange service account tokens for AWS credentials.

**Chain:**
- root: no IAM OIDC provider matches the cluster's OIDC issuer URL
- s1: a pod's projected OIDC JWT cannot be validated by `AssumeRoleWithWebIdentity`
- s2: the STS credential exchange fails
- D: the pod receives `InvalidClientTokenId` or `AccessDenied`

**Indicators:**
- root: [Step 8] cluster OIDC issuer URL does not match any ARN in `aws iam list-open-id-connect-providers` output
- D: [Symptom] pods fail with `InvalidClientTokenId` or `An error occurred (AccessDenied) when calling AssumeRoleWithWebIdentity`

**Interventions:**
- **remediation** (root): register the cluster's IAM OIDC provider.

  ```bash
  eksctl utils associate-iam-oidc-provider \
    --cluster my-cluster --approve
  ```

  **Verification:** confirm registration and test IRSA from a pod:

  ```bash
  aws iam list-open-id-connect-providers
  kubectl run irsa-test --image=amazon/aws-cli --restart=Never \
    --overrides='{"spec":{"serviceAccountName":"my-service-account"}}' \
    -- sts get-caller-identity
  kubectl logs irsa-test
  kubectl delete pod irsa-test
  ```

  IRSA works if the output shows the IAM role ARN associated with the service account, not the node instance role. If a provider exists with a stale thumbprint, delete it first. Pods using IRSA may need a restart to pick up new tokens.

### Cause H: STS regional endpoint disabled for the AWS region

**Statement:** The AWS STS regional endpoint for the cluster's region is not enabled, causing token generation to fail with `InvalidClientTokenId`.

**Chain:**
- root: the STS regional endpoint for the cluster's region is not activated in account settings
- s1: `aws eks get-token` calls that regional STS endpoint and the credential is rejected as invalid (not unauthorized)
- D: token generation fails with `InvalidClientTokenId`

**Indicators:**
- root: [Step 3] `aws eks get-token` fails with `InvalidClientTokenId`
- D: [Symptom] error message contains `The security token included in the request is invalid`

**Interventions:**
- **remediation** (root): activate the regional STS endpoint for the account.

  ```bash
  # Activate STS regional endpoint (AWS Console path):
  # IAM → Account settings → STS endpoints → Activate for your region
  # Or via CLI:
  aws iam set-security-token-service-preferences \
    --global-endpoint-token-version v2Token
  ```

  **Verification:** re-run Step 3 against the region; token generation succeeds:

  ```bash
  aws sts get-caller-identity --region us-west-2
  aws eks get-token --cluster-name my-cluster --region us-west-2
  kubectl get nodes
  ```

  Takes effect within minutes of activation.

### Cause Z: Unidentified

**Statement:** The authentication failure does not match any of the above causes after completing all diagnostic steps.

**Chain:**
- root: an edge case across the credential chain, STS, IAM Authenticator, ConfigMap/access-entry resolution, or RBAC not covered above
- D: the authentication failure persists with no identified root cause

**Indicators:**
- root: [Default] all Steps 1–9 completed but root cause not identified

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the SME (AWS Support).

  ```bash
  # Collect node-level diagnostics
  sudo bash /etc/eks/log-collector-script/eks-log-collector.sh

  # Check cluster health issues via AWS API
  aws eks describe-cluster --name my-cluster \
    --query 'cluster.health.issues'

  # Run AWS-provided SSM automation
  # AWSSupport-TroubleshootEKSWorkerNode via Systems Manager console
  ```

  **Risk:** Low — collecting diagnostic data is non-destructive. **Duration:** Escalate to AWS Support if no resolution within 30 minutes. **Verification:** confirmed by AWS Support case resolution or successful `kubectl get nodes` after remediation. Escalate with the `eks-log-collector.sh` bundle and the output of `aws eks describe-cluster --name my-cluster`.

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
- [AWS EKS: Create an IAM OIDC provider for your cluster](https://docs.aws.amazon.com/eks/latest/userguide/enable-iam-roles-for-service-accounts.html) — IRSA requires an IAM OIDC provider matching the cluster's OIDC issuer URL; grounds Step 8's issuer-to-provider correspondence check (`describe-cluster` issuer vs `aws iam list-open-id-connect-providers`) and Cause G's remediation (`eksctl utils associate-iam-oidc-provider --approve`).
- [AWS STS: AssumeRoleWithWebIdentity API Reference](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html) — STS validates the service account's web identity token against the registered provider before issuing credentials; grounds Cause G's failure chain (no matching provider means the STS credential exchange fails and the pod never obtains AWS credentials).
