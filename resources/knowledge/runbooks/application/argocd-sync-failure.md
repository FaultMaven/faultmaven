---
id: "argocd-sync-failure"
title: "ArgoCD Application Sync Failure"
domain: application
service: argocd
symptom_class: [deployment_failure]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
verified_by: "kb-researcher"
status: draft
tags: [argocd, gitops, kubernetes, sync-waves, resource-hooks]
difficulty: intermediate
---

## Symptom Recognition

- Application status shows `SyncFailed` with message `one or more synchronization tasks are not valid` or `one or more synchronization tasks completed unsuccessfully`
- ArgoCD UI or CLI reports `OutOfSync` state that persists after manual sync attempts
- Sync operation log displays `ComparisonError` or `resource not found` during diff detection
- Resource hooks (PreSync, Sync, PostSync) stuck in `Running` or transitioned to `Failed`
- A sync wave blocks indefinitely waiting for a health check that never passes
- Error messages include `the server could not find the requested resource`, `metadata.resourceVersion: Invalid value`, or `failed to load target state: ... unable to unmarshal`
- Application reports `OutOfSync` despite no visible diff between live state and desired state (phantom drift)
- Repeated `app.kubernetes.io/instance` label conflicts when multiple Applications manage overlapping resources

## Applicability

- ArgoCD v2.6 through v2.14+
- Requires `argocd` CLI authenticated against the ArgoCD API server
- Requires `kubectl` access to the cluster running ArgoCD (read access to application namespace; cluster-admin or equivalent RBAC to inspect events and hook jobs)
- Read access to the Git repository configured as the application source
- Tools: `argocd`, `kubectl`, `jq`; optionally `helm` or `kustomize` for manifest generation validation

## Diagnostic Steps

### Step 1: Confirm application sync status and operation error

Confirm current sync and health status of the failing application. This reveals the top-level sync result and last operation error.

```bash
argocd app get <APP_NAME> --output json | jq '{syncStatus: .status.sync.status, healthStatus: .status.health.status, operationState: .status.operationState}'
```

Expected output: `operationState.phase` is `Failed` with a `message` field identifying the failure. If `phase` is `Running` and has not progressed for several minutes, a hook or health check is blocking.

### Step 2: Identify failing resources in the sync operation

List all resources in the last sync operation with non-Synced status. This pinpoints which specific resource(s) caused the failure.

```bash
argocd app get <APP_NAME> --output json | jq '.status.operationState.syncResult.resources[] | select(.status != "Synced") | {namespace, name, kind, status, message}'
```

Expected output: Each entry with `status` of `SyncFailed` includes a `message` with the Kubernetes API error — admission webhook denial, immutable field change, or resource quota violation.

### Step 3: Inspect resource hook execution

Inspect resource hook execution. Hooks run as Jobs annotated with `argocd.argoproj.io/hook`; a failed hook blocks the entire sync.

```bash
kubectl get jobs -n <APP_NAMESPACE> -l app.kubernetes.io/instance=<APP_NAME> --sort-by=.metadata.creationTimestamp
```

Then inspect the most recent hook job:

```bash
kubectl describe job <HOOK_JOB_NAME> -n <APP_NAMESPACE>
kubectl logs job/<HOOK_JOB_NAME> -n <APP_NAMESPACE> --tail=100
```

Expected output: Job shows `Complete` if successful. `BackoffLimitExceeded` or pods in `CrashLoopBackOff` confirm the hook script is failing. A job still `Running` beyond expected duration indicates a hung external dependency.

### Step 4: Verify sync wave ordering

Verify sync wave ordering in the application manifests.

```bash
argocd app manifests <APP_NAME> | grep -B5 'argocd.argoproj.io/sync-wave'
```

Or inspect source manifests directly:

```bash
grep -rn 'sync-wave' <PATH_TO_APP_MANIFESTS>/ | sort -t'"' -k2 -n
```

Expected output: CRDs and namespaces at wave `-1` or lower; services and configmaps at wave `0`; deployments and statefulsets at wave `1` or higher. A Deployment in a lower wave than its ConfigMap confirms ordering misconfiguration.

### Step 5: Diagnose diff detection and phantom drift

Diagnose diff detection and phantom drift. Phantom drift occurs when ArgoCD reports `OutOfSync` but no meaningful change exists.

