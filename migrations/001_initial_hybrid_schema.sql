-- Migration: 001 - Initial Hybrid Schema (10 Tables)
-- Date: 2025-01-09
-- Description: Production-ready PostgreSQL schema with hybrid normalization
--              Normalized tables for high-cardinality data (evidence, hypotheses, solutions)
--              JSONB columns for low-cardinality flexible data (consulting, conclusions)
--
-- Design Reference: docs/architecture/case-storage-design.md

-- ============================================================================
-- EXTENSIONS
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================================
-- ENUMS
-- ============================================================================

CREATE TYPE case_status AS ENUM (
    'consulting',
    'problem_verification',
    'root_cause_analysis',
    'solution_implementation',
    'resolved',
    'closed',
    'archived'
);

CREATE TYPE evidence_category AS ENUM (
    'LOGS_AND_ERRORS',
    'STRUCTURED_CONFIG',
    'METRICS_AND_PERFORMANCE',
    'UNSTRUCTURED_TEXT',
    'SOURCE_CODE',
    'VISUAL_EVIDENCE',
    'UNKNOWN'
);

CREATE TYPE hypothesis_status AS ENUM (
    'proposed',
    'testing',
    'validated',
    'invalidated',
    'deferred'
);

CREATE TYPE solution_status AS ENUM (
    'proposed',
    'in_progress',
    'implemented',
    'verified',
    'rejected'
);

CREATE TYPE message_role AS ENUM (
    'user',
    'assistant',
    'system'
);

CREATE TYPE file_processing_status AS ENUM (
    'pending',
    'processing',
    'completed',
    'failed'
);

-- ============================================================================
-- TABLE: cases
-- ============================================================================

CREATE TABLE cases (
    -- Primary Key
    case_id VARCHAR(17) PRIMARY KEY,

    -- Core Attributes
    user_id VARCHAR(255) NOT NULL,
    title VARCHAR(200) NOT NULL,
    status case_status NOT NULL DEFAULT 'consulting',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Phase: Consulting (JSONB - low cardinality, flexible)
    consulting JSONB NOT NULL DEFAULT '{
        "initial_description": "",
        "context": {},
        "user_goals": []
    }'::jsonb,

    -- Phase: Problem Verification (JSONB)
    problem_verification JSONB DEFAULT NULL,

    -- Phase: Working Conclusion (JSONB)
    working_conclusion JSONB DEFAULT NULL,

    -- Phase: Root Cause Conclusion (JSONB)
    root_cause_conclusion JSONB DEFAULT NULL,

    -- Path Selection (JSONB)
    path_selection JSONB DEFAULT NULL,

    -- Degraded Mode (JSONB)
    degraded_mode JSONB DEFAULT NULL,

    -- Escalation State (JSONB)
    escalation_state JSONB DEFAULT NULL,

    -- Documentation (JSONB)
    documentation JSONB DEFAULT '{
        "summary": "",
        "timeline": [],
        "lessons_learned": []
    }'::jsonb,

    -- Progress Tracking (JSONB)
    progress JSONB DEFAULT '{
        "current_phase": "consulting",
        "completion_percentage": 0,
        "milestones": []
    }'::jsonb,

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Constraints
    CONSTRAINT cases_title_not_empty CHECK (LENGTH(TRIM(title)) > 0),
    CONSTRAINT cases_user_id_not_empty CHECK (LENGTH(TRIM(user_id)) > 0)
);

-- Indexes
CREATE INDEX idx_cases_user_id ON cases(user_id);
CREATE INDEX idx_cases_status ON cases(status);
CREATE INDEX idx_cases_created_at ON cases(created_at DESC);
CREATE INDEX idx_cases_updated_at ON cases(updated_at DESC);

-- GIN index for JSONB queries
CREATE INDEX idx_cases_consulting_gin ON cases USING GIN (consulting);
CREATE INDEX idx_cases_problem_verification_gin ON cases USING GIN (problem_verification);
CREATE INDEX idx_cases_metadata_gin ON cases USING GIN (metadata);

-- ============================================================================
-- TABLE: evidence
-- ============================================================================

