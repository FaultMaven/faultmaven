---
id: java-jvm-oom
title: "Java JVM OutOfMemoryError — Diagnosis and Resolution"
domain: application
service: java
symptom_class:
  - oom
severity: critical
scope: global
version: "2.1.0"
last_updated: "2026-03-26"
verified_by: kb-researcher
status: draft
tags:
  - java
  - jvm
  - oom
  - heap
  - memory-leak
  - garbage-collection
difficulty: advanced
---

# Java JVM OutOfMemoryError

## Problem Definition

Applies to Java 11+ (HotSpot JVM) running on Linux. Requires access to the JVM process (via `jcmd`, `jmap`, `jstat`) and application logs. For Kubernetes deployments, `kubectl exec` access to the pod is needed. Eclipse MAT or VisualVM is required for heap dump analysis.

A `java.lang.OutOfMemoryError` occurs when the JVM cannot allocate an object because a memory region is exhausted and garbage collection cannot reclaim sufficient space. Six distinct variants exist, each pointing to a different memory region: `Java heap space` (heap full), `GC overhead limit exceeded` (98%+ time in GC with less than 2% reclaimed), `Metaspace` (class metadata exhaustion from classloader leaks or heavy reflection), `Direct buffer memory` (off-heap NIO buffers exceeding `-XX:MaxDirectMemorySize`), `unable to create new native thread` (OS thread limit reached), and `Compressed class space` (compressed class pointer space exhausted). In Kubernetes, the JVM process may be killed with exit code 137 (OOMKilled) if the container memory limit is exceeded before the JVM's own `-Xmx` is reached, producing no Java-level error message. Common root causes include unbounded in-memory caches, database queries returning full result sets without pagination, ThreadLocal values not cleaned in thread pools, classloader leaks during hot-redeployment, and heap sizing that does not account for non-heap memory regions.

## Diagnostic Steps

### 1. Identify the OOM error variant from logs

Determines which JVM memory region is exhausted, directing all subsequent investigation.

```bash
grep -i "OutOfMemoryError" /var/log/app/application.log | tail -5
```

For Kubernetes:

```bash
kubectl logs <pod-name> -n <namespace> --previous | grep -i "OutOfMemoryError"
```

**Expected output:** One of: `Java heap space`, `GC overhead limit exceeded`, `Metaspace`, `Direct buffer memory`, `unable to create new native thread`, or `Compressed class space`.

**What this means:** Each variant requires a different investigation path. `Java heap space` and `GC overhead limit exceeded` are the most common (90%+ of cases) and point to heap-level issues. If no OOM message exists but the pod shows `OOMKilled`, the container exceeded its memory limit — check `kubectl describe pod` for the `Last State: Terminated, Reason: OOMKilled` entry.

### 2. Check JVM memory configuration and current usage

Compares configured maximums against actual utilization to determine if the heap is undersized or genuinely leaking.

```bash
jps -v | grep -v Jps
jcmd <pid> VM.flags | grep -E "Xmx|Xms|MaxMetaspaceSize|MaxDirectMemorySize|MaxRAMPercentage"
jcmd <pid> GC.heap_info
```

**Expected output:** `GC.heap_info` shows used heap, committed heap, and max heap. A healthy application uses 40-70% of max heap at steady state.

**What this means:** If used heap is above 85% of max, the application is close to OOM. If `-Xmx` is small relative to the workload (e.g., 256m for a Spring Boot application), the heap may simply be undersized. If `-Xmx` is generous (4g+) and heap is still full, a memory leak is likely.

### 3. Monitor GC behavior in real time

Reveals whether GC is keeping up with allocation or falling behind, and whether Full GC is reclaiming meaningful space.

```bash
jstat -gcutil <pid> 5000 20
```

**Expected output:** Columns include S0, S1 (survivor spaces), E (Eden), O (Old generation), M (Metaspace), GCT (GC time). A healthy JVM shows O below 70%, FGC (Full GC count) incrementing slowly, and GCT as a small fraction of uptime.

