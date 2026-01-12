# Knowledge Curator Review Guidelines

As a Knowledge Curator, you are the guardian of quality for the FaultMaven Knowledge Base. Your reviews ensure that every runbook is accurate, safe, and valuable to the community.

---

## Your Role

**Responsibilities:**
- Review all runbook pull requests for quality and accuracy
- Verify technical correctness through testing
- Ensure security best practices are followed
- Provide constructive feedback to contributors
- Maintain consistency across the knowledge base
- Approve or reject submissions with clear reasoning

**Authority:**
- Only approved Knowledge Curators can merge runbook PRs
- You have the final say on technical accuracy
- You can request changes, additional testing, or clarification
- You can escalate complex reviews to senior curators

**Time Commitment:**
- Target: Complete review within 3 business days
- Complex reviews: May take up to 5 business days
- Urgent/critical runbooks: Same-day review when possible

---

## Review Process

### Step 1: Initial Assessment (5 minutes)

**Check automated validation:**
- [ ] GitHub Actions checks passed
- [ ] No YAML syntax errors
- [ ] All required sections present
- [ ] Markdown formatting valid

**Quick scan:**
- [ ] Runbook ID is unique
- [ ] Title format is correct
- [ ] Technology tag matches directory
- [ ] Contributor checklist is complete

**Decision point:**
- ✅ Looks good → Proceed to detailed review
- ⚠️ Issues found → Request changes immediately
- ❌ Major problems → Reject with feedback

### Step 2: Technical Accuracy Review (15-30 minutes)

**Test all commands:**

1. **Set up test environment:**
   ```bash
   # Create isolated test environment
   # Use staging/development cluster, not production!
   ```

2. **Run each diagnostic command:**
   - Verify command syntax is correct
   - Check that command produces expected output
   - Confirm command is safe to run
   - Test with different scenarios if applicable

3. **Test all solutions:**
   - Follow solution steps exactly as written
   - Verify solution actually resolves the problem
   - Check for any undocumented prerequisites
   - Confirm time estimates are realistic

**Review checklist:**
- [ ] All commands have correct syntax
- [ ] Expected outputs match reality
- [ ] Solutions actually solve the stated problem
- [ ] No broken or incorrect commands
- [ ] Time estimates are realistic
- [ ] Prerequisites are complete and accurate

### Step 3: Safety & Security Review (10 minutes)

**Check for dangerous operations:**
- [ ] No `rm -rf` or destructive commands without warnings
- [ ] No `--force` flags without explanation
- [ ] No production modifications without safeguards
- [ ] Backup steps before destructive changes

**Verify security practices:**
- [ ] No hardcoded credentials
- [ ] No API keys or tokens in examples
- [ ] Secrets properly masked (e.g., `<your-password>`)
- [ ] Follows principle of least privilege
- [ ] No security vulnerabilities introduced

**Check sensitive operations:**
```bash
# Bad: No warning for destructive operation
kubectl delete namespace production

# Good: Clear warning and confirmation
# WARNING: This will delete all resources in the namespace!
# Make sure you have backups before proceeding.
kubectl delete namespace <namespace> --dry-run=client
# Review the output, then remove --dry-run if sure
```

**Security checklist:**
- [ ] No credentials exposed
- [ ] Dangerous operations clearly marked
- [ ] Backup steps included for destructive changes
- [ ] Commands follow security best practices
- [ ] No unnecessary privileges required

### Step 4: Quality & Completeness Review (10 minutes)

**Content quality:**
- [ ] Quick Reference Card is concise and accurate
- [ ] Symptoms are observable and specific
- [ ] Common causes are ranked by likelihood
- [ ] Diagnostic steps are logical and complete
- [ ] Solutions are detailed and actionable
- [ ] Root cause analysis explains the "why"
- [ ] Prevention section has tactical + strategic advice

**Writing quality:**
- [ ] Language is clear and professional
- [ ] No grammar or spelling errors
- [ ] Technical terms are explained when first used
- [ ] Appropriate for stated difficulty level
- [ ] Code blocks use proper language tags
- [ ] Commands have explanatory comments

**Metadata quality:**
- [ ] All frontmatter fields complete
- [ ] Tags are relevant and useful
- [ ] Severity matches problem impact
- [ ] Difficulty matches complexity
- [ ] Status is appropriate (draft vs verified)

**Completeness checklist:**
- [ ] All required sections present
- [ ] Minimum 2 diagnostic steps
- [ ] Minimum 1 complete solution
- [ ] Prevention strategies included
- [ ] Related runbooks linked
- [ ] Version history started

