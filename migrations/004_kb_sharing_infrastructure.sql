-- Schema Extension: 004 - Knowledge Base Sharing Infrastructure
-- Date: 2025-01-14
-- Description: Add knowledge base document sharing capabilities
--              - kb_documents (document metadata and ownership)
--              - kb_document_shares (individual user sharing)
--              - kb_document_team_shares (team-based sharing)
--              - kb_document_org_shares (organization-wide sharing)
--              - Visibility levels: private, shared, team, organization
--
-- Design Reference: docs/architecture/data-storage-design.md (Section 5 - Knowledge Base Sharing)
-- Resolves: Design gaps for sharing runbooks with specific users and teams
--
-- Architecture Change:
-- - From: Per-user ChromaDB collections (user_kb_{user_id})
-- - To: Hybrid model with visibility control
--       * Private: user_kb_private_{user_id} (backward compatible)
--       * Shared: kb_shared (all shared documents with metadata filtering)

-- ============================================================================
-- ENUMS
-- ============================================================================

-- Document visibility levels
CREATE TYPE kb_visibility AS ENUM (
    'private',      -- Only owner can access
    'shared',       -- Shared with specific users
    'team',         -- Shared with specific teams
    'organization'  -- Shared with entire organization
);

-- Document types
CREATE TYPE kb_document_type AS ENUM (
    'runbook',
    'procedure',
    'documentation',
    'troubleshooting_guide',
    'best_practices',
    'incident_postmortem',
    'architecture_diagram',
    'other'
);

-- Share permission levels
CREATE TYPE kb_share_permission AS ENUM (
    'read',   -- Can view document
    'write'   -- Can view and edit document
);

COMMENT ON TYPE kb_visibility IS 'Document visibility scope';
COMMENT ON TYPE kb_document_type IS 'Knowledge base document categories';
COMMENT ON TYPE kb_share_permission IS 'Permission level for shared documents';

-- ============================================================================
-- TABLE: kb_documents
-- ============================================================================

CREATE TABLE kb_documents (
    -- Primary Key
    doc_id VARCHAR(20) PRIMARY KEY DEFAULT ('kbdoc_' || substr(gen_random_uuid()::text, 1, 15)),

    -- Ownership
    owner_user_id VARCHAR(20) NOT NULL,
    org_id VARCHAR(20) REFERENCES organizations(org_id) ON DELETE CASCADE,

    -- Document Info
    title VARCHAR(500) NOT NULL,
    description TEXT,
    document_type kb_document_type NOT NULL DEFAULT 'other',

    -- Storage References
    chromadb_collection VARCHAR(100) NOT NULL,  -- Which ChromaDB collection stores this
    chromadb_doc_count INTEGER DEFAULT 0,  -- Number of chunks in ChromaDB

    -- Content Metadata
    file_size INTEGER,  -- Original file size in bytes
    original_filename VARCHAR(255),
    content_type VARCHAR(100),  -- MIME type
    storage_path VARCHAR(1000),  -- S3 or local path for original file

    -- Visibility and Access
    visibility kb_visibility NOT NULL DEFAULT 'private',

    -- Tags and Categories
    tags TEXT[],  -- Array of tags for filtering

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Soft Delete
    deleted_at TIMESTAMPTZ,

    -- Constraints
    CONSTRAINT kb_documents_title_not_empty CHECK (LENGTH(TRIM(title)) > 0),
    CONSTRAINT kb_documents_collection_not_empty CHECK (LENGTH(TRIM(chromadb_collection)) > 0),
    CONSTRAINT kb_documents_file_size_positive CHECK (file_size IS NULL OR file_size > 0)
);

