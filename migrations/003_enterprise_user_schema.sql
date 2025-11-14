-- Schema Extension: 003 - Enterprise User & Teams Infrastructure
-- Date: 2025-01-14
-- Description: Implement enterprise SaaS multi-tenancy with teams and RBAC
--              - organizations (workspaces/tenants)
--              - organization_members (user-org mapping with roles)
--              - teams (sub-organization collaboration groups)
--              - team_members (user-team mapping)
--              - roles (RBAC role definitions)
--              - permissions (RBAC permission definitions)
--              - role_permissions (role-permission mapping)
--              - user_audit_log (security audit trail)
--
-- Design Reference: docs/architecture/user-storage-design.md (Full Enterprise Schema)
-- Resolves: Design gaps for team collaboration and organization-wide sharing

-- ============================================================================
-- ENUMS
-- ============================================================================

-- Organization plan tiers
CREATE TYPE org_plan_tier AS ENUM (
    'free',
    'pro',
    'enterprise'
);

-- User audit event types
CREATE TYPE audit_event_type AS ENUM (
    'login',
    'logout',
    'login_failed',
    'password_changed',
    'email_verified',
    'account_created',
    'account_deleted',
    'role_assigned',
    'role_removed',
    'permission_granted',
    'permission_revoked',
    'case_created',
    'case_viewed',
    'case_updated',
    'case_deleted',
    'case_shared',
    'kb_document_uploaded',
    'kb_document_viewed',
    'kb_document_shared',
    'org_created',
    'org_settings_changed',
    'team_created',
    'team_member_added',
    'team_member_removed'
);

-- Audit event categories
CREATE TYPE audit_category AS ENUM (
    'authentication',
    'authorization',
    'data_access',
    'administration',
    'security'
);

COMMENT ON TYPE org_plan_tier IS 'Organization subscription plan levels';
COMMENT ON TYPE audit_event_type IS 'Specific user action types for audit trail';
COMMENT ON TYPE audit_category IS 'High-level categorization of audit events';

-- ============================================================================
-- TABLE: organizations
-- ============================================================================

CREATE TABLE organizations (
    -- Primary Key
    org_id VARCHAR(20) PRIMARY KEY DEFAULT ('org_' || substr(gen_random_uuid()::text, 1, 17)),

    -- Organization Info
    name VARCHAR(200) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,  -- URL-friendly identifier (e.g., 'acme-corp')
    description TEXT,

    -- Subscription
    plan_tier org_plan_tier NOT NULL DEFAULT 'free',
    max_members INTEGER NOT NULL DEFAULT 5,  -- Based on plan
    max_cases INTEGER,  -- NULL = unlimited

    -- Settings
    settings JSONB DEFAULT '{}'::jsonb,

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Soft Delete
    deleted_at TIMESTAMPTZ,

    -- Constraints
    CONSTRAINT organizations_name_not_empty CHECK (LENGTH(TRIM(name)) > 0),
    CONSTRAINT organizations_slug_format CHECK (slug ~ '^[a-z0-9-]+$'),
    CONSTRAINT organizations_max_members_positive CHECK (max_members > 0)
);

-- Indexes
CREATE INDEX idx_organizations_slug ON organizations(slug) WHERE deleted_at IS NULL;
CREATE INDEX idx_organizations_plan_tier ON organizations(plan_tier);
CREATE INDEX idx_organizations_created_at ON organizations(created_at DESC);
CREATE INDEX idx_organizations_settings_gin ON organizations USING GIN (settings);

-- Auto-update updated_at
CREATE TRIGGER organizations_update_updated_at
    BEFORE UPDATE ON organizations
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE organizations IS 'Multi-tenant workspaces for enterprise SaaS';
COMMENT ON COLUMN organizations.slug IS 'URL-friendly identifier for organization (lowercase, hyphens only)';
COMMENT ON COLUMN organizations.max_members IS 'Maximum members allowed based on plan tier';

-- ============================================================================
-- TABLE: organization_members
-- ============================================================================

