"""Auth Module - Infrastructure Repositories

Contains repository implementations for auth persistence.

Only the repositories this module owns live here. ``UserRepository`` and
``OrganizationRepository`` are shared persistence and live in
``faultmaven.infrastructure.persistence``; they were listed here for a while
after moving, which read as ownership this package does not have.

``SessionRepository`` was removed in storage redesign 2026-04 phase 3: auth
sessions are Redis-only via ``RedisSessionStore`` (FakeRedis local, real Redis
cloud). See case-and-session-concepts.md v2.1 + deployment-schema-strategy.md
§11.1.
"""

# Don't eagerly import to avoid circular imports.
# Repositories are imported directly from these submodules when needed.

__all__ = [
    "oauth_code_repository",
    "sso_org_mapping_repository",
]
