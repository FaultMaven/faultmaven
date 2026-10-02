# Database Reference

## Schema

FaultMaven uses **Alembic** for database migrations. The chain is a baseline plus additive revisions on top of it. The baseline, `001_enterprise_baseline` (ADR-017), creates the schema it was written with, the PostgreSQL RLS policies, the append-only operator triggers, the last-admin constraint trigger, and the seed rows (the Standalone enterprise, the Standalone default team, the RBAC roles and permissions); each later revision adds to it without amending it — `002_llm_usage_ledger` adds `llm_usage_daily` and `llm_turn_spend` with their RLS policies, `003_admin_case_metadata` adds the two PostgreSQL `SECURITY DEFINER` functions behind the cross-enterprise operator case list (`admin_case_metadata_page`, `admin_case_metadata_count`; executable by the runtime role `faultmaven_app`, not `PUBLIC`; no table, and nothing on SQLite), `004_definer_trigger_hardening` gives the baseline's two definer trigger functions `row_security = off` and `pg_temp` last on their `search_path`, and `005_definer_search_path_without_public` re-creates all four `SECURITY DEFINER` functions (the baseline's two trigger guards and 003's two reads) with `search_path = pg_catalog, pg_temp`, `row_security = off` and every relation schema-qualified, keeping their owners, grants, comments and triggers, and revokes `CREATE` on schema `public` from `PUBLIC` where the migrating role can (otherwise it warns; see `docs/guides/database-migrations.md`). `006_kb_conversion_source_storage_ref_null` changes no schema: it sets `uploaded_files.storage_ref` to NULL on every KB conversion-source row (`upload_source = 'conversion_source'`, no `case_id`), so the column holds a storage-backend key or NULL in every row (#836). On PostgreSQL it runs under `row_security = off`, so a role the policy would filter raises rather than clearing a subset, and its `downgrade()` changes nothing. `alembic heads` prints the current head; `tests/integration/test_alembic_migrations.py` pins it together with the expected table set. The dialect rule every migration must satisfy (SQLite as well as PostgreSQL) is stated once, in the repository `CLAUDE.md` §Database.

- **Authoritative source**: `alembic/versions/` in the repo root
- **ER diagram**: [docs/architecture/data-and-storage/er-diagram.md](../../architecture/data-and-storage/er-diagram.md) (regenerate via `scripts/generate_er_diagram.py`)
- **Schema specifications**: [docs/architecture/data-and-storage/schemas/](../../architecture/data-and-storage/schemas/)

### Migration commands

```bash
alembic upgrade head          # Apply all migrations
alembic revision --autogenerate -m "description"  # Create new migration
alembic downgrade -1          # Revert last migration
```

### Tables by domain

**User domain:** `users`, `organizations`, `organization_members`, `roles`, `permissions`, `role_permissions`, `teams`, `team_members`, `team_invitations`, `user_audit_log`, `oauth_authorization_codes`, `token_revocations`

**Case domain:** `cases`, `case_messages`, `case_actions`, `case_tags`, `case_checkpoints`, `case_entities`, `evidence`, `hypotheses`, `hypothesis_evidence`, `solutions`, `uploaded_files`, `investigation_sessions`, `reports`, `conversion_jobs`, `conversion_drafts`; the causal graph and evidence needs: `causal_nodes`, `causal_edges`, `causal_node_evidence`, `evidence_needs`, `evidence_need_fulfillment`

Investigation activity is recorded in `case_messages` and `case_actions`. `investigation_sessions.total_agent_executions` is a counter on the session row, not a pointer into a table of executions: `agent_executions` / `agent_tool_calls` are gone, together with their ORM models and the `ICaseRepository` read/write methods (#1350) — `ApiCaseService.get_case_with_details` no longer accepts `include_executions`; the call raises `TypeError` rather than returning an empty list.

**Knowledge domain (case-adjacent):** `knowledge_items`, `knowledge_suggestions`

**Tenancy, sharing and usage:** `enterprises`, `sso_org_mappings`, `sso_personal_enterprises`, `resource_shares`, `turn_usage` — semantics in `.claude/rules/data-model.md` and [sso-org-mapping.md](../../architecture/security/sso-org-mapping.md)

**LLM usage ledger (#640):** `llm_usage_daily` (billed LLM calls per enterprise, UTC day, billing subject, actor, provider, model and outcome) and `llm_turn_spend` (one row per engine turn that made a billed call; deleted with its case) — what each row means, attribution, retention and the fail-open counter: [llm-cost-observability.md](../../operations/monitoring/llm-cost-observability.md#the-usage-ledger)

**Operator access (ADR-012 D9):** `operator_access_grants`, `operator_access_audit` — [break-glass-content-access.md](../../architecture/security/break-glass-content-access.md)

**Config domain:** `config_overrides` (dashboard-managed settings, hot-reloaded at runtime — cloud mode only; standalone uses `.env` as the sole source of truth)

This listing is by hand; the ER diagram is generated from the ORM models, so trust the diagram on a disagreement.

Historical pre-Alembic SQL scripts are preserved at `docs/archive/legacy-schema/` for reference only. Do not apply them.

## Engine configuration

The async SQLAlchemy engine setup lives in [`faultmaven/infrastructure/persistence/database.py`](../../../faultmaven/infrastructure/persistence/database.py) and branches on the URL scheme.

- **SQLite (Standalone)**: `NullPool` + a per-connection `connect` event listener that sets six PRAGMAs (3 correctness: `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`; 3 performance: `synchronous=NORMAL`, `temp_store=MEMORY`, `cache_size=-64000`). Full table, rationale, and deployment assumptions in [docs/operations/data-storage-management.md → SQLite Database Management → Engine configuration](../../operations/data-storage-management.md#engine-configuration).
- **PostgreSQL (Cloud)**: connection pool with `pool_pre_ping`, env-driven pool sizing (`pool_size`, `max_overflow`, `pool_timeout`, `pool_recycle`). Server-side knobs (`shared_buffers`, `work_mem`, `synchronous_commit`, etc.) live in the PG server config, not the application engine layer.

The SQLite PRAGMAs do **not** run against PostgreSQL — the branch is gated on `is_sqlite(url)`.

### Regression coverage

Engine setup is pinned by tests in [`tests/unit/infrastructure/persistence/test_sqlite_pragmas.py`](../../../tests/unit/infrastructure/persistence/test_sqlite_pragmas.py) — verifies all 6 PRAGMAs apply on every NullPool checkout, since PRAGMAs are per-connection state in SQLite.
