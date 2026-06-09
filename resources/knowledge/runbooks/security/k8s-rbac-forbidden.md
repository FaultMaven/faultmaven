---
id: "k8s-rbac-forbidden"
title: "Kubernetes RBAC 403 Forbidden"
domain: security
service: kubernetes
symptom_class: [auth_failure]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
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

### Step 1: Identify the subject, verb, resource, and namespace from the error message

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

### Step 3: Find all RoleBindings and ClusterRoleBindings for the subject

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

### Step 4: Inspect the referenced Role or ClusterRole for the missing permission

```bash
kubectl get role <role-name> -n <namespace> -o yaml
```

```bash
kubectl get clusterrole <clusterrole-name> -o yaml
```

Expected output: a `rules` list. Confirm the relevant API group, resource, and verb are all present in the same rule entry. A missing verb, a missing resource, or a wrong API group each independently causes the 403.

### Step 5: Verify the RoleBinding namespace matches the request namespace

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

### Step 6: Check for broken aggregated ClusterRole

```bash
kubectl get clusterrole <role-name> -o jsonpath='{.aggregationRule}'
```

Expected output: empty when no aggregation is used. Non-empty output means the ClusterRole inherits rules from child ClusterRoles selected by a label selector.

```bash
kubectl get clusterroles -l <aggregation-label-key>=<aggregation-label-value>
```

Expected output: the list of child ClusterRoles whose rules are merged into the parent. Empty output means no children match the selector, so the parent has no rules.

### Step 7: Check API server audit logs for the authorization decision

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

**Mechanism:** Kubernetes RBAC denies all access by default. A permission is granted only when a RoleBinding (namespace-scoped) or ClusterRoleBinding (cluster-wide) links the subject to a Role or ClusterRole whose rules cover the requested verb, resource, and API group. Without any such binding the API server returns 403 regardless of whether a suitable Role exists.

**Indicator:**

- [Step 3] No output from both the namespace-scoped and cluster-scoped binding queries
- [Step 2] `kubectl auth can-i` returns `no`

<!-- match: {"step": 2, "predicate": "contains", "target": "no"} -->

**Mitigation:**

- **Risk:** Granting the built-in `view` ClusterRole via a RoleBinding provides broader read access than a purpose-built Role but is safe for read-only diagnostics.
- **Command:**

  ```bash
  kubectl create rolebinding <binding-name> \
    --clusterrole=view \
    --serviceaccount=<sa-namespace>:<sa-name> \
    -n <namespace>
  ```

- **Duration:** Remove within 24 hours and replace with a purpose-built Role.

**Resolution:**

```bash
kubectl create role <role-name> -n <namespace> \
  --verb=<verb1>,<verb2> \
  --resource=<resource>

kubectl create rolebinding <binding-name> -n <namespace> \
  --role=<role-name> \
  --serviceaccount=<sa-namespace>:<sa-name>
```

- **Impact:** Namespace-scoped; affects only the specified namespace.
- **Rollback:** `kubectl delete rolebinding <binding-name> -n <namespace>`

**Verification:**

```bash
kubectl auth can-i <verb> <resource> -n <namespace> \
  --as=system:serviceaccount:<sa-namespace>:<sa-name>
```

Expected output: `yes`.

### Cause B: Role missing required verb or subresource

**Statement:** A binding exists but the referenced Role or ClusterRole does not include the specific verb or subresource needed by the request.

**Mechanism:** Kubernetes matches permissions per rule entry; every element of the triple (API group, resource/subresource, verb) must appear together in the same rule for the permission to be granted. A Role that lists `pods` with `get` and `list` does not automatically grant `pods/log` or `watch`; each omission produces a separate 403 for that exact combination.

**Indicator:**

- [Step 2] `kubectl auth can-i` returns `no` for the specific verb/subresource
- [Step 4] The `rules` section of the Role or ClusterRole does not contain the required verb or lists the resource without the required subresource

<!-- match: {"step": 2, "predicate": "contains", "target": "no"} -->

**Mitigation:**

