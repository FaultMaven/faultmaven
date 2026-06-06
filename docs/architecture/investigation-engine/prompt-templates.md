# Prompt Templates Reference

> **Authoritative source:** `faultmaven/core/investigation/prompts/templates.py`
>
> This document describes the structure, dispatch, and shared constants of the FaultMaven prompt-template system. The actual prompt text lives in code — this doc explains how the pieces fit together and where each behavior is anchored.
>
> **Related docs:**
>
> - Stage duties and gate conditions: [`agent-stage-playbook.md`](./agent-stage-playbook.md)
> - Behavioral rules and their injection points: [`agent-behavioral-rules.md`](./agent-behavioral-rules.md)
> - Stage transitions: [`investigation-lifecycle-logic.md`](./investigation-lifecycle-logic.md)

---

## 1. Three-Template System

FaultMaven uses three top-level prompt templates, dispatched by case state:

| Template | Used when | Stage instructions |
| --- | --- | --- |
| `INQUIRY_TEMPLATE` | `case.state == INQUIRY` | Self-contained (problem detection, formalization, confirmation handshake) |
| `INVESTIGATION_BASE` | `case.state == INVESTIGATING` | Adaptive — see §3 |
| `TERMINAL_TEMPLATE` | `case.state in {RESOLVED, CLOSED}` | Self-contained (read-only Q&A, report regeneration acknowledgment) |

Fallback variants (`FALLBACK_INQUIRY_TEMPLATE`, `FALLBACK_INVESTIGATION_TEMPLATE`, `FALLBACK_TERMINAL_TEMPLATE`) are used only when the primary assembly fails (token limit overflow or provider error).

---

## 2. Cross-Phase Shared Constants

To prevent drift between templates that share behavior, the module defines several constants string-concatenated into the templates that need them. Each is the **single source of truth** for its specific rule.

| Constant | Purpose | Used in |
| --- | --- | --- |
| `_ADVISOR_ROLE_CONSTRAINT` | Banned phrases ("Let me check", "I will run") + advisor-vs-actor framing | INQUIRY + INVESTIGATION_BASE + TERMINAL |
| `_ACTIVE_ADVISOR_ROLE_BLOCK` | Wraps `_ADVISOR_ROLE_CONSTRAINT` with SUGGEST/ASK pattern + BAD/GOOD examples | INQUIRY + INVESTIGATION_BASE |
| `_ACTION_IMPACT_BLOCK` | Diagnostic-vs-state-modifying classification + impact annotation | INQUIRY + INVESTIGATION_BASE |
| `_READING_DISCIPLINE_BLOCK` | Signal Extraction (Rule 7) + Full-Context Reasoning (Rule 8) | INQUIRY + INVESTIGATION_BASE |
| `_DATA_CITATION_RULE` | "Cite actual values from the structural index" specificity rule | INQUIRY TRIAGE SUMMARY + INVESTIGATION_BASE WORKING WITH EVIDENCE DATA |
| `_FOLLOW_UP_SUGGESTIONS_BLOCK` | COOPERATIVE / EVIDENCE / FREE_SPEECH suggestion definitions | INQUIRY + INVESTIGATION_BASE |
| `_AMBIGUITY_FIRST_RULE` | State-change ambiguity rule (require explicit directive) | INQUIRY + TREATMENT_INSTRUCTIONS |
| `_FILE_SELECTION_DEFAULT` | "Default search target: the file uploaded this turn" rule | `_EVIDENCE_GROUNDING_BLOCK` + `_RCA_DIAGNOSIS_BLOCK` SEARCH STRATEGY |
| `_EVIDENCE_GROUNDING_BLOCK` | Anti-hallucination hard constraints, USING EVIDENCE DATA by question type, 4-step procedure, EXAMPLES | INVESTIGATION_BASE via `{evidence_grounding}` placeholder |
| `_DIAGNOSTIC_REASONING_BLOCK` | OBSERVATION → ANALYSIS → CONCLUSION + confidence calibration + no premature resolution + PROHIBITED PATTERNS | INVESTIGATION_BASE via `{diagnostic_reasoning}` placeholder |

The two constants ending in `_BLOCK` and injected via placeholders (`_EVIDENCE_GROUNDING_BLOCK` and `_DIAGNOSTIC_REASONING_BLOCK`) are gated to `""` in `knowledge_query` mode — see §4.

### 2.1 XML Element Conventions in `<evidence_collected>`

`build_investigation_context()` renders evidence and uploaded files into a `<evidence_collected>` XML envelope. The element names and attribute names are load-bearing — the templates reference them by name when telling the LLM how to read context and how to populate `source_file_id` on `evidence_to_add`. They must stay in lockstep with the emitter in `prompts/context_builder.py`.

