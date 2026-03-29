---
id: eks-cluster-authentication
title: "AWS EKS Cluster Authentication Failures: Diagnosis and Resolution"
domain: compute
service: aws-eks
symptom_class:
  - auth_failure
severity: high
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - aws
  - eks
  - kubernetes
  - authentication
  - iam
  - aws-auth
  - oidc
  - kubectl
difficulty: intermediate
---

# AWS EKS Cluster Authentication Failures: Diagnosis and Resolution

## Problem Definition

This runbook applies to Amazon EKS clusters in any AWS region running Kubernetes 1.23 or later. You need the AWS CLI v2 with `eks:DescribeCluster` and `sts:GetCallerIdentity` permissions, `kubectl` 1.23+, and optionally `eksctl` for managing IAM identity mappings. For clusters using API-based access management (EKS platform version `eks.15`+ on Kubernetes 1.30+), you also need `eks:ListAccessEntries` and `eks:CreateAccessEntry` permissions.

Kubectl commands against an EKS cluster fail with `Unauthorized`, `Forbidden`, or `Access Denied` errors. Users, CI/CD pipelines, or node groups cannot authenticate to the Kubernetes API server, preventing cluster management, deployments, and node registration. EKS authentication is a two-step process: AWS IAM authenticates the caller's identity via `aws eks get-token`, then the Kubernetes API server maps the IAM identity to a Kubernetes user/group using either EKS access entries (API-based) or the `aws-auth` ConfigMap (legacy). A failure at either step produces an authentication error.

The most frequent causes are: kubeconfig using the wrong IAM identity or AWS profile, IAM principal not mapped in the `aws-auth` ConfigMap or access entries, IAM role ARN containing a path that EKS does not support, OIDC provider missing for IAM Roles for Service Accounts (IRSA), expired or incorrect STS token, and the cluster creator's IAM principal being deleted with no other admin mapping configured.

**Typical error presentation:**

```text
$ kubectl get pods
error: You must be logged in to the server (Unauthorized)
```

```text
$ kubectl get nodes
could not get token: AccessDenied: Access denied
```

For node registration failures:

```text
Container runtime network not ready: NetworkReady=false reason:NetworkPluginNotReady
Unable to register node with API server: Unauthorized
```

## Diagnostic Steps

### Step 1: Verify Your Current AWS Identity

**What this checks:** Which IAM user or role your kubectl session is using, so you can compare it against what the cluster authorizes.

```bash
aws sts get-caller-identity
```

**Expected output:** JSON with `UserId`, `Account`, and `Arn` fields showing the IAM principal.

**What the finding means:** If the ARN does not match the IAM principal authorized in the cluster, your AWS CLI is using the wrong profile, environment variable, or instance profile. The `Account` field confirms whether you are in the correct AWS account.

### Step 2: Verify Kubeconfig Configuration

**What this checks:** Whether the kubeconfig points to the correct cluster and uses the correct authentication command.

```bash
# Show current kubeconfig context
kubectl config current-context

# Show the full kubeconfig entry for the current context
kubectl config view --minify
```

**Expected output:** The `exec` section under `users` should use `aws eks get-token` with the correct `--cluster-name` and `--region`.

**What the finding means:** If an `--role-arn` is specified in the exec args, verify that role exists and you can assume it. If the cluster name or region is wrong, the token will be generated for the wrong cluster.

### Step 3: Test Token Generation

**What this checks:** Whether your IAM identity can generate a valid authentication token for the cluster.

```bash
aws eks get-token --cluster-name my-cluster --region us-east-1
```

**Expected output:** JSON containing a `token` field and an `expirationTimestamp`.

**What the finding means:** If this fails with `AccessDenied`, the IAM identity does not have permission to call `eks:DescribeCluster` or the STS token is invalid. If it succeeds but kubectl still fails, the issue is in the IAM-to-Kubernetes mapping (Step 4 or 5).

### Step 4: Check the Cluster Authentication Mode

**What this checks:** Whether the cluster uses ConfigMap-based, API-based, or dual authentication, which determines where identity mappings are stored.

```bash
aws eks describe-cluster --name my-cluster \
  --query 'cluster.accessConfig.authenticationMode'
```

