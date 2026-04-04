---
id: k8s-rbac-forbidden
title: "Kubernetes RBAC 403 Forbidden"
domain: security
service: kubernetes
symptom_class:
  - auth_failure
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - rbac
  - forbidden
  - clusterrole
  - rolebinding
  - serviceaccount
  - authorization
difficulty: intermediate
---

# Kubernetes RBAC 403 Forbidden

## Problem Definition

Applies to Kubernetes clusters v1.6+ with RBAC enabled (default since v1.6). Requires `cluster-admin` or equivalent read permissions on RBAC resources (`roles`, `rolebindings`, `clusterroles`, `clusterrolebindings`) for diagnosis. Affects both human users (via kubeconfig) and ServiceAccounts (pods making API calls).

Kubernetes RBAC 403 Forbidden errors occur when the API server rejects a request because the authenticated identity lacks the required permissions. The caller sees:

```
Error from server (Forbidden): pods is forbidden: User "system:serviceaccount:default:my-app"
cannot list resource "pods" in API group "" in the namespace "production"
```

```
Error from server (Forbidden): clusterroles.rbac.authorization.k8s.io is forbidden:
User "developer@example.com" cannot create resource "clusterroles" in API group
"rbac.authorization.k8s.io" at the cluster scope
```

The error message always contains four key pieces of information:

- **Who** — the authenticated user or ServiceAccount (`User` field).
- **What** — the verb and resource (`cannot list resource "pods"`).
- **Where** — the namespace or cluster scope (`in the namespace "production"` or `at the cluster scope`).
- **API group** — the resource's API group (`in API group ""` for core resources, or `"rbac.authorization.k8s.io"` for RBAC resources).

Common failure scenarios:

- **Missing RoleBinding or ClusterRoleBinding** — a Role/ClusterRole exists but is not bound to the subject.
- **Namespace scoping mismatch** — a RoleBinding grants access in namespace A, but the request targets namespace B.
- **ServiceAccount not assigned to pod** — the pod uses the `default` ServiceAccount which has no additional permissions.
- **Aggregated ClusterRole missing labels** — an aggregation rule does not match the expected child ClusterRoles.
- **Impersonation without permission** — a user impersonates another identity without `impersonate` verb permissions.

## Diagnostic Steps

### Step 1. Identify the subject and the denied action

Extracts the user/ServiceAccount, verb, resource, API group, and namespace from the error message. This information is needed for all subsequent diagnostic steps.

```bash
# Parse the error message for the key fields
# User: system:serviceaccount:default:my-app
# Verb: list
# Resource: pods
# API group: "" (core)
# Namespace: production
```

If the error comes from a pod, identify the ServiceAccount:

```bash
kubectl get pod my-app-pod -n default -o jsonpath='{.spec.serviceAccountName}'
```

Expected output is the ServiceAccount name (e.g., `my-app`). If the output is empty or `default`, the pod is using the default ServiceAccount which typically has no additional RBAC permissions.

### Step 2. Check if the subject can perform the action

Uses the `auth can-i` subcommand to verify whether the RBAC configuration permits the action. This queries the API server's authorization module directly.

```bash
# Check from the subject's perspective
kubectl auth can-i list pods -n production --as=system:serviceaccount:default:my-app
```

Expected output is `yes` or `no`. If `no`, the RBAC configuration does not permit this action. Use `--list` to see all permissions the subject has:

```bash
kubectl auth can-i --list --as=system:serviceaccount:default:my-app -n production
```

This returns a table of all permitted resources, verbs, and API groups in the specified namespace.

### Step 3. List all RoleBindings and ClusterRoleBindings for the subject

Enumerates every binding that references the subject to understand what permissions are currently granted and in which namespaces.

```bash
# Namespace-scoped bindings in the target namespace
kubectl get rolebindings -n production -o json | \
  python3 -c "
import json, sys
data = json.load(sys.stdin)
for rb in data['items']:
    for s in rb.get('subjects', []):
        if s.get('name') == 'my-app' or s.get('name') == 'system:serviceaccount:default:my-app':
            print(f\"RoleBinding: {rb['metadata']['name']} -> Role: {rb['roleRef']['name']}\")
"
```

```bash
# Cluster-scoped bindings
kubectl get clusterrolebindings -o json | \
  python3 -c "
import json, sys
data = json.load(sys.stdin)
for crb in data['items']:
    for s in crb.get('subjects', []):
        if s.get('name') == 'my-app' or s.get('name') == 'system:serviceaccount:default:my-app':
            print(f\"ClusterRoleBinding: {crb['metadata']['name']} -> ClusterRole: {crb['roleRef']['name']}\")
"
```

