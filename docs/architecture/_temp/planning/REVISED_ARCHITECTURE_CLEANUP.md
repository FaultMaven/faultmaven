# Architecture Folder Cleanup - Revised Plan

**Date**: 2025-10-11  
**Strategy**: Keep referenced docs at root, organize unreferenced but valuable docs

---

## Categories

### 🎯 KEEP AT ROOT (Referenced by architecture-overview.md)
Active architecture documents linked from the master document:
- architecture-overview.md (master)
- documentation-map.md (navigation)
- investigation-phases-and-ooda-integration.md
- evidence-collection-and-tracking-design.md
- case-lifecycle-management.md
- agentic-framework-design-specification.md
- agent_orchestration_design.md
- query-classification-and-prompt-engineering.md
- data-submission-design.md
- authentication-design.md
- dependency-injection-system.md
- developer-guide.md
- container-usage-guide.md
- testing-guide.md
- service-patterns.md
- interface-based-design.md
- ARCHITECTURE_EVOLUTION.md
- AGENTIC_FRAMEWORK_MIGRATION_GUIDE.md
- CONFIGURATION_SYSTEM_REFACTOR_DESIGN.md

### 📚 MOVE to reference/ (Valid but Unreferenced)
Valuable information not currently referenced in architecture-overview.md:
- COMPONENT_INTERACTIONS.md - Component interaction patterns (valuable reference)
- CRITICAL_CONCEPTS_AND_RELATIONSHIPS.md - Concept definitions (valuable reference)
- CONTEXT_ENGINEERING_ANALYSIS.md - Context analysis (valuable insights)
- infrastructure-layer-guide.md - Infrastructure guide (valuable reference)
- architectural-layers.md - Layer architecture (valuable reference)
- CASE_AGENT_INTEGRATION_DESIGN.md - Integration design
- CONVERSATIONAL_INTERACTION_MODEL_DESIGN.md - Interaction model design
- AGENTIC_FRAMEWORK_ARCHITECTURE.md - Framework architecture (possibly duplicate?)
- faultmaven_system_detailed_design.md - Detailed design (comprehensive reference)

### 🔄 MOVE to legacy/ (Superseded)
Documents superseded by newer designs:
- DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md - Superseded by Investigation Phases
- SUB_AGENT_ARCHITECTURE.md - Legacy multi-agent design
- SYSTEM_ARCHITECTURE.md - Superseded by Architecture Overview v2.0

### 🗑️ MOVE to _temp/ (Temporary/Obsolete)
Status reports, working notes, planning documents:

**Status Reports**:
- PHASE_2_COMPLETE_SUMMARY.md
- EVIDENCE_CENTRIC_IMPLEMENTATION_STATUS.md
- DEPLOYMENT_GUIDE.md (or keep as reference?)

**Working Documents**:
- ooda_surgical_replacement.md - Surgical replacement notes
- ooda_prompt_complete.md - Prompt working notes
- AUTHENTICATION_SYSTEM_PLAN.md - Planning doc
- api_impact_analysis.md - Analysis notes

**Planning** (from this reorganization effort):
- REORGANIZATION_SUMMARY.md
- CODE_STRUCTURE_VALIDATION.md
- ARCHITECTURE_FOLDER_REORGANIZATION.md (the original overcomplex plan)

---

## Execution

### Step 1: Create reference/ directory
```bash
cd /home/swhouse/projects/FaultMaven/docs/architecture
mkdir -p reference
mkdir -p _temp/status-reports
mkdir -p _temp/working-docs
mkdir -p _temp/planning
```

### Step 2: Move to reference/ (9 files)
```bash
# Valuable reference material
mv COMPONENT_INTERACTIONS.md reference/
mv CRITICAL_CONCEPTS_AND_RELATIONSHIPS.md reference/
mv CONTEXT_ENGINEERING_ANALYSIS.md reference/
mv infrastructure-layer-guide.md reference/
mv architectural-layers.md reference/
mv CASE_AGENT_INTEGRATION_DESIGN.md reference/
mv CONVERSATIONAL_INTERACTION_MODEL_DESIGN.md reference/
mv AGENTIC_FRAMEWORK_ARCHITECTURE.md reference/  # Review: duplicate?
mv faultmaven_system_detailed_design.md reference/
```

### Step 3: Move to legacy/ (3 files)
```bash
mv DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md legacy/
mv SUB_AGENT_ARCHITECTURE.md legacy/
mv SYSTEM_ARCHITECTURE.md legacy/
```

### Step 4: Move to _temp/ (9 files)
```bash
# Status reports
mv PHASE_2_COMPLETE_SUMMARY.md _temp/status-reports/
mv EVIDENCE_CENTRIC_IMPLEMENTATION_STATUS.md _temp/status-reports/
mv DEPLOYMENT_GUIDE.md _temp/status-reports/  # Review: might be valuable

# Working documents
mv ooda_surgical_replacement.md _temp/working-docs/
mv ooda_prompt_complete.md _temp/working-docs/
mv AUTHENTICATION_SYSTEM_PLAN.md _temp/working-docs/
mv api_impact_analysis.md _temp/working-docs/

# Planning (from reorganization effort)
mv REORGANIZATION_SUMMARY.md _temp/planning/
mv CODE_STRUCTURE_VALIDATION.md _temp/planning/
```

