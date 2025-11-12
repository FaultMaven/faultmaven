# FaultMaven Database Design & Specification v2.0

## Executive Summary

This document defines the complete database schema for FaultMaven's milestone-based investigation system. All schemas align with Investigation Architecture Specification v2.0 and Case Model Design v2.0.

**Database**: PostgreSQL 14+ (with JSONB support)

**Key Characteristics**:
- Milestone-based progress tracking (no phases)
- Two terminal states: RESOLVED (with solution), CLOSED (without solution)
- Turn-based progression (not OODA)
- Purpose-driven evidence categorization
- Complete audit trails

---

## Table of Contents

1. [Database Overview](#1-database-overview)
2. [Core Tables](#2-core-tables)
3. [Investigation Tables](#3-investigation-tables)
4. [Evidence Tables](#4-evidence-tables)
5. [Hypothesis Tables](#5-hypothesis-tables)
6. [Solution Tables](#6-solution-tables)
7. [Tracking Tables](#7-tracking-tables)
8. [Supporting Tables](#8-supporting-tables)
9. [Indexes](#9-indexes)
10. [Constraints & Validation](#10-constraints--validation)
    - 10.5 [JSONB Fields and Pydantic Defaults: Critical Design Pattern](#105-jsonb-fields-and-pydantic-defaults-critical-design-pattern)
11. [Queries](#11-queries)
12. [Performance Optimization](#12-performance-optimization)
13. [Migration Strategy](#13-migration-strategy)
14. [Backup & Recovery](#14-backup--recovery)

---

## 1. Database Overview

### 1.1 Architecture Principles

```
┌─────────────────────────────────────────────────────────────┐
│ Core Entity: Case                                           │
│                                                              │
│ Lifecycle: CONSULTING → INVESTIGATING → RESOLVED/CLOSED     │
│ Progress: Milestone completions (not phases)                │
│ Tracking: Turn-based (not OODA)                             │
└──────────────────┬──────────────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        │                     │
    ┌───▼───┐           ┌─────▼─────┐
    │Related│           │  Tracking │
    │Entities│          │  Entities │
    └───┬───┘           └─────┬─────┘
        │                     │
    • Evidence           • TurnProgress
    • Hypotheses         • StatusTransitions
    • Solutions          • Milestones
    • ProblemVerification
```

### 1.2 Table Groups

| Group | Tables | Purpose |
|-------|--------|---------|
| **Core** | cases, users, organizations | Primary entities |
| **Investigation** | investigation_progress, problem_verification, path_selections | Investigation state |
| **Evidence** | evidence, evidence_milestones | Evidence management |
| **Hypothesis** | hypotheses, hypothesis_evidence | Hypothesis testing |
| **Solution** | solutions, solution_steps, solution_risks | Solution tracking |
| **Tracking** | turn_history, turn_milestones, case_status_transitions | Audit & progress |
| **Supporting** | degraded_mode, escalation_state, documentation | Special states |

### 1.3 Data Storage Strategy

**Relational Tables**: 
- Core entities with frequent queries
- Relationships and foreign keys
- High-cardinality data

**JSONB Columns**:
- Complex nested structures
- Flexible schemas
- Infrequently queried details

---

## 2. Core Tables

### 2.1 cases

```sql
CREATE TABLE cases (
    -- ============================================================
    -- Identity
    -- ============================================================
    case_id VARCHAR(17) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    organization_id VARCHAR(255) NOT NULL,
    title VARCHAR(200) NOT NULL,  -- Short label for headers/lists
    description TEXT DEFAULT '',  -- Confirmed problem description (max 2000 chars, validated in app layer)
    
    -- ============================================================
    -- Status (PRIMARY - User-Facing)
    -- ============================================================
    status VARCHAR(20) NOT NULL DEFAULT 'consulting',
    closure_reason VARCHAR(100),
    investigation_strategy VARCHAR(20) DEFAULT 'post_mortem',
    
    -- NOTE: decided_to_investigate is in consulting JSONB field, not as top-level column
    
    -- ============================================================
    -- Turn Tracking
    -- ============================================================
    current_turn INTEGER DEFAULT 0,
    turns_without_progress INTEGER DEFAULT 0,
    
    -- ============================================================
    -- Timestamps
    -- ============================================================
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMP WITH TIME ZONE,
    closed_at TIMESTAMP WITH TIME ZONE,
    
    -- ============================================================
    -- Complex Nested Data (JSONB)
    -- ============================================================
    consulting JSONB,
    problem_verification JSONB,
    path_selection JSONB,
    uploaded_files JSONB DEFAULT '[]'::jsonb NOT NULL,
    working_conclusion JSONB,
    root_cause_conclusion JSONB,
    degraded_mode JSONB,
    escalation_state JSONB,
    documentation JSONB,
    
    -- ============================================================
    -- Constraints
    -- ============================================================
    CONSTRAINT cases_status_check 
        CHECK (status IN ('consulting', 'investigating', 'resolved', 'closed')),
    
    CONSTRAINT cases_closure_reason_check
        CHECK (
            closure_reason IS NULL OR 
            closure_reason IN ('resolved', 'abandoned', 'escalated', 'consulting_only', 'duplicate', 'other')
        ),
    
    CONSTRAINT cases_strategy_check
        CHECK (investigation_strategy IN ('active_incident', 'post_mortem')),
    
    CONSTRAINT cases_turn_check 
        CHECK (current_turn >= 0 AND turns_without_progress >= 0),
    
    CONSTRAINT cases_resolved_timestamp_check
        CHECK (
            (status = 'resolved' AND resolved_at IS NOT NULL AND closed_at IS NOT NULL) OR
            (status != 'resolved' AND resolved_at IS NULL)
        ),
    
    CONSTRAINT cases_closed_timestamp_check
        CHECK (
            (status IN ('resolved', 'closed') AND closed_at IS NOT NULL) OR
            (status NOT IN ('resolved', 'closed') AND closed_at IS NULL)
        ),
    
    CONSTRAINT cases_closure_reason_required_check
        CHECK (
            (status IN ('resolved', 'closed') AND closure_reason IS NOT NULL) OR
            (status NOT IN ('resolved', 'closed') AND closure_reason IS NULL)
        ),
    
    CONSTRAINT cases_description_required_when_investigating
        CHECK (
            (status != 'investigating') OR
            (status = 'investigating' AND description IS NOT NULL AND description != '')
        ),
    
    CONSTRAINT cases_timestamp_order_check
        CHECK (
            created_at <= updated_at AND
            (resolved_at IS NULL OR created_at <= resolved_at) AND
            (closed_at IS NULL OR created_at <= closed_at) AND
            (resolved_at IS NULL OR closed_at IS NULL OR resolved_at <= closed_at)
        )
);

-- Indexes
CREATE INDEX idx_cases_user_status ON cases(user_id, status);
CREATE INDEX idx_cases_org_status ON cases(organization_id, status);
CREATE INDEX idx_cases_status ON cases(status);
CREATE INDEX idx_cases_created_at ON cases(created_at DESC);
CREATE INDEX idx_cases_updated_at ON cases(updated_at DESC);
CREATE INDEX idx_cases_closed_at ON cases(closed_at DESC) WHERE closed_at IS NOT NULL;
CREATE INDEX idx_cases_stuck ON cases(turns_without_progress) WHERE status = 'investigating' AND turns_without_progress >= 3;

-- JSONB Indexes
CREATE INDEX idx_cases_temporal_state ON cases((problem_verification->>'temporal_state')) WHERE problem_verification IS NOT NULL;
CREATE INDEX idx_cases_urgency_level ON cases((problem_verification->>'urgency_level')) WHERE problem_verification IS NOT NULL;
CREATE INDEX idx_cases_path ON cases((path_selection->>'path')) WHERE path_selection IS NOT NULL;
CREATE INDEX idx_cases_degraded_active ON cases((degraded_mode->>'exited_at')) WHERE degraded_mode IS NOT NULL;
CREATE INDEX idx_cases_uploaded_files_gin ON cases USING gin(uploaded_files);

-- Full-text search
CREATE INDEX idx_cases_title_fts ON cases USING gin(to_tsvector('english', title));
CREATE INDEX idx_cases_description_fts ON cases USING gin(to_tsvector('english', description));
-- Combined search (title + description)
CREATE INDEX idx_cases_title_description_fts ON cases USING gin(to_tsvector('english', title || ' ' || description));

COMMENT ON TABLE cases IS 'Root case entity representing one complete troubleshooting investigation';
COMMENT ON COLUMN cases.title IS 'Short case title for list views and headers (max 200 chars)';
COMMENT ON COLUMN cases.description IS 'Confirmed problem description - set when user confirms proposed_problem_statement (max 2000 chars, validated in app layer)';
COMMENT ON COLUMN cases.status IS 'Current lifecycle status: consulting | investigating | resolved | closed';
COMMENT ON COLUMN cases.closure_reason IS 'Why case was closed: resolved (with solution) | abandoned | escalated | consulting_only | duplicate | other';
COMMENT ON COLUMN cases.resolved_at IS 'When case reached RESOLVED status (solution verified)';
COMMENT ON COLUMN cases.closed_at IS 'When case reached terminal state (RESOLVED or CLOSED)';
COMMENT ON COLUMN cases.uploaded_files IS 'JSONB array of raw file metadata (UploadedFile objects) - files uploaded in any phase (CONSULTING or INVESTIGATING). Separate from evidence which is investigation-linked data created only in INVESTIGATING phase.';
-- NOTE: decided_to_investigate is stored in consulting JSONB field, not as a column
```

### 2.2 users

```sql
CREATE TABLE users (
    user_id VARCHAR(255) PRIMARY KEY,
    organization_id VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL UNIQUE,
    display_name VARCHAR(200),
    
    -- Permissions
    role VARCHAR(50) NOT NULL DEFAULT 'user',
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMP WITH TIME ZONE,
    
    CONSTRAINT users_role_check 
        CHECK (role IN ('user', 'admin', 'system_admin'))
);

CREATE INDEX idx_users_org ON users(organization_id);
CREATE INDEX idx_users_email ON users(email);

COMMENT ON TABLE users IS 'User accounts and authentication';
```

### 2.3 organizations

```sql
CREATE TABLE organizations (
    organization_id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    
    -- Settings
    settings JSONB,
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    
    -- Subscription
    plan VARCHAR(50) DEFAULT 'free',
    
    CONSTRAINT organizations_plan_check
        CHECK (plan IN ('free', 'pro', 'enterprise'))
);

COMMENT ON TABLE organizations IS 'Organizations/tenants';
```

---

## 3. Investigation Tables

### 3.1 investigation_progress

```sql
CREATE TABLE investigation_progress (
    case_id VARCHAR(17) PRIMARY KEY REFERENCES cases(case_id) ON DELETE CASCADE,
    
    -- ============================================================
    -- Verification Milestones
    -- ============================================================
    symptom_verified BOOLEAN DEFAULT FALSE,
    scope_assessed BOOLEAN DEFAULT FALSE,
    timeline_established BOOLEAN DEFAULT FALSE,
    changes_identified BOOLEAN DEFAULT FALSE,
    
    -- NOTE: temporal_state moved to problem_verification table (where it logically belongs)
    
    -- ============================================================
    -- Investigation Milestones
    -- ============================================================
    root_cause_identified BOOLEAN DEFAULT FALSE,
    root_cause_confidence REAL DEFAULT 0.0,
    root_cause_method VARCHAR(50),
    
    -- ============================================================
    -- Resolution Milestones
    -- ============================================================
    solution_proposed BOOLEAN DEFAULT FALSE,
    solution_applied BOOLEAN DEFAULT FALSE,
    solution_verified BOOLEAN DEFAULT FALSE,
    
    -- ============================================================
    -- Timestamps
    -- ============================================================
    verification_completed_at TIMESTAMP WITH TIME ZONE,
    investigation_completed_at TIMESTAMP WITH TIME ZONE,
    resolution_completed_at TIMESTAMP WITH TIME ZONE,
    
    -- ============================================================
    -- Constraints
    -- ============================================================
    CONSTRAINT ip_root_cause_confidence_check
        CHECK (root_cause_confidence >= 0.0 AND root_cause_confidence <= 1.0),
    
    CONSTRAINT ip_root_cause_method_check
        CHECK (
            root_cause_method IS NULL OR 
            root_cause_method IN ('direct_analysis', 'hypothesis_validation', 'correlation', 'other')
        ),
    
    CONSTRAINT ip_root_cause_consistency_check
        CHECK (
            (NOT root_cause_identified) OR 
            (root_cause_identified AND root_cause_confidence > 0.0 AND root_cause_method IS NOT NULL)
        ),
    
    CONSTRAINT ip_solution_ordering_check
        CHECK (
            (NOT solution_applied OR solution_proposed) AND
            (NOT solution_verified OR solution_applied)
        )
);

-- Indexes for milestone queries
CREATE INDEX idx_ip_verification_complete ON investigation_progress(symptom_verified, scope_assessed, timeline_established, changes_identified);
CREATE INDEX idx_ip_rca_complete ON investigation_progress(root_cause_identified);
CREATE INDEX idx_ip_solution_complete ON investigation_progress(solution_verified);

COMMENT ON TABLE investigation_progress IS 'Milestone-based progress tracking';
COMMENT ON COLUMN investigation_progress.root_cause_method IS 'How root cause was identified: direct_analysis | hypothesis_validation | correlation | other';
-- NOTE: temporal_state is in problem_verification table, not here
```

### 3.2 problem_verification

```sql
CREATE TABLE problem_verification (
    case_id VARCHAR(17) PRIMARY KEY REFERENCES cases(case_id) ON DELETE CASCADE,
    
    -- ============================================================
    -- Symptom
    -- ============================================================
    symptom_statement VARCHAR(1000) NOT NULL,
    symptom_indicators TEXT[],
    
    -- ============================================================
    -- Scope
    -- ============================================================
    affected_services TEXT[],
    affected_users VARCHAR(200),
    affected_regions TEXT[],
    severity VARCHAR(50) NOT NULL,
    user_impact VARCHAR(1000),
    
    -- ============================================================
    -- Timeline
    -- ============================================================
    started_at TIMESTAMP WITH TIME ZONE,
    noticed_at TIMESTAMP WITH TIME ZONE,
    resolved_naturally_at TIMESTAMP WITH TIME ZONE,
    duration INTERVAL,
    temporal_state VARCHAR(20),
    
    -- ============================================================
    -- Changes & Correlations (JSONB for flexibility)
    -- ============================================================
    recent_changes JSONB,
    correlations JSONB,
    correlation_confidence REAL DEFAULT 0.0,
    
    -- ============================================================
    -- Urgency
    -- ============================================================
    urgency_level VARCHAR(20) DEFAULT 'unknown',
    urgency_factors TEXT[],
    
    -- ============================================================
    -- Metadata
    -- ============================================================
    verified_at TIMESTAMP WITH TIME ZONE,
    verification_confidence REAL DEFAULT 0.0,
    
    -- ============================================================
    -- Constraints
    -- ============================================================
    CONSTRAINT pv_severity_check
        CHECK (severity IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')),
    
    CONSTRAINT pv_temporal_state_check
        CHECK (temporal_state IS NULL OR temporal_state IN ('ongoing', 'historical')),
    
    CONSTRAINT pv_urgency_level_check
        CHECK (urgency_level IN ('critical', 'high', 'medium', 'low', 'unknown')),
    
    CONSTRAINT pv_correlation_confidence_check
        CHECK (correlation_confidence >= 0.0 AND correlation_confidence <= 1.0),
    
    CONSTRAINT pv_verification_confidence_check
        CHECK (verification_confidence >= 0.0 AND verification_confidence <= 1.0),
    
    CONSTRAINT pv_timeline_order_check
        CHECK (
            (started_at IS NULL OR noticed_at IS NULL OR started_at <= noticed_at) AND
            (started_at IS NULL OR resolved_naturally_at IS NULL OR started_at <= resolved_naturally_at) AND
            (noticed_at IS NULL OR resolved_naturally_at IS NULL OR noticed_at <= resolved_naturally_at)
        )
);

-- Indexes
CREATE INDEX idx_pv_severity ON problem_verification(severity);
CREATE INDEX idx_pv_temporal_state ON problem_verification(temporal_state);
CREATE INDEX idx_pv_urgency_level ON problem_verification(urgency_level);
CREATE INDEX idx_pv_started_at ON problem_verification(started_at);

COMMENT ON TABLE problem_verification IS 'Consolidated problem verification data (symptom, scope, timeline, changes)';
```

### 3.3 path_selections

```sql
CREATE TABLE path_selections (
    case_id VARCHAR(17) PRIMARY KEY REFERENCES cases(case_id) ON DELETE CASCADE,
    
    path VARCHAR(20) NOT NULL,
    auto_selected BOOLEAN NOT NULL,
    rationale VARCHAR(500) NOT NULL,
    alternate_path VARCHAR(20),
    
    selected_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    selected_by VARCHAR(255) NOT NULL,
    
    -- Decision inputs
    temporal_state VARCHAR(20),
    urgency_level VARCHAR(20),
    
    CONSTRAINT ps_path_check
        CHECK (path IN ('mitigation_first', 'root_cause', 'user_choice')),

    CONSTRAINT ps_alternate_path_check
        CHECK (alternate_path IS NULL OR alternate_path IN ('mitigation_first', 'root_cause', 'user_choice'))
);

CREATE INDEX idx_ps_path ON path_selections(path);

COMMENT ON TABLE path_selections IS 'Investigation path selection (MITIGATION_FIRST vs ROOT_CAUSE vs USER_CHOICE)';
```

---

## 4. Uploaded Files & Evidence Tables

### 4.1 uploaded_files

**Purpose**: Track raw file metadata for all files uploaded to a case (any phase).

**Key Distinction**:
- `uploaded_files`: Raw file metadata (file_id, filename, size) - exists in ANY phase (CONSULTING or INVESTIGATING)
- `evidence`: Investigation-linked data derived from files - ONLY exists in INVESTIGATING phase

```sql
CREATE TABLE uploaded_files (
    -- ============================================================
    -- Identity
    -- ============================================================
    file_id VARCHAR(20) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,

    -- ============================================================
    -- File Metadata
    -- ============================================================
    filename VARCHAR(255) NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    data_type VARCHAR(50) NOT NULL,

    -- ============================================================
    -- Upload Context
    -- ============================================================
    uploaded_at_turn INTEGER NOT NULL CHECK (uploaded_at_turn >= 0),
    uploaded_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    source_type VARCHAR(50) NOT NULL DEFAULT 'file_upload',

    -- ============================================================
    -- Processing
    -- ============================================================
    preprocessing_summary VARCHAR(500),
    content_ref VARCHAR(1000) NOT NULL,

    -- ============================================================
    -- Constraints
    -- ============================================================
    CONSTRAINT uploaded_files_source_type_check
        CHECK (source_type IN ('file_upload', 'paste', 'screenshot', 'page_injection', 'agent_generated'))
);

-- Indexes
CREATE INDEX idx_uploaded_files_case ON uploaded_files(case_id, uploaded_at DESC);
CREATE INDEX idx_uploaded_files_source_type ON uploaded_files(source_type);
CREATE INDEX idx_uploaded_files_turn ON uploaded_files(uploaded_at_turn);

COMMENT ON TABLE uploaded_files IS 'Raw file metadata for all files uploaded to cases (any phase). Separate from evidence which is investigation-linked data created only in INVESTIGATING phase.';
COMMENT ON COLUMN uploaded_files.file_id IS 'Unique file identifier (matches data_id from data service)';
COMMENT ON COLUMN uploaded_files.content_ref IS 'Reference to stored file content (S3 URI or data service ID)';
COMMENT ON COLUMN uploaded_files.preprocessing_summary IS 'Brief summary from preprocessing pipeline (max 500 chars)';
```

### 4.2 evidence

```sql
CREATE TABLE evidence (
    evidence_id VARCHAR(15) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    
    -- ============================================================
    -- Purpose Classification
    -- ============================================================
    category VARCHAR(30) NOT NULL,
    primary_purpose VARCHAR(100) NOT NULL,
    
    -- ============================================================
    -- Content (Three-Tier Storage)
    -- ============================================================
    summary VARCHAR(500) NOT NULL,
    preprocessed_content TEXT NOT NULL,
    content_ref VARCHAR(1000) NOT NULL,
    content_size_bytes BIGINT NOT NULL,
    preprocessing_method VARCHAR(50) NOT NULL,
    compression_ratio REAL,
    analysis TEXT,
    
    -- ============================================================
    -- Source
    -- ============================================================
    source_type VARCHAR(50) NOT NULL,
    form VARCHAR(20) NOT NULL,
    
    -- ============================================================
    -- Metadata
    -- ============================================================
    collected_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    collected_by VARCHAR(255) NOT NULL,
    collected_at_turn INTEGER NOT NULL,
    
    -- ============================================================
    -- Constraints
    -- ============================================================
    CONSTRAINT evidence_category_check
        CHECK (category IN ('symptom_evidence', 'causal_evidence', 'resolution_evidence', 'other')),
    
    CONSTRAINT evidence_form_check
        CHECK (form IN ('document', 'user_input')),
    
    CONSTRAINT evidence_source_type_check
        CHECK (source_type IN (
            'log_file', 'metrics_data', 'config_file', 'code_review', 'screenshot',
            'command_output', 'database_query', 'trace_data', 'api_response',
            'user_report', 'monitoring_alert', 'other'
        )),
    
    CONSTRAINT evidence_turn_check
        CHECK (collected_at_turn >= 0)
);

-- Indexes
CREATE INDEX idx_evidence_case ON evidence(case_id, collected_at DESC);
CREATE INDEX idx_evidence_category ON evidence(category);
CREATE INDEX idx_evidence_turn ON evidence(case_id, collected_at_turn);

COMMENT ON TABLE evidence IS 'Evidence collected during investigation (independent of hypotheses)';
COMMENT ON COLUMN evidence.category IS 'System-inferred category based on evaluation results: symptom_evidence | causal_evidence | resolution_evidence | other';
COMMENT ON COLUMN evidence.summary IS 'Brief summary (<500 chars) for UI display';
COMMENT ON COLUMN evidence.preprocessed_content IS 'Extracted relevant content from preprocessing (5-50KB crime scene, anomalies, etc.) - used for hypothesis evaluation';
COMMENT ON COLUMN evidence.content_ref IS 'S3 URI to original raw file (1-10MB) for audit and deep dive';
COMMENT ON COLUMN evidence.content_size_bytes IS 'Size of original raw file in bytes';
COMMENT ON COLUMN evidence.preprocessing_method IS 'Method used: crime_scene_extraction, anomaly_detection, parse_and_sanitize, etc.';
COMMENT ON COLUMN evidence.compression_ratio IS 'Ratio of preprocessed to raw (e.g., 0.005 = 200:1 compression)';
```

### 4.3 evidence_milestones

```sql
CREATE TABLE evidence_milestones (
    evidence_id VARCHAR(15) NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    milestone_name VARCHAR(50) NOT NULL,
    
    PRIMARY KEY (evidence_id, milestone_name)
);

CREATE INDEX idx_em_milestone ON evidence_milestones(milestone_name);

COMMENT ON TABLE evidence_milestones IS 'Which milestones each evidence advanced';
```


## 5. Hypothesis Tables

### 5.1 hypotheses

```sql
CREATE TABLE hypotheses (
    hypothesis_id VARCHAR(15) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    
    statement VARCHAR(500) NOT NULL,
    category VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'captured',
    likelihood REAL NOT NULL DEFAULT 0.5,
    
    -- Metadata
    generated_at_turn INTEGER NOT NULL,
    generation_mode VARCHAR(30) NOT NULL,
    rationale VARCHAR(1000) NOT NULL,
    
    -- Testing history
    tested_at TIMESTAMP WITH TIME ZONE,
    concluded_at TIMESTAMP WITH TIME ZONE,
    
    CONSTRAINT hypotheses_category_check
        CHECK (category IN (
            'code', 'config', 'environment', 'network', 'data', 
            'hardware', 'external', 'human', 'other'
        )),
    
    CONSTRAINT hypotheses_status_check
        CHECK (status IN (
            'captured', 'active', 'validated', 'refuted', 'inconclusive', 'retired'
        )),
    
    CONSTRAINT hypotheses_likelihood_check
        CHECK (likelihood >= 0.0 AND likelihood <= 1.0),
    
    CONSTRAINT hypotheses_generation_mode_check
        CHECK (generation_mode IN ('opportunistic', 'systematic', 'forced_alternative')),
    
    CONSTRAINT hypotheses_turn_check
        CHECK (generated_at_turn >= 0),
    
    CONSTRAINT hypotheses_concluded_status_check
        CHECK (
            concluded_at IS NULL OR 
            status IN ('validated', 'refuted', 'retired')
        )
);

-- Indexes
CREATE INDEX idx_hypotheses_case ON hypotheses(case_id, generated_at_turn);
CREATE INDEX idx_hypotheses_status ON hypotheses(status);
CREATE INDEX idx_hypotheses_category ON hypotheses(category);
CREATE INDEX idx_hypotheses_case_status ON hypotheses(case_id, status);

COMMENT ON TABLE hypotheses IS 'Generated hypotheses for root cause exploration';
COMMENT ON COLUMN hypotheses.category IS 'For anchoring detection: code | config | environment | network | data | hardware | external | human | other';
COMMENT ON COLUMN hypotheses.generation_mode IS 'How generated: opportunistic | systematic | forced_alternative';
```

### 5.2 hypothesis_evidence

```sql
CREATE TABLE hypothesis_evidence (
    hypothesis_id VARCHAR(15) NOT NULL REFERENCES hypotheses(hypothesis_id) ON DELETE CASCADE,
    evidence_id VARCHAR(15) NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    
    stance VARCHAR(30) NOT NULL,
    reasoning TEXT NOT NULL,
    completeness REAL NOT NULL,
    analyzed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    
    PRIMARY KEY (hypothesis_id, evidence_id),
    
    CONSTRAINT he_stance_check
        CHECK (stance IN (
            'strongly_supports', 'supports', 'neutral', 
            'contradicts', 'strongly_contradicts', 'irrelevant'
        )),
    
    CONSTRAINT he_completeness_check
        CHECK (completeness >= 0.0 AND completeness <= 1.0)
);

-- Indexes
CREATE INDEX idx_he_hypothesis ON hypothesis_evidence(hypothesis_id);
CREATE INDEX idx_he_evidence ON hypothesis_evidence(evidence_id);
CREATE INDEX idx_he_stance ON hypothesis_evidence(stance) 
    WHERE stance IN ('strongly_supports', 'strongly_contradicts');

COMMENT ON TABLE hypothesis_evidence IS 'Many-to-many relationship between hypotheses and evidence';
COMMENT ON COLUMN hypothesis_evidence.stance IS 'How evidence relates to hypothesis: strongly_supports | supports | neutral | contradicts | strongly_contradicts | irrelevant';
COMMENT ON COLUMN hypothesis_evidence.reasoning IS 'LLM explanation of relationship';
COMMENT ON COLUMN hypothesis_evidence.completeness IS 'How well this evidence tests THIS hypothesis (0.0-1.0)';
```


## 6. Solution Tables

### 6.1 solutions

```sql
CREATE TABLE solutions (
    solution_id VARCHAR(15) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    
    solution_type VARCHAR(30) NOT NULL,
    title VARCHAR(200) NOT NULL,
    
    immediate_action TEXT,
    longterm_fix TEXT,
    
    -- Lifecycle
    proposed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    proposed_by VARCHAR(255) NOT NULL,
    applied_at TIMESTAMP WITH TIME ZONE,
    applied_by VARCHAR(255),
    verified_at TIMESTAMP WITH TIME ZONE,
    
    -- Verification
    verification_method VARCHAR(500),
    verification_evidence_id VARCHAR(15),
    effectiveness REAL,
    
    CONSTRAINT solutions_type_check
        CHECK (solution_type IN (
            'rollback', 'config_change', 'restart', 'scaling', 'code_fix',
            'workaround', 'infrastructure', 'data_fix', 'other'
        )),
    
    CONSTRAINT solutions_effectiveness_check
        CHECK (effectiveness IS NULL OR (effectiveness >= 0.0 AND effectiveness <= 1.0)),
    
    CONSTRAINT solutions_verification_consistency_check
        CHECK (
            (verified_at IS NULL AND effectiveness IS NULL) OR
            (verified_at IS NOT NULL AND effectiveness IS NOT NULL)
        ),
    
    CONSTRAINT solutions_content_check
        CHECK (
            immediate_action IS NOT NULL OR 
            longterm_fix IS NOT NULL
        )
);

-- Indexes
CREATE INDEX idx_solutions_case ON solutions(case_id, proposed_at DESC);
CREATE INDEX idx_solutions_type ON solutions(solution_type);
CREATE INDEX idx_solutions_verification_evidence ON solutions(verification_evidence_id) WHERE verification_evidence_id IS NOT NULL;

ALTER TABLE solutions
    ADD CONSTRAINT fk_solutions_evidence
    FOREIGN KEY (verification_evidence_id)
    REFERENCES evidence(evidence_id)
    ON DELETE SET NULL;

COMMENT ON TABLE solutions IS 'Proposed and applied solutions';
```

### 6.2 solution_steps

```sql
CREATE TABLE solution_steps (
    step_id SERIAL PRIMARY KEY,
    solution_id VARCHAR(15) NOT NULL REFERENCES solutions(solution_id) ON DELETE CASCADE,
    
    step_number INTEGER NOT NULL,
    description TEXT NOT NULL,
    command TEXT,
    
    CONSTRAINT ss_step_number_check
        CHECK (step_number > 0),
    
    UNIQUE (solution_id, step_number)
);

CREATE INDEX idx_ss_solution ON solution_steps(solution_id, step_number);

COMMENT ON TABLE solution_steps IS 'Step-by-step implementation instructions for solutions';
```

### 6.3 solution_risks

```sql
CREATE TABLE solution_risks (
    risk_id SERIAL PRIMARY KEY,
    solution_id VARCHAR(15) NOT NULL REFERENCES solutions(solution_id) ON DELETE CASCADE,
    
    description VARCHAR(500) NOT NULL
);

CREATE INDEX idx_sr_solution ON solution_risks(solution_id);

COMMENT ON TABLE solution_risks IS 'Risks or side effects of solutions';
```

---

## 7. Tracking Tables

### 7.1 turn_history

```sql
CREATE TABLE turn_history (
    turn_id SERIAL PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    
    turn_number INTEGER NOT NULL,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    
    progress_made BOOLEAN NOT NULL,
    outcome VARCHAR(30) NOT NULL,
    
    -- Optional summaries
    user_message_summary VARCHAR(500),
    agent_response_summary VARCHAR(500),
    
    CONSTRAINT th_turn_number_check
        CHECK (turn_number >= 0),
    
    CONSTRAINT th_outcome_check
        CHECK (outcome IN (
            'milestone_completed', 'data_provided', 'data_requested', 'data_not_provided',
            'hypothesis_tested', 'case_resolved', 'conversation', 'other'
        )),
    
    UNIQUE (case_id, turn_number)
);

-- Indexes
CREATE INDEX idx_th_case ON turn_history(case_id, turn_number);
CREATE INDEX idx_th_timestamp ON turn_history(timestamp DESC);
CREATE INDEX idx_th_outcome ON turn_history(outcome);

COMMENT ON TABLE turn_history IS 'Record of all turns (user message + agent response)';
COMMENT ON COLUMN turn_history.outcome IS 'LLM-observable outcome (what happened this turn) - not used for workflow control';
COMMENT ON COLUMN turn_history.progress_made IS 'Whether investigation advanced this turn (system-calculated)';
```

### 7.2 turn_milestones

```sql
CREATE TABLE turn_milestones (
    turn_id INTEGER NOT NULL REFERENCES turn_history(turn_id) ON DELETE CASCADE,
    milestone_name VARCHAR(50) NOT NULL,
    
    PRIMARY KEY (turn_id, milestone_name)
);

CREATE INDEX idx_tm_milestone ON turn_milestones(milestone_name);

COMMENT ON TABLE turn_milestones IS 'Milestones completed in each turn';
```

### 7.3 turn_evidence

```sql
CREATE TABLE turn_evidence (
    turn_id INTEGER NOT NULL REFERENCES turn_history(turn_id) ON DELETE CASCADE,
    evidence_id VARCHAR(15) NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    
    PRIMARY KEY (turn_id, evidence_id)
);

COMMENT ON TABLE turn_evidence IS 'Evidence added in each turn';
```

### 7.4 turn_hypotheses

```sql
CREATE TABLE turn_hypotheses (
    turn_id INTEGER NOT NULL REFERENCES turn_history(turn_id) ON DELETE CASCADE,
    hypothesis_id VARCHAR(15) NOT NULL REFERENCES hypotheses(hypothesis_id) ON DELETE CASCADE,
    action VARCHAR(20) NOT NULL,
    
    PRIMARY KEY (turn_id, hypothesis_id),
    
    CONSTRAINT th_action_check
        CHECK (action IN ('generated', 'validated', 'refuted'))
);

COMMENT ON TABLE turn_hypotheses IS 'Hypotheses generated or validated in each turn';
```

### 7.5 turn_solutions

```sql
CREATE TABLE turn_solutions (
    turn_id INTEGER NOT NULL REFERENCES turn_history(turn_id) ON DELETE CASCADE,
    solution_id VARCHAR(15) NOT NULL REFERENCES solutions(solution_id) ON DELETE CASCADE,
    
    PRIMARY KEY (turn_id, solution_id)
);

COMMENT ON TABLE turn_solutions IS 'Solutions proposed in each turn';
```

### 7.6 case_status_transitions

```sql
CREATE TABLE case_status_transitions (
    transition_id SERIAL PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    
    from_status VARCHAR(20) NOT NULL,
    to_status VARCHAR(20) NOT NULL,
    triggered_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    triggered_by VARCHAR(255) NOT NULL,
    reason VARCHAR(500) NOT NULL,
    
    CONSTRAINT cst_from_status_check
        CHECK (from_status IN ('consulting', 'investigating', 'resolved', 'closed')),
    
    CONSTRAINT cst_to_status_check
        CHECK (to_status IN ('consulting', 'investigating', 'resolved', 'closed')),
    
    CONSTRAINT cst_valid_transition_check
        CHECK (
            -- CONSULTING can go to INVESTIGATING or CLOSED
            (from_status = 'consulting' AND to_status IN ('investigating', 'closed')) OR
            -- INVESTIGATING can go to RESOLVED or CLOSED
            (from_status = 'investigating' AND to_status IN ('resolved', 'closed')) OR
            -- Initial transition (same status)
            (from_status = to_status)
        )
);

-- Indexes
CREATE INDEX idx_cst_case ON case_status_transitions(case_id, triggered_at);
CREATE INDEX idx_cst_transition_type ON case_status_transitions(from_status, to_status);

COMMENT ON TABLE case_status_transitions IS 'Audit trail of all status changes';
```

---

## 8. Supporting Tables

### 8.1 degraded_modes

```sql
CREATE TABLE degraded_modes (
    case_id VARCHAR(17) PRIMARY KEY REFERENCES cases(case_id) ON DELETE CASCADE,
    
    mode_type VARCHAR(30) NOT NULL,
    entered_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    reason VARCHAR(1000) NOT NULL,
    attempted_actions TEXT[],
    
    -- Fallback
    fallback_offered VARCHAR(1000),
    user_choice VARCHAR(100),
    
    -- Exit
    exited_at TIMESTAMP WITH TIME ZONE,
    exit_reason VARCHAR(500),
    
    CONSTRAINT dm_mode_type_check
        CHECK (mode_type IN (
            'no_progress', 'limited_data', 'user_blocked',
            'hypothesis_deadlock', 'external_dependency', 'other'
        )),
    
    CONSTRAINT dm_user_choice_check
        CHECK (
            user_choice IS NULL OR
            user_choice IN ('accept_fallback', 'provide_more_data', 'escalate', 'abandon')
        )
);

CREATE INDEX idx_dm_active ON degraded_modes(case_id) WHERE exited_at IS NULL;

COMMENT ON TABLE degraded_modes IS 'Investigation degraded mode (blocked or struggling)';
```

### 8.2 escalation_states

```sql
CREATE TABLE escalation_states (
    case_id VARCHAR(17) PRIMARY KEY REFERENCES cases(case_id) ON DELETE CASCADE,
    
    escalation_type VARCHAR(30) NOT NULL,
    reason VARCHAR(1000) NOT NULL,
    escalated_to VARCHAR(200),
    escalated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    
    context_summary TEXT NOT NULL,
    key_findings TEXT[],
    
    resolution TEXT,
    resolved_at TIMESTAMP WITH TIME ZONE,
    
    CONSTRAINT es_escalation_type_check
        CHECK (escalation_type IN (
            'expertise_required', 'permissions_required', 'no_progress',
            'user_request', 'critical_severity', 'other'
        ))
);

CREATE INDEX idx_es_active ON escalation_states(case_id) WHERE resolved_at IS NULL;

COMMENT ON TABLE escalation_states IS 'Investigation escalated to human expert';
```

### 8.3 documentation

```sql
CREATE TABLE documentation (
    case_id VARCHAR(17) PRIMARY KEY REFERENCES cases(case_id) ON DELETE CASCADE,
    
    runbook_entry TEXT,
    post_mortem_id VARCHAR(50),
    
    lessons_learned TEXT[],
    what_went_well TEXT[],
    what_could_improve TEXT[],
    
    preventive_measures TEXT[],
    monitoring_recommendations TEXT[],
    
    generated_at TIMESTAMP WITH TIME ZONE,
    generated_by VARCHAR(255) DEFAULT 'agent'
);

COMMENT ON TABLE documentation IS 'Documentation generated when case closes';
```

### 8.4 generated_documents

```sql
CREATE TABLE generated_documents (
    document_id VARCHAR(15) PRIMARY KEY,
    case_id VARCHAR(17) NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    
    document_type VARCHAR(30) NOT NULL,
    title VARCHAR(200) NOT NULL,
    content_ref VARCHAR(1000) NOT NULL,
    
    generated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    format VARCHAR(50) NOT NULL,
    size_bytes INTEGER,
    
    CONSTRAINT gd_document_type_check
        CHECK (document_type IN (
            'incident_report', 'post_mortem', 'runbook', 'chat_summary',
            'timeline', 'evidence_bundle', 'other'
        )),
    
    CONSTRAINT gd_format_check
        CHECK (format IN ('markdown', 'pdf', 'html', 'json', 'txt', 'other')),
    
    CONSTRAINT gd_size_check
        CHECK (size_bytes IS NULL OR size_bytes >= 0)
);

CREATE INDEX idx_gd_case ON generated_documents(case_id, generated_at DESC);
CREATE INDEX idx_gd_type ON generated_documents(document_type);

COMMENT ON TABLE generated_documents IS 'Generated document artifacts';
```

---

## 9. Indexes

### 9.1 Performance Indexes

```sql
-- Common query patterns

-- Find active investigations
CREATE INDEX idx_active_investigations 
    ON cases(organization_id, updated_at DESC) 
    WHERE status = 'investigating';

-- Find stuck investigations
CREATE INDEX idx_stuck_investigations 
    ON cases(organization_id, turns_without_progress DESC) 
    WHERE status = 'investigating' AND turns_without_progress >= 3;

-- Find recent completions
CREATE INDEX idx_recent_completions 
    ON cases(organization_id, closed_at DESC) 
    WHERE status IN ('resolved', 'closed');

-- Find cases by milestone completion
CREATE INDEX idx_cases_needing_rca 
    ON investigation_progress(case_id) 
    WHERE symptom_verified = TRUE AND root_cause_identified = FALSE;

-- Find validated hypotheses
CREATE INDEX idx_validated_hypotheses 
    ON hypotheses(case_id) 
    WHERE status = 'validated';
```

### 9.2 Analytics Indexes

```sql
-- Time-to-resolution analytics
CREATE INDEX idx_resolution_time 
    ON cases(organization_id, created_at, closed_at) 
    WHERE status IN ('resolved', 'closed');

-- Evidence collection patterns
CREATE INDEX idx_evidence_category_analysis 
    ON evidence(case_id, category, collected_at_turn);

-- Hypothesis success rate
CREATE INDEX idx_hypothesis_outcomes 
    ON hypotheses(case_id, status, category);

-- Solution effectiveness
CREATE INDEX idx_solution_effectiveness 
    ON solutions(case_id, solution_type, effectiveness) 
    WHERE effectiveness IS NOT NULL;
```

---

## 10. Constraints & Validation

### 10.1 Database Functions

```sql
-- Function to validate status transitions
CREATE OR REPLACE FUNCTION validate_status_transition()
RETURNS TRIGGER AS $$
BEGIN
    -- Terminal states cannot transition
    IF OLD.status IN ('resolved', 'closed') AND NEW.status != OLD.status THEN
        RAISE EXCEPTION 'Cannot transition from terminal state %', OLD.status;
    END IF;
    
    -- INVESTIGATING requires problem confirmation AND user commitment (in consulting JSONB)
    IF NEW.status = 'investigating' THEN
        IF NEW.consulting IS NULL THEN
            RAISE EXCEPTION 'Cannot transition to investigating without consulting data';
        END IF;
        
        IF NOT COALESCE((NEW.consulting->>'problem_statement_confirmed')::boolean, FALSE) THEN
            RAISE EXCEPTION 'Cannot transition to investigating without confirmed problem statement';
        END IF;
        
        IF NOT COALESCE((NEW.consulting->>'decided_to_investigate')::boolean, FALSE) THEN
            RAISE EXCEPTION 'Cannot transition to investigating without user commitment';
        END IF;
    END IF;
    
    -- RESOLVED requires solution_verified
    IF NEW.status = 'resolved' THEN
        IF NOT EXISTS (
            SELECT 1 FROM investigation_progress 
            WHERE case_id = NEW.case_id AND solution_verified = TRUE
        ) THEN
            RAISE EXCEPTION 'Cannot transition to resolved without solution verification';
        END IF;
    END IF;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tr_validate_status_transition
    BEFORE UPDATE OF status ON cases
    FOR EACH ROW
    EXECUTE FUNCTION validate_status_transition();
```

```sql
-- Function to auto-update timestamps
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tr_cases_updated_at
    BEFORE UPDATE ON cases
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();
```

```sql
-- Function to record status transitions
CREATE OR REPLACE FUNCTION record_status_transition()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status != OLD.status THEN
        INSERT INTO case_status_transitions (
            case_id, from_status, to_status, triggered_by, reason
        ) VALUES (
            NEW.case_id,
            OLD.status,
            NEW.status,
            COALESCE(current_setting('app.user_id', TRUE), 'system'),
            COALESCE(current_setting('app.transition_reason', TRUE), 'Status changed')
        );
    END IF;
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tr_record_status_transition
    AFTER UPDATE OF status ON cases
    FOR EACH ROW
    EXECUTE FUNCTION record_status_transition();
```

### 10.2 Referential Integrity

```sql
-- Ensure evidence for CAUSAL category references valid hypothesis
ALTER TABLE evidence
    ADD CONSTRAINT fk_evidence_hypothesis_valid
    CHECK (
        category != 'causal_evidence' OR
        tests_hypothesis_id IN (SELECT hypothesis_id FROM hypotheses)
    );

-- Ensure solution verification evidence exists
ALTER TABLE solutions
    ADD CONSTRAINT fk_solution_verification_evidence_valid
    CHECK (
        verification_evidence_id IS NULL OR
        verification_evidence_id IN (SELECT evidence_id FROM evidence)
    );
```

---

## 10.5 JSONB Fields and Pydantic Defaults: Critical Design Pattern

### The NULL vs default_factory Mismatch

**Problem**: Pydantic models use `default_factory` for complex fields, but PostgreSQL defaults to NULL.

**Example** (from Case model):
```python
# Pydantic Model (case.py)
class Case(BaseModel):
    consulting: ConsultingData = Field(
        default_factory=ConsultingData,  # Creates empty ConsultingData() when instantiating in Python
        description="Pre-investigation CONSULTING status data"
    )
```

```sql
-- Database Schema (cases table)
CREATE TABLE cases (
    consulting JSONB,  -- Defaults to NULL if not explicitly set!
    ...
);
```

**The Mismatch**:
- **In Python**: `Case()` creates `consulting=ConsultingData()` automatically (via `default_factory`)
- **In Database**: New rows have `consulting=NULL` unless explicitly set during INSERT
- **When Loading**: Repository passes `consulting=NULL` from database → overrides Pydantic default!

### Root Cause Impact

When loading cases from database:
```python
# Repository deserialization (BROKEN):
consulting = ConsultingData(**json.loads(row.consulting))  # ❌ Crashes if row.consulting is NULL!

# Repository deserialization (CORRECT):
consulting = (
    ConsultingData(**json.loads(row.consulting))
    if row.consulting  # ✅ Check for NULL first
    else ConsultingData()  # ✅ Use Pydantic default
)
```

### When This Occurs

NULL values in JSONB fields can occur from:
1. **Old migrations** - Rows created before field was added
2. **Manual database edits** - Direct SQL modifications
3. **Buggy save logic** - Not serializing field properly
4. **Schema evolution** - Adding new fields to existing tables

### Mandatory Pattern for Repository Layer

**ALL fields with `default_factory` MUST be null-checked during deserialization:**

```python
def _row_to_case(self, row) -> Case:
    """Convert database row to Case domain model."""

    # ✅ CORRECT: Fields with default_factory
    consulting = (
        ConsultingData(**json.loads(row.consulting))
        if row.consulting
        else ConsultingData()  # Match Pydantic default
    )

    documentation = (
        DocumentationData(**json.loads(row.documentation))
        if row.documentation
        else DocumentationData()  # Match Pydantic default
    )

    # ✅ CORRECT: Optional fields (explicitly nullable)
    path_selection = (
        PathSelection(**json.loads(row.path_selection))
        if row.path_selection
        else None  # Intentionally nullable
    )

    return Case(
        consulting=consulting,
        documentation=documentation,
        path_selection=path_selection,
        ...
    )
```

### Database Design Decision

**DO NOT add NOT NULL constraints to JSONB fields with `default_factory`:**

```sql
-- ❌ WRONG: Adding NOT NULL without DEFAULT
consulting JSONB NOT NULL,  -- Will break INSERTs that don't specify consulting

-- ❌ WRONG: Adding DEFAULT '{}'
consulting JSONB NOT NULL DEFAULT '{}',  -- Empty object != ConsultingData()

-- ✅ CORRECT: Allow NULL, handle in repository
consulting JSONB,  -- Repository initializes ConsultingData() if NULL
```

**Rationale**:
- PostgreSQL can't know Pydantic's `default_factory` logic
- Database schema defaults (`DEFAULT '{}'`) don't match application semantics
- Repository layer is the correct place to handle NULL → default mapping
- Allows backward compatibility with existing NULL rows

### Testing Requirements

Add regression tests for NULL handling:

```python
async def test_case_with_null_consulting_loads_successfully():
    """Ensure cases with NULL consulting field load without crashing."""
    # Simulate database row with NULL consulting
    case = repository._row_to_case(mock_row_with_null_consulting)

    assert case.consulting is not None
    assert isinstance(case.consulting, ConsultingData)
    assert case.consulting.proposed_problem_statement is None

async def test_all_default_factory_fields_handle_null():
    """Verify all fields with default_factory handle NULL gracefully."""
    test_fields = ['consulting', 'documentation', 'progress', ...]

    for field in test_fields:
        row = create_mock_row_with_null_field(field)
        case = repository._row_to_case(row)

        # Should not crash and should have valid default object
        assert getattr(case, field) is not None
```

### Summary

| Aspect | Database (SQL) | Pydantic (Python) | Repository (Bridge) |
|--------|----------------|-------------------|---------------------|
| **Default** | NULL | `default_factory=Cls()` | Must handle NULL → default |
| **Storage** | JSONB column | Object instance | Serialize/deserialize |
| **Pattern** | Allow NULL | Use `default_factory` | ✅ Null-check before deserializing |
| **Testing** | Migration scripts | Unit tests | Integration tests |

**Key Takeaway**: The repository layer MUST bridge the gap between database NULL values and Pydantic defaults. Never assume JSONB fields match Pydantic's `default_factory` behavior.

---

## 11. Queries

### 11.1 Case Management Queries

```sql
-- Get active cases for user
SELECT 
    c.case_id,
    c.title,
    c.status,
    c.current_turn,
    ip.symptom_verified,
    ip.root_cause_identified,
    ip.solution_verified,
    c.updated_at
FROM cases c
LEFT JOIN investigation_progress ip ON c.case_id = ip.case_id
WHERE c.user_id = :user_id
    AND c.status IN ('consulting', 'investigating')
ORDER BY c.updated_at DESC;

-- Get stuck investigations
SELECT 
    c.case_id,
    c.title,
    c.turns_without_progress,
    c.updated_at,
    ip.symptom_verified,
    ip.root_cause_identified
FROM cases c
JOIN investigation_progress ip ON c.case_id = ip.case_id
WHERE c.status = 'investigating'
    AND c.turns_without_progress >= 3
ORDER BY c.turns_without_progress DESC, c.updated_at DESC;

-- Get case with full details
SELECT 
    c.*,
    ip.*,
    pv.*,
    ps.*,
    (
        SELECT json_agg(e.* ORDER BY e.collected_at)
        FROM evidence e
        WHERE e.case_id = c.case_id
    ) as evidence,
    (
        SELECT json_agg(h.* ORDER BY h.generated_at_turn)
        FROM hypotheses h
        WHERE h.case_id = c.case_id
    ) as hypotheses,
    (
        SELECT json_agg(s.* ORDER BY s.proposed_at)
        FROM solutions s
        WHERE s.case_id = c.case_id
    ) as solutions,
    (
        SELECT json_agg(th.* ORDER BY th.turn_number)
        FROM turn_history th
        WHERE th.case_id = c.case_id
    ) as turn_history
FROM cases c
LEFT JOIN investigation_progress ip ON c.case_id = ip.case_id
LEFT JOIN problem_verification pv ON c.case_id = pv.case_id
LEFT JOIN path_selections ps ON c.case_id = ps.case_id
WHERE c.case_id = :case_id;
```

### 11.2 Progress Tracking Queries

```sql
-- Get cases at each milestone stage
SELECT 
    CASE
        WHEN ip.symptom_verified = FALSE THEN 'Understanding'
        WHEN ip.root_cause_identified = FALSE THEN 'Diagnosing'
        WHEN ip.solution_verified = FALSE THEN 'Resolving'
        ELSE 'Complete'
    END as stage,
    COUNT(*) as case_count
FROM cases c
JOIN investigation_progress ip ON c.case_id = ip.case_id
WHERE c.status = 'investigating'
GROUP BY stage;

-- Get milestone completion rates
SELECT 
    COUNT(*) FILTER (WHERE symptom_verified) * 100.0 / COUNT(*) as symptom_verified_pct,
    COUNT(*) FILTER (WHERE scope_assessed) * 100.0 / COUNT(*) as scope_assessed_pct,
    COUNT(*) FILTER (WHERE timeline_established) * 100.0 / COUNT(*) as timeline_established_pct,
    COUNT(*) FILTER (WHERE changes_identified) * 100.0 / COUNT(*) as changes_identified_pct,
    COUNT(*) FILTER (WHERE root_cause_identified) * 100.0 / COUNT(*) as root_cause_identified_pct,
    COUNT(*) FILTER (WHERE solution_proposed) * 100.0 / COUNT(*) as solution_proposed_pct,
    COUNT(*) FILTER (WHERE solution_applied) * 100.0 / COUNT(*) as solution_applied_pct,
    COUNT(*) FILTER (WHERE solution_verified) * 100.0 / COUNT(*) as solution_verified_pct
FROM investigation_progress;
```

### 11.3 Analytics Queries

```sql
-- Average time to resolution by organization
SELECT 
    c.organization_id,
    COUNT(*) as total_cases,
    AVG(EXTRACT(EPOCH FROM (c.closed_at - c.created_at)) / 3600) as avg_hours_to_close,
    AVG(c.current_turn) as avg_turns,
    COUNT(*) FILTER (WHERE c.status = 'resolved') as resolved_count,
    COUNT(*) FILTER (WHERE c.status = 'closed') as closed_count
FROM cases c
WHERE c.closed_at IS NOT NULL
GROUP BY c.organization_id;

-- Evidence collection patterns
SELECT 
    e.category,
    COUNT(*) as evidence_count,
    AVG(e.collected_at_turn) as avg_turn_collected,
    COUNT(DISTINCT e.case_id) as cases_with_this_evidence
FROM evidence e
GROUP BY e.category
ORDER BY evidence_count DESC;

-- Hypothesis success rates by category
SELECT 
    h.category,
    COUNT(*) as total_hypotheses,
    COUNT(*) FILTER (WHERE h.status = 'validated') as validated_count,
    COUNT(*) FILTER (WHERE h.status = 'refuted') as refuted_count,
    COUNT(*) FILTER (WHERE h.status = 'inconclusive') as inconclusive_count,
    COUNT(*) FILTER (WHERE h.status = 'validated') * 100.0 / COUNT(*) as validation_rate
FROM hypotheses h
GROUP BY h.category
ORDER BY validation_rate DESC;

-- Solution effectiveness by type
SELECT 
    s.solution_type,
    COUNT(*) as total_solutions,
    COUNT(*) FILTER (WHERE s.effectiveness IS NOT NULL) as verified_solutions,
    AVG(s.effectiveness) FILTER (WHERE s.effectiveness IS NOT NULL) as avg_effectiveness
FROM solutions s
GROUP BY s.solution_type
ORDER BY avg_effectiveness DESC NULLS LAST;

-- Path selection distribution
SELECT 
    ps.path,
    ps.auto_selected,
    COUNT(*) as selection_count,
    AVG(EXTRACT(EPOCH FROM (c.closed_at - c.created_at)) / 3600) as avg_hours_to_close
FROM path_selections ps
JOIN cases c ON ps.case_id = c.case_id
WHERE c.closed_at IS NOT NULL
GROUP BY ps.path, ps.auto_selected;
```

### 11.4 Operational Queries

```sql
-- Find cases in degraded mode
SELECT 
    c.case_id,
    c.title,
    dm.mode_type,
    dm.reason,
    dm.entered_at,
    c.turns_without_progress
FROM cases c
JOIN degraded_modes dm ON c.case_id = dm.case_id
WHERE dm.exited_at IS NULL
ORDER BY dm.entered_at;

-- Find escalated cases
SELECT 
    c.case_id,
    c.title,
    es.escalation_type,
    es.reason,
    es.escalated_to,
    es.escalated_at
FROM cases c
JOIN escalation_states es ON c.case_id = es.case_id
WHERE es.resolved_at IS NULL
ORDER BY es.escalated_at;

```

---

## 12. Performance Optimization

### 12.1 Partitioning Strategy

```sql
-- Partition cases by created_at (monthly partitions)
CREATE TABLE cases_partitioned (
    LIKE cases INCLUDING ALL
) PARTITION BY RANGE (created_at);

-- Create partitions for each month
CREATE TABLE cases_2025_01 PARTITION OF cases_partitioned
    FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');

CREATE TABLE cases_2025_02 PARTITION OF cases_partitioned
    FOR VALUES FROM ('2025-02-01') TO ('2025-03-01');

-- ... continue for each month
```

### 12.2 Materialized Views

```sql
-- Case summary view for dashboards
CREATE MATERIALIZED VIEW case_summary_mv AS
SELECT 
    c.case_id,
    c.organization_id,
    c.user_id,
    c.title,
    c.status,
    c.current_turn,
    c.created_at,
    c.closed_at,
    ip.symptom_verified,
    ip.root_cause_identified,
    ip.solution_verified,
    CASE
        WHEN ip.symptom_verified = FALSE THEN 'Understanding'
        WHEN ip.root_cause_identified = FALSE THEN 'Diagnosing'
        WHEN ip.solution_verified = FALSE THEN 'Resolving'
        ELSE 'Complete'
    END as stage,
    (SELECT COUNT(*) FROM evidence WHERE case_id = c.case_id) as evidence_count,
    (SELECT COUNT(*) FROM hypotheses WHERE case_id = c.case_id) as hypothesis_count,
    (SELECT COUNT(*) FROM solutions WHERE case_id = c.case_id) as solution_count
FROM cases c
LEFT JOIN investigation_progress ip ON c.case_id = ip.case_id;

CREATE UNIQUE INDEX idx_case_summary_mv_case_id ON case_summary_mv(case_id);
CREATE INDEX idx_case_summary_mv_org_status ON case_summary_mv(organization_id, status);

-- Refresh strategy
CREATE OR REPLACE FUNCTION refresh_case_summary_mv()
RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY case_summary_mv;
END;
$$ LANGUAGE plpgsql;

-- Schedule refresh (via pg_cron or external scheduler)
-- SELECT cron.schedule('refresh-case-summary', '*/5 * * * *', 'SELECT refresh_case_summary_mv()');
```

### 12.3 Query Optimization

```sql
-- Use EXPLAIN ANALYZE to optimize slow queries
EXPLAIN ANALYZE
SELECT 
    c.case_id,
    c.title,
    ip.root_cause_identified
FROM cases c
JOIN investigation_progress ip ON c.case_id = ip.case_id
WHERE c.organization_id = :org_id
    AND c.status = 'investigating'
    AND ip.symptom_verified = TRUE
    AND ip.root_cause_identified = FALSE;

-- Add covering index if needed
CREATE INDEX idx_investigation_progress_covering 
    ON investigation_progress(case_id, symptom_verified, root_cause_identified)
    INCLUDE (root_cause_confidence);
```

---

## 13. Migration Strategy

### 13.1 Migration from Old Schema

```sql
-- Migration script: Phase-based to Milestone-based

BEGIN;

-- Step 1: Add new columns to existing cases table
ALTER TABLE cases
    ADD COLUMN IF NOT EXISTS current_turn INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS turns_without_progress INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS consulting JSONB,
    ADD COLUMN IF NOT EXISTS problem_verification JSONB,
    ADD COLUMN IF NOT EXISTS path_selection JSONB,
    ADD COLUMN IF NOT EXISTS uploaded_files JSONB DEFAULT '[]'::jsonb NOT NULL,
    ADD COLUMN IF NOT EXISTS working_conclusion JSONB,
    ADD COLUMN IF NOT EXISTS root_cause_conclusion JSONB;
    -- NOTE: decided_to_investigate is in consulting JSONB, not a separate column

-- Step 2: Create investigation_progress table
CREATE TABLE IF NOT EXISTS investigation_progress (
    -- ... (as defined above)
);

-- Step 3: Migrate phase data to milestones
INSERT INTO investigation_progress (
    case_id,
    symptom_verified,
    scope_assessed,
    timeline_established,
    changes_identified,
    root_cause_identified,
    solution_proposed,
    solution_applied,
    solution_verified
)
SELECT 
    case_id,
    current_phase >= 1 as symptom_verified,
    current_phase >= 1 as scope_assessed,
    current_phase >= 2 as timeline_established,
    current_phase >= 2 as changes_identified,
    current_phase >= 4 as root_cause_identified,
    current_phase >= 5 as solution_proposed,
    current_phase >= 5 as solution_applied,
    current_phase >= 6 as solution_verified
FROM old_cases
ON CONFLICT (case_id) DO NOTHING;

-- Step 4: Migrate old iteration data to turn history
-- NOTE: Old design used OODA terminology; new design uses Turn terminology
INSERT INTO turn_history (
    case_id,
    turn_number,
    timestamp,
    progress_made,
    outcome
)
SELECT 
    case_id,
    iteration_number as turn_number,
    iteration_timestamp as timestamp,
    COALESCE(progress_made, FALSE) as progress_made,
    CASE 
        WHEN outcome = 'hypothesis_validated' THEN 'hypothesis_tested'
        WHEN outcome = 'data_requested' THEN 'data_requested'
        WHEN outcome = 'milestone_completed' THEN 'milestone_completed'
        WHEN outcome = 'resolved' THEN 'case_resolved'
        WHEN outcome = 'data_provided' THEN 'data_provided'
        WHEN outcome = 'conversation' THEN 'conversation'
        -- NOTE: 'blocked' removed from new design (use data_not_provided + turns_without_progress)
        WHEN outcome = 'blocked' THEN 'data_not_provided'
        ELSE 'other'
    END as outcome
FROM old_iterations  -- Old table name (was ooda_iterations)
ORDER BY case_id, iteration_number;

-- Step 5: Update status values if needed
UPDATE cases
SET status = CASE 
    WHEN status = 'active' THEN 'investigating'
    WHEN status = 'completed' AND closure_reason = 'resolved' THEN 'resolved'
    WHEN status = 'completed' THEN 'closed'
    ELSE status
END
WHERE status IN ('active', 'completed');

-- Step 6: Migrate evidence-hypothesis linkages to junction table
CREATE TABLE IF NOT EXISTS hypothesis_evidence (
    hypothesis_id VARCHAR(15) NOT NULL REFERENCES hypotheses(hypothesis_id) ON DELETE CASCADE,
    evidence_id VARCHAR(15) NOT NULL REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    stance VARCHAR(30) NOT NULL,
    reasoning TEXT NOT NULL,
    completeness REAL NOT NULL,
    analyzed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    PRIMARY KEY (hypothesis_id, evidence_id)
);

INSERT INTO hypothesis_evidence (hypothesis_id, evidence_id, stance, reasoning, completeness, analyzed_at)
SELECT 
    tests_hypothesis_id,
    evidence_id,
    CASE 
        WHEN stance = 'strongly_supports' THEN 'strongly_supports'
        WHEN stance = 'supports' THEN 'supports'
        WHEN stance = 'neutral' THEN 'neutral'
        WHEN stance = 'contradicts' THEN 'contradicts'
        WHEN stance = 'strongly_contradicts' THEN 'strongly_contradicts'
        ELSE 'neutral'
    END,
    'Migrated from old single-hypothesis linkage',
    0.8,
    collected_at
FROM evidence
WHERE tests_hypothesis_id IS NOT NULL;

-- Step 7: Drop old evidence columns and tables
ALTER TABLE evidence
    DROP COLUMN IF EXISTS tests_hypothesis_id,
    DROP COLUMN IF EXISTS stance,
    DROP COLUMN IF EXISTS fulfills_request_id;

DROP TABLE IF EXISTS evidence_requests;
DROP TABLE IF EXISTS evidence_requirements;

-- Step 8: Add investigation_strategy to cases
ALTER TABLE cases
    ADD COLUMN IF NOT EXISTS investigation_strategy VARCHAR(20) DEFAULT 'post_mortem'
        CHECK (investigation_strategy IN ('active_incident', 'post_mortem'));

-- Step 9: Drop old phase-based columns
ALTER TABLE cases
    DROP COLUMN IF EXISTS current_phase,
    DROP COLUMN IF EXISTS phase_history;
    
-- NOTE: Old design may have had these iteration-tracking columns
-- DROP COLUMN IF EXISTS ooda_active,
-- DROP COLUMN IF EXISTS ooda_iterations;

COMMIT;
```

### 13.2 Data Validation After Migration

```sql
-- Validate all cases have investigation_progress
SELECT COUNT(*) 
FROM cases c
LEFT JOIN investigation_progress ip ON c.case_id = ip.case_id
WHERE c.status IN ('investigating', 'resolved', 'closed')
    AND ip.case_id IS NULL;
-- Should return 0

-- Validate terminal states have closure_reason
SELECT COUNT(*)
FROM cases
WHERE status IN ('resolved', 'closed')
    AND closure_reason IS NULL;
-- Should return 0

-- Validate turn history continuity
SELECT case_id, COUNT(*), MAX(turn_number)
FROM turn_history
GROUP BY case_id
HAVING COUNT(*) != MAX(turn_number) + 1;
-- Should return empty (turn numbers must be sequential starting from 0)
```

---

## 14. Backup & Recovery

### 14.1 Backup Strategy

```bash
#!/bin/bash
# Daily backup script

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backups/faultmaven"
DB_NAME="faultmaven_production"

# Full backup
pg_dump -Fc $DB_NAME > "$BACKUP_DIR/full_backup_$DATE.dump"

# Backup critical tables separately for faster restore
pg_dump -Fc -t cases -t investigation_progress -t evidence -t hypotheses \
    $DB_NAME > "$BACKUP_DIR/critical_tables_$DATE.dump"

# Compress old backups
find $BACKUP_DIR -name "*.dump" -mtime +7 -exec gzip {} \;

# Delete backups older than 30 days
find $BACKUP_DIR -name "*.dump.gz" -mtime +30 -delete
```

### 14.2 Point-in-Time Recovery

```sql
-- Enable WAL archiving in postgresql.conf
-- wal_level = replica
-- archive_mode = on
-- archive_command = 'cp %p /archive/%f'

-- Create base backup
SELECT pg_start_backup('daily_backup');
-- Copy data directory
SELECT pg_stop_backup();

-- Restore to specific point in time
-- 1. Restore base backup
-- 2. Create recovery.conf:
restore_command = 'cp /archive/%f %p'
recovery_target_time = '2025-11-03 14:30:00'
```

### 14.3 Disaster Recovery Testing

```sql
-- Test restore procedure (on test database)

-- 1. Drop test database
DROP DATABASE IF EXISTS faultmaven_test;

-- 2. Create fresh database
CREATE DATABASE faultmaven_test;

-- 3. Restore from backup
pg_restore -d faultmaven_test /backups/full_backup_latest.dump

-- 4. Validate data integrity
\c faultmaven_test

SELECT COUNT(*) FROM cases;
SELECT COUNT(*) FROM evidence;
SELECT COUNT(*) FROM hypotheses;

-- Check for orphaned records
SELECT COUNT(*) 
FROM evidence e
LEFT JOIN cases c ON e.case_id = c.case_id
WHERE c.case_id IS NULL;
-- Should return 0
```

---

## Appendix A: Complete Schema DDL

```sql
-- Complete schema creation script
-- Run this to create FaultMaven database from scratch

BEGIN;

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Users and Organizations
CREATE TABLE organizations (
    organization_id VARCHAR(255) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    settings JSONB,
    plan VARCHAR(50) DEFAULT 'free',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CONSTRAINT organizations_plan_check CHECK (plan IN ('free', 'pro', 'enterprise'))
);

CREATE TABLE users (
    user_id VARCHAR(255) PRIMARY KEY,
    organization_id VARCHAR(255) NOT NULL REFERENCES organizations(organization_id),
    email VARCHAR(255) NOT NULL UNIQUE,
    display_name VARCHAR(200),
    role VARCHAR(50) NOT NULL DEFAULT 'user',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT users_role_check CHECK (role IN ('user', 'admin', 'system_admin'))
);

-- Core cases table
-- (Full definition from Section 2.1)

-- Investigation tables
-- (Full definitions from Section 3)

-- Evidence tables
-- (Full definitions from Section 4)

-- Hypothesis tables
-- (Full definitions from Section 5)

-- Solution tables
-- (Full definitions from Section 6)

-- Tracking tables
-- (Full definitions from Section 7)

-- Supporting tables
-- (Full definitions from Section 8)

-- All indexes
-- (From Section 9)

-- All triggers and functions
-- (From Section 10)

COMMIT;
```

---

**Document Version**: 2.0  
**Last Updated**: 2025-11-03  
**Status**: Production Specification  
**Alignment**: 
- Investigation Architecture Specification v2.0
- Case Model Design v2.0

**Database**: PostgreSQL 14+  
**Schema**: faultmaven_v2