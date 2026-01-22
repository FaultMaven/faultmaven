# FaultMaven Current Architecture

## Overview

FaultMaven is a modular monolith application for AI-powered software fault investigation and resolution.

## Architecture Layers

### 1. API Layer (`faultmaven/api/`)
- FastAPI-based REST API endpoints
- Request/response handling
- Authentication middleware
- Route definitions

### 2. Services Layer (`faultmaven/services/`)
- Business logic implementation
- Service orchestration
- Data processing pipelines
- Evidence artifact management

### 3. Core Layer (`faultmaven/core/`)
- Domain models
- Core utilities
- Shared abstractions
- Service factories

### 4. Infrastructure Layer (`faultmaven/infrastructure/`)
- Database persistence
- External service integrations
- File storage
- Caching (Redis)
- Vector storage (ChromaDB)

### 5. Configuration (`faultmaven/config/`)
- Centralized settings management
- Environment-based configuration
- Feature flags
- Security settings

## Key Components

### Dependency Injection Container
- Centralized service registration
- Lifecycle management
- Health checking
- Service discovery

### Agent System
- LLM-powered investigation agent
- Prompt management
- Conversation flow
- Tool integration

### Knowledge Base
- Document ingestion
- Vector embeddings
- Semantic search
- RAG (Retrieval-Augmented Generation)

### Protection System
- Input sanitization
- Rate limiting
- Security monitoring
- Threat detection

## Design Principles

1. **Modularity**: Clear separation of concerns
2. **Testability**: Dependency injection and mocking
3. **Scalability**: Async operations and caching
4. **Security**: Defense in depth, zero trust
5. **Observability**: Logging, tracing, metrics

## Technology Stack

- **Framework**: FastAPI
- **Language**: Python 3.11+
- **Database**: PostgreSQL, Redis
- **Vector DB**: ChromaDB
- **LLM Providers**: OpenAI, Anthropic, Fireworks
- **Testing**: pytest, pytest-asyncio

## Future Roadmap

- Enhanced multi-tenancy support
- Advanced workflow automation
- Real-time collaboration features
- Enterprise SSO integration
