"""LLM prompts and token/threshold limits for the conversion pipeline."""

from faultmaven.modules.knowledge.taxonomy import (
    RunbookDomain,
    RunbookSeverity,
    SymptomClass,
    render_vocabulary,
)

# Output budget for one runbook conversion, and the cap the single truncation
# retry may raise it to (#1094). A full runbook — frontmatter, symptom
# recognition, diagnostic steps, causes, resolution — is a long document, so a
# genuine overrun is plausible here in a way it is not on short prose paths.
RUNBOOK_MAX_TOKENS = 4096
RUNBOOK_MAX_TOKENS_CEILING = RUNBOOK_MAX_TOKENS * 2

# Same pair for the analysis pass that precedes conversion.
ANALYSIS_MAX_TOKENS = 2048
ANALYSIS_MAX_TOKENS_CEILING = ANALYSIS_MAX_TOKENS * 2

# Threshold for parallel vs sequential conversion
PARALLEL_THRESHOLD = 6

# =============================================================================
# LLM Prompts
# =============================================================================

ANALYSIS_SYSTEM_PROMPT = """You are an expert at analyzing technical documentation to identify distinct
failure modes. A failure mode is defined by WHAT THE OPERATOR OBSERVES: one
symptom surface -- the alert that fires, the error the clients see, the metric
that breaches. It is NOT defined by why it happened.

This distinction decides how many items you return, so apply it literally:

- Several ROOT CAUSES of the SAME observable symptom are ONE failure mode.
  A guide covering "502 Bad Gateway" whose causes are a dead upstream, a slow
  upstream, oversized headers and stale DNS describes ONE failure mode. The
  operator sees one thing -- a 502 -- and has to work out which cause it is.
  Distinguishing between them is the JOB the runbook does; it is not a reason
  to write four runbooks. Return one item whose `symptoms_summary` describes
  the shared symptom and whose `resolution_summary` names each cause -- that
  pair is the analysis record the operator reads back, so it must account for
  every cause you merged.
- Different observable symptoms are different failure modes. A reference
  covering `OOMKilled`, `ImagePullBackOff`, `Pending` and `CrashLoopBackOff`
  describes FOUR: an operator seeing one of them is not seeing the others.

The reliable test is `symptom_class`. If two candidate items would carry the
same SET of `symptom_class` values for the same `service`, they are one failure
mode with two causes -- merge them. Overlapping sets are a warning sign too:
["oom"] and ["oom", "crash_loop"] for one service usually means one observed
failure that you have described twice, so re-read the source and decide which
single set is right. If you find yourself distinguishing items by their FIX
rather than by what is observed, you are splitting causes, not failure modes.

Your task: Read the provided document and identify every distinct failure mode
it covers. For each failure mode, provide:
1. A short title (include the technology and the observed failure)
2. The symptoms or error messages associated with it
3. A brief summary of the resolution approach -- naming each documented cause
   when the symptom has several

Rules:
- If the document covers only ONE failure mode, return exactly one item. A
  document organised as one symptom with several causes IS this case, however
  many causes it lists.
- If the document is purely architectural/conceptual with no failure modes,
  return an empty list and set "is_actionable" to false.
- Do NOT invent failure modes not present in the source material.
- Failure modes must be distinct in what is OBSERVED. Differing only in
  resolution is not distinct -- that is one failure mode with several causes.
- `symptom_class` values MUST come from this controlled vocabulary: __SYMPTOM_CLASS_VOCAB__. Choose the closest-fitting value(s); omit anything that doesn't fit (the runbook author uses free-text `tags` for long-tail symptoms). This is the same vocabulary the runbook frontmatter is validated against, and it keys failure-mode deduplication -- an off-vocabulary value here silently escapes both.

Respond with JSON matching this schema:
{
  "is_actionable": true/false,
  "failure_modes": [
    {
      "id": "kebab-case-id",
      "title": "Technology Failure Description",
      "domain": "__DOMAIN_VOCAB__",
      "service": "specific-service-name",
      "symptom_class": ["<one or more values from the controlled vocabulary above>"],
      "severity": "__SEVERITY_VOCAB__",
      "symptoms_summary": "Error messages and symptoms",
      "resolution_summary": "Brief resolution approach"
    }
  ],
  "source_assessment": {
    "content_type": "troubleshooting_guide|incident_report|postmortem|vendor_docs|other",
    "actionability_rating": "high|medium|low",
    "missing_information": ["list of missing info"]
  }
}"""

