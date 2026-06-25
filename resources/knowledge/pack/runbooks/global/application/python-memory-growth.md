---
id: "python-memory-growth"
title: "Python process memory growth (leaks, reference cycles, unbounded caches)"
domain: application
service: python
symptom_class: [oom]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [oom, memory-leak, tracemalloc, reference-cycle, copy-on-write]
difficulty: advanced
---

## Symptom Recognition

- Process RSS climbs monotonically over hours/days and never plateaus; `RES` in `top`/`htop` only grows.
- Linux OOM killer kills the process: `dmesg` shows `Out of memory: Killed process <pid> (python)` / `oom-kill:...`.
- Containerized: pod restarts with `OOMKilled` (exit code 137); cgroup `memory.current` rises toward `memory.max`.
- Application raises `MemoryError`, or allocation latency spikes as the allocator thrashes.
- After a `fork()` (e.g. Gunicorn/uWSGI prefork, `multiprocessing`), total memory of all workers grows far faster than expected.

## Applicability

- CPython 3.7+ (`gc.freeze()` requires 3.7+; `tracemalloc` requires 3.4+). Examples assume 3.11+.
- Access: ability to run the target process with extra env vars / startup flags, or to attach via `py-spy`/`gdb` for live processes.
- Tools: `tracemalloc` and `gc` (stdlib); `objgraph` (`pip install objgraph`); `py-spy` (`pip install py-spy`); OS tools `top`/`htop`, `ps`, `dmesg`, `pmap`.

## Diagnostic Steps

### Step 1: Confirm growth is in the Python heap (not native) and read OS-level totals

```bash
# Watch RSS over time for the target pid
ps -o pid,rss,vsz,cmd -p "$PID"
# Per-mapping breakdown: distinguishes Python arenas from native libs (e.g. libssl, numpy MKL)
pmap -x "$PID" | sort -k3 -n -r | head -20
# Confirm an OOM kill happened, if any
dmesg -T | grep -iE "oom|killed process" | tail -5
```

Expected output: `RSS` (KB) rising across repeated runs; `pmap` shows whether growth is in `[anon]`/`[heap]` (Python objects) or in a named native library mapping.

### Step 2: Diff two tracemalloc snapshots to find the allocating line

```bash
PYTHONTRACEMALLOC=25 python - <<'PY'
import tracemalloc, gc
from yourapp import run_one_cycle  # replace with one unit of repeated work
tracemalloc.start(25)
run_one_cycle()                      # warm up
snap1 = tracemalloc.take_snapshot()
for _ in range(100):                 # repeat the suspected leaking work
    run_one_cycle()
gc.collect()
snap2 = tracemalloc.take_snapshot()
top = snap2.compare_to(snap1, 'lineno')
print("[ Top 10 growth ]")
for stat in top[:10]:
    print(stat)
cur, peak = tracemalloc.get_traced_memory()
print(f"traced current={cur/1e6:.1f}MB peak={peak/1e6:.1f}MB")
# Full traceback of the single biggest block:
for line in snap2.statistics('traceback')[0].traceback.format():
    print(line)
PY
```

Expected output: a ranked list like `app/cache.py:42: size=120 MiB (+118 MiB), count=50000 (+49900)`. A line whose `size`/`count` grows roughly linearly with the loop count is the leak site.

### Step 3: Measure the GC and look for uncollectable cycles

```bash
python - <<'PY'
import gc
from yourapp import run_one_cycle
gc.set_debug(gc.DEBUG_LEAK)   # = DEBUG_COLLECTABLE|DEBUG_UNCOLLECTABLE|DEBUG_SAVEALL
for _ in range(50):
    run_one_cycle()
print("gen counts:", gc.get_count())
print("collected unreachable:", gc.collect())
print("gc.garbage (uncollectable):", len(gc.garbage))
for obj in gc.garbage[:5]:
    print("  ", type(obj), id(obj))
for s in gc.get_stats():
    print(s)
PY
```

Expected output: `gc.collect()` returns a large nonzero count that grows each cycle (collectable cycles), and/or `gc.garbage` is non-empty (truly uncollectable). `DEBUG_LEAK` prints `gc: collectable <object ...>` lines for each cycle member.