- **Risk:** Patching the existing Role immediately affects all subjects bound to it.
- **Command:**

  ```bash
  kubectl patch role <role-name> -n <namespace> --type=json \
    -p='[{"op":"add","path":"/rules/-","value":{"apiGroups":[""],"resources":["<resource>"],"verbs":["<verb>"]}}]'
  ```

- **Duration:** Permanent; re-validate in staging before applying to production.

**Resolution:**

```bash
kubectl edit role <role-name> -n <namespace>
# Add the missing verb to the appropriate rule entry or add a new rule stanza.
```

**Verification:**

```bash
kubectl auth can-i <verb> <resource> -n <namespace> \
  --as=system:serviceaccount:<sa-namespace>:<sa-name>
```

Expected output: `yes`.

### Cause C: RoleBinding exists in the wrong namespace

**Statement:** The subject has a RoleBinding granting the required permission, but that binding is in a different namespace from the one targeted by the request.

**Mechanism:** A RoleBinding is namespace-scoped: it grants access only to resources in the namespace where the binding itself resides. A binding in `staging` does not propagate to `production`. This causes an asymmetric 403 where `kubectl auth can-i` returns `yes` in the source namespace and `no` in the target namespace.

**Indicator:**

- [Step 5] Binding found, but its namespace does not match the namespace in the 403 error
- [Step 2] `kubectl auth can-i` returns `yes` in one namespace and `no` in another

<!-- match: {"step": 5, "predicate": "contains", "target": "ns:"} -->

**Mitigation:**

- **Risk:** Creating a new RoleBinding in the target namespace grants access in that namespace only.
- **Command:**

  ```bash
  kubectl create rolebinding <binding-name> \
    --clusterrole=<clusterrole-name> \
    --serviceaccount=<sa-namespace>:<sa-name> \
    -n <target-namespace>
  ```

- **Duration:** Permanent.

**Resolution:** Same as Mitigation.

**Verification:**

```bash
kubectl auth can-i <verb> <resource> -n <target-namespace> \
  --as=system:serviceaccount:<sa-namespace>:<sa-name>
```

Expected output: `yes`.

### Cause D: Pod is using the default ServiceAccount

**Statement:** The pod was deployed without an explicit `serviceAccountName` and is using the `default` ServiceAccount, which has no RBAC permissions beyond the cluster baseline.

**Mechanism:** When `spec.serviceAccountName` is omitted from a Pod spec, Kubernetes assigns the `default` ServiceAccount for that namespace. Unless an administrator has explicitly granted permissions to the `default` ServiceAccount (which violates least-privilege), API calls from the pod are rejected because the `default` SA has no bindings.

**Indicator:**

- [Step 1] `kubectl get pod ... -o jsonpath='{.spec.serviceAccountName}'` returns `default` or empty
- [Step 3] No bindings found for `default` ServiceAccount that cover the required permission

<!-- match: {"step": 1, "predicate": "contains", "target": "default"} -->

**Mitigation:**

- **Risk:** Creating and assigning a purpose-built ServiceAccount requires a pod rollout.
- **Command:**

  ```bash
  kubectl create serviceaccount <sa-name> -n <namespace>
  kubectl create role <role-name> -n <namespace> --verb=<verbs> --resource=<resources>
  kubectl create rolebinding <binding-name> -n <namespace> \
    --role=<role-name> --serviceaccount=<namespace>:<sa-name>
  kubectl patch deployment <deployment-name> -n <namespace> \
    -p '{"spec":{"template":{"spec":{"serviceAccountName":"<sa-name>"}}}}'
  ```

- **Duration:** Permanent; rolling restart required.

**Resolution:** Same as Mitigation.

**Verification:**

```bash
kubectl rollout status deployment/<deployment-name> -n <namespace>
kubectl logs -l app=<app-label> -n <namespace> --tail=50 | grep -i "forbidden\|403"
```

Expected output: rollout complete and no `forbidden`/`403` lines in logs.

### Cause E: Aggregated ClusterRole has no matching child ClusterRoles

**Statement:** The ClusterRole uses an `aggregationRule` label selector that does not match any child ClusterRoles, leaving the parent with an empty rule set.

