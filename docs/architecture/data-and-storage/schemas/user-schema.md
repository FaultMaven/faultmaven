# User Storage Design - Enterprise SaaS

**Version**: 3.0
**Status**: ✅ Implemented (baseline migration `001_clean_baseline`)
**Last Updated**: 2026-04-18
**Implementation**: SQLAlchemy models at `faultmaven/infrastructure/persistence/models.py`; baseline migration at `alembic/versions/20260317_1919_424078e5aa04_001_clean_baseline.py`

---

## Implementation Status

All user-domain tables, repositories, services, and API endpoints are implemented and deployed via the baseline migration. This document describes the live schema.

| Component | Status | Location |
|-----------|--------|----------|
| ✅ Schema | Deployed | `alembic/versions/20260317_*_001_clean_baseline.py` |
| ✅ SQLAlchemy models | Live | `faultmaven/infrastructure/persistence/models.py` |
| ✅ Repositories | Live | `faultmaven/modules/auth/infrastructure/repositories/` |
| ✅ Services | Live | `faultmaven/modules/auth/domain/services/` |
| ✅ API endpoints | Live | `faultmaven/modules/auth/api/` (auth, oauth, organizations, teams, session) |

**Tables delivered by the user domain** (11 total): `users`, `organizations`, `organization_members`, `roles`, `permissions`, `role_permissions`, `teams`, `team_members`, `user_audit_log`, `oauth_revoked_tokens`, `oauth_authorization_codes`. Row-Level Security policies, seed roles/permissions, and the `user_has_org_permission()` helper are all installed by the baseline migration.

---

## Executive Summary

This document defines the **authoritative user storage design** for FaultMaven as an enterprise SaaS application.

**Design Philosophy**: Follow industry best practices for multi-tenant SaaS authentication and authorization.

**Key Requirements**:
- ✅ Multi-tenancy (organizations/workspaces)
- ✅ Role-Based Access Control (RBAC)
- ✅ Team collaboration
- ✅ Audit trail
- ✅ SSO integration ready
- ✅ Secure password storage
- ✅ Account lifecycle management

**Storage Strategy**:
- **Development**: Redis (DevUserStore) - simple, fast
- **Production**: PostgreSQL - normalized, scalable, ACID guarantees

---

## Table of Contents

