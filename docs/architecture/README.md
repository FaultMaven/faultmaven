# FaultMaven Architecture Documentation

Master index for all architecture documentation.

---

## 🎯 Start Here

**Investigation Architecture v2.0** - Milestone-based investigation framework (current design)

### **Core v2.0 Documents (Production Specification)**:

| Document | Purpose |
|----------|---------|
| **[Investigation Architecture](./milestone-based-investigation-framework.md)** | 🎯 Investigation workflow, lifecycle, stages, milestones |
| **[Case Data Model Design](./case-data-model-design.md)** | 🎯 Complete data structures, validation, database schema |
| **[DB Design Specifications](./db-design-specifications.md)** | 🎯 PostgreSQL schema, migrations, queries |
| **[Prompt Engineering Guide](./prompt-engineering-guide.md)** | 🎯 LLM prompts, templates, strategies |
| **[Prompt Templates](./prompt-templates.md)** | Implementation-ready prompt code |
| **[Prompt Implementation Examples](./prompt-implementation-examples.md)** | Complete code examples |

**Architecture Philosophy**: Milestone-based (not phase-based) investigation where agents complete tasks opportunistically based on data availability.

---

## 📋 Navigation

### Primary Documents (v2.0 - Current)

| Document | Version | Purpose |
|----------|---------|---------|
| **[Investigation Architecture](./milestone-based-investigation-framework.md)** | v2.0 | 🎯 Milestone-based investigation framework |
| **[Case Data Model Design](./case-data-model-design.md)** | v2.0 | 🎯 Complete data models and validation |
| **[DB Design Specifications](./db-design-specifications.md)** | v2.0 | 🎯 Database schema and migrations |
| **[Prompt Engineering Guide](./prompt-engineering-guide.md)** | v2.0 | 🎯 Prompt templates and strategies |
| **[Architecture Overview](./architecture-overview.md)** | v2.0* | System architecture (needs v2.0 update) |

### Supporting Components

| Document | Purpose |
|----------|---------|
| **[Case and Session Concepts](./case-and-session-concepts.md)** | Case vs Session distinction, multi-device support |
| **[Knowledge Base Architecture](./knowledge-base-architecture.md)** | Vector database, RAG, knowledge retrieval |
| **[Data Submission Design](./data-submission-design.md)** | File uploads and data handling |
| **[Data Preprocessing Design](./data-preprocessing-design-specification.md)** | Data preprocessing pipeline |
| **[QA Tools Design](./qa-tools-design.md)** | Question answering tools and sub-agents |

### Infrastructure

| Document | Purpose |
|----------|---------|
| **[Dependency Injection System](./dependency-injection-system.md)** | DI container and service interfaces |
| **[Authentication Design](./authentication-design.md)** | Authentication architecture |

### Implementation Guides

| Document | Purpose |
|----------|---------|
| **[Container Usage Guide](./container-usage-guide.md)** | DI container practical guide |
| **[Testing Guide](./testing-guide.md)** | Testing strategies |
| **[Service Patterns](./service-patterns.md)** | Service layer patterns |
| **[Interface-Based Design](./interface-based-design.md)** | Interface design guidelines |

### Archived Documents (Superseded by v2.0)

**These documents are OBSOLETE** - superseded by milestone-based investigation framework v2.0

| Archived Document | Superseded By | Reason |
|-------------------|---------------|--------|
| ~~investigation-phases-and-ooda-integration.md~~ | milestone-based-investigation-framework.md v2.0 | Old 7-phase model replaced by milestones |
| ~~evidence-collection-and-tracking-design.md~~ | case-data-model-design.md v2.0 (Evidence sections) | Merged into unified data model |
| ~~investigation-state-and-control-design.md~~ | milestone-based-investigation-framework.md v2.0 | Old phase-based design |
| ~~prompt-engineering-architecture.md~~ | prompt-engineering-guide.md v2.0 | Outdated prompt design |
| ~~data-models-reference.md~~ | case-data-model-design.md v2.0 | Old OODA-based models |

**Location**: `/archive/` directory

**Note**: `document-generation-and-closure-design.md` introduces `DOCUMENTING` status - conflicts with v2.0 (needs review)

---

## 📁 Subdirectories

### [reference/](./reference/)
**9 documents** - Valuable analysis and detailed designs not currently linked from architecture-overview.md

Includes:
- Component interaction patterns
- Critical concepts and relationships
- Context engineering analysis
- Infrastructure layer guides
- Detailed system designs

### [legacy/](./legacy/)
**3 documents** - Superseded architecture documents preserved for historical context

Includes:
- Doctor-Patient Prompting v1.0 (superseded by Investigation Phases v2.1)
- Sub-Agent Architecture v1.0 (superseded by Agentic Framework)
- System Architecture v1.0 (superseded by Architecture Overview v2.0)

### [diagrams/](./diagrams/)
**3 diagrams + README** - Visual architecture representations

Includes:
- System architecture diagrams (multiple views)
- Mermaid diagram sources

### [decisions/](./decisions/)
**1 document + README** - Architecture Decision Records (ADRs)

Includes:
- Architecture decision guide and framework

### [_temp/](./temp/)
**9 documents** - Temporary status reports and planning docs (to review/delete later)

Contains:
- status-reports/ - Implementation status (3 files)
- working-docs/ - Working notes (4 files)
- planning/ - Reorganization planning (3 files)

---

## Documentation Organization

### At Root Level (~19 active docs)
All documents referenced by architecture-overview.md remain at the root level for:
- ✅ No broken links
- ✅ Simple relative paths
- ✅ Easy access to primary documents
- ✅ Logical grouping via architecture-overview.md

### In Subdirectories (organized by purpose)
- **reference/**: Supplementary material (not currently linked)
- **legacy/**: Historical/superseded documents
- **diagrams/**: Visual representations
- **decisions/**: ADRs
- **_temp/**: To review and delete

---

## Quick Stats

| Category | Count | Location |
|----------|-------|----------|
| **Active architecture docs** | ~19 | Root level |
| **Reference material** | 9 | reference/ |
| **Legacy/superseded** | 3 | legacy/ |
| **Diagrams** | 3 | diagrams/ |
| **ADRs** | 1 | decisions/ |
| **Temporary/planning** | 9 | _temp/ |
| **Total** | ~44 | |

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

4. **Historical context?**: Check [legacy/](./legacy/)
   - Understand architecture evolution
   - See what changed and why

---

## Maintenance

- **Primary Owner**: Architecture Team
- **Review Cycle**: Quarterly or on major changes
- **Master Document**: architecture-overview.md
- **Organization**: Code-aligned (matches faultmaven/ structure)

---

**Last Updated**: 2025-10-13
**Architecture Version**: v2.0

---

## 📌 Latest Addition (2025-10-13)

### Data Preprocessing System v4.0

**[data-preprocessing-design.md](./data-preprocessing-design.md)** - Complete design specification for data preprocessing system

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
- [data-submission-design.md](./data-submission-design.md) - Upload flow and dual submission paths
- [simplified-3-step-pipeline.md](./simplified-3-step-pipeline.md) - Quick reference