| Element | Phase | Attributes | Notes |
| --- | --- | --- | --- |
| `<uploaded_file …>` | INQUIRY (and INVESTIGATING when no Evidence rows exist yet) | `file_id="file_…"`, `filename`, `data_type`, `searchable="true"` | Surfaced when `case.evidence` is empty but `case.uploaded_files` carry a non-trivial `structural_index`. The file id is exposed under `file_id`, matching the attribute name used on `<evidence>` so the source_file_id rule is phase-uniform. |
| `<evidence …>` | INVESTIGATING | `id="ev_…"` (evidence id), `file_id="file_…"` (source file FK), `data_type`, `searchable`, `confidence` | The `id=` attribute is the evidence id; `file_id=` is the FK back to `uploaded_files`. The LLM passes either value into `search_file`'s `evidence_id` parameter — the tool resolves both forms via `search_file_tool`'s dual-resolution path. |

**Rule:** when the LLM is asked to populate `evidence_to_add.source_file_id`, the templates instruct it to copy verbatim from the `file_id` attribute on either element. The pre-existing `evidence_id` parameter name on `search_file` is a naming artifact; the tool accepts both an `ev_…` and a `file_…` value, so the templates can speak in `file_id` terms uniformly without renaming the tool API.

The INQUIRY template includes a SEARCHING UPLOADED FILES block that codifies this contract (and explicitly steers the agent to `search_file` for count queries — `<file_extract>` is a structural summary, not an authoritative count source).

---

## 3. INVESTIGATION_BASE Structure

`INVESTIGATION_BASE` is the most complex template because it must serve four stages plus the knowledge-query bypass. The same outer shell is reused; what differs is the `{adaptive_instructions}` payload and which optional blocks are present.

### 3.1 Block order

The template is structured so the LLM reads input-handling and evidence-classification rules **before** its stage-specific task, then output-shaping rules last so they're freshest when composing the response.

```text
CONTEXT HEADER (dynamic, ~2-5K+ tokens)
  STATUS: INVESTIGATING
  Identity, case context, milestones, evidence,
  entity highlights, hypotheses, investigation journal,
  working conclusion, pending action,
  conversation history, system feedback, user message

INPUT HANDLING
  READING DISCIPLINE                              (_READING_DISCIPLINE_BLOCK)

EVIDENCE INTERPRETATION (rules-before-task)
  {evidence_grounding}                            (_EVIDENCE_GROUNDING_BLOCK, gated)
  EVIDENCE FROM ATTACHMENTS
  WORKING WITH EVIDENCE DATA                      (uses _DATA_CITATION_RULE)
  EVIDENCE CLASSIFICATION — DECISION TREE
  CREATING EVIDENCE RECORDS                       (evidence_to_add schema)
  EVIDENCE SUMMARY QUALITY
  INVESTIGATION JOURNAL                           (journal_entries schema)
  PROACTIVE BLOCKER DETECTION                     (missing_critical_data)

STAGE INSTRUCTIONS
  YOUR TASK: {adaptive_instructions}              (see §3.2)

CROSS-STAGE PRINCIPLES
  KEY PRINCIPLES                                  (8 bullets — see below)
  FOLLOW-UP SUGGESTIONS                           (_FOLLOW_UP_SUGGESTIONS_BLOCK)
  MILESTONE ATTRIBUTION

OUTPUT SHAPING
  ASSISTANT ROLE                                  (_ACTIVE_ADVISOR_ROLE_BLOCK)
  ACTION IMPACT                                   (_ACTION_IMPACT_BLOCK)
  CONCISENESS
  {diagnostic_reasoning}                          (_DIAGNOSTIC_REASONING_BLOCK, gated)
  CRITICAL: REASONING-FIRST REQUIREMENT           (internal_reasoning emission gate)

SECURITY
  <security_constraints>                          (7 immutable rules)

TAIL
  CRITICAL: Do NOT restate or summarize...        (anti-padding closer)
```

**KEY PRINCIPLES bullets** (cross-stage, always present in INVESTIGATION_BASE):

