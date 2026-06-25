---
id: "jenkins-build-failure"
title: "Jenkins build/agent failures: remoting, controller OOM, full workspace, or plugin mismatch"
domain: application
service: jenkins
symptom_class: [deployment_failure, timeout]
severity: high
scope: global
version: "1.0.0"
last_updated: "2026-06-24"
verified_by: "kb-researcher"
status: draft
tags: [agent-disconnect, channel-closed, outofmemoryerror, executor-starvation, no-space-left-on-device]
difficulty: intermediate
---

## Symptom Recognition

- Build console / agent log: `Remote call on JNLP4-connect connection from <host> failed. The channel is closing down or has closed down`
- Controller log (`jenkins.log`): `<agent> JNLP agent ... is disconnected`, preceded by `Ping failed. Terminating the channel <agent>.` / `onDeadPing failed`
- Controller log: `java.lang.OutOfMemoryError: Java heap space` or `java.lang.OutOfMemoryError: GC overhead limit exceeded`
- Build sits in the build queue with tooltip `(pending—Waiting for next available executor)` and never starts
- Build fails with `java.io.IOException: ... No space left on device` or `OSError: [Errno 28] No space left on device`
- Startup banner: `Some plugins could not be loaded due to unsatisfied dependencies. Fix these issues and restart Jenkins to re-enable these plugins.`

## Applicability

- Jenkins controller 2.4xx LTS or newer; inbound (JNLP4) or SSH agents.
- Required access: admin on `Manage Jenkins` (System Information, System Log, Script Console), shell access to the controller host and the affected agent host.
- Tools: `df`, `free`, `top`/`jstat`, `jcmd`/`jstack`, a browser for `<JENKINS_URL>/manage/`, and read access to `JENKINS_HOME` and agent remoting logs.

## Diagnostic Steps

### Step 1: Inspect the controller log for OOM and agent-disconnect lines

```bash
# systemd install
journalctl -u jenkins --since "1 hour ago" --no-pager | \
  grep -Ei "OutOfMemoryError|Terminating the channel|onDeadPing|is disconnected|closing down"
# war/tar install
grep -Ei "OutOfMemoryError|Terminating the channel|onDeadPing|is disconnected|closing down" \
  "$JENKINS_HOME/logs/jenkins.log" /var/log/jenkins/jenkins.log 2>/dev/null
```

Expected output: no matching lines on a healthy controller; matches identify OOM or agent-channel termination.

### Step 2: Read the affected agent's connection log

```bash
# In the UI:  <JENKINS_URL>/computer/<AGENTNAME>/log
# On the agent host (inbound agent started via agent.jar):
tail -n 200 /var/log/jenkins-agent/remoting.log 2>/dev/null || \
  tail -n 200 ~/remoting/logs/remoting.log.0
```

Expected output: a clean agent shows `Connected` / `Remoting version: ...`; an unhealthy agent shows `The channel is closing down or has closed down`, `Read timed out`, or a connection-refused/EOF stack trace.

### Step 3: Check controller JVM heap usage and GC pressure

```bash
JPID=$(pgrep -f 'jenkins.war\|/usr/share/java/jenkins')
jcmd "$JPID" GC.heap_info
jstat -gcutil "$JPID" 1000 5   # watch FGC/FGCT columns
```

Expected output: `GC.heap_info` shows `used` well below `capacity`; in `jstat`, `O` (old gen %) is not pinned near 100 and `FGC` (full-GC count) is not climbing every second.

### Step 4: Check disk space on JENKINS_HOME, the workspace, and /tmp

```bash
df -h "$JENKINS_HOME" /tmp                      # controller
df -h /var/lib/jenkins/workspace /tmp           # agent: remote root + tmp
du -sh "$JENKINS_HOME"/workspace/* 2>/dev/null | sort -rh | head
```

Expected output: every filesystem reports `Use%` below ~90% with free space above the agent temp-space threshold (1 GB by default); no filesystem at `100%`.

### Step 5: Inspect the build queue and executor availability

```bash
# Script Console:  <JENKINS_URL>/manage/script
# Paste:
Jenkins.instance.queue.items.each { println "${it.task.name} :: ${it.why}" }
Jenkins.instance.computers.each { c ->
  println "${c.displayName} online=${c.online} idleExecutors=${c.countIdle()}/${c.numExecutors}" }
```

Expected output: an empty or short queue with healthy reasons; a stuck build prints a `why` such as `Waiting for next available executor` or `<node> is offline`, and an offline/zero-idle node identifies the bottleneck.

### Step 6: Verify plugin load state and versions

