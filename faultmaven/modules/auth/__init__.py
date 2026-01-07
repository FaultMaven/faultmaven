"""Auth Module

Contains authentication, authorization, user management, session management,
team management, and organization management.

Structure:
- api/: API routes for auth endpoints
- domain/: Domain models and services
  - models/: Auth, User, Session, Team, Organization, RBAC models
  - services/: Auth, Session, User, Team, Organization services
- infrastructure/: Persistence layer
  - repositories/: Repository implementations
  - stores/: Store implementations
"""

# Don't eagerly import api to avoid circular imports
# API routes will be imported by the main app router

__all__ = []
