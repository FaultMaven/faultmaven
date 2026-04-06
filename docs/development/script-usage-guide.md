# FaultMaven Deployment Scripts Usage Guide

Quick reference for using the FaultMaven deployment scripts.

## Two Deployment Modes

FaultMaven supports two local deployment modes:

| Mode | Script | Use Case | Pros | Cons |
|------|--------|----------|------|------|
| **Docker-based** | `./faultmaven.sh` | Production-like local deployment | Isolated, consistent, includes dashboard | Slower startup, requires Docker |
| **Process-based** | `./scripts/faultmaven-dev.sh` | Development and testing | Fast startup, easier debugging | API only (no dashboard) |

## Quick Start

### Docker-Based Deployment

```bash
# Start all services (API + Dashboard)
./faultmaven.sh start

# Check health
./faultmaven.sh health

# View logs
./faultmaven.sh logs

# Stop services
./faultmaven.sh stop
```

### Process-Based Deployment

```bash
# Start API only (requires Python virtual environment)
./scripts/faultmaven-dev.sh start

# Check health
./scripts/faultmaven-dev.sh health

# View logs
./scripts/faultmaven-dev.sh logs

# Stop API
./scripts/faultmaven-dev.sh stop
```

## Command Reference

### Common Commands (Both Scripts)

| Command | Description | Example |
|---------|-------------|---------|
| `start` | Start FaultMaven services | `./faultmaven.sh start` |
| `stop` | Stop all services | `./faultmaven.sh stop` |
| `restart` | Restart services | `./faultmaven.sh restart` |
| `health` | Run comprehensive health checks | `./faultmaven.sh health` |
| `logs` | Stream application logs | `./faultmaven.sh logs` |
| `create-user` | Create a new user account | `./faultmaven.sh create-user` |
| `list-users` | List all user accounts | `./faultmaven.sh list-users` |
| `delete-user` | Delete a user account | `./faultmaven.sh delete-user bob` |

### Docker-Specific Commands

| Command | Description | Example |
|---------|-------------|---------|
| `start --demo` | Start with demo/sample data | `./faultmaven.sh start --demo` |
| `build` | Build Docker images from source | `./faultmaven.sh build` |
| `ps` | Show running containers | `./faultmaven.sh ps` |
| `kill` | Force-kill all containers | `./faultmaven.sh kill` |
| `clean` | Remove data (keep images) | `./faultmaven.sh clean` |
| `clean --wipe-data` | Remove data and wipe database | `./faultmaven.sh clean --wipe-data` |
| `prune` | Remove containers and images | `./faultmaven.sh prune` |

### Process-Specific Commands

| Command | Description | Example |
|---------|-------------|---------|
| `test` | Run tests via scripts/tests.py | `./scripts/faultmaven-dev.sh test --unit` |

## Health Checks Explained

The `health` command performs comprehensive validation:

### Process-Based (`faultmaven-dev.sh health`)

```bash
Running comprehensive health checks...

Checking process... ✓ Running (PID 123456)
Checking port 8090 ownership... ✓ Listening (owned by PID 123456)

HTTP Endpoints:
---------------
API Health... ✓ OK (HTTP 200)
API Docs... ✓ OK (HTTP 200)
OpenAPI Spec... ✓ OK (HTTP 200)

✓ All health checks passed!
```

**What it checks:**
1. Process is running
2. Process owns the port (not another process)
3. HTTP health endpoint responds
4. API docs endpoint responds
5. OpenAPI spec endpoint responds

### Docker-Based (`faultmaven.sh health`)

```bash
Running comprehensive health checks...

Container Status:
-----------------
NAME                 IMAGE              STATUS
faultmaven-api-1     faultmaven-api     Up 5 minutes
faultmaven-dash-1    faultmaven-dash    Up 5 minutes

HTTP Health Checks:
-------------------
API (port 8090)... ✓ OK
Dashboard (port 3333)... ✓ OK

✓ All health checks passed!
```

