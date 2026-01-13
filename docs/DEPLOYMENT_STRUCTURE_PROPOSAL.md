# FaultMaven Documentation Structure Proposal

## Overview

This document proposes a documentation structure that clearly separates:
1. **FaultMaven Core** - Design and implementation (the application code)
2. **Local Deployment** - User-controlled deployment (covered in this repository)
3. **Cloud Deployment** - Managed SaaS (mentioned, with links to where users can learn more)

This structure aligns with the [Deployment Agnostic Architecture](../faultmaven-doc-internal/architecture/deployment-agnostic-architecture.md) document.

---

## Proposed Structure

```
docs/
├── README.md                          # Master index (updated)
│
├── core/                              # NEW: FaultMaven Core documentation
│   ├── README.md                      # Core overview and navigation
│   ├── architecture/                  # Move from docs/architecture/
│   │   ├── README.md                  # Architecture index
│   │   ├── deployment-agnostic-design.md  # Link to internal doc (or summary)
│   │   ├── module-organization-design.md
│   │   ├── dependency-injection-system.md
│   │   └── ... (all existing architecture docs)
│   ├── api/                           # Move from docs/api/
│   │   └── ...
│   ├── development/                   # Move from docs/development/
│   │   └── ...
│   └── contributing/                  # Core contribution guidelines
│       └── ...
│
├── deployment/                        # NEW: Deployment documentation
│   ├── README.md                      # Deployment overview
│   │                                  # Explains: Local vs Cloud, what this repo covers
│   ├── local/                         # Local Deployment (user-controlled)
│   │   ├── README.md                  # Local deployment overview
│   │   ├── installation/              # Move from docs/installation/
│   │   │   └── INSTALLATION_GUIDE.md
│   │   ├── configuration/             # Local deployment configuration
│   │   │   ├── environment-variables.md
│   │   │   ├── provider-selection.md  # TENANT_PROVIDER, STORAGE_BACKEND, etc.
│   │   │   └── defaults.md            # SQLite, filesystem, in-memory defaults
│   │   ├── docker/                    # Docker deployment
│   │   │   ├── docker-compose.md
│   │   │   └── Dockerfile.md
│   │   ├── infrastructure/            # Local infrastructure setup
│   │   │   ├── database.md            # SQLite (default), optional PostgreSQL
│   │   │   ├── storage.md             # Filesystem (default), optional S3
│   │   │   ├── vector-store.md        # ChromaDB (default), optional Pinecone
│   │   │   └── cache.md               # In-memory (default), optional Redis
│   │   └── troubleshooting/           # Local deployment issues
│   │       └── ...
│   │
│   └── cloud/                         # Cloud Deployment (SaaS)
│       ├── README.md                  # Cloud deployment overview
│       │                               # Explains: Managed SaaS, subscription-based
│       │                               # Links to: faultmaven.ai/pricing, signup
│       └── overview.md                # What Cloud Deployment offers
│                                       # (no implementation details - that's private)
│
├── getting-started/                   # Keep (user onboarding)
│   └── ...
│
├── how-to/                            # Keep (operational guides)
│   └── ...
│
├── infrastructure/                    # Keep (infrastructure setup guides)
│   └── ...
│
├── testing/                           # Keep
│   └── ...
│
├── security/                          # Keep
│   └── ...
│
├── logging/                           # Keep
│   └── ...
│
└── runbooks/                         # Keep
    └── ...
```

---

## Key Documents to Create/Update

### 1. `docs/core/README.md` (NEW)

**Purpose**: Entry point for FaultMaven Core documentation

**Content**:
- What is FaultMaven Core?
- Core components (Case, Knowledge, Evidence, Agent, Session)
- Architecture overview
- Links to:
  - Architecture docs
  - API documentation
  - Development guides
  - Contributing guidelines

**Key Message**: "FaultMaven Core is the application code shared by all deployments. It's deployment-agnostic—the same code runs in Local and Cloud deployments."

### 2. `docs/deployment/README.md` (NEW)

