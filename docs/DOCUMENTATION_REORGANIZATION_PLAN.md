# Documentation Reorganization Plan

**Date**: 2025-10-11  
**Purpose**: Reorganize scattered documentation into a clean, hierarchical structure  
**Status**: PROPOSAL

---

## Summary

### Files to KEEP and ORGANIZE (Permanent Documentation)
- Architecture diagrams → `docs/architecture/diagrams/`
- Architecture decision guide → `docs/architecture/decisions/`
- Knowledge base system → `docs/specifications/`
- User guide → `docs/getting-started/`
- Provider guide → `docs/development/`
- Logging policy → `docs/logging/`
- Schema alignment → `docs/api/`
- Opik setup → `docs/infrastructure/`
- Test guides → `docs/testing/`

### Files to MOVE to _temp/ (Temporary/Obsolete)
- Implementation plans and status reports (PHASE_0_*, IMPLEMENTATION_*, DOCTOR_PATIENT_*)
- AI working notes (CLAUDE.md)
- Technical debt tracking (TECHNICAL_DEBT.md)
- Future enhancements (FUTURE_ENHANCEMENTS.md)
- Duplicate system requirements
- Legacy architecture documents (MICROSERVICES_ARCHITECTURE.md, TECHNICAL_SPECIFICATIONS.md)

