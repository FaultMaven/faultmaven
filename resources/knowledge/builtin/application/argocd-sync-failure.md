---
id: argocd-sync-failure
title: "ArgoCD Application Sync Failure"
domain: application
service: argocd
symptom_class:
  - deployment_failure
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-03-26"
verified_by: "kb-researcher"
status: draft
tags:
  - argocd
  - gitops
  - kubernetes
  - sync-failure
  - deployment
  - resource-hooks
  - sync-waves
difficulty: intermediate
---

# ArgoCD Application Sync Failure

## Problem Definition

ArgoCD is a declarative GitOps continuous delivery tool for Kubernetes (v2.6 through v2.14+). Diagnosing sync failures requires `kubectl` access to the cluster running ArgoCD, the `argocd` CLI authenticated against the ArgoCD API server, and read access to the Git repository configured as the application source. Cluster-admin or equivalent RBAC permissions are needed to inspect resource events and hook jobs.

Sync failures manifest when an ArgoCD Application remains in `OutOfSync` or transitions to `SyncFailed` / `Degraded` status. Common symptoms include:

- Application status shows `SyncFailed` with message `one or more synchronization tasks are not valid`
- Sync operation log displays `ComparisonError` or `resource not found` during diff detection
- Resource hooks (PreSync, Sync, PostSync) stuck in `Running` or transitioned to `Failed`
- Sync waves execute out of order or a wave blocks indefinitely waiting for health checks
- UI shows `OutOfSync` despite no visible diff between live state and desired state (phantom drift)
- Error messages such as `the server could not find the requested resource`, `metadata.resourceVersion: Invalid value`, or `failed to load target state: ... unable to unmarshal`
- Repeated `app.kubernetes.io/instance` label conflicts when multiple Applications manage overlapping resources

## Diagnostic Steps

### Step 1. Confirm Application Sync Status and Error

Check the current sync and health status of the failing application. This reveals the top-level sync result and the last operation error.

```bash
argocd app get <APP_NAME> --output json | jq '{syncStatus: .status.sync.status, healthStatus: .status.health.status, operationState: .status.operationState}'
```

Expected output when sync has failed:

```json
{
  "syncStatus": "OutOfSync",
  "healthStatus": "Degraded",
  "operationState": {
    "phase": "Failed",
    "message": "one or more synchronization tasks completed unsuccessfully",
    "syncResult": {
      "resources": [...]
    }
  }
}
```

If `operationState.phase` is `Failed`, the `message` and `syncResult.resources` fields identify which resources failed. If `phase` is `Running` and has not progressed for several minutes, a hook or health check is blocking the operation.

### Step 2. Identify Failing Resources in the Sync Operation

List all resources involved in the last sync operation and their individual results. This pinpoints exactly which resource(s) caused the failure.

```bash
argocd app get <APP_NAME> --output json | jq '.status.operationState.syncResult.resources[] | select(.status != "Synced") | {namespace, name, kind, status, message}'
```

Each entry with `status` of `SyncFailed` or `Pruned` includes a `message` explaining the Kubernetes API error. Common messages include admission webhook denials, immutable field changes, and resource quota violations.

### Step 3. Inspect Resource Hook Execution

Resource hooks (PreSync, Sync, PostSync, SyncFail) run as Jobs or other resources annotated with `argocd.argoproj.io/hook`. A failed hook blocks the entire sync operation. Check hook status:

```bash
kubectl get jobs -n <APP_NAMESPACE> -l app.kubernetes.io/instance=<APP_NAME> --sort-by=.metadata.creationTimestamp
```

Then inspect the most recent hook job:

```bash
kubectl describe job <HOOK_JOB_NAME> -n <APP_NAMESPACE>
kubectl logs job/<HOOK_JOB_NAME> -n <APP_NAMESPACE> --tail=100
```

If the hook job shows `BackoffLimitExceeded` or its pods are in `CrashLoopBackOff`, the hook script itself is failing. If the job is still `Running` beyond its expected duration, it may be hung on an external dependency (database migration, API call).

### Step 4. Verify Sync Wave Ordering

