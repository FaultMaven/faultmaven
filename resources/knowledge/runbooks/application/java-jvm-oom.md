---
id: "java-jvm-oom"
title: "Java JVM OutOfMemoryError"
domain: application
service: java
symptom_class: [oom]
severity: critical
scope: global
version: "1.0.0"
last_updated: "2026-05-12"
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

### Step 1

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

### Step 2

Check JVM memory configuration and current heap utilization to determine whether the heap is undersized or genuinely leaking.

```bash
jps -v | grep -v Jps
jcmd <pid> VM.flags | grep -E "Xmx|Xms|MaxMetaspaceSize|MaxDirectMemorySize|MaxRAMPercentage"
jcmd <pid> GC.heap_info
```

Expected output: `GC.heap_info` prints used heap, committed heap, and max heap. A healthy application uses 40–70% of max heap at steady state. Used heap above 85% of max heap indicates imminent OOM.

### Step 3

Monitor GC behavior in real time to determine whether GC is keeping up with allocation or thrashing.

```bash
jstat -gcutil <pid> 5000 20
```

Expected output: Columns `S0`, `S1` (survivor spaces), `E` (Eden), `O` (old generation), `M` (Metaspace), `YGC`, `FGC`, `GCT`. Old generation (`O`) above 90% and `FGC` incrementing every few seconds indicates GC thrash. `GCT` exceeding 5% of uptime degrades application throughput.

### Step 4

Capture a heap dump for offline analysis to identify which objects are consuming memory and why they cannot be collected.

```bash
jcmd <pid> GC.heap_dump /tmp/heapdump_$(date +%s).hprof
```

Expected output: A `.hprof` file approximately equal to used heap size. Generation takes roughly 2 seconds per GB of used heap, during which the JVM is paused. If the JVM is unresponsive, capture the dump before killing — it is the most valuable diagnostic artifact.

### Step 5

Analyze the heap dump with Eclipse MAT or JDK Mission Control to identify the largest retained objects and reference chains preventing garbage collection.

```bash
jcmd <pid> GC.class_histogram filename=/tmp/histogram.txt
```

In Eclipse MAT: open the `.hprof` file, run Leak Suspects Report, then examine the Dominator Tree for objects retaining the most heap by retained size.

Expected output: The class histogram shows instance counts and byte totals per class. The Leak Suspects Report identifies 1–3 objects holding disproportionate heap. Right-click → Path to GC Roots → exclude weak references to trace back to the holding GC root (static field = cache, Thread = ThreadLocal, ClassLoader = classloader leak).

### Step 6

Check class loading statistics when the OOM variant is `Metaspace` or `Compressed class space`.

```bash
jcmd <pid> VM.classloader_stats
jstat -gcmetacapacity <pid> 5000 10
```

Expected output: `VM.classloader_stats` prints classloader hierarchy with class counts. A growing class count over successive polls indicates a classloader leak. `gcmetacapacity` shows Metaspace used, committed, and max — Metaspace near `MaxMetaspaceSize` confirms exhaustion.

### Step 7

Check thread count when the OOM variant is `unable to create new native thread`.

```bash
jcmd <pid> Thread.print | grep -c "tid="
cat /proc/<pid>/status | grep Threads
ulimit -u
cat /proc/sys/kernel/threads-max
```

Expected output: Thread count above 5,000 is abnormal for most applications. Each thread allocates 512 KB–1 MB of native stack. If `ulimit -u` or `threads-max` is lower than the thread count, the OS refuses to create more threads.

### Step 8

Use Native Memory Tracking (NMT) to diagnose native heap growth when Java-level OOM is absent but process RSS grows continuously. Requires JVM to be started with NMT enabled.

```bash
jcmd <pid> VM.native_memory summary.diff
```

Expected output: NMT diff shows reserved and committed delta per category (Java Heap, Class, Thread, Code). Growing `Class` or `Thread` committed memory indicates classloader or thread leaks respectively.

