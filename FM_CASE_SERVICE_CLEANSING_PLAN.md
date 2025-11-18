# fm-case-service → PUBLIC Cleansing Plan

**Goal:** Transform the existing private `fm-case-service` into the PUBLIC open-source version by removing all enterprise-specific features.

**Status:** ✅ Already mostly clean! No organization_id/team_id found.

---

## 1. Current Assessment

### ✅ Already PUBLIC-Ready Features
- Basic case CRUD (create, read, update, delete)
- Single-user authorization (user_id based)
- Session linking
- Status management
- SQLite support
- Auto-generated titles
- Pagination

### ❌ Features to Remove/Simplify (if present)
- PostgreSQL production code (keep SQLite only for PUBLIC)
- Any organization/team references (none found ✅)
- RBAC checks (currently uses simple user_id ownership ✅)
- Advanced analytics/metrics
- Enterprise-only API endpoints

---

## 2. Cleansing Checklist

### Phase 1: Database Layer

**File:** `src/case_service/infrastructure/database/models.py`

**Current State:** ✅ Clean
- Uses `user_id` (not organization_id)
- Simple ownership model
- No multi-tenant features

**Action:** ✅ No changes needed

---

**File:** `src/case_service/infrastructure/database/client.py`

**Check for:**
- [ ] PostgreSQL-specific code
- [ ] Connection pooling for enterprise scale
- [ ] Multi-tenant database selection

**Action:**
```python
# KEEP: SQLite async support
# REMOVE: PostgreSQL connection strings
# REMOVE: Any connection pooling complexity (use simple SQLite)
```

**Example Clean Code:**
```python
# PUBLIC version - SQLite only
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

DATABASE_URL = "sqlite+aiosqlite:///data/cases.db"

engine = create_async_engine(
    DATABASE_URL,
    echo=True,  # For debugging
)
```

---

### Phase 2: API Layer

**File:** `src/case_service/api/routes/cases.py`

