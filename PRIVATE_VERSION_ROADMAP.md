# FaultMaven PRIVATE Enterprise Version - Roadmap

**Status**: PUBLIC v1.0.0 Complete ✅ | PRIVATE Refactoring Pending ⏳
**Updated**: November 16, 2025

---

## Overview

Now that the PUBLIC open-source foundation is complete (6 microservices cleansed and ready for Docker Hub), the PRIVATE enterprise version needs to be refactored to follow the **Enterprise Superset** model.

## Current State

### ✅ PUBLIC Version (Complete)
- 6 microservices cleansed and tagged v1.0.0
- Docker Compose deployment package ready
- All using SQLite, single-user model
- Ready for GitHub + Docker Hub publication

### ⏳ PRIVATE Version (Needs Refactoring)
- Still using monolithic code with microservices extraction in progress
- Has enterprise features (organizations, teams, PostgreSQL)
- Needs to be refactored to consume PUBLIC Docker images

---

## Enterprise Superset Architecture

```
┌──────────────────────────────────────────────────────────┐
│              PRIVATE Enterprise Deployment                │
│                                                           │
│  ┌────────────────────────────────────────────────────┐  │
│  │         Proprietary Services (Source Code)          │  │
│  ├────────────────────────────────────────────────────┤  │
│  │  • fm-agent-service (LangGraph AI agent)           │  │
│  │  • fm-investigation-service (Workflow engine)      │  │
│  │  • fm-analytics-service (Advanced metrics)         │  │
│  │  • fm-notification-service (Alerts & webhooks)     │  │
│  └────────────────────────────────────────────────────┘  │
│                           ↓                               │
│  ┌────────────────────────────────────────────────────┐  │
│  │    Enterprise Extensions (Overlay Services)         │  │
│  ├────────────────────────────────────────────────────┤  │
│  │  • Supabase Authentication (instead of fm-auth)    │  │
│  │  • PostgreSQL (instead of SQLite)                  │  │
│  │  • Organization/Team management                    │  │
│  │  • RBAC & permissions                              │  │
│  │  • Multi-tenancy extensions                        │  │
│  │  • S3/MinIO for file storage                       │  │
│  └────────────────────────────────────────────────────┘  │
│                           ↓                               │
│  ┌────────────────────────────────────────────────────┐  │
│  │      PUBLIC Foundation (Docker Images)              │  │
│  ├────────────────────────────────────────────────────┤  │
│  │  FROM: faultmaven/fm-auth-service:latest           │  │
│  │  FROM: faultmaven/fm-session-service:latest        │  │
│  │  FROM: faultmaven/fm-case-service:latest           │  │
│  │  FROM: faultmaven/fm-knowledge-service:latest      │  │
│  │  FROM: faultmaven/fm-evidence-service:latest       │  │
│  │  FROM: faultmaven/fm-api-gateway:latest            │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

---

## Remaining Tasks for PRIVATE Version

### Phase 1: Refactor Existing PRIVATE Microservices ⏳

The 6 PUBLIC microservices currently exist in `/home/swhouse/projects/` as PRIVATE versions with organization/team features. These need refactoring:

#### Task 1.1: Update fm-auth-service (PRIVATE)
**Location**: `/home/swhouse/projects/fm-auth-service/`

**Current State**: PostgreSQL, organization/team support
**Target State**: Use PUBLIC Docker image as base, add Supabase integration

**Actions**:
- [ ] Create Dockerfile extending PUBLIC image:
  ```dockerfile
  FROM faultmaven/fm-auth-service:latest
  # Add Supabase client libraries
  # Add organization/team models
  # Add RBAC middleware
  ```
- [ ] Add Supabase authentication provider
- [ ] Add organization/team management endpoints
- [ ] Keep PostgreSQL for enterprise metadata
- [ ] Add multi-tenancy support

#### Task 1.2: Update fm-session-service (PRIVATE)
**Location**: `/home/swhouse/projects/fm-session-service/`

**Current State**: Basic Redis session management
**Target State**: Use PUBLIC image, add organization context

**Actions**:
- [ ] Extend PUBLIC Docker image
- [ ] Add organization_id to session context
- [ ] Add team_id support for shared sessions
- [ ] Add session analytics/tracking

#### Task 1.3: Update fm-case-service (PRIVATE)
**Location**: `/home/swhouse/projects/fm-case-service/`

**Current State**: SQLite, single-user
**Target State**: Use PUBLIC image, add PostgreSQL + multi-tenancy

**Actions**:
- [ ] Extend PUBLIC Docker image
- [ ] Add PostgreSQL database configuration
- [ ] Add organization_id, team_id to models
- [ ] Add case sharing/collaboration features
- [ ] Add advanced case analytics

#### Task 1.4: Update fm-knowledge-service (PRIVATE)
**Location**: `/home/swhouse/projects/fm-knowledge-service/`

**Current State**: ChromaDB, SQLite
**Target State**: Use PUBLIC image, add organization isolation

**Actions**:
- [ ] Extend PUBLIC Docker image
- [ ] Add organization_id filtering to ChromaDB queries
- [ ] Add PostgreSQL for enterprise document metadata
- [ ] Add document sharing across teams
- [ ] Add S3/MinIO for document storage

#### Task 1.5: Update fm-evidence-service (PRIVATE)
**Location**: `/home/swhouse/projects/fm-evidence-service/`

**Current State**: Local filesystem storage
**Target State**: Use PUBLIC image, add S3 + multi-tenancy

**Actions**:
- [ ] Extend PUBLIC Docker image
- [ ] Add S3/MinIO storage backend
- [ ] Add organization_id, team_id to evidence
- [ ] Add evidence sharing policies
- [ ] Add advanced search/filtering

#### Task 1.6: Update fm-api-gateway (PRIVATE)
**Location**: `/home/swhouse/projects/fm-api-gateway/`

**Current State**: Pluggable auth (fm-auth or Supabase)
**Target State**: Use PUBLIC image, configure for Supabase

**Actions**:
- [ ] Extend PUBLIC Docker image
- [ ] Configure Supabase as default auth provider
- [ ] Add organization/team headers (X-Org-ID, X-Team-ID)
- [ ] Add RBAC enforcement
- [ ] Add rate limiting per organization

---

### Phase 2: Complete PRIVATE-Only Services ⏳

These services exist only in PRIVATE and need completion:

#### Task 2.1: Complete fm-agent-service
**Location**: `/home/swhouse/projects/fm-agent-service/`

**Status**: Partially implemented
**Purpose**: LangGraph AI agent for autonomous troubleshooting

**Actions**:
- [ ] Complete 7-component agent architecture
- [ ] Add organization-specific knowledge bases
- [ ] Add team collaboration features
- [ ] Add investigation workflow integration
- [ ] Add advanced reasoning capabilities

#### Task 2.2: Complete fm-investigation-service
**Location**: `/home/swhouse/projects/fm-investigation-service/`

**Status**: Partially implemented
**Purpose**: Workflow engine for multi-step investigations

**Actions**:
- [ ] Complete investigation state machine
- [ ] Add hypothesis tracking
- [ ] Add solution validation
- [ ] Add investigation templates
- [ ] Add collaboration features

#### Task 2.3: Create fm-analytics-service (Optional)
**Location**: New service needed

**Purpose**: Advanced analytics and reporting for enterprises

**Actions**:
- [ ] Design analytics data model
- [ ] Implement metrics collection
- [ ] Add dashboards/reporting
- [ ] Add trend analysis
- [ ] Add cost tracking

#### Task 2.4: Create fm-notification-service (Optional)
**Location**: New service needed

**Purpose**: Alerts, webhooks, and integrations

**Actions**:
- [ ] Design notification system
- [ ] Add Slack/Teams integrations
- [ ] Add email notifications
- [ ] Add webhooks for external systems
- [ ] Add notification preferences

---

### Phase 3: Create PRIVATE Deployment Package ⏳

Similar to `faultmaven-deploy`, create enterprise deployment.

#### Task 3.1: Create docker-compose.private.yml

**Actions**:
- [ ] Base services on PUBLIC Docker images
- [ ] Add PostgreSQL database
- [ ] Add Supabase (or configure external)
- [ ] Add S3/MinIO for storage
- [ ] Add proprietary services (agent, investigation)
- [ ] Add monitoring (Prometheus, Grafana)
- [ ] Add logging (ELK or Loki)

**Example Structure**:
```yaml
version: '3.8'