1. [Current vs Target State](#1-current-vs-target-state)
2. [Enterprise SaaS Schema](#2-enterprise-saas-schema)
3. [Multi-Tenancy Design](#3-multi-tenancy-design)
4. [RBAC Model](#4-rbac-model)
5. [Security](#5-security)
6. [Audit Trail](#6-audit-trail)
7. [Migration Path](#7-migration-path)
8. [Implementation Checklist](#8-implementation-checklist)

---

## 1. Current vs Target State

### 1.1 Current Implementation (Development)

**Storage**: Redis via `DevUserStore`

**Schema** (Redis keys):
```
auth:user:{user_id} → {
  user_id, username, email, display_name,
  hashed_password, created_at, last_login, roles
}
auth:username:{username} → user_id
auth:email:{email} → user_id
```

**Limitations**:
- ❌ No organization/workspace support
- ❌ No team management
- ❌ No granular permissions
- ❌ No audit trail
- ❌ No SSO integration
- ❌ Roles as simple strings (not relational)

### 1.2 Target State (Production PostgreSQL)

**Storage**: PostgreSQL with normalized enterprise schema

**Tables**: 8 core tables
- `users` - User accounts
- `organizations` - Tenant workspaces
- `organization_members` - User-organization mapping
- `teams` - Sub-organization groups
- `team_members` - User-team mapping
- `roles` - Role definitions
- `permissions` - Permission definitions
- `role_permissions` - Role-permission mapping
- `user_audit_log` - Security audit trail

---

## 2. Enterprise SaaS Schema

### 2.1 Core Entity-Relationship Diagram

```
┌──────────────┐        ┌─────────────────┐        ┌──────────┐
│ users        │────────│ org_members     │────────│   orgs   │
│              │  N:M   │                 │   N:1  │          │
│ • user_id    │        │ • user_id       │        │ • organization_id │
│ • email      │        │ • organization_id        │        │ • name   │
│ • username   │        │ • role          │        │ • plan   │
└──────────────┘        │ • joined_at     │        └──────────┘
                        └─────────────────┘
                                │
                                │ N:1
                                ▼
                        ┌─────────────────┐
                        │ roles           │
                        │ • role_id       │
                        │ • name          │
                        │ • scope         │
                        └─────────────────┘
                                │
                                │ N:M
                                ▼
                        ┌─────────────────┐
                        │ permissions     │
                        │ • permission_id │
                        │ • resource      │
                        │ • action        │
                        └─────────────────┘
```

### 2.2 PostgreSQL Schema Definition

#### Table: users

```sql
CREATE TABLE users (
    -- Primary Key
    user_id VARCHAR(36) PRIMARY KEY DEFAULT gen_random_uuid()::text,

    -- Denormalized role string (in addition to organization_members.role_id)
    -- Used for simple local-auth deployments. Column width matches the live schema.
    roles VARCHAR(500),

    -- Authentication
    email VARCHAR(255) NOT NULL UNIQUE,
    username VARCHAR(100) UNIQUE,  -- Optional, email can be used
    hashed_password VARCHAR(255),  -- NULL for SSO-only users

    -- Profile
    display_name VARCHAR(200),
    avatar_url VARCHAR(500),
    timezone VARCHAR(50) DEFAULT 'UTC',
    locale VARCHAR(10) DEFAULT 'en-US',

    -- Account Status
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_email_verified BOOLEAN NOT NULL DEFAULT false,
    email_verified_at TIMESTAMPTZ,

    -- SSO Integration
    sso_provider VARCHAR(50),  -- 'google', 'okta', 'azure', etc.
    sso_provider_id VARCHAR(255),  -- External ID from SSO provider
    UNIQUE(sso_provider, sso_provider_id),

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    last_password_change_at TIMESTAMPTZ,

    -- Soft Delete
    deleted_at TIMESTAMPTZ,

    -- Constraints
    CONSTRAINT users_email_format CHECK (email ~* '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}$'),
    CONSTRAINT users_password_or_sso CHECK (
        (hashed_password IS NOT NULL) OR (sso_provider IS NOT NULL)
    )
);

-- Indexes
CREATE INDEX idx_users_email ON users(email) WHERE deleted_at IS NULL;
CREATE INDEX idx_users_sso ON users(sso_provider, sso_provider_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_users_created_at ON users(created_at DESC);

COMMENT ON TABLE users IS 'User accounts with SSO support and soft delete';
COMMENT ON COLUMN users.hashed_password IS 'Bcrypt hash, NULL for SSO-only users';
COMMENT ON COLUMN users.sso_provider_id IS 'External user ID from SSO provider (e.g., Google sub claim)';
```

#### Table: organizations

```sql
CREATE TABLE organizations (
    -- Primary Key
    organization_id VARCHAR(64) PRIMARY KEY DEFAULT ('org_' || gen_random_uuid()::text),

    -- Organization Info
    name VARCHAR(200) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,  -- URL-friendly identifier
    domain VARCHAR(255),  -- Primary email domain (for auto-join)

    -- Subscription
    plan_tier VARCHAR(20) NOT NULL DEFAULT 'free',  -- 'free', 'pro', 'enterprise'
    max_users INTEGER NOT NULL DEFAULT 5,

    -- Billing
    stripe_customer_id VARCHAR(100),
    stripe_subscription_id VARCHAR(100),

    -- Settings
    settings JSONB DEFAULT '{}'::jsonb,  -- Flexible org-specific settings

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trial_ends_at TIMESTAMPTZ,

    -- Soft Delete
    deleted_at TIMESTAMPTZ,

    -- Constraints
    CONSTRAINT organizations_slug_format CHECK (slug ~* '^[a-z0-9-]+$'),
    CONSTRAINT organizations_plan_tier_valid CHECK (plan_tier IN ('free', 'pro', 'enterprise'))
);

-- Indexes
CREATE INDEX idx_organizations_slug ON organizations(slug) WHERE deleted_at IS NULL;
CREATE INDEX idx_organizations_domain ON organizations(domain) WHERE deleted_at IS NULL;

COMMENT ON TABLE organizations IS 'Tenant organizations (workspaces) for multi-tenancy';
COMMENT ON COLUMN organizations.slug IS 'URL slug for organization (e.g., acme-corp)';
```

#### Table: organization_members

```sql
CREATE TABLE organization_members (
    -- Composite Primary Key
    user_id VARCHAR(20) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    organization_id VARCHAR(20) NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, organization_id),

    -- Role in Organization
    role_id VARCHAR(20) NOT NULL REFERENCES roles(role_id),

    -- Invitation
    invited_by VARCHAR(20) REFERENCES users(user_id),
    invited_at TIMESTAMPTZ,
    invitation_accepted_at TIMESTAMPTZ,

    -- Timestamps
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Constraints
    CONSTRAINT org_members_accepted_after_invited CHECK (
        invitation_accepted_at IS NULL OR invitation_accepted_at >= invited_at
    )
);

-- Indexes
CREATE INDEX idx_org_members_organization_id ON organization_members(organization_id);
CREATE INDEX idx_org_members_role_id ON organization_members(role_id);

COMMENT ON TABLE organization_members IS 'User membership in organizations with roles';
```

#### Table: teams

```sql
CREATE TABLE teams (
    -- Primary Key
    team_id VARCHAR(20) PRIMARY KEY DEFAULT ('team_' || gen_random_uuid()::text),

    -- Parent Organization
    organization_id VARCHAR(20) NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,

    -- Team Info
    name VARCHAR(200) NOT NULL,
    description TEXT,

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Soft Delete
    deleted_at TIMESTAMPTZ,

    -- Unique name within organization
    UNIQUE (organization_id, name)
);

-- Indexes
CREATE INDEX idx_teams_organization_id ON teams(organization_id) WHERE deleted_at IS NULL;

COMMENT ON TABLE teams IS 'Sub-organization groups for collaboration';
```

#### Table: team_members

```sql
CREATE TABLE team_members (
    -- Composite Primary Key
    user_id VARCHAR(20) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    team_id VARCHAR(20) NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, team_id),

    -- Timestamps
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_team_members_team_id ON team_members(team_id);

COMMENT ON TABLE team_members IS 'User membership in teams';
```

#### Table: roles

```sql
CREATE TABLE roles (
    -- Primary Key
    role_id VARCHAR(20) PRIMARY KEY,

    -- Role Info
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,

    -- Scope
    scope VARCHAR(20) NOT NULL DEFAULT 'organization',  -- 'system', 'organization', 'team'

    -- Built-in vs Custom
    is_system_role BOOLEAN NOT NULL DEFAULT false,

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Constraints
    CONSTRAINT roles_scope_valid CHECK (scope IN ('system', 'organization', 'team'))
);

-- Seed System Roles
INSERT INTO roles (role_id, name, description, scope, is_system_role) VALUES
('role_super_admin', 'Super Admin', 'Full system access (FaultMaven staff only)', 'system', true),
('role_org_owner', 'Owner', 'Organization owner with full admin rights', 'organization', true),
('role_org_admin', 'Admin', 'Organization administrator', 'organization', true),
('role_org_member', 'Member', 'Standard organization member', 'organization', true),
('role_org_viewer', 'Viewer', 'Read-only organization access', 'organization', true),
('role_team_lead', 'Team Lead', 'Team leader with management rights', 'team', true),
('role_team_member', 'Team Member', 'Standard team member', 'team', true);

COMMENT ON TABLE roles IS 'Role definitions for RBAC system';
COMMENT ON COLUMN roles.is_system_role IS 'System roles cannot be deleted or modified';
```

#### Table: permissions

```sql
CREATE TABLE permissions (
    -- Primary Key
    permission_id VARCHAR(30) PRIMARY KEY,

    -- Permission Definition
    resource VARCHAR(50) NOT NULL,  -- 'cases', 'knowledge_base', 'settings', etc.
    action VARCHAR(50) NOT NULL,    -- 'read', 'write', 'delete', 'manage'
    description TEXT,

    -- Unique permission per resource+action
    UNIQUE (resource, action)
);

-- Seed Core Permissions
INSERT INTO permissions (permission_id, resource, action, description) VALUES
-- Cases
('perm_cases_read', 'cases', 'read', 'View cases'),
('perm_cases_write', 'cases', 'write', 'Create and edit cases'),
('perm_cases_delete', 'cases', 'delete', 'Delete cases'),
('perm_cases_manage', 'cases', 'manage', 'Full case management'),

-- Knowledge Base
('perm_kb_read', 'knowledge_base', 'read', 'View knowledge base'),
('perm_kb_write', 'knowledge_base', 'write', 'Add to knowledge base'),
('perm_kb_manage', 'knowledge_base', 'manage', 'Manage knowledge base'),

-- Organization Settings
('perm_org_read', 'organization', 'read', 'View organization settings'),
('perm_org_write', 'organization', 'write', 'Edit organization settings'),
('perm_org_manage', 'organization', 'manage', 'Full organization management'),

-- Users
('perm_users_read', 'users', 'read', 'View users'),
('perm_users_invite', 'users', 'invite', 'Invite users'),
('perm_users_manage', 'users', 'manage', 'Manage users and roles'),

-- Teams
('perm_teams_read', 'teams', 'read', 'View teams'),
('perm_teams_write', 'teams', 'write', 'Create and edit teams'),
('perm_teams_manage', 'teams', 'manage', 'Full team management');

COMMENT ON TABLE permissions IS 'Permission definitions for RBAC system';
```

#### Table: role_permissions

```sql
CREATE TABLE role_permissions (
    -- Composite Primary Key
    role_id VARCHAR(20) NOT NULL REFERENCES roles(role_id) ON DELETE CASCADE,
    permission_id VARCHAR(30) NOT NULL REFERENCES permissions(permission_id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
);

-- Seed Role-Permission Mappings
-- Owner: Full access
INSERT INTO role_permissions (role_id, permission_id)
SELECT 'role_org_owner', permission_id FROM permissions WHERE resource IN ('cases', 'knowledge_base', 'organization', 'users', 'teams');

-- Admin: Most permissions except org management
INSERT INTO role_permissions (role_id, permission_id)
SELECT 'role_org_admin', permission_id FROM permissions WHERE resource IN ('cases', 'knowledge_base', 'users', 'teams');

-- Member: Read/write cases and KB
INSERT INTO role_permissions (role_id, permission_id)
SELECT 'role_org_member', permission_id FROM permissions WHERE resource IN ('cases', 'knowledge_base') AND action IN ('read', 'write');

-- Viewer: Read-only
INSERT INTO role_permissions (role_id, permission_id)
SELECT 'role_org_viewer', permission_id FROM permissions WHERE action = 'read';

COMMENT ON TABLE role_permissions IS 'Role-to-permission mappings for RBAC';
```

#### Table: user_audit_log

```sql
CREATE TABLE user_audit_log (
    -- Primary Key
    audit_id BIGSERIAL PRIMARY KEY,

    -- Actor
    user_id VARCHAR(20) REFERENCES users(user_id) ON DELETE SET NULL,
    organization_id VARCHAR(20) REFERENCES organizations(organization_id) ON DELETE SET NULL,

    -- Event
    event_type VARCHAR(100) NOT NULL,  -- 'user.login', 'user.password_change', 'role.assigned', etc.
    event_category VARCHAR(50) NOT NULL,  -- 'authentication', 'authorization', 'data_access'

    -- Details
    resource_type VARCHAR(50),  -- 'case', 'user', 'team', etc.
    resource_id VARCHAR(50),
    details JSONB,

    -- Context
    ip_address INET,
    user_agent TEXT,

    -- Timestamp
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_user_audit_log_user_id ON user_audit_log(user_id, created_at DESC);
CREATE INDEX idx_user_audit_log_organization_id ON user_audit_log(organization_id, created_at DESC);
CREATE INDEX idx_user_audit_log_event_type ON user_audit_log(event_type);
CREATE INDEX idx_user_audit_log_created_at ON user_audit_log(created_at DESC);

-- Partition by month for performance (optional, for high-volume)
-- CREATE TABLE user_audit_log_y2025m01 PARTITION OF user_audit_log
-- FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');

COMMENT ON TABLE user_audit_log IS 'Security audit trail for compliance and forensics';
```

---

## 3. Multi-Tenancy Design

### 3.1 Tenant Isolation

**Organization Scope**: Each organization is a separate tenant workspace.

```sql
-- Get cases for user's organization
SELECT c.*
FROM cases c
JOIN users u ON u.user_id = c.user_id
JOIN organization_members om ON om.user_id = u.user_id
WHERE om.organization_id = :current_organization_id
AND om.user_id = :current_user_id;
```

### 3.2 Cross-Organization Access

**Forbidden**: Users cannot access data from other organizations.

**Implementation**: Row-Level Security (RLS) in PostgreSQL

```sql
-- Enable RLS on cases table
ALTER TABLE cases ENABLE ROW LEVEL SECURITY;

-- Policy: Users can only see cases from their organization
CREATE POLICY cases_org_isolation ON cases
FOR SELECT
USING (
    user_id IN (
        SELECT u.user_id
        FROM users u
        JOIN organization_members om ON om.user_id = u.user_id
        WHERE om.organization_id = current_setting('app.current_organization_id')::varchar
    )
);
```

### 3.3 Organization Types

| Plan | Max Users | Features |
|------|-----------|----------|
| **Free** | 5 | Basic troubleshooting, 10 cases/month |
| **Pro** | 50 | Advanced features, unlimited cases, API access |
| **Enterprise** | Unlimited | SSO, custom roles, dedicated support, SLA |

---

## 4. RBAC Model

### 4.1 Permission Model

**Format**: `{resource}.{action}`

**Examples**:
- `cases.read` - View cases
- `cases.write` - Create/edit cases
- `cases.delete` - Delete cases
- `users.invite` - Invite users
- `organization.manage` - Manage org settings

### 4.2 Role Hierarchy

```
System Scope:
  └─ Super Admin (FaultMaven staff only)

Organization Scope:
  ├─ Owner (full control)
  ├─ Admin (manage users, settings)
  ├─ Member (read/write cases)
  └─ Viewer (read-only)

Team Scope:
  ├─ Team Lead (manage team)
  └─ Team Member (team collaboration)
```

### 4.3 Permission Check Example

```python
async def check_permission(user_id: str, organization_id: str, permission: str) -> bool:
    """
    Check if user has permission in organization.

    Args:
        user_id: User identifier
        organization_id: Organization identifier
        permission: Permission string (e.g., 'cases.write')

    Returns:
        True if user has permission
    """
    query = """
        SELECT EXISTS (
            SELECT 1
            FROM organization_members om
            JOIN role_permissions rp ON rp.role_id = om.role_id
            JOIN permissions p ON p.permission_id = rp.permission_id
            WHERE om.user_id = :user_id
            AND om.organization_id = :organization_id
            AND (
                p.resource || '.' || p.action = :permission
                OR p.action = 'manage' AND p.resource = split_part(:permission, '.', 1)
            )
        )
    """

    result = await db.execute(query, {
        "user_id": user_id,
        "organization_id": organization_id,
        "permission": permission
    })

    return result.scalar()
```

---

## 5. Security

### 5.1 Password Requirements

**Hashing**: Bcrypt with cost factor 12

```python
import bcrypt

def hash_password(password: str) -> str:
    """Hash password using bcrypt."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    """Verify password against hash."""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
```

**Requirements**:
- Minimum 12 characters
- At least one uppercase letter
- At least one lowercase letter
- At least one number
- At least one special character

### 5.2 SSO Integration

**Supported Providers**: Google, Microsoft Azure AD, Okta

**Flow**:
1. User initiates SSO login
2. Redirect to provider (OAuth2/SAML)
3. Provider authenticates user
4. Callback with identity token
5. Match user by `sso_provider` + `sso_provider_id`
6. Create user if first login (Just-In-Time provisioning)

**User Record**:
```sql
-- SSO user (no password)
INSERT INTO users (email, sso_provider, sso_provider_id)
VALUES ('john@acme.com', 'google', 'google_user_12345');
```

### 5.3 Session Management

**Storage**: Redis (real or FakeRedis — see `faultmaven/modules/auth/infrastructure/stores/`).

**Session Security**:

- ✅ HTTP-only cookies
- ✅ Secure flag (HTTPS only)
- ✅ SameSite=Strict

**TTL concepts (distinct values — see `faultmaven/config/settings.py` and `auth_session_service.py`)**:

- **JWT access token**: 60 min default (`JWT_ACCESS_TOKEN_EXPIRY`)
- **JWT refresh token**: 7 days default (`JWT_REFRESH_TOKEN_EXPIRY`)
- **Session record TTL (Redis)**: 24 h default (`SessionSettings.session_ttl_hours`)
- **Session inactivity timeout**: 30 min default (`SessionSettings.timeout_minutes`)

---

## 6. Audit Trail

### 6.1 Logged Events

| Event Type | Category | Example |
|------------|----------|---------|
| `user.login` | authentication | User successful login |
| `user.login_failed` | authentication | Failed login attempt |
| `user.logout` | authentication | User logout |
| `user.password_change` | authentication | Password changed |
| `user.created` | authorization | New user created |
| `user.role_assigned` | authorization | Role assigned to user |
| `case.created` | data_access | Case created |
| `case.viewed` | data_access | Case viewed |
| `case.deleted` | data_access | Case deleted |
| `org.settings_changed` | administration | Organization settings modified |

### 6.2 Retention Policy

- **Authentication events**: 90 days
- **Authorization events**: 1 year
- **Data access events**: 1 year
- **Administration events**: 7 years (compliance)

### 6.3 Query Examples

```sql
-- Get all login attempts for user
SELECT *
FROM user_audit_log
WHERE user_id = 'user_abc123'
AND event_category = 'authentication'
ORDER BY created_at DESC
LIMIT 50;

-- Detect suspicious activity (multiple failed logins)
SELECT user_id, ip_address, COUNT(*) as failed_attempts
FROM user_audit_log
WHERE event_type = 'user.login_failed'
AND created_at > NOW() - INTERVAL '1 hour'
GROUP BY user_id, ip_address
HAVING COUNT(*) > 5;
```

---

## 7. Implementation Status

✅ **Implemented and deployed** — baseline migration `001_clean_baseline` (revision `424078e5aa04`) creates all 11 user-domain tables, 7 seed roles, and 19 seed permissions. The live source of truth is:

- SQLAlchemy models: `faultmaven/infrastructure/persistence/models.py`
- Repositories: `faultmaven/modules/auth/infrastructure/repositories/`
- Services: `faultmaven/modules/auth/domain/services/`
- API: `faultmaven/modules/auth/api/` (auth, oauth, organizations, teams, session, rate_limiting)
- Row-Level Security and the `user_has_org_permission()` function are installed by the baseline migration

---

## Summary

### Enterprise SaaS User Schema

✅ **Multi-Tenancy**: Organization-based isolation
✅ **RBAC**: Flexible role and permission system
✅ **Team Collaboration**: Sub-organization teams
✅ **SSO Ready**: Google, Azure AD, Okta support
✅ **Audit Trail**: Comprehensive security logging
✅ **Scalable**: Normalized schema, Row-Level Security

### Deployment State

- ✅ Schema deployed via baseline migration (`alembic upgrade head`)
- ✅ All repositories and services live
- ✅ 30+ REST endpoints under `modules/auth/api/`
- ✅ Multi-tenancy via `organizations` + `organization_members` + `teams`
- ✅ Full RBAC via `roles`, `permissions`, `role_permissions`
- ✅ Audit trail via `user_audit_log`
- ✅ SSO via `sso_provider` / `sso_provider_id` columns; OAuth code flow via `oauth_authorization_codes` and `oauth_revoked_tokens`

---

**References**:

- SQLAlchemy models: [faultmaven/infrastructure/persistence/models.py](../../../../faultmaven/infrastructure/persistence/models.py)
- Auth module: [faultmaven/modules/auth/](../../../../faultmaven/modules/auth/)
- Baseline migration: [alembic/versions/20260317_1919_424078e5aa04_001_clean_baseline.py](../../../../alembic/versions/20260317_1919_424078e5aa04_001_clean_baseline.py)
- Redis/FakeRedis session store: [faultmaven/modules/auth/infrastructure/stores/](../../../../faultmaven/modules/auth/infrastructure/stores/)