```bash
# Script Console:
Jenkins.instance.pluginManager.plugins.findAll { !it.isActive() }.each {
  println "INACTIVE ${it.shortName} ${it.version}" }
Jenkins.instance.pluginManager.failedPlugins.each {
  println "FAILED ${it.name} -> ${it.cause}" }
println "core=" + Jenkins.VERSION
# Also: <JENKINS_URL>/manage/systemInfo  and  <JENKINS_URL>/log/all
```

Expected output: no `INACTIVE`/`FAILED` lines; failures print the unsatisfied dependency (e.g. `requires <plugin> >= <version>`) or `Jenkins (X) or higher required`.

## Causes

### Cause A: Network/environment breaks the agent remoting channel (missed pings)

**Statement:** An external factor on the agent or network path (firewall idle-timeout, NAT/proxy drop, power-saving sleep, or a transient packet loss) silently severs the TCP connection, so the controller's ping watchdog misses replies and terminates the channel mid-build.
**Chain:**
- root: environment drops or stalls the agent↔controller TCP connection
- s1: ping watchdog gets no reply within the interval and declares the channel dead
- s2: controller terminates the channel; the build's executor is lost
- D: the in-flight build aborts and the symptom error strings appear

**Indicators:**
- root: [Step 2] agent log shows `The channel is closing down or has closed down` or `Read timed out` with no OOM nearby
  <!-- match: {"step": 2, "predicate": "contains", "target": "channel is closing down"} -->
- s1: [Step 1] controller log shows `onDeadPing failed` then `Terminating the channel`
  <!-- match: {"step": 1, "predicate": "contains", "target": "Terminating the channel"} -->
- s2: [Symptom] build console shows `Remote call on JNLP4-connect connection ... failed. The channel is closing down`

**Interventions:**
- **remediation** (root): remove the external drop — raise/disable the firewall/NAT idle timeout on the agent path and disable OS power-saving/sleep on the agent host so the connection stays alive.

  ```bash
  # On the agent host (Linux): keep the box awake and TCP keepalives tight
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
  sudo sysctl -w net.ipv4.tcp_keepalive_time=120 net.ipv4.tcp_keepalive_intvl=30
  ```

  **Verification:** re-run Step 1 over the next build cycle — no new `Terminating the channel` lines for that agent; the build completes.
- **defensive_fix** (s1): make Jenkins tolerate brief stalls by shortening the keep-alive ping interval so the channel is kept warm and dead peers are detected predictably.

  ```groovy
  // Script Console — enable a 5-min ping interval (seconds)
  System.setProperty("hudson.slaves.ChannelPinger.pingIntervalSeconds", "300")
  ```

  **Verification:** Step 2 agent log shows steady `Ping` activity and no `Read timed out` over a sustained build.

### Cause B: Controller JVM heap exhausted (OutOfMemoryError)

**Statement:** The controller heap is too small for the live data set or for transient spikes (large test-report parsing, many concurrent builds), so the JVM throws OutOfMemoryError and stalls or kills builds and agent channels.
**Chain:**
- root: controller `-Xmx` heap is smaller than the working set / transient peak
- s1: old generation fills and the GC spends most CPU reclaiming nothing
- s2: JVM throws `OutOfMemoryError`; threads (including agent channels) die or hang
- D: builds fail, hang, or agents disconnect with OOM in the log

**Indicators:**
- root: [Step 3] `jstat` shows old-gen `O` pinned ~100% and `FGC` climbing each second
  <!-- match: {"step": 3, "predicate": "contains", "target": "OutOfMemory"} -->
- s1: [Step 3] `GC.heap_info` shows `used` ≈ `capacity` (no headroom)
- s2: [Step 1] controller log shows `java.lang.OutOfMemoryError: Java heap space` or `GC overhead limit exceeded`
  <!-- match: {"step": 1, "predicate": "contains", "target": "OutOfMemoryError"} -->

**Interventions:**
- **remediation** (root): raise the heap and switch to G1GC with the Jenkins-recommended large-instance flags, then capture a heap dump if it recurs.

  ```bash
  # /etc/sysconfig/jenkins (or systemd drop-in / jenkins.xml on Windows)
  JENKINS_JAVA_OPTIONS="-Xms4g -Xmx4g \
    -XX:+UseG1GC -XX:+AlwaysPreTouch \
    -XX:+UseStringDeduplication \
    -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/var/lib/jenkins/dumps"
  sudo systemctl restart jenkins
  ```

  **Verification:** after restart re-run Step 3 — under load old-gen `O` stays below ~80% and `FGC` is occasional, not continuous; no new OOM in Step 1.
