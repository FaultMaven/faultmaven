-- Schema Extension: 002 - Case Sharing Infrastructure
-- Date: 2025-01-14
-- Description: Add case sharing and collaboration features
--              - case_participants table for individual user sharing
--              - Support for role-based access (owner, collaborator, viewer)
--              - Audit trail for sharing actions
--
-- Design Reference: docs/architecture/data-storage-design.md (Section 3.3 - Case Sharing)
-- Resolves: Design gap for sharing cases with specific users

-- ============================================================================
-- ENUMS
-- ============================================================================

-- Participant role enum
CREATE TYPE participant_role AS ENUM (
    'owner',
    'collaborator',
    'viewer'
);

COMMENT ON TYPE participant_role IS 'Role assignments for case participants';

-- ============================================================================
-- TABLE: case_participants
-- ============================================================================

CREATE TABLE case_participants (
    -- Composite Primary Key
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    user_id VARCHAR(20) NOT NULL,
    PRIMARY KEY (case_id, user_id),

    -- Role and Permissions
    role participant_role NOT NULL DEFAULT 'viewer',

    -- Audit Trail
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added_by VARCHAR(20),  -- User who shared the case
    last_accessed_at TIMESTAMPTZ,  -- Track when participant last viewed

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Constraints
    CONSTRAINT case_participants_role_not_null CHECK (role IS NOT NULL)
);

-- Indexes
CREATE INDEX idx_case_participants_case_id ON case_participants(case_id);
CREATE INDEX idx_case_participants_user_id ON case_participants(user_id);
CREATE INDEX idx_case_participants_role ON case_participants(role);
CREATE INDEX idx_case_participants_added_at ON case_participants(added_at DESC);

-- GIN index for metadata queries
CREATE INDEX idx_case_participants_metadata_gin ON case_participants USING GIN (metadata);

COMMENT ON TABLE case_participants IS 'Case sharing with individual users - tracks who has access to each case';
COMMENT ON COLUMN case_participants.role IS 'Access level: owner (full control), collaborator (read/write), viewer (read-only)';
COMMENT ON COLUMN case_participants.added_by IS 'User who shared the case with this participant';
COMMENT ON COLUMN case_participants.last_accessed_at IS 'Track participant engagement for analytics';

-- ============================================================================
-- TABLE: case_sharing_audit
-- ============================================================================

CREATE TABLE case_sharing_audit (
    -- Primary Key
    audit_id SERIAL PRIMARY KEY,

    -- References
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    target_user_id VARCHAR(20) NOT NULL,  -- User being granted/revoked access
    action_by VARCHAR(20) NOT NULL,  -- User performing the action

    -- Action Details
    action VARCHAR(20) NOT NULL,  -- 'shared', 'unshared', 'role_changed'
    old_role participant_role,
    new_role participant_role,

    -- Timeline
    action_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Constraints
    CONSTRAINT case_sharing_audit_action_valid CHECK (
        action IN ('shared', 'unshared', 'role_changed')
    )
);

-- Indexes
CREATE INDEX idx_case_sharing_audit_case_id ON case_sharing_audit(case_id);
CREATE INDEX idx_case_sharing_audit_target_user ON case_sharing_audit(target_user_id);
CREATE INDEX idx_case_sharing_audit_action_by ON case_sharing_audit(action_by);
CREATE INDEX idx_case_sharing_audit_action_at ON case_sharing_audit(action_at DESC);

COMMENT ON TABLE case_sharing_audit IS 'Audit trail for case sharing actions - compliance and security';

-- ============================================================================
-- VIEWS
-- ============================================================================

-- View: Cases shared with user
CREATE VIEW user_shared_cases AS
SELECT
    cp.user_id,
    cp.case_id,
    c.title,
    c.status,
    c.user_id AS case_owner_id,
    cp.role AS participant_role,
    cp.added_at AS shared_at,
    cp.added_by AS shared_by,
    cp.last_accessed_at,
    c.created_at AS case_created_at,
    c.updated_at AS case_updated_at
FROM case_participants cp
JOIN cases c ON cp.case_id = c.case_id
ORDER BY cp.added_at DESC;

COMMENT ON VIEW user_shared_cases IS 'All cases shared with each user with role and metadata';

-- View: Case collaboration summary
CREATE VIEW case_collaboration_summary AS
SELECT
    c.case_id,
    c.title,
    c.user_id AS owner_id,
    c.status,
    COUNT(cp.user_id) AS participant_count,
    COUNT(cp.user_id) FILTER (WHERE cp.role = 'owner') AS owner_count,
    COUNT(cp.user_id) FILTER (WHERE cp.role = 'collaborator') AS collaborator_count,
    COUNT(cp.user_id) FILTER (WHERE cp.role = 'viewer') AS viewer_count,
    MAX(cp.added_at) AS last_shared_at,
    MAX(cp.last_accessed_at) AS last_accessed_at
FROM cases c
LEFT JOIN case_participants cp ON c.case_id = cp.case_id
GROUP BY c.case_id, c.title, c.user_id, c.status;

COMMENT ON VIEW case_collaboration_summary IS 'Collaboration statistics per case';

-- ============================================================================
-- FUNCTIONS
-- ============================================================================

-- Function: Add or update case participant
CREATE OR REPLACE FUNCTION upsert_case_participant(
    p_case_id VARCHAR(17),
    p_user_id VARCHAR(20),
    p_role participant_role,
    p_added_by VARCHAR(20)
)
RETURNS VOID AS $$
DECLARE
    v_old_role participant_role;
