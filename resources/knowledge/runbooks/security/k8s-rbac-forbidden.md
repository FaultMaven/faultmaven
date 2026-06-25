---
id: "k8s-rbac-forbidden"
title: "Kubernetes RBAC 403 Forbidden"
domain: security
service: kubernetes
symptom_class: [auth_failure]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags: [rbac, forbidden, clusterrole, rolebinding, serviceaccount, authorization, impersonation]
difficulty: intermediate
---

## Symptom Recognition

The API server returns HTTP 403 with a message of the form:

```text
Error from server (Forbidden): pods is forbidden: User "system:serviceaccount:default:my-app"
cannot list resource "pods" in API group "" in the namespace "production"
```

```text
Error from server (Forbidden): clusterroles.rbac.authorization.k8s.io is forbidden:
User "developer@example.com" cannot create resource "clusterroles" in API group
"rbac.authorization.k8s.io" at the cluster scope
```

The message always encodes four diagnostically relevant fields: the authenticated identity (`User`), the denied verb, the resource (and subresource), and the namespace or cluster scope. Pods that call the Kubernetes API internally surface the same error in container logs or as a non-zero exit code from `curl`/`kubectl` inside the container. Kubernetes audit logs record the event with `responseStatus.code = 403`.

## Applicability

Applies to Kubernetes v1.6+ with RBAC enabled (default since v1.6). Diagnosis requires read access to RBAC resources (`roles`, `rolebindings`, `clusterroles`, `clusterrolebindings`) in the target namespace, plus permission to use `kubectl auth can-i --as`. Resolution requires `cluster-admin` or equivalent write access to RBAC resources. Covers both human users (via kubeconfig) and ServiceAccounts (pods making Kubernetes API calls).

## Diagnostic Steps

### Step 1: Identify subject, verb, resource, and namespace

Run the failing command and capture the exact error text. If the error originates from inside a pod, retrieve the pod's ServiceAccount before proceeding.

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.serviceAccountName}'
```

Expected output: the ServiceAccount name (e.g., `my-app`). Output of `default` or empty means the pod uses the default ServiceAccount, which has no additional RBAC permissions.

### Step 2: Confirm the denial with auth can-i

```bash
kubectl auth can-i <verb> <resource> -n <namespace> \
  --as=system:serviceaccount:<sa-namespace>:<sa-name>
```

Expected output: `no` when the permission is missing, `yes` when it is granted.

```bash
kubectl auth can-i --list \
  --as=system:serviceaccount:<sa-namespace>:<sa-name> \
  -n <namespace>
```

Expected output: a table listing all permitted verb/resource/API-group combinations for the subject in that namespace.

### Step 3: Find bindings for the subject

```bash
kubectl get rolebindings -n <namespace> -o json | \
  python3 -c "
import json, sys
data = json.load(sys.stdin)
for rb in data['items']:
    for s in rb.get('subjects', []):
        if s.get('name') in ('<sa-name>', 'system:serviceaccount:<sa-namespace>:<sa-name>'):
            print(rb['metadata']['name'], '->', rb['roleRef']['name'])
"
```

Expected output: one line per binding that references the subject. No output means the subject has no namespace-scoped bindings.

```bash
kubectl get clusterrolebindings -o json | \
  python3 -c "
import json, sys
data = json.load(sys.stdin)
for crb in data['items']:
    for s in crb.get('subjects', []):
        if s.get('name') in ('<sa-name>', 'system:serviceaccount:<sa-namespace>:<sa-name>'):
            print(crb['metadata']['name'], '->', crb['roleRef']['name'])
"
```

Expected output: one line per cluster-scoped binding. No output means the subject has no cluster-wide bindings.

### Step 4: Inspect the Role or ClusterRole rules

```bash
kubectl get role <role-name> -n <namespace> -o yaml
```

```bash
kubectl get clusterrole <clusterrole-name> -o yaml
```

Expected output: a `rules` list. Confirm the relevant API group, resource, and verb are all present in the same rule entry. A missing verb, a missing resource, or a wrong API group each independently causes the 403.

### Step 5: Verify the binding namespace matches the request

```bash
kubectl get rolebindings -A -o json | \
  python3 -c "
import json, sys
data = json.load(sys.stdin)
for rb in data['items']:
    for s in rb.get('subjects', []):
        if s.get('name') == '<sa-name>':
            print('ns:', rb['metadata']['namespace'], '-> role:', rb['roleRef']['name'])
