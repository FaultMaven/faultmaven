#!/bin/bash
#
# Git History Cleanup Script - RSA Key Exposure Remediation
#
# This script removes the exposed RSA private key from git history
# for incident INCIDENT-2026-01-23-001
#
# WARNING: This rewrites git history. Coordinate with your team before running.
#
# Usage:
#   ./scripts/security/cleanup_exposed_keys_from_history.sh [--dry-run]
#

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DRY_RUN=false

# Parse arguments
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo -e "${YELLOW}Running in DRY RUN mode - no changes will be made${NC}\n"
fi

# Check if we're in a git repository
if ! git -C "$REPO_ROOT" rev-parse --git-dir > /dev/null 2>&1; then
    echo -e "${RED}Error: Not in a git repository${NC}"
    exit 1
fi

cd "$REPO_ROOT"

echo -e "${GREEN}==================================================================${NC}"
echo -e "${GREEN}Git History Cleanup - RSA Key Exposure Remediation${NC}"
echo -e "${GREEN}Incident: INCIDENT-2026-01-23-001${NC}"
echo -e "${GREEN}==================================================================${NC}"
echo ""

# Show current status
echo -e "${YELLOW}Current repository status:${NC}"
git log --oneline --since="2026-01-22" | head -10
echo ""

# Warning
echo -e "${RED}WARNING: This operation will rewrite git history!${NC}"
echo -e "${RED}         All team members will need to re-clone or reset their repos.${NC}"
echo ""

if [[ "$DRY_RUN" == "false" ]]; then
    read -p "Are you sure you want to proceed? (yes/no): " -r
    echo
    if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# Backup current state
echo -e "${GREEN}Step 1: Creating backup branch${NC}"
BACKUP_BRANCH="backup-before-history-cleanup-$(date +%Y%m%d-%H%M%S)"
if [[ "$DRY_RUN" == "false" ]]; then
    git branch "$BACKUP_BRANCH"
    echo "  ✓ Created backup branch: $BACKUP_BRANCH"
else
    echo "  [DRY RUN] Would create backup branch: $BACKUP_BRANCH"
fi
echo ""

# Method 1: Using git filter-repo (recommended)
if command -v git-filter-repo &> /dev/null; then
    echo -e "${GREEN}Step 2: Removing exposed key using git-filter-repo${NC}"

    # The key pattern to remove (first line of the exposed key)
    KEY_PATTERN="MIIEpAIBAAKCAQEAr0+UcafV6BzHPkDETQ11"

    if [[ "$DRY_RUN" == "false" ]]; then
        # Create expressions file for filter-repo
        cat > /tmp/filter-expressions.txt <<EOF
# Remove hardcoded RSA private key from test file
regex:TEST_PRIVATE_KEY = """-----BEGIN RSA PRIVATE KEY-----.*?-----END RSA PRIVATE KEY-----"""==>TEST_PRIVATE_KEY, TEST_PUBLIC_KEY = _generate_test_rsa_keypair()
regex:TEST_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----"""==>
EOF

        # Run git-filter-repo
        git filter-repo \
            --replace-text /tmp/filter-expressions.txt \
            --force

        rm /tmp/filter-expressions.txt
        echo "  ✓ Removed exposed key from history using git-filter-repo"
    else
        echo "  [DRY RUN] Would run: git filter-repo --replace-text /tmp/filter-expressions.txt --force"
    fi

# Method 2: Using BFG Repo-Cleaner (alternative)
elif command -v bfg &> /dev/null; then
    echo -e "${GREEN}Step 2: Removing exposed key using BFG Repo-Cleaner${NC}"

    # Create a file with the key pattern to remove
    echo "$KEY_PATTERN" > /tmp/exposed-key-pattern.txt

    if [[ "$DRY_RUN" == "false" ]]; then
        bfg --replace-text /tmp/exposed-key-pattern.txt
        git reflog expire --expire=now --all
        git gc --prune=now --aggressive
        rm /tmp/exposed-key-pattern.txt
        echo "  ✓ Removed exposed key from history using BFG"
    else
        echo "  [DRY RUN] Would run: bfg --replace-text /tmp/exposed-key-pattern.txt"
    fi

# Method 3: Manual git filter-branch (fallback - slow but works)
else
    echo -e "${YELLOW}Warning: Neither git-filter-repo nor BFG found. Using git filter-branch (slow).${NC}"
    echo -e "${GREEN}Step 2: Removing exposed key using git filter-branch${NC}"

    if [[ "$DRY_RUN" == "false" ]]; then
        git filter-branch --force --index-filter \
            "git rm --cached --ignore-unmatch tests/unit/modules/auth/domain/services/test_jwt_token_generator.py" \
            --prune-empty --tag-name-filter cat -- --all

        echo "  ✓ Removed file from history using git filter-branch"
        echo "  ⚠  Note: You'll need to manually re-add the file with the fixed version"
    else
        echo "  [DRY RUN] Would run: git filter-branch --force --index-filter ..."
    fi
fi
echo ""

# Verify cleanup
echo -e "${GREEN}Step 3: Verifying cleanup${NC}"
if git log --all --pretty=format: --name-only | grep -q "MIIEpAIBAAKCAQEAr0+UcafV6BzHPkDETQ11"; then
    echo -e "  ${RED}✗ Exposed key pattern still found in history!${NC}"
    echo "  Manual cleanup may be required."
else
    echo "  ✓ Exposed key pattern not found in history"
fi
echo ""

# Show what needs to be done next
echo -e "${GREEN}==================================================================${NC}"
echo -e "${GREEN}Next Steps:${NC}"
echo -e "${GREEN}==================================================================${NC}"
echo ""
echo "1. Review the changes:"
echo "   git log --oneline --since='2026-01-22'"
echo ""
echo "2. Re-add the fixed test file (if using filter-branch method):"
echo "   git add tests/unit/modules/auth/domain/services/test_jwt_token_generator.py"
echo "   git commit -m 'fix(security): Use dynamic RSA key generation in tests'"
echo ""
echo "3. Force push to remote (COORDINATE WITH TEAM FIRST!):"
echo "   git push origin --force --all"
echo "   git push origin --force --tags"
echo ""
echo "4. Notify team members to re-clone or reset:"
echo "   git fetch origin"
echo "   git reset --hard origin/main  # WARNING: Loses local changes"
echo ""
echo "5. Backup branch created (in case you need to revert):"
echo "   $BACKUP_BRANCH"
echo ""
echo -e "${YELLOW}Important: Coordinate with your team before force pushing!${NC}"
echo ""

# Final checks
if [[ "$DRY_RUN" == "false" ]]; then
    echo -e "${GREEN}Cleanup complete!${NC}"
    echo ""
    echo "Run this script with --dry-run to preview changes before executing."
else
    echo -e "${YELLOW}DRY RUN complete - no changes were made.${NC}"
    echo "Remove --dry-run flag to execute the cleanup."
fi