- **mitigation** (s2): immediate restart to clear the wedged heap and unblock the queue while the new heap config is staged.

  ```bash
  sudo systemctl restart jenkins
  ```

  **Risk:** aborts in-flight builds and briefly disconnects all agents. **Duration:** until the next heap-pressure spike (minutes-to-hours). **Verification:** the build queue drains and Step 3 shows fresh, low heap usage.

### Cause C: Workspace / temp filesystem full on the agent

**Statement:** The agent's remote-root (workspace) or `/tmp` filesystem fills with stale workspaces, cached tools, and build artifacts, so writes fail and the node is marked unavailable for scheduling.
**Chain:**
- root: workspace/tmp filesystem on the agent reaches 100% (stale workspaces + caches accumulate)
- s1: build steps fail to write or Jenkins flags the node below its free-temp-space threshold
- s2: the node stops accepting work / the running build aborts
- D: build fails with `No space left on device` or sits unscheduled

**Indicators:**
- root: [Step 4] `df -h` shows the workspace or `/tmp` filesystem at `100%` Use
  <!-- match: {"step": 4, "predicate": "contains", "target": "100%"} -->
- s1: [Symptom] build console shows `No space left on device` (Java IOException or `Errno 28`)
  <!-- match: {"step": 4, "predicate": "contains", "target": "No space left on device"} -->
- s2: [Step 5] queue `why` shows the node offline / `low disk space` and `idleExecutors=0`

**Interventions:**
- **remediation** (root): reclaim space and prevent regrowth — enable the Workspace Cleanup plugin (`cleanWs()` post-build) or a discard policy so workspaces and old artifacts do not accumulate.

  ```groovy
  // Declarative pipeline — purge the workspace after every build
  post { always { cleanWs() } }
  // And cap build retention in job config:  buildDiscarder(logRotator(numToKeepStr: '20'))
  ```

  **Verification:** re-run Step 4 — workspace/`/tmp` Use% drops well below 90% and stays there across several builds.
- **mitigation** (s1): manually free space now to unblock scheduling.

  ```bash
  # On the agent host — remove stale per-job workspaces and tmp scratch
  rm -rf /var/lib/jenkins/workspace/*@tmp /var/lib/jenkins/workspace/*@2
  find /tmp -maxdepth 1 -name 'jenkins*' -mtime +1 -exec rm -rf {} +
  df -h /var/lib/jenkins/workspace /tmp
  ```

  **Risk:** deleting a workspace of a currently running build corrupts that build; confirm the node is idle first. **Duration:** until workspaces refill. **Verification:** Step 4 shows free space restored; Step 5 shows the node back online.

### Cause D: Plugin/core version mismatch after an upgrade

**Statement:** A plugin (or the core) was upgraded such that an installed plugin's required dependency or minimum Jenkins version is no longer satisfied, so the plugin fails to load and breaks the jobs/build steps that depend on it.
**Chain:**
- root: an upgraded plugin requires a newer dependency or core version than is installed
- s1: Jenkins disables the plugin at startup (unsatisfied dependency)
- s2: pipeline steps/SCM/agent providers from that plugin are missing
- D: builds fail to start or error on the missing step

**Indicators:**
- root: [Step 6] failed-plugins list prints `requires <plugin> >= <version>` or `Jenkins (X) or higher required`
  <!-- match: {"step": 6, "predicate": "contains", "target": "required"} -->
- s1: [Symptom] startup banner shows `Some plugins could not be loaded due to unsatisfied dependencies`
- s2: [Step 6] `<JENKINS_URL>/log/all` shows `Failed Loading plugin <name>` / `INACTIVE` entries

**Interventions:**
- **remediation** (root): install the missing/updated dependency or upgrade core to the version the plugin requires, then restart.

  ```bash
  # Pin/upgrade via plugin manager or jenkins-plugin-cli, then restart
  jenkins-plugin-cli --plugins <plugin>:<version> <missing-dependency>:latest
  sudo systemctl restart jenkins
  ```

  **Verification:** re-run Step 6 — no `INACTIVE`/`FAILED` plugins and the dependency banner is gone; the affected build step resolves.
- **mitigation** (s1): roll the offending plugin back to the last-known-good `.jpi` to restore service immediately.

  ```bash
  # Downgrade in Manage Jenkins > Plugins > Installed (per-plugin "Downgrade"),
  # or remove the pinned .jpi and restore the prior version:
  mv "$JENKINS_HOME/plugins/<plugin>.jpi" "$JENKINS_HOME/plugins/<plugin>.jpi.bak"
  cp /backup/plugins/<plugin>.jpi "$JENKINS_HOME/plugins/<plugin>.jpi"
  sudo systemctl restart jenkins
  ```

  **Risk:** the rolled-back version may lack a needed fix or re-introduce its own incompatibility. **Duration:** until a compatible upgrade is planned. **Verification:** Step 6 shows the plugin active again and builds run.

