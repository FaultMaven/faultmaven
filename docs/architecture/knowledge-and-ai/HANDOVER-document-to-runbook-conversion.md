# Handover: Document-to-Runbook Conversion Feature

**Date:** 2026-03-22
**From:** KB Architecture & Toolkit work
**To:** Implementation agent
**Status:** Design complete, ready for implementation

---

## What This Feature Does

Converts uploaded documents (PDF, DOCX, TXT, Markdown, HTML) into one or more standardized runbook files that comply with FaultMaven's Runbook Content Architecture. The feature adds a preprocessing pipeline, a new LLM capability, API endpoints, and Dashboard UI.

## Design Specification

The complete specification is at:
**`faultmaven/docs/architecture/knowledge-and-ai/document-to-runbook-conversion.md`**

Read the entire document before starting. It covers:

1. System architecture and component diagram
2. Preprocessing pipeline (6 stages: format extraction → content cleanup → PII scan → size check → metadata extraction → content triage)
3. New `KNOWLEDGE_PROVIDER` LLM capability definition
4. Two-phase LLM prompt design (analysis + conversion)
5. Multi-runbook splitting logic (one runbook per failure mode)
6. API contract (5 endpoints with request/response schemas)
7. Dashboard UI flow (wireframe-level)
8. Storage and lifecycle (source file retention, draft management, ingestion trigger)
9. Error handling
10. Security considerations

## Key Design Decisions Already Made

These are settled — do not revisit without consulting the user:

| Decision | Rationale |
|----------|-----------|
| **Hard reject at 30K tokens** (no silent truncation) | Silent truncation causes data loss the user can't detect |
| **PII redaction BEFORE LLM** | Source docs may go to third-party LLM APIs |
| **Content triage sends only first 2K tokens** to classifier | Classifiers have small context windows; intro is sufficient |
| **Frontmatter mutation via `python-frontmatter`** | Not regex — YAML has edge cases |
| **One runbook = one failure mode** | Multi-topic docs produce multiple runbooks |
| **Drafts NOT ingested into ChromaDB** | Only `verified` status triggers ingestion |
| **Input limited to local file upload** | No URL scraping; users save web content to files first |
| **Batch = loop over single-file endpoint** | No separate batch API |

## Architecture Context

Read these docs for background — the conversion feature must be consistent with them:

| Document | What it covers |
|----------|---------------|
| `knowledge-and-ai/runbook-content-architecture.md` | Template, taxonomy, quality gates, lifecycle (5 states: draft, in-review, verified, stale, deprecated) |
| `knowledge-and-ai/knowledge-base-architecture.md` | 3-tier KB (global/team/personal), federated search, staleness-aware synthesis, storage architecture |
| `data-and-storage/repository-pattern.md` | Pluggable storage (local=PersistentClient, cloud=HttpClient) |
| `data-processing/data-preprocessing-design-specification.md` | Existing evidence preprocessing — reuse extractors, not new ones |

## Files to Create/Modify

### New files (FaultMaven API):

```
faultmaven/modules/knowledge/domain/services/conversion_service.py   # Orchestrates the pipeline
faultmaven/modules/knowledge/domain/services/document_preprocessor.py # 6-stage preprocessing
faultmaven/modules/knowledge/domain/models/conversion.py             # ConversionJob, ConversionDraft models
faultmaven/modules/knowledge/api/conversion_routes.py                # 5 API endpoints
```

### Existing files to modify:

```
faultmaven/config/settings.py          # Add KNOWLEDGE_PROVIDER capability + model fields
faultmaven/infrastructure/llm/router.py # Register KNOWLEDGE_PROVIDER routing
faultmaven/modules/knowledge/contracts.py # Add IConversionService interface
faultmaven/main.py                     # Register conversion routes
```

### New files (Dashboard):

```
src/pages/ConvertDocumentPage.tsx       # Upload + conversion UI
src/components/RunbookEditor.tsx        # Markdown editor for draft review
src/services/conversion-service.ts      # API client for conversion endpoints
```

### Storage directories:

```
data/knowledge/global/                  # Global KB runbook source files
data/knowledge/team_{team_id}/          # Team KB runbook source files
data/knowledge/user_{user_id}/          # Personal KB runbook source files
data/knowledge/sources/{conversion_id}/ # Retained source documents
```

## Validation and Quality

The conversion pipeline must validate output using the same gates as the KB Toolkit:

- **Gate 1**: YAML frontmatter — 11 required fields, controlled vocabulary for domain/service/status/scope
- **Gate 2**: Structural linting — 6 required sections (Problem Definition, Diagnostic Steps, Mitigation, Root Cause Resolution, Verification, Prevention) + Sources (mandatory)
- **Quality scoring**: 4 dimensions (completeness, clarity, actionability, comprehensiveness). Score < 50 → warn user

The KB Toolkit's `RunbookValidator` and `QualityScorer` classes can be imported directly or their logic replicated. They live at:
- `faultmaven-kb-toolkit/kb_toolkit/core/validator.py`
- `faultmaven-kb-toolkit/kb_toolkit/core/quality.py`

## Testing Requirements

Per project standards: no code merges without tests.

- Unit tests for `DocumentPreprocessor` (each of the 6 stages independently)
- Unit tests for `ConversionService` (mock LLM calls, verify pipeline orchestration)
- Integration tests for API endpoints (file upload → conversion → draft CRUD → verify → ingestion)
- Test the hard reject at 30K tokens
- Test PII redaction before LLM call
- Test multi-runbook splitting (source with 3 failure modes → 3 drafts)
- Test content triage rejection (non-troubleshooting document → rejected)

## What NOT to Change

- The KB Toolkit codebase (`faultmaven-kb-toolkit/`) — that's being worked on separately
- The runbook template or taxonomy — those are finalized
- The knowledge-base-architecture.md — federated search, staleness-aware synthesis are designed but separate implementation work
- The existing `POST /api/v1/knowledge/documents` upload endpoint — that's the user self-service path, this feature adds a separate conversion path

## Questions?

If any design decision is unclear, refer to the specification first. If the spec doesn't cover it, ask the user before making assumptions.
