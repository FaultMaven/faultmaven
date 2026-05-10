# FaultMaven Database ER Diagram

> **Auto-generated** from SQLAlchemy models on 2026-05-10 01:19 UTC.
> Do not edit manually — run `python scripts/generate_er_diagram.py --update` to regenerate.
> Render with any Mermaid-compatible viewer (GitHub, VS Code, Mermaid Live Editor).

## Summary

**32 tables** in the schema.

| Table | Columns | Primary Key | Foreign Keys |
|-------|---------|-------------|--------------|
| `agent_executions` | 17 | `execution_id` | cases, investigation_sessions, organizations |
| `agent_tool_calls` | 13 | `tool_call_id` | agent_executions, organizations |
| `case_actions` | 9 | `transition_id` | cases, organizations |
| `case_checkpoints` | 9 | `checkpoint_id` | cases, organizations |
| `case_entities` | 8 | `case_id, entity_type, entity_value, evidence_id` | cases, evidence, organizations |
| `case_messages` | 9 | `message_id` | cases, organizations |
| `case_tags` | 5 | `tag_id` | cases, organizations |
| `cases` | 26 | `case_id` | organizations, teams, users |
| `conversion_drafts` | 22 | `id` | conversion_jobs, knowledge_items, organizations, users |
| `conversion_jobs` | 13 | `id` | cases, organizations, teams, uploaded_files, users |
| `enterprises` | 11 | `enterprise_id` | — |
| `evidence` | 19 | `evidence_id` | cases, organizations, uploaded_files |
| `hypotheses` | 23 | `hypothesis_id` | cases, organizations, users |
| `hypothesis_evidence` | 8 | `hypothesis_id, evidence_id` | evidence, hypotheses, organizations, users |
| `investigation_sessions` | 17 | `session_id` | cases, organizations, users |
| `knowledge_items` | 29 | `item_id` | organizations, teams, users |
| `knowledge_suggestions` | 26 | `suggestion_id` | cases, knowledge_items, organizations, users |
| `llm_config_overrides` | 4 | `key` | users |
| `oauth_authorization_codes` | 7 | `code` | users |
| `oauth_revoked_tokens` | 3 | `jti` | — |
| `organization_members` | 9 | `user_id, organization_id` | organizations, roles, users |
| `organizations` | 11 | `organization_id` | enterprises, users |
| `permissions` | 4 | `permission_id` | — |
| `reports` | 16 | `report_id` | cases, organizations, users |
| `role_permissions` | 2 | `role_id, permission_id` | permissions, roles |
| `roles` | 7 | `role_id` | — |
| `solutions` | 23 | `solution_id` | cases, hypotheses, organizations, users |
| `team_members` | 4 | `user_id, team_id` | teams, users |
| `teams` | 7 | `team_id` | organizations |
| `uploaded_files` | 13 | `file_id` | cases, organizations, users |
| `user_audit_log` | 11 | `audit_id` | organizations, users |
| `users` | 19 | `user_id` | enterprises |

## ER Diagram

