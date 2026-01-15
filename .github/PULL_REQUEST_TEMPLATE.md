## Description
<!-- Provide a clear and concise description of what this PR does -->

## Related Issue
<!-- Link to the issue this PR addresses (e.g., Fixes #123, Closes #456) -->

## Type of Change
- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Database schema change
- [ ] Refactor / Performance improvement
- [ ] Documentation update

## How Has This Been Tested?
<!-- Describe the tests you ran to verify your changes -->
- [ ] Unit tests added/updated
- [ ] Integration tests added/updated
- [ ] Manual testing performed (describe scenarios)
- [ ] Performance/load testing (if applicable)

---

## 🏛️ Monolith Architectural Safeguards

### Module Boundaries
- [ ] **Domain Isolation:** I have strictly respected domain boundaries
  - No direct imports between decoupled modules
  - Used public Interfaces/Service classes from `faultmaven.models.interfaces`
  - Dependencies flow inward (infrastructure → domain, never domain → infrastructure)

### Database Safety
- [ ] **Migration Safety:**
  - [ ] Migrations are **non-locking** and backwards compatible
  - [ ] Safe to run while old code is live (no column drops, renames handled in phases)
  - [ ] Tested rollback scenario

- [ ] **Query Performance:**
  - [ ] No "N+1" queries introduced (used `selectinload`/`joinedload` where needed)
  - [ ] Added indexes for new query patterns
  - [ ] Considered impact on existing queries

### Dependency Management
- [ ] **Dependency Check:**
  - [ ] No new heavy dependencies added without team discussion
  - [ ] Dependencies scoped appropriately (core vs. enterprise extras)
  - [ ] Transitive dependencies reviewed for conflicts

### Testing Standards
- [ ] **Test Coverage:**
  - [ ] New code has unit tests (maintain 40%+ coverage)
  - [ ] Critical paths have integration tests
  - [ ] Edge cases and error paths tested
  - [ ] Tests follow the [Testing Standards](../docs/standards/TESTING_STANDARDS.md)

---

## 🚀 CI/CD Policy Checklist

### Deployment Separation
- [ ] This PR does **not** introduce direct Kubernetes deploy logic
  - No `kubectl` commands in source code
  - No `helm` charts in `/faultmaven` directory

- [ ] Deployment configuration changes handled separately in:
  - `faultmaven-enterprise-infra` repository (Kubernetes manifests)
  - `fm-charts` repository (Helm charts)

### Docker & Containerization
- [ ] Dockerfile changes are minimal and necessary
- [ ] No secrets or credentials in Docker image layers
- [ ] Image size impact considered (if Dockerfile changed)

---

## 📋 Code Quality Checklist

- [ ] My code follows the project's style guidelines (black, isort, flake8)
- [ ] I have performed a self-review of my own code
- [ ] I have commented my code, particularly in hard-to-understand areas
- [ ] I have made corresponding changes to the documentation
- [ ] My changes generate no new warnings
- [ ] I have added tests that prove my fix is effective or that my feature works
- [ ] New and existing unit tests pass locally with my changes
- [ ] Any dependent changes have been merged and published

---

## 🔒 Security Checklist (if applicable)

- [ ] No sensitive data (API keys, passwords, tokens) in code
- [ ] Input validation added for user-facing endpoints
- [ ] SQL injection prevention verified (using ORM, parameterized queries)
- [ ] XSS prevention verified (proper output encoding)
- [ ] Authentication/authorization checks in place
- [ ] Rate limiting considered for new endpoints

---

## 📸 Screenshots / Videos (if applicable)
<!-- Add screenshots or videos demonstrating UI changes or new features -->

---

## 🔗 Additional Context
<!-- Add any other context, decisions made, or alternatives considered -->

---

## 📝 Reviewer Notes
<!-- Highlight specific areas you'd like reviewers to focus on -->

---

## ✅ Pre-Merge Checklist

- [ ] All CI checks passing
- [ ] Code review approved by at least one maintainer
- [ ] Documentation updated (if needed)
- [ ] CHANGELOG.md updated (if user-facing changes)
- [ ] Ready to merge