**Total Files Moving to _temp/**: ~15 files  
**After 1-2 weeks review**: Delete `_temp/` directory

---

## Current Problems

### 1. **Root-Level Clutter** (10+ docs at project root)
```
FaultMaven/
├── architecture-diagram.md          # Should be in docs/architecture/
├── CLAUDE.md                         # AI assistant notes - should be in docs/development/
├── DOCTOR_PATIENT_IMPLEMENTATION_SUMMARY.md  # Should be in archive/ or docs/releases/
├── FRONTEND_DATA_UPLOAD_IMPLEMENTATION_REQUEST.md  # Should be in docs/features/ or archive/
├── IMPLEMENTATION_COMPLETE.md        # Should be in docs/releases/
├── IMPLEMENTATION_PLAN.md            # Should be in docs/releases/ or recycle/
├── IMPLEMENTATION_README.md          # Should be in docs/development/
├── MICROSERVICES_ARCHITECTURE.md     # Should be in archive/ (superseded?)
├── PHASE_0_AUDIT_REPORT.md          # Should be in docs/releases/
├── PHASE_0_ENHANCEMENTS_SUMMARY.md  # Should be in docs/releases/
├── README.md                         # ✅ Correct - keep at root
├── TECHNICAL_SPECIFICATIONS.md       # Should be in docs/specifications/
```

### 2. **Code Directory Contains Docs**
```
faultmaven/
├── ARCHITECTURE_DIAGRAM.md          # Should be in docs/architecture/
├── ARCHITECTURE_DIAGRAM.mmd         # Should be in docs/architecture/
```

### 3. **Tests Directory Contains Docs**
```
tests/
├── ARCHITECTURE_TESTING_GUIDE.md    # Should be in docs/testing/
├── NEW_TEST_PATTERNS.md             # Should be in docs/testing/
```

### 4. **Loose Files in docs/**
```
docs/
├── ARCHITECTURE_DECISION_GUIDE.md   # Should be in docs/architecture/ or docs/guides/
├── CODE_OF_CONDUCT.md               # ✅ Correct (community standard)
├── CONTRIBUTING.md                  # ✅ Correct (community standard)
├── faultmaven_system_requirements_v2.md  # Duplicate - consolidate with system-requirements-specification.md
├── FAULTMAVEN_SYSTEM_REQUIREMENTS.md     # Duplicate - consolidate
├── FLAGS_AND_CONFIG.md              # Should be in docs/development/ or docs/guides/
├── FUTURE_ENHANCEMENTS.md           # Should be in docs/releases/ or docs/roadmap/
├── how-to-add-providers.md          # Should be in docs/guides/ or docs/development/
├── KNOWLEDGE_BASE_SYSTEM.md         # Should be in docs/architecture/ or docs/specifications/
├── LOGGING_POLICY.md                # Should be in docs/logging/ (consolidate)
├── opik-setup.md                    # Should be in docs/infrastructure/
├── SCHEMA_ALIGNMENT.md              # Should be in docs/architecture/ or docs/api/
├── TECHNICAL_DEBT.md                # Should be in docs/releases/ or docs/development/
├── USER_GUIDE.md                    # ✅ Correct placement
```

### 5. **Duplicate Content**
- `FAULTMAVEN_SYSTEM_REQUIREMENTS.md` vs `faultmaven_system_requirements_v2.md` vs `system-requirements-specification.md`
- Multiple architecture diagrams in different locations
- Implementation plans and summaries scattered

---

## Recommended Structure

### **Clean Hierarchy** (Industry Best Practices)

```
FaultMaven/
│
├── README.md                        # ✅ Project overview (keep at root)
├── LICENSE                          # ✅ License file (keep at root)
│
├── docs/                            # All documentation
│   │
│   ├── README.md                    # Documentation index and navigation
│   │
│   ├── CODE_OF_CONDUCT.md          # ✅ Community standards
│   ├── CONTRIBUTING.md              # ✅ Contribution guidelines
│   │
│   ├── getting-started/             # NEW: Quick start and onboarding
│   │   ├── README.md
│   │   ├── installation.md
│   │   ├── quickstart.md
│   │   └── user-guide.md            # Move from docs/USER_GUIDE.md
│   │
│   ├── architecture/                # ✅ System architecture (well organized)
│   │   ├── README.md                # Index linking to architecture-overview.md
│   │   ├── architecture-overview.md # 🎯 Master document
│   │   ├── diagrams/                # NEW: Centralize all diagrams
│   │   │   ├── system-architecture.md      # From root/architecture-diagram.md
│   │   │   ├── system-architecture.mmd     # From faultmaven/ARCHITECTURE_DIAGRAM.mmd
│   │   │   ├── DI-diagram.mmd
│   │   │   └── ... (other diagrams)
│   │   ├── investigation-phases-and-ooda-integration.md
│   │   ├── evidence-collection-and-tracking-design.md
│   │   ├── case-lifecycle-management.md
│   │   ├── agentic-framework-design-specification.md
│   │   ├── query-classification-and-prompt-engineering.md
│   │   ├── data-submission-design.md
│   │   ├── authentication-design.md
│   │   ├── dependency-injection-system.md
│   │   ├── ... (all other architecture docs)
│   │   │
│   │   ├── decisions/               # NEW: Architecture Decision Records (ADRs)
│   │   │   ├── README.md
│   │   │   ├── 001-agentic-framework.md
│   │   │   ├── 002-investigation-phases.md
│   │   │   └── architecture-decision-guide.md  # Move from docs/
│   │   │
│   │   ├── legacy/                  # Legacy/superseded architecture
│   │   │   ├── DOCTOR_PATIENT_PROMPTING_ARCHITECTURE.md
│   │   │   ├── SUB_AGENT_ARCHITECTURE.md
│   │   │   ├── SYSTEM_ARCHITECTURE.md (v1.0)
│   │   │   └── microservices-architecture.md  # Move from root/
│   │   │
│   │   └── diagrams-source/         # Source files for diagrams
│   │
│   ├── specifications/              # ✅ System requirements and specs
│   │   ├── README.md
│   │   ├── system-requirements-specification.md  # 🎯 Authoritative (v2.0)
│   │   ├── CASE_SESSION_CONCEPTS.md
│   │   ├── SESSION_MANAGEMENT_SPEC.md
│   │   ├── CONFIGURATION_MANAGEMENT_SPEC.md
│   │   ├── knowledge-base-system.md  # Move from docs/KNOWLEDGE_BASE_SYSTEM.md
│   │   └── ... (other specs)
│   │
│   ├── api/                         # ✅ API documentation
│   │   ├── README.md
│   │   ├── openapi.locked.yaml      # 🎯 Authoritative OpenAPI spec
│   │   ├── schema-alignment.md      # Move from docs/SCHEMA_ALIGNMENT.md
│   │   ├── v3.1.0-TROUBLESHOOTING-GUIDE.md
│   │   └── ... (other API docs)
│   │
│   ├── development/                 # ✅ Developer guides
│   │   ├── README.md
│   │   ├── setup-and-installation.md  # Move from root/IMPLEMENTATION_README.md
│   │   ├── ENVIRONMENT_VARIABLES.md
│   │   ├── CONTEXT_MANAGEMENT.md
│   │   ├── TOKEN_ESTIMATION.md
│   │   ├── flags-and-configuration.md  # Move from docs/FLAGS_AND_CONFIG.md
│   │   ├── how-to-add-providers.md     # Move from docs/
│   │   ├── claude-ai-notes.md          # Move from root/CLAUDE.md
│   │   └── technical-debt.md           # Move from docs/TECHNICAL_DEBT.md
│   │
│   ├── guides/                      # ✅ How-to guides and tutorials
│   │   ├── README.md
│   │   ├── agentic-framework-integration.md
│   │   └── ... (other guides)
│   │
│   ├── infrastructure/              # ✅ Infrastructure setup
│   │   ├── README.md
│   │   ├── opik-setup.md            # Move from docs/
│   │   ├── Local-LLM-Setup.md
│   │   ├── redis-architecture-guide.md
│   │   ├── KB_METADATA_PERSISTENCE.md
│   │   └── ... (other infra docs)
│   │
│   ├── testing/                     # ✅ Testing documentation
│   │   ├── README.md
│   │   ├── testing-strategy.md
│   │   ├── architecture-testing-guide.md  # Move from tests/
│   │   ├── new-test-patterns.md           # Move from tests/
│   │   └── ... (other testing docs)
│   │
│   ├── security/                    # ✅ Security documentation
│   │   ├── README.md
│   │   └── ... (security docs)
│   │
│   ├── logging/                     # ✅ Logging documentation
│   │   ├── README.md
│   │   ├── logging-policy.md        # Move from docs/LOGGING_POLICY.md
│   │   ├── architecture.md
│   │   ├── configuration.md
│   │   └── ... (other logging docs)
│   │
│   ├── frontend/                    # ✅ Frontend documentation
│   │   ├── README.md
│   │   ├── api-integration.md
│   │   └── ... (other frontend docs)
│   │
│   ├── releases/                    # Release notes (if needed later)
│   │   ├── README.md
│   │   └── changelog.md             # NEW: Consolidated changelog (create if needed)
│   │
│   ├── runbooks/                    # ✅ Operational runbooks
│   │   └── ... (operational guides)
│   │
│   ├── troubleshooting/             # ✅ Troubleshooting guides
│   │   └── ... (troubleshooting docs)
│   │
│   ├── migration/                   # ✅ Migration guides
│   │   └── ... (migration docs)
│   │
│   └── features/                    # ✅ Feature documentation
│       ├── README.md
│       ├── runbook-creation.md
│       └── frontend-data-upload.md  # Move from root/FRONTEND_DATA_UPLOAD_IMPLEMENTATION_REQUEST.md
│
├── faultmaven/                      # ✅ Source code ONLY (no docs!)
│   ├── api/
│   ├── core/
│   ├── infrastructure/
│   ├── models/
│   ├── services/
│   └── ... (code only)
│
├── tests/                           # ✅ Test code ONLY (no docs!)
│   └── ... (test code only)
│
├── archive/                         # ✅ Archived/superseded code
│   └── ... (old implementations)
│
└── recycle/                         # ✅ Docs to be reviewed/deleted
    └── ... (candidate for deletion)
```

---

## Quick Reference: Where Files Are Going

| Current Location | Type | Destination |
|-----------------|------|-------------|
| **Root Level** | | |
| `architecture-diagram.md` | Keep | `docs/architecture/diagrams/system-architecture.md` |
| `CLAUDE.md` | Temp | `_temp/root-level-docs/` |
| `PHASE_0_AUDIT_REPORT.md` | Temp | `_temp/root-level-docs/` |
| `PHASE_0_ENHANCEMENTS_SUMMARY.md` | Temp | `_temp/root-level-docs/` |
| `IMPLEMENTATION_COMPLETE.md` | Temp | `_temp/root-level-docs/` |
| `IMPLEMENTATION_PLAN.md` | Temp | `_temp/root-level-docs/` |
| `IMPLEMENTATION_README.md` | Temp | `_temp/root-level-docs/` |
| `DOCTOR_PATIENT_IMPLEMENTATION_SUMMARY.md` | Temp | `_temp/root-level-docs/` |
| `FRONTEND_DATA_UPLOAD_IMPLEMENTATION_REQUEST.md` | Temp | `_temp/root-level-docs/` |
| `MICROSERVICES_ARCHITECTURE.md` | Temp | `_temp/root-level-docs/` |
| `TECHNICAL_SPECIFICATIONS.md` | Temp | `_temp/root-level-docs/` |
| **faultmaven/ (code dir)** | | |
| `faultmaven/ARCHITECTURE_DIAGRAM.md` | Keep | `docs/architecture/diagrams/system-architecture-code.md` |
| `faultmaven/ARCHITECTURE_DIAGRAM.mmd` | Keep | `docs/architecture/diagrams/system-architecture.mmd` |
| **docs/ (loose files)** | | |
| `ARCHITECTURE_DECISION_GUIDE.md` | Keep | `architecture/decisions/architecture-decision-guide.md` |
| `KNOWLEDGE_BASE_SYSTEM.md` | Keep | `specifications/knowledge-base-system.md` |
| `how-to-add-providers.md` | Keep | `development/how-to-add-providers.md` |
| `opik-setup.md` | Keep | `infrastructure/opik-setup.md` |
| `SCHEMA_ALIGNMENT.md` | Keep | `api/schema-alignment.md` |
| `LOGGING_POLICY.md` | Keep | `logging/logging-policy.md` |
| `USER_GUIDE.md` | Keep | `getting-started/user-guide.md` |
| `FLAGS_AND_CONFIG.md` | Temp | `_temp/loose-docs/` |
| `TECHNICAL_DEBT.md` | Temp | `_temp/loose-docs/` |
| `FUTURE_ENHANCEMENTS.md` | Temp | `_temp/loose-docs/` |
| `FAULTMAVEN_SYSTEM_REQUIREMENTS.md` | Temp | `_temp/duplicates/` (duplicate) |
| `faultmaven_system_requirements_v2.md` | Temp | `_temp/duplicates/` (duplicate) |
| **tests/ (test dir)** | | |
| `tests/ARCHITECTURE_TESTING_GUIDE.md` | Keep | `docs/testing/architecture-testing-guide.md` |
| `tests/NEW_TEST_PATTERNS.md` | Keep | `docs/testing/new-test-patterns.md` |

**Summary**:
- **Keep & Organize**: ~12 files (permanent documentation)
- **Move to _temp/**: ~15 files (temporary/obsolete)
- **Already in recycle/**: Leave as-is (can delete later)

---

## Migration Plan

### Phase 1: Create New Structure (15 minutes)
```bash
cd /home/swhouse/projects/FaultMaven

# Create new directories in docs/
mkdir -p docs/getting-started
mkdir -p docs/architecture/diagrams
mkdir -p docs/architecture/decisions
mkdir -p docs/architecture/legacy

# Create temporary folders for obsolete files
mkdir -p _temp/root-level-docs
mkdir -p _temp/loose-docs  
mkdir -p _temp/duplicates
```

**Directories NOT Created**:
- ~~`docs/releases/phase-0/`, `phase-1/`, `phase-2/`~~ - Phase reports are temporary, moving to `_temp/`
- ~~`docs/releases/implementation-plans/`~~ - Implementation plans are temporary, moving to `_temp/`

### Phase 2: Move Root-Level Docs (20 minutes)

**Strategy**: Keep only permanent docs, move temporary/obsolete to `_temp/` for later cleanup

```bash
# Create temporary folder for obsolete files
mkdir -p _temp/root-level-docs

# PERMANENT DOCS - Keep these, move to proper location
# Architecture diagrams
mv architecture-diagram.md docs/architecture/diagrams/system-architecture.md
mv faultmaven/ARCHITECTURE_DIAGRAM.md docs/architecture/diagrams/system-architecture-code.md
mv faultmaven/ARCHITECTURE_DIAGRAM.mmd docs/architecture/diagrams/system-architecture.mmd

# TEMPORARY/OBSOLETE - Move to _temp/ for later deletion
# Implementation status reports (temporary documentation)
mv PHASE_0_AUDIT_REPORT.md _temp/root-level-docs/
mv PHASE_0_ENHANCEMENTS_SUMMARY.md _temp/root-level-docs/
mv IMPLEMENTATION_COMPLETE.md _temp/root-level-docs/
mv DOCTOR_PATIENT_IMPLEMENTATION_SUMMARY.md _temp/root-level-docs/
mv IMPLEMENTATION_PLAN.md _temp/root-level-docs/
mv IMPLEMENTATION_README.md _temp/root-level-docs/
mv FRONTEND_DATA_UPLOAD_IMPLEMENTATION_REQUEST.md _temp/root-level-docs/

# AI working notes (can be referenced but not permanent documentation)
mv CLAUDE.md _temp/root-level-docs/

# Legacy/superseded architecture (decide: keep or delete?)
mv MICROSERVICES_ARCHITECTURE.md _temp/root-level-docs/
mv TECHNICAL_SPECIFICATIONS.md _temp/root-level-docs/
```

### Phase 3: Move Loose docs/ Files (15 minutes)
```bash
cd docs/

# Create temporary folder for obsolete files
mkdir -p _temp/loose-docs

# PERMANENT DOCS - Keep these, move to proper location
# Architecture decisions
mv ARCHITECTURE_DECISION_GUIDE.md architecture/decisions/architecture-decision-guide.md

# Move to specifications
mv KNOWLEDGE_BASE_SYSTEM.md specifications/knowledge-base-system.md

# Move to development
mv how-to-add-providers.md development/

# Move to infrastructure
mv opik-setup.md infrastructure/

# Move to API
mv SCHEMA_ALIGNMENT.md api/schema-alignment.md

# Move to logging
mv LOGGING_POLICY.md logging/logging-policy.md

# Move to getting-started
mv USER_GUIDE.md getting-started/user-guide.md

# TEMPORARY/OBSOLETE - Move to _temp/ for later cleanup
# Working documents and obsolete content
mv FLAGS_AND_CONFIG.md _temp/loose-docs/  # Likely obsolete (covered in development docs)
mv TECHNICAL_DEBT.md _temp/loose-docs/    # Temporary tracking doc
mv FUTURE_ENHANCEMENTS.md _temp/loose-docs/  # Temporary planning doc
```

### Phase 4: Move Test Docs (10 minutes)
```bash
# Move from tests/ to docs/testing/
mv tests/ARCHITECTURE_TESTING_GUIDE.md docs/testing/architecture-testing-guide.md
mv tests/NEW_TEST_PATTERNS.md docs/testing/new-test-patterns.md
```

### Phase 5: Consolidate Duplicates (15 minutes)
```bash
cd docs/

# Create folder for duplicate/obsolete files
mkdir -p _temp/duplicates

# System Requirements - Keep only authoritative version
# KEEP: system-requirements-specification.md (v2.0, most recent)
# REMOVE: Older versions
mv FAULTMAVEN_SYSTEM_REQUIREMENTS.md _temp/duplicates/
mv faultmaven_system_requirements_v2.md _temp/duplicates/

# Architecture Diagrams - Already consolidated in Phase 2
# Keep: docs/architecture/diagrams/* (moved in Phase 2)
# Any duplicates found should be moved to _temp/duplicates/
```

### Phase 6: Create Index Files (30 minutes)
Create README.md files in each major directory:
- `docs/README.md` - Master documentation index
- `docs/getting-started/README.md`
- `docs/architecture/README.md` (link to architecture-overview.md)
- `docs/architecture/diagrams/README.md`
- `docs/architecture/decisions/README.md`
- `docs/specifications/README.md`
- `docs/releases/README.md`
- `docs/testing/README.md`
- etc.

### Phase 7: Update Cross-References (1-2 hours)
1. Update `architecture-overview.md` links to reflect new paths
2. Update `system-requirements-specification.md` links
3. Update API documentation links
4. Search and replace old paths across all docs

### Phase 8: Update Root README.md (15 minutes)
Update project README to point to new documentation structure:
```markdown
## Documentation

📚 **Complete documentation is in [`docs/`](./docs/)**

Quick Links:
- 🚀 [Getting Started](./docs/getting-started/)
- 🏗️ [Architecture Overview](./docs/architecture/architecture-overview.md)
- 📋 [System Requirements](./docs/specifications/system-requirements-specification.md)
- 🔌 [API Documentation](./docs/api/)
- 💻 [Development Guide](./docs/development/)
- 🧪 [Testing Guide](./docs/testing/)
```

### Phase 9: Review and Clean Up _temp/ (1-2 weeks later)
```bash
# After reorganization has been in use for 1-2 weeks:

# 1. Review files in _temp/ to confirm nothing needed
cd _temp/
ls -la root-level-docs/
ls -la loose-docs/
ls -la duplicates/

# 2. If confident nothing is needed, delete entire _temp/ directory
cd ..
rm -rf _temp/

# 3. Commit the cleanup
git add .
git commit -m "docs: remove obsolete temporary files after reorganization"
```

**Note**: Don't delete `_temp/` immediately! Let it sit for 1-2 weeks to ensure nothing important was accidentally categorized as temporary.

---

## Benefits

### 1. **Clean Project Root** ✅
- Only essential files at root (README, LICENSE, config files)
- Professional appearance for GitHub visitors
- Easy to navigate for new contributors

### 2. **Logical Documentation Hierarchy** ✅
- All docs in `docs/` directory
- Clear categorization (architecture, development, testing, etc.)
- Easy to find related documents

### 3. **Separation of Concerns** ✅
- Source code directories contain ONLY code
- Test directories contain ONLY tests
- Documentation centralized in `docs/`

### 4. **Historical Tracking** ✅
- Release notes organized by phase
- Implementation plans archived
- Clear evolution trail

### 5. **Easier Maintenance** ✅
- One place to look for docs
- Clear naming conventions
- Reduced duplication

### 6. **Better Onboarding** ✅
- New developers find everything in `docs/`
- Getting started guide separate from deep-dive docs
- Progressive disclosure of complexity

---

## Naming Conventions

### Files
- Use **lowercase-with-hyphens** for new files: `system-architecture.md`
- Legacy files in UPPERCASE can stay temporarily but rename over time
- Be descriptive: `authentication-design.md` not `auth.md`

### Directories
- Use **lowercase** for directories: `docs/architecture/`
- Use **plural** where appropriate: `docs/releases/`, `docs/guides/`
- Use **full words**: `infrastructure/` not `infra/`

---

## Validation Checklist

After reorganization, verify:

- [ ] Project root has minimal files (README, LICENSE, configs only)
- [ ] All docs are in `docs/` directory
- [ ] No docs in `faultmaven/` code directory
- [ ] No docs in `tests/` directory
- [ ] Each major directory has a README.md index
- [ ] All cross-references updated
- [ ] No broken links in documentation
- [ ] Duplicate files archived or deleted
- [ ] `architecture-overview.md` links work correctly
- [ ] CI/CD documentation paths updated (if any)

---

## Estimated Time

- **Phase 1** (Create structure): 15 minutes
- **Phase 2** (Move root files): 20 minutes
- **Phase 3** (Move loose docs): 15 minutes
- **Phase 4** (Move test docs): 10 minutes
- **Phase 5** (Consolidate duplicates): 15 minutes
- **Phase 6** (Create indexes): 30 minutes
- **Phase 7** (Update references): 1-2 hours
- **Phase 8** (Root README): 15 minutes
- **Phase 9** (_temp/ cleanup): 30 minutes (done 1-2 weeks later)

**Total**: 2.5-3.5 hours (initial reorganization)  
**Cleanup**: 30 minutes (after 1-2 week review period)

---

## Rollback Plan

1. Keep git history intact (use `git mv` instead of `mv`)
2. Create a branch for reorganization
3. Test all links before merging
4. If issues found, can revert commit

---

## Next Steps

1. **Review this plan** - Approve or modify
2. **Create branch**: `git checkout -b docs-reorganization`
3. **Execute phases 1-8** in order
4. **Test thoroughly** - Verify all links work
5. **Create PR** - Review changes
6. **Merge** - Apply to main/master

---

**Status**: PROPOSAL - Ready for execution  
**Impact**: LOW (documentation only, no code changes)  
**Risk**: LOW (can revert via git)  
**Benefit**: HIGH (much cleaner project structure)

---

## Before & After Comparison

### BEFORE (Current State)
```
FaultMaven/
├── README.md
├── LICENSE
├── architecture-diagram.md               # ❌ 10+ docs at root
├── CLAUDE.md
├── PHASE_0_AUDIT_REPORT.md
├── IMPLEMENTATION_COMPLETE.md
├── MICROSERVICES_ARCHITECTURE.md
├── ... (7 more root-level .md files)
├── faultmaven/
│   ├── ARCHITECTURE_DIAGRAM.md           # ❌ Docs in code directory
│   └── ... (source code)
├── tests/
│   ├── ARCHITECTURE_TESTING_GUIDE.md     # ❌ Docs in test directory
│   └── ... (test code)
└── docs/
    ├── ARCHITECTURE_DECISION_GUIDE.md    # ❌ 10+ loose files in docs/
    ├── FLAGS_AND_CONFIG.md
    ├── FUTURE_ENHANCEMENTS.md
    ├── ... (10+ more loose files)
    ├── architecture/ ✅
    ├── specifications/ ✅
    └── ... (organized subdirectories)
```

### AFTER (Proposed State)
```
FaultMaven/
├── README.md                             # ✅ Only essential files at root
├── LICENSE
├── _temp/                                # 🗑️ Temporary (delete after 1-2 weeks)
│   ├── root-level-docs/                  # 10 obsolete files
│   ├── loose-docs/                       # 3 obsolete files
│   └── duplicates/                       # 2 duplicate files
├── faultmaven/                           # ✅ Source code ONLY
│   └── ... (no documentation)
├── tests/                                # ✅ Test code ONLY
│   └── ... (no documentation)
└── docs/                                 # ✅ ALL documentation here
    ├── README.md                         # 🆕 Master index
    ├── getting-started/                  # 🆕 User onboarding
    │   └── user-guide.md
    ├── architecture/                     # ✅ Enhanced structure
    │   ├── architecture-overview.md      # 🎯 Master document
    │   ├── diagrams/                     # 🆕 All diagrams centralized
    │   ├── decisions/                    # 🆕 ADRs
    │   └── legacy/                       # 🆕 Superseded docs
    ├── specifications/                   # ✅ Organized
    │   ├── system-requirements-specification.md  # 🎯 Authoritative (v2.0)
    │   └── knowledge-base-system.md      # Moved from docs/
    ├── api/                              # ✅ Organized
    │   └── schema-alignment.md           # Moved from docs/
    ├── development/                      # ✅ Organized
    │   └── how-to-add-providers.md       # Moved from docs/
    ├── infrastructure/                   # ✅ Organized
    │   └── opik-setup.md                 # Moved from docs/
    ├── logging/                          # ✅ Organized
    │   └── logging-policy.md             # Moved from docs/
    ├── testing/                          # ✅ Organized
    │   ├── architecture-testing-guide.md # Moved from tests/
    │   └── new-test-patterns.md          # Moved from tests/
    └── ... (all other organized subdirectories)
```

**Result**:
- ✅ Clean project root (2 files: README + LICENSE)
- ✅ Code directories contain ONLY code
- ✅ All documentation in `docs/` with clear hierarchy
- ✅ Temporary files in `_temp/` for easy review and deletion

---

**End of Reorganization Plan**

