---
id: "java-jvm-oom"
title: "Java JVM OutOfMemoryError"
domain: application
service: java
symptom_class: [oom]
severity: critical
scope: global
version: "2.0.0"
last_updated: "2026-06-25"
verified_by: "kb-researcher"
status: draft
tags: [jvm, heap, metaspace, gc, memory-leak, hotspot]
difficulty: advanced
---

## Symptom Recognition

- `java.lang.OutOfMemoryError: Java heap space` in application logs or stderr
- `java.lang.OutOfMemoryError: GC overhead limit exceeded` — JVM spending 98%+ of time in GC, recovering less than 2% of heap across 5 consecutive full GC cycles
- `java.lang.OutOfMemoryError: Metaspace` — class metadata exhausted in native memory
- `java.lang.OutOfMemoryError: Direct buffer memory` — off-heap NIO buffer pool exhausted
- `java.lang.OutOfMemoryError: unable to create new native thread` — OS thread limit reached
- `java.lang.OutOfMemoryError: Compressed class space` — compressed class pointer space exhausted
- In Kubernetes: pod terminated with exit code 137 (`OOMKilled`) and no Java-level OOM message in logs
- Application becomes unresponsive with CPU near 100% (GC thrash) before crashing
- Frequent full GC events visible in GC logs with old generation not recovering

## Applicability

Applies to Java 11+ (HotSpot JVM) on Linux. Requires access to the JVM process via `jcmd`, `jmap`, and `jstat`. For Kubernetes, `kubectl exec` access to the running pod is required. Eclipse MAT or JDK Mission Control (JMC) is needed for heap dump analysis. Native Memory Tracking (NMT) requires JVM restart with `-XX:NativeMemoryTracking=detail` enabled at startup.

## Diagnostic Steps

### Step 1: Identify the OOM error variant

Identify the OOM error variant from application logs to determine which memory region is exhausted.

```bash
grep -i "OutOfMemoryError" /var/log/app/application.log | tail -20
```

For Kubernetes pods:

```bash
kubectl logs <pod-name> -n <namespace> --previous | grep -i "OutOfMemoryError"
kubectl describe pod <pod-name> -n <namespace> | grep -A5 "Last State"
```

Expected output: One of `Java heap space`, `GC overhead limit exceeded`, `Metaspace`, `Direct buffer memory`, `unable to create new native thread`, or `Compressed class space`. For Kubernetes OOMKilled, `describe pod` shows `Reason: OOMKilled`.

### Step 2: Check JVM memory configuration and heap utilization

Check JVM memory configuration and current heap utilization to determine whether the heap is undersized or genuinely leaking.

```bash
jps -v | grep -v Jps
jcmd <pid> VM.flags | grep -E "Xmx|Xms|MaxMetaspaceSize|MaxDirectMemorySize|MaxRAMPercentage"
jcmd <pid> GC.heap_info
```

Expected output: `GC.heap_info` prints used heap, committed heap, and max heap. A healthy application uses 40–70% of max heap at steady state. Used heap above 85% of max heap indicates imminent OOM.

### Step 3: Monitor GC behavior in real time

Monitor GC behavior in real time to determine whether GC is keeping up with allocation or thrashing.

```bash
jstat -gcutil <pid> 5000 20
```

Expected output: Columns `S0`, `S1` (survivor spaces), `E` (Eden), `O` (old generation), `M` (Metaspace), `YGC`, `FGC`, `GCT`. Old generation (`O`) above 90% and `FGC` incrementing every few seconds indicates GC thrash. `GCT` exceeding 5% of uptime degrades application throughput.

### Step 4: Capture a heap dump for offline analysis

Capture a heap dump for offline analysis to identify which objects are consuming memory and why they cannot be collected.

```bash
jcmd <pid> GC.heap_dump /tmp/heapdump_$(date +%s).hprof
```

Expected output: A `.hprof` file approximately equal to used heap size. Generation takes roughly 2 seconds per GB of used heap, during which the JVM is paused. If the JVM is unresponsive, capture the dump before killing — it is the most valuable diagnostic artifact.

