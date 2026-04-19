"""Auth Module - Infrastructure Repositories

Contains repository implementations for auth persistence.
"""

# Don't eagerly import to avoid circular imports
# Repositories will be imported directly when needed

__all__ = [
    # SessionRepository removed in storage redesign 2026-04 phase 3.
    # Auth sessions are Redis-only via RedisSessionStore (FakeRedis local,
    # real Redis cloud). See case-and-session-concepts.md v2.1 +
    # deployment-schema-strategy.md §11.1.
    "OrganizationRepository",
    "TeamRepository",
    "UserRepository",
    "InMemoryOAuthCodeRepository",
    "RedisOAuthCodeRepository",
    "PostgresOAuthCodeRepository",
]