If no bindings are found, the subject has no RBAC permissions beyond the default (which is typically none for custom ServiceAccounts). This is the most common root cause.

### Step 4. Inspect the referenced Role or ClusterRole

Reads the rules in the Role/ClusterRole to verify it includes the required verb, resource, and API group.

```bash
kubectl get role my-app-role -n production -o yaml
```

Or for a ClusterRole:

```bash
kubectl get clusterrole my-app-clusterrole -o yaml
```

Check the `rules` section for an entry that matches all three: the API group, the resource name, and the verb. A missing verb (e.g., `list` is present but `watch` is not) or a missing resource (e.g., `pods` but not `pods/log`) causes the 403.

### Step 5. Verify namespace scoping

Confirms that the RoleBinding is in the same namespace as the request. A RoleBinding in namespace `staging` does not grant access to namespace `production`.

```bash
kubectl get rolebindings -A -o json | \
  python3 -c "
import json, sys
data = json.load(sys.stdin)
for rb in data['items']:
    for s in rb.get('subjects', []):
        if s.get('name') == 'my-app':
            print(f\"Namespace: {rb['metadata']['namespace']} -> Role: {rb['roleRef']['name']}\")
"
```

If the binding exists in the wrong namespace, you need either a new RoleBinding in the correct namespace or a ClusterRoleBinding for cluster-wide access.

### Step 6. Check for aggregated ClusterRole issues

If the ClusterRole uses aggregation rules, verifies that the label selectors match the child ClusterRoles.

```bash
kubectl get clusterrole my-aggregated-role -o jsonpath='{.aggregationRule}'
```

If an aggregation rule is present, list the child ClusterRoles that match:

```bash
kubectl get clusterroles -l rbac.example.com/aggregate-to-my-role=true
```

If no child ClusterRoles match the label selector, the aggregated ClusterRole has no rules and all requests are denied.

### Step 7. Check API server audit logs

Retrieves the audit log entry for the denied request, providing the full authorization decision including which authorizer rejected it.

```bash
# Location varies by cluster setup. Common paths:
# Managed (EKS/GKE/AKS): CloudWatch Logs, Cloud Logging, or Azure Monitor
# Self-managed: /var/log/kubernetes/audit.log on control plane nodes

# For EKS:
aws logs filter-log-events \
  --log-group-name /aws/eks/my-cluster/cluster \
  --start-time "$(date -u -d '30 minutes ago' +%s)000" \
  --filter-pattern '{ $.responseStatus.code = 403 }'
```

The audit log shows the full request context and the authorization decision, confirming whether RBAC, webhook, or another authorizer rejected the request.

## Mitigation

### Option 1: Bind the subject to an existing permissive ClusterRole

**Risk**: The `view` ClusterRole grants read access to most resources cluster-wide (when used with ClusterRoleBinding) or namespace-wide (when used with RoleBinding). This is broader than a targeted Role but safe for read-only diagnostics.

**Command**:

```bash
kubectl create rolebinding my-app-view \
  --clusterrole=view \
  --serviceaccount=default:my-app \
  -n production
```

**Verify**:

```bash
kubectl auth can-i list pods -n production --as=system:serviceaccount:default:my-app
```

Expected output: `yes`.

**Duration**: Remove within 24 hours and replace with a purpose-built Role.

### Option 2: Temporarily grant cluster-admin (emergency only)

**Risk**: `cluster-admin` grants unrestricted access to the entire cluster. Use only for break-glass scenarios when the exact missing permission is unknown and the service is down.

**Command**:

```bash
kubectl create clusterrolebinding my-app-emergency \
  --clusterrole=cluster-admin \
  --serviceaccount=default:my-app
```

**Verify**:

```bash
kubectl auth can-i '*' '*' --as=system:serviceaccount:default:my-app
```

Expected output: `yes`.

**Duration**: Remove within 1 hour. Document the temporary grant in your incident channel.

## Root Cause Resolution

**If** no RoleBinding or ClusterRoleBinding exists for the subject → create the appropriate binding:

```bash
# Create a Role with the minimum required permissions
kubectl create role my-app-role -n production \
  --verb=get,list,watch \
  --resource=pods

# Bind it to the ServiceAccount
kubectl create rolebinding my-app-binding -n production \
  --role=my-app-role \
  --serviceaccount=default:my-app
```

