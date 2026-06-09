---
id: my-runbook-id                 # kebab-case; the KB item id is derived from this
title: "Technology - Failure Mode"  # include the failure mode, not just the technology
domain: database                   # database|networking|compute|application|security|storage|messaging
service: postgresql                # the specific technology
symptom_class: [latency, timeout]  # failure-mode tags (list)
severity: high                     # critical|high|medium|low|info
scope: global                      # global|team|personal
version: "1.0.0"                   # semantic version
last_updated: "2026-06-09"         # ISO date (YYYY-MM-DD)
verified_by: "your-name"           # author/reviewer (or "kb-researcher" for agent-generated)
status: draft                      # draft|in-review|verified|stale|deprecated
tags: [postgres, pool]             # optional; max 10
difficulty: intermediate           # optional; beginner|intermediate|advanced|expert
---

# Runbook: Technology - Failure Mode

> This is the **current per-Cause template**. Canonical spec + rationale:
> [`runbook-content-architecture.md` §3](../../architecture/knowledge-and-ai/runbook-content-architecture.md).
> The body is ingested as vector chunks split on `##` and `### Cause N` headers —
> every section and every Cause becomes one retrieval chunk, so keep each
> self-contained and actionable (no commentary or "why" prose). Required sections:
> Symptom Recognition, Applicability, Diagnostic Steps, Causes, Prevention, Sources.

## Symptom Recognition

The symptoms that trigger this runbook — exact error messages, alert names, and
metric patterns as they appear in production. Keep symptoms + their error strings
together. Be specific (generic descriptions match too many runbooks).

```
example error string as it appears in logs/alerts
```

## Applicability

When this runbook applies — concrete system/version, required tools, and
access/permissions. Confirms the runbook fits the user's environment.

## Diagnostic Steps

### Step 1: <what this step establishes>

```bash
# command
```

What to look for in the output and what it implies. (Findings here are what each
Cause's **Indicator** points back to.)

### Step 2: <what this step establishes>

```bash
# command
```

What to look for and what it implies.

## Causes

One `### Cause N` per distinct root cause (`### Cause A`, `### Cause B`, …), ending
with `### Cause Z: Unidentified`. Each Cause is one self-contained chunk with these
bolded fields, in order.

### Cause A: <short name of the cause>

**Statement:** One declarative sentence stating the cause — not a symptom, not a fix. (≤300 chars; copied verbatim into the engine's `RootCauseConclusion.root_cause`.)
**Mechanism:** How the cause produces the symptom — the causal chain. (≤800 chars; copied into `RootCauseConclusion.mechanism`.)
**Indicator:** What in the Diagnostic Steps above confirms THIS cause specifically.
**Mitigation:** Fast, reversible relief.

```bash
# mitigation command
```

**Resolution:** The permanent fix.

```bash
# resolution command
```

**Verification:** How to confirm this cause is resolved (metric/command + observation period).

### Cause Z: Unidentified

**Statement:** Root cause not determined from the indicators above.
**Mechanism:** N/A.
**Indicator:** None of the above indicators match.
**Mitigation:** Apply the safest general mitigation and gather more diagnostics.
**Verification:** N/A.

## Prevention

Configuration changes, monitoring alerts, and capacity thresholds that avoid
recurrence.

## Sources

- [Source title](https://example.com) — what was used (Priority 1: Official docs)