**Expected output:** One of `CONFIG_MAP`, `API`, or `API_AND_CONFIG_MAP`.

**What the finding means:** `CONFIG_MAP` means only the `aws-auth` ConfigMap is used (check Step 5). `API` means only EKS access entries are used (check Step 6). `API_AND_CONFIG_MAP` means both are active and either can grant access.

### Step 5: Check aws-auth ConfigMap (Legacy Authentication)

**What this checks:** Whether your IAM principal is mapped to a Kubernetes user/group in the ConfigMap.

```bash
# Using eksctl (recommended)
eksctl get iamidentitymapping --cluster my-cluster

# Or directly via kubectl (requires existing access)
kubectl get configmap aws-auth -n kube-system -o yaml
```

**Expected output:** Your IAM role or user ARN listed in `mapRoles` or `mapUsers` with appropriate `groups`.

**What the finding means:** If your IAM principal does not appear, it has no Kubernetes access via ConfigMap. Verify the `groups` list includes the required Kubernetes group (e.g., `system:masters` for admin access, `system:bootstrappers` and `system:nodes` for worker nodes). IAM role ARNs must not contain a path (e.g., use `arn:aws:iam::111122223333:role/MyRole`, not `arn:aws:iam::111122223333:role/path/to/MyRole`).

### Step 6: Check EKS Access Entries (API-Based Authentication)

**What this checks:** Whether your IAM principal has an access entry in the cluster via the EKS API.

```bash
# List all access entries for the cluster
aws eks list-access-entries --cluster-name my-cluster

# Describe a specific access entry
aws eks describe-access-entry --cluster-name my-cluster \
  --principal-arn arn:aws:iam::111122223333:role/MyRole
```

**Expected output:** Your IAM principal ARN listed in the access entries.

**What the finding means:** If your IAM principal does not appear, it has no Kubernetes access via the API method. You need to create an access entry and associate an access policy.

### Step 7: Verify OIDC Provider (for IRSA / Service Account Issues)

**What this checks:** Whether the cluster's OIDC provider is registered in IAM, which is required for pods using IAM Roles for Service Accounts.

```bash
# Get the OIDC issuer URL
aws eks describe-cluster --name my-cluster \
  --query 'cluster.identity.oidc.issuer' --output text

# List IAM OIDC providers and check if the cluster's provider exists
aws iam list-open-id-connect-providers
```

**Expected output:** The OIDC issuer URL from the cluster should match one of the registered providers.

**What the finding means:** If pods using service accounts with IAM roles fail to authenticate to AWS services, the OIDC provider may be missing or its thumbprint may be stale.

### Step 8: Check kubectl Verbose Output

**What this checks:** Whether the failure is authentication (401) or authorization (403), which are different problems with different fixes.

```bash
kubectl get pods -v=6
```

**Expected output:** HTTP request and response details including status codes.

**What the finding means:** `401 Unauthorized` means authentication failed (IAM identity not recognized). `403 Forbidden` means authentication succeeded but the Kubernetes user lacks RBAC permissions for the requested action.

## Mitigation

### Option 1: Update Kubeconfig with Correct Credentials

Use when the kubeconfig is stale or pointing to the wrong IAM identity.

- **Risk:** Low. Only updates the local kubeconfig file.
- **Command:**

  ```bash
  # Update kubeconfig for the cluster
  aws eks update-kubeconfig --name my-cluster --region us-east-1

  # If you need to use a specific IAM role
  aws eks update-kubeconfig --name my-cluster --region us-east-1 \
    --role-arn arn:aws:iam::111122223333:role/EKSAdminRole
  ```

- **Verify:**

  ```bash
  kubectl get nodes
  ```

  The command should succeed and list cluster nodes.
- **Duration:** Immediate.

### Option 2: Add IAM Identity Mapping via eksctl

Use when the IAM principal is not in the aws-auth ConfigMap.

- **Risk:** Medium. Grants Kubernetes access to the specified IAM identity. Use the least-privilege group; avoid `system:masters` for non-admin users.
- **Command:**

  ```bash
  # Add an IAM role mapping
  eksctl create iamidentitymapping --cluster my-cluster \
    --arn arn:aws:iam::111122223333:role/MyRole \
    --group system:masters \
    --username admin-user
  ```