```bash
argocd app diff <APP_NAME>
```

Then check existing ignoreDifferences configuration:

```bash
argocd app get <APP_NAME> --output json | jq '.spec.ignoreDifferences'
```

Expected output: `argocd app diff` showing only server-side defaulted fields (e.g., `metadata.managedFields`, `spec.replicas` managed by HPA) with `ignoreDifferences` returning `null` or `[]` confirms phantom drift from missing diff normalization.

### Step 6: Check ArgoCD application controller logs

Check ArgoCD application controller logs for internal errors such as manifest generation failures, Helm template errors, or Kustomize build problems.

```bash
kubectl logs -n argocd deployment/argocd-application-controller --tail=200 | grep -i -E "error|fail|<APP_NAME>"
```

Expected output: `ComparisonError` indicates the desired manifest cannot be parsed or a CRD is missing. `context deadline exceeded` suggests the API server is overloaded or the Git repository is unreachable. `Failed to load target state` points to Helm/Kustomize generation failure.

### Step 7: Validate Git repository connectivity and manifest generation

Validate Git repository connectivity and manifest generation.

```bash
argocd repo get <REPO_URL> --output json | jq '{connectionState: .connectionState}'
```

For Helm or Kustomize sources, test manifest generation independently:

```bash
helm template <RELEASE_NAME> <CHART_PATH> --values <VALUES_FILE> --namespace <NAMESPACE>
```

```bash
kustomize build <KUSTOMIZE_PATH>
```

Expected output: `connectionState.status` of `Successful` confirms repo access. Any non-zero exit from `helm template` or `kustomize build` confirms the issue is in source manifests rather than ArgoCD itself.

## Causes

### Cause A: Resource Hook Failure

**Statement:** A PreSync, Sync, or PostSync hook Job failed or timed out, blocking the entire sync operation from completing.

**Mechanism:** ArgoCD executes hook resources (Jobs annotated with `argocd.argoproj.io/hook`) as part of the sync lifecycle. If a hook Job exceeds its `backoffLimit`, the Job transitions to `BackoffLimitExceeded` and ArgoCD marks the sync operation as Failed. Hooks with external dependencies (database migrations, API calls) can also hang indefinitely if the dependency is unavailable.

**Indicator:**

- [Step 3] `kubectl describe job` shows `BackoffLimitExceeded` or pods in `CrashLoopBackOff`
- [Step 1] `operationState.phase` is `Failed` and `message` references a hook resource name

<!-- match: {"step": 3, "predicate": "contains", "target": "BackoffLimitExceeded"} -->

**Mitigation:**

- **Risk:** Deleting the hook job bypasses pre-conditions (migrations, schema changes) or post-conditions (smoke tests). Verify the hook is non-critical or has already run successfully before deleting.

- **Command:**

  ```bash
  kubectl delete job <HOOK_JOB_NAME> -n <APP_NAMESPACE>
  argocd app sync <APP_NAME>
  ```

- **Duration:** One-time; the hook re-executes on the next sync.

**Resolution:**

Fix the underlying hook script or add a readiness check. If the hook depends on a service not yet available, add an init container or retry loop within the Job, or restructure the hook to a higher sync wave after its dependency:

```bash
# Add hook-delete-policy to prevent stale jobs blocking future syncs
kubectl annotate job <HOOK_JOB_NAME> -n <APP_NAMESPACE> \
  argocd.argoproj.io/hook-delete-policy=HookSucceeded
```

Update the hook manifest in Git to include `argocd.argoproj.io/hook-delete-policy: HookSucceeded`.

**Verification:** Run `argocd app sync <APP_NAME>` and confirm `argocd app get <APP_NAME> | grep "Sync Status"` returns `Synced`. Verify hook jobs complete: `kubectl get jobs -n <APP_NAMESPACE> -l app.kubernetes.io/instance=<APP_NAME> -o custom-columns=NAME:.metadata.name,STATUS:.status.conditions[0].type` shows `Complete`.

---

### Cause B: Sync Wave Ordering Misconfiguration

**Statement:** A resource is applied before a prerequisite resource because sync wave annotations are missing or incorrectly ordered.