### Step 5: Consistency & Integration Review (5 minutes)

**Check for duplicates:**
```bash
# Search for similar runbooks
grep -r "similar problem" docs/runbooks/

# Check existing runbooks in same technology
ls docs/runbooks/kubernetes/
```

- [ ] No duplicate runbook IDs
- [ ] Not covering exact same problem as existing runbook
- [ ] If similar, provides unique value or different approach

**Verify cross-references:**
- [ ] Related runbooks actually exist
- [ ] Links are correctly formatted
- [ ] External links are valid and authoritative
- [ ] References make sense

**Check integration:**
- [ ] Category README updated if needed
- [ ] Main README updated if needed
- [ ] Follows established patterns in category

---

## Approval Decision Matrix

### ✅ Approve Immediately

**Criteria:**
- All automated checks pass
- All commands tested and work correctly
- No security issues
- High quality writing
- Complete metadata
- No major issues found

**Action:**
- Add comment: "Reviewed and approved. Excellent work!"
- Merge the PR
- Monitor ingestion process

### ⚠️ Request Changes

**Criteria:**
- Minor technical inaccuracies
- Missing information
- Commands need clarification
- Security concerns (fixable)
- Writing needs improvement
- Metadata incomplete

**Action:**
- Use "Request Changes" on GitHub
- Provide specific, actionable feedback
- Tag specific lines with comments
- Example: "Please add expected output for this command"

**Feedback template:**
```markdown
Thank you for your contribution! I've reviewed your runbook and found a few items that need attention:

**Technical Issues:**
- [ ] Line 45: This command is missing the namespace flag
- [ ] Line 67: Expected output doesn't match what I see in testing

**Security Concerns:**
- [ ] Line 89: Please add a warning before this destructive operation

**Completeness:**
- [ ] Prevention section needs long-term strategies

Once these are addressed, I'll be happy to approve!
```

### ❌ Reject

**Criteria:**
- Fundamentally incorrect information
- Dangerous commands without safeguards
- Duplicates existing runbook exactly
- Not following template structure
- Not tested by contributor
- Major security vulnerabilities

**Action:**
- Close the PR with explanation
- Provide clear reasoning
- Offer to help contributor improve for resubmission

**Rejection template:**
```markdown
Thank you for your contribution. Unfortunately, I cannot approve this runbook in its current form for the following reasons:

**Critical Issues:**
1. [Specific issue]
2. [Specific issue]

**Recommendation:**
[Suggest how to fix or alternative approach]

If you'd like to revise and resubmit, please:
1. [Action item]
2. [Action item]

Feel free to reach out if you have questions!
```

---

## Common Review Scenarios

### Scenario 1: Commands Don't Work

**Problem:** Contributor's commands fail in your test environment

**Actions:**
1. Verify you're testing in correct environment
2. Check for undocumented prerequisites
3. Request clarification from contributor
4. Suggest corrections if you know the fix

**Example feedback:**
```markdown
I tested the commands in Step 2 and got this error:
```bash
Error: the namespace "production" does not exist
```

Could you update the instructions to include creating the namespace first, or specify that this assumes production namespace exists?
```

### Scenario 2: Security Risk

**Problem:** Runbook includes risky operations without safeguards

**Actions:**
1. Request changes immediately
2. Explain the security risk
3. Suggest safer alternatives
4. Don't merge until resolved

**Example feedback:**
```markdown
**Security Concern:**

Line 78 includes `kubectl delete namespace production` without any warnings or safeguards. This is extremely dangerous.

Please add:
1. A clear warning box before the command
2. A --dry-run step to preview
3. Backup instructions
4. Confirmation prompt

Example:
```
⚠️ **WARNING**: This will permanently delete all resources in the namespace!

1. Backup first:
   kubectl get all -n production -o yaml > production-backup.yaml

2. Preview what will be deleted:
   kubectl delete namespace production --dry-run=client

3. If you're absolutely sure, remove --dry-run
```
```

### Scenario 3: Incomplete Information

**Problem:** Runbook is missing key information

**Actions:**
1. List what's missing
2. Explain why it's important
3. Request additions
4. Offer examples if helpful

**Example feedback:**
```markdown
The runbook is well-written but missing some key information:

**Prevention Section:**
- Currently only has tactical prevention
- Please add 2-3 long-term strategic prevention strategies
- Example: "Implement monitoring alerts for pod restarts exceeding threshold"

**Expected Outputs:**
- Diagnostic Step 2 shows a command but no expected output
- Please add what users should see if problem exists
```

