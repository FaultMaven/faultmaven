# FaultMaven Database ER Diagram

> **Auto-generated** from SQLAlchemy models on 2026-09-06 11:10 UTC.
> Do not edit manually — run `python scripts/generate_er_diagram.py --update` to regenerate.
> Render with any Mermaid-compatible viewer (GitHub, VS Code, Mermaid Live Editor).

## Summary

**41 tables** in the schema.

| Table | Columns | Primary Key | Foreign Keys |
|-------|---------|-------------|--------------|
| `case_actions` | 10 | `transition_id` | cases, enterprises, organizations |
| `case_checkpoints` | 10 | `checkpoint_id` | cases, enterprises, organizations |
| `case_entities` | 9 | `case_id, entity_type, entity_value, evidence_id` | cases, enterprises, evidence, organizations |
| `case_messages` | 11 | `message_id` | cases, enterprises, organizations |
| `case_tags` | 6 | `tag_id` | cases, enterprises, organizations |
| `cases` | 27 | `case_id` | enterprises, organizations, users |
| `causal_edges` | 10 | `edge_id` | cases, causal_nodes, enterprises, organizations |
| `causal_node_evidence` | 9 | `node_id, evidence_id` | causal_nodes, enterprises, evidence, organizations |
| `causal_nodes` | 22 | `node_id` | cases, enterprises, organizations |
| `config_overrides` | 6 | `key` | users |
| `conversion_drafts` | 23 | `id` | conversion_jobs, enterprises, knowledge_items, organizations, users |
| `conversion_jobs` | 15 | `id` | cases, enterprises, organizations, uploaded_files, users |
| `enterprises` | 12 | `enterprise_id` | — |
| `evidence` | 25 | `evidence_id` | cases, enterprises, organizations, uploaded_files |
| `evidence_need_fulfillment` | 6 | `need_id, evidence_id` | enterprises, evidence, evidence_needs, organizations |
| `evidence_needs` | 17 | `need_id` | cases, enterprises, organizations |
| `hypotheses` | 26 | `hypothesis_id` | cases, causal_nodes, enterprises, organizations, users |
| `hypothesis_evidence` | 9 | `hypothesis_id, evidence_id` | enterprises, evidence, hypotheses, organizations, users |
| `investigation_sessions` | 18 | `session_id` | cases, enterprises, organizations, users |
| `knowledge_items` | 29 | `item_id` | enterprises, organizations, users |
| `knowledge_suggestions` | 31 | `suggestion_id` | cases, enterprises, knowledge_items, organizations, users |
| `oauth_authorization_codes` | 8 | `code` | users |
| `operator_access_audit` | 12 | `audit_id` | — |
| `operator_access_grants` | 14 | `grant_id` | — |
| `organization_members` | 10 | `user_id, organization_id` | enterprises, organizations, roles, users |
| `organizations` | 12 | `organization_id` | enterprises, users |
| `permissions` | 4 | `permission_id` | — |
| `reports` | 17 | `report_id` | cases, enterprises, organizations, users |
| `resource_shares` | 9 | `share_id` | enterprises, organizations, users |
| `role_permissions` | 2 | `role_id, permission_id` | permissions, roles |
| `roles` | 7 | `role_id` | — |
| `solutions` | 29 | `solution_id` | cases, causal_nodes, enterprises, evidence, hypotheses, organizations |
| `sso_org_mappings` | 5 | `provider, provider_org_id` | enterprises |
| `sso_personal_enterprises` | 9 | `subject` | enterprises |
| `team_invitations` | 10 | `invitation_id` | enterprises, teams, users |
| `team_members` | 4 | `user_id, team_id` | teams, users |
| `teams` | 7 | `team_id` | enterprises |
| `turn_usage` | 5 | `billing_subject_kind, billing_subject_id, usage_date` | enterprises |
| `uploaded_files` | 20 | `file_id` | cases, enterprises, organizations, users |
| `user_audit_log` | 14 | `audit_id` | enterprises, organizations, users |
| `users` | 22 | `user_id` | enterprises |

## ER Diagram

