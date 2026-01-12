# Contributing to FaultMaven Knowledge Base

Thank you for contributing to the FaultMaven runbook library! Your expertise helps the entire community troubleshoot issues faster and more effectively.

## How to Contribute

### 1. Create Your Runbook

**Start with the template:**
```bash
# Copy the template
cp TEMPLATE.md <category>/<technology>-<problem>.md

# Example:
cp TEMPLATE.md kubernetes/k8s-pod-pending.md
```

**Fill in all sections:**
- Update the YAML frontmatter with accurate metadata
- Describe observable symptoms clearly
- Provide step-by-step diagnostic commands
- Include multiple solution paths when applicable
- Add prevention strategies
- Link related runbooks

**Test everything:**
- Run every command in a real environment
- Verify expected outputs match reality
- Test in conditions similar to where the problem occurs
- Confirm solutions actually resolve the issue

### 2. Submit a Pull Request

**Fork and branch:**
```bash
# Fork the repository on GitHub
# Clone your fork
git clone https://github.com/YOUR_USERNAME/FaultMaven.git
cd FaultMaven

# Create a feature branch
git checkout -b runbook/kubernetes-pod-pending

# Add your runbook
git add docs/runbooks/kubernetes/k8s-pod-pending.md
git commit -m "Add runbook for Kubernetes Pod Pending state"
git push origin runbook/kubernetes-pod-pending
```

**Create the PR:**
- Go to the FaultMaven repository on GitHub
- Click "New Pull Request"
- Select your branch
- Fill out the PR template completely
- Submit for review

### 3. Review Process

**What happens next:**
1. **Automated validation** runs immediately
   - YAML syntax check
   - Required section verification
   - Metadata completeness
   - Markdown formatting

2. **Knowledge Curator review** (within 3 business days)
   - Technical accuracy verification
   - Command testing
   - Security review
   - Quality assessment

3. **Feedback and revisions** (if needed)
   - Curator provides specific feedback
   - You make requested changes
   - Update your PR

4. **Approval and merge**
   - Once approved, runbook is merged
   - Automatically ingested into knowledge base
   - Available to all FaultMaven users

---

## Quality Requirements

Your runbook must meet these standards to be accepted:

### Content Requirements

**Required sections:**
- [ ] Quick Reference Card (symptoms, causes, quick fix)
- [ ] Diagnostic Steps (minimum 2 steps with commands)
- [ ] Solutions (minimum 1 complete solution)
- [ ] Root Cause Analysis (why this happens)
- [ ] Prevention (tactical and strategic)
- [ ] Related Issues (links to related runbooks)

**Metadata requirements:**
- [ ] Unique `id` (no duplicates)
- [ ] Descriptive `title` in format "[Technology] - [Problem]"
- [ ] Correct `technology` tag
- [ ] Appropriate `severity` level
- [ ] At least 3 relevant `tags`
- [ ] Accurate `difficulty` level
- [ ] Current `last_updated` date
- [ ] `verified_by` field filled
- [ ] Correct `status` (draft or verified)

### Command Requirements

**All commands must:**
- [ ] Be tested in a real environment
- [ ] Include expected output examples
- [ ] Have explanatory comments
- [ ] Specify language in code blocks (```bash, ```yaml, etc.)
- [ ] Be safe to run (no destructive commands without warnings)

### Writing Quality

**Your runbook should:**
- [ ] Use clear, concise language
- [ ] Avoid jargon without explanation
- [ ] Be appropriate for the target difficulty level
- [ ] Include specific commands, not general advice
- [ ] Provide exact error messages to match
- [ ] Explain the "why" behind solutions

### Security Considerations

**You must:**
- [ ] Never include real credentials or secrets
- [ ] Warn before any destructive operations
- [ ] Follow principle of least privilege
- [ ] Include security implications in prevention section
- [ ] Use safe command examples

---

## Style Guide

### Naming Conventions

**File names:**
- Use lowercase with hyphens
- Format: `<technology>-<short-description>.md`
- Examples:
  - `k8s-pod-crashloop.md`
  - `redis-connection-refused.md`
  - `postgres-slow-queries.md`

