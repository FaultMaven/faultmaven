---
id: "argocd-sync-failure"
title: "ArgoCD Application Sync Failure"
domain: application
service: argocd
symptom_class: [deployment_failure]
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
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

**Chain:**
- root: A hook Job (annotated `argocd.argoproj.io/hook`) fails its script or hangs on an unavailable external dependency, exceeding its `backoffLimit`.
- s1: The hook Job transitions to `BackoffLimitExceeded` (or stays `Running` indefinitely), so its sync phase never completes.
- D: ArgoCD marks the sync operation `Failed` and the application does not reach `Synced` (see Symptom Recognition).

**Indicators:**
- root: [Step 3] `kubectl describe job` shows `BackoffLimitExceeded` or pods in `CrashLoopBackOff`
  <!-- match: {"step": 3, "predicate": "contains", "target": "BackoffLimitExceeded"} -->
- s1: [Step 1] `operationState.phase` is `Failed` and `message` references a hook resource name

**Interventions:**
- **remediation** (root): Fix the underlying hook script or add a readiness check. If the hook depends on a service not yet available, add an init container or retry loop within the Job, or restructure the hook to a higher sync wave after its dependency. Update the hook manifest in Git to include `argocd.argoproj.io/hook-delete-policy: HookSucceeded` to prevent stale jobs blocking future syncs.

  ```bash
  kubectl annotate job <HOOK_JOB_NAME> -n <APP_NAMESPACE> \
    argocd.argoproj.io/hook-delete-policy=HookSucceeded
  ```

  **Verification:** Run `argocd app sync <APP_NAME>` and confirm `argocd app get <APP_NAME> | grep "Sync Status"` returns `Synced`. Verify hook jobs complete: `kubectl get jobs -n <APP_NAMESPACE> -l app.kubernetes.io/instance=<APP_NAME> -o custom-columns=NAME:.metadata.name,STATUS:.status.conditions[0].type` shows `Complete`.
- **mitigation** (s1): Delete the stuck hook Job so the sync can re-execute it on the next attempt.

  ```bash
  kubectl delete job <HOOK_JOB_NAME> -n <APP_NAMESPACE>
  argocd app sync <APP_NAME>
  ```

  **Risk:** Deleting the hook job bypasses pre-conditions (migrations, schema changes) or post-conditions (smoke tests). Verify the hook is non-critical or has already run successfully before deleting. **Duration:** One-time; the hook re-executes on the next sync. **Verification:** Re-run Step 3; the new hook Job reaches `Complete` and the sync proceeds.

---

### Cause B: Sync Wave Ordering Misconfiguration

**Statement:** A resource is applied before a prerequisite resource because sync wave annotations are missing or incorrectly ordered.

**Chain:**
- root: Sync-wave annotations assign a dependent resource (e.g. a Deployment, or a CustomResource) an equal or lower wave than the resource it requires (its ConfigMap/Secret, or its CRD).
- s1: ArgoCD applies the dependent resource before its prerequisite exists, so it cannot mount the volume / read the env var, or the API type is not yet registered (`the server could not find the requested resource`).
- D: The wave never becomes healthy and the sync operation fails (see Symptom Recognition).

**Indicators:**
- root: [Step 4] A resource's wave number is higher than (or equal to) a resource that depends on it
- s1: [Step 2] `message` contains `the server could not find the requested resource` for a custom resource kind
  <!-- match: {"step": 2, "predicate": "contains", "target": "the server could not find the requested resource"} -->

**Interventions:**
- **remediation** (root): Update the manifests in Git to assign correct wave annotations — CRDs and Namespaces at wave `-1` or lower, Services and ConfigMaps at wave `0`, Deployments and StatefulSets at wave `1` or higher. Commit the corrected annotations and let ArgoCD sync the updated revision. Rollback: revert the wave annotation commits in Git and trigger a sync.

  ```bash
  # CRDs and Namespaces: wave -1 or lower
  # Services and ConfigMaps: wave 0
  # Deployments and StatefulSets: wave 1 or higher
  grep -rn 'sync-wave' <PATH_TO_APP_MANIFESTS>/ | sort -t'"' -k2 -n
  ```

  **Verification:** Run `argocd app manifests <APP_NAME> | grep -B5 'sync-wave'` and confirm ordering. Trigger a clean sync: `argocd app sync <APP_NAME>` and confirm `Sync Status: Synced`.