```mermaid
erDiagram
    case_actions {
        INTEGER transition_id PK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR from_state
        VARCHAR to_state
        TEXT reason
        VARCHAR triggered_by
        TEXT metadata
        DATETIME transitioned_at
    }
    case_checkpoints {
        VARCHAR checkpoint_id PK
        VARCHAR enterprise_id FK
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
        VARCHAR enterprise_id FK
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
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        INTEGER turn_number
        VARCHAR role
        TEXT content
        VARCHAR author_id
        INTEGER token_count
        TEXT metadata
        DATETIME created_at
    }
    case_tags {
        INTEGER tag_id PK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR tag
        DATETIME created_at
    }
    cases {
        VARCHAR case_id PK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR user_id FK
        VARCHAR title
        TEXT description
        VARCHAR state
        VARCHAR source
        TEXT investigation_strategy
        INTEGER current_turn
        INTEGER turns_without_progress
        INTEGER version
        VARCHAR closure_reason
        DATETIME last_activity_at
        DATETIME resolved_at
        DATETIME closed_at
        TEXT disposition_eligibility
        TEXT inquiry
        TEXT problem_verification
        TEXT working_conclusion
        TEXT root_cause_conclusion
        TEXT escalation_state
        TEXT documentation
        TEXT progress
        TEXT metadata
        DATETIME created_at
        DATETIME updated_at
    }
    causal_edges {
        VARCHAR edge_id PK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR cause_node_id FK
        VARCHAR effect_node_id FK
        VARCHAR and_group
        TEXT reasoning
        INTEGER created_at_turn
        DATETIME created_at
    }
    causal_node_evidence {
        VARCHAR node_id PK
        VARCHAR evidence_id PK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR stance
        NUMERIC stance_confidence
        TEXT reasoning
        INTEGER linked_at_turn
        DATETIME created_at
    }
    causal_nodes {
        VARCHAR node_id PK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        TEXT statement
        VARCHAR node_type
        VARCHAR node_state
        VARCHAR validation_method
        NUMERIC belief
        BOOLEAN signature_consistent
        BOOLEAN actionable
        VARCHAR category
        INTEGER state_epoch
        INTEGER generated_at_turn
        INTEGER last_updated_turn
        INTEGER last_progress_at_turn
        INTEGER iterations_without_progress
        VARCHAR refutation_reason
        TEXT rationale
        TEXT metadata
        DATETIME proposed_at
        DATETIME updated_at
    }
    config_overrides {
        VARCHAR key PK
        TEXT value
        VARCHAR category
        VARCHAR source
        VARCHAR updated_by FK
        DATETIME updated_at
    }
    conversion_drafts {
        VARCHAR id PK
        VARCHAR enterprise_id FK
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
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR user_id FK
        VARCHAR case_id FK
        VARCHAR live_case_id
        VARCHAR source_file_id FK
        VARCHAR scope
        VARCHAR status
        VARCHAR source_type
        INTEGER failure_modes_detected
        JSON analysis_result
        JSON warnings
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
        VARCHAR domain
    }
    evidence {
        VARCHAR evidence_id PK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR source_file_id FK
        VARCHAR category
        VARCHAR source_type
        VARCHAR primary_purpose
        TEXT analysis
        VARCHAR processing_mode
        TEXT advances_milestones
        VARCHAR summary
        TEXT extract
        BOOLEAN is_primary
        FLOAT reliability_score
        TEXT tags
        INTEGER collected_at_turn
        DATETIME coverage_start_ts
        DATETIME coverage_end_ts
        VARCHAR coverage_source
        BOOLEAN vectorized
        VARCHAR collected_by
        TEXT metadata
        DATETIME created_at
        DATETIME updated_at
    }
    evidence_need_fulfillment {
        VARCHAR need_id PK
        VARCHAR evidence_id PK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        INTEGER linked_at_turn
        DATETIME created_at
    }
    evidence_needs {
        VARCHAR need_id PK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR purpose
        VARCHAR request_text
        VARCHAR rationale
        VARCHAR priority
        VARCHAR state
        VARCHAR obtainability
        TEXT motivating_hypothesis_ids
        TEXT surfaced_turns
        BOOLEAN engine_inferred
        VARCHAR superseded_reason
        INTEGER created_at_turn
        DATETIME created_at
        DATETIME updated_at
    }
    hypotheses {
        VARCHAR hypothesis_id PK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR root_node_id FK
        TEXT path
        TEXT statement
        VARCHAR state
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
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR relationship_type
        NUMERIC confidence
        INTEGER linked_at_turn
        VARCHAR linked_by FK
        DATETIME created_at
    }
    investigation_sessions {
        VARCHAR session_id PK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR user_id FK
        VARCHAR state
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
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR scope
        VARCHAR owner_id FK
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
        VARCHAR enterprise_id FK
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
        BOOLEAN validation_passed
        TEXT validation_errors
        TEXT validation_warnings
        INTEGER version
        DATETIME created_at
        DATETIME updated_at
    }
    oauth_authorization_codes {
        VARCHAR code PK
        VARCHAR user_id FK
        TEXT redirect_uri
        VARCHAR code_challenge
        DATETIME expires_at
        BOOLEAN used
        VARCHAR organization_id
        DATETIME created_at
    }
    operator_access_audit {
        INTEGER audit_id PK
        VARCHAR operator_user_id
        VARCHAR operator_username
        VARCHAR action
        VARCHAR target_enterprise_id
        VARCHAR target_case_id
        TEXT reason
        VARCHAR grant_id
        DATETIME expires_at
        VARCHAR deployment_mode
        TEXT details
        DATETIME created_at
    }
    operator_access_grants {
        VARCHAR grant_id PK
        VARCHAR operator_user_id
        VARCHAR operator_username
        VARCHAR target_case_id
        VARCHAR target_enterprise_id
        TEXT reason
        DATETIME created_at
        DATETIME expires_at
        DATETIME revoked_at
        VARCHAR revoked_by
        VARCHAR approval_state
        VARCHAR approved_by
        DATETIME approved_at
        VARCHAR deployment_mode
    }
    organization_members {
        VARCHAR user_id PK
        VARCHAR organization_id PK
        VARCHAR enterprise_id FK
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
        INTEGER daily_turn_cap
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
        VARCHAR enterprise_id FK
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
    resource_shares {
        VARCHAR share_id PK
        VARCHAR resource_type
        VARCHAR resource_id
        VARCHAR scope_type
        VARCHAR scope_id
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR created_by FK
        DATETIME created_at
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
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR case_id FK
        VARCHAR hypothesis_id FK
        VARCHAR node_id FK
        VARCHAR quadrant
        VARCHAR title
        TEXT description
        VARCHAR solution_type
        VARCHAR state
        VARCHAR risk_level
        VARCHAR estimated_effort
        TEXT immediate_action
        TEXT longterm_fix
        TEXT implementation_steps
        TEXT commands
        TEXT risks
        VARCHAR proposed_by
        VARCHAR applied_by
        VARCHAR verification_method
        VARCHAR verification_evidence_id FK
        FLOAT effectiveness
        TEXT verification_result
        DATETIME verified_at
        TEXT metadata
        DATETIME proposed_at
        DATETIME applied_at
        DATETIME updated_at
    }
    sso_org_mappings {
        VARCHAR provider PK
        VARCHAR provider_org_id PK
        VARCHAR enterprise_id FK
        DATETIME created_at
        DATETIME updated_at
    }
    sso_personal_enterprises {
        VARCHAR subject PK
        VARCHAR provider
        VARCHAR enterprise_id FK
        VARCHAR provider_org_id
        BOOLEAN membership_confirmed
        DATETIME retired_at
        VARCHAR retirement_state
        DATETIME created_at
        DATETIME updated_at
    }
    team_invitations {
        VARCHAR invitation_id PK
        VARCHAR enterprise_id FK
        VARCHAR team_id FK
        VARCHAR email
        VARCHAR invited_user_id FK
        VARCHAR invited_by FK
        VARCHAR status
        DATETIME created_at
        DATETIME expires_at
        DATETIME accepted_at
    }
    team_members {
        VARCHAR user_id PK
        VARCHAR team_id PK
        VARCHAR team_role
        DATETIME joined_at
    }
    teams {
        VARCHAR team_id PK
        VARCHAR enterprise_id FK
        VARCHAR name
        TEXT description
        DATETIME created_at
        DATETIME updated_at
        DATETIME deleted_at
    }
    turn_usage {
        VARCHAR enterprise_id FK
        VARCHAR billing_subject_kind PK
        VARCHAR billing_subject_id PK
        DATE usage_date PK
        INTEGER turn_count
    }
    uploaded_files {
        VARCHAR file_id PK
        VARCHAR enterprise_id FK
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
        TEXT summary
        TEXT structural_index
        VARCHAR data_type
        DATETIME coverage_start_ts
        DATETIME coverage_end_ts
        VARCHAR coverage_source
        DATETIME uploaded_at
    }
    user_audit_log {
        INTEGER audit_id PK
        VARCHAR user_id FK
        VARCHAR enterprise_id FK
        VARCHAR organization_id FK
        VARCHAR event_type
        VARCHAR event_category
        VARCHAR resource_type
        VARCHAR resource_id
        TEXT details
        VARCHAR ip_address
        TEXT user_agent
        VARCHAR session_id
        BOOLEAN success
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
        TEXT dev_roles
        VARCHAR account_kind
        VARCHAR service_channel
    }
    cases ||--o{ case_actions : ""
    cases ||--o{ case_checkpoints : ""
    cases ||--o{ case_entities : ""
    cases ||--o{ case_messages : ""
    cases ||--o{ case_tags : ""
    cases ||--o{ causal_edges : ""
    cases ||--o{ causal_nodes : ""
    cases ||--o{ conversion_jobs : ""
    cases ||--o{ evidence : ""
    cases ||--o{ evidence_needs : ""
    cases ||--o{ hypotheses : ""
    cases ||--o{ investigation_sessions : ""
    cases ||--o{ knowledge_suggestions : ""
    cases ||--o{ reports : ""
    cases ||--o{ solutions : ""
    cases ||--o{ uploaded_files : ""
    causal_nodes ||--o{ causal_edges : ""
    causal_nodes ||--o{ causal_node_evidence : ""
    causal_nodes ||--o{ hypotheses : ""
    causal_nodes ||--o{ solutions : ""
    conversion_jobs ||--o{ conversion_drafts : ""
    enterprises ||--o{ case_actions : ""
    enterprises ||--o{ case_checkpoints : ""
    enterprises ||--o{ case_entities : ""
    enterprises ||--o{ case_messages : ""
    enterprises ||--o{ case_tags : ""
    enterprises ||--o{ cases : ""
    enterprises ||--o{ causal_edges : ""
    enterprises ||--o{ causal_node_evidence : ""
    enterprises ||--o{ causal_nodes : ""
    enterprises ||--o{ conversion_drafts : ""
    enterprises ||--o{ conversion_jobs : ""
    enterprises ||--o{ evidence : ""
    enterprises ||--o{ evidence_need_fulfillment : ""
    enterprises ||--o{ evidence_needs : ""
    enterprises ||--o{ hypotheses : ""
    enterprises ||--o{ hypothesis_evidence : ""
    enterprises ||--o{ investigation_sessions : ""
    enterprises ||--o{ knowledge_items : ""
    enterprises ||--o{ knowledge_suggestions : ""
    enterprises ||--o{ organization_members : ""
    enterprises ||--o{ organizations : ""
    enterprises ||--o{ reports : ""
    enterprises ||--o{ resource_shares : ""
    enterprises ||--o{ solutions : ""
    enterprises ||--o{ sso_org_mappings : ""
    enterprises ||--o{ sso_personal_enterprises : ""
    enterprises ||--o{ team_invitations : ""
    enterprises ||--o{ teams : ""
    enterprises ||--o{ turn_usage : ""
    enterprises ||--o{ uploaded_files : ""
    enterprises ||--o{ user_audit_log : ""
    enterprises ||--o{ users : ""
    evidence ||--o{ case_entities : ""
    evidence ||--o{ causal_node_evidence : ""
    evidence ||--o{ evidence_need_fulfillment : ""
    evidence ||--o{ hypothesis_evidence : ""
    evidence ||--o{ solutions : ""
    evidence_needs ||--o{ evidence_need_fulfillment : ""
    hypotheses ||--o{ hypothesis_evidence : ""
    hypotheses ||--o{ solutions : ""
    knowledge_items ||--o{ conversion_drafts : ""
    knowledge_items ||--o{ knowledge_suggestions : ""
    organizations ||--o{ case_actions : ""
    organizations ||--o{ case_checkpoints : ""
    organizations ||--o{ case_entities : ""
    organizations ||--o{ case_messages : ""
    organizations ||--o{ case_tags : ""
    organizations ||--o{ cases : ""
    organizations ||--o{ causal_edges : ""
    organizations ||--o{ causal_node_evidence : ""
    organizations ||--o{ causal_nodes : ""
    organizations ||--o{ conversion_drafts : ""
    organizations ||--o{ conversion_jobs : ""
    organizations ||--o{ evidence : ""
    organizations ||--o{ evidence_need_fulfillment : ""
    organizations ||--o{ evidence_needs : ""
    organizations ||--o{ hypotheses : ""
    organizations ||--o{ hypothesis_evidence : ""
    organizations ||--o{ investigation_sessions : ""
    organizations ||--o{ knowledge_items : ""
    organizations ||--o{ knowledge_suggestions : ""
    organizations ||--o{ organization_members : ""
    organizations ||--o{ reports : ""
    organizations ||--o{ resource_shares : ""
    organizations ||--o{ solutions : ""
    organizations ||--o{ uploaded_files : ""
    organizations ||--o{ user_audit_log : ""
    permissions ||--o{ role_permissions : ""
    roles ||--o{ organization_members : ""
    roles ||--o{ role_permissions : ""
    teams ||--o{ team_invitations : ""
    teams ||--o{ team_members : ""
    uploaded_files ||--o{ conversion_jobs : ""
    uploaded_files ||--o{ evidence : ""
    users ||--o{ cases : ""
    users ||--o{ config_overrides : ""
    users ||--o{ conversion_drafts : ""
    users ||--o{ conversion_jobs : ""
    users ||--o{ hypotheses : ""
    users ||--o{ hypothesis_evidence : ""
    users ||--o{ investigation_sessions : ""
    users ||--o{ knowledge_items : ""
    users ||--o{ knowledge_suggestions : ""
    users ||--o{ oauth_authorization_codes : ""
    users ||--o{ organization_members : ""
    users ||--o{ organizations : ""
    users ||--o{ reports : ""
    users ||--o{ resource_shares : ""
    users ||--o{ team_invitations : ""
    users ||--o{ team_members : ""
    users ||--o{ uploaded_files : ""
    users ||--o{ user_audit_log : ""
```