**Mechanism:** Kubernetes aggregated ClusterRoles dynamically merge rules from all ClusterRoles whose labels match the parent's `aggregationRule.clusterRoleSelectors`. If no child ClusterRole carries the required label, the aggregated ClusterRole has zero rules and denies every request, regardless of whether the label was once present or has since been removed.

**Indicator:**

- [Step 6] `kubectl get clusterrole ... -o jsonpath='{.aggregationRule}'` returns a non-empty value
- [Step 6] `kubectl get clusterroles -l <selector>` returns no resources

<!-- match: {"step": 6, "predicate": "absent", "target": "items"} -->

**Mitigation:**

- **Risk:** Adding a label to a child ClusterRole immediately merges its rules into the aggregated parent, affecting all subjects bound to it.
- **Command:**

  ```bash
  kubectl label clusterrole <child-clusterrole-name> <aggregation-label-key>=<aggregation-label-value>
  ```

- **Duration:** Permanent; verify the merged rules are not broader than intended.

**Resolution:** Same as Mitigation.

**Verification:**

```bash
kubectl get clusterrole <parent-clusterrole-name> -o yaml | grep -A20 rules
kubectl auth can-i <verb> <resource> --as=system:serviceaccount:<sa-namespace>:<sa-name>
```

Expected output: the `rules` section shows merged rules from child ClusterRoles and `auth can-i` returns `yes`.

### Cause F: Impersonation denied

**Statement:** The caller is attempting to impersonate another user, group, or ServiceAccount but does not have the `impersonate` verb on the target resource type.

**Mechanism:** Kubernetes impersonation (`--as` flag or `Impersonate-User` HTTP header) requires an explicit `impersonate` permission in a Role or ClusterRole on the `users`, `groups`, or `serviceaccounts` resource. Without it the API server rejects the impersonation attempt with 403 before evaluating what permissions the impersonated identity would have had.

**Indicator:**

- [Symptom] Error message references impersonation: `cannot impersonate resource "users"`
- [Step 7] Audit log shows `verb: impersonate` in the denied record

**Mitigation:**

- **Risk:** Granting `impersonate` is high-privilege; restrict to specific identities and resource names where possible.
- **Command:**

  ```bash
  kubectl create clusterrole impersonator \
    --verb=impersonate \
    --resource=users,groups,serviceaccounts

  kubectl create clusterrolebinding allow-impersonation \
    --clusterrole=impersonator \
    --user=<admin-user>
  ```

- **Duration:** Permanent; review scope before applying.

**Resolution:** Same as Mitigation.

**Verification:**

```bash
kubectl auth can-i impersonate users --as=<admin-user>
kubectl get pods --as=<target-user>
```

Expected output: first command returns `yes`; second command succeeds without 403.

### Cause Z: Unidentified authorization failure

**Statement:** The 403 error cannot be attributed to a missing binding, wrong namespace, incomplete role, default ServiceAccount, broken aggregation, or impersonation gap.

**Mechanism:** A non-RBAC authorizer (Node, Webhook) may be rejecting the request, or the RBAC configuration involves a complex chain of aggregated roles, group memberships, or admission webhook interactions that require deeper cluster-level investigation beyond standard RBAC queries.

**Indicator:**

- [Default] All standard RBAC checks pass (`auth can-i` returns `yes`) but the request still fails with 403
- [Step 7] Audit log shows a non-RBAC authorizer decision or a webhook deny

**Mitigation:**

- **Risk:** Temporarily enabling verbose API server logging increases log volume.
- **Command:**

  ```bash
  kubectl cluster-info dump | grep authorization-mode
  kubectl logs -n kube-system kube-apiserver-<node-name> | grep -i "denied\|forbidden" | tail -30
  ```

- **Duration:** Log inspection only; no cluster state change.

**Resolution:** Out of runbook scope — escalate to cluster administrator with the full audit log entry and the output of `kubectl auth can-i --list --as=<subject>`.

**Verification:** Confirmed resolution after cluster administrator identifies and addresses the non-RBAC authorizer rule.

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