- **mitigation** (s1): Manually apply the missing prerequisite (e.g. the CRD) out of band so the dependent resource finds its API type, then re-sync.

  ```bash
  # Move CRD to wave -1 in the manifest, then commit and push to Git
  # Immediate workaround: manually apply the CRD first
  kubectl apply -f <CRD_MANIFEST_FILE>
  argocd app sync <APP_NAME>
  ```

  **Risk:** Changing wave annotations modifies apply order for all syncs; test in a non-production Application first. **Duration:** Until the corrected wave annotations are merged and synced from Git. **Verification:** Re-run Step 2; the `the server could not find the requested resource` error is gone and the resource syncs.

---

### Cause C: Phantom Drift from Server-Side Field Mutations

**Statement:** ArgoCD continuously reports `OutOfSync` because server-side controllers mutate fields that ArgoCD treats as owned by Git, even though no actual configuration drift exists.

**Chain:**
- root: A server-side controller (HPA on `spec.replicas`, Istio injecting sidecars, Kubernetes adding `metadata.managedFields` / defaulting `imagePullPolicy`) writes fields back to the live resource after ArgoCD applies it, and no `ignoreDifferences` rule excludes them.
- s1: On the next reconciliation ArgoCD detects a diff between the Git-desired state and the mutated live state and marks the application `OutOfSync`.
- D: Every reconciliation loop triggers a spurious sync attempt that can never converge (see Symptom Recognition phantom drift).

**Indicators:**
- root: [Step 5] `ignoreDifferences` returns `null` or `[]`
  <!-- match: {"step": 5, "predicate": "absent", "target": "ignoreDifferences"} -->
- s1: [Step 5] `argocd app diff` shows only server-defaulted fields such as `spec.replicas`, `metadata.managedFields`, or injected sidecar containers

**Interventions:**
- **remediation** (root): Define `ignoreDifferences` declaratively in the Application manifest in Git so the rules are version-controlled. For HPA-managed replicas also enable `RespectIgnoreDifferences`. Rollback: remove the `ignoreDifferences` entries from the Application manifest and re-sync.

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

  Then enable the sync option:

  ```bash
  argocd app set <APP_NAME> --sync-option RespectIgnoreDifferences=true
  ```

  **Verification:** Run `argocd app diff <APP_NAME>` — expected: no output (zero diff). Confirm `argocd app get <APP_NAME> | grep "Sync Status"` returns `Synced`.
- **mitigation** (s1): Suppress the noisy field imperatively on the live Application to stop the flapping while the declarative rule is prepared.

  ```bash
  argocd app set <APP_NAME> \
    --ignore-difference group=apps,kind=Deployment,jsonPointers='["/spec/replicas"]'
  argocd app sync <APP_NAME>
  ```

  **Risk:** Suppressing diffs on specific fields means genuine changes to those fields will also be ignored. Scope ignore rules as narrowly as possible using `jsonPointers`. **Duration:** Persists in the Application spec until reconciled with the Git-declared rules. **Verification:** Re-run Step 5; `argocd app diff` no longer reports the server-defaulted field.

---

### Cause D: Resource Version Conflict from Concurrent Controllers

**Statement:** ArgoCD's sync fails with a `resourceVersion` conflict because another controller modified the resource between ArgoCD's read and patch operations.

**Chain:**
- root: A competing controller (HPA, VPA, Istio, or a manual `kubectl edit`) writes to a resource that ArgoCD also manages, between ArgoCD's read of `resourceVersion` and its client-side-apply patch.
- s1: The API server rejects ArgoCD's patch with `metadata.resourceVersion: Invalid value` because the stale `resourceVersion` no longer matches.
- D: The sync operation fails — transiently or persistently depending on how frequently the competing controller writes (see Symptom Recognition).

**Indicators:**
- root: [Step 1] `operationState.phase` is `Failed` and the failing resource is also managed by HPA or another controller
- s1: [Step 2] `message` contains `metadata.resourceVersion: Invalid value`
  <!-- match: {"step": 2, "predicate": "contains", "target": "metadata.resourceVersion: Invalid value"} -->