**Mechanism:** ArgoCD applies resources in ascending sync-wave order and waits for each wave to become healthy before proceeding. When a Deployment references a ConfigMap or Secret in a higher wave, the Deployment fails to mount the volume or read the environment variable. Similarly, a CustomResource applied before its CRD is installed causes an `the server could not find the requested resource` error because the API type does not yet exist in the cluster.

**Indicator:**

- [Step 4] A resource's wave number is higher than a resource that depends on it
- [Step 2] `message` contains `the server could not find the requested resource` for a custom resource kind

<!-- match: {"step": 2, "predicate": "contains", "target": "the server could not find the requested resource"} -->

**Mitigation:**

- **Risk:** Changing wave annotations modifies apply order for all syncs; test in a non-production Application first.

- **Command:**

  ```bash
  # Move CRD to wave -1 in the manifest, then commit and push to Git
  # Immediate workaround: manually apply the CRD first
  kubectl apply -f <CRD_MANIFEST_FILE>
  argocd app sync <APP_NAME>
  ```

- **Duration:** Until the corrected wave annotations are merged and synced from Git.

**Resolution:**

Update the manifests in Git to assign correct wave annotations:

```bash
# CRDs and Namespaces: wave -1 or lower
# Services and ConfigMaps: wave 0
# Deployments and StatefulSets: wave 1 or higher
grep -rn 'sync-wave' <PATH_TO_APP_MANIFESTS>/ | sort -t'"' -k2 -n
```

Commit the corrected annotations and let ArgoCD sync the updated revision.

- **Impact:** Cluster-wide for this Application; affects all future syncs.
- **Rollback:** Revert the wave annotation commits in Git and trigger a sync.

**Verification:** Run `argocd app manifests <APP_NAME> | grep -B5 'sync-wave'` and confirm ordering. Trigger a clean sync: `argocd app sync <APP_NAME>` and confirm `Sync Status: Synced`.

---

### Cause C: Phantom Drift from Server-Side Field Mutations

**Statement:** ArgoCD continuously reports `OutOfSync` because server-side controllers mutate fields that ArgoCD treats as owned by Git, even though no actual configuration drift exists.

**Mechanism:** Controllers such as HPA (modifying `spec.replicas`), Istio (injecting sidecar containers), and Kubernetes itself (adding `metadata.managedFields`, defaulting `imagePullPolicy`) write fields back to resources after ArgoCD applies them. On the next reconciliation ArgoCD detects a diff between the Git-desired state and the live state, incorrectly marking the application `OutOfSync`. Without `ignoreDifferences` configured, every reconciliation loop triggers a spurious sync attempt.

**Indicator:**

- [Step 5] `argocd app diff` shows only server-defaulted fields such as `spec.replicas`, `metadata.managedFields`, or injected sidecar containers
- [Step 5] `ignoreDifferences` returns `null` or `[]`

<!-- match: {"step": 5, "predicate": "absent", "target": "ignoreDifferences"} -->

**Mitigation:**

- **Risk:** Suppressing diffs on specific fields means genuine changes to those fields will also be ignored. Scope ignore rules as narrowly as possible using `jsonPointers`.

- **Command:**

  ```bash
  argocd app set <APP_NAME> \
    --ignore-difference group=apps,kind=Deployment,jsonPointers='["/spec/replicas"]'
  argocd app sync <APP_NAME>
  ```

- **Duration:** Permanent; configuration persists in the Application spec.

**Resolution:**

Define `ignoreDifferences` declaratively in the Application manifest in Git so the rules are version-controlled:

```yaml
spec:
  ignoreDifferences:
    - group: apps
      kind: Deployment
      jsonPointers:
        - /spec/replicas
    - group: ""
      kind: ""
      managedFieldsManagers:
        - kube-controller-manager
        - istio-sidecar-injector
```

For HPA-managed replicas also enable `RespectIgnoreDifferences`:

```bash
argocd app set <APP_NAME> --sync-option RespectIgnoreDifferences=true
```

- **Impact:** Application-scoped; affects diff computation for this Application only.
- **Rollback:** Remove the `ignoreDifferences` entries from the Application manifest and re-sync.

**Verification:** Run `argocd app diff <APP_NAME>` — expected: no output (zero diff). Confirm `argocd app get <APP_NAME> | grep "Sync Status"` returns `Synced`.

