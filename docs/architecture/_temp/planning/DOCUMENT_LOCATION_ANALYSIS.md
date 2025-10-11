# Document Location Analysis

**Date**: 2025-10-11  
**Purpose**: Find optimal locations for misplaced documents

---

## Documents to Relocate

### 1. CASE_PERSISTENCE_SYSTEM_DESIGN.md

**Current Location**: `docs/technical-design/CASE_PERSISTENCE_SYSTEM_DESIGN.md`

**Content Analysis**:
- Technical design for case persistence
- Redis implementation architecture
- Service layer integration (CaseService, SessionService, AgentService)
- Data models, API endpoints, testing strategy
- **1777 lines** of comprehensive technical architecture

**Content Type**: Architecture Design Document

**Recommended Location**: `docs/architecture/reference/case-persistence-system-design.md`

**Reasoning**:
- ✅ It's an architecture design (belongs in architecture/)
- ✅ Very detailed technical spec (good fit for reference/)
- ✅ Not currently referenced in architecture-overview.md (unreferenced)
- ✅ Valuable reference material for case persistence implementation
- ✅ Follows naming convention after move (lowercase-hyphen)

**Alternative**: `docs/architecture/case-persistence-system-design.md` (if should be promoted to referenced doc)

---

### 2. TECHNICAL_DEBT_CLEANUP_SPEC.md

**Current Location**: `docs/specifications/TECHNICAL_DEBT_CLEANUP_SPEC.md`

**Content Analysis**:
- **Status**: COMPLETED (September 16, 2025)
- Specification for cleaning up legacy code and feature flags
- All items marked as completed ✅
- Historical documentation of cleanup work
- **759 lines** of completed specification

**Content Type**: Completed Specification (Historical)

**Recommended Location**: `docs/architecture/_temp/working-docs/technical-debt-cleanup-spec.md.old`

**Reasoning**:
- ✅ Work is COMPLETED - no longer active specification
- ✅ Historical value only (shows what was cleaned up)
- ✅ Should be in _temp/ for review/deletion
- ✅ Not needed for ongoing work
- ✅ Can be deleted after review period

**Alternative**: `docs/releases/` (if want to keep as historical record of completed work)

---

### 3. knowledge-base-system.md

**Current Location**: `docs/specifications/knowledge-base-system.md`

**Content Analysis**:
- **Comprehensive user guide** for knowledge base system
- Contribution workflow for community contributors
- Review process and quality standards
- Tools & commands reference
- FAQs for contributors
- **1566 lines** of user/contributor documentation

**Content Type**: User/Contributor Guide (NOT a specification)

**Recommended Location**: `docs/guides/knowledge-base-system.md`

**Reasoning**:
- ✅ It's a guide for users and contributors (not a technical spec)
- ✅ Contains FAQs, tutorials, workflows (guide characteristics)
- ✅ Audience is contributors, not architects (user-facing)
- ✅ Better fits with other guides
- ❌ NOT a specification (doesn't define WHAT system must do)
- ❌ NOT architecture (doesn't define HOW system is built)

**Alternative**: Keep in `docs/specifications/` and rename to `knowledge-base-specification.md` if it's meant to be authoritative spec

---

## Summary of Recommendations

| Document | Current | Recommended | Reasoning |
|----------|---------|-------------|-----------|
| **CASE_PERSISTENCE_SYSTEM_DESIGN.md** | technical-design/ | `architecture/reference/` | Architecture design, unreferenced |
| **TECHNICAL_DEBT_CLEANUP_SPEC.md** | specifications/ | `architecture/_temp/` | Completed work, historical only |
| **knowledge-base-system.md** | specifications/ | `guides/` | User guide, not specification |

---

## Proposed Actions

### Action 1: Move Case Persistence Design
```bash
cd /home/swhouse/projects/FaultMaven

# Move to architecture reference
mv docs/technical-design/CASE_PERSISTENCE_SYSTEM_DESIGN.md \
   docs/architecture/reference/case-persistence-system-design.md

# Check if technical-design/ is now empty
ls docs/technical-design/
# If empty, remove directory
rmdir docs/technical-design/
```

### Action 2: Archive Technical Debt Spec
```bash
# Move to _temp/ (completed work)
mv docs/specifications/TECHNICAL_DEBT_CLEANUP_SPEC.md \
   docs/architecture/_temp/working-docs/technical-debt-cleanup-spec.md.old
```

### Action 3: Move Knowledge Base to Guides
```bash
# Move to guides directory
mv docs/specifications/knowledge-base-system.md \
   docs/guides/knowledge-base-system.md
```

---

## Directory Structure After Moves

```
docs/
├── architecture/
│   ├── reference/
│   │   ├── case-persistence-system-design.md  ⬅️ MOVED from technical-design/
│   │   └── ... (other reference docs)
│   └── _temp/
│       └── working-docs/
│           ├── technical-debt-cleanup-spec.md.old  ⬅️ MOVED from specifications/
│           └── ... (other temp docs)
│
├── guides/
│   ├── knowledge-base-system.md  ⬅️ MOVED from specifications/
│   └── ... (other guides)
│
├── specifications/  (cleaner now!)
│   ├── system-requirements-specification.md
│   ├── SESSION_MANAGEMENT_SPEC.md
│   ├── CONFIGURATION_MANAGEMENT_SPEC.md
│   ├── INTERFACE_DOCUMENTATION_SPEC.md
│   └── ERROR_CONTEXT_ENHANCEMENT_SPEC.md
│
└── technical-design/  (check if empty, remove if so)
```

---

## Benefits

### ✅ Better Categorization
- Architecture designs in architecture/
- User guides in guides/
- Completed work in _temp/
- Specifications remain true specs

### ✅ Cleaner specifications/ Directory
- Only active specifications
- Clear purpose (WHAT system must do)
- No user guides mixed in

### ✅ More Discoverable
- Guides in guides/ (obvious)
- Reference material in architecture/reference/
- Completed work isolated in _temp/

---

**Should I execute these moves?**

1. Case Persistence Design → architecture/reference/
2. Technical Debt Cleanup → architecture/_temp/
3. Knowledge Base System → guides/

