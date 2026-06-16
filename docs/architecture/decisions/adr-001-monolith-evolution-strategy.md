# ADR-001: FaultMaven Platform Evolution Strategy

**Date**: 2025-12-28
**Status**: Accepted
**Decision Makers**: Engineering Leadership, Product Team
**Affects**: Platform Architecture, Development Roadmap, Community Adoption

---

## Context

FaultMaven had two competing codebases representing different architectural approaches:

1. **FaultMaven-Mono** (Legacy Monolith)
   - Monolithic architecture with 7,770 lines of production code
   - **1,425 tests** with **71% code coverage**
   - Complete enterprise features: 7 LLM providers, PII redaction (Presidio), observability (Opik tracing, Prometheus metrics)
   - Battle-tested in production environments
   - Database: PostgreSQL/SQLite with SQLAlchemy ORM
   - Session management: Redis/in-memory
   - Strong test coverage across all modules

2. **faultmaven** (Modular Microservices)
   - Modern modular monolith architecture (auth, session, case, knowledge, evidence, agent modules)
   - **148 tests** with **47% code coverage**
   - Clean architecture with dependency injection
   - Interface-based design patterns
   - Missing enterprise features (Presidio, Opik, Prometheus)
   - Limited test coverage

**The Decision Point**: Which codebase should become the production platform for FaultMaven's future?

---

## Decision

We have chosen to **evolve FaultMaven-Mono** as the production codebase, applying incremental refactoring to adopt the modular architecture patterns from `faultmaven` over a structured **20-week evolution strategy**.

The repository has been renamed from `FaultMaven-Mono` to **`faultmaven`** to reflect its new status as the official platform.

---

## Rationale

### Technical Factors

#### 1. Test Coverage as Production Readiness Indicator

| Metric | FaultMaven-Mono | faultmaven (modular) |
|--------|-----------------|---------------------|
| **Tests** | 1,425 tests | 148 tests |
| **Coverage** | 71% | 47% |
| **Test Quality** | Production-grade | Basic unit tests |

**Analysis**: The 1,425 tests in FaultMaven-Mono represent years of edge case handling, integration scenarios, and production bug fixes. This is not just code—it's **encoded knowledge** of how the system behaves in real-world conditions.

#### 2. Enterprise Feature Completeness

**FaultMaven-Mono includes**:
- ✅ 7 LLM providers (OpenAI, Anthropic, Fireworks, Gemini, Groq, HuggingFace, Local)
- ✅ PII redaction (Presidio microservice integration)
- ✅ Distributed tracing (Opik)
- ✅ Metrics collection (Prometheus)
- ✅ Multi-session management (Redis)
- ✅ Knowledge base (ChromaDB with RAG)
- ✅ Evidence processing pipelines
- ✅ Case lifecycle management

**faultmaven (modular) lacks**:
- ❌ Presidio integration
- ❌ Opik tracing
- ❌ Prometheus metrics
- ❌ Multi-LLM fallback chains
- ❌ Production-tested evidence processing

**Analysis**: Rebuilding these features in `faultmaven` would require 8-12 weeks of development and extensive testing.

#### 3. Battle-Tested Code

FaultMaven-Mono has:
- Production deployment history
- Real user feedback incorporated
- Performance optimizations
- Security hardening
- Error handling for edge cases

**Analysis**: Production experience is irreplaceable. Starting fresh means re-encountering solved problems.

### Strategic Factors

#### 1. Time to Market

| Approach | Estimated Timeline |
|----------|-------------------|
| **Evolve FaultMaven-Mono** | **20 weeks** (4 phases) |
| **Rebuild on faultmaven** | **40+ weeks** (rewrite all features) |

**Analysis**: Evolving FaultMaven-Mono delivers a production-ready platform in **half the time**.

#### 2. Risk Management

**"The Big Rewrite" Anti-Pattern**:
- Industry research shows 80% of rewrites fail or take 2-3x longer than expected
- Rewrites often miss critical edge cases captured in original code
- Team velocity drops as they rediscover solved problems

