#!/usr/bin/env python3
"""Generate OpenAPI specification from FastAPI application.

This script generates the current OpenAPI spec and optionally compares it
with the locked version to identify API changes.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, Set, Tuple

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set environment to skip service checks during spec generation
import os
os.environ['SKIP_SERVICE_CHECKS'] = 'true'

# Import FastAPI app
from faultmaven.main import app


def generate_openapi_spec() -> Dict[str, Any]:
    """Generate OpenAPI specification from FastAPI app."""
    return app.openapi()


def save_spec(spec: Dict[str, Any], output_path: Path) -> None:
    """Save OpenAPI spec to YAML file."""
    import yaml

    with open(output_path, 'w') as f:
        yaml.dump(spec, f, default_flow_style=False, sort_keys=False, indent=2)

    print(f"✅ OpenAPI spec saved to: {output_path}")


def load_yaml_spec(path: Path) -> Dict[str, Any]:
    """Load OpenAPI spec from YAML file."""
    import yaml

    if not path.exists():
        return {}

    with open(path, 'r') as f:
        return yaml.safe_load(f)


def compare_specs(current: Dict[str, Any], locked: Dict[str, Any]) -> Tuple[bool, list]:
    """Compare current spec with locked spec and identify changes.

    Returns:
        (has_breaking_changes, change_list)
    """
    changes = []
    has_breaking = False

    # Compare paths (endpoints)
    current_paths = set(current.get('paths', {}).keys())
    locked_paths = set(locked.get('paths', {}).keys())

    # Removed endpoints (BREAKING)
    removed_paths = locked_paths - current_paths
    if removed_paths:
        has_breaking = True
        for path in sorted(removed_paths):
            changes.append(f"🔴 BREAKING: Removed endpoint: {path}")

    # Added endpoints (NON-BREAKING)
    added_paths = current_paths - locked_paths
    if added_paths:
        for path in sorted(added_paths):
            changes.append(f"🟢 Added endpoint: {path}")

    # Compare common paths for method changes
    for path in sorted(current_paths & locked_paths):
        current_methods = set(current['paths'][path].keys())
        locked_methods = set(locked['paths'][path].keys())

        # Removed HTTP methods (BREAKING)
        removed_methods = locked_methods - current_methods
        if removed_methods:
            has_breaking = True
            for method in sorted(removed_methods):
                changes.append(f"🔴 BREAKING: Removed {method.upper()} method from {path}")

        # Added HTTP methods (NON-BREAKING)
        added_methods = current_methods - locked_methods
        if added_methods:
            for method in sorted(added_methods):
                changes.append(f"🟢 Added {method.upper()} method to {path}")

        # Compare request/response schemas for common methods
        for method in sorted(current_methods & locked_methods):
            method_changes = compare_method(
                current['paths'][path][method],
                locked['paths'][path][method],
                f"{method.upper()} {path}"
            )
            changes.extend(method_changes)

            # Check if any changes are breaking
            if any("BREAKING" in change for change in method_changes):
                has_breaking = True

    # Compare schemas/components
    schema_changes = compare_schemas(
        current.get('components', {}).get('schemas', {}),
        locked.get('components', {}).get('schemas', {})
    )
    changes.extend(schema_changes)

    if any("BREAKING" in change for change in schema_changes):
        has_breaking = True

    return has_breaking, changes


def compare_method(current: Dict, locked: Dict, endpoint_name: str) -> list:
    """Compare individual HTTP method schemas."""
    changes = []

    # Compare request body if present
    current_request = current.get('requestBody', {}).get('content', {})
    locked_request = locked.get('requestBody', {}).get('content', {})

    if current_request != locked_request:
        # Detailed comparison would go here
        # For now, just note that it changed
        if locked_request and not current_request:
            changes.append(f"🔴 BREAKING: Removed request body from {endpoint_name}")
        elif not locked_request and current_request:
            changes.append(f"🟡 Added request body to {endpoint_name}")

    # Compare required parameters
    current_params = current.get('parameters', [])
    locked_params = locked.get('parameters', [])

    current_required = {p['name'] for p in current_params if p.get('required', False)}
    locked_required = {p['name'] for p in locked_params if p.get('required', False)}

    # New required parameters are BREAKING
    new_required = current_required - locked_required
    if new_required:
        for param in sorted(new_required):
            changes.append(f"🔴 BREAKING: New required parameter '{param}' in {endpoint_name}")

    # Removed required parameters (changed to optional) is NON-BREAKING
    removed_required = locked_required - current_required
    if removed_required:
        for param in sorted(removed_required):
            changes.append(f"🟢 Parameter '{param}' is now optional in {endpoint_name}")

    # Compare response schemas
    current_responses = set(current.get('responses', {}).keys())
    locked_responses = set(locked.get('responses', {}).keys())

    # Removed response codes might be BREAKING depending on which ones
    removed_responses = locked_responses - current_responses
    if removed_responses:
        for code in sorted(removed_responses):
            if code in {'200', '201', '204'}:  # Success codes
                changes.append(f"🔴 BREAKING: Removed {code} response from {endpoint_name}")
            else:
                changes.append(f"🟡 Removed {code} response from {endpoint_name}")

    return changes


def compare_schemas(current: Dict, locked: Dict) -> list:
    """Compare schema definitions."""
    changes = []

    current_schemas = set(current.keys())
    locked_schemas = set(locked.keys())

    # Removed schemas (BREAKING if used in API)
    removed = locked_schemas - current_schemas
    if removed:
        for schema in sorted(removed):
            changes.append(f"🔴 BREAKING: Removed schema: {schema}")

    # Added schemas (NON-BREAKING)
    added = current_schemas - locked_schemas
    if added:
        for schema in sorted(added):
            changes.append(f"🟢 Added schema: {schema}")

    # Compare common schemas for field changes
    for schema in sorted(current_schemas & locked_schemas):
        current_props = current[schema].get('properties', {})
        locked_props = locked[schema].get('properties', {})

        current_required = set(current[schema].get('required', []))
        locked_required = set(locked[schema].get('required', []))

        # New required fields are BREAKING
        new_required = current_required - locked_required
        if new_required:
            for field in sorted(new_required):
                changes.append(f"🔴 BREAKING: New required field '{field}' in schema {schema}")

        # Removed required fields (now optional) is NON-BREAKING
        removed_required = locked_required - current_required
        if removed_required:
            for field in sorted(removed_required):
                changes.append(f"🟢 Field '{field}' is now optional in schema {schema}")

        # Removed fields entirely (BREAKING)
        removed_fields = set(locked_props.keys()) - set(current_props.keys())
        if removed_fields:
            for field in sorted(removed_fields):
                changes.append(f"🔴 BREAKING: Removed field '{field}' from schema {schema}")

        # Added fields (NON-BREAKING)
        added_fields = set(current_props.keys()) - set(locked_props.keys())
        if added_fields:
            for field in sorted(added_fields):
                changes.append(f"🟢 Added field '{field}' to schema {schema}")

    return changes


def main():
    """Main function."""
    # Paths
    project_root = Path(__file__).parent.parent
    locked_spec_path = project_root / "docs" / "api" / "openapi.locked.yaml"
    current_spec_path = project_root / "docs" / "api" / "openapi.current.yaml"

    # Generate current spec
    print("Generating current OpenAPI specification...")
    current_spec = generate_openapi_spec()

    # Save current spec
    save_spec(current_spec, current_spec_path)

    # Load locked spec if it exists
    if locked_spec_path.exists():
        print(f"\nComparing with locked spec: {locked_spec_path}")
        locked_spec = load_yaml_spec(locked_spec_path)

        has_breaking, changes = compare_specs(current_spec, locked_spec)

        if changes:
            print("\n" + "=" * 80)
            print("API CHANGES DETECTED")
            print("=" * 80)

            for change in changes:
                print(change)

            print("\n" + "=" * 80)

            if has_breaking:
                print("\n⚠️  BREAKING CHANGES DETECTED")
                print("The API has breaking changes that may affect existing clients.")
                print("Consider:")
                print("  1. Creating a new API version (v2)")
                print("  2. Providing migration guide for clients")
                print("  3. Updating the locked spec only after review")
                return 1
            else:
                print("\n✅ NON-BREAKING CHANGES")
                print("The API changes are backward compatible.")
                print("You can update the locked spec with:")
                print(f"  cp {current_spec_path} {locked_spec_path}")
                return 0
        else:
            print("\n✅ No API changes detected")
            print("Current API matches locked specification.")
            return 0
    else:
        print(f"\n⚠️  No locked spec found at: {locked_spec_path}")
        print("Creating initial locked spec...")
        save_spec(current_spec, locked_spec_path)
        print("\n✅ Initial locked spec created")
        return 0


if __name__ == "__main__":
    sys.exit(main())
