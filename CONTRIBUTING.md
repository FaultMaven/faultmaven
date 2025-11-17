# Contributing to FaultMaven

Thank you for your interest in contributing to FaultMaven! This document explains our development model and how to contribute effectively.

---

## 🏗️ Development Model: Enterprise Superset

FaultMaven uses a unique **upstream/downstream** architecture:

### Public Repositories (Upstream)
- **Role:** Single source of truth for all core logic
- **License:** Apache 2.0 (fully open source)
- **Location:** `github.com/FaultMaven/*`
- **Registry:** Docker Hub (`faultmaven/*`)

### Private Repositories (Downstream)
- **Role:** Extends public foundation with enterprise features
- **License:** Proprietary
- **Location:** Internal/private GitHub repos
- **Registry:** GitHub Container Registry (GHCR)

**Key Principle:**
> All bug fixes and core improvements are made in PUBLIC repositories first. Private enterprise repos consume public code via Docker images.

---

## 🎯 Contributing to Public Repositories

### What Belongs in Public vs Private

**✅ Public (This Repo):**
- Core troubleshooting logic
- RAG & knowledge base functionality
- Case management (single-user)
- Session management
- JWT authentication
- SQLite/Redis support
- Multi-LLM integrations (OpenAI, Anthropic, Fireworks)
- Browser extension (chat interface)
- Dashboard (KB management UI)

**❌ Private (Enterprise Only):**
- Organizations & teams
- RBAC & permissions
- PostgreSQL support
- Multi-tenancy
- SSO/SAML
- Advanced analytics
- Compliance features (SOC2, HIPAA)

### How to Contribute

1. **Fork** the repository you want to contribute to
2. **Create** a feature branch (`git checkout -b feature/amazing-feature`)
3. **Make** your changes
4. **Test** locally using `docker-compose` (see Testing section below)
5. **Commit** your changes (`git commit -m 'Add amazing feature'`)
6. **Push** to your fork (`git push origin feature/amazing-feature`)
7. **Open** a Pull Request

### Pull Request Guidelines

- Provide a clear description of the changes
- Reference any related issues
- Ensure all tests pass
- Update documentation if needed
- Keep changes focused (one feature/fix per PR)

---

## 🧹 Cleansing Workflow (Maintainers Only)

This section documents how maintainers port new features from private enterprise repos to public repos.

### Step 1: Copy Code from Private Repo

```bash
# Navigate to private enterprise service
cd /path/to/fm-case-service-enterprise

# Copy relevant code to public repo
cp -r src/core /path/to/public/fm-case-service/src/
```

### Step 2: Cleanse Enterprise Features

Remove or modify the following:

**Database Fields:**
```python
# REMOVE these fields from SQLAlchemy models
organization_id: Mapped[str]
team_id: Mapped[str]
created_by_user_id: Mapped[str]
```

**RBAC Logic:**
```python
# REMOVE permission checks
@require_permission("cases:write")
def create_case(...):
    pass

# REMOVE organization filtering
cases = db.query(Case).filter(Case.organization_id == org_id).all()
```

**PostgreSQL Support:**
```python
# REMOVE psycopg2 from requirements.txt
# psycopg2-binary==2.9.9  # <-- DELETE THIS

# REMOVE PostgreSQL connection logic
if db_type == "postgresql":
    engine = create_async_engine(postgres_url)  # <-- DELETE THIS
```

**Multi-Tenancy:**
```python
# REMOVE tenant context managers
with tenant_context(org_id):
    # ...
```

### Step 3: Simplify to SQLite

```python
# PUBLIC version should use SQLite exclusively
DATABASE_URL = "sqlite+aiosqlite:///data/cases.db"

# Remove conditional database logic
engine = create_async_engine(DATABASE_URL)
```

### Step 4: Update Dependencies

```bash
# requirements.txt (PUBLIC version)
fastapi==0.109.0
sqlalchemy==2.0.25
aiosqlite==0.19.0           # SQLite async driver
redis==5.0.1                # Session storage
chromadb==0.4.22            # Vector DB
# DO NOT include: psycopg2-binary, boto3, supabase-py
```

### Step 5: Test in Public Stack

```bash
cd /path/to/faultmaven-deploy
docker-compose down
docker-compose build
docker-compose up -d

# Verify service works
curl http://localhost:8000/health
```

### Step 6: Publish to Docker Hub

```bash
# Tag the release
git tag v1.0.0
git push origin v1.0.0

# GitHub Actions will automatically:
# - Build Docker image
# - Push to faultmaven/fm-case-service:v1.0.0
# - Update :latest tag
```

### Step 7: Update Private Repo to Inherit