### Step 5: Create README files
```bash
# reference/README.md
cat > reference/README.md << 'EOF'
# Architecture Reference Documents

Valuable architecture documents providing detailed analysis, patterns, and design explorations. These documents contain important information but are not currently linked from the main architecture-overview.md.

## Component Architecture
- [Component Interactions](./COMPONENT_INTERACTIONS.md) - Component interaction patterns and data flows
- [Critical Concepts and Relationships](./CRITICAL_CONCEPTS_AND_RELATIONSHIPS.md) - Core concept definitions and relationships
- [Case-Agent Integration Design](./CASE_AGENT_INTEGRATION_DESIGN.md) - Integration architecture

## Infrastructure & Layers
- [Infrastructure Layer Guide](./infrastructure-layer-guide.md) - Comprehensive infrastructure guide
- [Architectural Layers](./architectural-layers.md) - Layer architecture and responsibilities

## Design Analysis
- [Context Engineering Analysis](./CONTEXT_ENGINEERING_ANALYSIS.md) - Context management analysis
- [Conversational Interaction Model](./CONVERSATIONAL_INTERACTION_MODEL_DESIGN.md) - Interaction model design
- [Detailed System Design](./faultmaven_system_detailed_design.md) - Comprehensive system design

## Framework Variants
- [Agentic Framework Architecture](./AGENTIC_FRAMEWORK_ARCHITECTURE.md) - Alternative framework view (check if duplicate)

---

**Purpose**: These documents provide deeper dives and alternative perspectives that complement the main architecture documentation. They may be integrated into architecture-overview.md in future updates.

**Last Updated**: 2025-10-11
EOF

# legacy/README.md (update existing or create)
cat > legacy/README.md << 'EOF'
# Legacy Architecture Documents

Historical architecture documents that have been superseded by newer designs. These are preserved for reference and to understand the evolution of the system.

## Superseded Architectures

- **[Doctor-Patient Prompting v1.0](./DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md)** - Original evidence-based prompting architecture
  - **Superseded by**: [Investigation Phases and OODA Integration Framework v2.1](../investigation-phases-and-ooda-integration.md)
  - **Note**: Phase-specific sub-agents still exist in code at `faultmaven/services/agentic/doctor_patient/sub_agents/`

- **[Sub-Agent Architecture v1.0](./SUB_AGENT_ARCHITECTURE.md)** - Original multi-agent coordination system
  - **Superseded by**: [Agentic Framework Design Specification](../agentic-framework-design-specification.md)
  - **Value**: Reference for agent coordination patterns

- **[System Architecture v1.0](./SYSTEM_ARCHITECTURE.md)** - Original architecture document
  - **Superseded by**: [Architecture Overview v2.0](../architecture-overview.md)
  - **Value**: Historical context

---

**Purpose**: Preserve institutional knowledge and design evolution history. These documents explain "why we changed" and provide context for current architecture decisions.

**Last Updated**: 2025-10-11
EOF
```

---

## Final Structure (After Cleanup)

```
docs/architecture/
├── README.md                     # 🆕 Master index (to create)
├── architecture-overview.md      # 🎯 Master document (keep at root)
├── documentation-map.md          # 🎯 Navigation (keep at root)
│
├── [~19 active architecture docs at root]
│   ├── investigation-phases-and-ooda-integration.md
│   ├── evidence-collection-and-tracking-design.md
│   ├── case-lifecycle-management.md
│   ├── agentic-framework-design-specification.md
│   ├── agent_orchestration_design.md
│   ├── query-classification-and-prompt-engineering.md
│   ├── data-submission-design.md
│   ├── authentication-design.md
│   ├── dependency-injection-system.md
│   ├── developer-guide.md
│   ├── container-usage-guide.md
│   ├── testing-guide.md
│   ├── service-patterns.md
│   ├── interface-based-design.md
│   ├── ARCHITECTURE_EVOLUTION.md
│   ├── AGENTIC_FRAMEWORK_MIGRATION_GUIDE.md
│   └── CONFIGURATION_SYSTEM_REFACTOR_DESIGN.md
│
├── reference/                    # 🆕 Valuable but unreferenced (9 files)
│   ├── README.md
│   ├── COMPONENT_INTERACTIONS.md
│   ├── CRITICAL_CONCEPTS_AND_RELATIONSHIPS.md
│   ├── CONTEXT_ENGINEERING_ANALYSIS.md
│   ├── infrastructure-layer-guide.md
│   ├── architectural-layers.md
│   ├── CASE_AGENT_INTEGRATION_DESIGN.md
│   ├── CONVERSATIONAL_INTERACTION_MODEL_DESIGN.md
│   ├── AGENTIC_FRAMEWORK_ARCHITECTURE.md
│   └── faultmaven_system_detailed_design.md
│
├── legacy/                       # ✅ Superseded architecture (3 files)
│   ├── README.md
│   ├── DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md
│   ├── SUB_AGENT_ARCHITECTURE.md
│   └── SYSTEM_ARCHITECTURE.md
│
├── diagrams/                     # ✅ Already organized (3 files + README)
├── decisions/                    # ✅ Already organized (1 file + README)
│
└── _temp/                        # 🗑️ Temporary/obsolete (9 files)
    ├── status-reports/           # 3 files
    ├── working-docs/             # 4 files
    └── planning/                 # 2 files
```

---

**This makes much more sense!** 

Should I proceed with this **revised cleanup**:
- Only move **valuable but unreferenced** docs to `reference/`
- Move **temporary/obsolete** to `_temp/`
- Move **legacy** to `legacy/`
- **Keep active docs at architecture/ root** (don't break links!)