---

### Cause D: Resource Version Conflict from Concurrent Controllers

**Statement:** ArgoCD's sync fails with a `resourceVersion` conflict because another controller modified the resource between ArgoCD's read and patch operations.

**Mechanism:** ArgoCD uses client-side apply, which reads the current `resourceVersion` and includes it in the patch. If HPA, VPA, Istio, or a manual `kubectl edit` writes to the resource between ArgoCD's read and patch, the API server rejects the patch with `metadata.resourceVersion: Invalid value`. This creates a race condition that causes transient or persistent sync failures depending on how frequently the competing controller writes.

**Indicator:**

- [Step 2] `message` contains `metadata.resourceVersion: Invalid value`
- [Step 1] `operationState.phase` is `Failed` and the failing resource is also managed by HPA or another controller

<!-- match: {"step": 2, "predicate": "contains", "target": "metadata.resourceVersion: Invalid value"} -->

**Mitigation:**

- **Risk:** Retrying a failed sync may loop indefinitely if the competing controller writes continuously. Force-sync replaces the resource unconditionally, causing a brief restart.

- **Command:**

  ```bash
  argocd app sync <APP_NAME> --force --retry-limit 3
  ```

- **Duration:** 2–5 minutes; not a permanent fix without enabling server-side apply.

**Resolution:**

Enable server-side apply so the API server uses field ownership instead of `resourceVersion` for conflict resolution:

```bash
argocd app set <APP_NAME> --sync-option ServerSideApply=true
argocd app sync <APP_NAME>
```

Or patch the Application resource directly:

```yaml
spec:
  syncPolicy:
    syncOptions:
      - ServerSideApply=true
```

- **Impact:** Cluster-wide for all resources in this Application; changes apply semantics from client-side to server-side.
- **Rollback:** `argocd app set <APP_NAME> --sync-option ServerSideApply=false` and re-sync.

**Verification:** Trigger sync: `argocd app sync <APP_NAME>`. Confirm `argocd app get <APP_NAME> | grep "Sync Status"` returns `Synced` without `resourceVersion` errors in subsequent syncs over 15 minutes.

---

### Cause E: Manifest Generation Failure (Helm or Kustomize)

**Statement:** ArgoCD cannot render the application manifests because a Helm template error or Kustomize build failure prevents loading the desired state from Git.

**Mechanism:** When ArgoCD fetches the target revision from Git, it runs `helm template` or `kustomize build` to produce the desired Kubernetes manifests. If the chart has missing or incompatible values files, a broken Kustomize patch targeting a non-existent resource, or a dependency chart that is unavailable, the render step fails. ArgoCD logs `Failed to load target state` and cannot proceed to diff or apply resources.

**Indicator:**

- [Step 6] Controller logs contain `Failed to load target state` or `ComparisonError`
- [Step 7] `helm template` or `kustomize build` exits non-zero locally

<!-- match: {"step": 7, "predicate": "exit_code", "target": 1} -->

**Mitigation:**

- **Risk:** Rolling back to a previously working Git revision reverts all changes in the broken commit, including unrelated fixes.

- **Command:**

  ```bash
  argocd app history <APP_NAME>
  argocd app rollback <APP_NAME> <LAST_GOOD_HISTORY_ID>
  ```

- **Duration:** Until the broken manifests are fixed in Git.

**Resolution:**

Fix the manifest errors in Git and push a corrected revision:

```bash
# Validate locally before pushing
helm template <RELEASE_NAME> <CHART_PATH> --values <VALUES_FILE> --namespace <NAMESPACE>
kustomize build <KUSTOMIZE_PATH>
```

After pushing the fix, ArgoCD auto-detects the new revision. Trigger an explicit sync if auto-sync is disabled:

```bash
argocd app sync <APP_NAME>
```

- **Impact:** Git repository change; affects all environments sourcing the same path.
- **Rollback:** Revert the broken commit in Git.

**Verification:** `argocd app get <APP_NAME> --output json | jq '.status.operationState.phase'` returns `Succeeded`. Controller logs (`kubectl logs -n argocd deployment/argocd-application-controller --tail=50`) show no `ComparisonError`.

---

### Cause F: Git Repository Unreachable or Credentials Expired

