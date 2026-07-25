# User Management Scripts

This directory contains utilities for managing user accounts and roles in FaultMaven.

## Quick Reference

```bash
# List all users
python scripts/auth/list_users.py

# Create a new regular user
python scripts/auth/create_user.py --username alice --role user

# Create a new organization admin (tenant-bounded)
python scripts/auth/create_user.py --username bob --role admin

# Create a new platform admin (deployment operator, cross-tenant)
python scripts/auth/create_user.py --username carol --role platform_admin

# Promote an existing user to platform admin
python scripts/auth/promote_to_platform_admin.py alice

# Demote a platform admin back to a regular user
python scripts/auth/demote_from_platform_admin.py bob

# Mint the Slack service account an OAuth refresh-token credential
# (AUTH_MODE=oauth only — see docs/operations/security/service-account-credentials.md)
python scripts/auth/provision_service_account.py --username slack-agent
```

---

## Available Scripts

### 1. `list_users.py` - View All Users

Lists all registered users with their roles and details.

**Usage:**
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
👑 1    admin@faultmaven.ai  admin@faultmaven.ai            admin                860e6629-1e12-4921-ac6a...
   2    alice                alice@dev.faultmaven.local     user                 225bae2f-f459-4a54-9c08...
   3    bob                  bob@dev.faultmaven.local       user                 3a94f837-013e-4538-a80c...

================================================================================
Total: 3 user(s)
  Admins: 1
  Regular users: 2
================================================================================
```

👑 = Admin user

---

### 2. `create_user.py` - Create New User

Creates a new user account with specified role.

**Interactive Mode:**
```bash
python scripts/auth/create_user.py --interactive
```

**Command-Line Mode:**
```bash
# Create regular user (default)
python scripts/auth/create_user.py --username alice

# Create admin user
python scripts/auth/create_user.py --username bob --role admin

# With custom email and display name
python scripts/auth/create_user.py \
  --username charlie \
  --email charlie@company.com \
  --display-name "Charlie Brown" \
  --role user
```

**Options:**
- `--username, -u`: Username (required)
- `--email, -e`: Email address (optional, auto-generated if not provided)
- `--display-name, -d`: Display name (optional, auto-generated if not provided)
- `--role, -r`: User role - `user`, `admin` (org-scoped), or `platform_admin` (deployment operator). Default: `user`
- `--interactive, -i`: Interactive mode (prompts for all values)

**Example:**
```bash
$ python scripts/auth/create_user.py --username alice --role user

================================================================================
Create New User Account
================================================================================

Checking if user 'alice' exists...

Creating user 'alice'...
✅ User created successfully!

User Details:
  User ID: 225bae2f-f459-4a54-9c08-2da5c2b3a961
  Username: alice
  Email: alice@dev.faultmaven.local
  Display Name: Alice
  Roles: ['user']
  Created: 2025-10-23 12:34:56+00:00
```

---

### 3. `promote_to_platform_admin.py` - Promote User to Platform Admin

Grants the operator role set (`user` + `admin` + `platform_admin`) to an existing
user. `platform_admin` is the DEPLOYMENT-scoped role (ADR-012 D9) that carries
cross-tenant reach; the org-scoped `admin` is granted alongside it because an
operator also needs authority inside its own organization.

**Usage:**
```bash
python scripts/auth/promote_to_platform_admin.py <username>
```

**Example:**
```bash
$ python scripts/auth/promote_to_platform_admin.py alice

================================================================================
Promote User to Platform Admin
================================================================================

Looking up user 'alice'...
✅ Found user: 225bae2f-f459-4a54-9c08-2da5c2b3a961
   Email: alice@dev.faultmaven.local
   Current roles: ['user']

Granting operator roles ['admin', 'platform_admin'] to user 'alice'...
✅ User promoted to platform admin successfully!

Updated roles: ['user', 'admin', 'platform_admin']

User 'alice' can now:
  ✅ List cases across all users and organizations
  ✅ Administer user accounts
  ✅ View and change LLM configuration
  ✅ Manage the Global KB (upload, update, delete, bulk ops)
```

---

### 4. `demote_from_platform_admin.py` - Demote Platform Admin to Regular User

Removes the `platform_admin` role from a user account, revoking cross-tenant
reach. The organization-scoped `admin` role is deliberately left in place —
withdrawing operator status should not also strip authority inside the user's
own organization. Remove that separately if you mean to.

**Usage:**
```bash
python scripts/auth/demote_from_platform_admin.py <username>
```

**Example:**
```bash
$ python scripts/auth/demote_from_platform_admin.py bob

================================================================================
Demote Platform Admin to Regular User
================================================================================

Looking up user 'bob'...
✅ Found user: 3a94f837-013e-4538-a80c-07eacc5612ef
   Email: bob@dev.faultmaven.local
   Current roles: ['user', 'admin', 'platform_admin']

Removing 'platform_admin' role from user 'bob'...
✅ Platform admin role removed successfully!

Updated roles: ['user', 'admin']

User 'bob' can no longer:
  ❌ List cases across all users and organizations
  ❌ Administer user accounts
  ❌ View or change LLM configuration
  ❌ Manage the Global KB (upload, update, delete, bulk ops)

User 'bob' can still:
  ✅ Search Global KB
  ✅ Manage their own User KB