**If** the Role exists but is missing the required verb or resource → patch the Role:

```bash
kubectl patch role my-app-role -n production --type=json \
  -p='[{"op": "add", "path": "/rules/-", "value": {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]}}]'
```

**If** the binding references the wrong namespace for the ServiceAccount → fix the subject namespace in the binding:

```bash
kubectl edit rolebinding my-app-binding -n production
# Ensure subjects[].namespace matches the ServiceAccount's namespace
```

**If** the pod is using the default ServiceAccount → assign the correct ServiceAccount to the pod:

```bash
kubectl patch deployment my-app -n default \
  -p '{"spec": {"template": {"spec": {"serviceAccountName": "my-app"}}}}'
```

**If** a ClusterRole aggregation is broken → add the correct label to the child ClusterRole:

```bash
kubectl label clusterrole my-child-role rbac.example.com/aggregate-to-my-role=true
```

**If** a user needs cross-namespace access → use a ClusterRoleBinding instead of per-namespace RoleBindings:

```bash
kubectl create clusterrolebinding my-app-cluster-binding \
  --clusterrole=my-app-clusterrole \
  --serviceaccount=default:my-app
```

**If** impersonation is failing → grant the impersonate verb on the target user/group/serviceaccount:

```bash
kubectl create clusterrole impersonator \
  --verb=impersonate \
  --resource=users,groups,serviceaccounts

kubectl create clusterrolebinding allow-impersonation \
  --clusterrole=impersonator \
  --user=admin@example.com
```

## Verification

1. Re-run the original kubectl command or trigger the pod's API call and confirm it succeeds:

```bash
kubectl auth can-i list pods -n production --as=system:serviceaccount:default:my-app
```

Expected output: `yes`.

2. List the subject's effective permissions to confirm the full set:

```bash
kubectl auth can-i --list --as=system:serviceaccount:default:my-app -n production
```

3. If the issue was with a pod, restart the pod and verify it operates without 403 errors in its logs:

```bash
kubectl rollout restart deployment my-app -n default
kubectl logs -l app=my-app -n default --tail=50 | grep -i "forbidden\|403"
```

Expected output: no forbidden/403 lines.

4. Remove any temporary emergency bindings:

```bash
kubectl delete clusterrolebinding my-app-emergency 2>/dev/null
kubectl delete rolebinding my-app-view -n production 2>/dev/null
```

## Prevention

1. **Define RBAC manifests in version control** alongside application deployments. Include Role, RoleBinding, and ServiceAccount in the same Helm chart or Kustomize overlay.

2. **Use the principle of least privilege** — grant only the specific verbs, resources, and namespaces the application needs. Avoid `cluster-admin` for workloads.

3. **Audit RBAC permissions regularly** with `kubectl auth can-i --list` or tools like `rbac-lookup`:

```bash
# Using rbac-lookup (https://github.com/FairwindsOps/rbac-lookup)
kubectl rbac-lookup my-app --kind serviceaccount
```

4. **Set up alerts for 403 errors in audit logs** to detect permission issues before users report them:

```bash
# For clusters with Prometheus + kube-apiserver metrics
# Alert rule: rate(apiserver_request_total{code="403"}[5m]) > 0.1
```

5. **Use namespace-scoped Roles and RoleBindings** by default. Only use ClusterRoles and ClusterRoleBindings when the workload genuinely needs cross-namespace or cluster-wide access.

6. **Test RBAC changes in staging** before production. Use `kubectl auth can-i --dry-run=server` to validate without modifying cluster state.

7. **Document each ServiceAccount's required permissions** in application documentation or Helm chart values so that RBAC configuration is part of the deployment checklist.

## Sources

- [Using RBAC Authorization - Kubernetes Documentation](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
- [Checking API Access - Kubernetes Documentation](https://kubernetes.io/docs/reference/access-authn-authz/authorization/#checking-api-access)
- [Configure Service Accounts for Pods - Kubernetes Documentation](https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/)
- [Auditing - Kubernetes Documentation](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/)
- [Role and ClusterRole - Kubernetes API Reference](https://kubernetes.io/docs/reference/kubernetes-api/authorization-resources/role-v1/)
- [RBAC Good Practices - Kubernetes Documentation](https://kubernetes.io/docs/concepts/security/rbac-good-practices/)