### Cause Z: Unidentified

**Statement:** Diagnostics do not match any known root cause above; capture a full diagnostic snapshot and escalate.
**Indicators:**
- [Default]

**Interventions:**
- **mitigation** (D): collect a Support Core support bundle plus a controller thread dump and recent logs, then escalate to the Jenkins administrator/SME.

  ```bash
  # Thread dump (UI):  <JENKINS_URL>/threadDump   |  Support bundle:  Manage Jenkins > Support
  JPID=$(pgrep -f 'jenkins.war\|/usr/share/java/jenkins')
  jstack "$JPID" > /tmp/jenkins-threaddump-$(date +%s).txt
  journalctl -u jenkins --since "2 hours ago" --no-pager > /tmp/jenkins-recent.log
  df -h "$JENKINS_HOME" /tmp >> /tmp/jenkins-recent.log
  ```

  **Risk:** none (read-only capture). **Duration:** n/a. **Verification:** support bundle, thread dump, and logs attached to the escalation ticket.

## Prevention

- Set the controller heap explicitly (`-Xmx` = `-Xms`) with G1GC and `-XX:+HeapDumpOnOutOfMemoryError`; size heap to live data plus headroom for test-report spikes. Alert on JVM old-gen > 80% and full-GC rate.
- Monitor `df` on every agent's remote root and `/tmp`; alert at 85% and auto-clean workspaces via `cleanWs()` and `logRotator` build retention. Keep agent free temp space above the 1 GB threshold.
- Use agent labels (do not tie jobs to a single named agent) and provision enough executors so one offline node cannot starve the queue; alert on queue wait time and on `<node> is offline`.
- Keep agent hosts awake (disable sleep/power-saving) and tune `ChannelPinger`/TCP keepalives so idle channels are not dropped by firewalls/NAT.
- Stage plugin/core upgrades on a test controller; read each plugin's required-core and dependency versions before upgrading, and keep prior `.jpi` files for fast rollback.

## Sources

- [Diagnosing errors](https://www.jenkins.io/doc/book/troubleshooting/diagnosing-errors/) — official "Diagnosing Errors" entry point: log locations, thread dumps, support bundle, System Information (page repeatedly timed out on fetch; corroborated via the searches below).
- [Executor starvation](https://www.jenkins.io/doc/book/using/executor-starvation/) — build-queue "Waiting for next available executor" reasons (agent offline, executor busy, label busy), `/computer/<AGENT>` status URL, and the labels/add-agents fix (Step 5, Cause C/queue).
- [Gc tuning](https://www.jenkins.io/blog/2016/11/21/gc-tuning/) — recommended G1GC flags (`-XX:+UseG1GC -XX:+AlwaysPreTouch -XX:+UseStringDeduplication`) and large-instance tuning (Step 3, Cause B).
- [3309623](https://wiki.jenkins.io/JENKINS/3309623.html) — "I'm getting OutOfMemoryError": bigger heap guidance, `-XX:+HeapDumpOnOutOfMemoryError`, `-XX:-UseGCOverheadLimit` (Cause B).
- [12011](https://community.jenkins.io/t/how-can-i-figure-out-why-my-agents-are-closing/12011) / https://community.jenkins.io/t/windows-agent-debugging-unexpected-termination-of-the-channel/10377 — exact channel-termination strings (`Terminating the channel`, `onDeadPing failed`, `channel is closing down`) and `ChannelPinger.pingIntervalSeconds` / power-saving fixes (Cause A).
- [14157](https://community.jenkins.io/t/noob-builds-fail-on-node-oserror-errno-28-no-space-left-on-device/14157) / https://community.jenkins.io/t/the-jenkins-pipeline-was-stuck-in-the-queue-because-the-built-in-node-was-not-able-to-provide-an-available-executor-additionally-jenkins-detected-low-temporary-disk-space-on-tmp-below-1-gb-threshold/37021 — `No space left on device`/`Errno 28`, workspace cleanup, and the low-`/tmp` (1 GB) executor-unavailability link (Step 4, Cause C).
- [7376](https://community.jenkins.io/t/some-plugins-could-not-be-loaded-due-to-unsatisfied-dependencies-fix-these-issues-and-restart-jenkins-to-re-enable-these-plugins/7376) / https://www.jenkins.io/blog/2022/02/10/last-plugin-version-not-installable/ — unsatisfied-dependency banner text and plugin/core version-requirement causes (Step 6, Cause D).