**Runbook IDs:**
- Use lowercase with hyphens or underscores
- Must be unique across all runbooks
- Format: `<technology>-<problem-identifier>`
- Examples:
  - `k8s-pod-crashloopbackoff`
  - `redis-connection-refused`
  - `postgres-connection-pool-exhausted`

### Command Formatting

**Always use code blocks:**
```bash
# Good: Code block with language specified
kubectl get pods -n production
```

**Include explanatory comments:**
```bash
# Check for pods in CrashLoopBackOff state
kubectl get pods -A | grep CrashLoop

# Get detailed information about the failing pod
kubectl describe pod <pod-name> -n <namespace>
```

**Show expected output:**
```bash
kubectl get pods -n production
```
**Output:**
```
NAME                   READY   STATUS             RESTARTS   AGE
app-7d9f8c6b5-4xkz2   0/1     CrashLoopBackOff   5          3m
```

### Difficulty Levels

**Beginner:**
- Common, well-known issues
- Simple diagnostic steps (2-3 commands)
- Single, straightforward solution
- No advanced concepts required
- Example: Redis connection refused

**Intermediate:**
- Moderately complex issues
- Multiple diagnostic steps
- May require understanding of system architecture
- Multiple solution paths
- Example: Kubernetes pod CrashLoopBackOff

**Advanced:**
- Complex, multi-system issues
- Deep diagnostic investigation required
- Requires advanced technical knowledge
- Multiple interdependent solutions
- Example: Database performance tuning under load

### Severity Levels

**Critical:**
- Complete service outage
- Data loss risk
- Security breach
- Requires immediate action

**High:**
- Significant service degradation
- Affecting many users
- Performance severely impacted
- Should be addressed urgently

**Medium:**
- Partial functionality impaired
- Affecting some users
- Noticeable but not severe impact
- Should be addressed soon

**Low:**
- Minor inconvenience
- Few users affected
- Minimal impact
- Can be scheduled for later

---

## Examples of Good Runbooks

Look at these examples for inspiration:

1. **[Kubernetes Pod CrashLoopBackOff](kubernetes/k8s-pod-crashloopbackoff.md)**
   - Clear symptoms and causes
   - Multiple diagnostic steps
   - Three complete solutions
   - Comprehensive prevention section

2. **[Redis Out of Memory](redis/redis-out-of-memory.md)**
   - Excellent quick reference card
   - Detailed memory diagnostics
   - Multiple resolution strategies
   - Strong prevention guidance

---

## Getting Help

**Questions about contributing?**
- Open a [GitHub Discussion](https://github.com/FaultMaven/FaultMaven/discussions)
- Join our community chat
- Email: knowledge-team@faultmaven.io

**Technical issues with your runbook?**
- Create a [GitHub Issue](https://github.com/FaultMaven/FaultMaven/issues)
- Tag with `runbook-help`
- Provide details about what you're stuck on

**Need a reviewer?**
- Tag `@faultmaven-curators` in your PR
- Mention you need review priority in PR description

---

## Recognition

We appreciate your contributions! Contributors will be:
- Listed in the runbook's `verified_by` field
- Credited in release notes
- Recognized in our community hall of fame
- Eligible for contributor badges (coming soon)

---

## Code of Conduct

By contributing, you agree to:
- Be respectful and constructive
- Provide accurate, tested information
- Accept feedback graciously
- Help maintain quality standards
- Follow the FaultMaven Code of Conduct

---

## License

By contributing to this repository, you agree that your contributions will be licensed under the Apache-2.0 License.

All runbooks become part of the FaultMaven Knowledge Base and are available to the community under this license.

---

## Quick Checklist

Before submitting your PR, verify:

- [ ] Used TEMPLATE.md as the base
- [ ] All commands tested in real environment
- [ ] YAML frontmatter complete and accurate
- [ ] All required sections present
- [ ] Expected outputs are accurate
- [ ] Security considerations addressed
- [ ] Related runbooks linked
- [ ] Prevention strategies included
- [ ] Code blocks have language specified
- [ ] No credentials or secrets included
- [ ] PR template filled out completely

---

**Thank you for making FaultMaven better for everyone!**