**What this means:** O consistently above 90% means old generation is nearly full. FGC incrementing every few seconds means the JVM is thrashing in Full GC. If Old generation does not drop after Full GC, those objects are all live — either a leak or undersized heap. GCT exceeding 5% of uptime indicates GC overhead is degrading application throughput.

### 4. Capture a heap dump for offline analysis

Produces a snapshot of all live objects and their references for identifying what is consuming memory and why it cannot be collected.

```bash
jcmd <pid> GC.heap_dump /tmp/heapdump.hprof
```

Or using jmap (older method):

```bash
jmap -dump:live,format=b,file=/tmp/heapdump.hprof <pid>
```

**Expected output:** A `.hprof` file roughly the size of used heap (generating takes approximately 2 seconds per GB of used heap, during which the JVM is paused).

**What this means:** The heap dump must be analyzed with Eclipse MAT or VisualVM. If the application is already unresponsive due to GC thrashing, capture the dump quickly before killing the process — this is the most valuable diagnostic artifact.

### 5. Analyze the heap dump with Eclipse MAT

Identifies the objects retaining the most memory and the reference chains preventing garbage collection.

Open the `.hprof` file in Eclipse MAT and run:
- **Leak Suspects Report** (automatic) — identifies the largest memory consumers with reference chains
- **Dominator Tree** — shows which objects "dominate" (retain) the most heap by retained size
- **Histogram** — lists object counts and shallow/retained sizes by class

**Expected output:** The Leak Suspects report highlights 1-3 objects retaining disproportionate heap. Common findings include `HashMap$Node[]` or `ArrayList` reachable from a static field, large `byte[]` from cached responses, or thousands of identical `String` objects.

**What this means:** Trace from the suspect object back to its GC root (right-click > Path to GC Roots > exclude weak references). The GC root reveals what is holding the reference: a static field means unbounded cache, a Thread means ThreadLocal leak, a ClassLoader means classloader leak.

### 6. Check class loading for Metaspace OOM

Identifies classloader leaks where classes are loaded repeatedly but never unloaded.

```bash
jcmd <pid> VM.classloader_stats
jstat -gcmetacapacity <pid> 5000 10
```

**Expected output:** `VM.classloader_stats` shows classloader hierarchy with class counts. A healthy application has a stable number of loaded classes. `gcmetacapacity` shows Metaspace used, committed, and max.

**What this means:** If class count grows continuously over time, a classloader leak is occurring. This is common in application servers with hot-redeployment (Tomcat, JBoss) where old classloaders are not garbage collected because something holds a reference to a loaded class.

### 7. Check thread count for native thread OOM

Determines whether the OS thread limit has been reached.

```bash
jcmd <pid> Thread.print | grep -c "tid="
cat /proc/<pid>/status | grep Threads
ulimit -u
cat /proc/sys/kernel/threads-max
```

**Expected output:** Thread count in the hundreds is typical. Thread count in the thousands (5,000+) is abnormal for most applications.

**What this means:** Each thread allocates a stack (default 512KB-1MB). 5,000 threads with 1MB stacks consume 5GB of native memory. If `ulimit -u` or `threads-max` is lower than the thread count, the OS refuses to create more. The fix is to reduce thread creation (use thread pools) or increase OS limits.

## Mitigation

### Option 1: Restart the application

**Risk:** Low. Brief service interruption. Use with a load balancer to drain connections first.

**Command:**

```bash
sudo systemctl restart myapp
# Or for Kubernetes:
kubectl delete pod <pod-name> -n <namespace>
```

**Verify:** `curl -s http://localhost:8080/health` returns 200. `jcmd <pid> GC.heap_info` shows heap usage at baseline.

**Duration:** 30 seconds to 5 minutes depending on application startup time.

### Option 2: Increase heap size as a temporary measure