```

---

## User Roles Explained

### Regular User (`user` role)
**Can:**
- ✅ Login and authenticate
- ✅ Search Global KB (read-only)
- ✅ List Global KB documents (read-only)
- ✅ Upload to their own User KB
- ✅ Manage their own User KB documents
- ✅ Use all troubleshooting features

**Cannot:**
- ❌ Upload to Global KB
- ❌ Modify Global KB documents
- ❌ Delete Global KB documents

### Organization Admin (`user` + `admin` roles)
Tenant-bounded: full authority inside ONE organization, none outside it.
Assignable through the user-management API (`POST /admin/users/{id}/roles`).

### Platform Admin (`user` + `admin` + `platform_admin` roles)
The DEPLOYMENT operator (ADR-012 D9). Can do everything above, PLUS:
- ✅ List cases across all users and organizations (`GET /admin/cases`)
- ✅ Administer user accounts
- ✅ View and change LLM configuration
- ✅ Manage the Global KB (upload, update, delete, bulk ops)

`platform_admin` is deliberately absent from the org `Role` enum, so it grants
no org permissions on its own — and the user-management API cannot mint one.
Grant it only with `promote_to_platform_admin.py`. The standalone deployment's
seeded account holds all three roles: it is both its org's admin and the
operator.

---

## API Usage

### Register a New User (API)
```bash
curl -X POST http://localhost:8090/api/v1/auth/dev-register \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice"}'
```

**Response:**
```json
{
  "access_token": "abc-123...",
  "token_type": "bearer",
  "expires_in": 86400,
  "session_id": "session-xyz...",
  "user": {
    "user_id": "225bae2f-f459-4a54-9c08...",
    "username": "alice",
    "email": "alice@dev.faultmaven.local",
    "display_name": "Alice",
    "roles": ["user"],
    "is_dev_user": true,
    "created_at": "2025-10-23T12:34:56Z"
  }
}
```

### Login (API)
```bash
curl -X POST http://localhost:8090/api/v1/auth/dev-login \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice"}'
```

### Get Current User Profile (API)
```bash
curl http://localhost:8090/api/v1/auth/me \
  -H 'Authorization: Bearer YOUR_TOKEN'
```

**Response:**
```json
{
  "user_id": "225bae2f-f459-4a54-9c08...",
  "username": "alice",
  "email": "alice@dev.faultmaven.local",
  "display_name": "Alice",
  "roles": ["user"],
  "is_dev_user": true,
  "created_at": "2025-10-23T12:34:56Z",
  "last_login": null,
  "token_count": 1
}
```

---

## Storage Details

**Where are users stored?**
- Users are stored in Redis (not hard-coded)
- Redis keys: `auth:user:{user_id}`, `auth:username:{username}`, `auth:email:{email}`
- User data includes roles, which are persisted in Redis

**Default behavior:**
- New users created via API default to `['user']` role (since `DevUser.__post_init__()` sets it)
- Use `promote_to_platform_admin.py` to grant operator privileges

**Data persistence:**
- Users persist across server restarts (stored in Redis)
- Tokens expire after 24 hours
- Redis data persists according to Redis configuration

---

## Common Workflows

### Initial Setup - Create First Admin
```bash
# 1. Create an admin user
python scripts/auth/create_user.py --username admin --role admin

# 2. Login to get token
curl -X POST http://localhost:8090/api/v1/auth/dev-login \
  -H 'Content-Type: application/json' \
  -d '{"username": "admin"}'

# 3. Use token for admin operations
```

### Onboard New Team Member
```bash
# 1. Create regular user account
python scripts/auth/create_user.py --username newuser --role user

# 2. Send them login instructions
# 3. If they need admin access later:
python scripts/auth/promote_to_platform_admin.py newuser
```

### Audit User Accounts
```bash
# List all users and their roles
python scripts/auth/list_users.py

# Check specific user
python scripts/auth/list_users.py | grep alice
```

### Revoke Admin Access
```bash
# Demote user back to regular user
python scripts/auth/demote_from_platform_admin.py username
```

---

## Troubleshooting

### "User not found"
- Check username spelling
- Run `python scripts/auth/list_users.py` to see all users
- Usernames are case-sensitive and stored in lowercase

### "User already exists"
- Usernames and emails must be unique
- Use different username or email
- Check existing users with `list_users.py`

### "Failed to get user store from container"
- Ensure Redis is running
- Check Redis connection settings in `.env`:
  - `REDIS_HOST=192.168.0.111`
  - `REDIS_PORT=30379`
- Run with `SKIP_SERVICE_CHECKS=true` for local development

### No users in the system
- Users are created on-demand (not pre-seeded)
- Create first user with `create_user.py`
- Or use API registration endpoint

---

## Security Notes

1. **Development Environment Only**: These scripts are for development. In production:
   - Use OAuth2/OIDC providers
   - Implement proper password hashing
   - Add multi-factor authentication
   - Use secure token management

2. **Token Security**:
   - Tokens are stored as SHA-256 hashes
   - Tokens expire after 24 hours
   - Never log tokens in production

3. **Role Changes**:
   - Role changes take effect immediately
   - Users must re-login to get updated roles in new tokens
   - Existing tokens retain old roles until expiration

4. **Admin Access**:
   - Audit admin accounts regularly
   - Follow principle of least privilege
   - Document who has admin access and why

---

## Next Steps

After creating users:
1. Start the server: `./run_faultmaven.sh`
2. Test authentication with the API
3. Verify role-based access control works
4. See [Role-Based Access Control](../../docs/rbac.md) for more details

For questions or issues, see the main FaultMaven documentation.