CREATE TABLE evidence (
    -- Primary Key
    evidence_id VARCHAR(15) PRIMARY KEY,

    -- Foreign Key
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Core Attributes
    category evidence_category NOT NULL,
    summary VARCHAR(500) NOT NULL,
    preprocessed_content TEXT NOT NULL,
    content_ref VARCHAR(1000),  -- S3 URI for raw content

    -- Metadata
    file_size INTEGER,
    filename VARCHAR(255),
    upload_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Constraints
    CONSTRAINT evidence_summary_not_empty CHECK (LENGTH(TRIM(summary)) > 0),
    CONSTRAINT evidence_content_not_empty CHECK (LENGTH(TRIM(preprocessed_content)) > 0)
);

-- Indexes
CREATE INDEX idx_evidence_case_id ON evidence(case_id);
CREATE INDEX idx_evidence_category ON evidence(category);
CREATE INDEX idx_evidence_upload_timestamp ON evidence(upload_timestamp DESC);
CREATE INDEX idx_evidence_metadata_gin ON evidence USING GIN (metadata);

-- Full-text search on preprocessed content
CREATE INDEX idx_evidence_content_fts ON evidence USING GIN (to_tsvector('english', preprocessed_content));

-- ============================================================================
-- TABLE: hypotheses
-- ============================================================================

CREATE TABLE hypotheses (
    -- Primary Key
    hypothesis_id VARCHAR(15) PRIMARY KEY,

    -- Foreign Key
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Core Attributes
    description TEXT NOT NULL,
    status hypothesis_status NOT NULL DEFAULT 'proposed',
    confidence_score DECIMAL(3,2),  -- 0.00 to 1.00

    -- Supporting Evidence
    supporting_evidence_ids TEXT[],  -- Array of evidence_id references

    -- Validation
    validation_result TEXT,
    validation_timestamp TIMESTAMPTZ,

    -- Timeline
    proposed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Constraints
    CONSTRAINT hypotheses_description_not_empty CHECK (LENGTH(TRIM(description)) > 0),
    CONSTRAINT hypotheses_confidence_range CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1))
);

-- Indexes
CREATE INDEX idx_hypotheses_case_id ON hypotheses(case_id);
CREATE INDEX idx_hypotheses_status ON hypotheses(status);
CREATE INDEX idx_hypotheses_proposed_at ON hypotheses(proposed_at DESC);
CREATE INDEX idx_hypotheses_confidence_score ON hypotheses(confidence_score DESC NULLS LAST);

-- ============================================================================
-- TABLE: solutions
-- ============================================================================

CREATE TABLE solutions (
    -- Primary Key
    solution_id VARCHAR(15) PRIMARY KEY,

    -- Foreign Key
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Core Attributes
    description TEXT NOT NULL,
    status solution_status NOT NULL DEFAULT 'proposed',
    implementation_steps TEXT[],

    -- Risk Assessment
    risk_level VARCHAR(20),  -- 'low', 'medium', 'high', 'critical'
    estimated_effort VARCHAR(50),

    -- Validation
    verification_result TEXT,
    verification_timestamp TIMESTAMPTZ,

    -- Timeline
    proposed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    implemented_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Constraints
    CONSTRAINT solutions_description_not_empty CHECK (LENGTH(TRIM(description)) > 0),
    CONSTRAINT solutions_risk_level_valid CHECK (risk_level IS NULL OR risk_level IN ('low', 'medium', 'high', 'critical'))
);

-- Indexes
CREATE INDEX idx_solutions_case_id ON solutions(case_id);
CREATE INDEX idx_solutions_status ON solutions(status);
CREATE INDEX idx_solutions_proposed_at ON solutions(proposed_at DESC);
CREATE INDEX idx_solutions_risk_level ON solutions(risk_level);

-- ============================================================================
-- TABLE: case_messages
-- ============================================================================

CREATE TABLE case_messages (
    -- Primary Key
    message_id VARCHAR(20) PRIMARY KEY,

    -- Foreign Key
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Core Attributes
    role message_role NOT NULL,
    content TEXT NOT NULL,

    -- Timeline
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Constraints
    CONSTRAINT case_messages_content_not_empty CHECK (LENGTH(TRIM(content)) > 0)
);