CREATE TABLE organization_members (
    -- Composite Primary Key
    user_id VARCHAR(20) NOT NULL,
    org_id VARCHAR(20) NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, org_id),

    -- Role within organization
    role_id VARCHAR(20) NOT NULL,  -- References roles.role_id (added later)

    -- Timestamps
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ,

    -- Constraints
    CONSTRAINT organization_members_role_not_empty CHECK (LENGTH(TRIM(role_id)) > 0)
);

-- Indexes
CREATE INDEX idx_organization_members_org_id ON organization_members(org_id);
CREATE INDEX idx_organization_members_user_id ON organization_members(user_id);
CREATE INDEX idx_organization_members_role_id ON organization_members(role_id);
CREATE INDEX idx_organization_members_joined_at ON organization_members(joined_at DESC);

COMMENT ON TABLE organization_members IS 'User membership in organizations with role assignments';
COMMENT ON COLUMN organization_members.role_id IS 'Organization-level role (e.g., owner, admin, member, viewer)';

-- ============================================================================
-- TABLE: teams
-- ============================================================================

CREATE TABLE teams (
    -- Primary Key
    team_id VARCHAR(20) PRIMARY KEY DEFAULT ('team_' || substr(gen_random_uuid()::text, 1, 17)),

    -- Parent Organization
    org_id VARCHAR(20) NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,

    -- Team Info
    name VARCHAR(200) NOT NULL,
    description TEXT,

    -- Settings
    settings JSONB DEFAULT '{}'::jsonb,

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Soft Delete
    deleted_at TIMESTAMPTZ,

    -- Constraints
    CONSTRAINT teams_name_not_empty CHECK (LENGTH(TRIM(name)) > 0)
);

