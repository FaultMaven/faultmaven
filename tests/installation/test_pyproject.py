"""Tests for pyproject.toml configuration."""

import tomllib
from pathlib import Path


def test_pyproject_toml_exists():
    """Test that pyproject.toml exists."""
    pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
    assert pyproject_path.exists(), "pyproject.toml must exist"


def test_pyproject_toml_valid():
    """Test that pyproject.toml is valid TOML."""
    pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    assert "project" in data, "pyproject.toml must have [project] section"
    assert "name" in data["project"], "project must have name"
    assert data["project"]["name"] == "faultmaven", "project name must be faultmaven"


def test_optional_dependencies_structure():
    """Test that optional dependencies are properly structured."""
    pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    assert "project" in data
    assert "optional-dependencies" in data["project"]

    # Verify expected dependency groups exist
    expected_groups = ["enterprise", "test", "dev"]
    for group in expected_groups:
        assert (
            group in data["project"]["optional-dependencies"]
        ), f"Missing dependency group: {group}"


def test_core_dependencies_present():
    """Test that core dependencies are present."""
    pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    assert "project" in data
    assert "dependencies" in data["project"]

    # Verify key dependencies
    deps = data["project"]["dependencies"]
    dep_names = [dep.split(">=")[0].split("[")[0] for dep in deps]

    required_deps = ["fastapi", "uvicorn", "sqlalchemy", "pydantic"]
    for req_dep in required_deps:
        assert req_dep in dep_names, f"Missing required dependency: {req_dep}"


def test_test_dependencies_include_code_quality():
    """Test that test dependencies include code quality tools."""
    pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    test_deps = data["project"]["optional-dependencies"]["test"]
    # Extract package names, handling version specifiers: >=, ==, <=, ~=, etc.
    dep_names = [
        dep.split(">=")[0]
        .split("==")[0]
        .split("<=")[0]
        .split("~=")[0]
        .split("[")[0]
        .strip()
        for dep in test_deps
    ]

    # Code quality tools should be in test dependencies for CI
    code_quality_tools = ["black", "isort", "flake8"]
    for tool in code_quality_tools:
        assert tool in dep_names, f"Missing code quality tool in test deps: {tool}"