- **Verify:**

  ```bash
  eksctl get iamidentitymapping --cluster my-cluster
  kubectl get nodes
  ```

  The identity should appear in the mapping list and kubectl commands should succeed.
- **Duration:** Immediate. The mapping takes effect within seconds.

### Option 3: Create an EKS Access Entry

For clusters using API-based access management.

- **Risk:** Medium. Grants cluster access at the API level. Access policies control what the principal can do.
- **Command:**

  ```bash
  # Create access entry for a user or role
  aws eks create-access-entry --cluster-name my-cluster \
    --principal-arn arn:aws:iam::111122223333:role/MyRole \
    --type STANDARD

  # Associate an access policy for cluster admin
  aws eks associate-access-policy --cluster-name my-cluster \
    --principal-arn arn:aws:iam::111122223333:role/MyRole \
    --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
    --access-scope type=cluster
  ```

- **Verify:**

  ```bash
  aws eks list-access-entries --cluster-name my-cluster
  kubectl get nodes
  ```

- **Duration:** Immediate.

### Option 4: Fix Node Group Authentication

When worker nodes fail to register with `Unauthorized`.

- **Risk:** Low. Restores the required node-to-API-server authentication mapping.
- **Command:**

  ```bash
  # For aws-auth ConfigMap clusters
  eksctl create iamidentitymapping --cluster my-cluster \
    --arn arn:aws:iam::111122223333:role/NodeInstanceRole \
    --group system:bootstrappers,system:nodes \
    --username system:node:{{EC2PrivateDNSName}}

  # For API-based access clusters
  aws eks create-access-entry --cluster-name my-cluster \
    --principal-arn arn:aws:iam::111122223333:role/NodeInstanceRole \
    --type EC2_LINUX
  ```

- **Verify:**

  ```bash
  kubectl get nodes
  ```

  Nodes should appear and transition to `Ready` status within 2-5 minutes.
- **Duration:** Nodes register within 2-5 minutes.

## Root Cause Resolution

**If** Step 1 shows an IAM identity different from what was authorized **then** the AWS CLI is using the wrong profile, environment variable, or instance profile. Fix the credential chain:

```bash
# Check which credentials are being used and why
aws configure list

# Set the correct profile explicitly
export AWS_PROFILE=eks-admin

# Or unset conflicting environment variables
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN

# Regenerate kubeconfig
aws eks update-kubeconfig --name my-cluster --region us-east-1
```

**If** Step 5 shows a role ARN with a path (e.g., `arn:aws:iam::111122223333:role/path/to/MyRole`) **then** EKS does not support paths in the aws-auth ConfigMap. Replace the mapping with the path-free ARN:

```bash
# Remove the incorrect mapping
eksctl delete iamidentitymapping --cluster my-cluster \
  --arn arn:aws:iam::111122223333:role/path/to/MyRole

# Add with the correct ARN format (no path)
eksctl create iamidentitymapping --cluster my-cluster \
  --arn arn:aws:iam::111122223333:role/MyRole \
  --group system:masters \
  --username admin
```

**If** the ConfigMap uses an instance profile ARN instead of a role ARN **then** replace it with the role ARN. Instance profile ARNs look like `arn:aws:iam::111122223333:instance-profile/MyRole`; the correct format is `arn:aws:iam::111122223333:role/MyRole`.

**If** Step 5 shows the aws-auth ConfigMap is missing or corrupted **then** recreate it using the cluster creator's credentials (who has implicit admin access):

```yaml
# Save as aws-auth.yaml and apply with: kubectl apply -f aws-auth.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: aws-auth
  namespace: kube-system
data:
  mapRoles: |
    - rolearn: arn:aws:iam::111122223333:role/NodeInstanceRole
      username: system:node:{{EC2PrivateDNSName}}
      groups:
        - system:bootstrappers
        - system:nodes
    - rolearn: arn:aws:iam::111122223333:role/AdminRole
      username: admin
      groups:
        - system:masters
```

**If** Step 7 shows no OIDC provider for the cluster **then** create one:

```bash
eksctl utils associate-iam-oidc-provider --cluster my-cluster --approve
```