## Causes

### Cause A: Unbounded in-memory cache or collection

**Statement:** A `HashMap`, `ConcurrentHashMap`, `ArrayList`, or equivalent collection reachable from a static field or long-lived scope grows without eviction until it exhausts heap.

**Mechanism:** Static or application-scoped fields hold strong references to all entries, preventing GC from reclaiming them. Each new entry increases the retained set, and because no eviction or expiry policy exists, the old generation fills continuously across GC cycles. Full GC cannot reclaim these live objects, and `Java heap space` or `GC overhead limit exceeded` is thrown.

**Indicator:**

- [Step 1] Log shows `Java heap space` or `GC overhead limit exceeded`
- [Step 3] Old generation (`O`) climbs to 90%+ and does not drop after full GC
- [Step 5] Dominator Tree shows `HashMap$Node[]` or `ArrayList` reachable from a static field retaining the majority of heap

<!-- match: {"step": 1, "predicate": "contains", "target": "Java heap space"} -->
<!-- match: {"step": 3, "predicate": "threshold", "target": "O", "op": ">", "value": 90} -->

**Mitigation:**

- **Risk:** Restarting clears the cache, causing a cold-start load spike on downstream services.
- **Command:**

  ```bash
  sudo systemctl restart myapp
  # Kubernetes:
  kubectl rollout restart deployment/<name> -n <namespace>
  ```

- **Duration:** 1–5 minutes. Monitor heap post-restart.

**Resolution:**

```bash
# Replace unbounded Map with a Caffeine bounded cache in application code:
# Cache<Key, Value> cache = Caffeine.newBuilder()
#     .maximumSize(10_000)
#     .expireAfterWrite(Duration.ofMinutes(30))
#     .build();
# Redeploy after code change.
```

- **Impact:** Cluster-wide. Requires application redeployment.
- **Rollback:** Revert to previous deployment version: `kubectl rollout undo deployment/<name> -n <namespace>`

**Verification:** After fix, run `jstat -gcutil <pid> 10000` for 1 hour. Old generation (`O`) must stabilize below 80% and not trend upward under stable load. `FGC` count increments no more than once per 10 minutes.

### Cause B: Database queries returning unbounded result sets

**Statement:** A query fetches all rows from a large table without pagination, loading the full result set into heap as entity objects.

**Mechanism:** Methods such as `findAll()` or equivalent in Spring Data load every matching row into memory as a list of entity objects. Under normal load this may be tolerable, but as the table grows the allocation spike eventually exhausts old generation. GC cannot reclaim these short-lived but massive allocations fast enough, triggering `Java heap space` or GC overhead errors.

**Indicator:**

- [Step 1] OOM coincides with specific API endpoints (visible in access logs) or scheduled jobs
- [Step 5] Heap histogram shows large counts of JPA entity classes (`com.example.*Entity`) or `Object[]` arrays
- [Step 4] Heap dump Leak Suspects path traces through `List` to ORM-managed collections

<!-- match: {"step": 1, "predicate": "contains", "target": "Java heap space"} -->

**Mitigation:**

- **Risk:** Restart clears allocated objects immediately; data is not lost.
- **Command:**

  ```bash
  kubectl delete pod <pod-name> -n <namespace>
  ```

- **Duration:** 30 seconds for pod replacement.

**Resolution:**

```bash
# Implement pagination in Spring Data (keyset or offset):
# Page<Entity> findAll(Pageable pageable);
# repository.findAll(PageRequest.of(0, 500));
# Or use streaming: Stream<Entity> streamAll();
# Redeploy after code change.
```

**Verification:** Monitor heap with `jcmd <pid> GC.heap_info` before and after the endpoint call. Used heap should return to pre-call baseline after GC, not grow each call.

### Cause C: ThreadLocal values not cleaned in thread pools