**Statement:** ArgoCD cannot fetch manifests because the configured repository credentials are expired, invalid, or the network path to the Git host is broken.

**Mechanism:** ArgoCD's repo-server continuously polls the configured Git repository. When SSH keys are rotated, HTTPS tokens expire, or network policy blocks egress to the Git host, the repo-server can no longer clone or fetch the repository. All Applications sourced from that repository enter a degraded state and cannot sync until connectivity is restored. The `connectionState` in the repository status reflects the failure.

**Indicator:**

- [Step 7] `connectionState.status` is not `Successful`
- [Step 6] Controller logs contain `rpc error` or `context deadline exceeded` when referencing the repository

<!-- match: {"step": 7, "predicate": "contains", "target": "Failed"} -->

**Mitigation:**

- **Risk:** Rotating credentials invalidates existing sessions. Brief period of continued failure until the new secret propagates.

- **Command:**

  ```bash
  argocd repo add <REPO_URL> --ssh-private-key-path <KEY_PATH> --upsert
  ```

- **Duration:** Immediate upon successful credential update.

**Resolution:**

Update the repository credentials and verify connectivity:

```bash
# For SSH repos
argocd repo add git@github.com:<ORG>/<REPO>.git \
  --ssh-private-key-path ~/.ssh/argocd_deploy_key --upsert

# For HTTPS repos
argocd repo add https://github.com/<ORG>/<REPO>.git \
  --username <USERNAME> --password <TOKEN> --upsert

# Verify connection
argocd repo get <REPO_URL> --output json | jq '.connectionState'
```

- **Impact:** All Applications sourcing from this repository; resolves immediately after credential update.
- **Rollback:** Re-add old credentials if the new ones are incorrect.

**Verification:** `argocd repo get <REPO_URL> --output json | jq '.connectionState.status'` returns `"Successful"`. Trigger a sync: `argocd app sync <APP_NAME>` completes without repository errors.

---

### Cause G: Overlapping Resource Ownership Between Applications

**Statement:** Two ArgoCD Applications manage the same Kubernetes resource, causing label conflicts and repeated sync failures as each Application tries to own the resource.

**Mechanism:** ArgoCD sets the `app.kubernetes.io/instance` label on every resource it manages. When two Applications include the same resource (e.g., a shared ConfigMap or Namespace), the second sync overwrites the label with its own Application name, causing the first Application to report the resource as `OutOfSync` on the next reconciliation. This creates a flip-flop loop where neither Application can reach a stable `Synced` state.

**Indicator:**

- [Step 2] Multiple Applications show the same resource in their sync failure list
- [Step 1] `operationState` message references label conflict or `app.kubernetes.io/instance`

<!-- match: {"step": 2, "predicate": "contains", "target": "app.kubernetes.io/instance"} -->

**Mitigation:**

- **Risk:** Temporarily excluding a resource from one Application leaves it unmanaged; ensure the other Application maintains ownership.

- **Command:**

  ```bash
  # Exclude the shared resource from one Application using resource exclusion
  argocd app set <APP_NAME> \
    --resource-exclusion 'group=*,kind=ConfigMap,name=<SHARED_CONFIGMAP>'
  argocd app sync <APP_NAME>
  ```

- **Duration:** Until a permanent ownership split is implemented in Git.

**Resolution:**

Refactor so each Kubernetes resource is owned by exactly one ArgoCD Application. Move shared resources (Namespaces, CRDs, shared ConfigMaps) to a dedicated `shared-infra` Application:

```bash
# Verify which Applications claim the resource
kubectl get <KIND> <NAME> -n <NAMESPACE> \
  -o jsonpath='{.metadata.labels.app\.kubernetes\.io/instance}'
```

Update the Git repositories to remove the shared resource from all but one Application.

**Verification:** `argocd app get <APP_NAME> | grep "Sync Status"` returns `Synced` for all involved Applications. `kubectl get <KIND> <NAME> -n <NAMESPACE> -o jsonpath='{.metadata.labels.app\.kubernetes\.io/instance}'` shows a single Application name.

---

### Cause Z: Unidentified Sync Failure

**Statement:** The ArgoCD sync failure cause cannot be determined from the available diagnostic output produced by Steps 1–7.