services:
  # PUBLIC Base Services (from Docker Hub)
  auth-service:
    image: faultmaven/fm-auth-service:latest
    # Override with enterprise config

  session-service:
    image: faultmaven/fm-session-service:latest

  case-service:
    image: faultmaven/fm-case-service:latest

  knowledge-service:
    image: faultmaven/fm-knowledge-service:latest

  evidence-service:
    image: faultmaven/fm-evidence-service:latest

  api-gateway:
    image: faultmaven/fm-api-gateway:latest
    environment:
      - AUTH_PROVIDER=supabase

  # PRIVATE Enterprise Services (from private registry)
  agent-service:
    image: registry.faultmaven.com/fm-agent-service:latest

  investigation-service:
    image: registry.faultmaven.com/fm-investigation-service:latest

  # Enterprise Infrastructure
  postgres:
    image: postgres:15

  minio:
    image: minio/minio:latest

  supabase:
    # Supabase stack
```

#### Task 3.2: Create PRIVATE deployment documentation

**Actions**:
- [ ] Create PRIVATE_DEPLOYMENT.md
- [ ] Document environment variables
- [ ] Document enterprise features
- [ ] Document migration from PUBLIC
- [ ] Document backup/restore procedures

---

### Phase 4: Implement One-Way Sync Workflow ⏳

#### Task 4.1: Setup CI/CD for PUBLIC Repos

**Actions**:
- [ ] Create GitHub Actions workflows for each PUBLIC repo
- [ ] Build Docker images on tag push
- [ ] Push to Docker Hub (faultmaven/*)
- [ ] Run tests before publishing
- [ ] Create GitHub releases automatically

**Example Workflow** (`.github/workflows/docker-publish.yml`):
```yaml
name: Build and Push Docker Image

