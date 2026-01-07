"""Case Module - Vertical Slice

Manages case lifecycle, investigation sessions, and case data ingestion.

Structure:
- api/: API routes for case endpoints
- domain/: Domain models and services
  - models.py: Case, CaseStatus, CaseMessage, etc.
  - services/: CaseService, InvestigationSessionService, CaseStatusManager
  - investigation_session.py: Investigation session domain model
- infrastructure/: Persistence layer
  - case_repository.py: Abstract repository interface
  - database_case_repository.py: Database implementation
  - postgresql_hybrid_case_repository.py: PostgreSQL + vector store implementation
  - investigation_session_repository.py: Session persistence
"""

# Don't eagerly import to avoid circular imports
# Components will be imported directly when needed

__all__ = []
