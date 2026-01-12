#!/bin/bash
# FaultMaven Test Cleanup Commands
# Date: 2026-01-09
# Purpose: Execute Phase 1 cleanup of test directory

set -e  # Exit on error

cd /home/swhouse/product/faultmaven

echo "=== FaultMaven Test Cleanup - Phase 1 ==="
echo ""

# Verify we're in the right directory
if [ ! -f "pyproject.toml" ]; then
    echo "ERROR: Not in faultmaven directory!"
    exit 1
fi

echo "Step 1: Verify obsolete files exist before deletion..."
if [ -d "tests/unit/phase_handlers" ]; then
    echo "✓ tests/unit/phase_handlers/ exists (7 OBSOLETE files)"
    ls -1 tests/unit/phase_handlers/OBSOLETE_*.py 2>/dev/null | wc -l
else
    echo "⚠️  tests/unit/phase_handlers/ not found"
fi

if [ -d "tests/unit/security" ]; then
    echo "✓ tests/unit/security/ exists (empty directory)"
else
    echo "⚠️  tests/unit/security/ not found"
fi

if [ -d "tests/integration/ooda" ]; then
    echo "✓ tests/integration/ooda/ exists (empty directory)"
else
    echo "⚠️  tests/integration/ooda/ not found"
fi

if [ -d "tests/unit/quality" ]; then
    echo "✓ tests/unit/quality/ exists (2 unused utility files)"
    ls -1 tests/unit/quality/*.py 2>/dev/null | wc -l
else
    echo "⚠️  tests/unit/quality/ not found"
fi

echo ""
read -p "Continue with deletion? (y/N) " -n 1 -r
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

echo ""
echo "Step 2: Delete obsolete phase handler tests..."
if [ -d "tests/unit/phase_handlers" ]; then
    rm -rf tests/unit/phase_handlers/
    echo "✓ Deleted tests/unit/phase_handlers/"
else
    echo "⚠️  tests/unit/phase_handlers/ already deleted"
fi

echo ""
echo "Step 3: Delete empty security directory..."
if [ -d "tests/unit/security" ]; then
    rm -rf tests/unit/security/
    echo "✓ Deleted tests/unit/security/"
else
    echo "⚠️  tests/unit/security/ already deleted"
fi

echo ""
echo "Step 4: Delete empty ooda directory..."
if [ -d "tests/integration/ooda" ]; then
    rm -rf tests/integration/ooda/
    echo "✓ Deleted tests/integration/ooda/"
else
    echo "⚠️  tests/integration/ooda/ already deleted"
fi

echo ""
echo "Step 5: Check if quality utilities are used..."
if [ -d "tests/unit/quality" ]; then
    echo "Searching for imports of benchmark_queries or quality_scorer..."
    USAGE=$(grep -r "benchmark_queries\|quality_scorer" faultmaven/ tests/ 2>/dev/null || true)

    if [ -z "$USAGE" ]; then
        echo "✓ No imports found - safe to delete"
        rm -rf tests/unit/quality/
        echo "✓ Deleted tests/unit/quality/"
    else
        echo "⚠️  Found usage - NOT deleting:"
        echo "$USAGE"
        echo ""
        echo "Manual review required for tests/unit/quality/"
    fi
else
    echo "⚠️  tests/unit/quality/ already deleted"
fi

echo ""
echo "Step 6: Verify cleanup..."
source .venv/bin/activate
TEST_COUNT=$(pytest tests --collect-only -q 2>&1 | tail -1 | grep -oE '[0-9]+ tests collected' || echo "collection failed")
echo "Test collection: $TEST_COUNT"

echo ""
echo "Step 7: Run unit tests to verify no breakage..."
echo "(This may take a few minutes)"
pytest tests/unit/ -q --tb=no 2>&1 | tail -20

echo ""
echo "=== Cleanup Complete ==="
echo ""
echo "Next Steps:"
echo "1. Review test results above"
echo "2. Commit cleanup changes: git add tests/ && git commit -m 'Clean up obsolete test files'"
echo "3. Proceed to Phase 2: Fix architecture tests"
echo "   See: docs/working/COMPREHENSIVE_TEST_REVIEW_2026-01-09.md"
echo ""