on:
  push:
    tags:
      - 'v*'

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v2

      - name: Login to Docker Hub
        uses: docker/login-action@v2
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      - name: Extract version
        id: meta
        run: echo "VERSION=${GITHUB_REF#refs/tags/v}" >> $GITHUB_OUTPUT

      - name: Build and push
        uses: docker/build-push-action@v4
        with:
          context: .
          push: true
          tags: |
            faultmaven/fm-case-service:${{ steps.meta.outputs.VERSION }}
            faultmaven/fm-case-service:latest
```

#### Task 4.2: Configure PRIVATE to Pull PUBLIC Images

**Actions**:
- [ ] Update PRIVATE docker-compose to use `faultmaven/*` images
- [ ] Add image update checks
- [ ] Add version pinning for stability
- [ ] Document upgrade procedures

---

### Phase 5: Testing & Validation ⏳

#### Task 5.1: Test PUBLIC Deployment End-to-End

**Actions**:
- [ ] Deploy using docker-compose from scratch
- [ ] Test user registration/login
- [ ] Test case creation workflow
- [ ] Test document upload/search
- [ ] Test evidence file upload
- [ ] Verify data persistence across restarts

#### Task 5.2: Test PRIVATE Deployment End-to-End

**Actions**:
- [ ] Deploy PRIVATE stack from scratch
- [ ] Test Supabase authentication
- [ ] Test organization/team creation
- [ ] Test multi-tenancy isolation
- [ ] Test agent service integration
- [ ] Test investigation workflows
- [ ] Verify enterprise features

#### Task 5.3: Test PUBLIC→PRIVATE Sync

**Actions**:
- [ ] Make changes to PUBLIC repo
- [ ] Trigger CI/CD pipeline
- [ ] Verify Docker Hub image updates
- [ ] Pull updated images in PRIVATE deployment
- [ ] Verify compatibility
- [ ] Test rollback procedures

---

## Priority Ranking

### **High Priority** (Must Have for PRIVATE v1.0)
1. ✅ Complete PUBLIC microservices (DONE)
2. ⏳ Refactor PRIVATE microservices to extend PUBLIC images (Phase 1)
3. ⏳ Complete fm-agent-service (Task 2.1)
4. ⏳ Create PRIVATE docker-compose deployment (Task 3.1)
5. ⏳ Setup CI/CD for PUBLIC→Docker Hub (Task 4.1)

### **Medium Priority** (Nice to Have)
6. ⏳ Complete fm-investigation-service (Task 2.2)
7. ⏳ Add Supabase integration to gateway (Task 1.6)
8. ⏳ Add S3/MinIO to evidence service (Task 1.5)

### **Low Priority** (Future Versions)
9. ⏳ Create fm-analytics-service (Task 2.3)
10. ⏳ Create fm-notification-service (Task 2.4)

---

## Timeline Estimate

### Week 1-2: Phase 1 - Refactor PRIVATE Microservices
- Extend PUBLIC Docker images
- Add enterprise overlays
- Test individual services

### Week 3: Phase 2 - Complete Agent Service
- Finish LangGraph implementation
- Add enterprise integrations
- Test agent workflows

### Week 4: Phase 3 - PRIVATE Deployment Package
- Create docker-compose.private.yml
- Add enterprise infrastructure (PostgreSQL, MinIO, Supabase)
- Document deployment

### Week 5: Phase 4 - CI/CD Pipeline
- Setup GitHub Actions
- Configure Docker Hub publishing
- Test automated workflows

### Week 6: Phase 5 - Testing & Validation
- End-to-end testing
- Performance testing
- Security auditing

**Total Estimated Time**: 6 weeks for PRIVATE v1.0

---

## Success Criteria

### PUBLIC Success (✅ Achieved)
- [x] 6 microservices cleansed
- [x] Apache 2.0 licensed
- [x] SQLite, single-user
- [x] Docker Compose deployment
- [x] Ready for GitHub/Docker Hub

### PRIVATE Success (⏳ Pending)
- [ ] PRIVATE extends PUBLIC Docker images
- [ ] Supabase authentication working
- [ ] Organization/team multi-tenancy
- [ ] Agent service functional
- [ ] Investigation workflows complete
- [ ] End-to-end PRIVATE deployment works
- [ ] PUBLIC→PRIVATE sync automated

---

## Next Immediate Actions

1. **Choose Starting Point**: Decide which PRIVATE service to refactor first
   - Recommended: Start with **fm-api-gateway** (simplest, foundational)

2. **Setup Development Environment**:
   - Pull PUBLIC Docker images locally
   - Test extending them with Dockerfile inheritance

3. **Create PRIVATE Branch Strategy**:
   - Keep PRIVATE repos separate from PUBLIC
   - Use Docker image extension pattern

4. **Document Decision**:
   - Create PRIVATE architecture doc
   - Define service contracts
   - Plan database migration from SQLite to PostgreSQL

---

## Questions to Resolve

1. **Supabase Hosting**: Self-hosted or Supabase Cloud?
2. **Database Migration**: How to handle existing PRIVATE data?
3. **Service Discovery**: Kubernetes vs Docker Compose for PRIVATE?
4. **Registry**: Use Docker Hub or private registry for PRIVATE images?
5. **Licensing**: How to distribute PRIVATE version (SaaS only vs license)?

---

## Resources

- PUBLIC Repos: `/home/swhouse/projects/faultmaven-public-repos/`
- PRIVATE Repos: `/home/swhouse/projects/fm-*-service/`
- PUBLIC Design: `/home/swhouse/projects/FaultMaven/PUBLIC_OPENSOURCE_DESIGN.md`
- This Roadmap: `/home/swhouse/projects/FaultMaven/PRIVATE_VERSION_ROADMAP.md`

---

**Status**: Ready to begin PRIVATE refactoring once PUBLIC is published! 🚀