-- Indexes
CREATE INDEX idx_case_messages_case_id ON case_messages(case_id);
CREATE INDEX idx_case_messages_timestamp ON case_messages(timestamp ASC);
CREATE INDEX idx_case_messages_role ON case_messages(role);

-- ============================================================================
-- TABLE: uploaded_files
-- ============================================================================

CREATE TABLE uploaded_files (
    -- Primary Key
    file_id VARCHAR(15) PRIMARY KEY,

    -- Foreign Key
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Core Attributes
    filename VARCHAR(255) NOT NULL,
    file_size INTEGER NOT NULL,
    content_type VARCHAR(100),
    storage_path VARCHAR(1000),  -- S3 or local path

    -- Processing
    processing_status file_processing_status NOT NULL DEFAULT 'pending',
    processing_error TEXT,

    -- Timeline
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Constraints
    CONSTRAINT uploaded_files_filename_not_empty CHECK (LENGTH(TRIM(filename)) > 0),
    CONSTRAINT uploaded_files_file_size_positive CHECK (file_size > 0)
);

-- Indexes
CREATE INDEX idx_uploaded_files_case_id ON uploaded_files(case_id);
CREATE INDEX idx_uploaded_files_uploaded_at ON uploaded_files(uploaded_at DESC);
CREATE INDEX idx_uploaded_files_processing_status ON uploaded_files(processing_status);

-- ============================================================================
-- TABLE: case_status_transitions
-- ============================================================================

CREATE TABLE case_status_transitions (
    -- Primary Key
    transition_id SERIAL PRIMARY KEY,

    -- Foreign Key
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Core Attributes
    from_status case_status,
    to_status case_status NOT NULL,
    reason TEXT,

    -- Timeline
    transitioned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb
);

-- Indexes
CREATE INDEX idx_case_status_transitions_case_id ON case_status_transitions(case_id);
CREATE INDEX idx_case_status_transitions_timestamp ON case_status_transitions(transitioned_at DESC);

-- ============================================================================
-- TABLE: case_tags
-- ============================================================================

CREATE TABLE case_tags (
    -- Primary Key
    tag_id SERIAL PRIMARY KEY,

    -- Foreign Key
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Core Attributes
    tag VARCHAR(50) NOT NULL,

    -- Timeline
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Constraints
    CONSTRAINT case_tags_unique UNIQUE (case_id, tag),
    CONSTRAINT case_tags_tag_not_empty CHECK (LENGTH(TRIM(tag)) > 0)
);

-- Indexes
CREATE INDEX idx_case_tags_case_id ON case_tags(case_id);
CREATE INDEX idx_case_tags_tag ON case_tags(tag);

-- ============================================================================
-- TABLE: agent_tool_calls
-- ============================================================================

CREATE TABLE agent_tool_calls (
    -- Primary Key
    call_id VARCHAR(20) PRIMARY KEY,

    -- Foreign Key
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- Core Attributes
    tool_name VARCHAR(100) NOT NULL,
    tool_input JSONB NOT NULL,
    tool_output JSONB,

    -- Status
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- 'pending', 'running', 'success', 'error'
    error_message TEXT,

    -- Performance
    duration_ms INTEGER,

    -- Timeline
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,

    -- Metadata
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Constraints
    CONSTRAINT agent_tool_calls_tool_name_not_empty CHECK (LENGTH(TRIM(tool_name)) > 0),
    CONSTRAINT agent_tool_calls_status_valid CHECK (status IN ('pending', 'running', 'success', 'error'))
);

-- Indexes
CREATE INDEX idx_agent_tool_calls_case_id ON agent_tool_calls(case_id);
CREATE INDEX idx_agent_tool_calls_tool_name ON agent_tool_calls(tool_name);
CREATE INDEX idx_agent_tool_calls_status ON agent_tool_calls(status);
CREATE INDEX idx_agent_tool_calls_started_at ON agent_tool_calls(started_at DESC);
CREATE INDEX idx_agent_tool_calls_tool_input_gin ON agent_tool_calls USING GIN (tool_input);

-- ============================================================================
-- FUNCTIONS
-- ============================================================================

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- TRIGGERS
-- ============================================================================