**Risk:** Medium. May mask a memory leak, allowing it to grow larger before the next OOM. Ensure the host or container has sufficient physical memory.

**Command:**

```bash
export JAVA_OPTS="-Xms2g -Xmx4g -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tmp"
```

For Kubernetes, also increase the container memory limit (set container limit to 1.3x the -Xmx value to account for non-heap memory):

```bash
kubectl patch deployment <name> -n <namespace> --type='json' \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"5Gi"}]'
```

**Verify:** `jcmd <pid> VM.flags | grep Xmx` shows the new max. `jstat -gcutil <pid> 5000` shows O (Old gen) below 80%.

**Duration:** Restart time (1-5 minutes).

### Option 3: Trigger manual GC to reclaim memory temporarily

**Risk:** Low but causes a stop-the-world pause proportional to heap size (0.5-10 seconds).

**Command:**

```bash
jcmd <pid> GC.run
```

**Verify:** `jstat -gcutil <pid> 1000` shows O (Old gen) dropping. If it does not drop significantly, all objects are live — this confirms a leak or undersized heap.

**Duration:** GC pause typically 0.5-10 seconds.

### Option 4: Capture diagnostics and kill an unresponsive JVM

**Risk:** Medium. Service is already degraded; killing formalizes the outage but preserves the most valuable diagnostic data.

**Command:**

```bash
jcmd <pid> GC.heap_dump /tmp/heapdump_$(date +%s).hprof
jcmd <pid> Thread.print > /tmp/threaddump_$(date +%s).txt
kill -9 <pid>
sudo systemctl start myapp
```

**Verify:** `ls -lh /tmp/heapdump_*.hprof` shows the dump file. Application restarts and responds to health checks.

**Duration:** Heap dump capture takes approximately 2 seconds per GB. Restart adds 1-5 minutes.

## Root Cause Resolution

**If** heap dump shows a growing unbounded collection (HashMap, ArrayList, ConcurrentHashMap) reachable from a static field → replace with a bounded cache (Caffeine, Guava) with `maximumSize` and `expireAfterWrite`. For static Maps, add eviction logic or switch to `WeakHashMap` if entries should be collected when keys are unreachable.

**If** heap dump shows large numbers of entity objects from database queries → implement pagination (`LIMIT`/`OFFSET` or keyset pagination) or streaming (`Stream<T>` in Spring Data). Never call `findAll()` on tables with unbounded row counts.

**If** ThreadLocal values accumulate in thread pools → ensure `ThreadLocal.remove()` is called in a `finally` block after each task. In servlet containers, use `ServletRequestListener` or filter cleanup. In Spring, use request-scoped beans instead of ThreadLocal.

**If** Old generation does not drop after Full GC and no leak is found in heap dump → the heap is undersized for the workload. Set `-Xmx` to 70-80% of container memory, or use `-XX:MaxRAMPercentage=75.0` for automatic sizing. Reserve 25% for non-heap (Metaspace, thread stacks, direct buffers, JIT code cache).

**If** the OOM is `Metaspace` and loaded class count grows continuously → investigate classloader leaks. In application servers with hot-redeployment, ensure old classloaders are fully dereferenced. Set `-XX:MaxMetaspaceSize=512m` to bound Metaspace and make leaks fail faster. Use `jcmd <pid> VM.classloader_stats` to identify which classloader holds the most classes.

**If** the OOM is `Direct buffer memory` → increase `-XX:MaxDirectMemorySize` and investigate NIO buffer leaks. For Netty applications, enable leak detection with `-Dio.netty.leakDetection.level=PARANOID`. Ensure `ByteBuffer.allocateDirect()` buffers are explicitly freed or go out of scope.

**If** the OOM is `unable to create new native thread` → reduce thread count by using bounded thread pools (`Executors.newFixedThreadPool()`) instead of unbounded thread creation. If the application legitimately needs many threads, increase OS limits (`ulimit -u`, `/proc/sys/kernel/threads-max`) and reduce per-thread stack size with `-Xss256k`.