**Mechanism:** Sync failures can be triggered by combinations of cluster-level constraints (API server throttling, admission webhook misconfiguration, RBAC denials) or environmental conditions (network partitions, corrupt etcd entries) that do not surface a distinctive error string. Without a clear signal from the operation state, resource sync result, hook logs, controller logs, or repository connection state, applying any Cause A–G fix risks masking the true driver.

**Indicator:**

- [Default] Steps 1–7 did not reveal a matching error pattern

**Mitigation:**

- **Risk:** Collecting additional diagnostic data is low-risk. Avoid force-syncing without understanding the root cause.

- **Command:**

  ```bash
  # Collect full application state for escalation
  argocd app get <APP_NAME> --output json > /tmp/argocd-app-state.json
  kubectl logs -n argocd deployment/argocd-application-controller --tail=500 \
    > /tmp/argocd-controller.log
  kubectl logs -n argocd deployment/argocd-repo-server --tail=200 \
    > /tmp/argocd-repo-server.log
  ```

- **Duration:** Diagnostic only; no change applied.

**Resolution:** Out of runbook scope. Escalate with the collected diagnostic bundle to the ArgoCD operator or open an issue at [github.com/argoproj/argo-cd/issues](https://github.com/argoproj/argo-cd/issues) with the full application state JSON and controller logs.

**Verification:** N/A — escalation required.

## Prevention

- **Pin CRDs to sync-wave -1 or lower.** All CustomResourceDefinitions and Namespace resources must sync before any resources that depend on them. Enforce with a CI lint check on ArgoCD annotations.

- **Set hook deletion policies.** Add `argocd.argoproj.io/hook-delete-policy: HookSucceeded` (or `BeforeHookCreation`) to all hook resources to prevent stale jobs from accumulating and blocking subsequent syncs.

- **Enable Server-Side Apply for multi-controller applications.** Applications with HPA, VPA, or Istio injection benefit from `ServerSideApply=true` to avoid `resourceVersion` conflicts from field ownership contention.

- **Define ignoreDifferences declaratively in Git.** Version-control diff suppression rules rather than applying them ad-hoc so they are reviewed and audited.

- **Validate manifests in CI before merge.** Run `helm template`, `kustomize build`, and `kubeconform` or `kubeval` in the CI pipeline to catch template errors and schema violations before they reach ArgoCD.

- **Configure sync retries and timeouts.** Set `spec.syncPolicy.retry` with a `limit` and exponential `backoff` to handle transient failures automatically:

  ```yaml
  spec:
    syncPolicy:
      retry:
        limit: 3
        backoff:
          duration: 10s
          factor: 2
          maxDuration: 3m
  ```

- **Enforce single resource ownership.** Each Kubernetes resource must be managed by exactly one ArgoCD Application. Use resource exclusion filters or tracking annotations to prevent ownership conflicts.

- **Monitor sync status with alerts.** Configure Prometheus alerts on `argocd_app_info{sync_status="OutOfSync"}` and `argocd_app_sync_total{phase="Failed"}` to detect failures promptly.

## Sources

- [ArgoCD Operator Manual — Troubleshooting](https://argo-cd.readthedocs.io/en/stable/operator-manual/troubleshooting/) — Priority 2 (official OSS docs). Operator-level diagnosis of sync failures, controller errors, and repository connectivity.
- [ArgoCD User Guide — Sync Options](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/) — Priority 1 (official docs). ServerSideApply, CreateNamespace, PrunePropagationPolicy, RespectIgnoreDifferences, and other sync option effects.
- [ArgoCD User Guide — Resource Hooks](https://argo-cd.readthedocs.io/en/stable/user-guide/resource_hooks/) — Priority 1 (official docs). Hook types, lifecycle, deletion policies, and failure handling.
- [ArgoCD User Guide — Sync Waves and Phases](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/) — Priority 1 (official docs). Wave ordering, health checks between waves, and hook execution sequence.
- [ArgoCD User Guide — Diffing Customization](https://argo-cd.readthedocs.io/en/stable/user-guide/diffing/) — Priority 1 (official docs). ignoreDifferences configuration, system-level diff settings, and managedFields exclusion.
- [Kubernetes Server-Side Apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/) — Priority 1 (official docs). Field ownership semantics and conflict resolution used by ArgoCD ServerSideApply sync option.
