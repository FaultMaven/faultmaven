# FaultMaven Architecture Documentation

Master index for all architecture documentation.

---

## 🎯 Start Here

**Investigation Architecture v2.0** - Milestone-based investigation framework (current design)

### **Core v2.0 Documents (Production Specification)**:

| Document | Purpose |
|----------|---------|
| **[Investigation Architecture](investigation-engine/milestone-based-investigation-framework.md)** | 🎯 Investigation workflow, lifecycle, stages, milestones |
| **[Case Storage Design](data-and-storage/case-storage-design.md)** | 🎯 PostgreSQL schema (10 tables) |
| **[Prompt Engineering Guide](investigation-engine/prompt-engineering-guide.md)** | 🎯 LLM prompts, templates, strategies |
| **[Prompt Templates](investigation-engine/prompt-templates.md)** | Implementation-ready prompt code |
| **[Prompt Implementation Examples](investigation-engine/prompt-implementation-examples.md)** | Complete code examples |

**Architecture Philosophy**: Milestone-based (not phase-based) investigation where agents complete tasks opportunistically based on data availability.

---

## 📋 Navigation

### Primary Documents (v2.0 - Current)

| Document | Version | Purpose |
|----------|---------|---------|
| **[Architecture Overview](./architecture-overview.md)** | v3.0 | 🎯 Master system architecture document |
| **[Investigation Architecture](investigation-engine/milestone-based-investigation-framework.md)** | v2.0 | 🎯 Milestone-based investigation framework |
| **[Case Storage Design](data-and-storage/case-storage-design.md)** | v2.0 | 🎯 PostgreSQL schema (10 tables) |
| **[Prompt Engineering Guide](investigation-engine/prompt-engineering-guide.md)** | v2.0 | 🎯 Prompt templates and strategies |

### Supporting Components

| Document | Purpose |
|----------|---------|
| **[Case and Session Concepts](case-and-session/case-and-session-concepts.md)** | Case vs Session distinction, multi-device support |
| **[Knowledge Base Architecture](knowledge-and-ai/knowledge-base-architecture.md)** | Vector database, RAG, knowledge retrieval |
| **[Data Submission Design](data-processing/data-submission-design.md)** | File uploads and data handling |
| **[Data Preprocessing Design](data-processing/data-preprocessing-design-specification.md)** | Data preprocessing pipeline |
| **[QA Tools Design](knowledge-and-ai/qa-tools-design.md)** | Question answering tools and sub-agents |

### Infrastructure

| Document | Purpose |
|----------|---------|
| **[Dependency Injection System](core-architecture/dependency-injection-system.md)** | DI container and service interfaces |
| **[IAM Design](security/iam-design.md)** | Identity and Access Management |

### Implementation Guides

| Document | Purpose |
|----------|---------|
| **[Container Usage Guide](guides/container-usage-guide.md)** | DI container practical guide |
| **[Testing Guide](guides/testing-guide.md)** | Testing strategies |
| **[Service Patterns](core-architecture/service-patterns.md)** | Service layer patterns |
| **[Interface-Based Design](core-architecture/interface-based-design.md)** | Interface design guidelines |

### Archived Documents (Superseded by v2.0)

**These documents are OBSOLETE** - superseded by milestone-based investigation framework v2.0

| Archived Document | Superseded By | Reason |
|-------------------|---------------|--------|
| ~~case-data-model-design.md~~ | case-storage-design.md | Merged into unified storage design |
| ~~db-design-specifications.md~~ | case-storage-design.md | Merged into unified storage design |

**Location**: `archive/` directory

---

## 📁 Subdirectories

### [archive/](./archive/)
**3 documents** - Superseded architecture documents preserved for historical context

### [decisions/](./decisions/)
**2 documents + README** - Architecture Decision Records (ADRs)

### [diagrams/](./diagrams/)
**3 diagrams + README** - Visual architecture representations (Mermaid sources)

### [reference/](./reference/)
**9 documents** - Detailed designs and analysis (component interactions, infrastructure guides)

### [specifications/](./specifications/)
**4 documents** - Formal specifications (configuration management, session management, etc.)

---

## Documentation Organization

### At Root Level (~33 active docs)
All documents referenced by architecture-overview.md remain at the root level for:
- ✅ No broken links
- ✅ Simple relative paths
- ✅ Easy access to primary documents
- ✅ Logical grouping via architecture-overview.md

### In Subdirectories (organized by purpose)
- **archive/**: Historical/superseded documents
- **decisions/**: Architecture Decision Records
- **diagrams/**: Visual representations
- **reference/**: Supplementary material
- **specifications/**: Formal specifications

---

## Quick Stats

| Category | Count | Location |
|----------|-------|----------|
| **Active architecture docs** | ~33 | Root level |
| **Reference material** | 9 | reference/ |
| **Archived/superseded** | 3 | archive/ |
| **Diagrams** | 3 | diagrams/ |
| **ADRs** | 2 | decisions/ |
| **Total** | ~50 | |

---

## For Developers

### Finding Documentation

1. **Start with**: [Architecture Overview](./architecture-overview.md)
   - Provides complete navigation map
   - Organized by code structure (10 sections)
   - Links to all related documents

2. **Need specific info?**: Check [Documentation Map](./documentation-map.md)
   - Status of all documents
   - Creation priorities
   - Dependencies

3. **Looking for something unreferenced?**: Check [reference/](./reference/)
   - Detailed analysis documents
   - Alternative perspectives
   - Supplementary material

4. **Historical context?**: Check [archive/](./archive/)
   - Understand architecture evolution
   - See what changed and why

---

## Maintenance

- **Primary Owner**: Architecture Team
- **Review Cycle**: Quarterly or on major changes
- **Master Document**: architecture-overview.md
- **Organization**: Code-aligned (matches faultmaven/ structure)

---

**Last Updated**: 2026-01-06
**Architecture Version**: v3.0

---

## 📌 Latest Addition (2025-10-13)

### Data Preprocessing System v4.0

**[data-preprocessing-design-specification.md](data-processing/data-preprocessing-design-specification.md)** - Complete design specification for data preprocessing system

**What it covers**:
- 3-step pipeline architecture (Classify → Preprocess → LLM Analysis)
- 8 data types with detailed specifications (LOG_FILE, ERROR_REPORT, CONFIG_FILE, METRICS_DATA, etc.)
- Complete preprocessor implementations for each type
- LLM integration and prompt structure
- Security & privacy (PII redaction, sanitization)
- Phased implementation roadmap with effort estimates
- Dependencies and testing strategy

**Status**: ✅ Final design - Ready for implementation

**Quick Summary**:
```
Step 1: Classify (✅ Implemented)
  ↓
Step 2: Preprocess (⚠️ To Implement - THIS DOCUMENT)
  ├─ LogPreprocessor (P1 - 6 hours)
  ├─ ErrorPreprocessor (P1 - 6 hours)
  ├─ ConfigPreprocessor (P2 - 8 hours)
  ├─ MetricsPreprocessor (P2 - 8 hours)
  └─ Others (P3-P5)
  ↓
Step 3: LLM Analysis (✅ Ready)
```

**Related Documents**:
- [data-submission-design.md](data-processing/data-submission-design.md) - Upload flow and dual submission paths