### Step 5: Analyze the heap dump for retained objects

Analyze the heap dump with Eclipse MAT or JDK Mission Control to identify the largest retained objects and reference chains preventing garbage collection.

```bash
jcmd <pid> GC.class_histogram filename=/tmp/histogram.txt
```

In Eclipse MAT: open the `.hprof` file, run Leak Suspects Report, then examine the Dominator Tree for objects retaining the most heap by retained size.

Expected output: The class histogram shows instance counts and byte totals per class. The Leak Suspects Report identifies 1–3 objects holding disproportionate heap. Right-click → Path to GC Roots → exclude weak references to trace back to the holding GC root (static field = cache, Thread = ThreadLocal, ClassLoader = classloader leak).

### Step 6: Check class loading statistics

Check class loading statistics when the OOM variant is `Metaspace` or `Compressed class space`.

```bash
jcmd <pid> VM.classloader_stats
jstat -gcmetacapacity <pid> 5000 10
```

Expected output: `VM.classloader_stats` prints classloader hierarchy with class counts. A growing class count over successive polls indicates a classloader leak. `gcmetacapacity` shows Metaspace used, committed, and max — Metaspace near `MaxMetaspaceSize` confirms exhaustion.

### Step 7: Check thread count

Check thread count when the OOM variant is `unable to create new native thread`.

```bash
jcmd <pid> Thread.print | grep -c "tid="
cat /proc/<pid>/status | grep Threads
ulimit -u
cat /proc/sys/kernel/threads-max
```

Expected output: Thread count above 5,000 is abnormal for most applications. Each thread allocates 512 KB–1 MB of native stack. If `ulimit -u` or `threads-max` is lower than the thread count, the OS refuses to create more threads.

### Step 8: Use Native Memory Tracking to diagnose native growth

Use Native Memory Tracking (NMT) to diagnose native heap growth when Java-level OOM is absent but process RSS grows continuously. Requires JVM to be started with NMT enabled.

```bash
jcmd <pid> VM.native_memory summary.diff
```

Expected output: NMT diff shows reserved and committed delta per category (Java Heap, Class, Thread, Code). Growing `Class` or `Thread` committed memory indicates classloader or thread leaks respectively.

## Causes

### Cause A: Unbounded in-memory cache or collection

**Statement:** A collection (`HashMap`, `ConcurrentHashMap`, `ArrayList`, or equivalent) reachable from a static field or long-lived scope grows without eviction until it exhausts the heap.

**Chain:**
- root: a static or application-scoped field holds a collection with no eviction or expiry policy
- s1: each new entry adds a strong reference, so GC cannot reclaim entries and the retained set grows monotonically across GC cycles
- s2: the old generation fills continuously; full GC cannot reclaim these live objects
- D: heap is exhausted and `Java heap space` / `GC overhead limit exceeded` is thrown (points at Symptom Recognition)

**Indicators:**
- root: [Step 5] Dominator Tree shows `HashMap$Node[]` or `ArrayList` reachable from a static field retaining the majority of heap
- s2: [Step 3] Old generation (`O`) climbs to 90%+ and does not drop after full GC
  <!-- match: {"step": 3, "predicate": "threshold", "target": "O", "op": ">", "value": 90} -->
- D: [Step 1] Log shows `Java heap space` or `GC overhead limit exceeded`
  <!-- match: {"step": 1, "predicate": "contains", "target": "Java heap space"} -->

**Interventions:**
- **remediation** (root): Replace the unbounded collection with a bounded cache in application code, then redeploy.

  ```bash
  # Replace unbounded Map with a Caffeine bounded cache in application code:
  # Cache<Key, Value> cache = Caffeine.newBuilder()
  #     .maximumSize(10_000)
  #     .expireAfterWrite(Duration.ofMinutes(30))
  #     .build();
  # Redeploy after code change. Cluster-wide; requires application redeployment.
  # Rollback: kubectl rollout undo deployment/<name> -n <namespace>
  ```

  **Verification:** After fix, run `jstat -gcutil <pid> 10000` for 1 hour. Old generation (`O`) must stabilize below 80% and not trend upward under stable load. `FGC` count increments no more than once per 10 minutes.