-- Indexes
CREATE INDEX idx_teams_org_id ON teams(org_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_teams_created_at ON teams(created_at DESC);
CREATE INDEX idx_teams_settings_gin ON teams USING GIN (settings);

-- Auto-update updated_at
CREATE TRIGGER teams_update_updated_at
    BEFORE UPDATE ON teams
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE teams IS 'Sub-organization groups for collaboration (e.g., SRE Team, Backend Team)';

-- ============================================================================
-- TABLE: team_members
-- ============================================================================

CREATE TABLE team_members (
    -- Composite Primary Key
    user_id VARCHAR(20) NOT NULL,
    team_id VARCHAR(20) NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, team_id),

    -- Optional team-specific role
    team_role VARCHAR(20),  -- 'lead', 'member', or custom

    -- Timestamps
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_team_members_team_id ON team_members(team_id);
CREATE INDEX idx_team_members_user_id ON team_members(user_id);
CREATE INDEX idx_team_members_team_role ON team_members(team_role);

COMMENT ON TABLE team_members IS 'User membership in teams within organizations';
COMMENT ON COLUMN team_members.team_role IS 'Optional team-specific role (e.g., team_lead, team_member)';

-- ============================================================================
-- TABLE: roles
-- ============================================================================

CREATE TABLE roles (
    -- Primary Key
    role_id VARCHAR(20) PRIMARY KEY,

    -- Role Definition
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    scope VARCHAR(20) NOT NULL DEFAULT 'organization',  -- 'system', 'organization', 'team'

    -- Built-in vs Custom
    is_system_role BOOLEAN NOT NULL DEFAULT false,

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Constraints
    CONSTRAINT roles_name_not_empty CHECK (LENGTH(TRIM(name)) > 0),
    CONSTRAINT roles_scope_valid CHECK (scope IN ('system', 'organization', 'team'))
);

-- Indexes
CREATE INDEX idx_roles_scope ON roles(scope);
CREATE INDEX idx_roles_is_system_role ON roles(is_system_role);

COMMENT ON TABLE roles IS 'Role definitions for RBAC system';
COMMENT ON COLUMN roles.is_system_role IS 'System roles cannot be deleted or modified by users';
COMMENT ON COLUMN roles.scope IS 'Role scope: system (platform-wide), organization (workspace), team (sub-group)';

-- Seed System Roles
INSERT INTO roles (role_id, name, description, scope, is_system_role) VALUES
-- System-level roles (FaultMaven platform staff)
('role_super_admin', 'Super Admin', 'Full system access (FaultMaven staff only)', 'system', true),

-- Organization-level roles
('role_org_owner', 'Owner', 'Organization owner with full admin rights', 'organization', true),
('role_org_admin', 'Admin', 'Organization administrator - manage users, teams, settings', 'organization', true),
('role_org_member', 'Member', 'Standard organization member - create/edit cases and KB', 'organization', true),
('role_org_viewer', 'Viewer', 'Read-only organization access', 'organization', true),

-- Team-level roles
('role_team_lead', 'Team Lead', 'Team leader with management rights', 'team', true),
('role_team_member', 'Team Member', 'Standard team member', 'team', true);

-- ============================================================================
-- TABLE: permissions
-- ============================================================================

CREATE TABLE permissions (
    -- Primary Key
    permission_id VARCHAR(30) PRIMARY KEY,

    -- Permission Definition
    resource VARCHAR(50) NOT NULL,  -- 'cases', 'knowledge_base', 'teams', 'organization', 'users'
    action VARCHAR(50) NOT NULL,    -- 'read', 'write', 'delete', 'manage'
    description TEXT,

    -- Unique permission per resource+action
    UNIQUE (resource, action)
);

COMMENT ON TABLE permissions IS 'Permission definitions for RBAC system';
COMMENT ON COLUMN permissions.resource IS 'Protected resource type (cases, knowledge_base, teams, etc.)';
COMMENT ON COLUMN permissions.action IS 'Action allowed on resource (read, write, delete, manage)';

-- Seed Core Permissions
INSERT INTO permissions (permission_id, resource, action, description) VALUES
-- Cases
('perm_cases_read', 'cases', 'read', 'View cases'),
('perm_cases_write', 'cases', 'write', 'Create and edit cases'),
('perm_cases_delete', 'cases', 'delete', 'Delete cases'),
('perm_cases_manage', 'cases', 'manage', 'Full case management including sharing'),

-- Knowledge Base
('perm_kb_read', 'knowledge_base', 'read', 'View knowledge base documents'),
('perm_kb_write', 'knowledge_base', 'write', 'Upload and edit KB documents'),
('perm_kb_delete', 'knowledge_base', 'delete', 'Delete KB documents'),
('perm_kb_manage', 'knowledge_base', 'manage', 'Full KB management including sharing'),

-- Organization
('perm_org_read', 'organization', 'read', 'View organization info'),
('perm_org_write', 'organization', 'write', 'Edit organization settings'),
('perm_org_manage', 'organization', 'manage', 'Full organization management (billing, deletion)'),

-- Users
('perm_users_read', 'users', 'read', 'View organization users'),
('perm_users_write', 'users', 'write', 'Invite and manage users'),
('perm_users_manage', 'users', 'manage', 'Full user management including role assignment'),

-- Teams
('perm_teams_read', 'teams', 'read', 'View teams'),
('perm_teams_write', 'teams', 'write', 'Create and edit teams'),
('perm_teams_manage', 'teams', 'manage', 'Full team management including member assignment');

-- ============================================================================
-- TABLE: role_permissions
-- ============================================================================

CREATE TABLE role_permissions (
    -- Composite Primary Key
    role_id VARCHAR(20) NOT NULL REFERENCES roles(role_id) ON DELETE CASCADE,
    permission_id VARCHAR(30) NOT NULL REFERENCES permissions(permission_id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_id)
);

-- Indexes
CREATE INDEX idx_role_permissions_role_id ON role_permissions(role_id);
CREATE INDEX idx_role_permissions_permission_id ON role_permissions(permission_id);

COMMENT ON TABLE role_permissions IS 'Role-to-permission mappings for RBAC';

-- Seed Role-Permission Mappings

-- Super Admin: All permissions
INSERT INTO role_permissions (role_id, permission_id)
SELECT 'role_super_admin', permission_id FROM permissions;

-- Organization Owner: Full access to org resources
INSERT INTO role_permissions (role_id, permission_id)
SELECT 'role_org_owner', permission_id FROM permissions
WHERE resource IN ('cases', 'knowledge_base', 'organization', 'users', 'teams');

-- Organization Admin: Most permissions except org management (no billing/deletion)
INSERT INTO role_permissions (role_id, permission_id)
SELECT 'role_org_admin', permission_id FROM permissions
WHERE resource IN ('cases', 'knowledge_base', 'users', 'teams')
   OR (resource = 'organization' AND action IN ('read', 'write'));

-- Organization Member: Read/write cases and KB, read teams/users
INSERT INTO role_permissions (role_id, permission_id)
SELECT 'role_org_member', permission_id FROM permissions
WHERE (resource IN ('cases', 'knowledge_base') AND action IN ('read', 'write'))
   OR (resource IN ('teams', 'users', 'organization') AND action = 'read');

-- Organization Viewer: Read-only access
INSERT INTO role_permissions (role_id, permission_id)
SELECT 'role_org_viewer', permission_id FROM permissions WHERE action = 'read';

-- Team Lead: Manage team, read/write cases and KB
INSERT INTO role_permissions (role_id, permission_id)
SELECT 'role_team_lead', permission_id FROM permissions
WHERE (resource = 'teams' AND action IN ('read', 'write', 'manage'))
   OR (resource IN ('cases', 'knowledge_base') AND action IN ('read', 'write'));

-- Team Member: Read/write cases and KB
INSERT INTO role_permissions (role_id, permission_id)
SELECT 'role_team_member', permission_id FROM permissions
WHERE resource IN ('cases', 'knowledge_base') AND action IN ('read', 'write');

-- ============================================================================
-- TABLE: user_audit_log
-- ============================================================================

CREATE TABLE user_audit_log (
    -- Primary Key
    audit_id BIGSERIAL PRIMARY KEY,

    -- Who and When
    user_id VARCHAR(20) NOT NULL,
    event_type audit_event_type NOT NULL,
    event_category audit_category NOT NULL,

    -- What
    resource_type VARCHAR(50),  -- 'case', 'kb_document', 'user', 'team', 'organization'
    resource_id VARCHAR(50),
    details JSONB,

    -- Context
    ip_address INET,
    user_agent TEXT,
    session_id VARCHAR(50),

    -- Organization Context
    org_id VARCHAR(20),  -- NULL for non-org events

    -- Timeline
    event_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Security
    success BOOLEAN NOT NULL DEFAULT true
);

-- Indexes
CREATE INDEX idx_user_audit_log_user_id ON user_audit_log(user_id);
CREATE INDEX idx_user_audit_log_event_type ON user_audit_log(event_type);
CREATE INDEX idx_user_audit_log_event_category ON user_audit_log(event_category);
CREATE INDEX idx_user_audit_log_resource ON user_audit_log(resource_type, resource_id);
CREATE INDEX idx_user_audit_log_org_id ON user_audit_log(org_id);
CREATE INDEX idx_user_audit_log_event_at ON user_audit_log(event_at DESC);
CREATE INDEX idx_user_audit_log_success ON user_audit_log(success) WHERE success = false;

-- GIN index for details queries
CREATE INDEX idx_user_audit_log_details_gin ON user_audit_log USING GIN (details);

COMMENT ON TABLE user_audit_log IS 'Comprehensive security and compliance audit trail';
COMMENT ON COLUMN user_audit_log.success IS 'Whether the action succeeded (false = failed attempt)';

-- ============================================================================
-- EXTEND EXISTING TABLES
-- ============================================================================

-- Add organization_id to cases table for multi-tenancy
ALTER TABLE cases ADD COLUMN org_id VARCHAR(20) REFERENCES organizations(org_id);
CREATE INDEX idx_cases_org_id ON cases(org_id);

-- Add team_id to cases table for team-based sharing
ALTER TABLE cases ADD COLUMN team_id VARCHAR(20) REFERENCES teams(team_id);
CREATE INDEX idx_cases_team_id ON cases(team_id);

COMMENT ON COLUMN cases.org_id IS 'Organization this case belongs to (multi-tenant isolation)';
COMMENT ON COLUMN cases.team_id IS 'Optional team this case is assigned to (enables team-wide access)';

-- ============================================================================
-- VIEWS
-- ============================================================================

-- View: Organization member summary
CREATE VIEW organization_member_summary AS
SELECT
    o.org_id,
    o.name AS org_name,
    o.plan_tier,
    COUNT(om.user_id) AS member_count,
    COUNT(om.user_id) FILTER (WHERE om.role_id = 'role_org_owner') AS owner_count,
    COUNT(om.user_id) FILTER (WHERE om.role_id = 'role_org_admin') AS admin_count,
    COUNT(om.user_id) FILTER (WHERE om.role_id = 'role_org_member') AS member_count_standard,
    COUNT(om.user_id) FILTER (WHERE om.role_id = 'role_org_viewer') AS viewer_count,
    o.max_members,
    CASE
        WHEN COUNT(om.user_id) >= o.max_members THEN true
        ELSE false
    END AS at_capacity
FROM organizations o
LEFT JOIN organization_members om ON o.org_id = om.org_id
GROUP BY o.org_id, o.name, o.plan_tier, o.max_members;

COMMENT ON VIEW organization_member_summary IS 'Organization membership statistics and capacity status';

-- View: Team member summary
CREATE VIEW team_member_summary AS
SELECT
    t.team_id,
    t.org_id,
    t.name AS team_name,
    COUNT(tm.user_id) AS member_count,
    COUNT(tm.user_id) FILTER (WHERE tm.team_role = 'lead') AS lead_count,
    MAX(tm.joined_at) AS last_member_added_at
FROM teams t
LEFT JOIN team_members tm ON t.team_id = tm.team_id
WHERE t.deleted_at IS NULL
GROUP BY t.team_id, t.org_id, t.name;

COMMENT ON VIEW team_member_summary IS 'Team membership statistics';

-- View: User permissions (for access control checks)
CREATE VIEW user_effective_permissions AS
SELECT DISTINCT
    om.user_id,
    om.org_id,
    p.permission_id,
    p.resource,
    p.action
FROM organization_members om
JOIN role_permissions rp ON rp.role_id = om.role_id
JOIN permissions p ON p.permission_id = rp.permission_id;

COMMENT ON VIEW user_effective_permissions IS 'All effective permissions for users in organizations';

-- ============================================================================
-- FUNCTIONS
-- ============================================================================

-- Function: Check if user has permission in organization
CREATE OR REPLACE FUNCTION user_has_org_permission(
    p_user_id VARCHAR(20),
    p_org_id VARCHAR(20),
    p_permission VARCHAR(50)  -- Format: 'resource.action' e.g., 'cases.write'
)
RETURNS BOOLEAN AS $$
DECLARE
    v_resource VARCHAR(50);
    v_action VARCHAR(50);
BEGIN
    -- Parse permission string
    v_resource := split_part(p_permission, '.', 1);
    v_action := split_part(p_permission, '.', 2);

    RETURN EXISTS (
        SELECT 1
        FROM organization_members om
        JOIN role_permissions rp ON rp.role_id = om.role_id
        JOIN permissions p ON p.permission_id = rp.permission_id
        WHERE om.user_id = p_user_id
        AND om.org_id = p_org_id
        AND p.resource = v_resource
        AND (p.action = v_action OR p.action = 'manage')
    );
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION user_has_org_permission IS 'Check if user has specific permission in organization';

-- Function: Get user role in organization
CREATE OR REPLACE FUNCTION get_user_org_role(
    p_user_id VARCHAR(20),
    p_org_id VARCHAR(20)
)
RETURNS VARCHAR(20) AS $$
DECLARE
    v_role_id VARCHAR(20);
BEGIN
    SELECT role_id INTO v_role_id
    FROM organization_members
    WHERE user_id = p_user_id AND org_id = p_org_id;

    RETURN v_role_id;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_user_org_role IS 'Get user role ID in organization';

-- Function: Check if user is in team
CREATE OR REPLACE FUNCTION user_is_team_member(
    p_user_id VARCHAR(20),
    p_team_id VARCHAR(20)
)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM team_members
        WHERE user_id = p_user_id AND team_id = p_team_id
    );
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION user_is_team_member IS 'Check if user is member of team';

