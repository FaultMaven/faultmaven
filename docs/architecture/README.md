# FaultMaven Architecture Documentation

Master index for all architecture documentation.

---

## 🎯 Start Here

**[Architecture Overview v2.0](./architecture-overview.md)** - Master architecture document (code-aligned, 40+ linked docs)

This is the authoritative architecture document that provides:
- Complete system design overview
- Visual architecture diagrams
- Code-aligned documentation map (10 sections)
- Links to all architecture documents

---

## 📋 Navigation

### Primary Documents

| Document | Version | Purpose |
|----------|---------|---------|
| **[Architecture Overview](./architecture-overview.md)** | v2.0 | 🎯 Master architecture document |
| **[Documentation Map](./documentation-map.md)** | - | Complete document navigation and status |
| **[Investigation Phases Framework](./investigation-phases-and-ooda-integration.md)** | v2.1 | 🎯 Process framework (7 phases + OODA) |
| **[Evidence Collection Design](./evidence-collection-and-tracking-design.md)** | v2.1 | 🎯 Evidence data models and behaviors |
| **[Case Lifecycle Management](./case-lifecycle-management.md)** | v1.0 | Case status state machine |

### Framework and Components

| Document | Purpose |
|----------|---------|
| **[Investigation Phases and OODA Integration](./investigation-phases-and-ooda-integration.md)** | ✅ **IMPLEMENTED** - 7-phase investigation framework |
| **[Agent Orchestration](./agent_orchestration_design.md)** | Agent workflow coordination |
| **[Data Submission Design](./data-submission-design.md)** | Data upload handling (10K limit) |
| **[Data Preprocessing Design](./data-preprocessing-design.md)** | ✅ **AUTHORITATIVE** - Complete data preprocessing blueprint (v4.0) |

### Infrastructure

| Document | Purpose |
|----------|---------|
| **[Dependency Injection System](./dependency-injection-system.md)** | DI container and service interfaces |
| **[Authentication Design](./authentication-design.md)** | Authentication architecture |

### Implementation Guides

| Document | Purpose |
|----------|---------|
| **[Developer Guide](./developer-guide.md)** | Development workflow and setup |
| **[Container Usage Guide](./container-usage-guide.md)** | DI container practical guide |
| **[Testing Guide](./testing-guide.md)** | Testing strategies |
| **[Service Patterns](./service-patterns.md)** | Service layer patterns |
| **[Interface-Based Design](./interface-based-design.md)** | Interface design guidelines |

### Evolution and History

| Document | Purpose |
|----------|---------|
| **[Configuration Refactor](./configuration-system-refactor-design.md)** | Config system evolution |
| **[Prompt Engineering Architecture](./prompt-engineering-architecture.md)** | Multi-layer prompts, context management, token optimization |

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

