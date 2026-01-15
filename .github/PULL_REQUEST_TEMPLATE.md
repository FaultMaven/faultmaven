<!--
INSTRUCTIONS:
1. Fill out the "Description" and "Type of Change" sections (required)
2. DELETE any sections that don't apply to your PR
3. Expand collapsible sections only if relevant to your change
4. For small bug fixes, you can delete everything except Description and Type of Change
-->

## Description
<!-- Provide a clear and concise description of what this PR does -->

## Related Issue
<!-- Link to the issue this PR addresses (e.g., Fixes #123, Closes #456) -->

## Type of Change
<!-- Check all that apply - this helps reviewers know what to focus on -->
- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Database schema change (requires special review - expand "Database Safety" section below)
- [ ] Refactor / Performance improvement
- [ ] Documentation update

---

<details>
<summary><b>🏛️ Monolith Architectural Safeguards</b> (expand if adding features or refactoring)</summary>

### Module Boundaries
- [ ] I have respected domain boundaries (no direct imports between decoupled modules)
- [ ] Used public Interfaces/Services from `faultmaven.models.interfaces`
- [ ] Dependencies flow inward (infrastructure → domain, never reverse)

### Dependency Management
- [ ] No new heavy dependencies added without team discussion
- [ ] Dependencies scoped appropriately (core vs. enterprise extras)

</details>

<details>
<summary><b>🗄️ Database Safety</b> (expand if database schema changed)</summary>

### Migration Safety
- [ ] Migrations are **non-locking** and backwards compatible
- [ ] Safe to run while old code is live (no column drops, renames handled in phases)
- [ ] Tested rollback scenario

### Query Performance
- [ ] No "N+1" queries introduced (used `selectinload`/`joinedload` where needed)
- [ ] Added indexes for new query patterns
- [ ] Verified impact on existing queries

</details>

<details>
<summary><b>🧪 Testing</b> (expand if new code added)</summary>

- [ ] New code has unit tests (maintain 40%+ coverage)
- [ ] Critical paths have integration tests
- [ ] Edge cases and error paths tested
- [ ] Tests follow [Testing Standards](../docs/standards/TESTING_STANDARDS.md)

</details>

<details>
<summary><b>🚀 CI/CD & Deployment</b> (expand if touching infrastructure)</summary>

- [ ] This PR does **not** introduce direct Kubernetes deploy logic (no `kubectl`, no `helm` in `/faultmaven`)
- [ ] Deployment config changes handled separately in `faultmaven-enterprise-infra` or `fm-charts`
- [ ] Dockerfile changes are minimal and necessary
- [ ] No secrets or credentials in Docker image layers

</details>

<details>
<summary><b>🔒 Security</b> (expand if touching auth, APIs, or user input)</summary>

- [ ] No sensitive data (API keys, passwords, tokens) in code
- [ ] Input validation added for user-facing endpoints
- [ ] SQL injection prevention verified (using ORM, parameterized queries)
- [ ] XSS prevention verified (proper output encoding)
- [ ] Authentication/authorization checks in place
- [ ] Rate limiting considered for new endpoints

</details>

---

## How Has This Been Tested?
<!-- Describe the tests you ran to verify your changes -->
- [ ] Unit tests pass locally
- [ ] Integration tests pass locally (if applicable)
- [ ] Manual testing performed (describe key scenarios)

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
- [ ] Code review approved
- [ ] Documentation updated (if user-facing changes)
- [ ] Ready to merge
