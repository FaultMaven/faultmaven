# FaultMaven Documentation Audit - Executive Summary

**Date**: 2026-01-04
**Audit Scope**: All documentation in `faultmaven` repository (331 markdown files)
**Purpose**: Prepare for open-source launch with user-focused documentation

---

## TL;DR

The `faultmaven` repository contains **too much internal engineering documentation** that confuses users about what FaultMaven is and how to use it.

**Current state**: 331 files, 54% internal architecture docs, 12% ops runbooks
**Target state**: 30-40 files, 100% user-focused documentation

**Key problem**: We're exposing "deployment neutrality" and internal engineering decisions to users who just want to know:
1. How do I install locally?
2. How do I use FaultMaven Cloud?

---

## Key Findings

### 🔴 Critical Issues

1. **Deployment Neutrality Over-Exposed**
   - 1,682-line internal roadmap document in public repo
   - Extensive discussion of infrastructure abstraction (user doesn't care)
   - **Fix**: Move to `faultmaven-doc-internal`

2. **94 Architecture Files in Public Repo**
   - OODA loops, milestone frameworks, dependency injection deep-dives
   - Internal engineering decisions visible to users
   - **Fix**: Keep 1 simplified 3-page overview, move 93 files to internal

3. **Kubernetes/Enterprise Ops Runbooks Public**
   - 40+ operational runbooks for Kubernetes, PostgreSQL, Redis
   - Not relevant to local users (SQLite)
   - **Fix**: Move to `faultmaven-enterprise-infra`

4. **No Clear "FaultMaven Cloud" Alternative**
   - README focuses on local installation only
   - Users should see: "Install locally OR use FaultMaven Cloud"
   - **Fix**: Add prominent cloud signup option

---

### ✅ What's Working Well

1. **Excellent Installation Guide**
   - QUICKSTART.md is perfect (5-minute setup)
   - README has good quick start section
   - Zero dependencies for local install

2. **Strong Development Documentation**
   - Contributing guide, testing standards, code patterns
   - Good foundation for open-source contributors

3. **Comprehensive (but misplaced) Architecture Docs**
   - High-quality internal documentation
   - Just needs to be moved to internal repository

---

## Recommended Actions

### Phase 1: Critical Cleanup (Week 1-2) - P0

**Move 220 files out of public repository**:

| Category | Files | Destination | Rationale |
|----------|-------|-------------|-----------|
| Architecture docs | 94 files | `faultmaven-doc-internal/architecture/` | Internal engineering decisions |
| Logging/Observability | 7 files | `faultmaven-doc-internal/observability/` | Internal implementation |
| Security implementation | 4 files | `faultmaven-doc-internal/security/` | Internal security details |
| Infrastructure internal | 3 files | `faultmaven-doc-internal/infrastructure/` | Redis/Opik internals |
| Planning docs | 3 files | `faultmaven-doc-internal/planning/` | Roadmaps, evolution strategy |
| Database schemas | 3 files | `faultmaven-doc-internal/database/` | Internal schemas |
| Ops runbooks | 40+ files | `faultmaven-enterprise-infra/runbooks/` | Enterprise operations |
| Archive/obsolete | 26 files | `docs/archive/2025/` | Cleanup |

**Simplify public documentation**:

1. **README.md** - Remove:
   - Deployment neutrality discussion
   - Enterprise infrastructure details (Opik, Presidio, Prometheus)
   - Add: Prominent "FaultMaven Cloud" signup option

2. **.env.example** - Reduce from 50+ vars to 10-15:
   - Keep: LLM API keys, basic config
   - Remove: Opik, Presidio, Redis, PostgreSQL enterprise config

3. **Documentation index** - Reorganize:
   - Getting Started (installation, cloud)
   - User Guide (API, features, workflows)
   - Development (contributing, plugins)
   - Architecture (simplified 3-page overview only)

---

### Phase 2: Create User-Facing Docs (Week 3-4) - P1

**New documents needed**:

1. `/docs/architecture/overview.md` (NEW)
   - Simplified architecture (3 pages max)
   - Browser → API → Agent → Knowledge Base
   - Technology stack overview
   - NO: Dependency injection details, OODA loops, milestone frameworks

2. `/docs/user-guide/use-case-examples.md` (NEW)
   - 3-5 real-world troubleshooting scenarios
   - Step-by-step workflows with API calls
   - Show what FaultMaven can do

3. `/docs/plugins/` (NEW)
   - Creating LLM provider plugins
   - Creating storage backend plugins
   - Creating agent tools
   - Enable community contributions

4. `/docs/troubleshooting/common-issues.md` (NEW)
   - Installation errors
   - API connection problems
   - LLM provider errors
   - User-level debugging (NOT Kubernetes/Redis)

5. `/docs/getting-started/cloud-setup.md` (NEW)
   - How to sign up for FaultMaven Cloud
   - Cloud vs local comparison
   - Migration guide (local → cloud)

---

### Phase 3: Polish & Refinement (Week 5-6) - P2

1. Archive obsolete documents
2. Update documentation index
3. Run link checker
4. External beta testing with 2-3 new contributors

---

## Success Metrics

| Metric | Before | Target |
|--------|--------|--------|
| **Total public docs** | 331 files | 30-40 files |
| **Time to first contribution** | Unknown | <30 minutes |
| **"Deployment neutrality" mentions** | 15+ | 0 |
| **Enterprise infra docs in public** | 40+ files | 0 files |
| **Architecture doc pages** | 200+ pages | <10 pages |

---

## User Experience Comparison

### Before (Current State)

**User arrives at GitHub repository**:
- Sees: 331 documentation files
- Finds: "Deployment neutrality", "Provider pattern", "OODA loops", "Milestone-based investigation framework"
- Thinks: "This is too complex, I don't understand what this does"
- Leaves: Confused, doesn't contribute

### After (Target State)

**User arrives at GitHub repository**:
- Sees: Clear choice - "Install locally (5 min)" OR "Use FaultMaven Cloud"
- Finds: Quick start, use case examples, simple architecture overview
- Thinks: "I understand what this does, let me try it"
- Contributes: Sets up locally in <30 minutes, submits first PR

---

## Implementation Timeline

### Week 1-2: Move Internal Documentation
- Create `faultmaven-doc-internal` repository
- Move 180 internal docs
- Move 40 ops docs to `faultmaven-enterprise-infra`
- Update cross-references

### Week 3-4: Simplify & Create
- Simplify README.md, .env.example
- Create 5 new user-facing documents
- Add "FaultMaven Cloud" signup links
- Create simplified architecture overview

### Week 5-6: Polish
- Archive obsolete docs
- Update documentation index
- Link checking
- Beta testing with external contributors

**Total effort**: 6 weeks
**Launch-ready**: End of Week 6

---

## Risks & Mitigation

### Risk 1: Broken Links

**Mitigation**:
- Automated link checker before/after moves
- Create redirect READMEs in old locations
- Test all links in CI/CD

### Risk 2: Loss of Context

**Mitigation**:
- Don't delete - move to internal/archive
- Maintain git history (use `git mv`)
- Document move rationale in commits

### Risk 3: User Confusion During Transition

**Mitigation**:
- Add deprecation notices before removing
- Announce changes in CHANGELOG
- 30-day notice for any public doc removal

---

## Decision Required

**Approve this audit and action plan?**

- [ ] YES - Proceed with Phase 1 (move internal docs)
- [ ] NO - Revise approach (provide feedback)
- [ ] DEFER - Need more information (specify)

**Questions to address**:
1. Should we create `faultmaven-doc-internal` as separate repo or subdirectory?
2. Should we remove docs immediately or deprecate for 30 days first?
3. Who owns creating the 5 new user-facing documents?
4. What's the target date for open-source launch?

---

## Next Steps (if approved)

1. **Engineering Lead**: Assign team for documentation reorganization (2 people, 2 weeks)
2. **Tech Writer**: Create 5 new user-facing documents (Week 3-4)
3. **Product Manager**: Draft "FaultMaven Cloud" signup page copy
4. **DevOps**: Set up `faultmaven-enterprise-infra` repository structure
5. **Community Manager**: Plan open-source launch announcement

---

**Full Audit Report**: `/home/swhouse/product/faultmaven/docs/working/AUDIT-documentation-public-repo-strategy.md`

**Contact**: Tech Writing Team
**Review Meeting**: [Schedule here]