**What it checks:**
1. All containers are running
2. HTTP health endpoints respond
3. Data directory size

## Port Conflict Resolution

### Symptom: "Port already in use"

```bash
$ ./scripts/faultmaven-dev.sh start

✗ Port 8090 is already in use

Diagnosing port conflict...

✗ Port 8090 is in use by Docker containers (docker-proxy)

You have two options:
  1. Stop Docker deployment:
     ./faultmaven.sh stop

  2. Use a different port for process-based deployment:
     - Edit .env and change PORT=8090 to PORT=8091
     - Then run: ./scripts/faultmaven-dev.sh start
```

### Solution Options

**Option 1: Stop conflicting deployment**
```bash
# If Docker is using the port, stop it
./faultmaven.sh stop

# Then start process-based
./scripts/faultmaven-dev.sh start
```

**Option 2: Use different port**
```bash
# Edit .env file
vi .env  # Change PORT=8090 to PORT=8091

# Start on different port
./scripts/faultmaven-dev.sh start
```

**Option 3: Run both (different ports)**
```bash
# Docker on default ports (8000, 3000)
./faultmaven.sh start

# Process-based on port 8091
vi .env  # Set PORT=8091
./scripts/faultmaven-dev.sh start
```

## Troubleshooting

### Issue: Health check fails with "Not responding"

**Check logs:**
```bash
# Process-based
tail -f /tmp/faultmaven-dev.log

# Docker-based
./faultmaven.sh logs api
```

**Common causes:**
- Service still starting up (wait 30-60 seconds)
- Port conflict (see Port Conflict Resolution above)
- Missing environment variables (check `.env` file)
- Database migration failed (check logs for errors)

### Issue: Stale PID file

**Symptom:**
```bash
$ ./scripts/faultmaven-dev.sh health

⚠ Process 123456 exists but not listening on port 8090 (stale PID file)
✗ Not running
```

**Automatic fix:**
The script automatically detects and removes stale PID files. Just run `start` again:
```bash
./scripts/faultmaven-dev.sh start
```

### Issue: "status" command deprecated warning

**Symptom:**
```bash
$ ./scripts/faultmaven-dev.sh status

⚠ 'status' command is deprecated. Use 'health' instead.
```

**Solution:**
Use `health` command instead:
```bash
./scripts/faultmaven-dev.sh health
```

## User Management

### Create a User

**Interactive:**
```bash
./faultmaven.sh create-user
# Or
./scripts/faultmaven-dev.sh create-user

# Follow prompts:
# - Username: alice
# - Email: alice@example.com (or leave empty for auto-generation)
# - Display Name: Alice Smith (or leave empty)
# - Role: user (or admin)
```

### List All Users

```bash
./faultmaven.sh list-users

# Output:
# ==================================================================================
# Found 3 user(s):
#
# #    USERNAME             EMAIL                          ROLES                USER_ID
# ----------------------------------------------------------------------------------
# 👑  1    admin                admin@localhost                admin                abc123...
#     2    alice                alice@example.com              user                 def456...
#     3    bob                  bob@example.com                user                 ghi789...
# ==================================================================================
```

### Delete a User

```bash
./faultmaven.sh delete-user bob
# Or provide username interactively
./faultmaven.sh delete-user

# Confirmation required:
# Type 'DELETE' to confirm: DELETE
```

## Access Points

### Docker-Based Deployment

After `./faultmaven.sh start`:

- **Dashboard**: http://localhost:3333
- **API**: http://localhost:8090
- **API Docs**: http://localhost:8090/docs
- **OpenAPI Spec**: http://localhost:8090/openapi.json

### Process-Based Deployment

After `./scripts/faultmaven-dev.sh start`:

- **API**: http://localhost:8090 (or custom port from `.env`)
- **API Docs**: http://localhost:8090/docs
- **OpenAPI Spec**: http://localhost:8090/openapi.json

