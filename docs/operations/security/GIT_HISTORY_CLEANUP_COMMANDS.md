# Git History Cleanup Commands - RSA Key Exposure

**Incident**: INCIDENT-2026-01-23-001
**Date**: 2026-01-23
**Purpose**: Remove exposed RSA private key from git history

---

## ⚠️ CRITICAL WARNING

**This operation rewrites git history and affects all team members.**

Before proceeding:
1. Notify all team members
2. Ensure all pending PRs are merged or noted
3. Backup the repository
4. Coordinate timing with the team
5. Plan for team members to re-clone or reset their repos

---

## Option 1: Using git-filter-repo (Recommended)

### Prerequisites
```bash
# Install git-filter-repo
pip3 install git-filter-repo

# OR
brew install git-filter-repo  # macOS
```

### Commands

```bash
cd /home/swhouse/product/faultmaven

# 1. Backup current state
git branch backup-before-cleanup-$(date +%Y%m%d-%H%M%S)

# 2. Create replace expressions file
cat > /tmp/key-cleanup.txt <<'EOF'
# Remove hardcoded RSA private key (replace with dynamic generation)
regex:TEST_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----.*?-----END RSA PRIVATE KEY-----"""==>TEST_PRIVATE_KEY, TEST_PUBLIC_KEY = _generate_test_rsa_keypair()

# Remove public key constant (already replaced by dynamic generation)
regex:TEST_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----"""==>
EOF

# 3. Run filter-repo
git filter-repo --replace-text /tmp/key-cleanup.txt --force

# 4. Clean up
rm /tmp/key-cleanup.txt

# 5. Verify
git log --all -S "MIIEpAIBAAKCAQEAr0+UcafV6BzHPkDETQ11"
# Should return no results

# 6. Force push (COORDINATE WITH TEAM FIRST!)
git push origin --force --all
git push origin --force --tags
```

---

## Option 2: Using BFG Repo-Cleaner (Alternative)

### Prerequisites
```bash
# Download BFG
wget https://repo1.maven.org/maven2/com/madgag/bfg/1.14.0/bfg-1.14.0.jar
alias bfg="java -jar bfg-1.14.0.jar"
```

### Commands

```bash
cd /home/swhouse/product/faultmaven

# 1. Backup
git branch backup-before-cleanup-$(date +%Y%m%d-%H%M%S)

# 2. Clone a fresh copy (BFG works on bare repos)
cd ..
git clone --mirror /home/swhouse/product/faultmaven faultmaven-mirror.git

# 3. Run BFG to remove the exposed key pattern
bfg --replace-text key-patterns.txt faultmaven-mirror.git

# Where key-patterns.txt contains:
# MIIEpAIBAAKCAQEAr0+UcafV6BzHPkDETQ11==>***REMOVED***

# 4. Clean up the repository
cd faultmaven-mirror.git
git reflog expire --expire=now --all
git gc --prune=now --aggressive

# 5. Push changes
git push

# 6. Update your working copy
cd /home/swhouse/product/faultmaven
git fetch origin
git reset --hard origin/main
```

---

## Option 3: Using Automated Script

We've provided a script that handles the cleanup:

```bash
cd /home/swhouse/product/faultmaven

# Dry run first (preview changes)
./scripts/security/cleanup_exposed_keys_from_history.sh --dry-run

# Execute cleanup
./scripts/security/cleanup_exposed_keys_from_history.sh
```

---

## Team Notification Template

Send this to your team BEFORE running the cleanup:

```
Subject: URGENT: Git History Rewrite Required - RSA Key Exposure

Team,

We've detected an exposed RSA private key in our git history (test key only, not production).
We need to rewrite git history to remove it permanently.

IMPACT:
- Git history will be rewritten
- All team members must re-sync their local repositories
- Timing: [INSERT DATE/TIME]

WHAT YOU NEED TO DO:

1. Commit and push all your work BEFORE [INSERT TIME]
2. After the cleanup (you'll receive confirmation), run:

   cd /home/swhouse/product/faultmaven
   git fetch origin
   git reset --hard origin/main  # WARNING: This discards local changes

3. If you have uncommitted work, stash it first:

   git stash
   git fetch origin
   git reset --hard origin/main
   git stash pop  # May have conflicts

BRANCHES AND PRs:
- Open PRs will need to be rebased
- Feature branches will need to be rebased on new main

TIMING:
- Cleanup window: [INSERT TIME RANGE]
- Expected duration: 15-30 minutes
- No pushes during this window

Questions? Contact DevOps team.

Incident Reference: INCIDENT-2026-01-23-001
```