"
```

Expected output: the namespace column should match the namespace in the 403 error. A mismatch means the binding exists in the wrong namespace.

### Step 6: Check for a broken aggregated ClusterRole

```bash
kubectl get clusterrole <role-name> -o jsonpath='{.aggregationRule}'
```

Expected output: empty when no aggregation is used. Non-empty output means the ClusterRole inherits rules from child ClusterRoles selected by a label selector.

```bash
kubectl get clusterroles -l <aggregation-label-key>=<aggregation-label-value>
```

Expected output: the list of child ClusterRoles whose rules are merged into the parent. Empty output means no children match the selector, so the parent has no rules.

### Step 7: Check API server audit logs

```bash
# Self-managed cluster (control-plane node):
grep '"code":403' /var/log/kubernetes/audit.log | tail -20 | python3 -m json.tool | grep -E 'user|verb|resource|namespace'
```

```bash
# EKS:
aws logs filter-log-events \
  --log-group-name /aws/eks/<cluster-name>/cluster \
  --start-time "$(date -u -d '30 minutes ago' +%s)000" \
  --filter-pattern '{ $.responseStatus.code = 403 }'
```

Expected output: audit records containing the full user identity, impersonated user (if any), verb, resource, and namespace, confirming whether RBAC or another authorizer (e.g., webhook) rejected the request.

## Causes

### Cause A: Missing RoleBinding or ClusterRoleBinding

**Statement:** The subject has no binding that connects it to a Role or ClusterRole granting the required permission.

**Chain:**
- root: No RoleBinding or ClusterRoleBinding links the subject to any Role or ClusterRole covering the requested verb, resource, and API group.
- s1: RBAC denies by default, so with no binding the subject holds no grant for the request regardless of whether a suitable Role exists.
- D: The API server returns HTTP 403 Forbidden (see Symptom Recognition).

**Indicators:**
- root: [Step 3] No output from both the namespace-scoped and cluster-scoped binding queries.
- s1: [Step 2] `kubectl auth can-i` returns `no`.
  <!-- match: {"step": 2, "predicate": "contains", "target": "no"} -->

**Interventions:**
- **remediation** (root): Create a purpose-built Role and bind the subject to it.

  ```bash
  kubectl create role <role-name> -n <namespace> \
    --verb=<verb1>,<verb2> \
    --resource=<resource>

  kubectl create rolebinding <binding-name> -n <namespace> \
    --role=<role-name> \
    --serviceaccount=<sa-namespace>:<sa-name>
  ```

  **Verification:** re-run Step 2; `kubectl auth can-i <verb> <resource> -n <namespace> --as=system:serviceaccount:<sa-namespace>:<sa-name>` returns `yes`. Rollback: `kubectl delete rolebinding <binding-name> -n <namespace>`.
- **mitigation** (root): Bind the subject to the built-in `view` ClusterRole for read-only diagnostics.

  ```bash
  kubectl create rolebinding <binding-name> \
    --clusterrole=view \
    --serviceaccount=<sa-namespace>:<sa-name> \
    -n <namespace>
  ```

  **Risk:** `view` grants broader read access than a purpose-built Role, but is safe for read-only diagnostics. **Duration:** Remove within 24 hours and replace with a purpose-built Role. **Verification:** re-run Step 2; `kubectl auth can-i` returns `yes`.

### Cause B: Role missing required verb or subresource

**Statement:** A binding exists but the referenced Role or ClusterRole does not include the specific verb or subresource needed by the request.

**Chain:**
- root: The bound Role or ClusterRole has no rule entry pairing the requested verb/subresource with the requested resource and API group in the same rule.
- s1: RBAC matches the triple (API group, resource/subresource, verb) per rule, so any one omission leaves the request ungranted (e.g. `pods` get/list does not grant `pods/log` or `watch`).
- D: The API server returns HTTP 403 Forbidden for that exact combination (see Symptom Recognition).

**Indicators:**
- root: [Step 4] The `rules` section does not contain the required verb, or lists the resource without the required subresource.
- s1: [Step 2] `kubectl auth can-i` returns `no` for the specific verb/subresource.
  <!-- match: {"step": 2, "predicate": "contains", "target": "no"} -->

**Interventions:**
- **remediation** (root): Add the missing verb to the appropriate rule entry, or add a new rule stanza.

  ```bash
  kubectl edit role <role-name> -n <namespace>
  # Add the missing verb to the appropriate rule entry or add a new rule stanza.
  ```

  **Verification:** re-run Step 2; `kubectl auth can-i <verb> <resource> -n <namespace> --as=system:serviceaccount:<sa-namespace>:<sa-name>` returns `yes`.
- **mitigation** (root): Patch the existing Role to append the missing rule.

  ```bash
  kubectl patch role <role-name> -n <namespace> --type=json \
    -p='[{"op":"add","path":"/rules/-","value":{"apiGroups":[""],"resources":["<resource>"],"verbs":["<verb>"]}}]'
  ```

  **Risk:** Patching the existing Role immediately affects all subjects bound to it. **Duration:** Permanent; re-validate in staging before applying to production. **Verification:** re-run Step 2; `kubectl auth can-i` returns `yes`.

### Cause C: RoleBinding exists in the wrong namespace

**Statement:** The subject has a RoleBinding granting the required permission, but that binding is in a different namespace from the one targeted by the request.

**Chain:**
- root: The RoleBinding that grants the permission resides in a namespace other than the one named in the 403 error.
- s1: A RoleBinding is namespace-scoped and does not propagate, so the grant applies only in the binding's own namespace (e.g. `staging` does not reach `production`).
- s2: `kubectl auth can-i` returns `yes` in the source namespace and `no` in the target namespace — an asymmetric denial.
- D: The API server returns HTTP 403 Forbidden in the target namespace (see Symptom Recognition).

**Indicators:**
- root: [Step 5] Binding found, but its namespace does not match the namespace in the 403 error.
  <!-- match: {"step": 5, "predicate": "contains", "target": "ns:"} -->
- s2: [Step 2] `kubectl auth can-i` returns `yes` in one namespace and `no` in another.

**Interventions:**
- **remediation** (root): Create a RoleBinding in the target namespace.

  ```bash
  kubectl create rolebinding <binding-name> \
    --clusterrole=<clusterrole-name> \
    --serviceaccount=<sa-namespace>:<sa-name> \
    -n <target-namespace>
  ```

  **Verification:** re-run Step 2 in the target namespace; `kubectl auth can-i <verb> <resource> -n <target-namespace> --as=system:serviceaccount:<sa-namespace>:<sa-name>` returns `yes`.

### Cause D: Pod is using the default ServiceAccount

**Statement:** The pod was deployed without an explicit `serviceAccountName` and uses the `default` ServiceAccount, which has no RBAC permissions beyond the cluster baseline.

**Chain:**
- root: The Pod spec omits `spec.serviceAccountName`, so Kubernetes assigns the namespace `default` ServiceAccount.
- s1: The `default` ServiceAccount has no bindings (granting it permissions would violate least-privilege), so the pod's API calls carry no grant.
- D: The API server returns HTTP 403 Forbidden for the pod's request (see Symptom Recognition).

**Indicators:**
- root: [Step 1] `kubectl get pod ... -o jsonpath='{.spec.serviceAccountName}'` returns `default` or empty.
  <!-- match: {"step": 1, "predicate": "contains", "target": "default"} -->
- s1: [Step 3] No bindings found for the `default` ServiceAccount that cover the required permission.

**Interventions:**
- **remediation** (root): Create a purpose-built ServiceAccount, grant it a Role, and roll the deployment onto it.

  ```bash
  kubectl create serviceaccount <sa-name> -n <namespace>
  kubectl create role <role-name> -n <namespace> --verb=<verbs> --resource=<resources>
  kubectl create rolebinding <binding-name> -n <namespace> \
    --role=<role-name> --serviceaccount=<namespace>:<sa-name>
  kubectl patch deployment <deployment-name> -n <namespace> \
    -p '{"spec":{"template":{"spec":{"serviceAccountName":"<sa-name>"}}}}'
  ```

  **Verification:** the rollout completes and logs show no forbidden/403 lines:

  ```bash
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  kubectl logs -l app=<app-label> -n <namespace> --tail=50 | grep -i "forbidden\|403"
  ```

### Cause E: Aggregated ClusterRole has no matching child ClusterRoles

**Statement:** The ClusterRole uses an `aggregationRule` label selector that does not match any child ClusterRoles, leaving the parent with an empty rule set.

**Chain:**
- root: No child ClusterRole carries a label matched by the parent's `aggregationRule.clusterRoleSelectors` (label never present or since removed).
- s1: Aggregated ClusterRoles merge rules only from label-matching children, so with no match the parent's merged rule set is empty.
- s2: The empty-ruled parent grants nothing, so the subject bound to it holds no permission.
- D: The API server returns HTTP 403 Forbidden for every request under that ClusterRole (see Symptom Recognition).

**Indicators:**
- root: [Step 6] `kubectl get clusterroles -l <selector>` returns no resources.
  <!-- match: {"step": 6, "predicate": "absent", "target": "items"} -->
- s1: [Step 6] `kubectl get clusterrole ... -o jsonpath='{.aggregationRule}'` returns a non-empty value.

**Interventions:**
- **remediation** (root): Label a child ClusterRole so its rules merge into the aggregated parent.

  ```bash
  kubectl label clusterrole <child-clusterrole-name> <aggregation-label-key>=<aggregation-label-value>
  ```

  **Risk:** Merging the child's rules immediately affects all subjects bound to the parent; verify the merged rules are not broader than intended. **Verification:** the parent shows merged rules and the subject is permitted:

  ```bash
  kubectl get clusterrole <parent-clusterrole-name> -o yaml | grep -A20 rules
  kubectl auth can-i <verb> <resource> --as=system:serviceaccount:<sa-namespace>:<sa-name>
  ```

### Cause F: Impersonation denied

**Statement:** The caller is attempting to impersonate another user, group, or ServiceAccount but does not have the `impersonate` verb on the target resource type.

**Chain:**
- root: The caller's Role or ClusterRole lacks the `impersonate` verb on the `users`, `groups`, or `serviceaccounts` resource being impersonated.
- s1: Impersonation (`--as` / `Impersonate-User` header) requires that explicit grant, so the API server rejects the attempt before evaluating the impersonated identity's permissions.
- D: The API server returns HTTP 403 Forbidden referencing impersonation (see Symptom Recognition).

**Indicators:**
- root: [Step 7] Audit log shows `verb: impersonate` in the denied record.
- s1: [Symptom] Error message references impersonation: `cannot impersonate resource "users"`.
  <!-- match: {"step": 7, "predicate": "contains", "target": "impersonate"} -->

**Interventions:**
- **remediation** (root): Grant the `impersonate` verb to the caller via a ClusterRole and binding.

  ```bash
  kubectl create clusterrole impersonator \
    --verb=impersonate \
    --resource=users,groups,serviceaccounts

  kubectl create clusterrolebinding allow-impersonation \
    --clusterrole=impersonator \
    --user=<admin-user>
  ```

  **Risk:** Granting `impersonate` is high-privilege; restrict to specific identities and resource names where possible, and review scope before applying. **Verification:** `kubectl auth can-i impersonate users --as=<admin-user>` returns `yes` and `kubectl get pods --as=<target-user>` succeeds without 403.

### Cause Z: Unidentified

**Statement:** The 403 cannot be attributed to any known RBAC cause and requires deeper, cluster-level investigation (e.g. a non-RBAC authorizer or admission webhook).

**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot and escalate to the cluster administrator (SME).

  ```bash
  kubectl cluster-info dump | grep authorization-mode
  kubectl logs -n kube-system kube-apiserver-<node-name> | grep -i "denied\|forbidden" | tail -30
  kubectl auth can-i --list --as=<subject>
  ```

  **Risk:** Read-only inspection; verbose API server logging increases log volume but makes no cluster state change. **Duration:** Until the SME identifies and addresses the non-RBAC authorizer rule. **Verification:** the cluster administrator confirms the responsible authorizer and the request no longer returns 403.

## Prevention

1. Define RBAC manifests (Role, RoleBinding, ServiceAccount) in version control alongside the application's Deployment or Helm chart. Apply them together so RBAC permissions are never missing at pod startup.

2. Use `kubectl auth can-i --list --as=<sa>` in CI pipelines after deploying RBAC changes to validate that all required permissions are present before the workload starts.

3. Apply the principle of least privilege: grant only the specific verbs and resources the workload needs. Avoid wildcard verbs (`"*"`) and wildcard resources.

4. Prefer namespace-scoped `Role` and `RoleBinding` over cluster-scoped `ClusterRole` and `ClusterRoleBinding` unless the workload genuinely needs cross-namespace access.

5. Set `automountServiceAccountToken: false` on ServiceAccounts and individual Pods that do not need to call the Kubernetes API. This eliminates the attack surface for token misuse.

6. Alert on sustained 403 rates in API server metrics:

   ```yaml
   # Prometheus alert rule
   - alert: KubernetesRBACForbiddenSpike
     expr: rate(apiserver_request_total{code="403"}[5m]) > 0.5
     for: 2m
     labels:
       severity: warning
     annotations:
       summary: "Kubernetes API 403 rate elevated — possible RBAC misconfiguration"
   ```

7. Run periodic RBAC audits using `rbac-lookup` or `kubectl-who-can` to detect overly permissive bindings and stale ServiceAccount permissions.

## Sources

- [Using RBAC Authorization — Kubernetes Documentation](https://kubernetes.io/docs/reference/access-authn-authz/rbac/) — primary reference for Role, ClusterRole, RoleBinding, ClusterRoleBinding structure and aggregation rules (Priority 1)
- [Authorization Overview — Kubernetes Documentation](https://kubernetes.io/docs/reference/access-authn-authz/authorization/) — authorization modes, request attributes, `kubectl auth can-i` usage, and audit integration (Priority 1)
- [RBAC Good Practices — Kubernetes Documentation](https://kubernetes.io/docs/concepts/security/rbac-good-practices/) — least-privilege guidance, impersonation design, high-risk permissions, and prevention recommendations (Priority 1)
