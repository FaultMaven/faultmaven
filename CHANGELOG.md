# Changelog

All notable changes to FaultMaven will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **KB Vector Store**: Introduced `KnowledgeVectorStore` for KB retrieval — uses collection names as-is from `KBConfig`. Replaces `CaseVectorStore` (which prepends `case_`) for all Q&A tools. Global KB collection standardized to `global_kb`.

- Removed `IP_ADDRESS` from Presidio default `entities_to_protect` — public IPs are investigation evidence, not PII. Private IPs (RFC1918) remain redacted by the regex layer.

### Fixed

- **KB Collection Mismatch**: Fixed silent empty results from KB Q&A tools caused by ingestion writing to `faultmaven_kb` while retrieval searched `case_global_kb` (CaseVectorStore prefix). Also fixes case evidence double-prefix bug (`case_case_{id}`).
- **Opik Ghost Spans**: Removed duplicate `@opik.track` decorator from `LLMRouter.generate()` — it created an empty outer span on every LLM call since `generate()` is a thin delegate to `route()` which already has the decorator
- **Router Timeout Enforcement**: Changed `timeout=None` to `timeout=self.request_timeout` in `LLMRouter.route()` — the configured timeout (default 30s) was never enforced at the router level, allowing unbounded latency when the fallback chain tried multiple slow providers
- **Test Mock Setup**: Fixed 3 tests in `test_router.py` where `aiohttp` response mocks were missing `response.text` setup, causing `AsyncMock` objects to leak into error messages recorded by Opik tracing

### Added (Existing)

- Session-level tools documentation in `docs/tools/`
- MCP (Model Context Protocol) integration guide
- Comprehensive tool catalog and developer guide
- Email uniqueness constraint migration (008_email_uniqueness_constraint)
- Database migration scripts for data integrity:
  - `scripts/check_duplicate_emails.py` - Check for duplicate email addresses
  - `scripts/resolve_duplicate_emails.py` - Resolve duplicate emails (auto/interactive modes)
  - `scripts/backfill_closed_at_timestamps.py` - Backfill missing closed_at timestamps

### Changed (Existing)

- Documentation reorganization: cleaner folder structure
- Renamed `guides/` to `how-to/` for clarity
- Moved historical docs to `archive/`
- Consolidated specifications into `architecture/specifications/`

### Fixed (Existing)

- **CRITICAL**: Storage infrastructure gaps causing data loss (Commits 52bfb854, b434152a, ecaafed7)
  - **Message Persistence**: Fixed PostgreSQL repositories missing `_upsert_messages()` call
    - All conversation messages now persist correctly across application restarts
    - Messages stored in normalized `case_messages` table
  - **Missing Case Fields**: Added 9 critical fields to PostgreSQL repository
    - `organization_id` - Multi-tenancy security restored
    - `description` - Problem descriptions now saved
    - `closure_reason`, `investigation_strategy` - Case metadata preserved
    - `current_turn`, `turns_without_progress` - Turn tracking restored
    - `last_activity_at`, `resolved_at`, `closed_at` - Timestamp tracking complete
  - **Missing Repository Methods**: Added 6 methods to infrastructure PostgreSQL repository
    - Report CRUD operations: `add_report()`, `get_report()`, `get_reports()`, `update_report()`, `delete_report()`
    - Rate limiting: `count_user_cases_on_date()`
  - PostgreSQL repositories now have full parity with SQLite repository
  - All tests passing: 29 unit tests, 8 SQLite integration tests, 6 Alembic migration tests
- **TEST**: Alembic migration test expectations (Commit 67d615a5)
  - Updated for migration 013_verification_suggestions (knowledge_suggestions table)
  - Fixed database path handling to use absolute paths
- **CRITICAL**: Email uniqueness not enforced in database (Security Bug)
  - Added explicit UNIQUE constraint on users.email (case-insensitive)
  - InMemoryUserRepository now stores deep copies to prevent mutable reference bugs
  - Prevents authentication bypass via duplicate email accounts
  - Migration: `20250109_1000_008_add_email_uniqueness_constraint.py`
- **CRITICAL**: Case cleanup using wrong timestamp (Data Integrity Bug)
  - DatabaseCaseRepository.cleanup_expired() now uses closed_at from metadata
  - Previously used updated_at, causing premature deletion of recently-updated closed cases
  - Added backfill script for missing closed_at timestamps
  - Prevents database bloat and incorrect case aging

### Removed

- Obsolete `_temp/` folder from architecture docs
- Redundant planning documents

### Security

- Email uniqueness enforcement prevents duplicate account creation (authentication bypass risk)
- Added comprehensive unit tests for email uniqueness and immutability

## [2.0.0] - 2025-10-04

### Added
- Infrastructure improvements (see `docs/archive/release-notes/`)
- Enhanced investigation phases and OODA integration
- Evidence-centric troubleshooting framework

### Changed
- Major architecture refactoring
- Clean dependency injection system
- Interface-based design patterns

## [1.0.0] - Initial Release

For historical release notes, see `docs/archive/release-notes/`.

---

**Note**: This changelog started with v2.0. For earlier releases, see archived documentation.
