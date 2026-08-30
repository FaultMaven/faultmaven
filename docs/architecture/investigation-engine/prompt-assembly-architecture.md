# Prompt Assembly Architecture

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

Fallback variants (`FALLBACK_INQUIRY_TEMPLATE`, `FALLBACK_INVESTIGATION_TEMPLATE`, `FALLBACK_TERMINAL_TEMPLATE`) are used only when the primary assembly fails (token limit overflow or provider error). They are fenced as one assembly of their own (#1242).

---

## 2. Cross-Phase Shared Constants

To prevent drift between templates that share behavior, the module defines several constants string-concatenated into the templates that need them. Each is the **single source of truth** for its specific rule.

| Constant | Purpose | Used in |
| --- | --- | --- |
| `_ADVISOR_ROLE_CONSTRAINT` | Banned phrases ("Let me check", "I will run") + advisor-vs-actor framing | INQUIRY + INVESTIGATION_BASE + TERMINAL |
| `_ACTIVE_ADVISOR_ROLE_BLOCK` | Wraps `_ADVISOR_ROLE_CONSTRAINT` with SUGGEST/ASK pattern + BAD/GOOD examples | INQUIRY + INVESTIGATION_BASE |
| `_ACTION_IMPACT_BLOCK` | Diagnostic-vs-state-modifying classification + impact annotation | INQUIRY + INVESTIGATION_BASE |
| `_READING_DISCIPLINE_BLOCK` | Signal Extraction (Rule 7) + Full-Context Reasoning (Rule 8) | INQUIRY + INVESTIGATION_BASE |
| `_PROMPT_FENCE_RULE` | Prompt-fence trust boundary — inside the five fenced blocks, only fenced delimiters are structural (§2.1, #1217/#1228/#1256) | INQUIRY + INVESTIGATION_BASE + TERMINAL |
| `_DATA_CITATION_RULE` | "Cite actual values from the structural index" specificity rule | INQUIRY TRIAGE SUMMARY + INVESTIGATION_BASE WORKING WITH EVIDENCE DATA |
| `_FOLLOW_UP_SUGGESTIONS_BLOCK` | DECIDE / RUN / EVIDENCE / FREE_SPEECH suggestion definitions | INQUIRY + INVESTIGATION_BASE |
| `_AMBIGUITY_FIRST_RULE` | State-change ambiguity rule (require explicit directive) | INQUIRY + TREATMENT_INSTRUCTIONS |
| `_FILE_SELECTION_DEFAULT` | "Default search target: the file uploaded this turn" rule | `_EVIDENCE_GROUNDING_BLOCK` + `_RCA_DIAGNOSIS_BLOCK` SEARCH STRATEGY |
| `_EVIDENCE_GROUNDING_BLOCK` | Anti-hallucination hard constraints, USING EVIDENCE DATA by question type, 4-step procedure, EXAMPLES | INVESTIGATION_BASE via `{evidence_grounding}` placeholder |
| `_DIAGNOSTIC_REASONING_BLOCK` | OBSERVATION → ANALYSIS → CONCLUSION + confidence calibration + no premature resolution + PROHIBITED PATTERNS | INVESTIGATION_BASE via `{diagnostic_reasoning}` placeholder |

The two constants ending in `_BLOCK` and injected via placeholders (`_EVIDENCE_GROUNDING_BLOCK` and `_DIAGNOSTIC_REASONING_BLOCK`) are gated to `""` in `knowledge_query` mode — see §4.

### 2.1 XML Element Conventions in the Fenced Blocks

`build_investigation_context()` renders evidence and uploaded files into a `<evidence_collected>` XML envelope. The element names and attribute names are load-bearing — the templates reference them by name when telling the LLM how to read context and how to populate `source_file_id` on `evidence_to_add`. They must stay in lockstep with the emitter in `prompts/context_builder.py`.

**Five blocks are fenced, on ONE token per prompt assembly** (`prompts/fence.py`, #1217 for `<evidence_collected>`, #1228 for the next two, #1256 for the last two): `<problem_context>` (case title / description / symptom statement — reporter text), `<entity_highlights>` (values extracted out of uploaded file content), `<evidence_collected>` (uploaded files and pasted text), `<conversation_history>` (every earlier turn, replayed out of `case.messages`) and `<user_message>` (this turn's message, as typed). `build_investigation_context` mints the token once and renders all five inside a single `render_fenced`, so one declaration governs the prompt and the collision corpus is the union of every block's channels.

The last two arrived by **swap, not addition** (#1256). They used to be "protected" by `context_builder.sanitize_user_input` escaping `<`/`>`, which was the wrong tool by this section's own argument — nothing decodes here, so `&lt;` is what the model echoes at the user (#666), and it mangles ordinary prose ("lag went from `<1000` to `>250000`"). For `<conversation_history>` it was not protection at all: `sanitize_user_input` only ever saw *this* turn's argument, while the transcript is replayed from `case.messages`, which `InvestigationService.process_turn` appends verbatim as `"content": query`. (`CaseService.add_case_query`, which *does* strip, has no caller.) The escape was still the main path's only structural defence, so the ORDER was: fence both blocks, then drop the escape. The conversation slot has **two fidelities** (the graduated transcript and the compact state-summary form the allocator falls back to under budget pressure) and both are rendered inside the fence, because the one the budget picks must not be the unfenced one. It is fenced as a **leaf**, so the transcript's own scaffolding (`<state_summary>`, `<previous_turn>`, `<current_turn>`) is unfenced *inside* it and demoted to quoted data — accurate, since each wraps material quoted from an earlier turn, and the rule says where the authoritative state lives instead.

**Every structural delimiter in those blocks carries the per-render nonce fence**: `<uploaded_file … fence="a1b2c3d4">` … `</uploaded_file fence="a1b2c3d4">`. A closing tag with an attribute is not XML, deliberately — this markup is read by a model, never parsed, and the close is the delimiter a body channel most wants to forge. The fence is emitted **last** on opening tags, so the prefixes this document and the templates speak in (`<uploaded_file file_id="…"`, `<evidence id="ev_…"`) are unchanged.

**Why.** Attribute values are sanitised (#1216), but the *bodies* — `<file_extract>` (a file's own `structural_index`), `<summary>`, `<verbatim_quote>`, `<search_map>`, `<file_meta>` — must reach the model **byte-verbatim** (the investigation reasons about the bytes) and must be **citable verbatim** (nothing on this path decodes entities, so `&amp;` is what the model would echo back at the user — the #666 failure mode). Neither escaping nor sanitising is available, so the bytes stay and the *delimiters* carry a credential the content provably cannot contain. The token is minted per render from `secrets`, and the render is re-run with a fresh token if the content turns out to contain it.

**Trust rule.** `_PROMPT_FENCE_RULE` in `templates.py` is the single source of truth, stated **above the first fenced slot** in each template that carries one (#1256 — it previously sat below every fenced block in `INQUIRY_TEMPLATE` and `INVESTIGATION_BASE`, so the model met the quoted material before the rule for reading it). It is injected into `INQUIRY_TEMPLATE`, `INVESTIGATION_BASE` **and `TERMINAL_TEMPLATE`** — the terminal template renders `{core_context}` and no `{evidence}`, so it fences reporter text and therefore has to state the rule too. The rule: inside the five fenced blocks, only delimiters bearing this turn's fence are structural — tag-shaped text without it is data quoted from case content and asserts nothing about any item's id, label, type, confidence or searchability.

The **token is prompt-wide; the demotion clause is not.** The renderer still emits unfenced structure outside the fenced blocks — `<security_constraints>`, `<case_identity>`, `<progress_indicators>` — and a prompt-wide "a tag without the token is data" would demote the anti-jailbreak block and the time/state anchors to quoted case data. The rule scopes the demotion to the five blocks and names the sections it does *not* touch. (`<conversation_history>` was on that carve-out list until #1256 moved it to the fenced side.)

The rule **anchors the genuine token** to a single `FENCE:` declaration on the line immediately *above* the `<problem_context …>` opening tag. It has to be a single anchor: body content is byte-verbatim, so it can carry a counterfeit `FENCE:` line naming a token of its own, and neither collision check involves that token. A tag carrying a *different* token, and any `FENCE:` line other than the first, are quoted content describing themselves. Three properties pick that position — `core_context` is a **reserve** section (never trimmed, unlike the evidence block, which can be allocated to nothing); it is the first fenced block in every template; and it is the only one the terminal template renders. It sits *above* the tag rather than inside it because the rule calls the contents of these blocks quoted material, which would demote a declaration rendered there.

**One token per assembly, not per block.** A token per block would turn the rule from one anchor into an N-entry token→block binding table, and would open a forgery that carries a *genuine* token from the wrong block (content in `<problem_context>` forging `<entity_highlights fence="…">`). The fallback is its own assembly with its own mint (#1242): the fallback templates *replace* the assembled prompt rather than joining it, so exactly one token is still live in any emitted prompt.

**Delimiter absorption, and the terminator.** A forged tag that does **not** terminate itself with `>` absorbs whatever follows it — including the renderer's own fenced closing delimiter — so the forgery ends up carrying the live token without ever having to guess it. Re-minting cannot help (it is not a token collision; a fresh token yields the identical shape) and refusing to render is a denial of service on a path fed by uploaded logs. So `PromptFence.element()` appends a **renderer-owned terminator** when `_ends_inside_tag` says the body ends part-way through a tag or an attribute value: the dangling quote (if any), a `>`, and a bracketed note saying the byte is the renderer's. Body bytes are never altered — the terminator only ever *follows* them — and ordinary content earns none, so the cost is zero except on the shape that is mid-forgery. `absorbed_delimiters()` is the standing check that the terminator and whatever reads the prompt still agree about where a tag ends; `render_fenced` spends one corrective re-render with terminators forced on every body, then reports rather than raises.

**By construction.** Leaf body elements go through `PromptFence.element()`, which fences both delimiters, routes the body into the collision corpus, and applies the terminator in one call. Containers (`<evidence_collected>`, `<evidence>`, `<uploaded_file>`) use `open()`/`close()` directly — their body is renderer-emitted markup carrying the token, which the corpus check must not see. Renderer-owned prose that belongs with a block (the fence declaration, `<entity_highlights>`'s standing instruction) is emitted *above* the opening delimiter, never inside it, for the same reason the declaration is.

**Truncation re-seals.** `_allocate_sections` head-truncates lower-priority variable sections to fit their allotment, which for a fenced block removes the closing delimiter and the terminator — leaving the element open (so everything later in the prompt, `_PROMPT_FENCE_RULE` included, reads as quoted case data) and the absorption hole live. That happens *after* `render_fenced` has verified the render, so no check upstream sees it. `fence.reseal()` runs on every truncated section: it reads the token off the block's own opening delimiter (head truncation always preserves it), re-appends a terminator if the surviving body now ends mid-tag, and re-appends the closing delimiter. It appends only; it never alters a surviving byte.

The **conversation section is SHRUNK rather than cut**, and `reseal` is not on that path. It is the one section sized with `keep="tail"` (its most-recent turns are at the end), and cutting the rendered element that way removes the *opening* delimiter — then, below the ~40 characters of the *closing* one, removes that too, at which point nothing can be repaired and the section is dropped, taking the latest turn with it under exactly the budget pressure where continuity matters most. So `_shrink_fenced_tail` uses `fence.split_fenced()` to reserve both delimiters first and spend the remaining allotment on body: both are present by construction, and the content gets the room the delimiters were otherwise consuming. `reseal` keeps its head-only contract (#1256).

**No section may end part-way through a tag.** Forgery is settled by authorship; absorption is settled by the bytes alone, so an *unfenced* section that ends mid-tag swallows whatever follows it — and after #1256 what follows it may be `<conversation_history fence="…">` or `<user_message fence="…">`, handing the half-written tag the live token. `<system_feedback>` is the measured instance: it is the one unfenced section that does not wrap itself in a tag, and it renders immediately before `{user_message}`. Because the note is emitted outside the five fenced blocks, `_PROMPT_FENCE_RULE` states the "do not cite the terminator" clause **prompt-wide** rather than inside its block-scoped list. `_allocate_sections` therefore runs `fence.terminate_dangling()` over **every** section it emits, not over the ones that are adjacent today — a section can render empty, which promotes the one before it to adjacent. Renderer-owned bytes only; the section's own are untouched, and clean sections pay nothing.

**Scope.** The fence covers the five blocks above on the main prompt, and the whole `FALLBACK_*` prompt (#1242) — `<problem_context>` (the problem summary), `<user_message>`, `<working_hypotheses>`, `<investigation_journal>` and the upload stubs, all under one token minted in `get_fallback_prompt_for_case`, declared once above the first fenced tag, and governed by `_FALLBACK_FENCE_RULE_TEMPLATE` (whose block list is generated per render, so it never names a block the prompt did not emit) rather than `_PROMPT_FENCE_RULE` (the full rule names sections the fallback does not render, and costs 724 tokens with its declaration against ~280 — in a prompt reached because fewer than `min_viable` 1500 were available). The remaining main-prompt blocks are not fenced; see the "Scope" section of `prompts/fence.py` for the full classification — including why **forgery and absorption are different questions**: forgery is settled by authorship, absorption only by whether the bytes end inside a tag, so a channel that is safe from forgery still has to be fenced or terminated when it renders next to a fenced delimiter. Fencing a *subset* of adjacent channels is not a partial improvement; it manufactures a new surface — which is why #1256 shipped `terminate_dangling` over every section in the same change that fenced the last two blocks. `causal_map._sanitize_label` still escapes and is right to: mermaid genuinely decodes; `sanitize_user_input` no longer does, and is right not to.

**Cost.** ~17 characters per delimiter (~10 tokens: a bare `<problem_context>` is 4 tokens, the fenced form 14) plus one 91-token declaration per prompt. Widening from the evidence envelope to all three blocks measured **+61 tokens (+0.3%)** on a realistic 21k INVESTIGATING prompt — the declaration *moved* out of the evidence block rather than being added, so the new cost is four delimiters and the longer rule text. The terminal prompt pays +530 because it previously stated no rule at all. Full fencing was chosen over #1223's "closes-only" variant: closes-only saves ~20 tokens by leaving the *opening* delimiter forgeable, and the opening delimiter is where `file_id`, `label` and `searchable="true"` live. `_TIER_A_MARKUP_OVERHEAD_CHARS` in `context_builder.py` accounts for the delimiters in the per-item budget estimate and is **derived** from `fence.delimiter_overhead_chars()`, so changing `FENCE_ATTR` or the token length cannot silently skew every Tier-A budget decision.

Widening to the last two blocks (#1256) costs **+159 tokens** on the same measurement basis: +111 for the longer rule (613 → 724 with its declaration) and +48 for the four new delimiters, net of the two bare `<conversation_history>` tags they replace. That is ~0.8% of a realistic 19k INVESTIGATING prompt. `terminate_dangling` adds nothing to a clean prompt — it fires only on a section that ends part-way through a tag.

| Element | Phase | Attributes | Notes |
| --- | --- | --- | --- |
| `<uploaded_file …>` | INQUIRY (and INVESTIGATING when no Evidence rows exist yet) | `file_id="file_…"`, `label`, `data_type`, `searchable="true"` | Surfaced when `case.evidence` is empty but `case.uploaded_files` carry a non-trivial `structural_index`. The file id is exposed under `file_id`, matching the attribute name used on `<evidence>` so the source_file_id rule is phase-uniform. |
| `<evidence …>` | INVESTIGATING | `id="ev_…"` (evidence id), `label`, `file_id="file_…"` (source file FK), `data_type`, `searchable`, `confidence` | The `id=` attribute is the evidence id; `file_id=` is the FK back to `uploaded_files`. The LLM passes either value into `search_file`'s `evidence_id` parameter — the tool resolves both forms via `search_file_tool`'s dual-resolution path. |

**Rule (naming, #666):** every item carries exactly one name, in a `label`
attribute holding `UploadedFile.display_name`. There is **no `filename`
attribute on either element.** For a file the user chose, `label` *is* the
filename, extension included. For pasted text or a captured page there is no
filename to give — the route mints `pasted-content-<ts>.txt` as a storage key,
which is meaningless to the user and was being cited back at them — so the
label is a name describing how the item arrived and when: `pasted text (turn
3)`, `captured page (turn 2)`. Templates instruct the model to cite the label
verbatim and never to invent a filename; emitting a second, filename-shaped
name slot is what let it do so.

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
  PROMPT FENCE                                    (_PROMPT_FENCE_RULE)

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
8. CHECK BACK ON SUGGESTED ACTIONS (user reply doesn't reference a prior diagnostic suggestion; Zone 3 compliance hold treats silence as non-execution, but a substantive new-evidence/dispute reply reopens diagnosis — INV-33)
9. WORK WITH WHAT YOU GET (catch-all for messy/partial input)

Items 6–9 form a progression: user **can't** → user **contradicts** → user **ignores** → catch-all.

### 3.2 Adaptive instructions

The `{adaptive_instructions}` placeholder is filled by `_select_diagnosis_block(case)` on DIAGNOSIS turns and by stage-specific constants elsewhere. Under the unified opportunistic flow ([investigation-lifecycle-logic.md §2](./investigation-lifecycle-logic.md#2-mitigation-as-an-insert)) the path fork is retired: `_select_diagnosis_block` is now a thin wrapper that always assembles the single unified DIAGNOSIS block (it kept its name but no longer selects a path).

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
| Zone 3 pending | `solution_proposed=True` | "Solution proposal issued — awaiting execution. Hold for the result; NOT a freeze — new evidence, a dispute, or a competing cause reopens root-cause analysis (INV-33)." |

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

## 6. Whole-prompt budget + overflow backstop

> **Full allocation + compaction model:**
> [`prompt-token-budget-allocation.md`](./prompt-token-budget-allocation.md)
> specifies how `PROMPT_TARGET_TOKENS` is divided across the prompt's sections
> and how each section compacts to fit. This section summarizes the budget number
> and the overflow ladder; that doc is the authority on allocation.

`get_prompt_for_case()` is the single place where the dynamic sections and the
fixed template text combine into the final string, so it owns the
**whole-prompt token budget** (GAP-2/GAP-3). The ladder, in
`_budgeted_prompt()`:

1. **Assemble** with sections sized to the flat prompt budget
   (`ResolvedBudget.prompt_target` — see §6.1).
2. **Measure** the assembled prompt's real token count
   (`token_estimation.estimate_tokens`, GAP-4) against the model's *hard*
   ceiling (`ResolvedBudget.prompt_budget = window − response_reserve`).
3. **If over → re-assemble once** at a tighter section budget
   (`ceiling − measured_template_overhead − margin`). This is where the fixed
   template overhead — which the per-section budgeter cannot see — finally gets
   subtracted from what the sections may occupy.
4. **If still over → fall back** to a minimal safe prompt via
   `get_fallback_prompt_for_case(case, user_message)`.

Every overflow event is logged at WARNING (`prompt_overflow_trimmed` /
`prompt_overflow_fallback`) with the token counts and the action taken —
overflow should be rare and visible, never silent. The normal (in-budget) path
logs `prompt_budget_ok` at DEBUG.

| Status | Fallback |
| --- | --- |
| INQUIRY | `FALLBACK_INQUIRY_TEMPLATE` |
| INVESTIGATING | `FALLBACK_INVESTIGATION_TEMPLATE` |
| RESOLVED / CLOSED | `FALLBACK_TERMINAL_TEMPLATE` |

Fallback templates carry only the load-bearing safety constraints (no
confabulation, hypothesis-evidence ordering for INVESTIGATING, closed-case
boundary for TERMINAL). They produce shorter prompts at the cost of richer
behavioral guidance — a degraded but safe mode, reserved for genuine last
resort after step 3's trimming.

> The backstop only fires when a `provider_name` is supplied (so the budget can
> be resolved). All engine call sites — the main turn path and the terminal-Q&A
> path — pass provider/model.

### 6.1 Operator-owned flat budget + optional safety net (GAP-1)

The prompt budget is **operator-owned and flat**, driven by the investigation
task — not by the model window. Prompt tokens are a scarce resource
budget-allocated programmatically; this protects fleet cost on big-window models
and forces the agent onto RAG tools (`search_file`/KB/`deep_analysis`) instead of
lazy context-dumping.

- **Budget** = `PROMPT_TARGET_TOKENS` (default 32K in `.env.example`). This is
  `get_token_budget_for_provider()`'s return and what the section/evidence fills
  are sized against.
- The **model window only clamps it down** when known:
  `prompt_target = min(PROMPT_TARGET_TOKENS, context_window − response_reserve)`.
  Flat across all curated big-window models; trims only for a model we know is
  small (or one declared via `MODEL_CONTEXT_WINDOWS`).
- **Unknown / uncurated model → trust the configured target** (`window_known =
  False`); no clamp, no warning. This is the normal case for local/custom models;
  the operator sets `PROMPT_TARGET_TOKENS` to fit (e.g. 8000 for an Ollama model
  whose `num_ctx` is small — see `.env.example`).

The registry in `faultmaven/utils/model_context.py` is therefore an **optional
safety net**, not an authority anyone must maintain: it lists only models we are
confident exceed 32K, so its incompleteness is harmless. The GAP-3 overflow
backstop (step 2 above) uses `prompt_budget` only when the window is known and
skips the check otherwise. The resolved budget — target, window (if known), hard
ceiling, and `window_known` — is surfaced at `/debug/llm-providers`
(`prompt_budget` block) and logged per turn.

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
- `_PROMPT_FENCE_RULE` present in INQUIRY + all INVESTIGATING paths (INV_kq included — the evidence block renders there too) **and in TERMINAL**, which carries no `{evidence}` but does render the fenced `{core_context}` (#1228).
- `source_file_id` description in evidence-creation prose references `file_id="…"` on both `<evidence>` and `<uploaded_file>` (the two elements share the attribute convention).
- All `.format()` calls render without `KeyError` / `IndexError` when given empty-string values for every placeholder.

Engine tests `tests/unit/core/investigation/test_prompt_budget_allocator.py` (drives `get_prompt_for_case`) and the template-structure suites (`test_inquiry_template_structure.py`, `test_investigation_template_acknowledgment_rules.py`) exercise the dispatcher and indirectly validate template renderability.