**Incremental Refactoring** (Our Approach):
- Preserve working code while improving architecture
- Tests ensure no regressions
- Continuous delivery of value
- Lower risk of catastrophic failure

#### 3. Community Adoption Timeline

**Goal**: Enable community contributions by Phase 2 (Week 12)

**FaultMaven-Mono Evolution Path**:
- ✅ Week 8: API feature parity achieved
- ✅ Week 12: Community-ready packaging (SQLite, zero dependencies)
- Week 20: Full modular architecture

**faultmaven Rebuild Path**:
- Week 12: Still missing enterprise features
- Week 24: Feature parity (maybe)
- Week 40+: Community-ready (optimistic)

**Analysis**: Evolving FaultMaven-Mono gets the community involved **28 weeks earlier**.

---

## Implementation Strategy

### Phase 1: API Feature Parity (Weeks 1-8) ✅ COMPLETE

**Goal**: Match faultmaven's 15 CRITICAL endpoints with deployment-neutral implementation.

**Deliverables**:
- ✅ 15 CRITICAL endpoints implemented (cases, sessions, query, data, knowledge)
- ✅ In-memory storage adapters (zero external dependencies)
- ✅ Deployment neutrality (works in any environment)
- ✅ 409+ tests (376 baseline + 33 new)
- ✅ 90%+ test coverage maintained
- ✅ 6 PRs merged (#27, #28, #29, #30, #31, #32)

**Outcome**: Core functionality restored, migration unblocked.

---

### Phase 2: Stabilization (Weeks 9-12) ✅ COMPLETE

**Goal**: Community-ready packaging with graceful degradation.

**Deliverables**:
- ✅ Graceful degradation shims (Opik, Presidio, Prometheus) - PR #33
- ✅ Modern packaging (`pyproject.toml`) with community/enterprise split - PR #34
- ✅ Installation guide (466 lines)
- ✅ Quick start guide (5-minute onboarding)
- ✅ ADR documentation (this document)
- ✅ Repository renamed: `FaultMaven-Mono` → `faultmaven`

**Outcome**: Community can contribute, onboarding time reduced from 1-2 weeks to <5 minutes.

---

### Phase 3: Architectural Refactoring (Weeks 13-20) 🚧 PLANNED

**Goal**: Adopt modular monolith architecture from faultmaven.

**Strategy**: "The Shuffle" - Move files without rewriting logic.

**Deliverables**:
- Module structure (`auth/`, `session/`, `case/`, `knowledge/`, `evidence/`, `agent/`, `api-gateway/`)
- Interface-based design (`ILLMProvider`, `ISessionStore`, `IVectorStore`, etc.)
- Dependency injection container
- Clean architecture layers (API → Service → Core → Infrastructure)
- Documentation site (MkDocs or Docusaurus)
- Architecture compliance testing

**Outcome**: Modern, maintainable architecture with production features preserved.

---

### Phase 4: Community Growth (Weeks 21+) 🚧 PLANNED

**Goal**: Build contributor community and ecosystem.

**Deliverables**:
- Public documentation site
- Community onboarding guides
- Contributor recognition program
- Plugin/extension system
- Integration marketplace

**Outcome**: Thriving open-source community.

---

## Consequences

### Positive Consequences

1. **Faster Time to Market**
   - 20 weeks vs. 40+ weeks
   - Community involvement 28 weeks earlier
   - Production-ready platform delivered sooner

2. **Lower Risk**
   - No "big rewrite" risk
   - Tests ensure no regressions
   - Continuous value delivery

3. **Enterprise Features Preserved**
   - 7 LLM providers
   - PII redaction (Presidio)
   - Observability (Opik, Prometheus)
   - Battle-tested code

4. **Test Safety Net**
   - 1,425 tests protect against regressions
   - 71% coverage maintained
   - Edge cases preserved

5. **Knowledge Retention**
   - Production insights encoded in tests
   - No rediscovery of solved problems
   - Team velocity maintained

### Negative Consequences

1. **Technical Debt Carried Forward**
   - Monolithic coupling must be addressed incrementally
   - Some legacy patterns will persist during transition
   - Cleanup work required in Phase 3

2. **Team Learning Curve**
   - Must learn modular monolith patterns
   - Disciplined refactoring required
   - Code review rigor increases

3. **Temporary Architecture Inconsistency**
   - Mixed patterns during Phases 2-3
   - Documentation must stay current
   - New contributors may be confused

### Mitigation Strategies

1. **Import Linter**: Enforce module boundaries during refactoring
2. **Architecture Tests**: Detect violations of clean architecture
3. **Living Documentation**: Keep architecture docs synced with code
4. **Incremental Cleanup**: Refactor one module at a time
5. **Code Review Focus**: Ensure new code follows target patterns

---

## Alternatives Considered

### Alternative 1: Rebuild on faultmaven (Modular)

**Pros**:
- Clean slate, modern architecture
- No legacy technical debt
- Interface-based from day one

**Cons**:
- 40+ weeks to feature parity
- 80% risk of rewrite failure
- Lose 1,425 tests (years of knowledge)
- Community adoption delayed 28 weeks
- Must rebuild enterprise features

**Rejected Because**: Time and risk too high.

---

### Alternative 2: Run Both in Parallel

**Pros**:
- No commitment required upfront
- Learn from both approaches
- Migrate users gradually

**Cons**:
- Double maintenance burden
- Team split between codebases
- Confusion for community
- Slow progress on both
- No clear end state

**Rejected Because**: Unsustainable resource drain.

---

### Alternative 3: Hybrid Approach (Extract Services)

**Pros**:
- Extract modules as microservices
- Incremental decoupling
- Preserve working code

**Cons**:
- Operational complexity (orchestration, networking)
- Distributed system debugging challenges
- Deployment complexity increases
- Not aligned with modular monolith goal

**Rejected Because**: Adds complexity without architectural benefit.

---

## Success Metrics

### Phase 2 Metrics (Community Adoption) ✅ ACHIEVED

- ✅ Onboarding time: <5 minutes (down from 1-2 weeks)
- ✅ Zero external dependencies: SQLite, local files, FakeRedis sessions
- ✅ Installation guide published: 466 lines
- ✅ Quick start guide published
- ✅ Community/enterprise packaging split working

### Phase 3 Metrics (Architecture Refactoring) 🎯 TARGET

- 🎯 Module structure adopted (6 modules)
- 🎯 Interface compliance: 90%+ of services use interfaces
- 🎯 Test coverage maintained: 71%+
- 🎯 Import linter passing: Zero cross-module violations
- 🎯 Documentation site live

### Phase 4 Metrics (Community Growth) 🎯 TARGET

- 🎯 Contributors: 10+ active contributors
- 🎯 GitHub stars: 500+
- 🎯 Community PRs: 20+ merged
- 🎯 Plugins/extensions: 5+ community-built

---

## References

- **[FaultMaven Platform Evolution Strategy](../../FAULTMAVEN_PLATFORM_EVOLUTION_STRATEGY.md)** - Complete 20-week roadmap
- **[Installation Guide](../../getting-started/installation.md)** - Standalone and Cloud installation
- **[Quick Start Guide](../../QUICKSTART.md)** - 5-minute onboarding
- **[Architecture Overview](../architecture-overview.md)** - System architecture documentation

---

## Review and Updates

This ADR should be reviewed at the end of each phase:

- **Phase 2 Review** (Week 12): ✅ COMPLETE - 2026-01-01
- **Phase 3 Review** (Week 20): 🚧 PLANNED
- **Phase 4 Review** (Week 32): 🚧 PLANNED

---

**Signed Off By**:
- Engineering Leadership: ✅ Approved
- Product Team: ✅ Approved
- Community Team: ✅ Approved

**Last Updated**: 2026-01-01 (Phase 2 completion)