**Purpose**: Entry point for deployment documentation

**Content**:
- Deployment-agnostic architecture overview
- Local Deployment vs Cloud Deployment comparison
- What this repository covers (Local Deployment)
- What Cloud Deployment offers (managed SaaS)
- Links to:
  - Local deployment guides
  - Cloud deployment overview
  - Deployment-agnostic architecture document (internal)

**Key Message**: "This repository documents Local Deployment (user-controlled). Cloud Deployment (managed SaaS) is available as a subscription service."

### 3. `docs/deployment/local/README.md` (NEW)

**Purpose**: Local Deployment overview

**Content**:
- What is Local Deployment?
- Who controls infrastructure (user)
- Default stack: SQLite, filesystem, in-memory cache, ChromaDB
- Optional upgrades: PostgreSQL, S3, Redis, etc.
- Deployment options: Python process, Docker, Docker Compose, daemon
- Links to installation, configuration, troubleshooting

**Key Message**: "Local Deployment is user-controlled. You choose how to run it and which backends to use."

### 4. `docs/deployment/cloud/README.md` (NEW)

**Purpose**: Cloud Deployment overview

**Content**:
- What is Cloud Deployment?
- Managed SaaS offering
- Features: Multi-user, team collaboration, SSO, case sharing
- Subscription-based
- Links to:
  - faultmaven.ai/pricing
  - faultmaven.ai/signup
  - faultmaven.ai/product (feature comparison)
- Note: Implementation details are private (internal repo)

**Key Message**: "Cloud Deployment is a managed SaaS service. Subscribe to use it—no deployment required."

### 5. Update `docs/README.md`

**Changes**:
- Add sections for Core and Deployment
- Update navigation to reflect new structure
- Maintain backward compatibility with existing links

### 6. Update `README.md` (root)

**Changes**:
- Clarify "Local vs Cloud" section
- Add link to `docs/deployment/` for deployment information
- Update "Architecture" section to link to `docs/core/architecture/`
- Maintain quick start (Local Deployment focus)

---

## Migration Plan

### Phase 1: Create New Structure
1. Create `docs/core/` directory
2. Create `docs/deployment/` directory structure
3. Create new README files

### Phase 2: Move Existing Docs
1. Move `docs/architecture/` → `docs/core/architecture/`
2. Move `docs/api/` → `docs/core/api/`
3. Move `docs/development/` → `docs/core/development/`
4. Move `docs/installation/` → `docs/deployment/local/installation/`
5. Create symlinks or redirects for backward compatibility

### Phase 3: Create New Content
1. Write `docs/core/README.md`
2. Write `docs/deployment/README.md`
3. Write `docs/deployment/local/README.md`
4. Write `docs/deployment/cloud/README.md`
5. Create `docs/deployment/local/configuration/` docs

### Phase 4: Update Existing Docs
1. Update `docs/README.md`
2. Update root `README.md`
3. Update all internal links
4. Add redirects/aliases for old paths

---

## Benefits

1. **Clear Separation**: Core vs Deployment concerns are clearly separated
2. **User Clarity**: Users immediately understand what this repo covers (Core + Local)
3. **Cloud Awareness**: Cloud Deployment is mentioned but clearly marked as SaaS
4. **Maintainability**: Structure aligns with deployment-agnostic architecture
5. **Discoverability**: Easy to find Core docs vs Deployment docs

---

## Questions to Consider

1. **Backward Compatibility**: Should we maintain symlinks/redirects for old paths?
2. **Internal Doc Link**: Should we link to the internal deployment-agnostic-architecture.md, or create a public summary?
3. **Cloud Details**: How much detail about Cloud Deployment should be in the public repo? (Probably just overview + links)
4. **Migration Timeline**: Should this be done incrementally or all at once?

---

## Next Steps

1. Review this proposal
2. Decide on migration approach (incremental vs all-at-once)
3. Create new directory structure
4. Write new README files
5. Move existing documentation
6. Update links and references
7. Test navigation and discoverability
