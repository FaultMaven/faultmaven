---
name: ingestion-pipeline
description: Triggers when modifying data extraction, content classification, chunking, preprocessing, extractors, or vectorization of investigation evidence under faultmaven/modules/preprocessing/ or faultmaven/core/preprocessing/. Do NOT trigger on retrieval/search, query construction, runbook upload/authoring, or browser-side preprocessing.
---

# Skill: ingestion-pipeline

**What this skill does:** Makes sure you read the current data processing design docs *before* modifying the ingestion, classification, or chunking pipeline. The pipeline is scenario-driven (Triage / Directed Analysis / Knowledge Query / Semantic Search) and uses a unified DataType taxonomy — subtle behaviors not obvious from code alone.

**What this skill does NOT do:** Restate the pipeline design. The docs are the source of truth and evolve with every extractor addition or classifier revision.

---

## Authoritative Documents

Read these before acting:

1. **`docs/architecture/data-processing/README.md`** — Start here. Declares the unified DataType taxonomy (6 types), the scenario-driven processing model, document inventory, and implementation status.
2. **`docs/architecture/data-processing/data-preprocessing-design-specification.md`** — Preprocessing design spec (v5.0). The primary design doc for this skill's scope.
3. **`docs/architecture/data-processing/data-classification-strategy.md`** — How raw inputs are classified into DataTypes.
4. **`docs/architecture/data-processing/evidence-classification-design.md`** — Evidence categorization.
5. **`docs/architecture/data-processing/evidence-flow-architecture.md`** — End-to-end evidence flow.
6. **`docs/architecture/data-processing/evidence-failure-modes.md`** — Known failure modes and their handling.
7. **`docs/architecture/data-processing/platform-specific-extractors.md`** — The 11 platform-specific extractors.

If any referenced document does not exist at the path above, **stop and tell the user** — do not fabricate content to fill the gap.

---

## Code Scope

This skill covers changes to:
- `faultmaven/modules/preprocessing/` — Domain Service: classifier, chunking_service, extractors, preprocessors
- `faultmaven/core/preprocessing/` — Tier 0/1 mechanical preprocessor

---

## Procedure

1. **Read the data-processing README** (`docs/architecture/data-processing/README.md`) for document inventory and current implementation status. Status flags matter — some design elements may be specified but not yet implemented.
2. **Read the design docs relevant to the change.** Preprocessing spec is almost always required; classification-strategy is required when touching the classifier; extractors doc when adding/modifying an extractor.
3. **Read the target code** — classifier, chunking service, relevant extractor, preprocessor — before editing.
4. **Apply the change** conforming to the documented pipeline design.

If the design docs and the existing code appear to contradict each other, **stop and ask the user which side is authoritative** before proceeding. Do not silently pick one side. Use `/design-check data-processing` for a full drift report.

---

## Scope Boundaries

**This skill governs the API-side write path for user-submitted investigation data:**
- Data classification into the unified DataType taxonomy
- Content extraction (all 11 extractors)
- Chunking (boundaries, overlap, size)
- Preprocessing (Tier 0/1 mechanical, higher tiers as specified)
- Vectorization of evidence for downstream retrieval

**This skill does NOT govern:**
- Retrieval / search / reranking — see `rag-architecture`
- Runbook ingestion (knowledge-base building from authoritative content) — handled by the knowledge module's own ingestion, out of scope here
- Browser-side preprocessing (e.g., `htmlToStructuredText.js` in `faultmaven-copilot`) — this skill is API-side only
- Agent orchestration that triggers ingestion — see `investigation-framework`
- Module structure of the preprocessing module — see `architecture`
