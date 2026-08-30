# Data Classification Strategy

**Version**: 3.0
**Status**: Design Specification (matches implementation)
**Date**: 2026-04-18
**Role in Four-Tier Model**: **Tier 0: Classification** — the first stage in the [Data Preprocessing](./data-preprocessing-design-specification.md) four-tier model. Tier 0 runs on every submission (file uploads and pasted text via `POST /cases/{id}/turns`), completes in <100 ms with zero LLM calls, and produces a `DataType` enum + confidence score that determines which Tier 1 extractor runs next.

**Scope**: This document covers **data-type classification** — determining what kind of content has been submitted (logs vs metrics vs configuration, etc.). It is separate from **evidence classification** (symptom / causal / symptom-absence / causal-absence evidence — claim-anchored categories the LLM assigns during `INVESTIGATING`), which is described in [evidence-driven-investigation-framework.md §5](../investigation-engine/evidence-driven-investigation-framework.md#5-evidence-model).

**Entry point**: All turns arrive via `POST /cases/{id}/turns` as `{query?, attachments?[]}`. Attachments are routed through Tier 0+1 via `PreprocessingService.classify_and_extract()`. Query-only turns skip preprocessing entirely.

---

## Table of Contents

1. [Overview](#overview)
2. [Two-Layer Data Type Enum](#two-layer-data-type-enum)
3. [5-Priority Classification](#5-priority-classification)
4. [Priority 1 — User Override](#priority-1--user-override)
5. [Priority 2 — Agent Hint](#priority-2--agent-hint)
6. [Priority 3 — Source URL Patterns](#priority-3--source-url-patterns)
7. [Priority 4 — Browser Context](#priority-4--browser-context)
8. [Priority 5 — Rule-Based Content + Extension](#priority-5--rule-based-content--extension)
9. [Command-Output Classification](#command-output-classification)
10. [Disambiguation Helpers](#disambiguation-helpers)
11. [Confidence Thresholds](#confidence-thresholds)
12. [`classification_failed` Path (Cooperative Clarification)](#classification_failed-path-cooperative-clarification)
13. [Telemetry](#telemetry)
14. [Future Work](#future-work)

---

## Overview

### Classification Challenge

Submitted content can have overlapping characteristics (logs contain errors, metrics can be logged), ambiguous structure (CSVs could be metrics or lookup tables), missing context (pasted snippets have no filename), or short length.

### Design Goals

1. **Accuracy** — >95% correct classification on clear cases.
2. **Robustness** — graceful degradation on ambiguous cases.
3. **Speed** — <100 ms, zero LLM calls in Tier 0.
4. **Explainability** — every decision carries a `source` tag (user_override / agent_hint / source_url / browser_context / rule_based / rule_based_best_effort).
5. **Cooperative-clarification fallback** — when confidence falls below 0.50 the classifier sets `classification_failed=True` and populates `suggested_types`. The agent still runs the turn best-effort on the raw file; after the turn, `InvestigationService` injects DECIDE suggestions — one set per failed attachment — that reclassify the file mechanically when clicked or typed.

### Architectural Position

```
POST /cases/{id}/turns → {query?, attachments?[]}
         ↓
Step 1 (pre-LLM, per attachment):
         ↓
Tier 0: DataClassifier.classify() (THIS DOC)       zero LLM, <100 ms
         ↓
Tier 1: extractor.extract()                        zero LLM, <2 s
  ├── LOGS_AND_ERRORS      → LogsAndErrorsExtractor   (crime_scene)
  ├── ERROR_REPORT         → ErrorReportExtractor     (exception_context)
  ├── TRACE_DATA           → TraceDataExtractor       (trace_correlation)
  ├── COMMAND_OUTPUT       → CommandOutputExtractor   (command_parsing)
  ├── METRICS_AND_PERFORMANCE → MetricsExtractor      (statistical)
  ├── PROFILING_DATA       → ProfilingExtractor       (profiling_hotspot)
  ├── STRUCTURED_CONFIG    → ConfigExtractor          (direct)
  ├── SOURCE_CODE          → SourceCodeExtractor      (ast_parse)
  ├── UNSTRUCTURED_TEXT    → TextExtractor            (direct)
  ├── DOCUMENTATION        → DocumentationExtractor   (documentation_structure)
  └── VISUAL_EVIDENCE      → VisualExtractor          (vision)
         ↓
Evidence created → Context Sliding Window → LLM Inference (Step 2)
```

**Pasted text** is submitted via the `pasted_content` form field on `/turns`, wrapped into an attachment with a synthetic filename (`pasted-content-{ts}.txt` or `page-capture-{ts}.txt`), and goes through the same Tier 0+1 pipeline. Pasted content has no filename extension to use as a hint; the classifier falls back to content patterns.

---

## Two-Layer Data Type Enum

The classifier emits a **detailed 12-value `DataType`** (internal). Downstream, `to_unified_data_type()` collapses it to a **unified 6-value `UnifiedDataType`** (public).

Two layers are needed for a concrete reason: Tier 1 dispatches to 11 specialized extractors based on the fine-grained type, while the Evidence API and UI surface a simpler, stable vocabulary for users and external callers.

| Detailed (`DataType`, 12 values) | Unified (`UnifiedDataType`, 6 values) | Extractor |
|---|---|---|
| `LOGS_AND_ERRORS` | `LOGS` | LogsAndErrorsExtractor |
| `ERROR_REPORT` | `LOGS` | ErrorReportExtractor |
| `TRACE_DATA` | `LOGS` | TraceDataExtractor |
| `COMMAND_OUTPUT` | `LOGS` | CommandOutputExtractor |
| `METRICS_AND_PERFORMANCE` | `METRICS` | MetricsExtractor |
| `PROFILING_DATA` | `METRICS` | ProfilingExtractor |
| `STRUCTURED_CONFIG` | `CONFIGURATION` | ConfigExtractor |
| `SOURCE_CODE` | `CODE` | SourceCodeExtractor |
| `UNSTRUCTURED_TEXT` | `TEXT` | TextExtractor |
| `DOCUMENTATION` | `TEXT` | DocumentationExtractor |
| `VISUAL_EVIDENCE` | `IMAGE` | VisualExtractor |
| `UNANALYZABLE` | `TEXT` | (none — reference-only placeholder) |

Mapping is defined in `faultmaven/core/preprocessing/models.py::_DETAILED_TO_UNIFIED`.

### `ClassificationResult`

```python
class ClassificationResult(BaseModel):
    data_type: DataType            # 12-value detailed enum
    confidence: float              # 0.0 - 1.0
    source: Literal[               # How the classification was determined
        "user_override",
        "agent_hint",
        "source_url",
        "browser_context",
        "rule_based",
        "rule_based_best_effort",
    ]
    classification_failed: bool    # True when confidence < 0.50 (triggers cooperative-clarification suggestions)
    suggested_types: Optional[List[DataType]]  # Populated on every classification_failed path; drives cooperative-clarification DECIDE suggestions
    source_type: Optional[str]     # page_capture / text_paste / file_upload
                                   # (propagated from source_metadata)
```

Note: the `source` field replaced an earlier `fallback_level: int` design. The named source is more informative — it encodes *which signal won*, not just how deep in the decision tree we went.

---

## 5-Priority Classification

Classification walks five priorities **in signal-reliability order** (not in confidence-magnitude order — a 0.95 agent hint is more trustworthy than a 0.98 content-pattern match, because the hint was given by another evidence-aware component):

| Priority | Signal | Confidence range | `source` value |
|---|---|---|---|
| 1 | User override | 1.00 | `user_override` |
| 2 | Validated agent hint | 0.95 | `agent_hint` |
| 3 | Source URL pattern | 0.88 – 0.96 | `source_url` |
| 4 | Browser context | 0.85 – 0.92 | `browser_context` |
| 5 | Rule-based content + extension | 0.45 – 0.99 | `rule_based` / `rule_based_best_effort` |

Each priority is either *matched and returned* or *skipped to the next*. Priority 5 always returns a result (worst case: `UNSTRUCTURED_TEXT` at 0.30 with `classification_failed=True`).

### Pre-Priority guard — empty/whitespace short-circuit

Before Priority 1 runs, `classify()` checks for empty or whitespace-only content. A 0-byte upload (or a paste that boils down to whitespace) carries no analysable signal — routing it through Priority 5 would land on a low-confidence `UNSTRUCTURED_TEXT` with `classification_failed=True`, which surfaces the cooperative-clarification modal asking "what should we treat this as?" That is the wrong UX for a confirmed-empty file. The guard returns `(UNANALYZABLE, confidence=1.0, source="rule_based")` and lets the preprocessing service emit a clean "file is empty" placeholder. The guard is deliberately placed **above** Priority 1 because a user_override on empty content is also meaningless. See `classifier.py:classify` (the `if not content or not content.strip()` block).

---

## Priority 1 — User Override

When the user has explicitly selected a type (e.g., via the `classification_failed` modal), bypass all inference. `confidence = 1.0`.

```python
if user_override:
    return ClassificationResult(
        data_type=user_override, confidence=1.0, source="user_override",
        classification_failed=False,
    )
```

---

## Priority 2 — Agent Hint

Agents (investigation, copilot) can suggest a type when they have context. The hint is validated against content/filename heuristics before acceptance — a hint that contradicts obvious signals is rejected.

```python
if agent_hint and self._validate_hint(filename, content, agent_hint):
    return ClassificationResult(
        data_type=agent_hint, confidence=0.95, source="agent_hint",
        classification_failed=False,
    )
```

`_validate_hint()` checks:

- `VISUAL_EVIDENCE` — must have image extension (`.png/.jpg/.jpeg/.gif/.webp/.bmp`)
- `STRUCTURED_CONFIG` — either config extension (`.yaml/.yml/.json/.toml/.ini/.env/.config`) or key-value pattern in content
- `SOURCE_CODE` — either code extension (`.py/.js/.ts/.jsx/.tsx/.java/.go/.rs/.cpp/.c/.h/.rb/.php/.swift/.kt/.scala/.sh/.bash`) or code-keyword pattern
- All other types — accepted without validation (hard to heuristically validate)

The validator is a safety valve: a bad agent hint (e.g., poisoned by prompt injection) could otherwise route secrets to the wrong extractor.

---

## Priority 3 — Source URL Patterns

Page captures carry `source_metadata.source_url`. The URL substring is often the strongest available signal — a URL containing `sentry.io` is definitively an error-tracking page.

| Category | Domains/patterns | DataType | Base confidence |
|---|---|---|---|
| Error tracking & logs | `sentry.io`, `bugsnag.com`, `rollbar.com`, `app.datadoghq.com/logs`, `kibana`, `splunk.com`, `logz.io`, `papertrailapp.com`, `/logs/` | `LOGS_AND_ERRORS` | 0.85 – 0.94 |
| APM & metrics | `grafana`, `app.datadoghq.com/apm` \| `/metric` \| `/dashboard`, `prometheus`, `newrelic.com`, `honeycomb.io`, `jaeger`, `zipkin`, `wandb.ai`, `/metrics/`, `/dashboard/` | `METRICS_AND_PERFORMANCE` | 0.82 – 0.92 |
| LLM observability (traces with embedded prompts) | `comet.com/opik`, `opik.comet.com`, `app.opik.com`, `langfuse.com`, `smith.langchain.com`, `langsmith`, `phoenix.arize.com`, `app.helicone.ai` | `TRACE_DATA` | 0.88 – 0.92 |
| Cloud consoles | `console.aws.amazon.com/cloudwatch`, `console.cloud.google.com/logs`, `portal.azure.com` | context-dependent | 0.88 – 0.90 |
| Source code | `github.com`, `gitlab.com`, `bitbucket.org` | `SOURCE_CODE` | 0.90 |
| Documentation | `readthedocs.io`, `docs.`, `confluence`, `notion.so` | `UNSTRUCTURED_TEXT` | 0.88 |

When `source_type == "page_capture"`, add `PAGE_CAPTURE_CONFIDENCE_BOOST = 0.02` (max 0.98). URLs are more specific for page captures than for file uploads or text pastes, justifying the bump.

The full pattern list lives in `classifier.py::_classify_from_source_url()`. Adding a platform is a matter of appending to `url_patterns`.

---

## Priority 4 — Browser Context

The copilot extension passes a `browser_context` string (`sentry`, `kibana`, `grafana`, `splunk`, `prometheus`, `datadog`, `jaeger`, `zipkin`). Confidence is lower than Priority 3 because the context is less specific than a URL.

| context | DataType | Confidence |
|---|---|---|
| `sentry` | `LOGS_AND_ERRORS` | 0.92 |
| `kibana` / `splunk` | `LOGS_AND_ERRORS` | 0.90 |
| `grafana` | `METRICS_AND_PERFORMANCE` | 0.90 |
| `prometheus` | `METRICS_AND_PERFORMANCE` | 0.92 |
| `datadog` | `METRICS_AND_PERFORMANCE` | 0.88 |
| `jaeger` / `zipkin` | `METRICS_AND_PERFORMANCE` | 0.85 |

---

## Priority 5 — Rule-Based Content + Extension

The catch-all. Runs when no stronger signal is available. Uses a sample of the first 5 KB of content plus the filename extension.

**File-upload boost.** When `source_metadata.source_type == "file_upload"`, add `FILE_UPLOAD_CONFIDENCE_BOOST = 0.03` to the final score (capped by type-specific max). Rationale: file extensions are trustworthy signals that pasted text and page captures don't carry.

### Pre-decision guard: documentation extensions skip the STRUCTURED_CONFIG branch

A `_doc_exts_early` set (`.md`, `.rst`, `.adoc`, `.txt`) is checked at the top of `_classify_with_rules` and, when matched, the STRUCTURED_CONFIG content heuristics are skipped. Without the guard, `.md` files containing YAML-looking fenced blocks or frontmatter would score high on the config detector and outrank the documentation path. The guard is an upstream filter, not a content veto — VISUAL_EVIDENCE / TRACE / PROFILING / etc. still run normally on those extensions.

### Decision order within Priority 5

1. **`VISUAL_EVIDENCE`** — image extension → 0.98+boost (max 0.99).
2. **`TRACE_DATA` vs `PROFILING_DATA`** — see [disambiguation helpers](#disambiguation-helpers).
3. **Command output** — see [command-output classification](#command-output-classification).
4. **`ERROR_REPORT` vs `LOGS_AND_ERRORS`** — stack trace *without* timestamps → ERROR_REPORT; timestamps + log-level patterns → LOGS_AND_ERRORS. `.log` extension lowers the required pattern count (1), `.txt` needs more (2), others need strong patterns (≥3).
5. **`METRICS_AND_PERFORMANCE`** — CSV/TSV with metric-keyword density, or JSON-array time-series shape.
6. **`STRUCTURED_CONFIG`** — config extension (`.yaml/.yml/.json/.toml/.ini/.env/.config`) → 0.92+boost; or ≥2 config patterns → 0.75+boost.
7. **`SOURCE_CODE`** — code extension → 0.95+boost; or ≥2 code patterns → 0.80+boost.
8. **HTML → `DOCUMENTATION`** — `.html/.htm` extension or ≥3 HTML structural patterns → 0.88+boost. Detected *before* generic documentation/text so page-like content is not misclassified.
9. **`DOCUMENTATION` vs `UNSTRUCTURED_TEXT`** — `.md/.rst/.adoc` or (≥2 markdown patterns + prose density > 0.1) → DOCUMENTATION. Otherwise `.txt` or ≥1 markdown pattern → UNSTRUCTURED_TEXT at 0.72.
10. **CSV/TSV structural gate** — see [disambiguation helpers](#disambiguation-helpers).
11. **Best-effort fallback** — if any content score ≥1, return the winning type with confidence 0.50 and `source="rule_based_best_effort"`, `classification_failed=True`.
12. **True fallback** — nothing matched: `UNSTRUCTURED_TEXT` at 0.30, `classification_failed=True`.

### Extension-sensitive LOGS_AND_ERRORS thresholds

| Extension | Patterns required | Confidence ramp |
|---|---|---|
| `.log` | 1 text OR 1 structured | 0.88 – 0.97 (`0.88 + min(score, 3) * 0.03`) |
| `.txt` | 2 text OR 2 structured | 0.85 – 0.94 |
| other | 3 text OR 3 structured | 0.88 – 0.95 |

This encodes the intuition that `.log` is itself strong evidence, `.txt` is ambiguous, and unknown extensions need to earn the classification through content patterns alone.

---

## Command-Output Classification

Tier 0 recognizes **14 Linux/Unix commands** via `COMMAND_OUTPUTS` (classifier.py). Each command requires **≥2 pattern matches** to classify — this limits false positives from content that incidentally contains a single header-like line.

Commands route to the detailed `DataType` whose extractor produces the best structural index:

| Commands | Detailed type | Extractor | Rationale |
|---|---|---|---|
| `top`, `ps`, `vmstat`, `iostat`, `netstat`, `free`, `df`, `lsof` | `COMMAND_OUTPUT` | CommandOutputExtractor | Tabular resource-monitoring output; extractor parses process tables, IO stats, disk usage |
| `dmesg`, `journalctl`, `strace`, `ltrace` | `LOGS_AND_ERRORS` | LogsAndErrorsExtractor | Time-ordered log-like output with severity/context |
| `perf` | `PROFILING_DATA` | ProfilingExtractor | Performance counter / hotspot data |
| `lscpu` | `STRUCTURED_CONFIG` | ConfigExtractor | Machine-inventory key-value output |

Pattern table (excerpts — see `COMMAND_OUTPUTS` in `classifier.py` for the authoritative set):

```python
"top": {
    "type": DataType.COMMAND_OUTPUT,
    "patterns": [
        r"top\s+-\s+\d{2}:\d{2}:\d{2}\s+up",
        r"Tasks:\s+\d+\s+total,\s+\d+\s+running",
        r"%Cpu\(s\):",
        r"KiB Mem\s*:",
        r"PID\s+USER\s+PR\s+NI\s+VIRT\s+RES",
    ],
    "confidence": 0.95,
},
"dmesg": {
    "type": DataType.LOGS_AND_ERRORS,
    "patterns": [
        r"^\[\s*[\d\.]+\]",
        r"\bkernel:\s",
        r"\bLinux version\s",
    ],
    "confidence": 0.95,
},
"lscpu": {
    "type": DataType.STRUCTURED_CONFIG,
    "patterns": [
        r"Architecture:\s+\w+",
        r"CPU\(s\):\s+\d+",
        r"Model name:\s+",
    ],
    "confidence": 0.95,
},
```

Adding a command means appending to `COMMAND_OUTPUTS` with ≥2 patterns and a confidence.

---

## Disambiguation Helpers

### `_disambiguate_profiling_vs_trace()`

Both data types have numeric ID patterns that can look similar. Rules:

- **Trace wins** if ≥2 of four patterns match:
  - `(traceId|trace_id)["\s:]+[a-f0-9]{32}` — OpenTelemetry trace IDs
  - `(spanId|span_id|parentId)["\s:]+[a-f0-9]{16,}` — span IDs
  - `"(serviceName|service\.name)"` — service mesh identifier
  - `"operationName".*"spans":\s*\[` — Jaeger structure
- Otherwise, **profiling wins** if ≥1 of four patterns match:
  - `\bncalls\s+tottime\s+percall\s+cumtime` — cProfile header
  - `[\w\.]+(?:;[\w\.]+)+\s+\d+` — flame-graph syntax
  - `\d+\s+calls?\s+in\s+[\d\.]+\s+seconds` — profiling summary
  - `filename:lineno\(function\)` — cProfile format

Confidence: TRACE_DATA at 0.95+boost, PROFILING_DATA at 0.92+boost.

### CSV/TSV structural gate

Best-effort fallback for `.csv/.tsv` files uses **structural analysis** rather than vocabulary scoring. The reason: cell content produces cross-type false positives ("error" → LOGS, "interface" → CODE, "cpu" → METRICS).

A CSV/TSV file is classified as `METRICS_AND_PERFORMANCE` (at 0.55, `classification_failed=True`) when **all three** hold:

1. **Structural breadth**: ≥5 columns (a data export, not a lookup table).
2. **Data density**: ≥10 data rows in the sample (a real dataset, not a stub).
3. **Numeric/metric evidence**: numeric-cell ratio ≥10% OR ≥1 metrics-vocabulary keyword matched.

Otherwise the file falls back to `UNSTRUCTURED_TEXT` at 0.45, also with `classification_failed=True`. Both paths populate `suggested_types` and surface cooperative-clarification suggestions so the user can re-prompt with the correct type.

---

## Confidence Thresholds

Named in `CONFIDENCE_THRESHOLDS` (classifier.py):

```python
CONFIDENCE_THRESHOLDS = {
    "classification_failed": 0.50,   # below → classification_failed=True → cooperative-clarification suggestions
    "auto_accept": 0.85,             # at/above → auto-accept in downstream UX
}
```

Thresholds are named so downstream UX (cooperative-clarification injector, agent tools) can reason about confidence bands symbolically.

A third threshold — `LOW_CONFIDENCE_THRESHOLD = 0.65` — lives in `core/preprocessing/evidence_metadata.py` and captures the "classified-but-shaky" band between `auto_accept` (0.85) and `classification_failed` (0.50). When the per-attempt confidence falls in `[0.50, 0.65)`, the context_builder attaches `confidence="low"` to the `<evidence>` element in the agent prompt so the LLM treats the row as a hint rather than an assertion. Above 0.65, no marker is attached.

### Confidence boosts by content origin

The `_origin_boost()` step in `_classify_with_rules` adds a small confidence bump for signals that ride alongside the content:

| Origin | Boost | Reason |
| --- | --- | --- |
| `file_upload` | `FILE_UPLOAD_CONFIDENCE_BOOST = 0.03` | A real filename + extension is a weak but real signal. |
| `page_capture` | `PAGE_CAPTURE_CONFIDENCE_BOOST = 0.02` | Captured DOM carries structural hints (panels, headings) the raw classifier underweights. |
| `text_paste` / chat-paste | `TEXT_PASTE_CONFIDENCE_BOOST = 0.0` | **Deliberately zero.** Paste content is format-neutral — there is no filename, no extension, no source URL. A nonzero boost would override rule-based signals that are doing the actual work. ISS-053. |

The explicit zero is documented as a constant rather than left implicit because the boost table is the single audit point operators check when classification confidence looks off.

---

## `classification_failed` Path (Cooperative Clarification)

When classification produces `confidence < 0.50`, or when the CSV/TSV structural gate yields low-confidence results, `classification_failed=True`. A third trigger — **short-text ambiguity** — also forces this path:

| Constant | Value | Role |
| --- | --- | --- |
| `_SHORT_TEXT_MAX_CHARS` | 1500 | Upper bound on content length for the guard to fire. |
| `_SHORT_TEXT_MAX_LINES` | 25 | Upper bound on line count for the guard to fire. |
| `_AMBIGUITY_MIN_DISTINCT_CATEGORIES` | 3 | Min number of distinct rule-based categories whose patterns scored on the sample for the content to be flagged ambiguous. |

When a `.txt`-extension paste lands inside both size bounds **and** triggers patterns in 3 or more distinct categories (e.g. a 20-line snippet that matches log, config, and code patterns simultaneously), `_assess_short_text_ambiguity()` forces `confidence = 0.40` and re-emits the result with `classification_failed=True` so the cooperative-clarification path runs. Without the guard, the rule-based scorer often returns one of the matched categories at 0.60+ on weak evidence and the user never sees the "this is ambiguous" affordance. ISS-023.

Inside the guard, the per-category pattern checks themselves have signal-strength bars: **log-line shape requires at least two line-start datetime matches** before LOGS_AND_ERRORS counts as a candidate (ISS-050). A single datetime inside prose — e.g. a maintenance-window header like "scheduled for 2026-05-14 14:00 UTC" — is too weak; counting it would mis-suggest LOGS for any short notice that mentions a date. URL, email, code-fence, JSON-shape, and key:value patterns each contribute a single category vote on first match.

Every classification_failed path populates `ClassificationResult.suggested_types` with 2–3 candidate DataTypes from the scoring pass. The service short-circuits extraction and returns a placeholder `PreprocessingResult` with:

- `extraction_method = "classification_failed"`
- Content: a user-facing text like `[Classification uncertain for 'foo.csv' — requesting user input] Suggested types: metrics_and_performance, unstructured_text`
- `extraction_metadata.suggested_types`: list of candidate DataType values (as strings)

The agent still runs the turn using its file-reading tools (`search_file`, `deep_analysis`), producing a best-effort answer from the raw bytes. After the turn runs, `InvestigationService._build_classification_clarification` injects **DECIDE suggestions** ahead of the engine's follow-ups in `TurnResponse.suggested_actions`.

**One set of cards per failed attachment, not per turn.** The `files` cap is `maxItems: 1`, but `pasted_content` is a separate form field that legitimately rides alongside a file, and the paste arm reaches `classification_failed` on its own — so a turn carries up to **two** attachments and both can fail. Each failed attachment gets up to 3 type-specific cards plus a **"Something else"** fallback, so a paste+file turn where both fell below threshold emits up to **8** cards spanning two attachments (#1222). The emitter does not reason from any field's cap; it clarifies whatever failed.

Each card carries:

| Field | Content |
|---|---|
| `payload` | The click-to-send message, naming the attachment the way the **user** knows it (`_clarification_subject`): a real file is *"Treat the file you shared (\"foo.csv\") as metrics or performance data."*; a paste is *"Treat the text you pasted as …"*, a capture *"Treat the page you captured as …"*. Never the minted `pasted-content-<ts>.txt` transport name (#1198/#666). |
| `label` | The button. Bare (*"Metrics"*) while one attachment is on offer; otherwise qualified with the attachment's short name — *"Metrics (foo.csv)"*, *"Metrics (pasted text)"* — because two cards reading *"Metrics"* are indistinguishable both on screen and in the resolver's numbered choice list. When the set spans **turns**, the qualifier also carries the turn (*"Metrics (pasted text, turn 4)"*): two pastes are both "pasted text", so only the turn separates them. |
| `intent` | The engine-owned `file_reclassification` intent — `{"type": "file_reclassification", "file_id": …, "data_type": …}`. |

**The intent is the contract, not the text.** Every card has carried a `file_reclassification` intent since the cross-client resolution contract landed. Clients forward it on click, and the SERVICE handler (`_handle_file_reclassification`) re-runs preprocessing under `user_override` **mechanically — no LLM call**, so the choice can never be misread as an analysis request (e.g. deep-analyzing the file instead of re-labeling it). The `payload` remains the human-readable record of the choice; intent routing takes precedence over it server-side. A user who *types* a choice instead of clicking reaches the same handler through `IntentResolver` — see [choice-response-resolution.md](../investigation-engine/choice-response-resolution.md).

**The question outlives its turn, for a bounded number of turns.** `case.last_suggestions` is server-side memory (never rendered — the cards come from the TurnResponse), and it is rebuilt every turn, so a clarification the user did not answer used to vanish the moment anything else happened: answering one of two questions deleted the other (#1222), and ignoring the question entirely deleted it outright (#1245). Every stored entry now carries the turn that offered it (`offered_turn`) and the target file's `data_type` at that moment (`offered_data_type`), and an unanswered clarification is carried forward while all of:

- it is at most `_CLARIFICATION_CARRY_TURNS` (**3**) turns old — offered on turn *T*, answerable on *T+1…T+3*;
- at most `_CLARIFICATION_SPAN_CAP` (**3**) attachments are on offer, newest offer first, whole attachments admitted or dropped;
- the target file is still in the case, the case is not terminal, and the file's `data_type` still matches `offered_data_type` — a change means the file was reclassified by *some* path, so the question is answered, whoever answered it.

Engine follow-ups (confirmation, status transition) are not clarifications and expire after one turn. An entry with no `offered_turn` is treated as expired: it was not written by the turn seam, so nothing knows what turn it belongs to.

When the user clicks a card, the next turn resolves the intent structurally. No re-classification at the classifier layer, no new LLM integration — deterministic post-turn injection using the existing DECIDE suggestion plumbing.

A rejected alternative: an "LLM rescue" pass that would auto-classify ambiguous files via a cheap LLM call. Rejected because DECIDE suggestions are zero-cost, user-authoritative (ground truth), and have no telemetry prerequisite. See [data-preprocessing-design-specification.md](./data-preprocessing-design-specification.md) §2.5.

A parallel path exists for `UNANALYZABLE`:

- `extraction_method = "none"`
- Content: `[File 'foo.bin' marked as UNANALYZABLE — reference only, no analysis performed]`
- Signals: VISUAL_EVIDENCE when vision is disabled, or a user-opted-out file type.

Both placeholder paths produce real `PreprocessingResult` objects so the downstream Evidence pipeline handles them uniformly.

---

## Telemetry

`DataClassifier.classify()` is decorated with `@_opik_track_classifier` (classifier.py). When Opik is installed **and** tracing is enabled (`OPIK_ENABLED=true`), every call produces a span tagged with:

- `source:<source>` — user_override / agent_hint / source_url / browser_context / rule_based / rule_based_best_effort
- `data_type:<detailed_value>` — e.g., `logs_and_errors`, `command_output`
- `confidence:<bucket>` — `high` (≥0.85), `medium` (0.50–0.85), `low` (<0.50)
- `classification_failed:<bool>`

This lets us query classification distributions (e.g., "what fraction of turns hit `rule_based_best_effort` last week?") and detect pattern drift without touching logs. When Opik is not installed the decorator is not applied at all, and when tracing is disabled the wrapper re-checks the flag on every call and runs the undecorated function — so no Opik client is constructed either way.

---

## Future Work

- **Classification telemetry dashboard.** Use the Opik tags above to visualise classification outcomes over time and flag pattern drift (e.g., new URL patterns that should be added, command-output variants that are being missed).
- **Cooperative-clarification "pick one" visual grouping** (frontend UX polish). Today up to 8 clarification suggestions — 3–4 per failed attachment, and a turn can carry two — render as a flat list of clickable bullets. Functionally correct, but not framed as mutually-exclusive alternatives, and with two attachments in play the grouping is carried only by the label qualifier (*"Metrics (foo.csv)"* vs *"Metrics (pasted text)"*). A `<ClarificationGroup>` component per attachment, with an "Is this …?" header naming its subject, would improve discoverability and make the per-attachment structure visible rather than inferred. Tracked as a Dashboard/Copilot frontend task; not blocking any backend behavior.
