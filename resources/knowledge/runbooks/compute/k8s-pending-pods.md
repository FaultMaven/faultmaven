---
id: k8s-pending-pods
title: "Kubernetes Pods Stuck in Pending State"
domain: compute
service: kubernetes
symptom_class:
  - scheduling_failure
severity: high
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: kb-researcher
status: draft
tags:
  - kubernetes
  - scheduler
  - pending
  - scheduling
  - taints
  - affinity
  - pvc
  - quota
difficulty: intermediate
---

# Kubernetes Pods Stuck in Pending State

## Symptom Recognition

- `kubectl get pods` reports `STATUS: Pending` and `READY: 0/N` for the affected pod, with `AGE` increasing while the pod never transitions to `ContainerCreating` or `Running` (e.g. `my-app-abc123-xyz   0/1   Pending   0   15m`).
- `kubectl describe pod` shows a `FailedScheduling` event from `default-scheduler` (or the named scheduler in use) under the `Events` table, with a `Message` beginning `0/N nodes are available:` followed by a comma-separated list of predicate failures.
- Common scheduler message tails include `Insufficient cpu`, `Insufficient memory`, `Insufficient ephemeral-storage`, `node(s) didn't match Pod's node affinity/selector`, `node(s) didn't match Pod's nodeSelector`, `node(s) had untolerated taint`, `node(s) had taints that the pod didn't tolerate`, `pod has unbound immediate PersistentVolumeClaims`, `node(s) exceed max volume count`, `node(s) didn't satisfy existing pods anti-affinity rules`, `node(s) didn't match pod topology spread constraints`, and `node(s) didn't have free ports for the requested pod ports`.
- For ResourceQuota rejection the pod is never created in the first place: the controller (Deployment/ReplicaSet/StatefulSet) records a `FailedCreate` event with message `pods "<name>" is forbidden: exceeded quota: <quota-name>, requested: ..., used: ..., limited: ...`, and `kubectl get pods -n <namespace>` shows fewer replicas than desired with no Pending pod at all.
- For scheduling gates the pod status is the literal string `SchedulingGated`, not `Pending`, and the scheduler emits no `FailedScheduling` event because the pod has not yet entered the scheduling queue.
- The pod's `.status.conditions[?(@.type=="PodScheduled")].status` is `False` with `reason: Unschedulable` for genuine scheduler failures, or `reason: SchedulingGated` for gated pods.

## Applicability

- Kubernetes 1.24 or newer on any distribution (vanilla, EKS, GKE, AKS, OpenShift, k3s). Scheduling-gate diagnostics require 1.26+ (the feature is GA from 1.30).
- Requires `kubectl` access with `get`, `list`, `describe` verbs on `pods`, `nodes`, `persistentvolumeclaims`, `persistentvolumes`, `storageclasses`, `resourcequotas`, `limitranges`, and `events` in the target namespace, plus cluster-wide `get` on `nodes` and `storageclasses`.
- `kubectl top nodes` and `kubectl top pods` require the `metrics-server` add-on to be installed and healthy in `kube-system`.
- Inspection of the scheduler itself (`kubectl logs -n kube-system -l component=kube-scheduler`) requires access to the `kube-system` namespace and assumes a self-hosted scheduler; on managed services (EKS, GKE, AKS) the scheduler is hidden and only its events are visible.
- Modifying node labels, taints, or cordoning requires `update` on `nodes` (typically a cluster-admin or platform-operator role).

## Diagnostic Steps

### Step 1: Confirm pod is Pending and capture age

```bash
kubectl get pod <pod-name> -n <namespace> -o wide
```

Expected output: a row showing `STATUS: Pending` (or `SchedulingGated`) with `READY: 0/N`, no `NODE` assigned (`<none>`), and an `AGE` value. A pod Pending for more than 1-2 minutes is not transient — it has a structural scheduling constraint.

### Step 2: Read FailedScheduling event message verbatim

```bash
kubectl describe pod <pod-name> -n <namespace>
```

Expected output: an `Events` table at the bottom of the description containing one or more `Warning  FailedScheduling  default-scheduler  ...` lines. The Message field is the scheduler's verdict and is the single most important diagnostic: it begins `0/N nodes are available:` and lists every failing predicate (e.g. `0/5 nodes are available: 2 Insufficient cpu, 3 node(s) had taints that the pod didn't tolerate`). Record the full message string.

### Step 3: Capture the scheduling condition in structured form

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .status.conditions[*]}{.type}{"  "}{.status}{"  "}{.reason}{"  "}{.message}{"\n"}{end}'
```

Expected output: one line per condition. The `PodScheduled  False  Unschedulable  <message>` line confirms the scheduler rejected the pod and repeats the predicate-failure list machine-readably. `PodScheduled  False  SchedulingGated` indicates `.spec.schedulingGates` is non-empty.