### Step 4: Count live objects by type and trace what holds them

```bash
python - <<'PY'
import gc, objgraph
from yourapp import run_one_cycle
for _ in range(100):
    run_one_cycle()
gc.collect()
objgraph.show_growth(limit=15)          # types whose instance count grew most
print("tracked objects:", len(gc.get_objects()))
# Pick the suspect type, find one instance, show what refers to it:
import yourapp
objs = objgraph.by_type('YourLeakyClass')
if objs:
    objgraph.show_backrefs(objs[:1], max_depth=5, filename='/tmp/backrefs.png')
    print("referrers:", [type(r) for r in gc.get_referrers(objs[0])][:10])
PY
```

Expected output: `show_growth` lists a class with a steadily increasing count (e.g. `YourLeakyClass  50000  +50000`); `gc.get_referrers` / the backref graph reveals the container (a module-level `dict`/`list`, a registry, or a closure cell) still holding every instance.

## Causes

### Cause A: Module-global or class-level container accumulates entries without bound
**Statement:** A long-lived module-global or class-attribute container (a `list`, `dict`, or registry that is never trimmed) keeps appending one entry per request/iteration, so every object created on the hot path stays reachable for the life of the process.
**Chain:**
- root: a process-lifetime container is appended to on every iteration and never evicted
- s1: every per-iteration object remains strongly reachable from that container
- s2: live Python-heap object count and RSS rise linearly with workload
- D: RSS exceeds the cgroup/OS limit and the process is OOM-killed
**Indicators:**
- root: [Step 4] `objgraph.show_growth` shows one type growing linearly and `gc.get_referrers` points at a module-level/class-level `dict`/`list`
  <!-- match: {"step": 4, "predicate": "contains", "target": "+"} -->
- s1: [Step 2] tracemalloc diff blames a single line that appends to that container, with `count` proportional to iterations
  <!-- match: {"step": 2, "predicate": "contains", "target": "size="} -->
- s2: [Step 1] RSS in `ps` rises monotonically across runs with no plateau
- D: [Symptom] `dmesg` shows `Out of memory: Killed process` for the python pid
**Interventions:**
- **remediation** (root): Replace the unbounded container with a bounded cache and explicit eviction; cap size at the working-set, not the lifetime, of the workload.

  ```python
  from functools import lru_cache
  # for a manual dict cache, switch to a bounded structure:
  from cachetools import LRUCache
  CACHE = LRUCache(maxsize=10_000)     # evicts least-recently-used past maxsize
  # for decorated functions:
  @lru_cache(maxsize=1024)             # NOT maxsize=None (unbounded)
  def expensive(key): ...
  ```

  **Verification:** Re-run Step 2; the previously-growing line's `size`/`count` now plateaus instead of scaling with loop count.
- **mitigation** (s1): Periodically clear the container on a size/time threshold until the bounded structure ships.

  ```python
  if len(CACHE) > 50_000:
      CACHE.clear()        # or .cache_clear() for an lru_cache-decorated fn
  ```

  **Risk:** A full clear causes a cold-cache latency spike and can re-fetch hot keys. **Duration:** Hours, until the bounded cache is deployed. **Verification:** Step 1 RSS sawtooths (drops on each clear) instead of climbing monotonically.

### Cause B: Reference cycle containing an object with a finalizer is collectable but never collected promptly (or is uncollectable on older runtimes)
**Statement:** Objects are linked in reference cycles (e.g. parent↔child back-references, or a cycle captured by a `__del__`/finalizer), so their refcounts never reach zero and they accumulate between generational GC passes — and on legacy runtimes the cycle is placed in `gc.garbage` and never freed.
**Chain:**
- root: code creates reference cycles among objects the cyclic GC must reclaim
- s1: refcounting alone cannot free the cycle members; they survive until a gen-2 GC pass
- s2: cycle members accumulate faster than collection runs (or land in `gc.garbage`), growing the heap
- D: RSS exceeds the limit and the process is OOM-killed
**Indicators:**
- root: [Step 3] `gc.collect()` returns a large, growing count of unreachable objects and/or `len(gc.garbage) > 0`
  <!-- match: {"step": 3, "predicate": "contains", "target": "gc.garbage (uncollectable):"} -->