**Note**: Process-based deployment does NOT include the dashboard.

## Log Files

### Process-Based

```bash
# Live tail
tail -f /tmp/faultmaven-dev.log

# View with script
./scripts/faultmaven-dev.sh logs
```

### Docker-Based

```bash
# All services
./faultmaven.sh logs

# Specific service
./faultmaven.sh logs api
./faultmaven.sh logs dashboard

# Last 100 lines
./faultmaven.sh logs --tail 100
```

## Environment Configuration

Both scripts use the `.env` file in the project root:

```bash
# Required: At least one LLM provider
OPENAI_API_KEY=sk-...              # OpenAI GPT
ANTHROPIC_API_KEY=sk-ant-...       # Anthropic Claude
GROQ_API_KEY=gsk-...               # Groq (FREE tier!)

# Optional: Port configuration
PORT=8090                           # API port (process-based)

# Optional: Feature flags
RUN_SCHEDULER=false                # In-process job scheduler
LAZY_LOAD_ML_MODELS=true           # Lazy ML model loading
```

## Best Practices

### Development Workflow

1. **Use process-based for quick iterations:**
   ```bash
   ./scripts/faultmaven-dev.sh start
   # Make code changes
   ./scripts/faultmaven-dev.sh restart
   ```

2. **Use Docker for integration testing:**
   ```bash
   ./faultmaven.sh start
   # Test full stack (API + Dashboard)
   ```

3. **Run health checks before committing:**
   ```bash
   ./scripts/faultmaven-dev.sh health
   ./scripts/faultmaven-dev.sh test --unit
   ```

### Production-Like Local Testing

1. **Use Docker deployment:**
   ```bash
   ./faultmaven.sh start
   ```

2. **Verify all services healthy:**
   ```bash
   ./faultmaven.sh health
   ```

3. **Monitor logs:**
   ```bash
   ./faultmaven.sh logs
   ```

### Cleanup

```bash
# Process-based: Just stop
./scripts/faultmaven-dev.sh stop

# Docker-based: Stop and optionally clean
./faultmaven.sh stop               # Stop (keep data and images)
./faultmaven.sh clean              # Stop and remove data
./faultmaven.sh prune              # Stop, remove data AND images
```

## Tips & Tricks

### Run Tests

```bash
# All tests
./scripts/faultmaven-dev.sh test

# Unit tests only
./scripts/faultmaven-dev.sh test --unit

# Integration tests only
./scripts/faultmaven-dev.sh test --integration

# Specific test file
./scripts/faultmaven-dev.sh test tests/unit/test_foo.py
```

### Check Specific Port

```bash
# What's using port 8090?
ss -tlnp | grep :8090
# or
sudo lsof -i :8090
```

### Quick Port Conflict Fix

```bash
# Stop everything FaultMaven-related
./faultmaven.sh stop
./scripts/faultmaven-dev.sh stop

# Verify ports are free
ss -tlnp | grep -E ':(8090|8000|3000)'

# Start what you need
./scripts/faultmaven-dev.sh start
```

### Demo Mode (Docker Only)

```bash
# Start with sample data for demos/testing
./faultmaven.sh start --demo

# Includes:
# - Sample runbooks in knowledge base
# - Demo user accounts
# - Example cases
```

## Getting Help

```bash
# Show usage for any script
./faultmaven.sh help
./scripts/faultmaven-dev.sh help

# Or use --help flag
./faultmaven.sh --help
./scripts/faultmaven-dev.sh --help
```

## See Also

- [QUICKSTART.md](../../QUICKSTART.md) - Getting started guide
- [Fixed Script Improvements](../working/FIXED-script-improvements-2026-02-13.md) - Recent fixes and changes
- [Testing Standards](../../.claude/standards/TESTING_STANDARDS.md) - Testing guidelines
