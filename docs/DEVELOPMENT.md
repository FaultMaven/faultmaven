# FaultMaven Development Setup

This guide will help you set up a local development environment for contributing to FaultMaven.

## Prerequisites

### Required
- **Git** - Version control
- **Docker** - For running infrastructure (Redis, ChromaDB)
- **Docker Compose** - For orchestrating services
- **Python 3.11+** - For running services locally
- **Node.js 18+** - For browser extension development (optional)

### Recommended
- **VSCode** or **PyCharm** - IDEs with Python support
- **Postman** or **HTTPie** - For API testing
- **pgAdmin** or **DBeaver** - For database inspection (optional)

---

## Quick Start

### 1. Clone Repositories

You can work on individual services or clone everything:

```bash
# Clone the repository you want to work on
git clone https://github.com/FaultMaven/fm-agent-service.git
cd fm-agent-service

# Or clone multiple services
mkdir faultmaven-dev
cd faultmaven-dev
git clone https://github.com/FaultMaven/fm-auth-service.git
git clone https://github.com/FaultMaven/fm-case-service.git
git clone https://github.com/FaultMaven/fm-agent-service.git
# ... etc
```

### 2. Set Up Infrastructure

Start Redis and ChromaDB (required by most services):

```bash
# Create docker-compose.yml for infrastructure
cat > docker-compose-infra.yml <<EOF
version: '3.8'
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data

  chromadb:
    image: chromadb/chroma:latest
    ports:
      - "8000:8000"
    volumes:
      - chromadb-data:/chroma/chroma
    environment:
      - ANONYMIZED_TELEMETRY=False

volumes:
  redis-data:
  chromadb-data:
EOF

# Start infrastructure
docker-compose -f docker-compose-infra.yml up -d
```

### 3. Set Up Python Environment

For each service you're working on:

```bash
cd fm-agent-service

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -e .              # Install service
pip install -e ".[dev]"       # Install dev dependencies (pytest, etc.)
```

### 4. Configure Environment

Create `.env` file:

```bash
# LLM Provider (required for agent service)
OPENAI_API_KEY=sk-...
# OR
ANTHROPIC_API_KEY=sk-ant-...
# OR
FIREWORKS_API_KEY=fw_...

# Database
DB_TYPE=sqlite
PROFILE=public

# Infrastructure
REDIS_HOST=localhost
REDIS_PORT=6379
CHROMADB_HOST=localhost
CHROMADB_PORT=8000

# Development
DEBUG=true
LOG_LEVEL=DEBUG
```

### 5. Run Service Locally

```bash
# Start the service
uvicorn agent_service.main:app --reload --port 8006

# Service will auto-reload on code changes
```

---

## Development Workflow

### Making Changes

1. **Create a feature branch**
   ```bash
   git checkout -b feature/add-hypothesis-tracking
   ```

2. **Make your changes**
   - Edit code in `src/`
   - Add tests in `tests/`
   - Update documentation if needed

3. **Run tests**
   ```bash
   pytest                    # Run all tests
   pytest tests/unit         # Run unit tests only
   pytest -v                 # Verbose output
   pytest --cov=agent_service  # With coverage
   ```

4. **Check code quality**
   ```bash
   # Format code
   black src tests

   # Check style
   ruff src tests

   # Type checking (if service uses it)
   mypy src
   ```

5. **Commit and push**
   ```bash
   git add .
   git commit -m "feat: Add hypothesis tracking to agent service"
   git push origin feature/add-hypothesis-tracking
   ```

6. **Create Pull Request**
   - Go to GitHub and create a PR
   - Fill in the PR template
   - Wait for CI checks and review

---

## Testing

### Unit Tests

Test individual functions without external dependencies:

```bash
pytest tests/unit -v
```

**Example unit test:**
```python
def test_message_validation():
    from agent_service.models import Message

    msg = Message(
        role="user",
        content="Test message"
    )
    assert msg.role == "user"
    assert msg.content == "Test message"
```

### Integration Tests

Test services with real infrastructure:

```bash
# Ensure Redis and ChromaDB are running
docker-compose -f docker-compose-infra.yml up -d

# Run integration tests
pytest tests/integration -v
```

**Example integration test:**
```python
def test_knowledge_search(client):
    """Test searching the knowledge base"""
    response = client.post(
        "/v1/knowledge/search",
        json={"query": "database timeout"}
    )
    assert response.status_code == 200
    assert "results" in response.json()
```

### Running Specific Tests

```bash
# Single test file
pytest tests/unit/test_agent.py

# Single test function
pytest tests/unit/test_agent.py::test_message_validation

# Tests matching pattern
pytest -k "agent"
```

### Test Coverage

```bash
# Generate coverage report
pytest --cov=agent_service --cov-report=html

# Open in browser
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```

---

## Working on Specific Services

### Agent Service

**Location:** `fm-agent-service/`

**Key files:**
- `src/agent_service/core/agent/` - AI agent implementation
- `src/agent_service/api/routes/` - API endpoints

**Development:**
```bash
# Start dependencies
docker-compose -f docker-compose-infra.yml up -d

# Run service
cd fm-agent-service
source .venv/bin/activate
uvicorn agent_service.main:app --reload --port 8006

# Test endpoint
curl http://localhost:8006/health
```

---

### Knowledge Service

**Location:** `fm-knowledge-service/`

**Key files:**
- `src/knowledge_service/core/knowledge/` - Knowledge base logic
- `src/knowledge_service/infrastructure/` - ChromaDB integration

