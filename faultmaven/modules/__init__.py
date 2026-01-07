"""FaultMaven Modules Package

This package contains vertical slice modules that own complete feature stacks:
- agent: AI agent orchestration, investigation workflows, and tools
- auth: Authentication, authorization, users, sessions, teams, organizations, RBAC
- case: Case management, investigation sessions, case data ingestion
- evidence: Evidence upload, storage, linking, and retrieval
- knowledge: Knowledge base, RAG, vector search, and semantic search
- report: Report generation and recommendations

Each module follows the vertical slice architecture pattern:
    modules/{name}/
        api/         # REST API endpoints
        domain/      # Business logic (services, models)
        infrastructure/  # Infrastructure implementations (repositories, external clients)
"""