**Statement:** `ThreadLocal` values set during request processing are never removed, causing each pooled thread to accumulate references to request objects across its entire lifetime.

**Mechanism:** Thread pools reuse threads across many requests. A `ThreadLocal` bound to a thread-pool thread persists until `ThreadLocal.remove()` is called. Without explicit cleanup, every processed request adds to the retention graph of that thread, and because all threads in the pool are always alive (GC roots), their `ThreadLocal` maps are never collectible. The aggregate grows proportionally to thread count times request object size.

**Indicator:**

- [Step 7] Thread count is stable (not exploding) but heap grows proportionally to request volume
- [Step 5] Path to GC Roots for retained objects ends at `Thread` → `ThreadLocalMap` → `Entry`
- [Step 3] Old generation grows gradually under load and does not return to baseline after GC

**Mitigation:**

- **Risk:** Low — restarting the application clears all thread state.
- **Command:**

  ```bash
  kubectl rollout restart deployment/<name> -n <namespace>
  ```

- **Duration:** 2–5 minutes.

**Resolution:**

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

### Cause D: Classloader leak in hot-redeployment environment

**Statement:** Old classloaders from previous deployments are not garbage collected because a framework, library, or static field holds a reference to a class loaded by the old classloader.

**Mechanism:** Each application deployment creates a new classloader. When undeployment occurs (Tomcat, JBoss, OSGi), the old classloader should be dereferenced and GC'd. If any surviving object holds a reference to a class loaded by the old classloader — via static fields, thread contexts, JDBC drivers, or logging frameworks — the entire old classloader graph (all its loaded classes and their static state) is retained in Metaspace. Repeated hot-redeploys accumulate classloaders until Metaspace is exhausted.

**Indicator:**

- [Step 1] OOM variant is `Metaspace`
- [Step 6] `VM.classloader_stats` shows growing number of classloaders with each redeployment
- [Step 6] `jstat -gcmetacapacity` shows Metaspace committed approaching `MaxMetaspaceSize`

<!-- match: {"step": 1, "predicate": "contains", "target": "Metaspace"} -->
<!-- match: {"step": 6, "predicate": "threshold", "target": "M", "op": ">", "value": 90} -->

**Mitigation:**

- **Risk:** Full JVM restart required; brief service interruption.
- **Command:**

  ```bash
  jcmd <pid> VM.classloader_stats > /tmp/classloader_before.txt
  sudo systemctl restart myapp
  ```

- **Duration:** Full restart, 1–5 minutes.

**Resolution:**

```bash
# Set a hard Metaspace cap to make leaks fail faster:
# Add to JVM flags: -XX:MaxMetaspaceSize=512m
# Investigate and fix the classloader reference:
#   - Deregister JDBC drivers in ServletContextListener.contextDestroyed()
#   - Flush ThreadLocal values referencing application classes
#   - Use jmap -clstats to identify which classloader holds the most classes
```

**Verification:** After fix and redeployment, `jstat -gcmetacapacity <pid> 5000` shows Metaspace committed stable across multiple deployments, not growing with each cycle.

### Cause E: Container memory limit below JVM total memory footprint

**Statement:** The Kubernetes container memory limit is set at or below the JVM's total memory usage (heap + Metaspace + thread stacks + direct buffers + JIT code cache), so the Linux OOM killer terminates the JVM before any Java-level OutOfMemoryError is thrown.

**Mechanism:** The JVM's `-Xmx` controls only heap. Non-heap regions (Metaspace, thread stacks at ~1 MB each, JIT code cache up to 240 MB, direct buffers) add 300 MB–1 GB on top of heap. If the container memory limit equals `-Xmx`, the total JVM RSS exceeds the limit under normal operation, and the cgroup OOM killer sends SIGKILL (exit code 137). No Java OOM log entry is produced.

**Indicator:**

