# API Key Security Guide

**Created:** 2026-01-13
**Status:** CRITICAL - IMMEDIATE ACTION REQUIRED

---

## 🚨 What Happened

An OpenAI API key was exposed in git commit history when a working document containing real API keys was committed to the repository. Even after making the repository private and deleting the file, the key remained in git history.

**Affected Key:** `sk-proj-ZfyDZoUA...` (now disabled by OpenAI)
**Exposure Vector:** Git commit history in `docs/_archive/working/PRE-BETA-RELEASE-AUDIT-2026-01-11.md`
**Resolution:** File deleted in commit `c50656e8`, key disabled by OpenAI

---

## ✅ Immediate Actions Taken

1. **Sanitized Local Files**
   - Removed real API keys from `.env` file
   - Replaced with placeholder: `sk-proj-YOUR-KEY-HERE`

2. **Verified .gitignore**
   - `.env` is properly gitignored
   - `docs/_archive/` is gitignored
   - `docs/working/` is gitignored

3. **Git History**
   - Offending file already deleted from current state
   - Key remains in historical commits (repo is private)

---

## 🔐 Security Best Practices

### NEVER Commit These Files

```bash
# Files that MUST NEVER be committed:
.env                    # Contains real API keys
.env.local              # Local environment overrides
.env.*.local            # Any environment-specific secrets
**/*secret*             # Any file with "secret" in name
**/*key*                # Any file with "key" in name
**/API_KEYS.txt         # Obvious key storage
**/credentials.*        # Credential files
```

### Safe to Commit

```bash
# Files that ARE safe to commit:
.env.example            # Template with placeholders
.env.template           # Alternative template name
docs/**/*.md            # Documentation (check for keys first!)
```

### Before Committing ANY File

**ALWAYS run these checks:**

```bash
# 1. Search for API key patterns
grep -r "sk-proj\|sk-pro\|fw_\|AIza\|gsk_\|hf_\|sk-or-v1" <file>

# 2. Search for common secret patterns
grep -r "api[_-]key\|secret\|password\|token" <file> -i

# 3. Check git status before committing
git status
git diff --staged

# 4. Review EVERY line of EVERY file being committed
git diff --staged <file>
```

---

## 🛡️ Prevention Strategies

### 1. Use Git Pre-Commit Hooks

Install `pre-commit` framework to automatically scan for secrets:

```bash
# Install pre-commit
pip install pre-commit

# Create .pre-commit-config.yaml
cat > .pre-commit-config.yaml <<EOF
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
        exclude: package.lock.json
EOF

# Initialize
pre-commit install
pre-commit run --all-files
```

### 2. Use Environment Variable Injection

**For local development:**
```bash
# Set environment variables in shell (not in files)
export OPENAI_API_KEY="sk-proj-..."
./faultmaven.sh start
```

**For Docker:**
```bash
# Pass via docker-compose env_file
docker compose --env-file .env.local up
```

**For Production:**
```bash
# Use secrets management (Kubernetes Secrets, AWS Secrets Manager, etc.)
kubectl create secret generic faultmaven-secrets \
  --from-literal=openai-api-key=sk-proj-...
```

### 3. Use Separate Secrets Files

```bash
# Create a secrets directory that's gitignored
mkdir -p ~/.faultmaven/secrets
echo 'sk-proj-...' > ~/.faultmaven/secrets/openai_api_key

# Reference in code
with open(os.path.expanduser('~/.faultmaven/secrets/openai_api_key')) as f:
    OPENAI_API_KEY = f.read().strip()
```

### 4. Rotate Keys Immediately After Exposure

**If a key is EVER committed to git:**

1. **Revoke the key immediately** (don't wait!)
   - OpenAI: https://platform.openai.com/api-keys
   - Anthropic: https://console.anthropic.com/settings/keys
   - Google: https://console.cloud.google.com/apis/credentials

2. **Generate a new key**

3. **Update all systems using the old key**

4. **Consider the old key compromised forever** (even after deleting from git)

---

## 🔍 How to Check All Repositories

Run this script to scan ALL repositories for exposed keys:

```bash
#!/bin/bash
# scan-all-repos.sh

REPOS=(
  "/home/swhouse/product/faultmaven"
  "/home/swhouse/product/faultmaven-dashboard"
  "/home/swhouse/product/faultmaven-copilot"
  "/home/swhouse/product/.github"
)

for repo in "${REPOS[@]}"; do
  echo "=== Scanning $repo ==="
  cd "$repo" || continue

  # Search git history for API key patterns
  git log --all --patch | grep -i "sk-proj\|sk-pro\|AIza\|gsk_\|fw_\|hf_" && \
    echo "⚠️  FOUND POTENTIAL API KEY IN $repo" || \
    echo "✅ No API keys found in $repo"

  # Search current files
  grep -r "sk-proj\|sk-pro\|AIza\|gsk_\|fw_\|hf_" . \
    --exclude-dir=".git" \
    --exclude-dir="node_modules" \
    --exclude-dir="__pycache__" \
    --exclude-dir=".venv" && \
    echo "⚠️  FOUND API KEY IN CURRENT FILES" || \
    echo "✅ No API keys in current files"

  echo ""
done
```

---

## 📋 Incident Response Checklist

When an API key is exposed:

- [ ] **Immediately revoke the exposed key** (do not delay!)
- [ ] **Generate a new key** with same permissions
- [ ] **Update all systems** using the old key
- [ ] **Identify how the key was exposed** (git commit, screenshot, logs, etc.)
- [ ] **Remove the exposure vector** (delete file, purge git history, remove from logs)
- [ ] **Add prevention measures** (pre-commit hooks, better .gitignore, team training)
- [ ] **Document the incident** (what happened, how it was fixed, lessons learned)
- [ ] **Review all other keys** (assume if one was exposed, others might be too)
- [ ] **Monitor for unauthorized API usage** (check billing, API logs)

---

## 🎓 Team Training

### Key Principles

1. **API keys are like passwords** - Never share them, never commit them
2. **Git history is permanent** - Deleting a file doesn't delete history
3. **Private repos aren't secure** - Treat all repos as if they're public
4. **When in doubt, don't commit** - Ask for review if unsure
5. **Rotate keys regularly** - Even if not exposed, rotate every 90 days

### Common Mistakes

❌ "I'll just commit this temporarily and remove it later"
✅ Never commit secrets, even temporarily

❌ "The repo is private, so it's safe"
✅ Private repos can become public, be breached, or have access expanded

❌ "I'll redact the key in a future commit"
✅ Git history retains ALL past versions permanently

❌ "I'll just put the key in a comment"
✅ Comments are code - never put secrets in code

---

## 📞 Additional Resources

- **GitHub Secret Scanning:** https://docs.github.com/en/code-security/secret-scanning
- **GitGuardian:** https://www.gitguardian.com/ (automated secret detection)
- **OpenAI Security Best Practices:** https://platform.openai.com/docs/guides/safety-best-practices
- **OWASP Secrets Management:** https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

---

## 🔄 Current Status

**Repository:** faultmaven
**Last Checked:** 2026-01-13
**Status:** ✅ No active API keys in git history (exposed key was in deleted file)
**Action Required:** Generate new OpenAI API key and update local `.env`

**Next Steps:**
1. Generate new OpenAI API key from https://platform.openai.com/api-keys
2. Update `OPENAI_API_KEY` in `.env` file
3. Test the new key with `./faultmaven.sh start`
4. Install pre-commit hooks to prevent future exposures
5. Review all other API keys (Anthropic, Google, etc.) and consider rotating them as well