-- Function: Get all user teams in organization
CREATE OR REPLACE FUNCTION get_user_teams(
    p_user_id VARCHAR(20),
    p_org_id VARCHAR(20)
)
RETURNS TABLE (
    team_id VARCHAR(20),
    team_name VARCHAR(200),
    team_role VARCHAR(20),
    joined_at TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT t.team_id, t.name, tm.team_role, tm.joined_at
    FROM teams t
    JOIN team_members tm ON t.team_id = tm.team_id
    WHERE tm.user_id = p_user_id
    AND t.org_id = p_org_id
    AND t.deleted_at IS NULL
    ORDER BY tm.joined_at DESC;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_user_teams IS 'Get all teams user belongs to in organization';

-- ============================================================================
-- ROW-LEVEL SECURITY (RLS) - Examples
-- ============================================================================

-- Enable RLS on cases table
ALTER TABLE cases ENABLE ROW LEVEL SECURITY;

-- Policy: Users can access their own cases
CREATE POLICY cases_owner_access ON cases
    FOR ALL
    USING (user_id = current_setting('app.user_id', true)::VARCHAR);

-- Policy: Users can access cases in their organization
CREATE POLICY cases_org_access ON cases
    FOR SELECT
    USING (
        org_id IN (
            SELECT org_id FROM organization_members
            WHERE user_id = current_setting('app.user_id', true)::VARCHAR
        )
    );

-- Policy: Users can access cases shared with them
CREATE POLICY cases_shared_access ON cases
    FOR SELECT
    USING (
        case_id IN (
            SELECT case_id FROM case_participants
            WHERE user_id = current_setting('app.user_id', true)::VARCHAR
        )
    );

-- Policy: Users can access team cases they belong to
CREATE POLICY cases_team_access ON cases
    FOR SELECT
    USING (
        team_id IN (
            SELECT team_id FROM team_members
            WHERE user_id = current_setting('app.user_id', true)::VARCHAR
        )
    );

COMMENT ON POLICY cases_owner_access ON cases IS 'Users can access their own cases';
COMMENT ON POLICY cases_org_access ON cases IS 'Users can view cases in their organization';
COMMENT ON POLICY cases_shared_access ON cases IS 'Users can view cases shared with them';
COMMENT ON POLICY cases_team_access ON cases IS 'Users can view cases assigned to their teams';

-- ============================================================================
-- GRANTS (adjust based on your user/role setup)
-- ============================================================================

-- Example: Grant privileges to application role
-- GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO faultmaven_app;
-- GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO faultmaven_app;
-- GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO faultmaven_app;

-- ============================================================================
-- VERIFICATION
-- ============================================================================

-- To verify extension:
-- \d organizations
-- \d organization_members
-- \d teams
-- \d team_members
-- \d roles
-- \d permissions
-- \d role_permissions
-- \d user_audit_log

-- Test permission checks:
-- SELECT user_has_org_permission('user_alice', 'org_123', 'cases.write');
-- SELECT get_user_org_role('user_alice', 'org_123');
-- SELECT user_is_team_member('user_alice', 'team_456');
-- SELECT * FROM get_user_teams('user_alice', 'org_123');

-- ============================================================================
-- SCHEMA EXTENSION COMPLETE
-- ============================================================================
