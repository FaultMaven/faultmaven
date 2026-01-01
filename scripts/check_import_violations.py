#!/usr/bin/env python3
"""
Import Linter Violation Checker for CI/CD

This script runs import-linter and compares the results against the established
baseline to detect new architectural violations.

Usage:
    python scripts/check_import_violations.py

Exit codes:
    0 - No new violations detected (OK to merge)
    1 - New violations detected (block merge)
    2 - Critical contracts broken (block merge immediately)

Baseline (updated 2026-01-01 after Phase 3 Week 14-15):
    - Contract 1 (Service Independence): 0 violations (FIXED via DI Container)
    - Contract 2 (Services → API): 0 violations (MUST REMAIN 0)
    - Contract 3 (Models → Services): 0 violations (MUST REMAIN 0)

All contracts MUST remain at 0 violations. Any new violation blocks merge.
"""

import subprocess
import sys
import re
from typing import Dict, Tuple


# Baseline violations (updated in Phase 3 Week 14-15)
# ALL contracts must remain at 0 violations after DI Container implementation
BASELINE = {
    "Service layer independence": 0,  # Fixed in Week 14-15 via DI Container
    "Services cannot import API layer": 0,  # CRITICAL: must remain 0
    "Models cannot import services": 0,  # CRITICAL: must remain 0
}

# All contracts are now critical (must remain at 0)
CRITICAL_CONTRACTS = [
    "Service layer independence",  # Now critical (was fixed in Week 14-15)
    "Services cannot import API layer",
    "Models cannot import services",
]


