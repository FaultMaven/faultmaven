# FaultMaven Architecture Overview

This document explains how FaultMaven's services work together to deliver AI-powered troubleshooting.

## System Overview

FaultMaven uses a **microservices architecture** where each service handles a specific concern. Services communicate via REST APIs and share data through Redis (cache/queue) and ChromaDB (knowledge base).

```
┌─────────────────────────────────────────────────────┐
│              Browser Extension / Dashboard           │
└────────────────────┬────────────────────────────────┘
                     │ HTTPS
                     ▼
┌─────────────────────────────────────────────────────┐
│                  API Gateway (8000)                  │
│          Routes requests + Authentication            │
└─────┬───────┬──────┬───────┬────────┬───────┬───────┘
      │       │      │       │        │       │
      ▼       ▼      ▼       ▼        ▼       ▼
   ┌─────┐ ┌────┐ ┌────┐ ┌──────┐ ┌────┐ ┌──────┐
   │Auth │ │Sess│ │Case│ │Knowl │ │Evid│ │Agent │
   │8001 │ │8002│ │8003│ │ 8004 │ │8005│ │ 8006 │
   └─────┘ └────┘ └────┘ └──────┘ └────┘ └──────┘
      │       │      │       │        │       │
      ▼       ▼      ▼       ▼        ▼       ▼
   ┌──────────────────────────────────────────────┐
   │         Infrastructure Layer                  │
   │  SQLite | Redis | ChromaDB | LLM Providers   │
   └──────────────────────────────────────────────┘
```

## Service Responsibilities

### API Gateway (Port 8000)
**Purpose:** Single entry point for all client requests

**Responsibilities:**
- Routes requests to appropriate services
- Handles authentication (JWT validation)
- Provides `/health` and `/v1/meta/capabilities` endpoints
- CORS management

**Technology:** FastAPI, Python

---

### Auth Service (Port 8001)
**Purpose:** User authentication

**Responsibilities:**
- User registration and login
- JWT token generation and validation
- Password hashing (bcrypt)

**Data Store:** SQLite (users table)

**Technology:** FastAPI, SQLAlchemy, JWT

---

### Session Service (Port 8002)
**Purpose:** Track user sessions

**Responsibilities:**
- Create and manage user sessions
- Session expiration and cleanup
- Store session metadata

**Data Store:** Redis (key-value with TTL)

**Technology:** FastAPI, Redis

---

### Case Service (Port 8003)
**Purpose:** Manage troubleshooting investigations

**Responsibilities:**
- Create and track cases
- Store messages and conversation history
- Track investigation progress
- Case status management

**Data Store:** SQLite (cases, messages tables)

**Technology:** FastAPI, SQLAlchemy

---

### Knowledge Service (Port 8004)
**Purpose:** Knowledge base with semantic search

**Responsibilities:**
- Document upload and ingestion
- Convert documents to embeddings
- Semantic search via vector similarity
- Manage three knowledge collections:
  - **User KB:** Personal runbooks (permanent)
  - **Global KB:** System-wide documentation (permanent)
  - **Case Evidence:** Investigation-specific data (ephemeral)

**Data Store:** ChromaDB (vector embeddings), SQLite (metadata)

**Technology:** FastAPI, ChromaDB, Sentence Transformers (BGE-M3)

---

### Evidence Service (Port 8005)
**Purpose:** File uploads and attachments

**Responsibilities:**
- Accept file uploads (logs, screenshots, configs)
- Store files on disk
- Retrieve files by ID
- Link files to cases

**Data Store:** Local filesystem, SQLite (metadata)

**Technology:** FastAPI, multipart/form-data

---

### Agent Service (Port 8006)
**Purpose:** AI-powered troubleshooting conversations

**Responsibilities:**
- Process user messages
- Generate AI responses
- Track investigation context
- Call Knowledge Service for relevant documentation
- Manage conversation flow

**Data Store:** None (stateless, relies on Case Service)

**Technology:** FastAPI, LangChain, OpenAI/Anthropic/Fireworks APIs

---

### Job Worker (Background)
**Purpose:** Async background task processing

**Responsibilities:**
- Process document uploads asynchronously
- Generate embeddings for knowledge base
- Clean up old cases
- Delete ephemeral evidence collections
- Generate post-mortem documentation

**Task Queue:** Redis + Celery

**Technology:** Python, Celery, Celery Beat (scheduler)

---

## Data Flow Examples

### Example 1: User Asks a Question

```
1. User types message in browser extension
2. Extension → API Gateway → Agent Service
3. Agent Service → Knowledge Service (search for relevant docs)
4. Agent Service → LLM Provider (OpenAI/Anthropic)
5. Agent Service → Case Service (store message + response)
6. Response flows back: Agent → Gateway → Extension
```

### Example 2: Upload a Document

```
1. User uploads PDF in dashboard
2. Dashboard → API Gateway → Knowledge Service
3. Knowledge Service → Job Worker (async task queued in Redis)
4. Job Worker:
   a. Extracts text from PDF
   b. Splits into chunks
   c. Generates embeddings via Sentence Transformers
   d. Stores in ChromaDB
5. Document is now searchable
```

### Example 3: Create a New Case