### Scenario 4: Duplicate Content

**Problem:** Runbook covers same ground as existing runbook

**Actions:**
1. Link to existing runbook
2. Explain the duplication
3. Suggest consolidation or differentiation
4. Close or request significant changes

**Example feedback:**
```markdown
This runbook is very similar to our existing [Kubernetes Pod CrashLoopBackOff](kubernetes/k8s-pod-crashloopbackoff.md) runbook.

**Options:**
1. **Enhance the existing runbook** - If you have additional insights, consider submitting a PR to improve the existing runbook instead
2. **Differentiate your approach** - If your runbook solves the problem differently or covers a specific variant, please update the title and introduction to make this clear
3. **Merge the content** - We can work together to merge the best parts of both

Which approach would you prefer?
```

---

## Best Practices for Reviewers

### Provide Constructive Feedback

**Good feedback:**
- Specific and actionable
- Points to exact lines
- Explains "why" not just "what"
- Offers examples or alternatives
- Maintains respectful tone

**Example:**
```markdown
Line 45: The kubectl command is missing the `-n` flag for namespace.

This will fail if the user's current context isn't set to the production namespace.

Suggested fix:
```bash
kubectl get pods -n production | grep CrashLoop
```
```

**Poor feedback:**
- "This is wrong"
- "Fix the commands"
- "Doesn't work"

### Test Thoroughly But Efficiently

**Do:**
- Test in safe, isolated environment
- Run all diagnostic commands
- Verify at least the primary solution
- Check for obvious errors

**Don't:**
- Test every possible edge case
- Test in production
- Spend hours on a simple runbook
- Block contributor while you over-test

**Balance:** Trust contributor did basic testing, but verify critical paths.

### Communicate Timelines

**If you can't review within 3 days:**
- Comment on PR with expected timeline
- Tag another curator if urgent
- Don't leave contributors hanging

**Example:**
```markdown
Thanks for the contribution! I'm currently handling several reviews and won't be able to get to this until next week. I've tagged @curator-alice who may be able to review sooner.
```

### Escalate When Needed

**Escalate to senior curators if:**
- Technical accuracy is unclear
- Security implications are complex
- Contributor disputes your feedback
- You're unsure about approval decision

---

## Tools and Resources

### Testing Environments

**Required:**
- Access to Kubernetes test cluster
- Redis test instance
- PostgreSQL test database
- Docker environment

**Recommended:**
- Staging environment mirroring production
- Automated testing scripts
- Command validation tools

### Review Tools

**GitHub:**
- Use "Request changes" for required fixes
- Use "Comment" for suggestions
- Use "Approve" only when ready to merge

**Command validation:**
```bash
# Validate Kubernetes YAML
kubectl apply -f runbook-example.yaml --dry-run=server

# Test command syntax
bash -n command-script.sh

# Validate Markdown
npx markdownlint runbook.md
```

---

## Reviewer Checklist

Use this for every review:

### Pre-Review
- [ ] Automated checks passed
- [ ] Contributor checklist complete
- [ ] Initial scan shows no obvious issues

### Technical Review
- [ ] All commands tested in safe environment
- [ ] Expected outputs match reality
- [ ] Solutions actually solve the problem
- [ ] Prerequisites are accurate and complete

### Security Review
- [ ] No credentials exposed
- [ ] Dangerous operations have warnings
- [ ] Follows security best practices
- [ ] No unnecessary privileges required

### Quality Review
- [ ] All required sections present
- [ ] Writing is clear and professional
- [ ] Metadata is complete and accurate
- [ ] Appropriate for difficulty level

### Integration Review
- [ ] No duplicate runbooks
- [ ] Related runbooks linked correctly
- [ ] Fits well in knowledge base structure

### Final Decision
- [ ] Approve / Request Changes / Reject
- [ ] Feedback is specific and constructive
- [ ] Timeline communicated if delayed

---

## Getting Help as a Reviewer

**Questions about a review?**
- Ask in #knowledge-curators channel
- Tag @senior-curators
- Email: curators@faultmaven.io

**Escalate complex reviews:**
- Security concerns → Tag @security-team
- Technical disputes → Tag @senior-curators
- Policy questions → Tag @knowledge-lead

---

## Curator Statistics

Track your review performance:
- Reviews completed per month
- Average review time
- Approval vs change request rate
- Contributor satisfaction

**Goal:** Maintain quality while keeping contributors engaged and motivated.

---

**Thank you for maintaining the quality of the FaultMaven Knowledge Base!**

Your thorough reviews ensure that every runbook helps users solve problems quickly and safely.