BEGIN
    -- Check if participant already exists
    SELECT role INTO v_old_role
    FROM case_participants
    WHERE case_id = p_case_id AND user_id = p_user_id;

    IF FOUND THEN
        -- Update existing participant
        IF v_old_role != p_role THEN
            UPDATE case_participants
            SET role = p_role
            WHERE case_id = p_case_id AND user_id = p_user_id;

            -- Log role change
            INSERT INTO case_sharing_audit (
                case_id, target_user_id, action_by, action, old_role, new_role
            ) VALUES (
                p_case_id, p_user_id, p_added_by, 'role_changed', v_old_role, p_role
            );
        END IF;
    ELSE
        -- Insert new participant
        INSERT INTO case_participants (case_id, user_id, role, added_by)
        VALUES (p_case_id, p_user_id, p_role, p_added_by);

        -- Log sharing action
        INSERT INTO case_sharing_audit (
            case_id, target_user_id, action_by, action, new_role
        ) VALUES (
            p_case_id, p_user_id, p_added_by, 'shared', p_role
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION upsert_case_participant IS 'Add or update case participant with automatic audit logging';

-- Function: Remove case participant
CREATE OR REPLACE FUNCTION remove_case_participant(
    p_case_id VARCHAR(17),
    p_user_id VARCHAR(20),
    p_removed_by VARCHAR(20)
)
RETURNS VOID AS $$
DECLARE
    v_old_role participant_role;
BEGIN
    -- Get current role
    SELECT role INTO v_old_role
    FROM case_participants
    WHERE case_id = p_case_id AND user_id = p_user_id;

    IF FOUND THEN
        -- Remove participant
        DELETE FROM case_participants
        WHERE case_id = p_case_id AND user_id = p_user_id;

        -- Log unsharing action
        INSERT INTO case_sharing_audit (
            case_id, target_user_id, action_by, action, old_role
        ) VALUES (
            p_case_id, p_user_id, p_removed_by, 'unshared', v_old_role
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION remove_case_participant IS 'Remove case participant with automatic audit logging';

-- Function: Check if user has access to case
CREATE OR REPLACE FUNCTION user_can_access_case(
    p_user_id VARCHAR(20),
    p_case_id VARCHAR(17)
)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        -- User is the case owner
        SELECT 1 FROM cases
        WHERE case_id = p_case_id AND user_id = p_user_id

        UNION

        -- User is a participant
        SELECT 1 FROM case_participants
        WHERE case_id = p_case_id AND user_id = p_user_id
    );
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION user_can_access_case IS 'Check if user has access to case (owner or participant)';

-- Function: Get user role for case
CREATE OR REPLACE FUNCTION get_user_case_role(
    p_user_id VARCHAR(20),
    p_case_id VARCHAR(17)
)
RETURNS TEXT AS $$
DECLARE
    v_role TEXT;
BEGIN
    -- Check if user is the owner
    IF EXISTS (SELECT 1 FROM cases WHERE case_id = p_case_id AND user_id = p_user_id) THEN
        RETURN 'owner';
    END IF;

    -- Check participant role
    SELECT role::TEXT INTO v_role
    FROM case_participants
    WHERE case_id = p_case_id AND user_id = p_user_id;

    RETURN v_role;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_user_case_role IS 'Get user role for a case (owner, collaborator, viewer, or NULL)';

-- ============================================================================
-- TRIGGERS
-- ============================================================================

-- Trigger: Update last_accessed_at when case is viewed
CREATE OR REPLACE FUNCTION update_participant_last_accessed()
RETURNS TRIGGER AS $$
BEGIN
    -- This will be called from application code when a participant views a case
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Note: Application code should call this when tracking access:
-- UPDATE case_participants SET last_accessed_at = NOW()
-- WHERE case_id = :case_id AND user_id = :user_id;

-- ============================================================================
-- DATA INTEGRITY
-- ============================================================================

-- Ensure case owner is always included as participant with 'owner' role
-- This is enforced at application level, not database level
-- Rationale: owner is implicit in cases.user_id, explicit in case_participants for consistency

-- ============================================================================
-- SECURITY
-- ============================================================================

-- Row-Level Security policies should be added in production:
-- Example:
-- ALTER TABLE case_participants ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY case_participants_access ON case_participants
--   FOR SELECT
--   USING (user_id = current_setting('app.user_id')::VARCHAR);

-- ============================================================================
-- GRANTS (adjust based on your user/role setup)
-- ============================================================================

-- Example: Grant privileges to application role
-- GRANT ALL PRIVILEGES ON case_participants TO faultmaven_app;
-- GRANT ALL PRIVILEGES ON case_sharing_audit TO faultmaven_app;
-- GRANT EXECUTE ON FUNCTION upsert_case_participant TO faultmaven_app;
-- GRANT EXECUTE ON FUNCTION remove_case_participant TO faultmaven_app;
-- GRANT EXECUTE ON FUNCTION user_can_access_case TO faultmaven_app;
-- GRANT EXECUTE ON FUNCTION get_user_case_role TO faultmaven_app;

-- ============================================================================
-- VERIFICATION
-- ============================================================================

-- To verify extension:
-- \d case_participants
-- \d case_sharing_audit
-- SELECT * FROM user_shared_cases LIMIT 5;
-- SELECT * FROM case_collaboration_summary LIMIT 5;

-- Test access check:
-- SELECT user_can_access_case('user_alice', 'case_abc123');
-- SELECT get_user_case_role('user_alice', 'case_abc123');

-- ============================================================================
-- SCHEMA EXTENSION COMPLETE
-- ============================================================================