-- Cases table
CREATE TRIGGER cases_update_updated_at
    BEFORE UPDATE ON cases
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Hypotheses table
CREATE TRIGGER hypotheses_update_updated_at
    BEFORE UPDATE ON hypotheses
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Solutions table
CREATE TRIGGER solutions_update_updated_at
    BEFORE UPDATE ON solutions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- VIEWS
-- ============================================================================

-- Case overview with counts
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
    COUNT(DISTINCT f.file_id) AS file_count
FROM cases c
LEFT JOIN evidence e ON c.case_id = e.case_id
LEFT JOIN hypotheses h ON c.case_id = h.case_id
LEFT JOIN solutions s ON c.case_id = s.case_id
LEFT JOIN case_messages m ON c.case_id = m.case_id
LEFT JOIN uploaded_files f ON c.case_id = f.case_id
GROUP BY c.case_id, c.user_id, c.title, c.status, c.created_at, c.updated_at;

-- Active hypotheses
CREATE VIEW active_hypotheses AS
SELECT
    h.hypothesis_id,
    h.case_id,
    h.description,
    h.status,
    h.confidence_score,
    h.proposed_at,
    c.title AS case_title,
    c.status AS case_status
FROM hypotheses h
JOIN cases c ON h.case_id = c.case_id
WHERE h.status IN ('proposed', 'testing')
ORDER BY h.confidence_score DESC NULLS LAST, h.proposed_at DESC;

-- Recent evidence
CREATE VIEW recent_evidence AS
SELECT
    e.evidence_id,
    e.case_id,
    e.category,
    e.summary,
    e.filename,
    e.file_size,
    e.upload_timestamp,
    c.title AS case_title,
    c.status AS case_status
FROM evidence e
JOIN cases c ON e.case_id = c.case_id
ORDER BY e.upload_timestamp DESC
LIMIT 100;

-- ============================================================================
-- COMMENTS
-- ============================================================================

COMMENT ON TABLE cases IS 'Core case records with hybrid normalization: normalized attributes + JSONB for flexible low-cardinality data';
COMMENT ON TABLE evidence IS 'Evidence artifacts with preprocessed content and S3 references for raw data';
COMMENT ON TABLE hypotheses IS 'Root cause hypotheses with validation tracking';
COMMENT ON TABLE solutions IS 'Proposed solutions with implementation tracking';
COMMENT ON TABLE case_messages IS 'Conversation history between user and AI agent';
COMMENT ON TABLE uploaded_files IS 'File upload metadata and processing status';
COMMENT ON TABLE case_status_transitions IS 'Audit trail of case status changes';
COMMENT ON TABLE case_tags IS 'User-defined tags for case categorization';
COMMENT ON TABLE agent_tool_calls IS 'Agent tool execution audit trail for observability';

COMMENT ON COLUMN cases.consulting IS 'JSONB: {initial_description, context, user_goals} - consulting phase data';
COMMENT ON COLUMN cases.problem_verification IS 'JSONB: {blast_radius, timeline, symptoms} - problem verification phase';
COMMENT ON COLUMN cases.working_conclusion IS 'JSONB: {current_hypothesis, evidence_summary} - working conclusions';
COMMENT ON COLUMN cases.root_cause_conclusion IS 'JSONB: {root_cause, validation_evidence} - final root cause';
COMMENT ON COLUMN cases.path_selection IS 'JSONB: {selected_path, rationale, alternatives} - troubleshooting path';
COMMENT ON COLUMN cases.degraded_mode IS 'JSONB: {enabled, reason, workarounds} - degraded mode state';
COMMENT ON COLUMN cases.escalation_state IS 'JSONB: {escalated, level, assignee} - escalation tracking';
COMMENT ON COLUMN evidence.content_ref IS 'S3 URI or file path for raw content (tier-3 storage)';
COMMENT ON COLUMN evidence.preprocessed_content IS 'Sanitized, summarized content ready for LLM consumption (tier-2 storage)';

-- ============================================================================
-- GRANTS (adjust based on your user/role setup)
-- ============================================================================

-- Example: Grant all privileges to application role
-- GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO faultmaven_app;
-- GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO faultmaven_app;
-- GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO faultmaven_app;

-- ============================================================================
-- MIGRATION COMPLETE
-- ============================================================================

-- To verify migration:
-- SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name;
-- SELECT * FROM case_overview LIMIT 5;