```dockerfile
# fm-case-service-enterprise/Dockerfile
FROM faultmaven/fm-case-service:v1.0.0

# Add enterprise overlays
COPY ./enterprise /app/enterprise

# Install PostgreSQL driver
RUN pip install psycopg2-binary==2.9.9

# Override entrypoint if needed
COPY ./enterprise-entrypoint.sh /app/entrypoint.sh
```

---

## 🧪 Testing Your Contributions

### Local Development Setup

1. **Clone the deploy repository:**
   ```bash
   git clone https://github.com/FaultMaven/faultmaven-deploy.git
   cd faultmaven-deploy
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env and add your LLM API key
   ```

3. **Start services:**
   ```bash
   docker-compose up -d
   ```

4. **Verify health:**
   ```bash
   curl http://localhost:8000/health
   curl http://localhost:8000/v1/meta/capabilities
   ```

### Testing Your Service Changes

If you're contributing to a specific microservice:

1. **Build your modified service:**
   ```bash
   cd /path/to/fm-case-service
   docker build -t faultmaven/fm-case-service:local .
   ```

2. **Update docker-compose.yml to use local image:**
   ```yaml
   services:
     case:
       image: faultmaven/fm-case-service:local  # Use local build
       # ... rest of config
   ```

3. **Restart the service:**
   ```bash
   docker-compose up -d case
   ```

4. **Test your changes:**
   ```bash
   # Example: Test case creation
   curl -X POST http://localhost:8000/v1/cases \
     -H "Content-Type: application/json" \
     -d '{"title": "Test Case", "description": "Testing changes"}'
   ```

---

## 📝 Code Style Guidelines

### Python (Backend Services)

- Follow **PEP 8** style guide
- Use **type hints** for all functions
- Use **async/await** for I/O operations
- Format code with **Black** (line length: 100)
- Sort imports with **isort**

```python
# Good example
async def get_case(case_id: str, db: AsyncSession) -> Case:
    """Retrieve a case by ID.

    Args:
        case_id: The case identifier
        db: Database session

    Returns:
        The case object

    Raises:
        NotFoundError: If case doesn't exist
    """
    result = await db.execute(select(Case).where(Case.id == case_id))
    case = result.scalar_one_or_none()
    if not case:
        raise NotFoundError(f"Case {case_id} not found")
    return case
```

### TypeScript (Frontend)

- Use **TypeScript** (no plain JavaScript)
- Follow **React best practices** (hooks, functional components)
- Use **Tailwind CSS** for styling
- Format code with **Prettier**

```typescript
// Good example
interface Case {
  id: string;
  title: string;
  description: string;
  createdAt: Date;
}

const CaseList: React.FC = () => {
  const [cases, setCases] = useState<Case[]>([]);

  useEffect(() => {
    fetchCases().then(setCases);
  }, []);

  return (
    <div className="space-y-4">
      {cases.map(case => (
        <CaseCard key={case.id} case={case} />
      ))}
    </div>
  );
};
```

---

## 🐛 Reporting Bugs

1. **Search** existing issues to avoid duplicates
2. **Use** the bug report template
3. **Include:**
   - Steps to reproduce
   - Expected behavior
   - Actual behavior
   - Environment (Docker version, OS, etc.)
   - Logs (if applicable)

---

## 💡 Feature Requests

1. **Check** if the feature belongs in public or enterprise
2. **Use** the feature request template
3. **Explain:**
   - The problem you're trying to solve
   - Your proposed solution
   - Why this should be in the public version

**Note:** Features requiring multi-tenancy, RBAC, or PostgreSQL should be requested via the enterprise support channel.

---

## 📜 Commit Message Guidelines

Use conventional commits:

```
feat: Add RAG reranking support
fix: Resolve session timeout issue
docs: Update deployment guide
refactor: Simplify auth middleware
test: Add integration tests for knowledge service
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `refactor`: Code refactoring
- `test`: Adding or updating tests
- `chore`: Maintenance tasks
- `perf`: Performance improvements

---

## 🔒 Security Vulnerabilities

**DO NOT** open public issues for security vulnerabilities.

Instead, see [SECURITY.md](SECURITY.md) for reporting instructions.

---

## 📄 License

By contributing to FaultMaven, you agree that your contributions will be licensed under the Apache 2.0 License.

This includes:
- ✅ Grant of copyright license
- ✅ Grant of patent license
- ✅ Right to sublicense and distribute

See [LICENSE](LICENSE) for full details.

---

## 🤝 Community

- **GitHub Discussions:** For questions and general discussion
- **GitHub Issues:** For bug reports and feature requests
- **Pull Requests:** For code contributions

We strive to maintain a welcoming and inclusive community. Please read our [Code of Conduct](CODE_OF_CONDUCT.md).

---

## 🙏 Thank You

Your contributions help make FaultMaven better for everyone. We appreciate your time and effort!

**FaultMaven Maintainers**
