# CI/CD and Dashboard Fixes - Implementation Guide

**Date:** 2025-11-17
**Status:** Solutions Ready for Deployment

---

## Issues Identified & Resolved

This document provides complete solutions for the 3 critical issues:

1. ✅ **5 repositories missing CI/CD workflows** → Docker publishing automation
2. ✅ **faultmaven-dashboard needs CI/CD urgently** → Blocks docker-compose deployment
3. ✅ **Dashboard missing 2 pages** → Case list and settings pages

---

## Solution 1: Backend Services CI/CD Workflow

### Issue
5 backend microservices need GitHub Actions workflows to automatically build and publish Docker images to Docker Hub.

**Affected Repositories:**
- `fm-api-gateway`
- `fm-auth-service`
- `fm-case-service`
- `fm-session-service`
- `fm-knowledge-service`
- `fm-evidence-service` (6th service)

### Solution Created

**File:** `docs/cicd-templates/backend-service-docker.yml`

**Features:**
- ✅ Builds on push to main and on version tags (`v*`)
- ✅ Multi-architecture support (amd64, arm64)
- ✅ Automatic version tagging (semver)
- ✅ Docker layer caching for faster builds
- ✅ Updates Docker Hub description from README
- ✅ Skip build on pull requests (test only)

### Implementation Instructions

For EACH of the 6 backend service repositories:

```bash
# 1. Navigate to repository
cd <repository-name>

# 2. Create workflow directory
mkdir -p .github/workflows

# 3. Copy template
cp /path/to/FaultMaven/docs/cicd-templates/backend-service-docker.yml .github/workflows/docker-publish.yml

# 4. Add Docker Hub secrets to GitHub
# Go to: Settings → Secrets and variables → Actions
# Add:
#   - DOCKERHUB_USERNAME
#   - DOCKERHUB_TOKEN
```

### Testing

```bash
# Test workflow locally (optional)
act push --secret DOCKERHUB_USERNAME=<user> --secret DOCKERHUB_TOKEN=<token>

# Or push to trigger:
git add .github/workflows/docker-publish.yml
git commit -m "Add CI/CD workflow for Docker publishing"
git push origin main

# Watch the action run in GitHub Actions tab
```

### Expected Result

**On push to main:**
```
Image published to: faultmaven/fm-case-service:latest
Image published to: faultmaven/fm-case-service:main-abc1234
```

**On version tag (v1.0.0):**
```
Image published to: faultmaven/fm-case-service:latest
Image published to: faultmaven/fm-case-service:1.0.0
Image published to: faultmaven/fm-case-service:1.0
Image published to: faultmaven/fm-case-service:1
```

---

## Solution 2: Dashboard CI/CD Workflow