1. Evidence-Driven Progress (no evidence = indicator stays False)
2. NAME THE NEXT DATA POINT (substantive-turn gated)
3. ONE PRIMARY ASK
4. Evidence requests should be specific and actionable
5. Maintain a working conclusion at all times
6. GRACEFUL PIVOT (user can't / won't provide data)
7. ACKNOWLEDGE CORRECTIONS (user contradicts a prior claim)
8. CHECK BACK ON SUGGESTED ACTIONS (user reply doesn't reference a prior diagnostic suggestion; exception: Zone 3 compliance hold)
9. WORK WITH WHAT YOU GET (catch-all for messy/partial input)

Items 6–9 form a progression: user **can't** → user **contradicts** → user **ignores** → catch-all.

### 3.2 Adaptive instructions

The `{adaptive_instructions}` placeholder is filled by `_select_diagnosis_block(case)` on DIAGNOSIS turns and by stage-specific constants elsewhere. Under the unified opportunistic flow ([investigation-flow-redesign.md](./investigation-flow-redesign.md)) the path fork is retired: `_select_diagnosis_block` is now a thin wrapper that always assembles the single unified DIAGNOSIS block (it kept its name but no longer selects a path).

| Stage / mode | Adaptive instructions |
| --- | --- |
| DIAGNOSIS | `_get_diagnosis_focus_emphasis(progress)` + `_RCA_DIAGNOSIS_BLOCK` |
| MITIGATION | `MITIGATION_INSTRUCTIONS` |
| TREATMENT | `TREATMENT_INSTRUCTIONS` |
| Knowledge query | `KNOWLEDGE_QUERY_INSTRUCTIONS` |

`_RCA_DIAGNOSIS_BLOCK` is composed from a shared vocabulary of sub-blocks (`_DIAGNOSIS_ZONES_PREAMBLE`, `_EVIDENCE_REQUEST_FORMAT_BLOCK`, `_URGENCY_RECOGNITION_BLOCK`). The hypothesis-creation mandate (`_HYPOTHESIS_EVIDENCE_ORDERING_BLOCK`) is contained inside it and reached on every DIAGNOSIS turn — the former path-conditional blocks (`_SYMPTOM_VALIDATION_BLOCK`, `_GATE3_PENDING_BLOCK`, `_POST_MITIGATION_RCA_PREFIX`) and their pre-mitigation emission ban were removed. See `agent-stage-playbook.md` for the current DIAGNOSIS routing.

`_get_diagnosis_focus_emphasis(progress)` prepends a Zone-aware progress signal:

| Zone | Condition | Prepended emphasis |
| --- | --- | --- |
| Zone 1 | `symptom_verified=False` | "Symptom verification pending — search for evidence the problem exists" |
| Zone 2 | `symptom_verified=True`, `cause_state != IDENTIFIED` | "Root cause analysis — form hypotheses, search for causal evidence" |
| Zone 3 | `cause_state == IDENTIFIED`, `solution_proposed=False` | "Solution needed — propose a concrete, executable fix" |
| Zone 3 pending | `solution_proposed=True` | "Solution proposal issued — awaiting execution. Do not request further evidence or introduce alternative proposals." |

(The zone conditions now read the engine-derived `cause_state` enum, not the removed `root_cause_identified` boolean.)

---

## 4. `knowledge_query` Mode Bypass

When `processing_mode == "knowledge_query"`, the user is asking a general technical question rather than progressing the investigation. The dispatcher:

1. Sets `adaptive_instructions = KNOWLEDGE_QUERY_INSTRUCTIONS`. This block waives evidence-grounding and diagnostic-reasoning expectations: *"The DIAGNOSTIC REASONING REQUIREMENTS and EVIDENCE GROUNDING rules do not apply. Connect to the case context when relevant — but this is optional."*
2. Sets `evidence_grounding = ""` so `_EVIDENCE_GROUNDING_BLOCK` is absent from the rendered prompt.
3. Sets `diagnostic_reasoning = ""` so `_DIAGNOSTIC_REASONING_BLOCK` is absent from the rendered prompt.

**Why suppress rather than exempt:** earlier versions kept the rule blocks present and stated "the above rules don't apply." The result was ~4KB of waived rule text alongside a waiver — high signal/noise. The current design omits the waived blocks entirely. The waiver line in `KNOWLEDGE_QUERY_INSTRUCTIONS` remains as a hint that the rules exist in other modes, but the bulk doesn't.

**What stays in INV_kq mode:** READING DISCIPLINE, the evidence-handling rules (still useful if the user pivots to a case-specific question), KEY PRINCIPLES (with `NAME THE NEXT DATA POINT` self-gating via "skip for general-knowledge questions"), FOLLOW-UP SUGGESTIONS, ASSISTANT ROLE, ACTION IMPACT, CONCISENESS, CRITICAL: REASONING-FIRST REQUIREMENT (conditional — inert when no milestones advance), and `<security_constraints>`.

---

## 5. Dispatch: `get_prompt_for_case()`

The single entry point is `templates.get_prompt_for_case(case, user_message, ...)`. It:

1. Builds the dynamic context via `build_investigation_context(...)` from `prompts/context_builder.py`.
2. Selects the template based on `case.state`:
   - `INQUIRY` → `INQUIRY_TEMPLATE.format(**ctx)`
   - `INVESTIGATING` → see step 3
   - `RESOLVED` / `CLOSED` → `TERMINAL_TEMPLATE.format(...)`
3. For INVESTIGATING:
   - Picks `adaptive_instr` per stage (DIAGNOSIS / MITIGATION / TREATMENT) or replaces it entirely with `KNOWLEDGE_QUERY_INSTRUCTIONS` when `processing_mode == "knowledge_query"`.
   - Sets `evidence_grounding` and `diagnostic_reasoning` to either their respective `_*_BLOCK` constants or `""` based on the same `is_knowledge_query` flag.
   - Renders `INVESTIGATION_BASE.format(adaptive_instructions=..., evidence_grounding=..., diagnostic_reasoning=..., **ctx)`.

The dispatcher is the only place where mode-conditional gating happens. The templates themselves are mode-agnostic — they only know how to interpolate their placeholders.

---

## 6. Fallback Templates

When the primary template renders too long for the model's context window, or when an unrecoverable rendering error occurs, the engine falls back to a minimal variant via `get_fallback_prompt_for_case(case, user_message)`:

| Status | Fallback |
| --- | --- |
| INQUIRY | `FALLBACK_INQUIRY_TEMPLATE` |
| INVESTIGATING | `FALLBACK_INVESTIGATION_TEMPLATE` |
| RESOLVED / CLOSED | `FALLBACK_TERMINAL_TEMPLATE` |

Fallback templates carry only the load-bearing safety constraints (no confabulation, hypothesis-evidence ordering for INVESTIGATING, closed-case boundary for TERMINAL). They produce shorter prompts at the cost of richer behavioral guidance — a degraded but safe mode.

---

## 7. Token-Reduction Trade-offs

The current design prioritizes signal density over example coverage in shared blocks. Specific choices worth knowing:

- **`_EVIDENCE_GROUNDING_BLOCK` USING EVIDENCE DATA section** is condensed — 6 question types (characterization / retrieval / count / temporal / file-internal identifier) with one-line rules each, rather than per-type runbooks. Three load-bearing caveats are preserved verbatim: `search_file` returns max 20 results by default; the IP auth breakdown table vs. "Distinct IPs" line-occurrence distinction; and the "internal/undocumented identifier" callout.
- **CONCISENESS** is a single sentence rather than a bullet list — the bullet list version ironically diluted its own message.
- **`_RCA_DIAGNOSIS_BLOCK` retains its own `FOLLOW-UP AFTER USER ACTIONS` block** (Zone 1/2-scoped with Zone 3 exclusion). The general `FOLLOW-UP REQUIREMENTS` block that previously appeared in `INVESTIGATION_BASE` was removed because each stage handles result-verification in its own playbook (MITIGATION's *Track Mitigation Progress*, TREATMENT's *Verify Result*). The KEY PRINCIPLES `CHECK BACK ON SUGGESTED ACTIONS` bullet covers the cross-stage gap where the user's reply doesn't reference a prior diagnostic suggestion.

---

## 8. Audit Invariants

For any rendering audit (e.g., regression testing the templates after edits), the following invariants should hold across the 8 dispatch paths (INQUIRY, INV_kq, DIAG_Z1/Z2/Z3, MITIGATION, TREATMENT, TERMINAL):

- No stale v2 references (`_check_fast_track_resolution`, `KB_FAST_TRACK`, `INQUIRY → RESOLVED` edge).
- `**KB-RESOLUTION VARIANT` only in TREATMENT.
- `EVIDENCE GROUNDING (CRITICAL - Anti-Hallucination):` only in case-investigating modes (DIAG, MIT, TRE) — absent from INV_kq, INQUIRY, TERMINAL.
- `DIAGNOSTIC REASONING REQUIREMENTS (Anti-Hallucination):` only in case-investigating modes — absent from INV_kq, INQUIRY, TERMINAL.
- 4-step procedure (`1. Identify the next data point` ... `4. Only ask the user`) present in DIAG_Z1/Z2/Z3, MITIGATION, TREATMENT.
- `_FILE_SELECTION_DEFAULT` canonical text count per path: DIAG×2, MIT/TRE×1, INV_kq/INQUIRY/TERMINAL×0.
- `SEARCHING UPLOADED FILES` block present only in INQUIRY; references `<uploaded_file file_id=…>` and `<evidence id="ev_…" searchable="true">` (no `evidence_id=` attribute name — see §2.1).
- `source_file_id` description in evidence-creation prose references `file_id="…"` on both `<evidence>` and `<uploaded_file>` (the two elements share the attribute convention).
- All `.format()` calls render without `KeyError` / `IndexError` when given empty-string values for every placeholder.

Engine tests `tests/unit/core/investigation/test_indicator_evaluator.py` and `tests/unit/modules/agent/tools/test_kb_qa_cause_parsing.py` (20 tests total) exercise the dispatcher and indirectly validate template renderability.