-- Indexes
CREATE INDEX idx_kb_documents_owner ON kb_documents(owner_user_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_kb_documents_org_id ON kb_documents(org_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_kb_documents_visibility ON kb_documents(visibility);
CREATE INDEX idx_kb_documents_document_type ON kb_documents(document_type);
CREATE INDEX idx_kb_documents_created_at ON kb_documents(created_at DESC);
CREATE INDEX idx_kb_documents_tags ON kb_documents USING GIN (tags);
CREATE INDEX idx_kb_documents_metadata_gin ON kb_documents USING GIN (metadata);

-- Full-text search on title and description
CREATE INDEX idx_kb_documents_fts ON kb_documents USING GIN (
    to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(description, ''))
);

-- Auto-update updated_at
CREATE TRIGGER kb_documents_update_updated_at
    BEFORE UPDATE ON kb_documents
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE kb_documents IS 'Knowledge base document metadata with ownership and visibility control';
COMMENT ON COLUMN kb_documents.chromadb_collection IS 'ChromaDB collection name where document chunks are stored';
COMMENT ON COLUMN kb_documents.chromadb_doc_count IS 'Number of embedding chunks stored in ChromaDB';
COMMENT ON COLUMN kb_documents.visibility IS 'Access scope: private (owner only), shared (specific users), team, organization';

-- ============================================================================
-- TABLE: kb_document_shares
-- ============================================================================

CREATE TABLE kb_document_shares (
    -- Composite Primary Key
    doc_id VARCHAR(20) NOT NULL REFERENCES kb_documents(doc_id) ON DELETE CASCADE,
    shared_with_user_id VARCHAR(20) NOT NULL,
    PRIMARY KEY (doc_id, shared_with_user_id),

    -- Permission Level
    permission kb_share_permission NOT NULL DEFAULT 'read',

    -- Audit Trail
    shared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    shared_by VARCHAR(20) NOT NULL,  -- User who shared the document
    last_accessed_at TIMESTAMPTZ,

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Indexes
CREATE INDEX idx_kb_document_shares_doc_id ON kb_document_shares(doc_id);
CREATE INDEX idx_kb_document_shares_user_id ON kb_document_shares(shared_with_user_id);
CREATE INDEX idx_kb_document_shares_permission ON kb_document_shares(permission);
CREATE INDEX idx_kb_document_shares_shared_at ON kb_document_shares(shared_at DESC);

COMMENT ON TABLE kb_document_shares IS 'Individual user sharing for knowledge base documents';
COMMENT ON COLUMN kb_document_shares.permission IS 'Access level: read (view only) or write (view and edit)';

-- ============================================================================
-- TABLE: kb_document_team_shares
-- ============================================================================

CREATE TABLE kb_document_team_shares (
    -- Composite Primary Key
    doc_id VARCHAR(20) NOT NULL REFERENCES kb_documents(doc_id) ON DELETE CASCADE,
    team_id VARCHAR(20) NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    PRIMARY KEY (doc_id, team_id),

    -- Permission Level
    permission kb_share_permission NOT NULL DEFAULT 'read',

    -- Audit Trail
    shared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    shared_by VARCHAR(20) NOT NULL,

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Indexes
CREATE INDEX idx_kb_document_team_shares_doc_id ON kb_document_team_shares(doc_id);
CREATE INDEX idx_kb_document_team_shares_team_id ON kb_document_team_shares(team_id);
CREATE INDEX idx_kb_document_team_shares_permission ON kb_document_team_shares(permission);
CREATE INDEX idx_kb_document_team_shares_shared_at ON kb_document_team_shares(shared_at DESC);

COMMENT ON TABLE kb_document_team_shares IS 'Team-based sharing for knowledge base documents';

-- ============================================================================
-- TABLE: kb_document_org_shares
-- ============================================================================

CREATE TABLE kb_document_org_shares (
    -- Composite Primary Key
    doc_id VARCHAR(20) NOT NULL REFERENCES kb_documents(doc_id) ON DELETE CASCADE,
    org_id VARCHAR(20) NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,
    PRIMARY KEY (doc_id, org_id),

    -- Permission Level
    permission kb_share_permission NOT NULL DEFAULT 'read',

    -- Audit Trail
    shared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    shared_by VARCHAR(20) NOT NULL,

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Indexes
CREATE INDEX idx_kb_document_org_shares_doc_id ON kb_document_org_shares(doc_id);
CREATE INDEX idx_kb_document_org_shares_org_id ON kb_document_org_shares(org_id);
CREATE INDEX idx_kb_document_org_shares_permission ON kb_document_org_shares(permission);
CREATE INDEX idx_kb_document_org_shares_shared_at ON kb_document_org_shares(shared_at DESC);

COMMENT ON TABLE kb_document_org_shares IS 'Organization-wide sharing for knowledge base documents';

-- ============================================================================
-- TABLE: kb_sharing_audit
-- ============================================================================

CREATE TABLE kb_sharing_audit (
    -- Primary Key
    audit_id SERIAL PRIMARY KEY,

    -- References
    doc_id VARCHAR(20) NOT NULL REFERENCES kb_documents(doc_id) ON DELETE CASCADE,
    action_by VARCHAR(20) NOT NULL,  -- User performing the action

    -- Action Details
    action VARCHAR(20) NOT NULL,  -- 'shared', 'unshared', 'permission_changed', 'visibility_changed'
    share_type VARCHAR(20),  -- 'user', 'team', 'organization'
    target_id VARCHAR(20),  -- User, team, or org ID

    -- Changes
    old_permission kb_share_permission,
    new_permission kb_share_permission,
    old_visibility kb_visibility,
    new_visibility kb_visibility,

    -- Timeline
    action_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Constraints
    CONSTRAINT kb_sharing_audit_action_valid CHECK (
        action IN ('shared', 'unshared', 'permission_changed', 'visibility_changed')
    ),
    CONSTRAINT kb_sharing_audit_share_type_valid CHECK (
        share_type IS NULL OR share_type IN ('user', 'team', 'organization')
    )
);

-- Indexes
CREATE INDEX idx_kb_sharing_audit_doc_id ON kb_sharing_audit(doc_id);
CREATE INDEX idx_kb_sharing_audit_action_by ON kb_sharing_audit(action_by);
CREATE INDEX idx_kb_sharing_audit_action ON kb_sharing_audit(action);
CREATE INDEX idx_kb_sharing_audit_action_at ON kb_sharing_audit(action_at DESC);

COMMENT ON TABLE kb_sharing_audit IS 'Audit trail for KB document sharing actions';

-- ============================================================================
-- VIEWS
-- ============================================================================

-- View: User's accessible KB documents (all documents user can access)
CREATE VIEW user_accessible_kb_documents AS
SELECT DISTINCT
    d.doc_id,
    d.owner_user_id,
    d.org_id,
    d.title,
    d.description,
    d.document_type,
    d.visibility,
    d.tags,
    d.created_at,
    d.updated_at,
    CASE
        WHEN d.owner_user_id = 'CURRENT_USER' THEN 'owner'
        WHEN ds.permission IS NOT NULL THEN ds.permission::TEXT
        WHEN dts.permission IS NOT NULL THEN dts.permission::TEXT
        WHEN dos.permission IS NOT NULL THEN dos.permission::TEXT
        ELSE NULL
    END AS user_permission
FROM kb_documents d
LEFT JOIN kb_document_shares ds ON d.doc_id = ds.doc_id
LEFT JOIN kb_document_team_shares dts ON d.doc_id = dts.doc_id
LEFT JOIN kb_document_org_shares dos ON d.doc_id = dos.doc_id
WHERE d.deleted_at IS NULL;

COMMENT ON VIEW user_accessible_kb_documents IS 'All KB documents accessible to users with their permission levels';

-- View: KB document sharing summary
CREATE VIEW kb_document_sharing_summary AS
SELECT
    d.doc_id,
    d.title,
    d.owner_user_id,
    d.visibility,
    COUNT(DISTINCT ds.shared_with_user_id) AS user_share_count,
    COUNT(DISTINCT dts.team_id) AS team_share_count,
    COUNT(DISTINCT dos.org_id) AS org_share_count,
    MAX(GREATEST(
        ds.shared_at,
        dts.shared_at,
        dos.shared_at
    )) AS last_shared_at
FROM kb_documents d
LEFT JOIN kb_document_shares ds ON d.doc_id = ds.doc_id
LEFT JOIN kb_document_team_shares dts ON d.doc_id = dts.doc_id
LEFT JOIN kb_document_org_shares dos ON d.doc_id = dos.doc_id
WHERE d.deleted_at IS NULL
GROUP BY d.doc_id, d.title, d.owner_user_id, d.visibility;

COMMENT ON VIEW kb_document_sharing_summary IS 'Sharing statistics per KB document';

-- ============================================================================
-- FUNCTIONS
-- ============================================================================

-- Function: Check if user can access KB document
CREATE OR REPLACE FUNCTION user_can_access_kb_document(
    p_user_id VARCHAR(20),
    p_doc_id VARCHAR(20)
)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        -- User is the document owner
        SELECT 1 FROM kb_documents
        WHERE doc_id = p_doc_id AND owner_user_id = p_user_id AND deleted_at IS NULL

        UNION

        -- Document is shared directly with user
        SELECT 1 FROM kb_document_shares
        WHERE doc_id = p_doc_id AND shared_with_user_id = p_user_id

        UNION

        -- Document is shared with user's team
        SELECT 1 FROM kb_document_team_shares dts
        JOIN team_members tm ON dts.team_id = tm.team_id
        WHERE dts.doc_id = p_doc_id AND tm.user_id = p_user_id

        UNION

        -- Document is shared with user's organization
        SELECT 1 FROM kb_document_org_shares dos
        JOIN organization_members om ON dos.org_id = om.org_id
        WHERE dos.doc_id = p_doc_id AND om.user_id = p_user_id
    );
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION user_can_access_kb_document IS 'Check if user has access to KB document';

-- Function: Get user permission for KB document
CREATE OR REPLACE FUNCTION get_user_kb_document_permission(
    p_user_id VARCHAR(20),
    p_doc_id VARCHAR(20)
)
RETURNS TEXT AS $$
DECLARE
    v_permission TEXT;
BEGIN
    -- Check if user is the owner
    IF EXISTS (SELECT 1 FROM kb_documents WHERE doc_id = p_doc_id AND owner_user_id = p_user_id AND deleted_at IS NULL) THEN
        RETURN 'write';  -- Owners always have write permission
    END IF;

    -- Check direct user share (highest priority)
    SELECT permission::TEXT INTO v_permission
    FROM kb_document_shares
    WHERE doc_id = p_doc_id AND shared_with_user_id = p_user_id;

    IF v_permission IS NOT NULL THEN
        RETURN v_permission;
    END IF;

    -- Check team share
    SELECT dts.permission::TEXT INTO v_permission
    FROM kb_document_team_shares dts
    JOIN team_members tm ON dts.team_id = tm.team_id
    WHERE dts.doc_id = p_doc_id AND tm.user_id = p_user_id
    LIMIT 1;

    IF v_permission IS NOT NULL THEN
        RETURN v_permission;
    END IF;

    -- Check organization share
    SELECT dos.permission::TEXT INTO v_permission
    FROM kb_document_org_shares dos
    JOIN organization_members om ON dos.org_id = om.org_id
    WHERE dos.doc_id = p_doc_id AND om.user_id = p_user_id
    LIMIT 1;

    RETURN v_permission;  -- NULL if no access
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION get_user_kb_document_permission IS 'Get user permission level for KB document (read, write, or NULL)';

-- Function: Share KB document with user
CREATE OR REPLACE FUNCTION share_kb_document_with_user(
    p_doc_id VARCHAR(20),
    p_shared_with_user_id VARCHAR(20),
    p_permission kb_share_permission,
    p_shared_by VARCHAR(20)
)
RETURNS VOID AS $$
DECLARE
    v_old_permission kb_share_permission;
BEGIN
    -- Check if already shared
    SELECT permission INTO v_old_permission
    FROM kb_document_shares
    WHERE doc_id = p_doc_id AND shared_with_user_id = p_shared_with_user_id;

    IF FOUND THEN
        -- Update permission if different
        IF v_old_permission != p_permission THEN
            UPDATE kb_document_shares
            SET permission = p_permission
            WHERE doc_id = p_doc_id AND shared_with_user_id = p_shared_with_user_id;

            -- Log permission change
            INSERT INTO kb_sharing_audit (
                doc_id, action_by, action, share_type, target_id, old_permission, new_permission
            ) VALUES (
                p_doc_id, p_shared_by, 'permission_changed', 'user', p_shared_with_user_id, v_old_permission, p_permission
            );
        END IF;
    ELSE
        -- Insert new share
        INSERT INTO kb_document_shares (doc_id, shared_with_user_id, permission, shared_by)
        VALUES (p_doc_id, p_shared_with_user_id, p_permission, p_shared_by);

        -- Log sharing action
        INSERT INTO kb_sharing_audit (
            doc_id, action_by, action, share_type, target_id, new_permission
        ) VALUES (
            p_doc_id, p_shared_by, 'shared', 'user', p_shared_with_user_id, p_permission
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION share_kb_document_with_user IS 'Share KB document with user with automatic audit logging';

-- Function: Share KB document with team
CREATE OR REPLACE FUNCTION share_kb_document_with_team(
    p_doc_id VARCHAR(20),
    p_team_id VARCHAR(20),
    p_permission kb_share_permission,
    p_shared_by VARCHAR(20)
)
RETURNS VOID AS $$
BEGIN
    -- Insert or update team share
    INSERT INTO kb_document_team_shares (doc_id, team_id, permission, shared_by)
    VALUES (p_doc_id, p_team_id, p_permission, p_shared_by)
    ON CONFLICT (doc_id, team_id) DO UPDATE
    SET permission = p_permission;

    -- Log sharing action
    INSERT INTO kb_sharing_audit (
        doc_id, action_by, action, share_type, target_id, new_permission
    ) VALUES (
        p_doc_id, p_shared_by, 'shared', 'team', p_team_id, p_permission
    );

    -- Update document visibility if needed
    UPDATE kb_documents
    SET visibility = 'team'
    WHERE doc_id = p_doc_id AND visibility = 'private';
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION share_kb_document_with_team IS 'Share KB document with team with automatic audit logging';

-- Function: Unshare KB document from user
CREATE OR REPLACE FUNCTION unshare_kb_document_from_user(
    p_doc_id VARCHAR(20),
    p_user_id VARCHAR(20),
    p_unshared_by VARCHAR(20)
)
RETURNS VOID AS $$
DECLARE
    v_old_permission kb_share_permission;
BEGIN
    -- Get current permission
    SELECT permission INTO v_old_permission
    FROM kb_document_shares
    WHERE doc_id = p_doc_id AND shared_with_user_id = p_user_id;

    IF FOUND THEN
        -- Remove share
        DELETE FROM kb_document_shares
        WHERE doc_id = p_doc_id AND shared_with_user_id = p_user_id;

        -- Log unsharing action
        INSERT INTO kb_sharing_audit (
            doc_id, action_by, action, share_type, target_id, old_permission
        ) VALUES (
            p_doc_id, p_unshared_by, 'unshared', 'user', p_user_id, v_old_permission
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION unshare_kb_document_from_user IS 'Unshare KB document from user with automatic audit logging';

-- ============================================================================
-- ROW-LEVEL SECURITY (RLS)
-- ============================================================================

-- Enable RLS on kb_documents
ALTER TABLE kb_documents ENABLE ROW LEVEL SECURITY;

-- Policy: Users can access their own documents
CREATE POLICY kb_documents_owner_access ON kb_documents
    FOR ALL
    USING (owner_user_id = current_setting('app.user_id', true)::VARCHAR);

-- Policy: Users can access documents shared with them
CREATE POLICY kb_documents_shared_access ON kb_documents
    FOR SELECT
    USING (
        doc_id IN (
            SELECT doc_id FROM kb_document_shares
            WHERE shared_with_user_id = current_setting('app.user_id', true)::VARCHAR
        )
    );

-- Policy: Users can access documents shared with their teams
CREATE POLICY kb_documents_team_access ON kb_documents
    FOR SELECT
    USING (
        doc_id IN (
            SELECT dts.doc_id FROM kb_document_team_shares dts
            JOIN team_members tm ON dts.team_id = tm.team_id
            WHERE tm.user_id = current_setting('app.user_id', true)::VARCHAR
        )
    );

-- Policy: Users can access documents shared with their organization
CREATE POLICY kb_documents_org_access ON kb_documents
    FOR SELECT
    USING (
        doc_id IN (
            SELECT dos.doc_id FROM kb_document_org_shares dos
            JOIN organization_members om ON dos.org_id = om.org_id
            WHERE om.user_id = current_setting('app.user_id', true)::VARCHAR
        )
    );

COMMENT ON POLICY kb_documents_owner_access ON kb_documents IS 'Users can access their own KB documents';
COMMENT ON POLICY kb_documents_shared_access ON kb_documents IS 'Users can access KB documents shared with them';
COMMENT ON POLICY kb_documents_team_access ON kb_documents IS 'Users can access KB documents shared with their teams';
COMMENT ON POLICY kb_documents_org_access ON kb_documents IS 'Users can access KB documents shared with their organization';

-- ============================================================================
-- GRANTS (adjust based on your user/role setup)
-- ============================================================================

-- Example: Grant privileges to application role
-- GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO faultmaven_app;
-- GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO faultmaven_app;
-- GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO faultmaven_app;

-- ============================================================================
-- MIGRATION NOTES
-- ============================================================================

-- Existing ChromaDB collections migration:
-- - user_kb_{user_id} collections remain for backward compatibility
-- - New documents use kb_private_{user_id} (private) or kb_shared (shared)
-- - Migration script needed to:
--   1. Create kb_documents entries for existing user KB documents
--   2. Keep existing collections as-is (no data movement)
--   3. Update UserKBVectorStore to check both old and new collections

-- ============================================================================
-- VERIFICATION
-- ============================================================================

-- To verify extension:
-- \d kb_documents
-- \d kb_document_shares
-- \d kb_document_team_shares
-- \d kb_document_org_shares
-- \d kb_sharing_audit

-- Test access checks:
-- SELECT user_can_access_kb_document('user_alice', 'kbdoc_123');
-- SELECT get_user_kb_document_permission('user_alice', 'kbdoc_123');

-- Test sharing:
-- SELECT share_kb_document_with_user('kbdoc_123', 'user_bob', 'read', 'user_alice');
-- SELECT share_kb_document_with_team('kbdoc_123', 'team_456', 'read', 'user_alice');

-- ============================================================================
-- SCHEMA EXTENSION COMPLETE
-- ============================================================================