- **mitigation** (s2): Restart the process to clear the accumulated cache and buy time for the durable fix.

  ```bash
  sudo systemctl restart myapp
  # Kubernetes:
  kubectl rollout restart deployment/<name> -n <namespace>
  ```

  **Risk:** Restarting clears the cache, causing a cold-start load spike on downstream services. **Duration:** 1–5 minutes; monitor heap post-restart. **Verification:** `jcmd <pid> GC.heap_info` shows used heap back at steady-state baseline immediately after restart.

### Cause B: Database queries returning unbounded result sets

**Statement:** A query fetches all rows from a large table without pagination, loading the full result set into heap as entity objects.

**Chain:**
- root: a `findAll()`-style query (or equivalent Spring Data method) runs without pagination against a large, growing table
- s1: every matching row is materialized into heap as a list of ORM entity objects, producing a large allocation spike per call
- s2: as the table grows the spike exhausts the old generation; GC cannot reclaim these massive allocations fast enough
- D: heap is exhausted and `Java heap space` / `GC overhead limit exceeded` is thrown (points at Symptom Recognition)

**Indicators:**
- root: [Step 1] OOM coincides with specific API endpoints (visible in access logs) or scheduled jobs
- s1: [Step 5] Heap histogram shows large counts of JPA entity classes (`com.example.*Entity`) or `Object[]` arrays
- s1: [Step 4] Heap dump Leak Suspects path traces through `List` to ORM-managed collections
- D: [Step 1] Log shows `Java heap space`
  <!-- match: {"step": 1, "predicate": "contains", "target": "Java heap space"} -->

**Interventions:**
- **remediation** (root): Implement pagination or streaming in the data-access layer, then redeploy.

  ```bash
  # Implement pagination in Spring Data (keyset or offset):
  # Page<Entity> findAll(Pageable pageable);
  # repository.findAll(PageRequest.of(0, 500));
  # Or use streaming: Stream<Entity> streamAll();
  # Redeploy after code change.
  ```

  **Verification:** Monitor heap with `jcmd <pid> GC.heap_info` before and after the endpoint call. Used heap should return to pre-call baseline after GC, not grow each call.
- **mitigation** (s2): Replace the affected pod to clear the allocated result set immediately.

  ```bash
  kubectl delete pod <pod-name> -n <namespace>
  ```

  **Risk:** Restart clears allocated objects immediately; data is not lost. **Duration:** ~30 seconds for pod replacement. **Verification:** `kubectl get pod <pod-name> -n <namespace>` shows a fresh Running pod and `jcmd <pid> GC.heap_info` shows used heap at baseline.

### Cause C: ThreadLocal values not cleaned in thread pools

**Statement:** `ThreadLocal` values set during request processing are never removed, causing each pooled thread to accumulate references to request objects across its entire lifetime.

**Chain:**
- root: request-processing code sets a `ThreadLocal` but never calls `ThreadLocal.remove()`, on threads owned by a long-lived thread pool
- s1: pool threads are reused across requests and stay alive as GC roots, so each thread's `ThreadLocalMap` retains every request object it ever held
- s2: the aggregate retained set grows proportionally to thread count times request object size, filling the old generation
- D: heap is exhausted and `Java heap space` is thrown (points at Symptom Recognition)

**Indicators:**
- root: [Step 5] Path to GC Roots for retained objects ends at `Thread` → `ThreadLocalMap` → `Entry`
- s1: [Step 7] Thread count is stable (not exploding) but heap grows proportionally to request volume
- s2: [Step 3] Old generation grows gradually under load and does not return to baseline after GC