Sync waves control the order in which resources are applied. Resources with lower wave numbers sync first. Misconfigured waves cause dependency failures when a resource is applied before its prerequisite exists.

```bash
argocd app manifests <APP_NAME> --source live 2>/dev/null; argocd app manifests <APP_NAME> | grep -B5 'argocd.argoproj.io/sync-wave'
```

Alternatively, inspect the source manifests directly:

```bash
grep -rn 'sync-wave' <PATH_TO_APP_MANIFESTS>/ | sort -t'"' -k2 -n
```

Verify that CRDs and namespaces are in wave -1 or lower, services and configmaps in wave 0, and deployments/statefulsets in wave 1+. If a Deployment references a ConfigMap that is in a higher wave, the Deployment will fail to mount it.

### Step 5. Diagnose Diff Detection and Phantom Drift

ArgoCD computes diffs between the desired state (Git) and the live state (cluster). Phantom drift occurs when ArgoCD reports `OutOfSync` but no meaningful change exists, often caused by server-side defaulting, kubectl last-applied-annotation differences, or CRD schema normalization.

```bash
argocd app diff <APP_NAME> --local <PATH_TO_LOCAL_MANIFESTS>
```

If the diff shows fields you did not set (for example, `metadata.managedFields`, defaulted container resource requests, or strategy fields), ArgoCD is detecting server-side mutations. Check for known diff customizations:

```bash
argocd app get <APP_NAME> --output json | jq '.spec.ignoreDifferences'
```

If `ignoreDifferences` is null or empty and phantom diffs appear, this confirms the application needs diff normalization configuration.

### Step 6. Check ArgoCD Application Controller Logs

The application controller is responsible for sync operations. Its logs reveal internal errors such as manifest generation failures, Helm template errors, or Kustomize build problems.

```bash
kubectl logs -n argocd deployment/argocd-application-controller --tail=200 | grep -i -E "error|fail|<APP_NAME>"
```

Look for errors like `ComparisonError`, `Failed to load target state`, `rpc error`, or `context deadline exceeded`. A `ComparisonError` typically indicates the desired manifest cannot be parsed or a CRD is missing from the cluster. A `context deadline exceeded` suggests the API server is overloaded or the Git repository is unreachable.

### Step 7. Validate Git Repository Connectivity and Manifest Generation

Sync failures can originate from ArgoCD being unable to fetch or render manifests from the source repository.

```bash
argocd repo get <REPO_URL> --output json | jq '{connectionState: .connectionState}'
```

If `connectionState.status` is not `Successful`, the repository credentials or network path are broken. For Helm or Kustomize sources, test manifest generation independently:

```bash
# For Helm sources
helm template <RELEASE_NAME> <CHART_PATH> --values <VALUES_FILE> --namespace <NAMESPACE>

# For Kustomize sources
kustomize build <KUSTOMIZE_PATH>
```

Template errors here confirm the issue is in the source manifests rather than in ArgoCD itself.

## Mitigation

### Option A. Retry Sync with Force and Prune

Use when the sync failed due to a transient API server error, resource version conflict, or leftover resources that need pruning.

- **Risk**: Force-replacing resources causes brief downtime for affected workloads. Pruning deletes resources present in the cluster but absent from Git.
- **Command**:
  ```bash
  argocd app sync <APP_NAME> --force --prune --retry-limit 3
  ```
- **Verify**:
  ```bash
  argocd app get <APP_NAME> | grep -E "Sync Status|Health Status"
  ```
- **Duration**: 2-5 minutes depending on resource count and hook execution time.

### Option B. Skip Failed Resource Hook

Use when a PreSync or PostSync hook is blocking the sync but the core application resources are correct.

- **Risk**: Skipping hooks means pre-conditions (migrations, schema changes) or post-conditions (smoke tests, notifications) are not enforced. Verify the hook is non-critical before skipping.
- **Command**:
  ```bash
  # Delete the blocking hook job so ArgoCD can proceed
  kubectl delete job <HOOK_JOB_NAME> -n <APP_NAMESPACE>
  # Sync with selective resource targeting, excluding hooks
  argocd app sync <APP_NAME> --resource ':*:*'
  ```