### Step 4: List node allocatable vs requested resources

```bash
kubectl describe nodes | grep -E "^Name:|Allocated resources|cpu  |memory  " -A 1
kubectl top nodes
```

Expected output: per-node `Allocated resources` block showing `Requests` and `Limits` totals as both absolute values and percentages of allocatable. `kubectl top nodes` shows actual live usage. A node with `cpu  Requests: 95%` or `memory  Requests: 100%` cannot fit additional CPU/memory requests; live usage from `kubectl top` is informational only — the scheduler decides on requests, not usage.

### Step 5: List node taints

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints,READY:.status.conditions[?\(@.type==\"Ready\"\)].status
```

Expected output: each node's name, its taints array (key/value/effect tuples or `<none>`), and Ready status. Well-known taints to recognize: `node.kubernetes.io/not-ready:NoSchedule`, `node.kubernetes.io/unschedulable:NoSchedule` (cordoned), `node.kubernetes.io/disk-pressure:NoSchedule`, `node.kubernetes.io/memory-pressure:NoSchedule`, `node.kubernetes.io/pid-pressure:NoSchedule`, `node-role.kubernetes.io/control-plane:NoSchedule`.

### Step 6: List node labels and the pod's affinity/nodeSelector requirements

```bash
kubectl get nodes --show-labels
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{"nodeSelector="}{.spec.nodeSelector}{"\nnodeAffinity="}{.spec.affinity.nodeAffinity}{"\ntopologySpreadConstraints="}{.spec.topologySpreadConstraints}{"\n"}'
```

Expected output: per-node label map, followed by the pod's `nodeSelector`, `nodeAffinity` (with `requiredDuringSchedulingIgnoredDuringExecution` and `preferredDuringSchedulingIgnoredDuringExecution` arrays), and `topologySpreadConstraints`. Cross-check: does at least one node satisfy every key/operator/value in `requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms`?

### Step 7: List PVCs referenced by the pod and their bind status

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.volumes[?(@.persistentVolumeClaim)]}{.persistentVolumeClaim.claimName}{"\n"}{end}'
kubectl get pvc -n <namespace>
kubectl describe pvc <pvc-name> -n <namespace>
```

Expected output: list of PVC names referenced by the pod, then the PVC table showing `STATUS` (`Bound`, `Pending`, or `Lost`) and `STORAGECLASS`, then a detailed description with `Events` showing `ProvisioningFailed`, `WaitForFirstConsumer`, `FailedBinding`, or `Successfully provisioned volume`.

### Step 8: Check ResourceQuota and LimitRange in the namespace

```bash
kubectl describe resourcequota -n <namespace>
kubectl describe limitrange -n <namespace>
kubectl get events -n <namespace> --field-selector reason=FailedCreate --sort-by='.lastTimestamp' | tail -20
```

Expected output: per-quota `Used` vs `Hard` columns for `requests.cpu`, `requests.memory`, `limits.cpu`, `limits.memory`, `pods`, `count/*`, etc. Any row where `Used` equals or would exceed `Hard` blocks new pods. The `FailedCreate` events from controllers carry the exact admission error string `pods "<name>" is forbidden: exceeded quota: <quota>, requested: ..., used: ..., limited: ...`.

### Step 9: Verify the kube-scheduler is running and processing the pod

```bash
kubectl get pods -n kube-system -l component=kube-scheduler
kubectl logs -n kube-system -l component=kube-scheduler --tail=100 | grep -i "<pod-name>\|FailedScheduling\|error"
kubectl get events --all-namespaces --field-selector source=default-scheduler --sort-by='.lastTimestamp' | tail -20
```

Expected output: the scheduler pod row should show `Running` with `READY: 1/1` (or for HA `READY: 1/1` on the leader and replicas Ready). The logs should contain a recent line referencing the pending pod. If no scheduler events exist cluster-wide for any recent pod, the scheduler itself is the failure. On managed services (EKS, GKE, AKS) the scheduler pod is not visible; rely on the events query instead.

### Step 10: Check hostPort conflicts and volume-attachment limits

```bash
kubectl get pod <pod-name> -n <namespace> -o jsonpath='{range .spec.containers[*].ports[*]}{.hostPort}{"  "}{.protocol}{"\n"}{end}'
kubectl get pods --all-namespaces -o jsonpath='{range .items[*].spec.containers[*].ports[?(@.hostPort)]}{.hostPort}{"  "}{end}' | tr ' ' '\n' | sort | uniq -c | sort -rn | head -20
```

Expected output: the pod's declared `hostPort` values (if any), then a frequency table of hostPort usage across the cluster. A hostPort claimed on every eligible node will surface as `node(s) didn't have free ports for the requested pod ports` in the Step 2 scheduler message.

## Causes

### Cause A: No node has enough unallocated CPU or memory for the pod's requests

**Statement:** The pod's aggregate `resources.requests.cpu` or `resources.requests.memory` exceeds the remaining unallocated capacity on every node, so the scheduler's `NodeResourcesFit` filter eliminates all candidates.

**Chain:**
- root: the pod's CPU/memory requests exceed remaining unallocated headroom on every node
- s1: the `NodeResourcesFit` predicate rejects each node (request > node allocatable minus already-bound requests)
- s2: zero candidate nodes survive filtering, so the scheduler emits `FailedScheduling` with `Insufficient cpu`/`Insufficient memory`
- D: the pod stays Pending and is never bound to a node (Symptom Recognition)

**Indicators:**
- s2: [Step 2] scheduler event message contains `Insufficient cpu`
- s2: [Step 2] scheduler event message contains `Insufficient memory`
- root: [Step 4] every node's `Allocated resources` block shows `cpu  Requests` or `memory  Requests` at or near 100% of allocatable (scheduler decides on requests, not `kubectl top` live usage)

**Interventions:**
- **remediation** (root): add capacity or right-size requests from observed usage so the request fits within some node's headroom.

  ```bash
  # Option 1: add a node (manual or via Cluster Autoscaler scale-up trigger)
  kubectl get pods -n kube-system -l app.kubernetes.io/name=cluster-autoscaler
  kubectl logs -n kube-system -l app.kubernetes.io/name=cluster-autoscaler --tail=50 | grep -i "scale up\|unschedulable"
  # Option 2: right-size requests from observed usage (Prometheus / kubectl top history)
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --requests=cpu=<right-sized-cpu>,memory=<right-sized-mem>
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** After the change, re-run Step 1; `kubectl get pod <pod-name> -n <namespace>` transitions to `Running` within 30 seconds and `kubectl describe node <assigned-node>` shows the pod under `Non-terminated Pods` with the new requests counted toward allocated.
- **mitigation** (s1): lower `resources.requests` so the pod fits an existing node's headroom.

  ```bash
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --requests=cpu=<lower-cpu>,memory=<lower-mem>
  ```

  **Risk:** Lowering requests risks CPU throttling or OOM kills if actual usage exceeds the reduced request; only safe when observed peak usage is well below the new request. **Duration:** Permanent if sized from observed p99 usage with 25-30% headroom; revisit if traffic patterns change. **Verification:** re-run Step 1; the pod reaches `Running` and `kubectl describe node <assigned-node>` counts the new requests toward allocated.

### Cause B: No node carries labels that satisfy the pod's nodeSelector or required node affinity

**Statement:** The pod's `spec.nodeSelector` or `nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution` specifies label keys/values that no node in the cluster has, so the scheduler filters every node out before scoring.

**Chain:**
- root: the pod's `nodeSelector`/required `nodeAffinity` names label keys/values that no node carries
- s1: required node affinity is a hard predicate, so every node lacking a matching label/value is filtered out
- s2: zero nodes pass, so the scheduler emits `node(s) didn't match Pod's node affinity/selector` or `nodeSelector`
- D: the pod stays Pending and is never bound to a node (Symptom Recognition)

**Indicators:**
- s2: [Step 2] scheduler event message contains `node(s) didn't match Pod's node affinity/selector`
- s2: [Step 2] scheduler event message contains `node(s) didn't match Pod's nodeSelector`
- root: [Step 6] no node in `kubectl get nodes --show-labels` satisfies every key/value pair in the pod's `nodeSelector` or `requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms` (common shapes: `nodepool=gpu` with no GPU pool, drained zone, or a typo like `beta.kubernetes.io/arch`)

**Interventions:**
- **remediation** (root): provision a matching node pool, or relax the affinity from required to preferred so scheduling falls back to any node.

  ```bash
  # Option 1: provision a matching node pool (cluster-autoscaler / managed-service console) and let it auto-label
  # Option 2: relax the affinity from required to preferred so scheduling falls back to any node
  kubectl get deployment <deployment-name> -n <namespace> -o yaml > /tmp/<name>.yaml
  # Edit nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution -> preferredDuringSchedulingIgnoredDuringExecution with weight: 100
  kubectl apply -f /tmp/<name>.yaml
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** re-run Step 1; `kubectl get pod <pod-name> -n <namespace> -o wide` shows an assigned `NODE` and `kubectl describe node <assigned-node>` confirms the node carries the required labels.
- **mitigation** (root): label an existing node so it satisfies the pod's selector.

  ```bash
  kubectl label node <node-name> <key>=<value>
  ```

  **Risk:** Labeling a node that does not actually have the property (e.g. labeling a non-GPU node `nodepool=gpu`) lets scheduling succeed but the workload fails at runtime; only label nodes that genuinely match. **Duration:** Permanent until the node-pool composition changes; safe if the label reflects reality. **Verification:** re-run Step 1; the pod gets an assigned `NODE` and `kubectl describe node <assigned-node>` shows the new label.

### Cause C: Every candidate node has a NoSchedule taint the pod does not tolerate

**Statement:** All nodes that would otherwise be feasible carry a `NoSchedule` (or `NoExecute`) taint, and the pod's `tolerations` array does not match that taint key/value/effect.

**Chain:**
- root: every otherwise-feasible node carries a `NoSchedule`/`NoExecute` taint the pod's `tolerations` do not match
- s1: the `TaintToleration` predicate filters out each node with an un-tolerated `NoSchedule`/`NoExecute` taint
- s2: zero nodes survive, so the scheduler emits `node(s) had taints that the pod didn't tolerate` / `untolerated taint`
- D: the pod stays Pending and is never bound to a node (Symptom Recognition)

**Indicators:**
- s2: [Step 2] scheduler event message contains `node(s) had taints that the pod didn't tolerate`
- s2: [Step 2] scheduler event message contains `untolerated taint`
- root: [Step 5] every node listed shows at least one `NoSchedule`/`NoExecute` taint whose key is absent from the pod's `tolerations` (dedicated pools, control-plane-only nodes, pressure taints, or a cordoned `unschedulable` node)

**Interventions:**
- **remediation** (root): add the matching toleration to the deployment so the pod can land on the tainted nodes.

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=json \
    -p='[{"op":"add","path":"/spec/template/spec/tolerations","value":[{"key":"<taint-key>","operator":"Equal","value":"<taint-value>","effect":"NoSchedule"}]}]'
  kubectl rollout status deployment/<deployment-name> -n <namespace>
  ```

  **Verification:** re-run Step 1; `kubectl get pod <pod-name> -n <namespace> -o wide` shows `NODE` populated within 30 seconds and `kubectl describe node <assigned-node>` confirms the node still carries the taint while the pod tolerates it.
- **mitigation** (root): remove the taint from a node so the pod can schedule (only if the taint was a stopgap, not a dedicated-pool design).

  ```bash
  kubectl taint nodes <node-name> <key>=<value>:NoSchedule-
  ```

  **Risk:** Removing a taint opens the node to every pod that previously avoided it; pressure taints (`memory-pressure`, `disk-pressure`) should rarely be removed — they indicate the node is unhealthy. **Duration:** Minutes-to-hours while triaging the workload's intended placement. **Verification:** re-run Step 1; the pod gets an assigned `NODE` and Step 5 shows the taint removed.

### Cause D: PersistentVolumeClaim referenced by the pod is unbound under Immediate binding mode

**Statement:** The pod mounts a PVC that is stuck `Pending` (no matching PV, provisioner failure, or no StorageClass) and the PVC's `volumeBindingMode` is `Immediate`, so the scheduler refuses to place the pod.

**Chain:**
- root: an `Immediate`-mode PVC the pod mounts fails to bind (no matching PV, no StorageClass, or `ProvisioningFailed`)
- s1: the PVC stays `Pending` because Immediate binding tried to bind at creation, before pod scheduling
- s2: the scheduler's `VolumeBinding` predicate fails any pod referencing that PVC with `pod has unbound immediate PersistentVolumeClaims`
- D: the pod stays Pending and is never bound to a node (Symptom Recognition)

**Indicators:**
- s2: [Step 2] scheduler event message contains `pod has unbound immediate PersistentVolumeClaims`
- s1: [Step 7] one or more PVCs referenced by the pod show `STATUS: Pending` in `kubectl get pvc`
- root: [Step 7] PVC events contain `ProvisioningFailed`, `no persistent volumes available for this claim and no storage class is set`, or `waiting for a volume to be created` (or a zone-topology mismatch: PV pre-committed in one zone, capacity only in another)

**Interventions:**
- **remediation** (root): switch the StorageClass to `WaitForFirstConsumer` so binding defers to pod scheduling, or fix the underlying provisioner.

  ```bash
  # Option 1: switch the StorageClass to WaitForFirstConsumer so binding defers until pod scheduling picks a node
  kubectl get storageclass <sc-name> -o yaml > /tmp/<sc>.yaml
  # Edit volumeBindingMode: WaitForFirstConsumer, then recreate (StorageClass volumeBindingMode is immutable)
  kubectl delete storageclass <sc-name> && kubectl apply -f /tmp/<sc>.yaml
  # Option 2: fix the underlying provisioner (CSI driver pod not Ready, IAM permission missing, cloud-provider quota hit)
  kubectl logs -n kube-system <csi-controller-pod> -c <csi-provisioner> --tail=100
  ```

  **Verification:** re-run Step 7; the PVC transitions to `Bound` and re-run Step 1 shows the pod transitions out of Pending within 60 seconds.
- **mitigation** (root): manually create a static PV matching the PVC to unblock the single claim.

  ```bash
  kubectl describe pvc <pvc-name> -n <namespace>
  kubectl get storageclass
  # Verify provisioner pod health (e.g. ebs-csi-controller, cinder-csi)
  kubectl get pods -A | grep -E 'csi|provisioner'
  ```

  **Risk:** A manual static PV bypasses dynamic provisioning checks; it must match `storageClassName`, `accessModes`, and capacity exactly or it will not bind. **Duration:** Diagnostic; fix the provisioner or StorageClass before relying on dynamic provisioning again. **Verification:** re-run Step 7; the PVC reaches `Bound` and the pod leaves Pending.

### Cause E: Namespace ResourceQuota blocks the controller from creating the pod

**Statement:** The namespace's ResourceQuota is exhausted, so the API server's quota admission plugin returns 403 on pod creation and the controlling ReplicaSet or StatefulSet cannot materialize the pod at all.

**Chain:**
- root: the namespace ResourceQuota is exhausted (a `Used` row equals or would exceed `Hard`)
- s1: quota admission runs synchronously on pod-create and rejects the request with `exceeded quota` / `is forbidden`
- s2: the pod is never created, so the controlling ReplicaSet/StatefulSet records a `FailedCreate` event and replicas stay below desired
- D: the workload never reaches its desired replica count; no Pending pod even appears (Symptom Recognition)

**Indicators:**
- s2: [Step 8] `kubectl get events --field-selector reason=FailedCreate` shows messages containing `exceeded quota`
- s1: [Step 8] `kubectl get events --field-selector reason=FailedCreate` shows messages containing `is forbidden`
- root: [Step 8] `kubectl describe resourcequota` shows at least one row where `Used` equals `Hard`, and `kubectl get pods -n <namespace>` shows fewer replicas than the Deployment's `spec.replicas`

**Interventions:**
- **remediation** (root): raise the quota if justified, reduce per-pod requests to fit, or add a LimitRange supplying default requests.

  ```bash
  # Option 1: raise the quota if justified (capacity review with the namespace owner)
  kubectl edit resourcequota <quota-name> -n <namespace>
  # Option 2: reduce per-pod requests so the workload fits within the existing quota
  kubectl set resources deployment/<deployment-name> -n <namespace> \
    --requests=cpu=<lower>,memory=<lower>
  # Option 3: add a LimitRange providing defaults so pods missing explicit requests are not rejected
  kubectl apply -f - <<EOF
  apiVersion: v1
  kind: LimitRange
  metadata:
    name: defaults
    namespace: <namespace>
  spec:
    limits:
    - type: Container
      defaultRequest:
        cpu: 100m
        memory: 128Mi
      default:
        cpu: 500m
        memory: 512Mi
  EOF
  ```

  **Verification:** re-run Step 8; `kubectl describe resourcequota -n <namespace>` shows `Used` below `Hard` for the saturated row and `kubectl get deployment <deployment-name> -n <namespace>` reaches its desired replica count.
- **mitigation** (s1): delete completed/failed pods to free quota headroom so the controller can create the new pod.

  ```bash
  kubectl delete pods -n <namespace> --field-selector=status.phase=Succeeded
  kubectl delete pods -n <namespace> --field-selector=status.phase=Failed
  ```

  **Risk:** Deleting completed/failed pods to free `count/pods` quota is safe; deleting active pods to free `requests.cpu`/`requests.memory` interrupts those services. **Duration:** Immediate; revisit if the namespace fills again. **Verification:** re-run Step 8; the saturated quota row drops below `Hard` and the deployment reaches desired replicas.

### Cause F: Pod requests a hostPort already taken on every eligible node

**Statement:** The pod declares a `hostPort` and every node in the cluster already has another pod bound to the same `(hostPort, protocol, hostIP)` tuple, so the `NodePorts` predicate eliminates all candidates.

**Chain:**
- root: the pod declares a `hostPort` whose `(hostPort, protocol, hostIP)` tuple is already claimed on every node
- s1: the `NodePorts` predicate filters out each node where the tuple is in use (only one pod per node may claim it)
- s2: zero nodes survive, so the scheduler emits `node(s) didn't have free ports for the requested pod ports`
- D: the pod stays Pending and is never bound to a node (Symptom Recognition)

**Indicators:**
- s2: [Step 2] scheduler event message contains `node(s) didn't have free ports for the requested pod ports`
- root: [Step 10] the pod declares one or more `hostPort` values in `.spec.containers[*].ports[*].hostPort`
- s1: [Step 10] the same hostPort is already claimed on every node (frequency table count equals or exceeds total node count — e.g. a DaemonSet hostPort, or two exporters contending on `:9100`)

**Interventions:**
- **remediation** (root): replace the hostPort with a Service so the pod is reachable without consuming a host port (or pin to a DaemonSet if one-per-node is genuinely required).

  ```bash
  # Replace hostPort with a Service (NodePort or ClusterIP) so the pod is reachable without consuming a host port
  kubectl expose deployment/<deployment-name> -n <namespace> --port=<pod-port> --target-port=<pod-port> --type=NodePort
  # OR if a hostPort is genuinely required (e.g. ingress controller), constrain replicas to one per node via a DaemonSet
  ```

  **Verification:** re-run Step 1; `kubectl get pod -l <selector> -n <namespace> -o wide` shows all replicas scheduled and `kubectl get service <deployment-name> -n <namespace>` reports an assigned `CLUSTER-IP` (and `NodePort` if applicable).
- **mitigation** (root): remove the hostPort from the pod template so the `NodePorts` predicate no longer filters nodes.

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=json \
    -p='[{"op":"remove","path":"/spec/template/spec/containers/0/ports/0/hostPort"}]'
  ```

  **Risk:** Removing the hostPort changes the workload's external connectivity model; downstream clients reaching the pod via node IP+port will break until a Service replaces the hostPort. **Duration:** Permanent if a Service replaces the role of hostPort. **Verification:** re-run Step 1; all replicas schedule and Step 10 shows the hostPort no longer declared.

### Cause G: Pod topology spread constraint with DoNotSchedule cannot be satisfied

**Statement:** The pod's `topologySpreadConstraints` with `whenUnsatisfiable: DoNotSchedule` requires a maximum skew of pods across topology domains (zones, nodes) that no remaining node can satisfy without exceeding the skew.

**Chain:**
- root: a `topologySpreadConstraints` with `whenUnsatisfiable: DoNotSchedule` (or required pod anti-affinity) demands balance no node can meet
- s1: the `PodTopologySpread` predicate rejects any node whose addition would breach `maxSkew` (a depleted zone has no eligible node)
- s2: zero nodes survive, so the scheduler emits `node(s) didn't match pod topology spread constraints` / `didn't satisfy existing pods anti-affinity rules`
- D: the pod stays Pending and is never bound to a node (Symptom Recognition)

**Indicators:**
- s2: [Step 2] scheduler event message contains `node(s) didn't match pod topology spread constraints`
- s2: [Step 2] scheduler event message contains `node(s) didn't satisfy existing pods anti-affinity rules`
- root: [Step 6] the pod spec contains `topologySpreadConstraints` with `whenUnsatisfiable: DoNotSchedule` or `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution`

**Interventions:**
- **remediation** (root): add capacity in the depleted topology domain (new node in the zone, or uncordon drained nodes), or raise `maxSkew` if looser balance is acceptable.

  ```bash
  # Option 1: add capacity in the depleted topology domain (new node in the zone, or restore drained nodes)
  kubectl get nodes -L topology.kubernetes.io/zone
  kubectl uncordon <drained-node>
  # Option 2: increase maxSkew if the workload can tolerate looser balance
  kubectl patch deployment <deployment-name> -n <namespace> --type=json \
    -p='[{"op":"replace","path":"/spec/template/spec/topologySpreadConstraints/0/maxSkew","value":3}]'
  ```

  **Verification:** re-run Step 1; `kubectl get pod -l <selector> -n <namespace> -o wide` shows pods distributed across zones and all replicas reach `STATUS: Running`.
- **mitigation** (root): relax `whenUnsatisfiable` from `DoNotSchedule` to `ScheduleAnyway` so the pod can schedule despite skew.

  ```bash
  kubectl patch deployment <deployment-name> -n <namespace> --type=json \
    -p='[{"op":"replace","path":"/spec/template/spec/topologySpreadConstraints/0/whenUnsatisfiable","value":"ScheduleAnyway"}]'
  ```

  **Risk:** `ScheduleAnyway` lets the workload concentrate in one zone — a single-zone outage will then take all replicas down. **Duration:** Hours-to-days; revert once the depleted zone has capacity again. **Verification:** re-run Step 1; the pod reaches `Running` (note resilience is temporarily reduced until reverted).

### Cause H: Pod has scheduling gates that have not been cleared

**Statement:** The pod's `.spec.schedulingGates` array is non-empty, so the scheduler does not even queue the pod for evaluation and the pod stays in `STATUS: SchedulingGated` indefinitely until a controller (or operator) removes the gates.

**Chain:**
- root: the pod's `.spec.schedulingGates` array is non-empty (set at creation by an admission/quota/custom controller)
- s1: the scheduler skips any gated pod entirely — it never enters the scheduling queue and emits no `FailedScheduling` event
- s2: the pod sits in `STATUS: SchedulingGated` with `PodScheduled False SchedulingGated` until every gate is PATCHed away (a hung gate-controller blocks it permanently)
- D: the pod is never queued or bound to a node (Symptom Recognition)

**Indicators:**
- s2: [Step 1] pod `STATUS` is the literal string `SchedulingGated`
- s2: [Step 3] `PodScheduled  False  SchedulingGated` appears in the conditions output
- s1: [Step 2] the Events table contains NO `FailedScheduling` event (the scheduler never considered the pod)

**Interventions:**
- **remediation** (root): identify and fix the controller that owns the gate, or remove the gate from the pod template if the controller is no longer needed.

  ```bash
  # Identify the controller that owns the gate (gate name often namespaced, e.g. example.com/quota-check)
  kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.schedulingGates[*].name}'
  # Inspect the controller's logs to find out why it has not cleared the gate
  kubectl get pods --all-namespaces -l <controller-selector>
  kubectl logs -n <controller-ns> <controller-pod> --tail=200
  # Fix the controller, or remove the gate from the pod template if the controller is no longer needed
  kubectl edit deployment <deployment-name> -n <namespace>   # remove spec.template.spec.schedulingGates
  ```

  **Verification:** re-run Step 1; the pod transitions from `SchedulingGated` to `Pending` then `Running`, and `kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.schedulingGates}'` returns empty/null.
- **mitigation** (s2): manually clear the scheduling gate on the affected pod to release it from the queue hold.

  ```bash
  kubectl get pod <pod-name> -n <namespace> -o jsonpath='{.spec.schedulingGates}'
  kubectl patch pod <pod-name> -n <namespace> --type=json \
    -p='[{"op":"replace","path":"/spec/schedulingGates","value":[]}]'
  ```

  **Risk:** Manually clearing a gate bypasses the controller that owns it; if it was holding the pod for a quota or admission check, the pod may run despite a policy violation. Confirm with the gate-owner first. **Duration:** Permanent for the affected pod; new pods will be created with the gate again until the controller is fixed. **Verification:** re-run Step 1; the pod leaves `SchedulingGated` and reaches `Running`.

### Cause I: kube-scheduler is down or partitioned from the API server

**Statement:** No active kube-scheduler instance is processing pods cluster-wide, so every newly-created pod accumulates in Pending without any `FailedScheduling` event because no scheduler is generating events.

**Chain:**
- root: no active kube-scheduler leader is processing pods (leader crashed, lost its lease, or is partitioned from the API server)
- s1: no scheduling decisions occur, so newly-created pods are never evaluated and the scheduler generates no events
- s2: every recently-created pod across the cluster sits in Pending, often with empty `Events` (the event source is offline)
- D: the pod (and all peers) stay Pending and are never bound to a node (Symptom Recognition)

**Indicators:**
- root: [Step 9] `kubectl get pods -n kube-system -l component=kube-scheduler` shows no Running pod, or all replicas in `CrashLoopBackOff`, `Pending`, or `Error`
- s1: [Step 9] `kubectl get events --field-selector source=default-scheduler` returns no events in the last several minutes despite multiple recently-created pods
- s2: [Symptom] every recently-created pod across the cluster is Pending, not just the one being investigated

**Interventions:**
- **remediation** (root): inspect scheduler logs/config and restore a healthy scheduler (self-hosted), or open a vendor support ticket (managed services).

  ```bash
  # Self-hosted clusters: inspect scheduler logs and config
  kubectl logs -n kube-system -l component=kube-scheduler --tail=200 --previous
  kubectl get configmap -n kube-system kube-scheduler-config -o yaml
  # Managed services (EKS/GKE/AKS): the scheduler is vendor-operated — open a support ticket with the cluster ID, region, and approximate time of the outage.
  ```

  **Verification:** re-run Step 9; `kubectl get pods -n kube-system -l component=kube-scheduler` shows `Running 1/1` (or expected HA count) and a new test pod (`kubectl run test --image=busybox --restart=Never -- sleep 60`) reaches `Running` within 30 seconds.
- **mitigation** (root): restart the scheduler pod to force re-election of a healthy leader.

  ```bash
  kubectl delete pod -n kube-system -l component=kube-scheduler
  ```

  **Risk:** Restarting is safe in HA setups (a follower takes over); in single-replica clusters there is a 10-30 second scheduling outage during restart. **Duration:** Single restart cycle (seconds). **Verification:** re-run Step 9; the scheduler returns to `Running` and a new test pod schedules within 30 seconds.

### Cause Z: Unidentified

**Statement:** The pod is stuck in Pending (or `SchedulingGated`) but no indicator from Causes A through I matches the gathered evidence.

**Chain:**
- root: the scheduler is failing to place the pod (or the pod is not queued) for a reason outside Causes A-I
- s1: the collected event, node ledger, taints, labels, PVC state, quota, hostPort, topology, gates, and scheduler health point to no standard failure mode (e.g. custom `schedulerName`, admission webhook dropping bind, blocked preemption, autoscaler with no matching template, custom PreFilter plugin)
- D: the pod stays Pending or `SchedulingGated` and is never bound to a node (Symptom Recognition)

**Indicators:**
- root: [Default] Pod confirmed `Pending` or `SchedulingGated` ([Step 1], [Step 3]) but Causes A-I indicators do not match the gathered evidence

**Interventions:**
- **mitigation** (D): capture a full diagnostic snapshot and escalate to the SME.

  ```bash
  kubectl run probe --image=registry.k8s.io/pause:3.9 --restart=Never -n <namespace>
  kubectl get pod probe -n <namespace> -o wide
  kubectl delete pod probe -n <namespace>
  ```

  Capture the artefacts from Steps 1-10 (pod description, scheduler event verbatim, structured conditions, node resource ledger, node taints/labels, pod affinity/topology spec, PVC inventory, quota state, scheduler health, hostPort frequency table) plus the pod's `.spec.schedulerName` and any custom scheduler logs, and escalate to the platform on-call or cluster operator with the failure-mode summary. **Risk:** The probe pod (stripped of nodeSelector, tolerations, affinity, topology, hostPort) may bypass intended placement rules; delete it after triage. **Duration:** Single probe cycle (under 1 minute). **Verification:** hand-off acknowledged by the receiving engineer; an incident ticket is opened with the captured artefacts attached and a follow-up owner assigned.

## Prevention

- Set both `requests` and `limits` for CPU and memory on every container, and enforce defaults cluster-wide via a `LimitRange` per namespace so workloads cannot be deployed without sizing.
- Run Cluster Autoscaler (or its managed equivalent: Karpenter on EKS, GKE node auto-provisioning, AKS cluster autoscaler) with node-pool templates matching every workload profile in the cluster (GPU, large-memory, zone-pinned).
- Alert on `kube_pod_status_phase{phase="Pending"}` lasting more than 10 minutes with a Prometheus rule so Pending pods surface before users notice. A complementary alert on `scheduler_pending_pods{queue="gated"} > 0` catches stuck scheduling gates.
- Use `WaitForFirstConsumer` as the StorageClass `volumeBindingMode` for any topology-aware storage (EBS, Persistent Disk, Azure Disk) so PVC binding follows pod scheduling instead of pre-committing to a zone.
- Audit ResourceQuota headroom monthly: page when `Used / Hard` exceeds 80% on any quota row so capacity can be adjusted before pods fail to create.
- Avoid `hostPort` for workloads with more than one replica per node — use a `Service` (ClusterIP or NodePort) instead, or pin the workload to a `DaemonSet` if one-per-node is genuinely required.
- Treat node taints as policy artefacts: tag every custom taint with an owner and a sunset date, and remove cordons / maintenance taints promptly once the operation completes.
- Pin scheduler configuration in version control: a misconfigured scheduler profile (custom PreFilter plugin, wrong leader-election lease) is hard to spot without a known-good baseline to diff against.
- Validate workload constraints in CI: kustomize/helm rendering should fail the pipeline when a manifest references a `nodeSelector` key with no matching node in the target cluster's known label set.

## Sources

- [Kubernetes - Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) - Priority 1. Pending-pod diagnostic flow, the role of `kubectl describe pod`, common scheduling-failure causes (insufficient resources, hostPort).
- [Kubernetes - kube-scheduler](https://kubernetes.io/docs/concepts/scheduling-eviction/kube-scheduler/) - Priority 1. Scheduling lifecycle (filtering then scoring), `FailedScheduling` event semantics, predicate and scoring plugin model.
- [Kubernetes - Taints and Tolerations](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/) - Priority 1. Well-known taints (`node-role.kubernetes.io/control-plane`, `node.kubernetes.io/disk-pressure`, `memory-pressure`, `pid-pressure`, `unschedulable`, `not-ready`), `NoSchedule` vs `NoExecute` vs `PreferNoSchedule`, toleration operator semantics.
- [Kubernetes - Assigning Pods to Nodes](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/) - Priority 1. nodeSelector vs node affinity, `requiredDuringSchedulingIgnoredDuringExecution` vs `preferredDuringSchedulingIgnoredDuringExecution`, pod topology spread constraints (`maxSkew`, `whenUnsatisfiable`).
- [Kubernetes - Persistent Volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/) - Priority 1. PVC binding lifecycle, `volumeBindingMode: Immediate` vs `WaitForFirstConsumer`, `ProvisioningFailed`, the `pod has unbound immediate PersistentVolumeClaims` scheduler message.
- [Kubernetes - Resource Quotas](https://kubernetes.io/docs/concepts/policy/resource-quotas/) - Priority 1. Quota admission semantics, the `exceeded quota` / `is forbidden` rejection string, LimitRange interaction for default requests, why Deployment creation succeeds while pod creation fails.
- [Kubernetes - Pod Scheduling Readiness](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-scheduling-readiness/) - Priority 1. Scheduling gates, the `SchedulingGated` pod status, `scheduler_pending_pods{queue="gated"}` metric, gate creation/removal rules.
