# Role-Based Access Control (RBAC)

## Overview

FaultMaven implements a role-based access control system to manage user permissions for Global Knowledge Base operations. This document describes the RBAC implementation, user management, and integration guidelines.

**Implementation Date:** October 2025
**Status:** ✅ Production Ready

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [User Roles](#user-roles)
3. [Protected Endpoints](#protected-endpoints)
4. [User Management](#user-management)
5. [Frontend Integration](#frontend-integration)
6. [Security](#security)
7. [Testing](#testing)

---

## Quick Start

### For Administrators

**List all users:**
```bash
python scripts/auth/list_users.py
```

**Create a platform admin (deployment operator, cross-tenant reach):**
```bash
python scripts/auth/create_user.py --username myoperator --role platform_admin
```

**Create an organization admin (tenant-bounded):**
```bash
python scripts/auth/create_user.py --username myadmin --role admin
```

**Promote an existing user to platform admin:**
```bash
fm-promote-platform-admin username
```

> **Upgrading an existing deployment.** The `admin` role no longer grants
> operator access. The bootstrap account (`admin` / `admin@local.faultmaven`) is
> re-granted the operator roles automatically at startup, but any *other*
> account that was an operator under the old single-role model — one created
> with `--role admin`, or promoted by the former `promote_to_admin.py` — holds
> only the org-scoped `admin` and will get 403 on every operator endpoint until
> it is promoted with the command above.

### For Developers

**Check if user is a platform admin in backend:**
```python
from faultmaven.api.v1.auth_dependencies import require_platform_admin

@router.post("/admin-endpoint")
async def admin_only(current_user: DevUser = Depends(require_platform_admin)):
    # Only platform admins can access this
    ...
```

**Check if user is a platform admin in frontend:**
```typescript
// The operator role, NOT the org-scoped `admin` — see the note below.
const isPlatformAdmin = user.roles.includes('platform_admin');

{isPlatformAdmin && (
  <button onClick={uploadToGlobalKB}>Upload to Global KB</button>
)}
```

---

## User Roles

> **Two role vocabularies, deliberately disjoint (ADR-012 D9).** The
> **operator** role `platform_admin` is deployment-scoped: it governs Global-KB
> authoring, cross-tenant case listing, user administration and LLM
> configuration, and is enforced by the `require_platform_admin` dependency.
> Separately, an **org-scoped** `Role` enum (`admin`, `member`, `viewer`) in
> `modules/auth/domain/models/rbac.py` carries a granular `Permission` mapping
> for organization/team RBAC; `admin` there is tenant-bounded and never crosses
> tenants. `platform_admin` is intentionally **not** a member of that enum —
> which is what stops `POST /admin/users/{id}/roles` from minting operators —
> so it maps to no granular permissions on its own. Wiring those permissions to
> endpoint checks is tracked as a separate RBAC reconciliation (#706). See
> [iam-design.md § Role Implementation](iam-design.md#role-implementation).

### Where roles are stored, and what reads them

`users.dev_roles` (a JSON array) is the **single canonical source of the JWT
`roles` claim, in both `AUTH_MODE=local` and `AUTH_MODE=oauth`**, and remains so
across the multi-tenant flip. `organization_members.role_id` records
*affiliation* — which organization a user belongs to and with what standing,
written by the SSO login path and read for membership verification. It is **not**
the claim source, and deriving the claim from it is deliberately left unwired
rather than half-wired: a partially-wired derivation is what makes role
administration silently stop working. Consequently `assign_role` / `remove_role`
writing `dev_roles` is correct in both modes and needs no deployment gate.

Roles live on **two independent axes**, and role administration must respect the
boundary:

| Axis | Values | Granted by | Read by |
|------|--------|-----------|---------|
| Org-scoped | `admin`, `member`, `viewer` | `POST/DELETE /admin/users/{id}/roles` | `Permission` mapping (not yet wired to endpoint checks) |
| Deployment | `platform_admin` | `fm-promote-platform-admin` only | `require_platform_admin` / `is_platform_admin()` |
| Base | `user` | registration, SSO JIT provisioning | nothing today (grants no permissions) |

The role-management API replaces a user's **org-scoped** role and preserves
every other role the account holds. It previously replaced the whole list, so
aiming it at an operator revoked `platform_admin` while reporting a successful
assignment — a silent operator lockout recoverable only from a shell (#706).
`platform_admin` is revoked by `fm-demote-platform-admin` and nothing else.

> **Note on today's enforcement surface.** `require_role` / `require_any_role`
> exist but have no production call sites, and JWT `scopes` are a fixed list
> rather than role-derived. So `platform_admin` is currently the only role the
> enforcement path actually consults; the org-scoped axis is recorded and
> surfaced (user listing, filtering) ahead of the permission wiring above.

### 1. Regular User (`user` role)

**Default role for all users**

#### Permissions
- ✅ Login and authenticate
- ✅ Search Global Knowledge Base (read-only)
- ✅ List Global KB documents (read-only)
- ✅ View Global KB document details (read-only)
- ✅ Upload documents to their own User KB
- ✅ Manage their own User KB documents (create, update, delete)
- ✅ Create and manage troubleshooting cases
- ✅ Use all troubleshooting features

#### Restrictions
- ❌ Cannot upload to Global KB
- ❌ Cannot modify Global KB documents
- ❌ Cannot delete Global KB documents
- ❌ Cannot perform bulk operations on Global KB

### 2. Platform Admin (`user` + `admin` + `platform_admin` roles)

**Enhanced permissions for KB content management**

#### Additional Permissions
All regular user permissions, PLUS:
- ✅ Upload documents to Global KB
- ✅ Update Global KB documents
- ✅ Delete Global KB documents
- ✅ Bulk update operations on Global KB
- ✅ Bulk delete operations on Global KB

---

## Protected Endpoints

> **Which endpoints require authentication is now generated, not maintained
> here.** [`docs/reference/api/README.md`](../../reference/api/README.md) states
> the scheme each operation requires, derived from the running app and enforced
> by the `api-contract-drift` CI job — it cannot fall behind the routes.
>
> The tables below remain authoritative for what this file uniquely knows:
> *which role or ownership* a route demands (`platform_admin` only, "404 if not
> visible", team membership). OpenAPI expresses "a bearer token is required"
> and nothing finer. When the two disagree about whether a route is
> authenticated at all, the generated reference is right by construction and
> this file needs correcting.

### Tenant-Scoped Resolution

Roles decide *what* a principal may do. They never decide *whose* data it
applies to. That is a separate, mandatory predicate:

> **Every path that resolves data by id or by similarity carries a tenant
> predicate and fails closed.** No tenant context means no results and no
> query — never an unscoped one. An id outside the caller's tenant answers
> **404**, exactly as an absent id does.

The three parts each close a different way of losing the predicate:

- **By id.** The lookup is split in two: a *trusted* unscoped load with no actor
  (`KnowledgeService.get_document`, `SuggestionService.get_suggestion`) used by
  ingestion, extraction and the write-policy check, and an *actor-facing* scoped
  load (`get_document_visible`, `get_suggestion_visible`) that every route uses.
  The scoped form takes a required `organization_id` and returns nothing for an
  absent id and for an out-of-tenant id alike, so no caller can tell them apart.
- **By similarity.** A vector query names no id, so the metadata predicate is
  the only isolation there is. `RunbookKnowledgeBase.search_runbooks` requires
  the searching principal's KB scope filter (`build_kb_scope_filter`: global ∪
  owned ∪ team-shared, the same allowlist as every other KB read), ANDs it into
  the ChromaDB `where` clause alongside `document_type == "runbook"`, and
  **refuses with a typed error** — never a silent `[]` — when no scope filter
  is supplied (fm#1030).
- **By allowlist.** Team visibility resolves to a set of ids in SQL
  (`resource_shares`). Both directions of that resolution — the inventory
  clause's share sub-select and `IShareRepository.list_resource_ids` — match the
  share row's own `organization_id`, so a row stamped with a foreign tenant
  grants nothing.

**404, not 403.** A refusal that distinguishes "you may not see this" from "this
does not exist" is an existence oracle: it confirms an id, and with it the shape
of another tenant's data. Out-of-tenant and absent therefore share one status and
one message on every id-addressed route.

**403 only for a caller with no tenant at all.** `require_actor_organization`
resolves the actor's organization and refuses (403) rather than returning `None`
for a caller to degrade into an unscoped query. That refusal does not depend on
the requested id, so it is not an oracle. Under `TENANT_PROVIDER=multi` the
Standalone sentinel is refused too: there it identifies the deployment, not an
organization — the same rule the request front door applies in
`api/middleware/tenant_scope.py`. Enforcing it in both places keeps the guarantee
independent of which dependencies a given router mounts.

**One predicate, two enforcement styles.** "Is this a usable tenant?" is decided
in exactly one place — `config.tenant_context.usable_tenant_id`, which answers
`None` for an absent org and for the Standalone sentinel under
`TENANT_PROVIDER=multi`. Every site that needs the answer calls it; none carries
its own copy of the test. Two of them **refuse**: `bind_request_org_context` at
the request front door and `require_actor_organization` at the route, both 403
with `UNSCOPED_REQUEST_MSG`. The rest **degrade** — the case read allowlist
(`CaseService._resolve_shared_case_ids`, `_resolve_team_filter_case_ids`), the KB
team arm (`resolve_shared_kb_ids`), the agent's shared-KB arm, and both
runbook-similarity consumers collapse to the empty set. **Degrading is deliberate
there**, because those arms already collapse on every other resolution failure
and narrowing an allowlist is the fail-closed direction — the owner arm still
answers, so the listing narrows rather than breaks. What none of them may do is
*query with the sentinel as the predicate*.

The degrading sites matter because the value they receive is not trustworthy on
its own: `CaseService.create_case` stamps `Case.organization_id` from the *total*
`get_current_org_id`, so a case written from a context that never bound a tenant
carries the sentinel — and `organization_id` is `str`/`min_length=1`, so that is
a perfectly valid row. Every reader that turns it into a predicate resolves it
through `usable_tenant_id` first.

That distinction is why the contextvar has two readers. `get_current_org_id` is
total — its default *is* the sentinel — so it can never be the subject of a
fail-closed guard; `if not get_current_org_id()` is unreachable code. Anywhere
the value becomes a query predicate, read `get_current_tenant_id`, which applies
`usable_tenant_id` first. An execution context that never bound a tenant (a
background task that did not inherit the request context) reads as the sentinel,
and under multi the sentinel is not a tenant.

*Rejected alternative: scoping the trusted load itself — it backs the write-policy
check and internal ingestion, neither of which has an actor to scope by.*

### Admin-Only Endpoints

Publishing into the Global KB requires the `platform_admin` role:

| Method | Endpoint | Description | Required Role |
|--------|----------|-------------|---------------|
| POST | `/api/v1/knowledge/documents` | Upload document to Global KB | `platform_admin` |

### Ownership-Aware Document Writes

These endpoints authenticate any user and then apply the per-document write
policy (`modules/knowledge/domain/document_write.py`): the owner may write
their own personal/team runbook; a non-owner only as the single-tenant
platform operator; global-scope writes follow the global-tier authoring policy
(operator, single-tenant only). The bulk routes run the same policy once per
target and pass only permitted ids to the service — refused targets come back
in `errors`, never counted as updated/deleted. A refusal over a document the
caller cannot even *see* answers 404, identically to an absent id, so the
response is never an existence oracle.

Bulk batches are de-duplicated (first-seen order) and capped at
`MAX_BULK_DOCUMENT_IDS` = 200; a larger batch is refused with 400. The gate does
per-target database work, and repeating an id would otherwise both amplify that
work and time the difference between an invisible target and an absent one
despite their identical messages. Per-target `errors` carry no exception text.

| Method | Endpoint | Description | Required Role |
|--------|----------|-------------|---------------|
| PUT | `/api/v1/knowledge/documents/{id}` | Update a document | Owner, or platform operator |
| DELETE | `/api/v1/knowledge/documents/{id}` | Delete a document | Owner, or platform operator |
| POST | `/api/v1/knowledge/documents/bulk-update` | Bulk update documents | Per target, as above |
| POST | `/api/v1/knowledge/documents/bulk-delete` | Bulk delete documents | Per target, as above |

### Suggestion Review

Knowledge suggestions extracted from cases. Every route requires
`platform_admin` **and** resolves inside the caller's organization per
[Tenant-Scoped Resolution](#tenant-scoped-resolution): an id belonging to
another tenant answers 404 and nothing is written. Approval additionally
publishes at global scope, so it carries the global-tier authoring gate.

| Method | Endpoint | Description | Required Role |
|--------|----------|-------------|---------------|
| GET | `/api/v1/knowledge/suggestions` | List the caller's org's suggestions | `platform_admin`, own org only |
| GET | `/api/v1/knowledge/suggestions/{id}` | Suggestion detail | `platform_admin`; 404 if out of tenant |
| PUT | `/api/v1/knowledge/suggestions/{id}` | Edit before approval | `platform_admin`; 404 if out of tenant |
| POST | `/api/v1/knowledge/suggestions/{id}/approve` | Approve → knowledge item | `platform_admin` + global-authoring gate; 404 if out of tenant |
| POST | `/api/v1/knowledge/suggestions/{id}/reject` | Reject with reason | `platform_admin`; 404 if out of tenant |
| POST | `/api/v1/knowledge/suggestions/{id}/remediate-pii` | Mark PII remediated | `platform_admin`; 404 if out of tenant |

### Public Endpoints (All Authenticated Users)

These endpoints are accessible to every authenticated user, whatever their roles:

The id-addressed reads resolve their target through the read-visibility rule
(global ∪ own ∪ shared-to-my-teams) **and** require the row to be published or
owned by the caller: deleting a built-in global runbook is implemented as an
unpublish, so an unpublished row is a deleted one and must not stay readable by
id. An author still reaches their own unpublished draft.

| Method | Endpoint | Description | Required Role |
|--------|----------|-------------|---------------|
| POST | `/api/v1/knowledge/search` | Search Global KB | Any authenticated user |
| GET | `/api/v1/knowledge/documents` | List KB documents | Optional auth (anonymous sees global only) |
| GET | `/api/v1/knowledge/documents/{id}` | Get specific document | Any authenticated user; 404 if not visible |
| GET | `/api/v1/knowledge/documents/{id}/snippet` | Document snippet (hover card) | Any authenticated user; 404 if not visible |
| GET | `/api/v1/knowledge/stats` | Get KB statistics | Any authenticated user |
| GET | `/api/v1/knowledge/analytics/search` | Search analytics | Any authenticated user |
| POST | `/api/v1/users/{user_id}/kb/documents` | Upload to User KB | Owner or platform admin |
| GET | `/api/v1/users/{user_id}/kb/documents` | List User KB documents | Owner or platform admin |
| DELETE | `/api/v1/users/{user_id}/kb/documents/{id}` | Delete from User KB | Owner or platform admin |

---

## User Management

### Using Management Scripts

FaultMaven provides CLI tools for user management in two places. Operator
procedures — promotion, demotion, service-account and tenant provisioning —
are `fm-*` console entrypoints that ship with the installed package
(`faultmaven/cli/`), so they run in the API pod as well as in a checkout.
Account-creation and listing conveniences (`create_user.py`, `list_users.py`)
stay in `scripts/auth/` and are run from a checkout.

#### 1. List Users

**View all users with their roles:**
```bash
python scripts/auth/list_users.py
```

**Example Output:**
```
================================================================================
FaultMaven User Accounts
================================================================================

Found 5 user(s):

#    USERNAME             EMAIL                          ROLES                USER_ID
----------------------------------------------------------------------------------------------------
👑 1    admin@company.com    admin@company.com   user, admin, platform_admin   860e6629-1e12-4921...
   2    alice                alice@company.com              user                 225bae2f-f459-4a54...
   3    bob                  bob@company.com                user                 3a94f837-013e-4538...

================================================================================
Total: 3 user(s)
  Admins: 1
  Regular users: 2
================================================================================
```

👑 = Admin user

#### 2. Create User

**Interactive mode (prompts for all values):**
```bash
python scripts/auth/create_user.py --interactive
```

**Command-line mode:**
```bash
# Create regular user
python scripts/auth/create_user.py --username alice --role user

# Create an organization admin (tenant-bounded)
python scripts/auth/create_user.py --username bob --role admin

# Create a platform admin (deployment operator)
python scripts/auth/create_user.py --username carol --role platform_admin

# With custom email and display name
python scripts/auth/create_user.py \
  --username charlie \
  --email charlie@company.com \
  --display-name "Charlie Brown" \
  --role user
```

**Output:**
```
✅ User created successfully!

User Details:
  User ID: 657f11b1-5b97-4882-9801-a5bed1c3830c
  Username: alice
  Email: alice@company.com
  Display Name: Alice
  Roles: ['user']
  Created: 2025-10-23 12:34:56+00:00
```

#### 3. Promote to Admin

**Grant the operator role to an existing user:**
```bash
fm-promote-platform-admin alice
```

**Output:**
```
✅ User promoted to platform admin successfully!

Updated roles: ['user', 'admin', 'platform_admin']

User 'alice' can now:
  ✅ Upload documents to Global KB
  ✅ Update Global KB documents
  ✅ Delete Global KB documents
  ✅ Perform bulk operations on Global KB
```

#### 4. Demote from Admin

**Revoke the operator role from a user:**
```bash
fm-demote-platform-admin bob
```

**Output:**
```
✅ Admin role removed successfully!

Updated roles: ['user']

User 'bob' can no longer:
  ❌ Upload documents to Global KB
  ❌ Update Global KB documents
  ❌ Delete Global KB documents
```

### Using the API

#### Register New User
```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice", "email": "alice@company.com"}'
```

**Response:**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer",
  "expires_in": 900,
  "refresh_expires_in": 604800,
  "session_id": "session-41afd36b...",
  "user": {
    "user_id": "user-123",
    "username": "alice",
    "email": "alice@dev.faultmaven.local",
    "display_name": "Alice",
    "roles": ["user"],
    "auth_mode": "local",
    "created_at": "2025-10-23T12:00:00Z"
  }
}
```

#### Login (Verify Roles)
```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice", "password": "optional-password"}'
```

**Response includes roles:**
```json
{
  "user": {
    "roles": ["user", "admin", "platform_admin"]
  }
}
```

#### Get Current User Profile
```bash
curl http://localhost:8000/api/v1/auth/me \
  -H 'Authorization: Bearer YOUR_TOKEN'
```

**Response:**
```json
{
  "user_id": "user-123",
  "username": "alice",
  "email": "alice@company.com",
  "display_name": "Alice Smith",
  "roles": ["user", "admin", "platform_admin"],
  "created_at": "2025-10-23T12:00:00Z",
  "last_login": null
}
```

---

## Frontend Integration

### TypeScript Interfaces

```typescript
interface User {
  user_id: string;
  username: string;
  email: string;
  display_name: string;
  roles: string[];
  created_at: string;
}

interface AuthResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  session_id: string;
  user: User;
}
```

### Role Checking Utilities

```typescript
class RoleChecker {
  /**
   * Check if user has a specific role
   */
  hasRole(user: User, role: string): boolean {
    return user.roles?.includes(role) ?? false;
  }

  /**
   * Check if user holds the platform_admin operator role
   */
  isAdmin(user: User): boolean {
    return this.hasRole(user, 'platform_admin');
  }

  /**
   * Check if user can manage Global KB
   */
  canManageGlobalKB(user: User): boolean {
    return this.isAdmin(user);
  }

  /**
   * Check if user has any of the specified roles
   */
  hasAnyRole(user: User, roles: string[]): boolean {
    return roles.some(role => this.hasRole(user, role));
  }
}

// Usage
const roleChecker = new RoleChecker();
const canUpload = roleChecker.canManageGlobalKB(user);
```

### Conditional UI Rendering

```typescript
import { useState, useEffect } from 'react';

const KnowledgeBasePanel = () => {
  const [user, setUser] = useState<User | null>(null);
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    // Fetch current user
    fetchCurrentUser().then(userData => {
      setUser(userData);
      setIsAdmin(userData.roles.includes('platform_admin'));
    });
  }, []);

  if (!user) return <div>Loading...</div>;

  return (
    <div className="kb-panel">
      <h1>Knowledge Base</h1>

      {/* Admin-only section */}
      {isAdmin && (
        <div className="admin-section">
          <h2>Global KB Management</h2>
          <button onClick={handleUploadToGlobal}>
            Upload to Global KB
          </button>
          <button onClick={handleManageGlobal}>
            Manage Global KB
          </button>
        </div>
      )}

      {/* Available to all users */}
      <div className="user-section">
        <h2>Search Knowledge Base</h2>
        <SearchComponent />
      </div>

      <div className="user-section">
        <h2>My Knowledge Base</h2>
        <button onClick={handleUploadToUser}>
          Upload to My KB
        </button>
        <UserKBList userId={user.user_id} />
      </div>
    </div>
  );
};
```

### React Hook for Roles

```typescript
import { useEffect, useState } from 'react';

interface UseRolesReturn {
  user: User | null;
  isAdmin: boolean;
  hasRole: (role: string) => boolean;
  loading: boolean;
}

export function useRoles(): UseRolesReturn {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Load user from auth state
    const loadUser = async () => {
      try {
        const response = await fetch('/api/v1/auth/me', {
          headers: {
            'Authorization': `Bearer ${getStoredToken()}`
          }
        });

        if (response.ok) {
          const userData = await response.json();
          setUser(userData);
        }
      } catch (error) {
        console.error('Failed to load user:', error);
      } finally {
        setLoading(false);
      }
    };

    loadUser();
  }, []);

  const hasRole = (role: string) => {
    return user?.roles?.includes(role) ?? false;
  };

  const isAdmin = user?.roles?.includes('platform_admin') ?? false;

  return { user, isAdmin, hasRole, loading };
}

// Usage
const MyComponent = () => {
  const { user, isAdmin, hasRole, loading } = useRoles();

  if (loading) return <div>Loading...</div>;

  return (
    <div>
      <p>Welcome, {user?.display_name}</p>
      {isAdmin && <AdminPanel />}
      {hasRole('editor') && <EditorPanel />}
    </div>
  );
};
```

---

## Security

### Server-Side Validation

**All role checks are enforced server-side**. Frontend role checking is only for UI rendering convenience.

```python
# Backend enforcement
from faultmaven.api.v1.auth_dependencies import require_platform_admin

@router.post("/knowledge/documents")
async def upload_document(
    file: UploadFile,
    current_user: DevUser = Depends(require_platform_admin)  # Enforced server-side
):
    """This endpoint is protected - platform admins only"""
    ...
```

### Authorization Flow

```mermaid
sequenceDiagram
    participant Frontend
    participant API
    participant Auth
    participant KB Service

    Frontend->>API: POST /knowledge/documents (+ Bearer token)
    API->>Auth: Validate token
    Auth-->>API: User (with roles)
    API->>API: Check if 'platform_admin' in user.roles
    alt Is Admin
        API->>KB Service: Upload document
        KB Service-->>API: Success
        API-->>Frontend: 201 Created
    else Not Admin
        API-->>Frontend: 403 Forbidden
    end
```

### Error Responses

#### 401 Unauthorized (No Token / Invalid Token)
```json
{
  "detail": "Not authenticated"
}
```

#### 403 Forbidden (Valid Token, Insufficient Permissions)
```json
{
  "error": "Forbidden",
  "message": "This operation requires platform administrator privileges",
  "required_role": "admin",
  "user_roles": ["user"]
}
```

### Security Best Practices

1. **Never trust frontend role checks** for security decisions
2. **Always validate roles** on the backend for protected operations
3. **Log all admin operations** with user ID for audit trails
4. **Roles require new token** - role changes need re-authentication
5. **Use HTTPS** in production to protect token transmission

### Audit Logging

All admin operations are logged:

```python
logger.warning(
    f"Authorization denied: User {user.user_id} ({user.username}) "
    f"attempted admin-only operation without admin role (roles: {user.roles})"
)

logger.info(f"Admin operation: User {user.user_id} uploaded document to Global KB")
```

---

## Testing

### Automated Testing

**Run RBAC tests:**
```bash
python scripts/test_rbac.py
```

**Expected output:**
```
✅ Regular user has 'user' role: True
✅ Regular user has 'admin' role: False
✅ Admin user has 'user' role: True
✅ Admin user has 'admin' role: True
✅ All role checks passed!
```

### Manual API Testing

**1. Create test users:**
```bash
python scripts/auth/create_user.py --username testuser --role user
python scripts/auth/create_user.py --username testadmin --role admin
```

**2. Login as regular user:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "testuser"}' | jq
```

**3. Try to upload as regular user (should fail):**
```bash
curl -X POST http://localhost:8000/api/v1/knowledge/documents \
  -H 'Authorization: Bearer REGULAR_USER_TOKEN' \
  -F 'file=@test.txt' \
  -F 'title=Test' \
  -F 'document_type=reference'

# Expected: 403 Forbidden
```

**4. Login as admin:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "testadmin"}' | jq
```

**5. Upload as admin (should succeed):**
```bash
curl -X POST http://localhost:8000/api/v1/knowledge/documents \
  -H 'Authorization: Bearer ADMIN_TOKEN' \
  -F 'file=@test.txt' \
  -F 'title=Test' \
  -F 'document_type=reference'

# Expected: 201 Created
```

---

## Troubleshooting

### User can't access admin endpoints

**Symptoms:**
- User gets 403 Forbidden on Global KB operations
- User believes they should have admin access

**Diagnosis:**
```bash
# Check user's current roles
python scripts/auth/list_users.py | grep username

# Expected output shows roles
👑 1    username    email@company.com    admin    user-id
```

**Solution:**
```bash
# Promote user to admin
fm-promote-platform-admin username

# User must re-login to get updated roles in token
```

### Roles not appearing in API response

**Symptoms:**
- Login response doesn't include `roles` field
- Frontend can't check user roles

**Diagnosis:**
- Check API response models include `roles` field
- Verify backend returns roles in UserProfile

**Solution:**
- Ensure using updated `faultmaven/models/api_auth.py`
- Verify `UserProfile` model has `roles: List[str]` field

### Frontend shows admin UI to non-admins

**Symptoms:**
- Regular users see admin buttons
- But get 403 when clicking them

**Diagnosis:**
- Frontend role check logic may be incorrect
- Token may be stale with old roles

**Solution:**
```typescript
// Correct role checking
const isAdmin = user?.roles?.includes('platform_admin') ?? false;

// Force token refresh
await authManager.refreshToken();
```

---

## Summary

- **Account Role Model**: `user` and `admin` account roles for Global-KB access (a separate org-scoped `Role` enum — `admin`/`member`/`viewer` — exists for organization/team RBAC; see the note under [User Roles](#user-roles))
- **Server-Side Enforcement**: All security checks on backend
- **User Management Tools**: CLI scripts for easy administration
- **Frontend Integration**: Roles included in login response
- **Well Tested**: Comprehensive test coverage

For more details, see:
- [IAM Design](./iam-design.md)
- [User Management Scripts](../../../scripts/auth/README.md)