**Interventions:**
- **remediation** (root): Call `ThreadLocal.remove()` in a `finally` block after each request, or replace `ThreadLocal` with request-scoped beans, then redeploy.

  ```bash
  # Ensure ThreadLocal.remove() is called in a finally block after each request:
  # try {
  #     threadLocal.set(value);
  #     processRequest();
  # } finally {
  #     threadLocal.remove();
  # }
  # Or replace ThreadLocal with request-scoped Spring beans.
  ```

  **Verification:** After fix, `jstat -gcutil <pid> 10000` under steady load shows old generation (`O`) stable or declining between full GC cycles, not monotonically increasing.
- **mitigation** (s2): Restart the application to clear all accumulated thread-local state.

  ```bash
  kubectl rollout restart deployment/<name> -n <namespace>
  ```

  **Risk:** Low — restarting the application clears all thread state. **Duration:** 2–5 minutes. **Verification:** `jstat -gcutil <pid> 10000` post-restart shows old generation back at baseline and growing only slowly under load.

### Cause D: Classloader leak in hot-redeployment environment

**Statement:** A surviving object (framework, library, static field, JDBC driver, or thread context) holds a reference to a class loaded by an old classloader, so the old classloader and all its loaded classes cannot be garbage collected.

**Chain:**
- root: after undeployment, a surviving object still references a class loaded by the old deployment's classloader (static field, thread context, JDBC driver, or logging framework)
- s1: the referenced class pins the entire old classloader graph — all its loaded classes and static state — so the classloader cannot be GC'd and stays resident in Metaspace
- s2: each hot-redeploy creates a new classloader while the old ones leak, so retained Metaspace grows with every redeployment until it approaches `MaxMetaspaceSize`
- D: Metaspace is exhausted and `Metaspace` OOM is thrown (points at Symptom Recognition)

**Indicators:**
- root: [Step 6] `VM.classloader_stats` shows a growing number of classloaders with each redeployment
- s2: [Step 6] `jstat -gcmetacapacity` shows Metaspace committed approaching `MaxMetaspaceSize`
  <!-- match: {"step": 6, "predicate": "threshold", "target": "M", "op": ">", "value": 90} -->
- D: [Step 1] OOM variant is `Metaspace`
  <!-- match: {"step": 1, "predicate": "contains", "target": "Metaspace"} -->

**Interventions:**
- **remediation** (root): Fix the leaking reference so the old classloader becomes collectible (deregister JDBC drivers, flush offending ThreadLocals), and set a hard Metaspace cap to fail leaks faster.

  ```bash
  # Set a hard Metaspace cap to make leaks fail faster:
  # Add to JVM flags: -XX:MaxMetaspaceSize=512m
  # Investigate and fix the classloader reference:
  #   - Deregister JDBC drivers in ServletContextListener.contextDestroyed()
  #   - Flush ThreadLocal values referencing application classes
  #   - Use jmap -clstats to identify which classloader holds the most classes
  ```

  **Verification:** After fix and redeployment, `jstat -gcmetacapacity <pid> 5000` shows Metaspace committed stable across multiple deployments, not growing with each cycle.
- **mitigation** (s2): Capture the classloader state, then restart the JVM to release all leaked classloaders.

  ```bash
  jcmd <pid> VM.classloader_stats > /tmp/classloader_before.txt
  sudo systemctl restart myapp
  ```

  **Risk:** Full JVM restart required; brief service interruption. **Duration:** Full restart, 1–5 minutes. **Verification:** `jstat -gcmetacapacity <pid> 5000` after restart shows Metaspace committed back at baseline.

### Cause E: Container memory limit below JVM total memory footprint

**Statement:** The Kubernetes container memory limit is set at or below the JVM's total memory footprint (heap + Metaspace + thread stacks + direct buffers + JIT code cache), so the cgroup OOM killer terminates the JVM before any Java-level OutOfMemoryError is thrown.

**Chain:**
- root: the container memory limit is set close to or equal to `-Xmx`, leaving no headroom for non-heap regions (Metaspace, ~1 MB/thread stacks, JIT code cache up to 240 MB, direct buffers — typically 300 MB–1 GB on top of heap)
- s1: under normal operation total JVM RSS exceeds the container memory limit because `-Xmx` controls only heap, not the non-heap regions
- s2: the cgroup OOM killer sends SIGKILL to the JVM (exit code 137) with no Java-level OOM log entry produced
- D: the pod terminates as OOMKilled (points at Symptom Recognition)

