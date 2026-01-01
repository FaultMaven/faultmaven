"""FaultMaven Modules Package

This package contains vertical slice modules that own complete feature stacks:
- knowledge: Knowledge base, RAG, vector search, and semantic search
- (future modules will be added here as they're extracted)

Each module follows the vertical slice architecture pattern:
    modules/{name}/
        api/         # REST API endpoints
        domain/      # Business logic (services, models)
        infrastructure/  # Infrastructure implementations (repositories, external clients)
"""
