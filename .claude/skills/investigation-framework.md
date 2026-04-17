---
name: investigation-framework
description: Triggers when modifying investigation orchestration, milestone logic, hypothesis management, agent tools, prompt templates, context building, stage transitions, or any code under faultmaven/modules/agent/ or faultmaven/core/investigation/. Do NOT trigger on knowledge-module work, auth work, retrieval/search, preprocessing, or any other non-agent domain.
---

# Skill: investigation-framework

**What this skill does:** Makes sure you read the current investigation framework design docs *before* modifying investigation orchestration, agent behavior, or prompts. The framework has behavioral subtleties (opportunistic milestone completion, hypothesis lifecycle, confidence decay, anchoring detection) that are specified in docs — not inferable from the code alone.

**What this skill does NOT do:** Restate the framework. The design docs are the source of truth and change over time.

---

## Authoritative Documents

Read these before acting. Two of the four canonical source-of-truth documents declared in `docs/architecture/README.md` live in this section:

1. **`docs/architecture/investigation-engine/README.md`** — Declares reading order. Start here. Directs you through the framework, lifecycle, data models, prompts, and behavioral rules.
2. **`docs/architecture/investigation-engine/investigation-lifecycle-logic.md`** — Canonical. Case actions, state transitions, stage routing, turn tracking.
3. **`docs/architecture/investigation-engine/agent-behavioral-rules.md`** — Canonical. The 7 prompt-injected rules that constrain agent output.
4. **`docs/architecture/investigation-engine/evidence-driven-investigation-framework.md`** — The framework itself: milestones, hypothesis lifecycle, stage semantics.
5. **`docs/architecture/investigation-engine/investigation-data-models.md`** — Data structures passed between engine, tools, and LLM.
6. **`docs/architecture/investigation-engine/prompt-templates.md`** — INQUIRY / INVESTIGATING / TERMINAL templates.
7. **`docs/architecture/investigation-engine/orchestration-capabilities.md`** — What orchestration can and cannot do.
8. **`docs/architecture/investigation-engine/error-handling-and-recovery.md`** — Failure modes and recovery paths.
9. **`docs/architecture/investigation-engine/progress-transparency.md`** — Progress reporting, repair patterns.
10. **`docs/architecture/investigation-engine/investigation-journal.md`** — Journal design.
11. **`docs/architecture/investigation-engine/intent-resolution.md`** — Bounded-choice matching and hypothesis action routing.

If any referenced document does not exist at the path above, **stop and tell the user** — do not fabricate content to fill the gap.

---

## Code Scope

This skill covers changes to:
- `faultmaven/modules/agent/` — Investigation orchestration, agent API, agent tools
- `faultmaven/core/investigation/` — Milestone engine, hypothesis manager, prompt templates, context builder

---

## Procedure

1. **Read the investigation-engine README** (`docs/architecture/investigation-engine/README.md`) for the current reading order. The order matters — some docs assume earlier ones.
2. **Read the design docs relevant to the change.** If you are touching:
   - Milestone transitions or stage logic → lifecycle-logic + framework
   - Agent output shape or tool behavior → behavioral-rules + prompt-templates
   - Hypothesis scoring, decay, or anchoring → framework + data-models
   - Prompts → prompt-templates + behavioral-rules
   - Error paths → error-handling-and-recovery
3. **Read the target code** (milestone engine, hypothesis manager, relevant tool, relevant template) before editing.
4. **Apply the change** preserving the behavioral design as documented. Investigation behavior is prompt- and state-driven; surface-level refactors can change semantics.

If the design docs and the existing code appear to contradict each other, **stop and ask the user which side is authoritative** before proceeding. Do not silently pick one side. Use `/design-check investigation` for a full drift report.

---

## Scope Boundaries

**This skill governs:**
- Investigation orchestration and lifecycle (milestones, stages, transitions)
- Agent behavior, prompts, and the 7 behavioral rules
- Hypothesis lifecycle (CAPTURED → ACTIVE → VALIDATED/REFUTED/RETIRED) and confidence dynamics
- Agent tool design and invocation (read-path tools like `search_file`, `deep_analysis`, KB Q&A tools)
- Context building, journal, progress reporting

**This skill does NOT govern:**
- Retrieval internals (vector search, reranking, hybrid retrieval) — see `rag-architecture`. Agent tools that *call* retrieval are in scope here; the retrieval mechanics they call into are not.
- Evidence ingestion, classification, chunking — see `ingestion-pipeline`
- Module organization or cross-module imports — see `architecture`
- Knowledge module business logic unrelated to investigation — out of scope