**Indicators:**
- root: [Step 2] `-Xmx` is close to the container memory limit (within 20%)
- s2: [Step 1] `kubectl describe pod` shows `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`
  <!-- match: {"step": 1, "predicate": "exit_code", "target": 137} -->
- s2: [Step 1] No `OutOfMemoryError` in application logs
- D: [Step 1] `kubectl describe pod` shows `OOMKilled`
  <!-- match: {"step": 1, "predicate": "contains", "target": "OOMKilled"} -->

**Interventions:**
- **remediation** (root): Use percentage-based heap sizing and size the container limit to ~1.3x heap so non-heap regions have headroom.

  ```bash
  # Use percentage-based heap sizing and set container limit to 1.3x heap:
  # JVM flag: -XX:MaxRAMPercentage=75.0
  # This automatically sets Xmx to 75% of container memory,
  # leaving 25% for non-heap regions.
  # Set container limit: limits.memory = desired_heap / 0.75
  # Cluster-wide for the affected deployment; requires rolling restart.
  # Rollback: kubectl rollout undo deployment/<name> -n <namespace>
  ```

  **Verification:** After fix, `kubectl get pods -n <namespace>` shows 0 restarts over 24 hours. `jcmd <pid> GC.heap_info` shows JVM healthy. `kubectl top pod <pod-name>` shows RSS well below container limit.
- **mitigation** (s1): Raise the container memory limit to give the existing JVM footprint headroom.

  ```bash
  kubectl patch deployment <name> -n <namespace> --type='json' \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"<NEW_LIMIT>"}]'
  ```

  **Risk:** Increasing container memory limit may affect cluster scheduling if node capacity is tight. **Duration:** Rolling restart, 2–5 minutes. **Verification:** `kubectl get pods -n <namespace>` shows the pod Running with no further OOMKilled events; `kubectl top pod <pod-name>` shows RSS below the new limit.

### Cause F: NIO direct buffer exhaustion

**Statement:** Off-heap `ByteBuffer.allocateDirect()` buffers accumulate because they are not explicitly freed and GC does not collect them promptly, exhausting the direct buffer pool bounded by `-XX:MaxDirectMemorySize`.

**Chain:**
- root: code (often Netty or an NIO framework) allocates direct buffers via `ByteBuffer.allocateDirect()` without explicitly freeing them
- s1: a direct buffer's native memory is only released when its `DirectByteBuffer` Java object is GC'd, but full GC runs infrequently in heap-light applications, so native direct-buffer usage grows while heap stays low
- s2: direct buffer usage reaches the `-XX:MaxDirectMemorySize` bound
- D: the direct buffer pool is exhausted and `Direct buffer memory` OOM is thrown (points at Symptom Recognition)

**Indicators:**
- root: [Step 8] NMT shows growing native committed memory not accounted for by heap, class, or thread categories
- s1: [Step 2] Heap usage is low (below 50%) while the OOM occurs
- D: [Step 1] OOM variant is `Direct buffer memory`
  <!-- match: {"step": 1, "predicate": "contains", "target": "Direct buffer memory"} -->

**Interventions:**
- **remediation** (root): Ensure direct buffers are released (let references go out of scope or explicitly clean them), enable Netty leak detection to find allocate-without-release sites, and raise `MaxDirectMemorySize` as a short-term cushion.

  ```bash
  # Increase MaxDirectMemorySize as a short-term measure:
  # -XX:MaxDirectMemorySize=2g
  #
  # For Netty applications, enable leak detection to find allocate-without-release sites:
  # -Dio.netty.leakDetection.level=PARANOID
  #
  # Ensure ByteBuffer references go out of scope or are explicitly freed with
  # ((DirectBuffer) buf).cleaner().clean() where applicable.
  ```

  **Verification:** After fix, `jcmd <pid> VM.native_memory summary` shows direct memory (reported under Internal or Code category) stable under load. No further `Direct buffer memory` OOM events in logs.