**Current Endpoints:** (all look PUBLIC-appropriate)
- `POST /api/v1/cases` - Create
- `GET /api/v1/cases/{case_id}` - Get
- `PUT /api/v1/cases/{case_id}` - Update
- `DELETE /api/v1/cases/{case_id}` - Delete
- `GET /api/v1/cases` - List (user's own cases)
- `GET /api/v1/cases/session/{session_id}` - Get by session
- `POST /api/v1/cases/{case_id}/status` - Update status

**Check for Enterprise-Only Endpoints:**
- [ ] `/api/v1/cases/organization/{org_id}` - List org cases → REMOVE
- [ ] `/api/v1/cases/team/{team_id}` - List team cases → REMOVE
- [ ] `/api/v1/cases/{case_id}/share` - Share case → REMOVE
- [ ] `/api/v1/cases/{case_id}/transfer` - Transfer ownership → REMOVE

**Action:** Review routes file and remove any enterprise endpoints

---

### Phase 3: Authorization Logic

**File:** `src/case_service/core/case_manager.py`

**Current Authorization:** (should be simple user_id check)
```python
# PUBLIC version - simple ownership check
if case.user_id != current_user_id:
    raise Forbidden("Not authorized to access this case")
```

**Check for:**
- [ ] Organization membership checks
- [ ] Team permission checks
- [ ] Role-based access (admin, member, viewer)

**Action:** Ensure only simple `user_id` equality checks

---

### Phase 4: Dependencies & Configuration

**File:** `pyproject.toml` or `requirements.txt`

**Dependencies to REMOVE:**
```toml
# REMOVE these enterprise dependencies
psycopg2-binary  # PostgreSQL driver
sqlalchemy-pg8000  # PostgreSQL async driver
redis  # If used for distributed caching
celery  # If used for background jobs
prometheus-client  # Advanced metrics
```

**Dependencies to KEEP:**
```toml
# KEEP these core dependencies
fastapi
uvicorn
sqlalchemy
aiosqlite  # SQLite async driver
pydantic
python-dotenv
```

---

**File:** `src/case_service/config/settings.py`

**Check for:**
- [ ] `POSTGRESQL_URL` configuration
- [ ] `ORGANIZATION_MODE` flags
- [ ] Enterprise feature flags

**Clean Example:**
```python
# PUBLIC version - simple configuration
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///data/cases.db"

    # API
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8003

    # Logging
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
```

---

### Phase 5: Documentation

**File:** `README.md`

**Changes Needed:**
- [x] Update title: "FaultMaven Case Service - Open Source"
- [x] Add Apache 2.0 license badge
- [x] Remove references to "microservices migration"
- [x] Add "single-user deployment" notes
- [x] Update installation to use Docker
- [x] Add link to `faultmaven-deploy` for easy setup

**New README Structure:**
```markdown
# FaultMaven Case Service

**Open-Source Case Management Microservice**

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

## Overview

Lightweight case management service for FaultMaven troubleshooting platform.
Designed for single-user, self-hosted deployments.

## Features

- ✅ Case lifecycle management (active → investigating → resolved)
- ✅ Session linking
- ✅ SQLite storage (zero configuration)
- ✅ RESTful API
- ✅ Auto-generated titles
- ✅ Tags and metadata support

## Quick Start

### Using Docker (Recommended)

See [faultmaven-deploy](https://github.com/FaultMaven/faultmaven-deploy)
for complete deployment with all services.

### Standalone Development

\`\`\`bash
# Install
pip install -r requirements.txt

# Run
uvicorn case_service.main:app --port 8003
\`\`\`

## API Documentation

Once running, visit: `http://localhost:8003/docs`

## License

Apache 2.0 - see [LICENSE](LICENSE)
```

---

### Phase 6: Testing

**File:** `tests/`

**Check for:**
- [ ] Tests that assume multi-tenancy
- [ ] Tests for organization/team features
- [ ] Tests for PostgreSQL-specific features

**Action:**
- Keep all user_id-based ownership tests
- Remove any organization-scoped tests
- Ensure tests use SQLite (in-memory for speed)

**Example Test:**
```python
# PUBLIC version - simple ownership test
async def test_user_can_only_access_own_cases():
    # User 1 creates a case
    case = await create_case(user_id="user1", title="Test Case")

    # User 2 tries to access it
    with pytest.raises(Forbidden):
        await get_case(case_id=case.case_id, user_id="user2")

    # User 1 can access it
    retrieved = await get_case(case_id=case.case_id, user_id="user1")
    assert retrieved.case_id == case.case_id
```

---

### Phase 7: Licensing

**File:** `LICENSE`

**Action:**
```bash
# Add Apache 2.0 license
cp /path/to/apache-2.0-template LICENSE
```

**File:** `NOTICE` (optional but recommended)

**Content:**
```
FaultMaven Case Service
Copyright 2024-2025 FaultMaven

This product includes software developed by FaultMaven.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
```

---

### Phase 8: Docker Configuration

**File:** `Dockerfile`

**PUBLIC Version:**
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir poetry && \
    poetry config virtualenvs.create false && \
    poetry install --no-dev --no-interaction --no-ansi

# Copy source
COPY src/ ./src/

# Create data directory for SQLite
RUN mkdir -p /data && chmod 777 /data

# Expose port
EXPOSE 8003

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8003/health || exit 1

# Run service
CMD ["uvicorn", "case_service.main:app", "--host", "0.0.0.0", "--port", "8003"]
```

---

**File:** `.dockerignore`

**Add:**
```
__pycache__/
*.py[cod]
.pytest_cache/
.venv/
tests/
docs/
*.md
.git/
.gitignore
```

---

## 3. Verification Checklist

After cleansing, verify:

- [ ] No references to `organization_id` or `team_id`
- [ ] No PostgreSQL dependencies in requirements
- [ ] README mentions "open source" and "single-user"
- [ ] LICENSE file is Apache 2.0
- [ ] Dockerfile builds successfully
- [ ] Service runs with SQLite (no external DB needed)
- [ ] All tests pass
- [ ] API docs are accessible at `/docs`
- [ ] Health check endpoint works: `GET /health`

---

## 4. Post-Cleansing: Create PUBLIC Repository

```bash
# 1. Create new PUBLIC repo on GitHub
gh repo create FaultMaven/fm-case-service --public

# 2. Copy cleansed code to new directory
cp -r fm-case-service faultmaven-fm-case-service-public
cd faultmaven-fm-case-service-public

# 3. Initialize git
git init
git add .
git commit -m "Initial public release - Apache 2.0"

# 4. Push to PUBLIC repo
git remote add origin https://github.com/FaultMaven/fm-case-service.git
git branch -M main
git push -u origin main

# 5. Create release
git tag -a v1.0.0 -m "First public release"
git push origin v1.0.0
```

---

## 5. Next: Refactor PRIVATE to Consume PUBLIC

After the PUBLIC version is published, refactor the PRIVATE repo:

**File:** `Dockerfile.enterprise` (new file in PRIVATE repo)

```dockerfile
# Start from PUBLIC Docker image
FROM faultmaven/fm-case-service:v1.0.0

# Add enterprise layer
COPY enterprise/ /app/enterprise/

# Install enterprise dependencies
COPY requirements.enterprise.txt /app/
RUN pip install --no-cache-dir -r requirements.enterprise.txt

# Override with enterprise config
COPY config.enterprise.yaml /app/config/

# Enterprise entry point (extends public service)
CMD ["python", "-m", "enterprise.case_service_wrapper"]
```

**File:** `enterprise/case_service_wrapper.py` (new file in PRIVATE repo)

```python
"""Enterprise wrapper for public fm-case-service.

Adds:
- Organization and team support
- RBAC checks
- PostgreSQL database
- Advanced analytics
"""

from case_service.main import app  # Import PUBLIC app
from enterprise.middleware.org_filter import OrganizationFilter
from enterprise.middleware.rbac import RBACMiddleware

# Add enterprise middleware
app.add_middleware(OrganizationFilter)
app.add_middleware(RBACMiddleware)

# Add enterprise routes
from enterprise.routes import org_cases, team_cases
app.include_router(org_cases.router)
app.include_router(team_cases.router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
```

---

## 6. Summary

**fm-case-service Assessment:**
- ✅ Already 95% PUBLIC-ready!
- ✅ No organization/team code found
- ⚠️ May have PostgreSQL config to remove
- ⚠️ Need to verify no enterprise endpoints

**Estimated Cleansing Time:** 2-4 hours

**Key Actions:**
1. Remove PostgreSQL dependencies
2. Simplify database config to SQLite-only
3. Update README for public consumption
4. Add Apache 2.0 LICENSE
5. Test with Docker
6. Push to PUBLIC GitHub repo

This service is a great starting point because it's already clean!