```mermaid
erDiagram
    agent_executions {
        VARCHAR execution_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR session_id FK
        VARCHAR agent_type
        VARCHAR agent_model
        VARCHAR status
        DATETIME started_at
        DATETIME completed_at
        INTEGER execution_duration_ms
        TEXT prompt
        TEXT response
        TEXT error_message
        TEXT token_usage
        TEXT metadata
        DATETIME created_at
        DATETIME updated_at
    }
    agent_tool_calls {
        VARCHAR tool_call_id PK
        VARCHAR organization_id FK
        VARCHAR execution_id FK
        VARCHAR tool_name
        TEXT tool_input
        TEXT tool_output
        VARCHAR status
        TEXT error_message
        DATETIME started_at
        DATETIME completed_at
        INTEGER duration_ms
        DATETIME created_at
        DATETIME updated_at
    }
    case_actions {
        INTEGER transition_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR from_status
        VARCHAR to_status
        TEXT reason
        VARCHAR triggered_by
        TEXT metadata
        DATETIME transitioned_at
    }
    case_checkpoints {
        VARCHAR checkpoint_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        INTEGER turn_number
        JSON case_snapshot
        VARCHAR snapshot_hash
        VARCHAR trigger
        TEXT metadata
        DATETIME created_at
    }
    case_entities {
        VARCHAR case_id PK
        VARCHAR organization_id FK
        VARCHAR entity_type PK
        VARCHAR entity_value PK
        VARCHAR evidence_id PK
        INTEGER mention_count
        BOOLEAN in_error_context
        DATETIME first_seen_ts
    }
    case_messages {
        VARCHAR message_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        INTEGER turn_number
        VARCHAR role
        TEXT content
        INTEGER token_count
        TEXT metadata
        DATETIME created_at
    }
    case_tags {
        INTEGER tag_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR tag
        DATETIME created_at
    }
    cases {
        VARCHAR case_id PK
        VARCHAR organization_id FK
        VARCHAR team_id FK
        VARCHAR user_id FK
        VARCHAR title
        TEXT description
        VARCHAR status
        TEXT investigation_strategy
        INTEGER current_turn
        INTEGER turns_without_progress
        INTEGER version
        VARCHAR closure_reason
        DATETIME last_activity_at
        DATETIME resolved_at
        DATETIME closed_at
        TEXT inquiry
        TEXT problem_verification
        TEXT working_conclusion
        TEXT root_cause_conclusion
        TEXT path_selection
        TEXT escalation_state
        TEXT documentation
        TEXT progress
        TEXT metadata
        DATETIME created_at
        DATETIME updated_at
    }
    conversion_drafts {
        VARCHAR id PK
        VARCHAR organization_id FK
        VARCHAR conversion_id FK
        VARCHAR knowledge_item_id FK
        VARCHAR verified_by FK
        VARCHAR runbook_id
        VARCHAR title
        VARCHAR file_path
        VARCHAR status
        VARCHAR source_type
        VARCHAR document_type
        VARCHAR domain
        VARCHAR service
        VARCHAR severity
        TEXT tags
        BOOLEAN validation_passed
        JSON validation_errors
        JSON validation_warnings
        NUMERIC quality_score
        JSON quality_details
        DATETIME created_at
        DATETIME verified_at
    }
    conversion_jobs {
        VARCHAR id PK
        VARCHAR organization_id FK
        VARCHAR user_id FK
        VARCHAR team_id FK
        VARCHAR case_id FK
        VARCHAR source_file_id FK
        VARCHAR scope
        VARCHAR status
        VARCHAR source_type
        INTEGER failure_modes_detected
        JSON analysis_result
        DATETIME created_at
        DATETIME completed_at
    }
    enterprises {
        VARCHAR enterprise_id PK
        VARCHAR name
        VARCHAR slug
        VARCHAR plan_tier
        INTEGER max_members
        INTEGER max_cases
        VARCHAR billing_email
        TEXT settings
        DATETIME created_at
        DATETIME updated_at
        DATETIME deleted_at
    }
    evidence {
        VARCHAR evidence_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR source_file_id FK
        VARCHAR category
        VARCHAR source_type
        VARCHAR form
        VARCHAR summary
        TEXT extract
        BOOLEAN is_primary
        FLOAT reliability_score
        TEXT tags
        INTEGER collected_at_turn
        DATETIME coverage_start_ts
        DATETIME coverage_end_ts
        BOOLEAN vectorized
        TEXT metadata
        DATETIME created_at
        DATETIME updated_at
    }
    hypotheses {
        VARCHAR hypothesis_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        TEXT statement
        VARCHAR status
        NUMERIC likelihood
        NUMERIC initial_likelihood
        VARCHAR category
        VARCHAR generation_mode
        TEXT rationale
        TEXT retirement_reason
        VARCHAR refutation_reason
        INTEGER generated_at_turn
        INTEGER last_updated_turn
        INTEGER last_progress_at_turn
        INTEGER iterations_without_progress
        DATETIME tested_at
        DATETIME concluded_at
        VARCHAR created_by FK
        VARCHAR updated_by FK
        TEXT metadata
        DATETIME proposed_at
        DATETIME updated_at
    }
    hypothesis_evidence {
        VARCHAR hypothesis_id PK
        VARCHAR evidence_id PK
        VARCHAR organization_id FK
        VARCHAR relationship_type
        NUMERIC confidence
        INTEGER linked_at_turn
        VARCHAR linked_by FK
        DATETIME created_at
    }
    investigation_sessions {
        VARCHAR session_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR user_id FK
        VARCHAR status
        DATETIME started_at
        DATETIME ended_at
        DATETIME last_activity_at
        INTEGER total_duration_ms
        TEXT session_goal
        TEXT findings_summary
        INTEGER total_token_usage
        INTEGER total_agent_executions
        INTEGER token_budget_limit
        TEXT metadata
        DATETIME created_at
        DATETIME updated_at
    }
    knowledge_items {
        VARCHAR item_id PK
        VARCHAR organization_id FK
        VARCHAR scope
        VARCHAR owner_id FK
        VARCHAR team_id FK
        VARCHAR source_suggestion_id
        VARCHAR title
        TEXT content
        VARCHAR item_type
        VARCHAR category
        TEXT tags
        VARCHAR embedding_model
        TEXT embedding_vector
        INTEGER embedding_version
        VARCHAR source_url
        VARCHAR author
        VARCHAR language
        INTEGER verification_level
        VARCHAR verification_reason
        VARCHAR verified_by FK
        DATETIME verified_at
        INTEGER view_count
        INTEGER helpful_count
        INTEGER not_helpful_count
        DATETIME last_retrieved_at
        BOOLEAN is_published
        TEXT metadata
        DATETIME created_at
        DATETIME updated_at
    }
    knowledge_suggestions {
        VARCHAR suggestion_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR knowledge_item_id FK
        VARCHAR status
        VARCHAR suggested_title
        TEXT suggested_content
        VARCHAR suggested_type
        VARCHAR extracted_by FK
        DATETIME extracted_at
        BOOLEAN include_messages
        BOOLEAN include_evidence
        VARCHAR pii_scan_status
        TEXT pii_scan_result
        VARCHAR pii_remediated_by FK
        DATETIME pii_remediated_at
        VARCHAR source_case_title
        INTEGER message_count
        INTEGER evidence_count
        VARCHAR reviewed_by FK
        DATETIME reviewed_at
        TEXT review_notes
        TEXT rejection_reason
        TEXT metadata
        DATETIME created_at
        DATETIME updated_at
    }
    llm_config_overrides {
        VARCHAR key PK
        TEXT value
        VARCHAR updated_by FK
        DATETIME updated_at
    }
    oauth_authorization_codes {
        VARCHAR code PK
        VARCHAR user_id FK
        TEXT redirect_uri
        VARCHAR code_challenge
        DATETIME expires_at
        BOOLEAN used
        DATETIME created_at
    }
    oauth_revoked_tokens {
        VARCHAR jti PK
        DATETIME revoked_at
        DATETIME expires_at
    }
    organization_members {
        VARCHAR user_id PK
        VARCHAR organization_id PK
        VARCHAR role_id FK
        VARCHAR invited_by FK
        DATETIME invited_at
        DATETIME invitation_accepted_at
        DATETIME joined_at
        DATETIME last_active_at
        DATETIME updated_at
    }
    organizations {
        VARCHAR organization_id PK
        VARCHAR enterprise_id FK
        VARCHAR name
        VARCHAR slug
        TEXT description
        VARCHAR owner_id FK
        BOOLEAN is_active
        TEXT settings
        DATETIME created_at
        DATETIME updated_at
        DATETIME deleted_at
    }
    permissions {
        VARCHAR permission_id PK
        VARCHAR resource
        VARCHAR action
        TEXT description
    }
    reports {
        VARCHAR report_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR generated_by FK
        VARCHAR report_type
        INTEGER version
        BOOLEAN is_current
        BOOLEAN linked_to_closure
        VARCHAR title
        TEXT content
        VARCHAR format
        VARCHAR generation_status
        INTEGER generation_time_ms
        JSON metadata
        DATETIME generated_at
        DATETIME updated_at
    }
    role_permissions {
        VARCHAR role_id PK
        VARCHAR permission_id PK
    }
    roles {
        VARCHAR role_id PK
        VARCHAR name
        TEXT description
        VARCHAR scope
        BOOLEAN is_system_role
        DATETIME created_at
        DATETIME updated_at
    }
    solutions {
        VARCHAR solution_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR hypothesis_id FK
        VARCHAR title
        TEXT description
        VARCHAR solution_type
        VARCHAR status
        VARCHAR risk_level
        VARCHAR estimated_effort
        TEXT immediate_action
        TEXT longterm_fix
        TEXT implementation_steps
        TEXT commands
        TEXT risks
        TEXT verification_result
        DATETIME verification_timestamp
        VARCHAR created_by FK
        VARCHAR updated_by FK
        TEXT metadata
        DATETIME proposed_at
        DATETIME implemented_at
        DATETIME updated_at
    }
    team_members {
        VARCHAR user_id PK
        VARCHAR team_id PK
        VARCHAR team_role
        DATETIME joined_at
    }
    teams {
        VARCHAR team_id PK
        VARCHAR organization_id FK
        VARCHAR name
        TEXT description
        DATETIME created_at
        DATETIME updated_at
        DATETIME deleted_at
    }
    uploaded_files {
        VARCHAR file_id PK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR uploaded_by FK
        VARCHAR filename
        BIGINT size_bytes
        VARCHAR content_type
        VARCHAR content_hash
        VARCHAR storage_ref
        VARCHAR upload_source
        INTEGER uploaded_at_turn
        TEXT metadata
        DATETIME uploaded_at
    }
    user_audit_log {
        INTEGER audit_id PK
        VARCHAR user_id FK
        VARCHAR organization_id FK
        VARCHAR event_type
        VARCHAR event_category
        VARCHAR resource_type
        VARCHAR resource_id
        TEXT details
        VARCHAR ip_address
        TEXT user_agent
        DATETIME created_at
    }
    users {
        VARCHAR user_id PK
        VARCHAR enterprise_id FK
        VARCHAR username
        VARCHAR email
        VARCHAR display_name
        VARCHAR avatar_url
        VARCHAR timezone
        VARCHAR locale
        VARCHAR hashed_password
        BOOLEAN is_active
        BOOLEAN is_email_verified
        DATETIME email_verified_at
        VARCHAR sso_provider
        VARCHAR sso_provider_id
        DATETIME last_login_at
        DATETIME last_password_change_at
        DATETIME created_at
        DATETIME updated_at
        DATETIME deleted_at
    }
    agent_executions ||--o{ agent_tool_calls : ""
    cases ||--o{ agent_executions : ""
    cases ||--o{ case_actions : ""
    cases ||--o{ case_checkpoints : ""
    cases ||--o{ case_entities : ""
    cases ||--o{ case_messages : ""
    cases ||--o{ case_tags : ""
    cases ||--o{ conversion_jobs : ""
    cases ||--o{ evidence : ""
    cases ||--o{ hypotheses : ""
    cases ||--o{ investigation_sessions : ""
    cases ||--o{ knowledge_suggestions : ""
    cases ||--o{ reports : ""
    cases ||--o{ solutions : ""
    cases ||--o{ uploaded_files : ""
    conversion_jobs ||--o{ conversion_drafts : ""
    enterprises ||--o{ organizations : ""
    enterprises ||--o{ users : ""
    evidence ||--o{ case_entities : ""
    evidence ||--o{ hypothesis_evidence : ""
    hypotheses ||--o{ hypothesis_evidence : ""
    hypotheses ||--o{ solutions : ""
    investigation_sessions ||--o{ agent_executions : ""
    knowledge_items ||--o{ conversion_drafts : ""
    knowledge_items ||--o{ knowledge_suggestions : ""
    organizations ||--o{ agent_executions : ""
    organizations ||--o{ agent_tool_calls : ""
    organizations ||--o{ case_actions : ""
    organizations ||--o{ case_checkpoints : ""
    organizations ||--o{ case_entities : ""
    organizations ||--o{ case_messages : ""
    organizations ||--o{ case_tags : ""
    organizations ||--o{ cases : ""
    organizations ||--o{ conversion_drafts : ""
    organizations ||--o{ conversion_jobs : ""
    organizations ||--o{ evidence : ""
    organizations ||--o{ hypotheses : ""
    organizations ||--o{ hypothesis_evidence : ""
    organizations ||--o{ investigation_sessions : ""
    organizations ||--o{ knowledge_items : ""
    organizations ||--o{ knowledge_suggestions : ""
    organizations ||--o{ organization_members : ""
    organizations ||--o{ reports : ""
    organizations ||--o{ solutions : ""
    organizations ||--o{ teams : ""
    organizations ||--o{ uploaded_files : ""
    organizations ||--o{ user_audit_log : ""
    permissions ||--o{ role_permissions : ""
    roles ||--o{ organization_members : ""
    roles ||--o{ role_permissions : ""
    teams ||--o{ cases : ""
    teams ||--o{ conversion_jobs : ""
    teams ||--o{ knowledge_items : ""
    teams ||--o{ team_members : ""
    uploaded_files ||--o{ conversion_jobs : ""
    uploaded_files ||--o{ evidence : ""
    users ||--o{ cases : ""
    users ||--o{ conversion_drafts : ""
    users ||--o{ conversion_jobs : ""
    users ||--o{ hypotheses : ""
    users ||--o{ hypothesis_evidence : ""
    users ||--o{ investigation_sessions : ""
    users ||--o{ knowledge_items : ""
    users ||--o{ knowledge_suggestions : ""
    users ||--o{ llm_config_overrides : ""
    users ||--o{ oauth_authorization_codes : ""
    users ||--o{ organization_members : ""
    users ||--o{ organizations : ""
    users ||--o{ reports : ""
    users ||--o{ solutions : ""
    users ||--o{ team_members : ""
    users ||--o{ uploaded_files : ""
    users ||--o{ user_audit_log : ""
```
