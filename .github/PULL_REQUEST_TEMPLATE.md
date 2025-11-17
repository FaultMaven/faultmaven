## Description

<!-- Provide a clear and concise description of your changes -->

## Type of Change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature (non-breaking change that adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Refactoring (no functional changes)
- [ ] Performance improvement
- [ ] Other (please describe):

## Related Issues

<!-- Link to related issues using #issue_number -->

Fixes #
Closes #
Related to #

## Changes Made

<!-- List the specific changes you've made -->

-
-
-

## Testing

**How has this been tested?**

- [ ] Unit tests
- [ ] Integration tests
- [ ] Manual testing
- [ ] End-to-end testing

**Test Configuration:**
- Docker version:
- Docker Compose version:
- OS:
- LLM Provider (if relevant):

**Test Steps:**

1.
2.
3.

## Checklist

**Code Quality:**
- [ ] My code follows the project's style guidelines
- [ ] I have performed a self-review of my code
- [ ] I have commented my code, particularly in hard-to-understand areas
- [ ] My changes generate no new warnings or errors

**Testing:**
- [ ] I have added tests that prove my fix is effective or that my feature works
- [ ] New and existing unit tests pass locally with my changes
- [ ] I have tested this with `docker-compose up` from `faultmaven-deploy`

**Documentation:**
- [ ] I have updated the documentation accordingly
- [ ] I have updated the README (if needed)
- [ ] I have added/updated code comments

**Public vs Enterprise:**
- [ ] This change is appropriate for the **public open-source version**
- [ ] This does NOT introduce dependencies on:
  - [ ] Multi-tenancy (organizations/teams)
  - [ ] RBAC/advanced permissions
  - [ ] PostgreSQL (SQLite only for public)
  - [ ] Supabase (JWT only for public)
  - [ ] Enterprise-specific features

**Dependencies:**
- [ ] I have not added new dependencies (or I have justified them below)
- [ ] All new dependencies are compatible with Apache 2.0 license

**Security:**
- [ ] My changes do not introduce security vulnerabilities
- [ ] I have not committed secrets or API keys
- [ ] I have validated all user inputs
- [ ] I have not exposed sensitive information in logs

## Breaking Changes

<!-- If this is a breaking change, describe the impact and migration path -->

**Impact:**

**Migration Instructions:**

## Screenshots (if applicable)

<!-- Add screenshots to demonstrate UI changes -->

## Additional Notes

<!-- Any additional information reviewers should know -->

## Reviewer Checklist (for maintainers)

- [ ] Code review completed
- [ ] Architecture aligns with public/private separation
- [ ] No enterprise features leaked into public code
- [ ] Tests pass
- [ ] Documentation updated
- [ ] Ready to merge