```
1. User starts troubleshooting session in extension
2. Extension → API Gateway → Case Service (create case)
3. Case Service stores case in SQLite
4. Extension → Agent Service (send first message)
5. Agent Service → Case Service (link message to case)
6. Conversation continues with case_id in all requests
```

## Key Design Decisions

### Why Microservices?

**Benefits:**
- **Independent scaling:** Scale Agent Service separately from Case Service
- **Technology flexibility:** Each service can use best-fit tools
- **Fault isolation:** Knowledge Service issues don't affect authentication
- **Clear boundaries:** Each service has a single responsibility

**Trade-offs:**
- More complex deployment (mitigated by docker-compose)
- Network latency between services (acceptable for our use case)

### Why SQLite for Self-Hosted?

**Benefits:**
- Zero configuration (no database server needed)
- Single file backup/restore
- Perfect for single-user or small team deployments
- Excellent performance for < 100GB data

**Enterprise uses PostgreSQL** for multi-tenancy and scale.

### Why Redis?

**Used for:**
1. **Session storage** - Fast key-value lookups with TTL
2. **Celery task queue** - Reliable message broker
3. **Cache** - Future use for API response caching

### Why ChromaDB?

**Benefits:**
- Purpose-built for embeddings/vector search
- Easy to run in Docker
- Good performance for < 1M documents
- Open source and actively maintained

### Why Multiple LLM Providers?

**Benefits:**
- **No vendor lock-in** - Switch providers anytime
- **Failover** - If OpenAI is down, try Anthropic
- **Cost optimization** - Use cheaper providers when possible
- **Feature access** - Different models for different tasks

## Security Considerations

### Authentication Flow
1. User logs in → Auth Service generates JWT
2. JWT stored in browser extension
3. All requests include JWT in `Authorization: Bearer <token>`
4. API Gateway validates JWT before routing

### Data Privacy
- All sensitive data (PII, credentials) is sanitized before sending to LLM
- Knowledge base is private per user (user_id scoping)
- Case data is isolated by user

### API Security
- Rate limiting (configurable per service)
- CORS policies
- Input validation via Pydantic models

## Deployment Modes

### Self-Hosted (Docker Compose)
**Target:** Individuals, small teams

**Stack:**
- SQLite databases (no server needed)
- Redis container
- ChromaDB container
- All services in docker-compose.yml

**Command:** `docker-compose up -d`

### Enterprise (Kubernetes)
**Target:** Organizations, 100+ users

**Changes:**
- PostgreSQL instead of SQLite
- S3/MinIO for file storage
- Helm charts for deployment
- Horizontal pod autoscaling

## Monitoring and Observability

### Health Checks
Every service exposes `/health` endpoint:
```bash
curl http://localhost:8001/health
# Response: {"status": "healthy"}
```

### Logs
- Structured JSON logging
- Stdout/stderr captured by Docker
- View with: `docker-compose logs -f service-name`

### Metrics (Future)
- Prometheus metrics endpoint (`/metrics`)
- Grafana dashboards
- Request latency, error rates, LLM usage

## Scaling Considerations

### Horizontal Scaling
These services can run multiple instances:
- ✅ API Gateway
- ✅ Agent Service
- ✅ Knowledge Service
- ✅ Job Worker

These need special handling:
- ⚠️ Session Service (requires Redis clustering)
- ⚠️ Auth Service (can scale, but shares SQLite - needs PostgreSQL)

### Vertical Scaling
Resource-intensive services:
- **Agent Service:** High CPU during LLM calls
- **Knowledge Service:** High memory for embeddings
- **Job Worker:** High CPU for document processing

## Technology Stack Summary

| Component | Technology | Why |
|-----------|------------|-----|
| **API Framework** | FastAPI | Async, OpenAPI docs, Python ecosystem |
| **Database** | SQLite / PostgreSQL | Zero-config / Scale |
| **Cache/Queue** | Redis | Fast, reliable, widely supported |
| **Vector DB** | ChromaDB | Purpose-built for embeddings |
| **LLM** | OpenAI/Anthropic/Fireworks | Best-in-class AI capabilities |
| **Embeddings** | Sentence Transformers | Open source, multilingual |
| **Task Queue** | Celery | Mature, distributed task processing |
| **Deployment** | Docker + Compose | Easy self-hosting |

## Development Workflow

### Running Locally
```bash
# Start all services
cd faultmaven-deploy
docker-compose up -d

# Check logs
docker-compose logs -f agent-service

# Rebuild after code changes
docker-compose up -d --build agent-service
```

### Testing a Service
```bash
cd fm-agent-service
python -m pytest tests/
```

### Adding a New Endpoint
1. Add route in `fm-{service}/src/{service}/api/routes/`
2. Add business logic in `fm-{service}/src/{service}/domain/`
3. Update OpenAPI docs (auto-generated from FastAPI)
4. Add tests
5. Build and deploy

## Future Architecture Improvements

- [ ] **API Gateway caching** - Cache Knowledge Service responses
- [ ] **Event bus** - Replace sync REST with async events (RabbitMQ/Kafka)
- [ ] **GraphQL** - Single query for complex data needs
- [ ] **gRPC** - Faster inter-service communication
- [ ] **Service mesh** - Istio for advanced traffic management

---

**Last Updated:** 2025-11-20
**Version:** 2.0