**If** the IAM principal that created the cluster has been deleted and no other admin mapping exists **then** use the EKS API to recover access (requires IAM permissions, not kubectl):

```bash
# Switch authentication mode to include API
aws eks update-cluster-config --name my-cluster \
  --access-config authenticationMode=API_AND_CONFIG_MAP

# Create an access entry for a recovery role
aws eks create-access-entry --cluster-name my-cluster \
  --principal-arn arn:aws:iam::111122223333:role/RecoveryRole \
  --type STANDARD
aws eks associate-access-policy --cluster-name my-cluster \
  --principal-arn arn:aws:iam::111122223333:role/RecoveryRole \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
  --access-scope type=cluster
```

## Verification

After applying a fix, confirm authentication works end-to-end:

```bash
# Verify kubectl authentication and identity (requires Kubernetes 1.27+)
kubectl auth whoami
```

This returns the Kubernetes username and groups your IAM identity is mapped to. For older clusters:

```bash
kubectl get nodes
```

A successful response with node list confirms authentication and basic authorization.

```bash
# Verify node registration (if fixing node auth)
kubectl get nodes -o wide
```

All expected nodes should be in `Ready` status. If nodes remain `NotReady`, check kubelet logs on the node via SSM:

```bash
aws ssm start-session --target i-node-instance-id
# Then: sudo journalctl -u kubelet -f
```

```bash
# Verify IRSA (if applicable)
kubectl run test-irsa --image=amazon/aws-cli --restart=Never \
  --overrides='{"spec":{"serviceAccountName":"my-service-account"}}' \
  -- sts get-caller-identity
kubectl logs test-irsa
kubectl delete pod test-irsa
```

The output should show the IAM role associated with the service account, not the node instance role.

## Prevention

### Use API-Based Access Management

Migrate from the aws-auth ConfigMap to EKS access entries. Access entries are managed via the EKS API, are auditable via CloudTrail, and do not risk cluster lockout from a corrupted ConfigMap:

```bash
aws eks update-cluster-config --name my-cluster \
  --access-config authenticationMode=API_AND_CONFIG_MAP
```

### Always Have Multiple Admin Identities

Never rely on a single IAM principal for cluster admin access. Create at least two admin mappings: one for the operations team role and one for a break-glass emergency role stored in a secure vault.

### Use IAM Roles, Not IAM Users

Map IAM roles (not individual users) in the aws-auth ConfigMap or access entries. Roles can be assumed by multiple team members and are easier to rotate than user credentials.

### Audit Access Mappings Regularly

Review aws-auth ConfigMap or access entries quarterly. Remove mappings for departed team members and unused roles:

```bash
eksctl get iamidentitymapping --cluster my-cluster
aws eks list-access-entries --cluster-name my-cluster
```

### Enable CloudTrail Logging for EKS

Ensure CloudTrail is logging EKS API calls (`eks:DescribeCluster`, `sts:GetCallerIdentity`) and Kubernetes audit logs to diagnose authentication failures retroactively.

### Pin kubeconfig to Specific IAM Roles

In CI/CD pipelines, always specify `--role-arn` in `aws eks update-kubeconfig` to avoid depending on ambient credentials that may change.

## Sources

- [AWS EKS: Troubleshooting](https://docs.aws.amazon.com/eks/latest/userguide/troubleshooting.html) - Official EKS troubleshooting guide covering authentication errors, aws-auth ConfigMap, access entries, and node registration issues.
- [AWS EKS: Grant IAM Access to Kubernetes](https://docs.aws.amazon.com/eks/latest/userguide/grant-k8s-access.html) - Detailed guide on EKS access entries and IAM-to-Kubernetes identity mapping.
- [AWS EKS: Enabling IAM Principal Access](https://docs.aws.amazon.com/eks/latest/userguide/add-user-role.html) - aws-auth ConfigMap configuration reference.
- [AWS EKS: Troubleshooting IAM](https://docs.aws.amazon.com/eks/latest/userguide/security_iam_troubleshoot.html) - IAM-specific EKS troubleshooting for role assumption and token generation failures.
- [eksctl: IAM Identity Mappings](https://eksctl.io/usage/iam-identity-mappings/) - eksctl documentation for managing aws-auth ConfigMap entries.