**Development:**
```bash
# Ensure ChromaDB is running
docker-compose -f docker-compose-infra.yml up -d chromadb

# Run service
cd fm-knowledge-service
source .venv/bin/activate
uvicorn knowledge_service.main:app --reload --port 8004
```

---

### Browser Extension

**Location:** `faultmaven-copilot/`

**Setup:**
```bash
cd faultmaven-copilot
pnpm install

# Development build with hot reload
pnpm dev

# Load extension in browser:
# Chrome: chrome://extensions → Load unpacked → .output/chrome-mv3
# Firefox: about:debugging → Load Temporary Add-on → .output/firefox-mv2
```

**Tech stack:**
- WXT framework (WebExtension toolkit)
- React + TypeScript
- Tailwind CSS

---

## Debugging

### Python Services

**VSCode `launch.json`:**
```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Run Agent Service",
      "type": "python",
      "request": "launch",
      "module": "uvicorn",
      "args": [
        "agent_service.main:app",
        "--reload",
        "--port",
        "8006"
      ],
      "envFile": "${workspaceFolder}/.env"
    }
  ]
}
```

**Set breakpoints** in VSCode and press F5 to start debugging.

### Viewing Logs

```bash
# Service logs (if running in Docker)
docker-compose logs -f agent-service

# Infrastructure logs
docker-compose -f docker-compose-infra.yml logs -f redis
docker-compose -f docker-compose-infra.yml logs -f chromadb
```

### Database Inspection

**SQLite:**
```bash
# Install sqlite3
sqlite3 data/cases.db

# View tables
.tables

# Query data
SELECT * FROM cases LIMIT 10;
```

**ChromaDB:**
```python
import chromadb

client = chromadb.HttpClient(host="localhost", port=8000)
collections = client.list_collections()
print(collections)
```

---

## Common Tasks

### Add a New API Endpoint

1. **Add route** in `src/{service}/api/routes/`
   ```python
   @router.post("/new-endpoint")
   async def new_endpoint(data: RequestModel):
       return {"status": "success"}
   ```

2. **Add business logic** in `src/{service}/core/` or `src/{service}/domain/`

3. **Add tests** in `tests/`

4. **Test manually:**
   ```bash
   curl -X POST http://localhost:8006/v1/new-endpoint \
     -H "Content-Type: application/json" \
     -d '{"key":"value"}'
   ```

### Update Database Schema

**For SQLite services:**

1. Update model in `src/{service}/models/`
2. Create migration (if using Alembic):
   ```bash
   alembic revision --autogenerate -m "Add new column"
   alembic upgrade head
   ```

3. Or delete and recreate database (dev only):
   ```bash
   rm data/*.db
   # Restart service to recreate
   ```

### Add New Dependency

```bash
# Add to pyproject.toml
[project]
dependencies = [
    "new-package>=1.0.0"
]

# Reinstall
pip install -e .
```

### Regenerate Requirements

Some services use `requirements.txt`:

```bash
pip freeze > requirements.txt
```

---

## IDE Setup

### VSCode

**Recommended extensions:**
- Python (Microsoft)
- Pylance
- Ruff
- Black Formatter
- Docker
- GitLens

**Settings (`.vscode/settings.json`):**
```json
{
  "python.linting.enabled": true,
  "python.linting.ruffEnabled": true,
  "python.formatting.provider": "black",
  "editor.formatOnSave": true,
  "[python]": {
    "editor.defaultFormatter": "ms-python.black-formatter"
  }
}
```

### PyCharm

1. **Interpreter:** Set to `.venv/bin/python`
2. **Code Style:** Configure Black formatter
3. **Run Configuration:** Add Uvicorn run config

---

## Troubleshooting

### Service won't start

**Check dependencies:**
```bash
docker ps  # Ensure Redis/ChromaDB are running
```

**Check environment:**
```bash
cat .env  # Ensure all required vars are set
```

**Check logs:**
```bash
# Look for errors in terminal output
uvicorn agent_service.main:app --reload --log-level debug
```

### Tests failing

**Update dependencies:**
```bash
pip install -e ".[dev]"
```

**Clear cache:**
```bash
pytest --cache-clear
```

**Run in verbose mode:**
```bash
pytest -vv --tb=long
```

### Import errors

**Ensure package is installed in editable mode:**
```bash
pip install -e .
```

**Check PYTHONPATH:**
```bash
export PYTHONPATH=/path/to/project/src:$PYTHONPATH
```

---

## Getting Help

- **GitHub Issues:** Report bugs or request features
- **GitHub Discussions:** Ask questions or share ideas
- **Code Comments:** Check inline documentation
- **API Docs:** Visit http://localhost:8006/docs for interactive API testing

---

## Best Practices

### Code Style
- Follow PEP 8 (enforced by Black and Ruff)
- Use type hints
- Write docstrings for public functions
- Keep functions small and focused

### Testing
- Write tests for new features
- Maintain test coverage above 70%
- Use descriptive test names
- Mock external dependencies in unit tests

### Git
- Use meaningful commit messages
- Keep commits small and focused
- Rebase before pushing to keep history clean
- Reference issues in commits (e.g., "fixes #123")

### Documentation
- Update README when adding features
- Keep API docs in sync with code
- Add inline comments for complex logic

---

**Last Updated:** 2025-11-20
**Version:** 2.0
**Status:** Current