# Constrain the analysis LLM's `symptom_class` to the controlled vocabulary so the
# extracted value is already in-vocab: it keys failure-mode dedup
# (``_convert_all_failure_modes``) AND is validated in the produced frontmatter.
# Without this, analysis free-picks an off-vocab label, the conversion prompt
# (rule 9) later reclassifies it, and the dedup key no longer equals the persisted
# symptom_class — so two modes that classify to the same value slip dedup and yield
# duplicate runbooks. Every vocabulary the schema names — symptom_class, domain
# and severity — is rendered from the taxonomy enums, so the prompt cannot offer
# a value the validator or the draft row's CHECK refuses (#1886: it offered
# ``info`` while the CHECK did not admit it).
ANALYSIS_SYSTEM_PROMPT = (
    ANALYSIS_SYSTEM_PROMPT.replace(
        "__SYMPTOM_CLASS_VOCAB__", render_vocabulary(SymptomClass)
    )
    .replace("__DOMAIN_VOCAB__", render_vocabulary(RunbookDomain, "|"))
    .replace("__SEVERITY_VOCAB__", render_vocabulary(RunbookSeverity, "|"))
)

# DESIGN DECISION (predicate-less conversion — intentional, not a gap).
# The conversion path (document -> runbook, case -> runbook) authors the v4 match
# surface + topology: Statement / optional Chain / Indicators / quadrant-tagged
# Interventions. It deliberately does NOT author ``<!-- match -->`` predicates (the
# deterministic validation surface). Predicate authoring is a separate, Phase-0-gated
# enrichment owned by the kb-toolkit generation path (kb-init / kb-researcher; Slice 6
# / #584) — its symptom-telemetry ``target`` contract and ``stance`` counterfactual are
# not yet ratified, so emitting predicates here would produce ill-formed ones. A
# conversion-produced runbook is therefore predicate-less by design; it still MATCHES
# and instantiates (the Statement + Chain do that) and grounds via the LLM tier. When
# the predicate contract lands, predicates are added by re-running the toolkit
# enrichment over these runbooks, not by expanding this prompt.
# See docs/working/AUDIT-runbook-template.md §3 (mirror 5) + PLAN 1a.
CONVERSION_SYSTEM_PROMPT = """You are a technical writer converting source material into a FaultMaven v4
causal-chain runbook. You MUST produce output that exactly matches the template below.
Every section and sub-field is required. Do not add sections. Do not rename sections.
Do not include commentary, explanations, or meta-text -- only the runbook.

TEMPLATE:
=========

---
id: {{id}}
title: "{{title}}"
domain: {{domain}}
service: {{service}}
symptom_class: [{{symptom_classes}}]
scope: {{scope}}
tags: [{{tags}}]
difficulty: intermediate
severity: {{severity}}
version: "1.0.0"
last_updated: "{{today_iso}}"
verified_by: ""
status: draft
---

# Runbook: {{title}}

## Symptom Recognition
- Exact alert names as they appear: "Alert: ..."
- Error messages as they appear in logs: "ERROR: ..."
- Metric patterns: "metric > threshold for duration"

## Applicability
State the software version range, required access level, and tools needed
(e.g. "PostgreSQL 14+, AWS RDS or self-hosted. Requires pg_monitor role. Tools: psql.").

## Diagnostic Steps

### Step 1: {{description}}
```{{language}}
{{command}}
```
{{what to look for in the output — be specific}}

### Step 2: {{description}}
...

## Causes

### Cause A: {{name}}
**Statement:** Single declarative sentence stating the single root cause (≤300 chars).
**Chain:**
- root: the root cause (the chain's top node; mirrors Statement)
- s1: intermediate state — the direct effect of the node above
- D: the failure (points at Symptom Recognition; do not re-author it)
**Indicators:**
- root: [Step 1] {{observable from Step 1 that confirms the root rung}}
- s1: [Step 2] {{observable that confirms intermediate state s1}}
**Interventions:**
- **remediation** (root): {{the durable fix at the root}}

  ```{{language}}
  {{durable fix command}}
  ```

  **Verification:** Re-run Step N; {{what confirms the fix worked}}.
- **mitigation** (s1): {{a temporary interception — include only if one genuinely exists}}

  ```{{language}}
  {{quick fix command}}
  ```

  **Risk:** {{what could go wrong}}. **Duration:** {{how long safe}}. **Verification:** {{cause-specific check}}.

### Cause Z: Unidentified
**Statement:** None of the documented causes match the observed evidence.
**Indicators:**
- [Default]
**Interventions:**
- **mitigation** (D): Capture full diagnostic output and consult an SME.
  **Risk:** Diagnostic only. **Duration:** Until SME review. **Verification:** N/A.

## Prevention
- {{configuration change to prevent recurrence}}
- {{monitoring alert to add}}

## Sources
- {{source_filename}} -- primary source document for this runbook

=========

RULES:
1. Every section and sub-field MUST contain content. No empty fields.
2. ## Diagnostic Steps MUST contain fenced code blocks under numbered `### Step N: <title>` headers (number, colon, then a short inline title).
3. ## Causes MUST have at least one real ### Cause A subsection AND the fallback ### Cause Z: Unidentified.
4. Each ### Cause declares exactly ONE root — never two roots, never an AND-gate. Each ### Cause (except Z) needs **Statement**, **Indicators**, and **Interventions**; **Chain** is optional (omit it for a simple one-step cause). For two co-necessary conditions: when one enables the other, express them as sequential Chain rungs; when neither causes the other, fold the second into the root Statement.
5. Statement ≤300 characters; each Chain rung ≤300 characters. Each complete ### Cause block (heading through its last Intervention) under 2800 characters — split a sprawling failure mode into separate Causes. Hard limits.
6. Each Indicator entry carries a rung ref (`root`, `s1`, …, or `D`) and at least one `[Step N]` (N matches an existing Diagnostic Step) or `[Symptom]`; the Cause Z fallback uses `- [Default]`.
7. Each Intervention is tagged with exactly one quadrant — `remediation` / `defensive_fix` / `mitigation` / `loop_break` — names the rung it targets in `(parens)`, and carries a **Verification:**; every `mitigation` also carries **Risk** and **Duration**.
8. If source material lacks enough information for a field, write "[INSUFFICIENT SOURCE DATA -- manual completion required]".
9. Use the `domain` and `service` values provided; do not change them. `symptom_class` MUST be one or more values from this controlled vocabulary: __SYMPTOM_CLASS_VOCAB__ — usually one; add another only if the failure mode genuinely spans a second class. Choose the closest fit to this failure mode (use any suggested value only as a starting point); never invent a value — put a long-tail symptom in `tags` instead."""

# Bind the controlled `symptom_class` vocabulary into the rules so the produced
# frontmatter is in-vocab for BOTH the document and case paths — the case path
# supplies no symptom_class taxonomy, so the model classifies here rather than
# emitting an off-vocab placeholder. RunbookValidator (the draft-validation gate)
# is the mechanical backstop if the model still strays off-vocab. Rendered from
# the ``SymptomClass`` enum so the prompt can't drift from the gate.
CONVERSION_SYSTEM_PROMPT = CONVERSION_SYSTEM_PROMPT.replace(
    "__SYMPTOM_CLASS_VOCAB__", render_vocabulary(SymptomClass)
)
