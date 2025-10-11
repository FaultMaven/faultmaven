# Unreferenced Files Check - docs/architecture/

**Date**: 2025-10-11  
**Purpose**: Identify files at architecture/ root NOT referenced in architecture-overview.md

---

## Files Currently at architecture/ Root

From list_dir, these files are at root level (excluding subdirectories):
1. README.md ✅ (index)
2. architecture-overview.md ✅ (master)
3. documentation-map.md ✅ (navigation)
4. investigation-phases-and-ooda-integration.md ✅ (referenced)
5. evidence-collection-and-tracking-design.md ✅ (referenced)
6. case-lifecycle-management.md ✅ (referenced)
7. agentic-framework-design-specification.md ✅ (referenced)
8. agent_orchestration_design.md ✅ (referenced as "Agent Orchestration Design")
9. query-classification-and-prompt-engineering.md ✅ (referenced)
10. data-submission-design.md ✅ (referenced)
11. authentication-design.md ✅ (referenced)
12. dependency-injection-system.md ✅ (referenced as "Dependency Injection System")
13. developer-guide.md ✅ (referenced)
14. container-usage-guide.md ✅ (referenced as "Container Usage Guide")
15. testing-guide.md ✅ (referenced as "Testing Guide")
16. service-patterns.md ✅ (referenced as "Service Layer Patterns")
17. interface-based-design.md ✅ (referenced as "Interface-Based Design Guide")
18. ARCHITECTURE_EVOLUTION.md ✅ (referenced as "Architecture Evolution")
19. AGENTIC_FRAMEWORK_MIGRATION_GUIDE.md ✅ (referenced as "Agentic Framework Migration Guide")
20. CONFIGURATION_SYSTEM_REFACTOR_DESIGN.md ✅ (referenced as "Configuration System Refactor")
21. DI-diagram.mmd ✅ (diagram source)
22. faultmaven_integrated_design.md_replaced ❓ (old file?)
23. REVISED_ARCHITECTURE_CLEANUP.md ❌ (my planning doc)

---

## Unreferenced Files Identified

### 1. ❌ REVISED_ARCHITECTURE_CLEANUP.md
**Type**: Reorganization planning document  
**Action**: Move to `_temp/planning/`

### 2. ❓ faultmaven_integrated_design.md_replaced
**Type**: Old file with ".md_replaced" extension (superseded?)  
**Action**: Move to `_temp/working-docs/` or delete

---

## Recommended Actions

```bash
cd /home/swhouse/projects/FaultMaven/docs/architecture

# Move planning doc to _temp/
mv REVISED_ARCHITECTURE_CLEANUP.md _temp/planning/

# Move old replaced file to _temp/
mv faultmaven_integrated_design.md_replaced _temp/working-docs/
```

---

## After Cleanup

**Files at architecture/ root**: ~21 files
- 3 master/index files (README, architecture-overview, documentation-map)
- 17 referenced architecture documents
- 1 diagram source (DI-diagram.mmd)

All files at root will be either:
- ✅ Master documents (README, overview, map)
- ✅ Referenced in architecture-overview.md
- ✅ Diagram sources (.mmd files)

---

**Status**: Ready to execute cleanup