**Interventions:**
- **remediation** (root): Enable server-side apply so the API server uses field ownership instead of `resourceVersion` for conflict resolution. Rollback: `argocd app set <APP_NAME> --sync-option ServerSideApply=false` and re-sync.

  ```bash
  argocd app set <APP_NAME> --sync-option ServerSideApply=true
  argocd app sync <APP_NAME>
  ```

  Or declare it in the Application spec:

  ```yaml
  spec:
    syncPolicy:
      syncOptions:
        - ServerSideApply=true
  ```

  **Verification:** Trigger sync: `argocd app sync <APP_NAME>`. Confirm `argocd app get <APP_NAME> | grep "Sync Status"` returns `Synced` without `resourceVersion` errors in subsequent syncs over 15 minutes.
- **mitigation** (s1): Force a bounded retry to push the patch through against the racing controller.

  ```bash
  argocd app sync <APP_NAME> --force --retry-limit 3
  ```

  **Risk:** Retrying a failed sync may loop indefinitely if the competing controller writes continuously. Force-sync replaces the resource unconditionally, causing a brief restart. **Duration:** 2–5 minutes; not a permanent fix without enabling server-side apply. **Verification:** Re-run Step 2; the `metadata.resourceVersion: Invalid value` error clears and the resource reaches `Synced`.

---

### Cause E: Manifest Generation Failure (Helm or Kustomize)

**Statement:** ArgoCD cannot render the application manifests because a Helm template error or Kustomize build failure prevents loading the desired state from Git.

**Chain:**
- root: The Git target revision contains a manifest-generation defect — missing/incompatible Helm values, a Kustomize patch targeting a non-existent resource, or an unavailable dependency chart.
- s1: ArgoCD's `helm template` / `kustomize build` render step fails, so the desired state cannot be produced and the controller logs `Failed to load target state` / `ComparisonError`.
- D: ArgoCD cannot diff or apply resources and the sync fails (see Symptom Recognition).

**Indicators:**
- root: [Step 7] `helm template` or `kustomize build` exits non-zero locally
  <!-- match: {"step": 7, "predicate": "exit_code", "target": 1} -->
- s1: [Step 6] Controller logs contain `Failed to load target state` or `ComparisonError`

**Interventions:**
- **remediation** (root): Fix the manifest errors in Git, validate locally, and push a corrected revision. ArgoCD auto-detects the new revision; trigger an explicit sync if auto-sync is disabled. Rollback: revert the broken commit in Git.

  ```bash
  # Validate locally before pushing
  helm template <RELEASE_NAME> <CHART_PATH> --values <VALUES_FILE> --namespace <NAMESPACE>
  kustomize build <KUSTOMIZE_PATH>
  ```

  After pushing the fix:

  ```bash
  argocd app sync <APP_NAME>
  ```

  **Verification:** `argocd app get <APP_NAME> --output json | jq '.status.operationState.phase'` returns `Succeeded`. Controller logs (`kubectl logs -n argocd deployment/argocd-application-controller --tail=50`) show no `ComparisonError`.
- **mitigation** (s1): Roll back the Application to the last known-good revision so a working desired state can render while the broken commit is fixed.

  ```bash
  argocd app history <APP_NAME>
  argocd app rollback <APP_NAME> <LAST_GOOD_HISTORY_ID>
  ```

  **Risk:** Rolling back to a previously working Git revision reverts all changes in the broken commit, including unrelated fixes. **Duration:** Until the broken manifests are fixed in Git. **Verification:** Re-run Step 6; the `Failed to load target state` / `ComparisonError` entries are gone and the sync renders.

---

### Cause F: Git Repository Unreachable or Credentials Expired

**Statement:** ArgoCD cannot fetch manifests because the configured repository credentials are expired, invalid, or the network path to the Git host is broken.

**Chain:**
- root: Repository access breaks — SSH keys rotated, HTTPS tokens expired, or network policy blocks egress to the Git host.
- s1: The repo-server can no longer clone or fetch the repository, and `connectionState` reflects the failure (controller logs show `rpc error` / `context deadline exceeded`).
- D: All Applications sourced from that repository enter a degraded state and cannot sync (see Symptom Recognition).

**Indicators:**
- root: [Step 7] `connectionState.status` is not `Successful`
  <!-- match: {"step": 7, "predicate": "contains", "target": "Failed"} -->
- s1: [Step 6] Controller logs contain `rpc error` or `context deadline exceeded` when referencing the repository