### Issue
`faultmaven-dashboard` repository blocks docker-compose deployment because:
- No CI/CD workflow exists
- No Docker image published to Docker Hub
- `docker-compose.yml` references `faultmaven/faultmaven-dashboard:latest` (doesn't exist)

**Impact:** 🔴 **CRITICAL** - Users cannot deploy complete stack

### Solution Created

**File:** `docs/cicd-templates/dashboard-docker.yml`

**Features:**
- ✅ Builds Vite application before Docker image
- ✅ Runs tests in CI pipeline
- ✅ Multi-architecture support (amd64, arm64)
- ✅ Automatic version tagging
- ✅ Handles Vite environment variables at build time
- ✅ Docker layer caching

### Implementation Instructions

```bash
# 1. Navigate to faultmaven-dashboard repository
cd faultmaven-dashboard

# 2. Create workflow directory
mkdir -p .github/workflows

# 3. Copy dashboard-specific workflow
cp /path/to/FaultMaven/docs/cicd-templates/dashboard-docker.yml .github/workflows/docker-publish.yml

# 4. Verify Dockerfile exists
# Should be a multi-stage build:
#   - Stage 1: Build Vite app (npm run build)
#   - Stage 2: Serve with nginx

# 5. Add Docker Hub secrets (if not already added)
# Settings → Secrets → Add:
#   - DOCKERHUB_USERNAME
#   - DOCKERHUB_TOKEN

# 6. Commit and push
git add .github/workflows/docker-publish.yml
git commit -m "Add CI/CD workflow for dashboard Docker publishing"
git push origin main
```

### Expected Result

**Image published to Docker Hub:**
```
faultmaven/faultmaven-dashboard:latest
faultmaven/faultmaven-dashboard:1.0.0
```

**Verification:**
```bash
# Pull and test
docker pull faultmaven/faultmaven-dashboard:latest
docker run -p 3000:80 faultmaven/faultmaven-dashboard:latest

# Should be accessible at http://localhost:3000
```

### Urgency Timeline

**Why this is urgent:**
1. docker-compose.yml references this image (will fail to pull)
2. Users cannot complete Quick Start without it
3. Blocks all testing and demonstrations

**Estimated Time to Fix:** 30 minutes
1. Copy workflow: 5 minutes
2. Configure secrets: 5 minutes
3. Push and wait for build: 15 minutes
4. Verify image: 5 minutes

---

## Solution 3: Dashboard Missing Pages

### Issue
Dashboard is missing 2 critical pages mentioned in Design v2.0:
- **Case List Page** (read-only view of all cases)
- **Settings Page** (LLM configuration)

**Current State:** Dashboard only has KB upload/search pages

### Solution Created

**Files:**
1. `docs/dashboard-templates/CaseListPage.tsx` - Complete case list component
2. `docs/dashboard-templates/SettingsPage.tsx` - Complete settings component

### Features Implemented

#### Case List Page (`CaseListPage.tsx`)

**Features:**
- ✅ Fetches cases from `/v1/cases` API
- ✅ Displays case cards with status, priority, tags
- ✅ Filtering by status (all, open, in_progress, resolved, closed)
- ✅ Sorting by date/priority
- ✅ Read-only view (with note to use extension for editing)
- ✅ Responsive design
- ✅ Loading states and error handling
- ✅ Refresh button
- ✅ Pretty date formatting (relative time)

**UI Components:**
- Status badges (color-coded)
- Priority badges (critical, high, medium, low)
- Tag display
- View Details button (links to API endpoint)
- Empty state message

#### Settings Page (`SettingsPage.tsx`)

**Features:**
- ✅ API endpoint configuration with test button
- ✅ LLM provider selection (OpenAI, Anthropic, Fireworks)
- ✅ Model selection (per provider)
- ✅ Temperature slider (0.0 - 1.0)
- ✅ Max tokens input
- ✅ Dark mode toggle (placeholder)
- ✅ Save to localStorage
- ✅ Sync with backend (optional)
- ✅ Success/error messages

**Sections:**
1. API Configuration
   - URL input
   - Test connection button
   - Examples for self-hosted vs enterprise
2. LLM Configuration
   - Provider dropdown
   - Model dropdown (dynamically filtered)
   - Temperature slider with labels
   - Max tokens input
   - API key info box
3. Display Preferences
   - Dark mode toggle (with "coming soon" note)

### Implementation Instructions

```bash
# 1. Navigate to faultmaven-dashboard repository
cd faultmaven-dashboard

# 2. Copy page templates
cp /path/to/FaultMaven/docs/dashboard-templates/CaseListPage.tsx src/pages/
cp /path/to/FaultMaven/docs/dashboard-templates/SettingsPage.tsx src/pages/

# 3. Update router to include new pages
# Edit: src/App.tsx or src/main.tsx (wherever router is defined)
```

### Router Integration

Add these routes to your React Router configuration:

```typescript
// src/App.tsx or wherever routes are defined
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { KBPage } from './pages/KBPage';
import { CaseListPage } from './pages/CaseListPage';
import { SettingsPage } from './pages/SettingsPage';
import { LoginPage } from './pages/LoginPage';

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/kb" element={<KBPage />} />
        <Route path="/cases" element={<CaseListPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/" element={<Navigate to="/kb" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
```

### Navigation Menu

Add navigation links to your layout:

```typescript
// src/components/Layout.tsx or similar
const navigation = [
  { name: 'Knowledge Base', href: '/kb', icon: BookIcon },
  { name: 'Cases', href: '/cases', icon: FolderIcon },
  { name: 'Settings', href: '/settings', icon: CogIcon },
];
```

### Testing

```bash
# 1. Install dependencies (if needed)
npm install

# 2. Run development server
npm run dev

# 3. Navigate to pages
# http://localhost:5173/cases     - Case list
# http://localhost:5173/settings  - Settings

# 4. Test functionality
# - Case list should fetch and display cases
# - Settings should save to localStorage
# - API connection test should work
```

### Expected Result

**Case List Page:**
- Shows all cases from API
- Can filter by status
- Can sort by date/priority
- Displays status/priority badges
- Shows relative timestamps
- Responsive layout

**Settings Page:**
- Can configure API endpoint
- Can test connection
- Can select LLM provider and model
- Can adjust temperature and max tokens
- Saves settings persistently

---

## Deployment Order (Critical Path)

To unblock docker-compose deployment as quickly as possible:

### Step 1: Dashboard CI/CD (URGENT - 30 minutes)

```bash
# In faultmaven-dashboard repo
cp ../FaultMaven/docs/cicd-templates/dashboard-docker.yml .github/workflows/docker-publish.yml
git add .github/workflows/
git commit -m "Add CI/CD workflow for Docker publishing"
git push origin main

# Wait for build to complete (~15 minutes)
# Verify image exists on Docker Hub
```

### Step 2: Add Missing Pages (HIGH - 1 hour)

```bash
# Still in faultmaven-dashboard repo
cp ../FaultMaven/docs/dashboard-templates/CaseListPage.tsx src/pages/
cp ../FaultMaven/docs/dashboard-templates/SettingsPage.tsx src/pages/

# Update router (see Router Integration section above)
# Commit and push (triggers new build)
```

### Step 3: Backend Services CI/CD (MEDIUM - 2 hours)

For each of 6 backend services:
```bash
cd <service-repo>
cp ../FaultMaven/docs/cicd-templates/backend-service-docker.yml .github/workflows/docker-publish.yml
git add .github/workflows/
git commit -m "Add CI/CD workflow"
git push origin main
```

**Can be done in parallel** (run all 6 at once)

---

## Verification Checklist

### Dashboard CI/CD
- [ ] Workflow file exists: `.github/workflows/docker-publish.yml`
- [ ] Docker Hub secrets configured
- [ ] Workflow runs successfully on push
- [ ] Image published: `docker pull faultmaven/faultmaven-dashboard:latest`
- [ ] Image works: `docker run -p 3000:80 faultmaven/faultmaven-dashboard:latest`
- [ ] Dashboard accessible at http://localhost:3000

### Backend Services CI/CD
For each of 6 services:
- [ ] Workflow file exists in each repo
- [ ] Docker Hub secrets configured
- [ ] Workflow runs successfully
- [ ] Images published to Docker Hub
- [ ] All images have `:latest` tag
- [ ] Can pull and run each image

### Dashboard Pages
- [ ] CaseListPage.tsx exists in src/pages/
- [ ] SettingsPage.tsx exists in src/pages/
- [ ] Router includes `/cases` route
- [ ] Router includes `/settings` route
- [ ] Navigation menu updated
- [ ] Case list page displays cases
- [ ] Settings page saves preferences
- [ ] No TypeScript errors
- [ ] npm run build succeeds

---

## Docker Hub Repository Setup

Before CI/CD will work, ensure Docker Hub repositories exist:

### Create Repositories

**Via Docker Hub Web UI:**
1. Go to https://hub.docker.com/
2. Click "Create Repository"
3. Create these repositories (all public):
   - `faultmaven/fm-api-gateway`
   - `faultmaven/fm-auth-service`
   - `faultmaven/fm-case-service`
   - `faultmaven/fm-session-service`
   - `faultmaven/fm-knowledge-service`
   - `faultmaven/fm-evidence-service`
   - `faultmaven/faultmaven-dashboard`

**Or via CLI:**
```bash
# Install Docker Hub CLI tool
# https://github.com/docker/hub-tool

# Login
docker login

# Create repos (automated)
for repo in fm-api-gateway fm-auth-service fm-case-service fm-session-service fm-knowledge-service fm-evidence-service faultmaven-dashboard; do
  # Repository will be created on first push
  echo "Repo $repo will be created on first CI/CD run"
done
```

---

## GitHub Secrets Configuration

Each repository needs Docker Hub credentials:

### Add Secrets (for each repo)

**Via GitHub Web UI:**
1. Go to repository → Settings → Secrets and variables → Actions
2. Click "New repository secret"
3. Add two secrets:

| Secret Name | Value |
|-------------|-------|
| `DOCKERHUB_USERNAME` | Your Docker Hub username |
| `DOCKERHUB_TOKEN` | Your Docker Hub access token |

**Generate Docker Hub Token:**
1. Go to https://hub.docker.com/settings/security
2. Click "New Access Token"
3. Name: "FaultMaven CI/CD"
4. Permissions: Read & Write
5. Copy token (save it - won't be shown again)

---

## Troubleshooting

### Issue: Workflow fails with "unauthorized"

**Solution:**
```bash
# Verify secrets are set correctly
# Settings → Secrets → Check DOCKERHUB_USERNAME and DOCKERHUB_TOKEN
# Regenerate token if needed
```

### Issue: Image build fails on arm64

**Solution:**
```yaml
# Edit workflow file to build only amd64 for now
platforms: linux/amd64
```

### Issue: Dashboard build fails with TypeScript errors

**Solution:**
```bash
# Fix TypeScript errors locally first
npm run build  # Should succeed locally

# Common fixes:
# - Import React: import { useState } from 'react';
# - Type interfaces properly
# - Fix any linting errors
```

### Issue: Vite environment variables not working

**Solution:**
```typescript
// In dashboard code, use:
const apiUrl = import.meta.env.VITE_API_URL || 'http://localhost:8000';

// NOT:
const apiUrl = process.env.VITE_API_URL;  // Wrong (Node.js style)
```

---

## Timeline & Effort Estimates

| Task | Priority | Time | Blocker |
|------|----------|------|---------|
| Dashboard CI/CD setup | 🔴 Critical | 30 min | docker-compose |
| Add missing pages | 🟠 High | 1 hour | User experience |
| Backend CI/CD (all 6) | 🟡 Medium | 2 hours | Automation |
| **Total** | | **3.5 hours** | |

### Parallel Execution

Can be done simultaneously:
- Dashboard CI/CD (Person A)
- Backend services CI/CD (Person B)
- Add missing pages (Person A, after dashboard CI/CD)

**Fastest completion:** 1.5 hours with 2 people

---

## Success Criteria

**All issues resolved when:**

✅ **CI/CD Workflows (Issue 1 & 2):**
- [ ] All 7 repositories have `.github/workflows/docker-publish.yml`
- [ ] All 7 Docker images published to Docker Hub
- [ ] All images have `:latest` tag
- [ ] docker-compose.yml can pull all images successfully
- [ ] `docker-compose up -d` works end-to-end

✅ **Missing Dashboard Pages (Issue 3):**
- [ ] Case list page accessible at `/cases`
- [ ] Settings page accessible at `/settings`
- [ ] Both pages functional with no errors
- [ ] Navigation menu includes links to both pages
- [ ] Dashboard Docker image includes new pages

**Verification Command:**
```bash
# Should succeed completely
cd faultmaven-deploy
docker-compose pull  # All images pull successfully
docker-compose up -d  # All 8 services start (7 + redis)
curl http://localhost:8000/health  # API responds
curl http://localhost:3000  # Dashboard loads
curl http://localhost:3000/cases  # Case list loads
curl http://localhost:3000/settings  # Settings loads
```

---

## Summary

All 3 issues have complete, production-ready solutions:

1. ✅ **Backend CI/CD**: Template ready (`backend-service-docker.yml`)
2. ✅ **Dashboard CI/CD**: Template ready (`dashboard-docker.yml`) - **URGENT**
3. ✅ **Missing Pages**: Complete implementations (`CaseListPage.tsx`, `SettingsPage.tsx`)

**Next Steps:**
1. Deploy dashboard CI/CD first (30 min) - unblocks docker-compose
2. Add missing pages to dashboard (1 hour)
3. Deploy backend CI/CD workflows (2 hours, can be parallel)

**Files Ready for Transfer:**
- `docs/cicd-templates/backend-service-docker.yml`
- `docs/cicd-templates/dashboard-docker.yml`
- `docs/dashboard-templates/CaseListPage.tsx`
- `docs/dashboard-templates/SettingsPage.tsx`

All ready to copy to respective repositories!