---

## Post-Cleanup Verification

After cleanup, verify the key is removed:

```bash
# Search for the exposed key pattern in all history
git log --all --pretty=format: --name-only --diff-filter=D | \
    xargs git grep -l "MIIEpAIBAAKCAQEAr0+UcafV6BzHPkDETQ11"

# Should return no results

# Search in all commits
git log --all -S "BEGIN RSA PRIVATE KEY" --source --all

# Check specific file history
git log --all --full-history -- tests/unit/modules/auth/domain/services/test_jwt_token_generator.py

# Verify current version uses dynamic generation
git show HEAD:tests/unit/modules/auth/domain/services/test_jwt_token_generator.py | grep "_generate_test_rsa_keypair"
# Should show the function call
```

---

## Rollback Procedure (If Needed)

If something goes wrong, you can rollback using the backup branch:

```bash
# List backup branches
git branch | grep backup-before-cleanup

# Restore from backup
git checkout backup-before-cleanup-[TIMESTAMP]
git branch -D main
git checkout -b main
git push origin main --force
```

---

## Alternative: Squash and Start Fresh (Nuclear Option)

If git-filter-repo doesn't work or the history is too complex:

```bash
# 1. Create new orphan branch
git checkout --orphan new-main

# 2. Add all files (with fixes applied)
git add .

# 3. Create initial commit
git commit -m "Initial commit (history reset due to security incident)"

# 4. Delete old main
git branch -D main

# 5. Rename new-main to main
git branch -m main

# 6. Force push
git push origin main --force
```

**Pros**: Complete clean slate
**Cons**: Loses all commit history, blame info, and commit messages

---

## GitHub/GitLab Considerations

### GitHub
- Cached commits may still be accessible via API for ~90 days
- Contact GitHub support to purge cached commits: https://support.github.com
- Consider making the repo private temporarily

### GitLab
- GitLab caches may retain old commits
- Use GitLab's repository cleanup feature
- Contact GitLab support if needed

### BitBucket
- Use Bitbucket's repository cleanup feature
- Old commits may be cached

---

## Files Modified in This Remediation

```
✅ Modified:
- tests/unit/modules/auth/domain/services/test_jwt_token_generator.py
  (Replaced hardcoded key with dynamic generation)

- .gitignore
  (Added patterns for *.pem, *.key, etc.)

- .pre-commit-config.yaml
  (Added RSA private key detection hook)

✅ Created:
- docs/operations/security/INCIDENT-2026-01-23-RSA-KEY-EXPOSURE.md
  (Incident report)

- docs/operations/security/key-management.md
  (Key management best practices)

- scripts/security/cleanup_exposed_keys_from_history.sh
  (Automated cleanup script)

- docs/operations/security/GIT_HISTORY_CLEANUP_COMMANDS.md
  (This file)
```

---

## Timeline

| Time (UTC) | Action | Status |
|------------|--------|--------|
| 2026-01-23 00:18:01 | Key exposed in commit | ❌ Incident |
| 2026-01-23 (current) | Investigation complete | ✅ Complete |
| 2026-01-23 (current) | Test file fixed (dynamic keys) | ✅ Complete |
| 2026-01-23 (current) | .gitignore updated | ✅ Complete |
| 2026-01-23 (current) | Pre-commit hooks added | ✅ Complete |
| 2026-01-23 (current) | Documentation created | ✅ Complete |
| 2026-01-23 (pending) | Git history cleanup | ⏳ Pending team coordination |
| 2026-01-23 (pending) | Force push to remote | ⏳ Pending team coordination |
| 2026-01-23 (pending) | Team re-sync | ⏳ Pending team coordination |

---

## Support

- **DevOps Team**: devops@faultmaven.ai
- **Security Team**: security@faultmaven.ai
- **Incident Lead**: [Your Name]

---

## References

- [git-filter-repo documentation](https://github.com/newren/git-filter-repo)
- [BFG Repo-Cleaner](https://rtyley.github.io/bfg-repo-cleaner/)
- [GitHub: Removing sensitive data](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
- [Incident Report](./INCIDENT-2026-01-23-RSA-KEY-EXPOSURE.md)
- [Key Management Guide](./key-management.md)
