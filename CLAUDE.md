# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Environment Setup
```bash
# Initial setup
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-test.txt
python -m spacy download en_core_web_lg

# Configuration
cp .env.example .env
# Edit .env with your API keys

# Hostname resolution for K8s services
echo "192.168.0.111 opik.faultmaven.local" >> /etc/hosts
echo "192.168.0.111 redis.faultmaven.local" >> /etc/hosts
echo "192.168.0.111 chromadb.faultmaven.local" >> /etc/hosts
echo "192.168.0.111 presidio.faultmaven.local" >> /etc/hosts
```

### Running the Application
```bash
# Standard startup
./run_faultmaven.sh

# Development mode (full transparency, all logs)
./run_faultmaven_dev.sh

# Docker stack (with Redis, ChromaDB dependencies)
docker-compose up --build -d

# K8s cluster connectivity (default configuration)
./run_faultmaven_dev.sh

# Alternative: Use local Redis (requires local Redis server)
REDIS_URL="redis://localhost:6379" ./run_faultmaven_dev.sh
```

### Testing
```bash
# Full test suite with coverage
pytest --cov=faultmaven tests/

# Specific test categories
pytest -m "unit"           # Unit tests only
pytest -m "integration"    # Integration tests (requires Docker)
pytest -m "llm"           # LLM-related tests
pytest -m "agent"         # Agent workflow tests
pytest -m "security"      # Security/redaction tests

# Single test
pytest tests/llm/test_router.py::TestLLMRouter::test_route_success_first_provider -v

# Advanced test runner with linting and type checking
python run_tests.py --all
python run_tests.py --unit --coverage --html
```

### Code Quality
```bash
# Pre-commit hooks (install once)
pre-commit install

# Manual linting/formatting
black faultmaven tests
flake8 faultmaven tests
mypy faultmaven
```

## Architecture Overview

### Core Design Patterns

**Centralized Provider Registry**: All LLM providers are managed through a single `PROVIDER_SCHEMA` in `infrastructure/llm/providers/registry.py`. This eliminates scattered configuration and provides unified fallback strategies.

**Dependency Injection**: Services are managed through `container.py` with singleton instances and lazy initialization. All services are injected via FastAPI dependencies.

**Five-Phase SRE Doctrine**: The AI agent follows a structured troubleshooting methodology defined in `core/agent/doctrine.py`:
1. Define Blast Radius
2. Establish Timeline  
3. Formulate Hypothesis
4. Validate Hypothesis
5. Propose Solution

**Service-Oriented Architecture**: Clear separation between API layer, service layer, core domain, and infrastructure:

```
API Layer (api/v1/) 
  ↓ Dependencies
Service Layer (services/)
  ↓ Business Logic  
Core Domain (core/)
  ↓ External Integrations
Infrastructure (infrastructure/)
```

### Key Components

**LLM Router** (`infrastructure/llm/router.py`): Handles multi-provider routing with automatic fallback, semantic caching, and request sanitization. Uses the centralized provider registry for configuration.

**Agent System** (`core/agent/`): LangGraph-based reasoning engine that follows the five-phase doctrine. Orchestrates tools and manages state transitions through troubleshooting workflows.

**Knowledge Base** (`core/knowledge/`): RAG-powered document processing using ChromaDB and BGE-M3 embeddings for context-aware retrieval. Default configuration connects to `chromadb.faultmaven.local:30432` with token authentication.

**Security Layer** (`infrastructure/security/`): K8s Presidio microservice for PII redaction with custom regex patterns. Default configuration connects to `presidio.faultmaven.local:30433/30434`. All data is sanitized before external processing.

**Observability** (`infrastructure/observability/`): Opik tracing for LLM calls and agent workflows. Default configuration connects to `opik.faultmaven.local:30080`.

### Configuration Management

**Environment Variables**: Primary configuration through `.env` file. All providers use a consistent naming pattern:
- `{PROVIDER}_API_KEY` for authentication
- `{PROVIDER}_MODEL` for default model selection  
- `CHAT_PROVIDER` to select primary provider

**Provider Addition**: To add new LLM providers, update `PROVIDER_SCHEMA` in the registry with API configuration, then users can reference it via environment variables.

**Opik Observability**: Default setup connects to team server at `opik.faultmaven.local`. For custom setups, use config templates in `scripts/config/`.

### Data Flow

1. **Browser Extension** → **FastAPI Router** → **Dependency Injection**
2. **SessionService** manages conversation state in Redis
3. **AgentService** orchestrates the five-phase troubleshooting workflow
4. **Core Agent** executes LangGraph state machine with tools
5. **LLM Router** handles provider selection and fallback
6. **Knowledge Base** provides RAG context from ChromaDB
7. **Security Layer** sanitizes all external-bound data
8. **Observability** traces all operations to Opik

### Testing Architecture

**Markers**: Tests are categorized by domain (`llm`, `agent`, `security`, etc.) and type (`unit`, `integration`).

**Coverage**: 80% minimum coverage enforced. Integration tests require Docker services.

**LLM Testing**: Uses mock responses and provider registry resets for isolation. Tests cover fallback chains and provider-specific configurations.

## Critical Implementation Notes

### LLM Provider Registry
- All provider configuration is centralized in `PROVIDER_SCHEMA`
- Adding providers requires updating the schema, not scattered config files
- Fallback chains are automatically generated based on available API keys
- Use `reset_registry()` in tests to ensure clean state

### Agent State Management
- Sessions are stored in Redis with configurable timeout
- Agent state follows LangGraph patterns with explicit state transitions
- Use `@trace` decorator on agent methods for observability

### Security Requirements
- All user input must go through `DataSanitizer.sanitize()` before LLM processing
- PII redaction is mandatory for external API calls
- Use security test markers for privacy-related functionality

### Observability Setup
- Opik hostname must resolve via `/etc/hosts` (no hardcoded IPs)
- All LLM calls and agent workflows are automatically traced
- Use custom config scripts for non-standard Opik deployments