- s1: [Step 3] `DEBUG_LEAK` prints `gc: collectable <...>` lines for the cycle members each cycle
  <!-- match: {"step": 3, "predicate": "contains", "target": "collectable"} -->
- s2: [Step 4] `gc.get_objects()` length and the suspect type's count keep rising despite an explicit `gc.collect()`
- D: [Symptom] pod restarts `OOMKilled` (exit 137)
**Interventions:**
- **remediation** (root): Break the cycle at the source — use `weakref.ref`/`weakref.WeakValueDictionary` for back-references, or null out the link in a `try/finally`/context manager so no cycle forms.

  ```python
  import weakref
  class Child:
      def __init__(self, parent):
          self._parent = weakref.ref(parent)   # not a strong back-reference
  ```

  **Verification:** Re-run Step 3; `gc.collect()` returns ~0 unreachable per cycle and `gc.garbage` stays empty.
- **defensive_fix** (s1): Force a full collection at a safe checkpoint to reclaim cycles before they pile up.

  ```python
  import gc
  gc.collect()   # full collection across all generations at end of each request batch
  ```

  **Verification:** Step 1 RSS plateaus or sawtooths at the checkpoint cadence rather than climbing continuously.

### Cause C: A C-extension / native allocation leaks memory invisible to the Python GC
**Statement:** A C extension or `ctypes`/`cffi` binding (or a native library such as an image, crypto, or BLAS backend) allocates off the Python heap and fails to free it, so RSS grows even though Python-object counts and tracemalloc totals stay flat.
**Chain:**
- root: a native/C-extension code path allocates memory it never frees (missing `free`/`Py_DECREF`)
- s1: the bytes live outside the Python heap, so `gc` and `tracemalloc` do not see or reclaim them
- s2: process RSS rises while Python-tracked memory stays flat
- D: RSS exceeds the limit and the process is OOM-killed
**Indicators:**
- root: [Step 1] `pmap -x` shows growth concentrated in a named native library mapping or large `[anon]` regions, not Python arenas
  <!-- match: {"step": 1, "predicate": "contains", "target": "anon"} -->
