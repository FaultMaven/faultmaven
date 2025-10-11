# Architecture Folder Reorganization Plan

**Date**: 2025-10-11  
**Current Files**: 45 markdown files (many loose at root level)  
**Goal**: Organize into logical subdirectories

---

## Current Problem

**docs/architecture/** has 45 files, many loose at root level with unclear categorization.

---

## Proposed Subdirectories

```
docs/architecture/
├── README.md (create new)         # Master index
├── architecture-overview.md       # 🎯 Keep at root (master document)
├── documentation-map.md           # 🎯 Keep at root (navigation)
│
├── core-framework/                # 🆕 Core investigation framework (3 files)
│   ├── investigation-phases-and-ooda-integration.md
│   ├── evidence-collection-and-tracking-design.md
│   └── case-lifecycle-management.md
│
├── agentic-system/                # 🆕 Agentic framework architecture (4 files)
│   ├── agentic-framework-design-specification.md
│   ├── agent_orchestration_design.md
│   ├── query-classification-and-prompt-engineering.md
│   └── AGENTIC_FRAMEWORK_ARCHITECTURE.md (duplicate/superseded?)
│
├── api-and-data/                  # 🆕 API and data architecture (2 files)
│   ├── data-submission-design.md
│   └── api_impact_analysis.md
│
├── infrastructure/                # 🆕 Infrastructure components (4 files)
│   ├── dependency-injection-system.md
│   ├── authentication-design.md
│   ├── infrastructure-layer-guide.md
│   └── architectural-layers.md
│
├── patterns-and-guides/           # 🆕 Implementation patterns and guides (5 files)
│   ├── developer-guide.md
│   ├── container-usage-guide.md
│   ├── testing-guide.md
│   ├── service-patterns.md
│   └── interface-based-design.md
│
├── evolution/                     # 🆕 Evolution and migration (3 files)
│   ├── ARCHITECTURE_EVOLUTION.md
│   ├── AGENTIC_FRAMEWORK_MIGRATION_GUIDE.md
│   └── CONFIGURATION_SYSTEM_REFACTOR_DESIGN.md
│
├── legacy/                        # ✅ Already exists - Superseded architecture (3 files)
│   ├── DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md
│   ├── SUB_AGENT_ARCHITECTURE.md
│   └── SYSTEM_ARCHITECTURE.md
│
├── diagrams/                      # ✅ Already organized (3 files + README)
│   ├── system-architecture.md
│   ├── system-architecture-code.md
│   └── system-architecture.mmd
│
├── decisions/                     # ✅ Already organized (1 file + README)
│   └── architecture-decision-guide.md
│
└── _temp/                         # 🆕 Temporary/obsolete files (12 files)
    ├── status-reports/
    │   ├── PHASE_2_COMPLETE_SUMMARY.md
    │   ├── EVIDENCE_CENTRIC_IMPLEMENTATION_STATUS.md
    │   └── DEPLOYMENT_GUIDE.md (?)
    ├── analysis/
    │   ├── CONTEXT_ENGINEERING_ANALYSIS.md
    │   ├── CRITICAL_CONCEPTS_AND_RELATIONSHIPS.md
    │   ├── COMPONENT_INTERACTIONS.md
    │   └── CASE_AGENT_INTEGRATION_DESIGN.md
    ├── working-docs/
    │   ├── ooda_surgical_replacement.md
    │   ├── ooda_prompt_complete.md
    │   ├── AUTHENTICATION_SYSTEM_PLAN.md
    │   ├── CONVERSATIONAL_INTERACTION_MODEL_DESIGN.md
    │   └── faultmaven_system_detailed_design.md
    └── planning/
        ├── REORGANIZATION_SUMMARY.md (this can be moved here)
        └── CODE_STRUCTURE_VALIDATION.md (this can be moved here)
```

---

## File Classification

### 🎯 MASTER DOCUMENTS (Keep at Root - 2 files)
- `architecture-overview.md` - 🎯 Master architecture document (v2.0, code-aligned)
- `documentation-map.md` - Complete documentation map and status

### ✅ CORE FRAMEWORK (Move to core-framework/ - 3 files)
- `investigation-phases-and-ooda-integration.md` - 🎯 Process framework (v2.1)
- `evidence-collection-and-tracking-design.md` - 🎯 Evidence models (v2.1)
- `case-lifecycle-management.md` - 🎯 Case status (v1.0)

### ✅ AGENTIC SYSTEM (Move to agentic-system/ - 4 files)
- `agentic-framework-design-specification.md` - Primary spec
- `agent_orchestration_design.md` - Agent coordination
- `query-classification-and-prompt-engineering.md` - Classification & prompts
- `AGENTIC_FRAMEWORK_ARCHITECTURE.md` - Duplicate? (review needed)

### ✅ API AND DATA (Move to api-and-data/ - 2 files)
- `data-submission-design.md` - Data upload handling
- `api_impact_analysis.md` - API analysis

### ✅ INFRASTRUCTURE (Move to infrastructure/ - 4 files)
- `dependency-injection-system.md` - DI container
- `authentication-design.md` - Auth system
- `infrastructure-layer-guide.md` - Infrastructure guide
- `architectural-layers.md` - Layer architecture

### ✅ PATTERNS AND GUIDES (Move to patterns-and-guides/ - 5 files)
- `developer-guide.md` - Development workflow
- `container-usage-guide.md` - DI container usage
- `testing-guide.md` - Testing strategies
- `service-patterns.md` - Service patterns
- `interface-based-design.md` - Interface design

### ✅ EVOLUTION (Move to evolution/ - 3 files)
- `ARCHITECTURE_EVOLUTION.md` - Evolution history
- `AGENTIC_FRAMEWORK_MIGRATION_GUIDE.md` - Framework migration
- `CONFIGURATION_SYSTEM_REFACTOR_DESIGN.md` - Config refactor

### 🔄 LEGACY (Move to legacy/ - 3 files)
- `DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md` - Superseded by Investigation Phases
- `SUB_AGENT_ARCHITECTURE.md` - Legacy multi-agent
- `SYSTEM_ARCHITECTURE.md` - Superseded by Architecture Overview v2.0

### 🗑️ TEMPORARY/OBSOLETE (Move to _temp/ - 12 files)

**Status Reports** (_temp/status-reports/):
- `PHASE_2_COMPLETE_SUMMARY.md` - Phase completion report
- `EVIDENCE_CENTRIC_IMPLEMENTATION_STATUS.md` - Implementation status
- `DEPLOYMENT_GUIDE.md` - Temporary guide (or keep?)

**Analysis Documents** (_temp/analysis/):
- `CONTEXT_ENGINEERING_ANALYSIS.md` - Working analysis
- `CRITICAL_CONCEPTS_AND_RELATIONSHIPS.md` - Temporary analysis
- `COMPONENT_INTERACTIONS.md` - Might be valuable (review)
- `CASE_AGENT_INTEGRATION_DESIGN.md` - Integration design

**Working Documents** (_temp/working-docs/):
- `ooda_surgical_replacement.md` - Surgical replacement notes
- `ooda_prompt_complete.md` - Prompt working doc
- `AUTHENTICATION_SYSTEM_PLAN.md` - Planning doc
- `CONVERSATIONAL_INTERACTION_MODEL_DESIGN.md` - Design exploration
- `faultmaven_system_detailed_design.md` - Detailed design (superseded?)

**Planning/Reorganization** (_temp/planning/):
- `REORGANIZATION_SUMMARY.md` - This reorganization summary
- `CODE_STRUCTURE_VALIDATION.md` - Structure validation

---

## Execution Plan

### Phase 1: Create Subdirectories
```bash
cd /home/swhouse/projects/FaultMaven/docs/architecture

mkdir -p core-framework
mkdir -p agentic-system
mkdir -p api-and-data
mkdir -p infrastructure
mkdir -p patterns-and-guides
mkdir -p evolution
mkdir -p _temp/status-reports
mkdir -p _temp/analysis
mkdir -p _temp/working-docs
mkdir -p _temp/planning
```

### Phase 2: Move Core Framework (3 files)
```bash
mv investigation-phases-and-ooda-integration.md core-framework/
mv evidence-collection-and-tracking-design.md core-framework/
mv case-lifecycle-management.md core-framework/
```

### Phase 3: Move Agentic System (4 files)
```bash
mv agentic-framework-design-specification.md agentic-system/
mv agent_orchestration_design.md agentic-system/
mv query-classification-and-prompt-engineering.md agentic-system/
mv AGENTIC_FRAMEWORK_ARCHITECTURE.md agentic-system/  # Review: duplicate?
```

### Phase 4: Move API and Data (2 files)
```bash
mv data-submission-design.md api-and-data/
mv api_impact_analysis.md api-and-data/
```

### Phase 5: Move Infrastructure (4 files)
```bash
mv dependency-injection-system.md infrastructure/
mv authentication-design.md infrastructure/
mv infrastructure-layer-guide.md infrastructure/
mv architectural-layers.md infrastructure/
```

### Phase 6: Move Patterns and Guides (5 files)
```bash
mv developer-guide.md patterns-and-guides/
mv container-usage-guide.md patterns-and-guides/
mv testing-guide.md patterns-and-guides/
mv service-patterns.md patterns-and-guides/
mv interface-based-design.md patterns-and-guides/
```

### Phase 7: Move Evolution (3 files)
```bash
mv ARCHITECTURE_EVOLUTION.md evolution/
mv AGENTIC_FRAMEWORK_MIGRATION_GUIDE.md evolution/
mv CONFIGURATION_SYSTEM_REFACTOR_DESIGN.md evolution/
```

### Phase 8: Move Legacy (3 files)
```bash
mv DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md legacy/
mv SUB_AGENT_ARCHITECTURE.md legacy/
mv SYSTEM_ARCHITECTURE.md legacy/
```

### Phase 9: Move Temporary/Obsolete (12 files)
```bash
# Status reports
mv PHASE_2_COMPLETE_SUMMARY.md _temp/status-reports/
mv EVIDENCE_CENTRIC_IMPLEMENTATION_STATUS.md _temp/status-reports/
mv DEPLOYMENT_GUIDE.md _temp/status-reports/  # Review: might be valuable

# Analysis documents
mv CONTEXT_ENGINEERING_ANALYSIS.md _temp/analysis/
mv CRITICAL_CONCEPTS_AND_RELATIONSHIPS.md _temp/analysis/
mv COMPONENT_INTERACTIONS.md _temp/analysis/  # Review: might be valuable
mv CASE_AGENT_INTEGRATION_DESIGN.md _temp/analysis/

# Working documents
mv ooda_surgical_replacement.md _temp/working-docs/
mv ooda_prompt_complete.md _temp/working-docs/
mv AUTHENTICATION_SYSTEM_PLAN.md _temp/working-docs/
mv CONVERSATIONAL_INTERACTION_MODEL_DESIGN.md _temp/working-docs/
mv faultmaven_system_detailed_design.md _temp/working-docs/

# Planning documents (from this reorganization effort)
mv REORGANIZATION_SUMMARY.md _temp/planning/
mv CODE_STRUCTURE_VALIDATION.md _temp/planning/
```

### Phase 10: Create Index README Files
Create README.md in each subdirectory (see detailed content below)

---

## Estimated Time

- Phase 1: Create directories (2 min)
- Phases 2-9: Move files (15 min)
- Phase 10: Create index files (20 min)

**Total**: ~40 minutes

---

## After Reorganization

```
docs/architecture/
├── README.md                     # 🆕 Master index
├── architecture-overview.md      # 🎯 Keep at root (master doc)
├── documentation-map.md          # 🎯 Keep at root (navigation)
│
├── core-framework/               # 🆕 3 authoritative framework docs
│   ├── README.md
│   ├── investigation-phases-and-ooda-integration.md
│   ├── evidence-collection-and-tracking-design.md
│   └── case-lifecycle-management.md
│
├── agentic-system/               # 🆕 4 agentic framework docs
│   ├── README.md
│   ├── agentic-framework-design-specification.md
│   ├── agent_orchestration_design.md
│   ├── query-classification-and-prompt-engineering.md
│   └── AGENTIC_FRAMEWORK_ARCHITECTURE.md
│
├── api-and-data/                 # 🆕 2 API/data docs
│   ├── README.md
│   ├── data-submission-design.md
│   └── api_impact_analysis.md
│
├── infrastructure/               # 🆕 4 infrastructure docs
│   ├── README.md
│   ├── dependency-injection-system.md
│   ├── authentication-design.md
│   ├── infrastructure-layer-guide.md
│   └── architectural-layers.md
│
├── patterns-and-guides/          # 🆕 5 implementation guides
│   ├── README.md
│   ├── developer-guide.md
│   ├── container-usage-guide.md
│   ├── testing-guide.md
│   ├── service-patterns.md
│   └── interface-based-design.md
│
├── evolution/                    # 🆕 3 evolution/migration docs
│   ├── README.md
│   ├── ARCHITECTURE_EVOLUTION.md
│   ├── AGENTIC_FRAMEWORK_MIGRATION_GUIDE.md
│   └── CONFIGURATION_SYSTEM_REFACTOR_DESIGN.md
│
├── legacy/                       # ✅ 3 superseded docs
│   ├── README.md (create)
│   ├── DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md
│   ├── SUB_AGENT_ARCHITECTURE.md
│   └── SYSTEM_ARCHITECTURE.md
│
├── diagrams/                     # ✅ Already organized (3 + README)
│   └── ...
│
├── decisions/                    # ✅ Already organized (1 + README)
│   └── ...
│
└── _temp/                        # 🗑️ 12 temporary/obsolete files
    ├── status-reports/           # 3 files
    ├── analysis/                 # 4 files
    ├── working-docs/             # 5 files
    └── planning/                 # 2 files (our reorganization docs)
```

---

## File Categorization

### 🎯 MASTER (Keep at Root - 2 files)
- `architecture-overview.md` - Master architecture document
- `documentation-map.md` - Complete documentation map

### ✅ CORE FRAMEWORK (3 files)
These are the authoritative process framework documents:
- investigation-phases-and-ooda-integration.md (v2.1)
- evidence-collection-and-tracking-design.md (v2.1)
- case-lifecycle-management.md (v1.0)

### ✅ AGENTIC SYSTEM (4 files)
Agentic framework architecture and implementation:
- agentic-framework-design-specification.md (primary)
- agent_orchestration_design.md
- query-classification-and-prompt-engineering.md
- AGENTIC_FRAMEWORK_ARCHITECTURE.md (review: might be duplicate)

### ✅ API AND DATA (2 files)
API and data architecture:
- data-submission-design.md
- api_impact_analysis.md

### ✅ INFRASTRUCTURE (4 files)
Infrastructure component architecture:
- dependency-injection-system.md
- authentication-design.md
- infrastructure-layer-guide.md
- architectural-layers.md

### ✅ PATTERNS AND GUIDES (5 files)
Implementation patterns and developer guides:
- developer-guide.md
- container-usage-guide.md
- testing-guide.md
- service-patterns.md
- interface-based-design.md

### ✅ EVOLUTION (3 files)
Architecture evolution and migration:
- ARCHITECTURE_EVOLUTION.md
- AGENTIC_FRAMEWORK_MIGRATION_GUIDE.md
- CONFIGURATION_SYSTEM_REFACTOR_DESIGN.md

### 🔄 LEGACY (3 files - Already in legacy/)
Superseded architecture documents:
- DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md
- SUB_AGENT_ARCHITECTURE.md
- SYSTEM_ARCHITECTURE.md

### 🗑️ TEMPORARY (12 files - Move to _temp/)

**Status Reports** (3 files):
- PHASE_2_COMPLETE_SUMMARY.md - Phase completion
- EVIDENCE_CENTRIC_IMPLEMENTATION_STATUS.md - Implementation status
- DEPLOYMENT_GUIDE.md - Guide (review: might be valuable)

**Analysis** (4 files):
- CONTEXT_ENGINEERING_ANALYSIS.md - Working analysis
- CRITICAL_CONCEPTS_AND_RELATIONSHIPS.md - Concepts analysis
- COMPONENT_INTERACTIONS.md - Component analysis (review: might be valuable)
- CASE_AGENT_INTEGRATION_DESIGN.md - Integration design

**Working Documents** (5 files):
- ooda_surgical_replacement.md - OODA replacement notes
- ooda_prompt_complete.md - Prompt working notes
- AUTHENTICATION_SYSTEM_PLAN.md - Planning document
- CONVERSATIONAL_INTERACTION_MODEL_DESIGN.md - Design exploration
- faultmaven_system_detailed_design.md - Detailed design (superseded?)

**Planning** (2 files - from this reorganization):
- REORGANIZATION_SUMMARY.md
- CODE_STRUCTURE_VALIDATION.md

---

## Summary Stats

| Category | Count | Action |
|----------|-------|--------|
| Master (root) | 2 | Keep at root |
| Core Framework | 3 | Move to core-framework/ |
| Agentic System | 4 | Move to agentic-system/ |
| API and Data | 2 | Move to api-and-data/ |
| Infrastructure | 4 | Move to infrastructure/ |
| Patterns & Guides | 5 | Move to patterns-and-guides/ |
| Evolution | 3 | Move to evolution/ |
| Legacy | 3 | Already in legacy/ |
| Diagrams | 3 + README | Already in diagrams/ |
| Decisions | 1 + README | Already in decisions/ |
| Temporary/Obsolete | 12 | Move to _temp/ |
| **Total** | **45** | |

**New subdirectories**: 6  
**Index README files to create**: 6

---

## Benefits

### ✅ Better Organization
- Logical grouping by domain
- Clear categories (framework, agentic, infrastructure, etc.)
- Easy to find related documents

### ✅ Cleaner Navigation
- 2 master docs at root (not 45!)
- Each subdirectory has index README
- Related docs grouped together

### ✅ Easier Maintenance
- Update frequency clear by category
- Temporary files isolated
- Legacy docs separated

### ✅ Code Alignment
- Matches architecture-overview.md structure
- Easier to map docs to code modules

---

## Status

**Ready for execution**: Yes  
**Estimated time**: 40 minutes  
**Risk**: Low (all files preserved)

---

**End of Plan**

