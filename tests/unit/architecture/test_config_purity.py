"""
Config Purity Guardrails Tests

Enforces the deployment-agnostic core principle: only config/settings.py and
the composition root may read environment variables. Services, core, and API
handlers must have zero deployment-specific branching.

This test fails if forbidden patterns (os.getenv, load_dotenv) exist outside
allowed modules, making violations visible and preventing new ones.

Allowed modules:
    - faultmaven/config/settings.py (main configuration)
    - faultmaven/main.py (bootstrap/composition root)

Forbidden directories (must be pure):
    - faultmaven/services/
    - faultmaven/core/
    - faultmaven/api/
"""

import ast
import re
from pathlib import Path
from typing import NamedTuple

import pytest


class Violation(NamedTuple):
    """Represents a config purity violation."""

    file: str
    line: int
    pattern: str
    content: str


class ConfigPurityScanner:
    """
    Scans for environment variable access and dotenv loading in forbidden modules.

    Uses both regex (for fast scanning) and AST (for accurate detection) to find:
    - os.getenv() calls
    - os.environ access (dict-style and .get())
    - load_dotenv() calls
    - dotenv imports followed by usage
    """

    # Directories that must remain pure (no env var access)
    FORBIDDEN_DIRS = (
        "faultmaven/services",
        "faultmaven/core",
        "faultmaven/api",
    )

    # Files allowed to access environment variables
    ALLOWED_FILES = (
        "faultmaven/config/settings.py",  # Main unified configuration
        "faultmaven/main.py",  # Composition root / bootstrap
    )

    # Regex patterns for quick detection
    FORBIDDEN_PATTERNS = (
        (r"\bos\.getenv\s*\(", "os.getenv()"),
        (r"\bos\.environ\s*\[", "os.environ[]"),
        (r"\bos\.environ\.get\s*\(", "os.environ.get()"),
        (r"\bload_dotenv\s*\(", "load_dotenv()"),
    )

    def __init__(self, project_root: Path):
        self.project_root = project_root

    def scan_forbidden_directories(self) -> list[Violation]:
        """
        Scan forbidden directories for config purity violations.

        Returns a list of violations found. Empty list means the codebase
        is compliant with the deployment-agnostic core principle.
        """
        violations = []

        for forbidden_dir in self.FORBIDDEN_DIRS:
            dir_path = self.project_root / forbidden_dir
            if not dir_path.exists():
                continue

            for py_file in dir_path.rglob("*.py"):
                # Skip __pycache__ directories
                if "__pycache__" in str(py_file):
                    continue

                # Check if file is in allowlist (using relative path)
                rel_path = py_file.relative_to(self.project_root)
                if any(str(rel_path) == allowed for allowed in self.ALLOWED_FILES):
                    continue

                violations.extend(self._scan_file(py_file))

        return violations

    def _scan_file(self, file_path: Path) -> list[Violation]:
        """Scan a single file for forbidden patterns."""
        violations = []

        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return violations

        lines = content.split("\n")
        rel_path = str(file_path.relative_to(self.project_root))

        for line_no, line in enumerate(lines, 1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            for pattern, pattern_name in self.FORBIDDEN_PATTERNS:
                if re.search(pattern, line):
                    violations.append(
                        Violation(
                            file=rel_path,
                            line=line_no,
                            pattern=pattern_name,
                            content=stripped[:100],  # Truncate long lines
                        )
                    )

        return violations


class DotenvImportScanner:
    """
    Detects load_dotenv() calls at import time in non-config modules.

    This catches the pattern where dotenv is imported and called at module
    load time, which violates the principle of configuration centralization.
    """

    # Only settings.py and main.py are allowed to import and use dotenv
    ALLOWED_DOTENV_FILES = (
        "faultmaven/config/settings.py",
        "faultmaven/main.py",
    )

    def __init__(self, project_root: Path):
        self.project_root = project_root

    def scan_for_import_time_dotenv(self) -> list[Violation]:
        """
        Find modules that import and call load_dotenv at import time.

        This is particularly problematic because it means environment
        variables are loaded before the composition root has a chance
        to configure the application.
        """
        violations = []
        faultmaven_dir = self.project_root / "faultmaven"

        if not faultmaven_dir.exists():
            return violations

        for py_file in faultmaven_dir.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue

            rel_path = str(py_file.relative_to(self.project_root))

            # Skip allowed files
            if any(rel_path == allowed for allowed in self.ALLOWED_DOTENV_FILES):
                continue

            violations.extend(self._scan_for_dotenv_import(py_file, rel_path))

        return violations

    def _scan_for_dotenv_import(
        self, file_path: Path, rel_path: str
    ) -> list[Violation]:
        """Check if a file imports and uses dotenv at module level."""
        violations = []

        try:
            content = file_path.read_text(encoding="utf-8")
            tree = ast.parse(content, filename=str(file_path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            return violations

        has_dotenv_import = False
        import_line = 0

        for node in ast.walk(tree):
            # Check for 'from dotenv import load_dotenv'
            if isinstance(node, ast.ImportFrom):
                if node.module == "dotenv":
                    for alias in node.names:
                        if alias.name == "load_dotenv":
                            has_dotenv_import = True
                            import_line = node.lineno
                            break

            # Check for 'import dotenv'
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "dotenv":
                        has_dotenv_import = True
                        import_line = node.lineno
                        break

        # If dotenv is imported, check for module-level calls
        if has_dotenv_import:
            for node in ast.iter_child_nodes(tree):
                # Only check module-level expressions (not inside functions/classes)
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    call = node.value
                    # Check for load_dotenv() call
                    if (
                        isinstance(call.func, ast.Name)
                        and call.func.id == "load_dotenv"
                    ):
                        violations.append(
                            Violation(
                                file=rel_path,
                                line=node.lineno,
                                pattern="import-time load_dotenv()",
                                content=f"load_dotenv() called at module level (import at line {import_line})",
                            )
                        )
                    # Check for dotenv.load_dotenv() call
                    elif (
                        isinstance(call.func, ast.Attribute)
                        and call.func.attr == "load_dotenv"
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "dotenv"
                    ):
                        violations.append(
                            Violation(
                                file=rel_path,
                                line=node.lineno,
                                pattern="import-time dotenv.load_dotenv()",
                                content=f"dotenv.load_dotenv() called at module level (import at line {import_line})",
                            )
                        )

        return violations


def format_violations(violations: list[Violation]) -> str:
    """Format violations for readable test output."""
    if not violations:
        return "No violations found."

    lines = [f"Found {len(violations)} config purity violation(s):\n"]

    for v in violations:
        lines.append(f"  {v.file}:{v.line}")
        lines.append(f"    Pattern: {v.pattern}")
        lines.append(f"    Content: {v.content}")
        lines.append("")

    lines.append(
        "Only faultmaven/config/settings.py and faultmaven/main.py are allowed"
    )
    lines.append("to access environment variables. Services, core, and API handlers")
    lines.append("must receive configuration via dependency injection.")

    return "\n".join(lines)


class TestConfigPurity:
    """
    Test suite ensuring configuration purity in the FaultMaven codebase.

    These tests enforce the deployment-agnostic core principle by failing
    if environment variable access is found outside of allowed modules.
    """

    @pytest.fixture(scope="class")
    def project_root(self) -> Path:
        """Get the project root directory."""
        return Path(__file__).parent.parent.parent.parent

    @pytest.fixture(scope="class")
    def purity_scanner(self, project_root: Path) -> ConfigPurityScanner:
        """Create a config purity scanner."""
        return ConfigPurityScanner(project_root)

    @pytest.fixture(scope="class")
    def dotenv_scanner(self, project_root: Path) -> DotenvImportScanner:
        """Create a dotenv import scanner."""
        return DotenvImportScanner(project_root)

    @pytest.mark.architecture
    def test_no_env_access_in_services_core_api(
        self, purity_scanner: ConfigPurityScanner
    ):
        """
        Forbidden directories must not access environment variables directly.

        The following directories must remain "pure" with no deployment-specific
        code or environment variable access:

        - faultmaven/services/  (business logic layer)
        - faultmaven/core/      (domain logic layer)
        - faultmaven/api/       (API handlers layer)

        All configuration must be passed via dependency injection from the
        composition root.
        """
        violations = purity_scanner.scan_forbidden_directories()

        assert not violations, format_violations(violations)

    @pytest.mark.architecture
    def test_no_import_time_dotenv_outside_config(
        self, dotenv_scanner: DotenvImportScanner
    ):
        """
        load_dotenv() must not be called at import time in non-config modules.

        Calling load_dotenv() at module import time causes environment variables
        to be loaded before the composition root can configure the application,
        breaking the deployment-agnostic core principle.

        Only these files are allowed to use load_dotenv:
        - faultmaven/config/settings.py
        - faultmaven/main.py (composition root)
        """
        violations = dotenv_scanner.scan_for_import_time_dotenv()

        assert not violations, format_violations(violations)

    @pytest.mark.architecture
    def test_scanner_detects_known_patterns(self, project_root: Path):
        """
        Verify the scanner correctly detects all forbidden patterns.

        This is a meta-test to ensure the scanner logic is working correctly.
        """
        scanner = ConfigPurityScanner(project_root)

        # Verify patterns compile and match expected strings
        test_cases = [
            ("x = os.getenv('FOO')", "os.getenv()"),
            ("x = os.getenv( 'FOO' )", "os.getenv()"),
            ("val = os.environ['BAR']", "os.environ[]"),
            ("val = os.environ.get('BAZ')", "os.environ.get()"),
            ("load_dotenv()", "load_dotenv()"),
            ("load_dotenv(override=True)", "load_dotenv()"),
        ]

        for test_line, expected_pattern in test_cases:
            matched = False
            for pattern, pattern_name in scanner.FORBIDDEN_PATTERNS:
                if re.search(pattern, test_line):
                    assert (
                        pattern_name == expected_pattern
                    ), f"Pattern mismatch for '{test_line}': expected {expected_pattern}, got {pattern_name}"
                    matched = True
                    break
            assert matched, f"No pattern matched for: {test_line}"

    @pytest.mark.architecture
    def test_scanner_ignores_comments(self, project_root: Path, tmp_path: Path):
        """
        Verify the scanner ignores patterns in comments.

        Comments explaining the architecture should not trigger violations.
        """
        # Create a temporary test file with commented patterns
        test_file = tmp_path / "test_module.py"
        test_file.write_text(
            """
# This module doesn't use os.getenv() directly
# Instead it receives config via dependency injection
# Previously this used load_dotenv() but now uses settings

def some_function(config):
    # os.environ['OLD_WAY'] was replaced with config injection
    return config.some_value
"""
        )

        scanner = ConfigPurityScanner(tmp_path)
        # Point scanner to tmp_path which acts as project root
        scanner.FORBIDDEN_DIRS = ("",)  # Scan root
        scanner.ALLOWED_FILES = ()  # No allowlist

        # Manually scan the test file
        violations = scanner._scan_file(test_file)

        # All patterns are in comments, so no violations expected
        assert not violations, f"Comments should not trigger violations: {violations}"


class TestConfigPurityAllowlist:
    """Tests verifying the allowlist configuration is correct."""

    @pytest.fixture(scope="class")
    def project_root(self) -> Path:
        """Get the project root directory."""
        return Path(__file__).parent.parent.parent.parent

    @pytest.mark.architecture
    def test_allowed_files_exist(self, project_root: Path):
        """Verify allowed files exist in the project."""
        allowed_files = [
            "faultmaven/config/settings.py",
            "faultmaven/main.py",
        ]

        for allowed_file in allowed_files:
            file_path = project_root / allowed_file
            assert file_path.exists(), f"Allowed file does not exist: {allowed_file}"

    @pytest.mark.architecture
    def test_forbidden_directories_exist(self, project_root: Path):
        """Verify forbidden directories exist in the project."""
        forbidden_dirs = [
            "faultmaven/services",
            "faultmaven/core",
            "faultmaven/api",
        ]

        for forbidden_dir in forbidden_dirs:
            dir_path = project_root / forbidden_dir
            assert (
                dir_path.exists()
            ), f"Forbidden directory does not exist: {forbidden_dir}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
