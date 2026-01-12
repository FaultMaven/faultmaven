-- Migration: 005 - Add Reports Table (TD-001)
-- Date: 2026-01-10
-- Description: Add reports table to Case schema for persistent report storage
--              Migrates reports from ephemeral Redis + ChromaDB to PostgreSQL
--              Reports now persist with case lifecycle (CASCADE delete)
--
-- Design Reference: docs/architecture/case-storage-design.md Section 4.9
--                   docs/architecture/data-storage-design.md Section 8
-- Related: TD-001 (module-organization-design.md)

BEGIN;

-- ============================================================================
-- TABLE: reports
-- ============================================================================

CREATE TABLE reports (
    report_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- ============================================================
    -- Report Type & Versioning
    -- ============================================================
    report_type VARCHAR(30) NOT NULL,              -- incident_report | runbook | post_mortem
    version INTEGER NOT NULL DEFAULT 1,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,      -- Latest version for this report_type
    linked_to_closure BOOLEAN NOT NULL DEFAULT FALSE,

    -- ============================================================
    -- Content
    -- ============================================================
    title VARCHAR(200) NOT NULL,
    content TEXT NOT NULL,                         -- Full markdown content
    format VARCHAR(20) NOT NULL DEFAULT 'markdown',

    -- ============================================================
    -- Generation Metadata
    -- ============================================================
    generation_status VARCHAR(20) NOT NULL,        -- generating | completed | failed
    generation_time_ms INTEGER NOT NULL CHECK (generation_time_ms >= 0 AND generation_time_ms <= 120000),
    generated_by VARCHAR(255),                     -- Optional: user_id who triggered generation (not in CaseReport model yet)

    -- ============================================================
    -- Runbook-Specific Metadata (JSONB for flexibility)
    -- ============================================================
    metadata JSONB DEFAULT '{}'::jsonb,            -- RunbookMetadata: source, domain, tags, etc.

    -- ============================================================
    -- Timestamps
    -- ============================================================
    generated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),

    CONSTRAINT reports_type_check
        CHECK (report_type IN ('incident_report', 'runbook', 'post_mortem')),

    CONSTRAINT reports_status_check
        CHECK (generation_status IN ('generating', 'completed', 'failed')),

    CONSTRAINT reports_format_check
        CHECK (format IN ('markdown')),

    CONSTRAINT reports_version_check
        CHECK (version >= 1 AND version <= 5)
);

-- Ensure only one current version per report_type per case (partial unique index)
CREATE UNIQUE INDEX idx_reports_current_unique
    ON reports(case_id, report_type)
    WHERE is_current = TRUE;

-- Indexes for report queries
CREATE INDEX idx_reports_case ON reports(case_id);
CREATE INDEX idx_reports_type_version ON reports(case_id, report_type, version DESC);
CREATE INDEX idx_reports_closure ON reports(case_id) WHERE linked_to_closure = TRUE;
CREATE INDEX idx_reports_generated_at ON reports(generated_at DESC);

-- Full-text search on report content
CREATE INDEX idx_reports_content_search ON reports USING gin(
    to_tsvector('english', title || ' ' || content)
);

COMMENT ON TABLE reports IS 'Generated case reports (incident reports, runbooks, post-mortems) - versioned, persistent storage (TD-001)';
COMMENT ON COLUMN reports.metadata IS 'Runbook-specific metadata: source (incident_driven/document_driven), domain, tags, etc.';
COMMENT ON COLUMN reports.generated_by IS 'Optional: user_id who triggered generation (not in CaseReport Pydantic model yet)';

-- ============================================================================
-- UPDATE VIEWS (Add report counts)
-- ============================================================================

-- Update case_overview view to include report count
DROP VIEW IF EXISTS case_overview;
CREATE VIEW case_overview AS
SELECT
    c.case_id,
    c.user_id,
    c.title,
    c.status,
    c.created_at,
    c.updated_at,
    COUNT(DISTINCT e.evidence_id) AS evidence_count,
    COUNT(DISTINCT h.hypothesis_id) AS hypothesis_count,
    COUNT(DISTINCT s.solution_id) AS solution_count,
    COUNT(DISTINCT m.message_id) AS message_count,
    COUNT(DISTINCT f.file_id) AS file_count,
    COUNT(DISTINCT r.report_id) AS report_count
FROM cases c
LEFT JOIN evidence e ON c.case_id = e.case_id
LEFT JOIN hypotheses h ON c.case_id = h.case_id
LEFT JOIN solutions s ON c.case_id = s.case_id
LEFT JOIN case_messages m ON c.case_id = m.case_id
LEFT JOIN uploaded_files f ON c.case_id = f.case_id
LEFT JOIN reports r ON c.case_id = r.case_id AND r.is_current = TRUE
GROUP BY c.case_id, c.user_id, c.title, c.status, c.created_at, c.updated_at;

COMMIT;

-- ============================================================================
-- VERIFICATION
-- ============================================================================

-- Verify table created
SELECT COUNT(*) FROM reports;

-- Verify indexes created
SELECT indexname, indexdef 
FROM pg_indexes 
WHERE tablename = 'reports'
ORDER BY indexname;

-- Verify FK constraint
SELECT 
    tc.constraint_name, 
    tc.table_name, 
    kcu.column_name, 
    ccu.table_name AS foreign_table_name,
    ccu.column_name AS foreign_column_name 
FROM information_schema.table_constraints AS tc 
JOIN information_schema.key_column_usage AS kcu
  ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage AS ccu
  ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY' 
  AND tc.table_name = 'reports';
