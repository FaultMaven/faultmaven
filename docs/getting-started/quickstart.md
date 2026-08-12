# FaultMaven Quick Start (5 Minutes)

Get FaultMaven running locally in under 5 minutes with zero external dependencies.

## Prerequisites

- **Python 3.11+** ([Download](https://www.python.org/downloads/))
- **Git** ([Download](https://git-scm.com/downloads))
- **(Optional)** OpenAI, Anthropic, or Fireworks AI API key

**System Requirements**:
- 4GB RAM minimum (8GB recommended)
- 2GB free disk space
- macOS, Linux, or Windows

---

## Step 1: Clone & Install (2 minutes)

```bash
# Clone the repository
git clone https://github.com/FaultMaven/faultmaven.git
cd faultmaven

# Create virtual environment
python -m venv .venv

# Activate virtual environment
# On Linux/macOS:
source .venv/bin/activate

# On Windows:
.venv\Scripts\activate

# Install FaultMaven (standalone)
pip install -e .
```

**What's happening?**
- Standalone installs with **zero external dependencies**
- Uses SQLite database (local file)
- Local file storage for evidence
- In-memory session management
- ChromaDB for knowledge base (embedded mode)

---

## Step 2: Configure (1 minute)

```bash
# Copy example environment file
cp .env.example .env

# Edit .env and add your LLM API key
# Use your preferred editor (nano, vim, code, etc.)
nano .env
```

**Minimal configuration** (choose one):

```bash
# Option 1: OpenAI (recommended for beginners)
CHAT_PROVIDER=openai
OPENAI_API_KEY=sk-your-openai-key-here

# Option 2: Anthropic Claude
CHAT_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-your-anthropic-key-here

# Option 3: Fireworks AI (fast and cost-effective)
CHAT_PROVIDER=fireworks
FIREWORKS_API_KEY=fw-your-fireworks-key-here
```

**No API key?** You can still run FaultMaven with a local LLM (see [Advanced Options](#advanced-options) below).

---

## Step 3: Run (1 minute)

```bash
# Start FaultMaven
python -m faultmaven.main

# Or use uvicorn directly (recommended for development)
uvicorn faultmaven.main:app --reload --host 0.0.0.0 --port 8000
```

**First startup** is fast. FaultMaven automatically:
- Creates data directories and runs database migrations
- Creates a default admin account (`admin` / `admin@local.faultmaven`)
- Ingests the 59 built-in runbooks from the **KB pack** — they ship pre-chunked and pre-embedded, so this is just vector writes (seconds), **not** an embedding run

Subsequent startups skip already-ingested runbooks (content-hash). You'll see:

```
INFO:     ✅ Auto-ingested 59 built-in runbooks into knowledge base
INFO:     🚀 FaultMaven API server startup COMPLETE
INFO:     Uvicorn running on http://0.0.0.0:8090 (Press CTRL+C to quit)
```

**Access the API**:
- **API Base**: http://localhost:8090
- **Interactive Docs**: http://localhost:8090/docs (Swagger UI)
- **Health Check**: http://localhost:8090/health

---

## Step 4: Try It Out (1 minute)

### Create a Troubleshooting Case

```bash
curl -X POST http://localhost:8090/api/v1/cases \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Website performance degradation",
    "description": "Users reporting slow page load times since 2PM"
  }'
```

**Response** (abridged):
```json
{
  "case_id": "case_abc123",
  "title": "Website performance degradation",
  "state": "inquiry",
  "current_turn": 0,
  "created_at": "2026-01-01T12:00:00Z"
}
```

### Query the AI Agent

Every exchange with a case — a question, an upload, or both — is a turn:

```bash
curl -X POST http://localhost:8090/api/v1/cases/case_abc123/turns \
  -F "query=What should I check first for this slowdown?"
```

**Response** (example, abridged):
```json
{
  "agent_response": "Start with the database query times and memory usage on the app tier. To narrow it down, share the slow-query log and the pod memory metrics for the 2PM window.",
  "turn_number": 1,
  "case_state": "inquiry",
  "milestones_completed": [],
  "progress_made": true
}
```

### Upload Data (Logs, Metrics, Config)

Raw data enters a case as a turn attachment. FaultMaven extracts the evidence
from what you upload:

```bash
curl -X POST http://localhost:8090/api/v1/cases/case_abc123/turns \
  -F "query=Here are the application logs from the incident window" \
  -F "files=@/path/to/application.log"
```

You can also paste text directly with `-F "pasted_content=..."` instead of
attaching a file.

---

## What's Running?

FaultMaven Standalone runs entirely on your local machine:

| Component | Storage Type | Location |
|-----------|--------------|----------|
| **Database** | SQLite | `./faultmaven.db` |
| **File Storage** | Local filesystem | `./data/uploads/` |
| **Sessions** | In-memory | RAM (cleared on restart) |
| **Knowledge Base** | ChromaDB (embedded) | `./data/chroma-kb/` |
| **Evidence Vectors** | ChromaDB (embedded) | `./data/chroma-evidence/` |
| **Vector Embeddings** | Sentence Transformers | Local model cache |

**No external services required!** Perfect for:
- Local development and testing
- Learning and experimentation
- Contributing to FaultMaven
- Offline troubleshooting workflows

---

## Next Steps

### Explore the API

Visit the interactive API documentation:
- **Swagger UI**: http://localhost:8090/docs
- **ReDoc**: http://localhost:8090/redoc

### Read the Guides

- **[Installation Guide](installation.md)** - Comprehensive setup instructions
- **[Architecture Overview](architecture/architecture-overview.md)** - System design and components
- **[API Documentation](api/)** - Complete API reference
- **[Development Guide](development/)** - Contributing and development setup

### Scaling Beyond Standalone

Standalone runs on fixed simple defaults (SQLite, in-process FakeRedis, embedded ChromaDB) and is single-user. Production-grade, multi-tenant infrastructure (PostgreSQL, Redis, object storage) is provided by the **Cloud** deployment — a cloud-native architecture configured via Kubernetes.

See [Installation Guide — Scaling Beyond Standalone (Cloud)](installation.md#scaling-beyond-standalone-cloud) for details.

### Join the Community

- **GitHub Discussions**: [Ask questions, share ideas](https://github.com/FaultMaven/faultmaven/discussions)
- **GitHub Issues**: [Report bugs, request features](https://github.com/FaultMaven/faultmaven/issues)
- **Documentation**: [Complete docs on GitHub](https://github.com/FaultMaven/faultmaven)

---

## Troubleshooting

### Issue: `ModuleNotFoundError: No module named 'faultmaven'`

**Cause**: Virtual environment not activated or installation failed.

**Solution**:
```bash
# Ensure virtual environment is activated
source .venv/bin/activate  # Linux/macOS
.venv\Scripts\activate      # Windows

# Reinstall
pip install -e .
```

### Issue: `ValueError: No LLM API key configured`

**Cause**: Missing API key in `.env` file.

**Solution**:
```bash
# Check .env file exists
ls -la .env

# Add API key for your chosen provider
echo "OPENAI_API_KEY=sk-your-key-here" >> .env
```

### Issue: `Port 8000 already in use`

**Cause**: Another service is using port 8000.

**Solution**:
```bash
# Use a different port
uvicorn faultmaven.main:app --port 8080

# Or find and kill the process using port 8000
lsof -ti:8090 | xargs kill -9  # Linux/macOS
```

### Issue: `ImportError: cannot import name 'XXX' from 'pydantic'`

**Cause**: Incompatible Pydantic version.

**Solution**:
```bash
# Ensure Pydantic 2.6+ is installed
pip install --upgrade 'pydantic>=2.6,<2.10'
```

### Issue: Database locked error

**Cause**: Multiple processes accessing SQLite database.

**Solution**:
```bash
# Stop all FaultMaven processes
pkill -f "python -m faultmaven"

# Restart with single process
python -m faultmaven
```

---

## Advanced Options

### Option 1: Use Local LLM (No API Key Required)

Run FaultMaven with Ollama (100% local, offline):

```bash
# Install Ollama (https://ollama.ai)
curl -fsSL https://ollama.ai/install.sh | sh

# Download a model
ollama pull llama2

# Configure FaultMaven to use local LLM
echo "CHAT_PROVIDER=local" >> .env
echo "LOCAL_LLM_URL=http://localhost:11434" >> .env
echo "LOCAL_LLM_MODEL=llama2" >> .env

# Start FaultMaven
python -m faultmaven
```

### Option 2: Docker Deployment

Run FaultMaven in Docker (production-ready):

```bash
# Build Docker image
docker build -t faultmaven:latest .

# Run container
docker run -d \
  -p 8000:8090 \
  -e OPENAI_API_KEY=sk-your-key \
  -v $(pwd)/data:/app/data \
  faultmaven:latest
```

### Option 3: Development Mode with Hot Reload

For active development:

```bash
# Install dev dependencies
pip install -e .[dev,test]

# Run with auto-reload
uvicorn faultmaven.main:app --reload --log-level debug
```

### Option 4: Custom Configuration

Fine-tune FaultMaven behavior:

```bash
# .env customization
MAX_UPLOAD_SIZE_MB=50                    # Increase upload limit
SESSION_TIMEOUT_MINUTES=240              # 4-hour session timeout
LOG_LEVEL=DEBUG                          # Verbose logging
ENABLE_WEB_SEARCH=true                   # Enable web search tool
TAVILY_API_KEY=your_tavily_key           # Web search API key
```

---

## What's Next?

You're now running FaultMaven locally! Here's what you can do:

1. **Explore the API**: Try different endpoints in Swagger UI
2. **Upload real troubleshooting data**: Logs, configs, metrics
3. **Test AI agent capabilities**: Natural language queries, evidence analysis
4. **Read the architecture docs**: Understand how FaultMaven works
5. **Contribute**: Fix bugs, add features, improve documentation

**Welcome to the FaultMaven community!** 🚀

---

## Support

Need help? We're here for you:

- **Documentation**: [github.com/FaultMaven/faultmaven](https://github.com/FaultMaven/faultmaven)
- **GitHub Discussions**: [Community Q&A](https://github.com/FaultMaven/faultmaven/discussions)
- **Issues**: [Bug reports and feature requests](https://github.com/FaultMaven/faultmaven/issues)
- **Email**: [support@faultmaven.ai](mailto:support@faultmaven.ai)