def run_import_linter() -> Tuple[str, int]:
    """
    Run import-linter and capture output.

    Returns:
        Tuple of (output string, exit code)
    """
    # Try .venv/bin/lint-imports first, then fallback to system lint-imports
    import os
    venv_lint_imports = os.path.join(os.path.dirname(__file__), "../.venv/bin/lint-imports")

    lint_import_cmd = venv_lint_imports if os.path.exists(venv_lint_imports) else "lint-imports"

    try:
        result = subprocess.run(
            [lint_import_cmd, "--config", ".importlinter"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        print("❌ ERROR: import-linter timed out after 120 seconds")
        sys.exit(2)
    except FileNotFoundError:
        print("❌ ERROR: lint-imports command not found")
        print("   Install with: pip install import-linter")
        sys.exit(2)


def parse_contract_results(output: str) -> Dict[str, int]:
    """
    Parse import-linter output to extract contract violation counts.

    Args:
        output: The full output from import-linter

    Returns:
        Dictionary mapping contract name to violation count
    """
    results = {}

    # Extract the summary section
    if "Contracts:" in output:
        # Parse "Contracts: X kept, Y broken" line
        contracts_line = re.search(r"Contracts:\s+(\d+)\s+kept,\s+(\d+)\s+broken", output)
        if contracts_line:
            kept = int(contracts_line.group(1))
            broken = int(contracts_line.group(2))
            print(f"📊 Contract Summary: {kept} kept, {broken} broken")

    # Parse individual contract results
    # Look for patterns like "Service layer independence BROKEN" or "... KEPT"
    contract_pattern = r"^(.+?)\s+(BROKEN|KEPT)\s*$"

    for line in output.split("\n"):
        match = re.match(contract_pattern, line.strip())
        if match:
            contract_name = match.group(1).strip()
            status = match.group(2)

            if status == "KEPT":
                results[contract_name] = 0
            elif status == "BROKEN":
                # Count violations for this contract from the detailed section
                violations = count_violations_for_contract(output, contract_name)
                results[contract_name] = violations

    return results


def count_violations_for_contract(output: str, contract_name: str) -> int:
    """
    Count the number of violations for a specific contract.

    Args:
        output: The full output from import-linter
        contract_name: Name of the contract to count violations for

    Returns:
        Number of violations detected
    """
    # Look for the "Broken contracts" section
    if "Broken contracts" not in output:
        return 0

    # Split on "Broken contracts" header to get the detailed section
    broken_section_parts = output.split("Broken contracts")
    if len(broken_section_parts) < 2:
        return 0

    broken_section = broken_section_parts[1]

    # Look for the contract header followed by dashes
    # Pattern: "\nContract Name\n---------\n"
    contract_header = f"\n{contract_name}\n"
    if contract_header not in broken_section:
        return 0

    # Get everything after this contract's header
    after_header = broken_section.split(contract_header)[1]

    # The contract section continues until we hit another contract header
    # (which would be another contract name followed by dashes)
    # For simplicity, just take everything after the header since there's
    # typically only one broken contract at a time
    contract_section = after_header

    # Count unique "is not allowed to import" lines
    # Each one represents a distinct violation (module A -> module B)
    violations = contract_section.count("is not allowed to import")

    return violations


def check_violations(current: Dict[str, int]) -> Tuple[bool, str]:
    """
    Check current violations against baseline.

    Args:
        current: Current violation counts by contract

    Returns:
        Tuple of (is_valid, message)
    """
    messages = []
    is_valid = True

    print("\n" + "="*60)
    print("VIOLATION COMPARISON")
    print("="*60)

    for contract_name, baseline_count in BASELINE.items():
        current_count = current.get(contract_name, 0)
        is_critical = contract_name in CRITICAL_CONTRACTS

        if current_count > baseline_count:
            # New violations detected!
            delta = current_count - baseline_count
            if is_critical:
                messages.append(
                    f"❌ CRITICAL: {contract_name}\n"
                    f"   Baseline: {baseline_count} violations\n"
                    f"   Current:  {current_count} violations\n"
                    f"   NEW VIOLATIONS: +{delta}\n"
                    f"   This contract MUST remain at 0 violations!"
                )
                is_valid = False
            else:
                messages.append(
                    f"⚠️  WARNING: {contract_name}\n"
                    f"   Baseline: {baseline_count} violations\n"
                    f"   Current:  {current_count} violations\n"
                    f"   NEW VIOLATIONS: +{delta}\n"
                    f"   Please remove new violations before merging."
                )
                is_valid = False
        elif current_count < baseline_count:
            # Violations reduced! (good news)
            delta = baseline_count - current_count
            messages.append(
                f"✅ IMPROVED: {contract_name}\n"
                f"   Baseline: {baseline_count} violations\n"
                f"   Current:  {current_count} violations\n"
                f"   VIOLATIONS FIXED: -{delta} 🎉"
            )
        else:
            # Same as baseline
            if is_critical and current_count == 0:
                messages.append(
                    f"✅ CRITICAL CONTRACT KEPT: {contract_name}\n"
                    f"   Violations: {current_count} (required: 0)"
                )
            else:
                messages.append(
                    f"➡️  UNCHANGED: {contract_name}\n"
                    f"   Violations: {current_count} (baseline: {baseline_count})"
                )

    # Check for new contracts not in baseline
    for contract_name, current_count in current.items():
        if contract_name not in BASELINE:
            messages.append(
                f"ℹ️  NEW CONTRACT: {contract_name}\n"
                f"   Violations: {current_count}\n"
                f"   (Not in baseline, will be tracked from now on)"
            )

    return is_valid, "\n\n".join(messages)


def main():
    """Main entry point."""
    print("🔍 Running import-linter architectural boundary check...")
    print("="*60)

    # Run import-linter
    output, exit_code = run_import_linter()

    # Parse results
    current_violations = parse_contract_results(output)

    if not current_violations:
        print("❌ ERROR: Could not parse import-linter output")
        print("\nOutput:")
        print(output)
        sys.exit(2)

    # Check against baseline
    is_valid, message = check_violations(current_violations)

    print(message)
    print("="*60)

    if is_valid:
        print("\n✅ SUCCESS: No new architectural violations detected")
        print("   Safe to merge!")
        sys.exit(0)
    else:
        print("\n❌ FAILURE: New architectural violations detected")
        print("   Please fix violations before merging")
        print("\nTo see detailed violations, run:")
        print("   lint-imports --config .importlinter")
        sys.exit(1)


if __name__ == "__main__":
    main()
