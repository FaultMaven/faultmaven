# Local Development Setup

This guide covers running FaultMaven components as local processes instead of Docker containers. This is recommended for:

- Contributors developing FaultMaven
- Debugging and troubleshooting
- Environments where Docker isn't available

For the quickest setup, use [Docker Compose](../../README.md#quick-start) instead.

---

## Prerequisites

| Component | Requirement |
|-----------|-------------|
| **API Backend** | Python 3.11+ |
| **Dashboard** | Node.js 18+, npm or pnpm |
| **Copilot** | Node.js 18+, pnpm |
| **LLM Provider** | API key (OpenAI, Anthropic, etc.) or Ollama |

---

## Component Setup

### 1. Backend API

```bash
# Clone and setup
git clone https://github.com/FaultMaven/faultmaven.git
cd faultmaven

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Configure environment
cp .env.example .env
# Edit .env: Set your LLM API keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
# Note: AUTH_PROVIDER defaults to "no-auth" for local development (no authentication required)

# Start the server
./scripts/faultmaven-dev.sh start
# Or: python -m faultmaven.main
```

The API will be available at http://localhost:8000

### 2. Dashboard (Optional)

In a separate terminal:

```bash
# Clone dashboard repository
git clone https://github.com/FaultMaven/faultmaven-dashboard.git
cd faultmaven-dashboard

# Install dependencies
npm install

# Configure API endpoint
cp .env.example .env
# The default VITE_API_URL=http://localhost:8000 should work for local development

# Start development server
npm run dev
```

The Dashboard will be available at http://localhost:5173

### 3. Copilot Browser Extension (Optional)

In a separate terminal:

```bash
# Clone copilot repository
git clone https://github.com/FaultMaven/faultmaven-copilot.git
cd faultmaven-copilot

# Install dependencies
pnpm install

# Configure environment
cp .env.example .env

# Start development build with hot-reload
pnpm dev          # Chrome
pnpm dev:firefox  # Firefox
```

Load the extension:
- **Chrome:** `chrome://extensions` → Developer mode → Load unpacked → Select `.output/chrome-mv3/`
- **Firefox:** `about:debugging#/runtime/this-firefox` → Load Temporary Add-on → Select file in `.output/firefox-mv3/`

Configure the extension to use your local API: Extension icon → Settings → API URL: `http://localhost:8000`

---

## Development with External Services

For development scenarios requiring persistent storage across restarts, use the development Docker Compose configuration:

```bash
# Start Redis + ChromaDB
docker compose -f docker-compose.dev.yml up -d redis chromadb

# Update .env to use external services
SESSION_STORAGE_TYPE=redis
VECTOR_STORAGE_TYPE=chromadb
REDIS_URL=redis://localhost:6379
CHROMADB_URL=http://localhost:8001

# Start the API
./scripts/faultmaven-dev.sh start
```

---

## Verifying Your Setup

After starting the services, run the health check script to verify everything is working:

```bash
# From the faultmaven directory
./scripts/faultmaven-dev.sh health
```

This will check:

- API process is running on port 8000
- Dashboard dev server is running on port 5173
- All HTTP endpoints are responding correctly

---

## Access Points

| Component | URL | Notes |
|-----------|-----|-------|
| API | http://localhost:8000 | Backend server |
| API Docs | http://localhost:8000/docs | OpenAPI documentation |
| Dashboard | http://localhost:5173 | Development server (Vite with hot-reload) |
| Dashboard | http://localhost:3000 | Docker/production |

---

## Troubleshooting

### API won't start

1. Check Python version: `python --version` (requires 3.11+)
2. Verify virtual environment is activated
3. Check `.env` has valid LLM API keys
4. Review logs: `LOG_LEVEL=DEBUG ./scripts/faultmaven-dev.sh start`

### Dashboard can't connect to API

1. Verify API is running: `curl http://localhost:8000/health`
2. Check CORS settings in `.env`: `CORS_ALLOW_ORIGINS` should include `http://localhost:5173`
3. Verify `VITE_API_URL` in dashboard `.env`

### Extension not working

1. Ensure extension is loaded and enabled in browser
2. Check extension settings: API URL should be `http://localhost:8000`
3. Open browser DevTools → Console for error messages

---

## Next Steps

- [Testing Guide](./testing-standards.md) - Run the test suite
- [Architecture Guide](../architecture/README.md) - Understand the codebase
- [Contributing Guide](../CONTRIBUTING.md) - Submit changes