- s1: [Step 2] `tracemalloc.get_traced_memory()` current stays roughly flat while OS RSS climbs (the gap is native)
- s2: [Step 4] `len(gc.get_objects())` and `objgraph.show_growth` are flat despite rising RSS
- D: [Symptom] application raises `MemoryError` or the container is `OOMKilled`
**Interventions:**
- **remediation** (root): Upgrade/patch the offending native library to a version with the leak fixed, or fix the binding to release every native handle (call the library's `free`/`close`/`del` in a `finally`).

  ```bash
  pip install --upgrade <leaky-package>     # pull in the upstream leak fix
  # verify which native libs are mapped and their versions
  pmap -x "$PID" | grep -iE '\.so'
  ```

  **Verification:** Re-run Step 1; the native mapping's size stops growing across repeated workloads.
- **mitigation** (s1): Recycle workers after a bounded number of requests so native leaks are reclaimed by process exit (e.g. Gunicorn `--max-requests`).

  ```bash
  gunicorn app:app --workers 4 --max-requests 1000 --max-requests-jitter 100
  ```

  **Risk:** Periodic worker restarts drop in-flight in-memory state and add brief latency. **Duration:** Until the native leak is patched. **Verification:** Step 1 RSS sawtooths at the `--max-requests` cadence instead of climbing without bound.

### Cause D: Cyclic GC disabled before fork is never re-enabled in workers, so prefork copy-on-write pages keep getting dirtied
**Statement:** A `gc.disable()`/`gc.freeze()` performed in the parent before `fork()` (to maximize copy-on-write sharing) is not re-enabled in the children, so child cyclic garbage is never collected and gc_ref bookkeeping/uncollected cycles dirty the shared pages — inflating per-worker RSS.
**Chain:**
- root: GC is disabled/frozen pre-fork in the parent but `gc.enable()` is never called in the child workers
- s1: children never run cyclic collection, so cycles created post-fork accumulate and shared pages are dirtied (CoW breaks)
- s2: each worker's private RSS grows and total prefork memory balloons
- D: workers are OOM-killed (exit 137) one after another
**Indicators:**
- root: [Step 3] in a worker, `gc.isenabled()` is `False` and `gc.get_count()` keeps rising with no collections
  <!-- match: {"step": 3, "predicate": "contains", "target": "gen counts:"} -->
- s1: [Step 4] worker `gc.get_objects()` length grows continuously and `gc.collect()` (when forced) reclaims a large count
- s2: [Step 1] each forked worker's RSS climbs in parallel; `pmap` shows growing private (non-shared) pages
- D: [Symptom] multiple workers restart `OOMKilled` (exit 137)
**Interventions:**
- **remediation** (root): Use the documented CoW-friendly sequence — `gc.disable()` early in the parent, `gc.freeze()` right before fork, and `gc.enable()` early in each child.

  ```python
  import gc, os
  gc.disable()
  # ... build long-lived parent state ...
  gc.freeze()                 # move parent objects to the permanent generation
  pid = os.fork()
  if pid == 0:                # child
      gc.enable()             # re-enable cyclic collection in the worker
      # ... serve requests ...
  ```

  **Verification:** Re-run Step 3 in a worker; `gc.isenabled()` is `True`, `gc.get_count()` cycles instead of growing, and Step 1 worker RSS plateaus.

### Cause Z: Unidentified
**Statement:** Memory growth is confirmed but none of the above roots match the collected evidence; the leak source is not yet localized.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture a full diagnostic snapshot and escalate to the SME.

  ```bash
  ps -o pid,rss,vsz,cmd -p "$PID" > /tmp/mem_ps.txt
  pmap -x "$PID" > /tmp/mem_pmap.txt
  py-spy dump --pid "$PID" > /tmp/mem_pyspy.txt
  python - <<'PY' > /tmp/mem_gc.txt
  import gc; print(gc.get_count(), gc.get_stats()); print("garbage:", len(gc.garbage))
  PY
  dmesg -T | grep -iE "oom|killed process" | tail -20 > /tmp/mem_dmesg.txt
  ```

  **Risk:** Diagnostic snapshot only; does not stop the growth. **Duration:** Single capture; recycle the process if OOM is imminent. **Verification:** Snapshot files exist and are attached to the escalation ticket for SME review.

## Prevention

- Bound every cache: `functools.lru_cache(maxsize=N)` (never `maxsize=None` on hot paths), or `cachetools.LRUCache`/`TTLCache`; never use a bare module-global `dict`/`list` as a lifetime cache.
- Use `weakref`/`WeakValueDictionary` for registries and parent↔child back-references to avoid reference cycles.
- Add a memory regression test: assert tracemalloc `get_traced_memory()` does not grow after N iterations of the hot path.
- Alert on RSS slope, not just absolute RSS: page when RSS rises monotonically for >30 min with no plateau, and alert on container `OOMKilled` / exit-code-137 restarts.
- Set container `memory.max` (cgroup) and a Python `RLIMIT_AS` so a leak fails fast and is observable, rather than thrashing the host.
- For prefork servers, set `--max-requests`/`--max-requests-jitter` (Gunicorn) or equivalent as a safety net, and follow the `gc.disable()`→`gc.freeze()`→`gc.enable()` fork sequence.
- Keep native dependencies patched; subscribe to security/leak advisories for crypto, image, and BLAS backends.

## Sources

- [Tracemalloc](https://docs.python.org/3/library/tracemalloc.html) — `tracemalloc.start(nframes)`, `take_snapshot()`, `Snapshot.compare_to(old, 'lineno')`, `statistics('traceback')`, `get_traced_memory()`, and the two-snapshot diff / "Top 10" display example used in Step 2.
- [Gc](https://docs.python.org/3/library/gc.html) — `gc.set_debug(gc.DEBUG_LEAK)` (= `DEBUG_COLLECTABLE|DEBUG_UNCOLLECTABLE|DEBUG_SAVEALL`), `gc.garbage` population of uncollectable objects, `gc.collect()`, `gc.get_objects()`, `gc.get_referrers()`, `gc.get_count()`, `gc.get_stats()`, and the `gc.disable()`→`gc.freeze()`→`gc.enable()` copy-on-write-friendly fork sequence (Cause D, Step 3).