- [Step 1] `kubectl describe pod` shows `Last State: Terminated, Reason: OOMKilled, Exit Code: 137`
- [Step 1] No `OutOfMemoryError` in application logs
- [Step 2] `-Xmx` is close to the container memory limit (within 20%)

<!-- match: {"step": 1, "predicate": "contains", "target": "OOMKilled"} -->
<!-- match: {"step": 1, "predicate": "exit_code", "target": 137} -->

**Mitigation:**

- **Risk:** Increasing container memory limit may affect cluster scheduling if node capacity is tight.
- **Command:**

  ```bash
  kubectl patch deployment <name> -n <namespace> --type='json' \
    -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"<NEW_LIMIT>"}]'
  ```

- **Duration:** Rolling restart, 2–5 minutes.

**Resolution:**

```bash
# Use percentage-based heap sizing and set container limit to 1.3x heap:
# JVM flag: -XX:MaxRAMPercentage=75.0
# This automatically sets Xmx to 75% of container memory,
# leaving 25% for non-heap regions.
# Set container limit: limits.memory = desired_heap / 0.75
```

- **Impact:** Cluster-wide for the affected deployment. Requires rolling restart.
- **Rollback:** `kubectl rollout undo deployment/<name> -n <namespace>`

**Verification:** After fix, `kubectl get pods -n <namespace>` shows 0 restarts over 24 hours. `jcmd <pid> GC.heap_info` shows JVM healthy. `kubectl top pod <pod-name>` shows RSS well below container limit.

### Cause F: NIO direct buffer exhaustion

**Statement:** Off-heap `ByteBuffer.allocateDirect()` buffers accumulate because they are not explicitly freed and GC does not collect them promptly, exhausting the direct buffer pool bounded by `-XX:MaxDirectMemorySize`.

**Mechanism:** Direct buffers reside in native (off-heap) memory managed by `java.nio.DirectByteBuffer`. They are freed when the `DirectByteBuffer` Java object is GC'd, but full GC may not run frequently enough in heap-light applications, allowing direct buffer usage to grow. Netty and NIO-based frameworks allocate large direct buffer pools that can exhaust the limit even when heap is healthy.

**Indicator:**

- [Step 1] OOM variant is `Direct buffer memory`
- [Step 2] Heap usage is low (below 50%) while the OOM occurs
- [Step 8] NMT shows growing native committed memory not accounted for by heap, class, or thread categories

<!-- match: {"step": 1, "predicate": "contains", "target": "Direct buffer memory"} -->

**Mitigation:**

- **Risk:** Low — triggering GC forces collection of phantom-reachable direct buffer objects.
- **Command:**

  ```bash
  jcmd <pid> GC.run
  ```

- **Duration:** GC pause 0.5–10 seconds.

**Resolution:**

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

### Cause Z: Unidentified

**Statement:** The JVM OutOfMemoryError cannot be attributed to any of the documented causes with available diagnostic data.

**Mechanism:** [Default]

**Indicator:**

- [Default] None of the documented cause indicators match the collected diagnostics

**Mitigation:**

- **Risk:** Low — capture diagnostics before restarting to preserve evidence.
- **Command:**

  ```bash
  jcmd <pid> GC.heap_dump /tmp/heapdump_$(date +%s).hprof
  jcmd <pid> Thread.print > /tmp/threaddump_$(date +%s).txt
  jcmd <pid> VM.native_memory > /tmp/nmt_$(date +%s).txt
  kill -9 <pid>
  sudo systemctl start myapp
  ```

- **Duration:** 5–15 minutes for artifact capture and restart.

**Resolution:** Out of runbook scope. Escalate to JVM specialist with heap dump, thread dump, NMT output, and full GC log (`-Xlog:gc*:file=/tmp/gc.log:time,uptime,level,tags:filecount=5,filesize=50m`).

**Verification:** Application restarts and responds to health checks: `curl -s http://localhost:8080/health` returns HTTP 200.

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