## Verification

1. **Monitor heap usage for at least 1 hour after the fix:**

```bash
jstat -gcutil <pid> 10000
```

Old generation (O) should stabilize below 80% and not trend upward continuously. FGC count should increment slowly (less than once per 10 minutes).

2. **Confirm no OOM errors in logs:**

```bash
tail -f /var/log/app/application.log | grep -i "OutOfMemoryError"
```

No new OOM errors should appear. For Kubernetes, verify `kubectl get pods` shows no recent restarts.

3. **Verify GC health metrics:**

```bash
jstat -gcutil <pid> 5000
```

GC time (GCT) should be less than 5% of uptime. Full GC pause duration (visible in GC logs) should be under 1 second for G1GC.

4. **Run a load test and verify memory returns to baseline:**

```bash
jcmd <pid> GC.heap_info  # before load
# Run load test (k6, JMeter, wrk)
jcmd <pid> GC.run
jcmd <pid> GC.heap_info  # after load + GC
```

Used heap should return to near pre-test levels. If it does not, a leak may still exist under load.

## Prevention

- **Always enable `-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/var/dumps/`** in all environments. Add `-XX:+ExitOnOutOfMemoryError` in containerized deployments so the orchestrator restarts the process immediately instead of leaving it degraded.
- **Use percentage-based heap sizing in containers** with `-XX:MaxRAMPercentage=75.0` instead of hardcoded `-Xmx` values. This automatically adapts when container memory limits change.
- **Enable GC logging in production** with `-Xlog:gc*:file=/var/log/app/gc.log:time,uptime,level,tags:filecount=5,filesize=50m`. GC logs have negligible performance impact and are essential for post-mortem analysis.
- **Alert on heap usage before OOM occurs.** Use Micrometer/Prometheus JVM metrics and alert when `jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"} > 0.85` for 10 minutes.
- **Use bounded caches exclusively.** Never use raw `HashMap` or `ConcurrentHashMap` as caches. Use Caffeine or Guava with `maximumSize` and `expireAfterWrite` configured.
- **Paginate all database queries.** Never use `findAll()` on unbounded tables. Implement keyset pagination for large result sets.
- **Clean up ThreadLocal values in thread pools.** Always call `remove()` in a `finally` block. Consider request-scoped dependency injection instead of ThreadLocal.
- **Choose an appropriate GC algorithm.** G1GC (default Java 11+) handles most workloads. ZGC (Java 15+) provides sub-millisecond pause times for latency-sensitive applications with large heaps.

## Sources

- [Oracle — Troubleshoot Memory Leaks (Java 25)](https://docs.oracle.com/en/java/javase/25/troubleshoot/troubleshooting-memory-leaks.html) — Official JVM memory leak diagnosis including jcmd, jmap, jstat, heap dump analysis, NMT, and JFR procedures
- [Oracle — Understand the OutOfMemoryError Exception](https://docs.oracle.com/javase/8/docs/technotes/guides/troubleshoot/memleaks002.html) — Detailed explanation of each OOM variant, causes, and JVM flags
- [Eclipse Memory Analyzer (MAT)](https://eclipse.dev/mat/) — Heap dump analysis tool for identifying memory leaks via Leak Suspects, Dominator Tree, and Histogram views
- [HeapHero — JVM Memory Model Deep Dive](https://blog.heaphero.io/a-deep-dive-into-the-jvm-memory-model-how-heap-stack-and-metaspace-function-and-fail/) — Heap, Stack, and Metaspace architecture with failure mode analysis
- [HeapHero — Types of OutOfMemoryError](https://blog.heaphero.io/types-of-outofmemoryerror/) — All nine OOM variants with causes and solutions
- [GetYourGuide — Debugging JVM OutOfMemoryErrors](https://www.getyourguide.careers/posts/debugging-jvm-outofmemoryerrors-a-step-by-step-guide) — Production step-by-step OOM debugging guide