**Interventions:**
- **remediation** (root): Update the repository credentials and verify connectivity. Rollback: re-add old credentials if the new ones are incorrect.

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

  **Verification:** `argocd repo get <REPO_URL> --output json | jq '.connectionState.status'` returns `"Successful"`. Trigger a sync: `argocd app sync <APP_NAME>` completes without repository errors.
- **mitigation** (root): Upsert the rotated credential immediately to restore fetch while the proper secret rotation propagates.

  ```bash
  argocd repo add <REPO_URL> --ssh-private-key-path <KEY_PATH> --upsert
  ```

  **Risk:** Rotating credentials invalidates existing sessions. Brief period of continued failure until the new secret propagates. **Duration:** Immediate upon successful credential update. **Verification:** Re-run Step 7; `connectionState.status` returns to `Successful`.

---

### Cause G: Overlapping Resource Ownership Between Applications

**Statement:** Two ArgoCD Applications manage the same Kubernetes resource, causing label conflicts and repeated sync failures as each Application tries to own the resource.

**Chain:**
- root: Two Applications include the same Kubernetes resource (e.g. a shared ConfigMap or Namespace) in their managed set.
- s1: Each sync overwrites the resource's `app.kubernetes.io/instance` label with its own Application name, so the other Application reports the resource `OutOfSync` on the next reconciliation.
- D: A flip-flop loop forms where neither Application reaches a stable `Synced` state (see Symptom Recognition).

**Indicators:**
- root: [Step 2] Multiple Applications show the same resource in their sync failure list
- s1: [Step 1] `operationState` message references label conflict or `app.kubernetes.io/instance`
  <!-- match: {"step": 1, "predicate": "contains", "target": "app.kubernetes.io/instance"} -->

**Interventions:**
- **remediation** (root): Refactor so each Kubernetes resource is owned by exactly one ArgoCD Application. Move shared resources (Namespaces, CRDs, shared ConfigMaps) to a dedicated `shared-infra` Application and remove them from all but one Application in Git.

  ```bash
  # Verify which Applications claim the resource
  kubectl get <KIND> <NAME> -n <NAMESPACE> \
    -o jsonpath='{.metadata.labels.app\.kubernetes\.io/instance}'
  ```

  **Verification:** `argocd app get <APP_NAME> | grep "Sync Status"` returns `Synced` for all involved Applications. `kubectl get <KIND> <NAME> -n <NAMESPACE> -o jsonpath='{.metadata.labels.app\.kubernetes\.io/instance}'` shows a single Application name.
- **mitigation** (s1): Exclude the shared resource from one Application to break the label flip-flop until ownership is split in Git.

  ```bash
  # Exclude the shared resource from one Application using resource exclusion
  argocd app set <APP_NAME> \
    --resource-exclusion 'group=*,kind=ConfigMap,name=<SHARED_CONFIGMAP>'
  argocd app sync <APP_NAME>
  ```

  **Risk:** Temporarily excluding a resource from one Application leaves it unmanaged; ensure the other Application maintains ownership. **Duration:** Until a permanent ownership split is implemented in Git. **Verification:** Re-run Step 1; the `app.kubernetes.io/instance` label conflict no longer appears and the excluding Application reaches `Synced`.

---

### Cause Z: Unidentified

**Statement:** The ArgoCD sync failure cause cannot be determined from the available diagnostic output produced by Steps 1–7.

**Indicators:**
- [Default] Steps 1–7 did not reveal a matching error pattern. Sync failures can be triggered by cluster-level constraints (API server throttling, admission webhook misconfiguration, RBAC denials) or environmental conditions (network partitions, corrupt etcd entries) that do not surface a distinctive error string.

**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot and escalate to the SME rather than force-syncing blindly.

  ```bash
  # Collect full application state for escalation
  argocd app get <APP_NAME> --output json > /tmp/argocd-app-state.json
  kubectl logs -n argocd deployment/argocd-application-controller --tail=500 \
    > /tmp/argocd-controller.log
  kubectl logs -n argocd deployment/argocd-repo-server --tail=200 \
    > /tmp/argocd-repo-server.log
  ```

  Escalate with the collected diagnostic bundle to the ArgoCD operator or open an issue at [github.com/argoproj/argo-cd/issues](https://github.com/argoproj/argo-cd/issues) with the full application state JSON and controller logs.

  **Risk:** Collecting additional diagnostic data is low-risk. Avoid force-syncing without understanding the root cause. **Duration:** Diagnostic only; no change applied. **Verification:** N/A — escalation required.

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