- **mitigation** (s1): Force a GC to collect phantom-reachable direct buffer objects and release their native memory.

  ```bash
  jcmd <pid> GC.run
  ```

  **Risk:** Low — triggering GC forces collection of phantom-reachable direct buffer objects. **Duration:** GC pause 0.5–10 seconds. **Verification:** `jcmd <pid> VM.native_memory summary` shows direct/native committed memory dropping after the forced GC.

### Cause Z: Unidentified

**Statement:** The JVM OutOfMemoryError cannot be attributed to any of the documented causes with available diagnostic data.

**Indicators:**
- [Default] None of the documented cause indicators match the collected diagnostics

**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot (heap dump, thread dump, NMT output), restart the service, and escalate to a JVM specialist.

  ```bash
  jcmd <pid> GC.heap_dump /tmp/heapdump_$(date +%s).hprof
  jcmd <pid> Thread.print > /tmp/threaddump_$(date +%s).txt
  jcmd <pid> VM.native_memory > /tmp/nmt_$(date +%s).txt
  kill -9 <pid>
  sudo systemctl start myapp
  ```

  **Risk:** Low — capture diagnostics before restarting to preserve evidence. **Duration:** 5–15 minutes for artifact capture and restart. Escalate to a JVM specialist with heap dump, thread dump, NMT output, and full GC log (`-Xlog:gc*:file=/tmp/gc.log:time,uptime,level,tags:filecount=5,filesize=50m`). **Verification:** Application restarts and responds to health checks: `curl -s http://localhost:8080/health` returns HTTP 200.

## Prevention

- Enable crash diagnostics in all environments: `-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/var/dumps/ -XX:+ExitOnOutOfMemoryError`. `ExitOnOutOfMemoryError` ensures the orchestrator restarts the degraded JVM immediately.
- Use percentage-based heap sizing in containers: `-XX:MaxRAMPercentage=75.0` instead of hardcoded `-Xmx`. This adapts automatically when container memory limits change and leaves room for non-heap regions.
- Enable GC logging with negligible overhead: `-Xlog:gc*:file=/var/log/app/gc.log:time,uptime,level,tags:filecount=5,filesize=50m`. GC logs are the first artifact examined in any OOM post-mortem.
- Alert before OOM occurs: use Micrometer or Prometheus JVM metrics and alert when `jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"} > 0.85` for 10 consecutive minutes.
- Use bounded caches exclusively: replace raw `HashMap` or `ConcurrentHashMap` used as caches with Caffeine or Guava caches configured with `maximumSize` and `expireAfterWrite`.
- Paginate all database queries: never call `findAll()` on tables with unbounded row counts. Use `Pageable` with Spring Data or keyset pagination for large result sets.
- Always call `ThreadLocal.remove()` in a `finally` block after request processing. In Spring, prefer request-scoped beans over `ThreadLocal`.
- Enable NMT in summary mode in production: `-XX:NativeMemoryTracking=summary`. The performance overhead is under 5% and enables baseline diffing during incidents.

## Sources

- [Oracle — Troubleshoot Memory Leaks, Java SE 21](https://docs.oracle.com/en/java/javase/21/troubleshoot/troubleshooting-memory-leaks.html) — Official diagnostic commands (jcmd, jmap, NMT, JFR), heap dump analysis workflow, OOM variant table with JVM flags. Priority 1.
- [Oracle — Understand the OutOfMemoryError Exception, Java SE 8](https://docs.oracle.com/javase/8/docs/technotes/guides/troubleshoot/memleaks002.html) — Authoritative description of each OOM variant with exact error message strings and resolution approaches. Priority 1.
- [Oracle — Diagnostic Tools for Java SE 21](https://docs.oracle.com/en/java/javase/21/troubleshoot/diagnostic-tools.html) — Full reference for jcmd, jmap, jstat, jstack, NMT, and JFR with expected output examples. Priority 1.