- **Verify**:
  ```bash
  argocd app get <APP_NAME> --output json | jq '.status.sync.status'
  ```
- **Duration**: 1-3 minutes.

### Option C. Roll Back to Last Successful Sync Revision

Use when the new Git revision contains a breaking manifest change and you need to restore service immediately.

- **Risk**: Rolls back all resources to the previous revision. Any data migrations in the new revision are not reversed; database state may be inconsistent if the new revision included schema changes.
- **Command**:
  ```bash
  # List recent sync history
  argocd app history <APP_NAME>
  # Roll back to a specific revision
  argocd app rollback <APP_NAME> <HISTORY_ID>
  ```
- **Verify**:
  ```bash
  argocd app get <APP_NAME> | grep "Sync Status"
  kubectl rollout status deployment/<MAIN_DEPLOYMENT> -n <APP_NAMESPACE>
  ```
- **Duration**: 2-5 minutes.

### Option D. Apply ignoreDifferences to Suppress Phantom Drift

Use when the application reports `OutOfSync` due to server-side field mutations that are not actual configuration drift.

- **Risk**: Suppressing diffs on specific fields means genuine changes to those fields will also be ignored. Scope the ignore rules as narrowly as possible.
- **Command**:
  ```bash
  argocd app set <APP_NAME> --ignore-difference group=apps,kind=Deployment,jsonPointers='["/spec/replicas"]'
  # Or patch the Application resource directly
  kubectl patch application <APP_NAME> -n argocd --type merge -p '{
    "spec": {
      "ignoreDifferences": [
        {
          "group": "apps",
          "kind": "Deployment",
          "jsonPointers": ["/spec/replicas"]
        }
      ]
    }
  }'
  argocd app sync <APP_NAME>
  ```
- **Verify**:
  ```bash
  argocd app diff <APP_NAME>
  ```
- **Duration**: Under 1 minute.

## Root Cause Resolution

**If** the sync failed with `the server could not find the requested resource` for a custom resource, the CRD has not been installed or is in a sync wave equal to or higher than the custom resource itself. Move the CRD to sync-wave `-1` by adding the annotation `argocd.argoproj.io/sync-wave: "-1"` to the CRD manifest and ensure the CR is in wave `0` or higher.

**If** hook jobs fail with `BackoffLimitExceeded` and the logs show connection refused or timeout errors, the hook depends on a service not yet available in the cluster. Restructure hooks: move the dependency to a lower sync wave or add a readiness check (init container or retry loop) within the hook job script.

**If** the diff shows `metadata.resourceVersion: Invalid value` or a conflict on `resourceVersion`, the resource was modified externally (by another controller, HPA, or manual kubectl edit) between ArgoCD reading the live state and applying the patch. Enable server-side apply by setting `syncOptions: ["ServerSideApply=true"]` in the Application spec, which uses field ownership to avoid conflicts:

```bash
argocd app set <APP_NAME> --sync-option ServerSideApply=true
argocd app sync <APP_NAME>
```

**If** phantom diffs appear on fields like `spec.replicas` (due to HPA) or `metadata.managedFields`, configure `ignoreDifferences` for those specific JSON pointers as shown in Mitigation Option D, or enable server-side diff by setting `syncOptions: ["RespectIgnoreDifferences=true"]`.

**If** manifest generation fails (`Failed to load target state`) due to Helm or Kustomize errors, fix the templates in the Git repository. Common causes include missing values files, incompatible Helm chart versions, and Kustomize patches targeting non-existent resources. Test locally with `helm template` or `kustomize build` before pushing.

**If** multiple Applications manage overlapping resources (label conflict on `app.kubernetes.io/instance`), each Application must own a distinct set of resources. Refactor the Applications so that shared resources live in a dedicated Application (for example, `shared-infra`) and other Applications reference them via `argocd.argoproj.io/tracking-id` or exclude them with resource filters.

**If** the Git repository is unreachable (`connectionState` not `Successful`), verify SSH keys or HTTPS credentials in the ArgoCD repo secret. Rotate expired credentials:

```bash
argocd repo add <REPO_URL> --ssh-private-key-path <KEY_PATH> --upsert
```

## Verification

After applying a fix, confirm the application has fully synced and is healthy.

1. Check sync and health status:

```bash
argocd app get <APP_NAME> | grep -E "Sync Status|Health Status|Operation"
```

Expected: `Sync Status: Synced`, `Health Status: Healthy`, and no `Operation` in progress.

2. Verify all resources are synced without drift:

```bash
argocd app diff <APP_NAME>
```

Expected: No output (no differences detected).

3. Confirm workload pods are running:

```bash
kubectl get pods -n <APP_NAMESPACE> -l app.kubernetes.io/instance=<APP_NAME> --field-selector=status.phase!=Running
```

Expected: No pods listed (all pods are in Running phase).

4. If hooks were involved, verify hook jobs completed:

```bash
kubectl get jobs -n <APP_NAMESPACE> -l app.kubernetes.io/instance=<APP_NAME> -o custom-columns=NAME:.metadata.name,STATUS:.status.conditions[0].type
```

Expected: All jobs show `Complete`.

5. Verify the sync was recorded in ArgoCD history:

```bash
argocd app history <APP_NAME> | head -5
```

Expected: Latest entry shows the target revision with status `Succeeded`.

## Prevention

- **Pin CRDs to sync-wave -1 or lower.** All CustomResourceDefinitions and namespace resources should sync before any custom resources or workloads. Enforce this with a CI lint check on ArgoCD annotations.

- **Set hook deletion policies.** Add `argocd.argoproj.io/hook-delete-policy: HookSucceeded` (or `BeforeHookCreation`) to all hook resources. This prevents stale hook jobs from accumulating and blocking subsequent syncs.

- **Use Server-Side Apply for complex applications.** Applications with multiple controllers modifying the same resources (HPA, VPA, Istio injection) benefit from `ServerSideApply=true` to avoid field ownership conflicts.

- **Configure ignoreDifferences declaratively.** Define `ignoreDifferences` in the Application manifest in Git rather than applying it ad-hoc. This ensures diff suppression rules are version-controlled and reviewed.

- **Validate manifests in CI before merge.** Run `helm template`, `kustomize build`, and `kubeval`/`kubeconform` in the CI pipeline to catch template errors, schema violations, and missing values before they reach ArgoCD.

- **Set sync timeouts and retry limits.** Configure `spec.syncPolicy.retry` with a `limit` (for example, 3) and `backoff` to handle transient failures automatically without manual intervention:

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

- **Avoid overlapping Application resource ownership.** Each Kubernetes resource should be managed by exactly one ArgoCD Application. Use resource exclusion filters or tracking annotations to prevent conflicts.

- **Monitor sync status with alerts.** Configure Prometheus alerts on ArgoCD metrics `argocd_app_info{sync_status="OutOfSync"}` and `argocd_app_sync_total{phase="Failed"}` to detect sync failures promptly.

## Sources

- [ArgoCD Sync Operations Documentation](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-options/) -- Official guide covering sync options, server-side apply, and diff customization.
- [ArgoCD Resource Hooks Documentation](https://argo-cd.readthedocs.io/en/stable/user-guide/resource_hooks/) -- Reference for PreSync, Sync, PostSync, and SyncFail hooks with deletion policies.
- [ArgoCD Sync Waves and Phases](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/) -- Explanation of sync wave ordering, health checks between waves, and hook execution.
- [ArgoCD Diffing Customization](https://argo-cd.readthedocs.io/en/stable/user-guide/diffing/) -- Guide for configuring ignoreDifferences, system-level diff settings, and managed-fields exclusion.
- [ArgoCD Troubleshooting Guide](https://argo-cd.readthedocs.io/en/stable/operator-manual/troubleshooting/) -- Operator manual for diagnosing common issues including sync failures, controller errors, and repository connectivity.
- [Kubernetes Server-Side Apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/) -- Kubernetes documentation on field ownership and conflict resolution with server-side apply.
