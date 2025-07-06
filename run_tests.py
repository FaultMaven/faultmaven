#!/usr/bin/env python3
"""Test runner script for FaultMaven backend."""

import argparse
import subprocess
import sys
import os
from pathlib import Path


def run_command(cmd, description):
    """Run a command and handle errors."""
    print(f"\n{'='*60}")
    print(f"Running: {description}")
    print(f"Command: {' '.join(cmd)}")
    print('='*60)
    
    result = subprocess.run(cmd, capture_output=False)
    
    if result.returncode != 0:
        print(f"\n❌ {description} failed with exit code {result.returncode}")
        return False
    else:
        print(f"\n✅ {description} completed successfully")
        return True


def main():
    """Main test runner function."""
    parser = argparse.ArgumentParser(description="FaultMaven Test Runner")
    parser.add_argument(
        "--unit", 
        action="store_true", 
        help="Run unit tests only"
    )
    parser.add_argument(
        "--integration", 
        action="store_true", 
        help="Run integration tests only"
    )
    parser.add_argument(
        "--security", 
        action="store_true", 
        help="Run security tests only"
    )
    parser.add_argument(
        "--api", 
        action="store_true", 
        help="Run API tests only"
    )
    parser.add_argument(
        "--coverage", 
        action="store_true", 
        help="Run tests with coverage report"
    )
    parser.add_argument(
        "--parallel", 
        action="store_true", 
        help="Run tests in parallel"
    )
    parser.add_argument(
        "--verbose", 
        action="store_true", 
        help="Verbose output"
    )
    parser.add_argument(
        "--html", 
        action="store_true", 
        help="Generate HTML coverage report"
    )
    parser.add_argument(
        "--lint", 
        action="store_true", 
        help="Run linting checks"
    )
    parser.add_argument(
        "--type-check", 
        action="store_true", 
        help="Run type checking"
    )
    parser.add_argument(
        "--all", 
        action="store_true", 
        help="Run all tests and checks"
    )
    
    args = parser.parse_args()
    
    # Change to project root
    project_root = Path(__file__).parent
    os.chdir(project_root)
    
    success = True
    
    # Linting
    if args.lint or args.all:
        print("\n🔍 Running linting checks...")
        
        # Black formatting check
        if not run_command(
            ["black", "--check", "--diff", "faultmaven", "tests"],
            "Code formatting check (black)"
        ):
            success = False
        
        # Flake8 linting
        if not run_command(
            ["flake8", "faultmaven", "tests"],
            "Code linting (flake8)"
        ):
            success = False
        
        # Import sorting check
        if not run_command(
            ["isort", "--check-only", "--diff", "faultmaven", "tests"],
            "Import sorting check (isort)"
        ):
            success = False
    
    # Type checking
    if args.type_check or args.all:
        print("\n🔍 Running type checking...")
        if not run_command(
            ["mypy", "faultmaven"],
            "Type checking (mypy)"
        ):
            success = False
    
    # Test execution
    if not (args.lint or args.type_check):
        print("\n🧪 Running tests...")
        
        # Build pytest command
        pytest_cmd = ["pytest"]
        
        if args.unit:
            pytest_cmd.extend(["-m", "unit"])
        elif args.integration:
            pytest_cmd.extend(["-m", "integration"])
        elif args.security:
            pytest_cmd.extend(["-m", "security"])
        elif args.api:
            pytest_cmd.extend(["-m", "api"])
        
        if args.coverage or args.all:
            pytest_cmd.extend([
                "--cov=faultmaven",
                "--cov-report=term-missing",
                "--cov-report=xml"
            ])
            
            if args.html:
                pytest_cmd.append("--cov-report=html:htmlcov")
        
        if args.parallel:
            pytest_cmd.extend(["-n", "auto"])
        
        if args.verbose:
            pytest_cmd.append("-v")
        
        if not run_command(pytest_cmd, "Test execution"):
            success = False
    
    # Security checks
    if args.all:
        print("\n🔒 Running security checks...")
        
        # Bandit security linting
        if not run_command(
            ["bandit", "-r", "faultmaven"],
            "Security linting (bandit)"
        ):
            success = False
        
        # Safety dependency check
        if not run_command(
            ["safety", "check"],
            "Dependency security check (safety)"
        ):
            success = False
    
    # Summary
    print("\n" + "="*60)
    if success:
        print("🎉 All checks passed successfully!")
        sys.exit(0)
    else:
        print("❌ Some checks failed. Please review the output above.")
        sys.exit(1)


if __name__ == "__main__":
    main() 