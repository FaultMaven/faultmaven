# FaultMaven User Guide

Welcome to FaultMaven! This guide will help you get started with the AI-powered troubleshooting copilot.

## 1. System Requirements

* **Python:** Version 3.11 or higher
* **Docker & Docker Compose:** For running backing services
* **Browser:** Chrome or Firefox (for the Copilot Extension)

## 2. Installation

### Option A: Docker (Recommended for Users)

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/FaultMaven/faultmaven.git
    cd faultmaven
    ```

2.  **Configure Environment Variables**
    ```bash
    cp .env.example .env
    # Edit .env and add at least one LLM provider API key
    ```

3.  **Start the Stack**
    ```bash
    docker-compose up --build -d
    ```

4.  **Verify Services**
    ```bash
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    ```
    
    You should see:
    ```
    NAMES               STATUS              PORTS
    faultmaven-backend  Up                  0.0.0.0:8000->8000/tcp
    chromadb            Up                  8000/tcp
    redis               Up                  6379/tcp
    ```

### Option B: Local Development Setup

See the **[Development Docs](../development/)** for detailed development setup.

## 3. Core Concepts

FaultMaven uses three main concepts for organizing troubleshooting work:

### Sessions
- **Purpose**: Browser-level context and lifecycle management
- **Duration**: Active while browser is open, can be resumed across restarts
- **Use Case**: Maintains your connection and session state

### Cases
- **Purpose**: Investigation containers for specific problems
- **Duration**: Persistent, can span multiple sessions
- **Use Case**: Track a single incident from start to resolution

### Queries
- **Purpose**: Individual questions or data submissions within a case
- **Duration**: Single request-response cycle
- **Use Case**: Each interaction with the AI agent

**Relationship**: `Session → Contains → Multiple Cases → Each Contains → Multiple Queries`

## 4. Using FaultMaven

### Step 1: Create a Session

Sessions provide browser-level continuity and can be resumed:

```bash
# Create new session
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "my-device-123",
    "timeout_minutes": 60,
    "session_type": "troubleshooting"
  }' \
  http://localhost:8000/api/v1/sessions

# Response includes:
# - session_id: Your session identifier
# - session_resumed: Whether an existing session was resumed
```

### Step 2: Create a Case

Cases represent specific investigations:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: <your-session-id>" \
  -d '{
    "title": "API latency spike investigation",
    "description": "Users reporting slow API responses",
    "severity": "high"
  }' \
  http://localhost:8000/api/v1/cases

# Response includes:
# - case_id: Your case identifier
# - status: Case status (OPEN)
```

### Step 3: Upload Data (Optional)

Upload logs, metrics, or configuration files to a case:

```bash
curl -X POST \
  -F "file=@/path/to/your/logfile.log" \
  -F "data_type=logs" \
  -H "X-Session-ID: <your-session-id>" \
  http://localhost:8000/api/v1/cases/<case-id>/data

# Response includes:
# - data_id: Uploaded data identifier
# - status: Processing status
```

### Step 4: Submit Queries

Ask questions or request analysis within your case:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: <your-session-id>" \
  -d '{
    "query": "What are the most common errors in the uploaded logs?",
    "include_plan": true
  }' \
  http://localhost:8000/api/v1/cases/<case-id>/queries

# Response includes:
# - query_id: Query identifier for tracking
# - status: Processing status (COMPLETED or PROCESSING)
# - response: AI agent response with analysis
```

### Step 5: Continue the Investigation

Continue submitting queries to build your investigation:

```bash
# Follow-up query
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: <your-session-id>" \
  -d '{
    "query": "Can you propose a solution for the timeout errors?",
    "include_evidence": true
  }' \
  http://localhost:8000/api/v1/cases/<case-id>/queries
```

## 5. Using the Browser Extension

For a better experience, use the **FaultMaven Copilot** browser extension:

1. **Install the Extension**
   - Download from [Chrome Web Store] or [Firefox Add-ons]
   - Or build from source: https://github.com/FaultMaven/faultmaven-copilot

2. **Configure API Connection**
   - Open extension settings
   - Set API URL: `http://localhost:8000` (for local) or your deployed URL
   - The extension will automatically manage sessions and cases

3. **Start Troubleshooting**
   - Click the FaultMaven icon in your browser
   - Create a new case or resume an existing one
   - Upload files, paste logs, or ask questions
   - The AI will guide you through investigation phases

## 6. Main API Endpoints

For programmatic access, these are the core endpoints:

### Session Management
- `POST /api/v1/sessions` - Create or resume a session
- `GET /api/v1/sessions/{session_id}` - Get session details
- `DELETE /api/v1/sessions/{session_id}` - End a session

### Case Management
- `POST /api/v1/cases` - Create a new case
- `GET /api/v1/cases/{case_id}` - Get case details
- `GET /api/v1/cases` - List your cases
- `POST /api/v1/cases/{case_id}/queries` - Submit query to case
- `POST /api/v1/cases/{case_id}/data` - Upload data to case

### Knowledge Base
- `POST /api/v1/knowledge/documents` - Upload runbooks/docs
- `GET /api/v1/knowledge/documents` - List your documents
- `POST /api/v1/knowledge/search` - Search knowledge base

### Data Processing
- `POST /api/v1/data/upload` - Upload data for analysis
- `GET /api/v1/data/{data_id}` - Get processed data

## 7. Response Types

FaultMaven uses 7 intelligent response types based on investigation context:

1. **ANSWER** - Direct answer to your question
2. **PLAN_PROPOSAL** - Investigation plan suggestion
3. **CLARIFICATION_REQUEST** - Needs more information
4. **CONFIRMATION_REQUEST** - Requests approval for actions
5. **SOLUTION_READY** - Proposed solution ready
6. **NEEDS_MORE_DATA** - Requires additional evidence
7. **ESCALATION_REQUIRED** - Issue requires human escalation

## 8. Advanced Features

### Multi-Session Support
- Maintain multiple concurrent troubleshooting sessions
- Resume sessions across browser restarts using `client_id`
- Access sessions from multiple devices

### Knowledge Base Integration
- Upload your team's runbooks and documentation
- AI automatically searches relevant docs during investigations
- Private, scoped to your user account

### Investigation Phases
FaultMaven guides you through 7 structured phases:
0. **Intake** - Problem understanding
1. **Blast Radius** - Impact assessment
2. **Timeline** - Event reconstruction
3. **Hypothesis** - Root cause theories
4. **Validation** - Testing hypotheses
5. **Solution** - Fix proposal
6. **Documentation** - Post-mortem

## 9. Configuration

### Environment Variables

Key configuration options in `.env`:

```env
# LLM Provider (required - choose one)
CHAT_PROVIDER=fireworks  # fireworks, openai, anthropic, gemini, local
FIREWORKS_API_KEY=your_api_key
OPENAI_API_KEY=your_api_key
ANTHROPIC_API_KEY=your_api_key

# Services
REDIS_URL=redis://localhost:6379
CHROMA_HOST=localhost
CHROMA_PORT=8000

# Security
PII_REDACTION_ENABLED=true
PRESIDIO_URL=http://localhost:3001

# Observability (optional)
OPIK_ENABLED=true
OPIK_URL=http://localhost:5555
```

### Supported LLM Providers

FaultMaven supports 7 LLM providers with automatic fallback:

- **Fireworks AI**: `llama-v3p1-8b-instruct`, `llama-v3p1-70b-instruct`, `mixtral-8x7b-instruct`
- **OpenAI**: `gpt-4o`, `gpt-4o-mini`, `gpt-3.5-turbo`
- **Anthropic**: `claude-3-5-sonnet-20241022`, `claude-3-haiku-20240307`
- **Google Gemini**: `gemini-1.5-pro`, `gemini-1.5-flash`
- **HuggingFace**: Various open-source models
- **OpenRouter**: Multi-provider access
- **Local**: Ollama, vLLM, or any OpenAI-compatible server

## 10. Troubleshooting

### Common Issues

**Connection Refused**
```bash
# Check if services are running
docker ps
# Restart if needed
docker-compose restart
```

**API Key Errors**
```bash
# Verify your .env file has at least one LLM provider API key
cat .env | grep API_KEY
```

**Session Expired**
```bash
# Sessions expire after timeout_minutes (default: 30)
# Create a new session or use client_id for resumption
```

**Service Health Check**
```bash
# Check all services are healthy
curl http://localhost:8000/health/dependencies
```

## 11. Next Steps

- **[API Documentation](../api/README.md)** - Complete API reference
- **[Architecture Overview](../architecture/architecture-overview.md)** - System design
- **[Contributing Guidelines](../CONTRIBUTING.md)** - Contributing to FaultMaven
- **[Security Guide](../security/)** - Security and privacy features

## 12. Support

- **GitHub Issues**: https://github.com/FaultMaven/faultmaven/issues
- **Discord Community**: https://discord.com/faultmaven
- **Email**: support@faultmaven.ai

---

**Last Updated**: 2025-10-12  
**Version**: 2.0
